# Atlas V1: deterministic business knowledge graph

**Status:** Phase 1 complete, covering the ontology, schema, normalized documents and the structured layer. The ontology is now at **V1.1** (see `ontology/CHANGELOG.md`).

The prototype (`brain/`, tag `prototype-v0`) is kept for reference and for its evaluation harnesses. V1 lives in `atlas/`, with tables in Postgres schema `kg`.

## Principle

Extractors only **propose candidates**. The ontology (`ontology/v1_0/*.yaml`) and the compiler (`atlas/compiler/`) decide what becomes a graph fact:

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

## Components

| Path | Role |
|---|---|
| `ontology/v1_0/core.yaml`, `business.yaml` | Entity types (with identity fields and properties), roles, and relations with allowed source/target types and constraints |
| `ontology/v1_0/aliases.yaml`, `mappings.yaml` | Language variation → canonical schema; context-dependent mappings (e.g. OWNS between Person and Project → OWNS_PROJECT) |
| `ontology/v1_0/validation.yaml` | Provenance classes and the evidence each requires; acceptance thresholds; structured-source trust; email-domain employment policy; resolution thresholds; normalization rules |
| `atlas/ontology/` | Loader (rejects inconsistent config at startup), mapper, validator. All pure functions of the ontology. |
| `atlas/ingestion/` | `NormalizedDocument` plus adapters: email JSON, meeting JSON (Fathom webhook shape), plain text/Markdown |
| `atlas/extraction/structured.py` | Metadata → candidates, provenance `STRUCTURED_SOURCE` |
| `atlas/resolution/` | Deterministic normalization; identity resolution (identifiers first, names never merge) |
| `atlas/compiler/` | The only decision-maker |
| `atlas/graph/` | Repository (the only writer) and a query layer, including `explain_edge` |
| `atlas/review/`, `atlas/provenance/` | Review queue (deduplicated, with frequency); append-only audit log (a DB trigger blocks UPDATE and DELETE) |
| `migrations/versions/0002_kg_v1_schema.py` | The `kg` schema |

## Structured rules (Phase 1)

| Source field | Candidate fact | Trust |
|---|---|---|
| email `from` | Document `AUTHORED_BY` Person | 1.00 |
| email `to`/`cc`/`bcc` | Document `SENT_TO` Person (`recipient_type`) *(1.1; under 1.0 → review)* | 1.00 |
| meeting `calendar_invitees` | Meeting `HAS_PARTICIPANT` Person (invited ≠ attended) | 0.95 |
| meeting transcript speaker (email, or invitee `matched_speaker_display_name`) | Person `ATTENDED` Meeting | 0.99 |
| meeting `recorded_by` | Person `ATTENDED` Meeting | 0.95 |
| `recorded_by.team` | Person `MEMBER_OF` Team | 0.90 |
| participant email domain (policy-controlled; generic domains excluded) | Person `WORKS_AT` Organization | 0.70 |
| meeting `action_items` | ActionItem `ORIGINATED_IN` Meeting, `ASSIGNED_TO` Person (quote = item text) *(1.1; under 1.0 → review)* | 0.95 |

## Identity resolution (Phase 1 policy)

- **Identifiers** (email, domain, meeting id, source id, team key) are normalized and matched exactly.
  - One owner → MATCH.
  - Several owners → POSSIBLE_DUPLICATE review.
  - None → CREATE.
- **Name only:**
  - No existing match → CREATE a weak (`name_only`) entity.
  - Any existing match → AMBIGUOUS_ENTITY_MATCH review.
  - A name alone never merges.
- **A new identified entity whose name matches a weak one** is created anyway and flagged POSSIBLE_DUPLICATE. False negatives are preferred over false merges.
- **Name-only entities also store a `source_mention` key** (source system + document + mention). Reprocessing the same document, for example after an ontology upgrade, finds its own entity, while the same bare name in *another* document still goes to review. The key never upgrades identity strength.

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
uv run pytest tests/v1
```

## Sample corpus result

`examples/business` has 3 emails, 2 Fathom-shaped meetings and 1 note. With **0 LLM calls**, it produces:

- 15 entities and 18 edges, with 26 evidence rows and **0 unsupported edges**;
- one Sarah and one Mike across all sources, including the upper-case email variant;
- Priya: `HAS_PARTICIPANT` but no `ATTENDED`; Dana (gmail.com): no `WORKS_AT`;
- 3 review items:
  - `SENT_TO` (×4)
  - `ActionItem` (×2)
  - the name-only "Sarah Chen" invitee (AMBIGUOUS, not merged)

Re-running is a no-op, and two fresh runs produce identical graphs.

## Ontology versions

- **V1.1** added `ActionItem`, `ASSIGNED_TO`, `ORIGINATED_IN` and `SENT_TO`. These were the two gaps real data surfaced under V1.0.
- **Upgrading a graph built under 1.0:**
  - the open gap reviews close automatically (`AUTO_RESOLVED`, audited);
  - every document is reprocessed under 1.1;
  - new facts are tagged `1.1`, and existing `1.0` records are untouched.
- On the sample corpus the upgrade adds 2 entities and 8 edges, opens **0** new review items, and leaves 1 genuine AMBIGUOUS review: a name-only "Sarah Chen".
- Selecting a version: `ATLAS_ONTOLOGY_DIR=ontology/v1_0` (the default is v1_1).

## Still open

1. **Organization display names are their domains** (`acme.com`) until a directory or the text layer supplies names.
2. **Earlier amendments for text extraction**, applied from Phase 3:
   - keep `RELATED_TO` / `ASSOCIATED_WITH` out of the LLM's choices;
   - compute evidence offsets in the compiler;
   - relation-pass gating;
   - review priority and auto-resolve rules;
   - a two-level stability target.

## Next phases

- **Phase 2:** PDF and DOCX adapters (page-aware), plus a generic structured JSON adapter.
- **Phase 3+:** text extraction tiers, cheapest first, all producing candidates into the same compiler:
  1. dictionary linking and rules;
  2. small local models;
  3. LLM only for the residual.
- **Also:** scored entity resolution; the review API; a golden dataset; the 10-run stability test.
