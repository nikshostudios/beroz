# Process Inbox Skill

## Trigger
- APScheduler cron job every 15 minutes
- Runs for all recruiter accounts in the team

## Output (per run)
```json
{
  "processed": "int",
  "candidate_replies": "int",
  "new_requirements_flagged": "int",
  "chase_drafts_pending": "int",
  "errors": "int"
}
```

## Model Routing
- claude-haiku-4-5: email classification, field extraction
- claude-sonnet-4: only if candidate reply is ambiguous (followup agent escalation)

## Steps

1. **Load recruiter list** from Supabase or env config
2. **For each recruiter**:
   a. Call `outlook.get_unread_emails(recruiter_email, last_24h=True)`
   b. Classify each email with haiku-4-5: `candidate_reply` | `new_requirement` | `other`

3. **For `candidate_reply`**:
   a. Match to `outreach_log` by sender email OR `outlook_thread_id`
   b. If matched: run followup agent with email_body + candidate_id + requirement_id
   c. Followup agent extracts table fields -> updates `candidate_details`
   d. If all required fields complete -> set status `ready_for_review`, flag in web app
   e. If incomplete -> store `chase_draft` in Supabase for recruiter approval

4. **For `new_requirement`**:
   a. Extract JD text from email body/attachment using haiku-4-5
   b. Flag for TL in web app with extracted text
   c. Do NOT auto-create requirement -- TL reviews first

5. **For `other`**: skip, no action

6. **Log all** to `/logs/inbox_YYYYMMDD.log`

## Matching Logic
- Primary: match sender email to `candidates.email` via `outreach_log`
- Secondary: match `outlook_thread_id` to `outreach_log.outlook_thread_id`
- If no match found: classify as `other`, log for manual review

## DB Tables Touched
- Read: `outreach_log`, `candidates`, `candidate_details`
- Write: `candidate_details` (update), `outreach_log` (update reply_received)
