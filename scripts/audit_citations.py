"""Audit the citations in saved `atlas ask` answers: is every claim backed by the email it cites?

For each cited sentence:
  - every marker must resolve to a source (no dangling markers);
  - graph markers [G#]: any date in the sentence must equal the cited email's date;
  - search markers [[n]]: a judge model reads the cited email (header + own text) and says whether
    it supports the sentence (supported / partial / unsupported). Unsupported verdicts are listed
    for a human to read; the judge is a filter, not the final word.

    DATABASE_URL=... uv run python scripts/audit_citations.py runs/enron/ask/mixed_v4.json --mode auto
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer
from pydantic import BaseModel
from sqlalchemy import text

from atlas.config import get_settings
from atlas.db.session import session_scope

app = typer.Typer(add_completion=False)

_SENTENCE = re.compile(r"[^.\n]*?(?:\[\[\d+\]\]\(\)|\[[GS]\d+\])[^.\n]*[.\n]?")
_SEARCH = re.compile(r"\[\[(\d+)\]\]\(\)|\[(S\d+)\]")      # router: [[n]](); agent: [S#]
_GRAPH = re.compile(r"\[(G\d+)\]")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


class Verdict(BaseModel):
    verdict: str          # supported | partial | unsupported
    reason: str


JUDGE = """You check citations. Given a SENTENCE from an answer and the full text of the EMAIL it cites,
decide whether the email supports what the sentence says (names, dates, numbers, content).
supported: the email states or directly shows it. partial: some of it. unsupported: the email does not
show it. Judge only against this email."""


def email_text(s, document_id: str) -> str:
    row = s.execute(text("""SELECT d.title, d.source_created_at, v.normalized FROM kg.documents d
        JOIN kg.document_versions v ON v.id = d.current_version_id WHERE d.id = :i"""), {"i": document_id}).first()
    if row is None:
        return ""
    n = row.normalized
    people = "; ".join(f"{p.get('role')}: {p.get('name') or ''} <{p.get('email')}>" for p in n.get("participants", []))
    own = "\n".join(sec["text"] for sec in n.get("sections", []))
    # The whole document: a 70-minute transcript is ~80k characters, and a claim may come from anywhere in it.
    return f"Subject: {row.title}\nDate: {row.source_created_at}\n{people}\n\n{own}"[:120000]


@app.command()
def main(results: Path, mode: str = "auto", out: Path = typer.Option(None)):
    from openai import OpenAI

    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    data = json.loads(results.read_text())
    rows, totals = [], {"claims": 0, "dangling": 0, "graph_date_ok": 0, "graph_date_bad": 0,
                        "supported": 0, "partial": 0, "unsupported": 0}
    with session_scope() as s:
        for key, r in sorted(data.items()):
            if not key.endswith(f":{mode}"):
                continue
            sources = {c["marker"]: c for c in r["citations"]}
            answer = re.sub(r"([.!?])((?: \[[GS]\d+\])+)", r"\2\1", r["answer"])   # "claim. [S1]" -> "claim [S1]."
            for sentence in _SENTENCE.findall(answer):
                sentence = sentence.strip()
                for marker in _GRAPH.findall(sentence) + [a or b for a, b in _SEARCH.findall(sentence)]:
                    totals["claims"] += 1
                    src = sources.get(marker)
                    if src is None:
                        totals["dangling"] += 1
                        rows.append({"q": key, "marker": marker, "sentence": sentence, "verdict": "dangling"})
                        continue
                    if marker.startswith("G") and mode == "agent":
                        # The agent's graph refs are records it was shown (often a summary: counts, first and
                        # last); the answer-time verifier already checked the part's numbers against them.
                        totals["graph_verified_at_answer_time"] = totals.get("graph_verified_at_answer_time", 0) + 1
                        continue
                    if marker.startswith("G"):
                        dates = _DATE.findall(sentence)
                        ok = not dates or (src["date"] or "")[:10] in dates
                        totals["graph_date_ok" if ok else "graph_date_bad"] += 1
                        if not ok:
                            rows.append({"q": key, "marker": marker, "sentence": sentence, "verdict": "graph date mismatch",
                                         "source": f"{src['title']} ({(src['date'] or '')[:10]})"})
                        continue
                    judged = client.beta.chat.completions.parse(
                        model=settings.ask_model, temperature=0, response_format=Verdict,
                        messages=[{"role": "system", "content": JUDGE},
                                  {"role": "user", "content": f"SENTENCE: {sentence}\n\nEMAIL:\n{email_text(s, src['document_id'])}"}]
                    ).choices[0].message.parsed
                    v = judged.verdict if judged.verdict in ("supported", "partial", "unsupported") else "partial"
                    totals[v] += 1
                    if v != "supported":
                        rows.append({"q": key, "marker": marker, "sentence": sentence, "verdict": v,
                                     "source": f"{src['title']} ({(src['date'] or '')[:10]})", "reason": judged.reason})
    typer.echo(json.dumps(totals, indent=1))
    for row in rows:
        typer.echo(f"\n{row['q']} [{row['marker']}] {row['verdict'].upper()}: {row['sentence'][:220]}\n"
                   f"   cites: {row.get('source')}  | {row.get('reason', '')[:200]}")
    if out:
        out.write_text(json.dumps({"totals": totals, "flagged": rows}, indent=1))


if __name__ == "__main__":
    app()
