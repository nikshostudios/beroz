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
import json
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
            # Apollo pre-reveal signals: returned by /mixed_people/api_search
            # without spending a credit. has_direct_phone is "Yes"/"No"/"Maybe...",
            # has_email and has_country are bools, last_refreshed_at is ISO ts.
            "first_name": person.get("first_name"),
            "last_name_obfuscated": person.get("last_name_obfuscated"),
            "has_email": person.get("has_email"),
            "has_direct_phone": person.get("has_direct_phone"),
            "has_country": person.get("has_country"),
            "apollo_last_refreshed_at": person.get("last_refreshed_at"),
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


def _build_apollo_search_body(params: dict, market: str) -> dict:
    default_loc = "Singapore" if market == "SG" else "India"
    body: dict = {"per_page": 50}
    # Apollo's api_search treats q_keywords as an AND full-text match (not
    # fuzzy, despite what the UI suggests). Anything beyond ~2 tokens rapidly
    # drops total_entries to 0. Cap to 2 tokens, and only include when we
    # don't already have person_titles (which is more precise).
    q_raw = (params.get("q_keywords") or "").strip()
    if q_raw and not params.get("person_titles"):
        body["q_keywords"] = " ".join(q_raw.split()[:2])
    if params.get("person_titles"):
        body["person_titles"] = list(params["person_titles"])[:4]
    body["person_locations"] = list(
        params.get("person_locations") or [default_loc]
    )
    if params.get("person_seniorities"):
        body["person_seniorities"] = list(params["person_seniorities"])
    return body


