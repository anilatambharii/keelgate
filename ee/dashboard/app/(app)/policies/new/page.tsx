import type { Metadata } from "next";
import { PolicyEditor } from "@/components/policy-editor";

export const metadata: Metadata = { title: "New policy pack" };

export default function Page() {
  return <PolicyEditor />;
}
