"""
agent/event_parser.py

Extracts school events from PDFs using two strategies:
  1. Text-based PDFs  → PyPDF2 text extraction → Claude text analysis
  2. Image-based PDFs → Convert pages to images → Claude vision analysis

Image-based PDFs (scanned documents, screenshots, calendar photos) are
handled by rendering each page as a PNG and sending it to Claude's vision API.
This correctly handles school calendar images, newsletter scans, etc.
"""
import io
import json
import re
import base64
from datetime import date

import anthropic
import hearth_config as cfg
from agent.state import HearthState

_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

EVENT_TYPES = [
    "dress_down_day", "early_dismissal", "recital", "movie_night", "field_trip",
    "special_day", "doctor_appointment", "sports_game", "school_holiday", "other",
]

EXTRACT_PROMPT = """You are reading a school calendar or newsletter for a family assistant called Hearth.
Today is {today}. Children in this family: {children}.
Valid event_type values: {event_types}.

Extract EVERY school event, holiday, special day, early dismissal, no-school day,
field trip, mass, picture day, parent-teacher conference, report card day,
or any other notable date that a parent would want to know about.

For EACH event return a JSON object:
{{
  "child_name": "<child name from family list, or 'all' if school-wide>",
  "event_type": "<one of the valid types>",
  "event_date": "<YYYY-MM-DD — use the year shown on the calendar>",
  "event_time": "<HH:MM 24h or null>",
  "notes": "<brief plain-English description of the event>"
}}

Rules:
- Include ALL events visible in the calendar, not just upcoming ones — the parent will filter.
- For recurring events (e.g. "First Friday/No School" every month), create one entry per occurrence.
- Use the year shown on the calendar document (e.g. 2025 or 2026).
- If a date range is shown (e.g. "Nov 26-28 Thanksgiving Break"), create one entry per day.
- school_holiday = no school day (Labor Day, Thanksgiving, Christmas, etc.)
- early_dismissal = shortened school day
- special_day = picture day, mass, spirit day, dress-down, etc.

Return ONLY a JSON array. No prose, no markdown fences."""


def _pdf_to_images(pdf_bytes: bytes) -> list[bytes]:
    """Convert PDF pages to PNG images using pdf2image (poppler)."""
    try:
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(pdf_bytes, dpi=200)
        result = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            result.append(buf.getvalue())
        return result
    except ImportError:
        return []
    except Exception as e:
        print(f"[event_parser] pdf2image failed: {e}")
        return []


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from text-based PDFs."""
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception:
        return ""


def _extract_via_vision(image_bytes_list: list[bytes], children: list) -> list:
    """Send PDF pages as images to Claude vision."""
    today_str    = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(children) or "all children"
    prompt       = EXTRACT_PROMPT.format(
        today=today_str, children=children_str,
        event_types=", ".join(EVENT_TYPES))

    # Build content blocks — one image per page
    content = []
    for img_bytes in image_bytes_list[:4]:   # cap at 4 pages
        b64 = base64.standard_b64encode(img_bytes).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64}
        })
    content.append({"type": "text", "text": prompt})

    resp = _client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        events = json.loads(raw)
        return events if isinstance(events, list) else []
    except json.JSONDecodeError:
        return []


def _extract_via_text(text: str, children: list) -> list:
    """Extract events from plain text using Claude."""
    today_str    = date.today().strftime("%A, %B %d, %Y")
    children_str = ", ".join(children) or "all children"
    prompt       = EXTRACT_PROMPT.format(
        today=today_str, children=children_str,
        event_types=", ".join(EVENT_TYPES))

    full_prompt = f"{prompt}\n\nDocument text:\n---\n{text[:6000]}\n---"

    resp = _client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": full_prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        events = json.loads(raw)
        return events if isinstance(events, list) else []
    except json.JSONDecodeError:
        return []


def event_parser(state: HearthState) -> HearthState:
    pdf_bytes = state.get("pdf_bytes")
    raw_text  = state.get("raw_text") or ""
    children  = cfg.CHILDREN

    if not pdf_bytes and not raw_text:
        return {**state, "extracted_events": [],
                "response": "⚠️ No PDF or text to parse."}

    # ── Strategy 1: try vision first for PDFs (handles image-based calendars) ──
    if pdf_bytes:
        images = _pdf_to_images(pdf_bytes)
        if images:
            print(f"[event_parser] Using vision — {len(images)} page(s)")
            events = _extract_via_vision(images, children)
            if events:
                return {**state, "input_type": "manual",
                        "extracted_events": events,
                        "response": f"Found {len(events)} event(s) via vision. Review and confirm to save."}

    # ── Strategy 2: fall back to text extraction ──────────────────────────────
    if pdf_bytes:
        text = _pdf_to_text(pdf_bytes)
    else:
        text = raw_text

    if len(text.strip()) < 30:
        return {**state, "extracted_events": [],
                "response": "⚠️ Could not read this PDF — it may be image-only. "
                           "Try installing poppler: `brew install poppler` then restart."}

    events = _extract_via_text(text, children)
    if not events:
        return {**state, "extracted_events": [],
                "response": "No events found. Try adding them manually."}

    return {**state, "input_type": "manual",
            "extracted_events": events,
            "response": f"Found {len(events)} event(s). Review and confirm to save."}
