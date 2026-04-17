"""ExcelTech AI agent core — business logic merged into Flask.

Previously this lived in ``backend/ai-agents/main.py`` as a separate FastAPI
service that Flask proxied to over HTTP. The split existed only for local-dev
ergonomics; in production only Flask ever ran, so every write route returned
502s. This module contains the same logic as native Python functions that the
Flask app imports and calls directly.

Public API
----------
- ``init()`` — call once at Flask startup to load agent prompts and the
  Anthropic client. Safe to call multiple times (idempotent).
- Handler functions (``create_requirement``, ``source_requirement`` etc.) —
  called from Flask routes. They raise ``CoreError(status, message)`` on
  user-facing failures which Flask translates to jsonify + status code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from .config import db, outlook, sourcing, search_parser, market_intelligence

log = logging.getLogger(__name__)

# ── Module state (populated by init()) ─────────────────────────

AGENTS: dict[str, str] = {}
SKILLS: dict[str, str] = {}
_claude: anthropic.Anthropic | None = None
_initialized = False
_init_lock = threading.Lock()

LOG_DIR = Path(__file__).parent / "logs"


# ── Errors ─────────────────────────────────────────────────────

class CoreError(Exception):
    """User-facing error with HTTP status code. Flask layer converts to JSON."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


# ── Init ───────────────────────────────────────────────────────

def _load_md_files(directory: str) -> dict[str, str]:
    result = {}
    md_dir = Path(__file__).parent / directory
    if not md_dir.exists():
        return result
    for f in md_dir.glob("*.md"):
        result[f.stem] = f.read_text()
    return result


def init() -> None:
    """Load agent prompts, Anthropic client, and verify Supabase. Idempotent."""
    global AGENTS, SKILLS, _claude, _initialized
    with _init_lock:
        if _initialized:
            return
        AGENTS = _load_md_files("agents")
        SKILLS = _load_md_files("skills")
        log.info("[ai_core] Loaded %d agents, %d skills", len(AGENTS), len(SKILLS))

        try:
            _claude = anthropic.Anthropic()
        except Exception as e:
            log.warning("[ai_core] Anthropic client init failed: %s", e)
            _claude = None

        LOG_DIR.mkdir(exist_ok=True)
        _initialized = True


# ── Auth helper ────────────────────────────────────────────────

def _require_role(role: str, allowed: list[str]) -> None:
    if role not in allowed:
        raise CoreError(403, f"Role '{role}' not allowed. Need: {allowed}")


# ── Async helper ───────────────────────────────────────────────

def _run_async(coro):
    """Run an async coroutine from sync Flask code.

    Each call gets its own event loop — fine for the request-per-worker model
    of gunicorn. For long-running background work we spawn a thread whose
    target owns its own event loop.
    """
    return asyncio.run(coro)


def _run_in_background(target, *args, **kwargs) -> None:
    """Fire-and-forget equivalent of FastAPI BackgroundTasks.add_task.

    ``target`` may be a regular function or an async coroutine function — we
    detect at runtime and run the coroutine in a fresh event loop in the
    spawned thread.
    """

    def _runner():
        try:
            result = target(*args, **kwargs)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception:
            log.exception("[ai_core] background task %s failed", target.__name__)

    threading.Thread(target=_runner, daemon=True).start()


# ── LLM helpers ────────────────────────────────────────────────

def _log_tokens(endpoint: str, model: str, input_tokens: int,
                output_tokens: int) -> None:
    log_file = LOG_DIR / f"tokens_{datetime.now().strftime('%Y%m%d')}.log"
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        with open(log_file, "a") as f:
            f.write(f"{ts}|{endpoint}|{model}|{input_tokens}|{output_tokens}\n")
    except Exception:
        log.exception("Failed to log token usage")


def _call_claude(model: str, system: str, user_msg: str,
                 max_tokens: int = 2048, endpoint: str = "unknown") -> str:
    if _claude is None:
        raise CoreError(500, "Anthropic client not initialized — check ANTHROPIC_API_KEY")
    resp = _claude.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    _log_tokens(endpoint, model, resp.usage.input_tokens, resp.usage.output_tokens)
    return resp.content[0].text


def _parse_llm_json(text: str) -> dict | list | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


# ── Market normalization ───────────────────────────────────────

MARKET_MAP = {"india": "IN", "singapore": "SG", "in": "IN", "sg": "SG"}


def _normalize_market(market: str | None) -> str | None:
    if not market:
        return market
    return MARKET_MAP.get(market.lower(), market)


# ── Matching ───────────────────────────────────────────────────

MATCH_BATCH_SIZE = 20
MATCH_MIN_SCORE = 60


def _build_requirement_summary(requirement: dict) -> str:
    parts = [
        f"Role: {requirement.get('role_title', 'N/A')}",
        f"Client: {requirement.get('client_name', 'N/A')}",
        f"Skills: {', '.join(requirement.get('skills_required', []))}",
    ]
    if requirement.get("skillset"):
        parts.append(f"Skillset description: {requirement['skillset']}")
    if requirement.get("experience_min") is not None:
        parts.append(f"Min experience: {requirement['experience_min']}")
    if requirement.get("salary_budget"):
        parts.append(f"Salary budget: {requirement['salary_budget']}")
    if requirement.get("location"):
        parts.append(f"Location: {requirement['location']}")
    if requirement.get("contract_type"):
        parts.append(f"Contract: {requirement['contract_type']}")
    if requirement.get("notice_period"):
        parts.append(f"Notice period: {requirement['notice_period']}")
    return "\n".join(parts)


def _build_candidate_summary(cand: dict, idx: int) -> str:
    skills = cand.get("skills") or []
    return (
        f"[{idx}] ID: {cand['id']}\n"
        f"  Name: {cand.get('name', 'N/A')}\n"
        f"  Skills: {', '.join(skills) if skills else 'N/A'}\n"
        f"  Experience: {cand.get('total_experience', 'N/A')}\n"
        f"  Location: {cand.get('current_location', 'N/A')}\n"
        f"  Current role: {cand.get('current_job_title', 'N/A')} "
        f"at {cand.get('current_employer', 'N/A')}"
    )


def _score_candidate_batch(candidates: list[dict], requirement: dict) -> list[dict]:
    req_summary = _build_requirement_summary(requirement)
    cand_block = "\n\n".join(
        _build_candidate_summary(c, i + 1) for i, c in enumerate(candidates)
    )
    prompt = (
        f"You are a recruitment matching engine. Score each candidate 0-100 "
        f"for fit against this requirement. Consider skill synonyms "
        f"(React = ReactJS, .NET = dotnet), transferable experience, "
        f"seniority level, and location fit.\n\n"
        f"REQUIREMENT:\n{req_summary}\n\n"
        f"CANDIDATES:\n{cand_block}\n\n"
        f"Return a JSON array. Each element must have:\n"
        f"- \"candidate_id\": the exact ID string from above\n"
        f"- \"score\": integer 0-100\n"
        f"- \"reasoning\": one sentence explaining the score\n\n"
        f"Return ONLY the JSON array, no other text."
    )
    result_text = _call_claude(
        "claude-sonnet-4-20250514",
        "You are an expert IT recruitment matcher. Score candidates accurately. "
        "Understand skill synonyms, related technologies, and experience levels.",
        prompt, max_tokens=4096, endpoint="/match-scoring",
    )
    parsed = _parse_llm_json(result_text)
    if not isinstance(parsed, list):
        return []
    valid = []
    cand_ids = {c["id"] for c in candidates}
    for entry in parsed:
        if (isinstance(entry, dict)
                and entry.get("candidate_id") in cand_ids
                and isinstance(entry.get("score"), (int, float))
                and 0 <= entry["score"] <= 100):
            valid.append({
                "candidate_id": entry["candidate_id"],
                "score": int(entry["score"]),
                "reasoning": str(entry.get("reasoning", "")),
            })
    return valid


