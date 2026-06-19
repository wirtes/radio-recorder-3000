from __future__ import annotations

import atexit
from datetime import datetime, timedelta, timezone
from threading import Thread
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from .db import execute, now_iso, query
from .processing import deliver_recording, record_show

_scheduler = None


def _scheduled_datetime(show, now_local: datetime) -> datetime:
    hour, minute = map(int, show["start_time"].split(":"))
    return now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def runs_on_weekday(frequency: str, configured_weekday: int | None, weekday: int) -> bool:
    if frequency == "weekly":
        return configured_weekday == weekday
    if frequency == "weekdays":
        return weekday < 5
    return True


def scan_due_shows(app) -> None:
    with app.app_context():
        tz = ZoneInfo(__import__("os").environ.get("TZ", "UTC"))
        now_local = datetime.now(tz)
        shows = query("SELECT * FROM shows WHERE enabled=1")
        for show in shows:
            scheduled = _scheduled_datetime(show, now_local)
            if not runs_on_weekday(
                show["frequency"], show["weekday"], now_local.weekday()
            ):
                continue
            if not (scheduled <= now_local < scheduled + timedelta(minutes=2)):
                continue
            scheduled_utc = scheduled.astimezone(timezone.utc).isoformat()
            try:
                recording_id = execute(
                    """
                    INSERT INTO recordings(show_id, scheduled_at, status, created_at, updated_at)
                    VALUES(?, ?, 'queued', ?, ?)
                    """,
                    (show["id"], scheduled_utc, now_iso(), now_iso()),
                )
            except Exception:
                continue
            Thread(target=record_show, args=(app, recording_id), daemon=True).start()


def retry_deliveries(app) -> None:
    with app.app_context():
        rows = query(
            """
            SELECT id FROM recordings
            WHERE status IN ('ready', 'delivery_pending')
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            """,
            (now_iso(),),
        )
        for row in rows:
            deliver_recording(row["id"])


def start_scheduler(app) -> None:
    global _scheduler
    if _scheduler:
        return
    _scheduler = BackgroundScheduler(timezone=ZoneInfo(__import__("os").environ.get("TZ", "UTC")))
    _scheduler.add_job(scan_due_shows, "interval", minutes=1, args=[app], max_instances=1)
    _scheduler.add_job(retry_deliveries, "interval", minutes=1, args=[app], max_instances=1)
    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False) if _scheduler.running else None)
