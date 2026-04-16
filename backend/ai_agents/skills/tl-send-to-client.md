# TL Send to Client Skill

## Trigger
- TL clicks "Approve + Send to Client" in web app

## Input
- `submission_id` uuid

## Output
```json
{
  "sent": true,
  "sent_at": "timestamp"
}
```

## Model Routing
- No LLM needed -- this is a send + DB update flow

## Steps

1. **Load submission** from Supabase `submissions` table by `submission_id`
2. **Verify user role** = TL before proceeding
   - If not TL: return error, do not send
3. **Attach** file at `formatted_doc_path` to email
4. **Send** via TL's Microsoft Graph API Outlook account
   - To: client contact email (from `requirements` or `client_contacts`)
   - Subject: standard submission subject with candidate name + role
   - Body: brief cover note + attachment
5. **Update** Supabase `submissions`:
   - `tl_approved` = true
   - `tl_approved_at` = now()
   - `sent_to_client_at` = now()
   - `final_status` = "Submitted"
6. **Update** `candidate_details` status to `submitted_to_client`
7. **For GeBIZ**: update `gebiz_submissions` table
   - Set tender_number, school_name, submission_date = today
8. **Log** to `/logs/submissions_YYYYMMDD.log`

## Access Control
- Requires TL role -- check user role before executing
- Recruiters cannot trigger this skill

## DB Tables Touched
- Read: `submissions`, `candidates`, `requirements`, `client_contacts`
- Write: `submissions` (update), `candidate_details` (update status), `gebiz_submissions` (insert/update for SG)
