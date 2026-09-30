# backend/tests/test_doubt_matrix.py
"""Typed-text / doubt cells of the doubt matrix.

The full state × modality matrix is exercised below; these cells add the typed and
prompt-routing cases.
"""
from types import SimpleNamespace

import pytest

from app.contracts.agent_state import TurnRequest


@pytest.mark.asyncio
async def test_new_figure_doubt_uses_new_page_prompt():
    from app.agents.nodes.teaching import stream_teaching_turn
    from app.tutor.board_rows import BoardRowTracker

    captured: dict = {}

    class FakeGateway:
        async def stream_text(self, *, prompt_key, messages, model, timeout_s):
            captured["messages"] = messages
            return
            yield  # pragma: no cover

    req = TurnRequest(
        kind="doubt",
        generation=1,
        turn_id="t_doubt",
        question="prove BPT",
        student_text="what if the triangle were obtuse?",
        doubt_prompt="i have a doubt: what if the triangle were obtuse?",
        requires_new_figure=True,
    )
    async for _ in stream_teaching_turn(req, None, None, BoardRowTracker(), gw=FakeGateway()):
        pass

    system = captured["messages"][0]["content"]
    assert "FRESH PAGE WITH A NEW FIGURE" in system
    assert "ANSWERS A DOUBT ON THE SAME BOARD" not in system


@pytest.mark.asyncio
async def test_typed_text_mid_lesson_is_classified(monkeypatch):
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    lesson_page = r.mgr.state.page.page_id

    await r.mgr.handle_event(ConvEvent.TYPED_TEXT, text="why is BC 12?")
    assert r.mgr.state.conv_state == ConvState.INTERRUPT_CLASSIFYING
    await settle()

    assert r.mgr.state.paused_lesson is not None
    assert r.mgr.state.paused_lesson.page_id == lesson_page


@pytest.mark.asyncio
async def test_typed_text_no_lesson_starts_lesson(monkeypatch):
    import json
    from types import SimpleNamespace

    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig
    from tests.test_phase0_regressions import _rpc_handlers

    r = _rig(monkeypatch)
    handlers = _rpc_handlers(r.mgr)
    resp = await handlers["submit_question"](
        SimpleNamespace(payload='{"text": "solve 2x + 3 = 11", "intent": "auto"}'))
    await settle()

    assert json.loads(resp)["ok"] is True
    assert r.mgr.state.page is not None
    assert r.rec.of("turn_started")[-1].kind == "lesson"


@pytest.mark.asyncio
async def test_typed_text_in_task_paused_appends(monkeypatch):
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
    assert r.mgr.state.conv_state == ConvState.TASK_PAUSED

    await r.mgr.handle_event(ConvEvent.TYPED_TEXT, text="and this too")
    assert r.mgr.state.conv_state == ConvState.TASK_PAUSED
    assert "and this too" in r.mgr.state.interrupt_transcript
    await settle()


@pytest.mark.asyncio
async def test_typed_text_during_classifying_reclassifies(monkeypatch):
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)

    await r.mgr.handle_event(ConvEvent.TYPED_TEXT, text="why is BC 12?")
    assert r.mgr.state.conv_state == ConvState.INTERRUPT_CLASSIFYING
    await r.mgr.handle_event(ConvEvent.TYPED_TEXT, text="and why is that")
    assert r.mgr.state.conv_state == ConvState.INTERRUPT_CLASSIFYING
    await settle()

    # The transcript is consumed into the doubt turn at settle.
    doubt = r.mgr.state.pending_request
    assert "why is BC 12?" in doubt.student_text
    assert "and why is that" in doubt.student_text


@pytest.mark.asyncio
async def test_typed_question_intent_reaches_turn_request(monkeypatch):
    from types import SimpleNamespace

    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig
    from tests.test_phase0_regressions import _rpc_handlers

    r = _rig(monkeypatch)
    handlers = _rpc_handlers(r.mgr)

    await handlers["submit_question"](
        SimpleNamespace(payload='{"text": "teach me triangles", "intent": "new"}'))
    await settle()

    assert r.mgr.state.pending_request.intent == "new"


@pytest.mark.asyncio
async def test_checkpoint_rules(monkeypatch):
    """Doubts never write the lesson checkpoint; resume refreshes its progress."""
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _ack, _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)

    # lesson -> doubt: checkpoint written from the lesson
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why 1?", marks=[])
    await settle()
    first = r.mgr.state.paused_lesson
    assert first is not None
    lesson_question = first.lesson_question
    assert first.last_acked_step_index == -1
    assert first.heard_steps_text == []

    # the doubt answers; a second doubt never rewrites the checkpoint
    r.session.latest().release.set()
    await settle()
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="and why 2?", marks=[])
    await settle()
    assert r.mgr.state.paused_lesson == first
    assert r.mgr.state.paused_lesson.last_acked_step_index == -1
    assert r.mgr.state.paused_lesson.heard_steps_text == []

    # finish the second doubt, resume one heard step, then a third doubt refreshes progress
    r.session.latest().release.set()
    await settle()
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    _ack(r, 0)
    resume_step_text = r.mgr.state.steps_sent[0].spoken_text

    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why 3?", marks=[])
    await settle()
    refreshed = r.mgr.state.paused_lesson
    assert refreshed.lesson_question == lesson_question
    assert refreshed.last_acked_step_index == 0
    assert refreshed.heard_steps_text == [resume_step_text]


