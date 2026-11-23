"""Isolated, bounded extraction of drawing title-block fields from real PDFs.

The tool detects *candidates* with page coordinates; it does not apply GOST,
authenticate signatures, determine released/current revisions or approve manufacture.
"""
from __future__ import annotations
import json
import pathlib
import re
import sys
import tempfile

MAX_BYTES = 9_000_000
MAX_PAGES = 80
MAX_WORDS = 18000

def short_box(word, page):
    x0,y0,x1,y1 = word[:4]
    return [round(x0/page.rect.width,4),round(y0/page.rect.height,4),
            round(x1/page.rect.width,4),round(y1/page.rect.height,4)]

def record(name, value, word, page, reason):
    return {"field":name,"value":value,"page":1,"bbox":short_box(word,page),
            "method":"PDF_TEXT_COORDINATES","rule":reason}

def inspect(path, group, *, try_ocr=False):
    import fitz
    file=pathlib.Path(path)
    if not file.is_file() or file.stat().st_size>MAX_BYTES:
        raise ValueError("PDF is missing or too large")
    result={"format":"UNKNOWN","designation":None,"revision":None,
            "material_grade":None,"revision_history":[],"evidence":[],
            "warnings":[],"recognition":"NOT_CONFIRMED","ocr_candidate":None}
    with fitz.open(file) as pdf:
        if pdf.needs_pass or not len(pdf) or len(pdf)>MAX_PAGES:
            raise ValueError("PDF is password protected, empty or too long")
        page=pdf[0]
        if page.rect.width<50 or page.rect.height<50:
            raise ValueError("Invalid PDF page dimensions")
        words=page.get_text("words",sort=True)
        if len(words)>MAX_WORDS:
            raise ValueError("Page has too many extracted tokens")
        text=page.get_text("text",sort=True)
        result["page_count"]=len(pdf)
        result["text_layer"]=bool(text.strip())
        w,h=page.rect.width,page.rect.height
        if group=="Passport_Assembly":
            result["format"]="PASSPORT_TITLE_BLOCK"
            # The actual document number and revision are printed together in
            # the lower-right title block, not assumed from the filename.
            ids=[tok for tok in words if re.fullmatch(r"\d{7}",tok[4])
                 and tok[1]>0.84*h and tok[0]>0.70*w]
            for number in ids:
                revisions=[tok for tok in words if re.fullmatch(r"[A-Z]",tok[4])
                           and tok[0]>number[2]+15 and tok[0]>.92*w
                           and abs(tok[1]-number[1])<9]
                if len(revisions)==1:
                    revision=revisions[0]
                    result["designation"]=number[4]
                    result["revision"]=revision[4]
                    result["evidence"].extend([
                        record("designation",number[4],number,page,"bottom-right drawing number field"),
                        record("revision",revision[4],revision,page,"same title-block row to the right")])
                    break
            # A visible revision history is provenance, not an approval event.
            history=[]
            for token in words:
                if not(re.fullmatch(r"[A-Z]",token[4]) and
                       .66*w<=token[0]<=.77*w and 0.04*h<=token[1]<.18*h):
                    continue
                dates=[other for other in words
                       if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}",other[4])
                       and .78*w<other[0] and abs(other[1]-token[1])<10]
                history.append({"revision":token[4],
                                "date_as_printed":dates[0][4] if dates else None,
                                "page":1,
                                "bbox":short_box(token,page)})
            result["revision_history"]=history[:12]
            if result["revision"]:
                revisions_in_history={item["revision"] for item in history}
                if revisions_in_history and result["revision"] not in revisions_in_history:
                    result["warnings"].append("TITLE_REVISION_ABSENT_FROM_VISIBLE_HISTORY")
        elif group.startswith("BCN3D_"):
            result["format"]="BCN3D_CATALAN_TITLE_BLOCK"
            # The reference may be e.g. BCN3DSigma-M... rather than the
            # code in the public filename. Keep both identifiers distinct.
            refs=[tok for tok in words if re.fullmatch(
                r"(?:\d{3}M\d{2}C\d{2}\.\dP\d+[A-Z]|BCN3DSigma-M\d+C\d+\.\dP\d+[A-Z])",
                tok[4],re.I) and tok[1]>.82*h]
            if len(refs)==1:
                ref=refs[0]
                result["designation"]=ref[4]
                result["evidence"].append(record("designation",ref[4],ref,page,
                                                "Catalan Referència title-block area"))
            # An explicit revision near 'Versió plànol', not a page or product
            # release version. A V1.0 marking is not a change notice.
            labels=[tok for tok in words if "versi" in tok[4].casefold()
                    and tok[1]>.76*h]
            revs=[tok for tok in words if re.fullmatch(r"V\d+\.\d+(?:\.\d+)?",tok[4])
                  and tok[1]>.80*h]
            for label in labels:
                near=[tok for tok in revs if 0<=tok[1]-label[1]<=35
                      and abs(tok[0]-label[0])<=85]
                if len(near)==1:
                    result["revision"]=near[0][4]
                    result["evidence"].append(record("revision",near[0][4],
                                                   near[0],page,
                                                   "value below Versió plànol"))
                    break
        elif group=="ArcBlue_HMT_Lite":
            result["format"]="ARCBLUE_CHINESE_TITLE_BLOCK"
            bottom_words=[tok for tok in words if tok[1]>.58*h]
            bottom=" ".join(tok[4] for tok in bottom_words)
            grade_match=re.search(r"6061[- ]?T6|AISI\s*304|SUS\s*304",bottom,re.I)
            if grade_match:
                grade=grade_match.group(0)
                first=next((tok for tok in bottom_words if grade.split()[0].casefold()
                            in tok[4].casefold()),None)
                if first:
                    result["material_grade"]=grade
                    result["evidence"].append(record("material_grade",grade,first,
                                             page,"visible material label region"))
            # Drawings may have a revision *label* but no value; never use 'A3'
            # (paper size) or a coordinate-grid 'A' as revision.
            result["warnings"].append("ARCBLUE_REVISION_NOT_CONFIRMABLE_FROM_TEXT_LAYER")
        else:
            result["warnings"].append("NO_KNOWN_TITLE_BLOCK_PARSER")
        if result["format"]=="PASSPORT_TITLE_BLOCK" and not any(
                "REVISION" in tok[4].upper() and tok[1]>.65*h for tok in words):
            result["format"]="UNKNOWN"
            result["warnings"].append("NO_PASSPORT_TITLE_BLOCK_FOUND")
        if result["format"]=="BCN3D_CATALAN_TITLE_BLOCK" and not any(
                "versi" in tok[4].casefold() and tok[1]>.70*h for tok in words):
            result["format"]="UNKNOWN"
            result["warnings"].append("NO_BCN3D_TITLE_BLOCK_FOUND")
        if result["designation"] or result["revision"] or result["material_grade"]:
            result["recognition"]="CANDIDATE_FIELDS_EXTRACTED"
        if try_ocr and not result["designation"] and not result["revision"]:
            try:
                # Title-block OCR is a review hint only. No OCR output is ever
                # elevated to a confirmed revision or approval.
                from extract_worker import ocr_image
                clip=fitz.Rect(.52*w,.66*h,w,h)
                pix=page.get_pixmap(matrix=fitz.Matrix(1.8,1.8),clip=clip,
                                    colorspace=fitz.csRGB,alpha=False)
                if pix.width*pix.height>7_000_000:
                    raise ValueError("OCR crop too large")
                with tempfile.TemporaryDirectory(prefix="p8-stamp-ocr-") as tmp:
                    image=pathlib.Path(tmp)/"crop.png"
                    pix.save(str(image))
                    candidate,language=ocr_image(image)
                    result["ocr_candidate"]={"text":candidate[:750],
                                             "language":language,
                                             "status":"UNVERIFIED_OCR_HINT"}
                    result["warnings"].append("OCR_REQUIRES_VISUAL_REVIEW")
            except Exception:
                result["warnings"].append("OCR_UNAVAILABLE_OR_FAILED")
    return result

def main():
    if len(sys.argv)<3:
        raise ValueError("usage: p8_pdf_worker.py PDF_PATH SOURCE_GROUP [--ocr]")
    result=inspect(sys.argv[1],sys.argv[2],try_ocr="--ocr" in sys.argv[3:])
    print('P8_RESULT_JSON:'+json.dumps(result,ensure_ascii=True))

if __name__=="__main__":
    try:main()
    except Exception as error:
        print('P8_RESULT_JSON:'+json.dumps({"error":type(error).__name__+": "+str(error)[:200]},ensure_ascii=True))
        sys.exit(2)
