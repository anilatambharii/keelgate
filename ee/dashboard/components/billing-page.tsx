"use client";
import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { daysLeft, fmtCents, fmtNumber } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import { isStripeUrl } from "@/lib/stripe-url";
import type { BillingInfo, InvoicePreview, PlanInfo } from "@/lib/types";
import { DailyChart, fillMonth } from "./daily-chart";
import { isAdmin, useMe } from "./me-context";
import { Meter } from "./meter";
import { errorMessage, useToast } from "./toast";
import { Alert, Badge, Button, Card, fmtDate, PageHeader, Spinner, Table, TD, TH } from "./ui";

/** Stripe may send the period end as unix seconds; the contract says ISO. Accept both. */
export function fmtPeriodEnd(v: string | number | null): string {
  if (typeof v === "number") return fmtDate(new Date(v * 1000).toISOString());
  return fmtDate(v);
}

const TEST_MODE = process.env.NEXT_PUBLIC_STRIPE_TEST_MODE !== "false";

export function planAction(
  plan: PlanInfo,
  billing: BillingInfo,
): { kind: "none" | "checkout" | "portal" | "contract"; label: string } {
  const hasSub = Boolean(billing.subscription);
  const isCurrent = plan.id === billing.plan;
  if (!plan.self_serve) {
    return plan.id === "community"
      ? { kind: "none", label: "" }
      : { kind: "contract", label: "Sold by contract" };
  }
  if (hasSub) {
    return isCurrent
      ? { kind: "none", label: "" }
      : { kind: "portal", label: "Change plan in billing portal" };
  }
  const trial = plan.trial_days > 0 && billing.billing_status === "trialing";
  const label = trial ? `Start ${plan.trial_days}-day trial` : isCurrent ? "Subscribe" : "Upgrade";
  return { kind: "checkout", label };
}

