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

# In-memory auth codes
_auth_codes: dict = {}



@app.on_event("startup")
def startup():
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    migrate()
    init_db()
    init_profiles()
    _init_push_tokens()
    _init_camps()
    _upgrade_camps_table()
    _init_camp_tasks()
    _init_preferences()
    _upgrade_camps_table_v2()
    _init_camp_checklist()
    _init_prescriptions()
    # Start background scheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    def run_daily_briefing():
        from agent.calendar_agent import _conn
        from datetime import date
        with _conn() as c:
            user_ids = [r[0] for r in c.execute(
                "SELECT DISTINCT user_id FROM push_tokens").fetchall()]
        for user_id in user_ids:
            tokens = _get_push_tokens(user_id)
            if not tokens: continue
            today_events = _query_today(user_id)
            if not today_events:
                body = "No events today. Enjoy your day!"
            else:
                parts = []
                for ev in today_events[:3]:
                    note   = ev.get("notes") or ev.get("event_type","").replace("_"," ").title()
                    time   = " at " + ev["event_time"] if ev.get("event_time") else ""
                    child  = ev.get("child_name","")
                    prefix = (child + ": ") if child and child != "all" else ""
                    parts.append(prefix + note + time)
                body = ", ".join(parts)
                if len(today_events) > 3:
                    body += f" +{len(today_events)-3} more"
            _send_push(tokens, "🏠 Hearth Morning Briefing", body)

    def run_nudges():
        from agent.calendar_agent import _conn
        from datetime import date, timedelta
        today    = date.today().isoformat()
        in_2days = (date.today() + timedelta(days=2)).isoformat()
        with _conn() as c:
            user_ids = [r[0] for r in c.execute(
                "SELECT DISTINCT user_id FROM push_tokens").fetchall()]
        for user_id in user_ids:
            tokens = _get_push_tokens(user_id)
            if not tokens: continue
            with _conn() as c:
                day_of  = [dict(r) for r in c.execute(
                    "SELECT * FROM events WHERE user_id=? AND event_date=? AND nudge_sent_day=0",
                    (user_id, today)).fetchall()]
                two_day = [dict(r) for r in c.execute(
                    "SELECT * FROM events WHERE user_id=? AND event_date=? AND nudge_sent_48h=0",
                    (user_id, in_2days)).fetchall()]
            for ev in day_of:
                note   = ev.get("notes") or ev.get("event_type","").replace("_"," ").title()
                time   = " at " + ev["event_time"] if ev.get("event_time") else ""
                child  = ev.get("child_name","")
                prefix = (child + ": ") if child and child != "all" else ""
                _send_push(tokens, "📅 Today", prefix + note + time)
                with _conn() as c:
                    c.execute("UPDATE events SET nudge_sent_day=1 WHERE id=?", (ev["id"],))
                    c.commit()
            for ev in two_day:
                note   = ev.get("notes") or ev.get("event_type","").replace("_"," ").title()
                child  = ev.get("child_name","")
                prefix = (child + ": ") if child and child != "all" else ""
                _send_push(tokens, "⏰ In 2 days", prefix + note)
                with _conn() as c:
                    c.execute("UPDATE events SET nudge_sent_48h=1 WHERE id=?", (ev["id"],))
                    c.commit()

    scheduler = BackgroundScheduler(timezone=pytz.timezone("America/New_York"))
    scheduler.add_job(run_daily_briefing, CronTrigger(hour=7, minute=0))
    scheduler.add_job(run_nudges,         CronTrigger(hour=7, minute=5))
    scheduler.start()
    print("[hearth] scheduler started")
    print(f"[hearth] started — {cfg.DB_PATH}")


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


# ── Token helpers ──────────────────────────────────────────────────────────────

def _token_dir(user_id: str) -> str:
    d = os.path.join(cfg.DATA_DIR, "tokens", user_id)
    os.makedirs(d, exist_ok=True)
    return d

def _safe_email(email: str) -> str:
    return email.replace(".", "_").replace("@", "_at_")

def _save_token(user_id: str, email: str, token_data: dict):
    safe = _safe_email(email)
    path = os.path.join(_token_dir(user_id), "gmail_" + safe + ".json")
    with open(path, "w") as f:
        json.dump(token_data, f)

def _load_token(user_id: str, email: str):
    safe = _safe_email(email)
    path = os.path.join(_token_dir(user_id), "gmail_" + safe + ".json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)

def _list_connected_emails(user_id: str):
    d = _token_dir(user_id)
    emails = []
    for fname in os.listdir(d):
        if fname.startswith("gmail_") and fname.endswith(".json"):
            safe  = fname[6:-5]
            email = safe.replace("_at_", "@", 1)
            parts = email.split("@")
            email = "@".join(p.replace("_", ".") for p in parts)
            emails.append(email)
    return emails

def _get_fresh_token(user_id: str, email: str):
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
    if not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
                token_data["access_token"] = creds.token
                _save_token(user_id, email, token_data)
            except Exception as e:
                print("[token refresh] " + str(e))
                return None
        else:
            return None
    return creds.token if creds.valid else None


# ── Google Calendar helpers ────────────────────────────────────────────────────

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
        scopes=GMAIL_SCOPES,
    )
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["access_token"] = creds.token
            _save_token(user_id, email, token_data)
        except Exception as e:
            print(f"[gcal] refresh failed for {email} user {user_id}: {e}")
            if "invalid_grant" in str(e):
                import os as _os
                safe = email.replace(".", "_").replace("@", "_at_")
                path = _os.path.join(_token_dir(user_id), "gmail_" + safe + ".json")
                if _os.path.exists(path): _os.remove(path)
                print(f"[gcal] deleted expired token for {email}")
            return None
    return build("calendar", "v3", credentials=creds)


def _gcal_event_exists(service, summary: str, event_date: str) -> bool:
    try:
        day_start = event_date + "T00:00:00Z"
        day_end   = event_date + "T23:59:59Z"
        result    = service.events().list(
            calendarId="primary",
            timeMin=day_start, timeMax=day_end,
            q=summary[:30], singleEvents=True
        ).execute()
        for ev in result.get("items", []):
            if summary.lower()[:20] in ev.get("summary", "").lower():
                return True
    except:
        pass
    return False


def _write_to_gcal(user_id: str, email: str, summary: str,
                   event_date: str, event_time: str = None,
                   description: str = None):
    from datetime import datetime, timedelta
    service = _get_gcal_service(user_id, email)
    if not service:
        return None
    if _gcal_event_exists(service, summary, event_date):
        print("[gcal] duplicate skipped: " + summary)
        return "duplicate"
    if event_time:
        start_dt = event_date + "T" + event_time + ":00"
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
        print("[gcal write] " + str(e))
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
    from agent.calendar_agent import _conn
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
        s = summary.lower()
        if any(k in s for k in ["doctor","dentist","appointment","checkup","vaccination"]):
            etype = "doctor_appointment"
        elif any(k in s for k in ["dismissal","no school","half day","school closed","holiday"]):
            etype = "early_dismissal"
        elif any(k in s for k in ["recital","performance","concert","show","art show"]):
            etype = "recital"
        elif any(k in s for k in ["field trip","trip"]):
            etype = "field_trip"
        elif any(k in s for k in ["swim","gymnastic","karate","soccer","cheer","tumbl","rock climb","sport","practice","game"]):
            etype = "sports_game"
        elif any(k in s for k in ["piano","music","art","tutor","drama","activity","class"]):
            etype = "activity"
        elif any(k in s for k in ["teacher gift","fundraiser","book fair","pta donation",
                                      "class gift","contribution","donate","spirit wear"]):
            etype = "school_fundraiser"
        elif any(k in s for k in ["bill","payment","invoice","due","renewal","insurance"]):
            etype = "bill"
        else:
            etype = "other"
        with _conn() as c:
            c.execute(
                "INSERT INTO events(user_id,child_name,event_type,event_date,"
                "event_time,notes,gcal_event_id) VALUES(?,?,?,?,?,?,?)",
                (user_id, "all", etype, event_date, event_time, summary, gcal_id))
            c.commit()
        new += 1
    return {"new": new, "skipped": skipped, "error": None}


# ── DB helper for tasks/patterns ───────────────────────────────────────────────

def _tasks_conn():
    from agent.calendar_agent import _conn
    return _conn()


# ── Auth ───────────────────────────────────────────────────────────────────────

import random, string

def _generate_auth_code(user_record: dict) -> str:
    code = "".join(random.choices(string.digits, k=6))
    _auth_codes[code] = user_record
    return code


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
            _sync_gcal_to_db(user_id, email)
            _scan_sent_mail(user_id, email, days_back=90)
            _scan_bill_emails(user_id, email, days_back=30)
            _evaluate_patterns(user_id)
        except Exception as e:
            print("[bg scan] " + str(e))
    threading.Thread(target=bg_scan, daemon=True).start()

    import urllib.parse as up
    user_json = up.quote(json.dumps(user_record))
    token_val = up.quote(token_data["access_token"])
    deep_link = APP_SCHEME + "://auth?user=" + user_json + "&token=" + token_val
    name      = user_record.get("name", "there")
    msg       = "Gmail account added!" if add_account else "Signed in to Hearth!"
    code_6    = _generate_auth_code(user_record)

    html  = "<html><head>"
    html += "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    html += "<script>setTimeout(function(){window.location.href='" + deep_link + "';},800);</script>"
    html += "<style>body{font-family:sans-serif;text-align:center;padding:40px;background:#FFF8F0;color:#8B4513}"
    html += "a{background:#E8734A;color:white;padding:16px 32px;border-radius:12px;text-decoration:none;font-size:18px;font-weight:bold;display:block;margin:16px auto;max-width:200px}"
    html += ".code{font-size:48px;font-weight:900;letter-spacing:12px;color:#E8734A;margin:24px 0;padding:20px;background:#fff;border-radius:16px;border:3px solid #E8734A}"
    html += "</style></head><body>"
    html += "<h1>&#127968;</h1><h2>Welcome, " + name + "!</h2>"
    html += "<p>" + msg + "</p>"
    html += "<a href='" + deep_link + "'>Open Hearth App</a>"
    html += "<p style='margin-top:24px;color:#A0856B'>Or enter this code in Hearth:</p>"
    html += "<div class='code'>" + code_6 + "</div>"
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


@app.get("/auth/code")
def get_user_by_code(code: str):
    user_record = _auth_codes.get(code)
    if not user_record:
        return {"user": None, "error": "Invalid or expired code"}
    del _auth_codes[code]
    return {"user": user_record, "error": None}


@app.get("/auth/speech-token")
def get_speech_token(user_id: str):
    try:
        from google.oauth2 import service_account
        sa_json = os.getenv("GOOGLE_SPEECH_SA", "")
        if not sa_json:
            return {"token": None, "error": "Service account not configured"}
        sa_info = json.loads(sa_json)
        creds   = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        from google.auth.transport.requests import Request as GRequest
        creds.refresh(GRequest())
        return {"token": creds.token, "error": None}
    except Exception as e:
        return {"token": None, "error": str(e)}


# ── Gmail scan ─────────────────────────────────────────────────────────────────

