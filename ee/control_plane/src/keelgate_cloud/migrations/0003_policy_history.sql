-- Every activation of a policy version (deploy or rollback), oldest first by seq. Rollback reads
-- this to find the version that was active before the current one. Append-only for the app role.

CREATE TABLE policy_deployments (
    seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      uuid NOT NULL REFERENCES orgs (id),
    pack_id     uuid NOT NULL REFERENCES policy_packs (id),
    version_id  uuid NOT NULL REFERENCES policy_versions (id),
    kind        text NOT NULL CHECK (kind IN ('deploy', 'rollback')),
    deployed_by uuid REFERENCES users (id),
    deployed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX policy_deployments_pack ON policy_deployments (pack_id, seq DESC);

ALTER TABLE policy_deployments ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_deployments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON policy_deployments
    USING (org_id = app_org()) WITH CHECK (org_id = app_org());

GRANT SELECT, INSERT ON policy_deployments TO __APP_ROLE__;
