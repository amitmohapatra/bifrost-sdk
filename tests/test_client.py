"""The client's behaviour, with the gateway mocked at the transport.

Every test below except the happy paths exists because a real gateway did this to a real
codebase — twice, in two separate implementations that made the same mistakes independently.
"""

from __future__ import annotations

import json as jsonlib
import random

import httpx
import pytest
import respx

import bifrost_sdk
from bifrost_sdk import (
    AuthenticationError,
    BadRequestError,
    Bifrost,
    BifrostError,
    CircuitOpen,
    ConflictError,
    EmptyResponse,
    GatewayError,
    InvalidJSON,
    NotFoundError,
    PermissionDeniedError,
    RateLimited,
    RateLimitedError,
    ServerError,
    UnprocessableError,
    Unreachable,
)
from bifrost_sdk import _breaker as breaker_module
from bifrost_sdk._retry import MAX_WAIT, backoff, retry_after
from bifrost_sdk.headers import Options

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


# ----------------------------------------------------------------- per-request options


def _capture():
    seen: dict[str, object] = {}

    def handler(request):
        seen["headers"] = {k: v for k, v in request.headers.items() if k.startswith("x-bf-")}
        seen["all_headers"] = dict(request.headers)
        seen["body"] = jsonlib.loads(request.content) if request.content else {}
        return reply("ok")

    return seen, handler


async def test_options_send_every_field_as_its_gateway_header() -> None:
    seen, handler = _capture()
    bf = client(handler)
    options = Options(
        prompt_id="p-123",
        prompt_version=3,
        mcp_clients=["memory"],
        mcp_tools=["memory-recall"],
        parent_request_id="run-1",
        session_id="thread-9",
        customer_id="acme",
        customer_name="Acme Industrial",
        dimensions={"team": "payments"},
    )
    await bf.chat("what changed?", model="gemini/gemini-3.6-flash", options=options)
    assert seen["headers"] == {
        "x-bf-prompt-id": "p-123",
        "x-bf-prompt-version": "3",
        "x-bf-mcp-include-clients": "memory",
        "x-bf-mcp-include-tools": "memory-recall",
        "x-bf-parent-request-id": "run-1",
        "x-bf-session-id": "thread-9",
        "x-bf-customer-id": "acme",
        "x-bf-customer-name": "Acme Industrial",
        "x-bf-dim-team": "payments",
    }
    assert seen["body"]["model"] == "gemini/gemini-3.6-flash"
    await bf.aclose()


async def test_no_options_sends_no_gateway_headers_at_all() -> None:
    """An empty header is not the same as an absent one: for MCP scope it means deny-all."""
    seen, handler = _capture()
    bf = client(handler)
    await bf.chat("hi")
    assert seen["headers"] == {}
    await bf.aclose()


def test_an_empty_mcp_scope_is_sent_as_deny_all_and_none_as_absent() -> None:
    """Verified against the gateway: absent = unscoped, present-and-empty = nothing."""
    assert Options().headers() == {}
    assert Options(mcp_clients=[], mcp_tools=()).headers() == {
        "x-bf-mcp-include-clients": "",
        "x-bf-mcp-include-tools": "",
    }
    assert Options(mcp_tools=["erp-*", "crm-find"]).headers() == {
        "x-bf-mcp-include-tools": "erp-*,crm-find"
    }


def test_options_are_immutable_and_merge_into_a_copy() -> None:
    clients = ["memory"]
    base = Options(mcp_clients=clients)
    clients.append("erp")
    assert base.mcp_clients == ("memory",), "a caller's list cannot change a built Options"
    merged = base.merged(session_id="s-1")
    assert merged is not base
    assert base.session_id is None
    assert merged.headers()["x-bf-session-id"] == "s-1"


async def test_private_opts_out_of_content_logging() -> None:
    seen, handler = _capture()
    bf = client(handler)
    await bf.chat("my card number is ...", options=Options(content_logging=False))
    assert seen["headers"] == {"x-bf-disable-content-logging": "true"}
    await bf.aclose()


def test_a_prompt_version_never_travels_without_its_prompt() -> None:
    """Version alone selects nothing and looks like the prompt was ignored."""
    assert Options(prompt_version=4).headers() == {}
    assert Options(prompt_id="p", prompt_version=4).headers() == {
        "x-bf-prompt-id": "p",
        "x-bf-prompt-version": "4",
    }


