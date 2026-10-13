"use client";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { fmtDurationNs } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import { layoutTrace, spanTone, type SpanRow } from "@/lib/spans";
import type { Span } from "@/lib/types";
import { errorMessage } from "./toast";
import {
  Alert,
  Badge,
  Button,
  Card,
  cx,
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

const PAGE = 25;

export function TraceList() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const list = useLoad(
    () => api.traces({ q: q || undefined, errors_only: errorsOnly || undefined, limit: PAGE + 1, offset }),
    [q, errorsOnly, offset],
  );
  const rows = list.data?.items ?? [];
  const hasMore = rows.length > PAGE;
  const shown = rows.slice(0, PAGE);

  function search(e: FormEvent) {
    e.preventDefault();
    setOffset(0);
    setQ(input.trim());
  }

  return (
    <>
      <PageHeader title="Traces" subtitle="Spans shipped by your agents, grouped by trace." />
      <Card className="mb-4">
        <form onSubmit={search} role="search" className="flex flex-wrap items-end gap-3">
          <div className="min-w-[14rem] flex-1">
            <TextField
              label="Search span names"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="e.g. transfer_funds"
            />
          </div>
          <label className="flex items-center gap-2 pb-2 text-sm">
            <input
              type="checkbox"
              checked={errorsOnly}
              onChange={(e) => {
                setOffset(0);
                setErrorsOnly(e.target.checked);
              }}
            />
            Errors only
          </label>
          <Button type="submit" variant="primary">
            Search
          </Button>
        </form>
      </Card>

      {list.error ? (
        <Alert tone="danger">{errorMessage(list.error)}</Alert>
      ) : list.loading && !list.data ? (
        <Spinner />
      ) : shown.length === 0 ? (
        <EmptyState>No traces match. Traces appear after an agent exports spans.</EmptyState>
      ) : (
        <>
          <Table caption="Traces">
            <thead>
              <tr>
                <th className={TH}>Trace</th>
                <th className={TH}>Root span</th>
                <th className={TH}>Spans</th>
                <th className={TH}>Duration</th>
                <th className={TH}>Status</th>
                <th className={TH}>Received</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((t) => (
                <tr key={t.trace_id}>
                  <td className={TD}>
                    <Link href={`/traces/${t.trace_id}`} className="font-mono text-accent underline">
                      {t.trace_id.slice(0, 12)}
                    </Link>
                  </td>
                  <td className={`${TD} max-w-xs truncate`} title={t.root_name ?? ""}>
                    {t.root_name ?? "-"}
                  </td>
                  <td className={TD}>{t.spans}</td>
                  <td className={TD}>
                    {t.start_ns !== null && t.end_ns !== null ? fmtDurationNs(t.end_ns - t.start_ns) : "-"}
                  </td>
                  <td className={TD}>
                    <Badge tone={t.has_error ? "danger" : "ok"}>{t.has_error ? "error" : "ok"}</Badge>
                  </td>
                  <td className={TD}>{fmtDate(t.ingested_at)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
          <nav aria-label="Pagination" className="mt-3 flex items-center justify-between">
            <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              Previous
            </Button>
            <span className="text-sm text-muted">
              {offset + 1} to {offset + shown.length}
            </span>
            <Button disabled={!hasMore} onClick={() => setOffset(offset + PAGE)}>
              Next
            </Button>
          </nav>
        </>
      )}
    </>
  );
}

const TONE_FILL = {
  ok: "fill-ok",
  error: "fill-danger",
  unset: "fill-muted",
} as const;

function Waterfall({ row, totalNs }: { row: SpanRow; totalNs: number }) {
  const W = 100;
  const total = Math.max(totalNs, 1);
  const x = row.offsetNs === null ? 0 : (row.offsetNs / total) * W;
  const w = row.durationNs === null ? 0.5 : Math.max((row.durationNs / total) * W, 0.5);
  return (
    <svg viewBox={`0 0 ${W} 10`} preserveAspectRatio="none" width="100%" height="14" aria-hidden="true">
      <rect x="0" y="4" width={W} height="2" className="fill-surface2" />
      <rect x={Math.min(x, W - 0.5)} y="1" width={Math.min(w, W - x)} height="8" className={TONE_FILL[spanTone(row.span.status)]} />
    </svg>
  );
}

export function TraceDetailView({ id }: { id: string }) {
  const trace = useLoad(() => api.trace(id), [id]);
  const [selected, setSelected] = useState<string | null>(null);

  if (trace.error) {
    return (
      <>
        <PageHeader title="Trace" />
        <Alert tone="danger">{errorMessage(trace.error)}</Alert>
        <p className="mt-3">
          <Link href="/traces" className="text-accent underline">
            Back to traces
          </Link>
        </p>
      </>
    );
  }
  if (!trace.data) return <Spinner />;

  const layout = layoutTrace(trace.data.spans);
  const sel: Span | undefined =
    trace.data.spans.find((s) => s.span_id === selected) ?? layout.rows[0]?.span;
  const errors = trace.data.spans.filter((s) => spanTone(s.status) === "error").length;

  return (
    <>
      <PageHeader
        title="Trace"
        subtitle={
          <>
            <span className="font-mono">{trace.data.trace_id}</span> - {trace.data.spans.length} spans,{" "}
            {fmtDurationNs(layout.totalNs)}
            {errors ? ` - ${errors} with errors` : ""}
          </>
        }
        actions={
          <Link href="/traces" className="text-sm text-accent underline">
            All traces
          </Link>
        }
      />
      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <Card title="Span tree and timing">
          <ul role="tree" aria-label="Spans" className="space-y-0.5">
            {layout.rows.map((r) => {
              const tone = spanTone(r.span.status);
              const isSel = sel?.span_id === r.span.span_id;
              return (
                <li key={r.span.span_id} role="treeitem" aria-selected={isSel} aria-level={r.depth + 1}>
                  <button
                    type="button"
                    onClick={() => setSelected(r.span.span_id)}
                    className={cx(
                      "grid w-full grid-cols-[minmax(8rem,2fr)_3fr_4.5rem] items-center gap-2 rounded px-2 py-1 text-left text-sm hover:bg-surface2",
                      isSel && "bg-infobg",
                    )}
                  >
                    <span className="flex min-w-0 items-center gap-1">
                      <span aria-hidden="true" className="shrink-0 text-muted">
                        {r.depth > 0 ? `${"  ".repeat(r.depth)}└` : ""}
                      </span>
                      <span className={cx("truncate", tone === "error" && "text-danger")}>{r.span.name}</span>
                      {tone === "error" ? <Badge tone="danger">error</Badge> : null}
                    </span>
                    <Waterfall row={r} totalNs={layout.totalNs} />
                    <span className="text-right text-xs text-muted">{fmtDurationNs(r.durationNs)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Card>
        <Card title="Span details">
          {sel ? (
            <div className="space-y-3 text-sm">
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                <dt className="text-muted">Name</dt>
                <dd className="break-words">{sel.name}</dd>
                <dt className="text-muted">Status</dt>
                <dd>
                  <Badge tone={spanTone(sel.status) === "error" ? "danger" : spanTone(sel.status) === "ok" ? "ok" : "neutral"}>
                    {sel.status ?? "unset"}
                  </Badge>
                </dd>
                <dt className="text-muted">Span id</dt>
                <dd className="font-mono text-xs">{sel.span_id}</dd>
                <dt className="text-muted">Parent</dt>
                <dd className="font-mono text-xs">{sel.parent_span_id ?? "none"}</dd>
                <dt className="text-muted">Duration</dt>
                <dd>
                  {sel.start_ns !== null && sel.end_ns !== null ? fmtDurationNs(sel.end_ns - sel.start_ns) : "-"}
                </dd>
              </dl>
              <div>
                <p className="mb-1 font-medium">Attributes</p>
                <p className="mb-1 text-xs text-muted">Set by the agent process. Treat as untrusted text.</p>
                <JsonBlock value={sel.attributes ?? {}} label="Span attributes" />
              </div>
            </div>
          ) : (
            <EmptyState>No spans.</EmptyState>
          )}
        </Card>
      </div>
    </>
  );
}
