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
from core06.text_grounding import (
    build_pg_tsqueries,
    contains_number,
    evaluate_identifier_match,
    extract_identifiers,
    identifier_key,
    non_identifier_tokens,
    standalone_numbers,
)

DENSE_SCORE_THRESHOLD=0.82
DENSE_ONLY_MIN_SCORE=0.85

_STAGE_COLLECTION=os.environ.get("FACTORY_V06_STAGING_COLLECTION")
if _STAGE_COLLECTION is not None:
    if os.environ.get("FACTORY_V06_UNIFIED_STAGING")!="1" or not re.fullmatch(
        r"factory_ingest_stage_[0-9a-f]{12}",_STAGE_COLLECTION):
        raise RuntimeError("INVALID_ISOLATED_STAGING_COLLECTION")
COLLECTION=_STAGE_COLLECTION or "factory_v06_demo_hybrid"
TENANT="synthetic-demo"
VECTOR_SIZE=384
NAMESPACE=uuid.UUID("06fef1ba-e774-4c30-ac9d-cc66b0e6f896")
_DEFAULT_MODEL_PATH=(
    Path.home()/".cache"/"huggingface"/"hub"/
    "models--intfloat--multilingual-e5-small"/"snapshots"/
    "614241f622f53c4eeff9890bdc4f31cfecc418b3"
)
MODEL_PATH=Path(os.environ.get("FACTORY_EMBEDDING_MODEL_PATH") or str(_DEFAULT_MODEL_PATH))
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

def indexable_text(body:str,title:str="",designation:str="",limit:int=15000)->str:
    """Combine document title, designation and chunk body for dense/sparse indexing."""
    parts=[]
    t=(title or "").strip()
    d=(designation or "").strip()
    if t:parts.append(t)
    if d and d not in t:parts.append(d)
    parts.append(body)
    return "\n".join(parts)[:limit]

def index_demo_corpus(encoder,client):
    """Explicit administrative job. Call only against synthetic P13 demo data."""
    ensure_collection(client)
    with database_session(admin=True) as con:
        rows=con.execute("""SELECT c.id,c.document_id,c.body,d.scope_id,d.title,d.designation
             FROM core.document_chunk c
             JOIN core.document d ON d.id=c.document_id
             WHERE d.source_kind='p13-demo' AND d.deleted_at IS NULL
             ORDER BY c.id""").fetchall()
    if not rows or len(rows)>5000:raise RuntimeError("UNEXPECTED_PILOT_INDEX_SIZE")
    indexed_texts=[indexable_text(r[2],r[4] or "",r[5] or "") for r in rows]
    vectors=encoder.embed(indexed_texts)
    points=[]
    for r,text_for_index,dense in zip(rows,indexed_texts,vectors):
        cid,did,_body,scope,_title,_designation=r
        point_id=str(uuid.uuid5(NAMESPACE,cid))
        sparse=sparse_tokens(text_for_index)
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

def rrf_scores(rankings):
    scores={}
    for _source,ids in rankings.items():
        seen=set()
        for i,cid in enumerate(ids):
            if cid in seen:continue
            seen.add(cid)
            scores[cid]=scores.get(cid,0.0)+1.0/(60+i+1)
    return scores

def rrf(rankings,limit):
    scores=rrf_scores(rankings)
    return sorted(scores,key=lambda x:(-scores[x],x))[:limit*5]

