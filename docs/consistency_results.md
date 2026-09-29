# Consistency results (Phase 1)

**Setup:**

| | |
|---|---|
| Corpus | `examples/corpus`: 6 documents, 6 chunks |
| Model | `gpt-4o-mini`, temperature 0, no seed |
| Runs | 10 per experiment, each starting from an empty graph with fresh LLM calls |
| Pipeline | Full: resolution + validation |
| Date | 2026-09-27 |

**Reproduce:**
```bash
uv run python scripts/evaluate_consistency.py --out runs/experiment_a
uv run python scripts/evaluate_consistency.py --ontology examples/ontology.json --out runs/experiment_b
```

Raw per-run outputs (`run_NN.json`, including the raw LLM output per chunk) are written to `runs/`, which is git-ignored.

## Numbers

| metric (pairwise over 45 run pairs) | A: dynamic | B: ontology |
|---|---|---|
| entities per run | 18–20 | 18–19 |
| edges per run | 25–33 | 25–33 |
| **entity Jaccard** mean (min–max) | **0.94** (0.86–1.00) | 0.92 (0.85–1.00) |
| entity Jaccard, alias-aware | 0.94 | 0.92 |
| entity type consistency | 1.00 | 0.95 (KRAS: Protein 9 / Gene 1) |
| **relationship Jaccard, full triple** | **0.45** (0.29–0.62) | **0.56** (0.39–0.76) |
| relationship Jaccard, directed pair (ignore type) | 0.58 | 0.63 |
| relationship Jaccard, unordered pair | 0.65 | 0.63 |
| relationship type consistency (same pair → same types) | 0.48 | **0.76** |
| direction consistency | 0.90 | 0.93 |
| edges present in all 10 runs / distinct edges seen | 12 / 78 | 14 / 57 |
| edges seen in only 1 run | 24 | 10 |

Rejections totalled over all 10 runs:

| reason | A | B |
|---|---|---|
| `evidence_not_in_source` | 13 | 3 |
| `self_relationship` | 8 | 6 |
| `source_substituted_in_evidence` | 2 | 9 |

## Findings

1. **Entities are stable; relationships are not.**
   - About 94% of the entity set carries over between any two runs; the worst pair is still 0.86.
   - About 45% of edge triples carry over.
   - The instability comes from three independent sources, which the three Jaccard granularities separate:
     - **which pairs get connected at all:** unordered pair 0.65
     - **direction:** directed pair 0.58
     - **the relation label:** triple 0.45
2. **Identity resolution holds across runs.**
   - Strict and alias-aware entity Jaccard are identical. Each run collapses Sotorasib / AMG 510 / Lumakras, adagrasib / MRTX849 / Krazati, and NSCLC / non-small cell lung cancer into the same canonical entity, with the same canonical name.
   - Entity drift is almost entirely about *recall*: whether minor entities are extracted at all, such as EGFR, docetaxel, or "KRYSTAL-1 trial" vs "KRYSTAL-1".
3. **Label vocabulary is the largest single source of edge instability in A.**
   - The same fact appears as `targets`, `inhibits`, `binds`, or `selectively_inhibits` in different runs.
   - The ontology (B) raises label consistency from 0.48 to 0.76 and cuts distinct edges from 78 to 57.
4. **Stable is not the same as correct.**
   - Constraining the vocabulary makes the model shoehorn facts into the nearest allowed type. Some *wrong* edges become perfectly stable in B (present in 10/10 runs):
     - `KRAS -[encodes]-> MAPK pathway`
     - `KRAS G12C -[variant_of]-> colorectal cancer`
     - `CodeBreaK 100 -[approved_for]-> Sotorasib`
   - A allows more label drift but produces fewer such forced errors.
   - Stability metrics must be paired with a correctness check: a gold set, or domain/range signatures.
5. **Validation catches a real class of errors.**
   - `evidence_not_in_source` catches paraphrased "quotes".
   - `source_substituted_in_evidence` catches edges whose quote is about a different entity of the same type. Example: the quote "AMG 510 selectively inhibits KRAS G12C" was offered as evidence for *adagrasib*.
   - `self_relationship` catches alias statements like "sotorasib, previously known as AMG 510" after the two names resolve to one entity.
   - **It does not catch semantically wrong but grounded edges.** Examples: `KRAS G12C -[targets]-> Sotorasib` (reversed), and `adagrasib -[approved_for]-> FDA`.
6. **Direction is mostly stable (0.90–0.93) but not canonical.**
   - Both `Sotorasib -[evaluated_in]-> CodeBreaK 100` and `CodeBreaK 100 -[evaluated_in]-> Sotorasib` occur, sometimes in the same run.
   - Inverse maps (`RELATIONSHIP_TYPE_INVERSES`) plus domain/range signatures would fix this.

## Baselines: is the instability ours or the LLM's?

Two additional arms were added on the same corpus with the same model (`gpt-4o-mini`, temperature 0, 10 runs):

