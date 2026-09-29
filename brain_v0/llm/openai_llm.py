from __future__ import annotations

from typing import TypeVar

from openai import LengthFinishReasonError, OpenAI
from pydantic import BaseModel

from brain_v0.llm.base import OutputTooLongError

T = TypeVar("T", bound=BaseModel)


class OpenAIStructuredLLM:
    """OpenAI structured outputs: the response is constrained to the JSON
    schema of `schema` and parsed straight into the Pydantic model."""

    def __init__(self, model: str, api_key: str | None = None, temperature: float = 0.0,
                 seed: int | None = None, max_retries: int = 8):
        self.model_name = model
        self.temperature = temperature
        self.seed = seed
        self._client = OpenAI(api_key=api_key, max_retries=max_retries)

    def generate(self, system: str, user: str, schema: type[T]) -> T:
        try:
            completion = self._client.chat.completions.parse(
                model=self.model_name,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
                temperature=self.temperature,
                seed=self.seed,
            )
        except LengthFinishReasonError as exc:
            raise OutputTooLongError(str(exc)[:200]) from exc
        message = completion.choices[0].message
        if message.parsed is None:
            raise RuntimeError(f"LLM returned no parsed output (refusal={message.refusal!r})")
        return message.parsed
