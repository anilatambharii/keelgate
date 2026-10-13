"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, authRequest } from "@/lib/api";
import { MIN_PASSWORD_LENGTH, passwordHint, safeNext, validEmail } from "@/lib/validation";
import { Alert, Button, Card, TextField } from "./ui";

function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function LoginForm({
  next,
  expired,
  publicUrl,
}: {
  next?: string;
  expired?: boolean;
  publicUrl: string;
}) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [org, setOrg] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await authRequest("/auth/login", { email: email.trim(), password });
      router.replace(safeNext(next));
      router.refresh();
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  }

  function startSso(e: FormEvent) {
    e.preventDefault();
    const slug = org.trim().toLowerCase();
    if (!slug) return;
    // An external navigation to the control plane, not an in-app route.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign(`${publicUrl}/api/sso/login?org=${encodeURIComponent(slug)}`);
  }

  return (
    <div className="space-y-4">
      <Card title="Sign in">
        {expired ? (
          <div className="mb-3">
            <Alert tone="warn">Your session ended. Sign in again to continue.</Alert>
          </div>
        ) : null}
        <form onSubmit={submit} className="space-y-3" noValidate>
          <TextField
            label="Email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <TextField
            label="Password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <Button type="submit" variant="primary" className="w-full" disabled={busy || !email || !password}>
            {busy ? "Signing in..." : "Sign in"}
          </Button>
        </form>
      </Card>
      <Card title="Sign in with SSO">
        <form onSubmit={startSso} className="space-y-3">
          <TextField
            label="Organisation slug"
            hint="The short name of your organisation, for example acme-4f2a1c."
            autoComplete="organization"
            value={org}
            onChange={(e) => setOrg(e.target.value)}
          />
          <Button type="submit" className="w-full" disabled={!org.trim()}>
            Continue with SSO
          </Button>
        </form>
      </Card>
      <p className="text-center text-sm text-muted">
        New to Keelgate Cloud?{" "}
        <Link href="/signup" className="text-accent underline">
          Create an organisation
        </Link>
      </p>
    </div>
  );
}

export function SignupForm() {
  const router = useRouter();
  const [orgName, setOrgName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const hint = passwordHint(password);
  const canSubmit = orgName.trim() !== "" && validEmail(email) && password.length >= MIN_PASSWORD_LENGTH;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setBusy(true);
    try {
      await authRequest("/auth/signup", {
        org_name: orgName.trim(),
        email: email.trim(),
        password,
      });
      router.replace("/");
      router.refresh();
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card title="Create your organisation">
        <form onSubmit={submit} className="space-y-3" noValidate>
          <TextField
            label="Organisation name"
            autoComplete="organization"
            required
            maxLength={200}
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
          />
          <TextField
            label="Work email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <div>
            <TextField
              label="Password"
              type="password"
              autoComplete="new-password"
              required
              minLength={MIN_PASSWORD_LENGTH}
              aria-describedby="password-hint"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <p
              id="password-hint"
              aria-live="polite"
              className={`mt-1 text-xs ${hint.ok ? "text-ok" : "text-muted"}`}
            >
              {hint.text}
            </p>
          </div>
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <Button type="submit" variant="primary" className="w-full" disabled={busy || !canSubmit}>
            {busy ? "Creating..." : "Create organisation"}
          </Button>
          <p className="text-xs text-muted">
            You start on a free trial. You become the organisation owner.
          </p>
        </form>
      </Card>
      <p className="text-center text-sm text-muted">
        Already have an account?{" "}
        <Link href="/login" className="text-accent underline">
          Sign in
        </Link>
      </p>
    </div>
  );
}

/** Reads the session token from the URL fragment, stores it via a route handler, then moves on. */
export function SsoComplete() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const token = params.get("token");
    // Remove the token from the address bar and history straight away.
    window.history.replaceState(null, "", window.location.pathname);
    if (!token) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setError("No sign-in token was found. Start the sign-in again.");
      return;
    }
    authRequest("/auth/sso-complete", { token })
      .then(() => {
        router.replace("/");
        router.refresh();
      })
      .catch((err) => setError(message(err)));
  }, [router]);

  if (error) {
    return (
      <Card title="Sign-in failed">
        <Alert tone="danger">{error}</Alert>
        <p className="mt-3 text-sm">
          <Link href="/login" className="text-accent underline">
            Back to sign in
          </Link>
        </p>
      </Card>
    );
  }
  return (
    <Card title="Signing you in">
      <p role="status" className="text-sm text-muted">
        Finishing single sign-on...
      </p>
    </Card>
  );
}

export const SSO_ERRORS: Record<string, string> = {
  idp_unreachable: "We could not reach your identity provider. Try again in a moment.",
  idp_error: "Your identity provider reported an error. Ask your administrator to check the SSO setup.",
  invalid_state: "The sign-in attempt expired or was not started here. Start again from the sign-in page.",
  invalid_request: "The identity provider sent an incomplete response. Start the sign-in again.",
  not_available: "Single sign-on is not available for this organisation.",
  token_exchange_failed: "The identity provider refused the sign-in. Ask your administrator to check the client credentials.",
  token_invalid: "The identity provider sent a sign-in token we could not accept.",
  email_not_verified: "Your identity provider has not verified your email address.",
  domain_not_allowed: "Your email domain is not allowed to sign in to this organisation.",
  account_unavailable: "Your account is disabled or could not be created. Contact your administrator.",
  server_error: "Something went wrong on our side. Try again, and tell support if it continues.",
};

export function ssoErrorMessage(code: string | undefined): string {
  if (code && Object.prototype.hasOwnProperty.call(SSO_ERRORS, code)) return SSO_ERRORS[code];
  return "Single sign-on failed. Start again from the sign-in page.";
}
