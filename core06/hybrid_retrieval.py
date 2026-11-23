"""Policy-first P3 retrieval adapter: Qdrant candidate IDs + PostgreSQL RLS hydration.

No Qdrant point payload contains document text, chunk body, ACL grants,
personal names, access epochs, or session identifiers. Qdrant can be stale,
compromised or reindexed: only fresh PostgreSQL RLS authorizes final text.
Dense model is local-only and never calls an external model API.

Pilot encoder: cached multilingual-e5-small (384 dimensions, CPU).
Quality on Russian manufacturing corpora is NOT yet certified.
"""
from __future__ import annotations
import hashlib,math,os,re,uuid
from dataclasses import dataclass
from pathlib import Path
from collections import Counter

from qdrant_client import QdrantClient,models
from core06.db_client import DEFAULT_DATABASE,database_session,InvalidRequest

_STAGE_COLLECTION=os.environ.get("FACTORY_V06_STAGING_COLLECTION")
if _STAGE_COLLECTION is not None:
    if os.environ.get("FACTORY_V06_UNIFIED_STAGING")!="1" or not re.fullmatch(
        r"factory_ingest_stage_[0-9a-f]{12}",_STAGE_COLLECTION):
        raise RuntimeError("INVALID_ISOLATED_STAGING_COLLECTION")
COLLECTION=_STAGE_COLLECTION or "factory_v06_demo_hybrid"
TENANT="synthetic-demo"
VECTOR_SIZE=384
NAMESPACE=uuid.UUID("06fef1ba-e774-4c30-ac9d-cc66b0e6f896")
MODEL_PATH=Path(r"C:\Users\user\.cache\huggingface\hub\models--intfloat--multilingual-e5-small\snapshots\614241f622f53c4eeff9890bdc4f31cfecc418b3")
APIKEY=Path(r"K:\ManufacturingAI-CoreRuntime\credentials\qdrant-api-key.txt")
URL="http://127.0.0.1:16333"
_ISOLATED_VECTOR_PORT = os.environ.get("FACTORY_V06_ISOLATED_VECTOR_PORT")
if _ISOLATED_VECTOR_PORT is not None:
    if (not _STAGE_COLLECTION or DEFAULT_DATABASE != _STAGE_COLLECTION
            or not re.fullmatch(r"19[2-8][0-9]{2}", _ISOLATED_VECTOR_PORT)):
        raise RuntimeError("INVALID_ISOLATED_VECTOR_ENDPOINT")
    URL = "http://127.0.0.1:" + _ISOLATED_VECTOR_PORT
PAYLOAD_KEYS=frozenset(("tenant_id","document_id","chunk_id","acl_scope_id"))
TOKENS=re.compile(r"(?u)[^\W_][\w.\-]{1,63}")

def safe_text(value,limit=250):
    if not isinstance(value,str) or not 1<=len(value.strip())<=limit:
        raise InvalidRequest("INVALID_QUERY")
    return value.strip()

def sparse_tokens(value):
    raw=[x.lower().rstrip(".-") for x in TOKENS.findall(value.lower())]
    words=[x for x in raw if len(x)>=2][:4096]
    counts=Counter(words)
    hashed={}
    for term,freq in counts.items():
        key=int.from_bytes(hashlib.blake2b(term.encode("utf-8"),
             digest_size=4,person=b"v06sparse").digest(),"big") & 0x7fffffff
        hashed[key]=hashed.get(key,0.0)+float(freq)
    if not hashed:
        # Qdrant disallows some empty sparse vectors; no sparse candidates.
        return None
    idx=sorted(hashed)
    return models.SparseVector(indices=idx,values=[hashed[i] for i in idx])

