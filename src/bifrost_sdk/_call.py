"""A call being described, before it is sent.

The flat verbs on :class:`~bifrost_sdk._client.Bifrost` answer the common question in one line.
This is for the other case: a request assembled from several places at once — a stored
prompt, a scoped set of MCP servers, a session, a customer to bill, a model chosen at
runtime. Written as keyword arguments that is a wall of ``None``; written as a chain it
reads as the decision it is::

    answer = await (
        bf.prompt(PROMPT_ID, version=3)
          .mcp(clients=["memory"])
          .session(thread_id)
          .customer(id="acme")
          .using("gemini/gemini-3.6-flash")
          .chat("what changed since Friday?")
    )

Every step returns a **new** ``Call``. A half-built chain is therefore safe to keep as a
template and finish differently in two places, which is what a harness does when one agent
definition serves many turns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from bifrost_sdk._headers import Options

if TYPE_CHECKING:  # pragma: no cover
    from bifrost_sdk._client import Bifrost, Messages


class Call:
    """An immutable, partially-specified request. Terminal verbs send it."""

    __slots__ = ("_client", "_model", "_options", "_params")

    def __init__(
        self,
        client: Bifrost,
        *,
        options: Options | None = None,
        model: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._options = options or Options()
        self._model = model
        self._params: dict[str, Any] = dict(params or {})

    # ------------------------------------------------------------------ chaining
    def _with(
        self, *, options: Options | None = None, model: str | None = None, **params: Any
    ) -> Call:
        return Call(
            self._client,
            options=options or self._options,
            model=model or self._model,
            params={**self._params, **params},
        )

    def using(self, model: str) -> Call:
        """Choose the model for this call."""
        return self._with(model=model)

    def prompt(self, prompt_id: str, *, version: int | None = None) -> Call:
        """Inject a stored prompt from the gateway's repository."""
        return self._with(options=self._options.merged(prompt_id=prompt_id, prompt_version=version))

    def mcp(
        self, *, clients: Sequence[str] | None = None, tools: Sequence[str] | None = None
    ) -> Call:
        """Narrow which MCP servers and tools this call may see.

        Worth doing on every call that uses MCP at all: unscoped, the gateway offers every
        tool it knows, which is paid for in prompt tokens on each request and grants reach
        nobody chose.
        """
        return self._with(
            options=self._options.merged(
                mcp_clients=tuple(clients or self._options.mcp_clients),
                mcp_tools=tuple(tools or self._options.mcp_tools),
            )
        )

    def session(self, session_id: str, *, mcp_session_id: str | None = None) -> Call:
        """Tie the call to a conversation, for session-aware tools and log grouping."""
        return self._with(
            options=self._options.merged(
                session_id=session_id, mcp_session_id=mcp_session_id or self._options.mcp_session_id
            )
        )

    def customer(self, *, id: str | None = None, name: str | None = None) -> Call:
        """Attribute spend. ``id`` is what budgets and reports are keyed on."""
        return self._with(options=self._options.merged(customer_id=id, customer_name=name))

    def project(self, project_id: str) -> Call:
        return self._with(options=self._options.merged(project_id=project_id))

    def cache(
        self,
        *,
        key: str | None = None,
        kind: str | None = None,
        threshold: float | None = None,
    ) -> Call:
        """Semantic cache controls for this call. ``kind`` is the gateway's cache type."""
        return self._with(
            options=self._options.merged(cache_key=key, cache_type=kind, cache_threshold=threshold)
        )

    def private(self) -> Call:
        """Keep this request and its response out of the gateway's content logs.

        For turns carrying personal data, credentials or anything under a retention rule.
        The gateway logs content by default, so this is opt-out and has to be deliberate.
        """
        return self._with(options=self._options.merged(content_logging=False))

    def dimensions(self, **values: str) -> Call:
        """Reporting dimensions (``team="payments"``) attached to this call's telemetry."""
        return self._with(
            options=self._options.merged(dimensions={**self._options.dimensions, **values})
        )

    def header(self, **values: str) -> Call:
        """Arbitrary request headers — including the ones routing rules match on.

        The gateway's routing rules are CEL expressions evaluated against ``headers`` and
        ``params`` maps that the *caller* fills in, alongside ``model``, ``provider``,
        ``virtual_key_id``, ``team_id``, ``budget_used`` and a lazily-computed
        ``complexity_tier``. So a rule like::

            headers["x-tier"] == "batch" && complexity_tier == "low"

        is steered from here. Prefer :meth:`dimensions` when the value is also something you
        want to report on; use this when it is purely a routing signal.
        """
        return self._with(options=self._options.merged(extra={**self._options.extra, **values}))

    def key(self, virtual_key: str) -> Call:
        """Use a specific virtual key — the identity budgets and limits hang off."""
        return self._with(options=self._options.merged(virtual_key=virtual_key))

    def options(self, **params: Any) -> Call:
        """Anything else that belongs in the request body (``temperature``, ``top_p``…)."""
        return self._with(**params)

    # ------------------------------------------------------------------ terminals
    async def chat(self, prompt: Messages, **kwargs: Any) -> str:
        return await self._client.chat(prompt, **self._send(kwargs))

    async def json(self, prompt: Messages, **kwargs: Any) -> dict[str, Any]:
        return await self._client.json(prompt, **self._send(kwargs))

    async def complete(self, prompt: Messages, **kwargs: Any) -> dict[str, Any]:
        return await self._client.complete(prompt, **self._send(kwargs))

    def stream(self, prompt: Messages, **kwargs: Any) -> AsyncIterator[str]:
        return self._client.stream(prompt, **self._send(kwargs))

    # ------------------------------------------------------------------ internals
    def _send(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        """What the terminal verb passes through to the client."""
        sent: dict[str, Any] = {**self._params, **overrides}
        if self._model and "model" not in sent:
            sent["model"] = self._model
        sent["_options"] = self._options
        return sent

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        set_headers = sorted(self._options.headers())
        return f"Call(model={self._model!r}, headers={set_headers})"


__all__ = ["Call"]
