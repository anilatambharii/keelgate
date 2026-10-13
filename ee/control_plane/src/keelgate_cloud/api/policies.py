"""The hosted policy registry: dashboard API (``/api/policies``) and agent API (``/v1/policies``).

Versions are immutable. A version is deployed only when its cases compile, exist and pass *now*,
against the stored sources; the stored report is evidence, never trusted on its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg.errors
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from keelgate.audit import AuditError
from psycopg.types.json import Jsonb

from keelgate_cloud import policy_registry as reg
from keelgate_cloud.api.deps import Agent, State, User, require_roles
from keelgate_cloud.errors import (
    ApiError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationFailedError,
)

router = APIRouter(tags=["policies"])


class IntegrityError(ApiError):
    """Stored sources no longer hash to their recorded version."""

    status = 500
    code = "integrity_error"


WRITERS = require_roles("owner", "admin")


def _evaluation_slot(principal: User) -> Iterator[None]:
    """Limit how many policy evaluations one org can have running at once."""
    try:
        reg.acquire_org_slot(principal.org_id)
    except reg.EvaluatorBusyError as exc:
        raise RateLimitedError(str(exc), code="policy_busy") from exc
    try:
        yield
    finally:
        reg.release_org_slot(principal.org_id)


EVAL_SLOT = Depends(_evaluation_slot)


# -------------------------------------------------------------------------------------- helpers


def _lock_pack(conn: Any, name: str) -> dict[str, Any]:
    """The pack row, locked for the rest of the transaction (serialises numbering and deploys)."""
    pack = conn.execute(
        "SELECT id, name, created_at FROM policy_packs WHERE name = %s FOR UPDATE", (name,)
    ).fetchone()
    if pack is None:
        raise NotFoundError("no such policy pack")
    return dict(pack)


def _active(conn: Any, pack_id: Any) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT a.version_id, a.deployed_by, a.deployed_at, v.version, v.source_hash "
        "FROM policy_active a JOIN policy_versions v ON v.id = a.version_id WHERE a.pack_id = %s",
        (pack_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _insert_version(
    conn: Any,
    *,
    org_id: str,
    pack_id: Any,
    version: int,
    body: reg.PackBody,
    report: dict[str, Any],
    user_id: str | None,
) -> dict[str, Any]:
    row = conn.execute(
        "INSERT INTO policy_versions (org_id, pack_id, version, files, query, source_hash, cases, "
        "test_report, tests_passed, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING version, source_hash, tests_passed, created_at, created_by",
        (
            org_id,
            pack_id,
            version,
            Jsonb(body.files),
            body.query,
            report["source_hash"],
            Jsonb([c.model_dump(exclude_unset=True, mode="json") for c in body.cases]),
            Jsonb(report),
            reg.tests_passed(report),
            user_id,
        ),
    ).fetchone()
    assert row is not None  # noqa: S101
    return dict(row)


def _evaluate(body: reg.PackBody) -> dict[str, Any]:
    reg.check_pack(body)
    report = reg.evaluate_cases(body.files, body.query, body.cases)
    if not report["compiled"]:
        raise ValidationFailedError(
            f"the policy does not compile: {report['compile_error']}", code="compile_error"
        )
    return report


def _created(row: dict[str, Any], name: str, report: dict[str, Any]) -> dict[str, Any]:
    return {
        "pack": name,
        "version": row["version"],
        "source_hash": row["source_hash"],
        "tests_passed": row["tests_passed"],
        "created_at": row["created_at"],
        "created_by": None if row["created_by"] is None else str(row["created_by"]),
        "active": False,
        "test_report": report,
    }


def _version_row(conn: Any, name: str, number: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT v.*, p.name AS pack_name FROM policy_versions v "
        "JOIN policy_packs p ON p.id = v.pack_id WHERE p.name = %s AND v.version = %s",
        (name, number),
    ).fetchone()
    if row is None:
        raise NotFoundError("no such policy version")
    return dict(row)


# --------------------------------------------------------------------------------------- listing


@router.get("/api/policies/templates")
def list_templates(principal: User) -> dict[str, Any]:  # noqa: ARG001
    """Starter packs for the editor."""
    return {"items": reg.templates()}


@router.get("/api/policies")
def list_policies(principal: User, state: State) -> dict[str, Any]:
    """Every pack of the org with its active and latest version."""
    with state.db.tx(principal.org_id) as conn:
        rows = conn.execute(
            "SELECT p.name, av.version AS active_version, lv.version AS latest_version, "
            "GREATEST(p.created_at, lv.created_at, a.deployed_at) AS updated_at "
            "FROM policy_packs p "
            "LEFT JOIN policy_active a ON a.pack_id = p.id "
            "LEFT JOIN policy_versions av ON av.id = a.version_id "
            "LEFT JOIN LATERAL (SELECT version, created_at FROM policy_versions "
            "  WHERE pack_id = p.id ORDER BY version DESC LIMIT 1) lv ON true "
            "ORDER BY p.name"
        ).fetchall()
    return {"items": rows}


# ------------------------------------------------------------------------------------- simulate


@router.post("/api/policies/simulate", dependencies=[WRITERS, EVAL_SLOT])
def simulate(body: reg.SimulateBody) -> dict[str, Any]:
    """Evaluate ad-hoc input(s) against unsaved sources. Persists nothing."""
    if (body.input is None) == (body.cases is None):
        raise ValidationFailedError("send exactly one of `input` or `cases`", code="invalid_input")
    reg.validate_sources(body.files, body.query)
    cases: list[reg.SimCase]
    if body.cases is not None:
        cases = body.cases
    else:
        assert body.input is not None  # noqa: S101
        cases = [reg.SimCase(name="input", input=body.input)]
    reg.validate_cases(cases)
    report = reg.evaluate_cases(body.files, body.query, cases)
    return {
        "compile_error": report["compile_error"],
        "compiled": report["compiled"],
        "source_hash": report["source_hash"],
        "total": report["total"],
        "passed_count": report["passed_count"],
        "all_passed": report["all_passed"],
        "results": report["cases"],
    }


# --------------------------------------------------------------------------------- create / read


@router.post("/api/policies", status_code=201, dependencies=[WRITERS, EVAL_SLOT])
def create_pack(body: reg.CreateBody, principal: User, state: State) -> dict[str, Any]:
    """Create a pack and its first version. The version is tested but not deployed."""
    reg.validate_name(body.name)
    report = _evaluate(body)
    try:
        with state.db.tx(principal.org_id) as conn:
            pack = conn.execute(
                "INSERT INTO policy_packs (org_id, name) VALUES (%s, %s) RETURNING id",
                (principal.org_id, body.name),
            ).fetchone()
            assert pack is not None  # noqa: S101
            row = _insert_version(
                conn,
                org_id=principal.org_id,
                pack_id=pack["id"],
                version=1,
                body=body,
                report=report,
                user_id=principal.user_id,
            )
    except psycopg.errors.UniqueViolation as exc:
        raise ConflictError("a policy pack with this name already exists") from exc
    return _created(row, body.name, report)


@router.get("/api/policies/{name}/versions")
def list_versions(name: str, principal: User, state: State) -> dict[str, Any]:
    """The versions of a pack, newest first."""
    with state.db.tx(principal.org_id) as conn:
        pack = conn.execute("SELECT id FROM policy_packs WHERE name = %s", (name,)).fetchone()
        if pack is None:
            raise NotFoundError("no such policy pack")
        rows = conn.execute(
            "SELECT v.version, v.source_hash, v.tests_passed, v.created_at, v.created_by, "
            "(a.version_id = v.id) AS active "
            "FROM policy_versions v LEFT JOIN policy_active a ON a.pack_id = v.pack_id "
            "WHERE v.pack_id = %s ORDER BY v.version DESC",
            (pack["id"],),
        ).fetchall()
    items = [
        {**r, "active": bool(r["active"]), "created_by": r["created_by"] and str(r["created_by"])}
        for r in rows
    ]
    return {"items": items}


@router.post("/api/policies/{name}/versions", status_code=201, dependencies=[WRITERS, EVAL_SLOT])
def create_version(name: str, body: reg.PackBody, principal: User, state: State) -> dict[str, Any]:
    """Add version n+1. Numbering is serialised by a lock on the pack row."""
    report = _evaluate(body)
    with state.db.tx(principal.org_id) as conn:
        pack = _lock_pack(conn, name)
        last = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS n FROM policy_versions WHERE pack_id = %s",
            (pack["id"],),
        ).fetchone()
        assert last is not None  # noqa: S101
        row = _insert_version(
            conn,
            org_id=principal.org_id,
            pack_id=pack["id"],
            version=last["n"] + 1,
            body=body,
            report=report,
            user_id=principal.user_id,
        )
    return _created(row, name, report)


@router.get("/api/policies/{name}/versions/{number}")
def get_version(name: str, number: int, principal: User, state: State) -> dict[str, Any]:
    """One version in full: sources, query, cases and the stored test report."""
    with state.db.tx(principal.org_id) as conn:
        row = _version_row(conn, name, number)
        active = _active(conn, row["pack_id"])
    return {
        "pack": row["pack_name"],
        "version": row["version"],
        "source_hash": row["source_hash"],
        "files": row["files"],
        "query": row["query"],
        "cases": row["cases"],
        "test_report": row["test_report"],
        "tests_passed": row["tests_passed"],
        "created_at": row["created_at"],
        "created_by": row["created_by"] and str(row["created_by"]),
        "active": active is not None and active["version_id"] == row["id"],
    }


# ------------------------------------------------------------------------------ deploy / rollback


def _refuse(code: str, message: str, report: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if report is not None:
        body["report"] = report
    return JSONResponse(body, status_code=409)


def _audit(state: Any, principal: Any, event: str, payload: dict[str, Any]) -> None:
    try:
        state.audit.append(
            tenant_id=principal.org_id, event_type=event, actor=principal.actor, payload=payload
        )
    except AuditError as exc:
        raise ConflictError(
            f"refused because the audit record could not be written: {exc}", code="audit_rejected"
        ) from exc


def _activate(
    conn: Any, org_id: str, pack_id: Any, version_id: Any, user_id: str | None, kind: str
) -> Any:
    row = conn.execute(
        "INSERT INTO policy_active (org_id, pack_id, version_id, deployed_by) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT (pack_id) DO UPDATE SET version_id = EXCLUDED.version_id,"
        " deployed_by = EXCLUDED.deployed_by, deployed_at = now() RETURNING deployed_at",
        (org_id, pack_id, version_id, user_id),
    ).fetchone()
    conn.execute(
        "INSERT INTO policy_deployments (org_id, pack_id, version_id, kind, deployed_by) "
        "VALUES (%s,%s,%s,%s,%s)",
        (org_id, pack_id, version_id, kind, user_id),
    )
    return row["deployed_at"]


def _deployed(name: str, version: int, digest: str, at: Any, principal: Any) -> dict[str, Any]:
    return {
        "pack": name,
        "version": version,
        "source_hash": digest,
        "deployed_at": at,
        "deployed_by": principal.user_id,
    }


@router.post(
    "/api/policies/{name}/versions/{number}/deploy",
    response_model=None,
    dependencies=[WRITERS, EVAL_SLOT],
)
def deploy(name: str, number: int, principal: User, state: State) -> Any:
    """Make a version active, but only if its cases exist and pass right now."""
    with state.db.tx(principal.org_id) as conn:
        row = _version_row(conn, name, number)
    cases = [reg.Case.model_validate(c) for c in row["cases"]]
    if not cases:
        return _refuse("no_tests", "a version with no test cases cannot be deployed")
    if reg.source_hash(row["files"]) != row["source_hash"]:
        raise IntegrityError("stored sources do not match their hash")
    stored = row["test_report"] or {}
    if not row["tests_passed"]:
        return _refuse("tests_failed", "this version's tests did not pass", stored)
    # Never trust the stored report: re-run the cases against the stored sources now.
    fresh = reg.evaluate_cases(row["files"], row["query"], cases)
    if stored.get("source_hash") != row["source_hash"] or not reg.tests_passed(fresh):
        return _refuse("tests_failed", "the cases do not pass against the stored sources", fresh)
    _audit(
        state,
        principal,
        "policy.deployed",
        {"pack": name, "version": number, "source_hash": row["source_hash"]},
    )
    with state.db.tx(principal.org_id) as conn:
        pack = _lock_pack(conn, name)
        at = _activate(conn, principal.org_id, pack["id"], row["id"], principal.user_id, "deploy")
    return _deployed(name, number, row["source_hash"], at, principal)


@router.post("/api/policies/{name}/rollback", response_model=None, dependencies=[WRITERS])
def rollback(name: str, principal: User, state: State) -> Any:
    """Re-activate the version that was active before the current one."""
    with state.db.tx(principal.org_id) as conn:
        pack = _lock_pack(conn, name)
        current = _active(conn, pack["id"])
        target = None
        if current is not None:
            target = conn.execute(
                "SELECT v.id, v.version, v.source_hash FROM policy_deployments d "
                "JOIN policy_versions v ON v.id = d.version_id "
                "WHERE d.pack_id = %s AND d.version_id <> %s ORDER BY d.seq DESC LIMIT 1",
                (pack["id"], current["version_id"]),
            ).fetchone()
    if current is None:
        return _refuse("nothing_active", "this pack has no active version to roll back from")
    if target is None:
        return _refuse("nothing_to_roll_back", "there is no earlier active version")
    _audit(
        state,
        principal,
        "policy.rolled_back",
        {
            "pack": name,
            "version": target["version"],
            "source_hash": target["source_hash"],
            "from_version": current["version"],
        },
    )
    with state.db.tx(principal.org_id) as conn:
        pack = _lock_pack(conn, name)
        now = _active(conn, pack["id"])
        if now is None or now["version_id"] != current["version_id"]:
            return _refuse("conflict", "the active version changed during the rollback; retry")
        at = _activate(
            conn, principal.org_id, pack["id"], target["id"], principal.user_id, "rollback"
        )
    return _deployed(name, target["version"], target["source_hash"], at, principal)


# ------------------------------------------------------------------------------------ agent API


@router.get("/v1/policies/{name}/active")
def active_bundle(name: str, principal: Agent, state: State) -> dict[str, Any]:
    """The deployed bundle: what ``keelgate.cloud.CloudPolicyEngine`` downloads and evaluates."""
    with state.db.tx(principal.org_id) as conn:
        row = conn.execute(
            "SELECT p.name, v.version, v.files, v.query, v.source_hash, a.deployed_at "
            "FROM policy_packs p JOIN policy_active a ON a.pack_id = p.id "
            "JOIN policy_versions v ON v.id = a.version_id WHERE p.name = %s",
            (name,),
        ).fetchone()
    if row is None:
        raise NotFoundError("no policy version is deployed under this name")
    if reg.source_hash(row["files"]) != row["source_hash"]:
        raise IntegrityError("stored sources do not match their hash")
    return dict(row)
