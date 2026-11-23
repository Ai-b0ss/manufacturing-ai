-- Factory Knowledge v0.6: PostgreSQL policy-ledger foundation (pilot test).
-- Use only on isolated local cluster. All dynamic user access remains behind
-- the future Keycloak/OIDC-authorized policy gateway.
-- No app role owns protected tables or bypasses PostgreSQL RLS.
BEGIN;
CREATE SCHEMA IF NOT EXISTS core AUTHORIZATION factory_owner;
REVOKE ALL ON SCHEMA core FROM PUBLIC;
GRANT USAGE ON SCHEMA core TO factory_gateway;
SET ROLE factory_owner;
SET search_path = core,pg_catalog;

CREATE TABLE IF NOT EXISTS core.subject (
    id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    source text NOT NULL DEFAULT 'demo_import',
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS core.scope (
    id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    classification text NOT NULL DEFAULT 'internal'
        CHECK (classification IN ('public','internal','restricted','secret')),
    epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0)
);
CREATE TABLE IF NOT EXISTS core.scope_grant (
    subject_id text NOT NULL REFERENCES core.subject(id) ON DELETE RESTRICT,
    scope_id text NOT NULL REFERENCES core.scope(id) ON DELETE RESTRICT,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(subject_id,scope_id)
);
CREATE INDEX IF NOT EXISTS grant_scope_idx ON core.scope_grant(scope_id,subject_id);
CREATE TABLE IF NOT EXISTS core.policy_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0)
);
INSERT INTO core.policy_state(singleton,generation) VALUES(true,0) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS core.document (
    id text PRIMARY KEY,
    title text NOT NULL,
    designation text NOT NULL DEFAULT '',
    doc_type text NOT NULL DEFAULT 'reference',
    scope_id text NOT NULL REFERENCES core.scope(id),
    revision text NOT NULL DEFAULT '',
    source_sha256 text CHECK(source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
    source_kind text NOT NULL DEFAULT 'sqlite_demo',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS doc_scope_idx ON core.document(scope_id);
CREATE INDEX IF NOT EXISTS doc_designation_idx ON core.document(designation);
CREATE TABLE IF NOT EXISTS core.document_chunk (
    id text PRIMARY KEY,
    document_id text NOT NULL REFERENCES core.document(id) ON DELETE CASCADE,
    body text NOT NULL,
    page_no integer CHECK(page_no IS NULL OR page_no>0),
    extraction_method text NOT NULL DEFAULT 'text',
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig,body)) STORED
);
CREATE INDEX IF NOT EXISTS chunk_by_document ON core.document_chunk(document_id);
CREATE INDEX IF NOT EXISTS chunk_fts_gin ON core.document_chunk USING gin(search_vector);
CREATE TABLE IF NOT EXISTS core.finance (
    id text PRIMARY KEY,
    title text NOT NULL,
    planned numeric(16,2) NOT NULL,
    actual numeric(16,2) NOT NULL,
    scope_id text NOT NULL REFERENCES core.scope(id)
);
CREATE TABLE IF NOT EXISTS core.legacy_change_notice (
    id text PRIMARY KEY,
    part_id text NOT NULL,
    scope_id text NOT NULL REFERENCES core.scope(id),
    new_revision text NOT NULL,
    valid_from text NOT NULL,
    directive text NOT NULL,
    summary text NOT NULL
);
CREATE TABLE IF NOT EXISTS core.legacy_lot (
    id text PRIMARY KEY,
    part_id text NOT NULL,
    revision_id text NOT NULL,
    manufactured_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS core.engineering_source (
    id text PRIMARY KEY,
    source_group text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    relative_path text NOT NULL,
    original_sha256 text NOT NULL CHECK(original_sha256 ~ '^[0-9a-f]{64}$'),
    original_size bigint NOT NULL CHECK(original_size>=0),
    original_url text NOT NULL DEFAULT '',
    parse_status text NOT NULL,
    page_count integer NOT NULL DEFAULT 0,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    scope_id text NOT NULL REFERENCES core.scope(id)
);
CREATE TABLE IF NOT EXISTS core.engineering_source_page (
    source_id text NOT NULL REFERENCES core.engineering_source(id) ON DELETE CASCADE,
    page integer NOT NULL CHECK(page>0),
    body text NOT NULL,
    status text NOT NULL,
    PRIMARY KEY(source_id,page)
);
CREATE TABLE IF NOT EXISTS core.engineering_bom_line (
    source_id text NOT NULL REFERENCES core.engineering_source(id) ON DELETE CASCADE,
    row_number integer NOT NULL,
    part_key text NOT NULL,
    part_name text NOT NULL,
    item_no text NOT NULL,
    material text NOT NULL,
    quantity text NOT NULL,
    unit text NOT NULL,
    revision text NOT NULL,
    row_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(source_id,row_number)
);
CREATE TABLE IF NOT EXISTS core.audit_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    outcome text NOT NULL
);
CREATE TABLE IF NOT EXISTS core.migration_manifest (
    source_name text PRIMARY KEY,
    source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
    source_bytes bigint NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    rows_json jsonb NOT NULL,
    baseline text NOT NULL DEFAULT 'p13-demo'
);

