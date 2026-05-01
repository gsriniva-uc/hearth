import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

"""
ui/app.py
Hearth — Family OS · Streamlit UI

Tabs:  📅 Calendar  |  📬 Gmail Scan  |  📄 Upload PDF
Settings in sidebar expander (not a tab).
"""

import streamlit as st
from datetime import date

import hearth_config as cfg
from agent.graph import run
from agent.calendar_agent import init_db, _query_upcoming, _delete_event, _insert_event
from agent.gmail_agent import (
    is_credentials_configured,
    is_authenticated,
    scan_gmail_for_school_events,
)


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Hearth 🏠",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_db()


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🏠 Hearth")
    st.caption(cfg.get_family_summary())
    st.divider()

    if cfg.is_configured():
        st.success("✅ Ready")
    else:
        st.error("⚠️ ANTHROPIC_API_KEY or CHILDREN not set")

    upcoming = _query_upcoming(days_ahead=7)
    st.metric("Events this week", len(upcoming))

    st.divider()

    # ── Gmail auth status ─────────────────────────────────────────────────────
    st.markdown("**📬 Gmail**")
    if not is_credentials_configured():
        st.warning("credentials file missing")
        st.caption("Add `data/google_credentials.json`")
    elif is_authenticated():
        st.success("Connected")
    else:
        st.info("Click 'Scan Gmail' to connect")

    st.divider()

    # ── Manual nudge trigger ──────────────────────────────────────────────────
    st.markdown("**🔔 Nudges**")
    if st.button("Run scan now"):
        from scheduler.nudge_scheduler import run_nudge_scan
        with st.spinner("Scanning…"):
            result = run_nudge_scan()
        st.success(f"Sent: {result['sent']} · Failed: {result['failed']}")

    st.divider()

    # ── Settings expander ─────────────────────────────────────────────────────
    with st.expander("⚙️ Settings"):
        rows = {
            "ANTHROPIC_API_KEY":      "✅ set" if cfg.ANTHROPIC_API_KEY else "❌ missing",
            "CHILDREN":               ", ".join(cfg.CHILDREN) or "❌ missing",
            "PARENT_EMAIL":           cfg.PARENT_EMAIL or "—",
            "POWER_AUTOMATE_WEBHOOK": "✅ set" if cfg.POWER_AUTOMATE_WEBHOOK else "⚠️ not set",
            "NUDGE_SCAN_HOUR":        str(cfg.NUDGE_SCAN_HOUR),
            "CLAUDE_MODEL":           cfg.CLAUDE_MODEL,
            "LANGSMITH_TRACING":      "enabled" if cfg.LANGSMITH_TRACING else "disabled",
        }
        for k, v in rows.items():
            c1, c2 = st.columns([4, 5])
            c1.caption(k)
            c2.write(v)


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_cal, tab_gmail, tab_pdf = st.tabs(["📅 Calendar", "📬 Gmail Scan", "📄 Upload PDF"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — CALENDAR
# ══════════════════════════════════════════════════════════════════════════════

with tab_cal:
    col_hd, col_days = st.columns([5, 2])
    with col_hd:
        st.subheader("Upcoming events")
    with col_days:
        days = st.slider("Days ahead", 7, 90, 14, key="cal_days",
                         label_visibility="collapsed")

    events = _query_upcoming(days_ahead=days)

    if not events:
        st.info(f"No events in the next {days} days. Scan Gmail or add one below.")
    else:
        from itertools import groupby
        for event_date, group in groupby(events, key=lambda e: e["event_date"]):
            day_label = date.fromisoformat(event_date).strftime("%A, %b %d")
            st.markdown(f"**{day_label}**")
            for ev in group:
                label     = ev["event_type"].replace("_", " ").title()
                time_str  = f" · {ev['event_time']}" if ev.get("event_time") else ""
                notes_str = f" — {ev['notes']}"      if ev.get("notes")      else ""
                nudges    = "".join([
                    " 7d✓"  if ev.get("nudge_sent_7d")  else "",
                    " 48h✓" if ev.get("nudge_sent_48h") else "",
                    " day✓" if ev.get("nudge_sent_day") else "",
                ])
                c1, c2 = st.columns([9, 1])
                with c1:
                    st.markdown(
                        f"&nbsp;&nbsp;&nbsp;**{ev['child_name']}** · {label}"
                        f"{time_str}{notes_str}"
                        + (f" `{nudges.strip()}`" if nudges.strip() else "")
                    )
                with c2:
                    if st.button("🗑", key=f"del_{ev['id']}"):
                        _delete_event(ev["id"])
                        st.rerun()
            st.markdown("")

    st.divider()
    st.subheader("Add or ask")
    st.caption('e.g. "Add dress-down day for Avery on May 9" · "What\'s happening this week?"')

    user_input = st.text_area("Command or question", height=68, key="nl_input",
                              placeholder='Try: "Avery has a recital June 3 at 6pm"')
    if st.button("Send ➤", key="nl_send", disabled=not (user_input or "").strip()):
        with st.spinner("Thinking…"):
            result = run(raw_text=user_input.strip())
        st.markdown(result.get("response", "Done."))
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — GMAIL SCAN
# ══════════════════════════════════════════════════════════════════════════════

with tab_gmail:
    st.subheader("Scan Gmail for school events")

    # ── Setup instructions ────────────────────────────────────────────────────
    if not is_credentials_configured():
        st.error("Gmail not configured yet. Follow these steps:")
        st.markdown("""
**One-time Google setup (5 minutes):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → New project → name it `Hearth`
2. **APIs & Services** → Enable APIs → search **Gmail API** → Enable
3. **APIs & Services** → OAuth consent screen → External → fill in app name → Save
4. **APIs & Services** → Credentials → Create Credentials → **OAuth 2.0 Client ID**
   - Application type: **Desktop app** → Create
5. Download the JSON → save it as **`data/google_credentials.json`** in your Hearth project
6. Come back here and click **Scan Gmail** — your browser will open for a one-time login

After that, Hearth remembers your token and never asks again.
        """)
        st.stop()

    # ── Scan UI ───────────────────────────────────────────────────────────────
    st.caption(
        "Hearth searches your inbox for school newsletters, dismissal notices, "
        "recital reminders, and more — then extracts events for you to review."
    )

    c1, c2 = st.columns([2, 5])
    with c1:
        days_back = st.selectbox("Scan last", [7, 14, 30], index=1,
                                  format_func=lambda d: f"{d} days")
    with c2:
        st.write("")
        st.write("")
        scan_btn = st.button("📬 Scan Gmail now", key="gmail_scan")

    if not is_authenticated():
        st.info("👆 First scan will open a browser window to connect your Google account. "
                "This only happens once.")

    if scan_btn:
        with st.spinner("Connecting to Gmail and reading your inbox… (20–30 seconds)"):
            result = scan_gmail_for_school_events(days_back=days_back)

        if result.get("error"):
            st.error(result["error"])
        else:
            st.session_state["gmail_events"] = result.get("events", [])
            st.success(result.get("response", "Scan complete."))

    # ── Review & confirm ──────────────────────────────────────────────────────
    if st.session_state.get("gmail_events"):
        extracted = st.session_state["gmail_events"]
        st.subheader(f"Review — {len(extracted)} event(s) found")
        st.caption("Uncheck any you don't want to save, then click Save.")

        keep_flags = []
        for i, ev in enumerate(extracted):
            label  = ev.get("event_type", "other").replace("_", " ").title()
            source = ev.get("source_email", "")
            display = (
                f"**{ev.get('event_date')}** · {ev.get('child_name', 'all')} · {label}"
                + (f" at {ev['event_time']}" if ev.get("event_time") else "")
                + (f" — {ev['notes']}"       if ev.get("notes")      else "")
                + (f" *(from: {source})*"    if source                else "")
            )
            keep = st.checkbox(display, value=True, key=f"gmail_keep_{i}")
            keep_flags.append(keep)

        if st.button("✅ Save selected events", key="gmail_save"):
            to_save = [ev for ev, keep in zip(extracted, keep_flags) if keep]
            saved   = 0
            for ev in to_save:
                try:
                    _insert_event(
                        child_name=ev.get("child_name", "all"),
                        event_type=ev.get("event_type", "other"),
                        event_date=ev["event_date"],
                        event_time=ev.get("event_time"),
                        notes=ev.get("notes"),
                    )
                    saved += 1
                except Exception:
                    pass
            st.success(f"✅ {saved} event(s) saved to calendar.")
            st.session_state.pop("gmail_events", None)
            st.rerun()

    elif not scan_btn:
        st.markdown("""
**What Hearth looks for:**

| | Event type | Nudges |
|---|---|---|
| 👕 | Dress-down / spirit days | 48h + day-of |
| 🏫 | Early dismissals | 48h + day-of |
| 🎭 | Recitals and performances | 7d (costume) + 48h + day-of |
| 🚌 | Field trips | 7d (permission slip) + 48h (packed lunch) + day-of |
| 📸 | Picture days and special days | 48h + day-of |
| 🏥 | Doctor appointments | 48h + day-of |
| 📅 | Weekly preview | Every Sunday morning |
        """)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — UPLOAD PDF (fallback)
# ══════════════════════════════════════════════════════════════════════════════

with tab_pdf:
    st.subheader("Upload a school newsletter PDF")
    st.caption("Use this if your school sends newsletters as PDF attachments.")

    uploaded = st.file_uploader("Drop the PDF here", type=["pdf"])

    if uploaded:
        if st.button("📋 Extract events", key="pdf_extract"):
            with st.spinner("Reading newsletter…"):
                result = run(pdf_bytes=uploaded.read())
            st.session_state["pdf_events"] = result.get("extracted_events", [])
            st.info(result.get("response", ""))

    if st.session_state.get("pdf_events"):
        extracted = st.session_state["pdf_events"]
        st.subheader(f"Review — {len(extracted)} event(s) found")

        keep_flags = []
        for i, ev in enumerate(extracted):
            label   = ev.get("event_type", "other").replace("_", " ").title()
            display = (
                f"**{ev.get('event_date')}** · {ev.get('child_name', 'all')} · {label}"
                + (f" at {ev['event_time']}" if ev.get("event_time") else "")
                + (f" — {ev['notes']}"       if ev.get("notes")      else "")
            )
            keep = st.checkbox(display, value=True, key=f"pdf_keep_{i}")
            keep_flags.append(keep)

        if st.button("✅ Save selected events", key="pdf_save"):
            to_save = [ev for ev, keep in zip(extracted, keep_flags) if keep]
            saved = 0
            for ev in to_save:
                try:
                    _insert_event(
                        child_name=ev.get("child_name", "all"),
                        event_type=ev.get("event_type", "other"),
                        event_date=ev["event_date"],
                        event_time=ev.get("event_time"),
                        notes=ev.get("notes"),
                    )
                    saved += 1
                except Exception:
                    pass
            st.success(f"✅ {saved} event(s) saved.")
            st.session_state.pop("pdf_events", None)
            st.rerun()
