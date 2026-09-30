# backend/tests/test_phase0_regressions.py
"""Regression tests: interrupt threshold, persistence, RPCs and turn KPIs."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, settings
from tests.fakes import settle


def _rpc_handlers(manager):
    from app.transport import register_rpcs

    handlers: dict = {}

    class FakeParticipant:
        def register_rpc_method(self, name, fn):
            handlers[name] = fn

    register_rpcs(FakeParticipant(), manager)
    return handlers


def test_min_words_default_three():
    assert Settings().INTERRUPT_MIN_WORDS == 3


def test_min_words_clamped():
    assert Settings(INTERRUPT_MIN_WORDS=1).INTERRUPT_MIN_WORDS == 3


def test_min_words_escape_hatch():
    assert Settings(INTERRUPT_MIN_WORDS=1, ALLOW_SUB3_INTERRUPT=True).INTERRUPT_MIN_WORDS == 1


def test_session_uses_min_words(monkeypatch):
    import app.voice.session as session_mod
    captured: dict = {}

    class FakeAgentSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(session_mod, "AgentSession", FakeAgentSession)
    monkeypatch.setattr(session_mod, "build_stt", lambda: None)
    monkeypatch.setattr(session_mod, "build_tts", lambda: None)
    monkeypatch.setattr(session_mod.settings, "INTERRUPT_MIN_WORDS", 3)
    session_mod.build_agent_session(SimpleNamespace(proc=SimpleNamespace(userdata={"vad": None})), None)
    assert captured["turn_handling"]["interruption"]["min_words"] == 3


def test_sqlite_fallback_url_absolute(monkeypatch):
    from app.persistence import db
    monkeypatch.setattr(db.settings, "SQLITE_FALLBACK_PATH", "")
    url = db.sqlite_fallback_url()
    path = url.split("sqlite+aiosqlite:///", 1)[1]
    assert Path(path).is_absolute()
    assert path.endswith("math_tutor.db")


@pytest.mark.asyncio
async def test_worker_calls_init_db(monkeypatch):
    import app.main as main_mod

    init_mock = AsyncMock()
    monkeypatch.setattr(main_mod, "init_db", init_mock)

    class FakeSession:
        def __init__(self):
            self.listeners = []

        def on(self, evt, cb):
            self.listeners.append((evt, cb))

    fake_session = FakeSession()

    async def fake_start_session(ctx, manager):
        return fake_session

    monkeypatch.setattr(main_mod, "start_session", fake_start_session)

    ctx = SimpleNamespace(
        connect=AsyncMock(),
        room=None,
        add_shutdown_callback=lambda f: None,
    )
    await main_mod.entrypoint(ctx)
    init_mock.assert_awaited_once()
    assert len(fake_session.listeners) == 3   # includes the session error listener


def _paused(completed: bool = False):
    from app.contracts.agent_state import PausedLesson
    return PausedLesson(
        board_id="b", page_id="p", lesson_question="q", turn_plan=None,
        solver_projection=None, diagram=None, figure_drawn=False,
        lesson_turn_id="t", last_acked_step_index=-1, lesson_completed=completed,
    )


@pytest.mark.asyncio
async def test_continue_rpc_reports_illegal():
    from app.contracts.agent_state import ConvState
    from app.state_machine.manager import StateMachineManager

    mgr = StateMachineManager(session=None)
    mgr.state.conv_state = ConvState.TASK_CORRECTING
    mgr.state.paused_lesson = _paused()
    handlers = _rpc_handlers(mgr)

    resp = await handlers["continue_lesson"](SimpleNamespace(payload="{}"))
    assert json.loads(resp)["ok"] is False
    assert json.loads(resp)["reason"] == "not_now"


@pytest.mark.asyncio
async def test_continue_rpc_nothing_to_continue():
    from app.state_machine.manager import StateMachineManager

    mgr = StateMachineManager(session=None)
    handlers = _rpc_handlers(mgr)

    resp = await handlers["continue_lesson"](SimpleNamespace(payload="{}"))
    assert json.loads(resp)["ok"] is False
    assert json.loads(resp)["reason"] == "nothing_to_continue"


@pytest.mark.asyncio
async def test_history_messages_are_dicts():
    from app.agents.nodes.teaching import stream_teaching_turn
    from app.contracts.agent_state import ConversationTurn, TurnRequest
    from app.tutor.board_rows import BoardRowTracker

    captured: dict = {}

    class FakeGateway:
        async def stream_text(self, *, prompt_key, messages, model, timeout_s):
            captured["messages"] = messages
            return
            yield  # pragma: no cover

    req = TurnRequest(kind="lesson", generation=1, turn_id="t1", question="find AC")
    history = [
        ConversationTurn(role="student", content="q"),
        ConversationTurn(role="tutor", content="a"),
    ]
    out = []
    async for item in stream_teaching_turn(req, None, None, BoardRowTracker(), history=history, gw=FakeGateway()):
        out.append(item)

    msgs = captured["messages"]
    assert all(isinstance(m, dict) for m in msgs)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1] == {"role": "user", "content": "q"}
    assert msgs[2] == {"role": "assistant", "content": "a"}


@pytest.mark.asyncio
async def test_history_appended_on_complete(monkeypatch):
    from tests.test_lifecycle_scenarios import _ack, _rig, _start_lesson

    # History internals with FEATURE_MEMORY off (the memory path is covered by
    # test_memory.py::test_history_uses_heard_steps).
    monkeypatch.setattr(settings, "FEATURE_MEMORY", False)
    r = _rig(monkeypatch)
    h = await _start_lesson(r)
    _ack(r, 0)
    _ack(r, 1)
    h.release.set()
    await settle()

    history = r.mgr.state.history
    assert [t.role for t in history] == ["student", "tutor"]
    assert history[0].content == "In triangle ABC find AC"
    assert "Lesson step 0." in history[1].content and "Lesson step 1." in history[1].content
    assert "Lesson step 2." not in history[1].content


@pytest.mark.asyncio
async def test_doubt_text_persisted(monkeypatch):
    from sqlalchemy import select

    import app.persistence.repo as repo
    from app.persistence.models import Turn
    from app.state_machine.states import ConvEvent
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why is BC 12?", marks=[])
    await settle()
    r.session.latest().release.set()
    await settle()

    async with repo.async_session() as s:
        turns = list((await s.execute(select(Turn).order_by(Turn.order_index))).scalars().all())
    doubts = [t for t in turns if t.scene_artifacts and t.scene_artifacts.get("kind") == "doubt"]
    assert doubts and doubts[-1].question == "why is BC 12?"


@pytest.mark.asyncio
async def test_set_speed_unsupported_reason():
    from app.state_machine.manager import StateMachineManager

    mgr = StateMachineManager(session=None)
    handlers = _rpc_handlers(mgr)

    resp = await handlers["set_speed"](SimpleNamespace(payload='{"speed": 1.2}'))
    assert json.loads(resp)["ok"] is False
    assert json.loads(resp)["reason"] == "speed_unsupported"


@pytest.mark.asyncio
async def test_midstream_failure_is_partial(monkeypatch):
    import app.agents.graph as graph_mod
    from app.state_machine.manager import PARTIAL_PAUSE_LINE
    from app.state_machine.states import ConvEvent
    from tests.fakes import scripted_producer
    from tests.test_lifecycle_scenarios import _rig, _start_lesson, _turns_in_db

    r = _rig(monkeypatch)
    monkeypatch.setattr(
        graph_mod, "iter_turn_steps",
        scripted_producer({"lesson": ["step one.", "step two.", "step three."]}, fail_after=2),
    )
    h = await _start_lesson(r)
    await settle()

    spoken = "".join(h.spoken)
    assert "step one." in spoken and "step two." in spoken
    assert PARTIAL_PAUSE_LINE in spoken
    assert "step three." not in spoken

    h.release.set()
    await settle()
    ended = r.rec.of("turn_ended")
    assert ended and ended[-1].status == "partial"
    assert r.mgr.can_continue()
    turns = await _turns_in_db()
    assert turns[-1].scene_artifacts["status"] == "partial"


@pytest.mark.asyncio
async def test_failure_before_steps_keeps_checkpoint(monkeypatch):
    import app.agents.graph as graph_mod
    from app.state_machine.states import ConvEvent
    from tests.fakes import scripted_producer
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    # Regeneration path (the failing resume producer); parked activation is covered by
    # test_resume_parked.py::test_resume_fallback_when_run_failed.
    from app.config import settings
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", False)
    r = _rig(monkeypatch)
    await _start_lesson(r)
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why is BC 12?", marks=[])
    await settle()
    checkpoint = r.mgr.state.paused_lesson
    assert checkpoint is not None

    # The doubt answers normally; only the RESUME producer fails before any step.
    r.session.latest().release.set()
    await settle()
    monkeypatch.setattr(
        graph_mod, "iter_turn_steps",
        scripted_producer({"resume": ["should not be spoken"]}, fail_after=0),
    )
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    r.session.latest().release.set()
    await settle()

    assert r.mgr.state.paused_lesson == checkpoint         # untouched by a failed resume
    assert r.mgr.can_continue()
    assert r.rec.of("turn_ended")[-1].status == "partial"


@pytest.mark.asyncio
async def test_new_lesson_zero_step_failure_no_checkpoint(monkeypatch):
    import app.agents.graph as graph_mod
    from app.state_machine.manager import FALLBACK_LINE
    from app.state_machine.states import ConvEvent
    from tests.fakes import scripted_producer
    from tests.test_lifecycle_scenarios import _rig

    r = _rig(monkeypatch)
    monkeypatch.setattr(
        graph_mod, "iter_turn_steps",
        scripted_producer({"lesson": ["never spoken"]}, fail_after=0),
    )
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="anything")
    await settle()
    h = r.session.latest()
    assert "".join(h.spoken).strip() == FALLBACK_LINE

    h.release.set()
    await settle()
    assert r.mgr.state.paused_lesson is None
    assert not r.mgr.can_continue()
    ended = r.rec.of("turn_ended")
    assert ended and ended[-1].status == "partial"
    assert len(r.rec.of("notice")) == 1


def test_deliberate_pruned():
    from app.state_machine.manager import StateMachineManager

    mgr = StateMachineManager(session=None)
    mgr._speech_seq = 200
    for tok in range(100, 201):
        mgr._deliberate.add(tok)

    mgr._interrupt_speech("test")

    assert all(tok >= mgr._speech_seq - 50 for tok in mgr._deliberate)
    assert 100 not in mgr._deliberate
    assert 200 in mgr._deliberate


@pytest.mark.asyncio
async def test_stream_watchdog_idle(monkeypatch):
    from app.agents.nodes.teaching import stream_teaching_turn
    from app.contracts.agent_state import TurnRequest
    from app.gateway.groq_client import GatewayStreamError
    from app.tutor.board_rows import BoardRowTracker

    class SleepingGateway:
        async def stream_text(self, *, prompt_key, messages, model, timeout_s):
            await asyncio.sleep(5)
            yield "never"

    monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(settings, "STREAM_TOTAL_TIMEOUT_S", 5.0)
    req = TurnRequest(kind="lesson", generation=1, turn_id="t1", question="q")
    with pytest.raises(GatewayStreamError):
        async for _ in stream_teaching_turn(req, None, None, BoardRowTracker(), gw=SleepingGateway()):
            pass


def test_stale_board_report_ignored():
    """A report from another sub-page must not overwrite the current page's rows."""
    from app.contracts.agent_state import PageRecord
    from app.contracts.messages import BoardReport, BoardRow
    from app.state_machine.manager import StateMachineManager

    mgr = StateMachineManager(session=None)
    mgr.state.page = PageRecord(board_id="b", page_id="root", lesson_question="q")
    mgr.ledger.current_sub("root")                 # the student sees "root"

    mgr.on_board_report(BoardReport(page_id="root_p2", rows=[BoardRow(row_id="w9", text="stale")],
                                    rows_remaining=4))

    assert mgr.state.rows is None
    assert mgr.row_tracker.visible_rows == []


