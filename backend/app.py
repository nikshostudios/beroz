#!/usr/bin/env python3
# DO NOT generate any summary documents, Word files, or additional output files
# after running this script. Terminal output only.
"""
Recruitment Agent — Flask Web App
Production-ready web interface for resume parsing and screening.
"""

import io
import os
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import json
import re
import smtplib
import subprocess
import tempfile
import threading
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

from flask import (
    Flask, request, redirect, url_for, session, Response,
    stream_with_context, send_file, jsonify
)
from werkzeug.security import check_password_hash
import fitz  # PyMuPDF for PDF JD extraction
from docx import Document
from anthropic import Anthropic
import gspread
from google.oauth2.service_account import Credentials

DASHBOARD_DIST = Path(__file__).parent / "dashboard" / "dist"
FRONTEND_DIR = Path(__file__).parent.parent  # Root of project (where index.html lives)
app = Flask(__name__, static_folder=None)
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.secret_key = _secret
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

from outreach import outreach_bp
app.register_blueprint(outreach_bp)

SCRIPT_DIR = Path(__file__).parent
RESUMES_DIR = SCRIPT_DIR / "resumes"
JD_FILE = SCRIPT_DIR / "jd.txt"
REPORT_FILE = SCRIPT_DIR / "last_screening_results.json"

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SCREENED_SHEET_ID = os.environ.get("SCREENED_SHEET_ID", "")

SCREEN_PROFILE_DIR = SCRIPT_DIR / "screen_profile_tmp"
SUBMISSION_RESUMES_DIR = SCRIPT_DIR / "submission_resumes"

ALLOWED_RESUME_EXT = {".pdf", ".docx", ".doc"}
ALLOWED_JD_EXT = {".txt", ".pdf"}

SESSION_VERSION = "5"

CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
SOURCE_TMP_DIR = SCRIPT_DIR / "source_tmp"
SOURCING_SHEET_ID = os.environ.get("SOURCING_SHEET_ID", "")

SOURCE_RECRUITERS = {
    "Devesh Jhinga": "dev@exceltechcomputers.com",
    "Manoj Thakran": "manoj@exceltechcomputers.com",
    "Raju Akula": "raju.akula@exceltechcomputers.com",
    "Atul Kumar": "atul@exceltechcomputers.com",
    "Rohit Kumar": "rohit@exceltechcomputers.com",
    "Raghav Sharma": "raghav@exceltechcomputers.com",
    "Priya": "priya@exceltechcomputers.com",
    "Narender Kumar": "narender@exceltechcomputers.com",
    "Recruit Email": "recruit@exceltechcomputers.com",
}

RECRUITER_LOGINS = {
    "devesh": {"password": "pbkdf2:sha256:1000000$zs3rmQ5dPLuicYIc$141877ac3b11225855d98268e900c5e0baaefd1a0b465e88b50e8fd985b1f1fb", "name": "Devesh Jhinga", "email": "dev@exceltechcomputers.com", "role": "recruiter"},
    "manoj": {"password": "pbkdf2:sha256:1000000$CdnMd8zBFbMbRsrY$ccbbf73f8db12c1239d5df6d7971163340d2a75aa5814769ed323181a6294f2d", "name": "Manoj Thakran", "email": "manoj@exceltechcomputers.com", "role": "recruiter"},
    "raju": {"password": "pbkdf2:sha256:1000000$04koQd6DOQdCncx9$2a8801c0e313d731cc5481c6d197385259a0ced33df13c2767d24d70c721ef6c", "name": "Raju Akula", "email": "raju.akula@exceltechcomputers.com", "role": "tl"},
    "atul": {"password": "pbkdf2:sha256:1000000$fLS6nn9isnX3gFl7$c0ef42a3daaedff1e3cfc869b17f5e3add612d125751c91217cb07f1bf55a62d", "name": "Atul Kumar", "email": "atul@exceltechcomputers.com", "role": "recruiter"},
    "rohit": {"password": "pbkdf2:sha256:1000000$mDxhtmr3c9xNKT2i$0d3afe96dd883f24f3c099318d081f233639af6e0ebb220b477c5191763bceb5", "name": "Rohit Kumar", "email": "rohit@exceltechcomputers.com", "role": "recruiter"},
    "raghav": {"password": "pbkdf2:sha256:1000000$cUaxyIf2OWTcUk1T$aa7bff19b28193860183144f595687f44f785cc9fec5ddb5d54fcb7e2d689386", "name": "Raghav Sharma", "email": "raghav@exceltechcomputers.com", "role": "recruiter"},
    "priya": {"password": "pbkdf2:sha256:1000000$NvRnlnZltFFRqUpI$5cbd59985fdb390003d1d7aef6ea7cb6d172c974e3d1af6131cc748badba7a31", "name": "Priya", "email": "priya@exceltechcomputers.com", "role": "recruiter"},
    "narender": {"password": "pbkdf2:sha256:1000000$mfVcCE09yApnLLNp$dd179ca93a2fce1002e76ec2ae5090829c9ffc879b045bcb3dc5b4bed820cb8d", "name": "Narender Kumar", "email": "narender@exceltechcomputers.com", "role": "recruiter"},
    "recruit": {"password": "pbkdf2:sha256:1000000$7bWtXpGERBVjVwTM$af6fd6c5bd8d97691abe495cd4e820e632dd96513337c079a4cd79939d5f88c4", "name": "Recruit Email", "email": "recruit@exceltechcomputers.com", "role": "recruiter"},
}

# ── AI agent core (formerly a separate FastAPI service on localhost:8001).
# Now merged into this Flask process — see backend/ai_agents/core.py.
from ai_agents import core as ai_core
ai_core.init()

# ── Background scheduler for sequence_tick + inbox_poll ──
if os.environ.get("ENABLE_SCHEDULER") == "1":
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    from ai_agents.config.cron import _cron_log

    if not os.environ.get("RECRUITER_EMAILS"):
        _cron_log("WARNING: RECRUITER_EMAILS is not set — inbox_poll_cron will do nothing")

    def _bg_sequence_tick(user_role=None):
        return ai_core.sequence_tick(user_role)

    def _bg_inbox_poll():
        try:
            result = ai_core.process_inbox({"recruiter_email": None})
            _cron_log(f"inbox_poll OK: {result}")
        except Exception as e:
            _cron_log(f"inbox_poll ERR: {e}")

    _scheduler.add_job(
        _bg_sequence_tick, "interval", minutes=5,
        id="sequence_tick_cron", misfire_grace_time=60, max_instances=1,
    )
    _scheduler.add_job(
        _bg_inbox_poll, "interval", minutes=15,
        id="inbox_poll_cron", misfire_grace_time=60, max_instances=1,
    )
    _scheduler.start()
    _cron_log("BackgroundScheduler started: sequence_tick/5m, inbox_poll/15m")


def _ai_core_call(fn, *args, **kwargs):
    """Invoke an ai_core handler and translate CoreError → (body, status)."""
    try:
        return jsonify(fn(*args, **kwargs)), 200
    except ai_core.CoreError as e:
        return jsonify({"error": e.message}), e.status
    except Exception as e:
        app.logger.exception("ai_core handler %s crashed", getattr(fn, "__name__", "?"))
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ── Startup healthcheck ────────────────────────────────────────
# Verify external dependencies are reachable at boot. Log loudly if not —
# this is the line that would have caught the original FastAPI-never-deployed
# bug on day one. Keep this pattern for any future external service.
try:
    _supabase = ai_core.db.get_client()
    _critical_tables = (
        "requirements",
        "candidate_details",
        "candidate_shortlists",
        "candidate_notes",
        "submissions",
        "outreach_log",
        "agentic_boost_runs",
    )
    _missing_tables = []
    for _table in _critical_tables:
        try:
            _supabase.table(_table).select("id").limit(1).execute()
        except Exception as _table_err:
            _missing_tables.append((_table, _table_err))
    if _missing_tables:
        _details = "; ".join(
            f"{_table}: {_err}" for _table, _err in _missing_tables
        )
        app.logger.error(
            "[startup] Supabase reachable but schema is incomplete: %s",
            _details,
        )
    else:
        app.logger.info("[startup] Supabase reachable and critical tables present")
except Exception as _e:
    app.logger.error("[startup] Supabase unreachable: %s — DB-backed routes will fail", _e)

# Template for future external-service healthchecks (e.g. Foundit, Apollo):
#
# try:
#     http_requests.get(f"{SOME_SERVICE_URL}/health", timeout=2).raise_for_status()
#     app.logger.info("[startup] %s reachable", SOME_SERVICE_URL)
# except Exception as _e:
#     app.logger.error("[startup] %s unreachable: %s", SOME_SERVICE_URL, _e)


def _password_env_key(email: str) -> str:
    local = email.split("@")[0].replace(".", "_").upper()
    return f"OUTLOOK_PASSWORD_{local}"


def _extract_text_from_pdf_source(filepath: Path) -> str:
    text = ""
    with fitz.open(filepath) as doc:
        for page in doc:
            text += page.get_text()
    text = text.strip()
    if text:
        return text
    # Fallback: image-based PDF — use Claude vision to OCR
    return _ocr_pdf_with_vision(filepath)


def _ocr_pdf_with_vision(filepath: Path) -> str:
    """Extract text from image-based PDFs using Claude vision API."""
    import base64
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""
    client = Anthropic(api_key=api_key)
    all_text = []
    with fitz.open(filepath) as doc:
        for page_num, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": "Extract ALL text from this image exactly as written. Preserve the layout and structure. Return ONLY the extracted text, no commentary."},
                    ],
                }],
            )
            all_text.append(response.content[0].text.strip())
    return "\n\n".join(all_text)


def _extract_text_from_docx(filepath: Path) -> str:
    doc = Document(filepath)
    return "\n".join(p.text for p in doc.paragraphs).strip()


def _extract_text(filepath: Path) -> str:
    suffix = filepath.suffix.lower()
    if suffix == ".pdf":
        return _extract_text_from_pdf_source(filepath)
    elif suffix in (".docx", ".doc"):
        return _extract_text_from_docx(filepath)
    return ""


def _parse_api_response(raw: str) -> dict:
    raw = raw.strip()
    # Remove markdown code fences if present
    if "```" in raw:
        lines = raw.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        raw = "\n".join(lines).strip()
    # If there's extra text around JSON, extract the JSON object
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]
    return json.loads(raw)


def _screen_candidate(client: Anthropic, resume_text: str, jd_text: str) -> dict:
    system_prompt = (
        "You are a strict, experienced recruitment screener. Evaluate the resume against the job description with high standards. "
        "Be critical and realistic — do NOT inflate scores.\n\n"
        "EVALUATION CRITERIA (check each carefully):\n"
        "1. REQUIRED SKILLS MATCH: Does the candidate have the specific technical skills, tools, and technologies listed as required? Count how many required skills are present vs missing.\n"
        "2. EXPERIENCE LEVEL: Does the candidate's years and depth of experience match what the JD requires? Junior candidates should not score high for senior roles.\n"
        "3. DOMAIN/INDUSTRY FIT: Has the candidate worked in the same or closely related industry/domain?\n"
        "4. ROLE RELEVANCE: Is the candidate's recent work history aligned with this specific role, or is it a different function entirely?\n"
        "5. LOCATION/LOGISTICS: Consider any visa, relocation, or availability concerns if mentioned.\n\n"
        "SCORING RULES (be strict):\n"
        "- 9-10 (Excellent Match): Meets ALL required skills, experience level matches or exceeds, strong domain fit. Rare — only for near-perfect matches.\n"
        "- 7-8 (Strong Match): Meets MOST required skills (80%+), experience is close to required, relevant domain.\n"
        "- 5-6 (Good Match): Meets SOME required skills (50-80%), may lack experience depth or domain fit. Worth considering but has gaps.\n"
        "- 3-4 (Weak Match): Meets FEW required skills (<50%), significant experience or domain gaps.\n"
        "- 1-2 (Rejected): Minimal or no overlap with requirements.\n\n"
        "IMPORTANT: A candidate who has general IT experience but lacks the SPECIFIC skills in the JD should score 4-5 at most, not 7+. "
        "Partial keyword matches (e.g. candidate knows Python but JD requires Embedded C) do NOT count as a skill match.\n\n"
        "Return ONLY valid JSON with these exact keys:\n"
        '  "score": integer from 1 to 10\n'
        '  "label": exactly one of "Excellent Match", "Strong Match", "Good Match", "Rejected"\n'
        '  "reason": one sentence explaining the score, mentioning specific skills matched and missing\n'
        '  "skillset": 1-2 words MAXIMUM for the primary job role keyword (e.g. "Python", "Embedded C", "DevOps"). Or "" if not found.\n'
        '  "name": full name of the candidate, or "" if not found\n'
        '  "contact_no": phone number, or "" if not found\n'
        '  "email": email address, or "" if not found\n'
        "Do NOT guess or fabricate. If not clearly present, use empty string.\n"
        "Label mapping: 9-10 = Excellent Match, 7-8 = Strong Match, 5-6 = Good Match, 1-4 = Rejected."
    )
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Job Description:\n\n{jd_text}\n\n---\n\nResume text:\n\n{resume_text}"}],
    )
    try:
        return _parse_api_response(response.content[0].text)
    except (json.JSONDecodeError, IndexError):
        return {"score": 0, "label": "Rejected", "reason": "Parse error",
                "skillset": "", "name": "", "contact_no": "", "email": ""}


def _extract_jd_details(client: Anthropic, jd_text: str) -> dict:
    system_prompt = (
        "Extract job details from this job description. "
        "Return ONLY valid JSON with these exact keys:\n"
        '  "job_title": the job title or role name\n'
        '  "summary": 2-3 sentence summary covering role, key responsibilities, and requirements\n'
        "Be concise and professional."
    )
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": jd_text}],
    )
    try:
        return _parse_api_response(response.content[0].text)
    except (json.JSONDecodeError, IndexError):
        return {"job_title": "Open Position", "summary": "Please see the attached job description for details."}


def _get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
    else:
        if not CREDENTIALS_FILE.exists():
            return None
        creds = Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=scopes)
    return gspread.authorize(creds)


def _get_worksheet(spreadsheet, name):
    for ws in spreadsheet.worksheets():
        if ws.title == name or ws.title.strip() == name.strip():
            return ws
    return None


def _log_to_sheet(recruiter_name, candidate, client_name, location, status):
    sheet_id = os.environ.get("SOURCING_SHEET_ID")
    if not sheet_id:
        return "SOURCING_SHEET_ID not set"
    gc = _get_gspread_client()
    if not gc:
        return "Google credentials not configured"
    try:
        spreadsheet = gc.open_by_key(sheet_id)
    except Exception as e:
        return f"Cannot open sheet: {e}"
    today = date.today().strftime("%d-%b-%y-%a")
    contact = str(candidate.get("contact_no", "")).strip()
    if contact.endswith(".0"):
        contact = contact[:-2]
    contact = re.sub(r"[^0-9+\s()\-]", "", contact)
    row = [
        today, recruiter_name, candidate.get("name", "").title(),
        candidate.get("skillset", ""), candidate.get("email", ""),
        contact, client_name, location, status,
    ]
    for tab_name in [recruiter_name, "MasterList"]:
        ws = _get_worksheet(spreadsheet, tab_name)
        if ws:
            all_vals = ws.get_all_values()
            next_row = len(all_vals) + 1
            for col_idx, val in enumerate(row, start=1):
                ws.update_cell(next_row, col_idx, val)
    return "ok"


# --- Auth ---

def is_logged_in():
    return (session.get("logged_in") == True and
            session.get("version") == SESSION_VERSION)


# --- New React Dashboard (served from dashboard/dist/) ---

@app.route("/new")
@app.route("/new/")
@app.route("/new/<path:path>")
def new_dashboard(path=""):
    """Serve the React dashboard. All sub-routes fall back to index.html for client-side routing."""
    if path and (DASHBOARD_DIST / path).is_file():
        return send_file(DASHBOARD_DIST / path)
    return send_file(DASHBOARD_DIST / "index.html")


# --- Juicebox Frontend (served from project root) ---

@app.route("/app")
def juicebox_dashboard():
    """Serve the ExcelTech-specific frontend."""
    if not is_logged_in():
        return redirect(url_for("login"))
    return send_file(FRONTEND_DIR / "frontend-exceltech" / "index.html")


@app.route("/frontend-saas/<path:path>")
def saas_frontend(path="index.html"):
    """Serve the general SaaS frontend."""
    filepath = FRONTEND_DIR / "frontend-saas" / path
    if filepath.is_file():
        return send_file(filepath)
    return send_file(FRONTEND_DIR / "frontend-saas" / "index.html")


@app.route("/api/session")
def api_session():
    """Return current user session info for the frontend."""
    if not is_logged_in():
        return jsonify({"logged_in": False}), 401
    return jsonify({
        "logged_in": True,
        "name": session.get("recruiter_name", ""),
        "email": session.get("recruiter_email", ""),
        "role": session.get("recruiter_role", "recruiter"),
    })


@app.route("/api/candidates", methods=["GET"])
def api_candidates():
    """Fetch candidates + pipeline summary via ai_core."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    market = request.args.get("market") or None
    try:
        pipeline_data = ai_core.pipeline_summary(market)
    except ai_core.CoreError as e:
        return jsonify({"candidates": [], "error": e.message}), e.status
    return jsonify({
        "candidates": pipeline_data.get("candidates", []),
        "pipeline": pipeline_data.get("pipeline", []),
    })


@app.route("/api/search", methods=["POST"])
def api_search():
    """Natural language search parsing via ai_core."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    return _ai_core_call(ai_core.parse_search, request.get_json(silent=True))


@app.route("/api/search/run", methods=["POST"])
def api_search_run():
    """Unified search (natural / jd / manual) — parse + fetch + rank candidates."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    market = request.args.get("market") or None
    return _ai_core_call(ai_core.run_search, request.get_json(silent=True), market)


@app.route("/api/apollo/credits", methods=["GET"])
def api_apollo_credits():
    """Return remaining Apollo credit counters (cached 5 min)."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    return _ai_core_call(ai_core.get_apollo_credits)


@app.route("/api/candidates/<cid>/reveal", methods=["POST"])
def api_candidate_reveal(cid):
    """Reveal name, email, or phone for a candidate via Apollo /people/match."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    body = request.get_json(silent=True) or {}
    field = body.get("field")
    if field not in ("name", "email", "phone"):
        return jsonify({"error": "field must be name | email | phone"}), 422
    return _ai_core_call(ai_core.reveal_candidate_field, cid, field)


@app.route("/api/candidates/<cid>/reveal/status", methods=["GET"])
def api_candidate_reveal_status(cid):
    """Poll the latest phone-reveal status for a candidate."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    return _ai_core_call(ai_core.get_phone_reveal_status, cid)


