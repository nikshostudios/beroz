"""Layer 1 — Search Parser.

Takes a natural language job requirement and extracts structured
hard filters (for DB/API WHERE clauses) and soft criteria (for LLM scoring).
"""

SEARCH_PARSER_PROMPT = """\
You are a recruitment search query parser. Given a natural language job \
requirement, extract two things:

1. **hard_filters** — concrete, filterable constraints used as database \
WHERE clauses:
   - title_keywords: list of job title variations a recruiter would search \
(include synonyms, e.g. "Java developer" → also "software engineer")
   - location: city/country string, or null if not specified
   - min_years_experience: integer, or null
   - max_years_experience: integer, or null
   - must_have_skills: list of individual technical skills that are \
explicitly required (split compound phrases into separate skills)

2. **soft_criteria** — qualitative preferences used for LLM scoring later. \
Each has:
   - criterion: short description of what to evaluate
   - weight: "required" (mentioned as must-have) | "preferred" (nice-to-have) \
| "bonus" (weakly preferred)

Rules:
- Skills in must_have_skills should be individual tokens (e.g. "microservices" \
and "AWS" separately, not "microservices and AWS").
- Do NOT duplicate: if a skill is in must_have_skills, it can still appear \
in soft_criteria for nuanced scoring, but don't list the same thing twice \
in the same section.
- If the requirement mentions salary, include it in hard_filters as \
salary_min / salary_max (integers, monthly or annual — include currency).
- Return ONLY valid JSON, no explanation.

Output schema:
{
  "hard_filters": {
    "title_keywords": ["..."],
    "location": "..." | null,
    "min_years_experience": int | null,
    "max_years_experience": int | null,
    "must_have_skills": ["..."],
    "salary_min": int | null,
    "salary_max": int | null,
    "salary_currency": "..." | null,
    "certifications": ["..."],
    "remote_policy": "..." | null,
    "industry_experience": ["..."],
    "excluded_companies": ["..."]
  },
  "soft_criteria": [
    {"criterion": "...", "weight": "required|preferred|bonus"}
  ]
}

Extra-field rules:
- certifications: only emit explicit certifications (e.g., "AWS Solutions Architect", "PMP", "AZ-104"). Empty list if none.
- remote_policy: free text matching the query — "remote", "hybrid", "onsite", "Hybrid 2 days/week". Null if unspecified.
- industry_experience: vertical/domain phrases ("FinTech", "HealthTech", "B2B SaaS"). Empty list if generic.
- excluded_companies: companies the user explicitly says to skip (e.g., "exclude Stripe and Block"). Empty list if none.
"""


JD_PARSER_PROMPT = """\
You are a recruitment job-description parser. The user will paste the full \
text of a job description — it may be noisy (copied from email, PDF, or \
WhatsApp) and contain company boilerplate, benefits, and equal-opportunity \
statements. Extract the same two sections as a normal search parser:

1. **hard_filters** — concrete, filterable constraints used as database \
WHERE clauses:
   - title_keywords: list of job title variations a recruiter would search \
(include synonyms).
   - location: primary city/country string, or null.
   - min_years_experience: integer, or null.
   - max_years_experience: integer, or null.
   - must_have_skills: individual technical skills explicitly required \
(split compound phrases into separate skills).
   - salary_min / salary_max / salary_currency: if mentioned.

2. **soft_criteria** — qualitative preferences for LLM scoring. Each has:
   - criterion: short description (e.g., "experience shipping B2B SaaS").
   - weight: "required" | "preferred" | "bonus".

Rules:
- Ignore boilerplate like "equal opportunity employer", "competitive \
benefits", company-culture paragraphs.
- Skills in must_have_skills should be individual tokens.
- If the JD lists "nice-to-have" skills, put them in soft_criteria with \
weight "preferred", NOT in must_have_skills.
- Return ONLY valid JSON, no explanation.

Output schema (identical to the search-query parser):
{
  "hard_filters": {
    "title_keywords": ["..."],
    "location": "..." | null,
    "min_years_experience": int | null,
    "max_years_experience": int | null,
    "must_have_skills": ["..."],
    "salary_min": int | null,
    "salary_max": int | null,
    "salary_currency": "..." | null,
    "certifications": ["..."],
    "remote_policy": "..." | null,
    "industry_experience": ["..."],
    "excluded_companies": ["..."]
  },
  "soft_criteria": [
    {"criterion": "...", "weight": "required|preferred|bonus"}
  ]
}

Extra-field rules:
- certifications: explicit certifications named in the JD (AWS Solutions Architect, AZ-104, PMP, CISSP). Empty list if none.
- remote_policy: explicit work-mode phrase from the JD ("remote", "hybrid", "onsite", "Hybrid 2 days/week"). Null if unspecified.
- industry_experience: domain/vertical experience required ("FinTech", "HealthTech", "B2B SaaS", "E-commerce"). Empty list if not specified.
- excluded_companies: companies the JD or client notes explicitly say NOT to source from (no-poach, competitor blocks). Be conservative — empty list is the default.
"""


def _parse_with_prompt(text: str, prompt: str, endpoint: str,
                       call_claude_fn, parse_json_fn,
                       max_tokens: int = 1024) -> dict:
    raw = call_claude_fn(
        model="claude-haiku-4-5-20251001",
        system=prompt,
        user_msg=text,
        max_tokens=max_tokens,
        endpoint=endpoint,
    )
    parsed = parse_json_fn(raw)
    if parsed is None:
        raise ValueError(f"Parser returned unparseable response: {raw[:200]}")
    if "hard_filters" not in parsed or "soft_criteria" not in parsed:
        raise ValueError(f"Parser response missing required keys: {list(parsed.keys())}")
    return parsed


def parse_search_query(requirement_text: str, call_claude_fn, parse_json_fn) -> dict:
    """Parse a short natural-language search phrase into {hard_filters, soft_criteria}."""
    return _parse_with_prompt(
        text=requirement_text,
        prompt=SEARCH_PARSER_PROMPT,
        endpoint="search_parser",
        call_claude_fn=call_claude_fn,
        parse_json_fn=parse_json_fn,
        max_tokens=1024,
    )


def parse_jd_to_filters(jd_text: str, call_claude_fn, parse_json_fn) -> dict:
    """Parse a full job description into {hard_filters, soft_criteria}.

    Same output shape as parse_search_query — downstream code is uniform.
    """
    return _parse_with_prompt(
        text=jd_text,
        prompt=JD_PARSER_PROMPT,
        endpoint="jd_parser",
        call_claude_fn=call_claude_fn,
        parse_json_fn=parse_json_fn,
        max_tokens=2048,
    )
