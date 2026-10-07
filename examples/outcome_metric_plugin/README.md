# keelgate-example-metrics

A minimal external package that contributes outcome metrics to Keelgate evals. It is how a
downstream project such as Tycheon plugs in financial metrics: declare entry points in the
`keelgate.outcome_metrics` group and install the package.

```toml
[project.entry-points."keelgate.outcome_metrics"]
brier_score = "keelgate_example_metrics:BrierScore"
```

Each entry point names a class (instantiated with no arguments), a zero-argument factory or an
instance that satisfies `keelgate.evals.OutcomeMetric`:

```python
class BrierScore:
    name = "brier_score"
    higher_is_better = False

    def compute(self, records): ...  # -> keelgate.evals.MetricResult
```

Records carry `predicted` and `realized` mappings whose keys you define. These metrics read
`predicted["p_up"]` (a probability) and `realized["up"]` (a boolean).

In the Keelgate repo this package is installed in the dev environment, so `keelgate eval run`
discovers it. Elsewhere: `pip install ./examples/outcome_metric_plugin`, then `keelgate eval list`.
