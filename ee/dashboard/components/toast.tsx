"use client";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError } from "@/lib/api";

type ToastTone = "ok" | "danger" | "info";
interface ToastItem {
  id: number;
  tone: ToastTone;
  text: string;
}
interface ToastApi {
  success: (text: string) => void;
  info: (text: string) => void;
  error: (err: unknown, fallback?: string) => void;
}

const Ctx = createContext<ToastApi | null>(null);

export function errorMessage(err: unknown, fallback = "Something went wrong"): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

const TONE_CLASS = {
  ok: "bg-okbg text-ok",
  danger: "bg-dangerbg text-danger",
  info: "bg-infobg text-accent",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const push = useCallback((tone: ToastTone, text: string) => {
    const id = nextId.current++;
    setItems((cur) => [...cur.slice(-3), { id, tone, text }]);
    setTimeout(
      () => setItems((cur) => cur.filter((t) => t.id !== id)),
      tone === "danger" ? 9000 : 5000,
    );
  }, []);
  const api = useMemo<ToastApi>(
    () => ({
      success: (t) => push("ok", t),
      info: (t) => push("info", t),
      error: (e, fb) => push("danger", errorMessage(e, fb)),
    }),
    [push],
  );
  return (
    <Ctx.Provider value={api}>
      {children}
      {/* Always mounted so assistive technology announces additions. */}
      <div
        aria-live="polite"
        aria-atomic="false"
        role="status"
        data-testid="toasts"
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
      >
        {items.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto rounded-md border border-line px-3 py-2 text-sm shadow ${TONE_CLASS[t.tone]}`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useToast must be used inside <ToastProvider>");
  return v;
}
