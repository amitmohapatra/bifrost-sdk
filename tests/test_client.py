"""The client's behaviour, with the gateway mocked at the transport.

Every test below except the happy paths exists because a real gateway did this to a real
codebase — twice, in two separate implementations that made the same mistakes independently.
"""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest

from bifrost_sdk import Bifrost, EmptyResponse, GatewayError, InvalidJSON, RateLimited, Unreachable
from bifrost_sdk._headers import Options
from bifrost_sdk._retry import retry_after

BASE = "http://gateway.test/v1"


def client(handler, **kw) -> Bifrost:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url=BASE)
    return Bifrost(BASE, model="test/model", client=http, **kw)


def reply(content, *, finish="stop", usage=None):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": usage or {},
        },
    )


# ----------------------------------------------------------------- the three verbs


async def test_chat_returns_text() -> None:
    bf = client(lambda r: reply("pong"))
    assert await bf.chat("ping") == "pong"
    await bf.aclose()


async def test_a_bare_string_becomes_one_user_turn() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(jsonlib.loads(request.content))
        return reply("ok")

    bf = client(handler)
    await bf.chat("hello", system="be brief")
    await bf.aclose()
    assert seen["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]


async def test_json_parses_an_object() -> None:
    bf = client(lambda r: reply('{"worthy": true}'))
    assert await bf.json("is this worth it?") == {"worthy": True}
    await bf.aclose()


async def test_json_survives_a_fenced_code_block() -> None:
    """Providers wrap JSON in markdown even when asked not to."""
    bf = client(lambda r: reply('```json\n{"a": 1}\n```'))
    assert await bf.json("x") == {"a": 1}
    await bf.aclose()


async def test_json_survives_prose_around_the_object() -> None:
    bf = client(lambda r: reply('Sure! Here it is: {"a": 1} — hope that helps'))
    assert await bf.json("x") == {"a": 1}
    await bf.aclose()


async def test_json_that_is_not_json_is_an_error_not_a_guess() -> None:
    bf = client(lambda r: reply("I'd rather not."))
    with pytest.raises(InvalidJSON):
        await bf.json("x")
    await bf.aclose()


async def test_stream_yields_deltas_unbuffered() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    bf = client(lambda r: httpx.Response(200, text=body))
    assert [d async for d in bf.stream("hi")] == ["Hel", "lo"]
    await bf.aclose()


# ----------------------------------------------------------------- the hard-won behaviour


async def test_an_exhausted_output_budget_is_an_error_not_an_empty_string() -> None:
    """A reasoning model spends the budget thinking before it emits anything.

    Measured against gemini-3.6-flash: "Reply with exactly: OK" consumed 57 reasoning tokens,
    so a small max_tokens comes back 200 OK with "" and finish_reason=length. Returning that
    as a completion pushes a silently degraded answer into every call site.
    """
    bf = client(
        lambda r: reply(
            "",
            finish="length",
            usage={"completion_tokens": 16, "completion_tokens_details": {"reasoning_tokens": 16}},
        )
    )
    with pytest.raises(EmptyResponse, match="output budget"):
        await bf.chat("hi")
    await bf.aclose()


async def test_a_short_answer_that_simply_finished_is_fine() -> None:
    bf = client(lambda r: reply("OK", finish="stop"))
    assert await bf.chat("hi") == "OK"
    await bf.aclose()


async def test_rate_limit_is_its_own_error_not_a_gateway_failure() -> None:
    """429 means the gateway is healthy and asking for less.

    Callers trip circuit breakers on failure; if a rate limit looks like one, backpressure
    becomes an outage. Measured: 17 rate limits opened a breaker and the next 62 calls failed
    without a request ever being sent.
    """
    bf = client(lambda r: httpx.Response(429, text="slow down"), max_retries=0)
    with pytest.raises(RateLimited) as caught:
        await bf.chat("hi")
    assert not isinstance(caught.value, GatewayError)
    await bf.aclose()


async def test_retry_after_header_is_honoured_over_the_backoff() -> None:
    bf = client(lambda r: httpx.Response(429, headers={"Retry-After": "7"}), max_retries=0)
    with pytest.raises(RateLimited) as caught:
        await bf.chat("hi")
    assert caught.value.retry_after == 7.0
    await bf.aclose()


async def test_retry_delay_is_read_from_the_body_when_there_is_no_header() -> None:
    """Gemini answers "Please retry in 59.18s" in the body and sends no Retry-After header.

    A client that only reads the header sees nothing and falls back to a backoff measured in
    milliseconds against a window measured in a minute.
    """
    bf = client(
        lambda r: httpx.Response(429, text="Quota exceeded. Please retry in 59.18s."),
        max_retries=0,
    )
    with pytest.raises(RateLimited) as caught:
        await bf.chat("hi")
    assert caught.value.retry_after == pytest.approx(59.18)
    await bf.aclose()


async def test_a_bad_request_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="malformed")

    bf = client(handler, max_retries=3)
    with pytest.raises(GatewayError):
        await bf.chat("hi")
    assert calls["n"] == 1, "a 400 is the caller's mistake; retrying only spends the budget"
    await bf.aclose()


