"""Baseline: run the same corpus through Cognee's default `cognify` N times.

Runs in a separate virtualenv with Cognee installed from references/cognee
(see docs/consistency_results.md), NOT in the brain project env:

    uv venv references/cognee-venv --python 3.12
    VIRTUAL_ENV=references/cognee-venv uv pip install -e references/cognee
    references/cognee-venv/bin/python scripts/cognee_baseline.py --runs 10

Each run prunes Cognee's stores, adds the corpus, runs cognify with the same
model as our experiments (gpt-4o-mini, temperature 0), and exports the entity
graph in the snapshot format that brain.evaluation.consistency scores:
Entity nodes (type from their `is_a` EntityType), and Entity->Entity edges
only (structural contains / is_a / is_part_of / made_from edges are dropped).
Score with the brain env afterwards:

    uv run python scripts/evaluate_consistency.py --from-dir runs/cognee_baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRUCTURAL = {"contains", "is_a", "is_part_of", "made_from", "belongs_to_set", "exists_in"}


def allow_special_tokens_in_text() -> None:
    """Runtime shim, not a Cognee patch: tiktoken refuses to encode special-token
    strings such as '<|endofprompt|>' by default. Cognee's chunker counts tokens
    with it, so one paper quoting such a string (the GPT-4 report does) aborts
    the whole cognify. Document text should be encoded as plain text."""
    import tiktoken.core

    original = tiktoken.core.Encoding.encode
    if getattr(original, "_plain_text_shim", False):
        return

    def encode(self, text, *, allowed_special=frozenset(), disallowed_special=()):
        return original(self, text, allowed_special=allowed_special, disallowed_special=disallowed_special)

    encode._plain_text_shim = True
    tiktoken.core.Encoding.encode = encode


def configure_env(model: str) -> None:
    """Point Cognee at the same model as our experiments. The API key is read
    from the brain project's .env and only placed in this process's env."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        raise SystemExit("OPENAI_API_KEY not found in environment or .env")
    data = ROOT / "references" / "cognee-data"
    os.environ.update({
        "LLM_PROVIDER": "openai", "LLM_MODEL": f"openai/{model}", "LLM_API_KEY": key,
        "LLM_TEMPERATURE": "0",
        "EMBEDDING_PROVIDER": "openai", "EMBEDDING_MODEL": "openai/text-embedding-3-small",
        "EMBEDDING_DIMENSIONS": "1536", "EMBEDDING_API_KEY": key,
        "DATA_ROOT_DIRECTORY": str(data / "data"), "SYSTEM_ROOT_DIRECTORY": str(data / "system"),
        "TELEMETRY_DISABLED": "1",
    })


async def export_entity_graph() -> dict:
    from cognee.infrastructure.databases.graph import get_graph_engine

    engine = await get_graph_engine()
    nodes, edges = await engine.get_graph_data()
    props = {str(node_id): p for node_id, p in nodes}
    entity_ids = {i for i, p in props.items() if p.get("type") == "Entity"}
    type_of = {}
    for src, tgt, rel, _ in edges:
        if rel == "is_a" and str(src) in entity_ids:
            type_of[str(src)] = props.get(str(tgt), {}).get("name")
    entities = [{"name": props[i].get("name"), "type": type_of.get(i) or "Unknown", "aliases": [],
                 "description": props[i].get("description")} for i in sorted(entity_ids)]
    relationships = []
    for src, tgt, rel, edge_props in edges:
        src, tgt = str(src), str(tgt)
        if rel in STRUCTURAL or src not in entity_ids or tgt not in entity_ids:
            continue
        relationships.append({"source": props[src].get("name"), "type": rel, "target": props[tgt].get("name"),
                              "support": 1, "edge_text": (edge_props or {}).get("edge_text")})
    return {"entities": entities,
            "relationships": sorted(relationships, key=lambda r: (r["source"], r["type"], r["target"])),
            "graph_totals": {"nodes": len(nodes), "edges": len(edges)}}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--corpus", default=str(ROOT / "examples" / "corpus"))
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--out", default=str(ROOT / "runs" / "cognee_baseline"))
    args = parser.parse_args()
    configure_env(args.model)
    allow_special_tokens_in_text()

    import cognee  # after env is configured

    files = sorted(str(p) for p in Path(args.corpus).iterdir() if p.suffix.lower() in {".md", ".txt", ".pdf"})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for i in range(1, args.runs + 1):
        t0 = time.time()
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        await cognee.add(files, dataset_name="corpus")
        await cognee.cognify(datasets=["corpus"])
        snap = await export_entity_graph()
        (out / f"run_{i:02d}.json").write_text(json.dumps(snap, indent=2, default=str))
        print(f"run {i:02d}: {len(snap['entities'])} entities, {len(snap['relationships'])} edges "
              f"({time.time() - t0:.1f}s)", flush=True)
    meta = {"experiment": "Cognee baseline (default cognify, KnowledgeGraph model)", "model": args.model,
            "temperature": 0.0, "corpus": args.corpus, "runs": args.runs,
            "cognee_commit": "c4cd8ceb9509dff6bddfabdadbeab7cc040bc32b"}
    (out / "summary.json").write_text(json.dumps({"meta": meta}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
