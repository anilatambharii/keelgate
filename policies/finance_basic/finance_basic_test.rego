package keelgate.finance_basic_test

import rego.v1

import data.keelgate.finance_basic

# 2026-10-05 is a Monday. In October New York is on EDT (UTC-4), so the market
# opens at 13:30Z and closes at 20:00Z. In January it is EST (UTC-5): 14:30Z.
monday_open := "2026-10-05T14:30:00+00:00"

limits := {
	"max_notional_per_action": 50000,
	"max_daily_exposure": 100000,
	"restricted_symbols": ["TSLA", "gme"],
	"approval_one_click_notional": 10000,
	"approval_explicit_notional": 25000,
	"trading_hours": {"tz": "America/New_York", "open_minute": 570, "close_minute": 960},
}

base := {
	"action": {
		"tool": "trade.paper_execute",
		"side_effect": "WRITE",
		"capability": "trade:paper_execute",
		"args": {},
	},
	"actor": {"agent_id": "agent-1", "tenant_id": "tenant-1", "grant_id": "g-1"},
	"resource": {"symbol": "AAPL", "notional": 5000},
	"context": {
		"as_of": monday_open,
		"execution_mode": "paper",
		"exposure": {"daily_notional": 0},
		"limits": limits,
		"positions": {},
	},
}

with_ctx(overrides) := object.union(base, {"context": overrides})

with_res(overrides) := object.union(base, {"resource": overrides})

at(as_of) := with_ctx({"as_of": as_of})

effect(inp) := e if e := finance_basic.decision.effect with input as inp

reasons(inp) := r if r := finance_basic.decision.reasons with input as inp

tier(inp) := x if x := finance_basic.decision.approval_tier with input as inp

# ------------------------------------------------------------------- allow

test_allow_baseline if effect(base) == "ALLOW"

test_allow_has_no_tier if tier(base) == null

test_allow_at_exact_open if effect(at("2026-10-05T13:30:00+00:00")) == "ALLOW"

test_allow_just_before_close if effect(at("2026-10-05T19:59:00+00:00")) == "ALLOW"

# The same wall-clock hour is a different UTC hour in winter. A naive fixed-UTC
# window gets one of these wrong.
test_allow_at_open_in_winter if effect(at("2026-01-05T14:30:00+00:00")) == "ALLOW"

test_allow_report_write_needs_no_notional if {
	inp := object.union(base, {"action": {"capability": "report:write"}})
	effect(json.remove(inp, ["resource"])) == "ALLOW"
}

test_allow_read_market_data if {
	inp := object.union(base, {"action": {"capability": "market_data:read", "side_effect": "READ"}})
	effect(inp) == "ALLOW"
}

test_allow_propose_outside_hours if {
	inp := object.union(at("2026-10-10T03:00:00+00:00"), {"action": {"capability": "trade:propose", "side_effect": "PROPOSE"}})
	effect(inp) == "ALLOW"
}

# -------------------------------------------------------------- trading hours

test_deny_before_open if effect(at("2026-10-05T13:29:00+00:00")) == "DENY"

test_deny_at_exact_close if effect(at("2026-10-05T20:00:00+00:00")) == "DENY"

test_deny_before_open_in_winter if effect(at("2026-01-05T14:29:00+00:00")) == "DENY"

test_deny_saturday if effect(at("2026-10-10T15:00:00+00:00")) == "DENY"

test_deny_sunday if effect(at("2026-10-11T15:00:00+00:00")) == "DENY"

test_deny_hours_reason if "outside trading hours" in reasons(at("2026-10-10T15:00:00+00:00"))

test_deny_missing_as_of if {
	inp := json.remove(base, ["context/as_of"])
	effect(inp) == "DENY"
}

test_deny_unparseable_as_of if effect(at("next tuesday")) == "DENY"

test_deny_as_of_without_offset if effect(at("2026-10-05T14:30:00")) == "DENY"

test_deny_missing_trading_hours if {
	inp := json.remove(base, ["context/limits/trading_hours"])
	effect(inp) == "DENY"
}

test_deny_unknown_timezone if {
	hours := {"tz": "Mars/Olympus", "open_minute": 570, "close_minute": 960}
	effect(with_ctx({"limits": object.union(limits, {"trading_hours": hours})})) == "DENY"
}

# ------------------------------------------------------------ paper only

test_deny_live_mode if effect(with_ctx({"execution_mode": "live"})) == "DENY"

test_deny_live_mode_reason if {
	"live execution is forbidden: mode must be paper or simulation" in reasons(with_ctx({"execution_mode": "live"}))
}

test_deny_missing_mode if {
	inp := json.remove(base, ["context/execution_mode"])
	effect(inp) == "DENY"
}

test_allow_simulation_mode if effect(with_ctx({"execution_mode": "simulation"})) == "ALLOW"

# ------------------------------------------------------------ restricted list

test_deny_restricted_symbol if effect(with_res({"symbol": "TSLA"})) == "DENY"

