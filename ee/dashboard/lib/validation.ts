export const MIN_PASSWORD_LENGTH = 12;

export interface PasswordHint {
  ok: boolean;
  text: string;
}

/** Live hint for the sign-up password field. The server is the authority on the rules. */
export function passwordHint(password: string): PasswordHint {
  const n = password.length;
  if (n === 0) return { ok: false, text: `Use at least ${MIN_PASSWORD_LENGTH} characters.` };
  if (n < MIN_PASSWORD_LENGTH) {
    return {
      ok: false,
      text: `${MIN_PASSWORD_LENGTH - n} more character${MIN_PASSWORD_LENGTH - n === 1 ? "" : "s"} needed (${n}/${MIN_PASSWORD_LENGTH}).`,
    };
  }
  return { ok: true, text: `Long enough (${n} characters).` };
}

/** Only same-site relative paths are acceptable post-login targets. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return "/";
  return next;
}

export function validEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}
