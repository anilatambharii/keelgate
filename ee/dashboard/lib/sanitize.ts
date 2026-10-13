// C0/C1 control characters except tab and newline, plus bidirectional overrides/isolates that can
// make text read differently from what it is.
const UNSAFE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f‪-‮⁦-⁩]/g;

/** Strip characters that can spoof or hide text. Output is still rendered as text, never HTML. */
export function safeText(value: unknown): string {
  if (value === null || value === undefined) return "";
  const s = typeof value === "string" ? value : String(value);
  return s.replace(UNSAFE, "");
}

export function safeJson(value: unknown): string {
  try {
    return safeText(JSON.stringify(value, null, 2) ?? "null");
  } catch {
    return "[unprintable]";
  }
}
