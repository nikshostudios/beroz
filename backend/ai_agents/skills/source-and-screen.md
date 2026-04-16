# Source and Screen Skill

## Trigger
- TL creates a new requirement
- Recruiter clicks "Source Now" in web app

## Input
- `requirement_id` uuid

## Output
```json
{
  "sourced": "int",
  "screened": "int",
  "shortlisted": "int",
  "linkedin_search_string": "str",
  "top_candidates": [{"candidate_id": "", "score": 0, "recommendation": "", "reasoning": ""}]
}
```

## Model Routing
- claude-haiku-4-5: search param parsing, deduplication, DB writes
- claude-sonnet-4: screening scoring, LinkedIn boolean string

## Steps

1. **Load requirement** from Supabase `requirements` table by `requirement_id`
2. **Run sourcing agent** with requirement dict
   - All channels run in parallel (Foundit, MyCareersFuture, Apollo)
   - LinkedIn: generate search string only, no scraping
   - Deduplicate by email, fallback name + employer
   - Upsert new candidates into `candidates` table
3. **For each new candidate**: run screener agent automatically
   - Input: candidate dict + requirement dict
   - Screener writes result to `screenings` table
4. **Aggregate results**:
   - Count sourced, screened, shortlisted (recommendation = "shortlist")
   - Sort by score descending
   - Top candidates = top 10 by score
5. **Return** full output to web app

## Error Handling
- If a sourcing channel fails: log error, continue with other channels
- If screener fails for a candidate: log, skip, continue with next
- All errors logged to `/logs/sourcing_YYYYMMDD.log`

## DB Tables Touched
- Read: `requirements`, `portal_credentials`
- Write: `candidates` (upsert), `screenings` (insert)
