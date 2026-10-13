import type { Metadata } from "next";
import Link from "next/link";
import { ssoErrorMessage } from "@/components/auth-forms";
import { Alert, Card } from "@/components/ui";

export const metadata: Metadata = { title: "Sign-in problem" };

export default async function SsoErrorPage({
  searchParams,
}: {
  searchParams: Promise<{ code?: string }>;
}) {
  const { code } = await searchParams;
  return (
    <Card title="Single sign-on failed">
      <Alert tone="danger">{ssoErrorMessage(code)}</Alert>
      {code ? <p className="mt-2 text-xs text-muted">Reference: {code.slice(0, 64)}</p> : null}
      <p className="mt-4 text-sm">
        <Link href="/login" className="text-accent underline">
          Back to sign in
        </Link>
      </p>
    </Card>
  );
}
