# Ontology changelog

Each version is an immutable directory. The pipeline refuses to run if a version's files change after first use.
Graph objects record the version they were created under.

## 1.3 (2026-09-29)

Driven by the first real-data run: 5,000 Enron emails.

- **Bulk senders** (`email_domain_employment.bulk_senders`): mailing-list, newsletter and system addresses never imply `WORKS_AT` and never get a name or address linking. Consumer ISPs were added to the generic domains, and subdomains of generic domains now match (`email.msn.com`).
- **Person identity field `mailbox`**: the local part at the registrable domain. `pallen@ect.enron.com` and `pallen@enron.com` are one person.
- **Organizations are keyed by registrable domain**, so `ect.enron.com` is Enron and counts as internal. The sending subdomain is kept as the `WORKS_AT` edge property `email_domain`.
- **Names from addresses** (`email_identity.names_from_address`): a display name, or else a first.last / first_last / first.m.last address, gives "First Last". Handles such as `pallen` are not names.
- **Address-pattern linking** (`email_identity.link_address_patterns`): a flast handle (`jadams`) links to the one first.last person it fits at the same organization. A lastname handle (`lavorato`) links only inside internal domains.
  - Links are never made when two or more people fit (that raises a POSSIBLE_DUPLICATE review), when the handle is someone's first name there (`john` vs `john.arnold`), or when a display name disagrees.
  - Each link is audited as `identity_linked`.

## 1.2 (2026-09-28)

A policy-only change; no schema changes.

- **Trust rule `document.author` (0.95):** a document's author identified **by email** in structured metadata, for example Drive API metadata in an Atlas document JSON, becomes `Document AUTHORED_BY Person`.
- **Name-only authors from file metadata are never turned into edges.** This covers PDF/DOCX core properties, which are often junk.
- **Drive `owner` / `editor` roles** were briefly proposed as `OWNED_BY` / `EDITED_BY`. Governance decision (2026-09-28): they stay document metadata only and no edges are proposed.

## 1.1 (2026-09-28)

Added in response to NEW_ONTOLOGY_CANDIDATE review items raised by real data under 1.0.

- **Entity `ActionItem`** (business): a task someone committed to or was assigned.
  - Identity: `action_item_key` (source system + meeting + content hash) or `external_id`.
  - Properties: description, status, due_date, source_system, recording_timestamp, playback_url.
- **`ASSIGNED_TO`**: ActionItem → Person.
- **`ORIGINATED_IN`**: ActionItem → Meeting | Document (at most one).
- **`SENT_TO`** (core): Document → Person. The edge property `recipient_type` is to | cc | bcc.
- New aliases (`task`, `action item`, `follow up`, `sent to`, `assigned to`, …) and trust rules (`email.recipients`, `meeting.action_item`).

Upgrading from 1.0:
- **Review items:** open `NEW_ONTOLOGY_CANDIDATE` items for these concepts are closed as `AUTO_RESOLVED`, and the change is audited.
- **Documents** are reprocessed under 1.1. New facts get `ontology_version = "1.1"`, while existing 1.0 entities and edges keep `1.0`.

## 1.0 (2026-09-28)

Initial Core + Business ontology.
