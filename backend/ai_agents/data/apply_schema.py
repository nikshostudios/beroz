#!/usr/bin/env python3
"""Apply a SQL file to Supabase via the Management API.

Usage:
    SUPABASE_ACCESS_TOKEN=sbp_... python apply_schema.py phase3_shortlists_notes.sql

Env vars:
    SUPABASE_ACCESS_TOKEN   Personal Access Token from Supabase → Account → Access Tokens
    SUPABASE_URL            Used to derive the project ref (already set in Railway)

Generate a PAT at: https://supabase.com/dashboard/account/tokens
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

# Load .env from project root if present
_env = Path(__file__).resolve().parents[3] / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        pass


def _project_ref() -> str:
    url = os.environ.get("SUPABASE_URL", "")
    if "//" not in url:
        raise SystemExit("SUPABASE_URL is not set or malformed")
    return url.split("//")[1].split(".")[0]


def run_sql(sql: str) -> None:
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        raise SystemExit(
            "Set SUPABASE_ACCESS_TOKEN to a Personal Access Token.\n"
            "Generate one at: Supabase Dashboard → Account → Access Tokens"
        )

    ref = _project_ref()
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "supabase-cli/2.0",
        },
        method="POST",
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        result = json.loads(r.read().decode())
        print(f"OK (status {r.status}) — {len(result)} rows returned")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        raise SystemExit(f"HTTP {e.code}: {body}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python apply_schema.py <sql_file>")
        raise SystemExit(1)

    sql_path = Path(sys.argv[1])
    if not sql_path.is_absolute():
        sql_path = Path(__file__).parent / sql_path
    if not sql_path.exists():
        raise SystemExit(f"File not found: {sql_path}")

    ref = _project_ref()
    print(f"Applying {sql_path.name} to project {ref}...")
    run_sql(sql_path.read_text())
    print("Done.")


if __name__ == "__main__":
    main()
