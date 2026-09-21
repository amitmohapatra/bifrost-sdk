"""Virtual keys, budgets and the rest of the governance surface.

A virtual key is not an API key with a nicer name. It is the whole permission-and-spend
envelope for a caller: which providers and models, which MCP servers and which of their
tools, what budget, whose spend, at what rate. Both ``provider_configs`` and ``mcp_configs``
are **deny-by-default** — an empty list permits nothing, not everything.

That is why an application should carry a virtual key and almost no other configuration: the
allow-lists belong in one place that an administrator controls, not duplicated into every
agent's settings file where they drift and nobody can audit them.
"""

from __future__ import annotations

from typing import Any

from bifrost_sdk.resources._base import Resource, unwrap


class VirtualKeys(Resource):
    """``bf.vk`` — virtual keys and what they are allowed to do."""

    async def list(self, **filters: Any) -> list[dict[str, Any]]:
        payload = await self._get("/api/governance/virtual-keys", **filters)
        return list(unwrap(payload, "virtual_keys", "virtualKeys", "keys") or [])

    async def get(self, vk_id: str) -> dict[str, Any]:
        return await self._get(f"/api/governance/virtual-keys/{vk_id}")

    async def quota(self, **filters: Any) -> dict[str, Any]:
        """What is left of this key's budget and rate limit.

        The one governance route the gateway lets a *virtual key* call about itself, rather
        than requiring an admin token — so an agent can check its own remaining budget
        before starting expensive work.
        """
        return await self._get("/api/governance/virtual-keys/quota", **filters)

    async def create(
        self,
        name: str,
        *,
        provider_configs: list[dict[str, Any]] | None = None,
        mcp_configs: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Create a virtual key.

        ``provider_configs`` and ``mcp_configs`` are named explicitly because leaving them
        out is a decision, not a default: the key is created permitting nothing, and the
        first call made with it fails in a way that looks like a gateway fault.
        """
        body: dict[str, Any] = {"name": name, **fields}
        if provider_configs is not None:
            body["provider_configs"] = provider_configs
        if mcp_configs is not None:
            body["mcp_configs"] = mcp_configs
        return await self._post("/api/governance/virtual-keys", body)

    async def update(self, vk_id: str, **changes: Any) -> dict[str, Any]:
        return await self._put(f"/api/governance/virtual-keys/{vk_id}", changes)

    async def delete(self, vk_id: str) -> None:
        await self._delete(f"/api/governance/virtual-keys/{vk_id}")

    async def rotate(self, vk_id: str | None = None, **fields: Any) -> dict[str, Any]:
        """Issue a new secret for a key. Without ``vk_id`` this rotates in bulk."""
        path = (
            f"/api/governance/virtual-keys/{vk_id}/rotate"
            if vk_id
            else "/api/governance/virtual-keys/rotate"
        )
        return await self._post(path, fields or None)


class Governance(Resource):
    """``bf.governance`` — budgets, rate limits, teams and customers."""

    async def budgets(self, **filters: Any) -> list[dict[str, Any]]:
        return list(unwrap(await self._get("/api/governance/budgets", **filters), "budgets") or [])

    async def rate_limits(self, **filters: Any) -> list[dict[str, Any]]:
        payload = await self._get("/api/governance/rate-limits", **filters)
        return list(unwrap(payload, "rate_limits", "rateLimits") or [])

    async def teams(self, **filters: Any) -> list[dict[str, Any]]:
        return list(unwrap(await self._get("/api/governance/teams", **filters), "teams") or [])

    async def customers(self, **filters: Any) -> list[dict[str, Any]]:
        payload = await self._get("/api/governance/customers", **filters)
        return list(unwrap(payload, "customers") or [])


__all__ = ["Governance", "VirtualKeys"]
