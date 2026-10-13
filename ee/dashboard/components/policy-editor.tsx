"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  blankCase,
  casesToDrafts,
  parseCases,
  refusalReport,
  type CaseDraft,
} from "@/lib/policy";
import type { PolicyTemplate, PolicyVersion, SimulationResult, TestReport } from "@/lib/types";
import { isAdmin, useMe } from "./me-context";
import { ReportView, SimulationView, VersionsTable } from "./policy-parts";
import { errorMessage, useToast } from "./toast";
import { Alert, Button, Card, ConfirmDialog, cx, PageHeader, SelectField, Spinner, TextAreaField, TextField } from "./ui";

const STARTER_FILE = "policy.rego";

export interface EditorProps {
  /** An existing pack to edit; omit to create a new one. */
  name?: string;
}

export function PolicyEditor({ name: existingName }: EditorProps) {
  const me = useMe();
  const toast = useToast();
  const router = useRouter();
  const canWrite = isAdmin(me.user.role);

  const [name, setName] = useState(existingName ?? "");
  const [files, setFiles] = useState<Record<string, string>>({ [STARTER_FILE]: "" });
  const [activeFile, setActiveFile] = useState(STARTER_FILE);
  const [query, setQuery] = useState("");
  const [drafts, setDrafts] = useState<CaseDraft[]>([]);
  const [caseErrors, setCaseErrors] = useState<Record<string, string>>({});
  const [newFile, setNewFile] = useState("");

  const [versions, setVersions] = useState<PolicyVersion[]>([]);
  const [loading, setLoading] = useState(Boolean(existingName));
  const [loadError, setLoadError] = useState<string | null>(null);
  const [templates, setTemplates] = useState<PolicyTemplate[]>([]);

  const [sim, setSim] = useState<SimulationResult | null>(null);
  const [simBusy, setSimBusy] = useState(false);
  const [saveBusy, setSaveBusy] = useState(false);
  const [deployBusy, setDeployBusy] = useState(false);
  const [refusal, setRefusal] = useState<{ message: string; report: TestReport | null } | null>(null);
  const [confirmRollback, setConfirmRollback] = useState(false);
  const [saveReport, setSaveReport] = useState<TestReport | null>(null);

  const loadVersion = useCallback(async (n: number, pack: string) => {
    const v = await api.policyVersion(pack, n);
    setFiles(v.files ?? {});
    setActiveFile(Object.keys(v.files ?? {})[0] ?? STARTER_FILE);
    setQuery(v.query ?? "");
    setDrafts(casesToDrafts(v.cases));
    setCaseErrors({});
    setSim(null);
  }, []);

  const reloadVersions = useCallback(
    async (pack: string, loadLatest: boolean) => {
      const res = await api.policyVersions(pack);
      const items = res.items ?? [];
      setVersions(items);
      if (loadLatest && items.length > 0) await loadVersion(items[0].version, pack);
    },
    [loadVersion],
  );

  useEffect(() => {
    if (!existingName) return;
    let cancelled = false;
    (async () => {
      try {
        await reloadVersions(existingName, true);
      } catch (e) {
        if (!cancelled) setLoadError(errorMessage(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [existingName, reloadVersions]);

  useEffect(() => {
    api
      .policyTemplates()
      .then((r) => setTemplates(r.items ?? []))
      .catch(() => setTemplates([]));
  }, []);

  const fileNames = useMemo(() => Object.keys(files), [files]);
  const activeVersion = versions.find((v) => v.active);

  function applyTemplate(t: PolicyTemplate) {
    setFiles(t.files);
    setActiveFile(Object.keys(t.files)[0] ?? STARTER_FILE);
    setQuery(t.query);
    setDrafts(casesToDrafts(t.cases));
    setCaseErrors({});
    setSim(null);
    if (!existingName && !name) setName(t.name);
    toast.info(`Loaded the ${t.name} template. Review it, then simulate.`);
  }

  function addFile() {
    const n = newFile.trim();
    if (!/^[A-Za-z0-9_.-]+\.rego$/.test(n)) {
      toast.error(new Error("File names must end in .rego and use letters, digits, dot, dash or underscore"));
      return;
    }
    if (files[n] !== undefined) {
      toast.error(new Error("A file with that name already exists"));
      return;
    }
    setFiles({ ...files, [n]: "" });
    setActiveFile(n);
    setNewFile("");
  }

  function removeFile(n: string) {
    const next = { ...files };
    delete next[n];
    if (Object.keys(next).length === 0) next[STARTER_FILE] = "";
    setFiles(next);
    setActiveFile(Object.keys(next)[0]);
  }

  async function simulate() {
    const parsed = parseCases(drafts);
    setCaseErrors(parsed.errors);
    if (Object.keys(parsed.errors).length > 0) {
      toast.error(new Error("Fix the highlighted test cases first"));
      return;
    }
    if (parsed.cases.length === 0) {
      toast.error(new Error("Add at least one test case to simulate"));
      return;
    }
    setSimBusy(true);
    setSim(null);
    try {
      setSim(await api.simulate({ files, query: query.trim(), cases: parsed.cases }));
    } catch (e) {
      toast.error(e);
    } finally {
      setSimBusy(false);
    }
  }

  async function save() {
    const parsed = parseCases(drafts);
    setCaseErrors(parsed.errors);
    if (Object.keys(parsed.errors).length > 0) {
      toast.error(new Error("Fix the highlighted test cases first"));
      return;
    }
    if (parsed.cases.some((c) => !c.expect)) {
      toast.error(new Error('Saved cases need an "expect" so the tests can pass or fail'));
      return;
    }
    const body = { files, query: query.trim(), cases: parsed.cases };
    setSaveBusy(true);
    setRefusal(null);
    try {
      if (!existingName) {
        const pack = name.trim();
        const created = await api.createPolicy({ name: pack, ...body });
        setSaveReport(created.test_report ?? null);
        toast.success(`Saved ${pack} v${created.version}. Tests ${created.tests_passed ? "passed" : "did not pass"}.`);
        router.push(`/policies/${encodeURIComponent(pack)}`);
        return;
      }
      const created = await api.createPolicyVersion(existingName, body);
      setSaveReport(created.test_report ?? null);
      toast.success(`Saved v${created.version}. Tests ${created.tests_passed ? "passed" : "did not pass"}.`);
      await reloadVersions(existingName, false);
    } catch (e) {
      toast.error(e);
    } finally {
      setSaveBusy(false);
    }
  }

  async function deploy(v: PolicyVersion) {
    if (!existingName) return;
    setDeployBusy(true);
    setRefusal(null);
    try {
      await api.deployPolicy(existingName, v.version);
      toast.success(`Deployed v${v.version}. Agents pick it up on their next refresh.`);
      await reloadVersions(existingName, false);
    } catch (e) {
      const r = refusalReport(e);
      if (r) setRefusal({ message: r.message, report: r.report });
      toast.error(e);
    } finally {
      setDeployBusy(false);
    }
  }

  async function rollback() {
    if (!existingName) return;
    setDeployBusy(true);
    try {
      await api.rollbackPolicy(existingName);
      toast.success("Rolled back to the previous active version");
      setConfirmRollback(false);
      await reloadVersions(existingName, false);
    } catch (e) {
      toast.error(e);
    } finally {
      setDeployBusy(false);
    }
  }

  if (loading) return <Spinner />;
  if (loadError) {
    return (
      <>
        <PageHeader title="Policy" />
        <Alert tone="danger">{loadError}</Alert>
        <p className="mt-3">
          <Link href="/policies" className="text-accent underline">
            Back to policies
          </Link>
        </p>
      </>
    );
  }

  const canSave = canWrite && !saveBusy && query.trim() !== "" && (existingName || name.trim() !== "");

  return (
    <>
      <PageHeader
        title={existingName ? existingName : "New policy pack"}
        subtitle={
          existingName
            ? activeVersion
              ? `Active version: v${activeVersion.version}`
              : "No version is deployed yet."
            : "Write Rego, add test cases, simulate, then save a version and deploy it."
        }
        actions={
          <>
            <Link href="/policies" className="text-sm text-accent underline">
              All policies
            </Link>
            {existingName && canWrite && versions.length > 1 && activeVersion ? (
              <Button onClick={() => setConfirmRollback(true)} disabled={deployBusy}>
                Roll back
              </Button>
            ) : null}
          </>
        }
      />

      {!canWrite ? (
        <div className="mb-4">
          <Alert tone="warn">You can read policies. Only owners and admins can simulate, save or deploy.</Alert>
        </div>
      ) : null}

      <div className="grid gap-4">
        <Card
          title="Sources"
          actions={
            templates.length > 0 ? (
              <div className="w-56">
                <SelectField
                  label="Start from template"
                  value=""
                  onChange={(e) => {
                    const t = templates.find((x) => x.name === e.target.value);
                    if (t) applyTemplate(t);
                  }}
                  disabled={!canWrite}
                >
                  <option value="">Choose...</option>
                  {templates.map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name}
                    </option>
                  ))}
                </SelectField>
              </div>
            ) : null
          }
        >
          {!existingName ? (
            <div className="mb-3 max-w-sm">
              <TextField
                label="Pack name"
                hint="Agents refer to the pack by this name."
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
                disabled={!canWrite}
              />
            </div>
          ) : null}

          <div role="tablist" aria-label="Policy files" className="mb-2 flex flex-wrap gap-1 border-b border-line">
            {fileNames.map((n) => (
              <button
                key={n}
                role="tab"
                type="button"
                aria-selected={n === activeFile}
                onClick={() => setActiveFile(n)}
                className={cx(
                  "-mb-px border-b-2 px-3 py-1.5 font-mono text-sm",
                  n === activeFile ? "border-accent text-accent" : "border-transparent text-muted hover:text-fg",
                )}
              >
                {n}
              </button>
            ))}
          </div>
          <TextAreaField
            label={`Contents of ${activeFile}`}
            className="mono min-h-[18rem] whitespace-pre font-mono text-xs"
            spellCheck={false}
            wrap="off"
            value={files[activeFile] ?? ""}
            onChange={(e) => setFiles({ ...files, [activeFile]: e.target.value })}
            disabled={!canWrite}
          />
          {canWrite ? (
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <div className="w-56">
                <TextField
                  label="New file"
                  placeholder="helpers.rego"
                  value={newFile}
                  onChange={(e) => setNewFile(e.target.value)}
                />
              </div>
              <Button onClick={addFile} disabled={!newFile.trim()}>
                Add file
              </Button>
              {fileNames.length > 1 ? (
                <Button onClick={() => removeFile(activeFile)}>Remove {activeFile}</Button>
              ) : null}
            </div>
          ) : null}
          <div className="mt-4 max-w-xl">
            <TextField
              label="Query"
              hint="The Rego rule that returns the decision, for example data.acme.starter.decision"
              className="font-mono"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              maxLength={300}
              disabled={!canWrite}
            />
          </div>
        </Card>

        <Card
          title={`Test cases (${drafts.length})`}
          actions={
            canWrite ? (
              <Button onClick={() => setDrafts([...drafts, blankCase()])}>Add case</Button>
            ) : null
          }
        >
          {drafts.length === 0 ? (
            <p className="text-sm text-muted">
              No cases yet. A version can only be deployed when it has cases and every case passes.
            </p>
          ) : (
            <ul className="space-y-3">
              {drafts.map((d, i) => (
                <li key={d.key}>
                  <TextAreaField
                    label={`Case ${i + 1} (JSON: name, input, expect)`}
                    className="mono min-h-[10rem] font-mono text-xs"
                    spellCheck={false}
                    value={d.text}
                    aria-invalid={caseErrors[d.key] ? true : undefined}
                    onChange={(e) =>
                      setDrafts(drafts.map((x) => (x.key === d.key ? { ...x, text: e.target.value } : x)))
                    }
                    disabled={!canWrite}
                  />
                  {caseErrors[d.key] ? (
                    <p role="alert" className="mt-1 text-xs text-danger">
                      {caseErrors[d.key]}
                    </p>
                  ) : null}
                  {canWrite ? (
                    <Button className="mt-1" onClick={() => setDrafts(drafts.filter((x) => x.key !== d.key))}>
                      Remove case {i + 1}
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Try it">
          <div className="flex flex-wrap gap-2">
            <Button onClick={simulate} disabled={!canWrite || simBusy || query.trim() === ""}>
              {simBusy ? "Simulating..." : "Simulate"}
            </Button>
            <Button variant="primary" onClick={save} disabled={!canSave}>
              {saveBusy ? "Saving..." : "Save as new version"}
            </Button>
          </div>
          <p className="mt-2 text-xs text-muted">
            Simulate evaluates the cases against these sources without saving. Saving creates an
            immutable version and runs its tests on the server; a version is deployable only if they pass.
          </p>
          {sim ? (
            <div className="mt-3">
              <SimulationView result={sim} />
            </div>
          ) : null}
          {saveReport ? (
            <div className="mt-3">
              <p className="mb-1 text-sm font-medium">Last saved version: server test run</p>
              <ReportView title="Server test run" report={saveReport} />
            </div>
          ) : null}
        </Card>

        {existingName ? (
          <Card title="Versions">
            {refusal ? (
              <div className="mb-3 space-y-2">
                <Alert tone="danger" title="Deploy refused">
                  {refusal.message}
                </Alert>
                {refusal.report ? <ReportView title="Tests that stopped the deploy" report={refusal.report} /> : null}
              </div>
            ) : null}
            <VersionsTable
              versions={versions}
              canWrite={canWrite}
              busy={deployBusy}
              onDeploy={deploy}
              onLoad={(v) => {
                loadVersion(v.version, existingName).then(
                  () => toast.info(`Loaded v${v.version} into the editor`),
                  (e) => toast.error(e),
                );
              }}
            />
          </Card>
        ) : null}
      </div>

      {confirmRollback ? (
        <ConfirmDialog
          title="Roll back the active version?"
          message="The previously active version becomes active again. Agents use it on their next refresh."
          confirmLabel="Roll back"
          busy={deployBusy}
          onConfirm={rollback}
          onCancel={() => setConfirmRollback(false)}
        />
      ) : null}
    </>
  );
}
