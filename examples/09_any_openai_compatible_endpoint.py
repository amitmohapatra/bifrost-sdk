"""09: any OpenAI-compatible endpoint, a reasoning model's profile, no Bifrost extras.

The client's ``gateway`` is ``openai-compatible`` unless told otherwise: it sends no ``x-bf-*``
header, and a Bifrost-only method raises ``BifrostOnlyError`` before sending anything. The
profile, not the model's name, decides the body: a model that reasons gets its budget as
``max_completion_tokens`` and no ``temperature``. ``auth=`` takes any ``httpx.Auth``; here a
bearer key built by name, as an ``LLM_AUTH`` setting would give it. Offline: ``respx`` answers
as an OpenAI-compatible endpoint would (not ``_fake_gateway.py``, which is a Bifrost gateway).

    uv run python examples/09_any_openai_compatible_endpoint.py
"""

import asyncio
import json

import respx

from bifrost_sdk import Bifrost, BifrostOnlyError, ModelProfile, auth_strategy

URL = "http://llm.test/v1"  # e.g. https://api.openai.com/v1, an Ollama or vLLM server
KEY = "sk-placeholder"  # a provider key from your own settings; never printed
BUDGET = 4000
REPLY = {"choices": [{"message": {"content": "Q3 grew 12%."}, "finish_reason": "stop"}]}


async def main() -> None:
    with respx.mock(assert_all_called=True) as endpoint:
        completions = endpoint.post(f"{URL}/chat/completions").respond(200, json=REPLY)
        async with Bifrost(
            URL,
            model="o4-mini",
            auth=auth_strategy("bearer", KEY),
            profile=ModelProfile(reasoning_output=True, temperature=False),
        ) as bf:
            print("answer:", await bf.chat("Summarise the Q3 report", max_tokens=BUDGET))

            sent = completions.calls.last.request
            body = json.loads(sent.content)
            assert body["max_completion_tokens"] == BUDGET
            assert "max_tokens" not in body and "temperature" not in body
            assert not [h for h in sent.headers if h.startswith("x-bf-")]
            print("body keys:", sorted(body))

            try:
                await bf.tools()
            except BifrostOnlyError as exc:
                print("refused, unsent:", exc)


if __name__ == "__main__":
    asyncio.run(main())
