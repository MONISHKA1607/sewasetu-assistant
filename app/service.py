# -*- coding: utf-8 -*-
"""
Sewa Setu shared service layer.

The single source of truth for scheme rules, application creation and officer
decisions. The Flask web portal AND the MCP servers call these functions, so no
channel can admit an application (or record a decision) another channel would refuse.
"""
import os
import re
import random
import hashlib
import configparser
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")
BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"), "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")
PILOT_BLOCKS = [b.strip() for b in
                os.environ.get("PILOT_BLOCKS", config.get("agent", "pilot_blocks", fallback=",".join(BLOCKS))).split(",")
                if b.strip()]
REASON_MIN, REASON_MAX = 10, 500


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def get_db():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST") or config.get("database", "host"), port=config.get("database", "port"),
        dbname=config.get("database", "name"), user=config.get("database", "user"),
        password=os.environ.get("DB_PASSWORD") or config.get("database", "password"))


def sanitize(value, maxlen=100):
    if value is None:
        return ""
    return value.strip()[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)", (app_id, action, actor, (note or "")[:500], datetime.now()))


# --------------------------------------------------------------------------- schema
def ensure_schema():
    """Idempotent migrations: docker initdb scripts run only on a fresh volume."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_drafts (
            mobile VARCHAR(15) PRIMARY KEY, data JSONB, doc_path VARCHAR(200), updated_at TIMESTAMP);
        CREATE TABLE IF NOT EXISTS submission_keys (
            mobile VARCHAR(15), idem_key VARCHAR(64), application_no VARCHAR(20),
            created_at TIMESTAMP, PRIMARY KEY (mobile, idem_key));
        CREATE TABLE IF NOT EXISTS upload_tokens (
            token_hash VARCHAR(64) PRIMARY KEY, mobile VARCHAR(15), created_at TIMESTAMP,
            expires_at TIMESTAMP, used_at TIMESTAMP);
        CREATE TABLE IF NOT EXISTS eval_runs (
            id SERIAL PRIMARY KEY, run_at TIMESTAMP, scenario VARCHAR(60), persona VARCHAR(60),
            role VARCHAR(10), goal TEXT, passed BOOLEAN, failure_category VARCHAR(60),
            transcript JSONB, tool_calls JSONB, assertions JSONB, note TEXT);
    """)
    cur.execute("""
        ALTER TABLE oauth_refresh_tokens ADD COLUMN IF NOT EXISTS sid VARCHAR(32);
        CREATE TABLE IF NOT EXISTS revoked_sessions (sid VARCHAR(32) PRIMARY KEY, revoked_at TIMESTAMP);
    """)
    # application_no had no uniqueness guarantee; seed has none duplicated, so this is safe
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_app_no ON applications(application_no)")
    conn.commit(); cur.close(); conn.close()


# --------------------------------------------------------------------------- rules
def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    return None if delta.total_seconds() <= 0 else int(delta.total_seconds() // 3600)


def validate_application(data, doc_path):
    """Returns (errors, cleaned). Unchanged behaviour from the original portal."""
    errors = []
    cleaned = {
        "applicant_name": sanitize(data.get("applicant_name")),
        "village": (data.get("village") or "").strip(),
        "block": (data.get("block") or "").strip(),
        "gender": data.get("gender") or "",
        "marital_status": data.get("marital_status") or "",
        "husband_name": (data.get("husband_name") or "").strip(),
        "husband_employer": "",
        "bank_account": re.sub(r"[\s-]", "", data.get("bank_account") or ""),
        "ifsc": re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper(),
        "doc_path": doc_path or "", "dob": None,
    }
    for label, key in (("full name", "applicant_name"), ("village", "village"),
                       ("block", "block"), ("bank account", "bank_account")):
        if not cleaned[key]:
            errors.append("%s is required" % label)
    if cleaned["block"] and cleaned["block"] not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))
    if cleaned["bank_account"] and not re.fullmatch(r"\d{9,18}", cleaned["bank_account"]):
        errors.append("bank account number must be 9 to 18 digits")
    if cleaned["ifsc"] and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        if age_on(dob, now_ist().date()) < MIN_AGE:
            errors.append("applicant must be %d years of age or above" % MIN_AGE)
    if not cleaned["doc_path"]:
        errors.append("age proof document is required")
    if now_ist() > SCHEME_DEADLINE:
        errors.append("the application window has closed")
    return errors, cleaned


