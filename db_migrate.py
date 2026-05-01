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

    conn.commit()
    conn.close()
    print("[migrate] Done.")

if __name__ == "__main__":
    migrate()
