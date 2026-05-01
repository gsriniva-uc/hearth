import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from datetime import date

st.set_page_config(
    page_title="Hearth 🏠",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

import hearth_config as cfg
from agent.graph import run
from agent.calendar_agent import init_db, _query_upcoming, _delete_event, _insert_event
from agent.gmail_agent import (
    is_credentials_configured,
    is_authenticated,
    auto_scan_and_save,
    scan_gmail_for_school_events,
)

init_db()


# ── Auto Gmail scan on startup ────────────────────────────────────────────────
# Runs once per session — silently adds new events, skips duplicates

if "startup_scan_done" not in st.session_state:
    st.session_state["startup_scan_done"] = True
    if is_credentials_configured() and is_authenticated():
        with st.spinner("📬 Checking Gmail for new school events…"):
            result = auto_scan_and_save(days_back=14)
        if result.get("new", 0) > 0:
            st.toast(f"📅 {result['new']} new school event(s) added from Gmail", icon="📬")
        elif result.get("error"):
            st.toast(f"Gmail scan: {result['error']}", icon="⚠️")


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

    # ── Gmail status ──────────────────────────────────────────────────────────
    st.markdown("**📬 Gmail**")
    if not is_credentials_configured():
        st.warning("Not configured")
        st.caption("See Gmail Scan tab for setup")
    elif is_authenticated():
        st.success("Connected · auto-scanning")
        if st.button("🔄 Refresh now", key="sidebar_refresh"):
            with st.spinner("Scanning…"):
                result = auto_scan_and_save(days_back=14)
            if result.get("error"):
                st.error(result["error"])
            else:
                st.success(f"+{result['new']} new · {result['skipped']} already saved")
    else:
        st.info("Click 'Scan Gmail' tab to connect")

    st.divider()

    # ── Nudge trigger ─────────────────────────────────────────────────────────
    st.markdown("**🔔 Nudges**")
    if st.button("Run scan now"):
        from scheduler.nudge_scheduler import run_nudge_scan
        with st.spinner("Scanning…"):
            result = run_nudge_scan()
        st.success(f"Sent: {result['sent']} · Failed: {result['failed']}")

    st.divider()

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

tab_cal, tab_gmail, tab_pdf = st.tabs(["📅 Calendar", "📬 Gmail Setup", "📄 Upload PDF"])


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
        st.info(f"No events in the next {days} days. Gmail auto-scans on startup.")
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
# TAB 2 — GMAIL SETUP / MANUAL SCAN
# ══════════════════════════════════════════════════════════════════════════════

with tab_gmail:
    if not is_credentials_configured():
        st.subheader("Connect Gmail")
        st.error("Google credentials not found. Follow these steps:")
        st.markdown("""
**One-time setup (5 minutes):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → select your project
2. **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth 2.0 Client ID**
   - Application type: **Desktop app** → Name: `Hearth` → Create
3. Download the JSON → save as **`data/google_credentials.json`**
4. **APIs & Services** → **OAuth consent screen** → **Test users** → add your Gmail address
5. Restart the app — Gmail will auto-scan on every startup
        """)
    else:
        st.subheader("Gmail — auto-scanning")

        if is_authenticated():
            st.success("✅ Gmail connected. Hearth scans automatically every time you open the app.")
            st.caption("New events are added silently. Duplicates are skipped automatically.")

            st.divider()
            st.markdown("**Manual scan**")
            st.caption("Force a fresh scan right now — useful after receiving a new newsletter.")

            c1, c2 = st.columns([2, 5])
            with c1:
                days_back = st.selectbox("Scan last", [7, 14, 30], index=1,
                                          format_func=lambda d: f"{d} days")
            with c2:
                st.write("")
                st.write("")
                if st.button("🔄 Scan now", key="gmail_manual_scan"):
                    with st.spinner("Scanning Gmail…"):
                        result = auto_scan_and_save(days_back=days_back)
                    if result.get("error"):
                        st.error(result["error"])
                    else:
                        st.success(
                            f"Scanned {result['emails_scanned']} email(s) · "
                            f"**{result['new']} new event(s) added** · "
                            f"{result['skipped']} already in calendar"
                        )
                        if result["new"] > 0:
                            st.rerun()
        else:
            st.info("Credentials found. Click below to connect your Google account.")
            st.caption("A browser window will open once for sign-in. After that, it's automatic.")
            if st.button("🔗 Connect Gmail now"):
                with st.spinner("Opening browser for Google sign-in…"):
                    result = auto_scan_and_save(days_back=14)
                if result.get("error"):
                    st.error(result["error"])
                else:
                    st.success(f"Connected! Added {result['new']} event(s).")
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — UPLOAD PDF
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
            st.success(f"✅ {saved} event(s) saved.")
            st.session_state.pop("pdf_events", None)
            st.rerun()
