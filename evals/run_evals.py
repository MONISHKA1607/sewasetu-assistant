# -*- coding: utf-8 -*-
"""Persona evals for the Sewa Setu assistants.

Each scenario is one invented person with a goal. The runner signs in through the portal's real OAuth
flow, talks to the live MCP endpoints with a real MCP client, then reads the portal's DATABASE to decide
pass or fail. Nothing the assistant 'says' counts as evidence. Results go to the eval_runs table, which
/eval-dashboard displays.

This is the scripted driver (deterministic, no model). The same checks can be reused by an LLM driver.
Run:  docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile evals run --rm evals
"""
import asyncio, contextlib, json, os, random, re, sys, time
from datetime import datetime, timedelta

import requests
from psycopg2.extras import Json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
sys.path.insert(0, os.environ.get("SERVICE_DIR", "/app"))
import service
from oauth_helper import sign_in, BASE, PORTAL
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

CIT_RES, OFF_RES = BASE + "/mcp/citizen", BASE + "/mcp/officer"
CIT_URL = os.environ.get("CIT_URL", "http://mcp-citizen:8101/mcp/citizen")
OFF_URL = os.environ.get("OFF_URL", "http://mcp-officer:8102/mcp/officer")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/var/sewasetu/uploads")
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
ONLY = set(a for a in sys.argv[1:])


# ------------------------------------------------------------------ plumbing
def q(sql, *a):
    c = service.get_db(); cur = c.cursor(); cur.execute(sql, a)
    try:
        r = cur.fetchall()
    except Exception:
        r = []
    c.commit(); cur.close(); c.close(); return r


def newmobile():
    return "9" + "%09d" % random.randint(0, 999999999)


def redact(o):
    s = json.dumps(o, ensure_ascii=False, default=str)
    s = re.sub(r"/agent-upload/[A-Za-z0-9_\-]+", "/agent-upload/[one-time-link]", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-\.]+", "[token]", s)
    return json.loads(s)


class Run:
    def __init__(self, key, persona, role, goal, expected):
        self.key, self.persona, self.role, self.goal, self.expected = key, persona, role, goal, expected
        self.transcript, self.calls, self.checks = [], [], []

    def say(self, who, text):
        self.transcript.append({"who": who, "text": text})

    async def call(self, s, name, **kw):
        t = time.time()
        try:
            r = await s.call_tool(name, kw)
            txt = r.content[0].text if r.content else ""
            out = r.structuredContent or (json.loads(txt) if txt.strip().startswith("{") else {"text": txt})
            if r.isError:
                out = {"error": txt}
        except Exception as e:
            out = {"transport_error": repr(e)[:200]}
        shown = dict(kw)
        if shown.get("bank_account"):
            shown["bank_account"] = "ending " + re.sub(r"\D", "", shown["bank_account"])[-4:]
        self.calls.append({"tool": name, "args": redact(shown), "result": redact(out),
                           "ms": int((time.time() - t) * 1000)})
        return out

    def check(self, name, cond, detail="", cat="functional"):
        self.checks.append({"name": name, "passed": bool(cond), "detail": str(detail)[:900], "category": cat})
        return bool(cond)


