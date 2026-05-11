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
            print("[gcal] refresh failed: " + str(e))
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
    query   = (
        "after:" + after + " (subject:("
        "\"early dismissal\" OR \"no school\" OR \"half day\" OR \"field trip\" "
        "OR \"permission slip\" OR PTA OR \"parent teacher\" OR \"report card\" "
        "OR \"spirit day\" OR \"dress down\" OR \"dress code\" OR \"school closed\" "
        "OR \"school closure\" OR recital OR practice OR tryouts OR \"schedule change\" "
        "OR \"map test\" OR \"special event\" OR \"sign up\" OR newsletter "
        "OR cheerleading OR tumbling OR climbing OR karate OR piano OR music "
        "OR appointment OR checkup OR vaccination OR immunization OR dentist "
        "OR pediatric OR prescription OR refill "
        "OR \"bill due\" OR \"payment due\" OR invoice OR renewal OR insurance "
        "OR maintenance OR delivery OR repair "
        "OR invitation OR RSVP OR birthday OR party OR celebration "
        "OR reminder OR deadline OR rescheduled OR cancelled "
        "OR \"art show\" OR artwork OR performance OR showcase OR ceremony "
        "OR gymnastics OR gymnastic OR show OR exhibit OR concert OR assembly "
        "OR \"picture day\" OR \"rock climbing\"))"
    )
    result   = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
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
        "Valid event_type: dress_down_day,early_dismissal,recital,field_trip,"
        "special_day,doctor_appointment,sports_game,school_holiday,activity,bill,other\n"
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

            s = subject.lower()
            if any(k in s for k in ["refill", "prescription", "medication"]):
                ptype = "prescription"
                freq  = 30
            elif any(k in s for k in ["appointment", "schedule", "booking"]):
                ptype = "doctor"
                freq  = 90
            elif any(k in s for k in ["claim", "reimbursement", "eob"]):
                ptype = "insurance"
                freq  = 14
            elif any(k in s for k in ["pharmacy", "cvs", "walgreens", "rite aid"]):
                ptype = "pharmacy"
                freq  = 30
            else:
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
        "(\"payment due\" OR \"bill is ready\" OR \"amount due\" "
        "OR \"statement available\" OR \"your bill\" OR invoice "
        "OR \"balance due\" OR \"due date\" OR \"minimum payment\")"
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
            errors.append(email + ": " + str(r["error"]))
        else:
            total_new     += r.get("new", 0)
            total_skipped += r.get("skipped", 0)
            accounts_ok   += 1
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
    emails    = _list_connected_emails(req.user_id)
    if confirmed and emails:
        for ev in confirmed:
            try:
                gcal_summary = ev.get("notes") or ev.get("event_type","event").replace("_"," ").title()
                child = ev.get("child_name","")
                if child and child != "all":
                    gcal_summary = child + " - " + gcal_summary
                svc = _get_gcal_service(req.user_id, emails[0])
                if svc and not _gcal_event_exists(svc, gcal_summary, ev["event_date"]):
                    gcal_id = _write_to_gcal(req.user_id, emails[0], gcal_summary,
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=cfg.API_PORT, reload=True)
