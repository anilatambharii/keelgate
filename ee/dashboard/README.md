# Keelgate Cloud dashboard

Proprietary (see `../LICENSE`). Next.js (App Router) + TypeScript (strict) + Tailwind. It is not part of
the Python wheel. It talks only to the control plane in `../control_plane`.

## Run it

```bash
# 1. the control plane (from ee/control_plane)
uv run python -m keelgate_cloud serve        # http://localhost:8000, docs at /api/docs

# 2. the dashboard (from ee/dashboard)
npm install
cp .env.example .env.local                   # then edit if your control plane is elsewhere
npm run dev                                  # http://localhost:3000
# or: npm run build && npm start
```

Set `KEELGATE_CP_DASHBOARD_URL` (and `KEELGATE_CP_CORS_ORIGINS`) on the control plane to the dashboard
origin: the SSO round trip and Stripe return URLs redirect there.

| Variable | Where | Meaning |
|---|---|---|
| `CONTROL_PLANE_URL` | server only | Control plane the BFF and server components call. Default `http://localhost:8000`. |
| `NEXT_PUBLIC_CONTROL_PLANE_PUBLIC_URL` | browser | URL shown in the agent snippet and used by "Sign in with SSO" (`/api/sso/login`). Inlined at build time. |
| `NEXT_PUBLIC_STRIPE_TEST_MODE` | browser | `false` hides the "Test mode" badge on the invoice preview. The API does not report `livemode`, so this is a deploy-time switch (default: badge shown). |
| `COOKIE_SECURE` | server | `true`/`false` overrides the default (Secure in production builds). |

## Scripts

| | |
|---|---|
| `npm run typecheck` | `tsc --noEmit`, strict |
| `npm run lint` | ESLint (next core-web-vitals + typescript) |
| `npm test` | Vitest + Testing Library |
| `npm run build` | production build |
| `npm run smoke` | `node scripts/smoke.mjs` against a running control plane and a running dashboard (`DASHBOARD_URL`, default `http://localhost:3000`). It signs up a fresh org through the BFF and exercises keys, approvals, traces, exports and logout. |

Telemetry is off (`next telemetry disable` runs before dev/build). There are no CDNs, external fonts or
analytics; the app makes no requests except to its own origin (and the control plane from the server).

## Security model

- **The browser never holds the session token.** `/auth/login`, `/auth/signup` and `/auth/sso-complete`
  (route handlers) store the control-plane session token in the httpOnly, `SameSite=Lax` cookie
  `kg_session` (`Secure` in production). The token is never in a response body. `/sso/complete` reads the
  token from the URL fragment, clears the fragment at once, and the route handler verifies it with
  `GET /api/me` before storing it.
- **`/bff/[...path]`** forwards browser requests to the control plane adding `Authorization: Bearer`.
  It forwards only `/api/...` (never `/v1/`, no dot-segments or smuggled separators), only the headers
  `content-type`, `accept`, `idempotency-key`, never cookies, never follows redirects, caps bodies at
  2 MiB, requires a same-origin `Origin` (or `Sec-Fetch-Site: same-origin`) for every non-GET, passes
  status/content-type/content-disposition through (exports stream as files), strips `Set-Cookie`, and
  turns a control-plane 401 into a 401 with `x-kg-login-required: 1` that also clears the cookie.
  The same-origin check compares `Origin` to the `Host` header; behind a reverse proxy make sure `Host`
  is the public host.
- **`proxy.ts`** (Next 16 name for middleware) redirects signed-out users to `/login`, signed-in users
  away from `/login` and `/signup`, and sets a per-request nonce CSP (`script-src 'self' 'nonce-...'
  'strict-dynamic'`, `connect-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`), plus
  `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy`, and
  `Cache-Control: no-store`. The cookie's validity is checked by the server layout; a dead session is
  sent through `/auth/expired`, which clears the cookie.
- **Untrusted text.** Agent- and model-written fields (rationale, tool name, agent id, args, sources,
  span attributes, audit payloads) are rendered as React text or pretty-printed JSON in `<pre>`, in
  labelled boxes, with control and bidi-override characters stripped. There is no
  `dangerouslySetInnerHTML`. Source URIs are shown as text, not links. No inline `style` attributes are
  used (bars and charts are SVG) so the CSP needs no `unsafe-inline`.
- **Authorisation is the server's.** The UI hides or disables what a role cannot do and explains why, but
  every rule (roles, tier clearance, routing rules, plan features) is enforced by the control plane.

## Layout

```
app/(auth)   login, signup, sso/complete, sso/error
app/(app)    overview, keys, billing, traces, approvals(+routes), policies, audit, team, settings
app/bff      the forwarder         app/auth   cookie-setting route handlers
lib/         api client, bff, auth-routes, csp, session, helpers   proxy.ts  middleware
components/  pages and small UI primitives (no UI library)          tests/    Vitest
```
