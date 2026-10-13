"use client";
import Link from "next/link";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "secondary" | "danger" | "ghost";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accentfg hover:opacity-90 border border-transparent",
  secondary: "bg-surface text-fg border border-line hover:bg-surface2",
  danger: "bg-danger text-accentfg border border-transparent hover:opacity-90",
  ghost: "bg-transparent text-fg border border-transparent hover:bg-surface2",
};
const BTN =
  "inline-flex items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";

export function Button({
  variant = "secondary",
  className,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <button type={type} className={cx(BTN, VARIANTS[variant], className)} {...rest} />;
}

export function LinkButton({
  href,
  variant = "secondary",
  children,
  className,
}: {
  href: string;
  variant?: Variant;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Link href={href} className={cx(BTN, VARIANTS[variant], className)}>
      {children}
    </Link>
  );
}

const INPUT =
  "w-full rounded-md border border-line bg-surface px-3 py-1.5 text-sm text-fg placeholder:text-muted disabled:opacity-60";

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={htmlFor} className="block text-sm font-medium">
        {label}
      </label>
      {children}
      {hint ? <p className="text-xs text-muted">{hint}</p> : null}
      {error ? (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** A labelled text input: the label is always programmatically associated. */
export function TextField({
  label,
  hint,
  error,
  className,
  id,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: ReactNode;
  error?: string | null;
}) {
  const auto = useId();
  const inputId = id ?? auto;
  return (
    <Field label={label} hint={hint} error={error} htmlFor={inputId}>
      <input id={inputId} className={cx(INPUT, className)} {...rest} />
    </Field>
  );
}

export function SelectField({
  label,
  hint,
  className,
  id,
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement> & { label: string; hint?: ReactNode }) {
  const auto = useId();
  const selId = id ?? auto;
  return (
    <Field label={label} hint={hint} htmlFor={selId}>
      <select id={selId} className={cx(INPUT, className)} {...rest}>
        {children}
      </select>
    </Field>
  );
}

export function TextAreaField({
  label,
  hint,
  className,
  id,
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { label: string; hint?: ReactNode }) {
  const auto = useId();
  const taId = id ?? auto;
  return (
    <Field label={label} hint={hint} htmlFor={taId}>
      <textarea id={taId} className={cx(INPUT, className)} {...rest} />
    </Field>
  );
}

export function Card({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx("rounded-lg border border-line bg-surface p-4", className)}>
      {title || actions ? (
        <div className="mb-3 flex items-center justify-between gap-2">
          {title ? <h2 className="text-base font-semibold">{title}</h2> : <span />}
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export type Tone = "neutral" | "ok" | "warn" | "danger" | "info";
const TONES: Record<Tone, string> = {
  neutral: "bg-surface2 text-muted",
  ok: "bg-okbg text-ok",
  warn: "bg-warnbg text-warn",
  danger: "bg-dangerbg text-danger",
  info: "bg-infobg text-accent",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={cx("inline-block rounded px-1.5 py-0.5 text-xs font-medium", TONES[tone])}>
      {children}
    </span>
  );
}

export function Alert({
  tone = "info",
  title,
  children,
  role,
}: {
  tone?: Tone;
  title?: string;
  children?: ReactNode;
  role?: "alert" | "status";
}) {
  return (
    <div
      role={role ?? (tone === "danger" ? "alert" : "status")}
      className={cx("rounded-md border border-line px-3 py-2 text-sm", TONES[tone])}
    >
      {title ? <p className="font-semibold">{title}</p> : null}
      {children ? <div>{children}</div> : null}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <p role="status" className="text-sm text-muted">
      {label}...
    </p>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {subtitle ? <p className="mt-0.5 text-sm text-muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </header>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-line px-4 py-8 text-center text-sm text-muted">
      {children}
    </p>
  );
}

/** Untrusted text (agent- or model-written) in a clearly labelled box. Always rendered as text. */
export function UntrustedBox({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-md border border-warn/60 bg-warnbg/40">
      <p className="border-b border-warn/40 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-warn">
        {label} - written by an agent, not verified
      </p>
      <div className="whitespace-pre-wrap break-words px-3 py-2 text-sm">{children}</div>
    </div>
  );
}

/** JSON (or any value) as pretty-printed text. Never HTML. */
export function JsonBlock({ value, label }: { value: unknown; label?: string }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? "null";
  } catch {
    text = String(value);
  }
  return (
    <pre
      aria-label={label}
      tabIndex={0}
      className="max-h-96 overflow-auto rounded-md border border-line bg-surface2 p-3 text-xs"
    >
      {text}
    </pre>
  );
}

/** An accessible modal dialog: focus moves in, Escape closes, Tab stays inside. */
export function Modal({
  title,
  onClose,
  children,
  dismissable = true,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  dismissable?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const node = ref.current;
    const previous = document.activeElement as HTMLElement | null;
    const focusable = () =>
      Array.from(
        node?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
    focusable()[0]?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && dismissable) {
        e.stopPropagation();
        onClose();
      } else if (e.key === "Tab") {
        const items = focusable();
        if (items.length === 0) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [onClose, dismissable]);
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4">
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="max-h-[90vh] w-full max-w-lg overflow-auto rounded-lg border border-line bg-surface p-5 shadow-xl"
      >
        <h2 id={titleId} className="mb-3 text-lg font-semibold">
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <div className="mb-4 text-sm">{message}</div>
      <div className="flex justify-end gap-2">
        <Button onClick={onCancel}>Cancel</Button>
        <Button variant="danger" onClick={onConfirm} disabled={busy}>
          {busy ? "Working..." : confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <Button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setState("copied");
        } catch {
          setState("failed");
        }
        setTimeout(() => setState("idle"), 2000);
      }}
    >
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed - select and copy" : label}
    </Button>
  );
}

export function Table({ children, caption }: { children: ReactNode; caption?: string }) {
  return (
    <div className="overflow-x-auto rounded-md border border-line">
      <table className="w-full text-left text-sm">
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        {children}
      </table>
    </div>
  );
}
export const TH =
  "border-b border-line bg-surface2 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted";
export const TD = "border-b border-line px-3 py-2 align-top last:border-b-0";

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "-" : d.toLocaleString();
}
