"""Score saved answers (from scripts/evaluate_ask.py) against a golden set's `expect`
checks and print pass rates by source, type and check. Needs no model or database.

    uv run python scripts/score_ask.py examples/qa/golden_template.json runs/golden/results.json \
        --mode agent --out runs/golden/scores.json

Exits 1 when the overall pass rate is below --min-pass, so it can gate CI.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import typer

from atlas.retrieval.golden import score, summarize, validate

app = typer.Typer(add_completion=False)


@app.command()
def main(questions: Path, results: Path, mode: str = typer.Option("agent", help="Which mode's answers to score"),
         out: Path = typer.Option(None, help="Write per-answer scores and the summary here"),
         min_pass: float = typer.Option(0.0, help="Fail (exit 1) below this overall pass rate, 0 to 1")):
    qs = json.loads(questions.read_text())
    if problems := validate(qs):
        raise typer.BadParameter("\n".join(problems), param_hint="questions")
    by_id = {q["id"]: q for q in qs}
    saved = json.loads(results.read_text())
    scored: dict[str, list[dict]] = defaultdict(list)
    for key, r in saved.items():
        qid, key_mode = key.split("#")[0].rsplit(":", 1)
        if key_mode == mode and qid in by_id:
            scored[qid].append({**score(by_id[qid], r), "key": key})
    unanswered = sorted(set(by_id) - set(scored))
    summary = summarize(qs, scored) | {"mode": mode, "unanswered": unanswered}

    overall = summary["overall"]
    typer.echo(f"Overall: {overall['pass_rate']} over {overall['questions']} graded questions "
               f"({len(summary['ungraded'])} ungraded, {len(unanswered)} with no saved answer)")
    for title, table in (("Source", summary["by_source"]), ("Type", summary["by_type"]),
                         ("Check", summary["by_check"])):
        typer.echo(f"\n{title:14s} questions  pass rate")
        for name, row in table.items():
            typer.echo(f"  {name:12s} {row['questions']:9d}  {row['pass_rate']:.2f}")
    for label in ("failing", "flaky"):
        if summary[label]:
            typer.echo(f"\n{label.capitalize()}: {', '.join(summary[label])}")
    for qid in summary["failing"] + summary["flaky"]:
        for s in scored[qid]:
            if not s["pass"]:
                why = s.get("error") or "; ".join(f"{name}: {c['detail']}" for name, c in s["checks"].items()
                                                  if not c["pass"])
                typer.echo(f"  {s['key']:18s} {why}")
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "scores": scored}, indent=1))
    if overall["pass_rate"] is not None and overall["pass_rate"] < min_pass:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
