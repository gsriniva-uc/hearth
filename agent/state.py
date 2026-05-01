"""
agent/state.py

Shared state definition for the Hearth LangGraph graph.
Kept in its own module to avoid circular imports.
"""

from typing import TypedDict, Optional


class HearthState(TypedDict):
    input_type:       str            # "manual" | "nl_command" | "pdf" | "query" | "unknown"
    raw_text:         Optional[str]  # typed text from the user
    pdf_bytes:        Optional[bytes]# raw bytes of an uploaded PDF
    extracted_events: list           # set by event_parser
    confirmed_events: list           # set by calendar_agent after CRUD
    response:         str            # final reply to surface in the UI
