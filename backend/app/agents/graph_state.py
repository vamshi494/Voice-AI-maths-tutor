# app/agents/graph_state.py
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict
from app.contracts.agent_state import PageRecord, TurnRequest, VisualStatus
from app.contracts.board_ops import Step
from app.contracts.diagram import VerifiedDiagram
from app.contracts.problem_ir import ProblemIR
from app.contracts.scene import SceneDocument, ValidationReport
from app.contracts.solver import AuthorityAudit, SolverResult
from app.contracts.turn_plan import PlanIssue, TurnPlan


@dataclass
class RunContext:
    """Per-run isolation handle: cancellation and page writes stay with the run."""

    run_id: str
    page_record: PageRecord | None
    row_tracker: Any
    is_cancelled: Callable[[], bool]
    late_scene: Any = None          # the scene task that outlived the join budget
    page_commit: Any = None         # the chapter page's built PageCommit (multi-block)
    sticky_scenes: dict[str, Any] = field(default_factory=dict)   # block id -> scene
    figure_addons: list[str] = field(default_factory=list)        # every figure prompt_addon


class GraphState(TypedDict, total=False):
    request: TurnRequest
    plan: TurnPlan | None
    plan_issues: list[PlanIssue]
    problem_ir: ProblemIR | None
    solver: SolverResult | None
    audit: AuthorityAudit | None
    scene: Any                      # compiled RenderScene (scene_engine.compile) or None
    report: ValidationReport | None
    diagram: VerifiedDiagram | None
    page_commit: Any                # PageCommit built by the page pipeline
    visual_status: VisualStatus
    raw_response: str
    steps: list[Step]
    late_scene: Any
    # Runtime context
    run_ctx: RunContext
    row_tracker: Any
    agent_state: Any
    history: list[dict[str, str]]
    memory: Any
    stored_turns: list[Any]
    solver_projection: dict[str, str]
    gw: Any
