#!/usr/bin/env python3
"""One-time migration: ExcelTech submission tracker Excel → Supabase.

Usage:
    python migrate.py --file /path/to/tracker.xlsx --sheet all --dry-run
    python migrate.py --file /path/to/tracker.xlsx --sheet "Master submission sheet"
    python migrate.py --file /path/to/tracker.xlsx --market SG --dry-run
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)

# Add parent to path so we can import config.db
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.db import get_client, upsert_candidate_by_email

# ── Constants ──────────────────────────────────────────────────

INDIA_CLIENTS = {
    "hcl", "hcl india", "ncr", "ncr in", "tech mahindra",
    "paytm", "flipkart", "keva", "bajaj", "wipro", "infosys",
}
SG_KEYWORDS = {
    "lgt bank", "lgt", "edgefield", "millennia", "school",
    "secondary", "institute", "polytechnic", "ite", "gebiz",
    "singapore", "temasek", "ngee ann", "nanyang",
}

SHEET_HANDLERS = {}  # populated by decorators below


def _reg(name):
    def decorator(fn):
        SHEET_HANDLERS[name] = fn
        return fn
    return decorator


# ── Helpers ────────────────────────────────────────────────────

def _clean(val):
    """Return stripped string or None."""
    if pd.isna(val):
        return None
    s = str(val).strip()
    return s if s else None


def _clean_phone(val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"[^0-9+\s()\-]", "", s)
    return s if s else None


def _parse_date(val):
    """Try to parse a date value into ISO string."""
    if pd.isna(val) or val is None:
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()[:10]
    s = str(val).strip()
    for fmt in ("%d-%b-%y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
                "%d %b %Y", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _infer_market(client_name: str | None) -> str:
    """Infer IN or SG from client name."""
    if not client_name:
        return "IN"
    lower = client_name.lower().strip()
    if any(k in lower for k in SG_KEYWORDS):
        return "SG"
    if any(k in lower for k in INDIA_CLIENTS):
        return "IN"
    return "IN"


def _map_final_status(raw: str | None) -> str | None:
    """Normalize final status to match DB check constraint."""
    if not raw:
        return None
    s = raw.strip()
    mapping = {
        "submitted": "Submitted", "shortlisted": "Shortlisted",
        "kiv": "KIV", "not shortlisted": "Not Shortlisted",
        "selected-joined": "Selected-Joined", "selected": "Selected",
        "backed out": "Backed out", "rejected": "Rejected",
        "selected-backed out": "Selected-Backed out",
    }
    return mapping.get(s.lower(), s if s in mapping.values() else None)


def _map_placement_type(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().upper()
    if s in ("FTE", "TP", "C2H"):
        return s
    if "third" in s.lower() or "contract" in s.lower():
        return "TP"
    return None


def _col(df, *names):
    """Find first matching column name (case-insensitive)."""
    cols_lower = {c.lower().strip(): c for c in df.columns}
    for n in names:
        key = n.lower().strip()
        if key in cols_lower:
            return cols_lower[key]
    return None


def _get(row, df, *names):
    """Get value from row using flexible column matching."""
    col = _col(df, *names)
    if col is None:
        return None
    return row.get(col)


class MigrationReport:
    def __init__(self):
        self.entries = []

    def add(self, sheet, rows_read, inserted, skipped_dup, skipped_blank, errors):
        self.entries.append({
            "sheet": sheet, "rows_read": rows_read, "inserted": inserted,
            "skipped_dup": skipped_dup, "skipped_blank": skipped_blank,
            "errors": errors,
        })

    def print_summary(self):
        print("\n" + "=" * 80)
        print("MIGRATION REPORT")
        print("=" * 80)
        fmt = "{:<30} {:>8} {:>8} {:>8} {:>8} {:>6}"
        print(fmt.format("Sheet", "Read", "Insert", "Dup", "Blank", "Err"))
        print("-" * 80)
        for e in self.entries:
            print(fmt.format(
                e["sheet"][:30], e["rows_read"], e["inserted"],
                e["skipped_dup"], e["skipped_blank"], e["errors"]))
        print("=" * 80)

    def save(self, path: str):
        with open(path, "w") as f:
            f.write("MIGRATION REPORT\n")
            f.write(f"Date: {datetime.now().isoformat()}\n\n")
            fmt = "{:<30} {:>8} {:>8} {:>8} {:>8} {:>6}\n"
            f.write(fmt.format("Sheet", "Read", "Insert", "Dup", "Blank", "Err"))
            f.write("-" * 80 + "\n")
            for e in self.entries:
                f.write(fmt.format(
                    e["sheet"][:30], e["rows_read"], e["inserted"],
                    e["skipped_dup"], e["skipped_blank"], e["errors"]))


report = MigrationReport()


def _safe_insert(table: str, data: dict, dry_run: bool) -> bool:
    """Insert a row, return True on success."""
    if dry_run:
        return True
    try:
        get_client().table(table).insert(data).execute()
        return True
    except Exception as e:
        print(f"    [ERR] {table} insert: {e}")
        return False


def _safe_upsert_candidate(data: dict, dry_run: bool) -> dict | None:
    """Upsert candidate by email, return row or None."""
    if dry_run:
        return {"id": "dry-run-id"}
    try:
        return upsert_candidate_by_email(data)
    except Exception as e:
        print(f"    [ERR] candidate upsert: {e}")
        return None


# ── Sheet Handlers ─────────────────────────────────────────────

@_reg("Master submission sheet")
def migrate_master(df, dry_run, market_filter):
    inserted = skipped_dup = skipped_blank = errors = 0
    seen_emails = set()

    for idx, row in df.iterrows():
        name = _clean(_get(row, df, "Name of the Candidate", "Name of Candidate",
                           "Candidate Name", "Name"))
        if not name:
            skipped_blank += 1
            continue

        email = _clean(_get(row, df, "Email ID", "Email", "E-mail"))
        client = _clean(_get(row, df, "Client", "Client Name"))
        market = _infer_market(client)

        if market_filter and market_filter != "both" and market != market_filter:
            skipped_blank += 1
            continue

        # Upsert candidate
        cand_data = {
            "name": name,
            "email": email,
            "phone": _clean_phone(_get(row, df, "Contact No.", "Contact No", "Mob", "Phone")),
            "cv_id": _clean(_get(row, df, "CVID", "CV ID", "CV Id")),
            "market": market,
            "source": "excel_migration",
        }
        if email and email in seen_emails:
            skipped_dup += 1
            continue
        if email:
            seen_emails.add(email)

        cand = _safe_upsert_candidate(
            {k: v for k, v in cand_data.items() if v is not None},
            dry_run,
        )
        if not cand:
            errors += 1
            continue

        # Insert submission
        sub_data = {
            "candidate_id": cand["id"],
            "client_name": client,
            "market": market,
            "submitted_by_recruiter": _clean(_get(row, df, "Recuiter's name",
                                                   "Recruiter's name", "Recruiter", "Recruiter Name")),
            "submitted_at": _parse_date(_get(row, df, "Submission Date", "Date")),
            "final_status": _map_final_status(_clean(_get(row, df, "Final status",
                                                          "Final Status", "Status"))),
            "remarks": _clean(_get(row, df, "Remarks")),
        }
        sub_data = {k: v for k, v in sub_data.items() if v is not None}

        if _safe_insert("submissions", sub_data, dry_run):
            inserted += 1
        else:
            errors += 1

    return inserted, skipped_dup, skipped_blank, errors


@_reg("Interview Tracker Updated")
def migrate_interviews(df, dry_run, market_filter):
    inserted = skipped_dup = skipped_blank = errors = 0
    seen_emails = set()

    for idx, row in df.iterrows():
        name = _clean(_get(row, df, "Name of Candidate", "Name of the Candidate",
                           "Candidate Name", "Name"))
        if not name:
            skipped_blank += 1
            continue

        email = _clean(_get(row, df, "Email", "Email ID", "E-mail"))
        client = _clean(_get(row, df, "Client", "End client", "End Client"))

        # Upsert candidate
        cand_data = {
            "name": name,
            "email": email,
            "phone": _clean_phone(_get(row, df, "Mob", "Contact No", "Phone")),
            "cv_id": _clean(_get(row, df, "CV ID", "CVID")),
            "market": _infer_market(client),
            "source": "excel_migration",
        }
        if email and email in seen_emails:
            cand = _safe_upsert_candidate(
                {k: v for k, v in cand_data.items() if v is not None}, dry_run)
            skipped_dup += 1
        else:
            if email:
                seen_emails.add(email)
            cand = _safe_upsert_candidate(
                {k: v for k, v in cand_data.items() if v is not None}, dry_run)

        if not cand:
            errors += 1
            continue

        tracker_data = {
            "candidate_id": cand["id"],
            "recruiter": _clean(_get(row, df, "Recruiter", "Recruiter Name")),
            "interview_date": _parse_date(_get(row, df, "Interview Date")),
            "interview_time": _clean(_get(row, df, "Time", "Interview Time")),
            "status": _clean(_get(row, df, "Status", "Final Status")),
            "end_client": _clean(_get(row, df, "End client", "End Client", "Client")),
            "placement_type": _map_placement_type(_clean(_get(row, df, "Placement type",
                                                               "Placement Type"))),
            "doj": _parse_date(_get(row, df, "DOJ")),
            "package": _clean(_get(row, df, "Package")),
            "sap_id": _clean(_get(row, df, "SAP ID", "SAP Id")),
            "remarks": _clean(_get(row, df, "Remarks")),
        }
        tracker_data = {k: v for k, v in tracker_data.items() if v is not None}

        if _safe_insert("interview_tracker", tracker_data, dry_run):
            inserted += 1
        else:
            errors += 1

    return inserted, skipped_dup, skipped_blank, errors


@_reg("Onboarded Candidates")
def migrate_onboarded(df, dry_run, market_filter):
    inserted = skipped_dup = skipped_blank = errors = 0

    for idx, row in df.iterrows():
        name = _clean(_get(row, df, "Name", "Name of Candidate", "Candidate Name"))
        if not name:
            skipped_blank += 1
            continue

        email = _clean(_get(row, df, "Email Id", "Email ID", "Email"))
        client = _clean(_get(row, df, "End client", "End Client", "Client"))

        cand_data = {
            "name": name,
            "email": email,
            "phone": _clean_phone(_get(row, df, "Contact No", "Contact No.", "Mob")),
            "cv_id": _clean(_get(row, df, "CV ID", "CVID")),
            "market": _infer_market(client),
            "source": "excel_migration",
        }
        cand = _safe_upsert_candidate(
            {k: v for k, v in cand_data.items() if v is not None}, dry_run)
        if not cand:
            errors += 1
            continue

        sub_data = {
            "candidate_id": cand["id"],
            "client_name": client,
            "market": _infer_market(client),
            "submitted_by_recruiter": _clean(_get(row, df, "Recruiter")),
            "final_status": _map_final_status(_clean(_get(row, df, "Status"))),
            "placement_type": _map_placement_type(_clean(_get(row, df, "Placement type",
                                                               "Placement Type"))),
            "doj": _parse_date(_get(row, df, "DOJ")),
            "package": _clean(_get(row, df, "Package")),
            "sap_id": _clean(_get(row, df, "SAP ID", "SAP Id")),
            "remarks": _clean(_get(row, df, "Remarks")),
            "tl_approved": True,
        }
        sub_data = {k: v for k, v in sub_data.items() if v is not None}

        if _safe_insert("submissions", sub_data, dry_run):
            inserted += 1
        else:
            errors += 1

    return inserted, skipped_dup, skipped_blank, errors


@_reg("Requirements")
def migrate_requirements(df, dry_run, market_filter):
    inserted = skipped_dup = skipped_blank = errors = 0

    for idx, row in df.iterrows():
        role = _clean(_get(row, df, "Requirement", "Role", "Role Title"))
        client = _clean(_get(row, df, "Client", "Client Name"))
        if not role and not client:
            skipped_blank += 1
            continue

        location = _clean(_get(row, df, "Location"))
        market = _infer_market(client)
        if not market and location:
            market = "SG" if "singapore" in (location or "").lower() else "IN"

        if market_filter and market_filter != "both" and market != market_filter:
            skipped_blank += 1
            continue

        recruiter_raw = _clean(_get(row, df, "Recruiter", "Recruiter Name"))
        assigned = [r.strip() for r in (recruiter_raw or "").split(",") if r.strip()] or None

        contract_raw = _clean(_get(row, df, "Type", "Contract Type"))
        contract_type = None
        if contract_raw:
            ct = contract_raw.upper().strip()
            if ct in ("FTE", "TP", "C2H", "CONTRACT"):
                contract_type = ct if ct != "CONTRACT" else "Contract"

        status_raw = _clean(_get(row, df, "Status"))
        status = "open"
        if status_raw:
            sl = status_raw.lower()
            if "close" in sl:
                status = "closed"
            elif "hold" in sl:
                status = "on_hold"

        req_data = {
            "market": market or "IN",
            "client_name": client or "Unknown",
            "client_manager": _clean(_get(row, df, "Name of client Mgr",
                                          "Client Manager", "Manager's name", "Manager")),
            "role_title": role or "Open Position",
            "skillset": _clean(_get(row, df, "Skillset", "Skills")),
            "salary_budget": _clean(_get(row, df, "Budget", "Salary Budget")),
            "experience_min": _clean(_get(row, df, "Experience", "Exp", "Experience Min")),
            "notice_period": _clean(_get(row, df, "Notice Period")),
            "location": location,
            "contract_type": contract_type,
            "br_sf_id": _clean(_get(row, df, "BR/SF ID", "BR SF ID")),
            "jd_file_path": _clean(_get(row, df, "JD", "JD File")),
            "status": status,
            "assigned_recruiters": assigned,
            "bd_owner": _clean(_get(row, df, "BD name", "BD Name", "BD")),
        }
        req_data = {k: v for k, v in req_data.items() if v is not None}

        if _safe_insert("requirements", req_data, dry_run):
            inserted += 1
        else:
            errors += 1

    return inserted, skipped_dup, skipped_blank, errors


@_reg("GeBIZ")
def migrate_gebiz(df, dry_run, market_filter):
    inserted = skipped_dup = skipped_blank = errors = 0
    seen_emails = set()

    for idx, row in df.iterrows():
        name = _clean(_get(row, df, "Name", "Candidate Name"))
        if not name:
            skipped_blank += 1
            continue

        email = _clean(_get(row, df, "Candidate's email ID", "Email", "E-mail",
                            "Candidate's Email ID"))

        cand_data = {
            "name": name,
            "email": email,
            "phone": _clean_phone(_get(row, df, "Candidate's contact No.",
                                       "Contact No", "Phone")),
            "current_ctc": _clean(_get(row, df, "Current salary", "Current Salary")),
            "expected_ctc": _clean(_get(row, df, "Expected Salary", "Expected salary")),
            "preferred_location": _clean(_get(row, df, "Preferred location",
                                               "Preferred Location")),
            "market": "SG",
            "source": "excel_migration",
        }
        if email and email in seen_emails:
            skipped_dup += 1
            cand = _safe_upsert_candidate(
                {k: v for k, v in cand_data.items() if v is not None}, dry_run)
        else:
            if email:
                seen_emails.add(email)
            cand = _safe_upsert_candidate(
                {k: v for k, v in cand_data.items() if v is not None}, dry_run)

        if not cand:
            errors += 1
            continue

        # Each row can have up to 4 tender/school pairs
        tender_cols = []
        for i in range(1, 5):
            for pattern in [f"Tender no {i}", f"Tender No {i}", f"Tender no{i}",
                            f"tender no {i}", "Tender no", "Tender No"]:
                tc = _col(df, pattern if i > 1 else pattern)
                if tc:
                    tender_cols.append((tc, i))
                    break

        # Fallback: look for any column containing "tender"
        if not tender_cols:
            for c in df.columns:
                if "tender" in c.lower():
                    tender_cols.append((c, 0))

        school_cols = []
        for i in range(1, 5):
            for pattern in [f"School name {i}", f"School Name {i}",
                            f"School name{i}", "School name", "School Name"]:
                sc = _col(df, pattern if i > 1 else pattern)
                if sc:
                    school_cols.append((sc, i))
                    break
        if not school_cols:
            for c in df.columns:
                if "school" in c.lower():
                    school_cols.append((c, 0))

        # Build tender/school mapping
        school_map = {i: sc for sc, i in school_cols}
        gebiz_inserted = False

        for tc, i in tender_cols:
            tender = _clean(row.get(tc))
            if not tender:
                continue
            school_col = school_map.get(i)
            school = _clean(row.get(school_col)) if school_col else None

            gebiz_data = {
                "candidate_id": cand["id"],
                "tender_number": tender,
                "school_name": school,
                "submission_date": _parse_date(_get(row, df, "Submission Date", "Date")),
                "remarks": _clean(_get(row, df, "Remarks")),
            }
            gebiz_data = {k: v for k, v in gebiz_data.items() if v is not None}

            if _safe_insert("interview_tracker", gebiz_data, dry_run):
                inserted += 1
                gebiz_inserted = True
            else:
                errors += 1

        if not gebiz_inserted:
            # No tender found, still count the candidate
            skipped_blank += 1

    return inserted, skipped_dup, skipped_blank, errors


@_reg("SG Submission")
def migrate_sg_submission(df, dry_run, market_filter):
    """SG market submissions — same structure as master sheet."""
    return _migrate_submission_sheet(df, dry_run, "SG")


def _migrate_client_sheet(df, dry_run, client_name, market):
    """Generic handler for client-specific sheets (HCL, Tech Mahindra, Keva)."""
    inserted = skipped_dup = skipped_blank = errors = 0
    seen_emails = set()

    for idx, row in df.iterrows():
        name = _clean(_get(row, df, "Name", "Name of the Candidate",
                           "Candidate Name", "Name of Candidate"))
        if not name:
            skipped_blank += 1
            continue

        email = _clean(_get(row, df, "Email", "Email ID", "E-mail"))

        cand_data = {
            "name": name,
            "email": email,
            "phone": _clean_phone(_get(row, df, "Contact No", "Contact No.",
                                       "Mob", "Phone")),
            "cv_id": _clean(_get(row, df, "CV ID", "CVID")),
            "market": market,
            "source": "excel_migration",
        }
        if email and email in seen_emails:
            skipped_dup += 1
            continue
        if email:
            seen_emails.add(email)

        cand = _safe_upsert_candidate(
            {k: v for k, v in cand_data.items() if v is not None}, dry_run)
        if not cand:
            errors += 1
            continue

        # Insert as submission
        sub_data = {
            "candidate_id": cand["id"],
            "client_name": client_name,
            "market": market,
            "submitted_by_recruiter": _clean(_get(row, df, "Recruiter",
                                                   "Recruiter's name", "Recruiter Name")),
            "submitted_at": _parse_date(_get(row, df, "Date", "Submission Date")),
            "final_status": _map_final_status(_clean(_get(row, df, "Status",
                                                          "Final Status", "Final status"))),
            "remarks": _clean(_get(row, df, "Remarks")),
        }
        sub_data = {k: v for k, v in sub_data.items() if v is not None}

        if _safe_insert("submissions", sub_data, dry_run):
            inserted += 1
        else:
            errors += 1

    return inserted, skipped_dup, skipped_blank, errors


def _migrate_submission_sheet(df, dry_run, market):
    """Generic submission sheet migration."""
    return _migrate_client_sheet(df, dry_run, None, market)


@_reg("HCL - Non IT")
def migrate_hcl(df, dry_run, market_filter):
    return _migrate_client_sheet(df, dry_run, "HCL", "IN")


@_reg("Tech Mahindra")
def migrate_techmahindra(df, dry_run, market_filter):
    return _migrate_client_sheet(df, dry_run, "Tech Mahindra", "IN")


@_reg("Keva")
def migrate_keva(df, dry_run, market_filter):
    return _migrate_client_sheet(df, dry_run, "Keva", "IN")


@_reg("F2F GCP")
def migrate_f2f_gcp(df, dry_run, market_filter):
    inserted = skipped_dup = skipped_blank = errors = 0
    seen = set()

    for idx, row in df.iterrows():
        name = _clean(_get(row, df, "Name", "Candidate Name"))
        if not name:
            skipped_blank += 1
            continue

        email = _clean(_get(row, df, "Email", "Email ID"))
        phone = _clean_phone(_get(row, df, "Mob", "Phone", "Contact No"))

        # Deduplicate by email or phone (kosala vinayakan duplicate)
        dedup_key = email or phone
        if dedup_key and dedup_key in seen:
            skipped_dup += 1
            continue
        if dedup_key:
            seen.add(dedup_key)

        avail = _clean(_get(row, df, "Available on 8th", "Available on 8th?",
                            "Availability"))
        notes_parts = [p for p in [
            f"Available on 8th: {avail}" if avail else None,
            _clean(_get(row, df, "Remarks")),
        ] if p]

        cand_data = {
            "name": name,
            "email": email,
            "phone": phone,
            "cv_id": _clean(_get(row, df, "Serial Number", "S.No", "S. No")),
            "market": "SG",
            "source": "excel_migration",
        }
        cand_data = {k: v for k, v in cand_data.items() if v is not None}

        cand = _safe_upsert_candidate(cand_data, dry_run)
        if not cand:
            errors += 1
            continue
        inserted += 1

    return inserted, skipped_dup, skipped_blank, errors


@_reg("Flipkart")
def migrate_flipkart(df, dry_run, market_filter):
    inserted = skipped_dup = skipped_blank = errors = 0
    seen = set()

    for idx, row in df.iterrows():
        name = _clean(_get(row, df, "Name", "Candidate Name"))
        if not name:
            skipped_blank += 1
            continue

        phone = _clean_phone(_get(row, df, "Phone No.", "Phone No", "Phone",
                                   "Contact No"))
        email = _clean(_get(row, df, "Email", "Email ID"))

        dedup_key = email or phone
        if dedup_key and dedup_key in seen:
            skipped_dup += 1
            continue
        if dedup_key:
            seen.add(dedup_key)

        coming_from = _clean(_get(row, df, "Coming From"))
        licence = _clean(_get(row, df, "Licence No", "Licence No."))
        notes_parts = [p for p in [
            f"Coming from: {coming_from}" if coming_from else None,
            f"Licence: {licence}" if licence else None,
            _clean(_get(row, df, "Remarks")),
        ] if p]

        cand_data = {
            "name": name,
            "phone": phone,
            "email": email,
            "current_location": _clean(_get(row, df, "Location", "Current Location")),
            "current_job_title": _clean(_get(row, df, "Position applied",
                                              "Position", "Role")),
            "availability_date": _parse_date(_get(row, df, "Joining Date", "DOJ")),
            "market": "IN",
            "source": "excel_migration",
        }
        cand_data = {k: v for k, v in cand_data.items() if v is not None}

        cand = _safe_upsert_candidate(cand_data, dry_run)
        if not cand:
            errors += 1
            continue

        status = _map_final_status(_clean(_get(row, df, "Status", "Final Status")))
        recruiter = _clean(_get(row, df, "Recruiter"))

        sub_data = {
            "candidate_id": cand["id"],
            "client_name": "Flipkart",
            "market": "IN",
            "submitted_by_recruiter": recruiter,
            "final_status": status,
            "remarks": "; ".join(notes_parts) if notes_parts else None,
        }
        sub_data = {k: v for k, v in sub_data.items() if v is not None}

        if _safe_insert("submissions", sub_data, dry_run):
            inserted += 1
        else:
            errors += 1

    return inserted, skipped_dup, skipped_blank, errors


# ── Main ───────────────────────────────────────────────────────

def run_migration(file_path: str, sheet_name: str, dry_run: bool,
                  market_filter: str | None):
    if not Path(file_path).exists():
        print(f"Error: File not found: {file_path}")
        sys.exit(1)

    print(f"{'DRY RUN — ' if dry_run else ''}Migration: {file_path}")
    print(f"Sheet filter: {sheet_name}, Market filter: {market_filter or 'all'}")
    print()

    # Read all sheet names
    xls = pd.ExcelFile(file_path)
    available_sheets = xls.sheet_names
    print(f"Available sheets: {available_sheets}")
    print()

    # Explicit skips
    SKIP_SHEETS = {"sg database", "360f", "login credentials"}

    # Ordered processing: candidates first, then requirements, contacts, gebiz tenders
    ORDERED_SHEETS = [
        "Master submission sheet",
        "Interview Tracker Updated",
        "Onboarded Candidates",
        "HCL - Non IT",
        "GeBIZ",
        "SG Submission",
        "Tech Mahindra",
        "Keva",
        "F2F GCP",
        "Flipkart",
        "Requirements",
        "Contacts",
    ]

    if sheet_name == "all":
        # Process in defined order, then any remaining
        ordered = [s for s in ORDERED_SHEETS if s in available_sheets]
        remaining = [s for s in available_sheets
                     if s not in ordered and s.lower() not in SKIP_SHEETS]
        sheets_to_process = ordered + remaining
    else:
        if sheet_name not in available_sheets:
            # Try fuzzy match
            matches = [s for s in available_sheets if sheet_name.lower() in s.lower()]
            if matches:
                sheets_to_process = matches
                print(f"Fuzzy matched: {matches}")
            else:
                print(f"Error: Sheet '{sheet_name}' not found.")
                print(f"Available: {available_sheets}")
                sys.exit(1)
        else:
            sheets_to_process = [sheet_name]

    for sname in sheets_to_process:
        print(f"\n--- Processing: {sname} ---")

        if sname.lower() in SKIP_SHEETS:
            print(f"  [SKIP] Explicitly skipped: '{sname}'")
            report.add(sname, 0, 0, 0, 0, 0)
            continue

        # Find handler
        handler = None
        for handler_name, handler_fn in SHEET_HANDLERS.items():
            if handler_name.lower() == sname.lower():
                handler = handler_fn
                break
        if not handler:
            # Fuzzy match
            for handler_name, handler_fn in SHEET_HANDLERS.items():
                if handler_name.lower() in sname.lower() or sname.lower() in handler_name.lower():
                    handler = handler_fn
                    print(f"  Matched handler: {handler_name}")
                    break

        if not handler:
            print(f"  [SKIP] No handler for sheet '{sname}'")
            report.add(sname, 0, 0, 0, 0, 0)
            continue

        try:
            df = pd.read_excel(file_path, sheet_name=sname)
        except Exception as e:
            print(f"  [ERR] Could not read sheet: {e}")
            report.add(sname, 0, 0, 0, 0, 1)
            continue

        rows_read = len(df)
        print(f"  Rows: {rows_read}")
        print(f"  Columns: {list(df.columns)}")

        if dry_run and rows_read > 0:
            print(f"\n  First 5 rows preview:")
            print(df.head(5).to_string(index=False, max_colwidth=30))
            print()

        try:
            inserted, skipped_dup, skipped_blank, errors = handler(
                df, dry_run, market_filter)
        except Exception as e:
            print(f"  [ERR] Handler failed: {e}")
            import traceback
            traceback.print_exc()
            report.add(sname, rows_read, 0, 0, 0, 1)
            continue

        print(f"  Result: {inserted} inserted, {skipped_dup} dup, "
              f"{skipped_blank} blank, {errors} errors")
        report.add(sname, rows_read, inserted, skipped_dup, skipped_blank, errors)

    report.print_summary()

    report_path = str(Path(__file__).parent /
                       f"migration_report_{datetime.now().strftime('%Y%m%d')}.txt")
    report.save(report_path)
    print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate ExcelTech submission tracker Excel to Supabase")
    parser.add_argument("--file", required=True, help="Path to Excel file")
    parser.add_argument("--sheet", default="all",
                        help="Sheet name to migrate (or 'all')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without inserting")
    parser.add_argument("--market", default=None, choices=["IN", "SG", "both"],
                        help="Filter by market")
    args = parser.parse_args()

    run_migration(args.file, args.sheet, args.dry_run, args.market)
