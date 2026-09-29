"""Deterministic string normalization for identity. Same input, same key, always."""

from __future__ import annotations

import re
import unicodedata


def normalize_name(name: str) -> str:
    """'Sarah  CHEN' -> 'sarah chen'; "Acme's" -> 'acme'; punctuation -> space."""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = re.sub(r"['’]s\b", "", text)
    text = re.sub(r"[^\w]+|_", " ", text)
    return " ".join(text.split())


def normalize_email(email: str) -> str:
    return unicodedata.normalize("NFKC", email).strip().lower()


def normalize_domain(domain: str) -> str:
    d = unicodedata.normalize("NFKC", domain).strip().lower().rstrip(".")
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d.removeprefix("www.")


def domain_of(email: str) -> str | None:
    email = normalize_email(email)
    return normalize_domain(email.rsplit("@", 1)[1]) if "@" in email else None


# Identifier types that are case-insensitive by nature. Everything else is a
# source-native id (Drive file ids, Gmail message ids, paths, meeting ids) and
# is case-SENSITIVE: lowercasing could merge two different objects.
CASE_INSENSITIVE_IDENTIFIERS = {"email", "domain", "team_key", "mailbox"}


def normalize_identifier(identifier_type: str, value: str) -> str:
    if identifier_type in ("email", "mailbox"):
        return normalize_email(value)
    if identifier_type == "domain":
        return normalize_domain(value)
    value = unicodedata.normalize("NFKC", value).strip()
    return value.lower() if identifier_type in CASE_INSENSITIVE_IDENTIFIERS else value