async def _apollo_search_raw(body: dict) -> dict:
    """Low-level POST to /v1/mixed_people/api_search.

    Returns the parsed response payload (including `pagination.total_entries`
    and `people`) on 200 OK. Does NOT raise on `people=[]` — adaptive callers
    need to inspect `total_entries` to decide whether to retry. Raises
    RuntimeError only on non-200 responses or missing API key.
    """
    api_key = (os.environ.get("APOLLO_API_KEY")
               or os.environ.get("APOLLO_API"))
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY (or APOLLO_API) not set")
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
        return resp.json() or {}


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

    Raises RuntimeError on `people=[]` for back-compat with `run_search`.
    The agentic-boost pipeline uses `source_apollo_structured_adaptive`.
    """
    body = _build_apollo_search_body(params, market)
    resp_json = await _apollo_search_raw(body)
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
            + ")"
        )
    # Use person_titles as the match-skills hint so downstream tagging keeps
    # working; fall back to keywords split.
    hint = list(params.get("person_titles") or
                (params.get("q_keywords") or "").split())
    return _normalize_apollo_people(people, hint, market)


async def source_apollo_structured_adaptive(
    params: dict, market: str, min_total: int = 50,
) -> tuple[list[dict], list[dict]]:
    """Try `params`; if `total_entries < min_total`, loosen progressively.

    Returns `(candidates, iteration_log)`. The iteration log is a list of
    dicts: `[{step, dropped, total_entries, returned}, ...]`. Each step
    drops ONE constraint at a time and retries:

        1. Original params.
        2. Drop `q_keywords`.
        3. Drop `person_seniorities`.
        4. Trim `person_titles` to its first single entry.
        5. Drop `person_titles` entirely (titles-only fallback).

    Stops at the first step where `total_entries >= min_total`. If every
    step yields below `min_total`, returns whichever step produced the most
    candidates and tags the final log entry with `degraded=True`.
    """
    base = dict(params or {})
    steps: list[tuple[str | None, dict]] = [(None, dict(base))]
    if base.get("q_keywords"):
        s = dict(steps[-1][1])
        s.pop("q_keywords", None)
        steps.append(("q_keywords", s))
    if base.get("person_seniorities"):
        s = dict(steps[-1][1])
        s.pop("person_seniorities", None)
        steps.append(("person_seniorities", s))
    titles = list(base.get("person_titles") or [])
    if len(titles) > 1:
        s = dict(steps[-1][1])
        s["person_titles"] = titles[:1]
        steps.append(("person_titles[1:]", s))
    if titles:
        s = dict(steps[-1][1])
        s.pop("person_titles", None)
        steps.append(("person_titles", s))

    log_entries: list[dict] = []
    best: tuple[int, list[dict], dict] | None = None
    for idx, (dropped, step_params) in enumerate(steps, start=1):
        body = _build_apollo_search_body(step_params, market)
        try:
            resp_json = await _apollo_search_raw(body)
        except RuntimeError as exc:
            log.warning("apollo_adaptive: step=%d dropped=%s raised %s",
                        idx, dropped, exc)
            log_entries.append({
                "step": idx, "dropped": dropped,
                "total_entries": None, "returned": 0,
                "error": str(exc)[:300],
            })
            continue
        people = resp_json.get("people", []) or []
        pagination = resp_json.get("pagination") or {}
        total = pagination.get("total_entries") or 0
        hint = list(step_params.get("person_titles") or
                    (step_params.get("q_keywords") or "").split())
        candidates = _normalize_apollo_people(people, hint, market)
        entry = {
            "step": idx,
            "dropped": dropped,
            "total_entries": total,
            "returned": len(candidates),
        }
        log.info("apollo_adaptive: step=%d dropped=%s total_entries=%d returned=%d",
                 idx, dropped, total, len(candidates))
        log_entries.append(entry)
        if total >= min_total and candidates:
            return candidates, log_entries
        if best is None or len(candidates) > best[0]:
            best = (len(candidates), candidates, entry)

    if best and best[1]:
        best[2]["degraded"] = True
        return best[1], log_entries
    return [], log_entries


# ── Apollo per-row reveal + org enrichment ─────────────────────

async def apollo_people_match(apollo_person_id: str | None = None,
                              linkedin_url: str | None = None,
                              email: str | None = None,
                              reveal_phone_number: bool = False,
                              webhook_url: str | None = None) -> dict:
    """Reveal a single Apollo contact via /v1/people/match.

    Pass at least one identifier (id > linkedin > email). When
    `reveal_phone_number` is True Apollo returns the mobile number
    asynchronously to `webhook_url` (required when reveal_phone_number=True),
    consuming one phone credit on top of the email-reveal cost.
    """
    api_key = (os.environ.get("APOLLO_API_KEY")
               or os.environ.get("APOLLO_API"))
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY (or APOLLO_API) not set")
    if not (apollo_person_id or linkedin_url or email):
        raise RuntimeError("apollo_people_match needs id, linkedin, or email")
    if reveal_phone_number and not webhook_url:
        raise RuntimeError("apollo_people_match: webhook_url required when reveal_phone_number=True")

    body: dict = {"reveal_personal_emails": True}
    if apollo_person_id:
        body["id"] = apollo_person_id
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    if email:
        body["email"] = email
    if reveal_phone_number:
        body["reveal_phone_number"] = True
        body["webhook_url"] = webhook_url

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


# ── GitHub Users API (free, global, engineers) ─────────────────

GITHUB_API_BASE = "https://api.github.com"


def _normalize_github_users(users: list[dict],
                            match_skills: list[str],
                            market: str) -> list[dict]:
    """Map GitHub `/users/{login}` payloads to candidate dict shape.

    GitHub returns `bio`, `location`, `company`, `email` (often null), `blog`,
    plus `login` and `html_url`. We synthesize a job title from `bio` when
    present, fall back to "@{login}" so the row has a name even when the user
    hasn't set their public display name.
    """
    results = []
    for u in users:
        login = u.get("login") or ""
        name = u.get("name") or (f"@{login}" if login else "")
        if not name:
            continue
        company = (u.get("company") or "").lstrip("@") or None
        bio = u.get("bio") or ""
        # Tag with any match_skill mentioned in bio so the screener has signal.
        bio_lower = bio.lower()
        skills = [s for s in match_skills if s and s.lower() in bio_lower]
        if not skills:
            skills = list(match_skills)
        html_url = u.get("html_url") or (
            f"https://github.com/{login}" if login else None)
        results.append({
            "name": name,
            "email": u.get("email"),
            "current_employer": company,
            "current_job_title": bio[:200] if bio else None,
            "current_location": u.get("location"),
            "skills": skills,
            "source": "github",
            "market": market,
            # Real DB columns — survive the run_all_sources strip and the
            # agentic-boost upsert without any per-source rewiring.
            "github_url": html_url,
            "source_profile_url": html_url,
            "source_metadata": {
                "github_login": login,
                "followers": u.get("followers"),
                "public_repos": u.get("public_repos"),
                "hireable": u.get("hireable"),
                "blog": u.get("blog") or None,
            },
        })
    return results


async def source_github(skills: list[str], location: str | None,
                        market: str) -> list[dict]:
    """Search GitHub for engineers via the public Users Search API.

    Free with `GITHUB_TOKEN`: 30 search req/min, 5,000 REST req/hr. Builds a
    query like `language:Python location:Berlin followers:>10` from the
    requirement skills + location, then enriches the top 30 hits with one
    /users/{login} call each.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set")

    # GitHub user search supports `language:`, `location:`, `followers:>N`.
    # Pick the first skill that looks like a programming language; treat the
    # rest as keywords. Cheap heuristic — anything <= 12 chars + no space.
    lang = next((s for s in skills if s and len(s) <= 12 and " " not in s),
                None)
    qualifiers = ["type:user"]
    if lang:
        qualifiers.append(f"language:{lang}")
    if location:
        loc = f'"{location}"' if " " in location else location
        qualifiers.append(f"location:{loc}")
    qualifiers.append("followers:>10")
    q = " ".join(qualifiers)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GITHUB_API_BASE}/search/users",
            headers=headers,
            params={"q": q, "per_page": 30, "sort": "followers"},
        )
        if resp.status_code == 422:
            raise RuntimeError(
                f"github search 422: {resp.text[:200]} (q={q})")
        if resp.status_code != 200:
            raise RuntimeError(
                f"github HTTP {resp.status_code}: {resp.text[:300]}")
        items = (resp.json() or {}).get("items", [])

        # Enrich with full profile (bio/location/email/company aren't in
        # search results). Bounded concurrency to stay under secondary
        # rate limits.
        sem = asyncio.Semaphore(5)

        async def _fetch_user(login: str) -> dict | None:
            async with sem:
                try:
                    r = await client.get(
                        f"{GITHUB_API_BASE}/users/{login}", headers=headers)
                    if r.status_code == 200:
                        return r.json()
                except Exception:
                    log.warning("github /users/%s failed", login,
                                exc_info=True)
                return None

        full = await asyncio.gather(
            *[_fetch_user(it.get("login")) for it in items if it.get("login")])
        users = [u for u in full if u]

    return _normalize_github_users(users, skills, market)


