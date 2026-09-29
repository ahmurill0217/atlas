"""Embedding providers behind a one-method interface."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from brain.config import Settings, get_settings


class Embedder(Protocol):
    name: str  # recorded per chunk; a different name triggers re-embedding
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, model: str, dim: int, api_key: str | None = None, batch_size: int = 128):
        from openai import OpenAI

        self.model, self.dim, self.batch_size = model, dim, batch_size
        self.name = f"openai:{model}:{dim}"
        self._client = OpenAI(api_key=api_key, max_retries=8)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            resp = self._client.embeddings.create(
                model=self.model, input=texts[i : i + self.batch_size], dimensions=self.dim
            )
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out


class HashEmbedder:
    """Deterministic, offline bag-of-words embedder (feature hashing of word
    unigrams and bigrams). Not semantic, but lexical overlap gives usable
    similarity for tests and for trying the pipeline without an API key."""

    def __init__(self, dim: int):
        self.dim = dim
        self.name = f"hash:{dim}"

    def _vector(self, text: str) -> list[float]:
        words = re.findall(r"[a-z0-9]+", text.lower())
        features = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        vec = [0.0] * self.dim
        for feature in features:
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


def get_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.embedding_provider == "hash":
        return HashEmbedder(settings.embedding_dim)
    if settings.embedding_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set; use EMBEDDING_PROVIDER=hash for offline use")
        return OpenAIEmbedder(settings.embedding_model, settings.embedding_dim, settings.openai_api_key)
    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {settings.embedding_provider!r}")
