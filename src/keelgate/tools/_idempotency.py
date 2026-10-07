"""Idempotency for WRITE tools.

A key is claimed before the tool runs. Three outcomes matter beyond "new":

* **DONE** - the same key and the same arguments already ran; return the stored
  result instead of repeating the side effect.
* **CONFLICT** - the key was used with *different* arguments. Refused outright.
* **UNKNOWN** - a previous attempt may or may not have taken effect (it timed
  out, or its result could not be validated). It is never retried automatically:
  repeating a possibly-executed money movement is worse than stopping for a human.

This in-memory store is process-local. A deployment with several processes, or
one that must survive a restart, needs a shared durable implementation of the
same protocol; that is a known K1 gap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class ClaimState(StrEnum):
    """Where an idempotency key stands when a caller tries to claim it.

    ``NEW`` may run; ``DONE`` replays the stored result; ``IN_FLIGHT`` is running now;
    ``UNKNOWN`` died mid-flight and must never be retried automatically; ``CONFLICT`` means the
    key was reused for different arguments.
    """

    NEW = "NEW"
    DONE = "DONE"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"
    IN_FLIGHT = "IN_FLIGHT"


@dataclass(frozen=True)
class Claim:
    """The outcome of claiming an idempotency key.

    ``state`` says whether the caller may run the call, must replay the stored ``output``, or is
    looking at a call that is still in flight or of unknown outcome.
    """

    state: ClaimState
    output: dict[str, Any] | None = None


class IdempotencyStore(Protocol):
    """Where idempotency keys and stored results live.

    Implement this to share them across processes. ``claim`` must be atomic, and a key left in
    flight by a process that died must read as ``UNKNOWN``.
    """

    def peek(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim: ...

    def claim(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim: ...

    def complete(self, tenant_id: str, tool: str, key: str, output: dict[str, Any]) -> None: ...

    def mark_unknown(self, tenant_id: str, tool: str, key: str) -> None: ...

    def release(self, tenant_id: str, tool: str, key: str) -> None: ...


@dataclass
class _Entry:
    args_hash: str
    state: ClaimState
    output: dict[str, Any] | None = None


class InMemoryIdempotencyStore:
    """Process-local idempotency store.

    Fine for tests and single-process use. It is lost on restart, so use ``SqliteIdempotencyStore``
    (or your own ``IdempotencyStore``) when a WRITE must never run twice across restarts.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        self._lock = threading.Lock()

    def peek(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim:
        """Read the state of a key without claiming it."""
        with self._lock:
            return self._classify(self._entries.get((tenant_id, tool, key)), args_hash)

    def claim(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim:
        """Atomically claim a key for execution; NEW means the caller may run."""
        with self._lock:
            entry = self._entries.get((tenant_id, tool, key))
            if entry is None:
                self._entries[(tenant_id, tool, key)] = _Entry(args_hash, ClaimState.IN_FLIGHT)
                return Claim(ClaimState.NEW)
            return self._classify(entry, args_hash)

    @staticmethod
    def _classify(entry: _Entry | None, args_hash: str) -> Claim:
        if entry is None:
            return Claim(ClaimState.NEW)
        if entry.args_hash != args_hash:
            return Claim(ClaimState.CONFLICT)
        if entry.state is ClaimState.DONE:
            return Claim(ClaimState.DONE, entry.output)
        return Claim(entry.state)

    def complete(self, tenant_id: str, tool: str, key: str, output: dict[str, Any]) -> None:
        with self._lock:
            entry = self._entries[(tenant_id, tool, key)]
            entry.state = ClaimState.DONE
            entry.output = output

    def mark_unknown(self, tenant_id: str, tool: str, key: str) -> None:
        with self._lock:
            self._entries[(tenant_id, tool, key)].state = ClaimState.UNKNOWN

    def release(self, tenant_id: str, tool: str, key: str) -> None:
        """Forget a claim that provably did not execute, so a retry is allowed."""
        with self._lock:
            self._entries.pop((tenant_id, tool, key), None)


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency (
    tenant_id   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    key         TEXT NOT NULL,
    args_hash   TEXT NOT NULL,
    state       TEXT NOT NULL,
    output_json TEXT,
    PRIMARY KEY (tenant_id, tool, key)
);
"""


class SqliteIdempotencyStore:
    """Durable idempotency, safe to share between processes on one host.

    This is what makes resume safe. If a process dies *after* a WRITE completed but
    *before* the loop checkpointed it, the key is ``DONE`` and the retry replays the
    stored result. If it dies *during* the WRITE, the key is still ``IN_FLIGHT``, which
    a retry reads as outcome-unknown and refuses to repeat: a possibly-executed money
    movement is never run twice automatically.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SQLITE_SCHEMA)
        self._lock = threading.RLock()

    def _entry(self, tenant_id: str, tool: str, key: str) -> _Entry | None:
        row = self._conn.execute(
            "SELECT args_hash, state, output_json FROM idempotency "
            "WHERE tenant_id = ? AND tool = ? AND key = ?",
            (tenant_id, tool, key),
        ).fetchone()
        if row is None:
            return None
        output = json.loads(row[2]) if row[2] is not None else None
        return _Entry(row[0], ClaimState(row[1]), output)

    def peek(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim:
        with self._lock:
            return InMemoryIdempotencyStore._classify(self._entry(tenant_id, tool, key), args_hash)

    def claim(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                entry = self._entry(tenant_id, tool, key)
                if entry is None:
                    self._conn.execute(
                        "INSERT INTO idempotency (tenant_id, tool, key, args_hash, state) "
                        "VALUES (?,?,?,?,?)",
                        (tenant_id, tool, key, args_hash, ClaimState.IN_FLIGHT.value),
                    )
                    self._conn.execute("COMMIT")
                    return Claim(ClaimState.NEW)
                self._conn.execute("COMMIT")
                return InMemoryIdempotencyStore._classify(entry, args_hash)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def complete(self, tenant_id: str, tool: str, key: str, output: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE idempotency SET state = ?, output_json = ? "
                "WHERE tenant_id = ? AND tool = ? AND key = ?",
                (
                    ClaimState.DONE.value,
                    json.dumps(output, sort_keys=True, allow_nan=False),
                    tenant_id,
                    tool,
                    key,
                ),
            )

    def mark_unknown(self, tenant_id: str, tool: str, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE idempotency SET state = ? WHERE tenant_id = ? AND tool = ? AND key = ?",
                (ClaimState.UNKNOWN.value, tenant_id, tool, key),
            )

    def release(self, tenant_id: str, tool: str, key: str) -> None:
        """Forget a claim that provably did not execute, so a retry is allowed."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM idempotency WHERE tenant_id = ? AND tool = ? AND key = ?",
                (tenant_id, tool, key),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