def _match_candidates_for_requirement(requirement: dict) -> list[dict]:
    requirement_id = requirement["id"]
    all_candidates = db.search_candidates_broad(
        market=requirement.get("market"), limit=200,
    )
    if not all_candidates:
        return []
    all_ids = [c["id"] for c in all_candidates]
    cached = db.get_cached_match_scores(requirement_id, all_ids)
    to_score = [c for c in all_candidates if c["id"] not in cached]
    new_scores = []
    for i in range(0, len(to_score), MATCH_BATCH_SIZE):
        batch = to_score[i:i + MATCH_BATCH_SIZE]
        new_scores.extend(_score_candidate_batch(batch, requirement))
    if new_scores:
        db.upsert_match_scores(requirement_id, new_scores)
    all_scores: dict[str, dict] = {}
    for cid, data in cached.items():
        all_scores[cid] = data
    for s in new_scores:
        all_scores[s["candidate_id"]] = {"score": s["score"], "reasoning": s["reasoning"]}
    matched = [
        {"candidate_id": cid, **data}
        for cid, data in all_scores.items()
        if data["score"] >= MATCH_MIN_SCORE
    ]
    matched.sort(key=lambda x: x["score"], reverse=True)
    return matched


# ── Screening ──────────────────────────────────────────────────

def _screen_candidates(requirement_id: str, requirement: dict,
                       just_sourced: list[dict]) -> None:
    skill_matched = db.search_candidates_by_skill(
        requirement.get("skills_required", []), requirement.get("market"),
    )
    seen_ids = set()
    candidates = []
    for cand in just_sourced + skill_matched:
        if cand["id"] not in seen_ids:
            seen_ids.add(cand["id"])
            candidates.append(cand)
    log.info("Screening %d candidates for requirement %s",
             len(candidates), requirement_id)
    for cand in candidates:
        existing = (db.get_client().table("screenings")
                    .select("id")
                    .eq("candidate_id", cand["id"])
                    .eq("requirement_id", requirement_id)
                    .execute().data)
        if existing:
            continue
        try:
            result_text = _call_claude(
                "claude-sonnet-4-20250514",
                AGENTS.get("screener", ""),
                f"Score this candidate.\nCANDIDATE: {cand}\n"
                f"REQUIREMENT: {requirement}\nReturn JSON only.",
                max_tokens=1024, endpoint="/source-and-screen",
            )
            screening = json.loads(result_text)
        except (json.JSONDecodeError, Exception) as e:
            log.error("Screening error for %s: %s",
                      cand.get("name", cand["id"]), e)
            continue
        screening["candidate_id"] = cand["id"]
        screening["requirement_id"] = requirement_id
        db.insert_screening(screening)


async def _run_source_and_screen(requirement_id: str) -> dict:
    requirement = db.get_requirement_by_id(requirement_id)
    if not requirement:
        return {"error": "Requirement not found"}
    source_results = await sourcing.run_all_sources(requirement)
    linkedin_str = sourcing.generate_linkedin_search_string(requirement)
    just_sourced = source_results.pop("upserted_candidates", [])
    matched = _match_candidates_for_requirement(requirement)
    screened = shortlisted = 0
    top_candidates: list[dict] = []
    for match in matched:
        cand = db.get_candidate_by_id(match["candidate_id"])
        if not cand:
            continue
        existing = (db.get_client().table("screenings")
                    .select("id")
                    .eq("candidate_id", cand["id"])
                    .eq("requirement_id", requirement_id)
                    .execute().data)
        if existing:
            continue
        result_text = _call_claude(
            "claude-sonnet-4-20250514",
            AGENTS.get("screener", ""),
            f"Score this candidate.\nCANDIDATE: {cand}\n"
            f"REQUIREMENT: {requirement}\nReturn JSON only.",
            max_tokens=1024, endpoint="/source-and-screen",
        )
        try:
            screening = json.loads(result_text)
        except json.JSONDecodeError:
            log.error("Screening parse error for %s: %s",
                      cand.get("name", cand["id"]), result_text[:200])
            continue
        screening["candidate_id"] = cand["id"]
        screening["requirement_id"] = requirement_id
        db.insert_screening(screening)
        screened += 1
        if screening.get("recommendation") == "shortlist":
            shortlisted += 1
            top_candidates.append({
                "candidate_id": cand["id"],
                "match_score": match["score"],
                "match_reasoning": match["reasoning"],
                "screening_score": screening.get("score"),
                "recommendation": screening.get("recommendation"),
                "reasoning": screening.get("reasoning"),
            })
    top_candidates.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    return {
        "sourced": source_results.get("total_unique", 0),
        "matched": len(matched),
        "screened": screened,
        "shortlisted": shortlisted,
        "linkedin_search_string": linkedin_str,
        "top_candidates": top_candidates[:10],
    }


# ── Inbox ──────────────────────────────────────────────────────

async def _run_process_inbox(recruiter_email: str | None) -> dict:
    if recruiter_email:
        recruiter_list = [recruiter_email]
    else:
        team_emails = os.environ.get("RECRUITER_EMAILS", "")
        recruiter_list = [e.strip() for e in team_emails.split(",") if e.strip()]
    totals = {"processed": 0, "candidate_replies": 0,
              "new_requirements_flagged": 0, "chase_drafts_pending": 0, "errors": 0}
    for email in recruiter_list:
        try:
            unread = outlook.get_unread_emails(email, hours_back=24)
        except Exception:
            totals["errors"] += 1
            continue
        for msg in unread:
            totals["processed"] += 1
            classification = _call_claude(
                "claude-haiku-4-5-20251001",
                "Classify this email as exactly one of: "
                "candidate_reply, new_requirement, other. "
                "Return only the classification word.",
                f"FROM: {msg['sender_email']}\n"
                f"SUBJECT: {msg['subject']}\n"
                f"BODY: {msg['body_text'][:500]}",
                max_tokens=20, endpoint="/inbox/classify",
            ).strip().lower()
            if "candidate_reply" in classification:
                totals["candidate_replies"] += 1
                try:
                    pending = db.get_pending_replies(email)
                    matched = next(
                        (p for p in pending
                         if p.get("outlook_thread_id") == msg["thread_id"]),
                        None,
                    )
                    if matched:
                        db.mark_reply_received(matched["id"])
                        result = _call_claude(
                            "claude-haiku-4-5-20251001",
                            AGENTS.get("followup", ""),
                            f"Parse reply:\n{msg['body_text']}",
                            max_tokens=2048, endpoint="/inbox/parse-reply",
                        )
                        try:
                            parsed = json.loads(result)
                            if parsed.get("fields_filled"):
                                update = parsed["fields_filled"]
                                update["status"] = parsed.get(
                                    "status", "details_received")
                                db.upsert_candidate_details(
                                    matched["candidate_id"],
                                    matched["requirement_id"], update)
                            if parsed.get("chase_draft"):
                                totals["chase_drafts_pending"] += 1
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    totals["errors"] += 1
            elif "new_requirement" in classification:
                totals["new_requirements_flagged"] += 1
            if ("candidate_reply" in classification
                    or "new_requirement" in classification):
                try:
                    outlook.mark_as_read(email, msg["message_id"])
                except Exception:
                    pass
    return totals


# ═══════════════════════════════════════════════════════════════
# Public handlers — called from Flask routes
# ═══════════════════════════════════════════════════════════════

# ── Search ─────────────────────────────────────────────────────

def parse_search(payload: dict) -> dict:
    requirement_text = payload.get("requirement_text") if isinstance(payload, dict) else None
    if not requirement_text or not isinstance(requirement_text, str):
        raise CoreError(422, "requirement_text (string) is required")
    try:
        result = search_parser.parse_search_query(
            requirement_text=requirement_text,
            call_claude_fn=_call_claude,
            parse_json_fn=_parse_llm_json,
        )
        return {"status": "ok", "parsed": result}
    except ValueError as e:
        raise CoreError(422, str(e))


SEARCH_RESULT_LIMIT = 30


def _exp_years(cand: dict) -> float | None:
    """Parse a candidate's total_experience text ('5 years', '3+', '7.5 yrs') to float."""
    raw = cand.get("total_experience")
    if not raw:
        return None
    import re
    m = re.search(r"(\d+(?:\.\d+)?)", str(raw))
    return float(m.group(1)) if m else None


