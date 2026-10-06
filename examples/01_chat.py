"""01: one completion as text, with per-request options.

``chat`` returns the answer as a string. ``Options`` becomes ``x-bf-*`` headers; every
completion also carries the deny-all MCP scope, so the gateway adds no tools of its own.
Offline: a scripted gateway answers (``_fake_gateway.py``).

    uv run python examples/01_chat.py
"""

import asyncio

from _fake_gateway import KEY, URL, FakeGateway

from bifrost_sdk import NO_GATEWAY_TOOLS, Bifrost, Options

MODEL = "provider/model"  # as the gateway names it


async def main() -> None:
    with FakeGateway() as gw:
        async with Bifrost(f"{URL}/v1", model=MODEL, api_key=KEY) as bf:
            assert await bf.ping()  # GET /v1/models answered 2xx

            options = Options(session_id="chat-42", customer_id="acme", content_logging=False)
            text = await bf.chat("Summarise the Q3 report", system="Be brief.", options=options)
            print("answer:", text)

            sent = gw.last("/v1/chat/completions").headers
            assert all(sent[name] == value for name, value in NO_GATEWAY_TOOLS.items())
            print("headers sent:", sorted(h for h in sent if h.startswith("x-bf-")))


if __name__ == "__main__":
    asyncio.run(main())
