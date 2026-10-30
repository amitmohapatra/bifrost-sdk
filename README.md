# bifrost-sdk

An OpenAI-compatible async client, with [Bifrost](https://github.com/maximhq/bifrost) extras.

It speaks OpenAI's chat completions to any endpoint that does: OpenAI, OpenRouter, a LiteLLM
proxy, Ollama, vLLM, llama.cpp, the Anthropic and Gemini compatible endpoints, Bifrost. It
knows a URL, a model *name* and how to authenticate (a bearer key, a refreshed Google token
for Vertex AI, an AWS SigV4 signature for Bedrock), shapes each request from what the model
accepts (its profile, never its name), and imports no provider SDK. Built for a Bifrost
gateway (`gateway="bifrost"`), it adds the gateway's extras: MCP tools, the gateway's log of
tool calls, per-request `x-bf-*` options, and a separate operator client for virtual keys,
budgets, routing rules, stored prompts, skills and Virtual MCPs. It reads no environment
variable, and it imports neither `trellis.contracts` nor any agent framework.

## Start here

1. **Install** it: `uv add bifrost-sdk` ([Install](#install)).
2. **Read the [Quickstart](#quickstart)**: text, a parsed object and a stream in six lines.
3. **Run the examples**: `make examples` runs nine scripts against an in-process fake
   gateway, from one completion to Virtual MCPs, the circuit breaker and a plain
   OpenAI-compatible endpoint with a reasoning model's profile, with no network
   ([examples/](examples/README.md)).
4. **Pick your method** with [Which method, when](#which-method-when).
5. **Go deeper** in the [documentation](#documentation): the architecture and its sequence
   diagrams, every method, configuration, errors and troubleshooting.

## Where this fits: two ways to use Trellis

Trellis is five repos: **agent-harness** runs your agent, **agent-runs** keeps runs durable
(the inbox, schedules, workers, webhooks), **agent-memory-service** gives agents memory,
**agent-contracts** holds the types they share, and **this package** is the client every model
call and every MCP tool call on the platform goes through.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#among-the-five-trellis-repos) shows who imports
it and how.

- **Way 1, wrapped.** `from trellis import Harness; h = Harness(); agent = h.wrap(my_agent)`.
  The harness runs your agent (LangGraph, Deep Agents, OpenAI Agents SDK, Claude Agent SDK,
  a plain function) and uses every block, this one included, automatically.
- **Way 2, pluggable blocks.** Keep your framework untouched and import only the blocks you
  want, `bifrost_sdk` among them.

| | What happens with `bifrost_sdk` |
|---|---|
| **Way 1, wrapped** | With `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_GATEWAY=bifrost` set, the harness lists the MCP tools the key allows (`tools()`), runs each call the model makes with `execute_tool` once governance lets it, reads a Code Mode script's nested calls back with `mcp_logs`, and calls `complete` for its `ReAct` loop and its LLM judge. The memory service makes its own model calls through it as well. You write no Bifrost code; a framework's own model object points at the gateway's OpenAI-compatible endpoint. |
| **Way 2, pluggable** | Your framework runs the agent, untouched; you list the key's MCP tools, offer them to the model and run the calls it makes: |

```python
from bifrost_sdk import Bifrost

async with Bifrost(LLM_BASE_URL, model=MODEL, api_key=LLM_API_KEY, gateway="bifrost") as bf:
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

Harness docs: [the two ways](https://github.com/amitmohapatra/agent-harness/blob/main/README.md#two-ways-to-use-trellis) ·
[every page](https://github.com/amitmohapatra/agent-harness/blob/main/docs/README.md) ·
blocks: [governance](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/governance.md),
[evaluation](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/evaluation.md) ·
recipes: [LangGraph](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/langgraph.md),
[OpenAI Agents SDK](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/openai-agents.md),
[Claude Agent SDK](https://github.com/amitmohapatra/agent-harness/blob/main/docs/blocks/claude-agent-sdk.md).

## Install

```bash
uv add bifrost-sdk        # or: pip install bifrost-sdk
```

Requires Python 3.12+. Dependencies: `httpx`, `pydantic`. Two extras add an auth strategy
each: `bifrost-sdk[google]` (google-auth, for `GoogleAuth`) and `bifrost-sdk[aws]` (botocore,
for `AwsSigV4Auth`).

## Quickstart

```python
from bifrost_sdk import Bifrost

async with Bifrost("http://gateway:8080/v1", model="provider/model", api_key=VIRTUAL_KEY) as bf:
    text = await bf.chat("summarise this")  # -> str
    data = await bf.json("extract the fields", schema=SCHEMA)  # -> dict
    async for delta in bf.stream("write a story"):  # -> AsyncIterator[str]
        print(delta, end="")
```

`model` is a model as the endpoint names it (`provider/model` on a gateway, `gpt-4o-mini` on
OpenAI). `VIRTUAL_KEY` comes from your own settings: a provider's key for its own endpoint, or a
Bifrost virtual key, the caller's whole permission-and-spend envelope (which models and MCP
tools, what budget and rate limit). `Bifrost` is an async context manager; outside
`async with`, call `await bf.aclose()`. [examples/01_chat.py](examples/01_chat.py) is the same
call against a Bifrost gateway, runnable offline.

### Any endpoint, any login

```python
from bifrost_sdk import Bifrost, ModelProfile, auth_strategy

# A reasoning model on Vertex AI: a Google token, refreshed as it expires; the budget is sent
# as max_completion_tokens and no temperature, because the profile says so.
async with Bifrost(
    VERTEX_OPENAI_URL,
    model="google/gemini-2.5-pro",
    auth=auth_strategy("google"),  # or GoogleAuth(credentials), AwsSigV4Auth(region=...)
    profile=ModelProfile(reasoning_output=True, temperature=False),
) as bf:
    text = await bf.chat("summarise this")
```

`gateway` is `"openai-compatible"` unless you pass `"bifrost"`; on any other gateway the
Bifrost-only methods raise `BifrostOnlyError` before sending anything.
[examples/09_any_openai_compatible_endpoint.py](examples/09_any_openai_compatible_endpoint.py)
runs offline.

## Which method, when

| You want to… | Call | Returns |
| --- | --- | --- |
| get the model's answer as text | `bf.chat(prompt)` | `str` |
| get a parsed object (with a JSON schema, or tolerant parsing of prose and code fences) | `bf.json(prompt, schema=...)` | `dict` |
| show text as it is generated (time-to-first-token matters) | `bf.stream(prompt)` | `AsyncIterator[str]` |
| build a framework on the gateway: usage, `tool_calls`, `finish_reason`, only the keys you set | `bf.complete(prompt)` | `dict` (the gateway's response) |
| reach a provider that logs in with Google or AWS, not a key | `Bifrost(url, auth=auth_strategy("google"))` (or `"aws"`, `GoogleAuth(...)`, `AwsSigV4Auth(...)`) | a client |
| send what a reasoning model accepts (`max_completion_tokens`, no `temperature`) | `Bifrost(url, profile=ModelProfile(reasoning_output=True))` | a client |
| know which MCP tools this key may run, to offer them to a model (Bifrost) | `bf.tools(clients, only)`, or one Virtual MCP's: `bf.tools(slug=...)` | `list[ToolDef]` |
| run a tool call the model asked for (Bifrost), after your own policy check (in Trellis, `trellis.harness.governance`; a wrapped agent's harness does both) | `bf.execute_tool(tool_call)` (`slug=` to run it through one Virtual MCP) | `dict` (`{"role": "tool", …}`) |
| read back what tools ran (e.g. a Code Mode script's nested calls) (Bifrost) | `bf.mcp_logs(since, parent_request_id=...)` | `list[MCPLog]` |
| check readiness (health probe) | `bf.ping()` | `bool`, never raises |
| see every registered MCP server and all its tools (Bifrost admin) | `bf.mcp.clients()` | `list[MCPClient]` |
| register / change / remove an MCP server | `bf.mcp.add` / `update` / `remove` | `None` |
| create, rotate or check a virtual key | `Admin(...).vk.*` | `dict` / `list[dict]` |
| read budgets, rate limits, teams, customers | `Admin(...).governance.*` | `list[dict]` |
| route requests by CEL rule or by complexity tier | `Admin(...).routing.*` | `dict` / `list[dict]` |
| store, version and resolve prompts for injection | `Admin(...).prompts.*` + `Options(prompt_id=...)` | `Prompt` / `PromptVersion` |
| publish, read (body and files) and roll back Agent Skills | `Admin(...).skills.*` | `Skill` / `SkillVersion` / `bytes` |
| bundle tools of several MCP servers behind one endpoint for some keys | `Admin(...).virtual_mcps.*` | `VirtualMCP` |

Signatures, per-request `Options`, MCP tools and every admin route: [docs/api.md](docs/api.md).

## Configuration

Everything is a constructor argument; the package reads no environment variable (google-auth
and botocore find their own credentials when none are passed). The defaults are an
`openai-compatible` gateway, a plain chat model's profile, a 60 s timeout, 2 retries with
jittered backoff, a 2048-token output budget for `chat`/`json`/`stream`, and a circuit breaker
that opens after 5 failed calls for 30 s.
Every argument with its default and an example: [docs/configuration.md](docs/configuration.md).

## Errors, in short

Every exception is a `BifrostError` and says whether the same call may succeed later
(`retryable`). A 429 is `RateLimited`, deliberately not a `GatewayError`, and never trips the
breaker. Each common status has its own `GatewayError` subclass (`BadRequestError`,
`AuthenticationError`, ..., `ServerError`). A Bifrost-only method on a client built for
another gateway raises `BifrostOnlyError`, unsent. Only `chat`, `json` and `complete` retry. The
hierarchy, which method raises what, the retry policy and the breaker:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#errors). What to do about each:
[docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

| Page | What it answers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Its place among the five repos, the gateway's three surfaces, the modules, sequence diagrams (completion, stream, deny-all scope, MCP list and execute, Virtual MCP and forwarded headers, prompts and skills), errors, retries, the breaker |
| [docs/api.md](docs/api.md) | Every method and its signature, `Options` and its headers, MCP tools, administration, the exports |
| [examples/](examples/README.md) | Nine runnable scripts, offline, simplest first |
| [docs/configuration.md](docs/configuration.md) | Every constructor argument: default, automatic or not, example; the names other repos use |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Errors and surprises, their causes and fixes; FAQ |
| [docs/versioning.md](docs/versioning.md) | Which versions of the Trellis repos go together, and the version rules |
| [CHANGELOG.md](CHANGELOG.md) | What each version changed |
| [docs/adr/](docs/adr/README.md) | Why: constructor arguments only, the retry and breaker policy, deny-all completions, an OpenAI-compatible client with Bifrost extras |

## Development

```bash
make sync          # uv sync --all-extras
make check         # lint, format-check, test, examples, links: what CI runs
make test-live     # against a real gateway and local MCP servers (below)
```

The unit tests (`make test`) never touch the network: the gateway is an
`httpx.MockTransport`, or `respx` where the test lets the SDK build its own HTTP client. They
cover every line and branch of `src/`. CI (`.github/workflows/ci.yml`) runs `make check`'s
steps on every pull request and on pushes to `main`.

**Live tests.** `make test-live` (`pytest -m live`) runs them against a real gateway and real
MCP servers, with no public host involved. The MCP servers are `tests/local_mcp.py`'s: three
small streamable-HTTP servers the tests serve on free loopback ports for the session (a wiki
server whose tools publish no annotations, one whose tools publish differing annotations, and
an `echo`/`whoami` server that reads a forwarded caller header). Each test registers what it
uses as a temporary MCP client, with a temporary virtual key where it needs one, and removes
them afterwards.

The gateway refuses to register a loopback server for an unauthenticated caller ("set an admin
password to allow this"), so the tests register as the gateway's admin, as an operator would:
dashboard auth on, `POST /api/session/login`, and the session token it returns as
`admin_token=` / `Admin(token=)`. The simplest way is to let the tests start their own gateway
(`tests/live_gateway.py`) from a local `bifrost-http` binary:

```bash
BIFROST_LIVE_GATEWAY_BIN=/path/to/bifrost-http \
BIFROST_LIVE_GATEWAY_CONFIG=/path/to/config.json \
BIFROST_LIVE_MODEL=provider/model \
make test-live
```

It runs on a free `127.0.0.1` port with a new app directory (under `BIFROST_LIVE_GATEWAY_DIR`,
else pytest's temporary directory; its output is `gateway.log` there), and stops when the
session ends. Its `config.json` is `BIFROST_LIVE_GATEWAY_CONFIG`'s (your providers) with the
test settings on top: SQLite stores in its app directory, dashboard auth with a random admin
password, no MCP clients, logging on, and `enforce_auth_on_inference` off, because the MCP
registry tests list and run tools as an unscoped caller (the key tests make their own keys).
No other gateway is touched.

Against a gateway that is already running, set `LLM_BASE_URL` (e.g.
`http://localhost:8091/v1`) and, for the tests that register MCP servers,
`BIFROST_LIVE_ADMIN_USERNAME` and `BIFROST_LIVE_ADMIN_PASSWORD` (they skip without). Every
variable is in [docs/configuration.md](docs/configuration.md#the-live-tests-variables).

The live tests skip when neither `BIFROST_LIVE_GATEWAY_BIN` nor `LLM_BASE_URL` is set, or the
gateway is unreachable. `LLM_BASE_URL` is the name every repository in the platform uses for
the model endpoint. A plain `uv run pytest` deselects them (`addopts` in `pyproject.toml`),
because `LLM_BASE_URL` is often set in a shell that did not mean to register clients on that
gateway.

The live tests of prompts, skills, Virtual MCPs, per-user headers and the no-gateway-tools
guarantee create and delete their own prompts, skills, Virtual MCPs and virtual keys. The
ones that complete need `BIFROST_LIVE_MODEL` (`provider/model`, any model the gateway
serves).

`tests/test_live_providers.py` sends one completion to each provider whose key is set
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`), through its OpenAI-compatible
endpoint; a provider without a key is not collected, so it is never a skip.
`LLM_LIVE_<PROVIDER>_MODEL` overrides its model.