def _apply_python_filters(candidates: list[dict], filters: dict) -> list[dict]:
    """Apply filters Supabase helpers can't cover: experience range, title keywords."""
    min_exp = filters.get("min_years_experience")
    max_exp = filters.get("max_years_experience")
    title_kws = [t.lower() for t in (filters.get("title_keywords") or []) if t]
    employer = (filters.get("current_employer") or "").strip().lower()
    out = []
    for c in candidates:
        exp = _exp_years(c)
        if min_exp is not None and exp is not None and exp < float(min_exp):
            continue
        if max_exp is not None and exp is not None and exp > float(max_exp):
            continue
        if title_kws:
            title = (c.get("current_job_title") or "").lower()
            if not any(kw in title for kw in title_kws):
                continue
        if employer:
            emp = (c.get("current_employer") or "").lower()
            if employer not in emp:
                continue
        out.append(c)
    return out


def _score_candidates_for_search(candidates: list[dict], filters: dict,
                                 soft_criteria: list[dict]) -> list[dict]:
    """Batch-score candidates 0-100 against parsed filters + soft criteria."""
    if not candidates:
        return []
    req_lines = []
    if filters.get("title_keywords"):
        req_lines.append(f"Role: {', '.join(filters['title_keywords'])}")
    if filters.get("must_have_skills"):
        req_lines.append(f"Must-have skills: {', '.join(filters['must_have_skills'])}")
    if filters.get("location"):
        req_lines.append(f"Location: {filters['location']}")
    if filters.get("min_years_experience") is not None:
        req_lines.append(f"Min experience: {filters['min_years_experience']} years")
    if filters.get("max_years_experience") is not None:
        req_lines.append(f"Max experience: {filters['max_years_experience']} years")
    if soft_criteria:
        crit_block = "; ".join(
            f"[{c.get('weight', 'preferred')}] {c.get('criterion', '')}"
            for c in soft_criteria if c.get("criterion")
        )
        if crit_block:
            req_lines.append(f"Soft criteria: {crit_block}")
    req_summary = "\n".join(req_lines) or "Open search"

    all_scores: dict[str, dict] = {}
    for i in range(0, len(candidates), MATCH_BATCH_SIZE):
        batch = candidates[i:i + MATCH_BATCH_SIZE]
        cand_block = "\n\n".join(
            _build_candidate_summary(c, j + 1) for j, c in enumerate(batch)
        )
        prompt = (
            "You are a recruitment matching engine. Score each candidate 0-100 "
            "for fit against this search. Consider skill synonyms (React = ReactJS, "
            ".NET = dotnet), transferable experience, seniority, and location fit.\n\n"
            f"SEARCH:\n{req_summary}\n\n"
            f"CANDIDATES:\n{cand_block}\n\n"
            "Return a JSON array. Each element must have:\n"
            "- \"candidate_id\": exact ID string from above\n"
            "- \"score\": integer 0-100\n"
            "- \"reasoning\": one short sentence\n\n"
            "Return ONLY the JSON array, no other text."
        )
        result_text = _call_claude(
            "claude-sonnet-4-20250514",
            "You are an expert IT recruitment matcher. Score candidates accurately.",
            prompt, max_tokens=4096, endpoint="/search-run-scoring",
        )
        parsed = _parse_llm_json(result_text)
        if not isinstance(parsed, list):
            continue
        cand_ids = {c["id"] for c in batch}
        for entry in parsed:
            if (isinstance(entry, dict)
                    and entry.get("candidate_id") in cand_ids
                    and isinstance(entry.get("score"), (int, float))
                    and 0 <= entry["score"] <= 100):
                all_scores[entry["candidate_id"]] = {
                    "score": int(entry["score"]),
                    "reasoning": str(entry.get("reasoning", "")),
                }
    return [
        {"candidate_id": cid, **data}
        for cid, data in all_scores.items()
        if data["score"] >= MATCH_MIN_SCORE
    ]


def run_search(payload: dict, market: str | None) -> dict:
    """Unified search handler: parses (for natural/jd modes) then fetches + ranks candidates.

    Payload shape:
        {mode: 'natural'|'jd'|'manual',
         text?: str,                      # required for natural / jd
         filters?: dict,                  # required for manual; parser output shape
         soft_criteria?: list[dict]}      # optional for manual
    """
    if not isinstance(payload, dict):
        raise CoreError(422, "payload must be a JSON object")
    mode = (payload.get("mode") or "natural").lower()
    if mode not in ("natural", "jd", "manual"):
        raise CoreError(422, f"invalid mode: {mode}")

    filters: dict = {}
    soft_criteria: list[dict] = []

    if mode in ("natural", "jd"):
        text = payload.get("text") or payload.get("requirement_text") or ""
        if not isinstance(text, str) or not text.strip():
            raise CoreError(422, "text (string) is required for this mode")
        try:
            if mode == "jd":
                parsed = search_parser.parse_jd_to_filters(
                    text, _call_claude, _parse_llm_json)
            else:
                parsed = search_parser.parse_search_query(
                    text, _call_claude, _parse_llm_json)
        except ValueError as e:
            raise CoreError(422, str(e))
        filters = parsed.get("hard_filters", {}) or {}
        soft_criteria = parsed.get("soft_criteria", []) or []
    else:  # manual
        filters = payload.get("filters") or {}
        soft_criteria = payload.get("soft_criteria") or []
        if not isinstance(filters, dict):
            raise CoreError(422, "filters must be an object")

    market = _normalize_market(market) or _normalize_market(filters.get("market"))

    must_skills = filters.get("must_have_skills") or []
    location = filters.get("location")
    if must_skills:
        candidates = db.search_candidates_by_skill(must_skills, market)
    else:
        candidates = db.search_candidates_broad(
            market=market, location=location, limit=200)

    candidates = _apply_python_filters(candidates, filters)

    scored = _score_candidates_for_search(candidates, filters, soft_criteria)
    scored.sort(key=lambda x: x["score"], reverse=True)

    cand_by_id = {c["id"]: c for c in candidates}
    results = []
    for s in scored[:SEARCH_RESULT_LIMIT]:
        c = cand_by_id.get(s["candidate_id"])
        if not c:
            continue
        results.append({
            "id": c["id"],
            "name": c.get("name"),
            "email": c.get("email"),
            "skills": c.get("skills") or [],
            "current_location": c.get("current_location"),
            "current_job_title": c.get("current_job_title"),
            "current_employer": c.get("current_employer"),
            "total_experience": c.get("total_experience"),
            "score": s["score"],
            "reasoning": s["reasoning"],
        })

    return {
        "status": "ok",
        "mode": mode,
        "filters": filters,
        "soft_criteria": soft_criteria,
        "candidate_pool": len(candidates),
        "candidates": results,
    }


# ── Requirements ───────────────────────────────────────────────

def list_requirements(market: str | None, status: str = "open",
                      created_after: str | None = None,
                      project_id: str | None = None,
                      assigned_to: str | None = None) -> dict:
    """List requirements, optionally scoped to one recruiter's assignments.

    assigned_to: when set, filter to requirements whose assigned_recruiters
    array contains this email. Used for the recruiter's default 'My Requirements'
    view. TL-only requirements (created with empty assigned_recruiters) are
    excluded from every recruiter's view.
    """
    if market == "all":
        market = None
    if status == "open":
        reqs = db.get_open_requirements(market, created_after=created_after,
                                        project_id=project_id)
    else:
        q = (db.get_client().table("requirements")
             .select("id, market, client_name, role_title, skills_required, "
                     "status, assigned_recruiters, created_at, project_id")
             .eq("status", status))
        if market:
            q = q.eq("market", market)
        if created_after:
            q = q.gte("created_at", created_after)
        if project_id:
            q = q.eq("project_id", project_id)
        reqs = q.execute().data
    if assigned_to:
        reqs = [r for r in reqs
                if assigned_to in (r.get("assigned_recruiters") or [])]
    # Annotate each requirement with how many candidates are above threshold
    if reqs:
        req_ids = [r["id"] for r in reqs]
        try:
            rows = (db.get_client().table("match_scores")
                    .select("requirement_id, score")
                    .in_("requirement_id", req_ids)
                    .gte("score", MATCH_MIN_SCORE)
                    .execute().data)
            counts: dict[str, int] = {}
            for row in rows:
                rid = row["requirement_id"]
                counts[rid] = counts.get(rid, 0) + 1
            for r in reqs:
                raw = counts.get(r["id"], 0)
                r["matched_count"] = min(raw, DEFAULT_SOURCE_CAP_PER_REQ)
        except Exception:
            for r in reqs:
                r["matched_count"] = 0
    return {"requirements": reqs, "count": len(reqs)}


