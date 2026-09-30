"""``bifrost_sdk.admin``: routes, unwrapping, and the deny-by-default trap.

Every path asserted here was read out of Bifrost's route table, not guessed: an unknown
``/api`` path answers 200 with the UI's HTML, so a wrong route fails late and confusingly.
"""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest

from bifrost_sdk import GatewayError, Unreachable
from bifrost_sdk.admin import Admin


def client(handler) -> Admin:
    transport = httpx.MockTransport(handler)
    return Admin(
        "http://gateway.test",
        client=httpx.AsyncClient(transport=transport, base_url="http://gateway.test"),
    )


def record(payload, status=200):
    seen: dict[str, object] = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["body"] = jsonlib.loads(request.content) if request.content else None
        return httpx.Response(status, json=payload)

    return seen, handler


# ----------------------------------------------------------------- prompts / skills


async def test_prompt_versions_use_the_nested_route() -> None:
    seen, handler = record({"versions": [{"version": 3}]})
    admin = client(handler)
    assert await admin.prompts.versions("p-1") == [{"version": 3}]
    assert seen["path"] == "/api/prompt-repo/prompts/p-1/versions"
    await admin.aclose()


async def test_skill_version_shift_is_a_post_not_an_update() -> None:
    """Serving is decoupled from publishing: rollback points at an existing version."""
    seen, handler = record({"served": "1.2.0"})
    admin = client(handler)
    await admin.skills.shift_version("s-1", "1.2.0")
    assert (seen["method"], seen["path"]) == ("POST", "/api/skills/s-1/shift-version")
    assert seen["body"] == {"version": "1.2.0"}
    await admin.aclose()


async def test_a_list_endpoint_without_an_envelope_still_works() -> None:
    _seen, handler = record([{"id": "s1"}])
    admin = client(handler)
    assert await admin.skills.list() == [{"id": "s1"}]
    await admin.aclose()


# ----------------------------------------------------------------- governance


async def test_quota_is_the_route_a_key_may_call_about_itself() -> None:
    seen, handler = record({"remaining": 12.5})
    admin = client(handler)
    assert await admin.vk.quota() == {"remaining": 12.5}
    assert seen["path"] == "/api/governance/virtual-keys/quota"
    await admin.aclose()


async def test_creating_a_key_without_configs_permits_nothing() -> None:
    """Deny-by-default is the gateway's rule; the SDK must not paper over it by inventing
    a permissive default. A key created bare fails on first use, and that has to be the
    caller's visible decision rather than a surprise."""
    seen, handler = record({"id": "vk1"})
    admin = client(handler)
    await admin.vk.create("triage-agent")
    assert seen["body"] == {"name": "triage-agent"}
    assert "provider_configs" not in seen["body"]
    await admin.aclose()


async def test_a_key_carries_its_provider_and_mcp_allow_lists() -> None:
    seen, handler = record({"id": "vk1"})
    admin = client(handler)
    await admin.vk.create(
        "triage-agent",
        provider_configs=[{"provider": "gemini", "allowed_models": ["gemini-3.6-flash"]}],
        mcp_configs=[{"mcp_client_name": "memory", "tools_to_execute": ["recall"]}],
    )
    assert seen["body"]["provider_configs"][0]["provider"] == "gemini"
    assert seen["body"]["mcp_configs"][0]["tools_to_execute"] == ["recall"]
    await admin.aclose()


async def test_bulk_rotate_and_single_rotate_use_different_routes() -> None:
    seen, handler = record({"rotated": 1})
    admin = client(handler)
    await admin.vk.rotate("vk1")
    assert seen["path"] == "/api/governance/virtual-keys/vk1/rotate"
    await admin.vk.rotate()
    assert seen["path"] == "/api/governance/virtual-keys/rotate"
    await admin.aclose()


# ----------------------------------------------------------------- failure modes


async def test_an_html_body_is_reported_as_a_bad_body_not_a_parse_crash() -> None:
    """What the invented ``tools()`` endpoint actually did: the SPA's catch-all answered 200
    with HTML, and the caller saw a JSONDecodeError with no hint of the real cause."""

    def handler(request):
        return httpx.Response(200, text="<!doctype html><html>…", headers={})

    admin = client(handler)
    with pytest.raises(GatewayError, match="non-JSON body"):
        await admin.prompts.list()
    await admin.aclose()


async def test_an_error_status_keeps_its_code() -> None:
    def handler(request):
        return httpx.Response(403, text="forbidden")

    admin = client(handler)
    with pytest.raises(GatewayError) as caught:
        await admin.vk.list()
    assert caught.value.details["status"] == 403
    await admin.aclose()


async def test_a_delete_returning_no_content_is_not_an_error() -> None:
    def handler(request):
        return httpx.Response(204)

    admin = client(handler)
    assert await admin.skills.delete("s1") is None
    await admin.aclose()


async def test_an_unreachable_gateway_says_so() -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    admin = client(handler)
    with pytest.raises(Unreachable):
        await admin.routing.rules()
    await admin.aclose()


def test_the_origin_is_derived_from_a_v1_url() -> None:
    admin = Admin("http://gateway.test:8080/v1", token="t")
    assert str(admin._http.base_url) == "http://gateway.test:8080"
    assert admin._http.headers["Authorization"] == "Bearer t"


def test_a_base_url_is_required() -> None:
    with pytest.raises(ValueError, match="base_url"):
        Admin("")


async def test_admin_closes_only_the_client_it_created() -> None:
    external = httpx.AsyncClient()
    async with Admin("http://gateway.test", client=external):
        pass
    assert not external.is_closed
    await external.aclose()
