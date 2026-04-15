#!/usr/bin/env python3
"""Local Foundit Sourcing Agent
================================
Runs on your Mac, sources candidates from Foundit using your local Chrome
session (same IP as login → no session invalidation), and pushes results
directly to Supabase. Railway then picks them up for matching + screening.

WHY LOCAL:
  Foundit ties the session cookie to the originating IP. Railway's servers
  have a different IP, so the cookie is invalidated within minutes. Running
  here bypasses that entirely — no cookie refresh needed during a session.

USAGE:
  # Install deps once
  pip3 install httpx supabase browser-cookie3 python-dotenv

  # Run continuously (polls every 10 minutes)
  python3 scripts/local_foundit_agent.py

  # Run once and exit
  python3 scripts/local_foundit_agent.py --once

  # Source a specific requirement only
  python3 scripts/local_foundit_agent.py --req-id c8e44291-95e5-4533-b21e-31a3d7d4c62e

  # Dry run — show what would be sourced, don't write to DB
  python3 scripts/local_foundit_agent.py --dry-run --once

PREREQUISITES:
  - Chrome open and logged into recruiter.foundit.sg
  - .env file at el-paso/.env with SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Load .env ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV_FILE = REPO_ROOT / ".env"

def load_env(path: Path) -> None:
    """Simple .env loader — avoids requiring python-dotenv if not installed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

load_env(ENV_FILE)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("local-foundit")

# ── Constants ─────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 10 * 60          # poll every 10 minutes
FOUNDIT_RECRUITER_DOMAIN = "recruiter.foundit.sg"
FOUNDIT_CORP_ID   = int(os.environ.get("FOUNDIT_CORP_ID",   "560219"))
FOUNDIT_SUBUID    = int(os.environ.get("FOUNDIT_SUBUID",    "1347319"))
FOUNDIT_CORP_NAME = os.environ.get("FOUNDIT_CORP_NAME", "ExcelTech Computers Pte Ltd")
FOUNDIT_SITE_CONTEXT = {"SG": "monstersingapore", "IN": "monsterindia"}
FOUNDIT_DOMAIN    = "recruiter.foundit.sg"
REQUIRED_COOKIES  = ["C", "csrftoken", "django_language"]


# ── Custom exceptions ─────────────────────────────────────────────────────────

class _CookieExpiredError(Exception):
    """Raised when Foundit returns 401/403 — session expired or IP mismatch."""
    def __init__(self, status_code: int):
        super().__init__(f"Foundit returned HTTP {status_code}")
        self.status_code = status_code


# ── Cookie reading ─────────────────────────────────────────────────────────────

def get_chrome_cookie() -> str:
    """Read the Foundit session cookie from Chrome's local cookie store."""
    try:
        import browser_cookie3
    except ImportError:
        log.error("browser-cookie3 not installed. Run: pip3 install browser-cookie3")
        sys.exit(1)

    chrome_base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    profiles = []
    for base in [chrome_base, Path.home() / "Library" / "Application Support" / "Chromium"]:
        if not base.exists():
            continue
        for entry in base.iterdir():
            if entry.name in ("Default",) or entry.name.startswith("Profile"):
                cookie_path = entry / "Cookies"
                if cookie_path.exists():
                    profiles.append(cookie_path)

    if not profiles:
        log.error("No Chrome profiles found. Is Chrome installed?")
        sys.exit(1)

    for cookie_file in profiles:
        try:
            # browser_cookie3 only copies the main Cookies file, not the WAL.
            # Chrome uses WAL-mode SQLite — in-memory updates live in Cookies-wal
            # and may not be flushed to the main file yet. Copy BOTH files to a
            # temp dir so sqlite3 merges them and returns the live state.
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_db = Path(tmpdir) / "Cookies"
                shutil.copy2(cookie_file, tmp_db)
                for ext in ("-wal", "-shm"):
                    src = Path(str(cookie_file) + ext)
                    if src.exists():
                        shutil.copy2(src, Path(tmpdir) / ("Cookies" + ext))

                jar = browser_cookie3.chrome(
                    cookie_file=str(tmp_db),
                    domain_name=FOUNDIT_DOMAIN,
                )
                cookies = {c.name: c.value for c in jar}
            if "C" in cookies:
                # Build ordered cookie header
                parts = [f"{k}={cookies[k]}" for k in REQUIRED_COOKIES if k in cookies]
                parts += [f"{k}={v}" for k, v in cookies.items() if k not in REQUIRED_COOKIES]
                log.info("✅ Got Foundit session cookie from Chrome profile '%s' (C=%.8s...)",
                         cookie_file.parent.name, cookies["C"])
                return "; ".join(parts)
        except Exception as e:
            log.debug("Profile %s: %s", cookie_file.parent.name, e)

    log.error(
        "No active Foundit session found in Chrome.\n"
        "  → Open Chrome and log into recruiter.foundit.sg, then run this script again."
    )
    sys.exit(1)


