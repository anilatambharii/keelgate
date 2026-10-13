import type { Metadata } from "next";
import { TraceList } from "@/components/traces";

export const metadata: Metadata = { title: "Traces" };

export default function Page() {
  return <TraceList />;
}
