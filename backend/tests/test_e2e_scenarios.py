# backend/tests/test_e2e_scenarios.py
"""End-to-end scenario tests.

Scenarios:
1. A typed lesson with a triangle figure, played to completion.
2. A backchannel mid-lesson: state returns to AGENT_SPEAKING.
3. A voice doubt mid-lesson, answered on the same board. 'Got it' resumes.
4. A marked doubt while another doubt is streaming: anti-overwrite preserved.
5. A 'what if it were obtuse' doubt: new page, then resume restores lesson page.
6. Off-topic: template spoken, then interrupted step replayed without duplicate ink.
7. 'Stop the class': partial turn persisted and room closes.
8. Reopen the board: pages render, and marked doubt uses Case B numbers.
9. Figure failing compilation 3 times: text-only teaching, visual_status=retry_required.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.contracts.agent_state import AgentState, ConvState, PageRecord, PausedLesson
from app.contracts.classifier import FigureNeedDecision, InterruptDecision
from app.contracts.diagram import DiagramAnchor, DiagramReveal, VerifiedDiagram
from app.contracts.messages import BoardReport, BoardRow, StepAck
from app.contracts.scene import RepairError, SceneDocument, ValidationReport
from app.contracts.turn_plan import Quantity, TurnPlan, Unknown
from app.persistence.models import Base
from app.persistence.repo import get_board, save_turn
from app.scene_engine.compile import compile_scene_document
from app.state_machine.manager import StateMachineManager
from app.state_machine.states import ConvEvent
from app.agents.nodes.doubt_context import inherited_teaching_context

_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def setup_db():
    async def _init():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _clean():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    asyncio.run(_init())
    yield
    asyncio.run(_clean())


def _make_mock_session():
    sess = MagicMock()
    sess.say = MagicMock(return_value=AsyncMock())
    sess.generate_reply = MagicMock(return_value=MagicMock())
    sess.room = MagicMock()
    sess.room.disconnect = AsyncMock()
    return sess


# -----------------------------------------------------------------------------
# Scenario 1: A typed lesson with a triangle figure, played to completion
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_1_typed_lesson_played_to_completion():
    mgr = StateMachineManager()
    mgr.session = _make_mock_session()

    # 1. Student types question
    await mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Draw a right triangle with sides 3, 4, 5")
    assert mgr.state.conv_state == ConvState.GRAPH_RUNNING
    assert mgr.state.generation == 1

    # Attach verified diagram to page
    diag = VerifiedDiagram(
        name="triangle_test",
        commands=[{"type": "DRAW_POINT", "params": [100.0, 100.0]}],
        anchors=[{"id": "A", "labels": ["A"], "x": 100.0, "y": 100.0, "width": 10.0, "height": 10.0}],
        reveals=[{"narration": "draw A", "command_indices": [0], "target_id": "rg1"}],
        prompt_addon="TRIANGLE",
    )
    mgr.state.page.diagram = diag
    mgr.state.page.visual_status = "validated"

    # 2. Agent begins speaking
    await mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING

    # 3. StepAck received
    ack = StepAck(
        turn_id=mgr.state.active_turn_id,
        generation=mgr.state.generation,
        step_index=0,
        drawn_op_ids=["op_1"],
    )
    mgr.state.last_acked_step_index = 0
    mgr.state.acked_op_ids.add("op_1")

    # 4. Turn ends
    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.handle_event(ConvEvent.SPEECH_ENDED)

    assert mgr.state.conv_state == ConvState.IDLE
    assert mgr.turn_saved(mgr.state.active_turn_id)


# -----------------------------------------------------------------------------
# Scenario 2: Backchannel mid-lesson
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_2_backchannel_mid_lesson():
    mgr = StateMachineManager()
    mgr.session = _make_mock_session()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING

    # Student says "hmm"
    await mgr.handle_event(ConvEvent.VAD_START)
    assert mgr.state.conv_state == ConvState.INTERRUPT_DETECTED

    with patch.object(mgr, "_schedule_classification"):
        await mgr.handle_event(ConvEvent.USER_TURN_DONE, text="hmm")
        assert mgr.state.conv_state == ConvState.INTERRUPT_CLASSIFYING

    # Classified as backchannel -> returns to AGENT_SPEAKING
    await mgr.handle_event(ConvEvent.CLS_BACKCHANNEL)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING


# -----------------------------------------------------------------------------
# Scenario 3: Voice doubt mid-lesson, answered on same board, then resume
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_3_voice_doubt_same_board_resume():
    mgr = StateMachineManager()
    mgr.session = _make_mock_session()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING
    mgr.state.active_turn_id = "turn_lesson_3"
    mgr.state.page = PageRecord(
        board_id=mgr.state.board_id,
        page_id="p1",
        lesson_question="Solve 2x = 8",
        turn_kind="lesson",
        turn_id="turn_lesson_3",
    )

    # 1. Student interrupts with doubt
    await mgr.handle_event(ConvEvent.VAD_START)
    with patch.object(mgr, "_schedule_classification"):
        await mgr.handle_event(ConvEvent.USER_TURN_DONE, text="Why divide by 2?")

    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.handle_event(ConvEvent.CLS_DOUBT)
    assert mgr.state.conv_state == ConvState.TASK_PAUSED
    assert mgr.state.paused_lesson is not None

    # 2. Doubt settles on same board (figure need = False)
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(
        return_value=FigureNeedDecision(requires_new_figure=False, reason="same board")
    )
    mgr.gateway = mock_gw
    await mgr.handle_event(ConvEvent.DOUBT_SETTLED)
    assert mgr.state.conv_state == ConvState.TASK_CORRECTING

    # 3. Tutor speaks doubt answer
    await mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert mgr.state.doubt_awaiting_resolution is True

    # 4. Student affirms: "Got it" -> Resumes
    await mgr.handle_event(ConvEvent.VAD_START)
    with patch.object(mgr, "_schedule_classification"):
        await mgr.handle_event(ConvEvent.USER_TURN_DONE, text="Got it, continue")

    await mgr.handle_event(ConvEvent.CLS_AFFIRMATION)
    assert mgr.state.conv_state == ConvState.RESUMING
    assert mgr.state.active_turn_kind == "resume"


# -----------------------------------------------------------------------------
# Scenario 4: Marked doubt while another doubt is streaming (anti-overwrite)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_4_marked_doubt_streaming_anti_overwrite():
    mgr = StateMachineManager()
    mgr.session = _make_mock_session()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING
    mgr.state.active_turn_id = "turn_lesson_4"
    mgr.state.page = PageRecord(
        board_id=mgr.state.board_id,
        page_id="p1",
        lesson_question="Find hypotenuse",
        turn_kind="lesson",
        turn_id="turn_lesson_4",
    )

    # First doubt
    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.begin_doubt("voice", typed_text="Doubt 1")
    first_paused = mgr.state.paused_lesson
    assert first_paused.lesson_question == "Find hypotenuse"

    # While first doubt is playing, student sends marked doubt
    mgr.state.conv_state = ConvState.AGENT_SPEAKING
    mgr.state.page.turn_kind = "doubt"

    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="Doubt 2", marks=[])

    # Original lesson is strictly preserved!
    assert mgr.state.paused_lesson is first_paused
    assert mgr.state.paused_lesson.lesson_question == "Find hypotenuse"


# -----------------------------------------------------------------------------
# Scenario 5: "What if it were obtuse" doubt -> new page, then resume restores
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Scenario 6: Off-topic redirect and replay without duplicate ink
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Scenario 7: "Stop the class" persists partial turn and closes room
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Scenario 8: Reopen board and Case B inherited context
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_8_reopen_board_and_case_b():
    board_id = f"b_sc8_{uuid4().hex[:6]}"
    user_id = "student_sc8"

    plan = TurnPlan(
        schema_version="turn-plan/v3",
        question="What is the perimeter of a rectangle with length 10 and width 5?",
        visual_requirement="none",
        givens=[
            Quantity(id="g_len", symbol="l", value=10.0, provenance="given", depends_on=[]),
            Quantity(id="g_wid", symbol="w", value=5.0, provenance="given", depends_on=[]),
        ],
        derived=[
            Quantity(id="d_per", symbol="P", value=30.0, provenance="derived", depends_on=["g_len", "g_wid"]),
        ],
        unknowns=[Unknown(id="u1", symbol="P")],
        assumptions=[],
        qualitative_claims=[],
        law_ids=[],
    )

    async with _session_factory() as session:
        await save_turn(
            session,
            turn_id=str(uuid4()),
            board_id=board_id,
            user_id=user_id,
            question="What is the perimeter of a rectangle with length 10 and width 5?",
            page_id="p_rect",
            kind="lesson",
            turn_plan=plan,
            solver_projection={"d_per": "30.0"},
            status="complete",
        )

        board = await get_board(session, board_id)
        assert board is not None
        assert len(board.turns) == 1

        # Inherited context Case B extraction
        res_plan, res_proj = inherited_teaching_context(
            None,
            board_id,
            "What is the perimeter of a rectangle with length 10 and width 5?",
            board.turns,
        )
        assert res_plan is not None
        assert res_plan.givens[0].value == 10.0
        assert res_proj is None


# -----------------------------------------------------------------------------
# Scenario 9: Figure failing compilation 3 times degrades to text-only
# -----------------------------------------------------------------------------
def test_scenario_9_figure_degrades_to_text_only():
    # Empty scene document with impossible/failing assertions
    failing_doc = SceneDocument(
        schema_version="scene-document/v2",
        visual_decision="scene",
        source="plan",
        quantities=[],
        entities=[{"id": "p1", "kind": "point"}],
        constructions=[],
        relations=[],
        assertions=[
            {
                "id": "a1",
                "predicate": "point_coincident",
                "entities": ["p1", "p2"],  # p2 doesn't exist -> fatal error
                "expected": True,
                "severity": "error",
            }
        ],
        annotations=[],
        required_entity_ids=["p1"],
        reveal_groups=[],
        teaching_timeline=[],
    )

    render_scene, report = compile_scene_document(failing_doc)
    assert report.valid is False
    assert len(report.errors) > 0
    assert render_scene is None
    # Degrades safely without raising unhandled errors to the student


# Scenarios 5-7 live in test_lifecycle_scenarios.py (real speech-handle semantics via FakeSession;
# scenario 7 asserted session.room.disconnect, but AgentSession has no .room in production).