@app.route("/api/apollo/phone-webhook", methods=["POST"])
def api_apollo_phone_webhook():
    """Apollo posts the revealed phone number here. Token-signed, no auth."""
    from ai_agents import webhook_signing
    request_id = request.args.get("request_id", "")
    candidate_id = request.args.get("candidate_id", "")
    sig = request.args.get("sig", "")
    if not webhook_signing.verify_phone_reveal(request_id, candidate_id, sig):
        return jsonify({"error": "invalid signature"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        result = ai_core.handle_phone_webhook(request_id, candidate_id, payload)
        return jsonify(result), 200
    except Exception as e:
        # Always return 200 so Apollo doesn't retry on our bugs; log loudly.
        import logging
        logging.getLogger(__name__).exception("phone webhook handler failed")
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/candidates/<cid>/detail", methods=["GET"])
def api_candidate_detail_view(cid):
    """Return full candidate row + lazy-loaded company enrichment."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.get_candidate_detail, cid, role, email)


@app.route("/api/candidates/<cid>/dnc", methods=["POST"])
def api_candidate_dnc(cid):
    """Toggle do_not_call on a candidate."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    body = request.get_json(silent=True) or {}
    value = bool(body.get("value", True))
    try:
        row = ai_core.db.update_candidate(cid, {"do_not_call": value})
        return jsonify({"ok": True, "do_not_call": row.get("do_not_call")}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/outreach/emails", methods=["POST"])
def api_outreach_emails():
    """Fetch and classify inbox emails via ai_core."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    return _ai_core_call(ai_core.process_inbox, request.get_json(silent=True))


@app.route("/api/outreach/suggest", methods=["POST"])
def api_outreach_suggest():
    """Get AI-drafted reply suggestion via ai_core."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.prepare_outreach,
                         request.get_json(silent=True), role, email)


@app.route("/api/outreach/send", methods=["POST"])
def api_outreach_send():
    """Send outreach email via ai_core → Graph API."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.send_outreach,
                         request.get_json(silent=True), role, email)


@app.route("/home")
def home():
    """Landing page with links to both SaaS and ExcelTech frontends."""
    return send_file(FRONTEND_DIR / "index.html")


@app.route("/", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for("juicebox_dashboard"))
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = RECRUITER_LOGINS.get(username)
        if user and check_password_hash(user["password"], password):
            session["logged_in"] = True
            session["version"] = SESSION_VERSION
            session["recruiter_name"] = user["name"]
            session["recruiter_email"] = user["email"]
            session["recruiter_role"] = user.get("role", "recruiter")
            session.permanent = False
            return redirect(url_for("juicebox_dashboard"))
        error = "Invalid username or password"
    return LOGIN_HTML.replace("{{ERROR}}", error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Dashboard ---

@app.route("/dashboard")
def dashboard():
    if not is_logged_in():
        return redirect(url_for("login"))
    if JD_FILE.exists():
        JD_FILE.unlink()
    jd_exists = JD_FILE.exists() and JD_FILE.stat().st_size > 0
    jd_name = "jd.txt" if jd_exists else ""
    sheet_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit" if GOOGLE_SHEET_ID else "#"
    screened_url = f"https://docs.google.com/spreadsheets/d/{SCREENED_SHEET_ID}/edit" if SCREENED_SHEET_ID else ""
    recruiters = os.environ.get("RECRUITERS", "")
    recruiters_list = [r.strip() for r in recruiters.split(",") if r.strip()] if recruiters else []
    # Build source recruiter options HTML
    src_recruiter_opts = ""
    for rname, remail in SOURCE_RECRUITERS.items():
        src_recruiter_opts += f'          <option value="{rname}" data-email="{remail}">{rname} ({remail})</option>\n'
    sourcing_url = f"https://docs.google.com/spreadsheets/d/{SOURCING_SHEET_ID}/edit" if SOURCING_SHEET_ID else ""
    sourcing_link = f'<a class="btn btn-outline" href="{sourcing_url}" target="_blank">Open Sourcing Tracker</a>' if sourcing_url else ""
    # Build outreach recruiter options (same recruiters as source)
    outreach_recruiter_opts = ""
    for rname, remail in SOURCE_RECRUITERS.items():
        outreach_recruiter_opts += f'              <option value="{rname}" data-email="{remail}">{rname} ({remail})</option>\n'
    logged_in_name = session.get("recruiter_name", "")
    logged_in_email = session.get("recruiter_email", "")
    user_role = session.get("recruiter_role", "recruiter")
    submissions_tab_display = "" if user_role == "tl" else "display:none;"
    return (DASHBOARD_HTML
            .replace("{{JD_EXISTS}}", "true" if jd_exists else "false")
            .replace("{{JD_NAME}}", jd_name)
            .replace("{{SHEET_URL}}", sheet_url)
            .replace("{{SCREENED_URL}}", screened_url)
            .replace("{{SOURCE_RECRUITER_OPTIONS}}", src_recruiter_opts)
            .replace("{{SOURCING_LINK}}", sourcing_link)
            .replace("{{OUTREACH_RECRUITER_OPTIONS}}", outreach_recruiter_opts)
            .replace("{{LOGGED_IN_NAME}}", logged_in_name)
            .replace("{{LOGGED_IN_EMAIL}}", logged_in_email)
            .replace("{{USER_ROLE}}", user_role)
            .replace("{{SUBMISSIONS_TAB_DISPLAY}}", submissions_tab_display))


# --- Upload ---

@app.route("/upload", methods=["POST"])
def upload():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    RESUMES_DIR.mkdir(exist_ok=True)
    resume_count = 0
    jd_uploaded = False

    # Handle resume files
    resumes = request.files.getlist("resumes")
    for f in resumes:
        if f and f.filename:
            ext = Path(f.filename).suffix.lower()
            if ext in ALLOWED_RESUME_EXT:
                f.save(RESUMES_DIR / f.filename)
                resume_count += 1

    # Handle JD file
    jd = request.files.get("jd")
    if jd and jd.filename:
        ext = Path(jd.filename).suffix.lower()
        if ext == ".txt":
            jd.save(JD_FILE)
            jd_uploaded = True
        elif ext == ".pdf":
            # Save temp, extract text, write to jd.txt
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                jd.save(tmp.name)
                text = ""
                with fitz.open(tmp.name) as doc:
                    for page in doc:
                        text += page.get_text()
                os.unlink(tmp.name)
            JD_FILE.write_text(text.strip())
            jd_uploaded = True

    return jsonify({
        "resumes_uploaded": resume_count,
        "jd_uploaded": jd_uploaded,
        "jd_name": "jd.txt" if jd_uploaded else "",
    })


@app.route("/delete-jd", methods=["POST"])
def delete_jd():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if JD_FILE.exists():
        JD_FILE.unlink()
    return jsonify({"deleted": True})


@app.route("/clear-resumes", methods=["POST"])
def clear_resumes():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if RESUMES_DIR.exists():
        for f in RESUMES_DIR.iterdir():
            if f.is_file():
                f.unlink()
    return jsonify({"cleared": True})


# --- Run agent ---

def stream_agent(screen=False, recruiter="", client_name="", manager_name="", requirement=""):
    cmd = [sys.executable, str(SCRIPT_DIR / "agent.py")]
    if screen:
        cmd.append("--screen")
    if recruiter:
        cmd.extend(["--recruiter", recruiter])
    if client_name:
        cmd.extend(["--client", client_name])
    if manager_name:
        cmd.extend(["--manager", manager_name])
    if requirement:
        cmd.extend(["--requirement", requirement])

    env = os.environ.copy()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(SCRIPT_DIR),
        env=env,
    )

    import queue
    q = queue.Queue()
    done_event = threading.Event()

    def keepalive():
        while not done_event.is_set():
            time.sleep(15)
            if not done_event.is_set():
                q.put(": keepalive\n\n")

    def read_output():
        for line in iter(process.stdout.readline, ""):
            q.put(f"data: {json.dumps({'line': line.rstrip()})}\n\n")
        process.wait()
        q.put(None)  # sentinel

    ka_thread = threading.Thread(target=keepalive, daemon=True)
    reader_thread = threading.Thread(target=read_output, daemon=True)
    ka_thread.start()
    reader_thread.start()

    output_lines = []
    while True:
        item = q.get()
        if item is None:
            break
        if not item.startswith(":"):
            # Parse the line for output_lines collection
            try:
                parsed = json.loads(item.split("data: ", 1)[1].split("\n")[0])
                if "line" in parsed:
                    output_lines.append(parsed["line"])
            except (json.JSONDecodeError, IndexError):
                pass
        yield item

    done_event.set()

    if screen:
        save_screening_results(output_lines)

    yield f"data: {json.dumps({'done': True, 'exit_code': process.returncode})}\n\n"


def save_screening_results(lines):
    """Parse terminal output to extract screening results for PDF report."""
    results = []
    in_results = False
    for line in lines:
        if "SCREENING RESULTS" in line:
            in_results = True
            continue
        if in_results and line.strip().startswith(("1.", "2.", "3.", "4.", "5.",
                                                    "6.", "7.", "8.", "9.")):
            # Parse: "  1. Name                          9/10  Label              [STATUS]"
            parts = line.strip()
            try:
                idx = parts.index(".")
                rest = parts[idx+1:].strip()
                # Find score pattern
                import re
                match = re.search(r'(\S.+?)\s+(\d+)/10\s+(.+?)\s+\[(PASS|REJECT)\]', rest)
                if match:
                    results.append({
                        "name": match.group(1).strip(),
                        "score": int(match.group(2)),
                        "label": match.group(3).strip(),
                        "status": match.group(4),
                    })
            except (ValueError, AttributeError):
                pass
        elif in_results and line.strip().startswith("     ") and results:
            # Reason line
            results[-1]["reason"] = line.strip()
        elif in_results and line.startswith("---"):
            in_results = False

    if results:
        REPORT_FILE.write_text(json.dumps(results, indent=2))


@app.route("/run")
def run_agent():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    recruiter = request.args.get("recruiter", "")
    client_name = request.args.get("client_name", "")
    manager_name = request.args.get("manager_name", "")
    requirement = request.args.get("requirement", "")
    return Response(
        stream_with_context(stream_agent(screen=False, recruiter=recruiter,
                                          client_name=client_name, manager_name=manager_name, requirement=requirement)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/run-screen")
def run_screen():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    recruiter = request.args.get("recruiter", "")
    client_name = request.args.get("client_name", "")
    manager_name = request.args.get("manager_name", "")
    requirement = request.args.get("requirement", "")
    return Response(
        stream_with_context(stream_agent(screen=True, recruiter=recruiter,
                                          client_name=client_name, manager_name=manager_name, requirement=requirement)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Screen Profile ---

@app.route("/upload-screen-profile", methods=["POST"])
def upload_screen_profile():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    SCREEN_PROFILE_DIR.mkdir(exist_ok=True)

    resume = request.files.get("resume")
    jd = request.files.get("jd")

    if not resume or not resume.filename:
        return jsonify({"error": "Resume file is required"}), 400
    if not jd or not jd.filename:
        return jsonify({"error": "JD file is required"}), 400

    # Save resume
    resume_ext = Path(resume.filename).suffix.lower()
    if resume_ext not in ALLOWED_RESUME_EXT:
        return jsonify({"error": "Resume must be PDF or DOCX"}), 400
    resume_path = SCREEN_PROFILE_DIR / f"resume{resume_ext}"
    resume.save(resume_path)

    # Save JD
    jd_ext = Path(jd.filename).suffix.lower()
    if jd_ext not in ALLOWED_JD_EXT:
        return jsonify({"error": "JD must be TXT or PDF"}), 400
    jd_path = SCREEN_PROFILE_DIR / f"jd{jd_ext}"
    jd.save(jd_path)

    return jsonify({
        "resume_path": str(resume_path),
        "jd_path": str(jd_path),
    })


def stream_screen_profile(resume_path, jd_path, recruiter):
    cmd = [
        sys.executable, str(SCRIPT_DIR / "agent.py"),
        "--screen-profile",
        "--resume-file", resume_path,
        "--jd-file", jd_path,
        "--recruiter", recruiter,
    ]
    env = os.environ.copy()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(SCRIPT_DIR),
        env=env,
    )

    import queue
    q = queue.Queue()
    done_event = threading.Event()

    def keepalive():
        while not done_event.is_set():
            time.sleep(15)
            if not done_event.is_set():
                q.put(": keepalive\n\n")

    def read_output():
        for line in iter(process.stdout.readline, ""):
            q.put(f"data: {json.dumps({'line': line.rstrip()})}\n\n")
        process.wait()
        q.put(None)

    ka_thread = threading.Thread(target=keepalive, daemon=True)
    reader_thread = threading.Thread(target=read_output, daemon=True)
    ka_thread.start()
    reader_thread.start()

    while True:
        item = q.get()
        if item is None:
            break
        yield item

    done_event.set()
    yield f"data: {json.dumps({'done': True, 'exit_code': process.returncode})}\n\n"


@app.route("/run-screen-profile")
def run_screen_profile():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    recruiter = request.args.get("recruiter", "")
    resume_path = request.args.get("resume_path", "")
    jd_path = request.args.get("jd_path", "")
    if not recruiter or not resume_path or not jd_path:
        return jsonify({"error": "Missing recruiter, resume_path, or jd_path"}), 400
    return Response(
        stream_with_context(stream_screen_profile(resume_path, jd_path, recruiter)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Report ---

@app.route("/report")
def report():
    if not is_logged_in():
        return redirect(url_for("login"))

    if not REPORT_FILE.exists():
        return "No screening results available.", 404

    results = json.loads(REPORT_FILE.read_text())

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    doc = SimpleDocTemplate(tmp.name, pagesize=A4,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Heading1"],
        fontSize=20, spaceAfter=6, textColor=colors.HexColor("#1a1a2e"),
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", parent=styles["Normal"],
        fontSize=10, spaceAfter=20, textColor=colors.grey,
    )

    elements = []
    elements.append(Paragraph("Screening Report", title_style))
    elements.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %b %Y, %I:%M %p')}",
        subtitle_style
    ))

    # Summary counts
    passed = sum(1 for r in results if r.get("status") == "PASS")
    rejected = sum(1 for r in results if r.get("status") == "REJECT")
    elements.append(Paragraph(
        f"Total: {len(results)} &nbsp;|&nbsp; Passed: {passed} &nbsp;|&nbsp; Rejected: {rejected}",
        styles["Normal"]
    ))
    elements.append(Spacer(1, 16))

    # Table
    header = ["#", "Candidate", "Score", "Label", "Status", "Reason"]
    data = [header]
    for i, r in enumerate(results, 1):
        data.append([
            str(i),
            r.get("name", ""),
            f"{r.get('score', 0)}/10",
            r.get("label", ""),
            r.get("status", ""),
            r.get("reason", ""),
        ])

    col_widths = [0.3*inch, 1.5*inch, 0.6*inch, 1.2*inch, 0.7*inch, 2.7*inch]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("ALIGN", (4, 0), (4, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(table)

    doc.build(elements)
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"screening_report_{datetime.now().strftime('%Y%m%d')}.pdf",
                     mimetype="application/pdf")


@app.route("/sheet")
def sheet():
    if not is_logged_in():
        return redirect(url_for("login"))
    if GOOGLE_SHEET_ID:
        return redirect(f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit")
    return "GOOGLE_SHEET_ID not configured.", 500



LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ExcelTech Recruitment Agent — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #f5f7fa;
    --card-bg: #ffffff;
    --text: #1a2e4a;
    --text-secondary: #5a6a7e;
    --border: #dce1e8;
    --primary: #1a2e4a;
    --accent: #0066cc;
    --accent-hover: #0052a3;
    --input-bg: #f5f7fa;
    --shadow: rgba(26, 46, 74, 0.08);
  }
  [data-theme="dark"] {
    --bg: #0d1117;
    --card-bg: #1a2e4a;
    --text: #ffffff;
    --text-secondary: #8b9dc3;
    --border: #2a4060;
    --primary: #0066cc;
    --accent: #3399ff;
    --accent-hover: #0066cc;
    --input-bg: #0d1117;
    --shadow: rgba(0, 0, 0, 0.3);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    transition: background 0.3s, color 0.3s;
  }
  .theme-toggle {
    position: fixed;
    top: 20px;
    right: 20px;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 50px;
    padding: 8px 14px;
    cursor: pointer;
    font-size: 18px;
    transition: all 0.3s;
    z-index: 100;
    box-shadow: 0 2px 8px var(--shadow);
  }
  .theme-toggle:hover { transform: scale(1.1); }
  .login-card {
    background: var(--card-bg);
    border-radius: 16px;
    padding: 48px 44px;
    width: 100%;
    max-width: 420px;
    box-shadow: 0 8px 40px var(--shadow);
    text-align: center;
    transition: background 0.3s, box-shadow 0.3s;
  }
  .login-card .logo { height: 52px; margin-bottom: 16px; }
  .login-card .tagline {
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 36px;
    font-style: italic;
  }
  .login-card h1 {
    font-size: 22px;
    font-weight: 700;
    margin-bottom: 28px;
    color: var(--text);
  }
  .login-card input[type="text"],
  .login-card input[type="password"] {
    width: 100%;
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--input-bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    font-size: 15px;
    outline: none;
    transition: border 0.2s, background 0.3s;
    margin-bottom: 12px;
  }
  .login-card input[type="text"]:focus,
  .login-card input[type="password"]:focus { border-color: var(--accent); }
  .login-card button {
    width: 100%;
    padding: 14px;
    margin-top: 16px;
    background: var(--accent);
    color: #ffffff;
    border: none;
    border-radius: 10px;
    font-family: 'Inter', sans-serif;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s, transform 0.1s;
  }
  .login-card button:hover { background: var(--accent-hover); transform: translateY(-1px); }
  .login-card button:active { transform: translateY(0); }
  .error { color: #e53e3e; font-size: 13px; margin-top: 12px; }
  .footer {
    position: fixed;
    bottom: 0;
    width: 100%;
    text-align: center;
    padding: 16px;
    font-size: 12px;
    color: var(--text-secondary);
    transition: color 0.3s;
  }
</style>
</head>
<body>
  <button class="theme-toggle" onclick="toggleTheme()" id="themeBtn" title="Toggle light/dark mode"></button>
  <div class="login-card">
    <img src="https://img1.wsimg.com/isteam/ip/9c692295-846f-4f03-8a5c-33df6bb637f4/blob-0001.png/:/rs=h:59,cg:true,m/qt=q:100/ll" alt="ExcelTech" class="logo">
    <div class="tagline">Excellence each time, in every delivery</div>
    <h1>Recruitment Agent</h1>
    <form method="POST">
      <input type="text" name="username" placeholder="Username" autofocus required>
      <input type="password" name="password" placeholder="Password" required>
      <button type="submit">Sign In</button>
    </form>
    <div class="error">{{ERROR}}</div>
  </div>
  <div class="footer">ExcelTech Computers Pte Ltd &copy; 2025 &middot; Singapore</div>
<script>
  function getTheme() { return localStorage.getItem('etTheme') || 'light'; }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    document.getElementById('themeBtn').textContent = t === 'dark' ? 'Light' : 'Dark';
    localStorage.setItem('etTheme', t);
  }
  function toggleTheme() { applyTheme(getTheme() === 'dark' ? 'light' : 'dark'); }
  applyTheme(getTheme());
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ExcelTech Recruitment Agent</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #f5f7fa;
    --card-bg: #ffffff;
    --text: #1a2e4a;
    --text-secondary: #5a6a7e;
    --border: #dce1e8;
    --border-dashed: #b0bec5;
    --primary: #1a2e4a;
    --accent: #0066cc;
    --accent-hover: #0052a3;
    --success: #00a651;
    --success-hover: #008c44;
    --danger: #e53e3e;
    --input-bg: #f5f7fa;
    --terminal-bg: #0d1117;
    --terminal-text: #c9d1d9;
    --shadow: rgba(26, 46, 74, 0.08);
    --nav-bg: #1a2e4a;
  }
  [data-theme="dark"] {
    --bg: #0d1117;
    --card-bg: #1a2e4a;
    --text: #ffffff;
    --text-secondary: #8b9dc3;
    --border: #2a4060;
    --border-dashed: #3a5070;
    --primary: #0066cc;
    --accent: #3399ff;
    --accent-hover: #0066cc;
    --success: #00c853;
    --success-hover: #00a651;
    --danger: #ff5252;
    --input-bg: #0d1117;
    --terminal-bg: #010409;
    --terminal-text: #c9d1d9;
    --shadow: rgba(0, 0, 0, 0.3);
    --nav-bg: #0a1628;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    transition: background 0.3s, color 0.3s;
  }

  /* Navbar */
  .navbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 28px;
    height: 60px;
    background: var(--nav-bg);
    border-bottom: 1px solid rgba(255,255,255,0.08);
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .navbar .left { display: flex; align-items: center; gap: 14px; }
  .navbar .logo { height: 34px; }
  .navbar .center {
    position: absolute;
    left: 50%;
    transform: translateX(-50%);
    font-size: 15px;
    font-weight: 600;
    color: #ffffff;
    letter-spacing: 0.3px;
  }
  .navbar .right { display: flex; align-items: center; gap: 16px; }
  .navbar a.nav-link {
    color: rgba(255,255,255,0.65);
    text-decoration: none;
    font-size: 13px;
    font-weight: 500;
    transition: color 0.2s;
  }
  .navbar a.nav-link:hover { color: #ffffff; }
  .theme-toggle {
    background: rgba(255,255,255,0.1);
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 50px;
    padding: 5px 10px;
    cursor: pointer;
    font-size: 16px;
    transition: all 0.3s;
    line-height: 1;
  }
  .theme-toggle:hover { background: rgba(255,255,255,0.2); transform: scale(1.05); }

  /* Container */
  .container { max-width: 960px; margin: 28px auto; padding: 0 24px; }

  /* Section titles */
  .section-title {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-secondary);
    margin-bottom: 14px;
  }

  /* Upload grid */
  .upload-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 24px;
  }
  @media (max-width: 640px) { .upload-grid { grid-template-columns: 1fr; } }
  .drop-zone {
    background: var(--card-bg);
    border: 2px dashed var(--border-dashed);
    border-radius: 14px;
    padding: 36px 24px;
    text-align: center;
    cursor: pointer;
    transition: all 0.25s;
    min-height: 180px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 12px var(--shadow);
  }
  .drop-zone:hover, .drop-zone.dragover {
    border-color: var(--accent);
    background: var(--card-bg);
    box-shadow: 0 4px 20px var(--shadow);
    transform: translateY(-2px);
  }
  .drop-zone .icon {
    width: 48px; height: 48px; margin-bottom: 14px; opacity: 0.7;
    display: flex; align-items: center; justify-content: center;
  }
  .drop-zone .icon svg { width: 40px; height: 40px; fill: var(--accent); }
  .drop-zone .label { font-size: 14px; color: var(--text-secondary); line-height: 1.6; }
  .drop-zone .label strong { color: var(--accent); font-weight: 600; }
  .drop-zone input { display: none; }
  .file-list {
    margin-top: 14px; font-size: 12px; color: var(--text-secondary);
    max-height: 100px; overflow-y: auto; width: 100%; text-align: left; padding: 0 8px;
  }
  .file-list div {
    padding: 4px 0; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 6px;
  }
  .file-list div::before { content: "\25B8"; font-size: 11px; }
  .jd-badge {
    display: inline-flex; align-items: center; gap: 10px;
    background: var(--input-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 14px; font-size: 13px; margin-top: 14px;
    color: var(--text-secondary); transition: all 0.3s;
  }
  .jd-badge .remove {
    cursor: pointer; color: var(--danger); font-weight: 600;
    font-size: 16px; line-height: 1; transition: transform 0.2s;
  }
  .jd-badge .remove:hover { transform: scale(1.2); }

  /* Buttons */
  .actions { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .btn {
    padding: 12px 26px; border: none; border-radius: 10px;
    font-family: 'Inter', sans-serif; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: all 0.2s; text-decoration: none;
    display: inline-flex; align-items: center; gap: 8px;
  }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn:active:not(:disabled) { transform: translateY(1px); }
  .btn-primary { background: var(--primary); color: #ffffff; }
  .btn-primary:hover:not(:disabled) { background: var(--accent-hover); transform: translateY(-1px); }
  [data-theme="dark"] .btn-primary { background: var(--accent); }
  [data-theme="dark"] .btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
  .btn-green { background: var(--success); color: #ffffff; }
  .btn-green:hover:not(:disabled) { background: var(--success-hover); transform: translateY(-1px); }
  .btn-outline {
    background: transparent; color: var(--accent);
    border: 1.5px solid var(--accent); font-weight: 600;
  }
  .btn-outline:hover:not(:disabled) { background: var(--accent); color: #ffffff; transform: translateY(-1px); }

  /* Status */
  .status-area {
    background: var(--terminal-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; min-height: 120px; max-height: 420px;
    overflow-y: auto; font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px; line-height: 1.8; white-space: pre-wrap; display: none;
    color: var(--terminal-text); box-shadow: inset 0 2px 8px rgba(0,0,0,0.15);
  }
  .status-area.active { display: block; }
  .status-area .line-pass { color: #00c853; }
  .status-area .line-reject { color: #ff5252; }
  .status-area .line-info { color: #8b949e; }
  .status-area .line-ok { color: #58a6ff; }
  .status-area .line-heading { color: #ffa657; font-weight: bold; }

  /* Summary bar */
  .summary-bar { display: none; gap: 12px; margin-top: 16px; flex-wrap: wrap; }
  .summary-bar.active { display: flex; }

  /* Footer */
  .footer {
    text-align: center; padding: 24px 16px; margin-top: 40px;
    font-size: 12px; color: var(--text-secondary);
    border-top: 1px solid var(--border); transition: all 0.3s;
  }

  /* Scrollbar */
  .status-area::-webkit-scrollbar { width: 6px; }
  .status-area::-webkit-scrollbar-track { background: transparent; }
  .status-area::-webkit-scrollbar-thumb { background: #3a4a5a; border-radius: 3px; }

  /* Tabs */
  .tabs {
    display: flex; gap: 0; margin-bottom: 24px;
    border-bottom: 2px solid var(--border);
  }
  .tab-btn {
    padding: 12px 28px; border: none; background: transparent;
    font-family: 'Inter', sans-serif; font-size: 14px; font-weight: 600;
    color: var(--text-secondary); cursor: pointer; transition: all 0.2s;
    border-bottom: 2px solid transparent; margin-bottom: -2px;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  /* Recruiter Hub */
  .hub-label { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-bottom: 4px; display: block; }
  .hub-select, .hub-input {
    width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 10px;
    background: var(--input-bg); color: var(--text); font-family: 'Inter', sans-serif;
    font-size: 13px; outline: none; transition: border 0.2s;
  }
  .hub-select:focus, .hub-input:focus { border-color: var(--accent); }
  .zone-clear {
    position: absolute; top: 8px; right: 8px; background: none; border: 1px solid var(--border);
    border-radius: 6px; padding: 2px 8px; font-size: 11px; color: var(--text-secondary);
    cursor: pointer; z-index: 2; transition: all 0.2s;
  }
  .zone-clear:hover { background: var(--danger); color: #fff; border-color: var(--danger); }
  .drop-zone { position: relative; }
  .hub-actions { display: flex; gap: 10px; margin: 18px 0; flex-wrap: wrap; }
  .hub-btn { font-size: 13px; font-weight: 600; padding: 10px 18px; border-radius: 10px; border: none; cursor: pointer; transition: all 0.2s; }
  .hub-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .hub-btn-crm { background: var(--accent); color: #fff; }
  .hub-btn-crm:hover:not(:disabled) { background: #0055aa; }
  .hub-btn-screen { background: #2e7d32; color: #fff; }
  .hub-btn-screen:hover:not(:disabled) { background: #1b5e20; }
  .hub-btn-profile { background: #6a1b9a; color: #fff; }
  .hub-btn-profile:hover:not(:disabled) { background: #4a148c; }
  .hub-btn-outreach { background: #e65100; color: #fff; }
  .hub-btn-outreach:hover:not(:disabled) { background: #bf360c; }
  .hub-outreach-expand {
    max-height: 0; overflow: hidden; transition: max-height 0.4s ease, padding 0.4s ease, opacity 0.3s ease;
    opacity: 0; padding: 0 16px;
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px;
  }
  .hub-outreach-expand.open {
    max-height: 200px; padding: 16px; opacity: 1; margin-bottom: 14px;
  }

  /* Popup modal */
  .hub-modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000;
    display: flex; align-items: center; justify-content: center;
  }
  .hub-modal {
    background: var(--card-bg); border-radius: 14px; padding: 28px; width: 420px; max-width: 92vw;
    box-shadow: 0 8px 32px rgba(0,0,0,0.25);
  }
  .hub-modal h3 { margin: 0 0 18px; font-size: 16px; color: var(--text); }
  .hub-modal-fields { display: flex; flex-direction: column; gap: 12px; margin-bottom: 20px; }
  .hub-modal-btns { display: flex; gap: 10px; justify-content: flex-end; }

  /* Rename & ZIP inline */
  .hub-zip-inline { display: flex; align-items: center; gap: 10px; margin: 8px 0 14px; }
  .hub-btn-zip {
    font-size: 12px; font-weight: 600; padding: 6px 14px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--input-bg); color: var(--text);
    cursor: pointer; transition: all 0.2s;
  }
  .hub-btn-zip:hover { border-color: var(--accent); color: var(--accent); }

  /* Source Candidates (Hunt) tab — reused for review table */
  .hunt-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 28px;
    max-width: 1280px; margin: 0 auto;
  }
  @media (max-width: 900px) { .hunt-grid { grid-template-columns: 1fr; } }
  .hunt-panel {
    background: var(--card-bg); border-radius: 14px;
    padding: 24px; box-shadow: 0 2px 12px var(--shadow);
    transition: background 0.3s, box-shadow 0.3s;
  }
  .hunt-form-row { display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
  .hunt-form-row > * { flex: 1; min-width: 0; }
  #tab-hub select, #tab-hub input[type="text"] {
    padding: 10px 14px; border: 1px solid var(--border); border-radius: 10px;
    background: var(--input-bg); color: var(--text); font-family: 'Inter', sans-serif;
    font-size: 13px; outline: none; transition: border 0.2s; width: 100%;
  }
  #tab-hub select:focus, #tab-hub input[type="text"]:focus { border-color: var(--accent); }
  #tab-hub label { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-bottom: 4px; display: block; }
  #tab-hub .drop-zone {
    border-radius: 12px; padding: 24px 16px; min-height: 130px;
  }
  #tab-hub .drop-zone .icon { width: 36px; height: 36px; margin-bottom: 10px; }
  #tab-hub .drop-zone .icon svg { width: 32px; height: 32px; }
  #tab-hub .drop-zone .label { font-size: 13px; line-height: 1.5; }
  #tab-hub .file-list { font-size: 11px; max-height: 80px; padding: 0 6px; }
  #tab-hub .file-list div { padding: 3px 0; gap: 4px; }
  .hunt-terminal {
    background: var(--terminal-bg); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px; min-height: 80px; max-height: 260px;
    overflow-y: auto; font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 12px; line-height: 1.7; white-space: pre-wrap;
    color: var(--terminal-text); display: none;
    box-shadow: inset 0 2px 6px rgba(0,0,0,0.15); margin-top: 14px;
  }
  .hunt-terminal.active { display: block; }
  .hunt-terminal .t-pass { color: #00c853; }
  .hunt-terminal .t-fail { color: #ff5252; }
  .hunt-terminal .t-info { color: #8b949e; }
  .hunt-terminal::-webkit-scrollbar { width: 5px; }
  .hunt-terminal::-webkit-scrollbar-track { background: transparent; }
  .hunt-terminal::-webkit-scrollbar-thumb { background: #3a4a5a; border-radius: 3px; }
  .hunt-review-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 14px; }
  .hunt-review-table th {
    background: var(--primary); color: #fff; padding: 8px 10px;
    text-align: left; font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  [data-theme="dark"] .hunt-review-table th { background: var(--accent); }
  .hunt-review-table td {
    padding: 8px 10px; border-bottom: 1px solid var(--border);
    vertical-align: middle; color: var(--text);
  }
  .hunt-review-table tr:nth-child(even) { background: var(--input-bg); }
  .hunt-review-table .score-pass { color: var(--success); font-weight: 600; }
  .hunt-review-table .score-fail { color: var(--danger); font-weight: 600; }
  .hunt-action-btn {
    border: none; border-radius: 6px; padding: 4px 10px; cursor: pointer;
    font-size: 14px; font-weight: 700; transition: all 0.15s; margin: 0 2px;
  }
  .hunt-action-btn.accept { background: #e8f5e9; color: var(--success); }
  .hunt-action-btn.accept:hover { background: var(--success); color: #fff; }
  .hunt-action-btn.accept.active { background: var(--success); color: #fff; }
  .hunt-action-btn.reject { background: #ffebee; color: var(--danger); }
  .hunt-action-btn.reject:hover { background: var(--danger); color: #fff; }
  .hunt-action-btn.reject.active { background: var(--danger); color: #fff; }
  .hunt-email-status { font-size: 11px; font-weight: 600; }
  .hunt-email-status.sent { color: var(--success); }
  .hunt-email-status.failed { color: var(--danger); }
  .hunt-empty-state { text-align: center; padding: 40px 20px; color: var(--text-secondary); font-size: 14px; }
  .hunt-review-wrap { max-height: 400px; overflow-y: auto; }
  .hunt-review-wrap::-webkit-scrollbar { width: 5px; }
  .hunt-review-wrap::-webkit-scrollbar-track { background: transparent; }
  .hunt-review-wrap::-webkit-scrollbar-thumb { background: #3a4a5a; border-radius: 3px; }
  .hunt-actions-bar { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }

  /* --- Notification bell --- */
  .notif-wrapper { position: relative; }
  .notif-bell {
    background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.15);
    border-radius: 50%; width: 34px; height: 34px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; transition: all 0.2s; position: relative;
  }
  .notif-bell:hover { background: rgba(255,255,255,0.2); }
  .notif-badge {
    position: absolute; top: -4px; right: -4px; background: var(--danger);
    color: #fff; font-size: 10px; font-weight: 700; border-radius: 50%;
    width: 18px; height: 18px; display: none; align-items: center;
    justify-content: center; line-height: 18px; text-align: center;
  }
  .notif-dropdown {
    display: none; position: absolute; top: 44px; right: 0; width: 340px;
    max-height: 400px; overflow-y: auto; background: var(--card-bg);
    border: 1px solid var(--border); border-radius: 12px;
    box-shadow: 0 8px 32px var(--shadow); z-index: 100; padding: 8px 0;
  }
  .notif-dropdown .notif-item {
    padding: 10px 16px; font-size: 12px; color: var(--text);
    border-bottom: 1px solid var(--border); line-height: 1.5;
  }
  .notif-dropdown .notif-item.unread { background: rgba(0,102,204,0.06); font-weight: 500; }
  .notif-dropdown .notif-item .notif-time { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }
  .notif-dropdown .notif-empty { padding: 20px; text-align: center; color: var(--text-secondary); font-size: 13px; }

  /* --- Requirements Board --- */
  .req-board-header {
    display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;
  }
  .req-board-header h2 { margin: 0; color: var(--text); }
  .req-market-tabs { display: flex; gap: 0; margin-bottom: 20px; }
  .req-market-tab {
    padding: 8px 20px; border: 1px solid var(--border); background: transparent;
    font-family: 'Inter', sans-serif; font-size: 13px; font-weight: 600;
    color: var(--text-secondary); cursor: pointer; transition: all 0.2s;
  }
  .req-market-tab:first-child { border-radius: 8px 0 0 8px; }
  .req-market-tab:last-child { border-radius: 0 8px 8px 0; }
  .req-market-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .req-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
  }
  @media (max-width: 640px) { .req-grid { grid-template-columns: 1fr; } }
  .req-card {
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; box-shadow: 0 2px 8px var(--shadow); transition: all 0.2s;
  }
  .req-card:hover { box-shadow: 0 4px 16px var(--shadow); transform: translateY(-1px); }
  .req-card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
  .req-card-title { font-size: 15px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
  .req-card-client { font-size: 12px; color: var(--text-secondary); }
  .req-card-market {
    font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px;
    background: var(--accent); color: #fff;
  }
  .req-card-recruiters { font-size: 11px; color: var(--text-secondary); margin: 8px 0; }
  .pipeline-bar {
    height: 8px; background: var(--border); border-radius: 4px;
    overflow: hidden; display: flex; margin: 10px 0 6px;
  }
  .pipeline-seg { height: 100%; transition: width 0.3s; }
  .pipeline-sourced { background: #64b5f6; }
  .pipeline-screened { background: #ffa726; }
  .pipeline-outreached { background: #ab47bc; }
  .pipeline-replied { background: #66bb6a; }
  .pipeline-submitted { background: #26a69a; }
  .pipeline-legend {
    display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: var(--text-secondary);
  }
  .pipeline-legend span { display: flex; align-items: center; gap: 4px; }
  .pipeline-legend .dot { width: 8px; height: 8px; border-radius: 50%; }
  .req-card-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  .req-card-actions .btn { font-size: 12px; padding: 6px 14px; }
  .req-card-date { font-size: 11px; color: var(--text-secondary); margin: 4px 0 0; }

  /* --- Toast notification --- */
  .toast-container {
    position: fixed; top: 70px; right: 20px; z-index: 2000;
    display: flex; flex-direction: column; gap: 8px;
  }
  .toast {
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 20px; min-width: 280px; max-width: 400px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.15); font-size: 13px; color: var(--text);
    animation: toastIn 0.3s ease-out; display: flex; align-items: flex-start; gap: 10px;
  }
  .toast.toast-success { border-left: 4px solid var(--success); }
  .toast.toast-error { border-left: 4px solid var(--danger); }
  .toast .toast-close {
    background: none; border: none; color: var(--text-secondary); cursor: pointer;
    font-size: 16px; line-height: 1; margin-left: auto; padding: 0;
  }
  @keyframes toastIn { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }

  /* --- Create Requirement modal --- */
  .create-req-modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000;
    display: none; align-items: center; justify-content: center;
  }
  .create-req-form {
    background: var(--card-bg); border-radius: 14px; padding: 28px;
    width: 520px; max-width: 94vw; max-height: 85vh; overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0,0,0,0.25);
  }
  .create-req-form h3 { margin: 0 0 18px; font-size: 16px; color: var(--text); }
  .create-req-form .form-group { margin-bottom: 14px; }
  .create-req-form label { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-bottom: 4px; display: block; }
  .create-req-form input, .create-req-form select, .create-req-form textarea {
    width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 10px;
    background: var(--input-bg); color: var(--text); font-family: 'Inter', sans-serif;
    font-size: 13px; outline: none; transition: border 0.2s;
  }
  .create-req-form textarea { min-height: 100px; resize: vertical; }
  .create-req-form input:focus, .create-req-form select:focus, .create-req-form textarea:focus { border-color: var(--accent); }
  .create-req-btns { display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; }

  /* --- Submission Queue --- */
  .sub-filters { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .sub-filter-btn {
    padding: 6px 14px; border: 1px solid var(--border); border-radius: 8px;
    background: transparent; color: var(--text-secondary); font-size: 12px;
    font-weight: 600; cursor: pointer; transition: all 0.2s;
  }
  .sub-filter-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .sub-group { margin-bottom: 20px; }
  .sub-group-title { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
  .sub-card {
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; margin-bottom: 10px; display: flex; justify-content: space-between;
    align-items: center; flex-wrap: wrap; gap: 12px;
  }
  .sub-card-info { flex: 1; min-width: 200px; }
  .sub-card-name { font-size: 14px; font-weight: 600; color: var(--text); }
  .sub-card-meta { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
  .sub-card-score {
    font-size: 13px; font-weight: 700; padding: 4px 10px; border-radius: 6px;
  }
  .sub-card-score.green { background: #e8f5e9; color: var(--success); }
  .sub-card-score.yellow { background: #fff8e1; color: #f57f17; }
  .sub-card-score.red { background: #ffebee; color: var(--danger); }
  .sub-card-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .sub-card-actions .btn { font-size: 12px; padding: 6px 14px; }

  /* --- LinkedIn modal --- */
  .linkedin-modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000;
    display: none; align-items: center; justify-content: center;
  }
  .linkedin-modal-content {
    background: var(--card-bg); border-radius: 14px; padding: 24px;
    width: 480px; max-width: 92vw; box-shadow: 0 8px 32px rgba(0,0,0,0.25);
  }
  .linkedin-search-str {
    background: var(--terminal-bg); color: var(--terminal-text); padding: 14px;
    border-radius: 8px; font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px; word-break: break-all; margin: 12px 0;
  }

  /* --- Outreach Tab --- */
  .outreach-grid { display: grid; grid-template-columns: 340px 1fr; gap: 18px; min-height: 520px; }
  .outreach-panel { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; overflow-y: auto; }
  .outreach-controls { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
  .outreach-controls label { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-bottom: 2px; display: block; }
  .outreach-controls select, .outreach-controls input[type="date"] {
    width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-size: 13px;
  }
  .outreach-controls select:focus, .outreach-controls input[type="date"]:focus { border-color: var(--accent); outline: none; }
  .outreach-cat-section { margin-top: 10px; }
  .outreach-cat-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600;
    background: var(--bg); border: 1px solid var(--border); margin-bottom: 4px; user-select: none;
  }
  .outreach-cat-header:hover { background: var(--border); }
  .outreach-cat-badge {
    background: var(--accent); color: #fff; border-radius: 10px; padding: 1px 8px; font-size: 11px; font-weight: 700;
  }
  .outreach-cat-list { max-height: 0; overflow: hidden; transition: max-height 0.3s ease; }
  .outreach-cat-list.open { max-height: 600px; overflow-y: auto; }
  .outreach-email-item {
    padding: 8px 10px; border-bottom: 1px solid var(--border); cursor: pointer; font-size: 12px;
  }
  .outreach-email-item:hover { background: var(--bg); }
  .outreach-email-item.selected { background: rgba(0,102,204,0.08); border-left: 3px solid var(--accent); }
  .outreach-email-item .em-from { font-weight: 600; color: var(--text); }
  .outreach-email-item .em-subject { color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .outreach-email-item .em-time { color: var(--text-secondary); font-size: 11px; }
  .outreach-viewer-empty { text-align: center; padding: 60px 20px; color: var(--text-secondary); font-size: 14px; }
  .outreach-viewer-header { margin-bottom: 14px; font-size: 13px; line-height: 1.6; }
  .outreach-viewer-header strong { color: var(--text); }
  .outreach-viewer-body { font-size: 13px; line-height: 1.6; max-height: 260px; overflow-y: auto; padding: 12px; background: var(--bg); border-radius: 8px; border: 1px solid var(--border); }
  .outreach-reply-area { margin-top: 14px; }
  .outreach-reply-area textarea {
    width: 100%; min-height: 120px; padding: 10px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-size: 13px; font-family: inherit; resize: vertical;
  }
  .outreach-reply-area textarea:focus { border-color: var(--accent); outline: none; }
  .outreach-reply-btns { display: flex; gap: 10px; margin-top: 10px; align-items: center; }
  .outreach-status { font-size: 12px; font-weight: 600; margin-left: 8px; }
  .outreach-status.ok { color: var(--success); }
  .outreach-status.err { color: var(--danger); }
  .outreach-loading { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin-left: 6px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (max-width: 800px) { .outreach-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <div class="navbar">
    <div class="left">
      <img src="https://img1.wsimg.com/isteam/ip/9c692295-846f-4f03-8a5c-33df6bb637f4/blob-0001.png/:/rs=h:59,cg:true,m/qt=q:100/ll" alt="ExcelTech" class="logo">
    </div>
    <div class="center">Recruitment Agent</div>
    <div class="right">
      <span style="color:rgba(255,255,255,0.8);font-size:13px;margin-right:12px;">{{LOGGED_IN_NAME}}</span>
      <div class="notif-wrapper">
        <button class="notif-bell" onclick="toggleNotifications()" title="Notifications">
          &#128276;
          <span class="notif-badge" id="notifBadge"></span>
        </button>
        <div class="notif-dropdown" id="notifDropdown">
          <div id="notifList"><div class="notif-empty">No notifications</div></div>
        </div>
      </div>
      <button class="theme-toggle" onclick="toggleTheme()" id="themeBtn" title="Toggle light/dark mode"></button>
      <a href="/logout" class="nav-link">Logout</a>
    </div>
  </div>

  <div class="container">
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('hub')">Recruiter Hub</button>
      <button class="tab-btn" onclick="switchTab('outreach')">Outreach</button>
      <button class="tab-btn" onclick="switchTab('requirements')">Requirements</button>
      <button class="tab-btn" onclick="switchTab('submissions')" style="{{SUBMISSIONS_TAB_DISPLAY}}">Submissions</button>
    </div>

    <!-- Recruiter Hub tab -->
    <div class="tab-panel active" id="tab-hub">
      <!-- 1. Recruiter dropdown -->
      <div style="margin-bottom:16px">
        <label class="hub-label">Select Recruiter</label>
        <select id="hubRecruiterSelect" class="hub-select">
          <option value="">-- Select Recruiter --</option>
{{SOURCE_RECRUITER_OPTIONS}}
        </select>
      </div>

      <!-- 2. Upload zones -->
      <div class="upload-grid">
        <div class="drop-zone" id="resumeZone">
          <button class="zone-clear" id="resumeClearBtn" onclick="event.stopPropagation(); hubClearResumes()" style="display:none">&times; Clear</button>
          <div class="icon"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zM6 20V4h7v5h5v11H6zm8-6v4h-4v-4H8l4-4 4 4h-2z"/></svg></div>
          <div class="label">Drag &amp; drop <strong>resumes</strong> here<br>PDF or DOCX files</div>
          <input type="file" id="resumeInput" multiple accept=".pdf,.docx,.doc">
          <div class="file-list" id="resumeList"></div>
        </div>
        <div class="drop-zone" id="jdZone">
          <button class="zone-clear" id="jdClearBtn" onclick="event.stopPropagation(); hubClearJd()" style="display:none">&times; Clear</button>
          <div class="icon"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zM6 20V4h7v5h5v11H6zm2-5h8v2H8v-2zm0-3h8v2H8v-2zm0-3h5v2H8V9z"/></svg></div>
          <div class="label">Drag &amp; drop <strong>Job Description</strong><br>TXT or PDF file</div>
          <input type="file" id="jdInput" accept=".txt,.pdf">
          <div id="jdDisplay"></div>
        </div>
      </div>

      <!-- 3. Three action buttons + Rename ZIP -->
      <div class="hub-actions">
        <button class="btn hub-btn hub-btn-crm" id="btnAddCRM" disabled onclick="hubAction('crm')">Add to CRM</button>
        <button class="btn hub-btn hub-btn-screen" id="btnScreenCRM" disabled onclick="hubAction('screen_crm')">Screen then Add to CRM</button>
        <button class="btn hub-btn hub-btn-outreach" id="btnSendOutreach" disabled onclick="hubAction('send_outreach')">Send Outreach Email</button>
      </div>
      <div class="hub-zip-inline">
        <button class="btn hub-btn-zip" id="btnRenameZip" onclick="hubShowRenameZip()">Rename &amp; ZIP</button>
        <span id="hubZipInline" style="display:none">
          <input type="text" id="hubZipRole" class="hub-input" placeholder="Role name" style="width:180px;display:inline-block">
          <button class="btn hub-btn-zip" onclick="hubDoRenameZip()">Download</button>
        </span>
      </div>

      <!-- 4. Terminal/status output -->
      <div class="status-area" id="hubStatusArea"></div>

      <!-- 5. Results/links section -->
      <div class="summary-bar" id="hubSummaryBar">
        <a class="btn btn-outline" href="/sheet" target="_blank" id="hubBtnSheet" style="display:none">Open Google Sheet</a>
        <button class="btn btn-outline" id="hubBtnReport" style="display:none" onclick="downloadReport()">Download Screening Report</button>
        {{SOURCING_LINK}}
      </div>

      <!-- Review table (used by Screen+CRM and Send Outreach) -->
      <div id="hubReviewSection" style="display:none">
        <p class="section-title" id="hubReviewTitle">Results</p>
        <div class="hunt-review-wrap">
          <table class="hunt-review-table">
            <thead><tr><th>Name</th><th>Skill</th><th>Email</th><th>Phone</th><th>Score</th><th>Action</th><th>Status</th></tr></thead>
            <tbody id="hubReviewBody"></tbody>
          </table>
        </div>
        <div class="hunt-actions-bar" id="hubReviewActions"></div>
      </div>
    </div>

    <!-- Popup modal -->
    <div class="hub-modal-overlay" id="hubModal" style="display:none" onclick="if(event.target===this)hubCloseModal()">
      <div class="hub-modal">
        <h3 id="hubModalTitle">Details</h3>
        <div class="hub-modal-fields">
          <div>
            <label class="hub-label">Client</label>
            <select id="hubPopupClient" class="hub-select" onchange="document.getElementById('hubPopupClientOther').style.display=this.value==='Other'?'block':'none'">
              <option value="">-- Select Client --</option>
              <option>HCL</option><option>LGCNS</option><option>DXC</option><option>ELASTIC</option><option>MOE</option>
              <option value="Other">Other (type below)</option>
            </select>
            <input type="text" id="hubPopupClientOther" class="hub-input" placeholder="Type client name" style="display:none;margin-top:6px">
          </div>
          <div id="hubPopupManagerWrap">
            <label class="hub-label">Manager's Name</label>
            <input type="text" id="hubPopupManager" class="hub-input" placeholder="e.g. John Smith">
          </div>
          <div>
            <label class="hub-label">Requirement / Role</label>
            <input type="text" id="hubPopupRequirement" class="hub-input" placeholder="e.g. Java Developer">
          </div>
          <div id="hubPopupLocationWrap" style="display:none">
            <label class="hub-label">Work Location</label>
            <input type="text" id="hubPopupLocation" class="hub-input" placeholder="e.g. Singapore">
          </div>
        </div>
        <div class="hub-modal-btns">
          <button class="btn btn-outline" onclick="hubCloseModal()">Cancel</button>
          <button class="btn btn-primary" onclick="hubProceedAction()">Proceed</button>
        </div>
      </div>
    </div>

    <!-- Outreach tab -->
    <div class="tab-panel" id="tab-outreach">
      <div class="outreach-grid">
        <!-- Left panel: controls + email list -->
        <div class="outreach-panel">
          <p class="section-title">Outreach Emails</p>
          <div class="outreach-controls">
            <div>
              <label>Recruiter</label>
              <select id="outreachRecruiterSelect">
                <option value="">-- Select Recruiter --</option>
{{OUTREACH_RECRUITER_OPTIONS}}
              </select>
            </div>
            <div>
              <label>Date</label>
              <input type="date" id="outreachDate">
            </div>
            <button class="btn btn-primary" id="outreachBtnLoad" onclick="outreachLoadEmails()">Load Emails</button>
          </div>
          <div id="outreachEmailList">
            <div class="outreach-cat-section">
              <div class="outreach-cat-header" onclick="outreachToggle('requirements')">
                <span>REQUIREMENTS</span><span class="outreach-cat-badge" id="outreachCntReq">0</span>
              </div>
              <div class="outreach-cat-list" id="outreachListReq"></div>
            </div>
            <div class="outreach-cat-section">
              <div class="outreach-cat-header" onclick="outreachToggle('candidate_replies')">
                <span>CANDIDATE REPLIES</span><span class="outreach-cat-badge" id="outreachCntCand">0</span>
              </div>
              <div class="outreach-cat-list" id="outreachListCand"></div>
            </div>
            <div class="outreach-cat-section">
              <div class="outreach-cat-header" onclick="outreachToggle('action_needed')">
                <span>ACTION NEEDED</span><span class="outreach-cat-badge" id="outreachCntAction">0</span>
              </div>
              <div class="outreach-cat-list" id="outreachListAction"></div>
            </div>
            <div class="outreach-cat-section">
              <div class="outreach-cat-header" onclick="outreachToggle('fyi')">
                <span>FYI</span><span class="outreach-cat-badge" id="outreachCntFyi">0</span>
              </div>
              <div class="outreach-cat-list" id="outreachListFyi"></div>
            </div>
          </div>
        </div>
        <!-- Right panel: viewer + reply -->
        <div class="outreach-panel">
          <div id="outreachViewerEmpty" class="outreach-viewer-empty">Select an email to view its content.</div>
          <div id="outreachViewer" style="display:none">
            <div class="outreach-viewer-header">
              <div><strong>From:</strong> <span id="outreachFrom"></span></div>
              <div><strong>Subject:</strong> <span id="outreachSubject"></span></div>
              <div><strong>Time:</strong> <span id="outreachTime"></span></div>
            </div>
            <div class="outreach-viewer-body" id="outreachBody"></div>
            <div class="outreach-reply-area">
              <div class="outreach-reply-btns" style="margin-bottom:8px">
                <button class="btn btn-outline" onclick="outreachGetSuggestion()">Get AI Suggestion</button>
                <span id="outreachSuggestStatus"></span>
              </div>
              <textarea id="outreachReplyText" placeholder="Type or edit your reply here..."></textarea>
              <div class="outreach-reply-btns">
                <button class="btn btn-primary" onclick="outreachSendReply()">Send Reply</button>
                <span id="outreachSendStatus"></span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Requirements Board tab -->
    <div class="tab-panel" id="tab-requirements">
      <div class="req-board-header">
        <h2 style="font-size:18px;font-weight:600;margin:0;">Requirements Board</h2>
        <div style="display:flex;gap:10px;align-items:center;">
          <button class="btn btn-primary" onclick="reqCreateModal()" style="{{SUBMISSIONS_TAB_DISPLAY}}">Create Requirement</button>
          <button class="btn btn-outline" onclick="loadRequirements()">Refresh</button>
          <button class="btn btn-outline" id="btnShowAllReqs" onclick="loadAllRequirements()">Show All</button>
        </div>
      </div>
      <div class="req-market-tabs">
        <button class="req-market-tab active" onclick="reqSwitchMarket('all',this)">All</button>
        <button class="req-market-tab" onclick="reqSwitchMarket('IN',this)">India</button>
        <button class="req-market-tab" onclick="reqSwitchMarket('SG',this)">Singapore</button>
      </div>
      <div class="req-grid" id="reqGrid">
        <p style="color:var(--text-secondary);text-align:center;grid-column:1/-1;">Switch to this tab to load requirements.</p>
      </div>
    </div>

    <!-- Submissions Queue tab (TL only) -->
    <div class="tab-panel" id="tab-submissions" style="{{SUBMISSIONS_TAB_DISPLAY}}">
      <div class="req-board-header">
        <h2 style="font-size:18px;font-weight:600;margin:0;">Submission Queue</h2>
        <button class="btn btn-outline" onclick="loadSubmissions()">Refresh</button>
      </div>
      <div class="sub-filters">
        <button class="sub-filter-btn active" onclick="subFilter('pending',this)">Pending Review</button>
        <button class="sub-filter-btn" onclick="subFilter('approved',this)">Approved</button>
        <button class="sub-filter-btn" onclick="subFilter('sent',this)">Sent to Client</button>
      </div>
      <div id="subList">
        <p style="color:var(--text-secondary);text-align:center;">Switch to this tab to load submissions.</p>
      </div>
    </div>

  </div>

  <!-- Create Requirement Modal -->
  <div class="create-req-modal" id="createReqModal">
    <div class="create-req-form">
      <h3>Create New Requirement</h3>
      <div class="form-group">
        <label>Client Name</label>
        <input type="text" id="reqClientName" placeholder="e.g. HCL Technologies">
      </div>
      <div class="form-group">
        <label>Market</label>
        <select id="reqMarket"><option value="IN">India</option><option value="SG">Singapore</option></select>
      </div>
      <div class="form-group">
        <label>Role Title</label>
        <input type="text" id="reqRoleTitle" placeholder="e.g. ServiceNow Developer">
      </div>
      <div class="form-group">
        <label>Skills Required (comma-separated)</label>
        <input type="text" id="reqSkills" placeholder="e.g. ServiceNow, ITSM, JavaScript">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div class="form-group">
          <label>Experience (min years)</label>
          <input type="text" id="reqExpMin" placeholder="e.g. 5">
        </div>
        <div class="form-group">
          <label>Salary Budget</label>
          <input type="text" id="reqSalary" placeholder="e.g. 12-15 LPA">
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div class="form-group">
          <label>Location</label>
          <input type="text" id="reqLocation" placeholder="e.g. Bangalore">
        </div>
        <div class="form-group">
          <label>Contract Type</label>
          <select id="reqContractType"><option value="FTE">FTE</option><option value="TP">Third Party</option><option value="C2H">C2H</option><option value="Contract">Contract</option></select>
        </div>
      </div>
      <div class="form-group" id="reqTenderWrap" style="display:none;">
        <label>Tender Number (GeBIZ)</label>
        <input type="text" id="reqTender" placeholder="Tender number">
      </div>
      <div class="form-group">
        <label>JD Text (optional — AI will extract skills if provided)</label>
        <textarea id="reqJdText" placeholder="Paste the full job description here..."></textarea>
      </div>
      <div class="create-req-btns">
        <button class="btn btn-outline" onclick="reqCloseModal()">Cancel</button>
        <button class="btn btn-primary" onclick="reqSubmitCreate()">Create &amp; Source</button>
      </div>
    </div>
  </div>

  <!-- LinkedIn Search String Modal -->
  <div class="linkedin-modal" id="linkedinModal">
    <div class="linkedin-modal-content">
      <h3 style="font-size:16px;font-weight:600;margin:0 0 10px;">LinkedIn Search String</h3>
      <p style="font-size:12px;color:var(--text-secondary);margin-bottom:8px;">Copy and paste this into LinkedIn search:</p>
      <div class="linkedin-search-str" id="linkedinStr"></div>
      <div style="display:flex;gap:10px;justify-content:flex-end;">
        <button class="btn btn-primary" onclick="navigator.clipboard.writeText(document.getElementById('linkedinStr').textContent);this.textContent='Copied!'">Copy</button>
        <button class="btn btn-outline" onclick="document.getElementById('linkedinModal').style.display='none'">Close</button>
      </div>
    </div>
  </div>

  <!-- Toast container -->
  <div class="toast-container" id="toastContainer"></div>

  <!-- TL Approve + Send Modal -->
  <div class="create-req-modal" id="tlSendModal">
    <div class="create-req-form" style="width:480px;">
      <h3>Approve &amp; Send to Client</h3>
      <div class="form-group">
        <label>Client Email</label>
        <input type="text" id="tlClientEmail" placeholder="client@company.com">
      </div>
      <div class="form-group">
        <label>Email Subject</label>
        <input type="text" id="tlEmailSubject">
      </div>
      <div class="form-group">
        <label>Additional Notes (optional)</label>
        <textarea id="tlEmailNotes" style="min-height:60px;" placeholder="Any notes to include..."></textarea>
      </div>
      <input type="hidden" id="tlSubmissionId">
      <div class="create-req-btns">
        <button class="btn btn-outline" onclick="document.getElementById('tlSendModal').style.display='none'">Cancel</button>
        <button class="btn btn-green" onclick="tlApproveSend()">Approve &amp; Send</button>
      </div>
    </div>
  </div>

  <!-- TL Reject/Feedback Modal -->
  <div class="create-req-modal" id="tlRejectModal">
    <div class="create-req-form" style="width:420px;">
      <h3>Send Back to Recruiter</h3>
      <div class="form-group">
        <label>Feedback Note</label>
        <textarea id="tlRejectNote" style="min-height:80px;" placeholder="What needs to be corrected..."></textarea>
      </div>
      <input type="hidden" id="tlRejectSubId">
      <div class="create-req-btns">
        <button class="btn btn-outline" onclick="document.getElementById('tlRejectModal').style.display='none'">Cancel</button>
        <button class="btn" style="background:var(--danger);color:#fff;" onclick="tlRejectSubmission()">Send Back</button>
      </div>
    </div>
  </div>

  <div class="footer">ExcelTech Computers Pte Ltd &copy; 2025 &middot; Singapore</div>

<script>
  // Theme
  function getTheme() { return localStorage.getItem('etTheme') || 'light'; }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    document.getElementById('themeBtn').textContent = t === 'dark' ? 'Light' : 'Dark';
    localStorage.setItem('etTheme', t);
  }
  function toggleTheme() { applyTheme(getTheme() === 'dark' ? 'light' : 'dark'); }
  applyTheme(getTheme());

  let jdExists = {{JD_EXISTS}};
  let jdName = "{{JD_NAME}}";
  let screenedUrl = "{{SCREENED_URL}}";
  let resumeFiles = [];
  let jdFile = null;
  let hubRunning = false;
  let hubActiveAction = '';  // 'crm', 'screen_crm', 'send_outreach'
  let hubScreeningResults = null;
  let hubJdDetails = null;
  let hubJdPath = '';
  let hubCandidateDecisions = {};

  const USER_ROLE = '{{USER_ROLE}}';

  // Toast notification helper
  function showToast(msg, type) {
    type = type || 'success';
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML = '<div style="flex:1">' + escapeHtml(msg) + '</div>' +
      '<button class="toast-close" onclick="this.parentElement.remove()">&times;</button>';
    container.appendChild(toast);
    setTimeout(function() { if (toast.parentElement) toast.remove(); }, 5000);
  }

  // Tab switching
  function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-' + tab).classList.add('active');
    document.querySelector('.tab-btn[onclick*="\\'' + tab + '\\'"]').classList.add('active');
    if (tab === 'requirements') loadRequirements();
    if (tab === 'submissions') loadSubmissions();
  }

  // --- Resume zone ---
  const resumeZone = document.getElementById('resumeZone');
  const resumeInput = document.getElementById('resumeInput');
  resumeZone.addEventListener('click', () => { if (!hubRunning) resumeInput.click(); });
  resumeZone.addEventListener('dragover', e => { e.preventDefault(); resumeZone.classList.add('dragover'); });
  resumeZone.addEventListener('dragleave', () => resumeZone.classList.remove('dragover'));
  resumeZone.addEventListener('drop', e => { e.preventDefault(); resumeZone.classList.remove('dragover'); addResumeFiles(e.dataTransfer.files); });
  resumeInput.addEventListener('change', () => addResumeFiles(resumeInput.files));

  function addResumeFiles(files) {
    for (const f of files) {
      const ext = f.name.split('.').pop().toLowerCase();
      if (['pdf', 'docx', 'doc'].includes(ext)) resumeFiles.push(f);
    }
    document.getElementById('resumeList').innerHTML = resumeFiles.map(f => '<div>' + f.name + '</div>').join('');
    document.getElementById('resumeClearBtn').style.display = resumeFiles.length ? '' : 'none';
    hubUpdateUI();
  }

  function hubClearResumes() {
    resumeFiles = [];
    document.getElementById('resumeList').innerHTML = '';
    document.getElementById('resumeClearBtn').style.display = 'none';
    resumeInput.value = '';
    hubUpdateUI();
  }

  function hubClearJd() {
    if (jdExists) {
      fetch('/delete-jd', { method: 'POST' });
      jdExists = false;
    }
    jdFile = null;
    document.getElementById('jdDisplay').innerHTML = '';
    document.getElementById('jdClearBtn').style.display = 'none';
    jdInput.value = '';
    hubUpdateUI();
  }

  // --- JD zone ---
  const jdZone = document.getElementById('jdZone');
  const jdInput = document.getElementById('jdInput');
  jdZone.addEventListener('click', () => { if (!hubRunning && !jdExists && !jdFile) jdInput.click(); });
  jdZone.addEventListener('dragover', e => { e.preventDefault(); jdZone.classList.add('dragover'); });
  jdZone.addEventListener('dragleave', () => jdZone.classList.remove('dragover'));
  jdZone.addEventListener('drop', e => { e.preventDefault(); jdZone.classList.remove('dragover'); if (e.dataTransfer.files.length) setJdFile(e.dataTransfer.files[0]); });
  jdInput.addEventListener('change', () => { if (jdInput.files.length) setJdFile(jdInput.files[0]); });

  function setJdFile(f) {
    const ext = f.name.split('.').pop().toLowerCase();
    if (['txt', 'pdf'].includes(ext)) {
      jdFile = f; jdExists = false;
      document.getElementById('jdDisplay').innerHTML = '<div class="jd-badge">' + f.name + '</div>';
      document.getElementById('jdClearBtn').style.display = '';
      hubUpdateUI();
    }
  }

  // --- Recruiter select change ---
  document.getElementById('hubRecruiterSelect').addEventListener('change', hubUpdateUI);

  function hubUpdateUI() {
    const hasResumes = resumeFiles.length > 0;
    const hasJd = jdExists || jdFile !== null;
    const hasRecruiter = !!document.getElementById('hubRecruiterSelect').value;
    const ready = hasResumes && hasRecruiter && !hubRunning;

    document.getElementById('btnAddCRM').disabled = !ready;
    document.getElementById('btnScreenCRM').disabled = !(ready && hasJd);
    document.getElementById('btnSendOutreach').disabled = !(ready && hasJd);
    document.getElementById('btnRenameZip').disabled = !hasResumes;
  }

  if (jdExists) {
    document.getElementById('jdDisplay').innerHTML = '<div class="jd-badge">' + jdName + '</div>';
    document.getElementById('jdClearBtn').style.display = '';
  }

  // Auto-select and lock recruiter dropdown to logged-in user
  (function() {
    const loggedInName = '{{LOGGED_IN_NAME}}';
    if (loggedInName) {
      ['hubRecruiterSelect', 'outreachRecruiterSelect'].forEach(id => {
        const sel = document.getElementById(id);
        for (let i = 0; i < sel.options.length; i++) {
          if (sel.options[i].value === loggedInName) {
            sel.selectedIndex = i; sel.disabled = true; break;
          }
        }
      });
    }
  })();

  hubUpdateUI();

  function escapeHtml(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }
  function downloadReport() { window.open('/report', '_blank'); }

  function hubLog(text, cls) {
    const sa = document.getElementById('hubStatusArea');
    sa.className = 'status-area active';
    let c = cls || 'line-info';
    if (!cls) {
      if (text.includes('OK \\u2014') || text.includes('PASSED') || text.includes('Email Sent') || text.includes('Done')) c = 'line-pass';
      else if (text.includes('REJECTED') || text.includes('Error') || text.includes('Failed')) c = 'line-reject';
      else if (text.includes('===') || text.includes('---') || text.includes('SCREENING RESULTS')) c = 'line-heading';
      else if (text.includes('processed') || text.includes('added') || text.includes('Rows') || text.includes('Saved') || text.includes('uploaded') || text.includes('Uploaded')) c = 'line-ok';
    }
    sa.innerHTML += '<div class="' + c + '">' + escapeHtml(text) + '</div>';
    sa.scrollTop = sa.scrollHeight;
  }

  function hubResetOutput() {
    const sa = document.getElementById('hubStatusArea');
    sa.className = 'status-area active'; sa.innerHTML = '';
    document.getElementById('hubSummaryBar').className = 'summary-bar';
    document.getElementById('hubBtnSheet').style.display = 'none';
    document.getElementById('hubBtnReport').style.display = 'none';
    document.getElementById('hubReviewSection').style.display = 'none';
    hubScreeningResults = null; hubCandidateDecisions = {};
    document.getElementById('hubReviewBody').innerHTML = '';
    document.getElementById('hubReviewActions').innerHTML = '';
  }

  function hubGetRecruiter() {
    const sel = document.getElementById('hubRecruiterSelect');
    return { name: sel.value, email: sel.options[sel.selectedIndex]?.dataset?.email || '' };
  }

  // --- Popup modal logic ---
  function hubShowPopup(action) {
    hubActiveAction = action;
    const modal = document.getElementById('hubModal');
    const managerWrap = document.getElementById('hubPopupManagerWrap');
    const locationWrap = document.getElementById('hubPopupLocationWrap');
    // Reset fields
    document.getElementById('hubPopupClient').value = '';
    document.getElementById('hubPopupClientOther').value = '';
    document.getElementById('hubPopupClientOther').style.display = 'none';
    document.getElementById('hubPopupManager').value = '';
    document.getElementById('hubPopupRequirement').value = '';
    document.getElementById('hubPopupLocation').value = '';
    // Show/hide fields based on action
    if (action === 'send_outreach') {
      managerWrap.style.display = 'none';
      locationWrap.style.display = 'block';
      document.getElementById('hubModalTitle').textContent = 'Outreach Email Details';
    } else {
      managerWrap.style.display = 'block';
      locationWrap.style.display = 'none';
      document.getElementById('hubModalTitle').textContent = action === 'crm' ? 'Add to CRM — Details' : 'Screen & Add to CRM — Details';
    }
    modal.style.display = 'flex';
  }

  function hubCloseModal() {
    document.getElementById('hubModal').style.display = 'none';
    hubActiveAction = '';
  }

  function hubGetPopupValues() {
    let client = document.getElementById('hubPopupClient').value;
    if (client === 'Other') client = document.getElementById('hubPopupClientOther').value.trim();
    return {
      client: client,
      manager: document.getElementById('hubPopupManager').value.trim(),
      requirement: document.getElementById('hubPopupRequirement').value.trim(),
      location: document.getElementById('hubPopupLocation').value.trim(),
    };
  }

  async function hubProceedAction() {
    const vals = hubGetPopupValues();
    if (!vals.client) { alert('Please select a client'); return; }
    if (!vals.requirement) { alert('Please enter a requirement / role'); return; }
    if (hubActiveAction !== 'send_outreach' && !vals.manager) { alert('Please enter manager name'); return; }
    if (hubActiveAction === 'send_outreach' && !vals.location) { alert('Please enter work location'); return; }

    const action = hubActiveAction;  // save before closing modal
    hubCloseModal();

    if (hubRunning) return;
    hubRunning = true;
    hubResetOutput();
    hubUpdateUI();
    const recruiter = hubGetRecruiter();

    if (action === 'crm') {
      await hubRunCRM(recruiter, vals);
    } else if (action === 'screen_crm') {
      await hubRunScreenThenCRM(recruiter, vals);
    } else if (action === 'send_outreach') {
      await hubRunOutreach(recruiter, vals);
    }

    hubRunning = false;
    hubUpdateUI();
  }

  // --- Main action dispatcher (shows popup first) ---
  function hubAction(action) {
    if (hubRunning) return;
    hubShowPopup(action);
  }

  // --- Button 1: Add to CRM (no screening) ---
  async function hubRunCRM(recruiter, vals) {
    hubLog('Uploading files...');
    const formData = new FormData();
    for (const f of resumeFiles) formData.append('resumes', f);
    if (jdFile) formData.append('jd', jdFile);

    try {
      const uploadRes = await fetch('/upload', { method: 'POST', body: formData });
      const uploadData = await uploadRes.json();
      hubLog('Uploaded ' + (uploadData.resumes_uploaded || 0) + ' resume(s)' + (uploadData.jd_uploaded ? ' + JD' : ''));
      if (uploadData.jd_uploaded) { jdExists = true; }
    } catch (e) {
      hubLog('Upload failed: ' + e.message); return;
    }

    const endpoint = '/run?recruiter=' + encodeURIComponent(recruiter.name)
      + '&client_name=' + encodeURIComponent(vals.client)
      + '&manager_name=' + encodeURIComponent(vals.manager)
      + '&requirement=' + encodeURIComponent(vals.requirement);
    hubLog('Starting agent...');

    await new Promise((resolve) => {
      const evtSource = new EventSource(endpoint);
      evtSource.onmessage = function(event) {
        const data = JSON.parse(event.data);
        if (data.done) {
          evtSource.close();
          document.getElementById('hubSummaryBar').className = 'summary-bar active';
          document.getElementById('hubBtnSheet').style.display = 'inline-flex';
          resolve(); return;
        }
        if (data.line !== undefined) hubLog(data.line);
      };
      evtSource.onerror = function() { evtSource.close(); hubLog('Connection lost.'); resolve(); };
    });
  }

  // --- Button 2: Screen then Add to CRM ---
  async function hubRunScreenThenCRM(recruiter, vals) {
    hubScreeningResults = null; hubJdDetails = null; hubCandidateDecisions = {};

    hubLog('Uploading files...');
    const formData = new FormData();
    for (const f of resumeFiles) formData.append('resumes', f);
    if (jdFile) formData.append('jd', jdFile);
    formData.append('client_name', vals.client);
    formData.append('location', '');
    formData.append('recruiter', recruiter.name);

    let uploadData;
    try {
      const res = await fetch('/source/upload', { method: 'POST', body: formData });
      uploadData = await res.json();
      if (uploadData.error) { hubLog('Error: ' + uploadData.error); return; }
      hubLog('Uploaded ' + uploadData.count + ' resume(s) + JD.');
    } catch (e) {
      hubLog('Upload failed: ' + e.message); return;
    }

    hubLog('Screening candidates against JD... (this may take a moment)');
    try {
      const res = await fetch('/source/screen', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resume_paths: uploadData.resume_paths, jd_path: uploadData.jd_path }),
      });
      const screenData = await res.json();
      if (screenData.error) { hubLog('Error: ' + screenData.error); return; }
      hubScreeningResults = screenData.results;
      hubJdDetails = screenData.jd_details;
      hubJdPath = screenData.jd_path;
      for (const r of hubScreeningResults) {
        hubLog(r.name + ' \\u2014 ' + r.match_score, r.score >= 6 ? 'line-pass' : 'line-reject');
      }
      hubLog('Screening complete. ' + hubScreeningResults.length + ' candidate(s) processed.');
      hubRenderReviewTable('screen_crm', recruiter, vals);
    } catch (e) {
      hubLog('Screening failed: ' + e.message);
    }
  }

  // --- Button 3: Send Outreach Email ---
  async function hubRunOutreach(recruiter, vals) {
    hubScreeningResults = null; hubJdDetails = null; hubCandidateDecisions = {};

    hubLog('Uploading files...');
    const formData = new FormData();
    for (const f of resumeFiles) formData.append('resumes', f);
    if (jdFile) formData.append('jd', jdFile);
    formData.append('client_name', vals.client);
    formData.append('location', vals.location);
    formData.append('recruiter', recruiter.name);

    let uploadData;
    try {
      const res = await fetch('/source/upload', { method: 'POST', body: formData });
      uploadData = await res.json();
      if (uploadData.error) { hubLog('Error: ' + uploadData.error); return; }
      hubLog('Uploaded ' + uploadData.count + ' resume(s) + JD.');
    } catch (e) {
      hubLog('Upload failed: ' + e.message); return;
    }

    hubLog('Screening candidates against JD... (this may take a moment)');
    try {
      const res = await fetch('/source/screen', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resume_paths: uploadData.resume_paths, jd_path: uploadData.jd_path }),
      });
      const screenData = await res.json();
      if (screenData.error) { hubLog('Error: ' + screenData.error); return; }
      hubScreeningResults = screenData.results;
      hubJdDetails = screenData.jd_details;
      hubJdPath = screenData.jd_path;
      for (const r of hubScreeningResults) {
        hubLog(r.name + ' \\u2014 ' + r.match_score, r.score >= 6 ? 'line-pass' : 'line-reject');
      }
      hubLog('Screening complete. ' + hubScreeningResults.length + ' candidate(s) processed.');
      hubRenderReviewTable('send_outreach', recruiter, vals);
    } catch (e) {
      hubLog('Screening failed: ' + e.message);
    }
  }

  // --- Render review table with appropriate action buttons ---
  function hubRenderReviewTable(flow, recruiter, vals) {
    if (!hubScreeningResults || !hubScreeningResults.length) return;
    document.getElementById('hubReviewSection').style.display = 'block';
    const body = document.getElementById('hubReviewBody');
    body.innerHTML = '';
    hubScreeningResults.forEach((r, i) => {
      const passed = r.score >= 6;
      const scoreClass = passed ? 'score-pass' : 'score-fail';
      // Default: ✓ for passed, ✗ for failed
      hubCandidateDecisions[i] = passed ? 'accept' : 'reject';
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + escapeHtml(r.name) + '</td>' +
        '<td>' + escapeHtml(r.skillset) + '</td>' +
        '<td style="font-size:11px;word-break:break-all">' + escapeHtml(r.email) + '</td>' +
        '<td style="font-size:11px">' + escapeHtml(r.contact_no) + '</td>' +
        '<td class="' + scoreClass + '">' + escapeHtml(r.match_score) + '</td>' +
        '<td>' +
          '<button class="hunt-action-btn accept' + (passed ? ' active' : '') + '" data-idx="' + i + '" onclick="hubDecide(' + i + ',\\'accept\\')">&#10003;</button>' +
          '<button class="hunt-action-btn reject' + (!passed ? ' active' : '') + '" data-idx="' + i + '" onclick="hubDecide(' + i + ',\\'reject\\')">&#10007;</button>' +
        '</td>' +
        '<td class="hunt-email-status" id="hubStatus-' + i + '"></td>';
      body.appendChild(tr);
    });

    // Build action buttons based on flow
    const actionsDiv = document.getElementById('hubReviewActions');
    actionsDiv.innerHTML = '';

    if (flow === 'screen_crm') {
      actionsDiv.innerHTML =
        '<button class="btn hub-btn hub-btn-crm" onclick="hubAddSelectedToCRM()">Add Selected to CRM</button>' +
        '<button class="btn hub-btn hub-btn-screen" onclick="hubAddAllPassedToCRM()">Add All Passed to CRM</button>';
    } else if (flow === 'send_outreach') {
      actionsDiv.innerHTML =
        '<button class="btn hub-btn hub-btn-outreach" onclick="hubSendEmailsSelected()">Send Emails to Selected</button>' +
        '<button class="btn hub-btn hub-btn-screen" onclick="hubSendEmailsAllPassed()">Send Emails to All Passed</button>';
    }

    // Store flow context for action buttons
    window._hubFlowRecruiter = recruiter;
    window._hubFlowVals = vals;
  }

  function hubDecide(idx, action) {
    hubCandidateDecisions[idx] = action;
    document.querySelectorAll('.hunt-action-btn[data-idx="' + idx + '"]').forEach(btn => {
      btn.classList.remove('active');
      if (btn.classList.contains(action)) btn.classList.add('active');
    });
  }

  // --- Screen+CRM: Add Selected to CRM ---
  async function hubAddSelectedToCRM() {
    await hubAddToCRMByFilter('selected');
  }

  async function hubAddAllPassedToCRM() {
    await hubAddToCRMByFilter('passed');
  }

  async function hubAddToCRMByFilter(mode) {
    if (hubRunning) return;
    hubRunning = true; hubUpdateUI();
    const recruiter = window._hubFlowRecruiter;
    const vals = window._hubFlowVals;

    // Determine which candidates to add
    let indices = [];
    if (mode === 'selected') {
      indices = Object.entries(hubCandidateDecisions).filter(([_, d]) => d === 'accept').map(([i]) => parseInt(i));
    } else {
      hubScreeningResults.forEach((r, i) => { if (r.score >= 6) indices.push(i); });
    }

    if (!indices.length) { hubLog('No candidates to add.'); hubRunning = false; hubUpdateUI(); return; }

    // Get selected candidate filenames to only upload those
    const selectedResults = indices.map(i => hubScreeningResults[i]);
    const selectedFileNames = new Set(selectedResults.map(r => r.file));

    hubLog('Uploading ' + indices.length + ' candidate(s) to CRM...');

    // Clear old resumes first, then upload only selected
    try { await fetch('/clear-resumes', { method: 'POST' }); } catch(e) {}

    const formData = new FormData();
    for (const f of resumeFiles) {
      if (selectedFileNames.has(f.name)) formData.append('resumes', f);
    }
    if (jdFile) formData.append('jd', jdFile);

    try {
      const uploadRes = await fetch('/upload', { method: 'POST', body: formData });
      const uploadData = await uploadRes.json();
      hubLog('Uploaded ' + (uploadData.resumes_uploaded || 0) + ' resume(s)');
      if (uploadData.jd_uploaded) { jdExists = true; }
    } catch (e) {
      hubLog('Upload failed: ' + e.message); hubRunning = false; hubUpdateUI(); return;
    }

    const endpoint = '/run-screen?recruiter=' + encodeURIComponent(recruiter.name)
      + '&client_name=' + encodeURIComponent(vals.client)
      + '&manager_name=' + encodeURIComponent(vals.manager)
      + '&requirement=' + encodeURIComponent(vals.requirement);
    hubLog('Adding to CRM...');

    await new Promise((resolve) => {
      const evtSource = new EventSource(endpoint);
      evtSource.onmessage = function(event) {
        const data = JSON.parse(event.data);
        if (data.done) {
          evtSource.close();
          document.getElementById('hubSummaryBar').className = 'summary-bar active';
          document.getElementById('hubBtnSheet').style.display = 'inline-flex';
          document.getElementById('hubBtnReport').style.display = 'inline-flex';
          resolve(); return;
        }
        if (data.line !== undefined) hubLog(data.line);
      };
      evtSource.onerror = function() { evtSource.close(); hubLog('Connection lost.'); resolve(); };
    });

    hubRunning = false; hubUpdateUI();
  }

  // --- Send Outreach: Send Emails to Selected / All Passed ---
  async function hubSendEmailsSelected() {
    await hubSendEmailsByFilter('selected');
  }
  async function hubSendEmailsAllPassed() {
    await hubSendEmailsByFilter('passed');
  }

  async function hubSendEmailsByFilter(mode) {
    if (hubRunning) return;
    hubRunning = true; hubUpdateUI();
    const recruiter = window._hubFlowRecruiter;
    const vals = window._hubFlowVals;

    let candidates = [];
    let candidateIndices = [];
    if (mode === 'selected') {
      Object.entries(hubCandidateDecisions).forEach(([idx, d]) => {
        if (d === 'accept') { candidates.push(hubScreeningResults[parseInt(idx)]); candidateIndices.push(parseInt(idx)); }
      });
    } else {
      hubScreeningResults.forEach((r, i) => {
        if (r.score >= 6) { candidates.push(r); candidateIndices.push(i); }
      });
    }

    if (!candidates.length) { hubLog('No candidates selected.'); hubRunning = false; hubUpdateUI(); return; }
    hubLog('Sending emails to ' + candidates.length + ' candidate(s)...');

    try {
      const res = await fetch('/source/send-emails', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          candidates: candidates, recruiter_name: recruiter.name,
          recruiter_email: recruiter.email,
          client_name: vals.client, location: vals.location,
          requirement: vals.requirement,
          jd_details: hubJdDetails, jd_path: hubJdPath,
        }),
      });
      const data = await res.json();
      if (data.error) { hubLog('Error: ' + data.error); hubRunning = false; hubUpdateUI(); return; }
      data.statuses.forEach((s, si) => {
        const idx = candidateIndices[si];
        const el = document.getElementById('hubStatus-' + idx);
        if (el) {
          if (s.status === 'Email Sent') {
            el.className = 'hunt-email-status sent'; el.textContent = 'Email Sent';
            hubLog(s.name + ' \\u2014 Email Sent', 'line-pass');
          } else {
            el.className = 'hunt-email-status failed'; el.textContent = 'Failed';
            hubLog(s.name + ' \\u2014 Failed: ' + (s.reason || ''), 'line-reject');
          }
        }
      });
      hubLog('Done.');
      document.getElementById('hubSummaryBar').className = 'summary-bar active';
    } catch (e) {
      hubLog('Send failed: ' + e.message);
    }
    hubRunning = false; hubUpdateUI();
  }

  // --- Download Renamed ZIP ---
  async function hubDownloadZip() {
    if (!hubScreeningResults) return;
    const vals = window._hubFlowVals;
    const role = vals ? vals.requirement : 'Resumes';
    const passedFiles = [];
    hubScreeningResults.forEach((r, i) => {
      if (r.score >= 6 && r.resume_path) {
        passedFiles.push({ name: r.name, path: r.resume_path });
      }
    });
    if (!passedFiles.length) { alert('No passed candidates to zip.'); return; }
    try {
      const res = await fetch('/download-zip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: role, files: passedFiles }),
      });
      if (!res.ok) { alert('ZIP download failed'); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url;
      a.download = 'Requirement.zip';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { alert('ZIP download failed: ' + e.message); }
  }

  // --- Rename & ZIP (standalone, near upload zone) ---
  function hubShowRenameZip() {
    const inline = document.getElementById('hubZipInline');
    inline.style.display = inline.style.display === 'none' ? 'inline' : 'none';
    document.getElementById('hubZipRole').value = '';
  }

  async function hubDoRenameZip() {
    const role = document.getElementById('hubZipRole').value.trim();
    if (!role) { alert('Enter a role name'); return; }
    if (!resumeFiles.length) { alert('No resumes uploaded'); return; }

    hubLog('Creating ZIP with ' + resumeFiles.length + ' file(s)...');
    const formData = new FormData();
    for (const f of resumeFiles) formData.append('resumes', f);

    try {
      const res = await fetch('/rename-zip?role=' + encodeURIComponent(role), {
        method: 'POST', body: formData,
      });
      if (!res.ok) { hubLog('ZIP download failed'); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url;
      a.download = 'Requirement.zip';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      hubLog('ZIP downloaded: Requirement.zip', 'line-pass');
    } catch (e) { hubLog('ZIP failed: ' + e.message); }
  }

  // --- Outreach Tab ---
  let outreachEmails = [];
  let outreachSelected = null;

  // Default date to today
  (function() {
    const d = new Date();
    const iso = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
    document.getElementById('outreachDate').value = iso;
  })();

  function outreachToggle(cat) {
    const map = {requirements:'outreachListReq', candidate_replies:'outreachListCand', action_needed:'outreachListAction', fyi:'outreachListFyi'};
    const el = document.getElementById(map[cat]);
    if (el) el.classList.toggle('open');
  }

  async function outreachLoadEmails() {
    const sel = document.getElementById('outreachRecruiterSelect');
    const recruiterEmail = sel.options[sel.selectedIndex]?.dataset?.email;
    const dateVal = document.getElementById('outreachDate').value;
    if (!recruiterEmail || !dateVal) { alert('Select a recruiter and date'); return; }

    const btn = document.getElementById('outreachBtnLoad');
    btn.disabled = true; btn.textContent = 'Loading...';

    try {
      const res = await fetch('/outreach/emails', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({recruiter_email: recruiterEmail, date: dateVal})
      });
      const data = await res.json();
      if (data.error) { alert(data.error); return; }

      outreachEmails = data.emails;
      const counts = data.counts;

      document.getElementById('outreachCntReq').textContent = counts.requirements || 0;
      document.getElementById('outreachCntCand').textContent = counts.candidate_replies || 0;
      document.getElementById('outreachCntAction').textContent = counts.action_needed || 0;
      document.getElementById('outreachCntFyi').textContent = counts.fyi || 0;

      const lists = {requirements:'outreachListReq', candidate_replies:'outreachListCand', action_needed:'outreachListAction', fyi:'outreachListFyi'};
      Object.values(lists).forEach(id => { document.getElementById(id).innerHTML = ''; });

      outreachEmails.forEach((em, idx) => {
        const listId = lists[em.category];
        if (!listId) return;
        const div = document.createElement('div');
        div.className = 'outreach-email-item';
        div.dataset.idx = idx;
        const t = em.time ? new Date(em.time).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
        div.innerHTML = '<div class="em-from">' + escapeHtml(em.from_name) + '</div>'
          + '<div class="em-subject">' + escapeHtml(em.subject) + '</div>'
          + '<div class="em-time">' + t + '</div>';
        div.onclick = () => outreachSelectEmail(idx);
        document.getElementById(listId).appendChild(div);
      });

      // Auto-open categories that have emails
      Object.entries(lists).forEach(([cat, id]) => {
        const el = document.getElementById(id);
        if (counts[cat] > 0) el.classList.add('open'); else el.classList.remove('open');
      });

      // Clear viewer
      outreachSelected = null;
      document.getElementById('outreachViewerEmpty').style.display = '';
      document.getElementById('outreachViewer').style.display = 'none';

    } catch (e) {
      alert('Failed to load emails: ' + e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Load Emails';
    }
  }

  function outreachSelectEmail(idx) {
    outreachSelected = outreachEmails[idx];
    document.querySelectorAll('.outreach-email-item').forEach(el => el.classList.remove('selected'));
    document.querySelector('.outreach-email-item[data-idx="' + idx + '"]')?.classList.add('selected');

    document.getElementById('outreachViewerEmpty').style.display = 'none';
    document.getElementById('outreachViewer').style.display = '';
    document.getElementById('outreachFrom').textContent = outreachSelected.from_name + ' <' + outreachSelected.from_email + '>';
    document.getElementById('outreachSubject').textContent = outreachSelected.subject;
    document.getElementById('outreachTime').textContent = outreachSelected.time ? new Date(outreachSelected.time).toLocaleString() : '';
    document.getElementById('outreachBody').innerHTML = outreachSelected.body || escapeHtml(outreachSelected.preview);
    document.getElementById('outreachReplyText').value = '';
    document.getElementById('outreachSuggestStatus').innerHTML = '';
    document.getElementById('outreachSendStatus').innerHTML = '';
  }

  async function outreachGetSuggestion() {
    if (!outreachSelected) return;
    const sel = document.getElementById('outreachRecruiterSelect');
    const recruiterEmail = sel.options[sel.selectedIndex]?.dataset?.email;
    const recruiterName = sel.options[sel.selectedIndex]?.value?.split(' (')[0] || '';
    const statusEl = document.getElementById('outreachSuggestStatus');
    statusEl.innerHTML = '<span class="outreach-loading"></span> Generating...';

    try {
      const res = await fetch('/outreach/suggest', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          recruiter_email: recruiterEmail,
          email_id: outreachSelected.id,
          conversation_id: outreachSelected.conversation_id,
          recruiter_name: recruiterName
        })
      });
      const data = await res.json();
      if (data.error) { statusEl.innerHTML = '<span class="outreach-status err">' + escapeHtml(data.error) + '</span>'; return; }
      document.getElementById('outreachReplyText').value = data.suggestion;
      statusEl.innerHTML = '<span class="outreach-status ok">Done</span>';
    } catch (e) {
      statusEl.innerHTML = '<span class="outreach-status err">Failed: ' + escapeHtml(e.message) + '</span>';
    }
  }

  async function outreachSendReply() {
    if (!outreachSelected) return;
    const replyBody = document.getElementById('outreachReplyText').value.trim();
    if (!replyBody) { alert('Write a reply first'); return; }
    const sel = document.getElementById('outreachRecruiterSelect');
    const recruiterEmail = sel.options[sel.selectedIndex]?.dataset?.email;
    const statusEl = document.getElementById('outreachSendStatus');
    statusEl.innerHTML = '<span class="outreach-loading"></span> Sending...';

    try {
      const res = await fetch('/outreach/send', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          recruiter_email: recruiterEmail,
          email_id: outreachSelected.id,
          reply_body: replyBody
        })
      });
      const data = await res.json();
      if (data.error) { statusEl.innerHTML = '<span class="outreach-status err">' + escapeHtml(data.error) + '</span>'; return; }
      statusEl.innerHTML = '<span class="outreach-status ok">Sent!</span>';
      document.getElementById('outreachReplyText').value = '';
    } catch (e) {
      statusEl.innerHTML = '<span class="outreach-status err">Failed: ' + escapeHtml(e.message) + '</span>';
    }
  }

  // =====================================================
  // NOTIFICATIONS
  // =====================================================
  function toggleNotifications() {
    const dd = document.getElementById('notifDropdown');
    dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
  }
  document.addEventListener('click', e => {
    const w = document.querySelector('.notif-wrapper');
    if (w && !w.contains(e.target)) document.getElementById('notifDropdown').style.display = 'none';
  });

  async function loadNotifications() {
    try {
      const res = await fetch('/api/notifications');
      const data = await res.json();
      const notifs = data.notifications || [];
      const badge = document.getElementById('notifBadge');
      const list = document.getElementById('notifList');
      const unread = notifs.filter(n => !n.read).length;
      badge.style.display = unread > 0 ? 'flex' : 'none';
      badge.textContent = unread;
      if (!notifs.length) {
        list.innerHTML = '<div class="notif-empty">No notifications</div>';
        return;
      }
      list.innerHTML = notifs.slice(0, 20).map(n =>
        '<div class="notif-item' + (n.read ? '' : ' unread') + '">' +
        escapeHtml(n.message) +
        '<div class="notif-time">' + (n.time ? new Date(n.time).toLocaleString() : '') + '</div>' +
        '</div>'
      ).join('');
    } catch(e) {}
  }
  loadNotifications();
  setInterval(loadNotifications, 60000);

  // =====================================================
  // REQUIREMENTS BOARD
  // =====================================================
  let reqCurrentMarket = 'all';
  let reqAllData = [];

  function reqSwitchMarket(market, btn) {
    reqCurrentMarket = market;
    document.querySelectorAll('.req-market-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderRequirements();
  }

  function loadAllRequirements() {
    loadRequirements(true);
    document.getElementById('btnShowAllReqs').style.display = 'none';
  }

  async function loadRequirements(showAll) {
    const grid = document.getElementById('reqGrid');
    grid.innerHTML = '<p style="color:var(--text-secondary);text-align:center;grid-column:1/-1;">Loading...</p>';
    try {
      let url = '/api/requirements?status=open';
      if (showAll) url += '&created_after=2000-01-01T00:00:00';
      const res = await fetch(url);
      const data = await res.json();
      reqAllData = data.requirements || [];
      // Also load pipeline data
      try {
        const pRes = await fetch('/api/pipeline');
        const pData = await pRes.json();
        const pipeMap = {};
        (pData.pipeline || []).forEach(p => { pipeMap[p.requirement_id] = p; });
        reqAllData.forEach(r => { r._pipeline = pipeMap[r.id] || {}; });
      } catch(e) {}
      renderRequirements();
    } catch(e) {
      grid.innerHTML = '<p style="color:var(--danger);text-align:center;grid-column:1/-1;">Failed to load requirements</p>';
    }
  }

  function renderRequirements() {
    const grid = document.getElementById('reqGrid');
    let reqs = reqAllData;
    if (reqCurrentMarket !== 'all') reqs = reqs.filter(r => r.market === reqCurrentMarket);
    if (!reqs.length) {
      grid.innerHTML = '<p style="color:var(--text-secondary);text-align:center;grid-column:1/-1;">No requirements found.</p>';
      return;
    }
    grid.innerHTML = reqs.map(r => {
      const p = r._pipeline || {};
      const total = Math.max(p.sourced || 1, 1);
      const recruiters = (r.assigned_recruiters || []).join(', ') || 'Unassigned';
      const createdDate = r.created_at ? new Date(r.created_at).toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'numeric'}) : '';
      return '<div class="req-card">' +
        '<div class="req-card-header">' +
          '<div><div class="req-card-title">' + escapeHtml(r.role_title || '') + '</div>' +
          '<div class="req-card-client">' + escapeHtml(r.client_name || '') + '</div></div>' +
          '<span class="req-card-market">' + escapeHtml(r.market || '') + '</span>' +
        '</div>' +
        '<div class="req-card-date">Created: ' + createdDate + '</div>' +
        '<div class="req-card-recruiters">Recruiters: ' + escapeHtml(recruiters) + '</div>' +
        '<div class="pipeline-bar">' +
          '<div class="pipeline-seg pipeline-sourced" style="width:' + Math.min(100, ((p.sourced||0)/total)*100) + '%"></div>' +
          '<div class="pipeline-seg pipeline-screened" style="width:' + Math.min(100, ((p.screened||0)/total)*100) + '%"></div>' +
          '<div class="pipeline-seg pipeline-outreached" style="width:' + Math.min(100, ((p.outreached||0)/total)*100) + '%"></div>' +
          '<div class="pipeline-seg pipeline-replied" style="width:' + Math.min(100, ((p.replied||0)/total)*100) + '%"></div>' +
          '<div class="pipeline-seg pipeline-submitted" style="width:' + Math.min(100, ((p.sent_to_client||0)/total)*100) + '%"></div>' +
        '</div>' +
        '<div class="pipeline-legend">' +
          '<span><span class="dot" style="background:#64b5f6;"></span> Sourced: ' + (p.sourced||0) + '</span>' +
          '<span><span class="dot" style="background:#ffa726;"></span> Screened: ' + (p.screened||0) + '</span>' +
          '<span><span class="dot" style="background:#ab47bc;"></span> Outreached: ' + (p.outreached||0) + '</span>' +
          '<span><span class="dot" style="background:#66bb6a;"></span> Replied: ' + (p.replied||0) + '</span>' +
          '<span><span class="dot" style="background:#26a69a;"></span> Submitted: ' + (p.sent_to_client||0) + '</span>' +
        '</div>' +
        '<div class="req-card-actions">' +
          '<button class="btn btn-primary" onclick="reqSourceNow(\\'' + r.id + '\\')">Source Now</button>' +
          '<button class="btn btn-outline" onclick="reqShowLinkedin(\\'' + r.id + '\\')">LinkedIn String</button>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  async function reqSourceNow(reqId) {
    const btn = event.target;
    btn.disabled = true; btn.textContent = 'Sourcing...';
    try {
      const res = await fetch('/api/requirements/' + reqId + '/source', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'X-User-Role': USER_ROLE, 'X-User-Email': '{{LOGGED_IN_EMAIL}}'}
      });
      const data = await res.json();
      if (data.error) { showToast('Error: ' + data.error, 'error'); }
      else {
        const msg = data.message || ('Sourced: ' + (data.sourced||0));
        showToast(msg);
        if (data.linkedin_search_string) {
          document.getElementById('linkedinStr').textContent = data.linkedin_search_string;
          document.getElementById('linkedinModal').style.display = 'flex';
        }
      }
      loadRequirements();
    } catch(e) { showToast('Failed: ' + e.message, 'error'); }
    btn.disabled = false; btn.textContent = 'Source Now';
  }

  async function reqShowLinkedin(reqId) {
    try {
      const res = await fetch('/api/requirements/' + reqId + '/linkedin');
      const data = await res.json();
      document.getElementById('linkedinStr').textContent = data.linkedin_search_string || 'No search string available';
      document.getElementById('linkedinModal').style.display = 'flex';
    } catch(e) { alert('Failed to get LinkedIn string'); }
  }

  // Create Requirement
  function reqCreateModal() {
    document.getElementById('createReqModal').style.display = 'flex';
    document.getElementById('reqMarket').addEventListener('change', function() {
      document.getElementById('reqTenderWrap').style.display = this.value === 'SG' ? 'block' : 'none';
    });
  }
  function reqCloseModal() { document.getElementById('createReqModal').style.display = 'none'; }

  async function reqSubmitCreate() {
    const body = {
      client_name: document.getElementById('reqClientName').value.trim(),
      market: document.getElementById('reqMarket').value,
      role_title: document.getElementById('reqRoleTitle').value.trim(),
      skills_required: document.getElementById('reqSkills').value.split(',').map(s => s.trim()).filter(Boolean),
      experience_min: document.getElementById('reqExpMin').value.trim() || null,
      salary_budget: document.getElementById('reqSalary').value.trim() || null,
      location: document.getElementById('reqLocation').value.trim() || null,
      contract_type: document.getElementById('reqContractType').value,
      tender_number: document.getElementById('reqTender').value.trim() || null,
      jd_text: document.getElementById('reqJdText').value.trim() || null,
    };
    if (!body.client_name || !body.role_title) { alert('Client name and role title are required'); return; }

    try {
      const res = await fetch('/api/requirements/create', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'X-User-Role': USER_ROLE, 'X-User-Email': '{{LOGGED_IN_EMAIL}}'},
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (data.error) { showToast('Error: ' + data.error, 'error'); return; }
      showToast('Requirement created! Sourcing started in background.');
      reqCloseModal();
      loadRequirements();
    } catch(e) { showToast('Failed: ' + e.message, 'error'); }
  }

  // =====================================================
  // SUBMISSION QUEUE (TL only)
  // =====================================================
  let subAllData = [];
  let subCurrentFilter = 'pending';

  function subFilter(filter, btn) {
    subCurrentFilter = filter;
    document.querySelectorAll('.sub-filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderSubmissions();
  }

  async function loadSubmissions() {
    const list = document.getElementById('subList');
    list.innerHTML = '<p style="color:var(--text-secondary);text-align:center;">Loading...</p>';
    try {
      const res = await fetch('/api/tl/queue');
      const data = await res.json();
      subAllData = data.queue || [];
      renderSubmissions();
    } catch(e) {
      list.innerHTML = '<p style="color:var(--danger);text-align:center;">Failed to load submissions</p>';
    }
  }

  function renderSubmissions() {
    const list = document.getElementById('subList');
    let subs = subAllData;
    if (subCurrentFilter === 'pending') subs = subs.filter(s => !s.tl_approved);
    else if (subCurrentFilter === 'approved') subs = subs.filter(s => s.tl_approved && !s.sent_to_client_at);
    else if (subCurrentFilter === 'sent') subs = subs.filter(s => s.sent_to_client_at);

    if (!subs.length) {
      list.innerHTML = '<p style="color:var(--text-secondary);text-align:center;">No submissions in this category.</p>';
      return;
    }

    // Group by requirement
    const grouped = {};
    subs.forEach(s => {
      const key = s.requirement_id || 'unknown';
      if (!grouped[key]) grouped[key] = { client: s.client_name, items: [] };
      grouped[key].items.push(s);
    });

    let html = '';
    Object.entries(grouped).forEach(([reqId, group]) => {
      html += '<div class="sub-group">';
      html += '<div class="sub-group-title">' + escapeHtml(group.client || 'Unknown Client') + '</div>';
      group.items.forEach(s => {
        const det = s.candidate_details || {};
        const scr = s.screening || {};
        const score = scr.score || 0;
        const scoreClass = score >= 7 ? 'green' : score >= 5 ? 'yellow' : 'red';
        const candName = det.full_name || 'Unknown';
        html += '<div class="sub-card">' +
          '<div class="sub-card-info">' +
            '<div class="sub-card-name">' + escapeHtml(candName) + '</div>' +
            '<div class="sub-card-meta">By ' + escapeHtml(s.submitted_by_recruiter || '') +
              ' &middot; ' + (s.submitted_at ? new Date(s.submitted_at).toLocaleDateString() : '') + '</div>' +
            (scr.reasoning ? '<div class="sub-card-meta" style="margin-top:4px;font-style:italic;">' + escapeHtml(scr.reasoning) + '</div>' : '') +
          '</div>' +
          '<span class="sub-card-score ' + scoreClass + '">' + score + '/10 &middot; ' + escapeHtml(scr.recommendation || '') + '</span>' +
          '<div class="sub-card-actions">' +
            (s.formatted_doc_path ? '<a class="btn btn-outline" href="/api/download-doc?path=' + encodeURIComponent(s.formatted_doc_path) + '" target="_blank">Download</a>' : '') +
            (!s.tl_approved ? '<button class="btn btn-green" onclick="tlShowApprove(\\'' + s.id + '\\',\\'' + escapeHtml(candName) + '\\')">Approve &amp; Send</button>' : '') +
            (!s.tl_approved ? '<button class="btn" style="background:var(--danger);color:#fff;font-size:12px;padding:6px 14px;" onclick="tlShowReject(\\'' + s.id + '\\')">Send Back</button>' : '') +
          '</div>' +
        '</div>';
      });
      html += '</div>';
    });
    list.innerHTML = html;
  }

  function tlShowApprove(subId, candName) {
    document.getElementById('tlSubmissionId').value = subId;
    document.getElementById('tlEmailSubject').value = 'Candidate Profile: ' + candName + ' | ExcelTech Computers';
    document.getElementById('tlClientEmail').value = '';
    document.getElementById('tlEmailNotes').value = '';
    document.getElementById('tlSendModal').style.display = 'flex';
  }

  async function tlApproveSend() {
    const body = {
      submission_id: document.getElementById('tlSubmissionId').value,
      tl_email: '{{LOGGED_IN_EMAIL}}',
      client_email: document.getElementById('tlClientEmail').value.trim(),
      email_subject: document.getElementById('tlEmailSubject').value.trim(),
      email_body_notes: document.getElementById('tlEmailNotes').value.trim() || null,
    };
    if (!body.client_email) { alert('Enter client email'); return; }
    try {
      const res = await fetch('/api/tl/approve-and-send', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'X-User-Role': USER_ROLE, 'X-User-Email': '{{LOGGED_IN_EMAIL}}'},
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (data.error) { alert('Error: ' + data.error); return; }
      alert('Sent to client!');
      document.getElementById('tlSendModal').style.display = 'none';
      loadSubmissions();
    } catch(e) { alert('Failed: ' + e.message); }
  }

  function tlShowReject(subId) {
    document.getElementById('tlRejectSubId').value = subId;
    document.getElementById('tlRejectNote').value = '';
    document.getElementById('tlRejectModal').style.display = 'flex';
  }

  async function tlRejectSubmission() {
    const subId = document.getElementById('tlRejectSubId').value;
    const note = document.getElementById('tlRejectNote').value.trim();
    if (!note) { alert('Enter feedback note'); return; }
    try {
      const res = await fetch('/api/tl/reject', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'X-User-Role': USER_ROLE, 'X-User-Email': '{{LOGGED_IN_EMAIL}}'},
        body: JSON.stringify({ submission_id: subId, feedback: note })
      });
      const data = await res.json();
      if (data.error) { alert('Error: ' + data.error); return; }
      alert('Sent back to recruiter.');
      document.getElementById('tlRejectModal').style.display = 'none';
      loadSubmissions();
    } catch(e) { alert('Failed: ' + e.message); }
  }
</script>
</body>
</html>"""


# --- AI Agent Routes (formerly FastAPI, now served directly by Flask) ---

@app.route("/api/requirements", methods=["GET"])
def api_requirements():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    market = request.args.get("market")
    status = request.args.get("status", "open")
    created_after = request.args.get("created_after")
    project_id = request.args.get("project_id") or None
    # scope: 'mine' (recruiter's assigned reqs; default for recruiters) or 'all'
    scope = request.args.get("scope")
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    if scope is None:
        scope = "all" if role == "tl" else "mine"
    assigned_to = email if scope == "mine" else None
    # Default: only show requirements from the last 30 days to reduce clutter
    if not created_after and status == "open":
        from datetime import datetime, timedelta
        created_after = (datetime.utcnow() - timedelta(days=30)).isoformat()
    return _ai_core_call(ai_core.list_requirements, market, status,
                         created_after, project_id, assigned_to)


@app.route("/api/requirements/create", methods=["POST"])
def api_create_requirement():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.create_requirement,
                         request.get_json(silent=True), role, email)


@app.route("/api/requirements/<req_id>", methods=["PATCH"])
def api_update_requirement(req_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.update_requirement, req_id,
                         request.get_json(silent=True), role, email)


@app.route("/api/requirements/<req_id>/close", methods=["POST"])
def api_close_requirement(req_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.close_requirement, req_id, role, email)


@app.route("/api/requirements/<req_id>/pin", methods=["POST"])
def api_pin_requirement(req_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    body = request.get_json(silent=True) or {}
    should_pin = bool(body.get("pin", True))
    return _ai_core_call(ai_core.pin_requirement, req_id, should_pin, role, email)


@app.route("/api/requirements/<req_id>/clone", methods=["POST"])
def api_clone_requirement(req_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.clone_requirement, req_id, role, email)


@app.route("/api/requirements/<req_id>", methods=["DELETE"])
def api_delete_requirement(req_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.delete_requirement, req_id, role, email)


@app.route("/api/requirements/wipe-all", methods=["POST"])
def api_wipe_all_requirements():
    """Destructively delete every requirement + dependent rows. TL only."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.wipe_all_requirements, role, email)


@app.route("/api/requirements/<req_id>/source", methods=["POST"])
def api_source_requirement(req_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.source_requirement, req_id, role, email)


@app.route("/api/requirements/source-batch", methods=["POST"])
def api_source_batch():
    """Run Source Now on multiple requirements, capped per requirement."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.source_requirements_batch,
                         request.get_json(silent=True), role, email)


@app.route("/api/requirements/<req_id>/candidates", methods=["GET"])
def api_requirement_candidates(req_id):
    """Return top matched candidates for a requirement (match_scores backed)."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.get_requirement_candidates,
                         req_id, role, email)


# ── Candidate detail / shortlist / notes (Phase 3) ──

@app.route("/api/candidates/<candidate_id>/detail", methods=["GET"])
def api_candidate_detail(candidate_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.get_candidate_detail,
                         candidate_id, role, email)


@app.route("/api/candidates/<candidate_id>/shortlist", methods=["POST"])
def api_candidate_shortlist(candidate_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.toggle_shortlist_candidate,
                         candidate_id, request.get_json(silent=True),
                         role, email)


@app.route("/api/candidates/<candidate_id>/notes", methods=["POST"])
def api_candidate_add_note(candidate_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.add_note_to_candidate,
                         candidate_id, request.get_json(silent=True),
                         role, email)


@app.route("/api/candidates/export/pdf", methods=["POST"])
def api_candidates_export_pdf():
    """Generate a multi-page PDF with one page per selected candidate."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    body = request.get_json(silent=True) or {}
    candidate_ids = body.get("candidate_ids") or []
    if not candidate_ids:
        return jsonify({"error": "No candidate IDs provided"}), 400
    try:
        doc = fitz.open()
        role = session.get("recruiter_role", "recruiter")
        email_addr = session.get("recruiter_email", "")
        for cid in candidate_ids:
            try:
                detail = ai_core.get_candidate_detail(cid, role, email_addr)
                cand = detail.get("candidate") or {}
            except Exception:
                cand = {}
            if not cand:
                continue
            page = doc.new_page(width=595, height=842)  # A4
            y = 60
            name = cand.get("name") or "Unknown"
            page.insert_text((50, y), name, fontsize=18, fontname="helv", color=(0.1, 0.1, 0.1))
            y += 28
            title = cand.get("current_job_title") or ""
            employer = cand.get("current_employer") or ""
            if title or employer:
                page.insert_text((50, y), f"{title}{' @ ' + employer if employer else ''}", fontsize=12, color=(0.4, 0.4, 0.4))
                y += 20
            email = cand.get("email") or ""
            location = cand.get("current_location") or ""
            experience = cand.get("total_experience") or ""
            contact_line = "  |  ".join(filter(None, [email, location, experience]))
            if contact_line:
                page.insert_text((50, y), contact_line, fontsize=10, color=(0.5, 0.5, 0.5))
                y += 18
            y += 8
            page.draw_line((50, y), (545, y), color=(0.85, 0.85, 0.85), width=0.5)
            y += 16
            reasoning = cand.get("reasoning") or ""
            if reasoning:
                page.insert_text((50, y), "Summary", fontsize=11, fontname="helv", color=(0.3, 0.2, 0.6))
                y += 18
                words = reasoning.split()
                line, lines = [], []
                for w in words:
                    line.append(w)
                    if len(" ".join(line)) > 85:
                        lines.append(" ".join(line[:-1]))
                        line = [w]
                if line:
                    lines.append(" ".join(line))
                for ln in lines:
                    page.insert_text((50, y), ln, fontsize=10, color=(0.2, 0.2, 0.2))
                    y += 15
                    if y > 780:
                        break
        buf = io.BytesIO(doc.tobytes())
        doc.close()
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf",
                         as_attachment=True, download_name="candidates.pdf")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/candidates/<candidate_id>/submit-to-tl", methods=["POST"])
