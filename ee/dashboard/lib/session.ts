/** The session cookie. The control-plane session token lives only here, httpOnly. */
export const COOKIE_NAME = "kg_session";
/** The control plane's sessions last 12 hours. */
export const SESSION_MAX_AGE_SECONDS = 12 * 60 * 60;

function secure(): boolean {
  const override = process.env.COOKIE_SECURE;
  if (override === "true") return true;
  if (override === "false") return false;
  return process.env.NODE_ENV === "production";
}

/** A `Set-Cookie` value that stores the session token. */
export function sessionCookieHeader(token: string): string {
  const parts = [
    `${COOKIE_NAME}=${encodeURIComponent(token)}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    `Max-Age=${SESSION_MAX_AGE_SECONDS}`,
  ];
  if (secure()) parts.push("Secure");
  return parts.join("; ");
}

/** A `Set-Cookie` value that removes the session cookie. */
export function clearCookieHeader(): string {
  const parts = [`${COOKIE_NAME}=`, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"];
  if (secure()) parts.push("Secure");
  return parts.join("; ");
}

/** Read the session token out of a Cookie request header. */
export function tokenFromCookieHeader(header: string | null): string | null {
  if (!header) return null;
  for (const part of header.split(";")) {
    const idx = part.indexOf("=");
    if (idx < 0) continue;
    if (part.slice(0, idx).trim() === COOKIE_NAME) {
      const value = part.slice(idx + 1).trim();
      try {
        return decodeURIComponent(value) || null;
      } catch {
        return null;
      }
    }
  }
  return null;
}
