"""Command line interface: `python -m brain <command>`."""

from __future__ import annotations

import json
import logging
import textwrap

import typer
from brain.config import get_settings
from brain.db import get_engine, session_scope
from brain.db.admin import GRAPH_TABLES, SOURCE_TABLES, truncate
from brain.graph.models import RelationshipView
from brain.graph.traversal import GraphQueries

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Knowledge brain on Postgres + pgvector.")



def _print_relationship(rel: RelationshipView, evidence: bool = True, indent: str = "  ") -> None:
    conf = f"{rel.confidence:.2f}" if rel.confidence is not None else "-"
    typer.echo(f"{indent}{rel.triple()}   (support={rel.support_count}, confidence={conf})")
    if evidence:
        for ev in rel.evidence:
            quote = textwrap.shorten(ev.evidence_text, 160)
            typer.echo(f"{indent}    ↳ \"{quote}\"  [{ev.source_uri} #chunk{ev.chunk_index}]")


def _or_exit(factory):
    """Build a provider, turning configuration errors into a clean CLI message."""
    try:
        return factory()
    except RuntimeError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def ingest(path: str = typer.Argument(..., help="File or directory of .txt/.md files")) -> None:
    """Parse and chunk documents into Postgres (no LLM calls)."""
    from brain.pipeline.ingest import ingest_path

    with session_scope() as session:
        stats = ingest_path(session, path)
    typer.echo(f"added={len(stats.added)} updated={len(stats.updated)} unchanged={len(stats.unchanged)} "
               f"failed={len(stats.failed)} chunks_written={stats.chunks_written}")
    for failure in stats.failed:
        typer.echo(f"  FAILED {failure}", err=True)