@contextlib.asynccontextmanager
async def _session(url, token):
    async with streamablehttp_client(url, headers={"Authorization": "Bearer " + token}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


@contextlib.asynccontextmanager
async def citizen(mobile):
    tok = sign_in("citizen", CIT_RES, mobile)
    async with _session(CIT_URL, tok["access_token"]) as s:
        yield s, tok


@contextlib.asynccontextmanager
async def officer():
    tok = sign_in("officer", OFF_RES)
    async with _session(OFF_URL, tok["access_token"]) as s:
        yield s, tok


def http_status(url, token):
    return requests.post(url, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=20).status_code


async def upload_doc(run, s):
    res = await run.call(s, "get_document_upload_link")
    link = re.sub(r"^https?://[^/]+", PORTAL, res.get("upload_link", ""))
    r = requests.post(link, files={"document": ("age_proof.pdf", b"%PDF-1.4\n% eval age proof\n", "application/pdf")}, timeout=30)
    return r.status_code


async def apply_full(run, s, d, ifsc="SBIN0003077"):
    await run.call(s, "save_details", **d)
    await upload_doc(run, s)
    await run.call(s, "save_details", ifsc=ifsc, gender=d.get("gender", "Female"), marital_status=d.get("marital_status", "Widowed"))
    return await run.call(s, "submit_application", declaration_confirmed=True)


def rows_for(m):
    return q("SELECT application_no, status, block, doc_path, decided_by FROM applications WHERE mobile=%s", m)


def audit_for(m):
    return q("SELECT a.action, a.actor, a.note FROM audit_log a JOIN applications p ON p.id=a.application_id "
             "WHERE p.mobile=%s ORDER BY a.id", m)


def person(name, dob, village, block, bank):
    return dict(applicant_name=name, dob=dob, village=village, block=block, bank_account=bank)


# ------------------------------------------------------------------ scenarios
async def kamla(run):
    m = newmobile()
    run.say("Grandson (for Kamla Devi, 70, Sonari, Assamese speaker)", "My grandmother wants the old age pension. She is 70, from Sonari.")
    async with citizen(m) as (s, tok):
        info = await run.call(s, "get_scheme_info")
        run.check("assistant learns the window state from the portal", "application_window" in info)
        res = await apply_full(run, s, person("Kamla Devi", "04/07/1956", "Lakhipur", "Sonari block", "1234 5678 9012"))
        run.say("Assistant", "Application submitted: %s" % res.get("application_no"))
        st = await run.call(s, "get_my_application_status")
    rows = rows_for(m)
    run.check("exactly one application row exists", len(rows) == 1, rows, "critical")
    if rows:
        no, status, block, doc, _ = rows[0]
        run.check("application number matches what the assistant told the grandson", res.get("application_no") == no, "told=%s db=%s" % (res.get("application_no"), no), "critical")
        run.check("status is PENDING, block normalised to Sonari", status == "PENDING" and block == "Sonari", (status, block))
        run.check("an age-proof file path is on record", bool(doc), doc, "critical")
        if os.path.isdir(UPLOAD_DIR):
            run.check("the uploaded age-proof file really exists on disk", os.path.exists(doc), doc, "critical")
        run.check("citizen status tool reports the same number", st.get("application_no") == no)
    au = audit_for(m)
    run.check("audit trail has SUBMIT marked as the agent channel", any(a[0] == "SUBMIT" and "channel=agent" in (a[2] or "") for a in au), au)
    run.check("portal login account created (same as web submission)", len(q("SELECT 1 FROM portal_users WHERE mobile=%s", m)) == 1)


async def sita(run):
    m = newmobile()
    run.say("Sita Devi (58, Rajapara)", "I heard everyone gets the pension. Please apply for me, I really need it.")
    async with citizen(m) as (s, tok):
        r1 = await run.call(s, "save_details", **person("Sita Devi", "15/03/1968", "Dhalpur", "Rajapara", "998877665544"))
        run.check("assistant is told she is not eligible", any("NOT ELIGIBLE" in p for p in r1.get("problems", [])), r1.get("problems"))
        run.say("Sita Devi", "Please just try to submit it anyway.")
        await upload_doc(run, s)
        await run.call(s, "save_details", ifsc="SBIN0003077", gender="Female", marital_status="Married")
        r2 = await run.call(s, "submit_application", declaration_confirmed=True)
        run.check("submission is refused", r2.get("submitted") is False, r2.get("errors"), "critical")
    run.check("no application row was created", len(rows_for(m)) == 0, rows_for(m), "critical")


async def harun(run):
    m = newmobile()
    run.say("Harun Ali (64, Khelua, kiosk)", "I know my date of birth and bank account but my certificate is at home.")
    async with citizen(m) as (s, tok):
        await run.call(s, "save_details", **person("Harun Ali", "02/02/1962", "Bhogpur", "Khelua", "556677889900"))
        await run.call(s, "save_details", ifsc="PUNB0123400", gender="Male", marital_status="Married")
        r = await run.call(s, "submit_application", declaration_confirmed=True)
        run.check("submission refused, document named as the missing item", r.get("submitted") is False and "document" in json.dumps(r).lower(), r.get("errors"), "critical")
    run.check("no application row was created", len(rows_for(m)) == 0, "", "critical")
    run.say("Harun Ali", "(next day, new kiosk session) I have the paper now.")
    async with citizen(m) as (s2, tok2):
        d = await run.call(s2, "save_details")
        run.check("draft was kept on the server and resumes", d.get("draft", {}).get("applicant_name") == "Harun Ali", d.get("draft"))
        res = await run.call(s2, "submit_application", declaration_confirmed=True)
        run.check("still cannot submit until the document is uploaded", res.get("submitted") is False)
        await upload_doc(run, s2)
        res = await run.call(s2, "submit_application", declaration_confirmed=True)
        run.check("submits once the document arrives", res.get("submitted") is True, res)
    run.check("exactly one application row", len(rows_for(m)) == 1, "", "critical")


async def namti(run):
    m = newmobile()
    run.say("Citizen (66, Namti)", "I live in Namti block. Please apply for my pension.")
    async with citizen(m) as (s, tok):
        r1 = await run.call(s, "save_details", **person("Gopal Das", "11/11/1960", "Namtigaon", "Namti", "334455667788"))
        run.check("assistant is told assistants are not enabled for Namti", any("not enabled" in p for p in r1.get("problems", [])), r1.get("problems"))
        await upload_doc(run, s)
        await run.call(s, "save_details", ifsc="SBIN0003077", gender="Male", marital_status="Married")
        r2 = await run.call(s, "submit_application", declaration_confirmed=True)
        run.check("submission refused with a pointer to the web portal", r2.get("submitted") is False and "web portal" in json.dumps(r2).lower(), r2.get("errors"), "critical")
    run.check("no application row was created", len(rows_for(m)) == 0, "", "critical")


async def parvati(run):
    m = newmobile()
    run.say("Parvati Devi (72, Sonari)", "(Earlier) I applied through my son's phone.")
    async with citizen(m) as (s, tok):
        first = await apply_full(run, s, person("Parvati Devi", "20/05/1954", "Rangapani", "Sonari", "445566778899"))
    run.say("Daughter", "Mother needs the pension. Let me apply again with her number.")
    async with citizen(m) as (s2, tok2):
        info = await run.call(s2, "get_scheme_info")
        run.check("the existing application is flagged in the very first call (saves a turn-limited model its budget)",
                  info.get("existing_application", {}).get("application_no") == first.get("application_no"), info.get("existing_application"), "critical")
        d0 = await run.call(s2, "save_details", **person("Parvati Devi", "20/05/1954", "Rangapani", "Sonari", "445566778899"))
        run.check("save_details also stops the assistant early", any("ALREADY HAS" in x for x in d0.get("problems", [])), d0.get("problems", [""])[:1])
        await upload_doc(run, s2)
        await run.call(s2, "save_details", ifsc="SBIN0003077", gender="Female", marital_status="Widowed")
        second = await run.call(s2, "submit_application", declaration_confirmed=True)
    run.check("second submission is not accepted as a new application", second.get("submitted") is False and second.get("already_has_application") is True, second, "critical")
    run.check("assistant points to the existing application number", second.get("application_no") == first.get("application_no"), "")
    run.check("only one active application exists", len(rows_for(m)) == 1, rows_for(m), "critical")


async def retry_and_race(run):
    m = newmobile()
    run.say("Citizen (61, Khelua)", "The network dropped after I pressed submit. I pressed it again, twice.")
    async with citizen(m) as (s, tok):
        await run.call(s, "save_details", **person("Lakshmi Bora", "09/09/1964", "Tulsipur", "Khelua", "778899001122"))
        await upload_doc(run, s)
        await run.call(s, "save_details", ifsc="SBIN0003077", gender="Female", marital_status="Widowed")
        # two simultaneous submits on separate sessions: the race the advisory lock exists for
        async with citizen(m) as (s2, tok2):
            ra, rb = await asyncio.gather(run.call(s, "submit_application", declaration_confirmed=True),
                                          run.call(s2, "submit_application", declaration_confirmed=True))
        rc = await run.call(s, "submit_application", declaration_confirmed=True)
    run.check("simultaneous double-submit created exactly one row", len(rows_for(m)) == 1, rows_for(m), "critical")
    nos = {r.get("application_no") for r in (ra, rb, rc) if r.get("application_no")}
    run.check("every reply that names an application names the same one", len(nos) == 1, nos, "critical")
    run.check("a later retry returns the same number, not an error", rc.get("submitted") is True and rc.get("already_submitted_earlier") is True, rc)


async def ramesh_states(run):
    run.say("Ramesh Das (68, Khelua)", "I applied some days ago. Has it been approved? When will I get money?")
    people = {}
    for key, nm in (("pending", "Ramesh Das"), ("approved", "Anil Das"), ("rejected", "Bimal Das"), ("deemed", "Chandra Das")):
        m = newmobile(); people[key] = m
        async with citizen(m) as (s, tok):
            await apply_full(run, s, person(nm, "01/01/1958", "Dibrupur", "Khelua", "1122334455%02d" % random.randint(10, 99)))
    async with officer() as (o, otok):
        no_a = rows_for(people["approved"])[0][0]; no_r = rows_for(people["rejected"])[0][0]
        await run.call(o, "approve_application", application_no=no_a, note="Documents verified")
        await run.call(o, "reject_application", application_no=no_r, reason="Age proof is illegible; please reapply with a clear copy.")
    # fixture: the nightly job's outcome, written the way scripts/deemed_approval.py writes it
    q("UPDATE applications SET status='DEEMED_APPROVED', decided_at=%s, decided_by='RTPS-AUTO' WHERE mobile=%s", datetime.now(), people["deemed"])
    q("INSERT INTO audit_log (application_id, action, actor, note, at) SELECT id,'DEEMED_APPROVED','system:rtps-cron','not processed within SLA',%s FROM applications WHERE mobile=%s", datetime.now(), people["deemed"])
    out = {}
    for key, m in people.items():
        async with citizen(m) as (s, tok):
            out[key] = await run.call(s, "get_my_application_status")
    run.check("pending: reports PENDING and the deemed-approval date", out["pending"].get("status") == "PENDING" and "deemed_approval_on_or_after" in out["pending"], out["pending"].get("status"))
    run.check("approved: says approved by a block officer", out["approved"].get("status") == "APPROVED" and "officer" in out["approved"].get("meaning", "").lower())
    run.check("rejected: shares the officer's recorded reason", out["rejected"].get("status") == "REJECTED" and "illegible" in out["rejected"].get("reason_recorded", ""))
    d = out["deemed"]
    run.check("deemed approval is NOT described as an officer approval", d.get("status") == "DEEMED_APPROVED" and "No officer personally reviewed" in d.get("meaning", "") and "block officer" not in d.get("meaning", "").replace("No officer", ""), d.get("meaning"), "critical")
    run.check("no payment date is promised anywhere", all("Do not state a date" in o.get("payment_note", "") for o in out.values()), "")
    for key, m in people.items():
        want = {"pending": "PENDING", "approved": "APPROVED", "rejected": "REJECTED", "deemed": "DEEMED_APPROVED"}[key]
        run.check("%s: assistant's status equals the database status" % key, rows_for(m)[0][1] == want == out[key].get("status"), "", "critical")


async def kiosk(run):
    a, b = newmobile(), newmobile()
    run.say("Citizen A on a shared kiosk", "(applies, then walks away without signing out)")
    async with citizen(a) as (s, tokA):
        await apply_full(run, s, person("Mina Gogoi", "03/03/1959", "Sapekhati", "Sonari", "223344556677"))
        names = {t.name for t in (await s.list_tools()).tools}
        schemas = json.dumps([t.inputSchema for t in (await s.list_tools()).tools]).lower()
    run.check("citizen endpoint exposes no officer tools", not (names & {"approve_application", "reject_application", "list_pending", "queue_summary"}), sorted(names), "critical")
    run.check("no citizen tool accepts a mobile number or phone argument", "mobile" not in schemas and "phone" not in schemas, "", "critical")
    run.say("Citizen B", "(next person at the kiosk signs in with their own number)")
    async with citizen(b) as (s2, tokB):
        st = await run.call(s2, "get_my_application_status")
    run.check("citizen B cannot see citizen A's application", st.get("has_application") is False, st, "critical")
    before = http_status(CIT_URL, tokA["access_token"])
    requests.post(PORTAL + "/oauth/revoke", data={"token": tokA["refresh_token"]}, timeout=20)
    after = http_status(CIT_URL, tokA["access_token"])
    run.check("ending the sign-in cuts access at once (not after 20 minutes)", before == 200 and after == 401, "%s -> %s" % (before, after), "critical")


async def meena(run):
    m1, m2 = newmobile(), newmobile()
    run.say("Meena Sharma (block officer)", "Show me what is waiting, let me look at one, reject the weak one and approve the good one.")
    async with citizen(m1) as (s, t):
        await apply_full(run, s, person("Hira Devi", "14/04/1957", "Kalapani", "Sonari", "100200300400"))
    async with citizen(m2) as (s, t):
        await apply_full(run, s, person("Dulal Das", "30/06/1955", "Pathar", "Rajapara", "500600700800"))
    n1, n2 = rows_for(m1)[0][0], rows_for(m2)[0][0]
    async with officer() as (o, otok):
        qs = await run.call(o, "queue_summary")
        run.check("queue summary counts waiting cases", qs.get("pending_total", 0) >= 2, qs.get("pending_total"))
        lp = await run.call(o, "list_pending", limit=50)
        run.check("both new applications are visible in the officer's queue", {n1, n2} <= {x["application_no"] for x in lp.get("applications", [])}, "", "critical")
        g = await run.call(o, "get_application", application_no=n1)
        run.check("bank account is masked in the review view", re.fullmatch(r"X+\d{4}", g.get("bank_account", "")) is not None, g.get("bank_account"))
        r0 = await run.call(o, "reject_application", application_no=n1, reason="")
        run.check("rejection without a reason is refused and nothing changes", r0.get("decided") is False and rows_for(m1)[0][1] == "PENDING", r0, "critical")
        r1 = await run.call(o, "reject_application", application_no=n1, reason="Date of birth on certificate does not match the application.")
        row = rows_for(m1)[0]; au = audit_for(m1)[-1]
        run.check("rejection is recorded against the officer, with the reason", row[1] == "REJECTED" and au[0] == "REJECTED" and au[1].startswith("officer:") and "does not match" in (au[2] or ""), au, "critical")
        r2 = await run.call(o, "approve_application", application_no=n2, note="Verified against Voter ID")
        run.check("approval recorded and the tool reports the real status", r2.get("status") == "APPROVED" and rows_for(m2)[0][1] == "APPROVED", r2, "critical")
        r3 = await run.call(o, "approve_application", application_no=n1)
        run.check("a decided case cannot be overturned", r3.get("decided") is False and rows_for(m1)[0][1] == "REJECTED", r3, "critical")
    async with citizen(m1) as (s, t):
        st = await run.call(s, "get_my_application_status")
    run.check("the citizen sees the officer's decision and reason", st.get("status") == "REJECTED" and "does not match" in st.get("reason_recorded", ""), "")
    tokc = sign_in("citizen", OFF_RES, newmobile())
    run.check("a citizen's token is refused by the officer endpoint", http_status(OFF_URL, tokc["access_token"]) == 401, "", "critical")
    toko = sign_in("officer", CIT_RES)
    run.check("an officer's token is refused by the citizen endpoint", http_status(CIT_URL, toko["access_token"]) == 401, "", "critical")
    toka = sign_in("officer", BASE + "/some-other-service")
    run.check("a token minted for a different service is refused", http_status(OFF_URL, toka["access_token"]) == 401, "", "critical")
    run.check("a forged token is refused", http_status(OFF_URL, "eyJhbGciOiJSUzI1NiJ9.e30.x") == 401, "", "critical")


async def officer_scope(run):
    run.say("Officer", "Show me the application from Namti. (Namti is outside the pilot.)")
    m = newmobile()
    q("INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status, village, block, bank_account, ifsc, doc_path, status, submitted_at) "
      "VALUES (%s,'Scope Test',%s,'1955-01-01','Male','Married','Namtigaon','Namti','900800700600','SBIN0003077','x.pdf','PENDING',%s)",
      "SSP26%06d" % random.randint(0, 999999), m, datetime.now())
    no = rows_for(m)[0][0]
    async with officer() as (o, tok):
        g = await run.call(o, "get_application", application_no=no)
        run.check("case outside the pilot blocks is not shown", "error" in g and "applicant_name" not in json.dumps(g) and "name" not in g, g, "critical")
        a = await run.call(o, "approve_application", application_no=no)
        run.check("deciding a case outside the pilot is refused", a.get("decided") is False and rows_for(m)[0][1] == "PENDING", a, "critical")
        lp = await run.call(o, "list_pending", block="Namti")
        run.check("listing an out-of-scope block is refused", "error" in lp, lp)
    q("DELETE FROM applications WHERE mobile=%s", m)


async def window_rule(run):
    run.say("Rule check", "What happens if someone submits after the scheme window closes? (service layer, deadline moved into the past)")
    data = {"applicant_name": "Late Applicant", "dob": "01/01/1950", "village": "V", "block": "Sonari", "bank_account": "123456789012", "ifsc": "SBIN0003077"}
    old = service.SCHEME_DEADLINE
    service.SCHEME_DEADLINE = datetime.now() - timedelta(days=1)
    try:
        errs, _ = service.validate_application(data, "x.pdf")
        run.check("validator reports the window has closed", any("window has closed" in e for e in errs), errs, "critical")
        m = newmobile()
        c = service.get_db(); cur = c.cursor()
        res = service.submit_application(cur, m, data, "x.pdf", channel="agent")
        c.rollback(); cur.close(); c.close()
        run.check("shared submission path refuses it for every channel", res["ok"] is False, res["errors"], "critical")
        run.check("nothing was written", len(rows_for(m)) == 0)
    finally:
        service.SCHEME_DEADLINE = old


SCENARIOS = [
    ("kamla_happy_path", "Kamla Devi, 70, Sonari", "citizen", "Apply for the pension with her grandson's help", "Exactly one valid PENDING application with an age-proof file and an agent-channel audit entry", kamla),
    ("sita_underage", "Sita Devi, 58, Rajapara", "citizen", "Insists on applying although under 60", "No application is created", sita),
    ("harun_no_document", "Harun Ali, 64, Khelua, kiosk", "citizen", "Apply without his age-proof; return the next day", "Nothing submitted without the document; draft survives; one application once the document arrives", harun),
    ("namti_out_of_pilot", "Gopal Das, 66, Namti", "citizen", "Apply from a block outside the pilot", "Refused with a pointer to the web portal; no application", namti),
    ("parvati_duplicate", "Parvati Devi, 72, Sonari", "citizen", "Daughter applies again for an existing application", "No second active application", parvati),
    ("lakshmi_retry_and_race", "Lakshmi Bora, 61, Khelua", "citizen", "Network drops; presses submit repeatedly and simultaneously", "Exactly one application however many submits", retry_and_race),
    ("ramesh_status_states", "Ramesh Das, 68, Khelua (+3 neighbours)", "citizen", "Asks if approved and when he will be paid", "Real status in all four states; deemed approval never called an officer approval; no payment date", ramesh_states),
    ("shared_kiosk", "Mina Gogoi, 63, Sonari, shared kiosk", "citizen", "Walks away signed in; next person uses the kiosk", "No cross-citizen leakage; sign-out cuts access at once; no mobile argument; no officer tools", kiosk),
    ("meena_officer_queue", "Meena Sharma, block officer", "officer", "Review the queue, reject one case with a reason, approve another", "Decisions attributed to the officer with reasons; guard rails hold; cross-role tokens refused", meena),
    ("officer_scope", "Officer vs an out-of-pilot case", "officer", "Try to see and decide a Namti case", "Not visible, not decidable", officer_scope),
    ("window_closed_rule", "Anyone after 31 Oct 23:59", "rule", "Submit after the window closes", "Refused for every channel; nothing written", window_rule),
]


async def main():
    service.ensure_schema()
    results = []
    for key, persona, role, goal, expected, fn in SCENARIOS:
        if ONLY and key not in ONLY:
            continue
        run = Run(key, persona, role, goal, expected)
        t = time.time()
        try:
            await fn(run)
        except Exception as e:  # a crashed scenario is a failed scenario, never silently skipped
            run.check("scenario ran to completion", False, repr(e)[:250], "harness")
        passed = bool(run.checks) and all(c["passed"] for c in run.checks)
        failed = [c for c in run.checks if not c["passed"]]
        cat = None if passed else failed[0]["category"]
        print(("PASS " if passed else "FAIL "), key, "(%d/%d checks, %.0fs)" % (sum(c["passed"] for c in run.checks), len(run.checks), time.time() - t))
        for c in failed:
            print("      x", c["name"], "|", c["detail"][:150])
        q("INSERT INTO eval_runs (run_id, run_at, scenario, persona, role, goal, passed, failure_category, transcript, tool_calls, assertions, note, driver) "
          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'scripted')",
          RUN_ID, datetime.now(), key, persona, role, goal, passed, cat, Json(run.transcript), Json(run.calls),
          Json(run.checks), "Expected: " + expected)
        results.append(passed)
    print("\nRUN %s: %d/%d scenarios passed" % (RUN_ID, sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
