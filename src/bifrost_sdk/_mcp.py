"""MCP clients registered in the gateway, and the tools they expose.

Gateway facts this module encodes (verified against a running gateway):

* A tool is addressed as ``<client>-<tool>`` everywhere a request names it (execution,
  ``x-bf-mcp-include-tools``); ``GET /api/mcp/clients`` lists tools by their bare name.
* A client's ``tools_to_execute`` is enforced at execution: ``["*"]`` allows all, an empty
  list allows none. The listing still shows every discovered tool.
* ``PUT /api/mcp/client/{id}`` answers 200 but ignores connection changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bifrost_sdk._api import Resource

ConnectionType = Literal["http", "sse", "stdio", "inprocess"]
#: Between a client's name and its tool's name, in every name a request carries.
TOOL_SEPARATOR = "-"
#: Allows every tool (``tools_to_execute``) or every client (``x-bf-mcp-include-clients``).
ALL = "*"
#: The gateway's maximum page size for ``GET /api/mcp/clients``.
_PAGE = 100


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MCPConnection(_Frozen):
    """How the gateway reaches the server. Only ``http``/``sse`` can be registered here."""

    type: ConnectionType
    url: str | None = None


class MCPClientConfig(_Frozen):
    """An MCP client's configuration.

    ``tools_to_execute`` has no default on purpose: an empty list lets no tool run, so the
    choice between ``["*"]`` and nothing must be visible at the call site.
    ``tools_to_auto_execute`` is Agent Mode (the gateway runs tools itself); Trellis never
    uses it and always sends it empty unless told otherwise.
    """

    name: str = Field(min_length=1)
    connection: MCPConnection
    tools_to_execute: tuple[str, ...]
    tools_to_auto_execute: tuple[str, ...] = ()
    is_code_mode_client: bool = False

    @field_validator("name")
    @classmethod
    def _no_separator(cls, name: str) -> str:
        if TOOL_SEPARATOR in name:
            raise ValueError(f"an MCP client name may not contain {TOOL_SEPARATOR!r}")
        return name

    def _mutable(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_code_mode_client": self.is_code_mode_client,
            "tools_to_execute": list(self.tools_to_execute),
            "tools_to_auto_execute": list(self.tools_to_auto_execute),
        }

    @classmethod
    def _from_gateway(cls, config: dict[str, Any]) -> MCPClientConfig:
        url = config.get("connection_string")
        if isinstance(url, dict):  # an env-var wrapper: {"value": ..., "type": "plain_text"}
            url = url.get("value")
        return cls(
            name=config["name"],
            connection=MCPConnection(type=config["connection_type"], url=url or None),
            tools_to_execute=tuple(config.get("tools_to_execute") or ()),
            tools_to_auto_execute=tuple(config.get("tools_to_auto_execute") or ()),
            is_code_mode_client=bool(config.get("is_code_mode_client")),
        )


class ToolDef(_Frozen):
    """One MCP tool, named as requests name it: ``<client>-<tool>``."""

    name: str
    client: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: The tool's client is a Code Mode client: completions see the Code Mode meta-tools
    #: (``listToolFiles``, ``readToolFile``, ``getToolDocs``, ``executeToolCode``) instead.
    code_mode: bool = False


class MCPClient(_Frozen):
    """A registered client as the gateway reports it."""

    id: str
    config: MCPClientConfig
    state: str
    disabled: bool = False
    #: Every tool the server exposes, whether or not ``tools_to_execute`` lets it run.
    tools: tuple[ToolDef, ...] = ()

    @property
    def executable(self) -> tuple[ToolDef, ...]:
        """The tools this client's ``tools_to_execute`` lets run."""
        allowed = self.config.tools_to_execute
        if ALL in allowed:
            return self.tools
        prefix = self.config.name + TOOL_SEPARATOR
        return tuple(t for t in self.tools if t.name.removeprefix(prefix) in allowed)

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> MCPClient:
        raw = entry["config"]
        config = MCPClientConfig._from_gateway(raw)
        return cls(
            id=raw["client_id"],
            config=config,
            state=str(entry.get("state") or ""),
            disabled=bool(raw.get("disabled")),
            tools=tuple(
                ToolDef(
                    name=f"{config.name}{TOOL_SEPARATOR}{tool['name']}",
                    client=config.name,
                    description=str(tool.get("description") or ""),
                    parameters=dict(tool.get("parameters") or {}),
                    code_mode=config.is_code_mode_client,
                )
                for tool in entry.get("tools") or ()
            ),
        )


def scope(clients: Iterable[str] | None, only: Iterable[str] | None) -> Callable[[ToolDef], bool]:
    """What ``x-bf-mcp-include-clients`` / ``-tools`` with these values admit.

    ``None`` is an absent header (no restriction); an empty sequence admits nothing. Clients
    match by name or ``*``; tools by exact ``<client>-<tool>`` or ``<client>-*``.
    """
    client_set = None if clients is None else frozenset(clients)
    tool_set = None if only is None else frozenset(only)

    def admits(tool: ToolDef) -> bool:
        if client_set is not None and not ({tool.client, ALL} & client_set):
            return False
        wildcard = f"{tool.client}{TOOL_SEPARATOR}{ALL}"
        return tool_set is None or bool({tool.name, wildcard} & tool_set)

    return admits


class MCP(Resource):
    """``bf.mcp`` — list and manage the gateway's MCP clients."""

    async def clients(self) -> list[MCPClient]:
        """Every registered client, across all pages."""
        found: list[MCPClient] = []
        while True:
            page = await self._api.items(
                "/api/mcp/clients", ("clients",), limit=_PAGE, offset=len(found)
            )
            found.extend(MCPClient._from_gateway(entry) for entry in page)
            if len(page) < _PAGE:
                return found

    async def add(self, config: MCPClientConfig) -> None:
        """Register a client. Refused by the gateway for private-network targets unless the
        caller is authenticated."""
        if config.connection.type not in ("http", "sse") or not config.connection.url:
            raise ValueError("only http/sse clients with a url can be registered")
        await self._api.post(
            "/api/mcp/client",
            {
                **config._mutable(),
                "connection_type": config.connection.type,
                "connection_string": config.connection.url,
            },
        )

    async def update(self, current: MCPClient, desired: MCPClientConfig) -> None:
        """Change a client's name, tool allow-lists or Code Mode flag.

        The gateway silently ignores connection changes, so a different ``connection`` is
        refused here: remove the client and add it again.
        """
        if desired.connection != current.config.connection:
            raise ValueError(
                f"MCP client {current.config.name!r}: the connection cannot be updated; "
                "remove and add the client"
            )
        await self._api.put(f"/api/mcp/client/{current.id}", desired._mutable())

    async def remove(self, client_id: str) -> None:
        await self._api.delete(f"/api/mcp/client/{client_id}")
