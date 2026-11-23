"""Manufacturing AI v0.6 — executable reference security/engineering invariants.

NOT a production authorizer, PG RLS, Qdrant/Haystack adapter, LLM, CAD parser,
SSE security proxy, cryptographic isolation, or production deployment.  SQLite
is a deterministic harness to test the domain contract before integration.

Tests must not be confused with proof of deployed-system security.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unittest
from dataclasses import dataclass
from datetime import date

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE policy_clock(id INTEGER PRIMARY KEY CHECK(id = 1), epoch INTEGER NOT NULL);
INSERT INTO policy_clock VALUES(1, 1);
CREATE TABLE acl_scope(scope_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 1);
CREATE TABLE subject_grant(subject_id TEXT NOT NULL, scope_id TEXT NOT NULL,
    PRIMARY KEY(subject_id,scope_id), FOREIGN KEY(scope_id) REFERENCES acl_scope);
CREATE TABLE document_scope(document_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL,
    registry_complete INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(scope_id) REFERENCES acl_scope);
CREATE TABLE notice(notice_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, source TEXT NOT NULL,
    FOREIGN KEY(scope_id) REFERENCES acl_scope);
CREATE TABLE notice_target(notice_id TEXT NOT NULL, document_id TEXT NOT NULL, artifact_ref TEXT NOT NULL,
    PRIMARY KEY(notice_id,document_id,artifact_ref),
    FOREIGN KEY(notice_id) REFERENCES notice, FOREIGN KEY(document_id) REFERENCES document_scope);
CREATE TABLE production_lot(lot_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL,
    produced_on TEXT, serial_no INTEGER, order_id TEXT,
    revision_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(revision_confirmed IN (0,1)));
CREATE TABLE implementation_rule(notice_id TEXT NOT NULL, plant_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('DATE','SERIAL','ORDER','DEFERRED')),
    cutoff_value TEXT, approved INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0,1)),
    PRIMARY KEY(notice_id,plant_id), FOREIGN KEY(notice_id) REFERENCES notice);
CREATE TABLE lot_directive(lot_id TEXT NOT NULL, notice_id TEXT NOT NULL,
    instruction TEXT NOT NULL CHECK(instruction IN ('use','rework','do_not_use','unaffected')),
    PRIMARY KEY(lot_id,notice_id), FOREIGN KEY(lot_id) REFERENCES production_lot,
    FOREIGN KEY(notice_id) REFERENCES notice);
CREATE TABLE drawing(drawing_id TEXT PRIMARY KEY);
CREATE TABLE variant(variant_id TEXT PRIMARY KEY,designation TEXT NOT NULL UNIQUE);
CREATE TABLE drawing_applicability(drawing_id TEXT NOT NULL,variant_id TEXT NOT NULL,
    PRIMARY KEY(drawing_id,variant_id),FOREIGN KEY(drawing_id) REFERENCES drawing,
    FOREIGN KEY(variant_id) REFERENCES variant);
CREATE TABLE user_draft(draft_id TEXT PRIMARY KEY,owner TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE conversation(thread_id TEXT PRIMARY KEY, owner TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE conversation_source(thread_id TEXT NOT NULL, resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,PRIMARY KEY(thread_id,resource_kind,resource_id),
    FOREIGN KEY(thread_id) REFERENCES conversation);
CREATE TABLE conversation_share(thread_id TEXT NOT NULL,recipient TEXT NOT NULL,
    PRIMARY KEY(thread_id,recipient),FOREIGN KEY(thread_id) REFERENCES conversation);
-- Authoritative text and lexical index are in a protected SQL boundary;
-- Qdrant-like candidate points carry IDs and vectors only.
CREATE TABLE chunk_source(chunk_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,body TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES document_scope);
CREATE VIRTUAL TABLE chunk_fts USING fts5(chunk_id UNINDEXED,body,tokenize='unicode61');
"""


@dataclass(frozen=True)
class IndexedPoint:
    point_id: str
    document_id: str
    scope_id: str
    dense_vector: tuple[float, ...] = (0.0, 0.0)
    sparse_vector: tuple[tuple[int, float], ...] = ()
    # No body/text, access_epoch, grants or personal names in this search payload.


