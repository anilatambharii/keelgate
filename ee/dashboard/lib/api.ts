/**
 * Browser-side typed client for the control plane, via the dashboard's own BFF (`/bff/api/...`).
 * The browser never holds the session token: the BFF adds it from an httpOnly cookie.
 */
import type {
  ApiKey,
  ApprovalRequest,
  ApprovalRoute,
  ApprovalRouteInput,
  AuditPage,
  BillingInfo,
  CreatedApiKey,
  InvoicePreview,
  Me,
  OrgSettings,
  ResidencyRegions,
  PolicyCase,
  PolicyListItem,
  PolicyTemplate,
  PolicyVersion,
  SimulationResult,
  SsoConfig,
  TeamUser,
  TraceDetail,
  TraceSummary,
  Usage,
} from "./types";

export const BFF_PREFIX = "/bff";

/** Any failure of a request: the API's `{"error": {code, message}}`, or a transport problem. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  get isAuth(): boolean {
    return this.status === 401;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
}

let unauthenticatedHandler: (() => void) | null = defaultUnauthenticated;

function defaultUnauthenticated(): void {
  // Full page load: the route handler clears the dead cookie and redirects to /login.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  if (typeof window !== "undefined") window.location.assign("/auth/expired");
}

/** Replace what happens on a 401 (tests; the default sends the user to /login). */
export function setUnauthenticatedHandler(fn: (() => void) | null): void {
  unauthenticatedHandler = fn;
}

