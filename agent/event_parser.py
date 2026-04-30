"""
agent/event_parser.py

Extracts structured school events from a PDF newsletter (or plain text).

Flow:
  1. Extract raw text from PDF bytes using PyPDF2
  2. Send text + today's date to Claude for structured extraction
  3. Write extracted events into state["extracted_events"] for calendar_agent to confirm

The Streamlit UI shows extracted_events to the user BEFORE calendar_agent commits them,
so the parent can review / remove false positives (e.g. dates in page footers).
"""

import json
from datetime import date

import anthropic
import PyPDF2
import io

import hearth_config as cfg
from agent.graph import HearthState   # type: ignore


_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

EVENT_TYPES = [
    "dress_down_day", "early_dismissal", "recital", "movie_night",
    "field_trip", "special_day", "doctor_appointment", "sports_game",
    "school_holiday", "other",
]


def _pdf_to_text(pdf_bytes: bytes) -> str:
    reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _extract_events(text: str, children: list[str]) -> list[dict]:
    today_str = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(children) if children else "the children"
    event_types_str = ", ".join(EVENT_TYPES)

    prompt = f"""You are parsing a school newsletter to extract family calendar events.
Today is {today_str}.
Children in this family: {children_str}.
Valid event_type values: {event_types_str}.

Newsletter text:
---
{text[:6000]}
---

Extract every school event, special day, early dismissal, field trip, recital,
movie night, or other notable date mentioned.

Return a JSON array. Each item:
{{
  "child_name": "<name from family list, or 'all' if applies to everyone>",
  "event_type": "<one of the valid types>",
  "event_date": "<YYYY-MM-DD — resolve all relative and partial dates from today>",
  "event_time": "<HH:MM in 24h or null>",
  "notes": "<brief description or null>"
}}

Rules:
- Ignore page numbers, headers, footers, and contact info dates.
- If a date is ambiguous (e.g. "the 5th" with no month), use the next upcoming occurrence.
- Return ONLY the JSON array. No markdown, no prose."""

    resp = _client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    try:
        events = json.loads(raw)
        return events if isinstance(events, list) else []
    except json.JSONDecodeError:
        return []


def event_parser(state: HearthState) -> HearthState:
    pdf_bytes = state.get("pdf_bytes")
    raw_text  = state.get("raw_text") or ""

    # Source text: PDF takes priority, then raw_text (for pasted newsletter text)
    if pdf_bytes:
        try:
            text = _pdf_to_text(pdf_bytes)
        except Exception as e:
            return {**state,
                    "extracted_events": [],
                    "response": f"⚠️ Could not read PDF: {e}"}
    elif raw_text:
        text = raw_text
    else:
        return {**state, "extracted_events": [],
                "response": "⚠️ No PDF or text to parse."}

    if len(text.strip()) < 30:
        return {**state, "extracted_events": [],
                "response": "⚠️ The document appears to be empty or unreadable."}

    events = _extract_events(text, cfg.CHILDREN)

    if not events:
        return {**state, "extracted_events": [],
                "response": "No events found in the newsletter. Try adding them manually."}

    # Set input_type so calendar_agent knows these are pre-parsed
    return {
        **state,
        "input_type": "manual",          # calendar_agent will add directly
        "extracted_events": events,
        "response": f"Found {len(events)} event(s). Review below, then confirm to save.",
    }
