# Changelog

What changed in each version of `bifrost-sdk`. Which versions of the other Trellis packages
go with which is in [docs/versioning.md](docs/versioning.md); the reasons are in the
[ADRs](docs/adr/README.md).

## Unreleased

* `NO_GATEWAY_TOOLS` is exported from `bifrost_sdk` and `bifrost_sdk.headers`, so a
  framework's own model client pointed at the gateway sends the same deny-all scope.
* Documentation: a "Start here" README; the method reference moved to
  [docs/api.md](docs/api.md); errors, retries and the breaker live once, in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), which gains sequences for the deny-all
  completion, a Virtual MCP with a forwarded header, and prompts and skills;
  [configuration](docs/configuration.md), [troubleshooting](docs/troubleshooting.md),
  [versioning](docs/versioning.md) and three [ADRs](docs/adr/README.md).
* Eight offline examples against an in-process fake gateway ([examples/](examples/README.md)),
  a `Makefile` with the targets CI runs, and a link check; CI runs the examples and the link
  check.

## 0.3.0 (2026-10-05)

* **Completions never get the gateway's MCP tools.** `chat`, `json`, `stream` and `complete`
  send the MCP scope headers empty (deny-all) and refuse an `Options` with a scope.
* **Virtual MCPs**: `admin.virtual_mcps` (create, update, attach to a key) and `slug=` on
  `tools()` and `execute_tool()`.
* **Forwarded caller headers**: `Options(extra=...)` on `execute_tool` reaches the MCP server
  when the client's `allowed_extra_headers` names it.
* **Prompt and skills repositories**: `admin.prompts` and `admin.skills`, typed; stored
  prompts injected with `Options(prompt_id=, prompt_version=)`.
* **Hardening**: typed status errors (`BadRequestError` ... `ServerError`,
  `RateLimitedError`), `retryable` on every error, a breaker that counts only 5xx and
  transport failures, full-jitter backoff, pooled clients.
* `ToolDef.annotations` (the server's MCP tool annotations); `tools()` lists through the
  gateway's `/mcp` with the virtual key, never `/api`.
* `ManagementAPI` reads every page of a list, and non-JSON downloads.

## 0.2.0 (2026-09-30)

* Per-request options are one `Options` argument (the `Call` chain is gone).
* Typed MCP clients (`MCPClient`, `MCPClientConfig`) and a scoped tool listing.
* `mcp_logs()` reads the gateway's MCP execution log.
* Gateway administration moved to `bifrost_sdk.admin`.
* Opt-in live tests against a running gateway.

## 0.1.0 (2026-09-21)

* A client for the Bifrost gateway: `chat`, `json`, `stream`, `complete`, `ping`; retries
  with the delay read from the whole rate-limit body; a circuit breaker.
