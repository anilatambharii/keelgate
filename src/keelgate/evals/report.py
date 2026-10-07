"""JSON and HTML reports, and regression checks against a committed baseline."""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from pathlib import Path

    from keelgate.evals.types import CaseResult, EvalReport, SuiteResult

BASELINE_VERSION: Final = 1
# Floating-point results differ in the last digits between Python versions and platforms; that is
# noise, not a regression. Anything real is far larger than this.
EPSILON: Final = 1e-9


def to_dict(report: EvalReport) -> dict[str, Any]:
    return {
        "keelgate_version": report.version,
        "generated_at": report.generated_at,
        "mode": report.mode,
        "passed": report.passed,
        "regressions": report.regressions,
        "notes": report.notes,
        "summary": {
            s.name: {
                "passed": s.passed,
                "ran": len(s.ran),
                "failed": sum(not c.passed for c in s.ran),
                "skipped": len(s.cases) - len(s.ran),
                "pass_rate": round(s.pass_rate, 4),
            }
            for s in report.suites
        },
        "metrics": [
            {
                "name": m.name,
                "value": m.value,
                "n": m.n,
                "higher_is_better": m.higher_is_better,
                "source": m.source,
                "details": m.details,
            }
            for m in report.metrics
        ],
        "suites": [
            {
                "name": s.name,
                "description": s.description,
                "passed": s.passed,
                "error": s.error,
                "cases": [_case(c) for c in s.cases],
            }
            for s in report.suites
        ],
    }


def _case(c: CaseResult) -> dict[str, Any]:
    return {
        "id": c.case_id,
        "title": c.title,
        "category": c.category,
        "passed": c.passed,
        "skipped": c.skipped,
        "detail": c.detail,
        "duration_ms": round(c.duration_ms, 1),
        "evidence": c.evidence,
    }


def write_json(report: EvalReport, path: Path) -> None:
    path.write_text(
        json.dumps(to_dict(report), indent=2, sort_keys=True, default=str) + "\n", "utf-8"
    )


# ------------------------------------------------------------------------ HTML

