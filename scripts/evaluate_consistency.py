"""Run the same corpus through extraction N times and measure graph stability.

Each run starts from an empty graph (documents/chunks are kept) in a separate
database (`<DATABASE_URL db>_eval` by default), runs the full build with fresh
LLM calls, and exports the canonical graph. Outputs, under runs/<timestamp>/:

    run_01.json ... run_NN.json   canonical entities, edges + evidence, decisions, raw LLM output
    summary.json                  all metrics
    report.md                     human-readable report

Usage:
    python scripts/evaluate_consistency.py                      # Experiment A (dynamic types)
    python scripts/evaluate_consistency.py --ontology examples/ontology.json   # Experiment B
    python scripts/evaluate_consistency.py --from-dir runs/<ts> # recompute metrics only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from brain_v0.config import get_settings  # noqa: E402
from brain_v0.db.admin import GRAPH_TABLES, SOURCE_TABLES, ensure_database, migrate, sibling_database_url, truncate  # noqa: E402
from brain_v0.evaluation.consistency import consistency_report, raw_snapshot, render_markdown  # noqa: E402
from brain_v0.evaluation.snapshot import export_snapshot  # noqa: E402
from brain_v0.llm import get_llm  # noqa: E402
from brain_v0.pipeline.build_brain import build  # noqa: E402
from brain_v0.pipeline.ingest import ingest_path  # noqa: E402


def print_summary(report: dict) -> None:
    for r in report["per_run"]:
        print(f"Run {r['run']}: {r['entities']} entities, {r['relationships']} edges")
    print()
    print(f"Entity Jaccard mean (strict names):   {report['entity_jaccard_strict']['mean']:.2f}")
    print(f"Entity Jaccard mean (alias-aware):    {report['entity_jaccard_alias_aware']['mean']:.2f}")
    print(f"Relationship Jaccard mean (triple):   {report['relationship_jaccard_triple']['mean']:.2f}")
    print(f"Relationship Jaccard mean (dir pair): {report['relationship_jaccard_directed_pair']['mean']:.2f}")
    print(f"Relationship Jaccard mean (any pair): {report['relationship_jaccard_unordered_pair']['mean']:.2f}")
    print(f"Entity type consistency:              {report['entity_type_consistency']['fully_consistent']:.2f}")
    print(f"Relationship type consistency:        {report['relationship_type_consistency']['fully_consistent']:.2f}")
    print(f"Direction consistency:                {report['direction_consistency']['fully_consistent']:.2f}")
    print(f"Edges present in all runs:            {report['relationships_in_all_runs']} / {report['relationships_total_distinct']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--corpus", default="examples/corpus")
    parser.add_argument("--database-url", default=None, help="Defaults to <DATABASE_URL>_eval.")
    parser.add_argument("--out", default=None, help="Output directory (default runs/<timestamp>).")
    parser.add_argument("--ontology", default=None,
                        help="JSON file with allowed_entity_types / allowed_relationship_types (Experiment B).")
    parser.add_argument("--from-dir", default=None, help="Recompute metrics from existing run_*.json files.")
    parser.add_argument("--raw", action="store_true",
                        help="With --from-dir: score the raw LLM output (no resolution/validation) instead. "
                             "Writes summary_raw.json / report_raw.md.")
    args = parser.parse_args()

    if args.from_dir:
        out = Path(args.from_dir)
        snapshots = [json.loads(p.read_text()) for p in sorted(out.glob("run_*.json"))]
        meta = json.loads((out / "summary.json").read_text()).get("meta", {}) if (out / "summary.json").exists() else {}
        if args.raw:
            snapshots = [raw_snapshot(s) for s in snapshots]
            meta = {**meta, "variant": "raw LLM output, no resolution/validation"}
    else:
        settings = get_settings()
        experiment = "A (dynamic types)"
        if args.ontology:
            ontology = json.loads(Path(args.ontology).read_text())
            settings = settings.model_copy(update={
                "allowed_entity_types": ontology.get("allowed_entity_types", []),
                "allowed_relationship_types": ontology.get("allowed_relationship_types", []),
            })
            experiment = f"B (ontology: {args.ontology})"
        url = args.database_url or sibling_database_url(settings.database_url, "eval")
        ensure_database(url)
        migrate(url)
        engine = create_engine(url)
        truncate(engine, GRAPH_TABLES + SOURCE_TABLES)
        with Session(engine) as session, session.begin():
            ingested = ingest_path(session, args.corpus, settings)

        out = Path(args.out or f"runs/{datetime.now():%Y%m%d-%H%M%S}")
        out.mkdir(parents=True, exist_ok=True)
        llm = get_llm(settings)
        meta = {"experiment": experiment, "model": llm.model_name, "temperature": settings.llm_temperature,
                "seed": settings.llm_seed, "corpus": args.corpus, "documents": len(ingested.added),
                "chunks": ingested.chunks_written, "runs": args.runs, "database": url.rsplit("/", 1)[-1],
                "started": datetime.now().isoformat(timespec="seconds")}
        print(f"Experiment {experiment}: {meta['documents']} documents, {meta['chunks']} chunks, "
              f"{args.runs} runs with {llm.model_name}\n")

        snapshots = []
        for i in range(1, args.runs + 1):
            truncate(engine, GRAPH_TABLES)
            t0 = time.time()
            report = build(llm, None, engine=engine, settings=settings, force=True, label=f"consistency-{i:02d}")
            with Session(engine) as session:
                snap = export_snapshot(session)
            snap["build_stats"] = dict(report.stats)
            snap["errors"] = report.errors
            (out / f"run_{i:02d}.json").write_text(json.dumps(snap, indent=2, default=str))
            snapshots.append(snap)
            print(f"  run {i:02d}: {len(snap['entities'])} entities, {len(snap['relationships'])} edges "
                  f"({time.time() - t0:.1f}s, {len(report.errors)} errors)")
        print()

    report = consistency_report(snapshots)
    suffix = "_raw" if args.raw else ""
    (out / f"summary{suffix}.json").write_text(json.dumps({"meta": meta, **report}, indent=2, default=str))
    (out / f"report{suffix}.md").write_text(render_markdown(report, meta))
    print_summary(report)
    print(f"\nWrote {out}/report{suffix}.md and summary{suffix}.json")


if __name__ == "__main__":
    main()
