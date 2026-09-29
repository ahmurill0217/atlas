"""Question answering over the brain: retrieve context, then one LLM call.

Two retrieval modes, same answer step:
  hybrid - top chunks + graph facts (1-hop around entities in the chunks/query),
           each fact carrying its verbatim evidence quote
  vector - top chunks only (plain vector RAG baseline)

Every piece of context has an id ([S1] source chunk, [F1] graph fact); the
LLM must cite ids, and citations are checked against what was retrieved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from brain.embeddings.embedder import Embedder
from brain.llm.base import StructuredLLM
from brain.search import hybrid_search, search_chunks

SYSTEM_PROMPT = """\
You answer questions using ONLY the context provided.

The context has two kinds of items:
- [S#] source passages: original document text. These are authoritative.
- [F#] graph facts: relationships machine-extracted from the documents, each with
  the quote it was extracted from. Use them to connect information across
  documents, but they can be mislabeled or point the wrong way. If a fact
  conflicts with its quote or with a source passage, trust the text.

Rules:
- Use only the context. Do not add background knowledge.
- Cite the ids of the items that support each claim.
- If the context does not contain the answer, set `answerable` to false and say
  that the information is not in the provided documents.
- Be concise and complete: if the question asks for a list, give every item the
  context supports.
"""


class GeneratedAnswer(BaseModel):
    answer: str
    citations: list[str] = Field(description="Ids of context items that support the answer, e.g. ['S1', 'F3'].")
    answerable: bool = Field(description="False if the context does not contain the answer.")


@dataclass
class ContextItem:
    id: str
    kind: str  # "source" | "fact"
    text: str
    reference: str  # document / chunk it came from


@dataclass
class AskResult:
    question: str
    mode: str
    answer: str
    answerable: bool
    citations: list[str]
    invalid_citations: list[str]
    context: list[ContextItem] = field(default_factory=list)

    def cited(self) -> list[ContextItem]:
        by_id = {c.id: c for c in self.context}
        return [by_id[c] for c in self.citations if c in by_id]


def build_context(session: Session, embedder: Embedder, question: str, mode: str = "hybrid",
                  top_k: int = 3, max_facts: int = 25) -> list[ContextItem]:
    items: list[ContextItem] = []
    if mode == "vector":
        chunks, relationships = search_chunks(session, embedder, question, top_k), []
    elif mode == "hybrid":
        result = hybrid_search(session, embedder, question, top_k=top_k, max_relationships=max_facts)
        chunks, relationships = result.chunks, result.relationships
    else:
        raise ValueError(f"unknown mode {mode!r}")
    for i, chunk in enumerate(chunks, 1):
        ref = f"{chunk.source_uri} #chunk{chunk.chunk_index}"
        items.append(ContextItem(f"S{i}", "source", chunk.text, ref))
    for i, rel in enumerate(relationships, 1):
        ev = rel.evidence[0] if rel.evidence else None
        quote = f' | evidence: "{ev.evidence_text}"' if ev else ""
        ref = f"{ev.source_uri} #chunk{ev.chunk_index}" if ev else ""
        items.append(ContextItem(f"F{i}", "fact", f"{rel.triple()}{quote}", ref))
    return items


def format_context(items: list[ContextItem]) -> str:
    return "\n\n".join(f"[{c.id}] ({c.reference})\n{c.text}" if c.kind == "source" else f"[{c.id}] {c.text}"
                       for c in items)


def ask(session: Session, embedder: Embedder, llm: StructuredLLM, question: str,
        mode: str = "hybrid", top_k: int = 3) -> AskResult:
    context = build_context(session, embedder, question, mode, top_k)
    user = f"Context:\n{format_context(context)}\n\nQuestion: {question}"
    generated = llm.generate(SYSTEM_PROMPT, user, GeneratedAnswer)
    known = {c.id for c in context}
    citations = [c.strip("[] ") for c in generated.citations]
    return AskResult(question, mode, generated.answer, generated.answerable,
                     [c for c in citations if c in known], [c for c in citations if c not in known], context)
