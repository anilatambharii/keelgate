"""Capability strings and signed grants: scope, expiry, forgery, revocation."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pyseto
import pytest
from pydantic import BaseModel

from keelgate.capabilities import (
    Capability,
    GrantExpiredError,
    GrantInvalidError,
    GrantNotYetValidError,
    GrantRevokedError,
    GrantSigner,
    GrantVerifier,
    InMemoryBudgetLedger,
    InMemoryRevocationList,
    InvalidCapabilityError,
    UnknownKeyError,
    issue_grant,
)
from tests.conftest import MARKET_OPEN, Clock


def make(
    *,
    ttl: timedelta = timedelta(minutes=10),
    caps: tuple[str, ...] = ("market_data:read",),
    clock: Clock | None = None,
) -> tuple[GrantSigner, GrantVerifier, Clock, str]:
    clock = clock or Clock()
    signer = GrantSigner.generate()
    verifier = GrantVerifier({"k1": signer.public_key_pem()}, clock=clock)
    token = issue_grant(
        signer, agent_id="a1", tenant_id="t1", capabilities=caps, max_cost=10, ttl=ttl, clock=clock
    ).token
    return signer, verifier, clock, token


# ----------------------------------------------------------------- capability


@pytest.mark.parametrize("value", ["market_data:read", "trade:paper_execute", "a:b", "a1:b2_c"])
def test_valid_capabilities(value: str) -> None:
    cap = Capability(value)
    assert cap == value
    assert f"{cap.resource}:{cap.action}" == value


@pytest.mark.parametrize(
    "value",
    [
        "trade:*",
        "*",
        "*:read",
        "trade:",
        ":read",
        "Trade:read",
        "trade",
        "a:b:c",
        "a b:c",
        "",
        "x:\n",
    ],
)
def test_wildcards_and_malformed_capabilities_are_rejected(value: str) -> None:
    with pytest.raises(InvalidCapabilityError):
        Capability(value)


def test_capability_rejects_non_strings() -> None:
    with pytest.raises(InvalidCapabilityError):
        Capability(123)


def test_capability_validates_inside_pydantic_models() -> None:
    class M(BaseModel):
        cap: Capability

    assert M(cap="a:b").cap == "a:b"
    with pytest.raises(ValueError, match="invalid capability"):
        M(cap="a:*")


# ---------------------------------------------------------------------- issue


def test_grant_round_trips_every_claim() -> None:
    signer, verifier, clock, token = make(caps=("market_data:read", "trade:propose"))
    grant = verifier.verify(token)
    assert grant.agent_id == "a1"
    assert grant.tenant_id == "t1"
    assert grant.capabilities == {"market_data:read", "trade:propose"}
    assert grant.budget.max_cost == 10
    assert grant.issued_at == clock.now
    assert grant.expires_at == clock.now + timedelta(minutes=10)
    assert grant.key_id == signer.key_id


def test_grant_allows_only_exact_capabilities() -> None:
    _, verifier, _, token = make(caps=("market_data:read",))
    grant = verifier.verify(token)
    assert grant.allows("market_data:read")
    assert not grant.allows("market_data:write")
    assert not grant.allows("trade:propose")
    assert not grant.allows("market_data:*")
    assert not grant.allows("")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"capabilities": []}, "at least one capability"),
        ({"capabilities": ["trade:*"]}, "invalid capability"),
        ({"ttl": timedelta(0)}, "positive"),
        ({"ttl": timedelta(seconds=-5)}, "positive"),
        ({"ttl": timedelta(days=2)}, "exceeds the maximum"),
        ({"agent_id": ""}, "agent_id"),
        ({"agent_id": "bad id with spaces"}, "agent_id"),
        ({"tenant_id": "../etc"}, "tenant_id"),
        ({"max_cost": -1}, ""),
        ({"max_cost": float("nan")}, ""),
        ({"max_cost": float("inf")}, ""),
    ],
)
def test_issue_grant_refuses_unsafe_input(kwargs: dict[str, Any], message: str) -> None:
    base: dict[str, Any] = {
        "agent_id": "a1",
        "tenant_id": "t1",
        "capabilities": ["market_data:read"],
        "max_cost": 1,
        "ttl": timedelta(minutes=1),
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=message or None):
        issue_grant(GrantSigner.generate(), **base)


def test_issue_grant_requires_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        issue_grant(
            GrantSigner.generate(),
            agent_id="a",
            tenant_id="t",
            capabilities=["a:b"],
            max_cost=1,
            ttl=timedelta(minutes=1),
            clock=lambda: datetime(2026, 1, 1),  # noqa: DTZ001 - the point of the test
        )


def test_grants_get_unique_ids() -> None:
    signer = GrantSigner.generate()
    ids = {
        issue_grant(
            signer,
            agent_id="a",
            tenant_id="t",
            capabilities=["a:b"],
            max_cost=1,
            ttl=timedelta(minutes=1),
        ).grant.grant_id
        for _ in range(20)
    }
    assert len(ids) == 20


# --------------------------------------------------------------------- expiry


def test_grant_is_valid_up_to_but_not_including_expiry() -> None:
    _, verifier, clock, token = make(ttl=timedelta(minutes=10))
    clock.advance(timedelta(minutes=10) - timedelta(microseconds=1))
    verifier.verify(token)
    clock.advance(timedelta(microseconds=1))
    with pytest.raises(GrantExpiredError):
        verifier.verify(token)


def test_grant_far_past_expiry_stays_expired() -> None:
    _, verifier, clock, token = make()
    clock.advance(timedelta(days=365))
    with pytest.raises(GrantExpiredError):
        verifier.verify(token)


def test_grant_not_valid_before_not_before() -> None:
    clock = Clock()
    signer = GrantSigner.generate()
    verifier = GrantVerifier({"k1": signer.public_key_pem()}, clock=clock)
    token = issue_grant(
        signer,
        agent_id="a",
        tenant_id="t",
        capabilities=["a:b"],
        max_cost=1,
        ttl=timedelta(minutes=5),
        clock=clock,
        not_before=clock.now + timedelta(minutes=1),
    ).token
    with pytest.raises(GrantNotYetValidError):
        verifier.verify(token)
    clock.advance(timedelta(minutes=1))
    verifier.verify(token)


def test_leeway_extends_both_edges_by_exactly_the_leeway() -> None:
    clock = Clock()
    signer = GrantSigner.generate()
    verifier = GrantVerifier(
        {"k1": signer.public_key_pem()}, clock=clock, leeway=timedelta(seconds=30)
    )
    token = issue_grant(
        signer,
        agent_id="a",
        tenant_id="t",
        capabilities=["a:b"],
        max_cost=1,
        ttl=timedelta(minutes=1),
        clock=clock,
    ).token
    clock.advance(timedelta(seconds=89))
    verifier.verify(token)
    clock.advance(timedelta(seconds=1))
    with pytest.raises(GrantExpiredError):
        verifier.verify(token)


def test_negative_leeway_is_refused() -> None:
    with pytest.raises(ValueError, match="leeway"):
        GrantVerifier({"k1": GrantSigner.generate().public_key_pem()}, leeway=timedelta(seconds=-1))


# ------------------------------------------------------------------- forgery


def test_tampered_payload_is_rejected() -> None:
    _, verifier, _, token = make()
    head, kind, payload, footer = token.split(".")
    flipped = payload[:-4] + ("AAAA" if not payload.endswith("AAAA") else "BBBB")
    with pytest.raises(GrantInvalidError):
        verifier.verify(".".join([head, kind, flipped, footer]))


def test_tampered_footer_is_rejected() -> None:
    _, verifier, _, token = make()
    head, kind, payload, _ = token.split(".")
    import base64

    forged = base64.urlsafe_b64encode(b'{"kid":"k1","extra":1}').rstrip(b"=").decode()
    with pytest.raises(GrantInvalidError):
        verifier.verify(".".join([head, kind, payload, forged]))


def test_token_signed_by_another_key_is_rejected_even_with_a_trusted_kid() -> None:
    _, verifier, clock, _ = make()
    attacker = GrantSigner.generate(key_id="k1")  # same id, different key
    token = issue_grant(
        attacker,
        agent_id="a",
        tenant_id="t",
        capabilities=["trade:paper_execute"],
        max_cost=1e9,
        ttl=timedelta(minutes=5),
        clock=clock,
    ).token
    with pytest.raises(GrantInvalidError):
        verifier.verify(token)


def test_unknown_key_id_is_rejected() -> None:
    _, verifier, clock, _ = make()
    rogue = GrantSigner.generate(key_id="rogue")
    token = issue_grant(
        rogue,
        agent_id="a",
        tenant_id="t",
        capabilities=["a:b"],
        max_cost=1,
        ttl=timedelta(minutes=5),
        clock=clock,
    ).token
    with pytest.raises(UnknownKeyError):
        verifier.verify(token)


def test_token_minted_for_another_purpose_is_rejected() -> None:
    """Same signing key, different implicit assertion: must not verify as a grant."""
    signer, verifier, _, _ = make()
    other_purpose = pyseto.encode(
        signer._key,
        b'{"typ":"keelgate.grant.v1"}',
        footer=b'{"kid":"k1"}',
        implicit_assertion=b"some-other-service",
    ).decode()
    with pytest.raises(GrantInvalidError):
        verifier.verify(other_purpose)


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "not-a-token",
        "v4.public.",
        "v4.local.abc.def",
        "v2.public.abc.def",
        "a.b.c.d.e",
        "v4.public.AAAA.AAAA",
    ],
)
def test_garbage_tokens_are_rejected(garbage: str) -> None:
    _, verifier, _, _ = make()
    with pytest.raises(GrantInvalidError):
        verifier.verify(garbage)


def test_non_string_token_is_rejected() -> None:
    _, verifier, _, _ = make()
    with pytest.raises(GrantInvalidError):
        verifier.verify(None)  # type: ignore[arg-type]


def _forged(signer: GrantSigner, clock: Clock, **claim_overrides: Any) -> str:
    """Correctly signed, but with claims no honest issuer would produce."""
    now = clock.now
    claims: dict[str, Any] = {
        "typ": "keelgate.grant.v1",
        "jti": "j1",
        "iss": "keelgate",
        "kid": signer.key_id,
        "sub": "a1",
        "tenant": "t1",
        "caps": ["a:b"],
        "budget": {"max_cost": 1},
        "iat": now.isoformat(),
        "nbf": now.isoformat(),
        "exp": (now + timedelta(minutes=5)).isoformat(),
    }
    claims.update(claim_overrides)
    return signer.sign(claims)


@pytest.mark.parametrize(
    "overrides",
    [
        {"typ": "something.else"},
        {"caps": []},
        {"caps": ["trade:*"]},
        {"caps": "a:b"},
        {"tenant": ""},
        {"tenant": "has space"},
        {"sub": "../x"},
        {"budget": {"max_cost": -1}},
        {"budget": {}},
        {"exp": "not-a-time"},
        {"exp": "2026-10-05T14:30:00"},  # no timezone
        {"kid": "different"},
    ],
)
def test_validly_signed_but_malformed_claims_are_rejected(overrides: dict[str, Any]) -> None:
    signer, verifier, clock, _ = make()
    with pytest.raises(GrantInvalidError):
        verifier.verify(_forged(signer, clock, **overrides))


def test_a_signed_grant_longer_than_max_ttl_is_rejected() -> None:
    signer, verifier, clock, _ = make()
    token = _forged(signer, clock, exp=(clock.now + timedelta(days=30)).isoformat())
    with pytest.raises(GrantInvalidError, match="lifetime"):
        verifier.verify(token)


def test_a_grant_that_expires_before_it_starts_is_rejected() -> None:
    signer, verifier, clock, _ = make()
    token = _forged(signer, clock, exp=(clock.now - timedelta(minutes=1)).isoformat())
    with pytest.raises(GrantInvalidError):
        verifier.verify(token)


# ----------------------------------------------------------------- revocation


def test_revoked_grant_is_rejected() -> None:
    clock = Clock()
    signer = GrantSigner.generate()
    revocations = InMemoryRevocationList()
    verifier = GrantVerifier({"k1": signer.public_key_pem()}, clock=clock, revocations=revocations)
    issued = issue_grant(
        signer,
        agent_id="a",
        tenant_id="t",
        capabilities=["a:b"],
        max_cost=1,
        ttl=timedelta(minutes=5),
        clock=clock,
    )
    verifier.verify(issued.token)
    revocations.revoke(issued.grant.grant_id)
    with pytest.raises(GrantRevokedError):
        verifier.verify(issued.token)


def test_verifier_needs_a_key() -> None:
    with pytest.raises(ValueError, match="at least one"):
        GrantVerifier({})


def test_verifier_can_hold_several_keys_for_rotation() -> None:
    clock = Clock()
    old, new = GrantSigner.generate(key_id="old"), GrantSigner.generate(key_id="new")
    verifier = GrantVerifier(
        {"old": old.public_key_pem(), "new": new.public_key_pem()}, clock=clock
    )
    for signer in (old, new):
        token = issue_grant(
            signer,
            agent_id="a",
            tenant_id="t",
            capabilities=["a:b"],
            max_cost=1,
            ttl=timedelta(minutes=1),
            clock=clock,
        ).token
        assert verifier.verify(token).key_id == signer.key_id


# ----------------------------------------------------------- secrets / repr


def test_token_and_private_key_do_not_leak_through_repr() -> None:
    signer = GrantSigner.generate()
    issued = issue_grant(
        signer,
        agent_id="a",
        tenant_id="t",
        capabilities=["a:b"],
        max_cost=1,
        ttl=timedelta(minutes=1),
    )
    assert issued.token not in repr(issued)
    assert "PRIVATE" not in repr(signer)
    assert "PRIVATE" not in signer.public_key_pem().decode()


def test_public_key_cannot_sign() -> None:
    signer = GrantSigner.generate()
    with pytest.raises(Exception):  # noqa: B017 - any failure to sign is the point
        GrantSigner(signer.public_key_pem(), key_id="k1").sign({"a": 1})


# --------------------------------------------------------------------- budget


def test_budget_reserve_release_and_limit() -> None:
    ledger = InMemoryBudgetLedger()
    assert ledger.try_reserve("g", 6, 10)
    assert ledger.try_reserve("g", 4, 10)  # exactly at the limit
    assert not ledger.try_reserve("g", 0.01, 10)
    assert ledger.spent("g") == 10
    ledger.release("g", 4)
    assert ledger.try_reserve("g", 4, 10)


def test_budget_is_tracked_per_grant() -> None:
    ledger = InMemoryBudgetLedger()
    assert ledger.try_reserve("g1", 10, 10)
    assert ledger.try_reserve("g2", 10, 10)


@pytest.mark.parametrize("bad", [-1.0, float("nan")])
def test_budget_rejects_impossible_costs(bad: float) -> None:
    assert not InMemoryBudgetLedger().try_reserve("g", bad, 10)


def test_budget_release_never_goes_negative() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.release("g", 50)
    assert ledger.spent("g") == 0


def test_budget_cannot_be_oversubscribed_by_concurrent_callers() -> None:
    ledger = InMemoryBudgetLedger()
    wins: list[bool] = []

    def worker() -> None:
        for _ in range(50):
            wins.append(ledger.try_reserve("g", 1, 100))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 100
    assert ledger.spent("g") == 100


def test_default_clock_issues_usable_grants() -> None:
    """No injected clock: the real UTC clock must produce a verifiable grant."""
    signer = GrantSigner.generate()
    verifier = GrantVerifier({"k1": signer.public_key_pem()})
    token = issue_grant(
        signer,
        agent_id="a",
        tenant_id="t",
        capabilities=["a:b"],
        max_cost=1,
        ttl=timedelta(minutes=1),
    ).token
    assert verifier.verify(token).expires_at > datetime.now(UTC)
    assert MARKET_OPEN.year == 2026  # fixture sanity: tests use a fixed instant, not the wall clock