async def test_options_reach_every_verb() -> None:
    seen, handler = _capture()

    def json_handler(request):
        handler(request)
        return reply('{"ok": true}')

    bf = client(json_handler)
    options = Options(session_id="s-1")
    assert await bf.chat("a", options=options) == '{"ok": true}'
    assert seen["headers"]["x-bf-session-id"] == "s-1"
    assert await bf.json("b", options=options) == {"ok": True}
    assert seen["headers"]["x-bf-session-id"] == "s-1"
    await bf.complete("c", options=options)
    assert seen["headers"]["x-bf-session-id"] == "s-1"
    assert "options" not in seen["body"], "options are headers, never body keys"
    await bf.aclose()


async def test_streaming_carries_options_too() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["headers"] = {k: v for k, v in request.headers.items() if k.startswith("x-bf-")}
        body = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body)

    bf = client(handler)
    options = Options(session_id="s-2", mcp_clients=["memory"])
    deltas = [d async for d in bf.stream("go", options=options)]
    assert deltas == ["hi"]
    assert seen["headers"]["x-bf-session-id"] == "s-2"
    assert seen["headers"]["x-bf-mcp-include-clients"] == "memory"
    await bf.aclose()


async def test_a_routing_header_is_sent_verbatim() -> None:
    """Routing rules are CEL over a caller-supplied ``headers`` map."""
    seen, handler = _capture()
    bf = client(handler)
    await bf.chat("go", options=Options(extra={"x-tier": "batch"}))
    assert seen["all_headers"]["x-tier"] == "batch"
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


# ------------------------------------------------- ping means usable, not merely answering


@pytest.mark.parametrize(
    ("status", "usable", "why"),
    [
        (200, True, "the gateway listed its models"),
        (404, False, "something is there, but /models is not it"),
        (401, False, "reachable and refusing us — every call will refuse too"),
        (500, False, "the gateway itself is unwell"),
        (503, False, "the gateway is unwell"),
    ],
)
async def test_ping_reports_a_gateway_we_can_actually_use(
    status: int, usable: bool, why: str
) -> None:
    """It used to accept anything below 500, which makes the check unable to fail in the
    one case it exists for: a base_url pointing at something that is not this gateway.

    Not hypothetical — the default is localhost:8090/v1, and on the machine this was
    written on a different service held 8090 and answered 404. A deployment pointed there
    reported its model dependency healthy and failed on every actual call. In
    agent-memory-service that value feeds /health/ready and the dependency_up gauge.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(status)

    client = Bifrost(
        "http://gateway/v1",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://gateway/v1"
        ),
    )
    assert await client.ping() is usable, why
    await client.aclose()


async def test_an_unreachable_gateway_pings_false_rather_than_raising() -> None:
    """Readiness calls this in a loop; it must never be the thing that breaks the probe."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening")

    client = Bifrost(
        "http://gateway/v1",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://gateway/v1"
        ),
    )
    assert await client.ping() is False
    await client.aclose()


# --------------------------------------------------------------------- breaker
#
# The breaker used to live in the memory service's LLM adapter and in the harness's model
# client, as the same thirty lines twice. These are the behaviours both of them had to get
# right, asserted once.


def _boom(status: int = 500):
    """A handler that always fails, counting how many requests actually reached it."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"error": "boom"})

    return handler, calls


async def test_the_breaker_opens_and_stops_sending() -> None:
    handler, calls = _boom()
    bf = client(handler, max_retries=0, circuit_failure_threshold=2, circuit_open_seconds=60.0)
    for _ in range(2):
        with pytest.raises(GatewayError):
            await bf.chat("hi")
    assert calls["n"] == 2
    with pytest.raises(CircuitOpen) as caught:
        await bf.chat("hi")
    assert calls["n"] == 2, "an open circuit must not reach the transport"
    assert 0 < caught.value.retry_after <= 60.0
    assert caught.value.details["retry_after_seconds"] == caught.value.retry_after


async def test_rate_limits_do_not_open_the_breaker() -> None:
    """17 rate limits once opened the circuit; the next 62 calls sent nothing at all."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="Please retry in 0.01s")

    bf = client(handler, max_retries=0, circuit_failure_threshold=2, circuit_open_seconds=60.0)
    for _ in range(5):
        with pytest.raises(RateLimited):
            await bf.chat("hi")
    assert calls["n"] == 5, "backpressure is not an outage"
    assert bf._breaker.consecutive_failures == 0