# ── Hugging Face API (free, ML/AI engineers) ───────────────────

HF_API_BASE = "https://huggingface.co/api"


def _normalize_hf_users(users: list[dict],
                        match_skills: list[str],
                        market: str) -> list[dict]:
    """Map Hugging Face `/users/{username}/overview` payloads to candidate dicts.

    HF doesn't expose location/email publicly. We rely on `fullname`, `details`
    (bio), and `orgs` for the basic profile, and stuff the model/dataset/like
    counts into `source_metadata` for downstream filtering.
    """
    results = []
    for u in users:
        username = u.get("user") or u.get("username") or ""
        if not username:
            continue
        fullname = u.get("fullname") or f"@{username}"
        bio = (u.get("details") or "").strip()
        num_models = u.get("numModels") or 0
        num_datasets = u.get("numDatasets") or 0
        num_likes = u.get("numLikes") or 0
        orgs_raw = u.get("orgs") or []
        orgs = []
        for o in orgs_raw:
            if isinstance(o, dict):
                orgs.append({
                    "name": o.get("fullname") or o.get("name") or o.get("user"),
                    "type": o.get("type"),
                })
        first_org = next((o.get("name") for o in orgs if o.get("name")), None)
        if bio:
            title = bio[:200]
        else:
            title = f"ML engineer ({num_models} models on Hugging Face)"
        # Tag any match_skill mentioned in the bio so the screener has signal.
        bio_lower = bio.lower()
        skills = [s for s in match_skills if s and s.lower() in bio_lower]
        if not skills:
            skills = list(match_skills)
        profile_url = f"https://huggingface.co/{username}"
        results.append({
            "name": fullname,
            "email": None,
            "current_employer": first_org,
            "current_job_title": title,
            "current_location": None,
            "skills": skills,
            "source": "huggingface",
            "market": market,
            "source_profile_url": profile_url,
            "source_metadata": {
                "hf_username": username,
                "num_models": num_models,
                "num_datasets": num_datasets,
                "num_likes": num_likes,
                "orgs": orgs,
            },
        })
    return results


