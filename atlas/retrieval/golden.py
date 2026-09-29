"""Score `atlas ask` answers against a golden set, with no model and no database.

A golden question says what a correct answer must contain, so a run can be graded by
code instead of by hand. The format (full reference: docs/golden_set.md):

    {"id": "gm-rel-001", "source": "gmail", "type": "relationship",
     "question": "When did I last email Steve Lafontaine?", "as": "john.arnold@enron.com",
     "expect": {"facts": ["2001-12-11"], "tools": ["interactions"],
                "documents": ["<atlas document uuid>"]},
     "key_points": ["last 2001-12-11, 26 emails"], "notes": "..."}

`expect` holds the checks; each is optional and only the ones given are scored:

  - facts: strings the answer must contain. An item may be a list of alternatives
    (["86", "eighty-six"]); ISO dates also match as "May 7, 2001", "7 May 2001" and so on.
  - absent: strings the answer must not contain (a wrong person, a wrong figure).
  - documents: documents the answer must cite, each an Atlas document id or its exact
    title; documents_match "all" (default) or "any".
  - tools: tools the agent must call (e.g. interactions for a count, so it isn't
    guessed from search results).
  - clarify: true when the answer must be a question back ("Which Sarah?"), false when it
    must not be.
  - unanswerable: true when the sources don't answer it: the answer must cite nothing.

A question with no checks is "ungraded" and left to a person or a judge via key_points.
The type must be one of TYPES only when a question has checks, so the older hand-graded
sets in examples/qa (types person, deal, prep) still load.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

SOURCES = {"gmail", "drive", "fathom", "mixed"}
TYPES = {"relationship", "content", "mixed", "unanswerable", "clarify"}
EXPECT_KEYS = {"facts", "absent", "documents", "documents_match", "tools", "clarify", "unanswerable"}
CHECKS = ["facts", "absent", "documents", "tools", "clarify", "unanswerable"]

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def _norm(value: str) -> str:
    value = value.casefold().replace("’", "'").replace("‘", "'")
    return " " + " ".join(re.sub(r"[^\w]+", " ", value).split()) + " "


def _forms(fact: str) -> list[str]:
    """A fact and the other ways an answer may write it (dates only)."""
    m = _ISO.match(fact.strip())
    if not m:
        return [fact]
    try:
        d = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return [fact]
    month = _MONTHS[d.month - 1]
    return [fact, f"{month} {d.day}, {d.year}", f"{month} {d.day} {d.year}", f"{d.day} {month} {d.year}",
            f"{month[:3]} {d.day}, {d.year}", f"{d.day} {month[:3]} {d.year}", f"{d.month}/{d.day}/{d.year}"]


def contains(text: str, fact: str | list[str]) -> bool:
    """Whether text states the fact (any alternative), ignoring case and punctuation, on
    word boundaries: "86" does not match "186"."""
    haystack = _norm(text)
    alternatives = fact if isinstance(fact, list) else [fact]
    return any(_norm(form) in haystack for alt in alternatives for form in _forms(alt) if _norm(form).strip())


def _cited(document: str, citations: list[dict]) -> bool:
    """A document is an Atlas document id or an exact title (case aside). Titles keep a set
    readable and survive re-ingestion: a file's id depends on the path it was ingested from."""
    if _UUID.match(document):
        return any(c.get("document_id") == document.lower() for c in citations)
    return any((c.get("title") or "").casefold() == document.casefold() for c in citations)


def validate(questions: list[dict]) -> list[str]:
    """Problems with a golden set's format; empty when it is valid."""
    problems, seen = [], set()
    for i, q in enumerate(questions):
        where = f"question {q.get('id') or i}"
        for key in ("id", "question", "type"):
            if not q.get(key):
                problems.append(f"{where}: missing {key}")
        if q.get("id") in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(q.get("id"))
        if q.get("source") is not None and q["source"] not in SOURCES:
            problems.append(f"{where}: source must be one of {sorted(SOURCES)}")
        expect = q.get("expect") or {}
        if expect and q.get("type") and q["type"] not in TYPES:       # older hand-graded sets use their own types
            problems.append(f"{where}: type must be one of {sorted(TYPES)}")
        if unknown := set(expect) - EXPECT_KEYS:
            problems.append(f"{where}: unknown expect keys {sorted(unknown)}")
        for key in ("facts", "absent", "documents", "tools"):
            if key in expect and not isinstance(expect[key], list):
                problems.append(f"{where}: expect.{key} must be a list")
        if expect.get("documents_match", "all") not in ("all", "any"):
            problems.append(f"{where}: expect.documents_match must be all or any")
        if q.get("type") == "clarify" and expect.get("clarify") is not True:
            problems.append(f"{where}: a clarify question needs expect.clarify = true")
        if q.get("type") == "unanswerable" and expect.get("unanswerable") is not True:
            problems.append(f"{where}: an unanswerable question needs expect.unanswerable = true")
    return problems


