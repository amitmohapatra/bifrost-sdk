"""Virtual MCPs: curated bundles of tools from one or more MCP clients, served at
``/mcp/<slug>`` and reachable only through the virtual keys they are attached to.

The two ways a key reaches them differ, and that is the point of having them:

* through the gateway's MCP endpoint, ``/mcp/<slug>`` serves exactly that bundle —
  ``Bifrost.tools(slug=...)`` and ``Bifrost.execute_tool(..., slug=...)`` — while the plain
  ``/mcp`` serves the union of everything the key can reach;
* a key's attached bundles also join that union for ``/v1/mcp/tool/execute``.

Gateway facts this module encodes (verified against a running gateway):

* The ``tools`` map is keyed by MCP client **id** (``MCPClient.id``). The gateway also accepts
  a client *name* when saving, but serves nothing for it.
* ``["*"]`` is every tool of the client, including later ones; ``[]`` is none.
* The slug is derived from the name when not given, unique across bundles and MCP clients
  (409 on a clash), and permanent: an update ignores it.
* A slug the key is not attached to answers 403. Requires governance on the gateway.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from bifrost_sdk._api import Resource
from bifrost_sdk._mcp import _Frozen

#: The gateway's maximum page size for ``GET /api/mcp/virtual-mcps``.
_PAGE = 100


class VirtualMCP(_Frozen):
    """A Virtual MCP, with the virtual keys it is attached to."""

    id: int
    name: str
    #: Served at ``/mcp/<slug>``.
    slug: str
    #: MCP client id → the tool names it contributes (``("*",)``: all).
    tools: dict[str, tuple[str, ...]]
    enabled: bool = True
    description: str | None = None
    virtual_key_ids: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> VirtualMCP:
        return cls(
            id=entry["id"],
            name=entry["name"],
            slug=entry["endpoint_slug"],
            tools={
                spec["mcp_client_id"]: tuple(spec.get("tool_names") or ())
                for spec in entry.get("tools") or ()
            },
            enabled=bool(entry.get("enabled")),
            description=entry.get("description"),
            virtual_key_ids=tuple(entry.get("virtual_key_ids") or ()),
            created_at=entry.get("created_at"),
            updated_at=entry.get("updated_at"),
        )


def _specs(tools: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
    return [{"mcp_client_id": client, "tool_names": list(names)} for client, names in tools.items()]


class VirtualMCPs(Resource):
    """``admin.virtual_mcps`` — Virtual MCPs and their virtual-key attachments."""

    async def list(self, search: str | None = None) -> list[VirtualMCP]:
        params = {"search": search} if search else {}
        entries = await self._api.pages("/api/mcp/virtual-mcps", ("virtual_mcps",), _PAGE, **params)
        return [VirtualMCP._from_gateway(entry) for entry in entries]

    async def get(self, vmcp_id: int) -> VirtualMCP:
        payload = await self._api.get(f"/api/mcp/virtual-mcps/{vmcp_id}")
        return VirtualMCP._from_gateway(payload["virtual_mcp"])

    async def create(
        self,
        name: str,
        tools: Mapping[str, Sequence[str]],
        *,
        slug: str | None = None,
        description: str | None = None,
        enabled: bool = True,
    ) -> VirtualMCP:
        """Create a bundle of ``tools`` (MCP client id → tool names, ``["*"]`` for all).

        ``slug`` defaults to one derived from ``name`` and can never be changed afterwards.
        It is reachable through no key until :meth:`attach`.
        """
        body: dict[str, Any] = {"name": name, "tools": _specs(tools), "enabled": enabled}
        if slug is not None:
            body["endpoint_slug"] = slug
        if description is not None:
            body["description"] = description
        payload = await self._api.post("/api/mcp/virtual-mcps", body)
        return VirtualMCP._from_gateway(payload["virtual_mcp"])

    async def update(
        self,
        vmcp_id: int,
        *,
        name: str | None = None,
        tools: Mapping[str, Sequence[str]] | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> VirtualMCP:
        """Change what is given; leave the rest as it is. ``tools`` replaces the whole map."""
        changes: dict[str, Any] = {
            key: value
            for key, value in (("name", name), ("description", description), ("enabled", enabled))
            if value is not None
        }
        if tools is not None:
            changes["tools"] = _specs(tools)
        payload = await self._api.put(f"/api/mcp/virtual-mcps/{vmcp_id}", changes)
        return VirtualMCP._from_gateway(payload["virtual_mcp"])

    async def delete(self, vmcp_id: int) -> None:
        """Stop serving it and detach it from every key."""
        await self._api.delete(f"/api/mcp/virtual-mcps/{vmcp_id}")

    async def attach(self, vmcp_id: int, vk_id: str) -> None:
        """Let the virtual key ``vk_id`` (its id, not its secret) reach this bundle."""
        await self._api.post(f"/api/mcp/virtual-mcps/{vmcp_id}/virtual-keys/{vk_id}")

    async def detach(self, vmcp_id: int, vk_id: str) -> None:
        await self._api.delete(f"/api/mcp/virtual-mcps/{vmcp_id}/virtual-keys/{vk_id}")


__all__ = ["VirtualMCP", "VirtualMCPs"]