async def source_huggingface(skills: list[str], market: str) -> list[dict]:
    """Search Hugging Face model authors via the public API.

    Flow: search models by each skill (treated as tag/keyword), dedupe authors
    across results, then enrich the top 30 with /users/{username}/overview.

    `HF_TOKEN` is optional — only used to bump rate limits. Gating happens at
    the caller via `HF_ENABLED`.
    """
    if not skills:
        raise RuntimeError("huggingface needs at least one skill to query")

    token = os.environ.get("HF_TOKEN")
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Pick up to 3 ML-leaning skills to drive the model search. Cap to avoid
    # blowing through the rate limit on a 10-skill JD.
    queries = [s for s in skills if s][:3]
    if not queries:
        return []

    seen_authors: set[str] = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for q in queries:
            try:
                resp = await client.get(
                    f"{HF_API_BASE}/models",
                    headers=headers,
                    params={"search": q, "limit": 50},
                )
                if resp.status_code != 200:
                    log.warning("hf models search %r HTTP %s: %s",
                                q, resp.status_code, resp.text[:200])
                    continue
                for model in resp.json() or []:
                    author = model.get("author")
                    if author and author not in seen_authors:
                        seen_authors.add(author)
                        if len(seen_authors) >= 60:
                            break
            except Exception:
                log.warning("hf models search failed for %r", q,
                            exc_info=True)
            if len(seen_authors) >= 60:
                break

        # Cap enrichment at 30 authors per requirement.
        author_list = list(seen_authors)[:30]
        sem = asyncio.Semaphore(5)

        async def _fetch_user(username: str) -> dict | None:
            async with sem:
                try:
                    r = await client.get(
                        f"{HF_API_BASE}/users/{username}/overview",
                        headers=headers,
                    )
                    if r.status_code == 200:
                        data = r.json() or {}
                        data.setdefault("user", username)
                        return data
                except Exception:
                    log.warning("hf /users/%s/overview failed", username,
                                exc_info=True)
                return None

        full = await asyncio.gather(
            *[_fetch_user(a) for a in author_list])
        users = [u for u in full if u]

    return _normalize_hf_users(users, skills, market)


# ── Apify multi-actor (LinkedIn People Search + YC directory) ─

APIFY_API_BASE = "https://api.apify.com/v2"
DEFAULT_APIFY_LINKEDIN_ACTOR = "curious_coder/linkedin-search-scraper"
DEFAULT_APIFY_YC_ACTOR = "michael.g/y-conductor-scraper"

_YC_ROLE_TRIGGERS = (
    "founder", "founding", "early-stage", "early stage",
    "startup", "first hire",
)


