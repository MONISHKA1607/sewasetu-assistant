"""End-to-end upload checks against the live endpoint. Verdicts come from HTTP codes and from what the
assistant's own tool then reports (the draft), never from what the page says."""
import asyncio, json, os, re, sys
import requests
sys.path.insert(0, os.path.dirname(__file__))
from oauth_helper import sign_in, BASE, PORTAL
from sample_docs import valid_pdf, valid_png, VALID_JPEG
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

CIT_URL = os.environ.get("CIT_URL", "http://mcp-citizen:8101/mcp/citizen")
ok_all = True
def check(name, cond, detail=""):
    global ok_all; ok_all &= bool(cond); print(("PASS " if cond else "FAIL ") + name, detail)

def post(link, name, data, mime="application/octet-stream"):
    return requests.post(link, files={"document": (name, data, mime)}, timeout=60).status_code

async def tool(s, name, **kw):
    r = await s.call_tool(name, kw); return r.structuredContent or json.loads(r.content[0].text)

async def link_for(s):
    l = (await tool(s, "get_document_upload_link"))["upload_link"]
    return re.sub(r"^https?://[^/]+", PORTAL, l)

async def main():
    mobile = "9" + "%09d" % (abs(hash(os.urandom(4))) % 10**9)
    tok = sign_in("citizen", BASE + "/mcp/citizen", mobile)
    async with streamablehttp_client(CIT_URL, headers={"Authorization": "Bearer " + tok["access_token"]}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await tool(s, "save_details", applicant_name="Upload Test", dob="01/01/1950", village="V", block="Sonari", bank_account="123456789012")
            l1 = await link_for(s)
            bad = [("the old 26-byte placeholder PDF", "a.pdf", b"%PDF-1.4\n% demo age proof\n"),
                   ("a truncated PDF", "a.pdf", valid_pdf()[:len(valid_pdf()) // 2]),
                   ("a text file renamed .pdf", "a.pdf", b"this is plain text, not a pdf"),
                   ("a PNG renamed .jpg", "a.jpg", valid_png()),
                   ("an empty file", "a.pdf", b""),
                   ("a file over 5 MB", "a.pdf", b"%PDF-1.4\n" + b"0" * 5_300_000),
                   ("a .txt file", "a.txt", valid_pdf())]
            for label, fn, data in bad:
                check("rejected: " + label, post(l1, fn, data) == 400)
            d = await tool(s, "save_details")
            check("after every rejection the draft still says NOT uploaded (nothing was saved)", d["draft"]["age_proof_document"] == "NOT uploaded", d["draft"]["age_proof_document"])
            check("the link survived all the rejections and still accepts a good file", post(l1, "proof.pdf", valid_pdf(), "application/pdf") == 200)
            d = await tool(s, "save_details")
            check("the assistant now sees the document as uploaded", d["draft"]["age_proof_document"] == "uploaded")
            check("the same link cannot be used twice", post(l1, "proof.pdf", valid_pdf(), "application/pdf") == 410)
            l2 = await link_for(s)
            check("a valid PNG is accepted", post(l2, "proof.png", valid_png(), "image/png") == 200)
            l3 = await link_for(s)
            check("a valid JPEG is accepted", post(l3, "proof.jpg", VALID_JPEG, "image/jpeg") == 200)
    path_dir = "/var/sewasetu/uploads/agent"
    if os.path.isdir(path_dir):
        leftovers = [f for f in os.listdir(path_dir) if f.endswith(".part")]
        check("no half-written .part files are left behind", not leftovers, leftovers)

asyncio.run(main())
sys.exit(0 if ok_all else 1)