def _scan_single_gmail(user_id: str, email: str) -> dict:
    from googleapiclient.discovery import build
    from datetime import date, timedelta
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_data = _load_token(user_id, email)
    if not token_data:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": "Not authenticated"}

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
    after   = (date.today() - timedelta(days=30)).strftime("%Y/%m/%d")
    # Pass 1 — subject keyword match
    query1 = ("after:" + after + " (subject:(reminder OR newsletter OR RSVP OR "
              "cheerleading OR performance OR appointment OR invoice OR payment OR "
              "festival OR recital OR dismissal OR activity OR birthday OR show OR "
              "concert OR \"no school\" OR gymnastics OR class OR trial OR "
              "\"art show\" OR \"picture day\" OR \"early dismissal\" OR "
              "\"field trip\" OR \"dress down\" OR \"spirit day\" OR "
              "\"school closed\" OR \"half day\" OR \"parent teacher\" OR "
              "\"summer camp\" OR \"camp registration\" OR \"camp enrollment\" OR "
              "\"camp forms\" OR \"camp orientation\" OR camper OR Campanion))"
    )
    # Pass 2 — recent emails regardless of subject
    query2 = "after:" + after

    result1  = service.users().messages().list(userId="me", q=query1, maxResults=30).execute()
    result2  = service.users().messages().list(userId="me", q=query2, maxResults=20).execute()

    # Merge and deduplicate by message ID
    seen_ids = set()
    messages = []
    for msg in result1.get("messages", []) + result2.get("messages", []):
        if msg["id"] not in seen_ids:
            seen_ids.add(msg["id"])
            messages.append(msg)

    if not messages:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}
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
    for msg in messages[:30]:
        try:
            full    = service.users().messages().get(
                userId="me", id=msg["id"], format="full").execute()
            headers = full["payload"].get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
            date_h  = next((h["value"] for h in headers if h["name"] == "Date"), "")
            body    = extract_body(full["payload"])
            if body:
                emails_data.append({"subject": subject, "body": body[:5000], "received": date_h})
        except: continue

    if not emails_data:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}

    children_str = ", ".join(get_children(user_id)) or "the children"
    today_str    = date.today().strftime("%A, %B %d, %Y")
    today_iso    = date.today().isoformat()
    digest       = "".join(
        "--- " + e["subject"] + " (received: " + e.get("received","") + ") ---\n" + e["body"] + "\n"
        for e in emails_data)

    import anthropic as ant
    prompt = (
        "Hearth assistant. Today: " + today_str + " (" + today_iso + "). Children: " + children_str + ".\n"
        "Extract upcoming school events. Use the email received date to resolve relative dates like 'tomorrow'.\n"
        "Return JSON array:\n"
        "[{\"child_name\":\"...\",\"event_type\":\"...\","
        "\"event_date\":\"YYYY-MM-DD\",\"event_time\":null,\"notes\":\"...\"}]\n"
        "Use school_fundraiser for: teacher gifts, PTA donations, book fairs, class contributions, spirit wear, fundraising requests from school.\n""Use bill ONLY for: utility bills, credit cards, insurance invoices, subscription renewals from companies — NOT school contribution requests.\n""Valid event_type: dress_down_day,early_dismissal,recital,field_trip,"
        "special_day,doctor_appointment,sports_game,school_holiday,activity,bill,school_fundraiser,other\n"
        "Emails:\n" + digest[:5000] + "\nONLY JSON array."
    )

    client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    resp   = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=4096,
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
        event_id = _insert_event(user_id, child, etype, edate, ev.get("event_time"), ev.get("notes"))
        # Write to Google Calendar
        try:
            gcal_summary = ev.get("notes") or etype.replace("_", " ").title()
            if child and child != "all":
                gcal_summary = child + " - " + gcal_summary
            emails_list = _list_connected_emails(user_id)
            if emails_list:
                svc = _get_gcal_service(user_id, emails_list[0])
                if svc and not _gcal_event_exists(svc, gcal_summary, edate):
                    gcal_id = _write_to_gcal(user_id, emails_list[0], gcal_summary,
                                             edate, ev.get("event_time"), ev.get("notes"))
                    if gcal_id and gcal_id != "duplicate":
                        from agent.calendar_agent import _conn
                        with _conn() as c:
                            c.execute("UPDATE events SET gcal_event_id=? WHERE id=?",
                                      (gcal_id, event_id))
                            c.commit()
        except Exception as e:
            print("[gcal scan write] " + str(e))
        new += 1

    return {"new": new, "skipped": skipped, "emails_scanned": len(emails_data), "error": None}


# ── Actions — Sent mail scanner, bill scanner, pattern evaluator ───────────────

PHARMACY_DOMAINS    = ["cvs.com","walgreens.com","riteaid.com","duanereade.com",
                       "costco.com","walmart.com","kroger.com","mailmymed.com",
                       "expressscripts.com","caremark.com","optumrx.com"]
INSURANCE_DOMAINS   = ["aetna.com","cigna.com","bcbs.com","bcbsnc.com","bcbsma.com",
                       "uhc.com","unitedhealthcare.com","humana.com","anthem.com",
                       "emblemhealth.com","oxford.com","magellanhealth.com"]
MEDICAL_KEYWORDS_DOMAIN = ["medical","health","clinic","hospital","pediatric",
                            "dental","dentist","physician","surgery","orthopedic",
                            "dermatology","cardiology","neurology","oncology",
                            "radiology","pediatrics","familymed","urgentcare"]
PRESCRIPTION_SUBJECTS = ["refill","prescription ready","medication ready",
                          "rx ready","your prescription","pickup ready"]
DOCTOR_SUBJECTS       = ["appointment confirmation","your appointment",
                          "visit reminder","appointment reminder","upcoming appointment"]
INSURANCE_SUBJECTS    = ["explanation of benefits","eob","claim processed",
                          "claim approved","claim denied","reimbursement","your claim"]

def _is_pharmacy(addr: str) -> bool:
    return any(d in addr for d in PHARMACY_DOMAINS)

def _is_insurance_health(addr: str) -> bool:
    return any(d in addr for d in INSURANCE_DOMAINS)

def _is_medical(addr: str) -> bool:
    return any(k in addr for k in MEDICAL_KEYWORDS_DOMAIN)

def _classify_pattern(subject: str, from_addr: str, to_addr: str):
    s   = subject.lower()
    frm = from_addr.lower()
    to  = to_addr.lower()
    if _is_pharmacy(frm) and any(k in s for k in PRESCRIPTION_SUBJECTS):
        return ("prescription", 30)
    if _is_insurance_health(frm) and any(k in s for k in INSURANCE_SUBJECTS):
        return ("insurance", 14)
    if _is_medical(frm) and any(k in s for k in DOCTOR_SUBJECTS):
        return ("doctor", 90)
    if _is_pharmacy(to) and any(k in s for k in ["refill","prescription","medication"]):
        return ("prescription", 30)
    if _is_medical(to) and any(k in s for k in ["appointment","refill","prescription"]):
        return ("doctor", 90)
    return (None, None)

def _scan_sent_mail(user_id: str, email: str, days_back: int = 90) -> dict:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from datetime import date, timedelta

    token_data = _load_token(user_id, email)
    if not token_data:
        return {"patterns_found": 0, "error": "Not authenticated"}

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
    after   = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    med_query = (
        "in:sent after:" + after + " "
        "(refill OR prescription OR medication OR "
        "appointment OR \"follow up\" OR \"follow-up\" OR "
        "claim OR reimbursement OR insurance OR "
        "pharmacy OR CVS OR Walgreens)"
    )

    result   = service.users().messages().list(userId="me", q=med_query, maxResults=50).execute()
    messages = result.get("messages", [])

    def get_header(headers, name):
        return next((h["value"] for h in headers if h["name"] == name), "")

    patterns_found = 0
    for msg in messages[:30]:
        try:
            full    = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["Subject", "To", "Date"]).execute()
            headers = full["payload"].get("headers", [])
            subject = get_header(headers, "Subject")
            to      = get_header(headers, "To")
            date_h  = get_header(headers, "Date")
            if not to or not subject:
                continue

            frm   = get_header(headers, "From")
            ptype, freq = _classify_pattern(subject, frm, to)
            if not ptype:
                continue

            with _tasks_conn() as c:
                existing = c.execute(
                    "SELECT id, confidence_score FROM patterns "
                    "WHERE user_id=? AND pattern_type=? AND contact_email=?",
                    (user_id, ptype, to)).fetchone()
                if existing:
                    c.execute(
                        "UPDATE patterns SET confidence_score=MIN(1.0, confidence_score+0.2),"
                        "last_action_date=? WHERE id=?",
                        (date_h, existing["id"]))
                    c.commit()
                else:
                    next_due = (date.today() + timedelta(days=freq)).isoformat()
                    c.execute(
                        "INSERT INTO patterns(user_id,pattern_type,contact_email,"
                        "contact_name,keywords,frequency_days,last_action_date,"
                        "next_due_date,confidence_score) VALUES(?,?,?,?,?,?,?,?,?)",
                        (user_id, ptype, to,
                         to.split("<")[0].strip() or to,
                         subject[:100], freq, date_h, next_due, 0.5))
                    c.commit()
                    patterns_found += 1
        except Exception as e:
            print("[sent scan] " + str(e))
            continue

    return {"patterns_found": patterns_found, "error": None}


def _scan_bill_emails(user_id: str, email: str, days_back: int = 30) -> dict:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from datetime import date, timedelta
    import anthropic as ant

    token_data = _load_token(user_id, email)
    if not token_data:
        return {"bills_found": 0, "error": "Not authenticated"}

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
    after   = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    bill_query = (
        "after:" + after + " "
        "(subject:(invoice OR outstanding OR \"payment due\" OR \"bill is ready\" "
        "OR \"amount due\" OR \"statement available\" OR \"your bill\" "
        "OR \"balance due\" OR \"minimum payment\" OR \"auto pay\" "
        "OR autopay OR pledge OR \"payment reminder\" OR \"amount owed\"))"
    )
    result   = service.users().messages().list(userId="me", q=bill_query, maxResults=30).execute()
    messages = result.get("messages", [])

    def extract_body(payload):
        data = payload.get("body", {}).get("data", "")
        if data and "text" in payload.get("mimeType", ""):
            try: return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            except: return ""
        for part in payload.get("parts", []):
            t = extract_body(part)
            if t: return t
        return ""

    bills_found = 0
    for msg in messages[:30]:
        try:
            full    = service.users().messages().get(
                userId="me", id=msg["id"], format="full").execute()
            headers = full["payload"].get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
            sender  = next((h["value"] for h in headers if h["name"] == "From"), "")
            body    = extract_body(full["payload"])[:3000]
            if not body:
                continue

            prompt = (
                "Extract bill payment details from this email.\n"
                "Subject: " + subject + "\nFrom: " + sender + "\nBody: " + body[:1000] + "\n"
                "Return ONLY JSON: {\"company\": \"...\", \"amount\": \"$XX.XX or null\","
                "\"due_date\": \"YYYY-MM-DD or null\","
                "\"payment_url\": \"https://... or null\","
                "\"company_login_url\": \"https://... or null\","
                "\"is_bill\": true or false}"
            )

            client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
            resp   = client.messages.create(
                model=cfg.CLAUDE_MODEL, max_tokens=300,
                messages=[{"role": "user", "content": prompt}])
            raw    = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
            raw    = re.sub(r"\s*```$", "", raw)
            data   = json.loads(raw)

            if not data.get("is_bill") or not data.get("company"):
                continue
            if not data.get("due_date"):
                continue

            with _tasks_conn() as c:
                existing = c.execute(
                    "SELECT id FROM tasks WHERE user_id=? AND task_type='bill' "
                    "AND contact_name=? AND due_date=?",
                    (user_id, data["company"], data["due_date"])).fetchone()
                if existing:
                    continue

                title = data["company"]
                if data.get("amount"):
                    title += " " + data["amount"]

                c.execute(
                    "INSERT INTO tasks(user_id,task_type,title,status,due_date,"
                    "payment_url,amount,company_login_url,contact_name) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (user_id, "bill", title, "pending", data["due_date"],
                     data.get("payment_url"), data.get("amount"),
                     data.get("company_login_url"), data["company"]))
                c.commit()
                bills_found += 1
        except Exception as e:
            print("[bill scan] " + str(e))
            continue

    return {"bills_found": bills_found, "error": None}


