# Atlas V1: deterministic business knowledge graph

**Status:** Phase 1 complete, covering the ontology, schema, normalized documents and the structured layer.

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
| email `to`/`cc` | Document `SENT_TO` Person → **no V1.0 relation → review** | — |
| meeting `calendar_invitees` | Meeting `HAS_PARTICIPANT` Person (invited ≠ attended) | 0.95 |
| meeting transcript speaker (email, or invitee `matched_speaker_display_name`) | Person `ATTENDED` Meeting | 0.99 |
| meeting `recorded_by` | Person `ATTENDED` Meeting | 0.95 |
| `recorded_by.team` | Person `MEMBER_OF` Team | 0.90 |
| participant email domain (policy-controlled; generic domains excluded) | Person `WORKS_AT` Organization | 0.70 |
| meeting `action_items` | ActionItem → **no V1.0 type → review** | — |

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

## Decisions pending

1. **Ontology gaps surfaced by real data.** These are ontology governance calls, not pipeline changes:
   - email recipients (`SENT_TO` Document → Person): add a relation in V1.1, or keep them as document metadata only?
   - Fathom action items: add an `ActionItem` type (with `ASSIGNED_TO`) in V1.1?
2. **Organization display names are their domains** (`acme.com`) until a directory or the text layer supplies names. Identity is unaffected.
3. **Earlier amendments not yet applied**, since they matter once text extraction starts:
   - keep `RELATED_TO` / `ASSOCIATED_WITH` out of the LLM's choices;
   - the compiler computes evidence offsets from the quote (never trusting model offsets);
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
