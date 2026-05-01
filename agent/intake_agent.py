"""agent/intake_agent.py — classifies input type within add_event intent"""
import anthropic
from langgraph.graph import END
import hearth_config as cfg
from agent.state import HearthState

_client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

def intake_agent(state: HearthState) -> HearthState:
    if state.get("pdf_bytes"):
        return {**state, "input_type": "pdf"}
    text = (state.get("raw_text") or "").strip()
    if not text:
        return {**state, "input_type": "unknown",
                "response": "No input received. Please type a command or upload a PDF."}
    resp = _client.messages.create(
        model=cfg.CLAUDE_MODEL, max_tokens=10,
        messages=[{"role": "user", "content":
            f'Classify: "manual" (stating an event), "nl_command" (edit existing), "query" (question).\nInput: "{text}"\nOne word only.'}],
    )
    classification = resp.content[0].text.strip().lower()
    if classification not in ("manual", "nl_command", "query"):
        classification = "manual"
    return {**state, "input_type": classification}

def route_from_intake(state: HearthState) -> str:
    return {
        "pdf":        "event_parser",
        "manual":     "calendar_agent",
        "nl_command": "calendar_agent",
        "query":      "calendar_agent",
        "unknown":    END,
    }.get(state["input_type"], END)