def _evaluate_patterns(user_id: str) -> dict:
    from datetime import date, timedelta

    with _tasks_conn() as c:
        patterns = c.execute(
            "SELECT * FROM patterns WHERE user_id=? AND confidence_score >= 0.5",
            (user_id,)).fetchall()

    tasks_created = 0
    today = date.today().isoformat()

    for p in patterns:
        p = dict(p)
        if not p.get("next_due_date"):
            continue
        if p["next_due_date"] > today:
            continue

        with _tasks_conn() as c:
            existing = c.execute(
                "SELECT id FROM tasks WHERE user_id=? AND contact_email=? "
                "AND task_type=? AND status='pending'",
                (user_id, p["contact_email"], p["pattern_type"])).fetchone()
            if existing:
                continue

        ptype   = p["pattern_type"]
        contact = p["contact_name"] or p["contact_email"]
        child   = p.get("child_name", "")
        child_s = (" for " + child) if child else ""

        if ptype == "prescription":
            subject   = "Prescription Refill Request" + child_s
            body      = "Dear " + contact + ",\n\nI would like to request a prescription refill" + child_s + ".\n\nPlease let me know if you need any additional information.\n\nThank you"
            title     = "Prescription refill — " + contact + child_s
            task_type = "draft"
        elif ptype == "doctor":
            subject   = "Follow-up Appointment Request" + child_s
            body      = "Dear " + contact + ",\n\nI would like to schedule a follow-up appointment" + child_s + ".\n\nPlease let me know your available times.\n\nThank you"
            title     = "Doctor follow-up — " + contact + child_s
            task_type = "draft"
        elif ptype == "insurance":
            subject   = "Follow Up: Insurance Claim Status"
            body      = "Dear " + contact + ",\n\nI am following up on my recent insurance claim. Could you please provide a status update?\n\nThank you"
            title     = "Insurance claim follow-up — " + contact
            task_type = "followup"
        elif ptype == "pharmacy":
            subject   = "Prescription Refill Request" + child_s
            body      = "Dear " + contact + ",\n\nI would like to request a refill" + child_s + ".\n\nThank you"
            title     = "Pharmacy refill — " + contact + child_s
            task_type = "draft"
        else:
            continue

        next_due = (date.today() + timedelta(days=p["frequency_days"])).isoformat()
        with _tasks_conn() as c:
            c.execute(
                "INSERT INTO tasks(user_id,task_type,title,status,due_date,"
                "draft_to,draft_subject,draft_body,contact_name,contact_email,"
                "child_name,recurrence_days,last_triggered) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, task_type, title, "pending", today,
                 p["contact_email"], subject, body,
                 p["contact_name"], p["contact_email"],
                 child, p["frequency_days"], today))
            c.execute("UPDATE patterns SET next_due_date=? WHERE id=?",
                      (next_due, p["id"]))
            c.commit()
        tasks_created += 1

    return {"tasks_created": tasks_created}


def _send_draft_email(user_id: str, email: str, to: str, subject: str, body: str) -> bool:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from email.mime.text import MIMEText
    import base64 as b64

    token_data = _load_token(user_id, email)
    if not token_data:
        return False
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    service = build("gmail", "v1", credentials=creds)
    msg            = MIMEText(body)
    msg["to"]      = to
    msg["subject"] = subject
    raw = b64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception as e:
        print("[send email] " + str(e))
        return False


# ── Gmail endpoints ────────────────────────────────────────────────────────────

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
    total_new = total_skipped = accounts_ok = 0
    errors = []
    for email in emails:
        r = _scan_single_gmail(user_id, email)
        if r.get("error"):
            if "expired" in str(r.get("error","")).lower() or "invalid_grant" in str(r.get("error","")).lower() or "not authenticated" in str(r.get("error","")).lower():
                errors.append({"email": email, "error": "token_expired",
                    "message": email + " needs to reconnect",
                    "reauth_url": "https://hearth-4kqf.onrender.com/auth/login?user_id=" + user_id + "&add_account=true"})
            else:
                errors.append({"email": email, "error": str(r["error"])})
        else:
            total_new     += r.get("new", 0)
            total_skipped += r.get("skipped", 0)
            accounts_ok   += 1
    # Run lifecycle check for pending/registered camps
    try:
        from agent.calendar_agent import _conn as _lc_conn
        with _lc_conn() as c:
            lc_camps = [dict(r) for r in c.execute(
                "SELECT * FROM camps WHERE user_id=? AND status IN ('pending','registered')",
                (user_id,)).fetchall()]
        for lc_camp in lc_camps:
            try:
                _check_camp_lifecycle(user_id, lc_camp)
            except Exception as e:
                print("[lifecycle scan] camp " + str(lc_camp.get("id")) + ": " + str(e))
    except Exception as e:
        print("[lifecycle scan] " + str(e))

    return {"new": total_new, "skipped": total_skipped,
            "accounts_scanned": accounts_ok,
            "errors": errors, "error": None}


@app.get("/connected-gmails")
def connected_gmails(user_id: str):
    return {"emails": _list_connected_emails(user_id)}


# ── Google Calendar endpoints ──────────────────────────────────────────────────

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
    emails     = _list_connected_emails(user_id)
    if not emails or not summary or not event_date:
        return {"gcal_id": None, "error": "Missing required fields"}
    gcal_id = _write_to_gcal(user_id, emails[0], summary, event_date,
                             req.get("event_time"), req.get("description"))
    return {"gcal_id": gcal_id, "error": None if gcal_id else "Write failed"}


# ── Events ─────────────────────────────────────────────────────────────────────

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


# ── Tasks / Actions ────────────────────────────────────────────────────────────

def _get_tasks(user_id: str, status: str = "pending") -> list:
    from datetime import datetime
    now = datetime.now().isoformat()
    with _tasks_conn() as c:
        if status == "pending":
            rows = c.execute(
                "SELECT * FROM tasks WHERE user_id=? "
                "AND status IN ('pending','snoozed') "
                "AND (snoozed_until IS NULL OR snoozed_until <= ?) "
                "ORDER BY due_date ASC",
                (user_id, now)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM tasks WHERE user_id=? AND status=? ORDER BY due_date DESC",
                (user_id, status)).fetchall()
    return [dict(r) for r in rows]


@app.get("/tasks")
def get_tasks(user_id: str, status: str = "pending"):
    return _get_tasks(user_id, status)

class TaskCreateRequest(BaseModel):
    user_id:       str
    task_type:     str
    title:         str
    due_date:      Optional[str] = None
    amount:        Optional[str] = None
    payment_url:   Optional[str] = None
    company_login_url: Optional[str] = None
    contact_name:  Optional[str] = None
    contact_email: Optional[str] = None
    child_name:    Optional[str] = None
    draft_to:      Optional[str] = None
    draft_subject: Optional[str] = None
    draft_body:    Optional[str] = None

