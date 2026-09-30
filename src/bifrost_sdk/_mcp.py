"""``bf.mcp``: the MCP servers registered in the gateway."""

from __future__ import annotations

from typing import Any

from bifrost_sdk._api import Resource


class MCP(Resource):
    """List and manage the gateway's MCP clients."""

    async def clients(self) -> list[dict[str, Any]]:
        """Every registered MCP server and its connection state."""
        return await self._api.items("/api/mcp/clients", ("clients",))

    async def add(
        self,
        name: str,
        *,
        connection_type: str,
        connection_string: str | None = None,
        **config: Any,
    ) -> None:
        """Register an MCP server."""
        body: dict[str, Any] = {"name": name, "connection_type": connection_type, **config}
        if connection_string is not None:
            body["connection_string"] = connection_string
        await self._api.post("/api/mcp/client", body)

    async def update(self, client_id: str, **changes: Any) -> None:
        await self._api.put(f"/api/mcp/client/{client_id}", changes)

    async def remove(self, client_id: str) -> None:
        await self._api.delete(f"/api/mcp/client/{client_id}")