async def test_a_server_error_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else reply("recovered")

    bf = client(handler, max_retries=3, backoff_seconds=0.0)
    assert await bf.chat("hi") == "recovered"
    assert calls["n"] == 3
    await bf.aclose()


async def test_an_unreachable_gateway_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    bf = client(handler, max_retries=0)
    with pytest.raises(Unreachable):
        await bf.chat("hi")
    await bf.aclose()


# ----------------------------------------------------------------- ergonomics


async def test_a_model_is_required_somewhere() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: reply("x")), base_url=BASE)
    bf = Bifrost(BASE, client=http)
    with pytest.raises(ValueError, match="no model"):
        await bf.chat("hi")
    await bf.aclose()


async def test_the_per_call_model_overrides_the_default() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = jsonlib.loads(request.content)["model"]
        return reply("x")

    bf = client(handler)
    await bf.chat("hi", model="other/model")
    await bf.aclose()
    assert seen["model"] == "other/model"


async def test_it_works_as_an_async_context_manager() -> None:
    transport = httpx.MockTransport(lambda r: reply("ok"))
    async with Bifrost(
        BASE, model="m", client=httpx.AsyncClient(transport=transport, base_url=BASE)
    ) as bf:
        assert await bf.chat("hi") == "ok"


def test_a_base_url_is_required() -> None:
    with pytest.raises(ValueError, match="base_url"):
        Bifrost("")


# ----------------------------------------------------------------- the raw payload


