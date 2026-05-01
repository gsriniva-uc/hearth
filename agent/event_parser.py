"""
agent/event_parser.py

Extracts structured school events from a PDF newsletter or plain text.
"""

import json
from datetime import date

import anthropic
import PyPDF2
import io

import hearth_config as cfg
from agent.state import HearthState


_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

EVENT_TYPES = [
    "dress_down_day", "early_dismissal", "recital", "movie_night",
    "field_trip", "special_day", "doctor_appointment", "sports_game",
    "school_holiday", "other",
]


def _pdf_to_text(pdf_bytes: bytes) -> str:
    reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_events(text: str, children: list) -> list:
    today_str = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(children) if children else "the children"

    prompt = f"""You are parsing a school newsletter to extract family calendar events.
Today is {today_str}.
Children in this family: {children_str}.
Valid event_type values: {", ".join(EVENT_TYPES)}.

Newsletter text:
---
{text[:6000]}
---

Extract every school event, special day, early dismissal, field trip, recital,
movie night, or other notable date mentioned.

Return a JSON array. Each item:
{{
  "child_name": "<name from family list, or 'all'>",
  "event_type": "<one of the valid types>",
  "event_date": "<YYYY-MM-DD>",
  "event_time": "<HH:MM or null>",
  "notes": "<brief description or null>"
}}

Ignore page numbers, headers, footers, contact info dates.
Return ONLY the JSON array."""

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

    if pdf_bytes:
        try:
            text = _pdf_to_text(pdf_bytes)
        except Exception as e:
            return {**state, "extracted_events": [],
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

    return {
        **state,
        "input_type": "manual",
        "extracted_events": events,
        "response": f"Found {len(events)} event(s). Review below, then confirm to save.",
    }
