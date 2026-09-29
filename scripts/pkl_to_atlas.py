"""The persistent-knowledge-layer demo corpus -> Atlas input files.

https://github.com/mcekikj/persistent-knowledge-layer (MIT) ships 21 synthetic insurance
documents built to trap plain retrieval (scoped rules, a live contradiction, superseded
versions, effective dates, rationale buried in an email). This converts them so the same
traps can be put to `atlas ask` (questions: examples/qa/pkl_trap_questions.json).

  - the two email threads become one email JSON per message, with names only: the corpus
    has no addresses, and inventing some would give the graph identities it can't have;
  - everything else becomes an Atlas document JSON dated by its effective, loss or meeting
    date, so dates reach the graph and search headers instead of only the body text.

The text is kept whole, front matter included, so version and supersession lines stay
visible to search.

    git clone https://github.com/mcekikj/persistent-knowledge-layer ../persistent-knowledge-layer
    uv run python scripts/pkl_to_atlas.py ../persistent-knowledge-layer runs/pkl/corpus
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

SOURCE_SYSTEM = "pkl"
_FIELD = re.compile(r"^([A-Za-z_ ]+):\s*(.+?)\s*$")
_DATE_KEYS = ("effective_date", "effective date", "loss_date", "meeting_date", "date")
_RULE = re.compile(r"^-{20,}\s*$", re.MULTILINE)
_HEADER = re.compile(r"^(From|To|Cc|Subject|Date):\s*(.*?)\s*$")


def header_fields(text: str) -> dict[str, str]:
    """The key: value lines at the top of a file (YAML front matter or a plain header)."""
    fields = {}
    for line in text.splitlines()[:15]:
        if m := _FIELD.match(line.strip()):
            fields.setdefault(m[1].strip().lower(), m[2].strip())
    return fields


def document_date(fields: dict[str, str]) -> str | None:
    for key in _DATE_KEYS:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields.get(key, "")):
            return f"{fields[key]}T00:00:00Z"
    return None


def person(value: str) -> dict:
    """"E. Vasquez (Portfolio Analytics)" -> {"name": "E. Vasquez"}; no address is invented."""
    return {"name": re.sub(r"\s*\(.*\)\s*$", "", value).strip()}


def emails(text: str, doc_id: str) -> list[dict]:
    """One email JSON per message of a thread file: a header block between two rules, then
    the body up to the next rule."""
    blocks = [b.strip("\n") for b in _RULE.split(text)]
    out = []
    for header, body in zip(blocks[1::2], blocks[2::2]):
        fields = {}
        for line in header.splitlines():
            if m := _HEADER.match(line.strip()):
                fields[m[1].lower()] = m[2]
        if "from" not in fields:
            continue
        sent = datetime.strptime(fields["date"], "%d %B %Y, %H:%M")
        out.append({"source_system": SOURCE_SYSTEM, "message_id": f"{doc_id}-{len(out) + 1}", "thread_id": doc_id,
                    "subject": fields.get("subject"), "from": person(fields["from"]),
                    "to": [person(p) for p in fields.get("to", "").split(",") if p.strip()],
                    "cc": [person(p) for p in fields.get("cc", "").split(",") if p.strip()],
                    "date": sent.isoformat() + "Z", "body": body.strip()})
    return out


def document(text: str, fields: dict[str, str], doc_id: str, filename: str) -> dict:
    return {"atlas_document": "1.0", "source_system": SOURCE_SYSTEM, "id": doc_id, "source_type": "document",
            "title": fields.get("title") or filename, "created_at": document_date(fields), "text": text,
            "metadata": {"filename": filename, **{k: v for k, v in fields.items()
                                                  if k in ("version", "status", "superseded_by", "superseded_on",
                                                           "supersedes", "case_id")}}}


@app.command()
def main(repo: Path, out: Path):
    raw = repo / "demo_data" / "raw"
    if not raw.is_dir():
        raise typer.BadParameter(f"{raw} not found; pass the root of a persistent-knowledge-layer clone")
    out.mkdir(parents=True, exist_ok=True)
    counts = {"emails": 0, "documents": 0}
    for path in sorted(raw.iterdir()):
        text = path.read_text(encoding="utf-8")
        fields = header_fields(text)
        doc_id = fields.get("document_id") or fields.get("document id") or path.stem
        messages = emails(text, doc_id) if "email" in path.stem else []
        for m in messages:
            (out / f"{m['message_id']}.json").write_text(json.dumps(m, indent=1))
        if messages:
            counts["emails"] += len(messages)
        else:
            (out / f"{doc_id}.json").write_text(json.dumps(document(text, fields, doc_id, path.name), indent=1))
            counts["documents"] += 1
    typer.echo(f"{counts['documents']} documents and {counts['emails']} emails written to {out}")


if __name__ == "__main__":
    app()
