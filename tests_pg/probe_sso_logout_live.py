"""Exercise the real Keycloak authorization-code + PKCE web flow with HTTP cookies.

No password grant, no synthetic JWT and no token printed. Test account credentials
read from protected local test file. Validates session, CSRF and logout.
"""
from __future__ import annotations
import json,html.parser,sys,urllib.parse
from pathlib import Path
import httpx

URL="http://localhost:19136"
KC="http://127.0.0.1:18080"
CREDS=Path(r"K:\ManufacturingAI-CoreRuntime\credentials")
class FormParser(html.parser.HTMLParser):
 def __init__(self):
  super().__init__();self.action=None;self.inputs={}
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=="form" and a.get("id")=="kc-form-login":self.action=a.get("action")
  if tag=="input" and a.get("name"):
   self.inputs[a["name"]]=a.get("value","")
def test():
 passwords=json.loads((CREDS/"keycloak-demo-users.json").read_text(encoding="utf-8"))
 stages={}
 with httpx.Client(timeout=15,trust_env=False,follow_redirects=False) as client:
  root=client.get(URL+"/")
  stages["ui_status"]=root.status_code
  login=client.get(URL+"/auth/login")
  stages["login_status"]=login.status_code
  location=login.headers.get("location","")
  parsed=urllib.parse.urlparse(location)
  args=urllib.parse.parse_qs(parsed.query)
  if login.status_code!=302 or not location.startswith(KC+"/realms/factory-core-v06/"):
   raise RuntimeError("INVALID_LOGIN_REDIRECT")
  stages["pkce_required"]=args.get("code_challenge_method")==["S256"]
  stages["challenge_present"]=len(args.get("code_challenge",[""])[0])>=40
  stages["state_present"]=bool(args.get("state"))
  if not stages["pkce_required"]:raise RuntimeError("PKCE_MISSING")
  response=client.get(location)
  stages["keycloak_form_status"]=response.status_code
  form=FormParser();form.feed(response.text)
  if not form.action:raise RuntimeError("KEYCLOAK_LOGIN_FORM_MISSING")
  values=dict(form.inputs);values.update({"username":"engineer","password":passwords["engineer"]})
  # HTTPX strictly refuses Secure cookies over HTTP. Chromium explicitly
  # permits Secure cookies on loopback origins; the real Chrome E2E is the
  # authoritative browser test. The test client emulates that one exception.
  for cookie in client.cookies.jar:
   if cookie.domain=="127.0.0.1" and cookie.secure:cookie.secure=False
  submitted=client.post(form.action,data=values)
  stages["credentials_submit_status"]=submitted.status_code
  redirect=submitted.headers.get("location","")
  if submitted.status_code not in (302,303) or not redirect.startswith(URL+"/auth/callback?"):
   import re
   body=re.sub(r"<[^>]*>"," ",submitted.text)
   body=re.sub(r"\\s+"," ",body).strip()
   stages["keycloak_error_text"]=body[:500]
   stages["login_post_host"]=urllib.parse.urlparse(form.action).hostname
   stages["login_post_path"]=urllib.parse.urlparse(form.action).path
   stages["keycloak_cookies"]=[{"name":x.name,"domain":x.domain,"path":x.path,"secure":x.secure} for x in client.cookies.jar]
   print(json.dumps(stages,ensure_ascii=True))
   raise RuntimeError("KEYCLOAK_DID_NOT_REDIRECT_TO_CALLBACK "+str(submitted.status_code))
  callback=client.get(redirect)
  stages["callback_status"]=callback.status_code
  stages["session_cookie_set"]="factory_p2_session=" in callback.headers.get("set-cookie","")
  if callback.status_code!=303:raise RuntimeError("OIDC_CALLBACK_FAILED")
  me=client.get(URL+"/api/me")
  stages["me_status"]=me.status_code
  stages["actor"]=me.json().get("actor")
  if me.status_code!=200 or me.json().get("actor")!="engineer":
   raise RuntimeError("SESSION_IDENTITY_MISMATCH")
  malicious=client.post(URL+"/auth/logout",headers={"Origin":URL,"X-CSRF-Token":"wrong"})
  stages["logout_wrong_csrf"]=malicious.status_code
  without_origin=client.post(URL+"/auth/logout",headers={"X-CSRF-Token":me.json()["csrf"]})
  stages["logout_without_origin"]=without_origin.status_code
  if malicious.status_code!=403 or without_origin.status_code!=403:raise RuntimeError("CSRF_POLICY_FAILED")
  logout=client.post(URL+"/auth/logout",headers={"Origin":URL,"X-CSRF-Token":me.json()["csrf"]})
  stages["logout_status"]=logout.status_code
  after=client.get(URL+"/api/me")
  stages["after_logout_status"]=after.status_code
  if logout.status_code!=200 or after.status_code!=401:raise RuntimeError("SESSION_REVOCATION_FAILED")
  login_again=client.get(URL+"/auth/login")
  stages["second_login_request"]=login_again.status_code
  location2=login_again.headers.get("location","")
  if login_again.status_code==302 and location2.startswith(KC+"/realms/factory-core-v06/"):
   keycloak_again=client.get(location2)
   stages["second_keycloak_status"]=keycloak_again.status_code
   dest=keycloak_again.headers.get("location","")
   stages["silent_sso_after_app_logout"]=keycloak_again.status_code in (302,303) and dest.startswith(URL+"/auth/callback?")
   stages["keycloak_form_required_again"]=keycloak_again.status_code==200 and "kc-form-login" in keycloak_again.text
 return stages
if __name__=="__main__":
 try:print(json.dumps(test(),ensure_ascii=True))
 except Exception as e:
  print(json.dumps({"error":type(e).__name__,"message":str(e)[:300]},ensure_ascii=True))
  sys.exit(1)
