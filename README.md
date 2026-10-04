# bifrost-sdk

A small async client for a [Bifrost](https://github.com/maximhq/bifrost) LLM gateway.

The gateway holds the provider keys. This client knows a URL, a **virtual key** and a model
*name*, and imports no provider SDK — which is what lets the same code run against OpenAI,
Anthropic, Gemini or a local model by changing a string. It covers the three things an
application asks a gateway for — completions, MCP tools, and the gateway's log of tool calls —
plus a separate operator client for virtual keys, budgets, routing rules, stored prompts and
skills.

How the pieces fit, with diagrams: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Where this fits: two ways to use Trellis

Trellis is used in one of two ways, and each block works in both:

- **Way 1, wrapped.** `from trellis import Harness; h = Harness(); agent = h.wrap(my_agent)`.
  The harness runs your agent (LangGraph, Deep Agents, OpenAI Agents SDK, Claude Agent SDK,
  a plain function) and uses every block automatically: memory context, recording and
  feedback; durable runs, the inbox, schedules and the worker in agent-runs; governance of
  tool calls; models and MCP tools through Bifrost; evals; the AG-UI and A2A surfaces.
- **Way 2, pluggable blocks.** Keep your framework untouched and import only the blocks you
  want: `trellis.memory` (`MemoryClient`), `trellis.runs` (`RunsClient`, `Worker`,
  `webhooks.verify_signature`), `trellis.contracts` (the shared types), `bifrost_sdk` (models
  and MCP tools through Bifrost), and from the harness repo `trellis.harness.governance`
  (`Governance.from_env`, `check`, `governed`), `trellis.harness.evals` (`EvalServices`,
  `evaluate`, `judge`) and `trellis.harness.a2a.remote`.

A package shipped from its own repo is top-level `trellis.X`; anything from the harness repo
is `trellis.harness.X`. `bifrost_sdk` (pip `bifrost-sdk`) is the exception: it keeps its own,
older name.

**This package** is `bifrost_sdk`, the client every model call and every MCP tool call on the
platform goes through: the gateway holds the provider keys and the MCP servers, and this
client knows a URL, a virtual key and a model name. It reads no environment variables and
imports neither `trellis.contracts` nor any agent framework.

| | What happens with `bifrost_sdk` |
|---|---|
| **Way 1, wrapped** | With `BIFROST_URL` and `BIFROST_VIRTUAL_KEY` set, the harness lists the MCP tools the key allows (`tools()`), runs each call the model makes with `execute_tool` once governance lets it, reads a Code Mode script's nested calls back with `mcp_logs`, and calls `complete` for its `ReAct` loop and its LLM judge. The memory service makes its own model calls through it as well. You write no Bifrost code; a framework's own model object points at the gateway's OpenAI-compatible endpoint. |
| **Way 2, pluggable** | Your framework runs the agent, untouched; you list the key's MCP tools, offer them to the model and run the calls it makes: |

```python
from bifrost_sdk import Bifrost

async with Bifrost(BIFROST_URL, model=MODEL, api_key=VIRTUAL_KEY) as bf:
    tools = await bf.tools()  # the MCP tools this virtual key may run
    offered = [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]
    reply = await bf.complete(question, tools=offered)
    for call in reply["choices"][0]["message"].get("tool_calls") or []:
        turn = await bf.execute_tool(call)  # after your own check, e.g. trellis.harness.governance
```

- **Choose Way 1 when** you want the gateway's MCP tools offered, governed, run and recorded
  on every run of a wrapped agent with no code.
- **Choose Way 2 when** your framework already owns the model loop and you want the gateway
  behind it: run each tool call through `trellis.harness.governance` (`Governance.check`, or
  `governed` around the call) before `execute_tool`. Or when you are building on the gateway
  rather than an agent: `chat`, `json` and `stream`, and `Admin` for virtual keys, budgets and
  routing ([Which method, when](#which-method-when)).

Harness docs: [the two ways](https://github.com/amitmohapatra/agent-harness/blob/main/README.md#two-ways-to-use-trellis) · [every page](https://github.com/amitmohapatra/agent-harness/blob/main/docs/README.md) ·
blocks: [governance](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/governance.md), [evaluation](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/evaluation.md), [contracts](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/contracts.md) ·
recipes: [LangGraph](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/langgraph.md), [OpenAI Agents SDK](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/openai-agents.md),
[Claude Agent SDK](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/claude-agent-sdk.md).

## Install

```bash
uv add bifrost-sdk        # or: pip install bifrost-sdk
```

Requires Python 3.12+. Dependencies: `httpx`, `pydantic`.

## Quickstart

```python
from bifrost_sdk import Bifrost

async with Bifrost(
    "http://gateway:8080/v1", model="gemini/gemini-3.6-flash", api_key=VIRTUAL_KEY
) as bf:
    text = await bf.chat("summarise this")  # -> str
    data = await bf.json("extract the fields", schema=SCHEMA)  # -> dict
    async for delta in bf.stream("write a story"):  # -> AsyncIterator[str]
        print(delta, end="")
```

`Bifrost` is an async context manager; outside `async with`, call `await bf.aclose()`.

## Configuration

The package reads **no environment variables** and no config files: everything is a
constructor argument, so the application decides where values come from. (The only
environment variables in this repository are the opt-in live tests', see [Tests](#tests).)

### `Bifrost(base_url, *, ...)`

| Argument | Default | Meaning |
| --- | --- | --- |
| `base_url` | required | The gateway's inference endpoint, e.g. `http://gateway:8080/v1`. `/mcp` and `/api/*` are derived from its origin. |
| `model` | `None` | Default model, as the gateway names it (`provider/model`). Every verb takes `model=` to override it; a call with neither raises `ValueError`. |
| `api_key` | `None` | The gateway **virtual key** (see below). Never a provider key. |
| `timeout` | `60.0` | Seconds per request (read, write, and waiting for a pooled connection). The connect timeout is `min(5.0, timeout)`. |
| `max_retries` | `2` | Retries after the first attempt, for `chat`, `json` and `complete`. |
| `backoff_seconds` | `0.5` | Base of the jittered exponential backoff when a failure carries no delay of its own: the wait before retry *n* is drawn uniformly from `[0, backoff_seconds * 2**n)` (windows `0.5`, `1.0`, `2.0`, …, capped at 30 s). |
| `max_tokens` | `2048` | Output budget `chat`, `json` and `stream` send when the call does not pass one. |
| `circuit_failure_threshold` | `5` | Consecutive failed calls (transport failures and 5xx; never a 4xx or 429) that open the circuit breaker; `0` disables it. |
| `circuit_open_seconds` | `30.0` | How long an open circuit fails calls without sending them. |
| `admin_token` | `None` | Bearer token for `/api/*` (MCP client registry, MCP logs); defaults to `api_key`. |
| `client` / `admin_client` | `None` | Your own `httpx.AsyncClient` for `/v1` + `/mcp` / for `/api/*` (tests, proxies, custom transports). One you pass is not closed by `aclose()`. |

The clients `Bifrost` and `Admin` build for themselves pool connections explicitly: at most
100 open (`MAX_CONNECTIONS`), 20 kept idle for reuse, each for 30 seconds
(`bifrost_sdk._api`). A request beyond the cap waits for a free connection, up to `timeout`.
Pass your own `client=` for other numbers.

### The virtual key

A Bifrost virtual key is the caller's whole permission-and-spend envelope: which providers
and models, which MCP servers and tools, what budget and rate limit. Pass it as `api_key=`:
it is sent as `Authorization: Bearer <key>` on every inference request, on the `/mcp` tool
listing and on tool execution, so the gateway lists and runs only what the key allows. It is
also the default bearer for `/api/*`, which a gateway with admin auth on closes to a virtual
key — pass `admin_token=` for `bf.mcp` and `mcp_logs` there.

`Options(virtual_key=...)` sends the key as the `x-bf-vk` header on one request instead, for a
process that serves several callers through one client.

### `Admin(base_url, *, token=None, timeout=60.0, client=None)`

`base_url` may be the gateway origin or its `/v1` URL; `token` is sent as
`Authorization: Bearer <token>`.

## Which method, when

| You want to… | Call | Returns |
| --- | --- | --- |
| get the model's answer as text | `bf.chat(prompt)` | `str` |
| get a parsed object (with a JSON schema, or tolerant parsing of prose and code fences) | `bf.json(prompt, schema=...)` | `dict` |
| show text as it is generated (time-to-first-token matters) | `bf.stream(prompt)` | `AsyncIterator[str]` |
| build a framework on the gateway: usage, `tool_calls`, `finish_reason`, only the keys you set | `bf.complete(prompt)` | `dict` (the gateway's response) |
| know which MCP tools this key may run, to offer them to a model | `bf.tools(clients, only)` | `list[ToolDef]` |
| run a tool call the model asked for, after your own policy check (in Trellis, `trellis.harness.governance`; a wrapped agent's harness does both) | `bf.execute_tool(tool_call)` | `dict` (`{"role": "tool", …}`) |
| read back what tools ran (e.g. a Code Mode script's nested calls) | `bf.mcp_logs(since, parent_request_id=...)` | `list[MCPLog]` |
| check readiness (health probe) | `bf.ping()` | `bool`, never raises |
| see every registered MCP server and all its tools (admin) | `bf.mcp.clients()` | `list[MCPClient]` |
| register / change / remove an MCP server | `bf.mcp.add` / `update` / `remove` | `None` |
| create, rotate or check a virtual key | `Admin(...).vk.*` | `dict` / `list[dict]` |
| read budgets, rate limits, teams, customers | `Admin(...).governance.*` | `list[dict]` |
| route requests by CEL rule or by complexity tier | `Admin(...).routing.*` | `dict` / `list[dict]` |
| store and version prompts for injection | `Admin(...).prompts.*` + `Options(prompt_id=...)` | `dict` / `list[dict]` |
| publish and roll back Agent Skills | `Admin(...).skills.*` | `dict` / `list[dict]` |

## The completion verbs

| Method | Signature | Use it when |
| --- | --- | --- |
| `chat` | `chat(prompt, *, model=None, system=None, max_tokens=None, temperature=0.0, tools=None, options=None, **extra) -> str` | you want the answer |
| `json` | `json(prompt, *, schema=None, model=None, system=None, max_tokens=None, options=None, **extra) -> dict` | you want the answer parsed, and checked if the provider ignores `response_format` |
| `stream` | `stream(prompt, *, model=None, system=None, max_tokens=None, temperature=0.0, tools=None, options=None, **extra) -> AsyncIterator[str]` | time-to-first-token matters. Not retried: a partly consumed stream cannot be replayed without showing duplicate text. Uses the circuit breaker like the other verbs; an error the gateway sends inside the stream raises `GatewayError` |
| `complete` | `complete(prompt, *, model=None, system=None, max_tokens=None, temperature=None, tools=None, timeout=None, options=None, **extra) -> dict` | you need the whole response — usage, tool calls, `finish_reason` — because you are building a framework on the gateway rather than calling one |

- `prompt` is a `Messages`: a string (one user turn) or a list of `{"role", "content"}`
  messages. `system=` prepends a system turn.
- `**extra` goes into the request body verbatim (`top_p=`, `seed=`, `tool_choice=`, …).
- `chat`, `json` and `stream` are opinionated: they always send a `max_tokens` and a
  `temperature` (`json` always `0.0`). `complete` is not — it sends only the keys you passed,
  because a caller building its own request should not find keys in the body it never set.
- `json(schema=...)` sends `response_format` as a strict `json_schema`; the reply is parsed
  defensively either way (code fences and surrounding prose are stripped) and anything that
  is not one JSON object raises `InvalidJSON`.
- `complete(timeout=...)` overrides the client's timeout for that one request, for callers
  running under a deadline.
- A reply whose content is a list of parts is joined into text; a reply with no text that did
  not run out of budget (a tool-call turn) is `""` from `chat`.

`ping()` says whether `GET /models` succeeds (any non-2xx, or no answer, is `False`), with a
5-second timeout, and never raises.

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

`Options` is frozen; `opts.merged(session_id=...)` returns a copy. Unset fields send nothing.
The header names are public constants in `bifrost_sdk.headers`:

| `Options` field | Header (constant) | Effect |
| --- | --- | --- |
| `virtual_key` | `x-bf-vk` (`VIRTUAL_KEY`) | governance identity for this request |
| `prompt_id`, `prompt_version` | `x-bf-prompt-id`, `x-bf-prompt-version` (`PROMPT_ID`, `PROMPT_VERSION`) | inject a stored prompt; no id, no version header (a version alone selects nothing); no version, the latest committed one |
| `mcp_clients`, `mcp_tools` | `x-bf-mcp-include-clients`, `x-bf-mcp-include-tools` (`MCP_CLIENTS`, `MCP_TOOLS`) | MCP scope, comma-joined (below) |
| `mcp_session_id` | `x-bf-mcp-session-id` (`MCP_SESSION`) | MCP session |
| `parent_request_id` | `x-bf-parent-request-id` (`PARENT_REQUEST_ID`) | correlate tool executions (Code Mode's nested calls) |
| `session_id` | `x-bf-session-id` (`SESSION`) | conversation identity |
| `cache_key`, `cache_type`, `cache_threshold` | `x-bf-cache-key`, `x-bf-cache-type`, `x-bf-cache-threshold` (`CACHE_*`) | semantic cache control |
| `customer_id`, `customer_name`, `project_id` | `x-bf-customer-id`, `x-bf-customer-name`, `x-bf-project-id` (`CUSTOMER_ID`, `CUSTOMER_NAME`, `PROJECT_ID`) | spend attribution |
| `content_logging=False` | `x-bf-disable-content-logging: true` (`NO_CONTENT_LOGGING`) | keep this payload out of the logs |
| `dimensions={"team": "payments"}` | `x-bf-dim-team: payments` (`DIMENSION_PREFIX`) | reporting dimensions |
| `extra` | as given, applied last | anything else |

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

- `tools(clients=None, only=None)` lists what a request scoped with the same values could
  execute, as the gateway lists it for the virtual key: its own MCP endpoint (`POST /mcp`,
  JSON-RPC `tools/list`) asked with the key — never `/api`, which admin auth closes to a
  virtual key. So the listing is exactly what the key's MCP allow-list admits (a key with no
  MCP configuration sees no tools), and a listing the gateway refuses raises `GatewayError`.
  `ToolDef` carries `name` (`<client>-<tool>`), `client`, `description`, `parameters` (JSON
  schema), `code_mode` and `annotations`.
- `ToolDef.annotations` is the server's MCP `ToolAnnotations` — `read_only_hint`,
  `destructive_hint`, `idempotent_hint`, `open_world_hint` (each `bool | None`; the camelCase
  wire names parse too) — or `None` when the server published none; the memory service's tool
  catalog is then the source of a tool's risk tier.
- A **Code Mode** client's tools are not in `tools/list` (its meta-tools are). `tools()` reads
  them from the meta-tools — `listToolFiles` names the Code Mode clients, `readToolFile` holds
  one `def tool(param: type, ...)` declaration per tool — and marks them `code_mode=True`.
  The gateway publishes no JSON schema and no annotations for them: `parameters` is derived
  from the signature (`str`/`int`/`float`/`bool`/`list`/`dict`; any other type admits any
  value; a parameter with a default is optional), `annotations` is `None`, and the
  description is the declaration's comment, which the gateway may truncate. The
  `readToolFile` calls run four at a time; the tools come back in `listToolFiles` order
  whatever order the reads finish in, and if any read fails, the listing raises the failure
  of the first-listed client.
- `bf.mcp.clients()` is the admin registry (`GET /api/mcp/clients`, every page, every
  discovered tool, annotations joined from `/mcp` when that listing answers); with admin auth
  on it needs `admin_token`. `MCPClient` carries `id`, `config` (`MCPClientConfig`), `state`,
  `disabled` and `tools`; `MCPClient.executable` is the subset its `tools_to_execute` lets run.
- `execute_tool(tool_call, *, options=None, timeout=None)` takes an entry of a completion's
  `tool_calls` (`POST /v1/mcp/tool/execute?format=chat`). It is not retried and does not count
  toward the circuit breaker: a tool may have side effects, and a refused call (out of scope,
  not allowed) is a 400 `GatewayError`. A call with no function name raises `ValueError`
  before anything is sent.
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
`since` must be timezone-aware and `limit` between 1 and 1000 (`ValueError` otherwise).
`since` is inclusive and the gateway writes logs asynchronously (seconds behind), so a caller
paging forward dedups by `id`. `MCPLog` also carries `parent_request_id` and `latency_ms`;
`arguments` is parsed from the logged JSON text when it parses, and kept verbatim otherwise.
`/api/mcp-logs` is an admin route: with admin auth on, it needs `admin_token`.

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

`add` registers only `http`/`sse` connections with a URL (`ValueError` otherwise). `update`
changes name, tool allow-lists and the Code Mode flag. The gateway answers 200 to a connection
change and ignores it, so `update` refuses one: remove the client and add it again.
`tools_to_auto_execute` (Agent Mode) defaults to empty. The gateway refuses private-network
MCP targets to unauthenticated callers.

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

A virtual key's `provider_configs` and `mcp_configs` are deny-by-default: a key created
without them permits nothing. Keyword arguments (`**fields`, `**changes`, `**filters`) are
sent as the body or the query string unchanged, so any field the gateway accepts can be set.
List methods unwrap the gateway's envelope (`{"virtual_keys": [...]}` and its spellings) and
return `[]` for an envelope they do not recognise.

| Namespace | Method | Route |
| --- | --- | --- |
| `admin.vk` | `list(**filters)` | `GET /api/governance/virtual-keys` |
| | `get(vk_id)` | `GET /api/governance/virtual-keys/{vk_id}` |
| | `quota(**filters)` — the one route a virtual key may call about itself | `GET /api/governance/virtual-keys/quota` |
| | `create(name, *, provider_configs=None, mcp_configs=None, **fields)` | `POST /api/governance/virtual-keys` |
| | `update(vk_id, **changes)` | `PUT /api/governance/virtual-keys/{vk_id}` |
| | `delete(vk_id)` | `DELETE /api/governance/virtual-keys/{vk_id}` |
| | `rotate(vk_id=None, **fields)` — without `vk_id`, in bulk | `POST /api/governance/virtual-keys[/{vk_id}]/rotate` |
| `admin.governance` | `budgets(**filters)` | `GET /api/governance/budgets` |
| | `rate_limits(**filters)` | `GET /api/governance/rate-limits` |
| | `teams(**filters)` | `GET /api/governance/teams` |
| | `customers(**filters)` | `GET /api/governance/customers` |
| `admin.routing` | `rules(**filters)` | `GET /api/routing/rules` |
| | `add_rule(**rule)` — the match is a CEL expression | `POST /api/routing/rules` |
| | `update_rule(rule_id, **changes)` | `PUT /api/routing/rules/{rule_id}` |
| | `delete_rule(rule_id)` | `DELETE /api/routing/rules/{rule_id}` |
| | `complexity_config()` / `set_complexity_config(**config)` | `GET` / `PUT /api/routing/complexity-analyzer-config` |
| | `complexity_status()` — the analyzer warms asynchronously | `GET /api/routing/complexity-analyzer-status` |
| `admin.prompts` | `list(**filters)` / `get(prompt_id)` | `GET /api/prompt-repo/prompts[/{prompt_id}]` |
| | `create(name, **fields)` / `update(prompt_id, **changes)` / `delete(prompt_id)` | `POST` / `PUT` / `DELETE /api/prompt-repo/prompts[/{prompt_id}]` |
| | `versions(prompt_id)` / `add_version(prompt_id, **fields)` — only a committed version can be injected | `GET` / `POST /api/prompt-repo/prompts/{prompt_id}/versions` |
| | `version(version_id)` | `GET /api/prompt-repo/versions/{version_id}` |
| | `folders(**filters)` | `GET /api/prompt-repo/folders` |
| `admin.skills` | `list(**filters)` / `get(skill_id)` | `GET /api/skills[/{skill_id}]` |
| | `create(name, **fields)` / `update(skill_id, **changes)` / `delete(skill_id)` | `POST` / `PUT` / `DELETE /api/skills[/{skill_id}]` |
| | `versions(skill_id)` | `GET /api/skills/{skill_id}/versions` |
| | `shift_version(skill_id, version)` — serve another published version (rollback) | `POST /api/skills/{skill_id}/shift-version` |

Management calls are not retried (most are writes) and do not use the circuit breaker. An
empty body (`204`) returns `None`; a `200` that is not JSON (the gateway's UI answers unknown
`/api` paths with HTML) raises `GatewayError`.

## Errors

```
BifrostError                .details: whatever the gateway said (status, body excerpt, …)
├── Unreachable             the gateway could not be contacted (connect error, timeout)
├── RateLimited             a 429; carries .retry_after (seconds, or None)
│   └── RateLimitedError    what this client raises for a 429 (.status == 429)
├── GatewayError            an error status, or a body this client cannot use (.status, or None)
│   ├── BadRequestError         400  (e.g. the prompt is over the context length)
│   ├── AuthenticationError     401
│   ├── PermissionDeniedError   403
│   ├── NotFoundError           404
│   ├── ConflictError           409
│   ├── UnprocessableError      422
│   └── ServerError             5xx
├── CircuitOpen             recent calls failed; this one was not sent (.retry_after)
├── EmptyResponse           200 OK with no text (see below)
└── InvalidJSON             json() could not parse the reply
```

Any other error status (`408`, `413`, …) is a plain `GatewayError`. The typed classes are
subclasses, so `except GatewayError` still catches every one of them.

Every error carries `retryable`: whether the *same* call may succeed if made again later.
It is `True` for `Unreachable`, `RateLimited`, `CircuitOpen`, and for a `GatewayError` whose
status is in `RETRYABLE` (`408, 409, 425, 500, 502, 503, 504`); `False` for everything else —
including a `501`, a `GatewayError` without a status, `EmptyResponse` and `InvalidJSON`.
Callers above this client read it rather than re-deriving the rule.

| Raised by | `Unreachable` | `RateLimited` | `GatewayError` | `CircuitOpen` | `EmptyResponse` | `InvalidJSON` |
| --- | --- | --- | --- | --- | --- | --- |
| `chat`, `complete`* | after retries | after retries | yes | yes | `chat` only | |
| `json` | after retries | after retries | yes | yes | yes | yes |
| `stream` | yes | yes | yes | yes | | |
| `tools`, `execute_tool`, `mcp_logs`, `bf.mcp.*`, `Admin.*` | yes | yes | yes | | | |
| `ping` | never raises | | | | | |

\* `complete` returns the payload as it is, so an empty answer is the caller's to read.
Invalid arguments (no model, a naive `since`, a tool call without a name, an unregistrable
MCP connection) raise `ValueError` before any request is sent.

```python
from bifrost_sdk import (
    BadRequestError,
    BifrostError,
    CircuitOpen,
    EmptyResponse,
    GatewayError,
    RateLimited,
)

try:
    text = await bf.chat(prompt)
except RateLimited as exc:  # healthy gateway asking for less: wait exc.retry_after
    ...
except CircuitOpen as exc:  # nothing was sent; the gateway was failing moments ago
    ...
except EmptyResponse:  # a reasoning model spent the budget: raise max_tokens
    ...
except BadRequestError:  # this request will never work as sent: change it
    ...
except GatewayError as exc:  # exc.status, exc.retryable, exc.details["body"]
    ...
except BifrostError:  # Unreachable, InvalidJSON
    ...
```

`RateLimited` (and so `RateLimitedError`) is deliberately **not** a `GatewayError`. A 429
means the gateway is healthy and saying so; the circuit breaker does not count it, or "slow
down" becomes "stop". Measured on a real run: 17 rate limits opened a breaker and the next 62
calls failed instantly without a request ever being sent. Code that catches `GatewayError` to
count failures has therefore never seen a 429, and the typed `RateLimitedError` keeps it so.

`EmptyResponse` exists because reasoning models spend the output budget on thinking before
emitting anything, so too small a `max_tokens` returns 200 OK with `""` and
`finish_reason="length"`. Measured against gemini-3.6-flash: "Reply with exactly: OK" consumed
57 reasoning tokens. Returning `""` would push a silently degraded answer into every call site.
Its `.details` carry `completion_tokens` and `reasoning_tokens`.

## Retries and the circuit breaker

Retried (`chat`, `json`, `complete`): `408, 409, 425, 429, 500, 502, 503, 504` (exported as
`bifrost_sdk.RETRYABLE`) and transport failures (connect errors, timeouts). Everything else
is the caller's mistake and is raised on the first attempt — retrying a 400 only spends the
budget. A `200` whose body is not JSON is a gateway fault: raised at once, counted by the
breaker.

The wait comes from `Retry-After` when the gateway sends one, as a delay or an HTTP date, and
from the *body* when it does not: Gemini answers "Please retry in 59.18s" with no header at
all. That delay is used as given, without jitter — the gateway knows when it will have room.
Without either, the wait is **exponential backoff with full jitter**: a uniform draw from
`[0, backoff_seconds * 2**attempt)`, so processes that failed together do not all retry in
the same instant when the gateway comes back. No wait is longer than 30 seconds
(`bifrost_sdk._retry.MAX_WAIT`), whoever asked for it.

After `circuit_failure_threshold` consecutive failed calls (default 5; 0 disables it) the
client fails fast with `CircuitOpen` for `circuit_open_seconds`, so an outage costs one
timeout rather than one per request. A call whose retries are all spent counts once. After
the window the next call is sent: a success closes the circuit, a failure opens it again.

Only failures that say the *gateway* is unwell count: transport failures (connect errors,
timeouts, a connection lost mid-stream), any 5xx, and a `200` whose body is unusable. No 4xx
counts — not a 429 (backpressure), not a 400, 404 or 422 (a verdict on that request: a few
over-long prompts must not fail every other agent sharing the client), and not a 401 or 403
either (one mis-keyed caller must not take the gateway away from the correctly keyed ones).
A 4xx neither adds to a streak of failures nor ends it.

`chat`, `json`, `complete` and `stream` check and feed the breaker; a stream counts once it
is accepted (success) or when it fails to start or loses its connection (failure). Tool
execution and management calls neither check nor feed it.

## Tests

```bash
uv run pytest                                                # unit tests, gateway mocked
BIFROST_URL=http://localhost:8091/v1 uv run pytest -m live   # against a running gateway
```

The unit tests never touch the network: the gateway is an `httpx.MockTransport`, or `respx`
where the test lets the SDK build its own HTTP client. They cover every line and branch of
`src/`.

The live tests register temporary MCP clients (`BIFROST_LIVE_MCP_URL`, default the public
DeepWiki server, which publishes no annotations; `BIFROST_LIVE_ANNOTATED_MCP_URL`, default the
public Context7 server, which does) and remove them afterwards; they skip when `BIFROST_URL`
is unset or the gateway is unreachable. `BIFROST_URL` is the name every repository in the
platform uses for the gateway; the older `BIFROST_LIVE_URL` is still read when it is unset.
A plain `uv run pytest` deselects them (`addopts` in `pyproject.toml`), because `BIFROST_URL`
is often set in a shell that did not mean to register clients on that gateway.

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check` and the unit tests on
every pull request and on pushes to `main`.