export function buildUrl(path: string, query?: Query): string {
  const url = `${BFF_PREFIX}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

/** Turn the body of a failed response into an ApiError. Understands FastAPI's 422 shape too. */
export function errorFrom(status: number, parsed: unknown, fallbackText = ""): ApiError {
  if (parsed && typeof parsed === "object") {
    const rec = parsed as Record<string, unknown>;
    const err = rec.error;
    if (err && typeof err === "object") {
      const e = err as Record<string, unknown>;
      const code = typeof e.code === "string" ? e.code : `http_${status}`;
      const message = typeof e.message === "string" ? e.message : `Request failed (${status})`;
      return new ApiError(status, code, message, rec);
    }
    if (Array.isArray(rec.detail)) {
      const msg = rec.detail
        .map((d) => {
          if (d && typeof d === "object") {
            const dd = d as { loc?: unknown[]; msg?: unknown };
            const where = Array.isArray(dd.loc) ? dd.loc.filter((p) => p !== "body").join(".") : "";
            return `${where ? where + ": " : ""}${String(dd.msg ?? "invalid")}`;
          }
          return String(d);
        })
        .join("; ");
      return new ApiError(status, "invalid_input", msg || "Invalid input", rec.detail);
    }
    if (typeof rec.detail === "string") return new ApiError(status, `http_${status}`, rec.detail);
  }
  const text = fallbackText.trim().slice(0, 200);
  return new ApiError(status, `http_${status}`, text || `Request failed (${status})`);
}

async function parseBody(res: Response): Promise<{ parsed: unknown; text: string }> {
  const text = await res.text();
  if (!text) return { parsed: undefined, text };
  try {
    return { parsed: JSON.parse(text), text };
  } catch {
    return { parsed: undefined, text };
  }
}

export async function request<T>(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers: opts.body !== undefined ? { "content-type": "application/json" } : undefined,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin",
      cache: "no-store",
      signal: opts.signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(0, "network_error", "Could not reach the server. Check your connection.");
  }
  const { parsed, text } = await parseBody(res);
  if (!res.ok) {
    const err = errorFrom(res.status, parsed, text);
    if (res.status === 401 && unauthenticatedHandler) unauthenticatedHandler();
    throw err;
  }
  return parsed as T;
}

/** POST to one of the dashboard's own auth route handlers (login, signup, sso). */
export async function authRequest<T = { ok: true }>(path: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the server. Check your connection.");
  }
  const { parsed, text } = await parseBody(res);
  if (!res.ok) throw errorFrom(res.status, parsed, text);
  return parsed as T;
}

/** Download a file through the BFF (exports). Returns the file name used. */
export async function downloadFile(path: string, query: Query, fallbackName: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), { credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the server. Check your connection.");
  }
  if (!res.ok) {
    const { parsed, text } = await parseBody(res);
    const err = errorFrom(res.status, parsed, text);
    if (res.status === 401 && unauthenticatedHandler) unauthenticatedHandler();
    throw err;
  }
  const name = filenameFromDisposition(res.headers.get("content-disposition")) ?? fallbackName;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return name;
}

export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
  if (!m) return null;
  try {
    const name = decodeURIComponent(m[1]).replace(/[\\/]/g, "_");
    return name || null;
  } catch {
    return null;
  }
}

const enc = encodeURIComponent;

export const api = {
  me: () => request<Me>("GET", "/api/me"),
  logout: () => authRequest("/auth/logout", {}),

  // keys
  listKeys: () => request<{ items: ApiKey[] }>("GET", "/api/keys"),
  createKey: (name: string, expires_in_days?: number | null) =>
    request<CreatedApiKey>("POST", "/api/keys", {
      body: { name, ...(expires_in_days ? { expires_in_days } : {}) },
    }),
  revokeKey: (id: string) => request<void>("DELETE", `/api/keys/${enc(id)}`),

  // team
  listUsers: () => request<{ items: TeamUser[] }>("GET", "/api/users"),
  inviteUser: (body: { email: string; role: string; password?: string; approval_tier: string }) =>
    request<TeamUser>("POST", "/api/users", { body }),
  disableUser: (id: string) => request<void>("DELETE", `/api/users/${enc(id)}`),
  setUserTier: (id: string, approval_tier: string) =>
    request<void>("PATCH", `/api/users/${enc(id)}`, { body: { approval_tier } }),

  // usage & traces
  usage: (period?: string) => request<Usage>("GET", "/api/usage", { query: { period } }),
  traces: (q: { q?: string; errors_only?: boolean; limit?: number; offset?: number }) =>
    request<{ items: TraceSummary[] }>("GET", "/api/traces", { query: q }),
  trace: (id: string) => request<TraceDetail>("GET", `/api/traces/${enc(id)}`),

  // approvals
  approvals: (status: string, signal?: AbortSignal) =>
    request<{ items: ApprovalRequest[] }>("GET", "/api/approvals", {
      query: { status, limit: 200 },
      signal,
    }),
  approve: (id: string, body: { note?: string; signoff_code?: string }) =>
    request<ApprovalRequest>("POST", `/api/approvals/${enc(id)}/approve`, { body }),
  reject: (id: string, body: { note?: string }) =>
    request<ApprovalRequest>("POST", `/api/approvals/${enc(id)}/reject`, { body }),
  routes: () => request<{ items: ApprovalRoute[] }>("GET", "/api/approval-routes"),
  createRoute: (body: ApprovalRouteInput) =>
    request<ApprovalRoute>("POST", "/api/approval-routes", { body }),
  deleteRoute: (id: string) => request<void>("DELETE", `/api/approval-routes/${enc(id)}`),

  // billing
  billing: () => request<BillingInfo>("GET", "/api/billing"),
  checkout: (plan: string) => request<{ url: string }>("POST", "/api/billing/checkout", { body: { plan } }),
  portal: () => request<{ url: string }>("POST", "/api/billing/portal", { body: {} }),
  invoicePreview: () => request<InvoicePreview>("GET", "/api/billing/invoice-preview"),

  // policies
  policies: () => request<{ items: PolicyListItem[] }>("GET", "/api/policies"),
  policyTemplates: () => request<{ items: PolicyTemplate[] }>("GET", "/api/policies/templates"),
  policyVersions: (name: string) =>
    request<{ items: PolicyVersion[] }>("GET", `/api/policies/${enc(name)}/versions`),
  policyVersion: (name: string, n: number) =>
    request<PolicyVersion>("GET", `/api/policies/${enc(name)}/versions/${n}`),
  createPolicy: (body: PolicyDraft & { name: string }) =>
    request<PolicyVersion>("POST", "/api/policies", { body }),
  createPolicyVersion: (name: string, body: PolicyDraft) =>
    request<PolicyVersion>("POST", `/api/policies/${enc(name)}/versions`, { body }),
  simulate: (body: { files: Record<string, string>; query: string; cases: PolicyCase[] }) =>
    request<SimulationResult>("POST", "/api/policies/simulate", { body }),
  deployPolicy: (name: string, n: number) =>
    request<unknown>("POST", `/api/policies/${enc(name)}/versions/${n}/deploy`, { body: {} }),
  rollbackPolicy: (name: string) =>
    request<unknown>("POST", `/api/policies/${enc(name)}/rollback`, { body: {} }),

  // audit
  audit: (q: {
    after_seq?: number;
    limit?: number;
    event_type?: string;
    actor?: string;
    newest_first?: boolean;
  }) => request<AuditPage>("GET", "/api/audit", { query: q }),
  exportAudit: (format: "json" | "csv") =>
    downloadFile("/api/audit/export", { format }, `keelgate-audit.${format}`),
  exportCompliance: () =>
    downloadFile("/api/audit/export/compliance", {}, "keelgate-compliance-export.zip"),

  // settings & sso
  settings: () => request<OrgSettings>("GET", "/api/settings"),
  updateSettings: (body: { name?: string; residency?: string }) =>
    request<OrgSettings>("PATCH", "/api/settings", { body }),
  residencyRegions: () => request<ResidencyRegions>("GET", "/api/settings/residency-regions"),
  sso: () => request<SsoConfig>("GET", "/api/sso"),
  saveSso: (body: Record<string, unknown>) => request<SsoConfig>("PUT", "/api/sso", { body }),
  deleteSso: () => request<void>("DELETE", "/api/sso"),
};

export interface PolicyDraft {
  files: Record<string, string>;
  query: string;
  cases: PolicyCase[];
}
