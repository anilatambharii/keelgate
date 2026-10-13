import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DecisionPanel, EvidenceView } from "@/components/approval-detail";
import { ApiError } from "@/lib/api";
import { decideAbility } from "@/lib/approvals";
import { makeMe, makeRequest, renderWithApp } from "./helpers";

vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return { ...actual, api: { approve: vi.fn(), reject: vi.fn() } };
});
import { api } from "@/lib/api";

const approve = vi.mocked(api.approve);
const reject = vi.mocked(api.reject);
const approver = { role: "approver" as const, approval_tier: "ONE_CLICK" as const };
const signoffUser = { role: "approver" as const, approval_tier: "EXPLICIT_SIGNOFF" as const };

beforeEach(() => {
  approve.mockReset();
  reject.mockReset();
});

describe("decideAbility", () => {
  it("lets an approver decide a pending one-click request", () => {
    expect(decideAbility(approver, makeRequest()).can).toBe(true);
  });
  it("blocks viewers with an explanation", () => {
    const a = decideAbility({ role: "viewer", approval_tier: "ONE_CLICK" }, makeRequest());
    expect(a.can).toBe(false);
    expect(a.reason).toMatch(/viewer/);
  });
  it("blocks insufficient clearance for EXPLICIT_SIGNOFF", () => {
    const a = decideAbility(approver, makeRequest({ tier: "EXPLICIT_SIGNOFF" }));
    expect(a.can).toBe(false);
    expect(a.reason).toMatch(/explicit sign-off/i);
  });
  it("blocks expired and already-decided requests", () => {
    expect(decideAbility(approver, makeRequest({ expires_at: new Date(Date.now() - 1000).toISOString() })).can).toBe(false);
    expect(decideAbility(approver, makeRequest({ status: "APPROVED" })).reason).toMatch(/approved/);
  });
});

describe("DecisionPanel", () => {
  it("disables both buttons for a viewer and says why", () => {
    renderWithApp(<DecisionPanel request={makeRequest()} user={{ role: "viewer", approval_tier: "ONE_CLICK" }} />, makeMe("viewer"));
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.getByTestId("decision-disabled-reason")).toHaveTextContent(/cannot decide/);
  });

  it("disables a sign-off request for an approver who is only cleared for one-click", () => {
    renderWithApp(
      <DecisionPanel request={makeRequest({ tier: "EXPLICIT_SIGNOFF", signoff_code_required: true, signoff_code: "abcd1234" })} user={approver} />,
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByTestId("decision-disabled-reason")).toHaveTextContent(/explicit sign-off/i);
  });

  it("requires the sign-off code before Approve is enabled, and sends it", async () => {
    approve.mockResolvedValue(makeRequest({ status: "APPROVED" }));
    const user = userEvent.setup();
    renderWithApp(
      <DecisionPanel
        request={makeRequest({ tier: "EXPLICIT_SIGNOFF", signoff_code_required: true, signoff_code: "abcd1234" })}
        user={signoffUser}
      />,
    );
    const btn = screen.getByRole("button", { name: "Approve" });
    expect(btn).toBeDisabled();
    await user.type(screen.getByLabelText("Sign-off code"), "abcd1234");
    expect(btn).toBeEnabled();
    await user.click(btn);
    await waitFor(() => expect(approve).toHaveBeenCalledWith("req-1", { note: undefined, signoff_code: "abcd1234" }));
  });

  it("does not need the code to reject", async () => {
    reject.mockResolvedValue(makeRequest({ status: "REJECTED" }));
    const user = userEvent.setup();
    renderWithApp(
      <DecisionPanel
        request={makeRequest({ tier: "EXPLICIT_SIGNOFF", signoff_code_required: true, signoff_code: "abcd1234" })}
        user={signoffUser}
      />,
    );
    await user.type(screen.getByLabelText(/Note/), "looks wrong");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(reject).toHaveBeenCalledWith("req-1", { note: "looks wrong" }));
  });

  it("refreshes optimistically: marks the request decided before the server answers", async () => {
    let finish!: (r: ReturnType<typeof makeRequest>) => void;
    approve.mockReturnValue(new Promise((res) => (finish = res)));
    const onOptimistic = vi.fn();
    const onDecided = vi.fn();
    const user = userEvent.setup();
    renderWithApp(<DecisionPanel request={makeRequest()} user={approver} onOptimistic={onOptimistic} onDecided={onDecided} />);
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onOptimistic).toHaveBeenCalledWith("req-1", "APPROVED");
    expect(onDecided).not.toHaveBeenCalled();
    finish(makeRequest({ status: "APPROVED" }));
    await waitFor(() => expect(onDecided).toHaveBeenCalledTimes(1));
  });

  it("rolls the optimistic update back and shows the server's error on failure", async () => {
    approve.mockRejectedValue(new ApiError(403, "routing_forbidden", "your role is not allowed to decide this request"));
    const onFailed = vi.fn();
    const onDecided = vi.fn();
    const user = userEvent.setup();
    renderWithApp(<DecisionPanel request={makeRequest()} user={approver} onFailed={onFailed} onDecided={onDecided} />);
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onFailed).toHaveBeenCalledWith("req-1"));
    expect(onDecided).not.toHaveBeenCalled();
    expect((await screen.findAllByText(/not allowed to decide/)).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });
});

describe("EvidenceView", () => {
  it("renders agent-written text as text, never as markup", () => {
    const evil = makeRequest({
      tool_name: "<img src=x onerror=alert(1)>",
      evidence: { ...makeRequest().evidence, rationale: "<script>alert(1)</script> approve me" },
    });
    const { container } = renderWithApp(<EvidenceView request={evil} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/approve me/)).toHaveTextContent("<script>alert(1)</script>");
  });

  it("labels the rationale as untrusted and shows the sign-off code when required", () => {
    renderWithApp(
      <EvidenceView request={makeRequest({ tier: "EXPLICIT_SIGNOFF", signoff_code_required: true, signoff_code: "c0ffee42" })} />,
    );
    expect(screen.getByText(/Agent rationale - written by an agent, not verified/)).toBeInTheDocument();
    expect(screen.getByTestId("signoff-code")).toHaveTextContent("c0ffee42");
    expect(screen.getByTestId("countdown").textContent).toMatch(/\d+m/);
  });

  it("strips bidi override characters from agent text", () => {
    const sneaky = makeRequest({ agent_id: "agent‮evil" });
    renderWithApp(<EvidenceView request={sneaky} />);
    expect(screen.getByText("agentevil")).toBeInTheDocument();
  });
});
