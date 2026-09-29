"""Convert a Google Takeout Gmail export (.mbox) into Atlas email JSON files, one per message.

Export: takeout.google.com -> "Deselect all" -> Mail -> "All Mail data included" (or pick labels)
-> export once, .zip. Unzip; the file is Takeout/Mail/"All mail Including Spam and Trash.mbox".

    uv run python scripts/takeout_to_atlas.py "Takeout/Mail/All mail Including Spam and Trash.mbox" \\
        runs/gmail/json --since 2025-01-01 --limit 5000

- Skipped by Gmail label (X-GM-LABELS): Spam, Trash, Chat; Promotions and Social too unless
  --keep-promotions (they are what a newsletter looks like; the graph ignores broadcast senders
  anyway, but they cost indexing time).
- --limit keeps the most recent messages; one copy per Message-ID.
- Body: the text/plain part, else the HTML part reduced to text.
- Attachments: name and type are recorded on the email. With --attachments DIR, PDF and DOCX
  files are saved there too, for `atlas ingest DIR`.
- uri: a Gmail search link for the message (rfc822msgid), so a citation opens the email.
"""

from __future__ import annotations

import hashlib
import json
import mailbox
import re
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import typer

app = typer.Typer(add_completion=False)

SKIP = {"spam", "trash", "chat", "chats"}
PROMOTIONS = {"category promotions", "category social", "promotions", "social"}
SAVE_TYPES = {"application/pdf": ".pdf",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}


class _Text(HTMLParser):
    """HTML -> text: drop script/style, a line break at block elements."""
    BLOCK = {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _content(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def body_text(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None and (text := _content(plain).strip()):
        return text
    html = msg.get_body(preferencelist=("html",))
    return html_to_text(_content(html)) if html is not None else ""


def labels(headers) -> list[str]:
    raw = str(headers.get("X-GM-LABELS") or "")
    return [label.strip().strip('"') for label in raw.split(",") if label.strip()]


def date_of(headers) -> datetime | None:
    try:
        when = parsedate_to_datetime(str(headers.get("Date")))
    except (TypeError, ValueError, IndexError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def addresses(msg: EmailMessage, field: str) -> list[dict]:
    values = [str(v) for v in msg.get_all(field, [])]
    return [{"name": name or None, "email": addr.lower()} for name, addr in getaddresses(values) if "@" in addr]


def message_id(headers, raw: bytes) -> str:
    mid = str(headers.get("Message-ID") or "").strip().strip("<>")
    return mid or "sha256:" + hashlib.sha256(raw).hexdigest()[:32]


def to_atlas(msg: EmailMessage, mid: str, when: datetime | None, message_labels: list[str]) -> dict:
    attachments = [{"filename": part.get_filename(), "mime_type": part.get_content_type()}
                   for part in msg.iter_attachments() if part.get_filename()]
    [sender] = addresses(msg, "From")[:1] or [None]
    return {
        "source_system": "gmail",
        "message_id": mid,
        "thread_id": str(msg.get("X-GM-THRID") or "") or None,
        "subject": str(msg.get("Subject") or "") or None,
        "from": sender,
        "to": addresses(msg, "To"),
        "cc": addresses(msg, "Cc"),
        "date": when.isoformat() if when else None,
        "body": body_text(msg),
        "attachments": attachments,
        "labels": message_labels,
        "uri": "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{mid}", safe=""),
    }


def _stem(mid: str) -> str:
    return hashlib.sha256(mid.encode()).hexdigest()[:16]


@app.command()
def main(mbox: Path, out_dir: Path,
         since: str = typer.Option("", help="Only messages on or after this date (YYYY-MM-DD)"),
         limit: int = typer.Option(0, help="Keep the most recent N messages (0 = all)"),
         keep_promotions: bool = typer.Option(False, help="Keep Promotions and Social"),
         attachments: Path = typer.Option(None, help="Save PDF and DOCX attachments here")):
    floor = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) if since else None
    skip = SKIP | (set() if keep_promotions else PROMOTIONS)
    box = mailbox.mbox(str(mbox), create=False)
    counts = {"read": 0, "skipped_label": 0, "before_since": 0, "duplicate": 0, "unparsable": 0}

    # Pass 1: headers only, to choose messages (label, date, one per Message-ID, most recent first).
    chosen: dict[str, tuple] = {}
    for key in box.iterkeys():
        counts["read"] += 1
        try:
            raw = box.get_bytes(key)
            headers = BytesHeaderParser(policy=policy.default).parsebytes(raw)
            message_labels = labels(headers)
            when, mid = date_of(headers), message_id(headers, raw)
        except Exception:                      # a malformed message must not stop the export
            counts["unparsable"] += 1
            continue
        if skip & {label.lower() for label in message_labels}:
            counts["skipped_label"] += 1
        elif floor and (when is None or when < floor):
            counts["before_since"] += 1
        elif mid in chosen:
            counts["duplicate"] += 1
        else:
            chosen[mid] = (key, when, message_labels)
    order = sorted(chosen.items(), key=lambda kv: kv[1][1] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
    if limit:
        order = order[:limit]

    # Pass 2: parse and write the chosen messages.
    written = saved = 0
    for mid, (key, when, message_labels) in order:
        try:
            msg = BytesParser(policy=policy.default).parsebytes(box.get_bytes(key))
            payload = to_atlas(msg, mid, when, message_labels)
        except Exception:
            counts["unparsable"] += 1
            continue
        folder = out_dir / (when.strftime("%Y-%m") if when else "undated")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{_stem(mid)}.json").write_text(json.dumps(payload))
        written += 1
        if attachments:
            for i, part in enumerate(msg.iter_attachments()):
                suffix = SAVE_TYPES.get(part.get_content_type())
                if suffix and (data := part.get_payload(decode=True)):
                    attachments.mkdir(parents=True, exist_ok=True)
                    name = Path(part.get_filename() or f"attachment{i}").stem[:60]
                    (attachments / f"{_stem(mid)}_{i}_{name}{suffix}").write_bytes(data)
                    saved += 1
    typer.echo(json.dumps({**counts, "written": written, "attachments_saved": saved}))


if __name__ == "__main__":
    app()
