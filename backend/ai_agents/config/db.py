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


def get_open_requirements(market: str | None = None, created_after: str | None = None,
                          project_id: str | None = None):
    q = get_client().table("requirements").select(
        "id, market, client_name, role_title, skillset, skills_required, "
        "experience_min, salary_budget, location, contract_type, status, "
        "assigned_recruiters, created_at, project_id"
    ).eq("status", "open")
    if market:
        q = q.eq("market", market)
    if created_after:
        q = q.gte("created_at", created_after)
    if project_id:
        q = q.eq("project_id", project_id)
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


# ── Admin: wipe all requirements (+ dependents) ──────────────

def wipe_all_requirements() -> dict[str, int]:
    """Hard-delete every requirement and every row that references one.

    FKs on requirements don't cascade, so children must go first. Candidates,
    projects, interview_tracker etc. are untouched. Returns per-table delete
    counts for the caller to display.
    """
    client = get_client()
    counts: dict[str, int] = {}
    # Use a dummy value "not-null-sentinel" — supabase-py requires a filter
    # on delete(), so we use `neq` against an impossible id to match all rows.
    IMPOSSIBLE_ID = "00000000-0000-0000-0000-000000000000"
    for table in ("submissions", "outreach_log",
                  "match_scores", "candidate_details", "screenings"):
        try:
            res = (client.table(table).delete()
                   .neq("id", IMPOSSIBLE_ID).execute())
            counts[table] = len(res.data or [])
        except Exception:
            counts[table] = -1  # failed; caller will see it in the summary
    try:
        res = (client.table("requirements").delete()
               .neq("id", IMPOSSIBLE_ID).execute())
        counts["requirements"] = len(res.data or [])
    except Exception:
        counts["requirements"] = -1
    return counts


# ── Shortlists + Notes (Phase 3) ────────────────────────────

def toggle_shortlist(candidate_id: str, user_email: str,
                     note: str | None = None) -> dict:
    """Add or remove a candidate from a user's shortlist.

    Returns {status: 'added' | 'removed', row: <dict>}.
    """
    client = get_client()
    existing = (client.table("candidate_shortlists")
                .select("id")
                .eq("candidate_id", candidate_id)
                .eq("user_email", user_email)
                .execute().data)
    if existing:
        client.table("candidate_shortlists").delete().eq("id", existing[0]["id"]).execute()
        return {"status": "removed", "id": existing[0]["id"]}
    payload = {"candidate_id": candidate_id, "user_email": user_email}
    if note:
        payload["note"] = note
    row = (client.table("candidate_shortlists")
           .insert(payload).execute().data[0])
    return {"status": "added", "row": row}


def is_shortlisted(candidate_id: str, user_email: str) -> bool:
    rows = (get_client().table("candidate_shortlists")
            .select("id")
            .eq("candidate_id", candidate_id)
            .eq("user_email", user_email)
            .execute().data)
    return bool(rows)


def list_shortlists_for_user(user_email: str) -> list[dict]:
    """Return shortlist rows with joined candidate columns."""
    rows = (get_client().table("candidate_shortlists")
            .select("id, candidate_id, note, created_at, "
                    "candidates(id, name, email, phone, skills, "
                    "total_experience, current_location, current_job_title, "
                    "current_employer, market, linkedin_url)")
            .eq("user_email", user_email)
            .order("created_at", desc=True)
            .execute().data)
    return rows


def delete_shortlists(user_email: str, shortlist_ids: list[str]) -> int:
    if not shortlist_ids:
        return 0
    res = (get_client().table("candidate_shortlists")
           .delete()
           .in_("id", shortlist_ids)
           .eq("user_email", user_email)
           .execute())
    return len(res.data or [])


def add_candidate_note(candidate_id: str, user_email: str, content: str) -> dict:
    return (get_client().table("candidate_notes")
            .insert({"candidate_id": candidate_id,
                     "user_email": user_email,
                     "content": content})
            .execute().data[0])


def list_candidate_notes(candidate_id: str) -> list[dict]:
    return (get_client().table("candidate_notes")
            .select("id, user_email, content, created_at")
            .eq("candidate_id", candidate_id)
            .order("created_at", desc=True)
            .execute().data)


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


def get_pipeline_summary(market: str | None = None, project_id: str | None = None):
    q = get_client().table("submissions").select(
        "id, candidate_id, requirement_id, client_name, market, "
        "final_status, placement_type, submitted_at, tl_approved"
    )
    if market:
        q = q.eq("market", market)
    if project_id:
        # Submissions don't carry project_id directly — filter via the requirement FK.
        req_ids = [r["id"] for r in (get_client().table("requirements")
                                     .select("id")
                                     .eq("project_id", project_id)
                                     .execute().data)]
        if not req_ids:
            return []
        q = q.in_("requirement_id", req_ids)
    return q.order("submitted_at", desc=True).execute().data


# ── Interview tracker ───────────────────────────────────────

def insert_interview_tracker(data: dict) -> dict:
    return get_client().table("interview_tracker").insert(data).execute().data[0]


# ── Projects ────────────────────────────────────────────────

def insert_project(data: dict) -> dict:
    return get_client().table("projects").insert(data).execute().data[0]


