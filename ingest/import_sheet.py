"""One-shot import of legacy Sheet1 time rows into Postgres."""

from __future__ import annotations

from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from db import count_entries, get_session, init_db, record_sync_run, upsert_entries
from sheets_refs import load_sheet_time_rows


def run_import() -> dict:
    started = datetime.now(timezone.utc)
    init_db()
    rows = load_sheet_time_rows()
    session = get_session()
    try:
        before = count_entries(session)
        inserted = upsert_entries(session, rows)
        after = count_entries(session)
        debug = {
            "sheetRows": len(rows),
            "rowsInserted": inserted,
            "dbCountBefore": before,
            "dbCountAfter": after,
        }
        record_sync_run(
            session,
            job="sheet_import",
            rows_inserted=inserted,
            status="ok",
            debug=debug,
            started_at=started,
        )
        return debug
    except Exception as e:
        session.rollback()
        record_sync_run(
            session,
            job="sheet_import",
            rows_inserted=0,
            status="error",
            debug={"error": str(e)},
            started_at=started,
        )
        raise
    finally:
        session.close()


if __name__ == "__main__":
    print(run_import())
