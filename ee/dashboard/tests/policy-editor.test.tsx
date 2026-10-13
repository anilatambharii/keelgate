import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PolicyEditor } from "@/components/policy-editor";
import { ApiError } from "@/lib/api";
import { canDeploy, deployDisabledReason, parseCases, refusalReport } from "@/lib/policy";
import type { PolicyVersion } from "@/lib/types";
import { makeMe, renderWithApp } from "./helpers";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }) }));
vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      policyVersions: vi.fn(),
      policyVersion: vi.fn(),
      policyTemplates: vi.fn(),
      simulate: vi.fn(),
      createPolicyVersion: vi.fn(),
      createPolicy: vi.fn(),
      deployPolicy: vi.fn(),
      rollbackPolicy: vi.fn(),
    },
  };
});
import { api } from "@/lib/api";

const m = {
  versions: vi.mocked(api.policyVersions),
  version: vi.mocked(api.policyVersion),
  templates: vi.mocked(api.policyTemplates),
  simulate: vi.mocked(api.simulate),
  deploy: vi.mocked(api.deployPolicy),
  save: vi.mocked(api.createPolicyVersion),
};

const CASE = JSON.stringify({ name: "read ok", input: { action: {} }, expect: { effect: "ALLOW" } });
const v = (n: number, tests_passed: boolean | null, active = false): PolicyVersion => ({
  version: n,
  tests_passed,
  active,
  created_at: "2026-10-01T00:00:00Z",
});

function setup(versions: PolicyVersion[]) {
  m.versions.mockResolvedValue({ items: versions });
  m.version.mockResolvedValue({
    ...versions[0],
    files: { "p.rego": "package p" },
    query: "data.p.decision",
    cases: JSON.parse(`[${CASE}]`),
  });
  m.templates.mockResolvedValue({ items: [] });
}

beforeEach(() => {
  Object.values(m).forEach((f) => f.mockReset());
  vi.mocked(api.rollbackPolicy).mockReset();
});

describe("policy helpers", () => {
  it("parseCases flags bad JSON, missing name and missing input per case", () => {
    const { cases, errors } = parseCases([
      { key: "a", text: "{nope" },
      { key: "b", text: JSON.stringify({ input: {} }) },
      { key: "c", text: JSON.stringify({ name: "x" }) },
      { key: "d", text: CASE },
    ]);
    expect(Object.keys(errors).sort()).toEqual(["a", "b", "c"]);
    expect(cases).toHaveLength(1);
  });

  it("only versions with passing tests can be deployed", () => {
    expect(canDeploy(v(1, true))).toBe(true);
    expect(canDeploy(v(1, false))).toBe(false);
    expect(canDeploy(v(1, null))).toBe(false);
    expect(canDeploy(v(1, true, true))).toBe(false);
    expect(deployDisabledReason(v(1, null))).toMatch(/no passing tests/);
  });

  it("extracts the server's refusal report from a 409", () => {
    const err = new ApiError(409, "tests_failed", "no", { error: {}, report: { total: 1 } });
    expect(refusalReport(err)?.report?.total).toBe(1);
    expect(refusalReport(new ApiError(500, "x", "y"))).toBeNull();
  });
});

