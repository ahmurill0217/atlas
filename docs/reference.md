# Atlas reference: ingestion, ontology, compiler

This page covers how documents become graph facts. For how questions are answered, see `docs/architecture.md`.

## Principle

Extractors only **propose candidates**. The ontology (`ontology/*.yaml`) and the compiler (`atlas/compiler/`) decide what becomes a graph fact:

- Nothing outside the ontology can be written.
- Ambiguity goes to review instead of being guessed.
- Every edge carries evidence.
- Every decision is stored and can be replayed.

## Pipeline

```
source file ──► adapter ──► NormalizedDocument ──► segment ──► persist (document / version / sections)
                  (atlas/ingestion)                              │ same version already processed? → no-op
                                                                 ▼
                                   extract: StructuredExtractor (code only, no model)   → CandidateSet
                                                                 ▼
                                   GraphCompiler (atlas/compiler)
                                     entities: map type → validate → resolve identity → create | match | review
                                     edges:    endpoints resolved → map relation → validate → cardinality
                                               → confidence policy → commit edge + evidence | review | reject
                                                                 ▼
                                   kg.entities / kg.edges / kg.edge_evidence / kg.review_items / kg.audit_log
```

A document version is processed once per (pipeline version, ontology checksum), so re-running a corpus is a no-op.

## Components

| Path | Role |
|---|---|
| `ontology/core.yaml`, `business.yaml` | Entity types (identity fields, properties), roles, and relations with allowed source/target types and constraints |
| `ontology/aliases.yaml`, `mappings.yaml` | Language variation → canonical schema; context-dependent mappings (e.g. OWNS between Person and Project → OWNS_PROJECT) |
| `ontology/validation.yaml` | Provenance classes and the evidence each requires; acceptance thresholds; structured-source trust; employment-by-domain policy (generic domains, bulk senders); email identity policy |
| `atlas/ontology/` | Loader (rejects inconsistent config at startup), mapper, validator. All pure functions of the ontology. |
| `atlas/ingestion/` | `NormalizedDocument` plus adapters: email JSON, meeting JSON (Fathom webhook shape), generic "Atlas document" JSON (Drive-style metadata), PDF (page-aware), DOCX (heading- and table-aware), plain text/Markdown |
| `atlas/ingestion/segmentation.py` | Splits only oversized blocks, at sentence boundaries, keeping offsets exact |
| `atlas/extraction/structured.py` | Metadata → candidates, provenance `STRUCTURED_SOURCE` |
| `atlas/resolution/` | Normalization, identity resolution, email identity rules |
| `atlas/compiler/` | The only decision-maker |
| `atlas/graph/` | Repository (the only writer), sweep (a changed or deleted document replaces its contribution), queries including `explain_edge`, HTML viewer |
| `atlas/review/`, `atlas/provenance/` | Review queue (deduplicated, with frequency); append-only audit log (a DB trigger blocks UPDATE and DELETE) |
| `migrations/versions/0002_kg_v1_schema.py`, `0003_…` | The `kg` schema |

## Structured rules

| Source field | Candidate fact | Trust |
|---|---|---|
| email `from` | Document `AUTHORED_BY` Person | 1.00 |
| email `to` / `cc` / `bcc` | Document `SENT_TO` Person (`recipient_type`) | 1.00 |
| document author / creator identified by email | Document `AUTHORED_BY` Person | 0.95 |
| meeting `calendar_invitees` | Meeting `HAS_PARTICIPANT` Person (invited ≠ attended) | 0.95 |
| meeting transcript speaker (email, or invitee `matched_speaker_display_name`) | Person `ATTENDED` Meeting | 0.99 |
| meeting `recorded_by` | Person `ATTENDED` Meeting | 0.95 |
| `recorded_by.team` | Person `MEMBER_OF` Team | 0.90 |
| meeting `action_items` | ActionItem `ORIGINATED_IN` Meeting, `ASSIGNED_TO` Person (quote = item text) | 0.95 |
| participant email domain (not generic, not a bulk sender) | Person `WORKS_AT` Organization | 0.70 |

Kept as metadata, not edges:
- Drive owner / editor / viewer roles.
- Name-only file-metadata authors ("Microsoft Office User").

## Identity resolution

