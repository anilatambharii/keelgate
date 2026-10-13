"use client";
import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { countdown } from "@/lib/format";
import { useLoad, useNow, usePoll } from "@/lib/hooks";
import { safeText } from "@/lib/sanitize";
import type { ApprovalRequest, ApprovalStatus } from "@/lib/types";
import { DecisionPanel, EvidenceView, StatusBadge } from "./approval-detail";
import { isAdmin, useMe } from "./me-context";
import { errorMessage } from "./toast";
import { Alert, Badge, Button, Card, cx, EmptyState, fmtDate, PageHeader, Spinner } from "./ui";

type Tab = "pending" | "history";

export function ApprovalsPage() {
  const me = useMe();
  const [tab, setTab] = useState<Tab>("pending");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [optimistic, setOptimistic] = useState<Record<string, ApprovalStatus>>({});
  const now = useNow();

  const pending = useLoad(() => api.approvals("PENDING"), []);
  const history = useLoad(() => api.approvals("ALL"), [tab]);

  // Poll the inbox every 5 seconds; the hook pauses while the tab is hidden.
  usePoll(() => void pending.reload(), 5000, tab === "pending");

  const pendingItems = useMemo(() => {
    const items = pending.data?.items ?? [];
    return items
      .filter((r) => !optimistic[r.request_id])
      .sort((a, b) => new Date(a.expires_at).getTime() - new Date(b.expires_at).getTime());
  }, [pending.data, optimistic]);

  const historyItems = useMemo(
    () => (history.data?.items ?? []).filter((r) => r.status !== "PENDING"),
    [history.data],
  );

  const list = tab === "pending" ? pendingItems : historyItems;
  const selected: ApprovalRequest | null =
    list.find((r) => r.request_id === selectedId) ??
    (tab === "pending" && list.length > 0 && selectedId === null ? list[0] : null);

  const onOptimistic = useCallback((id: string, status: ApprovalStatus) => {
    setOptimistic((cur) => ({ ...cur, [id]: status }));
  }, []);
  const onFailed = useCallback((id: string) => {
    setOptimistic((cur) => {
      const next = { ...cur };
      delete next[id];
      return next;
    });
  }, []);
  const onDecided = useCallback(
    async (updated: ApprovalRequest) => {
      setSelectedId(null);
      await pending.reload();
      setOptimistic((cur) => {
        const next = { ...cur };
        delete next[updated.request_id];
        return next;
      });
      if (tab === "history") await history.reload();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tab],
  );

  const active = tab === "pending" ? pending : history;

  return (
    <>
      <PageHeader
        title="Approvals"
        subtitle="Actions your agents want to take that a policy says need a person."
        actions={
          isAdmin(me.user.role) ? (
            <Link href="/approvals/routes" className="text-sm text-accent underline">
              Routing rules
            </Link>
          ) : null
        }
      />
      <div role="tablist" aria-label="Approvals view" className="mb-4 flex gap-1 border-b border-line">
        {(["pending", "history"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            type="button"
            aria-selected={tab === t}
            id={`tab-${t}`}
            aria-controls="approvals-panel"
            onClick={() => {
              setTab(t);
              setSelectedId(null);
            }}
            className={cx(
              "-mb-px border-b-2 px-4 py-2 text-sm font-medium",
              tab === t ? "border-accent text-accent" : "border-transparent text-muted hover:text-fg",
            )}
          >
            {t === "pending" ? `Pending (${pendingItems.length})` : "History"}
          </button>
        ))}
      </div>

      <div id="approvals-panel" role="tabpanel" aria-labelledby={`tab-${tab}`} className="grid gap-4 lg:grid-cols-[22rem_1fr]">
        <div>
          {tab === "pending" ? (
            <p className="mb-2 text-xs text-muted" aria-live="off">
              Refreshes every 5 seconds while this tab is visible.
            </p>
          ) : (
            <div className="mb-2 flex justify-end">
              <Button onClick={() => void history.reload()}>Refresh</Button>
            </div>
          )}
          {active.error ? (
            <Alert tone="danger">{errorMessage(active.error)}</Alert>
          ) : active.loading && !active.data ? (
            <Spinner />
          ) : list.length === 0 ? (
            <EmptyState>
              {tab === "pending" ? "Nothing is waiting for you." : "No decided requests yet."}
            </EmptyState>
          ) : (
            <ul aria-label={tab === "pending" ? "Pending approvals" : "Approval history"} className="space-y-2">
              {list.map((r) => {
                const isSel = selected?.request_id === r.request_id;
                return (
                  <li key={r.request_id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(r.request_id)}
                      aria-current={isSel ? "true" : undefined}
                      className={cx(
                        "w-full rounded-md border p-3 text-left hover:bg-surface2",
                        isSel ? "border-accent bg-infobg" : "border-line bg-surface",
                      )}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-sm">{safeText(r.tool_name)}</span>
                        {tab === "pending" ? (
                          <Badge tone={r.tier === "EXPLICIT_SIGNOFF" ? "danger" : "info"}>
                            {r.tier === "EXPLICIT_SIGNOFF" ? "sign-off" : "one-click"}
                          </Badge>
                        ) : (
                          <StatusBadge status={r.status} />
                        )}
                      </span>
                      <span className="mt-1 block truncate text-xs text-muted">
                        agent {safeText(r.agent_id)}
                      </span>
                      <span className="mt-0.5 block text-xs text-muted">
                        {tab === "pending"
                          ? `expires in ${countdown(r.expires_at, now)}`
                          : `${fmtDate(r.decided_at ?? r.created_at)}`}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div>
          {selected ? (
            <Card title={<span className="font-mono">{safeText(selected.tool_name)}</span>}>
              <div className="space-y-4">
                <EvidenceView request={selected} />
                {selected.status === "PENDING" ? (
                  <DecisionPanel
                    key={selected.request_id}
                    request={selected}
                    user={me.user}
                    onOptimistic={onOptimistic}
                    onDecided={onDecided}
                    onFailed={onFailed}
                  />
                ) : null}
              </div>
            </Card>
          ) : (
            <EmptyState>Select a request to review its evidence.</EmptyState>
          )}
        </div>
      </div>
    </>
  );
}
