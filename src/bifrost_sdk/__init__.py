"""A small async client for a Bifrost LLM gateway.

    from bifrost_sdk import Bifrost, Options

    async with Bifrost("http://gateway/v1", model="gemini/gemini-3.6-flash") as bf:
        text = await bf.chat("summarise this")
        data = await bf.json("extract the fields", schema=SCHEMA)
        async for delta in bf.stream("write a story"):
            ...
        raw = await bf.complete("summarise this")  # usage, tool calls, finish reason

Per-request gateway behaviour (stored prompt, MCP scope, session, spend attribution, no
content logging) is an :class:`Options` passed as ``options=``. MCP tools are listed with
``bf.tools(...)``, run with ``bf.execute_tool(call)`` and read back from the gateway's log
with ``bf.mcp_logs(since)``; ``bf.mcp`` manages the MCP clients themselves. Gateway
administration lives in :mod:`bifrost_sdk.admin`.
"""

from bifrost_sdk._client import Bifrost, Messages
from bifrost_sdk._errors import (
    AuthenticationError,
    BadRequestError,
    BifrostError,
    CircuitOpen,
    ConflictError,
    EmptyResponse,
    GatewayError,
    InvalidJSON,
    NotFoundError,
    PermissionDeniedError,
    RateLimited,
    RateLimitedError,
    ServerError,
    UnprocessableError,
    Unreachable,
)
from bifrost_sdk._mcp import (
    MCPClient,
    MCPClientConfig,
    MCPConnection,
    MCPLog,
    ToolAnnotations,
    ToolDef,
)
from bifrost_sdk._retry import RETRYABLE
from bifrost_sdk.headers import NO_GATEWAY_TOOLS, Options

__all__ = [
    "NO_GATEWAY_TOOLS",
    "RETRYABLE",
    "AuthenticationError",
    "BadRequestError",
    "Bifrost",
    "BifrostError",
    "CircuitOpen",
    "ConflictError",
    "EmptyResponse",
    "GatewayError",
    "InvalidJSON",
    "MCPClient",
    "MCPClientConfig",
    "MCPConnection",
    "MCPLog",
    "Messages",
    "NotFoundError",
    "Options",
    "PermissionDeniedError",
    "RateLimited",
    "RateLimitedError",
    "ServerError",
    "ToolAnnotations",
    "ToolDef",
    "UnprocessableError",
    "Unreachable",
]