describe("PolicyEditor", () => {
  it("loads the latest version into the editor", async () => {
    setup([v(2, true, true), v(1, false)]);
    renderWithApp(<PolicyEditor name="acme" />);
    expect(await screen.findByLabelText("Contents of p.rego")).toHaveValue("package p");
    expect(screen.getByLabelText("Query")).toHaveValue("data.p.decision");
    expect(screen.getByLabelText(/^Case 1/)).toBeInTheDocument();
  });

  it("simulate shows pass and fail with expected vs actual", async () => {
    setup([v(1, true)]);
    m.simulate.mockResolvedValue({
      compiled: true,
      compile_error: null,
      total: 2,
      passed_count: 1,
      all_passed: false,
      results: [
        { name: "read ok", passed: true, expected: { effect: "ALLOW" }, actual: { effect: "ALLOW", reasons: [] } },
        { name: "write denied", passed: false, expected: { effect: "DENY" }, actual: { effect: "ALLOW", reasons: ["oops"] } },
      ],
    });
    const user = userEvent.setup();
    renderWithApp(<PolicyEditor name="acme" />);
    await screen.findByLabelText("Query");
    await user.click(screen.getByRole("button", { name: "Simulate" }));
    const rows = await screen.findAllByTestId("case-result");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("pass")).toBeInTheDocument();
    expect(within(rows[1]).getByText("fail")).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent('"effect":"DENY"');
    expect(rows[1]).toHaveTextContent('"effect":"ALLOW"');
    expect(m.simulate).toHaveBeenCalledWith(expect.objectContaining({ query: "data.p.decision" }));
  });

  it("shows compile errors inline", async () => {
    setup([v(1, true)]);
    m.simulate.mockResolvedValue({
      compiled: false,
      compile_error: "rego_parse_error: unexpected }",
      total: 1,
      passed_count: 0,
      all_passed: false,
      results: [],
    });
    const user = userEvent.setup();
    renderWithApp(<PolicyEditor name="acme" />);
    await screen.findByLabelText("Query");
    await user.click(screen.getByRole("button", { name: "Simulate" }));
    expect(await screen.findByText(/does not compile/)).toBeInTheDocument();
    expect(screen.getByText(/rego_parse_error/)).toBeInTheDocument();
  });

  it("blocks simulate on malformed case JSON without calling the server", async () => {
    setup([v(1, true)]);
    const user = userEvent.setup();
    renderWithApp(<PolicyEditor name="acme" />);
    const box = await screen.findByLabelText(/^Case 1/);
    await user.clear(box);
    await user.type(box, "not json");
    await user.click(screen.getByRole("button", { name: "Simulate" }));
    expect(await screen.findByText(/Not valid JSON/)).toBeInTheDocument();
    expect(m.simulate).not.toHaveBeenCalled();
  });

  it("only enables Deploy for a version whose tests passed", async () => {
    setup([v(3, true), v(2, false), v(1, null)]);
    renderWithApp(<PolicyEditor name="acme" />);
    await screen.findByText("v3");
    expect(screen.getByRole("button", { name: /Deploys*version 3/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Deploys*version 2/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Deploys*version 1/ })).toBeDisabled();
    expect(screen.getByText(/Its tests did not pass/)).toBeInTheDocument();
  });

  it("deploys a passing version and refreshes the list", async () => {
    setup([v(1, true)]);
    m.deploy.mockResolvedValue({});
    const user = userEvent.setup();
    renderWithApp(<PolicyEditor name="acme" />);
    await user.click(await screen.findByRole("button", { name: /Deploys*version 1/ }));
    await waitFor(() => expect(m.deploy).toHaveBeenCalledWith("acme", 1));
    await waitFor(() => expect(m.versions).toHaveBeenCalledTimes(2));
  });

  it("shows the server's tests_failed report when it refuses a deploy", async () => {
    setup([v(1, true)]);
    m.deploy.mockRejectedValue(
      new ApiError(409, "tests_failed", "the cases do not pass against the stored sources", {
        error: { code: "tests_failed" },
        report: {
          compiled: true,
          compile_error: null,
          total: 1,
          passed_count: 0,
          all_passed: false,
          cases: [{ name: "regressed", passed: false, expected: { effect: "DENY" }, actual: { effect: "ALLOW" } }],
        },
      }),
    );
    const user = userEvent.setup();
    renderWithApp(<PolicyEditor name="acme" />);
    await user.click(await screen.findByRole("button", { name: /Deploys*version 1/ }));
    expect(await screen.findByText("Deploy refused")).toBeInTheDocument();
    expect(screen.getByText("regressed")).toBeInTheDocument();
  });

  it("is read-only for approvers", async () => {
    setup([v(1, true)]);
    renderWithApp(<PolicyEditor name="acme" />, makeMe("approver"));
    expect(await screen.findByText(/Only owners and admins can simulate/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Simulate" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Deploys*version 1/ })).toBeDisabled();
  });
});
