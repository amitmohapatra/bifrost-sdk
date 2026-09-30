"""The prompt repository: stored, versioned prompts the gateway injects.

Injection happens at inference time via headers —
``bf.chat(..., options=Options(prompt_id=id, prompt_version=3))``. These methods manage what
is stored, and read a prompt back when an application needs to show or diff it.
"""

from __future__ import annotations

from typing import Any

from bifrost_sdk._api import Resource


class Prompts(Resource):
    """``admin.prompts`` — the stored-prompt repository."""

    async def list(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._api.items("/api/prompt-repo/prompts", ("prompts",), **filters)

    async def get(self, prompt_id: str) -> dict[str, Any]:
        return await self._api.get(f"/api/prompt-repo/prompts/{prompt_id}")

    async def create(self, name: str, **fields: Any) -> dict[str, Any]:
        return await self._api.post("/api/prompt-repo/prompts", {"name": name, **fields})

    async def update(self, prompt_id: str, **changes: Any) -> dict[str, Any]:
        return await self._api.put(f"/api/prompt-repo/prompts/{prompt_id}", changes)

    async def delete(self, prompt_id: str) -> None:
        await self._api.delete(f"/api/prompt-repo/prompts/{prompt_id}")

    # ------------------------------------------------------------------ versions
    async def versions(self, prompt_id: str) -> list[dict[str, Any]]:
        """Committed versions, newest first as the gateway returns them.

        A prompt without a committed version cannot be injected: the header selects a
        version, and omitting it selects the *latest committed* one — so a draft is invisible
        to inference no matter how it looks in the UI.
        """
        return await self._api.items(
            f"/api/prompt-repo/prompts/{prompt_id}/versions", ("versions",)
        )

    async def add_version(self, prompt_id: str, **fields: Any) -> dict[str, Any]:
        return await self._api.post(f"/api/prompt-repo/prompts/{prompt_id}/versions", fields)

    async def version(self, version_id: str) -> dict[str, Any]:
        return await self._api.get(f"/api/prompt-repo/versions/{version_id}")

    # ------------------------------------------------------------------ folders
    async def folders(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._api.items("/api/prompt-repo/folders", ("folders",), **filters)


__all__ = ["Prompts"]
