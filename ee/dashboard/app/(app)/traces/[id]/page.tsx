import type { Metadata } from "next";
import { TraceDetailView } from "@/components/traces";

export const metadata: Metadata = { title: "Trace" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <TraceDetailView id={id} />;
}
