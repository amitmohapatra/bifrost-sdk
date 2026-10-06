"""03: text as it is generated.

``stream`` yields each delta from the gateway's server-sent events. It is not retried: a
partly shown answer cannot be replayed. Offline.

    uv run python examples/03_stream.py
"""

import asyncio

from _fake_gateway import KEY, URL, FakeGateway

from bifrost_sdk import Bifrost


async def main() -> None:
    with FakeGateway():
        async with Bifrost(f"{URL}/v1", model="provider/model", api_key=KEY) as bf:
            deltas = []
            async for delta in bf.stream("a dragon who audits invoices"):
                deltas.append(delta)
                print(delta, end="", flush=True)
            print(f"\n{len(deltas)} deltas")
            assert len(deltas) > 1


if __name__ == "__main__":
    asyncio.run(main())
