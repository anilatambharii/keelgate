import { fmtNumber } from "@/lib/format";

/** A labelled usage bar. Drawn as SVG so it needs no inline CSS (the CSP forbids inline styles). */
export function Meter({
  label,
  value,
  cap,
  unit,
}: {
  label: string;
  value: number;
  cap: number | null;
  unit?: string;
}) {
  const pct = cap && cap > 0 ? Math.min(100, (value / cap) * 100) : 0;
  const remaining = cap === null ? null : Math.max(cap - value, 0);
  const tone = pct >= 100 ? "fill-danger" : pct >= 80 ? "fill-warn" : "fill-accent";
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="font-medium">{label}</span>
        <span className="text-muted">
          {fmtNumber(value)}
          {cap === null ? " (no cap on this plan)" : ` of ${fmtNumber(cap)}`}
          {unit ? ` ${unit}` : ""}
        </span>
      </div>
      {cap !== null ? (
        <svg
          role="progressbar"
          aria-label={`${label} used`}
          aria-valuemin={0}
          aria-valuemax={cap}
          aria-valuenow={Math.min(value, cap)}
          width="100%"
          height="8"
          className="mt-1 block overflow-hidden rounded"
        >
          <rect width="100%" height="8" className="fill-surface2" />
          <rect width={`${pct}%`} height="8" className={tone} />
        </svg>
      ) : null}
      {remaining !== null ? (
        <p className="mt-0.5 text-xs text-muted">{fmtNumber(remaining)} remaining this period</p>
      ) : null}
    </div>
  );
}
