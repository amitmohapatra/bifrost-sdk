"""What can go wrong, and whether trying again could help."""

from __future__ import annotations

from typing import Any

import httpx

from bifrost_sdk._retry import retry_after

#: At or above this status the gateway reported a problem.
ERROR_STATUS = 400
_RATE_LIMITED = 429
#: How much of an error body to carry on the exception, for logs and debugging.
_BODY_EXCERPT = 500


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


class CircuitOpen(BifrostError):
    """The breaker is open: recent calls failed, so this one was not sent.

    ``retry_after`` is how many seconds are left on the open circuit. Deliberately not a
    :class:`GatewayError` — nothing was asked of the gateway, so nothing it said applies.
    """

    def __init__(self, message: str, *, retry_after: float = 0.0, **details: Any) -> None:
        super().__init__(message, retry_after_seconds=retry_after, **details)
        self.retry_after = retry_after


class EmptyResponse(BifrostError):
    """The model returned no text.

    Reasoning models spend the output budget on thinking before emitting anything, so too
    small a ``max_tokens`` comes back 200 OK with an empty string and ``finish_reason=length``.
    Measured against gemini-3.6-flash: "Reply with exactly: OK" consumed 57 reasoning tokens.
    Returning "" would push a silently degraded answer into every call site.
    """


class InvalidJSON(BifrostError):
    """``json()`` asked for structured output and could not parse what came back."""


def unreachable(exc: httpx.TransportError) -> Unreachable:
    """A transport failure (connect, timeout, protocol) as this client's error."""
    return Unreachable(f"gateway unreachable ({type(exc).__name__})")


def from_response(response: httpx.Response) -> RateLimited | GatewayError:
    """The error an error-status response means.

    The whole body is parsed and only an excerpt carried: Gemini's quota reply puts
    "Please retry in 28.9s." at index 483 of 751, so truncating before parsing lost the delay.
    """
    body = response.text
    excerpt = body[:_BODY_EXCERPT]
    if response.status_code == _RATE_LIMITED:
        return RateLimited(
            "gateway rate limited the request",
            retry_after=retry_after(response.headers, body),
            status=_RATE_LIMITED,
            body=excerpt,
        )
    return GatewayError(
        f"gateway returned {response.status_code}", status=response.status_code, body=excerpt
    )
