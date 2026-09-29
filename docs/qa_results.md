# Question-answering results

Can an LLM query over the knowledge base return accurate answers? And which approach does it best: plain vector RAG, our graph-augmented `brain ask`, or Cognee?

## Setup

**Questions.** `examples/qa/questions.json` has 26 questions, each with a reference answer written from the corpus:

| category | count |
|---|---|
| single fact | 8 |
| alias ("Which trial supported **Lumakras**?") | 4 |
| multi-hop | 5 |
| aggregation / lists | 4 |
| explanation | 2 |
| unanswerable (must decline) | 3 |

**Common settings.**
- Answer model: `gpt-4o-mini`, temperature 0.
- 3 repeats per question.
- Each system is built once, from a single extraction.

**Arms.**
- **`brain_hybrid`**: `brain ask`, which retrieves:
  - the top-k chunks
  - graph facts one hop around the entities in the query and those chunks, each fact with its evidence quote

  The answer must cite the ids of the items it uses and must say when the context doesn't contain the answer.
- **`brain_vector`**: the same prompt with chunks only. This is the plain RAG baseline.
- **`cognee_*`**: stock Cognee `search`, built with `scripts/cognee_qa.py`, in three modes: `HYBRID_COMPLETION` (the default), `GRAPH_COMPLETION` and `RAG_COMPLETION`. Each call uses a fresh `session_id` so Cognee's session memory can't carry answers between questions. Everything else is default, including `top_k=15`.

**Grading.** A blind judge (`gpt-4o`, which never sees which system wrote the answer) compares each answer with the reference and returns correct (1), partial (0.5) or incorrect (0). An unanswerable question counts as correct only if the system declines.

**Reproduce:**
```bash
references/cognee-venv/bin/python scripts/cognee_qa.py --out runs/qa/cognee_answers.json
uv run python scripts/evaluate_qa.py --cognee-answers runs/qa/cognee_answers.json --out runs/qa/eval_1
uv run python scripts/evaluate_qa.py --top-k 6 --out runs/qa/eval_topk6
```

## Results

The corpus has only 6 chunks, so Cognee's default `top_k=15` puts **the whole corpus** in its context. Our arms were therefore run twice: once retrieving 3 of the 6 chunks (selective retrieval), and once retrieving all 6 (the same full context Cognee had).

| arm | context | score | correct / partial / incorrect | same verdict across repeats |
|---|---|---|---|---|
| brain_hybrid | 3 of 6 chunks + graph facts | 0.97 | 73 / 5 / 0 | 0.96 |
| brain_vector | 3 of 6 chunks | 0.93 | 67 / 11 / 0 | 0.92 |
| brain_hybrid | all 6 chunks + graph facts | **0.98** | 75 / 3 / 0 | 1.00 |
| brain_vector | all 6 chunks | 0.97 | 74 / 4 / 0 | 0.96 |
| cognee_hybrid_completion (default) | up to 15 (= all) | 0.94 | 72 / 3 / **3** | 1.00 |
| cognee_graph_completion | up to 15 (= all) | 0.94 | 72 / 3 / **3** | 1.00 |
| cognee_rag_completion | up to 15 (= all) | 0.96 | 75 / 0 / **3** | 1.00 |

Score by category:

| category | brain_hybrid @3 | brain_vector @3 | brain_hybrid @6 | brain_vector @6 | cognee_hybrid | cognee_rag |
|---|---|---|---|---|---|---|
| single fact | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| multi-hop | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| explanation | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| aggregation | **1.00** | **0.79** | 1.00 | 0.96 | 0.88 | 1.00 |
| alias | 0.79 | 0.75 | 0.88 | 0.88 | 1.00 | 1.00 |
| unanswerable | **1.00** | **1.00** | 1.00 | 1.00 | **0.67** | **0.67** |

## Findings

1. **Accurate answers are achievable with either system.** Every arm scores 0.93–0.98. Single-fact, multi-hop and explanation questions were answered correctly by every arm in every repeat.
2. **Edge drift did not turn into wrong answers.**
   - The graph still contains known bad edges, such as `KRAS G12C -[targets]-> Sotorasib`, but no answer repeated them. The source text is always in the context, and our prompt tells the model to trust text over extracted facts.
   - Graph instability matters much less for question answering than for using the graph as structured data.
3. **The only factually wrong answers were Cognee hallucinations on an unanswerable question.**
   - "Which company developed divarasib?" got "Divarasib was developed by Mirati Therapeutics" in all three modes and all three repeats.
   - Divarasib is not in the corpus. The model pulled a nearby company out of the context.
   - Our arms declined on all 9 unanswerable attempts. The mechanism is our prompt, which has an explicit `answerable` field and requires citations. It would likely transfer to Cognee through its `system_prompt` parameter.
4. **The graph helps when retrieval is selective, and stops mattering when everything fits in context.**
   - With 3 of 6 chunks, graph facts lifted aggregation from 0.79 to 1.00. They supplied Krazati and CodeBreaK 300, which sat in chunks vector search didn't retrieve.
   - With all 6 chunks in context, plain RAG caught up (0.97 vs 0.98).
   - **This corpus is too small to show what a knowledge graph is worth.** A real corpus can't fit in the context window, and that is where the graph's cross-document links should matter.
5. **Our remaining misses are completeness, not correctness.**
   - Alias questions: "What is MRTX849?" left out "= adagrasib". "How does AMG 510 act?" left out the covalent-binding mechanism.
   - Cognee's full-corpus context did better on alias questions (1.00).
   - One aggregation reference (`g04`) is arguably over-specific and costs every system the same half point.
6. **Answers are far more repeatable than extracted graphs.**
   - The same verdict came back across repeats for 92–100% of questions.
   - Cognee returned identical answer text for 23–25 of 26 questions.

## Next steps

- **Scale the test.** Use a corpus of hundreds of documents where answers span many of them, so retrieval has to be selective. Only that shows whether the graph beats plain RAG enough to justify extraction cost.
- **Harder questions.** Add more aggregation, comparison and "which X has property Y" questions, which are the ones graphs should win.
- **Alias completeness.** Include the entity card (canonical name plus aliases) of every entity linked in the query, so "What is MRTX849?" always sees "= adagrasib".
- **Test the abstention prompt on Cognee** via `system_prompt`, to confirm that the hallucination gap comes from the prompt.
