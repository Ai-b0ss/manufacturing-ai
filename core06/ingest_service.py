"""Bounded, local-only upload extraction and atomic PostgreSQL ingestion.

Only the already verified Keycloak -> actor mapping may call persist_upload().
The DB SECURITY DEFINER function is the final writer authorization boundary.
This module does not index vectors or publish a document outside PostgreSQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid
import zipfile

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 85_000
MEDIA = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXTRACT_PYTHON = Path(
    r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"
)


class UploadInputError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedUpload:
    id: str
    filename: str
    title: str
    media_type: str
    sha256: str
    original_bytes: bytes
    chunks: tuple[dict, ...]
    note: str


def _validate_filename(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not 1 <= len(filename) <= 180
        or any(ord(c) < 32 or c in "/\\" for c in filename)
        or filename in (".", "..")
    ):
        raise UploadInputError("INVALID_FILENAME")
    if filename[-1] in " .":
        raise UploadInputError("INVALID_FILENAME")
    if Path(filename).suffix.lower() not in MEDIA:
        raise UploadInputError("UNSUPPORTED_FILE_TYPE")
    return filename


def _validate_bytes(extension: str, content: bytes) -> None:
    if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_BYTES:
        raise UploadInputError("FILE_SIZE_OUT_OF_RANGE")
    if extension == ".pdf" and not content.startswith(b"%PDF-"):
        raise UploadInputError("INVALID_PDF_SIGNATURE")
    if extension == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(content)) as z:
                if "word/document.xml" not in z.namelist() or "[Content_Types].xml" not in z.namelist():
                    raise UploadInputError("INVALID_DOCX_STRUCTURE")
        except (zipfile.BadZipFile, EOFError) as exc:
            raise UploadInputError("INVALID_DOCX_STRUCTURE") from exc
    if extension == ".txt":
        if b"\x00" in content:
            raise UploadInputError("TEXT_HAS_NUL")
        try:
            content.decode("utf-8-sig")
        except UnicodeError:
            try:
                content.decode("cp1251")
            except UnicodeError as exc:
                raise UploadInputError("INVALID_TEXT_ENCODING") from exc


def _hidden_startupinfo():
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return info


def _extract_worker(source: Path) -> dict:
    executable = Path(os.environ.get("FACTORY_EXTRACTION_PYTHON", str(DEFAULT_EXTRACT_PYTHON)))
    if not executable.is_file():
        raise UploadInputError("EXTRACTION_RUNTIME_MISSING")
    try:
        result = subprocess.run(
            [str(executable), str(ROOT / "extract_worker.py"), str(source)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=_hidden_startupinfo(),
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadInputError("EXTRACTION_TIMEOUT") from exc
    if result.returncode != 0 or len(result.stdout) > 2_000_000:
        raise UploadInputError("EXTRACTION_FAILED")
    try:
        payload = json.loads(result.stdout.decode("ascii"))
    except (UnicodeError, ValueError) as exc:
        raise UploadInputError("EXTRACTION_INVALID_RESULT") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise UploadInputError("EXTRACTION_NO_TEXT")
    return payload


def prepare_upload(filename: str, title: str, content: bytes, extractor=None) -> PreparedUpload:
    filename = _validate_filename(filename)
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
        raise UploadInputError("INVALID_TITLE")
    extension = Path(filename).suffix.lower()
    _validate_bytes(extension, content)
    with tempfile.TemporaryDirectory(prefix="factory-v06-ingest-") as temp_dir:
        source = Path(temp_dir) / ("source" + extension)
        source.write_bytes(content)
        extracted = (extractor or _extract_worker)(source)

    if not isinstance(extracted, dict) or not isinstance(extracted.get("pages"), list):
        raise UploadInputError("EXTRACTION_INVALID_RESULT")
    pages = extracted["pages"]
    if not 1 <= len(pages) <= 100:
        raise UploadInputError("INVALID_EXTRACTED_PAGES")
    chunks = []
    total = 0
    for page in pages:
        if not isinstance(page, dict):
            raise UploadInputError("INVALID_EXTRACTED_CHUNK")
        number, body, method = page.get("page"), page.get("text"), page.get("extraction_method")
        if type(number) is not int or not 1 <= number <= 9999:
            raise UploadInputError("INVALID_PAGE_NUMBER")
        if not isinstance(body, str) or not body.strip():
            raise UploadInputError("EMPTY_EXTRACTED_TEXT")
        if method not in ("text", "ocr"):
            raise UploadInputError("INVALID_EXTRACTION_METHOD")
        total += len(body)
        if total > MAX_TEXT:
            raise UploadInputError("EXTRACTED_TEXT_TOO_LARGE")
        chunks.append({"page": number, "text": body, "extraction_method": method})

    return PreparedUpload(
        id="upload-" + uuid.uuid4().hex,
        filename=filename,
        title=title.strip(),
        media_type=MEDIA[extension],
        sha256=hashlib.sha256(content).hexdigest(),
        original_bytes=content,
        chunks=tuple(chunks),
        note=str(extracted.get("note", ""))[:600],
    )


def persist_upload(db, verified_actor: str, scope_id: str, upload: PreparedUpload, idempotency_key: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", verified_actor):
        raise UploadInputError("INVALID_VERIFIED_ACTOR")
    if not isinstance(scope_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope_id):
        raise UploadInputError("INVALID_SCOPE")
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", idempotency_key):
        raise UploadInputError("INVALID_IDEMPOTENCY_KEY")
    with db.transaction():
        db.execute("SELECT set_config('factory.actor_id',%s,true)", (verified_actor,))
        allowed = db.execute("SELECT core.can_write_scope(%s)", (scope_id,)).fetchone()[0]
        if not allowed:
            raise PermissionError("SCOPE_WRITE_DENIED")
        row = db.execute(
            "SELECT core.ingest_document_once(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (
                idempotency_key, upload.id, scope_id, upload.title, upload.filename,
                upload.media_type, upload.original_bytes, upload.sha256,
                json.dumps(upload.chunks, ensure_ascii=False),
            ),
        ).fetchone()
        if not row or not isinstance(row[0], str) or not re.fullmatch(r"upload-[0-9a-f]{32}", row[0]):
            raise RuntimeError("INGEST_DID_NOT_CONFIRM_VALID_ID")
    return row[0]
