"use client";
import Link from "next/link";
import { useMemo } from "react";
import { api } from "@/lib/api";
import { agentSnippet, computeChecklist } from "@/lib/checklist";
import { daysLeft } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import { isAdmin, useMe } from "./me-context";
import { Meter } from "./meter";
import { errorMessage } from "./toast";
import { Alert, Badge, Card, CopyButton, PageHeader } from "./ui";

export function Overview({ publicUrl }: { publicUrl: string }) {
  const me = useMe();
  const admin = isAdmin(me.user.role);
  const usage = useLoad(() => api.usage(), []);
  const keys = useLoad(() => (admin ? api.listKeys() : Promise.resolve(null)), [admin]);
  const traces = useLoad(() => api.traces({ limit: 1 }), []);
  const approvals = useLoad(() => api.approvals("ALL"), []);
  const billing = useLoad(() => (admin ? api.billing() : Promise.resolve(null)), [admin]);

  const items = useMemo(
    () =>
      computeChecklist({
        keyCount: admin ? (keys.data ? keys.data.items.filter((k) => !k.revoked_at).length : 0) : null,
        usage: usage.data,
        traceCount: traces.data ? traces.data.items.length : 0,
        approvals: approvals.data ? approvals.data.items : [],
        billingStatus: me.org.billing_status,
        hasSubscription: admin ? Boolean(billing.data?.subscription) : null,
      }),
    [admin, keys.data, usage.data, traces.data, approvals.data, billing.data, me.org.billing_status],
  );
  const done = items.filter((i) => i.done).length;
  const left = me.org.billing_status === "trialing" ? daysLeft(me.org.trial_ends_at) : null;
  const snippet = agentSnippet(publicUrl, me.org.id);
  const u = usage.data;

  return (
    <>
      <PageHeader title="Overview" subtitle={`${me.org.name} - ${done} of ${items.length} steps done`} />

      {left !== null ? (
        <div className="mb-4">
          <Alert tone={left <= 3 ? "warn" : "info"} title={`Trial: ${left} day${left === 1 ? "" : "s"} left`}>
            {admin ? (
              <>
                Choose a plan on the{" "}
                <Link href="/billing" className="underline">
                  billing page
                </Link>{" "}
                to keep your agents running after the trial.
              </>
            ) : (
              "Ask an owner to choose a plan before the trial ends."
            )}
          </Alert>
        </div>
      ) : null}
      {me.org.billing_status !== "trialing" && me.org.billing_status !== "active" ? (
        <div className="mb-4">
          <Alert tone="danger" title={`Billing status: ${me.org.billing_status}`}>
            Agents may be refused until billing is resolved.
          </Alert>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Get started">
          <ol className="space-y-3" aria-label="Setup checklist">
            {items.map((i) => (
              <li key={i.id} className="flex gap-3">
                <span
                  aria-hidden="true"
                  className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-xs ${
                    i.done ? "border-ok bg-okbg text-ok" : "border-line text-muted"
                  }`}
                >
                  {i.done ? "✓" : ""}
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-medium">
                    {i.href ? (
                      <Link href={i.href} className="hover:underline">
                        {i.title}
                      </Link>
                    ) : (
                      i.title
                    )}{" "}
                    <span className="sr-only">{i.done ? "(done)" : "(not done yet)"}</span>
                    {i.unknown && !i.done ? <Badge>ask an admin</Badge> : null}
                  </p>
                  <p className="text-xs text-muted">{i.hint}</p>
                </div>
              </li>
            ))}
          </ol>
        </Card>

        <Card title="Plan and usage" actions={<Badge tone="info">{me.org.plan_name}</Badge>}>
          {usage.error ? (
            <Alert tone="danger">{errorMessage(usage.error)}</Alert>
          ) : !u ? (
            <p className="text-sm text-muted" role="status">
              Loading usage...
            </p>
          ) : (
            <div className="space-y-4">
              <Meter
                label="Gated actions"
                value={u.usage.gated_actions ?? 0}
                cap={u.limits.gated_actions}
              />
              <Meter label="Agents" value={u.agents} cap={u.limits.max_agents} />
              <p className="text-xs text-muted">Period {u.period}.</p>
              {admin ? (
                <Link href="/billing" className="text-sm text-accent underline">
                  Usage and billing
                </Link>
              ) : null}
            </div>
          )}
        </Card>

        <Card
          title="Connect a governed agent"
          className="lg:col-span-2"
          actions={<CopyButton text={snippet} label="Copy snippet" />}
        >
          <p className="mb-2 text-sm text-muted">
            Create an API key on the{" "}
            {admin ? (
              <Link href="/keys" className="text-accent underline">
                API keys page
              </Link>
            ) : (
              "API keys page (ask an admin)"
            )}
            , then set it as <code>KEELGATE_API_KEY</code>. The full key is shown only once, when it is
            created; this snippet never contains it.
          </p>
          <pre
            tabIndex={0}
            aria-label="Agent setup snippet"
            className="overflow-auto rounded-md border border-line bg-surface2 p-3 text-xs"
          >
            {snippet}
          </pre>
        </Card>
      </div>
    </>
  );
}
