# Configuration

`bifrost-sdk` reads **no environment variable and no file**
([ADR 0001](adr/0001-constructor-arguments-only.md)). Every setting is a constructor argument,
so the application decides where values come from. "Automatic?" says whether you get the
behaviour without passing anything.

## `Bifrost(base_url, *, ...)`

```python
from bifrost_sdk import Bifrost

bf = Bifrost(
    "http://gateway:8080/v1",
    model="provider/model",
    api_key=virtual_key,  # from your own settings; never printed, never a provider key
    timeout=30.0,
    max_retries=3,
)
```

| Argument | Default | Automatic? | What it does | Example |
|---|---|---|---|---|
| `base_url` | required | no | The gateway's inference endpoint. `/mcp` and `/api/*` are derived from its origin. | `"http://gateway:8080/v1"` |
| `model` | `None` | no | Default model, as the gateway names it (`provider/model`). Every verb takes `model=` to override it; a call with neither raises `ValueError`. | `model="provider/model"` |
| `api_key` | `None` | no | The gateway **virtual key**, sent as `Authorization: Bearer <key>` on `/v1` and `/mcp`. Never a provider key. | `api_key=settings.bifrost_virtual_key` |
| `timeout` | `60.0` | yes | Seconds per request (read, write, waiting for a pooled connection). The connect timeout is `min(5.0, timeout)`. `complete(timeout=)` overrides it for one call. | `timeout=30.0` |
| `max_retries` | `2` | yes | Retries after the first attempt, for `chat`, `json` and `complete` only. `0` disables retries. | `max_retries=0` |
| `backoff_seconds` | `0.5` | yes | Base of the jittered exponential backoff when a failure carries no delay of its own: retry *n* waits a uniform draw from `[0, backoff_seconds * 2**n)`, capped at 30 s. | `backoff_seconds=1.0` |
| `max_tokens` | `2048` | yes | Output budget `chat`, `json` and `stream` send when the call passes none. `complete` sends only what you pass. | `max_tokens=4096` |
| `circuit_failure_threshold` | `5` | yes | Consecutive failed calls (transport failures and 5xx; never a 4xx or 429) that open the circuit breaker. `0` disables it. | `circuit_failure_threshold=0` |
| `circuit_open_seconds` | `30.0` | yes | How long an open circuit fails calls without sending them. | `circuit_open_seconds=10` |
| `admin_token` | the `api_key` | yes | Bearer token for `/api/*` (`bf.mcp`, `mcp_logs`). A gateway with admin auth on closes `/api` to a virtual key, so pass one there. | `admin_token=settings.bifrost_admin_token` |
| `client` | a pooled `httpx.AsyncClient` | yes | Your own client for `/v1` and `/mcp` (a proxy, a custom transport). One you pass is not closed by `aclose()`. | `client=httpx.AsyncClient(base_url=..., transport=...)` |
| `admin_client` | a pooled `httpx.AsyncClient` | yes | Your own client for `/api/*`. | `admin_client=...` |

`Bifrost` is an async context manager. Outside `async with`, call `await bf.aclose()`.

## `Admin(base_url, *, ...)`

```python
from bifrost_sdk.admin import Admin

async with Admin("http://gateway:8080", token=admin_token) as admin:
    keys = await admin.vk.list()
```

| Argument | Default | Automatic? | What it does |
|---|---|---|---|
| `base_url` | required | no | The gateway's origin, or its `/v1` URL. |
| `token` | `None` | no | Sent as `Authorization: Bearer <token>`. |
| `timeout` | `60.0` | yes | Seconds per request, as for `Bifrost`. |
| `client` | a pooled `httpx.AsyncClient` | yes | Your own client for `/api/*`. |

## Per request: `Options`

What the gateway does for one call (a stored prompt, the MCP scope of `execute_tool`, the
session, spend attribution, no content logging, any other header) is an `Options` passed as
`options=`. Unset fields send nothing. Every field and its header:
[api.md](api.md#per-request-options).

Always sent, with no option to turn it off: the deny-all MCP scope on every completion
(`NO_GATEWAY_TOOLS`, [ADR 0003](adr/0003-completions-never-get-gateway-tools.md)).

## Fixed in code

| Constant | Value | What it bounds |
|---|---|---|
| `_api.MAX_CONNECTIONS` | `100` | open connections per client the SDK builds; a request beyond it waits for one, up to `timeout` |
| `_api.MAX_KEEPALIVE_CONNECTIONS`, `_api.KEEPALIVE_EXPIRY` | `20`, `30` s | idle connections kept for reuse, and for how long |
| `_retry.MAX_WAIT` | `30` s | the longest wait before a retry, whoever asked for it |
| `RETRYABLE` | `408, 409, 425, 429, 500, 502, 503, 504` | the statuses completions retry |
| `_client.CODE_MODE_READS` | `4` | Code Mode declarations read at a time when listing tools |
| `_client.MAX_LOG_PAGE` | `1000` | the largest `mcp_logs(limit=)` |

Pass your own `client=` for other connection limits.

## The names the other repos use

The SDK reads none of these, but every Trellis repo that uses it reads the same two names and
passes them in, and the live tests here read the first:

| Variable | Read by | Passed as |
|---|---|---|
| `BIFROST_URL` | agent-harness, agent-memory-service, this repo's live tests | `base_url` (the `/v1` URL) |
| `BIFROST_VIRTUAL_KEY` | agent-harness, agent-memory-service | `api_key` |

## The live tests' variables

Only `make test-live` (`pytest -m live`) reads these. A plain `make test` deselects the live
tests, because `BIFROST_URL` is often set in a shell that did not mean to register clients on
that gateway.

| Variable | Default | What it is |
|---|---|---|
| `BIFROST_URL` | unset: the live tests skip | the gateway's `/v1` URL; `BIFROST_LIVE_URL` is still read when it is unset |
| `BIFROST_LIVE_MODEL` | unset: the tests that complete skip | a `provider/model` the gateway serves |
| `BIFROST_LIVE_MCP_URL` | the public DeepWiki MCP server | an MCP server that publishes no annotations |
| `BIFROST_LIVE_ANNOTATED_MCP_URL` | the public Context7 MCP server | an MCP server that publishes annotations |

The examples read nothing: they run against an in-process fake gateway
([examples/](../examples/README.md)).
