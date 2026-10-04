"""Tamper-evident, hash-chained audit log and its verifier."""

from keelgate.audit.log import AuditLog
from keelgate.audit.records import (
    AuditError,
    AuditRecord,
    ChainHead,
    ChainVerification,
    EventType,
    canonical_json,
    genesis_hash,
    verify_chain,
)
from keelgate.audit.stores import AuditStore, PostgresAuditStore, SqliteAuditStore

__all__ = [
    "AuditError",
    "AuditLog",
    "AuditRecord",
    "AuditStore",
    "ChainHead",
    "ChainVerification",
    "EventType",
    "PostgresAuditStore",
    "SqliteAuditStore",
    "canonical_json",
    "genesis_hash",
    "verify_chain",
]
