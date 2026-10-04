"""The gateway's management API (``/api/*``), shared by ``Bifrost.mcp`` and ``Admin``.

``/api`` is a sibling of the ``/v1`` inference base, not a child of it, and authenticates
with a bearer token rather than a virtual key — so it gets its own httpx client rooted at
the gateway's origin.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from bifrost_sdk._errors import ERROR_STATUS, GatewayError, from_response, unreachable

#: Connect timeout ceiling: a gateway that does not accept a connection in this long is down.
CONNECT_TIMEOUT = 5.0
#: Connection pool bounds, explicit rather than httpx's defaults, so the numbers a process
#: runs with are written down somewhere. One client is shared by every agent in a process:
#: the cap bounds what that process can open against the gateway, and a request beyond it
#: waits for a free connection (up to the pool timeout) instead of opening another.
MAX_CONNECTIONS = 100
#: Idle connections kept for reuse. Enough for a steady stream of calls not to reconnect.
MAX_KEEPALIVE_CONNECTIONS = 20
#: How long an idle connection is kept. httpx's default of five seconds is often shorter than
#: the gap between an agent's turns, which then pay a fresh TCP (and TLS) handshake each.
KEEPALIVE_EXPIRY = 30.0


def limits() -> httpx.Limits:
    """The connection pool every client this package builds uses."""
    return httpx.Limits(
        max_connections=MAX_CONNECTIONS,
        max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=KEEPALIVE_EXPIRY,
    )


def timeouts(timeout: float) -> httpx.Timeout:
    """``timeout`` for reading, writing and waiting on the pool; connecting is capped lower."""
    return httpx.Timeout(
        connect=min(CONNECT_TIMEOUT, timeout), read=timeout, write=timeout, pool=timeout
    )


def origin(base_url: str) -> str:
    """The scheme and host of a gateway URL, dropping any path such as ``/v1``."""
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else base_url.rstrip("/")


def management_client(base_url: str, token: str | None, timeout: float) -> httpx.AsyncClient:
    """An httpx client for ``/api/*`` on the gateway that ``base_url`` points at."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=origin(base_url),
        headers=headers,
        timeout=timeouts(timeout),
        limits=limits(),
    )


class ManagementAPI:
    """JSON over ``/api/*`` with this client's error vocabulary. Not retried: most are writes."""

    __slots__ = ("_http",)

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        """One call. Returns parsed JSON, or ``None`` for an empty body."""
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            raise unreachable(exc) from exc
        if response.status_code >= ERROR_STATUS:
            raise from_response(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            # The gateway's UI catch-all answers unknown /api paths with 200 and HTML.
            raise GatewayError(
                f"{method} {path} returned a non-JSON body", body=response.text[:300]
            ) from exc

    async def items(self, path: str, keys: tuple[str, ...], **params: Any) -> list[dict[str, Any]]:
        """A list endpoint's items, unwrapped from the first envelope key present."""
        payload = await self.get(path, **params)
        if isinstance(payload, dict):
            payload = next((payload[k] for k in keys if k in payload), None)
        return list(payload or [])

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params or None)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self.request("POST", path, json=body)

    async def put(self, path: str, body: Any = None) -> Any:
        return await self.request("PUT", path, json=body)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)


class Resource:
    """One ``/api/*`` area, bound to a :class:`ManagementAPI`."""

    __slots__ = ("_api",)

    def __init__(self, api: ManagementAPI) -> None:
        self._api = api