- **Identifiers** (email, mailbox, domain, meeting id, source id, team key) are normalized and matched exactly.
  - One owner → MATCH.
  - Several owners → POSSIBLE_DUPLICATE review.
  - None → CREATE.
- **Email identity** (policy `email_identity`):
  - `mailbox` = local part at the registrable domain, so `pallen@ect.enron.com` is `pallen@enron.com`. Organizations are keyed by registrable domain.
  - "First Last" comes from a display name or a first.last address.
  - A flast handle (`jadams`) links to the one first.last person it fits at the same organization. A lastname handle (`lavorato`) links only inside internal domains.
  - Nothing links when several people fit (POSSIBLE_DUPLICATE review instead), when the handle is someone's first name, or when a display name disagrees. Links are audited as `identity_linked`.
- **Name only:**
  - No existing match → CREATE a weak (`name_only`) entity.
  - Any existing match → AMBIGUOUS_ENTITY_MATCH review.
  - A name alone never merges.
- **Name-only entities store a `source_mention` key**, so reprocessing the same document finds its own entity.
- **Property provenance:** each property records the document that set it.
  - The same document re-describing itself supersedes its old value (audited).
  - A *different* document disagreeing raises CONFLICTING_FACT.
- **Source-native IDs are case-sensitive** (Drive IDs, message IDs, paths, meeting IDs). Only emails, mailboxes, domains and team keys are case-folded.

## Ingestion details

- **Segmentation** follows each format's natural boundaries:
  - email: new text vs quoted chain (Outlook banners, indented banners, "Forwarded by", Lotus Notes inline headers);
  - meetings: summary, speaker turns, action items;
  - Markdown / DOCX: heading sections, paragraphs, tables;
  - PDF: pages, then headings and paragraphs.
- **Oversized blocks** (over 2,000 characters) are split only at sentence boundaries.
- **PDF:**
  - Extraction artifacts are repaired: ligatures, hyphenation, small caps, NUL bytes.
  - A scanned PDF is ingested and flagged `needs_ocr`.
  - An unreadable file fails alone; the run continues.
- **Dates:** a source date before 1990 is a placeholder. It becomes unknown, and the raw value is kept in metadata.
- **Generic JSON (`atlas_document: "1.0"`)** is the contract any connector can emit: metadata, participants with roles, and text or sections.

## Ontology versions

- **The ontology is versioned** (`version:` in every file; currently 1.0, see `ontology/CHANGELOG.md`). A graph records the checksum of each version it uses, and the pipeline refuses to run if those files change afterwards. Bump the version instead of editing in place.
- **A new version:**
  - closes the review items it now covers (`AUTO_RESOLVED`, audited);
  - reprocesses documents;
  - leaves records created under the old version untouched (`tests/test_ontology_upgrade.py`).
- **Selecting a directory:** `ATLAS_ONTOLOGY_DIR` (default `ontology/`).

## Run

```bash
uv run alembic upgrade head
export ATLAS_INTERNAL_DOMAINS='["northwind.io"]'   # your own email domains
uv run python -m atlas ontology --details
uv run python -m atlas ingest examples/business
uv run python -m atlas entities
uv run python -m atlas edges
uv run python -m atlas entity sarah.chen@acme.com
uv run python -m atlas why <edge-id>          # evidence → candidates → mapping → decision → audit
uv run python -m atlas reviews
uv run python -m atlas stats                  # unsupported_edges must be 0
uv run pytest
```

## Sample corpus result

`examples/business` holds 9 documents: 3 emails, 2 Fathom-shaped meetings, a Drive JSON document, a DOCX, a PDF and a Markdown note. With **0 LLM calls** it produces:

- 21 entities and 27 edges, with 38 evidence rows and **0 unsupported edges**;
- one Sarah and one Mike across all sources, including an upper-case email variant;
- Priya with `HAS_PARTICIPANT` but no `ATTENDED`; Dana (gmail.com) with no `WORKS_AT`;
- 1 review item: the name-only "Sarah Chen" invitee (AMBIGUOUS, not merged).

Re-running is a no-op, and two fresh runs produce identical graphs.

## Still open

- **Organization display names are their domains** (`acme.com`) until a directory or the text layer supplies names.
- **Text extraction**, if added, produces candidates into the same compiler, cheapest tier first:
  1. linking known entity names;
  2. deterministic patterns (signatures, titles);
  3. models only for the residual.

  Findings so far: `docs/findings.md`.
