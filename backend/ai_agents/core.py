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
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anthropic

from .config import db, outlook, sourcing, search_parser, market_intelligence
from . import webhook_signing

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


SHORTLIST_SCHEMA_ERROR = (
    "Supabase is missing shortlist tables (`candidate_shortlists` / "
    "`candidate_notes`). Run "
    "`backend/ai_agents/data/phase3_shortlists_notes.sql` in the SQL Editor "
    "for the same Supabase project your app is using."
)


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


def _is_missing_table_error(exc: Exception, table_name: str) -> bool:
    msg = str(exc)
    return "PGRST205" in msg and table_name in msg


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
# Lower bar for ad-hoc searches: Apollo rows often miss `current_location`
# (LLM penalises that) so a 30-point floor keeps results visible while still
# filtering obvious garbage.
SEARCH_MIN_SCORE = 30


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
        f"seniority level, and location fit. When a candidate's Location is "
        f"\"N/A\" (unknown, e.g. Apollo redacts it), treat it as neutral — "
        f"neither a positive nor a negative — and score on the remaining "
        f"signals.\n\n"
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

_BOUNCE_SENDER_PATTERNS = (
    "mailer-daemon@", "postmaster@", "noreply-bounce@", "bounce@",
)
_BOUNCE_SUBJECT_PATTERNS = (
    "undeliverable", "delivery status notification (failure)",
    "mail delivery failed", "returned mail", "failure notice",
)


def _looks_like_bounce(msg: dict) -> bool:
    sender = (msg.get("sender_email") or "").lower()
    subject = (msg.get("subject") or "").lower()
    if any(sender.startswith(p) for p in _BOUNCE_SENDER_PATTERNS):
        return True
    return any(subject.startswith(p) or p in subject
               for p in _BOUNCE_SUBJECT_PATTERNS)


