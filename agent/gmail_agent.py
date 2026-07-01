"""
agent/gmail_agent.py — Multi-user version

Token stored per user: data/tokens/{user_id}/google_token.json
Credentials file shared: data/google_credentials.json

Email collection uses 3 prongs (merged by message ID before extraction):
  1. Keyword query — fast, catches obvious school/event language
  2. Sender allowlist — all emails from senders matching school names in child profiles
  3. Full inbox scan — all emails in the window, catches anything prongs 1+2 miss
"""
import base64, json, os, re
from datetime import date, timedelta
import anthropic
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import hearth_config as cfg
from agent.auth import get_credentials, token_path

EVENT_TYPES = [
    # School
    "dress_down_day", "early_dismissal", "field_trip", "picture_day",
    "school_holiday", "school_event", "parent_teacher_conference",
    # Activities & classes
    "camp", "class", "practice", "recital", "performance", "sports_game",
    "tournament", "tryout",
    # Health
    "doctor_appointment", "dentist_appointment", "therapy_appointment",
    # Registrations & payments
    "registration_confirmed", "payment_confirmed", "registration_deadline",
    # Misc
    "movie_night", "special_day", "other",
]

def is_credentials_configured(): return os.path.exists(cfg.GOOGLE_CREDS)
def is_authenticated(user_id: str) -> bool: return get_credentials(user_id) is not None

def _get_service(user_id: str):
    creds = get_credentials(user_id)
    if not creds: raise Exception("Not authenticated — please sign in first")
    return build("gmail","v1",credentials=creds)

def _extract_body(payload):
    data = payload.get("body",{}).get("data","")
    if data and "text" in payload.get("mimeType",""):
        try: return base64.urlsafe_b64decode(data).decode("utf-8",errors="ignore")
        except: return ""
    for part in payload.get("parts",[]):
        t = _extract_body(part)
        if t: return t
    return ""

def _parse_message(full) -> dict | None:
    """Return a normalised email dict from a Gmail full-message response, or None if no body."""
    headers = full["payload"].get("headers", [])
    subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
    sender  = next((h["value"] for h in headers if h["name"] == "From"), "")
    body    = _extract_body(full["payload"])
    if not body:
        return None
    return {"id": full["id"], "subject": subject, "sender": sender, "body": body[:3000]}

def _fetch_batch(service, message_refs: list) -> list:
    """Fetch full messages for a list of {id} refs; skip failures."""
    emails = []
    for msg in message_refs:
        try:
            full = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            parsed = _parse_message(full)
            if parsed:
                emails.append(parsed)
        except:
            continue
    return emails

# ── Prong 1: keyword search (broad — any child activity language) ──────────────

ACTIVITY_QUERY = (
    "subject:(camp OR class OR practice OR registration OR confirmed OR "
    "\"sign up\" OR enrolled OR schedule OR recital OR performance OR "
    "tournament OR tryout OR appointment OR dismissal OR \"field trip\" OR "
    "newsletter OR \"no school\" OR \"early release\" OR \"school holiday\")"
)

def _fetch_by_keyword(service, after: str, max_results=75) -> list:
    result = service.users().messages().list(
        userId="me", q=f"{ACTIVITY_QUERY} after:{after}", maxResults=max_results
    ).execute()
    return _fetch_batch(service, result.get("messages", []))

# ── Prong 2: sender allowlist (school names from child profiles) ───────────────

def _school_names_for_user(user_id: str) -> list[str]:
    """Return lowercased school names from all child profiles for this user."""
    try:
        from agent.profile_agent import get_all_profiles
        profiles = get_all_profiles(user_id)
        return [p["school"].lower() for p in profiles if p.get("school")]
    except:
        return []

def _fetch_by_sender(service, after: str, school_names: list, max_results=50) -> list:
    if not school_names:
        return []
    # Build a Gmail OR query matching each school name in the From field
    from_clauses = " OR ".join(f'from:"{name}"' for name in school_names)
    query = f"({from_clauses}) after:{after}"
    result = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    return _fetch_batch(service, result.get("messages", []))

# ── Prong 3: full inbox scan ───────────────────────────────────────────────────

