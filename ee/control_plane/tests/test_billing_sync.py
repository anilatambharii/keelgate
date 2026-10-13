"""sync-stripe, the local invoice estimate, the CLI, and the test-mode-only guarantee."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

import pytest
import stripe

from keelgate_cloud import metering
from keelgate_cloud.billing import cli
from keelgate_cloud.billing.client import make_client
from keelgate_cloud.billing.estimate import estimate_invoice
from keelgate_cloud.billing.sync import sync_stripe
from keelgate_cloud.config import ConfigError, Settings
from keelgate_cloud.db import Database
from keelgate_cloud.plans import PlanCatalogue, default_catalogue
from tests.harness import SECRET, Org
from tests.test_billing_support import MOCK_KEY, MOCK_URL, RecordingHttp, recording_client


def obj(value: Any) -> Any:
    """dict -> attribute-style object, like the SDK's response objects."""
    if isinstance(value, dict):
        return SimpleNamespace(**{k: v if k == "metadata" else obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [obj(v) for v in value]
    return value


class InMemoryStripe:
    """Just enough of Stripe (products, prices, meters) to prove the sync converges."""

    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.meters: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.v1 = SimpleNamespace(
            products=SimpleNamespace(
                retrieve=self.retrieve_product,
                create=self.create_product,
                update=self.update_product,
            ),
            prices=SimpleNamespace(
                list=self.list_prices, create=self.create_price, update=self.update_price
            ),
            billing=SimpleNamespace(
                meters=SimpleNamespace(list=self.list_meters, create=self.create_meter)
            ),
        )

    def retrieve_product(self, pid: str) -> Any:
        if pid not in self.products:
            raise stripe.InvalidRequestError("No such product", "id", code="resource_missing")
        return obj(self.products[pid])

    def create_product(self, params: dict[str, Any]) -> Any:
        self.calls.append("product.create")
        self.products[params["id"]] = {"active": True, "description": None, **params}
        return obj(self.products[params["id"]])

    def update_product(self, pid: str, params: dict[str, Any]) -> Any:
        self.calls.append("product.update")
        self.products[pid].update(params)
        return obj(self.products[pid])

    def list_prices(self, params: dict[str, Any]) -> Any:
        keys = params.get("lookup_keys", [])
        found = [p for p in self.prices.values() if p["active"] and p["lookup_key"] in keys]
        return obj({"data": found[: params.get("limit", 10)]})

    def create_price(self, params: dict[str, Any]) -> Any:
        self.calls.append("price.create")
        pid = f"price_{len(self.prices) + 1}"
        if params.get("transfer_lookup_key"):
            for other in self.prices.values():
                if other["lookup_key"] == params["lookup_key"]:
                    other["lookup_key"] = None
        recurring = {
            "interval_count": 1,
            "usage_type": "licensed",
            "meter": None,
            **params["recurring"],
        }
        price = {"id": pid, "active": True, "currency": params["currency"], **params}
        price["recurring"] = recurring
        price.setdefault("unit_amount", None)
        self.prices[pid] = price
        return obj(price)

    def update_price(self, pid: str, params: dict[str, Any]) -> Any:
        self.calls.append("price.update")
        self.prices[pid].update(params)
        return obj(self.prices[pid])

    def list_meters(self, _params: dict[str, Any]) -> Any:
        return obj({"data": list(self.meters.values())})

    def create_meter(self, params: dict[str, Any]) -> Any:
        self.calls.append("meter.create")
        self.meters["mtr_1"] = {"id": "mtr_1", **params}
        return obj(self.meters["mtr_1"])

    def active_price(self, key: str) -> dict[str, Any] | None:
        found = [p for p in self.prices.values() if p["active"] and p["lookup_key"] == key]
        assert len(found) <= 1
        return found[0] if found else None


def catalogue_with(**team: Any) -> PlanCatalogue:
    base = default_catalogue()
    plans = {i: base.get(i) for i in base.ids()}
    plans["team"] = plans["team"].model_copy(update=team)
    return PlanCatalogue(plans)


def verbs(actions: list[Any]) -> list[tuple[str, str, str]]:
    return [(a.verb, a.what, a.name) for a in actions]


# ------------------------------------------------------------------------------------ sync


def test_first_sync_creates_meter_products_and_base_prices_but_no_overage() -> None:
    fake = InMemoryStripe()
    actions = sync_stripe(fake, default_catalogue())
    assert ("created", "meter", "keelgate_gated_actions") in verbs(actions)
    assert sorted(fake.products) == ["keelgate_business", "keelgate_team"]  # no free/contract plans
    assert fake.products["keelgate_team"]["metadata"] == {"keelgate_plan": "team"}
    team = fake.active_price("keelgate_team_monthly")
    assert team is not None
    assert (team["unit_amount"], team["currency"], team["recurring"]["interval"]) == (
        49900,
        "usd",
        "month",
    )
    business = fake.active_price("keelgate_business_monthly")
    assert business is not None and business["unit_amount"] == 250000
    assert fake.active_price("keelgate_team_overage") is None  # overage: block -> no invented price
    assert len(fake.prices) == 2


def test_rerunning_changes_nothing() -> None:
    fake = InMemoryStripe()
    sync_stripe(fake, default_catalogue())
    fake.calls.clear()
    second = sync_stripe(fake, default_catalogue())
    assert fake.calls == []
    assert {a.verb for a in second} == {"unchanged"}


def test_a_changed_amount_makes_a_new_price_moves_the_key_and_archives_the_old_one() -> None:
    fake = InMemoryStripe()
    sync_stripe(fake, default_catalogue())
    old = fake.active_price("keelgate_team_monthly")
    assert old is not None
    actions = sync_stripe(fake, catalogue_with(price_monthly_cents=59900))
    new = fake.active_price("keelgate_team_monthly")
    assert new is not None and new["id"] != old["id"] and new["unit_amount"] == 59900
    assert old["active"] is False and old["lookup_key"] is None
    assert ("archived", "price", "keelgate_team_monthly") in verbs(actions)
    fake.calls.clear()
    sync_stripe(fake, catalogue_with(price_monthly_cents=59900))
    assert fake.calls == []  # converged


def test_a_renamed_plan_updates_its_product() -> None:
    fake = InMemoryStripe()
    sync_stripe(fake, default_catalogue())
    actions = sync_stripe(fake, catalogue_with(description="New words."))
    assert ("updated", "product", "keelgate_team") in verbs(actions)
    assert fake.products["keelgate_team"]["description"] == "New words."


def test_a_numeric_overage_makes_a_graduated_metered_price() -> None:
    fake = InMemoryStripe()
    cat = catalogue_with(overage=0.05)
    sync_stripe(fake, cat)
    price = fake.active_price("keelgate_team_overage")
    assert price is not None
    assert price["billing_scheme"] == "tiered" and price["tiers_mode"] == "graduated"
    assert price["tiers"] == [
        {"up_to": 1_000_000, "unit_amount": 0},  # the allowance is free
        {"up_to": "inf", "unit_amount_decimal": "0.05"},
    ]
    assert price["recurring"]["usage_type"] == "metered"
    assert price["recurring"]["meter"] == "mtr_1"
    fake.calls.clear()
    sync_stripe(fake, cat)
    assert fake.calls == []
    sync_stripe(fake, catalogue_with(overage=0.1))  # a new rate -> a new price
    assert fake.active_price("keelgate_team_overage")["tiers"][1]["unit_amount_decimal"] == "0.1"  # type: ignore[index]  # active_price is Optional; just created
    sync_stripe(fake, default_catalogue())  # back to a hard stop: the overage price is archived
    assert fake.active_price("keelgate_team_overage") is None


def test_a_numeric_overage_without_an_allowance_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="included_gated_actions"):
        sync_stripe(InMemoryStripe(), catalogue_with(included_gated_actions=None, overage=0.1))


# ------------------------------------------- the same requests, checked by stripe-mock's spec


class ForceCreate:
    """A real SDK client (stripe-mock) that finds nothing existing, so every create path runs."""

    def __init__(self, real: Any) -> None:
        def missing(*_a: Any, **_k: Any) -> Any:
            raise stripe.InvalidRequestError("No such product", "id", code="resource_missing")

        empty = SimpleNamespace(data=[])
        v1 = real.v1
        self.v1 = SimpleNamespace(
            products=SimpleNamespace(
                retrieve=missing, create=v1.products.create, update=v1.products.update
            ),
            prices=SimpleNamespace(
                list=lambda *_a, **_k: empty, create=v1.prices.create, update=v1.prices.update
            ),
            billing=SimpleNamespace(
                meters=SimpleNamespace(
                    list=lambda *_a, **_k: empty, create=v1.billing.meters.create
                )
            ),
        )


def test_every_create_request_is_valid_against_stripes_spec() -> None:
    real, http = recording_client()
    actions = sync_stripe(ForceCreate(real), catalogue_with(overage=0.05))
    assert [a.verb for a in actions].count("created") == 1 + 2 + 3  # meter, 2 products, 3 prices
    meter = dict(parse_qsl(str(http.to("/v1/billing/meters")[0]["data"])))
    assert meter["event_name"] == "keelgate_gated_actions"
    assert meter["default_aggregation[formula]"] == "sum"
    prices = [
        dict(parse_qsl(str(r["data"]))) for r in http.to("/v1/prices") if r["method"] == "post"
    ]
    overage = next(p for p in prices if p.get("lookup_key") == "keelgate_team_overage")
    assert overage["tiers[1][up_to]"] == "inf"
    assert overage["recurring[usage_type]"] == "metered"
    assert overage["transfer_lookup_key"] == "true"


def test_update_and_archive_requests_are_valid_against_stripes_spec() -> None:
    real, http = recording_client()
    # stripe-mock always returns its canned product and price, which differ from ours:
    # the sync therefore updates the product and replaces the price, archiving the old one.
    actions = sync_stripe(real, default_catalogue())
    kinds = {a.verb for a in actions}
    assert {"updated", "created", "archived"} <= kinds
    assert any(
        r["method"] == "post" and "/v1/products/keelgate_team" in r["url"] for r in http.sent
    )


def test_sync_against_a_dead_stripe_fails_loudly() -> None:
    dead = stripe.StripeClient(
        MOCK_KEY, base_addresses={"api": "http://127.0.0.1:9"}, max_network_retries=0
    )
    with pytest.raises(stripe.APIConnectionError):
        sync_stripe(dead, default_catalogue())


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, pg_dsns: dict[str, str]) -> None:
    monkeypatch.chdir(tmp_path)  # no stray dev .env
    monkeypatch.setenv("KEELGATE_CP_DATABASE_URL", pg_dsns["app"])
    monkeypatch.setenv("KEELGATE_CP_SECRET_KEY", SECRET)
    monkeypatch.setenv("KEELGATE_CP_STRIPE_API_KEY", MOCK_KEY)
    monkeypatch.setenv("KEELGATE_CP_STRIPE_API_BASE", MOCK_URL)


