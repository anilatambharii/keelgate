"use client";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import type { ApprovalRoute, ApprovalTier, Role } from "@/lib/types";
import { isAdmin, useMe } from "./me-context";
import { errorMessage, useToast } from "./toast";
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  PageHeader,
  SelectField,
  Spinner,
  Table,
  TD,
  TextField,
  TH,
} from "./ui";

const ROLES: Role[] = ["owner", "admin", "approver", "viewer"];

export function RoutesPage() {
  const me = useMe();
  const toast = useToast();
  const routes = useLoad(() => api.routes(), []);
  const [pattern, setPattern] = useState("*");
  const [priority, setPriority] = useState("100");
  const [minTier, setMinTier] = useState<ApprovalTier>("ONE_CLICK");
  const [escalate, setEscalate] = useState<"" | ApprovalTier>("");
  const [roles, setRoles] = useState<Role[]>(["owner", "admin", "approver"]);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<ApprovalRoute | null>(null);

  if (!isAdmin(me.user.role)) {
    return (
      <>
        <PageHeader title="Approval routing rules" />
        <Alert tone="warn">Only owners and admins can manage routing rules.</Alert>
      </>
    );
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    const p = Number(priority);
    if (!Number.isInteger(p) || p < 0 || p > 10000) {
      toast.error(new Error("Priority must be a whole number from 0 to 10000"));
      return;
    }
    if (roles.length === 0) {
      toast.error(new Error("Choose at least one role that may decide"));
      return;
    }
    setBusy(true);
    try {
      await api.createRoute({
        priority: p,
        tool_pattern: pattern.trim() || "*",
        min_tier: minTier,
        allowed_roles: roles,
        escalate_to: escalate || null,
        enabled: true,
      });
      toast.success("Rule added");
      await routes.reload();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.deleteRoute(deleting.id);
      toast.success("Rule deleted");
      setDeleting(null);
      await routes.reload();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  }

  const items = routes.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Approval routing rules"
        subtitle="The first enabled rule (lowest priority number) that matches decides who may approve. Rules can only make approval stricter."
        actions={
          <Link href="/approvals" className="text-sm text-accent underline">
            Back to approvals
          </Link>
        }
      />
      <div className="grid gap-4">
        <Card title="Add a rule">
          <form onSubmit={create} className="grid gap-3 md:grid-cols-2">
            <TextField
              label="Tool pattern"
              hint="A glob, for example transfer_* or *. Matches the tool name."
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
              required
              maxLength={128}
            />
            <TextField
              label="Priority"
              hint="Lower numbers are checked first."
              inputMode="numeric"
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            />
            <SelectField
              label="Applies from tier"
              value={minTier}
              onChange={(e) => setMinTier(e.target.value as ApprovalTier)}
            >
              <option value="ONE_CLICK">ONE_CLICK and above</option>
              <option value="EXPLICIT_SIGNOFF">EXPLICIT_SIGNOFF only</option>
            </SelectField>
            <SelectField
              label="Escalate to"
              hint="Raise matching requests to a stricter tier. Never lowers a tier."
              value={escalate}
              onChange={(e) => setEscalate(e.target.value as "" | ApprovalTier)}
            >
              <option value="">No change</option>
              <option value="ONE_CLICK">ONE_CLICK</option>
              <option value="EXPLICIT_SIGNOFF">EXPLICIT_SIGNOFF</option>
            </SelectField>
            <fieldset className="md:col-span-2">
              <legend className="mb-1 text-sm font-medium">Roles allowed to decide</legend>
              <div className="flex flex-wrap gap-4">
                {ROLES.map((r) => (
                  <label key={r} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={roles.includes(r)}
                      onChange={(e) =>
                        setRoles((cur) => (e.target.checked ? [...cur, r] : cur.filter((x) => x !== r)))
                      }
                    />
                    {r}
                  </label>
                ))}
              </div>
            </fieldset>
            <div className="md:col-span-2">
              <Button type="submit" variant="primary" disabled={busy}>
                Add rule
              </Button>
            </div>
          </form>
        </Card>

        <Card title="Rules">
          {routes.error ? (
            <Alert tone="danger">{errorMessage(routes.error)}</Alert>
          ) : routes.loading && !routes.data ? (
            <Spinner />
          ) : items.length === 0 ? (
            <EmptyState>
              No rules. By default owners, admins and approvers may decide any request.
            </EmptyState>
          ) : (
            <Table caption="Routing rules">
              <thead>
                <tr>
                  <th className={TH}>Priority</th>
                  <th className={TH}>Tool pattern</th>
                  <th className={TH}>From tier</th>
                  <th className={TH}>Allowed roles</th>
                  <th className={TH}>Escalate to</th>
                  <th className={TH}>Enabled</th>
                  <th className={TH}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.id}>
                    <td className={TD}>{r.priority}</td>
                    <td className={TD}>
                      <code>{r.tool_pattern}</code>
                    </td>
                    <td className={TD}>{r.min_tier}</td>
                    <td className={TD}>{r.allowed_roles.join(", ")}</td>
                    <td className={TD}>{r.escalate_to ?? "-"}</td>
                    <td className={TD}>
                      <Badge tone={r.enabled ? "ok" : "neutral"}>{r.enabled ? "yes" : "no"}</Badge>
                    </td>
                    <td className={TD}>
                      <Button variant="danger" onClick={() => setDeleting(r)}>
                        Delete<span className="sr-only"> rule for {r.tool_pattern}</span>
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </div>
      {deleting ? (
        <ConfirmDialog
          title="Delete this rule?"
          message={
            <>
              Requests for <code>{deleting.tool_pattern}</code> will fall back to the next matching rule
              or the default.
            </>
          }
          confirmLabel="Delete rule"
          busy={busy}
          onConfirm={remove}
          onCancel={() => setDeleting(null)}
        />
      ) : null}
    </>
  );
}
