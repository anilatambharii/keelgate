"""The hosted policy registry: validate, test, simulate, version and deploy per-tenant Rego packs.

A pack is flat: ``{name.rego: text}`` plus the ``query`` that yields the decision and the test
``cases``. Evaluation uses the open-source in-process engine (``keelgate.policy.RegoEngine``), the
same code a client runs, so a decision made here is the decision the agent gets.

What the evaluation guard guarantees, and what it cannot
--------------------------------------------------------
Every compile and every decision runs on a worker thread with a deadline. A policy that takes too
long fails *that case* and the request returns on time. Python cannot kill a thread, and the Rego
interpreter is native code, so a runaway evaluation keeps running in the background until it ends
on its own. The guard therefore also (a) caps the total time one request may spend on cases,
(b) caps the number of abandoned, still-running workers process-wide and refuses new evaluations
(503) beyond it, and (c) bounds the work an attacker can submit (file, case and input sizes).
It is not a CPU or memory sandbox: for hostile tenants run the control plane's evaluator in a
separate, resource-limited process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar

from keelgate.policy import RegoEngine, load_pack_sources, pack_path
from keelgate.policy._engine import hash_sources
from pydantic import BaseModel, ConfigDict, Field

from keelgate_cloud.errors import ApiError, ValidationFailedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from keelgate.policy import PolicyDecision

MAX_FILES: Final = 20
MAX_FILE_BYTES: Final = 200 * 1024
MAX_TOTAL_BYTES: Final = 500 * 1024
MAX_CASES: Final = 200
MAX_INPUT_BYTES: Final = 64 * 1024
MAX_ERROR_CHARS: Final = 2000
CASE_TIMEOUT_SECONDS: Final = 3.0
COMPILE_TIMEOUT_SECONDS: Final = 10.0
REQUEST_BUDGET_SECONDS: Final = 30.0
MAX_STUCK_WORKERS: Final = 8

PACK_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
FILE_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.rego$")
QUERY_RE: Final = re.compile(r"^data(\.[A-Za-z_][A-Za-z0-9_]*){1,10}$")

_T = TypeVar("_T")

Effect = Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
Tier = Literal["ONE_CLICK", "EXPLICIT_SIGNOFF"]


# ------------------------------------------------------------------------------------ models


class Expect(BaseModel):
    """What a case expects. ``approval_tier`` is only compared when the case states it."""

    model_config = ConfigDict(extra="forbid")

    effect: Effect
    approval_tier: Tier | None = None


class Case(BaseModel):
    """A test case of a stored pack."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    input: dict[str, Any]
    expect: Expect


