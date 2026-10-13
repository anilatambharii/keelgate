// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { forward, upstreamPath } from "@/lib/bff";

const TOKEN = "kgs_" + "A".repeat(43);
const BASE = "http://cp.test";

function req(path: string, init: RequestInit & { headers?: Record<string, string> } = {}): Request {
  return new Request(`http://dash.test${path}`, init);
}

function upstream(res: Response) {
  return vi.fn(async () => res);
}

const authed = { cookie: `kg_session=${TOKEN}`, host: "dash.test", origin: "http://dash.test" };

describe("upstreamPath allow-list", () => {
  it("accepts /api/ paths only", () => {
    expect(upstreamPath(["api", "keys"])).toBe("/api/keys");
    expect(upstreamPath(["api", "policies", "my pack", "versions", "2"])).toBe(
      "/api/policies/my%20pack/versions/2",
    );
  });

  it("refuses the agent API and everything else", () => {
    expect(upstreamPath(["v1", "audit", "events"])).toBeNull();
    expect(upstreamPath(["healthz"])).toBeNull();
    expect(upstreamPath(["api"])).toBeNull();
    expect(upstreamPath([])).toBeNull();
  });

  it("refuses traversal and smuggled separators", () => {
    expect(upstreamPath(["api", "..", "v1", "x"])).toBeNull();
    expect(upstreamPath(["api", "%2e%2e", "v1"])).toBeNull();
    expect(upstreamPath(["api", "a%2Fb"])).toBeNull();
    expect(upstreamPath(["api", "a\\b"])).toBeNull();
    expect(upstreamPath(["api", "", "x"])).toBeNull();
    expect(upstreamPath(["api", "x?y=1"])).toBeNull();
  });
});

