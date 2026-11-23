"""Bounded PDF callout extraction for review-only CAD/drawing number comparison.

Does not infer dimension type from nearby drawing geometry, infer tolerances,
recognize manufacturing approval, or read unknown user-selected filesystem paths.
"""
from __future__ import annotations
import hashlib
import json
import pathlib
import re
import sys

MAX_PDF_BYTES=9_000_000
MAX_PAGES=75
MAX_WORDS=20000
MAX_ANNOTATIONS=350
LINEAR=re.compile(r"^\d{1,4}(?:[.,]\d{1,3})?$")
RADIUS=re.compile(r"^[Rr]\d{1,4}(?:[.,]\d{1,3})?$")
ANGULAR=re.compile(r"^\d{1,3}(?:[.,]\d{1,3})?°$")
DIAMETER=re.compile(r"^[Ø⌀Φφ]\d{1,4}(?:[.,]\d{1,3})?$")
COMPOSITE=re.compile(r"^\d+(?:\.\d+)?[×x*]\d+(?:\.\d+)?(?:mm)?$",re.I)

def inspect(source_path):
    import fitz
    path=pathlib.Path(source_path)
    if not path.is_file() or path.stat().st_size>MAX_PDF_BYTES:
        raise ValueError("PDF absent or exceeds 9 MB limit")
    raw=path.read_bytes()
    if len(raw)>MAX_PDF_BYTES:
        raise ValueError("PDF too large")
    digest=hashlib.sha256(raw).hexdigest()
    annotations=[]
    with fitz.open(stream=raw,filetype="pdf") as document:
        if document.needs_pass or not len(document) or len(document)>MAX_PAGES:
            raise ValueError("Protected, empty or excessive PDF")
        for page_no,page in enumerate(document):
            if page.rect.width<=30 or page.rect.height<=30:
                raise ValueError("Invalid page geometry")
            text=page.get_text("text",sort=True)
            joined=re.sub(r"\s+","",text).casefold()
            # One clear global note is the only reason we can label all bare
            # dimension callouts as millimetres in this drawing family.
            units="mm" if ("尺寸使用毫米" in joined
                           or "dimensionsinmillimetres" in joined
                           or "allmeasurementsinmm" in joined) else "UNVERIFIED"
            words=page.get_text("words",sort=True)
            if len(words)>MAX_WORDS:
                raise ValueError("Too many PDF tokens")
            for word in words:
                x0,y0,x1,y1,token=word[:5]
                token=token.strip()
                # No title blocks, coordinate-grid letters, side labels
                # or paper-format tokens. Only dimensions in drawing region.
                if not (.035*page.rect.width<=x0<=.96*page.rect.width and
                        .035*page.rect.height<=y0<=.77*page.rect.height):
                    continue
                kind=None
                if LINEAR.fullmatch(token): kind="LINEAR"
                elif RADIUS.fullmatch(token):kind="RADIUS"
                elif ANGULAR.fullmatch(token):kind="ANGULAR"
                elif DIAMETER.fullmatch(token):kind="DIAMETER"
                elif COMPOSITE.fullmatch(token):kind="COMPOSITE"
                if kind is None:continue
                if kind=="COMPOSITE":
                    nominal=None
                else:
                    number=re.search(r"\d+(?:[.,]\d+)?",token)
                    nominal=float(number.group(0).replace(",",".")) if number else None
                if nominal is not None and not(0<nominal<10000):
                    continue
                box=[round(coord,6) for coord in (
                    x0/page.rect.width,y0/page.rect.height,
                    x1/page.rect.width,y1/page.rect.height)]
                annotation_id=hashlib.sha256(
                    f"{digest}|{page_no+1}|{token}|{box}".encode("utf-8")
                ).hexdigest()[:24]
                annotations.append({
                    "annotation_id":annotation_id,
                    "page":page_no+1,
                    "label":token,
                    "kind":kind,
                    "nominal":nominal,
                    "unit":units if kind!="ANGULAR" else "degree",
                    "bbox":box,
                    "basis":"PDF_TEXT_LAYER_WORD_BOX",
                    "tolerance_status":"NOT_ESTABLISHED",
                    "feature_mapping_status":"NOT_ESTABLISHED"
                })
                if len(annotations)>MAX_ANNOTATIONS:
                    raise ValueError("Too many candidate annotations")
    return {
        "state":"CANDIDATE_CALLOUTS_ONLY",
        "source_sha256":digest,
        "annotations":annotations,
        "count":len(annotations),
        "manufacturing_authorization":False,
        "warnings":[
            "Bare numbers may refer to local features, holes, radii, angles or section positions, not CAD bounding boxes",
            "A numeric coincidence is not a validated feature mapping",
            "Unspecified drawing tolerances, revisions and signatures are not evaluated"
        ]
    }

if __name__=="__main__":
    try:
        if len(sys.argv)!=2:raise ValueError("PDF path required")
        print("P9_RESULT_JSON:"+json.dumps(inspect(sys.argv[1]),ensure_ascii=True,allow_nan=False))
    except Exception as error:
        print("P9_RESULT_JSON:"+json.dumps({"error":str(error)[:180]},ensure_ascii=True))
        sys.exit(2)
