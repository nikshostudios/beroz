"""Market intelligence integrations — job posting + salary data (v2).

These functions query job POSTINGS and salary benchmarks (not candidate profiles).
They feed the Market Intelligence Agent and enhance the Screener Agent with
market-aware salary scoring.

Channels (v2):
1. MyCareersFuture — SG government API (free, job postings)
2. Foundit public — legacy Firecrawl/Scrape.do scrapers
3. TheirStack — 315K+ sources, job postings, company intel ($59/mo)
4. SerpApi/Google Jobs — 1000+ boards aggregated ($0.001/req)
5. Adzuna — job postings + salary benchmarks (free tier)
"""

import asyncio
import logging
import os
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SCRAPE_DO_BASE = "https://api.scrape.do"


# ── MyCareersFuture (SG only, free public API) ────────────────

async def search_mcf_jobs(skills: list[str], experience_min: str | None = None,
                          role_type: str | None = None) -> list[dict]:
    """Search Singapore government job portal for matching job postings.
    Returns job listings (title, company, salary, skills required).
    NOT candidate profiles."""
    params = {
        "search": " ".join(skills),
        "limit": 50,
        "page": 0,
        "sortBy": "new_posting_date",
    }
    if role_type:
        params["employmentTypes"] = role_type

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.mycareersfuture.gov.sg/v2/jobs",
            params=params,
        )
        resp.raise_for_status()

    results = []
    for item in resp.json().get("results", []):
        company = item.get("postedCompany", {}).get("name", "")
        salary = item.get("salary", {})
        sal_min = salary.get("minimum")
        sal_max = salary.get("maximum")
        sal_type = salary.get("type", {}).get("salaryType", "Monthly")
        skill_names = [s.get("skill", "") for s in item.get("skills", [])]

        results.append({
            "job_title": item.get("title", ""),
            "company": company,
            "skills_required": skill_names,
            "salary_min": sal_min,
            "salary_max": sal_max,
            "salary_type": sal_type,
            "location": "Singapore",
            "description": item.get("description", "")[:500],
            "job_url": f"https://www.mycareersfuture.gov.sg/job/{item.get('uuid', '')}",
            "min_experience": item.get("minimumYearsExperience"),
            "source": "mycareersfuture",
        })
    return results


# ── Foundit Public via Firecrawl ──────────────────────────────

def _basic_skills_match(skills_snippet: str, required_skills: list[str]) -> bool:
    """Quick keyword check — does the snippet contain at least one required skill?"""
    snippet_lower = skills_snippet.lower()
    return any(skill.lower() in snippet_lower for skill in required_skills)


async def _foundit_scrape_search_page(
    api_key: str, query: str, experience_min: str | None,
    location: str | None, credential: dict,
) -> list[dict]:
    """Scrape Foundit public search results page via Firecrawl.
    Returns job postings, NOT candidate profiles."""
    search_url = (f"https://www.foundit.in/srp/results?"
                  f"searchId=&query={query}"
                  f"&locations={location or 'India'}")
    if experience_min:
        search_url += f"&experienceRanges={experience_min}~15"

    search_schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "current_job_title": {"type": "string"},
                        "total_experience": {"type": "string"},
                        "current_location": {"type": "string"},
                        "skills_snippet": {"type": "string"},
                        "last_active": {"type": "string"},
                        "profile_url": {"type": "string"},
                    },
                },
            },
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "url": search_url,
                "formats": ["extract"],
                "extract": {"schema": search_schema},
                "actions": [
                    {"type": "navigate", "url": "https://www.foundit.in/login"},
                    {"type": "fill", "selector": "input[name='username']",
                     "value": credential["username"]},
                    {"type": "fill", "selector": "input[name='password']",
                     "value": credential["password"]},
                    {"type": "click", "selector": "button[type='submit']"},
                    {"type": "wait", "milliseconds": 3000},
                    {"type": "navigate", "url": search_url},
                    {"type": "wait", "milliseconds": 2000},
                ],
            },
        )
        resp.raise_for_status()

    extracted = resp.json().get("data", {}).get("extract", {})
    return extracted.get("candidates", [])