async def _apify_run_actor(actor_id: str, body: dict,
                           token: str, timeout_sec: int = 60) -> list[dict]:
    """Sync-run an Apify actor and return the dataset items inline.

    Wraps `POST /v2/acts/{actor}/run-sync-get-dataset-items`. Returns [] on
    any non-2xx so a single failing actor doesn't blow up the whole source.
    """
    url = (f"{APIFY_API_BASE}/acts/{actor_id.replace('/', '~')}"
           f"/run-sync-get-dataset-items")
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        resp = await client.post(url, params={"token": token}, json=body)
        if resp.status_code not in (200, 201):
            log.warning("apify actor %s HTTP %s: %s",
                        actor_id, resp.status_code, resp.text[:300])
            return []
        try:
            data = resp.json() or []
        except Exception:
            log.warning("apify actor %s returned non-JSON body", actor_id)
            return []
        if isinstance(data, dict):
            data = data.get("items") or []
        return data if isinstance(data, list) else []


def _normalize_apify_linkedin(items: list[dict],
                              match_skills: list[str],
                              market: str) -> list[dict]:
    """Map Apify LinkedIn People Search items to candidate dict shape.

    The actor returns name/headline/location/profile URL/current company
    (field names vary by provider, so we accept several aliases).
    """
    results = []
    for it in items:
        name = (it.get("name") or it.get("fullName")
                or it.get("title") or "")
        if not name:
            continue
        headline = (it.get("headline") or it.get("jobTitle")
                    or it.get("subtitle") or "")
        company = (it.get("companyName") or it.get("company")
                   or it.get("currentCompany") or None)
        if isinstance(company, dict):
            company = company.get("name") or company.get("title")
        location = (it.get("location") or it.get("locationName")
                    or it.get("city") or None)
        profile_url = (it.get("profileUrl") or it.get("url")
                       or it.get("link") or it.get("publicProfileUrl"))
        # Tag any match_skill mentioned in the headline so the screener
        # has signal to bite on.
        h_lower = headline.lower()
        skills = [s for s in match_skills if s and s.lower() in h_lower]
        if not skills:
            skills = list(match_skills)
        results.append({
            "name": name,
            "email": None,
            "current_employer": company,
            "current_job_title": headline[:200] if headline else None,
            "current_location": location,
            "skills": skills,
            "source": "linkedin_apify",
            "market": market,
            "source_profile_url": profile_url,
            "source_metadata": {
                "apify_actor": "linkedin",
                "linkedin_url": profile_url,
                "raw_headline": headline or None,
            },
        })
    return results


def _normalize_apify_yc(items: list[dict], market: str) -> list[dict]:
    """Map Apify YC company items to one candidate per founder.

    Each YC company emits 0..N founder rows; if no founders are listed we
    fall back to a single company-level row anchored on the company name.
    """
    results = []
    for company in items:
        company_name = (company.get("name") or company.get("companyName")
                        or company.get("title") or "")
        if not company_name:
            continue
        company_url = (company.get("website") or company.get("url")
                       or company.get("yc_url"))
        batch = company.get("batch") or company.get("ycBatch")
        industry = (company.get("industry") or company.get("vertical")
                    or company.get("industries"))
        if isinstance(industry, list):
            industry = ", ".join(str(i) for i in industry if i) or None
        founders = (company.get("founders") or company.get("team")
                    or company.get("people") or [])
        if not founders:
            results.append({
                "name": company_name,
                "email": None,
                "current_employer": company_name,
                "current_job_title": "Startup (YC)",
                "current_location": company.get("location") or None,
                "skills": [],
                "source": "yc",
                "market": market,
                "source_profile_url": company_url,
                "source_metadata": {
                    "apify_actor": "yc",
                    "yc_batch": batch,
                    "industry": industry,
                    "company": company_name,
                },
            })
            continue
        for f in founders:
            if not isinstance(f, dict):
                continue
            fname = (f.get("name") or f.get("fullName") or "").strip()
            if not fname:
                continue
            results.append({
                "name": fname,
                "email": f.get("email") or None,
                "current_employer": company_name,
                "current_job_title": (f.get("title") or "Founder"),
                "current_location": (f.get("location")
                                     or company.get("location") or None),
                "skills": [],
                "source": "yc",
                "market": market,
                "source_profile_url": (f.get("linkedinUrl")
                                       or f.get("linkedin")
                                       or company_url),
                "source_metadata": {
                    "apify_actor": "yc",
                    "yc_batch": batch,
                    "industry": industry,
                    "company": company_name,
                    "company_url": company_url,
                },
            })
    return results