class LocalE5:
    def __init__(self):
        if not (MODEL_PATH/"model.safetensors").is_file():
            raise RuntimeError("PINNED_LOCAL_EMBEDDING_MODEL_MISSING")
        os.environ["HF_HUB_OFFLINE"]="1"
        os.environ["TRANSFORMERS_OFFLINE"]="1"
        from sentence_transformers import SentenceTransformer
        self.model=SentenceTransformer(str(MODEL_PATH),device="cpu",
            local_files_only=True,trust_remote_code=False)
        size=self.model.get_sentence_embedding_dimension()
        if size!=VECTOR_SIZE:raise RuntimeError("UNEXPECTED_LOCAL_MODEL_DIMENSION")

    def embed(self,texts,query=False):
        prefix="query: " if query else "passage: "
        texts=[prefix+safe_text(t,15000) for t in texts]
        arr=self.model.encode(texts,batch_size=8,normalize_embeddings=True,
            convert_to_numpy=True,show_progress_bar=False)
        if len(arr)!=len(texts):raise RuntimeError("EMBEDDING_BATCH_SIZE_MISMATCH")
        result=[]
        for row in arr:
            vector=[float(v) for v in row]
            if len(vector)!=VECTOR_SIZE or not all(math.isfinite(x) for x in vector):
                raise RuntimeError("NONFINITE_OR_INVALID_EMBEDDING")
            result.append(vector)
        return result

def qdrant_client(timeout=10):
    settings = dict(url=URL, prefer_grpc=False, timeout=timeout, trust_env=False,
                    check_compatibility=False)
    if _ISOLATED_VECTOR_PORT is not None:
        return QdrantClient(**settings)
    token=APIKEY.read_text(encoding="ascii")
    if len(token)<40:raise RuntimeError("QDRANT_API_KEY_TOO_SHORT")
    return QdrantClient(api_key=token, **settings)

def ensure_collection(client):
    if not client.collection_exists(COLLECTION):
        client.create_collection(collection_name=COLLECTION,
            vectors_config={"dense":models.VectorParams(size=VECTOR_SIZE,
                distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse":models.SparseVectorParams(
                modifier=models.Modifier.IDF)})
        for key in ("tenant_id","acl_scope_id","document_id"):
            client.create_payload_index(COLLECTION,key,models.PayloadSchemaType.KEYWORD,
                                        wait=True)
    info=client.get_collection(COLLECTION)
    vectors=info.config.params.vectors
    if "dense" not in vectors or vectors["dense"].size!=VECTOR_SIZE:
        raise RuntimeError("QDRANT_COLLECTION_VECTOR_SCHEMA_MISMATCH")
    if "sparse" not in (info.config.params.sparse_vectors or {}):
        raise RuntimeError("QDRANT_COLLECTION_SPARSE_SCHEMA_MISMATCH")
    return info

def index_demo_corpus(encoder,client):
    """Explicit administrative job. Call only against synthetic P13 demo data."""
    ensure_collection(client)
    with database_session(admin=True) as con:
        rows=con.execute("""SELECT c.id,c.document_id,c.body,d.scope_id
             FROM core.document_chunk c
             JOIN core.document d ON d.id=c.document_id
             WHERE d.source_kind='p13-demo' AND d.deleted_at IS NULL
             ORDER BY c.id""").fetchall()
    if not rows or len(rows)>5000:raise RuntimeError("UNEXPECTED_PILOT_INDEX_SIZE")
    vectors=encoder.embed([r[2] for r in rows])
    points=[]
    for r,dense in zip(rows,vectors):
        cid,did,body,scope=r
        point_id=str(uuid.uuid5(NAMESPACE,cid))
        sparse=sparse_tokens(body)
        if sparse is None:raise RuntimeError("EMPTY_SOURCE_CHUNK")
        payload={"tenant_id":TENANT,"document_id":did,
                 "chunk_id":cid,"acl_scope_id":scope}
        assert PAYLOAD_KEYS==frozenset(payload)
        points.append(models.PointStruct(id=point_id,
            vector={"dense":dense,"sparse":sparse},
            payload=payload))
    # Replacing point IDs is idempotent; stale points are never treated as
    # authorized. Full corpus reconciliation/deletion is a later milestone.
    for start in range(0,len(points),20):
        client.upsert(COLLECTION,points[start:start+20],wait=True)
    stats=client.count(COLLECTION,exact=True).count
    if stats<len(points):raise RuntimeError("QDRANT_UPSERT_INCOMPLETE")
    return {"indexed":len(points),"collection_points":stats,
            "payload_keys":sorted(PAYLOAD_KEYS),
            "model":"intfloat/multilingual-e5-small",
            "source":"real PostgreSQL demo"}

def rrf(rankings,limit):
    scores={}
    for source,ids in rankings.items():
        seen=set()
        for i,cid in enumerate(ids):
            if cid in seen:continue
            seen.add(cid)
            scores[cid]=scores.get(cid,0.0)+1.0/(60+i+1)
    return sorted(scores,key=lambda x:(-scores[x],x))[:limit*5]

