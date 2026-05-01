import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

"""
ui/app.py
Hearth — Family OS · Streamlit UI

Tabs:
  1. Calendar   — view upcoming events, NL add/query
  2. Gmail Scan — scan inbox for school emails, review, confirm
  3. Upload PDF — manual newsletter upload fallback

Settings live in the sidebar expander — not a tab.
"""

import streamlit as st
from datetime import date

import hearth_config as cfg
from agent.graph import run
from agent.calendar_agent import init_db, _query_upcoming, _delete_event, _insert_event


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

    status_ok = cfg.is_configured()
    if status_ok:
        st.success("✅ Ready")
    else:
        st.error("⚠️ ANTHROPIC_API_KEY or CHILDREN not set")

    # ── Quick stats ───────────────────────────────────────────────────────────
    upcoming = _query_upcoming(days_ahead=7)
    st.metric("Events this week", len(upcoming))

    st.divider()

    # ── Manual nudge trigger ──────────────────────────────────────────────────
    st.markdown("**🔔 Nudge scan**")
    if st.button("Run now", help="Fire today's pending nudges"):
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

    # ── Upcoming events ───────────────────────────────────────────────────────
    col_hd, col_days = st.columns([5, 2])
    with col_hd:
        st.subheader("Upcoming events")
    with col_days:
        days = st.slider("Days ahead", 7, 90, 14, key="cal_days", label_visibility="collapsed")

    events = _query_upcoming(days_ahead=days)

    if not events:
        st.info(f"No events in the next {days} days. Add one below or scan Gmail.")
    else:
        # Group by date
        from itertools import groupby
        for event_date, group in groupby(events, key=lambda e: e["event_date"]):
            day_label = date.fromisoformat(event_date).strftime("%A, %b %d")
            st.markdown(f"**{day_label}**")
            for ev in group:
                label    = ev["event_type"].replace("_", " ").title()
                time_str = f" · {ev['event_time']}" if ev.get("event_time") else ""
                notes_str = f" — {ev['notes']}" if ev.get("notes") else ""
                nudge_str = ""
                if ev.get("nudge_sent_7d"):  nudge_str += " 7d✓"
                if ev.get("nudge_sent_48h"): nudge_str += " 48h✓"
                if ev.get("nudge_sent_day"): nudge_str += " day✓"

                c1, c2 = st.columns([9, 1])
                with c1:
                    st.markdown(
                        f"&nbsp;&nbsp;&nbsp;**{ev['child_name']}** · {label}{time_str}{notes_str}"
                        + (f" `{nudge_str.strip()}`" if nudge_str else "")
                    )
                with c2:
                    if st.button("🗑", key=f"del_{ev['id']}"):
                        _delete_event(ev["id"])
                        st.rerun()
            st.markdown("")

    st.divider()

    # ── NL input ──────────────────────────────────────────────────────────────
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
    st.caption(
        "Hearth searches your inbox for school newsletters, dismissal notices, "
        "recital reminders, and more — then extracts events for you to review before saving."
    )

    col1, col2 = st.columns([2, 5])
    with col1:
        days_back = st.selectbox("Scan last", [7, 14, 30], index=1, key="gmail_days",
                                  format_func=lambda d: f"{d} days")
    with col2:
        st.write("")
        st.write("")
        scan_btn = st.button("📬 Scan Gmail now", key="gmail_scan")

    if scan_btn:
        with st.spinner("Reading your inbox… this may take 20–30 seconds"):
            from agent.gmail_agent import scan_gmail_for_school_events
            result = scan_gmail_for_school_events(days_back=days_back)

        st.session_state["gmail_events"] = result.get("events", [])
        st.session_state["gmail_summary"] = result.get("raw_summary", "")

        if result.get("raw_summary"):
            st.info(result["raw_summary"])

    # ── Review extracted events ───────────────────────────────────────────────
    if st.session_state.get("gmail_events"):
        extracted = st.session_state["gmail_events"]
        st.subheader(f"Review — {len(extracted)} event(s) found")
        st.caption("Uncheck any you don't want to save.")

        keep_flags = []
        for i, ev in enumerate(extracted):
            label = ev.get("event_type", "other").replace("_", " ").title()
            source = ev.get("source_email", "")
            display = (
                f"**{ev.get('event_date')}** · {ev.get('child_name', 'all')} · {label}"
                + (f" at {ev['event_time']}" if ev.get("event_time") else "")
                + (f" — {ev['notes']}" if ev.get("notes") else "")
                + (f" *(from: {source})*" if source else "")
            )
            keep = st.checkbox(display, value=True, key=f"gmail_keep_{i}")
            keep_flags.append(keep)

        if st.button("✅ Save selected events", key="gmail_save"):
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
            st.success(f"✅ {saved} event(s) saved to calendar.")
            st.session_state.pop("gmail_events", None)
            st.rerun()

    elif not scan_btn:
        st.markdown("""
**What Hearth looks for:**
- 👕 Dress-down / spirit days
- 🏫 Early dismissals
- 🎭 Recitals and performances
- 🚌 Field trips
- 📸 Picture days and special days
- 🏥 Doctor appointment reminders
- 📰 Any school newsletter with upcoming dates

**Nudges you'll get once events are saved:**
- Sunday weekly preview for the week ahead
- 7-day heads-up for recitals and field trips
- 48-hour reminder for any event
- Morning-of reminder for dismissals and performances
        """)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — UPLOAD PDF (fallback)
# ══════════════════════════════════════════════════════════════════════════════

with tab_pdf:
    st.subheader("Upload a school newsletter PDF")
    st.caption("Use this if your school sends newsletters as PDF attachments rather than email body text.")

    uploaded = st.file_uploader("Drop the PDF here", type=["pdf"])

    if uploaded:
        if st.button("📋 Extract events", key="pdf_extract"):
            with st.spinner("Reading newsletter…"):
                result = run(pdf_bytes=uploaded.read())

            extracted = result.get("extracted_events", [])
            st.session_state["pdf_events"] = extracted
            st.info(result.get("response", ""))

    if st.session_state.get("pdf_events"):
        extracted = st.session_state["pdf_events"]
        st.subheader(f"Review — {len(extracted)} event(s) found")

        keep_flags = []
        for i, ev in enumerate(extracted):
            label = ev.get("event_type", "other").replace("_", " ").title()
            display = (
                f"**{ev.get('event_date')}** · {ev.get('child_name', 'all')} · {label}"
                + (f" at {ev['event_time']}" if ev.get("event_time") else "")
                + (f" — {ev['notes']}" if ev.get("notes") else "")
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