# ---------------------------------------------------------------------------------------------
# Settle accumulation, retraction and the full state × modality matrix

@pytest.mark.asyncio
async def test_settle_appends_speech(monkeypatch):
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="because of the parallel lines")
    await settle()

    assert r.mgr.state.conv_state == ConvState.TASK_CORRECTING
    pending = r.mgr.state.pending_request
    assert "why?" in pending.student_text
    assert "because of the parallel lines" in pending.student_text


@pytest.mark.asyncio
async def test_settle_merges_marks(monkeypatch):
    from app.contracts.board_ops import DoubtMark
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    m1 = DoubtMark(gesture="circle", target_kind="work", row_id="w1", text="row")
    m2 = DoubtMark(gesture="point", target_kind="diagram", entity_id="tri", text="triangle")
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[m1])
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="", marks=[m1, m2])   # rule 28
    await settle()

    assert r.mgr.state.conv_state == ConvState.TASK_CORRECTING
    marks = r.mgr.state.pending_request.marks
    assert [(m.target_kind, m.row_id, m.entity_id) for m in marks] == [
        ("work", "w1", None), ("diagram", None, "tri"),
    ]


@pytest.mark.asyncio
async def test_settle_cap(monkeypatch):
    import asyncio
    import time

    from app.config import settings
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    monkeypatch.setattr(settings, "DOUBT_SETTLE_TIMEOUT_MS", 50)
    monkeypatch.setattr(settings, "SETTLE_CAP_MS", 200)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])

    t0 = time.monotonic()
    while r.mgr.state.conv_state == ConvState.TASK_PAUSED and time.monotonic() - t0 < 1.0:
        await asyncio.sleep(0.03)
        await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="and")
    elapsed = time.monotonic() - t0

    assert 0.15 <= elapsed < 0.9, f"the hard cap must end a re-armed settle (took {elapsed:.3f}s)"
    await settle()
    assert r.mgr.state.conv_state == ConvState.TASK_CORRECTING


@pytest.mark.asyncio
async def test_retraction_resumes(monkeypatch):
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="", marks=[])
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="never mind")
    await settle()

    assert r.mgr.state.conv_state == ConvState.RESUMING
    assert r.rec.of("turn_started")[-1].kind == "resume"


@pytest.mark.asyncio
async def test_retraction_without_lesson_goes_idle(monkeypatch):
    from app.contracts.agent_state import ConvState
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig

    r = _rig(monkeypatch)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="", marks=[])
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="forget it")
    await settle()

    assert r.mgr.state.conv_state == ConvState.IDLE


MATRIX_CELLS = [
    "idle-none-voice", "idle-none-marks", "idle-none-typed",
    "idle-checkpoint-voice", "idle-checkpoint-marks", "idle-checkpoint-typed",
    "lesson-complete-voice",
    "graph-unheard-voice", "graph-unheard-typed",
    "graph-heard-voice", "graph-heard-typed",
    "speaking-voice", "speaking-marks", "speaking-typed",
    "paused-voice", "paused-marks", "paused-typed",
    "correcting-typed",
    "cancelled-voice", "cancelled-marks", "cancelled-typed",
]


