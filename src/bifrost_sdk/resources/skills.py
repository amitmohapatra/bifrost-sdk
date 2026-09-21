"""The skills repository: versioned Agent Skills the gateway stores and serves.

A skill is a ``SKILL.md`` plus its files, published under semantic versions. The gateway can
serve them to skill-aware harnesses directly, which is why the useful operations here are
read and version-shift rather than a full editor.
"""

from __future__ import annotations

from typing import Any

from bifrost_sdk.resources._base import Resource, unwrap


class Skills(Resource):
    """``bf.skills`` — the skills repository."""

    async def list(self, **filters: Any) -> list[dict[str, Any]]:
        return list(unwrap(await self._get("/api/skills", **filters), "skills") or [])

    async def get(self, skill_id: str) -> dict[str, Any]:
        return await self._get(f"/api/skills/{skill_id}")

    async def create(self, name: str, **fields: Any) -> dict[str, Any]:
        return await self._post("/api/skills", {"name": name, **fields})

    async def update(self, skill_id: str, **changes: Any) -> dict[str, Any]:
        return await self._put(f"/api/skills/{skill_id}", changes)

    async def delete(self, skill_id: str) -> None:
        await self._delete(f"/api/skills/{skill_id}")

    async def versions(self, skill_id: str) -> list[dict[str, Any]]:
        return list(unwrap(await self._get(f"/api/skills/{skill_id}/versions"), "versions") or [])

    async def shift_version(self, skill_id: str, version: str) -> dict[str, Any]:
        """Change which published version is *served*, without publishing a new one.

        The rollback path: serving is decoupled from publishing, so a bad skill version is
        undone by pointing at the previous one rather than by re-uploading it.
        """
        return await self._post(f"/api/skills/{skill_id}/shift-version", {"version": version})


__all__ = ["Skills"]
