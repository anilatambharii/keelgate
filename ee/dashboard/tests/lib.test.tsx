import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SignupForm, SsoComplete, ssoErrorMessage } from "@/components/auth-forms";
import { fillMonth } from "@/components/daily-chart";
import { computeChecklist, agentSnippet } from "@/lib/checklist";
import { countdown, daysLeft, fmtCents, fmtDurationNs } from "@/lib/format";
import { usePoll } from "@/lib/hooks";
import { safeText } from "@/lib/sanitize";
import { layoutTrace, spanTone } from "@/lib/spans";
import { isStripeUrl } from "@/lib/stripe-url";
import type { Span } from "@/lib/types";
import { passwordHint, safeNext } from "@/lib/validation";
import { makeRequest } from "./helpers";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn() }) }));
const authRequest = vi.fn();
vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return { ...actual, authRequest: (...a: unknown[]) => authRequest(...a) };
});

describe("validation", () => {
  it("gives a live password hint", () => {
    expect(passwordHint("").ok).toBe(false);
    expect(passwordHint("short").text).toMatch(/7 more characters/);
    expect(passwordHint("abcdefghijkl").ok).toBe(true);
  });
  it("only accepts local post-login targets", () => {
    expect(safeNext("/approvals")).toBe("/approvals");
    expect(safeNext("//evil.example")).toBe("/");
    expect(safeNext("https://evil.example")).toBe("/");
    expect(safeNext("/\\evil")).toBe("/");
    expect(safeNext(undefined)).toBe("/");
  });
});

describe("format and sanitize", () => {
  it("formats countdowns, days, money and durations", () => {
    const now = new Date("2026-10-13T12:00:00Z");
    expect(countdown("2026-10-13T12:03:12Z", now)).toBe("3m 12s");
    expect(countdown("2026-10-13T11:00:00Z", now)).toBe("expired");
    expect(daysLeft("2026-10-16T11:00:00Z", now)).toBe(3);
    expect(fmtCents(49900)).toBe("$499.00");
    expect(fmtDurationNs(2_500_000)).toBe("2.5 ms");
  });
  it("removes control and bidi characters", () => {
    expect(safeText("a\u0007b‮c⁧d")).toBe("abcd");
    expect(safeText(null)).toBe("");
  });
  it("only follows https stripe.com addresses", () => {
    expect(isStripeUrl("https://checkout.stripe.com/c/pay/cs_test_1")).toBe(true);
    expect(isStripeUrl("http://checkout.stripe.com/x")).toBe(false);
    expect(isStripeUrl("https://stripe.com.evil.example/x")).toBe(false);
    expect(isStripeUrl("javascript:alert(1)")).toBe(false);
  });
  it("fills every day of a month", () => {
    const days = fillMonth("2026-02", [{ day: "2026-02-03", value: 5 }]);
    expect(days).toHaveLength(28);
    expect(days[2].value).toBe(5);
  });
});

describe("span layout", () => {
  const s = (id: string, parent: string | null, start: number, end: number, status = "OK"): Span => ({
    trace_id: "t",
    span_id: id,
    parent_span_id: parent,
    name: id,
    start_ns: start,
    end_ns: end,
    status,
    attributes: {},
  });
  it("orders parents before children and indents by depth", () => {
    const l = layoutTrace([s("c", "b", 3, 4), s("a", null, 0, 10), s("b", "a", 1, 9)]);
    expect(l.rows.map((r) => [r.span.span_id, r.depth])).toEqual([["a", 0], ["b", 1], ["c", 2]]);
    expect(l.totalNs).toBe(10);
  });
  it("treats orphans as roots and survives cycles", () => {
    const l = layoutTrace([s("x", "missing", 0, 1), s("p", "q", 2, 3), s("q", "p", 2, 3)]);
    expect(l.rows).toHaveLength(3);
  });
  it("maps status to a tone", () => {
    expect(spanTone("ERROR")).toBe("error");
    expect(spanTone("OK")).toBe("ok");
    expect(spanTone(null)).toBe("unset");
  });
});

