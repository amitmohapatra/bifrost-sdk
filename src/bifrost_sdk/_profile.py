"""What a model accepts, as data: requests are shaped from it, never from the model's name."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """The two facts about a model that decide a request's shape.

    The names are LangChain's model-profile keys (and ``trellis.contracts.ModelProfile``'s,
    which this package does not import): map one onto this field by field. A model that
    reasons, or takes no temperature, is sent ``max_completion_tokens`` in place of
    ``max_tokens`` and no ``temperature``, which OpenAI's reasoning models refuse. The default
    is a plain chat model: ``max_tokens`` and the temperature the call gives.
    """

    reasoning_output: bool = False
    temperature: bool = True

    @property
    def fixed_sampling(self) -> bool:
        """Whether the request carries ``max_completion_tokens`` and no ``temperature``."""
        return self.reasoning_output or not self.temperature


__all__ = ["ModelProfile"]
