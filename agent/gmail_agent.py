"""
agent/gmail_agent.py — Single source of truth for all Gmail scanning.

All email scanning logic lives here. main.py imports scan_gmail_account
and auto_scan_and_save — nothing email-related is defined in main.py.

scan_gmail_account(user_id, email)
    Full 3-pass scan for one connected Gmail account:
      Pass 1 — keyword match on subject (fast, high-precision)
      Pass 2 — full inbox (catch-all)
      Merged → blocked sender filter → allowlist → Claude triage
      → full body fetch → Claude extraction → save to DB + GCal

auto_scan_and_save(user_id, children, days_back)
    Wrapper: scans all connected Gmail accounts for a user.
"""
import base64, json, os, re
from datetime import date, timedelta
import anthropic
from googleapiclient.discovery import build
import hearth_config as cfg
from agent.auth import (
    get_credentials, list_connected_emails,
    get_fresh_credentials, load_email_token,
)

# ── Event types ────────────────────────────────────────────────────────────────

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
    "movie_night", "special_day", "activity", "school_fundraiser", "other",
]

# ── Known family platform domains (auto-allowlisted) ──────────────────────────

KNOWN_FAMILY_DOMAINS = [
    "seesaw.me", "classdojo.com", "remind.com", "parentsquare.com",
    "campintouch.com", "campanion.com", "campdoc.com", "ultracamp.com",
    "campminder.com", "mybrightwheel.com", "schoolmessenger.com",
    "finalforms.com", "campbrain.com", "myschoolapp.com", "blackbaud.com",
    "powerschool.com", "transact.com", "campnetwork.com",
]

# ── Keyword query (subject-level, fast pass) ───────────────────────────────────

