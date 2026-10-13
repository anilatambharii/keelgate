export function daysLeft(iso: string | null | undefined, now: Date = new Date()): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.ceil((t - now.getTime()) / 86_400_000));
}

export function fmtCents(cents: number | null | undefined, currency = "usd"): string {
  if (cents === null || cents === undefined) return "-";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: currency.toUpperCase() }).format(
      cents / 100,
    );
  } catch {
    return `${(cents / 100).toFixed(2)} ${currency.toUpperCase()}`;
  }
}

export function fmtNumber(n: number | null | undefined): string {
  return n === null || n === undefined ? "-" : new Intl.NumberFormat("en-US").format(n);
}

/** Duration in nanoseconds as a short human string. */
export function fmtDurationNs(ns: number | null | undefined): string {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return "-";
  if (ns < 1_000) return `${Math.round(ns)} ns`;
  if (ns < 1_000_000) return `${(ns / 1_000).toFixed(1)} us`;
  if (ns < 1_000_000_000) return `${(ns / 1_000_000).toFixed(1)} ms`;
  return `${(ns / 1_000_000_000).toFixed(2)} s`;
}

/** "3m 12s" until `expires`, or "expired". */
export function countdown(expiresIso: string, now: Date = new Date()): string {
  const ms = new Date(expiresIso).getTime() - now.getTime();
  if (Number.isNaN(ms)) return "-";
  if (ms <= 0) return "expired";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}
