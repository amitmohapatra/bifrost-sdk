"""07: operator tooling: register an MCP server, and create a virtual key that may use it.

``bf.mcp`` manages MCP clients; ``Admin`` manages virtual keys, budgets and routing. A key's
``provider_configs`` and ``mcp_configs`` are deny-by-default, so they are named at the call
site. Keys are never printed. Offline.

    uv run python examples/07_admin_mcp_clients_and_keys.py
"""

import asyncio

from _fake_gateway import ADMIN_TOKEN, KEY, URL, FakeGateway

from bifrost_sdk import Bifrost, MCPClientConfig, MCPConnection
from bifrost_sdk.admin import Admin


async def main() -> None:
    with FakeGateway():
        async with Bifrost(f"{URL}/v1", api_key=KEY, admin_token=ADMIN_TOKEN) as bf:
            config = MCPClientConfig(
                name="docs",  # no "-": it separates client from tool in every tool name
                connection=MCPConnection(type="http", url="https://docs.example.com/mcp"),
                tools_to_execute=["*"],  # required: an empty list lets no tool run
            )
            await bf.mcp.add(config)
            clients = await bf.mcp.clients()
            for client in clients:
                name, tools = client.config.name, len(client.tools)
                print(f"client {name:<5} {client.state:<10} {tools} tools")
            [docs] = [c for c in clients if c.config.name == "docs"]
            await bf.mcp.remove(docs.id)

        async with Admin(URL, token=ADMIN_TOKEN) as admin:
            created = await admin.vk.create(
                "triage-agent",
                provider_configs=[{"provider": "provider", "allowed_models": ["model"]}],
                mcp_configs=[{"mcp_client_name": "erp", "tools_to_execute": ["get_stock"]}],
            )
            key = created["virtual_key"]
            print("virtual key created:", key["id"], key["name"])  # the id, never the secret
            print("keys:", [k["name"] for k in await admin.vk.list()])


if __name__ == "__main__":
    asyncio.run(main())
