"""The graph side of `atlas ask`: link the people and companies a question names,
and describe them from email metadata. Routing and answering: atlas/retrieval/router.py.

  1. link   people / organizations named in the question to graph entities, two ways:
            - from the question text, exactly: a full name, alias or email address
              for a person (a single word like "Bob" never links this way);
              capitalized words for a company whose domain label they spell
              ("Prebon" -> prebon.com, "Bank of America" -> bankofamerica.com);
            - from the mentions the router's model read in the question ("Sarah",
              "Tom from Alloy"): candidates by first name, name or domain prefix,
              ranked by how much the asker has emailed each. A clear leader is
              linked and noted as an assumption; otherwise the candidates are
              returned for a clarifying question ("Do you mean Sarah Chen at
              acme.com?"), and nothing is answered from a guess.
            `asker_email` (the person asking) makes "I / we / you" resolve.
  2. facts  a metadata profile per entity (addresses, employer, volume, active
            period, frequent correspondents inside and outside the organization
            with last contact, recent conversations); for two people, how often
            each wrote to the other and when, kept apart from "both on one email";
            for a company and a person, who there the person has dealt with.
            Every fact names an email that shows it ([G#], see GraphContext.cite).

Broadcast senders (addresses that never receive mail: newsletters, stores, alerts)
are left out of correspondents and recent conversations; placeholder dates are ignored.
Evaluation: docs/ask_evaluation.md.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.ingestion.normalized import EARLIEST_PLAUSIBLE_DATE
from atlas.resolution.normalize import normalize_email, normalize_name

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
    facts: str = ""
    # Emails that show the facts, cited as [G1], [G2], ...: marker -> document id, title, date.
    sources: dict[str, dict] = field(default_factory=dict)
    # For the reader: assumptions made in linking a mention ("Took "Sarah" to mean ...").
    notes: list[str] = field(default_factory=list)
    # Mentions nobody in the graph matches; worth saying only when the answer needed the graph.
    unknown: list[str] = field(default_factory=list)
    # Mentions with several close candidates: {"mention", "type", "candidates": [...]}. Non-empty
    # means ask the user which one they mean; no facts are built.
    ambiguous: list[dict] = field(default_factory=list)

    def cite(self, s: Session, document_entity_id) -> str:
        """Marker for the email behind a fact (a graph Document entity), e.g. "[G3]"; "" if unknown."""
        if document_entity_id is None:
            return ""
        for marker, src in self.sources.items():
            if src["entity_id"] == str(document_entity_id):
                return f"[{marker}]"
        row = s.execute(text("""SELECT d.id::text AS document_id, e.canonical_name AS title,
                e.properties->>'created_at' AS date FROM kg.entities e
            JOIN kg.entity_external_ids x ON x.entity_id = e.id AND x.identifier_type = 'source_id'
            JOIN kg.documents d ON d.source_system || ':' || d.source_external_id = x.value
            WHERE e.id = :i"""), {"i": document_entity_id}).first()
        if row is None:
            return ""
        marker = f"G{len(self.sources) + 1}"
        self.sources[marker] = {"entity_id": str(document_entity_id), "document_id": row.document_id,
                                "title": row.title, "date": row.date}
        return f"[{marker}]"


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
            if not rows and capitalized:
                # A multi-word company name whose words make its domain label: Bank of America -> bankofamerica.com
                rows = s.execute(text("""SELECT e.id, e.entity_type, e.canonical_name, e.properties FROM kg.entities e
                    JOIN kg.entity_external_ids x ON x.entity_id = e.id AND x.identifier_type = 'domain'
                    WHERE e.status = 'active' AND split_part(x.value, '.', 1) = :k"""), {"k": key.replace(" ", "")}).all()
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


class PersonMention(BaseModel):
    name: str                      # as written: "Sarah", "Sarah Chen", "sarah.chen@acme.com"
    organization: str | None       # as written, when the question ties them to one: "Tom from Alloy"


class OrgMention(BaseModel):
    name: str                      # as written: "Acme", "BofA"
    full_name: str | None          # a well-known abbreviation spelled out: "BofA" -> "Bank of America"


_COMM = "'AUTHORED_BY', 'SENT_TO', 'HAS_PARTICIPANT', 'ATTENDED'"
_ENTITY = "SELECT DISTINCT e.id, e.entity_type, e.canonical_name, e.properties FROM kg.entities e"
ASKER = "the person asking (I / we / you)"
# A candidate is taken without asking when it has at least this many times the emails of the next one.
LEAD = 3


def _linked(row, matched: str) -> LinkedEntity:
    return LinkedEntity(row.id, row.entity_type, row.canonical_name, matched,
                        bool((row.properties or {}).get("is_internal")))


def _person_candidates(s: Session, key: str) -> tuple[list, bool]:
    """(people, partial). A full name: an exact name or alias match, else people with that last
    name and a first name one is a prefix of ("Steve Lafontaine" -> Steven Lafontaine). One word:
    everyone with it as their name or first name, by name, alias or address ("sarah" ->
    sarah.chen@); an exact match alone is not enough, as senders named just "mike" exist."""
    words = key.split()
    if len(words) == 1:
        return s.execute(text(_ENTITY + """ LEFT JOIN kg.entity_aliases a ON a.entity_id = e.id
            WHERE e.status = 'active' AND e.entity_type = 'Person'
              AND (e.normalized_name = :k OR a.normalized_alias = :k
                   OR e.normalized_name LIKE :k || ' %' OR a.normalized_alias LIKE :k || ' %'
                   OR EXISTS (SELECT 1 FROM kg.entity_external_ids x WHERE x.entity_id = e.id
                              AND x.identifier_type = 'email'
                              AND split_part(split_part(x.value, '@', 1), '.', 1) = :k))"""),
                         {"k": key}).all(), True
    rows = s.execute(text(_ENTITY + """ LEFT JOIN kg.entity_aliases a ON a.entity_id = e.id
        WHERE e.status = 'active' AND e.entity_type = 'Person'
          AND (e.normalized_name = :k OR a.normalized_alias = :k)"""), {"k": key}).all()
    if rows:
        return rows, False
    return s.execute(text(_ENTITY + """ WHERE e.status = 'active' AND e.entity_type = 'Person'
          AND e.normalized_name LIKE '% ' || :last
          AND (split_part(e.normalized_name, ' ', 1) LIKE :first || '%'
               OR :first LIKE split_part(e.normalized_name, ' ', 1) || '%')"""),
                     {"first": words[0], "last": words[-1]}).all(), True


def _org_candidates(s: Session, key: str) -> tuple[list, bool]:
    """(organizations, partial): an exact name, alias or domain label ("bank of america" ->
    bankofamerica.com), else domains starting with the first word ("alloy therapeutics" -> alloytx.com)."""
    rows = s.execute(text(_ENTITY + """ LEFT JOIN kg.entity_aliases a ON a.entity_id = e.id
        WHERE e.status = 'active' AND e.entity_type = 'Organization'
          AND (e.normalized_name = :k OR a.normalized_alias = :k
               OR EXISTS (SELECT 1 FROM kg.entity_external_ids x WHERE x.entity_id = e.id
                          AND x.identifier_type = 'domain' AND split_part(x.value, '.', 1) = :label))"""),
                     {"k": key, "label": key.replace(" ", "")}).all()
    if rows:
        return rows, False
    first = key.split()[0]
    if len(first) < 4:
        return [], True
    return s.execute(text(_ENTITY + """ JOIN kg.entity_external_ids x ON x.entity_id = e.id
        AND x.identifier_type = 'domain'
        WHERE e.status = 'active' AND e.entity_type = 'Organization'
          AND split_part(x.value, '.', 1) LIKE :first || '%'"""), {"first": first}).all(), True


def _affinity(s: Session, ids: list, asker_id, organizations: bool = False) -> dict:
    """Emails per candidate: shared with the asker when known, else in total. For organizations,
    emails with anyone who works there."""
    target = "w.target_entity_id" if organizations else "eb.target_entity_id"
    works = ("JOIN kg.edges w ON w.source_entity_id = eb.target_entity_id AND w.relation_type = 'WORKS_AT'"
             if organizations else "")
    if asker_id:
        sql = f"""SELECT {target} AS id, count(DISTINCT eb.source_entity_id) AS n FROM kg.edges ea
            JOIN kg.edges eb ON eb.source_entity_id = ea.source_entity_id AND eb.relation_type IN ({_COMM})
            {works}
            WHERE ea.target_entity_id = :asker AND ea.relation_type IN ({_COMM})
              AND eb.target_entity_id <> :asker AND {target} = ANY(:ids) GROUP BY 1"""
    else:
        sql = f"""SELECT {target} AS id, count(DISTINCT eb.source_entity_id) AS n FROM kg.edges eb {works}
            WHERE eb.relation_type IN ({_COMM}) AND {target} = ANY(:ids) GROUP BY 1"""
    return {r.id: r.n for r in s.execute(text(sql), {"ids": ids, "asker": asker_id})}


def _candidate(s: Session, row, n: int) -> dict:
    """What a person needs to tell candidates apart."""
    if row.entity_type == "Organization":
        return {"name": row.canonical_name, "emails": n}
    email = s.execute(text("""SELECT min(value) FROM kg.entity_external_ids WHERE entity_id = :i
        AND identifier_type = 'email'"""), {"i": row.id}).scalar()
    org = s.execute(text("""SELECT min(o.canonical_name) FROM kg.edges w JOIN kg.entities o ON o.id = w.target_entity_id
        WHERE w.source_entity_id = :i AND w.relation_type = 'WORKS_AT'"""), {"i": row.id}).scalar()
    return {"name": row.canonical_name, "email": email, "organization": org, "emails": n}


def _choose(s: Session, ctx: GraphContext, mention: str, rows: list, partial: bool, asker,
            organizations: bool = False) -> LinkedEntity | None:
    """Link the one candidate, or a clear leader by emails (noted as an assumption); otherwise
    record the mention as ambiguous (several) or unknown (none)."""
    kind = "Organization" if organizations else "Person"
    rows = list({r.id: r for r in rows if not asker or r.id != asker.id}.values())
    if not rows:
        ctx.unknown.append(f'No {"company" if organizations else "one"} called "{mention}" is in the relationship graph.')
        return None
    scores = _affinity(s, [r.id for r in rows], asker.id if asker else None, organizations)
    rows.sort(key=lambda r: -scores.get(r.id, 0))
    first = scores.get(rows[0].id, 0)
    second = scores.get(rows[1].id, 0) if len(rows) > 1 else 0
    if len(rows) == 1 or (first > 0 and first >= LEAD * second):
        chosen = _linked(rows[0], mention)
        if partial or len(rows) > 1:
            whose = "you email most" if asker else "with the most email"
            desc = _candidate(s, rows[0], first)
            label = f'{desc["name"]} <{desc["email"]}>' if desc.get("email") else desc["name"]
            ctx.notes.append(f'Took "{mention}" to mean {label}'
                             + (f", the match {whose}" if len(rows) > 1 else "")
                             + "; ask again with a full name or email address if not.")
        return chosen
    ctx.ambiguous.append({"mention": mention, "type": kind, "relative_to_asker": bool(asker),
                          "candidates": [_candidate(s, r, scores.get(r.id, 0)) for r in rows[:5]]})
    return None


def resolve_mentions(s: Session, ctx: GraphContext, people: list[PersonMention], organizations: list[OrgMention],
                     asker: LinkedEntity | None) -> None:
    """Link the mentions the question-reading model found that the exact linker did not."""
    def linked(key: str) -> LinkedEntity | None:
        for e in ctx.entities:
            for n in (normalize_name(e.matched), normalize_name(e.name)):
                if key == n or f" {key} " in f" {n} ":
                    return e
        return None

    org_ids: dict[str, object] = {}
    named = {normalize_name(o.name) for o in organizations}
    orgs = list(organizations) + [OrgMention(name=p.organization, full_name=None) for p in people
                                  if p.organization and normalize_name(p.organization) not in named]
    for o in orgs:
        key = normalize_name(o.name)
        if not key or "@" in o.name or key in org_ids:
            continue
        if (known := linked(key)) is not None:
            org_ids[key] = known.id
            continue
        # The spelled-out name first ("Bank of America" -> bankofamerica.com), then as written.
        rows, partial = [], True
        for k in dict.fromkeys(filter(None, [normalize_name(o.full_name or ""), key])):
            found, loose = _org_candidates(s, k)
            if found and not loose:
                rows, partial = found, False
                break
            rows = rows or found
        if (chosen := _choose(s, ctx, o.name, rows, partial, asker, organizations=True)) is not None:
            ctx.entities.append(chosen)
            org_ids[key] = chosen.id
    for p in people:
        key = normalize_name(p.name)
        if not key or "@" in p.name or linked(key) is not None:
            continue
        rows, partial = _person_candidates(s, key)
        org_id = org_ids.get(normalize_name(p.organization or ""))
        if org_id is not None:
            at_org = set(people_of(s, org_id))
            rows = [r for r in rows if r.id in at_org] or rows
        if (chosen := _choose(s, ctx, p.name, rows, partial, asker)) is not None:
            ctx.entities.append(chosen)


def people_of(s: Session, org_id: uuid.UUID) -> list[uuid.UUID]:
    return list(s.execute(text("""SELECT source_entity_id FROM kg.edges
        WHERE target_entity_id = :o AND relation_type = 'WORKS_AT' AND status = 'active'"""), {"o": org_id}).scalars())


# Contacts of a person: everyone they wrote to, plus everyone who wrote to them
# and is not a broadcast sender (an address that never receives mail in the corpus).
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


def person_facts(s: Session, e: LinkedEntity, ctx: GraphContext) -> str:
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
    peers_sql = text(_CONTACTS + """,
         mine AS (SELECT source_entity_id AS doc FROM kg.edges WHERE target_entity_id = :i
                  AND relation_type IN ('AUTHORED_BY', 'SENT_TO'))
        SELECT p.canonical_name, count(DISTINCT g.source_entity_id) AS n,
               max(d.properties->>'created_at') AS last,
               (array_agg(d.id ORDER BY d.properties->>'created_at' DESC NULLS LAST))[1] AS last_doc FROM kg.edges g
        JOIN mine ON mine.doc = g.source_entity_id JOIN contacts t ON t.p = g.target_entity_id
        JOIN kg.entities p ON p.id = g.target_entity_id JOIN kg.entities d ON d.id = g.source_entity_id
        WHERE g.relation_type IN ('AUTHORED_BY', 'SENT_TO')
          AND (NOT :external OR NOT EXISTS (
              SELECT 1 FROM kg.edges w JOIN kg.entities o ON o.id = w.target_entity_id
              WHERE w.source_entity_id = p.id AND w.relation_type = 'WORKS_AT'
                AND (o.properties->>'is_internal')::boolean))
        GROUP BY p.canonical_name ORDER BY n DESC LIMIT 8""")
    peers = s.execute(peers_sql, {"i": e.id, "external": False}).all()
    external = s.execute(peers_sql, {"i": e.id, "external": True}).all()
    contacts = s.execute(text(_CONTACTS + " SELECT count(*) FROM contacts"), {"i": e.id}).scalar()
    subjects = s.execute(text(_CONTACTS + """
        SELECT DISTINCT d.id, d.canonical_name, d.properties->>'created_at' AS at FROM kg.edges g
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
             "  Most frequent correspondents (emails together, last contact): "
             + (", ".join(f"{p.canonical_name} ({p.n}, last {(p.last or '?')[:10]} {ctx.cite(s, p.last_doc)})"
                         for p in peers) or "none"),
             "  Most frequent correspondents outside our organization: "
             + (", ".join(f"{p.canonical_name} ({p.n}, last {(p.last or '?')[:10]} {ctx.cite(s, p.last_doc)})"
                         for p in external) or "none"),
             "  Most recent conversations: "
             + ("; ".join(f"\"{r.canonical_name}\" ({(r.at or '')[:10]}) {ctx.cite(s, r.id)}" for r in subjects)
                or "none")]
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


