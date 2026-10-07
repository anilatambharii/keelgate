"""The ``keelgate`` command line: ``eval run``, ``eval list`` and ``replay``.

Exit codes: 0 everything held, 1 a case failed or a regression was found, 2 bad usage or a
configuration problem (so CI can tell "the code is worse" from "the job is misconfigured").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from keelgate.evals._types import EvalReport
    from keelgate.llm import LLMClient

OK: Final = 0
FAILED: Final = 1
USAGE: Final = 2
PROVIDERS: Final = ("anthropic", "openai", "google", "ollama", "vllm")


def _live_client(provider: str) -> LLMClient:
    from keelgate.llm import providers  # noqa: PLC0415 - SDKs load lazily inside each client

    factories = {
        "anthropic": providers.AnthropicClient,
        "openai": providers.OpenAIClient,
        "google": providers.GoogleClient,
        "ollama": providers.OllamaClient,
        "vllm": providers.VLLMClient,
    }
    return factories[provider]()  # type: ignore[no-any-return]


def _print_summary(report: EvalReport) -> None:
    out = sys.stdout.write
    out(f"\nKeelgate evals   mode={report.mode}   keelgate {report.version}\n\n")
    for suite in report.suites:
        verb = "blocked" if suite.name == "redteam" else "passed"
        ran = suite.ran
        mark = "PASS" if suite.passed else "FAIL"
        out(f"  [{mark}] {suite.name:<11} {sum(c.passed for c in ran)}/{len(ran)} {verb}\n")
        if suite.error:
            out(f"         error: {suite.error}\n")
        for case in ran:
            if not case.passed:
                out(f"         x {case.case_id}  {case.title}\n           {case.detail}\n")
    if report.metrics:
        out("\n  metrics\n")
        for m in report.metrics:
            arrow = "higher" if m.higher_is_better else "lower"
            out(f"    {m.name:<30} {m.value:>9.4f}   n={m.n}  ({arrow} is better, {m.source})\n")
    if report.regressions:
        out("\n  REGRESSIONS\n")
        for line in report.regressions:
            out(f"    - {line}\n")
    for note in report.notes:
        out(f"\n  note: {note}\n")
    out(f"\n  {'ALL CHECKS PASSED' if report.passed else 'FAILED'}\n\n")


def _run(args: argparse.Namespace) -> int:
    from keelgate.evals import (  # noqa: PLC0415
        SUITES,
        EvalContext,
        baseline_of,
        compare,
        load_records,
        run_suites,
        write_html,
        write_json,
        write_markdown,
    )

    names = list(SUITES) if args.suite == "all" else [s.strip() for s in args.suite.split(",")]
    ctx = EvalContext(mode=args.mode)
    if args.mode == "live":
        if not args.provider or not args.model:
            sys.stderr.write("live mode needs --provider and --model\n")
            return USAGE
        try:
            ctx = EvalContext(
                mode="live", live_client=_live_client(args.provider), model=args.model
            )
        except (ImportError, ValueError, TypeError) as exc:
            sys.stderr.write(f"could not create the {args.provider} client: {exc}\n")
            return USAGE
    try:
        records = load_records(args.outcomes) if args.outcomes else None
        report = asyncio.run(run_suites(names, ctx, records=records))
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return USAGE

    baseline_path = Path(args.baseline) if args.baseline else None
    if baseline_path is not None and baseline_path.exists() and not args.update_baseline:
        try:
            baseline: dict[str, Any] = json.loads(baseline_path.read_text(encoding="utf-8"))
        except ValueError:
            sys.stderr.write(f"error: {baseline_path} is not valid JSON\n")
            return USAGE
        report.regressions = compare(report, baseline, tolerance=args.tolerance)
    elif baseline_path is not None and not args.update_baseline:
        report.notes.append(f"no baseline at {baseline_path}; regressions were not checked")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = {f.strip() for f in args.format.split(",")}
    if "json" in formats:
        write_json(report, out_dir / "report.json")
    if "html" in formats:
        write_html(report, out_dir / "report.html")
    if "md" in formats:
        write_markdown(report, out_dir / "report.md")
    if args.update_baseline and baseline_path is not None:
        if report.failures:
            sys.stderr.write("refusing to write a baseline from a run with failing cases\n")
            return FAILED
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline_of(report), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        report.notes.append(f"baseline written to {baseline_path}")

    _print_summary(report)
    sys.stdout.write(f"  reports: {out_dir / 'report.json'}, {out_dir / 'report.html'}\n\n")
    return OK if report.passed or args.no_fail else FAILED


def _list(_: argparse.Namespace) -> int:
    from keelgate.evals import _redteam as redteam  # noqa: PLC0415
    from keelgate.evals import _trajectory as trajectory  # noqa: PLC0415
    from keelgate.evals import _unit as unit  # noqa: PLC0415
    from keelgate.evals._metrics import discover_metrics  # noqa: PLC0415

    out = sys.stdout.write
    out("unit\n" + "".join(f"  {c.case_id}  {c.prompt}\n" for c in unit.CASES))
    out("trajectory\n" + "".join(f"  {c.case_id}  {c.title}\n" for c in trajectory.CASES))
    out("redteam\n")
    for c in redteam.CASES:
        out(f"  {c.case_id}  [{c.category}] {c.title}\n")
    out("outcome (entry point group keelgate.outcome_metrics)\n")
    for m in discover_metrics():
        out(f"  {m.entry_point}  {m.source}  {m.error or 'ok'}\n")
    return OK


def _replay(args: argparse.Namespace) -> int:
    from keelgate.loop import (  # noqa: PLC0415
        NotReplayableError,
        Recording,
        SqliteCheckpointStore,
        replay,
    )

    if not Path(args.checkpoints).exists():
        sys.stderr.write(f"error: no checkpoint database at {args.checkpoints}\n")
        return USAGE
    store = SqliteCheckpointStore(args.checkpoints)
    try:
        recording = Recording.from_store(
            store, args.tenant, trace_id=args.trace_id, run_id=args.run_id
        )
        report = asyncio.run(replay(recording))
    except (NotReplayableError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return USAGE
    finally:
        store.close()
    if args.json:
        payload = {
            "identical": report.identical,
            "trace_id": recording.trace_id,
            "run_id": recording.run_id,
            "steps": len(recording.steps),
            "divergences": [
                {"where": d.where, "expected": str(d.expected), "actual": str(d.actual)}
                for d in report.divergences
            ],
            "mismatched_calls": list(report.mismatched_calls),
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(
            f"run {recording.run_id} (trace {recording.trace_id}): {len(recording.steps)} step(s), "
            f"{'replays identically' if report.identical else 'DIVERGES'}\n"
        )
        for d in report.divergences:
            sys.stdout.write(f"  - {d.where}: expected {d.expected!r}, got {d.actual!r}\n")
        for line in report.mismatched_calls:
            sys.stdout.write(f"  - {line}\n")
    return OK if report.identical else FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keelgate", description="Keelgate command line")
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("eval", help="run evals and red-team suites").add_subparsers(
        dest="eval_command", required=True
    )
    run = ev.add_parser("run", help="run suites and write a JSON + HTML report")
    run.add_argument(
        "--suite", default="all", help="all, or a comma list of unit,trajectory,redteam,outcome"
    )
    run.add_argument("--mode", choices=("fake", "live"), default="fake")
    run.add_argument("--provider", choices=PROVIDERS, help="live mode: which provider")
    run.add_argument("--model", help="live mode: the model name")
    run.add_argument("--out", default="eval-report", help="directory for report.json / report.html")
    run.add_argument("--format", default="json,html", help="json, html, md (comma list)")
    run.add_argument("--baseline", help="a baseline JSON; any regression against it fails the run")
    run.add_argument(
        "--update-baseline", action="store_true", help="write the baseline from this run"
    )
    run.add_argument("--tolerance", type=float, default=0.0, help="allowed metric drop vs baseline")
    run.add_argument(
        "--outcomes", help="a JSON-lines file of outcome records for the outcome suite"
    )
    run.add_argument("--no-fail", action="store_true", help="exit 0 even when something failed")
    run.set_defaults(handler=_run)
    ev.add_parser("list", help="list suites, cases and discovered metrics").set_defaults(
        handler=_list
    )

    rp = sub.add_parser("replay", help="rebuild a run from its trace id and replay it")
    rp.add_argument("--checkpoints", required=True, help="path to a SQLite checkpoint database")
    rp.add_argument("--tenant", required=True)
    key = rp.add_mutually_exclusive_group(required=True)
    key.add_argument("--trace-id")
    key.add_argument("--run-id")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(handler=_replay)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
