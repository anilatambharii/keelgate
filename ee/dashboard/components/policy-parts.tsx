"use client";
import { safeJson, safeText } from "@/lib/sanitize";
import type { CaseResult, PolicyVersion, SimulationResult, TestReport } from "@/lib/types";
import { deployDisabledReason } from "@/lib/policy";
import { Alert, Badge, Button, fmtDate, Table, TD, TH } from "./ui";

function compact(v: unknown): string {
  if (v === null || v === undefined) return "-";
  return safeText(JSON.stringify(v));
}

export function CaseResultRow({ r }: { r: CaseResult }) {
  const tone = r.passed === true ? "ok" : r.passed === false ? "danger" : "neutral";
  const label = r.passed === true ? "pass" : r.passed === false ? "fail" : "no expectation";
  return (
    <li className="rounded-md border border-line p-2 text-sm" data-testid="case-result">
      <p className="flex flex-wrap items-center gap-2">
        <Badge tone={tone}>{label}</Badge>
        <span className="font-medium">{safeText(r.name)}</span>
      </p>
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
        <dt className="text-muted">Expected</dt>
        <dd className="break-all font-mono">{compact(r.expected)}</dd>
        <dt className="text-muted">Actual</dt>
        <dd className="break-all font-mono">{compact(r.actual)}</dd>
        {r.error ? (
          <>
            <dt className="text-danger">Error</dt>
            <dd className="break-words text-danger">{safeText(r.error)}</dd>
          </>
        ) : null}
      </dl>
    </li>
  );
}

export function ReportView({
  report,
  title,
}: {
  report: Pick<TestReport, "compiled" | "compile_error" | "total" | "passed_count" | "all_passed"> & {
    cases?: CaseResult[];
  };
  title: string;
}) {
  return (
    <div className="space-y-2" aria-label={title}>
      {report.compile_error ? (
        <Alert tone="danger" title="The policy does not compile">
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-xs">
            {safeText(report.compile_error)}
          </pre>
        </Alert>
      ) : (
        <p className="text-sm">
          <Badge tone={report.all_passed ? "ok" : "danger"}>
            {report.passed_count} of {report.total} passed
          </Badge>
        </p>
      )}
      {report.cases?.length ? (
        <ul className="space-y-2">
          {report.cases.map((c, i) => (
            <CaseResultRow key={i} r={c} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function SimulationView({ result }: { result: SimulationResult }) {
  return <ReportView title="Simulation result" report={{ ...result, cases: result.results }} />;
}

export function VersionsTable({
  versions,
  canWrite,
  busy,
  onDeploy,
  onLoad,
}: {
  versions: PolicyVersion[];
  canWrite: boolean;
  busy: boolean;
  onDeploy: (v: PolicyVersion) => void;
  onLoad: (v: PolicyVersion) => void;
}) {
  return (
    <Table caption="Policy versions">
      <thead>
        <tr>
          <th className={TH}>Version</th>
          <th className={TH}>Tests</th>
          <th className={TH}>Saved</th>
          <th className={TH}>State</th>
          <th className={TH}>
            <span className="sr-only">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {versions.map((v) => {
          const reason = deployDisabledReason(v);
          const deployDisabled = !canWrite || busy || reason !== null;
          return (
            <tr key={v.version}>
              <td className={TD}>v{v.version}</td>
              <td className={TD}>
                {v.tests_passed === true ? (
                  <Badge tone="ok">passed</Badge>
                ) : v.tests_passed === false ? (
                  <Badge tone="danger">failed</Badge>
                ) : (
                  <Badge>none</Badge>
                )}
              </td>
              <td className={TD}>{fmtDate(v.created_at)}</td>
              <td className={TD}>{v.active ? <Badge tone="info">active</Badge> : "-"}</td>
              <td className={TD}>
                <div className="flex flex-wrap gap-2">
                  <Button onClick={() => onLoad(v)} disabled={busy}>
                    Open<span className="sr-only"> version {v.version}</span>
                  </Button>
                  <Button
                    variant="primary"
                    disabled={deployDisabled}
                    onClick={() => onDeploy(v)}
                    title={!canWrite ? "Only owners and admins can deploy" : (reason ?? undefined)}
                  >
                    Deploy<span className="sr-only"> version {v.version}</span>
                  </Button>
                </div>
                {reason && !v.active ? <p className="mt-1 text-xs text-muted">{reason}</p> : null}
              </td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

export function JsonPre({ value, label }: { value: unknown; label: string }) {
  return (
    <pre
      aria-label={label}
      tabIndex={0}
      className="max-h-64 overflow-auto rounded-md border border-line bg-surface2 p-3 text-xs"
    >
      {safeJson(value)}
    </pre>
  );
}
