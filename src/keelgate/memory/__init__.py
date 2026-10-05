"""Layered memory: working, episodic, semantic and procedural."""

from keelgate.memory.backends import MemoryBackend, PostgresMemoryBackend, SqliteMemoryBackend
from keelgate.memory.embedder import Embedder, HashEmbedder, cosine
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
    "EpisodicMemory",
    "HashEmbedder",
    "InvalidMemoryWriteError",
    "Memory",
    "MemoryBackend",
    "MemoryRecord",
    "MemoryStoreError",
    "MemoryTier",
    "PostgresMemoryBackend",
    "ProceduralMemory",
    "RecordNotFoundError",
    "SemanticMemory",
    "SqliteMemoryBackend",
    "WorkingMemory",
    "cosine",
]
