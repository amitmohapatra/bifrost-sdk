# ADR 0003: A completion never gets the gateway's MCP tools

**Status:** accepted · **Date:** 2026-10-05 (recorded 2026-10-06) · **Version:** 0.3.0

## Context
A Bifrost virtual key with MCP access makes the gateway add the key's MCP tools to every
completion made with it. The model is then offered tools the caller never declared (a
two-tool server granted to the key took "say hi" from 32 to 272 prompt tokens), and the
gateway's agent loop runs, itself, any call to a tool listed in a client's
`tools_to_auto_execute`. Neither the caller's governance nor its records see that call. In
Trellis the caller's governance (`trellis.harness.governance`) is what decides whether a tool
call may run, so a call it cannot see is a hole.

## Decision
- `chat`, `json`, `stream` and `complete` always send the two MCP scope headers present and
  empty (`x-bf-mcp-include-clients: ""`, `x-bf-mcp-include-tools: ""`), the gateway's
  deny-all. Under it nothing is added and that execution is refused (logged as an error).
- An `Options` with a non-empty `mcp_clients` or `mcp_tools` on a completion raises
  `ValueError` before anything is sent. The MCP scope is for `execute_tool`.
- The header pair is exported as `bifrost_sdk.NO_GATEWAY_TOOLS`, so a framework's own model
  client pointed at the gateway sends the same thing.
- The caller lists tools (`tools()`), offers them as `tools=`, checks each call and runs it
  (`execute_tool`).

## Consequences
- No header switches the gateway's agent loop off: with a client that has
  `tools_to_auto_execute`, the refusal is fed back to the model. So keep it empty, which
  `MCPClientConfig` does by default.
- The live suite checks both halves against a real gateway.
- The sequence is in
  [ARCHITECTURE.md](../ARCHITECTURE.md#a-completion-never-gets-the-gateways-mcp-tools).
