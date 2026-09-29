"""`python -m atlas <command>` — ingest, inspect, and explain the canonical graph."""

from __future__ import annotations

import json
import uuid

import typer
from sqlalchemy import text

from atlas.config import get_settings
from atlas.db.session import get_engine, session_scope
from atlas.graph.queries import GraphQueries
from atlas.ontology.loader import OntologyError, load_ontology

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Atlas V1: deterministic business knowledge graph.")

KG_TABLES = ["audit_log", "review_items", "candidate_edges", "candidate_entities", "edge_evidence", "edges",
             "entity_merge_history", "entity_external_ids", "entity_aliases", "entities", "document_processing",
             "document_sections", "document_versions", "documents", "ingestion_runs", "ontology_versions"]


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


@app.command()
def ontology(details: bool = typer.Option(False, "--details", help="List every type and relation.")) -> None:
    """Load and consistency-check the ontology."""
    try:
        o = load_ontology(str(get_settings().ontology_dir))
    except OntologyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Ontology {o.version}  checksum {o.checksum[:12]}  "
               f"{len(o.entity_types)} entity types, {len(o.roles)} roles, {len(o.relations)} relations: OK")
    if details:
        for t in o.entity_types.values():
            parent = f" (is a {t.parent})" if t.parent else ""
            typer.echo(f"  {t.module:9s} {t.name}{parent}  identity={list(o.identity_fields(t.name))}")
        for r in o.relations.values():
            typer.echo(f"  {r.module:9s} {r.name}: {list(r.source)} -> {list(r.target)}")


@app.command()
def ingest(path: str, traces: bool = typer.Option(False, "--traces", help="Print per-document stage output.")) -> None:
    """Run the pipeline over a file or directory (Phase 1: structured fields only, no LLM)."""
    from atlas.pipeline import KnowledgeIngestionPipeline

    report = KnowledgeIngestionPipeline().ingest(path, keep_traces=traces)
    typer.echo(f"run {report.run_id}")
    for key, value in sorted(report.stats.items()):
        typer.echo(f"  {key:34s} {value:g}")
    for t in report.traces:
        typer.echo(f"\n{t.status.upper():9s} {t.path}" + (f"  {t.error}" if t.error else ""))
        if traces and t.report:
            typer.echo(_dump({"entities": t.report.entities, "edges": t.report.edges}))


@app.command()
def entities(entity_type: str = typer.Option(None, "--type")) -> None:
    """List canonical entities with identifiers and roles."""
    with session_scope() as s:
        rows = GraphQueries(s).list_entities(entity_type)
    for e in rows:
        ids = ", ".join(f"{k}={v}" for k, v in sorted(e["identifiers"].items()))
        roles = f"  roles={e['roles']}" if e["roles"] else ""
        typer.echo(f"{e['entity_type']:13s} {e['canonical_name']:32s} [{ids or 'name only'}]{roles}  {e['id']}")
    typer.echo(f"\n{len(rows)} entities")


@app.command()
def edges(relation: str = typer.Option(None, "--relation"), source_type: str = typer.Option(None, "--from"),
          target_type: str = typer.Option(None, "--to")) -> None:
    """List canonical edges."""
    with session_scope() as s:
        rows = GraphQueries(s).get_edges(source_type, relation, target_type)
    for g in rows:
        typer.echo(f"{g['source_name']} -[{g['relation_type']}]-> {g['target_name']}   "
                   f"({g['provenance_class']}, conf={g['confidence']:.2f}, evidence={g['evidence_count']})  {g['id']}")
    typer.echo(f"\n{len(rows)} edges")


