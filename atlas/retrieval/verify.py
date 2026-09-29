"""Check an answer's citations against what the tools actually returned.

An answer is a list of parts (a sentence or bullet each), every factual part citing refs from
the session's ledger (atlas.retrieval.tools). A part passes when:

  - every ref it cites was returned by a tool (no invented refs);
  - each text ref (S#) comes with a quote found word for word in that passage (case,
    punctuation and whitespace aside; "..." may join pieces that are each found);
  - every number in the part (counts, days, years) appears in its cited evidence: the
    quotes and the graph records. "September 23, 2026" passes against 2026-09-23.

A part with a number and no citation fails; a short uncited part (a heading, a "the sources
do not say") passes. This is mechanical: it cannot tell that a quote is apt, only that the
words and figures come from the sources, which rules out invented quotes, dates and counts.
"""

from __future__ import annotations

import re

from atlas.retrieval.tools import Ledger

ANSWER_SCHEMA = {"type": "function", "function": {
    "name": "submit_answer",
    "description": "Submit the final answer as parts, or a clarifying question. The answer is verified; "
                   "parts that fail come back to be fixed or dropped.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["clarify", "parts"], "properties": {
        "clarify": {"type": ["string", "null"], "description": "A question for the user instead of an answer"},
        "parts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["text", "citations"], "properties": {
            "text": {"type": "string", "description": "One claim: a sentence or bullet (markdown)"},
            "citations": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                     "required": ["ref", "quote"], "properties": {
                "ref": {"type": "string", "description": "G# or S# from a tool result"},
                "quote": {"type": ["string", "null"],
                          "description": "For S#: the exact words from the passage that support the claim"}}}}}}}}}}}

_NUMBER = re.compile(r"\d+")
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")   # ids carry no facts
_LIST_MARK = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
_UNCITED_WORDS = 12


def _norm(value: str) -> str:
    value = value.casefold().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def _numbers(value: str) -> set[int]:
    return {int(n) for n in _NUMBER.findall(value)}


def quote_found(quote: str, passage: str) -> bool:
    haystack = _norm(passage)
    pieces = [_norm(p) for p in re.split(r"\.\.\.|…", quote) if _norm(p)]
    return bool(pieces) and all(p in haystack for p in pieces)


def check_part(part: dict, ledger: Ledger) -> list[str]:
    body = _LIST_MARK.sub("", part.get("text") or "")
    citations = part.get("citations") or []
    problems, evidence = [], []
    for c in citations:
        ref, quote = c.get("ref") or "", (c.get("quote") or "").strip()
        entry = ledger.get(ref)
        if entry is None:
            problems.append(f"{ref} was not returned by any tool")
        elif entry.kind == "text":
            if len(_norm(quote).split()) < 3:
                problems.append(f"{ref}: give the exact words (3 or more) from the passage that support this")
            elif not quote_found(quote, entry.text):
                problems.append(f"{ref}: the quote is not in that passage")
            else:
                evidence.append(quote)
        else:
            evidence.append(_ID.sub(" ", entry.text))
            if quote:
                evidence.append(quote)
    numbers = _numbers(body)
    if citations and not problems:
        missing = sorted(numbers - _numbers(" ".join(evidence)))
        if missing:
            problems.append(f"numbers not in the cited evidence: {missing}")
    if not citations and (numbers or len(body.split()) > _UNCITED_WORDS):
        problems.append("a factual claim needs citations")
    return problems


def verify(parts: list[dict], ledger: Ledger) -> dict:
    results = [{"index": i, "text": p.get("text", ""), "problems": check_part(p, ledger)} for i, p in enumerate(parts)]
    failed = [r for r in results if r["problems"]]
    return {"accepted": not failed, "parts": len(parts), "failed": failed}


def render(parts: list[dict], report: dict, ledger: Ledger) -> tuple[str, list[dict], list[dict]]:
    """(answer markdown with [G#]/[S#] markers, the citations it uses, the parts dropped as unverified)."""
    failed = {r["index"] for r in report["failed"]}
    lines, used, dropped = [], {}, []
    for i, part in enumerate(parts):
        if i in failed:
            dropped.append({"text": part.get("text", ""),
                            "problems": next(r["problems"] for r in report["failed"] if r["index"] == i)})
            continue
        refs = list(dict.fromkeys(c["ref"] for c in part.get("citations") or []))
        for c in part.get("citations") or []:
            used.setdefault(c["ref"], c.get("quote"))
        body, markers = part.get("text", "").rstrip(), "".join(f" [{r}]" for r in refs)
        end = body[-1:] if body[-1:] in ".!?" else ""                  # "claim [S1]." keeps marker and claim together
        lines.append(body[:len(body) - len(end)] + markers + end)
    citations = []
    for ref, quote in used.items():
        e = ledger.get(ref)
        citations.append({"marker": ref, "source": "graph" if e.kind == "graph" else "search",
                          "document_id": e.document_id, "title": e.title, "date": (e.date or "")[:10] or None,
                          "quote": quote})
    text = "\n".join(line if _LIST_MARK.match(line) or line.startswith("#") else line + "\n" for line in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), citations, dropped
