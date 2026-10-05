"""Durable state: what must survive a killed process, and how it must read afterwards."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from keelgate.capabilities import (
    InMemoryBudgetLedger,
    InMemoryRevocationList,
    SqliteBudgetLedger,
    SqliteRevocationList,
)
from keelgate.tools import (
    CallContext,
    ClaimState,
    ErrorCode,
    InMemoryIdempotencyStore,
    OutcomeStatus,
    SideEffect,
    SqliteIdempotencyStore,
    ToolGateway,
)
from tests.conftest import Harness, policy_context, run

TRADE = {"symbol": "AAPL", "notional": 5000, "client_order_id": "dur-1"}

# ------------------------------------------------------------------- budget


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request: pytest.FixtureRequest) -> Any:
    return InMemoryBudgetLedger() if request.param == "memory" else SqliteBudgetLedger()


def test_ledgers_share_reserve_release_and_limit_semantics(ledger: Any) -> None:
    assert ledger.try_reserve("g", 6, 10)
    assert ledger.try_reserve("g", 4, 10)
    assert not ledger.try_reserve("g", 0.01, 10)
    ledger.release("g", 4)
    assert ledger.try_reserve("g", 4, 10)
    assert ledger.spent("g") == 10


@pytest.mark.parametrize("bad", [-1.0, float("nan")])
def test_ledgers_reject_impossible_costs(ledger: Any, bad: float) -> None:
    assert not ledger.try_reserve("g", bad, 10)


def test_ledger_release_never_goes_negative(ledger: Any) -> None:
    ledger.release("g", 50)
    assert ledger.spent("g") == 0


def test_a_budget_survives_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "budget.sqlite"
    first = SqliteBudgetLedger(db)
    assert first.try_reserve("g", 7, 10)
    first.close()
    second = SqliteBudgetLedger(db)
    assert second.spent("g") == 7
    assert not second.try_reserve("g", 4, 10)  # a restart does not hand back a fresh budget
    assert second.try_reserve("g", 3, 10)


def test_the_durable_ledger_cannot_be_oversubscribed_concurrently(tmp_path: Path) -> None:
    ledger = SqliteBudgetLedger(tmp_path / "b.sqlite")
    wins: list[bool] = []

    def worker() -> None:
        for _ in range(25):
            wins.append(ledger.try_reserve("g", 1, 100))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 100
    assert ledger.spent("g") == 100


def test_two_handles_on_one_file_share_one_budget(tmp_path: Path) -> None:
    db = tmp_path / "b.sqlite"
    a, b = SqliteBudgetLedger(db), SqliteBudgetLedger(db)
    assert a.try_reserve("g", 6, 10)
    assert not b.try_reserve("g", 6, 10)
    assert b.try_reserve("g", 4, 10)


# -------------------------------------------------------------- revocation


@pytest.mark.parametrize("make", [InMemoryRevocationList, SqliteRevocationList])
def test_revocation_lists_share_semantics(make: Any) -> None:
    revocations = make()
    assert not revocations.is_revoked("a")
    revocations.revoke("a")
    revocations.revoke("a")  # idempotent
    assert revocations.is_revoked("a")
    assert not revocations.is_revoked("b")


def test_a_revocation_survives_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "r.sqlite"
    first = SqliteRevocationList(db)
    first.revoke("g1")
    first.close()
    assert SqliteRevocationList(db).is_revoked("g1")


# -------------------------------------------------------------- idempotency


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Any:
    return InMemoryIdempotencyStore() if request.param == "memory" else SqliteIdempotencyStore()


def test_idempotency_stores_share_claim_semantics(store: Any) -> None:
    assert store.peek("t", "tool", "k", "h").state is ClaimState.NEW
    assert store.claim("t", "tool", "k", "h").state is ClaimState.NEW
    assert store.claim("t", "tool", "k", "h").state is ClaimState.IN_FLIGHT
    assert store.peek("t", "tool", "k", "other").state is ClaimState.CONFLICT
    store.complete("t", "tool", "k", {"order_id": "o1", "n": [1, 2]})
    done = store.peek("t", "tool", "k", "h")
    assert done.state is ClaimState.DONE and done.output == {"order_id": "o1", "n": [1, 2]}


def test_idempotency_stores_share_unknown_and_release(store: Any) -> None:
    store.claim("t", "tool", "k", "h")
    store.mark_unknown("t", "tool", "k")
    assert store.peek("t", "tool", "k", "h").state is ClaimState.UNKNOWN
    store.release("t", "tool", "k")
    assert store.peek("t", "tool", "k", "h").state is ClaimState.NEW


def test_idempotency_is_isolated_by_tenant_and_tool(store: Any) -> None:
    store.claim("t1", "tool", "k", "h")
    assert store.peek("t2", "tool", "k", "h").state is ClaimState.NEW
    assert store.peek("t1", "other", "k", "h").state is ClaimState.NEW


def test_a_completed_write_replays_after_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "i.sqlite"
    first = SqliteIdempotencyStore(db)
    first.claim("t", "tool", "k", "h")
    first.complete("t", "tool", "k", {"order_id": "o1"})
    first.close()
    claim = SqliteIdempotencyStore(db).peek("t", "tool", "k", "h")
    assert claim.state is ClaimState.DONE and claim.output == {"order_id": "o1"}


def test_a_write_interrupted_mid_flight_stays_unrepeatable_after_a_restart(tmp_path: Path) -> None:
    """The process died while the tool body ran. The key must NOT read as free."""
    db = tmp_path / "i.sqlite"
    first = SqliteIdempotencyStore(db)
    assert first.claim("t", "tool", "k", "h").state is ClaimState.NEW
    first.close()  # no complete(): the process "died"
    reopened = SqliteIdempotencyStore(db)
    assert reopened.peek("t", "tool", "k", "h").state is ClaimState.IN_FLIGHT
    assert reopened.claim("t", "tool", "k", "h").state is ClaimState.IN_FLIGHT  # not NEW


def test_two_handles_cannot_both_claim_the_same_key(tmp_path: Path) -> None:
    db = tmp_path / "i.sqlite"
    a, b = SqliteIdempotencyStore(db), SqliteIdempotencyStore(db)
    states = [a.claim("t", "tool", "k", "h").state, b.claim("t", "tool", "k", "h").state]
    assert states.count(ClaimState.NEW) == 1


# -------------------------------------------------- the gateway on durable state


def gateway_over(h: Harness, idempotency: SqliteIdempotencyStore) -> ToolGateway:
    return ToolGateway(
        registry=h.registry,
        verifier=h.verifier,
        engine=h.engine,
        audit=h.audit,
        approvals=h.queue,
        idempotency=idempotency,
    )


def call(h: Harness, gateway: ToolGateway, token: str, args: dict[str, Any]) -> Any:
    return run(
        gateway.call(
            tool_name="trade_paper_execute", arguments=args, grant_token=token, context=h.ctx()
        )
    )


def test_a_restarted_gateway_replays_a_completed_write_instead_of_repeating_it(
    h: Harness, tmp_path: Path
) -> None:
    db = tmp_path / "i.sqlite"
    token = h.grant()
    first = call(h, gateway_over(h, SqliteIdempotencyStore(db)), token, TRADE)
    assert first.ok and len(h.executed) == 1

    again = call(h, gateway_over(h, SqliteIdempotencyStore(db)), token, TRADE)  # "restarted"
    assert again.ok and again.replayed
    assert len(h.executed) == 1


def test_a_restarted_gateway_refuses_to_repeat_a_write_that_died_mid_flight(
    h: Harness, tmp_path: Path
) -> None:
    db = tmp_path / "i.sqlite"
    # Simulate the dead process: it claimed the key, then never finished.
    dead = SqliteIdempotencyStore(db)
    args = {"symbol": "AAPL", "notional": 5000.0, "client_order_id": "dur-1"}
    from keelgate.approvals import action_hash

    dead.claim("tenant-1", "trade_paper_execute", "dur-1", action_hash("trade_paper_execute", args))
    dead.close()

    out = call(h, gateway_over(h, SqliteIdempotencyStore(db)), h.grant(), TRADE)
    assert out.error is not None and out.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert h.executed == []


# ------------------------------------------------------------ allowed side effects


def ctx_with(h: Harness, allowed: frozenset[SideEffect] | None) -> CallContext:
    return CallContext(
        tenant_id="tenant-1", policy_context=policy_context(), allowed_side_effects=allowed
    )


@pytest.mark.parametrize("tool_name", ["trade_paper_execute", "trade_propose", "report_write"])
def test_a_read_only_call_cannot_use_propose_or_write_tools(h: Harness, tool_name: str) -> None:
    args = (
        {"title": "x"}
        if tool_name == "report_write"
        else {"symbol": "AAPL", "notional": 100, "client_order_id": "ro"}
    )
    token = h.grant(cost=10.0)
    out = h.call(tool_name, args, token=token, context=ctx_with(h, frozenset({SideEffect.READ})))
    assert out.status is OutcomeStatus.DENIED
    assert out.error is not None and out.error.code is ErrorCode.SIDE_EFFECT_NOT_PERMITTED
    assert h.executed == []
    assert h.gateway._ledger.spent(h.verifier.verify(token).grant_id) == 0
    assert any(
        r.payload.get("code") == "side_effect_not_permitted" for r in h.audit.records("tenant-1")
    )


def test_a_read_only_call_may_use_read_tools(h: Harness) -> None:
    out = h.call(
        "market_quote", {"symbol": "AAPL"}, context=ctx_with(h, frozenset({SideEffect.READ}))
    )
    assert out.ok


def test_no_restriction_means_unrestricted(h: Harness) -> None:
    assert h.call("trade_paper_execute", TRADE, context=ctx_with(h, None)).ok


def test_an_empty_allowed_set_permits_nothing(h: Harness) -> None:
    out = h.call("market_quote", {"symbol": "AAPL"}, context=ctx_with(h, frozenset()))
    assert out.error is not None and out.error.code is ErrorCode.SIDE_EFFECT_NOT_PERMITTED


def test_the_restriction_applies_even_when_the_grant_would_allow_the_tool(h: Harness) -> None:
    out = h.call(
        "trade_paper_execute",
        TRADE,
        token=h.grant(("trade:paper_execute",)),
        context=ctx_with(h, frozenset({SideEffect.READ, SideEffect.PROPOSE})),
    )
    assert out.error is not None and out.error.code is ErrorCode.SIDE_EFFECT_NOT_PERMITTED


# ------------------------------------------------------------- schema override


def test_a_tool_spec_can_carry_an_external_schema(h: Harness) -> None:
    from dataclasses import replace

    spec = h.registry.get("market_quote").spec  # type: ignore[union-attr]
    custom = {"type": "object", "properties": {"x": {"type": "integer"}}}
    assert replace(spec, input_schema=custom).input_schema == custom
    described = {d["name"]: d for d in h.registry.describe()}
    assert described["market_quote"]["input_schema"]["properties"].keys() == {"symbol"}
