"""Claim-then-complete idempotency for POSTs that carry an ``Idempotency-Key``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from keelgate_cloud.errors import ConflictError

if TYPE_CHECKING:
    from keelgate_cloud.state import AppState

_PENDING: Final = {"pending": True}


def claim(state: AppState, org_id: str, scope: str, key: str) -> dict[str, Any] | None:
    """Claim the key. Returns the stored response if the request already completed.

    Returns ``None`` when this caller now owns the key and must run the request and then call
    :func:`complete`. A request that is still running elsewhere is a conflict the caller retries.
    """
    with state.db.tx(org_id) as conn:
        inserted = conn.execute(
            "INSERT INTO idempotency_keys (org_id, scope, key, response) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (org_id, scope, key, json.dumps(_PENDING)),
        )
        if inserted.rowcount:
            return None
        row = conn.execute(
            "SELECT response FROM idempotency_keys WHERE scope = %s AND key = %s", (scope, key)
        ).fetchone()
    stored: dict[str, Any] = row["response"] if row else {}
    if stored == _PENDING:
        raise ConflictError("the same request is still being processed", code="request_in_progress")
    return stored


def complete(state: AppState, org_id: str, scope: str, key: str, response: dict[str, Any]) -> None:
    """Record the response a claimed key should replay."""
    with state.db.tx(org_id) as conn:
        conn.execute(
            "UPDATE idempotency_keys SET response = %s WHERE scope = %s AND key = %s",
            (json.dumps(response), scope, key),
        )
