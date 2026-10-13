import type { Metadata } from "next";
import { SsoComplete } from "@/components/auth-forms";

export const metadata: Metadata = { title: "Signing in", referrer: "no-referrer" };

export default function SsoCompletePage() {
  return <SsoComplete />;
}
