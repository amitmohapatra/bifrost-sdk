"""Shared plumbing for the management namespaces.

The inference verbs live on :class:`~bifrost_sdk._client.Bifrost` because they are the
reason the client exists. Everything else — MCP servers, the prompt repository, skills,
governance — is CRUD over ``/api/*``, and giving each its own flat method would produce a
class with a hundred of them that nobody can read. They are namespaces instead::

    await bf.mcp.clients()
    await bf.prompts.list()
    await bf.vk.quota()

Two things every namespace needs and none should reimplement: ``/api`` is a sibling of the
``/v1`` base URL the client was built with, and management routes take a bearer token rather
than the virtual key inference uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from bifrost_sdk._errors import GatewayError, Unreachable

if TYPE_CHECKING:  # pragma: no cover
    from bifrost_sdk._client import Bifrost

_ERROR = 400


class Resource:
    """One ``/api/*`` area of the gateway, bound to a client."""

    __slots__ = ("_bifrost",)

    def __init__(self, bifrost: Bifrost) -> None:
        self._bifrost = bifrost

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: Any = None
    ) -> Any:
        """One management call. Returns parsed JSON, or ``None`` for an empty body."""
        try:
            response = await self._bifrost._admin.request(method, path, json=json, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Unreachable(f"gateway unreachable ({type(exc).__name__})") from exc
        if response.status_code >= _ERROR:
            raise self._bifrost._error(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayError(
                f"{method} {path} returned a non-JSON body", body=response.text[:300]
            ) from exc

    # Thin aliases; they make the resource classes below read as route tables.
    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def _post(self, path: str, body: Any = None, **params: Any) -> Any:
        return await self._request("POST", path, json=body, params=params or None)

    async def _put(self, path: str, body: Any = None) -> Any:
        return await self._request("PUT", path, json=body)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)


def unwrap(payload: Any, *keys: str) -> Any:
    """Gateway list endpoints wrap their results; unwrap the first key that is present.

    ``{"clients": [...], "count": 3}`` and ``{"prompts": [...]}`` are the same shape with
    different names, and a caller should not have to know which one they are looking at.
    """
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                return payload[key]
    return payload


__all__ = ["Resource", "unwrap"]
