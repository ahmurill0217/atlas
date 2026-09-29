"""Interactive HTML view of the canonical graph (vis-network, one self-contained file).

Two views:
  - neighborhood(entity): the stored facts around one entity, expanded breadth-first
    with a per-node fan-out cap (most recently seen edges first).
  - people_network(): a *derived* Person-to-Person view (who emails whom, weighted by
    message count). Derived edges are labelled as such; they are not stored facts.
"""

from __future__ import annotations

import html
import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

COLORS = {"Person": "#4f7cff", "Organization": "#f59e0b", "Document": "#9ca3af", "Meeting": "#10b981",
          "ActionItem": "#ef4444", "Team": "#8b5cf6", "Project": "#ec4899"}

_EDGES_AROUND = text("""
    SELECT g.id, g.source_entity_id, g.relation_type, g.target_entity_id, g.provenance_class, g.confidence,
           g.ontology_version, g.first_seen_at, g.last_seen_at, g.properties,
           (SELECT count(*) FROM kg.edge_evidence v WHERE v.edge_id = g.id) AS evidence
    FROM kg.edges g
    WHERE g.status = 'active' AND (g.source_entity_id = :id OR g.target_entity_id = :id)
      AND (CAST(:relations AS text[]) IS NULL OR g.relation_type = ANY(CAST(:relations AS text[])))
    ORDER BY g.last_seen_at DESC NULLS LAST
    LIMIT :cap""")

_ENTITIES = text("""SELECT id, entity_type, canonical_name, properties, identity_strength
                    FROM kg.entities WHERE id = ANY(:ids)""")


def _node(row) -> dict:
    return {"id": str(row.id), "label": row.canonical_name[:40], "group": row.entity_type,
            "color": COLORS.get(row.entity_type, "#6b7280"),
            "details": {"type": row.entity_type, "name": row.canonical_name,
                        "identity": row.identity_strength, **(row.properties or {})}}


def _edge(row) -> dict:
    return {"id": str(row.id), "from": str(row.source_entity_id), "to": str(row.target_entity_id),
            "label": row.relation_type, "arrows": "to",
            "details": {"relation": row.relation_type, "provenance": row.provenance_class,
                        "confidence": row.confidence, "ontology": row.ontology_version,
                        "evidence rows": row.evidence, "first seen": row.first_seen_at,
                        "last seen": row.last_seen_at, **(row.properties or {}),
                        "explain": f"python -m atlas why {row.id}"}}


def neighborhood(s: Session, center: uuid.UUID, depth: int = 2, fanout: int = 25) -> dict:
    """Stored facts within `depth` hops of `center`; each node expands at most `fanout` edges.
    Every Person reached also shows its WORKS_AT edges."""
    edges: dict[str, dict] = {}
    seen, frontier = {center}, [center]
    for _ in range(depth):
        nxt = []
        for node in frontier:
            for row in s.execute(_EDGES_AROUND, {"id": node, "relations": None, "cap": fanout}):
                edges[str(row.id)] = _edge(row)
                for other in (row.source_entity_id, row.target_entity_id):
                    if other not in seen:
                        seen.add(other)
                        nxt.append(other)
        frontier = nxt
    nodes = {r.id: r for r in s.execute(_ENTITIES, {"ids": list(seen)})}
    for pid in [i for i, r in nodes.items() if r.entity_type == "Person"]:
        for row in s.execute(_EDGES_AROUND, {"id": pid, "relations": ["WORKS_AT"], "cap": 5}):
            edges[str(row.id)] = _edge(row)
            seen.add(row.target_entity_id)
    nodes = {r.id: r for r in s.execute(_ENTITIES, {"ids": list(seen)})}
    return {"nodes": [_node(r) | ({"size": 30} if r.id == center else {}) for r in nodes.values()],
            "edges": list(edges.values()), "center": str(center)}


