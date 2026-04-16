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


# ── Requirements ───────────────────────────────────────────────

def list_requirements(market: str | None, status: str = "open",
                      created_after: str | None = None) -> dict:
    if market == "all":
        market = None
    if status == "open":
        reqs = db.get_open_requirements(market, created_after=created_after)
    else:
        q = (db.get_client().table("requirements")
             .select("id, market, client_name, role_title, skills_required, "
                     "status, assigned_recruiters, created_at")
             .eq("status", status))
        if market:
            q = q.eq("market", market)
        if created_after:
            q = q.gte("created_at", created_after)
        reqs = q.execute().data
    return {"requirements": reqs, "count": len(reqs)}


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

    req = db.insert_requirement(data)
    _run_in_background(_run_source_and_screen, req["id"])
    return {"requirement_id": req["id"], "sourcing_started": True,
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
        details = db.get_candidate_details(sub["candidate_id"],
                                           sub["requirement_id"])
        screenings = (db.get_client().table("screenings")
                      .select("score, recommendation, reasoning")
                      .eq("candidate_id", sub["candidate_id"])
                      .eq("requirement_id", sub["requirement_id"])
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

def pipeline_summary(market: str | None) -> dict:
    reqs = db.get_open_requirements(market)
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
