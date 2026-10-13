"use client";
import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import type { OrgSettings, SsoConfig } from "@/lib/types";
import { isAdmin, useMe } from "./me-context";
import { errorMessage, useToast } from "./toast";
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfirmDialog,
  PageHeader,
  SelectField,
  Spinner,
  TextField,
} from "./ui";

export function parseDomains(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((d) => d.trim().toLowerCase().replace(/^@/, ""))
    .filter(Boolean);
}

export function SettingsPage() {
  const me = useMe();
  const owner = me.user.role === "owner";
  const settings = useLoad(() => api.settings(), []);
  const regions = useLoad(() => api.residencyRegions(), []);
  const s = settings.data;
  const hasSso = (s?.features ?? me.org.features).includes("sso");

  return (
    <>
      <PageHeader title="Settings" subtitle={`Organisation slug: ${me.org.slug}`} />
      <div className="grid gap-4">
        <Card title="Organisation">
          {settings.error ? (
            <Alert tone="danger">{errorMessage(settings.error)}</Alert>
          ) : !s ? (
            <Spinner />
          ) : (
            <OrgForm
              key={`${s.name}|${s.residency}`}
              s={s}
              owner={owner}
              regions={regions.data?.regions ?? [{ id: s.residency, label: s.residency }]}
              onSaved={(v) => settings.setData(v)}
            />
          )}
        </Card>

        <Card title="Retention">
          {!s ? (
            <Spinner />
          ) : (
            <>
              <dl className="grid max-w-xl grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-sm">
                <dt className="text-muted">Traces kept for</dt>
                <dd>{s.retention.trace_retention_days === null ? "Set by contract" : `${s.retention.trace_retention_days} days`}</dd>
                <dt className="text-muted">Audit records kept at least</dt>
                <dd>
                  {s.retention.audit_min_retention_days === null
                    ? "Set by contract"
                    : `${s.retention.audit_min_retention_days} days`}
                </dd>
                <dt className="text-muted">Audit log immutable</dt>
                <dd>{s.retention.audit_immutable ? "Yes" : "No"}</dd>
              </dl>
              <p className="mt-2 text-xs text-muted">
                Retention follows your plan ({me.org.plan_name}) and cannot be changed here.
              </p>
            </>
          )}
        </Card>

        {isAdmin(me.user.role) ? (
          hasSso ? (
            <SsoCard />
          ) : (
            <Card title="Single sign-on">
              <Alert tone="info" title="SSO is not part of your plan">
                OIDC single sign-on is available on higher plans.{" "}
                <Link href="/billing" className="underline">
                  See plans
                </Link>
                .
              </Alert>
            </Card>
          )
        ) : null}
      </div>
    </>
  );
}

