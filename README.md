# bifrost-sdk

A small async client for a [Bifrost](https://github.com/maximhq/bifrost) LLM gateway.

The gateway holds the provider keys. This client knows a URL and a model *name*, and imports
no provider SDK — which is what lets the same code run against OpenAI, Anthropic, Gemini or a
local model by changing a string.

```python
from bifrost_sdk import Bifrost

async with Bifrost("http://gateway:8080/v1", model="gemini/gemini-3.6-flash") as bf:
    text = await bf.chat("summarise this")  # -> str
    data = await bf.json("extract the fields", schema=SCHEMA)  # -> dict
    async for delta in bf.stream("write a story"):  # -> AsyncIterator[str]
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

`ping()` says whether the gateway answers `/models` successfully, and never raises.

## Per-request options

Everything the gateway does for one call is selected by an `x-bf-*` header. Build an
`Options` and pass it as `options=` to any verb (or to `execute_tool`):

```python
from bifrost_sdk import Options

opts = Options(
    prompt_id="p-123",  # inject a stored prompt
    prompt_version=3,
    mcp_clients=["erp"],  # MCP scope
    mcp_tools=["erp-get_stock"],
    session_id=thread_id,
    customer_id="acme",
    content_logging=False,  # keep this turn out of the gateway's content logs
    extra={"x-tier": "batch"},  # any other header, e.g. one a routing rule matches on
)
text = await bf.chat("what changed?", options=opts)
```

`Options` is frozen; `opts.merged(session_id=...)` returns a copy. The header names are
public constants in `bifrost_sdk.headers`.

**MCP scope.** `mcp_clients=None` / `mcp_tools=None` (the default) sends no header: the
request sees every tool its virtual key allows. An **empty** sequence sends the header empty,
which the gateway treats as deny-all. Clients are named as registered (or `*`); tools as
`<client>-<tool>` or `<client>-*` — a bare tool name or a bare `*` matches nothing.

## MCP tools

The gateway injects MCP tools into completions but, outside Agent Mode, does not run them: the
caller does, so policy, audit and approval can sit in front of each call.

```python
tools = await bf.tools(clients=["erp"], only=["erp-*"])  # -> list[ToolDef]
turn = await bf.execute_tool(tool_call, options=Options(mcp_clients=["erp"]))
# turn is {"role": "tool", "content": ..., "tool_call_id": ...}, ready to append
```

- `tools(clients, only)` lists what a request scoped with the same values could execute:
  disabled clients and tools outside a client's `tools_to_execute` are left out. `ToolDef`
  carries `name` (`<client>-<tool>`), `client`, `description`, `parameters` (JSON schema),
  `code_mode` and `annotations`. The virtual key's own MCP allow-list is applied by the gateway
  at execution.
- `ToolDef.annotations` is the server's MCP `ToolAnnotations` — `read_only_hint`,
  `destructive_hint`, `idempotent_hint`, `open_world_hint` (each `bool | None`) — or `None`
  when the server published none. `GET /api/mcp/clients` drops annotations, so the listing
  joins them from the gateway's own MCP endpoint (`POST /mcp`, `tools/list`), which keeps
  them. When that endpoint is unavailable, or the server publishes none, `annotations` is
  `None` and the memory service's tool catalog is the source of a tool's risk tier.
- `execute_tool(tool_call)` takes an entry of a completion's `tool_calls`. It is not retried
  and does not count toward the circuit breaker: a tool may have side effects, and a refused
  call (out of scope, not allowed) is a 400 `GatewayError`.
- **Code Mode.** For a client with `is_code_mode_client=True`, completions see the gateway's
  meta-tools (`listToolFiles`, `readToolFile`, `getToolDocs`, `executeToolCode`) instead of
  its tools; run them with `execute_tool` like any other. Send
  `Options(parent_request_id=...)` with `executeToolCode` to find its nested calls afterwards:

```python
from datetime import UTC, datetime, timedelta

logs = await bf.mcp_logs(datetime.now(UTC) - timedelta(minutes=5), parent_request_id=run_id)
for log in logs:  # MCPLog: id, timestamp, client, tool, name, status, arguments, result, error
    ...
```

`mcp_logs(since, limit=100, parent_request_id=None)` reads `GET /api/mcp-logs` oldest first.
`since` is inclusive and the gateway writes logs asynchronously (seconds behind), so a caller
paging forward dedups by `id`.

### Managing MCP clients

```python
from bifrost_sdk import MCPClientConfig, MCPConnection

config = MCPClientConfig(
    name="erp",  # no "-": it separates client from tool in every tool name
    connection=MCPConnection(type="http", url="https://erp.example.com/mcp"),
    tools_to_execute=["*"],  # required: an empty list lets no tool run
)
await bf.mcp.add(config)
[erp] = [c for c in await bf.mcp.clients() if c.config.name == "erp"]  # -> MCPClient
await bf.mcp.update(erp, config.model_copy(update={"is_code_mode_client": True}))
await bf.mcp.remove(erp.id)
```

`update` changes name, tool allow-lists and the Code Mode flag. The gateway answers 200 to a
connection change and ignores it, so `update` refuses one: remove the client and add it
again. `tools_to_auto_execute` (Agent Mode) defaults to empty. The gateway refuses
private-network MCP targets to unauthenticated callers.

`/api/*` routes authenticate with `admin_token=` (defaulting to `api_key=`).

## Administration

Virtual keys, budgets, routing rules, stored prompts and skills are operator tooling, on a
separate client:

```python
from bifrost_sdk.admin import Admin

async with Admin("http://gateway:8080", token=ADMIN_TOKEN) as admin:
    await admin.vk.create("triage-agent", provider_configs=[...], mcp_configs=[...])
    rules = await admin.routing.rules()
```

Namespaces: `vk`, `governance`, `routing`, `prompts`, `skills`. A virtual key's
`provider_configs` and `mcp_configs` are deny-by-default: a key created without them permits
nothing.

## Errors

```
BifrostError
├── Unreachable     the gateway could not be contacted
├── RateLimited     429; carries .retry_after
├── GatewayError    the gateway answered with an error status
├── CircuitOpen     recent calls failed; this one was not sent (.retry_after)
├── EmptyResponse   200 OK with no text (see below)
└── InvalidJSON     json() could not parse the reply
```

`RateLimited` is deliberately **not** a `GatewayError`. A 429 means the gateway is healthy and
saying so; the circuit breaker does not count it, or "slow down" becomes "stop". Measured on a
real run: 17 rate limits opened a breaker and the next 62 calls failed instantly without a
request ever being sent.

`EmptyResponse` exists because reasoning models spend the output budget on thinking before
emitting anything, so too small a `max_tokens` returns 200 OK with `""` and
`finish_reason="length"`. Measured against gemini-3.6-flash: "Reply with exactly: OK" consumed
57 reasoning tokens. Returning `""` would push a silently degraded answer into every call site.

## Retries and the circuit breaker

Retried (`chat`, `json`, `complete`): `408, 409, 425, 429, 500, 502, 503, 504` (exported as
`bifrost_sdk.RETRYABLE`). Everything else is the caller's mistake and is raised on the first
attempt — retrying a 400 only spends the budget.

The wait comes from `Retry-After` when the gateway sends one, as a delay or an HTTP date, and
from the *body* when it does not: Gemini answers "Please retry in 59.18s" with no header at
all.

After `circuit_failure_threshold` consecutive failed calls (default 5; 0 disables it) the
client fails fast with `CircuitOpen` for `circuit_open_seconds`, so an outage costs one
timeout rather than one per request.

## Tests

```bash
uv run pytest                                            # unit tests, gateway mocked
BIFROST_LIVE_URL=http://localhost:8091/v1 uv run pytest -m live  # against a running gateway
```

The live tests register temporary MCP clients (`BIFROST_LIVE_MCP_URL`, default the public
DeepWiki server, which publishes no annotations; `BIFROST_LIVE_ANNOTATED_MCP_URL`, default the
public Context7 server, which does) and remove them afterwards; they skip when the gateway is
unreachable.

## Install

```bash
uv add bifrost-sdk        # or: pip install bifrost-sdk
```

Requires Python 3.12+. Dependencies: `httpx`, `pydantic`.
