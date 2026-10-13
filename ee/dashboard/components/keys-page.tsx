"use client";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import type { ApiKey, CreatedApiKey } from "@/lib/types";
import { isAdmin, useMe } from "./me-context";
import { errorMessage, useToast } from "./toast";
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfirmDialog,
  CopyButton,
  EmptyState,
  fmtDate,
  Modal,
  PageHeader,
  Spinner,
  Table,
  TD,
  TextField,
  TH,
} from "./ui";

/** Shows a freshly created key. This is the only time the full secret exists in the browser. */
export function KeyShownOnceDialog({
  created,
  onClose,
}: {
  created: CreatedApiKey;
  onClose: () => void;
}) {
  const [saved, setSaved] = useState(false);
  return (
    <Modal title="Copy your new API key" onClose={onClose} dismissable={false}>
      <Alert tone="warn" title="This is the only time you will see this key.">
        Keelgate stores only a hash. Copy it now and keep it in a secrets manager. If you lose it,
        revoke it and create a new one.
      </Alert>
      <p className="mb-1 mt-3 text-sm font-medium">{created.name}</p>
      <pre
        data-testid="api-key-secret"
        aria-label="New API key"
        tabIndex={0}
        className="overflow-x-auto rounded-md border border-line bg-surface2 p-3 text-sm"
      >
        {created.key}
      </pre>
      <div className="mt-2">
        <CopyButton text={created.key} label="Copy key" />
      </div>
      <label className="mt-4 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} />I have
        stored this key somewhere safe
      </label>
      <div className="mt-4 flex justify-end">
        <Button variant="primary" onClick={onClose} disabled={!saved}>
          Done
        </Button>
      </div>
    </Modal>
  );
}

function keyStatus(k: ApiKey): { label: string; tone: "ok" | "danger" | "warn" } {
  if (k.revoked_at) return { label: "Revoked", tone: "danger" };
  if (k.expires_at && new Date(k.expires_at).getTime() < Date.now()) {
    return { label: "Expired", tone: "warn" };
  }
  return { label: "Active", tone: "ok" };
}

export function KeysPage() {
  const me = useMe();
  const toast = useToast();
  const keys = useLoad(() => api.listKeys(), []);
  const [name, setName] = useState("");
  const [days, setDays] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);

  if (!isAdmin(me.user.role)) {
    return (
      <>
        <PageHeader title="API keys" />
        <Alert tone="warn">Only owners and admins can manage API keys.</Alert>
      </>
    );
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    const n = Number(days);
    if (days && (!Number.isInteger(n) || n < 1 || n > 3650)) {
      toast.error(new Error("Expiry must be a whole number of days between 1 and 3650"));
      return;
    }
    setBusy(true);
    try {
      const res = await api.createKey(name.trim(), days ? n : null);
      setCreated(res);
      setName("");
      setDays("");
      await keys.reload();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (!revoking) return;
    setRevokeBusy(true);
    try {
      await api.revokeKey(revoking.id);
      toast.success(`Revoked ${revoking.name}`);
      setRevoking(null);
      await keys.reload();
    } catch (err) {
      toast.error(err);
    } finally {
      setRevokeBusy(false);
    }
  }

  const items = keys.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="API keys"
        subtitle="Agents use these keys to reach the control plane. A key names your organisation."
      />
      <div className="grid gap-4">
        <Card title="Create a key">
          <form onSubmit={create} className="grid gap-3 sm:grid-cols-[1fr_12rem_auto] sm:items-end">
            <TextField
              label="Name"
              required
              maxLength={100}
              placeholder="production-agent"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <TextField
              label="Expires in (days, optional)"
              inputMode="numeric"
              value={days}
              onChange={(e) => setDays(e.target.value)}
            />
            <Button type="submit" variant="primary" disabled={busy || !name.trim()}>
              {busy ? "Creating..." : "Create key"}
            </Button>
          </form>
        </Card>

        <Card title="Keys">
          {keys.error ? (
            <Alert tone="danger">{errorMessage(keys.error)}</Alert>
          ) : keys.loading && !keys.data ? (
            <Spinner />
          ) : items.length === 0 ? (
            <EmptyState>No API keys yet. Create one to connect an agent.</EmptyState>
          ) : (
            <Table caption="API keys">
              <thead>
                <tr>
                  <th className={TH}>Key</th>
                  <th className={TH}>Name</th>
                  <th className={TH}>Created</th>
                  <th className={TH}>Last used</th>
                  <th className={TH}>Expires</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((k) => {
                  const st = keyStatus(k);
                  return (
                    <tr key={k.id}>
                      <td className={TD}>
                        <code>{k.key_prefix}...</code>
                      </td>
                      <td className={TD}>{k.name}</td>
                      <td className={TD}>{fmtDate(k.created_at)}</td>
                      <td className={TD}>{k.last_used_at ? fmtDate(k.last_used_at) : "Never"}</td>
                      <td className={TD}>{k.expires_at ? fmtDate(k.expires_at) : "Never"}</td>
                      <td className={TD}>
                        <Badge tone={st.tone}>{st.label}</Badge>
                      </td>
                      <td className={TD}>
                        {!k.revoked_at ? (
                          <Button variant="danger" onClick={() => setRevoking(k)}>
                            Revoke<span className="sr-only"> {k.name}</span>
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          )}
        </Card>
      </div>

      {created ? <KeyShownOnceDialog created={created} onClose={() => setCreated(null)} /> : null}
      {revoking ? (
        <ConfirmDialog
          title="Revoke this key?"
          message={
            <>
              Any agent using <strong>{revoking.name}</strong> (<code>{revoking.key_prefix}...</code>)
              will be refused immediately. This cannot be undone.
            </>
          }
          confirmLabel="Revoke key"
          busy={revokeBusy}
          onConfirm={revoke}
          onCancel={() => setRevoking(null)}
        />
      ) : null}
    </>
  );
}
