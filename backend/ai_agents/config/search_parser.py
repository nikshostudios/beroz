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
    "salary_currency": "..." | null
  },
  "soft_criteria": [
    {"criterion": "...", "weight": "required|preferred|bonus"}
  ]
}
"""


def parse_search_query(requirement_text: str, call_claude_fn, parse_json_fn) -> dict:
    """Parse a natural language requirement into structured filters + criteria.

    Args:
        requirement_text: Natural language job requirement string.
        call_claude_fn: Reference to main._call_claude (avoids circular import).
        parse_json_fn: Reference to main._parse_llm_json.

    Returns:
        Dict with hard_filters and soft_criteria, or raises ValueError.
    """
    raw = call_claude_fn(
        model="claude-haiku-4-5-20251001",
        system=SEARCH_PARSER_PROMPT,
        user_msg=requirement_text,
        max_tokens=1024,
        endpoint="search_parser",
    )
    parsed = parse_json_fn(raw)
    if parsed is None:
        raise ValueError(f"Search parser returned unparseable response: {raw[:200]}")

    # Validate required keys
    if "hard_filters" not in parsed or "soft_criteria" not in parsed:
        raise ValueError(f"Search parser response missing required keys: {list(parsed.keys())}")

    return parsed