@dataclass(frozen=True)
class LotDecision:
    state: str  # NEW_REVISION / WIP_USE / WIP_REWORK / WIP_BLOCK / WIP_UNAFFECTED / NEEDS_REVIEW
    reason: str


class ReferencePolicy:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.executescript(SCHEMA)
        self.index: dict[str, IndexedPoint] = {}

    def create_scope(self, scope: str):
        self.db.execute('INSERT OR IGNORE INTO acl_scope(scope_id) VALUES(?)',(scope,))

    def _clock_bump(self):
        self.db.execute('UPDATE policy_clock SET epoch=epoch+1 WHERE id=1')

    def _scope_bump(self, scope: str):
        self.db.execute('UPDATE acl_scope SET epoch=epoch+1 WHERE scope_id=?',(scope,))

    def clock(self) -> int:
        return self.db.execute('SELECT epoch FROM policy_clock WHERE id=1').fetchone()[0]

    def scope_epoch(self, scope: str) -> int:
        row=self.db.execute('SELECT epoch FROM acl_scope WHERE scope_id=?',(scope,)).fetchone()
        if row is None: raise KeyError(scope)
        return row[0]

    def add_document(self, doc: str, scope: str, *, registry_complete: bool=False):
        self.create_scope(scope)
        self.db.execute('INSERT INTO document_scope VALUES(?,?,?)',(doc,scope,int(registry_complete)))
        self.index[doc] = IndexedPoint(doc,doc,scope)
        self._scope_bump(scope); self._clock_bump()

    def grant(self,user:str,scope:str):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.create_scope(scope)
            cur=self.db.execute('INSERT OR IGNORE INTO subject_grant VALUES(?,?)',(user,scope))
            if cur.rowcount:
                self._scope_bump(scope);self._clock_bump()
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise

    def revoke(self,user:str,scope:str):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            cur=self.db.execute('DELETE FROM subject_grant WHERE subject_id=? AND scope_id=?',(user,scope))
            if cur.rowcount:
                self._scope_bump(scope);self._clock_bump()
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise

    def move_document(self, doc:str, target_scope:str):
        """Single atomic policy transaction; old vector points stay stale/denied.

        Production uses a PostgreSQL transaction + outbox for async Qdrant repair.
        The global epoch makes old cache keys unusable across overlapping scopes.
        """
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT scope_id FROM document_scope WHERE document_id=?',(doc,)).fetchone()
            if not row: raise KeyError(doc)
            old=row[0]
            if old != target_scope:
                self.create_scope(target_scope)
                self.db.execute('UPDATE document_scope SET scope_id=? WHERE document_id=?',
                                (target_scope,doc))
                self._scope_bump(old)
                self._scope_bump(target_scope)
                self._clock_bump()
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK'); raise

    def _has(self,user:str,scope:str) -> bool:
        return self.db.execute('SELECT EXISTS(SELECT 1 FROM subject_grant '
            'WHERE subject_id=? AND scope_id=?)',(user,scope)).fetchone()[0]==1

    def authorized(self,user:str,doc:str) -> bool:
        row=self.db.execute('SELECT scope_id FROM document_scope WHERE document_id=?',(doc,)).fetchone()
        return bool(row and self._has(user,row[0]))

    def can_read_notice(self,user:str,notice_id:str) -> bool:
        row=self.db.execute('SELECT scope_id FROM notice WHERE notice_id=?',(notice_id,)).fetchone()
        return bool(row and self._has(user,row[0]))

    def add_notice(self,notice_id:str,scope:str,source:str,targets:tuple[tuple[str,str],...]):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.create_scope(scope)
            self.db.execute('INSERT INTO notice VALUES(?,?,?)',(notice_id,scope,source))
            for doc,artifact in targets:
                self.db.execute('INSERT INTO notice_target VALUES(?,?,?)',(notice_id,doc,artifact))
            self._scope_bump(scope);self._clock_bump()
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise

    def document_currentness(self,user:str,doc:str) -> dict | None:
        """Same non-disclosing failure for secret notice and incomplete registry.

        This prototype does not implement effectivity certification, hence even
        all-visible notices are REVIEW_REQUIRED until a human verified them.
        """
        row=self.db.execute('SELECT registry_complete FROM document_scope WHERE document_id=?',
                            (doc,)).fetchone()
        if not row or not self.authorized(user,doc):return None
        notices=[r[0] for r in self.db.execute('SELECT DISTINCT notice_id FROM notice_target WHERE document_id=?',(doc,))]
        if not row[0] or any(not self.can_read_notice(user,n) for n in notices):
            return {'state':'CURRENTNESS_UNVERIFIED','visible_notices':()}
        if notices:
            return {'state':'REVIEW_REQUIRED','visible_notices':tuple(sorted(notices))}
        return {'state':'NO_REGISTERED_CHANGES','visible_notices':()}

    def content(self,user:str,doc:str,body:str) -> str | None:
        # Represents *source hydration*, not trusting stale Qdrant ACL fields.
        return body if self.authorized(user,doc) else None

    def add_chunk(self,chunk_id:str,doc:str,body:str):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO chunk_source VALUES(?,?,?)',(chunk_id,doc,body))
            self.db.execute('INSERT INTO chunk_fts VALUES(?,?)',(chunk_id,body))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise
        scope=self.db.execute('SELECT scope_id FROM document_scope WHERE document_id=?',(doc,)).fetchone()[0]
        self.index[chunk_id]=IndexedPoint(chunk_id,doc,scope)

    def lexical_candidates(self,query:str,limit:int=20) -> list[str]:
        """SQLite FTS5 test double for PostgreSQL tsvector, not pg_search/BM25."""
        tokens=re.findall(r'\w+',query,flags=re.UNICODE)
        if not tokens: return []
        fts=' OR '.join('"'+t.replace('"','')+'"' for t in tokens[:8])
        return [r[0] for r in self.db.execute(
            'SELECT chunk_id FROM chunk_fts WHERE chunk_fts MATCH ? ORDER BY bm25(chunk_fts) LIMIT ?',
            (fts,max(1,min(limit,100))))]

    def hydrate(self,user:str,chunk_id:str) -> str|None:
        row=self.db.execute('SELECT document_id,body FROM chunk_source WHERE chunk_id=?',(chunk_id,)).fetchone()
        if not row: return None
        return row[1] if self.authorized(user,row[0]) else None

    def read_cached(self,user:str,cache:dict,key:str) -> str|None:
        """Cache misses if policy generation changed; all references rechecked."""
        entry=cache.get(key)
        if not entry or not entry.get('sources') or key != self.cache_key(user,entry['route'],entry['params']):return None
        if not all(self.authorized_resource(user,kind,rid) for kind,rid in entry['sources']):return None
        return entry['body']

    def cache_key(self,user:str,route:str,params:dict) -> str:
        return cache_key(user,self.clock(),route,params)

    def authorized_resource(self,user:str,kind:str,rid:str)->bool:
        if kind=='document':return self.authorized(user,rid)
        if kind=='notice':return self.can_read_notice(user,rid)
        if kind=='draft':
            row=self.db.execute('SELECT owner FROM user_draft WHERE draft_id=?',(rid,)).fetchone()
            return bool(row and row[0]==user)
        return False

    def write_draft(self,user:str,draft_id:str,body:str):
        self.db.execute('INSERT INTO user_draft VALUES(?,?,?)',(draft_id,user,body))

    def read_draft(self,user:str,draft_id:str)->str|None:
        row=self.db.execute('SELECT body FROM user_draft WHERE draft_id=? AND owner=?',
                            (draft_id,user)).fetchone()
        return row[0] if row else None

    def create_thread(self,owner:str,thread_id:str,body:str,sources:tuple[tuple[str,str],...]):
        """Provenance completeness is a caller contract; untracked tool outputs forbidden."""
        if not sources:raise ValueError('source-backed thread must have provenance')
        if not all(self.authorized_resource(owner,k,r) for k,r in sources):raise PermissionError
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO conversation VALUES(?,?,?)',(thread_id,owner,body))
            for k,r in sources:self.db.execute('INSERT INTO conversation_source VALUES(?,?,?)',(thread_id,k,r))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise

    def share_thread(self,owner:str,thread_id:str,recipient:str):
        row=self.db.execute('SELECT owner FROM conversation WHERE thread_id=?',(thread_id,)).fetchone()
        if not row or row[0]!=owner:raise PermissionError
        if self.read_thread(owner,thread_id) is None:raise PermissionError
        self.db.execute('INSERT OR IGNORE INTO conversation_share VALUES(?,?)',(thread_id,recipient))

    def read_thread(self,user:str,thread_id:str)->str|None:
        row=self.db.execute('SELECT owner,body FROM conversation WHERE thread_id=?',(thread_id,)).fetchone()
        if not row:return None
        if row[0]!=user and not self.db.execute('SELECT EXISTS(SELECT 1 FROM conversation_share '
                'WHERE thread_id=? AND recipient=?)',(thread_id,user)).fetchone()[0]:return None
        sources=list(self.db.execute('SELECT resource_kind,resource_id FROM conversation_source '
                                      'WHERE thread_id=?',(thread_id,)))
        if not sources or not all(self.authorized_resource(user,k,r) for k,r in sources):return None
        return row[1]


