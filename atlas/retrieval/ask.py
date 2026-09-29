"""`atlas ask`: brain retrieves and answers; the graph profiles (and can boost).

  1. link   people / organizations named in the question to graph entities.
            Conservative: a person needs a full name, alias or email address (a
            single word like "Bob" never links a person); a capitalized word may
            link a company whose domain label it is. Uncertain -> no link.
  2. boost  (opt-in, `--boost`) the documents those entities appear on (AUTHORED_BY / SENT_TO; for an
            external organization, the documents of its people). Several people:
            the documents they share, else all of theirs. Internal organizations
            add nothing: every document involves them. The boost ranks these
            documents first; it never hides the rest. Evaluation 2026-09-29: a hard
            filter lost threads *about* a person that they were not on; the boost
            still crowded them out; the profile alone matched plain retrieval and
            added identity, so the profile without boost is the default.
  3. facts  a short metadata profile per entity: addresses, employer, volume,
            active period, frequent correspondents, recent subjects
  4. answer brain searches the whole index with the boost and answers with
            citations; the facts ride in the system prompt, labelled as metadata

With `use_graph=False` the question goes to brain unfiltered and without facts,
which is the baseline the graph has to beat.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.ingestion.normalized import EARLIEST_PLAUSIBLE_DATE
from atlas.resolution.normalize import normalize_email, normalize_name

MAX_SCOPE = 20_000
_WORD = re.compile(r"[^\s,;<>()]+@[^\s,;<>()]+|[A-Za-z][A-Za-z'&.-]*")
_STOP = {"the", "and", "for", "with", "about", "what", "who", "how", "did", "does", "our", "we", "me", "my",
         "you", "your", "prep", "call", "deal", "know", "tell", "when", "where", "which", "why", "all",
         "any", "this", "that", "from", "into", "over", "been", "have", "has", "was", "were", "are", "is"}


@dataclass
class LinkedEntity:
    id: uuid.UUID
    entity_type: str
    name: str
    matched: str
    is_internal: bool = False


@dataclass
class GraphContext:
    entities: list[LinkedEntity] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    scope: str = "unscoped"
    facts: str = ""


def _grams(question: str) -> list[tuple[str, bool]]:
    """(n-gram, starts capitalized) for n = 4..1, longest first."""
    words = _WORD.findall(question)
    out = []
    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            gram = " ".join(words[i:i + n]).strip(".,?!'\"")
            out.append((gram, gram[:1].isupper()))
    return out


def link_entities(s: Session, question: str) -> list[LinkedEntity]:
    found: dict[uuid.UUID, LinkedEntity] = {}
    covered: list[str] = []

    def add(row, matched: str):
        if row.id not in found:
            found[row.id] = LinkedEntity(row.id, row.entity_type, row.canonical_name, matched,
                                         bool((row.properties or {}).get("is_internal")))
            covered.append(normalize_name(matched))

    for gram, capitalized in _grams(question):
        key = normalize_name(gram)
        if not key or key in _STOP or any(key in c.split() or key in c for c in covered):
            continue
        if "@" in gram:
            rows = s.execute(text("""SELECT e.id, e.entity_type, e.canonical_name, e.properties FROM kg.entities e
                JOIN kg.entity_external_ids x ON x.entity_id = e.id
                WHERE x.identifier_type = 'email' AND x.value = :v"""), {"v": normalize_email(gram)}).all()
        elif " " in key:
            rows = s.execute(text("""SELECT DISTINCT e.id, e.entity_type, e.canonical_name, e.properties
                FROM kg.entities e LEFT JOIN kg.entity_aliases a ON a.entity_id = e.id
                WHERE e.status = 'active' AND e.entity_type IN ('Person', 'Organization')
                  AND (e.normalized_name = :k OR a.normalized_alias = :k)"""), {"k": key}).all()
        elif capitalized and len(key) >= 3:
            # One capitalized word never names a person ("Bob", "Jacques" are too
            # ambiguous); it may name a company whose domain label it is.
            rows = s.execute(text("""SELECT e.id, e.entity_type, e.canonical_name, e.properties FROM kg.entities e
                JOIN kg.entity_external_ids x ON x.entity_id = e.id AND x.identifier_type = 'domain'
                WHERE e.status = 'active' AND split_part(x.value, '.', 1) = :k"""), {"k": key}).all()
        else:
            continue
        if len(rows) == 1:
            add(rows[0], gram)
    return list(found.values())


# A document is in scope if one of the people is on it and it is not broadcast mail.
# Broadcast = written by an address that never receives mail in the corpus
# (newsletters, stores, system senders): a structural signal, no sender patterns.
_DOCS_OF_PERSON = """
    SELECT DISTINCT d.id::text FROM kg.edges g
    JOIN kg.entity_external_ids x ON x.entity_id = g.source_entity_id AND x.identifier_type = 'source_id'
    JOIN kg.documents d ON d.source_system || ':' || d.source_external_id = x.value
    LEFT JOIN kg.edges au ON au.source_entity_id = g.source_entity_id AND au.relation_type = 'AUTHORED_BY'
    WHERE g.target_entity_id = ANY(:ids) AND g.relation_type IN ('AUTHORED_BY', 'SENT_TO', 'HAS_PARTICIPANT')
      AND g.status = 'active'
      AND (au.target_entity_id IS NULL OR au.target_entity_id = ANY(:ids)
           OR EXISTS (SELECT 1 FROM kg.edges rx WHERE rx.target_entity_id = au.target_entity_id
                      AND rx.relation_type = 'SENT_TO'))"""

def documents_of(s: Session, person_ids: list[uuid.UUID]) -> set[str]:
    return set(s.execute(text(_DOCS_OF_PERSON), {"ids": person_ids}).scalars())


def people_of(s: Session, org_id: uuid.UUID) -> list[uuid.UUID]:
    return list(s.execute(text("""SELECT source_entity_id FROM kg.edges
        WHERE target_entity_id = :o AND relation_type = 'WORKS_AT' AND status = 'active'"""), {"o": org_id}).scalars())


# Contacts of a person: everyone they wrote to, plus everyone who wrote to them
# and is not a broadcast sender (same definition as the scope filter above).
_CONTACTS = """
    WITH authored AS (SELECT source_entity_id AS doc FROM kg.edges
                      WHERE target_entity_id = :i AND relation_type = 'AUTHORED_BY'),
         wrote_to AS (SELECT DISTINCT r.target_entity_id AS p FROM kg.edges r JOIN authored a ON a.doc = r.source_entity_id
                      WHERE r.relation_type = 'SENT_TO'),
         wrote_me AS (SELECT DISTINCT au.target_entity_id AS p FROM kg.edges rc
                      JOIN kg.edges au ON au.source_entity_id = rc.source_entity_id AND au.relation_type = 'AUTHORED_BY'
                      WHERE rc.target_entity_id = :i AND rc.relation_type = 'SENT_TO'
                        AND EXISTS (SELECT 1 FROM kg.edges rx WHERE rx.target_entity_id = au.target_entity_id
                                    AND rx.relation_type = 'SENT_TO')),
         contacts AS (SELECT p FROM wrote_to UNION SELECT p FROM wrote_me EXCEPT SELECT CAST(:i AS uuid))"""


def person_facts(s: Session, e: LinkedEntity) -> str:
    """Metadata profile. Correspondents and recent conversations leave out broadcast
    senders (newsletters, stores, systems), and dates before EARLIEST_PLAUSIBLE_DATE
    (placeholders) are ignored."""
    emails = s.execute(text("""SELECT value FROM kg.entity_external_ids WHERE entity_id = :i
        AND identifier_type = 'email' ORDER BY value"""), {"i": e.id}).scalars().all()
    orgs = s.execute(text("""SELECT o.canonical_name FROM kg.edges g JOIN kg.entities o ON o.id = g.target_entity_id
        WHERE g.source_entity_id = :i AND g.relation_type = 'WORKS_AT'"""), {"i": e.id}).scalars().all()
    vol = s.execute(text("""SELECT count(*) FILTER (WHERE relation_type = 'AUTHORED_BY') AS sent,
        count(*) FILTER (WHERE relation_type = 'SENT_TO') AS received
        FROM kg.edges WHERE target_entity_id = :i"""), {"i": e.id}).one()
    active = s.execute(text(_CONTACTS + """
        SELECT min(d.properties->>'created_at') AS first, max(d.properties->>'created_at') AS last FROM kg.edges g
        JOIN kg.entities d ON d.id = g.source_entity_id
        LEFT JOIN kg.edges au ON au.source_entity_id = g.source_entity_id AND au.relation_type = 'AUTHORED_BY'
        WHERE g.target_entity_id = :i AND g.relation_type IN ('AUTHORED_BY', 'SENT_TO')
          AND (au.target_entity_id = :i OR au.target_entity_id IN (SELECT p FROM contacts))
          AND d.properties->>'created_at' >= :floor_s"""),
                       {"i": e.id, "floor_s": EARLIEST_PLAUSIBLE_DATE.isoformat()}).one()
    peers = s.execute(text(_CONTACTS + """,
         mine AS (SELECT source_entity_id AS doc FROM kg.edges WHERE target_entity_id = :i
                  AND relation_type IN ('AUTHORED_BY', 'SENT_TO'))
        SELECT p.canonical_name, count(DISTINCT g.source_entity_id) AS n FROM kg.edges g
        JOIN mine ON mine.doc = g.source_entity_id JOIN contacts t ON t.p = g.target_entity_id
        JOIN kg.entities p ON p.id = g.target_entity_id
        WHERE g.relation_type IN ('AUTHORED_BY', 'SENT_TO')
        GROUP BY p.canonical_name ORDER BY n DESC LIMIT 8"""), {"i": e.id}).all()
    contacts = s.execute(text(_CONTACTS + " SELECT count(*) FROM contacts"), {"i": e.id}).scalar()
    subjects = s.execute(text(_CONTACTS + """
        SELECT DISTINCT d.canonical_name, d.properties->>'created_at' AS at FROM kg.edges g
        JOIN kg.entities d ON d.id = g.source_entity_id
        LEFT JOIN kg.edges au ON au.source_entity_id = g.source_entity_id AND au.relation_type = 'AUTHORED_BY'
        WHERE g.target_entity_id = :i AND g.relation_type IN ('AUTHORED_BY', 'SENT_TO')
          AND (au.target_entity_id = :i OR au.target_entity_id IN (SELECT p FROM contacts))
          AND d.properties->>'created_at' >= :floor_s
        ORDER BY at DESC LIMIT 6"""), {"i": e.id, "floor_s": EARLIEST_PLAUSIBLE_DATE.isoformat()}).all()
    lines = [f"- {e.name} (Person). Email addresses: {', '.join(emails) or 'none'}. "
             f"Employer by email domain: {', '.join(orgs) or 'unknown'}.",
             f"  Emails sent: {vol.sent}, received: {vol.received}; active {(active.first or '?')[:10]} to "
             f"{(active.last or '?')[:10]}; "
             f"contacts: {contacts}.",
             "  Most frequent correspondents: "
             + (", ".join(f"{p.canonical_name} ({p.n})" for p in peers) or "none"),
             "  Most recent conversations: "
             + ("; ".join(f"\"{r.canonical_name}\" ({(r.at or '')[:10]})" for r in subjects) or "none")]
    return "\n".join(lines)


def org_facts(s: Session, e: LinkedEntity) -> str:
    people = s.execute(text("""
        SELECT p.canonical_name, count(v.id) AS n FROM kg.edges w JOIN kg.entities p ON p.id = w.source_entity_id
        LEFT JOIN kg.edges v ON v.target_entity_id = p.id AND v.relation_type IN ('AUTHORED_BY', 'SENT_TO')
        WHERE w.target_entity_id = :o AND w.relation_type = 'WORKS_AT'
        GROUP BY p.canonical_name ORDER BY n DESC LIMIT 10"""), {"o": e.id}).all()
    total = s.execute(text("""SELECT count(*) FROM kg.edges WHERE target_entity_id = :o
        AND relation_type = 'WORKS_AT'"""), {"o": e.id}).scalar()
    kind = "our own organization" if e.is_internal else "an external organization"
    return (f"- {e.name} (Organization, {kind}). {total} known people by email domain. Most active: "
            + ", ".join(f"{p.canonical_name} ({p.n} emails)" for p in people))


def graph_context(s: Session, question: str) -> GraphContext:
    ctx = GraphContext(entities=link_entities(s, question))
    people = [e for e in ctx.entities if e.entity_type == "Person"]
    external_orgs = [e for e in ctx.entities if e.entity_type == "Organization" and not e.is_internal]
    sets = [documents_of(s, [p.id]) for p in people]
    sets += [documents_of(s, people_of(s, o.id)) for o in external_orgs]
    sets = [x for x in sets if x]
    if sets:
        shared = set.intersection(*sets)
        docs, ctx.scope = (shared, "shared") if len(sets) > 1 and shared else (set.union(*sets), "union")
        if len(sets) == 1:
            ctx.scope = "entity"
        ctx.document_ids = sorted(docs)[:MAX_SCOPE]
    facts = [person_facts(s, e) if e.entity_type == "Person" else org_facts(s, e) for e in ctx.entities]
    ctx.facts = "\n".join(facts)
    return ctx


SYSTEM_SUFFIX = """

# Knowledge graph context
The documents are emails. Every search result begins with its From / To / Cc / Date / Subject lines.
The facts below come from email metadata (who wrote to whom, and when). They are reliable for who
and when, but say nothing about content; use the search results for what was said, and cite them.
{scope}
{facts}
"""


def system_prompt(ctx: GraphContext) -> str:
    from brain.answer.prompts.chat_prompts import DEFAULT_SYSTEM_PROMPT

    scope = (f"Search covers every email; the {len(ctx.document_ids)} emails involving the entities below "
             "rank first. Emails about a person that they are not on can matter too."
             if ctx.document_ids else "Search covers every email.")
    return DEFAULT_SYSTEM_PROMPT + SYSTEM_SUFFIX.format(scope=scope, facts=ctx.facts or "(no entities recognized)")