def api_candidate_submit_to_tl(candidate_id):
    """Recruiter pushes a shortlisted candidate into the TL submission queue."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.submit_to_tl,
                         candidate_id, request.get_json(silent=True),
                         role, email)


@app.route("/api/shortlists", methods=["GET"])
def api_list_shortlists():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.list_user_shortlists, role, email)


@app.route("/api/shortlists/delete", methods=["POST"])
def api_delete_shortlists():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    payload = request.get_json(silent=True) or {}
    return _ai_core_call(ai_core.delete_user_shortlists,
                         role, email, payload.get("shortlist_ids") or [])


# ── Sequences (Phase 4) ──

@app.route("/api/sequences/draft", methods=["POST"])
def api_sequence_draft():
    """Generate a template + personalized previews for a batch of candidates."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    payload = request.get_json(silent=True) or {}
    # Inject recruiter display name so the AI can sign the email
    payload.setdefault("recruiter_name", session.get("recruiter_name", ""))
    return _ai_core_call(ai_core.draft_sequence, payload, role, email)


@app.route("/api/sequences/send", methods=["POST"])
def api_sequence_send():
    """Send each email via Graph API as the logged-in user's mailbox."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.send_sequence,
                         request.get_json(silent=True), role, email)


@app.route("/api/sequences", methods=["GET"])
def api_sequences_list():
    """List sequenced outreach emails for the logged-in user (legacy Phase 4).

    Query param: ?scope=mine (default) | all (TL only)
    """
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    scope = request.args.get("scope", "mine")
    return _ai_core_call(ai_core.list_sequences, role, email, scope)


# ── Sequences v2 ─────────────────────────────────────────────

@app.route("/api/sequences/list", methods=["GET"])
def api_sequences_v2_list():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    scope = request.args.get("scope", "mine")
    try:
        days = int(request.args.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    return _ai_core_call(ai_core.list_sequences_v2, role, email, scope, days)


@app.route("/api/sequences/generate", methods=["POST"])
def api_sequences_generate():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    payload = request.get_json(silent=True) or {}

    def _stream():
        try:
            yield from ai_core.generate_sequence_stream(payload, role, email)
        except Exception as exc:
            import json as _json
            yield f"data: {_json.dumps({'event': 'error', 'message': str(exc)})}\n\n"

    return Response(
        stream_with_context(_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Agentic Boost ─────────────────────────────────────────────

@app.route("/api/agentic-boost/launch", methods=["POST"])
def api_agentic_boost_launch():
    """SSE — multi-agent pipeline from JD paste to ranked candidates."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    payload = request.get_json(silent=True) or {}

    def _stream():
        try:
            yield from ai_core.launch_agentic_boost_stream(payload, role, email)
        except Exception as exc:
            import json as _json
            app.logger.exception("agentic boost stream crashed")
            yield f"data: {_json.dumps({'event': 'error', 'message': str(exc)})}\n\n"

    return Response(
        stream_with_context(_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/agentic-boost/runs", methods=["GET"])
def api_agentic_boost_list():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.list_agentic_boost_runs, role, email)


@app.route("/api/agentic-boost/runs/<boost_id>", methods=["GET"])
def api_agentic_boost_get(boost_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.get_agentic_boost_run, boost_id, role, email)


@app.route("/api/agentic-boost/drafts/<draft_id>", methods=["PATCH"])
def api_agentic_boost_edit_draft(draft_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.update_agentic_boost_draft, draft_id,
                         request.get_json(silent=True) or {}, role, email)


@app.route("/api/agentic-boost/drafts/<draft_id>/send", methods=["POST"])
def api_agentic_boost_send_draft(draft_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.send_agentic_boost_draft, draft_id,
                         role, email)


@app.route("/api/sequences/new", methods=["POST"])
def api_sequences_create():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.create_sequence,
                         request.get_json(silent=True) or {}, role, email)


