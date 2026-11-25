"""Local-only document text extraction, with Windows native OCR for scanned PDFs.

Runs in a separate process, bounded by ai_extension.parse_bytes. No cloud, no
external model downloads. Never trust an OCR result as a verified engineering fact.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import subprocess
import sys
import tempfile
import zipfile

MAX_PAGES=80
MAX_TOTAL=85000
MAX_EXPANDED=40*1024*1024
MAX_OCR_PAGES=10
MAX_OCR_TILES_TOTAL=12
MAX_TILES_PER_PAGE=12
MAX_TILES_AXIS=5
MIN_TEXT_LENGTH=20
SCALE=170/72
MAX_TILE_PX=2250
OVERLAP_PT=15
ROOT=pathlib.Path(__file__).resolve().parent
OCR_SCRIPT=ROOT/"windows_native_ocr.ps1"


def clean(text):
    return re.sub(r"\n{4,}", "\n\n\n",str(text).replace("\x00","").replace("\r\n","\n")).strip()


def _hidden_startupinfo():
    if not hasattr(subprocess,"STARTUPINFO"):
        return None
    info=subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess,"STARTF_USESHOWWINDOW",0)
    info.wShowWindow=getattr(subprocess,"SW_HIDE",0)
    return info


def ocr_image(path):
    if not OCR_SCRIPT.is_file():
        raise RuntimeError("Windows OCR script not installed")
    try:
        p=subprocess.run(["powershell","-NoProfile","-NonInteractive","-WindowStyle","Hidden",
                          "-ExecutionPolicy","Bypass","-File",str(OCR_SCRIPT),
                          "-ImagePath",str(path)],capture_output=True,text=True,
                         encoding="utf-8",errors="strict",timeout=25,
                         creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
                         startupinfo=_hidden_startupinfo())
    except OSError as exc:
        raise RuntimeError("Встроенный Windows OCR недоступен в этой среде.") from exc
    try:
        result=json.loads(p.stdout)
    except ValueError:
        raise RuntimeError("Встроенный Windows OCR не вернул результат.")
    if p.returncode!=0 or not result.get("ok"):
        raise RuntimeError("Windows OCR: "+str(result.get("error","Ошибка распознавания."))[:200])
    return clean(result.get("text","")),result.get("language","ru")


def raster_page_ocr(page,dir_path,tile_budget):
    """Only render bounded tiles (never a full unbounded A0 raster)."""
    import fitz
    w,h=float(page.rect.width),float(page.rect.height)
    if min(w,h)<30 or max(w,h)>12000:
        raise ValueError("Размер страницы PDF вне поддерживаемого диапазона.")
    nx=max(1,math.ceil((w*SCALE)/MAX_TILE_PX))
    ny=max(1,math.ceil((h*SCALE)/MAX_TILE_PX))
    if nx>MAX_TILES_AXIS or ny>MAX_TILES_AXIS or nx*ny>MAX_TILES_PER_PAGE:
        raise ValueError("Страница слишком широкоформатная для текущего лимита OCR.")
    if nx*ny>tile_budget:
        raise ValueError("Исчерпан лимит распознавания страниц этого документа.")
    texts=[];lang="ru"
    for iy in range(ny):
        for ix in range(nx):
            x0=w*ix/nx
            x1=w*(ix+1)/nx
            y0=h*iy/ny
            y1=h*(iy+1)/ny
            clip=fitz.Rect(max(0,x0-OVERLAP_PT),max(0,y0-OVERLAP_PT),
                           min(w,x1+OVERLAP_PT),min(h,y1+OVERLAP_PT))
            pix=page.get_pixmap(matrix=fitz.Matrix(SCALE,SCALE),clip=clip,
                                colorspace=fitz.csRGB,alpha=False)
            # Bounded raster footprint before the OCR process sees an image.
            if pix.width*pix.height>8_000_000:
                raise ValueError("OCR-фрагмент слишком большой.")
            img=dir_path/("tile_"+str(iy)+"_"+str(ix)+".png")
            pix.save(str(img))
            try:
                value,lang=ocr_image(img)
                if value:texts.append(value)
            finally:
                img.unlink(missing_ok=True)
    # Overlap may repeat a line. This does not authorize deduplicating numbers.
    return clean("\n".join(texts)),lang,nx*ny


def extract_pdf(source):
    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise ValueError("Для разбора PDF установите зависимости из requirements-documents.txt (PyMuPDF).") from exc
    result=[];missing=[];ocr_pages=[];skipped=[];ocr_count=0;tiles_used=0
    with fitz.open(str(source)) as doc:
        if doc.needs_pass:
            raise ValueError("PDF защищён паролем. Нужен незашифрованный файл.")
        if len(doc)>MAX_PAGES:
            raise ValueError(f"В этом пилоте поддерживаются PDF до {MAX_PAGES} страниц.")
        with tempfile.TemporaryDirectory(prefix="factory-native-ocr-") as tmp:
            folder=pathlib.Path(tmp)
            for page_no,page in enumerate(doc,1):
                value=clean(page.get_text("text",sort=True))
                if len(value)>=MIN_TEXT_LENGTH:
                    result.append({"page":page_no,"text":value,"extraction_method":"text"})
                    continue
                if ocr_count>=MAX_OCR_PAGES or tiles_used>=MAX_OCR_TILES_TOTAL:
                    missing.append(page_no);skipped.append(page_no)
                    continue
                ocr_count+=1
                try:
                    ocr_text,lang,tiles=raster_page_ocr(page,folder,MAX_OCR_TILES_TOTAL-tiles_used)
                    tiles_used+=tiles
                except (ValueError,RuntimeError,OSError,subprocess.TimeoutExpired) as exc:
                    missing.append(page_no)
                    skipped.append(page_no)
                    continue
                if len(ocr_text)>=MIN_TEXT_LENGTH:
                    result.append({"page":page_no,"text":ocr_text,
                                   "extraction_method":"ocr","ocr_language":lang})
                    ocr_pages.append(page_no)
                else:
                    missing.append(page_no)
    return result,missing,ocr_pages,skipped


def extract_docx(source):
    try:
        with zipfile.ZipFile(source) as z:
            if len(z.infolist())>2500 or sum(i.file_size for i in z.infolist())>MAX_EXPANDED:
                raise ValueError("DOCX содержит слишком много распакованных данных.")
            if any(i.flag_bits&1 for i in z.infolist()):
                raise ValueError("Зашифрованный DOCX не поддерживается.")
    except zipfile.BadZipFile as exc:
        raise ValueError("Повреждённый или неподдерживаемый DOCX-файл.") from exc
    try:
        from docx import Document
    except ModuleNotFoundError as exc:
        raise ValueError("Для разбора DOCX установите зависимости из requirements-documents.txt (python-docx).") from exc
    doc=Document(source)
    blocks=[]
    if hasattr(doc,"iter_inner_content"):
        for el in doc.iter_inner_content():
            if hasattr(el,"rows"):
                for row in el.rows:
                    values=[clean(c.text) for c in row.cells]
                    if any(values):blocks.append(" | ".join(values))
            else:
                text=clean(el.text)
                if text:blocks.append(text)
    else:
        blocks.extend(clean(p.text) for p in doc.paragraphs if clean(p.text))
        for table in doc.tables:
            blocks.extend(" | ".join(clean(c.text) for c in row.cells) for row in table.rows)
    text=clean("\n".join(blocks))
    return ([{"page":1,"text":text,"extraction_method":"text"}] if text else [])


def extract_txt(source):
    raw=source.read_bytes()
    try:text=raw.decode("utf-8-sig")
    except UnicodeError:
        try:text=raw.decode("cp1251")
        except UnicodeError as exc:
            raise ValueError("Не удалось определить кодировку TXT (поддерживаются UTF-8 и Windows-1251).") from exc
    text=clean(text)
    return ([{"page":1,"text":text,"extraction_method":"text"}] if text else [])


def main(source):
    ext=source.suffix.lower()
    if ext==".pdf":
        pages,missing,ocr_pages,skipped=extract_pdf(source)
    elif ext==".docx":
        pages=extract_docx(source);missing=[];ocr_pages=[];skipped=[]
    elif ext==".txt":
        pages=extract_txt(source);missing=[];ocr_pages=[];skipped=[]
    else:
        raise ValueError("Разрешены только PDF, DOCX и TXT.")
    total=sum(len(x["text"]) for x in pages)
    if total>MAX_TOTAL:
        raise ValueError("Документ слишком объёмный для учебного стенда (более 85 тыс. символов).")
    if not pages:
        raise ValueError("Текст не найден. OCR не распознал содержимое скана; "
                         "проверьте качество изображения или наличие языкового пакета Windows.")
    note=[]
    if ocr_pages:
        note.append("OCR распознал страницы "+", ".join(map(str,ocr_pages))+
                    ". Цифры, обозначения и допуски требуют сверки с оригиналом.")
    if missing:
        note.append("Не удалось извлечь текст со страниц "+", ".join(map(str,missing))+
                    ". Сведения на этих страницах не участвуют в поиске.")
    return {"pages":pages,"unreadable_pages":missing,"ocr_pages":ocr_pages,
            "skipped_ocr_pages":skipped,"total_chars":total,"note":" ".join(note)}


def emit_json(record):
    """Emit ASCII-only JSON regardless of the Windows active console code page."""
    sys.stdout.buffer.write((json.dumps(record,ensure_ascii=True)+chr(10)).encode("ascii"))


if __name__=="__main__":
    try:
        res=main(pathlib.Path(sys.argv[1]))
        emit_json({"ok":True,**res})
    except Exception as e:
        emit_json({"ok":False,"error":str(e)})
