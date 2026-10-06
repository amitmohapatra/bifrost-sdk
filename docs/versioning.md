# Versioning and compatibility

## What goes with what

The pins below are copied from each package's `pyproject.toml` on `main`.

| Package | Version | Requires `bifrost-sdk` | How it is installed |
|---|---|---|---|
| `bifrost-sdk` | **0.3.0** | — | this package |
| `trellis-harness` (agent-harness) | **0.4.0** | `>=0.3` | a path dependency on `../bifrost-sdk`, editable |
| `trellis-memory-service` (agent-memory-service) | **0.3.0** | `>=0.2` | a vendored copy in `vendor/bifrost-sdk` (`make vendor`); its `tests/unit/test_vendored_sdk.py` fails until the copy is refreshed after a change here |
| `trellis-contracts` (agent-contracts) | **0.6.1** | not a dependency | `classify()` maps this SDK's exceptions by class name and `AgentError.of` keeps their `retryable` |
| `agent-runs` / `trellis-runs` | **0.4.0** | not a dependency | runs make no model calls |
| `httpx` | `>=0.27` | — | runtime dependency |
| `pydantic` | `>=2.7` | — | runtime dependency |
| Python | `>=3.12` | — | |

The memory service relies on `NO_GATEWAY_TOOLS` behaviour (the deny-all scope on its
completions), which needs 0.3.0, while its pin admits 0.2. Its vendored copy is 0.3.0, so it
works, but the pin should be raised to `>=0.3` there.

## The version rules

The package is pre-1.0.

* **Minor (0.x.0)** for a new surface (0.2.0: per-request `Options`, typed MCP clients,
  `bifrost_sdk.admin`; 0.3.0: prompts, skills, Virtual MCPs, deny-all completions) or a change
  a caller must adapt to.
* **Patch (0.x.y)** for fixes and additions that change no behaviour a caller relies on.
* The exception class names are part of the contract: `trellis-contracts` maps them by name,
  so renaming one is a breaking change for both.

## Where changes are recorded

* [CHANGELOG.md](../CHANGELOG.md): what each version changed.
* [adr/](adr/README.md): why.
