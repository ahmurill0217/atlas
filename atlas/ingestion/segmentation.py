"""Stage 3 — segmentation along natural boundaries.

Adapters emit *blocks* at the boundaries their format provides (email body vs
quoted chain, transcript turn, Markdown/DOCX heading section, paragraph, table,
PDF page). This module only splits a block that is too long, and then only at
sentence boundaries — never mid-sentence, never by fixed size alone — and
assembles raw_text with exact section offsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from atlas.ingestion.normalized import Section

SECTION_MAX_CHARS = 2000

# Tokens ending in "." that do not end a sentence.
_ABBREVIATIONS = {"u.s", "e.g", "i.e", "etc", "vs", "dr", "mr", "mrs", "ms", "inc", "ltd", "co", "corp",
                  "no", "fig", "approx", "jr", "sr", "st", "dept", "est"}
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s+")


@dataclass
class Block:
    kind: str                     # paragraph | heading | table | page | summary | ...
    text: str
    metadata: dict = field(default_factory=dict)


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        words = text[start:m.start()].split()
        last = words[-1].lower().rstrip(".") if words else ""
        if last in _ABBREVIATIONS or re.fullmatch(r"[a-z]", last):
            continue
        spans.append((start, m.start() + 1))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text.rstrip())))
    return spans


def split_long(text: str, max_chars: int = SECTION_MAX_CHARS) -> list[str]:
    """Pack whole sentences into parts of at most max_chars; a single sentence
    longer than that is split at the last whitespace before the limit."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts, current = [], ""
    for s, e in sentence_spans(text):
        sentence = text[s:e].strip()
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def assemble(blocks: list[Block], max_chars: int = SECTION_MAX_CHARS) -> tuple[str, list[Section]]:
    """Blocks -> (raw_text, sections) with sequential ordinals and exact offsets.
    Oversized blocks become several sections sharing the block's metadata plus
    `part` / `parts`."""
    raw: list[str] = []
    sections: list[Section] = []
    cursor = 0
    for block in blocks:
        pieces = split_long(block.text, max_chars)
        for i, piece in enumerate(pieces):
            if raw:
                raw.append("\n\n")
                cursor += 2
            meta = dict(block.metadata)
            if len(pieces) > 1:
                meta.update(part=i + 1, parts=len(pieces))
            sections.append(Section(ordinal=len(sections), kind=block.kind, text=piece, start_char=cursor,
                                    end_char=cursor + len(piece), metadata=meta))
            raw.append(piece)
            cursor += len(piece)
    return "".join(raw), sections


def headed_blocks(items: list[tuple[int | None, str] | tuple[int | None, str, str]]) -> list[Block]:
    """(heading_level | None, text[, kind]) items in document order -> blocks whose
    metadata carries the heading path, e.g. ["Statement of Work", "Scope"].
    Level 0 is a title. Non-heading items keep their kind (paragraph, table, ...)."""
    path: list[tuple[int, str]] = []
    blocks: list[Block] = []
    for item in items:
        level, text = item[0], item[1].strip()
        kind = item[2] if len(item) > 2 else "paragraph"
        if not text:
            continue
        if level is not None:
            path = [(lv, t) for lv, t in path if lv < level] + [(level, text)]
            blocks.append(Block("heading", text, {"heading_path": [t for _, t in path], "level": level}))
        else:
            blocks.append(Block(kind, text, {"heading_path": [t for _, t in path]}))
    return blocks
