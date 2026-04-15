# Sourcing Agent

## Role
- Find matching candidates across multiple channels simultaneously

## Model
- claude-haiku-4-5 for search param generation
- claude-sonnet-4 for LinkedIn boolean string generation

## Input
- `requirement` dict: skills, experience, salary, location, market, contract_type

## Output JSON
```json
{
  "sourced_count": {"foundit": 0, "mycareersfuture": 0, "apollo": 0},
  "total_unique": 0,
  "linkedin_search_string": "string"
}
```

## Channels (run in parallel via asyncio)

### 1. MyCareersFuture API (SG market only)
- Free public API -- no credentials needed
- Search by skills, location, salary range
- Filter for Singapore Citizens/PRs when GeBIZ

### 2. Foundit (both markets, primarily India)
- Via Firecrawl scraping
- Use shared company credentials from Supabase `portal_credentials` table
- Rotate between available accounts -- don't hammer one account
- Search by skills, experience, location

### 3. Apollo.io Professional API (both markets)
- Passive candidate sourcing
- Search by job title, skills, company, location
- Use for candidates not actively looking

### 4. LinkedIn (manual -- search string only)
- **NEVER scrape LinkedIn**
- Generate optimal boolean search string for recruiter to copy-paste
- Format: `("skill1" OR "skill2") AND ("title1" OR "title2") AND location`

## Steps

1. Parse requirement -> build search params per channel
2. Run all applicable channels in parallel via asyncio
3. Deduplicate by email (fallback: name + current employer match)
4. Upsert all into Supabase `candidates` table with `source` field tagged
5. Run screener agent on every new candidate vs this requirement
6. Generate LinkedIn boolean search string

## Rules
- LinkedIn: NEVER scrape -- generate search string only
- Foundit: rotate across available portal credentials
- SG market: flag any candidate who is not Singaporean/PR (required for most GeBIZ roles)
- Log every sourcing run to `/logs/sourcing_YYYYMMDD.log`
- Deduplicate before inserting -- check email first, then name+employer

## Autonomy
- Full -- triggered by TL creating requirement or recruiter clicking "Source Now"

## DB Writes
- Upsert into `candidates`: all sourced candidate fields, source channel tagged
- Trigger `screener` agent for each new candidate found
- Log run details to file system
