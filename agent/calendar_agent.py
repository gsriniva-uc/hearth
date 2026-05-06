"""
agent/calendar_agent.py — Multi-user version

All queries scoped by user_id.
Schema includes user_id column (added via db_migrate.py).
"""
import json, sqlite3, os
from datetime import date, timedelta, datetime
import anthropic
import hearth_config as cfg
from agent.state import HearthState

EVENT_TYPES = [
    "dress_down_day","early_dismissal","recital","movie_night","field_trip",
    "special_day","doctor_appointment","sports_game","school_holiday","activity","other",
]
_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

# ── DB ─────────────────────────────────────────────────────────────────────────

def _conn():
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    c = sqlite3.connect(cfg.DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          TEXT    NOT NULL DEFAULT 'default',
            child_name       TEXT    NOT NULL,
            event_type       TEXT    NOT NULL,
            event_date       TEXT    NOT NULL,
            event_time       TEXT,
            notes            TEXT,
            nudge_sent_7d    INTEGER DEFAULT 0,
            nudge_sent_48h   INTEGER DEFAULT 0,
            nudge_sent_day   INTEGER DEFAULT 0,
            nudge_sent_1h    INTEGER DEFAULT 0,
            gcal_event_id    TEXT,
            outlook_event_id TEXT,
            created_at       TEXT    DEFAULT (datetime('now'))
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)")
        c.commit()

# ── CRUD (all scoped to user_id) ───────────────────────────────────────────────

def _insert_event(user_id, child_name, event_type, event_date,
                  event_time=None, notes=None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO events(user_id,child_name,event_type,event_date,event_time,notes)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, child_name, event_type, event_date, event_time, notes))
        c.commit()
        return cur.lastrowid

def _query_upcoming(user_id: str, days_ahead=14) -> list:
    today    = date.today().isoformat()
    cutoff   = (date.today()+timedelta(days=days_ahead)).isoformat()
    now_time = datetime.now().strftime("%H:%M")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE user_id=? AND event_date BETWEEN ? AND ?"
            " AND (event_date > ? OR event_time IS NULL OR event_time >= ?)"
            " ORDER BY event_date, event_time",
            (user_id, today, cutoff, today, now_time)).fetchall()
    return [dict(r) for r in rows]

def _query_today(user_id: str) -> list:
    today    = date.today().isoformat()
    now_time = datetime.now().strftime("%H:%M")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE user_id=? AND event_date=?"
            " AND (event_time IS NULL OR event_time >= ?)"
            " ORDER BY event_time",
            (user_id, today, now_time)).fetchall()
    return [dict(r) for r in rows]

def _delete_event(user_id: str, event_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM events WHERE id=? AND user_id=?", (event_id, user_id))
        c.commit()
        return cur.rowcount > 0

def _event_exists(user_id: str, child_name: str, event_type: str, event_date: str) -> bool:
    if not os.path.exists(cfg.DB_PATH): return False
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM events WHERE user_id=? AND child_name=?"
            " AND event_type=? AND event_date=?",
            (user_id, child_name, event_type, event_date)).fetchone()
    return row is not None

# ── Calendar sync ──────────────────────────────────────────────────────────────

def _sync_to_gcal(user_id, event_id, child_name, event_type,
                  event_date, event_time=None, notes=None):
    try:
        from agent.auth import get_credentials
        from googleapiclient.discovery import build
        creds = get_credentials(user_id)
        if not creds: return
        service = build("calendar","v3",credentials=creds)
        label   = event_type.replace("_"," ").title()
        start   = ({"dateTime":f"{event_date}T{event_time}:00","timeZone":"America/New_York"}
                   if event_time else {"date":event_date})
        end     = start
        body    = {"summary":f"{child_name}: {label}","description":notes or "",
                   "start":start,"end":end,
                   "reminders":{"useDefault":False,
                       "overrides":[{"method":"popup","minutes":1440},
                                    {"method":"email","minutes":1440}]}}
        ev = service.events().insert(calendarId="primary",body=body).execute()
        with _conn() as c:
            c.execute("UPDATE events SET gcal_event_id=? WHERE id=? AND user_id=?",
                      (ev["id"], event_id, user_id))
            c.commit()
    except Exception as e:
        print(f"[gcal] {e}")

def _sync_to_outlook(user_id, event_id, child_name, event_type,
                     event_date, event_time=None, notes=None):
    try:
        import requests as req
        if not os.path.exists(cfg.OUTLOOK_TOKEN_FILE): return
        with open(cfg.OUTLOOK_TOKEN_FILE) as f: td = json.load(f)
        token = td.get("access_token","")
        if not token: return
        label    = event_type.replace("_"," ").title()
        start_dt = f"{event_date}T{event_time}:00" if event_time else f"{event_date}T00:00:00"
        end_dt   = f"{event_date}T{event_time}:00" if event_time else f"{event_date}T23:59:00"
        body = {"subject":f"{child_name}: {label}",
                "body":{"contentType":"Text","content":notes or ""},
                "start":{"dateTime":start_dt,"timeZone":"Eastern Standard Time"},
                "end":  {"dateTime":end_dt,  "timeZone":"Eastern Standard Time"},
                "isAllDay": not event_time}
        r = req.post("https://graph.microsoft.com/v1.0/me/events", json=body,
                     headers={"Authorization":f"Bearer {token}",
                              "Content-Type":"application/json"}, timeout=15)
        if r.status_code == 201:
            with _conn() as c:
                c.execute("UPDATE events SET outlook_event_id=? WHERE id=? AND user_id=?",
                          (r.json().get("id",""), event_id, user_id))
                c.commit()
    except Exception as e:
        print(f"[outlook] {e}")

# ── LLM intent ─────────────────────────────────────────────────────────────────

