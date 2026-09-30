# app/agents/nodes/persist.py
from dataclasses import dataclass
from app.contracts.board_ops import Step
from app.contracts.diagram import VerifiedDiagram
from app.contracts.scene import SceneDocument, ValidationReport
from app.contracts.solver import AuthorityAudit, SolverResult
from app.contracts.turn_plan import TurnPlan


@dataclass
class TurnRecord:
    turn_id: str
    question: str
    raw_response: str
    steps: list[Step]
    trace_id: str | None = None
    scene: SceneDocument | None = None
    report: ValidationReport | None = None
    diagram: VerifiedDiagram | None = None
    visual_status: str = "text_only"
    plan: TurnPlan | None = None
    solver_projection: dict | None = None
