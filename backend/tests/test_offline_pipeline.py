# tests/test_offline_pipeline.py
import pytest
from app.agents.graph import iter_turn_steps, run_turn_stream
from app.contracts.agent_state import AgentState, PageRecord, PausedLesson, TurnRequest
from app.contracts.problem_ir import (
    BinaryNode,
    EvaluateReq,
    Evidence,
    NumberNode,
    ProblemEntity,
    ProblemExpression,
    ProblemFact,
    ProblemIR,
    ResultBinding,
)
from app.contracts.scene import SceneDocument
from app.contracts.turn_plan import Quantity, TurnPlan, Unknown
from app.tutor.board_rows import BoardRowTracker


class MockGateway:
    """Mock gateway for offline pipeline test."""

    async def complete_json(self, prompt_key, user, schema, model, timeout_s=None, fmt_args=None):
        if "turn_plan" in prompt_key:
            return TurnPlan(
                schema_version="turn-plan/v3",
                question="Find x when 2x = 6",
                visual_requirement="none",
                givens=[Quantity(id="g1", symbol="c", value=6.0, provenance="given", source_text="2x = 6")],
                derived=[Quantity(id="d1", symbol="x", value=3.0, provenance="derived", source_text="6 / 2", depends_on=["g1"])],
                unknowns=[Unknown(id="u1", symbol="x")],
                assumptions=[],
                qualitative_claims=[],
                law_ids=[],
            )
        if "problem_ir" in prompt_key:
            q = "Find x when 2x = 6"
            quote = "2x = 6"
            start = q.index(quote)
            end = start + len(quote)
            return ProblemIR(
                schema_version="problem-ir/v1",
                id="ir1",
                question=q,
                facts=[ProblemFact(id="f1", kind="given", statement="2x = 6", evidence=Evidence(source="question", start=start, end=end, quote=quote))],
                entities=[],
                expressions=[ProblemExpression(id="e1", value_type="scalar", root=NumberNode(kind="number", value=3.0), evidence_fact_ids=["f1"])],
                constraints=[],
                representation_intents=[],
                solve_requests=[EvaluateReq(id="sr1", kind="evaluate", expression_id="e1", result_binding=ResultBinding(turn_plan_quantity_id="d1", symbol="x", evidence_fact_ids=["f1"]))],
            )
        if "scene" in prompt_key:
            return SceneDocument(
                schema_version="scene/v1",
                id="s1",
                visual_decision="text_only",
                constructions=[],
                annotations=[],
                reveal_groups=[],
                assertions=[],
            )
        return None

    async def stream_text(self, prompt_key, messages, model, timeout_s=None):
        chunks = [
            "[STEP] First we divide both sides by 2 ",
            "[WRITE:x = 6 / 2] ",
            "giving [WRITE:x = 3] as our final answer. [/STEP]",
        ]
        for c in chunks:
            yield c


@pytest.mark.asyncio
async def test_offline_lesson_turn_stream():
    turn_id = "test-turn-123"
    req = TurnRequest(
        kind="lesson",
        turn_id=turn_id,
        generation=1,
        question="Find x when 2x = 6",
    )
    state = AgentState(
        session_id="sess-1",
        board_id="b1",
        user_id="u1",
        generation=1,
        active_turn_id=turn_id,
        active_turn_kind="lesson",
    )

    tracker = BoardRowTracker()
    mock_gw = MockGateway()

    # The graph yields steps; the manager/StreamPublisher publishes them. This test uses
    # the graph directly, so it collects the yielded steps (state.steps_sent is the manager's).
    steps = []
    tts_chunks = []
    async for step, tts in iter_turn_steps(req, row_tracker=tracker, agent_state=state, gw=mock_gw):
        steps.append(step)
        tts_chunks.append(tts + " ")

    full_tts = "".join(tts_chunks)
    assert len(tts_chunks) >= 1
    assert "First we divide both sides by 2" in full_tts

    # Verify steps yielded by the graph
    assert len(steps) == 1
    step0 = steps[0]
    assert step0.step_index == 0
    assert "First we divide both sides by 2 giving as our final answer." == step0.spoken_text

    # Verify board ops
    write_ops = [op for op in step0.ops if op.kind == "WRITE"]
    assert len(write_ops) == 2
    assert write_ops[0].row_id == "w1"
    assert write_ops[0].text == "x = 6 / 2"
    assert write_ops[1].row_id == "w2"
    assert write_ops[1].text == "x = 3"


