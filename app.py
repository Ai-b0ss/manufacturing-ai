"""Local-only manufacturing knowledge MVP. stdlib only, synthetic data.

This is a TEST BENCH, not an enterprise-grade security boundary. It deliberately
binds to loopback and never communicates with real 1C/PLM or external APIs.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.cookies
import json
import os
import pathlib
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import ai_extension
import engineering_corpus
import engineering_inspection
import dimension_trace
import feature_verification
import visual_review
import cad_visual
import drawing_registration
import audit_witness
import revision_gateway
import re
import secrets
import sqlite3
import threading
import time
from datetime import date
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BASE = pathlib.Path(__file__).resolve().parent
UI = BASE / "index.html"
DB = BASE / "data" / "demo.sqlite3"
CREDS = BASE / "data" / "demo_credentials.txt"

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS account (id TEXT PRIMARY KEY, role TEXT NOT NULL,
  salt TEXT NOT NULL, passhash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session (token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
  csrf TEXT NOT NULL, expires INTEGER NOT NULL, FOREIGN KEY(account_id) REFERENCES account(id));
CREATE TABLE IF NOT EXISTS scope (id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS acl (account_id TEXT NOT NULL, scope_id TEXT NOT NULL,
  PRIMARY KEY(account_id,scope_id), FOREIGN KEY(account_id) REFERENCES account(id),
  FOREIGN KEY(scope_id) REFERENCES scope(id));
CREATE TABLE IF NOT EXISTS document (id TEXT PRIMARY KEY, title TEXT NOT NULL,
  designation TEXT NOT NULL, type TEXT NOT NULL, scope_id TEXT NOT NULL,
  revision TEXT NOT NULL, FOREIGN KEY(scope_id) REFERENCES scope(id));
CREATE TABLE IF NOT EXISTS chunk (id TEXT PRIMARY KEY, doc_id TEXT NOT NULL,
  body TEXT NOT NULL, FOREIGN KEY(doc_id) REFERENCES document(id));
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  chunk_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS part (id TEXT PRIMARY KEY, title TEXT NOT NULL,
  base_document_id TEXT NOT NULL, current_revision TEXT NOT NULL,
  FOREIGN KEY(base_document_id) REFERENCES document(id));
CREATE TABLE IF NOT EXISTS change_notice (id TEXT PRIMARY KEY,
  part_id TEXT NOT NULL, scope_id TEXT NOT NULL, new_revision TEXT NOT NULL,
  valid_from TEXT NOT NULL, directive TEXT NOT NULL, summary TEXT NOT NULL,
  FOREIGN KEY(part_id) REFERENCES part(id), FOREIGN KEY(scope_id) REFERENCES scope(id));
CREATE TABLE IF NOT EXISTS change_notice_source (
  notice_id TEXT PRIMARY KEY, document_id TEXT NOT NULL UNIQUE,
  FOREIGN KEY(notice_id) REFERENCES change_notice(id),
  FOREIGN KEY(document_id) REFERENCES document(id));
CREATE TABLE IF NOT EXISTS app_schema_migration (
  id TEXT PRIMARY KEY, applied_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lot (id TEXT PRIMARY KEY, part_id TEXT NOT NULL,
  revision_id TEXT NOT NULL, manufactured_at TEXT NOT NULL,
  FOREIGN KEY(part_id) REFERENCES part(id));
CREATE TABLE IF NOT EXISTS finance (id TEXT PRIMARY KEY, title TEXT NOT NULL,
  planned REAL NOT NULL, actual REAL NOT NULL, scope_id TEXT NOT NULL,
  FOREIGN KEY(scope_id) REFERENCES scope(id));
CREATE TABLE IF NOT EXISTS audit_event (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
  actor TEXT, action TEXT, target TEXT, outcome TEXT);
"""
# Cyrillic/Latin glyphs are NOT globally interchangeable. Only make a
# secondary designation key to find candidates; always show original glyphs.
CONFUSABLE = str.maketrans({
    "A":"А", "B":"В", "C":"С", "E":"Е", "H":"Н", "K":"К",
    "M":"М", "O":"О", "P":"Р", "T":"Т", "X":"Х", "Y":"У",
})


def designation_key(value: str) -> str:
    return re.sub(r"[\s.\-]", "", value.upper().translate(CONFUSABLE))


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260_000).hex()


def connect(path: pathlib.Path = DB) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=8, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def audit(db, actor: str, action: str, target: str, outcome: str):
    db.execute("INSERT INTO audit_event(ts,actor,action,target,outcome) VALUES(?,?,?,?,?)",
               (int(time.time()), actor, action, target, outcome))


