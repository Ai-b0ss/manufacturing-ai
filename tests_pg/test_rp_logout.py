"""Isolated RP-initiated logout tests for the live Keycloak-backed BFF adapter.

No Keycloak admin operations, no real token requests, no modification of
the parallel agent's application or PostgreSQL. Uses an in-process TestClient.
"""
from __future__ import annotations
import time,unittest,urllib.parse
from fastapi.testclient import TestClient
from p2_gateway import app as app_module

class BrowserLogoutTests(unittest.TestCase):
    def setUp(self):
        self.c=TestClient(app_module.app,base_url="http://localhost:19136")
        self.handle="isolated-logout-test-"+str(time.time_ns())
        self.id_token="eyJhbGciOiJSUzI1NiJ9.mock-identity-token.signature"
        self.csrf="test-csrf-00000000"
        with app_module._LOCK:
            app_module._SESSIONS[self.handle]=app_module.WebSession(
                "not-a-real-access-token",int(time.time())+600,self.csrf,self.id_token)
    def tearDown(self):
        with app_module._LOCK:app_module._SESSIONS.pop(self.handle,None)
    def post(self,origin="http://localhost:19136",csrf=None):
        headers={"Origin":origin,"X-CSRF-Token":self.csrf if csrf is None else csrf}
        return self.c.post("/auth/logout",cookies={"factory_p2_session":self.handle},headers=headers)
    def alive(self):
        with app_module._LOCK:return self.handle in app_module._SESSIONS

    def test_01_bad_origin_does_not_revoke_or_redirect(self):
        r=self.post(origin="http://evil.localhost")
        self.assertEqual(r.status_code,403)
        self.assertTrue(self.alive())
        self.assertNotIn("identity_logout_url",r.text)

    def test_02_wrong_csrf_does_not_revoke(self):
        r=self.post(csrf="incorrect")
        self.assertEqual(r.status_code,403)
        self.assertTrue(self.alive())

    def test_03_valid_logout_revokes_first_then_returns_fixed_idp_logout(self):
        r=self.post()
        self.assertEqual(r.status_code,200)
        self.assertFalse(self.alive())
        data=r.json()
        self.assertEqual(data["status"],"local_session_revoked")
        uri=urllib.parse.urlparse(data["identity_logout_url"])
        self.assertEqual(uri.scheme,"http")
        self.assertEqual(uri.netloc,"127.0.0.1:18080")
        self.assertEqual(uri.path,"/realms/factory-core-v06/protocol/openid-connect/logout")
        params=urllib.parse.parse_qs(uri.query)
        self.assertEqual(params["client_id"],["factory-web"])
        self.assertEqual(params["id_token_hint"],[self.id_token])
        self.assertEqual(params["post_logout_redirect_uri"],["http://localhost:19136/"])
        self.assertEqual(r.headers["cache-control"],"no-store, max-age=0")

    def test_04_logout_url_does_not_accept_caller_redirect(self):
        r=self.c.post("/auth/logout?post_logout_redirect_uri=https%3A%2F%2Fevil.invalid",
            cookies={"factory_p2_session":self.handle},
            headers={"Origin":"http://localhost:19136","X-CSRF-Token":self.csrf,
                     "X-Forwarded-Host":"evil.invalid"})
        self.assertEqual(r.status_code,200)
        url=r.json()["identity_logout_url"]
        params=urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["post_logout_redirect_uri"],["http://localhost:19136/"])
        self.assertNotIn("evil.invalid",url)

    def test_05_replay_denied(self):
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.post().status_code,401)

    def test_06_cookie_cleared_and_token_not_returned_as_access_token(self):
        r=self.post()
        self.assertEqual(r.status_code,200)
        self.assertIn("factory_p2_session=",r.headers.get("set-cookie",""))
        self.assertIn("Max-Age=0",r.headers.get("set-cookie",""))
        self.assertNotIn("not-a-real-access-token",r.text)

if __name__=="__main__":unittest.main(verbosity=2)
