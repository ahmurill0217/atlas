"""Deterministic structured-field extractor (no model).

Turns the metadata of a NormalizedDocument (headers, invitees, speakers,
recorder, team) into candidates with provenance STRUCTURED_SOURCE. Precision
rules encoded here:
  - invited to a meeting  -> Meeting HAS_PARTICIPANT Person   (not ATTENDED)
  - spoke in / recorded it -> Person ATTENDED Meeting          (evidence of presence)
  - email sender          -> Document AUTHORED_BY Person
  - email domain          -> Person WORKS_AT Organization, only when the trust
                             policy enables it, the domain is not generic and the
                             address is not a bulk / mailing-list sender
  - email to/cc/bcc       -> Document SENT_TO Person (recipient_type)
  - meeting action item   -> ActionItem ORIGINATED_IN Meeting, ASSIGNED_TO Person
  - document author/creator with an email -> Document AUTHORED_BY Person
  - email address -> Person identity: `mailbox` identifier at the
    registrable domain (pallen@ect.enron.com == pallen@enron.com); "First Last" from a
    display name or a first.last address; organizations keyed by registrable domain
  - document owner / editor / viewer (e.g. Drive): kept as document metadata only
    (governance decision 2026-09-28), no edges proposed
The extractor proposes the same candidates whatever the ontology version; under
an ontology without a home for one, the compiler routes it to review.
"""

from __future__ import annotations

import hashlib
import uuid

from atlas.config import STRUCTURED_EXTRACTOR_VERSION
from atlas.extraction.candidates import CandidateEdge, CandidateEntity, CandidateSet, EvidenceRef
from atlas.ingestion.normalized import NormalizedDocument, Participant
from atlas.ontology.models import Ontology
from atlas.resolution.email_identity import (
    display_name,
    name_from_display,
    name_from_local,
    registrable_domain,
    split_address,
)
from atlas.resolution.normalize import domain_of, normalize_name

