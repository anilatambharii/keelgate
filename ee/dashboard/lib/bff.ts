/**
 * The backend-for-frontend forwarder. Browser requests to `/bff/api/...` are forwarded to the
 * control plane with the session token from the httpOnly cookie. Nothing else is ever forwarded:
 * only `/api/` paths (never `/v1/`), only a short allow-list of request headers, no cookies.
 */
import { controlPlaneUrl } from "./config";
import { isSameOrigin, SAFE_METHODS } from "./origin";
import { clearCookieHeader, tokenFromCookieHeader } from "./session";

const METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const FORWARD_REQUEST_HEADERS = ["content-type", "accept", "idempotency-key"];
const PASS_RESPONSE_HEADERS = ["content-type", "content-disposition"];
const MAX_BODY_BYTES = 2 * 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 30_000;

export function jsonError(status: number, code: string, message: string, extra?: HeadersInit): Response {
  const headers = new Headers(extra);
  headers.set("content-type", "application/json");
  headers.set("cache-control", "no-store");
  return new Response(JSON.stringify({ error: { code, message } }), { status, headers });
}

/**
 * Validate the path segments after `/bff/`. Returns the upstream path (`/api/...`) or null.
 * Rejects anything that is not under `api/`, and any dot-segment, empty segment or segment that
 * smuggles a slash, backslash, NUL or percent-encoding of one.
 */
export function upstreamPath(segments: string[]): string | null {
  if (segments.length < 2 || segments[0] !== "api") return null;
  const clean: string[] = [];
  for (const raw of segments) {
    let seg: string;
    try {
      seg = decodeURIComponent(raw);
    } catch {
      return null;
    }
    if (!seg || seg === "." || seg === ".." || /[\\/\0?#]/.test(seg) || /[\\/\0?#]/.test(raw)) {
      return null;
    }
    clean.push(encodeURIComponent(seg));
  }
  return "/" + clean.join("/");
}

export interface ForwardOptions {
  fetchImpl?: typeof fetch;
  baseUrl?: string;
}

export async function forward(
  req: Request,
  segments: string[],
  opts: ForwardOptions = {},
): Promise<Response> {
  const method = req.method.toUpperCase();
  if (!METHODS.has(method)) return jsonError(405, "method_not_allowed", "Method not allowed");

  const path = upstreamPath(segments);
  if (!path) return jsonError(404, "not_found", "Not found");

  const urlHost = new URL(req.url).host;
  if (!SAFE_METHODS.has(method) && !isSameOrigin(req.headers, urlHost)) {
    return jsonError(403, "cross_origin", "Cross-origin requests are not allowed");
  }

  const token = tokenFromCookieHeader(req.headers.get("cookie"));
  if (!token) {
    return jsonError(401, "unauthenticated", "Sign in to continue", {
      "x-kg-login-required": "1",
    });
  }

  const headers = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const v = req.headers.get(name);
    if (v) headers.set(name, v);
  }
  headers.set("authorization", `Bearer ${token}`);

  let body: ArrayBuffer | undefined;
  if (method !== "GET") {
    body = await req.arrayBuffer();
    if (body.byteLength > MAX_BODY_BYTES) {
      return jsonError(413, "payload_too_large", "Request body is too large");
    }
    if (body.byteLength === 0) body = undefined;
  }

  const search = new URL(req.url).search;
  const target = `${(opts.baseUrl ?? controlPlaneUrl()).replace(/\/+$/, "")}${path}${search}`;
  const doFetch = opts.fetchImpl ?? fetch;

  let upstream: Response;
  try {
    upstream = await doFetch(target, {
      method,
      headers,
      body,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch {
    return jsonError(502, "upstream_unavailable", "The control plane could not be reached");
  }

  if (upstream.status === 401) {
    // The session is gone or invalid: drop the cookie and tell the client to go to /login.
    const h = new Headers({ "x-kg-login-required": "1" });
    h.append("set-cookie", clearCookieHeader());
    return jsonError(401, "unauthenticated", "Your session has ended. Sign in again.", h);
  }
  if (upstream.status >= 300 && upstream.status < 400) {
    return jsonError(502, "upstream_redirect", "Unexpected redirect from the control plane");
  }

  const out = new Headers();
  for (const name of PASS_RESPONSE_HEADERS) {
    const v = upstream.headers.get(name);
    if (v) out.set(name, v);
  }
  out.set("cache-control", "no-store");
  out.set("x-content-type-options", "nosniff");
  // 204/205/304 must not carry a body.
  const noBody = upstream.status === 204 || upstream.status === 205 || upstream.status === 304;
  return new Response(noBody ? null : upstream.body, { status: upstream.status, headers: out });
}
