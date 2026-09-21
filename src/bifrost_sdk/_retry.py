"""When to try again, and how long to wait.

Both of these were learned the hard way against a real gateway, in two codebases that had
each implemented them wrong in the same way.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

#: Statuses where the same request may succeed later. Everything else is the caller's
#: mistake, and retrying it only spends the budget. Public, because a caller wrapping this
#: client needs to tell "we gave up after retrying" apart from "we never tried" when it
#: reports a failure, and re-deriving the set is how the two copies of it drifted.
RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

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


def backoff(attempt: int, base: float) -> float:
    """Exponential backoff for failures that carry no advice of their own."""
    return base * (2**attempt)