def test_the_cli_syncs_and_prints_what_it_did(
    cli_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["sync-stripe"]) == 0
    out = capsys.readouterr().out
    assert "keelgate_gated_actions" in out and "keelgate_team_monthly" in out
    assert "keelgate_business_monthly" in out


def test_the_cli_reports_usage(
    cli_env: None, capsys: pytest.CaptureFixture[str], db: Database
) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from tests.test_billing_support import billing_app, link  # noqa: PLC0415

    client = TestClient(billing_app(db))
    org = Org(client)
    link(db, org, customer="cus_test_cli", sub="sub_test_cli")
    with db.tx(org.id) as conn:
        metering.record(conn, org.id, metering.GATED_ACTIONS, 4)
    assert cli.main(["report-usage"]) == 0
    assert f"org {org.id}" in capsys.readouterr().out
    assert cli.main(["report-usage", "--period", "2026-13"]) == 2


def test_the_cli_rejects_unknown_commands_and_a_missing_key(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["nope"]) == 2
    monkeypatch.delenv("KEELGATE_CP_STRIPE_API_KEY")
    assert cli.main(["sync-stripe"]) == 2


def test_the_cli_refuses_a_live_key(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEELGATE_CP_STRIPE_API_KEY", "sk_live_" + "x" * 24)
    assert cli.main(["sync-stripe"]) == 2


# ----------------------------------------------------------------- test mode only


def test_the_server_refuses_a_live_key() -> None:
    live = "sk_live_" + "x" * 24  # pragma: allowlist secret
    restricted = "rk_live_" + "x" * 24  # pragma: allowlist secret
    with pytest.raises(ConfigError, match="non-test Stripe key"):
        Settings(database_url="x", secret_key=SECRET, stripe_api_key=live)
    with pytest.raises(ConfigError):
        Settings(database_url="x", secret_key=SECRET, stripe_api_key=restricted)
    assert Settings(database_url="x", secret_key=SECRET, stripe_api_key=MOCK_KEY)


def test_no_client_is_built_without_a_key_or_with_a_live_one() -> None:
    assert make_client(Settings(database_url="x", secret_key=SECRET)) is None
    live = Settings(
        database_url="x",
        secret_key=SECRET,
        stripe_api_key="sk_live_" + "x" * 24,  # pragma: allowlist secret
        allow_live_stripe=True,
    )
    object.__setattr__(live, "allow_live_stripe", False)  # frozen: simulate a bypassed check
    with pytest.raises(ValueError, match="non-test"):
        make_client(live)


def test_the_client_is_pointed_at_the_configured_base() -> None:
    client = make_client(
        Settings(
            database_url="x", secret_key=SECRET, stripe_api_key=MOCK_KEY, stripe_api_base=MOCK_URL
        )
    )
    assert client is not None
    assert isinstance(RecordingHttp, type)  # imported for the other modules' sake
    assert client.v1.products.list({"limit": 1}).data  # reached stripe-mock on localhost


# ------------------------------------------------------------------ local estimate


TEAM = default_catalogue().get("team")
BUSINESS = default_catalogue().get("business")
PERIOD = "2026-10"


def test_team_base_is_499_dollars_with_nothing_used() -> None:
    est = estimate_invoice(TEAM, PERIOD, 0)
    assert est["source"] == "local" and est["currency"] == "usd"
    assert est["subtotal_cents"] == est["total_cents"] == 49900
    assert est["period"] == {"id": PERIOD, "start": "2026-10-01", "end": "2026-11-01"}
    assert [(ln["quantity"], ln["amount_cents"]) for ln in est["lines"]] == [(1, 49900), (0, 0)]


def test_usage_within_the_million_included_adds_nothing() -> None:
    est = estimate_invoice(TEAM, PERIOD, 999_999)
    assert est["total_cents"] == 49900
    assert est["lines"][1]["quantity"] == 999_999
    assert est["notes"] == []


def test_at_and_beyond_the_cap_the_plan_blocks_and_never_bills_overage() -> None:
    at_cap = estimate_invoice(TEAM, PERIOD, 1_000_000)
    assert at_cap["total_cents"] == 49900 and at_cap["notes"]
    beyond = estimate_invoice(TEAM, PERIOD, 1_500_000)
    assert beyond["total_cents"] == 49900
    assert beyond["lines"][1]["quantity"] == 1_000_000  # only the included actions happened
    assert len(beyond["lines"]) == 2


def test_with_a_numeric_overage_only_the_excess_is_billed() -> None:
    plan = TEAM.model_copy(update={"overage": 0.1})  # a tenth of a cent per action
    within = estimate_invoice(plan, PERIOD, 1_000_000)
    assert within["total_cents"] == 49900
    beyond = estimate_invoice(plan, PERIOD, 1_200_000)
    line = beyond["lines"][2]
    assert (line["quantity"], line["unit_amount_cents"], line["amount_cents"]) == (
        200_000,
        0.1,
        20_000,
    )
    assert beyond["total_cents"] == beyond["subtotal_cents"] == 69_900


def test_business_has_no_cap_and_a_flat_base() -> None:
    est = estimate_invoice(BUSINESS, PERIOD, 50_000_000)
    assert est["total_cents"] == 250_000
    assert est["lines"][1]["quantity"] == 50_000_000
