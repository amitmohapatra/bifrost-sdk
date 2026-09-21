"""MCP servers, sessions and tool execution.

The gateway discovers each registered server's tools and injects them into completions on
its own; what it deliberately does *not* do is run them. That is the useful half of the
bargain — see :meth:`MCP.execute`.
"""

from __future__ import annotations

from typing import Any

from bifrost_sdk.resources._base import Resource, unwrap


class MCP(Resource):
    """``bf.mcp`` — the servers the gateway can reach, and the tools they expose."""

    # ------------------------------------------------------------------ execution
    async def execute(
        self,
        tool_call: dict[str, Any],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - httpx transport timeout
    ) -> dict[str, Any]:
        """Run one tool call; return the ``{"role": "tool", ...}`` turn to append.

        Delegates to the client so inference and tool execution share one retry policy —
        a tool call that fails on a 503 should be retried exactly like the completion that
        asked for it.
        """
        return await self._bifrost.execute_tool(tool_call, timeout=timeout)

    # ------------------------------------------------------------------ servers
    async def clients(self, **filters: Any) -> list[dict[str, Any]]:
        """Every registered MCP server and its connection state."""
        return list(unwrap(await self._get("/api/mcp/clients", **filters), "clients") or [])

    async def add(
        self,
        name: str,
        *,
        connection_type: str,
        connection_string: str | None = None,
        **config: Any,
    ) -> dict[str, Any]:
        """Register an MCP server.

        ``name`` may not contain hyphens and ``connection_type`` is one of ``http``, ``sse``
        or ``stdio`` — both enforced by the gateway, which also refuses ``stdio`` and
        private-network targets to unauthenticated callers because either would let anyone
        who can reach the API run code or probe the internal network.
        """
        body: dict[str, Any] = {"name": name, "connection_type": connection_type, **config}
        if connection_string is not None:
            body["connection_string"] = connection_string
        return await self._post("/api/mcp/client", body)

    async def update(self, client_id: str, **changes: Any) -> dict[str, Any]:
        return await self._put(f"/api/mcp/client/{client_id}", changes)

    async def remove(self, client_id: str) -> None:
        await self._delete(f"/api/mcp/client/{client_id}")

    async def reconnect(self, client_id: str) -> dict[str, Any]:
        """Re-establish a dropped connection without re-registering the server."""
        return await self._post(f"/api/mcp/client/{client_id}/reconnect")

    # ------------------------------------------------------------------ sessions
    async def sessions(self, **filters: Any) -> list[dict[str, Any]]:
        return list(unwrap(await self._get("/api/mcp/sessions", **filters), "sessions") or [])

    async def end_session(self, session_id: str) -> None:
        await self._delete(f"/api/mcp/sessions/{session_id}")

    # ------------------------------------------------------------------ library
    async def library(self, **filters: Any) -> list[dict[str, Any]]:
        """The gateway's catalogue of installable MCP servers."""
        return list(
            unwrap(await self._get("/api/mcp/library", **filters), "library", "items") or []
        )


__all__ = ["MCP"]