async def test_one_success_closes_the_breaker_again() -> None:
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    bf = client(handler, max_retries=0, circuit_failure_threshold=3)
    with pytest.raises(GatewayError):
        await bf.chat("hi")
    assert bf._breaker.consecutive_failures == 1
    state["fail"] = False
    assert await bf.chat("hi") == "ok"
    assert bf._breaker.consecutive_failures == 0


async def test_a_spent_retry_budget_counts_once_not_once_per_attempt() -> None:
    """Otherwise a threshold of 5 with 2 retries opens after two calls, not five."""
    handler, calls = _boom(503)
    bf = client(handler, max_retries=2, backoff_seconds=0.0, circuit_failure_threshold=5)
    for _ in range(2):
        with pytest.raises(GatewayError):
            await bf.chat("hi")
    assert calls["n"] == 6, "two calls, three attempts each"
    assert bf._breaker.consecutive_failures == 2
    bf._breaker.check()  # still closed: raises CircuitOpen otherwise


async def test_threshold_zero_disables_the_breaker() -> None:
    handler, calls = _boom()
    bf = client(handler, max_retries=0, circuit_failure_threshold=0)
    for _ in range(4):
        with pytest.raises(GatewayError):
            await bf.chat("hi")
    assert calls["n"] == 4


async def test_the_breaker_lets_a_call_through_once_the_open_window_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open is a pause, not a verdict: after ``circuit_open_seconds`` the next call is sent."""
    now = {"t": 1000.0}
    monkeypatch.setattr(breaker_module, "time", type("Clock", (), {"monotonic": lambda: now["t"]}))
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if state["fail"] else reply("back")

    bf = client(handler, max_retries=0, circuit_failure_threshold=1, circuit_open_seconds=30.0)
    with pytest.raises(GatewayError):
        await bf.chat("hi")
    now["t"] += 29.0
    with pytest.raises(CircuitOpen) as caught:
        await bf.chat("hi")
    assert caught.value.retry_after == 1.0
    assert caught.value.details["failures"] == 1
    now["t"] += 1.5
    state["fail"] = False
    assert await bf.chat("hi") == "back"
    await bf.aclose()


# ------------------------------------------------- the clients Bifrost builds for itself
#
# Every other test hands Bifrost a mocked httpx client. These let it build its own, as an
# application does, and intercept the wire with respx (whose router fails a test on any
# unmocked request or unused route) — so what is asserted is what a real gateway would get.

KEY = "vk-placeholder"
ADMIN_TOKEN = "admin-token-placeholder"


async def test_the_virtual_key_is_the_bearer_on_inference_and_mcp(
    respx_mock: respx.MockRouter,
) -> None:
    chat = respx_mock.post("http://gateway.test/v1/chat/completions").mock(return_value=reply("ok"))
    listing = respx_mock.post("http://gateway.test/mcp").mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    )
    bf = Bifrost("http://gateway.test/v1/", model="m", api_key=KEY)
    assert await bf.chat("hi") == "ok"
    assert await bf.tools() == []
    for route in (chat, listing):
        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.headers["Content-Type"] == "application/json"
    await bf.aclose()
    assert bf._client.is_closed and bf._admin.is_closed, "it closes what it opened"


async def test_the_admin_token_replaces_the_key_on_api_routes_only(
    respx_mock: respx.MockRouter,
) -> None:
    chat = respx_mock.post("http://gateway.test/v1/chat/completions").mock(return_value=reply("ok"))
    remove = respx_mock.delete("http://gateway.test/api/mcp/client/id-erp").mock(
        return_value=httpx.Response(200, json={})
    )
    bf = Bifrost("http://gateway.test/v1", model="m", api_key=KEY, admin_token=ADMIN_TOKEN)
    await bf.chat("hi")
    await bf.mcp.remove("id-erp")
    assert chat.calls.last.request.headers["Authorization"] == f"Bearer {KEY}"
    assert remove.calls.last.request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"
    await bf.aclose()


async def test_without_a_key_no_authorization_is_sent(respx_mock: respx.MockRouter) -> None:
    """A gateway with governance off takes anonymous calls; an empty bearer would be refused."""
    chat = respx_mock.post("http://gateway.test/v1/chat/completions").mock(return_value=reply("ok"))
    async with Bifrost("http://gateway.test/v1", model="m") as bf:
        await bf.chat("hi")
    assert "Authorization" not in chat.calls.last.request.headers


@pytest.mark.parametrize(("total", "connect"), [(60.0, 5.0), (2.0, 2.0)])
async def test_the_connect_timeout_is_capped_by_the_overall_one(
    total: float, connect: float
) -> None:
    """A gateway that does not accept a connection in five seconds is down; a client given
    less than that in total must not wait longer just to connect."""
    async with Bifrost("http://gateway.test/v1", model="m", timeout=total) as bf:
        for http in (bf._client, bf._admin):
            assert (http.timeout.read, http.timeout.connect) == (total, connect)


# ----------------------------------------------------------------- stream failure modes


async def test_a_stream_error_status_raises_once_with_its_body() -> None:
    """Streams are not retried: the first error status is the answer."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="upstream overloaded")

    bf = client(handler, max_retries=3)
    with pytest.raises(GatewayError) as caught:
        [d async for d in bf.stream("hi")]
    assert caught.value.details == {"status": 503, "body": "upstream overloaded"}
    assert calls["n"] == 1
    await bf.aclose()


