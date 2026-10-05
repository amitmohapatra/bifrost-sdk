"""Bifrost's per-request control plane: the ``x-bf-*`` headers, and :class:`Options`.

Almost everything the gateway does *for one call* — inject a stored prompt, narrow which MCP
servers are visible, attribute spend, reuse a semantic cache entry, keep a payload out of the
logs — is selected by a header. A misspelt header is silently ignored, so the names live here
once and callers build :class:`Options` instead of writing them by hand.
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

#: MCP scoping. Absent means "everything the virtual key allows"; present and empty means
#: "nothing". Clients are listed by name, tools as ``<client>-<tool>`` or ``<client>-*``.
MCP_CLIENTS = "x-bf-mcp-include-clients"
MCP_TOOLS = "x-bf-mcp-include-tools"
MCP_SESSION = "x-bf-mcp-session-id"

#: Correlation. Tool executions made under this header are logged with it as their
#: ``llm_request_id``, which is how Code Mode's nested calls are found again in the MCP logs.
PARENT_REQUEST_ID = "x-bf-parent-request-id"

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


def _scope(values: Iterable[str] | None) -> tuple[str, ...] | None:
    return None if values is None else tuple(values)


@dataclass(frozen=True, slots=True)
class Options:
    """Per-request gateway options, rendered to ``x-bf-*`` headers. Unset options send nothing.

    ``mcp_clients`` / ``mcp_tools``: ``None`` leaves MCP unscoped, an empty sequence denies
    every client or tool (the header is sent empty — the gateway's deny-all). They scope
    ``execute_tool``: a completion always sends both empty and refuses any other scope.
    ``mcp_session_id`` names the caller for per-user MCP credentials when the virtual key
    does not; a per-user credential header the server reads goes in ``extra``.
    ``extra`` holds any other header, e.g. one a routing rule matches on.
    """

    virtual_key: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    mcp_clients: Iterable[str] | None = None
    mcp_tools: Iterable[str] | None = None
    mcp_session_id: str | None = None
    parent_request_id: str | None = None
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

    def __post_init__(self) -> None:
        # Normalised to tuples so a caller's list cannot be mutated behind a frozen object.
        object.__setattr__(self, "mcp_clients", _scope(self.mcp_clients))
        object.__setattr__(self, "mcp_tools", _scope(self.mcp_tools))

    def merged(self, **changes: Any) -> Options:
        """A copy with ``changes`` applied. Sequences and mappings are replaced, not merged."""
        return replace(self, **changes)

    def headers(self) -> dict[str, str]:
        """The headers this selection means."""
        simple: tuple[tuple[str, Any], ...] = (
            (VIRTUAL_KEY, self.virtual_key),
            (MCP_SESSION, self.mcp_session_id),
            (PARENT_REQUEST_ID, self.parent_request_id),
            (SESSION, self.session_id),
            (CACHE_KEY, self.cache_key),
            (CACHE_TYPE, self.cache_type),
            (CACHE_THRESHOLD, self.cache_threshold),
            (CUSTOMER_ID, self.customer_id),
            (CUSTOMER_NAME, self.customer_name),
            (PROJECT_ID, self.project_id),
        )
        out = {name: str(value) for name, value in simple if value is not None}
        for name, scope in ((MCP_CLIENTS, self.mcp_clients), (MCP_TOOLS, self.mcp_tools)):
            if scope is not None:
                out[name] = ",".join(scope)
        if self.prompt_id:
            # A version without its id selects nothing and looks like the prompt was ignored.
            out[PROMPT_ID] = self.prompt_id
            if self.prompt_version is not None:
                out[PROMPT_VERSION] = str(self.prompt_version)
        if not self.content_logging:
            out[NO_CONTENT_LOGGING] = "true"
        out.update({f"{DIMENSION_PREFIX}{k}": str(v) for k, v in self.dimensions.items()})
        out.update(self.extra)
        return out


__all__ = [
    "CACHE_KEY",
    "CACHE_THRESHOLD",
    "CACHE_TYPE",
    "CUSTOMER_ID",
    "CUSTOMER_NAME",
    "DIMENSION_PREFIX",
    "MCP_CLIENTS",
    "MCP_SESSION",
    "MCP_TOOLS",
    "NO_CONTENT_LOGGING",
    "PARENT_REQUEST_ID",
    "PROJECT_ID",
    "PROMPT_ID",
    "PROMPT_VERSION",
    "SESSION",
    "VIRTUAL_KEY",
    "Options",
]
