"""
main.py — Hearth FastAPI entry point
"""

import os, json, hashlib, datetime, threading, re, base64
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import httpx

import hearth_config as cfg
from agent.calendar_agent import (
    init_db, _query_upcoming, _query_today,
    _insert_event, _delete_event, _event_exists,
)
from agent.profile_agent import init_profiles, get_all_profiles, upsert_profile, get_children
from agent.briefing_agent import _build_briefing
from agent.graph import run
from db_migrate import migrate

app = FastAPI(title="Hearth API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID",
    "289231572725-5fn10ulbb5hi6gqohnl1v6ourjsj01fu.apps.googleusercontent.com")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BACKEND_REDIRECT_URI = os.getenv("BACKEND_REDIRECT_URI",
    "https://hearth-4kqf.onrender.com/auth/callback")
APP_SCHEME           = os.getenv("APP_SCHEME", "hearthfresh")

GMAIL_SCOPES = [
    "openid", "profile", "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/cloud-platform",
]


@app.on_event("startup")
def startup():
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    migrate()
    init_db()
    init_profiles()
    print(f"[hearth] started — {cfg.DB_PATH}")


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


# ── Token helpers ─────────────────────────────────────────────────────────────

def _token_dir(user_id: str) -> str:
    d = os.path.join(cfg.DATA_DIR, "tokens", user_id)
    os.makedirs(d, exist_ok=True)
    return d

def _safe_email(email: str) -> str:
    """Convert email to safe filename: foo.bar@gmail.com -> foo_bar_at_gmail_com"""
    return email.replace(".", "_").replace("@", "_at_")

def _save_token(user_id: str, email: str, token_data: dict):
    safe = _safe_email(email)
    path = os.path.join(_token_dir(user_id), f"gmail_{safe}.json")
    with open(path, "w") as f:
        json.dump(token_data, f)

