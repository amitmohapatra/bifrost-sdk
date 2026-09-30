"""A small async client for a Bifrost LLM gateway.

    from bifrost_sdk import Bifrost, Options

    async with Bifrost("http://gateway/v1", model="gemini/gemini-3.6-flash") as bf:
        text = await bf.chat("summarise this")
        data = await bf.json("extract the fields", schema=SCHEMA)
        async for delta in bf.stream("write a story"):
            ...
        raw = await bf.complete("summarise this")  # usage, tool calls, finish reason

Per-request gateway behaviour (stored prompt, MCP scope, session, spend attribution, no
content logging) is an :class:`Options` passed as ``options=``.
"""

from bifrost_sdk._client import Bifrost, Messages
from bifrost_sdk._errors import (
    BifrostError,
    CircuitOpen,
    EmptyResponse,
    GatewayError,
    InvalidJSON,
    RateLimited,
    Unreachable,
)
from bifrost_sdk._retry import RETRYABLE
from bifrost_sdk.headers import Options

__all__ = [
    "RETRYABLE",
    "Bifrost",
    "BifrostError",
    "CircuitOpen",
    "EmptyResponse",
    "GatewayError",
    "InvalidJSON",
    "Messages",
    "Options",
    "RateLimited",
    "Unreachable",
]
