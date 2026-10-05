"""Worker for the multi-process LangGraph checkpoint race. argv: db_path start_epoch writer_id."""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

from keelgate.loop import StaleCheckpointError
from keelgate.loop.langgraph_store import LangGraphCheckpointStore
from keelgate.loop.state import LoopState

db, start, writer = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
now = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
state = LoopState(
    run_id="race",
    tenant_id="t1",
    agent_id="a",
    trace_id="tr",
    goal="g",
    as_of=now,
    created_at=now,
    updated_at=now,
    checkpoint_seq=1,
    iteration=writer,
)
store = LangGraphCheckpointStore.sqlite(db)

# Widen the read-then-write window, as a slow backend would, so an unguarded store always races.
_real_put = store._saver.put


def _slow_put(*args, **kwargs):  # type: ignore[no-untyped-def]
    time.sleep(0.4)
    return _real_put(*args, **kwargs)


store._saver.put = _slow_put  # type: ignore[method-assign]
while time.time() < start:
    time.sleep(0.001)
try:
    store.save(state)
    sys.stdout.write("WIN " + str(writer))
except StaleCheckpointError:
    sys.stdout.write("STALE " + str(writer))
