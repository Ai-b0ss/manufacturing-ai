"""Local JPEG preview of one authorized PDF page. Input PDF bytes on stdin.

No web calls, subprocess has a timeout and input is bounded by the caller.
This is a convenience to inspect OCR discrepancies, not a secure PDF sandbox.
"""
import math
import sys


def main():
    import fitz
    if len(sys.argv) not in (2,3):
        raise ValueError("Page number missing")
    n=int(sys.argv[1])
    if not 1<=n<=80:
        raise ValueError("Invalid page")
    limit=int(sys.argv[2]) if len(sys.argv)==3 else 5*1024*1024
    if not 1<=limit<=8*1024*1024:
        raise ValueError("Invalid size limit")
    data=sys.stdin.buffer.read(limit+1)
    if not data.startswith(b"%PDF-") or len(data)>limit:
        raise ValueError("Invalid PDF input")
    with fitz.open(stream=data,filetype="pdf") as doc:
        if doc.needs_pass or n>len(doc):
            raise ValueError("PDF page unavailable")
        page=doc[n-1]
        w,h=float(page.rect.width),float(page.rect.height)
        if min(w,h)<30 or max(w,h)>12000:
            raise ValueError("Invalid PDF dimensions")
        scale=min(1.8,math.sqrt(3_800_000/(w*h)))
        pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),
                             colorspace=fitz.csRGB,alpha=False)
        jpg=pix.tobytes("jpeg",jpg_quality=82)
        if len(jpg)>2_000_000:
            raise ValueError("Preview is too large")
        sys.stdout.buffer.write(jpg)
        sys.stdout.buffer.flush()


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(str(exc)[:180],file=sys.stderr)
        raise SystemExit(1)
