#!/usr/bin/env python3
"""Sourcing diagnostic — checks each channel and reports exact failures.

Run locally:
    cd "<repo>"
    export APOLLO_API_KEY=...        # optional
    export FOUNDIT_SESSION_COOKIE=... # optional
    export NAUKRI_SESSION_COOKIE=...  # optional
    python3 backend/scripts/diagnose_sourcing.py

Run on Railway (one-off):
    railway run python3 backend/scripts/diagnose_sourcing.py

No Supabase / Anthropic credentials needed — this only probes the third-party
sourcing APIs directly.
"""

import asyncio
import json
import os
import sys

import httpx

# Small fake requirement used across all channels
TEST_REQUIREMENT = {
    "skills": ["Python", "AWS"],
    "location": "Bangalore",
    "market": "IN",
}


def _print_header(title: str) -> None:
    print()
    print("=" * 72)
    print(" " + title)
    print("=" * 72)


async def test_apollo() -> None:
    _print_header("Apollo.io  —  POST https://api.apollo.io/v1/mixed_people/search")
    key = (os.environ.get("APOLLO_API_KEY")
           or os.environ.get("APOLLO_API") or "").strip()
    if not key:
        print("[SKIP] Neither APOLLO_API_KEY nor APOLLO_API is set.")
        print("       Set it in Railway → Variables (for prod) or export locally.")
        return
    alias = ("APOLLO_API_KEY" if os.environ.get("APOLLO_API_KEY")
             else "APOLLO_API")
    print(f"[OK]   {alias} is set ({len(key)} chars).")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.apollo.io/v1/mixed_people/search",
                headers={"X-Api-Key": key, "Cache-Control": "no-cache",
                         "Content-Type": "application/json"},
                json={
                    "q_keywords": " ".join(TEST_REQUIREMENT["skills"]),
                    "person_locations": [TEST_REQUIREMENT["location"]],
                    "per_page": 5,
                },
            )
    except httpx.RequestError as e:
        print(f"[FAIL] Network/transport error: {type(e).__name__}: {e}")
        return

    print(f"[HTTP] {resp.status_code} {resp.reason_phrase}")

    if resp.status_code == 401:
        print("[FAIL] 401 Unauthorized — the API key is rejected. "
              "Regenerate it in Apollo → Settings → Integrations → API.")
        print("       Body:", resp.text[:300])
        return
    if resp.status_code == 403:
        print("[FAIL] 403 Forbidden — Apollo has gated this endpoint behind "
              "paid tiers for some accounts. Check your plan.")
        print("       Body:", resp.text[:300])
        return
    if resp.status_code == 422:
        print("[FAIL] 422 Unprocessable — request body shape rejected.")
        print("       Body:", resp.text[:600])
        return
    if resp.status_code == 429:
        print("[FAIL] 429 Too Many Requests — Apollo rate-limited. "
              "Back off and try again.")
        return
    if resp.status_code >= 500:
        print(f"[FAIL] Apollo returned {resp.status_code}. Body:", resp.text[:300])
        return
    if resp.status_code != 200:
        print(f"[FAIL] Unexpected status. Body:", resp.text[:300])
        return

    data = resp.json()
    people = data.get("people", [])
    pagination = data.get("pagination", {})
    total = pagination.get("total_entries", "?")
    print(f"[OK]   Apollo returned {len(people)} people (total available: {total}).")
    if not people:
        print("[WARN] Zero results for this query. Could be (a) the q_keywords / "
              "person_locations combo is too narrow, (b) the account has no "
              "available credits for search results, or (c) your plan hides "
              "results behind the paywall.")
        return

    with_email = sum(1 for p in people if p.get("email"))
    print(f"[INFO] {with_email}/{len(people)} results include an email address.")
    print(f"[INFO] Sample: {people[0].get('name', '?')} — "
          f"{people[0].get('title', '?')} @ "
          f"{(people[0].get('organization') or {}).get('name', '?')}")


async def test_foundit() -> None:
    _print_header("Foundit Recruiter  —  cookie auth")
    cookie = os.environ.get("FOUNDIT_SESSION_COOKIE", "").strip()
    if not cookie:
        print("[SKIP] FOUNDIT_SESSION_COOKIE is not set.")
        return
    print(f"[OK]   FOUNDIT_SESSION_COOKIE is set ({len(cookie)} chars).")
    # Minimal ping — hit the search endpoint with a tiny query and see the
    # status code. A successful auth usually returns 200 with a JSON body.
    domain = os.environ.get("FOUNDIT_RECRUITER_DOMAIN", "recruiter.foundit.sg")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://{domain}/edge/recruiter-search/search?query=python",
                headers={"cookie": cookie},
                follow_redirects=False,
            )
    except httpx.RequestError as e:
        print(f"[FAIL] Network/transport error: {type(e).__name__}: {e}")
        return
    print(f"[HTTP] {resp.status_code} {resp.reason_phrase} (domain: {domain})")
    if resp.status_code in (401, 403):
        print("[FAIL] Cookie expired. Refresh via "
              "backend/scripts/refresh_foundit_cookie.py or log in via a browser.")
    elif resp.status_code == 200:
        print("[OK]   Foundit accepts the cookie.")
    else:
        print("[WARN] Unexpected status. Body:", resp.text[:300])


async def test_naukri() -> None:
    _print_header("Naukri Resdex  —  cookie auth, India only")
    cookie = os.environ.get("NAUKRI_SESSION_COOKIE", "").strip()
    if not cookie:
        print("[SKIP] NAUKRI_SESSION_COOKIE is not set.")
        return
    print(f"[OK]   NAUKRI_SESSION_COOKIE is set ({len(cookie)} chars).")
    domain = os.environ.get("NAUKRI_RESDEX_DOMAIN", "resdex.naukri.com")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://{domain}/v0/resdex-search/search",
                headers={"content-type": "application/json", "cookie": cookie,
                         "appid": "205", "systemid": "Starter"},
                json={"keyword": "python", "locations": ["India"],
                      "pageNo": 1, "noOfResults": 3},
            )
    except httpx.RequestError as e:
        print(f"[FAIL] Network/transport error: {type(e).__name__}: {e}")
        return
    print(f"[HTTP] {resp.status_code} {resp.reason_phrase}")
    if resp.status_code in (401, 403):
        print("[FAIL] Cookie expired. Refresh manually from DevTools.")
    elif resp.status_code == 200:
        data = resp.json()
        results = data.get("searchResults", data.get("results", []))
        print(f"[OK]   Naukri returned {len(results)} results.")
    else:
        print("[WARN] Unexpected status. Body:", resp.text[:300])


async def main() -> None:
    print("Sourcing-channel diagnostic — test query: "
          f"{json.dumps(TEST_REQUIREMENT)}")
    await test_apollo()
    await test_foundit()
    await test_naukri()
    print()
    print("Done. If every channel printed [SKIP], Source Now cannot find "
          "anyone — configure at least one.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
