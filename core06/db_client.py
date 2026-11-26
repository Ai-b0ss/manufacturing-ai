"""Bounded PostgreSQL v0.6 data-access prototype.

IMPORTANT: actor_id must come exclusively from a future verified OIDC principal,
not an HTTP request body. This module is DB-level testing only, not an IAM
gateway. Custom PG settings alone do not authenticate users.
"""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import re
import os
import psycopg

def _pg_port()->int:
    raw=os.environ.get("FACTORY_PG_PORT","55436").strip()
    if not raw.isascii() or not raw.isdigit() or not 1024<=int(raw)<=65535:
        raise RuntimeError("INVALID_FACTORY_PG_PORT")
    return int(raw)

HOST="127.0.0.1"
PORT=_pg_port()
_STAGE_DATABASE=os.environ.get("FACTORY_V06_STAGING_DATABASE")
if _STAGE_DATABASE is not None:
    if os.environ.get("FACTORY_V06_UNIFIED_STAGING")!="1" or not re.fullmatch(
        r"factory_ingest_stage_[0-9a-f]{12}",_STAGE_DATABASE):
        raise RuntimeError("INVALID_ISOLATED_STAGING_DATABASE")
DEFAULT_DATABASE=_STAGE_DATABASE or "factory_core_v06"
CREDS=Path(r"K:\ManufacturingAI-CoreRuntime\credentials")


def _creds_dir()->Path:
    raw=os.environ.get("FACTORY_PG_CREDS_DIR","").strip()
    if not raw:
        return CREDS
    if _pg_port()==55436:
        raise RuntimeError("CREDS_OVERRIDE_FORBIDDEN_ON_LIVE_PORT")
    path=Path(raw).resolve()
    if not path.is_dir():
        raise RuntimeError("INVALID_FACTORY_PG_CREDS_DIR")
    return path


class InvalidRequest(ValueError):pass

@contextmanager
def database_session(database=DEFAULT_DATABASE,admin=False):
    username="factory_pg_admin" if admin else "factory_gateway"
    credential_file="admin-password.txt" if admin else "gateway-password.txt"
    password=(_creds_dir()/credential_file).read_text(encoding="ascii")
    with psycopg.connect(host=HOST,port=_pg_port(),dbname=database,user=username,
                         password=password,connect_timeout=8,autocommit=True) as db:
        yield db

def _subject(subject):
    if not isinstance(subject,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,70}",subject):
        raise InvalidRequest("INVALID_VERIFIED_ACTOR")
    return subject

def _limit(limit):
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=50:
        raise InvalidRequest("INVALID_LIMIT")
    return limit

def list_authorized_documents(db,verified_actor,limit=50):
    subject=_subject(verified_actor);cap=_limit(limit)
    with db.transaction():
        db.execute("SELECT set_config('factory.actor_id', %s, true)",(subject,))
        return db.execute("""SELECT id,title,designation,doc_type,revision
                 FROM core.document ORDER BY id LIMIT %s""",(cap,)).fetchall()

def read_authorized_document(db,verified_actor,doc_id):
    subject=_subject(verified_actor)
    if not isinstance(doc_id,str) or not 1<=len(doc_id)<=128:
        raise InvalidRequest("DOCUMENT_ID_INVALID")
    with db.transaction():
        db.execute("SELECT set_config('factory.actor_id', %s, true)",(subject,))
        row=db.execute("""SELECT id,title,designation,doc_type,revision
                       FROM core.document WHERE id=%s""",(doc_id,)).fetchone()
        if row is None:return None
        chunks=db.execute("""SELECT id,body,page_no,extraction_method
                           FROM core.document_chunk WHERE document_id=%s ORDER BY id LIMIT 100""",
                          (doc_id,)).fetchall()
        return {"document":row,"chunks":chunks}

def search_authorized_chunks(db,verified_actor,query,limit=10):
    from core06.text_grounding import build_pg_tsqueries
    subject=_subject(verified_actor);cap=_limit(limit)
    if not isinstance(query,str) or not 1<=len(query.strip())<=250:
        raise InvalidRequest("QUERY_INVALID")
    ru_tsq,simple_tsq=build_pg_tsqueries(query)
    if not ru_tsq and not simple_tsq:
        return []
    with db.transaction():
        db.execute("SELECT set_config('factory.actor_id', %s, true)",(subject,))
        if ru_tsq and simple_tsq:
            return db.execute("""SELECT c.id,c.document_id,
                             ts_rank_cd(
                               setweight(to_tsvector('russian',concat_ws(' ',d.title,d.designation)),'A') ||
                               setweight(to_tsvector('russian',c.body),'B'),
                               to_tsquery('russian',%s)
                             ) +
                             ts_rank_cd(
                               setweight(to_tsvector('simple',translate(concat_ws(' ',d.title,d.designation),'.-','  ')),'A') ||
                               setweight(to_tsvector('simple',translate(c.body,'.-','  ')),'B'),
                               to_tsquery('simple',%s)
                             ) AS rank
                         FROM core.document_chunk c
                         JOIN core.document d ON d.id=c.document_id
                         WHERE to_tsvector('russian',concat_ws(' ',d.title,d.designation,c.body)) @@ to_tsquery('russian',%s)
                            OR to_tsvector('simple',translate(concat_ws(' ',d.title,d.designation,c.body),'.-','  ')) @@ to_tsquery('simple',%s)
                         ORDER BY rank DESC,c.id LIMIT %s""",
                    (ru_tsq,simple_tsq,ru_tsq,simple_tsq,cap)).fetchall()
        if ru_tsq:
            return db.execute("""SELECT c.id,c.document_id,
                             ts_rank_cd(
                               setweight(to_tsvector('russian',concat_ws(' ',d.title,d.designation)),'A') ||
                               setweight(to_tsvector('russian',c.body),'B'),
                               to_tsquery('russian',%s)
                             ) AS rank
                         FROM core.document_chunk c
                         JOIN core.document d ON d.id=c.document_id
                         WHERE to_tsvector('russian',concat_ws(' ',d.title,d.designation,c.body)) @@ to_tsquery('russian',%s)
                         ORDER BY rank DESC,c.id LIMIT %s""",
                    (ru_tsq,ru_tsq,cap)).fetchall()
        return db.execute("""SELECT c.id,c.document_id,
                         ts_rank_cd(
                           setweight(to_tsvector('simple',translate(concat_ws(' ',d.title,d.designation),'.-','  ')),'A') ||
                           setweight(to_tsvector('simple',translate(c.body,'.-','  ')),'B'),
                           to_tsquery('simple',%s)
                         ) AS rank
                     FROM core.document_chunk c
                     JOIN core.document d ON d.id=c.document_id
                     WHERE to_tsvector('simple',translate(concat_ws(' ',d.title,d.designation,c.body),'.-','  ')) @@ to_tsquery('simple',%s)
                     ORDER BY rank DESC,c.id LIMIT %s""",
                (simple_tsq,simple_tsq,cap)).fetchall()

def authorized_finance(db,verified_actor,limit=50):
    subject=_subject(verified_actor);cap=_limit(limit)
    with db.transaction():
        db.execute("SELECT set_config('factory.actor_id', %s, true)",(subject,))
        return db.execute("""SELECT id,title,planned,actual
                           FROM core.finance ORDER BY id LIMIT %s""",(cap,)).fetchall()
