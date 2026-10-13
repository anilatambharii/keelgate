/** Logic of the dashboard's own auth route handlers (login, signup, SSO complete, logout). */
import { jsonError } from "./bff";
import { controlPlaneUrl } from "./config";
import { isSameOrigin } from "./origin";
import { clearCookieHeader, sessionCookieHeader, tokenFromCookieHeader } from "./session";

type Fetch = typeof fetch;
export interface AuthDeps {
  fetchImpl?: Fetch;
  baseUrl?: string;
}

const TOKEN_RE = /^kgs_[A-Za-z0-9_-]{20,200}$/;

function okResponse(token: string, extra: Record<string, unknown> = {}): Response {
  const headers = new Headers({ "content-type": "application/json", "cache-control": "no-store" });
  headers.append("set-cookie", sessionCookieHeader(token));
  // The token itself is deliberately never part of the JSON body.
  return new Response(JSON.stringify({ ok: true, ...extra }), { status: 200, headers });
}

async function readJson(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const data: unknown = await req.json();
    return data && typeof data === "object" && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

async function callControlPlane(
  deps: AuthDeps,
  path: string,
  init: RequestInit,
): Promise<Response | null> {
  const base = (deps.baseUrl ?? controlPlaneUrl()).replace(/\/+$/, "");
  try {
    return await (deps.fetchImpl ?? fetch)(`${base}${path}`, {
      ...init,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    return null;
  }
}

/** Relay a control-plane error body verbatim (it is already {"error": {code, message}}). */
async function relayError(upstream: Response): Promise<Response> {
  let code = `http_${upstream.status}`;
  let message = "Request failed";
  try {
    const body = (await upstream.json()) as {
      error?: { code?: string; message?: string };
      detail?: { loc?: unknown[]; msg?: string }[];
    };
    if (body.error?.code) code = body.error.code;
    if (body.error?.message) message = body.error.message;
    else if (Array.isArray(body.detail)) {
      code = "invalid_input";
      message = body.detail.map((d) => d.msg ?? "invalid").join("; ");
    }
  } catch {
    /* keep defaults */
  }
  // A failed login must not be mistaken for an expired session by the client.
  const status = upstream.status === 401 ? 401 : upstream.status;
  return jsonError(status, code, message);
}

function guard(req: Request): Response | null {
  if (req.method !== "POST") return jsonError(405, "method_not_allowed", "Method not allowed");
  if (!isSameOrigin(req.headers, new URL(req.url).host)) {
    return jsonError(403, "cross_origin", "Cross-origin requests are not allowed");
  }
  return null;
}

export async function login(req: Request, deps: AuthDeps = {}): Promise<Response> {
  const blocked = guard(req);
  if (blocked) return blocked;
  const body = await readJson(req);
  if (!body || typeof body.email !== "string" || typeof body.password !== "string") { // pragma: allowlist secret
    return jsonError(422, "invalid_input", "Enter your email and password");
  }
  const upstream = await callControlPlane(deps, "/api/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: body.email, password: body.password }),
  });
  if (!upstream) return jsonError(502, "upstream_unavailable", "The control plane could not be reached");
  if (!upstream.ok) return relayError(upstream);
  const data = (await upstream.json()) as { token?: string };
  if (!data.token || !TOKEN_RE.test(data.token)) {
    return jsonError(502, "bad_upstream", "Unexpected response from the control plane");
  }
  return okResponse(data.token);
}

export async function signup(req: Request, deps: AuthDeps = {}): Promise<Response> {
  const blocked = guard(req);
  if (blocked) return blocked;
  const body = await readJson(req);
  if (
    !body ||
    typeof body.org_name !== "string" ||
    typeof body.email !== "string" ||
    typeof body.password !== "string" // pragma: allowlist secret
  ) {
    return jsonError(422, "invalid_input", "Enter an organisation name, email and password");
  }
  const upstream = await callControlPlane(deps, "/api/signup", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ org_name: body.org_name, email: body.email, password: body.password }),
  });
  if (!upstream) return jsonError(502, "upstream_unavailable", "The control plane could not be reached");
  if (!upstream.ok) return relayError(upstream);
  const data = (await upstream.json()) as { token?: string; org?: { name?: string; slug?: string } };
  if (!data.token || !TOKEN_RE.test(data.token)) {
    return jsonError(502, "bad_upstream", "Unexpected response from the control plane");
  }
  return okResponse(data.token, { org: { name: data.org?.name, slug: data.org?.slug } });
}

/** SSO finish: the page posts the token it read from the URL fragment. We verify it, then store it. */
export async function ssoComplete(req: Request, deps: AuthDeps = {}): Promise<Response> {
  const blocked = guard(req);
  if (blocked) return blocked;
  const body = await readJson(req);
  const token = body && typeof body.token === "string" ? body.token : "";
  if (!TOKEN_RE.test(token)) return jsonError(422, "invalid_token", "That sign-in token is not valid");
  const upstream = await callControlPlane(deps, "/api/me", {
    method: "GET",
    headers: { authorization: `Bearer ${token}` },
  });
  if (!upstream) return jsonError(502, "upstream_unavailable", "The control plane could not be reached");
  if (!upstream.ok) return jsonError(401, "invalid_token", "That sign-in token is not valid");
  return okResponse(token);
}

export async function logout(req: Request, deps: AuthDeps = {}): Promise<Response> {
  const blocked = guard(req);
  if (blocked) return blocked;
  const token = tokenFromCookieHeader(req.headers.get("cookie"));
  if (token) {
    await callControlPlane(deps, "/api/logout", {
      method: "POST",
      headers: { authorization: `Bearer ${token}` },
    });
  }
  const headers = new Headers({ "content-type": "application/json", "cache-control": "no-store" });
  headers.append("set-cookie", clearCookieHeader());
  return new Response(JSON.stringify({ ok: true }), { status: 200, headers });
}
