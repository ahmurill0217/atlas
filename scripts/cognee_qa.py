"""Answer the QA question set with Cognee (run in the Cognee venv).

Builds Cognee's graph once from the corpus (stock cognify, gpt-4o-mini), then
asks every question with each search mode, `--repeats` times. Each call gets a
fresh session_id so Cognee's session memory cannot carry answers between
questions. Everything else is Cognee's default (e.g. top_k=15).

    references/cognee-venv/bin/python scripts/cognee_qa.py --out runs/qa/cognee_answers.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cognee_baseline import ROOT, allow_special_tokens_in_text, configure_env  # noqa: E402

MODES = ["HYBRID_COMPLETION", "GRAPH_COMPLETION", "RAG_COMPLETION"]


def to_text(result) -> str:
    """cognee.search returns a list of result objects/strings; flatten to text."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(to_text(r) for r in result)
    for attr in ("search_result", "answer", "text"):
        if hasattr(result, attr):
            return to_text(getattr(result, attr))
    if isinstance(result, dict):
        for key in ("search_result", "answer", "text"):
            if key in result:
                return to_text(result[key])
    return str(result)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", default=str(ROOT / "examples" / "qa" / "questions.json"))
    parser.add_argument("--corpus", default=str(ROOT / "examples" / "corpus"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default=str(ROOT / "runs" / "qa" / "cognee_answers.json"))
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="Keep existing Cognee data; cognify skips already-completed documents.")
    parser.add_argument("--data-per-batch", type=int, default=None,
                        help="Documents cognified concurrently (Cognee default 20). Lower it on low rate-limit tiers.")
    parser.add_argument("--cognify-attempts", type=int, default=1,
                        help="Re-run cognify after a failure; incremental loading resumes where it stopped.")
    args = parser.parse_args()
    configure_env(args.model)
    allow_special_tokens_in_text()

    import cognee
    from cognee import SearchType

    if not args.skip_build:
        if not args.resume:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)
            files = sorted(str(p) for p in Path(args.corpus).iterdir() if p.suffix.lower() in {".md", ".txt", ".pdf"})
            if not files:
                raise SystemExit(f"no .md/.txt/.pdf files in {args.corpus}")
            print(f"adding {len(files)} files", flush=True)
            await cognee.add(files, dataset_name="corpus")
        kwargs = {"data_per_batch": args.data_per_batch} if args.data_per_batch else {}
        for attempt in range(1, args.cognify_attempts + 1):
            try:
                await cognee.cognify(datasets=["corpus"], **kwargs)
                break
            except Exception as exc:
                print(f"cognify attempt {attempt} failed: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
                if attempt == args.cognify_attempts:
                    raise
                await asyncio.sleep(60)  # let the rate-limit window reset
        print("cognify done", flush=True)

    questions = json.loads(Path(args.questions).read_text())[: args.limit]
    answers = []
    for mode in args.modes.split(","):
        for repeat in range(1, args.repeats + 1):
            for q in questions:
                try:
                    result = await cognee.search(query_text=q["question"], query_type=SearchType[mode],
                                                 datasets=["corpus"], session_id=f"qa-{uuid.uuid4()}")
                    text, error = to_text(result), None
                except Exception as exc:  # record and keep going
                    text, error = "", f"{type(exc).__name__}: {exc}"
                answers.append({"id": q["id"], "arm": f"cognee_{mode.lower()}", "repeat": repeat,
                                "answer": text, "error": error})
            print(f"{mode} repeat {repeat} done", flush=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(answers, indent=2))
    print(f"wrote {len(answers)} answers to {out}")


if __name__ == "__main__":
    asyncio.run(main())
