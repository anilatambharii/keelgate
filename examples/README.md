# Examples

Runnable examples land alongside the capabilities they demonstrate, so this
directory fills up as the harness does. Planned:

- a tool gated by a Rego policy, denied and then approved by a human;
- an `as_of` context build proving no post-cutoff document can leak in;
- a budgeted loop that is interrupted and resumed from its checkpoint;
- verifying an audit chain after a tampered record.

Every example runs against `keelgate.testing.FakeLLM` by default, so none of them
needs an API key, and none of them touches real money.
