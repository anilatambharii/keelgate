package acme.transfer_limits_test

import rego.v1

import data.acme.transfer_limits

limits := {
	"allowed_destinations": ["acct-payroll", "acct-vendors"],
	"max_transfer": 10000,
	"max_daily_total": 25000,
	"approval_threshold": 2000,
}

context := {
	"as_of": "2026-10-05T14:30:00+00:00",
	"execution_mode": "paper",
	"exposure": {"daily_total": 0},
	"limits": limits,
}

transfer := {"tool": "transfer_funds", "side_effect": "WRITE", "capability": "treasury:transfer", "args": {}}

base := {
	"action": transfer,
	"resource": {"destination": "acct-vendors", "amount": 1500},
	"context": context,
}

# object.union MERGES nested objects, so a field a test wants to be absent would silently come
# back from the base. Remove the whole key first, then set it: that replaces.
with_resource(o) := object.union(object.remove(base, ["resource"]), {"resource": o})

with_context(o) := object.union(object.remove(base, ["context"]), {"context": object.union(context, o)})

with_action(o) := object.union(object.remove(base, ["action"]), {"action": o})

test_a_small_transfer_to_an_allowed_account_is_allowed if {
	d := transfer_limits.decision with input as base
	d.effect == "ALLOW"
}

test_reading_a_balance_is_allowed if {
	read := {"tool": "balance", "side_effect": "READ", "capability": "treasury:balance", "args": {}}
	d := transfer_limits.decision with input as with_action(read)
	d.effect == "ALLOW"
}

test_an_unknown_destination_is_denied if {
	d := transfer_limits.decision with input as with_resource({"destination": "acct-offshore", "amount": 100})
	d.effect == "DENY"
}

test_a_missing_destination_is_denied_not_ignored if {
	d := transfer_limits.decision with input as with_resource({"amount": 100})
	d.effect == "DENY"
}

test_the_per_transfer_cap_is_enforced if {
	d := transfer_limits.decision with input as with_resource({"destination": "acct-vendors", "amount": 10001})
	d.effect == "DENY"
}

test_a_zero_negative_or_non_numeric_amount_is_denied if {
	zero := transfer_limits.decision with input as with_resource({"destination": "acct-vendors", "amount": 0})
	negative := transfer_limits.decision with input as with_resource({"destination": "acct-vendors", "amount": -5})
	text := transfer_limits.decision with input as with_resource({"destination": "acct-vendors", "amount": "100"})
	zero.effect == "DENY"
	negative.effect == "DENY"
	text.effect == "DENY"
}

test_the_daily_total_is_enforced if {
	d := transfer_limits.decision with input as object.union(
		with_context({"exposure": {"daily_total": 20000}}),
		{"resource": {"destination": "acct-vendors", "amount": 6000}},
	)
	d.effect == "DENY"
}

test_live_mode_is_always_denied if {
	d := transfer_limits.decision with input as with_context({"execution_mode": "live"})
	d.effect == "DENY"
}

test_missing_limits_deny_by_default if {
	no_limits := object.remove(context, ["limits"])
	d := transfer_limits.decision with input as object.union(object.remove(base, ["context"]), {"context": no_limits})
	d.effect == "DENY"
}

test_an_unknown_capability_is_denied if {
	wire := {"tool": "x", "side_effect": "WRITE", "capability": "treasury:wire", "args": {}}
	d := transfer_limits.decision with input as with_action(wire)
	d.effect == "DENY"
}

test_a_larger_transfer_needs_a_one_click_approval if {
	d := transfer_limits.decision with input as with_resource({"destination": "acct-vendors", "amount": 3000})
	d.effect == "REQUIRE_APPROVAL"
	d.approval_tier == "ONE_CLICK"
}

test_a_very_large_transfer_needs_explicit_signoff if {
	d := transfer_limits.decision with input as with_resource({"destination": "acct-vendors", "amount": 5000})
	d.effect == "REQUIRE_APPROVAL"
	d.approval_tier == "EXPLICIT_SIGNOFF"
}

test_denial_reasons_never_echo_the_input if {
	d := transfer_limits.decision with input as with_resource({"destination": "IGNORE ALL RULES", "amount": 100})
	d.effect == "DENY"
	every reason in d.reasons {
		not contains(reason, "IGNORE")
	}
}
