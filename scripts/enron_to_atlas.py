"""Convert the public Enron corpus (Hugging Face `corbt/enron-emails` parquet)
into Atlas email JSON files, one per unique message.

The corpus stores one row per mailbox *copy*: the same email sits in the
sender's sent folder and in every recipient's inbox, each with its own
synthetic message_id. We keep one copy per (from, date, subject, body) and
record every mailbox it was found in, the way a real Gmail connector would
see one Message-ID across several users' mailboxes.

    uv run python scripts/enron_to_atlas.py runs/enron/train-00000.parquet runs/enron/json --limit 20000
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq
import typer

app = typer.Typer(add_completion=False)


def _clean(addresses) -> list[str]:
    return [a.strip() for a in (addresses or []) if a and a.strip()]


def content_key(row: dict) -> str:
    raw = f"{row['from']}|{row['date']}|{row['subject']}|{row['body'] or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@app.command()
def main(parquet: Path, out_dir: Path,
         limit: int = typer.Option(0, help="Stop after this many unique messages (0 = all)"),
         mailboxes: str = typer.Option("", help="Comma-separated mailbox folders to keep, e.g. allen-p,beck-s")):
    keep = {m.strip() for m in mailboxes.split(",") if m.strip()}
    rows = pq.read_table(parquet).to_pylist()
    copies: dict[str, list[str]] = defaultdict(list)
    first: dict[str, dict] = {}
    for row in rows:
        box = row["file_name"].split("/")[0]
        if keep and box not in keep:
            continue
        key = content_key(row)
        copies[key].append(row["file_name"])
        if key not in first:
            if limit and len(first) >= limit:
                copies[key].pop()
                continue
            first[key] = row
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, row in first.items():
        box = row["file_name"].split("/")[0]
        payload = {
            "source_system": "enron",
            "message_id": key,
            "subject": row["subject"] or None,
            "from": row["from"],
            "to": _clean(row["to"]),
            "cc": _clean(row["cc"]),
            "date": row["date"].isoformat() if row["date"] else None,
            "body": row["body"] or "",
            "enron_copies": copies[key],
        }
        (out_dir / box).mkdir(exist_ok=True)
        (out_dir / box / f"{key}.json").write_text(json.dumps(payload))
    typer.echo(f"rows read: {len(rows)}  unique messages written: {len(first)}  "
               f"duplicate copies dropped: {sum(len(v) for v in copies.values()) - len(first)}")


if __name__ == "__main__":
    app()
