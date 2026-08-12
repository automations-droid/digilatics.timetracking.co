"""ClickUp time-entry + task-created sync → Postgres."""

from __future__ import annotations

import os
import re
import concurrent.futures
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

from db import delete_stale_entries, existing_entry_ids, get_session, init_db, record_sync_run, upsert_entries
from matching import build_match_index, match_client_with_launchpad
from sheets_refs import (
    allowed_client_names,
    load_employees,
    load_launchpad_subclients,
    load_master_clients,
)

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "25658037")
LOOKBACK_DAYS = int(os.getenv("CLICKUP_LOOKBACK_DAYS", "14"))
# Optional explicit range (YYYY-MM-DD, America/New_York). Overrides LOOKBACK_DAYS.
CLICKUP_START_DATE = os.getenv("CLICKUP_START_DATE", "").strip()
CLICKUP_END_DATE = os.getenv("CLICKUP_END_DATE", "").strip()
TZ = ZoneInfo("America/New_York")


def _headers() -> dict:
    return {"Authorization": CLICKUP_TOKEN}


def _clean_ascii(s: str) -> str:
    return re.sub(r"[^\x20-\x7E]", "", s or "").strip()


def _et_date_from_ms(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d")


def fetch_spaces(client: httpx.Client) -> dict[str, str]:
    r = client.get(
        f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}/space",
        params={"archived": "false"},
        headers=_headers(),
    )
    r.raise_for_status()
    return {str(s["id"]): s["name"] for s in (r.json().get("spaces") or [])}


def fetch_assignee_ids(client: httpx.Client) -> str:
    r = client.get(f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}", headers=_headers())
    r.raise_for_status()
    members = (r.json().get("team") or {}).get("members") or []
    return ",".join(str(m["user"]["id"]) for m in members if m.get("user") and m["user"].get("id"))


def fetch_time_entries(client: httpx.Client, start_ms: int, end_ms: int, assignees: str) -> list[dict]:
    r = client.get(
        f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}/time_entries",
        params={"start_date": start_ms, "end_date": end_ms, "assignee": assignees},
        headers=_headers(),
    )
    r.raise_for_status()
    return r.json().get("data") or []


def fetch_task(client: httpx.Client, task_id: str) -> dict:
    try:
        r = client.get(f"https://api.clickup.com/api/v2/task/{task_id}", headers=_headers())
        r.raise_for_status()
        task = r.json()
        return {
            "description": task.get("description") or task.get("text_content") or "",
            "customFields": task.get("custom_fields") or [],
        }
    except Exception:
        return {"description": "", "customFields": []}


def fetch_created_tasks_today(client: httpx.Client) -> list[dict]:
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    try:
        r = client.get(
            f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}/task",
            params={
                "date_created_gt": start_ms,
                "date_created_lt": end_ms,
                "include_closed": "true",
                "subtasks": "true",
            },
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json().get("tasks") or []
    except Exception:
        return []


