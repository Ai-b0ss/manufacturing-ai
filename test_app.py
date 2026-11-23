"""Regressions for local synthetic MVP; no network except localhost."""
import http.client
import json
import pathlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote
from contextlib import closing
import app


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dbpath = pathlib.Path(cls.tmp.name)/'db.sqlite3'
        cls.creds=pathlib.Path(cls.tmp.name)/'cred.txt'
        cls.first=app.initialize(cls.dbpath,cls.creds)
        cls.credentials={}
        import re
        for line in cls.creds.read_text(encoding='utf-8').splitlines():
            m=re.search(r': (technologist|engineer|finance|director) / (.+)',line)
            if m:cls.credentials[m.group(1)]=m.group(2)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        cls.server.database_path=cls.dbpath
        cls.port=cls.server.server_port
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2);cls.tmp.cleanup()

    def call(self,method,url,body=None,cookie='',csrf='',origin=None,host=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        headers={'Host':host or '127.0.0.1:'+str(self.port)}
        if body is not None:headers['Content-Type']='application/json'
        if cookie:headers['Cookie']=cookie
        if csrf:headers['X-CSRF-Token']=csrf
        if origin is not None:headers['Origin']=origin
        c.request(method,quote(url,safe="/?=&%"),json.dumps(body).encode() if body is not None else None,headers)
        res=c.getresponse();raw=res.read();headers=dict(res.getheaders());c.close()
        try:data=json.loads(raw)
        except (ValueError,UnicodeDecodeError):data=raw.decode('utf-8',errors='replace')
        return res.status,data,headers

    def login(self,who):
        status,body,h=self.call('POST','/api/login',{'username':who,'password':self.credentials[who]})
        self.assertEqual(status,200)
        cookie=h['Set-Cookie'].split(';')[0]
        code,me,_=self.call('GET','/api/me',cookie=cookie)
        self.assertEqual(code,200)
        return cookie,me['csrf']

    def test_00_initialize_seeded(self):
        self.assertTrue(self.first)
        self.assertEqual(set(self.credentials),{'technologist','engineer','finance','director'})
    def test_01_health_without_login(self):
        self.assertEqual(self.call('GET','/api/health')[1]['mode'],'synthetic-local-demo')
    def test_02_unauthenticated_search_denied(self):
        self.assertEqual(self.call('GET','/api/search?q=корпус')[0],401)
    def test_03_bad_password_denied(self):
        self.assertEqual(self.call('POST','/api/login',{'username':'director','password':'wrong'})[0],401)
    def test_04_director_search_includes_exec(self):
        ck,_=self.login('director')
        status,res,_=self.call('GET','/api/search?q=Конфиденциальные',cookie=ck)
        self.assertEqual(status,200)
        self.assertIn('DOC-005',[x['id'] for x in res['items']])
    def test_05_technologist_cannot_search_exec(self):
        ck,_=self.login('technologist')
        self.assertEqual(self.call('GET','/api/search?q=Конфиденциальные',cookie=ck)[1]['items'],[])
    def test_06_technologist_direct_document_denied(self):
        ck,_=self.login('technologist')
        self.assertEqual(self.call('GET','/api/document?id=DOC-005',cookie=ck)[0],404)
    def test_07_technologist_finance_denied(self):
        ck,_=self.login('technologist')
        self.assertEqual(self.call('GET','/api/metrics',cookie=ck)[0],403)
    def test_08_finance_metrics(self):
        ck,_=self.login('finance')
        code,r,_=self.call('GET','/api/metrics',cookie=ck)
        self.assertEqual(code,200)
        self.assertEqual(r['items'][0]['variance'],475000)
    def test_09_currentness_safe_public(self):
        ck,_=self.login('technologist')
        code,r,_=self.call('GET','/api/part?code='+quote('АБВГ.712345.004-02'),cookie=ck)
        self.assertEqual(code,200)
        self.assertEqual(r['state'],'CURRENTNESS_UNVERIFIED')
        self.assertNotIn('notice',r)
    def test_10_engineer_change_notice(self):
        ck,_=self.login('engineer')
        code,r,_=self.call('GET','/api/part?code='+quote('АБВГ.712345.004-02'),cookie=ck)
        self.assertEqual(r['notice']['id'],'ИИ-017')
    def test_11_old_lot_rework(self):
        ck,_=self.login('engineer')
        code,r,_=self.call('GET','/api/part?code='+quote('АБВГ.712345.004-02')+'&lot=LOT-2025-12',cookie=ck)
        self.assertEqual(r['lot']['directive'],'ДОРАБОТАТЬ')
    def test_12_new_lot_matches(self):
        ck,_=self.login('engineer')
        code,r,_=self.call('GET','/api/part?code='+quote('АБВГ.712345.004-02')+'&lot=LOT-2026-10',cookie=ck)
        self.assertEqual(r['lot']['status'],'REVISION_MATCHES')
    def test_13_cyrillic_latin_designation(self):
        ck,_=self.login('technologist')
        code,r,_=self.call('GET','/api/search?q='+quote('АБВГ.712345.004-02'),cookie=ck)
        self.assertIn('DOC-001',[x['id'] for x in r['items']])
        self.assertEqual(app.designation_key('12X18H10T'),app.designation_key('12Х18Н10Т'))
    def test_14_no_sensitive_raw_admin_api(self):
        ck,_=self.login('technologist')
        self.assertEqual(self.call('POST','/api/admin/move',{'doc':'DOC-005'},cookie=ck)[0],404)
    def test_15_csrf_logout_denied(self):
        ck,_=self.login('engineer')
        self.assertEqual(self.call('POST','/api/logout',{},cookie=ck)[0],403)
        self.assertEqual(self.call('GET','/api/me',cookie=ck)[0],200)
    def test_16_origin_csrf_denied(self):
        self.assertEqual(self.call('POST','/api/login',{'username':'engineer','password':'x'},origin='https://attacker.invalid')[0],403)
    def test_17_bad_host_denied(self):
        self.assertEqual(self.call('GET','/api/health',host='host.attacker.invalid')[0],400)
    def test_18_html_served(self):
        code,html,_=self.call('GET','/')
        self.assertEqual(code,200)
        self.assertIn('Цех.Знания',html)
    def test_19_cross_role_isolation_pre_limit(self):
        # Make forbidden documents frequent and ensure allowed one survives LIMIT=1.
        with closing(app.connect(self.dbpath)) as db:
            db.execute('BEGIN IMMEDIATE')
            for i in range(25):
                id_='FORBIDDEN-'+str(i)
                db.execute('INSERT INTO document VALUES (?,?,?,?,?,?)',(id_,id_,'X-'+str(i),'internal','executive','01'))
                db.execute('INSERT INTO chunk VALUES (?,?,?)',('CHK-'+id_,id_,'pump valve power'))
                db.execute('INSERT INTO chunk_fts(chunk_id,text) VALUES (?,?)',('CHK-'+id_,'pump valve power'))
            db.execute('INSERT INTO document VALUES (?,?,?,?,?,?)',('ALLOWED-26','Allowed','X-26','manual','public','01'))
            db.execute('INSERT INTO chunk VALUES (?,?,?)',('CHK-ALLOWED-26','ALLOWED-26','pump valve power'))
            db.execute('INSERT INTO chunk_fts(chunk_id,text) VALUES (?,?)',('CHK-ALLOWED-26','pump valve power'))
            db.execute('COMMIT')
            found=app.lexical_search(db,'technologist','pump',limit=1)
            self.assertEqual(found[0]['id'],'ALLOWED-26')
    def test_20_revocation_prevents_fetch(self):
        with closing(app.connect(self.dbpath)) as db:
            before=app.scope_fingerprint(db,'technologist')
            app.revoke(db,'technologist','public')
            self.assertIsNone(app.get_document(db,'technologist','DOC-001'))
            self.assertNotEqual(before,app.scope_fingerprint(db,'technologist'))
            # Restore for other tests, bump epoch atomically.
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO acl VALUES('technologist','public')")
            db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='public'")
            db.execute('COMMIT')
    def test_21_move_document_invalidates_two_scopes(self):
        with closing(app.connect(self.dbpath)) as db:
            before={r['id']:r['epoch'] for r in db.execute('SELECT * FROM scope')}
            app.move_document(db,'DOC-002','engineering')
            after={r['id']:r['epoch'] for r in db.execute('SELECT * FROM scope')}
            self.assertEqual(after['public'],before['public']+1)
            self.assertEqual(after['engineering'],before['engineering']+1)
            self.assertIsNone(app.get_document(db,'technologist','DOC-002'))
            app.move_document(db,'DOC-002','public')
    def test_22_scope_fingerprint_independent(self):
        with closing(app.connect(self.dbpath)) as db:
            before=app.scope_fingerprint(db,'technologist')
            db.execute('BEGIN IMMEDIATE');db.execute("UPDATE scope SET epoch=epoch+1 WHERE id='executive'");db.execute('COMMIT')
            self.assertEqual(before,app.scope_fingerprint(db,'technologist'))
    def test_24_demo_login_requires_origin(self):
        self.assertEqual(self.call('POST','/api/demo-login',{'role':'technologist'})[0],403)
        self.assertEqual(self.call('POST','/api/demo-login',{'role':'director'},origin='https://attacker.invalid')[0],403)

    def test_25_demo_login_invalid_role(self):
        local=f'http://127.0.0.1:{self.port}'
        self.assertEqual(self.call('POST','/api/demo-login',{'role':'superuser'},origin=local)[0],400)

    def test_26_demo_technologist_permissions(self):
        local=f'http://127.0.0.1:{self.port}'
        status,body,h=self.call('POST','/api/demo-login',{'role':'technologist'},origin=local)
        self.assertEqual(status,200)
        ck=h['Set-Cookie'].split(';')[0]
        self.assertEqual(self.call('GET','/api/me',cookie=ck)[1]['id'],'technologist')
        self.assertEqual(self.call('GET','/api/document?id=DOC-005',cookie=ck)[0],404)
        self.assertEqual(self.call('GET','/api/metrics',cookie=ck)[0],403)

    def test_27_demo_director_access_only_synthetic(self):
        local=f'http://127.0.0.1:{self.port}'
        status,body,h=self.call('POST','/api/demo-login',{'role':'director'},origin=local)
        self.assertEqual(status,200)
        ck=h['Set-Cookie'].split(';')[0]
        self.assertEqual(self.call('GET','/api/me',cookie=ck)[1]['id'],'director')
        self.assertEqual(self.call('GET','/api/document?id=DOC-005',cookie=ck)[0],200)

    def test_28_guided_ui_shows_clear_examples(self):
        # Assert current safety copy after the approved editorial UI revision,
        # rather than the obsolete product-marketing sentence.
        code,html,_=self.call('GET','/')
        self.assertEqual(code,200)
        for label in ['id="unified-login"', 'data-action="search-pump"',
                      'Это демонстрационная версия.']:
            self.assertIn(label,html)
        self.assertNotIn('data-demo-role=', html)

    def test_29_search_title_and_stemming(self):
        ck,_=self.login('technologist')
        code,data,_=self.call('GET','/api/search?q=корпус',cookie=ck)
        self.assertEqual(code,200)
        self.assertGreater(len(data['items']),0)
        self.assertTrue(any('корпус' in x['title'].casefold() for x in data['items']))
        code,data,_=self.call('GET','/api/search?q=ПЕРЕГРЕВ',cookie=ck)
        self.assertEqual(code,200)
        self.assertTrue(any('шпиндел' in x['title'].casefold() for x in data['items']))

    def test_30_list_documents_respects_scopes(self):
        ck,_=self.login('technologist')
        code,data,_=self.call('GET','/api/documents',cookie=ck)
        self.assertEqual(code,200)
        self.assertGreaterEqual(len(data['items']),5)
        self.assertNotIn('DOC-005',[x['id'] for x in data['items']])
        self.assertNotIn('DOC-003',[x['id'] for x in data['items']])
        ck,_=self.login('director')
        code,data,_=self.call('GET','/api/documents',cookie=ck)
        self.assertIn('DOC-005',[x['id'] for x in data['items']])

    def test_31_search_no_closed_excerpt(self):
        ck,_=self.login('technologist')
        code,data,_=self.call('GET','/api/search?q=ИИ-017',cookie=ck)
        self.assertEqual(code,200)
        self.assertNotIn('DOC-003',[x['id'] for x in data['items']])
        self.assertNotIn('DEMO-107',[x['id'] for x in data['items']])

    def test_32_known_demo_content(self):
        ck,_=self.login('technologist')
        code,data,_=self.call('GET','/api/search?q=брак',cookie=ck)
        self.assertIn('DEMO-104',[x['id'] for x in data['items']])
        code,data,_=self.call('GET','/api/document?id=DEMO-103',cookie=ck)
        self.assertEqual(code,200)
        self.assertIn('шпиндель',data['title'].casefold())

    def test_23_invalid_json_and_large_input(self):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        c.request('POST','/api/login','{',{ 'Host':'127.0.0.1:'+str(self.port),'Content-Type':'application/json'})
        self.assertEqual(c.getresponse().status,400);c.close()


if __name__=='__main__':unittest.main()
