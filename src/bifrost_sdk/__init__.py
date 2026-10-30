"""An OpenAI-compatible async client, with Bifrost extras.

    from bifrost_sdk import Bifrost, ModelProfile, Options

    async with Bifrost("http://gateway/v1", model="provider/model") as bf:
        text = await bf.chat("summarise this")
        data = await bf.json("extract the fields", schema=SCHEMA)
        async for delta in bf.stream("write a story"):
            ...
        raw = await bf.complete("summarise this")  # usage, tool calls, finish reason

Any OpenAI-compatible endpoint is served. ``auth=`` authenticates it (a bearer key, a
refreshed Google token, AWS SigV4: :mod:`bifrost_sdk.auth`) and ``profile=`` shapes each
request from what the model accepts (:class:`ModelProfile`). A client built with
``gateway="bifrost"`` has Bifrost's extras; on any other gateway they raise
:class:`BifrostOnlyError`. Per-request gateway behaviour (stored prompt, MCP scope, session,
spend attribution, no content logging) is an :class:`Options` passed as ``options=``. MCP
tools are listed with
``bf.tools(...)``, run with ``bf.execute_tool(call)`` and read back from the gateway's log
with ``bf.mcp_logs(since)``; ``bf.mcp`` manages the MCP clients themselves. Gateway
administration lives in :mod:`bifrost_sdk.admin`.
"""

from bifrost_sdk._client import GATEWAYS, Bifrost, Gateway, Messages
from bifrost_sdk._errors import (
    AuthenticationError,
    BadRequestError,
    BifrostError,
    BifrostOnlyError,
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
from bifrost_sdk._profile import ModelProfile
from bifrost_sdk._retry import RETRYABLE
from bifrost_sdk.auth import (
    AUTH_STRATEGIES,
    AuthStrategy,
    AwsSigV4Auth,
    BearerAuth,
    GoogleAuth,
    auth_strategy,
)
from bifrost_sdk.headers import NO_GATEWAY_TOOLS, Options

__all__ = [
    "AUTH_STRATEGIES",
    "GATEWAYS",
    "NO_GATEWAY_TOOLS",
    "RETRYABLE",
    "AuthStrategy",
    "AuthenticationError",
    "AwsSigV4Auth",
    "BadRequestError",
    "BearerAuth",
    "Bifrost",
    "BifrostError",
    "BifrostOnlyError",
    "CircuitOpen",
    "ConflictError",
    "EmptyResponse",
    "Gateway",
    "GatewayError",
    "GoogleAuth",
    "InvalidJSON",
    "MCPClient",
    "MCPClientConfig",
    "MCPConnection",
    "MCPLog",
    "Messages",
    "ModelProfile",
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
    "auth_strategy",
]
