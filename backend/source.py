#!/usr/bin/env python3
"""
Source Candidates — Flask Blueprint
Screen resumes against a JD, review results, send outreach emails,
and log to the Sourcing Tracker Google Sheet.
"""

import os
import sys
import json
import re
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

import fitz  # PyMuPDF
from docx import Document
from flask import Blueprint, request, session, redirect, url_for, jsonify, render_template
from anthropic import Anthropic
import gspread
from google.oauth2.service_account import Credentials

source_bp = Blueprint("source", __name__)

SCRIPT_DIR = Path(__file__).parent
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
SOURCE_TMP_DIR = SCRIPT_DIR / "source_tmp"

SESSION_VERSION = "2"

RECRUITERS = {
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


def _password_env_key(email: str) -> str:
    local = email.split("@")[0].replace(".", "_").upper()
    return f"OUTLOOK_PASSWORD_{local}"


def _is_logged_in():
    return (session.get("logged_in") is True and
            session.get("version") == SESSION_VERSION)


# --- Text extraction ---

def _extract_text_from_pdf(filepath: Path) -> str:
    text = ""
    with fitz.open(filepath) as doc:
        for page in doc:
            text += page.get_text()
    return text.strip()


def _extract_text_from_docx(filepath: Path) -> str:
    doc = Document(filepath)
    return "\n".join(p.text for p in doc.paragraphs).strip()


def _extract_text(filepath: Path) -> str:
    suffix = filepath.suffix.lower()
    if suffix == ".pdf":
        return _extract_text_from_pdf(filepath)
    elif suffix in (".docx", ".doc"):
        return _extract_text_from_docx(filepath)
    return ""


# --- API helpers ---

def _parse_api_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        raw = "\n".join(lines)
    return json.loads(raw)


def _screen_candidate(client: Anthropic, resume_text: str, jd_text: str) -> dict:
    system_prompt = (
        "You are a recruitment screener. Score the resume against the job description. "
        "Return ONLY valid JSON with these exact keys:\n"
        '  "score": integer from 1 to 10\n'
        '  "label": exactly one of "Excellent Match", "Strong Match", "Good Match", "Rejected"\n'
        '  "reason": one sentence explaining the score\n'
        '  "skillset": 1-2 words MAXIMUM for the primary job role keyword (e.g. "Python", "Embedded C", "DevOps"). Or "" if not found.\n'
        '  "name": full name of the candidate, or "" if not found\n'
        '  "contact_no": phone number, or "" if not found\n'
        '  "email": email address, or "" if not found\n'
        "Do NOT guess or fabricate. If not clearly present, use empty string.\n"
        "Scoring guide: 8-10 Excellent Match, 6-7 Strong Match or Good Match, below 6 Rejected."
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
    """Extract job title and key details from JD for email."""
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


# --- Google Sheets ---

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
        today,
        recruiter_name,
        candidate.get("name", "").title(),
        candidate.get("skillset", ""),
        candidate.get("email", ""),
        contact,
        client_name,
        location,
        status,
    ]

    for tab_name in [recruiter_name, "MasterList"]:
        ws = _get_worksheet(spreadsheet, tab_name)
        if ws:
            all_vals = ws.get_all_values()
            next_row = len(all_vals) + 1
            for col_idx, val in enumerate(row, start=1):
                ws.update_cell(next_row, col_idx, val)

    return "ok"


# --- Routes ---

@source_bp.route("/source")
def source_page():
    if not _is_logged_in():
        return redirect(url_for("login"))
    sourcing_sheet_id = os.environ.get("SOURCING_SHEET_ID", "")
    sourcing_url = f"https://docs.google.com/spreadsheets/d/{sourcing_sheet_id}/edit" if sourcing_sheet_id else ""
    return render_template("source.html",
                           recruiters=RECRUITERS,
                           sourcing_url=sourcing_url)


@source_bp.route("/source/upload", methods=["POST"])
def source_upload():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    SOURCE_TMP_DIR.mkdir(exist_ok=True)

    # Clear old files
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
        "resume_paths": saved_resumes,
        "jd_path": str(jd_path),
        "client_name": client_name,
        "location": location,
        "recruiter": recruiter,
        "count": len(saved_resumes),
    })


