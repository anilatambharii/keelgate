import type { Metadata } from "next";
import { LoginForm } from "@/components/auth-forms";
import { controlPlanePublicUrl } from "@/lib/config";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; expired?: string }>;
}) {
  const sp = await searchParams;
  return (
    <LoginForm next={sp.next} expired={sp.expired === "1"} publicUrl={controlPlanePublicUrl()} />
  );
}
