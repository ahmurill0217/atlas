"""persistent-knowledge-layer corpus -> Atlas files (scripts/pkl_to_atlas.py); no database needed."""

import importlib.util
import json

from atlas.ingestion.adapters import normalize_file
from tests.conftest import ROOT

_spec = importlib.util.spec_from_file_location("pkl_to_atlas", ROOT / "scripts" / "pkl_to_atlas.py")
pkl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkl)

RULE = "-" * 70
THREAD = f"""DOCUMENT ID: INS-SYN-018
TITLE: Email thread - Rationale
DATE: 2026-01-22

{RULE}
From:    E. Vasquez (Portfolio Analytics)
To:      D. Lindqvist (Head of Property Underwriting)
Cc:      M. Oyelaran (Catastrophe Risk)
Subject: Re: Roof threshold
Date:    22 January 2026, 16:41
{RULE}

Hence 15 years, and hence H3 only.

{RULE}
From:    D. Lindqvist (Head of Property Underwriting)
To:      E. Vasquez (Portfolio Analytics)
Subject: Re: Roof threshold
Date:    22 January 2026, 17:58
{RULE}

Understood, and agreed on both.
"""

POLICY = """---
document_id: INS-SYN-009
title: Policy Overview (v1.1 - SUPERSEDED)
version: 1.1
effective_date: 2025-01-01
superseded_by: INS-SYN-001
---

# Policy Overview

Depreciation is **not recoverable** after repair.
"""


def test_a_thread_becomes_one_email_per_message_with_names_only(tmp_path):
    first, reply = pkl.emails(THREAD, "INS-SYN-018")
    assert first["from"] == {"name": "E. Vasquez"} and first["cc"] == [{"name": "M. Oyelaran"}]
    assert (first["date"], reply["date"]) == ("2026-01-22T16:41:00Z", "2026-01-22T17:58:00Z")
    assert first["body"] == "Hence 15 years, and hence H3 only." and reply["message_id"] == "INS-SYN-018-2"
    (tmp_path / "m.json").write_text(json.dumps(first))
    doc = normalize_file(tmp_path / "m.json")
    assert doc.source_type == "email" and [(p.role, p.name, p.email) for p in doc.participants] == [
        ("sender", "E. Vasquez", None), ("to", "D. Lindqvist", None), ("cc", "M. Oyelaran", None)]


def test_a_document_keeps_its_text_and_is_dated_by_its_effective_date(tmp_path):
    fields = pkl.header_fields(POLICY)
    (tmp_path / "d.json").write_text(json.dumps(pkl.document(POLICY, fields, "INS-SYN-009", "09.md")))
    doc = normalize_file(tmp_path / "d.json")
    assert doc.title == "Policy Overview (v1.1 - SUPERSEDED)" and doc.created_at.date().isoformat() == "2025-01-01"
    assert "not recoverable" in doc.raw_text and "superseded_by: INS-SYN-001" in doc.raw_text
    assert doc.metadata["superseded_by"] == "INS-SYN-001"