describe("onboarding checklist", () => {
  const usage = (gated: number, agents: number) =>
    ({ usage: { gated_actions: gated }, agents, limits: {}, daily: [] }) as never;
  it("starts with only the org done", () => {
    const items = computeChecklist({
      keyCount: 0, usage: usage(0, 0), traceCount: 0, approvals: [], billingStatus: "trialing", hasSubscription: false,
    });
    expect(items.filter((i) => i.done).map((i) => i.id)).toEqual(["org"]);
  });
  it("is driven by real state", () => {
    const items = computeChecklist({
      keyCount: 1, usage: usage(4, 1), traceCount: 2,
      approvals: [makeRequest({ status: "APPROVED" })], billingStatus: "active", hasSubscription: true,
    });
    expect(items.every((i) => i.done)).toBe(true);
  });
  it("snippet has the base url and tenant but never a key", () => {
    const code = agentSnippet("https://cp.example", "org-1");
    expect(code).toContain('base_url="https://cp.example"');
    expect(code).toContain('TENANT_ID = "org-1"');
    expect(code).toContain('os.environ["KEELGATE_API_KEY"]');
    expect(code).not.toMatch(/kg_live_/);
  });
});

describe("usePoll", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  function Probe({ fn }: { fn: () => void }) {
    usePoll(fn, 5000);
    return null;
  }
  function setHidden(hidden: boolean) {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    document.dispatchEvent(new Event("visibilitychange"));
  }
  it("polls every 5s, pauses while hidden and refreshes on return", () => {
    const fn = vi.fn();
    render(<Probe fn={fn} />);
    act(() => void vi.advanceTimersByTime(10_000));
    expect(fn).toHaveBeenCalledTimes(2);
    act(() => setHidden(true));
    act(() => void vi.advanceTimersByTime(30_000));
    expect(fn).toHaveBeenCalledTimes(2);
    act(() => setHidden(false));
    expect(fn).toHaveBeenCalledTimes(3);
    act(() => void vi.advanceTimersByTime(5000));
    expect(fn).toHaveBeenCalledTimes(4);
    setHidden(false);
  });
});

describe("SignupForm", () => {
  beforeEach(() => {
    authRequest.mockReset();
    replace.mockReset();
  });
  it("keeps the button disabled until the password is long enough, with a live hint", async () => {
    const user = userEvent.setup();
    render(<SignupForm />);
    await user.type(screen.getByLabelText("Organisation name"), "Acme");
    await user.type(screen.getByLabelText("Work email"), "a@acme.io");
    const pw = screen.getByLabelText("Password");
    await user.type(pw, "short");
    expect(screen.getByText(/more characters needed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create organisation" })).toBeDisabled();
    await user.type(pw, "enoughenough");
    expect(screen.getByText(/Long enough/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create organisation" })).toBeEnabled();
  });
  it("posts to the dashboard route handler and shows server errors", async () => {
    authRequest.mockRejectedValueOnce(new Error("an account with this email already exists"));
    const user = userEvent.setup();
    render(<SignupForm />);
    await user.type(screen.getByLabelText("Organisation name"), "Acme");
    await user.type(screen.getByLabelText("Work email"), "a@acme.io");
    await user.type(screen.getByLabelText("Password"), "twelve-chars-ok");
    await user.click(screen.getByRole("button", { name: "Create organisation" }));
    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
    expect(authRequest).toHaveBeenCalledWith("/auth/signup", {
      org_name: "Acme", email: "a@acme.io", password: "twelve-chars-ok",
    });
  });
});

describe("SsoComplete", () => {
  beforeEach(() => {
    authRequest.mockReset();
    replace.mockReset();
  });
  it("reads the token from the fragment, clears the fragment, posts it and redirects", async () => {
    authRequest.mockResolvedValue({ ok: true });
    window.history.replaceState(null, "", "/sso/complete#token=kgs_" + "D".repeat(43));
    render(<SsoComplete />);
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(authRequest).toHaveBeenCalledWith("/auth/sso-complete", { token: "kgs_" + "D".repeat(43) });
    expect(window.location.hash).toBe("");
  });
  it("shows an error if there is no token", async () => {
    window.history.replaceState(null, "", "/sso/complete");
    render(<SsoComplete />);
    expect(await screen.findByText(/No sign-in token/)).toBeInTheDocument();
    expect(authRequest).not.toHaveBeenCalled();
  });
  it("maps SSO error codes to friendly messages", () => {
    expect(ssoErrorMessage("domain_not_allowed")).toMatch(/domain/);
    expect(ssoErrorMessage("totally_unknown")).toMatch(/failed/);
    expect(ssoErrorMessage("__proto__")).toMatch(/failed/);
  });
});
