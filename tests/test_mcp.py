"""MCP: typed client listing and CRUD, scoped tool listing, and tool execution.

Listing payloads are shaped like the running gateway's ``GET /api/mcp/clients`` answer
(bare tool names, ``connection_string`` wrapped as ``{"value", "type"}``).
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from bifrost_sdk import (
    Bifrost,
    GatewayError,
    MCPClient,
    MCPClientConfig,
    MCPConnection,
    MCPLog,
    Options,
    PermissionDeniedError,
    RateLimited,
    ServerError,
    ToolAnnotations,
    ToolDef,
    Unreachable,
)
from bifrost_sdk._client import CODE_MODE_READS
from bifrost_sdk._mcp import declared_tools

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


def listing(*entries: dict, mcp: httpx.Response | None = None):
    """The client listing; ``mcp`` answers the gateway's ``POST /mcp`` (default: every tool
    listed, none annotated — a key that may use them all)."""
    requests: list[httpx.Request] = []
    every = [
        {"name": f"{e['config']['name']}-{t['name']}", "annotations": {}}
        for e in entries
        for t in e.get("tools") or ()
    ]

    def handler(request):
        requests.append(request)
        if request.url.path == "/mcp":
            return mcp or rpc_tools(every)
        return httpx.Response(200, json={"clients": list(entries), "count": len(entries)})

    return requests, handler


def rpc_tools(tools: list[dict]) -> httpx.Response:
    """The gateway MCP endpoint's JSON-RPC ``tools/list`` answer."""
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}})


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


async def test_a_clients_forwarded_headers_are_read_back() -> None:
    """``allowed_extra_headers``: what the gateway forwards from a caller to the server."""
    listed = entry("erp", ["get_stock"])
    listed["config"]["allowed_extra_headers"] = ["x-user-token"]
    _, handler = listing(listed)
    bf = client(handler)
    [erp] = await bf.mcp.clients()
    assert erp.config.allowed_extra_headers == ("x-user-token",)
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


# ----------------------------------------------------------------- tool annotations


async def test_annotations_come_from_the_gateway_mcp_listing() -> None:
    """``/api/mcp/clients`` drops annotations; the gateway's ``/mcp`` keeps them by full name."""
    hints = {"readOnlyHint": True, "destructiveHint": False, "title": "Stock"}
    mcp = rpc_tools(
        [
            {"name": "erp-get_stock", "inputSchema": {}, "annotations": hints},
            {"name": "erp-create_po", "inputSchema": {}, "annotations": {}},
        ]
    )
    requests, handler = listing(entry("erp", ["get_stock", "create_po"]), mcp=mcp)
    bf = client(handler)
    stock, po = (await bf.mcp.clients())[0].tools
    assert stock.annotations == ToolAnnotations(read_only_hint=True, destructive_hint=False)
    assert stock.annotations.idempotent_hint is None
    assert po.annotations is None
    [call] = [r for r in requests if r.url.path == "/mcp"]
    assert call.method == "POST"
    assert jsonlib.loads(call.content)["method"] == "tools/list"
    await bf.aclose()