VISUAL_ALIAS=str.maketrans({'А':'A','В':'B','С':'C','Е':'E','Н':'H','К':'K',
    'М':'M','О':'O','Р':'P','Т':'T','Х':'X'})


def lookup_authoritative(query:str,allowed_values:set[str])->tuple[str,list[str]]:
    if query in allowed_values:return 'exact',[query]
    key=query.upper().translate(VISUAL_ALIAS)
    alternatives=sorted(v for v in allowed_values if v.upper().translate(VISUAL_ALIAS)==key)
    if len(alternatives)==1:return 'candidate_requires_registry_confirmation',alternatives
    if len(alternatives)>1:return 'ambiguous_requires_review',alternatives
    return 'not_found',[]


def cache_key(subject_id:str,epoch:int,route:str,parameters:dict)->str:
    payload=json.dumps([subject_id,epoch,route,parameters],ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def lot_instruction(db:sqlite3.Connection,lot_id:str,notice_id:str)->LotDecision:
    """Implement effectivity first, then evaluate old-stock disposition.

    Supported test-double rule: one APPROVED DATE cutoff per plant/notice.
    Real rules also depend on serial, order, organization, stock exhaustion.
    Never guess those from dates; return NEEDS_REVIEW until modeled/approved.
    """
    lot=db.execute('SELECT plant_id,produced_on,revision_confirmed FROM production_lot WHERE lot_id=?',
                   (lot_id,)).fetchone()
    rule=db.execute('SELECT kind,cutoff_value,approved FROM implementation_rule '
                    'WHERE notice_id=? AND plant_id=?',
                    (notice_id,lot[0] if lot else '')).fetchone()
    if not lot or not rule or not rule[2]:return LotDecision('NEEDS_REVIEW','no approved effectivity')
    kind,cutoff,_=rule
    if kind!='DATE' or not cutoff or not lot[1]:
        return LotDecision('NEEDS_REVIEW','complex or missing effectivity requires review')
    try:after=date.fromisoformat(lot[1])>=date.fromisoformat(cutoff)
    except ValueError:return LotDecision('NEEDS_REVIEW','invalid manufacturing or cutoff date')
    if after:
        if lot[2]:return LotDecision('NEW_REVISION','confirmed lot built under the new revision')
        return LotDecision('NEEDS_REVIEW','past cutoff but actual revision unconfirmed')
    directive=db.execute('SELECT instruction FROM lot_directive WHERE lot_id=? AND notice_id=?',
                         (lot_id,notice_id)).fetchone()
    if directive is None:return LotDecision('NEEDS_REVIEW','pre-cutoff lot, missing stock disposition')
    return LotDecision({'use':'WIP_USE','rework':'WIP_REWORK','do_not_use':'WIP_BLOCK',
                        'unaffected':'WIP_UNAFFECTED'}[directive[0]],'approved old-stock instruction')


class InvariantsV06(unittest.TestCase):
    def setUp(self):self.p=ReferencePolicy()
    def tearDown(self):self.p.db.close()

    def test_01_revoke_blocks_stale_search_without_index_writes(self):
        p=self.p;p.add_document('d','A');p.grant('u','A');before=p.index.copy();e=p.clock()
        self.assertTrue(p.authorized('u','d'))
        p.revoke('u','A')
        self.assertFalse(p.authorized('u','d'));self.assertEqual(before,p.index)
        self.assertEqual(p.clock(),e+1)

    def test_02_move_increments_both_scopes_and_global_epoch_atomically(self):
        p=self.p;p.add_document('d','A');p.create_scope('B');p.grant('u','A')
        before=(p.scope_epoch('A'),p.scope_epoch('B'),p.clock());point=p.index['d']
        p.move_document('d','B')
        self.assertEqual((p.scope_epoch('A'),p.scope_epoch('B'),p.clock()),tuple(x+1 for x in before))
        self.assertEqual(point,p.index['d']);self.assertFalse(p.authorized('u','d'))

    def test_03_cache_after_move_misses_even_with_stale_index_and_key(self):
        p=self.p;p.add_document('d','A');p.grant('u','A')
        before=p.cache_key('u','answer',{'q':'x'})
        cached={before:{'route':'answer','params':{'q':'x'},'body':'secret',
                        'sources':(('document','d'),)}}
        self.assertEqual(p.read_cached('u',cached,before),'secret')
        p.move_document('d','B')
        self.assertNotEqual(before,p.cache_key('u','answer',{'q':'x'}))
        self.assertIsNone(p.read_cached('u',cached,before))

    def test_04_no_effect_on_same_scope_move_and_failed_move_rolled_back(self):
        p=self.p;p.add_document('d','A');e=p.clock()
        p.move_document('d','A');self.assertEqual(p.clock(),e)
        with self.assertRaises(KeyError):p.move_document('missing','B')
        self.assertEqual(p.clock(),e)
        self.assertEqual(p.db.execute("SELECT count(*) FROM acl_scope WHERE scope_id='B'").fetchone()[0],0)

    def test_05_group_drawing_many_variants(self):
        p=self.p;p.db.execute("INSERT INTO drawing VALUES('draw')")
        for num in ('01','02','25'):
            v='АБВГ.712345.004-'+num
            p.db.execute('INSERT INTO variant VALUES(?,?)',(v,v))
            p.db.execute("INSERT INTO drawing_applicability VALUES('draw',?)",(v,))
        self.assertEqual(p.db.execute("SELECT count(*) FROM drawing_applicability WHERE drawing_id='draw'").fetchone()[0],3)

    def test_06_old_stock_requires_disposition_and_new_lot_uses_approved_rule(self):
        p=self.p;p.create_scope('A');p.add_notice('n','A','ИИ-17',())
        p.db.execute("INSERT INTO implementation_rule VALUES('n','plant','DATE','2026-03-01',1)")
        p.db.execute("INSERT INTO production_lot VALUES('old','plant','2026-02-01',NULL,NULL,0)")
        p.db.execute("INSERT INTO production_lot VALUES('new','plant','2026-04-01',NULL,NULL,1)")
        p.db.execute("INSERT INTO lot_directive VALUES('old','n','rework')")
        self.assertEqual(lot_instruction(p.db,'old','n').state,'WIP_REWORK')
        self.assertEqual(lot_instruction(p.db,'new','n').state,'NEW_REVISION')

    def test_07_new_lot_no_actual_revision_confirmation_still_review(self):
        p=self.p;p.create_scope('A');p.add_notice('n','A','ИИ',())
        p.db.execute("INSERT INTO implementation_rule VALUES('n','plant','DATE','2026-03-01',1)")
        p.db.execute("INSERT INTO production_lot VALUES('new','plant','2026-04-01',NULL,NULL,0)")
        self.assertEqual(lot_instruction(p.db,'new','n').state,'NEEDS_REVIEW')

    def test_08_old_lot_no_directive_review_and_complex_rule_review(self):
        p=self.p;p.create_scope('A');p.add_notice('n','A','ИИ',())
        p.db.execute("INSERT INTO implementation_rule VALUES('n','plant','DATE','2026-03-01',1)")
        p.db.execute("INSERT INTO production_lot VALUES('old','plant','2026-02-01',NULL,NULL,0)")
        self.assertEqual(lot_instruction(p.db,'old','n').state,'NEEDS_REVIEW')
        p.db.execute("UPDATE implementation_rule SET kind='DEFERRED',cutoff_value=NULL WHERE notice_id='n'")
        self.assertEqual(lot_instruction(p.db,'old','n').state,'NEEDS_REVIEW')

    def test_09_lexical_search_in_sql_not_text_in_index(self):
        p=self.p;p.add_document('d','A');p.grant('u','A')
        p.add_chunk('c1','d','Запасные детали и ремонт станка')
        self.assertIn('c1',p.lexical_candidates('ремонт'))
        point=p.index['c1']
        self.assertNotIn('body',vars(point));self.assertNotIn('текст',repr(point))
        self.assertEqual(p.hydrate('u','c1'),'Запасные детали и ремонт станка')
        p.revoke('u','A');self.assertIsNone(p.hydrate('u','c1'))

    def test_10_hidden_notice_does_not_become_oracle(self):
        p=self.p;p.add_document('drawing','workshop',registry_complete=True)
        p.add_document('other','workshop',registry_complete=False)
        p.grant('worker','workshop');p.add_notice('n','directors','secret',(('drawing','crop:1'),))
        self.assertEqual(p.document_currentness('worker','drawing'),
                         p.document_currentness('worker','other'))
        self.assertEqual(p.document_currentness('worker','drawing')['visible_notices'],())

    def test_11_authorized_notice_visible_but_not_auto_certified(self):
        p=self.p;p.add_document('drawing','A',registry_complete=True)
        p.add_notice('n','A','ИИ',(('drawing','crop:1'),));p.grant('engineer','A')
        self.assertEqual(p.document_currentness('engineer','drawing'),
                         {'state':'REVIEW_REQUIRED','visible_notices':('n',)})

    def test_12_advisory_does_not_bypass_notice_acl(self):
        p=self.p;p.add_document('drawing','workshop',registry_complete=True)
        p.add_notice('n','secret','cutoff information',(('drawing','crop'),))
        p.grant('u','workshop')
        self.assertIsNone(p.read_thread('u','nonexistent'))
        with self.assertRaises(PermissionError):
            p.create_thread('u','t','secret reference',(('document','drawing'),('notice','n')))

    def test_13_share_denied_when_recipient_lacks_any_source(self):
        p=self.p;p.add_document('drawing','workshop');p.add_notice('n','secret','ИИ',(('drawing','crop'),))
        p.grant('author','workshop');p.grant('author','secret');p.grant('other','workshop')
        p.create_thread('author','t','combined analysis',(('document','drawing'),('notice','n')))
        p.share_thread('author','t','other')
        self.assertIsNone(p.read_thread('other','t'))
        p.grant('other','secret');self.assertEqual(p.read_thread('other','t'),'combined analysis')
        p.revoke('other','secret');self.assertIsNone(p.read_thread('other','t'))

    def test_14_share_invalidated_after_document_move(self):
        p=self.p;p.add_document('d','A');p.grant('author','A');p.grant('recipient','A')
        p.create_thread('author','t','confidential',(('document','d'),))
        p.share_thread('author','t','recipient')
        self.assertIsNotNone(p.read_thread('recipient','t'))
        p.move_document('d','B');self.assertIsNone(p.read_thread('recipient','t'))

    def test_15_private_draft_not_shared_as_factual_answer(self):
        p=self.p;p.write_draft('author','draft','photo of defect')
        self.assertIsNone(p.read_draft('u','draft'))
        p.create_thread('author','t','draft reasoning',(('draft','draft'),))
        p.share_thread('author','t','u');self.assertIsNone(p.read_thread('u','t'))

    def test_16_confusables_are_candidates_not_silent_rewrite(self):
        self.assertEqual(lookup_authoritative('12X18H10T',{'12Х18Н10Т'}),
                         ('candidate_requires_registry_confirmation',['12Х18Н10Т']))
        self.assertEqual(lookup_authoritative('А0',{'А0','АО'}),('exact',['А0']))
        self.assertEqual(lookup_authoritative('АО',{'А0','АО'}),('exact',['АО']))

    def test_17_bogus_fts_query_cannot_inject_sql_or_read_hidden_chunks(self):
        p=self.p;p.add_document('d','A');p.add_chunk('c','d','Секретный техпроцесс')
        self.assertIn('c',p.lexical_candidates('Секретный" OR *'))
        self.assertIsNone(p.hydrate('u','c'))

    def test_18_cache_key_subject_and_epoch(self):
        self.assertNotEqual(cache_key('u',1,'metric',{'q':1}),cache_key('u',2,'metric',{'q':1}))
        self.assertNotEqual(cache_key('u',1,'metric',{'q':1}),cache_key('v',1,'metric',{'q':1}))

    def test_19_revoke_notice_mid_thread_denies_even_owner(self):
        p=self.p;p.add_document('drawing','A');p.add_notice('n','B','secret',(('drawing','crop'),))
        p.grant('author','A');p.grant('author','B')
        p.create_thread('author','t','answer',(('document','drawing'),('notice','n')))
        self.assertEqual(p.read_thread('author','t'),'answer')
        p.revoke('author','B');self.assertIsNone(p.read_thread('author','t'))

    def test_21_cached_answer_revoked_user_denied(self):
        p=self.p;p.add_document('d','A');p.grant('u','A')
        k=p.cache_key('u','chat',{'id':42})
        cache={k:{'route':'chat','params':{'id':42},'body':'answer',
                  'sources':(('document','d'),)}}
        self.assertEqual(p.read_cached('u',cache,k),'answer')
        p.revoke('u','A')
        self.assertIsNone(p.read_cached('u',cache,k))

    def test_22_new_notice_invalidates_cached_base_answer(self):
        p=self.p;p.add_document('drawing','A');p.grant('u','A')
        k=p.cache_key('u','chat',{'part':'X'})
        cache={k:{'route':'chat','params':{'part':'X'},'body':'old claim',
                  'sources':(('document','drawing'),)}}
        self.assertEqual(p.read_cached('u',cache,k),'old claim')
        p.add_notice('n','secret','ИИ',(('drawing','crop'),))
        self.assertIsNone(p.read_cached('u',cache,k))

    def test_23_empty_cache_provenance_never_trusted(self):
        p=self.p;p.grant('u','A')
        k=p.cache_key('u','chat',{'x':1})
        cache={k:{'route':'chat','params':{'x':1},'body':'unattributed fact','sources':()}}
        self.assertIsNone(p.read_cached('u',cache,k))

    def test_24_wip_unaffected_only_for_old_lot(self):
        p=self.p;p.create_scope('A');p.add_notice('n','A','ИИ',())
        p.db.execute("INSERT INTO implementation_rule VALUES('n','plant','DATE','2026-03-01',1)")
        p.db.execute("INSERT INTO production_lot VALUES('old','plant','2026-02-01',NULL,NULL,0)")
        p.db.execute("INSERT INTO production_lot VALUES('new','plant','2026-04-01',NULL,NULL,1)")
        p.db.execute("INSERT INTO lot_directive VALUES('old','n','unaffected')")
        self.assertEqual(lot_instruction(p.db,'old','n').state,'WIP_UNAFFECTED')
        self.assertEqual(lot_instruction(p.db,'new','n').state,'NEW_REVISION')

    def test_20_failed_notice_registration_rolls_back_entire_operation(self):
        p=self.p;p.add_document('drawing','A');e=p.clock()
        with self.assertRaises(sqlite3.IntegrityError):
            p.add_notice('n','secret','ИИ',(('missing','crop'),))
        self.assertEqual(p.clock(),e)
        self.assertEqual(p.db.execute('SELECT count(*) FROM notice').fetchone()[0],0)
        self.assertEqual(p.db.execute("SELECT count(*) FROM acl_scope WHERE scope_id='secret'").fetchone()[0],0)


if __name__ == '__main__':unittest.main(verbosity=2)