@app.post("/tasks")
def create_task(req: TaskCreateRequest):
    with _tasks_conn() as c:
        c.execute("""
            INSERT INTO tasks(user_id,task_type,title,status,due_date,
            amount,payment_url,company_login_url,contact_name,contact_email,
            child_name,draft_to,draft_subject,draft_body)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (req.user_id, req.task_type, req.title, "pending", req.due_date,
              req.amount, req.payment_url, req.company_login_url,
              req.contact_name, req.contact_email, req.child_name,
              req.draft_to, req.draft_subject, req.draft_body))
        c.commit()
        task_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"id": task_id, "status": "created"}


@app.post("/tasks/scan")
def scan_actions(user_id: str):
    emails = _list_connected_emails(user_id)
    if not emails:
        return {"error": "No Gmail connected"}
    total_patterns = total_bills = 0
    for email in emails:
        r1 = _scan_sent_mail(user_id, email, days_back=90)
        r2 = _scan_bill_emails(user_id, email, days_back=30)
        total_patterns += r1.get("patterns_found", 0)
        total_bills    += r2.get("bills_found", 0)
    r3 = _evaluate_patterns(user_id)
    return {
        "patterns_found": total_patterns,
        "bills_found":    total_bills,
        "tasks_created":  r3.get("tasks_created", 0),
        "error": None
    }


@app.post("/tasks/{task_id}/send")
def send_task(task_id: int, user_id: str):
    with _tasks_conn() as c:
        task = c.execute(
            "SELECT * FROM tasks WHERE id=? AND user_id=?",
            (task_id, user_id)).fetchone()
    if not task:
        raise HTTPException(404, "Task not found")
    task   = dict(task)
    emails = _list_connected_emails(user_id)
    if not emails:
        return {"status": "error", "error": "No Gmail connected"}
    ok = _send_draft_email(user_id, emails[0],
                           task["draft_to"], task["draft_subject"], task["draft_body"])
    if ok:
        with _tasks_conn() as c:
            c.execute("UPDATE tasks SET status='sent', last_triggered=datetime('now') WHERE id=?",
                      (task_id,))
            c.commit()
        return {"status": "sent"}
    return {"status": "error", "error": "Failed to send"}


@app.post("/tasks/{task_id}/snooze")
def snooze_task(task_id: int, user_id: str, days: int = 3):
    from datetime import datetime, timedelta
    snooze_until = (datetime.now() + timedelta(days=days)).isoformat()
    with _tasks_conn() as c:
        c.execute("UPDATE tasks SET status='snoozed', snoozed_until=? WHERE id=? AND user_id=?",
                  (snooze_until, task_id, user_id))
        c.commit()
    return {"status": "snoozed", "until": snooze_until}


@app.post("/tasks/{task_id}/done")
def done_task(task_id: int, user_id: str):
    with _tasks_conn() as c:
        c.execute("UPDATE tasks SET status='done' WHERE id=? AND user_id=?",
                  (task_id, user_id))
        c.commit()
    return {"status": "done"}


@app.post("/tasks/{task_id}/thumbs")
def thumbs_task(task_id: int, user_id: str, value: str):
    if value not in ("up", "down"):
        raise HTTPException(400, "value must be up or down")
    with _tasks_conn() as c:
        c.execute("UPDATE tasks SET thumbs=? WHERE id=? AND user_id=?",
                  (value, task_id, user_id))
        if value == "up":
            c.execute("UPDATE tasks SET status='done' WHERE id=? AND user_id=?",
                      (task_id, user_id))
        c.commit()
    return {"status": "ok", "thumbs": value}


@app.put("/tasks/{task_id}/draft")
def update_draft(task_id: int, user_id: str, req: dict):
    with _tasks_conn() as c:
        c.execute("UPDATE tasks SET draft_body=?, draft_subject=?, draft_to=? WHERE id=? AND user_id=?",
                  (req.get("body"), req.get("subject"), req.get("to"), task_id, user_id))
        c.commit()
    return {"status": "updated"}


@app.get("/patterns")
def get_patterns(user_id: str):
    with _tasks_conn() as c:
        rows = c.execute(
            "SELECT * FROM patterns WHERE user_id=? ORDER BY next_due_date",
            (user_id,)).fetchall()
    return [dict(r) for r in rows]


@app.delete("/patterns/{pattern_id}")
def delete_pattern(pattern_id: int, user_id: str):
    with _tasks_conn() as c:
        c.execute("DELETE FROM patterns WHERE id=? AND user_id=?",
                  (pattern_id, user_id))
        c.commit()
    return {"status": "deleted"}


# ── Transcribe ─────────────────────────────────────────────────────────────────

@app.post("/transcribe")
async def transcribe_audio(user_id: str, request: Request):
    import tempfile, subprocess
    from google.cloud import speech
    from google.oauth2 import service_account

    try:
        body      = await request.json()
        audio_b64 = body.get("audio", "")
        if not audio_b64:
            return {"transcript": "", "error": "No audio received"}
        m4a_bytes = base64.b64decode(audio_b64)
        print("[transcribe] received " + str(len(m4a_bytes)) + " bytes M4A")

        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
            tmp.write(m4a_bytes)
            m4a_path = tmp.name

        flac_path = m4a_path.replace(".m4a", ".flac")
        result    = subprocess.run(
            ["ffmpeg", "-y", "-i", m4a_path, "-ar", "16000", "-ac", "1", "-c:a", "flac", flac_path],
            capture_output=True, text=True, timeout=30)
        os.unlink(m4a_path)
        if result.returncode != 0:
            return {"transcript": "", "error": "Audio conversion failed"}

        with open(flac_path, "rb") as f:
            flac_bytes = f.read()
        os.unlink(flac_path)
        print("[transcribe] converted to " + str(len(flac_bytes)) + " bytes FLAC")

        sa_json = os.getenv("GOOGLE_SPEECH_SA", "")
        if not sa_json:
            return {"transcript": "", "error": "Speech service account not configured"}
        sa_info = json.loads(sa_json)
        creds   = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/cloud-platform"])

        client   = speech.SpeechClient(credentials=creds)
        audio    = speech.RecognitionAudio(content=flac_bytes)
        config   = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.FLAC,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="default",
        )
        response   = client.recognize(config=config, audio=audio)
        transcript = " ".join(r.alternatives[0].transcript for r in response.results).strip()
        print("[transcribe] transcript: '" + transcript + "'")
        return {"transcript": transcript, "error": None}

    except Exception as e:
        print("[transcribe] exception: " + str(e))
        return {"transcript": "", "error": str(e)}


# ── Briefing ───────────────────────────────────────────────────────────────────

@app.get("/briefing")
def get_briefing(user_id: str):
    text     = _build_briefing(user_id)
    today    = _query_today(user_id)
    upcoming = _query_upcoming(user_id, days_ahead=7)
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    return {"text": text, "today": today,
            "tomorrow": [e for e in upcoming if e["event_date"] == tomorrow],
            "this_week": upcoming, "tasks": []}


# ── Agent ──────────────────────────────────────────────────────────────────────

class AgentRequest(BaseModel):
    user_id: str; raw_text: str

@app.post("/agent")
def agent_chat(req: AgentRequest):
    result = run(raw_text=req.raw_text, user_id=req.user_id)

    # Write confirmed events to Google Calendar
    confirmed = result.get("confirmed_events", [])
    print("[gcal debug] confirmed_events: " + str(len(confirmed)) + " events")
    emails    = _list_connected_emails(req.user_id)
    # Use primary user email for GCal (not first connected which may be wife's)
    from agent.calendar_agent import _conn as _cal_conn
    session_path = os.path.join(cfg.DATA_DIR, "sessions", req.user_id + ".json")
    primary_email = None
    if os.path.exists(session_path):
        with open(session_path) as f:
            import json as _json
            primary_email = _json.load(f).get("email")
    gcal_email = primary_email if primary_email and primary_email in emails else (emails[0] if emails else None)
    print("[gcal debug] using email: " + str(gcal_email))
    if confirmed and gcal_email:
        for ev in confirmed:
            try:
                gcal_summary = ev.get("notes") or ev.get("event_type","event").replace("_"," ").title()
                child = ev.get("child_name","")
                if child and child != "all":
                    gcal_summary = child + " - " + gcal_summary
                svc = _get_gcal_service(req.user_id, gcal_email)
                if svc and not _gcal_event_exists(svc, gcal_summary, ev["event_date"]):
                    gcal_id = _write_to_gcal(req.user_id, gcal_email, gcal_summary,
                                             ev["event_date"], ev.get("event_time"),
                                             ev.get("notes"))
                    if gcal_id and gcal_id != "duplicate":
                        from agent.calendar_agent import _conn
                        with _conn() as c:
                            c.execute("UPDATE events SET gcal_event_id=? WHERE id=?",
                                      (gcal_id, ev.get("id")))
                            c.commit()
                        print("[gcal] wrote: " + gcal_summary + " on " + ev["event_date"])
            except Exception as e:
                print("[gcal write in agent endpoint] " + str(e))

    return {"response": result.get("response", "")}


# ── Profiles ───────────────────────────────────────────────────────────────────

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

@app.delete("/profiles/{name}")
def delete_profile(name: str, user_id: str):
    from agent.profile_agent import _conn as _prof_conn
    with _prof_conn() as c:
        c.execute("DELETE FROM profiles WHERE user_id=? AND name=?", (user_id, name))
        c.commit()
    return {"status": "deleted"}


# ── Debug ──────────────────────────────────────────────────────────────────────

@app.get("/debug/token")
def debug_token(user_id: str):
    return {"connected_emails": _list_connected_emails(user_id),
            "token_dir": _token_dir(user_id)}

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
            "client_secret_set": bool(GOOGLE_CLIENT_SECRET),
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/ffmpeg")
def debug_ffmpeg():
    import subprocess
    try:
        result = subprocess.run(["ffmpeg", "-version"],
                              capture_output=True, text=True, timeout=5)
        return {"available": True, "version": result.stdout.split("\n")[0]}
    except Exception as e:
        return {"available": False, "error": str(e)}

@app.post("/gmail/scan-debug")
async def gmail_scan_debug(user_id: str):
    from googleapiclient.discovery import build
    from datetime import date, timedelta
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    emails_found = _list_connected_emails(user_id)
    if not emails_found:
        return {"error": "No Gmail connected"}

    all_subjects = []
    for email in emails_found:
        token_data = _load_token(user_id, email)
        if not token_data:
            continue
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
        query   = (
            "after:" + after + " (subject:(dismissal OR recital OR newsletter OR show OR "
            "artwork OR gymnastics OR cheerleading OR \"art show\" OR "
            "\"no school\" OR \"field trip\" OR appointment OR "
            "reminder OR performance OR showcase OR assembly))"
        )
        result   = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
        messages = result.get("messages", [])
        for msg in messages[:10]:
            full    = service.users().messages().get(userId="me", id=msg["id"],
                      format="metadata", metadataHeaders=["Subject"]).execute()
            subject = next((h["value"] for h in full["payload"].get("headers", [])
                           if h["name"] == "Subject"), "no subject")
            all_subjects.append({"email": email, "subject": subject})
    return {"emails_found": len(all_subjects), "subjects": all_subjects}




@app.post("/analyze-image")
async def analyze_image(request: Request):
    """
    Analyze image/PDF with Claude Vision.
    Extract events, bills, prescriptions, appointments etc.
    Returns list of actionable items for user to confirm.
    """
    import anthropic as ant
    import re as _re

    try:
        body      = await request.json()
        user_id   = body.get("user_id", "")
        image_b64 = body.get("image", "")
        mime_type = body.get("mime_type", "image/jpeg")

        if not image_b64:
            return {"items": [], "error": "No image provided"}

        today_str = datetime.date.today().strftime("%A, %B %d, %Y")
        today_iso = datetime.date.today().isoformat()
        children  = get_children(user_id)
        child_str = ", ".join(children) or "the children"

        prompt = (
            "You are Hearth, a family AI assistant. Analyze this image and extract ALL actionable items.\n"
            "Today: " + today_str + " (" + today_iso + "). Children: " + child_str + ".\n\n"
            "Extract any of these:\n"
            "1. Calendar events (school events, appointments, activities)\n"
            "2. Bills or payments due\n"
            "3. Prescription refills needed\n"
            "4. Doctor/dentist appointments\n"
            "5. Any other actionable reminder\n\n"
            "Return ONLY a JSON array:\n"
            "[{\n"
            "  \"type\": \"event\" or \"task\",\n"
            "  \"title\": \"brief title\",\n"
            "  \"event_date\": \"YYYY-MM-DD or null\",\n"
            "  \"event_time\": \"HH:MM or null\",\n"
            "  \"event_type\": \"sports_game|activity|doctor_appointment|special_day|school_holiday|other\",\n"
            "  \"child_name\": \"name or null\",\n"
            "  \"notes\": \"details\",\n"
            "  \"task_type\": \"bill|draft|followup or null\",\n"
            "  \"amount\": \"$XX.XX or null\",\n"
            "  \"due_date\": \"YYYY-MM-DD or null\",\n"
            "  \"payment_url\": \"URL or null\",\n"
            "  \"contact_name\": \"name or null\",\n"
            "  \"draft_to\": \"email or null\",\n"
            "  \"draft_subject\": \"subject or null\",\n"
            "  \"draft_body\": \"email body or null\",\n"
            "  \"selected\": true\n"
            "}]\n"
            "If nothing actionable found, return []."
        )

        client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model=cfg.CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": mime_type if mime_type.startswith("image/") else "image/jpeg",
                            "data":       image_b64,
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        raw   = resp.content[0].text.strip()
        raw   = _re.sub(r"^```json\s*", "", raw)
        raw   = _re.sub(r"\s*```$", "", raw)
        items = json.loads(raw)

        # Ensure all items have selected=True by default
        for item in items:
            item["selected"] = True

        return {"items": items, "error": None}

    except Exception as e:
        print("[analyze-image] " + str(e))
        return {"items": [], "error": str(e)}



@app.post("/parse-pattern")
async def parse_pattern(request: Request):
    """Parse free text into a pattern using Claude."""
    import anthropic as ant
    body    = await request.json()
    user_id = body.get("user_id", "")
    text    = body.get("text", "")
    if not text:
        return {"pattern": None, "error": "No text provided"}

    children    = get_children(user_id)
    child_str   = ", ".join(children) or "none"
    today_iso   = datetime.date.today().isoformat()

    prompt = (
        "Extract a recurring reminder pattern from this text.\n"
        "Family children: " + child_str + ".\n"
        "Today: " + today_iso + ".\n\n"
        "Text: " + text + "\n\n"
        "Return ONLY JSON:\n"
        "{\n"
        "  \"pattern_type\": \"prescription|doctor|insurance|pharmacy|other\",\n"
        "  \"child_name\": \"child name or null\",\n"
        "  \"contact_name\": \"provider name or null\",\n"
        "  \"contact_email\": \"email address or null\",\n"
        "  \"frequency_days\": 30,\n"
        "  \"keywords\": \"brief description of what this reminder is for\"\n"
        "}\n"
        "If you cannot extract a clear pattern, return {}.\n"
        "ONLY JSON."
    )

    try:
        client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model=cfg.CLAUDE_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": prompt}])
        raw    = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
        raw    = re.sub(r"\s*```$", "", raw)
        data   = json.loads(raw)
        if not data or not data.get("pattern_type"):
            return {"pattern": None, "error": "Could not extract pattern"}
        return {"pattern": data, "error": None}
    except Exception as e:
        return {"pattern": None, "error": str(e)}


@app.post("/analyze-pattern")
async def analyze_pattern(request: Request):
    """Analyze image/PDF to extract a recurring pattern."""
    import anthropic as ant
    body      = await request.json()
    user_id   = body.get("user_id", "")
    image_b64 = body.get("image", "")
    mime_type = body.get("mime_type", "image/jpeg")
    if not image_b64:
        return {"pattern": None, "error": "No image provided"}

    children  = get_children(user_id)
    child_str = ", ".join(children) or "none"
    today_iso = datetime.date.today().isoformat()

    prompt = (
        "Look at this image and extract any recurring reminder or prescription pattern.\n"
        "Family children: " + child_str + ".\n"
        "Today: " + today_iso + ".\n\n"
        "Return ONLY JSON:\n"
        "{\n"
        "  \"pattern_type\": \"prescription|doctor|insurance|pharmacy|other\",\n"
        "  \"child_name\": \"child name or null\",\n"
        "  \"contact_name\": \"provider/pharmacy name or null\",\n"
        "  \"contact_email\": \"email or null\",\n"
        "  \"frequency_days\": 30,\n"
        "  \"keywords\": \"what medication or appointment this is for\"\n"
        "}\n"
        "If no pattern found, return {}.\n"
        "ONLY JSON."
    )

    try:
        client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model=cfg.CLAUDE_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": mime_type if mime_type.startswith("image/") else "image/jpeg",
                    "data": image_b64}},
                {"type": "text", "text": prompt}
            ]}])
        raw  = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
        raw  = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not data or not data.get("pattern_type"):
            return {"pattern": None, "error": "No pattern found"}
        return {"pattern": data, "error": None}
    except Exception as e:
        return {"pattern": None, "error": str(e)}


@app.post("/patterns/manual")
async def create_manual_pattern(request: Request):
    """Create a user-defined pattern manually."""
    from datetime import date, timedelta
    body         = await request.json()
    user_id      = body.get("user_id", "")
    pattern_type = body.get("pattern_type", "other")
    contact_name = body.get("contact_name", "")
    contact_email= body.get("contact_email", "")
    child_name   = body.get("child_name")
    frequency    = int(body.get("frequency_days", 30))
    keywords     = body.get("keywords", "")
    next_due     = date.today().isoformat()  # due immediately

    with _tasks_conn() as c:
        c.execute(
            "INSERT INTO patterns(user_id,pattern_type,contact_email,"
            "contact_name,keywords,frequency_days,last_action_date,"
            "next_due_date,confidence_score,child_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, pattern_type, contact_email, contact_name,
             keywords, frequency, date.today().isoformat(),
             next_due, 1.0, child_name))
        c.commit()

    # Evaluate immediately to create draft if due
    _evaluate_patterns(user_id)
    return {"status": "created"}


@app.get("/debug/all-users")
def debug_all_users():
    import os
    token_base = os.path.join(cfg.DATA_DIR, "tokens")
    users = []
    if os.path.exists(token_base):
        for user_id in os.listdir(token_base):
            token_dir = os.path.join(token_base, user_id)
            if os.path.isdir(token_dir):
                emails = []
                for fname in os.listdir(token_dir):
                    if fname.startswith("gmail_") and fname.endswith(".json"):
                        safe  = fname[6:-5]
                        email = safe.replace("_at_", "@", 1)
                        parts = email.split("@")
                        email = "@".join(p.replace("_", ".") for p in parts)
                        emails.append(email)
                users.append({"user_id": user_id, "emails": emails})
    return {"users": users, "count": len(users)}


# ── Push Notifications ─────────────────────────────────────────────────────────

def _init_push_tokens():
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                token      TEXT NOT NULL UNIQUE,
                platform   TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.commit()

def _get_push_tokens(user_id: str) -> list:
    from agent.calendar_agent import _conn
    with _conn() as c:
        rows = c.execute(
            "SELECT token FROM push_tokens WHERE user_id=?",
            (user_id,)).fetchall()
    return [r["token"] for r in rows]

def _send_push(tokens: list, title: str, body: str, data: dict = None):
    """Send push notification via Expo Push API."""
    import httpx
    if not tokens:
        return
    messages = [{"to": t, "title": title, "body": body,
                 "sound": "default", "data": data or {}}
                for t in tokens]
    try:
        httpx.post("https://exp.host/--/api/v2/push/send",
                   json=messages, timeout=10)
        print(f"[push] sent to {len(tokens)} device(s): {title}")
    except Exception as e:
        print(f"[push] failed: {e}")


@app.post("/push/register")
async def register_push_token(request: Request):
    body     = await request.json()
    user_id  = body.get("user_id", "")
    token    = body.get("token", "")
    platform = body.get("platform", "")
    if not user_id or not token:
        return {"status": "error", "error": "Missing user_id or token"}
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("""
            INSERT INTO push_tokens(user_id, token, platform)
            VALUES(?,?,?)
            ON CONFLICT(token) DO UPDATE SET user_id=excluded.user_id
        """, (user_id, token, platform))
        c.commit()
    print(f"[push] registered token for {user_id}")
    return {"status": "ok"}


@app.post("/push/daily-briefing")
async def send_daily_briefing(secret: str = ""):
    """Send 7am briefing to all users. Called by cron."""
    if secret != os.getenv("CRON_SECRET", "hearth-cron-2026"):
        raise HTTPException(403, "Unauthorized")
    from agent.calendar_agent import _conn
    from datetime import date
    with _conn() as c:
        user_ids = [r[0] for r in c.execute(
            "SELECT DISTINCT user_id FROM push_tokens").fetchall()]
    sent = 0
    for user_id in user_ids:
        tokens = _get_push_tokens(user_id)
        if not tokens:
            continue
        today_events = _query_today(user_id)
        if not today_events:
            body = "No events scheduled today. Enjoy your day!"
        else:
            parts = []
            for ev in today_events[:3]:
                note  = ev.get("notes") or ev.get("event_type","").replace("_"," ").title()
                time  = " at " + ev["event_time"] if ev.get("event_time") else ""
                child = ev.get("child_name","")
                if child and child != "all":
                    parts.append(child + ": " + note + time)
                else:
                    parts.append(note + time)
            body = ", ".join(parts)
            if len(today_events) > 3:
                body += f" +{len(today_events)-3} more"
        _send_push(tokens, "🏠 Hearth Morning Briefing", body)
        sent += 1
    return {"sent": sent}


@app.post("/push/nudge")
async def send_nudges(secret: str = ""):
    """Send nudge notifications for upcoming events. Called by cron."""
    if secret != os.getenv("CRON_SECRET", "hearth-cron-2026"):
        raise HTTPException(403, "Unauthorized")
    from agent.calendar_agent import _conn
    from datetime import date, timedelta
    today    = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    in_2days = (date.today() + timedelta(days=2)).isoformat()

    with _conn() as c:
        user_ids = [r[0] for r in c.execute(
            "SELECT DISTINCT user_id FROM push_tokens").fetchall()]

    sent = 0
    for user_id in user_ids:
        tokens = _get_push_tokens(user_id)
        if not tokens:
            continue
        with _conn() as c:
            # Day-of nudges (not yet sent)
            day_of = c.execute(
                "SELECT * FROM events WHERE user_id=? AND event_date=? AND nudge_sent_day=0",
                (user_id, today)).fetchall()
            # 48h nudges
            two_day = c.execute(
                "SELECT * FROM events WHERE user_id=? AND event_date=? AND nudge_sent_48h=0",
                (user_id, in_2days)).fetchall()

        for ev in [dict(r) for r in day_of]:
            note  = ev.get("notes") or ev.get("event_type","").replace("_"," ").title()
            time  = " at " + ev["event_time"] if ev.get("event_time") else ""
            child = ev.get("child_name","")
            prefix = (child + ": ") if child and child != "all" else ""
            _send_push(tokens, "📅 Today", prefix + note + time)
            with _conn() as c:
                c.execute("UPDATE events SET nudge_sent_day=1 WHERE id=?", (ev["id"],))
                c.commit()
            sent += 1

        for ev in [dict(r) for r in two_day]:
            note  = ev.get("notes") or ev.get("event_type","").replace("_"," ").title()
            child = ev.get("child_name","")
            prefix = (child + ": ") if child and child != "all" else ""
            _send_push(tokens, "⏰ In 2 days", prefix + note)
            with _conn() as c:
                c.execute("UPDATE events SET nudge_sent_48h=1 WHERE id=?", (ev["id"],))
                c.commit()
            sent += 1

    return {"nudges_sent": sent}


@app.post("/gmail/scan-debug-v2")
async def gmail_scan_debug_v2(user_id: str):
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from datetime import date, timedelta
    import anthropic as ant

    emails_list = _list_connected_emails(user_id)
    if not emails_list:
        return {"error": "No Gmail connected"}

    email = emails_list[0]
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

    service  = build("gmail", "v1", credentials=creds)
    after    = (date.today() - timedelta(days=3)).strftime("%Y/%m/%d")
    result   = service.users().messages().list(userId="me", q="after:" + after, maxResults=10).execute()
    messages = result.get("messages", [])

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
    for msg in messages[:5]:
        full    = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
        headers = full["payload"].get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
        date_h  = next((h["value"] for h in headers if h["name"] == "Date"), "")
        body    = extract_body(full["payload"])
        emails_data.append({"subject": subject, "received": date_h, "body_preview": body[:300]})

    return {"emails_found": len(emails_data), "emails": emails_data}


@app.get("/events/conflicts")
def get_conflicts(user_id: str, days_ahead: int = 14):
    """Find events with overlapping times on same date."""
    events = _query_upcoming(user_id, days_ahead=days_ahead)
    conflicts = []
    by_date = {}
    for ev in events:
        d = ev["event_date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(ev)
    for d, evs in by_date.items():
        timed = [e for e in evs if e.get("event_time")]
        for i in range(len(timed)):
            for j in range(i+1, len(timed)):
                if timed[i]["event_time"] == timed[j]["event_time"]:
                    conflicts.append({
                        "date":   d,
                        "event1": timed[i].get("notes") or timed[i]["event_type"],
                        "event2": timed[j].get("notes") or timed[j]["event_type"],
                        "time":   timed[i]["event_time"]
                    })
    return {"conflicts": conflicts}


@app.delete("/debug/delete-token")
def delete_token(user_id: str, email: str):
    """Delete a stored token to force re-auth."""
    import os
    safe  = email.replace(".", "_").replace("@", "_at_")
    path  = os.path.join(_token_dir(user_id), "gmail_" + safe + ".json")
    if os.path.exists(path):
        os.remove(path)
        return {"status": "deleted", "email": email}
    return {"status": "not found"}


# ── Camps ──────────────────────────────────────────────────────────────────────

def _init_camps():
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS camps (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id              TEXT NOT NULL,
                child_name           TEXT,
                camp_name            TEXT NOT NULL,
                registration_deadline TEXT,
                camp_start_date      TEXT,
                camp_end_date        TEXT,
                session_type         TEXT,
                registration_url     TEXT,
                status               TEXT DEFAULT 'pending',
                nudge_sent_2w        INTEGER DEFAULT 0,
                nudge_sent_3d        INTEGER DEFAULT 0,
                nudge_sent_day       INTEGER DEFAULT 0,
                confirmed_registered INTEGER DEFAULT 0,
                created_at           TEXT DEFAULT (datetime('now'))
            )
        """)
        c.commit()

