"""When to try again, and how long to wait.

Both of these were learned the hard way against a real gateway, in two codebases that had
each implemented them wrong in the same way.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

#: Statuses where the same request may succeed later. Everything else is the caller's
#: mistake, and retrying it only spends the budget. Public, because a caller wrapping this
#: client needs to tell "we gave up after retrying" apart from "we never tried" when it
#: reports a failure, and re-deriving the set is how the two copies of it drifted.
RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: The longest wait between two attempts, whoever chose it. A ``Retry-After`` beyond it is
#: honoured only up to here: a caller holding a request for minutes on a gateway's say-so is
#: worse off than one that fails and decides for itself, and every client in the platform
#: (the harness's runs client, the memory SDK) caps at the same thirty seconds.
MAX_WAIT = 30.0

#: Some providers put the delay in the error *body* rather than the header — Gemini answers
#: "Please retry in 59.18s". A client that only reads the header sees nothing and falls back
#: to a backoff measured in milliseconds against a window measured in a minute.
_IN_BODY = re.compile(r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.I)


def retry_after(headers: Any = None, body: str = "") -> float | None:
    """Seconds to wait, from the ``Retry-After`` header or the response body.

    A rate limit says *when* to come back, and that is almost never the exponential backoff.
    """
    raw = headers.get("retry-after") if headers is not None else None
    if raw:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
    match = _IN_BODY.search(body or "")
    return float(match.group(1)) if match else None


class Jitter(Protocol):
    """Anything with ``random.Random.random``: a seeded generator in tests, the shared one
    otherwise."""

    def random(self) -> float: ...


#: The process's jitter source. Not a security boundary, only a way to spread callers out.
_JITTER = random.Random()


def backoff(attempt: int, base: float, rng: Jitter | None = None) -> float:
    """Exponential backoff with full jitter, for failures that carry no advice of their own.

    A uniform draw from ``[0, min(MAX_WAIT, base * 2**attempt))``. The bare exponential was
    the same number in every process: when a gateway restarts, every agent that failed in
    the same second retries in the same second, and the stampede is the next outage. Full
    jitter spreads the retries across the whole window — the lowest total load of the
    standard schemes, at the price of an occasional retry that comes sooner than the
    exponential would have.
    """
    ceiling = min(MAX_WAIT, base * (2**attempt))
    return (rng or _JITTER).random() * ceiling
