"""A small MCP server (streamable HTTP) for the live tests, run in-process by a fixture.

Public MCP hosts may be out of reach, and the gateway refuses to *register* a loopback server
over its open management API — so this one is declared in the gateway's ``config.json``
(``mcp.client_configs``) under :data:`CLIENT`, pointing at :data:`URL`, with
``allowed_extra_headers: ["x-user-token"]``. The tests start it, have the gateway reconnect,
and use it; nothing here is registered at run time.

``whoami`` answers with the :data:`USER_HEADER` the gateway forwarded for the caller and
refuses without one: a header-authenticated server, the per-user credential case.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

#: The gateway's name for this server (``config.json``), and where it listens.
CLIENT = "sdklocal"
HOST, PORT = "127.0.0.1", 8097
URL = f"http://{HOST}:{PORT}/mcp"
#: The per-caller header the gateway forwards to this server (``allowed_extra_headers``).
USER_HEADER = "x-user-token"

server = MCPServer(CLIENT)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> str:
    """Return ``text`` unchanged."""
    return text


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def whoami(ctx: Context) -> str:
    """The caller, as the forwarded ``x-user-token`` header names them."""
    user = (ctx.headers or {}).get(USER_HEADER)
    if not user:
        raise ValueError(f"unauthenticated: no {USER_HEADER} header")
    return user


def _listening() -> bool:
    with contextlib.closing(socket.socket()) as probe:
        return probe.connect_ex((HOST, PORT)) == 0


@contextlib.asynccontextmanager
async def serving() -> AsyncIterator[None]:
    """Serve on :data:`PORT` for the duration; a server already listening there is reused."""
    if _listening():
        yield
        return
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", lifespan="on")
    runner = uvicorn.Server(config)
    task = asyncio.create_task(runner.serve())
    while not runner.started:
        if task.done():
            task.result()  # surfaces why it could not start
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        runner.should_exit = True
        await task
