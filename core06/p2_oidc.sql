-- P2: immutable OIDC principal -> internal subject mapping.
-- Never derive subject from caller-supplied actor_id, preferred_username, or roles.
BEGIN;
SET ROLE factory_owner;
CREATE TABLE IF NOT EXISTS core.oidc_identity (
    issuer text NOT NULL,
    external_sub text NOT NULL,
    subject_id text NOT NULL REFERENCES core.subject(id) ON DELETE RESTRICT,
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY(issuer,external_sub),
    CHECK (length(issuer) BETWEEN 12 AND 512),
    CHECK (length(external_sub) BETWEEN 1 AND 255)
);
CREATE INDEX IF NOT EXISTS external_subject_idx ON core.oidc_identity(subject_id);
CREATE OR REPLACE FUNCTION core.resolve_oidc_subject(p_issuer text,p_sub text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
SELECT s.id FROM core.oidc_identity i JOIN core.subject s ON s.id=i.subject_id
WHERE i.issuer=p_issuer AND i.external_sub=p_sub AND i.enabled AND s.active
$$;
RESET ROLE;
REVOKE ALL ON core.oidc_identity FROM PUBLIC;
REVOKE ALL ON FUNCTION core.resolve_oidc_subject(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.resolve_oidc_subject(text,text) TO factory_gateway;
COMMIT;
