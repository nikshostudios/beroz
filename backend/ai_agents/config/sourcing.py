"""Candidate sourcing pipeline — real candidate profiles only.

Channels (v2):
1. Foundit Recruiter API (direct, cookie auth) — primary
2. Apollo.io People Search API — passive candidates
3. Naukri Recruiter API (cookie auth) — India's #1 resume DB
4. Internal DB vector search — semantic matching via pgvector

Market intelligence channels (TheirStack, SerpApi/Google Jobs, Adzuna)
live in market_intelligence.py — they return job postings + salary data,
not candidate profiles.
"""

import asyncio
import logging
import os

import httpx

from .db import upsert_candidate_by_email, upsert_candidate_by_name

log = logging.getLogger(__name__)


# ── Apollo.io Professional API ─────────────────────────────────

def _normalize_apollo_people(people: list[dict],
                             match_skills: list[str],
                             market: str) -> list[dict]:
    """Map raw Apollo `people` payload to our candidate dict shape.

    `match_skills` is the list of skills the search was built from — we use
    them to opportunistically tag candidates whose title contains a known
    skill, so downstream skill-overlap filters and the screener have
    something to bite on.
    """
    results = []
    for person in people:
        person_skills = []
        if person.get("title"):
            person_skills.append(person["title"])
        title_lower = (person.get("title") or "").lower()
        for s in match_skills:
            if s and s.lower() in title_lower:
                person_skills.append(s)
        person_skills = list(dict.fromkeys(person_skills)) or list(match_skills)
        org = person.get("organization") or {}
        results.append({
            "name": person.get("name", ""),
            "email": person.get("email"),
            "linkedin_url": person.get("linkedin_url"),
            "current_job_title": person.get("title"),
            "current_employer": org.get("name"),
            "skills": person_skills,
            "source": "apollo",
            "market": market,
            # Underscore-prefixed: stripped by `run_all_sources` upsert path,
            # re-attached explicitly by `run_search` so the candidate row keeps
            # the Apollo ids needed for downstream /people/match reveals.
            "_apollo_person_id": person.get("id"),
            "_apollo_organization_id": org.get("id"),
        })
    return results


async def source_apollo(skills: list[str], location: str,
                        market: str) -> list[dict]:
    """Search Apollo.io People Search API for passive candidates.

    Raises RuntimeError with a descriptive message on non-200 responses so the
    caller can surface the reason instead of silently returning an empty list.
    """
    api_key = (os.environ.get("APOLLO_API_KEY")
               or os.environ.get("APOLLO_API"))
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY (or APOLLO_API) not set")
    region = "Singapore" if market == "SG" else "India"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.apollo.io/v1/mixed_people/api_search",
            headers={"X-Api-Key": api_key,
                     "Cache-Control": "no-cache",
                     "Content-Type": "application/json"},
            json={
                "q_keywords": " ".join(skills),
                "person_locations": [location or region],
                "per_page": 50,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"apollo HTTP {resp.status_code}: {resp.text[:300]}")

    return _normalize_apollo_people(resp.json().get("people", []), skills, market)


