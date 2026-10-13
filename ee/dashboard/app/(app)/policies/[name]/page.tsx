import type { Metadata } from "next";
import { PolicyEditor } from "@/components/policy-editor";

export const metadata: Metadata = { title: "Policy" };

export default async function Page({ params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return <PolicyEditor name={decodeURIComponent(name)} />;
}
