# -*- coding: utf-8 -*-
"""Citizen MCP server: helps one signed-in citizen check the scheme, apply, and follow up.

Design rules
 - Identity is the OAuth token's subject. No tool accepts a mobile number.
 - Every rule is enforced by service.py (the same code the website uses). The model only talks.
 - Every result says what to do next, because the caller may be a small model with few turns.
"""
import os
import re
import json
import hashlib
import secrets
import difflib
import logging
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from psycopg2.extras import Json

import service
from portal_auth import PortalTokenVerifier, ISSUER, subject_from_token

logging.basicConfig(level=logging.INFO)
PATH = "/mcp/citizen"
RESOURCE = ISSUER + PATH
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/var/sewasetu/uploads")

mcp = FastMCP(
    "Sewa Setu - Citizen assistant",
    instructions=(
        "You help ONE signed-in citizen apply for the Old Age Pension (Rs 250/month, age 60+) and "
        "follow up on it. Start with get_scheme_info. Collect details with save_details (ask for "
        "missing items in plain, simple words; use Hindi/Assamese/English as the person does). "
        "Never invent details, never guess a document, never claim an application exists unless "
        "submit_application or get_my_application_status returned an application number. "
        "Do not promise payment dates."),
    token_verifier=PortalTokenVerifier(RESOURCE, "citizen"),
    auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE, required_scopes=["citizen"],
                      validate_token_resource=True),
    stateless_http=True, json_response=True, streamable_http_path=PATH,
    host="0.0.0.0", port=int(os.environ.get("PORT", "8101")),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

REQUIRED = [("applicant_name", "full name as on the age proof"), ("dob", "date of birth (DD/MM/YYYY)"),
            ("village", "village or town"), ("block", "block"),
            ("bank_account", "bank account number (9-18 digits)")]
SHOULD_ASK = [("ifsc", "bank IFSC code (11 characters, printed on the passbook)"),
              ("gender", "gender (Male/Female/Other)"),
              ("marital_status", "marital status (Married/Unmarried/Widowed)")]


# ------------------------------------------------------------------ helpers
def _mobile():
    tok = get_access_token()
    sub = subject_from_token(tok) if tok else ""
    if not sub.startswith("citizen:"):
        raise PermissionError("not signed in as a citizen")
    return sub.split(":", 1)[1]


def _mask(m):
    return "XXXXXX" + m[-4:]


def _window():
    h = service.deadline_remaining()
    return {"open": h is not None, "hours_left": h,
            "closes": service.SCHEME_DEADLINE.strftime("%d %b %Y %H:%M IST")}


def _norm_block(v):
    v = re.sub(r"\bblock\b", "", (v or ""), flags=re.I).strip().lower()
    if not v:
        return ""
    for b in service.BLOCKS:
        if b.lower() == v:
            return b
    m = difflib.get_close_matches(v, [b.lower() for b in service.BLOCKS], n=1, cutoff=0.7)
    if m:
        return next(b for b in service.BLOCKS if b.lower() == m[0])
    for b in service.BLOCKS:
        if b.lower().startswith(v) or v.startswith(b.lower().split()[0]):
            return b
    return (v or "").title()


