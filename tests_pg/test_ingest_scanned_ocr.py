"""Actual local WinRT OCR for raster-only PDF (no text layer).

Synthetic image, not real manufacturing evidence. Does not touch the DB.
"""
from __future__ import annotations
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from core06.ingest_service import prepare_upload

PY = r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"


def _hidden_startupinfo():
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return info


def main():
    with TemporaryDirectory(prefix="factory-scanned-ocr-") as tmp:
        pdf = Path(tmp) / "scanned.pdf"
        generator = r"""
import io,sys
import fitz
from PIL import Image,ImageDraw,ImageFont
image=Image.new('RGB',(1600,600),'white')
draw=ImageDraw.Draw(image)
font=ImageFont.truetype(r'C:\Windows\Fonts\arial.ttf',84)
draw.text((110,100),'ASSEMBLY CONTROL',font=font,fill='black')
draw.text((110,230),'BOLT DIAMETER 24 MM',font=font,fill='black')
buffer=io.BytesIO()
image.save(buffer,format='PNG')
doc=fitz.open()
page=doc.new_page(width=800,height=300)
page.insert_image(page.rect,stream=buffer.getvalue())
doc.save(sys.argv[1])
doc.close()
"""
        r = subprocess.run(
            [PY, "-c", generator, str(pdf)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=_hidden_startupinfo(),
        )
        assert r.returncode == 0, r.stderr[:1000]
        result = prepare_upload("scanned.pdf", "Synthetic scanned page", pdf.read_bytes())
        assert result.chunks and all(c["extraction_method"] == "ocr" for c in result.chunks)
        recognized = " ".join(c["text"] for c in result.chunks).upper()
        assert "ASSEMBLY" in recognized or "DIAMETER" in recognized, recognized[:500]
        print("PASS actual raster-only PDF -> Windows OCR -> extracted chunks; advisory only")


if __name__ == "__main__":
    main()
