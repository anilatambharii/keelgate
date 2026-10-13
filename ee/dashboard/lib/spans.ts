import type { Span } from "./types";

export interface SpanRow {
  span: Span;
  depth: number;
  /** Offset from the trace start and duration, in nanoseconds (null when unknown). */
  offsetNs: number | null;
  durationNs: number | null;
}

export interface TraceLayout {
  rows: SpanRow[];
  startNs: number;
  endNs: number;
  totalNs: number;
}

function start(s: Span): number {
  return s.start_ns ?? Number.POSITIVE_INFINITY;
}

/** Order spans parent-first (depth-first by start time) with an indent depth for each. */
export function layoutTrace(spans: Span[]): TraceLayout {
  const byId = new Map<string, Span>();
  for (const s of spans) byId.set(s.span_id, s);
  const children = new Map<string, Span[]>();
  const roots: Span[] = [];
  for (const s of spans) {
    const p = s.parent_span_id;
    if (p && p !== s.span_id && byId.has(p)) {
      const list = children.get(p) ?? [];
      list.push(s);
      children.set(p, list);
    } else {
      roots.push(s); // a true root, or an orphan whose parent was never ingested
    }
  }
  const byStart = (a: Span, b: Span) => (start(a) < start(b) ? -1 : start(a) > start(b) ? 1 : 0);
  roots.sort(byStart);

  const starts = spans.map((s) => s.start_ns).filter((n): n is number => typeof n === "number");
  const ends = spans
    .map((s) => s.end_ns ?? s.start_ns)
    .filter((n): n is number => typeof n === "number");
  const startNs = starts.length ? Math.min(...starts) : 0;
  const endNs = ends.length ? Math.max(...ends) : startNs;

  const rows: SpanRow[] = [];
  const seen = new Set<string>();
  const visit = (s: Span, depth: number) => {
    if (seen.has(s.span_id)) return; // guards against cycles in malformed data
    seen.add(s.span_id);
    rows.push({
      span: s,
      depth,
      offsetNs: typeof s.start_ns === "number" ? s.start_ns - startNs : null,
      durationNs:
        typeof s.start_ns === "number" && typeof s.end_ns === "number"
          ? Math.max(0, s.end_ns - s.start_ns)
          : null,
    });
    for (const c of (children.get(s.span_id) ?? []).sort(byStart)) visit(c, depth + 1);
  };
  for (const r of roots) visit(r, 0);
  // Anything left is part of a cycle: show it rather than hide it.
  for (const s of spans) visit(s, 0);
  return { rows, startNs, endNs, totalNs: Math.max(endNs - startNs, 0) };
}

export type SpanTone = "ok" | "error" | "unset";

export function spanTone(status: string | null | undefined): SpanTone {
  const s = (status ?? "").toUpperCase();
  if (s === "ERROR") return "error";
  if (s === "OK") return "ok";
  return "unset";
}
