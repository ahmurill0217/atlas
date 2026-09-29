from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    start: int  # character offset into the parsed document text
    end: int
    token_count: int
