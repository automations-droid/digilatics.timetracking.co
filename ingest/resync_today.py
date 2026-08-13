"""Admin: re-pull ClickUp + Meet for today (ET) for everyone, upsert + reconcile DB."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sheets_refs import load_employees

log = logging.getLogger(__name__)
TZ = ZoneInfo("America/New_York")


def _today_et() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _all_user_emails() -> list[str]:
    """Union of dashboard users.json + employee workbook emails."""
    emails: set[str] = set()
    try:
        import json
        import os

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "users.json")
        path = os.path.normpath(path)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = json.load(f) or {}
            for e in raw:
                if e and "@" in e:
                    emails.add(str(e).lower().strip())
    except Exception:
        log.exception("failed reading users.json for resync")

    try:
        email_name, _, _, _ = load_employees()
        for e in email_name:
            if e and "@" in e:
                emails.add(str(e).lower().strip())
    except Exception:
        log.exception("failed reading employees for resync")

    return sorted(emails)


def run_resync_today_everyone(*, day: str | None = None) -> dict:
    """
    Pull today's ClickUp time + Google Meet for all known users into Postgres.
    Reconciles stale same-day rows for those users.
    """
    day = day or _today_et()
    emails = _all_user_emails()
    if not emails:
        return {"ok": False, "day": day, "error": "no users found"}

    from ingest.clickup import run_sync_today
    from ingest.meet import run_sync as meet_run_sync

    clickup = run_sync_today(user_emails=emails, day=day)
    meet = meet_run_sync(days=[day], user_emails=emails)

    return {
        "ok": True,
        "day": day,
        "users": len(emails),
        "clickup": {
            "rowsUpserted": clickup.get("rowsUpserted"),
            "rowsDeleted": clickup.get("rowsDeleted"),
            "entriesFetched": clickup.get("entriesFetched"),
            "rowsBuilt": clickup.get("rowsBuilt"),
        },
        "meet": {
            "rowsInserted": meet.get("rowsInserted"),
            "rowsDeleted": meet.get("rowsDeleted"),
            "totalNewMeetings": meet.get("totalNewMeetings"),
            "usersProcessed": meet.get("usersProcessed"),
            "usersErrored": meet.get("usersErrored"),
        },
    }
