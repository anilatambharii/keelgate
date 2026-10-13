import type { Metadata } from "next";
import { KeysPage } from "@/components/keys-page";

export const metadata: Metadata = { title: "API keys" };

export default function Page() {
  return <KeysPage />;
}
