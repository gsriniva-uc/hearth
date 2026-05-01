"""
agent/gmail_agent.py

Scans Gmail for school-related emails using Google OAuth 2.0 + Gmail REST API.
Runs automatically on app startup and on a daily schedule.
Deduplicates events before inserting — safe to run multiple times.
"""

import base64
import json
import os
import re
from datetime import date, timedelta

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hearth_config as cfg


# ── Constants ─────────────────────────────────────────────────────────────────

SCOPES           = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.path.join(cfg.DATA_DIR, "google_credentials.json")
TOKEN_FILE       = os.path.join(cfg.DATA_DIR, "google_token.json")

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
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    creds = None

    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"Google credentials not found at {CREDENTIALS_FILE}.\n"
            "Download your OAuth 2.0 client JSON from console.cloud.google.com "
            "and save it as data/google_credentials.json"
        )

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def is_authenticated() -> bool:
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        return creds and (creds.valid or creds.refresh_token)
    except Exception:
        return False


def is_credentials_configured() -> bool:
    return os.path.exists(CREDENTIALS_FILE)


# ── Deduplication ─────────────────────────────────────────────────────────────

def _event_exists(child_name: str, event_type: str, event_date: str) -> bool:
    """Returns True if an identical event is already in the DB."""
    import sqlite3
    if not os.path.exists(cfg.DB_PATH):
        return False
    with sqlite3.connect(cfg.DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM events WHERE child_name=? AND event_type=? AND event_date=?",
            (child_name, event_type, event_date),
        ).fetchone()
    return row is not None


# ── Gmail fetch ────────────────────────────────────────────────────────────────

def _fetch_recent_school_emails(service, days_back: int = 14) -> list:
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
            for header in full["payload"].get("headers", []):
                if header["name"] == "Subject":
                    subject = header["value"]

            body = _extract_body(full["payload"])
            if body:
                emails.append({"subject": subject, "body": body[:3000]})
        except Exception:
            continue

    return emails


def _extract_body(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if body_data and "text" in mime_type:
        try:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text

    return ""


# ── LLM extraction ────────────────────────────────────────────────────────────

def _extract_events_from_emails(emails: list) -> list:
    if not emails:
        return []

    today_str    = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(cfg.CHILDREN) if cfg.CHILDREN else "the children"

    email_digest = ""
    for i, em in enumerate(emails, 1):
        email_digest += f"\n--- Email {i}: {em['subject']} ---\n{em['body']}\n"

    prompt = f"""You are Hearth, a family calendar assistant. Today is {today_str}.
Children in this family: {children_str}.
Valid event_type values: {", ".join(EVENT_TYPES)}.

Below are {len(emails)} school-related emails.
Extract every upcoming school event, special day, dismissal, recital, field trip,
doctor appointment, no-school day, school holiday, or other family-relevant date.
Pay special attention to: days when school is closed, no-school notices, holiday breaks,
teacher workdays, and any day requiring different childcare arrangements.

{email_digest}

For each event return a JSON object:
{{
  "child_name":   "<child name from family list, or 'all'>",
  "event_type":   "<one of the valid types>",
  "event_date":   "<YYYY-MM-DD>",
  "event_time":   "<HH:MM 24h or null>",
  "notes":        "<brief description or null>",
  "source_email": "<subject line>"
}}

Rules:
- Only extract UPCOMING events (on or after today).
- Ignore past dates, page footers, sent dates, contact info.
- If same event appears in multiple emails, include it only once.

Return ONLY a JSON array. No prose, no markdown fences."""

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    resp   = client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$",     "", raw)

    try:
        events = json.loads(raw)
        return events if isinstance(events, list) else []
    except json.JSONDecodeError:
        return []


# ── Insert with dedup ─────────────────────────────────────────────────────────

def _insert_if_new(ev: dict) -> bool:
    """Insert event only if it doesn't already exist. Returns True if inserted."""
    from agent.calendar_agent import _insert_event

    child     = ev.get("child_name", "all")
    etype     = ev.get("event_type", "other")
    edate     = ev.get("event_date", "")

    if not edate:
        return False
    if _event_exists(child, etype, edate):
        return False

    try:
        _insert_event(
            child_name=child,
            event_type=etype,
            event_date=edate,
            event_time=ev.get("event_time"),
            notes=ev.get("notes"),
        )
        return True
    except Exception:
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def auto_scan_and_save(days_back: int = 14) -> dict:
    """
    Scans Gmail, extracts events, deduplicates, and saves directly to DB.
    No user review step — called automatically on app startup and daily cron.

    Returns: {new: int, skipped: int, emails_scanned: int, error: str|None}
    """
    try:
        service = get_gmail_service()
        emails  = _fetch_recent_school_emails(service, days_back=days_back)

        if not emails:
            return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}

        events  = _extract_events_from_emails(emails)
        new, skipped = 0, 0

        for ev in events:
            if _insert_if_new(ev):
                new += 1
            else:
                skipped += 1

        return {
            "new":            new,
            "skipped":        skipped,
            "emails_scanned": len(emails),
            "error":          None,
        }

    except FileNotFoundError as e:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": str(e)}
    except Exception as e:
        return {"new": 0, "skipped": 0, "emails_scanned": 0,
                "error": f"Gmail scan failed: {e}"}


def scan_gmail_for_school_events(days_back: int = 14) -> dict:
    """
    Manual scan with review — returns events without saving.
    Used by the Gmail tab for user-controlled review flow.
    """
    try:
        service = get_gmail_service()
        emails  = _fetch_recent_school_emails(service, days_back=days_back)

        if not emails:
            return {
                "events": [], "emails_scanned": 0,
                "response": f"No school-related emails found in the last {days_back} days.",
                "error": None,
            }

        events  = _extract_events_from_emails(emails)
        summary = (
            f"Scanned {len(emails)} email(s). Found {len(events)} upcoming event(s)."
        )

        return {"events": events, "emails_scanned": len(emails),
                "response": summary, "error": None}

    except FileNotFoundError as e:
        return {"events": [], "emails_scanned": 0, "response": "", "error": str(e)}
    except Exception as e:
        return {"events": [], "emails_scanned": 0, "response": "",
                "error": f"Gmail scan failed: {e}"}
