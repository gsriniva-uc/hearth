"""
agent/calendar_agent.py

Owns the SQLite events store. Handles CRUD operations driven by LLM-parsed intent.
"""

import json
import sqlite3
from datetime import date

import anthropic

import hearth_config as cfg
from agent.state import HearthState


EVENT_TYPES = [
    "dress_down_day", "early_dismissal", "recital", "movie_night",
    "field_trip", "special_day", "doctor_appointment", "sports_game",
    "school_holiday", "other",
]

_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)


def _get_conn() -> sqlite3.Connection:
    import os
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                child_name     TEXT    NOT NULL,
                event_type     TEXT    NOT NULL,
                event_date     TEXT    NOT NULL,
                event_time     TEXT,
                notes          TEXT,
                nudge_sent_7d  INTEGER DEFAULT 0,
                nudge_sent_48h INTEGER DEFAULT 0,
                nudge_sent_day INTEGER DEFAULT 0,
                created_at     TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def _insert_event(child_name, event_type, event_date, event_time=None, notes=None) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (child_name, event_type, event_date, event_time, notes) VALUES (?,?,?,?,?)",
            (child_name, event_type, event_date, event_time, notes),
        )
        conn.commit()
        return cur.lastrowid


def _query_upcoming(days_ahead: int = 14) -> list:
    from datetime import timedelta
    today = date.today().isoformat()
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE event_date BETWEEN ? AND ? ORDER BY event_date",
            (today, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def _delete_event(event_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        conn.commit()
        return cur.rowcount > 0


def _parse_intent(text: str, input_type: str, extracted_events: list) -> dict:
    today_str = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(cfg.CHILDREN) if cfg.CHILDREN else "the children"

    if extracted_events:
        return {"action": "add", "events": extracted_events, "reply": ""}

    prompt = f"""You are the calendar agent for a family app called Hearth.
Today is {today_str}.
Children in this family: {children_str}.
Valid event_type values: {", ".join(EVENT_TYPES)}.

The user said: "{text}"
Input classification: {input_type}

Return a JSON object:
{{
  "action": "add" | "delete" | "query" | "update",
  "events": [
    {{
      "child_name": "<name or 'all'>",
      "event_type": "<one of the valid types>",
      "event_date": "<YYYY-MM-DD>",
      "event_time": "<HH:MM or null>",
      "notes": "<any extra context or null>"
    }}
  ],
  "delete_id": null,
  "query_window_days": 7,
  "reply": ""
}}

Resolve relative dates from today. Return ONLY the JSON."""

    resp = _client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"action": "query", "events": [], "query_window_days": 7, "reply": ""}


def calendar_agent(state: HearthState) -> HearthState:
    init_db()

    intent = _parse_intent(
        text=state.get("raw_text") or "",
        input_type=state.get("input_type", "query"),
        extracted_events=state.get("extracted_events", []),
    )

    action = intent.get("action", "query")
    confirmed = []
    reply_lines = []

    if action == "add":
        for ev in intent.get("events", []):
            try:
                eid = _insert_event(
                    child_name=ev.get("child_name", "all"),
                    event_type=ev.get("event_type", "other"),
                    event_date=ev["event_date"],
                    event_time=ev.get("event_time"),
                    notes=ev.get("notes"),
                )
                confirmed.append({**ev, "id": eid})
                label = ev.get("event_type", "event").replace("_", " ").title()
                reply_lines.append(
                    f"✅ Added **{label}** for {ev.get('child_name', 'all')} on {ev['event_date']}"
                    + (f" at {ev['event_time']}" if ev.get("event_time") else "")
                )
            except Exception as e:
                reply_lines.append(f"⚠️ Could not add event: {e}")

    elif action == "delete":
        did = intent.get("delete_id")
        if did and _delete_event(int(did)):
            reply_lines.append(f"🗑️ Event #{did} deleted.")
        else:
            reply_lines.append("⚠️ Couldn't find that event to delete.")

    elif action == "query":
        days = intent.get("query_window_days", 7)
        rows = _query_upcoming(days_ahead=days)
        if not rows:
            reply_lines.append(f"📅 No events in the next {days} days.")
        else:
            reply_lines.append(f"📅 **Upcoming events (next {days} days):**\n")
            for r in rows:
                label = r["event_type"].replace("_", " ").title()
                time_str = f" at {r['event_time']}" if r.get("event_time") else ""
                reply_lines.append(
                    f"• [{r['id']}] **{r['child_name']}** — {label} on {r['event_date']}{time_str}"
                )

    reply = "\n".join(reply_lines) if reply_lines else "Done."
    return {**state, "confirmed_events": confirmed, "response": reply}
