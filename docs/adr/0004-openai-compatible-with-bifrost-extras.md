# ADR 0004: An OpenAI-compatible client with Bifrost extras

**Status:** accepted · **Date:** 2026-10-30 · **Version:** 0.4.0 · **Amends:**
[ADR 0001](0001-constructor-arguments-only.md), [ADR 0003](0003-completions-never-get-gateway-tools.md)

## Context
The harness and the memory service now reach any provider, not only a Bifrost gateway:
anything that speaks OpenAI's chat completions (OpenAI, OpenRouter, a LiteLLM proxy, Ollama,
vLLM, llama.cpp, the Anthropic and Gemini compatible endpoints, Bifrost), and Vertex AI and
Bedrock, whose OpenAI-compatible endpoints take a Google access token or an AWS SigV4
signature rather than a key. Both call models through this client. Which request a model
accepts (a reasoning model refuses `max_tokens` and any `temperature`) was guessed from model
names elsewhere. Pointed at anything but Bifrost, the MCP methods and the `x-bf-*` headers
mean nothing.

## Decision
- **Auth is an `httpx.Auth`.** `bifrost_sdk.auth` has `BearerAuth` (a key: what `api_key=`
  sends, as before), `GoogleAuth` (google-auth, the token refreshed before it expires; the
  `[google]` extra) and `AwsSigV4Auth` (botocore, the body signed; the `[aws]` extra), in a
  registry by name (`bearer`, `google`, `aws`) that `auth_strategy(name, api_key)` reads.
  `Bifrost(auth=)` takes any `httpx.Auth`.
- **Requests are shaped from a `ModelProfile`, never a model name.** A profile with
  `reasoning_output`, or without `temperature`, sends the budget as `max_completion_tokens`
  and no `temperature`. The names are LangChain's model-profile keys.
- **Bifrost's extras need `gateway="bifrost"`.** The default is `openai-compatible`: no
  `x-bf-*` header is sent, and `tools`, `execute_tool`, `mcp_logs`, `bf.mcp`, `options=` and
  `admin_token`/`admin_client` raise `BifrostOnlyError` before anything is sent. On Bifrost,
  every completion still sends the deny-all MCP scope (ADR 0003).
- **No dependency on `trellis-contracts`.** Its `Endpoint` and `ModelProfile` are mapped onto
  these arguments by the harness and the memory service: `base_url`, `gateway`,
  `auth_strategy(endpoint.auth, key)`, `ModelProfile(reasoning_output=, temperature=)`.

## Consequences
- 0.4.0, a minor release: a client for a Bifrost gateway passes `gateway="bifrost"`.
- The platform's variables are now `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_GATEWAY` and
  `LLM_AUTH` ([configuration.md](../configuration.md#the-names-the-other-repos-use)), replacing
  the `BIFROST_URL` and `BIFROST_VIRTUAL_KEY` ADR 0001 names.
- ADR 0001 holds for this package: it reads no environment variable. Without credentials
  passed in, google-auth and botocore find their own, as they always do. A bearer key may now
  be a provider's own key, sent to that provider's endpoint.
- The conformance suite (`tests/test_conformance.py`) runs every gateway, auth strategy and
  profile against stubs of each wire format, and recomputes the SigV4 signature from the
  specification.
