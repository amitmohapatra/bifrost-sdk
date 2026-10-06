"""A small, scripted Bifrost gateway for the examples, served in-process with ``respx``.

Every example talks to ``http://gateway.test`` through the real ``bifrost_sdk`` client; respx
intercepts the HTTP requests and answers them here, so nothing leaves the process. The
answers have the shapes a running gateway sends (the unit tests in ``tests/`` pin the same
shapes). The fake also *checks* what the client sent: every completion must carry the
deny-all MCP scope (``NO_GATEWAY_TOOLS``), or it answers 400.

What it knows:

* two MCP clients, ``erp`` (``get_stock``, ``create_po``) and ``crm`` (``find_customer``),
  reachable with the virtual key ``KEY``; ``erp`` forwards the ``x-user-token`` header;
* a model that answers text, a JSON object (wrapped in prose and a code fence, as models do),
  a stream, or a tool call when it is offered tools;
* the prompt repository, the skills repository, Virtual MCPs, MCP client registration,
  virtual keys and the MCP log;
* ``fail_next(status, times)`` to make the next completions fail, for retries and the breaker.

Not a gateway: no routing, budgets or providers, and only the routes the examples call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
import respx

URL = "http://gateway.test"
#: The virtual key the examples use. A placeholder: never a real key, never printed.
KEY = "vk-placeholder"
KEY_ID = "vk-1"
ADMIN_TOKEN = "admin-token-placeholder"
NOW = "2026-10-06T08:00:00Z"
DENY_ALL = {"x-bf-mcp-include-clients": "", "x-bf-mcp-include-tools": ""}

TOOLS: dict[str, dict[str, dict[str, Any]]] = {
    "erp": {
        "get_stock": {
            "description": "Units on hand for a SKU.",
            "inputSchema": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
            },
            "annotations": {"readOnlyHint": True},
        },
        "create_po": {
            "description": "Create a purchase order.",
            "inputSchema": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["sku", "qty"],
            },
            "annotations": {"destructiveHint": True},
        },
    },
    "crm": {
        "find_customer": {
            "description": "Find a customer by name.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            "annotations": {},
        }
    },
}
CLIENT_IDS = {"erp": "c-erp", "crm": "c-crm"}
FORWARDED = {"erp": ("x-user-token",)}
USERS = {"token-for-ada": "ada"}  # what the erp server makes of a forwarded token


def _ok(payload: Any = None) -> httpx.Response:
    return httpx.Response(200) if payload is None else httpx.Response(200, json=payload)


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}})


def _run_tool(client: str, tool: str, args: dict[str, Any], user: str | None) -> dict[str, Any]:
    """What the fake MCP servers answer."""
    if tool == "get_stock":
        out: dict[str, Any] = {"sku": args.get("sku"), "on_hand": 42}
    elif tool == "create_po":
        out = {"po": "PO-7", "sku": args.get("sku"), "qty": args.get("qty")}
    else:
        out = {"customer": args.get("name"), "tier": "gold"}
    if user is not None:
        out["as_user"] = user
    return out


@dataclass
class FakeGateway:
    """Use as ``with FakeGateway() as gw:``; ``gw.seen`` holds every request it answered."""

    seen: list[httpx.Request] = field(default_factory=list)
    prompts: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    skill_files: dict[tuple[str, str], str] = field(default_factory=dict)
    vmcps: dict[int, dict[str, Any]] = field(default_factory=dict)
    extra_clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    keys: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    _failures: list[int] = field(default_factory=list)
    _router: Any = None

    # ------------------------------------------------------------------ setup

    def __enter__(self) -> FakeGateway:
        self._router = respx.mock(assert_all_called=False)
        route = self._router.route
        host = "gateway.test"
        route(method="GET", host=host, path="/v1/models").mock(side_effect=self._models)
        route(method="POST", host=host, path="/v1/chat/completions").mock(
            side_effect=self._completion
        )
        route(method="POST", host=host, path="/v1/mcp/tool/execute").mock(side_effect=self._execute)
        route(method="POST", host=host, path__regex=r"^/mcp(?:/(?P<slug>[^/]+))?$").mock(
            side_effect=self._mcp
        )
        route(host=host, path__regex=r"^/api/(?P<rest>.+)$").mock(side_effect=self._api)
        self._router.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._router.stop()

    def fail_next(self, status: int, times: int = 1) -> None:
        """The next ``times`` completions answer ``status`` (503, 429, ...)."""
        self._failures.extend([status] * times)

    def last(self, path: str) -> httpx.Request:
        """The most recent request to ``path``."""
        return next(r for r in reversed(self.seen) if r.url.path == path)

    # ------------------------------------------------------------------ /v1

    def _models(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return _ok({"data": [{"id": "provider/model"}]})

    def _completion(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self._failures:
            status = self._failures.pop(0)
            if status == 429:  # noqa: PLR2004 - the delay in the body, as some providers send it
                return _error(429, "Quota exceeded. Please retry in 0.01s.")
            return _error(status, "upstream unavailable")
        scope = {k: request.headers.get(k) for k in DENY_ALL}
        if scope != DENY_ALL:
            return _error(400, "this fake gateway refuses a completion without deny-all")
        body = json.loads(request.content)
        messages = self._with_stored_prompt(request, body["messages"])
        if body.get("stream"):
            return self._stream(messages)
        message = self._answer(messages, body)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return _ok(
            {
                "id": "cmpl-1",
                "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21},
            }
        )

    def _with_stored_prompt(
        self, request: httpx.Request, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        prompt = self.prompts.get(request.headers.get("x-bf-prompt-id", ""))
        if not prompt or not prompt["versions"]:
            return messages  # an unknown id is not an error: the request goes as it is
        wanted = request.headers.get("x-bf-prompt-version")
        versions = prompt["versions"]
        version = next((v for v in versions if str(v["version_number"]) == wanted), versions[-1])
        return [row["message"] for row in version["messages"]] + messages

    @staticmethod
    def _answer(messages: list[dict[str, Any]], body: dict[str, Any]) -> dict[str, Any]:
        last = messages[-1]
        system = " ".join(m["content"] for m in messages if m["role"] == "system")
        if last["role"] == "tool":
            return {"role": "assistant", "content": f"Done. The tool said: {last['content']}"}
        if body.get("tools"):
            function = body["tools"][0]["function"]
            required = function.get("parameters", {}).get("required", [])
            args = {name: ("A-1" if name == "sku" else 12) for name in required}
            call = {"name": function["name"], "arguments": json.dumps(args)}
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": call}],
            }
        if body.get("response_format"):
            schema = body["response_format"]["json_schema"]["schema"]
            sample = {"string": "ACME GmbH", "number": 1240.5, "integer": 3, "boolean": True}
            obj = {k: sample.get(v.get("type")) for k, v in schema["properties"].items()}
            return {"role": "assistant", "content": f"Sure:\n```json\n{json.dumps(obj)}\n```"}
        text = f"(a scripted answer to: {last['content'][:40]})"
        if system:
            text = f"[{system}] {text}"
        return {"role": "assistant", "content": text}

    @staticmethod
    def _stream(messages: list[dict[str, Any]]) -> httpx.Response:
        words = f"Once upon a time, {messages[-1]['content'][:20]}...".split(" ")
        chunks = [
            json.dumps({"choices": [{"index": 0, "delta": {"content": word + " "}}]})
            for word in words
        ]
        sse = "".join(f"data: {c}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    # ------------------------------------------------------------------ MCP

    def _allowed(self, request: httpx.Request, slug: str | None) -> dict[str, set[str]]:
        """client -> tool names this request may reach."""
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return {}
        everything = {client: set(tools) for client, tools in TOOLS.items()}
        if slug is None:
            return everything
        if slug in TOOLS:  # one client's own endpoint
            return {slug: everything[slug]}
        vmcp = next((v for v in self.vmcps.values() if v["endpoint_slug"] == slug), None)
        if vmcp is None or KEY_ID not in vmcp["virtual_key_ids"]:
            raise PermissionError(slug)
        by_id = {cid: name for name, cid in CLIENT_IDS.items()}
        allowed: dict[str, set[str]] = {}
        for spec in vmcp["tools"]:
            client = by_id[spec["mcp_client_id"]]
            names = set(spec["tool_names"])
            allowed[client] = everything[client] if "*" in names else names & everything[client]
        return allowed

    def _mcp(self, request: httpx.Request, slug: str | None = None) -> httpx.Response:
        self.seen.append(request)
        message = json.loads(request.content)
        try:
            allowed = self._allowed(request, unquote(slug) if slug else None)
        except PermissionError:
            return _error(403, "virtual key is not attached to this Virtual MCP")
        if message["method"] == "tools/list":
            tools = [
                {"name": f"{client}-{tool}", **TOOLS[client][tool]}
                for client, names in allowed.items()
                for tool in sorted(names)
            ]
            return _ok({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": tools}})
        name = message["params"]["name"]
        client, _, tool = name.partition("-")
        if tool not in allowed.get(client, set()):
            result = {"isError": True, "content": [{"type": "text", "text": f"{name}: no"}]}
        else:
            user = self._user(request, client)
            out = _run_tool(client, tool, message["params"]["arguments"], user)
            self._log(name, out, request)
            result = {"content": [{"type": "text", "text": json.dumps(out)}]}
        return _ok({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _execute(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        call = json.loads(request.content)
        name = call["function"]["name"]
        client, _, tool = name.partition("-")
        scope = request.headers.get("x-bf-mcp-include-clients")
        in_scope = scope is None or client in {c for c in scope.split(",") if c}
        if not in_scope or tool not in self._allowed(request, None).get(client, set()):
            return _error(400, f"tool '{name}' is not available or not permitted")
        args = json.loads(call["function"].get("arguments") or "{}")
        out = _run_tool(client, tool, args, self._user(request, client))
        self._log(name, out, request)
        return _ok({"role": "tool", "content": json.dumps(out), "tool_call_id": call.get("id")})

    @staticmethod
    def _user(request: httpx.Request, client: str) -> str | None:
        for header in FORWARDED.get(client, ()):
            if header in request.headers:
                return USERS.get(request.headers[header], "unknown")
        return None

    def _log(self, name: str, out: dict[str, Any], request: httpx.Request) -> None:
        client, _, tool = name.partition("-")
        self.logs.append(
            {
                "id": f"log-{len(self.logs) + 1}",
                "llm_request_id": request.headers.get("x-bf-parent-request-id"),
                "timestamp": NOW,
                "tool_name": tool,
                "server_label": client,
                "status": "success",
                "arguments": json.dumps(out),
                "result": out,
                "latency": 12,
            }
        )

    # ------------------------------------------------------------------ /api

    def _api(self, request: httpx.Request, rest: str) -> httpx.Response:  # noqa: PLR0911
        self.seen.append(request)
        body = json.loads(request.content) if request.content else {}
        method, path = request.method, unquote(rest)
        if path == "mcp-logs":
            wanted = request.url.params.get("llm_request_ids")
            return _ok({"logs": [g for g in self.logs if wanted in (None, g["llm_request_id"])]})
        if path.startswith("prompt-repo/"):
            return self._prompt_repo(method, path.removeprefix("prompt-repo/"), body)
        if path.startswith("skills"):
            return self._skills(method, path, body, request)
        if path.startswith("mcp/virtual-mcps"):
            return self._virtual_mcps(method, path, body)
        if path.startswith("mcp/client"):
            return self._clients(method, path, body, request)
        if path == "governance/virtual-keys":
            if method == "POST":
                key = {"id": f"vk-{len(self.keys) + 2}", "name": body["name"], **body}
                self.keys.append(key)
                return _ok({"message": "created", "virtual_key": key})
            return _ok({"virtual_keys": self.keys})
        return _error(404, f"no route {method} /api/{path} in this fake")

    def _prompt_repo(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        if path == "prompts" and method == "POST":
            prompt = {"id": f"p-{len(self.prompts) + 1}", "name": body["name"], "versions": []}
            prompt |= {"created_at": NOW, "updated_at": NOW}
            self.prompts[prompt["id"]] = prompt
            return _ok({"prompt": self._prompt(prompt)})
        if path == "prompts":
            return _ok({"prompts": [self._prompt(p) for p in self.prompts.values()]})
        match = re.fullmatch(r"prompts/([^/]+)(/versions)?", path)
        prompt = self.prompts.get(match.group(1)) if match else None
        if prompt is None:
            return _error(404, "prompt not found")
        if match and match.group(2) and method == "POST":
            for version in prompt["versions"]:
                version["is_latest"] = False
            version = {
                "id": 100 + len(prompt["versions"]),
                "prompt_id": prompt["id"],
                "version_number": len(prompt["versions"]) + 1,
                "commit_message": body["commit_message"],
                "model_params": body["model_params"],
                "provider": body["provider"],
                "model": body["model"],
                "is_latest": True,
                "created_at": NOW,
                "messages": [
                    {"order_index": i, "message": m} for i, m in enumerate(body["messages"])
                ],
            }
            prompt["versions"].append(version)
            return _ok({"version": version})
        return _ok({"prompt": self._prompt(prompt)})

    @staticmethod
    def _prompt(prompt: dict[str, Any]) -> dict[str, Any]:
        latest = prompt["versions"][-1] if prompt["versions"] else None
        out = {k: v for k, v in prompt.items() if k != "versions"}
        return out | ({"latest_version": latest} if latest else {})

    def _skills(
        self, method: str, path: str, body: dict[str, Any], request: httpx.Request
    ) -> httpx.Response:
        if path.startswith("skills/serve/"):
            name, _, file = path.removeprefix("skills/serve/").partition("/files/")
            text = self.skill_files.get((name, file))
            return httpx.Response(200, text=text) if text else _error(404, "no such file")
        if path == "skills" and method == "POST":
            skill = {"id": f"s-{len(self.skills) + 1}", "name": body["name"]}
            self.skills[skill["id"]] = skill
            return _ok({"skill": self._publish(skill, body)})
        if path == "skills":
            search = request.url.params.get("search") or ""
            found = [s for s in self.skills.values() if search in s["name"]]
            listed = [{**s, "skill_md_body": "", "files": []} for s in found]
            offset = int(request.url.params.get("offset") or 0)
            return _ok({"skills": listed[offset:]})
        skill_id = path.removeprefix("skills/").split("/")[0]
        skill = self.skills.get(skill_id)
        if skill is None:
            return _error(404, "skill not found")
        if method == "PUT":
            return _ok({"skill": self._publish(skill, body)})
        return _ok({"skill": skill})

    def _publish(self, skill: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        files = [
            {
                "path": f["path"],
                "source_type": f["source_type"],
                "mime_type": f["mime_type"],
                "file_size_bytes": len(f["content"].encode()),
            }
            for f in body.get("files") or ()
        ]
        for f in body.get("files") or ():
            self.skill_files[(skill["name"], f["path"])] = f["content"]
        skill |= {
            "description": body["description"],
            "skill_md_body": body["skill_md_body"],
            "latest_version": body["version"],
            "highest_version": body["version"],
            "files": files,
            "created_at": NOW,
            "updated_at": NOW,
        }
        return skill

    def _virtual_mcps(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        attach = re.fullmatch(r"mcp/virtual-mcps/(\d+)/virtual-keys/([^/]+)", path)
        if attach:
            self.vmcps[int(attach.group(1))]["virtual_key_ids"].append(attach.group(2))
            return _ok()
        if method == "POST":
            vmcp = {
                "id": len(self.vmcps) + 1,
                "name": body["name"],
                "endpoint_slug": body.get("endpoint_slug")
                or re.sub(r"[^a-z0-9]+", "-", body["name"].lower()).strip("-"),
                "enabled": body.get("enabled", True),
                "description": body.get("description"),
                "tools": body["tools"],
                "virtual_key_ids": [],
                "created_at": NOW,
                "updated_at": NOW,
            }
            self.vmcps[vmcp["id"]] = vmcp
            return _ok({"virtual_mcp": vmcp})
        return _ok({"virtual_mcps": list(self.vmcps.values())})

    def _clients(
        self, method: str, path: str, body: dict[str, Any], request: httpx.Request
    ) -> httpx.Response:
        if method == "POST":
            client_id = f"c-{body['name']}"
            self.extra_clients[client_id] = {**body, "client_id": client_id}
            return _ok({"status": "success"})
        if method == "PUT":
            client_id = path.rsplit("/", 1)[1]
            self.extra_clients[client_id] |= body
            return _ok({"status": "success"})
        if method == "DELETE":
            self.extra_clients.pop(path.rsplit("/", 1)[1], None)
            return _ok({"status": "success"})
        builtin = [
            {
                "config": {
                    "client_id": CLIENT_IDS[name],
                    "name": name,
                    "connection_type": "http",
                    "connection_string": f"https://{name}.example.com/mcp",
                    "tools_to_execute": ["*"],
                    "allowed_extra_headers": list(FORWARDED.get(name, ())),
                },
                "state": "connected",
                "tools": [
                    {"name": tool, "description": spec["description"]}
                    for tool, spec in tools.items()
                ],
            }
            for name, tools in TOOLS.items()
        ]
        added = [
            {"config": c, "state": "connected", "tools": []} for c in self.extra_clients.values()
        ]
        offset = int(request.url.params.get("offset") or 0)
        return _ok({"clients": (builtin + added)[offset:]})