@app.route("/api/sequences/<seq_id>", methods=["GET"])
def api_sequences_detail(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    try:
        days = int(request.args.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    return _ai_core_call(ai_core.get_sequence_detail, seq_id, role, email, days)


@app.route("/api/sequences/<seq_id>", methods=["PUT"])
def api_sequences_update(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.update_sequence, seq_id,
                         request.get_json(silent=True) or {}, role, email)


@app.route("/api/sequences/<seq_id>", methods=["DELETE"])
def api_sequences_delete(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    hard = request.args.get("hard") == "1"
    return _ai_core_call(ai_core.delete_sequence, seq_id, role, email, hard)


@app.route("/api/sequences/<seq_id>/steps", methods=["POST"])
def api_sequences_create_step(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.create_step, seq_id,
                         request.get_json(silent=True) or {}, role, email)


@app.route("/api/sequences/<seq_id>/clone", methods=["POST"])
def api_sequences_clone(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.clone_sequence, seq_id, role, email)


@app.route("/api/sequences/<seq_id>/steps/<step_id>", methods=["PUT"])
def api_sequences_update_step(seq_id, step_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.update_step, seq_id, step_id,
                         request.get_json(silent=True) or {}, role, email)


@app.route("/api/sequences/<seq_id>/steps/reorder", methods=["POST"])
def api_sequences_reorder_steps(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    payload = request.get_json(silent=True) or {}
    return _ai_core_call(ai_core.reorder_steps, seq_id,
                         payload.get("step_ids") or [], role, email)


@app.route("/api/sequences/<seq_id>/preview", methods=["POST"])
def api_sequences_preview(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.preview_step1_for_candidates, seq_id,
                         request.get_json(silent=True), role, email)


@app.route("/api/sequences/<seq_id>/enroll", methods=["POST"])
def api_sequences_enroll(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.enroll_candidates, seq_id,
                         request.get_json(silent=True), role, email)


@app.route("/internal/sequence-tick", methods=["POST"])
def api_sequence_tick():
    # Dual auth: API key header (for external cron) OR session (for manual TL trigger)
    internal_key = os.environ.get("INTERNAL_API_KEY")
    req_key = request.headers.get("X-Internal-Key")
    if internal_key and req_key and req_key == internal_key:
        return _ai_core_call(ai_core.sequence_tick, None)
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    return _ai_core_call(ai_core.sequence_tick,
                         session.get("recruiter_role", "recruiter"))


@app.route("/api/sequences/<seq_id>/test-send", methods=["POST"])
def api_sequences_test_send(seq_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.test_send_step, seq_id,
                         request.get_json(silent=True) or {}, role, email)


# ── Signatures ───────────────────────────────────────────────

@app.route("/api/signatures", methods=["GET"])
def api_signatures_list():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.list_signatures_for_user, role, email)


@app.route("/api/signatures", methods=["POST"])
def api_signatures_create():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.create_signature,
                         request.get_json(silent=True) or {}, role, email)


@app.route("/api/signatures/<sig_id>", methods=["PUT"])
def api_signatures_update(sig_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.update_signature_handler, sig_id,
                         request.get_json(silent=True) or {}, role, email)


@app.route("/api/signatures/<sig_id>", methods=["DELETE"])
def api_signatures_delete(sig_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.delete_signature_handler, sig_id, role, email)


# ── Sequence tracking + unsubscribe (public; no auth) ───────

@app.route("/track/open/<token>.gif", methods=["GET"])
def track_open(token):
    """1×1 transparent GIF that records an open event."""
    try:
        gif_bytes, content_type = ai_core.track_open(token)
    except Exception:
        gif_bytes = ai_core._PIXEL_GIF
        content_type = "image/gif"
    return Response(
        gif_bytes,
        mimetype=content_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Content-Length": str(len(gif_bytes)),
        },
    )


@app.route("/track/click/<token>", methods=["GET"])
def track_click(token):
    """Record a click event and 302 to the original URL."""
    url_b64 = request.args.get("u", "")
    try:
        target = ai_core.track_click(token, url_b64)
    except Exception:
        target = "/"
    return redirect(target, code=302)


@app.route("/unsubscribe", methods=["GET"])
def unsubscribe_page():
    token = request.args.get("t", "")
    try:
        info = ai_core.unsubscribe_view(token)
    except ai_core.CoreError as e:
        return Response(
            f"<html><body style='font-family:Arial;padding:40px'>"
            f"<h2>Unsubscribe link is invalid</h2><p>{e.message}</p></body></html>",
            mimetype="text/html",
            status=e.status,
        )
    if info["already_unsubscribed"]:
        msg = (f"<p>{info['email']} is already unsubscribed. "
               "You won't receive any more messages from us.</p>")
        button = ""
    else:
        msg = (f"<p>Click the button below to confirm you'd like to stop "
               f"receiving emails from us at <strong>{info['email']}</strong>.</p>")
        button = (
            f"<form method='POST' action='/unsubscribe'>"
            f"<input type='hidden' name='t' value='{token}'>"
            f"<button type='submit' style='background:#7f56d9;color:white;"
            f"border:0;padding:12px 24px;border-radius:8px;font-size:16px;"
            f"cursor:pointer'>Confirm unsubscribe</button></form>"
        )
    html = (
        f"<html><body style='font-family:Arial,Helvetica,sans-serif;"
        f"max-width:520px;margin:60px auto;padding:24px;color:#222'>"
        f"<h2 style='margin-top:0'>Unsubscribe</h2>{msg}{button}"
        f"</body></html>"
    )
    return Response(html, mimetype="text/html")


@app.route("/unsubscribe", methods=["POST"])
def unsubscribe_commit():
    token = request.form.get("t") or (request.get_json(silent=True) or {}).get("t", "")
    try:
        result = ai_core.unsubscribe_commit(token)
    except ai_core.CoreError as e:
        return Response(
            f"<html><body style='font-family:Arial;padding:40px'>"
            f"<h2>Unsubscribe link is invalid</h2><p>{e.message}</p></body></html>",
            mimetype="text/html",
            status=e.status,
        )
    html = (
        f"<html><body style='font-family:Arial,Helvetica,sans-serif;"
        f"max-width:520px;margin:60px auto;padding:24px;color:#222'>"
        f"<h2>You're unsubscribed</h2>"
        f"<p>{result['email']} has been removed from our mailing list. "
        f"You won't receive any more messages from us.</p>"
        f"</body></html>"
    )
    return Response(html, mimetype="text/html")


@app.route("/api/requirements/<req_id>/linkedin")
def api_linkedin_string(req_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        requirement = ai_core.db.get_requirement_by_id(req_id)
        if not requirement:
            return jsonify({"error": "Requirement not found"}), 404
        linkedin = ai_core.sourcing.generate_linkedin_search_string(requirement)
        return jsonify({"linkedin_search_string": linkedin})
    except Exception as e:
        app.logger.exception("linkedin string generation failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pipeline")
def api_pipeline():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    market = request.args.get("market")
    project_id = request.args.get("project_id") or None
    return _ai_core_call(ai_core.pipeline_summary, market, project_id)


# ── Projects API ─────────────────────────────────────────────

@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.list_projects, email)


@app.route("/api/projects/create", methods=["POST"])
def api_create_project():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.create_project,
                         request.get_json(silent=True), role, email)


