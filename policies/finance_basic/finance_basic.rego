# finance_basic: deny-by-default gate for AI-proposed financial actions.
#
# Paper-only enforcement, a restricted-symbol list, a per-action notional cap, a
# daily exposure cap, trading hours, and approval tiers for large orders.
#
# Design rules every rule below follows:
#   1. Conditions are written POSITIVELY (limits_ok, market_open, ...) and the
#      deny rule is `not <positive>`. An undefined or malformed field makes the
#      positive rule undefined, so the action is denied rather than let through.
#   2. All inputs are untrusted-shaped. Types are checked before comparing.
#   3. Reasons never echo model-supplied strings (symbols, free text). They go
#      to humans and back to the model, so they carry fixed wording only.
#
# Input contract (built by keelgate, never by the model):
#   input.action   {tool, side_effect, capability, args}
#   input.actor    {agent_id, tenant_id, grant_id}
#   input.resource {symbol, notional}                  derived from tool args
#   input.context  {as_of, execution_mode, exposure, limits, positions}
#     as_of   RFC3339 with a numeric offset (for example +00:00)
#     limits  max_notional_per_action, max_daily_exposure, restricted_symbols,
#             approval_one_click_notional, approval_explicit_notional,
#             trading_hours {tz, open_minute, close_minute}
#
# Output: data.keelgate.finance_basic.decision
#   {effect: ALLOW | DENY | REQUIRE_APPROVAL, reasons: [string], approval_tier}

# METADATA
# title: finance_basic
# description: Deny-by-default gate for AI-proposed financial actions.
package keelgate.finance_basic

import rego.v1

modes := {"paper", "simulation"}

trade_capabilities := {"trade:propose", "trade:paper_execute"}

weekend := {"Saturday", "Sunday"}

# ---------------------------------------------------------------- decision

default decision := {
	"effect": "DENY",
	"reasons": ["no rule allows this action"],
	"approval_tier": null,
}

decision := {
	"effect": "DENY",
	"reasons": sort(deny),
	"approval_tier": null,
} if {
	count(deny) > 0
}

decision := {
	"effect": "REQUIRE_APPROVAL",
	"reasons": [approval_reason],
	"approval_tier": approval_tier,
} if {
	count(deny) == 0
	approval_tier
}

decision := {
	"effect": "ALLOW",
	"reasons": ["within configured limits"],
	"approval_tier": null,
} if {
	count(deny) == 0
	shape_ok
	not approval_tier
}

# ------------------------------------------------------------ what we know

# The only (capability, side effect) pairs this pack understands. Everything
# else is denied, including capabilities added later and not yet reviewed here.
shape_ok if {
	input.action.capability == "market_data:read"
	input.action.side_effect == "READ"
}

shape_ok if {
	input.action.capability == "trade:propose"
	input.action.side_effect == "PROPOSE"
}

shape_ok if {
	input.action.capability == "trade:paper_execute"
	input.action.side_effect == "WRITE"
}

shape_ok if {
	input.action.capability == "report:write"
	input.action.side_effect == "WRITE"
}

is_trade if input.action.capability in trade_capabilities

is_trade_write if {
	is_trade
	input.action.side_effect == "WRITE"
}

# ------------------------------------------------------- positive conditions

limits_ok if {
	limits := input.context.limits
	is_number(limits.max_notional_per_action)
	limits.max_notional_per_action > 0
	is_number(limits.max_daily_exposure)
	limits.max_daily_exposure > 0
	is_array(limits.restricted_symbols)
	is_number(limits.approval_one_click_notional)
	is_number(limits.approval_explicit_notional)
	limits.approval_one_click_notional <= limits.approval_explicit_notional
}

# `not x in set` does NOT deny when x is undefined, so the mode is checked
# positively and the deny rule negates the whole rule instead.
mode_ok if input.context.execution_mode in modes

symbol_ok if {
	is_string(input.resource.symbol)
	count(input.resource.symbol) > 0
}

notional_ok if {
	is_number(input.resource.notional)
	input.resource.notional > 0
}

within_action_limit if input.resource.notional <= input.context.limits.max_notional_per_action

within_daily_limit if {
	is_number(input.context.exposure.daily_notional)
	input.context.exposure.daily_notional >= 0
	input.context.exposure.daily_notional + input.resource.notional <= input.context.limits.max_daily_exposure
}

# Evaluated against as_of, never the wall clock, so a replay decides the same way.
market_open if {
	hours := input.context.limits.trading_hours
	ns := time.parse_rfc3339_ns(input.context.as_of)
	[hour, minute, _] := time.clock([ns, hours.tz])
	minute_of_day := (hour * 60) + minute
	minute_of_day >= hours.open_minute
	minute_of_day < hours.close_minute
	weekday := time.weekday([ns, hours.tz])
	not weekend[weekday]
}

restricted_symbols := {upper(s) |
	some s in input.context.limits.restricted_symbols
	is_string(s)
}

# --------------------------------------------------------------------- deny

deny contains "action is not permitted by finance_basic" if not shape_ok

# v1 is paper/simulation only. This is the policy-side half of that rule; the
# gateway refuses non-paper modes before it ever asks.
deny contains "live execution is forbidden: mode must be paper or simulation" if not mode_ok

deny contains "trading limits are missing or malformed" if {
	is_trade
	not limits_ok
}

deny contains "resource.symbol is missing or malformed" if {
	is_trade
	not symbol_ok
}

deny contains "resource.notional must be a positive number" if {
	is_trade
	not notional_ok
}

deny contains "symbol is on the restricted list" if {
	is_trade
	symbol_ok
	upper(input.resource.symbol) in restricted_symbols
}

deny contains "notional exceeds the per-action limit" if {
	is_trade
	limits_ok
	notional_ok
	not within_action_limit
}

deny contains "daily exposure limit would be exceeded" if {
	is_trade_write
	limits_ok
	notional_ok
	not within_daily_limit
}

deny contains "outside trading hours" if {
	is_trade_write
	not market_open
}

# ----------------------------------------------------------------- approval

approval_tier := "EXPLICIT_SIGNOFF" if {
	is_trade_write
	input.resource.notional > input.context.limits.approval_explicit_notional
} else := "ONE_CLICK" if {
	is_trade_write
	input.resource.notional > input.context.limits.approval_one_click_notional
}

approval_reason := "notional exceeds the explicit sign-off threshold" if {
	approval_tier == "EXPLICIT_SIGNOFF"
}

approval_reason := "notional exceeds the auto-approval threshold" if {
	approval_tier == "ONE_CLICK"
}
