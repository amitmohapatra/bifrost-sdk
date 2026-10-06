# ADR 0002: Retry only what is replayable; only a sick gateway trips the breaker

**Status:** accepted · **Date:** 2026-10-04 (recorded 2026-10-06) · **Version:** 0.3.0

## Context
Two incidents shaped the failure policy. A run that met 17 rate limits opened a circuit
breaker that counted every error, and the next 62 calls failed instantly without a request
ever being sent: "slow down" had become "stop". And a bare exponential backoff is the same
number in every process, so when the gateway came back, every agent that failed in the same
second retried in the same second. A few over-long prompts from one caller, or one mis-keyed
caller, also must not take the gateway away from every other agent sharing the client.

## Decision
- **Retried:** only `chat`, `json` and `complete`, on a status in `RETRYABLE` (`408, 409,
  425, 429, 500, 502, 503, 504`) or a transport failure, up to `max_retries`. Streams (the
  caller has seen part of the text), tool executions (side effects) and management calls
  (mostly writes) are never retried.
- **The wait:** the gateway's own delay when it gives one (`Retry-After`, or "retry in Ns" in
  the body), as given; otherwise full jitter, uniform in `[0, backoff_seconds * 2**attempt)`;
  never more than 30 seconds.
- **The breaker counts failed calls, not attempts,** and only failures that say the gateway
  is unwell: transport failures, 5xx, a 2xx with an unusable body. No 4xx counts, a 429
  included, and a 4xx neither extends nor resets a streak. After
  `circuit_failure_threshold` (default 5; 0 disables it) the client fails fast with
  `CircuitOpen` for `circuit_open_seconds`, then lets one call through.
- **Typed, not breaking:** each common status has a `GatewayError` subclass; `RateLimited`
  is deliberately not a `GatewayError`; every error carries `retryable`.

## Consequences
- An outage costs one timeout, not one per request.
- Code that counts `GatewayError` as failure never counts a 429.
- Callers above the SDK (the contracts' `AgentError.of`, the harness) read `retryable`
  instead of re-deriving the rule.
- The diagrams are in [ARCHITECTURE.md](../ARCHITECTURE.md#retry-policy).