class SimCase(BaseModel):
    """A simulation case: like a test case but the expectation is optional."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    input: dict[str, Any]
    expect: Expect | None = None


class PackBody(BaseModel):
    """The sources, query and cases of one version."""

    model_config = ConfigDict(extra="forbid")

    files: dict[str, str]
    query: str = Field(max_length=300)
    cases: list[Case] = Field(default_factory=list)


class CreateBody(PackBody):
    """A new pack and its first version."""

    name: str = Field(max_length=100)


class SimulateBody(BaseModel):
    """Ad-hoc evaluation: one ``input`` or several ``cases``, never persisted."""

    model_config = ConfigDict(extra="forbid")

    files: dict[str, str]
    query: str = Field(max_length=300)
    input: dict[str, Any] | None = None
    cases: list[SimCase] | None = None


# -------------------------------------------------------------------------------- validation


def validate_name(name: str) -> str:
    if not PACK_NAME_RE.match(name):
        raise ValidationFailedError(
            "pack name must be 1-63 characters of a-z, 0-9, '_' or '-', starting with a letter "
            "or digit",
            code="invalid_pack_name",
        )
    return name


def validate_sources(files: dict[str, str], query: str) -> None:
    """Refuse malformed or oversized sources before anything is compiled."""
    if not files:
        raise ValidationFailedError("a pack needs at least one .rego file", code="no_files")
    if len(files) > MAX_FILES:
        raise ValidationFailedError(f"a pack may have at most {MAX_FILES} files", code="too_big")
    total = 0
    for name, text in files.items():
        if name.endswith("_test.rego"):
            raise ValidationFailedError(
                f"{name!r} looks like a Rego test file; put test scenarios in `cases` instead",
                code="test_file_not_allowed",
            )
        if not FILE_NAME_RE.match(name) or ".." in name:
            raise ValidationFailedError(
                "file names must be bare names ending in .rego (letters, digits, '_', '-', '.')",
                code="invalid_file_name",
            )
        if "\x00" in text:
            raise ValidationFailedError(f"{name!r} contains a NUL character", code="invalid_file")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValidationFailedError(
                f"{name!r} is larger than {MAX_FILE_BYTES // 1024} KB", code="too_big"
            )
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ValidationFailedError(
            f"a pack may total at most {MAX_TOTAL_BYTES // 1024} KB", code="too_big"
        )
    if not QUERY_RE.match(query):
        raise ValidationFailedError(
            "query must look like data.<package>.<rule>, e.g. data.acme.transfer_limits.decision",
            code="invalid_query",
        )


def validate_cases(cases: list[Case] | list[SimCase]) -> None:
    if len(cases) > MAX_CASES:
        raise ValidationFailedError(f"at most {MAX_CASES} cases are allowed", code="too_big")
    for case in cases:
        validate_input(case.input, f"case {case.name!r}")


def validate_input(document: dict[str, Any], label: str = "input") -> None:
    try:
        encoded = json.dumps(document, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError(
            f"{label} is not a plain JSON document (NaN and infinity are refused)",
            code="invalid_input_document",
        ) from exc
    if "\\u0000" in encoded:
        raise ValidationFailedError(f"{label} contains a NUL character", code="invalid_input")
    if len(encoded.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValidationFailedError(
            f"{label} is larger than {MAX_INPUT_BYTES // 1024} KB", code="too_big"
        )


# ---------------------------------------------------------------------------- guarded running

_stuck: list[threading.Thread] = []
_stuck_lock = threading.Lock()


class EvaluatorBusyError(ApiError):
    """Too many abandoned evaluations are still running."""

    status = 503
    code = "evaluator_busy"


class _Timeout(Exception):
    pass


def _stuck_count() -> int:
    with _stuck_lock:
        _stuck[:] = [t for t in _stuck if t.is_alive()]
        return len(_stuck)


def _guarded(fn: Callable[[], _T], timeout: float) -> _T:
    """Run ``fn`` on a daemon thread; raise ``_Timeout`` if it has not finished in ``timeout``."""
    if _stuck_count() >= MAX_STUCK_WORKERS:
        raise EvaluatorBusyError("the policy evaluator is busy; try again shortly")
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, name="policy-eval", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        with _stuck_lock:
            _stuck.append(thread)
        raise _Timeout
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]


MAX_EVALUATIONS_PER_ORG: Final = 2
_org_slots: dict[str, int] = {}
_org_slots_lock = threading.Lock()


def acquire_org_slot(org_id: str) -> None:
    """Take one of an org's concurrent evaluation slots, or raise ``EvaluatorBusyError``.

    Evaluation runs on shared threads, so one tenant must not be able to occupy all of them.
    """
    with _org_slots_lock:
        if _org_slots.get(org_id, 0) >= MAX_EVALUATIONS_PER_ORG:
            raise EvaluatorBusyError("too many policy evaluations in flight for this organisation")
        _org_slots[org_id] = _org_slots.get(org_id, 0) + 1


def release_org_slot(org_id: str) -> None:
    """Give a slot back."""
    with _org_slots_lock:
        left = _org_slots.get(org_id, 0) - 1
        if left > 0:
            _org_slots[org_id] = left
        else:
            _org_slots.pop(org_id, None)


def source_hash(files: dict[str, str]) -> str:
    """The version hash the client recomputes and checks."""
    return hash_sources(files)


_PATH_BEFORE_REGO = re.compile(r"(?:[A-Za-z]:)?[^\s:'\"]*[\\/]([^\\/\s:'\"]+\.rego)")


def _clip(text: str) -> str:
    """A compiler message that is safe to show: no server paths, bounded length."""
    text = _PATH_BEFORE_REGO.sub(r"\1", text)
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "... (truncated)"


def _build_engine(files: dict[str, str], query: str) -> RegoEngine:
    with tempfile.TemporaryDirectory(prefix="keelgate-registry-") as tmp:
        for name, text in files.items():
            (Path(tmp) / name).write_text(text, encoding="utf-8")
        return RegoEngine(Path(tmp), query=query)


def compile_pack(files: dict[str, str], query: str) -> tuple[RegoEngine | None, str | None]:
    """Compile ``files``. Returns ``(engine, None)`` or ``(None, truncated compiler message)``."""
    try:
        return _guarded(lambda: _build_engine(files, query), COMPILE_TIMEOUT_SECONDS), None
    except _Timeout:
        return None, "compilation timed out"
    except EvaluatorBusyError:
        raise
    except Exception as exc:
        return None, _clip(f"{type(exc).__name__}: {exc}")


class Evaluator:
    """Decides raw input documents against one compiled pack, with a deadline per decision."""

    def __init__(self, files: dict[str, str], query: str, engine: RegoEngine) -> None:
        self._files, self._query, self._engine = files, query, engine
        self.version = engine.policy_version

    def decide(self, document: dict[str, Any]) -> tuple[PolicyDecision | None, str | None]:
        """A decision, or ``(None, reason)`` when evaluation timed out or could not run."""
        engine = self._engine

        def run() -> PolicyDecision:
            return asyncio.run(engine.decide_document(document))

        try:
            return _guarded(run, CASE_TIMEOUT_SECONDS), None
        except _Timeout:
            # The abandoned worker may still hold the engine's lock: never reuse that engine.
            rebuilt, _ = compile_pack(self._files, self._query)
            if rebuilt is not None:
                self._engine = rebuilt
            return None, f"evaluation exceeded {CASE_TIMEOUT_SECONDS:g}s and was abandoned"


def _actual(decision: PolicyDecision) -> dict[str, Any]:
    return {
        "effect": decision.effect.value,
        "reasons": list(decision.reasons),
        "approval_tier": decision.approval_tier.value if decision.approval_tier else None,
    }


def evaluate_cases(
    files: dict[str, str], query: str, cases: list[Case] | list[SimCase]
) -> dict[str, Any]:
    """Compile and run ``cases``; never raises for a bad pack, reports it instead.

    The report is ``{compiled, compile_error, source_hash, engine, total, passed_count,
    all_passed, cases: [{name, passed, expected, actual, error}]}``. ``passed`` is ``None`` for a
    case with no expectation (a simulation).
    """
    digest = source_hash(files)
    report: dict[str, Any] = {
        "compiled": False,
        "compile_error": None,
        "source_hash": digest,
        "engine": RegoEngine.name,
        "total": len(cases),
        "passed_count": 0,
        "all_passed": False,
        "cases": [],
    }
    engine, error = compile_pack(files, query)
    if engine is None:
        report["compile_error"] = error
        return report
    report["compiled"] = True
    evaluator = Evaluator(files, query, engine)
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    for case in cases:
        expected = None if case.expect is None else case.expect.model_dump(exclude_unset=True)
        decision: PolicyDecision | None
        problem: str | None
        if time.monotonic() - started > REQUEST_BUDGET_SECONDS:
            decision, problem = None, "request time budget exhausted before this case ran"
        else:
            decision, problem = evaluator.decide(case.input)
        actual = None if decision is None else _actual(decision)
        if (
            actual is not None
            and actual["reasons"]
            and actual["reasons"][0].startswith("policy evaluation failed")
        ):
            problem = actual["reasons"][0]  # an engine error is not a policy decision
        passed: bool | None
        if problem is not None:
            passed = False if case.expect is not None else None
        elif case.expect is None:
            passed = None
        else:
            passed = actual is not None and _matches(actual, case.expect)
        results.append(
            {
                "name": case.name,
                "passed": passed,
                "expected": expected,
                "actual": actual,
                "error": problem,
            }
        )
    report["cases"] = results
    report["passed_count"] = sum(1 for r in results if r["passed"] is True)
    report["all_passed"] = bool(results) and all(r["passed"] is True for r in results)
    return report


def _matches(actual: dict[str, Any], expect: Expect) -> bool:
    if actual["effect"] != expect.effect:
        return False
    stated = "approval_tier" in expect.model_fields_set
    return not stated or actual["approval_tier"] == expect.approval_tier


# ---------------------------------------------------------------------------------- templates


def _starter() -> dict[str, Any]:
    text = (
        "package acme.starter\n\n"
        "import rego.v1\n\n"
        "# Deny by default; allow only what is stated positively.\n"
        "default decision := {\n"
        '\t"effect": "DENY",\n'
        '\t"reasons": ["no rule allows this action"],\n'
        '\t"approval_tier": null,\n'
        "}\n\n"
        "decision := {\n"
        '\t"effect": "ALLOW",\n'
        '\t"reasons": ["read-only action"],\n'
        '\t"approval_tier": null,\n'
        "} if {\n"
        '\tinput.action.side_effect == "READ"\n'
        '\tinput.context.execution_mode == "paper"\n'
        "}\n"
    )
    base: dict[str, Any] = {
        "action": {
            "tool": "get_balance",
            "side_effect": "READ",
            "capability": "x:read",
            "args": {},
        },
        "actor": {"agent_id": "a", "tenant_id": "t", "grant_id": "g"},
        "resource": {},
        "context": {"as_of": "2026-10-05T14:30:00+00:00", "execution_mode": "paper"},
    }
    write = {**base, "action": {**base["action"], "side_effect": "WRITE"}}
    return {
        "name": "starter",
        "description": "A deny-by-default pack that allows reads in paper mode.",
        "files": {"starter.rego": text},
        "query": "data.acme.starter.decision",
        "cases": [
            {
                "name": "a read in paper mode is allowed",
                "input": base,
                "expect": {"effect": "ALLOW"},
            },
            {"name": "a write is denied", "input": write, "expect": {"effect": "DENY"}},
            {
                "name": "a read with the mode missing is denied",
                "input": {k: v for k, v in base.items() if k != "context"},
                "expect": {"effect": "DENY"},
            },
        ],
    }


def _transfer_limits_cases() -> list[dict[str, Any]]:
    limits = {
        "allowed_destinations": ["acct-payroll", "acct-vendors"],
        "max_transfer": 10_000,
        "max_daily_total": 25_000,
        "approval_threshold": 2_000,
    }

    def doc(amount: Any, destination: str | None = "acct-vendors", **ctx: Any) -> dict[str, Any]:
        resource: dict[str, Any] = {"amount": amount}
        if destination is not None:
            resource["destination"] = destination
        context = {
            "as_of": "2026-10-05T14:30:00+00:00",
            "execution_mode": "paper",
            "exposure": {"daily_total": 0},
            "limits": limits,
            **ctx,
        }
        return {
            "action": {
                "tool": "transfer_funds",
                "side_effect": "WRITE",
                "capability": "treasury:transfer",
                "args": {},
            },
            "actor": {"agent_id": "treasury-agent", "tenant_id": "acme", "grant_id": "g-1"},
            "resource": resource,
            "context": context,
        }

    return [
        {"name": "small transfer is allowed", "input": doc(1500), "expect": {"effect": "ALLOW"}},
        {
            "name": "unknown destination is denied",
            "input": doc(100, "acct-offshore"),
            "expect": {"effect": "DENY"},
        },
        {
            "name": "missing destination is denied",
            "input": doc(100, None),
            "expect": {"effect": "DENY"},
        },
        {"name": "over the cap is denied", "input": doc(12_000), "expect": {"effect": "DENY"}},
        {
            "name": "live mode is denied",
            "input": doc(100, execution_mode="live"),
            "expect": {"effect": "DENY"},
        },
        {
            "name": "above the threshold needs one click",
            "input": doc(3000),
            "expect": {"effect": "REQUIRE_APPROVAL", "approval_tier": "ONE_CLICK"},
        },
        {
            "name": "very large needs explicit signoff",
            "input": doc(5000),
            "expect": {"effect": "REQUIRE_APPROVAL", "approval_tier": "EXPLICIT_SIGNOFF"},
        },
    ]


def templates() -> list[dict[str, Any]]:
    """Starter packs for the editor: the shipped ``finance_basic`` and the custom-pack example."""
    items = [_starter()]
    with contextlib.suppress(FileNotFoundError):  # the pack ships with the open-source package
        items.append(
            {
                "name": "finance_basic",
                "description": "The shipped paper-trading pack: restricted list, caps, hours.",
                "files": load_pack_sources(pack_path("finance_basic")),
                "query": "data.keelgate.finance_basic.decision",
                "cases": [],
            }
        )
    example = pack_path("finance_basic").parents[1] / "examples" / "custom_policy_pack"
    with contextlib.suppress(FileNotFoundError):  # an installed wheel has no examples directory
        items.append(
            {
                "name": "transfer_limits",
                "description": "A worked custom pack: deny-by-default treasury transfer limits.",
                "files": load_pack_sources(example),
                "query": "data.acme.transfer_limits.decision",
                "cases": _transfer_limits_cases(),
            }
        )
    return items


def check_pack(body: PackBody) -> None:
    """Validate the sources, query and cases of a version."""
    validate_sources(body.files, body.query)
    validate_cases(body.cases)


def tests_passed(report: dict[str, Any]) -> bool:
    """A version has passed when it compiled and it has cases and every case passed."""
    return bool(report["compiled"] and report["all_passed"])