async def test_complete_returns_the_gateway_payload_untouched() -> None:
    """What the three verbs discard is exactly what a framework on top needs."""
    payload = {
        "id": "cmpl-1",
        "choices": [
            {
                "message": {"content": "hi", "tool_calls": [{"id": "t1"}]},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    bf = client(lambda r: httpx.Response(200, json=payload))
    assert await bf.complete("ping") == payload
    await bf.aclose()


async def test_complete_shares_the_retry_behaviour_of_the_other_verbs() -> None:
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, text="unavailable")
        return reply("recovered")

    bf = client(handler, backoff_seconds=0.0)
    payload = await bf.complete("ping")
    assert payload["choices"][0]["message"]["content"] == "recovered"
    await bf.aclose()


async def test_complete_does_not_swallow_an_empty_answer_into_a_string() -> None:
    """``chat`` raises EmptyResponse on a budget-exhausted reply; ``complete`` hands the
    caller the evidence instead, because a framework reports on it rather than retrying."""
    bf = client(lambda r: reply("", finish="length", usage={"completion_tokens": 64}))
    payload = await bf.complete("ping")
    assert payload["choices"][0]["finish_reason"] == "length"
    await bf.aclose()


async def test_a_deadline_narrows_the_timeout_for_one_request_only() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return reply("ok")

    bf = client(handler)
    await bf.complete("ping", timeout=2.5)
    assert seen["timeout"] == {"connect": 2.5, "pool": 2.5, "read": 2.5, "write": 2.5}
    await bf.aclose()


async def test_no_deadline_leaves_the_client_default_in_place() -> None:
    """httpx reads an explicit ``timeout=None`` as 'wait forever'; omitting it must not."""
    seen: dict[str, object] = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return reply("ok")

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url=BASE, timeout=httpx.Timeout(9.0))
    bf = Bifrost(BASE, model="test/model", client=http)
    await bf.complete("ping")
    assert seen["timeout"] == {"connect": 9.0, "pool": 9.0, "read": 9.0, "write": 9.0}
    await bf.aclose()


async def test_tools_reach_the_gateway_through_complete() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen.update(jsonlib.loads(request.content))
        return reply("ok")

    bf = client(handler)
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    await bf.complete("ping", tools=tools)
    assert seen["tools"] == tools
    await bf.aclose()


# ----------------------------------------------------------------- Retry-After spellings
#
# These moved here from the memory service when its copy of the parser was deleted. They are
# the forms RFC 9110 allows that the seconds-only reading misses.


def test_retry_after_may_be_an_http_date() -> None:
    value = retry_after({"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert value is not None and value > 0


def test_a_retry_after_date_already_in_the_past_is_not_a_negative_wait() -> None:
    assert retry_after({"retry-after": "Wed, 21 Oct 1999 07:28:00 GMT"}) == 0.0


def test_no_advice_at_all_falls_back_to_the_backoff() -> None:
    assert retry_after(None, "") is None
    assert retry_after({}, "") is None
    assert retry_after({"retry-after": "soon"}, "") is None


async def test_complete_sends_only_the_keys_it_was_given() -> None:
    """The verbs are opinionated; the raw path is not.

    A framework calling ``complete`` builds its own request, and a ``max_tokens`` or
    ``temperature`` it never set appearing in the body changes what the provider does — and
    what it charges — for reasons the caller cannot see.
    """
    seen: dict[str, object] = {}

    def handler(request):
        seen.update(jsonlib.loads(request.content))
        return reply("ok")

    bf = client(handler)
    await bf.complete([{"role": "user", "content": "hi"}])
    assert set(seen) == {"model", "messages"}
    await bf.aclose()


async def test_the_verbs_still_bound_the_output_budget() -> None:
    """The counterpart: ``chat`` must keep sending max_tokens, or a reasoning model can spend
    an unbounded budget thinking."""
    seen: dict[str, object] = {}

    def handler(request):
        seen.update(jsonlib.loads(request.content))
        return reply("ok")

    bf = client(handler, max_tokens=123)
    await bf.chat("hi")
    assert seen["max_tokens"] == 123
    assert seen["temperature"] == 0.0
    await bf.aclose()


async def test_structured_output_is_requested_deterministically() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen.update(jsonlib.loads(request.content))
        return reply('{"ok": true}')

    bf = client(handler)
    await bf.json("hi", schema={"type": "object"})
    assert seen["temperature"] == 0.0
    assert seen["response_format"]["type"] == "json_schema"
    await bf.aclose()


# ----------------------------------------------------------------- MCP through the gateway


async def test_execute_tool_returns_a_turn_ready_to_append() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"role": "tool", "content": "42", "tool_call_id": "call_1"})

    bf = client(handler)
    call = {"id": "call_1", "type": "function", "function": {"name": "answer", "arguments": "{}"}}
    turn = await bf.execute_tool(call)
    assert turn == {"role": "tool", "content": "42", "tool_call_id": "call_1"}
    assert seen["path"].endswith("/mcp/tool/execute")
    assert seen["query"] == {"format": "chat"}
    assert seen["body"] == call
    await bf.aclose()


async def test_a_tool_call_with_no_name_is_refused_before_a_request_is_sent() -> None:
    """A malformed call is the caller's bug; sending it wastes a round trip and returns a
    gateway error that reads like the gateway's fault."""
    sent = []
    bf = client(lambda r: (sent.append(r), reply("x"))[1])
    with pytest.raises(ValueError, match="no function name"):
        await bf.execute_tool({"id": "call_1", "type": "function", "function": {}})
    assert sent == []
    await bf.aclose()


async def test_tool_execution_failures_keep_their_status() -> None:
    bf = client(lambda r: httpx.Response(403, text="tool not allowed"))
    with pytest.raises(GatewayError) as caught:
        await bf.execute_tool({"function": {"name": "rm", "arguments": "{}"}})
    assert caught.value.details["status"] == 403
    await bf.aclose()


async def test_an_unreachable_gateway_is_not_reported_as_a_tool_failure() -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    bf = client(handler)
    with pytest.raises(Unreachable):
        await bf.execute_tool({"function": {"name": "answer", "arguments": "{}"}})
    await bf.aclose()


# ----------------------------------------------------------------- the fluent chain


def _capture():
    seen: dict[str, object] = {}

    def handler(request):
        seen["headers"] = {k: v for k, v in request.headers.items() if k.startswith("x-bf-")}
        seen["body"] = jsonlib.loads(request.content) if request.content else {}
        return reply("ok")

    return seen, handler


async def test_a_chain_sends_every_option_as_its_gateway_header() -> None:
    """One expression, one request: prompt, MCP scope, session, customer and model."""
    seen, handler = _capture()
    bf = client(handler)
    await (
        bf.prompt("p-123", version=3)
        .mcp(clients=["memory"], tools=["memory.recall"])
        .session("thread-9")
        .customer(id="acme", name="Acme Industrial")
        .dimensions(team="payments")
        .using("gemini/gemini-3.6-flash")
        .chat("what changed?")
    )
    assert seen["headers"] == {
        "x-bf-prompt-id": "p-123",
        "x-bf-prompt-version": "3",
        "x-bf-mcp-include-clients": "memory",
        "x-bf-mcp-include-tools": "memory.recall",
        "x-bf-session-id": "thread-9",
        "x-bf-customer-id": "acme",
        "x-bf-customer-name": "Acme Industrial",
        "x-bf-dim-team": "payments",
    }
    assert seen["body"]["model"] == "gemini/gemini-3.6-flash"
    await bf.aclose()


async def test_each_step_returns_a_new_call_so_a_template_is_reusable() -> None:
    """A half-built chain is what a harness keeps per agent and finishes per turn. If a step
    mutated in place, the second turn would inherit the first turn's session."""
    seen, handler = _capture()
    bf = client(handler)
    base = bf.prompt("p-1").mcp(clients=["memory"])
    first = base.session("thread-a")
    second = base.session("thread-b")

    assert base is not first and first is not second
    await first.chat("one")
    assert seen["headers"]["x-bf-session-id"] == "thread-a"
    await second.chat("two")
    assert seen["headers"]["x-bf-session-id"] == "thread-b"
    # the template itself never acquired a session
    await base.chat("three")
    assert "x-bf-session-id" not in seen["headers"]
    await bf.aclose()


async def test_a_plain_call_sends_no_gateway_headers_at_all() -> None:
    """Unset options must send nothing: an empty header is not the same as absent, and the
    gateway treats some of these as present-means-on."""
    seen, handler = _capture()
    bf = client(handler)
    await bf.chat("hi")
    assert seen["headers"] == {}
    await bf.aclose()


async def test_private_opts_out_of_content_logging() -> None:
    seen, handler = _capture()
    bf = client(handler)
    await bf.call().private().chat("my card number is ...")
    assert seen["headers"] == {"x-bf-disable-content-logging": "true"}
    await bf.aclose()


async def test_a_prompt_version_never_travels_without_its_prompt() -> None:
    """Version alone selects nothing and looks like the prompt was ignored."""
    assert Options(prompt_version=4).headers() == {}
    assert Options(prompt_id="p", prompt_version=4).headers() == {
        "x-bf-prompt-id": "p",
        "x-bf-prompt-version": "4",
    }


async def test_the_chain_reaches_every_terminal_verb() -> None:
    seen, handler = _capture()

    def json_handler(request):
        handler(request)
        return reply('{"ok": true}')

    bf = client(json_handler)
    chained = bf.call().session("s-1")
    assert await chained.chat("a") == '{"ok": true}'
    assert seen["headers"]["x-bf-session-id"] == "s-1"
    assert await chained.json("b") == {"ok": True}
    assert seen["headers"]["x-bf-session-id"] == "s-1"
    payload = await chained.complete("c")
    assert payload["choices"][0]["message"]["content"] == '{"ok": true}'
    assert seen["headers"]["x-bf-session-id"] == "s-1"
    await bf.aclose()


async def test_streaming_carries_the_chain_too() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["headers"] = {k: v for k, v in request.headers.items() if k.startswith("x-bf-")}
        body = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body)

    bf = client(handler)
    deltas = [d async for d in bf.call().session("s-2").mcp(clients=["memory"]).stream("go")]
    assert deltas == ["hi"]
    assert seen["headers"]["x-bf-session-id"] == "s-2"
    assert seen["headers"]["x-bf-mcp-include-clients"] == "memory"
    await bf.aclose()


