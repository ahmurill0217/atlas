"""Probe GLiNER2.5 (JointIE, typed schema) on real email bodies.

Exploration only: nothing is written to the graph. For a seeded random sample of
emails it runs the model on the message's own text (the quoted reply chain is
dropped by the email adapter) and records entities and relations with spans and
confidences, plus timings. Relations are labelled with the ontology relation they
would be proposed as.

    uv run --extra models python scripts/gliner_probe.py runs/enron/json_5k runs/enron/gliner \
        --model fastino/gliner2.5-base-v1 --emails 150
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import typer

from atlas.extraction.structured import StructuredExtractor
from atlas.ingestion.adapters import normalize_file
from atlas.ingestion.segmentation import split_long
from atlas.ontology import load_ontology

app = typer.Typer(add_completion=False)

ENTITIES = {
    "person": "A named individual human being",
    "organization": "A named company, agency, utility, bank or other organization",
    "project": "A named internal project, deal, initiative or program",
    "product": "A named product, service or software system",
}
# model relation -> (head type, tail type, ontology relation)
RELATIONS = {
    "works_for": ("person", "organization", "WORKS_AT"),
    "manages": ("person", "person", "MANAGES"),
    "reports_to": ("person", "person", "REPORTS_TO"),
    "works_on": ("person", "project", "WORKS_ON"),
    "customer_of": ("organization", "organization", "CUSTOMER_OF"),
    "uses_product": ("organization", "product", "USES_PRODUCT"),
}
CHUNK_CHARS = 1200


def sample_emails(corpus: Path, n: int, seed: int) -> list:
    extractor = StructuredExtractor(load_ontology("ontology/v1_3"))
    files = sorted(corpus.rglob("*.json"))
    random.Random(seed).shuffle(files)
    picked = []
    for f in files:
        doc = normalize_file(f)
        body = next((s.text for s in doc.sections if not s.metadata.get("quoted")), "")
        sender = doc.author.email if doc.author else None
        if 200 <= len(body) <= 4000 and sender and not extractor.is_bulk_sender(sender):
            picked.append((f, doc, body))
        if len(picked) == n:
            break
    return picked


@app.command()
def main(corpus: Path, out_dir: Path, model: str = "fastino/gliner2.5-base-v1", emails: int = 150,
         seed: int = 7, beam: int = 32):
    import torch
    from gliner2.joint_ie import JointIE, JointIEConfig

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    t0 = time.perf_counter()
    joint = JointIE.from_pretrained(model, map_location=device)
    load_s = time.perf_counter() - t0
    schema = joint.create_schema().entities(ENTITIES)
    for name, (head, tail, _) in RELATIONS.items():
        schema = schema.relation(name, head, tail)
    schema = schema.no_self_loops()
    config = JointIEConfig(optimizer="beam", beam_size=beam)

    sample = sample_emails(corpus, emails, seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{model.split('/')[-1]}.jsonl"
    chars, infer_s, n_rel, n_ent, infeasible = 0, 0.0, 0, 0, 0
    joint.extract("warm up", schema, config=config)
    with out.open("w") as fh:
        for path, doc, body in sample:
            for chunk in split_long(body, CHUNK_CHARS):
                t = time.perf_counter()
                result = joint.extract(chunk, schema, config=config)
                dt = time.perf_counter() - t
                infer_s, chars = infer_s + dt, chars + len(chunk)
                d = result.to_dict()
                infeasible += not result.feasible
                ents = {e["id"]: e for e in d["entities"]}
                rels = [{"relation": r["type"], "ontology": RELATIONS[r["type"]][2], "confidence": r["confidence"],
                         "head": ents[r["head"]]["text"], "tail": ents[r["tail"]]["text"],
                         "head_span": [ents[r["head"]]["start"], ents[r["head"]]["end"]],
                         "tail_span": [ents[r["tail"]]["start"], ents[r["tail"]]["end"]]} for r in d["relations"]]
                n_rel, n_ent = n_rel + len(rels), n_ent + len(d["entities"])
                fh.write(json.dumps({"file": str(path), "subject": doc.title, "sender": doc.author.email,
                                     "text": chunk, "entities": d["entities"], "relations": rels,
                                     "feasible": result.feasible, "seconds": round(dt, 3)}) + "\n")
    summary = {"model": model, "device": device, "emails": len(sample), "chars": chars,
               "load_seconds": round(load_s, 1), "inference_seconds": round(infer_s, 1),
               "ms_per_email": round(1000 * infer_s / max(len(sample), 1)), "entities": n_ent,
               "relations": n_rel, "infeasible_chunks": infeasible}
    (out_dir / f"{model.split('/')[-1]}.summary.json").write_text(json.dumps(summary, indent=2))
    typer.echo(json.dumps(summary, indent=2))


if __name__ == "__main__":
    app()