async def source_apify(skills: list[str], location: str | None,
                       market: str, role_title: str | None = None) -> list[dict]:
    """Run LinkedIn (always) + YC (only when role hints startup) via Apify.

    LinkedIn is the workhorse for IN/SG mid-level engineers; YC is a small
    secondary channel that only fires for startup-flavored requirements
    (founder/early-stage/first hire). Both actors are run in parallel.
    """
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN not set")

    linkedin_actor = (os.environ.get("APIFY_LINKEDIN_ACTOR_ID")
                      or DEFAULT_APIFY_LINKEDIN_ACTOR)
    yc_actor = (os.environ.get("APIFY_YC_ACTOR_ID")
                or DEFAULT_APIFY_YC_ACTOR)

    keyword = " ".join(s for s in (skills or []) if s).strip()
    if not keyword and role_title:
        keyword = role_title
    geo = location or ("Singapore" if market == "SG" else "India")

    # LinkedIn People Search URL — filter by people, keywords, and a free-text
    # location string (Apify's actor accepts this form per their docs).
    from urllib.parse import quote_plus
    search_url = ("https://www.linkedin.com/search/results/people/"
                  f"?keywords={quote_plus(keyword)}"
                  f"&origin=GLOBAL_SEARCH_HEADER")
    if geo:
        search_url += f"&location={quote_plus(geo)}"

    linkedin_body = {
        "searchUrl": search_url,
        "maxResults": 30,
        # Some actor variants use these alternate keys; harmless if ignored.
        "keywords": keyword,
        "location": geo,
        "limit": 30,
    }
    yc_body = {
        # Default to recent batches; user can override actor input via the
        # APIFY_YC_ACTOR_ID swap if they want a different shape.
        "batches": ["W26", "S25", "W25", "S24"],
        "maxItems": 25,
    }

    role_lower = (role_title or "").lower()
    run_yc = any(trig in role_lower for trig in _YC_ROLE_TRIGGERS)

    coros = [_apify_run_actor(linkedin_actor, linkedin_body, token)]
    if run_yc:
        coros.append(_apify_run_actor(yc_actor, yc_body, token))
    gathered = await asyncio.gather(*coros, return_exceptions=True)

    out: list[dict] = []
    li_items = gathered[0]
    if isinstance(li_items, Exception):
        log.warning("apify linkedin actor failed: %s", li_items)
    else:
        out.extend(_normalize_apify_linkedin(li_items, skills, market))
    if run_yc:
        yc_items = gathered[1]
        if isinstance(yc_items, Exception):
            log.warning("apify yc actor failed: %s", yc_items)
        else:
            out.extend(_normalize_apify_yc(yc_items, market))
    return out


# ── Web-search agent (Claude + Brave/SerpAPI) ──────────────────

WEB_AGENT_QUERY_MODEL = "claude-haiku-4-5-20251001"
WEB_AGENT_EXTRACT_MODEL = "claude-sonnet-4-20250514"
WEB_AGENT_MAX_QUERIES = 5
WEB_AGENT_MAX_PAGES = 30
WEB_AGENT_MAX_CANDIDATES = 30
WEB_AGENT_TOTAL_TIMEOUT_SEC = 60


def _strip_llm_json(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return cleaned


async def _brave_search(query: str, count: int = 10) -> list[dict]:
    """Call Brave Search Web API. Returns the `results` list or []."""
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not api_key:
        return []
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params={"q": query, "count": count},
        )
        if resp.status_code != 200:
            log.warning("brave search %r HTTP %s: %s",
                        query, resp.status_code, resp.text[:200])
            return []
        data = resp.json() or {}
        return ((data.get("web") or {}).get("results")) or []