def build_rows(
    entries: list[dict],
    space_map: dict[str, str],
    task_details: dict[str, dict],
    *,
    clients,
    index,
    subclients,
    email_name,
    email_fallback,
    email_team,
    existing_ids: set[str],
    allowed_clients: list[str] | None = None,
    skip_existing: bool = True,
) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    sample_unmatched: list[str] = []

    for item in entries:
        duration = int(item.get("duration") or 0)
        if duration == 0:
            continue
        entry_id = str(item.get("id") or "")
        if skip_existing and entry_id and entry_id in existing_ids:
            continue

        task_obj = item.get("task") if isinstance(item.get("task"), dict) else {}
        task_name = task_obj.get("name") or ""
        clean_task_name = _clean_ascii(task_name)
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        user_email = (user.get("email") or "").lower()
        user_name = email_name.get(user_email) or user.get("username") or user_email or "Unknown"
        user_fallback = email_fallback.get(user_email) or "Digilatics"
        task_id = str(task_obj.get("id") or "")
        task_url = f"https://app.clickup.com/t/{task_id}" if task_id else ""
        details = task_details.get(task_id) or {"description": "", "customFields": []}
        match = match_client_with_launchpad(
            title=clean_task_name,
            description=details.get("description") or "",
            custom_fields=details.get("customFields") or [],
            user_fallback=user_fallback,
            clients=clients,
            index=index,
            subclients=subclients,
            user_team=email_team.get(user_email) or "",
            user_email=user_email,
            allowed_clients=allowed_clients,
        )
        if match.get("via") == "fallback" and len(sample_unmatched) < 10:
            sample_unmatched.append(clean_task_name)

        loc = item.get("task_location") if isinstance(item.get("task_location"), dict) else {}
        space_id = str(loc.get("space_id") or "")
        space_name = space_map.get(space_id) or "Unknown"
        start_ms = int(item.get("start") or 0)
        date = _et_date_from_ms(start_ms)

        if match.get("region") and match.get("regionClients"):
            n = len(match["regionClients"])
            split_hours = round(duration / 3600000 / n, 2)
            split_minutes = round(duration / 60000 / n)
            for i, rc in enumerate(match["regionClients"]):
                split_id = f"{entry_id}_{i}"
                if skip_existing and split_id in existing_ids:
                    continue
                rows.append(
                    {
                        "date": date,
                        "client": rc,
                        "task": clean_task_name,
                        "user": user_name,
                        "hours": split_hours,
                        "minutes": split_minutes,
                        "source": "clickup",
                        "url": task_url,
                        "entryId": split_id,
                        "space": space_name,
                        "team": email_team.get(user_email) or "Unknown",
                        "matchVia": "launchpad_region_split",
                    }
                )
        else:
            rows.append(
                {
                    "date": date,
                    "client": match["client"],
                    "task": clean_task_name,
                    "user": user_name,
                    "hours": round(duration / 3600000, 2),
                    "minutes": round(duration / 60000),
                    "source": "clickup",
                    "url": task_url,
                    "entryId": entry_id,
                    "space": space_name,
                    "team": email_team.get(user_email) or "Unknown",
                    "matchVia": match.get("via"),
                }
            )
    return rows, sample_unmatched


