"""Background scheduler for ClickUp + Meet sync jobs."""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("ingest.scheduler")

CLICKUP_MINUTES = int(os.getenv("SYNC_CLICKUP_MINUTES", "30"))
MEET_MINUTES = int(os.getenv("SYNC_MEET_MINUTES", "30"))
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"

_scheduler: BackgroundScheduler | None = None


def _safe_clickup():
    try:
        from ingest.clickup import run_sync

        debug = run_sync()
        log.info("clickup sync ok inserted=%s", debug.get("rowsInserted"))
    except Exception:
        log.exception("clickup sync failed")


def _safe_meet():
    try:
        from ingest.meet import run_sync

        debug = run_sync()
        log.info("meet sync ok inserted=%s", debug.get("rowsInserted"))
    except Exception:
        log.exception("meet sync failed")


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not ENABLE_SCHEDULER:
        log.info("scheduler disabled (ENABLE_SCHEDULER=false)")
        return None
    if _scheduler is not None:
        return _scheduler
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(_safe_clickup, "interval", minutes=CLICKUP_MINUTES, id="clickup", max_instances=1)
    sched.add_job(_safe_meet, "interval", minutes=MEET_MINUTES, id="meet", max_instances=1)
    # Also run once shortly after boot
    sched.add_job(_safe_clickup, "date", id="clickup_boot")
    sched.add_job(_safe_meet, "date", id="meet_boot")
    sched.start()
    _scheduler = sched
    log.info(
        "scheduler started clickup_every=%sm meet_every=%sm",
        CLICKUP_MINUTES,
        MEET_MINUTES,
    )
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
