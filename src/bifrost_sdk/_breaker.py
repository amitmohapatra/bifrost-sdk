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
"""

from __future__ import annotations

import time

from bifrost_sdk._errors import CircuitOpen, RateLimited


class Breaker:
    """Consecutive-failure breaker. ``threshold=0`` disables it entirely."""

    __slots__ = ("consecutive_failures", "open_seconds", "open_until", "threshold")

    def __init__(self, threshold: int = 5, open_seconds: float = 30.0) -> None:
        self.threshold = threshold
        self.open_seconds = open_seconds
        self.consecutive_failures = 0
        self.open_until = 0.0

    @property
    def is_open(self) -> bool:
        return bool(self.threshold) and time.monotonic() < self.open_until

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
        """Count a failure, unless it is the gateway asking for less traffic.

        ``exc`` is the failure itself rather than a boolean, so the one rule that matters —
        rate limits do not trip the breaker — is applied here and cannot be got wrong
        differently in each caller.
        """
        if not self.threshold or isinstance(exc, RateLimited):
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.open_until = time.monotonic() + self.open_seconds
