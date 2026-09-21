"""A small client for a Bifrost LLM gateway.

Three verbs, because there are three things callers do::

    async with Bifrost("http://gateway/v1", model="gemini/gemini-3.6-flash") as bf:
        text = await bf.chat("summarise this")                  # -> str
        data = await bf.json("extract the fields", schema=S)    # -> dict
        async for delta in bf.stream("write a story"):          # -> AsyncIterator[str]
            ...

...and ``complete()`` when you need the gateway's whole response — usage, tool calls, finish
reason — rather than the part of it the three verbs keep.

The gateway holds the provider keys, so this client knows a URL and a model *name* and never
a provider credential. No provider SDK is imported here, which is what lets the same code run
against OpenAI, Anthropic or a local model by changing a string.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from bifrost_sdk._call import Call
from bifrost_sdk._errors import (
    EmptyResponse,
    GatewayError,
    InvalidJSON,
    RateLimited,
    Unreachable,
)
from bifrost_sdk._headers import Options
from bifrost_sdk._retry import RETRYABLE, backoff, retry_after
from bifrost_sdk.resources import MCP, Governance, Prompts, Routing, Skills, VirtualKeys

#: A message is ``{"role": ..., "content": ...}``; a bare string is shorthand for one user turn.
Messages = str | Sequence[dict[str, Any]]

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)

#: HTTP boundaries this client reasons about.
_ERROR = 400  # at or above: the gateway reported a problem
_RATE_LIMITED = 429
#: How much of an error body to carry on the exception, for logs and debugging.
_BODY_EXCERPT = 500
_SERVER_ERROR = 500  # at or above: the gateway itself is unwell, so ping() says no


def _origin(base_url: str) -> str:
    """The scheme+host of a gateway URL, dropping the ``/v1`` (or any) path suffix."""
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else base_url.rstrip("/")


def _admin_headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _messages(prompt: Messages, system: str | None) -> list[dict[str, Any]]:
    turns = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
    return [{"role": "system", "content": system}, *turns] if system else turns


class Bifrost:
    """One gateway, one default model, three verbs.

    Everything below the public methods exists because a real gateway did it to us: rate
    limits that carry their delay in the body, reasoning models that answer with nothing, and
    a circuit breaker that must not confuse "slow down" with "broken".
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        max_tokens: int = 2048,
        client: httpx.AsyncClient | None = None,
        admin_token: str | None = None,
        admin_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required (the gateway's /v1 endpoint)")
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        headers = {"Content-Type": "application/json"}
        if api_key:
            # A gateway virtual key, never a provider key: the provider's credential stays in
            # the gateway, which is the whole reason for having one.
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
        )
        self._owns_client = client is None
        #: Management routes live at ``/api/*`` — a sibling of the ``/v1`` inference base,
        #: not a child of it — and they authenticate with a bearer token rather than the
        #: virtual key inference uses. A second httpx client rooted at the origin is the
        #: honest way to say that; sticking "../api" on the inference base works until
        #: someone deploys the gateway under a path prefix.
        self._admin = admin_client or httpx.AsyncClient(
            base_url=_origin(base_url),
            headers=_admin_headers(admin_token or api_key),
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
        )
        self._owns_admin = admin_client is None

        self.mcp = MCP(self)
        self.prompts = Prompts(self)
        self.skills = Skills(self)
        self.vk = VirtualKeys(self)
        self.governance = Governance(self)
        self.routing = Routing(self)

    # ----------------------------------------------------------------- the three verbs

    async def chat(
        self,
        prompt: Messages,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        _options: Options | None = None,
        **extra: Any,
    ) -> str:
        """The model's reply, as text."""
        body = self._body(
            prompt,
            model=model,
            system=system,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature,
            tools=tools,
            extra=extra,
        )
        return self._text(await self._post(body, options=_options))

    async def json(
        self,
        prompt: Messages,
        *,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        _options: Options | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """The model's reply, parsed as an object.

        ``response_format`` is the gateway's job, but not every upstream provider honours it,
        so the text is parsed defensively rather than trusted.
        """
        body = self._body(
            prompt,
            model=model,
            system=system,
            max_tokens=max_tokens or self.max_tokens,
            temperature=0.0,  # structured output is a parse, not a performance
            extra=extra,
        )
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            }
        return _parse_json(self._text(await self._post(body, options=_options)))

    async def stream(
        self,
        prompt: Messages,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        _options: Options | None = None,
        **extra: Any,
    ) -> AsyncIterator[str]:
        """Text deltas, yielded as they arrive.

        Nothing is buffered: time-to-first-token is the point of streaming, and collecting the
        whole answer before yielding would throw it away. Streams are not retried — a partly
        consumed stream cannot be replayed without showing the caller duplicate text.
        """
        body = self._body(
            prompt,
            model=model,
            system=system,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature,
            tools=tools,
            extra=extra,
        ) | {"stream": True}
        try:
            headers = _options.headers() if _options is not None else None
            async with self._client.stream(
                "POST", "/chat/completions", json=body, headers=headers
            ) as response:
                if response.status_code >= _ERROR:
                    await response.aread()
                    raise self._error(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    delta = _delta(data)
                    if delta:
                        yield delta
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Unreachable(f"gateway unreachable ({type(exc).__name__})") from exc

    # ----------------------------------------------------------------- the raw payload

    async def complete(
        self,
        prompt: Messages,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        # ASYNC109: not a coroutine deadline to be replaced by asyncio.timeout — this is
        # httpx's own transport timeout, which fails the request retryably instead of
        # cancelling the caller mid-await.
        timeout: float | None = None,  # noqa: ASYNC109
        _options: Options | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """The gateway's response object, verbatim.

        The three verbs answer the question a caller usually has. This one is for the callers
        who need what those verbs throw away — token usage, tool calls, finish reason — which
        is anything building a framework *on* a gateway rather than calling one. Everything
        else is shared: same body, same retries, same rate-limit handling.

        ``timeout`` overrides the client's default for this request alone, for callers that
        run under a deadline and must not spend more of it than they have left.
        """
        body = self._body(
            prompt,
            model=model,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            extra=extra,
        )
        return await self._post(body, timeout=timeout, options=_options)

    # ----------------------------------------------------------------- building a call

    def using(self, model: str) -> Call:
        """Start a chained call on a specific model."""
        return Call(self, model=model).using(model)

    def prompt(self, prompt_id: str, *, version: int | None = None) -> Call:
        """Start a chained call that injects a stored prompt from the repository."""
        return Call(self, model=self.model).prompt(prompt_id, version=version)

    def call(self) -> Call:
        """An empty chain, for when the first thing you set is not the model or prompt."""
        return Call(self, model=self.model)

    # ----------------------------------------------------------------- MCP tools

    async def execute_tool(
        self,
        tool_call: dict[str, Any],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - httpx transport timeout, see complete()
    ) -> dict[str, Any]:
        """Run one tool call against the gateway's MCP servers; return the turn to append.

        The gateway discovers MCP servers and injects their tools into the request on its
        own, but it deliberately does **not** run them: it hands the calls back and waits to
        be asked. That is the useful half of the bargain — the caller keeps the decision, so
        a policy check, an audit record or a human approval can sit in front of a tool that
        sends email, and none of that is possible once the gateway has already sent it.

        Takes the tool-call object straight out of a completion's ``tool_calls`` and returns
        a ``{"role": "tool", ...}`` message ready to append to the conversation, so a caller
        never has to know the MCP wire format at all.
        """
        name = (tool_call.get("function") or {}).get("name") or tool_call.get("name")
        if not name:
            raise ValueError("tool_call has no function name")
        request: dict[str, Any] = {"json": tool_call, "params": {"format": "chat"}}
        if timeout is not None:
            request["timeout"] = timeout
        try:
            response = await self._client.post("/mcp/tool/execute", **request)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Unreachable(f"gateway unreachable ({type(exc).__name__})") from exc
        if response.status_code >= _ERROR:
            raise self._error(response)
        try:
            return dict(response.json())
        except ValueError as exc:
            raise GatewayError("tool execution returned a non-JSON body") from exc

    # ----------------------------------------------------------------- lifecycle

    async def ping(self) -> bool:
        """Whether the gateway answers. Never raises."""
        try:
            response = await self._client.get("/models", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code < _SERVER_ERROR

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._owns_admin:
            await self._admin.aclose()

    async def __aenter__(self) -> Bifrost:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ----------------------------------------------------------------- internals

    def _body(
        self,
        prompt: Messages,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The request body. ``None`` means "do not send this key at all".

        The verbs supply their own defaults before calling in, so they always send a
        ``max_tokens`` and a ``temperature``. ``complete()`` does not: a caller reaching for
        the raw payload is building its own request, and having a key appear that it never
        asked for is the kind of surprise a gateway charges for.
        """
        chosen = model or self.model
        if not chosen:
            raise ValueError("no model: pass model= here or set it on the client")
        body: dict[str, Any] = {"model": chosen, "messages": _messages(prompt, system)}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        body.update(extra or {})
        if tools:
            body["tools"] = tools
        return body

    async def _post(
        self,
        body: dict[str, Any],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - httpx transport timeout, see complete()
        options: Options | None = None,
    ) -> dict[str, Any]:
        # httpx reads an explicit timeout=None as "wait forever", so the argument is omitted
        # rather than passed through when the caller has no deadline of their own.
        request: dict[str, Any] = {"json": body}
        if timeout is not None:
            request["timeout"] = timeout
        if options is not None and (extra := options.headers()):
            request["headers"] = extra
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            wait = backoff(attempt, self.backoff_seconds)
            try:
                response = await self._client.post("/chat/completions", **request)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error = Unreachable(f"gateway unreachable ({type(exc).__name__})")
            else:
                if response.status_code < _ERROR:
                    try:
                        return dict(response.json())
                    except ValueError as exc:
                        raise GatewayError("gateway returned a non-JSON body") from exc
                error = self._error(response)
                if response.status_code not in RETRYABLE:
                    raise error
                wait = getattr(error, "retry_after", None) or wait
            if attempt < self.max_retries:
                await asyncio.sleep(min(wait, 60.0))
        raise error or GatewayError("request failed")

    @staticmethod
    def _error(response: httpx.Response) -> Exception:
        # Parse the whole body, carry a bounded slice. These were one variable, and the
        # truncation silently ate the rate-limit advice for the one provider that puts it in
        # the body: Gemini's quota reply is 751 characters with "Please retry in 28.9s." at
        # index 483, so a 500-character slice cut it at "Please retry in 8" — no trailing
        # "s", no match, no delay. The client then fell back to a backoff of half a second
        # against a window of half a minute and gave up in eight, which looked from the
        # outside like a gateway that would not serve us rather than one asking us to wait.
        body = response.text
        excerpt = body[:_BODY_EXCERPT]
        if response.status_code == _RATE_LIMITED:
            return RateLimited(
                "gateway rate limited the request",
                retry_after=retry_after(response.headers, body),
                status=_RATE_LIMITED,
                body=excerpt,
            )
        return GatewayError(
            f"gateway returned {response.status_code}", status=response.status_code, body=excerpt
        )

    @staticmethod
    def _text(payload: dict[str, Any]) -> str:
        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayError("gateway returned no choices", body=str(payload)[:300]) from exc
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):  # some gateways answer with content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content and choice.get("finish_reason") == "length":
            usage = payload.get("usage") or {}
            raise EmptyResponse(
                "no content: the output budget was spent before any text was produced "
                "(raise max_tokens)",
                completion_tokens=usage.get("completion_tokens"),
                reasoning_tokens=(usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                ),
            )
        return str(content or "")


def _parse_json(text: str) -> dict[str, Any]:
    stripped = _FENCE.sub("", text.strip())
    try:
        parsed = jsonlib.loads(stripped)
    except ValueError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise InvalidJSON("response is not JSON", body=text[:300]) from None
        try:
            parsed = jsonlib.loads(stripped[start : end + 1])
        except ValueError:
            raise InvalidJSON("response is not JSON", body=text[:300]) from None
    if not isinstance(parsed, dict):
        raise InvalidJSON("response is not a JSON object", body=text[:300])
    return parsed


def _delta(data: str) -> str:
    try:
        chunk = jsonlib.loads(data)
    except ValueError:
        return ""
    choices = chunk.get("choices") or [{}]
    return str((choices[0].get("delta") or {}).get("content") or "")


__all__ = ["Bifrost", "Messages"]