@app.route("/api/team", methods=["GET"])
def api_list_team():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    return _ai_core_call(ai_core.list_team)


@app.route("/api/projects/<project_id>", methods=["PATCH"])
def api_update_project(project_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.update_project, project_id,
                         request.get_json(silent=True), role, email)


@app.route("/api/projects/<project_id>/archive", methods=["POST"])
def api_archive_project(project_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.archive_project, project_id, role, email)


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def api_delete_project(project_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.delete_project, project_id, role, email)


@app.route("/api/tl/queue")
def api_tl_queue():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if session.get("recruiter_role") != "tl":
        return jsonify({"error": "TL only"}), 403
    return _ai_core_call(ai_core.tl_queue, "tl",
                         session.get("recruiter_email", ""))


@app.route("/api/tl/approve-and-send", methods=["POST"])
def api_tl_approve():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if session.get("recruiter_role") != "tl":
        return jsonify({"error": "TL only"}), 403
    return _ai_core_call(ai_core.tl_approve_and_send,
                         request.get_json(silent=True), "tl",
                         session.get("recruiter_email", ""))


@app.route("/api/tl/reject", methods=["POST"])
def api_tl_reject():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if session.get("recruiter_role") != "tl":
        return jsonify({"error": "TL only"}), 403
    return _ai_core_call(ai_core.tl_reject, request.get_json(silent=True), "tl")


