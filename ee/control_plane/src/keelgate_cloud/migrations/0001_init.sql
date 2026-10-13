-- Keelgate Cloud control plane: schema, row-level security and grants.
--
-- Tenant isolation is enforced by the database, not by the application. Every tenant table has an
-- org_id and a policy that only admits rows whose org_id equals the transaction's `app.org_id`.
-- The application role cannot bypass it (it owns nothing, is not a superuser and has no
-- BYPASSRLS), and FORCE ROW LEVEL SECURITY applies it to the owner as well. A transaction that
-- sets no org sees no rows and can write none: deny by default.
--
-- A few tables must be read before a caller is known (to turn an API key, a session token or a
-- login e-mail into an org). Those policies also admit exactly the row whose secret-derived key
-- equals a `app.lookup_*` setting, so a lookup can find one row and nothing else.

CREATE FUNCTION app_org() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('app.org_id', true), '')::uuid $$;

CREATE FUNCTION app_lookup(name text) RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('app.lookup_' || name, true), '') $$;

-- ---------------------------------------------------------------------------------------- orgs

CREATE TABLE orgs (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    slug                   text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    plan                   text NOT NULL DEFAULT 'team',
    billing_status         text NOT NULL DEFAULT 'trialing'
                           CHECK (billing_status IN
                                  ('trialing', 'active', 'past_due', 'canceled', 'trial_expired')),
    trial_ends_at          timestamptz,
    residency              text NOT NULL DEFAULT 'us' CHECK (residency IN ('us', 'eu', 'apac')),
    settings               jsonb NOT NULL DEFAULT '{}'::jsonb,
    stripe_customer_id     text UNIQUE,
    stripe_subscription_id text,
    created_at             timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------- users and keys

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs (id),
    email         text NOT NULL CHECK (email = lower(email) AND position('@' IN email) > 1),
    display_name  text NOT NULL DEFAULT '',
    password_hash text,
    role          text NOT NULL CHECK (role IN ('owner', 'admin', 'approver', 'viewer')),
    approval_tier text NOT NULL DEFAULT 'ONE_CLICK' CHECK (approval_tier IN
                                                          ('ONE_CLICK', 'EXPLICIT_SIGNOFF')),
    oidc_subject  text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    disabled_at   timestamptz
);
-- An e-mail address belongs to exactly one org: login finds the org from the address.
CREATE UNIQUE INDEX users_email_key ON users (email);
CREATE INDEX users_org ON users (org_id);

CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    org_id     uuid NOT NULL REFERENCES orgs (id),
    user_id    uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE api_keys (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       uuid NOT NULL REFERENCES orgs (id),
    name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    key_prefix   text NOT NULL,
    key_hash     text NOT NULL UNIQUE,
    created_by   uuid REFERENCES users (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at   timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX api_keys_org ON api_keys (org_id);

CREATE TABLE sso_configs (
    org_id             uuid PRIMARY KEY REFERENCES orgs (id),
    issuer             text NOT NULL,
    client_id          text NOT NULL,
    client_secret_enc  text NOT NULL,
    allowed_domains    text[] NOT NULL DEFAULT '{}',
    default_role       text NOT NULL DEFAULT 'viewer' CHECK (default_role IN
                                                            ('admin', 'approver', 'viewer')),
    enabled            boolean NOT NULL DEFAULT true,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sso_states (
    state         text PRIMARY KEY,
    org_id        uuid NOT NULL REFERENCES orgs (id),
    nonce         text NOT NULL,
    code_verifier text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------- audit and agents

-- Same columns as the open-source audit store, so the chain exports and verifies identically.
-- tenant_id in the chain is the org id as text.
CREATE TABLE audit_records (
    org_id       uuid   NOT NULL REFERENCES orgs (id),
    seq          bigint NOT NULL CHECK (seq >= 1),
    timestamp    text   NOT NULL,
    event_type   text   NOT NULL,
    actor        text   NOT NULL,
    payload_json text   NOT NULL,
    prev_hash    text   NOT NULL,
    hash         text   NOT NULL,
    PRIMARY KEY (org_id, seq)
);

CREATE FUNCTION audit_records_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_records is append-only';
END $$;

CREATE TRIGGER audit_records_no_update BEFORE UPDATE ON audit_records
    FOR EACH ROW EXECUTE FUNCTION audit_records_append_only();
CREATE TRIGGER audit_records_no_delete BEFORE DELETE ON audit_records
    FOR EACH ROW EXECUTE FUNCTION audit_records_append_only();
CREATE TRIGGER audit_records_no_truncate BEFORE TRUNCATE ON audit_records
    FOR EACH STATEMENT EXECUTE FUNCTION audit_records_append_only();

CREATE TABLE agents (
    org_id     uuid NOT NULL REFERENCES orgs (id),
    agent_id   text NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, agent_id)
);

-- A POST that carries an Idempotency-Key is executed once; a retry gets the stored response.
CREATE TABLE idempotency_keys (
    org_id     uuid NOT NULL REFERENCES orgs (id),
    scope      text NOT NULL,
    key        text NOT NULL,
    response   jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, scope, key)
);

-- -------------------------------------------------------------------------------- approvals

CREATE TABLE approvals (
    request_id text PRIMARY KEY,
    org_id     uuid NOT NULL REFERENCES orgs (id),
    status     text NOT NULL,
    expires_at timestamptz NOT NULL,
    body       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX approvals_org_status ON approvals (org_id, status, created_at);

CREATE TABLE approval_routes (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs (id),
    priority      integer NOT NULL DEFAULT 100,
    tool_pattern  text NOT NULL DEFAULT '*',
    min_tier      text NOT NULL DEFAULT 'ONE_CLICK' CHECK (min_tier IN
                                                          ('ONE_CLICK', 'EXPLICIT_SIGNOFF')),
    allowed_roles text[] NOT NULL DEFAULT '{owner,admin,approver}',
    escalate_to   text CHECK (escalate_to IN ('ONE_CLICK', 'EXPLICIT_SIGNOFF')),
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------- policies

CREATE TABLE policy_packs (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     uuid NOT NULL REFERENCES orgs (id),
    name       text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE TABLE policy_versions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       uuid NOT NULL REFERENCES orgs (id),
    pack_id      uuid NOT NULL REFERENCES policy_packs (id),
    version      integer NOT NULL,
    files        jsonb NOT NULL,
    query        text NOT NULL,
    source_hash  text NOT NULL,
    cases        jsonb NOT NULL DEFAULT '[]'::jsonb,
    test_report  jsonb,
    tests_passed boolean NOT NULL DEFAULT false,
    created_by   uuid REFERENCES users (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pack_id, version)
);

CREATE TABLE policy_active (
    org_id      uuid NOT NULL REFERENCES orgs (id),
    pack_id     uuid PRIMARY KEY REFERENCES policy_packs (id),
    version_id  uuid NOT NULL REFERENCES policy_versions (id),
    deployed_by uuid REFERENCES users (id),
    deployed_at timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------- usage and traces

CREATE TABLE usage_daily (
    org_id   uuid   NOT NULL REFERENCES orgs (id),
    day      date   NOT NULL,
    metric   text   NOT NULL CHECK (metric IN ('gated_actions', 'traces_stored', 'approvals')),
    quantity bigint NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (org_id, day, metric)
);

-- What has been reported to Stripe for a billing period, so a retry never double-bills.
CREATE TABLE usage_reports (
    org_id      uuid NOT NULL REFERENCES orgs (id),
    period      text NOT NULL CHECK (period ~ '^\d{4}-\d{2}$'),
    metric      text NOT NULL,
    reported    bigint NOT NULL DEFAULT 0 CHECK (reported >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, period, metric)
);

CREATE TABLE traces (
    org_id         uuid   NOT NULL REFERENCES orgs (id),
    trace_id       text   NOT NULL,
    span_id        text   NOT NULL,
    parent_span_id text,
    name           text   NOT NULL,
    start_ns       bigint,
    end_ns         bigint,
    status         text   NOT NULL,
    attributes     jsonb  NOT NULL DEFAULT '{}'::jsonb,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, trace_id, span_id)
);
CREATE INDEX traces_org_time ON traces (org_id, ingested_at);

-- ------------------------------------------------------------------------------------ stripe

-- Webhook deliveries already handled. Holds no tenant data, so no row-level security.
CREATE TABLE stripe_events (
    event_id    text PRIMARY KEY,
    type        text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- row-level security policies

-- Plain tenant tables: visible and writable only inside the transaction's own org.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'sso_configs', 'audit_records', 'agents', 'idempotency_keys', 'approvals',
        'approval_routes', 'policy_packs', 'policy_versions', 'policy_active', 'usage_daily',
        'usage_reports', 'traces'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (org_id = app_org()) '
            'WITH CHECK (org_id = app_org())', t);
    END LOOP;
END $$;

-- Tables that are also looked up before the caller is known.
ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;
ALTER TABLE orgs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON orgs
    USING (id = app_org()
           OR slug = app_lookup('slug')
           OR stripe_customer_id = app_lookup('stripe_customer'))
    WITH CHECK (id = app_org());

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON users
    USING (org_id = app_org() OR email = app_lookup('email'))
    WITH CHECK (org_id = app_org());

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sessions
    USING (org_id = app_org() OR token_hash = app_lookup('session'))
    WITH CHECK (org_id = app_org());

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON api_keys
    USING (org_id = app_org() OR key_hash = app_lookup('key'))
    WITH CHECK (org_id = app_org());

ALTER TABLE sso_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE sso_states FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sso_states
    USING (org_id = app_org() OR state = app_lookup('sso_state'))
    WITH CHECK (org_id = app_org());

-- An SSO login starts from an org slug, before anyone is signed in.
CREATE POLICY slug_lookup ON sso_configs FOR SELECT
    USING (org_id IN (SELECT id FROM orgs WHERE slug = app_lookup('slug')));

-- ---------------------------------------------------------------------------------- grants

GRANT USAGE ON SCHEMA public TO __APP_ROLE__;
GRANT EXECUTE ON FUNCTION app_org(), app_lookup(text) TO __APP_ROLE__;
GRANT SELECT, INSERT, UPDATE ON
    orgs, users, sessions, api_keys, sso_configs, sso_states, agents, approvals, approval_routes,
    policy_packs, policy_versions, policy_active, usage_daily, usage_reports, traces
    TO __APP_ROLE__;
-- Audit records can be added and read, never changed or removed, by the application.
GRANT SELECT, INSERT ON audit_records TO __APP_ROLE__;
GRANT SELECT, INSERT, UPDATE ON idempotency_keys TO __APP_ROLE__;
GRANT DELETE ON sessions, sso_states, traces, approval_routes, api_keys, idempotency_keys
    TO __APP_ROLE__;
GRANT SELECT, INSERT ON stripe_events TO __APP_ROLE__;
