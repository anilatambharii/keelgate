import { fmtNumber } from "@/lib/format";

export interface DayPoint {
  day: string; // YYYY-MM-DD
  value: number;
}

/** Fill every day of a YYYY-MM period (missing days are zero). */
export function fillMonth(period: string, points: DayPoint[]): DayPoint[] {
  const m = /^(\d{4})-(\d{2})$/.exec(period);
  if (!m) return points;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const byDay = new Map<string, number>();
  for (const p of points) byDay.set(p.day, (byDay.get(p.day) ?? 0) + p.value);
  const out: DayPoint[] = [];
  for (let d = 1; d <= days; d++) {
    const key = `${m[1]}-${m[2]}-${String(d).padStart(2, "0")}`;
    out.push({ day: key, value: byDay.get(key) ?? 0 });
  }
  return out;
}

/** A simple bar chart in SVG (no chart library, no inline CSS). */
export function DailyChart({ points, label }: { points: DayPoint[]; label: string }) {
  const W = 600;
  const H = 140;
  const pad = { l: 8, r: 8, t: 8, b: 18 };
  const max = Math.max(1, ...points.map((p) => p.value));
  const n = Math.max(points.length, 1);
  const slot = (W - pad.l - pad.r) / n;
  const bar = Math.max(slot - 2, 1);
  const total = points.reduce((s, p) => s + p.value, 0);
  return (
    <figure>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={`${label}: ${fmtNumber(total)} in total, peak ${fmtNumber(max === 1 && total === 0 ? 0 : max)} in a day`}
        className="block h-auto max-w-full"
      >
        <line
          x1={pad.l}
          x2={W - pad.r}
          y1={H - pad.b}
          y2={H - pad.b}
          className="stroke-line"
          strokeWidth="1"
        />
        {points.map((p, i) => {
          const h = Math.round(((H - pad.t - pad.b) * p.value) / max);
          const x = pad.l + i * slot + (slot - bar) / 2;
          return (
            <rect
              key={p.day}
              x={x}
              y={H - pad.b - h}
              width={bar}
              height={h}
              className="fill-accent"
              rx="1"
            >
              <title>{`${p.day}: ${fmtNumber(p.value)}`}</title>
            </rect>
          );
        })}
        {points.length > 0 ? (
          <>
            <text x={pad.l} y={H - 4} className="fill-muted" fontSize="10">
              {points[0].day.slice(5)}
            </text>
            <text x={W - pad.r} y={H - 4} textAnchor="end" className="fill-muted" fontSize="10">
              {points[points.length - 1].day.slice(5)}
            </text>
          </>
        ) : null}
      </svg>
      <figcaption className="mt-1 text-xs text-muted">{label}</figcaption>
      <details className="mt-1 text-xs">
        <summary className="cursor-pointer text-muted">Show as a table</summary>
        <table className="mt-1 text-left">
          <thead>
            <tr>
              <th className="pr-4 font-medium">Day</th>
              <th className="font-medium">Count</th>
            </tr>
          </thead>
          <tbody>
            {points
              .filter((p) => p.value > 0)
              .map((p) => (
                <tr key={p.day}>
                  <td className="pr-4">{p.day}</td>
                  <td>{fmtNumber(p.value)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
