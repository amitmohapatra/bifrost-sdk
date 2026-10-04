"""Fail fast while the gateway is down, instead of paying the timeout every call.

This lived in two places — the memory service's LLM adapter and the harness's model client —
in two repositories, as the same thirty lines with the same field names. Both were written
against the same gateway, and both learned the same lesson from the same incident: a 429 is
the gateway working and asking for less, not the gateway being broken. Counting rate limits
toward the breaker turns backpressure into an outage — measured on a real run, 17 rate limits
opened the circuit and the next 62 calls failed instantly without a request being sent.

Transport, retries and rate-limit parsing moved here for exactly this reason. The breaker was
left behind on the argument that only the caller knows what a failure costs it, which is true
of the *thresholds* and not of the mechanism — so the thresholds stay arguments and the
mechanism stops being copied.

What counts follows from the same argument: a 400 is the gateway working too. Counting
every error status meant a handful of over-long prompts — each refused in milliseconds with
a 400 by a perfectly healthy gateway — would open the circuit for every agent sharing the
client. So only the failures that say the *gateway* is unwell count: transport
failures (it could not be reached, or timed out) and 5xx (it, or its provider, broke).
Every 4xx is a verdict on the request, not on the gateway, and that includes 401 and 403:
a bad or under-privileged key is one caller's misconfiguration, and letting it open the
circuit would let one mis-keyed agent take the gateway away from the correctly keyed ones.
"""

from __future__ import annotations

import time

from bifrost_sdk._errors import SERVER_ERROR, CircuitOpen, GatewayError, Unreachable


def counts(exc: BaseException | None) -> bool:
    """Whether ``exc`` says the gateway is unwell, and so counts toward opening the circuit.

    Transport failures and 5xx do; so does a 2xx whose body is unusable (``status`` is
    ``None``: the gateway answered, wrongly), and a failure recorded without an exception.
    Rate limits, every other 4xx, and anything that is not a gateway failure do not.
    """
    if exc is None or isinstance(exc, Unreachable):
        return True
    if isinstance(exc, GatewayError):
        return exc.status is None or exc.status >= SERVER_ERROR
    return False


class Breaker:
    """Consecutive-failure breaker. ``threshold=0`` disables it entirely."""

    __slots__ = ("consecutive_failures", "open_seconds", "open_until", "threshold")

    def __init__(self, threshold: int = 5, open_seconds: float = 30.0) -> None:
        self.threshold = threshold
        self.open_seconds = open_seconds
        self.consecutive_failures = 0
        self.open_until = 0.0

    def check(self) -> None:
        """Raise if the circuit is open, carrying how long is left on it."""
        if not self.threshold:
            return
        remaining = self.open_until - time.monotonic()
        if remaining > 0:
            raise CircuitOpen(
                "gateway circuit open",
                retry_after=round(remaining, 1),
                failures=self.consecutive_failures,
            )

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self, exc: BaseException | None = None) -> None:
        """Count a failure, if it is one that says the gateway is unwell (:func:`counts`).

        ``exc`` is the failure itself rather than a boolean, so the rules that matter — rate
        limits and other 4xx do not trip the breaker — are applied here and cannot be got
        wrong differently in each caller. A failure that does not count leaves the streak
        as it was: the gateway answered, but not with the success that would end it.
        """
        if not self.threshold or not counts(exc):
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.open_until = time.monotonic() + self.open_seconds
