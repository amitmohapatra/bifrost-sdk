# bifrost-sdk

A small async client for a [Bifrost](https://github.com/maximhq/bifrost) LLM gateway.

The gateway holds the provider keys. This client knows a URL and a model *name*, and imports
no provider SDK — which is what lets the same code run against OpenAI, Anthropic, Gemini or a
local model by changing a string.

```python
from bifrost_sdk import Bifrost

async with Bifrost("http://gateway:8080/v1", model="gemini/gemini-3.6-flash") as bf:
    text = await bf.chat("summarise this")                    # -> str
    data = await bf.json("extract the fields", schema=SCHEMA)  # -> dict
    async for delta in bf.stream("write a story"):             # -> AsyncIterator[str]
        print(delta, end="")
```

## The four methods

| Method | Returns | Use it when |
| --- | --- | --- |
| `chat(prompt)` | `str` | you want the answer |
| `json(prompt, schema=...)` | `dict` | you want the answer parsed, and checked if the provider ignores `response_format` |
| `stream(prompt)` | `AsyncIterator[str]` | time-to-first-token matters. Not retried: a partly consumed stream cannot be replayed without showing duplicate text |
| `complete(prompt)` | `dict` | you need the whole response — usage, tool calls, `finish_reason` — because you are building a framework on the gateway rather than calling one |

`chat`, `json` and `stream` are opinionated: they always send a `max_tokens` and a
`temperature`. `complete` is not — it sends only the keys you passed, because a caller
building its own request should not find keys in the body it never set.

`ping()` says whether the gateway answers, and never raises.

## Errors

```
BifrostError
├── Unreachable     the gateway could not be contacted
├── RateLimited     429; carries .retry_after
├── GatewayError    the gateway answered with an error status
├── EmptyResponse   200 OK with no text (see below)
└── InvalidJSON     json() could not parse the reply
```

`RateLimited` is deliberately **not** a `GatewayError`. A 429 means the gateway is healthy and
saying so; a caller that trips a circuit breaker on failure must not trip it on backpressure,
or "slow down" becomes "stop". Measured on a real run: 17 rate limits opened a breaker and the
next 62 calls failed instantly without a request ever being sent.

`EmptyResponse` exists because reasoning models spend the output budget on thinking before
emitting anything, so too small a `max_tokens` returns 200 OK with `""` and
`finish_reason="length"`. Measured against gemini-3.6-flash: "Reply with exactly: OK" consumed
57 reasoning tokens. Returning `""` would push a silently degraded answer into every call site.

## Retries

Retried: `408, 409, 425, 429, 500, 502, 503, 504` (exported as `bifrost.RETRYABLE`). Everything
else is the caller's mistake and is raised on the first attempt — retrying a 400 only spends
the budget.

The wait comes from `Retry-After` when the gateway sends one, as a delay or an HTTP date, and
from the *body* when it does not: Gemini answers "Please retry in 59.18s" with no header at
all, and a client that reads only the header falls back to a backoff measured in milliseconds
against a window measured in a minute.

## Install

```bash
uv add bifrost-sdk        # or: pip install bifrost-sdk
```

Requires Python 3.12+. The only dependency is `httpx`.
