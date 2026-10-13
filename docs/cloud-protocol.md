# Control-plane protocol

`keelgate.cloud` speaks a small HTTP protocol. Keelgate Cloud implements it, and so can a service you
run yourself. All paths are under the control plane's base URL; every request carries
`Authorization: Bearer <api key>`. **The key names the tenant**: a request whose `tenant_id` differs
from the key's tenant is refused with `403 tenant_mismatch`.

Errors always have the shape `{"error": {"code": "...", "message": "..."}}`. Codes are stable:
`invalid_api_key` (401), `tenant_mismatch` (403), `subscription_inactive` (402), `plan_required`
(402), `quota_exceeded` (429, a plan limit), `agent_cap_reached` (429), `rate_limited` (429),
`event_not_accepted` (422), `invalid_input` (422), `approval_not_found` (404), and the other
`approval_*` codes of `keelgate.approvals`.

A `POST` that may be retried carries an `Idempotency-Key`; the server executes it once and replays
the stored response.

## Identity

`GET /v1/whoami` returns `{tenant_id, key_id, plan}` for the calling key, so a client does not need to be told its tenant.

## Audit

| Call | Meaning |
|---|---|
| `POST /v1/audit/events` | Body `{tenant_id, event_type, actor, payload_json}`. The **server** assigns `seq`, `timestamp`, `prev_hash` and `hash` and returns the stored `AuditRecord`. Approval events (`approval.*`) are refused: the server writes those itself. |
| `GET /v1/audit/records?after_seq=&limit=` | A page `{records, has_more}`, oldest first. |
| `GET /v1/audit/head` | `{seq, hash}`, or `null` for an empty chain. |

Because the server chains the records, the exported chain verifies with the open-source
`keelgate.audit.verify_chain`, which needs nothing from the server.

## Approvals

| Call | Meaning |
|---|---|
| `POST /v1/approvals` | Park an action: `{tenant_id, agent_id, tool_name, args_hash, tier, evidence, ttl_seconds}`. The org's routing rules may raise the tier, never lower it. |
| `GET /v1/approvals/{id}` | One request. Another tenant's id is `404`. |
| `GET /v1/approvals?status=PENDING` | The tenant's requests in a state. |
| `POST /v1/approvals/{id}/consume` | Spend an approved request once, for exactly this agent, tool and argument hash. |

Humans decide requests through the dashboard API, never through this agent API.

## Policy and traces

| Call | Meaning |
|---|---|
| `GET /v1/policies/{pack}/active` | The deployed bundle `{files, query, source_hash, version}`. `source_hash` is `keelgate.policy`'s hash of the sources; the client checks it and evaluates the pack **locally**. |
| `POST /v1/traces` | `{spans: [{trace_id, span_id, parent_span_id, name, start_ns, end_ns, status, attributes}]}`, at most 1000 spans. Returns `{accepted}`. |
