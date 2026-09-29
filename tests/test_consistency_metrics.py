from brain.evaluation.consistency import consistency_report, jaccard


def snap(entities, rels):
    return {"entities": [{"name": n, "type": t, "aliases": list(a)} for n, t, a in entities],
            "relationships": [{"source": s, "type": t, "target": o, "support": 1} for s, t, o in rels]}


def test_identical_runs_are_perfectly_stable():
    s = snap([("Sotorasib", "Drug", ["AMG 510"]), ("KRAS G12C", "Mutation", [])],
             [("Sotorasib", "targets", "KRAS G12C")])
    r = consistency_report([s, s, s])
    assert r["entity_jaccard_strict"]["mean"] == 1.0 and r["relationship_jaccard_triple"]["mean"] == 1.0
    assert r["relationships_in_all_runs"] == 1 and r["unstable_relationships"] == []


def test_alias_aware_matching_and_type_direction_metrics():
    a = snap([("Sotorasib", "Drug", ["AMG 510"]), ("KRAS G12C", "Mutation", [])],
             [("Sotorasib", "targets", "KRAS G12C")])
    # Different canonical name for the same drug, different type, flipped edge.
    b = snap([("AMG 510", "Compound", ["sotorasib"]), ("KRAS G12C", "Mutation", [])],
             [("KRAS G12C", "targets", "AMG 510")])
    r = consistency_report([a, b])
    assert r["entity_jaccard_strict"]["mean"] == 0.3333
    assert r["entity_jaccard_alias_aware"]["mean"] == 1.0
    assert r["relationship_jaccard_unordered_pair"]["mean"] == 1.0
    assert r["relationship_jaccard_directed_pair"]["mean"] == 0.0
    assert r["direction_consistency"]["fully_consistent"] == 0.0
    assert r["relationship_type_consistency"]["fully_consistent"] == 1.0
    assert r["entity_type_consistency"]["fully_consistent"] == 0.5
    assert jaccard(set(), set()) == 1.0


def test_raw_snapshot_keys_entities_by_name_and_keeps_all_edges():
    from brain.evaluation.consistency import raw_snapshot

    run = {"raw_extractions": [
        {"output": {"entities": [{"local_id": "a", "name": "Sotorasib", "entity_type": "Drug", "aliases": ["AMG 510"]},
                                 {"local_id": "b", "name": "KRAS G12C", "entity_type": "Mutation", "aliases": []}],
                    "relationships": [{"source_local_id": "a", "target_local_id": "b", "relationship_type": "Targets"}]}},
        {"output": {"entities": [{"local_id": "x", "name": "AMG 510", "entity_type": "Drug", "aliases": []},
                                 {"local_id": "y", "name": "kras g12c", "entity_type": "Mutation", "aliases": []}],
                    "relationships": [{"source_local_id": "x", "target_local_id": "y", "relationship_type": "inhibits"}]}},
    ]}
    snap = raw_snapshot(run)
    assert sorted(e["name"] for e in snap["entities"]) == ["AMG 510", "KRAS G12C", "Sotorasib"]
    assert {(r["source"], r["type"], r["target"]) for r in snap["relationships"]} == {
        ("Sotorasib", "targets", "KRAS G12C"), ("AMG 510", "inhibits", "KRAS G12C")}
