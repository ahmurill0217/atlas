"""Deterministic string normalization shared by extraction, resolution and
evaluation. Identity decisions start here, so keep it boring and predictable."""

from __future__ import annotations

import re
import unicodedata


def normalize_name(name: str) -> str:
    """'KRAS-G12C' -> 'kras g12c'; "Amgen's" -> 'amgen'."""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = re.sub(r"['’]s\b", "", text)
    text = re.sub(r"[^\w]+|_", " ", text)
    return " ".join(text.split())


def compact_name(name: str) -> str:
    """Whitespace-free key: 'AMG 510' and 'AMG510' collide."""
    return normalize_name(name).replace(" ", "")


def numeric_tokens(name: str) -> frozenset[str]:
    """Tokens containing digits. Names that differ here are different things
    ('CodeBreaK 100' vs 'CodeBreaK 200', 'G12C' vs 'G12D') however similar."""
    return frozenset(t for t in normalize_name(name).split() if any(c.isdigit() for c in t))


def normalize_entity_type(entity_type: str) -> str:
    """'clinical trial' / 'Clinical_Trial' / 'clinicalTrial' -> 'ClinicalTrial'."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", entity_type.strip())
    words = re.split(r"[^A-Za-z0-9]+", spaced)
    return "".join(w[:1].upper() + w[1:].lower() for w in words if w) or "Thing"


def normalize_relationship_type(relationship_type: str) -> str:
    """'Is Developed By' / 'isDevelopedBy' -> 'is_developed_by'."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", relationship_type.strip())
    return "_".join(w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w) or "related_to"
