# GLiNER2.5 probe on Enron email bodies (2026-09-29)

This was exploration only, and nothing was written to the graph.
- **Script:** `scripts/gliner_probe.py`, using the `models` extra (`uv run --extra models ...`).
- **Model:** `fastino/gliner2.5-base-v1` in JointIE mode (typed endpoints, beam 32).
- **Hardware:** Mac, MPS.
- **Sample:** 150 randomly sampled emails from `runs/enron/json_5k`, message text only.

## Schema given to the model (mapped to the ontology)

| Model relation | Endpoints | Ontology relation |
|---|---|---|
| works_for | person → organization | WORKS_AT |
| manages / reports_to | person → person | MANAGES / REPORTS_TO |
| works_on | person → project | WORKS_ON |
| customer_of | organization → organization | CUSTOMER_OF |
| uses_product | organization → product | USES_PRODUCT |

## Speed

| Model | ms per email | Relations per 150 emails |
|---|---|---|
| gliner2.5-base-v1 (194M) | 530–610 | 477–596 |
| gliner2.5-small-v1 (74M) | 645 | 534 |

- The small model was **not faster** on MPS, probably because beam decoding dominates.
- At about 0.55 s per email, 1M emails would take about a week on one Mac. A cloud GPU should be much faster, but that is untested.

## Precision: 50 facts per run, graded by hand

Grades:
- **correct:** true and stated in the text.
- **header:** true, but read off an address or reply header, so metadata already has it.
- **wrong:** false, or the endpoint is not a real entity.

The grades are stored per fact in `runs/enron/gliner*/sample50.json`.

| Run | Correct | Header | Wrong | Confidence ≥ 0.8 |
|---|---|---|---|---|
| Raw body (quoted/forwarded text left in) | 9 | 16 | 25 | 9 facts: 4 correct, 5 wrong |
| Clean body (quoted/forwarded text removed) | 7 | 8 | 35 | 11 facts: 5 correct, 5 wrong |

**About 15–18% of relations are new, correct facts. Confidence does not separate right from wrong:** "sender works_for BNP PARIBAS" scored 0.89, and a prebon.com broker "works_for Enron" scored 0.93.

## Why it fails, in order of volume

1. **Non-human email.** Newsletters, store promotions, sports tip sheets and legal disclaimers produce most of the errors. Examples: "Count Basie works_for CDNOW", "UBSPaineWebber customer_of UBSPaineWebber". These senders slip past the bulk-sender rules because their addresses look like `2.3894.22-how7spz28_ri.1@cda01.cdnow.com`.
2. **Non-entities as endpoints:** "Company T" (a case study), "employees", "sender", "HR", "Enron trader" (a song parody).
3. **Self-loops by text:** "Enron customer_of Enron". JointIE blocks self-loops by span, not by name.
4. **Wrong relation from co-occurrence.** People in the same paragraph get `manages` or `reports_to`. The subject of a sports story "works_for" the opposing team.
5. **Header residue.** Lotus Notes inline replies ("Jennifer Burns 10/12/2000 03:43 PM To: …") have no banner, so they remain in the text.

## What worked

- **Entity spans are mostly clean:** full person names and company names with exact offsets.
- **Correct relations come from explicit phrasing:**
  - title apposition: "Robert R. Price, President, buy.com"; "Sean Sullivan, E-Commerce Manager, CDNOW"; "Lee Jackson, Enron Plastics and Petrochemicals"
  - announcements: "appointment of Jeff McMahon as Enron's chief financial officer"
  - lists: "New Hires … ENA - Judy Zhang"
  - "reporting to me"

## Implications

- **Used raw, the model is not a fact source.** Its output must go through the compiler as candidates, where it gets:
  - endpoint linking to *known* graph entities;
  - type and self-loop checks;
  - the acceptance threshold.
- **Before running it at all:**
  - **Skip non-human mail.** Use a structural signal, e.g. two-way correspondence with an internal person, not only sender patterns.
  - **Strip Lotus inline reply headers.**
- **GLiNER's best use here is probably NER.** Its spans could feed the dictionary linker and signature/title patterns, rather than its relation head being trusted directly.
- **Next measurement:** entity precision, and relation precision after gating (human mail only, both endpoints linked to known entities).
