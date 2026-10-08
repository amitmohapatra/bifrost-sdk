"""Local MCP servers (streamable HTTP) for the live tests, so they need no public MCP host.

:func:`running` serves all three in a background thread, each on a free loopback port, for as
long as the ``with`` block lasts (the live suite holds it for the session). The tests register
them on the gateway themselves — as its admin, because the gateway refuses loopback targets to
unauthenticated callers — and remove them afterwards.

* :data:`wiki` — DeepWiki's tool and parameter names (``read_wiki_structure``,
  ``read_wiki_contents``, ``ask_wiki_question``; ``repoName``) over the fixed :data:`WIKIS`,
  and, like DeepWiki, **no annotations** on any tool.
* :data:`annotated` — every tool carries MCP annotations, and they differ (:data:`ANNOTATED`):
  a read-only lookup, an idempotent one, and a destructive one.
* :data:`local` — ``echo``, and ``whoami``, which answers with the :data:`USER_HEADER` the
  gateway forwarded for the caller and refuses without one: a header-authenticated server, the
  per-user credential case.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

HOST: Final = "127.0.0.1"
#: The per-caller header the gateway forwards to :data:`local` (``allowed_extra_headers``).
USER_HEADER: Final = "x-user-token"
#: How long :func:`running` waits for the servers to listen.
START_SECONDS: Final = 10.0

# ----------------------------------------------------------------------------- wiki

#: The repositories the wiki knows: each one's table of contents, and what it says.
WIKIS: Final[dict[str, tuple[tuple[str, ...], str]]] = {
    "maximhq/bifrost": (
        ("1 Overview", "2 Providers", "3 MCP Gateway", "4 Governance"),
        "Bifrost is an LLM gateway: one API in front of many providers and MCP servers.",
    ),
}

wiki = MCPServer("bfsdklivewiki")


def _wiki(repo: str) -> tuple[tuple[str, ...], str]:
    found = WIKIS.get(repo)
    if found is None:
        raise ValueError(f"no wiki for {repo!r}; known: {', '.join(WIKIS)}")
    return found


def structure(repo: str) -> str:
    """What ``read_wiki_structure`` answers for ``repo``."""
    return "\n".join(_wiki(repo)[0])


@wiki.tool()
def read_wiki_structure(repoName: str) -> str:
    """The table of contents of a repository's wiki."""
    return structure(repoName)


@wiki.tool()
def read_wiki_contents(repoName: str) -> str:
    """The contents of a repository's wiki."""
    return _wiki(repoName)[1]


@wiki.tool()
def ask_wiki_question(repoName: str, question: str) -> str:
    """An answer to a question about a repository, from its wiki."""
    return f"About {repoName}: {_wiki(repoName)[1]} (asked: {question})"


# ----------------------------------------------------------------------------- annotated

#: Each tool of :data:`annotated` and the annotations it publishes (the MCP wire names).
ANNOTATED: Final[dict[str, dict[str, bool]]] = {
    "resolve_library_id": {"readOnlyHint": True, "openWorldHint": False},
    "get_library_docs": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    "purge_library_cache": {"readOnlyHint": False, "destructiveHint": True},
}

annotated = MCPServer("bfsdkliveannotated")


@annotated.tool(annotations=ToolAnnotations(**ANNOTATED["resolve_library_id"]))
def resolve_library_id(libraryName: str) -> str:
    """The library ID that ``get_library_docs`` takes for a library's name."""
    return f"/{libraryName.lower()}/{libraryName.lower()}"


@annotated.tool(annotations=ToolAnnotations(**ANNOTATED["get_library_docs"]))
def get_library_docs(libraryId: str) -> str:
    """Documentation for a library, by its ID."""
    return f"Documentation for {libraryId}."


@annotated.tool(annotations=ToolAnnotations(**ANNOTATED["purge_library_cache"]))
def purge_library_cache(libraryId: str) -> str:
    """Drop every cached page of a library's documentation (irreversible)."""
    return f"purged {libraryId}"


# ----------------------------------------------------------------------------- local

local = MCPServer("bfsdklivelocal")


@local.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> str:
    """Return ``text`` unchanged."""
    return text


@local.tool(annotations=ToolAnnotations(readOnlyHint=True))
def whoami(ctx: Context) -> str:
    """The caller, as the forwarded ``x-user-token`` header names them."""
    user = (ctx.headers or {}).get(USER_HEADER)
    if not user:
        raise ValueError(f"unauthenticated: no {USER_HEADER} header")
    return user


# ----------------------------------------------------------------------------- serving


@dataclass(frozen=True)
class Servers:
    """Where each server listens (its streamable-HTTP endpoint)."""

    wiki: str
    annotated: str
    local: str


def _bound() -> socket.socket:
    """A listening socket on a free loopback port, handed to uvicorn (no port race)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, 0))
    sock.listen(128)
    return sock


@contextlib.contextmanager
def running() -> Iterator[Servers]:
    """Serve :data:`wiki`, :data:`annotated` and :data:`local` until the block exits."""
    apps = (wiki, annotated, local)
    sockets = [_bound() for _ in apps]
    runners = [
        uvicorn.Server(
            uvicorn.Config(
                server.streamable_http_app(stateless_http=True, json_response=True),
                log_level="warning",
                lifespan="on",
            )
        )
        for server in apps
    ]
    failure: list[BaseException] = []

    async def serve() -> None:
        await asyncio.gather(*(r.serve(sockets=[s]) for r, s in zip(runners, sockets, strict=True)))

    def main() -> None:
        try:
            asyncio.run(serve())
        except BaseException as exc:  # surfaced by the waiting thread
            failure.append(exc)

    thread = threading.Thread(target=main, name="local-mcp", daemon=True)
    thread.start()
    try:
        for _ in range(int(START_SECONDS / 0.05)):
            if failure:
                raise RuntimeError("the local MCP servers did not start") from failure[0]
            if all(r.started for r in runners):
                break
            threading.Event().wait(0.05)
        else:
            raise RuntimeError(f"the local MCP servers did not start in {START_SECONDS}s")
        urls = [f"http://{HOST}:{s.getsockname()[1]}/mcp" for s in sockets]
        yield Servers(*urls)
    finally:
        for runner in runners:
            runner.should_exit = True
        thread.join(timeout=START_SECONDS)
        for sock in sockets:
            sock.close()
