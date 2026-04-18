# ExcelTech AI Agent Layer — Context Rules

## Model routing decision table

| Task                        | Model                          | Agent file  | Max tokens |
|-----------------------------|--------------------------------|-------------|------------|
| Email classification        | claude-haiku-4-5-20251001      | (inline)    | 20         |
| JD skill extraction         | claude-haiku-4-5-20251001      | screener    | 512        |
| Inbox scan / reply parsing  | claude-haiku-4-5-20251001      | followup    | 2048       |
| Outreach email drafting     | claude-haiku-4-5-20251001      | outreach    | 2048       |
| Candidate screening         | claude-sonnet-4-20250514       | screener    | 1024       |
| Submission formatting       | claude-sonnet-4-20250514       | formatter   | 4096       |

**Rule**: Never use Sonnet for classification/parsing. Never use Haiku for screening/formatting.

## File loading strategy

- Agent `.md` files: loaded once at startup into `AGENTS` dict (main.py lifespan)
- Skill `.md` files: loaded once at startup into `SKILLS` dict
- Never re-read from disk per request
- If agent files change, restart the FastAPI process

## Cron schedule

| Job             | Interval | Config                                          |
|-----------------|----------|-------------------------------------------------|
| Inbox scan      | 15 min   | misfire_grace=60s, max_instances=1              |

- Scans ALL recruiter inboxes in parallel (asyncio.gather)
- Recruiter list from RECRUITER_EMAILS env var or Supabase portal_credentials
- Per-recruiter errors logged and skipped — never blocks other recruiters
- Logs to /logs/cron_YYYYMMDD.log

## Sourcing channel priority per market

### India (IN)
1. **Foundit** — primary channel, 3 shared accounts, credential rotation
2. **Apollo.io** — passive candidate enrichment
3. **LinkedIn** — manual only, AI generates boolean search string

### Singapore (SG)
1. **MyCareersFuture** — free government API, primary for SG
2. **Foundit** — secondary, same accounts as India
3. **Apollo.io** — enrichment
4. **LinkedIn** — manual only

All channels run in parallel via `asyncio.gather` in `sourcing.run_all_sources()`.

## Status values by table

### submissions.final_status
Submitted | Shortlisted | KIV | Not Shortlisted | Selected-Joined | Selected |
Backed out | Rejected | Selected-Backed out

### submissions.placement_type
FTE | TP | C2H

### candidate_details.status
pending | details_received | ready_for_review | submitted_to_client

### requirements.status
open | closed | on_hold

## GeBIZ-specific rules

- Market is always "SG"
- One candidate can be submitted to multiple tenders simultaneously
- Each tender submission is a separate row in `interview_tracker`
- Nationality check: GeBIZ school roles often require Singaporean/PR — flag in screening
- Tender numbers format: MOESCHETQxxxxxxxx
- When TL approves and sends, auto-insert into interview_tracker if requirement has tender_number

## Foundit credential rotation

- 3 shared company accounts stored in Supabase `portal_credentials` table
- `random.choice(creds)` selects account per scraping run
- Two-step scraping: search page (free) → profile page (1 credit per click)
- `FOUNDIT_MAX_PROFILE_CLICKS = 20` per run to control credit spend
- `_basic_skills_match` filters candidates BEFORE spending credits on profile clicks
- If a credential fails (expired/locked), log error and try next credential

## Error handling strategy

| Context                  | Strategy                                              |
|--------------------------|-------------------------------------------------------|
| Cron inbox scan          | Log + continue to next recruiter                      |
| Sourcing channel failure | Log + continue, return partial results from other channels |
| Single candidate screen  | Log + skip, continue batch                            |
| Outlook API failure      | Log + return error to caller                          |
| Supabase insert failure  | Log + return error (don't silently swallow)            |
| LLM JSON parse failure   | Use safe defaults, log warning                        |
| Migration row error      | Log + continue to next row                            |

**General principle**: Fail fast for user-facing endpoints (return HTTP error).
Log + continue for batch/cron operations (never let one bad row kill the job).
