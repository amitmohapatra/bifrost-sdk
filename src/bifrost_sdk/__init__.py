"""A small client for a Bifrost LLM gateway.

    from bifrost_sdk import Bifrost

    async with Bifrost("http://gateway/v1", model="gemini/gemini-3.6-flash") as bf:
        text = await bf.chat("summarise this")
        data = await bf.json("extract the fields", schema=SCHEMA)
        async for delta in bf.stream("write a story"):
            ...
        raw  = await bf.complete("summarise this")    # usage, tool calls, finish reason

Anything the gateway does *per request* — inject a stored prompt, scope which MCP servers are
visible, bill a customer, keep a turn out of the logs — is a chain:

    await (bf.prompt(PROMPT_ID).mcp(clients=["memory"]).session(thread).chat("what changed?"))

The gateway holds the provider keys; this client knows a URL and a model name.
"""

from bifrost_sdk._breaker import Breaker
from bifrost_sdk._call import Call
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
from bifrost_sdk._headers import Options
from bifrost_sdk._retry import RETRYABLE

__all__ = [
    "RETRYABLE",
    "Bifrost",
    "BifrostError",
    "Breaker",
    "Call",
    "CircuitOpen",
    "EmptyResponse",
    "GatewayError",
    "InvalidJSON",
    "Messages",
    "Options",
    "RateLimited",
    "Unreachable",
]
