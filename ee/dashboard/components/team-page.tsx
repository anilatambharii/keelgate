"use client";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import { safeText } from "@/lib/sanitize";
import type { ApprovalTier, Role, TeamUser } from "@/lib/types";
import { MIN_PASSWORD_LENGTH, validEmail } from "@/lib/validation";
import { isAdmin, useMe } from "./me-context";
import { errorMessage, useToast } from "./toast";
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  fmtDate,
  PageHeader,
  SelectField,
  Spinner,
  Table,
  TD,
  TextField,
  TH,
} from "./ui";

export function TeamPage() {
  const me = useMe();
  const toast = useToast();
  const owner = me.user.role === "owner";
  const users = useLoad(() => api.listUsers(), []);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("approver");
  const [password, setPassword] = useState("");
  const [tier, setTier] = useState<ApprovalTier>("ONE_CLICK");
  const [busy, setBusy] = useState(false);
  const [disabling, setDisabling] = useState<TeamUser | null>(null);

  if (!isAdmin(me.user.role)) {
    return (
      <>
        <PageHeader title="Team" />
        <Alert tone="warn">Only owners and admins can manage the team.</Alert>
      </>
    );
  }

  const pwError = password && password.length < MIN_PASSWORD_LENGTH ? `At least ${MIN_PASSWORD_LENGTH} characters, or leave empty.` : null;

  async function invite(e: FormEvent) {
    e.preventDefault();
    if (!validEmail(email) || pwError) return;
    setBusy(true);
    try {
      await api.inviteUser({
        email: email.trim(),
        role,
        approval_tier: tier,
        ...(password ? { password } : {}),
      });
      toast.success(`Added ${email.trim()}`);
      setEmail("");
      setPassword("");
      await users.reload();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    if (!disabling) return;
    setBusy(true);
    try {
      await api.disableUser(disabling.id);
      toast.success(`Disabled ${disabling.email}`);
      setDisabling(null);
      await users.reload();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  }

  async function changeTier(u: TeamUser, next: ApprovalTier) {
    try {
      await api.setUserTier(u.id, next);
      toast.success(`${u.email} is now cleared for ${next}`);
      await users.reload();
    } catch (err) {
      toast.error(err);
    }
  }

  const items = users.data?.items ?? [];

  return (
    <>
      <PageHeader title="Team" subtitle="People who can sign in to this organisation." />
      <div className="grid gap-4">
        <Card title="Add a person">
          <form onSubmit={invite} className="grid gap-3 md:grid-cols-2" noValidate>
            <TextField
              label="Email"
              type="email"
              required
              autoComplete="off"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <SelectField label="Role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="viewer">viewer - read only</option>
              <option value="approver">approver - can decide approvals</option>
              {owner ? <option value="admin">admin - manage keys, team, policies</option> : null}
            </SelectField>
            <TextField
              label="Initial password (optional)"
              type="password"
              autoComplete="new-password"
              hint="If set, share it over a secure channel and ask them to change it. Leave empty if they will sign in with SSO."
              error={pwError}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <SelectField
              label="Approval clearance"
              hint={
                owner
                  ? "EXPLICIT_SIGNOFF lets them decide the highest-risk requests."
                  : "Only the owner can grant EXPLICIT_SIGNOFF clearance."
              }
              disabled={!owner}
              value={tier}
              onChange={(e) => setTier(e.target.value as ApprovalTier)}
            >
              <option value="ONE_CLICK">ONE_CLICK</option>
              <option value="EXPLICIT_SIGNOFF">EXPLICIT_SIGNOFF</option>
            </SelectField>
            <div className="md:col-span-2">
              <Button type="submit" variant="primary" disabled={busy || !validEmail(email) || Boolean(pwError)}>
                Add person
              </Button>
            </div>
          </form>
        </Card>

        <Card title="People">
          {users.error ? (
            <Alert tone="danger">{errorMessage(users.error)}</Alert>
          ) : users.loading && !users.data ? (
            <Spinner />
          ) : items.length === 0 ? (
            <EmptyState>No people yet.</EmptyState>
          ) : (
            <Table caption="Team members">
              <thead>
                <tr>
                  <th className={TH}>Email</th>
                  <th className={TH}>Role</th>
                  <th className={TH}>Approval clearance</th>
                  <th className={TH}>Added</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((u) => {
                  const isSelf = u.id === me.user.id;
                  return (
                    <tr key={u.id}>
                      <td className={TD}>
                        {safeText(u.email)} {isSelf ? <Badge>you</Badge> : null}
                      </td>
                      <td className={TD}>
                        <Badge tone="info">{u.role}</Badge>
                      </td>
                      <td className={TD}>
                        {owner && !u.disabled_at ? (
                          <select
                            aria-label={`Approval clearance for ${u.email}`}
                            className="rounded-md border border-line bg-surface px-2 py-1 text-sm"
                            value={u.approval_tier}
                            onChange={(e) => changeTier(u, e.target.value as ApprovalTier)}
                          >
                            <option value="ONE_CLICK">ONE_CLICK</option>
                            <option value="EXPLICIT_SIGNOFF">EXPLICIT_SIGNOFF</option>
                          </select>
                        ) : (
                          u.approval_tier
                        )}
                      </td>
                      <td className={TD}>{fmtDate(u.created_at)}</td>
                      <td className={TD}>
                        <Badge tone={u.disabled_at ? "danger" : "ok"}>{u.disabled_at ? "disabled" : "active"}</Badge>
                      </td>
                      <td className={TD}>
                        {!u.disabled_at && !isSelf && u.role !== "owner" ? (
                          <Button variant="danger" onClick={() => setDisabling(u)}>
                            Disable<span className="sr-only"> {u.email}</span>
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          )}
          {!owner ? (
            <p className="mt-2 text-xs text-muted">Only the owner can change approval clearance.</p>
          ) : null}
        </Card>
      </div>
      {disabling ? (
        <ConfirmDialog
          title="Disable this person?"
          message={
            <>
              <strong>{safeText(disabling.email)}</strong> will be signed out and cannot sign in again.
            </>
          }
          confirmLabel="Disable"
          busy={busy}
          onConfirm={disable}
          onCancel={() => setDisabling(null)}
        />
      ) : null}
    </>
  );
}
