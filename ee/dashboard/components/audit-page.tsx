"use client";
import { useState, type FormEvent } from "react";
import { api, ApiError } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import { safeText } from "@/lib/sanitize";
import { isAdmin, useMe } from "./me-context";
import { errorMessage, useToast } from "./toast";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  fmtDate,
  JsonBlock,
  PageHeader,
  Spinner,
  Table,
  TD,
  TextField,
  TH,
} from "./ui";

const PAGE = 50;
export const VERIFIER_URL = "https://github.com/anilatambharii/keelgate/tree/main/src/keelgate/audit";

export function AuditPage() {
  const me = useMe();
  const toast = useToast();
  const canExport = me.user.role !== "viewer";
  const planHasCompliance = me.org.features.includes("compliance_exports");

  const [eventType, setEventType] = useState("");
  const [actor, setActor] = useState("");
  const [applied, setApplied] = useState({ eventType: "", actor: "" });
  const [newestFirst, setNewestFirst] = useState(true);
  // Cursor per page: the `after_seq` that produced it. Page 1 is cursor 0.
  const [cursors, setCursors] = useState<number[]>([0]);
  const [open, setOpen] = useState<number | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [planMessage, setPlanMessage] = useState<string | null>(null);

  const cursor = cursors[cursors.length - 1];
  const page = useLoad(
    () =>
      api.audit({
        after_seq: cursor || undefined,
        limit: PAGE,
        event_type: applied.eventType || undefined,
        actor: applied.actor || undefined,
        newest_first: newestFirst,
      }),
    [cursor, applied, newestFirst],
  );
  // The head of the chain is the newest record, whatever filter is applied to the list.
  const head = useLoad(() => api.audit({ limit: 1, newest_first: true }), []);
  const headRecord = head.data?.records[0];

  function apply(e: FormEvent) {
    e.preventDefault();
    setCursors([0]);
    setOpen(null);
    setApplied({ eventType: eventType.trim(), actor: actor.trim() });
  }

  async function doExport(kind: "json" | "csv" | "compliance") {
    setExporting(kind);
    setPlanMessage(null);
    try {
      const name = kind === "compliance" ? await api.exportCompliance() : await api.exportAudit(kind);
      toast.success(`Downloaded ${name}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 402) {
        setPlanMessage(err.message);
      } else {
        toast.error(err);
      }
    } finally {
      setExporting(null);
    }
  }

  const records = page.data?.records ?? [];
  const lastSeq = records.length ? records[records.length - 1].seq : 0;

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle="A tamper-evident chain: each record contains the hash of the one before it."
      />

      <div className="grid gap-4">
        <Card title="Chain head">
          {head.error ? (
            <Alert tone="danger">{errorMessage(head.error)}</Alert>
          ) : !head.data ? (
            <Spinner />
          ) : headRecord ? (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-muted">Sequence</dt>
              <dd>{headRecord.seq}</dd>
              <dt className="text-muted">Head hash</dt>
              <dd className="break-all font-mono text-xs" data-testid="head-hash">
                {headRecord.hash}
              </dd>
              <dt className="text-muted">Recorded</dt>
              <dd>{fmtDate(headRecord.timestamp)}</dd>
            </dl>
          ) : (
            <p className="text-sm text-muted">The chain is empty.</p>
          )}
          <details className="mt-3 text-sm">
            <summary className="cursor-pointer font-medium">How to verify this chain yourself</summary>
            <div className="mt-2 space-y-2 text-muted">
              <p>
                You do not have to trust this page. Export the chain as JSON and check it with the
                open-source verifier, which needs nothing from Keelgate Cloud:
              </p>
              <pre tabIndex={0} className="overflow-auto rounded-md border border-line bg-surface2 p-3 text-xs text-fg">
                {`pip install keelgate\nfrom keelgate.audit import verify_chain\n# records: the AuditRecord objects from the export; tenant_id: ${me.org.id}\nverify_chain(records, tenant_id="${me.org.id}")`}
              </pre>
              <p>
                Compare the final hash with the head hash above.{" "}
                <a
                  href={VERIFIER_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-accent underline"
                >
                  keelgate.audit.verify_chain (source, Apache-2.0)
                </a>
              </p>
            </div>
          </details>
        </Card>

        <Card title="Export">
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => doExport("json")} disabled={!canExport || exporting !== null}>
              {exporting === "json" ? "Preparing..." : "Export JSON"}
            </Button>
            <Button onClick={() => doExport("csv")} disabled={!canExport || exporting !== null}>
              {exporting === "csv" ? "Preparing..." : "Export CSV"}
            </Button>
            <Button onClick={() => doExport("compliance")} disabled={!canExport || exporting !== null}>
              {exporting === "compliance" ? "Preparing..." : "Compliance export (ZIP)"}
            </Button>
          </div>
          {!canExport ? (
            <p className="mt-2 text-xs text-muted">Viewers cannot export the audit log.</p>
          ) : !planHasCompliance ? (
            <p className="mt-2 text-xs text-muted">
              The compliance ZIP is not part of the {me.org.plan_name} plan.
              {isAdmin(me.user.role) ? " See the billing page to upgrade." : ""}
            </p>
          ) : null}
          {planMessage ? (
            <div className="mt-3">
              <Alert tone="warn" title="Plan upgrade required">
                {planMessage}
              </Alert>
            </div>
          ) : null}
          <p className="mt-2 text-xs text-muted">Each export is itself recorded in the audit log.</p>
        </Card>

        <Card title="Records">
          <form onSubmit={apply} role="search" className="mb-4 flex flex-wrap items-end gap-3">
            <div className="w-56">
              <TextField
                label="Event type"
                placeholder="approval.decided"
                value={eventType}
                onChange={(e) => setEventType(e.target.value)}
              />
            </div>
            <div className="w-56">
              <TextField label="Actor" value={actor} onChange={(e) => setActor(e.target.value)} />
            </div>
            <label className="flex items-center gap-2 pb-2 text-sm">
              <input
                type="checkbox"
                checked={newestFirst}
                onChange={(e) => {
                  setCursors([0]);
                  setNewestFirst(e.target.checked);
                }}
              />
              Newest first
            </label>
            <Button type="submit" variant="primary">
              Apply filters
            </Button>
          </form>

          {page.error ? (
            <Alert tone="danger">{errorMessage(page.error)}</Alert>
          ) : page.loading && !page.data ? (
            <Spinner />
          ) : records.length === 0 ? (
            <EmptyState>No audit records match.</EmptyState>
          ) : (
            <>
              <Table caption="Audit records">
                <thead>
                  <tr>
                    <th className={TH}>#</th>
                    <th className={TH}>Time</th>
                    <th className={TH}>Event</th>
                    <th className={TH}>Actor</th>
                    <th className={TH}>Hash</th>
                    <th className={TH}>
                      <span className="sr-only">Payload</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((r) => (
                    <RecordRow key={r.seq} open={open === r.seq} onToggle={() => setOpen(open === r.seq ? null : r.seq)} r={r} />
                  ))}
                </tbody>
              </Table>
              <nav aria-label="Pagination" className="mt-3 flex items-center justify-between">
                <Button
                  disabled={cursors.length <= 1}
                  onClick={() => {
                    setOpen(null);
                    setCursors(cursors.slice(0, -1));
                  }}
                >
                  Previous
                </Button>
                <span className="text-sm text-muted">
                  #{records[0].seq} to #{lastSeq}
                </span>
                <Button
                  disabled={!page.data?.has_more}
                  onClick={() => {
                    setOpen(null);
                    setCursors([...cursors, lastSeq]);
                  }}
                >
                  Next
                </Button>
              </nav>
            </>
          )}
        </Card>
      </div>
    </>
  );
}

function RecordRow({
  r,
  open,
  onToggle,
}: {
  r: import("@/lib/types").AuditRecord;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr>
        <td className={TD}>{r.seq}</td>
        <td className={TD}>{fmtDate(r.timestamp)}</td>
        <td className={TD}>
          <Badge tone="info">{safeText(r.event_type)}</Badge>
        </td>
        <td className={`${TD} max-w-[12rem] truncate`} title={safeText(r.actor)}>
          {safeText(r.actor)}
        </td>
        <td className={`${TD} font-mono text-xs`} title={r.hash}>
          {r.hash.slice(0, 12)}
        </td>
        <td className={TD}>
          <Button aria-expanded={open} onClick={onToggle}>
            {open ? "Hide" : "Payload"}
            <span className="sr-only"> for record {r.seq}</span>
          </Button>
        </td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={6} className={TD}>
            <p className="mb-1 text-xs text-muted">
              prev_hash <span className="font-mono">{r.prev_hash ?? "(genesis)"}</span>
            </p>
            <p className="mb-1 break-all text-xs text-muted">
              hash <span className="font-mono">{r.hash}</span>
            </p>
            <JsonBlock value={r.payload} label={`Payload of record ${r.seq}`} />
          </td>
        </tr>
      ) : null}
    </>
  );
}