async def _serpapi_search(query: str, count: int = 10) -> list[dict]:
    """Call SerpAPI as a Brave fallback. Returns `organic_results` or []."""
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://serpapi.com/search.json",
            params={"q": query, "engine": "google",
                    "num": count, "api_key": api_key},
        )
        if resp.status_code != 200:
            log.warning("serpapi %r HTTP %s: %s",
                        query, resp.status_code, resp.text[:200])
            return []
        data = resp.json() or {}
        return data.get("organic_results") or []


async def _generate_search_queries(role_title: str | None,
                                   location: str | None,
                                   skills: list[str]) -> list[str]:
    """Ask Claude Haiku for 3-5 Google-style search queries."""
    import anthropic  # local import — keeps cold-start light for non-LLM sources

    n = WEB_AGENT_MAX_QUERIES
    skills_str = ", ".join(skills[:6]) if skills else "(none)"
    prompt = (
        f"Generate {n} Google-style search queries to find {role_title or 'engineer'} "
        f"candidates in {location or 'India / SE Asia'} with skills {skills_str}. "
        "Bias toward queries that surface personal blogs, conference talks, "
        "GitHub READMEs, and Indian/SE-Asian tech community sites (Hashnode, "
        "Dev.to, Medium). Return ONLY a JSON array of strings, no commentary."
    )

    def _sync_call() -> str:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=WEB_AGENT_QUERY_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text if resp.content else ""

    try:
        text = await asyncio.to_thread(_sync_call)
    except Exception as exc:
        log.warning("web_agent query gen failed: %s", exc)
        return []
    parsed = None
    try:
        parsed = json.loads(_strip_llm_json(text))
    except Exception:
        log.warning("web_agent query gen returned non-JSON: %s", text[:200])
    if not isinstance(parsed, list):
        return []
    return [str(q) for q in parsed if q][:WEB_AGENT_MAX_QUERIES]


async def _fetch_pages(urls: list[str]) -> list[tuple[str, str]]:
    """Fetch up to WEB_AGENT_MAX_PAGES URLs concurrently. Returns (url, html)."""
    sem = asyncio.Semaphore(8)

    async def _fetch_one(url: str) -> tuple[str, str] | None:
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=5,
                                             follow_redirects=True) as client:
                    r = await client.get(url, headers={
                        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac "
                                       "OS X 10_15_7) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) "
                                       "Chrome/146.0.0.0 Safari/537.36")})
                if r.status_code == 200 and r.text:
                    return (url, r.text[:8000])
            except Exception:
                pass
            return None

    results = await asyncio.gather(
        *[_fetch_one(u) for u in urls[:WEB_AGENT_MAX_PAGES]])
    return [r for r in results if r]


async def _extract_candidates_from_corpus(
    corpus: list[tuple[str, str]],
    role_title: str | None,
    location: str | None,
    skills: list[str],
    market: str,
) -> list[dict]:
    """Hand the page corpus to Claude Sonnet for candidate extraction."""
    import anthropic

    if not corpus:
        return []

    skills_str = ", ".join(skills[:6]) if skills else "(any)"
    snippets = "\n\n---\n\n".join(
        f"URL: {url}\n{html}" for url, html in corpus)
    prompt = (
        f"Requirement: role={role_title or 'engineer'}; "
        f"location={location or 'India/SE Asia'}; skills={skills_str}.\n\n"
        "From the following web pages, extract people who clearly match "
        "the requirement. For each, return a JSON object with: name, "
        "current_job_title, current_employer, current_location, "
        "source_profile_url (the most relevant URL for that person), "
        "and evidence_quote (a short quote from the page that proves the "
        "match). Only include people whose page clearly establishes them as "
        f"fitting the requirement. Cap at {WEB_AGENT_MAX_CANDIDATES}. "
        "Return ONLY a JSON array, no commentary.\n\n"
        f"PAGES:\n{snippets}"
    )

    def _sync_call() -> str:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=WEB_AGENT_EXTRACT_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text if resp.content else ""

    try:
        text = await asyncio.to_thread(_sync_call)
    except Exception as exc:
        log.warning("web_agent extraction failed: %s", exc)
        return []
    try:
        parsed = json.loads(_strip_llm_json(text))
    except Exception:
        log.warning("web_agent extraction returned non-JSON: %s", text[:200])
        return []
    if not isinstance(parsed, list):
        return []

    out = []
    for item in parsed[:WEB_AGENT_MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "email": None,
            "current_employer": item.get("current_employer") or None,
            "current_job_title": item.get("current_job_title") or None,
            "current_location": item.get("current_location") or None,
            "skills": list(skills) if skills else [],
            "source": "web_agent",
            "market": market,
            "source_profile_url": item.get("source_profile_url") or None,
            "source_metadata": {
                "evidence_quote": item.get("evidence_quote") or None,
                "search_engine": ("brave" if os.environ.get(
                    "BRAVE_SEARCH_API_KEY") else "serpapi"),
            },
        })
    return out


