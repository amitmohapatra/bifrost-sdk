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
import random
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from bifrost_sdk._api import ManagementAPI, limits, management_client, origin, timeouts
from bifrost_sdk._breaker import Breaker
from bifrost_sdk._errors import (
    ERROR_STATUS,
    EmptyResponse,
    GatewayError,
    InvalidJSON,
    from_response,
    unreachable,
)
from bifrost_sdk._mcp import (
    CODE_MODE_META_TOOLS,
    MCP,
    MCP_ENDPOINT,
    MCPLog,
    ToolDef,
    declared_tools,
    listed_tool,
    scope,
    server_files,
)
from bifrost_sdk._retry import MAX_WAIT, RETRYABLE, Jitter, backoff
from bifrost_sdk.headers import MCP_CLIENTS, MCP_TOOLS, Options

#: A message is ``{"role": ..., "content": ...}``; a bare string is shorthand for one user turn.
Messages = str | Sequence[dict[str, Any]]

#: The gateway's maximum page size for ``GET /api/mcp-logs``.
MAX_LOG_PAGE = 1000

#: How many Code Mode declaration files are read at once. Each is one small JSON-RPC call;
#: reading them one after another made listing cost a round trip per Code Mode server, and
#: reading every one at once would let a gateway with many servers take the whole pool.
CODE_MODE_READS = 4

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)

#: On every completion: the MCP scope headers present and empty, the gateway's deny-all.
#: Without them a completion under a key with MCP access has the key's tools added to the
#: request — the model is offered tools the caller never declared (measured: "say hi" cost
#: 272 prompt tokens with one two-tool server granted, 32 under deny-all) — and the gateway's
#: agent loop runs, itself, any tool call naming a tool in a client's
#: ``tools_to_auto_execute``: a call no caller-side governance or record sees. Under deny-all
#: nothing is added and that execution is refused (logged as an error, never run). The loop
#: itself is not switched off by any header: with such a client, the refusal is fed back to
#: the model and the reply that returns is its answer to that. Keep ``tools_to_auto_execute``
#: empty (:class:`MCPClientConfig` does).
NO_GATEWAY_TOOLS = {MCP_CLIENTS: "", MCP_TOOLS: ""}


def _messages(prompt: Messages, system: str | None) -> list[dict[str, Any]]:
    turns = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
    return [{"role": "system", "content": system}, *turns] if system else turns


def _completion_headers(options: Options | None) -> dict[str, str]:
    """A completion's ``x-bf-*`` headers: ``options``, and never the gateway's MCP tools."""
    if options is not None and (options.mcp_clients or options.mcp_tools):
        raise ValueError(
            "a completion never carries the gateway's MCP tools: mcp_clients/mcp_tools scope "
            "execute_tool(); pass the tools to offer as tools="
        )
    return (options.headers() if options is not None else {}) | NO_GATEWAY_TOOLS


