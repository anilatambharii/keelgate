"""A checkpoint store backed by a LangGraph checkpointer (``pip install 'keelgate[langgraph]'``).

LangGraph's savers are usable on their own, outside a graph: each loop checkpoint becomes
one LangGraph checkpoint on the thread ``<tenant>::<run>``, with the loop state as the value
of a single channel. That gives Keelgate LangGraph's persistence backends (SQLite, Postgres)
without making the loop itself a LangGraph graph, so the core stays free of the dependency.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from keelgate.loop.checkpoint import StaleCheckpointError
from keelgate.loop.state import LoopState

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.checkpoint.base import BaseCheckpointSaver

_CHANNEL = "loop_state"
_SEP = "::"


class LangGraphCheckpointStore:
    """Loop checkpoints on a LangGraph saver.

    The stale-writer guard (``save`` refuses a sequence that is not newer) is a read followed by
    a write, which LangGraph's saver API cannot make atomic. A lock makes it safe between threads
    of one process. It does NOT protect two processes writing the same run; use
    :class:`~keelgate.loop.checkpoint.SqliteCheckpointStore`, whose primary key enforces it in
    the database, where more than one process may drive a run.
    """

    def __init__(self, saver: BaseCheckpointSaver[Any]) -> None:
        self._saver = saver
        self._lock = threading.Lock()

    @classmethod
    def sqlite(cls, path: str | Path = ":memory:") -> LangGraphCheckpointStore:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        return cls(SqliteSaver(conn))

    @staticmethod
    def _config(tenant_id: str, run_id: str) -> Any:
        if _SEP in tenant_id:  # keeps "<tenant>::<run>" unambiguous across tenants
            raise ValueError(f"a tenant id must not contain {_SEP!r}")
        return {"configurable": {"thread_id": f"{tenant_id}{_SEP}{run_id}", "checkpoint_ns": ""}}

    def save(self, state: LoopState) -> None:
        with self._lock:
            self._save(state)

    def _save(self, state: LoopState) -> None:
        config = self._config(state.tenant_id, state.run_id)
        latest = self._saver.get_tuple(config)
        parent = config
        if latest is not None:
            current = latest.checkpoint["channel_values"][_CHANNEL]["checkpoint_seq"]
            if current >= state.checkpoint_seq:
                raise StaleCheckpointError(
                    f"{state.run_id}: seq {state.checkpoint_seq} is not newer than {current}"
                )
            parent = latest.config
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {_CHANNEL: state.model_dump(mode="json")}
        checkpoint["channel_versions"] = {_CHANNEL: state.checkpoint_seq}
        self._saver.put(
            parent,
            checkpoint,
            {"source": "update", "step": state.checkpoint_seq, "parents": {}},
            {_CHANNEL: state.checkpoint_seq},
        )

    def load(self, tenant_id: str, run_id: str) -> LoopState | None:
        found = self._saver.get_tuple(self._config(tenant_id, run_id))
        if found is None:
            return None
        state = LoopState.model_validate(found.checkpoint["channel_values"][_CHANNEL])
        return state if state.tenant_id == tenant_id else None

    def runs(self, tenant_id: str, *, prefix: str = "") -> list[str]:
        self._config(tenant_id, "")  # validates the tenant id
        wanted = f"{tenant_id}{_SEP}{prefix}"
        threads = {
            t.config["configurable"]["thread_id"]
            for t in self._saver.list(None)
            if t.config["configurable"]["thread_id"].startswith(wanted)
        }
        return sorted(t.split(_SEP, 1)[1] for t in threads)
