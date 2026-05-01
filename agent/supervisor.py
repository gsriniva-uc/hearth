"""
agent/supervisor.py

Hearth supervisor — classifies user intent and routes to the right agent.

Intents:
  add_event   → calendar_agent
  query       → calendar_agent
  briefing    → briefing_agent
  profile     → profile_agent
  unknown     → calendar_agent (safe default)
"""
import anthropic
import hearth_config as cfg
from agent.state import HearthState

_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

INTENT_PROMPT = """You are the supervisor for Hearth, a family calendar assistant.
Classify the user input into exactly one intent:

- "add_event"  : adding a new event (e.g. "Add dress-down day Friday")
- "query"      : asking about upcoming events (e.g. "What's this week?")
- "briefing"   : requesting a daily summary (e.g. "Give me today's briefing")
- "profile"    : managing child profiles (e.g. "Add a child named Noah, grade 2")
- "unknown"    : anything else

Input: "{text}"
Reply with exactly one word."""

def supervisor(state: HearthState) -> HearthState:
    if state.get("pdf_bytes"):
        return {**state, "intent": "add_event", "input_type": "pdf"}

    text = (state.get("raw_text") or "").strip()
    if not text:
        return {**state, "intent": "unknown"}

    resp = _client.messages.create(
        model=cfg.CLAUDE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": INTENT_PROMPT.format(text=text)}],
    )
    intent = resp.content[0].text.strip().lower()
    if intent not in ("add_event", "query", "briefing", "profile"):
        intent = "query"
    return {**state, "intent": intent}

def route_from_supervisor(state: HearthState) -> str:
    routes = {
        "add_event": "intake_agent",
        "query":     "calendar_agent",
        "briefing":  "briefing_agent",
        "profile":   "profile_agent",
        "unknown":   "calendar_agent",
    }
    return routes.get(state.get("intent", "unknown"), "calendar_agent")
