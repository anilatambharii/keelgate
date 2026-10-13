import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MeProvider } from "@/components/me-context";
import { ToastProvider } from "@/components/toast";
import type { ApprovalRequest, Me, Role } from "@/lib/types";

export function makeMe(role: Role = "owner", tier: "ONE_CLICK" | "EXPLICIT_SIGNOFF" = "ONE_CLICK"): Me {
  return {
    user: { id: "u1", email: "a@example.com", role, approval_tier: tier },
    org: {
      id: "11111111-1111-1111-1111-111111111111",
      name: "Acme",
      slug: "acme-1",
      plan: "team",
      plan_name: "Team",
      features: ["sso"],
      billing_status: "trialing",
      trial_ends_at: new Date(Date.now() + 5 * 86_400_000).toISOString(),
      residency: "us",
    },
  };
}

export function makeRequest(over: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    request_id: "req-1",
    tenant_id: "t",
    agent_id: "agent-7",
    tool_name: "transfer_funds",
    args_hash: "a".repeat(64),
    tier: "ONE_CLICK",
    evidence: {
      tool: "transfer_funds",
      args: { amount: 120, to: "acct-9" },
      rationale: "Pay the vendor invoice.",
      sources: [{ uri: "https://example.com/inv/1", title: "Invoice 1" }],
      confidence: 0.82,
      verifier_flags: ["amount_near_limit"],
      policy_reasons: ["amount above auto limit"],
      policy_version: "v3",
    },
    evidence_hash: "deadbeef".repeat(8),
    status: "PENDING",
    created_at: new Date(Date.now() - 60_000).toISOString(),
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    decided_by: null,
    decided_at: null,
    decision_note: null,
    signoff_code_required: false,
    signoff_code: null,
    ...over,
  };
}

export function renderWithApp(ui: ReactElement, me: Me = makeMe()) {
  return render(
    <ToastProvider>
      <MeProvider me={me}>{ui}</MeProvider>
    </ToastProvider>,
  );
}

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}
