"""``bf.mcp``: the gateway's MCP clients."""

from __future__ import annotations

import json as jsonlib

import httpx

from bifrost_sdk import Bifrost

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
        seen["body"] = jsonlib.loads(request.content) if request.content else None
        return httpx.Response(status, json=payload)

    return seen, handler


async def test_mcp_clients_are_unwrapped_from_their_envelope() -> None:
    seen, handler = record({"clients": [{"name": "memory"}], "count": 1})
    bf = client(handler)
    assert await bf.mcp.clients() == [{"name": "memory"}]
    assert (seen["method"], seen["path"]) == ("GET", "/api/mcp/clients")
    await bf.aclose()


async def test_registering_a_server_posts_the_documented_shape() -> None:
    seen, handler = record({"status": "success"})
    bf = client(handler)
    await bf.mcp.add("memory", connection_type="http", connection_string="http://mcp:8200/mcp")
    assert (seen["method"], seen["path"]) == ("POST", "/api/mcp/client")
    assert seen["body"] == {
        "name": "memory",
        "connection_type": "http",
        "connection_string": "http://mcp:8200/mcp",
    }
    await bf.aclose()
