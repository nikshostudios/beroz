# Prepare Outreach Skill

## Trigger
- Recruiter selects a candidate and clicks "Prepare Outreach" in web app

## Input
- `candidate_id` uuid
- `requirement_id` uuid
- `recruiter_name` string
- `recruiter_email` string

## Output
- Phase 1 (draft): `{"draft_subject": "str", "draft_body": "str"}`
- Phase 2 (send): `{"sent": true, "outreach_log_id": "uuid"}`

## Model Routing
- claude-haiku-4-5: outreach agent drafts the email

## Steps

### Phase 1: Draft
1. **Load candidate** from Supabase `candidates` table
2. **Load requirement** from Supabase `requirements` table
3. **Run outreach agent** with candidate + requirement + recruiter info
   - Returns `{subject, body}` with pre-filled candidate details table
4. **Return draft** to web app for recruiter review and editing

### Phase 2: Send (recruiter clicks Send)
1. **Send email** via Microsoft Graph API from recruiter's own Outlook account
   - Use `outlook.send_email(recruiter_email, candidate.email, subject, body)`
2. **Capture** `outlook_message_id` and `outlook_thread_id` from Graph API response
3. **Insert** into Supabase `outreach_log`:
   - candidate_id, requirement_id, recruiter_email
   - outlook_message_id, outlook_thread_id
   - email_subject, sent_at, channel="email"
4. **Return** confirmation with outreach_log_id

## Critical Rule
- **NEVER send without recruiter clicking Send in web app**
- Phase 1 and Phase 2 are separate API calls
- Recruiter can edit subject and body before sending

## DB Tables Touched
- Read: `candidates`, `requirements`
- Write: `outreach_log` (insert after send)
