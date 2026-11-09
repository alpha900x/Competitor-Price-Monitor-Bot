"""Optional internal scheduler. OS cron calling `python src/main.py` is simpler."""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

from src.config import get_settings
from src.main import run_daily_job

log = logging.getLogger(__name__)


def start_scheduler() -> None:
    """Block forever, running run_daily_job daily at RUN_HOUR_UTC."""
    from apscheduler.schedulers.blocking import BlockingScheduler  # optional dependency

    settings = get_settings()
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(run_daily_job, "cron", hour=settings.RUN_HOUR_UTC, minute=0,
                  id="daily_run", max_instances=1, coalesce=True)
    log.info("scheduler started: daily at %02d:00 UTC", settings.RUN_HOUR_UTC)
    sched.start()


if __name__ == "__main__":
    start_scheduler()