def _fetch_all_inbox(service, after: str, max_results=200) -> list:
    result = service.users().messages().list(
        userId="me", q=f"after:{after}", maxResults=max_results
    ).execute()
    return _fetch_batch(service, result.get("messages", []))

# ── Merge + extract ────────────────────────────────────────────────────────────

def _merge_emails(*email_lists) -> list:
    """Deduplicate by message ID, preserving first-seen order."""
    seen = set()
    merged = []
    for lst in email_lists:
        for email in lst:
            if email["id"] not in seen:
                seen.add(email["id"])
                merged.append(email)
    return merged

def _fetch_emails(user_id: str, days_back=30) -> list:
    service = _get_service(user_id)
    after   = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")

    prong1 = _fetch_by_keyword(service, after)
    prong2 = _fetch_by_sender(service, after, _school_names_for_user(user_id))
    prong3 = _fetch_all_inbox(service, after)

    return _merge_emails(prong1, prong2, prong3)

def _extract_events(emails, children):
    if not emails: return []
    today_str    = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(children) or "the children"
    digest = "".join(f"\n--- FROM: {e['sender']} | SUBJECT: {e['subject']} ---\n{e['body']}\n" for e in emails)
    prompt = f"""You are Hearth, a family calendar assistant. Today: {today_str}. Children: {children_str}.

Your job is to extract ANYTHING from these emails that belongs on a family calendar or represents a commitment made for the children. Think broadly — not just school events but ALL kids activities.

Extract:
- School events (no school days, early dismissals, field trips, picture day, etc.)
- Classes and lessons (skating, swimming, dance, coding, art, music, etc.)
- Camps (summer camps, day camps, sports camps — including confirmation/registration emails)
- Sports (practices, games, tournaments, tryouts)
- Appointments (doctor, dentist, therapy)
- Performances and recitals
- Registration confirmations and payment receipts — these mean the child IS enrolled, extract the activity start date or session dates
- Deadlines (registration deadlines, form submission deadlines)

Valid event_type values: {", ".join(EVENT_TYPES)}

Use registration_confirmed when an email confirms a child is registered/enrolled/paid for something.
Use payment_confirmed when a payment receipt is found — extract what it was for and when the activity starts.

{len(emails)} emails:
{digest}

Return a JSON array of all events found. For each event:
- child_name: name of the child it applies to, or "all" if all children
- event_type: one of the valid types above
- event_date: YYYY-MM-DD (use activity start date for registrations; skip if truly no date found)
- event_time: HH:MM or null
- notes: brief description including activity name, location, or amount paid if relevant
- source_email: the email subject

Only include events with a date. ONLY return a JSON array, no other text."""
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    resp   = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=2048,
        messages=[{"role":"user","content":prompt}])
    raw = re.sub(r"^```json\s*","",resp.content[0].text.strip())
    raw = re.sub(r"\s*```$","",raw)
    try:
        evs = json.loads(raw); return evs if isinstance(evs,list) else []
    except: return []

def auto_scan_and_save(user_id: str, children: list, days_back=30) -> dict:
    """Scan Gmail for this user and save new events (deduped) to their event store."""
    from agent.calendar_agent import _insert_event, _event_exists, _sync_to_gcal, _sync_to_outlook
    try:
        emails = _fetch_emails(user_id, days_back)
        if not emails: return {"new":0,"skipped":0,"emails_scanned":0,"error":None}
        events = _extract_events(emails, children)
        new = skipped = 0
        for ev in events:
            child, etype, edate = ev.get("child_name","all"), ev.get("event_type","other"), ev.get("event_date","")
            if not edate: continue
            if _event_exists(user_id, child, etype, edate):
                skipped += 1; continue
            eid = _insert_event(user_id, child, etype, edate, ev.get("event_time"), ev.get("notes"))
            _sync_to_gcal(user_id, eid, child, etype, edate, ev.get("event_time"), ev.get("notes"))
            new += 1
        return {"new":new,"skipped":skipped,"emails_scanned":len(emails),"error":None}
    except Exception as e:
        return {"new":0,"skipped":0,"emails_scanned":0,"error":str(e)}