def people_network(s: Session, top: int = 60) -> dict:
    """Derived view: Person -> Person weighted by messages sent (AUTHORED_BY + SENT_TO on the same email)."""
    pairs = s.execute(text("""
        WITH sent AS (
            SELECT a.target_entity_id AS sender, r.target_entity_id AS recipient, count(*) AS n
            FROM kg.edges a JOIN kg.edges r ON r.source_entity_id = a.source_entity_id
            WHERE a.relation_type = 'AUTHORED_BY' AND r.relation_type = 'SENT_TO'
              AND a.target_entity_id <> r.target_entity_id
            GROUP BY 1, 2),
        top_people AS (
            SELECT p FROM (SELECT sender AS p, n FROM sent UNION ALL SELECT recipient, n FROM sent) x
            GROUP BY p ORDER BY sum(n) DESC LIMIT :top)
        SELECT sender, recipient, n FROM sent
        WHERE sender IN (SELECT p FROM top_people) AND recipient IN (SELECT p FROM top_people) AND n >= 2"""),
        {"top": top}).all()
    ids = {p for a, b, _ in pairs for p in (a, b)}
    works = s.execute(text("""SELECT g.id, g.source_entity_id, g.relation_type, g.target_entity_id,
            g.provenance_class, g.confidence, g.ontology_version, g.first_seen_at, g.last_seen_at, g.properties,
            (SELECT count(*) FROM kg.edge_evidence v WHERE v.edge_id = g.id) AS evidence
            FROM kg.edges g WHERE g.relation_type = 'WORKS_AT' AND g.source_entity_id = ANY(:ids)"""),
                      {"ids": list(ids)}).all()
    ids |= {w.target_entity_id for w in works}
    nodes = [_node(r) for r in s.execute(_ENTITIES, {"ids": list(ids)})]
    edges = [{"id": f"{a}>{b}", "from": str(a), "to": str(b), "arrows": "to", "value": n, "title": f"{n} emails",
              "dashes": True, "details": {"relation": "emailed (derived, not a stored fact)", "messages": n}}
             for a, b, n in pairs] + [_edge(w) for w in works]
    return {"nodes": nodes, "edges": edges, "center": None}


def render_html(graph: dict, title: str) -> str:
    data = json.dumps(graph, default=str)
    legend = "".join(f'<span><i style="background:{c}"></i>{t}</span>' for t, c in COLORS.items())
    return _TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__LEGEND__", legend).replace("__DATA__", data)


_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  :root { --bg:#ffffff; --fg:#111827; --muted:#6b7280; --panel:#f9fafb; --line:#e5e7eb; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0b0f17; --fg:#e5e7eb; --muted:#9ca3af; --panel:#111827; --line:#1f2937; } }
  * { box-sizing:border-box } body { margin:0; font:14px/1.4 system-ui,sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; flex-wrap:wrap }
  header h1 { font-size:15px; margin:0 } .legend span { margin-right:10px; color:var(--muted); font-size:12px }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:4px }
  main { display:flex; height:calc(100vh - 46px) } #net { flex:1; min-width:0 }
  aside { width:340px; border-left:1px solid var(--line); background:var(--panel); padding:14px 16px; overflow:auto }
  aside h2 { font-size:14px; margin:0 0 8px } table { width:100%; border-collapse:collapse; font-size:12px }
  td { border-top:1px solid var(--line); padding:4px 4px; vertical-align:top; word-break:break-word } td:first-child { color:var(--muted); width:38% }
  input { width:100%; padding:6px 8px; margin-bottom:10px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg) }
  @media (max-width: 700px) { main { flex-direction:column } aside { width:auto; height:40vh; border-left:0; border-top:1px solid var(--line) } }
</style></head><body>
<header><h1>__TITLE__</h1><div class="legend">__LEGEND__</div></header>
<main><div id="net"></div><aside><input id="q" placeholder="Find a node by name…">
<h2 id="h">Click a node or edge</h2><table id="t"></table></aside></main>
<script>
const G = __DATA__;
const nodes = new vis.DataSet(G.nodes.map(n => ({...n, shape: "dot", size: n.size || (n.group === "Document" ? 8 : 14),
  font: {size: 12, color: getComputedStyle(document.body).color}})));
const edges = new vis.DataSet(G.edges.map(e => ({...e, font: {size: 9, align: "middle", strokeWidth: 0,
  color: "#9ca3af"}, color: {color: "#9ca3af", opacity: 0.6}, smooth: false})));
const net = new vis.Network(document.getElementById("net"), {nodes, edges}, {
  physics: {solver: "forceAtlas2Based", stabilization: {iterations: 250}}, interaction: {hover: true}});
function show(title, obj) {
  document.getElementById("h").textContent = title;
  document.getElementById("t").innerHTML = Object.entries(obj).filter(([, v]) => v !== null && v !== "")
    .map(([k, v]) => `<tr><td>${k}</td><td>${String(typeof v === "object" ? JSON.stringify(v) : v)
      .replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]))}</td></tr>`).join("");
}
net.on("click", p => {
  if (p.nodes.length) { const n = nodes.get(p.nodes[0]); show(n.details.name, n.details); }
  else if (p.edges.length) { const e = edges.get(p.edges[0]); show(e.details.relation, e.details); }
});
document.getElementById("q").addEventListener("change", ev => {
  const q = ev.target.value.toLowerCase(); const hit = G.nodes.find(n => n.details.name.toLowerCase().includes(q));
  if (hit) { net.selectNodes([hit.id]); net.focus(hit.id, {scale: 1.4, animation: true}); show(hit.details.name, hit.details); }
});
if (G.center) net.once("stabilized", () => net.focus(G.center, {scale: 1, animation: true}));
</script></body></html>
"""
