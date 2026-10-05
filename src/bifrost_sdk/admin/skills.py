"""The skills repository: versioned Agent Skills the gateway stores and serves.

A skill is a ``SKILL.md`` body plus files, published under immutable SemVer versions; one of
them is *served* (what marketplaces, downloads and a plain :meth:`Skills.get` see), and
serving is decoupled from publishing — a version can be published without being served, and
rollback is :meth:`Skills.shift_version`, not a re-upload.

Gateway facts this module encodes (verified against a running gateway):

* ``/api/skills/{id}`` takes the id only (a name answers 404): :meth:`Skills.find` resolves a
  name through the listing.
* The listing leaves ``skill_md_body`` empty and carries no files; :meth:`Skills.get` has both,
  for the served version or, with ``version=``, any published one.
* A file's bytes are not in any JSON answer. They are read from the public serving route
  (``/api/skills/serve/<name>/files/<path>``), which serves the **served** version only: a
  file of a version that is not served cannot be read back.
* Versions only go up (the next must exceed the highest ever published, served or not), and a
  version string cannot be reused (409).
"""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

from pydantic import Field

from bifrost_sdk._api import Resource
from bifrost_sdk._mcp import _Frozen

#: The gateway's maximum page size for ``GET /api/skills`` and its versions.
_PAGE = 100


class SkillFile(_Frozen):
    """A file attached to one version of a skill (its bytes: :meth:`Skills.read_file`)."""

    path: str
    #: ``text``, ``url``, ``dataurl`` or ``upload``.
    source_type: str
    mime_type: str = ""
    size: int = 0
    #: Where a ``url`` file is fetched from when the skill is served.
    source_url: str | None = None

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> SkillFile:
        return cls(
            path=entry["path"],
            source_type=entry["source_type"],
            mime_type=entry.get("mime_type") or "",
            size=entry.get("file_size_bytes") or 0,
            source_url=entry.get("source_url"),
        )


class Skill(_Frozen):
    """A skill as one version of it reads: the served one, unless another was asked for."""

    id: str
    name: str
    description: str
    #: The version these fields are from.
    version: str
    #: The ``SKILL.md`` body (the gateway composes the frontmatter from the other fields).
    #: Empty in a listing: :meth:`Skills.get` reads it.
    body: str = ""
    #: This version's files. Empty in a listing.
    files: tuple[SkillFile, ...] = ()
    #: The highest version ever published, served or not: the next must exceed it.
    highest_version: str | None = None
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    extra_frontmatter: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> Skill:
        return cls(
            id=entry["id"],
            name=entry["name"],
            description=entry.get("description") or "",
            version=entry["latest_version"],
            body=entry.get("skill_md_body") or "",
            files=tuple(SkillFile._from_gateway(f) for f in entry.get("files") or ()),
            highest_version=entry.get("highest_version") or None,
            license=entry.get("license"),
            compatibility=entry.get("compatibility"),
            allowed_tools=entry.get("allowed_tools"),
            metadata=dict(entry.get("metadata") or {}),
            extra_frontmatter=dict(entry.get("extra_frontmatter") or {}),
            created_at=entry.get("created_at"),
            updated_at=entry.get("updated_at"),
        )


class SkillVersion(_Frozen):
    """One published version, as the version history lists it."""

    id: str
    version: str
    created_at: datetime | None = None
    created_by: str | None = None

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> SkillVersion:
        return cls(
            id=entry["id"],
            version=entry["version"],
            created_at=entry.get("created_at"),
            created_by=entry.get("created_by"),
        )


def _files(files: Mapping[str, str]) -> list[dict[str, str]]:
    """``path -> text`` as the gateway's inline-text file entries, typed by extension."""
    return [
        {
            "path": path,
            "source_type": "text",
            "content": text,
            "mime_type": mimetypes.guess_type(path)[0] or "text/plain",
        }
        for path, text in files.items()
    ]


def _version(
    description: str, body: str, version: str, files: Mapping[str, str], fields: dict[str, Any]
) -> dict[str, Any]:
    return {
        "description": description,
        "skill_md_body": body,
        "version": version,
        "files": _files(files),
        **fields,
    }


class Skills(Resource):
    """``admin.skills`` — the skills repository."""

    async def list(self, search: str | None = None) -> list[Skill]:
        """Every skill (``search`` matches name or description), each as its served version
        reads, without body or files."""
        params = {"search": search} if search else {}
        entries = await self._api.pages("/api/skills", ("skills",), _PAGE, **params)
        return [Skill._from_gateway(entry) for entry in entries]

    async def get(self, skill_id: str, version: str | None = None) -> Skill:
        """A skill with its body and files: the served version, or ``version``.
        :class:`~bifrost_sdk.NotFoundError` for an unknown id or version."""
        params = {"version": version} if version else {}
        payload = await self._api.get(f"/api/skills/{skill_id}", **params)
        return Skill._from_gateway(payload["skill"])

    async def find(self, name: str, version: str | None = None) -> Skill | None:
        """The skill called ``name`` (names are unique) read as :meth:`get` reads it, or
        ``None`` when there is no such skill."""
        found = [skill for skill in await self.list(search=name) if skill.name == name]
        return await self.get(found[0].id, version) if found else None

    async def versions(self, skill_id: str) -> list[SkillVersion]:
        """Every published version, newest first."""
        entries = await self._api.pages(f"/api/skills/{skill_id}/versions", ("versions",), _PAGE)
        return [SkillVersion._from_gateway(entry) for entry in entries]

    async def read_file(self, name: str, path: str) -> bytes:
        """A file of the skill's **served** version, by its path in the skill.

        Read from the gateway's public serving route — the only place a file's bytes are
        available. A version that is not served has no such route.
        """
        return await self._api.content(
            f"/api/skills/serve/{quote(name, safe='')}/files/{quote(path)}"
        )

    async def create(
        self,
        name: str,
        *,
        description: str,
        body: str,
        version: str,
        files: Mapping[str, str] | None = None,
        **fields: Any,
    ) -> Skill:
        """Create a skill and publish (and serve) its first version.

        ``files`` maps a path inside the skill to its text, stored inline. ``fields`` are the
        other frontmatter fields as the gateway names them (``license``, ``compatibility``,
        ``metadata``, ``allowed_tools``, ``extra_frontmatter``). The name is permanent.
        """
        published = _version(description, body, version, files or {}, fields)
        payload = await self._api.post("/api/skills", {"name": name, **published})
        return Skill._from_gateway(payload["skill"])

    async def publish(
        self,
        skill_id: str,
        *,
        description: str,
        body: str,
        version: str,
        files: Mapping[str, str] | None = None,
        serve: bool = True,
        **fields: Any,
    ) -> Skill:
        """Publish a new immutable version: the whole skill as it should read in it — the
        files listed are its files, and a file left out is not carried over.

        ``serve=False`` publishes without serving it (the served version stays as it was).
        Returns the skill as its served version reads afterwards.
        """
        update = _version(description, body, version, files or {}, fields) | {"serve": serve}
        payload = await self._api.put(f"/api/skills/{skill_id}", update)
        return Skill._from_gateway(payload["skill"])

    async def shift_version(self, skill_id: str, version: str) -> Skill:
        """Serve another published version, without publishing a new one: the rollback path."""
        payload = await self._api.post(
            f"/api/skills/{skill_id}/shift-version", {"version": version}
        )
        return Skill._from_gateway(payload["skill"])

    async def delete(self, skill_id: str) -> None:
        """Delete a skill with every version and file."""
        await self._api.delete(f"/api/skills/{skill_id}")


__all__ = ["Skill", "SkillFile", "SkillVersion", "Skills"]
