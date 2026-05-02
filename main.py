"""
main.py — Hearth FastAPI entry point

Endpoints for React Native app + Google OAuth backend flow.
"""

import os
import json
import hashlib
import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional
import httpx

import hearth_config as cfg
from agent.calendar_agent import (
    init_db, _query_upcoming, _query_today,
    _insert_event, _delete_event,
)
from agent.profile_agent import init_profiles, get_all_profiles, upsert_profile
from agent.briefing_agent import _build_briefing
from agent.graph import run
from db_migrate import migrate

app = FastAPI(title="Hearth API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Google OAuth config ───────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID",
    "289231572725-5fn10ulbb5hi6gqohnl1v6ourjsj01fu.apps.googleusercontent.com")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BACKEND_REDIRECT_URI = os.getenv("BACKEND_REDIRECT_URI",
    "https://hearth-4kqf.onrender.com/auth/callback")
APP_SCHEME           = "hearth-fresh"  # must match app.json scheme


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


# ── Google OAuth — backend flow ───────────────────────────────────────────────

@app.get("/auth/login")
def google_login():
    """Step 1 — redirect user to Google consent screen."""
    scope = " ".join([
        "openid", "profile", "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar",
    ])
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={BACKEND_REDIRECT_URI}"
        "&response_type=code"
        f"&scope={scope.replace(' ', '%20')}"
        "&access_type=offline"
        "&prompt=consent"
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def google_callback(code: str):
    """Step 2 — Google redirects here with code, we exchange for token."""
    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri":  BACKEND_REDIRECT_URI,
                "grant_type":    "authorization_code",
            }
        )
        token_data = token_res.json()

        if "error" in token_data:
            raise HTTPException(status_code=400,
                detail=f"Token error: {token_data.get('error_description', token_data['error'])}")

        # Get user info
        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        )
        user_info = user_res.json()

    # Create stable user_id from email
    email   = user_info.get("email", "")
    user_id = hashlib.md5(email.lower().encode()).hexdigest()[:12]

    # Save token scoped to this user
    token_dir  = os.path.join(cfg.DATA_DIR, "tokens", user_id)
    os.makedirs(token_dir, exist_ok=True)
    token_path = os.path.join(token_dir, "google_token.json")
    with open(token_path, "w") as f:
        json.dump(token_data, f)

    # Save session
    session_dir  = os.path.join(cfg.DATA_DIR, "sessions")
    os.makedirs(session_dir, exist_ok=True)
    session_path = os.path.join(session_dir, f"{user_id}.json")
    user_record  = {
        "user_id": user_id,
        "email":   email,
        "name":    user_info.get("name", email),
        "picture": user_info.get("picture", ""),
    }
    with open(session_path, "w") as f:
        json.dump(user_record, f)

    # Trigger Gmail scan in background — don't block the redirect
    import threading
    def background_scan():
        try:
            from agent.gmail_agent import auto_scan_and_save
            from agent.profile_agent import get_children
            children = get_children(user_id)
            result   = auto_scan_and_save(user_id, children, days_back=14)
            print(f"[gmail scan] user={user_id} new={result.get('new',0)}")
        except Exception as e:
            print(f"[gmail scan error] {e}")
    threading.Thread(target=background_scan, daemon=True).start()

    # Return HTML page that opens the app
    import urllib.parse
    user_json = urllib.parse.quote(json.dumps(user_record))
    token     = urllib.parse.quote(token_data["access_token"])
    deep_link = f"{APP_SCHEME}://auth?user={user_json}&token={token}"
    from fastapi.responses import HTMLResponse
    html = f"""
    <html>
    <head>
      <title>Hearth — Signed In</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <style>
        body {{ font-family: sans-serif; text-align: center; padding: 40px;
               background: #FFF8F0; color: #8B4513; }}
        h1 {{ font-size: 48px; margin-bottom: 8px; }}
        p  {{ color: #A0856B; margin-bottom: 32px; }}
        a  {{ background: #E8734A; color: white; padding: 16px 32px;
              border-radius: 12px; text-decoration: none;
              font-size: 18px; font-weight: bold; }}
      </style>
      <script>window.location.href = "{deep_link}";</script>
    </head>
    <body>
      <h1>🏠</h1>
      <h2>Welcome, {user_record["name"]}!</h2>
      <p>You are signed in to Hearth.</p>
      <a href="{deep_link}">Open Hearth App</a>
      <br><br>
      <p style="font-size:12px">If the app doesn't open automatically, tap the button above.</p>
    </body>
    </html>"""
    return HTMLResponse(html)


@app.post("/auth/register")
def register_user(req: dict):
    """Called by mobile app after OAuth to store token."""
    user    = req.get("user", {})
    user_id = user.get("user_id", "")
    token   = req.get("access_token", "")
    if user_id and token:
        token_dir = os.path.join(cfg.DATA_DIR, "tokens", user_id)
        os.makedirs(token_dir, exist_ok=True)
        with open(os.path.join(token_dir, "google_token.json"), "w") as f:
            json.dump({"access_token": token}, f)
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
    eid = _insert_event(req.user_id, req.child_name, req.event_type,
                        req.event_date, req.event_time, req.notes)
    return {"id": eid, "status": "created"}

@app.delete("/events/{event_id}")
def delete_event(event_id: int, user_id: str):
    ok = _delete_event(user_id, event_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"status": "deleted"}


# ── Tasks (stub) ──────────────────────────────────────────────────────────────
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
    result = run(raw_text=req.transcript, user_id=req.user_id)
    return {"status": "created", "response": result.get("response", "")}


# ── Briefing ──────────────────────────────────────────────────────────────────
@app.get("/briefing")
def get_briefing(user_id: str):
    text     = _build_briefing(user_id)
    today    = _query_today(user_id)
    upcoming = _query_upcoming(user_id, days_ahead=7)
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    return {
        "text":      text,
        "today":     today,
        "tomorrow":  [e for e in upcoming if e["event_date"] == tomorrow],
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
