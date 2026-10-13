"use client";
import Link from "next/link";
import { api } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import { safeText } from "@/lib/sanitize";
import { isAdmin, useMe } from "./me-context";
import { errorMessage } from "./toast";
import { Alert, Badge, EmptyState, fmtDate, LinkButton, PageHeader, Spinner, Table, TD, TH } from "./ui";

export function PoliciesList() {
  const me = useMe();
  const list = useLoad(() => api.policies(), []);
  const items = list.data?.items ?? [];
  return (
    <>
      <PageHeader
        title="Policies"
        subtitle="Policy packs are evaluated inside your agent process. Deploy a tested version to make it active."
        actions={isAdmin(me.user.role) ? <LinkButton href="/policies/new" variant="primary">New policy pack</LinkButton> : null}
      />
      {list.error ? (
        <Alert tone="danger">{errorMessage(list.error)}</Alert>
      ) : list.loading && !list.data ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState>
          No policy packs yet.
          {isAdmin(me.user.role) ? " Create one, or start from a template." : ""}
        </EmptyState>
      ) : (
        <Table caption="Policy packs">
          <thead>
            <tr>
              <th className={TH}>Name</th>
              <th className={TH}>Active</th>
              <th className={TH}>Latest</th>
              <th className={TH}>Updated</th>
            </tr>
          </thead>
          <tbody>
            {items.map((p) => (
              <tr key={p.name}>
                <td className={TD}>
                  <Link href={`/policies/${encodeURIComponent(p.name)}`} className="font-medium text-accent underline">
                    {safeText(p.name)}
                  </Link>
                </td>
                <td className={TD}>
                  {p.active_version ? <Badge tone="ok">v{p.active_version}</Badge> : <Badge>not deployed</Badge>}
                </td>
                <td className={TD}>{p.latest_version ? `v${p.latest_version}` : "-"}</td>
                <td className={TD}>{fmtDate(p.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </>
  );
}