def close_requirement(req_id: str, user_role: str, user_email: str) -> dict:
    """Mark a requirement as closed. TL only."""
    _require_role(user_role, ["tl"])
    existing = db.get_requirement_by_id(req_id)
    if not existing:
        raise CoreError(404, "Requirement not found")
    db.get_client().table("requirements").update(
        {"status": "closed"}).eq("id", req_id).execute()
    return {"status": "ok", "requirement_id": req_id}


def wipe_all_requirements(user_role: str, user_email: str) -> dict:
    """Destructively delete every requirement and its FK-linked children.

    TL only. Candidates, projects, client_contacts are kept. This is the
    "start fresh" operation agreed in the workflow spec — irreversible
    without a Supabase point-in-time restore.
    """
    _require_role(user_role, ["tl"])
    counts = db.wipe_all_requirements()
    log.warning("[wipe] %s wiped all requirements: %s", user_email, counts)
    return {"status": "ok", "deleted": counts}


DEFAULT_SOURCE_CAP_PER_REQ = 5


async def _source_and_score_capped(req_id: str, cap: int) -> dict:
    """Source candidates for one requirement, score, persist match_scores, and
    report how many ended up above threshold.

    Source pool = (external channels if any configured + available) UNION
    (internal DB candidates for this market). The internal fallback means
    Source Now still returns real results even when every external channel
    is unavailable (expired cookies, free-plan lockouts, etc.).

    The response includes a ``channel_errors`` map so the UI can show
    "apollo: HTTP 403" etc. rather than a silent zero.
    """
    requirement = db.get_requirement_by_id(req_id)
    if not requirement:
        return {"requirement_id": req_id, "error": "not_found",
                "sourced": 0, "top_count": 0, "channel_errors": {}}
    channel_errors: dict[str, str] = {}
    external_pool: list[dict] = []
    try:
        source_results = await sourcing.run_all_sources(requirement)
        external_pool = source_results.pop("upserted_candidates", []) or []
        channel_errors = source_results.pop("channel_errors", {}) or {}
        external_sourced_count = source_results.get("total_unique", 0)
    except Exception as e:
        log.exception("run_all_sources failed for %s", req_id)
        channel_errors["_run"] = str(e)
        external_sourced_count = 0

    # Always include an internal-DB slice to keep Source Now useful even when
    # external channels contribute zero (free-tier Apollo, expired cookies).
    internal_pool: list[dict] = []
    try:
        internal_pool = db.search_candidates_broad(
            market=requirement.get("market"),
            location=requirement.get("location"),
            limit=100,
        ) or []
    except Exception as e:
        log.exception("search_candidates_broad failed for %s", req_id)
        channel_errors["internal_db"] = f"{type(e).__name__}: {e}"

    # Merge + dedupe by id
    pool_by_id: dict[str, dict] = {}
    for c in external_pool + internal_pool:
        cid = c.get("id")
        if cid and cid not in pool_by_id:
            pool_by_id[cid] = c
    pool = list(pool_by_id.values())

    # Only re-score pool members that don't already have a match_score row
    # for this requirement (avoids redundant LLM cost on re-clicks).
    already_scored: dict[str, dict] = {}
    try:
        already_scored = db.get_cached_match_scores(
            req_id, [c["id"] for c in pool])
    except Exception:
        pass
    to_score = [c for c in pool if c["id"] not in already_scored]
    new_scores: list[dict] = []
    for i in range(0, len(to_score), MATCH_BATCH_SIZE):
        batch = to_score[i:i + MATCH_BATCH_SIZE]
        new_scores.extend(_score_candidate_batch(batch, requirement))
    if new_scores:
        try:
            db.upsert_match_scores(req_id, new_scores)
        except Exception:
            log.exception("upsert_match_scores failed for %s", req_id)

    try:
        top = db.get_match_scores_above(req_id, min_score=MATCH_MIN_SCORE)
    except Exception:
        top = []

    return {
        "requirement_id": req_id,
        "sourced": external_sourced_count,
        "internal_pool_size": len(internal_pool),
        "scored_new": len(new_scores),
        "top_count": min(len(top), cap),
        "channel_errors": channel_errors,
    }


def source_requirements_batch(payload: Any, user_role: str,
                              user_email: str) -> dict:
    """Run Source Now on multiple requirements in parallel with a per-req cap."""
    _require_role(user_role, ["recruiter", "tl"])
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    req_ids = payload.get("requirement_ids")
    if not isinstance(req_ids, list) or not req_ids:
        raise CoreError(422, "requirement_ids must be a non-empty list")
    if not all(isinstance(r, str) for r in req_ids):
        raise CoreError(422, "requirement_ids must be strings")
    cap = payload.get("per_req_cap", DEFAULT_SOURCE_CAP_PER_REQ)
    try:
        cap = max(1, int(cap))
    except (TypeError, ValueError):
        cap = DEFAULT_SOURCE_CAP_PER_REQ

    async def _all():
        return await asyncio.gather(
            *[_source_and_score_capped(r, cap) for r in req_ids])

    results = _run_async(_all())
    return {"status": "ok", "per_req_cap": cap, "results": results}


# ── Candidate detail + Shortlist + Notes (Phase 3) ─────────

def get_candidate_detail(candidate_id: str, user_role: str,
                         user_email: str) -> dict:
    """Full candidate view for the slide-over: base row + any existing
    candidate_details + notes + shortlist status + submissions for this
    candidate (so the slide-over can tell whether Submit-to-TL is already
    done per requirement)."""
    _require_role(user_role, ["recruiter", "tl"])
    cand = db.get_candidate_by_id(candidate_id)
    if not cand:
        raise CoreError(404, "Candidate not found")
    try:
        notes = db.list_candidate_notes(candidate_id)
    except Exception:
        notes = []
    try:
        shortlisted = db.is_shortlisted(candidate_id, user_email)
    except Exception:
        shortlisted = False
    try:
        detail_rows = (db.get_client().table("candidate_details")
                       .select("*").eq("candidate_id", candidate_id)
                       .execute().data)
    except Exception:
        detail_rows = []
    try:
        submissions = (db.get_client().table("submissions")
                       .select("id, requirement_id, submitted_by_recruiter, "
                               "submitted_at, tl_approved, tl_approved_at, "
                               "sent_to_client_at, final_status, "
                               "placement_type, remarks")
                       .eq("candidate_id", candidate_id)
                       .order("submitted_at", desc=True)
                       .execute().data)
    except Exception:
        submissions = []
    return {
        "candidate": cand,
        "details": detail_rows,
        "notes": notes,
        "shortlisted": shortlisted,
        "submissions": submissions,
    }


