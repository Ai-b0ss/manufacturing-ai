"""Actual PDF/DOCX/TXT extraction and negative input tests, no DB writes."""
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess

from core06.ingest_service import MAX_BYTES, UploadInputError, prepare_upload

PY = r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"


def _hidden_startupinfo():
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return info


def reject(filename, body):
    try:
        prepare_upload(filename, "Synthetic test", body)
    except UploadInputError:
        return
    raise AssertionError("Rejected input was accepted: " + filename)


def main():
    with TemporaryDirectory(prefix="factory-v06-extraction-check-") as tmp:
        pdf = Path(tmp) / "drawing.pdf"
        docx = Path(tmp) / "report.docx"
        code = (
            "import fitz,docx,sys;"
            "d=fitz.open();p=d.new_page();p.insert_text((70,80),'Technical drawing TOL789 revision 2B factory QA');"
            "d.save(sys.argv[1]);d.close();"
            "w=docx.Document();w.add_paragraph('Factory specifications DOC-325 in new revision');"
            "w.save(sys.argv[2])"
        )
        result = subprocess.run(
            [PY, "-c", code, str(pdf), str(docx)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=_hidden_startupinfo(),
        )
        assert result.returncode == 0, result.stderr[:400]
        for file in (pdf, docx):
            record = prepare_upload(file.name, file.stem, file.read_bytes())
            assert record.chunks and record.media_type and record.sha256
            assert "Factory" in record.chunks[0]["text"] or "Technical" in record.chunks[0]["text"]
        record = prepare_upload("simple.txt", "Text", b"Manufacturing local text source QA123")
        assert record.chunks and "QA123" in record.chunks[0]["text"]
        reject("../sneaky.txt", b"bad")
        reject("bad.pdf", b"not a pdf")
        reject("bad.txt", b"a" * (MAX_BYTES + 1))
        reject("bad.exe", b"MZ")
        reject("nul.txt", b"a\x00b")
    print("PASS actual PDF/DOCX/TXT extraction and 5 invalid-file cases")


if __name__ == "__main__":
    main()