async def source_web_agent(role_title: str | None, location: str | None,
                           skills: list[str], market: str) -> list[dict]:
    """Autonomously discover candidates from the open web via search + LLM.

    Pipeline: Claude Haiku generates queries -> Brave (fallback SerpAPI) ->
    fetch pages -> Claude Sonnet extracts people. Hard-capped at
    WEB_AGENT_TOTAL_TIMEOUT_SEC so a slow page can't block the boost.
    """
    if not skills:
        raise RuntimeError("web_agent needs skills_required to seed queries")
    if not (os.environ.get("BRAVE_SEARCH_API_KEY")
            or os.environ.get("SERPAPI_KEY")):
        raise RuntimeError("web_agent needs BRAVE_SEARCH_API_KEY or SERPAPI_KEY")

    async def _run() -> list[dict]:
        queries = await _generate_search_queries(role_title, location, skills)
        if not queries:
            return []

        urls: list[str] = []
        seen: set[str] = set()
        for q in queries:
            results = await _brave_search(q, count=10)
            engine = "brave"
            if not results:
                results = await _serpapi_search(q, count=10)
                engine = "serpapi"
            for r in results:
                u = r.get("url") or r.get("link")
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
                if len(urls) >= WEB_AGENT_MAX_PAGES:
                    break
            log.info("web_agent query=%r engine=%s urls_total=%d",
                     q, engine, len(urls))
            if len(urls) >= WEB_AGENT_MAX_PAGES:
                break

        if not urls:
            return []
        corpus = await _fetch_pages(urls)
        return await _extract_candidates_from_corpus(
            corpus, role_title, location, skills, market)

    try:
        return await asyncio.wait_for(_run(),
                                      timeout=WEB_AGENT_TOTAL_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        log.warning("web_agent hit %ds total timeout",
                    WEB_AGENT_TOTAL_TIMEOUT_SEC)
        return []


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

    # GitHub — global engineering, free with PAT
    if os.environ.get("GITHUB_TOKEN"):
        tasks.append(("github", source_github(skills, location, market)))

    # Hugging Face — ML/AI engineers, free public API. Gate on a separate flag
    # so we don't fire HF on every requirement (the public API works without
    # auth, so a token check alone wouldn't be enough of a gate).
    if os.environ.get("HF_ENABLED") or os.environ.get("HF_TOKEN"):
        tasks.append(("huggingface", source_huggingface(skills, market)))

    # Apify — LinkedIn People Search (always) + YC directory (only for
    # startup-flavored requirements). Workhorse for IN/SG mid-level roles.
    if os.environ.get("APIFY_TOKEN"):
        tasks.append(("apify", source_apify(
            skills, location, market, requirement.get("role_title"))))

    # Web-search agent — Claude + Brave/SerpAPI. Discovers people no other
    # channel surfaces (personal blogs, conference talks, GitHub READMEs).
    if (skills and (os.environ.get("BRAVE_SEARCH_API_KEY")
                    or os.environ.get("SERPAPI_KEY"))):
        tasks.append(("web_agent", source_web_agent(
            requirement.get("role_title"), location, skills, market)))

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
