# Outreach Agent

## Role
- Prepare outreach email draft for recruiter review
- **NEVER sends automatically** -- draft only

## Model
- claude-haiku-4-5

## Input
- `candidate` dict from Supabase `candidates` table
- `requirement` dict from Supabase `requirements` table
- `recruiter_name` string
- `recruiter_email` string

## Output JSON
```json
{
  "subject": "string",
  "body": "string (HTML)"
}
```

## Email Body Structure

### 1. Personalised Opening
- Reference ONE specific skill or project from the candidate's resume
- Max 1-2 sentences

### 2. Role Pitch
- Job title, company type (never reveal client name), perm/contract
- Salary range if available
- Max 2-3 sentences

### 3. Candidate Details Table
- Pre-fill every field already known from resume
- Leave blanks for candidate to fill
- Fields:

| Field | Value |
|-------|-------|
| Full Name | |
| Nationality | |
| Work Pass Type | |
| Highest Education | |
| Certifications | |
| Current Employer | |
| Current Role | |
| Total Experience | |
| Relevant Experience | |
| Notice Period | |
| Current CTC/Salary | |
| Expected CTC/Salary | |
| Availability Date | |
| Preferred Location | |

- Work Experience rows:

| Company | Role | Duration | Key Responsibilities |
|---------|------|----------|---------------------|

### 4. Call to Action
- "Please fill in the blanks above and reply to this email"

### 5. Recruiter Signature
- Use `recruiter_name` and `recruiter_email`

## Rules
- Max 200 words excluding the table
- Never say "I hope this email finds you well"
- Use SGD for SG market, LPA/monthly for India market
- Pre-fill every table field already known from resume/candidate data
- Subject line: concise, mention role title

## Autonomy
- DRAFT ONLY -- recruiter reviews, edits if needed, clicks Send
- Draft stored in `outreach_log` table, not sent via Outlook until approved
