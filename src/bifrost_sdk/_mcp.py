"""MCP clients registered in the gateway, and the tools they expose.

Gateway facts this module encodes (verified against a running gateway):

* A tool is addressed as ``<client>-<tool>`` everywhere a request names it (execution,
  ``x-bf-mcp-include-tools``); ``GET /api/mcp/clients`` lists tools by their bare name.
* A client's ``tools_to_execute`` is enforced at execution: ``["*"]`` allows all, an empty
  list allows none. The listing still shows every discovered tool.
* ``PUT /api/mcp/client/{id}`` answers 200 but ignores connection changes.
* ``GET /api/mcp/clients`` drops the servers' MCP tool annotations; the gateway's own MCP
  endpoint (``POST /mcp``, JSON-RPC ``tools/list``) keeps them, keyed ``<client>-<tool>``.
* ``POST /mcp`` lists only what the calling virtual key's MCP allow-list admits (a key with
  no MCP configuration sees no tools); without a key it lists every executable tool. It is
  the one listing a virtual key may ask for: with admin auth on, ``/api/*`` refuses it (401).
* For a Code Mode client, ``tools/list`` shows the meta-tools (``listToolFiles``,
  ``readToolFile``, ``getToolDocs``, ``executeToolCode``) instead of its tools. The tools
  themselves are only in the meta-tools' answers: ``listToolFiles`` names one
  ``servers/<client>.pyi`` per Code Mode client, and ``readToolFile`` of it holds one
  ``def <tool>(<param>: <type>, ...) -> dict:  # <description>`` line per tool — no JSON
  schema and no annotations.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bifrost_sdk._api import Resource
from bifrost_sdk._errors import GatewayError

ConnectionType = Literal["http", "sse", "stdio", "inprocess"]
#: Between a client's name and its tool's name, in every name a request carries.
TOOL_SEPARATOR = "-"
#: Allows every tool (``tools_to_execute``) or every client (``x-bf-mcp-include-clients``).
ALL = "*"
#: The gateway's maximum page size for ``GET /api/mcp/clients``.
_PAGE = 100
#: The gateway's own MCP server (a sibling of ``/v1``): the one listing a virtual key may ask
#: for, and the one that carries tool annotations.
MCP_ENDPOINT = "/mcp"


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
    ``allowed_extra_headers`` are the request headers the gateway forwards to this server
    when it runs a tool — how a per-user credential (``Options(extra={...})``) reaches a
    server that authenticates its caller; ``("*",)`` forwards any.
    """

    name: str = Field(min_length=1)
    connection: MCPConnection
    tools_to_execute: tuple[str, ...]
    tools_to_auto_execute: tuple[str, ...] = ()
    is_code_mode_client: bool = False
    allowed_extra_headers: tuple[str, ...] = ()

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
            "allowed_extra_headers": list(self.allowed_extra_headers),
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
            allowed_extra_headers=tuple(config.get("allowed_extra_headers") or ()),
        )


