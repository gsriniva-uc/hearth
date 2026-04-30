"""
agent/state.py

Shared LangGraph state definition and SQLite DB initialisation for Hearth.
All agent nodes import HearthState from here.
"""

import sqlite3
import uuid
from datetime import datetime
from typing import TypedDict, Optional

from hearth_config import DB_PATH, DATA_DIR
import os

# ── Ensure data dir exists ────────────────────────────────────────────────────
os.makedirs(DATA_DIR, exist_ok=True)


# ── LangGraph shared state ─────────────────────────────────────────────────────
class HearthState(TypedDict):
    # Input
    input_type: str          # "pdf" | "manual" | "nl_command" | "query" | "unknown"
    raw_text: Optional[str]  # typed text / NL command
    pdf_bytes: Optional[bytes]

    # Extracted from PDF
    extracted_events: list   # list of dicts before DB write

    # DB results
    confirmed_events: list   # events written or fetched

    # Final reply to surface in UI
    response: str


# ── SQLite schema ─────────────────────────────────────────────────────────────
EVENT_TYPES = [
    "dress_down",
    "early_dismissal",
    "recital",
    "movie_night",
    "field_trip",
    "special_day",
    "doctor_appointment",
    "other",
]


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id              TEXT PRIMARY KEY,
            child_name      TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            event_date      TEXT NOT NULL,
            event_time      TEXT,
            notes           TEXT,
            nudge_sent_7d   INTEGER DEFAULT 0,
            nudge_sent_48h  INTEGER DEFAULT 0,
            nudge_sent_day  INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_event_date ON events(event_date);
        CREATE INDEX IF NOT EXISTS idx_child_name ON events(child_name);
    """)
    conn.commit()
    conn.close()


# ── DB helpers ────────────────────────────────────────────────────────────────
def insert_event(
    child_name: str,
    event_type: str,
    event_date: str,
    event_time: str = None,
    notes: str = None,
) -> dict:
    """Insert a single event. Returns the full row as a dict."""
    event_id = str(uuid.uuid4())[:8]
    created_at = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO events
           (id, child_name, event_type, event_date, event_time, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (event_id, child_name, event_type, event_date, event_time, notes, created_at),
    )
    conn.commit()
    conn.close()
    return {
        "id": event_id,
        "child_name": child_name,
        "event_type": event_type,
        "event_date": event_date,
        "event_time": event_time,
        "notes": notes,
    }


def fetch_upcoming_events(days_ahead: int = 30) -> list[dict]:
    """Return events in the next N days, ordered by date."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM events
           WHERE event_date >= date('now')
             AND event_date <= date('now', ? || ' days')
           ORDER BY event_date""",
        (str(days_ahead),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_event(event_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
