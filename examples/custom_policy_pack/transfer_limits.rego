# transfer_limits: a custom Keelgate policy pack, deny-by-default limits for treasury transfers.
#
# This is the pack the "write your own policy" guide builds. It follows the same rules as the
# shipped finance_basic pack:
#   1. conditions are stated POSITIVELY and the deny rule is `not <positive rule>`, because in
#      Rego `not x in set` does not fire when x is undefined (that was a real fail-open once);
#   2. reasons never echo model-supplied strings;
#   3. everything not explicitly allowed is denied.
#
# Input (built by Keelgate, never by the model):
#   input.action   {tool, side_effect, capability, args}
#   input.resource {destination, amount}      derived from validated tool arguments
#   input.context  {as_of, execution_mode, exposure, limits}

# METADATA
# title: transfer_limits
# description: Deny-by-default limits for treasury transfers.
package acme.transfer_limits

import rego.v1

modes := {"paper", "simulation"}

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
	"reasons": ["amount is above the auto-approval threshold"],
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

# The only (capability, side effect) pairs this pack understands.
shape_ok if {
	input.action.capability == "treasury:balance"
	input.action.side_effect == "READ"
}

shape_ok if {
	input.action.capability == "treasury:transfer"
	input.action.side_effect == "WRITE"
}

is_transfer if input.action.capability == "treasury:transfer"

# ------------------------------------------------------- positive conditions

mode_ok if input.context.execution_mode in modes

limits_ok if {
	limits := input.context.limits
	is_array(limits.allowed_destinations)
	is_number(limits.max_transfer)
	limits.max_transfer > 0
	is_number(limits.max_daily_total)
	limits.max_daily_total >= limits.max_transfer
	is_number(limits.approval_threshold)
	limits.approval_threshold <= limits.max_transfer
}

allowed_destinations := {d |
	some d in input.context.limits.allowed_destinations
	is_string(d)
}

destination_ok if {
	is_string(input.resource.destination)
	input.resource.destination in allowed_destinations
}

amount_ok if {
	is_number(input.resource.amount)
	input.resource.amount > 0
}

within_cap if input.resource.amount <= input.context.limits.max_transfer

within_daily_total if {
	is_number(input.context.exposure.daily_total)
	input.context.exposure.daily_total >= 0
	input.context.exposure.daily_total + input.resource.amount <= input.context.limits.max_daily_total
}

# --------------------------------------------------------------------- deny

deny contains "action is not permitted by transfer_limits" if not shape_ok

deny contains "live execution is forbidden: mode must be paper or simulation" if not mode_ok

deny contains "transfer limits are missing or malformed" if {
	is_transfer
	not limits_ok
}

deny contains "destination is not on the allowlist" if {
	is_transfer
	limits_ok
	not destination_ok
}

deny contains "amount must be a positive number" if {
	is_transfer
	not amount_ok
}

deny contains "amount exceeds the per-transfer cap" if {
	is_transfer
	limits_ok
	amount_ok
	not within_cap
}

deny contains "daily total would be exceeded" if {
	is_transfer
	limits_ok
	amount_ok
	not within_daily_total
}

# ----------------------------------------------------------------- approval

approval_tier := "EXPLICIT_SIGNOFF" if {
	is_transfer
	input.resource.amount > input.context.limits.approval_threshold * 2
} else := "ONE_CLICK" if {
	is_transfer
	input.resource.amount > input.context.limits.approval_threshold
}
