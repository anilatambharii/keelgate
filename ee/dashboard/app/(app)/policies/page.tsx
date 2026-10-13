import type { Metadata } from "next";
import { PoliciesList } from "@/components/policies-list";

export const metadata: Metadata = { title: "Policies" };

export default function Page() {
  return <PoliciesList />;
}
