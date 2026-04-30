"""
hearth_config.py

Family configuration — read from environment variables or .env file.
Locally: set values in your .env file.
On Render: set as environment variables in the dashboard.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Family Identity ───────────────────────────────────────────────────────────
FAMILY_NAME   = os.getenv("FAMILY_NAME", "Our Family")
PARENT_NAME   = os.getenv("PARENT_NAME", "Mom")
PARENT_EMAIL  = os.getenv("PARENT_EMAIL", "")

# ── Children (comma-separated names) ─────────────────────────────────────────
_children_raw = os.getenv("CHILDREN", "")
CHILDREN = [c.strip() for c in _children_raw.split(",") if c.strip()]

# ── LLM ──────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ── LangSmith ────────────────────────────────────────────────────────────────
LANGSMITH_API_KEY  = os.getenv("LANGSMITH_API_KEY", "")
LANGSMITH_TRACING  = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
LANGSMITH_PROJECT  = os.getenv("LANGSMITH_PROJECT", "hearth")

# Must be set before any langgraph/langchain imports in other modules.
# hearth_config is always imported first, so this runs at the right time.
if LANGSMITH_TRACING:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"]    = LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"]    = LANGSMITH_PROJECT

# ── Nudge Dispatcher ─────────────────────────────────────────────────────────
POWER_AUTOMATE_WEBHOOK = os.getenv("POWER_AUTOMATE_WEBHOOK", "")

# ── Scheduler ────────────────────────────────────────────────────────────────
NUDGE_SCAN_HOUR   = int(os.getenv("NUDGE_SCAN_HOUR", "7"))
NUDGE_SCAN_MINUTE = int(os.getenv("NUDGE_SCAN_MINUTE", "0"))

# ── Data paths ────────────────────────────────────────────────────────────────
DATA_DIR = os.getenv("DATA_DIR", "data")
DB_PATH  = os.path.join(DATA_DIR, "hearth.db")

# ── API ───────────────────────────────────────────────────────────────────────
API_PORT = int(os.getenv("API_PORT", "8000"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def is_configured() -> bool:
    return bool(ANTHROPIC_API_KEY) and bool(CHILDREN)

def get_family_summary() -> str:
    return f"{FAMILY_NAME} · {PARENT_NAME} · Children: {', '.join(CHILDREN) or 'none set'}"

def print_config():
    print(f"Family   : {FAMILY_NAME}")
    print(f"Parent   : {PARENT_NAME} <{PARENT_EMAIL}>")
    print(f"Children : {CHILDREN}")
    print(f"LLM      : {CLAUDE_MODEL}")
    print(f"LangSmith: {'enabled → project=' + LANGSMITH_PROJECT if LANGSMITH_TRACING else 'disabled'}")
    print(f"Webhook  : {'set' if POWER_AUTOMATE_WEBHOOK else 'not set (nudges log to console)'}")

if __name__ == "__main__":
    print_config()
