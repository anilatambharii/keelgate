# Build a governed agent in 10 minutes

By the end you will have an agent that reads a quote and places paper trades, where every call is
gated by a signed grant and a policy, large orders wait for a human, retries cannot double-fire,
and everything is in a tamper-evident log. No API key and no network: the "model" is a script.

All the code on this page is one file, [`examples/tutorial_governed_agent.py`][tut], which the test
suite runs. If a snippet here does not work, that is a bug in the docs, so please report it.

[tut]: https://github.com/anilatambharii/keelgate/blob/main/examples/tutorial_governed_agent.py

## 1. Install

```bash
pip install keelgate
keelgate quickstart          # optional: 20 seconds to see the gate in action
```

Python 3.11 or 3.12. The core needs nothing else; framework adapters are extras
(`keelgate[langgraph]`, `keelgate[mcp]`, ...).

## 2. Declare what the agent may do

```python
--8<-- "examples/tutorial_governed_agent.py:imports"
```

A **tool** is a typed function with a capability and a side effect. Two things matter here: the
`WRITE` supplies an **idempotency key**, so retrying the same order can never place it twice, and
`resource` states exactly which facts the policy may look at, derived from *validated* arguments
(never from model prose).

```python
--8<-- "examples/tutorial_governed_agent.py:tools"
```

## 3. Give it authority, and put the gate in front

```python
--8<-- "examples/tutorial_governed_agent.py:authority"
```

The **grant** is the agent's entire authority: which capabilities, which tenant, how much budget,
until when. Anything not named is refused. The **gateway** is the only way to run a tool, and
`RegoEngine` is the `finance_basic` policy pack: deny by default, a restricted-symbol list, a
per-order cap, daily exposure, trading hours, and approval tiers for large orders.

## 4. See the gate decide

```python
--8<-- "examples/tutorial_governed_agent.py:calls"
```

Running it prints the following (the parenthesised notes are ours, not program output):

```text
allowed   -> OK
retry     -> OK                      (replayed: the order ran once)
denied    -> DENIED             policy_denied      (TSLA is on the restricted list)
parked    -> APPROVAL_REQUIRED  EXPLICIT_SIGNOFF   (30,000 is above the sign-off threshold)
approved  -> OK                                    (a human approved exactly these arguments)
```

Notice what did *not* happen. The denied order never reached your function. The parked order
waited for a person, and the approval was good for those exact arguments, once. The model was
never asked to be careful; none of this depends on it.

## 5. Let a model drive, durably

```python
--8<-- "examples/tutorial_governed_agent.py:loop"
```

The **loop** plans, acts, observes and verifies, checkpointing every step. Here a scripted model
quotes MSFT, places a paper order and reports:

```python
--8<-- "examples/tutorial_governed_agent.py:run"
```

Swap `FakeLLM` for a real client (`AnthropicClient`, `OpenAIClient`, `GoogleClient`, `OllamaClient`
or `VLLMClient`) and nothing else changes. The loop stops on explicit limits (`StopConditions`:
iterations, tokens, dollars, a timeout, a goal), and if the process dies it resumes from the last
checkpoint **without repeating an order that already ran**.

## 6. Prove what happened

```python
--8<-- "examples/tutorial_governed_agent.py:audit"
```

Every grant check, policy decision, approval and tool result is in a hash chain per tenant, written
*before* the side effect. Edit or delete a record and `verify_chain` fails. Run it from somewhere
that does not trust the writer for an independent check.

## Where next

| You want to | Read |
|---|---|
| Understand why it is built this way | [Concepts](concepts.md) |
| Write your own rules | [Custom policy pack](guides/custom-policy-pack.md) |
| Use LangGraph | [Governed LangGraph agent](guides/langgraph.md) |
| Give Claude Desktop or Cursor governed tools | [MCP server for Claude Desktop and Cursor](guides/mcp-clients.md) |
| Trace, replay and test a run | [Telemetry, replay and evals](telemetry-and-evals.md) |
| Depend on Keelgate from your own library | [Integration contract](integration-contract.md) |
| Know what can and cannot go wrong | [Security model](security-model.md) |