def list_projects_for_user(user_email: str) -> list[dict]:
    """Return every project the given user can see:
      - projects they created, OR
      - projects with access_level='shared', OR
      - projects where they're listed in project_collaborators.
    De-duped by id. Ordered newest first.
    """
    client = get_client()
    owned = (client.table("projects").select("*")
             .eq("created_by", user_email).execute().data)
    shared = (client.table("projects").select("*")
              .eq("access_level", "shared").execute().data)
    collab_rows = (client.table("project_collaborators").select("project_id")
                   .eq("user_email", user_email).execute().data)
    collab_ids = [r["project_id"] for r in collab_rows]
    collab = (client.table("projects").select("*")
              .in_("id", collab_ids).execute().data) if collab_ids else []
    seen, out = set(), []
    for p in owned + shared + collab:
        if p["id"] not in seen:
            seen.add(p["id"])
            out.append(p)
    out.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return out


def get_project(project_id: str) -> dict | None:
    rows = (get_client().table("projects").select("*")
            .eq("id", project_id).execute().data)
    return rows[0] if rows else None


def insert_project_collaborators(project_id: str, emails: list[str]) -> None:
    """Upsert a list of collaborator emails for a project.  No-op on empty list."""
    if not emails:
        return
    rows = [{"project_id": project_id, "user_email": e} for e in emails]
    (get_client().table("project_collaborators")
     .upsert(rows, on_conflict="project_id,user_email").execute())


def get_project_collaborators(project_id: str) -> list[str]:
    rows = (get_client().table("project_collaborators").select("user_email")
            .eq("project_id", project_id).execute().data)
    return [r["user_email"] for r in rows]


def get_all_requirements_for_project(project_id: str) -> list[dict]:
    """All requirements (any status) for a project — used for progress calc."""
    return (get_client().table("requirements")
            .select("id, status")
            .eq("project_id", project_id)
            .execute().data)


def update_project(project_id: str, patch: dict) -> dict:
    """Patch any subset of {title, access_level, status}.  Returns updated row."""
    return (get_client().table("projects").update(patch)
            .eq("id", project_id).execute().data[0])


def delete_project(project_id: str) -> None:
    """Hard-delete a project.
    - Null out requirements.project_id first (defensive — FK is nullable, but
      this is explicit and doesn't rely on DB behaviour).
    - project_collaborators has ON DELETE CASCADE in the schema.
    """
    (get_client().table("requirements").update({"project_id": None})
     .eq("project_id", project_id).execute())
    (get_client().table("projects").delete()
     .eq("id", project_id).execute())


def clear_project_collaborators(project_id: str) -> None:
    (get_client().table("project_collaborators").delete()
     .eq("project_id", project_id).execute())


# ── Sequences v2 ────────────────────────────────────────────

def insert_sequence(data: dict) -> dict:
    return get_client().table("sequences").insert(data).execute().data[0]


def insert_sequence_steps(steps: list[dict]) -> list[dict]:
    if not steps:
        return []
    return get_client().table("sequence_steps").insert(steps).execute().data


def get_sequence_full(seq_id: str) -> dict | None:
    rows = (get_client().table("sequences").select("*")
            .eq("id", seq_id).execute().data)
    if not rows:
        return None
    seq = rows[0]
    seq["steps"] = (get_client().table("sequence_steps").select("*")
                    .eq("sequence_id", seq_id)
                    .order("position").execute().data) or []
    return seq


def list_sequences_for_user(created_by: str, role: str) -> list[dict]:
    q = get_client().table("sequences").select("*")
    if role != "tl":
        q = q.eq("created_by", created_by)
    q = q.neq("status", "archived")
    return q.order("created_at", desc=True).execute().data or []


def update_sequence_row(seq_id: str, patch: dict) -> dict:
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    return (get_client().table("sequences").update(patch)
            .eq("id", seq_id).execute().data[0])


def update_step_row(step_id: str, patch: dict) -> dict:
    return (get_client().table("sequence_steps").update(patch)
            .eq("id", step_id).execute().data[0])


def archive_sequence(seq_id: str) -> dict:
    return update_sequence_row(seq_id, {"status": "archived"})


def count_sequence_metrics(seq_id: str) -> dict:
    """Return {active, replied, total, sent} run counts for one sequence."""
    runs = (get_client().table("sequence_runs").select("id, status")
            .eq("sequence_id", seq_id).execute().data) or []
    total = len(runs)
    active = sum(1 for r in runs if r["status"] == "active")
    replied = sum(1 for r in runs if r["status"] == "replied")
    # Count total sent step_sends
    run_ids = [r["id"] for r in runs]
    sent = 0
    if run_ids:
        sends = (get_client().table("sequence_step_sends").select("id")
                 .in_("run_id", run_ids).eq("status", "sent").execute().data) or []
        sent = len(sends)
    return {"total": total, "active": active, "replied": replied, "sent": sent}


def get_pending_replies(recruiter_email: str):
    return (get_client().table("outreach_log")
            .select("id, candidate_id, requirement_id, email_subject, "
                    "outlook_thread_id, sent_at, sequence_run_id")
            .eq("recruiter_email", recruiter_email)
            .eq("reply_received", False)
            .order("sent_at", desc=True)
            .execute().data)
