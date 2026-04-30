"""
channels/dispatcher.py

Fires nudge notifications via Power Automate HTTP trigger → Outlook.
Adapted from Alterus channels/ webhook dispatcher pattern.
"""

import requests
import hearth_config as cfg


def _fmt_recital(ev: dict, nudge_type: str) -> tuple[str, str]:
    name = ev["child_name"]
    ddate = ev["event_date"]
    time = ev.get("event_time") or "TBD"
    if nudge_type == "7d":
        return (f"🎭 {name}'s recital is in 7 days",
                f"{name}'s recital is on {ddate} at {time}.\nCheck the costume and confirm arrival time.")
    if nudge_type == "48h":
        return (f"🎭 {name}'s recital is in 2 days",
                f"{name}'s recital is on {ddate} at {time}.\nConfirm outfit, shoes, and arrival plan.")
    return (f"🎭 {name}'s recital is TODAY",
            f"Recital day! {name}'s performance starts at {time}. Good luck! 🌟")


def _fmt_dress_down(ev: dict, nudge_type: str) -> tuple[str, str]:
    name = ev["child_name"]
    ddate = ev["event_date"]
    if nudge_type == "48h":
        return (f"👕 Dress-down day for {name} tomorrow",
                f"Reminder: dress-down day on {ddate}. No uniform needed!")
    return (f"👕 Dress-down day for {name} TODAY",
            f"No uniform for {name} today ({ddate}). Casual clothes only!")


def _fmt_early_dismissal(ev: dict, nudge_type: str) -> tuple[str, str]:
    name = ev["child_name"]
    ddate = ev["event_date"]
    time = ev.get("event_time") or "early"
    label = "tomorrow" if nudge_type == "48h" else "TODAY"
    return (f"🏫 Early dismissal for {name} {label}",
            f"{name} has early dismissal {label} ({ddate}) at {time}.\nConfirm pick-up arrangements.")


def _fmt_field_trip(ev: dict, nudge_type: str) -> tuple[str, str]:
    name = ev["child_name"]
    ddate = ev["event_date"]
    notes = ev.get("notes") or ""
    if nudge_type == "7d":
        return (f"🚌 {name}'s field trip is next week",
                f"Field trip on {ddate}. {notes}\nCheck that the permission slip is signed.")
    if nudge_type == "48h":
        return (f"🚌 {name}'s field trip is in 2 days",
                f"Field trip on {ddate} — pack a lunch, sign the permission slip! {notes}")
    return (f"🚌 {name} has a field trip TODAY",
            f"Field trip day! Packed lunch + permission slip ready? {notes}")


def _fmt_generic(ev: dict, nudge_type: str) -> tuple[str, str]:
    name = ev["child_name"]
    label = ev["event_type"].replace("_", " ").title()
    ddate = ev["event_date"]
    time_str = f" at {ev['event_time']}" if ev.get("event_time") else ""
    when = {"7d": "in 7 days", "48h": "tomorrow", "day_of": "TODAY"}[nudge_type]
    notes = ev.get("notes") or ""
    return (f"📅 {label} for {name} {when}",
            f"{name} has a {label} {when} ({ddate}{time_str}).\n{notes}".strip())


_FORMATTERS = {
    "recital":         _fmt_recital,
    "dress_down_day":  _fmt_dress_down,
    "early_dismissal": _fmt_early_dismissal,
    "field_trip":      _fmt_field_trip,
}


def _build_message(nudge: dict) -> tuple[str, str]:
    fmt = _FORMATTERS.get(nudge.get("event_type", "other"), _fmt_generic)
    return fmt(nudge, nudge.get("nudge_type", "day_of"))


def fire_nudge(nudge: dict) -> bool:
    if not cfg.POWER_AUTOMATE_WEBHOOK:
        print(f"[dispatcher] POWER_AUTOMATE_WEBHOOK not set — skipping nudge for event {nudge.get('event_id')}")
        return False

    subject, body = _build_message(nudge)
    payload = {
        "to": cfg.PARENT_EMAIL,
        "subject": subject,
        "body": body,
        "event_id": nudge.get("event_id"),
        "nudge_type": nudge.get("nudge_type"),
    }

    try:
        resp = requests.post(cfg.POWER_AUTOMATE_WEBHOOK, json=payload, timeout=15)
        if resp.status_code < 300:
            print(f"[dispatcher] ✅  {subject}")
            return True
        print(f"[dispatcher] ❌  Webhook {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[dispatcher] ❌  Request failed: {e}")
        return False


def fire_all(nudges: list[dict]) -> dict:
    sent, failed = 0, 0
    for nudge in nudges:
        (sent := sent + 1) if fire_nudge(nudge) else (failed := failed + 1)
    return {"sent": sent, "failed": failed}