@app.command()
def why(edge_id: str) -> None:
    """Explain an edge: evidence, candidate proposals, mapping and decisions."""
    with session_scope() as s:
        result = GraphQueries(s).explain_edge(uuid.UUID(edge_id))
    if result is None:
        typer.echo("no such edge")
        raise typer.Exit(1)
    e = result["edge"]
    typer.echo(f"{e['source_name']} -[{e['relation_type']}]-> {e['target_name']}  ({e['provenance_class']}, "
               f"ontology {e['ontology_version']}, first seen {e['first_seen_at']}, last seen {e['last_seen_at']})")
    typer.echo("\nEvidence:")
    for v in result["evidence"]:
        where = v["evidence_text"] or f"field `{v['source_field']}`"
        typer.echo(f"  - {v['source_system']}:{v['source_external_id']} \"{v['title']}\" -> {where} "
                   f"[{v['extractor']} {v['extractor_version']}, conf {v['confidence']}]")
    typer.echo("\nDecisions:")
    for c in result["candidates"]:
        m = c["mapping"] or {}
        typer.echo(f"  - {c['source_system']}:{c['source_external_id']}: proposed {c['payload']['suggested_relation']} "
                   f"-> {m.get('canonical')} via {m.get('method')} -> {c['decision']} ({c['reason']})")
    typer.echo("\nAudit:")
    for a in result["audit"]:
        typer.echo(f"  - {a['at']} {a['actor']} {a['action']}")


@app.command()
def entity(name: str) -> None:
    """Show an entity (by name or email) with its edges."""
    with session_scope() as s:
        q = GraphQueries(s)
        found = q.find_entity(identifier=("email", name)) if "@" in name else q.find_entity(name=name)
        if not found:
            typer.echo("not found")
            raise typer.Exit(1)
        for e in found:
            typer.echo(f"{e['entity_type']} {e['canonical_name']}  {e['id']}")
            typer.echo(f"  identifiers: {e['identifiers']}  aliases: {e['aliases']}  roles: {e['roles']}")
            for g in q.get_outgoing_edges(e["id"]):
                typer.echo(f"  -[{g['relation_type']}]-> {g['target_name']} ({g['target_type']})  evidence={g['evidence_count']}")
            for g in q.get_incoming_edges(e["id"]):
                typer.echo(f"  <-[{g['relation_type']}]- {g['source_name']} ({g['source_type']})  evidence={g['evidence_count']}")


