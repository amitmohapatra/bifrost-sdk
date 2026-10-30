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
* Live tests need no public host: the MCP servers they register are local ones
  (`tests/local_mcp.py`, on free loopback ports for the session) in place of DeepWiki and
  Context7, and they register them as the gateway's admin (login, session token as
  `admin_token=`). With `BIFROST_LIVE_GATEWAY_BIN` they start and stop their own gateway
  (`tests/live_gateway.py`); against a running one, `BIFROST_LIVE_ADMIN_USERNAME` and
  `BIFROST_LIVE_ADMIN_PASSWORD`. `BIFROST_LIVE_MCP_URL` and `BIFROST_LIVE_ANNOTATED_MCP_URL`
  are gone, and so is the `sdklocal` client a gateway's `config.json` had to declare.
  ([README, Development](README.md#development))
* Eight offline examples against an in-process fake gateway ([examples/](examples/README.md)),
  a `Makefile` with the targets CI runs, and a link check; CI runs the examples and the link
  check.

## 0.4.0 (2026-10-30)

An OpenAI-compatible client, with Bifrost extras
([ADR 0004](docs/adr/0004-openai-compatible-with-bifrost-extras.md)).

* **Any OpenAI-compatible endpoint.** `Bifrost(gateway=)` is `"openai-compatible"` (the
  default) or `"bifrost"`. Only a Bifrost client sends `x-bf-*` headers (the deny-all MCP scope
  included); elsewhere `tools`, `execute_tool`, `mcp_logs`, `bf.mcp`, `options=` and
  `admin_token`/`admin_client` raise the new `BifrostOnlyError` before sending anything.
  **Breaking:** a client for a Bifrost gateway now passes `gateway="bifrost"`.
* **Auth strategies** (`bifrost_sdk.auth`), each an `httpx.Auth`, registered by name in
  `AUTH_STRATEGIES` and built with `auth_strategy(name, api_key)`: `BearerAuth` (what
  `api_key=` sends), `GoogleAuth` (a refreshed Google token; extra `[google]`) and
  `AwsSigV4Auth` (SigV4, body signed; extra `[aws]`). `Bifrost(auth=)` takes any `httpx.Auth`.
* **Requests shaped from a `ModelProfile`** (`reasoning_output`, `temperature`, LangChain's
  keys): a model that reasons or takes no temperature is sent `max_completion_tokens` and no
  `temperature`. No model-name checks.
* A conformance suite runs every gateway, auth strategy and profile against stubs of OpenAI,
  the Anthropic and Gemini compatible endpoints and Bifrost, with a Google token refresh and a
  SigV4 signature recomputed from the specification. Opt-in live smoke tests per provider,
  collected only when its key is set. Example 09.
* The live tests read `LLM_BASE_URL` (was `BIFROST_URL`); `BIFROST_LIVE_URL` is no longer
  read. The docs use the platform's `LLM_*` names.

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
