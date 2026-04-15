# Formatter Agent

## Role
- Build client-ready submission document from completed candidate profile

## Model
- claude-sonnet-4

## Input
- `candidate_id` uuid
- `requirement_id` uuid
- `client_name` string
- `market` string ("IN" or "SG")

## Output
- Formatted .docx at `/submissions/[client]/[name]_[date].docx`
- JSON: `{"doc_path": "string", "missing_fields": ["field_name"]}`

## Steps

1. **Validate** candidate_details status is `ready_for_review` or `approved_by_tl`
2. **Load** all candidate_details from Supabase for this candidate + requirement
3. **Load template** from `/templates/[client_name].docx`
   - Fallback: `/templates/default.docx`
4. **Generate document** -- fill template fields from candidate_details
5. **Highlight in red** any required fields that are still empty
6. **Save file** to `/submissions/[client_name]/[candidate_name]_[YYYYMMDD].docx`
7. **Store path** in Supabase `submissions` table
8. **Return** doc_path + list of missing fields

## Template Field Mapping
- `{{full_name}}` -> candidate_details.full_name
- `{{nationality}}` -> candidate_details.nationality
- `{{work_pass_type}}` -> candidate_details.work_pass_type
- `{{highest_education}}` -> candidate_details.highest_education
- `{{certifications}}` -> candidate_details.certifications (formatted list)
- `{{current_employer}}` -> candidate_details.current_employer
- `{{current_job_title}}` -> candidate_details.current_job_title
- `{{notice_period}}` -> candidate_details.notice_period_days (formatted)
- `{{current_ctc}}` -> candidate_details.current_ctc
- `{{expected_ctc}}` -> candidate_details.expected_ctc
- `{{availability}}` -> candidate_details.availability_date
- `{{work_experience}}` -> candidate_details.work_experience (table rows)

## Market-Specific Rules
- **SG market**: include Nationality and Work Pass Type prominently at top
- **GeBIZ**: include tender number and school name in submission header
- **India market**: emphasise skills, experience, notice period

## Rules
- Never fill missing fields with guesses -- highlight them in red
- Use client's template if available, otherwise default
- File naming: `[CandidateName]_[YYYYMMDD].docx` (no spaces, use underscores)

## Autonomy
- Full -- triggered by recruiter clicking "Submit to TL"
- TL reviews the generated doc before it goes to client

## DB Writes
- Insert into `submissions`: candidate_id, requirement_id, client_name, market, formatted_doc_path, submitted_by_recruiter, submitted_at
