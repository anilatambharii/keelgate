import type { ApprovalRequest, ApprovalTier, Role } from "./types";

export const DEFAULT_DECIDER_ROLES: Role[] = ["owner", "admin", "approver"];

export interface Decider {
  role: Role;
  approval_tier: ApprovalTier;
}

export interface Ability {
  can: boolean;
  /** Plain-language reason shown when `can` is false. */
  reason: string | null;
}

/**
 * Whether this user may decide this request, as far as the dashboard can tell. The server is the
 * authority (org routing rules can narrow it further); this only explains the obvious cases early.
 */
export function decideAbility(user: Decider, req: ApprovalRequest, now: Date = new Date()): Ability {
  if (req.status !== "PENDING") {
    return { can: false, reason: `This request is already ${req.status.toLowerCase()}.` };
  }
  if (new Date(req.expires_at).getTime() <= now.getTime()) {
    return { can: false, reason: "This request has expired. The agent must ask again." };
  }
  if (!DEFAULT_DECIDER_ROLES.includes(user.role)) {
    return {
      can: false,
      reason: `Your role (${user.role}) can view approvals but cannot decide them. Ask an approver, admin or owner.`,
    };
  }
  if (req.tier === "EXPLICIT_SIGNOFF" && user.approval_tier !== "EXPLICIT_SIGNOFF") {
    return {
      can: false,
      reason:
        "This request needs explicit sign-off, and you are only cleared for one-click approvals. An owner can raise your clearance on the Team page.",
    };
  }
  return { can: true, reason: null };
}

/** The sign-off code the API supplies (falls back to the first 8 hex of the evidence hash). */
export function signoffCodeOf(req: ApprovalRequest): string {
  return req.signoff_code ?? (req.evidence_hash ?? "").slice(0, 8);
}