def gateway_mcp(tools: list[dict], files: dict[str, str] | None = None, path: str = "/mcp"):
    """The gateway's ``POST /mcp`` (or ``path``, a ``/mcp/<slug>``) for one virtual key:
    ``tools/list`` answers ``tools`` (with the meta-tools when ``files`` names Code Mode
    clients), ``listToolFiles`` and ``readToolFile`` answer from ``files`` (``<client>`` -> its
    declarations)."""
    requests: list[httpx.Request] = []
    meta = [{"name": n, "annotations": {}} for n in ("executeToolCode", "listToolFiles")]

    def text(value: str) -> httpx.Response:
        result = {"content": [{"type": "text", "text": value}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def handler(request):
        requests.append(request)
        if request.url.path != path:
            return httpx.Response(401, text="Unauthorized")
        body = jsonlib.loads(request.content)
        if body["method"] == "tools/list":
            return rpc_tools([*tools, *(meta if files else [])])
        call = body["params"]
        if call["name"] == "listToolFiles":
            listed = "".join(f"\n  {c}.pyi" for c in files or {})
            return text(f"# Workflow: listToolFiles -> readToolFile\n\nservers/{listed}")
        client_name = call["arguments"]["fileName"].removeprefix("servers/").removesuffix(".pyi")
        return text((files or {})[client_name])

    return requests, handler


async def test_tools_are_the_virtual_keys_mcp_listing_never_the_admin_api() -> None:
    """With admin auth on, ``/api/*`` refuses a virtual key: the listing is ``POST /mcp``
    ``tools/list`` with the key, which holds exactly what the key allows."""
    tools = [
        {
            "name": "erp-get_stock",
            "description": "Stock of a SKU",
            "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
        {"name": "crm-find", "inputSchema": {"type": "object"}, "annotations": {}},
    ]
    requests, handler = gateway_mcp(tools)
    transport = httpx.MockTransport(handler)
    bf = Bifrost(
        BASE,
        api_key="vk-1",
        client=httpx.AsyncClient(
            transport=transport, base_url=BASE, headers={"Authorization": "Bearer vk-1"}
        ),
        admin_client=httpx.AsyncClient(transport=transport, base_url="http://gateway.test"),
    )
    stock, find = await bf.tools()
    assert (stock.name, stock.client, stock.description) == (
        "erp-get_stock",
        "erp",
        "Stock of a SKU",
    )
    assert stock.parameters["properties"] == {"sku": {"type": "string"}}
    assert stock.annotations == ToolAnnotations(read_only_hint=True)
    assert (find.client, find.annotations, find.code_mode) == ("crm", None, False)
    assert [r.url.path for r in requests] == ["/mcp"]
    assert requests[0].headers["Authorization"] == "Bearer vk-1"
    assert jsonlib.loads(requests[0].content)["method"] == "tools/list"
    await bf.aclose()


async def test_malformed_annotations_leave_a_tool_listed_without_them() -> None:
    _, handler = gateway_mcp([{"name": "erp-get_stock", "annotations": {"readOnlyHint": "x"}}])
    bf = client(handler)
    [tool] = await bf.tools()
    assert (tool.name, tool.annotations) == ("erp-get_stock", None)
    await bf.aclose()


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, text="Unauthorized"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}}),
    ],
    ids=["refused", "rpc-error"],
)
async def test_a_listing_the_gateway_refuses_is_an_error_not_an_empty_toolbox(answer) -> None:
    bf = client(lambda request: answer)
    with pytest.raises(GatewayError):
        await bf.tools()
    await bf.aclose()


DECLARATIONS = """# Total lines: 4 (this is the complete file, no need to paginate)
# docs server tools

def search(query: str, limit: int = None, tags: list[str] = None) -> dict:  # Search the docs.
def fetch(path: str, options: dict[str, Any], raw: Any) -> dict:  # Fetch one page...
"""


async def test_code_mode_clients_are_read_from_the_meta_tools_declarations() -> None:
    """``tools/list`` shows a Code Mode client's meta-tools, not its tools: they come from
    ``listToolFiles`` and ``readToolFile``, marked ``code_mode``, with no annotations."""
    requests, handler = gateway_mcp(
        [{"name": "erp-get_stock", "annotations": {}}], files={"docs": DECLARATIONS}
    )
    bf = client(handler)
    tools = await bf.tools()
    assert names(tools) == ["erp-get_stock", "docs-search", "docs-fetch"]
    erp, search, fetch = tools
    assert not erp.code_mode and search.code_mode and fetch.code_mode
    assert (search.client, search.description, search.annotations) == (
        "docs",
        "Search the docs.",
        None,
    )
    assert search.parameters == {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "tags": {"type": "array"},
        },
        "required": ["query"],
    }
    assert fetch.parameters["properties"] == {
        "path": {"type": "string"},
        "options": {"type": "object"},
        "raw": {},
    }
    assert fetch.parameters["required"] == ["path", "options", "raw"]
    calls = [jsonlib.loads(r.content)["params"] for r in requests[1:]]
    assert calls == [
        {"name": "listToolFiles", "arguments": {}},
        {"name": "readToolFile", "arguments": {"fileName": "servers/docs.pyi"}},
    ]
    await bf.aclose()


async def test_a_slug_lists_one_virtual_mcp_code_mode_declarations_included() -> None:
    """``/mcp/<slug>`` serves one Virtual MCP; every call of the listing goes there."""
    requests, handler = gateway_mcp(
        [{"name": "erp-get_stock", "annotations": {}}],
        files={"docs": DECLARATIONS},
        path="/mcp/finance-tools",
    )
    bf = client(handler)
    assert names(await bf.tools(slug="finance-tools")) == [
        "erp-get_stock",
        "docs-search",
        "docs-fetch",
    ]
    assert {r.url.path for r in requests} == {"/mcp/finance-tools"}
    await bf.aclose()