def _camps_conn():
    from agent.calendar_agent import _conn
    return _conn()


@app.post("/camps/parse")
async def parse_camps(request: Request):
    """Claude extracts camp details from free text, per child."""
    import anthropic as ant
    body      = await request.json()
    user_id   = body.get("user_id", "")
    child     = body.get("child_name", "")
    text      = body.get("text", "")
    today_iso = datetime.date.today().isoformat()

    if not text:
        return {"camps": [], "error": "No text"}

    prompt = (
        "Extract summer/activity camp details from this text.\n"
        "Child: " + child + "\n"
        "Today: " + today_iso + "\n\n"
        "Text: " + text + "\n\n"
        "Return ONLY a JSON array of camps:\n"
        "[{\n"
        "  \"camp_name\": \"name of camp\",\n"
        "  \"registration_deadline\": \"YYYY-MM-DD or null\",\n"
        "  \"camp_start_date\": \"YYYY-MM-DD or null\",\n"
        "  \"camp_end_date\": \"YYYY-MM-DD or null\",\n"
        "  \"session_type\": \"full_day or half_day or weekly or null\"\n"
        "}]\n"
        "If multiple camps mentioned, return all of them.\n"
        "ONLY JSON array."
    )

    try:
        client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model=cfg.CLAUDE_MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": prompt}])
        raw  = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
        raw  = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        return {"camps": data, "error": None}
    except Exception as e:
        return {"camps": [], "error": str(e)}


