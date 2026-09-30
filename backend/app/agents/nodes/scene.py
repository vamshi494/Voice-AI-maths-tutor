# app/agents/nodes/scene.py
from typing import Any
from app.config import settings
from app.contracts.diagram import VerifiedDiagram, has_drawable_ink
from app.contracts.scene import SceneDocument, ValidationReport
from app.contracts.turn_plan import TurnPlan
from app.gateway.groq_client import GroqGateway, gateway
from app.observability import log_event
from app.prompts.registry import ACTIVE
from app.scene_engine.capability import get_operator_catalogue, get_predicate_catalogue
from app.scene_engine.compile import RenderScene, compile_scene_document
from app.scene_engine.verified_diagram import build_verified_diagram
from app.agents.nodes.turn_plan import question_is_geometric, question_requires_visual


async def plan_and_compile_scene(
    question: str,
    plan: TurnPlan | None,
    namespace: str = "",
    gw: GroqGateway | None = None,
    declared_figure: bool = False,
) -> tuple[RenderScene | None, ValidationReport | None, VerifiedDiagram | None, str]:
    """Execute scene plan + compile + repair loop (up to SCENE_MAX_CANDIDATES=3).

    Returns: (RenderScene, ValidationReport, VerifiedDiagram, visual_status). Slot 0 is the
    COMPILED scene (EntityTable dict) the page pipeline projects and caches for sticky carry;
    the SceneDocument (a list of entities) must not be returned there, or every chapter figure
    block fails with AttributeError: 'list' object has no attribute 'values'. It is None
    whenever no validated diagram was produced.
    """
    gw_client = gw or gateway

    # 1. Gate. The figure is decoupled from the NUMERIC plan: a geometry question whose
    #    TurnPlan failed validation still deserves its figure, so a minimal plan carries
    #    only the question into the prompt.
    #    `declared_figure`: the chapter outline already decided this block IS a figure; its brief
    #    ("Line L: ax+by+c=0, point P(x0,y0).") must not be re-gated by the question regexes,
    #    which would return text_only before any LLM call.
    geometric = declared_figure or question_is_geometric(question) or question_requires_visual(question)
    if plan is None:
        if not geometric:
            return None, None, None, "text_only"
        plan = TurnPlan(question=question, visual_requirement="required" if question_requires_visual(question) else "optional")
        log_event("scene_planned_without_turnplan")
    if plan.visual_requirement == "none" and not geometric:
        return None, None, None, "text_only"

    turn_plan_json = plan.model_dump_json(by_alias=True, exclude_none=True)
    req_header = "MANDATORY FIGURE REQUEST (visualDecision MUST be 'scene'):\n" if declared_figure else ""
    user_prompt = f"{req_header}AUTHORITATIVE TURN PLAN:\n{turn_plan_json}\n\nSUBMITTED QUESTION:\n{question}\n\nReturn the scene-document JSON object."

    fmt_args = {
        "operator_catalogue": get_operator_catalogue(),
        "predicate_catalogue": get_predicate_catalogue(),
    }

    candidates_budget = settings.SCENE_MAX_CANDIDATES
    last_report: ValidationReport | None = None

    repair_errors: list[str] = []
    for attempt in range(candidates_budget):
        if attempt == 0:
            doc, parse_error = await gw_client.complete_json_ex(
                prompt_key=ACTIVE["scene.plan"],
                user=user_prompt,
                schema=SceneDocument,
                model=settings.MODEL_MAIN,
                timeout_s=settings.SCENE_CANDIDATE_TIMEOUT_S,
                fmt_args=fmt_args,
            )
        else:
            # Every repair attempt is told WHY the previous one failed, including schema
            # rejections, which do not set last_report; otherwise the repair prompt says
            # "ERRORS:" followed by nothing.
            errors_bulleted = "\n".join(f"- {e}" for e in repair_errors) or "- the previous reply was not usable"
            repair_user_prompt = f"{user_prompt}\n\nREPAIR\nERRORS:\n{errors_bulleted}"
            doc, parse_error = await gw_client.complete_json_ex(
                prompt_key=ACTIVE["scene.repair"],
                user=repair_user_prompt,
                schema=SceneDocument,
                model=settings.MODEL_MAIN,
                timeout_s=settings.SCENE_CANDIDATE_TIMEOUT_S,
                fmt_args={**fmt_args, "errors_bulleted": errors_bulleted},
            )

        if doc is None:
            repair_errors = [f"[invalid_json] {parse_error or 'the reply was not a scene-document JSON object'}"]
            log_event("scene_candidate_unparseable", attempt=attempt, error=(parse_error or "")[:300])
            continue

        if doc.visual_decision == "text_only":
            if declared_figure:
                repair_errors = ["[rejected_text_only] This page plan explicitly mandates a figure for this block. "
                                 "Do NOT return visualDecision='text_only'. Construct representative geometric entities "
                                 "(e.g. points, lines, segments) with symbolic labels."]
                log_event("scene_text_only_rejected", attempt=attempt)
                continue
            return None, None, None, "text_only"

        try:
            scene, report = compile_scene_document(doc, plan=plan)
        except Exception as e:  # defence in depth: the compiler must never crash a turn
            log_event("scene_compiler_crashed", attempt=attempt, error=f"{type(e).__name__}: {e}")
            repair_errors = [f"[compiler_error] {type(e).__name__}: {e}"]
            continue
        last_report = report

        if report.valid and scene is not None:
            try:
                diagram = build_verified_diagram(scene, plan=plan, namespace=namespace)
            except Exception as e:
                log_event("diagram_build_crashed", attempt=attempt, error=f"{type(e).__name__}: {e}")
                repair_errors = [f"[build_error] {type(e).__name__}: {e}"]
                continue
            if has_drawable_ink(diagram):
                log_event("scene_compiled", attempt=attempt, commands=len(diagram.commands),
                          anchors=len(diagram.anchors))
                return scene, report, diagram, "validated"
            log_event("scene_ink_gate_failed", attempt=attempt)
            repair_errors = ["[no_drawable_ink] the scene compiled but draws nothing visible on the board. "
                             "DRAW_POINT alone is invisible — you MUST add polygon, segment, circle, arc, "
                             "line, ray, or curve constructions that produce visible connected geometry. "
                             "For triangles: use triangle_sss/triangle_sas/triangle_asa or a polygon "
                             "construction with segment sides. Never rely on bare points."]
        else:
            repair_errors = [f"[{e.code}] {e.message}" for e in (report.errors if report else [])][:12]
            log_event("scene_compile_failed", attempt=attempt, errors=repair_errors[:5])

    # All candidates failed -> teach text-only (never an error to the student)
    log_event("diagram_unverified_continue")
    if plan.visual_requirement == "required" or question_requires_visual(question):
        visual_status = "retry_required"
    else:
        visual_status = "text_only"

    return None, last_report, None, visual_status