def _load_token(user_id: str, email: str) -> dict | None:
    safe = _safe_email(email)
    path = os.path.join(_token_dir(user_id), f"gmail_{safe}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)

def _list_connected_emails(user_id: str) -> list[str]:
    d = _token_dir(user_id)
    emails = []
    for fname in os.listdir(d):
        if fname.startswith("gmail_") and fname.endswith(".json"):
            safe  = fname[6:-5]  # strip gmail_ and .json
            # Reverse: foo_bar_at_gmail_com -> foo.bar@gmail.com
            email = safe.replace("_at_", "@", 1)
            # Restore dots: split on @ and replace _ with . in each part
            parts = email.split("@")
            email = "@".join(p.replace("_", ".") for p in parts)
            emails.append(email)
    return emails

def _get_fresh_token(user_id: str, email: str) -> str | None:
    """Get a valid access token, refreshing if needed."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    token_data = _load_token(user_id, email)
    if not token_data:
        return None
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
    )
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["access_token"] = creds.token
            _save_token(user_id, email, token_data)
        except Exception as e:
            print(f"[token refresh] {e}")
            return None
    return creds.token if creds.valid else None


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/auth/login")
def google_login(user_id: str = "", add_account: bool = False):
    scope = "%20".join(GMAIL_SCOPES)
    state = json.dumps({"user_id": user_id, "add_account": add_account})
    import urllib.parse
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?client_id=" + GOOGLE_CLIENT_ID +
        "&redirect_uri=" + BACKEND_REDIRECT_URI +
        "&response_type=code"
        "&scope=" + scope +
        "&access_type=offline"
        "&prompt=consent"
        "&include_granted_scopes=false"
        "&state=" + urllib.parse.quote(state)
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def google_callback(code: str, state: str = "{}"):
    import urllib.parse
    try:
        state_data   = json.loads(urllib.parse.unquote(state))
        existing_uid = state_data.get("user_id", "")
        add_account  = state_data.get("add_account", False)
    except:
        existing_uid = ""
        add_account  = False

    async with httpx.AsyncClient() as client:
        token_res = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": BACKEND_REDIRECT_URI, "grant_type": "authorization_code",
        })
        token_data = token_res.json()
        if "error" in token_data:
            raise HTTPException(400, str(token_data))
        user_res  = await client.get("https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": "Bearer " + token_data["access_token"]})
        user_info = user_res.json()

    email   = user_info.get("email", "")
    user_id = existing_uid if (add_account and existing_uid) else \
              hashlib.md5(email.lower().encode()).hexdigest()[:12]

    _save_token(user_id, email, token_data)

    session_dir  = os.path.join(cfg.DATA_DIR, "sessions")
    os.makedirs(session_dir, exist_ok=True)
    session_path = os.path.join(session_dir, user_id + ".json")

    if not add_account or not os.path.exists(session_path):
        user_record = {
            "user_id": user_id, "email": email,
            "name": user_info.get("name", email),
            "picture": user_info.get("picture", ""),
        }
        with open(session_path, "w") as f:
            json.dump(user_record, f)
    else:
        with open(session_path) as f:
            user_record = json.load(f)

    def bg_scan():
        try:
            _scan_single_gmail(user_id, email)
        except Exception as e:
            print(f"[bg scan] {e}")
    threading.Thread(target=bg_scan, daemon=True).start()

    import urllib.parse as up
    user_json = up.quote(json.dumps(user_record))
    token_val = up.quote(token_data["access_token"])
    deep_link = APP_SCHEME + "://auth?user=" + user_json + "&token=" + token_val
    name      = user_record.get("name", "there")
    msg       = "Gmail account added!" if add_account else "Signed in successfully!"
    code      = _generate_auth_code(user_record)

    html  = "<html><head>"
    html += "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    html += "<script>setTimeout(function(){window.location.href='" + deep_link + "';},800);</script>"
    html += "<style>"
    html += "body{font-family:sans-serif;text-align:center;padding:40px;background:#FFF8F0;color:#8B4513}"
    html += "a{background:#E8734A;color:white;padding:16px 32px;border-radius:12px;text-decoration:none;font-size:18px;font-weight:bold;display:block;margin:16px auto;max-width:200px}"
    html += ".code{font-size:48px;font-weight:900;letter-spacing:12px;color:#E8734A;margin:24px 0;padding:20px;background:#fff;border-radius:16px;border:3px solid #E8734A}"
    html += "</style></head><body>"
    html += "<h1>&#127968;</h1><h2>Welcome, " + name + "!</h2>"
    html += "<p>" + msg + "</p>"
    html += "<a href='" + deep_link + "'>Open Hearth App</a>"
    html += "<p style='margin-top:24px;color:#A0856B'>If the app doesn't open, enter this code in Hearth:</p>"
    html += "<div class='code'>" + code + "</div>"
    html += "<p style='font-size:12px;color:#A0856B'>Code expires after one use</p>"
    html += "</body></html>"
    return HTMLResponse(html)


@app.post("/auth/register")
def register_user(req: dict):
    user    = req.get("user", {})
    user_id = user.get("user_id", "")
    email   = user.get("email", "")
    token   = req.get("access_token", "")
    if user_id and token and email:
        _save_token(user_id, email, {"access_token": token})
    return {"status": "registered", "user_id": user_id}


# ── Speech token ──────────────────────────────────────────────────────────────

@app.get("/auth/speech-token")
def get_speech_token(user_id: str):
    """Return a service account token for Speech-to-Text API calls from the app."""
    try:
        import json as _json
        from google.oauth2 import service_account
        sa_json = os.getenv("GOOGLE_SPEECH_SA", "")
        if not sa_json:
            return {"token": None, "error": "Service account not configured"}
        sa_info = _json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        from google.auth.transport.requests import Request as GRequest
        creds.refresh(GRequest())
        return {"token": creds.token, "error": None}
    except Exception as e:
        return {"token": None, "error": str(e)}


# ── Gmail scan ────────────────────────────────────────────────────────────────

def _scan_single_gmail(user_id: str, email: str) -> dict:
    from googleapiclient.discovery import build
    from datetime import date, timedelta
    import anthropic
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_data = _load_token(user_id, email)
    if not token_data:
        return {"new": 0, "skipped": 0, "emails_scanned": 0,
                "error": f"Not authenticated: {email}"}

    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        token_data["access_token"] = creds.token
        _save_token(user_id, email, token_data)

    service = build("gmail", "v1", credentials=creds)
    after   = (date.today() - timedelta(days=14)).strftime("%Y/%m/%d")
    query   = ("(subject:("
               "\"early dismissal\" OR \"no school\" OR \"half day\" OR \"field trip\" "
               "OR \"permission slip\" OR PTA OR \"parent teacher\" OR \"report card\" "
               "OR \"spirit day\" OR \"dress down\" OR \"dress code\" OR \"school closed\" "
               "OR \"school closure\" OR recital OR practice OR tryouts OR \"schedule change\" "
               "OR \"map test\" OR \"special event\" OR \"sign up\" OR newsletter "
               "OR cheerleading OR tumbling OR climbing OR karate OR piano OR music "
               "OR appointment OR checkup OR vaccination OR immunization OR dentist "
               "OR pediatric OR prescription OR refill "
               "OR \"bill due\" OR \"payment due\" OR invoice OR renewal OR insurance "
               "OR maintenance OR delivery OR repair OR \"technician visit\" "
               "OR invitation OR RSVP OR birthday OR party OR celebration "
               "OR reminder OR deadline OR rescheduled OR cancelled)) after:" + after)
    result  = service.users().messages().list(
        userId="me", q=query, maxResults=50).execute()
    messages = result.get("messages", [])
    if not messages:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}

    def extract_body(payload):
        data = payload.get("body", {}).get("data", "")
        if data and "text" in payload.get("mimeType", ""):
            try: return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            except: return ""
        for part in payload.get("parts", []):
            t = extract_body(part)
            if t: return t
        return ""

    emails_data = []
    for msg in messages[:20]:
        try:
            full    = service.users().messages().get(
                userId="me", id=msg["id"], format="full").execute()
            subject = next((h["value"] for h in full["payload"].get("headers", [])
                           if h["name"] == "Subject"), "")
            body    = extract_body(full["payload"])
            if body: emails_data.append({"subject": subject, "body": body[:3000]})
        except: continue

    if not emails_data:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}

    children_str = ", ".join(get_children(user_id)) or "the children"
    today_str    = datetime.date.today().strftime("%A, %B %d, %Y")
    digest       = "".join("--- " + e["subject"] + " ---\n" + e["body"] + "\n"
                           for e in emails_data)

    prompt = ("Hearth assistant. Today: " + today_str +
              ". Children: " + children_str + ".\n"
              "Extract upcoming school events. Return JSON array:\n"
              "[{\"child_name\":\"...\",\"event_type\":\"...\","
              "\"event_date\":\"YYYY-MM-DD\",\"event_time\":null,\"notes\":\"...\"}]\n"
              "Valid event_type: dress_down_day,early_dismissal,recital,field_trip,"
              "special_day,doctor_appointment,sports_game,school_holiday,other\n"
              "Emails:\n" + digest[:5000] + "\nONLY JSON array.")

    import anthropic as ant
    client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    resp   = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=2048,
             messages=[{"role": "user", "content": prompt}])
    raw    = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
    raw    = re.sub(r"\s*```$", "", raw)
    try:    events = json.loads(raw)
    except: events = []

    new = skipped = 0
    for ev in events:
        child = ev.get("child_name", "all")
        etype = ev.get("event_type", "other")
        edate = ev.get("event_date", "")
        if not edate: continue
        if _event_exists(user_id, child, etype, edate): skipped += 1; continue
        _insert_event(user_id, child, etype, edate, ev.get("event_time"), ev.get("notes"))
        new += 1

    return {"new": new, "skipped": skipped, "emails_scanned": len(emails_data), "error": None}


@app.post("/gmail/scan")
def gmail_scan(user_id: str):
    emails = _list_connected_emails(user_id)
    if not emails:
        return {"new": 0, "skipped": 0, "error": "No Gmail accounts connected"}
    return _scan_single_gmail(user_id, emails[0])


@app.post("/gmail/scan-all")
def gmail_scan_all(user_id: str):
    emails = _list_connected_emails(user_id)
    if not emails:
        return {"new": 0, "skipped": 0, "accounts_scanned": 0, "error": "No accounts"}
    total_new = total_skipped = 0
    for email in emails:
        r = _scan_single_gmail(user_id, email)
        total_new     += r.get("new", 0)
        total_skipped += r.get("skipped", 0)
    return {"new": total_new, "skipped": total_skipped,
            "accounts_scanned": len(emails), "error": None}


@app.get("/connected-gmails")
def connected_gmails(user_id: str):
    return {"emails": _list_connected_emails(user_id)}


# ── Events ────────────────────────────────────────────────────────────────────

@app.get("/events")
def get_events(user_id: str, days_ahead: int = 14):
    return _query_upcoming(user_id, days_ahead=days_ahead)

@app.get("/events/today")
def get_today(user_id: str):
    return _query_today(user_id)

class EventRequest(BaseModel):
    user_id: str; child_name: str; event_type: str; event_date: str
    event_time: Optional[str] = None; notes: Optional[str] = None

@app.post("/events")
def add_event(req: EventRequest):
    eid = _insert_event(req.user_id, req.child_name, req.event_type,
                        req.event_date, req.event_time, req.notes)
    return {"id": eid, "status": "created"}

@app.delete("/events/{event_id}")
def delete_event(event_id: int, user_id: str):
    if not _delete_event(user_id, event_id):
        raise HTTPException(404, "Event not found")
    return {"status": "deleted"}


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.get("/tasks")
def get_tasks(user_id: str, status: str = "pending"):
    return []

@app.post("/tasks/{task_id}/send")
def send_task(task_id: int, user_id: str): return {"status": "sent"}

@app.post("/tasks/{task_id}/snooze")
def snooze_task(task_id: int, user_id: str, days: int = 3): return {"status": "snoozed"}

@app.post("/tasks/{task_id}/done")
def done_task(task_id: int, user_id: str): return {"status": "done"}

class VoiceRequest(BaseModel):
    user_id: str; transcript: str

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
    return {"text": text, "today": today,
            "tomorrow": [e for e in upcoming if e["event_date"] == tomorrow],
            "this_week": upcoming, "tasks": []}


# ── Agent ─────────────────────────────────────────────────────────────────────

class AgentRequest(BaseModel):
    user_id: str; raw_text: str

@app.post("/agent")
def agent_chat(req: AgentRequest):
    result = run(raw_text=req.raw_text, user_id=req.user_id)
    return {"response": result.get("response", "")}


# ── Profiles ──────────────────────────────────────────────────────────────────

@app.get("/profiles")
def get_profiles(user_id: str):
    return get_all_profiles(user_id)

class ProfileRequest(BaseModel):
    user_id: str; name: str
    grade: Optional[str]=None; school: Optional[str]=None
    activities: Optional[str]=None; notes: Optional[str]=None

@app.post("/profiles")
def save_profile(req: ProfileRequest):
    upsert_profile(req.user_id, req.name, req.grade,
                   req.school, req.activities, req.notes)
    return {"status": "saved"}


# ── Debug ─────────────────────────────────────────────────────────────────────

@app.get("/debug/token")
def debug_token(user_id: str):
    emails = _list_connected_emails(user_id)
    return {"connected_emails": emails, "token_dir": _token_dir(user_id)}




@app.post("/transcribe")
async def transcribe_audio(user_id: str, request: Request):
    """
    Receive base64 M4A audio from app.
    Convert M4A to FLAC using ffmpeg.
    Transcribe using Google Speech-to-Text SDK with service account.
    """
    import tempfile, subprocess
    from google.cloud import speech
    from google.oauth2 import service_account

    try:
        # Parse JSON body with base64 audio
        body      = await request.json()
        audio_b64 = body.get("audio", "")
        if not audio_b64:
            return {"transcript": "", "error": "No audio received"}

        # Decode base64 to M4A bytes
        m4a_bytes = base64.b64decode(audio_b64)
        print(f"[transcribe] received {len(m4a_bytes)} bytes M4A")

        # Write M4A to temp file
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
            tmp.write(m4a_bytes)
            m4a_path = tmp.name

        # Convert M4A to FLAC using ffmpeg
        flac_path = m4a_path.replace(".m4a", ".flac")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", m4a_path,
             "-ar", "16000", "-ac", "1", "-c:a", "flac", flac_path],
            capture_output=True, text=True, timeout=30
        )

        # Clean up M4A
        os.unlink(m4a_path)

        if result.returncode != 0:
            print(f"[transcribe] ffmpeg error: {result.stderr[-200:]}")
            return {"transcript": "", "error": "Audio conversion failed"}

        with open(flac_path, "rb") as f:
            flac_bytes = f.read()
        os.unlink(flac_path)
        print(f"[transcribe] converted to {len(flac_bytes)} bytes FLAC")

        # Load service account
        sa_json = os.getenv("GOOGLE_SPEECH_SA", "")
        if not sa_json:
            return {"transcript": "", "error": "Speech service account not configured"}
        sa_info = json.loads(sa_json)
        creds   = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

        # Transcribe
        client = speech.SpeechClient(credentials=creds)
        audio  = speech.RecognitionAudio(content=flac_bytes)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.FLAC,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="default",
        )
        response   = client.recognize(config=config, audio=audio)
        transcript = " ".join(
            r.alternatives[0].transcript for r in response.results
        ).strip()
        print(f"[transcribe] transcript: '{transcript}'")
        return {"transcript": transcript, "error": None}

    except Exception as e:
        print(f"[transcribe] exception: {e}")
        return {"transcript": "", "error": str(e)}


@app.get("/debug/ffmpeg")
def debug_ffmpeg():
    import subprocess
    try:
        result = subprocess.run(["ffmpeg", "-version"], 
                              capture_output=True, text=True, timeout=5)
        return {"available": True, "version": result.stdout.split("\n")[0]}
    except Exception as e:
        return {"available": False, "error": str(e)}


import random, string

# In-memory code store {code: user_record}
_auth_codes: dict = {}

def _generate_auth_code(user_record: dict) -> str:
    code = "".join(random.choices(string.digits, k=6))
    _auth_codes[code] = user_record
    return code

@app.get("/auth/code")
def get_user_by_code(code: str):
    user_record = _auth_codes.get(code)
    if not user_record:
        return {"user": None, "error": "Invalid or expired code"}
    del _auth_codes[code]  # one-time use
    return {"user": user_record, "error": None}


@app.get("/debug/token-keys")
def debug_token_keys(user_id: str, email: str):
    token_data = _load_token(user_id, email)
    if not token_data:
        return {"exists": False}
    return {"exists": True, "keys": list(token_data.keys()),
            "has_refresh": "refresh_token" in token_data}


@app.get("/debug/test-refresh")
def debug_test_refresh(user_id: str, email: str):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        token_data = _load_token(user_id, email)
        if not token_data:
            return {"error": "No token found"}
        creds = Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=GMAIL_SCOPES,
        )
        return {
            "valid": creds.valid,
            "has_refresh": bool(creds.refresh_token),
            "has_client_id": bool(creds.client_id),
            "has_client_secret": bool(creds.client_secret),
            "client_id_preview": GOOGLE_CLIENT_ID[:20],
            "client_secret_set": bool(GOOGLE_CLIENT_SECRET),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/gmail/scan-debug")
async def gmail_scan_debug(user_id: str):
    """Scan and return raw email subjects for debugging."""
    from googleapiclient.discovery import build
    from datetime import date, timedelta
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    emails_found = _list_connected_emails(user_id)
    if not emails_found:
        return {"error": "No Gmail connected"}

    email = emails_found[0]
    token_data = _load_token(user_id, email)
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())

    service = build("gmail", "v1", credentials=creds)
    after   = (date.today() - timedelta(days=30)).strftime("%Y/%m/%d")
    query   = ("(subject:(dismissal OR recital OR newsletter OR \"no school\" "
               "OR \"dress down\" OR \"field trip\" OR \"early release\" "
               "OR \"school holiday\" OR \"picture day\" OR \"early dismissal\" "
               "OR \"school closure\" OR \"parent teacher\" OR \"sign up\" "
               "OR \"special event\" OR rock OR climbing OR tumbling "
               "OR cheerleading OR karate OR piano OR music OR \"after school\")) after:" + after)
    result   = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    messages = result.get("messages", [])
    subjects = []
    for msg in messages[:10]:
        full    = service.users().messages().get(userId="me", id=msg["id"], format="metadata",
                  metadataHeaders=["Subject"]).execute()
        subject = next((h["value"] for h in full["payload"].get("headers", [])
                       if h["name"] == "Subject"), "no subject")
        subjects.append(subject)
    return {"emails_found": len(messages), "subjects": subjects}


# ── Google Calendar ────────────────────────────────────────────────────────────

def _get_gcal_service(user_id: str, email: str):
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    token_data = _load_token(user_id, email)
    if not token_data:
        return None
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["access_token"] = creds.token
            _save_token(user_id, email, token_data)
        except Exception as e:
            print(f"[gcal] refresh failed: {e}")
            return None
    return build("calendar", "v3", credentials=creds)


def _write_to_gcal(user_id: str, email: str, summary: str,
                   event_date: str, event_time: str = None,
                   description: str = None) -> str:
    from datetime import datetime, timedelta
    service = _get_gcal_service(user_id, email)
    if not service:
        return None
    if event_time:
        start_dt = f"{event_date}T{event_time}:00"
        try:
            dt  = datetime.fromisoformat(start_dt)
            end = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:00")
        except:
            end = start_dt
        start = {"dateTime": start_dt, "timeZone": "America/New_York"}
        end_t = {"dateTime": end,      "timeZone": "America/New_York"}
    else:
        start = {"date": event_date}
        end_t = {"date": event_date}
    body = {"summary": summary, "start": start, "end": end_t}
    if description:
        body["description"] = description
    try:
        result = service.events().insert(calendarId="primary", body=body).execute()
        return result.get("id")
    except Exception as e:
        print(f"[gcal write] {e}")
        return None


def _sync_gcal_to_db(user_id: str, email: str) -> dict:
    from datetime import datetime, timedelta, timezone
    service = _get_gcal_service(user_id, email)
    if not service:
        return {"new": 0, "error": "Not authenticated"}
    now    = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    try:
        result = service.events().list(
            calendarId="primary", timeMin=now, timeMax=cutoff,
            maxResults=50, singleEvents=True, orderBy="startTime",
        ).execute()
    except Exception as e:
        return {"new": 0, "error": str(e)}
    gcal_events = result.get("items", [])
    new = skipped = 0
    from agent.calendar_agent import _conn, _event_exists, _insert_event
    for ev in gcal_events:
        summary = ev.get("summary", "")
        if not summary:
            continue
        start      = ev.get("start", {})
        event_date = start.get("date") or start.get("dateTime", "")[:10]
        event_time = None
        if "dateTime" in start:
            try:
                dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
                event_time = dt.strftime("%H:%M")
            except:
                pass
        gcal_id = ev.get("id", "")
        with _conn() as c:
            exists = c.execute(
                "SELECT id FROM events WHERE user_id=? AND gcal_event_id=?",
                (user_id, gcal_id)).fetchone()
        if exists:
            skipped += 1
            continue
        # Simple keyword classification
        s = summary.lower()
        if any(k in s for k in ["doctor","dentist","appointment","checkup","vaccination"]):
            etype = "doctor_appointment"
        elif any(k in s for k in ["dismissal","no school","half day","school closed","holiday"]):
            etype = "early_dismissal"
        elif any(k in s for k in ["recital","performance","concert","show"]):
            etype = "recital"
        elif any(k in s for k in ["field trip","trip"]):
            etype = "field_trip"
        elif any(k in s for k in ["swim","gymnastic","karate","soccer","cheer","tumbl","rock climb","sport","practice","game"]):
            etype = "sports_game"
        elif any(k in s for k in ["piano","music","art","tutor","drama","activity","class"]):
            etype = "activity"
        elif any(k in s for k in ["bill","payment","invoice","due","renewal","insurance"]):
            etype = "bill"
        else:
            etype = "other"
        # Insert
        with _conn() as c:
            c.execute(
                "INSERT INTO events(user_id,child_name,event_type,event_date,"
                "event_time,notes,gcal_event_id) VALUES(?,?,?,?,?,?,?)",
                (user_id, "all", etype, event_date, event_time, summary, gcal_id))
            c.commit()
        new += 1
    return {"new": new, "skipped": skipped, "error": None}


@app.post("/gcal/sync")
def gcal_sync(user_id: str):
    emails = _list_connected_emails(user_id)
    if not emails:
        return {"new": 0, "error": "No Gmail connected"}
    total_new = 0
    for email in emails:
        r = _sync_gcal_to_db(user_id, email)
        total_new += r.get("new", 0)
    return {"new": total_new, "error": None}


@app.post("/gcal/write")
def gcal_write(req: dict):
    user_id    = req.get("user_id", "")
    summary    = req.get("summary", "")
    event_date = req.get("event_date", "")
    event_time = req.get("event_time")
    emails     = _list_connected_emails(user_id)
    if not emails or not summary or not event_date:
        return {"gcal_id": None, "error": "Missing required fields"}
    gcal_id = _write_to_gcal(emails[0], emails[0], summary, event_date,
                             event_time, req.get("description"))
    return {"gcal_id": gcal_id, "error": None if gcal_id else "Write failed"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=cfg.API_PORT, reload=True)
