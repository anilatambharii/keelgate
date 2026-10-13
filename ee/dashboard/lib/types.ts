/** Response types of the Keelgate Cloud control plane API (`/api/...`). */

export type Role = "owner" | "admin" | "approver" | "viewer";
export type ApprovalTier = "ONE_CLICK" | "EXPLICIT_SIGNOFF";
export type ApprovalStatus = "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "CONSUMED";
export type BillingStatus = string;

export interface ApiErrorBody {
  error: { code: string; message: string };
}

export interface Org {
  id: string;
  name: string;
  slug: string;
  plan: string;
  plan_name: string;
  features: string[];
  billing_status: BillingStatus;
  trial_ends_at: string | null;
  residency: string;
}

export interface Me {
  user: { id: string; email: string; role: Role; approval_tier: ApprovalTier };
  org: Org;
}

export interface SignupResponse {
  token: string;
  org: Org;
}

export interface ApiKey {
  id: string;
  key_prefix: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  [k: string]: unknown;
}

export interface CreatedApiKey extends ApiKey {
  key: string;
}

export interface TeamUser {
  id: string;
  email: string;
  display_name?: string | null;
  role: Role;
  approval_tier: ApprovalTier;
  created_at: string;
  disabled_at: string | null;
}

// ---------------------------------------------------------------------------- usage

export interface Usage {
  period: string;
  plan: string;
  usage: Record<string, number> & {
    gated_actions: number;
  };
  agents: number;
  limits: {
    gated_actions: number | null;
    max_agents: number | null;
    trace_retention_days: number | null;
    audit_min_retention_days: number | null;
    rate_limit_per_minute: number;
  };
  gated_actions_remaining: number | null;
  daily: { day: string; metric: string; quantity: number }[];
}

export interface TraceSummary {
  trace_id: string;
  spans: number;
  start_ns: number | null;
  end_ns: number | null;
  has_error: boolean;
  ingested_at: string;
  root_name: string | null;
}

export interface Span {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  start_ns: number | null;
  end_ns: number | null;
  status: string | null;
  attributes: Record<string, unknown> | null;
}

export interface TraceDetail {
  trace_id: string;
  spans: Span[];
}

// ------------------------------------------------------------------------- approvals

export interface EvidenceSource {
  uri: string;
  title?: string | null;
  retrieved_at?: string | null;
  sha256?: string | null;
}

export interface EvidenceBundle {
  tool: string;
  args: Record<string, unknown>;
  rationale: string;
  sources: EvidenceSource[];
  confidence: number | null;
  verifier_flags: string[];
  policy_reasons: string[];
  policy_version: string;
}

export interface ApprovalRequest {
  request_id: string;
  tenant_id: string;
  agent_id: string;
  tool_name: string;
  args_hash: string;
  tier: ApprovalTier;
  evidence: EvidenceBundle;
  evidence_hash: string;
  status: ApprovalStatus;
  created_at: string;
  expires_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  signoff_code_required: boolean;
  /** The code an approver must type back; present only for EXPLICIT_SIGNOFF requests. */
  signoff_code: string | null;
}

export interface ApprovalRoute {
  id: string;
  priority: number;
  tool_pattern: string;
  min_tier: ApprovalTier;
  allowed_roles: Role[];
  escalate_to: ApprovalTier | null;
  enabled: boolean;
}

export interface ApprovalRouteInput {
  priority: number;
  tool_pattern: string;
  min_tier: ApprovalTier;
  allowed_roles: Role[];
  escalate_to: ApprovalTier | null;
  enabled: boolean;
}

// --------------------------------------------------------------------------- billing

export interface PlanInfo {
  id: string;
  name: string;
  description: string;
  price_monthly_cents: number | null;
  included_gated_actions: number | null;
  features: string[];
  self_serve: boolean;
  trial_days: number;
}

export interface BillingInfo {
  plan: string;
  billing_status: BillingStatus;
  trial_ends_at: string | null;
  subscription: { id: string; status: string; current_period_end: string | number | null } | null;
  plans: PlanInfo[];
  portal_available: boolean;
}

export interface InvoiceLine {
  description: string;
  quantity: number;
  unit_amount_cents: number;
  amount_cents: number;
}

export interface InvoicePreview {
  currency: string;
  period: { id: string; start: string; end: string };
  lines: InvoiceLine[];
  subtotal_cents: number;
  total_cents: number;
  source: "local" | "stripe";
  notes?: string[];
  stripe_preview?: {
    source: "stripe";
    available: boolean;
    currency?: string;
    subtotal_cents?: number;
    total_cents?: number;
    lines?: { description: string; quantity: number; amount_cents: number }[];
  } | null;
}

// --------------------------------------------------------------------------- policies

export interface PolicyListItem {
  name: string;
  active_version: number | null;
  latest_version: number | null;
  updated_at: string | null;
}

export interface PolicyVersion {
  pack?: string;
  version: number;
  source_hash?: string;
  files?: Record<string, string>;
  query?: string;
  cases?: PolicyCase[];
  tests_passed: boolean | null;
  test_report?: TestReport | null;
  created_at?: string;
  created_by?: string | null;
  active?: boolean;
}

export interface CaseResult {
  name: string;
  /** null for a simulation case with no expectation. */
  passed: boolean | null;
  expected: { effect?: string; approval_tier?: string | null } | null;
  actual: { effect?: string; reasons?: string[]; approval_tier?: string | null } | null;
  error?: string | null;
}

export interface TestReport {
  compiled: boolean;
  compile_error: string | null;
  source_hash?: string;
  total: number;
  passed_count: number;
  all_passed: boolean;
  cases?: CaseResult[];
}

export interface SimulationResult {
  compiled: boolean;
  compile_error: string | null;
  source_hash?: string;
  total: number;
  passed_count: number;
  all_passed: boolean;
  results: CaseResult[];
}

/** A case as the editor holds it: expect is optional when simulating. */
export interface PolicyCase {
  name: string;
  input: Record<string, unknown>;
  expect?: { effect: string; approval_tier?: string | null };
}

export interface PolicyTemplate {
  name: string;
  description?: string;
  files: Record<string, string>;
  query: string;
  cases?: PolicyCase[];
}

// ------------------------------------------------------------------------------ audit

export interface AuditRecord {
  seq: number;
  timestamp: string;
  event_type: string;
  actor: string;
  payload: unknown;
  hash: string;
  prev_hash: string | null;
}

export interface AuditPage {
  records: AuditRecord[];
  has_more: boolean;
}

// ---------------------------------------------------------------------------- settings

export interface OrgSettings {
  name: string;
  slug: string;
  plan: string;
  residency: string;
  residency_enforced: boolean;
  residency_note?: string;
  retention: {
    trace_retention_days: number | null;
    audit_min_retention_days: number | null;
    audit_immutable: boolean;
  };
  features: string[];
}

export interface ResidencyRegions {
  regions: { id: string; label: string }[];
  enforced: boolean;
  note?: string;
}

export interface SsoConfig {
  issuer: string;
  client_id: string;
  client_secret_set: boolean;
  allowed_domains: string[];
  default_role: "approver" | "viewer";
  enabled: boolean;
  updated_at?: string;
  redirect_uri?: string;
}
