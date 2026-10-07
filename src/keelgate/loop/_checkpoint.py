"""Checkpoint stores: where loop state is saved after every step.

A store only has to save state atomically and return the latest. The sequence number makes
a stale writer harmless: if two processes ever drive the same run, the second to save is
refused instead of silently overwriting the first.

``default_checkpointer`` prefers LangGraph's SQLite saver when ``keelgate[langgraph]`` is
installed, and otherwise falls back to the standard-library store below. Both hold the same
JSON, so a run can be moved from one to the other.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from keelgate.loop._state import LoopState

if TYPE_CHECKING:
    from pathlib import Path


class StaleCheckpointError(Exception):
    """A newer checkpoint already exists: another writer got there first."""


@runtime_checkable
class CheckpointStore(Protocol):
    """Where loop state is saved after every step.

    ``save`` is atomic and refuses a sequence number that is not newer, so a stale writer cannot
    overwrite a newer checkpoint; ``load`` returns the latest state.
    """

    def save(self, state: LoopState) -> None:
        """Persist ``state`` atomically. Raises :class:`StaleCheckpointError` if it is not newer."""
        ...

    def load(self, tenant_id: str, run_id: str) -> LoopState | None:
        """The latest state, or None. A run in another tenant is simply absent."""
        ...

    def runs(self, tenant_id: str, *, prefix: str = "") -> list[str]: ...


@runtime_checkable
class HistoryCheckpointStore(CheckpointStore, Protocol):
    """A store that keeps every checkpoint, oldest first. Replay needs this."""

    def history(self, tenant_id: str, run_id: str) -> list[LoopState]: ...


class InMemoryCheckpointStore:
    """Process-local. Fine for tests; it does not survive the process, so it cannot resume."""

    def __init__(self) -> None:
        self._latest: dict[tuple[str, str], str] = {}
        self._history: dict[tuple[str, str], list[str]] = {}
        self._lock = threading.Lock()

    def save(self, state: LoopState) -> None:
        key = (state.tenant_id, state.run_id)
        with self._lock:
            current = self._latest.get(key)
            if (
                current is not None
                and LoopState.model_validate_json(current).checkpoint_seq >= state.checkpoint_seq
            ):
                raise StaleCheckpointError(
                    f"{state.run_id}: seq {state.checkpoint_seq} is not newer"
                )
            encoded = state.model_dump_json()
            self._latest[key] = encoded
            self._history.setdefault(key, []).append(encoded)

    def load(self, tenant_id: str, run_id: str) -> LoopState | None:
        with self._lock:
            raw = self._latest.get((tenant_id, run_id))
        return LoopState.model_validate_json(raw) if raw else None

    def history(self, tenant_id: str, run_id: str) -> list[LoopState]:
        with self._lock:
            rows = list(self._history.get((tenant_id, run_id), []))
        return [LoopState.model_validate_json(r) for r in rows]

    def runs(self, tenant_id: str, *, prefix: str = "") -> list[str]:
        with self._lock:
            return sorted(r for (t, r) in self._latest if t == tenant_id and r.startswith(prefix))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_checkpoints (
    tenant_id TEXT    NOT NULL,
    run_id    TEXT    NOT NULL,
    seq       INTEGER NOT NULL,
    state     TEXT    NOT NULL,
    PRIMARY KEY (tenant_id, run_id, seq)
);
"""


class SqliteCheckpointStore:
    """Durable, dependency-free. Keeps every checkpoint, so a run's history can be inspected."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def save(self, state: LoopState) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT MAX(seq) FROM loop_checkpoints WHERE tenant_id = ? AND run_id = ?",
                    (state.tenant_id, state.run_id),
                ).fetchone()
                if row[0] is not None and row[0] >= state.checkpoint_seq:
                    self._conn.execute("ROLLBACK")
                    raise StaleCheckpointError(
                        f"{state.run_id}: seq {state.checkpoint_seq} is not newer than {row[0]}"
                    )
                self._conn.execute(
                    "INSERT INTO loop_checkpoints (tenant_id, run_id, seq, state) VALUES (?,?,?,?)",
                    (state.tenant_id, state.run_id, state.checkpoint_seq, state.model_dump_json()),
                )
                self._conn.execute("COMMIT")
            except StaleCheckpointError:
                raise
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def load(self, tenant_id: str, run_id: str) -> LoopState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM loop_checkpoints WHERE tenant_id = ? AND run_id = ? "
                "ORDER BY seq DESC LIMIT 1",
                (tenant_id, run_id),
            ).fetchone()
        return LoopState.model_validate_json(row[0]) if row else None

    def history(self, tenant_id: str, run_id: str) -> list[LoopState]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state FROM loop_checkpoints WHERE tenant_id = ? AND run_id = ? "
                "ORDER BY seq",
                (tenant_id, run_id),
            ).fetchall()
        return [LoopState.model_validate_json(r[0]) for r in rows]

    def runs(self, tenant_id: str, *, prefix: str = "") -> list[str]:
        with self._lock:
            # "!" is the LIKE escape character: a run id containing % or _ must match literally.
            escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            rows = self._conn.execute(
                "SELECT DISTINCT run_id FROM loop_checkpoints WHERE tenant_id = ? "
                "AND run_id LIKE ? ESCAPE '!' ORDER BY run_id",
                (tenant_id, escaped + "%"),
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def default_checkpointer(path: str | Path = ":memory:") -> CheckpointStore:
    """A durable store: LangGraph's SQLite saver if installed, else the standard-library one.

    The choice depends on what is installed, so pass an explicit store anywhere the two
    must agree. Both hold the same JSON and a run can be moved between them.
    """
    try:
        from keelgate.loop.langgraph_store import LangGraphCheckpointStore  # noqa: PLC0415
    except ImportError:
        return SqliteCheckpointStore(path)
    return LangGraphCheckpointStore.sqlite(path)
