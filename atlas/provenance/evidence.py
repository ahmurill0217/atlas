from __future__ import annotations

import hashlib

from atlas.extraction.candidates import EvidenceRef

# Stronger provenance wins when one edge has several kinds of support.
PROVENANCE_STRENGTH = {"HUMAN_CONFIRMED": 4, "STRUCTURED_SOURCE": 3, "EXPLICIT_TEXT": 2, "INFERRED_TEXT": 1}


def evidence_key(ev: EvidenceRef) -> str:
    """Identity of one piece of support: re-ingesting the same version adds nothing."""
    raw = f"{ev.document_version_id}|{ev.source_field or ''}|{ev.section_ordinal if ev.section_ordinal is not None else ''}|{ev.evidence_text or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def stronger(a: str, b: str) -> str:
    return a if PROVENANCE_STRENGTH.get(a, 0) >= PROVENANCE_STRENGTH.get(b, 0) else b
