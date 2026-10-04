"""``bifrost_sdk.admin``: routes, unwrapping, and the deny-by-default trap.

Every path asserted here was read out of Bifrost's route table, not guessed: an unknown
``/api`` path answers 200 with the UI's HTML, so a wrong route fails late and confusingly.
"""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest
import respx

from bifrost_sdk import GatewayError, RateLimited, Unreachable
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


# ----------------------------------------------------------------- every route, once
#
# One row per admin method: what it sends (method, path, query, body) and what it returns.
# A row per method rather than a test per method, because the methods are deliberately thin —
# the route and the envelope key are the whole of what each one knows.

ITEM = {"id": "x-1"}

ROUTES = [
    # (call, method, path, query, body, answer, returned)
    (lambda a: a.vk.list(team_id="t"), "GET", "/api/governance/virtual-keys",
     {"team_id": "t"}, None, {"virtual_keys": [ITEM]}, [ITEM]),
    (lambda a: a.vk.list(), "GET", "/api/governance/virtual-keys",
     {}, None, {"virtualKeys": [ITEM]}, [ITEM]),
    (lambda a: a.vk.get("vk1"), "GET", "/api/governance/virtual-keys/vk1",
     {}, None, ITEM, ITEM),
    (lambda a: a.vk.quota(window="day"), "GET", "/api/governance/virtual-keys/quota",
     {"window": "day"}, None, {"remaining": 1}, {"remaining": 1}),
    (lambda a: a.vk.create("k", budget={"max_limit": 5}), "POST", "/api/governance/virtual-keys",
     {}, {"name": "k", "budget": {"max_limit": 5}}, ITEM, ITEM),
    (lambda a: a.vk.update("vk1", is_active=False), "PUT", "/api/governance/virtual-keys/vk1",
     {}, {"is_active": False}, ITEM, ITEM),
    (lambda a: a.vk.delete("vk1"), "DELETE", "/api/governance/virtual-keys/vk1",
     {}, None, None, None),
    (lambda a: a.vk.rotate("vk1", grace_seconds=60), "POST",
     "/api/governance/virtual-keys/vk1/rotate", {}, {"grace_seconds": 60}, ITEM, ITEM),
    (lambda a: a.vk.rotate(), "POST", "/api/governance/virtual-keys/rotate",
     {}, None, {"rotated": 3}, {"rotated": 3}),
    (lambda a: a.governance.budgets(), "GET", "/api/governance/budgets",
     {}, None, {"budgets": [ITEM]}, [ITEM]),
    (lambda a: a.governance.rate_limits(), "GET", "/api/governance/rate-limits",
     {}, None, {"rateLimits": [ITEM]}, [ITEM]),
    (lambda a: a.governance.teams(customer_id="c"), "GET", "/api/governance/teams",
     {"customer_id": "c"}, None, {"teams": [ITEM]}, [ITEM]),
    (lambda a: a.governance.customers(), "GET", "/api/governance/customers",
     {}, None, {"customers": [ITEM]}, [ITEM]),
    (lambda a: a.routing.rules(), "GET", "/api/routing/rules",
     {}, None, {"rules": [ITEM]}, [ITEM]),
    (lambda a: a.routing.add_rule(name="cheap", cel_expression="complexity_tier == 'low'"),
     "POST", "/api/routing/rules", {},
     {"name": "cheap", "cel_expression": "complexity_tier == 'low'"}, ITEM, ITEM),
    (lambda a: a.routing.update_rule("r1", enabled=False), "PUT", "/api/routing/rules/r1",
     {}, {"enabled": False}, ITEM, ITEM),
    (lambda a: a.routing.delete_rule("r1"), "DELETE", "/api/routing/rules/r1",
     {}, None, None, None),
    (lambda a: a.routing.complexity_config(), "GET", "/api/routing/complexity-analyzer-config",
     {}, None, {"enabled": True}, {"enabled": True}),
    (lambda a: a.routing.set_complexity_config(enabled=True), "PUT",
     "/api/routing/complexity-analyzer-config", {}, {"enabled": True}, ITEM, ITEM),
    (lambda a: a.routing.complexity_status(), "GET", "/api/routing/complexity-analyzer-status",
     {}, None, {"ready": False}, {"ready": False}),
    (lambda a: a.prompts.list(folder_id="f"), "GET", "/api/prompt-repo/prompts",
     {"folder_id": "f"}, None, {"prompts": [ITEM]}, [ITEM]),
    (lambda a: a.prompts.get("p1"), "GET", "/api/prompt-repo/prompts/p1", {}, None, ITEM, ITEM),
    (lambda a: a.prompts.create("triage", folder_id="f"), "POST", "/api/prompt-repo/prompts",
     {}, {"name": "triage", "folder_id": "f"}, ITEM, ITEM),
    (lambda a: a.prompts.update("p1", name="t2"), "PUT", "/api/prompt-repo/prompts/p1",
     {}, {"name": "t2"}, ITEM, ITEM),
    (lambda a: a.prompts.delete("p1"), "DELETE", "/api/prompt-repo/prompts/p1",
     {}, None, None, None),
    (lambda a: a.prompts.add_version("p1", commit_message="v2"), "POST",
     "/api/prompt-repo/prompts/p1/versions", {}, {"commit_message": "v2"}, ITEM, ITEM),
    (lambda a: a.prompts.version("v1"), "GET", "/api/prompt-repo/versions/v1",
     {}, None, ITEM, ITEM),
    (lambda a: a.prompts.folders(), "GET", "/api/prompt-repo/folders",
     {}, None, {"folders": [ITEM]}, [ITEM]),
    (lambda a: a.skills.get("s1"), "GET", "/api/skills/s1", {}, None, ITEM, ITEM),
    (lambda a: a.skills.create("triage", description="d"), "POST", "/api/skills",
     {}, {"name": "triage", "description": "d"}, ITEM, ITEM),
    (lambda a: a.skills.update("s1", description="e"), "PUT", "/api/skills/s1",
     {}, {"description": "e"}, ITEM, ITEM),
    (lambda a: a.skills.versions("s1"), "GET", "/api/skills/s1/versions",
     {}, None, {"versions": [ITEM]}, [ITEM]),
]  # fmt: skip


