"""One-off: sync Aug range + export verify sheet (CSV/XLSX)."""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure project root on path when run as script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def export_range(start: str, end: str, out_dir: Path) -> tuple[Path, Path, int]:
    from openpyxl import Workbook
    from sqlalchemy import select

    from db import TimeEntry, get_session, init_db

    init_db()
    session = get_session()
    try:
        rows = (
            session.execute(
                select(TimeEntry)
                .where(TimeEntry.date >= start, TimeEntry.date <= end)
                .order_by(TimeEntry.date, TimeEntry.source, TimeEntry.user, TimeEntry.id)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()

    headers = [
        "Date",
        "Client",
        "Task",
        "User",
        "Hours",
        "Minutes",
        "Source",
        "URL",
        "EntryId",
        "Space",
        "Team",
        "MatchVia",
    ]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"time_verify_{start}_to_{end}_{stamp}.csv"
    xlsx_path = out_dir / f"time_verify_{start}_to_{end}_{stamp}.xlsx"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(
                [
                    r.date,
                    r.client,
                    r.task,
                    r.user,
                    r.hours,
                    r.minutes,
                    r.source,
                    r.url,
                    r.entry_id,
                    r.space,
                    r.team,
                    r.match_via or "",
                ]
            )

    wb = Workbook()
    ws = wb.active
    ws.title = "TimeEntries"
    ws.append(headers)
    for r in rows:
        ws.append(
            [
                r.date,
                r.client,
                r.task,
                r.user,
                r.hours,
                r.minutes,
                r.source,
                r.url,
                r.entry_id,
                r.space,
                r.team,
                r.match_via or "",
            ]
        )
    # summary sheet
    ws2 = wb.create_sheet("Summary")
    ws2.append(["Date", "Source", "Rows", "Hours"])
    from collections import defaultdict

    agg = defaultdict(lambda: {"rows": 0, "hours": 0.0})
    via = defaultdict(int)
    for r in rows:
        key = (r.date, r.source)
        agg[key]["rows"] += 1
        agg[key]["hours"] += float(r.hours or 0)
        via[r.match_via or "(none)"] += 1
    for (d, src), v in sorted(agg.items()):
        ws2.append([d, src, v["rows"], round(v["hours"], 2)])
    ws2.append([])
    ws2.append(["MatchVia", "Count"])
    for k, c in sorted(via.items(), key=lambda x: -x[1]):
        ws2.append([k, c])

    wb.save(xlsx_path)
    return csv_path, xlsx_path, len(rows)


def clear_range(start: str, end: str) -> int:
    from sqlalchemy import delete

    from db import TimeEntry, get_session, init_db

    init_db()
    session = get_session()
    try:
        result = session.execute(
            delete(TimeEntry).where(TimeEntry.date >= start, TimeEntry.date <= end)
        )
        session.commit()
        return result.rowcount or 0
    finally:
        session.close()


def main():
    start = os.getenv("VERIFY_START", "2026-08-01")
    end = os.getenv("VERIFY_END", "2026-08-05")
    out_dir = Path(os.getenv("VERIFY_OUT_DIR", str(Path.home() / "Downloads")))
    out_dir.mkdir(parents=True, exist_ok=True)

    deleted = clear_range(start, end)
    print(f"cleared {deleted} existing rows for {start}..{end}")

    # ClickUp range sync
    os.environ["CLICKUP_START_DATE"] = start
    os.environ["CLICKUP_END_DATE"] = end
    # reload module constants if already imported
    import importlib
    import ingest.clickup as clickup

    importlib.reload(clickup)
    print("=== ClickUp sync ===")
    cu = clickup.run_sync()
    print(
        {
            k: cu.get(k)
            for k in (
                "newRows",
                "rowsInserted",
                "totalEntries",
                "matchBreakdown",
                "rangeStart",
                "rangeEnd",
            )
        }
    )

    # Meet day-by-day
    import ingest.meet as meet

    tz = ZoneInfo("America/New_York")
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    meet_totals = []
    day = d0
    while day <= d1:
        day_s = day.strftime("%Y-%m-%d")
        os.environ["MEET_SINGLE_DAY"] = day_s
        importlib.reload(meet)
        print(f"=== Meet sync {day_s} ===")
        m = meet.run_sync()
        meet_totals.append(
            {
                "day": day_s,
                "rowsInserted": m.get("rowsInserted"),
                "totalNewMeetings": m.get("totalNewMeetings"),
                "matchBreakdown": m.get("matchBreakdown"),
                "usersErrored": m.get("usersErrored"),
            }
        )
        print(meet_totals[-1])
        day += timedelta(days=1)

    csv_path, xlsx_path, n = export_range(start, end, out_dir)
    print("=== Export ===")
    print({"rows": n, "csv": str(csv_path), "xlsx": str(xlsx_path)})
    print({"clickup": cu.get("rowsInserted"), "meet_days": meet_totals, "deleted_before": deleted})


if __name__ == "__main__":
    main()