@app.route("/api/submissions/<submission_id>/client-feedback", methods=["POST"])
def api_submissions_client_feedback(submission_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if session.get("recruiter_role") != "tl":
        return jsonify({"error": "TL only"}), 403
    payload = request.get_json(silent=True) or {}
    payload["submission_id"] = submission_id
    return _ai_core_call(ai_core.tl_set_client_feedback, payload, "tl")


@app.route("/api/submissions/my")
def api_submissions_my():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.get_my_submissions, email)


@app.route("/api/submissions/create", methods=["POST"])
def api_submissions_create():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    email = session.get("recruiter_email", "")

    # Accept multipart/form-data (resume + form fields) OR legacy JSON.
    if request.content_type and request.content_type.startswith("multipart/"):
        payload = {k: (v if v != "" else None) for k, v in request.form.items()}
        resume = request.files.get("resume")
        if resume and resume.filename:
            ext = Path(resume.filename).suffix.lower()
            if ext in ALLOWED_RESUME_EXT:
                SUBMISSION_RESUMES_DIR.mkdir(exist_ok=True)
                cid = payload.get("candidate_id") or "unknown"
                fname = f"{cid}_{int(time.time())}{ext}"
                dest = SUBMISSION_RESUMES_DIR / fname
                resume.save(dest)
                payload["resume_path"] = str(dest)
            # Reject silently for unsupported extensions — submission still goes through.
    else:
        payload = request.get_json(silent=True)
    return _ai_core_call(ai_core.create_submission, payload, email)


