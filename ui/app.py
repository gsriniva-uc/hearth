import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from datetime import date, timedelta
from itertools import groupby

st.set_page_config(page_title="Hearth 🏠", page_icon="🏠", layout="wide",
                   initial_sidebar_state="expanded")

import hearth_config as cfg
from agent.auth import (sign_in_with_google, sign_out, list_sessions,
                        is_user_authenticated, is_credentials_configured)
from agent.calendar_agent import init_db, _query_today, _query_upcoming, _delete_event, _insert_event
from agent.briefing_agent import _build_briefing
from agent.profile_agent import init_profiles, get_all_profiles, get_children, upsert_profile
from agent.gmail_agent import auto_scan_and_save, is_authenticated as gmail_authed
from agent.graph import run

init_db()
init_profiles()

# ══════════════════════════════════════════════════════════════════════════════
# AUTH GATE — show login screen if not signed in
# ══════════════════════════════════════════════════════════════════════════════

def show_login():
    st.markdown("<br><br>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1,2,1])
    with col2:
        st.title("🏠 Hearth")
        st.subheader("Family OS")
        st.caption("Sign in with your Google account to get started.")
        st.divider()

        # Show existing sessions (returning users)
        sessions = list_sessions()
        if sessions:
            st.markdown("**Continue as:**")
            for s in sessions:
                col_a, col_b = st.columns([4,1])
                with col_a:
                    if st.button(f"👤 {s['name']} ({s['email']})",
                                 key=f"sess_{s['user_id']}", use_container_width=True):
                        # Re-authenticate if needed (token may be stale)
                        if is_user_authenticated(s["user_id"]):
                            st.session_state["user"] = s
                            st.rerun()
                        else:
                            st.warning("Session expired — please sign in again.")
                with col_b:
                    if st.button("✕", key=f"del_{s['user_id']}",
                                 help="Remove this account"):
                        sign_out(s["user_id"]); st.rerun()
            st.divider()

        if not is_credentials_configured():
            st.error("Google credentials not configured.")
            st.markdown("""
**One-time setup:**
1. [console.cloud.google.com](https://console.cloud.google.com) → Credentials
2. Download the Hearth OAuth 2.0 Client ID JSON
3. Save as `data/google_credentials.json`
4. Restart the app
            """)
        else:
            if st.button("🔐 Sign in with Google", use_container_width=True, type="primary"):
                with st.spinner("Opening Google sign-in…"):
                    try:
                        user = sign_in_with_google()
                        st.session_state["user"] = user
                        st.rerun()
                    except FileNotFoundError as e:
                        st.error(str(e))
                    except Exception as e:
                        st.error(f"Sign-in failed: {e}")

# ── Check auth ────────────────────────────────────────────────────────────────
if "user" not in st.session_state:
    show_login()
    st.stop()

user    = st.session_state["user"]
user_id = user["user_id"]
children = get_children(user_id)

# ── Auto Gmail scan on startup (per user) ─────────────────────────────────────
scan_key = f"startup_scan_{user_id}"
if scan_key not in st.session_state:
    st.session_state[scan_key] = True
    if gmail_authed(user_id):
        with st.spinner("📬 Checking Gmail for new school events…"):
            result = auto_scan_and_save(user_id, children, days_back=14)
        if result.get("new",0) > 0:
            st.toast(f"📅 {result['new']} new event(s) added from Gmail", icon="📬")

# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.title("🏠 Hearth")

    # User identity
    pic = user.get("picture","")
    if pic:
        st.markdown(f'<img src="{pic}" width="40" style="border-radius:50%">',
                    unsafe_allow_html=True)
    st.caption(f"**{user['name']}**")
    st.caption(user["email"])
    if st.button("Sign out", key="signout"):
        del st.session_state["user"]
        st.rerun()

    st.divider()

    # Child selector
    profiles = get_all_profiles(user_id)
    child_names = [p["name"] for p in profiles] or children
    selected_child = st.selectbox("Viewing", ["All children"] + child_names)

    st.metric("Events this week", len(_query_upcoming(user_id, days_ahead=7)))
    st.divider()

    # Quick actions
    st.markdown("**⚡ Quick actions**")
    if st.button("📋 Today's briefing"):
        briefing = _build_briefing(user_id)
        st.session_state["briefing"] = briefing

    if st.button("🔄 Refresh Gmail"):
        with st.spinner("Scanning…"):
            r = auto_scan_and_save(user_id, children, days_back=14)
        if r.get("error"): st.error(r["error"])
        else:
            st.success(f"+{r['new']} new · {r['skipped']} already saved")
            if r["new"] > 0: st.rerun()

    if st.button("🔔 Run nudges now"):
        from scheduler.nudge_scheduler import run_nudge_scan
        r = run_nudge_scan(user_id)
        st.success(f"Sent: {r['sent']} · Failed: {r['failed']}")

    st.divider()
    with st.expander("⚙️ Settings"):
        rows = {
            "Email":          user["email"],
            "User ID":        user_id,
            "NOTIFY_CHANNELS":  ", ".join(cfg.NOTIFY_CHANNELS),
            "TWILIO":           "✅" if cfg.TWILIO_ACCOUNT_SID else "⚠️ not set",
            "BRIEFING TIME":    f"{cfg.BRIEFING_HOUR:02d}:{cfg.BRIEFING_MINUTE:02d}",
            "CLAUDE_MODEL":     cfg.CLAUDE_MODEL,
        }
        for k,v in rows.items():
            c1,c2 = st.columns([4,5]); c1.caption(k); c2.write(v)

# ── Briefing banner ───────────────────────────────────────────────────────────
if st.session_state.get("briefing"):
    st.info(st.session_state["briefing"])
    if st.button("✕ Dismiss"): del st.session_state["briefing"]; st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════
tab_home, tab_week, tab_add, tab_profiles = st.tabs([
    "🏠 Today", "📅 This Week", "➕ Add Event", "👦 Profiles"])

# ── TAB 1: TODAY ──────────────────────────────────────────────────────────────
with tab_home:
    today = date.today()
    st.subheader(f"Today — {today.strftime('%A, %B %d')}")

    today_events = _query_today(user_id)
    if selected_child != "All children":
        today_events = [e for e in today_events if e["child_name"] in (selected_child,"all")]

    if today_events:
        for ev in today_events:
            label    = ev["event_type"].replace("_"," ").title()
            time_str = f" · {ev['event_time']}" if ev.get("event_time") else ""
            notes    = f" — {ev['notes']}" if ev.get("notes") else ""
            c1,c2 = st.columns([9,1])
            with c1: st.markdown(f"🔔 **{ev['child_name']}** — {label}{time_str}{notes}")
            with c2:
                if st.button("🗑", key=f"del_t_{ev['id']}"): _delete_event(user_id, ev["id"]); st.rerun()
    else:
        st.success("✓ Nothing scheduled today")

    st.divider()

    # Tomorrow
    tomorrow = today + timedelta(days=1)
    tmrw_evs = [e for e in _query_upcoming(user_id, days_ahead=2)
                if e["event_date"] == tomorrow.isoformat()]
    if tmrw_evs:
        st.markdown(f"**Tomorrow — {tomorrow.strftime('%A, %b %d')}**")
        for ev in tmrw_evs:
            st.markdown(f"&nbsp;&nbsp;• **{ev['child_name']}** — {ev['event_type'].replace('_',' ').title()}")

    # Needs attention
    st.divider()
    st.markdown("**📌 Needs attention**")
    upcoming = _query_upcoming(user_id, days_ahead=14)
    flags = []
    for ev in upcoming:
        days_away = (date.fromisoformat(ev["event_date"]) - today).days
        if ev["event_type"] == "recital" and days_away <= 7:
            flags.append(f"🎭 {ev['child_name']}'s recital in {days_away}d — confirm costume & arrival")
        elif ev["event_type"] == "field_trip" and days_away <= 7:
            flags.append(f"🚌 {ev['child_name']}'s field trip in {days_away}d — check permission slip")
        elif ev["event_type"] == "doctor_appointment" and days_away <= 2:
            flags.append(f"🏥 {ev['child_name']}'s appointment in {days_away}d")
    if flags:
        for f in flags: st.warning(f)
    else:
        st.caption("Nothing urgent right now ✓")

    st.divider()
    st.markdown("**💬 Ask Hearth**")
    user_input = st.text_input("", placeholder='"What does Emma have this week?"', key="chat")
    if st.button("Ask ➤") and user_input.strip():
        with st.spinner("…"):
            result = run(raw_text=user_input.strip(), user_id=user_id)
        st.markdown(result.get("response",""))
        st.rerun()

# ── TAB 2: THIS WEEK ──────────────────────────────────────────────────────────
with tab_week:
    col_hd, col_days = st.columns([5,2])
    with col_hd: st.subheader("Upcoming events")
    with col_days:
        days = st.selectbox("Show", [7, 14, 30, 60], index=1,
                            format_func=lambda d: f"Next {d} days",
                            key="week_days")

    events = _query_upcoming(user_id, days_ahead=days)
    if selected_child != "All children":
        events = [e for e in events if e["child_name"] in (selected_child,"all")]

    if not events:
        st.info(f"No events in the next {days} days.")
    else:
        for edate_str, group in groupby(events, key=lambda e: e["event_date"]):
            st.markdown(f"**{date.fromisoformat(edate_str).strftime('%A, %b %d')}**")
            for ev in group:
                label     = ev["event_type"].replace("_"," ").title()
                time_str  = f" · {ev['event_time']}" if ev.get("event_time") else ""
                notes_str = f" — {ev['notes']}"      if ev.get("notes")      else ""
                synced    = (" 📅G" if ev.get("gcal_event_id") else "") + \
                            (" 📅O" if ev.get("outlook_event_id") else "")
                c1,c2 = st.columns([9,1])
                with c1:
                    st.markdown(f"&nbsp;&nbsp;&nbsp;**{ev['child_name']}** · {label}"
                                f"{time_str}{notes_str}{synced}")
                with c2:
                    if st.button("🗑", key=f"del_w_{ev['id']}"): _delete_event(user_id, ev["id"]); st.rerun()
            st.markdown("")

# ── TAB 3: ADD EVENT ──────────────────────────────────────────────────────────
with tab_add:
    st.subheader("Add an event")
    mode = st.radio("Method", ["Type it", "Upload PDF"], horizontal=True)

    if mode == "Type it":
        text = st.text_area("Describe the event", height=80, key="add_text",
                            placeholder='"Emma has soccer at 5pm on May 15"')
        if st.button("Add ➤") and text.strip():
            with st.spinner("Adding…"):
                result = run(raw_text=text.strip(), user_id=user_id)
            st.markdown(result.get("response","")); st.rerun()
    else:
        uploaded = st.file_uploader("School newsletter PDF", type=["pdf"])
        if uploaded and st.button("📋 Extract events"):
            with st.spinner("Reading…"):
                result = run(pdf_bytes=uploaded.read(), user_id=user_id)
            st.session_state["pdf_events"] = result.get("extracted_events",[])
            st.info(result.get("response",""))

        if st.session_state.get("pdf_events"):
            extracted = st.session_state["pdf_events"]
            st.subheader(f"Review — {len(extracted)} event(s)")
            keep_flags = []
            for i,ev in enumerate(extracted):
                label = ev.get("event_type","other").replace("_"," ").title()
                keep_flags.append(st.checkbox(
                    f"**{ev.get('event_date')}** · {ev.get('child_name','all')} · {label}"
                    + (f" at {ev['event_time']}" if ev.get("event_time") else "")
                    + (f" — {ev['notes']}" if ev.get("notes") else ""),
                    value=True, key=f"pdf_{i}"))
            if st.button("✅ Save selected"):
                saved = 0
                for ev, keep in zip(extracted, keep_flags):
                    if keep:
                        try:
                            eid = _insert_event(user_id, ev.get("child_name","all"),
                                                ev.get("event_type","other"), ev["event_date"],
                                                ev.get("event_time"), ev.get("notes"))
                            from agent.calendar_agent import _sync_to_gcal
                            _sync_to_gcal(user_id, eid, ev.get("child_name","all"),
                                          ev.get("event_type","other"), ev["event_date"],
                                          ev.get("event_time"), ev.get("notes"))
                            saved += 1
                        except: pass
                st.success(f"✅ {saved} event(s) saved.")
                st.session_state.pop("pdf_events",None); st.rerun()

# ── TAB 4: PROFILES ───────────────────────────────────────────────────────────
with tab_profiles:
    st.subheader("Child profiles")
    profiles = get_all_profiles(user_id)
    if profiles:
        for p in profiles:
            with st.expander(f"**{p['name']}** — Grade {p.get('grade','?')} · {p.get('school','')}"):
                st.write(f"**Activities:** {p.get('activities','—')}")
                st.write(f"**Notes:** {p.get('notes','—')}")
    else:
        st.info("No profiles yet.")

    st.divider()
    st.markdown("**Add or update a profile**")
    with st.form("profile_form"):
        c1,c2 = st.columns(2)
        with c1:
            pname  = st.text_input("Child's name *")
            pgrade = st.text_input("Grade")
        with c2:
            pschool = st.text_input("School")
            pacts   = st.text_input("Activities")
        pnotes = st.text_area("Notes", height=60)
        if st.form_submit_button("Save profile") and pname.strip():
            upsert_profile(user_id, pname.strip(), pgrade, pschool, pacts, pnotes)
            st.success(f"✅ Saved profile for {pname}"); st.rerun()
