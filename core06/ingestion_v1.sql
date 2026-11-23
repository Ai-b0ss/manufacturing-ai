-- Factory Knowledge v0.6: isolated pilot upload/OCR schema.
-- Apply only to an isolated DB first; NEVER widen factory_gateway table write rights.
BEGIN;
SET ROLE factory_owner;
SET search_path=core,pg_catalog;

CREATE TABLE IF NOT EXISTS core.scope_write_grant (
  subject_id text NOT NULL REFERENCES core.subject(id) ON DELETE RESTRICT,
  scope_id text NOT NULL REFERENCES core.scope(id) ON DELETE RESTRICT,
  PRIMARY KEY (subject_id,scope_id)
);
ALTER TABLE core.scope_write_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.scope_write_grant FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS write_grant_owner ON core.scope_write_grant;
CREATE POLICY write_grant_owner ON core.scope_write_grant
  FOR ALL TO factory_owner USING(true) WITH CHECK(true);

CREATE TABLE IF NOT EXISTS core.document_blob (
  document_id text PRIMARY KEY REFERENCES core.document(id) ON DELETE CASCADE,
  filename text NOT NULL,
  media_type text NOT NULL,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  original_bytes bytea NOT NULL CHECK (octet_length(original_bytes) BETWEEN 1 AND 8388608),
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE core.document_blob ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.document_blob FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS blob_owner ON core.document_blob;
CREATE POLICY blob_owner ON core.document_blob
  FOR ALL TO factory_owner USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS blob_gateway ON core.document_blob;
CREATE POLICY blob_gateway ON core.document_blob
  FOR SELECT TO factory_gateway
  USING (core.can_read_document(document_id));

CREATE TABLE IF NOT EXISTS core.ingest_outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id text NOT NULL REFERENCES core.document(id) ON DELETE CASCADE,
  event_kind text NOT NULL DEFAULT 'index' CHECK (event_kind IN ('index','delete')),
  status text NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','processing','indexed','failed')),
  attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 100),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(document_id,event_kind)
);
ALTER TABLE core.ingest_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.ingest_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS outbox_owner ON core.ingest_outbox;
CREATE POLICY outbox_owner ON core.ingest_outbox
  FOR ALL TO factory_owner USING(true) WITH CHECK(true);

CREATE OR REPLACE FUNCTION core.can_write_scope(p_scope text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT core.can_read_scope(p_scope) AND EXISTS (
  SELECT 1 FROM core.scope_write_grant w JOIN core.subject s ON s.id=w.subject_id
  WHERE w.scope_id=p_scope AND w.subject_id=nullif(current_setting('factory.actor_id',true),'')
    AND s.active
 )
$$;

CREATE OR REPLACE FUNCTION core.ingest_document(
  p_doc text,p_scope text,p_title text,p_filename text,p_media text,
  p_content bytea,p_sha text,p_chunks jsonb)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
 item jsonb;
 page_text text;
 page_num integer;
 method text;
 idx integer:=0;
 total_chars integer:=0;
 actor text;
BEGIN
 actor:=nullif(current_setting('factory.actor_id',true),'');
 IF actor IS NULL OR NOT core.can_write_scope(p_scope) THEN
   RAISE EXCEPTION 'SCOPE_WRITE_DENIED' USING ERRCODE='42501';
 END IF;
 IF p_doc IS NULL OR p_doc !~ '^upload-[0-9a-f]{32}$' THEN
   RAISE EXCEPTION 'INVALID_DOCUMENT_ID' USING ERRCODE='22023';
 END IF;
 IF p_title IS NULL OR length(btrim(p_title)) NOT BETWEEN 1 AND 200 THEN
   RAISE EXCEPTION 'INVALID_TITLE' USING ERRCODE='22023';
 END IF;
 IF p_filename IS NULL OR length(p_filename) NOT BETWEEN 1 AND 180
   OR position('/' IN p_filename)>0 OR position(chr(92) IN p_filename)>0
   OR p_filename ~ '[[:cntrl:]]' THEN
   RAISE EXCEPTION 'INVALID_FILENAME' USING ERRCODE='22023';
 END IF;
 IF p_media IS NULL OR p_media NOT IN ('application/pdf',
   'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
   'text/plain') THEN
   RAISE EXCEPTION 'UNSUPPORTED_MEDIA' USING ERRCODE='22023';
 END IF;
 IF p_content IS NULL OR octet_length(p_content) NOT BETWEEN 1 AND 8388608 THEN
   RAISE EXCEPTION 'INVALID_FILE_SIZE' USING ERRCODE='22023';
 END IF;
 IF p_sha IS NULL OR p_sha !~ '^[0-9a-f]{64}$'
   OR encode(sha256(p_content),'hex')<>p_sha THEN
   RAISE EXCEPTION 'FILE_SHA256_MISMATCH' USING ERRCODE='22023';
 END IF;
 IF p_chunks IS NULL OR jsonb_typeof(p_chunks)<>'array' THEN
   RAISE EXCEPTION 'CHUNKS_NOT_ARRAY' USING ERRCODE='22023';
 END IF;
 IF jsonb_array_length(p_chunks) NOT BETWEEN 1 AND 100 THEN
   RAISE EXCEPTION 'CHUNK_COUNT_INVALID' USING ERRCODE='22023';
 END IF;
 INSERT INTO core.document(id,title,scope_id,doc_type,source_sha256,source_kind)
   VALUES(p_doc,btrim(p_title),p_scope,'uploaded',p_sha,'postgres_ingest');
 INSERT INTO core.document_blob(document_id,filename,media_type,content_sha256,original_bytes)
   VALUES(p_doc,p_filename,p_media,p_sha,p_content);
 FOR item IN SELECT value FROM jsonb_array_elements(p_chunks) LOOP
   idx:=idx+1;
   page_text:=item->>'text';
   method:=item->>'extraction_method';
   IF jsonb_typeof(item->'page')<>'number'
     OR coalesce(item->>'page','') !~ '^[1-9][0-9]{0,3}$'
     OR method NOT IN ('text','ocr')
     OR page_text IS NULL OR length(btrim(page_text))<1 THEN
     RAISE EXCEPTION 'INVALID_EXTRACTED_CHUNK' USING ERRCODE='22023';
   END IF;
   page_num:=(item->>'page')::integer;
   total_chars:=total_chars+length(page_text);
   IF total_chars>85000 THEN
     RAISE EXCEPTION 'EXTRACTED_TEXT_TOO_LARGE' USING ERRCODE='22023';
   END IF;
   INSERT INTO core.document_chunk(id,document_id,body,page_no,extraction_method)
     VALUES(p_doc||'-chunk-'||lpad(idx::text,3,'0'),
            p_doc,page_text,page_num,method);
 END LOOP;
 INSERT INTO core.ingest_outbox(document_id,event_kind)
   VALUES(p_doc,'index');
 INSERT INTO core.audit_event(actor,action,target,outcome)
   VALUES(actor,'upload_document',p_doc,'ok');
 RETURN p_doc;
END;
$$;

REVOKE ALL ON core.scope_write_grant,core.document_blob,core.ingest_outbox FROM PUBLIC;
REVOKE ALL ON FUNCTION core.can_write_scope(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION core.ingest_document(text,text,text,text,text,bytea,text,jsonb) FROM PUBLIC;
GRANT SELECT ON core.document_blob TO factory_gateway;
GRANT EXECUTE ON FUNCTION core.can_write_scope(text) TO factory_gateway;
GRANT EXECUTE ON FUNCTION core.ingest_document(text,text,text,text,text,bytea,text,jsonb)
  TO factory_gateway;
RESET ROLE;
COMMIT;