@app.route("/api/submissions/<submission_id>/comms")
def api_submissions_comms(submission_id):
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if session.get("recruiter_role") != "tl":
        return jsonify({"error": "TL only"}), 403
    return _ai_core_call(ai_core.get_submission_comms, submission_id)


@app.route("/api/submissions/tl")
def api_submissions_tl():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    if session.get("recruiter_role") != "tl":
        return jsonify({"error": "TL only"}), 403
    requirement_id = request.args.get("requirement_id") or None
    return _ai_core_call(ai_core.get_tl_submissions, requirement_id)


@app.route("/api/performance")
def api_performance():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    role = session.get("recruiter_role", "recruiter")
    email = session.get("recruiter_email", "")
    return _ai_core_call(ai_core.get_performance, role, email)


@app.route("/api/usage")
def api_usage():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    return _ai_core_call(ai_core.get_usage)


@app.route("/api/notifications")
def api_notifications():
    if not is_logged_in():
        return jsonify({"notifications": []})
    try:
        data = ai_core.pipeline_summary(None)
    except Exception:
        return jsonify({"notifications": []})
    notifs = []
    for p in (data.get("pipeline") or [])[:5]:
        if p.get("replied", 0) > 0:
            notifs.append({
                "message": f"{p.get('role_title', 'Role')}: {p['replied']} candidate(s) replied",
                "read": False, "time": None,
            })
        if p.get("shortlisted", 0) > 0:
            notifs.append({
                "message": f"{p.get('role_title', 'Role')}: {p['shortlisted']} shortlisted out of {p.get('screened', 0)} screened",
                "read": True, "time": None,
            })
    return jsonify({"notifications": notifs})


