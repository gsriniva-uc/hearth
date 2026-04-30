"""
scheduler/nudge_scheduler.py

Daily cron job — scans the events table and fires nudges via dispatcher.

Nudge timing matrix:
  Event type        7d    48h   day_of
  ─────────────────────────────────────
  recital           ✓     ✓     ✓
  field_trip        ✓     ✓     ✓
  dress_down_day    ✗     ✓     ✓
  early_dismissal   ✗     ✓     ✓
  movie_night       ✗     ✓     ✗
  special_day       ✗     ✓     ✓
  doctor_appt       ✗     ✓     ✓
  sports_game       ✗     ✓     ✓
  school_holiday    ✗     ✓     ✗
  other             ✗     ✓     ✓

Runs as a FastAPI app so Render/Docker can health-check it.
APScheduler fires the scan daily at NUDGE_SCAN_HOUR:NUDGE_SCAN_MINUTE.

Adapted from Alterus zoom_watcher.py — same poll-then-POST pattern.
"""

import sqlite3
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
import uvicorn

import hearth_config as cfg
from channels.dispatcher import fire_all

app = FastAPI(title="Hearth Nudge Scheduler")

# ── Nudge window rules ────────────────────────────────────────────────────────

_7D_TYPES = {"recital", "field_trip"}
_48H_TYPES = {
    "recital", "field_trip", "dress_down_day", "early_dismissal",
    "movie_night", "special_day", "doctor_appointment",
    "sports_game", "school_holiday", "other",
}
_DAY_OF_TYPES = {
    "recital", "field_trip", "dress_down_day", "early_dismissal",
    "special_day", "doctor_appointment", "sports_game", "other",
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _mark_sent(event_id: int, nudge_col: str):
    with _get_conn() as conn:
        conn.execute(f"UPDATE events SET {nudge_col}=1 WHERE id=?", (event_id,))
        conn.commit()


def _pending_nudges() -> list[dict]:
    """
    Return a list of nudge dicts for events whose windows are due TODAY
    and haven't been sent yet.
    """
    today      = date.today()
    in_2_days  = today + timedelta(days=2)
    in_7_days  = today + timedelta(days=7)

    nudges: list[dict] = []

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE event_date >= ? ORDER BY event_date",
            (today.isoformat(),),
        ).fetchall()

    for row in rows:
        ev = dict(row)
        etype = ev["event_type"]
        edate = date.fromisoformat(ev["event_date"])

        # day-of
        if edate == today and etype in _DAY_OF_TYPES and not ev["nudge_sent_day"]:
            nudges.append({**ev, "nudge_type": "day_of"})
            _mark_sent(ev["id"], "nudge_sent_day")

        # 48-hour
        if edate == in_2_days and etype in _48H_TYPES and not ev["nudge_sent_48h"]:
            nudges.append({**ev, "nudge_type": "48h"})
            _mark_sent(ev["id"], "nudge_sent_48h")

        # 7-day
        if edate == in_7_days and etype in _7D_TYPES and not ev["nudge_sent_7d"]:
            nudges.append({**ev, "nudge_type": "7d"})
            _mark_sent(ev["id"], "nudge_sent_7d")

    return nudges


def run_nudge_scan():
    """Called by APScheduler daily. Also exposed as a manual trigger endpoint."""
    print(f"[scheduler] Running nudge scan — {date.today().isoformat()}")
    nudges = _pending_nudges()
    if not nudges:
        print("[scheduler] No nudges to send today.")
        return {"sent": 0, "failed": 0}
    print(f"[scheduler] {len(nudges)} nudge(s) to fire.")
    result = fire_all(nudges)
    print(f"[scheduler] Done — sent: {result['sent']}, failed: {result['failed']}")
    return result


# ── APScheduler ───────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(
    run_nudge_scan,
    trigger="cron",
    hour=cfg.NUDGE_SCAN_HOUR,
    minute=cfg.NUDGE_SCAN_MINUTE,
    id="daily_nudge_scan",
)


@app.on_event("startup")
def startup():
    scheduler.start()
    print(f"[scheduler] Nudge scan scheduled daily at "
          f"{cfg.NUDGE_SCAN_HOUR:02d}:{cfg.NUDGE_SCAN_MINUTE:02d}")


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown()


# ── FastAPI endpoints ─────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "next_run": str(scheduler.get_job("daily_nudge_scan").next_run_time)}


@app.post("/trigger")
def manual_trigger():
    """Manually fire the nudge scan — useful for testing."""
    result = run_nudge_scan()
    return result


if __name__ == "__main__":
    uvicorn.run("scheduler.nudge_scheduler:app",
                host="0.0.0.0", port=cfg.API_PORT, reload=False)
