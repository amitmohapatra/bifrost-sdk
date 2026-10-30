"""One completion from each provider whose key is set, through its OpenAI-compatible endpoint.

Opt-in like every live test (``pytest -m live``), and collected only for the providers whose
key variable is set: a provider without a key is not a skipped test, it is not a test here.
The key is the provider's own (``OPENAI_API_KEY``, ...), read by this test alone and passed
as ``api_key=``; the SDK reads no variable. ``LLM_LIVE_<PROVIDER>_MODEL`` overrides the model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from bifrost_sdk import Bifrost


@dataclass(frozen=True)
class Provider:
    name: str
    key_variable: str
    base_url: str
    model: str

    @property
    def chosen_model(self) -> str:
        return os.environ.get(f"LLM_LIVE_{self.name.upper()}_MODEL") or self.model


KNOWN = (
    Provider("openai", "OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    Provider("anthropic", "ANTHROPIC_API_KEY", "https://api.anthropic.com/v1", "claude-haiku-4-5"),
    Provider(
        "gemini",
        "GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash",
    ),
)
WITH_KEYS = [provider for provider in KNOWN if os.environ.get(provider.key_variable)]

if WITH_KEYS:

    @pytest.mark.live
    @pytest.mark.parametrize("provider", WITH_KEYS, ids=lambda provider: provider.name)
    async def test_a_provider_answers_through_its_openai_compatible_endpoint(
        provider: Provider,
    ) -> None:
        key = os.environ[provider.key_variable]
        async with Bifrost(provider.base_url, model=provider.chosen_model, api_key=key) as bf:
            answer = await bf.chat("Reply with exactly: OK", max_tokens=256)
        assert "OK" in answer.upper()