async def test_a_slug_the_key_is_not_attached_to_is_permission_denied() -> None:
    bf = client(lambda r: httpx.Response(403, json={"error": {"message": "access_denied"}}))
    with pytest.raises(PermissionDeniedError):
        await bf.tools(slug="finance-tools")
    await bf.aclose()


async def test_no_annotation_lookup_without_tools() -> None:
    requests, handler = listing(entry("empty", []))
    bf = client(handler)
    await bf.mcp.clients()
    assert [r.url.path for r in requests] == ["/api/mcp/clients"]
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
                "allowed_extra_headers": [],
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
    desired = ERP.model_copy(
        update={
            "is_code_mode_client": True,
            "tools_to_execute": ("*",),
            "allowed_extra_headers": ("x-user-token",),
        }
    )
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
                "allowed_extra_headers": ["x-user-token"],
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


#: What the key's listing holds: erp's two tools, crm's ``find``, and Code Mode ``docs``.
KEY_TOOLS = [{"name": n, "annotations": {}} for n in ("erp-get_stock", "erp-create_po", "crm-find")]
KEY_FILES = {"docs": "def search(query: str) -> dict:  # Search.\n"}


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
    _, handler = gateway_mcp(KEY_TOOLS, files=KEY_FILES)
    bf = client(handler)
    assert names(await bf.tools(clients=clients, only=only)) == expected
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


