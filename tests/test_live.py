"""Against a running gateway. Opt-in: ``pytest -m live`` with ``BIFROST_URL`` set (e.g.
``http://localhost:8091/v1``) — the name every other repository in the platform uses for the
gateway. ``BIFROST_LIVE_URL`` is still read when ``BIFROST_URL`` is unset, as an alias.

Registers temporary MCP clients (``BIFROST_LIVE_MCP_URL``, default the public DeepWiki
server — the gateway refuses private-network targets to unauthenticated callers; and
``BIFROST_LIVE_ANNOTATED_MCP_URL``, default the public Context7 server, which publishes MCP
tool annotations) and removes them afterwards. Skipped when neither variable is set or the
gateway is unreachable. Deselected by a plain ``pytest`` (``addopts`` in ``pyproject.toml``):
``BIFROST_URL`` is set in shells that never meant to register clients on that gateway.

The tests of the prompt and skills repositories, Virtual MCPs, per-user headers and the
no-gateway-tools guarantee create (and delete) their own prompts, skills, Virtual MCPs and
virtual keys. Those that complete need ``BIFROST_LIVE_MODEL`` (``provider/model``); those that
run tools need the local MCP server of ``local_mcp.py`` declared in the gateway's
``config.json`` (they start the server; they skip when the gateway does not know it).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import local_mcp
from bifrost_sdk import (
    Bifrost,
    GatewayError,
    MCPClient,
    MCPClientConfig,
    MCPConnection,
    Options,
    PermissionDeniedError,
)
from bifrost_sdk.admin import Admin

pytestmark = pytest.mark.live

LIVE_URL = os.environ.get("BIFROST_URL") or os.environ.get("BIFROST_LIVE_URL")
#: ``provider/model`` the completion tests use; they skip without it.
LIVE_MODEL = os.environ.get("BIFROST_LIVE_MODEL")
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
        pytest.skip("BIFROST_URL (or BIFROST_LIVE_URL) not set")
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


async def test_a_virtual_key_lists_only_the_tools_it_allows(bf: Bifrost) -> None:
    """``tools()`` under a virtual key is that key's MCP allow-list, as the gateway reads it."""
    assert LIVE_URL is not None
    async with (
        _registered(bf, MCP_URL, ("*",)) as registered,
        Admin(LIVE_URL) as admin,
    ):
        name = registered.config.name
        created = await admin.vk.create(
            f"{name}-key",
            mcp_configs=[{"mcp_client_name": name, "tools_to_execute": [TOOL]}],
        )
        key = created["virtual_key"]
        try:
            async with Bifrost(LIVE_URL, api_key=key["value"]) as scoped:
                assert [t.name for t in await scoped.tools()] == [f"{name}-{TOOL}"]
        finally:
            await admin.vk.delete(key["id"])


# ----------------------------------------------------------------- local stack: repositories,
# Virtual MCPs, per-user headers, and completions that never get the gateway's MCP tools


def _model() -> str:
    if not LIVE_MODEL:
        pytest.skip("BIFROST_LIVE_MODEL (provider/model) not set")
    return LIVE_MODEL


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def admin(bf: Bifrost) -> AsyncIterator[Admin]:
    assert LIVE_URL is not None
    async with Admin(LIVE_URL) as client:
        yield client


@asynccontextmanager
async def _key(admin: Admin, mcp_configs: list[dict]) -> AsyncIterator[dict]:
    """A temporary virtual key that may use every provider, with these MCP grants."""
    created = await admin.vk.create(
        _unique("bfsdklive"), provider_configs=[], mcp_configs=mcp_configs,
        allow_all_providers=True,
    )  # fmt: skip
    key = created["virtual_key"]
    try:
        yield key
    finally:
        await admin.vk.delete(key["id"])


@pytest.fixture
async def local(bf: Bifrost) -> AsyncIterator[MCPClient]:
    """The gateway's client for ``local_mcp``, with the server running and its tools known."""
    async with local_mcp.serving():
        found = [c for c in await bf.mcp.clients() if c.config.name == local_mcp.CLIENT]
        if not found:
            pytest.skip(f"the gateway's config.json declares no {local_mcp.CLIENT!r} MCP client")
        if not found[0].tools:  # the server was down when the gateway listed it
            assert LIVE_URL is not None
            async with httpx.AsyncClient(base_url=LIVE_URL.removesuffix("/v1")) as http:
                await http.post(f"/api/mcp/client/{found[0].id}/refresh-tools")
        yield found[0]


def _granted(client: MCPClient) -> list[dict]:
    return [{"mcp_client_name": client.config.name, "tools_to_execute": ["*"]}]


