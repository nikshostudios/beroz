"""Cron job runners for ExcelTech AI Agent Layer."""

import asyncio
import os
from datetime import datetime
from pathlib import Path

from config import db, outlook
from config.db import get_client


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _cron_log(msg: str):
    """Append a line to today's cron log."""
    log_file = LOG_DIR / f"cron_{datetime.now().strftime('%Y%m%d')}.log"
    ts = datetime.now().isoformat(timespec="seconds")
    with open(log_file, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def _get_recruiter_emails() -> list[str]:
    """Load active recruiter emails from env var or Supabase."""
    env_val = os.environ.get("RECRUITER_EMAILS", "")
    if env_val.strip():
        return [e.strip() for e in env_val.split(",") if e.strip()]

    try:
        rows = (get_client().table("portal_credentials")
                .select("email")
                .eq("portal", "outlook")
                .eq("active", True)
                .execute().data)
        return [r["email"] for r in rows if r.get("email")]
    except Exception:
        return []


async def _process_inbox_for_recruiter(email: str, process_fn) -> dict:
    """Process inbox for a single recruiter, catching errors."""
    try:
        result = await process_fn(recruiter_email=email)
        _cron_log(f"OK  {email}: {result}")
        return {"email": email, "status": "ok", "result": result}
    except Exception as e:
        _cron_log(f"ERR {email}: {e}")
        return {"email": email, "status": "error", "error": str(e)}


async def run_inbox_scan(process_fn) -> dict:
    """Batch-scan all recruiter inboxes in parallel.

    Args:
        process_fn: async callable(recruiter_email=str) -> dict
                    (the _run_process_inbox function from main.py)
    """
    emails = _get_recruiter_emails()
    if not emails:
        _cron_log("SKIP: no recruiter emails configured")
        return {"skipped": True, "reason": "no recruiter emails"}

    _cron_log(f"START inbox scan for {len(emails)} recruiters")

    results = await asyncio.gather(
        *[_process_inbox_for_recruiter(e, process_fn) for e in emails],
    )

    ok = sum(1 for r in results if r["status"] == "ok")
    errs = sum(1 for r in results if r["status"] == "error")
    summary = {
        "recruiters_scanned": len(emails),
        "ok": ok,
        "errors": errs,
        "details": results,
    }
    _cron_log(f"DONE inbox scan: {ok} ok, {errs} errors")
    return summary


def setup_scheduler(scheduler, process_fn, sequence_tick_fn=None):
    """Configure APScheduler with cron jobs.

    Args:
        scheduler: AsyncIOScheduler instance
        process_fn: the _run_process_inbox coroutine from main.py
        sequence_tick_fn: optional callable for sequence_tick (sync)
    """
    async def _job():
        await run_inbox_scan(process_fn)

    scheduler.add_job(
        _job,
        "interval",
        minutes=15,
        id="inbox_cron",
        misfire_grace_time=60,
        max_instances=1,
    )

    if sequence_tick_fn:
        def _tick_job():
            try:
                result = sequence_tick_fn(user_role=None)
                _cron_log(f"sequence_tick OK: {result}")
            except Exception as e:
                _cron_log(f"sequence_tick ERR: {e}")

        scheduler.add_job(
            _tick_job,
            "interval",
            minutes=5,
            id="sequence_tick_cron",
            misfire_grace_time=60,
            max_instances=1,
        )