@dataclass(frozen=True)
class Hit:
    chunk_id:str
    document_id:str
    title:str
    text:str
    page_no:int|None
    revision:str
    score:float
    extraction_method:str="text"
    designation:str=""
    doc_type:str="internal"

class PolicyHybridRetriever:
    def __init__(self,encoder,client):
        self.encoder=encoder
        self.client=client

    def collect_vector_candidates(self,allowed_scopes,query,limit=20):
        if not allowed_scopes:return {"dense":[],"sparse":[]}
        filter_=models.Filter(must=[
            models.FieldCondition(key="tenant_id",match=models.MatchValue(value=TENANT)),
            models.FieldCondition(key="acl_scope_id",
                match=models.MatchAny(any=sorted(allowed_scopes)))])
        output={}
        vector=self.encoder.embed([query],query=True)[0]
        sparse=sparse_tokens(query)
        requests=[("dense",vector),("sparse",sparse)]
        for using,q in requests:
            if q is None:output[using]=[];continue
            res=self.client.query_points(collection_name=COLLECTION,
                query=q,using=using,query_filter=filter_,
                limit=limit,with_payload=True,with_vectors=False)
            ids=[]
            for point in res.points:
                payload=point.payload or {}
                # A malicious index may return any IDs; hydration never trusts it.
                if payload.get("tenant_id")!=TENANT:continue
                cid=payload.get("chunk_id")
                if isinstance(cid,str) and 0<len(cid)<=128:ids.append(cid)
            output[using]=ids
        return output

    def search(self,verified_actor,query,limit=10,oidc_issuer=None,oidc_sub=None):
        """Actor must be supplied by verified server-side Keycloak OIDC mapping."""
        from core06.db_client import _subject,_limit
        actor=_subject(verified_actor)
        q=safe_text(query)
        lim=_limit(limit)
        with database_session() as db:
            with db.transaction():
                db.execute("SELECT set_config('factory.actor_id',%s,true)",(actor,))
                scope_rows=db.execute("SELECT DISTINCT scope_id FROM core.document").fetchall()
                allowed={r[0] for r in scope_rows}
        if not allowed:return []
        # Both candidates and lexical search may be stale; hydration below is
        # a NEW PostgreSQL READ COMMITTED transaction at the moment of use.
        vector_rankings=self.collect_vector_candidates(allowed,q,limit=min(50,lim*5))
        with database_session() as db:
            with db.transaction():
                db.execute("SELECT set_config('factory.actor_id',%s,true)",(actor,))
                if (oidc_issuer is None)!=(oidc_sub is None):
                    raise InvalidRequest("INCOMPLETE_OIDC_CONTEXT")
                if oidc_issuer is not None:
                    # Recheck mapping at final hydration, not only before
                    # the vector search (identity may be disabled meanwhile).
                    row=db.execute("SELECT core.resolve_oidc_subject(%s,%s)",
                                   (oidc_issuer,oidc_sub)).fetchone()
                    if not row or row[0]!=actor:return []
                lexical=db.execute("""SELECT id FROM core.document_chunk
                       WHERE search_vector @@ plainto_tsquery('simple',%s)
                       ORDER BY ts_rank_cd(search_vector,plainto_tsquery('simple',%s)) DESC,id
                       LIMIT %s""",(q,q,lim*5)).fetchall()
                rankings={**vector_rankings,"lexical":[r[0] for r in lexical]}
                candidates=rrf(rankings,lim)
                if not candidates:return []
                rows=db.execute("""SELECT c.id,c.document_id,c.body,c.page_no,
                                      d.title,d.revision,c.extraction_method,
                                      d.designation,d.doc_type
                       FROM core.document_chunk c
                       JOIN core.document d ON d.id=c.document_id
                       WHERE c.id=ANY(%s)""",(candidates,)).fetchall()
                authorized={r[0]:r for r in rows}
                return [Hit(r[0],r[1],r[4],r[2],r[3],r[5],
                            1.0/(1+candidates.index(cid)),
                            "ocr" if r[6]=="ocr" else "text",
                            r[7] or "",r[8] or "internal")
                        for cid in candidates if (r:=authorized.get(cid)) is not None][:lim]