class ToolAnnotations(BaseModel):
    """The MCP server's hints about a tool (MCP ``ToolAnnotations``); each is ``None`` when the
    server did not say. Hints, not guarantees: a server can mislabel its tools. Fields are the
    MCP names in snake_case; the wire (camelCase) names parse too. Other keys (``title``) are
    ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    read_only_hint: bool | None = Field(default=None, alias="readOnlyHint")
    destructive_hint: bool | None = Field(default=None, alias="destructiveHint")
    idempotent_hint: bool | None = Field(default=None, alias="idempotentHint")
    open_world_hint: bool | None = Field(default=None, alias="openWorldHint")


class ToolDef(_Frozen):
    """One MCP tool, named as requests name it: ``<client>-<tool>``."""

    name: str
    client: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: The tool's client is a Code Mode client: completions see the Code Mode meta-tools
    #: (``listToolFiles``, ``readToolFile``, ``getToolDocs``, ``executeToolCode``) instead.
    code_mode: bool = False
    #: The server's annotations, when the gateway's MCP listing carries them; ``None`` when
    #: the server published none (the memory service's tool catalog is then the source).
    annotations: ToolAnnotations | None = None


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
    def _from_gateway(
        cls, entry: dict[str, Any], annotations: dict[str, ToolAnnotations] | None = None
    ) -> MCPClient:
        annotations = annotations or {}
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
                    annotations=annotations.get(f"{config.name}{TOOL_SEPARATOR}{tool['name']}"),
                )
                for tool in entry.get("tools") or ()
            ),
        )


class MCPLog(_Frozen):
    """One MCP tool execution as the gateway logged it."""

    id: str
    timestamp: datetime
    client: str
    #: The tool's own name; :attr:`name` is the ``<client>-<tool>`` form requests use.
    tool: str
    status: str
    #: The ``x-bf-parent-request-id`` the execution ran under (the gateway's ``llm_request_id``).
    parent_request_id: str | None = None
    arguments: Any = None
    result: Any = None
    error: str | None = None
    latency_ms: float | None = None

    @property
    def name(self) -> str:
        return f"{self.client}{TOOL_SEPARATOR}{self.tool}"

    @classmethod
    def _from_gateway(cls, entry: dict[str, Any]) -> MCPLog:
        arguments = entry.get("arguments")
        if isinstance(arguments, str):  # logged as the JSON text the model produced
            with suppress(ValueError):
                arguments = json.loads(arguments)
        error = ((entry.get("error_details") or {}).get("error") or {}).get("message")
        return cls(
            id=entry["id"],
            timestamp=entry["timestamp"],
            client=str(entry.get("server_label") or ""),
            tool=entry["tool_name"],
            status=entry["status"],
            parent_request_id=entry.get("llm_request_id"),
            arguments=arguments,
            result=entry.get("result"),
            error=error,
            latency_ms=entry.get("latency"),
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


#: Bifrost's Code Mode meta-tools, as ``tools/list`` names them.
CODE_MODE_META_TOOLS = frozenset(
    {"listToolFiles", "readToolFile", "getToolDocs", "executeToolCode"}
)
_SERVER_FILE = re.compile(r"^\s*(?:servers/)?([A-Za-z0-9_.]+)\.pyi\s*$", re.M)
_DEF = re.compile(r"^def (\w+)\((.*)\)\s*(?:->\s*[^:]+)?:\s*(?:#\s*(.*))?$", re.M)
#: A declaration's Python type as the JSON schema type it stands for (others: any value).
_JSON_TYPES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}


def listed_tool(tool: dict[str, Any]) -> ToolDef:
    """One ``tools/list`` entry (not a meta-tool) as a :class:`ToolDef`."""
    name = str(tool["name"])
    hints = None
    if isinstance(tool.get("annotations"), dict):
        with suppress(ValidationError):  # a server's malformed hints are no hints
            parsed = ToolAnnotations.model_validate(tool["annotations"])
            hints = parsed if parsed.model_fields_set else None
    schema = tool.get("inputSchema")
    return ToolDef(
        name=name,
        client=name.split(TOOL_SEPARATOR, 1)[0],
        description=str(tool.get("description") or ""),
        parameters=dict(schema) if isinstance(schema, dict) else {},
        annotations=hints,
    )


def server_files(text: str) -> list[str]:
    """The Code Mode clients ``listToolFiles`` names (one ``<client>.pyi`` each)."""
    return list(dict.fromkeys(_SERVER_FILE.findall(text)))


def declared_tools(client: str, text: str) -> list[ToolDef]:
    """A Code Mode client's tools from its ``readToolFile`` declarations. The parameters are
    the signature's, as JSON schema (a type outside ``str``/``int``/``float``/``bool``/
    ``list``/``dict`` admits any value; one with a default is optional); the description is
    the declaration's comment, which the gateway may truncate."""
    tools = []
    for name, params, comment in _DEF.findall(text):
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in _split(params):
            head, _, default = param.partition("=")
            pname, _, ptype = head.partition(":")
            pname = pname.strip().lstrip("*")
            if not pname:
                continue
            base = ptype.strip().split("[", 1)[0].strip()
            properties[pname] = {"type": _JSON_TYPES[base]} if base in _JSON_TYPES else {}
            if not default.strip():
                required.append(pname)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        tools.append(
            ToolDef(
                name=f"{client}{TOOL_SEPARATOR}{name}",
                client=client,
                description=comment.strip(),
                parameters=schema,
                code_mode=True,
            )
        )
    return tools


def _split(params: str) -> list[str]:
    """A signature's parameters, split on the commas outside brackets."""
    parts, depth, current = [], 0, ""
    for char in params:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    return [p for p in (*parts, current) if p.strip()]


class MCP(Resource):
    """``bf.mcp`` — list and manage the gateway's MCP clients."""

    async def clients(self) -> list[MCPClient]:
        """Every registered client, across all pages, with its tools' annotations (an admin
        listing: with admin auth on it needs ``admin_token``; :meth:`Bifrost.tools` is what a
        virtual key may list)."""
        entries = await self._api.pages("/api/mcp/clients", ("clients",), _PAGE)
        listed = await self._listing() if any(e.get("tools") for e in entries) else {}
        annotations = {name: hints for name, hints in (listed or {}).items() if hints is not None}
        return [MCPClient._from_gateway(entry, annotations) for entry in entries]

    async def _listing(self) -> dict[str, ToolAnnotations | None] | None:
        """``<client>-<tool>`` → annotations, from the gateway's MCP ``tools/list``.

        The gateway answers it for the key the request carries: the tools that key's MCP
        allow-list admits, no others. A tool whose server published no hints (the gateway
        lists it with ``annotations: {}``), or malformed ones, maps to ``None``. ``None`` for
        the whole listing means the endpoint is off or refused the call.
        """
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        try:
            payload = await self._api.post(MCP_ENDPOINT, request)
        except GatewayError:
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return None
        return {
            str(t["name"]): listed_tool(t).annotations
            for t in tools
            if isinstance(t, dict) and "name" in t
        }

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
