"""The management namespaces: routes, unwrapping, and the deny-by-default trap.

Every path asserted here was read out of Bifrost's Go route table, not guessed. An earlier
version of this client shipped a ``tools()`` method against an endpoint that does not exist,
which returned the SPA's HTML and failed as a JSON parse error — these tests are what stops
that being possible twice.
"""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest

from bifrost_sdk import Bifrost, GatewayError

BASE = "http://gateway.test/v1"


def client(handler) -> Bifrost:
    transport = httpx.MockTransport(handler)
    return Bifrost(
        BASE,
        model="test/model",
        client=httpx.AsyncClient(transport=transport, base_url=BASE),
        admin_client=httpx.AsyncClient(transport=transport, base_url="http://gateway.test"),
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


# ----------------------------------------------------------------- mcp


async def test_mcp_clients_are_unwrapped_from_their_envelope() -> None:
    seen, handler = record({"clients": [{"name": "memory"}], "count": 1})
    bf = client(handler)
    assert await bf.mcp.clients() == [{"name": "memory"}]
    assert (seen["method"], seen["path"]) == ("GET", "/api/mcp/clients")
    await bf.aclose()


async def test_registering_a_server_posts_the_documented_shape() -> None:
    seen, handler = record({"id": "c1"})
    bf = client(handler)
    await bf.mcp.add("memory", connection_type="http", connection_string="http://mcp:8200/mcp")
    assert (seen["method"], seen["path"]) == ("POST", "/api/mcp/client")
    assert seen["body"] == {
        "name": "memory",
        "connection_type": "http",
        "connection_string": "http://mcp:8200/mcp",
    }
    await bf.aclose()


async def test_mcp_execute_goes_through_the_inference_client_not_the_admin_one() -> None:
    """Tool execution is a ``/v1`` route and must inherit the inference retry policy: a tool
    call failing on a 503 should be retried exactly like the completion that asked for it."""
    seen, handler = record({"role": "tool", "content": "42", "tool_call_id": "t1"})
    bf = client(handler)
    turn = await bf.mcp.execute({"id": "t1", "function": {"name": "answer", "arguments": "{}"}})
    assert turn["content"] == "42"
    assert seen["path"].endswith("/mcp/tool/execute")
    await bf.aclose()


# ----------------------------------------------------------------- prompts / skills


async def test_prompt_versions_use_the_nested_route() -> None:
    seen, handler = record({"versions": [{"version": 3}]})
    bf = client(handler)
    assert await bf.prompts.versions("p-1") == [{"version": 3}]
    assert seen["path"] == "/api/prompt-repo/prompts/p-1/versions"
    await bf.aclose()


async def test_skill_version_shift_is_a_post_not_an_update() -> None:
    """Serving is decoupled from publishing: rollback points at an existing version."""
    seen, handler = record({"served": "1.2.0"})
    bf = client(handler)
    await bf.skills.shift_version("s-1", "1.2.0")
    assert (seen["method"], seen["path"]) == ("POST", "/api/skills/s-1/shift-version")
    assert seen["body"] == {"version": "1.2.0"}
    await bf.aclose()


async def test_a_list_endpoint_without_an_envelope_still_works() -> None:
    _seen, handler = record([{"id": "s1"}])
    bf = client(handler)
    assert await bf.skills.list() == [{"id": "s1"}]
    await bf.aclose()


# ----------------------------------------------------------------- governance


async def test_quota_is_the_route_a_key_may_call_about_itself() -> None:
    seen, handler = record({"remaining": 12.5})
    bf = client(handler)
    assert await bf.vk.quota() == {"remaining": 12.5}
    assert seen["path"] == "/api/governance/virtual-keys/quota"
    await bf.aclose()


async def test_creating_a_key_without_configs_permits_nothing() -> None:
    """Deny-by-default is the gateway's rule; the SDK must not paper over it by inventing
    a permissive default. A key created bare fails on first use, and that has to be the
    caller's visible decision rather than a surprise."""
    seen, handler = record({"id": "vk1"})
    bf = client(handler)
    await bf.vk.create("triage-agent")
    assert seen["body"] == {"name": "triage-agent"}
    assert "provider_configs" not in seen["body"]
    await bf.aclose()


async def test_a_key_carries_its_provider_and_mcp_allow_lists() -> None:
    seen, handler = record({"id": "vk1"})
    bf = client(handler)
    await bf.vk.create(
        "triage-agent",
        provider_configs=[{"provider": "gemini", "allowed_models": ["gemini-3.6-flash"]}],
        mcp_configs=[{"mcp_client_name": "memory", "tools_to_execute": ["memory.recall"]}],
    )
    assert seen["body"]["provider_configs"][0]["provider"] == "gemini"
    assert seen["body"]["mcp_configs"][0]["tools_to_execute"] == ["memory.recall"]
    await bf.aclose()


async def test_bulk_rotate_and_single_rotate_use_different_routes() -> None:
    seen, handler = record({"rotated": 1})
    bf = client(handler)
    await bf.vk.rotate("vk1")
    assert seen["path"] == "/api/governance/virtual-keys/vk1/rotate"
    await bf.vk.rotate()
    assert seen["path"] == "/api/governance/virtual-keys/rotate"
    await bf.aclose()


# ----------------------------------------------------------------- failure modes


async def test_an_html_body_is_reported_as_a_bad_body_not_a_parse_crash() -> None:
    """What the invented ``tools()`` endpoint actually did: the SPA's catch-all answered 200
    with HTML, and the caller saw a JSONDecodeError with no hint of the real cause."""

    def handler(request):
        return httpx.Response(200, text="<!doctype html><html>…", headers={})

    bf = client(handler)
    with pytest.raises(GatewayError, match="non-JSON body"):
        await bf.prompts.list()
    await bf.aclose()


async def test_an_error_status_keeps_its_code() -> None:
    def handler(request):
        return httpx.Response(403, text="forbidden")

    bf = client(handler)
    with pytest.raises(GatewayError) as caught:
        await bf.vk.list()
    assert caught.value.details["status"] == 403
    await bf.aclose()


async def test_a_delete_returning_no_content_is_not_an_error() -> None:
    def handler(request):
        return httpx.Response(204)

    bf = client(handler)
    assert await bf.mcp.remove("c1") is None
    await bf.aclose()
