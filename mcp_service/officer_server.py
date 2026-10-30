# -*- coding: utf-8 -*-
"""Officer MCP server: helps a block officer see the queue, inspect a case, and record a decision.

The officer stays the decision-maker. The server enforces: officer-only tokens, pilot-block scope,
pending-only decisions, a mandatory rejection reason, and attribution + audit for every decision.
It always reports the status read back from the database, never an assumption.
"""
import os
import logging
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings

import service
from portal_auth import PortalTokenVerifier, ISSUER, subject_from_token

logging.basicConfig(level=logging.INFO)
PATH = "/mcp/officer"
RESOURCE = ISSUER + PATH

mcp = FastMCP(
    "Sewa Setu - Officer assistant",
    instructions=(
        "You assist a block officer reviewing Old Age Pension applications. Start with queue_summary, "
        "then list_pending, then get_application for the case. Summarise facts and flag concerns, but the "
        "OFFICER decides: only call approve_application or reject_application after the officer has "
        "explicitly told you the decision (and, for rejection, the reason in their own words). Never "
        "invent a reason. Report the status the tool returns."),
    token_verifier=PortalTokenVerifier(RESOURCE, "officer"),
    auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE, required_scopes=["officer"],
                      validate_token_resource=True),
    stateless_http=True, json_response=True, streamable_http_path=PATH,
    host="0.0.0.0", port=int(os.environ.get("PORT", "8102")),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _officer():
    tok = get_access_token()
    sub = subject_from_token(tok) if tok else ""
    if not sub.startswith("officer:"):
        raise PermissionError("not signed in as a officer")
    return sub.split(":", 1)[1]


def _mask(v, keep=4):
    v = v or ""
    return ("X" * max(0, len(v) - keep)) + v[-keep:]


def _scope_clause():
    return "block = ANY(%s)", (list(service.PILOT_BLOCKS),)


def _days_left(submitted):
    if not submitted:
        return None
    return service.SLA_DAYS - (service.now_ist() - submitted).days


@mcp.tool()
def queue_summary() -> dict:
    """Counts of what is waiting, per block, plus how many are about to be (or overdue to be) deemed
    approved by the statutory %d-day rule. Call this first.""" % service.SLA_DAYS
    who = _officer()
    conn = service.get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT block, count(*), min(submitted_at) FROM applications "
                    "WHERE status='PENDING' AND block = ANY(%s) GROUP BY block ORDER BY count(*) DESC",
                    (list(service.PILOT_BLOCKS),))
        rows = cur.fetchall()
        cutoff = datetime.now() - timedelta(days=service.SLA_DAYS - 3)
        cur.execute("SELECT count(*) FROM applications WHERE status='PENDING' AND block = ANY(%s) "
                    "AND submitted_at < %s", (list(service.PILOT_BLOCKS), cutoff))
        urgent = cur.fetchone()[0]
        total = sum(r[1] for r in rows)
        return {"officer": who, "blocks_in_scope": service.PILOT_BLOCKS, "pending_total": total,
                "by_block": [{"block": r[0], "pending": r[1], "oldest_submitted": r[2].strftime("%d %b %Y")}
                             for r in rows],
                "urgent_within_3_days_of_deemed_approval": urgent,
                "next_step": "Call list_pending (oldest first) and offer to walk through the most urgent cases."
                             if total else "Nothing is waiting in your blocks. Tell the officer the queue is empty."}
    finally:
        cur.close(); conn.close()


@mcp.tool()
def list_pending(block: str = "", limit: int = 15, offset: int = 0) -> dict:
    """Pending applications, oldest first (most urgent first), optionally for one block.
    Shows days left before the law deems it approved. Use offset to page."""
    _officer()
    limit = max(1, min(int(limit or 15), 50)); offset = max(0, int(offset or 0))
    blocks = list(service.PILOT_BLOCKS)
    b = (block or "").strip().lower()
    if b:
        match = [x for x in service.BLOCKS if x.lower() == b.replace(" block", "")]
        if not match or match[0] not in service.PILOT_BLOCKS:
            return {"error": "that block is not in your scope", "blocks_in_scope": service.PILOT_BLOCKS}
        blocks = match
    conn = service.get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT application_no, applicant_name, block, village, submitted_at FROM applications "
                    "WHERE status='PENDING' AND block = ANY(%s) ORDER BY submitted_at ASC LIMIT %s OFFSET %s",
                    (blocks, limit, offset))
        rows = cur.fetchall()
        return {"applications": [{"application_no": r[0], "name": r[1], "block": r[2], "village": r[3],
                                  "submitted": r[4].strftime("%d %b %Y"), "days_left_before_deemed_approval": _days_left(r[4])}
                                 for r in rows],
                "returned": len(rows), "offset": offset,
                "next_step": "Use get_application(application_no) to review a case. If 'returned' equals the limit, "
                             "more may exist: call again with a larger offset."}
    finally:
        cur.close(); conn.close()


