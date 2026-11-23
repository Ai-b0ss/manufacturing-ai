"""Extract visible text evidence from a separate, copyrighted historical change notice.

The PDF text layer contains garbled degree symbols and glyph substitutions.
Neither an OCR guess nor the reference metadata counts as verification of the
changed numerical dimension or applicability date.
"""
from __future__ import annotations
import json
import pathlib
import re
import sys

def inspect(filename):
    import fitz
    path=pathlib.Path(filename)
    if not path.is_file() or path.stat().st_size>10_000_000:
        raise ValueError("Reference missing or exceeds 10 MB")
    with fitz.open(path) as pdf:
        if not pdf or len(pdf)>200 or pdf.needs_pass:
            raise ValueError("Invalid historical PDF")
        page_no=len(pdf)
        text=pdf[-1].get_text("text",sort=True)
        normalized=" ".join(text.split())
        has_notice=("ИЗВЕЩЕНИЕ ОБ ИЗМЕНЕНИИ" in normalized.upper()
                    or "ИЗВЕЩЕНИЕ" in normalized.upper())
        has_doc="СТО ЦКТИ 318.03-2009" in normalized
        has_id=bool(re.search(r"БВАИ\.\s*107-2012",normalized))
        match=re.search(r"угол.{0,140}",normalized,re.I)
        raw=match.group(0)[:170] if match else ""
        return {"state":"HISTORICAL_NOTICE_TEXT_LOCATED" if has_notice and has_doc and has_id else "NOT_FULLY_IDENTIFIED",
                "document_id":"СТО ЦКТИ 318.03-2009" if has_doc else None,
                "notice_id":"БВАИ.107-2012" if has_id else None,
                "page":page_no,
                "change_excerpt":raw,
                "numeric_change_status":"UNVERIFIED_TEXT_GLYPHS",
                "effective_date_status":"NOT_VERIFIED_IN_PDF",
                "document_rights":"THIRD_PARTY_REFERENCE_ONLY",
                "production_authorization":False,
                "warnings":["Garbling in the degree/tolerance expression requires visual review",
                            "No relationship to ArcBlue, BCN3D or Passport products established"]}
if __name__=="__main__":
    try:
        if len(sys.argv)!=2:raise ValueError("Provide PDF path")
        print('P8_RESULT_JSON:'+json.dumps(inspect(sys.argv[1]),ensure_ascii=True))
    except Exception as error:
        print('P8_RESULT_JSON:'+json.dumps({"error":type(error).__name__+": "+str(error)[:160]},ensure_ascii=True))
        sys.exit(2)