- **Raw A.** The *same* 10 extractions as A, scored without resolution or validation:
  - entities are keyed by normalized name only, which matches Cognee's identity rule
  - every extracted edge is kept
  - computed with `evaluate_consistency.py --from-dir runs/experiment_a --raw`
- **Cognee.** Cognee's default `cognify` pipeline, installed from `references/cognee` at commit `c4cd8ce` in a separate venv (`scripts/cognee_baseline.py`):
  - its own prompt and `KnowledgeGraph` model
  - only Entity→Entity edges are kept, dropping structural `contains` / `is_a` / `is_part_of` / `made_from`

| metric | Ours A | Raw A (no resolution/validation) | Ours B (ontology) | Cognee |
|---|---|---|---|---|
| entities / run | 18–20 | 24–26* | 18–19 | 29–32 |
| edges / run | 25–33 | 29–37 | 25–33 | 41–45 |
| entity Jaccard | 0.94 | 0.96* | 0.92 | 0.93 |
| entity type consistency | 1.00 | 1.00 | 0.95 | **0.72** |
| **relationship Jaccard, triple** | **0.45** | **0.41** | **0.56** | **0.47** |
| relationship Jaccard, directed pair | 0.58 | 0.57 | 0.63 | 0.65 |
| relationship Jaccard, unordered pair | 0.65 | 0.64 | 0.63 | 0.75 |
| relationship type consistency | 0.48 | 0.57 | 0.76 | 0.42 |
| direction consistency | 0.90 | 0.90 | 0.93 | 0.99 |
| edges in all 10 runs / distinct | 12 / 78 | 11 / 89 | 14 / 57 | 12 / 96 |
| edges in 5–9 of 10 runs | 11 | 12 | 13 | 33 |

\* The raw arm keeps alias duplicates (AMG 510, Lumakras, … as separate nodes). They are extracted consistently in every run, which pads the stable part of the set and nudges entity Jaccard *up*. A graph that duplicates entities *consistently* looks more stable on this metric. Both arms have the same three unstable entities: EGFR, "EGFR antibodies", "KRYSTAL-1 trial".

### What the baselines show

1. **Edge instability comes from the LLM, not from either pipeline.**
   - Full-triple Jaccard is 0.41 for raw output, 0.45 for our pipeline and 0.47 for Cognee.
   - All three sit in the same band, with the same number of always-present edges (11–12).
   - Neither pipeline's post-processing materially changes which facts the model chooses to emit.
2. **Our resolution and validation improve precision, not stability.**
   - Relative to raw output, A merges alias duplicates: 89 → 78 distinct edges, with one canonical node per real entity.
   - It also rejects about 23 unsupported, misattributed or self-referential edges across the 10 runs.
   - Stability moves only 0.41 → 0.45.
3. **Only constraining the vocabulary moved stability.** B is the only arm that clearly changed triple Jaccard (0.56). Finding 4 above still applies: some of that stability is wrong edges made consistent.
4. **Cognee's edges recur more often, but not more reliably.**
   - Cognee has more edges present in 5–9 runs (33 vs 11) and much better pair and direction stability (0.75 / 0.99).
   - Part of this is alias edges we deliberately reject (`sotorasib -[previously_known_as]-> amg 510` is one of its 12 always-present edges).
   - Part may be its prompt, which asks for a one-sentence fact per edge. That is worth testing in our extractor as a single-variable change, via replay-free A/B runs.
5. **Cognee's type labels are the least stable (0.72).** It uses free-form lowercase types with no normalization or voting: "study" vs "clinical trial", "cancertype" vs "disease". Our PascalCase normalization plus majority vote holds types at 1.00 in A.
6. **Correctness was not measured for any arm.** Cognee's graph has errors of the same kind as ours, e.g. `amg 510 -[selectively_inhibits]-> wild-type kras`, where the text says it *spares* wild-type KRAS. A gold edge set remains the most important missing piece.

## Suggested next experiments

- **Domain/range signatures per relationship type**, e.g. `targets: Drug → Gene|Protein|Mutation` and `variant_of: Mutation → Gene`, enforced in `RelationshipValidator`. This should remove most of the "stable but wrong" edges in B.
- **A small hand-labelled gold edge set for this corpus**, to report precision and recall next to stability.
- **Ask for a one-sentence fact per edge in our prompt**, as Cognee does, and re-measure pair and direction stability.
- **Several extractions per chunk with a vote**, keeping only edges seen in ≥ k of n samples. Compare the cost against the stability gain; in run A, 41 of the 78 distinct edges appeared in ≥ 3 of 10 runs (frequency histogram in `summary.json`).
- **An LLM or NLI verifier for accepted edges**, to catch semantic errors that grounding checks miss.
- **Replay experiments.** `brain reset --keep-extractions && brain build --replay` re-runs resolution and validation on identical LLM output, so resolver changes can be measured without LLM noise.
