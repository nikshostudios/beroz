"""ExcelTech AI Agent Layer — FastAPI app running alongside existing Flask app."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import db, outlook, sourcing, search_parser, market_intelligence

# ── Globals ────────────────────────────────────────────────────

AGENTS: dict[str, str] = {}
SKILLS: dict[str, str] = {}
scheduler = AsyncIOScheduler()
claude = None  # anthropic.Anthropic client


# ── Startup / Shutdown ─────────────────────────────────────────

def _load_md_files(directory: str) -> dict[str, str]:
    """Load all .md files from a directory into a {name: content} dict."""
    result = {}
    md_dir = Path(__file__).parent / directory
    for f in md_dir.glob("*.md"):
        result[f.stem] = f.read_text()
    return result


async def _verify_graph_api():
    test_email = os.environ.get("STARTUP_TEST_EMAIL")
    if test_email:
        try:
            outlook.get_access_token(test_email)
        except Exception as e:
            print(f"[WARN] Graph API check failed for {test_email}: {e}")


async def _scheduled_inbox_process():
    try:
        result = await _run_process_inbox(recruiter_email=None)
        print(f"[INBOX CRON] {result}")
    except Exception as e:
        print(f"[INBOX CRON ERROR] {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global AGENTS, SKILLS, claude
    AGENTS = _load_md_files("agents")
    SKILLS = _load_md_files("skills")
    print(f"[STARTUP] Loaded {len(AGENTS)} agents, {len(SKILLS)} skills")

    claude = anthropic.Anthropic()
    db.get_client()
    await _verify_graph_api()

    scheduler.add_job(_scheduled_inbox_process, "interval", minutes=15,
                      id="inbox_cron")
    scheduler.start()
    print("[STARTUP] Scheduler started — inbox cron every 15min")

    yield
    scheduler.shutdown()


# ── App ────────────────────────────────────────────────────────

app = FastAPI(title="ExcelTech AI Agents", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://exceltechcomputers.up.railway.app",
        "http://localhost:3000",
        "http://localhost:5000",
        "http://localhost:5001",
        "http://127.0.0.1:5000",
        "http://127.0.0.1:5001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}"},
    )


# ── Auth helpers ───────────────────────────────────────────────

def _require_role(role: str, allowed: list[str]):
    if role not in allowed:
        raise HTTPException(403, f"Role '{role}' not allowed. Need: {allowed}")


# ── LLM helper ─────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _log_tokens(endpoint: str, model: str, input_tokens: int,
                output_tokens: int):
    """Append token usage to daily log file."""
    log_file = LOG_DIR / f"tokens_{datetime.now().strftime('%Y%m%d')}.log"
    ts = datetime.now().isoformat(timespec="seconds")
    with open(log_file, "a") as f:
        f.write(f"{ts}|{endpoint}|{model}|{input_tokens}|{output_tokens}\n")


def _call_claude(model: str, system: str, user_msg: str,
                 max_tokens: int = 2048, endpoint: str = "unknown") -> str:
    resp = claude.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    _log_tokens(endpoint, model, resp.usage.input_tokens,
                resp.usage.output_tokens)
    return resp.content[0].text


def _parse_llm_json(text: str) -> dict | list | None:
    """Parse JSON from Claude response, stripping markdown fences if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


# ── Request models ─────────────────────────────────────────────

class RequirementCreate(BaseModel):
    client_name: str
    market: str
    role_title: str
    skillset: str | None = None
    skills_required: list[str] = []
    salary_budget: str | None = None
    experience_min: str | None = None
    location: str | None = None
    contract_type: str | None = None
    notice_period: str | None = None
    tender_number: str | None = None
    jd_text: str | None = None


class ScreenRequest(BaseModel):
    candidate_id: str
    requirement_id: str


class OutreachDraftRequest(BaseModel):
    candidate_id: str
    requirement_id: str
    recruiter_name: str
    recruiter_email: str


class OutreachSendRequest(BaseModel):
    candidate_id: str
    requirement_id: str
    recruiter_email: str
    final_subject: str
    final_body: str


class InboxProcessRequest(BaseModel):
    recruiter_email: str | None = None


class SubmitToTLRequest(BaseModel):
    candidate_id: str
    requirement_id: str
    recruiter_email: str


class TLApproveRequest(BaseModel):
    submission_id: str
    tl_email: str
    client_email: str
    email_subject: str
    email_body_notes: str | None = None


class TLRejectRequest(BaseModel):
    submission_id: str
    feedback: str = ""


class ProcessReplyRequest(BaseModel):
    recruiter_email: str
    sender_email: str
    thread_id: str
    body_text: str


