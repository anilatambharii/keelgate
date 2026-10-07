"""One contract, every checkpoint store: in-memory, SQLite, and LangGraph's SQLite saver."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from keelgate.loop import (
    CheckpointStore,
    InMemoryCheckpointStore,
    SqliteCheckpointStore,
    StaleCheckpointError,
)
from keelgate.loop._state import LoopState
from tests.conftest import MARKET_OPEN


def state(run_id: str = "r1", tenant: str = "t1", seq: int = 1, **kw: Any) -> LoopState:
    return LoopState(
        run_id=run_id,
        tenant_id=tenant,
        agent_id="a",
        trace_id="tr",
        goal="g",
        as_of=MARKET_OPEN,
        created_at=MARKET_OPEN,
        updated_at=MARKET_OPEN,
        checkpoint_seq=seq,
        **kw,
    )


def _langgraph(tmp_path: Path) -> CheckpointStore:
    pytest.importorskip("langgraph")
    from keelgate.loop.langgraph_store import LangGraphCheckpointStore

    return LangGraphCheckpointStore.sqlite(tmp_path / "lg.sqlite")


FACTORIES: dict[str, Callable[[Path], CheckpointStore]] = {
    "memory": lambda _p: InMemoryCheckpointStore(),
    "sqlite": lambda p: SqliteCheckpointStore(p / "cp.sqlite"),
    "langgraph": _langgraph,
}


@pytest.fixture(params=list(FACTORIES))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CheckpointStore]:
    made = FACTORIES[request.param](tmp_path)
    yield made
    close = getattr(made, "close", None)
    if callable(close):
        close()


def test_a_saved_state_round_trips_exactly(store: CheckpointStore) -> None:
    saved = state(iteration=3, tokens_used=120, dollars_used=0.5, final_answer="done")
    store.save(saved)
    assert store.load("t1", "r1") == saved


def test_load_returns_the_latest_and_none_for_an_unknown_run(store: CheckpointStore) -> None:
    assert store.load("t1", "missing") is None
    store.save(state(seq=1, iteration=1))
    store.save(state(seq=2, iteration=2))
    loaded = store.load("t1", "r1")
    assert loaded is not None and loaded.checkpoint_seq == 2 and loaded.iteration == 2


@pytest.mark.parametrize("stale_seq", [1, 2])
def test_a_stale_or_repeated_sequence_is_refused_and_changes_nothing(
    store: CheckpointStore, stale_seq: int
) -> None:
    store.save(state(seq=1, iteration=1))
    store.save(state(seq=2, iteration=2))
    with pytest.raises(StaleCheckpointError):
        store.save(state(seq=stale_seq, iteration=99))
    loaded = store.load("t1", "r1")
    assert loaded is not None and loaded.iteration == 2


def test_tenants_cannot_see_each_others_runs(store: CheckpointStore) -> None:
    store.save(state(run_id="shared", tenant="t1", iteration=1))
    store.save(state(run_id="shared", tenant="t2", iteration=2))
    one, two = store.load("t1", "shared"), store.load("t2", "shared")
    assert one is not None and one.iteration == 1
    assert two is not None and two.iteration == 2
    assert store.load("t3", "shared") is None
    assert store.runs("t1") == ["shared"] and store.runs("t3") == []


def test_one_tenants_sequence_does_not_block_anothers(store: CheckpointStore) -> None:
    store.save(state(tenant="t1", seq=5))
    store.save(state(tenant="t2", seq=1))  # would be stale if the tenants were conflated
    loaded = store.load("t2", "r1")
    assert loaded is not None and loaded.checkpoint_seq == 1


def test_runs_lists_by_prefix_and_treats_it_literally(store: CheckpointStore) -> None:
    for rid in ("a-1", "a-2", "b-1", "a%x", "a_y"):
        store.save(state(run_id=rid))
    assert store.runs("t1", prefix="a-") == ["a-1", "a-2"]
    assert store.runs("t1", prefix="a%") == ["a%x"]  # '%' and '_' are not wildcards
    assert store.runs("t1", prefix="a_") == ["a_y"]
    assert store.runs("t1") == sorted(["a-1", "a-2", "b-1", "a%x", "a_y"])


def test_a_hostile_run_id_cannot_reach_another_tenants_state(store: CheckpointStore) -> None:
    store.save(state(run_id="victim", tenant="t2"))
    for sneaky in ("t2::victim", "../victim", "victim' OR '1'='1", "t2"):
        assert store.load("t1", sneaky) is None


def test_concurrent_writers_to_one_sequence_let_exactly_one_win(store: CheckpointStore) -> None:
    wins: list[int] = []
    stale: list[int] = []
    barrier = threading.Barrier(4)

    def writer(n: int) -> None:
        barrier.wait()
        try:
            store.save(state(seq=1, iteration=n))
            wins.append(n)
        except (StaleCheckpointError, sqlite3.Error):
            stale.append(n)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and len(stale) == 3
    loaded = store.load("t1", "r1")
    assert loaded is not None and loaded.iteration == wins[0]


def test_a_sqlite_store_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite"
    first = SqliteCheckpointStore(path)
    first.save(state(seq=3, iteration=3))
    first.close()
    second = SqliteCheckpointStore(path)
    try:
        loaded = second.load("t1", "r1")
        assert loaded is not None and loaded.iteration == 3
    finally:
        second.close()


def test_a_run_can_move_between_the_sqlite_and_langgraph_stores(tmp_path: Path) -> None:
    pytest.importorskip("langgraph")
    from keelgate.loop.langgraph_store import LangGraphCheckpointStore

    a = SqliteCheckpointStore(tmp_path / "a.sqlite")
    b = LangGraphCheckpointStore.sqlite(tmp_path / "b.sqlite")
    try:
        a.save(state(seq=4, iteration=4, final_answer="x"))
        moved = a.load("t1", "r1")
        assert moved is not None
        b.save(moved)
        assert b.load("t1", "r1") == moved
    finally:
        a.close()


def test_the_langgraph_store_refuses_a_tenant_id_that_could_alias_another_tenants_runs(
    tmp_path: Path,
) -> None:
    store = _langgraph(tmp_path)
    for call in (
        lambda: store.save(state(tenant="t1::sneaky")),
        lambda: store.load("t1::sneaky", "r1"),
        lambda: store.runs("t1::sneaky"),
    ):
        with pytest.raises(ValueError, match="tenant id"):
            call()


def test_processes_racing_on_one_sequence_let_exactly_one_win(tmp_path: Path) -> None:
    """The LangGraph store is guarded across OS processes, not just threads."""
    import subprocess
    import sys
    import time

    pytest.importorskip("langgraph")
    db = str(tmp_path / "race.sqlite")
    start = time.time() + 6.0  # every worker has imported by then, so they collide
    root = Path(__file__).resolve().parents[1]
    workers = [
        subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, str(root / "tests" / "lg_writer.py"), db, str(start), str(n)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=root,
        )
        for n in range(5)
    ]
    outputs = [w.communicate(timeout=120) for w in workers]
    assert all(w.returncode == 0 for w in workers), [o[1][-500:] for o in outputs]
    verdicts = [o[0].split()[0] for o in outputs]
    assert verdicts.count("WIN") == 1 and verdicts.count("STALE") == 4, verdicts
    from keelgate.loop.langgraph_store import LangGraphCheckpointStore

    loaded = LangGraphCheckpointStore.sqlite(db).load("t1", "race")
    assert loaded is not None and loaded.checkpoint_seq == 1