@app.route("/api/download-doc")
def api_download_doc():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    doc_path = request.args.get("path", "")
    SAFE_DOC_DIR = Path(__file__).parent.resolve()
    p = Path(doc_path).resolve()
    if not str(p).startswith(str(SAFE_DOC_DIR)):
        return jsonify({"error": "Access denied"}), 403
    if p.exists() and p.is_file():
        return send_file(p, as_attachment=True)
    return jsonify({"error": "File not found"}), 404


# --- Source (Hunt) routes ---

@app.route("/source/upload", methods=["POST"])
def source_upload():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    SOURCE_TMP_DIR.mkdir(exist_ok=True)
    for f in SOURCE_TMP_DIR.iterdir():
        if f.is_file():
            f.unlink()
    resumes = request.files.getlist("resumes")
    jd = request.files.get("jd")
    client_name = request.form.get("client_name", "")
    location = request.form.get("location", "")
    recruiter = request.form.get("recruiter", "")
    if not resumes or not any(f.filename for f in resumes):
        return jsonify({"error": "No resumes uploaded"}), 400
    if not jd or not jd.filename:
        return jsonify({"error": "No JD uploaded"}), 400
    if not recruiter:
        return jsonify({"error": "No recruiter selected"}), 400
    allowed_resume = {".pdf", ".docx", ".doc"}
    allowed_jd = {".txt", ".pdf"}
    saved_resumes = []
    for f in resumes:
        if f and f.filename:
            ext = Path(f.filename).suffix.lower()
            if ext in allowed_resume:
                dest = SOURCE_TMP_DIR / f.filename
                f.save(dest)
                saved_resumes.append(str(dest))
    jd_ext = Path(jd.filename).suffix.lower()
    if jd_ext not in allowed_jd:
        return jsonify({"error": "JD must be TXT or PDF"}), 400
    jd_path = SOURCE_TMP_DIR / f"jd{jd_ext}"
    jd.save(jd_path)
    return jsonify({
        "resume_paths": saved_resumes, "jd_path": str(jd_path),
        "client_name": client_name, "location": location,
        "recruiter": recruiter, "count": len(saved_resumes),
    })


@app.route("/source/screen", methods=["POST"])
def source_screen():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json()
    resume_paths = data.get("resume_paths", [])
    jd_path = data.get("jd_path", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500
    client = Anthropic(api_key=api_key)
    jd_p = Path(jd_path)
    if not jd_p.exists():
        return jsonify({"error": "JD file not found"}), 400
    try:
        if jd_p.suffix.lower() == ".pdf":
            jd_text = _extract_text_from_pdf_source(jd_p)
        else:
            jd_text = jd_p.read_text().strip()
    except Exception as e:
        return jsonify({"error": f"Error reading JD: {e}"}), 500
    if not jd_text:
        return jsonify({"error": "Could not extract text from JD"}), 400
    jd_details = _extract_jd_details(client, jd_text)
    results = []
    for rp in resume_paths:
        rp_path = Path(rp)
        if not rp_path.exists():
            continue
        try:
            resume_text = _extract_text(rp_path)
        except Exception:
            resume_text = ""
        if not resume_text:
            results.append({
                "file": rp_path.name, "name": rp_path.stem, "skillset": "",
                "email": "", "contact_no": "", "score": 0, "label": "Rejected",
                "reason": "Could not extract text", "match_score": "0/10 \u2014 Rejected",
                "resume_path": str(rp_path),
            })
            continue
        info = _screen_candidate(client, resume_text, jd_text)
        score = info.get("score", 0)
        label = info.get("label", "Rejected")
        name = info.get("name", "").title() if info.get("name") else rp_path.stem
        results.append({
            "file": rp_path.name, "name": name,
            "skillset": info.get("skillset", ""), "email": info.get("email", ""),
            "contact_no": info.get("contact_no", ""), "score": score,
            "label": label, "reason": info.get("reason", ""),
            "match_score": f"{score}/10 \u2014 {label}",
            "resume_path": str(rp_path),
        })
    return jsonify({"results": results, "jd_details": jd_details, "jd_path": jd_path})


# Outreach Automation model notes:
# - Email classification (inbox triage) → claude-haiku-4-5-20251001
# - Reply suggestions (drafting responses) → claude-sonnet-4-20250514

@app.route("/source/send-emails", methods=["POST"])
def source_send_emails():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json()
    candidates = data.get("candidates", [])
    recruiter_name = data.get("recruiter_name", "")
    recruiter_email = data.get("recruiter_email", "")
    client_name = data.get("client_name", "")
    location = data.get("location", "")
    jd_details = data.get("jd_details", {})
    jd_path = data.get("jd_path", "")
    if not recruiter_email or not recruiter_name:
        return jsonify({"error": "Recruiter not specified"}), 400

    # Get Graph API token
    import msal
    import requests as req
    client_id = os.environ.get("AZURE_CLIENT_ID", "")
    tenant_id = os.environ.get("AZURE_TENANT_ID", "")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET", "")
    if not client_id or not tenant_id or not client_secret:
        return jsonify({"error": "Azure credentials not configured"}), 500
    msal_app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    token_result = msal_app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in token_result:
        return jsonify({"error": f"Token error: {token_result.get('error_description', 'Unknown')}"}), 500
    access_token = token_result["access_token"]
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    requirement = data.get("requirement", "")
    job_title = requirement or jd_details.get("job_title", "Open Position")
    jd_full_text = ""
    jd_file_obj = Path(jd_path) if jd_path else None
    if jd_file_obj and jd_file_obj.exists():
        try:
            if jd_file_obj.suffix.lower() == ".pdf":
                jd_full_text = _extract_text_from_pdf_source(jd_file_obj)
            else:
                jd_full_text = jd_file_obj.read_text(errors="replace").strip()
        except Exception:
            jd_full_text = jd_details.get("summary", "")

    # Prepare JD attachment as base64
    import base64
    jd_file_path = Path(jd_path) if jd_path else None
    jd_attachment = None
    if jd_file_path and jd_file_path.exists():
        jd_bytes = jd_file_path.read_bytes()
        jd_b64 = base64.b64encode(jd_bytes).decode("utf-8")
        jd_ext = jd_file_path.suffix.lower()
        jd_att_name = f"JD - {job_title}.pdf" if jd_ext == ".pdf" else f"JD - {job_title}.txt"
        content_type = "application/pdf" if jd_ext == ".pdf" else "text/plain"
        jd_attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": jd_att_name,
            "contentType": content_type,
            "contentBytes": jd_b64,
        }

    statuses = []
    for cand in candidates:
        cand_email = cand.get("email", "").strip()
        cand_name = cand.get("name", "Candidate")
        if not cand_email:
            statuses.append({"name": cand_name, "status": "Failed", "reason": "No email address"})
            _log_to_sheet(recruiter_name, cand, client_name, location, "Failed \u2014 No email")
            continue

        jd_html = jd_full_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

        body_html = f"""<div style="font-family:Calibri,Arial,sans-serif;font-size:14px;color:#333;">
<p>Hi {cand_name},</p>
<p>I hope you're doing well. My name is {recruiter_name}, and I'm a Technical Recruiter at ExcelTech Computers.
We have an exciting opening for a <b>{job_title}</b> at <b>{client_name}</b>, <b>{location}</b>, and your profile matches what we're looking for.</p>
<p><b>Role:</b> {job_title}<br><b>Location:</b> {location}<br><b>Company:</b> {client_name}</p>
<p><b>Job Description:</b></p>
<div style="background:#f9f9f9;border:1px solid #ddd;border-radius:6px;padding:12px;margin:8px 0;font-size:13px;">{jd_html}</div>
<p><b style="color:#c00;">Please share resume and below details (All mandatory):</b></p>
<table style="border-collapse:collapse;width:100%;max-width:500px;margin:8px 0;" cellpadding="0" cellspacing="0">
<tr style="background:#FFFF00;"><td style="border:1px solid #333;padding:6px 10px;font-weight:bold;" colspan="2">Hire Block</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;width:60%;">First Name (As Per 10th Marksheet)</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Last Name (As Per 10th Marksheet)</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">DOB (YYYY/MM/DD)</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Contact Number</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Email ID</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Total Experience (in months)</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Relevant Experience (in months)</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
</table>
<br>
<table style="border-collapse:collapse;width:100%;max-width:500px;margin:8px 0;" cellpadding="0" cellspacing="0">
<tr><td style="border:1px solid #999;padding:6px 10px;width:60%;">Passport Number/SSC Mark sheet Number</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Rehire. If yes share the old SAP ID</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Complete address details with City, State and Pin code</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Current CTC</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Expected CTC</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Current Location</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Preferred Location</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Notice Period or LWD</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
<tr><td style="border:1px solid #999;padding:6px 10px;">Current Company</td><td style="border:1px solid #999;padding:6px 10px;">&nbsp;</td></tr>
</table>
<p>Warm regards,<br><b>{recruiter_name}</b><br>ExcelTech Computers Pte Ltd</p>
</div>"""

        email_msg = {
            "message": {
                "subject": f"Exciting Opportunity \u2014 {job_title} at {client_name} | ExcelTech Computers",
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": cand_email}}],
            },
            "saveToSentItems": "true",
        }
        if jd_attachment:
            email_msg["message"]["attachments"] = [jd_attachment]

        try:
            send_url = f"https://graph.microsoft.com/v1.0/users/{recruiter_email}/sendMail"
            resp = req.post(send_url, headers=headers, json=email_msg)
            if resp.status_code == 202:
                statuses.append({"name": cand_name, "status": "Email Sent"})
                _log_to_sheet(recruiter_name, cand, client_name, location, "Email Sent")
            else:
                err_text = resp.text[:200]
                statuses.append({"name": cand_name, "status": "Failed", "reason": err_text})
                _log_to_sheet(recruiter_name, cand, client_name, location, f"Failed \u2014 {err_text}")
        except Exception as e:
            statuses.append({"name": cand_name, "status": "Failed", "reason": str(e)})
            _log_to_sheet(recruiter_name, cand, client_name, location, f"Failed \u2014 {e}")
    return jsonify({"statuses": statuses})


@app.route("/download-zip", methods=["POST"])
def download_zip():
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    import zipfile, io
    data = request.get_json()
    role = data.get("role", "Resumes")
    files_info = data.get("files", [])  # [{name, path}]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fi in files_info:
            fpath = Path(fi.get("path", ""))
            cand_name = fi.get("name", "Unknown").strip().replace(" ", "_")
            if fpath.exists():
                ext = fpath.suffix
                new_name = f"{cand_name}_{role.replace(' ', '_')}{ext}"
                zf.write(fpath, new_name)
    buf.seek(0)
    zip_name = "Requirement.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=zip_name)


@app.route("/rename-zip", methods=["POST"])
def rename_zip():
    """Extract candidate names from resume content using AI, rename to Name_Role.ext, return as ZIP."""
    if not is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    import zipfile, io
    role = request.args.get("role", "Resumes")
    resumes = request.files.getlist("resumes")
    if not resumes or not any(f.filename for f in resumes):
        return jsonify({"error": "No resumes uploaded"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = Anthropic(api_key=api_key) if api_key else None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in resumes:
            if not f or not f.filename:
                continue
            ext = Path(f.filename).suffix.lower()
            file_bytes = f.read()

            # Extract text from resume
            resume_text = ""
            try:
                if ext == ".pdf":
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(file_bytes)
                        tmp_path = tmp.name
                    with fitz.open(tmp_path) as doc:
                        for page in doc:
                            resume_text += page.get_text()
                    os.unlink(tmp_path)
                elif ext in (".docx", ".doc"):
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                        tmp.write(file_bytes)
                        tmp_path = tmp.name
                    resume_text = _extract_text_from_docx(Path(tmp_path))
                    os.unlink(tmp_path)
            except Exception:
                pass

            # Use AI to extract candidate name from resume text
            cand_name = Path(f.filename).stem  # fallback
            if resume_text.strip() and client:
                try:
                    resp = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=30,
                        system=(
                            "You extract candidate names from resumes. "
                            "The candidate's full name is almost always in the FIRST 1-3 lines of the resume text, often before any email/phone. "
                            "Return ONLY the person's full name in Title Case. Nothing else — no quotes, labels, or explanation. "
                            "Example outputs: Rahul Sharma, Priya Nair, Dhinesh Kumar"
                        ),
                        messages=[{"role": "user", "content": f"What is the candidate's full name?\n\n{resume_text[:2000]}"}],
                    )
                    extracted = resp.content[0].text.strip()
                    # Clean up any extra text the model might add
                    extracted = extracted.split("\n")[0].strip().strip('"').strip("'")
                    if extracted and "unknown" not in extracted.lower() and len(extracted) > 1 and len(extracted) < 60:
                        cand_name = extracted
                except Exception:
                    pass

            cand_name = cand_name.strip().replace("/", "_").replace("\\", "_")
            new_name = f"{cand_name}_{role}{ext}"
            zf.writestr(new_name, file_bytes)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="Requirement.zip")


if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", "5001"))
    app.run(debug=True, host="127.0.0.1", port=port)
