"""Actual PostgreSQL 18.7 integration tests. Never mutate the production/demo database.

Runs on one temporary isolated database created and dropped by the admin role.
Tests real PostgreSQL forced RLS, multiple connections, read-after-revoke and
transactional scope move, SQL injection and role permissions.
"""
from __future__ import annotations
import json,re,secrets,threading,time,unittest
from pathlib import Path

import psycopg
from psycopg import sql,errors

from core06 import db_client
from tools.provision_core import psql,CREDS,HOST,PORT

SCHEMA=Path(__file__).resolve().parent.parent/"core06"/"schema.sql"

class PostgreSQLCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db="factory_core_v06_test_"+secrets.token_hex(4)
        with db_client.database_session("postgres",admin=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.db)))
        try:
            psql(cls.db,file=SCHEMA)
            with db_client.database_session(cls.db,admin=True) as con:
                for name,classification in (("open","public"),("engineering","internal"),
                                            ("secret","secret"),("finance","restricted")):
                    con.execute("INSERT INTO core.scope(id,classification) VALUES(%s,%s)",
                                (name,classification))
                for subject in ("alice","bob","carol"):
                    con.execute("INSERT INTO core.subject(id,source) VALUES(%s,'pg_test')",(subject,))
                for actor,scope in (("alice","open"),("alice","engineering"),
                                    ("bob","open"),("bob","secret"),("bob","finance"),
                                    ("carol","open")):
                    con.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES(%s,%s)",
                                (actor,scope))
                for docid,scope,body in (("open-manual","open","Visible hydraulic manual."),
                                         ("engineering-drawing","engineering","Engineering radius 41.5."),
                                         ("secret-salary","secret","Director salary secret value 12345.")):
                    con.execute("INSERT INTO core.document(id,title,designation,scope_id) VALUES(%s,%s,%s,%s)",
                                (docid,docid,docid,scope))
                    con.execute("INSERT INTO core.document_chunk(id,document_id,body) VALUES(%s,%s,%s)",
                                (docid+"-c",docid,body))
                con.execute("""INSERT INTO core.finance(id,title,planned,actual,scope_id)
                               VALUES('secret-finance','Secret budget',10,12,'finance')""")
        except:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls,"db"):return
        with db_client.database_session("postgres",admin=True) as con:
            con.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(cls.db)))

    def as_role(self,actor):
        with db_client.database_session(self.db) as conn:
            return db_client.list_authorized_documents(conn,actor)

    def test_01_postgres_is_real_and_no_gateway_superuser(self):
        with db_client.database_session(self.db) as con:
            role=con.execute("""SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole
                                FROM pg_roles WHERE rolname=current_user""").fetchone()
            self.assertEqual(role,(False,False,False,False))
            version=con.execute("SHOW server_version").fetchone()[0]
            self.assertTrue(version.startswith("18."))
            tables=con.execute("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                                  WHERE n.nspname='core' AND c.relrowsecurity AND c.relforcerowsecurity""").fetchone()[0]
            self.assertEqual(tables,6)

    def test_02_default_denies_all_without_actor(self):
        with db_client.database_session(self.db) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM core.document").fetchone()[0],0)
            self.assertEqual(con.execute("SELECT count(*) FROM core.document_chunk").fetchone()[0],0)
            self.assertEqual(con.execute("SELECT count(*) FROM core.finance").fetchone()[0],0)

    def test_03_actor_scoping_and_no_cross_user_exposure(self):
        alice={r[0] for r in self.as_role("alice")}
        bob={r[0] for r in self.as_role("bob")}
        carol={r[0] for r in self.as_role("carol")}
        self.assertEqual(alice,{"open-manual","engineering-drawing"})
        self.assertEqual(bob,{"open-manual","secret-salary"})
        self.assertEqual(carol,{"open-manual"})

    def test_04_protected_chunks_and_fts_inherit_document_scope(self):
        with db_client.database_session(self.db) as con:
            ids=[r[1] for r in db_client.search_authorized_chunks(con,"alice","hydraulic")]
            self.assertEqual(ids,["open-manual"])
            self.assertEqual(db_client.search_authorized_chunks(con,"alice","salary"),[])
            ids=[r[1] for r in db_client.search_authorized_chunks(con,"bob","salary")]
            self.assertEqual(ids,["secret-salary"])
            self.assertIsNone(db_client.read_authorized_document(con,"alice","secret-salary"))
            self.assertEqual(len(db_client.read_authorized_document(con,"alice","engineering-drawing")["chunks"]),1)

    def test_05_finance_isolated_and_absent_from_tech_search(self):
        with db_client.database_session(self.db) as conn:
            self.assertEqual(db_client.authorized_finance(conn,"alice"),[])
            self.assertEqual(db_client.authorized_finance(conn,"bob")[0][0],"secret-finance")
            self.assertEqual(db_client.authorized_finance(conn,"carol"),[])

    def test_06_role_cannot_read_grants_or_audit(self):
        with db_client.database_session(self.db) as con:
            for table in ("core.scope_grant","core.audit_event","core.subject","core.migration_manifest"):
                with self.subTest(table=table):
                    with self.assertRaises(errors.InsufficientPrivilege):
                        con.execute("SELECT count(*) FROM "+table)

    def test_07_gateway_cannot_mutate_data_or_impersonate_owner(self):
        with db_client.database_session(self.db) as con:
            with self.assertRaises(errors.InsufficientPrivilege):
                con.execute("SET ROLE factory_owner")
            with self.assertRaises(errors.InsufficientPrivilege):
                con.execute("DELETE FROM core.scope_grant")
            with self.assertRaises(errors.InsufficientPrivilege):
                con.execute("SELECT core.revoke_scope_access('alice','engineering')")
            with self.assertRaises(errors.InsufficientPrivilege):
                con.execute("UPDATE core.document SET title='tampered'")

    def test_08_row_security_off_does_not_bypass_for_gateway(self):
        with db_client.database_session(self.db) as con:
            con.execute("SET row_security=off")
            with self.assertRaises(errors.InsufficientPrivilege):
                con.execute("SELECT * FROM core.document")

    def test_09_sql_injection_is_data_not_sql(self):
        payload="' OR 1=1; DROP TABLE core.document;--"
        with db_client.database_session(self.db) as con:
            self.assertIsNone(db_client.read_authorized_document(con,"alice",payload))
            self.assertEqual(db_client.search_authorized_chunks(con,"alice",payload),[])
        with db_client.database_session(self.db,admin=True) as admin:
            self.assertEqual(admin.execute("SELECT count(*) FROM core.document").fetchone()[0],3)

    def test_10_transaction_local_actor_does_not_leak_across_calls(self):
        with db_client.database_session(self.db) as con:
            self.assertEqual(len(db_client.list_authorized_documents(con,"bob")),2)
            self.assertEqual(con.execute("SELECT count(*) FROM core.document").fetchone()[0],0)
            self.assertEqual(len(db_client.list_authorized_documents(con,"alice")),2)
            self.assertEqual(con.execute("SELECT count(*) FROM core.document").fetchone()[0],0)

    def test_11_revoke_visible_in_existing_read_committed_transaction(self):
        scope="revoke_"+secrets.token_hex(4)
        subject="worker_"+secrets.token_hex(3)
        doc="revoked_"+secrets.token_hex(4)
        with db_client.database_session(self.db,admin=True) as admin:
            admin.execute("INSERT INTO core.scope(id) VALUES(%s)",(scope,))
            admin.execute("INSERT INTO core.subject(id) VALUES(%s)",(subject,))
            admin.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES(%s,%s)",(subject,scope))
            admin.execute("INSERT INTO core.document(id,title,scope_id) VALUES(%s,'transient',%s)",(doc,scope))
            admin.execute("INSERT INTO core.document_chunk(id,document_id,body) VALUES(%s,%s,'hidden-after-revoke')",
                          (doc+"-c",doc))
        with db_client.database_session(self.db) as con:
            with con.transaction():
                con.execute("SELECT set_config('factory.actor_id',%s,true)",(subject,))
                before=con.execute("SELECT count(*) FROM core.document WHERE id=%s",(doc,)).fetchone()[0]
                self.assertEqual(before,1)
                with db_client.database_session(self.db,admin=True) as admin:
                    gen=admin.execute("SELECT core.revoke_scope_access(%s,%s)",(subject,scope)).fetchone()[0]
                    self.assertGreaterEqual(gen,1)
                after=con.execute("SELECT count(*) FROM core.document WHERE id=%s",(doc,)).fetchone()[0]
                chunk=con.execute("SELECT count(*) FROM core.document_chunk WHERE document_id=%s",(doc,)).fetchone()[0]
                self.assertEqual((after,chunk),(0,0))

    def test_12_move_document_hides_both_doc_and_chunk_atomically(self):
        scope="moveopen_"+secrets.token_hex(4)
        dest="movesecret_"+secrets.token_hex(4)
        doc="move_"+secrets.token_hex(4)
        subject="moveuser_"+secrets.token_hex(3)
        with db_client.database_session(self.db,admin=True) as admin:
            for k in (scope,dest):admin.execute("INSERT INTO core.scope(id) VALUES(%s)",(k,))
            admin.execute("INSERT INTO core.subject(id) VALUES(%s)",(subject,))
            admin.execute("INSERT INTO core.scope_grant(subject_id,scope_id) VALUES(%s,%s)",(subject,scope))
            admin.execute("INSERT INTO core.document(id,title,scope_id) VALUES(%s,'test',%s)",(doc,scope))
            admin.execute("INSERT INTO core.document_chunk(id,document_id,body) VALUES(%s,%s,'sensitive')",
                          (doc+"-c",doc))
        with db_client.database_session(self.db) as con:
            self.assertIsNotNone(db_client.read_authorized_document(con,subject,doc))
        with db_client.database_session(self.db,admin=True) as admin:
            old_epoch=admin.execute("SELECT epoch FROM core.scope WHERE id=%s",(scope,)).fetchone()[0]
            admin.execute("SELECT core.move_document_scope(%s,%s)",(doc,dest))
            new_epoch=admin.execute("SELECT epoch FROM core.scope WHERE id=%s",(scope,)).fetchone()[0]
            self.assertEqual(new_epoch,old_epoch+1)
        with db_client.database_session(self.db) as con:
            self.assertIsNone(db_client.read_authorized_document(con,subject,doc))
            with con.transaction():
                con.execute("SELECT set_config('factory.actor_id',%s,true)",(subject,))
                self.assertEqual(con.execute("SELECT count(*) FROM core.document_chunk WHERE document_id=%s",(doc,)).fetchone()[0],0)

    def test_13_rollback_on_missing_document_preserves_ledger(self):
        with db_client.database_session(self.db,admin=True) as admin:
            before=admin.execute("SELECT generation FROM core.policy_state").fetchone()[0]
            with self.assertRaises(psycopg.Error):
                admin.execute("SELECT core.move_document_scope('missing-doc','engineering')")
            after=admin.execute("SELECT generation FROM core.policy_state").fetchone()[0]
            self.assertEqual(before,after)

    def test_14_literal_bad_subject_and_limit_fail_validation(self):
        with db_client.database_session(self.db) as con:
            for actor in ("", "../alice","alice' OR '1'='1",None):
                with self.subTest(actor=actor):
                    with self.assertRaises(db_client.InvalidRequest):
                        db_client.list_authorized_documents(con,actor)
            for limit in (0,-1,False,10000):
                with self.assertRaises(db_client.InvalidRequest):
                    db_client.list_authorized_documents(con,"alice",limit)

    def test_15_active_p13_migrated_database_is_separate_and_complete(self):
        with db_client.database_session("factory_core_v06",admin=True) as admin:
            expected={"document":13,"document_chunk":13,"scope":8,"scope_grant":13,
                      "engineering_source":40,"engineering_source_page":36,
                      "engineering_bom_line":351,"migration_manifest":2}
            for table,count in expected.items():
                with self.subTest(table=table):
                    actual=admin.execute("SELECT count(*) FROM core."+table).fetchone()[0]
                    self.assertEqual(actual,count)
        with db_client.database_session("factory_core_v06") as con:
            self.assertEqual(con.execute("SELECT count(*) FROM core.document").fetchone()[0],0)
            self.assertGreater(len(db_client.list_authorized_documents(con,"engineer")),0)

if __name__=="__main__":unittest.main(verbosity=2)
