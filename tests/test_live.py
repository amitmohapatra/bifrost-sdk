"""Against a running gateway. Opt-in: set ``BIFROST_LIVE_URL`` (e.g. ``http://localhost:8091/v1``).

Registers temporary MCP clients (``BIFROST_LIVE_MCP_URL``, default the public DeepWiki
server — the gateway refuses private-network targets to unauthenticated callers; and
``BIFROST_LIVE_ANNOTATED_MCP_URL``, default the public Context7 server, which publishes MCP
tool annotations) and removes them afterwards. Skipped when the variable is unset or the
gateway is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from bifrost_sdk import Bifrost, GatewayError, MCPClient, MCPClientConfig, MCPConnection, Options

pytestmark = pytest.mark.live

LIVE_URL = os.environ.get("BIFROST_LIVE_URL")
MCP_URL = os.environ.get("BIFROST_LIVE_MCP_URL", "https://mcp.deepwiki.com/mcp")
ANNOTATED_MCP_URL = os.environ.get("BIFROST_LIVE_ANNOTATED_MCP_URL", "https://mcp.context7.com/mcp")
#: A tool the default server exposes, and one it exposes that the client will not allow.
TOOL, OTHER = "read_wiki_structure", "ask_wiki_question"
#: How long discovery and the asynchronous log writer get before a test gives up.
SETTLE_SECONDS = 20.0
POLL_SECONDS = 0.5


async def _eventually(probe):
    """Poll the gateway (not an in-process event) until ``probe`` returns something."""
    for _ in range(int(SETTLE_SECONDS / POLL_SECONDS)):
        if result := await probe():
            return result
        await asyncio.sleep(POLL_SECONDS)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def bf() -> AsyncIterator[Bifrost]:
    if not LIVE_URL:
        pytest.skip("BIFROST_LIVE_URL not set")
    client = Bifrost(LIVE_URL)
    if not await client.ping():
        await client.aclose()
        pytest.skip(f"no gateway at {LIVE_URL}")
    yield client
    await client.aclose()


@asynccontextmanager
async def _registered(bf: Bifrost, url: str, allowed: tuple[str, ...]) -> AsyncIterator[MCPClient]:
    """A temporary client for ``url``, once the gateway has discovered its tools."""
    config = MCPClientConfig(
        name=f"bfsdklive{uuid.uuid4().hex[:8]}",
        connection=MCPConnection(type="http", url=url),
        tools_to_execute=allowed,
    )
    await bf.mcp.add(config)
    try:

        async def discovered() -> MCPClient | None:
            found = [c for c in await bf.mcp.clients() if c.config.name == config.name]
            return found[0] if found and found[0].tools else None

        yield await _eventually(discovered)
    finally:
        for client in await bf.mcp.clients():
            if client.config.name == config.name:
                await bf.mcp.remove(client.id)


@pytest.fixture
async def probe(bf: Bifrost) -> AsyncIterator[MCPClient]:
    async with _registered(bf, MCP_URL, (TOOL,)) as client:
        yield client


def _call(name: str, arguments: dict[str, str]) -> dict:
    return {
        "id": "live-1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def test_the_registered_client_reads_back_typed(bf: Bifrost, probe: MCPClient) -> None:
    assert probe.config.connection == MCPConnection(type="http", url=MCP_URL)
    assert probe.config.tools_to_execute == (TOOL,)
    assert f"{probe.config.name}-{OTHER}" in {t.name for t in probe.tools}
    assert [t.name for t in probe.executable] == [f"{probe.config.name}-{TOOL}"]


async def test_tools_are_scoped_like_the_include_headers(bf: Bifrost, probe: MCPClient) -> None:
    name = probe.config.name
    assert f"{name}-{TOOL}" in {t.name for t in await bf.tools()}
    assert [t.name for t in await bf.tools(clients=[name])] == [f"{name}-{TOOL}"]
    assert [t.name for t in await bf.tools(only=[f"{name}-*"])] == [f"{name}-{TOOL}"]
    assert await bf.tools(clients=[]) == []


async def test_execution_honours_scope_and_allow_list(bf: Bifrost, probe: MCPClient) -> None:
    name = probe.config.name
    args = {"repoName": "maximhq/bifrost"}
    turn = await bf.execute_tool(
        _call(f"{name}-{TOOL}", args), options=Options(mcp_tools=[f"{name}-*"])
    )
    assert turn["role"] == "tool" and turn["content"]
    for refused, options in (
        (f"{name}-{TOOL}", Options(mcp_clients=[])),  # present-and-empty is deny-all
        (f"{name}-{TOOL}", Options(mcp_tools=[TOOL])),  # bare names never match
        (f"{name}-{OTHER}", None),  # outside the client's tools_to_execute
    ):
        with pytest.raises(GatewayError) as caught:
            await bf.execute_tool(_call(refused, args), options=options)
        assert caught.value.details["status"] == 400


async def test_code_mode_nested_calls_are_found_by_parent(bf: Bifrost, probe: MCPClient) -> None:
    name = probe.config.name
    await bf.mcp.update(probe, probe.config.model_copy(update={"is_code_mode_client": True}))
    [updated] = [c for c in await bf.mcp.clients() if c.id == probe.id]
    assert updated.config.is_code_mode_client
    assert all(t.code_mode for t in updated.tools)

    parent = f"live-{uuid.uuid4().hex}"
    since = datetime.now(UTC) - timedelta(seconds=5)
    code = f'r = {name}.{TOOL}(repoName="maximhq/bifrost")\nresult = 1'
    turn = await bf.execute_tool(
        _call("executeToolCode", {"code": code}), options=Options(parent_request_id=parent)
    )
    assert turn["role"] == "tool"

    async def logged():
        return await bf.mcp_logs(since, parent_request_id=parent)

    [log] = await _eventually(logged)
    assert (log.client, log.tool, log.status) == (name, TOOL, "success")
    assert log.parent_request_id == parent
    assert log.arguments == {"repoName": "maximhq/bifrost"}


async def test_a_connection_change_is_refused_locally(bf: Bifrost, probe: MCPClient) -> None:
    moved = probe.config.model_copy(
        update={"connection": MCPConnection(type="http", url="https://example.com/mcp")}
    )
    with pytest.raises(ValueError, match="remove and add"):
        await bf.mcp.update(probe, moved)


async def test_annotations_survive_the_gateway(bf: Bifrost) -> None:
    """The server's MCP annotations reach ``ToolDef``; a server without them gives ``None``."""
    async with _registered(bf, ANNOTATED_MCP_URL, ("*",)) as annotated:
        assert annotated.tools
        for tool in annotated.tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
        [listed] = await bf.tools(clients=[annotated.config.name], only=[annotated.tools[0].name])
        assert listed.annotations == annotated.tools[0].annotations


async def test_a_server_without_annotations_lists_none(probe: MCPClient) -> None:
    assert all(t.annotations is None for t in probe.tools)
