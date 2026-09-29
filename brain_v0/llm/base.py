"""The whole LLM surface the brain needs: one structured-output call.

Keeping this to a single method is deliberate; swapping providers means
implementing `generate` for a Pydantic schema.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class OutputTooLongError(RuntimeError):
    """The model hit its output-length limit before finishing the structured
    response (e.g. a table-heavy chunk yielding hundreds of entities)."""


class StructuredLLM(Protocol):
    model_name: str

    def generate(self, system: str, user: str, schema: type[T]) -> T: ...