class Bifrost:
    """One gateway, one default model, three verbs.

    Every verb takes ``options=`` (:class:`~bifrost_sdk.headers.Options`) for per-request
    gateway behaviour, and never lets the gateway add or run MCP tools
    (:data:`NO_GATEWAY_TOOLS`): a model is offered the tools passed as ``tools=``, no others.
    Everything below the public methods exists because a real gateway did it to us: rate
    limits that carry their delay in the body, reasoning models that answer with nothing,
    and a circuit breaker that must not confuse "slow down" with "broken".
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
        circuit_failure_threshold: int = 5,
        circuit_open_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        admin_token: str | None = None,
        admin_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required (the gateway's /v1 endpoint)")
        if max_retries < 0:
            # Zero attempts would fail every call without sending it, as a "request failed"
            # that counts toward the breaker.
            raise ValueError("max_retries must be 0 or more")
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        #: Jitter for the retry backoff; a seeded ``random.Random`` makes a schedule
        #: reproducible.
        self._rng: Jitter = random.Random()
        #: Retries handle one bad call; the breaker handles a bad gateway. Without it an
        #: outage costs one full timeout *per request* — with it, one in total. Pass
        #: ``circuit_failure_threshold=0`` to turn it off and see every failure yourself.
        self._breaker = Breaker(circuit_failure_threshold, circuit_open_seconds)
        headers = {"Content-Type": "application/json"}
        if api_key:
            # A gateway virtual key, never a provider key: the provider's credential stays in
            # the gateway, which is the whole reason for having one.
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeouts(timeout),
            limits=limits(),
        )
        self._owns_client = client is None
        #: ``/api/*`` (MCP listing, logs, client CRUD) authenticates with a bearer token,
        #: defaulting to the virtual key.
        self._admin = admin_client or management_client(base_url, admin_token or api_key, timeout)
        self._owns_admin = admin_client is None
        self._api = ManagementAPI(self._admin)
        self.mcp = MCP(self._api)

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
        options: Options | None = None,
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
        return self._text(await self._post(body, options=options))

    async def json(
        self,
        prompt: Messages,
        *,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        options: Options | None = None,
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
        return _parse_json(self._text(await self._post(body, options=options)))

    async def stream(
        self,
        prompt: Messages,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        options: Options | None = None,
        **extra: Any,
    ) -> AsyncIterator[str]:
        """Text deltas, yielded as they arrive.

        Nothing is buffered: time-to-first-token is the point of streaming, and collecting the
        whole answer before yielding would throw it away. Streams are not retried — a partly
        consumed stream cannot be replayed without showing the caller duplicate text — but
        they do use the breaker: an open circuit raises :class:`CircuitOpen` before anything
        is sent, an accepted stream counts as a success, and a failure to start (or a
        connection lost mid-stream) counts as the completions' failures do. An error the
        gateway reports inside the stream is raised as a :class:`GatewayError` rather than
        ending the text early as though it were complete.
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
        headers = _completion_headers(options)
        self._breaker.check()
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=body, headers=headers
            ) as response:
                if response.status_code >= ERROR_STATUS:
                    await response.aread()
                    error = from_response(response)
                    self._breaker.record_failure(error)
                    raise error
                self._breaker.record_success()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    delta = _delta(data)
                    if delta:
                        yield delta
        except httpx.TransportError as exc:
            failure = unreachable(exc)
            self._breaker.record_failure(failure)
            raise failure from exc

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
        options: Options | None = None,
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
        return await self._post(body, timeout=timeout, options=options)

    # ----------------------------------------------------------------- MCP tools

    async def tools(
        self,
        clients: Iterable[str] | None = None,
        only: Iterable[str] | None = None,
        *,
        slug: str | None = None,
    ) -> list[ToolDef]:
        """The MCP tools a request scoped this way could execute, as the gateway lists them
        for this client's virtual key.

        ``slug`` lists one Virtual MCP (``admin.virtual_mcps``) or one MCP client's own
        endpoint, ``/mcp/<slug>``, instead of everything the key reaches (a slug the key is
        not attached to is a :class:`PermissionDeniedError`).

        ``clients`` and ``only`` mean what ``Options(mcp_clients=, mcp_tools=)`` mean on a
        request: ``None`` is unscoped, an empty sequence is nothing; tools are named
        ``<client>-<tool>`` or ``<client>-*``. The listing is the gateway's own MCP endpoint
        (``POST /mcp``, JSON-RPC ``tools/list``) asked with the virtual key — never ``/api``,
        which admin auth closes to a virtual key — so it holds exactly what the key allows,
        with the servers' annotations. A Code Mode client's tools are not in it (its
        meta-tools are): they are read from the meta-tools' declarations instead, marked
        ``code_mode``, with signature-derived ``parameters`` and no ``annotations``.
        """
        admits = scope(clients, only)
        return [tool for tool in await self._listed_tools(_endpoint(slug)) if admits(tool)]

    async def _listed_tools(self, endpoint: str) -> list[ToolDef]:
        result = await self._rpc("tools/list", {}, endpoint=endpoint)
        listed = [t for t in result.get("tools") or () if isinstance(t, dict) and "name" in t]
        tools = [listed_tool(t) for t in listed if t["name"] not in CODE_MODE_META_TOOLS]
        if any(t["name"] in CODE_MODE_META_TOOLS for t in listed):
            clients = server_files(await self._meta_text("listToolFiles", {}, endpoint))
            texts = await self._declarations(clients, endpoint)
            for client, text in zip(clients, texts, strict=True):
                tools.extend(declared_tools(client, text))
        return tools

    async def _declarations(self, clients: list[str], endpoint: str) -> list[str]:
        """Each Code Mode client's ``readToolFile`` text, in ``clients`` order.

        Read :data:`CODE_MODE_READS` at a time. Every read is awaited before any failure is
        raised, and the one raised is the first *in order*, so the listing fails the same way
        however the reads interleave — and no read is left running unobserved behind it.
        """
        gate = asyncio.Semaphore(CODE_MODE_READS)

        async def read(client: str) -> str:
            async with gate:
                arguments = {"fileName": f"servers/{client}.pyi"}
                return await self._meta_text("readToolFile", arguments, endpoint)

        results = await asyncio.gather(*(read(c) for c in clients), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return [str(result) for result in results]

    async def _meta_text(self, name: str, arguments: dict[str, Any], endpoint: str) -> str:
        """A Code Mode meta-tool's text answer, called through the gateway's MCP endpoint."""
        params = {"name": name, "arguments": arguments}
        result = await self._rpc("tools/call", params, endpoint=endpoint)
        if result.get("isError"):
            raise GatewayError(f"{name} failed", body=str(result.get("content"))[:300])
        return _text(result)

    async def _rpc(
        self, method: str, params: dict[str, Any], *, endpoint: str, **request: Any
    ) -> dict[str, Any]:
        """One JSON-RPC call to one of the gateway's MCP endpoints, with the virtual key.
        ``request`` is passed to httpx (``headers``, ``timeout``)."""
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        url = origin(str(self._client.base_url)) + endpoint
        try:
            response = await self._client.post(url, json=message, **request)
        except httpx.TransportError as exc:
            raise unreachable(exc) from exc
        if response.status_code >= ERROR_STATUS:
            raise from_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(f"MCP {method} returned a non-JSON body") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            error = payload.get("error") if isinstance(payload, dict) else None
            raise GatewayError(f"MCP {method} failed: {error}", body=response.text[:300])
        return payload["result"]

    async def mcp_logs(
        self, since: datetime, limit: int = 100, parent_request_id: str | None = None
    ) -> list[MCPLog]:
        """MCP tool executions logged at or after ``since``, oldest first.

        ``parent_request_id`` narrows to executions made under that
        ``Options(parent_request_id=)`` — for Code Mode, the nested calls of one script.
        Logs are written asynchronously (a few seconds behind), and ``since`` is inclusive:
        a caller paging forward dedups by :attr:`MCPLog.id`.
        """
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        if not 1 <= limit <= MAX_LOG_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_LOG_PAGE}")
        params: dict[str, Any] = {
            "start_time": since.isoformat(),
            "limit": limit,
            "sort_by": "timestamp",
            "order": "asc",
        }
        if parent_request_id is not None:
            params["llm_request_ids"] = parent_request_id
        entries = await self._api.items("/api/mcp-logs", ("logs",), **params)
        return [MCPLog._from_gateway(entry) for entry in entries]

    async def execute_tool(
        self,
        tool_call: dict[str, Any],
        *,
        options: Options | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - httpx transport timeout, see complete()
        slug: str | None = None,
    ) -> dict[str, Any]:
        """Run one MCP tool call through the gateway; return the ``{"role": "tool"}`` turn
        (``"is_error": True`` when the tool failed).

        ``tool_call`` is an entry of a completion's ``tool_calls`` (chat format), its name
        ``<client>-<tool>`` or a Code Mode meta-tool. ``options`` scopes it
        (``mcp_clients``/``mcp_tools``), correlates it (``parent_request_id``: Code Mode's
        nested calls are logged under it, see :meth:`mcp_logs`) and says who it is for: the
        gateway forwards to the server any ``extra`` header the client's
        ``allowed_extra_headers`` names, and keys per-user MCP credentials by the virtual key
        or ``mcp_session_id``. ``slug`` runs it through ``/mcp/<slug>`` — a Virtual MCP or one
        client's endpoint — where only that bundle's tools are permitted. Not retried and not
        counted by the breaker: a tool may have side effects, and a refused call is a 400 (an
        error turn through a slug).
        """
        function = tool_call.get("function") or {}
        if not function.get("name"):
            raise ValueError("tool_call has no function name")
        request: dict[str, Any] = {}
        if timeout is not None:
            request["timeout"] = timeout
        if options is not None:
            request["headers"] = options.headers()
        if slug is not None:
            params = {"name": function["name"], "arguments": _arguments(function)}
            result = await self._rpc("tools/call", params, endpoint=_endpoint(slug), **request)
            turn = {"role": "tool", "content": _text(result), "tool_call_id": tool_call.get("id")}
            return turn | ({"is_error": True} if result.get("isError") else {})
        request |= {"json": tool_call, "params": {"format": "chat"}}
        try:
            response = await self._client.post("/mcp/tool/execute", **request)
        except httpx.TransportError as exc:
            raise unreachable(exc) from exc
        if response.status_code >= ERROR_STATUS:
            raise from_response(response)
        return _json_object(response, "tool execution")

    # ----------------------------------------------------------------- lifecycle

    async def ping(self) -> bool:
        """Whether this is a gateway we can actually use. Never raises.

        Success, not merely "answered". It used to accept anything below 500, which makes
        the check unable to fail in the one case it exists for: a ``base_url`` pointing at
        something that is not this gateway. That is not hypothetical — the memory service's
        default was ``localhost:8090/v1``, and on the machine this was written on a
        *different* service held port 8090 and answered 404, so a misconfigured deployment
        reported its model dependency healthy and failed on every actual call.

        A 401 or 404 on ``/models`` means the same request to ``/chat/completions``, which
        is all this client ever sends, will not work either. Reporting that as up is worse
        than reporting nothing.
        """
        try:
            response = await self._client.get("/models", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.is_success

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
        request: dict[str, Any] = {"json": body, "headers": _completion_headers(options)}
        if timeout is not None:
            request["timeout"] = timeout
        self._breaker.check()
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            wait = backoff(attempt, self.backoff_seconds, self._rng)
            try:
                response = await self._client.post("/chat/completions", **request)
            except httpx.TransportError as exc:
                error = unreachable(exc)
            else:
                if response.status_code < ERROR_STATUS:
                    try:
                        payload = _json_object(response, "gateway")
                    except GatewayError as exc:
                        # A 200 the caller cannot use is still the gateway misbehaving.
                        self._breaker.record_failure(exc)
                        raise
                    self._breaker.record_success()
                    return payload
                error = from_response(response)
                if response.status_code not in RETRYABLE:
                    # Recorded, but a 4xx does not count: the breaker decides (_breaker.counts).
                    self._breaker.record_failure(error)
                    raise error
                # A delay the gateway asked for is honoured as given — no jitter: it already
                # knows when it will have room — up to MAX_WAIT.
                wait = getattr(error, "retry_after", None) or wait
            if attempt < self.max_retries:
                await asyncio.sleep(min(wait, MAX_WAIT))
        # The retries are spent. This counts once, not once per attempt: the breaker
        # measures failed *calls*, and a threshold of 5 would otherwise open after two.
        self._breaker.record_failure(error)
        raise error or GatewayError("request failed")

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


def _endpoint(slug: str | None) -> str:
    """The gateway's MCP endpoint: everything the key reaches, or the one ``/mcp/<slug>``."""
    return MCP_ENDPOINT if slug is None else f"{MCP_ENDPOINT}/{quote(slug, safe='')}"


def _arguments(function: dict[str, Any]) -> dict[str, Any]:
    """A chat tool call's arguments (a JSON object, usually as text) as an object."""
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = jsonlib.loads(arguments)
        except ValueError as exc:
            raise ValueError("tool_call arguments are not JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("tool_call arguments are not a JSON object")
    return arguments


def _text(result: dict[str, Any]) -> str:
    """An MCP ``tools/call`` result's text parts, joined by newlines; other parts dropped."""
    content = result.get("content") or ()
    return "\n".join(str(c["text"]) for c in content if isinstance(c, dict) and c.get("text"))


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


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    """A success response's body, which must be one JSON object."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise GatewayError(f"{what} returned a non-JSON body", body=response.text[:300]) from exc
    if not isinstance(payload, dict):
        raise GatewayError(f"{what} returned JSON that is not an object", body=response.text[:300])
    return payload


def _delta(data: str) -> str:
    """One ``data:`` chunk's text; ``""`` for anything that is not text.

    A chunk carrying ``error`` (and no choices) is the gateway reporting a failure after the
    stream began — the provider dropped, a filter tripped — and is raised: skipping it made a
    truncated answer look like a finished one.
    """
    try:
        chunk = jsonlib.loads(data)
    except ValueError:
        return ""
    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices")
    if not choices and chunk.get("error"):
        raise GatewayError("gateway reported an error mid-stream", body=data[:300])
    first = choices[0] if isinstance(choices, list) and choices else None
    delta = first.get("delta") if isinstance(first, dict) else None
    return str(delta.get("content") or "") if isinstance(delta, dict) else ""


__all__ = ["Bifrost", "Messages"]