def _norm_dob(v):
    v = (v or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return v


def _norm_gender(v):
    m = {"male": "Male", "m": "Male", "man": "Male", "female": "Female", "f": "Female", "woman": "Female",
         "other": "Other", "o": "Other"}
    return m.get((v or "").strip().lower(), (v or "").strip())


def _norm_marital(v):
    m = {"married": "Married", "unmarried": "Unmarried", "single": "Unmarried", "widowed": "Widowed",
         "widow": "Widowed", "widower": "Widowed"}
    return m.get((v or "").strip().lower(), (v or "").strip())


def _load_draft(cur, mobile):
    cur.execute("SELECT data, doc_path FROM agent_drafts WHERE mobile=%s", (mobile,))
    r = cur.fetchone()
    return (dict(r[0] or {}), r[1] or "") if r else ({}, "")


def _doc_ok(path):
    return bool(path) and os.path.exists(path)


def _draft_view(d, doc_path):
    v = {k: val for k, val in d.items() if val}
    if v.get("bank_account"):
        v["bank_account"] = "ending " + re.sub(r"\D", "", v["bank_account"])[-4:]
    v["age_proof_document"] = "uploaded" if _doc_ok(doc_path) else "NOT uploaded"
    return v


def _problems_and_missing(d, doc_path):
    problems = []
    dob = service.parse_dob(d.get("dob"))
    if d.get("dob") and dob is None:
        problems.append("date of birth must be a real date as DD/MM/YYYY")
    elif dob and service.age_on(dob, service.now_ist().date()) < service.MIN_AGE:
        problems.append("NOT ELIGIBLE: the applicant must be %d or older; this date of birth makes them %d. "
                        "Do not submit. Explain kindly, and suggest they ask at the Block Development Office "
                        "about other support schemes."
                        % (service.MIN_AGE, service.age_on(dob, service.now_ist().date())))
    if d.get("block"):
        if d["block"] not in service.BLOCKS:
            problems.append("block must be one of: " + ", ".join(service.BLOCKS))
        elif d["block"] not in service.PILOT_BLOCKS:
            problems.append("assistant applications are not enabled for %s block yet. Tell the citizen to "
                            "apply on the web portal or at the Block Development Office; nothing will be "
                            "created here." % d["block"])
    if d.get("bank_account") and not re.fullmatch(r"\d{9,18}", re.sub(r"[\s-]", "", d["bank_account"])):
        problems.append("bank account number must be 9 to 18 digits")
    if d.get("ifsc") and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", re.sub(r"[\s-]", "", d["ifsc"]).upper()):
        problems.append("IFSC must be 11 characters like SBIN0003077 (4 letters, a zero, 6 letters/digits)")
    if not _window()["open"]:
        problems.append("the application window has closed; no new applications can be submitted")
    missing = [{"field": k, "ask": a} for k, a in REQUIRED if not d.get(k)]
    should = [{"field": k, "ask": a} for k, a in SHOULD_ASK if not d.get(k)]
    if not _doc_ok(doc_path):
        missing.append({"field": "age_proof_document", "ask": "photo/scan of birth certificate, school "
                        "leaving certificate or Voter ID (use get_document_upload_link)"})
    return problems, missing, should


# ------------------------------------------------------------------ tools
@mcp.tool()
def get_scheme_info() -> dict:
    """Official, current rules of the Old Age Pension scheme and whether applications are open.
    Call this first. Also tells you who is signed in (masked) so a shared-phone user can confirm."""
    m = _mobile()
    return {
        "signed_in_citizen": _mask(m),
        "scheme": "Old Age Pension, Government of Purvanchal. Rs 250 per month by bank transfer (DBT).",
        "eligibility": "Resident of Purvanchal aged %d or above. One active application per person." % service.MIN_AGE,
        "application_window": _window(),
        "assistant_enabled_blocks": service.PILOT_BLOCKS,
        "other_blocks": [b for b in service.BLOCKS if b not in service.PILOT_BLOCKS],
        "other_blocks_note": "For these, assistants cannot apply yet: use the web portal or the Block Development Office.",
        "documents": "ONE age proof: birth certificate, school leaving certificate or Voter ID (JPG, PNG or PDF, up to 5 MB).",
        "processing": "A block officer decides within %d days; if no decision in that time the law treats it as approved "
                      "(deemed approval)." % service.SLA_DAYS,
        "next_step": "Ask whether they want to apply or check an existing application. To apply, ask for the "
                     "details and call save_details. To check, call get_my_application_status.",
    }


@mcp.tool()
def save_details(applicant_name: str = "", dob: str = "", gender: str = "", marital_status: str = "",
                 husband_name: str = "", village: str = "", block: str = "", bank_account: str = "",
                 ifsc: str = "") -> dict:
    """Save or correct draft application details for the signed-in citizen. Send only what the citizen
    actually told you; leave other fields empty. Calling with no arguments just shows the draft.
    Drafts are kept on the server, so a broken conversation can be resumed. Returns what is still missing."""
    mobile = _mobile()
    conn = service.get_db(); cur = conn.cursor()
    try:
        d, doc = _load_draft(cur, mobile)
        upd = {"applicant_name": service.sanitize(applicant_name), "dob": _norm_dob(dob),
               "gender": _norm_gender(gender), "marital_status": _norm_marital(marital_status),
               "husband_name": husband_name.strip(), "village": village.strip(),
               "block": _norm_block(block), "bank_account": re.sub(r"[\s-]", "", bank_account),
               "ifsc": re.sub(r"[\s-]", "", ifsc).upper()}
        changed = {k: v for k, v in upd.items() if v}
        d.update(changed)
        if changed:
            cur.execute("INSERT INTO agent_drafts (mobile, data, doc_path, updated_at) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (mobile) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at",
                        (mobile, Json(d), doc or None, datetime.now()))
            conn.commit()
        problems, missing, should = _problems_and_missing(d, doc)
        blocking = any(p.startswith(("NOT ELIGIBLE", "assistant applications", "the application window"))
                       for p in problems)
        if blocking:
            nxt = "STOP. Do not collect more details. Explain the first problem to the citizen kindly."
        elif problems:
            nxt = "Fix the listed problems by asking the citizen to re-confirm those values."
        elif missing or should:
            nxt = "Ask the citizen (one or two items at a time) for the items in 'missing' first, then 'should_ask'."
        else:
            nxt = ("Everything is ready. Read the details back to the citizen, read the declaration aloud "
                   "('I declare the information is true; false information is punishable and leads to cancellation "
                   "of pension'), and only after they agree call submit_application(declaration_confirmed=true).")
        return {"signed_in_citizen": _mask(mobile), "draft": _draft_view(d, doc), "problems": problems,
                "missing": missing, "should_ask": should, "next_step": nxt}
    finally:
        cur.close(); conn.close()


@mcp.tool()
def get_document_upload_link() -> dict:
    """Get a one-time link where the CITIZEN (not you) uploads their age-proof photo/scan in a browser.
    The link works for 30 minutes and only for the signed-in citizen's draft. Give the link to the
    citizen, ask them to upload, then call save_details() with no arguments to confirm it arrived."""
    mobile = _mobile()
    conn = service.get_db(); cur = conn.cursor()
    try:
        _, doc = _load_draft(cur, mobile)
        tok = secrets.token_urlsafe(24)
        cur.execute("INSERT INTO upload_tokens (token_hash, mobile, created_at, expires_at) VALUES (%s,%s,%s,%s)",
                    (hashlib.sha256(tok.encode()).hexdigest(), mobile, datetime.now(),
                     datetime.now() + timedelta(minutes=30)))
        conn.commit()
        return {"upload_link": ISSUER + "/agent-upload/" + tok, "valid_for_minutes": 30,
                "already_uploaded": _doc_ok(doc),
                "accepted": "JPG, PNG or PDF, up to 5 MB: birth certificate, school leaving certificate or Voter ID",
                "next_step": "Give this link to the citizen to open on their phone. You cannot upload for them and "
                             "must not pretend the document exists. After they say it is done, call save_details() "
                             "to check age_proof_document says 'uploaded'. If they have no document now, tell them "
                             "the draft is saved and they can return later."}
    finally:
        cur.close(); conn.close()


@mcp.tool()
def submit_application(declaration_confirmed: bool = False, idempotency_key: str = "") -> dict:
    """Submit the saved draft as the pension application. Only call after the citizen has heard the
    declaration and agreed (declaration_confirmed=true). Safe to retry: a repeat call with the same draft
    returns the same application number instead of creating a second one. If a call times out, call
    get_my_application_status before anything else."""
    mobile = _mobile()
    if not declaration_confirmed:
        return {"submitted": False, "next_step": "Read the declaration to the citizen and get their agreement, "
                "then call again with declaration_confirmed=true."}
    conn = service.get_db(); cur = conn.cursor()
    try:
        d, doc = _load_draft(cur, mobile)
        if not d:
            # A retry after a lost response: the first call already consumed the draft.
            cur.execute("SELECT application_no FROM submission_keys WHERE mobile=%s AND created_at > %s "
                        "ORDER BY created_at DESC LIMIT 1", (mobile, datetime.now() - timedelta(minutes=30)))
            prev = cur.fetchone()
            if prev:
                return {"submitted": True, "application_no": prev[0], "status": "PENDING",
                        "already_submitted_earlier": True,
                        "next_step": "This application was already submitted a moment ago; no second one was "
                                     "created. Tell the citizen the application number."}
            return {"submitted": False, "next_step": "No saved details. Call save_details first."}
        if not _doc_ok(doc):
            return {"submitted": False, "errors": ["age proof document has not been uploaded"],
                    "next_step": "Call get_document_upload_link and have the citizen upload it. Do not submit without it."}
        key = (idempotency_key or "").strip()[:64] or hashlib.sha256(
            json.dumps([d, doc], sort_keys=True).encode()).hexdigest()[:32]
        res = service.submit_application(cur, mobile, d, doc, channel="agent", idem_key=key)
        if res["ok"]:
            if not res["replay"]:
                cur.execute("DELETE FROM agent_drafts WHERE mobile=%s", (mobile,))
            conn.commit()
            return {"submitted": True, "application_no": res["application_no"], "status": "PENDING",
                    "already_submitted_earlier": res["replay"],
                    "next_step": "Tell the citizen the application number. A block officer will decide within %d days. "
                                 "They can ask you for status any time. Do not promise a payment date."
                                 % service.SLA_DAYS}
        conn.rollback()
        if res["existing"]:
            e = res["existing"]
            return {"submitted": False, "already_has_application": True, "application_no": e[1], "status": e[2],
                    "next_step": "No new application was created. Tell the citizen this one already exists and "
                                 "offer to check its status. A pending one can be withdrawn first if they need to re-apply."}
        return {"submitted": False, "errors": res["errors"],
                "next_step": "Explain the problems in simple words. Nothing was created. Fix with save_details if fixable."}
    finally:
        cur.close(); conn.close()


_STATUS_TEXT = {
    "PENDING": "Waiting for the block officer's decision.",
    "APPROVED": "Approved by a block officer.",
    "DEEMED_APPROVED": "Approved automatically under the Right to Public Services Act because it was not decided "
                       "within %d days. No officer personally reviewed it." % service.SLA_DAYS,
    "REJECTED": "Rejected by a block officer.",
    "WITHDRAWN": "Withdrawn by the applicant.",
}


@mcp.tool()
def get_my_application_status() -> dict:
    """Current status of the signed-in citizen's most recent application, straight from the portal."""
    mobile = _mobile()
    conn = service.get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT id, application_no, applicant_name, block, status, submitted_at, decided_at, decided_by, "
                    "bank_account FROM applications WHERE mobile=%s ORDER BY submitted_at DESC LIMIT 1", (mobile,))
        r = cur.fetchone()
        if not r:
            d, doc = _load_draft(cur, mobile)
            return {"signed_in_citizen": _mask(mobile), "has_application": False, "has_saved_draft": bool(d),
                    "next_step": "No application exists for this mobile number. Offer to help apply "
                                 "(get_scheme_info, then save_details)."}
        out = {"signed_in_citizen": _mask(mobile), "has_application": True, "application_no": r[1],
               "applicant_name": r[2], "block": r[3], "status": r[4], "meaning": _STATUS_TEXT.get(r[4], r[4]),
               "submitted_on": r[5].strftime("%d %b %Y") if r[5] else None,
               "decided_on": r[6].strftime("%d %b %Y") if r[6] else None,
               "bank_account": "ending " + (r[8] or "")[-4:]}
        if r[4] == "PENDING" and r[5]:
            out["deemed_approval_on_or_after"] = (r[5] + timedelta(days=service.SLA_DAYS)).strftime("%d %b %Y")
        if r[4] == "APPROVED":
            out["decided_by"] = r[7]
        if r[4] == "REJECTED":
            cur.execute("SELECT note FROM audit_log WHERE application_id=%s AND action='REJECTED' "
                        "ORDER BY at DESC LIMIT 1", (r[0],))
            n = cur.fetchone()
            out["reason_recorded"] = (n[0] if n and n[0] else "no reason recorded")
            out["can_apply_again"] = True
        out["payment_note"] = ("This system records the decision only, not payment dates. Do not state a date for the "
                               "first payment; for payment questions the citizen should ask the Block Development Office.")
        out["next_step"] = {"PENDING": "Tell them it is waiting and when it would be deemed approved if not decided. "
                                       "Offer withdrawal only if they want to correct a mistake.",
                            "REJECTED": "Share the reason gently; they may apply again with corrected details."}.get(
            r[4], "Report this status plainly.")
        return out
    finally:
        cur.close(); conn.close()