@app.post("/camps")
async def create_camp(request: Request):
    """Save a camp for a user."""
    body       = await request.json()
    user_id    = body.get("user_id", "")
    child      = body.get("child_name", "")
    camp_name  = body.get("camp_name", "")
    deadline   = body.get("registration_deadline")
    start_date = body.get("camp_start_date")
    end_date   = body.get("camp_end_date")
    session    = body.get("session_type")
    url        = body.get("registration_url")

    if not user_id or not camp_name:
        return {"status": "error", "error": "Missing user_id or camp_name"}

    with _camps_conn() as c:
        c.execute(
            "INSERT INTO camps(user_id, child_name, camp_name, registration_deadline, "
            "camp_start_date, camp_end_date, session_type, registration_url) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, child, camp_name, deadline, start_date, end_date, session, url))
        c.commit()
        camp_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    return {"status": "created", "id": camp_id}


@app.get("/camps")
def get_camps(user_id: str):
    """List all camps for a user."""
    with _camps_conn() as c:
        rows = c.execute(
            "SELECT * FROM camps WHERE user_id=? ORDER BY registration_deadline ASC",
            (user_id,)).fetchall()
    return [dict(r) for r in rows]


@app.put("/camps/{camp_id}/status")
async def update_camp_status(camp_id: int, request: Request):
    """Mark a camp as registered or missed."""
    body   = await request.json()
    status = body.get("status", "registered")
    user_id = body.get("user_id", "")
    with _camps_conn() as c:
        c.execute(
            "UPDATE camps SET status=?, confirmed_registered=? WHERE id=? AND user_id=?",
            (status, 1 if status == "registered" else 0, camp_id, user_id))
        c.commit()

    # If registered, write GCal events for camp dates
    if status == "registered":
        with _camps_conn() as c:
            camp = dict(c.execute("SELECT * FROM camps WHERE id=?", (camp_id,)).fetchone())
        if camp.get("camp_start_date") and camp.get("camp_end_date"):
            emails = _list_connected_emails(user_id)
            if emails:
                summary = camp["child_name"] + " - " + camp["camp_name"] if camp.get("child_name") else camp["camp_name"]
                try:
                    _write_to_gcal(user_id, emails[0], summary,
                                   camp["camp_start_date"], None,
                                   camp["camp_name"] + " camp (" + camp["camp_start_date"] + " to " + camp["camp_end_date"] + ")")
                    print(f"[camps] GCal event written for {summary}")
                except Exception as e:
                    print(f"[camps] GCal write error: {e}")

    return {"status": "updated"}


@app.delete("/camps/{camp_id}")
def delete_camp(camp_id: int, user_id: str):
    with _camps_conn() as c:
        c.execute("DELETE FROM camps WHERE id=? AND user_id=?", (camp_id, user_id))
        c.commit()
    return {"status": "deleted"}


@app.post("/camps/search-urls")
async def search_camp_urls(user_id: str = ""):
    """Search web for registration URLs for camps that don't have one."""
    import anthropic as ant
    with _camps_conn() as c:
        if user_id:
            camps = [dict(r) for r in c.execute(
                "SELECT * FROM camps WHERE user_id=? AND registration_url IS NULL AND status='pending'",
                (user_id,)).fetchall()]
        else:
            camps = [dict(r) for r in c.execute(
                "SELECT * FROM camps WHERE registration_url IS NULL AND status='pending'").fetchall()]

    updated = 0
    for camp in camps:
        try:
            client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
            prompt = (
                "Search for the registration URL for this camp: \"" + camp["camp_name"] + "\"\n"
                "Return ONLY a JSON object: {\"url\": \"https://...\" or null}\n"
                "If you cannot find a specific registration URL, return {\"url\": null}\n"
                "ONLY JSON."
            )
            resp = client.messages.create(
                model=cfg.CLAUDE_MODEL, max_tokens=200,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}])
            for block in resp.content:
                if hasattr(block, "text"):
                    raw = re.sub(r"^```json\s*", "", block.text.strip())
                    raw = re.sub(r"\s*```$", "", raw)
                    try:
                        data = json.loads(raw)
                        if data.get("url"):
                            with _camps_conn() as c:
                                c.execute("UPDATE camps SET registration_url=? WHERE id=?",
                                         (data["url"], camp["id"]))
                                c.commit()
                            updated += 1
                    except: pass
        except Exception as e:
            print(f"[camps] URL search error for {camp['camp_name']}: {e}")

    return {"updated": updated}


@app.post("/camps/nudge")
async def nudge_camps(secret: str = ""):
    """Send camp registration reminders. Called by cron."""
    if secret != os.getenv("CRON_SECRET", "hearth-cron-2026"):
        raise HTTPException(403, "Unauthorized")

    from datetime import date, timedelta
    today      = date.today()
    in_3_days  = (today + timedelta(days=3)).isoformat()
    in_14_days = (today + timedelta(days=14)).isoformat()
    today_iso  = today.isoformat()
    yesterday  = (today - timedelta(days=1)).isoformat()

    with _camps_conn() as c:
        camps_2w  = [dict(r) for r in c.execute(
            "SELECT * FROM camps WHERE status='pending' AND registration_deadline=? AND nudge_sent_2w=0",
            (in_14_days,)).fetchall()]
        camps_3d  = [dict(r) for r in c.execute(
            "SELECT * FROM camps WHERE status='pending' AND registration_deadline=? AND nudge_sent_3d=0",
            (in_3_days,)).fetchall()]
        camps_day = [dict(r) for r in c.execute(
            "SELECT * FROM camps WHERE status='pending' AND registration_deadline=? AND nudge_sent_day=0",
            (today_iso,)).fetchall()]
        camps_confirm = [dict(r) for r in c.execute(
            "SELECT * FROM camps WHERE status='pending' AND registration_deadline=? AND confirmed_registered=0",
            (yesterday,)).fetchall()]

    sent = 0
    for camp in camps_2w:
        tokens = _get_push_tokens(camp["user_id"])
        child  = (" for " + camp["child_name"]) if camp.get("child_name") else ""
        body   = camp["camp_name"] + child + " — registration closes " + camp["registration_deadline"]
        _send_push(tokens, "⏰ Camp deadline in 2 weeks", body)
        with _camps_conn() as c:
            c.execute("UPDATE camps SET nudge_sent_2w=1 WHERE id=?", (camp["id"],))
            c.commit()
        sent += 1

    for camp in camps_3d:
        tokens = _get_push_tokens(camp["user_id"])
        child  = (" for " + camp["child_name"]) if camp.get("child_name") else ""
        body   = camp["camp_name"] + child + " — only 3 days left to register!"
        _send_push(tokens, "🏕️ 3 days to register", body)
        with _camps_conn() as c:
            c.execute("UPDATE camps SET nudge_sent_3d=1 WHERE id=?", (camp["id"],))
            c.commit()
        sent += 1

    for camp in camps_day:
        tokens = _get_push_tokens(camp["user_id"])
        child  = (" for " + camp["child_name"]) if camp.get("child_name") else ""
        body   = "Last chance! " + camp["camp_name"] + child + " registration closes today."
        _send_push(tokens, "🚨 Camp deadline today", body)
        with _camps_conn() as c:
            c.execute("UPDATE camps SET nudge_sent_day=1 WHERE id=?", (camp["id"],))
            c.commit()
        sent += 1

    for camp in camps_confirm:
        tokens = _get_push_tokens(camp["user_id"])
        child  = (" for " + camp["child_name"]) if camp.get("child_name") else ""
        body   = "Did you register" + child + " for " + camp["camp_name"] + "? Tap to confirm."
        _send_push(tokens, "✅ Did you register?", body)
        sent += 1

    return {"nudges_sent": sent}


# ── User Preferences ──────────────────────────────────────────────────────────

def _init_preferences():
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                mental_load_areas TEXT DEFAULT '["school","camps","medical","bills","kids_apps"]',
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.commit()