def _extract_bounced_recipient(body: str) -> str | None:
    if not body:
        return None
    m = re.search(r"Final-Recipient:\s*[^;]+;\s*([^\s\r\n]+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip("<>").lower()
    m = re.search(r"failed recipient[s]?:?\s*([^\s\r\n]+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip("<>").lower()
    m = re.search(r"<([^>@\s]+@[^>@\s]+)>", body)
    if m:
        return m.group(1).lower()
    return None


def _handle_bounce(recruiter_email: str, msg: dict) -> bool:
    """Match a bounce notification to an outstanding outreach send and
    mark the run as bounced. Returns True if handled."""
    body = msg.get("body_text") or ""
    bounced_email = _extract_bounced_recipient(body)
    pending = db.get_pending_replies(recruiter_email) or []
    matched = None
    if bounced_email:
        cand_rows = (db.get_client().table("candidates").select("id")
                     .eq("email", bounced_email).limit(1).execute().data) or []
        cand_id = cand_rows[0]["id"] if cand_rows else None
        if cand_id:
            matched = next((p for p in pending
                            if p.get("candidate_id") == cand_id), None)
    if not matched:
        thread = msg.get("thread_id")
        if thread:
            matched = next((p for p in pending
                            if p.get("outlook_thread_id") == thread), None)
    if not matched:
        return False
    seq_run_id = matched.get("sequence_run_id")
    if seq_run_id:
        db.update_run_status(seq_run_id, "bounced", finished=True)
        db.skip_scheduled_sends(seq_run_id, reason="bounced")
        db.insert_run_event(seq_run_id, "bounced",
                            metadata={"bounced_email": bounced_email,
                                      "subject": msg.get("subject")})
    try:
        db.update_outreach_log(matched["id"],
                               {"error_message": "bounced"})
    except Exception:
        pass
    return True


def _classify_reply_intent(body_text: str) -> str:
    if not body_text:
        return "other"
    raw = _call_claude(
        "claude-haiku-4-5-20251001",
        ("Classify this candidate reply intent as exactly one of: "
         "interested, not_interested, out_of_office, other. "
         "Return only the single word."),
        body_text[:1500],
        max_tokens=10, endpoint="/inbox/classify-intent",
    ).strip().lower()
    for label in ("interested", "not_interested", "out_of_office"):
        if label in raw:
            return label
    return "other"


async def _run_process_inbox(recruiter_email: str | None) -> dict:
    if recruiter_email:
        recruiter_list = [recruiter_email]
    else:
        team_emails = os.environ.get("RECRUITER_EMAILS", "")
        recruiter_list = [e.strip() for e in team_emails.split(",") if e.strip()]
    totals = {"processed": 0, "candidate_replies": 0,
              "new_requirements_flagged": 0, "chase_drafts_pending": 0,
              "bounces": 0, "errors": 0}
    for email in recruiter_list:
        try:
            unread = outlook.get_unread_emails(email, hours_back=24)
        except Exception:
            totals["errors"] += 1
            continue
        for msg in unread:
            totals["processed"] += 1

            # 1. Bounce notifications first — they look like ordinary email
            #    but should never reach the candidate-reply classifier.
            if _looks_like_bounce(msg):
                try:
                    if _handle_bounce(email, msg):
                        totals["bounces"] += 1
                except Exception:
                    log.exception("bounce handling failed")
                    totals["errors"] += 1
                try:
                    outlook.mark_as_read(email, msg["message_id"])
                except Exception:
                    pass
                continue

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
                        # Halt sequence run if this outreach is part of one
                        seq_run_id = matched.get("sequence_run_id")
                        if seq_run_id:
                            try:
                                db.get_client().table("sequence_runs").update({
                                    "status": "replied",
                                    "finished_at": datetime.now(timezone.utc).isoformat(),
                                }).eq("id", seq_run_id).execute()
                                db.get_client().table("sequence_step_sends").update(
                                    {"status": "skipped"}
                                ).eq("run_id", seq_run_id).eq("status", "scheduled").execute()
                                db.get_client().table("sequence_run_events").insert({
                                    "run_id": seq_run_id, "event_type": "replied",
                                }).execute()
                            except Exception:
                                log.exception("halt sequence run %s on reply failed", seq_run_id)
                            # Classify reply intent (interested / out_of_office / etc)
                            try:
                                intent = _classify_reply_intent(msg.get("body_text", ""))
                                db.update_run_intent(seq_run_id, intent)
                                if intent == "interested":
                                    db.insert_run_event(seq_run_id, "interested",
                                                        metadata={"sender": msg.get("sender_email")})
                            except Exception:
                                log.exception("intent classification failed for run %s", seq_run_id)
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
        criteria_names = [c.get("criterion", "") for c in soft_criteria if c.get("criterion")]
        criterion_matches_instruction = (
            "\n- \"criterion_matches\": object mapping each soft criterion name to true/false "
            f"(criteria: {criteria_names})"
            if criteria_names else ""
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
            f"- \"reasoning\": one short sentence{criterion_matches_instruction}\n\n"
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
                cm = entry.get("criterion_matches")
                all_scores[entry["candidate_id"]] = {
                    "score": int(entry["score"]),
                    "reasoning": str(entry.get("reasoning", "")),
                    "criterion_matches": cm if isinstance(cm, dict) else {},
                }
    return [
        {"candidate_id": cid, **data}
        for cid, data in all_scores.items()
        if data["score"] >= SEARCH_MIN_SCORE
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

    # Pull live Apollo results, upsert to DB (gets them an id), then merge.
    # Fall back to title_keywords (always populated by the parser) when the
    # query mentions a role but no explicit skills, e.g. "network engineer".
    title_keywords = filters.get("title_keywords") or []
    apollo_skills = (
        must_skills
        or title_keywords[:4]
        or [sc["criterion"] for sc in soft_criteria[:3] if sc.get("criterion")]
    )
    apollo_db_rows: list[dict] = []
    if not apollo_skills:
        log.info("run_search: Apollo skipped — no skills/title_keywords/soft_criteria")
    elif not (os.environ.get("APOLLO_API_KEY") or os.environ.get("APOLLO_API")):
        log.info("run_search: Apollo skipped — APOLLO_API_KEY not set")
    else:
        try:
            log.info("run_search: calling Apollo skills=%s location=%s market=%s",
                     apollo_skills, location, market)
            apollo_raw = asyncio.run(
                sourcing.source_apollo(apollo_skills, location or "", market or "IN")
            )
            named_count = 0
            emailed_count = 0
            apollo_skipped_unreachable = 0
            for ac in apollo_raw:
                # Apollo says no email AND no/maybe phone -> unreachable. Skip.
                hp = (ac.get("has_direct_phone") or "").lower()
                if ac.get("has_email") is False and hp in ("", "no"):
                    apollo_skipped_unreachable += 1
                    continue
                clean = {k: v for k, v in ac.items() if not k.startswith("_")}
                # Apollo redacts `name` on most tiers. Synthesize a placeholder
                # so the row survives upsert; the UI will show a "Reveal name"
                # button that hits /people/match for the real value.
                if not clean.get("name"):
                    title = clean.get("current_job_title") or "Unknown role"
                    employer = clean.get("current_employer") or "unknown employer"
                    clean["name"] = f"{title} @ {employer} (Apollo)"
                # Re-attach the underscore-prefixed Apollo ids that the
                # normaliser stripped out so the candidate row keeps them.
                if ac.get("_apollo_person_id"):
                    clean["apollo_person_id"] = ac["_apollo_person_id"]
                if ac.get("_apollo_organization_id"):
                    clean["apollo_organization_id"] = ac["_apollo_organization_id"]
                if clean.get("name"):
                    named_count += 1
                if clean.get("email"):
                    emailed_count += 1
                try:
                    if clean.get("email"):
                        row = db.upsert_candidate_by_email(clean)
                    elif clean.get("name"):
                        row = db.upsert_candidate_by_name(clean)
                    else:
                        continue
                    if row and row.get("id"):
                        apollo_db_rows.append(row)
                except Exception as upsert_err:
                    log.warning("Apollo upsert failed: %s", upsert_err)
            log.info("run_search: Apollo returned %d raw, upserted %d, "
                     "%d named, %d emailed, %d skipped (unreachable)",
                     len(apollo_raw), len(apollo_db_rows),
                     named_count, emailed_count, apollo_skipped_unreachable)
        except Exception as e:
            log.warning("run_search: Apollo failed: %s", e)

    existing_emails = {c["email"] for c in candidates if c.get("email")}
    existing_names  = {c["name"]  for c in candidates if c.get("name")}
    for row in apollo_db_rows:
        if row.get("email") and row["email"] not in existing_emails:
            candidates.append(row)
            existing_emails.add(row["email"])
        elif row.get("name") and row["name"] not in existing_names:
            candidates.append(row)
            existing_names.add(row["name"])

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
            "phone": c.get("phone"),
            "linkedin_url": c.get("linkedin_url"),
            "skills": c.get("skills") or [],
            "current_location": c.get("current_location"),
            "current_job_title": c.get("current_job_title"),
            "current_employer": c.get("current_employer"),
            "total_experience": c.get("total_experience"),
            "apollo_person_id": c.get("apollo_person_id"),
            "apollo_organization_id": c.get("apollo_organization_id"),
            "do_not_call": c.get("do_not_call") or False,
            "do_not_email": c.get("do_not_email") or False,
            "score": s["score"],
            "reasoning": s["reasoning"],
            "criterion_matches": s.get("criterion_matches") or {},
        })

    return {
        "status": "ok",
        "mode": mode,
        "filters": filters,
        "soft_criteria": soft_criteria,
        "candidate_pool": len(candidates),
        "candidates": results,
    }


# ── Apollo reveal + credits + candidate detail ─────────────────

_apollo_credits_cache: dict = {}  # {"data": {...}, "expires": datetime}


def get_apollo_credits() -> dict:
    """Return remaining Apollo credits. Result is cached for 5 minutes."""
    now = datetime.now(timezone.utc)
    if _apollo_credits_cache.get("expires") and now < _apollo_credits_cache["expires"]:
        return _apollo_credits_cache["data"]
    try:
        raw = asyncio.run(sourcing.apollo_account_credits())
    except Exception as e:
        raise CoreError(502, f"Apollo credits check failed: {e}")
    month = raw.get("monthly_credits_limit", 0)
    used = raw.get("monthly_credits_used", 0)
    remaining = max(0, month - used)
    # Apollo /auth/health doesn't break out email vs phone separately —
    # expose whatever granularity the API returns.
    data = {
        "email_credits": raw.get("email_credits_per_month", remaining),
        "phone_credits": raw.get("phone_credits_per_month", remaining),
        "export_credits": raw.get("export_credits_per_month", remaining),
        "credits_used_month": used,
        "credits_limit_month": month,
        "credits_remaining": remaining,
        "raw": raw,
    }
    _apollo_credits_cache["data"] = data
    _apollo_credits_cache["expires"] = now + timedelta(minutes=5)
    return data


def reveal_candidate_field(cid: str, field: str,
                           requested_by: str | None = None) -> dict:
    """Call Apollo /people/match to reveal name, email, or phone for a candidate.

    For name/email the revealed value is returned synchronously and upserted
    back to the candidates row. For phone, Apollo delivers the number
    asynchronously via webhook (see app.py:/api/apollo/phone-webhook), so
    this returns {pending: True, value: None} and the frontend polls
    /api/candidates/<cid>/reveal/status until the webhook lands.
    """
    if field not in ("name", "email", "phone"):
        raise CoreError(422, "field must be name | email | phone")
    cand = db.get_candidate_by_id(cid)
    if not cand:
        raise CoreError(404, "Candidate not found")

    apollo_id = cand.get("apollo_person_id")
    linkedin = cand.get("linkedin_url")
    email = cand.get("email")
    if not (apollo_id or linkedin or email):
        raise CoreError(422, "Candidate has no Apollo id, LinkedIn URL, or email — cannot reveal")

    reveal_phone = field == "phone"
    webhook_url: str | None = None
    request_id: str | None = None
    if reveal_phone:
        public_url = (os.environ.get("PUBLIC_APP_URL") or "").rstrip("/")
        if not public_url:
            raise CoreError(500, "PUBLIC_APP_URL not set; phone reveal disabled")
        request_id = uuid.uuid4().hex
        sig = webhook_signing.sign_phone_reveal(request_id, cid)
        webhook_url = (f"{public_url}/api/apollo/phone-webhook"
                       f"?request_id={request_id}&candidate_id={cid}&sig={sig}")
        try:
            db.pending_phone_reveal_create(request_id, cid, requested_by)
        except Exception as e:
            log.warning("Failed to insert pending_phone_reveals row: %s", e)

    # Pre-flight credit gate for email reveals — Apollo may return a 200
    # with no email rather than a 402 when an account is depleted, so we
    # check our cached balance first and re-fetch once if it reads zero.
    if field == "email":
        cached = (_apollo_credits_cache.get("data") or {})
        if cached.get("email_credits", 1) <= 0:
            _apollo_credits_cache.clear()
            fresh = get_apollo_credits()
            if fresh.get("email_credits", 0) <= 0:
                raise CoreError(402, "out_of_credits")

    try:
        person = asyncio.run(sourcing.apollo_people_match(
            apollo_person_id=apollo_id,
            linkedin_url=linkedin,
            email=email,
            reveal_phone_number=reveal_phone,
            webhook_url=webhook_url,
        ))
    except RuntimeError as e:
        msg = str(e)
        if "credits_exhausted" in msg:
            raise CoreError(402, "apollo credits_exhausted")
        raise CoreError(502, f"Apollo error: {msg}")

    # Persist any synchronously-revealed values (name/email come back
    # synchronously even on a phone-reveal call).
    patch: dict = {}
    if person.get("name"):
        patch["name"] = person["name"]
    if person.get("email"):
        patch["email"] = person["email"]
    phone_list = person.get("phone_numbers") or []
    if phone_list:
        p = phone_list[0]
        phone_val = p.get("sanitized_number") or p.get("raw_number") if isinstance(p, dict) else str(p)
        if phone_val:
            patch["phone"] = phone_val
    patch["enriched_at"] = datetime.now(timezone.utc).isoformat()
    if patch:
        db.update_candidate(cid, patch)

    # Bust the credits cache so counter updates on next poll
    _apollo_credits_cache.clear()

    if reveal_phone:
        # Phone arrives async; tell the frontend to poll the status endpoint.
        return {
            "field": "phone",
            "value": None,
            "pending": True,
            "request_id": request_id,
            "candidate_id": cid,
        }

    # Sync reveal — pull the requested value out of the response.
    if field == "name":
        value = person.get("name") or ""
    else:  # email
        emails = person.get("email") or person.get("personal_emails") or []
        if isinstance(emails, list):
            value = emails[0] if emails else ""
        else:
            value = str(emails)

    revealed = bool(value)
    reason = None if revealed else (
        "no_email_on_apollo_record" if person else "apollo_no_match")

    if not revealed and field == "email":
        # Diagnostic: log what Apollo returned so we can tell whether the
        # cause is plan-tier (email_status='unavailable'), per-candidate
        # (Apollo has the person but no email on file), or genuinely empty.
        # Allowlist of keys avoids dumping the full enrichment payload.
        diag_keys = ("id", "email_status", "extrapolated_email_confidence_level",
                     "personal_emails", "email", "linkedin_url", "organization_id")
        redact_keys = ("email", "personal_emails", "phone_numbers")
        redacted = {k: ("<redacted>" if k in redact_keys else v)
                    for k, v in (person or {}).items() if k in diag_keys}
        log.info("apollo reveal returned no email cid=%s payload_keys=%s redacted=%s",
                 cid, sorted((person or {}).keys()), redacted)

    return {"field": field, "value": value, "revealed": revealed,
            "reason": reason, "candidate_id": cid}


def _auto_reveal_top_reachable(
    requirement_id: str,
    candidate_ids_by_score: list[str],
    requested_by: str,
    budget: int = 5,
) -> dict:
    """Auto-reveal up to `budget` top-scored Apollo candidates that Apollo
    flagged reachable (`has_email=True` OR `has_direct_phone='Yes'`).

    Pre-flight credit check skips the entire pass if remaining email credits
    fall below `budget` — avoids 402-on-Nth-call partial-state. On a successful
    /people/match call, name/email are persisted synchronously; phone arrives
    asynchronously via the existing `/api/apollo/phone-webhook` route.

    Returns: {revealed_email, revealed_phone_pending, skipped_credits,
              attempted, errors}.
    """
    summary = {
        "revealed_email": 0, "revealed_phone_pending": 0,
        "skipped_credits": False, "attempted": 0, "errors": 0,
    }
    if not candidate_ids_by_score:
        return summary

    try:
        credits = get_apollo_credits()
    except CoreError as e:
        log.warning("auto_reveal: credits check failed: %s", e.message)
        summary["skipped_credits"] = True
        return summary
    if (credits.get("email_credits") or 0) < budget:
        log.info(
            "auto_reveal: skipping (email_credits=%s < budget=%s)",
            credits.get("email_credits"), budget,
        )
        summary["skipped_credits"] = True
        return summary

    client = db.get_client()
    try:
        rows = (client.table("candidates")
                .select("id, source, apollo_person_id, linkedin_url, email, "
                        "has_email, has_direct_phone, do_not_email, do_not_call")
                .in_("id", candidate_ids_by_score).execute().data) or []
    except Exception:
        log.exception("auto_reveal: candidates fetch failed")
        return summary
    rows_by_id = {r["id"]: r for r in rows}

    reachable: list[dict] = []
    for cid in candidate_ids_by_score:
        r = rows_by_id.get(cid)
        if not r or r.get("source") != "apollo":
            continue
        if r.get("do_not_email") and r.get("do_not_call"):
            continue
        if not (r.get("apollo_person_id") or r.get("linkedin_url")
                or r.get("email")):
            continue
        is_reachable = (
            r.get("has_email") is True
            or (r.get("has_direct_phone") or "").lower() == "yes"
        )
        if not is_reachable:
            continue
        reachable.append(r)
        if len(reachable) >= budget:
            break

    summary["attempted"] = len(reachable)
    if not reachable:
        return summary

    public_url = (os.environ.get("PUBLIC_APP_URL") or "").rstrip("/")
    for r in reachable:
        cid = r["id"]
        reveal_phone = (
            (r.get("has_direct_phone") or "").lower() == "yes"
            and not r.get("do_not_call")
            and bool(public_url)
        )
        webhook_url: str | None = None
        request_id: str | None = None
        if reveal_phone:
            request_id = uuid.uuid4().hex
            sig = webhook_signing.sign_phone_reveal(request_id, cid)
            webhook_url = (f"{public_url}/api/apollo/phone-webhook"
                           f"?request_id={request_id}&candidate_id={cid}&sig={sig}")
            try:
                db.pending_phone_reveal_create(request_id, cid, requested_by)
            except Exception:
                log.exception(
                    "auto_reveal: pending_phone_reveals insert failed for %s",
                    cid)
                request_id = None
                webhook_url = None
                reveal_phone = False
        try:
            person = _run_async(sourcing.apollo_people_match(
                apollo_person_id=r.get("apollo_person_id"),
                linkedin_url=r.get("linkedin_url"),
                email=r.get("email"),
                reveal_phone_number=reveal_phone,
                webhook_url=webhook_url,
            ))
        except RuntimeError as e:
            msg = str(e)
            if "credits_exhausted" in msg:
                log.warning("auto_reveal: credits exhausted at cid=%s", cid)
                break
            log.warning("auto_reveal: /people/match failed for %s: %s",
                        cid, msg)
            summary["errors"] += 1
            continue

        patch: dict = {}
        if person.get("name"):
            patch["name"] = person["name"]
        sync_email = person.get("email")
        if not sync_email:
            pe = person.get("personal_emails")
            if isinstance(pe, list) and pe:
                first = pe[0]
                sync_email = (first if isinstance(first, str)
                              else (first.get("email") if isinstance(first, dict) else ""))
        if sync_email:
            patch["email"] = sync_email
        phone_list = person.get("phone_numbers") or []
        if phone_list and isinstance(phone_list[0], dict):
            p0 = phone_list[0]
            phone_val = p0.get("sanitized_number") or p0.get("raw_number")
            if phone_val:
                patch["phone"] = phone_val
        if patch:
            patch["enriched_at"] = datetime.now(timezone.utc).isoformat()
            try:
                db.update_candidate(cid, patch)
            except Exception:
                log.exception("auto_reveal: update_candidate failed for %s", cid)
        if patch.get("email"):
            summary["revealed_email"] += 1
        if reveal_phone and request_id:
            summary["revealed_phone_pending"] += 1

    _apollo_credits_cache.clear()
    return summary


async def _auto_enrich_linkedin_top(
    candidate_ids_by_score: list[str],
    budget: int = 5,
) -> dict:
    """Auto-enrich up to `budget` non-Apollo top rows that have a LinkedIn URL
    and no email yet.

    Mirrors the Apollo `_auto_reveal_top_reachable` shape but routes through
    harvestapi/linkedin-profile-scraper. We pull source_profile_url for rows
    sourced from linkedin_apify / web_agent / github / huggingface, keep only
    LinkedIn URLs that lack an email, cap at `budget`, and patch any
    {email, phone} we get back.

    Returns: {attempted, patched_email, patched_phone, errors}.
    """
    summary = {"attempted": 0, "patched_email": 0,
               "patched_phone": 0, "errors": 0}
    if not candidate_ids_by_score:
        return summary

    client = db.get_client()
    try:
        rows = (client.table("candidates")
                .select("id, source, email, source_profile_url")
                .in_("id", candidate_ids_by_score).execute().data) or []
    except Exception:
        log.exception("linkedin_enrich: candidates fetch failed")
        return summary
    rows_by_id = {r["id"]: r for r in rows}

    targets: list[dict] = []
    for cid in candidate_ids_by_score:
        r = rows_by_id.get(cid)
        if not r:
            continue
        if r.get("email"):
            continue
        url = (r.get("source_profile_url") or "").strip()
        if "linkedin.com/in/" not in url:
            continue
        targets.append(r)
        if len(targets) >= budget:
            break

    summary["attempted"] = len(targets)
    if not targets:
        return summary

    urls = [t["source_profile_url"] for t in targets]
    try:
        enriched = await sourcing.enrich_linkedin_with_apify(
            urls, max_profiles=budget)
    except Exception:
        log.exception("linkedin_enrich: apify call failed")
        summary["errors"] += 1
        return summary

    for t in targets:
        url = (t.get("source_profile_url") or "").rstrip("/").split("?")[0]
        info = enriched.get(url)
        if not info:
            continue
        patch: dict = {}
        if info.get("email"):
            patch["email"] = info["email"]
        if info.get("phone"):
            patch["phone"] = info["phone"]
        if not patch:
            continue
        patch["enriched_at"] = datetime.now(timezone.utc).isoformat()
        try:
            db.update_candidate(t["id"], patch)
        except Exception:
            log.exception("linkedin_enrich: update_candidate failed for %s",
                          t["id"])
            summary["errors"] += 1
            continue
        if patch.get("email"):
            summary["patched_email"] += 1
        if patch.get("phone"):
            summary["patched_phone"] += 1

    return summary


def get_phone_reveal_status(cid: str) -> dict:
    """Return latest phone-reveal status for a candidate (for frontend polling).

    Statuses: pending, received, no_phone, failed, expired, none.
    A still-pending row older than 5 min is returned as 'expired' (no DB write).
    """
    row = db.pending_phone_reveal_get_latest(cid)
    if not row:
        return {"status": "none"}
    status = row.get("status") or "pending"
    if status == "pending":
        try:
            requested_at = datetime.fromisoformat(
                row["requested_at"].replace("Z", "+00:00"))
        except Exception:
            requested_at = None
        if requested_at and (datetime.now(timezone.utc) - requested_at
                             > timedelta(minutes=5)):
            status = "expired"
    return {
        "status": status,
        "phone": row.get("phone_number"),
        "requested_at": row.get("requested_at"),
        "received_at": row.get("received_at"),
    }


def handle_phone_webhook(request_id: str, candidate_id: str,
                         payload: dict) -> dict:
    """Process an Apollo phone-reveal webhook callback. Idempotent."""
    pending = db.pending_phone_reveal_get(request_id)
    if not pending:
        log.warning("Phone webhook for unknown request_id=%s", request_id)
        return {"ok": True, "noop": True}
    if pending.get("status") != "pending":
        # Already processed — Apollo retries; respond 200 to stop them.
        return {"ok": True, "noop": True, "status": pending.get("status")}
    if pending.get("candidate_id") != candidate_id:
        log.warning("Phone webhook candidate_id mismatch: pending=%s req=%s",
                    pending.get("candidate_id"), candidate_id)
        return {"ok": True, "noop": True}

    person = (payload.get("person") or payload.get("contact")
              or payload or {})
    phone_list = person.get("phone_numbers") or []
    phone_val = ""
    if phone_list and isinstance(phone_list[0], dict):
        p = phone_list[0]
        phone_val = p.get("sanitized_number") or p.get("raw_number") or ""
    elif isinstance(person.get("phone"), str):
        phone_val = person["phone"]

    if phone_val:
        try:
            db.update_candidate(candidate_id, {
                "phone": phone_val,
                "enriched_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log.error("Failed to upsert phone for %s: %s", candidate_id, e)
        db.pending_phone_reveal_mark_received(
            request_id, "received", phone_val, payload)
        _apollo_credits_cache.clear()
        return {"ok": True, "status": "received"}

    db.pending_phone_reveal_mark_received(
        request_id, "no_phone", None, payload)
    return {"ok": True, "status": "no_phone"}


def _fetch_company_enrichment(org_id: str | None) -> dict:
    """Return cached company enrichment; lazy-fetch from Apollo on first touch."""
    if not org_id:
        return {}
    cached = db.get_company_enrichment(org_id)
    if cached:
        return cached
    try:
        org = asyncio.run(sourcing.apollo_organizations_enrich(
            apollo_organization_id=org_id))
    except Exception as e:
        log.warning("Company enrichment failed for %s: %s", org_id, e)
        return {}
    row = {
        "apollo_organization_id": org_id,
        "name": org.get("name"),
        "industries": org.get("industries") or [],
        "founded_year": org.get("founded_year"),
        "revenue": org.get("annual_revenue"),
        "market_cap": org.get("market_cap"),
        "employees": org.get("estimated_num_employees"),
        "hq_location": org.get("city") or org.get("country"),
        "technologies": [
            t.get("name") if isinstance(t, dict) else str(t)
            for t in (org.get("current_technologies") or [])
        ],
        "linkedin_url": org.get("linkedin_url"),
        "website_url": org.get("website_url"),
        "enrichment_json": org,
    }
    try:
        db.upsert_company_enrichment(row)
    except Exception as e:
        log.warning("upsert_company_enrichment failed: %s", e)
    return row


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
                r["matched_count"] = counts.get(r["id"], 0)
        except Exception:
            for r in reqs:
                r["matched_count"] = 0
    return {"requirements": reqs, "count": len(reqs)}


def update_requirement(req_id, payload, user_role, user_email):
    """Update a requirement's editable fields (TL-only)."""
    _require_role(user_role, ["tl"])
    if not payload or not isinstance(payload, dict):
        raise ValueError("Missing payload")
    allowed = {"role_title", "client_name", "market", "status", "skillset",
               "skills_required", "location", "contract_type", "notice_period",
               "salary_budget", "experience_min", "jd_text",
               "assigned_recruiters"}
    updates = {k: v for k, v in payload.items() if k in allowed and v is not None}
    if not updates:
        raise ValueError("No valid fields to update")
    result = db.get_client().table("requirements").update(updates).eq("id", req_id).execute()
    if not result.data:
        raise ValueError("Requirement not found")
    return {"ok": True, "requirement": result.data[0]}


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

    TL only. Candidates, projects, interview_tracker are kept. This is the
    "start fresh" operation agreed in the workflow spec — irreversible
    without a Supabase point-in-time restore.
    """
    _require_role(user_role, ["tl"])
    counts = db.wipe_all_requirements()
    log.warning("[wipe] %s wiped all requirements: %s", user_email, counts)
    return {"status": "ok", "deleted": counts}


DEFAULT_SOURCE_CAP_PER_REQ = 5


async def _source_and_score_capped(req_id: str, cap: int,
                                   _return_pool: bool = False) -> dict:
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

    result = {
        "requirement_id": req_id,
        "sourced": external_sourced_count,
        "internal_pool_size": len(internal_pool),
        "scored_new": len(new_scores),
        "top_count": min(len(top), cap),
        "channel_errors": channel_errors,
    }
    if _return_pool:
        result["_pool"] = pool
    return result


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
    try:
        outreach_rows = (db.get_client().table("outreach_log")
                         .select("id, requirement_id, recruiter_email, "
                                 "email_subject, email_body, status, "
                                 "sent_at, created_at, reply_received, "
                                 "replied_at, outlook_thread_id")
                         .eq("candidate_id", candidate_id)
                         .order("created_at", desc=True)
                         .execute().data) or []
    except Exception:
        outreach_rows = []
    # Enrich outreach with requirement role_title + client_name for display
    req_titles: dict[str, dict] = {}
    req_ids_needed = list({o.get("requirement_id") for o in outreach_rows if o.get("requirement_id")})
    if req_ids_needed:
        try:
            for r in (db.get_client().table("requirements")
                      .select("id, role_title, client_name")
                      .in_("id", req_ids_needed)
                      .execute().data):
                req_titles[r["id"]] = r
        except Exception:
            req_titles = {}
    for o in outreach_rows:
        meta = req_titles.get(o.get("requirement_id"), {})
        o["requirement_role_title"] = meta.get("role_title")
        o["requirement_client_name"] = meta.get("client_name")
    return {
        "candidate": cand,
        "details": detail_rows,
        "notes": notes,
        "shortlisted": shortlisted,
        "submissions": submissions,
        "outreach": outreach_rows,
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
    try:
        result = db.toggle_shortlist(candidate_id, user_email, note=note)
    except Exception as exc:
        if _is_missing_table_error(exc, "candidate_shortlists"):
            raise CoreError(500, SHORTLIST_SCHEMA_ERROR) from exc
        raise
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
    try:
        row = db.add_candidate_note(candidate_id, user_email, content.strip())
    except Exception as exc:
        if _is_missing_table_error(exc, "candidate_notes"):
            raise CoreError(500, SHORTLIST_SCHEMA_ERROR) from exc
        raise
    return {"status": "ok", "note": row}


def list_sequences(user_role: str, user_email: str,
                   scope: str = "mine") -> dict:
    """Return sequenced outreach emails grouped by requirement.

    scope='mine' → only emails this recruiter sent (recruiter_email=user_email)
    scope='all'  → TL sees everything, recruiter falls back to 'mine'
    """
    _require_role(user_role, ["recruiter", "tl"])
    q = db.get_client().table("outreach_log").select(
        "id, candidate_id, requirement_id, recruiter_email, email_subject, "
        "sent_at, reply_received, replied_at, outlook_thread_id"
    )
    if scope == "all" and user_role == "tl":
        pass  # no filter
    else:
        q = q.eq("recruiter_email", user_email)
    try:
        rows = q.order("sent_at", desc=True).limit(300).execute().data or []
    except Exception:
        rows = []

    if not rows:
        return {"sequences": [], "requirements": [], "count": 0}

    cand_ids = list({r.get("candidate_id") for r in rows if r.get("candidate_id")})
    req_ids = list({r.get("requirement_id") for r in rows if r.get("requirement_id")})

    cand_by_id: dict[str, dict] = {}
    req_by_id: dict[str, dict] = {}
    if cand_ids:
        try:
            for c in (db.get_client().table("candidates")
                      .select("id, name, email, current_job_title, current_employer")
                      .in_("id", cand_ids).execute().data) or []:
                cand_by_id[c["id"]] = c
        except Exception:
            cand_by_id = {}
    if req_ids:
        try:
            for r in (db.get_client().table("requirements")
                      .select("id, role_title, client_name, market, status")
                      .in_("id", req_ids).execute().data) or []:
                req_by_id[r["id"]] = r
        except Exception:
            req_by_id = {}

    sequences = []
    for o in rows:
        cand = cand_by_id.get(o.get("candidate_id"), {})
        req = req_by_id.get(o.get("requirement_id"), {})
        sequences.append({
            "id": o["id"],
            "candidate_id": o.get("candidate_id"),
            "candidate_name": cand.get("name"),
            "candidate_email": cand.get("email"),
            "candidate_role": cand.get("current_job_title"),
            "requirement_id": o.get("requirement_id"),
            "requirement_role_title": req.get("role_title"),
            "requirement_client_name": req.get("client_name"),
            "recruiter_email": o.get("recruiter_email"),
            "email_subject": o.get("email_subject"),
            "sent_at": o.get("sent_at"),
            "reply_received": bool(o.get("reply_received")),
            "replied_at": o.get("replied_at"),
        })

    # Summary per requirement for group headers
    requirements_summary = []
    for rid, req in req_by_id.items():
        req_rows = [s for s in sequences if s["requirement_id"] == rid]
        requirements_summary.append({
            "id": rid,
            "role_title": req.get("role_title"),
            "client_name": req.get("client_name"),
            "status": req.get("status"),
            "total_sent": len(req_rows),
            "replies": sum(1 for s in req_rows if s["reply_received"]),
        })
    requirements_summary.sort(key=lambda x: x.get("total_sent", 0), reverse=True)

    return {
        "sequences": sequences,
        "requirements": requirements_summary,
        "count": len(sequences),
    }


def list_user_shortlists(user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    try:
        rows = db.list_shortlists_for_user(user_email)
    except Exception as exc:
        if _is_missing_table_error(exc, "candidate_shortlists"):
            raise CoreError(500, SHORTLIST_SCHEMA_ERROR) from exc
        raise
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


def delete_user_shortlists(user_role: str, user_email: str,
                           shortlist_ids: list[str]) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    ids = [s for s in (shortlist_ids or []) if isinstance(s, str)]
    if not ids:
        raise CoreError(422, "shortlist_ids required")
    deleted = db.delete_shortlists(user_email, ids)
    return {"deleted": deleted}


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
    cand_ids = [m["candidate_id"] for m in match_rows]
    cands = (db.get_client().table("candidates").select("*")
             .in_("id", cand_ids).execute().data)
    cand_by_id = {c["id"]: c for c in cands}
    results = []
    for m in match_rows:
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
        summary = _run_async(
            _source_and_score_capped(req_id, DEFAULT_SOURCE_CAP_PER_REQ,
                                     _return_pool=True))
    except Exception as e:
        log.exception("_source_and_score_capped crashed for %s", req_id)
        raise CoreError(500, f"Sourcing failed: {e}")
    linkedin_str = sourcing.generate_linkedin_search_string(requirement)
    pool = summary.pop("_pool", [])
    _run_in_background(_screen_candidates, req_id, requirement, pool)
    ext = summary["sourced"]
    internal = summary["internal_pool_size"]
    matched = summary["top_count"]
    return {
        "sourced": ext,
        "internal_pool_size": internal,
        "top_count": matched,
        "channel_errors": summary.get("channel_errors", {}),
        "screened": "in_progress",
        "shortlisted": "in_progress",
        "linkedin_search_string": linkedin_str,
        "message": (
            f"Sourced {ext} new + {internal} from DB considered. "
            f"{matched} matched above threshold."
        ),
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
        db.insert_interview_tracker({
            "candidate_id": submission["candidate_id"],
            "requirement_id": submission["requirement_id"],
            "tender_number": requirement["tender_number"],
            "school_name": requirement.get("location"),
            "submission_date": now_ts[:10],
            "status": "Submitted",
        })
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


# ── Submissions (new dual-role endpoints) ──────────────────────

def _get_tl_emails() -> list[str]:
    """Return all TL email addresses from RECRUITER_LOGINS.
    Future-proof for multiple TLs — adding more to the map "just works"."""
    from app import RECRUITER_LOGINS
    return [u["email"] for u in RECRUITER_LOGINS.values()
            if u.get("role") == "tl" and u.get("email")]


def get_my_submissions(recruiter_email: str) -> dict:
    """Return this recruiter's submissions grouped by requirement."""
    client = db.get_client()
    # All submissions by this recruiter (any status)
    rows = (client.table("submissions")
            .select("id, candidate_id, requirement_id, submitted_at, "
                    "tl_approved, tl_approved_at, final_status, "
                    "submitted_by_recruiter, placement_type, doj, package, "
                    "sap_id, remarks")
            .eq("submitted_by_recruiter", recruiter_email)
            .order("submitted_at", desc=True)
            .execute().data)

    # Fetch requirements and candidates in bulk
    req_ids = list({r["requirement_id"] for r in rows if r.get("requirement_id")})
    cand_ids = list({r["candidate_id"] for r in rows if r.get("candidate_id")})

    reqs_map: dict = {}
    cands_map: dict = {}
    details_map: dict = {}
    if req_ids:
        for r in (client.table("requirements")
                  .select("id, role_title, client_name, market, assigned_recruiters")
                  .in_("id", req_ids).execute().data):
            reqs_map[r["id"]] = r
    if cand_ids:
        for c in (client.table("candidates")
                  .select("id, name, current_ctc, expected_ctc, notice_period, "
                          "email, phone, current_location, date_of_birth, "
                          "address_full, current_company, total_experience, "
                          "relevant_experience, preferred_location")
                  .in_("id", cand_ids).execute().data):
            cands_map[c["id"]] = c
        # Pull tl_feedback per (candidate_id, requirement_id) pair
        for d in (client.table("candidate_details")
                  .select("candidate_id, requirement_id, tl_feedback, status")
                  .in_("candidate_id", cand_ids).execute().data):
            details_map[(d["candidate_id"], d.get("requirement_id"))] = d

    # Also pull requirements assigned to this recruiter but not yet submitted
    try:
        all_reqs = (client.table("requirements")
                    .select("id, role_title, client_name, market, assigned_recruiters")
                    .eq("status", "open")
                    .execute().data)
        assigned_reqs = [r for r in all_reqs
                         if recruiter_email in (r.get("assigned_recruiters") or [])]
    except Exception:
        assigned_reqs = []
    for r in assigned_reqs:
        reqs_map.setdefault(r["id"], r)

    # Group submissions by requirement
    groups: dict = {}
    for sub in rows:
        rid = sub.get("requirement_id")
        if not rid:
            continue
        if rid not in groups:
            req = reqs_map.get(rid, {})
            groups[rid] = {"requirement": req, "submissions": []}
        cand = cands_map.get(sub.get("candidate_id"), {})
        det = details_map.get((sub.get("candidate_id"), rid), {})
        # Derive simple status for recruiter view
        if sub.get("final_status") == "rejected_by_tl":
            status = "rejected"
        elif sub.get("tl_approved"):
            status = "approved"
        else:
            status = "pending"
        groups[rid]["submissions"].append({
            **sub,
            "candidate_name": cand.get("name", ""),
            "current_ctc": cand.get("current_ctc", ""),
            "expected_ctc": cand.get("expected_ctc", ""),
            "notice_period": cand.get("notice_period", ""),
            "email": cand.get("email", ""),
            "phone": cand.get("phone", ""),
            "current_location": cand.get("current_location", ""),
            "preferred_location": cand.get("preferred_location", ""),
            "date_of_birth": cand.get("date_of_birth", ""),
            "address_full": cand.get("address_full", ""),
            "current_company": cand.get("current_company", ""),
            "total_experience": cand.get("total_experience", ""),
            "relevant_experience": cand.get("relevant_experience", ""),
            "tl_feedback": det.get("tl_feedback", ""),
            "status": status,
        })

    # Ensure every assigned requirement appears (even with 0 submissions)
    for rid, req in reqs_map.items():
        if rid not in groups:
            groups[rid] = {"requirement": req, "submissions": []}

    return {"groups": list(groups.values())}


def create_submission(payload: Any, recruiter_email: str) -> dict:
    """Recruiter submits an existing candidate for a requirement.
    Optionally patches new fields on the candidate row first."""
    body = _require_fields(payload, ["candidate_id", "requirement_id"])
    cid = body["candidate_id"]
    rid = body["requirement_id"]

    # Optional candidate field updates (new columns + existing)
    candidate_patch = {}
    for field in ("date_of_birth", "address_full", "current_company",
                  "phone", "email", "total_experience", "relevant_experience",
                  "current_ctc", "expected_ctc", "current_location",
                  "preferred_location", "notice_period", "name"):
        if body.get(field) is not None:
            candidate_patch[field] = body[field]
    if candidate_patch:
        db.get_client().table("candidates").update(candidate_patch).eq("id", cid).execute()

    # Check for duplicate submission
    existing = (db.get_client().table("submissions")
                .select("id, tl_approved, final_status")
                .eq("candidate_id", cid)
                .eq("requirement_id", rid)
                .execute().data)
    if existing:
        ex = existing[0]
        if not ex.get("tl_approved") and ex.get("final_status") != "rejected_by_tl":
            raise CoreError(409, "Candidate already submitted for this requirement")

    # Fetch requirement for client/market info
    req_rows = (db.get_client().table("requirements")
                .select("client_name, market, tender_number")
                .eq("id", rid).execute().data)
    req = req_rows[0] if req_rows else {}

    now_ts = datetime.now(timezone.utc).isoformat()
    sub_row = (db.get_client().table("submissions").insert({
        "candidate_id": cid,
        "requirement_id": rid,
        "client_name": req.get("client_name"),
        "market": req.get("market"),
        "tender_number": req.get("tender_number"),
        "submitted_by_recruiter": recruiter_email,
        "submitted_at": now_ts,
        "tl_approved": False,
    }).execute().data)

    db.upsert_candidate_details(cid, rid, {"status": "submitted_to_tl"})

    # Notify TL(s) — best-effort, must NOT block the submission insert.
    # Skip self-emails: a TL who self-assigned to a requirement and
    # submitted their own candidate shouldn't email themselves.
    try:
        tl_emails = [e for e in _get_tl_emails() if e != recruiter_email]
        if tl_emails:
            cand_row = db.get_candidate_by_id(cid) or {}
            cand_name = cand_row.get("name") or body.get("name") or "Candidate"
            role_title = req.get("role_title") if req else ""
            # role_title isn't in the select above — fetch it cheaply
            if not role_title:
                rt_rows = (db.get_client().table("requirements")
                           .select("role_title").eq("id", rid).execute().data)
                role_title = (rt_rows[0].get("role_title", "") if rt_rows else "")
            from app import RECRUITER_LOGINS
            email_to_name = {u["email"]: u["name"]
                             for u in RECRUITER_LOGINS.values()}
            recruiter_name = email_to_name.get(recruiter_email, recruiter_email)
            client_name = req.get("client_name", "") if req else ""
            market = req.get("market", "") if req else ""
            client_market = " · ".join(filter(None, [client_name, market]))
            current_ctc = cand_row.get("current_ctc") or body.get("current_ctc") or "—"
            expected_ctc = cand_row.get("expected_ctc") or body.get("expected_ctc") or "—"
            notice = cand_row.get("notice_period") or body.get("notice_period") or "—"
            subject = (f"New submission pending review: {cand_name} → "
                       f"{role_title or 'requirement'}")
            body_html = (
                f"<p>Hi,</p>"
                f"<p><b>{recruiter_name}</b> has submitted a new candidate "
                f"for your review.</p>"
                f"<ul>"
                f"<li><b>Candidate:</b> {cand_name}</li>"
                f"<li><b>Role:</b> {role_title or '—'}</li>"
                f"<li><b>Client / Market:</b> {client_market or '—'}</li>"
                f"<li><b>Current CTC:</b> {current_ctc}</li>"
                f"<li><b>Expected CTC:</b> {expected_ctc}</li>"
                f"<li><b>Notice:</b> {notice}</li>"
                f"</ul>"
                f"<p>Open the Submissions page to review and approve or reject.</p>"
            )
            for tl_email in tl_emails:
                try:
                    outlook.send_email(
                        from_email=recruiter_email,
                        to_email=tl_email,
                        subject=subject,
                        body=body_html,
                    )
                except Exception as e:
                    log.warning("TL submission notification failed for %s: %s",
                                tl_email, e)
    except Exception as e:
        log.warning("TL notification block failed: %s", e)

    return {"submission": sub_row[0] if sub_row else {}, "status": "pending"}


def get_tl_submissions(requirement_id: str | None = None) -> dict:
    """TL: all submissions grouped by requirement. Optionally filter by req."""
    client = db.get_client()

    query = (client.table("submissions")
             .select("id, candidate_id, requirement_id, submitted_by_recruiter, "
                     "submitted_at, tl_approved, tl_approved_at, final_status, "
                     "placement_type, doj, package, sap_id, remarks, "
                     "sent_to_client_at, tender_number, market"))
    if requirement_id:
        query = query.eq("requirement_id", requirement_id)
    rows = query.order("submitted_at", desc=True).execute().data

    req_ids = list({r["requirement_id"] for r in rows if r.get("requirement_id")})
    cand_ids = list({r["candidate_id"] for r in rows if r.get("candidate_id")})

    reqs_map: dict = {}
    cands_map: dict = {}
    details_map: dict = {}

    if req_ids:
        for r in (client.table("requirements")
                  .select("id, role_title, client_name, market, status")
                  .in_("id", req_ids).execute().data):
            reqs_map[r["id"]] = r
    if cand_ids:
        for c in (client.table("candidates")
                  .select("id, name, current_ctc, expected_ctc, notice_period, "
                          "email, phone, current_location, date_of_birth, "
                          "address_full, current_company, total_experience, "
                          "relevant_experience")
                  .in_("id", cand_ids).execute().data):
            cands_map[c["id"]] = c
        # Candidate details for tl_feedback
        for d in (client.table("candidate_details")
                  .select("candidate_id, requirement_id, tl_feedback, status")
                  .in_("candidate_id", cand_ids).execute().data):
            details_map[(d["candidate_id"], d.get("requirement_id"))] = d

    # Build recruiter name map
    from app import RECRUITER_LOGINS
    email_to_name = {u["email"]: u["name"] for u in RECRUITER_LOGINS.values()}

    groups: dict = {}
    for sub in rows:
        rid = sub.get("requirement_id")
        cid = sub.get("candidate_id")
        if not rid:
            continue
        if rid not in groups:
            groups[rid] = {"requirement": reqs_map.get(rid, {}), "submissions": []}
        cand = cands_map.get(cid, {})
        det = details_map.get((cid, rid), {})
        if sub.get("final_status") == "rejected_by_tl":
            status = "rejected"
        elif sub.get("tl_approved"):
            status = "approved"
        else:
            status = "pending"
        groups[rid]["submissions"].append({
            **sub,
            "candidate_name": cand.get("name", ""),
            "current_ctc": cand.get("current_ctc", ""),
            "expected_ctc": cand.get("expected_ctc", ""),
            "notice_period": cand.get("notice_period", ""),
            "email": cand.get("email", ""),
            "phone": cand.get("phone", ""),
            "current_location": cand.get("current_location", ""),
            "date_of_birth": cand.get("date_of_birth", ""),
            "address_full": cand.get("address_full", ""),
            "current_company": cand.get("current_company", ""),
            "total_experience": cand.get("total_experience", ""),
            "relevant_experience": cand.get("relevant_experience", ""),
            "tl_feedback": det.get("tl_feedback", ""),
            "recruiter_name": email_to_name.get(sub.get("submitted_by_recruiter", ""), ""),
            "status": status,
        })

    return {"groups": list(groups.values())}


# Allowed values for client-feedback final_status (post-Submitted lifecycle).
CLIENT_FEEDBACK_STATUSES = (
    "Shortlisted", "KIV", "Not Shortlisted", "Selected", "Selected-Joined",
    "Selected-Backed out", "Backed out", "Rejected",
)


def tl_set_client_feedback(payload: Any, user_role: str) -> dict:
    """TL records client-side lifecycle status on an already-sent submission."""
    _require_role(user_role, ["tl"])
    body = _require_fields(payload, ["submission_id", "final_status"])
    sub_id = body["submission_id"]
    final_status = body["final_status"]
    if final_status not in CLIENT_FEEDBACK_STATUSES:
        raise CoreError(422, f"Invalid final_status. Must be one of: "
                             f"{', '.join(CLIENT_FEEDBACK_STATUSES)}")
    rows = (db.get_client().table("submissions")
            .select("id, candidate_id, requirement_id, tl_approved, "
                    "tender_number, market")
            .eq("id", sub_id).execute().data)
    if not rows:
        raise CoreError(404, "Submission not found")
    sub = rows[0]
    if not sub.get("tl_approved"):
        raise CoreError(409, "Cannot record client feedback for a submission "
                             "that has not been approved/sent")

    placement_type = body.get("placement_type")
    doj = body.get("doj")
    package = body.get("package")
    sap_id = body.get("sap_id")
    remarks = body.get("remarks")

    if final_status == "Selected-Joined" and not doj:
        raise CoreError(422, "doj is required when final_status is "
                             "Selected-Joined")

    update_payload: dict = {"final_status": final_status}
    if placement_type is not None:
        update_payload["placement_type"] = placement_type
    if doj is not None:
        update_payload["doj"] = doj
    if package is not None:
        update_payload["package"] = package
    if sap_id is not None:
        update_payload["sap_id"] = sap_id
    if remarks is not None:
        update_payload["remarks"] = remarks
    db.get_client().table("submissions").update(update_payload).eq(
        "id", sub_id).execute()

    # SG + tender side-effect: upsert interview_tracker (a row may already
    # exist from tl_approve_and_send).
    requirement = db.get_requirement_by_id(sub["requirement_id"]) or {}
    if requirement.get("market") == "SG" and requirement.get("tender_number"):
        client = db.get_client()
        existing_tracker = (client.table("interview_tracker")
                            .select("id")
                            .eq("candidate_id", sub["candidate_id"])
                            .eq("requirement_id", sub["requirement_id"])
                            .limit(1).execute().data)
        tracker_payload: dict = {"status": final_status}
        for k in ("placement_type", "doj", "package", "sap_id", "remarks"):
            v = update_payload.get(k)
            if v is not None:
                tracker_payload[k] = v
        if existing_tracker:
            client.table("interview_tracker").update(tracker_payload).eq(
                "id", existing_tracker[0]["id"]).execute()
        else:
            tracker_payload.update({
                "candidate_id": sub["candidate_id"],
                "requirement_id": sub["requirement_id"],
                "tender_number": requirement.get("tender_number"),
                "school_name": requirement.get("location"),
            })
            db.insert_interview_tracker(tracker_payload)

    return {"status": "ok", "submission_id": sub_id,
            "final_status": final_status}


def get_performance(user_role: str, user_email: str) -> dict:
    """Performance metrics — TL sees all recruiters, recruiter sees own data."""
    client = db.get_client()
    from app import RECRUITER_LOGINS
    email_to_name = {u["email"]: u["name"] for u in RECRUITER_LOGINS.values()}

    # Build date boundaries for 30-day window
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    thirty_days_ago = (now - timedelta(days=30)).isoformat()
    yesterday = (now - timedelta(hours=24)).isoformat()

    # Fetch submissions (TL: all; recruiter: own)
    query = client.table("submissions").select(
        "id, submitted_by_recruiter, submitted_at, tl_approved, tl_approved_at, "
        "final_status")
    if user_role != "tl":
        query = query.eq("submitted_by_recruiter", user_email)
    all_subs = query.execute().data

    total = len(all_subs)
    approved = sum(1 for s in all_subs if s.get("tl_approved"))
    rejected = sum(1 for s in all_subs
                   if s.get("final_status") == "rejected_by_tl")
    pending = total - approved - rejected

    # Daily trend (last 30 days)
    from collections import Counter
    day_counter: Counter = Counter()
    for s in all_subs:
        ts = s.get("submitted_at") or ""
        if ts >= thirty_days_ago:
            day = ts[:10]  # YYYY-MM-DD
            day_counter[day] += 1
    by_day = [{"date": d, "count": c}
              for d, c in sorted(day_counter.items())]

    result: dict = {
        "summary": {"total": total, "approved": approved,
                    "rejected": rejected, "pending": pending},
        "by_day": by_day,
    }

    if user_role == "tl":
        # Per-recruiter breakdown
        rec_stats: dict = {}
        for s in all_subs:
            email = s.get("submitted_by_recruiter") or "unknown"
            if email not in rec_stats:
                rec_stats[email] = {"name": email_to_name.get(email, email),
                                    "total": 0, "approved": 0, "rejected": 0}
            rec_stats[email]["total"] += 1
            if s.get("tl_approved"):
                rec_stats[email]["approved"] += 1
            if s.get("final_status") == "rejected_by_tl":
                rec_stats[email]["rejected"] += 1
        result["by_recruiter"] = list(rec_stats.values())
    else:
        # Recent verdicts (last 24 hours) for recruiter transparency
        recent: list = []
        for s in all_subs:
            decided_at = s.get("tl_approved_at") or ""
            if decided_at and decided_at >= yesterday:
                if s.get("tl_approved"):
                    verdict = "approved"
                elif s.get("final_status") == "rejected_by_tl":
                    verdict = "rejected"
                else:
                    continue
                recent.append({"submission_id": s["id"], "verdict": verdict,
                                "decided_at": decided_at})
        result["recent_verdicts"] = recent

    return result


def get_usage() -> dict:
    """Aggregate real usage metrics from existing tables."""
    client = db.get_client()
    from datetime import date as date_type
    today = date_type.today().isoformat()

    screenings_total = 0
    screenings_today = 0
    emails_total = 0
    emails_today = 0
    matches_total = 0
    submissions_total = 0

    try:
        res = client.table("screenings").select("id, screened_at", count="exact").execute()
        screenings_total = res.count or 0
        screenings_today = sum(1 for r in (res.data or [])
                               if (r.get("screened_at") or "").startswith(today))
    except Exception:
        pass
    try:
        res = client.table("outreach_log").select("id, sent_at", count="exact").execute()
        emails_total = res.count or 0
        emails_today = sum(1 for r in (res.data or [])
                           if (r.get("sent_at") or "").startswith(today))
    except Exception:
        pass
    try:
        res = client.table("match_scores").select("id", count="exact").execute()
        matches_total = res.count or 0
    except Exception:
        pass
    try:
        res = client.table("submissions").select("id", count="exact").execute()
        submissions_total = res.count or 0
    except Exception:
        pass

    return {
        "ai_screenings_today": screenings_today,
        "ai_screenings_total": screenings_total,
        "emails_sent_today": emails_today,
        "emails_sent_total": emails_total,
        "candidates_scored_total": matches_total,
        "submissions_processed_total": submissions_total,
    }


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
                  .select("requirement_id, tl_approved, sent_to_client_at, final_status")
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
        placed_statuses = {"Selected-Joined", "Selected"}
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
            # Align frontend field names: submitted = all rows in TL queue
            "submitted": len(submissions_rows),
            # approved = sent to client
            "approved": sum(1 for s in submissions_rows
                            if s.get("sent_to_client_at")),
            # placed = final outcome (joined or selected)
            "placed": sum(1 for s in submissions_rows
                          if s.get("final_status") in placed_statuses),
            # legacy names kept for backwards compat
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
    Body: { title (required) }. New projects are always private to the creator;
    access_level/collaborators can be changed later via update_project()."""
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    title = (payload.get("title") or "").strip()
    if not title:
        raise CoreError(422, "Project title is required")

    proj = db.insert_project({
        "title": title,
        "access_level": "private",
        "status": "active",
        "created_by": user_email,
    })
    proj["collaborators"] = []
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


# ══════════════════════════════════════════════════════════════
# ── Sequences v2 — multi-step builder + AI streaming + enroll
# ══════════════════════════════════════════════════════════════

SEQUENCE_V2_SYSTEM = """\
You are an expert IT recruiter. You are drafting ONE email at a specific \
position in a multi-step sequence to a passive candidate. You will be given \
the full context (role, company, scheduling link), the position of this step, \
and the full drafts of all previous steps.

Rules:
- Under 150 words
- Warm, concrete, plain. No marketing fluff. No emoji.
- Use {{First Name}} placeholder wherever the candidate's first name goes.
- You MAY use: {{Current Company}}, {{Job Title}}, {{Sender First Name}}.
- Step 1 is a cold intro. Step 2+ are short follow-ups in the same thread \
  (leave subject empty — reply in thread).
- Return ONLY JSON — no backticks, no preamble:
  {"subject":"...","body":"...","wait_days":<int>,"send_time_local":"HH:MM"}
"""

# Suggested wait cadence used as a fallback if Claude returns nonsense
_WAIT_CADENCE = {1: 0, 2: 3, 3: 2, 4: 3, 5: 5}


def _validate_ai_config(payload: dict) -> dict:
    required = ["role", "company"]
    for f in required:
        if not (payload.get(f) or "").strip():
            raise CoreError(422, f"'{f}' is required for AI generation")
    num_steps = int(payload.get("num_steps") or 3)
    if num_steps not in (3, 4, 5):
        num_steps = 3
    return {
        "role": payload["role"].strip(),
        "company": payload["company"].strip(),
        "job_url": payload.get("job_url", ""),
        "company_url": payload.get("company_url", ""),
        "scheduling_link": payload.get("scheduling_link", ""),
        "num_steps": num_steps,
        "include_linkedin": bool(payload.get("include_linkedin")),
        "timezone": payload.get("timezone", "Asia/Kolkata"),
    }


def _build_step_user_prompt(cfg: dict, pos: int, total: int,
                            prior: list[dict], sender_email: str) -> str:
    ctx = (
        f"Role: {cfg['role']}\n"
        f"Company: {cfg['company']}\n"
    )
    if cfg.get("job_url"):
        ctx += f"Job URL: {cfg['job_url']}\n"
    if cfg.get("scheduling_link"):
        ctx += f"Scheduling link: {cfg['scheduling_link']}\n"
    ctx += f"Sender email: {sender_email}\n"
    ctx += f"\nYou are writing step {pos} of {total}.\n"
    if prior:
        ctx += "\nPrevious steps already drafted:\n"
        for i, s in enumerate(prior, 1):
            ctx += (f"\nStep {i}:\n"
                    f"  Subject: {s.get('subject', '(empty)')}\n"
                    f"  Body: {s.get('body', '')[:300]}\n"
                    f"  Wait days: {s.get('wait_days', 0)}\n")
    return ctx


def _fallback_step(cfg: dict, pos: int) -> dict:
    if pos == 1:
        return {
            "subject": f"Opportunity: {cfg['role']} at {cfg['company']}",
            "body": (
                f"Hi {{{{First Name}}}},\n\n"
                f"I'm reaching out about a {cfg['role']} opportunity at "
                f"{cfg['company']}. Your background looks like a strong match "
                f"— would you be open to a short chat?\n\nBest,\n{{{{Sender First Name}}}}"
            ),
            "wait_days": 0,
            "send_time_local": "09:00",
        }
    return {
        "subject": "",
        "body": (
            f"Hi {{{{First Name}}}},\n\nJust following up on my earlier note "
            f"about the {cfg['role']} role at {cfg['company']}. "
            f"Would love to connect — happy to keep it brief.\n\nBest,\n{{{{Sender First Name}}}}"
        ),
        "wait_days": _WAIT_CADENCE.get(pos, 3),
        "send_time_local": "09:00",
    }


def _sanitise_step(step: dict, pos: int) -> dict:
    wait = step.get("wait_days")
    if not isinstance(wait, int) or wait < 0 or wait > 30:
        wait = _WAIT_CADENCE.get(pos, 3) if pos > 1 else 0
    send_time = step.get("send_time_local", "09:00")
    if not re.match(r"^\d{2}:\d{2}$", str(send_time)):
        send_time = "09:00"
    return {
        "subject": step.get("subject", ""),
        "body": step.get("body", ""),
        "wait_days": wait,
        "send_time_local": send_time,
    }


def _linkedin_step(cfg: dict, pos: int) -> dict:
    return {
        "subject": "",
        "body": (
            f"Hi {{{{First Name}}}}, I also sent you a LinkedIn connection request "
            f"— I'd love to connect there too if email isn't the best channel. "
            f"({cfg['role']} at {cfg['company']})"
        ),
        "wait_days": 1,
        "send_time_local": "10:00",
        "step_type": "linkedin",
    }


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def generate_sequence_stream(payload: dict, user_role: str, user_email: str):
    """SSE generator — yields data: lines for the streaming endpoint."""
    _require_role(user_role, ["recruiter", "tl"])
    try:
        cfg = _validate_ai_config(payload)
    except CoreError as e:
        yield _sse({"event": "error", "message": e.message})
        return
    total = cfg["num_steps"]
    prior: list[dict] = []
    yield _sse({"event": "start", "total": total, "config": cfg})
    for pos in range(1, total + 1):
        yield _sse({"event": "writing", "step": pos, "total": total})
        try:
            raw = _call_claude(
                "claude-haiku-4-5-20251001",
                SEQUENCE_V2_SYSTEM,
                _build_step_user_prompt(cfg, pos, total, prior, user_email),
                max_tokens=900, endpoint=f"/sequences/generate:step{pos}",
            )
            step = _parse_llm_json(raw) or _fallback_step(cfg, pos)
        except Exception:
            log.exception("step %s generation failed", pos)
            step = _fallback_step(cfg, pos)
        step = _sanitise_step(step, pos)
        prior.append(step)
        yield _sse({"event": "step", "step": pos, "total": total, **step})
    if cfg.get("include_linkedin"):
        yield _sse({
            "event": "step",
            "step": total + 1, "total": total + 1,
            **_linkedin_step(cfg, total + 1),
        })
    yield _sse({"event": "done"})


# ── Variable substitution ──────────────────────────────────────

VAR_MAP = {
    "First Name":        lambda c, s: _first_name(c.get("name")),
    "Current Company":   lambda c, s: c.get("current_employer") or "your current company",
    "Job Title":         lambda c, s: c.get("current_job_title") or "your role",
    "Education":         lambda c, s: c.get("highest_education") or "your background",
    "Sender First Name": lambda c, s: _first_name(s.get("name")),
    "Sender Email":      lambda c, s: s.get("email", ""),
    "Scheduling Link":   lambda c, s: s.get("scheduling_link", ""),
}
_VAR_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _render_template(tpl: str, candidate: dict, sender: dict) -> str:
    return _VAR_RE.sub(
        lambda m: VAR_MAP.get(
            m.group(1).strip(), lambda c, s: m.group(0)
        )(candidate, sender),
        tpl or "",
    )


# ── CRUD ──────────────────────────────────────────────────────

def create_sequence(payload: dict, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    name = (payload.get("name") or "").strip()
    if not name:
        name = f"New Sequence — {datetime.now().strftime('%b %d')}"
    source = payload.get("source", "scratch")
    if source not in ("ai", "template", "scratch", "clone"):
        source = "scratch"
    seq = db.insert_sequence({
        "name": name,
        "created_by": user_email,
        "status": "draft",
        "source": source,
        "config": payload.get("config") or {},
        "project_id": payload.get("project_id") or None,
        "requirement_id": payload.get("requirement_id") or None,
    })
    steps_data = payload.get("steps") or []
    steps = []
    if steps_data:
        rows = [{
            "sequence_id": seq["id"],
            "position": i + 1,
            "step_type": s.get("step_type", "email"),
            "wait_days": s.get("wait_days", 0),
            "send_time_local": s.get("send_time_local", "09:00"),
            "subject_template": s.get("subject", ""),
            "body_template": s.get("body", ""),
            "reply_in_same_thread": i > 0,
        } for i, s in enumerate(steps_data)]
        steps = db.insert_sequence_steps(rows)
    seq["steps"] = steps
    return {"sequence": seq}


def list_sequences_v2(user_role: str, user_email: str,
                      scope: str = "mine", days: int = 7) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    seqs = db.list_sequences_for_user(user_email, user_role if scope == "all" else "recruiter")
    days = _clamp_chart_days(days)
    result = []
    agg = {"total": 0, "active": 0, "opened": 0, "clicked": 0,
           "replied": 0, "interested": 0, "bounced": 0, "sent": 0}
    for seq in seqs:
        try:
            metrics = db.count_sequence_metrics(seq["id"])
        except Exception:
            metrics = {}
        for k in agg:
            agg[k] += metrics.get(k, 0)
        try:
            step_count = len((db.get_client().table("sequence_steps")
                              .select("id").eq("sequence_id", seq["id"])
                              .execute().data) or [])
        except Exception:
            step_count = 0
        result.append({
            **seq,
            "metrics": metrics,
            "step_count": step_count,
        })
    stats = {
        "total": len(result),
        "active": sum(1 for s in result if s.get("status") == "active"),
        "opened": agg["opened"],
        "clicked": agg["clicked"],
        "replied": agg["replied"],
        "interested": agg["interested"],
        "bounced": agg["bounced"],
        "sent": agg["sent"],
        "draft": sum(1 for s in result if s.get("status") == "draft"),
    }
    chart = _build_chart_data(days=days)
    return {"stats": stats, "chart": chart, "sequences": result, "days": days}


def _clamp_chart_days(days: int) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    if d not in (7, 14, 30):
        d = 7
    return d


def _build_chart_data(days: int = 7,
                      sequence_id: str | None = None) -> dict:
    """Aggregate sent/scheduled counts over the last N days.

    If sequence_id is given, scope to that sequence by joining through
    sequence_runs.
    """
    days = _clamp_chart_days(days)
    today = datetime.now(timezone.utc).date()
    day_list = [(today - timedelta(days=i)).isoformat()
                for i in range(days - 1, -1, -1)]
    sent_map: dict[str, int] = {d: 0 for d in day_list}
    sched_map: dict[str, int] = {d: 0 for d in day_list}
    try:
        since = day_list[0] + "T00:00:00Z"
        if sequence_id:
            run_ids = [r["id"] for r in
                       (db.get_client().table("sequence_runs").select("id")
                        .eq("sequence_id", sequence_id).execute().data) or []]
            if not run_ids:
                return {"days": day_list,
                        "sent": [0] * days,
                        "scheduled": [0] * days}
            sends = (db.get_client().table("sequence_step_sends")
                     .select("status, sent_at, scheduled_for")
                     .in_("run_id", run_ids)
                     .gte("created_at", since).execute().data) or []
        else:
            sends = (db.get_client().table("sequence_step_sends")
                     .select("status, sent_at, scheduled_for")
                     .gte("created_at", since).execute().data) or []
        for row in sends:
            if row["status"] == "sent" and row.get("sent_at"):
                d = row["sent_at"][:10]
                if d in sent_map:
                    sent_map[d] += 1
            elif row["status"] == "scheduled" and row.get("scheduled_for"):
                d = row["scheduled_for"][:10]
                if d in sched_map:
                    sched_map[d] += 1
    except Exception:
        pass
    return {
        "days": day_list,
        "sent": [sent_map[d] for d in day_list],
        "scheduled": [sched_map[d] for d in day_list],
    }


def get_sequence_detail(seq_id: str, user_role: str, user_email: str,
                        days: int = 7) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    seq = db.get_sequence_full(seq_id)
    if not seq:
        raise CoreError(404, "Sequence not found")
    days = _clamp_chart_days(days)
    try:
        runs = (db.get_client().table("sequence_runs")
                .select(
                    "id, candidate_id, status, current_step_position, "
                    "started_at, next_send_at, enrolled_by, intent, "
                    "candidates(id, name, email, current_job_title)"
                )
                .eq("sequence_id", seq_id)
                .order("created_at", desc=True)
                .execute().data) or []
    except Exception:
        runs = []
    # Per-run engagement counts (Opened ×N · Clicked ×M).
    try:
        engagement = db.count_run_engagement([r["id"] for r in runs])
    except Exception:
        engagement = {}
    for r in runs:
        r["engagement"] = engagement.get(r["id"], {"opened": 0, "clicked": 0})
    metrics = {}
    try:
        metrics = db.count_sequence_metrics(seq_id)
    except Exception:
        pass
    chart = _build_chart_data(days=days, sequence_id=seq_id)
    return {"sequence": seq, "runs": runs, "metrics": metrics,
            "chart": chart, "days": days}


def update_sequence(seq_id: str, patch: dict,
                    user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    seq = db.get_sequence_full(seq_id)
    if not seq:
        raise CoreError(404, "Sequence not found")
    allowed = {k: v for k, v in patch.items()
               if k in ("name", "status", "config",
                        "is_pinned", "is_starred")}
    if not allowed:
        raise CoreError(422, "Nothing to update")
    if "status" in allowed and allowed["status"] not in (
            "draft", "active", "paused", "archived"):
        raise CoreError(422, "Invalid status")
    if "is_pinned" in allowed:
        allowed["is_pinned"] = bool(allowed["is_pinned"])
        allowed["pinned_at"] = (datetime.now(timezone.utc).isoformat()
                                if allowed["is_pinned"] else None)
    if "is_starred" in allowed:
        allowed["is_starred"] = bool(allowed["is_starred"])
    updated = db.update_sequence_row(seq_id, allowed)
    return {"sequence": updated}


def clone_sequence(seq_id: str, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    src = db.get_sequence_full(seq_id)
    if not src:
        raise CoreError(404, "Sequence not found")
    new_seq = db.clone_sequence_row(seq_id, user_email)
    return {"sequence": new_seq}


def update_step(seq_id: str, step_id: str, patch: dict,
                user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    allowed_keys = {"subject_template", "body_template",
                    "wait_days", "send_time_local", "step_type",
                    "signature_id", "include_unsubscribe"}
    allowed = {k: v for k, v in patch.items() if k in allowed_keys}
    if "signature_id" in allowed and allowed["signature_id"]:
        sig = db.get_signature(allowed["signature_id"])
        if not sig or sig.get("user_email") != user_email:
            raise CoreError(403, "Signature does not belong to you")
    if not allowed:
        raise CoreError(422, "Nothing to update")
    updated = db.update_step_row(step_id, allowed)
    db.update_sequence_row(seq_id, {})  # bump updated_at
    return {"step": updated}


def reorder_steps(seq_id: str, ordered_step_ids: list[str],
                  user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    client = db.get_client()
    # Set positions to negatives to dodge unique constraint, then set final
    for i, sid in enumerate(ordered_step_ids):
        client.table("sequence_steps").update({"position": -(i + 1)}).eq("id", sid).execute()
    for i, sid in enumerate(ordered_step_ids):
        client.table("sequence_steps").update({"position": i + 1}).eq("id", sid).execute()
    db.update_sequence_row(seq_id, {})
    return {"reordered": True}


def delete_sequence(seq_id: str, user_role: str, user_email: str,
                    hard: bool = False) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    seq = db.get_sequence_full(seq_id)
    if not seq:
        raise CoreError(404, "Sequence not found")
    if hard:
        db.delete_sequence_row(seq_id)
        return {"deleted": seq_id}
    db.archive_sequence(seq_id)
    return {"archived": seq_id}


def create_step(seq_id: str, payload: dict,
                user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    seq = db.get_sequence_full(seq_id)
    if not seq:
        raise CoreError(404, "Sequence not found")
    sig_id = (payload or {}).get("signature_id")
    if sig_id:
        sig = db.get_signature(sig_id)
        if not sig or sig.get("user_email") != user_email:
            raise CoreError(403, "Signature does not belong to you")
    new_step = db.insert_step_row(seq_id, payload or {})
    db.update_sequence_row(seq_id, {})
    return {"step": new_step}


def preview_step1_for_candidates(seq_id: str, payload: dict,
                                  user_role: str, user_email: str) -> list[dict]:
    _require_role(user_role, ["recruiter", "tl"])
    candidate_ids = (payload or {}).get("candidate_ids") or []
    if not candidate_ids:
        raise CoreError(422, "candidate_ids required")
    seq = db.get_sequence_full(seq_id)
    if not seq:
        raise CoreError(404, "Sequence not found")
    if not seq.get("steps"):
        raise CoreError(422, "Sequence has no steps")
    step1 = seq["steps"][0]
    # Fetch user info for sender substitution
    sender = {"email": user_email, "name": user_email.split("@")[0].title()}
    sched = seq.get("config", {}).get("scheduling_link", "")
    if sched:
        sender["scheduling_link"] = sched
    cands = (db.get_client().table("candidates")
             .select("id, name, email, current_job_title, current_employer")
             .in_("id", candidate_ids).execute().data) or []
    results = []
    for c in cands:
        issues = []
        if not c.get("email"):
            issues.append("no_email")
        rendered_subject = _render_template(step1.get("subject_template", ""), c, sender)
        rendered_body = _render_template(step1.get("body_template", ""), c, sender)
        results.append({
            "candidate_id": c["id"],
            "name": c.get("name"),
            "email": c.get("email"),
            "issues": issues,
            "rendered_subject": rendered_subject,
            "rendered_body": rendered_body,
        })
    return results


def enroll_candidates(seq_id: str, payload: dict,
                      user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    enrollments = (payload or {}).get("enrollments") or []
    if not enrollments:
        raise CoreError(422, "enrollments list required")
    seq = db.get_sequence_full(seq_id)
    if not seq:
        raise CoreError(404, "Sequence not found")
    steps = seq.get("steps") or []
    if not steps:
        raise CoreError(422, "Sequence has no steps")
    step1 = steps[0]
    sender = {"email": user_email, "name": user_email.split("@")[0].title()}
    sched = seq.get("config", {}).get("scheduling_link", "")
    if sched:
        sender["scheduling_link"] = sched

    client = db.get_client()
    now_utc = datetime.now(timezone.utc)
    results = []
    for idx, enroll in enumerate(enrollments):
        cid = enroll.get("candidate_id")
        to_email = enroll.get("to_email")
        if not cid or not to_email:
            results.append({"candidate_id": cid, "enrolled": False,
                            "error": "missing candidate_id or to_email"})
            continue
        try:
            cand = db.get_candidate_by_id(cid) or {}
            subject = enroll.get("subject") or _render_template(
                step1.get("subject_template", ""), cand, sender)
            body = enroll.get("body") or _render_template(
                step1.get("body_template", ""), cand, sender)
            # Stagger sends: candidate i sends at T + 2*i minutes
            send_at = now_utc + timedelta(minutes=2 * idx)
            # Enroll run (upsert — skip if already enrolled)
            existing_run = (client.table("sequence_runs")
                            .select("id, status")
                            .eq("sequence_id", seq_id)
                            .eq("candidate_id", cid)
                            .execute().data)
            if existing_run:
                results.append({"candidate_id": cid, "enrolled": False,
                                "error": "already enrolled"})
                continue
            run_row = (client.table("sequence_runs").insert({
                "sequence_id": seq_id,
                "candidate_id": cid,
                "from_email": user_email,
                "status": "active",
                "current_step_position": 1,
                "started_at": now_utc.isoformat(),
                "enrolled_by": user_email,
            }).execute().data[0])
            run_id = run_row["id"]
            # Send step 1 via Graph
            sent = outlook.send_email(
                from_email=user_email,
                to_email=to_email,
                subject=subject,
                body=body,
            )
            # outreach_log row (inbox scanner depends on this)
            req_id = seq.get("requirement_id")
            log_row = db.insert_outreach_log({
                "candidate_id": cid,
                "requirement_id": req_id,
                "recruiter_email": user_email,
                "outlook_message_id": sent.get("message_id"),
                "outlook_thread_id": sent.get("thread_id"),
                "email_subject": subject,
                "sent_at": sent.get("sent_at"),
                "sequence_run_id": run_id,
                "sequence_step_id": step1["id"],
            })
            # sequence_step_sends — step 1 sent
            client.table("sequence_step_sends").insert({
                "run_id": run_id,
                "step_id": step1["id"],
                "step_position": 1,
                "outreach_log_id": log_row["id"],
                "status": "sent",
                "scheduled_for": sent.get("sent_at"),
                "sent_at": sent.get("sent_at"),
            }).execute()
            # sequence_run_events
            client.table("sequence_run_events").insert({
                "run_id": run_id, "step_id": step1["id"],
                "event_type": "sent",
            }).execute()
            # Schedule step 2 if it exists (Phase D will actually send it)
            if len(steps) > 1:
                step2 = steps[1]
                wait_days = step2.get("wait_days", 3)
                next_send = (now_utc + timedelta(days=wait_days)).isoformat()
                client.table("sequence_step_sends").insert({
                    "run_id": run_id,
                    "step_id": step2["id"],
                    "step_position": 2,
                    "status": "scheduled",
                    "scheduled_for": next_send,
                }).execute()
                client.table("sequence_runs").update({
                    "next_send_at": next_send,
                    "current_step_position": 1,
                }).eq("id", run_id).execute()
            results.append({
                "candidate_id": cid, "enrolled": True,
                "run_id": run_id, "sent_at": sent.get("sent_at"),
            })
        except Exception as exc:
            log.exception("enroll candidate %s failed", cid)
            results.append({"candidate_id": cid, "enrolled": False, "error": str(exc)})
    # Activate sequence if it was draft
    if seq.get("status") == "draft" and any(r["enrolled"] for r in results):
        try:
            db.update_sequence_row(seq_id, {"status": "active"})
        except Exception:
            pass
    return {
        "enrolled": sum(1 for r in results if r.get("enrolled")),
        "results": results,
    }


def sequence_tick(user_role: str | None = None) -> dict:
    """Process due sequence sends. Called by scheduler (role=None) or manually (role=tl)."""
    if user_role is not None:
        _require_role(user_role, ["tl"])

    client = db.get_client()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Query due sends joined to active runs
    due_rows = (client.table("sequence_step_sends")
                .select("*, sequence_runs!inner(id, sequence_id, candidate_id, from_email, status)")
                .eq("status", "scheduled")
                .lte("scheduled_for", now_iso)
                .eq("sequence_runs.status", "active")
                .limit(50)
                .execute().data)

    sent_count = 0
    fail_count = 0
    skip_count = 0
    errors = []
    base_url = _public_base_url()

    for send_row in due_rows:
        send_id = send_row["id"]
        run = send_row["sequence_runs"]
        run_id = run["id"]
        seq_id = run["sequence_id"]
        cid = run["candidate_id"]
        from_email = run["from_email"]

        try:
            # a. Fetch candidate
            cand = db.get_candidate_by_id(cid) or {}
            to_email = cand.get("email")
            if not to_email:
                raise ValueError(f"Candidate {cid} has no email")

            # a1. Skip if recipient is in the global unsubscribe list.
            if db.is_email_unsubscribed(to_email):
                client.table("sequence_step_sends").update({
                    "status": "skipped",
                    "error_message": "unsubscribed",
                }).eq("id", send_id).execute()
                db.update_run_status(run_id, "paused", finished=False)
                db.skip_scheduled_sends(run_id, reason="unsubscribed")
                skip_count += 1
                continue

            # b. Fetch sequence + steps
            seq = db.get_sequence_full(seq_id)
            if not seq:
                raise ValueError(f"Sequence {seq_id} not found")
            steps = seq.get("steps") or []
            step_id = send_row["step_id"]
            step_pos = send_row["step_position"]
            step = next((s for s in steps if s["id"] == step_id), None)
            if not step:
                raise ValueError(f"Step {step_id} not found in sequence")

            # c. Render subject/body
            sender = {"email": from_email, "name": from_email.split("@")[0].title()}
            sched_link = seq.get("config", {}).get("scheduling_link", "")
            if sched_link:
                sender["scheduling_link"] = sched_link
            subject = _render_template(step.get("subject_template", ""), cand, sender)
            body = _render_template(step.get("body_template", ""), cand, sender)

            # c1. Append signature (if step has one assigned).
            sig_id = step.get("signature_id")
            if sig_id:
                sig = db.get_signature(sig_id)
                if sig:
                    body = f"{body}<br><br>{sig.get('html_body', '')}"

            # c2. Append unsubscribe footer (before tracking so the link
            # doesn't get rewritten through /track/click).
            if step.get("include_unsubscribe"):
                body = body + _build_unsubscribe_footer(run_id, to_email, base_url)

            # c3. Wrap remaining links + inject tracking pixel.
            tracking_token = uuid.uuid4().hex
            body = _rewrite_links_for_tracking(body, tracking_token, base_url)
            body = _inject_tracking_pixel(body, tracking_token, base_url)

            # d. Send email
            sent = outlook.send_email(
                from_email=from_email,
                to_email=to_email,
                subject=subject,
                body=body,
            )

            # e. Insert outreach_log
            req_id = seq.get("requirement_id")
            log_row = db.insert_outreach_log({
                "candidate_id": cid,
                "requirement_id": req_id,
                "recruiter_email": from_email,
                "outlook_message_id": sent.get("message_id"),
                "outlook_thread_id": sent.get("thread_id"),
                "email_subject": subject,
                "sent_at": sent.get("sent_at"),
                "sequence_run_id": run_id,
                "sequence_step_id": step_id,
                "tracking_token": tracking_token,
            })

            # f. Update sequence_step_sends → sent
            client.table("sequence_step_sends").update({
                "status": "sent",
                "sent_at": sent.get("sent_at"),
                "outreach_log_id": log_row["id"],
            }).eq("id", send_id).execute()

            # g. Insert sequence_run_events
            client.table("sequence_run_events").insert({
                "run_id": run_id,
                "step_id": step_id,
                "event_type": "sent",
            }).execute()

            # h. Check for next step
            next_pos = step_pos + 1
            next_step = next((s for s in steps if s.get("position") == next_pos), None)
            if next_step:
                wait_days = next_step.get("wait_days", 3)
                next_send_at = (datetime.now(timezone.utc) + timedelta(days=wait_days)).isoformat()
                client.table("sequence_step_sends").insert({
                    "run_id": run_id,
                    "step_id": next_step["id"],
                    "step_position": next_pos,
                    "status": "scheduled",
                    "scheduled_for": next_send_at,
                }).execute()
                client.table("sequence_runs").update({
                    "current_step_position": step_pos,
                    "next_send_at": next_send_at,
                }).eq("id", run_id).execute()
            else:
                # Sequence complete for this candidate
                client.table("sequence_runs").update({
                    "status": "completed",
                    "current_step_position": step_pos,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "next_send_at": None,
                }).eq("id", run_id).execute()

            sent_count += 1

        except Exception as exc:
            log.exception("sequence_tick send %s failed", send_id)
            fail_count += 1
            errors.append({"send_id": send_id, "error": str(exc)})
            # Mark send as failed
            try:
                client.table("sequence_step_sends").update({
                    "status": "failed",
                    "error_message": str(exc)[:500],
                }).eq("id", send_id).execute()
            except Exception:
                pass

    return {
        "ok": True,
        "processed": len(due_rows),
        "sent": sent_count,
        "skipped": skip_count,
        "failed": fail_count,
        "errors": errors,
    }


# ── Sequence tracking + unsubscribe + signatures ──────────────
#
# Tracking pixel and click-redirect URLs are anchored at PUBLIC_BASE_URL
# (e.g. https://app.example.com). Token = outreach_log.tracking_token (uuid).
# Unsubscribe uses an HMAC-signed token so a random visitor can't opt out
# arbitrary emails.

import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac

# 1×1 transparent GIF (43 bytes).
_PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def _public_base_url() -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


def _unsub_secret() -> bytes:
    return (os.environ.get("UNSUBSCRIBE_SECRET")
            or os.environ.get("SECRET_KEY")
            or "dev-unsub-secret").encode("utf-8")


def _build_unsub_token(run_id: str, email: str) -> str:
    """Compact urlsafe-b64 token: <run_id>.<email>.<sig>."""
    payload = f"{run_id}|{email.lower()}"
    sig = _hmac.new(_unsub_secret(), payload.encode("utf-8"),
                    _hashlib.sha256).digest()
    raw = payload.encode("utf-8") + b"." + sig[:16]
    return _b64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _verify_unsub_token(token: str) -> dict | None:
    try:
        pad = "=" * (-len(token) % 4)
        raw = _b64.urlsafe_b64decode(token + pad)
        body, sig_part = raw.rsplit(b".", 1)
        run_id, email = body.decode("utf-8").split("|", 1)
        expected = _hmac.new(_unsub_secret(), body, _hashlib.sha256).digest()[:16]
        if not _hmac.compare_digest(expected, sig_part):
            return None
        return {"run_id": run_id, "email": email}
    except Exception:
        return None


def _inject_tracking_pixel(html: str, token: str, base_url: str) -> str:
    if not base_url or not token:
        return html
    pixel = (f'<img src="{base_url}/track/open/{token}.gif" '
             f'width="1" height="1" alt="" '
             f'style="display:none;border:0" />')
    if "</body>" in html:
        return html.replace("</body>", pixel + "</body>", 1)
    return html + pixel


def _rewrite_links_for_tracking(html: str, token: str, base_url: str) -> str:
    """Rewrite each <a href="X"> to /track/click/<token>?u=<b64 X>.
    Skips mailto:, tel:, and the unsubscribe link (already on PUBLIC_BASE_URL).
    """
    if not base_url or not token or not html:
        return html

    skip_prefixes = ("mailto:", "tel:", f"{base_url}/track/",
                     f"{base_url}/unsubscribe")

    def _rewrite(match: re.Match) -> str:
        prefix, quote, url, suffix = match.group(1), match.group(2), match.group(3), match.group(4)
        if any(url.startswith(p) for p in skip_prefixes):
            return match.group(0)
        b64 = _b64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
        new_url = f"{base_url}/track/click/{token}?u={b64}"
        return f'{prefix}{quote}{new_url}{quote}{suffix}'

    pattern = re.compile(r'(<a[^>]*\shref=)(["\'])(.*?)(\2[^>]*>)', re.IGNORECASE)
    return pattern.sub(_rewrite, html)


def _build_unsubscribe_footer(run_id: str, email: str, base_url: str) -> str:
    if not base_url:
        return ""
    token = _build_unsub_token(run_id, email)
    return (
        '<p style="font-size:11px;color:#888;margin-top:24px;'
        'font-family:Arial,Helvetica,sans-serif">'
        "If you'd rather not hear from us, "
        f'<a href="{base_url}/unsubscribe?t={token}" '
        'style="color:#888;text-decoration:underline">unsubscribe here</a>.'
        "</p>"
    )


def track_open(token: str) -> tuple[bytes, str]:
    """Public — record an open event and return the 1×1 GIF.

    Returns (gif_bytes, content_type). Always returns a pixel even on lookup
    miss so the recipient client never shows a broken image.
    """
    try:
        log_row = db.get_outreach_log_by_token(token)
        if log_row and log_row.get("sequence_run_id"):
            run_id = log_row["sequence_run_id"]
            # Only the first open per run counts toward the "Opened" metric.
            if not db.has_run_event(run_id, "opened"):
                db.insert_run_event(
                    run_id, "opened",
                    step_id=log_row.get("sequence_step_id"),
                    metadata={"outreach_log_id": log_row.get("id")},
                )
    except Exception:
        log.exception("track_open failed for token=%s", token)
    return _PIXEL_GIF, "image/gif"


def track_click(token: str, url_b64: str) -> str:
    """Public — record a click and return the original URL to 302 to."""
    try:
        pad = "=" * (-len(url_b64) % 4)
        original = _b64.urlsafe_b64decode(url_b64 + pad).decode("utf-8")
    except Exception:
        original = "/"
    try:
        log_row = db.get_outreach_log_by_token(token)
        if log_row and log_row.get("sequence_run_id"):
            db.insert_run_event(
                log_row["sequence_run_id"], "clicked",
                step_id=log_row.get("sequence_step_id"),
                metadata={"url": original[:500],
                          "outreach_log_id": log_row.get("id")},
            )
    except Exception:
        log.exception("track_click failed for token=%s", token)
    if not original.startswith(("http://", "https://", "/")):
        original = "https://" + original
    return original


def unsubscribe_view(token: str) -> dict:
    """Public — return data for the unsubscribe confirmation page."""
    decoded = _verify_unsub_token(token)
    if not decoded:
        raise CoreError(400, "Invalid or expired unsubscribe link")
    return {
        "email": decoded["email"],
        "run_id": decoded["run_id"],
        "already_unsubscribed": db.is_email_unsubscribed(decoded["email"]),
    }


def unsubscribe_commit(token: str) -> dict:
    """Public — record the opt-out and pause any open runs for this email."""
    decoded = _verify_unsub_token(token)
    if not decoded:
        raise CoreError(400, "Invalid or expired unsubscribe link")
    email = decoded["email"]
    run_id = decoded["run_id"]
    db.insert_unsubscribe(email, source="link_click", sequence_run_id=run_id,
                          metadata={"token": token[:32]})
    # Halt the originating run + any other open runs for this email.
    try:
        client = db.get_client()
        cand = (client.table("candidates").select("id")
                .eq("email", email).limit(1).execute().data) or []
        cand_id = cand[0]["id"] if cand else None
        runs_q = client.table("sequence_runs").select("id, status")
        if cand_id:
            runs_q = runs_q.eq("candidate_id", cand_id)
        else:
            runs_q = runs_q.eq("id", run_id)
        runs = runs_q.execute().data or []
        for r in runs:
            if r["status"] in ("active", "paused"):
                db.update_run_status(r["id"], "paused", finished=False)
                db.skip_scheduled_sends(r["id"], reason="unsubscribed")
                db.insert_run_event(r["id"], "unsubscribed",
                                    metadata={"email": email})
    except Exception:
        log.exception("unsubscribe halt-runs failed for %s", email)
    return {"ok": True, "email": email}


# ── Signatures CRUD ────────────────────────────────────────────

def list_signatures_for_user(user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    return {"signatures": db.list_signatures(user_email)}


def create_signature(payload: dict, user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    name = (payload or {}).get("name", "").strip()
    html_body = (payload or {}).get("html_body", "").strip()
    is_default = bool((payload or {}).get("is_default"))
    if not name:
        raise CoreError(422, "Signature name is required")
    if not html_body:
        raise CoreError(422, "Signature body is required")
    sig = db.insert_signature(user_email, name, html_body, is_default)
    return {"signature": sig}


def update_signature_handler(sig_id: str, payload: dict,
                             user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    allowed = {k: v for k, v in (payload or {}).items()
               if k in ("name", "html_body", "is_default")}
    if not allowed:
        raise CoreError(422, "Nothing to update")
    existing = db.get_signature(sig_id)
    if not existing or existing.get("user_email") != user_email:
        raise CoreError(404, "Signature not found")
    sig = db.update_signature(sig_id, user_email, allowed)
    return {"signature": sig}


def delete_signature_handler(sig_id: str, user_role: str,
                             user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    existing = db.get_signature(sig_id)
    if not existing or existing.get("user_email") != user_email:
        raise CoreError(404, "Signature not found")
    db.delete_signature(sig_id, user_email)
    return {"deleted": sig_id}


# ── Test send (Preview & test button in the editor) ───────────

def test_send_step(seq_id: str, payload: dict,
                   user_role: str, user_email: str) -> dict:
    """Send a real test email to the current user using a step's draft body.

    Body and subject can be either a saved step (`step_id`) or live edits
    from the editor (`subject`, `body`). No outreach_log row, no run, no
    tracking pixel — just an email to confirm formatting.
    """
    _require_role(user_role, ["recruiter", "tl"])
    seq = db.get_sequence_full(seq_id)
    if not seq:
        raise CoreError(404, "Sequence not found")
    payload = payload or {}
    subject = payload.get("subject")
    body = payload.get("body")
    signature_id = payload.get("signature_id")
    step_id = payload.get("step_id")
    if (not subject or not body) and step_id:
        step = next((s for s in (seq.get("steps") or []) if s["id"] == step_id), None)
        if step:
            subject = subject or step.get("subject_template") or ""
            body = body or step.get("body_template") or ""
            signature_id = signature_id or step.get("signature_id")
    if not body:
        raise CoreError(422, "Body is required for test send")

    sender = {"email": user_email,
              "name": user_email.split("@")[0].title()}
    sched = (seq.get("config") or {}).get("scheduling_link", "")
    if sched:
        sender["scheduling_link"] = sched
    sample_candidate = {
        "name": sender["name"],
        "first_name": sender["name"].split(" ")[0],
        "current_employer": "Acme Inc.",
        "current_job_title": "Senior Engineer",
        "education": "B.Tech",
        "email": user_email,
    }
    rendered_subject = _render_template(subject or "(no subject)",
                                        sample_candidate, sender)
    rendered_body = _render_template(body, sample_candidate, sender)

    if signature_id:
        sig = db.get_signature(signature_id)
        if sig and sig.get("user_email") == user_email:
            rendered_body = f"{rendered_body}<br><br>{sig['html_body']}"

    test_subject = f"[TEST] {rendered_subject}"
    try:
        outlook.send_email(
            from_email=user_email,
            to_email=user_email,
            subject=test_subject,
            body=rendered_body,
        )
    except Exception as exc:
        raise CoreError(502, f"Send failed: {exc}")
    return {"ok": True, "to": user_email, "subject": test_subject}


# ── Agentic Boost ──────────────────────────────────────────────
#
# One-click pipeline: recruiter pastes a JD, five agents run in sequence and
# stream progress back over SSE. Each run auto-creates a `requirements` row
# (source='agentic_boost') so all existing dashboards (pipeline, shortlist,
# sequences, submissions) light up automatically with no extra UI plumbing.
#
# Agents (in order):
#   1. JD Parser         — claude-sonnet-4   → structured JD JSON
#   2. Boolean Builder   — claude-haiku-4-5  → boolean_string + apollo_params
#   3. Sourcing          — asyncio.gather(Apollo, Internal DB)
#   4. Screener          — _score_candidate_batch (claude-sonnet-4) → top 50
#   5. Outreach Drafter  — draft_sequence (claude-haiku-4-5) → outreach_log
#                          rows with status='draft'

BOOST_TOP_N = 50
BOOST_JD_MIN = 50
BOOST_JD_MAX = 30000


def _parse_jd_agent(jd_text: str, market: str) -> dict:
    """Run the JD Parser agent (claude-sonnet-4) over raw JD text."""
    prompt = AGENTS.get("jd_parser", "")
    raw = _call_claude(
        "claude-sonnet-4-20250514", prompt,
        f"Market: {market}\n\nJD text:\n{jd_text}",
        max_tokens=2048, endpoint="/agentic-boost/jd-parse",
    )
    return _parse_llm_json(raw) or {}


def _boolean_builder_agent(jd_parsed: dict) -> dict:
    """Run the Boolean Builder agent. Falls back to the heuristic
    sourcing.generate_linkedin_search_string() if the LLM returns nothing
    usable."""
    prompt = AGENTS.get("boolean_builder", "")
    if not prompt:
        # Agent prompt not loaded — straight fallback.
        return _boolean_builder_fallback(jd_parsed)
    raw = _call_claude(
        "claude-haiku-4-5-20251001", prompt,
        f"Parsed JD:\n{json.dumps(jd_parsed, indent=2)}",
        max_tokens=1024, endpoint="/agentic-boost/boolean",
    )
    parsed = _parse_llm_json(raw)
    if not isinstance(parsed, dict) or not parsed.get("boolean_string"):
        return _boolean_builder_fallback(jd_parsed)
    return parsed


def _boolean_builder_fallback(jd_parsed: dict) -> dict:
    """Heuristic boolean + minimal apollo_params when the LLM agent fails."""
    market = jd_parsed.get("market") or "IN"
    skills = jd_parsed.get("skills_required") or []
    location = jd_parsed.get("location") or ""
    boolean_string = sourcing.generate_linkedin_search_string({
        "skills_required": skills,
        "location": location,
        "market": market,
        "experience_min": jd_parsed.get("experience_min"),
    })
    return {
        "boolean_string": boolean_string,
        "apollo_params": {
            "q_keywords": " ".join(skills),
            "person_locations": [location or
                                 ("Singapore" if market == "SG" else "India")],
        },
        "linkedin_url": "",
    }


async def _internal_db_search(requirement: dict, limit: int = 200) -> list[dict]:
    """Internal-DB sourcing path — fetch a broad market+location pool and
    pre-filter by skill overlap so the screener only spends tokens on
    plausibly-relevant candidates."""
    pool = db.search_candidates_broad(
        market=requirement.get("market"),
        location=requirement.get("location"),
        limit=limit,
    )
    req_skills = {s.lower() for s in (requirement.get("skills_required") or [])}
    if not req_skills:
        return pool
    overlap = [
        c for c in pool
        if req_skills & {(s or "").lower() for s in (c.get("skills") or [])}
    ]
    # If skill overlap returns nothing, fall back to the full broad pool —
    # the screener can still find diamonds in the rough.
    return overlap or pool


def _enrich_top_candidates(top: list[dict], requirement_id: str,
                           user_email: str) -> list[dict]:
    """Merge match_scores rows with candidate/outreach/shortlist context.

    Used by both the live pipeline (`launch_agentic_boost_stream`) and the
    rehydrate path (`get_agentic_boost_run`) so the shape of a `top_candidate`
    is identical regardless of source. Three batched `.in_()` queries per call
    (candidates + outreach_log + candidate_shortlists) — constant regardless
    of len(top). Aliases `id` from `candidate_id` so the Searches renderers
    (which read `c.id`) work without modification.
    """
    if not top:
        return []
    top_ids = [t["candidate_id"] for t in top if t.get("candidate_id")]
    if not top_ids:
        return []
    client = db.get_client()
    try:
        cand_rows = (client.table("candidates")
                     .select("id, name, email, phone, current_job_title, "
                             "current_employer, current_location, skills, "
                             "do_not_email, do_not_call")
                     .in_("id", top_ids).execute().data) or []
    except Exception:
        log.exception("boost enrich: candidates fetch failed")
        cand_rows = []
    cands_by_id = {c["id"]: c for c in cand_rows}

    # Latest outreach row per candidate (for this requirement). Sorted desc so
    # the first row we see per candidate_id wins.
    drafts_by_cand: dict[str, dict] = {}
    try:
        outreach_rows = (client.table("outreach_log")
                         .select("id, candidate_id, email_subject, "
                                 "email_body, status, sent_at, created_at")
                         .eq("requirement_id", requirement_id)
                         .in_("candidate_id", top_ids)
                         .order("created_at", desc=True)
                         .execute().data) or []
    except Exception:
        log.exception("boost enrich: outreach fetch failed")
        outreach_rows = []
    for row in outreach_rows:
        cid = row.get("candidate_id")
        if cid and cid not in drafts_by_cand:
            drafts_by_cand[cid] = row

    shortlisted_ids: set[str] = set()
    try:
        sl_rows = (client.table("candidate_shortlists")
                   .select("candidate_id")
                   .eq("user_email", user_email)
                   .in_("candidate_id", top_ids)
                   .execute().data) or []
        shortlisted_ids = {r["candidate_id"] for r in sl_rows
                           if r.get("candidate_id")}
    except Exception:
        log.exception("boost enrich: shortlist fetch failed")

    out: list[dict] = []
    for t in top:
        cid = t.get("candidate_id")
        cand = cands_by_id.get(cid)
        if not cand:
            continue
        merged = {**cand, **t, "id": cid,
                  "shortlisted": cid in shortlisted_ids}
        latest = drafts_by_cand.get(cid)
        if latest:
            merged["draft_id"] = latest["id"]
            merged["draft_subject"] = latest.get("email_subject")
            merged["draft_body"] = latest.get("email_body")
            merged["draft_status"] = latest.get("status")
            merged["draft_sendable"] = (latest.get("status") == "draft")
        else:
            merged.setdefault("draft_sendable", False)
        out.append(merged)
    return out


def launch_agentic_boost_stream(payload: dict, user_role: str, user_email: str):
    """SSE generator — drives the 5-agent Agentic Boost pipeline.

    Yields `data: {json}\\n\\n` lines (see _sse). The Flask route wraps this
    in a streaming Response.
    """
    try:
        _require_role(user_role, ["recruiter", "tl"])
    except CoreError as e:
        yield _sse({"event": "error", "message": e.message})
        return

    payload = payload or {}
    jd_text = (payload.get("jd_text") or "").strip()
    market = _normalize_market(payload.get("market")) or "IN"

    if len(jd_text) < BOOST_JD_MIN:
        yield _sse({"event": "error",
                    "message": f"JD text too short (min {BOOST_JD_MIN} chars)"})
        return
    if len(jd_text) > BOOST_JD_MAX:
        yield _sse({"event": "error",
                    "message": f"JD text too long (max {BOOST_JD_MAX} chars)"})
        return

    try:
        boost_row = db.insert_boost_run({
            "created_by": user_email,
            "jd_text": jd_text,
            "status": "running",
        })
    except Exception as exc:
        log.exception("agentic boost: failed to insert run row")
        yield _sse({"event": "error",
                    "message": f"Could not start boost run: {exc}"})
        return

    boost_id = boost_row["id"]
    yield _sse({"event": "boost_start",
                "boost_id": boost_id, "total_agents": 5})

    # ── Agent 1: JD Parser ─────────────────────────────────────
    yield _sse({"event": "agent_start", "agent": "jd_parser",
                "idx": 1, "label": "Parsing JD"})
    try:
        jd_parsed = _parse_jd_agent(jd_text, market)
    except Exception as exc:
        log.exception("agentic boost: jd_parser failed")
        yield _sse({"event": "agent_error", "agent": "jd_parser",
                    "message": str(exc)})
        yield _sse({"event": "error",
                    "message": "Pipeline aborted — JD parsing failed"})
        try:
            db.update_boost_run(boost_id, {"status": "failed"})
        except Exception:
            pass
        return
    if not jd_parsed:
        yield _sse({"event": "agent_error", "agent": "jd_parser",
                    "message": "JD parser returned no structured fields"})
        yield _sse({"event": "error",
                    "message": "Pipeline aborted — JD parsing returned nothing"})
        try:
            db.update_boost_run(boost_id, {"status": "failed"})
        except Exception:
            pass
        return
    yield _sse({"event": "agent_done", "agent": "jd_parser",
                "payload": jd_parsed})

    # ── Auto-create requirement ────────────────────────────────
    req_payload = {
        "client_name": jd_parsed.get("client_name") or "Agentic Boost (auto)",
        "market": market,
        "role_title": jd_parsed.get("role_title") or "Role from JD",
        "skills_required": jd_parsed.get("skills_required") or [],
        "experience_min": (str(jd_parsed["experience_min"])
                           if jd_parsed.get("experience_min") is not None
                           else None),
        "location": jd_parsed.get("location"),
        "contract_type": jd_parsed.get("contract_type"),
        "assigned_recruiters": [user_email],
        "source": "agentic_boost",
        "boost_run": True,
        "jd_text": jd_text,
        "jd_parsed": jd_parsed,
    }
    try:
        req_row = db.insert_requirement(req_payload)
        requirement_id = req_row["id"]
        db.update_boost_run(boost_id, {"requirement_id": requirement_id})
    except Exception as exc:
        log.exception("agentic boost: requirement insert failed")
        yield _sse({"event": "error",
                    "message": f"Could not create requirement: {exc}"})
        try:
            db.update_boost_run(boost_id, {"status": "failed"})
        except Exception:
            pass
        return
    requirement = db.get_requirement_by_id(requirement_id) or req_row
    # Ensure downstream helpers see the parsed market and skills.
    requirement.setdefault("market", market)
    requirement.setdefault("skills_required",
                           jd_parsed.get("skills_required") or [])

    # ── Agent 2: Boolean Builder ───────────────────────────────
    yield _sse({"event": "agent_start", "agent": "boolean_builder",
                "idx": 2, "label": "Building boolean string"})
    try:
        boolean_output = _boolean_builder_agent({**jd_parsed, "market": market})
    except Exception as exc:
        log.exception("agentic boost: boolean_builder failed — using heuristic")
        boolean_output = _boolean_builder_fallback({**jd_parsed,
                                                   "market": market})
        yield _sse({"event": "agent_error", "agent": "boolean_builder",
                    "message": f"{exc} — using heuristic fallback"})
    yield _sse({"event": "agent_done", "agent": "boolean_builder",
                "payload": boolean_output})

    # ── Agent 3: Sourcing (Apollo + GitHub + HF + Apify + WebAgent + DB) ──
    apollo_enabled = bool(os.environ.get("APOLLO_API_KEY")
                          or os.environ.get("APOLLO_API"))
    github_enabled = bool(os.environ.get("GITHUB_TOKEN"))
    hf_enabled = bool(os.environ.get("HF_ENABLED")
                      or os.environ.get("HF_TOKEN"))
    apify_enabled = bool(os.environ.get("APIFY_TOKEN"))
    web_agent_enabled = bool(os.environ.get("BRAVE_SEARCH_API_KEY")
                             or os.environ.get("SERPAPI_KEY"))
    channel_errors: dict[str, str] = {}
    source_counts: dict[str, int] = {"apollo": 0, "internal_db": 0,
                                     "github": 0, "huggingface": 0,
                                     "linkedin_apify": 0, "yc": 0,
                                     "web_agent": 0}
    pool_by_id: dict[str, dict] = {}
    apollo_params = boolean_output.get("apollo_params") or {}

    enabled_labels = ["Internal DB"]
    if apollo_enabled:
        enabled_labels.append("Apollo")
    if github_enabled:
        enabled_labels.append("GitHub")
    if hf_enabled:
        enabled_labels.append("Hugging Face")
    if apify_enabled:
        enabled_labels.append("Apify")
    if web_agent_enabled:
        enabled_labels.append("Web Agent")
    yield _sse({"event": "agent_start", "agent": "sourcing", "idx": 3,
                "label": "Sourcing " + " + ".join(enabled_labels)})

    if not apollo_enabled:
        channel_errors["apollo"] = "skipped — APOLLO_API_KEY not set on server"
    elif not (apollo_params.get("person_titles")
              or apollo_params.get("q_keywords")):
        channel_errors["apollo"] = ("skipped — boolean builder produced no "
                                    "person_titles or q_keywords to search with")
        apollo_enabled = False

    if not github_enabled:
        channel_errors["github"] = "skipped — GITHUB_TOKEN not set on server"

    # GitHub takes free-text skills + a location string. Pull both from the
    # JD-parsed requirement so we don't depend on the Apollo-shaped params.
    gh_skills = (requirement.get("skills_required") or [])[:6]
    gh_location = requirement.get("location") or (
        "Singapore" if market == "SG" else "India")

    hf_skills = (requirement.get("skills_required") or [])[:6]
    if hf_enabled and not hf_skills:
        channel_errors["huggingface"] = (
            "skipped — no skills_required to seed model search")
        hf_enabled = False
    elif not hf_enabled:
        channel_errors["huggingface"] = (
            "skipped — HF_ENABLED not set on server")

    apify_skills = (requirement.get("skills_required") or [])[:6]
    apify_role = requirement.get("role_title")
    apify_location = requirement.get("location") or (
        "Singapore" if market == "SG" else "India")
    if not apify_enabled:
        channel_errors["apify"] = "skipped — APIFY_TOKEN not set on server"

    web_skills = (requirement.get("skills_required") or [])[:6]
    web_role = requirement.get("role_title")
    web_location = requirement.get("location") or (
        "Singapore" if market == "SG" else "India")
    if web_agent_enabled and not web_skills:
        channel_errors["web_agent"] = (
            "skipped — no skills_required to seed search queries")
        web_agent_enabled = False
    elif not web_agent_enabled:
        channel_errors["web_agent"] = (
            "skipped — neither BRAVE_SEARCH_API_KEY nor SERPAPI_KEY set")

    async def _run_sourcing():
        task_names: list[str] = []
        coros = []
        if apollo_enabled:
            task_names.append("apollo")
            coros.append(sourcing.source_apollo_structured_adaptive(
                apollo_params, market))
        if github_enabled:
            task_names.append("github")
            coros.append(sourcing.source_github(
                gh_skills, gh_location, market))
        if hf_enabled:
            task_names.append("huggingface")
            coros.append(sourcing.source_huggingface(hf_skills, market))
        if apify_enabled:
            task_names.append("apify")
            coros.append(sourcing.source_apify(
                apify_skills, apify_location, market, apify_role))
        if web_agent_enabled:
            task_names.append("web_agent")
            coros.append(sourcing.source_web_agent(
                web_role, web_location, web_skills, market))
        task_names.append("internal_db")
        coros.append(_internal_db_search(requirement, limit=200))
        gathered = await asyncio.gather(*coros, return_exceptions=True)
        return list(zip(task_names, gathered))

    try:
        gathered = _run_async(_run_sourcing())
    except Exception as exc:
        log.exception("agentic boost: sourcing top-level failed")
        gathered = []
        channel_errors["sourcing"] = str(exc)

    apollo_received = 0
    apollo_upsert_errs: list[str] = []
    apollo_skipped_no_key = 0
    apollo_skipped_unreachable = 0
    apollo_iteration_log: list[dict] = []
    for name, result in gathered:
        if isinstance(result, Exception):
            channel_errors[name] = f"{type(result).__name__}: {result}"
            continue
        if name == "apollo":
            # Adaptive wrapper returns (candidates, iteration_log).
            if isinstance(result, tuple) and len(result) == 2:
                result, apollo_iteration_log = result
            apollo_received = len(result)
        for cand in result:
            if name == "apollo":
                # Apollo says no email AND no/maybe phone -> unreachable. Skip.
                hp = (cand.get("has_direct_phone") or "").lower()
                if cand.get("has_email") is False and hp in ("", "no"):
                    apollo_skipped_unreachable += 1
                    continue
                # Apollo redacts `name` at search tier. Synthesize a placeholder
                # so the row survives upsert + scoring; the UI's reveal button
                # can upgrade it later. We deliberately do NOT fabricate
                # `current_location` — leave it NULL when Apollo doesn't
                # return one.
                if not cand.get("name"):
                    title = cand.get("current_job_title") or "Unknown role"
                    employer = cand.get("current_employer") or "unknown employer"
                    cand["name"] = f"{title} @ {employer} (Apollo)"
                # Drop the `_`-prefixed sentinel keys before upsert — they
                # aren't real columns and Supabase rejects the whole insert.
                apollo_pid = cand.pop("_apollo_person_id", None)
                apollo_oid = cand.pop("_apollo_organization_id", None)
                if apollo_pid:
                    cand["apollo_person_id"] = apollo_pid
                if apollo_oid:
                    cand["apollo_organization_id"] = apollo_oid
                # Persist Apollo results (they're new external candidates)
                try:
                    if cand.get("email"):
                        row = db.upsert_candidate_by_email(cand)
                    elif cand.get("name"):
                        row = db.upsert_candidate_by_name(cand)
                    else:
                        apollo_skipped_no_key += 1
                        continue
                except Exception as upsert_err:
                    if len(apollo_upsert_errs) < 3:
                        apollo_upsert_errs.append(
                            f"{type(upsert_err).__name__}: "
                            f"{str(upsert_err)[:200]}")
                    log.exception("apollo upsert failed for %s",
                                  cand.get("name"))
                    continue
                if row and row.get("id") and row["id"] not in pool_by_id:
                    pool_by_id[row["id"]] = row
                    source_counts["apollo"] += 1
            elif name == "github":
                # GitHub gives real names + locations, no redaction quirks.
                # source_metadata is a dict; jsonb column accepts it directly.
                try:
                    if cand.get("email"):
                        row = db.upsert_candidate_by_email(cand)
                    elif cand.get("name"):
                        row = db.upsert_candidate_by_name(cand)
                    else:
                        continue
                except Exception:
                    log.exception("github upsert failed for %s",
                                  cand.get("name"))
                    continue
                if row and row.get("id") and row["id"] not in pool_by_id:
                    pool_by_id[row["id"]] = row
                    source_counts["github"] += 1
            elif name == "huggingface":
                # HF profiles never have email; always upsert by name.
                try:
                    if cand.get("name"):
                        row = db.upsert_candidate_by_name(cand)
                    else:
                        continue
                except Exception:
                    log.exception("huggingface upsert failed for %s",
                                  cand.get("name"))
                    continue
                if row and row.get("id") and row["id"] not in pool_by_id:
                    pool_by_id[row["id"]] = row
                    source_counts["huggingface"] += 1
            elif name == "apify":
                # Apify is two sub-actors (linkedin + yc). The candidate
                # carries its own `source` already (linkedin_apify | yc), so
                # we attribute the count to the right sub-source for the UI.
                sub = cand.get("source") or "linkedin_apify"
                try:
                    if cand.get("email"):
                        row = db.upsert_candidate_by_email(cand)
                    elif cand.get("name"):
                        row = db.upsert_candidate_by_name(cand)
                    else:
                        continue
                except Exception:
                    log.exception("apify upsert failed for %s",
                                  cand.get("name"))
                    continue
                if row and row.get("id") and row["id"] not in pool_by_id:
                    pool_by_id[row["id"]] = row
                    source_counts[sub] = source_counts.get(sub, 0) + 1
            elif name == "web_agent":
                # Web agent extractions never have email; upsert by name.
                try:
                    if cand.get("name"):
                        row = db.upsert_candidate_by_name(cand)
                    else:
                        continue
                except Exception:
                    log.exception("web_agent upsert failed for %s",
                                  cand.get("name"))
                    continue
                if row and row.get("id") and row["id"] not in pool_by_id:
                    pool_by_id[row["id"]] = row
                    source_counts["web_agent"] += 1
            else:  # internal_db
                cid = cand.get("id")
                if cid and cid not in pool_by_id:
                    pool_by_id[cid] = cand
                    source_counts["internal_db"] += 1

    # Surface silent Apollo drops in channel_errors so the pipeline-error
    # alert can tell us whether the API returned nothing, rows got skipped
    # pre-upsert, or Supabase rejected every insert.
    if (apollo_enabled
            and source_counts["apollo"] == 0
            and "apollo" not in channel_errors):
        parts = [f"received {apollo_received} rows"]
        if apollo_skipped_unreachable:
            parts.append(f"{apollo_skipped_unreachable} skipped "
                         f"(Apollo-marked unreachable)")
        if apollo_skipped_no_key:
            parts.append(f"{apollo_skipped_no_key} skipped "
                         f"(no email/name after synthesis)")
        if apollo_upsert_errs:
            parts.append(f"upsert errs (up to 3): {apollo_upsert_errs}")
        elif apollo_received and not apollo_skipped_no_key:
            parts.append("all rows reached upsert but none persisted "
                         "(empty row returned)")
        channel_errors["apollo"] = "post-sourcing drop — " + "; ".join(parts)

    pool = list(pool_by_id.values())
    sourcing_payload: dict[str, Any] = {
        "counts": source_counts,
        "total_unique": len(pool),
        "channel_errors": channel_errors,
    }
    if apollo_skipped_unreachable:
        sourcing_payload["apollo_skipped_unreachable"] = apollo_skipped_unreachable
    if apollo_iteration_log:
        sourcing_payload["apollo_iterations"] = apollo_iteration_log
    yield _sse({"event": "agent_done", "agent": "sourcing",
                "payload": sourcing_payload})

    if not pool:
        yield _sse({"event": "error",
                    "message": "No candidates found from any channel"})
        try:
            db.update_boost_run(boost_id, {"status": "failed"})
        except Exception:
            pass
        return

    # ── Agent 4: Screener ──────────────────────────────────────
    yield _sse({"event": "agent_start", "agent": "screener", "idx": 4,
                "label": f"Scoring {len(pool)} candidates"})
    top_enriched: list[dict] = []
    try:
        all_ids = [c["id"] for c in pool]
        cached = db.get_cached_match_scores(requirement_id, all_ids)
        to_score = [c for c in pool if c["id"] not in cached]
        new_scores: list[dict] = []
        if to_score:
            for i in range(0, len(to_score), MATCH_BATCH_SIZE):
                batch = to_score[i:i + MATCH_BATCH_SIZE]
                try:
                    batch_scores = _score_candidate_batch(batch, requirement)
                except Exception:
                    log.exception("screener batch failed (i=%s)", i)
                    batch_scores = []
                new_scores.extend(batch_scores)
                yield _sse({"event": "agent_progress", "agent": "screener",
                            "message": (f"Scored "
                                        f"{min(i + MATCH_BATCH_SIZE, len(to_score))}"
                                        f"/{len(to_score)}")})
            if new_scores:
                db.upsert_match_scores(requirement_id, new_scores)
        top = (db.get_match_scores_above(
            requirement_id, min_score=MATCH_MIN_SCORE)[:BOOST_TOP_N])
        # Auto-reveal: budget=5. Walks the score-ordered top list and reveals
        # the highest-scoring Apollo rows Apollo itself flagged reachable.
        # Runs BEFORE _enrich so freshly-revealed emails flow into outreach.
        try:
            auto_reveal_summary = _auto_reveal_top_reachable(
                requirement_id=requirement_id,
                candidate_ids_by_score=[t["candidate_id"] for t in top
                                        if t.get("candidate_id")],
                requested_by=user_email,
                budget=5,
            )
        except Exception:
            log.exception("agentic boost: auto-reveal pass failed")
            auto_reveal_summary = None
        # LinkedIn enrichment: budget=5. Mirrors the Apollo auto-reveal but
        # for non-Apollo top rows that have a LinkedIn URL and no email.
        # Hits harvestapi/linkedin-profile-scraper to backfill email + phone
        # so outreach drafting has something to send to.
        linkedin_enrich_summary = None
        if os.environ.get("APIFY_TOKEN"):
            try:
                linkedin_enrich_summary = _run_async(
                    _auto_enrich_linkedin_top(
                        candidate_ids_by_score=[
                            t["candidate_id"] for t in top
                            if t.get("candidate_id")],
                        budget=5,
                    ))
            except Exception:
                log.exception("agentic boost: linkedin enrichment failed")
                linkedin_enrich_summary = None
        top_enriched = _enrich_top_candidates(
            top, requirement_id, user_email)
        yield _sse({"event": "agent_done", "agent": "screener", "payload": {
            "scored_total": len(pool),
            "top_count": len(top_enriched),
        }})
        if auto_reveal_summary is not None:
            yield _sse({"event": "agent_done", "agent": "auto_reveal",
                        "payload": auto_reveal_summary})
        if linkedin_enrich_summary is not None:
            yield _sse({"event": "agent_done", "agent": "linkedin_enrich",
                        "payload": linkedin_enrich_summary})
    except Exception as exc:
        log.exception("agentic boost: screener failed")
        yield _sse({"event": "agent_error", "agent": "screener",
                    "message": str(exc)})
        yield _sse({"event": "error",
                    "message": "Pipeline aborted — screening failed"})
        try:
            db.update_boost_run(boost_id, {"status": "failed"})
        except Exception:
            pass
        return

    # ── Agent 5: Outreach Drafter ──────────────────────────────
    yield _sse({"event": "agent_start", "agent": "outreach_drafter",
                "idx": 5,
                "label": f"Drafting {len(top_enriched)} emails"})
    if not top_enriched:
        yield _sse({"event": "agent_done", "agent": "outreach_drafter",
                    "payload": {"drafts_created": 0}})
    else:
        try:
            draft_result = draft_sequence(
                {"requirement_id": requirement_id,
                 "candidate_ids": [t["candidate_id"] for t in top_enriched],
                 "recruiter_name": user_email.split("@")[0].title()},
                user_role, user_email,
            )
            draft_ids_by_candidate: dict[str, str] = {}
            drafts_by_candidate: dict[str, dict] = {}
            for em in (draft_result.get("emails") or []):
                drafts_by_candidate[em["candidate_id"]] = em
                if not em.get("sendable"):
                    continue
                try:
                    log_row = db.insert_outreach_log({
                        "candidate_id": em["candidate_id"],
                        "requirement_id": requirement_id,
                        "recruiter_email": user_email,
                        "email_subject": em["subject"],
                        "email_body": em["body"],
                        "status": "draft",
                    })
                    draft_ids_by_candidate[em["candidate_id"]] = log_row["id"]
                except Exception:
                    log.exception("draft persist failed for candidate %s",
                                  em.get("candidate_id"))
            # Merge just-drafted subject/body over whatever the enricher
            # already stamped from DB (fresh drafts win — matters on the
            # live path where the DB row was just inserted above).
            for t in top_enriched:
                cid = t.get("candidate_id") or t.get("id")
                em = drafts_by_candidate.get(cid)
                did = draft_ids_by_candidate.get(cid)
                if did:
                    t["draft_id"] = did
                if em:
                    t["draft_subject"] = em.get("subject")
                    t["draft_body"] = em.get("body")
                    t["draft_sendable"] = em.get("sendable", False)
                    t["draft_status"] = ("draft" if em.get("sendable")
                                         else t.get("draft_status"))
                else:
                    t.setdefault("draft_sendable", False)
            yield _sse({"event": "agent_done", "agent": "outreach_drafter",
                        "payload": {
                            "drafts_created": len(draft_ids_by_candidate),
                        }})
        except Exception as exc:
            log.exception("agentic boost: outreach drafter failed")
            yield _sse({"event": "agent_error", "agent": "outreach_drafter",
                        "message": str(exc)})

    # ── Done ───────────────────────────────────────────────────
    try:
        db.update_boost_run(boost_id, {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        log.exception("agentic boost: failed to mark run completed")

    yield _sse({"event": "boost_done",
                "boost_id": boost_id,
                "requirement_id": requirement_id,
                "boolean_string": boolean_output.get("boolean_string", ""),
                "linkedin_url": boolean_output.get("linkedin_url", ""),
                "top_candidates": top_enriched})


def get_agentic_boost_run(boost_id: str, user_role: str,
                          user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    row = db.get_boost_run(boost_id)
    if not row:
        raise CoreError(404, "Boost run not found")
    if row.get("created_by") != user_email and user_role != "tl":
        raise CoreError(403, "Not your boost run")
    if row.get("requirement_id"):
        top_raw = (db.get_match_scores_above(
            row["requirement_id"], min_score=MATCH_MIN_SCORE)[:BOOST_TOP_N])
        row["top_candidates"] = _enrich_top_candidates(
            top_raw, row["requirement_id"], user_email)
    return row


def list_agentic_boost_runs(user_role: str, user_email: str) -> dict:
    _require_role(user_role, ["recruiter", "tl"])
    rows = db.list_boost_runs(
        created_by=user_email if user_role != "tl" else None)
    return {"runs": rows}


def update_agentic_boost_draft(draft_id: str, payload: dict,
                               user_role: str, user_email: str) -> dict:
    """Edit a draft's subject and/or body."""
    _require_role(user_role, ["recruiter", "tl"])
    if not isinstance(payload, dict):
        raise CoreError(422, "request body must be a JSON object")
    patch: dict = {}
    if "email_subject" in payload:
        patch["email_subject"] = payload["email_subject"]
    if "email_body" in payload:
        patch["email_body"] = payload["email_body"]
    if not patch:
        raise CoreError(422, "nothing to update (email_subject or email_body)")
    row = db.get_outreach_log(draft_id)
    if not row:
        raise CoreError(404, "Draft not found")
    if row.get("status") != "draft":
        raise CoreError(400,
                        f"Cannot edit — draft already {row.get('status')}")
    if (row.get("recruiter_email") != user_email and user_role != "tl"):
        raise CoreError(403, "Not your draft")
    updated = db.update_outreach_log(draft_id, patch)
    return {"status": "ok", "draft": updated}


def send_agentic_boost_draft(draft_id: str, user_role: str,
                             user_email: str) -> dict:
    """Send a draft via Outlook (the recruiter's mailbox)."""
    _require_role(user_role, ["recruiter", "tl"])
    row = db.get_outreach_log(draft_id)
    if not row:
        raise CoreError(404, "Draft not found")
    if row.get("status") != "draft":
        raise CoreError(400, f"Draft already {row.get('status')}")
    if (row.get("recruiter_email") != user_email and user_role != "tl"):
        raise CoreError(403, "Not your draft")
    cand = db.get_candidate_by_id(row["candidate_id"])
    if not cand or not cand.get("email"):
        raise CoreError(400, "Candidate has no email address")
    try:
        sent = outlook.send_email(
            from_email=user_email,
            to_email=cand["email"],
            subject=row.get("email_subject") or "",
            body=row.get("email_body") or "",
        )
    except Exception as exc:
        log.exception("agentic boost: outlook send failed for draft %s",
                      draft_id)
        try:
            db.update_outreach_log(draft_id, {"status": "failed"})
        except Exception:
            pass
        raise CoreError(502, f"Email send failed: {exc}")
    db.update_outreach_log(draft_id, {
        "status": "sent",
        "outlook_message_id": sent.get("message_id"),
        "outlook_thread_id": sent.get("thread_id"),
        "sent_at": sent.get("sent_at"),
    })
    return {"status": "sent",
            "draft_id": draft_id,
            "sent_at": sent.get("sent_at")}