class SearchParseRequest(BaseModel):
    requirement_text: str


# ── POST /search/parse (Layer 1 — Search Parser) ──────────────

@app.post("/search/parse")
async def parse_search(body: SearchParseRequest):
    """Layer 1: Parse natural language requirement into structured filters."""
    try:
        result = search_parser.parse_search_query(
            requirement_text=body.requirement_text,
            call_claude_fn=_call_claude,
            parse_json_fn=_parse_llm_json,
        )
        return {"status": "ok", "parsed": result}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── POST /requirements/create ─────────────────────────────────

@app.post("/requirements/create")
async def create_requirement(
    body: RequirementCreate,
    background: BackgroundTasks,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["tl"])

    data = body.model_dump(exclude_none=True)
    # Normalize market to DB values
    market_map = {"india": "IN", "singapore": "SG", "in": "IN", "sg": "SG"}
    if "market" in data:
        data["market"] = market_map.get(data["market"].lower(), data["market"])

    # ── JD Parser Agent (v2) — replaces the old Haiku skills-only extraction
    # Uses the dedicated jd_parser agent for full structured extraction:
    # skills (decomposed), salary, experience, location, red flags, etc.
    if body.jd_text:
        jd_parser_prompt = AGENTS.get("jd_parser", "")
        if jd_parser_prompt:
            parsed_raw = _call_claude(
                "claude-sonnet-4-20250514",
                jd_parser_prompt,
                f"Market: {data.get('market', 'IN')}\n\nJD text:\n{body.jd_text}",
                max_tokens=2048,
                endpoint="/requirements/create/jd-parse",
            )
            jd_parsed = _parse_llm_json(parsed_raw)
            if isinstance(jd_parsed, dict):
                # Apply parsed fields — only override if not already provided
                if not body.skills_required and jd_parsed.get("skills_required"):
                    data["skills_required"] = jd_parsed["skills_required"]
                if not body.experience_min and jd_parsed.get("experience_min"):
                    data["experience_min"] = str(jd_parsed["experience_min"])
                if not body.salary_budget and jd_parsed.get("salary_max"):
                    sal_min = jd_parsed.get("salary_min", "")
                    sal_max = jd_parsed.get("salary_max", "")
                    currency = jd_parsed.get("salary_currency", "")
                    data["salary_budget"] = f"{sal_min}-{sal_max} {currency}".strip()
                if not body.location and jd_parsed.get("location"):
                    data["location"] = jd_parsed["location"]
                if not body.contract_type and jd_parsed.get("contract_type"):
                    data["contract_type"] = jd_parsed["contract_type"]
                if not body.notice_period and jd_parsed.get("notice_period_max_days"):
                    data["notice_period"] = str(jd_parsed["notice_period_max_days"])
                # Store the full parse result for reference
                data["jd_parsed"] = jd_parsed
        else:
            # Fallback: old Haiku extraction if jd_parser agent not loaded
            if not body.skills_required:
                parsed = _call_claude(
                    "claude-haiku-4-5-20251001",
                    AGENTS.get("screener", ""),
                    f"Extract skills_required as a JSON array of strings "
                    f"from this JD:\n{body.jd_text}",
                    max_tokens=512,
                    endpoint="/requirements/create",
                )
                skills = _parse_llm_json(parsed)
                if isinstance(skills, list):
                    data["skills_required"] = skills

    data.pop("jd_text", None)
    req = db.insert_requirement(data)

    background.add_task(_run_source_and_screen, req["id"])
    return {"requirement_id": req["id"], "sourcing_started": True,
            "jd_parsed": data.get("jd_parsed")}


# ── POST /requirements/{req_id}/source ─────────────────────────

