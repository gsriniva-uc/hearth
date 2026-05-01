"""
main.py — Hearth FastAPI entry point

Exposes all endpoints the React Native app needs:
  /auth/register    POST  — register user after Google OAuth
  /events           GET   — upcoming events for user
  /events/today     GET   — today's events
  /events           POST  — add event
  /events/{id}      DELETE — delete event
  /tasks            GET   — pending tasks
  /tasks/{id}/send  POST  — send email draft
  /tasks/{id}/snooze POST — snooze task
  /tasks/{id}/done  POST  — mark task done
  /tasks/voice      POST  — create task from voice transcript
  /briefing         GET   — daily briefing
  /agent            POST  — chat with Hearth
  /gmail/scan       POST  — trigger Gmail scan
  /profiles         GET   — get children profiles
  /profiles         POST  — save child profile
  /health           GET   — health check
"""

import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

import hearth_config as cfg
from agent.calendar_agent import (
    init_db, _query_upcoming, _query_today,
    _insert_event, _delete_event, _event_exists
)
from agent.profile_agent import init_profiles, get_all_profiles, upsert_profile
from agent.briefing_agent import _build_briefing
from agent.graph import run
from db_migrate import migrate

app = FastAPI(title="Hearth API", version="1.0.0")

# ── CORS — allow React Native app to call this API ────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    migrate()
    init_db()
    init_profiles()
    print(f"[hearth] API started — DB at {cfg.DB_PATH}")


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


# ── Auth ──────────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    user:         dict
    access_token: str

@app.post("/auth/register")
def register_user(req: RegisterRequest):
    """Called after Google OAuth on the mobile app."""
    user_id = req.user.get("user_id", "")
    # Save token for Gmail access
    token_dir = os.path.join(cfg.DATA_DIR, "tokens", user_id)
    os.makedirs(token_dir, exist_ok=True)
    token_path = os.path.join(token_dir, "google_token.json")
    import json
    with open(token_path, "w") as f:
        json.dump({"access_token": req.access_token}, f)
    return {"status": "registered", "user_id": user_id}


# ── Events ────────────────────────────────────────────────────────────────────
@app.get("/events")
def get_events(user_id: str, days_ahead: int = 14):
    return _query_upcoming(user_id, days_ahead=days_ahead)

@app.get("/events/today")
def get_today(user_id: str):
    return _query_today(user_id)

class EventRequest(BaseModel):
    user_id:    str
    child_name: str
    event_type: str
    event_date: str
    event_time: Optional[str] = None
    notes:      Optional[str] = None

@app.post("/events")
def add_event(req: EventRequest):
    eid = _insert_event(
        req.user_id, req.child_name, req.event_type,
        req.event_date, req.event_time, req.notes
    )
    return {"id": eid, "status": "created"}

@app.delete("/events/{event_id}")
def delete_event(event_id: int, user_id: str):
    ok = _delete_event(user_id, event_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"status": "deleted"}


# ── Tasks (stub — returns empty list until task agent is built) ───────────────
@app.get("/tasks")
def get_tasks(user_id: str, status: str = "pending"):
    return []

@app.post("/tasks/{task_id}/send")
def send_task(task_id: int, user_id: str):
    return {"status": "sent"}

@app.post("/tasks/{task_id}/snooze")
def snooze_task(task_id: int, user_id: str, days: int = 3):
    return {"status": "snoozed"}

@app.post("/tasks/{task_id}/done")
def done_task(task_id: int, user_id: str):
    return {"status": "done"}

class VoiceRequest(BaseModel):
    user_id:    str
    transcript: str

@app.post("/tasks/voice")
def voice_task(req: VoiceRequest):
    """Create a task from a voice transcript."""
    result = run(raw_text=req.transcript, user_id=req.user_id)
    return {"status": "created", "response": result.get("response", "")}


# ── Briefing ──────────────────────────────────────────────────────────────────
@app.get("/briefing")
def get_briefing(user_id: str):
    text   = _build_briefing(user_id)
    today  = _query_today(user_id)
    upcoming = _query_upcoming(user_id, days_ahead=7)
    tomorrow_str = ""
    import datetime
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    tomorrow_evs = [e for e in upcoming if e["event_date"] == tomorrow]
    return {
        "text":      text,
        "today":     today,
        "tomorrow":  tomorrow_evs,
        "this_week": upcoming,
        "tasks":     [],
    }


# ── Agent / Chat ──────────────────────────────────────────────────────────────
class AgentRequest(BaseModel):
    user_id:  str
    raw_text: str

@app.post("/agent")
def agent_chat(req: AgentRequest):
    result = run(raw_text=req.raw_text, user_id=req.user_id)
    return {"response": result.get("response", "")}


# ── Gmail ─────────────────────────────────────────────────────────────────────
@app.post("/gmail/scan")
def gmail_scan(user_id: str):
    try:
        from agent.gmail_agent import auto_scan_and_save
        from agent.profile_agent import get_children
        children = get_children(user_id)
        result   = auto_scan_and_save(user_id, children, days_back=14)
        return result
    except Exception as e:
        return {"new": 0, "skipped": 0, "error": str(e)}


# ── Profiles ──────────────────────────────────────────────────────────────────
@app.get("/profiles")
def get_profiles(user_id: str):
    return get_all_profiles(user_id)

class ProfileRequest(BaseModel):
    user_id:    str
    name:       str
    grade:      Optional[str] = None
    school:     Optional[str] = None
    activities: Optional[str] = None
    notes:      Optional[str] = None

@app.post("/profiles")
def save_profile(req: ProfileRequest):
    upsert_profile(req.user_id, req.name, req.grade,
                   req.school, req.activities, req.notes)
    return {"status": "saved"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=cfg.API_PORT, reload=True)
