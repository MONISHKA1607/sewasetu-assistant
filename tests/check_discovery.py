"""Walks the discovery chain exactly as an MCP client does, and checks every URL is consistent."""
import os, sys, requests
BASE = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
PORTAL = os.environ.get("PORTAL_URL", BASE).rstrip("/")
MCP = {"citizen": os.environ.get("CIT_URL", PORTAL + "/mcp/citizen"), "officer": os.environ.get("OFF_URL", PORTAL + "/mcp/officer")}
ok = True
def check(name, cond, detail=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name, detail)
for role, url in MCP.items():
    r = requests.post(url, json={}, headers={"Accept": "application/json, text/event-stream"})
    h = r.headers.get("www-authenticate", "")
    check("%s: 401 with WWW-Authenticate" % role, r.status_code == 401 and "resource_metadata=" in h, r.status_code)
    prm_url = h.split('resource_metadata="')[1].split('"')[0] if 'resource_metadata="' in h else ""
    check("%s: metadata URL is on the public host" % role, prm_url.startswith(BASE), prm_url)
    prm = requests.get(prm_url.replace(BASE, PORTAL)).json()
    check("%s: resource equals the MCP endpoint URL" % role, prm.get("resource") == BASE + "/mcp/" + role, prm.get("resource"))
    asv = prm.get("authorization_servers", [None])[0]
    check("%s: authorization server is exactly the issuer (no trailing slash drift)" % role, asv == BASE, asv)
    meta = requests.get(PORTAL + "/.well-known/oauth-authorization-server").json()
    check("%s: AS metadata issuer equals authorization_servers entry" % role, meta["issuer"] == asv, meta["issuer"])
    check("%s: scope advertised" % role, prm.get("scopes_supported") == [role], prm.get("scopes_supported"))
for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "jwks_uri"):
    check("AS metadata %s is public" % k, meta[k].startswith(BASE), meta[k])
check("PKCE S256 advertised", meta["code_challenge_methods_supported"] == ["S256"])
sys.exit(0 if ok else 1)
