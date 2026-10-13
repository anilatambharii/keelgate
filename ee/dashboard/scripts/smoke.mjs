// Smoke test against a RUNNING control plane and a RUNNING dashboard (next dev or next start).
//   CONTROL_PLANE_URL (default http://localhost:8000)   DASHBOARD_URL (default http://localhost:3000)
// It starts nothing. It signs up a fresh org through the dashboard BFF and exercises the main paths.
import { randomBytes } from "node:crypto";

const CP = (process.env.CONTROL_PLANE_URL || "http://localhost:8000").replace(/\/+$/, "");
const DASH = (process.env.DASHBOARD_URL || "http://localhost:3000").replace(/\/+$/, "");
const ORIGIN = new URL(DASH).origin;

let passed = 0;
let failed = 0;
function check(name, ok, detail = "") {
  if (ok) {
    passed += 1;
    console.log(`  ok   ${name}`);
  } else {
    failed += 1;
    console.log(`  FAIL ${name} ${detail}`);
  }
}

// A tiny cookie jar: the browser would do this for us.
let jar = "";
function takeCookies(res) {
  for (const line of res.headers.getSetCookie()) {
    const [pair] = line.split(";");
    const [name, ...rest] = pair.split("=");
    const value = rest.join("=");
    const others = jar.split("; ").filter((c) => c && !c.startsWith(`${name}=`));
    jar = value ? [...others, `${name}=${value}`].join("; ") : others.join("; ");
  }
}

async function dash(path, { method = "GET", body, origin = ORIGIN, cookie = true, headers = {} } = {}) {
  const h = { ...headers };
  if (cookie && jar) h.cookie = jar;
  if (method !== "GET" && origin) h.origin = origin;
  if (body !== undefined) h["content-type"] = "application/json";
  const res = await fetch(`${DASH}${path}`, {
    method,
    headers: h,
    body: body === undefined ? undefined : JSON.stringify(body),
    redirect: "manual",
  });
  takeCookies(res);
  return res;
}

