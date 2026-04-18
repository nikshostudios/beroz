#!/usr/bin/env python3
# Run from the el-paso project root:
#   cd /Users/shohamshree/conductor/workspaces/recruitment-agents/el-paso
#   pip3 install browser-cookie3   # first time only
#   python3 scripts/refresh_foundit_cookie.py --railway
"""
Foundit Cookie Refresh Script
==============================
Reads the Foundit session cookie directly from Chrome's local cookie store
(no DevTools, no copy-pasting) and updates it in the local .env file.
Optionally pushes to Railway if the Railway CLI is installed.

Usage:
    python scripts/refresh_foundit_cookie.py              # update .env only
    python scripts/refresh_foundit_cookie.py --railway    # update .env + push to Railway

Prerequisites:
    - Chrome must be OPEN and you must be LOGGED INTO recruiter.foundit.sg
    - pip install browser-cookie3
    - (optional) Railway CLI installed and logged in: https://docs.railway.app/develop/cli

How it works:
    Chrome stores cookies in a local SQLite database, encrypted with your
    macOS Keychain. browser-cookie3 reads and decrypts them — same result
    as copying from DevTools Network tab, just automated.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV_FILE = REPO_ROOT / ".env"

# Fallback: if running from repo root directly
if not ENV_FILE.exists():
    ENV_FILE = Path.cwd() / ".env"
FOUNDIT_DOMAIN = "recruiter.foundit.sg"

# These are the cookies Foundit uses for session auth.
# Order matters — match the format the portal sends.
REQUIRED_COOKIES = ["C", "csrftoken", "django_language"]


def find_all_chrome_profiles() -> list[Path]:
    """Return paths to all Chrome profile cookie files on macOS."""
    chrome_base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    chromium_base = Path.home() / "Library" / "Application Support" / "Chromium"

    cookie_files = []
    for base in [chrome_base, chromium_base]:
        if not base.exists():
            continue
        for entry in base.iterdir():
            if entry.name in ("Default",) or entry.name.startswith("Profile"):
                cookie_path = entry / "Cookies"
                if cookie_path.exists():
                    cookie_files.append(cookie_path)
    return cookie_files


def get_chrome_cookies() -> dict[str, str]:
    """Read Foundit cookies from Chrome's local cookie store.
    Scans all Chrome profiles to find the one with the Foundit session."""
    try:
        import browser_cookie3
    except ImportError:
        print("ERROR: browser-cookie3 not installed.")
        print("Run: pip3 install browser-cookie3")
        sys.exit(1)

    profiles = find_all_chrome_profiles()
    if not profiles:
        print("ERROR: No Chrome profiles found on this machine.")
        sys.exit(1)

    print(f"Found {len(profiles)} Chrome profile(s) — scanning for Foundit cookies...")

    best_cookies = {}
    found_in = None

    for cookie_file in profiles:
        profile_name = cookie_file.parent.name
        try:
            jar = browser_cookie3.chrome(
                cookie_file=str(cookie_file),
                domain_name=FOUNDIT_DOMAIN,
            )
            cookies = {c.name: c.value for c in jar}
            if "C" in cookies:
                print(f"  ✅ Profile '{profile_name}': found C= session cookie")
                best_cookies = cookies
                found_in = profile_name
                break  # Use first profile that has the session cookie
            else:
                non_session = [k for k in cookies if k not in ("C",)]
                if non_session:
                    print(f"  ℹ️  Profile '{profile_name}': has {len(cookies)} Foundit cookies but no session (C=) — not logged in here")
                else:
                    print(f"  —  Profile '{profile_name}': no Foundit cookies")
        except Exception as e:
            print(f"  ⚠️  Profile '{profile_name}': could not read ({e})")

    if not best_cookies or "C" not in best_cookies:
        print("\nERROR: No Chrome profile has an active Foundit session.")
        print("Please open Chrome, go to recruiter.foundit.sg, and log in as the ExcelTech recruiter account.")
        print("Then run this script again.")
        sys.exit(1)

    print(f"\nUsing profile: '{found_in}'")
    return best_cookies


def build_cookie_header(cookies: dict[str, str]) -> str:
    """Build the full Cookie header string from individual cookies."""
    # Put the important session cookies first, then everything else
    ordered = []
    for key in REQUIRED_COOKIES:
        if key in cookies:
            ordered.append(f"{key}={cookies[key]}")

    for key, val in cookies.items():
        if key not in REQUIRED_COOKIES:
            ordered.append(f"{key}={val}")

    return "; ".join(ordered)


def update_env_file(cookie_str: str) -> None:
    """Replace the FOUNDIT_SESSION_COOKIE value in .env."""
    if not ENV_FILE.exists():
        print(f"ERROR: .env file not found at {ENV_FILE}")
        sys.exit(1)

    content = ENV_FILE.read_text()

    # Match the FOUNDIT_SESSION_COOKIE line (may span to end of line)
    pattern = r"^(FOUNDIT_SESSION_COOKIE=).*$"
    replacement = f"FOUNDIT_SESSION_COOKIE={cookie_str}"

    if re.search(pattern, content, flags=re.MULTILINE):
        new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
        ENV_FILE.write_text(new_content)
        print(f"✅ Updated FOUNDIT_SESSION_COOKIE in {ENV_FILE}")
    else:
        # Key not found — append it
        with open(ENV_FILE, "a") as f:
            f.write(f"\n{replacement}\n")
        print(f"✅ Added FOUNDIT_SESSION_COOKIE to {ENV_FILE}")


def push_to_railway(cookie_str: str) -> None:
    """Update the Railway environment variable via the CLI."""
    print("Pushing to Railway...")

    # Check railway CLI is available
    result = subprocess.run(["which", "railway"], capture_output=True)
    if result.returncode != 0:
        print("WARNING: Railway CLI not found. Install from https://docs.railway.app/develop/cli")
        print("Skipping Railway push — .env updated locally only.")
        return

    # Set the variable on Railway (targets the current linked project/service)
    cmd = ["railway", "variables", "set", f"FOUNDIT_SESSION_COOKIE={cookie_str}"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        print("✅ Railway env var updated")
        print("   Service will redeploy automatically.")
    else:
        print(f"ERROR: Railway CLI failed: {result.stderr}")
        print("You may need to run 'railway link' first to connect to your project.")


def validate_cookie(cookies: dict[str, str]) -> None:
    """Check that the critical session cookie is present."""
    if "C" not in cookies:
        print("WARNING: The 'C' session cookie was not found.")
        print("This usually means you're not logged into recruiter.foundit.sg in Chrome.")
        print("Please log in and run this script again.")
        sys.exit(1)

    c_val = cookies["C"]
    print(f"✅ Found session cookie: C={c_val[:8]}...{c_val[-4:]} ({len(c_val)} chars)")
    print(f"   Found {len(cookies)} total cookies for {FOUNDIT_DOMAIN}")


def main():
    parser = argparse.ArgumentParser(
        description="Refresh the Foundit session cookie from Chrome and update .env / Railway."
    )
    parser.add_argument(
        "--railway",
        action="store_true",
        help="Also push the updated cookie to Railway env vars (requires Railway CLI)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cookie string but don't write anything",
    )
    args = parser.parse_args()

    print(f"Reading Foundit cookies from Chrome for domain: {FOUNDIT_DOMAIN}")
    cookies = get_chrome_cookies()
    validate_cookie(cookies)

    cookie_str = build_cookie_header(cookies)

    if args.dry_run:
        print(f"\n--- Cookie string (dry run, not saved) ---")
        print(cookie_str[:120] + "..." if len(cookie_str) > 120 else cookie_str)
        return

    update_env_file(cookie_str)

    if args.railway:
        push_to_railway(cookie_str)
    else:
        print("\nTip: run with --railway to also push to Railway automatically.")
        print("     You'll still need to redeploy the ai_agents service manually,")
        print("     or trigger it via the Railway dashboard.")


if __name__ == "__main__":
    main()
