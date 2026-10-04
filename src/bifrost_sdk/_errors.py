"""What can go wrong, and whether trying again could help.

Every exception carries ``retryable``: whether the *same* call may succeed if made again
later. Callers above this client (the contracts' ``AgentError.of``) read that attribute
rather than re-deriving it from class names or statuses, which is how two copies of the
rule drift apart.
"""

from __future__ import annotations

from typing import Any

import httpx

from bifrost_sdk._retry import RETRYABLE, retry_after

#: At or above this status the gateway reported a problem.
ERROR_STATUS = 400
#: At or above this status the problem is the gateway's (or its upstream's), not the caller's.
SERVER_ERROR = 500
_RATE_LIMITED = 429
#: How much of an error body to carry on the exception, for logs and debugging.
_BODY_EXCERPT = 500


class BifrostError(Exception):
    """Base class. ``details`` carries whatever the gateway said, for logs and tests."""

    #: Whether the same call may succeed if made again later. False unless a subclass knows
    #: better: an error nobody classified is not one to retry optimistically.
    retryable: bool = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class Unreachable(BifrostError):
    """The gateway could not be contacted. Retrying may work."""

    retryable = True


class RateLimited(BifrostError):
    """The gateway asked for less traffic.

    Deliberately not a subclass of :class:`GatewayError`: a 429 means the gateway is healthy
    and saying so. Callers that trip a circuit breaker on failure must not trip it on this,
    or backpressure becomes an outage.
    """

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class GatewayError(BifrostError):
    """The gateway answered with an error status, or with a body this client cannot use.

    ``status`` is the HTTP status (``None`` for a 2xx whose body was unusable). The subclasses
    below name the common statuses so a caller can ``except NotFoundError`` instead of
    comparing numbers; anything they do not name is a plain ``GatewayError``.
    """

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        status = details.get("status")
        self.status: int | None = status if isinstance(status, int) else None
        self.retryable = self.status in RETRYABLE


class BadRequestError(GatewayError):
    """400: the request is malformed or refused as asked (a prompt over the context length,
    a tool call out of the key's scope). Sending it again gets the same answer."""


class AuthenticationError(GatewayError):
    """401: no key, or a key the gateway does not know."""


class PermissionDeniedError(GatewayError):
    """403: a known key that may not do this (model, tool or budget outside its envelope)."""


class NotFoundError(GatewayError):
    """404: no such model, route or resource."""


class ConflictError(GatewayError):
    """409: the request collides with the gateway's current state. In :data:`RETRYABLE`, so
    ``retryable`` and the completion retries treat it as transient."""


class UnprocessableError(GatewayError):
    """422: well-formed, but the gateway cannot act on it (a body that fails validation)."""


class ServerError(GatewayError):
    """5xx: the gateway or the provider behind it failed. ``retryable`` for 500/502/503/504;
    not for statuses such as 501 that no retry will change."""


class CircuitOpen(BifrostError):
    """The breaker is open: recent calls failed, so this one was not sent.

    ``retry_after`` is how many seconds are left on the open circuit. Deliberately not a
    :class:`GatewayError` — nothing was asked of the gateway, so nothing it said applies.
    """

    #: The same call will be sent once the window has passed.
    retryable = True

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


class RateLimitedError(RateLimited):
    """429, as raised by this client: :class:`RateLimited` under the name the other typed
    errors follow, with ``status`` like theirs.

    Not a :class:`GatewayError` either, for the reason :class:`RateLimited` gives: code that
    catches ``GatewayError`` to count failures has never seen a 429 there, and making it see
    one now would turn backpressure into an outage in every such caller.
    """

    def __init__(self, message: str, *, retry_after: float | None = None, **details: Any) -> None:
        super().__init__(message, retry_after=retry_after, **details)
        self.status = _RATE_LIMITED


#: The 4xx statuses with a class of their own. 429 is :class:`RateLimitedError`, every 5xx
#: is :class:`ServerError`, and anything else is a plain :class:`GatewayError`.
_BY_STATUS: dict[int, type[GatewayError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableError,
}


def from_response(response: httpx.Response) -> RateLimitedError | GatewayError:
    """The error an error-status response means.

    The whole body is parsed and only an excerpt carried: Gemini's quota reply puts
    "Please retry in 28.9s." at index 483 of 751, so truncating before parsing lost the delay.
    """
    body = response.text
    excerpt = body[:_BODY_EXCERPT]
    status = response.status_code
    if status == _RATE_LIMITED:
        return RateLimitedError(
            "gateway rate limited the request",
            retry_after=retry_after(response.headers, body),
            status=_RATE_LIMITED,
            body=excerpt,
        )
    kind = ServerError if status >= SERVER_ERROR else _BY_STATUS.get(status, GatewayError)
    return kind(f"gateway returned {status}", status=status, body=excerpt)
