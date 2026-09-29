"""Question-answering accuracy: brain (hybrid + vector-only) vs. Cognee.

1. Answers every question in examples/qa/questions.json with `brain ask` in
   hybrid mode (chunks + graph facts) and vector mode (chunks only = plain
   RAG baseline), `--repeats` times, against the current brain database.
2. Loads Cognee's answers produced by scripts/cognee_qa.py (optional).
3. Grades every answer blind (the judge never sees which system wrote it)
   against the reference answer with a stronger judge model.

    references/cognee-venv/bin/python scripts/cognee_qa.py --out runs/qa/cognee_answers.json
    uv run python scripts/evaluate_qa.py --cognee-answers runs/qa/cognee_answers.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel  # noqa: E402

from brain_v0.config import get_settings  # noqa: E402
from brain_v0.db import session_scope  # noqa: E402
from brain_v0.embeddings import get_embedder  # noqa: E402
from brain_v0.llm import get_llm  # noqa: E402
from brain_v0.llm.openai_llm import OpenAIStructuredLLM  # noqa: E402
from brain_v0.qa import ask  # noqa: E402

JUDGE_PROMPT = """\
You grade answers to questions about a small document collection.
You get the question, a reference answer written from the documents, and a candidate answer.

Verdicts:
- correct: the candidate contains all the key facts of the reference and nothing that contradicts it.
  Extra detail is fine if it is not wrong. Wording and aliases may differ (e.g. AMG 510 = sotorasib).
- partial: some but not all key facts, or correct key facts plus a clearly wrong claim.
- incorrect: key facts missing or wrong, or no real answer.