async def search_foundit_jobs_firecrawl(
    skills: list[str], experience_min: str | None = None,
    location: str | None = None, credential: dict | None = None,
) -> list[dict]:
    """Search Foundit public site for job postings via Firecrawl.
    Returns job listings, NOT candidate profiles."""
    if not credential:
        return []
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return []
    query = " ".join(skills)
    return await _foundit_scrape_search_page(
        api_key, query, experience_min, location, credential)


# ── Foundit Public via Scrape.do ──────────────────────────────

async def _scrape_do_get(url: str, render: bool = True) -> str:
    """Fetch a URL through scrape.do proxy. Returns raw HTML."""
    api_key = os.environ["SCRAPE_DO_API_KEY"]
    params = {
        "token": api_key,
        "url": url,
        "render": str(render).lower(),
        "waitUntil": "networkidle0",
    }
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.get(SCRAPE_DO_BASE, params=params)
        resp.raise_for_status()
    return resp.text


def _parse_foundit_search_html(html: str) -> list[dict]:
    """Extract job posting cards from Foundit search results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    cards = soup.select(".srpResultCardContainer")

    for card in cards:
        title_el = card.select_one(".jobTitle")
        company_el = card.select_one(".companyName p")
        exp_el = card.select_one(".experienceSalary .details")
        loc_el = card.select_one(".details.location")
        posted_el = card.select_one(".timeText")
        container = card.select_one(".cardContainer")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        job_id = container.get("id", "") if container else ""
        job_url = f"https://www.foundit.in/job/{job_id}" if job_id else ""

        results.append({
            "job_title": title,
            "company": company_el.get_text(strip=True) if company_el else "",
            "experience_range": exp_el.get_text(strip=True) if exp_el else "",
            "location": loc_el.get_text(strip=True) if loc_el else "",
            "job_url": job_url,
            "posted": posted_el.get_text(strip=True) if posted_el else "",
            "source": "foundit_public",
        })

    return results


async def search_foundit_jobs_scrape_do(
    skills: list[str], experience_min: str | None = None,
    location: str | None = None,
) -> list[dict]:
    """Search Foundit public site for job postings via Scrape.do.
    Returns job listings, NOT candidate profiles."""
    query = " ".join(skills)
    search_url = (f"https://www.foundit.in/srp/results?"
                  f"searchId=&query={quote_plus(query)}"
                  f"&locations={quote_plus(location or 'India')}")
    if experience_min:
        search_url += f"&experienceRanges={experience_min}~15"

    html = await _scrape_do_get(search_url)
    return _parse_foundit_search_html(html)


# ── TheirStack (315K+ job sources, $59/mo) ────────────────────

async def search_theirstack_jobs(
    skills: list[str],
    location: str | None = None,
    market: str = "IN",
    company_name: str | None = None,
    days_ago: int = 30,
    limit: int = 50,
) -> list[dict]:
    """Search TheirStack for job postings across 315K+ sources.

    Use cases:
    - Discover companies hiring for specific roles → BD opportunities
    - Match open jobs against internal candidate pool → proactive placement
    - Track existing client (HCL, TechM) postings → anticipate JDs
    - Competitive intel on other agencies scaling up

    Returns job postings with company data, NOT candidate profiles.
    Docs: https://theirstack.com/en/job-posting-api
    """
    api_key = os.environ.get("THEIRSTACK_API_KEY")
    if not api_key:
        log.warning("No THEIRSTACK_API_KEY set — skipping TheirStack search")
        return []

    # Map market to country codes AND TheirStack location IDs
    # Location IDs from TheirStack UI (more precise than country codes)
    country_map = {"IN": "IN", "SG": "SG", "india": "IN", "singapore": "SG"}
    location_id_map = {"SG": 1880251, "IN": 1269750}  # TheirStack internal IDs
    country = country_map.get(market, market)

    query_params = {
        "job_title_or": skills,
        "posted_at_max_age_days": days_ago,
        "limit": limit,
        "page": 0,
        "include_total_results": True,
        "blur_company_data": False,
        "order_by": [{"desc": True, "field": "date_posted"}],
    }

    # Location filter — prefer location IDs (matches TheirStack UI behaviour),
    # fall back to country code
    loc_id = location_id_map.get(country)
    if loc_id:
        query_params["job_location_or"] = [{"id": loc_id}]
    elif country:
        query_params["job_country_code_or"] = [country]
    if location:
        query_params["job_location_pattern_or"] = [location]

    # Optional: filter by specific company
    if company_name:
        query_params["company_name_pattern_or"] = [company_name]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.theirstack.com/v1/jobs/search",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=query_params,
            )

            if resp.status_code == 401:
                log.error("TheirStack API key invalid or expired")
                return []

            resp.raise_for_status()
            data = resp.json()

    except httpx.HTTPStatusError as e:
        log.error("TheirStack API error (HTTP %s): %s",
                  e.response.status_code, e.response.text[:300])
        return []
    except Exception as e:
        log.error("TheirStack API request failed: %s", e)
        return []

    results = []
    for job in data.get("data", []):
        results.append({
            "job_title": job.get("job_title", ""),
            "company": job.get("company_name", ""),
            "company_url": job.get("company_url", ""),
            "company_size": job.get("company_num_employees", ""),
            "location": job.get("job_location", ""),
            "salary_min": job.get("min_annual_salary"),
            "salary_max": job.get("max_annual_salary"),
            "salary_currency": job.get("salary_currency"),
            "skills_required": job.get("technologies", []),
            "date_posted": job.get("date_posted", ""),
            "job_url": job.get("url", ""),
            "ats_source": job.get("source", ""),
            "source": "theirstack",
        })

    log.info("TheirStack returned %d job postings", len(results))
    return results


# ── SerpApi / Google Jobs (1000+ boards, ~$0.001/req) ─────────

async def search_google_jobs(
    query: str,
    location: str | None = None,
    market: str = "IN",
    limit: int = 20,
) -> list[dict]:
    """Search Google Jobs via SerpApi — aggregates 1000+ job boards.

    One search for "ServiceNow Developer Bangalore" returns results
    from Indeed, LinkedIn, Glassdoor, Naukri, Foundit, company career
    pages, and more. Broadest coverage per API call.

    Docs: https://serpapi.com/google-jobs-api
    """
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        log.warning("No SERPAPI_API_KEY set — skipping Google Jobs search")
        return []

    # Build location string
    loc = location
    if not loc:
        loc = "Singapore" if market == "SG" else "India"

    params = {
        "engine": "google_jobs",
        "q": query,
        "location": loc,
        "api_key": api_key,
        "num": limit,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://serpapi.com/search",
                params=params,
            )

            if resp.status_code == 401:
                log.error("SerpApi API key invalid")
                return []

            resp.raise_for_status()
            data = resp.json()

    except httpx.HTTPStatusError as e:
        log.error("SerpApi error (HTTP %s): %s",
                  e.response.status_code, e.response.text[:300])
        return []
    except Exception as e:
        log.error("SerpApi request failed: %s", e)
        return []

    results = []
    for job in data.get("jobs_results", []):
        # Extract salary if present
        salary_info = job.get("detected_extensions", {})

        results.append({
            "job_title": job.get("title", ""),
            "company": job.get("company_name", ""),
            "location": job.get("location", ""),
            "description": job.get("description", "")[:500],
            "salary_info": salary_info.get("salary", ""),
            "job_type": salary_info.get("work_from_home", ""),
            "date_posted": salary_info.get("posted_at", ""),
            "schedule": salary_info.get("schedule_type", ""),
            "job_url": job.get("share_link", job.get("related_links", [{}])[0].get("link", "") if job.get("related_links") else ""),
            "via": job.get("via", ""),
            "source": "google_jobs",
        })

    log.info("Google Jobs (SerpApi) returned %d postings", len(results))
    return results


# ── Adzuna (job postings + salary benchmarks, free tier) ──────

# Adzuna country codes: https://developer.adzuna.com/overview
_ADZUNA_COUNTRY = {"IN": "in", "SG": "sg", "US": "us", "UK": "gb"}


async def search_adzuna_jobs(
    skills: list[str],
    location: str | None = None,
    market: str = "IN",
    limit: int = 20,
) -> list[dict]:
    """Search Adzuna for job postings with salary data.

    Docs: https://developer.adzuna.com/
    Free tier available. Returns structured salary ranges.
    """
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        log.warning("No ADZUNA_APP_ID/KEY set — skipping Adzuna search")
        return []

    country = _ADZUNA_COUNTRY.get(market, "in")
    query = " ".join(skills)

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": limit,
        "what": query,
        "sort_by": "relevance",
        "content-type": "application/json",
    }
    if location:
        params["where"] = location

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params=params,
            )

            if resp.status_code in (401, 403):
                log.error("Adzuna API credentials invalid")
                return []

            resp.raise_for_status()
            data = resp.json()

    except httpx.HTTPStatusError as e:
        log.error("Adzuna API error (HTTP %s): %s",
                  e.response.status_code, e.response.text[:300])
        return []
    except Exception as e:
        log.error("Adzuna API request failed: %s", e)
        return []

    results = []
    for job in data.get("results", []):
        results.append({
            "job_title": job.get("title", ""),
            "company": job.get("company", {}).get("display_name", ""),
            "location": job.get("location", {}).get("display_name", ""),
            "salary_min": job.get("salary_min"),
            "salary_max": job.get("salary_max"),
            "salary_is_predicted": job.get("salary_is_predicted"),
            "description": job.get("description", "")[:500],
            "date_posted": job.get("created", ""),
            "job_url": job.get("redirect_url", ""),
            "contract_type": job.get("contract_type", ""),
            "source": "adzuna",
        })

    log.info("Adzuna returned %d job postings", len(results))
    return results


async def get_adzuna_salary_benchmark(
    role_title: str,
    market: str = "IN",
    location: str | None = None,
) -> dict | None:
    """Get salary benchmark for a role from Adzuna's salary API.

    Returns: {median, percentile_25, percentile_75, sample_size}
    Feeds into the Screener agent for market-aware salary scoring.
    """
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        return None

    country = _ADZUNA_COUNTRY.get(market, "in")

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": role_title,
    }
    if location:
        params["where"] = location

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/history",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        # Adzuna returns monthly salary history
        # Extract latest data point for benchmarking
        months = data.get("month", {})
        if not months:
            return None

        # Get the most recent month's data
        latest_key = sorted(months.keys())[-1] if months else None
        if not latest_key:
            return None

        latest = months[latest_key]
        return {
            "role": role_title,
            "market": market,
            "location": location,
            "median_salary": latest,
            "data_month": latest_key,
            "source": "adzuna",
        }

    except Exception as e:
        log.warning("Adzuna salary lookup failed for '%s': %s", role_title, e)
        return None


# ── Unified market scan ───────────────────────────────────────

async def run_market_scan(
    skills: list[str],
    market: str = "IN",
    location: str | None = None,
    role_title: str | None = None,
) -> dict:
    """Run all market intelligence channels in parallel.

    Returns a dict with results from each source + salary benchmark.
    Used by the Market Intelligence Agent for weekly briefings.
    """
    tasks = []

    # TheirStack — always run if key available
    if os.environ.get("THEIRSTACK_API_KEY"):
        tasks.append(("theirstack", search_theirstack_jobs(
            skills, location, market)))

    # Google Jobs — always run if key available
    if os.environ.get("SERPAPI_API_KEY"):
        query = " ".join(skills)
        if location:
            query += f" {location}"
        tasks.append(("google_jobs", search_google_jobs(
            query, location, market)))

    # Adzuna — always run if credentials available
    if os.environ.get("ADZUNA_APP_ID"):
        tasks.append(("adzuna", search_adzuna_jobs(
            skills, location, market)))

    # MCF — SG only
    if market == "SG":
        tasks.append(("mycareersfuture", search_mcf_jobs(skills)))

    # Run in parallel
    results = {}
    gathered = await asyncio.gather(
        *[t[1] for t in tasks], return_exceptions=True)

    for (source_name, _), result in zip(tasks, gathered):
        if isinstance(result, Exception):
            log.error("Market intel channel %s failed: %s", source_name, result)
            results[source_name] = []
        else:
            results[source_name] = result

    # Salary benchmark (separate, fast)
    salary = None
    if role_title and os.environ.get("ADZUNA_APP_ID"):
        salary = await get_adzuna_salary_benchmark(role_title, market, location)

    results["salary_benchmark"] = salary
    results["total_jobs"] = sum(
        len(v) for v in results.values() if isinstance(v, list))

    return results
