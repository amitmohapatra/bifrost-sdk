"""Every registered route, auth strategy and profile, against stubs of each wire format.

Routes (:data:`GATEWAYS`) against OpenAI's chat completions and the Anthropic, Gemini and
Bifrost endpoints that speak it; auth strategies (:data:`AUTH_STRATEGIES`) built by name, as a
setting gives them, with a Google token that expires and is refreshed and an AWS SigV4
signature recomputed here from the specification; profiles that shape each request. The
endpoints are ``respx`` stubs: the client builds its own HTTP client, auth included.
"""

from __future__ import annotations

import hashlib
import hmac
import json as jsonlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import google.auth
import httpx
import pytest
import respx
from google.auth import credentials as google_credentials

from bifrost_sdk import (
    AUTH_STRATEGIES,
    GATEWAYS,
    AwsSigV4Auth,
    BearerAuth,
    Bifrost,
    BifrostOnlyError,
    GoogleAuth,
    ModelProfile,
    Options,
    auth_strategy,
)
from bifrost_sdk.auth import BEDROCK, CLOUD_PLATFORM
from bifrost_sdk.headers import MCP_CLIENTS, NO_GATEWAY_TOOLS

# ----------------------------------------------------------------- the wire formats


@dataclass(frozen=True)
class Wire:
    """One endpoint speaking chat completions: where it is, what it answers."""

    name: str
    base_url: str
    model: str
    reply: dict[str, Any]

    @property
    def completions(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def _choice(content: str) -> list[dict[str, Any]]:
    return [
        {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
    ]


WIRES = [
    Wire(
        "openai",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        {"id": "chatcmpl-1", "object": "chat.completion", "choices": _choice("pong")},
    ),
    Wire(
        "anthropic-compatible",
        "https://api.anthropic.com/v1/",
        "claude-haiku-4-5",
        {"id": "msg_01", "object": "chat.completion", "choices": _choice("pong")},
    ),
    Wire(
        "gemini-compatible",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.5-flash",
        {"object": "chat.completion", "model": "gemini-2.5-flash", "choices": _choice("pong")},
    ),
    Wire(
        "bifrost",
        "http://bifrost.test/v1",
        "openai/gpt-4o-mini",
        {"choices": _choice("pong"), "extra_fields": {"provider": "openai"}},
    ),
]


def _stream(text: str) -> httpx.Response:
    chunks = [{"choices": [{"delta": {"content": part}}]} for part in text]
    body = "".join(f"data: {jsonlib.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("gateway", GATEWAYS)
@pytest.mark.parametrize("wire", WIRES, ids=lambda wire: wire.name)
async def test_every_route_completes_against_every_wire_format(wire: Wire, gateway: str) -> None:
    with respx.mock(assert_all_called=True) as stub:
        route = stub.post(wire.completions).mock(
            side_effect=[httpx.Response(200, json=wire.reply), _stream("pong")]
        )
        async with Bifrost(wire.base_url, model=wire.model, api_key="k", gateway=gateway) as bf:
            assert await bf.chat("ping") == "pong"
            assert [d async for d in bf.stream("ping")] == ["p", "o", "n", "g"]
    for call in route.calls:
        sent = call.request
        assert jsonlib.loads(sent.content)["model"] == wire.model  # the name as given
        assert sent.headers["Authorization"] == "Bearer k"
        bifrost_headers = {k for k in sent.headers if k.startswith("x-bf-")}
        # The deny-all MCP scope is Bifrost's; another endpoint gets no x-bf-* header at all.
        assert bifrost_headers == (set(NO_GATEWAY_TOOLS) if gateway == "bifrost" else set())


# ----------------------------------------------------------------- Bifrost-only methods

ONE_CALL = {"id": "c1", "function": {"name": "erp-get_stock", "arguments": "{}"}}


@pytest.mark.parametrize(
    "call",
    [
        lambda bf: bf.tools(),
        lambda bf: bf.execute_tool(ONE_CALL),
        lambda bf: bf.mcp_logs(datetime.now(UTC)),
        lambda bf: bf.chat("hi", options=Options(session_id="s")),
        lambda bf: bf.stream("hi", options=Options(session_id="s")).__anext__(),
        lambda bf: bf.mcp.clients(),
    ],
    ids=["tools", "execute_tool", "mcp_logs", "options", "stream options", "mcp"],
)
async def test_a_bifrost_only_method_on_another_gateway_is_refused_unsent(call) -> None:
    with respx.mock(assert_all_mocked=True) as stub:
        async with Bifrost("https://api.openai.com/v1", model="m", api_key="k") as bf:
            assert bf.gateway == "openai-compatible"  # the default
            with pytest.raises(BifrostOnlyError, match="gateway='bifrost'"):
                await call(bf)
        assert not stub.calls


def test_a_management_credential_needs_a_bifrost_gateway() -> None:
    with pytest.raises(BifrostOnlyError, match="admin_token"):
        Bifrost("https://api.openai.com/v1", admin_token="t")


def test_an_unknown_gateway_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown gateway 'litellm'"):
        Bifrost("https://x/v1", gateway="litellm")  # type: ignore[arg-type]


async def test_bifrost_keeps_refusing_a_scope_on_a_completion() -> None:
    async with Bifrost("http://bifrost.test/v1", model="m", gateway="bifrost") as bf:
        with pytest.raises(ValueError, match="never carries"):
            await bf.chat("hi", options=Options(mcp_clients=["erp"]))
    assert MCP_CLIENTS in NO_GATEWAY_TOOLS


# ----------------------------------------------------------------- profiles

PLAIN, REASONING, NO_TEMPERATURE = (
    ModelProfile(),
    ModelProfile(reasoning_output=True, temperature=False),
    ModelProfile(temperature=False),
)


@pytest.mark.parametrize(
    ("profile", "budget", "sends_temperature"),
    [
        (PLAIN, "max_tokens", True),
        (REASONING, "max_completion_tokens", False),
        (NO_TEMPERATURE, "max_completion_tokens", False),
        (ModelProfile(reasoning_output=True), "max_completion_tokens", False),
    ],
    ids=["plain", "reasoning", "no temperature", "reasoning only"],
)
async def test_every_profile_shapes_every_verb(
    profile: ModelProfile, budget: str, sends_temperature: bool
) -> None:
    wire = WIRES[0]
    with respx.mock() as stub:
        route = stub.post(wire.completions).mock(
            side_effect=lambda request: (
                _stream("ok")
                if jsonlib.loads(request.content).get("stream")
                else httpx.Response(200, json={"choices": _choice('{"ok": true}')})
            )
        )
        async with Bifrost(wire.base_url, model="any-name", profile=profile) as bf:
            await bf.chat("x")
            await bf.json("x")
            [d async for d in bf.stream("x")]
            await bf.complete("x", max_tokens=99, temperature=0.7)
    bodies = [jsonlib.loads(call.request.content) for call in route.calls]
    for body in bodies:
        assert set(body) & {"max_tokens", "max_completion_tokens"} == {budget}
        assert ("temperature" in body) is sends_temperature
    assert bodies[-1][budget] == 99
    assert bodies[0][budget] == 2048  # the client's default budget, under the profile's name


async def test_complete_still_sends_only_what_it_is_given() -> None:
    wire = WIRES[0]
    with respx.mock() as stub:
        route = stub.post(wire.completions).respond(200, json=wire.reply)
        async with Bifrost(wire.base_url, model="m", profile=REASONING) as bf:
            await bf.complete("x")
    assert set(jsonlib.loads(route.calls.last.request.content)) == {"model", "messages"}


# ----------------------------------------------------------------- auth strategies


class FakeGoogleCredentials(google_credentials.Credentials):
    """Credentials whose every refresh issues the next token, valid for an hour."""

    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    def refresh(self, request: Any) -> None:
        self.refreshes += 1
        self.token = f"ya29.token-{self.refreshes}"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    def expire(self) -> None:
        self.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)


@pytest.fixture
def google_default(monkeypatch: pytest.MonkeyPatch) -> FakeGoogleCredentials:
    """Application default credentials: the fake, asked for the Vertex scope."""
    fake = FakeGoogleCredentials()

    def default(scopes: list[str] | None = None, **_: Any) -> tuple[Any, str]:
        assert scopes == [CLOUD_PLATFORM]
        return fake, "project"

    monkeypatch.setattr(google.auth, "default", default)
    return fake


AWS_KEY, AWS_SECRET, AWS_REGION = (
    "AKIDEXAMPLE",
    "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    "us-east-1",
)


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """An AWS environment of known credentials, and nothing of the machine's."""
    for name in ("AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", AWS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", AWS_SECRET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def _sigv4(request: httpx.Request, secret: str, service: str = BEDROCK) -> str:
    """The SigV4 signature of ``request``, computed from the specification, not botocore."""
    authorization = request.headers["Authorization"]
    fields = dict(part.split("=", 1) for part in authorization.split(" ", 1)[1].split(", "))
    signed = fields["SignedHeaders"].split(";")
    amz_date = request.headers["X-Amz-Date"]
    scope = f"{amz_date[:8]}/{AWS_REGION}/{service}/aws4_request"
    canonical = "\n".join(
        [
            request.method,
            request.url.path,
            request.url.query.decode(),
            "".join(f"{name}:{request.headers[name].strip()}\n" for name in signed),
            ";".join(signed),
            hashlib.sha256(request.content).hexdigest(),
        ]
    )
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = f"AWS4{secret}".encode()
    for part in (amz_date[:8], AWS_REGION, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    assert fields["Credential"] == f"{AWS_KEY}/{scope}"
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def _signature(request: httpx.Request) -> str:
    return request.headers["Authorization"].rsplit("Signature=", 1)[1]


@pytest.mark.parametrize("name", sorted(AUTH_STRATEGIES))
async def test_every_auth_strategy_built_by_name_authenticates_a_request(
    name: str, google_default: FakeGoogleCredentials, aws_env: None
) -> None:
    auth = auth_strategy(name, "sk-1" if name == "bearer" else None)
    assert isinstance(auth, AUTH_STRATEGIES[name])
    url = "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
    with respx.mock() as stub:
        route = stub.post(f"{url}/chat/completions").respond(200, json=WIRES[0].reply)
        async with Bifrost(url, model="m", auth=auth) as bf:
            assert await bf.chat("ping") == "pong"
    sent = route.calls.last.request
    expected = {
        "bearer": lambda: sent.headers["Authorization"] == "Bearer sk-1",
        "google": lambda: sent.headers["Authorization"] == "Bearer ya29.token-1",
        "aws": lambda: _signature(sent) == _sigv4(sent, AWS_SECRET),
    }
    assert expected[name]()


async def test_a_google_token_is_refreshed_once_it_expires(
    google_default: FakeGoogleCredentials,
) -> None:
    wire = WIRES[2]
    with respx.mock() as stub:
        route = stub.post(wire.completions).respond(200, json=wire.reply)
        async with Bifrost(wire.base_url, model="m", auth=GoogleAuth()) as bf:
            await bf.chat("a")
            await bf.chat("b")  # still valid: not refreshed
            google_default.expire()
            await bf.chat("c")
    tokens = [call.request.headers["Authorization"] for call in route.calls]
    assert tokens == ["Bearer ya29.token-1", "Bearer ya29.token-1", "Bearer ya29.token-2"]
    assert google_default.refreshes == 2


def test_google_auth_works_on_a_sync_client_too() -> None:
    fake = FakeGoogleCredentials()
    with respx.mock() as stub:
        route = stub.get("https://x.test/v1/models").respond(200, json={"data": []})
        httpx.Client(auth=GoogleAuth(fake)).get("https://x.test/v1/models")
    assert route.calls.last.request.headers["Authorization"] == "Bearer ya29.token-1"


async def test_an_aws_signature_covers_the_body_and_a_session_token(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-1")
    url = "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
    with respx.mock() as stub:
        route = stub.post(f"{url}/chat/completions").respond(200, json=WIRES[0].reply)
        async with Bifrost(url, model="m", auth=AwsSigV4Auth()) as bf:
            await bf.chat("ping")
    sent = route.calls.last.request
    assert sent.headers["X-Amz-Security-Token"] == "session-1"
    assert "x-amz-security-token" in sent.headers["Authorization"]
    assert _signature(sent) == _sigv4(sent, AWS_SECRET)
    tampered = httpx.Request(sent.method, sent.url, headers=sent.headers, content=b"{}")
    assert _signature(sent) != _sigv4(tampered, AWS_SECRET)


def test_aws_auth_signs_on_a_sync_client_for_another_service(aws_env: None) -> None:
    with respx.mock() as stub:
        route = stub.get("https://x.amazonaws.com/v1/models").respond(200, json={"data": []})
        httpx.Client(auth=AwsSigV4Auth(service="sagemaker")).get(
            "https://x.amazonaws.com/v1/models"
        )
    sent = route.calls.last.request
    assert _signature(sent) == _sigv4(sent, AWS_SECRET, service="sagemaker")


async def test_refreshable_aws_credentials_are_signed_after_a_refresh(aws_env: None) -> None:
    from botocore.credentials import RefreshableCredentials  # noqa: PLC0415

    def fetch() -> dict[str, str]:
        expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        return {"access_key": AWS_KEY, "secret_key": AWS_SECRET, "token": "", "expiry_time": expiry}

    stale = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    credentials = RefreshableCredentials.create_from_metadata(
        {"access_key": AWS_KEY, "secret_key": "old", "token": "", "expiry_time": stale},
        fetch,
        "test",
    )
    with respx.mock() as stub:
        route = stub.post("https://x.amazonaws.com/v1/chat/completions").respond(
            200, json=WIRES[0].reply
        )
        async with Bifrost(
            "https://x.amazonaws.com/v1", model="m", auth=AwsSigV4Auth(credentials=credentials)
        ) as bf:
            await bf.chat("ping")
    sent = route.calls.last.request
    assert _signature(sent) == _sigv4(sent, AWS_SECRET)


@pytest.mark.parametrize(
    ("name", "key", "message"),
    [
        ("bearer", None, "needs a key"),
        ("google", "sk-1", "takes no key"),
        ("aws", "sk-1", "takes no key"),
        ("kerberos", None, "unknown auth strategy 'kerberos'; known: aws, bearer, google"),
    ],
)
def test_a_setting_no_strategy_can_use_is_refused(name: str, key: str | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        auth_strategy(name, key)


def test_aws_auth_without_a_region_or_credentials_says_which(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    with pytest.raises(ValueError, match="needs a region"):
        AwsSigV4Auth()
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name)
    with pytest.raises(ValueError, match="no AWS credentials"):
        AwsSigV4Auth(region=AWS_REGION)


@pytest.mark.parametrize(
    ("strategy", "module", "extra"),
    [(GoogleAuth, "google.auth", "google"), (AwsSigV4Auth, "botocore.session", "aws")],
)
def test_a_strategy_without_its_extra_names_the_extra(
    strategy: type, module: str, extra: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(__import__("sys").modules, module, None)
    with pytest.raises(ImportError, match=rf"bifrost-sdk\[{extra}\]"):
        strategy()


def test_a_key_and_an_auth_together_are_refused() -> None:
    with pytest.raises(ValueError, match="not both"):
        Bifrost("https://x/v1", api_key="k", auth=BearerAuth("k"))


def test_an_auth_strategy_must_say_how_it_is_built_from_a_key() -> None:
    from bifrost_sdk import AuthStrategy  # noqa: PLC0415

    with pytest.raises(NotImplementedError):
        AuthStrategy.from_key(None)