@pytest.mark.asyncio
async def test_offline_doubt_turn_stream_same_board():
    turn_id = "test-doubt-456"
    req = TurnRequest(
        kind="doubt",
        turn_id=turn_id,
        generation=1,
        question="Find x when 2x = 6",
        doubt_prompt="Why did we divide by 2?",
        requires_new_figure=False,
    )
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Find x when 2x = 6",
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="x", value=3.0, provenance="derived", depends_on=[])],
        unknowns=[Unknown(id="u1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    state = AgentState(
        session_id="sess-1",
        board_id="b1",
        user_id="u1",
        generation=1,
        active_turn_id=turn_id,
        active_turn_kind="doubt",
        page=PageRecord(
            board_id="b1",
            page_id="p1",
            lesson_question="Find x when 2x = 6",
            turn_plan=plan,
            turn_kind="lesson",
            turn_id="turn-lesson-1",
        ),
    )
    tracker = BoardRowTracker()
    mock_gw = MockGateway()

    tts_chunks = []
    async for chunk in run_turn_stream(req, row_tracker=tracker, agent_state=state, gw=mock_gw):
        tts_chunks.append(chunk)

    assert len(tts_chunks) >= 1
    assert "First we divide both sides by 2" in "".join(tts_chunks)


@pytest.mark.asyncio
async def test_offline_resume_turn_stream():
    turn_id = "test-resume-789"
    req = TurnRequest(
        kind="resume",
        turn_id=turn_id,
        generation=1,
        question="Find x when 2x = 6",
    )
    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="Find x when 2x = 6",
        visual_requirement="none",
        givens=[],
        derived=[Quantity(id="d1", symbol="x", value=3.0, provenance="derived", depends_on=[])],
        unknowns=[Unknown(id="u1", symbol="x")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )
    state = AgentState(
        session_id="sess-1",
        board_id="b1",
        user_id="u1",
        generation=1,
        active_turn_id=turn_id,
        active_turn_kind="resume",
        paused_lesson=PausedLesson(
            board_id="b1",
            page_id="p1",
            lesson_question="Find x when 2x = 6",
            turn_plan=plan,
            solver_projection=None,
            diagram=None,
            figure_drawn=False,
            lesson_turn_id="turn-lesson-1",
            last_acked_step_index=0,
            lesson_completed=False,
        ),
    )
    tracker = BoardRowTracker()
    mock_gw = MockGateway()

    tts_chunks = []
    async for chunk in run_turn_stream(req, row_tracker=tracker, agent_state=state, gw=mock_gw):
        tts_chunks.append(chunk)

    assert len(tts_chunks) >= 1


@pytest.mark.asyncio
async def test_doubt_and_resume_prepare_nodes_directly():
    from app.agents.graph import doubt_prepare_node, resume_prepare_node

    req_doubt = TurnRequest(
        kind="doubt",
        turn_id="d1",
        generation=1,
        question="Solve x",
        doubt_prompt="What is x?",
        requires_new_figure=False,
    )
    doubt_res = await doubt_prepare_node({"request": req_doubt})
    assert "plan" in doubt_res
    assert "diagram" in doubt_res
    assert doubt_res["diagram"] is None

    # Test requires_new_figure=True when plan is None
    req_doubt_new_fig = TurnRequest(
        kind="doubt",
        turn_id="d2",
        generation=1,
        question="Solve x",
        doubt_prompt="What is x?",
        requires_new_figure=True,
    )
    mock_gw = MockGateway()
    doubt_new_fig_res = await doubt_prepare_node({"request": req_doubt_new_fig, "gw": mock_gw})
    assert doubt_new_fig_res["visual_status"] in ("text_only", "validated")

    # Test plan_and_compile_scene directly with plan=None
    from app.agents.nodes.scene import plan_and_compile_scene
    doc, rep, diag, vis = await plan_and_compile_scene("Solve x", None)
    assert doc is None
    assert diag is None
    assert vis == "text_only"

    req_resume = TurnRequest(
        kind="resume",
        turn_id="r1",
        generation=1,
        question="Solve x",
    )
    resume_res = await resume_prepare_node({"request": req_resume})
    assert "plan" in resume_res
    assert "diagram" in resume_res
    assert resume_res["plan"] is None
    assert resume_res["diagram"] is None

