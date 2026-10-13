import type { ReactNode } from "react";
import { Shell } from "@/components/shell";
import { requireMe } from "@/lib/server";

export default async function AppLayout({ children }: { children: ReactNode }) {
  const me = await requireMe();
  return <Shell me={me}>{children}</Shell>;
}
