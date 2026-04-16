"""ExcelTech Supabase database helpers."""

import os
from datetime import datetime, timezone
from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
    return _client


# ── Helpers ────────────────────────────────────────────────

import re

def _normalise_skills(skills: list) -> list[str]:
    """Split composite skill strings into individual skills.
    e.g. ["ServiceNow JavaScript ITSM", "CSS"] -> ["ServiceNow", "JavaScript", "ITSM", "CSS"]
    """
    out = []
    seen = set()
    for entry in skills:
        if not isinstance(entry, str):
            continue
        # Split on commas, semicolons, pipes, or 2+ spaces first
        parts = re.split(r"[,;|]|\s{2,}", entry)
        for part in parts:
            # Split remaining multi-word entries on single spaces
            # but keep known multi-word skills intact
            tokens = part.strip().split()
            for token in tokens:
                token = token.strip()
                if token and token.lower() not in seen:
                    seen.add(token.lower())
                    out.append(token)
    return out


# ── Candidates ──────────────────────────────────────────────

def insert_candidate(data: dict) -> dict:
    return get_client().table("candidates").insert(data).execute().data[0]


def get_candidate_by_id(candidate_id: str) -> dict | None:
    rows = (get_client().table("candidates")
            .select("*").eq("id", candidate_id).execute().data)
    return rows[0] if rows else None


def upsert_candidate_by_email(data: dict) -> dict:
    if "skills" in data and isinstance(data["skills"], list):
        data["skills"] = _normalise_skills(data["skills"])
    return (get_client().table("candidates")
            .upsert(data, on_conflict="email").execute().data[0])


def upsert_candidate_by_name(data: dict) -> dict:
    """Insert a candidate that has no email (e.g. from Foundit recruiter search).
    Check for existing record by name + source to avoid duplicates."""
    if "skills" in data and isinstance(data["skills"], list):
        data["skills"] = _normalise_skills(data["skills"])
    name = data.get("name", "").strip()
    if not name:
        return {}
    existing = (get_client().table("candidates")
                .select("id")
                .eq("name", name)
                .eq("source", data.get("source", ""))
                .execute().data)
    if existing:
        return (get_client().table("candidates")
                .update(data).eq("id", existing[0]["id"])
                .execute().data[0])
    return get_client().table("candidates").insert(data).execute().data[0]


def search_candidates_by_skill(skills: list[str], market: str | None = None):
    """Legacy keyword overlap search — kept for backward compat but no longer
    used for internal matching.  Use search_candidates_broad() + LLM scoring."""
    skills = _normalise_skills(skills)
    q = get_client().table("candidates").select(
        "id, name, email, phone, skills, total_experience, current_location, "
        "current_job_title, market"
    ).overlaps("skills", skills)
    if market:
        q = q.eq("market", market)
    return q.execute().data


def search_candidates_broad(market: str | None = None,
                            location: str | None = None,
                            limit: int = 200) -> list[dict]:
    """Fetch candidates in a broad category for LLM-based matching.
    Filters by market (required) and optionally location prefix.
    Returns up to `limit` candidates ordered by most recently added."""
    q = get_client().table("candidates").select(
        "id, name, email, phone, skills, total_experience, "
        "current_location, current_job_title, current_employer, market"
    )
    if market:
        q = q.eq("market", market)
    if location:
        q = q.ilike("current_location", f"%{location}%")
    return q.order("created_at", desc=True).limit(limit).execute().data


# ── Match Scores (LLM semantic matching cache) ────────────────

def get_cached_match_scores(requirement_id: str,
                            candidate_ids: list[str]) -> dict[str, dict]:
    """Return {candidate_id: {score, reasoning}} for already-scored pairs."""
    if not candidate_ids:
        return {}
    rows = (get_client().table("match_scores")
            .select("candidate_id, score, reasoning")
            .eq("requirement_id", requirement_id)
            .in_("candidate_id", candidate_ids)
            .execute().data)
    return {r["candidate_id"]: {"score": r["score"], "reasoning": r["reasoning"]}
            for r in rows}


def upsert_match_scores(requirement_id: str,
                        scores: list[dict]) -> list[dict]:
    """Batch upsert match scores.  Each dict needs candidate_id, score, reasoning."""
    rows = [{
        "requirement_id": requirement_id,
        "candidate_id": s["candidate_id"],
        "score": s["score"],
        "reasoning": s["reasoning"],
        "scored_at": datetime.now(timezone.utc).isoformat(),
    } for s in scores]
    return (get_client().table("match_scores")
            .upsert(rows, on_conflict="candidate_id,requirement_id")
            .execute().data)


def get_match_scores_above(requirement_id: str,
                           min_score: int = 60) -> list[dict]:
    """Get all match scores for a requirement above threshold, sorted desc."""
    return (get_client().table("match_scores")
            .select("candidate_id, score, reasoning")
            .eq("requirement_id", requirement_id)
            .gte("score", min_score)
            .order("score", desc=True)
            .execute().data)