@app.get("/user/preferences")
def get_preferences(user_id: str):
    from agent.calendar_agent import _conn
    import json
    with _conn() as c:
        row = c.execute("SELECT * FROM user_preferences WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return {"mental_load_areas": ["school","camps","medical","bills","kids_apps"]}
    return {"mental_load_areas": json.loads(row["mental_load_areas"])}

@app.post("/user/preferences")
async def save_preferences(request: Request):
    import json
    from agent.calendar_agent import _conn
    body  = await request.json()
    uid   = body.get("user_id","")
    areas = body.get("mental_load_areas", ["school","camps","medical","bills","kids_apps"])
    with _conn() as c:
        c.execute("""
            INSERT INTO user_preferences(user_id, mental_load_areas)
            VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET
            mental_load_areas=excluded.mental_load_areas,
            updated_at=datetime('now')
        """, (uid, json.dumps(areas)))
        c.commit()
    return {"status": "saved"}


# ── Camp Tasks ─────────────────────────────────────────────────────────────────

def _init_camp_tasks():
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS camp_tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                camp_id     INTEGER NOT NULL,
                title       TEXT NOT NULL,
                due_date    TEXT,
                status      TEXT DEFAULT 'pending',
                nudge_sent  INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        c.commit()

def _upgrade_camps_table():
    from agent.calendar_agent import _conn
    with _conn() as c:
        for col, defn in [
            ("camp_type",     "TEXT DEFAULT 'unknown'"),
            ("app_name",      "TEXT"),
            ("deep_link_url", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE camps ADD COLUMN {col} {defn}")
                c.commit()
                print(f"[camps] added column {col}")
            except Exception:
                pass


PLATFORM_MAP = {
    "campintouch":   ("Campanion",      "campanion://"),
    "campanion":     ("Campanion",      "campanion://"),
    "campdoc":       ("CampDoc",        None),
    "ultracamp":     ("UltraCamp",      "ultracamp://"),
    "campbrain":     ("CampBrain",      None),
    "jackrabbittech":("Jackrabbit",     "jackrabbit://"),
    "challengecamp": ("Challenge Camp", "challengecamp://"),
    "seesaw":        ("Seesaw",         "seesaw://"),
    "classdojo":     ("ClassDojo",      "classdojo://"),
    "remind":        ("Remind",         "remind://"),
    "active":        ("Active",         None),
}

def _detect_platform(url: str):
    if not url:
        return None, None
    url_lower = url.lower()
    for key, (app_name, deep_link) in PLATFORM_MAP.items():
        if key in url_lower:
            return app_name, deep_link
    return None, None

def _infer_camp_type(camp_name: str) -> str:
    name = camp_name.lower()
    overnight_keywords = ["ramah","yavneh","overnight","sleepaway","residential",
                          "sleep away","sleep-away","four week","six week","8 week"]
    enrichment_keywords = ["music","dance","piano","violin","tutor","art studio",
                           "theater","theatre","reading","language"]
    day_keywords = ["code wiz","codewiz","coding","stem","science","robotics",
                    "math","chess","lego","minecraft","roblox","soccer","swim",
                    "tennis","gymnastics","cheer","tumbling","karate","martial"]
    for k in overnight_keywords:
        if k in name: return "overnight"
    for k in day_keywords:
        if k in name: return "day"
    for k in enrichment_keywords:
        if k in name: return "enrichment"
    return "unknown"


@app.post("/camps/{camp_id}/generate-tasks")
async def generate_camp_tasks(camp_id: int, user_id: str):
    import json
    from datetime import date, timedelta
    from agent.calendar_agent import _conn
    import anthropic as ant

    with _conn() as c:
        camp = c.execute("SELECT * FROM camps WHERE id=? AND user_id=?",
                         (camp_id, user_id)).fetchone()
    if not camp:
        return {"status": "not found"}
    camp = dict(camp)

    camp_type  = camp.get("camp_type") or _infer_camp_type(camp.get("camp_name",""))
    start_date = camp.get("camp_start_date")
    camp_name  = camp.get("camp_name","camp")
    child      = camp.get("child_name","")

    if camp_type == "enrichment":
        return {"status": "no tasks needed", "tasks": []}

    if camp_type == "unknown":
        return {"status": "unknown camp type", "tasks": []}

    today = date.today()

    tasks_to_create = []

    if camp_type == "overnight":
        if start_date:
            try:
                start = date.fromisoformat(start_date)
                days_until = (start - today).days
                if days_until > 21:
                    tasks_to_create.append({
                        "title": f"Submit medical forms for {camp_name}",
                        "due_date": (start - timedelta(days=21)).isoformat(),
                    })
                if days_until > 14:
                    tasks_to_create.append({
                        "title": f"Complete waivers & chugim for {camp_name}",
                        "due_date": (start - timedelta(days=14)).isoformat(),
                    })
                if days_until > 7:
                    tasks_to_create.append({
                        "title": f"Start packing for {camp_name}",
                        "due_date": (start - timedelta(days=7)).isoformat(),
                    })
            except: pass
        else:
            tasks_to_create.append({
                "title": f"Submit medical forms for {camp_name}",
                "due_date": None,
            })
            tasks_to_create.append({
                "title": f"Complete waivers for {camp_name}",
                "due_date": None,
            })

    elif camp_type == "day":
        if start_date:
            try:
                start = date.fromisoformat(start_date)
                days_until = (start - today).days
                if days_until > 3:
                    tasks_to_create.append({
                        "title": f"Pack supplies for {camp_name}",
                        "due_date": (start - timedelta(days=1)).isoformat(),
                    })
            except: pass

    with _conn() as c:
        existing = c.execute(
            "SELECT title FROM camp_tasks WHERE camp_id=? AND user_id=?",
            (camp_id, user_id)).fetchall()
        existing_titles = {r["title"] for r in existing}

    created = []
    from agent.calendar_agent import _conn as conn
    for t in tasks_to_create:
        if t["title"] in existing_titles:
            continue
        with conn() as c:
            c.execute(
                "INSERT INTO camp_tasks(user_id,camp_id,title,due_date) VALUES(?,?,?,?)",
                (user_id, camp_id, t["title"], t.get("due_date")))
            c.commit()
        created.append(t)

    return {"status": "created", "tasks": created}


@app.get("/camps/{camp_id}/tasks")
def get_camp_tasks(camp_id: int, user_id: str):
    from agent.calendar_agent import _conn
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM camp_tasks WHERE camp_id=? AND user_id=? AND status='pending' ORDER BY due_date",
            (camp_id, user_id)).fetchall()
    return [dict(r) for r in rows]


@app.put("/camp-tasks/{task_id}/done")
def complete_camp_task(task_id: int, user_id: str):
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("UPDATE camp_tasks SET status='done' WHERE id=? AND user_id=?",
                  (task_id, user_id))
        c.commit()
    return {"status": "done"}


# ── Actions Briefing ───────────────────────────────────────────────────────────

@app.post("/actions/briefing")
async def actions_briefing(request: Request):
    import json, anthropic as ant
    from datetime import date, timedelta
    from agent.calendar_agent import _conn

    body    = await request.json()
    user_id = body.get("user_id","")
    today   = date.today().isoformat()
    in_7d   = (date.today() + timedelta(days=7)).isoformat()
    in_30d  = (date.today() + timedelta(days=30)).isoformat()

    prefs = get_preferences(user_id)
    areas = prefs.get("mental_load_areas", ["school","camps","medical","bills","kids_apps"])

    data = {}

    if "camps" in areas:
        with _conn() as c:
            data["camps"] = [dict(r) for r in c.execute(
                "SELECT * FROM camps WHERE user_id=? AND status='pending' ORDER BY registration_deadline",
                (user_id,)).fetchall()]
            data["registered_camps_with_next_steps"] = [dict(r) for r in c.execute(
                "SELECT id, camp_name, child_name, next_action, app_name, deep_link_url "
                "FROM camps WHERE user_id=? AND status='registered' AND next_action IS NOT NULL",
                (user_id,)).fetchall()]
            data["camp_tasks"] = [dict(r) for r in c.execute(
                """SELECT ct.*, c.camp_name, c.app_name, c.deep_link_url
                   FROM camp_tasks ct JOIN camps c ON ct.camp_id=c.id
                   WHERE ct.user_id=? AND ct.status='pending' ORDER BY ct.due_date""",
                (user_id,)).fetchall()]

    if "bills" in areas:
        with _conn() as c:
            data["bills"] = [dict(r) for r in c.execute(
                "SELECT * FROM tasks WHERE user_id=? AND task_type='bill' AND status='pending' ORDER BY due_date",
                (user_id,)).fetchall()]

    if "medical" in areas:
        with _conn() as c:
            data["medical"] = [dict(r) for r in c.execute(
                "SELECT * FROM tasks WHERE user_id=? AND task_type IN ('draft','followup') AND status='pending' ORDER BY due_date",
                (user_id,)).fetchall()]

    prompt = f"""You are Hearth, a family concierge AI. Today is {today}.

Here is the family's actionable data:
{json.dumps(data, indent=2, default=str)}

Generate a daily briefing of items that require the parent to take action.
Rules:
- ONLY include items where the parent needs to DO something
- Do NOT include calendar events or informational items
- For registered_camps_with_next_steps, create an item using the next_action text as the title.
  If app_name is set, set action_label to "Open {app_name}", app_name, and deep_link_url accordingly.
  Otherwise set action_label appropriately (e.g. "View details") with action_url null.
  Use item_type "camp" and item_id = the camp's id.
- Sort by urgency (most urgent first)
- Maximum 6 items

Return ONLY a JSON array:
[{{
  "urgency": "high" | "medium" | "low",
  "title": "short clear title",
  "subtitle": "one line of context — why it matters or when",
  "action_label": "Pay Now" | "Open App" | "Draft Email" | "Register" | "Submit" | "Done" | null,
  "action_url": "https://... or null",
  "app_name": "Campanion" | null,
  "deep_link_url": "campanion:// or null",
  "item_type": "camp" | "bill" | "medical" | "camp_task",
  "item_id": 123
}}]

Only return JSON array. No other text."""

    try:
        client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model=cfg.CLAUDE_MODEL, max_tokens=1500,
            messages=[{"role":"user","content":prompt}])
        raw  = re.sub(r"^```json\s*","",resp.content[0].text.strip())
        raw  = re.sub(r"\s*```$","",raw)
        items = json.loads(raw)
        return {"items": items, "error": None}
    except Exception as e:
        return {"items": [], "error": str(e)}


# ── Camp checklist & setup extensions ─────────────────────────────────────────

def _upgrade_camps_table_v2():
    from agent.calendar_agent import _conn
    with _conn() as c:
        for col, defn in [
            ("orientation_date",        "TEXT"),
            ("bus_needed",              "INTEGER DEFAULT 0"),
            ("meals_needed",            "INTEGER DEFAULT 0"),
            ("packing_list",            "TEXT"),
            ("class_selection_deadline","TEXT"),
            ("payment_schedule",        "TEXT"),
            ("next_action",             "TEXT"),
            ("lifecycle_checked_at",    "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE camps ADD COLUMN {col} {defn}")
                c.commit()
                print(f"[camps_v2] added {col}")
            except:
                pass

def _init_camp_checklist():
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS camp_checklist (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                camp_id      INTEGER NOT NULL,
                item_type    TEXT NOT NULL,
                title        TEXT NOT NULL,
                due_date     TEXT,
                status       TEXT DEFAULT 'pending',
                auto_trigger TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        c.commit()

def _init_prescriptions():
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS prescriptions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                child_name   TEXT,
                medication   TEXT NOT NULL,
                frequency    TEXT,
                notes        TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        c.commit()


@app.get("/user/setup-status")
def get_setup_status(user_id: str):
    """Returns completion status per mental load area."""
    import json
    from agent.calendar_agent import _conn

    prefs = get_preferences(user_id)
    areas = prefs.get("mental_load_areas", [])

    status = {}

    if "camps" in areas:
        with _conn() as c:
            camps = [dict(r) for r in c.execute(
                "SELECT * FROM camps WHERE user_id=?", (user_id,)).fetchall()]
        incomplete = [c for c in camps if not c.get("camp_start_date") or not c.get("orientation_date")]
        status["camps"] = {
            "count": len(camps),
            "incomplete": len(incomplete),
            "label": f"{len(camps)} camp(s) added" + (f" · {len(incomplete)} incomplete" if incomplete else " · complete") if camps else "Not set up yet"
        }

    if "medical" in areas:
        with _conn() as c:
            rxs = c.execute("SELECT * FROM prescriptions WHERE user_id=?", (user_id,)).fetchall()
        status["medical"] = {
            "count": len(rxs),
            "label": f"{len(rxs)} prescription(s) tracked" if rxs else "Not set up yet"
        }

    if "school" in areas:
        with _conn() as c:
            kids = [dict(r) for r in c.execute(
                "SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchall()]
        status["school"] = {
            "count": len(kids),
            "label": ", ".join(k["name"] for k in kids) if kids else "Not set up yet"
        }

    if "bills" in areas:
        status["bills"] = {"label": "Gmail scan active"}

    if "kids_apps" in areas:
        with _conn() as c:
            camps_with_apps = [dict(r) for r in c.execute(
                "SELECT DISTINCT app_name FROM camps WHERE user_id=? AND app_name IS NOT NULL",
                (user_id,)).fetchall()]
        app_names = [r["app_name"] for r in camps_with_apps if r["app_name"]]
        status["kids_apps"] = {
            "label": ", ".join(app_names) if app_names else "No apps detected yet"
        }

    return {"areas": areas, "status": status}


@app.post("/camps/{camp_id}/checklist/generate")
async def generate_camp_checklist(camp_id: int, user_id: str):
    """Generate checklist items for a camp."""
    import json
    from datetime import date, timedelta
    from agent.calendar_agent import _conn

    with _conn() as c:
        camp = c.execute("SELECT * FROM camps WHERE id=? AND user_id=?",
                         (camp_id, user_id)).fetchone()
    if not camp:
        return {"status": "not found"}
    camp = dict(camp)

    child      = camp.get("child_name", "")
    camp_name  = camp.get("camp_name", "camp")
    camp_type  = camp.get("camp_type") or _infer_camp_type(camp_name)
    start_date = camp.get("camp_start_date")
    deadline   = camp.get("registration_deadline")
    orientation= camp.get("orientation_date")

    items = []
    today = date.today()

    # Registration
    items.append({
        "item_type": "registration",
        "title": "Register for " + camp_name,
        "due_date": deadline,
        "auto_trigger": "registration_url_opened"
    })

    # Orientation
    if orientation:
        items.append({
            "item_type": "calendar",
            "title": "Add orientation to calendar",
            "due_date": orientation,
            "auto_trigger": "gcal_written"
        })

    # Camp dates on calendar
    if start_date:
        items.append({
            "item_type": "calendar",
            "title": "Add camp dates to calendar",
            "due_date": start_date,
            "auto_trigger": "gcal_written"
        })

    # Medical forms — check last doctor visit for this child
    if camp_type in ["overnight", "unknown"] and child:
        with _conn() as c:
            last_appt = c.execute(
                """SELECT event_date FROM events
                   WHERE user_id=? AND event_type='doctor_appointment'
                   AND (child_name=? OR child_name='all')
                   ORDER BY event_date DESC LIMIT 1""",
                (user_id, child)).fetchone()
        if last_appt:
            try:
                last_dt   = date.fromisoformat(last_appt["event_date"])
                months_ago = (today - last_dt).days / 30
                if months_ago > 11:
                    items.append({
                        "item_type": "medical",
                        "title": f"Schedule physical for {child} — last was {round(months_ago)}mo ago",
                        "due_date": (date.fromisoformat(start_date) - timedelta(days=14)).isoformat() if start_date else None,
                        "auto_trigger": None
                    })
                else:
                    items.append({
                        "item_type": "medical",
                        "title": f"Submit medical forms for {camp_name}",
                        "due_date": (date.fromisoformat(start_date) - timedelta(days=21)).isoformat() if start_date else None,
                        "auto_trigger": None
                    })
            except:
                pass
        else:
            items.append({
                "item_type": "medical",
                "title": f"Schedule physical for {child} — no recent visit found",
                "due_date": None,
                "auto_trigger": None
            })

    # Bus / meals
    if camp.get("bus_needed"):
        items.append({
            "item_type": "logistics",
            "title": "Sign up for bus",
            "due_date": deadline,
            "auto_trigger": None
        })
    if camp.get("meals_needed"):
        items.append({
            "item_type": "logistics",
            "title": "Sign up for meals",
            "due_date": deadline,
            "auto_trigger": None
        })

    # Packing list reminder
    if start_date:
        try:
            pack_date = (date.fromisoformat(start_date) - timedelta(days=2)).isoformat()
            items.append({
                "item_type": "packing",
                "title": "Prepare packing list for " + camp_name,
                "due_date": pack_date,
                "auto_trigger": None
            })
        except: pass

    # Save to DB — skip duplicates
    with _conn() as c:
        existing = {r["item_type"] + r["title"]
                    for r in c.execute(
                        "SELECT item_type, title FROM camp_checklist WHERE camp_id=? AND user_id=?",
                        (camp_id, user_id)).fetchall()}
        for item in items:
            key = item["item_type"] + item["title"]
            if key not in existing:
                c.execute(
                    "INSERT INTO camp_checklist(user_id,camp_id,item_type,title,due_date,auto_trigger) "
                    "VALUES(?,?,?,?,?,?)",
                    (user_id, camp_id, item["item_type"], item["title"],
                     item.get("due_date"), item.get("auto_trigger")))
        c.commit()

    return {"status": "ok", "items_created": len(items)}


@app.get("/camps/{camp_id}/checklist")
def get_camp_checklist(camp_id: int, user_id: str):
    from agent.calendar_agent import _conn
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM camp_checklist WHERE camp_id=? AND user_id=? ORDER BY due_date",
            (camp_id, user_id)).fetchall()
    return [dict(r) for r in rows]


@app.put("/camp-checklist/{item_id}/done")
def complete_checklist_item(item_id: int, user_id: str):
    from agent.calendar_agent import _conn
    with _conn() as c:
        c.execute("UPDATE camp_checklist SET status='done' WHERE id=? AND user_id=?",
                  (item_id, user_id))
        c.commit()
    return {"status": "done"}


@app.post("/prescriptions")
async def save_prescription(request: Request):
    import json
    from agent.calendar_agent import _conn
    import anthropic as ant
    body = await request.json()
    user_id = body.get("user_id", "")
    child   = body.get("child_name", "")
    text    = body.get("text", "")

    if not text:
        return {"prescriptions": [], "error": "No text"}

    prompt = (
        "Extract prescription/medication details from this text.\n"
        "Child: " + child + "\n"
        "Text: " + text + "\n\n"
        "Return ONLY a JSON array:\n"
        "[{\n"
        "  \"medication\": \"name\",\n"
        "  \"frequency\": \"daily/weekly/as needed\",\n"
        "  \"notes\": \"any extra info or null\"\n"
        "}]\n"
        "ONLY JSON."
    )
    try:
        client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model=cfg.CLAUDE_MODEL, max_tokens=500,
            messages=[{"role": "user", "content": prompt}])
        raw  = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
        raw  = re.sub(r"\s*```$", "", raw)
        meds = json.loads(raw)
        with _conn() as c:
            for med in meds:
                c.execute(
                    "INSERT INTO prescriptions(user_id,child_name,medication,frequency,notes) "
                    "VALUES(?,?,?,?,?)",
                    (user_id, child, med.get("medication",""),
                     med.get("frequency",""), med.get("notes","")))
            c.commit()
        return {"prescriptions": meds, "error": None}
    except Exception as e:
        return {"prescriptions": [], "error": str(e)}


@app.get("/prescriptions")
def get_prescriptions(user_id: str):
    from agent.calendar_agent import _conn
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM prescriptions WHERE user_id=? ORDER BY child_name, medication",
            (user_id,)).fetchall()
    return [dict(r) for r in rows]



# ── Camp lifecycle detection ────────────────────────────────────────────────

CAMP_LIFECYCLE_KEYWORDS = (
    "registered OR confirmation OR confirmed OR receipt OR welcome OR "
    "\"class selection\" OR \"course selection\" OR \"forms\" OR "
    "orientation OR \"what to bring\" OR \"pick-up\" OR \"pickup\" OR "
    "medical OR camper OR counselor OR waitlist OR payment"
)

def _check_camp_lifecycle(user_id: str, camp: dict) -> dict:
    """Search Gmail for lifecycle signals for a single camp and update its record."""
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from agent.calendar_agent import _conn
    import anthropic as ant

    camp_id   = camp["id"]
    camp_name = camp.get("camp_name", "")
    if not camp_name:
        return {"updated": False, "error": "no camp name"}

    emails = _list_connected_emails(user_id)
    if not emails:
        return {"updated": False, "error": "no gmail connected"}

    matched_emails = []
    for email in emails:
        token_data = _load_token(user_id, email)
        if not token_data:
            continue
        creds = Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        if not creds.valid and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_data["access_token"] = creds.token
                _save_token(user_id, email, token_data)
            except Exception:
                continue

        service = build("gmail", "v1", credentials=creds)
        query = '"' + camp_name + '" (' + CAMP_LIFECYCLE_KEYWORDS + ')'
        try:
            result   = service.users().messages().list(
                userId="me", q=query, maxResults=10).execute()
            messages = result.get("messages", [])
        except Exception as e:
            print("[lifecycle search] " + str(e))
            continue

        def extract_body(payload):
            data = payload.get("body", {}).get("data", "")
            if data and "text" in payload.get("mimeType", ""):
                try: return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                except: return ""
            for part in payload.get("parts", []):
                t = extract_body(part)
                if t: return t
            return ""

        for msg in messages[:8]:
            try:
                full    = service.users().messages().get(
                    userId="me", id=msg["id"], format="full").execute()
                headers = full["payload"].get("headers", [])
                subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
                date_h  = next((h["value"] for h in headers if h["name"] == "Date"), "")
                body    = extract_body(full["payload"])[:1500]
                if subject:
                    matched_emails.append({"subject": subject, "body": body, "date": date_h})
            except Exception:
                continue

    if not matched_emails:
        return {"updated": False, "found_emails": 0}

    digest = "\n".join(
        "--- " + e["subject"] + " (" + e.get("date","") + ") ---\n" + e["body"]
        for e in matched_emails)

    today_iso = __import__("datetime").date.today().isoformat()

    prompt = (
        "You are Hearth, a family concierge AI. Today is " + today_iso + ".\n"
        "Camp name: " + camp_name + "\n"
        "Camp type: " + (camp.get("camp_type") or "unknown") + "\n\n"
        "Here are emails mentioning this camp:\n" + digest[:4000] + "\n\n"
        "Determine the registration/lifecycle status. "
        "IMPORTANT: if ANY email implies registration is already complete "
        "(class selections, forms to fill out, orientation details, medical forms, "
        "pickup authorization, camper/counselor info, 'what to bring') then "
        "is_registered must be true, even if no explicit confirmation email exists.\n\n"
        "Return ONLY JSON:\n"
        "{\n"
        '  "is_registered": true or false,\n'
        '  "current_stage": "registered" | "class_selection_pending" | '
        '"forms_pending" | "orientation_known" | "ready" | "unknown",\n'
        '  "next_action": "short specific next step with deadline if mentioned, or null",\n'
        '  "forms_deadline": "YYYY-MM-DD or null",\n'
        '  "platform_mentioned": "Campanion" | "CampDoc" | "UltraCamp" | "CampBrain" | null\n'
        "}\n"
        "ONLY JSON, no other text."
    )

    try:
        client = ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model=cfg.CLAUDE_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}])
        raw  = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
        raw  = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        return {"updated": False, "error": str(e)}

    updates  = {}
    new_status = "registered" if data.get("is_registered") else camp.get("status")
    if new_status != camp.get("status"):
        updates["status"] = new_status

    if data.get("next_action"):
        updates["next_action"] = data["next_action"]

    platform = data.get("platform_mentioned")
    if platform and not camp.get("app_name"):
        for key, (app_name, deep_link) in PLATFORM_MAP.items():
            if app_name.lower() == platform.lower():
                updates["app_name"] = app_name
                if deep_link:
                    updates["deep_link_url"] = deep_link
                break

    updates["lifecycle_checked_at"] = __import__("datetime").datetime.now().isoformat()

    if updates:
        set_clause = ", ".join(k + "=?" for k in updates.keys())
        with _conn() as c:
            c.execute("UPDATE camps SET " + set_clause + " WHERE id=? AND user_id=?",
                      list(updates.values()) + [camp_id, user_id])
            c.commit()

    return {"updated": True, "found_emails": len(matched_emails), **data}