For questions whose reference says the documents do not contain the answer: `correct` only if the
candidate says it cannot answer / the information is not available. Any specific made-up answer is
`incorrect`.
"""

SCORE = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}


class Grade(BaseModel):
    verdict: Literal["correct", "partial", "incorrect"]
    reason: str


def generate_brain_answers(questions: list[dict], modes: list[str], repeats: int, top_k: int,
                           workers: int = 4, arm_suffix: str = "", done: set | None = None) -> list[dict]:
    """Answers for (question, mode, repeat) jobs not in `done`. A failed call is
    recorded with `error` (and not graded) so a re-run can fill it in."""
    embedder, llm = get_embedder(), get_llm()

    def one(job):
        q, mode, repeat = job
        base = {"id": q["id"], "arm": f"brain_{mode}{arm_suffix}", "repeat": repeat}
        try:
            with session_scope() as session:
                r = ask(session, embedder, llm, q["question"], mode, top_k)
        except Exception as exc:
            return {**base, "answer": "", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        return {**base, "answer": r.answer, "answerable": r.answerable, "citations": r.citations,
                "invalid_citations": r.invalid_citations, "error": None,
                "context_tokens_approx": sum(len(c.text) for c in r.context) // 4}

    done = done or set()
    jobs = [(q, m, r) for m in modes for r in range(1, repeats + 1) for q in questions
            if (q["id"], f"brain_{m}{arm_suffix}", r) not in done]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, jobs))


def grade_all(answers: list[dict], questions: dict[str, dict], judge_model: str) -> None:
    settings = get_settings()
    # Low concurrency + generous SDK retries (exponential backoff): judge models
    # often have tight tokens-per-minute limits.
    judge = OpenAIStructuredLLM(judge_model, settings.openai_api_key, temperature=0.0, max_retries=12)

    def one(a):
        q = questions[a["id"]]
        if not a["answer"].strip():
            return Grade(verdict="incorrect", reason="empty answer")
        user = (f"Question: {q['question']}\nReference answer: {q['reference']}\n"
                f"Candidate answer: {a['answer']}")
        return judge.generate(JUDGE_PROMPT, user, Grade)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for a, g in zip(answers, pool.map(one, answers)):
            a["verdict"], a["judge_reason"] = g.verdict, g.reason


def summarize(answers: list[dict], questions: dict[str, dict]) -> dict:
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for a in answers:
        by_arm[a["arm"]].append(a)
    summary = {}
    for arm, rows in sorted(by_arm.items()):
        per_cat: dict[str, list[float]] = defaultdict(list)
        verdicts_by_q: dict[str, list[str]] = defaultdict(list)
        for a in rows:
            per_cat[questions[a["id"]]["category"]].append(SCORE[a["verdict"]])
            verdicts_by_q[a["id"]].append(a["verdict"])
        scores = [SCORE[a["verdict"]] for a in rows]
        summary[arm] = {
            "score": round(sum(scores) / len(scores), 3),
            "verdicts": dict(Counter(a["verdict"] for a in rows)),
            "by_category": {c: round(sum(v) / len(v), 3) for c, v in sorted(per_cat.items())},
            "same_verdict_all_repeats": round(
                sum(1 for v in verdicts_by_q.values() if len(set(v)) == 1) / len(verdicts_by_q), 3),
            "answers_with_invalid_citations": sum(1 for a in rows if a.get("invalid_citations")),
            "errors": sum(1 for a in rows if a.get("error")),
            "context_tokens_mean": (round(sum(a["context_tokens_approx"] for a in rows if "context_tokens_approx" in a)
                                          / max(1, sum(1 for a in rows if "context_tokens_approx" in a)))
                                    if any("context_tokens_approx" in a for a in rows) else None),
        }
    return summary


def render(summary: dict, answers: list[dict], questions: dict[str, dict], meta: dict) -> str:
    arms = list(summary)
    cats = sorted({c for s in summary.values() for c in s["by_category"]})
    lines = ["# QA evaluation", ""] + [f"- {k}: `{v}`" for k, v in meta.items()] + [""]
    lines += ["| arm | score | correct / partial / incorrect | same verdict across repeats | context tokens (approx) |",
              "|---|---|---|---|---|"]
    for arm in arms:
        s, v = summary[arm], summary[arm]["verdicts"]
        lines.append(f"| {arm} | {s['score']:.2f} | {v.get('correct', 0)} / {v.get('partial', 0)} / "
                     f"{v.get('incorrect', 0)} | {s['same_verdict_all_repeats']:.2f} | {s['context_tokens_mean'] or 'n/a'} |")
    lines += ["", "## Score by category", "", "| category | " + " | ".join(arms) + " |",
              "|---|" + "---|" * len(arms)]
    for c in cats:
        n = sum(1 for q in questions.values() if q["category"] == c)
        lines.append(f"| {c} ({n}) | " + " | ".join(f"{summary[a]['by_category'].get(c, 0):.2f}" for a in arms) + " |")
    lines += ["", "## Non-correct answers", ""]
    for a in sorted(answers, key=lambda a: (a["id"], a["arm"], a["repeat"])):
        if a["verdict"] != "correct":
            lines.append(f"- **{a['id']}** `{a['arm']}` r{a['repeat']} **{a['verdict']}**: "
                         f"{questions[a['id']]['question']}\n  - answer: {a['answer'][:300]!r}\n"
                         f"  - judge: {a['judge_reason'][:300]}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", default="examples/qa/questions.json")
    parser.add_argument("--cognee-answers", default=None)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--modes", default="hybrid,vector")
    parser.add_argument("--judge-model", default="gpt-4o")
    parser.add_argument("--out", default=None)
    parser.add_argument("--workers", type=int, default=4, help="Concurrent answer generations.")
    parser.add_argument("--arm-suffix", default="", help="Appended to brain arm names, e.g. '_k8'.")
    parser.add_argument("--reuse-answers", action="store_true",
                        help="Grade the answers already saved in --out instead of generating new ones.")
    args = parser.parse_args()

    qlist = json.loads(Path(args.questions).read_text())
    questions = {q["id"]: q for q in qlist}
    out = Path(args.out or f"runs/qa/{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)

    answers = json.loads((out / "answers.json").read_text()) if (out / "answers.json").exists() else []
    answers = [a for a in answers if not a.get("error")]  # failed generations are retried
    if not args.reuse_answers:
        done = {(a["id"], a["arm"], a["repeat"]) for a in answers}
        answers += generate_brain_answers(qlist, [m for m in args.modes.split(",") if m], args.repeats,
                                          args.top_k, args.workers, args.arm_suffix, done)
        failed = sum(1 for a in answers if a.get("error"))
        if failed:
            print(f"{failed} answers failed to generate (see answers.json); re-run to retry them")
    if args.cognee_answers:  # merge in answers for arms not already present
        present = {a["arm"] for a in answers}
        answers += [a for a in json.loads(Path(args.cognee_answers).read_text()) if a["arm"] not in present]
    (out / "answers.json").write_text(json.dumps(answers, indent=2))  # saved before grading
    # Grade only successful answers not graded yet (grades are kept in answers.json).
    grade_all([a for a in answers if "verdict" not in a and not a.get("error")], questions, args.judge_model)
    (out / "answers.json").write_text(json.dumps(answers, indent=2))
    graded = [a for a in answers if "verdict" in a]

    meta = {"questions": len(qlist), "repeats": args.repeats,
            "answer_model": get_settings().llm_model, "judge_model": args.judge_model,
            "cognee_answers": args.cognee_answers}
    summary = summarize(graded, questions)
    (out / "answers.json").write_text(json.dumps(answers, indent=2))
    (out / "summary.json").write_text(json.dumps({"meta": meta, **summary}, indent=2))
    (out / "report.md").write_text(render(summary, graded, questions, meta))
    for arm, s in summary.items():
        print(f"{arm:32s} score={s['score']:.2f}  {s['verdicts']}  consistent={s['same_verdict_all_repeats']:.2f}")
    print(f"\nWrote {out}/report.md")


if __name__ == "__main__":
    main()