ACTIVITY_QUERY = (
    "subject:(camp OR class OR practice OR registration OR confirmed OR "
    "\"sign up\" OR enrolled OR schedule OR recital OR performance OR "
    "tournament OR tryout OR appointment OR dismissal OR \"field trip\" OR "
    "newsletter OR \"no school\" OR \"early release\" OR \"school holiday\" OR "
    "reminder OR RSVP OR invoice OR payment OR festival OR birthday OR show OR "
    "concert OR gymnastics OR skating OR swim OR swimming OR dance OR ballet OR "
    "soccer OR baseball OR softball OR tennis OR karate OR martial OR chess OR "
    "coding OR robotics OR art OR music OR club OR league OR team OR "
    "\"art show\" OR \"picture day\" OR \"spirit day\" OR "
    "\"school closed\" OR \"half day\" OR \"parent teacher\" OR "
    "\"summer camp\" OR \"camp registration\" OR \"camp enrollment\" OR "
    "\"camp forms\" OR \"camp orientation\" OR camper OR Campanion OR "
    "\"action required\" OR \"final reminder\" OR enrollment OR form OR forms OR "
    "welcome OR orientation OR waiver OR permission OR activity OR activities)"
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def is_credentials_configured() -> bool:
    return os.path.exists(cfg.GOOGLE_CREDS)

def is_authenticated(user_id: str) -> bool:
    return get_credentials(user_id) is not None

def _extract_body(payload) -> str:
    data = payload.get("body", {}).get("data", "")
    if data and "text" in payload.get("mimeType", ""):
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        except:
            return ""
    for part in payload.get("parts", []):
        t = _extract_body(part)
        if t:
            return t
    return ""

def _is_family_platform_sender(from_addr: str, extra_keywords: list) -> bool:
    f = from_addr.lower()
    for d in KNOWN_FAMILY_DOMAINS:
        if d in f:
            return True
    for kw in extra_keywords:
        if kw and len(kw) > 3 and kw.lower() in f:
            return True
    return False

def _extra_allowlist_keywords(user_id: str) -> list[str]:
    """School and camp names from this user's profiles — used for sender allowlisting."""
    keywords = []
    try:
        from agent.profile_agent import get_all_profiles
        for p in get_all_profiles(user_id):
            if p.get("school"):
                keywords += re.findall(r"[A-Za-z]{4,}", p["school"])
    except:
        pass
    try:
        from agent.calendar_agent import _conn
        with _conn() as c:
            for r in c.execute(
                "SELECT DISTINCT camp_name FROM camps WHERE user_id=?",
                (user_id,)).fetchall():
                keywords += re.findall(r"[A-Za-z]{4,}", r["camp_name"])
    except:
        pass
    return keywords

def _blocked_senders(user_id: str) -> list[str]:
    try:
        from agent.calendar_agent import _conn
        with _conn() as c:
            return [r["contact_name"] for r in c.execute(
                "SELECT contact_name FROM blocked_senders WHERE user_id=?",
                (user_id,)).fetchall()]
    except:
        return []

# ── Main scan function ─────────────────────────────────────────────────────────

def scan_gmail_account(user_id: str, email: str, days_back: int = 90) -> dict:
    """
    Full Gmail scan for one connected account. Returns {new, skipped, emails_scanned, error}.

    Pass 1: keyword subject match (fast, catches obvious phrasing)
    Pass 2: full inbox (catch-all for anything Pass 1 misses)
    Then: block filter → allowlist → Claude triage → body fetch → Claude extraction → save
    """
    from agent.calendar_agent import (
        _conn, _event_exists, _insert_event,
        _get_gcal_service, _gcal_event_exists, _write_to_gcal,
        _get_feedback_summary,
    )
    from agent.profile_agent import get_children

    creds = get_fresh_credentials(user_id, email)
    if not creds:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": "Not authenticated"}

    service = build("gmail", "v1", credentials=creds)
    after   = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")

    # Pass 1 — keyword subject match
    res1 = service.users().messages().list(
        userId="me", q=ACTIVITY_QUERY + " after:" + after, maxResults=150
    ).execute()
    # Pass 2 — full inbox
    res2 = service.users().messages().list(
        userId="me", q="after:" + after, maxResults=200
    ).execute()

    seen_ids  = set()
    all_msgs  = []
    for msg in res1.get("messages", []) + res2.get("messages", []):
        if msg["id"] not in seen_ids:
            seen_ids.add(msg["id"])
            all_msgs.append(msg)

    if not all_msgs:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}

    keyword_ids   = {m["id"] for m in res1.get("messages", [])}
    blocked       = _blocked_senders(user_id)
    extra_kws     = _extra_allowlist_keywords(user_id)

    # Fetch lightweight metadata for all candidates
    meta = []
    for msg in all_msgs[:200]:
        try:
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()
            headers = full["payload"].get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
            sender  = next((h["value"] for h in headers if h["name"] == "From"), "")
            date_h  = next((h["value"] for h in headers if h["name"] == "Date"), "")
            meta.append({"id": msg["id"], "subject": subject, "from": sender, "date": date_h})
        except:
            continue

    # Drop blocked senders
    if blocked:
        meta = [m for m in meta if not any(b.lower() in m["from"].lower() for b in blocked if b)]
        keyword_ids &= {m["id"] for m in meta}

    # Classify non-keyword emails: allowlisted vs needs Claude triage
    allowlisted_ids   = set()
    triage_candidates = []
    for m in meta:
        if m["id"] in keyword_ids:
            continue
        if _is_family_platform_sender(m["from"], extra_kws):
            allowlisted_ids.add(m["id"])
        else:
            triage_candidates.append(m)

    # Claude triage — cheap subject-only pass, processed in batches so nothing is dropped
    triage_ids = set()
    TRIAGE_BATCH = 80
    for batch_start in range(0, len(triage_candidates), TRIAGE_BATCH):
        batch = triage_candidates[batch_start:batch_start + TRIAGE_BATCH]
        lines = [f"{i}. Subject: {m['subject']} | From: {m['from']}"
                 for i, m in enumerate(batch)]
        triage_prompt = (
            "Below are email subjects and senders. Return the indices of emails that "
            "MIGHT be relevant to a family's logistics. Be generous — when in doubt, include it.\n"
            "Include anything about:\n"
            "- School events, forms, sign-ups, orientation, field trips, no-school days\n"
            "- Summer camps, day camps, sports camps, camp registration or confirmation\n"
            "- Kids classes or lessons (skating, swimming, dance, gymnastics, coding, art, music, etc.)\n"
            "- Club activities, sports leagues, teams\n"
            "- Sports practices, games, tournaments, tryouts\n"
            "- Doctor, dentist, or therapy appointments\n"
            "- Registration, enrollment, or payment confirmations for any kids activity\n"
            "- Welcome emails or orientation from any kids program\n"
            "- Bills or payments for the family\n"
            "Exclude only: investment/brokerage updates, marketing from stores, general news, "
            "job alerts, social media notifications.\n\n"
            + "\n".join(lines) +
            "\n\nReturn ONLY a JSON array of indices, e.g. [0,3,7]. If none, return []."
        )
        try:
            client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
            resp   = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=400,
                     messages=[{"role": "user", "content": triage_prompt}])
            raw  = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
            raw  = re.sub(r"\s*```$", "", raw)
            idxs = json.loads(raw)
            for i in idxs:
                if isinstance(i, int) and 0 <= i < len(batch):
                    triage_ids.add(batch[i]["id"])
        except Exception as e:
            print(f"[gmail triage] {e}")

    final_ids = keyword_ids | allowlisted_ids | triage_ids

    # Fetch full bodies for final set
    meta_by_id  = {m["id"]: m for m in meta}
    emails_data = []
    for mid in final_ids:
        m = meta_by_id.get(mid)
        if not m:
            continue
        try:
            full = service.users().messages().get(userId="me", id=mid, format="full").execute()
            body = _extract_body(full["payload"])
            if body:
                emails_data.append({
                    "subject":  m["subject"],
                    "from":     m["from"],
                    "body":     body[:3000],
                    "received": m["date"],
                })
        except:
            continue

    if not emails_data:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}

    print(f"[gmail] {len(emails_data)} emails → Claude | "
          f"keyword={len(keyword_ids)} allowlisted={len(allowlisted_ids)} triage={len(triage_ids)}")

    # Build extraction prompt
    children_str    = ", ".join(get_children(user_id)) or "the children"
    today_str       = date.today().strftime("%A, %B %d, %Y")
    today_iso       = date.today().isoformat()
    feedback_block  = ""
    try:
        fb = _get_feedback_summary(user_id)
        if fb:
            feedback_block = "\nUSER FEEDBACK HISTORY (avoid repeating past mistakes):\n" + fb + "\n"
    except:
        pass

    digest = "".join(
        f"--- {e['subject']} (from: {e['from']}, received: {e['received']}) ---\n{e['body']}\n"
        for e in emails_data
    )

    prompt = (
        f"Hearth assistant. Today: {today_str} ({today_iso}). Children: {children_str}.{feedback_block}\n"
        "Extract two kinds of items from these emails:\n"
        "1. Dated calendar events (must have a specific date)\n"
        "2. Action items — sign-ups, forms to complete, registration tasks (school_task)\n\n"
        "Return a JSON array. Each item is one of:\n"
        "{\"item_type\":\"event\",\"child_name\":\"...\",\"event_type\":\"...\","
        "\"event_date\":\"YYYY-MM-DD\",\"event_time\":null,\"notes\":\"...\"}\n"
        "{\"item_type\":\"school_task\",\"title\":\"short action title\","
        "\"due_date\":\"YYYY-MM-DD or null\",\"org_name\":\"sender org name\","
        "\"child_name\":\"name or null\",\"notes\":\"...\","
        "\"source_email\":\"exact subject line\","
        "\"action_url\":\"https://... or null\"}\n\n"
        f"Valid event_type: {','.join(EVENT_TYPES)}\n"
        "Use camp for summer/day/sports camps.\n"
        "Use class for recurring lessons (skating, swimming, dance, coding, art, music, etc.).\n"
        "Use registration_confirmed when an email confirms enrollment/registration/payment "
        "— use the activity start date as event_date.\n"
        "Use school_fundraiser for: teacher gifts, PTA donations, book fairs, spirit wear.\n"
        "Do NOT use bill as an event_type.\n"
        "Only create school_task for genuinely actionable items from schools, camps, or "
        "family platforms — not marketing or financial statements unrelated to kids.\n"
        "Always use the sender's actual organization name in org_name.\n"
        f"Emails:\n{digest[:30000]}\nONLY JSON array."
    )

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    resp   = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=4096,
             messages=[{"role": "user", "content": prompt}])
    raw    = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
    raw    = re.sub(r"\s*```$", "", raw)
    try:
        items = json.loads(raw)
    except:
        print(f"[gmail extract] JSON parse failed: {raw[:500]}")
        items = []

    print(f"[gmail extract] {len(emails_data)} emails → {len(items)} items")

    # Save extracted items
    new = skipped = 0
    gcal_service = None
    try:
        gcal_service = _get_gcal_service(user_id, email)
    except:
        pass

    for item in items:
        item_type = item.get("item_type", "event")

        if item_type == "school_task":
            title = (item.get("title") or "").strip()
            if not title:
                continue
            with _conn() as c:
                existing = c.execute(
                    "SELECT id FROM tasks WHERE user_id=? AND task_type='school_task' AND title=?",
                    (user_id, title)).fetchone()
                if existing:
                    skipped += 1
                    continue
                notes_parts = []
                if item.get("notes"):        notes_parts.append(item["notes"])
                if item.get("source_email"): notes_parts.append("From email: " + item["source_email"])
                c.execute(
                    "INSERT INTO tasks(user_id,task_type,title,status,due_date,"
                    "contact_name,child_name,notes,payment_url) VALUES(?,?,?,?,?,?,?,?,?)",
                    (user_id, "school_task", title, "pending",
                     item.get("due_date"), item.get("org_name"), item.get("child_name"),
                     " | ".join(notes_parts) or None, item.get("action_url")))
                c.commit()
            new += 1
            continue

        child = item.get("child_name") or "all"
        etype = item.get("event_type") or "other"
        edate = item.get("event_date", "")
        if not edate:
            continue
        if _event_exists(user_id, child, etype, edate):
            skipped += 1
            continue
        event_id = _insert_event(user_id, child, etype, edate, item.get("event_time"), item.get("notes"))
        # Sync to GCal
        try:
            if gcal_service:
                gcal_summary = item.get("notes") or etype.replace("_", " ").title()
                if child and child != "all":
                    gcal_summary = child + " - " + gcal_summary
                if not _gcal_event_exists(gcal_service, gcal_summary, edate):
                    gcal_id = _write_to_gcal(user_id, email, gcal_summary, edate,
                                             item.get("event_time"), item.get("notes"))
                    if gcal_id and gcal_id != "duplicate":
                        with _conn() as c:
                            c.execute("UPDATE events SET gcal_event_id=? WHERE id=?",
                                      (gcal_id, event_id))
                            c.commit()
        except Exception as e:
            print(f"[gcal scan write] {e}")
        new += 1

    return {"new": new, "skipped": skipped, "emails_scanned": len(emails_data), "error": None}


def auto_scan_and_save(user_id: str, children: list = None, days_back: int = 30) -> dict:
    """Scan all connected Gmail accounts for this user and save new events/tasks."""
    emails = list_connected_emails(user_id)
    if not emails:
        return {"new": 0, "skipped": 0, "emails_scanned": 0, "error": "No connected Gmail accounts"}

    total = {"new": 0, "skipped": 0, "emails_scanned": 0, "error": None}
    for email in emails:
        result = scan_gmail_account(user_id, email, days_back=days_back)
        total["new"]            += result.get("new", 0)
        total["skipped"]        += result.get("skipped", 0)
        total["emails_scanned"] += result.get("emails_scanned", 0)
        if result.get("error"):
            total["error"] = result["error"]
    return total
