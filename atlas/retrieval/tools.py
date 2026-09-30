"""Atlas and brain as tools for an answering model.

A platform's own agent loop registers `AtlasTools.schemas` and routes calls to
`AtlasTools.call`; `atlas.retrieval.agent` is the loop used here for tests and evaluation.

- The graph is exact over every email and meeting; search sees a sample. Counts, dates and
  who-met-whom come from the graph tools (find, interactions, contacts, participants);
  what was said from search and read.
- Emails and meetings are both interactions: people in roles at a time. An email is a
  Document (AUTHORED_BY sender, SENT_TO to/cc) dated by `created_at`; a meeting a Meeting
  (HAS_PARTICIPANT invitee, ATTENDED speaker) dated by `start_time`. `_PARTS` and
  `_EVENTS` put both in one shape, so no question needs code per source.
- Every record a tool returns carries a ref: G# for a graph record, S# for a passage of
  text. Refs live in the session's ledger, the only thing an answer may cite;
  atlas.retrieval.verify checks each citation against the entry it names.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.config import Settings
from atlas.ingestion.normalized import EARLIEST_PLAUSIBLE_DATE
from atlas.resolution.normalize import normalize_email, normalize_name
from atlas.retrieval.ask import LEAD, _affinity, _candidate, _org_candidates, _person_candidates, people_of

_PARTS = """parts AS (
    SELECT source_entity_id AS event, target_entity_id AS person, 'sender' AS role FROM kg.edges
     WHERE relation_type = 'AUTHORED_BY' AND status = 'active'
    UNION ALL SELECT source_entity_id, target_entity_id, COALESCE(properties->>'recipient_type', 'to') FROM kg.edges
     WHERE relation_type = 'SENT_TO' AND status = 'active'
    UNION ALL SELECT source_entity_id, target_entity_id, 'invitee' FROM kg.edges
     WHERE relation_type = 'HAS_PARTICIPANT' AND status = 'active'
    UNION ALL SELECT target_entity_id, source_entity_id, 'speaker' FROM kg.edges
     WHERE relation_type = 'ATTENDED' AND status = 'active')"""
_EVENTS = """events AS (
    SELECT d.id, CASE WHEN d.entity_type = 'Meeting' THEN 'meeting' ELSE 'email' END AS kind,
           d.canonical_name AS title, COALESCE(d.properties->>'created_at', d.properties->>'start_time') AS at
    FROM kg.entities d WHERE d.entity_type IN ('Document', 'Meeting') AND d.status = 'active')"""
_RECIPIENT = {"to", "cc", "bcc"}
_TEXT_LIMIT = 12000


@dataclass
class Entry:
    kind: str                  # "graph" | "text"
    data: dict                 # what the model was shown
    text: str                  # what a citation is checked against
    document_id: str | None = None
    title: str | None = None
    date: str | None = None


@dataclass
class Ledger:
    entries: dict[str, Entry] = field(default_factory=dict)

    def add(self, entry: Entry) -> str:
        prefix = "G" if entry.kind == "graph" else "S"
        for ref, known in self.entries.items():            # the same record keeps its ref
            if known.kind == entry.kind and known.text == entry.text:
                return ref
        ref = f"{prefix}{sum(e.kind == entry.kind for e in self.entries.values()) + 1}"
        self.entries[ref] = entry
        return ref

    def get(self, ref: str) -> Entry | None:
        return self.entries.get(ref)


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


_WINDOW = {"since": {"type": ["string", "null"], "description": "ISO date, inclusive"},
           "until": {"type": ["string", "null"], "description": "ISO date, exclusive"}}
_KIND = {"type": "string", "enum": ["any", "email", "meeting"]}

SCHEMAS = [
    _schema("find", "Resolve a name, email address or title to graph records: people, companies, meetings, "
            "documents. People are ranked by how much the asker deals with them. When several are close, the "
            "result says so: ask the user which one.",
            {"name": {"type": "string"},
             "type": {"type": ["string", "null"], "enum": ["Person", "Organization", "Meeting", "Document", None]}},
            ["name", "type"]),
    _schema("interactions", "Emails and meetings of a person or company (every one in the graph, exact), "
            "optionally only those shared with another person or company: totals by kind, who wrote to whom, "
            "first and last, and the most recent records. With a company as `other`, also each person there "
            "(by person), so no need to look up participants one email at a time.",
            {"entity": {"type": "string", "description": "id from find"},
             "other": {"type": ["string", "null"], "description": "id from find"},
             "kind": _KIND, **_WINDOW, "limit": {"type": "integer"}},
            ["entity", "other", "kind", "since", "until", "limit"]),
    _schema("contacts", "The people a person or company deals with most, by shared emails and meetings, with "
            "the last one. scope external = outside the asker's organization.",
            {"entity": {"type": "string", "description": "id from find"}, "kind": _KIND, **_WINDOW,
             "scope": {"type": "string", "enum": ["all", "internal", "external"]}, "limit": {"type": "integer"}},
            ["entity", "kind", "since", "until", "scope", "limit"]),
    _schema("participants", "Who was on an email or meeting, in what role (sender, to, cc, invitee, speaker).",
            {"event": {"type": "string", "description": "id of an email or meeting"}}, ["event"]),
    _schema("search", "Search the text of emails, meeting transcripts and documents. Returns passages; a "
            "sample, not everything.", {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ["query", "limit"]),
    _schema("read", "Read a whole email, meeting transcript or document (paged), e.g. to see who said what.",
            {"document_id": {"type": "string"}, "offset": {"type": "integer"}}, ["document_id", "offset"]),
]


class AtlasTools:
    """One question's worth of tool state: the asker and the ledger of citable records."""

    schemas = SCHEMAS

    def __init__(self, session: Session, settings: Settings, asker_email: str | None = None, brain=None):
        self.s, self.settings, self.ledger, self._brain = session, settings, Ledger(), brain
        self.asker = None
        if asker_email:
            row = self.s.execute(text("""SELECT e.id, e.canonical_name FROM kg.entities e
                JOIN kg.entity_external_ids x ON x.entity_id = e.id
                WHERE x.identifier_type = 'email' AND x.value = :v AND e.status = 'active'"""),
                                 {"v": normalize_email(asker_email)}).first()
            self.asker = {"id": str(row.id), "name": row.canonical_name, "email": asker_email} if row else None

    def call(self, name: str, args: dict) -> dict:
        if name not in {s["function"]["name"] for s in SCHEMAS}:
            return {"error": f"unknown tool {name}"}
        try:
            return getattr(self, name)(**args)
        except (ValueError, KeyError, TypeError) as exc:
            self.s.rollback()
            return {"error": str(exc)}

    # ---- graph -------------------------------------------------------------------------------

    def _cite(self, data: dict, event_id=None, title=None, date=None) -> str:
        document_id = self._document_id(event_id) if event_id else None
        return self.ledger.add(Entry("graph", data, json.dumps(data, default=str), document_id, title, date))

    def _document_id(self, entity_id) -> str | None:
        # An email's Document entity carries source_id, a Meeting its meeting_external_id: both "system:id".
        return self.s.execute(text("""SELECT d.id::text FROM kg.entity_external_ids x
            JOIN kg.documents d ON d.source_system || ':' || d.source_external_id = x.value
            WHERE x.entity_id = CAST(:i AS uuid) LIMIT 1"""), {"i": str(entity_id)}).scalar()

    def _entity(self, entity_id: str):
        try:
            uuid.UUID(entity_id)
        except ValueError:
            raise ValueError(f"{entity_id!r} is not an id; use an id returned by find") from None
        row = self.s.execute(text("""SELECT id, entity_type, canonical_name, properties FROM kg.entities
            WHERE id = CAST(:i AS uuid)"""), {"i": entity_id}).first()
        if row is None:
            raise ValueError(f"no entity {entity_id}")
        return row

    def _people(self, entity_id: str) -> list:
        row = self._entity(entity_id)
        return people_of(self.s, row.id) if row.entity_type == "Organization" else [row.id]

    def find(self, name: str, type: str | None = None) -> dict:
        asker_id = self.asker["id"] if self.asker else None
        if "@" in name:
            rows = self.s.execute(text("""SELECT e.id, e.entity_type, e.canonical_name, e.properties FROM kg.entities e
                JOIN kg.entity_external_ids x ON x.entity_id = e.id
                WHERE x.identifier_type = 'email' AND x.value = :v AND e.status = 'active'"""),
                                  {"v": normalize_email(name)}).all()
            groups = [(rows, False)]
        else:
            key = normalize_name(name)
            groups = []
            if type in (None, "Person"):
                groups.append(_person_candidates(self.s, key))
            if type in (None, "Organization"):
                groups.append(_org_candidates(self.s, key))
            if type in (None, "Meeting", "Document"):
                words = key.split()
                rows = self.s.execute(text(f"""SELECT id, entity_type, canonical_name, properties FROM kg.entities
                    WHERE status = 'active' AND entity_type = ANY(:types)
                      AND {' AND '.join(f"normalized_name LIKE :w{i}" for i in range(len(words))) or 'false'}
                    ORDER BY COALESCE(properties->>'created_at', properties->>'start_time') DESC NULLS LAST
                    LIMIT 10"""), {"types": [type] if type else ["Meeting", "Document"],
                                    **{f"w{i}": f"%{w}%" for i, w in enumerate(words)}}).all()
                groups.append((rows, True))
        out = []
        for rows, _ in groups:
            rows = list({r.id: r for r in rows}.values())
            people_orgs = [r for r in rows if r.entity_type in ("Person", "Organization")]
            scores = {}
            if people_orgs:
                scores |= _affinity(self.s, [r.id for r in people_orgs if r.entity_type == "Person"], asker_id)
                scores |= _affinity(self.s, [r.id for r in people_orgs if r.entity_type == "Organization"],
                                    asker_id, organizations=True)
            for r in rows:
                if r.entity_type in ("Person", "Organization"):
                    desc = _candidate(self.s, r, scores.get(r.id, 0))
                    desc["interactions_with_asker" if asker_id else "interactions"] = desc.pop("emails")
                else:
                    props = r.properties or {}
                    desc = {"name": r.canonical_name,
                            "date": props.get("created_at") or props.get("start_time")}
                record = {"id": str(r.id), "type": r.entity_type, **desc}
                out.append({"ref": self._cite(record, title=r.canonical_name), **record})
        out = [c for c in out if not self.asker or c["id"] != self.asker["id"]]
        score = "interactions_with_asker" if asker_id else "interactions"
        ranked = sorted((c for c in out if score in c), key=lambda c: -c[score])
        result = {"candidates": out[:15]}
        if len(ranked) > 1 and (ranked[0][score] == 0 or ranked[0][score] < LEAD * ranked[1][score]):
            result["note"] = "several close candidates: unless the question makes it clear, ask the user which one"
        if not out:
            result["note"] = f'nothing called "{name}" in the graph'
        return result

    def _events(self, people: list, others: list | None, kind: str, since, until) -> list:
        where = ["e.at >= :floor"]
        params = {"a": people, "b": others or [], "floor": EARLIEST_PLAUSIBLE_DATE.isoformat()}
        if since:
            where.append("e.at >= :since"); params["since"] = since
        if until:
            where.append("e.at < :until"); params["until"] = until
        if kind != "any":
            where.append("e.kind = :kind"); params["kind"] = kind
        return self.s.execute(text(f"""WITH {_PARTS}, {_EVENTS}
            SELECT e.id, e.kind, e.title, e.at,
                   array_agg(DISTINCT p.role) FILTER (WHERE p.person = ANY(:a)) AS a_roles,
                   array_agg(DISTINCT p.role) FILTER (WHERE p.person = ANY(:b)) AS b_roles,
                   array_agg(DISTINCT p.person::text || ' ' || p.role) FILTER (WHERE p.person = ANY(:b)) AS b_people
            FROM events e JOIN parts p ON p.event = e.id
            WHERE {' AND '.join(where)}
            GROUP BY e.id, e.kind, e.title, e.at
            HAVING count(*) FILTER (WHERE p.person = ANY(:a)) > 0
               AND (cardinality(CAST(:b AS uuid[])) = 0 OR count(*) FILTER (WHERE p.person = ANY(:b)) > 0)
            ORDER BY e.at DESC"""), params).all()

    def _event_record(self, r) -> dict:
        record = {"event_id": str(r.id), "kind": r.kind, "title": r.title, "date": (r.at or "")[:10]}
        return {"ref": self._cite(record, r.id, r.title, r.at), **record}

    def interactions(self, entity: str, other: str | None = None, kind: str = "any", since: str | None = None,
                     until: str | None = None, limit: int = 15) -> dict:
        a, b = self._people(entity), self._people(other) if other else None
        rows = self._events(a, b, kind, since, until)
        name_a = self._entity(entity).canonical_name
        name_b = self._entity(other).canonical_name if other else None
        summary = {"entity": name_a, "with": name_b, "kind": kind, "since": since, "until": until,
                   "total": len(rows), "emails": sum(r.kind == "email" for r in rows),
                   "meetings": sum(r.kind == "meeting" for r in rows)}
        if rows:
            summary["first_date"], summary["last_date"] = rows[-1].at[:10], rows[0].at[:10]
        records = [self._event_record(r) for r in rows[:max(1, min(limit, 50))]]
        if rows and len(rows) > len(records):
            records.append(self._event_record(rows[-1]))          # the first one, for "since when"
        result = {}
        if b is not None:
            def wrote(x_roles, y_roles):
                return "sender" in (x_roles or []) and bool(_RECIPIENT & set(y_roles or []))
            a_to_b = [r for r in rows if r.kind == "email" and wrote(r.a_roles, r.b_roles)]
            b_to_a = [r for r in rows if r.kind == "email" and wrote(r.b_roles, r.a_roles)]
            summary |= {"all emails and meetings both were on": summary.pop("total"),
                        "emails directly between them": len(a_to_b) + len(b_to_a),
                        f"emails {name_a} wrote to {name_b}": len(a_to_b),
                        f"emails {name_b} wrote to {name_a}": len(b_to_a),
                        "meetings together": summary.pop("meetings"),
                        "note": "'emails directly between them' is what they exchanged (one wrote, the other "
                                "received); 'all ... both were on' also counts mail others sent to both"}
            for label, subset in ((f"last email {name_a} to {name_b}", a_to_b),
                                  (f"last email {name_b} to {name_a}", b_to_a)):
                if subset:
                    result[label] = self._event_record(subset[0])
            meetings = [r for r in rows if r.kind == "meeting"]
            if meetings:
                result["last meeting together"] = self._event_record(meetings[0])
            if len(b) > 1:                                   # a company: who there, one record per person
                result["by person"], summary["distinct people there"] = self._by_person(rows, name_a, limit, a)
        return {"summary": {"ref": self._cite(summary), **summary}, **result, "records": records}

    def _by_person(self, rows, name_a: str, limit: int = 15, exclude=()) -> tuple[list[dict], int]:
        """For interactions with a company: the people there with the most interactions (volume,
        direction, dates), and how many people there are in all."""
        people: dict[str, dict] = {}
        for r in rows:                                       # newest first
            roles_by_person: dict[str, set] = {}
            for item in r.b_people or []:
                pid, role = item.split(" ", 1)
                roles_by_person.setdefault(pid, set()).add(role)
            for pid, roles in roles_by_person.items():
                if pid in {str(x) for x in exclude}:          # not the person asking, at their own company
                    continue
                row = people.setdefault(pid, {"total": 0, "sent_by_them": 0, "sent_to_them": 0,
                                              "first_date": None, "last_date": (r.at or "")[:10], "last": r})
                row["total"] += 1
                row["first_date"] = (r.at or "")[:10]
                if r.kind == "email" and "sender" in roles and _RECIPIENT & set(r.a_roles or []):
                    row["sent_by_them"] += 1
                if r.kind == "email" and "sender" in (r.a_roles or []) and _RECIPIENT & roles:
                    row["sent_to_them"] += 1
        out = []
        for pid, row in sorted(people.items(), key=lambda kv: -kv[1]["total"])[:limit]:
            person = self.s.execute(text("""SELECT e.canonical_name, (SELECT min(value) FROM kg.entity_external_ids x
                WHERE x.entity_id = e.id AND x.identifier_type = 'email') AS email FROM kg.entities e
                WHERE e.id = CAST(:i AS uuid)"""), {"i": pid}).first()
            last = row.pop("last")
            record = {"name": person.canonical_name, "email": person.email, "emails_and_meetings": row["total"],
                      f"emails they wrote to {name_a}": row["sent_by_them"],
                      f"emails {name_a} wrote to them": row["sent_to_them"],
                      "first_date": row["first_date"], "last_date": row["last_date"], "last_title": last.title}
            out.append({"ref": self._cite(record, last.id, last.title, last.at), **record})
        return out, len(people)

    def contacts(self, entity: str, kind: str = "any", since: str | None = None, until: str | None = None,
                 scope: str = "all", limit: int = 10) -> dict:
        a = self._people(entity)
        where = ["e.at >= :floor"]
        params = {"a": a, "floor": EARLIEST_PLAUSIBLE_DATE.isoformat()}
        if since:
            where.append("e.at >= :since"); params["since"] = since
        if until:
            where.append("e.at < :until"); params["until"] = until
        if kind != "any":
            where.append("e.kind = :kind"); params["kind"] = kind
        rows = self.s.execute(text(f"""WITH {_PARTS}, {_EVENTS},
            mine AS (SELECT DISTINCT p.event FROM parts p JOIN events e ON e.id = p.event
                     WHERE p.person = ANY(:a) AND {' AND '.join(where)})
            SELECT p.person, x.canonical_name AS name, count(DISTINCT p.event) AS n, max(e.at) AS last,
                   (array_agg(e.id ORDER BY e.at DESC))[1] AS last_event,
                   (SELECT bool_or((o.properties->>'is_internal')::boolean) FROM kg.edges w
                      JOIN kg.entities o ON o.id = w.target_entity_id
                     WHERE w.source_entity_id = p.person AND w.relation_type = 'WORKS_AT') AS internal,
                   (SELECT min(o.canonical_name) FROM kg.edges w JOIN kg.entities o ON o.id = w.target_entity_id
                     WHERE w.source_entity_id = p.person AND w.relation_type = 'WORKS_AT') AS organization
            FROM parts p JOIN mine m ON m.event = p.event JOIN events e ON e.id = p.event
            JOIN kg.entities x ON x.id = p.person
            WHERE p.person <> ALL(:a)
              AND EXISTS (SELECT 1 FROM parts r WHERE r.person = p.person AND r.role <> 'sender')
            GROUP BY p.person, x.canonical_name"""), params).all()
        if scope != "all":
            rows = [r for r in rows if bool(r.internal) == (scope == "internal")]
        rows = sorted(rows, key=lambda r: -r.n)
        out = []
        for r in rows[:max(1, min(limit, 30))]:
            last = self.s.execute(text("SELECT canonical_name FROM kg.entities WHERE id = :i"),
                                  {"i": r.last_event}).scalar()
            record = {"id": str(r.person), "name": r.name, "organization": r.organization,
                      "shared_emails_and_meetings": r.n, "last_date": (r.last or "")[:10], "last_title": last}
            out.append({"ref": self._cite(record, r.last_event, last, r.last), **record})
        return {"entity": self._entity(entity).canonical_name, "scope": scope, "contacts_in_all": len(rows),
                "shown": len(out), "contacts": out,
                "note": "the top contacts by shared emails and meetings; contacts_in_all counts every one. "
                        "Broadcast senders (addresses that never receive mail) are left out"}

    def participants(self, event: str) -> dict:
        ev = self._entity(event)
        rows = self.s.execute(text(f"""WITH {_PARTS}
            SELECT p.role, x.canonical_name AS name,
                   (SELECT min(value) FROM kg.entity_external_ids i WHERE i.entity_id = x.id
                     AND i.identifier_type = 'email') AS email
            FROM parts p JOIN kg.entities x ON x.id = p.person WHERE p.event = :e ORDER BY p.role, name"""),
                               {"e": ev.id}).all()
        props = ev.properties or {}
        people: dict[str, dict] = {}
        for r in rows:
            people.setdefault(r.name, {"name": r.name, "email": r.email, "roles": []})["roles"].append(r.role)
        record = {"event_id": str(ev.id), "kind": "meeting" if ev.entity_type == "Meeting" else "email",
                  "title": ev.canonical_name, "date": (props.get("created_at") or props.get("start_time") or "")[:10],
                  "people": list(people.values())}
        return {"ref": self._cite(record, ev.id, ev.canonical_name, record["date"]), **record,
                "document_id": self._document_id(ev.id)}

    # ---- text --------------------------------------------------------------------------------

    def _doc(self, document_id: str):
        return self.s.execute(text("""SELECT d.id::text AS id, d.title, d.source_type, d.source_created_at, v.normalized
            FROM kg.documents d JOIN kg.document_versions v ON v.id = d.current_version_id
            WHERE d.id = CAST(:i AS uuid)"""), {"i": document_id}).first()

    def search(self, query: str, limit: int = 6) -> dict:
        from brain import AccessScope
        from brain.models.results import SearchOptions

        from atlas.retrieval.brain_bridge import build_brain

        self._brain = self._brain or build_brain(self.settings)
        result = self._brain.searcher.search(query, access=AccessScope(bypass=True), options=SearchOptions(
            expand_queries=False, select_sections=False, expand_sections=False, max_llm_chunks=max(1, min(limit, 12))))
        out = []
        for section in (result.sections or [])[:max(1, min(limit, 12))]:
            doc_id = section.center_chunk.document_id
            row = self._doc(doc_id)
            date = row.source_created_at.isoformat()[:10] if row and row.source_created_at else None
            title = row.title if row else section.center_chunk.semantic_identifier
            passage = section.combined_content
            known = len(self.ledger.entries)
            ref = self.ledger.add(Entry("text", {"document_id": doc_id}, passage, doc_id, title, date))
            out.append({"ref": ref, "document_id": doc_id, "title": title, "date": date,
                        "source_type": row.source_type if row else None,
                        "text": passage if len(self.ledger.entries) > known else f"(shown before as {ref})"})
        return {"query": query, "passages": out}

    def read(self, document_id: str, offset: int = 0) -> dict:
        if (entry := self.ledger.get(document_id)) is not None:        # a ref works too
            document_id = entry.document_id or document_id
        row = self._doc(document_id)
        if row is None:
            raise ValueError(f"no document {document_id}")
        n = row.normalized
        people = "; ".join(f"{p.get('role')}: {p.get('name') or ''} <{p.get('email') or ''}>".replace(" <>", "")
                           for p in n.get("participants", []))
        body = "\n".join(sec["text"] for sec in n.get("sections", []) if not (sec.get("metadata") or {}).get("quoted"))
        whole = f"Title: {row.title}\nDate: {row.source_created_at}\nType: {row.source_type}\n{people}\n\n{body}"
        page = whole[offset: offset + _TEXT_LIMIT]
        ref = self.ledger.add(Entry("text", {"document_id": row.id, "offset": offset}, page, row.id, row.title,
                                    row.source_created_at.isoformat()[:10] if row.source_created_at else None))
        more = offset + _TEXT_LIMIT < len(whole)
        return {"ref": ref, "document_id": row.id, "title": row.title, "text": page,
                "next_offset": offset + _TEXT_LIMIT if more else None, "total_chars": len(whole)}
