"""
agent/outlook_agent.py

Outlook email scan + send via Microsoft Graph (MSAL OAuth).

Setup:
  1. portal.azure.com → App registrations → New registration
  2. Name: Hearth, Supported account types: Personal Microsoft accounts
  3. Authentication → Add platform → Mobile/desktop → redirect: http://localhost
  4. API permissions → Microsoft Graph → Mail.Read, Mail.Send, Calendars.ReadWrite
  5. Copy Application (client) ID → OUTLOOK_CLIENT_ID in .env
  Note: For personal accounts, client secret is not needed for delegated flow
"""
import json, os, re
from datetime import date, timedelta
import anthropic
import msal
import requests
import hearth_config as cfg

SCOPES = ["Mail.Read","Mail.Send","Calendars.ReadWrite","User.Read"]
GRAPH  = "https://graph.microsoft.com/v1.0"
EVENT_TYPES = ["dress_down_day","early_dismissal","recital","movie_night","field_trip",
               "special_day","doctor_appointment","sports_game","school_holiday","other"]
SCHOOL_KEYWORDS = [
    "dismissal","recital","dress down","field trip","picture day","spirit day",
    "newsletter","school event","early release","school holiday","no school",
    "holiday break","parent teacher","upcoming events","performance","fundraiser",
]

def _get_msal_app():
    return msal.PublicClientApplication(
        cfg.OUTLOOK_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{cfg.OUTLOOK_TENANT_ID}")

def _load_token():
    if not os.path.exists(cfg.OUTLOOK_TOKEN_FILE): return None
    with open(cfg.OUTLOOK_TOKEN_FILE) as f: return json.load(f)

def _save_token(token):
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    with open(cfg.OUTLOOK_TOKEN_FILE,"w") as f: json.dump(token, f)

def get_access_token():
    app = _get_msal_app()
    accounts = app.get_accounts()
    if accounts:
        token = app.acquire_token_silent(SCOPES, account=accounts[0])
        if token and "access_token" in token:
            _save_token(token); return token["access_token"]
    # Interactive login
    token = app.acquire_token_interactive(scopes=SCOPES)
    if "access_token" in token:
        _save_token(token); return token["access_token"]
    raise Exception(f"Outlook auth failed: {token.get('error_description','')}")

def is_configured(): return bool(cfg.OUTLOOK_CLIENT_ID)
def is_authenticated():
    if not os.path.exists(cfg.OUTLOOK_TOKEN_FILE): return False
    try:
        app = _get_msal_app()
        return bool(app.get_accounts())
    except: return False

def _fetch_school_emails(access_token, days_back=14):
    after = (date.today()-timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")
    filter_expr = f"receivedDateTime ge {after}"
    headers = {"Authorization":f"Bearer {access_token}"}
    url = f"{GRAPH}/me/messages?$filter={filter_expr}&$top=50&$select=subject,body,receivedDateTime"
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200: return []
    messages = resp.json().get("value", [])
    school_emails = []
    for msg in messages:
        subject = msg.get("subject","").lower()
        body    = msg.get("body",{}).get("content","").lower()
        if any(kw in subject or kw in body for kw in SCHOOL_KEYWORDS):
            school_emails.append({
                "subject": msg.get("subject",""),
                "body":    msg.get("body",{}).get("content","")[:3000],
            })
    return school_emails

def _extract_events(emails):
    if not emails: return []
    today_str    = date.today().strftime("%A, %B %d, %Y")
    from agent.profile_agent import get_children
    children_str = ", ".join(get_children(user_id)) or "the children"
    digest = "".join(f"\n--- {e['subject']} ---\n{e['body']}\n" for e in emails)
    prompt = f"""Hearth assistant. Today: {today_str}. Children: {children_str}.
Valid event_type: {", ".join(EVENT_TYPES)}.
{len(emails)} school emails from Outlook:
{digest}
Extract upcoming events. Return JSON array:
[{{"child_name":"...","event_type":"...","event_date":"YYYY-MM-DD","event_time":"HH:MM|null","notes":"...","source_email":"..."}}]
ONLY JSON array."""
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    resp = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=2048,
        messages=[{"role":"user","content":prompt}])
    raw = re.sub(r"^```json\s*","",resp.content[0].text.strip())
    raw = re.sub(r"\s*```$","",raw)
    try:
        events = json.loads(raw); return events if isinstance(events,list) else []
    except: return []

def auto_scan_and_save(days_back=14):
    if not is_configured():
        return {"new":0,"skipped":0,"emails_scanned":0,"error":"Outlook not configured"}
    try:
        access_token = get_access_token()
        emails  = _fetch_school_emails(access_token, days_back)
        if not emails: return {"new":0,"skipped":0,"emails_scanned":0,"error":None}
        events = _extract_events(emails)
        from agent.gmail_agent import _insert_if_new
        new = skipped = 0
        for ev in events:
            if _insert_if_new(ev): new += 1
            else: skipped += 1
        return {"new":new,"skipped":skipped,"emails_scanned":len(emails),"error":None}
    except Exception as e:
        return {"new":0,"skipped":0,"emails_scanned":0,"error":str(e)}
