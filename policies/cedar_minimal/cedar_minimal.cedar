// cedar_minimal: a SUBSET of finance_basic, to show the Cedar backend end to end.
//
// Not ported, on purpose:
//   - trading hours (Cedar has no timezone arithmetic; compute it upstream or use Rego)
//   - report:write and market_data:read
// Anything not explicitly permitted below is denied: Cedar is deny-by-default, and
// the adapter additionally treats any evaluation error as a denial.
//
// Amounts arrive as whole units, rounded UP by the adapter (Cedar has no floats).

// Paper or simulation only, a known capability/side-effect pair, and within limits.
@id("permit_paper_trades_within_limits")
permit (principal, action == Action::"invoke", resource)
when {
  ["paper", "simulation"].contains(context.execution_mode) &&
  ((resource.capability == "trade:propose" && resource.side_effect == "PROPOSE") ||
   (resource.capability == "trade:paper_execute" && resource.side_effect == "WRITE")) &&
  resource.notional > 0 &&
  resource.notional <= context.max_notional_per_action &&
  (resource.side_effect == "PROPOSE" ||
   context.daily_notional + resource.notional <= context.max_daily_exposure)
};

// A forbid always beats a permit.
@id("forbid_restricted_symbols")
forbid (principal, action == Action::"invoke", resource)
when { context.restricted_symbols.contains(resource.symbol) };

// Approval tiers for WRITE actions. Consulted only once "invoke" is permitted.
@id("explicit_signoff_above_threshold")
permit (principal, action == Action::"explicit_signoff", resource)
when {
  resource.side_effect == "WRITE" &&
  resource.notional > context.approval_explicit_notional
};

@id("one_click_above_threshold")
permit (principal, action == Action::"one_click", resource)
when {
  resource.side_effect == "WRITE" &&
  resource.notional > context.approval_one_click_notional
};
