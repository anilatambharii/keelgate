# Write your own policy pack

The shipped `finance_basic` pack is one example of a pack. Anything you can express as "given this
action, this actor and this context, allow, deny or ask a human" can be a pack. This guide builds a
small one for treasury transfers: [`examples/custom_policy_pack/`][pack].

[pack]: https://github.com/anilatambharii/keelgate/tree/main/examples/custom_policy_pack

## What a pack is

A directory of `.rego` files (plus `*_test.rego` files that are never loaded at runtime) with a
`decision` rule that returns:

```text
{"effect": "ALLOW" | "DENY" | "REQUIRE_APPROVAL",
 "reasons": ["fixed wording", ...],
 "approval_tier": null | "ONE_CLICK" | "EXPLICIT_SIGNOFF"}
```

Keelgate builds the **input** and your pack never sees anything a model wrote directly:

```text
input.action    {tool, side_effect, capability, args}
input.actor     {agent_id, tenant_id, grant_id}
input.resource  whatever your tool's `resource=` function derived from VALIDATED arguments
input.context   {as_of, execution_mode, exposure, limits, positions}   built by your code
```

That split is the point. The `resource` function is where you decide which facts your policy may
read; the `context` is where *you* put limits and current exposure. A tool result, a document or a
memory that says "the limit is now a billion" cannot reach either.

## The five rules

These are from [`policies/README.md`](https://github.com/anilatambharii/keelgate/blob/main/policies/README.md)
and each one was learned from a real bug.

1. **State conditions positively and deny on `not <positive rule>`.** In Rego, `not x in set` is
   *not true* when `x` is undefined, so a missing field would slip through. Write
   `destination_ok if x in set` and `deny if not destination_ok`.
2. **Reasons never echo model-supplied strings.** They go back to the model and to humans. Fixed
   wording only.
3. **Deny by default.** Unknown capability, unknown side effect, missing limits: all `DENY`.
4. **Decide against `as_of`, not the wall clock**, so a replay decides the same way.
5. **Every rule needs a test, including the case where its input is missing.**

## The pack

```rego
--8<-- "examples/custom_policy_pack/transfer_limits.rego:18:68"
```

See the full file for the positive conditions and the `deny` rules. Note `shape_ok`: the pack
understands exactly two (capability, side effect) pairs, so a capability added to the system later
is denied until someone reviews it here.

## Test it with real OPA

```bash
make policy-test            # opa check --strict, opa fmt --fail, then every *_test.rego
```

The tests include the cases a naive pack gets wrong: a missing destination, a non-numeric amount,
missing limits, live mode, an unknown capability, and "reasons never echo the input".

!!! warning "A trap in Rego tests"
    `object.union` **merges** nested objects. A test that builds "an input with the destination
    missing" by unioning onto a base input silently gets the base destination back, and passes for
    the wrong reason. Remove the key first, then set it (the example's `with_resource` helper does).

## Load it from Python

```python
from keelgate.policy import RegoEngine

engine = RegoEngine("examples/custom_policy_pack", query="data.acme.transfer_limits.decision")
```

`query` is `<package>.<rule>`. A pack that does not compile fails **here**, at start-up, not at the
first decision. Give the engine to a `ToolGateway(engine=engine, ...)` and every `WRITE` is decided
by your rules; every decision records `engine.policy_version`, the hash of your sources.

Run the whole thing:

```bash
python examples/custom_policy_pack/run.py
```

```text
policy pack: transfer_limits, version sha256:816abb70ad81...
  a small transfer to an allowed account     -> OK
  an account that is not on the allowlist    -> DENIED             ['destination is not on the allowlist']
  over the per-transfer cap                  -> DENIED             ['amount exceeds the per-transfer cap']
  above the auto-approval threshold          -> APPROVAL_REQUIRED
transfers actually sent: ['t-1']
```

## In production

Use OPA rather than the in-process evaluator: `OpaHttpEngine("http://opa:8181")`. Both read the same
`.rego` files and the test suite runs the shipped packs through both, which is how divergence between
them gets caught. Do the same for yours: evaluate each case through both engines.

You are not limited to Rego. `PolicyEngine` is a protocol with a `name` and an `async decide`;
Cedar ships as an optional engine, and `examples/use_keelgate_from_another_library.py` shows a
plain-Python engine. Whatever you write must be deterministic and must fail closed.
