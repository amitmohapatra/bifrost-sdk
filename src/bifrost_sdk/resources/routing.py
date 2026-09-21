"""Routing rules: which model or provider a request actually reaches.

A rule is a CEL expression evaluated per request against variables the gateway assembles —
``model``, ``provider``, ``request_type``, the caller's ``headers`` and ``params`` maps, the
governance identity (``virtual_key_id``, ``team_id``, ``customer_id``), live spend
(``tokens_used``, ``budget_used``) and a ``complexity_tier`` the gateway computes lazily,
only when a rule actually mentions it.

That last one is the interesting lever: it lets cheap questions fall to a small model without
the application deciding, or even knowing. A harness that reimplements model selection is
competing with this rather than using it.
"""

from __future__ import annotations

from typing import Any

from bifrost_sdk.resources._base import Resource, unwrap


class Routing(Resource):
    """``bf.routing`` — stored routing rules and the complexity analyzer."""

    async def rules(self, **filters: Any) -> list[dict[str, Any]]:
        return list(unwrap(await self._get("/api/routing/rules", **filters), "rules") or [])

    async def add_rule(self, **rule: Any) -> dict[str, Any]:
        """Create a routing rule. The matching half is a CEL expression."""
        return await self._post("/api/routing/rules", rule)

    async def update_rule(self, rule_id: str, **changes: Any) -> dict[str, Any]:
        return await self._put(f"/api/routing/rules/{rule_id}", changes)

    async def delete_rule(self, rule_id: str) -> None:
        await self._delete(f"/api/routing/rules/{rule_id}")

    # ------------------------------------------------------------------ complexity
    async def complexity_config(self) -> dict[str, Any]:
        return await self._get("/api/routing/complexity-analyzer-config")

    async def set_complexity_config(self, **config: Any) -> dict[str, Any]:
        return await self._put("/api/routing/complexity-analyzer-config", config)

    async def complexity_status(self) -> dict[str, Any]:
        """Whether the analyzer is ready. It warms asynchronously, and a rule that
        references ``complexity_tier`` before it is ready evaluates on an unknown value."""
        return await self._get("/api/routing/complexity-analyzer-status")


__all__ = ["Routing"]
