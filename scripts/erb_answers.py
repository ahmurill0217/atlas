"""Answer EnterpriseRAG-Bench questions with `atlas ask` and write them in the benchmark's format.

    source runs/erb/env.sh
    uv run python scripts/erb_answers.py runs/erb/questions.jsonl runs/erb/answers --modes agent,baseline

Writes <out>/<mode>.jsonl, one {"question_id", "answer", "document_ids"} per line, for the
benchmark's scorer (src/scripts/answer_evaluation/metrics_based_eval.py), and <out>/<mode>.full.json
with everything `atlas ask` returned. document_ids are the benchmark ids (dsid) of the documents the
answer cites: a Drive or HubSpot document's external id is its dsid, an email's is "<dsid>#<n>".
Citation markers ([G#]/[S#], brain's [[n]]()) are removed from the answer text. Resumable.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer
from sqlalchemy import text

from atlas.cli import run_ask
from atlas.db.session import session_scope

app = typer.Typer(add_completion=False)
_MARKER = re.compile(r"(?:\s*(?:\[[GS]\d+\]|\[\[\d+\]\]\([^)]*\)))+")          # [S1] [G2], brain's [[1]]()


def dsids(document_ids: list[str]) -> list[str]:
    if not document_ids:
        return []
    with session_scope() as s:
        rows = dict(s.execute(text("SELECT id::text, source_external_id FROM kg.documents WHERE id::text = ANY(:ids)"),
                              {"ids": list(document_ids)}).all())
    out = []
    for d in document_ids:
        dsid = (rows.get(d) or "").split("#")[0]
        if dsid and dsid not in out:
            out.append(dsid)
    return out


@app.command()
def main(questions: Path, out: Path, modes: str = "agent,baseline", workers: int = 4,
         only: str = typer.Option("", help="Comma-separated question ids")):
    qs = [json.loads(line) for line in questions.read_text().splitlines() if line.strip()]
    wanted = {q.strip() for q in only.split(",") if q.strip()}
    out.mkdir(parents=True, exist_ok=True)
    for mode in [m.strip() for m in modes.split(",") if m.strip()]:
        full_path = out / f"{mode}.full.json"
        full = json.loads(full_path.read_text()) if full_path.exists() else {}
        todo = [q for q in qs if (not wanted or q["question_id"] in wanted)
                and (q["question_id"] not in full or full[q["question_id"]].get("error"))]
        lock = threading.Lock()

        def one(q):
            try:
                r = run_ask(q["question"], mode=mode)
            except Exception as exc:                  # one failed question must not end the run
                r = {"answer": "", "citations": [], "error": f"{type(exc).__name__}: {exc}", "seconds": None}
            r["document_ids"] = dsids([c["document_id"] for c in r.get("citations") or [] if c.get("document_id")])
            with lock:
                full[q["question_id"]] = {**r, "question": q["question"], "question_type": q["question_type"]}
                full_path.write_text(json.dumps(full, indent=1, default=str))
                gold = set(q["expected_doc_ids"])
                typer.echo(f"{mode:8s} {q['question_id']} {q['question_type'][:12]:12s} "
                           f"docs={len(r['document_ids']):2d} gold_hit={bool(gold & set(r['document_ids']))!s:5s} "
                           f"{r.get('seconds') or 0:5.1f}s {('ERROR ' + r['error']) if r.get('error') else ''}")

        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(one, todo))
        with (out / f"{mode}.jsonl").open("w") as f:
            for q in qs:
                r = full.get(q["question_id"])
                if r and not r.get("error"):                  # unanswered is left out, not scored as wrong
                    f.write(json.dumps({"question_id": q["question_id"],
                                        "answer": _MARKER.sub("", r.get("answer") or "").strip(),
                                        "document_ids": r["document_ids"]}) + "\n")


if __name__ == "__main__":
    app()
