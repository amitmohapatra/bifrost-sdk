# Troubleshooting and FAQ

Each entry is what you see, why, and the fix. The full error vocabulary, and which method
raises what, is in [ARCHITECTURE.md](ARCHITECTURE.md#errors).

## Errors from a call

**`ValueError: ... model ...` before anything is sent.** Neither the client (`model=`) nor
the call names a model. Pass one, as the gateway names it: `provider/model`.

**`ValueError: a completion never carries the gateway's MCP tools`.** An `Options` with
`mcp_clients` or `mcp_tools` was passed to `chat`, `json`, `stream` or `complete`. The MCP
scope is for `execute_tool`. Offer tools to the model with `tools=` instead
([ADR 0003](adr/0003-completions-never-get-gateway-tools.md)).

**`EmptyResponse`.** The model answered 200 OK with no text because it ran out of output
budget, which reasoning models do while thinking. Raise `max_tokens`. `.details` carries
`completion_tokens` and `reasoning_tokens`.

**`InvalidJSON` from `json()`.** The reply held no single JSON object, even after prose and
code fences were stripped. Pass `schema=` (sent as a strict `json_schema`) or a model that
honours `response_format`.

**`BadRequestError` (400).** The request will never work as sent: over the context length,
an unknown parameter, a tool out of the key's MCP scope. It is not retried. Read
`exc.details["body"]`.

**`AuthenticationError` (401) or `PermissionDeniedError` (403).** The virtual key is wrong,
lacks the model, or (403 through a slug) is not attached to that Virtual MCP. On `/api/*`
with admin auth on, a virtual key is refused: pass `admin_token=`.

**`RateLimited` / `RateLimitedError` (429).** The gateway is healthy and asking for less.
Completions already waited `exc.retry_after` and retried `max_retries` times. It never trips
the breaker.

**`CircuitOpen`.** The last `circuit_failure_threshold` calls failed with 5xx or transport
errors, so this one was not sent. Wait `exc.retry_after`, or check the gateway. A test that
wants every failure passes `circuit_failure_threshold=0`.

**`Unreachable`.** No answer: wrong `base_url`, the gateway is down, or a timeout. `ping()`
says whether `GET /v1/models` succeeds, and never raises.

**`GatewayError: ... returned a non-JSON body`** from an admin call. The gateway's UI answers
unknown `/api` paths with 200 and HTML, so the path is wrong for this gateway version.

## MCP tools

**`tools()` returns an empty list.** The virtual key has no MCP configuration, or its
`mcp_configs` admit nothing: a key is deny-by-default. `bf.mcp.clients()` (with an admin
token) shows what is registered.

**A tool you expect is missing from `tools()`.** A Code Mode client's tools are not in
`tools/list`; `tools()` reads them from the meta-tools and marks them `code_mode=True`. A
tool name is `<client>-<tool>`; a bare tool name or a bare `*` in `only=` matches nothing.

**`execute_tool` raises a 400 `GatewayError`.** The call is outside its `Options` scope or
the key's allow-list. Through a slug the same refusal is an error turn (`"is_error": True`)
instead.

**`ValueError: tool_call has no function name` / `arguments are not a JSON object`.** The
`tool_call` is not an entry of a completion's `tool_calls` in chat format.

**`mcp_logs` misses a call that just ran.** The gateway writes logs asynchronously, seconds
behind. `since` is inclusive, so a caller paging forward dedups by `MCPLog.id`.

**`ValueError: since must be timezone-aware`.** Pass `datetime.now(UTC) - ...`.

**`bf.mcp.update` refuses a connection change.** The gateway answers 200 to a connection
change and ignores it, so the SDK refuses one. Remove the client and add it again.

**The gateway's agent loop still runs a tool.** No header turns that loop off. Under the
deny-all scope the execution is refused and fed back to the model. Keep a client's
`tools_to_auto_execute` empty, which `MCPClientConfig` does by default.

## Prompts and skills

**A stored prompt seems ignored.** An unknown `prompt_id` or version is not an error: the
request goes without the template. Resolve the name with `admin.prompts.find(name)` and send
its `id`. Only a committed version is injected.

**`ValueError: N prompts are named ...`.** The gateway does not keep prompt names unique.
Use an id.

**`ConflictError` (409) publishing a skill.** Skill versions only go up. Publish a higher
one, or `shift_version` to serve an existing one.

**`read_file` gives 404 for a file you published.** Files are served from the served version
only.

## FAQ

**Does it read `BIFROST_URL`?** No. Your application does and passes it in
([configuration.md](configuration.md)). Only the live tests read it.

**Can I run the examples without a gateway?** Yes, `make examples` runs them against an
in-process fake ([examples/](../examples/README.md)).

**How do I test my own code against it?** Pass `client=` an `httpx.AsyncClient` with an
`httpx.MockTransport`, or use `respx` as `examples/_fake_gateway.py` does.

**Is there a type checker in CI?** No. CI runs `ruff check`, `ruff format --check`, the unit
tests, the examples and the link check.