def ensure_demo_notice_link(db):
    """One-time migration for the known synthetic document fixture.

    Never recreate a deleted evidence link at subsequent server starts:
    it could be an intentional revocation or evidence correction.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        if not db.execute("SELECT 1 FROM app_schema_migration WHERE id=?",
                          ("link_demo_notice_v1",)).fetchone():
            db.execute("""
                INSERT OR IGNORE INTO change_notice_source(notice_id,document_id)
                SELECT n.id,d.id FROM change_notice n JOIN document d ON d.id='DOC-003'
                WHERE n.id='ИИ-017' AND n.part_id='АБВГ.712345.004-02'
                  AND n.new_revision='02' AND d.designation='ИИ-017'
                  AND d.type='change' AND d.revision='02'
            """)
            db.execute("INSERT INTO app_schema_migration(id,applied_at) VALUES(?,?)",
                       ("link_demo_notice_v1",int(time.time())))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def initialize(path: pathlib.Path = DB, credentials: pathlib.Path = CREDS, url: str = "http://127.0.0.1:19081"):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        db.executescript(SCHEMA)
        if db.execute("SELECT COUNT(*) FROM account").fetchone()[0]:
            bootstrap_examples(db)
            ai_extension.enable_schema(db)
            ensure_demo_notice_link(db)
            return False
        db.execute("BEGIN IMMEDIATE")
        roles = {
            "technologist": "Технолог цеха",
            "engineer": "Инженер-конструктор",
            "finance": "Финансовый аналитик",
            "director": "Руководитель",
        }
        secrets_file = ["Локальный демонстрационный стенд. Не использовать эти учётные данные в реальной системе.",
                        f"Адрес: {url}", ""]
        for username, role in roles.items():
            passphrase = secrets.token_urlsafe(14)
            salt = secrets.token_hex(16)
            db.execute("INSERT INTO account VALUES(?,?,?,?)",
                       (username, role, salt, password_hash(passphrase, salt)))
            secrets_file.append(f"{role}: {username} / {passphrase}")
        for scope in ["public", "engineering", "finance", "executive"]:
            db.execute("INSERT INTO scope VALUES (?, 1)", (scope,))
        for user, scope in [
            ("technologist", "public"), ("engineer", "public"), ("engineer", "engineering"),
            ("finance", "public"), ("finance", "finance"),
            ("director", "public"), ("director", "engineering"),
            ("director", "finance"), ("director", "executive"),
        ]:
            db.execute("INSERT INTO acl VALUES (?,?)", (user, scope))
        docs = [
            ("DOC-001", "Чертёж детали АБВГ.712345.004-02", "АБВГ.712345.004-02", "drawing", "public", "01",
             "Деталь АБВГ.712345.004-02. Материал 12Х18Н10Т. Базовый чертёж ревизии 01 от 2024 года. "
             "Параметры подлежат уточнению с учётом извещений об изменении. Не запускать производство по одному архивному листу."),
            ("DOC-002", "Технологическая карта корпуса насоса", "ТК-АБВГ-004", "routing", "public", "01",
             "Маршрут изготовления: заготовка, токарная обработка, фрезерование, контроль ОТК. "
             "Перед запуском проверить применимость технологической карты к исполнению детали и последним извещениям."),
            ("DOC-003", "Извещение об изменении ИИ-017", "ИИ-017", "change", "engineering", "02",
             "ИИ-017 для АБВГ.712345.004-02: ревизия 02 с 01.01.2026. Для старого задела указание: доработать. "
             "Новые партии выполнять по редакции 02. Изменение шероховатости в указанной области чертежа."),
            ("DOC-004", "План-факт затрат по заказу З-104", "ФИН-З-104", "finance", "finance", "2026-10",
             "Заказ З-104. Плановая себестоимость 4 200 000 рублей, фактическая 4 675 000 рублей. "
             "Дополнительные расходы по материалам и операциям подлежат сверке с учётными регистрами."),
            ("DOC-005", "Закрытый отчёт руководства", "РУК-001", "internal", "executive", "2026-11",
             "Конфиденциальные внутренние показатели дирекции. Искусственные данные только для проверки доступа."),
            ("DOC-006", "Инструкция по неисправностям станка", "СТ-И-001", "manual", "public", "03",
             "При перегреве шпинделя остановить оборудование согласно инструкции производителя и проверить смазку. "
             "Гипотезы неисправности не являются разрешением продолжать эксплуатацию."),
        ]
        for id_, title, code, kind, scope, rev, text in docs:
            db.execute("INSERT INTO document VALUES(?,?,?,?,?,?)", (id_,title,code,kind,scope,rev))
            db.execute("INSERT INTO chunk VALUES(?,?,?)", ("CHK-"+id_, id_,text))
            db.execute("INSERT INTO chunk_fts(chunk_id,text) VALUES(?,?)", ("CHK-"+id_,text))
        db.execute("INSERT INTO part VALUES(?,?,?,?)", ("АБВГ.712345.004-02", "Корпус насоса", "DOC-001", "02"))
        db.execute("INSERT INTO change_notice VALUES(?,?,?,?,?,?,?)", (
            "ИИ-017", "АБВГ.712345.004-02", "engineering", "02", "2026-01-01", "ДОРАБОТАТЬ",
            "Изменены требования к обработке поверхности. Для прежнего задела требуется доработка."))
        for id_,rev,date in [("LOT-2025-12", "01", "2025-12-20"), ("LOT-2026-10", "02", "2026-10-10")]:
            db.execute("INSERT INTO lot VALUES(?,?,?,?)", (id_, "АБВГ.712345.004-02", rev, date))
        db.execute("INSERT INTO finance VALUES(?,?,?,?,?)",("З-104","Заказ З-104",4_200_000,4_675_000,"finance"))
        db.execute("COMMIT")
        bootstrap_examples(db)
        ai_extension.enable_schema(db)
        ensure_demo_notice_link(db)
        credentials.write_text("\n".join(secrets_file)+"\n", encoding="utf-8")
        try: os.chmod(credentials,0o600)
        except OSError: pass
        return True
    except Exception:
        if db.in_transaction: db.execute("ROLLBACK")
        raise
    finally:
        db.close()



def bootstrap_examples(db):
    """Idempotently enrich only the synthetic tutorial corpus. Never ingest real files."""
    examples = [
        ("DEMO-101", "Насос Н-100 — что это и как устроен",
         "Н-100", "manual", "public", "учебный",
         "УЧЕБНЫЙ ПРИМЕР. Насос Н-100 служит для перекачивания воды. "
         "Основные узлы: корпус насоса, рабочее колесо, вал, подшипниковый узел и уплотнение. "
         "Корпус соединяет узлы и направляет поток жидкости. При обслуживании сначала изучают паспорт оборудования и журнал ремонтов. "
         "Все параметры и обозначения в этой базе вымышленные."),
        ("DEMO-102", "Корпус насоса — назначение и порядок изготовления",
         "Корпус Н-100", "routing", "public", "учебный",
         "УЧЕБНЫЙ ПРИМЕР. Корпус насоса удерживает рабочее колесо и обеспечивает герметичность. "
         "Пример маршрута: входной контроль заготовки, обработка базовых поверхностей, "
         "сверление посадочных отверстий, контроль размеров ОТК, сборка. "
         "Перед изготовлением инженер должен подтвердить актуальность чертежа и извещений об изменениях."),
        ("DEMO-103", "Что делать, если перегревается шпиндель станка",
         "СТАНОК-12 / ПАМЯТКА", "manual", "public", "учебный",
         "УЧЕБНАЯ ПАМЯТКА. Если шпиндель станка перегревается, остановите оборудование "
         "по заводскому регламенту и сообщите мастеру. Возможные направления диагностики: "
         "смазка подшипников, перегрузка, состояние системы охлаждения, вибрация, ошибка датчика. "
         "Не снимайте защитные кожухи и не запускайте станок до проверки уполномоченным специалистом."),
        ("DEMO-104", "Почему появился брак корпуса: проверка причин",
         "ОТК / БРАК", "manual", "public", "учебный",
         "УЧЕБНЫЙ ПРИМЕР. При обнаружении брака корпуса насоса: изолируйте изделие, "
         "зафиксируйте номер партии и измерения, проверьте актуальный чертёж и маршрутную карту, "
         "сравните калибровку инструмента и журнал станка. Возможные причины: износ инструмента, "
         "ошибка настройки, неверная ревизия документации. Решение об использовании брака принимает ОТК."),
        ("DEMO-105", "Как проверяют актуальность чертежа перед работой",
         "ПРОЦЕСС / КБ", "manual", "public", "учебный",
         "УЧЕБНАЯ ИНСТРУКЦИЯ. Перед производством детали технолог сверяет обозначение, "
         "исполнение, номер ревизии, изменения КБ, дату и номер партии. "
         "Если у сотрудника недостаточно прав или архив неполон, актуальность помечают "
         "как неподтверждённую и направляют запрос в КБ. Архивный лист не считается автоматически действующим."),
        ("DEMO-106", "Технологическая карта: проверка фрезеровки корпуса",
         "КАРТА / ФРЕЗЕРОВКА", "routing", "public", "учебный",
         "УЧЕБНЫЙ ПРИМЕР. Для фрезерования корпуса насоса сначала проверяют базирование, "
         "правильность установки приспособления, инструмент и чертёжную ревизию. "
         "После операции проводят контроль ОТК по технологической карте. "
         "Численные режимы обработки в демо не задаются — их утверждает технолог."),
        ("DEMO-107", "Инженеру: что изменилось в корпусе насоса",
         "ПРОЕКТ / ИИ-017", "change", "engineering", "учебный",
         "УЧЕБНЫЙ ПРИМЕР. Извещение ИИ-017 относится к корпусу насоса Н-100. "
         "В демонстрации обновлена редакция чертежа с 01 на 02, "
         "а старый задел требует решения инженера по доработке. "
         "Эта карточка доступна только инженерному отделу и руководителю."),
    ]
    db.execute("BEGIN IMMEDIATE")
    try:
        # Update only our known synthetic seed documents, preserving IDs and access scopes.
        db.execute("UPDATE document SET title=? WHERE id='DOC-001'",
                   ("Корпус насоса Н-100 — архивный чертёж детали",))
        db.execute("UPDATE document SET title=? WHERE id='DOC-002'",
                   ("Как изготовить корпус насоса — технологическая карта",))
        db.execute("UPDATE document SET title=? WHERE id='DOC-006'",
                   ("Станок: инструкция при перегреве шпинделя",))
        for id_,title,code,kind,scope,rev,body in examples:
            row=db.execute("SELECT 1 FROM document WHERE id=?",(id_,)).fetchone()
            if row is None:
                db.execute("INSERT INTO document VALUES (?,?,?,?,?,?)",(id_,title,code,kind,scope,rev))
                chunk="CHK-"+id_
                db.execute("INSERT INTO chunk VALUES (?,?,?)",(chunk,id_,body))
                db.execute("INSERT INTO chunk_fts(chunk_id,text) VALUES(?,?)",(chunk,body))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def list_documents(db,subject,limit=100):
    rows=db.execute("""SELECT d.id,d.title,d.designation,d.type,d.revision,d.scope_id,
                             c.body AS body,
                             EXISTS(SELECT 1 FROM upload_page up WHERE up.doc_id=d.id
                                    AND up.extraction_method='ocr') AS has_ocr
                      FROM document d JOIN acl a ON a.scope_id=d.scope_id
                      JOIN chunk c ON c.id=(SELECT c2.id FROM chunk c2
                          WHERE c2.doc_id=d.id ORDER BY c2.id LIMIT 1)
                      WHERE a.account_id=? ORDER BY d.title LIMIT ?""",
                    (subject,max(1,min(int(limit),100)))).fetchall()
    return [{"id":r["id"],"title":r["title"],"designation":r["designation"],
             "type":r["type"],"revision":r["revision"],"snippet":r["body"][:330],
             "has_ocr":bool(r["has_ocr"])} for r in rows]


def allowed(db, subject: str, scope: str) -> bool:
    return db.execute("SELECT 1 FROM acl WHERE account_id=? AND scope_id=?",
                      (subject,scope)).fetchone() is not None


def get_document(db, subject: str, doc_id: str):
    row=db.execute("SELECT d.* FROM document d JOIN acl a ON a.scope_id=d.scope_id "
                   "WHERE d.id=? AND a.account_id=?", (doc_id,subject)).fetchone()
    if row is None: return None
    if row["type"]=="uploaded":
        full_pages=db.execute("SELECT page_no,full_text,extraction_method FROM upload_page "
                              "WHERE doc_id=? ORDER BY page_no",(doc_id,)).fetchall()
        if full_pages:
            exact="\n\n".join(
                f"Страница {x['page_no']}"+
                (" · OCR (проверьте цифры и обозначения)" if x["extraction_method"]=="ocr" else "")+
                f"\n{x['full_text']}" for x in full_pages)
            return {**dict(row),"text":exact}
    text=db.execute("""SELECT c.body,cp.page_no FROM chunk c
         LEFT JOIN chunk_page cp ON cp.chunk_id=c.id
         WHERE c.doc_id=? ORDER BY c.id""",(doc_id,)).fetchall()
    full="\n\n".join((f"Страница {r['page_no']}\n" if r['page_no'] else "")+r["body"]
                      for r in text)
    return {**dict(row),"text":full}


def lexical_search(db,subject,raw_query,limit=20):
    """ACL-filtered lexical search, exact numeric/engineering identifier gates.

    One result per document, with its best matching chunk. Production will
    use an indexed, authorization-aware retrieval service rather than this
    small on-prem demonstration.
    """
    if not isinstance(raw_query,str) or len(raw_query)>250:return []
    tokens=ai_extension._tokens(raw_query)
    codes=ai_extension._IDENTIFIER_PATTERN.findall(raw_query)
    if not tokens and not codes:return []
    numbers=ai_extension._standalone_numbers(raw_query)
    required_keys={ai_extension._identifier_key(c) for c in codes}
    covered_keys=set()
    rows=db.execute("""SELECT d.id,d.title,d.designation,d.type,d.revision,d.scope_id,c.body,
                              COALESCE(cp.extraction_method,'text') AS extraction_method
        FROM document d JOIN acl a ON a.scope_id=d.scope_id AND a.account_id=?
        JOIN chunk c ON c.doc_id=d.id
        LEFT JOIN chunk_page cp ON cp.chunk_id=c.id""",(subject,)).fetchall()
    best={}
    for r in rows:
        full=r["title"]+" "+r["designation"]+" "+r["body"]
        normalized=full.casefold().replace("ё","е")
        heading=(r["title"]+" "+r["designation"]).casefold().replace("ё","е")
        if numbers and any(not ai_extension._contains_number(normalized,n) for n in numbers):
            continue
        method="ocr" if r["extraction_method"]=="ocr" else "text"
        eval_id=ai_extension._evaluate_identifier_match(
            raw_query,r["title"],r["designation"],r["body"],method)
        if required_keys and not eval_id.matched_keys:
            continue
        covered_keys.update(eval_id.matched_keys)
        hits=sum(t in normalized or (len(t)>5 and t[:5] in normalized) for t in tokens if not t.isdecimal() or t in numbers)
        if not hits and not required_keys:continue
        score=hits*4+sum(8 for t in tokens if (not t.isdecimal() or t in numbers) and t in heading)
        if required_keys:
            score+=60+20*len(eval_id.matched_keys)+15*len(eval_id.heading_keys)
            if eval_id.match_type=="ocr_fuzzy_candidate":score-=10
            if eval_id.sibling_variant_conflict:score-=30
        designation_match=(len(raw_query)>5 and
                           designation_key(r["designation"])==designation_key(raw_query))
        if designation_match:score+=30
        previous=best.get(r["id"])
        if previous is None or score>previous[0]:
            best[r["id"]]=(score,r)
    if required_keys and not required_keys.issubset(covered_keys):
        return []
    ranked=sorted(best.values(),key=lambda x:(-x[0],x[1]["title"]))[:max(1,min(int(limit),50))]
    result=[]
    for _,row in ranked:
        result.append({"id":row["id"],"title":row["title"],
          "designation":row["designation"],"type":row["type"],
          "revision":row["revision"],"snippet":row["body"][:330],
          "has_ocr":bool(db.execute("SELECT 1 FROM upload_page "
                    "WHERE doc_id=? AND extraction_method='ocr' LIMIT 1",
                    (row["id"],)).fetchone())})
    return result


def part_report(db, subject: str, designation: str, lot_id: str = ""):
    """Auditably compare one synthetic part's drawing, notices and batch.

    This only checks internal consistency of a tiny demo archive; it does not
    establish technical correctness or authorize manufacture.
    A workshop user gets exactly the same response for every lot or hidden
    change notice. The notice's existence must not leak across ACL boundaries.
    """
    if not isinstance(designation,str) or len(designation)>120:
        return {"state":"NOT_FOUND"}
    if not isinstance(lot_id,str) or len(lot_id)>120:
        return {"state":"LOT_NOT_FOUND"}
    # Never silently equate Latin and Cyrillic lookalikes in an engineering
    # identity check; search may suggest candidates, but verification is strict.
    key="".join(designation.upper().split()).replace(".","").replace("-","")
    part=next((r for r in db.execute("SELECT * FROM part")
               if "".join(r["id"].upper().split()).replace(".","").replace("-","")==key),None)
    if part is None:return {"state":"NOT_FOUND"}
    base=get_document(db,subject,part["base_document_id"])
    if base is None:return {"state":"NOT_FOUND"}
    withheld={"state":"CURRENTNESS_UNVERIFIED","part":part["id"],
              "message":"Доступных сведений недостаточно для проверки редакции."}
    if not allowed(db,subject,"engineering"):
        return withheld

    def uncertain(reason, sources=()):
        # Only authorized engineering/management roles receive a diagnostic
        # reason about accessible or inconsistent manufacturing records.
        return {"state":"CURRENTNESS_UNVERIFIED","part":part["id"],
                "message":"Не удалось собрать согласованный комплект документов.",
                "issue":reason,"evidence":list(sources)}

    base_ref={"doc_id":base["id"],"title":base["title"],"revision":base["revision"],
              "kind":"drawing"}
    evidence=[base_ref]
    if base["type"]!="drawing" or base["designation"]!=part["id"]:
        return uncertain("INVALID_BASE_DRAWING",evidence)

    notices=db.execute(
        "SELECT * FROM change_notice WHERE part_id=? ORDER BY valid_from,id",
        (part["id"],)).fetchall()
    if not notices:
        # One base drawing cannot prove no later notice exists.
        return uncertain("NO_NOTICE_CHAIN",evidence)
    if any(not allowed(db,subject,n["scope_id"]) for n in notices):
        # Even for a generally privileged role, an inaccessible notice is not
        # safe to use or announce. No notice metadata is returned.
        return withheld

    previous_revision=base["revision"]
    previous_date=None
    timeline=[]
    for notice in notices:
        link=db.execute("SELECT document_id FROM change_notice_source WHERE notice_id=?",
                        (notice["id"],)).fetchone()
        if link is None:
            return uncertain("NOTICE_SOURCE_NOT_LINKED",evidence)
        original=get_document(db,subject,link["document_id"])
        if original is None:
            # Treat hidden source as uncertain without saying which link exists.
            return withheld
        if (original["type"]!="change" or original["designation"]!=notice["id"]
                or original["revision"]!=notice["new_revision"]):
            return uncertain("NOTICE_SOURCE_MISMATCH",evidence)
        try:
            effective=date.fromisoformat(notice["valid_from"])
        except (ValueError,TypeError):
            return uncertain("INVALID_EFFECTIVE_DATE",evidence)
        if (previous_date is not None and effective<=previous_date) or (
                notice["new_revision"]==previous_revision):
            return uncertain("INCONSISTENT_NOTICE_SEQUENCE",evidence)
        if notice["directive"] not in {"ДОРАБОТАТЬ","ИСПОЛЬЗОВАТЬ","ЗАПРЕТИТЬ"}:
            return uncertain("UNKNOWN_STOCK_DIRECTIVE",evidence)
        evidence.append({"doc_id":original["id"],"title":original["title"],
                         "revision":original["revision"],"kind":"change_notice",
                         "notice_id":notice["id"],"effective_from":notice["valid_from"]})
        timeline.append((effective,notice))
        previous_revision=notice["new_revision"]
        previous_date=effective
    # A future-dated notice is not proof that its new revision is in force now.
    if any(effective>date.today() for effective,_ in timeline):
        return uncertain("NOTICE_NOT_YET_EFFECTIVE",evidence)
    if previous_revision!=part["current_revision"]:
        return uncertain("CURRENT_REVISION_MISMATCH",evidence)

    latest=timeline[-1][1]
    report={"state":"VERIFIED_FOR_DEMO","part":part["id"],
            "part_title":part["title"],"base_revision":base["revision"],
            "current_revision":part["current_revision"],
            "message":"Учебные документы согласованы. Не является производственным допуском.",
            "base_document":{"id":base["id"],"title":base["title"]},
            "notice":dict(latest),
            "evidence":evidence,
            "timeline":[{"notice_id":n["id"],"effective_from":n["valid_from"],
                         "new_revision":n["new_revision"],"directive":n["directive"]}
                        for _,n in timeline]}
    if lot_id:
        lot=db.execute("SELECT * FROM lot WHERE id=? AND part_id=?",
                       (lot_id,part["id"])).fetchone()
        if lot is None:
            return {"state":"LOT_NOT_FOUND","part":part["id"],
                    "evidence":evidence,"message":"Партия не найдена."}
        try:
            manufactured=date.fromisoformat(lot["manufactured_at"])
        except (ValueError,TypeError):
            return uncertain("INVALID_BATCH_DATE",evidence)
        if manufactured>date.today():
            return uncertain("FUTURE_BATCH_DATE",evidence)
        expected=base["revision"]
        for effective,n in timeline:
            if manufactured>=effective:expected=n["new_revision"]
            else:break
        matches_at_manufacture=lot["revision_id"]==expected
        matches_current=lot["revision_id"]==part["current_revision"]
        status="REVISION_MATCHES" if matches_at_manufacture and matches_current else "REQUIRES_ENGINEER_REVIEW"
        notice_at_cutover=next((n for effective,n in timeline
                               if effective>manufactured),None)
        directive=notice_at_cutover["directive"] if notice_at_cutover else None
        report["lot"]={"id":lot["id"],"manufactured_at":lot["manufactured_at"],
                       "built_revision":lot["revision_id"],
                       "expected_at_manufacture":expected,
                       "matches_at_manufacture":matches_at_manufacture,
                       "status":status,
                       "directive":directive}
    return report


def metrics(db, subject: str):
    # Deliberately only a typed allowlist; never executes user-supplied SQL.
    if not allowed(db,subject,"finance"): return None
    rows=db.execute("SELECT * FROM finance f JOIN acl a ON a.scope_id=f.scope_id "
                    "WHERE a.account_id=? ORDER BY f.id",(subject,)).fetchall()
    return [{"order":r["id"],"title":r["title"],"planned":r["planned"],
             "actual":r["actual"],"variance":round(r["actual"]-r["planned"],2)} for r in rows]


def revoke(db, subject: str, scope: str):
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DELETE FROM acl WHERE account_id=? AND scope_id=?", (subject,scope))
        db.execute("UPDATE scope SET epoch=epoch+1 WHERE id=?",(scope,))
        audit(db,"system","revoke",subject+":"+scope,"ok")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK");raise


def move_document(db, doc_id: str, new_scope: str):
    db.execute("BEGIN IMMEDIATE")
    try:
        r=db.execute("SELECT scope_id FROM document WHERE id=?",(doc_id,)).fetchone()
        if r is None:raise ValueError("document not found")
        old_scope=r[0]
        if old_scope!=new_scope:
            db.execute("UPDATE document SET scope_id=? WHERE id=?",(new_scope,doc_id))
            db.execute("UPDATE scope SET epoch=epoch+1 WHERE id IN (?,?)",(old_scope,new_scope))
        audit(db,"system","move",doc_id,"ok")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK");raise


def scope_fingerprint(db, subject: str):
    rows=db.execute("SELECT s.id,s.epoch FROM scope s JOIN acl a ON s.id=a.scope_id "
                    "WHERE a.account_id=? ORDER BY s.id",(subject,)).fetchall()
    return hashlib.sha256(json.dumps([(r[0],r[1]) for r in rows]).encode()).hexdigest()[:24]


class Handler(BaseHTTPRequestHandler):
    server_version="FactoryDemo/0.1"
    sys_version=""
    def log_message(self, fmt, *args):
        if self.path.startswith(('/api/login','/api/upload','/api/ask','/api/engineering/')):return
        super().log_message(fmt, *args)

    @property
    def database(self):
        return self.server.database_path

    def reply(self, data, code=200, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(data, (dict,list)):
            body=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
        elif isinstance(data,str):body=data.encode('utf-8')
        else:body=data
        self.send_response(code)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        for name,value in (headers or {}).items():self.send_header(name,value)
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)

    def fail(self,code=403,message="Доступ запрещён"):
        return self.reply({"error":message},code)

    def host_safe(self):
        host=self.headers.get("Host","").lower()
        return host in ("127.0.0.1:"+str(self.server.server_port),"localhost:"+str(self.server.server_port))

    def user(self,db):
        cookies=http.cookies.SimpleCookie()
        try:cookies.load(self.headers.get("Cookie",""))
        except http.cookies.CookieError:return None
        sid=cookies.get("demo_session")
        if sid is None:return None
        tokenhash=hashlib.sha256(sid.value.encode()).hexdigest()
        row=db.execute("SELECT account_id,csrf,expires FROM session WHERE token_hash=?",(tokenhash,)).fetchone()
        return row if row is not None and row["expires"]>int(time.time()) else None

    def do_GET(self):
        if not self.host_safe():return self.fail(400,"Недопустимый Host")
        p=urlsplit(self.path);q=parse_qs(p.query)
        if p.path=='/' or p.path=='/index.html':
            return self.reply(UI.read_bytes(),content_type="text/html; charset=utf-8")
        if p.path=='/style.css':
            return self.reply((BASE/'style.css').read_bytes(),content_type="text/css; charset=utf-8")
        if p.path=='/design-preview.css':
            return self.reply((BASE/'design-preview.css').read_bytes(),content_type="text/css; charset=utf-8")
        if p.path=='/assets/icons.svg':
            return self.reply((BASE/'assets'/'icons.svg').read_bytes(),content_type="image/svg+xml; charset=utf-8")
        if p.path=='/assets/brand-mark.svg':
            return self.reply((BASE/'assets'/'brand-mark.svg').read_bytes(),content_type="image/svg+xml; charset=utf-8")
        if p.path=='/assets/font-face.css':
            return self.reply((BASE/'assets'/'font-face.css').read_bytes(),content_type="text/css; charset=utf-8")
        if p.path.startswith('/assets/fonts/'):
            filename=p.path.removeprefix('/assets/fonts/')
            if not filename or not re.fullmatch(r'[a-z0-9-]+\.woff2',filename):
                return self.fail(404,"Шрифт не найден")
            font_path=BASE/'assets'/'fonts'/filename
            if not font_path.is_file():return self.fail(404,"Шрифт не найден")
            return self.reply(font_path.read_bytes(),content_type="font/woff2")
        if p.path=='/app.js':
            return self.reply((BASE/'app.js').read_bytes(),content_type="application/javascript; charset=utf-8")
        if p.path=='/sample-scan.pdf':
            return self.reply((BASE/'samples'/'factory_scanned_pump_demo.pdf').read_bytes(),
                    content_type="application/pdf",
                    headers={"Content-Disposition":'attachment; filename="factory_scanned_pump_demo.pdf"'})
        if p.path=='/api/health':return self.reply({"status":"ok","mode":"synthetic-local-demo","llm":"available-on-demand","p8_version":"drawing-revision-verification","p9_version":"dimension-traceability","p10_version":"analytic-surface-revision-evidence","p11_version":"visual-evidence-review","p12_version":"cad-visual-audit-witness","p13_version":"drawing-vector-cad-candidates"})
        if not p.path.startswith('/api/'):
            return self.fail(404,"Не найдено")
        with closing(connect(self.database)) as db:
            account=self.user(db)
            if account is None:return self.fail(401,"Требуется вход")
            subject=account["account_id"]
            if p.path=='/api/ai/status':
                return self.reply(ai_extension.status())
            if p.path=='/api/source-file':
                doc_id=q.get("id",[""])[0]
                if get_document(db,subject,doc_id) is None:
                    return self.fail(404,"Файл не найден")
                file=db.execute("SELECT content,mime FROM upload_source WHERE doc_id=?",
                                (doc_id,)).fetchone()
                if file is None:return self.fail(404,"Оригинал недоступен")
                # Download rather than execute potentially active PDF content in browser.
                suffix={"application/pdf":".pdf","text/plain":".txt"}.get(file["mime"],".docx")
                return self.reply(file["content"],content_type="application/octet-stream",
                                  headers={"Content-Disposition":'attachment; filename="source'+suffix+'"'})
            if p.path=='/api/page-preview':
                doc_id=q.get("id",[""])[0]
                if get_document(db,subject,doc_id) is None:
                    return self.fail(404,"Страница не найдена")
                file=db.execute("SELECT content,mime FROM upload_source WHERE doc_id=?",
                                (doc_id,)).fetchone()
                if file is None or file["mime"]!="application/pdf":
                    return self.fail(404,"Оригинальный PDF не найден")
                try:page_no=int(q.get("page",[""])[0])
                except (TypeError,ValueError):
                    return self.fail(400,"Неверная страница")
                try:jpeg=ai_extension.preview_pdf_page(file["content"],page_no)
                except ValueError as e:return self.fail(422,str(e))
                return self.reply(jpeg,content_type="image/jpeg")
            if p.path=='/api/me':
                role=db.execute("SELECT role FROM account WHERE id=?",(subject,)).fetchone()[0]
                scopes=[x[0] for x in db.execute("SELECT scope_id FROM acl WHERE account_id=?",(subject,))]
                return self.reply({"id":subject,"role":role,"scopes":scopes,"csrf":account["csrf"],
                                   "fingerprint":scope_fingerprint(db,subject)})
            if p.path=='/api/documents':
                items=list_documents(db,subject)
                return self.reply({"items":items,"total":len(items)})
            if p.path=='/api/search':
                items=lexical_search(db,subject,q.get("q",[""])[0]);return self.reply({"items":items,"total":len(items)})
            if p.path=='/api/document':
                doc=get_document(db,subject,q.get("id",[""])[0]);
                return self.reply(doc) if doc else self.fail(404,"Документ не найден")
            if p.path.startswith('/api/engineering/'):
                # Never expose a part existence, reference or source download to
                # sessions lacking the engineering scope.
                if not allowed(db,subject,"engineering"):
                    return self.fail(403,"Доступ ограничен")
                p8_original_scope=scope_fingerprint(db,subject)
                def protected_p8_reply(value, content_type=None):
                    # Recheck *after* extraction and before returning source evidence.
                    # Revocation during the slow OCR/metadata call cannot leak data.
                    if (self.user(db) is None or not allowed(db,subject,"engineering")
                            or scope_fingerprint(db,subject)!=p8_original_scope):
                        return self.fail(409,"Права доступа изменились. Повторите запрос.")
                    if content_type is not None:
                        return self.reply(value,content_type=content_type)
                    return self.reply(value)
                try:
                    if p.path=='/api/engineering/p13/vectors':
                        return protected_p8_reply(drawing_registration.preview(
                            q.get("q",[""])[0],q.get("annotation",[""])[0],
                            q.get("axis",["x"])[0]),content_type="image/png")
                    if p.path=='/api/engineering/p13/associations':
                        return protected_p8_reply(drawing_registration.describe(
                            q.get("q",[""])[0],q.get("annotation",[""])[0],
                            q.get("axis",["x"])[0]))
                    if p.path=='/api/engineering/p12/render':
                        return protected_p8_reply(cad_visual.render(
                            q.get("q",[""])[0],q.get("feature",[""])[0],
                            q.get("view",["iso"])[0]),content_type="image/png")
                    if p.path=='/api/engineering/p12/feature':
                        return protected_p8_reply(cad_visual.describe(
                            q.get("q",[""])[0],q.get("feature",[""])[0]))
                    if p.path=='/api/engineering/p12/audit':
                        return protected_p8_reply(visual_review.audit_status())
                    if p.path=='/api/engineering/p11/preview':
                        return protected_p8_reply(visual_review.render_preview(
                            q.get("q",[""])[0],q.get("annotation",[""])[0]),
                            content_type="image/png")
                    if p.path=='/api/engineering/p11/findings':
                        return protected_p8_reply(visual_review.list_findings(q.get("q",[""])[0]))
                    if p.path=='/api/engineering/p10/features':
                        return protected_p8_reply(feature_verification.candidates(q.get("q",[""])[0]))
                    if p.path=='/api/engineering/p10/compare':
                        return protected_p8_reply(feature_verification.compare(
                            q.get("q",[""])[0],q.get("annotation",[""])[0],
                            q.get("feature",[""])[0],q.get("axis",["x"])[0],
                            q.get("window",["0.02"])[0]))
                    if p.path=='/api/engineering/p10/revisions':
                        return protected_p8_reply(revision_gateway.status(q.get("q",[""])[0]))
                    if p.path=='/api/engineering/p9/catalog':
                        return protected_p8_reply(dimension_trace.catalog())
                    if p.path=='/api/engineering/p9/annotations':
                        return protected_p8_reply(dimension_trace.annotations(
                            q.get("q",[""])[0]))
                    if p.path=='/api/engineering/p9/compare':
                        return protected_p8_reply(dimension_trace.compare(
                            q.get("q",[""])[0],q.get("annotation",[""])[0],
                            q.get("axis",[""])[0],q.get("window",["0.02"])[0]))
                    if p.path=='/api/engineering/p8/summary':
                        return protected_p8_reply(engineering_inspection.summary())
                    if p.path=='/api/engineering/p8/catalog':
                        return protected_p8_reply(engineering_inspection.catalog())
                    if p.path=='/api/engineering/p8/part':
                        return protected_p8_reply(engineering_inspection.report_for_part(q.get("q",[""])[0]))
                    if p.path=='/api/engineering/p8/source':
                        return protected_p8_reply(engineering_inspection.inspect_source(q.get("id",[""])[0]))
                    if p.path=='/api/engineering/p8/ocr-hint':
                        return protected_p8_reply(engineering_inspection.inspect_source(
                            q.get("id",[""])[0],ocr=True))
                    if p.path=='/api/engineering/p8/compare-top':
                        return protected_p8_reply(engineering_inspection.compare_bcn3d_top_frames())
                    if p.path=='/api/engineering/p8/notice':
                        return protected_p8_reply(engineering_inspection.historical_notice())
                    if p.path=='/api/engineering/summary':
                        return self.reply(engineering_corpus.summary())
                    if p.path=='/api/engineering/ask':
                        return self.reply(engineering_corpus.answer(
                            q.get("q",[""])[0],group=q.get("group",[""])[0]))
                    if p.path=='/api/engineering/notice':
                        return self.reply(engineering_corpus.notice(q.get("id",[""])[0]))
                    if p.path=='/api/engineering/file':
                        data,suffix=engineering_corpus.original_file(q.get("id",[""])[0])
                        safe_suffix=suffix if suffix in {".pdf",".csv",".xlsx",".step",".stp",".txt",".md",".png"} else ".bin"
                        safe_id=q.get("id",[""])[0]
                        return self.reply(data,content_type="application/octet-stream",
                            headers={"Content-Disposition":'attachment; filename="engineering-'+safe_id+safe_suffix+'"'})
                except drawing_registration.RegistrationError as error:
                    if str(error) in {"ANNOTATION_ID_INVALID","REFERENCE_AXIS_INVALID"}:
                        return self.fail(422,"Неверный идентификатор размерной выноски или ось CAD")
                    if str(error) in {"ANNOTATION_NOT_FOUND","EXACT_DRAWING_CAD_PAIR_REQUIRED"}:
                        return self.fail(404,"Проверенная пара PDF и STEP не найдена")
                    if str(error)=="VECTOR_WORKERS_BUSY":
                        return self.fail(429,"Потоки распознавания размерных выносок заняты")
                    return self.fail(503,"Не удалось подтвердить исходные векторные данные PDF")
                except cad_visual.VisualError as error:
                    if str(error) in {"FEATURE_ID_INVALID","CAMERA_VIEW_INVALID"}:
                        return self.fail(422,"Неверный формат выбора CAD-поверхности")
                    if str(error) in {"FEATURE_ID_UNKNOWN","NO_SOURCE_TRIPLET"}:
                        return self.fail(404,"Проверенная CAD-поверхность не найдена")
                    if str(error)=="RENDER_BUSY":
                        return self.fail(429,"Все процессы визуализации заняты; повторите запрос")
                    return self.fail(503,"CAD-изображение недоступно")
                except visual_review.ReviewError as error:
                    if str(error) in {"INVALID_ANNOTATION_ID","INVALID_FEATURE_ID","QUERY_INVALID"}:
                        return self.fail(422,"Недопустимый идентификатор фрагмента")
                    if str(error) in {"EVIDENCE_ID_UNKNOWN","NO_EXACT_PUBLIC_TRIPLET"}:
                        return self.fail(404,"Исходное доказательство не найдено")
                    if str(error)=="REVIEW_CHAIN_BROKEN" or str(error).startswith("WITNESS_"):
                        return self.fail(409,"Нарушена целостность журнала или подписанного якоря")
                    return self.fail(503,"Не удалось сверить исходные доказательства")
                except feature_verification.FeatureError as error:
                    if str(error) in {"ID_INVALID","AXIS_INVALID","WINDOW_INVALID","WINDOW_LIMIT",
                                      "DIMENSION_SURFACE_TYPES_INCOMPATIBLE"}:
                        return self.fail(422,"Некорректная гипотеза о поверхности")
                    if str(error)=="UNRECOGNIZED_SOURCE_ELEMENT":
                        return self.fail(404,"Элемент в исходных документах не найден")
                    return self.fail(503,"Геометрическая проверка недоступна")
                except dimension_trace.DimensionError as error:
                    if str(error) in {"QUERY_INVALID","ANNOTATION_ID_INVALID","CAD_AXIS_INVALID",
                                      "WINDOW_INVALID","WINDOW_OUT_OF_RANGE","ONLY_LINEAR_CALLOUTS_SUPPORTED"}:
                        return self.fail(422,"Недопустимая гипотеза о размере")
                    if str(error)=="ANNOTATION_NOT_FOUND":
                        return self.fail(404,"Размер на чертеже не найден")
                    return self.fail(503,"Проверка размерных обозначений недоступна")
                except engineering_inspection.InspectionError as error:
                    # Avoid disclosing parser internals or document presence across
                    # ACL boundaries. Only authenticated engineers reach here.
                    if str(error) in {"SOURCE_NOT_FOUND","RELEASE_PAIR_NOT_FOUND"}:
                        return self.fail(404,"Инженерный источник не найден")
                    if str(error) in {"INSPECTION_INDEX_MISSING","INSPECTION_INDEX_STALE",
                                      "WORKER_FAILED_OR_TIMED_OUT","WORKER_REJECTED_SOURCE",
                                      "SOURCE_INSPECTION_STALE"}:
                        return self.fail(503,"Инженерная проверка недоступна; требуется пересборка или диагностика")
                    if str(error) in {"DRAWING_PDF_ONLY","OCR_PDF_ONLY","DISTINCT_SOURCES_REQUIRED"}:
                        return self.fail(422,"Несовместимые документы для операции")
                    return self.fail(409,"Инженерные данные не подтверждены")
                except engineering_corpus.CorpusError as error:
                    if str(error)=="CORPUS_NOT_IMPORTED":
                        return self.fail(503,"Инженерный корпус ещё не импортирован")
                    if str(error)=="SOURCE_NOT_FOUND":
                        return self.fail(404,"Исходный файл не найден")
                    return self.fail(409,"Контрольная сумма исходного файла не совпала")
                return self.fail(404,"Не найдено")
            if p.path=='/api/part':
                return self.reply(part_report(db,subject,q.get("code",[""])[0],q.get("lot",[""])[0]))
            if p.path=='/api/metrics':
                result=metrics(db,subject)
                return self.reply({"items":result}) if result is not None else self.fail(403)
            return self.fail(404,"Не найдено")

    def do_POST(self):
        # Consume small rejected requests before closing on Windows to prevent TCP RST.
        def discard_small_body():
            try:
                pending=int(self.headers.get("Content-Length","0"))
                if 0<pending<=4096:self.rfile.read(pending)
            except (ValueError,OSError,TimeoutError):pass
        if not self.host_safe():
            discard_small_body()
            return self.fail(400,"Недопустимый Host")
        origin=self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}",
                                     f"http://localhost:{self.server.server_port}"):
            discard_small_body()
            return self.fail(403,"Origin не совпадает")
        path=urlsplit(self.path).path
        known=("/api/login","/api/logout","/api/demo-login",
               "/api/upload","/api/delete-upload","/api/ask","/api/ai/start","/api/ai/stop",
               "/api/engineering/p11/finding")
        if path not in known:
            try:
                pending=int(self.headers.get("Content-Length","0"))
                if 0<pending<=4096:self.rfile.read(pending)
            except (ValueError,OSError):pass
            return self.fail(404,"Не найдено")
        max_size=ai_extension.MAX_UPLOAD_JSON if path=="/api/upload" else 4096
        try:
            n=int(self.headers.get("Content-Length","0"))
            if n<1:return self.fail(400,"Пустой запрос")
            if n>max_size:return self.fail(413,"Превышен допустимый размер")
        except ValueError:
            return self.fail(400,"Отсутствует размер запроса")
        with closing(connect(self.database)) as db:
            session=None
            if path not in ("/api/login","/api/demo-login"):
                session=self.user(db)
                if session is None:return self.fail(401,"Требуется вход")
                if not hmac.compare_digest(self.headers.get("X-CSRF-Token",""),session["csrf"]):
                    return self.fail(403,"Проверка CSRF не пройдена")
            try:
                form=json.loads(self.rfile.read(n))
            except (ValueError,UnicodeDecodeError):
                return self.fail(400,"Неверный JSON")
            if not isinstance(form,dict):
                return self.fail(400,"Нужен JSON-объект")
            if path=="/api/demo-login":
                if self.client_address[0] not in ("127.0.0.1","::1") or not origin:
                    return self.fail(403,"Демо-вход доступен только на этом компьютере")
                role=form.get("role")
                if role not in ("technologist","engineer","finance","director"):
                    return self.fail(400,"Неизвестная демонстрационная роль")
                row=db.execute("SELECT id FROM account WHERE id=?",(role,)).fetchone()
                if row is None:return self.fail(503,"Нет демонстрационного аккаунта")
                token=secrets.token_urlsafe(36);csrf=secrets.token_urlsafe(20)
                db.execute("INSERT INTO session VALUES (?,?,?,?)",
                    (hashlib.sha256(token.encode()).hexdigest(),role,csrf,int(time.time())+3600*8))
                audit(db,role,"demo_login",role,"ok")
                cookie=f"demo_session={token}; HttpOnly; SameSite=Strict; Path=/"
                return self.reply({"ok":True,"demo":True},headers={"Set-Cookie":cookie})
            if path=="/api/login":
                username=form.get("username","");password=form.get("password","")
                if not isinstance(username,str) or not isinstance(password,str):
                    return self.fail(400)
                row=db.execute("SELECT * FROM account WHERE id=?",(username,)).fetchone()
                salt=row["salt"] if row else "00"*16
                check=password_hash(password,salt)
                if row is None or not hmac.compare_digest(check,row["passhash"]):
                    return self.fail(401,"Неверные данные для входа")
                token=secrets.token_urlsafe(36);csrf=secrets.token_urlsafe(20)
                db.execute("INSERT INTO session VALUES (?,?,?,?)",
                    (hashlib.sha256(token.encode()).hexdigest(),username,csrf,int(time.time())+3600*8))
                audit(db,username,"login",username,"ok")
                cookie=f"demo_session={token}; HttpOnly; SameSite=Strict; Path=/"
                return self.reply({"ok":True},headers={"Set-Cookie":cookie})
            subject=session["account_id"]
            if path=="/api/engineering/p11/finding":
                if subject!="engineer" or not allowed(db,subject,"engineering"):
                    return self.fail(403,"Только учебный инженер может внести заметку")
                initial_fingerprint=scope_fingerprint(db,subject)
                @contextmanager
                def review_access_gate():
                    # Hold the auth DB write lock while the tiny review commit
                    # is performed, serializing revocation against that write.
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        current=self.user(db)
                        if (current is None or current["account_id"]!=subject or
                                not allowed(db,subject,"engineering") or
                                scope_fingerprint(db,subject)!=initial_fingerprint):
                            raise visual_review.ReviewError("ACCESS_CHANGED")
                        yield
                        db.execute("COMMIT")
                    except BaseException:
                        db.execute("ROLLBACK")
                        raise
                try:
                    result=visual_review.record_finding(
                        form.get("q"),form.get("annotation"),form.get("feature"),
                        form.get("decision"),form.get("note"),subject,form.get("event_id"),
                        axis=form.get("axis","x"),access_gate=review_access_gate)
                except visual_review.ReviewError as error:
                    if str(error)=="ACCESS_CHANGED":
                        return self.fail(409,"Права изменились во время сохранения")
                    if str(error)=="REVIEW_CHAIN_BROKEN" or str(error).startswith("WITNESS_"):
                        return self.fail(409,"Контрольная запись аудита не совпадает; запись отклонена")
                    if str(error)=="IDEMPOTENCY_CONFLICT":
                        return self.fail(409,"ID уже занят другой заметкой")
                    if str(error) in {"INVALID_ANNOTATION_ID","INVALID_FEATURE_ID",
                                      "INVALID_DECISION","NOTE_LENGTH_INVALID",
                                      "NOTE_CONTROL_CHARACTER","EVENT_ID_INVALID",
                                      "FEATURE_TYPE_MISMATCH","AXIS_INVALID"}:
                        return self.fail(422,"Некорректные данные заметки")
                    return self.fail(409,"Нет проверенной пары источников")
                except (feature_verification.FeatureError,dimension_trace.DimensionError,
                        engineering_corpus.CorpusError,engineering_inspection.InspectionError):
                    return self.fail(409,"Происхождение документа не подтверждено")
                return self.reply(result,200 if result["duplicate"] else 201)
            if path=="/api/logout":
                cookies=http.cookies.SimpleCookie();cookies.load(self.headers.get("Cookie",""))
                raw=cookies["demo_session"].value
                db.execute("DELETE FROM session WHERE token_hash=?",
                           (hashlib.sha256(raw.encode()).hexdigest(),))
                return self.reply({"ok":True},headers={
                    "Set-Cookie":"demo_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"})
            if path=="/api/upload":
                try:
                    item=ai_extension.ingest(db,subject,form,audit)
                    return self.reply(item,200 if item.get("duplicate") else 201)
                except PermissionError as e:return self.fail(403,str(e))
                except ValueError as e:return self.fail(422,str(e))
            if path=="/api/delete-upload":
                if not ai_extension.delete_uploaded(db,subject,form.get("id"),audit):
                    return self.fail(404,"Загруженный файл не найден")
                return self.reply({"deleted":True,"id":form.get("id")})
            if path=="/api/ai/start":
                try:return self.reply(ai_extension.start(),202)
                except ValueError as e:return self.fail(409,str(e))
            if path=="/api/ai/stop":
                try:return self.reply(ai_extension.stop())
                except ValueError as e:return self.fail(409,str(e))
            if path=="/api/ask":
                question=form.get("question")
                if not isinstance(question,str) or not question.strip() or len(question)>ai_extension.MAX_ASK:
                    return self.fail(400,"Вопрос должен содержать от 1 до 600 символов.")
                question=question.strip()
                evidence=ai_extension.retrieve(db,subject,question)
                if not evidence:
                    return self.reply({"answer":"По доступным документам ответ не найден. "
                                               "Уточните обозначение или загрузите подходящий документ.",
                                       "sources":[],"mode":"no_sources"})
                original_fingerprint=scope_fingerprint(db,subject)
                model_state=ai_extension.status()
                if model_state.get("status")!="ready":
                    if self.user(db) is None or scope_fingerprint(db,subject)!=original_fingerprint:
                        return self.fail(409,"Права доступа изменились во время ответа. Повторите запрос.")
                    for x in evidence:
                        if get_document(db,subject,x["doc_id"]) is None:
                            return self.fail(409,"Доступ к источнику изменился.")
                    llm_status=str(model_state.get("status") or "stopped")
                    fallback_messages={
                        "missing":"Найдены разрешённые фрагменты документов. Локальная модель не установлена (FACTORY_AI_MODEL / FACTORY_AI_SERVER); проверьте исходные фрагменты ниже.",
                        "starting":"Найдены разрешённые фрагменты документов. Локальная модель сейчас загружается; проверьте исходные фрагменты ниже или повторите запрос позже.",
                        "conflict":"Найдены разрешённые фрагменты документов. Порт локальной модели занят другим процессом; генерация отключена, проверьте исходные фрагменты ниже.",
                        "error":"Найдены разрешённые фрагменты документов. Локальная модель завершилась с ошибкой; проверьте исходные фрагменты ниже.",
                    }
                    fallback_answer=fallback_messages.get(
                        llm_status,
                        "Найдены разрешённые фрагменты документов. Локальная модель сейчас не запущена; проверьте исходные фрагменты ниже или запустите модель.")
                    sources=[{"number":i,"doc_id":e["doc_id"],"title":e["title"],
                              "page":e["page"],"excerpt":e["body"][:400],
                              "extraction_method":e.get("extraction_method","text"),
                              "identifier_match":e.get("identifier_match"),
                              "cited":False}
                             for i,e in enumerate(evidence,1)]
                    audit(db,subject,"ai_question","allowed_scopes:retrieval_only","ok")
                    return self.reply({"answer":fallback_answer,"citations":[],
                                       "citation_warning":True,"filtered_lines":0,
                                       "sources":sources,"mode":"retrieval_only",
                                       "llm_status":llm_status,
                                       "verification_required":True})
                try:
                    generated=ai_extension.generate(question,evidence)
                except ai_extension.BusyError as e:return self.fail(429,str(e))
                except ValueError as e:return self.fail(503,str(e))
                except Exception:
                    return self.fail(502,"Не удалось получить ответ от локальной модели.")
                if self.user(db) is None or scope_fingerprint(db,subject)!=original_fingerprint:
                    return self.fail(409,"Права доступа изменились во время ответа. Повторите запрос.")
                for x in evidence:
                    if get_document(db,subject,x["doc_id"]) is None:
                        return self.fail(409,"Доступ к источнику изменился.")
                sources=[{"number":i,"doc_id":e["doc_id"],"title":e["title"],
                          "page":e["page"],"excerpt":e["body"][:400],
                          "extraction_method":e.get("extraction_method","text"),
                          "identifier_match":e.get("identifier_match"),
                          "cited":i in generated.get("citations",[])}
                         for i,e in enumerate(evidence,1)]
                audit(db,subject,"ai_question","allowed_scopes","ok")
                needs_review=generated.get("citation_warning",False) or any(
                    e["extraction_method"]=="ocr" or e["identifier_match"] in ("homoglyph_candidate","ocr_fuzzy_candidate")
                    for e in sources)
                return self.reply({**generated,"sources":sources,"mode":"local_llm",
                                   "verification_required":needs_review})
            return self.fail(404,"Не найдено")


def serve(path=DB, host='127.0.0.1',port=19081,credentials=None):
    db_path=pathlib.Path(path)
    creds_path=pathlib.Path(credentials) if credentials is not None else (
        CREDS if db_path==DB else db_path.parent/"demo_credentials.txt")
    httpd=ThreadingHTTPServer((host,port),Handler)
    try:
        initialize(db_path,creds_path,url=f"http://{host}:{httpd.server_port}")
        httpd.database_path=db_path
        print(f"FACTORY_MVP_READY http://{host}:{httpd.server_port}",flush=True)
        print(f"CREDENTIALS_FILE {creds_path}",flush=True)
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=19081)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--db',default=str(DB))
    args=parser.parse_args()
    if args.host not in ('127.0.0.1','localhost'):
        parser.error('This synthetic demo only supports localhost')
    serve(path=pathlib.Path(args.db),host=args.host,port=args.port)
