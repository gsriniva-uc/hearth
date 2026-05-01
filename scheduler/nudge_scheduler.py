"""
scheduler/nudge_scheduler.py — Multi-user nudge scheduler

Runs briefing + nudge scan for ALL users each morning.
Per-user scoping via user_id on all DB queries.
"""
import sqlite3, os
from datetime import date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
import uvicorn
import hearth_config as cfg
from agent.notification_agent import notification_agent
from agent.briefing_agent import briefing_agent, _build_briefing
from agent.state import HearthState

app = FastAPI(title="Hearth Scheduler")

_7D_TYPES  = {"recital","field_trip"}
_48H_TYPES = {"recital","field_trip","dress_down_day","early_dismissal","doctor_appointment",
              "movie_night","special_day","sports_game","school_holiday","other"}
_DAY_TYPES = {"recital","field_trip","dress_down_day","early_dismissal","doctor_appointment",
              "special_day","sports_game","other"}

def _conn():
    if not os.path.exists(cfg.DB_PATH): return None
    c = sqlite3.connect(cfg.DB_PATH); c.row_factory = sqlite3.Row; return c

def _get_all_user_ids() -> list[str]:
    """Return distinct user_ids that have events."""
    c = _conn()
    if not c: return []
    rows = c.execute("SELECT DISTINCT user_id FROM events").fetchall()
    c.close()
    return [r["user_id"] for r in rows]

def _mark(user_id, event_id, col):
    c = _conn()
    if not c: return
    c.execute(f"UPDATE events SET {col}=1 WHERE id=? AND user_id=?", (event_id, user_id))
    c.commit(); c.close()

def _pending_nudges(user_id: str):
    c = _conn()
    if not c: return []
    today = date.today()
    d2, d7 = today+timedelta(days=2), today+timedelta(days=7)
    rows = c.execute(
        "SELECT * FROM events WHERE user_id=? AND event_date >= ? ORDER BY event_date",
        (user_id, today.isoformat())).fetchall()
    c.close()
    nudges = []
    for row in rows:
        ev, etype, edate = dict(row), row["event_type"], date.fromisoformat(row["event_date"])
        if cfg.NUDGE_7D_ENABLED and edate==d7 and etype in _7D_TYPES and not ev["nudge_sent_7d"]:
            nudges.append({**ev,"nudge_type":"7d"}); _mark(user_id, ev["id"],"nudge_sent_7d")
        if cfg.NUDGE_48H_ENABLED and edate==d2 and etype in _48H_TYPES and not ev["nudge_sent_48h"]:
            nudges.append({**ev,"nudge_type":"48h"}); _mark(user_id, ev["id"],"nudge_sent_48h")
        if cfg.NUDGE_MORNING_ENABLED and edate==today and etype in _DAY_TYPES and not ev["nudge_sent_day"]:
            nudges.append({**ev,"nudge_type":"day_of"}); _mark(user_id, ev["id"],"nudge_sent_day")
    return nudges

def _build_nudge_message(nudge):
    name  = nudge["child_name"]
    etype = nudge["event_type"].replace("_"," ").title()
    when  = {"7d":"in 7 days","48h":"tomorrow","day_of":"TODAY"}[nudge["nudge_type"]]
    time_str = f" at {nudge['event_time']}" if nudge.get("event_time") else ""
    notes    = f"\n{nudge['notes']}" if nudge.get("notes") else ""
    return f"📅 {name} — {etype} {when} ({nudge['event_date']}{time_str}){notes}"

def run_nudge_scan(user_id: str = None) -> dict:
    """Run for a specific user or all users."""
    user_ids = [user_id] if user_id else _get_all_user_ids()
    total_sent = total_failed = 0
    for uid in user_ids:
        nudges = _pending_nudges(uid)
        for nudge in nudges:
            msg = _build_nudge_message(nudge)
            state: HearthState = {
                "input_type":"","raw_text":None,"pdf_bytes":None,"user_id":uid,
                "intent":None,"extracted_events":[],"confirmed_events":[],
                "briefing_text":None,"target_child":None,
                "response":msg,"notify":True,"notify_message":msg
            }
            result = notification_agent(state)
            if "❌" not in result.get("response",""):
                total_sent += 1
            else:
                total_failed += 1
    return {"sent":total_sent,"failed":total_failed}

def run_briefings():
    """Send daily briefing to all users."""
    for uid in _get_all_user_ids():
        briefing = _build_briefing(uid)
        state: HearthState = {
            "input_type":"","raw_text":None,"pdf_bytes":None,"user_id":uid,
            "intent":"briefing","extracted_events":[],"confirmed_events":[],
            "briefing_text":briefing,"target_child":None,
            "response":briefing,"notify":True,"notify_message":briefing
        }
        notification_agent(state)

scheduler = BackgroundScheduler()
scheduler.add_job(run_briefings,  "cron", hour=cfg.BRIEFING_HOUR,   minute=cfg.BRIEFING_MINUTE)
scheduler.add_job(run_nudge_scan, "cron", hour=cfg.NUDGE_SCAN_HOUR, minute=cfg.NUDGE_SCAN_MINUTE)

@app.on_event("startup")
def startup(): scheduler.start()

@app.on_event("shutdown")
def shutdown(): scheduler.shutdown()

@app.get("/health")
def health(): return {"status":"ok","users":len(_get_all_user_ids())}

@app.post("/briefing")
def trigger_briefing(): run_briefings(); return {"ok":True}

@app.post("/nudges")
def trigger_nudges(): return run_nudge_scan()

if __name__ == "__main__":
    uvicorn.run("scheduler.nudge_scheduler:app", host="0.0.0.0", port=cfg.API_PORT)
