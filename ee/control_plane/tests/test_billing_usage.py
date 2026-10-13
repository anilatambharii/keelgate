"""Metered usage reporting: only the new increment, once, even across retries and failures."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

import pytest
import stripe
from fastapi.testclient import TestClient

from keelgate_cloud import metering
from keelgate_cloud.billing.usage import billed_orgs, report_org, report_usage
from keelgate_cloud.db import Database
from keelgate_cloud.plans import default_catalogue
from tests.harness import Org, make_app
from tests.test_billing_support import link, new_ids, recording_client

PERIOD = metering.period_of(metering.today())
CATALOGUE = default_catalogue()


class FakeMeterApi:
    """A Stripe stand-in that remembers meter events and can fail or dawdle on demand."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail = fail
        self.delay = delay
        self.v1 = SimpleNamespace(billing=SimpleNamespace(meter_events=self))

    def create(self, params: dict[str, Any]) -> Any:
        time.sleep(self.delay)
        if self.fail:
            raise stripe.APIConnectionError("stripe is down")
        self.events.append(params)
        return SimpleNamespace(identifier=params["identifier"])

    def for_org(self, org: Org) -> list[dict[str, Any]]:
        return [e for e in self.events if e["identifier"].startswith(f"{org.id}:")]


@pytest.fixture(scope="module")
def client(db: Database) -> TestClient:
    return TestClient(make_app(db))


def billed_org(client: TestClient, db: Database, used: int) -> Org:
    org = Org(client)
    ids = new_ids()
    link(db, org, customer=ids["CUSTOMER"], sub=ids["SUB"])
    add_usage(db, org, used)
    return org


def add_usage(db: Database, org: Org, n: int) -> None:
    with db.tx(org.id) as conn:
        metering.record(conn, org.id, metering.GATED_ACTIONS, n)


def reported(db: Database, org: Org) -> int:
    with db.tx(org.id) as conn:
        row = conn.execute(
            "SELECT reported FROM usage_reports WHERE period = %s AND metric = %s",
            (PERIOD, metering.GATED_ACTIONS),
        ).fetchone()
    return int(row["reported"]) if row else 0


def test_sends_the_usage_then_only_the_increment(client: TestClient, db: Database) -> None:
    org = billed_org(client, db, 5)
    api = FakeMeterApi()
    first = report_org(db, api, CATALOGUE, org.id, PERIOD)
    assert (first.sent, first.reported_total) == (5, 5)
    event = api.for_org(org)[0]
    assert event["event_name"] == "keelgate_gated_actions"
    assert event["identifier"] == f"{org.id}:{PERIOD}:5"
    assert event["payload"] == {
        "stripe_customer_id": event["payload"]["stripe_customer_id"],
        "value": "5",
    }

    again = report_org(db, api, CATALOGUE, org.id, PERIOD)  # nothing new: nothing sent
    assert again.sent == 0
    assert len(api.for_org(org)) == 1

    add_usage(db, org, 3)
    third = report_org(db, api, CATALOGUE, org.id, PERIOD)
    assert third.sent == 3
    assert [e["payload"]["value"] for e in api.for_org(org)] == ["5", "3"]
    assert api.for_org(org)[1]["identifier"] == f"{org.id}:{PERIOD}:8"
    assert reported(db, org) == 8


def test_a_stripe_failure_rolls_the_claim_back_and_the_retry_sends_it_once(
    client: TestClient, db: Database
) -> None:
    org = billed_org(client, db, 7)
    down = FakeMeterApi(fail=True)
    with pytest.raises(stripe.APIConnectionError):
        report_org(db, down, CATALOGUE, org.id, PERIOD)
    assert reported(db, org) == 0  # not claimed: the failure left no trace
    assert down.for_org(org) == []

    up = FakeMeterApi()
    assert report_org(db, up, CATALOGUE, org.id, PERIOD).sent == 7
    assert report_org(db, up, CATALOGUE, org.id, PERIOD).sent == 0
    assert len(up.for_org(org)) == 1
    assert reported(db, org) == 7


def test_two_reporters_at_once_send_the_usage_once(client: TestClient, db: Database) -> None:
    org = billed_org(client, db, 11)
    api = FakeMeterApi(delay=0.3)  # slow enough that both are inside the window together
    results: list[int] = []

    def run() -> None:
        results.append(report_org(db, api, CATALOGUE, org.id, PERIOD).sent)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [0, 11]
    assert [e["payload"]["value"] for e in api.for_org(org)] == ["11"]


def test_a_failure_for_one_org_does_not_stop_the_others(client: TestClient, db: Database) -> None:
    good = billed_org(client, db, 2)
    bad = billed_org(client, db, 4)

    class Flaky(FakeMeterApi):
        def create(self, params: dict[str, Any]) -> Any:
            if params["identifier"].startswith(f"{bad.id}:"):
                raise stripe.APIConnectionError("boom")
            return super().create(params)

    api = Flaky()
    by_org = {r.org_id: r for r in report_usage(db, api, CATALOGUE, PERIOD)}
    assert by_org[good.id].sent == 2 and by_org[good.id].error is None
    assert by_org[bad.id].error == "APIConnectionError"
    assert reported(db, bad) == 0


def test_only_stripe_billed_orgs_with_a_metered_plan_are_reported(
    client: TestClient, db: Database
) -> None:
    billed = billed_org(client, db, 3)
    unsubscribed = Org(client)  # still on its internal trial, no Stripe subscription
    add_usage(db, unsubscribed, 9)
    business = billed_org(client, db, 4)
    with db.tx(business.id) as conn:
        conn.execute("UPDATE orgs SET plan = 'business'")  # no action cap, so no meter
    ids = billed_orgs(db)
    assert billed.id in ids and business.id in ids
    assert unsubscribed.id not in ids

    api = FakeMeterApi()
    report_usage(db, api, CATALOGUE, PERIOD)
    assert len(api.for_org(billed)) == 1
    assert api.for_org(unsubscribed) == []
    assert api.for_org(business) == []


def test_the_sweep_policy_shows_nothing_without_the_switch(
    client: TestClient, db: Database
) -> None:
    billed_org(client, db, 1)
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM orgs").fetchone() == {"n": 0}


def test_usage_is_not_mixed_across_periods(client: TestClient, db: Database) -> None:
    org = billed_org(client, db, 6)
    api = FakeMeterApi()
    report_org(db, api, CATALOGUE, org.id, PERIOD)
    other = "2020-01"
    assert report_org(db, api, CATALOGUE, org.id, other).sent == 0  # nothing used that month
    assert len(api.for_org(org)) == 1


def test_the_meter_event_request_is_valid_for_stripe(client: TestClient, db: Database) -> None:
    org = billed_org(client, db, 9)
    real, http = recording_client()
    result = report_org(db, real, CATALOGUE, org.id, PERIOD)  # stripe-mock validates the shape
    assert result.sent == 9
    sent = dict(parse_qsl(str(http.to("/v1/billing/meter_events")[0]["data"])))
    assert sent["event_name"] == "keelgate_gated_actions"
    assert sent["payload[value]"] == "9"
    assert sent["identifier"] == f"{org.id}:{PERIOD}:9"