describe("forward", () => {
  it("returns 404 without calling the control plane for /v1 paths", async () => {
    const f = upstream(new Response("{}"));
    const res = await forward(req("/bff/v1/audit/events", { headers: authed }), ["v1", "audit", "events"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(404);
    expect(f).not.toHaveBeenCalled();
  });

  it("answers 401 with a login signal when there is no session cookie", async () => {
    const f = upstream(new Response("{}"));
    const res = await forward(req("/bff/api/me", { headers: { host: "dash.test" } }), ["api", "me"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(401);
    expect(res.headers.get("x-kg-login-required")).toBe("1");
    expect(f).not.toHaveBeenCalled();
  });

  it("adds the bearer token from the cookie and forwards only allow-listed headers", async () => {
    const f = upstream(
      new Response(JSON.stringify({ ok: true }), { headers: { "content-type": "application/json" } }),
    );
    const res = await forward(
      req("/bff/api/keys?x=1", {
        headers: { ...authed, accept: "application/json", "x-evil": "1", authorization: "Bearer attacker" },
      }),
      ["api", "keys"],
      { fetchImpl: f, baseUrl: BASE },
    );
    expect(res.status).toBe(200);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://cp.test/api/keys?x=1");
    const h = new Headers(init.headers);
    expect(h.get("authorization")).toBe(`Bearer ${TOKEN}`);
    expect(h.get("cookie")).toBeNull();
    expect(h.get("x-evil")).toBeNull();
    expect(h.get("origin")).toBeNull();
    expect(h.get("accept")).toBe("application/json");
  });

  it("never lets the token or cookies reach the browser in the response", async () => {
    const f = upstream(
      new Response(JSON.stringify({ items: [] }), {
        headers: { "content-type": "application/json", "set-cookie": "evil=1", "x-secret": "s" },
      }),
    );
    const res = await forward(req("/bff/api/keys", { headers: authed }), ["api", "keys"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(res.headers.get("x-secret")).toBeNull();
    expect(await res.text()).not.toContain(TOKEN);
  });

  describe("same-origin enforcement", () => {
    it("blocks a state-changing request from another origin", async () => {
      const f = upstream(new Response("{}"));
      const res = await forward(
        req("/bff/api/keys", {
          method: "POST",
          body: "{}",
          headers: { ...authed, origin: "https://evil.example" },
        }),
        ["api", "keys"],
        { fetchImpl: f, baseUrl: BASE },
      );
      expect(res.status).toBe(403);
      expect((await res.json()).error.code).toBe("cross_origin");
      expect(f).not.toHaveBeenCalled();
    });

    it("blocks a state-changing request that has no Origin and no Sec-Fetch-Site", async () => {
      const f = upstream(new Response("{}"));
      const res = await forward(
        req("/bff/api/keys/1", { method: "DELETE", headers: { cookie: authed.cookie, host: "dash.test" } }),
        ["api", "keys", "1"],
        { fetchImpl: f, baseUrl: BASE },
      );
      expect(res.status).toBe(403);
      expect(f).not.toHaveBeenCalled();
    });

    it("blocks Sec-Fetch-Site: cross-site", async () => {
      const f = upstream(new Response("{}"));
      const res = await forward(
        req("/bff/api/keys", {
          method: "POST",
          body: "{}",
          headers: { cookie: authed.cookie, host: "dash.test", "sec-fetch-site": "cross-site" },
        }),
        ["api", "keys"],
        { fetchImpl: f, baseUrl: BASE },
      );
      expect(res.status).toBe(403);
    });

    it("allows the same origin and forwards the JSON body", async () => {
      const f = upstream(
        new Response(JSON.stringify({ id: "k" }), {
          status: 201,
          headers: { "content-type": "application/json" },
        }),
      );
      const res = await forward(
        req("/bff/api/keys", {
          method: "POST",
          body: JSON.stringify({ name: "ci" }),
          headers: { ...authed, "content-type": "application/json" },
        }),
        ["api", "keys"],
        { fetchImpl: f, baseUrl: BASE },
      );
      expect(res.status).toBe(201);
      const init = (f.mock.calls[0] as unknown as [string, RequestInit])[1];
      expect(init.method).toBe("POST");
      expect(new TextDecoder().decode(init.body as ArrayBuffer)).toBe('{"name":"ci"}');
    });

    it("does not require an Origin for reads", async () => {
      const f = upstream(new Response("{}", { headers: { "content-type": "application/json" } }));
      const res = await forward(
        req("/bff/api/me", { headers: { cookie: authed.cookie, host: "dash.test" } }),
        ["api", "me"],
        { fetchImpl: f, baseUrl: BASE },
      );
      expect(res.status).toBe(200);
    });
  });

  it("passes through status, content-type and content-disposition for file exports", async () => {
    const f = upstream(
      new Response("seq,event\n1,x\n", {
        headers: {
          "content-type": "text/csv; charset=utf-8",
          "content-disposition": 'attachment; filename="audit.csv"',
        },
      }),
    );
    const res = await forward(req("/bff/api/audit/export?format=csv", { headers: authed }), ["api", "audit", "export"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/csv");
    expect(res.headers.get("content-disposition")).toBe('attachment; filename="audit.csv"');
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(await res.text()).toContain("seq,event");
  });

  it("passes non-401 errors (e.g. 402 plan_required) through unchanged", async () => {
    const body = { error: { code: "plan_required", message: "Upgrade to Business" } };
    const f = upstream(
      new Response(JSON.stringify(body), { status: 402, headers: { "content-type": "application/json" } }),
    );
    const res = await forward(
      req("/bff/api/audit/export/compliance", { headers: authed }),
      ["api", "audit", "export", "compliance"],
      { fetchImpl: f, baseUrl: BASE },
    );
    expect(res.status).toBe(402);
    expect(await res.json()).toEqual(body);
  });

  it("turns a control-plane 401 into a login signal and clears the cookie", async () => {
    const f = upstream(
      new Response(JSON.stringify({ error: { code: "invalid_session", message: "expired" } }), { status: 401 }),
    );
    const res = await forward(req("/bff/api/me", { headers: authed }), ["api", "me"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(401);
    expect(res.headers.get("x-kg-login-required")).toBe("1");
    const cookie = res.headers.get("set-cookie") ?? "";
    expect(cookie).toContain("kg_session=;");
    expect(cookie).toContain("Max-Age=0");
    expect(cookie).toContain("HttpOnly");
  });

  it("reports an unreachable control plane as 502", async () => {
    const f = vi.fn(async () => {
      throw new TypeError("ECONNREFUSED");
    });
    const res = await forward(req("/bff/api/me", { headers: authed }), ["api", "me"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(502);
    expect((await res.json()).error.code).toBe("upstream_unavailable");
  });

  it("does not follow or relay upstream redirects", async () => {
    const f = upstream(new Response(null, { status: 302, headers: { location: "http://evil.example" } }));
    const res = await forward(req("/bff/api/me", { headers: authed }), ["api", "me"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(502);
    expect(res.headers.get("location")).toBeNull();
    const init = (f.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.redirect).toBe("manual");
  });

  it("rejects unsupported methods and oversized bodies", async () => {
    const f = upstream(new Response("{}"));
    const opt = { fetchImpl: f, baseUrl: BASE };
    expect(
      (await forward(req("/bff/api/me", { method: "OPTIONS", headers: authed }), ["api", "me"], opt)).status,
    ).toBe(405);
    const big = "x".repeat(3 * 1024 * 1024);
    const res = await forward(
      req("/bff/api/keys", { method: "POST", body: big, headers: authed }),
      ["api", "keys"],
      opt,
    );
    expect(res.status).toBe(413);
    expect(f).not.toHaveBeenCalled();
  });

  it("returns an empty body for 204", async () => {
    const f = upstream(new Response(null, { status: 204 }));
    const res = await forward(req("/bff/api/keys/1", { method: "DELETE", headers: authed }), ["api", "keys", "1"], {
      fetchImpl: f,
      baseUrl: BASE,
    });
    expect(res.status).toBe(204);
    expect(await res.text()).toBe("");
  });
});
