"""Policy-safe local model adapter & hybrid retrieval D1-D5 regressions."""
from contextlib import contextmanager
import math
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core06 import hybrid_retrieval as hr
from core06.hybrid_retrieval import Hit
from core06.unified_ai import (
    evidence_from_hits,source_cards,unchanged_and_allowed,generated_response,search_only_response
)

def hit(cid="c1",did="d1",text="allowed text",revision="A",title="Allowed title",
        method="text",designation="",identifier_match=None):
    return Hit(cid,did,title,text,1,revision,0.9,method,designation,"internal",identifier_match)


class _InMemoryQdrant:
    """In-memory cosine dense + IDF sparse stub honoring score_threshold."""
    def __init__(self):
        self.points={}
        self.last_dense_threshold=None

    def collection_exists(self,name):return True
    def get_collection(self,name):
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(
            vectors={"dense":SimpleNamespace(size=hr.VECTOR_SIZE)},
            sparse_vectors={"sparse":{}},
        )))
    def upsert(self,collection_name,points,wait=True):
        for p in points:self.points[p.id]=p
    def count(self,collection_name,exact=True):
        return SimpleNamespace(count=len(self.points))
    def _idf(self,idx):
        n=max(1,len(self.points))
        df=sum(1 for p in self.points.values() if idx in p.vector["sparse"].indices)
        return math.log((n-df+0.5)/(df+0.5)+1.0)
    def query_points(self,collection_name,query,using,query_filter=None,limit=10,
                     with_payload=True,with_vectors=False,score_threshold=None):
        if using=="dense":self.last_dense_threshold=score_threshold
        allowed=None
        if query_filter and getattr(query_filter,"must",None):
            for c in query_filter.must:
                if c.key=="acl_scope_id":allowed=set(c.match.any)
        scored=[]
        for p in self.points.values():
            if allowed is not None and p.payload.get("acl_scope_id") not in allowed:
                continue
            if using=="dense":
                s=sum(a*b for a,b in zip(query,p.vector["dense"]))
            else:
                qm=dict(zip(query.indices,query.values))
                pm=dict(zip(p.vector["sparse"].indices,p.vector["sparse"].values))
                s=sum(qv*pm[i]*self._idf(i) for i,qv in qm.items() if i in pm)
                if s<=0:continue
            if score_threshold is not None and s<score_threshold:
                continue
            scored.append((s,p))
        scored.sort(key=lambda x:(-x[0],x[1].id))
        return SimpleNamespace(points=[SimpleNamespace(id=p.id,score=s,payload=p.payload) for s,p in scored[:limit]])


class _FakePgSession:
    """In-memory RLS-aware PostgreSQL stub simulating Russian-stem + simple OR FTS."""
    def __init__(self,docs,grants):
        self.docs=docs  # list of dicts: chunk_id, doc_id, title, designation, body, page_no, revision, method, scope, doc_type
        self.grants=grants
        self.actor=None

    def __enter__(self):return self
    def __exit__(self,*a):pass
    @contextmanager
    def transaction(self):
        self.actor=None
        try:yield
        finally:self.actor=None
    def _visible(self):
        scopes=self.grants.get(self.actor,set())
        return [d for d in self.docs if d["scope"] in scopes]
    def _stem(self,tok):
        tok=tok.casefold().replace("ё","е")
        return tok[:5] if len(tok)>5 else tok
    def _match_tsq(self,text,tsq,use_stem):
        if not tsq:return 0.0
        norm=re.sub(r"[.\-]+"," ",text.casefold().replace("ё","е"))
        words=re.findall(r"[0-9a-zа-яё]+",norm)
        stems={self._stem(w) for w in words} if use_stem else set(words)
        score=0.0
        for branch in [b.strip() for b in tsq.split("|") if b.strip()]:
            if "<->" in branch:
                parts=[p.strip("() ") for p in branch.split("<->")]
                for i in range(len(words)-len(parts)+1):
                    if words[i:i+len(parts)]==parts:
                        score+=0.4
                        break
            else:
                term=self._stem(branch) if use_stem else branch
                if term in stems:
                    score+=0.2
        return score
    def execute(self,sql_text,params=()):
        self._rows=[]
        if "set_config('factory.actor_id'" in sql_text:
            self.actor=params[0]
        elif "SELECT DISTINCT scope_id FROM core.document" in sql_text:
            self._rows=[(s,) for s in sorted({d["scope"] for d in self._visible()})]
        elif "to_tsquery(" in sql_text:
            has_ru="to_tsquery('russian'" in sql_text
            has_s="to_tsquery('simple'" in sql_text
            ru_q=params[0] if has_ru else ""
            s_q=params[1] if (has_ru and has_s) else (params[0] if has_s else "")
            lim=params[-1]
            ranked=[]
            for d in self._visible():
                head=f"{d['title']} {d['designation']}"
                r=(self._match_tsq(head,ru_q,True)*2.0+self._match_tsq(d["body"],ru_q,True)
                   +self._match_tsq(head,s_q,False)*2.0+self._match_tsq(d["body"],s_q,False))
                if r>0:ranked.append((d["chunk_id"],r))
            ranked.sort(key=lambda x:(-x[1],x[0]))
            self._rows=ranked[:lim]
        elif "WHERE c.id=ANY(%s)" in sql_text:
            wanted=set(params[0])
            self._rows=[
                (d["chunk_id"],d["doc_id"],d["body"],d["page_no"],d["title"],
                 d["revision"],d["method"],d["designation"],d["doc_type"])
                for d in self._visible() if d["chunk_id"] in wanted
            ]
        return self
    def fetchall(self):return self._rows
    def fetchone(self):return self._rows[0] if self._rows else None


