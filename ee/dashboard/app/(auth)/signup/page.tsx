import type { Metadata } from "next";
import { SignupForm } from "@/components/auth-forms";

export const metadata: Metadata = { title: "Create your organisation" };

export default function SignupPage() {
  return <SignupForm />;
}