test_deny_restricted_symbol_case_insensitive if effect(with_res({"symbol": "tsla"})) == "DENY"

test_deny_restricted_list_entry_lowercase if effect(with_res({"symbol": "GME"})) == "DENY"

test_restricted_reason_does_not_echo_symbol if {
	rs := reasons(with_res({"symbol": "TSLA"}))
	every r in rs {
		not contains(r, "TSLA")
	}
}

test_deny_restricted_even_for_propose if {
	inp := object.union(with_res({"symbol": "TSLA"}), {"action": {"capability": "trade:propose", "side_effect": "PROPOSE"}})
	effect(inp) == "DENY"
}

# ----------------------------------------------------------------- notional

test_deny_over_per_action_limit if effect(with_res({"notional": 50001})) == "DENY"

test_allow_exactly_at_per_action_limit_requires_approval if effect(with_res({"notional": 50000})) == "REQUIRE_APPROVAL"

test_deny_zero_notional if effect(with_res({"notional": 0})) == "DENY"

test_deny_negative_notional if effect(with_res({"notional": -100})) == "DENY"

test_deny_string_notional if effect(with_res({"notional": "5000"})) == "DENY"

test_deny_missing_notional if {
	inp := json.remove(base, ["resource/notional"])
	effect(inp) == "DENY"
}

test_deny_missing_symbol if {
	inp := json.remove(base, ["resource/symbol"])
	effect(inp) == "DENY"
}

# --------------------------------------------------------------------- daily

test_deny_daily_exposure_exceeded if effect(with_ctx({"exposure": {"daily_notional": 96000}})) == "DENY"

test_allow_daily_exposure_exactly_at_limit if effect(with_ctx({"exposure": {"daily_notional": 95000}})) == "ALLOW"

test_deny_missing_exposure if {
	inp := json.remove(base, ["context/exposure"])
	effect(inp) == "DENY"
}

test_deny_negative_exposure if effect(with_ctx({"exposure": {"daily_notional": -1}})) == "DENY"

test_propose_ignores_daily_exposure if {
	inp := object.union(with_ctx({"exposure": {"daily_notional": 99999999}}), {"action": {"capability": "trade:propose", "side_effect": "PROPOSE"}})
	effect(inp) == "ALLOW"
}

# ------------------------------------------------------------------ approval

test_one_click_above_threshold if {
	inp := with_res({"notional": 10001})
	effect(inp) == "REQUIRE_APPROVAL"
	tier(inp) == "ONE_CLICK"
}

test_no_approval_exactly_at_threshold if effect(with_res({"notional": 10000})) == "ALLOW"

test_explicit_signoff_above_threshold if {
	inp := with_res({"notional": 25001})
	effect(inp) == "REQUIRE_APPROVAL"
	tier(inp) == "EXPLICIT_SIGNOFF"
}

test_one_click_exactly_at_explicit_threshold if {
	inp := with_res({"notional": 25000})
	tier(inp) == "ONE_CLICK"
}

# A denial always wins over an approval requirement.
test_deny_beats_approval if {
	inp := object.union(with_res({"symbol": "TSLA", "notional": 30000}), {})
	effect(inp) == "DENY"
	tier(inp) == null
}

# ------------------------------------------------------------ deny-by-default

test_deny_unknown_capability if {
	effect(object.union(base, {"action": {"capability": "wire:transfer"}})) == "DENY"
}

test_deny_capability_with_wrong_side_effect if {
	effect(object.union(base, {"action": {"side_effect": "READ"}})) == "DENY"
}

test_deny_empty_input if effect({}) == "DENY"

test_deny_missing_limits if {
	inp := json.remove(base, ["context/limits"])
	effect(inp) == "DENY"
}

test_deny_malformed_limits if effect(with_ctx({"limits": {"max_notional_per_action": "lots"}})) == "DENY"

test_deny_thresholds_out_of_order if {
	bad := object.union(limits, {"approval_one_click_notional": 30000})
	effect(with_ctx({"limits": bad})) == "DENY"
}

test_deny_reasons_are_sorted_and_stable if {
	inp := with_res({"symbol": "TSLA", "notional": 99999999})
	rs := reasons(inp)
	rs == sort(rs)
}

# ------------------------------------------------------------------ A2A intake

test_allow_a2a_task_intake if {
	inp := object.union(base, {"action": {"capability": "a2a:task_submit", "side_effect": "PROPOSE"}})
	effect(json.remove(inp, ["resource"])) == "ALLOW"
}

test_deny_a2a_task_intake_as_a_write if {
	inp := object.union(base, {"action": {"capability": "a2a:task_submit", "side_effect": "WRITE"}})
	effect(json.remove(inp, ["resource"])) == "DENY"
}

test_deny_a2a_task_intake_in_live_mode if {
	inp := object.union(with_ctx({"execution_mode": "live"}), {"action": {"capability": "a2a:task_submit", "side_effect": "PROPOSE"}})
	effect(json.remove(inp, ["resource"])) == "DENY"
}