async def test_a_prompt_is_committed_resolved_and_applied(admin: Admin) -> None:
    """The stored template reaches the model: the prompt is exactly the template followed by
    the request's own turn (the same token count as sending both), and the reply follows it
    — on ``complete`` and on ``stream``."""
    model = _model()
    name = _unique("bfsdklive-pirate")
    prompt = await admin.prompts.create(name)
    try:
        system = {"role": "system", "content": "Answer every question with the single word ARRR."}
        version = await admin.prompts.commit(prompt.id, [system], model=model, message="v1")
        assert (version.number, version.messages, version.model) == (1, (system,), model)
        found = await admin.prompts.find(name)
        assert found is not None and found.id == prompt.id
        assert found.latest_version == version
        assert [v.number for v in await admin.prompts.versions(prompt.id)] == [1]
        assert await admin.prompts.version(version.id) == version

        assert LIVE_URL is not None
        async with _key(admin, []) as key, Bifrost(LIVE_URL, api_key=key["value"]) as bf:
            question = "What is 2 + 2?"
            turns = [system, {"role": "user", "content": question}]
            sent = await bf.complete(turns, model=model, max_tokens=20)
            selected = Options(prompt_id=prompt.id, prompt_version=version.number)
            reply = await bf.complete(question, model=model, max_tokens=20, options=selected)
            assert reply["usage"]["prompt_tokens"] == sent["usage"]["prompt_tokens"]
            assert "ARRR" in reply["choices"][0]["message"]["content"].upper()
            latest = Options(prompt_id=prompt.id)  # no version: the latest committed one
            streamed = "".join([d async for d in bf.stream(question, model=model, options=latest)])
            assert "ARRR" in streamed.upper()
    finally:
        await admin.prompts.delete(prompt.id)
    assert await admin.prompts.find(name) is None


async def test_a_skill_is_published_read_back_and_shifted(admin: Admin) -> None:
    """Body and files per version; the bytes of a file come from the served version."""
    name = _unique("bfsdklive-sql")
    rules = "references/rules.md"
    skill = await admin.skills.create(
        name, description="Reviews SQL.", body="# SQL review\nRead references/rules.md.",
        version="1.0.0", files={rules: "1. No SELECT *"},
    )  # fmt: skip
    try:
        assert (skill.version, [f.path for f in skill.files]) == ("1.0.0", [rules])
        found = await admin.skills.find(name)
        assert found is not None and found.body.startswith("# SQL review")
        [file] = found.files
        assert (file.source_type, file.mime_type, file.size) == ("text", "text/markdown", 14)
        assert await admin.skills.read_file(name, rules) == b"1. No SELECT *"

        staged = await admin.skills.publish(
            skill.id, description="Reviews SQL, v2.", body="# SQL review v2",
            version="1.1.0", files={rules: "1. No SELECT * (v2)"}, serve=False,
        )  # fmt: skip
        assert (staged.version, staged.highest_version) == ("1.0.0", "1.1.0")
        pinned = await admin.skills.get(skill.id, version="1.1.0")
        assert (pinned.version, pinned.body, pinned.description) == (
            "1.1.0",
            "# SQL review v2",
            "Reviews SQL, v2.",
        )
        assert [v.version for v in await admin.skills.versions(skill.id)] == ["1.1.0", "1.0.0"]
        assert await admin.skills.read_file(name, rules) == b"1. No SELECT *"
        assert (await admin.skills.shift_version(skill.id, "1.1.0")).version == "1.1.0"
        assert await admin.skills.read_file(name, rules) == b"1. No SELECT * (v2)"
    finally:
        await admin.skills.delete(skill.id)
    assert await admin.skills.find(name) is None


def _whoami(call_id: str = "live-who") -> dict:
    return _call(f"{local_mcp.CLIENT}-whoami", {}) | {"id": call_id}


async def test_a_callers_header_reaches_the_mcp_server_as_theirs(
    admin: Admin, local: MCPClient
) -> None:
    """Per-user credentials: the gateway forwards the header the client allows, per call —
    through ``/v1/mcp/tool/execute`` and through the client's own ``/mcp/<slug>``."""
    assert local.config.allowed_extra_headers == (local_mcp.USER_HEADER,)
    assert LIVE_URL is not None
    async with _key(admin, _granted(local)) as key, Bifrost(LIVE_URL, api_key=key["value"]) as bf:
        for user in ("alice", "bob"):
            options = Options(extra={local_mcp.USER_HEADER: user})
            for slug in (None, local_mcp.CLIENT):
                turn = await bf.execute_tool(_whoami(), options=options, slug=slug)
                assert (turn["content"], turn.get("is_error")) == (user, None)
        anonymous = await bf.execute_tool(_whoami())
        assert anonymous["is_error"] is True


