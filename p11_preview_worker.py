"""Render a small verifiable crop around a PDF callout (not a new source document).

The only caller is the P11 server, passing a SHA-checked immutable temporary
copy of the public PDF, and coordinates previously extracted by P9.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
from pathlib import Path
import sys

MAX_BYTES=9_000_000
MAX_RENDER_PIXELS=1_200_000

def render(filename, page_number, bbox):
    import fitz
    original=Path(filename)
    if not original.is_file() or original.stat().st_size>MAX_BYTES:
        raise ValueError("PDF_TOO_LARGE_OR_MISSING")
    data=original.read_bytes()
    sha=hashlib.sha256(data).hexdigest()
    if not isinstance(page_number,int) or not 1<=page_number<=75:
        raise ValueError("PAGE_INVALID")
    if not isinstance(bbox,list) or len(bbox)!=4 or any(
        not isinstance(x,(int,float)) or not math.isfinite(x) or not 0<=x<=1 for x in bbox
    ) or bbox[2]<=bbox[0] or bbox[3]<=bbox[1]:
        raise ValueError("BBOX_INVALID")
    with fitz.open(stream=data,filetype="pdf") as doc:
        if doc.needs_pass or len(doc)>75 or page_number>len(doc):
            raise ValueError("UNAVAILABLE_PDF_PAGE")
        page=doc[page_number-1]
        w,h=page.rect.width,page.rect.height
        if not 20<w<5000 or not 20<h<5000:
            raise ValueError("PAGE_DIMENSIONS_INVALID")
        target=fitz.Rect(bbox[0]*w,bbox[1]*h,bbox[2]*w,bbox[3]*h)
        if target.is_empty or not page.rect.contains(target):
            raise ValueError("ANNOTATION_OUTSIDE_PAGE")
        # Include a sufficiently broad context to reveal drawing leaders;
        # never present this crop as definitive 2-D to 3-D mapping evidence.
        horizontal=max(76.0,(target.width)*4)
        vertical=max(52.0,(target.height)*4)
        region=fitz.Rect(target.x0-horizontal,target.y0-vertical,
                         target.x1+horizontal,target.y1+vertical)
        clip=region & page.rect
        scale=min(2.25,math.sqrt(MAX_RENDER_PIXELS/(clip.width*clip.height)))
        if scale<0.5:raise ValueError("CROP_TOO_LARGE")
        page.draw_rect(target,color=(0.9,0.2,0.12),width=2.3,overlay=True)
        pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=clip,
                            colorspace=fitz.csRGB,alpha=False)
        if pix.width*pix.height>MAX_RENDER_PIXELS:
            raise ValueError("IMAGE_TOO_LARGE")
        png=pix.tobytes("png")
        if len(png)>2_000_000:raise ValueError("PNG_TOO_LARGE")
    return {
        "page":page_number,
        "source_sha256":sha,
        "width":pix.width,"height":pix.height,
        "image":base64.b64encode(png).decode("ascii"),
        "crop_normalized":[round(clip.x0/w,6),round(clip.y0/h,6),
                           round(clip.x1/w,6),round(clip.y1/h,6)],
        "interpretation":"DOCUMENT_CROP_ONLY_NOT_FEATURE_MAPPING"
    }

if __name__=="__main__":
    try:
        if len(sys.argv)!=4:raise ValueError("PDF PAGE BBOX_JSON required")
        outcome=render(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]))
        print("P11_PREVIEW_JSON:"+json.dumps(outcome,ensure_ascii=True,separators=(",",":")))
    except Exception as exc:
        print("P11_PREVIEW_JSON:"+json.dumps({"error":type(exc).__name__},ensure_ascii=True))
        sys.exit(2)