async def test_arbitrary_headers_reach_the_gateway_for_routing_rules() -> None:
    """Routing rules are CEL over a caller-supplied ``headers`` map, so anything set here is
    a routing input — not just the typed x-bf-* options."""
    seen, handler = _capture()
    bf = client(handler)
    await bf.call().header(**{"x-tier": "batch"}).dimensions(team="payments").chat("go")
    assert seen["headers"]["x-bf-dim-team"] == "payments"
    await bf.aclose()
    assert True


async def test_a_routing_header_is_sent_verbatim() -> None:
    sent: dict[str, object] = {}

    def handler(request):
        sent["x-tier"] = request.headers.get("x-tier")
        return reply("ok")

    bf = client(handler)
    await bf.call().header(**{"x-tier": "batch"}).chat("go")
    assert sent["x-tier"] == "batch"
    await bf.aclose()


def test_the_delay_is_read_from_a_real_gemini_quota_body() -> None:
    """Captured from the live gateway, not written from the documentation.

    Gemini answers 429 with the delay in the *body* and no ``Retry-After`` header at all.
    A client that reads only the header sees nothing, falls back to an exponential backoff
    measured in milliseconds, and spends its whole retry budget inside a window measured in
    seconds — which is how a rate limit becomes a failed request instead of a slow one.
    """
    captured = (
        "You exceeded your current quota, please check your plan and billing details. "
        "For more information on this error, head to: "
        "https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, "
        "head to: https://ai.dev/rate-limit. \n"
        "* Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
        "limit: 20, model: gemini-3.6-flash\n"
        "Please retry in 8.586631853s."
    )

    assert retry_after(None, captured) == pytest.approx(8.586631853)
    # An explicit header still wins: it is the protocol's answer, the body is a fallback.
    assert retry_after({"retry-after": "30"}, captured) == 30.0
    # And a body with no advice must not invent a delay.
    assert retry_after(None, "You exceeded your current quota.") is None


