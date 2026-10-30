"""Officer + security checks. Verdicts come from HTTP codes and database queries, never from model text."""
import asyncio, json, os, sys, subprocess, requests, psycopg2
sys.path.insert(0, os.path.dirname(__file__))
from oauth_helper import sign_in, BASE
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

CIT_RES, OFF_RES = BASE + "/mcp/citizen", BASE + "/mcp/officer"
CIT_URL = os.environ.get("CIT_URL", "http://localhost:8101/mcp/citizen")
OFF_URL = os.environ.get("OFF_URL", "http://localhost:8102/mcp/officer")
db = lambda: psycopg2.connect(host=os.environ.get("DB_HOST", "127.0.0.1"), port=int(os.environ.get("DB_PORT", "5433")), dbname="sewasetu", user="sewasetu", password="sewasetu123")
results = []
def check(name, cond, detail=""):
    results.append(cond); print(("PASS " if cond else "FAIL ") + name, detail)

def q(sql, *a):
    c = db(); cur = c.cursor(); cur.execute(sql, a); r = cur.fetchall(); c.close(); return r

def status_code(url, tok):
    return requests.post(url, headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code

async def tool(url, tok, name, **kw):
    async with streamablehttp_client(url, headers={"Authorization": "Bearer " + tok}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(name, kw)
            return res.structuredContent or json.loads(res.content[0].text)

async def main():
    # two fresh pending applications made through the citizen assistant
    nos = []
    import random
    sfx = "%06d" % random.randint(0, 999999)
    M1, M2, M3 = "98" + sfx + "11", "98" + sfx + "12", "98" + sfx + "13"
    for m in (M1, M2):
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "smoke_citizen.py"), m], capture_output=True, timeout=100)
        nos.append(q("select application_no from applications where mobile=%s", m)[0][0])
    A, B = nos
    off = sign_in("officer", OFF_RES); cit = sign_in("citizen", CIT_RES, M3)

    # role separation
    check("citizen token refused by officer endpoint", status_code(OFF_URL, cit["access_token"]) == 401)
    check("officer token refused by citizen endpoint", status_code(CIT_URL, off["access_token"]) == 401)
    wrong_aud = sign_in("officer", BASE + "/something-else")
    check("officer token for another audience refused", status_code(OFF_URL, wrong_aud["access_token"]) == 401)
    check("garbage token refused", status_code(OFF_URL, "abc.def.ghi") == 401)

    ot = off["access_token"]
    s = await tool(OFF_URL, ot, "queue_summary")
    check("queue summary counts pending in pilot blocks", s["pending_total"] >= 2, str(s["pending_total"]))
    l = await tool(OFF_URL, ot, "list_pending", limit=50)
    check("new applications appear in queue", {A, B} <= {x["application_no"] for x in l["applications"]})
    g = await tool(OFF_URL, ot, "get_application", application_no=A)
    check("bank account masked", g["bank_account"].startswith("X") and "9012" in g["bank_account"], g["bank_account"])
    check("review view has audit trail", len(g["audit_trail"]) == 1)

    # decision rules
    r = await tool(OFF_URL, ot, "reject_application", application_no=A, reason="")
    check("reject without reason refused", r["decided"] is False and q("select status from applications where application_no=%s", A)[0][0] == "PENDING")
    r = await tool(OFF_URL, ot, "reject_application", application_no=A, reason="bad")
    check("reject with trivial reason refused", r["decided"] is False)
    r = await tool(OFF_URL, ot, "reject_application", application_no=A, reason="Age proof document does not match the name given.")
    row = q("select status, decided_by from applications where application_no=%s", A)[0]
    aud = q("select action, actor, note from audit_log a join applications p on p.id=a.application_id where p.application_no=%s order by a.id desc limit 1", A)[0]
    check("rejection recorded with officer + reason", row == ("REJECTED", "admin") and aud[0] == "REJECTED" and aud[1].startswith("officer:") and "Age proof" in aud[2], str(aud))
    r = await tool(OFF_URL, ot, "approve_application", application_no=A)
    check("cannot overturn a decided case", r["decided"] is False and q("select status from applications where application_no=%s", A)[0][0] == "REJECTED")
    r = await tool(OFF_URL, ot, "approve_application", application_no=B, note="Documents verified")
    check("approve works and reports real status", r["decided"] and r["status"] == "APPROVED" and q("select status from applications where application_no=%s", B)[0][0] == "APPROVED")
    r = await tool(OFF_URL, ot, "get_application", application_no="SSP00000000")
    check("unknown application handled", "error" in r)

    # citizen sees the rejection reason (portal visible from both sides)
    ct = sign_in("citizen", CIT_RES, M1)
    st = await tool(CIT_URL, ct["access_token"], "get_my_application_status")
    check("citizen sees rejection + reason", st["status"] == "REJECTED" and "Age proof" in st["reason_recorded"])

    # kiosk: ending the sign-in cuts off the access token immediately
    before = status_code(OFF_URL, ot)
    requests.post(BASE + "/oauth/revoke", data={"token": off["refresh_token"]})
    after = status_code(OFF_URL, ot)
    check("revoked sign-in cuts off access token at once", before == 200 and after == 401, "%s -> %s" % (before, after))

asyncio.run(main())
print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
