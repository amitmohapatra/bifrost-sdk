"""MCP: typed client listing and CRUD, scoped tool listing, and tool execution.

Listing payloads are shaped like the running gateway's ``GET /api/mcp/clients`` answer
(bare tool names, ``connection_string`` wrapped as ``{"value", "type"}``).
"""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest
from pydantic import ValidationError

from bifrost_sdk import (
    Bifrost,
    GatewayError,
    MCPClient,
    MCPClientConfig,
    MCPConnection,
    Options,
    ToolDef,
    Unreachable,
)

BASE = "http://gateway.test/v1"


def client(handler) -> Bifrost:
    transport = httpx.MockTransport(handler)
    return Bifrost(
        BASE,
        model="test/model",
        client=httpx.AsyncClient(transport=transport, base_url=BASE),
        admin_client=httpx.AsyncClient(transport=transport, base_url="http://gateway.test"),
    )


def entry(
    name: str,
    tools: list[str],
    *,
    allowed: list[str] | None = None,
    code_mode: bool = False,
    disabled: bool = False,
) -> dict:
    return {
        "config": {
            "client_id": f"id-{name}",
            "name": name,
            "is_code_mode_client": code_mode,
            "connection_type": "http",
            "connection_string": {"value": f"https://{name}.test/mcp", "type": "plain_text"},
            "tools_to_execute": ["*"] if allowed is None else allowed,
            "disabled": disabled,
        },
        "tools": [
            {"name": t, "description": f"{t} it", "parameters": {"type": "object"}} for t in tools
        ],
        "state": "healthy",
    }


def listing(*entries: dict):
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"clients": list(entries), "count": len(entries)})

    return requests, handler


def names(tools: list[ToolDef]) -> list[str]:
    return [t.name for t in tools]


# ----------------------------------------------------------------- listing clients


async def test_clients_are_parsed_into_typed_models() -> None:
    _, handler = listing(entry("erp", ["get_stock"], code_mode=True))
    bf = client(handler)
    [erp] = await bf.mcp.clients()
    assert erp == MCPClient(
        id="id-erp",
        state="healthy",
        config=MCPClientConfig(
            name="erp",
            connection=MCPConnection(type="http", url="https://erp.test/mcp"),
            tools_to_execute=("*",),
            is_code_mode_client=True,
        ),
        tools=(
            ToolDef(
                name="erp-get_stock",
                client="erp",
                description="get_stock it",
                parameters={"type": "object"},
                code_mode=True,
            ),
        ),
    )
    await bf.aclose()


async def test_clients_are_read_across_every_page() -> None:
    """The gateway pages at 25 by default and at most 100."""
    offsets: list[str] = []

    def handler(request):
        offsets.append(request.url.params["offset"])
        assert request.url.params["limit"] == "100"
        start = int(request.url.params["offset"])
        count = 100 if start == 0 else 3
        page = [entry(f"c{start + i}", []) for i in range(count)]
        return httpx.Response(200, json={"clients": page})

    bf = client(handler)
    assert len(await bf.mcp.clients()) == 103
    assert offsets == ["0", "100"]
    await bf.aclose()


def test_a_client_name_may_not_contain_the_tool_separator() -> None:
    with pytest.raises(ValidationError, match="may not contain"):
        MCPClientConfig(
            name="my-erp", connection=MCPConnection(type="http", url="u"), tools_to_execute=()
        )


def test_tools_to_execute_must_be_chosen_explicitly() -> None:
    """An empty list lets nothing run, so there is no default to fall into."""
    with pytest.raises(ValidationError, match="tools_to_execute"):
        MCPClientConfig(name="erp", connection=MCPConnection(type="http", url="u"))  # type: ignore[call-arg]


# ----------------------------------------------------------------- client CRUD


def recorder():
    seen: list[tuple[str, str, object]] = []

    def handler(request):
        body = jsonlib.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"status": "success"})

    return seen, handler


ERP = MCPClientConfig(
    name="erp",
    connection=MCPConnection(type="http", url="https://erp.test/mcp"),
    tools_to_execute=("get_stock", "create_po"),
)


async def test_adding_a_client_posts_the_gateway_shape() -> None:
    seen, handler = recorder()
    bf = client(handler)
    await bf.mcp.add(ERP)
    assert seen == [
        (
            "POST",
            "/api/mcp/client",
            {
                "name": "erp",
                "connection_type": "http",
                "connection_string": "https://erp.test/mcp",
                "is_code_mode_client": False,
                "tools_to_execute": ["get_stock", "create_po"],
                "tools_to_auto_execute": [],
            },
        )
    ]
    await bf.aclose()


async def test_only_http_and_sse_clients_with_a_url_can_be_added() -> None:
    seen, handler = recorder()
    bf = client(handler)
    for connection in (MCPConnection(type="stdio"), MCPConnection(type="http")):
        with pytest.raises(ValueError, match="http/sse"):
            await bf.mcp.add(ERP.model_copy(update={"connection": connection}))
    assert seen == []
    await bf.aclose()


def current(config: MCPClientConfig = ERP) -> MCPClient:
    return MCPClient(id="id-erp", config=config, state="healthy")


async def test_updating_sends_only_the_mutable_fields() -> None:
    seen, handler = recorder()
    bf = client(handler)
    desired = ERP.model_copy(update={"is_code_mode_client": True, "tools_to_execute": ("*",)})
    await bf.mcp.update(current(), desired)
    assert seen == [
        (
            "PUT",
            "/api/mcp/client/id-erp",
            {
                "name": "erp",
                "is_code_mode_client": True,
                "tools_to_execute": ["*"],
                "tools_to_auto_execute": [],
            },
        )
    ]
    await bf.aclose()


