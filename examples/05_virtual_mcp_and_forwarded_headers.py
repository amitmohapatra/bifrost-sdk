"""05: a Virtual MCP (one endpoint for a bundle of tools) and a caller header forwarded to
the MCP server as a per-user credential.

An operator bundles ``erp``'s ``get_stock`` and all of ``crm`` as "Finance Tools" and
attaches it to a virtual key. The application lists and runs tools through its slug only. A
header the client's ``allowed_extra_headers`` names reaches the server on every call. Offline.

    uv run python examples/05_virtual_mcp_and_forwarded_headers.py
"""

import asyncio

from _fake_gateway import ADMIN_TOKEN, CLIENT_IDS, KEY, KEY_ID, URL, FakeGateway

from bifrost_sdk import Bifrost, Options, PermissionDeniedError
from bifrost_sdk.admin import Admin


async def main() -> None:
    with FakeGateway():
        async with Admin(URL, token=ADMIN_TOKEN) as admin:
            bundle = await admin.virtual_mcps.create(
                "Finance Tools",
                {CLIENT_IDS["erp"]: ["get_stock"], CLIENT_IDS["crm"]: ["*"]},  # by client id
            )
            print("created Virtual MCP:", bundle.slug)
            await admin.virtual_mcps.attach(bundle.id, KEY_ID)  # reachable from now on

        async with Bifrost(f"{URL}/v1", model="provider/model", api_key=KEY) as bf:
            listed = [t.name for t in await bf.tools(slug=bundle.slug)]
            print("through the slug:", listed)
            assert "erp-create_po" not in listed

            call = {
                "id": "c1",
                "function": {"name": "erp-get_stock", "arguments": '{"sku": "A-1"}'},
            }
            ada = Options(extra={"x-user-token": "token-for-ada"})  # forwarded to the server
            turn = await bf.execute_tool(call, slug=bundle.slug, options=ada)
            print("tool turn:", turn["content"])
            assert '"as_user": "ada"' in turn["content"]

            outside = {"id": "c2", "function": {"name": "erp-create_po", "arguments": "{}"}}
            turn = await bf.execute_tool(outside, slug=bundle.slug)
            print("outside the bundle is an error turn:", turn.get("is_error"))

            try:
                await bf.tools(slug="not-attached")
            except PermissionDeniedError as exc:
                print("a slug the key is not attached to:", exc.status)


if __name__ == "__main__":
    asyncio.run(main())
