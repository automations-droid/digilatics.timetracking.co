"""Lightweight today's time stats — live ClickUp + Meet rows from DB (synced every 30m)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

from sheets_refs import load_employees

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "25658037")
LIVE_CACHE_TTL = int(os.getenv("LIVE_TODAY_CACHE_SECONDS", "1800"))  # 30 minutes
TZ = ZoneInfo("America/New_York")

_cache: dict = {"ts": 0.0, "day": "", "clickup": None, "meet": None}
_user_live_cache: dict[str, dict] = {}  # email -> last Re-run Today pull (live APIs only)


def today_et() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def today_bounds_ms() -> tuple[int, int]:
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def fetch_clickup_today_raw(*, user_email: str | None = None) -> list[dict]:
    """ClickUp pull for today — duration + user only (no task/client matching).

    When user_email is set, only that assignee is queried (Re-run Today).
    """
    if not CLICKUP_TOKEN:
        return []
    start_ms, end_ms = today_bounds_ms()
    headers = {"Authorization": CLICKUP_TOKEN}
    with httpx.Client(timeout=45.0) as client:
        team = client.get(
            f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}",
            headers=headers,
        )
        team.raise_for_status()
        members = (team.json().get("team") or {}).get("members") or []
        if user_email:
            target = user_email.lower().strip()
            assignee_ids = [
                str(m["user"]["id"])
                for m in members
                if m.get("user")
                and m["user"].get("id")
                and (m["user"].get("email") or "").lower() == target
            ]
            if not assignee_ids:
                return []
            assignees = assignee_ids[0]
        else:
            assignees = ",".join(
                str(m["user"]["id"]) for m in members if m.get("user") and m["user"].get("id")
            )
        res = client.get(
            f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}/time_entries",
            params={"start_date": start_ms, "end_date": end_ms, "assignee": assignees},
            headers=headers,
        )
        res.raise_for_status()
        entries = res.json().get("data") or []

    rows: list[dict] = []
    for item in entries:
        dur_ms = int(item.get("duration") or 0)
        if dur_ms <= 0:
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        email = (user.get("email") or "").lower()
        name = (user.get("username") or email or "Unknown").strip()
        rows.append(
            {
                "email": email,
                "user": name,
                "minutes": round(dur_ms / 60000),
                "source": "clickup",
            }
        )
    return rows


def fetch_meet_today_db(*, profile: dict | None = None) -> list[dict]:
    from sqlalchemy import select

    from db import TimeEntry, get_session, init_db

    init_db()
    day = today_et()
    session = get_session()
    try:
        q = session.execute(
            select(TimeEntry).where(TimeEntry.date == day, TimeEntry.source == "meeting")
        ).scalars()
        rows = [
            {"user": (r.user or "").strip(), "minutes": int(r.minutes or 0), "source": "meeting"}
            for r in q
        ]
    finally:
        session.close()

    if profile:
        idents = {str(i).lower().strip() for i in profile.get("identities") or []}
        email = (profile.get("email") or "").lower()
        email_name, _, _, _ = load_employees()
        display = (email_name.get(email) or "").lower()
        if display:
            idents.add(display)
        rows = [r for r in rows if str(r.get("user") or "").lower() in idents]
    return rows


def fetch_meet_today_live(*, user_email: str) -> list[dict]:
    """Live Google Calendar meetings for today — logged-in user only."""
    from ingest.meet import day_bounds_iso, fetch_events_for_user, should_skip

    day = today_et()
    email = user_email.lower().strip()
    email_name, _, _, _ = load_employees()
    display = email_name.get(email) or email

    try:
        time_min, time_max = day_bounds_iso(day)
        events = fetch_events_for_user(email, time_min, time_max)
    except Exception:
        return []

    rows: list[dict] = []
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
        if not start or not end:
            continue
        if should_skip(event.get("summary") or ""):
            continue
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        minutes = round((end_dt - start_dt).total_seconds() / 60)
        if minutes <= 0:
            continue
        rows.append({"user": display, "minutes": minutes, "source": "meeting"})
    return rows


def _profile_db_names(profile: dict) -> set[str]:
    """All User-column values that may represent this person in Postgres."""
    email = (profile.get("email") or "").lower().strip()
    email_name, _, _, _ = load_employees()
    names: set[str] = set()
    if email:
        names.add(email)
        display = email_name.get(email)
        if display:
            names.add(display)
    for ident in profile.get("identities") or []:
        s = str(ident or "").strip()
        if s:
            names.add(s)
    if profile.get("name"):
        names.add(str(profile["name"]).strip())
    return {n for n in names if n}


def get_live_today_rows(
    *, force: bool = False, profile: dict | None = None
) -> tuple[list[dict], list[dict], str, str]:
    """Returns (clickup_rows, meet_rows, clickup_source, meet_source). Cached 30m by default."""
    global _cache, _user_live_cache
    day = today_et()
    now = time.time()
    user_email = ((profile or {}).get("email") or "").lower().strip() or None

    # Re-run Today: live ClickUp + Calendar only — no Claude, no DB sync.
    if force and user_email and profile:
        clickup = fetch_clickup_today_raw(user_email=user_email)
        meet = fetch_meet_today_live(user_email=user_email)
        _user_live_cache[user_email] = {
            "ts": now,
            "day": day,
            "clickup": clickup,
            "meet": meet,
        }
        return clickup, meet, "live", "live"

    # After a recent Re-run Today, keep showing that live pull for this user.
    if user_email and profile:
        cached_user = _user_live_cache.get(user_email)
        if (
            cached_user
            and cached_user.get("day") == day
            and now - float(cached_user.get("ts") or 0) < LIVE_CACHE_TTL
        ):
            return (
                cached_user["clickup"],
                cached_user["meet"],
                "live",
                "live",
            )

    if (
        not force
        and _cache.get("day") == day
        and _cache.get("clickup") is not None
        and _cache.get("meet") is not None
        and now - float(_cache.get("ts") or 0) < LIVE_CACHE_TTL
    ):
        return _cache["clickup"], _cache["meet"], "cache", "db"

    meet_source = "db"
    clickup = fetch_clickup_today_raw()
    meet = fetch_meet_today_db()
    clickup_source = "live"
    _cache.update(ts=now, day=day, clickup=clickup, meet=meet)
    return clickup, meet, clickup_source, meet_source


def scope_live_rows_own(clickup_rows: list[dict], meet_rows: list[dict], profile: dict) -> tuple[list[dict], list[dict]]:
    idents = {str(i).lower().strip() for i in profile.get("identities") or []}
    email = (profile.get("email") or "").lower()
    cu = [
        r
        for r in clickup_rows
        if r.get("email") == email or str(r.get("user") or "").lower() in idents
    ]
    mt = [r for r in meet_rows if str(r.get("user") or "").lower() in idents]
    return cu, mt


def scope_live_rows(clickup_rows: list[dict], meet_rows: list[dict], profile: dict) -> tuple[list[dict], list[dict]]:
    role = profile.get("role") or "employee"
    if role == "admin":
        return clickup_rows, meet_rows

    idents = {str(i).lower().strip() for i in profile.get("identities") or []}
    email = (profile.get("email") or "").lower()
    teams = {t.lower() for t in profile.get("teams") or []}

    email_name, _, email_team, _ = load_employees()
    name_to_email = {n.lower(): e for e, n in email_name.items() if n}

    if role == "lead":
        team_emails = {e for e, t in email_team.items() if (t or "").lower() in teams}
        team_emails.add(email)
        team_names = {email_name.get(e, "").lower() for e in team_emails if email_name.get(e)}
        team_names |= idents

        cu = [
            r
            for r in clickup_rows
            if r.get("email") in team_emails or str(r.get("user") or "").lower() in team_names
        ]
        mt = [
            r
            for r in meet_rows
            if str(r.get("user") or "").lower() in team_names
            or name_to_email.get(str(r.get("user") or "").lower(), "") in team_emails
        ]
        return cu, mt

    cu = [
        r
        for r in clickup_rows
        if r.get("email") == email or str(r.get("user") or "").lower() in idents
    ]
    mt = [r for r in meet_rows if str(r.get("user") or "").lower() in idents]
    return cu, mt


def aggregate_stats(clickup_rows: list[dict], meet_rows: list[dict]) -> dict:
    task_min = sum(int(r.get("minutes") or 0) for r in clickup_rows)
    meet_min = sum(int(r.get("minutes") or 0) for r in meet_rows)
    total_min = task_min + meet_min
    return {
        "taskMinutes": task_min,
        "meetMinutes": meet_min,
        "totalMinutes": total_min,
        "taskHours": round(task_min / 60, 2),
        "meetHours": round(meet_min / 60, 2),
        "totalHours": round(total_min / 60, 2),
        "taskEntries": len(clickup_rows),
        "meetEntries": len(meet_rows),
        "entries": len(clickup_rows) + len(meet_rows),
    }


def last_sync_at() -> str | None:
    from sqlalchemy import select

    from db import SyncRun, get_session

    session = get_session()
    try:
        run = session.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(1)).scalar_one_or_none()
        if run and run.finished_at:
            return run.finished_at.astimezone(TZ).isoformat()
        return None
    finally:
        session.close()


def live_today_payload(profile: dict, *, own_only: bool = True, force: bool = False) -> dict:
    clickup, meet, clickup_src, meet_src = get_live_today_rows(force=force, profile=profile)
    if own_only:
        cu, mt = scope_live_rows_own(clickup, meet, profile)
    else:
        cu, mt = scope_live_rows(clickup, meet, profile)
    stats = aggregate_stats(cu, mt)
    return {
        "date": today_et(),
        "timezone": "America/New_York",
        **stats,
        "clickupSource": clickup_src,
        "meetSource": meet_src,
        "refreshed": force,
        "refreshSeconds": LIVE_CACHE_TTL,
        "lastSyncAt": last_sync_at(),
    }
