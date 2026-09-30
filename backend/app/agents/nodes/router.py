# app/agents/nodes/router.py
from app.contracts.agent_state import TurnRequest


def route_turn_request(request: TurnRequest) -> str:
    """Route execution based on TurnRequest.kind."""
    if request.kind == "lesson":
        return "lesson_prepare"
    if request.kind == "doubt":
        return "doubt_prepare"
    if request.kind == "resume":
        return "resume_prepare"
    raise ValueError(f"Unknown TurnRequest kind: '{request.kind}'")
