# backend/tests/test_lifecycle_scenarios.py
"""Student/teacher lifecycle scenarios through the real StateMachineManager.

No LLM calls: the graph producer is scripted (fakes.scripted_producer) and classifiers are
stubbed. Speech goes through FakeSession, which consumes the manager's async iterator and
fires done-callbacks exactly like a LiveKit SpeechHandle (natural end / interrupted).
Sessions are created with hold=True: an utterance keeps "playing" until the test releases
it, so events can be injected while the tutor is mid-sentence.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.agents.graph as graph_mod
import app.persistence.repo as repo
import app.state_machine.lesson_runner as runner_mod
import app.state_machine.manager as mgr_mod
from app.contracts.agent_state import ConvState
from app.contracts.board_ops import DoubtMark
from app.contracts.messages import StepAck
from app.persistence.models import Segment, Turn
from app.state_machine.manager import FALLBACK_LINE, GOODBYE_LINE, StateMachineManager
from app.state_machine.states import ConvEvent
from tests import fakes
from tests.fakes import EventRecorder, FakeSession, scripted_producer, settle

LESSON = [f"Lesson step {i}." for i in range(4)]


def _rig(monkeypatch, scripts=None, delay=0.0, fail=False, hold=True):
    rec = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec)
    monkeypatch.setattr(runner_mod, "send_event", rec)
    monkeypatch.setattr(fakes, "send_event", rec)
    # The settle loop waits for a quiet period; keep tests fast.
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "DOUBT_SETTLE_TIMEOUT_MS", 50)
    monkeypatch.setattr(_settings, "SETTLE_CAP_MS", 500)
    scripts = scripts or {"lesson": LESSON, "doubt": ["Doubt answer one.", "Doubt answer two."],
                          "resume": ["Picking up again.", "And we finish."]}
    monkeypatch.setattr(graph_mod, "iter_turn_steps", scripted_producer(scripts, delay=delay, fail=fail))
    labels: list[str] = []
    fig = {"new": False}

    async def fake_classify(**kw):
        return labels.pop(0) if labels else "doubt"

    async def fake_fig(**kw):
        return fig["new"]

    monkeypatch.setattr(mgr_mod, "classify_interrupt", fake_classify)
    monkeypatch.setattr(mgr_mod, "classify_figure_need", fake_fig)
    session = FakeSession(hold=hold)
    mgr = StateMachineManager(session=session)
    return SimpleNamespace(mgr=mgr, session=session, rec=rec, labels=labels, fig=fig)


async def _start_lesson(r, text="In triangle ABC find AC"):
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text=text)
    await settle()
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    return r.session.latest()


async def _say_to_tutor(r, utterance: str, label: str):
    r.labels.append(label)
    await r.mgr.handle_event(ConvEvent.VAD_START)
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text=utterance)
    await settle()


def _ack(r, idx):
    st = r.mgr.state
    step = st.steps_sent[idx]
    r.mgr.on_step_ack(StepAck(turn_id=st.active_turn_id, generation=st.generation, step_index=idx,
                              drawn_op_ids=[op.op_id for op in step.ops]))


async def _turns_in_db():
    async with repo.async_session() as s:
        return list((await s.execute(select(Turn).order_by(Turn.order_index))).scalars().all())


# --------------------------------------------------------------------------- 1
@pytest.mark.asyncio
async def test_typed_lesson_plays_to_completion_and_persists(monkeypatch):
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING
    assert "".join(h.spoken).split() == " ".join(LESSON).split()   # all steps, in order
    started = r.rec.of("turn_started")[0]
    assert started.new_page is True and started.kind == "lesson"

    h.release.set()
    await settle()
    assert r.mgr.state.conv_state == ConvState.IDLE
    ended = r.rec.of("turn_ended")
    assert len(ended) == 1 and ended[0].status == "complete"
    assert r.mgr.state.page.lesson_completed is True
    turns = await _turns_in_db()
    assert len(turns) == 1 and turns[0].scene_artifacts["kind"] == "lesson"
    assert turns[0].scene_artifacts["status"] == "complete"


# --------------------------------------------------------------------------- 2
@pytest.mark.asyncio
async def test_stale_speech_callback_cannot_end_the_next_turn(monkeypatch):
    """Regression: the interrupted handle of turn A must not fire SPEECH_ENDED into turn B and
    persist B as complete while B is still planning."""
    r = _rig(monkeypatch, delay=0.02)
    h_a = await _start_lesson(r, "question A")
    turn_a = r.mgr.state.active_turn_id
    page_a = r.mgr.state.page.page_id

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="question B")
    turn_b = r.mgr.state.active_turn_id
    await settle()
    assert h_a.interrupted is True
    assert r.mgr.state.conv_state == ConvState.GRAPH_RUNNING
    assert r.mgr.turn_saved(turn_a) and not r.mgr.turn_saved(turn_b)
    assert [e.turn_id for e in r.rec.of("turn_ended")] == []
    assert [e.turn_id for e in r.rec.of("turn_cancelled")] == [turn_a]
    start_b = r.rec.of("turn_started")[-1]
    assert start_b.new_page is True and start_b.page_id != page_a   # a new lesson never draws over the old one

    await asyncio.sleep(0.15)                       # let B's producer finish
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()
    assert r.mgr.state.conv_state == ConvState.IDLE
    assert [e.turn_id for e in r.rec.of("turn_ended")] == [turn_b]
    statuses = {t.id: t.scene_artifacts["status"] for t in await _turns_in_db()}
    assert statuses == {turn_a: "partial", turn_b: "complete"}


# --------------------------------------------------------------------------- 3
@pytest.mark.asyncio
async def test_backchannel_while_speaking_keeps_playing(monkeypatch):
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    await _say_to_tutor(r, "hmm okay yes", "backchannel")
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING
    assert h.interrupted is False and r.rec.of("replay_from_step") == []


# --------------------------------------------------------------------------- 4
@pytest.mark.asyncio
async def test_speech_finishing_during_classification_does_not_wedge(monkeypatch):
    """Regression: SPEECH_ENDED arriving in INTERRUPT_* must not be dropped; the machine
    would otherwise return to AGENT_SPEAKING with nothing playing and never leave it."""
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.VAD_START)
    h.release.set()
    await settle()
    assert r.mgr.state.conv_state == ConvState.INTERRUPT_DETECTED
    r.labels.append("backchannel")
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="okay okay sure")
    await settle()
    assert r.mgr.state.conv_state == ConvState.IDLE
    assert len(r.rec.of("turn_ended")) == 1


# --------------------------------------------------------------------------- 5
@pytest.mark.asyncio
async def test_livekit_cut_then_backchannel_replays_from_unacked_step(monkeypatch):
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    _ack(r, 0)
    _ack(r, 1)
    await r.mgr.handle_event(ConvEvent.VAD_START)
    h.interrupt()                                   # LiveKit barge-in (>= min_words), not us
    await settle()
    r.labels.append("backchannel")
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="yes yes I see")
    await settle()
    replay = r.rec.of("replay_from_step")
    assert [e.step_index for e in replay] == [2]
    h2 = r.session.latest()
    assert h2 is not h and "".join(h2.spoken).startswith("Lesson step 2.")
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING
    h2.release.set()
    await settle()
    assert r.mgr.state.conv_state == ConvState.IDLE


# --------------------------------------------------------------------------- 6
@pytest.mark.asyncio
async def test_voice_doubt_same_board_then_got_it_resumes(monkeypatch):
    from app.config import settings
    # This cell exercises the regeneration resume path; the parked-activation path is
    # covered by test_resume_parked.py, so pin the flag off regardless of the environment.
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", False)
    r = _rig(monkeypatch)
    await _start_lesson(r)
    lesson_page = r.mgr.state.page.page_id
    lesson_turn = r.mgr.state.active_turn_id

    t0 = time.monotonic()
    await _say_to_tutor(r, "why is AB five", "doubt")
    assert time.monotonic() - t0 < 1.0             # settles on handle-done, not a flat 2.5 s
    st = r.mgr.state
    assert st.conv_state == ConvState.TASK_CORRECTING
    assert st.paused_lesson.page_id == lesson_page and st.paused_lesson.lesson_turn_id == lesson_turn
    assert st.paused_lesson.lesson_completed is False
    doubt_start = r.rec.of("turn_started")[-1]
    assert doubt_start.kind == "doubt" and doubt_start.new_page is False and doubt_start.page_id == lesson_page

    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert st.doubt_awaiting_resolution is True
    r.session.latest().release.set()
    await settle()
    assert st.conv_state == ConvState.IDLE
    assert r.rec.of("conv_state")[-1].can_continue_lesson is True

    await _say_to_tutor(r, "got it thanks", "affirmation")
    assert st.conv_state == ConvState.RESUMING
    resume_start = r.rec.of("turn_started")[-1]
    assert resume_start.kind == "resume" and resume_start.page_id == lesson_page
    assert r.rec.of("page_restore") == []           # same page: nothing to restore
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()
    assert st.conv_state == ConvState.IDLE and st.paused_lesson is None
    kinds = [t.scene_artifacts["kind"] for t in await _turns_in_db()]
    assert kinds == ["lesson", "doubt", "resume"]   # the doubt turn has its own kind


# --------------------------------------------------------------------------- 7
@pytest.mark.asyncio
async def test_doubt_on_doubt_keeps_the_original_lesson_snapshot(monkeypatch):
    r = _rig(monkeypatch)
    await _start_lesson(r)
    _ack(r, 0)
    _ack(r, 1)                                      # lesson progress the snapshot must keep
    await _say_to_tutor(r, "why is AB five", "doubt")
    snapshot = r.mgr.state.paused_lesson
    assert snapshot.last_acked_step_index == 1
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    mark = DoubtMark(gesture="circle", target_kind="work", row_id="w1", text="row 0")
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="and this?", marks=[mark])
    await settle()
    assert r.mgr.state.paused_lesson == snapshot     # not overwritten with the doubt's progress
    assert r.mgr.state.paused_lesson.last_acked_step_index == 1
    assert r.mgr.state.pending_request.question == snapshot.lesson_question


# --------------------------------------------------------------------------- 8
@pytest.mark.asyncio
async def test_new_figure_doubt_gets_fresh_page_and_resume_restores_lesson_page(monkeypatch):
    r = _rig(monkeypatch)
    await _start_lesson(r)
    lesson_page = r.mgr.state.page.page_id
    _ack(r, 0)
    acked_op = r.mgr.state.steps_sent[0].ops[0].op_id
    r.fig["new"] = True
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="what if angle B were obtuse?", marks=[])
    await settle()
    start = r.rec.of("turn_started")[-1]
    assert start.kind == "doubt" and start.new_page is True and start.page_id != lesson_page
    assert r.mgr.state.pending_request.namespace == "d1_"

    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    restore = r.rec.of("board_snapshot")
    assert len(restore) == 1 and restore[0].current.page_id == lesson_page
    assert [o.op_id for o in restore[0].current.sub_pages[-1].ops] == [acked_op]   # from the ledger
    assert r.mgr.state.page.page_id == lesson_page   # resume teaches on the LESSON page
    assert r.mgr.state.conv_state == ConvState.RESUMING


# --------------------------------------------------------------------------- 9
@pytest.mark.asyncio
async def test_affirmation_after_doubt_on_completed_lesson_goes_idle(monkeypatch):
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    h.release.set()
    await settle()
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why 13?", marks=[])
    await settle()
    assert r.mgr.state.paused_lesson.lesson_completed is True
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()
    assert r.rec.of("conv_state")[-1].can_continue_lesson is False
    await _say_to_tutor(r, "okay got it", "affirmation")
    assert r.mgr.state.conv_state == ConvState.IDLE


# --------------------------------------------------------------------------- 10
@pytest.mark.asyncio
async def test_off_topic_redirect_then_replay_same_generation(monkeypatch):
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    gen = r.mgr.state.generation
    _ack(r, 0)
    await _say_to_tutor(r, "who won the cricket match yesterday", "off_topic")
    assert r.mgr.state.conv_state == ConvState.TASK_REDIRECTED
    assert h.interrupted is True
    template = r.session.latest()
    assert "focus" in template.source or "part of" in template.source or "stay with" in template.source
    template.release.set()
    await settle()
    assert [e.step_index for e in r.rec.of("replay_from_step")] == [1]
    assert r.mgr.state.generation == gen            # no supersede
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING


# --------------------------------------------------------------------------- 10b
@pytest.mark.asyncio
async def test_redirect_emits_aside_pair(monkeypatch):
    """The redirect template is bracketed by aside{start}/aside{end}."""
    r = _rig(monkeypatch)
    await _start_lesson(r)
    await _say_to_tutor(r, "who won the cricket match yesterday", "off_topic")

    assert [(e.phase, e.kind) for e in r.rec.of("aside")] == [("start", "redirect")]
    template = r.session.latest()
    assert "focus" in template.source or "part of" in template.source or "stay with" in template.source

    template.release.set()
    await settle()
    assert [(e.phase, e.kind) for e in r.rec.of("aside")] == [
        ("start", "redirect"), ("end", "redirect"),
    ]


# --------------------------------------------------------------------------- 11
@pytest.mark.asyncio
async def test_marker_pause_does_not_truncate_the_lesson(monkeypatch):
    """Regression: a pause must not cancel the graph inside llm_node, or the replay can only
    re-speak the steps produced so far and a paused 6-step lesson ends after step 2."""
    six = [f"Part {i}." for i in range(6)]
    r = _rig(monkeypatch, scripts={"lesson": six}, delay=0.02)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="long lesson")
    await asyncio.sleep(0.05)
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    first = r.session.latest()
    await r.mgr.handle_event(ConvEvent.MARKER_ARMED)
    assert first.interrupted is True and r.mgr.state.conv_state == ConvState.AGENT_SPEAKING
    await asyncio.sleep(0.2)                        # producer keeps generating while paused
    await settle()
    assert len(r.mgr.state.steps_sent) == 6
    assert r.rec.of("turn_ended") == []             # a pause never ends the turn
    await r.mgr.handle_event(ConvEvent.MARKER_DISARMED)
    await settle()
    resumed = r.session.latest()
    assert "Part 5." in "".join(resumed.spoken)
    resumed.release.set()
    await settle()
    assert r.mgr.state.conv_state == ConvState.IDLE and len(r.rec.of("turn_ended")) == 1


# --------------------------------------------------------------------------- 11b
@pytest.mark.asyncio
async def test_marker_replays_same_step(monkeypatch):
    """No ack is sent for the interrupted step, so the server replays it, not the next."""
    r = _rig(monkeypatch)
    await _start_lesson(r)
    _ack(r, 0)
    _ack(r, 1)
    await r.mgr.handle_event(ConvEvent.MARKER_ARMED)
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING
    await r.mgr.handle_event(ConvEvent.MARKER_DISARMED)
    await settle()
    replay = r.rec.of("replay_from_step")
    assert replay and replay[-1].step_index == 2


# --------------------------------------------------------------------------- 12
@pytest.mark.asyncio
async def test_end_session_persists_partial_says_goodbye_and_disconnects(monkeypatch):
    r = _rig(monkeypatch)
    disconnected = asyncio.Event()

    class Room:
        async def disconnect(self):
            disconnected.set()

    r.mgr.room = Room()
    await _start_lesson(r)
    await _say_to_tutor(r, "stop the class please", "end_session")
    assert r.mgr.state.conv_state == ConvState.TASK_CANCELLED
    goodbye = r.session.latest()
    assert goodbye.source == GOODBYE_LINE and not disconnected.is_set()
    goodbye.release.set()
    await settle()
    assert disconnected.is_set()
    assert (await _turns_in_db())[0].scene_artifacts["status"] == "partial"
    assert await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="more") == ConvState.TASK_CANCELLED


# --------------------------------------------------------------------------- 13
@pytest.mark.asyncio
async def test_concurrent_events_are_serialized(monkeypatch):
    r = _rig(monkeypatch)
    await _start_lesson(r)
    gen, turn_a = r.mgr.state.generation, r.mgr.state.active_turn_id
    r.mgr.fire(ConvEvent.TYPED_QUESTION, text="next question")
    r.mgr.fire(ConvEvent.SPEECH_ENDED, generation=gen, turn_id=turn_a)
    await settle()
    assert r.mgr.state.conv_state == ConvState.GRAPH_RUNNING
    assert r.mgr.state.active_turn_id != turn_a
    assert all(e.turn_id == turn_a for e in r.rec.of("turn_ended"))


# --------------------------------------------------------------------------- 14
@pytest.mark.asyncio
async def test_stale_step_ack_is_ignored_and_fresh_ack_feeds_page_cache(monkeypatch):
    r = _rig(monkeypatch)
    await _start_lesson(r)
    st = r.mgr.state
    r.mgr.on_step_ack(StepAck(turn_id=st.active_turn_id, generation=st.generation - 1, step_index=3,
                              drawn_op_ids=["x"]))
    assert st.last_acked_step_index == -1
    _ack(r, 0)
    assert st.last_acked_step_index == 0
    acked = [lo.op.op_id for sub in r.mgr.ledger.pages[st.page.page_id] for lo in sub.ops if lo.acked]
    assert acked == [st.steps_sent[0].ops[0].op_id]


# --------------------------------------------------------------------------- 15
@pytest.mark.asyncio
async def test_producer_failure_speaks_fallback_and_returns_to_idle(monkeypatch):
    r = _rig(monkeypatch, fail=True)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="anything")
    await settle()
    h = r.session.latest()
    assert "".join(h.spoken) == FALLBACK_LINE
    assert len(r.rec.of("notice")) == 1
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    h.release.set()
    await settle()
    assert r.mgr.state.conv_state == ConvState.IDLE


# --------------------------------------------------------------------------- 16
@pytest.mark.asyncio
async def test_resume_with_no_steps_does_not_wedge_in_resuming(monkeypatch):
    from app.config import settings
    # Regeneration path (empty resume script); parked activation is covered in
    # test_resume_parked.py.
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", False)
    r = _rig(monkeypatch, scripts={"lesson": LESSON, "doubt": ["d"], "resume": []})
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
    await settle()
    r.session.latest().release.set()
    await settle()
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    assert r.mgr.state.conv_state == ConvState.RESUMING
    r.session.latest().release.set()
    await settle()
    assert r.mgr.state.conv_state == ConvState.IDLE


# --------------------------------------------------------------------------- 17
@pytest.mark.asyncio
async def test_doubt_settle_is_generation_bound(monkeypatch):
    r = _rig(monkeypatch)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why?", marks=[])
    assert r.mgr.state.conv_state == ConvState.TASK_PAUSED
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="new question instead")
    await settle()
    assert [e.kind for e in r.rec.of("turn_started")] == ["lesson", "lesson"]
    assert r.mgr.state.pending_request.kind == "lesson"


# --------------------------------------------------------------------------- 18
@pytest.mark.asyncio
async def test_interrupt_transcript_keeps_word_boundaries(monkeypatch):
    r = _rig(monkeypatch)

    async def slow_classify(**kw):
        await asyncio.sleep(0.05)
        return "backchannel"

    monkeypatch.setattr(mgr_mod, "classify_interrupt", slow_classify)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.VAD_START)
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="wait")
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="why is that")
    assert r.mgr.state.interrupt_transcript == "wait why is that"


# --------------------------------------------------------------------------- 19
@pytest.mark.asyncio
async def test_backchannel_replays_interrupted_step(monkeypatch):
    """The heard cursor, not the ack index, is what a backchannel replay resumes from."""
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    _ack(r, 0)                                      # step 0 heard
    await r.mgr.handle_event(ConvEvent.VAD_START)
    h.interrupt()                                   # LiveKit barge-in mid-step-1
    await settle()
    r.labels.append("backchannel")
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="yes yes I see")
    await settle()
    assert [e.step_index for e in r.rec.of("replay_from_step")] == [1]
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING


# --------------------------------------------------------------------------- 20
@pytest.mark.asyncio
async def test_hold_reasons_independent(monkeypatch):
    """Marker and user_pause are independent holds; the last release replays."""
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    st = r.mgr.state

    await r.mgr.handle_event(ConvEvent.MARKER_ARMED, reason="marker")
    await r.mgr.handle_event(ConvEvent.MARKER_ARMED, reason="user_pause")
    assert st.holds == {"marker", "user_pause"}
    assert h.interrupted is True

    await r.mgr.handle_event(ConvEvent.MARKER_DISARMED, reason="marker")
    assert st.holds == {"user_pause"}
    assert r.rec.of("replay_from_step") == [], "a remaining hold keeps the turn paused"

    await r.mgr.handle_event(ConvEvent.MARKER_DISARMED, reason="user_pause")
    assert st.holds == set() and st.marker_armed is False
    assert [e.step_index for e in r.rec.of("replay_from_step")] == [0]


@pytest.mark.asyncio
async def test_hold_reason_audio_blocked(monkeypatch):
    r = _rig(monkeypatch)
    await _start_lesson(r)
    st = r.mgr.state

    await r.mgr.handle_event(ConvEvent.MARKER_ARMED, reason="audio_blocked")
    await r.mgr.handle_event(ConvEvent.MARKER_ARMED, reason="audio_blocked")   # idempotent
    assert st.holds == {"audio_blocked"} and st.marker_armed is True

    await r.mgr.handle_event(ConvEvent.MARKER_DISARMED, reason="audio_blocked")
    assert st.holds == set() and st.marker_armed is False
    assert [e.step_index for e in r.rec.of("replay_from_step")] == [0]
