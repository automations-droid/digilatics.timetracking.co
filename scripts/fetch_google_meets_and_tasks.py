#!/usr/bin/env python3
"""
Fetch Google Calendar (Meet) events, Google Tasks, and ClickUp time entries.

Uses the same service-account domain-wide delegation as ingest/meet.py:
  - GOOGLE_SERVICE_ACCOUNT_FILE
  - GOOGLE_ADMIN_EMAIL
  - ALLOWED_DOMAIN

ClickUp (requires CLICKUP_API_TOKEN, CLICKUP_TEAM_ID in .env):
  - Team time entries for the selected date range

Required domain-wide delegation scopes (add in Google Admin → Security → API controls):
  - https://www.googleapis.com/auth/calendar.readonly
  - https://www.googleapis.com/auth/admin.directory.user.readonly
  - https://www.googleapis.com/auth/tasks.readonly

Env (optional):
  FETCH_START_DATE=2026-08-01   # YYYY-MM-DD, America/New_York
  FETCH_END_DATE=2026-08-11
  FETCH_LOOKBACK_DAYS=31          # used when start/end not set
  FETCH_EXCLUDE_EMAILS=khurram@digilatics.com,growth@digilatics.com
  FETCH_OUTPUT_DIR=./exports
  FETCH_WORKERS=6

Usage:
  cd /path/to/project
  python scripts/fetch_google_meets_and_tasks.py
  python scripts/fetch_google_meets_and_tasks.py --email ahmar@digilatics.com --start 2026-06-01 --end 2026-07-31 --skip-clickup
  python scripts/fetch_google_meets_and_tasks.py --meetings-only
  python scripts/fetch_google_meets_and_tasks.py --tasks-only
  python scripts/fetch_google_meets_and_tasks.py --clickup-only
  python scripts/fetch_google_meets_and_tasks.py --start 2026-08-01 --end 2026-08-11
  python scripts/pull_clickup_and_meet.py   # sync ClickUp + Meet → Postgres
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

TZ = ZoneInfo(os.getenv("FETCH_TIMEZONE", "America/New_York"))
HERE = str(ROOT)
SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
ADMIN_EMAIL = os.getenv("GOOGLE_ADMIN_EMAIL", "ambreen@digilatics.com")
DOMAIN = os.getenv("ALLOWED_DOMAIN", "digilatics.com")
LOOKBACK_DAYS = int(os.getenv("FETCH_LOOKBACK_DAYS", os.getenv("MEET_LOOKBACK_DAYS", "31")))
START_DATE = os.getenv("FETCH_START_DATE", os.getenv("MEET_START_DATE", "")).strip()
END_DATE = os.getenv("FETCH_END_DATE", os.getenv("MEET_END_DATE", "")).strip()
OUTPUT_DIR = Path(os.getenv("FETCH_OUTPUT_DIR", ROOT / "exports"))
WORKERS = int(os.getenv("FETCH_WORKERS", "6"))
EXCLUDE_EMAILS = {
    e.strip().lower()
    for e in os.getenv(
        "FETCH_EXCLUDE_EMAILS",
        os.getenv("MEET_EXCLUDE_EMAILS", "khurram@digilatics.com,growth@digilatics.com"),
    ).split(",")
    if e.strip()
}
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "25658037")

SCOPES_CALENDAR = ["https://www.googleapis.com/auth/calendar.readonly"]
SCOPES_TASKS = ["https://www.googleapis.com/auth/tasks.readonly"]
SCOPES_ADMIN = ["https://www.googleapis.com/auth/admin.directory.user.readonly"]


def delegated_creds(subject: str, scopes: list[str]):
    path = SA_FILE if os.path.isabs(SA_FILE) else os.path.join(HERE, SA_FILE)
    return service_account.Credentials.from_service_account_file(
        path, scopes=scopes, subject=subject
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch Google Meet, Tasks, and ClickUp for export")
    p.add_argument("--start", help="Start date YYYY-MM-DD (ET)")
    p.add_argument("--end", help="End date YYYY-MM-DD (ET)")
    p.add_argument("--lookback", type=int, help=f"Days back from today (default {LOOKBACK_DAYS})")
    p.add_argument("--email", action="append", default=[], help="Limit to one or more emails (repeatable)")
    p.add_argument("--output", type=Path, help="Output directory")
    p.add_argument("--workers", type=int, default=WORKERS)
    p.add_argument("--meetings-only", action="store_true")
    p.add_argument("--tasks-only", action="store_true")
    p.add_argument("--clickup-only", action="store_true")
    p.add_argument("--skip-clickup", action="store_true", help="Skip ClickUp export")
    return p.parse_args()


def resolve_date_range(args: argparse.Namespace) -> tuple[str, str]:
    start_s = (args.start or START_DATE).strip()
    end_s = (args.end or END_DATE).strip()
    if start_s and end_s:
        return start_s, end_s
    lookback = args.lookback if args.lookback is not None else LOOKBACK_DAYS
    today = datetime.now(TZ).date()
    start = today - timedelta(days=max(lookback - 1, 0))
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def range_bounds_iso(start_day: str, end_day: str) -> tuple[str, str]:
    """ET calendar-day bounds → UTC ISO for Calendar API."""
    start = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=TZ)
    end = datetime.strptime(end_day, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=TZ
    )
    return (
        start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def to_et_date(iso_dt: str | None) -> str:
    if not iso_dt:
        return ""
    try:
        dt = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
        return dt.astimezone(TZ).strftime("%Y-%m-%d")
    except ValueError:
        return iso_dt[:10] if len(iso_dt) >= 10 else iso_dt


def list_workspace_users() -> list[dict]:
    svc = build(
        "admin",
        "directory_v1",
        credentials=delegated_creds(ADMIN_EMAIL, SCOPES_ADMIN),
        cache_discovery=False,
    )
    users: list[dict] = []
    page_token = None
    while True:
        resp = (
            svc.users()
            .list(domain=DOMAIN, maxResults=500, pageToken=page_token, orderBy="email")
            .execute()
        )
        users.extend(resp.get("users") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return [
        u
        for u in users
        if u.get("primaryEmail") and u["primaryEmail"].lower() not in EXCLUDE_EMAILS
    ]


def fetch_user_meetings(email: str, time_min: str, time_max: str) -> tuple[list[dict], str | None]:
    rows: list[dict] = []
    try:
        svc = build(
            "calendar",
            "v3",
            credentials=delegated_creds(email, SCOPES_CALENDAR),
            cache_discovery=False,
        )
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
            for ev in resp.get("items") or []:
                start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
                end = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
                attendees = ev.get("attendees") or []
                attendee = next((a for a in attendees if a.get("email") == email), None)
                organizer = (ev.get("organizer") or {}).get("email", "")
                response = (attendee or {}).get("responseStatus") or (
                    "accepted" if organizer == email else "unknown"
                )
                duration_hours = ""
                if start and end and "T" in str(start) and "T" in str(end):
                    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    e = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    duration_hours = round((e - s).total_seconds() / 3600, 2)

                rows.append(
                    {
                        "user_email": email,
                        "event_id": ev.get("id", ""),
                        "date": to_et_date(start if "T" in str(start) else f"{start}T12:00:00"),
                        "summary": ev.get("summary") or "(no title)",
                        "start": start or "",
                        "end": end or "",
                        "duration_hours": duration_hours,
                        "response_status": response,
                        "organizer": organizer,
                        "hangout_link": ev.get("hangoutLink") or "",
                        "html_link": ev.get("htmlLink") or "",
                        "location": ev.get("location") or "",
                        "attendee_count": len(attendees),
                        "attendees": ", ".join(
                            a.get("email", "") for a in attendees if a.get("email")
                        )[:2000],
                        "all_day": "T" not in str(start or ""),
                    }
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return rows, None
    except Exception as e:
        return [], f"{email}: {e}"


def _task_in_range(task: dict, start_day: str, end_day: str) -> bool:
    """Include task if due, completed, or updated falls in [start_day, end_day]."""
    candidates = [
        task.get("due"),
        task.get("completed"),
        task.get("updated"),
    ]
    for raw in candidates:
        if not raw:
            continue
        d = to_et_date(raw if "T" in raw else f"{raw}T12:00:00")
        if d and start_day <= d <= end_day:
            return True
    return False


def fetch_user_tasks(email: str, start_day: str, end_day: str) -> tuple[list[dict], str | None]:
    rows: list[dict] = []
    try:
        svc = build(
            "tasks",
            "v1",
            credentials=delegated_creds(email, SCOPES_TASKS),
            cache_discovery=False,
        )
        tl_token = None
        while True:
            tl_resp = svc.tasklists().list(maxResults=100, pageToken=tl_token).execute()
            for tl in tl_resp.get("items") or []:
                tl_id = tl.get("id", "")
                tl_title = tl.get("title", "")
                t_token = None
                while True:
                    t_resp = (
                        svc.tasks()
                        .list(
                            tasklist=tl_id,
                            showCompleted=True,
                            showHidden=True,
                            maxResults=100,
                            pageToken=t_token,
                        )
                        .execute()
                    )
                    for t in t_resp.get("items") or []:
                        if not _task_in_range(t, start_day, end_day):
                            continue
                        rows.append(
                            {
                                "user_email": email,
                                "tasklist_id": tl_id,
                                "tasklist_title": tl_title,
                                "task_id": t.get("id", ""),
                                "title": t.get("title") or "(no title)",
                                "notes": (t.get("notes") or "")[:500],
                                "status": t.get("status", ""),
                                "due": t.get("due") or "",
                                "due_date": to_et_date(t.get("due")),
                                "completed": t.get("completed") or "",
                                "completed_date": to_et_date(t.get("completed")),
                                "updated": t.get("updated") or "",
                                "updated_date": to_et_date(t.get("updated")),
                                "parent": t.get("parent") or "",
                                "links": json.dumps(t.get("links") or []),
                            }
                        )
                    t_token = t_resp.get("nextPageToken")
                    if not t_token:
                        break
            tl_token = tl_resp.get("nextPageToken")
            if not tl_token:
                break
        return rows, None
    except Exception as e:
        return [], f"{email}: {e}"


def process_user(
    user: dict,
    *,
    time_min: str,
    time_max: str,
    start_day: str,
    end_day: str,
    do_meetings: bool,
    do_tasks: bool,
) -> tuple[list[dict], list[dict], str | None]:
    email = user["primaryEmail"]
    meetings: list[dict] = []
    tasks: list[dict] = []
    errors: list[str] = []

    if do_meetings:
        m_rows, m_err = fetch_user_meetings(email, time_min, time_max)
        meetings.extend(m_rows)
        if m_err:
            errors.append(m_err)

    if do_tasks:
        t_rows, t_err = fetch_user_tasks(email, start_day, end_day)
        tasks.extend(t_rows)
        if t_err:
            errors.append(t_err)

    err = "; ".join(errors) if errors else None
    return meetings, tasks, err


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


COMBINED_FIELDS = [
    "item_type",
    "user_email",
    "date",
    "title",
    "start",
    "end",
    "duration_hours",
    "status",
    "tasklist",
    "notes",
    "organizer",
    "hangout_link",
    "html_link",
    "location",
    "attendee_count",
    "attendees",
    "all_day",
    "due",
    "completed",
    "source_id",
]


def build_combined_rows(meetings: list[dict], tasks: list[dict]) -> list[dict]:
    """One sheet: Google Meet / calendar events + Google Tasks."""
    rows: list[dict] = []
    for m in meetings:
        hangout = (m.get("hangout_link") or "").strip()
        rows.append(
            {
                "item_type": "google_meet" if hangout else "calendar_event",
                "user_email": m.get("user_email", ""),
                "date": m.get("date", ""),
                "title": m.get("summary", ""),
                "start": m.get("start", ""),
                "end": m.get("end", ""),
                "duration_hours": m.get("duration_hours", ""),
                "status": m.get("response_status", ""),
                "tasklist": "",
                "notes": "",
                "organizer": m.get("organizer", ""),
                "hangout_link": hangout,
                "html_link": m.get("html_link", ""),
                "location": m.get("location", ""),
                "attendee_count": m.get("attendee_count", ""),
                "attendees": m.get("attendees", ""),
                "all_day": m.get("all_day", ""),
                "due": "",
                "completed": "",
                "source_id": m.get("event_id", ""),
            }
        )
    for t in tasks:
        rows.append(
            {
                "item_type": "google_task",
                "user_email": t.get("user_email", ""),
                "date": t.get("due_date") or t.get("completed_date") or t.get("updated_date") or "",
                "title": t.get("title", ""),
                "start": t.get("due", ""),
                "end": "",
                "duration_hours": "",
                "status": t.get("status", ""),
                "tasklist": t.get("tasklist_title", ""),
                "notes": t.get("notes", ""),
                "organizer": "",
                "hangout_link": "",
                "html_link": "",
                "location": "",
                "attendee_count": "",
                "attendees": "",
                "all_day": "",
                "due": t.get("due", ""),
                "completed": t.get("completed", ""),
                "source_id": t.get("task_id", ""),
            }
        )
    rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("start") or ""), str(r.get("title") or "")))
    return rows


def write_combined_csv(path: Path, meetings: list[dict], tasks: list[dict]) -> int:
    rows = build_combined_rows(meetings, tasks)
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COMBINED_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def sa_client_id() -> str:
    path = SA_FILE if os.path.isabs(SA_FILE) else os.path.join(HERE, SA_FILE)
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
        return str(info.get("client_id") or "").strip()
    except Exception:
        return ""


def print_tasks_auth_help() -> None:
    cid = sa_client_id() or "(open Cloud Console → IAM → Service accounts → digilatics-calendar-reader → Unique ID)"
    print(
        "\nGoogle Tasks (Calendar → Task tab) need Tasks API access.\n"
        "Calendar API does not return those Tasks — only Tasks API does.\n\n"
        "Fix (Google Admin):\n"
        "  1. Admin console → Security → Access and data control → API controls\n"
        "     → Domain-wide delegation → Manage domain-wide delegation\n"
        "  2. Open the client for this service account and ADD this scope\n"
        "     (keep existing calendar scopes):\n"
        "       https://www.googleapis.com/auth/tasks.readonly\n"
        f"  3. Client ID: {cid}\n"
        "  4. Also enable 'Google Tasks API' in Google Cloud Console for the project.\n"
        "  5. Re-run this script.\n"
    )


def fetch_clickup_time_entries(start_day: str, end_day: str) -> tuple[list[dict], str | None]:
    if not CLICKUP_TOKEN:
        return [], "CLICKUP_API_TOKEN is not set"

    start = datetime.fromisoformat(f"{start_day}T00:00:00-05:00").astimezone(timezone.utc)
    end = datetime.fromisoformat(f"{end_day}T23:59:59-05:00").astimezone(timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    headers = {"Authorization": CLICKUP_TOKEN}

    try:
        with httpx.Client(timeout=60.0) as client:
            team_r = client.get(
                f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}",
                headers=headers,
            )
            team_r.raise_for_status()
            members = (team_r.json().get("team") or {}).get("members") or []
            assignees = ",".join(
                str(m["user"]["id"]) for m in members if m.get("user") and m["user"].get("id")
            )

            space_r = client.get(
                f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}/space",
                params={"archived": "false"},
                headers=headers,
            )
            space_r.raise_for_status()
            space_map = {str(s["id"]): s["name"] for s in (space_r.json().get("spaces") or [])}

            entries_r = client.get(
                f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}/time_entries",
                params={"start_date": start_ms, "end_date": end_ms, "assignee": assignees},
                headers=headers,
            )
            entries_r.raise_for_status()
            entries = entries_r.json().get("data") or []
    except Exception as e:
        return [], f"ClickUp: {e}"

    rows: list[dict] = []
    for item in entries:
        duration_ms = int(item.get("duration") or 0)
        if duration_ms == 0:
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        start_ts = int(item.get("start") or 0)
        rows.append(
            {
                "entry_id": str(item.get("id") or ""),
                "date_et": to_et_date(
                    datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc).isoformat()
                    if start_ts
                    else None
                ),
                "duration_hours": round(duration_ms / 3600000, 4),
                "duration_ms": duration_ms,
                "user_id": str(user.get("id") or ""),
                "user_email": user.get("email") or "",
                "user_name": user.get("username") or user.get("email") or "",
                "task_id": str(task.get("id") or ""),
                "task_name": task.get("name") or "",
                "space_id": str(task.get("space_id") or ""),
                "space_name": space_map.get(str(task.get("space_id") or ""), ""),
                "description": item.get("description") or "",
                "billable": item.get("billable"),
                "source": "clickup",
            }
        )
    return rows, None


def main() -> int:
    args = parse_args()
    only_flags = sum(
        1 for flag in (args.meetings_only, args.tasks_only, args.clickup_only) if flag
    )
    if only_flags > 1:
        print("Use only one of --meetings-only, --tasks-only, --clickup-only", file=sys.stderr)
        return 2

    fetch_clickup_flag = args.clickup_only or (not args.skip_clickup and not args.meetings_only and not args.tasks_only)
    fetch_meetings_flag = args.meetings_only or (not args.tasks_only and not args.clickup_only)
    fetch_tasks_flag = args.tasks_only or (not args.meetings_only and not args.clickup_only)

    if args.clickup_only:
        fetch_meetings_flag = False
        fetch_tasks_flag = False
    if args.meetings_only:
        fetch_tasks_flag = False
        fetch_clickup_flag = False
    if args.tasks_only:
        fetch_meetings_flag = False
        fetch_clickup_flag = False

    start_day, end_day = resolve_date_range(args)
    time_min, time_max = range_bounds_iso(start_day, end_day)
    out_dir = args.output or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Domain: {DOMAIN}")
    print(f"Date range (ET): {start_day} → {end_day}")
    print(
        f"Fetch meetings: {fetch_meetings_flag} | "
        f"Fetch tasks: {fetch_tasks_flag} | "
        f"Fetch ClickUp: {fetch_clickup_flag}"
    )

    all_meetings: list[dict] = []
    all_tasks: list[dict] = []
    all_clickup: list[dict] = []
    errors: list[str] = []
    user_stats: list[dict] = []

    if fetch_clickup_flag:
        print("Fetching ClickUp time entries…")
        clickup_rows, clickup_err = fetch_clickup_time_entries(start_day, end_day)
        all_clickup.extend(clickup_rows)
        if clickup_err:
            errors.append(clickup_err)
        print(f"  ClickUp entries: {len(clickup_rows)}")

    if fetch_meetings_flag or fetch_tasks_flag:
        email_filter = {e.strip().lower() for e in (args.email or []) if e.strip()}
        if email_filter:
            users = [
                {"primaryEmail": e, "name": {"fullName": e.split("@")[0]}}
                for e in sorted(email_filter)
            ]
            print(f"Users: {len(users)} (email filter: {', '.join(sorted(email_filter))})")
        else:
            users = list_workspace_users()
            print(f"Users: {len(users)} (excluded: {len(EXCLUDE_EMAILS)})")

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    process_user,
                    u,
                    time_min=time_min,
                    time_max=time_max,
                    start_day=start_day,
                    end_day=end_day,
                    do_meetings=fetch_meetings_flag,
                    do_tasks=fetch_tasks_flag,
                ): u
                for u in users
            }
            done = 0
            for fut in as_completed(futures):
                u = futures[fut]
                email = u.get("primaryEmail", "")
                meetings, tasks, err = fut.result()
                all_meetings.extend(meetings)
                all_tasks.extend(tasks)
                if err:
                    errors.append(err)
                user_stats.append(
                    {
                        "email": email,
                        "name": u.get("name", {}).get("fullName", ""),
                        "meetings": len(meetings),
                        "tasks": len(tasks),
                        "error": err or "",
                    }
                )
                done += 1
                if done % 10 == 0 or done == len(users):
                    print(f"  … {done}/{len(users)} users processed")

    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    email_tag = ""
    if args.email:
        email_tag = "_" + "_".join(e.split("@")[0].lower() for e in args.email if e.strip())
    base = out_dir / f"workspace_export{email_tag}_{start_day}_to_{end_day}_{stamp}"

    payload = {
        "fetched_at": datetime.now(TZ).isoformat(),
        "domain": DOMAIN,
        "date_range": {"start": start_day, "end": end_day, "timezone": str(TZ)},
        "users_processed": len(user_stats),
        "meetings_count": len(all_meetings),
        "tasks_count": len(all_tasks),
        "clickup_count": len(all_clickup),
        "errors_sample": errors[:20],
        "user_stats": user_stats,
        "meetings": all_meetings,
        "tasks": all_tasks,
        "clickup": all_clickup,
    }

    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    meet_csv = base.with_name(base.name + "_meetings.csv")
    task_csv = base.with_name(base.name + "_tasks.csv")
    combined_csv = base.with_name(base.name + "_combined.csv")
    clickup_csv = base.with_name(base.name + "_clickup.csv")
    stats_csv = base.with_name(base.name + "_user_stats.csv")

    if fetch_meetings_flag:
        write_csv(meet_csv, all_meetings)
    if fetch_tasks_flag:
        write_csv(task_csv, all_tasks)
    if fetch_meetings_flag or fetch_tasks_flag:
        n_combined = write_combined_csv(combined_csv, all_meetings, all_tasks)
    else:
        n_combined = 0
    if fetch_clickup_flag:
        write_csv(clickup_csv, all_clickup)
    if user_stats:
        write_csv(stats_csv, user_stats)

    # Friendly single-user copy
    if args.email and len(args.email) == 1 and n_combined:
        who = args.email[0].split("@")[0].lower()
        friendly = out_dir / f"{who}_meets_and_tasks_{start_day}_to_{end_day}.csv"
        friendly.write_bytes(combined_csv.read_bytes())
    else:
        friendly = None

    print("\n=== Done ===")
    print(f"Meetings/calendar: {len(all_meetings)}")
    print(f"Google Tasks:      {len(all_tasks)}")
    print(f"Combined rows:     {n_combined}")
    print(f"ClickUp:           {len(all_clickup)}")
    print(f"Errors:            {len(errors)}")
    print(f"JSON:     {json_path}")
    if fetch_meetings_flag:
        print(f"Meet CSV: {meet_csv}")
    if fetch_tasks_flag:
        print(f"Task CSV: {task_csv}")
    if n_combined:
        print(f"Combined: {combined_csv}")
    if friendly:
        print(f"Sheet:    {friendly}")
    if fetch_clickup_flag:
        print(f"ClickUp:  {clickup_csv}")
    if user_stats:
        print(f"Stats:    {stats_csv}")

    if errors:
        print("\nFirst errors:")
        for e in errors[:5]:
            print(f"  - {e}")
        tasks_auth_fail = any(
            "unauthorized_client" in e.lower() or "tasks" in e.lower()
            for e in errors
        )
        if fetch_tasks_flag and (tasks_auth_fail or not all_tasks):
            print_tasks_auth_help()

    has_data = bool(all_meetings or all_tasks or all_clickup)
    return 1 if errors and not has_data else 0


if __name__ == "__main__":
    raise SystemExit(main())
