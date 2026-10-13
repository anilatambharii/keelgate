import type { Metadata } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";
import { ToastProvider } from "@/components/toast";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Keelgate Cloud", template: "%s - Keelgate Cloud" },
  description: "Approvals, audit and policy for governed AI agents.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Every page is rendered per request so that it carries the per-request CSP nonce.
  await connection();
  return (
    <html lang="en">
      <body className="min-h-screen">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2"
        >
          Skip to content
        </a>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