# ── Supabase helpers ──────────────────────────────────────────────────────────

def get_supabase():
    try:
        from supabase import create_client
    except ImportError:
        log.error("supabase not installed. Run: pip3 install supabase")
        sys.exit(1)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
        sys.exit(1)
    return create_client(url, key)


def get_open_requirements(db, req_id: str | None = None) -> list[dict]:
    """Fetch open requirements that need sourcing."""
    q = db.table("requirements").select(
        "id, role_title, market, skills_required, experience_min, location"
    )
    if req_id:
        q = q.eq("id", req_id)
    else:
        q = q.eq("status", "open")
    rows = q.execute().data
    return rows or []


def upsert_candidate(db, candidate: dict, dry_run: bool = False) -> bool:
    """Upsert a candidate to Supabase. Returns True if new/updated."""
    if dry_run:
        log.info("  [DRY RUN] Would upsert: %s (%s)", candidate.get("name"), candidate.get("current_job_title"))
        return True

    # Normalise skills
    skills = candidate.get("skills", [])
    if isinstance(skills, list):
        # Flatten any composite skill strings
        flat = []
        for s in skills:
            flat.extend(s.replace("/", ",").replace(";", ",").split(","))
        candidate["skills"] = [s.strip() for s in flat if s.strip()]

    try:
        if candidate.get("email") and "****" not in str(candidate.get("email", "")):
            db.table("candidates").upsert(candidate, on_conflict="email").execute()
        elif candidate.get("name"):
            # Check by name + source
            name = candidate["name"].strip()
            source = candidate.get("source", "foundit")
            existing = (db.table("candidates")
                        .select("id")
                        .eq("name", name)
                        .eq("source", source)
                        .execute().data)
            if existing:
                db.table("candidates").update(candidate).eq("id", existing[0]["id"]).execute()
            else:
                db.table("candidates").insert(candidate).execute()
        return True
    except Exception as e:
        log.warning("  Failed to upsert %s: %s", candidate.get("name"), e)
        return False


# ── Chrome-native API call (primary path) ─────────────────────────────────────

def _call_foundit_via_chrome(body: dict) -> dict:
    """
    Execute the Foundit search by injecting a synchronous XHR into an open
    recruiter.foundit.sg Chrome tab via AppleScript.

    Chrome handles all cookies natively (including httpOnly C= session cookie),
    so no cookie extraction is needed. The request body is base64-encoded to
    pass cleanly through the AppleScript / JS boundary without any escaping.

    Returns the parsed JSON response dict.
    Raises _CookieExpiredError on 401/403.
    Raises RuntimeError("no_foundit_tab") if no matching tab is open.
    """
    body_b64 = base64.b64encode(json.dumps(body).encode()).decode()

    js = f"""(function(){{
  try {{
    var b = JSON.parse(atob('{body_b64}'));
    var x = new XMLHttpRequest();
    x.open('POST','https://recruiter.foundit.sg/edge/recruiter-search/api/search-middleware/v2/search',false);
    x.setRequestHeader('Content-Type','application/json');
    var csrf=(document.cookie.match(/csrftoken=([^;]+)/)||[])[1]||'';
    if(csrf)x.setRequestHeader('X-CSRFToken',csrf);
    x.send(JSON.stringify(b));
    var status=x.status;
    if(status!==200){{return JSON.stringify({{ok:true,status:status,resumes:[]}});}}
    // Parse in JS and return only the resumes array — avoids truncation on large responses
    var full=JSON.parse(x.responseText);
    var resumes=(full.response||full).resumes||[];
    return JSON.stringify({{ok:true,status:status,resumes:resumes}});
  }}catch(e){{return JSON.stringify({{ok:false,error:e.toString()}});}}
}})()"""

    # Write JS to /tmp so AppleScript can load it — avoids ALL string-escaping issues
    js_path = Path("/tmp/foundit_chrome_request.js")
    js_path.write_text(js)

    applescript = """\
tell application "Google Chrome"
    set foundTab to missing value
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "recruiter.foundit.sg" then
                set foundTab to t
                exit repeat
            end if
        end repeat
        if foundTab is not missing value then exit repeat
    end repeat
    if foundTab is missing value then
        return "{\\"ok\\":false,\\"error\\":\\"no_foundit_tab\\"}"
    end if
    set jsCode to do shell script "cat /tmp/foundit_chrome_request.js"
    return execute foundTab javascript jsCode
end tell"""

    result = subprocess.run(
        ["osascript", "-e", applescript],
        capture_output=True, text=True, timeout=45,
    )

    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")

    raw = result.stdout.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unexpected response from AppleScript: {raw[:300]}")

    if not data.get("ok"):
        err = data.get("error", "unknown")
        if err == "no_foundit_tab":
            raise RuntimeError("no_foundit_tab")
        raise RuntimeError(f"JS error in Chrome: {err}")

    status = data.get("status", 0)
    if status in (401, 403):
        raise _CookieExpiredError(status)
    if status != 200:
        raise RuntimeError(f"Foundit API HTTP {status}")

    # Return in the same shape _parse_response expects: {"response": {"resumes": [...]}}
    return {"response": {"resumes": data.get("resumes", [])}}


