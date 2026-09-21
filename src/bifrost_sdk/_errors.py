"""What can go wrong, and whether trying again could help."""

from __future__ import annotations

from typing import Any


class BifrostError(Exception):
    """Base class. ``details`` carries whatever the gateway said, for logs and tests."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class Unreachable(BifrostError):
    """The gateway could not be contacted. Retrying may work."""


class RateLimited(BifrostError):
    """The gateway asked for less traffic.

    Deliberately not a subclass of :class:`GatewayError`: a 429 means the gateway is healthy
    and saying so. Callers that trip a circuit breaker on failure must not trip it on this,
    or backpressure becomes an outage.
    """

    def __init__(self, message: str, *, retry_after: float | None = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class GatewayError(BifrostError):
    """The gateway answered with an error status."""


class EmptyResponse(BifrostError):
    """The model returned no text.

    Reasoning models spend the output budget on thinking before emitting anything, so too
    small a ``max_tokens`` comes back 200 OK with an empty string and ``finish_reason=length``.
    Measured against gemini-3.6-flash: "Reply with exactly: OK" consumed 57 reasoning tokens.
    Returning "" would push a silently degraded answer into every call site.
    """


class InvalidJSON(BifrostError):
    """``json()`` asked for structured output and could not parse what came back."""