@app.post("/requirements/{req_id}/source")
async def source_requirement(
    req_id: str,
    background: BackgroundTasks,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["recruiter", "tl"])
    # Run sourcing inline (fast), then screen in background (slow — Claude calls)
    requirement = db.get_requirement_by_id(req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")

    try:
        source_results = await sourcing.run_all_sources(requirement)
    except Exception as e:
        log.exception("run_all_sources crashed for %s", req_id)
        raise HTTPException(500, f"Sourcing failed: {e}")

    linkedin_str = sourcing.generate_linkedin_search_string(requirement)
    just_sourced = source_results.pop("upserted_candidates", [])

    # Kick off screening in background so we don't time out
    background.add_task(_screen_candidates, req_id, requirement, just_sourced)

    return {
        "sourced": source_results.get("total_unique", 0),
        "screened": "in_progress",
        "shortlisted": "in_progress",
        "linkedin_search_string": linkedin_str,
        "message": f"Sourced {source_results.get('total_unique', 0)} candidates. "
                   f"Screening {len(just_sourced)} in background.",
    }


# ── GET /requirements ──────────────────────────────────────────

@app.get("/requirements")
async def list_requirements(
    market: str | None = None,
    status: str = "open",
    created_after: str | None = None,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
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


# ── POST /requirements/{req_id}/match ────────────────────────

@app.post("/requirements/{req_id}/match")
async def match_requirement(
    req_id: str,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    """Run LLM semantic matching for a requirement against the candidate pool."""
    _require_role(x_user_role, ["recruiter", "tl"])
    requirement = db.get_requirement_by_id(req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")

    matched = _match_candidates_for_requirement(requirement)
    return {
        "requirement_id": req_id,
        "matched": len(matched),
        "candidates": matched,
    }


# ── POST /candidates/screen ───────────────────────────────────

@app.post("/candidates/screen")
async def screen_candidate(
    body: ScreenRequest,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["recruiter", "tl"])
    candidate = db.get_candidate_by_id(body.candidate_id)
    requirement = db.get_requirement_by_id(body.requirement_id)
    if not candidate or not requirement:
        raise HTTPException(404, "Candidate or requirement not found")

    result = _call_claude(
        "claude-sonnet-4-20250514",
        AGENTS.get("screener", ""),
        f"Score this candidate against the requirement.\n\n"
        f"CANDIDATE: {candidate}\n\nREQUIREMENT: {requirement}\n\n"
        f"Return JSON only.",
        max_tokens=1024,
        endpoint="/candidates/screen",
    )

    screening = _parse_llm_json(result)
    if not isinstance(screening, dict):
        screening = {"score": 1, "recommendation": "reject",
                     "reasoning": f"Failed to parse screening result: {result[:200]}"}

    screening["candidate_id"] = body.candidate_id
    screening["requirement_id"] = body.requirement_id
    screening["recruiter_email"] = x_user_email
    db.insert_screening(screening)

    return {**screening,
            "outreach_ready": screening.get("recommendation") == "shortlist"}


# ── POST /candidates/prepare-outreach ──────────────────────────

@app.post("/candidates/prepare-outreach")
async def prepare_outreach(
    body: OutreachDraftRequest,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["recruiter"])
    candidate = db.get_candidate_by_id(body.candidate_id)
    requirement = db.get_requirement_by_id(body.requirement_id)
    if not candidate or not requirement:
        raise HTTPException(404, "Candidate or requirement not found")

    result = _call_claude(
        "claude-haiku-4-5-20251001",
        AGENTS.get("outreach", ""),
        f"Draft outreach email.\n\nCANDIDATE: {candidate}\n\n"
        f"REQUIREMENT: {requirement}\n\n"
        f"RECRUITER: {body.recruiter_name} <{body.recruiter_email}>\n\n"
        f"Return JSON with subject and body keys only.",
        max_tokens=2048,
        endpoint="/candidates/prepare-outreach",
    )

    draft = _parse_llm_json(result)
    if not isinstance(draft, dict):
        draft = {"subject": f"Opportunity: {requirement.get('role_title', '')}",
                 "body": result}

    return {"draft_subject": draft.get("subject", ""),
            "draft_body": draft.get("body", "")}


# ── POST /candidates/send-outreach ────────────────────────────

@app.post("/candidates/send-outreach")
async def send_outreach(
    body: OutreachSendRequest,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["recruiter"])
    candidate = db.get_candidate_by_id(body.candidate_id)
    if not candidate or not candidate.get("email"):
        raise HTTPException(400, "Candidate email not found")

    sent = outlook.send_email(
        from_email=body.recruiter_email,
        to_email=candidate["email"],
        subject=body.final_subject,
        body=body.final_body,
    )

    log = db.insert_outreach_log({
        "candidate_id": body.candidate_id,
        "requirement_id": body.requirement_id,
        "recruiter_email": body.recruiter_email,
        "outlook_message_id": sent["message_id"],
        "outlook_thread_id": sent["thread_id"],
        "email_subject": body.final_subject,
        "sent_at": sent["sent_at"],
    })

    return {"sent": True, "outreach_log_id": log["id"],
            "sent_at": sent["sent_at"]}


# ── POST /inbox/process ───────────────────────────────────────

@app.post("/inbox/process")
async def process_inbox(body: InboxProcessRequest):
    return await _run_process_inbox(body.recruiter_email)


# ── POST /inbox/process-reply ─────────────────────────────────

@app.post("/inbox/process-reply")
async def process_reply(body: ProcessReplyRequest):
    # Match by thread_id first, then by sender email
    pending = db.get_pending_replies(body.recruiter_email)
    matched = next(
        (p for p in pending if p.get("outlook_thread_id") == body.thread_id),
        None,
    )

    candidate_id = matched["candidate_id"] if matched else None
    requirement_id = matched["requirement_id"] if matched else None

    if not candidate_id:
        rows = (db.get_client().table("outreach_log")
                .select("candidate_id, requirement_id, id")
                .eq("recruiter_email", body.recruiter_email)
                .execute().data)
        for row in rows:
            cand = db.get_candidate_by_id(row["candidate_id"])
            if cand and cand.get("email") == body.sender_email:
                candidate_id = row["candidate_id"]
                requirement_id = row["requirement_id"]
                matched = row
                break

    if not candidate_id:
        raise HTTPException(404, "Could not match reply to any outreach")

    if matched:
        db.mark_reply_received(matched["id"])

    result = _call_claude(
        "claude-haiku-4-5-20251001",
        AGENTS.get("followup", ""),
        f"Parse this candidate reply and extract filled table fields.\n\n"
        f"EMAIL BODY:\n{body.body_text}\n\n"
        f"Return JSON: fields_filled (dict), fields_missing (list), "
        f"chase_draft (string or null), status (details_received or "
        f"ready_for_review)",
        max_tokens=2048,
        endpoint="/inbox/process-reply",
    )

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        parsed = {"fields_filled": {}, "fields_missing": [],
                  "chase_draft": None, "status": "details_received"}

    if parsed.get("fields_filled"):
        update_data = parsed["fields_filled"]
        update_data["status"] = parsed.get("status", "details_received")
        db.upsert_candidate_details(candidate_id, requirement_id, update_data)

    return {
        "candidate_id": candidate_id,
        "fields_filled": parsed.get("fields_filled", {}),
        "fields_missing": parsed.get("fields_missing", []),
        "chase_draft": parsed.get("chase_draft"),
        "status": parsed.get("status", "details_received"),
    }


# ── POST /candidates/submit-to-tl ─────────────────────────────

@app.post("/candidates/submit-to-tl")
async def submit_to_tl(
    body: SubmitToTLRequest,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["recruiter"])

    details = db.get_candidate_details(body.candidate_id, body.requirement_id)
    if not details or details.get("status") != "ready_for_review":
        raise HTTPException(400, "Candidate details not ready for review")

    candidate = db.get_candidate_by_id(body.candidate_id)
    requirement = db.get_requirement_by_id(body.requirement_id)

    client_name = requirement.get("client_name", "unknown").replace(" ", "_")
    cand_name = candidate.get("name", "unknown").replace(" ", "_")
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    doc_dir = Path(__file__).parent / "submissions" / client_name
    doc_dir.mkdir(parents=True, exist_ok=True)
    doc_path = str(doc_dir / f"{cand_name}_{date_str}.docx")

    # TODO: .docx generation via formatter agent + python-docx

    submission = db.insert_submission({
        "candidate_id": body.candidate_id,
        "requirement_id": body.requirement_id,
        "client_name": requirement.get("client_name"),
        "market": requirement.get("market"),
        "formatted_doc_path": doc_path,
        "submitted_by_recruiter": body.recruiter_email,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "tl_approved": False,
    })

    return {"doc_path": doc_path, "missing_fields": [],
            "submission_id": submission["id"]}


# ── GET /tl/queue ──────────────────────────────────────────────

@app.get("/tl/queue")
async def tl_queue(
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["tl"])

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


# ── POST /tl/approve-and-send ─────────────────────────────────

@app.post("/tl/approve-and-send")
async def tl_approve_and_send(
    body: TLApproveRequest,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    _require_role(x_user_role, ["tl"])

    rows = (db.get_client().table("submissions")
            .select("*").eq("id", body.submission_id).execute().data)
    if not rows:
        raise HTTPException(404, "Submission not found")
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
    if body.email_body_notes:
        body_html += f"<p>{body.email_body_notes}</p>"
    body_html += "<p>Best regards</p>"

    sent = outlook.send_email(
        from_email=body.tl_email,
        to_email=body.client_email,
        subject=body.email_subject,
        body=body_html,
        attachment_path=submission.get("formatted_doc_path"),
    )

    now_ts = datetime.now(timezone.utc).isoformat()
    db.tl_approve_submission(body.submission_id)
    db.get_client().table("submissions").update({
        "sent_to_client_at": now_ts,
        "final_status": "Submitted",
    }).eq("id", body.submission_id).execute()

    db.upsert_candidate_details(
        submission["candidate_id"], submission["requirement_id"],
        {"status": "submitted_to_client"})

    # GeBIZ handling
    if requirement.get("market") == "SG" and requirement.get("tender_number"):
        db.insert_gebiz_submission(
            submission["candidate_id"],
            requirement["tender_number"],
            school_name=requirement.get("location"),
        )

    return {"sent": True, "sent_at": now_ts}


# ── POST /tl/reject ───────────────────────────────────────────

@app.post("/tl/reject")
async def tl_reject(
    body: TLRejectRequest,
    x_user_role: str = Header(),
):
    _require_role(x_user_role, ["tl"])

    rows = (db.get_client().table("submissions")
            .select("id, candidate_id, requirement_id")
            .eq("id", body.submission_id).execute().data)
    if not rows:
        raise HTTPException(404, "Submission not found")
    submission = rows[0]

    db.update_submission_status(body.submission_id, "rejected_by_tl")
    db.upsert_candidate_details(
        submission["candidate_id"], submission["requirement_id"],
        {"status": "rejected_by_tl", "tl_feedback": body.feedback})

    return {"status": "rejected", "submission_id": body.submission_id}


# ── GET /pipeline ──────────────────────────────────────────────

@app.get("/pipeline")
async def pipeline_summary(
    market: str | None = None,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    reqs = db.get_open_requirements(market)
    if not reqs:
        return {"pipeline": []}

    # Limit to most recent 50 requirements to avoid PostgREST query size limits
    reqs = reqs[:50]
    req_ids = [r["id"] for r in reqs]

    # Batch-fetch all related data in 4 queries instead of 4*N
    from collections import defaultdict
    scr_by_req = defaultdict(list)
    out_by_req = defaultdict(list)
    sub_by_req = defaultdict(list)
    det_by_req = defaultdict(list)

    try:
        all_screenings = (db.get_client().table("screenings")
                          .select("requirement_id, recommendation")
                          .in_("requirement_id", req_ids).execute().data)
        for s in all_screenings:
            scr_by_req[s["requirement_id"]].append(s)
    except Exception:
        pass

    try:
        all_outreach = (db.get_client().table("outreach_log")
                        .select("requirement_id, reply_received")
                        .in_("requirement_id", req_ids).execute().data)
        for o in all_outreach:
            out_by_req[o["requirement_id"]].append(o)
    except Exception:
        pass

    try:
        all_submissions = (db.get_client().table("submissions")
                           .select("requirement_id, tl_approved, sent_to_client_at")
                           .in_("requirement_id", req_ids).execute().data)
        for s in all_submissions:
            sub_by_req[s["requirement_id"]].append(s)
    except Exception:
        pass

    try:
        all_details = (db.get_client().table("candidate_details")
                       .select("requirement_id, status")
                       .in_("requirement_id", req_ids).execute().data)
        for d in all_details:
            det_by_req[d["requirement_id"]].append(d)
    except Exception:
        pass

    pipeline = []
    for req in reqs:
        rid = req["id"]
        screenings = scr_by_req[rid]
        outreach = out_by_req[rid]
        submissions = sub_by_req[rid]
        details = det_by_req[rid]

        # Count sourced candidates by skill overlap with this requirement
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

        # Count LLM-matched candidates (scored above threshold)
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
            "outreached": len(outreach),
            "replied": sum(1 for o in outreach if o.get("reply_received")),
            "details_complete": sum(1 for d in details
                                    if d.get("status") == "ready_for_review"),
            "submitted_to_tl": len(submissions),
            "sent_to_client": sum(1 for s in submissions
                                  if s.get("sent_to_client_at")),
        })

    return {"pipeline": pipeline}


# ── GET /token-report ──────────────────────────────────────────

COST_PER_1K = {
    "claude-haiku-4-5-20251001": {"input": 0.001, "output": 0.005},
    "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
}


@app.get("/token-report")
async def token_report():
    log_files = sorted(LOG_DIR.glob("tokens_*.log"), reverse=True)
    if not log_files:
        return {"entries": [], "total_cost_usd": 0}

    latest = log_files[0]
    lines = latest.read_text().strip().split("\n")[-100:]
    entries = []
    total_cost = 0.0

    for line in lines:
        parts = line.split("|")
        if len(parts) != 5:
            continue
        ts, endpoint, model, inp, out = parts
        inp_tokens = int(inp)
        out_tokens = int(out)
        rates = COST_PER_1K.get(model, {"input": 0.003, "output": 0.015})
        cost = (inp_tokens * rates["input"] + out_tokens * rates["output"]) / 1000
        total_cost += cost
        entries.append({
            "timestamp": ts, "endpoint": endpoint, "model": model,
            "input_tokens": inp_tokens, "output_tokens": out_tokens,
            "cost_usd": round(cost, 6),
        })

    return {"entries": entries, "total_cost_usd": round(total_cost, 4),
            "log_file": latest.name}


# ── GET /health ────────────────────────────────────────────────

@app.get("/health")
async def health():
    checks = {"status": "ok", "supabase": False, "scheduler": False,
              "graph_api": False}
    try:
        db.get_client().table("requirements").select("id").limit(1).execute()
        checks["supabase"] = True
    except Exception:
        pass

    checks["scheduler"] = scheduler.running

    test_email = os.environ.get("STARTUP_TEST_EMAIL")
    if test_email:
        try:
            outlook.get_access_token(test_email)
            checks["graph_api"] = True
        except Exception:
            pass

    checks["status"] = ("ok" if all([checks["supabase"], checks["scheduler"]])
                        else "degraded")
    return checks


# ── LLM semantic matching ──────────────────────────────────────

MATCH_BATCH_SIZE = 20
MATCH_MIN_SCORE = 60


def _build_requirement_summary(requirement: dict) -> str:
    """Build a compact requirement string for the LLM scoring prompt."""
    parts = [
        f"Role: {requirement.get('role_title', 'N/A')}",
        f"Client: {requirement.get('client_name', 'N/A')}",
        f"Skills: {', '.join(requirement.get('skills_required', []))}",
    ]
    if requirement.get("skillset"):
        parts.append(f"Skillset description: {requirement['skillset']}")
    if requirement.get("experience_min"):
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
    """Build a compact candidate string for the LLM scoring prompt."""
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


def _score_candidate_batch(candidates: list[dict],
                           requirement: dict) -> list[dict]:
    """Send a batch of candidates to the LLM for semantic scoring.
    Returns list of {candidate_id, score, reasoning}."""
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
        prompt,
        max_tokens=4096,
        endpoint="/match-scoring",
    )

    parsed = _parse_llm_json(result_text)
    if not isinstance(parsed, list):
        return []

    # Validate and clean results
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
    """Run LLM-based semantic matching for a requirement.
    1. Broad-query candidates by market
    2. Check cache, skip already-scored
    3. Score uncached in batches of 20
    4. Cache results, return those above threshold"""
    requirement_id = requirement["id"]

    # Broad query: same market, up to 200 most recent candidates
    all_candidates = db.search_candidates_broad(
        market=requirement.get("market"),
        limit=200,
    )
    if not all_candidates:
        return []

    # Check cache
    all_ids = [c["id"] for c in all_candidates]
    cached = db.get_cached_match_scores(requirement_id, all_ids)

    # Filter to uncached only
    to_score = [c for c in all_candidates if c["id"] not in cached]

    # Score in batches of 20
    new_scores = []
    for i in range(0, len(to_score), MATCH_BATCH_SIZE):
        batch = to_score[i:i + MATCH_BATCH_SIZE]
        batch_scores = _score_candidate_batch(batch, requirement)
        new_scores.extend(batch_scores)

    # Cache new scores
    if new_scores:
        db.upsert_match_scores(requirement_id, new_scores)

    # Merge cached + new, filter above threshold, sort desc
    all_scores = {}
    for cid, data in cached.items():
        all_scores[cid] = data
    for s in new_scores:
        all_scores[s["candidate_id"]] = {
            "score": s["score"], "reasoning": s["reasoning"],
        }

    matched = [
        {"candidate_id": cid, **data}
        for cid, data in all_scores.items()
        if data["score"] >= MATCH_MIN_SCORE
    ]
    matched.sort(key=lambda x: x["score"], reverse=True)
    return matched


# ── Internal skill runners ─────────────────────────────────────

def _screen_candidates(requirement_id: str, requirement: dict,
                       just_sourced: list[dict]):
    """Screen candidates against a requirement. Runs in background."""
    skill_matched = db.search_candidates_by_skill(
        requirement.get("skills_required", []),
        requirement.get("market"),
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
                max_tokens=1024,
                endpoint="/source-and-screen",
            )
            screening = json.loads(result_text)
        except (json.JSONDecodeError, Exception) as e:
            log.error("Screening error for %s: %s",
                      cand.get("name", cand["id"]), e)
            continue

        screening["candidate_id"] = cand["id"]
        screening["requirement_id"] = requirement_id
        db.insert_screening(screening)
        log.info("Screened %s: score=%s rec=%s",
                 cand.get("name", "?"),
                 screening.get("score"),
                 screening.get("recommendation"))


async def _run_source_and_screen(requirement_id: str) -> dict:
    requirement = db.get_requirement_by_id(requirement_id)
    if not requirement:
        return {"error": "Requirement not found"}

    source_results = await sourcing.run_all_sources(requirement)
    linkedin_str = sourcing.generate_linkedin_search_string(requirement)

    # LLM-based semantic matching (replaces keyword .overlaps())
    matched = _match_candidates_for_requirement(requirement)

    # Screen top matches via the screener agent
    screened = 0
    shortlisted = 0
    top_candidates = []
    skipped_existing = 0
    parse_errors = 0

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
            skipped_existing += 1
            continue

        result_text = _call_claude(
            "claude-sonnet-4-20250514",
            AGENTS.get("screener", ""),
            f"Score this candidate.\nCANDIDATE: {cand}\n"
            f"REQUIREMENT: {requirement}\nReturn JSON only.",
            max_tokens=1024,
            endpoint="/source-and-screen",
        )
        try:
            screening = json.loads(result_text)
        except json.JSONDecodeError:
            parse_errors += 1
            log.error("Screening parse error for candidate %s: %s",
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
        "_debug": {
            "just_sourced_count": len(just_sourced),
            "skill_matched_count": len(skill_matched),
            "total_candidates_to_screen": len(candidates),
            "skipped_already_screened": skipped_existing,
            "parse_errors": parse_errors,
        },
    }


async def _run_process_inbox(recruiter_email: str | None) -> dict:
    if recruiter_email:
        recruiter_list = [recruiter_email]
    else:
        team_emails = os.environ.get("RECRUITER_EMAILS", "")
        recruiter_list = [e.strip() for e in team_emails.split(",")
                          if e.strip()]

    totals = {"processed": 0, "candidate_replies": 0,
              "new_requirements_flagged": 0, "chase_drafts_pending": 0,
              "errors": 0}

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
                max_tokens=20,
                endpoint="/inbox/classify",
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
                            max_tokens=2048,
                            endpoint="/inbox/parse-reply",
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

            if "candidate_reply" in classification or "new_requirement" in classification:
                try:
                    outlook.mark_as_read(email, msg["message_id"])
                except Exception:
                    pass

    return totals


# ── v2: Market Intelligence endpoints ─────────────────────────

class MarketScanRequest(BaseModel):
    skills: list[str]
    market: str = "IN"
    location: str | None = None
    role_title: str | None = None


@app.post("/market/scan")
async def run_market_scan(
    body: MarketScanRequest,
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    """Run all market intelligence channels — TheirStack, Google Jobs,
    Adzuna, MCF — and return job postings + salary benchmarks."""
    _require_role(x_user_role, ["tl"])
    results = await market_intelligence.run_market_scan(
        skills=body.skills,
        market=body.market,
        location=body.location,
        role_title=body.role_title,
    )
    return {"status": "ok", **results}


@app.post("/market/brief")
async def generate_market_brief(
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    """Generate a Market Intelligence Brief using all available data.
    Runs market scan across all channels, cross-references with internal
    candidate pool and open requirements, then produces an actionable brief."""
    _require_role(x_user_role, ["tl"])

    # Get all open requirements
    open_reqs = db.get_open_requirements(market=None)
    if not open_reqs:
        return {"status": "no_open_requirements"}

    # Collect unique skills across all open reqs
    all_skills = set()
    for req in open_reqs:
        for skill in req.get("skills_required", []):
            all_skills.add(skill)

    # Run market scan for both markets
    scan_in = await market_intelligence.run_market_scan(
        skills=list(all_skills), market="IN")
    scan_sg = await market_intelligence.run_market_scan(
        skills=list(all_skills), market="SG")

    # Get internal candidate summary (count by market)
    all_candidates_in = db.search_candidates_broad(market="IN", limit=500)
    all_candidates_sg = db.search_candidates_broad(market="SG", limit=500)

    # Build the intelligence brief via LLM
    mi_agent_prompt = AGENTS.get("market_intelligence", "")
    if not mi_agent_prompt:
        return {"status": "error", "detail": "market_intelligence agent not loaded"}

    user_msg = json.dumps({
        "market_data": {
            "india": {k: v for k, v in scan_in.items()
                      if k != "salary_benchmark"},
            "singapore": {k: v for k, v in scan_sg.items()
                          if k != "salary_benchmark"},
        },
        "salary_benchmarks": {
            "india": scan_in.get("salary_benchmark"),
            "singapore": scan_sg.get("salary_benchmark"),
        },
        "internal_candidates": {
            "india_count": len(all_candidates_in),
            "singapore_count": len(all_candidates_sg),
        },
        "open_requirements": [
            {"id": r["id"], "role_title": r.get("role_title"),
             "market": r.get("market"), "skills": r.get("skills_required")}
            for r in open_reqs[:20]
        ],
    }, default=str)

    brief_raw = _call_claude(
        "claude-sonnet-4-20250514",
        mi_agent_prompt,
        user_msg,
        max_tokens=4096,
        endpoint="/market/brief",
    )
    brief = _parse_llm_json(brief_raw)

    return {"status": "ok", "brief": brief,
            "jobs_scanned": scan_in.get("total_jobs", 0) + scan_sg.get("total_jobs", 0)}


# ── v2: Reactivation endpoint ─────────────────────────────────

@app.post("/candidates/reactivation-scan")
async def reactivation_scan(
    x_user_role: str = Header(),
    x_user_email: str = Header(),
):
    """Scan dormant candidates against all open requirements.
    Returns reactivation recommendations for each requirement."""
    _require_role(x_user_role, ["tl", "recruiter"])

    open_reqs = db.get_open_requirements(market=None)
    if not open_reqs:
        return {"status": "no_open_requirements", "recommendations": []}

    reactivation_agent_prompt = AGENTS.get("reactivation", "")
    if not reactivation_agent_prompt:
        return {"status": "error", "detail": "reactivation agent not loaded"}

    all_recommendations = []

    for req in open_reqs[:10]:  # Limit to 10 reqs per scan to control costs
        market = req.get("market")

        # Get dormant candidates (sourced 30+ days ago)
        all_candidates = db.search_candidates_broad(market=market, limit=200)
        if not all_candidates:
            continue

        # Filter to dormant-ish candidates (simple heuristic — will enhance
        # with proper last_contacted_at filtering when column is added)
        dormant = all_candidates  # TODO: filter by last_contacted_at > 30 days

        if not dormant:
            continue

        # Run reactivation agent
        user_msg = json.dumps({
            "requirement": {
                "id": req["id"],
                "role_title": req.get("role_title"),
                "skills_required": req.get("skills_required", []),
                "experience_min": req.get("experience_min"),
                "location": req.get("location"),
                "market": market,
                "salary_budget": req.get("salary_budget"),
            },
            "candidates": [
                {
                    "id": c["id"],
                    "name": c.get("name"),
                    "skills": c.get("skills", []),
                    "total_experience": c.get("total_experience"),
                    "current_location": c.get("current_location"),
                    "current_job_title": c.get("current_job_title"),
                    "current_employer": c.get("current_employer"),
                    "source": c.get("source"),
                }
                for c in dormant[:50]  # Top 50 per req to control token usage
            ],
        }, default=str)

        result_raw = _call_claude(
            "claude-haiku-4-5-20251001",
            reactivation_agent_prompt,
            user_msg,
            max_tokens=4096,
            endpoint="/candidates/reactivation-scan",
        )
        recs = _parse_llm_json(result_raw)

        if isinstance(recs, list):
            for rec in recs:
                rec["requirement_id"] = req["id"]
                rec["requirement_title"] = req.get("role_title")
            all_recommendations.extend(recs)

    # Sort by match_score descending
    all_recommendations.sort(
        key=lambda x: x.get("match_score", 0), reverse=True)

    return {
        "status": "ok",
        "requirements_scanned": min(len(open_reqs), 10),
        "recommendations": all_recommendations[:30],  # Top 30 overall
    }


# ── v2: JD Parse standalone endpoint ─────────────────────────

class JDParseRequest(BaseModel):
    jd_text: str
    market: str = "IN"


@app.post("/agents/parse-jd")
async def parse_jd(body: JDParseRequest):
    """Standalone JD parsing — extract structured fields from raw JD text.
    Uses the dedicated jd_parser agent (Sonnet 4) for full extraction."""
    jd_parser_prompt = AGENTS.get("jd_parser", "")
    if not jd_parser_prompt:
        raise HTTPException(500, "jd_parser agent not loaded")

    result_raw = _call_claude(
        "claude-sonnet-4-20250514",
        jd_parser_prompt,
        f"Market: {body.market}\n\nJD text:\n{body.jd_text}",
        max_tokens=2048,
        endpoint="/agents/parse-jd",
    )
    parsed = _parse_llm_json(result_raw)
    if not isinstance(parsed, dict):
        raise HTTPException(422, "Failed to parse JD — LLM returned invalid JSON")

    return {"status": "ok", "parsed": parsed}


# ── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", os.environ.get("AI_PORT", "8001")))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