def submit_to_tl(candidate_id: str, payload: Any, user_role: str,
                 user_email: str) -> dict:
    """Recruiter pushes a shortlisted candidate into the TL submission queue.

    Creates a submissions row with tl_approved=false. TL sees it in
    /api/tl/queue and can Approve-and-send or Reject from there.
    """
    _require_role(user_role, ["recruiter", "tl"])
    body = payload if isinstance(payload, dict) else {}
    requirement_id = (body.get("requirement_id") or "").strip() \
        if isinstance(body.get("requirement_id"), str) else body.get("requirement_id")
    if not requirement_id:
        raise CoreError(422, "requirement_id is required")
    cand = db.get_candidate_by_id(candidate_id)
    if not cand:
        raise CoreError(404, "Candidate not found")
    requirement = db.get_requirement_by_id(requirement_id)
    if not requirement:
        raise CoreError(404, "Requirement not found")

    existing = (db.get_client().table("submissions")
                .select("id, tl_approved, final_status, submitted_at")
                .eq("candidate_id", candidate_id)
                .eq("requirement_id", requirement_id)
                .execute().data)
    if existing:
        raise CoreError(409,
                        "Candidate already submitted for this requirement")

    placement = body.get("placement_type")
    if placement and placement not in ("FTE", "TP", "C2H"):
        raise CoreError(422, "placement_type must be FTE, TP, or C2H")

    remarks = (body.get("remarks") or body.get("tl_notes") or "").strip()
    now_ts = datetime.now(timezone.utc).isoformat()
    sub_row = db.insert_submission({
        "candidate_id": candidate_id,
        "requirement_id": requirement_id,
        "client_name": requirement.get("client_name") or "",
        "tender_number": requirement.get("tender_number"),
        "market": requirement.get("market"),
        "submitted_by_recruiter": user_email,
        "submitted_at": now_ts,
        "tl_approved": False,
        "placement_type": placement,
        "remarks": remarks,
    })
    try:
        db.upsert_candidate_details(candidate_id, requirement_id, {
            "status": "submitted_to_tl",
        })
    except Exception:
        pass  # non-fatal if candidate_details row insert fails
    return {
        "status": "submitted",
        "submission_id": sub_row["id"],
        "submitted_at": now_ts,
        "candidate_id": candidate_id,
        "requirement_id": requirement_id,
    }