def build_created_rows(
    created_tasks: list[dict],
    space_map: dict[str, str],
    *,
    clients,
    index,
    subclients,
    email_name,
    email_fallback,
    email_team,
    existing_ids: set[str],
    allowed_clients: list[str] | None = None,
    skip_existing: bool = True,
    creator_emails: set[str] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for t in created_tasks:
        creator = t.get("creator") or {}
        creator_email = (creator.get("email") or "").lower()
        if not creator_email:
            continue
        if creator_emails and creator_email not in creator_emails:
            continue
        creator_id = str(creator.get("id") or "")
        synthetic_id = f"created_{t.get('id')}_{creator_id}"
        if skip_existing and synthetic_id in existing_ids:
            continue
        clean_name = _clean_ascii(t.get("name") or "")
        user_fallback = email_fallback.get(creator_email) or "Digilatics"
        user_name = email_name.get(creator_email) or creator_email
        space_id = str((t.get("space") or {}).get("id") or "")
        space_name = space_map.get(space_id) or "Unknown"
        date = _et_date_from_ms(int(t.get("date_created") or 0))
        task_url = f"https://app.clickup.com/t/{t.get('id')}"
        match = match_client_with_launchpad(
            title=clean_name,
            description="",
            custom_fields=[],
            user_fallback=user_fallback,
            clients=clients,
            index=index,
            subclients=subclients,
            user_team=email_team.get(creator_email) or "",
            user_email=creator_email,
            allowed_clients=allowed_clients,
        )
        rows.append(
            {
                "date": date,
                "client": match["client"],
                "task": f"[Task Created] {clean_name}",
                "user": user_name,
                "hours": round(2 / 60, 2),
                "minutes": 2,
                "source": "clickup",
                "url": task_url,
                "entryId": synthetic_id,
                "space": space_name,
                "team": email_team.get(creator_email) or "Unknown",
                "matchVia": f"{match.get('via')}_created",
            }
        )
    return rows


def run_sync() -> dict:
    if not CLICKUP_TOKEN:
        raise RuntimeError("CLICKUP_API_TOKEN is not set")

    started = datetime.now(timezone.utc)
    init_db()
    clients, master_err = load_master_clients()
    email_name, email_fallback, email_team, emp_err = load_employees()
    subclients = load_launchpad_subclients()
    index = build_match_index(clients)
    allowed_clients = allowed_client_names(clients, email_fallback)

    if CLICKUP_START_DATE and CLICKUP_END_DATE:
        start = datetime.strptime(CLICKUP_START_DATE, "%Y-%m-%d").replace(tzinfo=TZ)
        end = datetime.strptime(CLICKUP_END_DATE, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999999, tzinfo=TZ
        )
    else:
        end = datetime.now(TZ)
        start = (end - timedelta(days=LOOKBACK_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    with httpx.Client(timeout=60.0) as client:
        space_map = fetch_spaces(client)
        assignees = fetch_assignee_ids(client)
        entries = fetch_time_entries(client, start_ms, end_ms, assignees)

        session = get_session()
        try:
            existing_ids = existing_entry_ids(session)
        finally:
            session.close()

        unique_task_ids: set[str] = set()
        for item in entries:
            if int(item.get("duration") or 0) == 0:
                continue
            eid = str(item.get("id") or "")
            if eid and eid in existing_ids:
                continue
            task_obj = item.get("task")
            tid = task_obj.get("id") if isinstance(task_obj, dict) else None
            if tid:
                unique_task_ids.add(str(tid))

        task_details: dict[str, dict] = {}

        def _one(tid: str):
            with httpx.Client(timeout=60.0) as c:
                return tid, fetch_task(c, tid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for tid, details in pool.map(lambda t: _one(t), unique_task_ids):
                task_details[tid] = details

        rows, sample_unmatched = build_rows(
            entries,
            space_map,
            task_details,
            clients=clients,
            index=index,
            subclients=subclients,
            email_name=email_name,
            email_fallback=email_fallback,
            email_team=email_team,
            existing_ids=existing_ids,
            allowed_clients=allowed_clients,
        )
        created = fetch_created_tasks_today(client)
        rows.extend(
            build_created_rows(
                created,
                space_map,
                clients=clients,
                index=index,
                subclients=subclients,
                email_name=email_name,
                email_fallback=email_fallback,
                email_team=email_team,
                existing_ids=existing_ids,
                allowed_clients=allowed_clients,
            )
        )

    breakdown: dict[str, int] = {}
    for r in rows:
        via = r.get("matchVia") or "unknown"
        breakdown[via] = breakdown.get(via, 0) + 1

    debug = {
        "clientsLoaded": len(clients),
        "masterLoadError": master_err,
        "employeeLoadError": emp_err,
        "emailNameMapSize": len(email_name),
        "emailFallbackMapSize": len(email_fallback),
        "emailTeamMapSize": len(email_team),
        "totalEntries": len(entries),
        "newRows": len(rows),
        "uniqueTasksFetched": len(unique_task_ids),
        "matchBreakdown": breakdown,
        "sampleUnmatched": sample_unmatched,
        "lookbackDays": LOOKBACK_DAYS,
        "rangeStart": start.isoformat(),
        "rangeEnd": end.isoformat(),
    }

    session = get_session()
    try:
        inserted = upsert_entries(session, rows)
        record_sync_run(
            session,
            job="clickup",
            rows_inserted=inserted,
            status="ok",
            debug=debug,
            started_at=started,
        )
    except Exception as e:
        session.rollback()
        record_sync_run(
            session,
            job="clickup",
            rows_inserted=0,
            status="error",
            debug={**debug, "error": str(e)},
            started_at=started,
        )
        raise
    finally:
        session.close()

    debug["rowsInserted"] = inserted
    return debug


def _assignee_ids_for_emails(client: httpx.Client, emails: set[str]) -> str:
    r = client.get(f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}", headers=_headers())
    r.raise_for_status()
    members = (r.json().get("team") or {}).get("members") or []
    ids = [
        str(m["user"]["id"])
        for m in members
        if m.get("user")
        and m["user"].get("id")
        and (m["user"].get("email") or "").lower() in emails
    ]
    return ",".join(ids)


def _day_bounds_ms(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def run_sync_today(
    *,
    user_emails: list[str],
    day: str | None = None,
    user_display_names: set[str] | None = None,
) -> dict:
    """Reconcile today's ClickUp rows for specific users (add/update/remove)."""
    if not CLICKUP_TOKEN:
        raise RuntimeError("CLICKUP_API_TOKEN is not set")

    started = datetime.now(timezone.utc)
    init_db()
    day = day or datetime.now(TZ).strftime("%Y-%m-%d")
    emails = {e.lower().strip() for e in user_emails if e and e.strip()}
    if not emails:
        return {"day": day, "rowsUpserted": 0, "rowsDeleted": 0}

    clients, master_err = load_master_clients()
    email_name, email_fallback, email_team, emp_err = load_employees()
    subclients = load_launchpad_subclients()
    index = build_match_index(clients)
    allowed_clients = allowed_client_names(clients, email_fallback)
    start_ms, end_ms = _day_bounds_ms(day)

    with httpx.Client(timeout=60.0) as client:
        assignees = _assignee_ids_for_emails(client, emails)
        if not assignees:
            return {"day": day, "rowsUpserted": 0, "rowsDeleted": 0, "error": "no assignees"}

        space_map = fetch_spaces(client)
        entries = fetch_time_entries(client, start_ms, end_ms, assignees)
        entries = [
            e
            for e in entries
            if int(e.get("duration") or 0) > 0
            and ((e.get("user") or {}).get("email") or "").lower() in emails
        ]

        session = get_session()
        try:
            existing_ids = existing_entry_ids(session)
        finally:
            session.close()

        unique_task_ids: set[str] = set()
        for item in entries:
            task_obj = item.get("task")
            tid = task_obj.get("id") if isinstance(task_obj, dict) else None
            if tid:
                unique_task_ids.add(str(tid))

        task_details: dict[str, dict] = {}

        def _one(tid: str):
            with httpx.Client(timeout=60.0) as c:
                return tid, fetch_task(c, tid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for tid, details in pool.map(lambda t: _one(t), unique_task_ids):
                task_details[tid] = details

        rows, sample_unmatched = build_rows(
            entries,
            space_map,
            task_details,
            clients=clients,
            index=index,
            subclients=subclients,
            email_name=email_name,
            email_fallback=email_fallback,
            email_team=email_team,
            existing_ids=existing_ids,
            allowed_clients=allowed_clients,
            skip_existing=False,
        )
        created = fetch_created_tasks_today(client)
        rows.extend(
            build_created_rows(
                created,
                space_map,
                clients=clients,
                index=index,
                subclients=subclients,
                email_name=email_name,
                email_fallback=email_fallback,
                email_team=email_team,
                existing_ids=existing_ids,
                allowed_clients=allowed_clients,
                skip_existing=False,
                creator_emails=emails,
            )
        )

    valid_ids = {str(r["entryId"]) for r in rows if r.get("entryId")}
    user_names = user_display_names
    if user_names is None:
        user_names = {email_name.get(e) or e for e in emails}
        user_names = {n for n in user_names if n}

    debug = {
        "day": day,
        "userEmails": sorted(emails),
        "entriesFetched": len(entries),
        "rowsBuilt": len(rows),
        "validEntryIds": len(valid_ids),
        "masterLoadError": master_err,
        "employeeLoadError": emp_err,
        "sampleUnmatched": sample_unmatched[:5],
    }

    session = get_session()
    try:
        upserted = upsert_entries(session, rows)
        deleted = delete_stale_entries(
            session,
            source="clickup",
            days=[day],
            valid_entry_ids=valid_ids,
            user_names=user_names,
        )
        record_sync_run(
            session,
            job="clickup_today",
            rows_inserted=upserted,
            status="ok",
            debug={**debug, "rowsDeleted": deleted},
            started_at=started,
        )
    except Exception as e:
        session.rollback()
        record_sync_run(
            session,
            job="clickup_today",
            rows_inserted=0,
            status="error",
            debug={**debug, "error": str(e)},
            started_at=started,
        )
        raise
    finally:
        session.close()

    debug["rowsUpserted"] = upserted
    debug["rowsDeleted"] = deleted
    return debug


if __name__ == "__main__":
    print(run_sync())
