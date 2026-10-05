"""Gateway administration: virtual keys, governance, routing rules, prompts, skills.

Operator tooling, kept apart from :class:`~bifrost_sdk.Bifrost` so an application's client
carries only what an application calls::

    from bifrost_sdk.admin import Admin

    async with Admin("http://gateway:8080", token=ADMIN_TOKEN) as admin:
        keys = await admin.vk.list()
"""

from __future__ import annotations

from typing import Any

import httpx

from bifrost_sdk._api import ManagementAPI, management_client
from bifrost_sdk.admin.governance import Governance, VirtualKeys
from bifrost_sdk.admin.prompts import Prompt, Prompts, PromptVersion
from bifrost_sdk.admin.routing import Routing
from bifrost_sdk.admin.skills import Skill, SkillFile, Skills, SkillVersion


class Admin:
    """The gateway's management API. ``base_url`` may be the origin or the ``/v1`` URL."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._http = client or management_client(base_url, token, timeout)
        self._owns_http = client is None
        api = ManagementAPI(self._http)
        self.vk = VirtualKeys(api)
        self.governance = Governance(api)
        self.routing = Routing(api)
        self.prompts = Prompts(api)
        self.skills = Skills(api)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Admin:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


__all__ = [
    "Admin",
    "Governance",
    "Prompt",
    "PromptVersion",
    "Prompts",
    "Routing",
    "Skill",
    "SkillFile",
    "SkillVersion",
    "Skills",
    "VirtualKeys",
]
