-- v0.6 isolated ingestion hardening: idempotency, logical deletion and outbox.
-- Apply after schema.sql + ingestion_v1.sql. Never run on live pilot until controlled rollout.
BEGIN;
SET ROLE factory_owner;
SET search_path=core,pg_catalog;

ALTER TABLE core.document ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE core.ingest_outbox ADD COLUMN IF NOT EXISTS lease_token text;

DROP POLICY IF EXISTS doc_gateway ON core.document;
CREATE POLICY doc_gateway ON core.document FOR SELECT TO factory_gateway
 USING (deleted_at IS NULL AND core.can_read_scope(scope_id));

CREATE OR REPLACE FUNCTION core.can_read_document(p_doc text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM core.document d
   WHERE d.id=p_doc AND d.deleted_at IS NULL AND core.can_read_scope(d.scope_id))
$$;

CREATE TABLE IF NOT EXISTS core.ingest_request (
  actor_id text NOT NULL REFERENCES core.subject(id) ON DELETE RESTRICT,
  request_key text NOT NULL CHECK(request_key ~ '^[A-Za-z0-9_-]{16,100}$'),
  request_digest text NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
  document_id text NOT NULL UNIQUE REFERENCES core.document(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(actor_id,request_key)
);
ALTER TABLE core.ingest_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.ingest_request FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ingest_request_owner ON core.ingest_request;
CREATE POLICY ingest_request_owner ON core.ingest_request
 FOR ALL TO factory_owner USING(true) WITH CHECK(true);

-- A request key is an idempotency fence, not an authority to bypass fresh ACL.
-- Call the v1 private function exactly once, within the same transaction.
CREATE OR REPLACE FUNCTION core.ingest_document_once(
  p_key text,p_doc text,p_scope text,p_title text,p_filename text,p_media text,
  p_content bytea,p_sha text,p_chunks jsonb)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
 actor text;
 fingerprint text;
 old_doc text;
 old_digest text;
BEGIN
 actor:=nullif(current_setting('factory.actor_id',true),'');
 IF actor IS NULL OR NOT core.can_write_scope(p_scope) THEN
   RAISE EXCEPTION 'SCOPE_WRITE_DENIED' USING ERRCODE='42501';
 END IF;
 IF p_key IS NULL OR p_key !~ '^[A-Za-z0-9_-]{16,100}$' THEN
   RAISE EXCEPTION 'IDEMPOTENCY_KEY_INVALID' USING ERRCODE='22023';
 END IF;
 IF p_sha IS NULL OR p_content IS NULL
    OR encode(sha256(p_content),'hex')<>p_sha THEN
   RAISE EXCEPTION 'FILE_SHA256_MISMATCH' USING ERRCODE='22023';
 END IF;
 IF p_title IS NULL OR p_filename IS NULL OR p_media IS NULL THEN
   RAISE EXCEPTION 'INVALID_METADATA' USING ERRCODE='22023';
 END IF;
 fingerprint:=encode(sha256(convert_to(
   jsonb_build_array(p_scope,p_title,p_filename,p_media,p_sha)::text,
   'UTF8')),'hex');
 PERFORM pg_advisory_xact_lock(hashtextextended(actor||':'||p_key,0));
 SELECT document_id,request_digest INTO old_doc,old_digest FROM core.ingest_request
   WHERE actor_id=actor AND request_key=p_key;
 IF FOUND THEN
   IF old_digest<>fingerprint THEN
      RAISE EXCEPTION 'IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST' USING ERRCODE='23505';
   END IF;
   IF NOT core.can_read_document(old_doc) THEN
      RAISE EXCEPTION 'IDEMPOTENT_DOCUMENT_NOT_AVAILABLE' USING ERRCODE='42501';
   END IF;
   RETURN old_doc;
 END IF;
 PERFORM core.ingest_document(p_doc,p_scope,p_title,p_filename,p_media,p_content,p_sha,p_chunks);
 INSERT INTO core.ingest_request(actor_id,request_key,request_digest,document_id)
 VALUES(actor,p_key,fingerprint,p_doc);
 RETURN p_doc;
END;
$$;

CREATE OR REPLACE FUNCTION core.delete_uploaded_document(p_doc text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
 actor text;
 doc_scope text;
 source text;
 deleted boolean;
BEGIN
 actor:=nullif(current_setting('factory.actor_id',true),'');
 IF actor IS NULL OR p_doc IS NULL OR p_doc !~ '^upload-[0-9a-f]{32}$' THEN
   RAISE EXCEPTION 'DELETE_NOT_AUTHORIZED' USING ERRCODE='42501';
 END IF;
 SELECT d.scope_id,d.source_kind,(d.deleted_at IS NOT NULL)
   INTO doc_scope,source,deleted
   FROM core.document d WHERE d.id=p_doc FOR UPDATE;
 IF NOT FOUND OR source<>'postgres_ingest' THEN
   RAISE EXCEPTION 'DOCUMENT_NOT_FOUND' USING ERRCODE='42501';
 END IF;
 IF NOT core.can_write_scope(doc_scope)
   OR NOT EXISTS (SELECT 1 FROM core.ingest_request r
                 WHERE r.document_id=p_doc AND r.actor_id=actor) THEN
   RAISE EXCEPTION 'DELETE_NOT_AUTHORIZED' USING ERRCODE='42501';
 END IF;
 IF deleted THEN RETURN false; END IF;
 UPDATE core.document SET deleted_at=now() WHERE id=p_doc;
 UPDATE core.ingest_outbox SET status='indexed',updated_at=now()
   WHERE document_id=p_doc AND event_kind='index' AND status IN ('pending','failed');
 INSERT INTO core.ingest_outbox(document_id,event_kind,status)
   VALUES(p_doc,'delete','pending')
 ON CONFLICT(document_id,event_kind) DO UPDATE
   SET status='pending',attempts=0,updated_at=now();
 INSERT INTO core.audit_event(actor,action,target,outcome)
   VALUES(actor,'delete_uploaded_document',p_doc,'ok');
 RETURN true;
END;
$$;


CREATE OR REPLACE FUNCTION core.writable_scopes()
RETURNS SETOF text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT s.id FROM core.scope s WHERE core.can_write_scope(s.id) ORDER BY s.id
$$;

CREATE OR REPLACE FUNCTION core.upload_index_status(p_doc text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT o.status FROM core.ingest_outbox o
 WHERE o.document_id=p_doc AND o.event_kind='index'
   AND core.can_read_document(p_doc)
$$;

RESET ROLE;
REVOKE ALL ON core.ingest_request FROM PUBLIC;
REVOKE ALL ON FUNCTION core.writable_scopes() FROM PUBLIC;
REVOKE ALL ON FUNCTION core.upload_index_status(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION core.ingest_document(text,text,text,text,text,bytea,text,jsonb) FROM factory_gateway;
REVOKE ALL ON FUNCTION core.ingest_document_once(text,text,text,text,text,text,bytea,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION core.delete_uploaded_document(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.ingest_document_once(text,text,text,text,text,text,bytea,text,jsonb) TO factory_gateway;
GRANT EXECUTE ON FUNCTION core.delete_uploaded_document(text) TO factory_gateway;
GRANT EXECUTE ON FUNCTION core.writable_scopes() TO factory_gateway;
GRANT EXECUTE ON FUNCTION core.upload_index_status(text) TO factory_gateway;
COMMIT;
