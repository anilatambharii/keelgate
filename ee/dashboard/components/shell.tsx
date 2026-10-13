"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import type { Me } from "@/lib/types";
import { isAdmin, MeProvider } from "./me-context";
import { Badge, Button, cx } from "./ui";

interface NavItem {
  href: string;
  label: string;
  adminOnly?: boolean;
}

export const NAV: NavItem[] = [
  { href: "/", label: "Overview" },
  { href: "/approvals", label: "Approvals" },
  { href: "/traces", label: "Traces" },
  { href: "/policies", label: "Policies" },
  { href: "/audit", label: "Audit" },
  { href: "/keys", label: "API keys", adminOnly: true },
  { href: "/billing", label: "Usage and billing", adminOnly: true },
  { href: "/team", label: "Team", adminOnly: true },
  { href: "/settings", label: "Settings" },
];

export function navFor(role: string): NavItem[] {
  return NAV.filter((n) => !n.adminOnly || isAdmin(role));
}

function active(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(href + "/");
}

export function Shell({ me, children }: { me: Me; children: ReactNode }) {
  const pathname = usePathname();
  const [busy, setBusy] = useState(false);

  async function signOut() {
    setBusy(true);
    try {
      await api.logout();
    } finally {
      // A full page load, so no client state from the old session survives.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign("/login");
    }
  }

  return (
    <MeProvider me={me}>
      <div className="min-h-screen md:flex">
        <aside className="border-b border-line bg-surface md:min-h-screen md:w-56 md:shrink-0 md:border-b-0 md:border-r">
          <div className="flex items-center justify-between px-4 py-3 md:block">
            <p className="font-semibold tracking-tight">Keelgate Cloud</p>
            <p className="max-w-[12rem] truncate text-xs text-muted md:mt-1" title={me.org.name}>
              {me.org.name}
            </p>
          </div>
          <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-2 pb-2 md:block md:space-y-0.5 md:overflow-visible">
            {navFor(me.user.role).map((n) => (
              <Link
                key={n.href}
                href={n.href}
                aria-current={active(pathname, n.href) ? "page" : undefined}
                className={cx(
                  "block whitespace-nowrap rounded-md px-3 py-1.5 text-sm",
                  active(pathname, n.href)
                    ? "bg-infobg font-medium text-accent"
                    : "text-fg hover:bg-surface2",
                )}
              >
                {n.label}
              </Link>
            ))}
          </nav>
        </aside>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-end gap-3 border-b border-line bg-surface px-4 py-2 text-sm">
            <span className="text-muted">{me.user.email}</span>
            <Badge tone="info">{me.user.role}</Badge>
            <Badge>{me.org.plan_name}</Badge>
            <Button onClick={signOut} disabled={busy}>
              Sign out
            </Button>
          </div>
          <main id="main" className="mx-auto max-w-6xl px-4 py-6">
            {children}
          </main>
        </div>
      </div>
    </MeProvider>
  );
}
