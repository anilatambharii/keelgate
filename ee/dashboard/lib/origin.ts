/** CSRF defence for state-changing requests: the request must come from this dashboard's origin. */
export const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function isSameOrigin(headers: Headers, fallbackHost?: string): boolean {
  const host = headers.get("host") ?? fallbackHost ?? null;
  const origin = headers.get("origin");
  if (origin) {
    if (!host) return false;
    try {
      return new URL(origin).host === host;
    } catch {
      return false;
    }
  }
  // No Origin header (some same-origin requests omit it): trust only the browser's own signal.
  const site = headers.get("sec-fetch-site");
  return site === "same-origin" || site === "none";
}
