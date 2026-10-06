"""02: a parsed JSON object, checked against a schema.

``json(schema=)`` sends ``response_format`` as a strict ``json_schema`` and parses the reply
defensively: prose and code fences around the object are stripped (the scripted model wraps
its answer in both). Offline.

    uv run python examples/02_json_schema.py
"""

import asyncio
import json

from _fake_gateway import KEY, URL, FakeGateway

from bifrost_sdk import Bifrost

INVOICE = {
    "type": "object",
    "properties": {
        "supplier": {"type": "string"},
        "total_eur": {"type": "number"},
        "lines": {"type": "integer"},
    },
    "required": ["supplier", "total_eur", "lines"],
    "additionalProperties": False,
}


async def main() -> None:
    with FakeGateway() as gw:
        async with Bifrost(f"{URL}/v1", model="provider/model", api_key=KEY) as bf:
            data = await bf.json("Extract the invoice fields: ...", schema=INVOICE)
            print("parsed:", data)
            assert set(data) == set(INVOICE["required"])

            body = json.loads(gw.last("/v1/chat/completions").content)
            assert body["response_format"]["type"] == "json_schema"
            assert body["temperature"] == 0.0  # json() is always deterministic


if __name__ == "__main__":
    asyncio.run(main())