_CSS = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#59636e;--line:#d0d7de;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--card:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--card:#161b22}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto}
h1{margin:0 0 4px;font-size:1.6rem}h2{margin:32px 0 8px;font-size:1.15rem}
.muted{color:var(--muted)}.banner{padding:12px 16px;border-radius:8px;margin:16px 0;font-weight:600;border:1px solid var(--line);background:var(--card)}
.banner.ok{border-color:var(--ok);color:var(--ok)}.banner.bad{border-color:var(--bad);color:var(--bad)}
table{width:100%;border-collapse:collapse;margin:8px 0}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.78rem;font-weight:600;border:1px solid currentColor}
.tag.ok{color:var(--ok)}.tag.bad{color:var(--bad)}.tag.skip{color:var(--warn)}
details{margin-top:4px}summary{cursor:pointer;color:var(--muted);font-size:.85rem}
pre{margin:6px 0 0;padding:8px;background:var(--card);border:1px solid var(--line);border-radius:6px;overflow:auto;font-size:.8rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (max-width:600px){th:nth-child(3),td:nth-child(3){display:none}}
"""  # noqa: E501 - minified CSS


def _tag(c: CaseResult, verb: str) -> str:
    if c.skipped:
        return '<span class="tag skip">skipped</span>'
    return (
        f'<span class="tag ok">{html.escape(verb)}</span>'
        if c.passed
        else '<span class="tag bad">FAILED</span>'
    )


def _suite_html(s: SuiteResult) -> str:
    verb = "blocked" if s.name == "redteam" else "pass"
    rows = []
    for c in s.cases:
        evidence = json.dumps(c.evidence, indent=2, sort_keys=True, default=str)
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(c.case_id)}</code></td>"
            f"<td>{html.escape(c.title)}<details><summary>evidence</summary>"
            f"<pre>{html.escape(evidence)}</pre></details></td>"
            f"<td class='muted'>{html.escape(c.category)}</td>"
            f"<td>{_tag(c, verb)}</td>"
            f"<td class='muted'>{html.escape(c.detail)}</td>"
            "</tr>"
        )
    error = f'<p class="tag bad">{html.escape(s.error)}</p>' if s.error else ""
    return (
        f"<h2>{html.escape(s.name)} <span class='muted'>&middot; "
        f"{sum(c.passed for c in s.ran)}/{len(s.ran)} {html.escape(verb)}</span></h2>"
        f"<p class='muted'>{html.escape(s.description)}</p>{error}"
        "<table><thead><tr><th>Case</th><th>Title</th><th>Category</th><th>Result</th><th>Detail</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def to_html(report: EvalReport) -> str:
    banner_class = "ok" if report.passed else "bad"
    headline = (
        "All checks passed."
        if report.passed
        else f"{len(report.failures)} failing case(s), {len(report.regressions)} regression(s)."
    )
    metrics = "".join(
        f"<tr><td><code>{html.escape(m.name)}</code></td><td>{m.value:.4f}</td><td>{m.n}</td>"
        f"<td>{'higher' if m.higher_is_better else 'lower'} is better</td>"
        f"<td class='muted'>{html.escape(m.source)}</td></tr>"
        for m in report.metrics
    )
    regressions = (
        "<h2>Regressions</h2><ul>"
        + "".join(f"<li>{html.escape(r)}</li>" for r in report.regressions)
        + "</ul>"
        if report.regressions
        else ""
    )
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in report.notes)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Keelgate eval report</title>"
        f"<style>{_CSS}</style></head><body><main>"
        "<h1>Keelgate eval report</h1>"
        f"<p class='muted'>keelgate {html.escape(report.version)} &middot; mode "
        f"<code>{html.escape(report.mode)}</code> &middot; {html.escape(report.generated_at)}</p>"
        f"<div class='banner {banner_class}'>{html.escape(headline)}</div>"
        f"{regressions}"
        + (f"<ul class='muted'>{notes}</ul>" if notes else "")
        + "<h2>Metrics</h2><table><thead><tr><th>Metric</th><th>Value</th><th>n</th>"
        f"<th>Direction</th><th>Source</th></tr></thead><tbody>{metrics}</tbody></table>"
        + "".join(_suite_html(s) for s in report.suites)
        + "</main></body></html>"
    )


def to_markdown(report: EvalReport) -> str:
    """A short summary for a CI job page: verdict, per-suite counts, metrics, what failed."""
    verdict = "PASSED" if report.passed else "FAILED"
    lines = [
        f"## Keelgate evals: {verdict}",
        "",
        f"mode `{report.mode}` · keelgate {report.version}",
        "",
        "| Suite | Result | Cases |",
        "|---|---|---|",
    ]
    for s in report.suites:
        verb = "blocked" if s.name == "redteam" else "passed"
        mark = "pass" if s.passed else "FAIL"
        lines.append(f"| {s.name} | {mark} | {sum(c.passed for c in s.ran)}/{len(s.ran)} {verb} |")
    if report.metrics:
        lines += ["", "| Metric | Value | n |", "|---|---|---|"]
        lines += [f"| `{m.name}` | {m.value:.4f} | {m.n} |" for m in report.metrics]
    problems = [f"- `{c.suite}/{c.case_id}`: {c.detail}" for c in report.failures]
    problems += [f"- regression: {r}" for r in report.regressions]
    if problems:
        lines += ["", "### Problems", *problems]
    return "\n".join(lines) + "\n"


def write_markdown(report: EvalReport, path: Path) -> None:
    path.write_text(to_markdown(report), "utf-8")


def write_html(report: EvalReport, path: Path) -> None:
    path.write_text(to_html(report), "utf-8")


# ------------------------------------------------------------------------ baseline


def baseline_of(report: EvalReport) -> dict[str, Any]:
    """The facts a future run must not regress below: passing cases and metric values."""
    return {
        "version": BASELINE_VERSION,
        "mode": report.mode,
        "cases": {s.name: sorted(c.case_id for c in s.ran if c.passed) for s in report.suites},
        "metrics": {
            m.name: {"value": m.value, "higher_is_better": m.higher_is_better}
            for m in report.metrics
        },
    }


def compare(report: EvalReport, baseline: dict[str, Any], *, tolerance: float = 0.0) -> list[str]:
    """Every way ``report`` is worse than ``baseline``. Empty means no regression.

    ``tolerance`` is the drop you accept in a metric (live models vary). A float-rounding sliver
    (``EPSILON``) is always accepted.
    """
    if baseline.get("version") != BASELINE_VERSION:
        return [
            f"the baseline has version {baseline.get('version')!r}, expected {BASELINE_VERSION}"
        ]
    problems: list[str] = []
    for suite_name, ids in sorted(baseline.get("cases", {}).items()):
        suite = report.suite(suite_name)
        if suite is None:
            problems.append(f"suite {suite_name!r} is in the baseline but was not run")
            continue
        now = {c.case_id: c for c in suite.ran}
        for case_id in ids:
            if case_id not in now:
                problems.append(
                    f"{suite_name}/{case_id}: passed before, but did not run (removed?)"
                )
            elif not now[case_id].passed:
                problems.append(f"{suite_name}/{case_id}: passed before, now fails")
    current = {m.name: m for m in report.metrics}
    for name, base in sorted(baseline.get("metrics", {}).items()):
        metric = current.get(name)
        if metric is None:
            problems.append(f"metric {name!r}: in the baseline but not produced")
            continue
        worse = (
            metric.value < base["value"] - tolerance - EPSILON
            if base.get("higher_is_better", True)
            else metric.value > base["value"] + tolerance + EPSILON
        )
        if worse:
            problems.append(
                f"metric {name!r}: {metric.value:.4f} is worse than "
                f"the baseline {base['value']:.4f}"
            )
    return problems