@app.command()
def view(name: str = typer.Argument(None, help="Entity name or email to center on; omit for the people network."),
         out: str = typer.Option("graph.html", "--out"),
         depth: int = typer.Option(2, help="Hops from the entity."),
         fanout: int = typer.Option(25, help="Max edges expanded per node (most recent first)."),
         top: int = typer.Option(60, help="People in the network view.")) -> None:
    """Write an interactive HTML view of the graph."""
    from pathlib import Path

    from atlas.graph.view import neighborhood, people_network, render_html

    with session_scope() as s:
        if name:
            q = GraphQueries(s)
            found = q.find_entity(identifier=("email", name)) if "@" in name else q.find_entity(name=name)
            if not found:
                typer.echo("not found")
                raise typer.Exit(1)
            graph, title = neighborhood(s, found[0]["id"], depth, fanout), f"Atlas: {found[0]['canonical_name']}"
        else:
            graph, title = people_network(s, top), "Atlas: people network"
    Path(out).write_text(render_html(graph, title))
    typer.echo(f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges -> {out}")


@app.command()
def index(force: bool = typer.Option(False, "--force", help="Re-embed documents brain already has.")) -> None:
    """Index every document's text into brain (OpenSearch + local embeddings)."""
    from atlas.retrieval.brain_bridge import index_all

    settings = get_settings()
    with session_scope() as s:
        stats = index_all(s, settings, force=force,
                          progress=lambda st: typer.echo(f"  {st['documents']} documents ...", err=True)
                          if st["documents"] % 1024 == 0 else None)
    typer.echo(_dump(stats))


def run_ask(question: str, use_graph: bool = True, boost: bool = False) -> dict:
    """Answer one question; returns the answer, citations, scope and timings.
    use_graph adds the metadata profile; boost also ranks the entities' documents first."""
    import time

    from brain import AccessScope, AnswerOptions

    from atlas.retrieval.ask import GraphContext, graph_context, system_prompt
    from atlas.retrieval.brain_bridge import build_brain

    settings = get_settings()
    t0 = time.perf_counter()
    with session_scope() as s:
        ctx = graph_context(s, question) if use_graph else GraphContext()
    if not boost:
        ctx.document_ids = []          # profile only: search is left untouched
    graph_ms = (time.perf_counter() - t0) * 1000
    brain = build_brain(settings, boost_document_ids=ctx.document_ids)
    options = AnswerOptions(force_search=True, system_prompt=system_prompt(ctx) if use_graph else None)
    answer, cited, error, usage = [], [], None, None
    for event in brain.answer(question, access=AccessScope(bypass=True), options=options):
        kind = getattr(event, "type", "")
        if kind == "answer_delta":
            answer.append(event.text)
        elif kind == "answer_done":
            cited, usage = event.cited_documents, event.usage
        elif kind == "answer_error":
            error = event.message
    return {"question": question, "mode": ("graph" if boost else "facts") if use_graph else "baseline",
            "entities": [{"name": e.name, "type": e.entity_type, "matched": e.matched} for e in ctx.entities],
            "scope": ctx.scope, "scope_documents": len(ctx.document_ids), "facts": ctx.facts,
            "answer": "".join(answer), "error": error,
            "citations": [{"document_id": d.document_id, "title": d.semantic_identifier,
                           "date": d.updated_at.isoformat() if d.updated_at else None} for d in cited],
            "graph_ms": round(graph_ms), "seconds": round(time.perf_counter() - t0, 1),
            "tokens": usage.model_dump() if usage else None}


@app.command()
def ask(question: str,
        no_graph: bool = typer.Option(False, "--no-graph", help="Plain retrieval: no graph profile."),
        boost: bool = typer.Option(False, "--boost", help="Also rank the named entities' documents first."),
        as_json: bool = typer.Option(False, "--json")) -> None:
    """Answer a question: brain retrieves and cites; the graph adds a metadata profile of the people named."""
    result = run_ask(question, use_graph=not no_graph, boost=boost)
    if as_json:
        typer.echo(_dump(result))
        return
    if result["entities"]:
        typer.echo("Entities: " + ", ".join(f"{e['name']} ({e['type']})" for e in result["entities"]))
    if result["scope_documents"]:
        typer.echo(f"Boosted: {result['scope_documents']} documents ({result['scope']})")
    typer.echo()
    typer.echo(result["error"] or result["answer"])
    typer.echo("\nSources:")
    for i, c in enumerate(result["citations"], 1):
        typer.echo(f"  [{i}] {c['title']} ({(c['date'] or '')[:10]})  {c['document_id']}")


@app.command()
def reviews(status: str = typer.Option("OPEN"), review_type: str = typer.Option(None, "--type")) -> None:
    """List review items (most frequent first)."""
    with session_scope() as s:
        rows = GraphQueries(s).list_reviews(status, review_type)
    for r in rows:
        typer.echo(f"{r['review_type']:24s} x{r['frequency']:<3d} {r['reason']}  {r['id']}")
    typer.echo(f"\n{len(rows)} review items")


@app.command()
def resolve(review_id: str, status: str = typer.Option(..., help="APPROVED | REJECTED | DEFERRED"),
            reviewer: str = typer.Option(..., help="Who made the decision."),
            note: str = typer.Option(..., help="Why.")) -> None:
    """Record a governance decision on a review item (audited)."""
    from atlas.review.service import resolve_review

    with session_scope() as s:
        item = resolve_review(s, uuid.UUID(review_id), status.upper(), reviewer, note)
        typer.echo(f"{item.review_type} -> {item.status} by {reviewer}: {note}")


@app.command()
def stats() -> None:
    """Graph counts, including unsupported edges (must be 0)."""
    with session_scope() as s:
        for k, v in GraphQueries(s).stats().items():
            typer.echo(f"{k:20s} {v}")


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete all V1 graph data (schema kg)."""
    if not yes:
        typer.confirm(f"Delete all kg.* data in {get_settings().database_url}?", abort=True)
    with get_engine().begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(f"kg.{t}" for t in KG_TABLES) + " CASCADE"))
    typer.echo("Deleted.")