@app.command()
def build(
    force: bool = typer.Option(False, help="Re-extract chunks even if a cached extraction exists."),
    replay: bool = typer.Option(False, help="Re-apply all chunks from cached LLM output (no new LLM calls "
                                "for cached chunks). Use after `reset --keep-extractions`."),
    label: str = typer.Option(None, help="Label stored on the extraction run."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Embed chunks, extract the knowledge graph, resolve and persist it."""
    from brain.embeddings import get_embedder
    from brain.llm import get_llm
    from brain.pipeline.build_brain import build as run_build

    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    report = run_build(_or_exit(get_llm), _or_exit(get_embedder), force=force, replay=replay, label=label)
    typer.echo(f"run {report.run_id}")
    for key, value in sorted(report.stats.items()):
        typer.echo(f"  {key:45s} {value}")
    for err in report.errors:
        typer.echo(f"  ERROR {err}", err=True)


@app.command()
def entities(
    entity_type: str = typer.Option(None, "--type", help="Filter by entity type."),
    ambiguous: bool = typer.Option(False, help="Only entities flagged AMBIGUOUS by resolution."),
) -> None:
    """List canonical entities with aliases."""
    with session_scope() as session:
        rows = GraphQueries(session).list_entities(entity_type, ambiguous)
    for e in rows:
        aliases = f"  aka {', '.join(e.aliases)}" if e.aliases else ""
        flag = "  [AMBIGUOUS]" if e.properties.get("resolution_status") == "ambiguous" else ""
        typer.echo(f"{e.entity_type:18s} {e.canonical_name}{aliases}  (mentions={e.mention_count}){flag}")
    typer.echo(f"\n{len(rows)} entities")


@app.command()
def relationships(
    relationship_type: str = typer.Option(None, "--type", help="Filter by relationship type."),
    evidence: bool = typer.Option(False, "--evidence", "-e", help="Show supporting evidence."),
) -> None:
    """List relationships (edges)."""
    with session_scope() as session:
        q = GraphQueries(session)
        rels = q.list_relationships(relationship_type)
        if evidence:
            q.with_evidence(rels)
    for rel in rels:
        _print_relationship(rel, evidence, indent="")
    typer.echo(f"\n{len(rels)} relationships")


@app.command()
def search(
    query: str,
    top_k: int = typer.Option(5, help="Number of chunks to retrieve."),
    chunks_only: bool = typer.Option(False, help="Semantic chunk search only (no graph)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Hybrid search: vector chunks + 1-hop graph expansion, with provenance."""
    from dataclasses import asdict

    from brain.embeddings import get_embedder
    from brain.search import hybrid_search, search_chunks

    embedder = _or_exit(get_embedder)
    with session_scope() as session:
        if chunks_only:
            hits = search_chunks(session, embedder, query, top_k)
            for h in hits:
                names = ", ".join(e["name"] for e in h.entities)
                typer.echo(f"{h.score:.3f}  {h.source_uri} #chunk{h.chunk_index}\n       {textwrap.shorten(h.text, 200)}\n       entities: {names}")
            return
        result = hybrid_search(session, embedder, query, top_k)
    if as_json:
        typer.echo(json.dumps(asdict(result), default=str, indent=2))
        return
    typer.echo(f"Query: {query}\n")
    if result.query_entities:
        typer.echo("Entities named in query: " + ", ".join(e.canonical_name for e in result.query_entities))
    typer.echo("\nTop chunks:")
    for h in result.chunks:
        typer.echo(f"  {h.score:.3f}  {h.source_uri} #chunk{h.chunk_index}: {textwrap.shorten(h.text, 110)}")
    typer.echo("\nRelationships (with evidence):")
    for rel in result.relationships:
        _print_relationship(rel)
    typer.echo(f"\n{len(result.entities)} entities, {len(result.relationships)} relationships in context")


@app.command()
def ask(
    question: str,
    mode: str = typer.Option("hybrid", help="hybrid (chunks + graph facts) or vector (chunks only)."),
    top_k: int = typer.Option(3, help="Number of source chunks to retrieve."),
    show_context: bool = typer.Option(False, help="Print the full retrieved context."),
) -> None:
    """Answer a question from the knowledge base, with cited sources."""
    from brain.embeddings import get_embedder
    from brain.llm import get_llm
    from brain.qa import ask as run_ask

    embedder, llm = _or_exit(get_embedder), _or_exit(get_llm)
    with session_scope() as session:
        result = run_ask(session, embedder, llm, question, mode, top_k)
    typer.echo(result.answer)
    if not result.answerable:
        typer.echo("(not answerable from the documents)")
    typer.echo("\nSources:")
    for item in result.cited():
        typer.echo(f"  [{item.id}] {item.reference}: {textwrap.shorten(item.text, 160)}")
    if result.invalid_citations:
        typer.echo(f"  (ignored citations not in context: {', '.join(result.invalid_citations)})")
    if show_context:
        typer.echo("\nContext:")
        for item in result.context:
            typer.echo(f"  [{item.id}] {textwrap.shorten(item.text, 200)}")


@app.command()
def graph(name: str, depth: int = typer.Option(1, help="Hops to expand (1-3).")) -> None:
    """Show an entity, its aliases and its neighbourhood with evidence."""
    from brain.search import graph_lookup

    with session_scope() as session:
        hood = graph_lookup(session, name, depth)
    if hood is None:
        typer.echo(f"No entity matches {name!r}")
        raise typer.Exit(1)
    e = hood.entity
    typer.echo(f"{e.canonical_name}  [{e.entity_type}]  id={e.id}")
    if e.aliases:
        typer.echo(f"  aliases: {', '.join(e.aliases)}")
    if e.description:
        typer.echo(f"  {e.description}")
    typer.echo(f"  type votes: {e.properties.get('type_votes')}  mentions: {e.mention_count}")
    typer.echo(f"\nRelationships ({len(hood.relationships)}):")
    for rel in hood.relationships:
        _print_relationship(rel)
    if hood.other_matches:
        typer.echo("\nOther matches: " + ", ".join(m.canonical_name for m in hood.other_matches))


@app.command()
def why(source: str, target: str, relationship_type: str = typer.Option(None, "--type")) -> None:
    """Why does the system believe SOURCE relates to TARGET? Prints the evidence."""
    with session_scope() as session:
        rels = GraphQueries(session).explain(source, target, relationship_type)
    if not rels:
        typer.echo("No relationship found between those entities.")
        raise typer.Exit(1)
    for rel in rels:
        _print_relationship(rel)
        for ev in rel.evidence:
            typer.echo(f"      run={ev.extraction_run_id} chunk={ev.chunk_id}")


@app.command()
def path(source: str, target: str, max_depth: int = typer.Option(3)) -> None:
    """Shortest paths between two entities (undirected, up to MAX_DEPTH hops)."""
    with session_scope() as session:
        q = GraphQueries(session)
        s, t = q.resolve_name(source), q.resolve_name(target)
        if not s or not t:
            typer.echo("Entity not found.")
            raise typer.Exit(1)
        paths = q.find_path(s[0], t[0], max_depth)
    for p in paths:
        typer.echo(p.describe())
    if not paths:
        typer.echo(f"No path within {max_depth} hops.")


@app.command()
def stats() -> None:
    """Row counts."""
    with session_scope() as session:
        for key, value in GraphQueries(session).stats().items():
            typer.echo(f"{key:18s} {value}")


@app.command()
def reset(
    everything: bool = typer.Option(False, "--all", help="Also delete documents and chunks."),
    keep_extractions: bool = typer.Option(False, help="Keep cached LLM output so `build --replay` can reuse it."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete the graph (and optionally all ingested documents)."""
    target = "EVERYTHING" if everything else "the knowledge graph (documents/chunks are kept)"
    if not yes:
        typer.confirm(f"Delete {target} in {get_settings().database_url}?", abort=True)
    tables = GRAPH_TABLES + (SOURCE_TABLES if everything else [])
    if keep_extractions and not everything:
        tables = [t for t in tables if t not in ("chunk_extractions", "extraction_runs")]
    truncate(get_engine(), tables)
    typer.echo(f"Deleted {target}.")