@source_bp.route("/source/screen", methods=["POST"])
def source_screen():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    resume_paths = data.get("resume_paths", [])
    jd_path = data.get("jd_path", "")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    client = Anthropic(api_key=api_key)

    # Read JD
    jd_p = Path(jd_path)
    if not jd_p.exists():
        return jsonify({"error": "JD file not found"}), 400

    try:
        if jd_p.suffix.lower() == ".pdf":
            jd_text = _extract_text_from_pdf(jd_p)
        else:
            jd_text = jd_p.read_text().strip()
    except Exception as e:
        return jsonify({"error": f"Error reading JD: {e}"}), 500

    if not jd_text:
        return jsonify({"error": "Could not extract text from JD"}), 400

    # Extract JD details for emails
    jd_details = _extract_jd_details(client, jd_text)

    # Screen each resume
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
                "reason": "Could not extract text", "match_score": "0/10 — Rejected",
            })
            continue

        info = _screen_candidate(client, resume_text, jd_text)
        score = info.get("score", 0)
        label = info.get("label", "Rejected")
        reason = info.get("reason", "")
        name = info.get("name", "").title() if info.get("name") else rp_path.stem

        results.append({
            "file": rp_path.name,
            "name": name,
            "skillset": info.get("skillset", ""),
            "email": info.get("email", ""),
            "contact_no": info.get("contact_no", ""),
            "score": score,
            "label": label,
            "reason": reason,
            "match_score": f"{score}/10 \u2014 {label}",
        })

    return jsonify({
        "results": results,
        "jd_details": jd_details,
        "jd_path": jd_path,
    })


@source_bp.route("/source/send-emails", methods=["POST"])
def source_send_emails():
    if not _is_logged_in():
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

    # Get password
    env_key = _password_env_key(recruiter_email)
    password = os.environ.get(env_key)
    if not password:
        return jsonify({"error": f"Email not configured for this recruiter yet (missing {env_key})"}), 400

    job_title = jd_details.get("job_title", "Open Position")
    jd_summary = jd_details.get("summary", "Please see the attached job description for details.")

    # Read JD file for attachment
    jd_file_path = Path(jd_path) if jd_path else None
    jd_attachment_data = None
    jd_attachment_name = None
    if jd_file_path and jd_file_path.exists():
        jd_attachment_data = jd_file_path.read_bytes()
        jd_attachment_name = f"JD - {job_title}.pdf" if jd_file_path.suffix.lower() == ".pdf" else f"JD - {job_title}.txt"

    statuses = []

    for cand in candidates:
        cand_email = cand.get("email", "").strip()
        cand_name = cand.get("name", "Candidate")

        if not cand_email:
            statuses.append({"name": cand_name, "status": "Failed", "reason": "No email address"})
            _log_to_sheet(recruiter_name, cand, client_name, location, "Failed \u2014 No email")
            continue

        # Build email
        body = (
            f"Hi {cand_name},\n\n"
            f"My name is {recruiter_name} from ExcelTech Computers. "
            f"We came across your profile and believe you could be a great fit for an exciting opportunity with one of our clients.\n\n"
            f"About the Role:\n{jd_summary}\n\n"
            f"Location: {location}\n"
            f"Client: {client_name}\n\n"
            f"Please find the job description attached for your reference. "
            f"If you are interested, kindly reply to this email with your updated resume and we will get back to you shortly.\n\n"
            f"Warm regards,\n"
            f"{recruiter_name}\n"
            f"ExcelTech Computers Pte Ltd"
        )

        msg = MIMEMultipart()
        msg["From"] = recruiter_email
        msg["To"] = cand_email
        msg["Reply-To"] = recruiter_email
        msg["Subject"] = f"Exciting Opportunity \u2014 {job_title} | ExcelTech Computers"
        msg.attach(MIMEText(body, "plain"))

        # Attach JD
        if jd_attachment_data and jd_attachment_name:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(jd_attachment_data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{jd_attachment_name}"')
            msg.attach(part)

        try:
            with smtplib.SMTP("smtp.office365.com", 587) as server:
                server.starttls()
                server.login(recruiter_email, password)
                server.send_message(msg)
            statuses.append({"name": cand_name, "status": "Email Sent"})
            _log_to_sheet(recruiter_name, cand, client_name, location, "Email Sent")
        except Exception as e:
            statuses.append({"name": cand_name, "status": "Failed", "reason": str(e)})
            _log_to_sheet(recruiter_name, cand, client_name, location, f"Failed \u2014 {e}")

    return jsonify({"statuses": statuses})
