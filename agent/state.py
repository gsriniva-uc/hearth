"""agent/state.py — Shared LangGraph state"""
from typing import TypedDict, Optional, Literal

class HearthState(TypedDict):
    # Input
    input_type:  str                          # manual | nl_command | pdf | query | unknown
    raw_text:    Optional[str]
    pdf_bytes:   Optional[bytes]
    user_id:     Optional[str]                # for multi-user (future)

    # Routing
    intent:      Optional[str]                # add_event | query | briefing | nudge | profile

    # Events
    extracted_events: list
    confirmed_events: list

    # Briefing
    briefing_text: Optional[str]

    # Profile
    target_child: Optional[str]

    # Output
    response:    str
    notify:      bool                         # should notification_agent fire?
    notify_message: Optional[str]             # message to deliver