async def test_a_rate_limited_stream_is_a_rate_limit() -> None:
    bf = client(lambda r: httpx.Response(429, headers={"Retry-After": "3"}))
    with pytest.raises(RateLimited) as caught:
        [d async for d in bf.stream("hi")]
    assert caught.value.retry_after == 3.0
    await bf.aclose()


async def test_a_stream_skips_everything_that_is_not_text() -> None:
    """Keep-alive comments, event lines, role-only and empty deltas, a malformed chunk: none
    of them is text, and a stream that ends without ``[DONE]`` simply ends."""
    body = (
        ": keep-alive\n\n"
        "event: message\n"
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        "data: {not json\n\n"
        'data: {"choices":[]}\n\n'
        'data: {"choices":[{"delta":{"content":""}}]}\n\n'
        'data:{"choices":[{"delta":{"content":"ok"}}]}\n\n'
    )
    bf = client(lambda r: httpx.Response(200, text=body))
    assert [d async for d in bf.stream("hi")] == ["ok"]
    await bf.aclose()


async def test_a_stream_sends_the_opinionated_body_with_stream_on() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, text="data: [DONE]\n\n")

    tools = [{"type": "function", "function": {"name": "lookup"}}]
    bf = client(handler, max_tokens=64)
    assert [d async for d in bf.stream("hi", tools=tools, temperature=0.7, top_p=0.9)] == []
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"] == {
        "model": "test/model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
        "temperature": 0.7,
        "top_p": 0.9,
        "tools": tools,
        "stream": True,
    }
    await bf.aclose()


async def test_a_stream_that_cannot_connect_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no bytes")

    bf = client(handler)
    with pytest.raises(Unreachable, match="ReadTimeout"):
        [d async for d in bf.stream("hi")]
    await bf.aclose()


# ----------------------------------------------------------------- retry schedule


def _sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the waits between attempts instead of sleeping them."""
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("bifrost_sdk._client.asyncio.sleep", sleep)
    return waits


def _then(*responses: httpx.Response):
    """A handler answering ``responses`` in order, counting the requests it saw."""
    queue = list(responses)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return queue.pop(0)

    return handler, calls


class _Draw:
    """A jitter source that always draws ``value``: the top of the window at 1.0."""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


async def test_without_advice_the_window_doubles_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = _sleeps(monkeypatch)
    handler, calls = _then(httpx.Response(502), httpx.Response(504), reply("ok"))
    bf = client(handler, max_retries=2, backoff_seconds=0.5)
    bf._rng = _Draw(1.0)
    assert await bf.chat("hi") == "ok"
    assert waits == [0.5, 1.0]
    assert calls["n"] == 3
    await bf.aclose()


async def test_the_backoff_is_a_uniform_draw_across_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full jitter: processes that failed in the same second must not all retry in the same
    second, or a gateway restart is followed by a stampede."""
    waits = _sleeps(monkeypatch)
    handler, _ = _then(*(httpx.Response(503) for _ in range(4)), reply("ok"))
    bf = client(handler, max_retries=4, backoff_seconds=1.0)
    bf._rng = random.Random(7)
    assert await bf.chat("hi") == "ok"
    expected = random.Random(7)
    assert waits == [expected.random() * 2**n for n in range(4)]
    assert all(0 <= wait < 2**n for n, wait in enumerate(waits))
    assert len(set(waits)) == 4
    await bf.aclose()


