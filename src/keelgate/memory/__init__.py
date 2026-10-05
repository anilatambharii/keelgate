"""Layered memory: working, episodic, semantic and procedural."""

from keelgate.memory.backends import MemoryBackend, PostgresMemoryBackend, SqliteMemoryBackend
from keelgate.memory.embedder import Embedder, HashEmbedder, cosine
from keelgate.memory.http_embedders import EmbeddingError, OllamaEmbedder, OpenAICompatibleEmbedder
from keelgate.memory.tiers import (
    EpisodicMemory,
    ProceduralMemory,
    SemanticMemory,
    WorkingMemory,
)
from keelgate.memory.types import (
    Attribution,
    ConcurrentWriteError,
    InvalidMemoryWriteError,
    Memory,
    MemoryRecord,
    MemoryStoreError,
    MemoryTier,
    RecordNotFoundError,
)

__all__ = [
    "Attribution",
    "ConcurrentWriteError",
    "Embedder",
    "EmbeddingError",
    "EpisodicMemory",
    "HashEmbedder",
    "InvalidMemoryWriteError",
    "Memory",
    "MemoryBackend",
    "MemoryRecord",
    "MemoryStoreError",
    "MemoryTier",
    "OllamaEmbedder",
    "OpenAICompatibleEmbedder",
    "PostgresMemoryBackend",
    "ProceduralMemory",
    "RecordNotFoundError",
    "SemanticMemory",
    "SqliteMemoryBackend",
    "WorkingMemory",
    "cosine",
]
