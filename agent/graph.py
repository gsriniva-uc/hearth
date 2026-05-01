"""agent/graph.py — Multi-user Hearth LangGraph"""
from langgraph.graph import StateGraph, END
from agent.state import HearthState
from agent.supervisor import supervisor, route_from_supervisor
from agent.intake_agent import intake_agent, route_from_intake
from agent.calendar_agent import calendar_agent
from agent.event_parser import event_parser
from agent.briefing_agent import briefing_agent
from agent.profile_agent import profile_agent
from agent.notification_agent import notification_agent

def build_graph():
    g = StateGraph(HearthState)
    g.add_node("supervisor",         supervisor)
    g.add_node("intake_agent",       intake_agent)
    g.add_node("event_parser",       event_parser)
    g.add_node("calendar_agent",     calendar_agent)
    g.add_node("briefing_agent",     briefing_agent)
    g.add_node("profile_agent",      profile_agent)
    g.add_node("notification_agent", notification_agent)
    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", route_from_supervisor, {
        "intake_agent":   "intake_agent",
        "calendar_agent": "calendar_agent",
        "briefing_agent": "briefing_agent",
        "profile_agent":  "profile_agent",
    })
    g.add_conditional_edges("intake_agent", route_from_intake, {
        "event_parser":   "event_parser",
        "calendar_agent": "calendar_agent",
        END: END,
    })
    g.add_edge("event_parser", "calendar_agent")
    for node in ("calendar_agent","briefing_agent"):
        g.add_conditional_edges(node,
            lambda s: "notification_agent" if s.get("notify") else END,
            {"notification_agent":"notification_agent", END:END})
    g.add_edge("profile_agent", END)
    g.add_edge("notification_agent", END)
    return g.compile()

hearth_graph = build_graph()

def run(raw_text: str = None, pdf_bytes: bytes = None, user_id: str = "default") -> HearthState:
    initial: HearthState = {
        "input_type":"","raw_text":raw_text,"pdf_bytes":pdf_bytes,
        "user_id":user_id,"intent":None,
        "extracted_events":[],"confirmed_events":[],
        "briefing_text":None,"target_child":None,
        "response":"","notify":False,"notify_message":None,
    }
    return hearth_graph.invoke(initial)
