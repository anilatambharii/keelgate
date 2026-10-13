import type { Metadata } from "next";
import { Overview } from "@/components/overview";
import { controlPlanePublicUrl } from "@/lib/config";

export const metadata: Metadata = { title: "Overview" };

export default function OverviewPage() {
  return <Overview publicUrl={controlPlanePublicUrl()} />;
}
