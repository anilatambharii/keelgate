"""Claim-then-complete idempotency for POSTs that carry an ``Idempotency-Key``.

A key is bound to a fingerprint of the request that first used it. Replaying the key with the same
request returns the stored response; reusing it for a different request is refused, so a key cannot
be used to make the server pretend an operation happened.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Final

from keelgate_cloud.errors import ConflictError

if TYPE_CHECKING:
    from keelgate_cloud.state import AppState

_PENDING: Final = "pending"


def fingerprint(request: Any) -> str:
    """A stable hash of a request body."""
    text = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def claim(
    state: AppState, org_id: str, scope: str, key: str, request_fingerprint: str
) -> dict[str, Any] | None:
    """Claim the key. Returns the stored response if the same request already completed.

    Returns ``None`` when this caller now owns the key and must run the request, then call
    :func:`complete` (or :func:`release` if it fails).
    """
    marker = {"state": _PENDING, "fp": request_fingerprint}
    with state.db.tx(org_id) as conn:
        inserted = conn.execute(
            "INSERT INTO idempotency_keys (org_id, scope, key, response) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (org_id, scope, key, json.dumps(marker)),
        )
        if inserted.rowcount:
            return None
        row = conn.execute(
            "SELECT response FROM idempotency_keys WHERE scope = %s AND key = %s", (scope, key)
        ).fetchone()
    stored: dict[str, Any] = row["response"] if row else {}
    if stored.get("fp") != request_fingerprint:
        raise ConflictError(
            "this idempotency key was already used for a different request",
            code="idempotency_key_reuse",
        )
    if stored.get("state") == _PENDING:
        raise ConflictError("the same request is still being processed", code="request_in_progress")
    response: dict[str, Any] = stored["response"]
    return response


def complete(
    state: AppState,
    org_id: str,
    scope: str,
    key: str,
    request_fingerprint: str,
    response: dict[str, Any],
) -> None:
    """Record the response a claimed key should replay."""
    with state.db.tx(org_id) as conn:
        conn.execute(
            "UPDATE idempotency_keys SET response = %s WHERE scope = %s AND key = %s",
            (
                json.dumps({"state": "done", "fp": request_fingerprint, "response": response}),
                scope,
                key,
            ),
        )


def release(state: AppState, org_id: str, scope: str, key: str) -> None:
    """Give up a claim after the request failed, so a corrected retry can proceed."""
    with state.db.tx(org_id) as conn:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE scope = %s AND key = %s "
            "AND response ->> 'state' = 'pending'",
            (scope, key),
        )
