"""hearth_config.py — Family OS configuration"""
import os
from dotenv import load_dotenv
load_dotenv()

# ── Family ────────────────────────────────────────────────────────────────────
FAMILY_NAME  = os.getenv("FAMILY_NAME", "Our Family")
PARENT_NAME  = os.getenv("PARENT_NAME", "Mom")
PARENT_EMAIL = os.getenv("PARENT_EMAIL", "")
PARENT_PHONE = os.getenv("PARENT_PHONE", "")   # E.164 format e.g. +16175551234
CHILDREN     = [c.strip() for c in os.getenv("CHILDREN", "").split(",") if c.strip()]

# ── LLM ──────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── LangSmith ────────────────────────────────────────────────────────────────
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY", "")
LANGSMITH_TRACING = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "hearth")

if LANGSMITH_TRACING:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"]    = LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"]    = LANGSMITH_PROJECT

# ── Google OAuth (shared by auth + gmail + gcal) ─────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID",
    "289231572725-5fn10ulbb5hi6gqohnl1v6ourjsj01fu.apps.googleusercontent.com")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_SCOPES        = [
    "openid", "profile", "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/cloud-platform",
]

# ── Gmail OAuth ───────────────────────────────────────────────────────────────
DATA_DIR         = os.getenv("DATA_DIR", "data")
GOOGLE_CREDS     = os.path.join(DATA_DIR, "google_credentials.json")
GOOGLE_TOKEN     = os.path.join(DATA_DIR, "google_token.json")
DB_PATH          = os.path.join(DATA_DIR, "hearth.db")

# ── Outlook / Microsoft Graph ─────────────────────────────────────────────────
OUTLOOK_CLIENT_ID     = os.getenv("OUTLOOK_CLIENT_ID", "")
OUTLOOK_CLIENT_SECRET = os.getenv("OUTLOOK_CLIENT_SECRET", "")
OUTLOOK_TENANT_ID     = os.getenv("OUTLOOK_TENANT_ID", "common")
OUTLOOK_TOKEN_FILE    = os.path.join(DATA_DIR, "outlook_token.json")

# ── Twilio (SMS + WhatsApp) ───────────────────────────────────────────────────
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_PHONE   = os.getenv("TWILIO_FROM_PHONE", "")    # SMS sender
TWILIO_WHATSAPP_FROM= os.getenv("TWILIO_WHATSAPP_FROM", "") # whatsapp:+14155238886

# ── Notification preferences ──────────────────────────────────────────────────
# Comma-separated: email, sms, whatsapp
NOTIFY_CHANNELS = [c.strip() for c in os.getenv("NOTIFY_CHANNELS", "email").split(",")]

# ── Scheduler ────────────────────────────────────────────────────────────────
BRIEFING_HOUR   = int(os.getenv("BRIEFING_HOUR", "7"))
BRIEFING_MINUTE = int(os.getenv("BRIEFING_MINUTE", "0"))
NUDGE_SCAN_HOUR = int(os.getenv("NUDGE_SCAN_HOUR", "7"))
NUDGE_SCAN_MINUTE = int(os.getenv("NUDGE_SCAN_MINUTE", "15"))

# ── Nudge timing (days before event) ─────────────────────────────────────────
NUDGE_7D_ENABLED   = os.getenv("NUDGE_7D_ENABLED",  "true").lower()  == "true"
NUDGE_48H_ENABLED  = os.getenv("NUDGE_48H_ENABLED", "true").lower()  == "true"
NUDGE_MORNING_ENABLED = os.getenv("NUDGE_MORNING_ENABLED","true").lower() == "true"
NUDGE_1H_ENABLED   = os.getenv("NUDGE_1H_ENABLED",  "false").lower() == "true"

API_PORT = int(os.getenv("API_PORT", "8000"))

def is_configured() -> bool:
    return bool(ANTHROPIC_API_KEY and CHILDREN)

def get_family_summary() -> str:
    return f"{FAMILY_NAME} · {PARENT_NAME} · {', '.join(CHILDREN) or 'no children set'}"

def print_config():
    print(f"Family   : {FAMILY_NAME}")
    print(f"Children : {CHILDREN}")
    print(f"Channels : {NOTIFY_CHANNELS}")
    print(f"Twilio   : {'set' if TWILIO_ACCOUNT_SID else 'not set'}")
    print(f"Outlook  : {'set' if OUTLOOK_CLIENT_ID else 'not set'}")
    print(f"LangSmith: {'on → ' + LANGSMITH_PROJECT if LANGSMITH_TRACING else 'off'}")

if __name__ == "__main__":
    print_config()