def score(question: dict, result: dict) -> dict:
    """Grade one answer (the dict atlas ask returns). Each check given in `expect` is
    {"pass": bool, "detail": ...}; the question passes when every check does."""
    expect = question.get("expect") or {}
    checks: dict[str, dict] = {}
    if result.get("error"):
        return {"id": question["id"], "graded": True, "pass": False, "error": result["error"], "checks": {}}
    text = "\n".join([result.get("answer") or "", result.get("clarify") or ""])
    called = {t.get("tool") for t in result.get("tool_calls") or []}

    if "facts" in expect:
        missing = [f for f in expect["facts"] if not contains(text, f)]
        checks["facts"] = {"pass": not missing, "detail": {"missing": missing}}
    if "absent" in expect:
        present = [f for f in expect["absent"] if contains(text, f)]
        checks["absent"] = {"pass": not present, "detail": {"present": present}}
    if "documents" in expect:
        missing = [d for d in expect["documents"] if not _cited(d, result.get("citations") or [])]
        found = len(expect["documents"]) - len(missing)
        ok = not missing if expect.get("documents_match", "all") == "all" else found > 0
        checks["documents"] = {"pass": ok, "detail": {"missing": missing}}
    if "tools" in expect:
        missing = [t for t in expect["tools"] if t not in called]
        checks["tools"] = {"pass": not missing, "detail": {"missing": missing}}
    if "clarify" in expect:
        asked = bool(result.get("clarify"))
        checks["clarify"] = {"pass": asked == bool(expect["clarify"]), "detail": {"asked": asked}}
    if expect.get("unanswerable"):
        checks["unanswerable"] = {"pass": not result.get("citations") and not result.get("clarify"),
                                  "detail": {"cited": len(result.get("citations") or [])}}
    graded = bool(checks)
    return {"id": question["id"], "graded": graded, "pass": graded and all(c["pass"] for c in checks.values()),
            "checks": checks}


def summarize(questions: list[dict], scored: dict[str, list[dict]]) -> dict:
    """Pass rates overall, by source and by type, and per check. `scored` maps a question id
    to its scores over repeated runs; a question's pass rate is the share of runs that
    passed, so a question that passes 2 of 3 runs counts 0.67."""
    by_id = {q["id"]: q for q in questions}
    groups: dict[str, dict[str, list[float]]] = {"overall": defaultdict(list), "source": defaultdict(list),
                                                  "type": defaultdict(list), "check": defaultdict(list)}
    ungraded, flaky, failing = [], [], []
    for qid, runs in scored.items():
        runs = [r for r in runs if r["graded"]]
        if not runs:
            ungraded.append(qid)
            continue
        rate = sum(r["pass"] for r in runs) / len(runs)
        q = by_id.get(qid, {})
        groups["overall"]["all"].append(rate)
        groups["source"][q.get("source") or "unknown"].append(rate)
        groups["type"][q.get("type") or "unknown"].append(rate)
        for name in CHECKS:
            results = [r["checks"][name]["pass"] for r in runs if name in r["checks"]]
            if results:
                groups["check"][name].append(sum(results) / len(results))
        if rate == 0:
            failing.append(qid)
        elif rate < 1:
            flaky.append(qid)

    def table(values: dict[str, list[float]]) -> dict:
        return {k: {"questions": len(v), "pass_rate": round(sum(v) / len(v), 3)} for k, v in sorted(values.items())}

    return {"overall": table(groups["overall"]).get("all", {"questions": 0, "pass_rate": None}),
            "by_source": table(groups["source"]), "by_type": table(groups["type"]),
            "by_check": table(groups["check"]), "failing": sorted(failing), "flaky": sorted(flaky),
            "ungraded": sorted(ungraded)}
