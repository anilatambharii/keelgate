"""Embeddings for semantic memory."""

from __future__ import annotations

import hashlib
import math
import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

_WORD = re.compile(r"\w+")


@runtime_checkable
class Embedder(Protocol):
    """Turns text into a vector of a fixed size (``dim``) for semantic search."""

    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashEmbedder:
    """A deterministic bag-of-words embedder with no model and no network.

    It hashes each word into one of ``dim`` buckets (with a hash-derived sign, which keeps
    unrelated words from piling up on one side) and L2-normalises. Texts that share words
    land close together, which is enough to test retrieval, `as_of` filtering and tenant
    isolation exactly. It captures **no** meaning beyond word overlap: use a real embedding
    model, behind the same protocol, for production retrieval quality.
    """

    def __init__(self, dim: int = 64) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            vector[bucket] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 when either vector is all zeros."""
    if len(a) != len(b):
        raise ValueError("vectors must have the same dimension")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