async function json(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const suffix = randomBytes(4).toString("hex");
const email = `smoke-${suffix}@example.com`;
const password = `smoke-pass-${suffix}-long`; // pragma: allowlist secret

console.log(`dashboard ${DASH}  control plane ${CP}`);

console.log("pages and headers");
{
  const res = await dash("/login", { cookie: false });
  check("GET /login is 200", res.status === 200, String(res.status));
  const csp = res.headers.get("content-security-policy") || "";
  check("CSP has a nonce and frame-ancestors none", /nonce-/.test(csp) && /frame-ancestors 'none'/.test(csp), csp);
  check("CSP connect-src is self", /connect-src 'self'/.test(csp));
  check("nosniff set", res.headers.get("x-content-type-options") === "nosniff");
  const root = await dash("/", { cookie: false });
  check("signed-out GET / redirects to /login", root.status === 307 && (root.headers.get("location") || "").includes("/login"));
  const guarded = await dash("/bff/api/me", { cookie: false });
  check("BFF without a cookie is 401 with login signal", guarded.status === 401 && guarded.headers.get("x-kg-login-required") === "1");
}

console.log("signup through the BFF");
let orgId = "";
{
  const res = await dash("/auth/signup", {
    method: "POST",
    cookie: false,
    body: { org_name: `Smoke ${suffix}`, email, password },
  });
  const body = await json(res);
  check("signup is 200", res.status === 200, JSON.stringify(body));
  const raw = res.headers.getSetCookie().join("\n");
  check("cookie is HttpOnly", /kg_session=[^;]+;.*HttpOnly/i.test(raw), raw);
  check("cookie is SameSite=Lax", /SameSite=Lax/i.test(raw));
  check("token is not in the JSON body", !JSON.stringify(body).includes("kgs_"));
  const cross = await dash("/auth/login", { method: "POST", cookie: false, origin: "https://evil.example", body: { email, password } });
  check("cross-origin login is refused", cross.status === 403);
}

console.log("authenticated pages");
{
  const home = await dash("/");
  check("GET / is 200 when signed in", home.status === 200, String(home.status));
  const html = await home.text();
  check("home page does not contain the session token", !html.includes(jar.split("=")[1] || "nope"));
  const login = await dash("/login");
  check("signed-in GET /login redirects away", login.status === 307);
  for (const p of ["/approvals", "/keys", "/billing", "/traces", "/policies", "/audit", "/team", "/settings"]) {
    const r = await dash(p);
    check(`GET ${p} is 200`, r.status === 200, String(r.status));
  }
}

console.log("API through the BFF");
let apiKey = "";
{
  const me = await dash("/bff/api/me");
  const meBody = await json(me);
  check("/bff/api/me returns the user", me.status === 200 && meBody.user?.email === email, JSON.stringify(meBody));
  orgId = meBody.org?.id || "";

  const created = await dash("/bff/api/keys", { method: "POST", body: { name: "smoke-key" } });
  const key = await json(created);
  check("create key is 201 and returns the secret once", created.status === 201 && typeof key.key === "string", JSON.stringify(key));
  apiKey = key.key || "";
  const list = await json(await dash("/bff/api/keys"));
  check("listing keys never repeats the secret", list.items?.length === 1 && !JSON.stringify(list).includes(apiKey));

  for (const [name, path] of [
    ["usage", "/bff/api/usage"],
    ["approvals", "/bff/api/approvals?status=PENDING"],
    ["traces", "/bff/api/traces"],
    ["billing", "/bff/api/billing"],
    ["invoice preview", "/bff/api/billing/invoice-preview"],
    ["policies", "/bff/api/policies"],
    ["policy templates", "/bff/api/policies/templates"],
    ["audit", "/bff/api/audit?limit=5&newest_first=true"],
    ["settings", "/bff/api/settings"],
    ["users", "/bff/api/users"],
  ]) {
    const r = await dash(path);
    check(`${name} is 200`, r.status === 200, `${r.status} ${JSON.stringify(await json(r)).slice(0, 200)}`);
  }

  const v1 = await dash("/bff/v1/audit/records");
  check("the agent API (/v1) is not reachable through the BFF", v1.status === 404);
  const dotdot = await dash("/bff/api/%2e%2e/v1/audit/head");
  check("path traversal is refused", dotdot.status === 404 || dotdot.status === 400, String(dotdot.status));
  const csrf = await dash("/bff/api/keys", { method: "POST", origin: "https://evil.example", body: { name: "x" } });
  check("cross-origin POST through the BFF is refused", csrf.status === 403);
  const noOrigin = await dash("/bff/api/keys", { method: "POST", origin: null, body: { name: "x" } });
  check("POST with no Origin is refused", noOrigin.status === 403);

  const exp = await dash("/bff/api/audit/export?format=json");
  check("audit export passes through as a file", exp.status === 200 && /attachment/.test(exp.headers.get("content-disposition") || ""), `${exp.status} ${exp.headers.get("content-disposition")}`);
  await exp.arrayBuffer();
  const comp = await dash("/bff/api/audit/export/compliance");
  const compBody = await json(comp);
  check("compliance export on a Team plan is 402 plan_required", comp.status === 402 && compBody.error?.code === "plan_required", JSON.stringify(compBody));
}

console.log("an agent parks an approval; a human decides it through the dashboard");
if (apiKey && orgId) {
  const agentHeaders = { authorization: `Bearer ${apiKey}`, "content-type": "application/json" };
  const argsHash = randomBytes(32).toString("hex");
  const parked = await fetch(`${CP}/v1/approvals`, {
    method: "POST",
    headers: agentHeaders,
    body: JSON.stringify({
      tenant_id: orgId,
      agent_id: "smoke-agent",
      tool_name: "transfer_funds",
      args_hash: argsHash,
      tier: "ONE_CLICK",
      evidence: { tool: "transfer_funds", args: { amount: 5 }, rationale: "smoke test" },
    }),
  });
  const approval = await json(parked);
  check("agent parks an approval", parked.status === 200 && approval.request_id, JSON.stringify(approval));

  const span = {
    trace_id: randomBytes(16).toString("hex"),
    span_id: randomBytes(8).toString("hex"),
    parent_span_id: null,
    name: "smoke.root",
    start_ns: 1_000_000_000,
    end_ns: 2_000_000_000,
    status: "OK",
    attributes: { note: "<script>alert(1)</script>" },
  };
  const tr = await fetch(`${CP}/v1/traces`, { method: "POST", headers: agentHeaders, body: JSON.stringify({ spans: [span] }) });
  check("agent ships a trace", tr.status === 200, String(tr.status));

  const pending = await json(await dash("/bff/api/approvals?status=PENDING"));
  const found = pending.items?.find((r) => r.request_id === approval.request_id);
  check("the approval shows in the dashboard inbox", Boolean(found));
  check("the request carries signoff_code_required", found && "signoff_code_required" in found);

  const traces = await json(await dash("/bff/api/traces"));
  check("the trace shows in the explorer", traces.items?.some((t) => t.trace_id === span.trace_id));
  const detail = await json(await dash(`/bff/api/traces/${span.trace_id}`));
  check("trace detail returns the span", detail.spans?.[0]?.name === "smoke.root");

  const decided = await dash(`/bff/api/approvals/${approval.request_id}/approve`, { method: "POST", body: { note: "smoke" } });
  const decidedBody = await json(decided);
  check("owner approves through the BFF", decided.status === 200 && decidedBody.status === "APPROVED", JSON.stringify(decidedBody));

  const usage = await json(await dash("/bff/api/usage"));
  check("usage reflects the activity", usage.usage?.approvals >= 1 || usage.agents >= 1, JSON.stringify(usage.usage));
}

console.log("sign out");
{
  const out = await dash("/auth/logout", { method: "POST", body: {} });
  check("logout is 200", out.status === 200);
  const after = await dash("/bff/api/me");
  check("after logout the BFF is 401", after.status === 401);
  const home = await dash("/");
  check("after logout / redirects to /login", home.status === 307);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
