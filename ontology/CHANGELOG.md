# Ontology changelog

Each version is an immutable directory. The pipeline refuses to run if a version's files change after first use.
Graph objects record the version they were created under.

## 1.2 (2026-09-28)

A policy-only change; no schema changes.

- **Trust rule `document.author` (0.95):** a document's author identified **by email** in structured metadata, for example Drive API metadata in an Atlas document JSON, becomes `Document AUTHORED_BY Person`.
- **Name-only authors from file metadata are never turned into edges.** This covers PDF/DOCX core properties, which are often junk.
- **Drive `owner` / `editor` roles are proposed as `OWNED_BY` / `EDITED_BY`.** These have no ontology home, so they surface as `NEW_ONTOLOGY_CANDIDATE` reviews for a governance decision.

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
