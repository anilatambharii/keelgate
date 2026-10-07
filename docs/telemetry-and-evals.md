# Telemetry, replay and evals

How to see a run, reproduce it, and keep proving the harness works. The reasoning is in
[ADR-0005](adr/0005-telemetry-replay-and-evals.md).

## Trace a run

```bash
make up                                      # starts Jaeger (UI :16686, OTLP/HTTP :4318)
uv run python examples/research_loop.py --trace
```

The example prints a link such as `http://localhost:16686/trace/<trace id>`. That one trace covers
the budget stop, the simulated restart and the resume, with a span per loop step, a `chat` span
per model call (tokens, cost), an `execute_tool` span per tool call and a `keelgate.policy.decide`
span (effect, engine, policy version hash) under each WRITE.

In your own code:

```python
from keelgate import telemetry

tel = telemetry.instrument(service_name="my-agent", redact=True)  # needs keelgate[otlp]
llm = telemetry.InstrumentedLLM(my_llm_client)  # model calls become spans
# ... run your Loop with `llm` ...
print(tel.costs.total(tenant_id="acme", agent_id="research-agent"))  # tokens and dollars
tel.shutdown()  # flush before exit
```

Point it elsewhere with `OTEL_EXPORTER_OTLP_ENDPOINT`. Phoenix and other OTLP backends should work
the same way; only Jaeger is verified.

**What is in a span.** Identifiers, verdicts, counts and a hash of the arguments. Never arguments,
tool output, prompts, responses or exception messages. `capture_content=True` (or
`OTEL_GENAI_CAPTURE_MESSAGE_CONTENT=true`) adds prompts and responses as span events; leave it off
in production, and use `redact=True` if you turn it on. Redaction is pattern-based (emails, card
numbers that pass Luhn, SSNs, phones, IBANs, API keys, JWTs, bearer tokens) and misses names and
free-text addresses.

## Replay a run

```bash
uv run keelgate replay --checkpoints path/to/checkpoints.sqlite --tenant acme --trace-id <trace id>
```

Rebuilds the run from its checkpoint history and replays it with recorded tool outputs. Nothing
executes. "Replays identically" means every step, call, outcome, verdict and the final answer
match; anything else is a divergence in the loop's own behaviour. Runs that stopped waiting for
approval, with an unknown outcome, or with an unrun plan are refused.

## Evals and red-team suites

```bash
make eval                                    # scripted model: free, deterministic, runs on every PR
uv run keelgate eval list                    # suites, cases and discovered outcome metrics
uv run keelgate eval run --suite redteam --out report
```

Writes `report.json`, `report.html` and `report.md`, and exits 1 if any case fails or anything is
worse than `evals/baseline.json`.

| Suite | Asks |
|---|---|
| `unit` | Does the model pick the right tool, with arguments that validate? |
| `trajectory` | Does the verifier catch seeded faults and leave good runs alone? |
| `redteam` | Assume the model is fooled. Does the harness still block the harm? Five categories: tool-output injection, document injection, memory poisoning, capability escalation, `as_of` leakage. |
| `outcome` | Metrics registered through the `keelgate.outcome_metrics` entry point. |

Red-team verdicts are about effects (did anything unauthorised happen), not about what the model
said, so they are identical for a scripted model and a real one.

### Real models

```bash
ANTHROPIC_API_KEY=... uv run keelgate eval run --mode live --provider anthropic \
    --model claude-haiku-4-5-20251001 --suite unit,trajectory,redteam
```

The nightly workflow does this for each provider whose secret is set. See `evals-nightly.yml` for
the repository variables it reads. These runs cost money and vary; baselines allow a tolerance, but
the red-team verdict has none.

### Regressions

```bash
uv run keelgate eval run --baseline evals/baseline.json            # fail on any regression
uv run keelgate eval run --baseline evals/baseline.json --update-baseline   # record a new one
```

A baseline is written only from a run with no failures. It records passing case ids and metric
values; a case that disappears counts as a regression, so deleting a failing test does not hide it.

## Contribute a metric

Declare an entry point in your package; no registration code and no change to Keelgate:

```toml
[project.entry-points."keelgate.outcome_metrics"]
brier_score = "mypkg.metrics:BrierScore"
```

```python
from keelgate.evals import MetricResult


class BrierScore:
    name = "brier_score"
    higher_is_better = False

    def compute(self, records):  # records: Sequence[OutcomeRecord]
        pairs = [(r.predicted["p"], 1.0 if r.realized["up"] else 0.0) for r in records]
        return MetricResult(self.name, sum((p - y) ** 2 for p, y in pairs) / len(pairs), len(pairs))
```

A complete example lives in `examples/outcome_metric_plugin/` and is installed in the dev
environment. Feed it your own data with `--outcomes records.jsonl` (one `{"predicted": {...},
"realized": {...}}` object per line). Plugins run in-process with full authority: install only
packages you trust.