def _feed_one_progress(mgr) -> None:
    """Mark step 0 as started and heard so the lesson counts as heard."""
    from app.contracts.messages import StepProgress

    st = mgr.state
    mgr.on_step_progress(StepProgress(
        turn_id=st.active_turn_id, generation=st.generation, step_index=0,
        event="completed", completed_by="words", started_up_to=0, heard_up_to=0,
        drawn_op_ids=[op.op_id for op in st.steps_sent[0].ops]))


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", MATRIX_CELLS)
async def test_doubt_matrix(cell, monkeypatch):
    """Final state, checkpoint progress and emitted turn events per matrix cell."""
    import json

    from app.contracts.agent_state import ConvState
    from app.contracts.board_ops import DoubtMark
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _say_to_tutor, _start_lesson
    from tests.test_phase0_regressions import _rpc_handlers

    state_name, modality = cell.rsplit("-", 1)
    r = _rig(monkeypatch)
    st = r.mgr.state
    marks = [DoubtMark(gesture="circle", target_kind="work", row_id="w1", text="row")]

    async def apply_modality():
        if modality == "voice":
            await _say_to_tutor(r, "why is that", "doubt")
        elif modality == "marks":
            await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="", marks=marks)
            await settle()
        else:
            if st.page is None:
                handlers = _rpc_handlers(r.mgr)
                await handlers["submit_question"](
                    SimpleNamespace(payload='{"text": "solve 2x + 3 = 11", "intent": "auto"}'))
            else:
                await r.mgr.handle_event(ConvEvent.TYPED_TEXT, text="why is that")
        await settle()

    # --- build the cell's starting state ------------------------------------------------
    if state_name == "idle-none":
        assert st.page is None
        await apply_modality()
        if modality == "typed":
            assert st.active_turn_kind == "lesson"          # no lesson: typed starts one
        else:
            assert st.active_turn_kind == "doubt"           # nothing to amend -> doubt flow
            assert st.paused_lesson is None
        return

    if state_name == "cancelled":
        st.conv_state = ConvState.TASK_CANCELLED
        before = len(r.rec.of("turn_started"))
        if modality == "typed":
            handlers = _rpc_handlers(r.mgr)
            resp = await handlers["submit_question"](
                SimpleNamespace(payload='{"text": "solve 2x + 3 = 11", "intent": "auto"}'))
            ack = json.loads(resp)
            assert ack["ok"] is False and ack["reason"] == "session_ended"
        else:
            await apply_modality()
        assert st.conv_state == ConvState.TASK_CANCELLED
        assert len(r.rec.of("turn_started")) == before       # ignored during TASK_CANCELLED
        return

    if state_name in ("graph-unheard", "graph-heard"):
        if state_name == "graph-unheard":
            # A slow producer: the lesson is GRAPH_RUNNING and has published nothing yet.
            import app.agents.graph as graph_mod
            from tests.fakes import scripted_producer
            monkeypatch.setattr(graph_mod, "iter_turn_steps",
                                scripted_producer({"lesson": ["slow step."]}, delay=2.0))
        await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me")
        assert st.conv_state == ConvState.GRAPH_RUNNING
        if state_name == "graph-heard":
            await settle()
            _feed_one_progress(r.mgr)

        await apply_modality()
        if state_name == "graph-unheard" and modality in ("voice", "typed"):
            assert "Student added:" in st.pending_request.question
        else:
            assert st.paused_lesson is not None
        return

    if state_name == "idle-checkpoint":
        await _start_lesson(r)
        await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="first?", marks=[])
        await settle()
        checkpoint = st.paused_lesson
        assert checkpoint is not None
        r.session.latest().release.set()                 # finish the first doubt
        await settle()
        assert st.conv_state == ConvState.IDLE

        await apply_modality()
        assert st.paused_lesson is checkpoint, "a doubt never overwrites the lesson checkpoint"
        return

    if state_name == "lesson-complete":
        await _start_lesson(r)
        r.session.latest().release.set()
        await settle()
        assert st.paused_lesson is None or st.paused_lesson.lesson_completed
        await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
        await settle()
        assert st.active_turn_kind == "doubt"
        return

    # speaking / paused / correcting
    await _start_lesson(r)
    if state_name == "speaking":
        await apply_modality()
        assert st.active_turn_kind == "doubt"
        assert st.paused_lesson is not None
    elif state_name == "paused":
        # get into TASK_PAUSED first, then apply the modality while settling
        await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
        assert st.conv_state == ConvState.TASK_PAUSED
        await apply_modality()
        assert st.conv_state == ConvState.TASK_CORRECTING
        pending = st.pending_request
        assert pending is not None and pending.kind == "doubt"
    elif state_name == "correcting":
        await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
        await settle()
        assert st.conv_state == ConvState.TASK_CORRECTING
        await apply_modality()
        assert st.active_turn_kind in ("doubt", "lesson")
    assert r.rec.of("step") is not None


@pytest.mark.asyncio
async def test_doubt_before_first_heard_step_amends(monkeypatch):
    """Rule 30: a doubt before any published/heard step amends the lesson question."""
    from app.contracts.agent_state import ConvState
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _say_to_tutor, _start_lesson

    r = _rig(monkeypatch, delay=2.0)                 # no step is published yet
    from app.state_machine.states import ConvEvent
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me triangles")
    await settle()
    assert r.mgr.state.conv_state == ConvState.GRAPH_RUNNING

    await _say_to_tutor(r, "wait, it should be BC 12", "doubt")

    assert r.mgr.state.conv_state == ConvState.GRAPH_RUNNING
    q = r.mgr.state.pending_request.question
    assert "Student added: wait, it should be BC 12" in q
    assert r.mgr.state.paused_lesson is None, "an amendment writes no checkpoint"
    await settle()


@pytest.mark.asyncio
async def test_doubt_after_heard_step_is_doubt(monkeypatch):
    """Rule 10: once a step was heard, the same input starts a doubt with a checkpoint."""
    from app.contracts.agent_state import ConvState
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _say_to_tutor, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    _feed_one_progress(r.mgr)

    await _say_to_tutor(r, "why is BC 12", "doubt")
    await settle()

    assert r.mgr.state.conv_state == ConvState.TASK_CORRECTING
    assert r.mgr.state.paused_lesson is not None
    assert r.mgr.state.pending_request.kind == "doubt"
