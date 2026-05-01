"""
agent/gmail_agent.py

Scans Gmail for school-related emails using Google OAuth 2.0 + Gmail REST API.

Setup (one-time per user):
  1. Go to console.cloud.google.com → New project → "Hearth"
  2. Enable Gmail API
  3. OAuth consent screen → External → add your email as test user
  4. Credentials → OAuth 2.0 Client ID → Desktop app → download JSON
  5. Save as data/google_credentials.json
  6. Run the app — first scan triggers browser OAuth flow → token saved to data/google_token.json
  7. All future scans use the saved token (auto-refreshed)
"""

import base64
import json
import os
import re
from datetime import date, timedelta, timezone, datetime
from email import message_from_bytes

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hearth_config as cfg


# ── Constants ─────────────────────────────────────────────────────────────────

SCOPES             = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE   = os.path.join(cfg.DATA_DIR, "google_credentials.json")
TOKEN_FILE         = os.path.join(cfg.DATA_DIR, "google_token.json")

SCHOOL_QUERY = (
    "(subject:(dismissal OR recital OR \"dress down\" OR \"field trip\" OR "
    "\"picture day\" OR \"spirit day\" OR newsletter OR \"school event\" OR "
    "\"early release\" OR \"school holiday\" OR \"no school\" OR \"day off\" OR "
    "\"schools closed\" OR \"school closed\" OR \"no classes\" OR \"holiday break\" OR "
    "\"parent teacher\" OR \"upcoming events\" OR performance OR fundraiser OR reminder) "
    "OR (\"no school\" OR \"school holiday\" OR \"early dismissal\" OR \"dress down\" "
    "OR \"field trip\" OR \"early release\" OR \"schools closed\"))"
)

EVENT_TYPES = [
    "dress_down_day", "early_dismissal", "recital", "movie_night",
    "field_trip", "special_day", "doctor_appointment", "sports_game",
    "school_holiday", "other",
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """
    Returns an authenticated Gmail API service.
    First call opens a browser for OAuth consent.
    Subsequent calls use the saved token (auto-refreshed).
    """
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    creds = None

    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"Google credentials not found at {CREDENTIALS_FILE}.\n"
            "Download your OAuth 2.0 client JSON from console.cloud.google.com "
            "and save it as data/google_credentials.json"
        )

    # Load saved token
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # Refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for next time
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def is_authenticated() -> bool:
    """Check if a valid token already exists — no browser needed."""
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        return creds and (creds.valid or creds.refresh_token)
    except Exception:
        return False


def is_credentials_configured() -> bool:
    return os.path.exists(CREDENTIALS_FILE)


# ── Gmail fetch ────────────────────────────────────────────────────────────────

def _fetch_recent_school_emails(service, days_back: int = 14) -> list[dict]:
    """Search Gmail and return a list of {subject, body, date} dicts."""
    after_date = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    query      = f"{SCHOOL_QUERY} after:{after_date}"

    result   = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = result.get("messages", [])

    emails = []
    for msg in messages:
        try:
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()

            subject = ""
            msg_date = ""
            for header in full["payload"].get("headers", []):
                if header["name"] == "Subject":
                    subject = header["value"]
                if header["name"] == "Date":
                    msg_date = header["value"]

            body = _extract_body(full["payload"])

            if body:
                emails.append({
                    "subject": subject,
                    "date":    msg_date,
                    "body":    body[:3000],   # cap per email to stay within token budget
                })
        except Exception:
            continue

    return emails


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text body from Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if body_data and "text" in mime_type:
        try:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    parts = payload.get("parts", [])
    for part in parts:
        text = _extract_body(part)
        if text:
            return text

    return ""


# ── LLM event extraction ──────────────────────────────────────────────────────

def _extract_events_from_emails(emails: list[dict]) -> list[dict]:
    """Send email bodies to Claude for structured event extraction."""
    if not emails:
        return []

    today_str    = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(cfg.CHILDREN) if cfg.CHILDREN else "the children"

    # Build email digest for Claude
    email_digest = ""
    for i, em in enumerate(emails, 1):
        email_digest += f"\n--- Email {i}: {em['subject']} ({em['date']}) ---\n{em['body']}\n"

    prompt = f"""You are Hearth, a family calendar assistant. Today is {today_str}.
Children in this family: {children_str}.
Valid event_type values: {", ".join(EVENT_TYPES)}.

Below are {len(emails)} school-related emails from the past few days.
Extract every upcoming school event, special day, dismissal, recital, field trip,
doctor appointment, no-school day, school holiday, or other family-relevant date from these emails.
Pay special attention to: days when school is closed, no-school notices, holiday breaks,
teacher workdays, and any day requiring different childcare arrangements.

{email_digest}

For each event, return a JSON object:
{{
  "child_name": "<child name from family list, or 'all' if it applies to everyone>",
  "event_type": "<one of the valid types>",
  "event_date": "<YYYY-MM-DD — resolve all relative and partial dates from today>",
  "event_time": "<HH:MM 24h format, or null>",
  "notes":      "<brief plain-English description, or null>",
  "source_email": "<subject line of the email this came from>"
}}

Rules:
- Only extract UPCOMING events (on or after today).
- Ignore past dates, page footers, sent dates, and contact info.
- If a date is ambiguous (e.g. "the 15th" with no month), use the next upcoming occurrence.
- If the same event appears in multiple emails, include it only once.

Return ONLY a JSON array of event objects. No prose, no markdown fences."""

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    resp   = client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = resp.content[0].text.strip()

    # Strip markdown fences if Claude added them
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$",     "", raw)

    try:
        events = json.loads(raw)
        return events if isinstance(events, list) else []
    except json.JSONDecodeError:
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def scan_gmail_for_school_events(days_back: int = 14) -> dict:
    """
    Main entry point called by the Streamlit UI.

    Returns:
      {
        "events":         list of structured event dicts,
        "emails_scanned": int,
        "response":       human-readable summary string,
        "error":          str or None
      }
    """
    try:
        service = get_gmail_service()
        emails  = _fetch_recent_school_emails(service, days_back=days_back)

        if not emails:
            return {
                "events":         [],
                "emails_scanned": 0,
                "response":       f"No school-related emails found in the last {days_back} days.",
                "error":          None,
            }

        events = _extract_events_from_emails(emails)

        summary = (
            f"Scanned {len(emails)} school-related email(s) from the last {days_back} days. "
            f"Found {len(events)} upcoming event(s)."
        )

        return {
            "events":         events,
            "emails_scanned": len(emails),
            "response":       summary,
            "error":          None,
        }

    except FileNotFoundError as e:
        return {"events": [], "emails_scanned": 0, "response": "", "error": str(e)}
    except Exception as e:
        return {"events": [], "emails_scanned": 0, "response": "",
                "error": f"Gmail scan failed: {e}"}