class UnifiedAITests(unittest.TestCase):
    def test_keeps_only_hydrated_evidence(self):
        a=[hit("c1","d1","A"*1500),hit("c2","d2","B"*1300)]
        evidence=evidence_from_hits(a)
        self.assertEqual(len(evidence),2)
        self.assertEqual(len(evidence[0]["body"]),700)
        self.assertEqual(len(source_cards(a)[0]["excerpt"]),400)
    def test_post_generation_policy_denies_changes_and_revocation(self):
        a=[hit("c1"),hit("c2","d2")]
        self.assertTrue(unchanged_and_allowed(a,list(reversed(a))))
        self.assertFalse(unchanged_and_allowed(a,[a[0]]))
        self.assertFalse(unchanged_and_allowed(a,[hit("c1",text="edited"),a[1]]))
        self.assertFalse(unchanged_and_allowed(a,[hit("c1",revision="B"),a[1]]))
        self.assertFalse(unchanged_and_allowed(a,[hit("c1"),hit("c1")]))
    def test_rejects_unverifiable_model_output(self):
        a=[hit()]
        self.assertIsNone(generated_response(a,{"answer":"claim","citations":[99]}))
        self.assertIsNone(generated_response(a,{"answer":"claim","citations":["1"]}))
        self.assertIsNone(generated_response(a,{"answer":"claim","citations":[]}))
    def test_source_numbering_is_authoritative(self):
        a=[hit("c1"),hit("c2","d2")]
        res=generated_response(a,{"answer":"Answer [2]","citations":[2,9],"filtered_lines":1})
        self.assertEqual(res["citations"],[2])
        self.assertEqual([x["cited"] for x in res["sources"]],[False,True])
        self.assertTrue(res["verification_required"])
        self.assertEqual(res["mode"],"local_llm_advisory")
    def test_fallback_never_claims_generation(self):
        res=search_only_response([hit()],"No model")
        self.assertEqual(res["mode"],"retrieval_only")
        self.assertTrue(res["citation_warning"])
    def test_d2_rejects_hallucinated_notice_on_public_doc(self):
        doc1=hit("c1","DOC-001","Деталь АБВГ.712345.004-02. Материал 12Х18Н10Т. Ревизия 01.",
                 title="Чертёж детали",designation="АБВГ.712345.004-02",identifier_match="exact")
        q="Что указано в извещении ИИ-017 для детали АБВГ.712345.004-02?"
        self.assertIsNone(generated_response([doc1],{"answer":"ИИ-017 требует уточнения [1]","citations":[1]},question=q))
    def test_d5_propagates_identifier_match_in_evidence_and_cards(self):
        h=hit("c1","DOC-001","Материал 12Х18Н10Т",identifier_match="homoglyph_candidate")
        self.assertEqual(evidence_from_hits([h])[0]["identifier_match"],"homoglyph_candidate")
        self.assertEqual(source_cards([h],[1])[0]["identifier_match"],"homoglyph_candidate")

    def test_hybrid_retriever_d1_to_d5_with_real_or_deterministic_encoder(self):
        docs=[
            {"chunk_id":"c-doc1","doc_id":"DOC-001","title":"Корпус насоса Н-100 — архивный чертёж детали",
             "designation":"АБВГ.712345.004-02","revision":"01","method":"text","page_no":1,"scope":"public","doc_type":"drawing",
             "body":"Деталь АБВГ.712345.004-02. Материал 12Х18Н10Т. Базовый чертёж ревизии 01 от 2024 года."},
            {"chunk_id":"c-doc3","doc_id":"DOC-003","title":"Извещение об изменении ИИ-017",
             "designation":"ИИ-017","revision":"02","method":"text","page_no":1,"scope":"engineering","doc_type":"change",
             "body":"ИИ-017 для АБВГ.712345.004-02: ревизия 02 с 01.01.2026. Для старого задела указание: доработать."},
            {"chunk_id":"c-doc4","doc_id":"DOC-004","title":"План-факт затрат по заказу З-104",
             "designation":"ФИН-З-104","revision":"2026-10","method":"text","page_no":1,"scope":"finance","doc_type":"finance",
             "body":"Заказ З-104. Плановая себестоимость 4 200 000 рублей, фактическая 4 675 000 рублей."},
            {"chunk_id":"c-doc6","doc_id":"DOC-006","title":"Станок: инструкция при перегреве шпинделя",
             "designation":"СТ-И-001","revision":"03","method":"text","page_no":1,"scope":"public","doc_type":"manual",
             "body":"При перегреве шпинделя остановить оборудование согласно инструкции и проверить смазку."},
            {"chunk_id":"c-ocr","doc_id":"OCR_PUMP","title":"Учебный паспорт насоса Н-125",
             "designation":"","revision":"01","method":"ocr","page_no":2,"scope":"engineering","doc_type":"uploaded",
             "body":"УЧЕБНЫЙ ПАСПОРТ ОБОРУДОВАНИЯ Насос Н-125, корпус АБВГ.712З45.ОО4-О2. Испытательное давление 25 бар. Температура 78 градусов. Марка материала 12Х18Н10Т. Перед пуском проверить фланец и смазку."},
            {"chunk_id":"c-n125m","doc_id":"SYNTH_N125M","title":"synth_n125m_rev02.txt",
             "designation":"","revision":"02","method":"text","page_no":1,"scope":"engineering","doc_type":"uploaded",
             "body":"Насос Н-125М, корпус АБВГ.712345.004-03, ревизия 02 от 15.02.2026. Испытательное давление 40 бар. Рабочая температура 95 градусов. Марка материала 08Х18Н10Т. Момент затяжки шпилек фланца 140 Нм. Не смешивать с базовым насосом Н-125 исполнения -02."},
        ]
        grants={"engineer":{"public","engineering"},"technologist":{"public"}}
        if (hr.MODEL_PATH/"model.safetensors").is_file():
            encoder=hr.LocalE5()
        else:
            class _ZeroEnc:
                def embed(self,texts,query=False):
                    return [[0.0]*hr.VECTOR_SIZE for _ in texts]
            encoder=_ZeroEnc()

        qdrant=_InMemoryQdrant()
        indexed=[hr.indexable_text(d["body"],d["title"],d["designation"]) for d in docs]
        vecs=encoder.embed(indexed)
        pts=[]
        for d,txt,dense in zip(docs,indexed,vecs):
            pts.append(SimpleNamespace(
                id=d["chunk_id"],
                vector={"dense":dense,"sparse":hr.sparse_tokens(txt)},
                payload={"tenant_id":hr.TENANT,"document_id":d["doc_id"],
                         "chunk_id":d["chunk_id"],"acl_scope_id":d["scope"]},
            ))
        qdrant.upsert(hr.COLLECTION,pts)

        retriever=hr.PolicyHybridRetriever(encoder,qdrant)
        with patch.object(hr,"database_session",lambda admin=False:_FakePgSession(docs,grants)):
            # D1: Q03 ranks OCR_PUMP #1, excludes SYNTH_N125M distractor
            r03=retriever.search("engineer","Какое испытательное давление и рабочая температура указаны в паспорте насоса Н-125?")
            self.assertEqual([h.document_id for h in r03],["OCR_PUMP"])
            self.assertEqual(r03[0].identifier_match,"exact")
            self.assertEqual(qdrant.last_dense_threshold,hr.DENSE_SCORE_THRESHOLD)

            # D1 positive control: Q04 retrieves SYNTH_N125M #1
            r04=retriever.search("engineer","Какое испытательное давление и момент затяжки фланца для насоса Н-125М (АБВГ.712345.004-03)?")
            self.assertEqual([h.document_id for h in r04],["SYNTH_N125M"])

            # D2 + D3: revoked/unauthorized/out-of-corpus queries return clean 0 hits
            self.assertEqual(retriever.search("technologist","Что указано в извещении ИИ-017 для детали АБВГ.712345.004-02?"),[])
            self.assertEqual(retriever.search("technologist","Какое испытательное давление у насоса Н-125 и какие требования в извещении ИИ-017?"),[])
            self.assertEqual(retriever.search("engineer","Какова плановая и фактическая себестоимость заказа З-104?"),[])
            self.assertEqual(retriever.search("engineer","Какой режим термообработки и предел прочности у сплава ВТ99-ЭКСТРА-777?"),[])
            self.assertEqual(retriever.search("engineer","КАЛИБРОКСЕНИУМ"),[])
            self.assertEqual(retriever.search("engineer","Какая погода завтра в Антарктиде?"),[])

            # D4: Q07 finds OCR_PUMP #1 with explicit ocr_fuzzy_candidate flag
            r07=retriever.search("engineer","Что нужно проверить перед пуском корпуса АБВГ.712345.004-02 согласно паспорту оборудования?")
            self.assertTrue(r07)
            self.assertEqual(r07[0].document_id,"OCR_PUMP")
            self.assertEqual(r07[0].extraction_method,"ocr")
            self.assertEqual(r07[0].identifier_match,"ocr_fuzzy_candidate")

            # D5: Q05 marks Latin homoglyph matches as homoglyph_candidate, excludes 08Х18Н10Т
            r05=retriever.search("engineer","В каких документах указан материал 12X18H10T?")
            self.assertEqual({h.document_id for h in r05},{"DOC-001","OCR_PUMP"})
            self.assertTrue(all(h.identifier_match=="homoglyph_candidate" for h in r05))

            # D3: Q12c returns real non-synthetic scores (not 1.0, 0.5)
            r12c=retriever.search("technologist","Что делать при перегреве шпинделя станка?")
            self.assertTrue(r12c)
            self.assertEqual(r12c[0].document_id,"DOC-006")
            self.assertGreater(r12c[0].score,0.0)
            self.assertLessEqual(r12c[0].score,1.0)
            if len(r12c)>1:
                self.assertNotEqual(r12c[1].score,0.5)

    def test_d1_indexers_include_title_and_designation_without_expanding_payload(self):
        from core06 import ingest_index_worker

        seen_texts=[]
        class _RecordingEncoder:
            def embed(self,texts,query=False):
                seen_texts.extend(texts)
                return [[1.0]+[0.0]*(hr.VECTOR_SIZE-1) for _ in texts]

        class _DemoDb:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def execute(self,sql_text,params=()):
                self._rows=[("c-1","DOC-001","Тело чанка","public","Учебный паспорт насоса Н-125","АБВГ.712345.004-02")]
                return self
            def fetchall(self):return self._rows

        qdrant=_InMemoryQdrant()
        with patch.object(hr,"database_session",lambda admin=True:_DemoDb()):
            res=hr.index_demo_corpus(_RecordingEncoder(),qdrant)
        self.assertEqual(res["indexed"],1)
        self.assertIn("Учебный паспорт насоса Н-125",seen_texts[0])
        self.assertIn("АБВГ.712345.004-02",seen_texts[0])
        self.assertEqual(frozenset(next(iter(qdrant.points.values())).payload),hr.PAYLOAD_KEYS)

        # Now verify ingest_index_worker._process_claimed also indexes title & designation
        worker_texts=[]
        class _WorkerEnc:
            def embed(self,texts,query=False):
                worker_texts.extend(texts)
                return [[1.0]+[0.0]*(hr.VECTOR_SIZE-1) for _ in texts]

        class _WorkerDb:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def execute(self,sql_text,params=()):
                self._sql=sql_text
                return self
            def fetchone(self):
                if "title,designation" in self._sql:
                    return ("engineering",None,"Учебный паспорт насоса Н-125","Н-125")
                return ("engineering",None)
            def fetchall(self):
                return [("u-1-c1","Испытательное давление 25 бар.")]

        qdrant2=_InMemoryQdrant()
        with patch.object(ingest_index_worker,"_mark",return_value=True):
            outcome=ingest_index_worker._process_claimed(
                (1,"upload-"+"a"*32,"index","lease-1"),
                lambda admin=True:_WorkerDb(),
                _WorkerEnc(),
                qdrant2,
            )
        self.assertEqual(outcome.result,"indexed")
        self.assertIn("Учебный паспорт насоса Н-125",worker_texts[0])
        self.assertIn("Н-125",worker_texts[0])
        self.assertEqual(frozenset(next(iter(qdrant2.points.values())).payload),hr.PAYLOAD_KEYS)

    def test_d2_d5_unified_ask_http_blocks_hallucination_and_propagates_flags(self):
        import time, uuid
        from fastapi.testclient import TestClient
        import ai_extension
        import p2_gateway.app as p2module
        from p2_gateway.security import Principal
        import unified_app

        origin="http://localhost:19139"
        cookie_name="factory_unified_session"
        cookie="d2-d5-test-"+uuid.uuid4().hex
        p2module._SESSIONS[cookie]=p2module.WebSession("mock-jwt",int(time.time())+300,"csrf-d2","id-d2")
        fake_principal=Principal(p2module.ISSUER,"sub-d2-d5-1234",p2module.WEB_CLIENT,int(time.time())+300)

        doc1_row=("c-doc1","DOC-001","Корпус насоса Н-100","Деталь АБВГ.712345.004-02. Материал 12Х18Н10Т. Ревизия 01.",
                  1,"01","text","АБВГ.712345.004-02","drawing")

        class _PgRecheck:
            def execute(self,q,p=()):
                self._q=q
                return self
            def fetchall(self):
                return [doc1_row] if "FROM core.document_chunk" in self._q else []

        @contextmanager
        def fake_actor_db(_principal):
            yield _PgRecheck(),"engineer"

        class _StubRetriever:
            def __init__(self,id_match):
                self.id_match=id_match
            def search(self,actor,question,limit=8,**kw):
                return [Hit("c-doc1","DOC-001","Корпус насоса Н-100",
                            "Деталь АБВГ.712345.004-02. Материал 12Х18Н10Т. Ревизия 01.",
                            1,"01",0.87,"text","АБВГ.712345.004-02","drawing",self.id_match)]

        try:
            with (
                patch.object(p2module,"APP_ORIGIN",origin),
                patch.object(unified_app,"APP_ORIGIN",origin),
                patch.object(p2module,"COOKIE_SESSION",cookie_name),
                patch.object(p2module,"verify_access_token",return_value=fake_principal),
                patch.object(unified_app,"_actor_db",fake_actor_db),
                patch.object(ai_extension,"status",return_value={"status":"ready"}),
            ):
                with TestClient(unified_app.app,base_url=origin,raise_server_exceptions=False) as client:
                    client.cookies.set(cookie_name,cookie)
                    hdrs={"Origin":origin,"X-CSRF-Token":"csrf-d2"}
                    # D2: Hallucinated ИИ-017 citation on DOC-001 must degrade to retrieval_only
                    with (
                        patch.object(unified_app,"_p3_retriever",return_value=_StubRetriever("exact")),
                        patch.object(ai_extension,"generate",return_value={
                            "answer":"Извещение ИИ-017 указано в тексте [1].","citations":[1],"filtered_lines":0
                        }),
                    ):
                        r=client.post("/unified/api/ask",
                            json={"question":"Что указано в извещении ИИ-017 для детали АБВГ.712345.004-02?"},
                            headers=hdrs)
                    self.assertEqual(r.status_code,200)
                    data=r.json()
                    self.assertEqual(data["mode"],"retrieval_only")
                    self.assertTrue(data["citation_warning"])
                    self.assertNotIn("ИИ-017",data["answer"])

                    # D5: Homoglyph candidate flag propagates to /unified/api/ask JSON sources
                    with (
                        patch.object(unified_app,"_p3_retriever",return_value=_StubRetriever("homoglyph_candidate")),
                        patch.object(ai_extension,"generate",return_value={
                            "answer":"В документе [1] указан материал 12X18H10T.","citations":[1],"filtered_lines":0
                        }),
                    ):
                        r2=client.post("/unified/api/ask",
                            json={"question":"В каких документах указан материал 12X18H10T?"},
                            headers=hdrs)
                    self.assertEqual(r2.status_code,200)
                    data2=r2.json()
                    self.assertEqual(data2["mode"],"local_llm_advisory")
                    self.assertEqual(data2["sources"][0]["identifier_match"],"homoglyph_candidate")
        finally:
            p2module._SESSIONS.pop(cookie,None)

if __name__=="__main__":unittest.main()