async def test_a_virtual_mcp_scopes_what_a_key_lists_and_runs(
    admin: Admin, local: MCPClient
) -> None:
    """A bundle is reachable only once attached, and through its slug only its tools run."""
    echo = f"{local_mcp.CLIENT}-echo"
    vmcp = await admin.virtual_mcps.create(_unique("bfsdklive vmcp"), {local.id: ["echo"]})
    assert LIVE_URL is not None
    try:
        assert vmcp.tools == {local.id: ("echo",)}
        assert vmcp.enabled and vmcp.virtual_key_ids == ()
        async with _key(admin, []) as key, Bifrost(LIVE_URL, api_key=key["value"]) as bf:
            with pytest.raises(PermissionDeniedError):
                await bf.tools(slug=vmcp.slug)
            await admin.virtual_mcps.attach(vmcp.id, key["id"])
            assert (await admin.virtual_mcps.get(vmcp.id)).virtual_key_ids == (key["id"],)
            assert [t.name for t in await bf.tools(slug=vmcp.slug)] == [echo]
            assert [t.name for t in await bf.tools()] == [echo], "the key's whole union"
            said = await bf.execute_tool(_call(echo, {"text": "hi"}), slug=vmcp.slug)
            assert said["content"] == "hi"
            outside = Options(extra={local_mcp.USER_HEADER: "alice"})
            refused = await bf.execute_tool(_whoami(), options=outside, slug=vmcp.slug)
            assert refused["is_error"] is True

            renamed = await admin.virtual_mcps.update(vmcp.id, name=_unique("bfsdklive renamed"))
            assert renamed.slug == vmcp.slug, "a slug is permanent"
            listed = await admin.virtual_mcps.list(search="bfsdklive renamed")
            assert [v.id for v in listed] == [vmcp.id]
            await admin.virtual_mcps.detach(vmcp.id, key["id"])
            with pytest.raises(PermissionDeniedError):
                await bf.tools(slug=vmcp.slug)
    finally:
        await admin.virtual_mcps.delete(vmcp.id)


async def _echo_runs(bf: Bifrost, since: datetime) -> list[str]:
    """The statuses of the local server's ``echo`` executions logged since ``since``, once
    the asynchronous log writer has had time to record one."""
    runs: list[str] = []
    for _ in range(int(SETTLE_SECONDS / POLL_SECONDS / 4)):
        logs = await bf.mcp_logs(since, limit=100)
        runs = [log.status for log in logs if log.name == f"{local_mcp.CLIENT}-echo"]
        if runs:
            break
        await asyncio.sleep(POLL_SECONDS)
    return runs


async def test_a_completion_is_offered_no_gateway_tool_and_has_none_run(
    admin: Admin, local: MCPClient, bf: Bifrost
) -> None:
    """The guard, against the gateway that would otherwise do both: a key granted a server
    gets its tools added to a bare request (the prompt grows), and the gateway's agent loop
    runs a tool the client lists in ``tools_to_auto_execute`` when the model calls it."""
    model = _model()
    echo = f"{local_mcp.CLIENT}-echo"
    assert LIVE_URL is not None
    async with (
        _key(admin, _granted(local)) as granted,
        _key(admin, []) as bare,
        Bifrost(LIVE_URL, api_key=granted["value"], max_retries=0) as sdk,
        Bifrost(LIVE_URL, api_key=bare["value"]) as nothing_to_add,
        httpx.AsyncClient(base_url=LIVE_URL, timeout=120) as raw,
    ):
        body = {"model": model, "messages": [{"role": "user", "content": "say hi"}]}
        unguarded = await raw.post(
            "/chat/completions",
            json=body | {"max_tokens": 5},
            headers={"Authorization": f"Bearer {granted['value']}"},
        )
        guarded = await sdk.complete("say hi", model=model, max_tokens=5)
        baseline = await nothing_to_add.complete("say hi", model=model, max_tokens=5)
        tokens = guarded["usage"]["prompt_tokens"]
        assert tokens == baseline["usage"]["prompt_tokens"], "no tool was added"
        assert unguarded.json()["usage"]["prompt_tokens"] > tokens, "a tool would have been"

        declared = [
            {
                "type": "function",
                "function": {
                    "name": echo,
                    "description": "Return text unchanged.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
        ]
        ask = "Use the tool to echo the word banana."
        auto = local.config.model_copy(update={"tools_to_auto_execute": ("echo",)})
        await bf.mcp.update(local, auto)
        try:
            since = datetime.now(UTC) - timedelta(seconds=1)
            # The gateway still feeds its refusal back to the model, whose answer may fail.
            with contextlib.suppress(GatewayError):
                await sdk.complete(
                    ask, model=model, max_tokens=60, tools=declared, tool_choice="required"
                )
            assert "success" not in await _echo_runs(bf, since), "the gateway ran the tool"

            since = datetime.now(UTC) - timedelta(seconds=1)
            await raw.post(
                "/chat/completions",
                json=body | {"messages": [{"role": "user", "content": ask}], "max_tokens": 60,
                             "tools": declared, "tool_choice": "required"},
                headers={"Authorization": f"Bearer {granted['value']}"},
            )  # fmt: skip
            assert "success" in await _echo_runs(bf, since), "without the guard, it would"
        finally:
            await bf.mcp.update(local, local.config)