def _lexical_candidates(db,query,limit):
    """RLS-scoped Russian + simple weighted lexical search over title, designation, body."""
    ru_tsq,simple_tsq=build_pg_tsqueries(query)
    if not ru_tsq and not simple_tsq:
        return []
    if ru_tsq and simple_tsq:
        return db.execute("""SELECT c.id,
                   ts_rank_cd(
                     setweight(to_tsvector('russian',d.title||' '||d.designation),'A') ||
                     setweight(to_tsvector('russian',c.body),'B'),
                     to_tsquery('russian',%s)
                   ) +
                   ts_rank_cd(
                     setweight(to_tsvector('simple',translate(d.title||' '||d.designation,'.-','  ')),'A') ||
                     setweight(to_tsvector('simple',translate(c.body,'.-','  ')),'B'),
                     to_tsquery('simple',%s)
                   ) AS rank
               FROM core.document_chunk c
               JOIN core.document d ON d.id=c.document_id
               WHERE to_tsvector('russian'::regconfig,c.body) @@ to_tsquery('russian',%s)
                  OR to_tsvector('russian'::regconfig,d.title||' '||d.designation) @@ to_tsquery('russian',%s)
                  OR to_tsvector('simple'::regconfig,translate(c.body,'.-','  ')) @@ to_tsquery('simple',%s)
                  OR to_tsvector('simple'::regconfig,translate(d.title||' '||d.designation,'.-','  ')) @@ to_tsquery('simple',%s)
               ORDER BY rank DESC,c.id LIMIT %s""",
               (ru_tsq,simple_tsq,ru_tsq,ru_tsq,simple_tsq,simple_tsq,limit)).fetchall()
    if ru_tsq:
        return db.execute("""SELECT c.id,
                   ts_rank_cd(
                     setweight(to_tsvector('russian',d.title||' '||d.designation),'A') ||
                     setweight(to_tsvector('russian',c.body),'B'),
                     to_tsquery('russian',%s)
                   ) AS rank
               FROM core.document_chunk c
               JOIN core.document d ON d.id=c.document_id
               WHERE to_tsvector('russian'::regconfig,c.body) @@ to_tsquery('russian',%s)
                  OR to_tsvector('russian'::regconfig,d.title||' '||d.designation) @@ to_tsquery('russian',%s)
               ORDER BY rank DESC,c.id LIMIT %s""",
               (ru_tsq,ru_tsq,ru_tsq,limit)).fetchall()
    return db.execute("""SELECT c.id,
               ts_rank_cd(
                 setweight(to_tsvector('simple',translate(d.title||' '||d.designation,'.-','  ')),'A') ||
                 setweight(to_tsvector('simple',translate(c.body,'.-','  ')),'B'),
                 to_tsquery('simple',%s)
               ) AS rank
           FROM core.document_chunk c
           JOIN core.document d ON d.id=c.document_id
           WHERE to_tsvector('simple'::regconfig,translate(c.body,'.-','  ')) @@ to_tsquery('simple',%s)
              OR to_tsvector('simple'::regconfig,translate(d.title||' '||d.designation,'.-','  ')) @@ to_tsquery('simple',%s)
           ORDER BY rank DESC,c.id LIMIT %s""",
           (simple_tsq,simple_tsq,simple_tsq,limit)).fetchall()

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
    identifier_match:str|None=None