def count_matched_candidates(requirement_id: str,
                             min_score: int = 0) -> int:
    """Count candidates scored for a requirement (for pipeline summary)."""
    result = (get_client().table("match_scores")
              .select("id", count="exact")
              .eq("requirement_id", requirement_id)
              .gte("score", min_score)
              .execute())
    return result.count or 0


# ── Requirements ────────────────────────────────────────────

def insert_requirement(data: dict) -> dict:
    return get_client().table("requirements").insert(data).execute().data[0]


def get_open_requirements(market: str | None = None, created_after: str | None = None):
    q = get_client().table("requirements").select(
        "id, market, client_name, role_title, skillset, skills_required, "
        "experience_min, salary_budget, location, contract_type, status, "
        "assigned_recruiters, created_at"
    ).eq("status", "open")
    if market:
        q = q.eq("market", market)
    if created_after:
        q = q.gte("created_at", created_after)
    return q.order("created_at", desc=True).execute().data


def get_requirement_by_id(requirement_id: str) -> dict | None:
    rows = (get_client().table("requirements")
            .select("*").eq("id", requirement_id).execute().data)
    return rows[0] if rows else None


def assign_recruiter_to_requirement(req_id: str, email: str) -> dict:
    req = get_requirement_by_id(req_id)
    if not req:
        raise ValueError(f"Requirement {req_id} not found")
    current = req.get("assigned_recruiters") or []
    if email not in current:
        current.append(email)
    return (get_client().table("requirements")
            .update({"assigned_recruiters": current})
            .eq("id", req_id).execute().data[0])


# ── Screenings ──────────────────────────────────────────────

def insert_screening(data: dict) -> dict:
    return get_client().table("screenings").insert(data).execute().data[0]


def get_shortlisted(requirement_id: str, min_score: int = 7):
    return (get_client().table("screenings")
            .select("id, candidate_id, score, skills_match_pct, "
                    "recommendation, reasoning, screened_at")
            .eq("requirement_id", requirement_id)
            .gte("score", min_score)
            .order("score", desc=True)
            .execute().data)


# ── Candidate Details ───────────────────────────────────────

def upsert_candidate_details(candidate_id: str, requirement_id: str,
                             data: dict) -> dict:
    data["candidate_id"] = candidate_id
    data["requirement_id"] = requirement_id
    return (get_client().table("candidate_details")
            .upsert(data, on_conflict="candidate_id,requirement_id")
            .execute().data[0])


def get_candidate_details(candidate_id: str,
                          requirement_id: str) -> dict | None:
    rows = (get_client().table("candidate_details")
            .select("*")
            .eq("candidate_id", candidate_id)
            .eq("requirement_id", requirement_id)
            .execute().data)
    return rows[0] if rows else None


# ── Outreach ────────────────────────────────────────────────

def insert_outreach_log(data: dict) -> dict:
    return get_client().table("outreach_log").insert(data).execute().data[0]


def mark_reply_received(outreach_log_id: str) -> dict:
    return (get_client().table("outreach_log")
            .update({"reply_received": True,
                     "replied_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", outreach_log_id).execute().data[0])


def get_pending_replies(recruiter_email: str):
    return (get_client().table("outreach_log")
            .select("id, candidate_id, requirement_id, email_subject, outlook_thread_id, sent_at")
            .eq("recruiter_email", recruiter_email)
            .eq("reply_received", False)
            .order("sent_at", desc=True)
            .execute().data)


# ── Submissions ─────────────────────────────────────────────

def insert_submission(data: dict) -> dict:
    return get_client().table("submissions").insert(data).execute().data[0]


def update_submission_status(submission_id: str, status: str) -> dict:
    return (get_client().table("submissions")
            .update({"final_status": status})
            .eq("id", submission_id).execute().data[0])


def tl_approve_submission(submission_id: str) -> dict:
    return (get_client().table("submissions")
            .update({"tl_approved": True,
                     "tl_approved_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", submission_id).execute().data[0])


def get_pipeline_summary(market: str | None = None):
    q = get_client().table("submissions").select(
        "id, candidate_id, requirement_id, client_name, market, "
        "final_status, placement_type, submitted_at, tl_approved"
    )
    if market:
        q = q.eq("market", market)
    return q.order("submitted_at", desc=True).execute().data


# ── GeBIZ ───────────────────────────────────────────────────

def insert_gebiz_submission(candidate_id: str, tender_number: str,
                            school_name: str | None = None) -> dict:
    data = {"candidate_id": candidate_id, "tender_number": tender_number}
    if school_name:
        data["school_name"] = school_name
    return get_client().table("gebiz_submissions").insert(data).execute().data[0]


def get_gebiz_by_candidate(candidate_id: str):
    return (get_client().table("gebiz_submissions")
            .select("id, tender_number, school_name, submission_date, "
                    "rechecking_date, status, remarks")
            .eq("candidate_id", candidate_id)
            .execute().data)
