# tests/test_doubt_context.py
import pytest
from app.agents.nodes.doubt_context import inherited_teaching_context
from app.contracts.agent_state import DoubtMark, PageRecord
from app.contracts.turn_plan import Quantity, TurnPlan, Unknown
from app.persistence.models import SceneArtifacts
from app.tutor.doubt_prompt import build_marked_doubt_prompt


class DummyTurn:
    def __init__(self, question: str, scene_artifacts: dict):
        self.question = question
        self.scene_artifacts = scene_artifacts


def make_plan(q: str) -> TurnPlan:
    return TurnPlan(
        schema_version="turn-plan/v3",
        question=q,
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="x", value=1.0, provenance="derived", depends_on=[])],
        unknowns=[Unknown(id="u1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )


def test_inherited_context_case_a_live_match():
    plan = make_plan("Find x")
    record = PageRecord(
        page_id="p1",
        board_id="b1",
        lesson_question="Find x",
        turn_kind="lesson",
        turn_plan=plan,
        solver_projection={"d1": "1"},
    )
    res_plan, res_proj = inherited_teaching_context(record, "b1", "Find x", [])
    assert res_plan == plan
    assert res_proj == {"d1": "1"}


def test_inherited_context_case_b_reopened_board():
    plan = make_plan("Find x")
    art = SceneArtifacts(
        page_id="p1",
        kind="lesson",
        status="complete",
        continues_board=False,
        turn_plan=plan,
    )
    cont_art = SceneArtifacts(
        page_id="p1",
        kind="lesson",
        status="complete",
        continues_board=True,
    )
    # Stored turns: previous turn for another question, then target turn, then a continuation turn
    turns = [
        DummyTurn("Old question", {}),
        DummyTurn("Find x", art.model_dump(by_alias=True)),
        DummyTurn("Find x", cont_art.model_dump(by_alias=True)),
    ]

    res_plan, res_proj = inherited_teaching_context(None, "b1", "Find x", turns)
    assert res_plan is not None
    assert res_plan.question == "Find x"
    assert res_proj is None


def test_inherited_context_case_b_stops_at_mismatch():
    art = SceneArtifacts(
        page_id="p1",
        kind="lesson",
        status="complete",
        continues_board=False,
    )
    turns = [
        DummyTurn("Different question", art.model_dump(by_alias=True)),
    ]
    res_plan, _ = inherited_teaching_context(None, "b1", "Find x", turns)
    assert res_plan is None


def test_build_marked_doubt_prompt_all_branches():
    # 1. Typed doubt with marks
    marks = [
        DoubtMark(gesture="circle", target_kind="work", row_id="w2", text="2x = 4"),
        DoubtMark(gesture="point", target_kind="diagram", entity_id="seg_AB", text="side AB"),
    ]
    prompt = build_marked_doubt_prompt(marks, "Why did 2 become 4?", "Solve 2x = 4")
    assert 'i have a doubt about the question "Solve 2x = 4".' in prompt
    assert "- i circled row w2: 2x = 4" in prompt
    assert "- i tapped the figure part seg_AB (side AB)" in prompt
    assert "my doubt: Why did 2 become 4?" in prompt

    # 2. No typed doubt
    prompt_no_typed = build_marked_doubt_prompt(marks, "", "Solve 2x = 4")
    assert "i did not type a doubt. i did not follow the part i marked." in prompt_no_typed

    # 3. Empty area mark only, no typed doubt
    empty_marks = [DoubtMark(gesture="point", target_kind="empty", text="")]
    prompt_empty = build_marked_doubt_prompt(empty_marks, "", "Solve 2x = 4")
    assert "- i tapped an empty area" in prompt_empty
    assert "the mark landed on an empty area, so ask what it is about before assuming which step is meant." in prompt_empty

    # 4. Voice doubt without marks or question context
    prompt_voice = build_marked_doubt_prompt([], "Can you repeat?", None)
    assert "i have a doubt about what is on the board." in prompt_voice
    assert "my doubt: Can you repeat?" in prompt_voice


def test_unmarked_voice_doubt_is_not_told_about_a_marked_part():
    """Without marks the prompt must not mention a marked part to explain; it recaps and does
    not ask which part is meant."""
    for doubt in ("You explain maybe then another example, what formula you used?",
                  "Can you summarize what we learned in the last lesson?"):
        prompt = build_marked_doubt_prompt([], doubt, "How to find distance between a line and a point?")
        assert "marked part" not in prompt
        assert "do not ask me which part" in prompt
        assert "summarize" in prompt and "recap" in prompt


def test_marked_doubt_keeps_the_marked_part_instruction():
    marks = [DoubtMark(gesture="circle", target_kind="work", row_id="w2", text="2x = 4")]
    prompt = build_marked_doubt_prompt(marks, "why?", "Solve 2x = 4")
    assert "teach that marked part again" in prompt