def rpc_result(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def test_execute_through_a_slug_is_a_json_rpc_call_answered_as_a_turn() -> None:
    """Through ``/mcp/<slug>`` only that bundle's tools are permitted; the answer has the
    shape ``/v1/mcp/tool/execute`` gives, and carries the caller's identity headers."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return rpc_result({"content": [{"type": "text", "text": "7"}, {"type": "image"}]})

    bf = client(handler)
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "erp-get_stock", "arguments": '{"sku": "A1"}'},
    }
    options = Options(mcp_session_id="user-9", extra={"x-user-token": "alice"})
    turn = await bf.execute_tool(call, options=options, timeout=3.0, slug="finance-tools")
    assert turn == {"role": "tool", "content": "7", "tool_call_id": "call_1"}
    [request] = seen
    assert request.url.path == "/mcp/finance-tools"
    assert jsonlib.loads(request.content) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "erp-get_stock", "arguments": {"sku": "A1"}},
    }
    assert request.headers["x-bf-mcp-session-id"] == "user-9"
    assert request.headers["x-user-token"] == "alice"
    assert request.extensions["timeout"]["read"] == 3.0
    await bf.aclose()


async def test_a_failed_tool_through_a_slug_is_an_error_turn() -> None:
    """The gateway refuses a tool outside the bundle in the result, not with a status."""
    refused = {"content": [{"type": "text", "text": "not permitted"}], "isError": True}
    bf = client(lambda r: rpc_result(refused))
    call = {"id": "c", "function": {"name": "erp-create_po", "arguments": {"sku": "A1"}}}
    turn = await bf.execute_tool(call, slug="finance-tools")
    assert turn == {
        "role": "tool",
        "content": "not permitted",
        "tool_call_id": "c",
        "is_error": True,
    }
    await bf.aclose()


async def test_a_slug_call_without_arguments_sends_an_empty_object() -> None:
    seen: list[dict] = []

    def handler(request):
        seen.append(jsonlib.loads(request.content)["params"])
        return rpc_result({"content": []})

    bf = client(handler)
    await bf.execute_tool({"function": {"name": "erp-ping"}}, slug="s")
    await bf.execute_tool({"function": {"name": "erp-ping", "arguments": ""}}, slug="s")
    assert seen == [{"name": "erp-ping", "arguments": {}}] * 2
    await bf.aclose()


@pytest.mark.parametrize(
    ("arguments", "message"), [("{not json", "not JSON"), ("[1, 2]", "not a JSON object")]
)
async def test_unusable_arguments_are_refused_before_a_slug_call(arguments, message) -> None:
    sent: list[httpx.Request] = []
    bf = client(lambda r: (sent.append(r), rpc_result({}))[1])
    call = {"function": {"name": "erp-ping", "arguments": arguments}}
    with pytest.raises(ValueError, match=message):
        await bf.execute_tool(call, slug="s")
    assert sent == []
    await bf.aclose()


# ----------------------------------------------------------------- MCP logs

SINCE = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)

#: Shaped like the gateway's GET /api/mcp-logs entries.
LOGGED = {
    "id": "log-1",
    "request_id": "log-1",
    "llm_request_id": "run-7",
    "timestamp": "2026-09-30T00:06:02.926531806Z",
    "tool_name": "read_wiki_structure",
    "server_label": "docs",
    "status": "success",
    "arguments": '{"repoName":"maximhq/bifrost"}',
    "result": {"content": "Available pages"},
    "latency": 353,
}
FAILED = {
    "id": "log-2",
    "timestamp": "2026-09-30T00:06:03Z",
    "tool_name": "x",
    "status": "error",
    "arguments": "not json",
    "error_details": {"error": {"message": "tool 'x' is not available or not permitted"}},
}


async def test_mcp_logs_are_read_oldest_first_from_since() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"logs": [LOGGED, FAILED], "pagination": {}})

    bf = client(handler)
    ok, failed = await bf.mcp_logs(SINCE, limit=50, parent_request_id="run-7")
    assert seen["path"] == "/api/mcp-logs"
    assert seen["query"] == {
        "start_time": "2026-09-30T00:05:00+00:00",
        "limit": "50",
        "sort_by": "timestamp",
        "order": "asc",
        "llm_request_ids": "run-7",
    }
    assert ok == MCPLog(
        id="log-1",
        timestamp=datetime(2026, 9, 30, 0, 6, 2, 926531, tzinfo=UTC),
        client="docs",
        tool="read_wiki_structure",
        status="success",
        parent_request_id="run-7",
        arguments={"repoName": "maximhq/bifrost"},
        result={"content": "Available pages"},
        latency_ms=353,
    )
    assert ok.name == "docs-read_wiki_structure"
    assert failed.error == "tool 'x' is not available or not permitted"
    assert failed.arguments == "not json", "unparseable arguments are kept verbatim"
    assert failed.parent_request_id is None
    await bf.aclose()


async def test_without_a_parent_no_request_filter_is_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json={"logs": []})

    bf = client(handler)
    assert await bf.mcp_logs(SINCE) == []
    assert "llm_request_ids" not in seen
    assert seen["limit"] == "100"
    await bf.aclose()


@pytest.mark.parametrize(
    ("since", "limit", "message"),
    [
        (datetime(2026, 9, 30), 10, "timezone-aware"),
        (SINCE, 0, "between 1 and 1000"),
        (SINCE, 1001, "between 1 and 1000"),
    ],
)
async def test_bad_log_queries_are_refused_before_a_request(since, limit, message) -> None:
    sent: list[httpx.Request] = []
    bf = client(lambda r: (sent.append(r), httpx.Response(200, json={"logs": []}))[1])
    with pytest.raises(ValueError, match=message):
        await bf.mcp_logs(since, limit=limit)
    assert sent == []
    await bf.aclose()


# ----------------------------------------------------------------- parsing edge cases


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (("*",), ["erp-get_stock", "erp-create_po"]),
        (("get_stock",), ["erp-get_stock"]),
        ((), []),
    ],
    ids=["all", "one", "none"],
)
def test_executable_is_what_tools_to_execute_lets_run(allowed, expected) -> None:
    """The listing shows every discovered tool; the allow-list decides which may run."""
    tools = tuple(ToolDef(name=f"erp-{t}", client="erp") for t in ("get_stock", "create_po"))
    erp = MCPClient(
        id="id-erp",
        config=ERP.model_copy(update={"tools_to_execute": allowed}),
        state="healthy",
        tools=tools,
    )
    assert names(list(erp.executable)) == expected


async def test_a_plain_connection_string_and_a_bare_entry_parse() -> None:
    """``connection_string`` may be a plain string or absent; ``state`` may be missing."""
    plain = entry("erp", [], disabled=True)
    plain["config"]["connection_string"] = "https://erp.test/mcp"
    plain.pop("state")
    bare = entry("crm", [])
    bare["config"].pop("connection_string")
    _, handler = listing(plain, bare)
    bf = client(handler)
    erp, crm = await bf.mcp.clients()
    assert erp.config.connection == MCPConnection(type="http", url="https://erp.test/mcp")
    assert (erp.state, erp.disabled) == ("", True)
    assert crm.config.connection == MCPConnection(type="http", url=None)
    await bf.aclose()


@pytest.mark.parametrize(
    "mcp",
    [
        httpx.Response(401, text="Unauthorized"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": "none"}}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=["refused", "tools-not-a-list", "rpc-error", "not-an-object"],
)
async def test_clients_are_listed_without_annotations_when_the_mcp_listing_fails(mcp) -> None:
    """Annotations are an enrichment: the admin registry is still the answer without them."""
    _, handler = listing(entry("erp", ["get_stock"]), mcp=mcp)
    bf = client(handler)
    [erp] = await bf.mcp.clients()
    assert [(t.name, t.annotations) for t in erp.tools] == [("erp-get_stock", None)]
    await bf.aclose()


async def test_logged_arguments_that_are_already_structured_are_kept() -> None:
    logged = {**LOGGED, "arguments": {"repoName": "x"}, "server_label": None}
    bf = client(lambda r: httpx.Response(200, json={"logs": [logged]}))
    [log] = await bf.mcp_logs(SINCE)
    assert (log.arguments, log.client, log.latency_ms) == ({"repoName": "x"}, "", 353)
    await bf.aclose()


async def test_a_log_page_without_an_envelope_or_a_body_is_read_as_is() -> None:
    answers = [httpx.Response(200, json=[LOGGED]), httpx.Response(200)]
    bf = client(lambda r: answers.pop(0))
    assert [log.id for log in await bf.mcp_logs(SINCE)] == ["log-1"]
    assert await bf.mcp_logs(SINCE) == []
    await bf.aclose()


async def test_listing_entries_that_are_not_tools_are_skipped() -> None:
    """Junk entries, nameless entries, a non-object schema and non-object hints are dropped
    one field at a time rather than failing the whole listing."""
    tools = [
        {"name": "erp-get_stock", "inputSchema": "not a schema", "annotations": ["x"]},
        "junk",
        {"description": "nameless"},
        {"name": "standalone", "description": None},
    ]
    _, handler = gateway_mcp(tools)
    bf = client(handler)
    stock, standalone = await bf.tools()
    assert (stock.parameters, stock.annotations) == ({}, None)
    assert (standalone.client, standalone.description) == ("standalone", "")
    await bf.aclose()


async def test_a_listing_with_no_tools_key_is_empty() -> None:
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {}}
    bf = client(lambda r: httpx.Response(200, json=rpc))
    assert await bf.tools() == []
    await bf.aclose()


@pytest.mark.parametrize(
    ("answer", "error", "message"),
    [
        (httpx.ConnectError("refused"), Unreachable, "ConnectError"),
        (httpx.Response(200, text="<html>"), GatewayError, "non-JSON"),
        (httpx.Response(200, json={"jsonrpc": "2.0", "result": []}), GatewayError, "failed"),
        (httpx.Response(200, json=[]), GatewayError, "failed: None"),
        (httpx.Response(429, text="slow down"), RateLimited, "rate limited"),
    ],
    ids=["unreachable", "non-json", "result-not-an-object", "not-an-object", "rate-limited"],
)
async def test_each_way_the_mcp_endpoint_can_fail_has_its_error(answer, error, message) -> None:
    def handler(request):
        if isinstance(answer, Exception):
            raise answer
        return answer

    bf = client(handler)
    with pytest.raises(error, match=message):
        await bf.tools()
    await bf.aclose()


async def test_a_failing_meta_tool_is_an_error_not_an_empty_code_mode_client() -> None:
    def handler(request):
        if jsonlib.loads(request.content)["method"] == "tools/list":
            return rpc_tools([{"name": "listToolFiles"}])
        result = {"isError": True, "content": [{"type": "text", "text": "denied"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    bf = client(handler)
    with pytest.raises(GatewayError, match="listToolFiles failed") as caught:
        await bf.tools()
    assert "denied" in caught.value.details["body"]
    await bf.aclose()


async def test_meta_tool_text_ignores_parts_that_are_not_text() -> None:
    """``readToolFile`` text may arrive in several parts, some of them not text at all."""

    def handler(request):
        body = jsonlib.loads(request.content)
        if body["method"] == "tools/list":
            return rpc_tools([{"name": "readToolFile"}])
        if body["params"]["name"] == "listToolFiles":
            content = [{"type": "text", "text": "servers/\n  docs.pyi\n  docs.pyi"}]
        else:
            content = [
                {"type": "text", "text": "def a() -> dict:  # A."},
                {"type": "image", "text": None},
                "junk",
                {"type": "text", "text": "def b(x: int) -> dict:"},
            ]
        result = {"content": content}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    bf = client(handler)
    a, b = await bf.tools()
    assert (a.name, a.description, a.parameters) == (
        "docs-a",
        "A.",
        {"type": "object", "properties": {}},
    )
    assert (b.name, b.description) == ("docs-b", "")
    assert b.parameters == {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }
    await bf.aclose()


def test_a_declaration_is_split_on_top_level_commas_only() -> None:
    """Nested brackets do not split a parameter; ``*`` / ``*args`` and a trailing comma do not
    become parameters of their own."""
    [tool] = declared_tools(
        "erp",
        "def f(m: dict[str, list[int]], t: tuple[int, int] = (1, 2), *, flag: bool, "
        "*args: str,) -> dict:  # F.",
    )
    assert tool.parameters == {
        "type": "object",
        "properties": {
            "m": {"type": "object"},
            "t": {},
            "flag": {"type": "boolean"},
            "args": {"type": "string"},
        },
        "required": ["m", "flag", "args"],
    }


async def test_execute_tool_passes_a_deadline_for_this_call_only() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"role": "tool", "content": "ok"})

    bf = client(handler)
    await bf.execute_tool({"function": {"name": "erp-answer"}}, timeout=1.5)
    assert seen["timeout"] == {"connect": 1.5, "pool": 1.5, "read": 1.5, "write": 1.5}
    await bf.aclose()


async def test_execute_tool_is_not_counted_by_the_breaker() -> None:
    """A tool's failure is the tool's, not the gateway's: it must not stop completions."""
    bf = client(lambda r: httpx.Response(500, text="tool crashed"))
    for _ in range(6):
        with pytest.raises(GatewayError):
            await bf.execute_tool({"function": {"name": "erp-answer"}})
    assert bf._breaker.consecutive_failures == 0
    await bf.aclose()


def test_tool_annotations_parse_from_either_spelling() -> None:
    wire = ToolAnnotations.model_validate({"readOnlyHint": True, "openWorldHint": False})
    python = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    assert wire == python
    assert (python.destructive_hint, python.idempotent_hint) == (None, None)


def code_mode_gateway(clients: list[str], *, failing: frozenset[str] = frozenset()):
    """A gateway whose ``readToolFile`` answers take longer the earlier the client is listed
    (so the reads finish in reverse order), recording how many were in flight at once."""
    state = {"in_flight": 0, "peak": 0}

    async def handler(request):
        body = jsonlib.loads(request.content)
        if body["method"] == "tools/list":
            return rpc_tools([{"name": "listToolFiles"}, {"name": "readToolFile"}])
        call = body["params"]
        if call["name"] == "listToolFiles":
            text = "servers/" + "".join(f"\n  {c}.pyi" for c in clients)
        else:
            name = call["arguments"]["fileName"].removeprefix("servers/").removesuffix(".pyi")
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            await asyncio.sleep(0.002 * (len(clients) - clients.index(name)))
            state["in_flight"] -= 1
            if name in failing:
                return httpx.Response(503, text=f"{name} down")
            text = f"def lookup_{name}() -> dict:  # {name}."
        result = {"content": [{"type": "text", "text": text}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return state, handler


async def test_code_mode_files_are_read_concurrently_bounded_and_in_listed_order() -> None:
    clients = [f"c{i}" for i in range(9)]
    state, handler = code_mode_gateway(clients)
    bf = client(handler)
    tools = await bf.tools()
    assert names(tools) == [f"{c}-lookup_{c}" for c in clients]
    assert 1 < state["peak"] <= CODE_MODE_READS
    await bf.aclose()


async def test_a_failed_code_mode_read_fails_the_listing_with_the_first_listed_failure() -> None:
    """The later-listed failure finishes first; the one raised is still the earlier-listed."""
    clients = ["a", "b", "c", "d"]
    _, handler = code_mode_gateway(clients, failing=frozenset({"b", "d"}))
    bf = client(handler)
    with pytest.raises(ServerError) as caught:
        await bf.tools()
    assert caught.value.details["body"] == "b down"
    await bf.aclose()
