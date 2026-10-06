# Architecture decision records

Each record says what was decided, why, and what it changed. A record is never rewritten
after it is accepted. A later decision **amends** it, or **supersedes** it as a whole, and
both headers say so.

These three record decisions made while the SDK was built; they were written down on
2026-10-06 from the code, the commits and the design notes that held them until then.

| ADR | Decision | Version | Status | Superseded by |
|---|---|---|---|---|
| [0001](0001-constructor-arguments-only.md) | Constructor arguments only; the gateway holds the credentials | 0.1.0 | accepted | — |
| [0002](0002-retries-and-the-circuit-breaker.md) | Retry only what is replayable; only a sick gateway trips the breaker | 0.3.0 | accepted | — |
| [0003](0003-completions-never-get-gateway-tools.md) | A completion never gets the gateway's MCP tools | 0.3.0 | accepted | — |

None is superseded. What each version changed: [CHANGELOG.md](../../CHANGELOG.md).
