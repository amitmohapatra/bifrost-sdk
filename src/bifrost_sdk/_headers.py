"""Bifrost's per-request control plane, which is its headers.

Almost everything the gateway can do *for one call* — inject a stored prompt, narrow which
MCP servers are visible, attribute spend to a customer, reuse a semantic cache entry, keep a
payload out of the logs — is selected with an ``x-bf-*`` header rather than a body field.
That is a good design in a gateway and a miserable one to use from an application: the names
are easy to misspell, a typo is silently ignored, and nothing tells you the set exists.

So the names live here once, and :class:`Options` is what callers actually build.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

#: Governance identity. Budgets, rate limits and model allow-lists are attached to this.
VIRTUAL_KEY = "x-bf-vk"

#: Stored-prompt injection. The gateway prepends the prompt's messages to the request.
PROMPT_ID = "x-bf-prompt-id"
PROMPT_VERSION = "x-bf-prompt-version"

#: MCP scoping. Without these a request sees every tool the gateway has, which is both a
#: token cost on every call and a blast radius no one chose.
MCP_CLIENTS = "x-bf-mcp-include-clients"
MCP_TOOLS = "x-bf-mcp-include-tools"
MCP_SESSION = "x-bf-mcp-session-id"

#: Conversation identity, for session-aware tools and for grouping logs.
SESSION = "x-bf-session-id"

#: Semantic cache control.
CACHE_KEY = "x-bf-cache-key"
CACHE_TYPE = "x-bf-cache-type"
CACHE_THRESHOLD = "x-bf-cache-threshold"

#: Spend attribution.
CUSTOMER_ID = "x-bf-customer-id"
CUSTOMER_NAME = "x-bf-customer-name"
PROJECT_ID = "x-bf-project-id"

#: Privacy. The gateway logs request and response content by default.
NO_CONTENT_LOGGING = "x-bf-disable-content-logging"

#: Prefix for arbitrary reporting dimensions (``x-bf-dim-team: payments``).
DIMENSION_PREFIX = "x-bf-dim-"


def _join(values: Iterable[str]) -> str:
    return ",".join(str(v) for v in values)


@dataclass(frozen=True, slots=True)
class Options:
    """Per-request gateway options, rendered to headers on the way out.

    Frozen, because these are built up by a fluent chain: every ``with_*`` returns a new one
    rather than mutating a shared object, so a half-configured call cannot leak into the next
    one through an alias.
    """

    virtual_key: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    mcp_clients: tuple[str, ...] = ()
    mcp_tools: tuple[str, ...] = ()
    mcp_session_id: str | None = None
    session_id: str | None = None
    cache_key: str | None = None
    cache_type: str | None = None
    cache_threshold: float | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    project_id: str | None = None
    content_logging: bool = True
    dimensions: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, str] = field(default_factory=dict)

    def merged(self, **changes: Any) -> Options:
        """A copy with ``changes`` applied. Sequences are replaced, not appended to."""
        return replace(self, **changes)

    def headers(self) -> dict[str, str]:
        """The ``x-bf-*`` headers this selection means. Unset options send nothing.

        Table-driven rather than a chain of ``if``: adding a gateway option should be one
        line here, and a reader should be able to see the whole control plane at once.
        """
        simple: tuple[tuple[str, Any], ...] = (
            (VIRTUAL_KEY, self.virtual_key),
            (MCP_SESSION, self.mcp_session_id),
            (SESSION, self.session_id),
            (CACHE_KEY, self.cache_key),
            (CACHE_TYPE, self.cache_type),
            (CACHE_THRESHOLD, self.cache_threshold),
            (CUSTOMER_ID, self.customer_id),
            (CUSTOMER_NAME, self.customer_name),
            (PROJECT_ID, self.project_id),
            (MCP_CLIENTS, _join(self.mcp_clients) or None),
            (MCP_TOOLS, _join(self.mcp_tools) or None),
        )
        out = {name: str(value) for name, value in simple if value is not None}
        if self.prompt_id:
            # The version is meaningless without the id, so they travel together: sending a
            # version alone silently selects nothing and looks like the prompt was ignored.
            out[PROMPT_ID] = self.prompt_id
            if self.prompt_version is not None:
                out[PROMPT_VERSION] = str(self.prompt_version)
        if not self.content_logging:
            # Present-and-true is what the gateway looks for; absent means "log as usual".
            out[NO_CONTENT_LOGGING] = "true"
        out.update({f"{DIMENSION_PREFIX}{k}": str(v) for k, v in self.dimensions.items()})
        out.update(self.extra)
        return out
