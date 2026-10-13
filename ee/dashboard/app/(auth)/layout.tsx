import type { ReactNode } from "react";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main id="main" className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-4 py-10">
      <p className="mb-6 text-center text-lg font-semibold tracking-tight">Keelgate Cloud</p>
      {children}
    </main>
  );
}
