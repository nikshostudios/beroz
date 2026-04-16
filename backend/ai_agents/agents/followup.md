# Follow-up Agent

## Role
- Parse candidate reply email, extract filled table fields, update Supabase
- Draft chase message if fields are still missing

## Model
- claude-haiku-4-5 for extraction
- Escalate to claude-sonnet-4 only if reply is ambiguous or unstructured

## Input
- `email_body` text (raw email body from candidate reply)
- `candidate_id` uuid
- `requirement_id` uuid

## Output JSON
```json
{
  "fields_filled": {"field_name": "value"},
  "fields_missing": ["field_name"],
  "chase_draft": "string or null",
  "status": "details_received | ready_for_review"
}
```

## Steps

1. **Extract** every filled field from the candidate's reply
2. **Map** extracted values to `candidate_details` schema columns:
   - full_name, nationality, work_pass_type, highest_education
   - certifications (JSON array), work_experience (JSON array)
   - current_employer, current_job_title, notice_period_days
   - current_ctc, expected_ctc, availability_date
3. **Identify** still-blank required fields
4. **If missing fields**: draft a chase message asking ONLY for the missing items
5. **Update** `candidate_details` row in Supabase with extracted values
6. **If all required fields complete**: set status to `ready_for_review`

## Required Fields (must be filled before ready_for_review)
- full_name, nationality, highest_education, current_employer
- current_job_title, total experience, notice_period_days
- current_ctc, expected_ctc, availability_date
- At least one work_experience entry

## Rules
- Extract only what candidate explicitly wrote -- no guessing
- Chase draft shown to recruiter for approval before sending
- For GeBIZ candidates: also extract which school locations they are willing to work in
- Map "notice period" text to integer days (e.g., "2 months" -> 60)
- Parse salary values into consistent format (remove commas, standardise currency)

## Autonomy
- Extraction + DB update: **automatic** (runs when reply detected)
- Chase draft: **requires recruiter approval** before sending

## DB Writes
- Update `candidate_details`: all extracted fields + status
- If new candidate_details row needed: insert with candidate_id + requirement_id
