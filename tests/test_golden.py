"""The golden-set scorer: pure functions over saved answers, no database or model."""

from __future__ import annotations

import json
from pathlib import Path

from atlas.retrieval.golden import contains, score, summarize, validate

ROOT = Path(__file__).resolve().parents[1]
DOC = "380cef24-048d-5a63-a8d7-0b490f333b8d"


def answer(text="", citations=(), tools=(), clarify=None, error=None) -> dict:
    return {"answer": text, "citations": list(citations), "tool_calls": [{"tool": t} for t in tools],
            "clarify": clarify, "error": error}


def question(**expect) -> dict:
    return {"id": "q1", "source": "gmail", "type": "content", "question": "?", "expect": expect}


def test_contains_ignores_case_and_punctuation_on_word_boundaries():
    assert contains("They sent **86** emails.", "86")
    assert not contains("They sent 186 emails.", "86")
    assert contains("USD 120,000 for the pilot", "120,000")
    assert contains("Contact: Kristin Walsh.", "kristin walsh")
    assert contains("It was eighty-six.", ["86", "eighty-six"])


def test_iso_dates_match_written_forms():
    for written in ("2001-05-07", "May 7, 2001", "7 May 2001", "May 7 2001", "5/7/2001"):
        assert contains(f"Last email: {written}.", "2001-05-07"), written
    assert not contains("May 17, 2001", "2001-05-07")


def test_facts_absent_documents_and_tools():
    q = question(facts=["10am", "Tuesday"], absent=["Monday"], documents=[DOC], tools=["search"])
    good = answer("Tuesday 10am works [S1].", citations=[{"document_id": DOC, "title": "Re: Atlas Rollout"}],
                  tools=["find", "search"])
    s = score(q, good)
    assert s["graded"] and s["pass"]

    bad = answer("Monday at 9 [S1].", citations=[{"document_id": "other", "title": "Other"}], tools=["find"])
    s = score(q, bad)
    assert not s["pass"]
    assert s["checks"]["facts"]["detail"]["missing"] == ["10am", "Tuesday"]
    assert s["checks"]["absent"]["detail"]["present"] == ["Monday"]
    assert not s["checks"]["documents"]["pass"] and not s["checks"]["tools"]["pass"]


def test_documents_match_by_title_and_any():
    cited = [{"document_id": DOC, "title": "Re: Atlas Rollout"}]
    assert score(question(documents=["re: atlas rollout"]), answer("x", cited))["pass"]
    assert score(question(documents=[DOC.upper()]), answer("x", cited))["pass"]
    assert not score(question(documents=["Re: Atlas Rollout", "Atlas pilot plan"]), answer("x", cited))["pass"]
    assert score(question(documents=["Re: Atlas Rollout", "Atlas pilot plan"], documents_match="any"),
                 answer("x", cited))["pass"]


def test_clarify_and_unanswerable():
    q = {**question(clarify=True), "type": "clarify"}
    assert score(q, answer("Which Sarah do you mean?", clarify="Which Sarah do you mean?"))["pass"]
    assert not score(q, answer("Sarah Chen works at Acme [G1]."))["pass"]

    q = {**question(unanswerable=True), "type": "unanswerable"}
    assert score(q, answer("The sources do not say."))["pass"]
    assert not score(q, answer("It is USD 120,000 [S1].", citations=[{"document_id": DOC}]))["pass"]


def test_error_fails_and_no_checks_is_ungraded():
    assert not score(question(facts=["x"]), answer(error="no answer after 24 steps"))["pass"]
    s = score({"id": "q1", "type": "person", "question": "?"}, answer("anything"))
    assert not s["graded"] and not s["pass"]


def test_summarize_pass_rates_over_repeated_runs():
    qs = [{"id": "a", "source": "gmail", "type": "content", "question": "?", "expect": {"facts": ["x"]}},
          {"id": "b", "source": "fathom", "type": "content", "question": "?", "expect": {"facts": ["x"]}},
          {"id": "c", "type": "person", "question": "?"}]
    ok, no = answer("x"), answer("y")
    scored = {"a": [score(qs[0], ok), score(qs[0], ok)],
              "b": [score(qs[1], ok), score(qs[1], no), score(qs[1], no)],
              "c": [score(qs[2], ok)]}
    s = summarize(qs, scored)
    assert s["overall"] == {"questions": 2, "pass_rate": round((1 + 1 / 3) / 2, 3)}
    assert s["by_source"]["gmail"]["pass_rate"] == 1.0
    assert s["by_source"]["fathom"]["pass_rate"] == 0.333
    assert s["flaky"] == ["b"] and s["failing"] == [] and s["ungraded"] == ["c"]


def test_validate_catches_format_mistakes():
    assert validate([{"id": "a", "type": "content", "question": "?", "expect": {"fact": ["x"]}}]) == [
        "question a: unknown expect keys ['fact']"]
    problems = validate([{"id": "a", "source": "email", "type": "clarify", "question": "?", "expect": {}},
                         {"id": "a", "type": "content", "question": "?", "expect": {"facts": "x"}}])
    assert "question a: source must be one of ['drive', 'fathom', 'gmail', 'mixed']" in problems
    assert "question a: a clarify question needs expect.clarify = true" in problems
    assert "question a: duplicate id" in problems
    assert "question a: expect.facts must be a list" in problems


def test_shipped_question_files_are_valid():
    for path in sorted((ROOT / "examples" / "qa").glob("*.json")):
        assert validate(json.loads(path.read_text())) == [], path.name
