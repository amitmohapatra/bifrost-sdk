"""How a request proves who sent it: one ``httpx.Auth`` per login, picked by name.

``bearer`` is a static key: an OpenAI-style API key, a Bifrost virtual key. ``google`` is a
Google access token, refreshed before it expires (Vertex AI's OpenAI-compatible endpoint).
``aws`` signs every request with SigV4 (Bedrock's). The last two need an extra each,
``bifrost-sdk[google]`` (google-auth) and ``bifrost-sdk[aws]`` (botocore), imported only when
the strategy is built. Without credentials passed in, google-auth and botocore find their own
the way they always do (application default credentials; the AWS environment, shared config
or instance role); this package reads no environment variable itself.

    auth = auth_strategy("google")                  # by name, as a setting gives it
    bf = Bifrost(url, model="google/gemini-2.5-pro", auth=auth)
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from collections.abc import AsyncGenerator, Generator, Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Self

import httpx

if TYPE_CHECKING:
    from botocore.credentials import Credentials as AwsCredentials
    from google.auth.credentials import Credentials as GoogleCredentials

#: The scope a Google token needs for Vertex AI.
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
#: The SigV4 service name Bedrock's endpoints are signed for.
BEDROCK = "bedrock"
#: An httpx auth flow: yields the request, is sent each response.
type _Flow = Generator[httpx.Request, httpx.Response, None]
type _AsyncFlow = AsyncGenerator[httpx.Request, httpx.Response]
#: The headers a SigV4 signature adds to a request.
_SIGV4_HEADERS = ("Authorization", "X-Amz-Date", "X-Amz-Security-Token")


class AuthStrategy(httpx.Auth):
    """An ``httpx.Auth`` the registry (:data:`AUTH_STRATEGIES`) can build from a key alone."""

    @classmethod
    def from_key(cls, api_key: str | None) -> Self:
        """The strategy for a setting that gives at most a key."""
        raise NotImplementedError


class BearerAuth(AuthStrategy):
    """``Authorization: Bearer <key>`` on every request."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("bearer auth needs a key")
        self._header = f"Bearer {api_key}"

    @classmethod
    def from_key(cls, api_key: str | None) -> Self:
        return cls(api_key or "")

    def auth_flow(self, request: httpx.Request) -> _Flow:
        request.headers["Authorization"] = self._header
        yield request


class GoogleAuth(AuthStrategy):
    """A Google OAuth access token as the bearer, refreshed when it has expired.

    ``credentials`` defaults to the application default credentials for ``scopes``. A token
    is refreshed by google-auth, which blocks, so the async flow does that in a thread and
    only then; a valid token is applied in place.
    """

    def __init__(
        self,
        credentials: GoogleCredentials | None = None,
        *,
        scopes: Sequence[str] = (CLOUD_PLATFORM,),
    ) -> None:
        google_auth = _extra("google.auth", "google")
        transport = _extra("google.auth.transport.requests", "google")
        if credentials is None:
            credentials, _ = google_auth.default(scopes=list(scopes))
        self._credentials = credentials
        self._transport = transport.Request()
        # One refresh at a time: two requests finding the token expired refresh it once.
        self._lock = threading.Lock()

    @classmethod
    def from_key(cls, api_key: str | None) -> Self:
        _no_key("google", api_key)
        return cls()

    def _apply(self, request: httpx.Request) -> None:
        headers: dict[str, str] = {}
        with self._lock:
            # Refreshes an expired token, then writes Authorization (and a quota project).
            self._credentials.before_request(
                self._transport, request.method, str(request.url), headers
            )
        request.headers.update(headers)

    def sync_auth_flow(self, request: httpx.Request) -> _Flow:
        self._apply(request)
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response, None]:
        if self._credentials.valid:
            self._apply(request)
        else:
            await asyncio.to_thread(self._apply, request)
        yield request


class AwsSigV4Auth(AuthStrategy):
    """Each request signed with AWS Signature Version 4, body included.

    ``credentials`` and ``region`` default to botocore's (the AWS environment, shared config
    or instance role; ``AWS_DEFAULT_REGION``). Credentials that must be refreshed are
    refreshed by botocore, which blocks, so the async flow signs in a thread only then.
    """

    requires_request_body = True

    def __init__(
        self,
        *,
        region: str | None = None,
        service: str = BEDROCK,
        credentials: AwsCredentials | None = None,
    ) -> None:
        session = _extra("botocore.session", "aws").get_session()
        found = credentials or session.get_credentials()
        if found is None:
            raise ValueError("aws auth found no AWS credentials (environment, config or role)")
        self._credentials = found
        self._region = region or session.get_config_variable("region")
        if not self._region:
            raise ValueError("aws auth needs a region: region= or AWS_DEFAULT_REGION")
        self._service = service

    @classmethod
    def from_key(cls, api_key: str | None) -> Self:
        _no_key("aws", api_key)
        return cls()

    def _sign(self, request: httpx.Request) -> None:
        from botocore.auth import SigV4Auth  # noqa: PLC0415 - the [aws] extra, checked in __init__
        from botocore.awsrequest import AWSRequest  # noqa: PLC0415

        # Only what no proxy rewrites is signed: the host, the content type, the body.
        signed = {"host": request.headers["host"]}
        if "content-type" in request.headers:
            signed["content-type"] = request.headers["content-type"]
        aws = AWSRequest(
            method=request.method, url=str(request.url), data=request.content, headers=signed
        )
        frozen = self._credentials.get_frozen_credentials()
        SigV4Auth(frozen, self._service, self._region).add_auth(aws)
        for name in _SIGV4_HEADERS:
            if name in aws.headers:
                request.headers[name] = aws.headers[name]

    def sync_auth_flow(self, request: httpx.Request) -> _Flow:
        request.read()
        self._sign(request)
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response, None]:
        await request.aread()
        refresh_needed = getattr(self._credentials, "refresh_needed", None)
        if refresh_needed is not None and refresh_needed():
            await asyncio.to_thread(self._sign, request)
        else:
            self._sign(request)
        yield request


#: Auth strategies by the name a setting gives (``LLM_AUTH`` in the Trellis repos). A strategy
#: of your own is added here under its name.
AUTH_STRATEGIES: dict[str, type[AuthStrategy]] = {
    "bearer": BearerAuth,
    "google": GoogleAuth,
    "aws": AwsSigV4Auth,
}


def auth_strategy(name: str, api_key: str | None = None) -> AuthStrategy:
    """The strategy registered as ``name``, built from ``api_key`` (``bearer``'s only)."""
    try:
        strategy = AUTH_STRATEGIES[name]
    except KeyError:
        known = ", ".join(sorted(AUTH_STRATEGIES))
        raise ValueError(f"unknown auth strategy {name!r}; known: {known}") from None
    return strategy.from_key(api_key)


def _extra(module: str, extra: str) -> ModuleType:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"{extra} auth needs the [{extra}] extra: pip install 'bifrost-sdk[{extra}]'"
        ) from exc


def _no_key(name: str, api_key: str | None) -> None:
    """A key given to a login that signs with its own credentials is a misconfiguration."""
    if api_key:
        raise ValueError(f"{name} auth signs with its own credentials and takes no key")


__all__ = [
    "AUTH_STRATEGIES",
    "BEDROCK",
    "CLOUD_PLATFORM",
    "AuthStrategy",
    "AwsSigV4Auth",
    "BearerAuth",
    "GoogleAuth",
    "auth_strategy",
]
