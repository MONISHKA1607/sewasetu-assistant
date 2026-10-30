import asyncio, json, os, sys, requests
from urllib import response
sys.path.insert(0, os.path.dirname(__file__))
from oauth_helper import sign_in, BASE
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
import tempfile

RES = BASE + "/mcp/citizen"
URL = os.environ.get("MCP_URL", "http://localhost:8101/mcp/citizen")
MOBILE = sys.argv[1] if len(sys.argv) > 1 else "9876500001"

async def call(sess, name, **kw):
    r = await sess.call_tool(name, kw)
    out = r.structuredContent or json.loads(r.content[0].text)
    print("\n>>", name, kw); print(json.dumps(out, indent=1, ensure_ascii=False)[:900]); return out

async def main():
    tok = sign_in("citizen", RES, MOBILE)
    print("token scope/session:", tok["scope"], tok["session_ends"])
    async with streamablehttp_client(URL, headers={"Authorization": "Bearer " + tok["access_token"]}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("TOOLS:", [t.name for t in (await s.list_tools()).tools])
            await call(s, "get_scheme_info")
            await call(s, "save_details", applicant_name="Kamla Devi", dob="12-03-1955", village="Lakhipur", block="Sonari block", bank_account="1234 5678 9012")
            up = await call(s, "get_document_upload_link")
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(b"%PDF-1.4 test")
                proof_path = f.name

            with open(proof_path, "rb") as proof_file:
                response = requests.post(
                up["upload_link"],
                files={"document": proof_file}
                )

            print("upload:", response.status_code)
            await call(s, "save_details", ifsc="sbin0003077", gender="female", marital_status="widow")
            await call(s, "submit_application")
            a = await call(s, "submit_application", declaration_confirmed=True)
            b = await call(s, "submit_application", declaration_confirmed=True)
            assert a["application_no"] == b["application_no"], "retry created a second application"
            await call(s, "get_my_application_status")
    return tok

asyncio.run(main())
