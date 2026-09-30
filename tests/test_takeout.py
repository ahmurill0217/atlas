"""Gmail Takeout (.mbox) -> Atlas email JSON (scripts/takeout_to_atlas.py); no database needed."""

import importlib.util
import json
import mailbox
from email.message import EmailMessage

from atlas.ingestion.adapters import normalize_file
from tests.conftest import ROOT

_spec = importlib.util.spec_from_file_location("takeout_to_atlas", ROOT / "scripts" / "takeout_to_atlas.py")
takeout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(takeout)


def _msg(mid, subject, date, labels="Inbox", html=None, plain="Hi Sam,\n\nSee attached.\n\nOn Mon, Sam wrote:\n> old",
         attach=None, header="X-GM-LABELS"):
    m = EmailMessage()
    m["From"] = '"Chen, Sarah" <Sarah.Chen@acme.com>'
    m["To"] = "Sam Lee <sam@example.com>, ops@acme.com"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = f"<{mid}@mail.acme.com>"
    m["X-GM-THRID"] = "1790000000000000001"
    m[header] = labels
    if html:
        m.set_content(html, subtype="html")
    else:
        m.set_content(plain)
    if attach:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename=attach)
    return m


def test_takeout_export_becomes_atlas_email_json(tmp_path):
    box = mailbox.mbox(str(tmp_path / "all.mbox"))
    for m in [
        _msg("a1", "=?utf-8?q?Pricing_=E2=80=94_Q3?=", "Tue, 03 Jun 2025 10:00:00 +0000", attach="Proposal v2.pdf"),
        _msg("a1", "duplicate copy", "Tue, 03 Jun 2025 10:00:00 +0000"),
        _msg("a2", "Only HTML", "Wed, 04 Jun 2025 10:00:00 +0000",
             html="<html><head><style>p{}</style></head><body><p>Call <b>Thursday</b>?</p><p>Sarah</p></body></html>"),
        _msg("a3", "You won", "Wed, 04 Jun 2025 11:00:00 +0000", labels="Spam"),
        _msg("a4", "50% off", "Wed, 04 Jun 2025 12:00:00 +0000", labels='"Category Promotions",Inbox'),
        _msg("a5", "Old news", "Mon, 01 Jan 2024 10:00:00 +0000"),
        _msg("a6", "Your order shipped", "Thu, 05 Jun 2025 10:00:00 +0000", labels="Category Updates,Inbox",
             header="X-Gmail-Labels"),                                     # newer exports' header name
        _msg("a7", "Re: invoice", "Thu, 05 Jun 2025 11:00:00 +0000", labels="Sent,Category Updates",
             header="X-Gmail-Labels"),
    ]:
        box.add(m)
    box.flush()
    out, files = tmp_path / "json", tmp_path / "files"

    takeout.main(tmp_path / "all.mbox", out, since="2025-01-01", limit=0, keep_automated=False, attachments=files)

    written = {json.loads(p.read_text())["message_id"]: p for p in out.rglob("*.json")}
    assert set(written) == {"a1@mail.acme.com", "a2@mail.acme.com", "a7@mail.acme.com"}   # what you sent is kept;
    # spam, promotions, updates, old and the duplicate are gone
    first = json.loads(written["a1@mail.acme.com"].read_text())
    assert first["subject"] == "Pricing — Q3" and first["thread_id"] == "1790000000000000001"
    assert first["from"] == {"name": "Chen, Sarah", "email": "sarah.chen@acme.com"}
    assert [a["email"] for a in first["to"]] == ["sam@example.com", "ops@acme.com"]
    assert first["attachments"] == [{"filename": "Proposal v2.pdf", "mime_type": "application/pdf"}]
    assert first["uri"].endswith("rfc822msgid%3Aa1%40mail.acme.com")
    assert [p.suffix for p in files.iterdir()] == [".pdf"]
    html_only = json.loads(written["a2@mail.acme.com"].read_text())
    assert html_only["body"] == "Call Thursday?\n\nSarah"

    doc = normalize_file(written["a1@mail.acme.com"])        # the existing adapter reads it as is
    assert doc.source_system == "gmail" and doc.author.email == "sarah.chen@acme.com"
    assert doc.sections[0].text.strip() == "Hi Sam,\n\nSee attached."           # quoted reply split off
    assert doc.created_at.isoformat().startswith("2025-06-03")


def test_limit_keeps_the_most_recent(tmp_path):
    box = mailbox.mbox(str(tmp_path / "all.mbox"))
    for i, day in enumerate(["01", "05", "03"]):
        box.add(_msg(f"m{i}", f"day {day}", f"Sun, {day} Jun 2025 10:00:00 +0000"))
    box.flush()
    takeout.main(tmp_path / "all.mbox", tmp_path / "json", since="", limit=2, keep_automated=False, attachments=None)
    subjects = {json.loads(p.read_text())["subject"] for p in (tmp_path / "json").rglob("*.json")}
    assert subjects == {"day 05", "day 03"}