async def test_a_long_rate_limit_body_still_yields_its_delay() -> None:
    """The delay must survive the excerpt the exception carries.

    Captured verbatim from the gateway: 751 characters, with the advice at index 483. The
    error body used to be truncated to 500 characters *before* it was parsed, which cut
    "Please retry in 28.9s." at "Please retry in 8" — no trailing "s", so no match, so no
    delay. The client then waited half a second against a window of half a minute and gave
    up, which looks from the outside like a gateway that will not serve you rather than one
    asking you to come back shortly.
    """
    body = (
        '{"is_bifrost_error":false,"status_code":429,"error":{"type":"RESOURCE_EXHAUSTED",'
        '"code":"429","message":"You exceeded your current quota, please check your plan and '
        "billing details. For more information on this error, head to: "
        "https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, "
        "head to: https://ai.dev/rate-limit. \\n* Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
        'model: gemini-3.6-flash\\nPlease retry in 28.973095374s."}}'
    )
    assert len(body) > 500, "the point of this test is a body longer than the excerpt"
    assert body.index("Please retry in") > 400, "and advice that lands past the cut"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=body)

    client = Bifrost(
        "http://gateway/v1",
        model="gemini/gemini-3.6-flash",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://gateway/v1"
        ),
        max_retries=0,
    )
    with pytest.raises(RateLimited) as raised:
        await client.chat("hello")

    assert raised.value.retry_after == pytest.approx(28.973095374)
    # The exception still carries only an excerpt: parsing everything is not a licence to
    # attach an unbounded body to an error that ends up in logs.
    assert len(raised.value.details["body"]) <= 500
    await client.aclose()