_BETWEEN = text("""
    SELECT count(DISTINCT d.id) AS total,
           count(DISTINCT d.id) FILTER (WHERE fa.target_entity_id = ANY(:a) AND tb.target_entity_id = ANY(:b)) AS a_to_b,
           count(DISTINCT d.id) FILTER (WHERE fa.target_entity_id = ANY(:b) AND tb.target_entity_id = ANY(:a)) AS b_to_a,
           min(d.properties->>'created_at') FILTER (WHERE d.properties->>'created_at' >= :floor_s) AS first,
           max(d.properties->>'created_at') FILTER (WHERE d.properties->>'created_at' >= :floor_s) AS last,
           (array_agg(d.id ORDER BY d.properties->>'created_at')
               FILTER (WHERE d.properties->>'created_at' >= :floor_s))[1] AS first_doc,
           (array_agg(d.id ORDER BY d.properties->>'created_at' DESC)
               FILTER (WHERE d.properties->>'created_at' >= :floor_s))[1] AS last_doc,
           max(d.properties->>'created_at') FILTER (WHERE fa.target_entity_id = ANY(:a)
               AND tb.target_entity_id = ANY(:b) AND d.properties->>'created_at' >= :floor_s) AS last_a_to_b,
           (array_agg(d.id ORDER BY d.properties->>'created_at' DESC) FILTER (WHERE fa.target_entity_id = ANY(:a)
               AND tb.target_entity_id = ANY(:b) AND d.properties->>'created_at' >= :floor_s))[1] AS last_a_to_b_doc,
           max(d.properties->>'created_at') FILTER (WHERE fa.target_entity_id = ANY(:b)
               AND tb.target_entity_id = ANY(:a) AND d.properties->>'created_at' >= :floor_s) AS last_b_to_a,
           (array_agg(d.id ORDER BY d.properties->>'created_at' DESC) FILTER (WHERE fa.target_entity_id = ANY(:b)
               AND tb.target_entity_id = ANY(:a) AND d.properties->>'created_at' >= :floor_s))[1] AS last_b_to_a_doc
    FROM kg.entities d
    JOIN kg.edges ea ON ea.source_entity_id = d.id AND ea.target_entity_id = ANY(:a)
         AND ea.relation_type IN ('AUTHORED_BY', 'SENT_TO', 'HAS_PARTICIPANT', 'ATTENDED')
    JOIN kg.edges eb ON eb.source_entity_id = d.id AND eb.target_entity_id = ANY(:b)
         AND eb.relation_type IN ('AUTHORED_BY', 'SENT_TO', 'HAS_PARTICIPANT', 'ATTENDED')
    LEFT JOIN kg.edges fa ON fa.source_entity_id = d.id AND fa.relation_type = 'AUTHORED_BY'
    LEFT JOIN kg.edges tb ON tb.source_entity_id = d.id AND tb.relation_type = 'SENT_TO'
    WHERE d.entity_type IN ('Document', 'Meeting')""")