def _parse_intent(text, input_type, extracted_events) -> dict:
    if extracted_events:
        return {"action":"add","events":extracted_events,"query_window_days":7}

    today_str    = date.today().strftime("%A, %B %d, %Y")
    today_iso    = date.today().isoformat()
    tomorrow_iso = (date.today()+timedelta(days=1)).isoformat()
    children_str = ", ".join(cfg.CHILDREN) or "the children"

    # Deterministic pre-check before hitting LLM
    text_lower = text.lower().strip()
    if text_lower.startswith("delete") or text_lower.startswith("remove"):
        # Extract event ID if present
        import re
        match = re.search(r"\d+", text_lower)
        delete_id = int(match.group()) if match else None
        return {"action":"delete","events":[],"delete_id":delete_id,"query_window_days":7}

    add_keywords = ["add ","adding ","schedule ","create ","put ","set up ",
                    "class","appointment","game","practice","lesson","session",
                    "swimming","gymnastics","soccer","dance","karate","piano",
                    "recital","dentist","doctor","therapy"]
    is_add = any(kw in text_lower for kw in add_keywords)
    if is_add:
        input_type = "manual"

    prompt = f"""Hearth calendar. Today: {today_str} ({today_iso}). Children: {children_str}.
"today" = {today_iso}, "tomorrow" = {tomorrow_iso}.
Resolve relative dates: "today"={today_iso}, "tomorrow"={tomorrow_iso}, "this Tuesday" etc.

Valid event_type: {", ".join(EVENT_TYPES)}.
Event type rules:
- sports_game: physical sports (swimming, gymnastics, soccer, dance, karate, tennis, baseball, rock climbing, tumbling, cheerleading)
- activity: non-sport classes (piano, art, tutoring, coding, music, drama, rock climbing, tumbling, cheerleading)
- recital: performances, concerts, shows only
- school_holiday: no school, holiday
- early_dismissal: early pickup/release
- dress_down_day: casual day, no uniform
- doctor_appointment: medical, dentist, therapy
- special_day: picture day, field trip, spirit day — always include what it is in notes
- other: ONLY if nothing else fits

User input: "{text}"
Input type: {input_type}

{"This looks like an ADD request — respond with action=add." if is_add else ""}

Return ONLY this JSON:
{{"action":"add"|"delete"|"query","events":[{{"child_name":"...","event_type":"...","event_date":"YYYY-MM-DD","event_time":"HH:MM|null","notes":"brief description"}}],"delete_id":null,"query_window_days":7}}"""

    resp = _client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=512,
        messages=[{"role":"user","content":prompt}])
    raw = resp.content[0].text.strip()
    import re as _re
    raw = _re.sub(r"^```json\s*","",raw)
    raw = _re.sub(r"\s*```$","",raw)
    try:
        return json.loads(raw)
    except:
        return {"action":"query","events":[],"query_window_days":7}

# ── Main node ───────────────────────────────────────────────────────────────────

def calendar_agent(state: HearthState) -> HearthState:
    init_db()
    user_id = state.get("user_id") or "default"
    intent  = _parse_intent(state.get("raw_text") or "",
                            state.get("input_type","query"),
                            state.get("extracted_events",[]))
    action, confirmed, lines = intent.get("action","query"), [], []

    if action == "add":
        for ev in intent.get("events",[]):
            try:
                eid = _insert_event(user_id, ev.get("child_name","all"),
                                    ev.get("event_type","other"), ev["event_date"],
                                    ev.get("event_time"), ev.get("notes"))
                confirmed.append({**ev,"id":eid})
                label = ev.get("event_type","event").replace("_"," ").title()
                from datetime import datetime
                try:
                    d = datetime.strptime(ev["event_date"], "%Y-%m-%d")
                    date_fmt = d.strftime("%m/%d/%y")
                except:
                    date_fmt = ev["event_date"]
                notes_label = ev.get("notes") or label
                time_str    = f" at {ev['event_time']}" if ev.get("event_time") else ""
                lines.append(f"✅ {ev.get('child_name','all')} — {notes_label} on {date_fmt}{time_str}")
                _sync_to_gcal(user_id, eid, ev.get("child_name","all"),
                              ev.get("event_type","other"), ev["event_date"],
                              ev.get("event_time"), ev.get("notes"))
                _sync_to_outlook(user_id, eid, ev.get("child_name","all"),
                                 ev.get("event_type","other"), ev["event_date"],
                                 ev.get("event_time"), ev.get("notes"))
            except Exception as e:
                lines.append(f"⚠️ Could not add: {e}")

    elif action == "delete":
        did = intent.get("delete_id")
        lines.append(f"🗑️ Deleted." if did and _delete_event(user_id, int(did))
                     else "⚠️ Event not found.")

    elif action == "query":
        days = intent.get("query_window_days",7)
        rows = _query_upcoming(user_id, days_ahead=days)
        if not rows:
            lines.append(f"📅 No events in the next {days} days.")
        else:
            lines.append(f"📅 **Next {days} days:**\n")
            for r in rows:
                label    = r["event_type"].replace("_"," ").title()
                time_str = f" at {r['event_time']}" if r.get("event_time") else ""
                from datetime import datetime
                try:
                    d = datetime.strptime(r["event_date"], "%Y-%m-%d")
                    date_fmt = d.strftime("%m/%d/%y")
                except:
                    date_fmt = r["event_date"]
                notes_str = f" ({r['notes']})" if r.get("notes") else ""
                lines.append(f"• **{r['child_name']}** — {label} on {date_fmt}{time_str}{notes_str}")

    return {**state, "confirmed_events":confirmed,
            "response":"\n".join(lines) or "Done.",
            "notify":bool(confirmed),
            "notify_message":"\n".join(lines) if confirmed else None}