@pytest.mark.parametrize("route", ROUTES, ids=[f"{row[1]} {row[2]}" for row in ROUTES])
async def test_every_admin_method_reaches_its_route(route) -> None:
    call, method, path, query, body, answer, returned = route
    seen, handler = record(answer, status=200 if answer is not None else 204)
    admin = client(handler)
    assert await call(admin) == returned
    assert (seen["method"], seen["path"], seen["query"], seen["body"]) == (
        method,
        path,
        query,
        body,
    )
    await admin.aclose()


async def test_a_list_with_none_of_its_envelope_keys_is_empty() -> None:
    """An envelope the client does not recognise is reported as nothing, not as its keys."""
    _seen, handler = record({"data": [ITEM], "count": 1})
    admin = client(handler)
    assert await admin.governance.budgets() == []
    await admin.aclose()


async def test_a_rate_limited_admin_call_is_a_rate_limit_not_a_gateway_error() -> None:
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "2"})

    admin = client(handler)
    with pytest.raises(RateLimited) as caught:
        await admin.vk.list()
    assert caught.value.retry_after == 2.0
    await admin.aclose()


async def test_admin_calls_are_not_retried() -> None:
    """Most management calls are writes; replaying a POST that timed out can create twice."""
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    admin = client(handler)
    with pytest.raises(GatewayError):
        await admin.vk.create("k")
    assert len(calls) == 1
    await admin.aclose()


async def test_the_admin_token_is_the_bearer(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get("http://gateway.test:8080/api/skills").mock(
        return_value=httpx.Response(200, json={"skills": []})
    )
    async with Admin("http://gateway.test:8080/v1", token="admin-token-placeholder") as admin:
        assert await admin.skills.list() == []
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer admin-token-placeholder"
    assert admin._http.is_closed, "it closes the client it created"


async def test_without_a_token_no_authorization_is_sent(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get("http://gateway.test/api/routing/rules").mock(
        return_value=httpx.Response(200, json={"rules": []})
    )
    async with Admin("http://gateway.test") as admin:
        await admin.routing.rules()
    assert "Authorization" not in route.calls.last.request.headers
