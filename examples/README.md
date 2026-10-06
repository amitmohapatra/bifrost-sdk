# Examples

Eight scripts, simplest first. Each runs **offline**: the real `bifrost_sdk` client talks to
`http://gateway.test`, and [`_fake_gateway.py`](_fake_gateway.py) answers those requests
in-process with `respx`, in the shapes a running gateway sends. No network, no key, no
environment variable. Each script asserts what it shows, so exit 0 is a passing check.

```bash
make examples                                      # all of them, as CI does
uv run python examples/04_mcp_list_and_execute.py  # one
```

They need the dev extras (`make sync`), which include `respx`.

| # | Script | What it shows | Read with |
|---|---|---|---|
| 01 | [01_chat.py](01_chat.py) | `chat`, `ping`, `Options` as `x-bf-*` headers, and the deny-all scope every completion carries | [api.md: completion verbs](../docs/api.md#the-completion-verbs) |
| 02 | [02_json_schema.py](02_json_schema.py) | `json(schema=)`: strict `json_schema`, a reply parsed out of prose and a code fence | [api.md: completion verbs](../docs/api.md#the-completion-verbs) |
| 03 | [03_stream.py](03_stream.py) | `stream`: deltas from server-sent events | [the stream sequence](../docs/ARCHITECTURE.md#a-chat-completion-with-a-virtual-key) |
| 04 | [04_mcp_list_and_execute.py](04_mcp_list_and_execute.py) | `tools()`, offer them, `complete`, your policy check, `execute_tool` with a scope, `mcp_logs`; a call out of scope; a scope refused on a completion | [Listing and calling MCP tools](../docs/ARCHITECTURE.md#listing-and-calling-mcp-tools) |
| 05 | [05_virtual_mcp_and_forwarded_headers.py](05_virtual_mcp_and_forwarded_headers.py) | `admin.virtual_mcps.create` and `attach`, `tools(slug=)`, `execute_tool(slug=)` with a forwarded `x-user-token`, an error turn, a 403 | [A Virtual MCP and a forwarded caller header](../docs/ARCHITECTURE.md#a-virtual-mcp-and-a-forwarded-caller-header) |
| 06 | [06_prompts_and_skills.py](06_prompts_and_skills.py) | `admin.prompts` create, commit, find; `Options(prompt_id=, prompt_version=)`; `admin.skills` create, find, `read_file` | [Stored prompts and skills](../docs/ARCHITECTURE.md#stored-prompts-and-skills) |
| 07 | [07_admin_mcp_clients_and_keys.py](07_admin_mcp_clients_and_keys.py) | `bf.mcp.add`, `clients`, `remove`; `admin.vk.create` with explicit provider and MCP configs | [api.md: administration](../docs/api.md#administration) |
| 08 | [08_errors_retries_breaker.py](08_errors_retries_breaker.py) | a 503 retried, a 429's own delay, `ServerError.retryable`, the breaker opening, `CircuitOpen` with nothing sent | [Errors](../docs/ARCHITECTURE.md#errors), [the circuit breaker](../docs/ARCHITECTURE.md#the-circuit-breaker) |

**Against a real gateway.** Drop the `with FakeGateway()` block and pass your gateway's `/v1`
URL and a virtual key from your own settings. The tool names (`erp-get_stock`, ...) are the
fake's; use the ones `tools()` lists for your key. The live test suite (`make test-live`)
covers the same calls against a running gateway.
