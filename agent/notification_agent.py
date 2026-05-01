"""
agent/notification_agent.py

Routes notifications to configured channels:
  email     — Gmail send or Outlook send
  sms       — Twilio SMS
  whatsapp  — Twilio WhatsApp

Channels controlled by NOTIFY_CHANNELS env var (comma-separated).
"""
import hearth_config as cfg
from agent.state import HearthState


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_via_gmail(subject: str, body: str) -> bool:
    try:
        import base64, os
        from email.mime.text import MIMEText
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not os.path.exists(cfg.GOOGLE_TOKEN): return False
        scopes = ["https://www.googleapis.com/auth/gmail.send"]
        creds = Credentials.from_authorized_user_file(cfg.GOOGLE_TOKEN, scopes)
        service = build("gmail", "v1", credentials=creds)

        msg = MIMEText(body)
        msg["to"]      = cfg.PARENT_EMAIL
        msg["from"]    = cfg.PARENT_EMAIL
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        print(f"[notify] ✅ Gmail sent: {subject}")
        return True
    except Exception as e:
        print(f"[notify] ❌ Gmail failed: {e}")
        return False


def _send_via_outlook(subject: str, body: str) -> bool:
    try:
        import json, os, requests
        if not os.path.exists(cfg.OUTLOOK_TOKEN_FILE): return False
        with open(cfg.OUTLOOK_TOKEN_FILE) as f:
            token_data = json.load(f)
        access_token = token_data.get("access_token","")
        if not access_token: return False

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType":"Text","content": body},
                "toRecipients": [{"emailAddress":{"address": cfg.PARENT_EMAIL}}],
            },
            "saveToSentItems": "true"
        }
        resp = requests.post("https://graph.microsoft.com/v1.0/me/sendMail",
                             json=payload,
                             headers={"Authorization":f"Bearer {access_token}",
                                      "Content-Type":"application/json"}, timeout=15)
        ok = resp.status_code == 202
        print(f"[notify] {'✅' if ok else '❌'} Outlook email: {subject}")
        return ok
    except Exception as e:
        print(f"[notify] ❌ Outlook failed: {e}")
        return False


def _send_email(subject: str, body: str) -> bool:
    """Try Gmail first, fall back to Outlook."""
    import os
    if os.path.exists(cfg.GOOGLE_TOKEN):
        return _send_via_gmail(subject, body)
    if os.path.exists(cfg.OUTLOOK_TOKEN_FILE):
        return _send_via_outlook(subject, body)
    print("[notify] ⚠️ No email connector configured")
    return False


# ── SMS ───────────────────────────────────────────────────────────────────────

def _send_sms(body: str) -> bool:
    if not cfg.TWILIO_ACCOUNT_SID or not cfg.PARENT_PHONE:
        print("[notify] ⚠️ Twilio SMS not configured")
        return False
    try:
        from twilio.rest import Client
        client = Client(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
        client.messages.create(body=body, from_=cfg.TWILIO_FROM_PHONE, to=cfg.PARENT_PHONE)
        print(f"[notify] ✅ SMS sent to {cfg.PARENT_PHONE}")
        return True
    except Exception as e:
        print(f"[notify] ❌ SMS failed: {e}")
        return False


# ── WhatsApp ──────────────────────────────────────────────────────────────────

def _send_whatsapp(body: str) -> bool:
    if not cfg.TWILIO_ACCOUNT_SID or not cfg.PARENT_PHONE:
        print("[notify] ⚠️ Twilio WhatsApp not configured")
        return False
    try:
        from twilio.rest import Client
        client = Client(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
        to = f"whatsapp:{cfg.PARENT_PHONE}"
        client.messages.create(body=body, from_=cfg.TWILIO_WHATSAPP_FROM, to=to)
        print(f"[notify] ✅ WhatsApp sent to {cfg.PARENT_PHONE}")
        return True
    except Exception as e:
        print(f"[notify] ❌ WhatsApp failed: {e}")
        return False


# ── Router ────────────────────────────────────────────────────────────────────

def notification_agent(state: HearthState) -> HearthState:
    message = state.get("notify_message") or state.get("response","")
    if not message:
        return {**state, "notify": False}

    # Build subject from first line
    first_line = message.split("\n")[0].replace("*","").strip()
    subject    = first_line[:80] if first_line else "Hearth reminder"
    results    = {}

    for channel in cfg.NOTIFY_CHANNELS:
        ch = channel.strip().lower()
        if ch == "email":
            results["email"]    = _send_email(subject, message)
        elif ch == "sms":
            # Strip markdown for SMS
            plain = message.replace("**","").replace("*","").replace("#","")
            results["sms"]      = _send_sms(plain[:1600])
        elif ch == "whatsapp":
            plain = message.replace("**","").replace("*","").replace("#","")
            results["whatsapp"] = _send_whatsapp(plain[:1600])

    sent = [ch for ch, ok in results.items() if ok]
    failed = [ch for ch, ok in results.items() if not ok]

    summary = ""
    if sent:
        summary = f"📨 Sent via: {', '.join(sent)}"
    if failed:
        summary += f" | ⚠️ Failed: {', '.join(failed)}"

    return {**state, "notify": False,
            "response": state.get("response","") + (f"\n\n{summary}" if summary else "")}