EXTRACTOR = "structured"
MIME_TYPES = {"email": "message/rfc822", "text": "text/plain", "pdf": "application/pdf",
              "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
PROVENANCE = "STRUCTURED_SOURCE"


class StructuredExtractor:
    def __init__(self, ontology: Ontology, internal_domains: list[str] | None = None):
        self.ontology = ontology
        self.trust = ontology.policies.structured_trust
        policy = ontology.policies.email_domain_employment
        self.domain_employment = bool(policy.get("enabled"))
        self.excluded_domains = {d.lower() for d in policy.get("excluded_domains", [])}
        self.bulk = policy.get("bulk_senders") or {}
        self.email_identity = ontology.policies.email_identity
        self.suffixes = self.email_identity.get("multi_part_suffixes", [])
        self.internal_domains = {self.org_domain(d.lower()) for d in (internal_domains or [])}

    def extract(self, doc: NormalizedDocument, document_version_id: uuid.UUID) -> CandidateSet:
        out = CandidateSet(document_id=doc.document_id, document_version_id=document_version_id,
                           extractor=EXTRACTOR, extractor_version=STRUCTURED_EXTRACTOR_VERSION)
        self._doc, self._version, self._out = doc, document_version_id, out
        self._entities: dict[str, CandidateEntity] = {}

        anchor = self._anchor(doc)
        people = {p.source_field: self._person(p) for p in doc.participants}

        if doc.source_type == "email":
            if doc.author and doc.author.source_field in people:
                self._edge(anchor, people[doc.author.source_field], "AUTHORED_BY", doc.author.source_field, "email.from")
            for p in doc.participants:
                if p.role in ("to", "cc", "bcc"):
                    self._edge(anchor, people[p.source_field], "SENT_TO", p.source_field, "email.recipients",
                               properties={"recipient_type": p.role})
        elif doc.source_type == "meeting":
            self._meeting_edges(doc, anchor, people)
        else:
            for p in doc.participants:
                if not p.email:
                    continue  # name-only file metadata never becomes an edge
                if p.role in ("author", "creator"):
                    self._edge(anchor, people[p.source_field], "AUTHORED_BY", p.source_field, "document.author")
                # Other roles (owner, editor, viewer, ...) stay in the stored
                # NormalizedDocument as metadata; the people are still resolved.

        for p in doc.participants:
            self._employment(p, people[p.source_field])
        out.entities = sorted(self._entities.values(), key=lambda e: e.local_id)
        out.edges = sorted(out.edges, key=lambda e: e.local_id)
        return out

    # --- entities ----------------------------------------------------------------

    def _add(self, entity: CandidateEntity) -> str:
        if entity.local_id in self._entities:
            existing = self._entities[entity.local_id]
            existing.aliases = sorted(set(existing.aliases) | set(entity.aliases))
            if not existing.name and entity.name:
                existing.name = entity.name
        else:
            self._entities[entity.local_id] = entity
        return entity.local_id

    def _base(self, source_field: str | None) -> dict:
        return dict(provenance_class=PROVENANCE, source_field=source_field, extractor=EXTRACTOR,
                    extractor_version=STRUCTURED_EXTRACTOR_VERSION)

    def _anchor(self, doc: NormalizedDocument) -> str:
        created = doc.created_at.isoformat() if doc.created_at else None
        if doc.source_type == "meeting":
            ids = {"meeting_external_id": f"{doc.source_system}:{doc.source_external_id}"}
            if doc.metadata.get("calendar_event_id"):
                ids["event_external_id"] = str(doc.metadata["calendar_event_id"])
            props = {"title": doc.title, "start_time": created,
                     "end_time": doc.metadata.get("recording_end_time") or doc.metadata.get("scheduled_end_time"),
                     "source_system": doc.source_system,
                     "meeting_external_id": ids["meeting_external_id"],
                     "is_external": doc.metadata.get("calendar_invitees_domains_type") == "one_or_more_external"}
            return self._add(CandidateEntity(local_id="meeting", suggested_type="Meeting", name=doc.title or "Meeting",
                                             identifiers=ids, properties={k: v for k, v in props.items() if v is not None},
                                             **self._base("recording_id")))
        source_id = f"{doc.source_system}:{doc.source_external_id}"
        props = {"title": doc.title, "source_system": doc.source_system, "source_id": source_id,
                 "created_at": created, "uri": doc.uri,
                 "mime_type": doc.metadata.get("mime_type") or MIME_TYPES.get(doc.source_type)}
        return self._add(CandidateEntity(local_id="document", suggested_type="Document",
                                         name=doc.title or ("(no subject)" if doc.source_type == "email"
                                                            else doc.source_external_id),
                                         identifiers={"source_id": source_id},
                                         properties={k: v for k, v in props.items() if v is not None},
                                         **self._base("message_id" if doc.source_type == "email" else "uri")))

    def _person(self, p: Participant) -> str:
        ids, props, name = {}, {}, p.name
        if p.email:
            local_id = f"person:email:{p.email}"
            local, domain = split_address(p.email)
            ids["email"] = p.email
            bulk = self.is_bulk_sender(p.email)       # lists / system senders: no name, no linking
            if self.email_identity.get("mailbox_across_subdomains") and not bulk \
                    and not self.is_generic_domain(domain):
                ids["mailbox"] = f"{local}@{self.org_domain(domain)}"
            parsed = None if bulk else name_from_display(p.name) or (
                name_from_local(local) if self.email_identity.get("names_from_address") else None)
            if parsed:
                name = display_name(*parsed)
                props = dict(zip(("first_name", "last_name"), name.split(" ", 1)))
            name = name or local
        else:
            local_id = f"person:name:{normalize_name(p.name or 'unknown')}"
        aliases = sorted({a for a in (p.name, name) if a})
        return self._add(CandidateEntity(local_id=local_id, suggested_type="Person", name=name, identifiers=ids,
                                         properties=props, aliases=aliases, **self._base(p.source_field)))

    def org_domain(self, domain: str) -> str:
        if not self.email_identity.get("mailbox_across_subdomains"):
            return domain
        return registrable_domain(domain, self.suffixes)

    def _employment(self, p: Participant, person_id: str) -> None:
        domain = domain_of(p.email) if p.email else None
        if not (self.domain_employment and domain) or self.is_generic_domain(domain):
            return
        if self.is_bulk_sender(p.email):
            return
        org = self.org_domain(domain)
        org_id = self._add(CandidateEntity(
            local_id=f"org:domain:{org}", suggested_type="Organization", name=org,
            identifiers={"domain": org},
            properties={"domain": org, "is_internal": org in self.internal_domains},
            **self._base(f"{p.source_field}.email")))
        self._edge(person_id, org_id, "WORKS_AT", f"{p.source_field}.email", "email_domain_employment",
                   properties={"email_domain": domain} if domain != org else None)

    def is_generic_domain(self, domain: str) -> bool:
        """Consumer mail providers, including their subdomains (email.msn.com)."""
        return any(domain == d or domain.endswith("." + d) for d in self.excluded_domains)

    def is_bulk_sender(self, email: str) -> bool:
        """Mailing-list, newsletter and system addresses (policy `bulk_senders`)."""
        local, _, domain = email.lower().partition("@")
        b = self.bulk
        labels = domain.split(".")
        return (local in b.get("local_parts", [])
                or local.startswith(tuple(b.get("local_part_prefixes", [])))
                or local.endswith(tuple(b.get("local_part_suffixes", [])))
                or (len(labels) > 2 and labels[0] in b.get("subdomain_labels", [])))

    # --- edges -----------------------------------------------------------------

    def _edge(self, src: str, tgt: str, relation: str, source_field: str, trust_key: str, properties: dict | None = None,
              evidence_text: str | None = None, section_ordinal: int | None = None) -> None:
        local_id = f"{src}|{relation}|{tgt}"
        if any(e.local_id == local_id for e in self._out.edges):
            return
        self._out.edges.append(CandidateEdge(
            local_id=local_id, source_local_id=src, target_local_id=tgt, suggested_relation=relation,
            provenance_class=PROVENANCE, extractor=EXTRACTOR, extractor_version=STRUCTURED_EXTRACTOR_VERSION,
            confidence=self.trust.get(trust_key, 0.0), properties=properties or {},
            evidence=EvidenceRef(document_id=self._doc.document_id, document_version_id=self._version,
                                 source_field=source_field, evidence_text=evidence_text,
                                 section_ordinal=section_ordinal, observed_at=self._doc.created_at)))

    def _meeting_edges(self, doc: NormalizedDocument, meeting: str, people: dict[str, str]) -> None:
        invitees = [p for p in doc.participants if p.role == "invitee"]
        for p in invitees:
            self._edge(meeting, people[p.source_field], "HAS_PARTICIPANT", p.source_field, "meeting.calendar_invitee")
        for p in doc.participants:
            if p.role == "speaker":
                self._edge(people[p.source_field], meeting, "ATTENDED", p.source_field, "meeting.transcript_speaker")
            elif p.role == "recorder":
                self._edge(people[p.source_field], meeting, "ATTENDED", p.source_field, "meeting.recorded_by")
                if p.team:
                    team_id = self._add(CandidateEntity(
                        local_id=f"team:{normalize_name(p.team)}", suggested_type="Team", name=p.team,
                        identifiers={"team_key": f"{doc.source_system}:{normalize_name(p.team)}"},
                        aliases=[p.team], **self._base(f"{p.source_field}.team")))
                    self._edge(people[p.source_field], team_id, "MEMBER_OF", f"{p.source_field}.team", "fathom.team")
        for s in doc.sections:
            if s.kind != "action_item":
                continue
            field = s.metadata.get("source_field")
            # Identity: the meeting plus the item's content, so reordering the list never swaps items.
            digest = hashlib.sha256(f"{s.metadata.get('recording_timestamp')}|{s.text}".encode()).hexdigest()[:16]
            props = {"description": s.text, "status": "completed" if s.metadata.get("completed") else "open",
                     "source_system": doc.source_system, "recording_timestamp": s.metadata.get("recording_timestamp"),
                     "playback_url": s.metadata.get("playback_url")}
            item = self._add(CandidateEntity(
                local_id=f"action_item:{digest}", suggested_type="ActionItem", name=s.text[:120],
                identifiers={"action_item_key": f"{doc.source_system}:{doc.source_external_id}:{digest}"},
                properties={k: v for k, v in props.items() if v is not None}, **self._base(field)))
            self._edge(item, meeting, "ORIGINATED_IN", field, "meeting.action_item",
                       evidence_text=s.text, section_ordinal=s.ordinal)
            email, name = s.metadata.get("assignee_email"), s.metadata.get("assignee_name")
            if email or name:
                assignee = self._person(Participant(name=name, email=email, role="assignee",
                                                    source_field=f"{field}.assignee"))
                self._edge(item, assignee, "ASSIGNED_TO", f"{field}.assignee", "meeting.action_item",
                           evidence_text=s.text, section_ordinal=s.ordinal)
