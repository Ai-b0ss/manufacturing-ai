"""Protected glue from RLS-hydrated P3 hits to existing offline MiniCPM.

No direct Qdrant payload, demo SQLite, or caller-provided sources may enter the
model. The caller must re-check the *same* hits after generation before releasing
the answer because document ACL may change while llama.cpp is working.
"""
from __future__ import annotations

from typing import Iterable

MAX_EVIDENCE = 4

def _method(hit):
    return "ocr" if getattr(hit,"extraction_method","text")=="ocr" else "text"

def identity(hit):
    return (hit.chunk_id,hit.document_id,hit.title,hit.text,
            hit.page_no,hit.revision,_method(hit))

def evidence_from_hits(hits):
    return [{"body":hit.text[:700],"title":hit.title[:160],
             "page":hit.page_no or 1,"extraction_method":_method(hit)}
            for hit in list(hits)[:MAX_EVIDENCE]]

def source_cards(hits,citations=()):
    cited=set(citations)
    return [{"number":i,"doc_id":hit.document_id,
             "title":hit.title,"page":hit.page_no or 1,
             "excerpt":hit.text[:400],"extraction_method":_method(hit),
             "cited":i in cited}
            for i,hit in enumerate(list(hits)[:MAX_EVIDENCE],1)]

def unchanged_and_allowed(original,refreshed):
    """Fail closed on any removed/edited/revoked source or duplicate chunk IDs."""
    fresh={}
    for hit in refreshed:
        if hit.chunk_id in fresh:return False
        fresh[hit.chunk_id]=identity(hit)
    ids=[hit.chunk_id for hit in original]
    return bool(ids) and len(set(ids))==len(ids) and all(
        fresh.get(hit.chunk_id)==identity(hit) for hit in original)

def search_only_response(hits,reason):
    return {"answer":reason,"mode":"retrieval_only",
            "sources":source_cards(hits),"citation_warning":True,
            "verification_required":True}

def generated_response(hits,generation):
    valid=sorted({n for n in generation.get("citations",[])
                  if type(n) is int and 1<=n<=len(hits)})
    if not valid or not isinstance(generation.get("answer"),str):
        return None
    # The model's citation IDs are strictly bounded to the RLS-hydrated hits.
    return {"answer":generation["answer"],"mode":"local_llm_advisory",
            "sources":source_cards(hits,valid),"citations":valid,
            "citation_warning":False,
            "verification_required":True,
            "filtered_lines":int(generation.get("filtered_lines",0))}
