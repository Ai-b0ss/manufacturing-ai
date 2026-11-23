"""Bounded PDF text extraction worker for the isolated public engineering corpus.

Uses the already installed, local PyMuPDF interpreter. Text extraction is not
OCR; empty image-based pages are explicitly recorded as unreadable.
"""
from __future__ import annotations
import json
import pathlib
import sys

def main(path_string: str) -> None:
    import fitz
    path = pathlib.Path(path_string)
    if not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError("PDF missing or exceeds 8 MB")
    pages = []
    with fitz.open(path) as document:
        if len(document) > 100:
            raise ValueError("PDF exceeds 100 pages")
        for index, page in enumerate(document):
            raw = page.get_text("text", sort=True)
            text = raw[:10000].strip()
            pages.append({"page": index + 1, "text": text,
                          "status": "TEXT_EXTRACTED" if text else "NO_TEXT_LAYER"})
    print(json.dumps({"pages": pages, "page_count": len(pages)}, ensure_ascii=True))

if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: engineering_pdf_worker.py <path>")
        main(sys.argv[1])
    except Exception as error:
        print(json.dumps({"error": type(error).__name__ + ": " + str(error)[:250]},
                         ensure_ascii=True))
        sys.exit(2)
