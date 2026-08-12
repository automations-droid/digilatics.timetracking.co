"""Google Calendar / Meet sync → Postgres (domain-wide delegation)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

from db import (
    delete_stale_meeting_entries,
    existing_entry_ids,
    get_session,
    init_db,
    record_sync_run,
    upsert_entries,
)
from matching import build_match_index, match_client_meet
from sheets_refs import allowed_client_names, load_employees, load_master_clients

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
ADMIN_EMAIL = os.getenv("GOOGLE_ADMIN_EMAIL", "ambreen@digilatics.com")
DOMAIN = os.getenv("ALLOWED_DOMAIN", "digilatics.com")
SINGLE_DAY = os.getenv("MEET_SINGLE_DAY", "").strip()  # YYYY-MM-DD or empty = lookback mode
MEET_LOOKBACK_DAYS = int(os.getenv("MEET_LOOKBACK_DAYS", "31"))
MEET_START_DATE = os.getenv("MEET_START_DATE", "").strip()
MEET_END_DATE = os.getenv("MEET_END_DATE", "").strip()
TZ = ZoneInfo("America/New_York")

EXCLUDE_EMAILS = {
    e.strip().lower()
    for e in os.getenv("MEET_EXCLUDE_EMAILS", "khurram@digilatics.com,growth@digilatics.com").split(",")
    if e.strip()
}

SKIP_KEYWORDS = [
    "out of office", "ooo", "afk", "away", "unavailable", "not available",
    "leave", "holiday", "vacation", "pto", "day off", "off today", "off duty",
    "wfh", "sick", "busy",
    "prayer", "namaz", "fajr", "fajar", "zuhr", "zohr", "asr", "maghrib",
    "isha", "jumma", "jummah", "jumu'ah", "tahajjud",
    "breakfast", "lunch", "dinner", "coffee break", "tea break", "snack", "break",
    "commute", "travel", "flight", "doctor", "dentist", "appointment", "gym",
    "workout", "school run", "pickup", "drop off", "haircut", "errand", "personal",
]

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
]


def should_skip(title: str) -> bool:
    lower = (title or "").lower()
    return any(k in lower for k in SKIP_KEYWORDS)


def delegated_creds(subject: str):
    path = SA_FILE if os.path.isabs(SA_FILE) else os.path.join(HERE, SA_FILE)
    return service_account.Credentials.from_service_account_file(
        path, scopes=SCOPES, subject=subject
    )


def list_users() -> list[dict]:
    svc = build(
        "admin",
        "directory_v1",
        credentials=delegated_creds(ADMIN_EMAIL),
        cache_discovery=False,
    )
    users = []
    page_token = None
    while True:
        resp = (
            svc.users()
            .list(domain=DOMAIN, maxResults=500, pageToken=page_token)
            .execute()
        )
        users.extend(resp.get("users") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return users


def fetch_events_for_user(email: str, time_min: str, time_max: str) -> list[dict]:
    svc = build(
        "calendar",
        "v3",
        credentials=delegated_creds(email),
        cache_discovery=False,
    )
    events = []
    page_token = None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(resp.get("items") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def processing_days() -> list[str]:
    """Days to sync (ET). SINGLE_DAY overrides; else explicit range or lookback."""
    if SINGLE_DAY:
        return [SINGLE_DAY]
    if MEET_START_DATE and MEET_END_DATE:
        d0 = datetime.strptime(MEET_START_DATE, "%Y-%m-%d").date()
        d1 = datetime.strptime(MEET_END_DATE, "%Y-%m-%d").date()
        if d0 > d1:
            d0, d1 = d1, d0
        out: list[str] = []
        cur = d0
        while cur <= d1:
            out.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return out
    today = datetime.now(TZ).date()
    return [
        (today - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(MEET_LOOKBACK_DAYS)
    ][::-1]


def day_bounds_iso(day: str) -> tuple[str, str]:
    # Mirror n8n: interpret day as ET midnight bounds via fixed -05:00 offset.
    # Good enough for sync windows; DST edge cases match prior n8n behavior.
    start = datetime.fromisoformat(f"{day}T00:00:00-05:00")
    end = datetime.fromisoformat(f"{day}T23:59:59-05:00")
    return start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), end.astimezone(
        timezone.utc
    ).isoformat().replace("+00:00", "Z")


def _sync_day(
    day_et: str,
    *,
    eligible: list[dict],
    existing_ids: set[str],
    clients,
    index,
    email_name,
    email_fallback,
    email_team,
    allowed_clients,
    reconcile: bool = False,
) -> tuple[list[dict], dict, list[str], set[str]]:
    time_min, time_max = day_bounds_iso(day_et)
    all_meetings: list[dict] = []
    found_entry_ids: set[str] = set()
    user_stats = {"processed": len(eligible), "errored": 0, "withEvents": 0}
    errors: list[str] = []

    def process_user(u: dict) -> tuple[list[dict], set[str], bool, str | None]:
        email = u["primaryEmail"]
        local_rows: list[dict] = []
        local_found: set[str] = set()
        try:
            events = fetch_events_for_user(email, time_min, time_max)
            had = len(events) > 0
            for event in events:
                if event.get("status") == "cancelled":
                    continue
                attendees = event.get("attendees") or []
                attendee = next((a for a in attendees if a.get("email") == email), None)
                organizer = (event.get("organizer") or {}).get("email")
                accepted = (
                    not attendee
                    or attendee.get("responseStatus") in ("accepted",)
                    or organizer == email
                )
                if not accepted:
                    continue
                start = (event.get("start") or {}).get("dateTime")
                end = (event.get("end") or {}).get("dateTime")
                if not start:
                    continue
                summary = event.get("summary") or ""
                if should_skip(summary):
                    continue
                entry_id = f"{event.get('id')}_{email}"
                local_found.add(entry_id)
                if not reconcile and entry_id in existing_ids:
                    continue
                attendee_emails = [a.get("email") for a in attendees if a.get("email")]
                user_fallback = email_fallback.get(email.lower()) or "Digilatics"
                match = match_client_meet(
                    title=summary,
                    attendee_emails=attendee_emails,
                    user_fallback=user_fallback,
                    clients=clients,
                    index=index,
                    user_team=email_team.get(email.lower()) or "",
                    user_email=email.lower(),
                    allowed_clients=allowed_clients,
                )
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                duration_ms = (end_dt - start_dt).total_seconds() * 1000
                event_date = start_dt.astimezone(TZ).strftime("%Y-%m-%d")
                local_rows.append(
                    {
                        "date": event_date,
                        "client": match["client"],
                        "task": summary,
                        "user": email_name.get(email.lower()) or email,
                        "hours": round(duration_ms / 3600000, 2),
                        "minutes": round(duration_ms / 60000),
                        "source": "meeting",
                        "url": "",
                        "entryId": entry_id,
                        "space": "-",
                        "team": email_team.get(email.lower()) or "Unknown",
                        "matchVia": match.get("via"),
                    }
                )
            return local_rows, local_found, had, None
        except Exception as e:
            return [], set(), False, f"{email}: {e}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(process_user, u) for u in eligible]
        for fut in as_completed(futures):
            rows, day_found, had, err = fut.result()
            found_entry_ids.update(day_found)
            if err:
                user_stats["errored"] += 1
                if len(errors) < 10:
                    errors.append(err)
            if had:
                user_stats["withEvents"] += 1
            all_meetings.extend(rows)

    return all_meetings, user_stats, errors, found_entry_ids


def run_sync(
    *,
    days: list[str] | None = None,
    user_emails: list[str] | None = None,
    user_display_names: set[str] | None = None,
) -> dict:
    started = datetime.now(timezone.utc)
    init_db()
    days = days or processing_days()
    reconcile = bool(user_emails)

    clients, master_err = load_master_clients()
    email_name, email_fallback, email_team, emp_err = load_employees()
    index = build_match_index(clients)
    allowed_clients = allowed_client_names(clients, email_fallback)

    session = get_session()
    try:
        existing_ids = existing_entry_ids(session)
    finally:
        session.close()

    users = list_users()
    eligible = [
        u
        for u in users
        if u.get("primaryEmail") and u["primaryEmail"].lower() not in EXCLUDE_EMAILS
    ]
    if user_emails:
        allowed = {e.lower().strip() for e in user_emails if e}
        eligible = [u for u in eligible if u.get("primaryEmail", "").lower() in allowed]

    all_meetings: list[dict] = []
    all_found_ids: set[str] = set()
    day_stats: list[dict] = []
    total_errored = 0
    total_with_events = 0
    errors: list[str] = []

    for day_et in days:
        day_rows, ustats, day_errors, day_found = _sync_day(
            day_et,
            eligible=eligible,
            existing_ids=existing_ids,
            clients=clients,
            index=index,
            email_name=email_name,
            email_fallback=email_fallback,
            email_team=email_team,
            allowed_clients=allowed_clients,
            reconcile=reconcile,
        )
        all_found_ids.update(day_found)
        for r in day_rows:
            existing_ids.add(r["entryId"])
        all_meetings.extend(day_rows)
        total_errored += ustats["errored"]
        total_with_events = max(total_with_events, ustats["withEvents"])
        day_stats.append(
            {"day": day_et, "newRows": len(day_rows), "calendarRows": len(day_found), "errors": len(day_errors)}
        )
        errors.extend(day_errors)

    seen: set[str] = set()
    rows: list[dict] = []
    for m in all_meetings:
        if m["entryId"] in seen:
            continue
        seen.add(m["entryId"])
        rows.append(m)

    breakdown: dict[str, int] = {}
    for r in rows:
        via = r.get("matchVia") or "unknown"
        breakdown[via] = breakdown.get(via, 0) + 1

    debug = {
        "processingDays": days,
        "userEmailsFilter": user_emails,
        "dayStats": day_stats,
        "lookbackDays": MEET_LOOKBACK_DAYS if not SINGLE_DAY and not (MEET_START_DATE and MEET_END_DATE) else None,
        "singleDayMode": bool(SINGLE_DAY),
        "clientsLoaded": len(clients),
        "masterLoadError": master_err,
        "employeeLoadError": emp_err,
        "emailTeamMapSize": len(email_team),
        "emailFallbackMapSize": len(email_fallback),
        "emailNameMapSize": len(email_name),
        "totalUsers": len(users),
        "usersProcessed": len(eligible),
        "usersWithEvents": total_with_events,
        "usersErrored": total_errored,
        "errorsSample": errors[:10],
        "existingIdsCount": len(existing_ids),
        "calendarEntryIds": len(all_found_ids),
        "totalNewMeetings": len(rows),
        "matchBreakdown": breakdown,
        "sampleUnmatched": [r["task"] for r in rows if r.get("matchVia") == "fallback"][:10],
        "distinctUsersInOutput": len({r["user"] for r in rows}),
    }

    session = get_session()
    try:
        inserted = upsert_entries(session, rows)
        user_names = user_display_names
        if user_names is None and user_emails:
            user_names = {
                (email_name.get(e.lower().strip()) or e.lower().strip())
                for e in user_emails
                if e and e.strip()
            }
        deleted = delete_stale_meeting_entries(
            session,
            days=days,
            valid_entry_ids=all_found_ids,
            user_names=user_names,
        )
        record_sync_run(
            session,
            job="meet",
            rows_inserted=inserted,
            status="ok",
            debug={**debug, "rowsDeleted": deleted},
            started_at=started,
        )
    except Exception as e:
        session.rollback()
        record_sync_run(
            session,
            job="meet",
            rows_inserted=0,
            status="error",
            debug={**debug, "error": str(e)},
            started_at=started,
        )
        raise
    finally:
        session.close()

    debug["rowsInserted"] = inserted
    debug["rowsDeleted"] = deleted
    return debug


if __name__ == "__main__":
    print(run_sync())
