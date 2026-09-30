# app/agents/nodes/turn_plan.py
import asyncio
import re
from time import monotonic
from app.config import settings
from app.contracts.turn_plan import TurnPlan
from app.gateway.groq_client import GroqGateway, gateway
from app.prompts.registry import ACTIVE
from app.tutor.arithmetic import reconcile_explicit_arithmetic
from app.tutor.consensus import LaneResult, select_consensus
from app.contracts.turn_plan import FATAL_PLAN_CODES
from app.observability import log_event
from app.tutor.plan_validation import is_fatal, validate_turn_plan

QUESTION_REQUIRES_VISUAL_REGEX = re.compile(
    r"\b(draw|construct|figure|diagram|graph|plot|sketch|shown|locate|number line|bisector)\b",
    re.IGNORECASE,
)


# Geometry NOUNS. CBSE geometry is almost always noun-framed ("In triangle ABC, AB = 5 cm,
# angle B = 90 deg. Find AC.") and carries none of the drawing VERBS above, so the verb
# regex alone would leave visual_requirement at whatever the model said -- usually "none"
# when the question only asks for a length -- and the scene compiler would be skipped.
QUESTION_IS_GEOMETRIC_REGEX = re.compile(
    r"\b(triangles?|circles?|chords?|tangents?|secants?|quadrilaterals?|parallelograms?|rhombus|"
    r"rhombi|trapezium|trapezia|rectangles?|square(?!\s*roots?)|polygons?|pentagons?|hexagons?|"
    r"angles?|perpendicular|parallel|bisectors?|radius|radii|diameters?|arcs?|sectors?|"
    r"segments?|transversals?|coordinates?|axis|axes|medians?|altitudes?|vertex|vertices|"
    r"rays?|cones?|cylinders?|spheres?|hemispheres?|cuboids?|cubes?|number\s+line|"
    r"collinear|concyclic|inscribed|circumscribed|congruent|similar\s+triangles)\b"
    r"|[\u2220\u25b3\u22a5\u2225]",  # angle, triangle, perpendicular, parallel symbols
    re.IGNORECASE,
)


def question_requires_visual(question: str) -> bool:
    """Question explicitly demands a drawing (verbs): upgrades visual_requirement to required."""
    return bool(QUESTION_REQUIRES_VISUAL_REGEX.search(question or ""))


def question_is_geometric(question: str) -> bool:
    """Question is about a figure (nouns): a figure helps, so never let it be 'none'."""
    return bool(QUESTION_IS_GEOMETRIC_REGEX.search(question or ""))


def apply_visual_gate(plan: TurnPlan, question: str) -> None:
    """Deterministic floor on the model's visual_requirement (it can raise, never lower)."""
    if question_requires_visual(question):
        plan.visual_requirement = "required"
    elif question_is_geometric(question) and plan.visual_requirement == "none":
        plan.visual_requirement = "optional"


async def run_lane(
    prompt_key: str, user: str, question: str, gw: GroqGateway | None = None
) -> LaneResult:
    """Execute a single TurnPlan generation lane."""
    gw_client = gw or gateway
    try:
        raw = await gw_client.complete_json(
            prompt_key=prompt_key,
            user=user,
            schema=TurnPlan,
            model=settings.MODEL_MAIN,
            timeout_s=settings.TURN_PLAN_LANE_TIMEOUT_S,
        )
        if raw is None:
            return LaneResult(plan=None, lane=prompt_key)
        plan, reconciled = reconcile_explicit_arithmetic(raw)
        apply_visual_gate(plan, question)
        issues = validate_turn_plan(plan, expected_question=question)
        if is_fatal(issues):
            log_event("turnplan_lane_rejected", lane=prompt_key,
                      codes=sorted({i.code for i in issues if i.code in FATAL_PLAN_CODES}))
            return LaneResult(plan=None, issues=issues, lane=prompt_key)
        return LaneResult(plan=plan, reconciled=reconciled, lane=prompt_key, issues=issues)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # a bug in one lane must never take down the race
        log_event("turnplan_lane_crashed", lane=prompt_key, error=f"{type(e).__name__}: {e}")
        return LaneResult(plan=None, lane=prompt_key)


async def plan_turn(
    question: str, conversation: str = "", gw: GroqGateway | None = None
) -> TurnPlan | None:
    """Execute TurnPlan dual-lane race or single primary lane based on TURN_PLAN_DUAL_LANE toggle."""
    gw_client = gw or gateway
    grace_s = settings.TURN_PLAN_PEER_GRACE_MS / 1000.0

    if not settings.TURN_PLAN_DUAL_LANE:
        primary = await run_lane(
            ACTIVE["turn_plan.primary"],
            f"{conversation}QUESTION\n{question}",
            question,
            gw=gw_client,
        )
        if primary.plan is not None:
            return primary.plan
        retry = await run_lane(
            ACTIVE["turn_plan.retry"],
            f"{conversation}QUESTION\n{question}\n\n"
            "Return a complete corrected plan. Missing requested numeric answers are fatal.",
            question,
            gw=gw_client,
        )
        return retry.plan

    lanes = {
        0: run_lane(
            ACTIVE["turn_plan.primary"],
            f"{conversation}QUESTION\n{question}",
            question,
            gw=gw_client,
        ),
        1: run_lane(
            ACTIVE["turn_plan.retry"],
            f"{conversation}QUESTION\n{question}\n\n"
            "Independently solve every requested unknown and return one complete checked plan.",
            question,
            gw=gw_client,
        ),
    }
    tasks = {i: asyncio.create_task(c) for i, c in lanes.items()}
    completed: list[LaneResult] = []
    first_valid_at: float | None = None

    try:
        while tasks:
            if first_valid_at is None:
                timeout = None
            else:
                timeout = max(0.0, grace_s - (monotonic() - first_valid_at))
            if timeout == 0.0:
                break
            done, _ = await asyncio.wait(
                tasks.values(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                break  # grace window expired
            for t in done:
                idx = next(i for i, tt in tasks.items() if tt is t)
                del tasks[idx]
                res = t.result()
                if res.plan is not None:
                    completed.append(res)
                    first_valid_at = first_valid_at or monotonic()
    finally:
        for t in tasks.values():
            t.cancel()  # abort the pending peer

    if completed:
        return select_consensus(completed)

    retry = await run_lane(
        ACTIVE["turn_plan.retry"],
        f"{conversation}QUESTION\n{question}\n\n"
        "Return a complete corrected plan. Missing requested numeric answers are fatal.",
        question,
        gw=gw_client,
    )
    return retry.plan
