# -*- coding: utf-8 -*-
"""Live demo chat: talk to the Sewa Setu assistants as a citizen or as the block officer.

You type; a Gemini model answers using ONLY the real MCP tools of the live endpoint. Every tool call and the
portal's raw answer is printed as it happens, so an audience can see that the portal, not the model, is the
authority. Sign-in uses the portal's real OAuth flow (the OTP is read from the SMS console automatically).

  docker compose -f docker-compose.yml --profile evals run --rm evals python demo/chat.py citizen
  docker compose -f docker-compose.yml --profile evals run --rm evals python demo/chat.py officer
Options: --mobile 9876543210   --auto-upload   (types /upload for you when a link appears)
In chat:  /upload  simulate opening the upload link   /quit  leave
"""
import asyncio, json, os, random, re, sys, time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
from oauth_helper import sign_in, BASE, PORTAL
from sample_docs import valid_pdf
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

ROLE = next((a for a in sys.argv[1:] if a in ("citizen", "officer")), "citizen")
ARG = lambda f, d=None: (sys.argv[sys.argv.index(f) + 1] if f in sys.argv and sys.argv.index(f) + 1 < len(sys.argv) else d)
MOBILE = ARG("--mobile") or "9" + "%09d" % random.randint(0, 999999999)
AUTO_UPLOAD = "--auto-upload" in sys.argv
SELFTEST = os.environ.get("DEMO_SELFTEST") == "1"
KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("DEMO_MODEL") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
URL = os.environ.get("CIT_URL" if ROLE == "citizen" else "OFF_URL", "http://mcp-%s:%s/mcp/%s" % (ROLE, 8101 if ROLE == "citizen" else 8102, ROLE))
RES = BASE + "/mcp/" + ROLE
API = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"

