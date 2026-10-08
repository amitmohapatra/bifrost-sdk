"""The gateway the live tests run against: one they start themselves, or the one
``BIFROST_URL`` names.

The gateway refuses to register a loopback, private-network or link-local MCP server for an
unauthenticated caller ("set an admin password to allow this"), and the live tests register
the local servers of ``local_mcp.py``. So they act as the gateway's admin, the way an operator
does: dashboard auth on (``auth_config`` with an admin username and password), a login
(``POST /api/session/login``), and the session token it returns sent as
``Authorization: Bearer`` on ``/api/*`` — the SDK's ``admin_token=`` / ``Admin(token=)``.

:func:`started` runs a separate, throwaway gateway from a local ``bifrost-http`` binary: its
own app directory, a free loopback port, and a ``config.json`` derived from an operator's
(its providers) with what the tests need on top. :func:`existing` uses a running one, logging
in when admin credentials are given.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

HOST: Final = "127.0.0.1"
#: How long a started gateway gets to answer ``/health``, and to exit when stopped.
START_SECONDS: Final = 60.0
STOP_SECONDS: Final = 15.0
#: The admin account a started gateway is configured with (its password is random).
ADMIN_USERNAME: Final = "bfsdklive"


@dataclass(frozen=True)
class Gateway:
    """Where the tests reach the gateway, and the admin session token for ``/api/*``
    (``None``: the management API is used unauthenticated)."""

    url: str
    admin_token: str | None


def login(url: str, username: str, password: str) -> str:
    """The admin session token for ``username``, as the dashboard's login hands it out."""
    origin = httpx.URL(url).copy_with(path="/", query=None)
    response = httpx.post(
        origin.join("/api/session/login"),
        json={"username": username, "password": password},
        timeout=30.0,
    )
    response.raise_for_status()
    token = response.cookies.get("token")
    if not token:
        raise RuntimeError(f"the gateway at {origin} answered the login without a session token")
    return token


def existing(url: str, username: str | None, password: str | None) -> Gateway:
    """The gateway at ``url``; logged in as its admin when both credentials are given."""
    if username and password:
        return Gateway(url, login(url, username, password))
    return Gateway(url, None)


def gateway_config(base: dict[str, Any], app_dir: Path, password: str) -> dict[str, Any]:
    """``base`` (an operator's ``config.json``: its providers, framework settings) with what
    the live tests need on top:

    * its stores in ``app_dir``, never the base gateway's databases;
    * dashboard auth on, with :data:`ADMIN_USERNAME` and ``password`` — so the tests may
      register loopback MCP servers, as the admin;
    * no MCP clients: the tests register their own and remove them;
    * request logging on (the MCP logs are read back), and inference auth not enforced: the
      MCP-registry tests list and run tools without a virtual key, as an unscoped caller; the
      tests of keys create their own.
    """
    config = json.loads(json.dumps(base))  # a deep copy
    for store, name in (("config_store", "config.db"), ("logs_store", "logs.db")):
        config[store] = {
            "enabled": True,
            "type": "sqlite",
            "config": {"path": str(app_dir / name)},
        }
    client = config.setdefault("client", {})
    client["enable_logging"] = True
    client["enforce_auth_on_inference"] = False
    config.setdefault("mcp", {})["client_configs"] = []
    config["auth_config"] = {
        "admin_username": ADMIN_USERNAME,
        "admin_password": password,
        "is_enabled": True,
    }
    return config


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _healthy(origin: str) -> bool:
    try:
        return httpx.get(f"{origin}/health", timeout=2.0).is_success
    except httpx.HTTPError:
        return False


@contextlib.contextmanager
def started(binary: Path, base_config: Path | None, app_dir: Path) -> Iterator[Gateway]:
    """Run ``binary`` on a free port with :func:`gateway_config` in ``app_dir``, logged in as
    its admin; stop it when the block exits. Its output is ``app_dir/gateway.log``."""
    base = json.loads(base_config.read_text()) if base_config else {}
    password = secrets.token_urlsafe(24)
    app_dir.mkdir(parents=True, exist_ok=True)
    config_path = app_dir / "config.json"
    # Provider keys and the admin password: readable by this user only.
    config_path.touch(mode=0o600)
    config_path.write_text(json.dumps(gateway_config(base, app_dir, password), indent=2))

    port = _free_port()
    origin = f"http://{HOST}:{port}"
    command = [str(binary), "-app-dir", str(app_dir), "-host", HOST, "-port", str(port)]
    with (app_dir / "gateway.log").open("wb") as log:
        process = subprocess.Popen(
            [*command, "-log-style", "json"],
            cwd=app_dir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + START_SECONDS
        while not _healthy(origin):
            if process.poll() is not None:
                raise RuntimeError(f"the gateway exited ({process.returncode}): {log.name}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the gateway did not start in {START_SECONDS}s: {log.name}")
            time.sleep(0.25)
        yield Gateway(f"{origin}/v1", login(origin, ADMIN_USERNAME, password))
    finally:
        process.terminate()
        try:
            process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def from_environment(scratch: Path) -> contextlib.AbstractContextManager[Gateway] | None:
    """The gateway the environment asks for (see the README, Development), or ``None``.

    ``BIFROST_LIVE_GATEWAY_BIN`` starts one (its base ``config.json`` from
    ``BIFROST_LIVE_GATEWAY_CONFIG``; its app directory a new one under
    ``BIFROST_LIVE_GATEWAY_DIR``, else ``scratch``). Otherwise ``BIFROST_URL`` (or
    ``BIFROST_LIVE_URL``) names a running one, and ``BIFROST_LIVE_ADMIN_USERNAME`` /
    ``BIFROST_LIVE_ADMIN_PASSWORD`` its admin.
    """
    env = os.environ
    if binary := env.get("BIFROST_LIVE_GATEWAY_BIN"):
        base = env.get("BIFROST_LIVE_GATEWAY_CONFIG")
        parent = Path(env.get("BIFROST_LIVE_GATEWAY_DIR") or scratch)
        parent.mkdir(parents=True, exist_ok=True)
        app_dir = Path(tempfile.mkdtemp(prefix="bifrost-live-", dir=parent))
        return started(Path(binary), Path(base) if base else None, app_dir)
    url = env.get("BIFROST_URL") or env.get("BIFROST_LIVE_URL")
    if not url:
        return None
    gateway = existing(
        url, env.get("BIFROST_LIVE_ADMIN_USERNAME"), env.get("BIFROST_LIVE_ADMIN_PASSWORD")
    )
    return contextlib.nullcontext(gateway)