def test_backoff_windows_are_capped_and_drawn_from_the_shared_source_by_default() -> None:
    assert backoff(3, 0.5, _Draw(0.5)) == 2.0
    assert backoff(20, 0.5, _Draw(1.0)) == MAX_WAIT == 30.0
    assert backoff(0, 0.0) == 0.0
    assert 0 <= backoff(2, 1.0) < 4.0


async def test_a_rate_limits_own_delay_replaces_the_backoff_up_to_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's delay is honoured as given, unjittered — it knows when it has room —
    but no longer than MAX_WAIT, whatever it asks for."""
    waits = _sleeps(monkeypatch)
    handler, _ = _then(
        httpx.Response(429, headers={"Retry-After": "300"}),
        httpx.Response(429, text="Please retry in 2.5s"),
        reply("ok"),
    )
    bf = client(handler, max_retries=2, backoff_seconds=0.5)
    bf._rng = _Draw(0.0)
    assert await bf.chat("hi") == "ok"
    assert waits == [30.0, 2.5]
    await bf.aclose()


async def test_no_wait_follows_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    waits = _sleeps(monkeypatch)
    handler, calls = _then(httpx.Response(500), httpx.Response(500))
    bf = client(handler, max_retries=1, backoff_seconds=0.25)
    bf._rng = _Draw(1.0)
    with pytest.raises(GatewayError):
        await bf.chat("hi")
    assert (calls["n"], waits) == (2, [0.25])
    await bf.aclose()


async def test_timeouts_are_retried_and_then_reported_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sleeps(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    bf = client(handler, max_retries=2)
    with pytest.raises(Unreachable, match="ReadTimeout"):
        await bf.complete("hi")
    assert calls["n"] == 3
    assert bf._breaker.consecutive_failures == 1
    await bf.aclose()


async def test_a_timeout_then_success_is_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _sleeps(monkeypatch)
    answers: list[object] = [httpx.ConnectTimeout("slow"), reply("ok")]

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    bf = client(handler, max_retries=1)
    assert await bf.chat("hi") == "ok"
    assert bf._breaker.consecutive_failures == 0
    await bf.aclose()


# ----------------------------------------------------------------- reading the reply


async def test_a_non_json_200_is_a_gateway_fault_counted_by_the_breaker() -> None:
    handler, calls = _then(httpx.Response(200, text="<!doctype html>"))
    bf = client(handler, max_retries=3)
    with pytest.raises(GatewayError, match="non-JSON"):
        await bf.chat("hi")
    assert calls["n"] == 1, "a 200 is not retried, however unusable"
    assert bf._breaker.consecutive_failures == 1
    await bf.aclose()


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": None}])
async def test_a_reply_without_choices_is_a_gateway_error(payload) -> None:
    bf = client(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(GatewayError, match="no choices"):
        await bf.chat("hi")
    await bf.aclose()


async def test_content_parts_are_joined_into_text() -> None:
    parts = [{"type": "text", "text": "Hel"}, {"type": "image_url"}, "noise", {"text": "lo"}]
    bf = client(lambda r: reply(parts))
    assert await bf.chat("hi") == "Hello"
    await bf.aclose()


async def test_no_text_that_did_not_run_out_of_budget_is_an_empty_string() -> None:
    """A tool-call turn has no text and that is the answer, not a failure."""
    bf = client(lambda r: reply(None, finish="tool_calls"))
    assert await bf.chat("hi") == ""
    await bf.aclose()


async def test_an_empty_answer_carries_the_token_counts() -> None:
    usage = {"completion_tokens": 32, "completion_tokens_details": {"reasoning_tokens": 30}}
    bf = client(lambda r: reply("", finish="length", usage=usage))
    with pytest.raises(EmptyResponse) as caught:
        await bf.chat("hi")
    assert caught.value.details == {"completion_tokens": 32, "reasoning_tokens": 30}
    await bf.aclose()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("here: {not: json}", "is not JSON"),
        ("} backwards {", "is not JSON"),
        ("[1, 2]", "not a JSON object"),
    ],
)
async def test_json_refuses_what_is_not_one_object(text: str, message: str) -> None:
    bf = client(lambda r: reply(text))
    with pytest.raises(InvalidJSON, match=message) as caught:
        await bf.json("x")
    assert caught.value.details["body"] == text
    await bf.aclose()


async def test_json_sends_the_schema_as_a_strict_response_format() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(jsonlib.loads(request.content))
        return reply('{"a": 1}')

    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    bf = client(handler)
    await bf.json("x", schema=schema, system="extract")
    assert seen["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": schema, "strict": True},
    }
    assert seen["messages"][0] == {"role": "system", "content": "extract"}
    await bf.aclose()


async def test_json_without_a_schema_sends_no_response_format() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(jsonlib.loads(request.content))
        return reply('{"a": 1}')

    bf = client(handler)
    assert await bf.json("x") == {"a": 1}
    assert "response_format" not in seen
    await bf.aclose()


async def test_a_message_list_is_sent_as_given_after_the_system_turn() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(jsonlib.loads(request.content))
        return reply("ok")

    turns = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    bf = client(handler)
    await bf.complete(turns, system="s", max_tokens=10, temperature=0.2, seed=7)
    assert seen == {
        "model": "test/model",
        "messages": [{"role": "system", "content": "s"}, *turns],
        "max_tokens": 10,
        "temperature": 0.2,
        "seed": 7,
    }
    await bf.aclose()


# ----------------------------------------------------------------- the rest of Options


def test_every_remaining_option_has_its_header() -> None:
    """The fields the verb-level test does not send: governance, cache, project, MCP session."""
    assert Options(
        virtual_key="vk-placeholder",
        mcp_session_id="mcp-s",
        cache_key="k",
        cache_type="semantic",
        cache_threshold=0.9,
        project_id="proj",
    ).headers() == {
        "x-bf-vk": "vk-placeholder",
        "x-bf-mcp-session-id": "mcp-s",
        "x-bf-cache-key": "k",
        "x-bf-cache-type": "semantic",
        "x-bf-cache-threshold": "0.9",
        "x-bf-project-id": "proj",
    }


def test_a_prompt_without_a_version_selects_the_latest_committed_one() -> None:
    assert Options(prompt_id="p").headers() == {"x-bf-prompt-id": "p"}


def test_extra_headers_are_applied_last() -> None:
    """``extra`` is the escape hatch: it may override a header the fields produced."""
    options = Options(session_id="s", extra={"x-bf-session-id": "override"})
    assert options.headers() == {"x-bf-session-id": "override"}


def test_a_retry_after_date_without_a_zone_is_read_as_utc() -> None:
    value = retry_after({"retry-after": "Wed, 21 Oct 2099 07:28:00 -0000"})
    assert value is not None and value > 0
    assert retry_after({"retry-after": "Wed, 21 Oct 1999 07:28:00 -0000"}) == 0.0


def test_an_unreadable_header_falls_back_to_the_body() -> None:
    assert retry_after({"retry-after": "soon"}, "Please retry in 4s.") == 4.0
    assert retry_after({"retry-after": "-5"}) == 0.0, "a negative delay is no delay"


# ----------------------------------------------------------------- typed errors


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (400, BadRequestError, False),
        (401, AuthenticationError, False),
        (403, PermissionDeniedError, False),
        (404, NotFoundError, False),
        (409, ConflictError, True),
        (422, UnprocessableError, False),
        (500, ServerError, True),
        (502, ServerError, True),
        (503, ServerError, True),
        (504, ServerError, True),
        (501, ServerError, False),
        (408, GatewayError, True),
        (413, GatewayError, False),
    ],
)
async def test_each_status_has_its_type_and_says_whether_a_retry_could_help(
    status: int, kind: type[GatewayError], retryable: bool
) -> None:
    """Typed, but still ``GatewayError``: code that caught the one class keeps working."""
    bf = client(lambda r: httpx.Response(status, text="no"), max_retries=0)
    with pytest.raises(GatewayError) as caught:
        await bf.chat("hi")
    assert type(caught.value) is kind
    assert (caught.value.status, caught.value.retryable) == (status, retryable)
    assert caught.value.details == {"status": status, "body": "no"}
    await bf.aclose()


async def test_a_429_is_a_rate_limited_error_and_still_not_a_gateway_error() -> None:
    bf = client(lambda r: httpx.Response(429, headers={"Retry-After": "4"}), max_retries=0)
    with pytest.raises(RateLimitedError) as caught:
        await bf.chat("hi")
    assert isinstance(caught.value, RateLimited)
    assert not isinstance(caught.value, GatewayError)
    assert (caught.value.status, caught.value.retry_after, caught.value.retryable) == (
        429,
        4.0,
        True,
    )
    await bf.aclose()


def test_every_error_says_whether_a_retry_could_help() -> None:
    """``AgentError.of`` in the contracts reads ``retryable`` off the exception itself."""
    assert Unreachable("x").retryable
    assert RateLimited("x").retryable
    assert CircuitOpen("x").retryable
    assert not EmptyResponse("x").retryable
    assert not InvalidJSON("x").retryable
    assert not BifrostError("x").retryable
    unusable = GatewayError("gateway returned a non-JSON body")
    assert (unusable.status, unusable.retryable) == (None, False)


def test_the_typed_errors_are_public() -> None:
    names = {
        "AuthenticationError",
        "BadRequestError",
        "ConflictError",
        "NotFoundError",
        "PermissionDeniedError",
        "RateLimitedError",
        "ServerError",
        "UnprocessableError",
    }
    assert names <= set(bifrost_sdk.__all__)
    assert all(hasattr(bifrost_sdk, name) for name in names)


# ----------------------------------------------------------------- what the breaker counts


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
async def test_a_refused_request_never_opens_the_breaker(status: int) -> None:
    """A healthy gateway refusing a bad prompt in milliseconds says nothing about the gateway.
    Counting it let a few over-long prompts fail every other agent sharing the client. A 409
    is retried first, and does not count once the retries are spent either."""
    handler, calls = _boom(status)
    bf = client(handler, max_retries=1, backoff_seconds=0.0, circuit_failure_threshold=2)
    for _ in range(5):
        with pytest.raises(GatewayError):
            await bf.chat("hi")
    assert calls["n"] == 5 * (2 if status == 409 else 1)
    assert bf._breaker.consecutive_failures == 0
    bf._breaker.check()
    await bf.aclose()


async def test_a_refusal_neither_counts_nor_resets_a_streak_of_server_errors() -> None:
    handler, _ = _then(httpx.Response(500), httpx.Response(400), httpx.Response(500))
    bf = client(handler, max_retries=0, circuit_failure_threshold=2)
    for _ in range(3):
        with pytest.raises(GatewayError):
            await bf.chat("hi")
    with pytest.raises(CircuitOpen):
        await bf.chat("hi")
    await bf.aclose()


async def test_a_server_error_no_retry_can_fix_still_counts() -> None:
    handler, calls = _boom(501)
    bf = client(handler, max_retries=3, circuit_failure_threshold=1)
    with pytest.raises(ServerError):
        await bf.chat("hi")
    assert calls["n"] == 1, "501 is not in RETRYABLE"
    with pytest.raises(CircuitOpen):
        await bf.chat("hi")
    await bf.aclose()


def test_the_breaker_counts_only_what_says_the_gateway_is_unwell() -> None:
    assert breaker_module.counts(None)
    assert breaker_module.counts(Unreachable("x"))
    assert breaker_module.counts(ServerError("x", status=503))
    assert breaker_module.counts(GatewayError("unusable body"))
    assert not breaker_module.counts(RateLimitedError("x", status=429))
    assert not breaker_module.counts(AuthenticationError("x", status=401))
    assert not breaker_module.counts(EmptyResponse("x"))


# ----------------------------------------------------------------- the stream and the breaker


async def test_an_open_circuit_fails_a_stream_before_anything_is_sent() -> None:
    handler, calls = _boom(503)
    bf = client(handler, max_retries=0, circuit_failure_threshold=1)
    with pytest.raises(ServerError):
        await bf.chat("hi")
    with pytest.raises(CircuitOpen):
        [d async for d in bf.stream("hi")]
    assert calls["n"] == 1
    await bf.aclose()


async def test_a_stream_that_fails_to_start_counts_like_a_completion() -> None:
    handler, _ = _boom(503)
    bf = client(handler, circuit_failure_threshold=2)
    for _ in range(2):
        with pytest.raises(ServerError):
            [d async for d in bf.stream("hi")]
    with pytest.raises(CircuitOpen):
        await bf.chat("hi")
    await bf.aclose()


async def test_a_refused_stream_does_not_count() -> None:
    handler, _ = _boom(400)
    bf = client(handler, circuit_failure_threshold=1)
    with pytest.raises(BadRequestError):
        [d async for d in bf.stream("hi")]
    assert bf._breaker.consecutive_failures == 0
    await bf.aclose()


async def test_an_accepted_stream_ends_a_failure_streak() -> None:
    handler, _ = _then(
        httpx.Response(500),
        httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"}}]}'),
    )
    bf = client(handler, max_retries=0, circuit_failure_threshold=3)
    with pytest.raises(ServerError):
        await bf.chat("hi")
    assert bf._breaker.consecutive_failures == 1
    assert [d async for d in bf.stream("hi")] == ["ok"]
    assert bf._breaker.consecutive_failures == 0
    await bf.aclose()


async def test_a_stream_that_cannot_connect_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    bf = client(handler, circuit_failure_threshold=1)
    with pytest.raises(Unreachable):
        [d async for d in bf.stream("hi")]
    with pytest.raises(CircuitOpen):
        [d async for d in bf.stream("hi")]
    await bf.aclose()


class _DropsMidStream(httpx.AsyncByteStream):
    """One chunk of text, then the connection goes."""

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        raise httpx.ReadError("connection reset")


async def test_a_connection_lost_mid_stream_is_unreachable_and_counts() -> None:
    bf = client(
        lambda r: httpx.Response(200, stream=_DropsMidStream()), circuit_failure_threshold=1
    )
    seen: list[str] = []
    with pytest.raises(Unreachable, match="ReadError"):
        async for delta in bf.stream("hi"):
            seen.append(delta)
    assert seen == ["Hel"]
    with pytest.raises(CircuitOpen):
        await bf.chat("hi")
    await bf.aclose()


async def test_an_error_reported_inside_a_stream_is_raised_not_a_short_answer() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"error":{"message":"upstream provider disconnected","type":"server_error"}}\n\n'
    )
    bf = client(lambda r: httpx.Response(200, text=body))
    seen: list[str] = []
    with pytest.raises(GatewayError, match="mid-stream") as caught:
        async for delta in bf.stream("hi"):
            seen.append(delta)
    assert seen == ["Hel"]
    assert "upstream provider disconnected" in caught.value.details["body"]
    await bf.aclose()


async def test_a_stream_ignores_chunks_of_the_wrong_shape() -> None:
    body = (
        "data: 42\n\n"
        'data: {"choices":["junk"]}\n\n'
        'data: {"choices":[{"delta":"junk"}]}\n\n'
        'data: {"choices":{"0":{}}}\n\n'
        'data: {"error":null,"choices":[{"delta":{"content":"ok"}}]}\n\n'
    )
    bf = client(lambda r: httpx.Response(200, text=body))
    assert [d async for d in bf.stream("hi")] == ["ok"]
    await bf.aclose()


# ----------------------------------------------------------------- the rest of the hardening


def test_a_negative_retry_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        Bifrost(BASE, max_retries=-1)


async def test_a_200_whose_json_is_not_an_object_is_a_counted_gateway_fault() -> None:
    bf = client(lambda r: httpx.Response(200, json=[1, 2]), max_retries=2)
    with pytest.raises(GatewayError, match="not an object") as caught:
        await bf.chat("hi")
    assert caught.value.status is None
    assert bf._breaker.consecutive_failures == 1
    await bf.aclose()


async def test_both_clients_pool_and_time_out_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict] = []

    class Spy(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Spy)
    async with Bifrost(BASE, model="m", timeout=12.0):
        pass
    assert len(built) == 2
    for kwargs in built:
        pool = kwargs["limits"]
        assert (pool.max_connections, pool.max_keepalive_connections, pool.keepalive_expiry) == (
            100,
            20,
            30.0,
        )
        assert kwargs["timeout"] == httpx.Timeout(connect=5.0, read=12.0, write=12.0, pool=12.0)
