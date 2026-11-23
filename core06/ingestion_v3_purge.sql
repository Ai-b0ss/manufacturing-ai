-- v0.6 controlled physical purge of already logically deleted uploaded documents.
-- This migration is applied only after schema.sql, ingestion_v1.sql, ingestion_v2.sql.
-- Qdrant absence is checked by a separate operator CLI WITH THE SAME advisory lock.
BEGIN;
SET ROLE factory_owner;
SET search_path=core,pg_catalog;

-- Preserve idempotency history without keeping an FK to physically purged data.
-- It is a minimal request digest tombstone, not the original title or file bytes.
ALTER TABLE core.ingest_request ALTER COLUMN document_id DROP NOT NULL;
ALTER TABLE core.ingest_request
  DROP CONSTRAINT IF EXISTS ingest_request_document_id_fkey;
ALTER TABLE core.ingest_request
  ADD CONSTRAINT ingest_request_document_id_fkey
  FOREIGN KEY(document_id) REFERENCES core.document(id) ON DELETE SET NULL;

-- Existing upload function must refuse to reuse a historical request key after
-- a physical purge (document_id is NULL by ON DELETE SET NULL).
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
   IF old_doc IS NULL OR NOT core.can_read_document(old_doc) THEN
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

CREATE OR REPLACE FUNCTION core.purge_deleted_upload(p_doc text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
 source text;
 deleted_at timestamptz;
 deletion_status text;
BEGIN
 -- Function is not granted to factory_gateway; reject non-admin sessions even
 -- if privileges are accidentally widened by a future SQL migration.
 IF session_user<>'factory_pg_admin' THEN
   RAISE EXCEPTION 'PURGE_ADMIN_ONLY' USING ERRCODE='42501';
 END IF;
 IF p_doc IS NULL OR p_doc !~ '^upload-[0-9a-f]{32}$' THEN
   RAISE EXCEPTION 'PURGE_DOCUMENT_ID_INVALID' USING ERRCODE='22023';
 END IF;
 -- Serializes with every Qdrant upsert/delete performed by ingest_index_worker.
 PERFORM pg_advisory_xact_lock(hashtextextended('purge-upload:'||p_doc,0));

 SELECT d.source_kind,d.deleted_at INTO source,deleted_at
 FROM core.document d WHERE d.id=p_doc FOR UPDATE;
 IF NOT FOUND THEN RETURN false; END IF;
 IF source<>'postgres_ingest' OR deleted_at IS NULL THEN
   RAISE EXCEPTION 'PURGE_REQUIRES_LOGICAL_DELETION' USING ERRCODE='42501';
 END IF;
 IF deleted_at > clock_timestamp() - interval '24 hours' THEN
   RAISE EXCEPTION 'PURGE_RETENTION_24_HOURS_NOT_MET' USING ERRCODE='42501';
 END IF;
 SELECT o.status INTO deletion_status FROM core.ingest_outbox o
 WHERE o.document_id=p_doc AND o.event_kind='delete' FOR UPDATE;
 IF deletion_status IS DISTINCT FROM 'indexed' THEN
   RAISE EXCEPTION 'PURGE_QDRANT_DELETION_NOT_CONFIRMED' USING ERRCODE='42501';
 END IF;
 IF EXISTS(SELECT 1 FROM core.ingest_outbox o
           WHERE o.document_id=p_doc AND o.status='processing') THEN
   RAISE EXCEPTION 'PURGE_INDEX_JOB_STILL_RUNNING' USING ERRCODE='42501';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM core.ingest_request r WHERE r.document_id=p_doc) THEN
   RAISE EXCEPTION 'PURGE_IDEMPOTENCY_RECORD_MISSING' USING ERRCODE='42501';
 END IF;

 -- The unique request digest tombstone survives with document_id=NULL.
 UPDATE core.ingest_request SET document_id=NULL WHERE document_id=p_doc;
 DELETE FROM core.document_blob WHERE document_id=p_doc;
 DELETE FROM core.document_chunk WHERE document_id=p_doc;
 DELETE FROM core.ingest_outbox WHERE document_id=p_doc;
 DELETE FROM core.document WHERE id=p_doc;
 INSERT INTO core.audit_event(actor,action,target,outcome)
   VALUES(session_user,'purge_uploaded_document',p_doc,'active_rows_deleted');
 RETURN true;
END;
$$;

RESET ROLE;
REVOKE ALL ON FUNCTION core.purge_deleted_upload(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION core.purge_deleted_upload(text) FROM factory_gateway;
GRANT EXECUTE ON FUNCTION core.purge_deleted_upload(text) TO factory_pg_admin;
-- v3 replaces the existing function body, retaining its narrow v2 gateway grant.
COMMIT;