@mcp.tool()
def withdraw_application(confirm: bool = False) -> dict:
    """Withdraw the citizen's PENDING application (e.g. to correct a mistake and apply afresh).
    Only after the citizen clearly asks for it; call with confirm=true. Cannot be undone."""
    mobile = _mobile()
    if not confirm:
        return {"withdrawn": False, "next_step": "Ask the citizen to confirm they want to withdraw the pending "
                "application (cannot be undone), then call with confirm=true."}
    conn = service.get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM applications WHERE mobile=%s AND status='PENDING' ORDER BY submitted_at DESC LIMIT 1",
                    (mobile,))
        r = cur.fetchone()
        n = service.withdraw_application(cur, mobile, r[0], channel="agent") if r else None
        conn.commit()
        if n:
            return {"withdrawn": True, "application_no": n,
                    "next_step": "Confirm it is withdrawn. They may now submit a new application (save_details again)."}
        return {"withdrawn": False, "next_step": "There is no pending application to withdraw (approved, rejected "
                "or already withdrawn ones cannot be withdrawn). Use get_my_application_status to explain."}
    finally:
        cur.close(); conn.close()


if __name__ == "__main__":
    try:
        service.ensure_schema()
    except Exception:
        logging.exception("schema check failed")
    mcp.run(transport="streamable-http")