function OrgForm({
  s,
  owner,
  regions,
  onSaved,
}: {
  s: OrgSettings;
  owner: boolean;
  regions: { id: string; label: string }[];
  onSaved: (s: OrgSettings) => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(s.name);
  const [residency, setResidency] = useState(s.residency);
  const [saving, setSaving] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    const body: { name?: string; residency?: string } = {};
    if (name.trim() !== s.name) body.name = name.trim();
    if (residency !== s.residency) body.residency = residency;
    if (Object.keys(body).length === 0) {
      toast.info("Nothing to change");
      return;
    }
    setSaving(true);
    try {
      onSaved(await api.updateSettings(body));
      toast.success("Settings saved");
    } catch (err) {
      toast.error(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={save} className="grid max-w-xl gap-3">
      <TextField
        label="Organisation name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={200}
        disabled={!owner}
      />
      <SelectField
        label="Data residency"
        value={residency}
        onChange={(e) => setResidency(e.target.value)}
        disabled={!owner}
        hint={
          s.residency_enforced
            ? "Data is stored in this region."
            : `This is a recorded label. It is not enforced: ${s.residency_note ?? "this deployment does not route or store data by region."}`
        }
      >
        {regions.map((r) => (
          <option key={r.id} value={r.id}>
            {r.label} ({r.id})
          </option>
        ))}
      </SelectField>
      <div className="flex items-center gap-3">
        <Button type="submit" variant="primary" disabled={!owner || saving}>
          {saving ? "Saving..." : "Save changes"}
        </Button>
        {!owner ? <span className="text-xs text-muted">Only the owner can change these.</span> : null}
      </div>
    </form>
  );
}

export function SsoCard() {
  const toast = useToast();
  const [cfg, setCfg] = useState<SsoConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [domains, setDomains] = useState("");
  const [role, setRole] = useState<"admin" | "approver" | "viewer">("viewer");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);

  function fill(c: SsoConfig) {
    setCfg(c);
    setIssuer(c.issuer);
    setClientId(c.client_id);
    setDomains(c.allowed_domains.join(", "));
    setRole(c.default_role);
    setEnabled(c.enabled);
    setSecret("");
  }

  useEffect(() => {
    let cancelled = false;
    api
      .sso()
      .then((c) => !cancelled && fill(c))
      .catch((e) => {
        if (cancelled) return;
        // 404 just means "not configured yet".
        if (!(e instanceof ApiError && e.status === 404)) setLoadError(errorMessage(e));
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const list = parseDomains(domains);
    if (list.length === 0) {
      toast.error(new Error("Add at least one allowed email domain"));
      return;
    }
    setBusy(true);
    try {
      const saved = await api.saveSso({
        issuer: issuer.trim(),
        client_id: clientId.trim(),
        ...(secret ? { client_secret: secret } : {}),
        allowed_domains: list,
        default_role: role,
        enabled,
      });
      fill(saved);
      toast.success("SSO configuration saved");
    } catch (err) {
      toast.error(err);
    } finally {
      setSecret("");
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await api.deleteSso();
      setCfg(null);
      setIssuer("");
      setClientId("");
      setDomains("");
      setSecret("");
      setEnabled(true);
      setRemoving(false);
      toast.success("SSO removed");
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="Single sign-on (OIDC)"
      actions={
        cfg ? <Badge tone={cfg.enabled ? "ok" : "neutral"}>{cfg.enabled ? "configured, enabled" : "configured, disabled"}</Badge> : <Badge>not configured</Badge>
      }
    >
      {loading ? (
        <Spinner />
      ) : loadError ? (
        <Alert tone="danger">{loadError}</Alert>
      ) : (
        <form onSubmit={submit} className="grid max-w-xl gap-3">
          {cfg?.redirect_uri ? (
            <p className="text-sm">
              Register this redirect URI with your identity provider:{" "}
              <code className="break-all">{cfg.redirect_uri}</code>
            </p>
          ) : null}
          <TextField
            label="Issuer URL"
            type="url"
            required
            placeholder="https://login.example.com"
            value={issuer}
            onChange={(e) => setIssuer(e.target.value)}
          />
          <TextField label="Client ID" required value={clientId} onChange={(e) => setClientId(e.target.value)} />
          <TextField
            label="Client secret"
            type="password"
            autoComplete="new-password"
            placeholder={cfg?.client_secret_set ? "A secret is stored - leave empty to keep it" : ""}
            required={!cfg?.client_secret_set}
            hint="Write-only. It is encrypted at rest and never shown again."
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
          />
          <TextField
            label="Allowed email domains"
            required
            placeholder="example.com, example.org"
            hint="Only people with an email at one of these domains can sign in."
            value={domains}
            onChange={(e) => setDomains(e.target.value)}
          />
          <SelectField
            label="Default role for new SSO users"
            value={role}
            onChange={(e) => setRole(e.target.value as "admin" | "approver" | "viewer")}
          >
            <option value="viewer">viewer</option>
            <option value="approver">approver</option>
            <option value="admin">admin</option>
          </SelectField>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            SSO enabled
          </label>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "Saving..." : cfg ? "Save SSO settings" : "Configure SSO"}
            </Button>
            {cfg ? (
              <Button variant="danger" onClick={() => setRemoving(true)} disabled={busy}>
                Remove SSO
              </Button>
            ) : null}
          </div>
        </form>
      )}
      {removing ? (
        <ConfirmDialog
          title="Remove SSO?"
          message="People who sign in with SSO keep their accounts but can no longer use SSO until you configure it again."
          confirmLabel="Remove SSO"
          busy={busy}
          onConfirm={remove}
          onCancel={() => setRemoving(false)}
        />
      ) : null}
    </Card>
  );
}
