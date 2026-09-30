# app/agents/nodes/doubt_context.py
from typing import Any
from app.contracts.agent_state import PageRecord
from app.contracts.turn_plan import TurnPlan
from app.persistence.models import SceneArtifacts
from app.tutor.plan_validation import validate_turn_plan


def inherited_teaching_context(
    record: PageRecord | None,
    board_id: str,
    lesson_question: str,
    stored_turns: list[Any],
) -> tuple[TurnPlan | None, dict[str, Any] | None]:
    """Inherit the teaching context of a doubt's lesson (Case A and Case B).

    Case A: live page matches board_id and lesson_question -> return live plan & solver_projection
    Case B: reopened board -> walk stored turns backwards, skip continuation turns, re-validate stored plan.
    """
    q = lesson_question.strip()

    # Case A: the live page matches
    if record and record.board_id == board_id and record.lesson_question.strip() == q:
        return record.turn_plan, record.solver_projection

    # Case B: a reopened board — walk the stored turns backwards
    for turn in reversed(stored_turns):
        if not turn.scene_artifacts:
            continue
        art = SceneArtifacts.model_validate(turn.scene_artifacts)  # validated on read
        if art.continues_board:
            continue  # skip nested continuation turns
        if turn.question.strip() != q:
            break
        plan = art.turn_plan
        if plan is not None:
            issues = validate_turn_plan(plan, expected_question=turn.question)
            plan = plan if not [i for i in issues if i.code != "limit_exceeded"] else None
        return plan, None

    return None, None
