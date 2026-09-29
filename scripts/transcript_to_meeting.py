"""Wrap a copied Fathom transcript (.docx / .txt / .md) into the Fathom meeting JSON the
meeting adapter reads, so the graph gets the meeting's attendees, speakers and date.

A copied transcript keeps the title line, the recording link and the speaker turns
("0:13 - Joe Finnell"), but not attendees' emails or the year. Emails come from
--attendee "Name <email>" or, for everyone else who spoke, --domain (first.last@domain).
A speaker label's parenthetical ("Angel Murillo (Alloy Therapeutics)") is dropped from the name.

    uv run python scripts/transcript_to_meeting.py "runs/work/meetings/Darwin Standup.docx" \\
        runs/work/meetings_json --date 2026-09-23 --domain alloytx.com

Production reads Fathom's API or webhook JSON directly; this is for transcripts copied by hand.
"""

from __future__ import annotations

import hashlib
import json
import re
from email.utils import parseaddr
from pathlib import Path

import typer

from atlas.ingestion.adapters import normalize_file

app = typer.Typer(add_completion=False)

_TURN = re.compile(r"(?:^|(?<=\s))(\d{1,2}(?::\d{2}){1,2}) - ([^\n]{2,80})\n")
_LINK = re.compile(r"https://fathom\.video/\S+")


def _seconds(stamp: str) -> int:
    total = 0
    for part in stamp.split(":"):
        total = total * 60 + int(part)
    return total


def turns(text: str) -> list[tuple[str, str, str]]:
    """(timestamp, speaker label, text) per speaker turn, in order."""
    marks = list(_TURN.finditer(text))
    return [(m.group(1), m.group(2).strip(), " ".join(text[m.end(): marks[i + 1].start() if i + 1 < len(marks)
                                                         else len(text)].split()))
            for i, m in enumerate(marks)]


def _name(label: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()


def _email(name: str, domain: str | None) -> str | None:
    words = re.sub(r"[^a-z ]", "", name.lower()).split()
    return f"{words[0]}.{words[-1]}@{domain}" if domain and len(words) >= 2 else None


@app.command()
def main(transcript: Path, out_dir: Path,
         date: str = typer.Option(..., help="Meeting date, YYYY-MM-DD (a copied transcript has no year)"),
         domain: str = typer.Option(None, help="Email domain for speakers without --attendee: first.last@domain"),
         attendee: list[str] = typer.Option([], help='"Name <email>", repeatable; also for people who did not speak'),
         title: str = typer.Option(None, help="Defaults to the transcript's first line")):
    text = normalize_file(transcript).raw_text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = title or re.sub(r"\s+-\s+[A-Z][a-z]+ \d{1,2}$", "", lines[0])
    link = (_LINK.search(text) or [None])[0]
    spoken = turns(text)

    emails = {name: addr.lower() for name, addr in (parseaddr(a) for a in attendee) if addr}
    labels = list(dict.fromkeys(label for _, label, _ in spoken))
    invitees = [{"name": _name(label), "email": emails.get(_name(label)) or _email(_name(label), domain),
                 "matched_speaker_display_name": label} for label in labels]
    invitees += [{"name": name, "email": addr, "matched_speaker_display_name": None}
                 for name, addr in emails.items() if name not in {i["name"] for i in invitees}]
    for inv in invitees:
        inv["email_domain"] = inv["email"].split("@")[1] if inv["email"] else None
        inv["is_external"] = bool(domain and inv["email_domain"] and inv["email_domain"] != domain)
    by_label = {i["matched_speaker_display_name"]: i["email"] for i in invitees}

    length = max((_seconds(t) for t, _, _ in spoken), default=0)
    payload = {
        "source_system": "fathom",
        "title": title, "meeting_title": title,
        "recording_id": hashlib.sha256((link or str(transcript)).encode()).hexdigest()[:16],
        "url": link, "share_url": link,
        "created_at": f"{date}T00:00:00Z",
        "recording_start_time": f"{date}T00:00:00Z",
        "calendar_invitees_domains_type": "only_internal" if not any(i["is_external"] for i in invitees)
                                          else "one_or_more_external",
        "calendar_invitees": invitees,
        "recorded_by": None,
        "transcript": [{"speaker": {"display_name": label, "matched_calendar_invitee_email": by_label.get(label)},
                        "text": said, "timestamp": stamp} for stamp, label, said in spoken if said],
        "default_summary": None,
        "action_items": [],
        "length_seconds": length,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{transcript.stem}.json"
    target.write_text(json.dumps(payload, indent=1))
    typer.echo(f"{target}: {len(payload['transcript'])} turns, speakers: "
               + ", ".join(f"{i['name']} <{i['email']}>" for i in invitees))


if __name__ == "__main__":
    app()
