"""04: list the MCP tools a key may run, offer them, and run the call the model makes.

The completion never gets the gateway's tools (deny-all): the caller lists them, offers them
as ``tools=``, checks each call, and runs it with ``execute_tool``. ``parent_request_id``
finds the execution again in the MCP log. Offline.

    uv run python examples/04_mcp_list_and_execute.py
"""

import asyncio
from datetime import UTC, datetime, timedelta

from _fake_gateway import KEY, URL, FakeGateway

from bifrost_sdk import Bifrost, GatewayError, Options

RUN_ID = "run-7"


def allowed(call: dict) -> bool:
    """Your policy, before anything runs (in Trellis: trellis.harness.governance)."""
    return call["function"]["name"] != "erp-create_po"


async def main() -> None:
    with FakeGateway():
        async with Bifrost(f"{URL}/v1", model="provider/model", api_key=KEY) as bf:
            tools = await bf.tools(clients=["erp"], only=["erp-get_stock"])
            for tool in await bf.tools():
                hints = tool.annotations
                print(f"listed {tool.name:<18} read_only={hints and hints.read_only_hint}")
            offered = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

            messages = [{"role": "user", "content": "How many A-1 do we have?"}]
            reply = await bf.complete(messages, tools=offered)
            message = reply["choices"][0]["message"]
            messages.append(message)
            for call in message.get("tool_calls") or []:
                assert allowed(call)
                scope = Options(mcp_clients=["erp"], parent_request_id=RUN_ID)
                messages.append(await bf.execute_tool(call, options=scope))
            final = await bf.complete(messages, tools=offered)
            print("answer:", final["choices"][0]["message"]["content"])

            since = datetime.now(UTC) - timedelta(days=1)
            for log in await bf.mcp_logs(since, parent_request_id=RUN_ID):
                print(f"log: {log.client}-{log.tool} {log.status}")

            out_of_scope = {
                "id": "c2",
                "function": {"name": "crm-find_customer", "arguments": "{}"},
            }
            try:
                await bf.execute_tool(out_of_scope, options=Options(mcp_clients=["erp"]))
            except GatewayError as exc:
                print(f"refused by the gateway: {exc.status}, retryable={exc.retryable}")

            try:
                await bf.chat("hi", options=Options(mcp_clients=["erp"]))
            except ValueError:
                print("refused before sending: a completion never carries an MCP scope")


if __name__ == "__main__":
    asyncio.run(main())