async def test_a_connection_change_is_refused_rather_than_silently_ignored() -> None:
    """The gateway answers 200 to a PUT with a new connection_string and keeps the old one."""
    seen, handler = recorder()
    bf = client(handler)
    moved = ERP.model_copy(
        update={"connection": MCPConnection(type="http", url="https://erp2.test/mcp")}
    )
    with pytest.raises(ValueError, match="remove and add"):
        await bf.mcp.update(current(), moved)
    assert seen == []
    await bf.aclose()


async def test_removing_a_client_is_a_delete_by_id() -> None:
    seen, handler = recorder()
    bf = client(handler)
    assert await bf.mcp.remove("id-erp") is None
    assert seen == [("DELETE", "/api/mcp/client/id-erp", None)]
    await bf.aclose()


# ----------------------------------------------------------------- scoped tool listing


GATEWAY = (
    entry("erp", ["get_stock", "create_po"]),
    entry("crm", ["find", "update"], allowed=["find"]),
    entry("docs", ["search"], code_mode=True),
    entry("old", ["gone"], disabled=True),
    entry("locked", ["secret"], allowed=[]),
)


async def test_unscoped_lists_every_executable_tool_of_enabled_clients() -> None:
    """``tools_to_execute`` is enforced at execution, so tools outside it are not offered."""
    _, handler = listing(*GATEWAY)
    bf = client(handler)
    assert names(await bf.tools()) == [
        "erp-get_stock",
        "erp-create_po",
        "crm-find",
        "docs-search",
    ]
    await bf.aclose()


@pytest.mark.parametrize(
    ("clients", "only", "expected"),
    [
        (["erp"], None, ["erp-get_stock", "erp-create_po"]),
        (["*"], ["crm-find"], ["crm-find"]),
        (None, ["erp-*", "docs-search"], ["erp-get_stock", "erp-create_po", "docs-search"]),
        (["crm"], ["erp-get_stock"], []),
        ([], None, []),
        (None, [], []),
        (None, ["*"], []),  # a bare * is not a tool pattern (the gateway rejects it too)
        (None, ["get_stock"], []),  # bare tool names never match
    ],
)
async def test_scoping_follows_the_include_header_semantics(clients, only, expected) -> None:
    _, handler = listing(*GATEWAY)
    bf = client(handler)
    assert names(await bf.tools(clients=clients, only=only)) == expected
    await bf.aclose()


async def test_code_mode_tools_are_marked() -> None:
    _, handler = listing(*GATEWAY)
    bf = client(handler)
    marked = {t.name: t.code_mode for t in await bf.tools()}
    assert marked["docs-search"] is True
    assert marked["erp-get_stock"] is False
    await bf.aclose()


# ----------------------------------------------------------------- execution


async def test_execute_tool_returns_a_turn_ready_to_append() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"role": "tool", "content": "42", "tool_call_id": "call_1"})

    bf = client(handler)
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "erp-answer", "arguments": "{}"},
    }
    turn = await bf.execute_tool(call)
    assert turn == {"role": "tool", "content": "42", "tool_call_id": "call_1"}
    assert seen["path"] == "/v1/mcp/tool/execute"
    assert seen["query"] == {"format": "chat"}
    assert seen["body"] == call
    await bf.aclose()


async def test_execute_tool_sends_scope_and_correlation_headers() -> None:
    seen: dict[str, str] = {}

    def handler(request):
        seen.update({k: v for k, v in request.headers.items() if k.startswith("x-bf-")})
        return httpx.Response(200, json={"role": "tool", "content": "ok"})

    bf = client(handler)
    options = Options(mcp_tools=["erp-get_stock"], parent_request_id="run-7")
    await bf.execute_tool({"function": {"name": "executeToolCode"}}, options=options)
    assert seen == {"x-bf-mcp-include-tools": "erp-get_stock", "x-bf-parent-request-id": "run-7"}
    await bf.aclose()


async def test_a_tool_call_with_no_name_is_refused_before_a_request_is_sent() -> None:
    sent: list[httpx.Request] = []
    bf = client(lambda r: (sent.append(r), httpx.Response(200, json={}))[1])
    with pytest.raises(ValueError, match="no function name"):
        await bf.execute_tool({"id": "call_1", "type": "function", "function": {}})
    assert sent == []
    await bf.aclose()


async def test_a_refused_tool_keeps_its_status_and_is_not_retried() -> None:
    """The gateway answers 400 for a tool outside the scope or the client's allow-list."""
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "not permitted"}})

    bf = client(handler)
    with pytest.raises(GatewayError) as caught:
        await bf.execute_tool({"function": {"name": "erp-create_po", "arguments": "{}"}})
    assert caught.value.details["status"] == 400
    assert len(calls) == 1
    await bf.aclose()


async def test_an_unreachable_gateway_is_not_reported_as_a_tool_failure() -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    bf = client(handler)
    with pytest.raises(Unreachable):
        await bf.execute_tool({"function": {"name": "erp-answer", "arguments": "{}"}})
    await bf.aclose()


async def test_a_non_json_tool_result_is_a_gateway_error() -> None:
    bf = client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(GatewayError, match="non-JSON"):
        await bf.execute_tool({"function": {"name": "erp-answer", "arguments": "{}"}})
    await bf.aclose()
