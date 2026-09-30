"""Deterministic reading of email addresses (policy `email_identity`).

  organization domain   registrable domain: ect.enron.com -> enron.com, news.bbc.co.uk -> bbc.co.uk
  mailbox               local part at the organization domain: pallen@ect.enron.com -> pallen@enron.com
                        (one mailbox reached through several company sub-domains)
  name from address     first.last / first_last / first-last / first.m.last -> "First Last"
                        (flast, lastname, digits: not a name; the address stays the handle)
  address patterns      a company that uses first.last also hands out flast (jadams) and
                        lastname (adams) addresses; see handle_patterns / name_patterns
  provider mailbox      providers that deliver several spellings to one mailbox (Gmail: dots and
                        a +tag ignored): kassondra.cisneroz+x@gmail.com -> kassondracisneroz@gmail.com

Everything here is a pure function of the address and the policy.
"""

from __future__ import annotations

import re

_SEP = re.compile(r"[._-]+")
_ALPHA = re.compile(r"[a-z]+")


def registrable_domain(domain: str, multi_part_suffixes: list[str]) -> str:
    labels = domain.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    keep = 3 if ".".join(labels[-2:]) in multi_part_suffixes else 2
    return ".".join(labels[-keep:])


def split_address(email: str) -> tuple[str, str]:
    local, _, domain = email.lower().strip().partition("@")
    return local, domain


def provider_mailbox(email: str, providers: dict) -> str | None:
    """The one mailbox a provider delivers this spelling to, or None if the provider has no such rule."""
    local, domain = split_address(email)
    rule = providers.get(domain)
    if not rule or not local:
        return None
    if rule.get("ignore_plus_tag"):
        local = local.split("+", 1)[0]
    if rule.get("ignore_dots"):
        local = local.replace(".", "")
    return f"{local}@{rule.get('same_as') or domain}" if local else None


def name_from_local(local: str) -> tuple[str, str] | None:
    """(first, last) when the local part is unambiguously first[.m].last, else None."""
    # '.' / '_' separate names when present, so a hyphen stays inside a surname
    # (martha.sumner-kenney); otherwise '-' separates (first-last).
    parts = [p for p in re.split(r"[._]+" if re.search(r"[._]", local) else r"-+", local) if p]
    if len(parts) not in (2, 3) or not all(_ALPHA.fullmatch(p) for p in parts[:-1]) \
            or not re.fullmatch(r"[a-z]+(-[a-z]+)?", parts[-1]):
        return None
    if len(parts) == 3 and len(parts[1]) != 1:
        return None
    first, last = parts[0], parts[-1]
    if len(first) < 2 or len(last) < 2:
        return None
    return first, last


def name_from_display(display: str | None) -> tuple[str, str] | None:
    """(first, last) from a header display name: 'Phillip Allen', 'Allen, Phillip', '"Allen, Phillip K."'."""
    if not display:
        return None
    text = display.strip().strip("'\"").strip()
    if "@" in text:
        return None
    if "," in text:
        last, _, first = text.partition(",")
        tokens = first.split()[:1] + [last.strip()]
    else:
        tokens = text.split()
        tokens = [tokens[0], tokens[-1]] if len(tokens) >= 2 else tokens
    tokens = [t.strip(".").lower() for t in tokens]
    if len(tokens) != 2 or not all(_ALPHA.fullmatch(t) and len(t) >= 2 for t in tokens):
        return None
    return tokens[0], tokens[1]


def display_name(first: str, last: str) -> str:
    return f"{first.capitalize()} {'-'.join(p.capitalize() for p in last.split('-'))}"


def handle_patterns(first: str, last: str) -> list[tuple[str, str]]:
    """Other local parts the same person commonly has: (flast, 'flast'), (lastname, 'lastname')."""
    return [(first[0] + last, "flast"), (last, "lastname")]


_SEP_RX = r"[._-]+([a-z][._-]+)?"


def flast_pattern(handle: str, org_domain: str) -> str | None:
    """Regex over mailbox identifiers: first.last addresses a flast handle fits ('pallen' -> p<first>.allen)."""
    if not _ALPHA.fullmatch(handle) or len(handle) < 3:
        return None
    return rf"^{handle[0]}[a-z]+{_SEP_RX}{handle[1:]}@{re.escape(org_domain)}$"


def lastname_pattern(handle: str, org_domain: str) -> str | None:
    """Regex over mailbox identifiers: first.last addresses a lastname handle fits ('lavorato' -> <first>.lavorato)."""
    if not _ALPHA.fullmatch(handle) or len(handle) < 3:
        return None
    return rf"^[a-z]{{2,}}{_SEP_RX}{handle}@{re.escape(org_domain)}$"


def first_name_pattern(handle: str, org_domain: str) -> str:
    """Regex: addresses that use the handle as a FIRST name (john -> john.arnold). A handle that
    is someone's first name at the organization is ambiguous and never links."""
    return rf"^{re.escape(handle)}[._-]+[a-z]{{2,}}.*@{re.escape(org_domain)}$"


PATTERNS = {"flast": flast_pattern, "lastname": lastname_pattern}