def active_application(cur, mobile):
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN %s ORDER BY submitted_at DESC LIMIT 1",
                (mobile, ACTIVE_STATUSES))
    return cur.fetchone()


def new_application_no(cur):
    """SSP + 2-digit year + 6 digits, retried until unused (the old code never checked)."""
    for _ in range(50):
        no = "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))
        cur.execute("SELECT 1 FROM applications WHERE application_no = %s", (no,))
        if not cur.fetchone():
            return no
    raise RuntimeError("could not allocate a unique application number")


def create_application(cur, mobile, cleaned, channel="web"):
    app_no = new_application_no(cur)
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s) RETURNING id""",
        (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
         cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
         cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
         cleaned["doc_path"], datetime.now()))
    new_id = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(cleaned["dob"].strftime("%d%m%Y"))))
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile, "channel=%s" % channel)
    return new_id, app_no


def submit_application(cur, mobile, data, doc_path, channel="web", idem_key=None):
    """The one submission path for every channel. Caller commits.

    Serialises per mobile (advisory lock held to end of transaction), replays an
    idempotency key instead of creating a second row, enforces one active
    application, then validates and creates.
    Returns dict: ok, application_no, id, errors, existing, replay.
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("submit:" + mobile,))
    if idem_key:
        cur.execute("SELECT application_no FROM submission_keys WHERE mobile=%s AND idem_key=%s",
                    (mobile, idem_key))
        r = cur.fetchone()
        if r:
            cur.execute("SELECT id FROM applications WHERE application_no=%s", (r[0],))
            return {"ok": True, "application_no": r[0], "id": cur.fetchone()[0],
                    "errors": [], "existing": None, "replay": True}
    existing = active_application(cur, mobile)
    if existing:
        return {"ok": False, "errors": [], "existing": existing, "replay": False}
    errors, cleaned = validate_application(data, doc_path)
    if channel == "agent" and cleaned["block"] and cleaned["block"] not in PILOT_BLOCKS:
        errors.append("assistant-submitted applications are not yet enabled for %s block; "
                      "apply on the web portal or at the Block Development Office"
                      % cleaned["block"])
    if errors:
        return {"ok": False, "errors": errors, "existing": None, "replay": False}
    new_id, app_no = create_application(cur, mobile, cleaned, channel)
    if idem_key:
        cur.execute("INSERT INTO submission_keys (mobile, idem_key, application_no, created_at) "
                    "VALUES (%s,%s,%s,%s)", (mobile, idem_key, app_no, datetime.now()))
    return {"ok": True, "application_no": app_no, "id": new_id, "errors": [],
            "existing": None, "replay": False}


# --------------------------------------------------------------------------- decisions
def decide_application(cur, app_id, new_status, actor, note="", require_reason=False):
    """Approve/reject a PENDING application. Caller commits.
    actor is e.g. 'admin' / 'officer-name'. Returns (ok, message, application_no)."""
    if new_status not in ("APPROVED", "REJECTED"):
        return False, "invalid decision", None
    note = (note or "").strip()
    if require_reason and new_status == "REJECTED" and not (REASON_MIN <= len(note) <= REASON_MAX):
        return False, "a rejection needs a reason of %d to %d characters" % (REASON_MIN, REASON_MAX), None
    cur.execute("UPDATE applications SET status=%s, decided_at=%s, decided_by=%s "
                "WHERE id=%s AND status='PENDING' RETURNING application_no",
                (new_status, datetime.now(), actor, app_id))
    row = cur.fetchone()
    if not row:
        return False, "only a pending application can be decided", None
    write_audit(cur, app_id, new_status, "admin:%s" % actor, note)
    return True, "ok", row[0]


def withdraw_application(cur, mobile, app_id, channel="web"):
    """Applicant withdraws their own PENDING application. Caller commits. Returns application_no or None."""
    cur.execute("UPDATE applications SET status='WITHDRAWN', decided_at=%s, decided_by='APPLICANT' "
                "WHERE id=%s AND mobile=%s AND status='PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile, "channel=%s" % channel)
    return row[0] if row else None
