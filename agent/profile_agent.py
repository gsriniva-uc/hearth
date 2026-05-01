"""agent/profile_agent.py — Multi-user child profiles"""
import json, sqlite3, os
import anthropic
import hearth_config as cfg
from agent.state import HearthState

_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

def _conn():
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    c = sqlite3.connect(cfg.DB_PATH); c.row_factory = sqlite3.Row; return c

def init_profiles():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   TEXT NOT NULL DEFAULT 'default',
            name      TEXT NOT NULL,
            grade     TEXT,
            school    TEXT,
            activities TEXT,
            notes     TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, name)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id)")
        c.commit()

def get_all_profiles(user_id: str) -> list:
    with _conn() as c:
        rows = c.execute("SELECT * FROM profiles WHERE user_id=? ORDER BY name",
                         (user_id,)).fetchall()
    return [dict(r) for r in rows]

def get_children(user_id: str) -> list[str]:
    """Return list of child names for this user (falls back to CHILDREN env var)."""
    profiles = get_all_profiles(user_id)
    if profiles:
        return [p["name"] for p in profiles]
    return cfg.CHILDREN

def upsert_profile(user_id, name, grade=None, school=None, activities=None, notes=None):
    with _conn() as c:
        c.execute("""INSERT INTO profiles(user_id,name,grade,school,activities,notes)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(user_id,name) DO UPDATE SET
                       grade=excluded.grade, school=excluded.school,
                       activities=excluded.activities, notes=excluded.notes""",
                  (user_id, name, grade, school, activities, notes))
        c.commit()

def profile_agent(state: HearthState) -> HearthState:
    init_profiles()
    user_id = state.get("user_id") or "default"
    text    = state.get("raw_text","")

    prompt = f"""Extract profile info: "{text}"
Return JSON: {{"action":"add"|"list","name":"...","grade":"...","school":"...","activities":"...","notes":"..."}}
ONLY JSON."""
    resp = _client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=256,
        messages=[{"role":"user","content":prompt}])
    try: data = json.loads(resp.content[0].text.strip())
    except: data = {"action":"list"}

    if data.get("action") == "add" and data.get("name"):
        upsert_profile(user_id, data["name"], data.get("grade"), data.get("school"),
                       data.get("activities"), data.get("notes"))
        return {**state,"response":f"✅ Profile saved for {data['name']}."}

    profiles = get_all_profiles(user_id)
    if not profiles:
        return {**state,"response":"No child profiles yet. Say 'Add child Emma, grade 3, Lincoln School'"}
    lines = ["**Child profiles:**"]
    for p in profiles:
        lines.append(f"• **{p['name']}** — Grade {p.get('grade','?')} · {p.get('school','')}"
                     + (f" · {p.get('activities','')}" if p.get("activities") else ""))
    return {**state,"response":"\n".join(lines)}
