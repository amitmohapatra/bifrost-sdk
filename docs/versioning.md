# Versioning and compatibility

## What goes with what

The pins below are copied from each package's `pyproject.toml` on `main`.

| Package | Version | Requires `bifrost-sdk` | How it is installed |
|---|---|---|---|
| `bifrost-sdk` | **0.4.0** | — | this package |
| `trellis-harness` (agent-harness) | **0.4.0** | `>=0.3` | a path dependency on `../bifrost-sdk`, editable |
| `trellis-memory-service` (agent-memory-service) | **0.3.0** | `>=0.3` | a vendored copy in `vendor/bifrost-sdk` (`make vendor`); its `tests/unit/test_vendored_sdk.py` fails until the copy is refreshed after a change here |
| `trellis-contracts` (agent-contracts) | **0.6.3** | not a dependency | `classify()` maps this SDK's exceptions by class name (`BifrostOnlyError` from 0.6.3) and `AgentError.of` keeps their `retryable`; its `Endpoint` and `ModelProfile` are mapped onto this SDK's arguments by the harness and the memory service |
| `agent-runs` / `trellis-runs` | **0.4.0** | not a dependency | runs make no model calls |
| `httpx` | `>=0.27` | — | runtime dependency |
| `pydantic` | `>=2.7` | — | runtime dependency |
| Python | `>=3.12` | — | |

The harness and the memory service use `gateway=`, `auth=` and `profile=` once they adopt
`LLM_GATEWAY` and `LLM_AUTH`, which needs 0.4.0: their pins should be raised to `>=0.4` in that
change, and the memory service's vendored copy refreshed (`make vendor`).

## The version rules

The package is pre-1.0.

* **Minor (0.x.0)** for a new surface (0.2.0: per-request `Options`, typed MCP clients,
  `bifrost_sdk.admin`; 0.3.0: prompts, skills, Virtual MCPs, deny-all completions; 0.4.0: any
  OpenAI-compatible endpoint, auth strategies, profiles) or a change a caller must adapt to.
* **Patch (0.x.y)** for fixes and additions that change no behaviour a caller relies on.
* The exception class names are part of the contract: `trellis-contracts` maps them by name,
  so renaming one is a breaking change for both.

## Where changes are recorded

* [CHANGELOG.md](../CHANGELOG.md): what each version changed.
* [adr/](adr/README.md): why.
