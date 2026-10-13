// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { buildCsp } from "@/lib/csp";
import { proxy } from "@/proxy";

function get(path: string, cookie?: string): NextRequest {
  return new NextRequest(`http://localhost:3000${path}`, cookie ? { headers: { cookie } } : undefined);
}
const SESSION = "kg_session=kgs_" + "C".repeat(43);

describe("proxy (middleware)", () => {
  it("redirects a signed-out visitor to /login and remembers where they were going", () => {
    const res = proxy(get("/approvals"));
    expect(res.status).toBe(307);
    const loc = new URL(res.headers.get("location") ?? "");
    expect(loc.pathname).toBe("/login");
    expect(loc.searchParams.get("next")).toBe("/approvals");
  });

  it("does not add ?next for the home page", () => {
    const res = proxy(get("/"));
    expect(new URL(res.headers.get("location") ?? "").search).toBe("");
  });

  it("lets signed-out visitors see login, signup and the SSO pages", () => {
    for (const p of ["/login", "/signup", "/sso/complete", "/sso/error"]) {
      expect(proxy(get(p)).headers.get("location"), p).toBeNull();
    }
  });

  it("sends a signed-in user away from /login and /signup", () => {
    for (const p of ["/login", "/signup"]) {
      const res = proxy(get(p, SESSION));
      expect(res.status).toBe(307);
      expect(new URL(res.headers.get("location") ?? "").pathname).toBe("/");
    }
  });

  it("lets a signed-in user through to app pages", () => {
    expect(proxy(get("/audit", SESSION)).headers.get("location")).toBeNull();
  });

  it("keeps a signed-in user on the SSO completion page", () => {
    expect(proxy(get("/sso/complete", SESSION)).headers.get("location")).toBeNull();
  });

  it("sets a strict, nonce-based CSP and the other security headers", () => {
    const res = proxy(get("/audit", SESSION));
    const csp = res.headers.get("content-security-policy") ?? "";
    expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
    expect(csp).not.toContain("'unsafe-inline'");
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    expect(res.headers.get("referrer-policy")).toBe("strict-origin-when-cross-origin");
    expect(res.headers.get("x-frame-options")).toBe("DENY");
    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  it("uses a fresh nonce for every request", () => {
    const a = proxy(get("/audit", SESSION)).headers.get("content-security-policy");
    const b = proxy(get("/audit", SESSION)).headers.get("content-security-policy");
    expect(a).not.toEqual(b);
  });

  it("also puts security headers on redirects", () => {
    expect(proxy(get("/team")).headers.get("x-content-type-options")).toBe("nosniff");
  });
});

describe("buildCsp", () => {
  it("only relaxes script rules in development", () => {
    expect(buildCsp("n", true)).toContain("'unsafe-eval'");
    expect(buildCsp("n", false)).not.toContain("'unsafe-eval'");
    expect(buildCsp("n", false)).not.toContain("'unsafe-inline'");
  });
});