class PolicyHybridRetriever:
    def __init__(self,encoder,client,dense_score_threshold=DENSE_SCORE_THRESHOLD):
        self.encoder=encoder
        self.client=client
        self.dense_score_threshold=dense_score_threshold

    def _collect_vector_candidates_scored(self,allowed_scopes,query,limit=20):
        if not allowed_scopes:
            return {"dense":[],"sparse":[]},{"dense":{},"sparse":{}}
        filter_=models.Filter(must=[
            models.FieldCondition(key="tenant_id",match=models.MatchValue(value=TENANT)),
            models.FieldCondition(key="acl_scope_id",
                match=models.MatchAny(any=sorted(allowed_scopes)))])
        output={}
        scores={}
        vector=self.encoder.embed([query],query=True)[0]
        sparse=sparse_tokens(query)
        requests=[("dense",vector),("sparse",sparse)]
        for using,q in requests:
            if q is None:
                output[using]=[];scores[using]={};continue
            kwargs=dict(collection_name=COLLECTION,query=q,using=using,
                        query_filter=filter_,limit=limit,
                        with_payload=True,with_vectors=False)
            if using=="dense" and self.dense_score_threshold is not None:
                kwargs["score_threshold"]=self.dense_score_threshold
            res=self.client.query_points(**kwargs)
            ids=[]
            src_scores={}
            for point in res.points:
                payload=point.payload or {}
                # A malicious index may return any IDs; hydration never trusts it.
                if payload.get("tenant_id")!=TENANT:continue
                cid=payload.get("chunk_id")
                pt_score=float(getattr(point,"score",0.0) or 0.0)
                if using=="dense" and self.dense_score_threshold is not None and pt_score<self.dense_score_threshold:
                    continue
                if isinstance(cid,str) and 0<len(cid)<=128:
                    ids.append(cid)
                    if cid not in src_scores or pt_score>src_scores[cid]:
                        src_scores[cid]=pt_score
            output[using]=ids
            scores[using]=src_scores
        return output,scores

    def collect_vector_candidates(self,allowed_scopes,query,limit=20):
        rankings,_scores=self._collect_vector_candidates_scored(allowed_scopes,query,limit=limit)
        return rankings

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
        vector_rankings,vector_scores=self._collect_vector_candidates_scored(
            allowed,q,limit=min(50,lim*5))
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
                lexical_rows=_lexical_candidates(db,q,lim*5)
                lexical_ids=[r[0] for r in lexical_rows]
                lexical_scores={r[0]:float(r[1] or 0.0) for r in lexical_rows}
                rankings={**vector_rankings,"lexical":lexical_ids}
                rrf_map=rrf_scores(rankings)
                candidates=sorted(rrf_map,key=lambda x:(-rrf_map[x],x))[:lim*5]
                if not candidates:return []
                rows=db.execute("""SELECT c.id,c.document_id,c.body,c.page_no,
                                      d.title,d.revision,c.extraction_method,
                                      d.designation,d.doc_type
                       FROM core.document_chunk c
                       JOIN core.document d ON d.id=c.document_id
                       WHERE c.id=ANY(%s)""",(candidates,)).fetchall()
                authorized={r[0]:r for r in rows}
                q_ids=extract_identifiers(q)
                required_keys={identifier_key(c) for c in q_ids}
                num_tokens=standalone_numbers(q)
                context_tokens=non_identifier_tokens(q)
                dense_map=vector_scores.get("dense",{})
                sparse_map=vector_scores.get("sparse",{})
                max_rrf=3.0/61.0
                covered_keys=set()
                scored_hits=[]
                for cid in candidates:
                    r=authorized.get(cid)
                    if r is None:continue
                    body=r[2]
                    title=r[4] or ""
                    designation=r[7] or ""
                    method="ocr" if r[6]=="ocr" else "text"
                    full_norm=f"{title} {designation} {body}".casefold().replace("ё","е")
                    if num_tokens and any(not contains_number(full_norm,num) for num in num_tokens):
                        continue
                    eval_id=evaluate_identifier_match(q,title,designation,body,method)
                    if required_keys:
                        if not eval_id.matched_keys:
                            continue
                        covered_keys.update(eval_id.matched_keys)
                    else:
                        dense_s=dense_map.get(cid,0.0)
                        has_lex_or_sparse=(cid in lexical_scores) or (cid in sparse_map)
                        if not has_lex_or_sparse and dense_s<DENSE_ONLY_MIN_SCORE:
                            continue
                    dense_s=dense_map.get(cid,0.0)
                    lex_s=lexical_scores.get(cid,0.0)
                    rrf_norm=min(1.0,rrf_map.get(cid,0.0)/max_rrf)
                    base_score=(0.45*rrf_norm
                                +0.35*max(0.0,min(1.0,dense_s))
                                +0.20*min(1.0,lex_s/0.5))
                    if required_keys:
                        cov_ratio=len(eval_id.matched_keys)/len(required_keys)
                        ctx_ratio=eval_id.corroborating_hits/max(1,len(context_tokens)) if context_tokens else 0.0
                        quality_bonus={"exact":0.04,"homoglyph_candidate":0.02,"ocr_fuzzy_candidate":0.01}[eval_id.match_type]
                        rank_score=(base_score
                                    +0.25*cov_ratio
                                    +0.15*len(eval_id.heading_keys)
                                    +0.22*ctx_ratio
                                    +quality_bonus
                                    -(0.30 if eval_id.sibling_variant_conflict else 0.0))
                    else:
                        ctx_ratio=eval_id.corroborating_hits/max(1,len(context_tokens)) if context_tokens else 0.0
                        rank_score=base_score+0.15*ctx_ratio
                    display_score=round(max(0.0001,min(1.0,rank_score)),4)
                    hit=Hit(r[0],r[1],title,body,r[3],r[5],display_score,
                            method,designation,r[8] or "internal",eval_id.match_type)
                    scored_hits.append((rank_score,hit))
                if required_keys and not required_keys.issubset(covered_keys):
                    return []
                scored_hits.sort(key=lambda item:(-item[0],item[1].document_id,item[1].chunk_id))
                return [hit for _rs,hit in scored_hits[:lim]]
