# ADR 0001: Constructor arguments only; the gateway holds the credentials

**Status:** accepted · **Date:** 2026-09-21 (recorded 2026-10-06) · **Version:** 0.1.0

## Context
The SDK is imported by services that each have their own configuration story: the harness
reads `BIFROST_URL` and `BIFROST_VIRTUAL_KEY` from its environment, the memory service from
its pydantic settings, tests from fixtures. A client that also read the environment would
have two sources for the same value, and the one it picked would depend on import order and
on which shell ran the process. The live tests showed the cost: `BIFROST_URL` is set in
shells that never meant to talk to that gateway.

Provider keys are the other half. A client that carried them would need a provider SDK per
provider and would spread the keys into every process that calls a model.

## Decision
- **The SDK reads no environment variable and no file.** Every value is a constructor
  argument (`Bifrost(base_url, *, model, api_key, timeout, ...)`, `Admin(base_url, *, token,
  ...)`). The application decides where values come from.
- **It sends a virtual key, never a provider key**, and imports no provider SDK. Changing
  provider is changing a model string (`provider/model`).
- **Per-request behaviour is `Options`**, rendered to `x-bf-*` headers whose names are
  constants in `bifrost_sdk.headers`, so a misspelt header cannot be silently ignored.

## Consequences
- Each consumer names its own variables; the platform's convention is `BIFROST_URL` and
  `BIFROST_VIRTUAL_KEY` ([configuration.md](../configuration.md)).
- The only environment variables in this repo are the opt-in live tests'.
- A process serving several callers through one client sends each caller's key per request
  with `Options(virtual_key=...)`.