def pair_facts(s: Session, a: LinkedEntity, b: LinkedEntity, ctx: GraphContext) -> str:
    """How two people relate in the metadata: volume each way, first and last contact."""
    r = s.execute(_BETWEEN, {"a": [a.id], "b": [b.id], "floor_s": EARLIEST_PLAUSIBLE_DATE.isoformat()}).one()
    if not r.total:
        return f"- {a.name} and {b.name}: no emails or meetings together in the corpus."
    def last(when, doc):
        return f"{when[:10]} {ctx.cite(s, doc)}" if when else "never"

    return (f"- {a.name} and {b.name}: {r.total} emails/meetings with both on them (either may be a sender, a "
            f"recipient, or one of several people copied). {a.name} wrote to {b.name} {r.a_to_b} times, last "
            f"{last(r.last_a_to_b, r.last_a_to_b_doc)}; {b.name} wrote to {a.name} {r.b_to_a} times, last "
            f"{last(r.last_b_to_a, r.last_b_to_a_doc)}. First email with both on it {(r.first or '?')[:10]} "
            f"{ctx.cite(s, r.first_doc)}, last {(r.last or '?')[:10]} {ctx.cite(s, r.last_doc)}.")


def org_person_facts(s: Session, org: LinkedEntity, person: LinkedEntity, ctx: GraphContext) -> str:
    """Who at an organization a person has dealt with, with volume and dates."""
    rows = []
    for pid in people_of(s, org.id):
        r = s.execute(_BETWEEN, {"a": [person.id], "b": [pid], "floor_s": EARLIEST_PLAUSIBLE_DATE.isoformat()}).one()
        if r.total:
            name = s.execute(text("SELECT canonical_name FROM kg.entities WHERE id = :i"), {"i": pid}).scalar()
            email = s.execute(text("""SELECT min(value) FROM kg.entity_external_ids WHERE entity_id = :i
                AND identifier_type = 'email'"""), {"i": pid}).scalar()
            rows.append((r.total, name, email, r.first, r.last, r.last_doc, r.a_to_b, r.b_to_a))
    if not rows:
        return f"- People at {org.name} who have dealt with {person.name}: none in the corpus."
    rows.sort(key=lambda x: -x[0])
    return (f"- People at {org.name} who have dealt with {person.name} ({len(rows)} addresses; one person may use "
            "several): " + "; ".join(f"{name} <{email}> ({n} emails, {(first or '?')[:10]} to {(last or '?')[:10]}"
                                     f" {ctx.cite(s, last_doc)}; {person.name} wrote to them {sent}, they wrote to "
                                     f"{person.name} {received})"
                                     for n, name, email, first, last, last_doc, sent, received in rows[:15]))


