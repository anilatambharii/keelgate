-- A PII-free index of orgs for the two jobs that must visit every org (usage reporting to Stripe
-- and retention sweeping).
--
-- Row-level security shows the application role only the org it acts for, and it should stay that
-- way: no policy on `orgs` is loosened for these jobs. Instead this table holds just what they
-- need to choose and scope their work (org id, plan, billing state), has no names, e-mails or
-- Stripe ids, and is kept current by a trigger. The application role may read it and nothing else.
-- Trade-off: the role can list every org id with its plan and billing state.

CREATE TABLE org_index (
    org_id           uuid PRIMARY KEY,
    plan             text    NOT NULL,
    billing_status   text    NOT NULL,
    has_subscription boolean NOT NULL
);

CREATE FUNCTION org_index_sync() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM org_index WHERE org_id = OLD.id;
        RETURN OLD;
    END IF;
    INSERT INTO org_index (org_id, plan, billing_status, has_subscription)
    VALUES (NEW.id, NEW.plan, NEW.billing_status,
            NEW.stripe_customer_id IS NOT NULL AND NEW.stripe_subscription_id IS NOT NULL)
    ON CONFLICT (org_id) DO UPDATE
        SET plan = EXCLUDED.plan,
            billing_status = EXCLUDED.billing_status,
            has_subscription = EXCLUDED.has_subscription;
    RETURN NEW;
END $$;

CREATE TRIGGER orgs_index_sync AFTER INSERT OR UPDATE OR DELETE ON orgs
    FOR EACH ROW EXECUTE FUNCTION org_index_sync();

-- Backfill (the owner role is subject to row-level security, so this sees only orgs it is
-- scoped to; a fresh database has none, and later rows arrive through the trigger).
GRANT SELECT ON org_index TO __APP_ROLE__;
