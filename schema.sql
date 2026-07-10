-- credit-ledger — standalone Postgres schema.
-- Two tables + one unique constraint + row-level security. Apply once:
--     psql "$DATABASE_URL" -f schema.sql

-- Your app almost certainly already has a tenants table; this is a minimal stand-in so the FK +
-- ON DELETE CASCADE keep balances / ledger rows tied to a tenant's lifecycle. Adapt as needed.
CREATE TABLE IF NOT EXISTS tenants (
    id   uuid PRIMARY KEY,
    slug text,
    name text
);

-- Live balance: one row per tenant.
CREATE TABLE credit_balance (
    tenant_id  uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    credits    numeric(18, 4) NOT NULL DEFAULT 0,
    updated_at timestamptz    NOT NULL DEFAULT now()
);

-- Append-only audit of every grant / spend.
CREATE TABLE credit_ledger (
    id              uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    delta           numeric(18, 4) NOT NULL,
    reason          varchar(120)   NOT NULL DEFAULT 'usage',
    idempotency_key varchar(200),
    balance_after   numeric(18, 4),
    created_at      timestamptz    NOT NULL DEFAULT now()
);
CREATE INDEX ix_credit_ledger_tenant ON credit_ledger (tenant_id);

-- THE idempotency guarantee: a retried / replayed charge with the same key can't insert a second row.
ALTER TABLE credit_ledger ADD CONSTRAINT uq_credit_ledger_idem UNIQUE (tenant_id, idempotency_key);

-- ── Row-level security: tenant isolation as a DATABASE guarantee ────────────────────────────────
-- The metering code binds `app.current_tenant` per transaction; FORCE RLS so even the table owner is
-- subject to the policy. Point GRANTs at your app's NON-superuser role (a superuser / BYPASSRLS role
-- would skip the policy — which is exactly why prod uses a plain role and the code binds the GUC).
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pantheon_app') THEN
        CREATE ROLE pantheon_app;
    END IF;
END $$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['credit_balance', 'credit_ledger'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY %I_tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.current_tenant'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.current_tenant'', true), '''')::uuid)',
            t, t);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO pantheon_app', t);
    END LOOP;
END $$;