def link_asker(s: Session, email: str) -> LinkedEntity | None:
    row = s.execute(text("""SELECT e.id, e.entity_type, e.canonical_name, e.properties FROM kg.entities e
        JOIN kg.entity_external_ids x ON x.entity_id = e.id
        WHERE x.identifier_type = 'email' AND x.value = :v AND e.status = 'active'"""),
                    {"v": normalize_email(email)}).first()
    return _linked(row, ASKER) if row else None


def graph_context(s: Session, question: str, asker_email: str | None = None,
                  people: list[PersonMention] = (), organizations: list[OrgMention] = ()) -> GraphContext:
    """`asker_email`: who is asking, so "I", "we" and "you" mean someone. The platform knows this
    for every question; relationships between the asker and the people named are added.
    `people` / `organizations`: mentions read from the question by the router's model. If any is
    ambiguous, the context carries the candidates and no facts."""
    ctx = GraphContext(entities=link_entities(s, question))
    asker = link_asker(s, asker_email) if asker_email else None
    resolve_mentions(s, ctx, list(people), list(organizations), asker)
    if ctx.ambiguous:
        return ctx
    if asker and all(e.id != asker.id for e in ctx.entities):
        ctx.entities.insert(0, asker)          # pairs read from the asker's side
    people = [e for e in ctx.entities if e.entity_type == "Person"]
    external_orgs = [e for e in ctx.entities if e.entity_type == "Organization" and not e.is_internal]
    facts = [person_facts(s, e, ctx) if e.entity_type == "Person" else org_facts(s, e)
             for e in ctx.entities if not (asker and e.id == asker.id)]
    facts[:0] = [f'- In the question, "{e.matched}" means {e.name}.' for e in ctx.entities
                 if e.matched != ASKER and normalize_name(e.matched) != normalize_name(e.name)
                 and "@" not in e.matched and e.entity_type == "Person"]
    if asker:
        facts.insert(0, f"- The person asking is {asker.name} ({asker_email}); \"I\", \"we\", \"me\" and \"you\" "
                        f"in the question refer to {asker.name}.")
    facts += [pair_facts(s, a, b, ctx) for i, a in enumerate(people) for b in people[i + 1:]]
    facts += [org_person_facts(s, o, p, ctx) for o in external_orgs for p in people]
    ctx.facts = "\n".join(facts)
    return ctx


SYSTEM_SUFFIX = """

# Knowledge graph context
The documents are emails. Every search result begins with its From / To / Cc / Date / Subject lines.
The facts below are computed from the metadata of EVERY email in the corpus: all senders, recipients
and dates, with one person's several addresses already combined. Search results are only a sample.
- For who someone deals with, how many emails, how often, who at a company, and first or last contact,
  answer from these facts. They are complete; do not recount from search results.
- The facts say nothing about content. For what was said, use the search results and cite them.
{facts}
"""


def system_prompt(ctx: GraphContext) -> str:
    from brain.answer.prompts.chat_prompts import DEFAULT_SYSTEM_PROMPT

    # Graph markers stay out of brain's prompt: given them, the model attached [G#] to quoted email
    # text they do not belong to. Graph citations come only from the graph's own answer.
    facts = re.sub(r" ?\[G\d+\]", "", ctx.facts)
    return DEFAULT_SYSTEM_PROMPT + SYSTEM_SUFFIX.format(facts=facts or "(no entities recognized)")
