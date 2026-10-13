"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { decideAbility, signoffCodeOf, type Decider } from "@/lib/approvals";
import { countdown } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { safeJson, safeText } from "@/lib/sanitize";
import type { ApprovalRequest, ApprovalStatus } from "@/lib/types";
import { useToast } from "./toast";
import { Alert, Badge, Button, TextAreaField, TextField, UntrustedBox, fmtDate } from "./ui";

const STATUS_TONE: Record<ApprovalStatus, "warn" | "ok" | "danger" | "neutral"> = {
  PENDING: "warn",
  APPROVED: "ok",
  REJECTED: "danger",
  EXPIRED: "neutral",
  CONSUMED: "neutral",
};

export function StatusBadge({ status }: { status: ApprovalStatus }) {
  return <Badge tone={STATUS_TONE[status]}>{status}</Badge>;
}

/** Everything an approver needs to decide. All agent-derived fields are shown as labelled text. */
export function EvidenceView({ request }: { request: ApprovalRequest }) {
  const ev = request.evidence;
  const now = useNow();
  return (
    <div className="space-y-3 text-sm">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <dt className="text-muted">Status</dt>
        <dd>
          <StatusBadge status={request.status} />
        </dd>
        <dt className="text-muted">Tier</dt>
        <dd>
          <Badge tone={request.tier === "EXPLICIT_SIGNOFF" ? "danger" : "info"}>{request.tier}</Badge>
        </dd>
        <dt className="text-muted">Requested by agent</dt>
        <dd className="break-all font-mono text-xs">{safeText(request.agent_id)}</dd>
        <dt className="text-muted">Tool</dt>
        <dd className="break-all font-mono text-xs">{safeText(request.tool_name)}</dd>
        <dt className="text-muted">Requested</dt>
        <dd>{fmtDate(request.created_at)}</dd>
        <dt className="text-muted">Expires</dt>
        <dd>
          {fmtDate(request.expires_at)}{" "}
          {request.status === "PENDING" ? (
            <Badge tone="warn">
              <span data-testid="countdown">{countdown(request.expires_at, now)}</span> left
            </Badge>
          ) : null}
        </dd>
        {request.decided_by ? (
          <>
            <dt className="text-muted">Decided by</dt>
            <dd className="break-all">
              {safeText(request.decided_by)} at {fmtDate(request.decided_at)}
            </dd>
          </>
        ) : null}
        {request.decision_note ? (
          <>
            <dt className="text-muted">Decision note</dt>
            <dd className="whitespace-pre-wrap break-words">{safeText(request.decision_note)}</dd>
          </>
        ) : null}
      </dl>

      <div>
        <p className="mb-1 font-medium">Arguments the agent will run with</p>
        <pre
          tabIndex={0}
          aria-label="Tool arguments"
          className="max-h-72 overflow-auto rounded-md border border-warn/60 bg-warnbg/40 p-3 text-xs"
        >
          {safeJson(ev.args)}
        </pre>
        <p className="mt-1 text-xs text-muted">
          The approval is bound to exactly these arguments (hash{" "}
          <span className="font-mono">{safeText(request.args_hash).slice(0, 16)}</span>).
        </p>
      </div>

      <UntrustedBox label="Agent rationale">{safeText(ev.rationale) || "(none given)"}</UntrustedBox>

      <div>
        <p className="mb-1 font-medium">Policy</p>
        <p className="text-xs text-muted">Version {safeText(ev.policy_version) || "unknown"}</p>
        {ev.policy_reasons.length ? (
          <ul className="list-disc pl-5">
            {ev.policy_reasons.map((r, i) => (
              <li key={i}>{safeText(r)}</li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">No policy reasons recorded.</p>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <p className="mb-1 font-medium">Confidence</p>
          <p>{ev.confidence === null || ev.confidence === undefined ? "Not stated" : `${Math.round(ev.confidence * 100)}%`}</p>
        </div>
        <div>
          <p className="mb-1 font-medium">Verifier flags</p>
          {ev.verifier_flags.length ? (
            <ul className="flex flex-wrap gap-1">
              {ev.verifier_flags.map((f, i) => (
                <li key={i}>
                  <Badge tone="warn">{safeText(f)}</Badge>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">None</p>
          )}
        </div>
      </div>

      <div>
        <p className="mb-1 font-medium">Sources the agent cited</p>
        {ev.sources.length ? (
          <ul className="space-y-1">
            {ev.sources.map((s, i) => (
              <li key={i} className="rounded border border-line px-2 py-1">
                <p className="break-all">{safeText(s.title) || safeText(s.uri)}</p>
                {/* Shown as text on purpose: an agent-supplied address must not be one click away. */}
                <p className="break-all font-mono text-xs text-muted">{safeText(s.uri)}</p>
                {s.retrieved_at ? (
                  <p className="text-xs text-muted">Retrieved {fmtDate(s.retrieved_at)}</p>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">No sources.</p>
        )}
      </div>

      {request.signoff_code_required ? (
        <div className="rounded-md border border-line bg-surface2 p-2">
          <p className="text-xs text-muted">Sign-off code for this request (type it back to approve)</p>
          <p className="font-mono text-base" data-testid="signoff-code">
            {safeText(signoffCodeOf(request))}
          </p>
        </div>
      ) : null}

      <p className="text-xs text-muted">
        Evidence hash <span className="font-mono">{safeText(request.evidence_hash)}</span>
      </p>
    </div>
  );
}

export interface DecisionPanelProps {
  request: ApprovalRequest;
  user: Decider;
  /** Called right away with the status the request is expected to take (optimistic update). */
  onOptimistic?: (id: string, status: ApprovalStatus) => void;
  /** Called with the server's version once the decision is stored. */
  onDecided?: (updated: ApprovalRequest) => void;
  /** Called if the decision failed so the caller can undo the optimistic update. */
  onFailed?: (id: string) => void;
}

export function DecisionPanel({ request, user, onOptimistic, onDecided, onFailed }: DecisionPanelProps) {
  const toast = useToast();
  const now = useNow();
  const [note, setNote] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const ability = decideAbility(user, request, now);
  const needsCode = request.signoff_code_required;
  const codeOk = !needsCode || code.trim().length > 0;
  const disabled = !ability.can || busy !== null;

  async function decide(kind: "approve" | "reject") {
    if (!ability.can) return;
    if (kind === "approve" && !codeOk) return;
    setError(null);
    setBusy(kind);
    onOptimistic?.(request.request_id, kind === "approve" ? "APPROVED" : "REJECTED");
    try {
      const trimmed = note.trim() || undefined;
      const updated =
        kind === "approve"
          ? await api.approve(request.request_id, {
              note: trimmed,
              ...(needsCode ? { signoff_code: code.trim() } : {}),
            })
          : await api.reject(request.request_id, { note: trimmed });
      toast.success(kind === "approve" ? "Approved" : "Rejected");
      onDecided?.(updated);
    } catch (err) {
      onFailed?.(request.request_id);
      const msg = err instanceof Error ? err.message : "The decision could not be saved";
      setError(msg);
      toast.error(err);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section aria-label="Decision" className="space-y-3 rounded-md border border-line bg-surface2 p-3">
      <h3 className="font-semibold">Your decision</h3>
      {!ability.can ? (
        <Alert tone="warn" role="status">
          <span data-testid="decision-disabled-reason">{ability.reason}</span>
        </Alert>
      ) : null}
      {needsCode ? (
        <TextField
          label="Sign-off code"
          hint={
            <>
              This action needs explicit sign-off. After reading the evidence above, type the sign-off
              code shown at its end. It is friction against a reflex click, not proof you read it.
            </>
          }
          autoComplete="off"
          spellCheck={false}
          value={code}
          onChange={(e) => setCode(e.target.value)}
          disabled={!ability.can}
        />
      ) : null}
      <TextAreaField
        label="Note (optional, recorded in the audit log)"
        rows={2}
        maxLength={1000}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        disabled={!ability.can}
      />
      {error ? <Alert tone="danger">{error}</Alert> : null}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          disabled={disabled || !codeOk}
          onClick={() => decide("approve")}
          aria-describedby={needsCode && !codeOk ? "signoff-needed" : undefined}
        >
          {busy === "approve" ? "Approving..." : "Approve"}
        </Button>
        <Button variant="danger" disabled={disabled} onClick={() => decide("reject")}>
          {busy === "reject" ? "Rejecting..." : "Reject"}
        </Button>
      </div>
      {needsCode && !codeOk && ability.can ? (
        <p id="signoff-needed" className="text-xs text-muted">
          Enter the sign-off code to enable Approve. Reject never needs it.
        </p>
      ) : null}
    </section>
  );
}
