"""Run-to-run stability metrics for extracted graphs.

A *snapshot* is the canonical graph after one full build:
    {"entities": [{"name", "type", "aliases": [...]}, ...],
     "relationships": [{"source", "type", "target", "support"}, ...]}

Two views of entity identity are measured:
  strict       - normalized canonical name. Penalizes a run that picked
                 "AMG 510" as the canonical name where others picked "Sotorasib".
  alias-aware  - entities from all runs are clustered when their name/alias
                 sets overlap, so the same thing under a different canonical
                 name still counts as the same entity.
Relationship keys use the alias-aware entity ids, at three granularities:
  triple (source, type, target) / directed pair (source, target) / unordered pair.
"""

from __future__ import annotations

import itertools
import statistics
from collections import Counter, defaultdict
from typing import Any

from brain_v0.normalize import normalize_name


def jaccard(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def pairwise(sets: list[set]) -> dict[str, float]:
    scores = [jaccard(a, b) for a, b in itertools.combinations(sets, 2)]
    if not scores:
        return {"mean": 1.0, "min": 1.0, "max": 1.0}
    return {"mean": round(statistics.mean(scores), 4), "min": round(min(scores), 4), "max": round(max(scores), 4)}


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def entity_clusters(snapshots: list[dict]) -> list[dict[str, str]]:
    """Per run: normalized canonical name -> cross-run cluster label."""
    uf = _UnionFind()
    for snap in snapshots:
        for e in snap["entities"]:
            forms = {normalize_name(e["name"])} | {normalize_name(a) for a in e.get("aliases", [])}
            forms.discard("")
            first, *rest = sorted(forms)
            for f in rest:
                uf.union(first, f)
    # Label each cluster by its most common canonical name across runs.
    names_by_root: dict[str, Counter] = defaultdict(Counter)
    for snap in snapshots:
        for e in snap["entities"]:
            names_by_root[uf.find(normalize_name(e["name"]))][e["name"]] += 1
    label = {root: c.most_common(1)[0][0] for root, c in names_by_root.items()}
    return [{normalize_name(e["name"]): label[uf.find(normalize_name(e["name"]))] for e in snap["entities"]}
            for snap in snapshots]


def consistency_report(snapshots: list[dict]) -> dict[str, Any]:
    n = len(snapshots)
    clusters = entity_clusters(snapshots)

    strict_entities = [{normalize_name(e["name"]) for e in s["entities"]} for s in snapshots]
    aliased_entities = [set(c.values()) for c in clusters]

    triples, directed, undirected = [], [], []
    types_by_run: list[dict[str, str]] = []
    for snap, cmap in zip(snapshots, clusters):
        t, d, u = set(), set(), set()
        for r in snap["relationships"]:
            s, o = cmap[normalize_name(r["source"])], cmap[normalize_name(r["target"])]
            t.add((s, r["type"], o))
            d.add((s, o))
            u.add(frozenset((s, o)))
        triples.append(t), directed.append(d), undirected.append(u)
        types_by_run.append({cmap[normalize_name(e["name"])]: e["type"] for e in snap["entities"]})

    # Entity type consistency across runs where the entity appears.
    entity_types: dict[str, list[str]] = defaultdict(list)
    for run_types in types_by_run:
        for ent, typ in run_types.items():
            entity_types[ent].append(typ)
    multi = {e: ts for e, ts in entity_types.items() if len(ts) >= 2}
    type_consistent = [e for e, ts in multi.items() if len(set(ts)) == 1]
    modal_share = [Counter(ts).most_common(1)[0][1] / len(ts) for ts in multi.values()]

    # Relationship type consistency: per unordered pair, same set of types in every run it appears.
    pair_types: dict[frozenset, list[frozenset]] = defaultdict(list)
    for t in triples:
        per_pair: dict[frozenset, set] = defaultdict(set)
        for s, typ, o in t:
            per_pair[frozenset((s, o))].add(typ)
        for pair, typs in per_pair.items():
            pair_types[pair].append(frozenset(typs))
    multi_pairs = {p: ts for p, ts in pair_types.items() if len(ts) >= 2}
    rel_type_consistent = [p for p, ts in multi_pairs.items() if len(set(ts)) == 1]

    # Direction consistency: per (unordered pair, type), same direction in every run.
    directions: dict[tuple, list[tuple]] = defaultdict(list)
    for t in triples:
        seen: dict[tuple, set] = defaultdict(set)
        for s, typ, o in t:
            seen[(frozenset((s, o)), typ)].add((s, o))
        for key, dirs in seen.items():
            directions[key].append(tuple(sorted(dirs)))
    multi_dir = {k: v for k, v in directions.items() if len(v) >= 2 and len(k[0]) == 2}
    dir_consistent = [k for k, v in multi_dir.items() if len(set(v)) == 1]

    triple_freq = Counter(x for t in triples for x in t)
    entity_freq = Counter(x for e in aliased_entities for x in e)

    def ratio(part, whole):
        return round(len(part) / len(whole), 4) if whole else 1.0

    return {
        "runs": n,
        "per_run": [{"run": i + 1, "entities": len(s["entities"]), "relationships": len(s["relationships"])}
                    for i, s in enumerate(snapshots)],
        "entity_jaccard_strict": pairwise(strict_entities),
        "entity_jaccard_alias_aware": pairwise(aliased_entities),
        "relationship_jaccard_triple": pairwise(triples),
        "relationship_jaccard_directed_pair": pairwise(directed),
        "relationship_jaccard_unordered_pair": pairwise(undirected),
        "entity_type_consistency": {"entities_in_2plus_runs": len(multi),
                                    "fully_consistent": ratio(type_consistent, multi),
                                    "mean_modal_share": round(statistics.mean(modal_share), 4) if modal_share else 1.0},
        "relationship_type_consistency": {"pairs_in_2plus_runs": len(multi_pairs),
                                          "fully_consistent": ratio(rel_type_consistent, multi_pairs)},
        "direction_consistency": {"typed_pairs_in_2plus_runs": len(multi_dir),
                                  "fully_consistent": ratio(dir_consistent, multi_dir)},
        "entities_in_all_runs": sum(1 for c in entity_freq.values() if c == n),
        "entities_total_distinct": len(entity_freq),
        "relationships_in_all_runs": sum(1 for c in triple_freq.values() if c == n),
        "relationships_total_distinct": len(triple_freq),
        "relationship_frequency_histogram": {k: v for k, v in sorted(Counter(triple_freq.values()).items())},
        "unstable_relationships": [
            {"triple": f"{s} -[{t}]-> {o}", "runs": c}
            for (s, t, o), c in sorted(triple_freq.items(), key=lambda kv: (kv[1], kv[0]))
            if c < n
        ],
        "stable_relationships": sorted(f"{s} -[{t}]-> {o}" for (s, t, o), c in triple_freq.items() if c == n),
        "type_variants": {e: dict(Counter(ts)) for e, ts in sorted(multi.items()) if len(set(ts)) > 1},
    }


def render_markdown(report: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = ["# Consistency report", ""]
    lines += [f"- {k}: `{v}`" for k, v in meta.items()]
    lines += ["", "## Per run", ""]
    lines += [f"Run {r['run']}: {r['entities']} entities, {r['relationships']} edges" for r in report["per_run"]]
    lines += ["", "## Stability (pairwise Jaccard over all run pairs)", "",
              "| metric | mean | min | max |", "|---|---|---|---|"]
    for key in ("entity_jaccard_strict", "entity_jaccard_alias_aware", "relationship_jaccard_triple",
                "relationship_jaccard_directed_pair", "relationship_jaccard_unordered_pair"):
        v = report[key]
        lines.append(f"| {key} | {v['mean']} | {v['min']} | {v['max']} |")
    lines += ["", "## Consistency", "",
              f"- Entity type consistency: {report['entity_type_consistency']}",
              f"- Relationship type consistency: {report['relationship_type_consistency']}",
              f"- Direction (source/target) consistency: {report['direction_consistency']}",
              f"- Entities in all runs: {report['entities_in_all_runs']} / {report['entities_total_distinct']} distinct",
              f"- Relationships in all runs: {report['relationships_in_all_runs']} / {report['relationships_total_distinct']} distinct",
              f"- Relationship frequency histogram (runs present -> #edges): {report['relationship_frequency_histogram']}",
              "", "## Entity type variants", ""]
    lines += [f"- {e}: {v}" for e, v in report["type_variants"].items()] or ["(none)"]
    lines += ["", "## Relationships present in every run", ""]
    lines += [f"- {t}" for t in report["stable_relationships"]] or ["(none)"]
    lines += ["", "## Unstable relationships (present in fewer than all runs)", ""]
    lines += [f"- {u['runs']}/{report['runs']}  {u['triple']}" for u in report["unstable_relationships"]] or ["(none)"]
    return "\n".join(lines) + "\n"


def raw_snapshot(run: dict) -> dict:
    """The graph a run would have produced with resolution and validation
    switched off: entities keyed only by normalized name (Cognee-style
    identity, no aliases), every extracted edge kept (types snake_cased)."""
    from brain_v0.normalize import normalize_relationship_type

    types: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, str] = {}
    relationships = set()
    for chunk in run["raw_extractions"]:
        by_local = {}
        for e in chunk["output"]["entities"]:
            key = normalize_name(e["name"])
            names.setdefault(key, e["name"])
            types[key][e["entity_type"]] += 1
            by_local[e["local_id"]] = names[key]
        for r in chunk["output"]["relationships"]:
            if r["source_local_id"] in by_local and r["target_local_id"] in by_local:
                relationships.add((by_local[r["source_local_id"]], normalize_relationship_type(r["relationship_type"]),
                                   by_local[r["target_local_id"]]))
    return {
        "entities": [{"name": names[k], "type": c.most_common(1)[0][0], "aliases": []} for k, c in types.items()],
        "relationships": [{"source": s, "type": t, "target": o, "support": 1} for s, t, o in sorted(relationships)],
    }
