"""
agent/intake_agent.py

Classifies incoming input as one of:
  pdf        → route to event_parser
  manual     → user is directly stating an event
  nl_command → natural language calendar operation
  query      → question about upcoming events
  unknown    → cannot classify
"""

import anthropic
from langgraph.graph import END

import hearth_config as cfg
from agent.state import HearthState


_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)


def intake_agent(state: HearthState) -> HearthState:
    # ── Deterministic fast-path ───────────────────────────────────────────────
    if state.get("pdf_bytes"):
        return {**state, "input_type": "pdf"}

    text = (state.get("raw_text") or "").strip()
    if not text:
        return {**state, "input_type": "unknown",
                "response": "I didn't receive any input. Please type a command or upload a PDF."}

    # ── LLM classification ────────────────────────────────────────────────────
    prompt = f"""Classify this input into exactly one category:
- "manual"     : user is directly stating an event to add
  (e.g. "Add dress-down day on Friday May 9", "Avery has a recital June 3 at 6pm")
- "nl_command" : natural-language calendar operation
  (e.g. "Delete the recital", "Move movie night to Saturday", "Show me next week")
- "query"      : question about upcoming events
  (e.g. "When is the next early dismissal?", "What's happening this week?")

Input: "{text}"

Reply with exactly one word — the category. Nothing else."""

    resp = _client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    classification = resp.content[0].text.strip().lower()
    if classification not in ("manual", "nl_command", "query"):
        classification = "nl_command"

    return {**state, "input_type": classification}


def route_from_intake(state: HearthState) -> str:
    routes = {
        "pdf":        "event_parser",
        "manual":     "calendar_agent",
        "nl_command": "calendar_agent",
        "query":      "calendar_agent",
        "unknown":    END,
    }
    return routes.get(state["input_type"], END)