async def source_apollo_structured(params: dict, market: str) -> list[dict]:
    """Apollo search using pre-built structured params from the boolean_builder
    agent (instead of raw skills + location).

    `params` shape (all keys optional):
        {
          "q_keywords": "ServiceNow JavaScript ITSM",
          "person_titles": ["ServiceNow Developer"],
          "person_locations": ["Bangalore, India"],
          "person_seniorities": ["senior"]
        }
    """
    api_key = (os.environ.get("APOLLO_API_KEY")
               or os.environ.get("APOLLO_API"))
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY (or APOLLO_API) not set")

    default_loc = "Singapore" if market == "SG" else "India"
    body = {
        "q_keywords": params.get("q_keywords", ""),
        "per_page": 50,
    }
    if params.get("person_titles"):
        body["person_titles"] = list(params["person_titles"])[:4]
    body["person_locations"] = list(
        params.get("person_locations") or [default_loc]
    )
    if params.get("person_seniorities"):
        body["person_seniorities"] = list(params["person_seniorities"])

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.apollo.io/v1/mixed_people/api_search",
            headers={"X-Api-Key": api_key,
                     "Cache-Control": "no-cache",
                     "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"apollo HTTP {resp.status_code}: {resp.text[:300]}")

    resp_json = resp.json()
    people = resp_json.get("people", [])
    if not people:
        pagination = resp_json.get("pagination") or {}
        total = pagination.get("total_entries")
        top_keys = list(resp_json.keys())[:10]
        err_field = (resp_json.get("error")
                     or resp_json.get("message")
                     or resp_json.get("errors"))
        raise RuntimeError(
            f"apollo 200 OK but 0 people returned "
            f"(total_entries={total}, response_keys={top_keys}"
            + (f", error_field={err_field}" if err_field else "")
            + f", body_preview={resp.text[:300]})"
        )

    # Use person_titles as the match-skills hint so downstream tagging keeps
    # working; fall back to keywords split.
    hint = list(params.get("person_titles") or
                (params.get("q_keywords") or "").split())
    return _normalize_apollo_people(people, hint, market)


# ── Apollo per-row reveal + org enrichment ─────────────────────

async def apollo_people_match(apollo_person_id: str | None = None,
                              linkedin_url: str | None = None,
                              email: str | None = None,
                              reveal_phone_number: bool = False) -> dict:
    """Reveal a single Apollo contact via /v1/people/match.

    Pass at least one identifier (id > linkedin > email). When
    `reveal_phone_number` is True Apollo also returns the mobile number,
    consuming one phone credit on top of the email-reveal cost.
    """
    api_key = (os.environ.get("APOLLO_API_KEY")
               or os.environ.get("APOLLO_API"))
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY (or APOLLO_API) not set")
    if not (apollo_person_id or linkedin_url or email):
        raise RuntimeError("apollo_people_match needs id, linkedin, or email")

    body: dict = {"reveal_personal_emails": True}
    if apollo_person_id:
        body["id"] = apollo_person_id
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    if email:
        body["email"] = email
    if reveal_phone_number:
        body["reveal_phone_number"] = True

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.apollo.io/v1/people/match",
            headers={"X-Api-Key": api_key,
                     "Cache-Control": "no-cache",
                     "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code == 402:
            raise RuntimeError("apollo credits_exhausted")
        if resp.status_code != 200:
            raise RuntimeError(
                f"apollo HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json() or {}
    return data.get("person") or data


async def apollo_organizations_enrich(
        apollo_organization_id: str | None = None,
        domain: str | None = None) -> dict:
    """Fetch full company profile via /v1/organizations/enrich.

    Either an Apollo org id or a website domain is required. Returns the
    `organization` payload (industries, founded year, revenue, employees,
    technologies, etc.).
    """
    api_key = (os.environ.get("APOLLO_API_KEY")
               or os.environ.get("APOLLO_API"))
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY (or APOLLO_API) not set")
    if not (apollo_organization_id or domain):
        raise RuntimeError(
            "apollo_organizations_enrich needs id or domain")

    params: dict = {}
    if apollo_organization_id:
        params["id"] = apollo_organization_id
    if domain:
        params["domain"] = domain

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.apollo.io/v1/organizations/enrich",
            headers={"X-Api-Key": api_key,
                     "Cache-Control": "no-cache",
                     "Accept": "application/json"},
            params=params,
        )
        if resp.status_code == 402:
            raise RuntimeError("apollo credits_exhausted")
        if resp.status_code != 200:
            raise RuntimeError(
                f"apollo HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json() or {}
    return data.get("organization") or data


async def apollo_account_credits() -> dict:
    """Return current Apollo credit counters via /v1/auth/health.

    Apollo's `/v1/auth/health` returns a dict that contains the seat's
    `email_credits_per_month`, `email_credits_used_this_month`, and the same
    pair for phone/export credits. We surface the *remaining* values so the
    UI can render a single number.
    """
    api_key = (os.environ.get("APOLLO_API_KEY")
               or os.environ.get("APOLLO_API"))
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY (or APOLLO_API) not set")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.apollo.io/v1/auth/health",
            headers={"X-Api-Key": api_key,
                     "Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"apollo HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json() or {}


# ── Naukri Recruiter API (cookie auth, India's #1 resume DB) ──

NAUKRI_RESDEX_DOMAIN = os.environ.get(
    "NAUKRI_RESDEX_DOMAIN", "resdex.naukri.com")


async def source_naukri_with_cookie(
    skills: list[str], experience_min: str | None,
    location: str | None, session_cookie: str | None = None,
) -> list[dict]:
    """Search Naukri Resdex (resume database) via their internal API.

    Same approach as Foundit: call the endpoint the SPA frontend uses,
    pass the session cookie from a manual browser login.

    Cookie refresh:
    1. Log into resdex.naukri.com manually in Chrome
    2. DevTools > Network > Fetch/XHR > run a search > copy Cookie header
    3. Set NAUKRI_SESSION_COOKIE env var
    4. Cookie expires in ~24-48 hours
    """
    cookie = session_cookie or os.environ.get("NAUKRI_SESSION_COOKIE")
    if not cookie:
        log.warning("No NAUKRI_SESSION_COOKIE set — skipping Naukri search")
        return []

    query = " ".join(skills)
    search_location = location or "India"

    # Naukri Resdex internal search API (captured from DevTools)
    api_url = (f"https://{NAUKRI_RESDEX_DOMAIN}"
               "/v0/resdex-search/search")

    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "cookie": cookie,
        "origin": f"https://{NAUKRI_RESDEX_DOMAIN}",
        "referer": f"https://{NAUKRI_RESDEX_DOMAIN}/search",
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/146.0.0.0 Safari/537.36"),
        "appid": "205",
        "systemid": "Starter",
    }

    body = {
        "keyword": query,
        "locations": [search_location],
        "pageNo": 1,
        "noOfResults": 50,
        "searchType": "RELEVANCE",
        "sort": "Relevance",
        "includeUnfilled": True,
    }

    # Add experience filter
    if experience_min:
        body["experienceFrom"] = int(experience_min)
        body["experienceTo"] = 30

    all_candidates = []

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(api_url, headers=headers, json=body)

            if resp.status_code in (401, 403):
                log.error(
                    "Naukri cookie expired — got HTTP %s. "
                    "Refresh manually: log into resdex.naukri.com, "
                    "copy Cookie from DevTools Network tab, update "
                    "NAUKRI_SESSION_COOKIE env var.",
                    resp.status_code,
                )
                return []

            resp.raise_for_status()
            data = resp.json()
            all_candidates = _parse_naukri_api_response(data)

    except httpx.HTTPStatusError as e:
        log.error("Naukri API error (HTTP %s): %s",
                  e.response.status_code, e.response.text[:300])
        return []
    except Exception as e:
        log.error("Naukri API request failed: %s", e)
        return []

    log.info("Naukri Resdex API returned %d candidates", len(all_candidates))
    return all_candidates


def _parse_naukri_api_response(data: dict) -> list[dict]:
    """Parse Naukri Resdex API response into candidate dicts.

    Response structure (from DevTools inspection):
        data.searchResults[] — each result has profileId, name,
        currentDesignation, currentCompany, totalExperience,
        currentLocation, keySkills, email, contactNumber.

    NOTE: The exact field names may vary. Update this parser after
    confirming the real API response shape via DevTools.
    """
    results = []
    search_results = data.get("searchResults", data.get("results", []))

    for c in search_results:
        name = c.get("name", c.get("fullName", ""))
        if not name or name == "N/A":
            continue

        # Skills: may be string (comma-separated) or list
        skills_raw = c.get("keySkills", c.get("skills", []))
        if isinstance(skills_raw, str):
            skills_raw = [s.strip() for s in skills_raw.split(",") if s.strip()]
        elif isinstance(skills_raw, list):
            skills_raw = [
                s.get("text", s) if isinstance(s, dict) else str(s)
                for s in skills_raw
            ]

        # Experience
        exp = c.get("totalExperience", c.get("experience", ""))
        if isinstance(exp, (int, float)):
            years = int(exp)
            months = round((exp - years) * 12)
            exp_str = f"{years} years"
            if months:
                exp_str += f" {months} months"
        else:
            exp_str = str(exp) if exp else ""

        # Location
        loc = c.get("currentLocation", c.get("location", ""))
        if isinstance(loc, list):
            loc = ", ".join(str(l) for l in loc)

        # Email
        email = c.get("email", c.get("emailId"))

        # Phone
        phone = c.get("contactNumber", c.get("mobile"))

        results.append({
            "name": name,
            "email": email,
            "phone": phone,
            "current_job_title": c.get("currentDesignation", ""),
            "current_employer": c.get("currentCompany", ""),
            "total_experience": exp_str,
            "current_location": loc,
            "skills": skills_raw,
            "source": "naukri",
            "market": "IN",
        })

    return results


# ── Run all sources in parallel ────────────────────────────────

async def run_all_sources(requirement: dict) -> dict:
    """Run applicable sourcing channels in parallel, deduplicate, upsert.

    Channel priority (v2):
    1. Internal DB (vector search) — $0, fastest, always first
    2. Foundit recruiter API — cookie auth, both markets
    3. Naukri Resdex API — cookie auth, India only
    4. Apollo — passive candidates, both markets
    """
    market = requirement.get("market", "IN")
    skills = requirement.get("skills_required", [])
    exp_min = requirement.get("experience_min")
    location = requirement.get("location")

    tasks = []

    # Foundit recruiter search (cookie auth) — primary channel, both markets
    foundit_cookie = os.environ.get("FOUNDIT_SESSION_COOKIE")
    if foundit_cookie:
        tasks.append(("foundit_recruiter", source_foundit_with_cookie(
            skills, exp_min, location, foundit_cookie, market)))

    # Naukri Resdex (cookie auth) — India market only
    naukri_cookie = os.environ.get("NAUKRI_SESSION_COOKIE")
    if naukri_cookie and market == "IN":
        tasks.append(("naukri", source_naukri_with_cookie(
            skills, exp_min, location, naukri_cookie)))

    # Apollo — both markets (skipped if no API key under either alias)
    if os.environ.get("APOLLO_API_KEY") or os.environ.get("APOLLO_API"):
        tasks.append(("apollo", source_apollo(skills, location, market)))

    # Run in parallel
    results_by_source: dict[str, int] = {}
    channel_errors: dict[str, str] = {}
    if not tasks:
        return {
            "total_unique": 0,
            "upserted_candidates": [],
            "channel_errors": {"_no_channels":
                "No sourcing channels configured — set APOLLO_API_KEY, "
                "FOUNDIT_SESSION_COOKIE, and/or NAUKRI_SESSION_COOKIE."},
        }
    gathered = await asyncio.gather(
        *[t[1] for t in tasks], return_exceptions=True)

    saved_emails = set()
    upserted_candidates = []
    for (source_name, _), result in zip(tasks, gathered):
        if isinstance(result, Exception):
            err_msg = f"{type(result).__name__}: {result}"
            log.error("Sourcing channel %s failed: %s", source_name, err_msg)
            results_by_source[source_name] = 0
            channel_errors[source_name] = err_msg
            continue

        # Deduplicate and upsert — only count candidates actually saved to DB
        _skip_fields = {"source_scraper", "last_active", "cv_id",
                        "skills_snippet", "company", "posted"}
        saved_count = 0
        for candidate in result:
            clean = {k: v for k, v in candidate.items()
                     if not k.startswith("_") and k not in _skip_fields}
            email = candidate.get("email")
            if email and email not in saved_emails:
                row = upsert_candidate_by_email(clean)
                saved_emails.add(email)
                saved_count += 1
                upserted_candidates.append(row)
            elif candidate.get("name"):
                row = upsert_candidate_by_name(clean)
                if row:
                    saved_count += 1
                    upserted_candidates.append(row)
        results_by_source[source_name] = saved_count
        log.info("Sourcing %s: %d returned, %d saved to DB",
                 source_name, len(result), saved_count)

    # total_unique was previously len(saved_emails), which ignored name-only
    # upserts (Apollo often doesn't expose email, so every Apollo candidate
    # was invisible to this counter). Fixed: use actual upsert count.
    return {
        **results_by_source,
        "total_unique": len(upserted_candidates),
        "upserted_candidates": upserted_candidates,
        "channel_errors": channel_errors,
    }


# ── Foundit recruiter search (direct API with session cookie) ──

# Recruiter portal domain — ExcelTech subscription is on .sg
FOUNDIT_RECRUITER_DOMAIN = os.environ.get(
    "FOUNDIT_RECRUITER_DOMAIN", "recruiter.foundit.sg")

# ExcelTech company identifiers on Foundit (from recruiter portal)
_FOUNDIT_CORP_ID = int(os.environ.get("FOUNDIT_CORP_ID", "560219"))
_FOUNDIT_SUBUID = int(os.environ.get("FOUNDIT_SUBUID", "1347319"))
_FOUNDIT_CORP_NAME = os.environ.get(
    "FOUNDIT_CORP_NAME", "ExcelTech Computers Pte Ltd")

# Site context per market
_FOUNDIT_SITE_CONTEXT = {
    "SG": "monstersingapore",
    "IN": "monsterindia",
}


async def source_foundit_with_cookie(
    skills: list[str], experience_min: str | None,
    location: str | None, session_cookie: str | None = None,
    market: str = "SG",
) -> list[dict]:
    """Search Foundit recruiter candidate database via their internal API.

    Calls the same JSON endpoint that the recruiter portal frontend uses.
    No Firecrawl needed — just a direct HTTP POST with the session cookie.

    Cookie refresh:
    1. Log into recruiter.foundit.sg manually in Chrome
    2. DevTools > Network > Fetch/XHR > click any request
    3. Copy the full Cookie header value from Request Headers
    4. Set FOUNDIT_SESSION_COOKIE env var
    5. Cookie expires in ~24-48 hours
    """
    cookie = session_cookie or os.environ.get("FOUNDIT_SESSION_COOKIE")
    if not cookie:
        log.warning("No FOUNDIT_SESSION_COOKIE set — skipping recruiter search")
        return []

    query = " ".join(skills)
    site_context = _FOUNDIT_SITE_CONTEXT.get(market, "monstersingapore")
    # The portal always sends INR even for Singapore searches — matching real API
    currency = "INR"
    search_location = location or ("Singapore" if market == "SG" else "India")

    api_url = (f"https://{FOUNDIT_RECRUITER_DOMAIN}"
               "/edge/recruiter-search/api/search-middleware/v2/search")

    headers = {
        "content-type": "application/json",
        "accept": "*/*",
        "cookie": cookie,
        "domain": f"https://{FOUNDIT_RECRUITER_DOMAIN}",
        "origin": f"https://{FOUNDIT_RECRUITER_DOMAIN}",
        "referer": f"https://{FOUNDIT_RECRUITER_DOMAIN}/edge/recruiter-search/search",
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/146.0.0.0 Safari/537.36"),
    }

    body = {
        "appName": "recSRP",
        "reqParam": {
            "corp_company_name": _FOUNDIT_CORP_NAME,
            "subuid": _FOUNDIT_SUBUID,
            "corp_id": _FOUNDIT_CORP_ID,
            "site_context": site_context,
            "recruiter_company_name": _FOUNDIT_CORP_NAME,
            "recruiter_db_access_contexts": [site_context],
            "session_cid": "4",
            "session_scid": "6",
            "channel_id": 4,
            "sub_channel_id": 6,
            "email": "",
            "logo": "",
            "is_new_search_request": True,
            "queries": {
                "all": query,
                "entities": {"DEFAULT": [query.lower()]},
                "synonyms": None,
                "exclude_synonyms": 0,
                "search_within": "contents",
                "search_scope_id": 1,
                "combined": [{"name": query, "type": "all"}],
                "derived_entities": {},
            },
            "service_filter": {
                # Real API sends location twice (city + country, both "Singapore")
                "location": [search_location, search_location],
            },
            "filters": {
                "company": {
                    "currency": currency,
                    "include_profiles_with_no_ctc": 1,
                    "include_profiles_with_no_notice_period": 1,
                    "serving_notice_period": 0,
                    "is_preferred_designations": False,
                },
                "additional": {
                    "show_active_created": "active",
                    "active_created_days": 180,
                    "age_include_profiles_without_age": True,
                },
                "show_only_contactable_profiles": False,
            },
            "refine_search": {"sort_by": "relevance"},
            "size": 40,
            "from": 0,
            "express_resumes": {"from": 0, "size": 4},
            "api_profile_flag": 1,
            "strict": True,
            "is_corp_based_search": False,
            "is_v2_request": True,
            "use_synonyms_fields": True,
            "sub_source": "search",
            "search_source": "New Search",
        },
    }

    # Add experience filter if specified
    if experience_min:
        body["reqParam"]["service_filter"]["experience"] = {
            "min": int(experience_min), "max": 30,
        }

    all_candidates = []

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Page 1
            resp = await client.post(api_url, headers=headers, json=body)

            if resp.status_code in (401, 403):
                log.error(
                    "Foundit cookie expired — got HTTP %s. "
                    "Refresh manually: log into recruiter.foundit.sg in Chrome, "
                    "copy Cookie from DevTools Network tab, update "
                    "FOUNDIT_SESSION_COOKIE env var.",
                    resp.status_code,
                )
                return []

            resp.raise_for_status()
            data = resp.json()
            page1 = _parse_foundit_api_response(data, market)
            all_candidates.extend(page1)

            # Page 2 if page 1 was full (API has no totalCount field)
            if len(page1) == 40:
                body["reqParam"]["from"] = 40
                body["reqParam"]["express_resumes"]["from"] = 4
                body["reqParam"]["is_new_search_request"] = False
                resp2 = await client.post(api_url, headers=headers, json=body)
                if resp2.status_code == 200:
                    page2 = _parse_foundit_api_response(resp2.json(), market)
                    all_candidates.extend(page2)

    except httpx.HTTPStatusError as e:
        log.error("Foundit API error (HTTP %s): %s",
                  e.response.status_code, e.response.text[:300])
        return []
    except Exception as e:
        log.error("Foundit API request failed: %s", e)
        return []

    log.info("Foundit recruiter API returned %d candidates", len(all_candidates))
    return all_candidates


def _parse_foundit_api_response(data: dict, market: str) -> list[dict]:
    """Parse the Foundit recruiter search API JSON response into candidate dicts.

    Response structure (actual API):
        data.response.resumes[] — each resume has nested objects for
        current_employment, current_location, skills, email, mobile_details.
    """
    results = []
    response = data.get("response", data)
    resumes = response.get("resumes", [])

    for c in resumes:
        name = c.get("name", "")
        if not name or name == "N/A":
            continue

        # Skills: list of {"text": "ServiceNow", "source": "core"}
        skills_raw = []
        for s in c.get("skills", []):
            if isinstance(s, dict):
                skills_raw.append(s.get("text", ""))
            elif isinstance(s, str):
                skills_raw.append(s)
        skills_raw = [s for s in skills_raw if s]

        # Experience: float like 4.09 (years)
        exp = c.get("experience")
        if isinstance(exp, (int, float)):
            years = int(exp)
            months = round((exp - years) * 12)
            exp_str = f"{years} years"
            if months:
                exp_str += f" {months} months"
        else:
            exp_str = str(exp) if exp else ""

        # Location: {"text": "Hyderabad, Telangana", "source": "core"}
        loc = c.get("current_location", "")
        if isinstance(loc, dict):
            loc = loc.get("text", "")
        elif isinstance(loc, list):
            loc = ", ".join(str(l) for l in loc)

        # Current employment: nested object
        emp = c.get("current_employment") or {}
        designation = emp.get("designation", {})
        if isinstance(designation, dict):
            job_title = designation.get("text", "")
        else:
            job_title = str(designation) if designation else ""
        employer_obj = emp.get("employer", {})
        if isinstance(employer_obj, dict):
            employer = employer_obj.get("text", "")
        else:
            employer = str(employer_obj) if employer_obj else ""

        # Email: list of {"id": "user@example.com", ...}
        email = None
        email_list = c.get("email", [])
        if isinstance(email_list, list) and email_list:
            email = email_list[0].get("id") if isinstance(email_list[0], dict) else None
        elif isinstance(email_list, str):
            email = email_list

        # Phone: mobile_details[0].number
        phone = None
        mobile = c.get("mobile_details", [])
        if isinstance(mobile, list) and mobile:
            phone = mobile[0].get("number") if isinstance(mobile[0], dict) else None

        results.append({
            "name": name,
            "email": email,
            "phone": phone,
            "current_job_title": job_title or c.get("curr_desig_cat"),
            "current_employer": employer,
            "total_experience": exp_str,
            "current_location": loc,
            "skills": skills_raw,
            "source": "foundit",
            "market": market,
        })

    return results


# ── LinkedIn search string (manual use only) ──────────────────

def generate_linkedin_search_string(requirement: dict) -> str:
    """Build boolean search string for recruiter to paste into LinkedIn."""
    skills = requirement.get("skills_required", [])
    location = requirement.get("location", "")
    market = requirement.get("market", "IN")

    # Skills OR group
    skills_part = " OR ".join(f'"{s}"' for s in skills)

    # Location
    loc = location or ("Singapore" if market == "SG" else "India")

    # Experience hint
    exp_min = requirement.get("experience_min", "")
    exp_part = ""
    if exp_min:
        exp_part = f' AND ("{exp_min}+ years" OR "senior" OR "lead")'

    return f"({skills_part}) AND \"{loc}\"{exp_part}"