def _fetch(cur, application_no):
    cur.execute("SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status, husband_name, "
                "village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE application_no=%s", (application_no.strip().upper(),))
    return cur.fetchone()


@mcp.tool()
def get_application(application_no: str) -> dict:
    """Full review view of one application: details (bank account and mobile masked), the audit trail,
    and checks that help the officer (age at submission, document on record, bank account shared with other
    applications). Does not decide anything."""
    _officer()
    conn = service.get_db(); cur = conn.cursor()
    try:
        r = _fetch(cur, application_no)
        if not r or r[9] not in service.PILOT_BLOCKS:
            return {"error": "no such application in your blocks", "application_no": application_no}
        cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id=%s ORDER BY at", (r[0],))
        audit = [{"at": a[0].strftime("%d %b %Y %H:%M"), "action": a[1], "actor": a[2], "note": a[3] or ""}
                 for a in cur.fetchall()]
        cur.execute("SELECT count(*) FROM applications WHERE bank_account=%s AND id<>%s", (r[10], r[0]))
        shared = cur.fetchone()[0]
        age_at_sub = service.age_on(r[4], r[14].date()) if r[4] and r[14] else None
        flags = []
        if not r[12]:
            flags.append("no age-proof document on record")
        if age_at_sub is not None and age_at_sub < service.MIN_AGE:
            flags.append("applicant was under %d at submission (age %d)" % (service.MIN_AGE, age_at_sub))
        if shared:
            flags.append("bank account also appears on %d other application(s)" % shared)
        return {"application_no": r[1], "status": r[13], "name": r[2], "mobile": _mask(r[3]), "dob": str(r[4]),
                "age_at_submission": age_at_sub, "gender": r[5], "marital_status": r[6], "late_husband": r[7] or None,
                "village": r[8], "block": r[9], "bank_account": _mask(r[10]), "ifsc": r[11],
                "age_proof_on_record": bool(r[12]), "submitted": r[14].strftime("%d %b %Y"),
                "days_left_before_deemed_approval": _days_left(r[14]) if r[13] == "PENDING" else None,
                "decided_at": r[15].strftime("%d %b %Y %H:%M") if r[15] else None, "decided_by": r[16],
                "flags": flags, "audit_trail": audit,
                "next_step": ("Summarise for the officer and mention any flags. Ask what they want to do: approve, "
                              "reject (needs a reason), or leave it." if r[13] == "PENDING" else
                              "This case is already decided (%s); it cannot be changed here." % r[13])}
    finally:
        cur.close(); conn.close()


def _decide(application_no, status, note):
    who = _officer()
    conn = service.get_db(); cur = conn.cursor()
    try:
        r = _fetch(cur, application_no)
        if not r or r[9] not in service.PILOT_BLOCKS:
            return {"decided": False, "error": "no such application in your blocks"}
        ok, msg, no = service.decide_application(cur, r[0], status, who, note, require_reason=True,
                                                  actor_label="officer")
        if not ok:
            conn.rollback()
            cur.execute("SELECT status FROM applications WHERE id=%s", (r[0],))
            return {"decided": False, "application_no": r[1], "current_status": cur.fetchone()[0], "reason": msg,
                    "next_step": "Nothing was changed. Tell the officer why."}
        conn.commit()
        cur.execute("SELECT status, decided_by, decided_at FROM applications WHERE id=%s", (r[0],))
        st, by, at = cur.fetchone()
        return {"decided": True, "application_no": r[1], "status": st, "recorded_against": "officer:" + by,
                "decided_at": at.strftime("%d %b %Y %H:%M"), "reason_recorded": (note or "").strip() or None,
                "next_step": "Confirm the decision to the officer exactly as returned here."}
    finally:
        cur.close(); conn.close()


@mcp.tool()
def approve_application(application_no: str, note: str = "") -> dict:
    """Record APPROVAL of a pending application against the signed-in officer. Only call after the officer
    has explicitly said to approve this case. Irreversible. Optional note."""
    return _decide(application_no, "APPROVED", note)


@mcp.tool()
def reject_application(application_no: str, reason: str) -> dict:
    """Record REJECTION of a pending application against the signed-in officer. Needs the officer's own
    reason (10-500 characters), which is kept in the audit trail and shown to the applicant. Never make up
    a reason. Irreversible."""
    return _decide(application_no, "REJECTED", reason)


if __name__ == "__main__":
    try:
        service.ensure_schema()
    except Exception:
        logging.exception("schema check failed")
    mcp.run(transport="streamable-http")
