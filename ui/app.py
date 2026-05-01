"""
ui/app.py

Hearth — Family OS · Streamlit UI

Tabs:
  1. Calendar   — view upcoming events, quick-add, natural language commands
  2. Upload     — drop a school newsletter PDF, review extracted events, confirm
  3. Settings   — family config status

Adapted from Alterus ui/app.py — same Streamlit patterns, new domain.
"""

import sqlite3
from datetime import date, timedelta
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import hearth_config as cfg

import streamlit as st

import hearth_config as cfg
from agent.graph import run
from agent.calendar_agent import init_db, _query_upcoming, _delete_event


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

    if not cfg.is_configured():
        st.error("⚠️ ANTHROPIC_API_KEY or CHILDREN not set in .env")
    else:
        st.success("Ready")

    st.caption(f"Children: {', '.join(cfg.CHILDREN) or '—'}")
    st.caption(f"Nudges: {'✅ configured' if cfg.POWER_AUTOMATE_WEBHOOK else '⚠️ webhook not set'}")


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_cal, tab_pdf, tab_settings = st.tabs(["📅 Calendar", "📄 Upload Newsletter", "⚙️ Settings"])


# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — CALENDAR
# ════════════════════════════════════════════════════════════════════════════════

with tab_cal:
    st.subheader("Upcoming events")

    # Event list
    days = st.slider("Show next N days", 7, 90, 14, key="cal_days")
    events = _query_upcoming(days_ahead=days)

    if not events:
        st.info(f"No events in the next {days} days. Add one below or upload a newsletter.")
    else:
        for ev in events:
            label = ev["event_type"].replace("_", " ").title()
            time_str = f" · {ev['event_time']}" if ev.get("event_time") else ""
            notes_str = f" — {ev['notes']}" if ev.get("notes") else ""
            col1, col2 = st.columns([8, 1])
            with col1:
                st.markdown(
                    f"**{ev['event_date']}**{time_str} · "
                    f"**{ev['child_name']}** · {label}{notes_str}"
                )
            with col2:
                if st.button("🗑", key=f"del_{ev['id']}", help="Delete event"):
                    _delete_event(ev["id"])
                    st.rerun()

    st.divider()

    # Natural language input
    st.subheader("Add or ask")
    st.caption('Try: "Add dress-down day for Avery on May 9" or "What\'s happening this week?"')

    user_input = st.text_area("Command or question", height=80, key="nl_input")
    if st.button("Send", key="nl_send", disabled=not user_input.strip()):
        with st.spinner("Thinking…"):
            result = run(raw_text=user_input.strip())
        st.markdown(result.get("response", "Done."))
        st.rerun()


# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — PDF UPLOAD
# ════════════════════════════════════════════════════════════════════════════════

with tab_pdf:
    st.subheader("Upload a school newsletter")
    st.caption("Hearth will extract all dates and events. You review before anything is saved.")

    uploaded = st.file_uploader("Drop the PDF here", type=["pdf"])

    if uploaded:
        if st.button("📋 Extract events", key="extract_btn"):
            with st.spinner("Reading newsletter…"):
                result = run(pdf_bytes=uploaded.read())

            extracted = result.get("extracted_events", [])
            st.session_state["extracted"] = extracted
            st.info(result.get("response", ""))

    # Show extracted events for review
    if st.session_state.get("extracted"):
        extracted = st.session_state["extracted"]
        st.subheader(f"Review — {len(extracted)} event(s) found")
        st.caption("Uncheck any you don't want to save.")

        keep_flags = []
        for i, ev in enumerate(extracted):
            label = ev.get("event_type", "other").replace("_", " ").title()
            default_label = (
                f"**{ev.get('event_date')}** · {ev.get('child_name', 'all')} · "
                f"{label}"
                + (f" at {ev['event_time']}" if ev.get("event_time") else "")
                + (f" — {ev['notes']}" if ev.get("notes") else "")
            )
            keep = st.checkbox(default_label, value=True, key=f"keep_{i}")
            keep_flags.append(keep)

        if st.button("✅ Save selected events", key="save_extracted"):
            to_save = [ev for ev, keep in zip(extracted, keep_flags) if keep]
            if to_save:
                with st.spinner("Saving…"):
                    result = run(raw_text="add confirmed events")
                    # Directly re-run with confirmed list
                    from agent.calendar_agent import _insert_event
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
                st.success(f"✅ {saved} event(s) saved to calendar.")
                st.session_state.pop("extracted", None)
                st.rerun()
            else:
                st.warning("Nothing selected to save.")


# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — SETTINGS
# ════════════════════════════════════════════════════════════════════════════════

with tab_settings:
    st.subheader("Configuration")

    st.markdown("**Family**")
    st.code(cfg.get_family_summary())

    st.markdown("**Environment**")
    rows = {
        "ANTHROPIC_API_KEY":     "✅ set" if cfg.ANTHROPIC_API_KEY else "❌ missing",
        "CHILDREN":              ", ".join(cfg.CHILDREN) or "❌ missing",
        "PARENT_EMAIL":          cfg.PARENT_EMAIL or "—",
        "POWER_AUTOMATE_WEBHOOK":"✅ set" if cfg.POWER_AUTOMATE_WEBHOOK else "⚠️ not set (nudges disabled)",
        "NUDGE_SCAN_HOUR":       str(cfg.NUDGE_SCAN_HOUR),
        "CLAUDE_MODEL":          cfg.CLAUDE_MODEL,
        "LANGSMITH_TRACING":     "enabled" if cfg.LANGSMITH_TRACING else "disabled",
    }
    for k, v in rows.items():
        col1, col2 = st.columns([3, 5])
        col1.caption(k)
        col2.write(v)

    st.divider()
    st.markdown("**Manual nudge trigger**")
    st.caption("Fires today's pending nudges immediately — useful for testing your webhook.")
    if st.button("🔔 Run nudge scan now"):
        from scheduler.nudge_scheduler import run_nudge_scan
        with st.spinner("Scanning…"):
            result = run_nudge_scan()
        st.success(f"Sent: {result['sent']} · Failed: {result['failed']}")