export function BillingPage({ checkout }: { checkout?: string }) {
  const me = useMe();
  const toast = useToast();
  const admin = isAdmin(me.user.role);
  const owner = me.user.role === "owner";
  const billing = useLoad(() => (admin ? api.billing() : Promise.resolve(null)), [admin]);
  const usage = useLoad(() => api.usage(), []);
  const invoice = useLoad(() => (admin ? api.invoicePreview() : Promise.resolve(null)), [admin]);
  const [pending, setPending] = useState<string | null>(null);

  const chart = useMemo(() => {
    const u = usage.data;
    if (!u) return [];
    return fillMonth(
      u.period,
      u.daily.filter((d) => d.metric === "gated_actions").map((d) => ({ day: d.day, value: d.quantity })),
    );
  }, [usage.data]);

  if (!admin) {
    return (
      <>
        <PageHeader title="Usage and billing" />
        <Alert tone="warn">Only owners and admins can see billing.</Alert>
      </>
    );
  }

  async function go(kind: "checkout" | "portal", plan?: string) {
    setPending(plan ?? kind);
    try {
      const res = kind === "checkout" && plan ? await api.checkout(plan) : await api.portal();
      if (!isStripeUrl(res.url)) {
        throw new Error("The billing provider returned an unexpected address. Nothing was changed.");
      }
      window.location.assign(res.url);
    } catch (err) {
      toast.error(err);
      setPending(null);
    }
  }

  const b = billing.data;
  const u = usage.data;
  const left = b && b.billing_status === "trialing" ? daysLeft(b.trial_ends_at) : null;

  return (
    <>
      <PageHeader title="Usage and billing" subtitle={u ? `Period ${u.period}` : undefined} />
      {checkout === "success" ? (
        <div className="mb-4">
          <Alert tone="ok" title="Checkout complete">
            Your subscription is being confirmed. This page updates when the payment provider has
            told us; refresh in a moment if the status below has not changed.
          </Alert>
        </div>
      ) : null}
      {checkout === "cancelled" ? (
        <div className="mb-4">
          <Alert tone="info">Checkout was cancelled. Nothing was charged.</Alert>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Current plan">
          {billing.error ? (
            <Alert tone="danger">{errorMessage(billing.error)}</Alert>
          ) : !b ? (
            <Spinner />
          ) : (
            <div className="space-y-2 text-sm">
              <p>
                <span className="text-lg font-semibold">{me.org.plan_name}</span>{" "}
                <Badge tone={b.billing_status === "active" || b.billing_status === "trialing" ? "ok" : "danger"}>
                  {b.billing_status}
                </Badge>
              </p>
              {left !== null ? (
                <p>
                  Trial ends {fmtDate(b.trial_ends_at)} ({left} day{left === 1 ? "" : "s"} left).
                </p>
              ) : null}
              {b.subscription ? (
                <p className="text-muted">
                  Subscription {b.subscription.status}
                  {b.subscription.current_period_end
                    ? `, renews ${fmtPeriodEnd(b.subscription.current_period_end)}`
                    : ""}
                  .
                </p>
              ) : (
                <p className="text-muted">No subscription yet.</p>
              )}
              <div className="pt-1">
                <Button
                  onClick={() => go("portal")}
                  disabled={!owner || !b.portal_available || pending !== null}
                  title={
                    !owner
                      ? "Only the owner can manage billing"
                      : !b.portal_available
                        ? "Available after your first checkout"
                        : undefined
                  }
                >
                  {pending === "portal" ? "Opening..." : "Manage billing"}
                </Button>
                {!owner ? (
                  <p className="mt-1 text-xs text-muted">Only the owner can change the plan or open the portal.</p>
                ) : !b.portal_available ? (
                  <p className="mt-1 text-xs text-muted">The portal becomes available after your first checkout.</p>
                ) : null}
              </div>
            </div>
          )}
        </Card>

        <Card title="Usage this period">
          {usage.error ? (
            <Alert tone="danger">{errorMessage(usage.error)}</Alert>
          ) : !u ? (
            <Spinner />
          ) : (
            <div className="space-y-4">
              <Meter label="Gated actions" value={u.usage.gated_actions ?? 0} cap={u.limits.gated_actions} />
              <Meter label="Agents" value={u.agents} cap={u.limits.max_agents} />
              <dl className="grid grid-cols-2 gap-2 text-sm">
                <div>
                  <dt className="text-muted">Traces stored</dt>
                  <dd className="text-lg font-semibold">{fmtNumber(u.usage.traces_stored ?? 0)}</dd>
                </div>
                <div>
                  <dt className="text-muted">Approvals</dt>
                  <dd className="text-lg font-semibold">{fmtNumber(u.usage.approvals ?? 0)}</dd>
                </div>
              </dl>
            </div>
          )}
        </Card>

        <Card title="Gated actions per day" className="lg:col-span-2">
          {u ? <DailyChart points={chart} label={`Gated actions per day, ${u.period}`} /> : <Spinner />}
        </Card>

        <div className="lg:col-span-2">
          <h2 className="mb-3 text-base font-semibold">Plans</h2>
          {!b ? (
            <Spinner />
          ) : (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              {b.plans.map((p) => {
                const action = planAction(p, b);
                const current = p.id === b.plan;
                return (
                  <section
                    key={p.id}
                    aria-label={`${p.name} plan`}
                    className={`flex flex-col rounded-lg border p-4 ${current ? "border-accent" : "border-line"} bg-surface`}
                  >
                    <h3 className="flex items-center gap-2 font-semibold">
                      {p.name} {current ? <Badge tone="info">Current</Badge> : null}
                    </h3>
                    <p className="mt-1 text-2xl font-semibold">
                      {p.price_monthly_cents === null ? "Custom" : fmtCents(p.price_monthly_cents)}
                      {p.price_monthly_cents !== null ? (
                        <span className="text-sm font-normal text-muted"> / month</span>
                      ) : null}
                    </p>
                    <p className="mt-1 text-sm text-muted">{p.description}</p>
                    <ul className="mt-3 flex-1 space-y-1 text-sm">
                      <li>
                        {p.included_gated_actions === null
                          ? "No monthly cap on gated actions"
                          : `${fmtNumber(p.included_gated_actions)} gated actions / month`}
                      </li>
                      {p.trial_days > 0 ? <li>{p.trial_days}-day trial</li> : null}
                      {p.features.map((f) => (
                        <li key={f}>{f.replace(/_/g, " ")}</li>
                      ))}
                    </ul>
                    <div className="mt-4">
                      {action.kind === "checkout" ? (
                        <Button
                          variant="primary"
                          className="w-full"
                          disabled={!owner || pending !== null}
                          onClick={() => go("checkout", p.id)}
                        >
                          {pending === p.id ? "Redirecting..." : action.label}
                          <span className="sr-only"> on the {p.name} plan</span>
                        </Button>
                      ) : action.kind === "portal" ? (
                        <Button className="w-full" disabled={!owner || pending !== null} onClick={() => go("portal")}>
                          {action.label}
                        </Button>
                      ) : action.kind === "contract" ? (
                        <p className="text-sm text-muted">{action.label}. Contact sales.</p>
                      ) : null}
                      {!owner && (action.kind === "checkout" || action.kind === "portal") ? (
                        <p className="mt-1 text-xs text-muted">Ask the owner to make this change.</p>
                      ) : null}
                    </div>
                  </section>
                );
              })}
            </div>
          )}
        </div>

        <Card
          title="Invoice preview"
          className="lg:col-span-2"
          actions={
            <span className="flex gap-2">
              {invoice.data ? (
                <Badge tone={invoice.data.source === "stripe" ? "info" : "neutral"}>
                  {invoice.data.source === "stripe" ? "Stripe preview" : "Local estimate"}
                </Badge>
              ) : null}
              {TEST_MODE ? <Badge tone="warn">Test mode</Badge> : null}
            </span>
          }
        >
          {invoice.error ? (
            <Alert tone="danger">{errorMessage(invoice.error)}</Alert>
          ) : !invoice.data ? (
            <Spinner />
          ) : (
            <InvoiceTable preview={invoice.data} />
          )}
        </Card>
      </div>
    </>
  );
}

function InvoiceTable({ preview }: { preview: InvoicePreview }) {
  const stripe = preview.stripe_preview;
  const cur = preview.currency;
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        Source: <strong>{preview.source === "stripe" ? "Stripe preview" : "local estimate"}</strong>.{" "}
        {preview.source === "local"
          ? "Computed from your plan and this period's usage. This is the figure of record."
          : "Taken from the payment provider."}{" "}
        Period {preview.period.start.slice(0, 10)} to {preview.period.end.slice(0, 10)}.
      </p>
      <Table caption="Invoice preview lines">
        <thead>
          <tr>
            <th className={TH}>Description</th>
            <th className={`${TH} text-right`}>Quantity</th>
            <th className={`${TH} text-right`}>Unit</th>
            <th className={`${TH} text-right`}>Amount</th>
          </tr>
        </thead>
        <tbody>
          {preview.lines.map((l, i) => (
            <tr key={i}>
              <td className={TD}>{l.description}</td>
              <td className={`${TD} text-right`}>{fmtNumber(l.quantity)}</td>
              <td className={`${TD} text-right`}>{fmtCents(l.unit_amount_cents, cur)}</td>
              <td className={`${TD} text-right`}>{fmtCents(l.amount_cents, cur)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={3} className={`${TD} text-right`}>
              Total
            </th>
            <td className={`${TD} text-right font-semibold`}>{fmtCents(preview.total_cents, cur)}</td>
          </tr>
        </tfoot>
      </Table>
      {preview.notes?.map((n, i) => (
        <p key={i} className="text-xs text-muted">
          {n}
        </p>
      ))}
      {stripe ? (
        stripe.available ? (
          <p className="text-sm">
            Stripe&apos;s own preview for the next invoice: <strong>{fmtCents(stripe.total_cents, stripe.currency ?? cur)}</strong>
            . It can differ from the estimate (proration, tax, trial).
          </p>
        ) : (
          <p className="text-xs text-muted">Stripe&apos;s preview is not available right now.</p>
        )
      ) : null}
    </div>
  );
}
