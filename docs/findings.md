# Findings that shaped the design

This is a record of the experiments whose code has since been removed. The code is in git history: tag `prototype-v0` holds the LLM prototype, and commit `03375c4` holds the GLiNER probe.

## 1. LLM extraction is unstable on relationships (prototype, KRAS corpus)

- **Setup:** 10 repeated runs of LLM entity and relationship extraction over the same corpus.
- **Entities were stable:** alias-aware Jaccard about 0.94.
- **Relationships were not:**

  | Run | Triple Jaccard |
  |---|---|
  | Raw LLM output | 0.41 |
  | Our resolver | 0.45 |
  | Cognee | 0.47 |
  | With a fixed ontology | 0.56 |

- The drift comes from the model, and Cognee shows the same behavior.
- On the question-answering side, both systems scored 0.93–0.98. Cognee invented one relationship; our prototype abstained.

**Consequence:** the graph is built deterministically from metadata. Models only propose; a compiler decides.

## 2. Cognee as a base (comparison, stopped)

- Same corpus, same questions.
- No advantage over our pipeline, and a heavier dependency.
- The comparison was stopped as not informative.

## 3. GLiNER2.5 relation extraction on email bodies (Enron)

- **Model:** base, JointIE, typed schema mapped to the ontology.
- **Precision:** 15–18% of extracted relations were new and correct, on 2 × 50 graded facts.
- **Confidence scores** did not separate right from wrong.
- **Error sources:** newsletters, non-entities ("Company T"), and co-occurrence mistaken for relation.
- **What worked:** entity spans were clean, and relations were right only for explicit phrasing (title apposition, announcements).
- **Speed:** about 0.55 s per email on a Mac GPU; the small model was not faster.

**Consequence:** model-based relation extraction is not a fact source here. If used later, use it for entity spans feeding deterministic rules.

## 4. Graph vs retrieval for answering (Enron)

See `docs/ask_evaluation.md`. In short: the graph for relationships, retrieval for content, both for mixed questions. The graph should never filter or boost the search.

## 5. Data quirks worth keeping in mind for real mail

- **Duplicate copies:** the same email appears once per mailbox (about 50% duplicates in Enron).
- **Reply chains:** quoted chains with indented banners, "Forwarded by" blocks, and Lotus Notes inline headers.
- **One person, several addresses:** subdomains, flast / lastname handles.
- **Broadcast senders:** list senders on subdomains (`mailman.`, `lists.`, `info.`) and consumer ISP subdomains.
- **Placeholder dates** (1980-01-01).
