"""The prompt repository: stored, versioned prompts the gateway injects.

Injection happens at inference time via headers, not by fetching text and pasting it into a
request — ``bf.prompt(id, version=3).chat(...)``. These methods are for *managing* what is
stored, and for reading a prompt back when an application needs to show or diff it.
"""

from __future__ import annotations

from typing import Any

from bifrost_sdk.resources._base import Resource, unwrap


class Prompts(Resource):
    """``bf.prompts`` — the stored-prompt repository."""

    async def list(self, **filters: Any) -> list[dict[str, Any]]:
        return list(unwrap(await self._get("/api/prompt-repo/prompts", **filters), "prompts") or [])

    async def get(self, prompt_id: str) -> dict[str, Any]:
        return await self._get(f"/api/prompt-repo/prompts/{prompt_id}")

    async def create(self, name: str, **fields: Any) -> dict[str, Any]:
        return await self._post("/api/prompt-repo/prompts", {"name": name, **fields})

    async def update(self, prompt_id: str, **changes: Any) -> dict[str, Any]:
        return await self._put(f"/api/prompt-repo/prompts/{prompt_id}", changes)

    async def delete(self, prompt_id: str) -> None:
        await self._delete(f"/api/prompt-repo/prompts/{prompt_id}")

    # ------------------------------------------------------------------ versions
    async def versions(self, prompt_id: str) -> list[dict[str, Any]]:
        """Committed versions, newest first as the gateway returns them.

        A prompt without a committed version cannot be injected: the header selects a
        version, and omitting it selects the *latest committed* one — so a draft is invisible
        to inference no matter how it looks in the UI.
        """
        payload = await self._get(f"/api/prompt-repo/prompts/{prompt_id}/versions")
        return list(unwrap(payload, "versions") or [])

    async def add_version(self, prompt_id: str, **fields: Any) -> dict[str, Any]:
        return await self._post(f"/api/prompt-repo/prompts/{prompt_id}/versions", fields)

    async def version(self, version_id: str) -> dict[str, Any]:
        return await self._get(f"/api/prompt-repo/versions/{version_id}")

    # ------------------------------------------------------------------ folders
    async def folders(self, **filters: Any) -> list[dict[str, Any]]:
        return list(unwrap(await self._get("/api/prompt-repo/folders", **filters), "folders") or [])


__all__ = ["Prompts"]
