"""Ask every question in each mode and save the answers for grading. Modes are
the router's (atlas/retrieval/router.py): auto, relationship, content, baseline.
A question may carry "as" (the asker's email). Resumable: answers already saved
are kept.

    ATLAS_BRAIN_INDEX=atlas_enron DATABASE_URL=... uv run python scripts/evaluate_ask.py \
        examples/qa/enron_questions.json runs/enron/ask/results.json
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from atlas.cli import run_ask

app = typer.Typer(add_completion=False)


@app.command()
def main(questions: Path, out: Path, only: str = typer.Option("", help="Comma-separated question ids"),
         modes: str = typer.Option("agent,auto", help="agent, auto, relationship, content, baseline")):
    qs = json.loads(questions.read_text())
    wanted = {q.strip() for q in only.split(",") if q.strip()}
    results = json.loads(out.read_text()) if out.exists() else {}
    out.parent.mkdir(parents=True, exist_ok=True)
    for q in qs:
        if wanted and q["id"] not in wanted:
            continue
        for mode in [m.strip() for m in modes.split(",") if m.strip()]:
            key = f"{q['id']}:{mode}"
            if key in results and not results[key].get("error"):
                continue
            try:
                r = run_ask(q["question"], mode=mode, asker=q.get("as"))
            except Exception as exc:                  # one failed question must not end the run
                typer.echo(f"{key:14s} FAILED {type(exc).__name__}: {exc}")
                continue
            results[key] = {**r, "id": q["id"], "type": q["type"], "key_points": q["key_points"]}
            out.write_text(json.dumps(results, indent=1, default=str))
            typer.echo(f"{key:14s} route={r['route']:12s} cited={len(r['citations']):2d} "
                       f"{r['seconds']:5.1f}s {'ERROR ' + r['error'] if r['error'] else ''}")


if __name__ == "__main__":
    app()
