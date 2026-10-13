import type { Metadata } from "next";
import { BillingPage } from "@/components/billing-page";

export const metadata: Metadata = { title: "Usage and billing" };

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ checkout?: string }>;
}) {
  const sp = await searchParams;
  return <BillingPage checkout={sp.checkout} />;
}