# ── Foundit API call ──────────────────────────────────────────────────────────

def _build_foundit_body(
    skills: list[str],
    experience_min: str | None,
    location: str | None,
    market: str,
    page: int = 0,
) -> dict:
    """Build the Foundit search request body for a given page (0-indexed)."""
    query = " ".join(skills) if skills else "IT developer"
    site_context = FOUNDIT_SITE_CONTEXT.get(market, "monstersingapore")
    search_location = location or ("Singapore" if market == "SG" else "India")

    body: dict = {
        "appName": "recSRP",
        "reqParam": {
            "corp_company_name": FOUNDIT_CORP_NAME,
            "subuid": FOUNDIT_SUBUID,
            "corp_id": FOUNDIT_CORP_ID,
            "site_context": site_context,
            "recruiter_company_name": FOUNDIT_CORP_NAME,
            "recruiter_db_access_contexts": [site_context],
            "session_cid": "4",
            "session_scid": "6",
            "channel_id": 4,
            "sub_channel_id": 6,
            "email": "",
            "logo": "",
            "is_new_search_request": page == 0,
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
                "location": [search_location, search_location],
            },
            "filters": {
                "company": {
                    "currency": "INR",
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
            "from": page * 40,
            "express_resumes": {"from": page * 4, "size": 4},
            "api_profile_flag": 1,
            "strict": True,
            "is_corp_based_search": False,
            "is_v2_request": True,
            "use_synonyms_fields": True,
            "sub_source": "search",
            "search_source": "New Search" if page == 0 else "Pagination",
        },
    }

    if experience_min:
        body["reqParam"]["service_filter"]["experience"] = {
            "min": int(experience_min), "max": 30
        }

    return body


async def source_foundit(
    skills: list[str],
    experience_min: str | None,
    location: str | None,
    cookie: str,
    market: str = "SG",
) -> list[dict]:
    """
    Source candidates from Foundit.

    Primary path: execute the search from within an open Chrome tab using
    AppleScript — Chrome handles all cookies natively (including httpOnly C=).

    Fallback path: direct HTTP with the cookie extracted from Chrome's disk store.
    Used automatically when no recruiter.foundit.sg tab is open in Chrome.
    """
    all_candidates: list[dict] = []
    use_chrome = True  # try Chrome first; flip to False on "no_foundit_tab"

    for page in range(2):
        if page == 1 and len(all_candidates) < 40:
            break  # page 1 wasn't full — no point fetching page 2

        body = _build_foundit_body(skills, experience_min, location, market, page)

        if use_chrome:
            try:
                log.debug("  Calling Foundit via Chrome tab (page %d)…", page + 1)
                response_json = _call_foundit_via_chrome(body)
            except RuntimeError as e:
                if "no_foundit_tab" in str(e):
                    log.warning(
                        "  No recruiter.foundit.sg tab open in Chrome — "
                        "falling back to direct HTTP (cookie may be stale)."
                    )
                    use_chrome = False
                else:
                    raise
            else:
                page_results = _parse_response(response_json, market)
                all_candidates.extend(page_results)
                log.info("  Page %d: %d candidates (via Chrome)", page + 1, len(page_results))
                continue

        # ── HTTP fallback ──────────────────────────────────────────────────
        try:
            import httpx
        except ImportError:
            log.error("httpx not installed. Run: pip3 install httpx")
            sys.exit(1)

        csrf_token = ""
        for part in cookie.split("; "):
            part = part.strip()
            if part.startswith("csrftoken="):
                csrf_token = part.split("=", 1)[1]
                break

        headers = {
            "content-type": "application/json",
            "accept": "*/*",
            "cookie": cookie,
            "x-csrftoken": csrf_token,
            "origin": f"https://{FOUNDIT_RECRUITER_DOMAIN}",
            "referer": f"https://{FOUNDIT_RECRUITER_DOMAIN}/edge/recruiter-search/search",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
        }
        api_url = (
            f"https://{FOUNDIT_RECRUITER_DOMAIN}"
            "/edge/recruiter-search/api/search-middleware/v2/search"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(api_url, headers=headers, json=body)

        if resp.status_code in (401, 403):
            raise _CookieExpiredError(resp.status_code)
        resp.raise_for_status()

        page_results = _parse_response(resp.json(), market)
        all_candidates.extend(page_results)
        log.info("  Page %d: %d candidates (via HTTP)", page + 1, len(page_results))

    return all_candidates


def _parse_response(data: dict, market: str) -> list[dict]:
    results = []
    resumes = data.get("response", data).get("resumes", [])

    for c in resumes:
        name = c.get("name", "")
        if not name or name == "N/A":
            continue

        # Skills
        skills_raw = []
        for s in c.get("skills", []):
            text = s.get("text", "") if isinstance(s, dict) else str(s)
            if text:
                skills_raw.append(text)

        # Experience
        exp = c.get("experience")
        if isinstance(exp, (int, float)):
            years = int(exp)
            months = round((exp - years) * 12)
            exp_str = f"{years} years" + (f" {months} months" if months else "")
        else:
            exp_str = str(exp) if exp else ""

        # Location
        loc = c.get("current_location", "")
        if isinstance(loc, dict):
            loc = loc.get("text", "")

        # Employment
        emp = c.get("current_employment") or {}
        desig = emp.get("designation", {})
        job_title = desig.get("text", "") if isinstance(desig, dict) else str(desig or "")
        employer_obj = emp.get("employer", {})
        employer = employer_obj.get("text", "") if isinstance(employer_obj, dict) else str(employer_obj or "")

        # Email
        email_list = c.get("email", [])
        email = None
        if isinstance(email_list, list) and email_list:
            first = email_list[0]
            raw_email = first.get("id") if isinstance(first, dict) else str(first)
            if raw_email and "****" not in raw_email:
                email = raw_email

        # Phone
        mobile = c.get("mobile_details", [])
        phone = None
        if isinstance(mobile, list) and mobile:
            first = mobile[0]
            phone = first.get("number") if isinstance(first, dict) else None

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


# ── Main sourcing loop ────────────────────────────────────────────────────────

async def source_requirement(db, req: dict, cookie: str, dry_run: bool) -> int:
    """Source candidates for one requirement. Returns count upserted."""
    req_id    = req["id"]
    title     = req.get("role_title", "Unknown Role")
    market    = req.get("market", "SG")
    skills    = req.get("skills_required") or []
    exp_min   = req.get("experience_min")
    location  = req.get("location")

    log.info("──────────────────────────────────────────")
    log.info("📋 Requirement: %s [%s]", title, market)
    log.info("   Skills: %s", ", ".join(skills) if skills else "(none)")
    log.info("   Experience min: %s", exp_min or "any")

    candidates = await source_foundit(skills, exp_min, location, cookie, market)

    if not candidates:
        log.warning("  Foundit returned 0 candidates for '%s'", title)
        return 0

    log.info("  Found %d candidates — upserting to Supabase...", len(candidates))
    saved = 0
    for cand in candidates:
        if upsert_candidate(db, {**cand, "market": market}, dry_run=dry_run):
            saved += 1

    log.info("  ✅ Upserted %d/%d candidates for '%s'", saved, len(candidates), title)
    return saved


async def run_once(args) -> None:
    db     = get_supabase()
    cookie = get_chrome_cookie()

    reqs = get_open_requirements(db, req_id=args.req_id)
    if not reqs:
        msg = f"Requirement {args.req_id} not found." if args.req_id else "No open requirements found."
        log.warning(msg)
        return

    log.info("Found %d requirement(s) to source", len(reqs))
    total_saved = 0
    for req in reqs:
        retries = 0
        while True:
            try:
                saved = await source_requirement(db, req, cookie, dry_run=args.dry_run)
                total_saved += saved
                break
            except _CookieExpiredError as e:
                retries += 1
                if retries > 2:
                    log.error("Cookie refresh failed after %d attempts — skipping requirement '%s'.",
                              retries, req.get("role_title", req["id"]))
                    break
                print(
                    f"\n⚠️  Foundit session expired ({e.status_code}).\n"
                    "   Chrome must be open with recruiter.foundit.sg loaded in a tab.\n"
                    "   1. Switch to Chrome\n"
                    "   2. Open (or reload) recruiter.foundit.sg\n"
                    "   3. Wait until the page finishes loading\n"
                    "   4. Come back here and press Enter to retry…",
                    flush=True,
                )
                try:
                    input()
                except EOFError:
                    pass  # non-interactive mode — just re-read without prompting
                log.info("Re-reading cookie from Chrome (attempt %d/2)…", retries)
                cookie = get_chrome_cookie()

    log.info("══════════════════════════════════════════")
    log.info("Done. Total candidates upserted: %d", total_saved)

    # ── Auto-match after sourcing ──────────────────────────────────────────────
    if getattr(args, "and_match", False) and not args.dry_run and total_saved > 0:
        railway_url = os.environ.get("RAILWAY_AGENT_URL", "").rstrip("/")
        email       = os.environ.get("RECRUITER_EMAIL", "")
        role        = os.environ.get("RECRUITER_ROLE", "recruiter")

        if not railway_url:
            log.warning("--and-match: RAILWAY_AGENT_URL not set in .env — skipping match step.")
        else:
            req_ids = [r["id"] for r in reqs]
            try:
                import httpx as _httpx
            except ImportError:
                log.error("httpx not installed — cannot trigger match. Run: pip3 install httpx")
                return

            log.info("\n🤖 Triggering LLM match on Railway for %d requirement(s)…", len(req_ids))
            async with _httpx.AsyncClient(timeout=300) as client:
                for req_id in req_ids:
                    url = f"{railway_url}/requirements/{req_id}/match"
                    log.info("  POST %s", url)
                    try:
                        resp = await client.post(
                            url,
                            headers={"x-user-role": role, "x-user-email": email},
                        )
                        data = resp.json()
                        if resp.status_code == 200:
                            log.info(
                                "  ✅ Matched %d candidates for requirement %s",
                                data.get("matched", "?"), req_id,
                            )
                        else:
                            log.error("  ❌ Match failed (%d): %s", resp.status_code, data)
                    except Exception as e:
                        log.error("  ❌ Match request failed: %s", e)


async def run_loop(args) -> None:
    interval = args.interval
    log.info("🚀 Local Foundit Agent started — polling every %d minutes", interval // 60)
    log.info("   Press Ctrl+C to stop.\n")

    while True:
        try:
            await run_once(args)
        except SystemExit:
            raise
        except Exception as e:
            log.error("Run failed: %s", e, exc_info=True)

        log.info("\n💤 Sleeping %d minutes until next poll...\n", interval // 60)
        await asyncio.sleep(interval)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Local Foundit sourcing agent — runs on your Mac, pushes candidates to Supabase."
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run once and exit instead of polling continuously."
    )
    parser.add_argument(
        "--req-id", metavar="UUID",
        help="Source a specific requirement ID only."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be sourced without writing to DB."
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL_SECONDS,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL_SECONDS})."
    )
    parser.add_argument(
        "--and-match", action="store_true",
        help="After sourcing, automatically trigger LLM matching on Railway. "
             "Requires RAILWAY_AGENT_URL and RECRUITER_EMAIL in .env."
    )
    args = parser.parse_args()

    if args.once or args.req_id or args.dry_run:
        asyncio.run(run_once(args))
    else:
        asyncio.run(run_loop(args))


if __name__ == "__main__":
    main()
