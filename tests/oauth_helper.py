"""Test helper: performs the portal's real OAuth flow (DCR + PKCE + OTP + consent) like an MCP client would."""
import base64, hashlib, os, re, secrets, time, json
from urllib.parse import urlparse, parse_qs
import requests

BASE = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def _otp_from_gateway(mobile, after_id=0):
    for _ in range(20):
        msgs = requests.get(BASE + "/__gateway/api/messages", params={"to": mobile}).json()["messages"]
        for m in reversed(msgs):
            if m["delivered"] and m["id"] > after_id and "OTP" in (m["text"] or ""):
                return re.search(r"OTP is (\d{6})", m["text"]).group(1)
        time.sleep(1)
    raise RuntimeError("no OTP")


def sign_in(role, resource, mobile=None, username="admin", password=None):
    password = password or os.environ.get("ADMIN_PASSWORD", "test123")
    s = requests.Session()
    reg = requests.post(BASE + "/oauth/register", json={"client_name": "eval-harness", "redirect_uris":
                        ["http://localhost:9999/cb"], "token_endpoint_auth_method": "none"}).json()
    cid = reg["client_id"]
    verifier = secrets.token_urlsafe(48)
    chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = s.get(BASE + "/oauth/authorize", params={"client_id": cid, "redirect_uri": "http://localhost:9999/cb",
              "response_type": "code", "scope": role, "state": "x", "code_challenge": chal,
              "code_challenge_method": "S256", "resource": resource}, allow_redirects=True)
    login_url = r.url
    if role == "citizen":
        before = 0
        msgs = requests.get(BASE + "/__gateway/api/messages", params={"to": mobile}).json()["messages"]
        before = max([m["id"] for m in msgs], default=0)
        s.post(login_url, data={"mobile": mobile})
        otp = _otp_from_gateway(mobile, before)
        r = s.post(login_url, data={"otp": otp}, allow_redirects=True)
    else:
        r = s.post(login_url, data={"username": username, "password": password}, allow_redirects=True)
    consent_url = r.url
    assert "consent" in consent_url, r.text[:300]
    r = s.post(consent_url, data={"decision": "allow"}, allow_redirects=False)
    code = parse_qs(urlparse(r.headers["Location"]).query)["code"][0]
    t = requests.post(BASE + "/oauth/token", data={"grant_type": "authorization_code", "code": code,
                      "client_id": cid, "redirect_uri": "http://localhost:9999/cb", "code_verifier": verifier,
                      "resource": resource}).json()
    t["client_id"] = cid
    return t
