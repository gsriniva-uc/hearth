"""
db_migrate.py

Run once to add user_id column to existing events and profiles tables.
Safe to run on a fresh DB or an existing one.

Usage:
    python db_migrate.py
"""
import sqlite3, os
import hearth_config as cfg

def migrate():
    if not os.path.exists(cfg.DB_PATH):
        print(f"[migrate] DB not found at {cfg.DB_PATH} — will be created fresh on first run.")
        return

    conn = sqlite3.connect(cfg.DB_PATH)
    cur  = conn.cursor()

    # ── events table ─────────────────────────────────────────────────────────
    cols = [r[1] for r in cur.execute("PRAGMA table_info(events)").fetchall()]

    if "user_id" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN user_id TEXT DEFAULT 'default'")
        print("[migrate] ✅ Added user_id to events")
    else:
        print("[migrate] events.user_id already exists")

    if "nudge_sent_1h" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN nudge_sent_1h INTEGER DEFAULT 0")
        print("[migrate] ✅ Added nudge_sent_1h to events")

    if "gcal_event_id" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN gcal_event_id TEXT")
        print("[migrate] ✅ Added gcal_event_id to events")

    if "outlook_event_id" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN outlook_event_id TEXT")
        print("[migrate] ✅ Added outlook_event_id to events")

    # ── profiles table ────────────────────────────────────────────────────────
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    if "profiles" in tables:
        pcols = [r[1] for r in cur.execute("PRAGMA table_info(profiles)").fetchall()]
        if "user_id" not in pcols:
            cur.execute("ALTER TABLE profiles ADD COLUMN user_id TEXT DEFAULT 'default'")
            print("[migrate] ✅ Added user_id to profiles")
        else:
            print("[migrate] profiles.user_id already exists")


    # ── tasks table ──────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           TEXT    NOT NULL,
            task_type         TEXT    NOT NULL,
            title             TEXT    NOT NULL,
            status            TEXT    DEFAULT 'pending',
            due_date          TEXT,
            draft_to          TEXT,
            draft_subject     TEXT,
            draft_body        TEXT,
            payment_url       TEXT,
            amount            TEXT,
            company_login_url TEXT,
            recurrence_days   INTEGER,
            last_triggered    TEXT,
            contact_name      TEXT,
            contact_email     TEXT,
            child_name        TEXT,
            source_email_date TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            snoozed_until     TEXT,
            thumbs            TEXT
        )
    """)

    # ── patterns table ────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          TEXT    NOT NULL,
            pattern_type     TEXT    NOT NULL,
            contact_email    TEXT,
            contact_name     TEXT,
            child_name       TEXT,
            keywords         TEXT,
            frequency_days   INTEGER DEFAULT 30,
            last_action_date TEXT,
            next_due_date    TEXT,
            confidence_score REAL    DEFAULT 1.0,
            created_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    print("[migrate] tasks and patterns tables ready")

    conn.commit()
    conn.close()
    print("[migrate] Done.")

if __name__ == "__main__":
    migrate()
