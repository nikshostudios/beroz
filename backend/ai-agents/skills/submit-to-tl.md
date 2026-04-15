# Submit to TL Skill

## Trigger
- Recruiter clicks "Submit to TL" in web app for a candidate

## Input
- `candidate_id` uuid
- `requirement_id` uuid
- `recruiter_email` string

## Output
```json
{
  "doc_path": "str",
  "missing_fields": ["field_name"]
}
```

## Model Routing
- claude-sonnet-4: formatter agent generates the .docx

## Steps

1. **Validate** `candidate_details` status = `ready_for_review`
   - If not: return error, do not proceed
2. **Load** all `candidate_details` from Supabase for this candidate + requirement
3. **Run formatter agent**:
   - Load template from `/templates/[client_name].docx` (fallback: `default.docx`)
   - Fill template fields from candidate_details
   - Highlight empty required fields in red
4. **Save** to `/submissions/[client_name]/[candidate_name]_[YYYYMMDD].docx`
5. **Insert** into Supabase `submissions` table:
   - candidate_id, requirement_id, client_name, market
   - formatted_doc_path, submitted_by_recruiter = recruiter_email
   - submitted_at = now(), tl_approved = false
6. **Return** doc_path + missing_fields list
7. Submission appears in **TL's Submission Queue** in web app

## Precondition
- Only runs when `candidate_details.status` = `ready_for_review`
- Rejects if status is anything else

## DB Tables Touched
- Read: `candidate_details`, `requirements`, `candidates`
- Write: `submissions` (insert)
