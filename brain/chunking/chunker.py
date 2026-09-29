"""Paragraph-aware sentence packing.

Same idea as Cognee's TextChunker (paragraph -> sentence -> pack up to a token
budget, never cutting mid-sentence), reimplemented in one small function.
Chunks carry character offsets so evidence and mentions can point back into
the source document.
"""

from __future__ import annotations

import re
from functools import lru_cache

from brain.chunking.models import TextChunk

# Abbreviations that end in "." but do not end a sentence.
_ABBREVIATIONS = {"u.s", "e.g", "i.e", "dr", "mr", "ms", "vs", "al", "fig", "no", "approx", "inc", "ltd"}
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s+")


@lru_cache
def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # offline / encoding download unavailable
        return None


def count_tokens(text: str) -> int:
    enc = _encoder()
    # disallowed_special=(): document text may quote special tokens such as
    # '<|endoftext|>' (LLM papers do); count them as plain text, don't raise.
    return len(enc.encode(text, disallowed_special=())) if enc else max(1, len(text) // 4)


def _split_sentences(text: str, offset: int) -> list[tuple[int, int]]:
    """Return (start, end) spans of sentences within `text`, shifted by `offset`."""
    spans, start = [], 0
    for match in _SENTENCE_END.finditer(text):
        before = text[start : match.start()].split()
        last_word = before[-1].lower().rstrip(".") if before else ""
        if last_word in _ABBREVIATIONS or re.fullmatch(r"[a-z]", last_word):
            continue
        spans.append((offset + start, offset + match.start() + 1))
        start = match.end()
    if text[start:].strip():
        spans.append((offset + start, offset + len(text.rstrip())))
    return spans


def _sentence_spans(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, starts_paragraph) for every sentence in the document."""
    out = []
    for para in re.finditer(r"[^\n](?:.|\n(?!\n))*", text):
        for i, (s, e) in enumerate(_split_sentences(para.group(0), para.start())):
            out.append((s, e, i == 0))
    return out


def chunk_text(text: str, max_tokens: int = 400, overlap_sentences: int = 0) -> list[TextChunk]:
    spans = _sentence_spans(text)
    chunks: list[TextChunk] = []
    current: list[tuple[int, int, bool]] = []
    current_tokens = 0

    def flush() -> None:
        start, end = current[0][0], current[-1][1]
        body = text[start:end]
        chunks.append(TextChunk(len(chunks), body, start, end, count_tokens(body)))

    for span in spans:
        tokens = count_tokens(text[span[0] : span[1]])
        if current and current_tokens + tokens > max_tokens:
            flush()
            current = current[-overlap_sentences:] if overlap_sentences else []
            current_tokens = sum(count_tokens(text[s:e]) for s, e, _ in current)
        current.append(span)
        current_tokens += tokens
    if current:
        flush()
    return chunks
