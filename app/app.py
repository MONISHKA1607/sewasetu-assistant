# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import io
import re
import random
import hashlib
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


import requests
import psycopg2
from service import (ACTIVE_STATUSES, BLOCKS, SCHEME_DEADLINE, MIN_AGE, SLA_DAYS, parse_dob,
                     age_on, validate_application, active_application, create_application,
                     submit_application, decide_application, deadline_remaining, hash_password,
                     write_audit, sanitize, ensure_schema, get_db, now_ist)
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort)
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or config.get("app", "secret_key")
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# An applicant may hold at most one application in these states at a time.
# WITHDRAWN and REJECTED applications do not block a fresh application.

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")


import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("Please choose a file to upload (JPG, PNG or PDF, up to 5 MB).")
            return render_template("upload.html")
        filename = f.filename.lower()
        content = f.read()
        if not filename.rsplit(".", 1)[-1] in ("pdf", "jpg", "jpeg", "png"):
            flash("Please upload a JPG, PNG or PDF file.")
            return render_template("upload.html")
        if len(content) > 5 * 1024 * 1024:
            flash("File is larger than 5 MB. Please upload a smaller file.")
            return render_template("upload.html")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, "%s_%s" % (session["verified_mobile"], filename))
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    # Preview everything before the applicant commits.
    data = session.get("form_data", {})
    errors, _ = validate_application(data, session.get("doc_path", ""))
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=os.path.basename(session.get("doc_path", "") or ""))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    errors, cleaned = validate_application(data, session.get("doc_path", ""))
    if errors:
        flash("Please correct the following before submitting: " + "; ".join(errors) + ".")
        return redirect(url_for("form_step", step=1))

    conn = get_db()
    cur = conn.cursor()
    res = submit_application(cur, mobile, data, session.get("doc_path", ""), channel="web")
    if not res["ok"]:
        conn.rollback(); cur.close(); conn.close()
        if res["existing"]:
            e = res["existing"]
            flash("Application %s (%s) already exists for this mobile number. A pending "
                  "application can be withdrawn from the status portal if you need to "
                  "apply afresh." % (e[1], e[2]))
            return redirect(url_for("index"))
        flash("Please correct the following before submitting: " + "; ".join(res["errors"]) + ".")
        return redirect(url_for("form_step", step=1))
    new_id, app_no = res["id"], res["application_no"]
    conn.commit()
    cur.close(); conn.close()

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())



# ---------------------------------------------------------------------------
# Age-proof upload for assistant-started applications.
# A model cannot hand over a file, so the assistant gives the citizen a one-time
# link bound to their (OAuth-verified) mobile number. Same file rules as /upload,
# plus a file-signature check.
# ---------------------------------------------------------------------------

_MAGIC = {"pdf": (b"%PDF",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
          "png": (b"\x89PNG\r\n\x1a\n",)}


def _upload_owner(cur, token):
    cur.execute("SELECT mobile FROM upload_tokens WHERE token_hash = %s AND used_at IS NULL "
                "AND expires_at > %s", (hashlib.sha256(token.encode()).hexdigest(), datetime.now()))
    r = cur.fetchone()
    return r[0] if r else None


@app.route("/agent-upload/<token>", methods=["GET", "POST"])
def agent_upload(token):
    conn = get_db(); cur = conn.cursor()
    mobile = _upload_owner(cur, token)
    if not mobile:
        cur.close(); conn.close()
        return render_template("agent_upload.html", mobile=None, done=False, error=None), 410
    masked = "XXXXXX" + mobile[-4:]
    if request.method == "POST":
        f = request.files.get("document")
        err = None
        content = b""
        ext = ""
        if f is None or not f.filename:
            err = "Please choose a file."
        else:
            ext = f.filename.lower().rsplit(".", 1)[-1]
            content = f.read()
            if ext not in _MAGIC:
                err = "Please upload a JPG, PNG or PDF file."
            elif len(content) > 5 * 1024 * 1024:
                err = "File is larger than 5 MB. Please upload a smaller file."
            elif not any(content.startswith(m) for m in _MAGIC[ext]):
                err = "This file does not look like a real %s." % ext.upper()
        if err:
            cur.close(); conn.close()
            return render_template("agent_upload.html", mobile=masked, done=False, error=err), 400
        os.makedirs(os.path.join(UPLOAD_DIR, "agent"), exist_ok=True)
        path = os.path.join(UPLOAD_DIR, "agent", "%s_%d.%s" % (mobile, int(datetime.now().timestamp()), ext))
        with open(path, "wb") as out:
            out.write(content)
        cur.execute("INSERT INTO agent_drafts (mobile, data, doc_path, updated_at) VALUES (%s, '{}', %s, %s) "
                    "ON CONFLICT (mobile) DO UPDATE SET doc_path = EXCLUDED.doc_path, updated_at = EXCLUDED.updated_at",
                    (mobile, path, datetime.now()))
        cur.execute("UPDATE upload_tokens SET used_at = %s WHERE token_hash = %s",
                    (datetime.now(), hashlib.sha256(token.encode()).hexdigest()))
        conn.commit(); cur.close(); conn.close()
        return render_template("agent_upload.html", mobile=masked, done=True, error=None)
    cur.close(); conn.close()
    return render_template("agent_upload.html", mobile=masked, done=False, error=None)

# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own PENDING application so they can apply afresh."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
        flash("Application %s has been withdrawn. You may submit a new application." % row[0])
    else:
        flash("Only a pending application can be withdrawn.")
    cur.close(); conn.close()
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (ADMIN_PASSWORD and request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    mobile = request.args.get("mobile", "").strip()
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE mobile = %s ORDER BY submitted_at ASC",
                    (mobile,))
        status = "mobile %s" % mobile
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page, mobile=mobile)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit)


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided and when."""
    if not session.get("admin"):
        abort(403)
    actor = session.get("admin_user", ADMIN_USERNAME)
    conn = get_db()
    cur = conn.cursor()
    ok, msg, app_no = decide_application(cur, app_id, new_status, actor, note)
    if ok:
        conn.commit()
        flash("Application %s %s." % (app_no, new_status.lower()))
    else:
        conn.rollback()
        flash("Only a pending application can be decided.")
    cur.close(); conn.close()
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


try:
    ensure_schema()
except Exception as _e:
    app.logger.error('schema migration failed: %s' % _e)

from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