@pytest.mark.asyncio
async def test_turn_kpis_computed(monkeypatch):
    """Turn end logs the KPI block."""
    from app.contracts.messages import StepProgress
    from app.state_machine.states import ConvEvent
    from tests.fakes import settle
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    logged: list[tuple] = []
    import app.state_machine.manager as mgr_mod
    monkeypatch.setattr(mgr_mod, "log_event", lambda name, **kw: logged.append((name, kw)))
    r = _rig(monkeypatch)
    await _start_lesson(r)
    st = r.mgr.state

    for idx, by in ((0, "words"), (1, "stall"), (2, "flush")):
        step = st.steps_sent[idx]
        r.mgr.on_step_progress(StepProgress(
            turn_id=st.active_turn_id, generation=st.generation, step_index=idx,
            event="completed", completed_by=by,
            started_up_to=idx if by != "flush" else idx - 1,
            heard_up_to=idx if by != "flush" else idx - 1, early_ops=1,
            drawn_op_ids=[op.op_id for op in step.ops]))

    r.session.latest().release.set()
    await settle()

    kpis = [kw for name, kw in logged if name == "turn_kpis"]
    assert kpis, "turn end must log turn_kpis"
    kpi = kpis[-1]
    assert kpi["steps"] == 4 and kpi["heardSteps"] == 2
    assert kpi["wordMatchRatio"] == 0.333 and kpi["stallRatio"] == 0.333 and kpi["flushRatio"] == 0.333
    assert kpi["earlyOps"] == 3 and kpi["audioMode"] == "voice"
    assert any(name == "turn_mark" and kw.get("mark") == "turn_end" for name, kw in logged)