def toggle_shortlist_candidate(candidate_id: str, payload: Any,
                               user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    cand = db.get_candidate_by_id(candidate_id)
    if not cand:
        raise CoreError(404, "Candidate not found")
    note = None
    if isinstance(payload, dict):
        raw = payload.get("note")
        if isinstance(raw, str) and raw.strip():
            note = raw.strip()
    result = db.toggle_shortlist(candidate_id, user_email, note=note)
    return {"status": "ok", **result}


def add_note_to_candidate(candidate_id: str, payload: Any,
                          user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be JSON object")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise CoreError(422, "content (non-empty string) required")
    cand = db.get_candidate_by_id(candidate_id)
    if not cand:
        raise CoreError(404, "Candidate not found")
    row = db.add_candidate_note(candidate_id, user_email, content.strip())
    return {"status": "ok", "note": row}


def list_user_shortlists(user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    rows = db.list_shortlists_for_user(user_email)
    cand_ids = [r.get("candidates", {}).get("id")
                for r in rows if r.get("candidates", {}).get("id")]
    # Batch-fetch latest submission per candidate so the UI can show
    # "Submitted" / "Approved" / "Sent" tags without another round-trip.
    submissions_by_cand: dict = {}
    if cand_ids:
        try:
            sub_rows = (db.get_client().table("submissions")
                        .select("candidate_id, requirement_id, tl_approved, "
                                "sent_to_client_at, final_status, submitted_at")
                        .in_("candidate_id", cand_ids)
                        .order("submitted_at", desc=True)
                        .execute().data) or []
            for sub in sub_rows:
                cid = sub.get("candidate_id")
                # Keep only the most recent submission per candidate
                if cid and cid not in submissions_by_cand:
                    submissions_by_cand[cid] = sub
        except Exception:
            submissions_by_cand = {}

    out = []
    for r in rows:
        cand = r.get("candidates") or {}
        cid = cand.get("id")
        sub = submissions_by_cand.get(cid) or {}
        if sub.get("sent_to_client_at"):
            sub_status = "sent_to_client"
        elif sub.get("tl_approved"):
            sub_status = "tl_approved"
        elif sub.get("final_status") == "rejected_by_tl":
            sub_status = "rejected_by_tl"
        elif sub:
            sub_status = "submitted_to_tl"
        else:
            sub_status = None
        out.append({
            "shortlist_id": r["id"],
            "shortlisted_at": r.get("created_at"),
            "note": r.get("note"),
            "id": cid,
            "name": cand.get("name"),
            "email": cand.get("email"),
            "phone": cand.get("phone"),
            "skills": cand.get("skills") or [],
            "current_job_title": cand.get("current_job_title"),
            "current_employer": cand.get("current_employer"),
            "current_location": cand.get("current_location"),
            "total_experience": cand.get("total_experience"),
            "linkedin_url": cand.get("linkedin_url"),
            "market": cand.get("market"),
            "submission_status": sub_status,
            "latest_requirement_id": sub.get("requirement_id") if sub else None,
        })
    return {"shortlists": out, "count": len(out)}


def get_requirement_candidates(req_id: str, user_role: str,
                               user_email: str) -> dict:
    """Return top matched candidates for a requirement (uses match_scores cache)."""
    _require_role(user_role, ["recruiter", "tl"])
    requirement = db.get_requirement_by_id(req_id)
    if not requirement:
        raise CoreError(404, "Requirement not found")
    match_rows = db.get_match_scores_above(req_id, min_score=MATCH_MIN_SCORE)
    if not match_rows:
        return {"requirement": requirement, "candidates": [],
                "cap": DEFAULT_SOURCE_CAP_PER_REQ}
    cand_ids = [m["candidate_id"] for m in match_rows[:DEFAULT_SOURCE_CAP_PER_REQ]]
    cands = (db.get_client().table("candidates").select("*")
             .in_("id", cand_ids).execute().data)
    cand_by_id = {c["id"]: c for c in cands}
    results = []
    for m in match_rows[:DEFAULT_SOURCE_CAP_PER_REQ]:
        c = cand_by_id.get(m["candidate_id"])
        if not c:
            continue
        results.append({
            "id": c["id"],
            "name": c.get("name"),
            "email": c.get("email"),
            "skills": c.get("skills") or [],
            "current_location": c.get("current_location"),
            "current_job_title": c.get("current_job_title"),
            "current_employer": c.get("current_employer"),
            "total_experience": c.get("total_experience"),
            "highest_education": c.get("highest_education"),
            "linkedin_url": c.get("linkedin_url"),
            "score": m["score"],
            "reasoning": m.get("reasoning", ""),
        })
    return {"requirement": requirement, "candidates": results,
            "cap": DEFAULT_SOURCE_CAP_PER_REQ}


def _validate_requirement_create(payload: Any) -> dict:
    """Manual validation replacing the FastAPI Pydantic RequirementCreate model.

    Returns a cleaned dict. Raises CoreError(422) on any problem.
    """
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")

    errors: list[str] = []

    def _req_str(key: str) -> str:
        v = payload.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{key} is required (non-empty string)")
            return ""
        return v.strip()

    def _opt_str(key: str) -> str | None:
        v = payload.get(key)
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            errors.append(f"{key} must be a string")
            return None
        return v

    data: dict[str, Any] = {
        "client_name": _req_str("client_name"),
        "market": _req_str("market"),
        "role_title": _req_str("role_title"),
    }

    # skills_required: list[str] (default [])
    skills = payload.get("skills_required", [])
    if skills is None:
        skills = []
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        errors.append("skills_required must be a list of strings")
        skills = []
    data["skills_required"] = [s.strip() for s in skills if s.strip()]

    # experience_min: int | None — accept int, numeric string, or empty
    exp = payload.get("experience_min")
    if exp is None or exp == "":
        data["experience_min"] = None
    else:
        try:
            data["experience_min"] = int(exp)
        except (TypeError, ValueError):
            errors.append("experience_min must be an integer")

    # Optional strings
    for key in ("skillset", "salary_budget", "location", "contract_type",
                "notice_period", "tender_number", "jd_text"):
        val = _opt_str(key)
        if val is not None:
            data[key] = val

    # assigned_recruiters: list[str] of recruiter emails (default []). Empty
    # list = TL-only (no recruiter sees it in their own list).
    assigned = payload.get("assigned_recruiters", [])
    if assigned is None:
        assigned = []
    if not isinstance(assigned, list) or not all(isinstance(s, str) for s in assigned):
        errors.append("assigned_recruiters must be a list of strings")
        assigned = []
    data["assigned_recruiters"] = [s.strip() for s in assigned if s.strip()]

    if errors:
        raise CoreError(422, "; ".join(errors))

    data["market"] = _normalize_market(data["market"])
    return data


def create_requirement(payload: Any, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["tl"])
    data = _validate_requirement_create(payload)
    jd_text = data.pop("jd_text", None)

    # JD parser agent — enrich fields from free-form JD text
    jd_parsed = None
    if jd_text:
        jd_parser_prompt = AGENTS.get("jd_parser", "")
        if jd_parser_prompt:
            parsed_raw = _call_claude(
                "claude-sonnet-4-20250514",
                jd_parser_prompt,
                f"Market: {data.get('market', 'IN')}\n\nJD text:\n{jd_text}",
                max_tokens=2048, endpoint="/requirements/create/jd-parse",
            )
            jd_parsed = _parse_llm_json(parsed_raw)
            if isinstance(jd_parsed, dict):
                if not data.get("skills_required") and jd_parsed.get("skills_required"):
                    data["skills_required"] = jd_parsed["skills_required"]
                if data.get("experience_min") is None and jd_parsed.get("experience_min"):
                    try:
                        data["experience_min"] = int(jd_parsed["experience_min"])
                    except (TypeError, ValueError):
                        pass
                if not data.get("salary_budget") and jd_parsed.get("salary_max"):
                    sal_min = jd_parsed.get("salary_min", "")
                    sal_max = jd_parsed.get("salary_max", "")
                    currency = jd_parsed.get("salary_currency", "")
                    data["salary_budget"] = f"{sal_min}-{sal_max} {currency}".strip()
                if not data.get("location") and jd_parsed.get("location"):
                    data["location"] = jd_parsed["location"]
                if not data.get("contract_type") and jd_parsed.get("contract_type"):
                    data["contract_type"] = jd_parsed["contract_type"]
                if not data.get("notice_period") and jd_parsed.get("notice_period_max_days"):
                    data["notice_period"] = str(jd_parsed["notice_period_max_days"])
                data["jd_parsed"] = jd_parsed
        else:
            # Fallback: simple skills extraction
            if not data.get("skills_required"):
                parsed = _call_claude(
                    "claude-haiku-4-5-20251001",
                    AGENTS.get("screener", ""),
                    f"Extract skills_required as a JSON array of strings "
                    f"from this JD:\n{jd_text}",
                    max_tokens=512, endpoint="/requirements/create",
                )
                skills = _parse_llm_json(parsed)
                if isinstance(skills, list):
                    data["skills_required"] = skills

    # TL-only requirements: if no recruiters were picked, the TL is keeping
    # it for themselves. Store their own email in assigned_recruiters so the
    # "My Requirements" filter picks it up for them (and still hides it from
    # other recruiters).
    if user_role == "tl" and not data.get("assigned_recruiters"):
        data["assigned_recruiters"] = [user_email]

    req = db.insert_requirement(data)
    return {"requirement_id": req["id"], "sourcing_started": False,
            "jd_parsed": jd_parsed}


def source_requirement(req_id: str, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    requirement = db.get_requirement_by_id(req_id)
    if not requirement:
        raise CoreError(404, "Requirement not found")
    try:
        source_results = _run_async(sourcing.run_all_sources(requirement))
    except Exception as e:
        log.exception("run_all_sources crashed for %s", req_id)
        raise CoreError(500, f"Sourcing failed: {e}")
    linkedin_str = sourcing.generate_linkedin_search_string(requirement)
    just_sourced = source_results.pop("upserted_candidates", [])
    _run_in_background(_screen_candidates, req_id, requirement, just_sourced)
    return {
        "sourced": source_results.get("total_unique", 0),
        "screened": "in_progress",
        "shortlisted": "in_progress",
        "linkedin_search_string": linkedin_str,
        "message": (f"Sourced {source_results.get('total_unique', 0)} candidates. "
                    f"Screening {len(just_sourced)} in background."),
    }


# ── Outreach ───────────────────────────────────────────────────

def _require_fields(payload: Any, fields: list[str]) -> dict:
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    missing = [f for f in fields if not payload.get(f)]
    if missing:
        raise CoreError(422, f"missing required fields: {', '.join(missing)}")
    return payload


def prepare_outreach(payload: Any, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter"])
    body = _require_fields(payload, ["candidate_id", "requirement_id",
                                     "recruiter_name", "recruiter_email"])
    candidate = db.get_candidate_by_id(body["candidate_id"])
    requirement = db.get_requirement_by_id(body["requirement_id"])
    if not candidate or not requirement:
        raise CoreError(404, "Candidate or requirement not found")
    result = _call_claude(
        "claude-haiku-4-5-20251001",
        AGENTS.get("outreach", ""),
        f"Draft outreach email.\n\nCANDIDATE: {candidate}\n\n"
        f"REQUIREMENT: {requirement}\n\n"
        f"RECRUITER: {body['recruiter_name']} <{body['recruiter_email']}>\n\n"
        f"Return JSON with subject and body keys only.",
        max_tokens=2048, endpoint="/candidates/prepare-outreach",
    )
    draft = _parse_llm_json(result)
    if not isinstance(draft, dict):
        draft = {"subject": f"Opportunity: {requirement.get('role_title', '')}",
                 "body": result}
    return {"draft_subject": draft.get("subject", ""),
            "draft_body": draft.get("body", "")}


# ── Sequences (Phase 4) — batch AI template + preview + send ───

SEQUENCE_DRAFT_SYSTEM = """\
You are an expert IT recruiter drafting a first-touch outreach email to a \
passive candidate about a specific role. Keep it warm, short (under 150 \
words), and concrete. Don't over-promise. Don't use marketing fluff.

Return ONLY valid JSON with this shape — no explanation, no backticks:
{
  "subject": "<concise subject line>",
  "body": "<full email body, plain text; use {FIRST_NAME} placeholder \
where the candidate's first name should go>"
}
"""


def _first_name(full_name: str | None) -> str:
    if not full_name:
        return "there"
    return full_name.split()[0] if full_name.split() else "there"


def draft_sequence(payload: Any, user_role: str, user_email: str) -> dict:
    """Generate one AI-drafted template + personalized previews for each candidate.

    Input: {requirement_id, candidate_ids: [...]}
    Output: {template: {subject, body}, emails: [{candidate_id, to_email,
             to_name, subject, body, sendable}]}
    """
    _require_role(user_role, ["recruiter", "tl"])
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    req_id = payload.get("requirement_id")
    cand_ids = payload.get("candidate_ids")
    if not req_id or not isinstance(req_id, str):
        raise CoreError(422, "requirement_id required")
    if not isinstance(cand_ids, list) or not cand_ids:
        raise CoreError(422, "candidate_ids must be a non-empty list")

    requirement = db.get_requirement_by_id(req_id)
    if not requirement:
        raise CoreError(404, "Requirement not found")

    cands = (db.get_client().table("candidates").select(
        "id, name, email, current_job_title, current_employer, "
        "current_location, skills").in_("id", cand_ids).execute().data)
    if not cands:
        raise CoreError(404, "No candidates found for those ids")

    # Lookup recruiter display name for the signature
    recruiter_name = payload.get("recruiter_name") or user_email.split("@")[0].title()

    # Compose the prompt context
    req_context = (
        f"Role: {requirement.get('role_title', '—')}\n"
        f"Client: {requirement.get('client_name', '—')}\n"
        f"Location: {requirement.get('location', '—')}\n"
        f"Market: {requirement.get('market', '—')}\n"
        f"Skills: {', '.join(requirement.get('skills_required', []) or [])}\n"
        f"Experience min: {requirement.get('experience_min', '—')}\n"
        f"Salary: {requirement.get('salary_budget', '—')}\n"
        f"Contract type: {requirement.get('contract_type', '—')}\n"
    )
    recruiter_sig = f"From the desk of {recruiter_name} ({user_email})."

    try:
        raw = _call_claude(
            "claude-haiku-4-5-20251001",
            SEQUENCE_DRAFT_SYSTEM,
            f"REQUIREMENT:\n{req_context}\n\nRECRUITER SIGNATURE:\n{recruiter_sig}\n\n"
            "Draft the template now. Remember: under 150 words, warm but \
not salesy, plain text.",
            max_tokens=1024,
            endpoint="/sequences/draft",
        )
        template = _parse_llm_json(raw)
    except Exception:
        log.exception("sequence draft generation failed")
        template = None

    if not isinstance(template, dict) or not template.get("body"):
        # Fallback template
        template = {
            "subject": f"Opportunity: {requirement.get('role_title', 'New role')} at {requirement.get('client_name', '')}",
            "body": (
                "Hi {FIRST_NAME},\n\n"
                f"I'm reaching out about a {requirement.get('role_title', 'role')} "
                f"opportunity with {requirement.get('client_name', 'our client')} "
                f"in {requirement.get('location', '')}. "
                "Your background looks like a strong match — would you be open to a short chat this week?\n\n"
                "Best,\n"
                f"{recruiter_name}"
            ),
        }

    # Personalize per candidate by substituting the first-name placeholder
    emails = []
    for c in cands:
        first = _first_name(c.get("name"))
        body = (template.get("body") or "").replace("{FIRST_NAME}", first)
        subj = (template.get("subject") or "").replace("{FIRST_NAME}", first)
        emails.append({
            "candidate_id": c["id"],
            "to_email": c.get("email"),
            "to_name": c.get("name"),
            "subject": subj,
            "body": body,
            "sendable": bool(c.get("email")),
        })
    return {
        "status": "ok",
        "template": template,
        "emails": emails,
        "requirement": {
            "id": req_id,
            "role_title": requirement.get("role_title"),
            "client_name": requirement.get("client_name"),
        },
    }


def send_sequence(payload: Any, user_role: str, user_email: str) -> dict:
    """Send each personalized email via Graph API as the logged-in user's mailbox.

    Input: {requirement_id, emails: [{candidate_id, to_email, subject, body}, ...]}
    """
    _require_role(user_role, ["recruiter", "tl"])
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    req_id = payload.get("requirement_id")
    emails = payload.get("emails")
    if not req_id or not isinstance(emails, list) or not emails:
        raise CoreError(422, "requirement_id and non-empty emails list required")

    results = []
    for em in emails:
        cid = em.get("candidate_id")
        to_email = em.get("to_email")
        subject = em.get("subject")
        body = em.get("body")
        if not (cid and to_email and subject and body):
            results.append({"candidate_id": cid, "sent": False,
                            "error": "missing to_email/subject/body"})
            continue
        try:
            sent = outlook.send_email(
                from_email=user_email,
                to_email=to_email,
                subject=subject,
                body=body,
            )
            log_row = db.insert_outreach_log({
                "candidate_id": cid,
                "requirement_id": req_id,
                "recruiter_email": user_email,
                "outlook_message_id": sent.get("message_id"),
                "outlook_thread_id": sent.get("thread_id"),
                "email_subject": subject,
                "sent_at": sent.get("sent_at"),
            })
            results.append({"candidate_id": cid, "sent": True,
                            "outreach_log_id": log_row["id"],
                            "sent_at": sent.get("sent_at")})
        except Exception as e:
            log.exception("sequence send failed for candidate %s", cid)
            results.append({"candidate_id": cid, "sent": False,
                            "error": str(e)})
    total_sent = sum(1 for r in results if r.get("sent"))
    return {"status": "ok", "total_sent": total_sent, "results": results}


def send_outreach(payload: Any, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter"])
    body = _require_fields(payload, ["candidate_id", "requirement_id",
                                     "recruiter_email", "final_subject",
                                     "final_body"])
    candidate = db.get_candidate_by_id(body["candidate_id"])
    if not candidate or not candidate.get("email"):
        raise CoreError(400, "Candidate email not found")
    sent = outlook.send_email(
        from_email=body["recruiter_email"],
        to_email=candidate["email"],
        subject=body["final_subject"],
        body=body["final_body"],
    )
    outreach_row = db.insert_outreach_log({
        "candidate_id": body["candidate_id"],
        "requirement_id": body["requirement_id"],
        "recruiter_email": body["recruiter_email"],
        "outlook_message_id": sent["message_id"],
        "outlook_thread_id": sent["thread_id"],
        "email_subject": body["final_subject"],
        "sent_at": sent["sent_at"],
    })
    return {"sent": True, "outreach_log_id": outreach_row["id"],
            "sent_at": sent["sent_at"]}


# ── Inbox ──────────────────────────────────────────────────────

def process_inbox(payload: Any) -> dict:
    recruiter_email = (payload or {}).get("recruiter_email") if isinstance(payload, dict) else None
    return _run_async(_run_process_inbox(recruiter_email))


# ── TL ─────────────────────────────────────────────────────────

def tl_queue(user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["tl"])
    pending = (db.get_client().table("submissions")
               .select("id, candidate_id, requirement_id, client_name, market, "
                       "formatted_doc_path, submitted_by_recruiter, submitted_at")
               .eq("tl_approved", False)
               .order("submitted_at", desc=True)
               .execute().data)
    enriched = []
    for sub in pending:
        cid, rid = sub.get("candidate_id"), sub.get("requirement_id")
        if not cid or not rid:
            continue  # skip malformed rows with null UUIDs
        details = db.get_candidate_details(cid, rid)
        screenings = (db.get_client().table("screenings")
                      .select("score, recommendation, reasoning")
                      .eq("candidate_id", cid)
                      .eq("requirement_id", rid)
                      .execute().data)
        enriched.append({
            **sub,
            "candidate_details": details,
            "screening": screenings[0] if screenings else {},
        })
    return {"queue": enriched, "count": len(enriched)}


def tl_approve_and_send(payload: Any, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["tl"])
    body = _require_fields(payload, ["submission_id", "tl_email",
                                     "client_email", "email_subject"])
    rows = (db.get_client().table("submissions")
            .select("*").eq("id", body["submission_id"]).execute().data)
    if not rows:
        raise CoreError(404, "Submission not found")
    submission = rows[0]
    candidate = db.get_candidate_by_id(submission["candidate_id"])
    requirement = db.get_requirement_by_id(submission["requirement_id"])
    cand_name = candidate.get("name", "Candidate")
    role_title = requirement.get("role_title", "")
    body_html = (
        f"<p>Dear Hiring Manager,</p>"
        f"<p>Please find attached the profile of <b>{cand_name}</b> "
        f"for the <b>{role_title}</b> position.</p>"
    )
    notes = body.get("email_body_notes")
    if notes:
        body_html += f"<p>{notes}</p>"
    body_html += "<p>Best regards</p>"
    sent = outlook.send_email(
        from_email=body["tl_email"],
        to_email=body["client_email"],
        subject=body["email_subject"],
        body=body_html,
        attachment_path=submission.get("formatted_doc_path"),
    )
    now_ts = datetime.now(timezone.utc).isoformat()
    db.tl_approve_submission(body["submission_id"])
    db.get_client().table("submissions").update({
        "sent_to_client_at": now_ts,
        "final_status": "Submitted",
    }).eq("id", body["submission_id"]).execute()
    db.upsert_candidate_details(
        submission["candidate_id"], submission["requirement_id"],
        {"status": "submitted_to_client"})
    if requirement.get("market") == "SG" and requirement.get("tender_number"):
        db.insert_gebiz_submission(
            submission["candidate_id"],
            requirement["tender_number"],
            school_name=requirement.get("location"),
        )
    return {"sent": True, "sent_at": now_ts}


def tl_reject(payload: Any, user_role: str) -> dict:
    _require_role(user_role, ["tl"])
    body = _require_fields(payload, ["submission_id"])
    feedback = body.get("feedback", "") or ""
    rows = (db.get_client().table("submissions")
            .select("id, candidate_id, requirement_id")
            .eq("id", body["submission_id"]).execute().data)
    if not rows:
        raise CoreError(404, "Submission not found")
    submission = rows[0]
    db.update_submission_status(body["submission_id"], "rejected_by_tl")
    db.upsert_candidate_details(
        submission["candidate_id"], submission["requirement_id"],
        {"status": "rejected_by_tl", "tl_feedback": feedback})
    return {"status": "rejected", "submission_id": body["submission_id"]}


# ── Pipeline ───────────────────────────────────────────────────

def pipeline_summary(market: str | None, project_id: str | None = None) -> dict:
    reqs = db.get_open_requirements(market, project_id=project_id)
    if not reqs:
        return {"pipeline": []}
    reqs = reqs[:50]
    req_ids = [r["id"] for r in reqs]
    scr_by_req: dict[str, list] = defaultdict(list)
    out_by_req: dict[str, list] = defaultdict(list)
    sub_by_req: dict[str, list] = defaultdict(list)
    det_by_req: dict[str, list] = defaultdict(list)
    try:
        for s in (db.get_client().table("screenings")
                  .select("requirement_id, recommendation")
                  .in_("requirement_id", req_ids).execute().data):
            scr_by_req[s["requirement_id"]].append(s)
    except Exception:
        pass
    try:
        for o in (db.get_client().table("outreach_log")
                  .select("requirement_id, reply_received")
                  .in_("requirement_id", req_ids).execute().data):
            out_by_req[o["requirement_id"]].append(o)
    except Exception:
        pass
    try:
        for s in (db.get_client().table("submissions")
                  .select("requirement_id, tl_approved, sent_to_client_at")
                  .in_("requirement_id", req_ids).execute().data):
            sub_by_req[s["requirement_id"]].append(s)
    except Exception:
        pass
    try:
        for d in (db.get_client().table("candidate_details")
                  .select("requirement_id, status")
                  .in_("requirement_id", req_ids).execute().data):
            det_by_req[d["requirement_id"]].append(d)
    except Exception:
        pass
    pipeline = []
    for req in reqs:
        rid = req["id"]
        screenings = scr_by_req[rid]
        outreach_rows = out_by_req[rid]
        submissions_rows = sub_by_req[rid]
        details = det_by_req[rid]
        req_skills = req.get("skills_required", [])
        if req_skills:
            req_skills = db._normalise_skills(req_skills)
        sourced_n = 0
        if req_skills:
            try:
                sourced_rows = (db.get_client().table("candidates")
                                .select("id", count="exact")
                                .overlaps("skills", req_skills)
                                .eq("market", req.get("market", "IN"))
                                .execute())
                sourced_n = sourced_rows.count or 0
            except Exception:
                sourced_n = 0
        matched_n = 0
        try:
            matched_n = db.count_matched_candidates(rid, min_score=60)
        except Exception:
            pass
        pipeline.append({
            "requirement_id": rid,
            "role_title": req.get("role_title"),
            "client_name": req.get("client_name"),
            "market": req.get("market"),
            "sourced": sourced_n,
            "matched": matched_n,
            "screened": len(screenings),
            "shortlisted": sum(1 for s in screenings
                               if s.get("recommendation") == "shortlist"),
            "outreached": len(outreach_rows),
            "replied": sum(1 for o in outreach_rows if o.get("reply_received")),
            "details_complete": sum(1 for d in details
                                    if d.get("status") == "ready_for_review"),
            "submitted_to_tl": len(submissions_rows),
            "sent_to_client": sum(1 for s in submissions_rows
                                  if s.get("sent_to_client_at")),
        })
    return {"pipeline": pipeline}


# ── Projects & Team ─────────────────────────────────────────

def list_team() -> dict:
    """Return all recruiters/TLs as {name, email, role} — no passwords.
    Populates the Collaborators picker in the Create Project modal.
    Local import of RECRUITER_LOGINS avoids a circular import at startup."""
    from app import RECRUITER_LOGINS
    team = [{"name": u["name"], "email": u["email"], "role": u["role"]}
            for u in RECRUITER_LOGINS.values()]
    return {"team": team}


def list_projects(user_email: str) -> dict:
    """Return all projects visible to this user, each enriched with
    progress (% of requirements closed) and collaborator emails."""
    projects = db.list_projects_for_user(user_email)
    for p in projects:
        reqs = db.get_all_requirements_for_project(p["id"])
        total = len(reqs)
        closed = sum(1 for r in reqs if r.get("status") == "closed")
        p["progress"] = round((closed / total) * 100) if total else 0
        p["requirements_count"] = total
        p["collaborators"] = db.get_project_collaborators(p["id"])
    return {"projects": projects, "count": len(projects)}


def create_project(payload: Any, user_role: str, user_email: str) -> dict:
    """Create a Project. No role gate — any logged-in user can create.
    Body: { title (required), access_level ('shared'|'private', default 'shared'),
            collaborators: [email,...] (optional) }."""
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    title = (payload.get("title") or "").strip()
    if not title:
        raise CoreError(422, "Project title is required")
    access = payload.get("access_level") or "shared"
    if access not in ("shared", "private"):
        raise CoreError(422, "access_level must be 'shared' or 'private'")
    collabs = payload.get("collaborators") or []
    if not isinstance(collabs, list):
        raise CoreError(422, "collaborators must be a list of emails")

    proj = db.insert_project({
        "title": title,
        "access_level": access,
        "status": "active",
        "created_by": user_email,
    })
    clean_collabs = [e.strip() for e in collabs
                     if isinstance(e, str) and e.strip() and e.strip() != user_email]
    db.insert_project_collaborators(proj["id"], clean_collabs)
    proj["collaborators"] = clean_collabs
    proj["progress"] = 0
    proj["requirements_count"] = 0
    return {"project": proj}


def update_project(project_id: str, payload: Any,
                   user_role: str, user_email: str) -> dict:
    """Edit a project's title / access_level / collaborators.
    Only the creator (created_by) may edit."""
    proj = db.get_project(project_id)
    if not proj:
        raise CoreError(404, "Project not found")
    if proj.get("created_by") != user_email:
        raise CoreError(403, "Only the project creator can edit this project")
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")

    patch = {}
    if "title" in payload:
        title = (payload["title"] or "").strip()
        if not title:
            raise CoreError(422, "title cannot be empty")
        patch["title"] = title
    if "access_level" in payload:
        if payload["access_level"] not in ("shared", "private"):
            raise CoreError(422, "access_level must be 'shared' or 'private'")
        patch["access_level"] = payload["access_level"]

    if "collaborators" in payload:
        if not isinstance(payload["collaborators"], list):
            raise CoreError(422, "collaborators must be a list of emails")
        db.clear_project_collaborators(project_id)
        clean = [e.strip() for e in payload["collaborators"]
                 if isinstance(e, str) and e.strip() and e.strip() != user_email]
        db.insert_project_collaborators(project_id, clean)

    updated = db.update_project(project_id, patch) if patch else proj
    updated["collaborators"] = db.get_project_collaborators(project_id)
    reqs = db.get_all_requirements_for_project(project_id)
    total = len(reqs)
    closed = sum(1 for r in reqs if r.get("status") == "closed")
    updated["progress"] = round((closed / total) * 100) if total else 0
    updated["requirements_count"] = total
    return {"project": updated}


def archive_project(project_id: str, user_role: str, user_email: str) -> dict:
    """Mark a project archived.  Owner-only."""
    proj = db.get_project(project_id)
    if not proj:
        raise CoreError(404, "Project not found")
    if proj.get("created_by") != user_email:
        raise CoreError(403, "Only the project creator can archive this project")
    updated = db.update_project(project_id, {"status": "archived"})
    return {"project": updated}


def delete_project(project_id: str, user_role: str, user_email: str) -> dict:
    """Hard-delete a project.  Owner-only.  Requirements are orphaned."""
    proj = db.get_project(project_id)
    if not proj:
        raise CoreError(404, "Project not found")
    if proj.get("created_by") != user_email:
        raise CoreError(403, "Only the project creator can delete this project")
    db.delete_project(project_id)
    return {"deleted": project_id}