-- Actor is injected transaction-locally ONLY by trusted gateway after auth.
-- WARNING: custom GUCs are not tamper-proof DB identity and cannot replace OIDC.
CREATE OR REPLACE FUNCTION core.can_read_scope(p_scope text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
SELECT EXISTS(
 SELECT 1 FROM core.scope_grant g JOIN core.subject s ON s.id=g.subject_id
 WHERE g.scope_id=p_scope AND s.active
   AND g.subject_id=nullif(current_setting('factory.actor_id',true),'')
)
$$;
CREATE OR REPLACE FUNCTION core.can_read_document(p_doc text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
SELECT EXISTS(
 SELECT 1 FROM core.document d
 WHERE d.id=p_doc AND core.can_read_scope(d.scope_id)
)
$$;

-- FORCED RLS means even the table owner is restricted without explicit owner policy.
ALTER TABLE core.document ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.document FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS doc_owner ON core.document;
CREATE POLICY doc_owner ON core.document FOR ALL TO factory_owner USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS doc_gateway ON core.document;
CREATE POLICY doc_gateway ON core.document FOR SELECT TO factory_gateway
    USING (core.can_read_scope(scope_id));

ALTER TABLE core.document_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.document_chunk FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chunk_owner ON core.document_chunk;
CREATE POLICY chunk_owner ON core.document_chunk FOR ALL TO factory_owner USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS chunk_gateway ON core.document_chunk;
CREATE POLICY chunk_gateway ON core.document_chunk FOR SELECT TO factory_gateway
    USING (core.can_read_document(document_id));

ALTER TABLE core.finance ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.finance FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS finance_owner ON core.finance;
CREATE POLICY finance_owner ON core.finance FOR ALL TO factory_owner USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS finance_gateway ON core.finance;
CREATE POLICY finance_gateway ON core.finance FOR SELECT TO factory_gateway
    USING (core.can_read_scope(scope_id));

ALTER TABLE core.engineering_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.engineering_source FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS es_owner ON core.engineering_source;
CREATE POLICY es_owner ON core.engineering_source FOR ALL TO factory_owner USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS es_gateway ON core.engineering_source;
CREATE POLICY es_gateway ON core.engineering_source FOR SELECT TO factory_gateway
    USING (core.can_read_scope(scope_id));

ALTER TABLE core.engineering_source_page ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.engineering_source_page FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ep_owner ON core.engineering_source_page;
CREATE POLICY ep_owner ON core.engineering_source_page FOR ALL TO factory_owner USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS ep_gateway ON core.engineering_source_page;
CREATE POLICY ep_gateway ON core.engineering_source_page FOR SELECT TO factory_gateway
    USING (EXISTS (SELECT 1 FROM core.engineering_source s
                   WHERE s.id=source_id AND core.can_read_scope(s.scope_id)));

ALTER TABLE core.engineering_bom_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.engineering_bom_line FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS eb_owner ON core.engineering_bom_line;
CREATE POLICY eb_owner ON core.engineering_bom_line FOR ALL TO factory_owner USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS eb_gateway ON core.engineering_bom_line;
CREATE POLICY eb_gateway ON core.engineering_bom_line FOR SELECT TO factory_gateway
    USING (EXISTS (SELECT 1 FROM core.engineering_source s
                   WHERE s.id=source_id AND core.can_read_scope(s.scope_id)));

-- Protected mutation functions are NOT executable by factory_gateway.
CREATE OR REPLACE FUNCTION core.revoke_scope_access(
    p_subject text,p_scope text,p_actor text DEFAULT 'policy_administrator')
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE g bigint;
BEGIN
 DELETE FROM core.scope_grant WHERE subject_id=p_subject AND scope_id=p_scope;
 UPDATE core.scope SET epoch=epoch+1 WHERE id=p_scope;
 IF NOT FOUND THEN RAISE EXCEPTION 'SCOPE_NOT_FOUND'; END IF;
 UPDATE core.policy_state SET generation=generation+1
   WHERE singleton=true RETURNING generation INTO g;
 INSERT INTO core.audit_event(actor,action,target,outcome)
   VALUES(p_actor,'revoke_scope',p_subject||'/'||p_scope,'ok');
 RETURN g;
END$$;

CREATE OR REPLACE FUNCTION core.move_document_scope(
    p_document text,p_new_scope text,p_actor text DEFAULT 'policy_administrator')
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE old_scope text; g bigint;
BEGIN
 SELECT scope_id INTO old_scope FROM core.document
   WHERE id=p_document FOR UPDATE;
 IF old_scope IS NULL THEN RAISE EXCEPTION 'DOCUMENT_NOT_FOUND'; END IF;
 IF NOT EXISTS (SELECT 1 FROM core.scope WHERE id=p_new_scope)
   THEN RAISE EXCEPTION 'SCOPE_NOT_FOUND'; END IF;
 IF old_scope<>p_new_scope THEN
   UPDATE core.document SET scope_id=p_new_scope WHERE id=p_document;
   UPDATE core.scope SET epoch=epoch+1 WHERE id IN (old_scope,p_new_scope);
   UPDATE core.policy_state SET generation=generation+1
     WHERE singleton=true RETURNING generation INTO g;
   INSERT INTO core.audit_event(actor,action,target,outcome)
     VALUES(p_actor,'move_document',p_document||':'||old_scope||'->'||p_new_scope,'ok');
 ELSE
   SELECT generation INTO g FROM core.policy_state WHERE singleton=true;
 END IF;
 RETURN g;
END$$;
RESET ROLE;

-- The application role cannot write data, change grants, or execute mutable policy functions.
REVOKE ALL ON ALL TABLES IN SCHEMA core FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA core FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA core FROM PUBLIC;
GRANT SELECT ON core.document,core.document_chunk,core.finance,
    core.engineering_source,core.engineering_source_page,core.engineering_bom_line
    TO factory_gateway;
GRANT EXECUTE ON FUNCTION core.can_read_scope(text),core.can_read_document(text)
    TO factory_gateway;
COMMIT;