@app.post("/camps/{camp_id}/check-status")
def check_camp_status(camp_id: int, user_id: str):
    from agent.calendar_agent import _conn
    with _conn() as c:
        camp = c.execute("SELECT * FROM camps WHERE id=? AND user_id=?",
                         (camp_id, user_id)).fetchone()
    if not camp:
        return {"status": "not found"}
    result = _check_camp_lifecycle(user_id, dict(camp))
    return result



@app.post("/camps/{camp_id}/check-status-debug")
def check_camp_status_debug(camp_id: int, user_id: str):
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from agent.calendar_agent import _conn

    with _conn() as c:
        camp = c.execute("SELECT * FROM camps WHERE id=? AND user_id=?",
                         (camp_id, user_id)).fetchone()
    if not camp:
        return {"status": "not found"}
    camp = dict(camp)
    camp_name = camp.get("camp_name", "")

    emails = _list_connected_emails(user_id)
    debug_info = {"camp_name": camp_name, "emails_checked": [], "query": None}

    query = "\"" + camp_name + "\" (" + CAMP_LIFECYCLE_KEYWORDS + ")"
    debug_info["query"] = query

    for email in emails:
        entry = {"email": email}
        token_data = _load_token(user_id, email)
        if not token_data:
            entry["error"] = "no token"
            debug_info["emails_checked"].append(entry)
            continue
        creds = Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        if not creds.valid and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_data["access_token"] = creds.token
                _save_token(user_id, email, token_data)
            except Exception as e:
                entry["refresh_error"] = str(e)
                debug_info["emails_checked"].append(entry)
                continue

        service = build("gmail", "v1", credentials=creds)
        try:
            result   = service.users().messages().list(
                userId="me", q=query, maxResults=10).execute()
            messages = result.get("messages", [])
            entry["messages_found"] = len(messages)
            entry["resultSizeEstimate"] = result.get("resultSizeEstimate")
        except Exception as e:
            entry["search_error"] = str(e)
        debug_info["emails_checked"].append(entry)

    return debug_info

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=cfg.API_PORT, reload=True)
