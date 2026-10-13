import { ApiError } from "./api";
import type { CaseResult, PolicyCase, PolicyVersion, TestReport } from "./types";

export interface CaseDraft {
  key: string;
  text: string;
}

let counter = 0;
export function newKey(): string {
  counter += 1;
  return `c${counter}`;
}

export function casesToDrafts(cases: PolicyCase[] | undefined): CaseDraft[] {
  return (cases ?? []).map((c) => ({ key: newKey(), text: JSON.stringify(c, null, 2) }));
}

export function blankCase(): CaseDraft {
  const example: PolicyCase = {
    name: "describe what should happen",
    input: {
      action: { tool: "get_balance", side_effect: "READ", capability: "x:read", args: {} },
      actor: { agent_id: "agent-1", tenant_id: "t", grant_id: "g" },
      resource: {},
      context: { as_of: "2026-10-05T14:30:00+00:00", execution_mode: "paper" },
    },
    expect: { effect: "ALLOW" },
  };
  return { key: newKey(), text: JSON.stringify(example, null, 2) };
}

export interface ParsedCases {
  cases: PolicyCase[];
  errors: Record<string, string>;
}

/** Parse each case's JSON text. Every case must be an object with a name and an `input` object. */
export function parseCases(drafts: CaseDraft[]): ParsedCases {
  const cases: PolicyCase[] = [];
  const errors: Record<string, string> = {};
  for (const d of drafts) {
    let value: unknown;
    try {
      value = JSON.parse(d.text);
    } catch (e) {
      errors[d.key] = `Not valid JSON: ${e instanceof Error ? e.message : "parse error"}`;
      continue;
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      errors[d.key] = "A case must be a JSON object";
      continue;
    }
    const v = value as Record<string, unknown>;
    if (typeof v.name !== "string" || !v.name.trim()) {
      errors[d.key] = 'A case needs a "name"';
      continue;
    }
    if (!v.input || typeof v.input !== "object" || Array.isArray(v.input)) {
      errors[d.key] = 'A case needs an "input" object';
      continue;
    }
    if (v.expect !== undefined) {
      const ex = v.expect as Record<string, unknown> | null;
      if (!ex || typeof ex !== "object" || typeof ex.effect !== "string") {
        errors[d.key] = '"expect" must be an object with an "effect"';
        continue;
      }
    }
    cases.push(v as unknown as PolicyCase);
  }
  return { cases, errors };
}

/** A saved test run only counts as passed when the server says so. */
export function canDeploy(v: Pick<PolicyVersion, "tests_passed" | "active">): boolean {
  return v.tests_passed === true && v.active !== true;
}

export function deployDisabledReason(v: Pick<PolicyVersion, "tests_passed" | "active">): string | null {
  if (v.active) return "This version is already active.";
  if (v.tests_passed === true) return null;
  if (v.tests_passed === false) return "Its tests did not pass. Fix the policy or the cases and save a new version.";
  return "It has no passing tests. Add test cases and save a new version.";
}

/** The `report` the server attaches to a 409 refusal, if any. */
export function refusalReport(err: unknown): { code: string; message: string; report: TestReport | null } | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const details = err.details as { report?: TestReport } | undefined;
  return { code: err.code, message: err.message, report: details?.report ?? null };
}

export function failedCases(report: TestReport | null | undefined): CaseResult[] {
  return (report?.cases ?? []).filter((c) => c.passed !== true);
}
