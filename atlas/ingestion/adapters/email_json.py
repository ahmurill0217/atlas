"""Email-like JSON -> NormalizedDocument.

Accepted shape (Gmail exports map onto it directly):
    {"message_id", "thread_id", "subject", "from", "to": [...], "cc": [...],
     "date", "body", "attachments": [{"filename", "mime_type"}], "source_system"?, "permissions"?}
Addresses may be "Name <addr>" strings or {"name", "email"} objects.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from atlas.ingestion.adapters.base import UnsupportedSource, build_text
from atlas.ingestion.normalized import NormalizedDocument, Participant, document_id_for

# Where the quoted reply chain or forwarded content starts: "On <date>, <person> wrote:",
# the first "> " line, "-----Original Message-----", or a "--- Forwarded by ... ---"
# banner (Outlook / Lotus Notes), allowing leading whitespace.
_QUOTE_START = re.compile(r"^[ \t]*(On .{5,200}wrote:\s*$|>.*$|-{2,} ?Original Message ?-{2,}|-{2,} ?Forwarded by .*$)",
                          re.MULTILINE)


def _addresses(value, role: str, field: str) -> list[Participant]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for i, item in enumerate(items):
        path = f"{field}[{i}]" if isinstance(value, list) else field
        if isinstance(item, dict):
            name, addr = item.get("name"), item.get("email")
        else:
            [(name, addr)] = getaddresses([str(item)]) or [("", "")]
        addr = (addr or "").strip().lower() or None
        out.append(Participant(name=(name or "").strip() or None, email=addr, role=role, source_field=path))
    return out


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return parsedate_to_datetime(str(value))


def split_quoted(body: str) -> tuple[str, str]:
    """(new text, quoted reply chain). Only the new text is this message's own content."""
    m = _QUOTE_START.search(body)
    return (body, "") if m is None else (body[: m.start()].rstrip(), body[m.start():].strip())


class EmailJsonAdapter:
    name = "email_json"

    def can_handle(self, path: Path, payload: dict | None) -> bool:
        return bool(payload) and "message_id" in payload and "from" in payload

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument:
        if not payload:
            raise UnsupportedSource(f"{path}: empty email payload")
        system = payload.get("source_system", "email")
        external_id = str(payload["message_id"])
        [sender] = _addresses(payload["from"], "sender", "from") or [None]
        participants = ([sender] if sender else []) + _addresses(payload.get("to"), "to", "to") \
            + _addresses(payload.get("cc"), "cc", "cc")
        new_text, quoted = split_quoted(payload.get("body") or "")
        raw_text, sections = build_text([("body", new_text, {}), ("quoted", quoted, {"quoted": True})])
        return NormalizedDocument(
            document_id=document_id_for(system, external_id),
            source_system=system,
            source_type="email",
            source_external_id=external_id,
            title=payload.get("subject"),
            uri=payload.get("uri"),
            raw_text=raw_text,
            author=sender,
            participants=participants,
            created_at=_parse_date(payload.get("date")),
            metadata={
                "subject": payload.get("subject"),
                "thread_id": payload.get("thread_id"),
                "message_id": external_id,
                "attachments": payload.get("attachments") or [],
            },
            sections=sections,
            permissions=payload.get("permissions") or {},
        )
