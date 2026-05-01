"""agent/briefing_agent.py — Multi-user daily briefing"""
from datetime import date, timedelta
import anthropic
import hearth_config as cfg
from agent.state import HearthState
from agent.calendar_agent import init_db, _query_today, _query_upcoming

_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

def _build_briefing(user_id: str) -> str:
    today     = date.today()
    today_str = today.strftime("%A, %B %d")
    today_evs = _query_today(user_id)
    week_evs  = [e for e in _query_upcoming(user_id, days_ahead=7)
                 if e["event_date"] != today.isoformat()]
    upcoming  = _query_upcoming(user_id, days_ahead=7)

    def fmt(ev):
        label    = ev["event_type"].replace("_"," ").title()
        time_str = f" at {ev['event_time']}" if ev.get("event_time") else ""
        notes    = f" — {ev['notes']}" if ev.get("notes") else ""
        return f"{ev['child_name']} — {label}{time_str}{notes}"

    sections = [f"🌅 **Good morning! Family briefing for {today_str}**\n"]

    if today_evs:
        sections.append("**TODAY**")
        for ev in today_evs: sections.append(f"• {fmt(ev)}")
    else:
        sections.append("**TODAY** — Nothing scheduled ✓")

    if week_evs:
        sections.append("\n**THIS WEEK**")
        for ev in week_evs:
            d = date.fromisoformat(ev["event_date"]).strftime("%A %b %d")
            sections.append(f"• {d}: {fmt(ev)}")

    reminders = []
    for ev in upcoming:
        edate = date.fromisoformat(ev["event_date"])
        days_away = (edate - today).days
        if days_away == 0: continue
        if ev["event_type"] == "recital" and days_away <= 7:
            reminders.append(f"• {ev['child_name']}'s recital in {days_away}d — confirm costume & arrival")
        elif ev["event_type"] == "field_trip" and days_away <= 7:
            reminders.append(f"• {ev['child_name']}'s field trip in {days_away}d — check permission slip")
        elif ev["event_type"] == "dress_down_day" and days_away == 1:
            reminders.append(f"• {ev['child_name']}'s dress-down day tomorrow — no uniform")
        elif ev["event_type"] == "early_dismissal" and days_away == 1:
            reminders.append(f"• Early dismissal tomorrow for {ev['child_name']} — confirm pick-up")

    if reminders:
        sections.append("\n**REMINDERS**")
        sections.extend(reminders)

    return "\n".join(sections)

def briefing_agent(state: HearthState) -> HearthState:
    init_db()
    user_id  = state.get("user_id") or "default"
    briefing = _build_briefing(user_id)
    return {**state, "briefing_text":briefing, "response":briefing,
            "notify":True, "notify_message":briefing}
