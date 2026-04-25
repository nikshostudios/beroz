"""Cost-capped smoke test for harvestapi/linkedin-profile-search.

Run from the repo root:
    APIFY_TOKEN=<token> python -m backend.ai_agents.scripts.test_apify_harvestapi

What it does (and what it costs):
    Hits the harvestapi LinkedIn search actor with a tiny budget — 1 page,
    10 max profiles. Pricing per the actor page is $0.1/page + $0.004/profile,
    so the worst-case spend is **$0.14** per run. Well under the $1 cap.

What it prints:
    1. The exact actor + body we sent
    2. The number of items returned
    3. The first raw item (so we can sanity-check field names)
    4. The first item run through `_normalize_apify_linkedin` (so we can
       confirm the candidate-dict mapping looks right)

If the printed candidate dict has populated `name`, `current_employer`,
`current_location`, and `source_profile_url`, the integration is good and
we can crank `maxItems` to 30 in the real boost flow without further code
changes.
"""

import asyncio
import json
import os
import sys

from backend.ai_agents.config import sourcing


SAMPLE_QUERY = {
    "skills": ["ServiceNow", "JavaScript"],
    "role_title": "ServiceNow Developer",
    "location": "Bangalore",
    "max_items": 10,
}


async def main() -> int:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("ERROR: set APIFY_TOKEN env var first.")
        return 1

    actor_id = (os.environ.get("APIFY_LINKEDIN_ACTOR_ID")
                or sourcing.DEFAULT_APIFY_LINKEDIN_ACTOR)
    body = {
        "profileScraperMode": "Full",
        "maxItems": SAMPLE_QUERY["max_items"],
        "startPage": 1,
        "searchQuery": " ".join(SAMPLE_QUERY["skills"]),
        "currentJobTitles": [SAMPLE_QUERY["role_title"]],
        "locations": [SAMPLE_QUERY["location"]],
    }

    print("=" * 60)
    print(f"Actor    : {actor_id}")
    print(f"Body     : {json.dumps(body, indent=2)}")
    print(f"Est cost : 1 page * $0.1 + {body['maxItems']} * $0.004 = "
          f"${0.1 + body['maxItems'] * 0.004:.2f}")
    print("=" * 60)

    items = await sourcing._apify_run_actor(
        actor_id, body, token, timeout_sec=120)
    print(f"\n→ Actor returned {len(items)} item(s)\n")
    if not items:
        print("⚠️  No items returned. Possible causes: token invalid, "
              "credit balance <$0.14, query produced zero results, "
              "actor still warming up.")
        return 2

    print("─── RAW first item ─────────────────────────────────────────")
    print(json.dumps(items[0], indent=2, default=str)[:2000])
    print()
    print("─── NORMALIZED first candidate ─────────────────────────────")
    normalized = sourcing._normalize_apify_linkedin(
        items[:1], SAMPLE_QUERY["skills"], market="IN")
    print(json.dumps(normalized[0] if normalized else {}, indent=2,
                     default=str))

    if normalized:
        c = normalized[0]
        gaps = [k for k in
                ("name", "current_employer", "current_location",
                 "source_profile_url")
                if not c.get(k)]
        if gaps:
            print(f"\n⚠️  Normalizer dropped these fields: {gaps}. "
                  "Re-tune _normalize_apify_linkedin field aliases.")
        else:
            print("\n✅ Normalizer mapped all key fields. Safe to wire "
                  "into the boost flow.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