C = {"dim": "\033[2m", "b": "\033[1m", "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "x": "\033[0m"}
def say(color, text): print(C[color] + text + C["x"], flush=True)


def gen(system, contents, tools):
    if SELFTEST:
        return mock(contents)
    for attempt in range(8):
        try:
            r = requests.post(API % MODEL, params={"key": KEY}, timeout=75, json={
                "systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                "tools": [{"functionDeclarations": tools}], "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096}})
        except requests.exceptions.RequestException:
            say("dim", "   (model did not answer, retrying)"); time.sleep(3); continue
        if r.status_code == 429 or r.status_code >= 500:
            m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', r.text)
            if "PerDay" in r.text or "limit: 0" in r.text:
                raise SystemExit("Gemini daily quota is used up for %s. Set DEMO_MODEL to another model or try later." % MODEL)
            d = min(60, float(m.group(1)) + 1 if m else 8)
            say("dim", "   (rate limited, waiting %.0fs)" % d); time.sleep(d); continue
        if r.status_code >= 400:
            raise SystemExit("Gemini error %s: %s" % (r.status_code, r.text[:300]))
        c = (r.json().get("candidates") or [{}])[0].get("content") or {"role": "model", "parts": []}
        c.setdefault("role", "model"); c.setdefault("parts", [])
        return c
    raise SystemExit("Gemini kept refusing; try again shortly.")


def to_tools(mcp_tools):
    def conv(v):
        o = {"type": (v.get("type") or "string").upper()}
        if v.get("description"):
            o["description"] = v["description"]
        return o
    out = []
    for t in mcp_tools:
        props = (t.inputSchema or {}).get("properties", {})
        d = {"name": t.name, "description": (t.description or "")[:900]}
        if props:
            d["parameters"] = {"type": "OBJECT", "properties": {k: conv(v) for k, v in props.items()},
                               **({"required": t.inputSchema["required"]} if t.inputSchema.get("required") else {})}
        out.append(d)
    return out


def do_upload(link):
    link = re.sub(r"^https?://[^/]+", PORTAL, link)
    r = requests.post(link, files={"document": ("age_proof.pdf", valid_pdf("Demo age proof"), "application/pdf")}, timeout=30)
    say("green" if r.status_code == 200 else "red", "   [phone] opened the link and uploaded a certificate (HTTP %s)" % r.status_code)


def show_result(name, out):
    txt = json.dumps(out, ensure_ascii=False)
    link = out.get("upload_link") if isinstance(out, dict) else None
    if link:
        txt = txt.replace(link, "<one-time upload link>")
    say("dim", "   <- " + (txt if len(txt) < 380 else txt[:380] + " ..."))
    return link


# ---- offline self-test policy (used only to check the plumbing without an API key)
def mock(contents):
    n = sum(1 for c in contents if c["role"] == "user" and any("functionResponse" in x for x in c["parts"]))
    seq = [("get_scheme_info", {}), ("save_details", {"applicant_name": "Kamla Devi", "dob": "04/07/1956", "village": "Lakhipur", "block": "Sonari", "bank_account": "123456789012"}),
           ("get_document_upload_link", {}), ("save_details", {"ifsc": "SBIN0003077", "gender": "Female", "marital_status": "Widowed"}),
           ("submit_application", {"declaration_confirmed": True})] if ROLE == "citizen" else [("queue_summary", {}), ("list_pending", {"limit": 3})]
    if n < len(seq):
        return {"role": "model", "parts": [{"functionCall": {"name": seq[n][0], "args": seq[n][1]}}]}
    return {"role": "model", "parts": [{"text": "Done."}]}


async def main():
    if not KEY and not SELFTEST:
        raise SystemExit("Set GEMINI_API_KEY (a free key from Google AI Studio) in your .env")
    who = "citizen phone XXXXXX%s" % MOBILE[-4:] if ROLE == "citizen" else "block officer"
    say("b", "\nSewa Setu %s assistant - signing in as %s through the portal's OAuth flow ..." % (ROLE, who))
    tok = sign_in(ROLE, RES, MOBILE) if ROLE == "citizen" else sign_in("officer", RES)
    say("green", "Signed in. The assistant can only do what this token allows (role=%s). Session ends %s.\n" % (ROLE, tok.get("session_ends", "?")))
    async with streamablehttp_client(URL, headers={"Authorization": "Bearer " + tok["access_token"]}) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = to_tools((await s.list_tools()).tools)
            say("dim", "Tools this endpoint exposes: " + ", ".join(t["name"] for t in tools) + "\n")
            system = ((init.instructions or "") + "\n\nYou are an assistant on a phone helping %s. Be brief and kind; reply in the language "
                      "the person uses. Use the tools for anything about the pension; never guess." % ("an Indian citizen" if ROLE == "citizen" else "a block officer"))
            contents, link = [], None
            script = iter(["Hi, I want to apply for my grandmother, she is 70 and lives in Sonari.", "/upload", "/quit"]) if SELFTEST else None
            while True:
                try:
                    line = next(script) if script else input(C["cyan"] + "You: " + C["x"]).strip()
                except (EOFError, StopIteration):
                    break
                if not line:
                    continue
                if line == "/quit":
                    break
                if line == "/upload":
                    if link:
                        do_upload(link)
                        line = "I have uploaded the document."
                    else:
                        say("yellow", "   (no upload link yet)"); continue
                if SELFTEST:
                    print("You: " + line)
                contents.append({"role": "user", "parts": [{"text": line}]})
                for _ in range(14):
                    c = gen(system, contents, tools)
                    contents.append(c)
                    fcs = [x["functionCall"] for x in c["parts"] if "functionCall" in x]
                    if not fcs:
                        say("green", "Assistant: " + "".join(x.get("text", "") for x in c["parts"]).strip() + "\n"); break
                    resp = []
                    for fc in fcs:
                        args = dict(fc.get("args") or {})
                        shown = {k: ("ending " + str(v)[-4:] if k == "bank_account" else v) for k, v in args.items()}
                        say("yellow", "   -> tool %s(%s)" % (fc["name"], ", ".join("%s=%r" % kv for kv in shown.items())))
                        res = await s.call_tool(fc["name"], args)
                        out = res.structuredContent or (json.loads(res.content[0].text) if res.content and res.content[0].text.startswith("{") else {"text": res.content[0].text if res.content else ""})
                        got = show_result(fc["name"], out)
                        if got:
                            link = got
                            if AUTO_UPLOAD or SELFTEST:
                                do_upload(got)
                            else:
                                say("yellow", "   [phone] open the upload link in your browser: " + got + "   (or type /upload)")
                        resp.append({"functionResponse": {"name": fc["name"], "response": {"result": out}}})
                    contents.append({"role": "user", "parts": resp})
                else:
                    say("red", "Assistant used its whole turn budget without answering.")
    say("b", "\nBye. (The sign-in ends on its own, or from the portal's 'Connected applications' page.)")


asyncio.run(main())
