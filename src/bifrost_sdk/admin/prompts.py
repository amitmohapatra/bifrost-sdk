"""The prompt repository: stored, versioned prompts the gateway injects.

Injection happens at inference time, by header: a completion sent with
``options=Options(prompt_id=prompt.id, prompt_version=3)`` has version 3's messages
**prepended** to its own, and the version's ``model_params`` applied wherever the request did
not set them (the gateway's ``prompts`` plugin). Without ``prompt_version`` the latest
committed version is used. An unknown id or version is not an error: the gateway logs a
warning and sends the request without the template — so resolve a prompt here, once, rather
than trusting a remembered id.

These methods manage what is stored. Gateway facts they encode (verified against a running
gateway): every answer is wrapped (``{"prompt": ...}``, ``{"version": ...}``); prompt names
are not unique; a version is immutable, numbered per prompt from 1, and stores its messages
as opaque JSON (plain chat messages work); deleting a prompt deletes its versions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import Field

from bifrost_sdk._api import Resource
from bifrost_sdk._mcp import _Frozen


class PromptVersion(_Frozen):
    """One committed, immutable version of a prompt."""

    #: The gateway's row id (``GET /api/prompt-repo/versions/{id}``) — not the version number.
    id: int
    prompt_id: str
    #: What ``Options(prompt_version=)`` selects: 1, 2, 3... per prompt.
    number: int
    #: The chat messages the gateway prepends, in order.
    messages: tuple[dict[str, Any], ...] = ()
    model_params: dict[str, Any] = Field(default_factory=dict)
    #: ``provider/model`` the version was written for (the request's own model still wins).
    model: str = ""
    commit_message: str = ""
    is_latest: bool = False
    created_at: datetime | None = None

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> PromptVersion:
        rows = sorted(entry.get("messages") or (), key=lambda row: row.get("order_index", 0))
        provider, model = entry.get("provider") or "", entry.get("model") or ""
        return cls(
            id=entry["id"],
            prompt_id=entry["prompt_id"],
            number=entry["version_number"],
            messages=tuple(row["message"] for row in rows),
            model_params=dict(entry.get("model_params") or {}),
            model=f"{provider}/{model}" if provider else model,
            commit_message=entry.get("commit_message") or "",
            is_latest=bool(entry.get("is_latest")),
            created_at=entry.get("created_at"),
        )


class Prompt(_Frozen):
    """A stored prompt, with its latest committed version when it has one."""

    #: What ``Options(prompt_id=)`` carries.
    id: str
    name: str
    folder_id: str | None = None
    #: ``None`` until a version is committed: such a prompt injects nothing.
    latest_version: PromptVersion | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> Prompt:
        latest = entry.get("latest_version")
        return cls(
            id=entry["id"],
            name=entry["name"],
            folder_id=entry.get("folder_id"),
            latest_version=PromptVersion._from_gateway(latest) if latest else None,
            created_at=entry.get("created_at"),
            updated_at=entry.get("updated_at"),
        )


class Prompts(Resource):
    """``admin.prompts`` — the stored-prompt repository."""

    async def list(self, folder_id: str | None = None) -> list[Prompt]:
        params = {"folder_id": folder_id} if folder_id else {}
        entries = await self._api.items("/api/prompt-repo/prompts", ("prompts",), **params)
        return [Prompt._from_gateway(entry) for entry in entries]

    async def get(self, prompt_id: str) -> Prompt:
        """A prompt by id; :class:`~bifrost_sdk.NotFoundError` if there is none."""
        payload = await self._api.get(f"/api/prompt-repo/prompts/{prompt_id}")
        return Prompt._from_gateway(payload["prompt"])

    async def find(self, name: str) -> Prompt | None:
        """The prompt called ``name``, or ``None``.

        Names are what people remember and ids are what the header needs, so this is how an
        application turns one into the other. The gateway does not keep names unique: two
        prompts with the same name raise ``ValueError`` rather than one being picked.
        """
        found = [prompt for prompt in await self.list() if prompt.name == name]
        if len(found) > 1:
            raise ValueError(f"{len(found)} prompts are named {name!r}; use an id")
        return found[0] if found else None

    async def create(self, name: str, *, folder_id: str | None = None) -> Prompt:
        body = {"name": name, **({"folder_id": folder_id} if folder_id else {})}
        payload = await self._api.post("/api/prompt-repo/prompts", body)
        return Prompt._from_gateway(payload["prompt"])

    async def update(self, prompt_id: str, **changes: Any) -> Prompt:
        """Rename or move a prompt (``name=``, ``folder_id=``). Its messages change only by
        committing a new version."""
        payload = await self._api.put(f"/api/prompt-repo/prompts/{prompt_id}", changes)
        return Prompt._from_gateway(payload["prompt"])

    async def delete(self, prompt_id: str) -> None:
        """Delete a prompt with all its versions."""
        await self._api.delete(f"/api/prompt-repo/prompts/{prompt_id}")

    # ------------------------------------------------------------------ versions
    async def versions(self, prompt_id: str) -> list[PromptVersion]:
        """Every committed version, as the gateway orders them."""
        entries = await self._api.items(
            f"/api/prompt-repo/prompts/{prompt_id}/versions", ("versions",)
        )
        return [PromptVersion._from_gateway(entry) for entry in entries]

    async def commit(
        self,
        prompt_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        model_params: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> PromptVersion:
        """Commit a new version: the next number, and from now on the latest.

        ``messages`` are chat messages (``{"role": "system", "content": ...}``), prepended to
        every request that selects this version. ``model`` is ``provider/model``, the form
        every other call in this package takes; ``model_params`` are defaults a request's own
        parameters override. ``message`` is the commit message.
        """
        provider, _, name = model.partition("/")
        if not (provider and name):
            raise ValueError(f"model must be 'provider/model', got {model!r}")
        body = {
            "commit_message": message,
            "messages": [dict(m) for m in messages],
            "model_params": dict(model_params or {}),
            "provider": provider,
            "model": name,
        }
        payload = await self._api.post(f"/api/prompt-repo/prompts/{prompt_id}/versions", body)
        return PromptVersion._from_gateway(payload["version"])

    async def version(self, version_id: int) -> PromptVersion:
        """A version by its row id (:attr:`PromptVersion.id`), not its number."""
        payload = await self._api.get(f"/api/prompt-repo/versions/{version_id}")
        return PromptVersion._from_gateway(payload["version"])

    # ------------------------------------------------------------------ folders
    async def folders(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._api.items("/api/prompt-repo/folders", ("folders",), **filters)


__all__ = ["Prompt", "PromptVersion", "Prompts"]
