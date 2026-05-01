"""
agent/graph.py

Hearth LangGraph graph definition.

Two independent lanes:
  1. User lane  — intake_agent routes typed input or PDF to calendar_agent / event_parser
  2. Cron lane  — nudge_scheduler triggers nudge_check → dispatcher

Graph:
    supervisor
        └── intake_agent
                ├── event_parser  (pdf path)
                │       └── calendar_agent
                ├── calendar_agent (manual / nl_command / query)
                └── END (unknown)
"""

from langgraph.graph import StateGraph, END

from agent.state import HearthState
from agent.intake_agent import intake_agent, route_from_intake
from agent.calendar_agent import calendar_agent
from agent.event_parser import event_parser


def build_graph() -> StateGraph:
    g = StateGraph(HearthState)

    g.add_node("intake_agent",   intake_agent)
    g.add_node("event_parser",   event_parser)
    g.add_node("calendar_agent", calendar_agent)

    g.set_entry_point("intake_agent")

    g.add_conditional_edges(
        "intake_agent",
        route_from_intake,
        {
            "event_parser":   "event_parser",
            "calendar_agent": "calendar_agent",
            END:               END,
        },
    )

    g.add_edge("event_parser",   "calendar_agent")
    g.add_edge("calendar_agent", END)

    return g.compile()


hearth_graph = build_graph()


def run(raw_text: str = None, pdf_bytes: bytes = None) -> HearthState:
    """
    Entry point for the Streamlit UI and FastAPI endpoints.
    """
    initial: HearthState = {
        "input_type":       "",
        "raw_text":         raw_text,
        "pdf_bytes":        pdf_bytes,
        "extracted_events": [],
        "confirmed_events": [],
        "response":         "",
    }
    return hearth_graph.invoke(initial)
