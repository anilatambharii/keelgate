// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { login, logout, signup, ssoComplete } from "@/lib/auth-routes";

const TOKEN = "kgs_" + "B".repeat(43);
const BASE = "http://cp.test";
const sameOrigin = { host: "dash.test", origin: "http://dash.test", "content-type": "application/json" };

function post(path: string, body: unknown, headers: Record<string, string> = sameOrigin): Request {
  return new Request(`http://dash.test${path}`, { method: "POST", body: JSON.stringify(body), headers });
}

const ok = (body: unknown, status = 200) =>
  vi.fn(
    async () =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("login", () => {
  it("stores the token in an httpOnly SameSite=Lax cookie and never in the body", async () => {
    const f = ok({ token: TOKEN });
    const res = await login(post("/auth/login", { email: "a@b.co", password: "pw" }), { // pragma: allowlist secret
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(200);
    const cookie = res.headers.get("set-cookie") ?? "";
    expect(cookie).toContain(`kg_session=${TOKEN}`);
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("SameSite=Lax");
    expect(cookie).toContain("Path=/");
    expect(await res.text()).not.toContain(TOKEN);
  });

  it("marks the cookie Secure in production", async () => {
    vi.stubEnv("NODE_ENV", "production");
    const res = await login(post("/auth/login", { email: "a@b.co", password: "pw" }), { // pragma: allowlist secret
      fetchImpl: ok({ token: TOKEN }),
      baseUrl: BASE,
    });
    expect(res.headers.get("set-cookie")).toContain("Secure");
  });

  it("is not Secure in development unless asked", async () => {
    vi.stubEnv("NODE_ENV", "development");
    const res = await login(post("/auth/login", { email: "a@b.co", password: "pw" }), { // pragma: allowlist secret
      fetchImpl: ok({ token: TOKEN }),
      baseUrl: BASE,
    });
    expect(res.headers.get("set-cookie")).not.toContain("Secure");
  });

  it("relays a wrong-password error without setting a cookie", async () => {
    const f = ok({ error: { code: "invalid_credentials", message: "wrong email or password" } }, 401);
    const res = await login(post("/auth/login", { email: "a@b.co", password: "bad" }), { // pragma: allowlist secret
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(401);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect((await res.json()).error.code).toBe("invalid_credentials");
  });

  it("refuses cross-origin posts", async () => {
    const f = ok({ token: TOKEN });
    const res = await login(
      post("/auth/login", { email: "a", password: "b" }, { ...sameOrigin, origin: "https://evil.example" }),
      { fetchImpl: f, baseUrl: BASE },
    );
    expect(res.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
  });

  it("refuses a token that does not look like a session token", async () => {
    const res = await login(post("/auth/login", { email: "a", password: "b" }), {
      fetchImpl: ok({ token: "nope" }),
      baseUrl: BASE,
    });
    expect(res.status).toBe(502);
    expect(res.headers.get("set-cookie")).toBeNull();
  });
});

describe("signup", () => {
  it("creates the org, sets the cookie and returns only non-secret org info", async () => {
    const f = ok({ token: TOKEN, org: { name: "Acme", slug: "acme-1", id: "x" } }, 201);
    const res = await signup(
      post("/auth/signup", { org_name: "Acme", email: "a@b.co", password: "correct horse battery" }), // pragma: allowlist secret
      { fetchImpl: f, baseUrl: BASE },
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("set-cookie")).toContain("HttpOnly");
    const body = await res.json();
    expect(body.org).toEqual({ name: "Acme", slug: "acme-1" });
    expect(JSON.stringify(body)).not.toContain(TOKEN);
  });

  it("rejects incomplete input before calling the control plane", async () => {
    const f = ok({});
    const res = await signup(post("/auth/signup", { email: "a@b.co" }), { fetchImpl: f, baseUrl: BASE });
    expect(res.status).toBe(422);
    expect(f).not.toHaveBeenCalled();
  });
});

describe("ssoComplete", () => {
  it("verifies the token with the control plane before storing it", async () => {
    const f = ok({ user: {}, org: {} });
    const res = await ssoComplete(post("/auth/sso-complete", { token: TOKEN }), { fetchImpl: f, baseUrl: BASE });
    expect(res.status).toBe(200);
    expect(res.headers.get("set-cookie")).toContain(`kg_session=${TOKEN}`);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://cp.test/api/me");
    expect(new Headers(init.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("rejects a malformed token without calling upstream", async () => {
    const f = ok({});
    const res = await ssoComplete(post("/auth/sso-complete", { token: "abc; Path=/" }), {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(422);
    expect(f).not.toHaveBeenCalled();
  });

  it("does not store a token the control plane rejects", async () => {
    const f = ok({ error: { code: "invalid_session", message: "x" } }, 401);
    const res = await ssoComplete(post("/auth/sso-complete", { token: TOKEN }), { fetchImpl: f, baseUrl: BASE });
    expect(res.status).toBe(401);
    expect(res.headers.get("set-cookie")).toBeNull();
  });
});

describe("logout", () => {
  it("ends the session upstream and clears the cookie", async () => {
    const f = vi.fn(async () => new Response(null, { status: 204 }));
    const res = await logout(post("/auth/logout", {}, { ...sameOrigin, cookie: `kg_session=${TOKEN}` }), {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("set-cookie")).toContain("Max-Age=0");
    const [url] = f.mock.calls[0] as unknown as [string];
    expect(url).toBe("http://cp.test/api/logout");
  });
});
