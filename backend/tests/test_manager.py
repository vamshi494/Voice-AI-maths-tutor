# backend/tests/test_manager.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.contracts.agent_state import AgentState, ConvState, PageRecord, PausedLesson
from app.state_machine.manager import StateMachineManager
from app.state_machine.states import ConvEvent


@pytest.mark.asyncio
async def test_manager_typed_question_transitions_to_graph_running():
    mgr = StateMachineManager()
    assert mgr.state.conv_state == ConvState.IDLE

    # Mock session
    mgr.session = MagicMock()
    mgr.session.generate_reply = MagicMock()

    await mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Find the roots of x^2 - 4 = 0")
    assert mgr.state.conv_state == ConvState.GRAPH_RUNNING
    assert mgr.state.generation == 1
    assert mgr.state.active_turn_id is not None
    assert mgr.state.page is not None
    assert mgr.state.page.lesson_question == "Find the roots of x^2 - 4 = 0"


@pytest.mark.asyncio
async def test_speech_started_and_ended_transitions():
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.GRAPH_RUNNING
    mgr.state.active_turn_id = "turn_1"

    await mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert mgr.state.conv_state == ConvState.AGENT_SPEAKING

    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.handle_event(ConvEvent.SPEECH_ENDED)
        assert mgr.state.conv_state == ConvState.IDLE
        assert mgr.turn_saved("turn_1")


@pytest.mark.asyncio
async def test_unlisted_transition_is_ignored_and_logged():
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.IDLE

    # In IDLE, SPEECH_ENDED is not a valid transition
    res = await mgr.handle_event(ConvEvent.SPEECH_ENDED)
    assert res == ConvState.IDLE
    assert mgr.state.conv_state == ConvState.IDLE


@pytest.mark.asyncio
async def test_interrupt_and_classification_flow():
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING

    # 1. User starts speaking -> INTERRUPT_DETECTED
    await mgr.handle_event(ConvEvent.VAD_START)
    assert mgr.state.conv_state == ConvState.INTERRUPT_DETECTED

    # 2. User finishes speaking -> INTERRUPT_CLASSIFYING
    with patch.object(mgr, "_schedule_classification"):
        await mgr.handle_event(ConvEvent.USER_TURN_DONE, text="Wait, why is that true?")
        assert mgr.state.conv_state == ConvState.INTERRUPT_CLASSIFYING
        assert "why is that true" in mgr.state.interrupt_transcript


@pytest.mark.asyncio
async def test_doubt_anti_overwrite_rule():
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING
    mgr.state.active_turn_id = "turn_lesson_1"
    mgr.state.page = PageRecord(
        board_id=mgr.state.board_id,
        page_id="p1",
        lesson_question="Solve 3x = 9",
        turn_kind="lesson",
        turn_id="turn_lesson_1",
    )

    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.begin_doubt("voice", typed_text="Why divide by 3?")

    assert mgr.state.paused_lesson is not None
    assert mgr.state.paused_lesson.lesson_question == "Solve 3x = 9"
    assert mgr.state.paused_lesson.lesson_turn_id == "turn_lesson_1"
    # The prompt is built at settle from the accumulated doubt inputs.
    with patch("app.state_machine.manager.classify_figure_need", AsyncMock(return_value=False)), \
            patch.object(mgr, "run_turn", AsyncMock()) as run_turn:
        await mgr._handle_doubt_settled()
    doubt = run_turn.call_args.args[0]
    assert doubt.kind == "doubt"
    assert "Why divide by 3?" in doubt.student_text

    # Second doubt during doubt MUST NOT overwrite the original paused lesson!
    original_pl = mgr.state.paused_lesson
    mgr.state.page.turn_kind = "doubt"
    mgr.state.page.lesson_question = "Some other question"

    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.begin_doubt("voice", typed_text="Wait what?")

    # PausedLesson must still be the original
    assert mgr.state.paused_lesson is original_pl
    assert mgr.state.paused_lesson.lesson_question == "Solve 3x = 9"


@pytest.mark.asyncio
async def test_resume_lesson():
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.IDLE
    mgr.state.paused_lesson = PausedLesson(
        board_id=mgr.state.board_id,
        page_id="p1",
        lesson_question="Solve 3x = 9",
        turn_plan=None,
        solver_projection=None,
        diagram=None,
        figure_drawn=False,
        lesson_turn_id="turn_lesson_1",
        last_acked_step_index=1,
        lesson_completed=False,
    )
    mgr.session = MagicMock()
    mgr.session.generate_reply = MagicMock()

    await mgr.handle_event(ConvEvent.CONTINUE)
    assert mgr.state.conv_state == ConvState.RESUMING
    assert mgr.state.active_turn_kind == "resume"




@pytest.mark.asyncio
async def test_end_session():
    mgr = StateMachineManager()
    mgr.state.conv_state = ConvState.AGENT_SPEAKING
    mgr.state.active_turn_id = "turn_1"
    mgr.session = MagicMock()
    mgr.session.say = MagicMock(return_value=AsyncMock())

    with patch("app.state_machine.manager.persist_turn_from_state", AsyncMock(return_value=True)):
        await mgr.handle_event(ConvEvent.END)

    assert mgr.state.conv_state == ConvState.TASK_CANCELLED

    # In terminal state, subsequent events are ignored
    await mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Another question")
    assert mgr.state.conv_state == ConvState.TASK_CANCELLED


# Off-topic replay and marker pause/resume live in test_lifecycle_scenarios.py, which drives a
# real turn through FakeSession (the MagicMock versions set conv_state by hand with nothing playing).
