# backend/tests/test_resume_parked.py
"""Run/park/resume tests. Offline: no LLM, no LiveKit, no sockets."""
import json

import pytest

import app.agents.graph as graph_mod
import app.state_machine.lesson_runner as runner_mod
import app.state_machine.manager as mgr_mod
from app.config import settings
from app.prompts.registry import RESUME_BRIDGE_LINE
from tests import fakes
from app.agents.graph import iter_turn_steps
from app.agents.graph_state import RunContext
from app.contracts.agent_state import AgentState, ConvState, PageRecord, TurnRequest
from app.contracts.diagram import VerifiedDiagram
from app.state_machine.lesson_runner import PageRun
from app.state_machine.manager import StateMachineManager
from app.state_machine.states import ConvEvent
from app.state_machine.turn_stream import TurnStream
from app.tutor.board_rows import BoardRowTracker
from tests.fakes import EventRecorder, make_step, scripted_producer, settle


@pytest.mark.asyncio
async def test_run_ctx_cancel_stops_stream():
    req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="q")
    cancel = {"v": False}
    ctx = RunContext(run_id="r1", page_record=None, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: cancel["v"])
    seen: list[int] = []

    class FakeGraph:
        async def astream(self, state, stream_mode="custom"):
            assert state["run_ctx"] is ctx
            yield {"step": make_step("t1", 0, 0, "one"), "tts_text": "one"}
            cancel["v"] = True
            yield {"step": make_step("t1", 0, 1, "two"), "tts_text": "two"}

    async for step, _tts in iter_turn_steps(req, graph=FakeGraph(), run_ctx=ctx):
        seen.append(step.step_index)
    assert seen == [0]


@pytest.mark.asyncio
async def test_manager_publishes_commit_before_steps(monkeypatch):
    from tests.test_lifecycle_scenarios import _rig

    # FEATURE_PAGE_COMMIT replaces diagram_commit with page_commit; this test pins the
    # legacy wire order. The flag-on order is pinned by
    # test_chapter_flow.py::test_page_commit_before_first_step.
    monkeypatch.setattr(settings, "FEATURE_PAGE_COMMIT", False)
    r = _rig(monkeypatch)
    diagram = VerifiedDiagram(name="tri", commands=[], anchors=[], reveals=[], prompt_addon="")

    async def producer(request, graph=None, row_tracker=None, agent_state=None, history=None,
                       stored_turns=None, gw=None, run_ctx=None, **kw):
        if agent_state is not None and agent_state.page is not None:
            agent_state.page.diagram = diagram
        yield make_step(request.turn_id, request.generation, 0, "Look at the triangle now."), "Look at the triangle now."

    monkeypatch.setattr(graph_mod, "iter_turn_steps", producer)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="In triangle ABC find AC")
    await settle()

    types = [e.type for e in r.rec.events]
    assert types.index("turn_started") < types.index("diagram_commit") < types.index("step")
    assert len(r.rec.of("diagram_commit")) == 1
    assert len(r.rec.of("step")) == 1
    assert r.mgr.state.steps_sent[0].spoken_text == "Look at the triangle now."


@pytest.mark.asyncio
async def test_parked_run_does_not_touch_active_page():
    from tests.test_geometry_pipeline_offline import MockGateway

    plan_raw = json.dumps({
        "question": "Solve 2x + 3 = 11", "givens": [], "unknowns": [{"id": "x", "symbol": "x"}],
        "derived": [{"id": "x", "symbol": "x", "value": 4, "sourceText": "(11 - 3) / 2 = 4"}],
        "visualRequirement": "none",
    })
    gw = MockGateway({"TurnPlan": [plan_raw], "ProblemIR": []})
    turn_id = "t_parked"
    lesson_page = PageRecord(board_id="b1", page_id="lessonpage", lesson_question="Find x",
                             turn_kind="lesson", turn_id=turn_id)
    st = AgentState(session_id="s1", user_id="u1", board_id="b1")
    st.page = PageRecord(board_id="b1", page_id="doubtpage", lesson_question="Why?",
                         turn_kind="doubt", turn_id="t_doubt")
    req = TurnRequest(kind="lesson", generation=0, turn_id=turn_id, question="Solve 2x + 3 = 11")
    ctx = RunContext(run_id="r1", page_record=lesson_page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)

    async for _step, _tts in iter_turn_steps(req, agent_state=st, gw=gw, run_ctx=ctx):
        pass

    assert lesson_page.turn_plan is not None, "the run's plan lands on the run's page record"
    assert st.page.turn_plan is None and st.page.diagram is None, "the active page is untouched"
    assert st.lesson_topic, "the active run still records its topic for classifiers/redirects"


def _publisher_rig(monkeypatch):
    rec = EventRecorder()
    monkeypatch.setattr(runner_mod, "send_event", rec)
    mgr = StateMachineManager(session=None)
    return mgr, mgr.lesson_runner.publisher, rec


def _make_run(req: TurnRequest, texts: list[str], production: str = "running") -> PageRun:
    page = PageRecord(board_id="b", page_id="page1", lesson_question=req.question)
    ctx = RunContext(run_id="r1", page_record=page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)
    run = PageRun(run_id="r1", kind=req.kind, lesson_id="L1", page_index=0, page_id="page1",
                  origin_turn_id=req.turn_id, stream=TurnStream(req), ctx=ctx)
    for i, text in enumerate(texts):
        run.stream.push(make_step(req.turn_id, req.generation, i, text, write=f"row {i}"), text)
    run.stream.finish()
    run.production = production
    return run


@pytest.mark.asyncio
async def test_publisher_reindexes_keeps_op_ids(monkeypatch):
    mgr, publisher, rec = _publisher_rig(monkeypatch)
    req = TurnRequest(kind="lesson", generation=1, turn_id="t_old", question="q")
    run = _make_run(req, ["five", "six"], production="done")

    await publisher.activate(run, 1, "t_new", 9)

    steps = [e.step for e in rec.of("step")]
    assert [(s.turn_id, s.generation, s.step_index, s.source_step_index) for s in steps] == [("t_new", 9, 0, 1)]
    assert steps[0].ops[0].op_id == "t_old:1:0", "op ids never change on replay/resume"
    assert steps[0].is_last is True
    assert mgr.state.steps_sent == steps
    assert mgr.state.published_upto == 0


@pytest.mark.asyncio
async def test_parked_mode_publishes_nothing(monkeypatch):
    _mgr, publisher, rec = _publisher_rig(monkeypatch)
    req = TurnRequest(kind="lesson", generation=1, turn_id="t_old", question="q")
    run = _make_run(req, ["one", "two"])
    run.stream = TurnStream(req)
    run.stream.push(make_step("t_old", 1, 0, "one"), "one")
    publisher.park(run)

    await publisher.on_new_item(run)
    run.stream.push(make_step("t_old", 1, 1, "two"), "two")
    await publisher.on_new_item(run)

    assert rec.of("step") == []


@pytest.mark.asyncio
async def test_discarded_run_silent(monkeypatch):
    rec = EventRecorder()
    monkeypatch.setattr(runner_mod, "send_event", rec)
    monkeypatch.setattr(graph_mod, "iter_turn_steps",
                        scripted_producer({"lesson": ["never"]}, delay=0.01))
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="lesson", generation=1, turn_id="t_discard", question="q")
    page = PageRecord(board_id="b", page_id="page1", lesson_question="q")

    run = mgr.lesson_runner.new_run(req, page)
    mgr.lesson_runner.active = run
    mgr.lesson_runner.discard_run(run.run_id)
    await settle()

    assert run.mode == "discarded"
    assert rec.of("step") == []
    assert mgr.state.steps_sent == []


@pytest.mark.asyncio
async def test_is_last_on_republished_done_run(monkeypatch):
    _mgr, publisher, rec = _publisher_rig(monkeypatch)
    req = TurnRequest(kind="resume", generation=2, turn_id="t_src", question="q")
    run = _make_run(req, ["one", "two", "three"], production="done")

    await publisher.activate(run, 0, "t_resume", 5)

    assert [e.step.is_last for e in rec.of("step")] == [False, False, True]


def _progress_rig():
    """A manager with one active run, without starting a producer."""
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="q")
    run = _make_run(req, ["one", "two", "three"])
    mgr.lesson_runner.active = run
    mgr.state.active_turn_id = "t1"
    mgr.state.active_turn_kind = "lesson"
    return mgr, run


@pytest.mark.asyncio
async def test_flush_completed_not_heard(monkeypatch):
    from app.contracts.messages import StepProgress

    mgr, run = _progress_rig()
    mgr.on_step_progress(StepProgress(turn_id="t1", generation=0, step_index=0, event="completed",
                                      completed_by="flush", started_up_to=0, heard_up_to=-1))

    assert run.heard_upto == -1, "a flush completion is not heard"
    assert mgr.state.heard_step_index == 0, "started still moves the classifier cursor"


@pytest.mark.asyncio
async def test_stall_counts_as_heard(monkeypatch):
    from app.contracts.messages import StepProgress

    mgr, run = _progress_rig()
    mgr.on_step_progress(StepProgress(turn_id="t1", generation=0, step_index=0, event="completed",
                                      completed_by="stall", started_up_to=0, heard_up_to=0))

    assert run.heard_upto == 0
    assert mgr.state.last_acked_step_index == 0


@pytest.mark.asyncio
async def test_resume_rows_from_ledger_not_report(monkeypatch):
    """The doubt/resume prompt rows come from the server ledger, not from client reports."""
    from app.contracts.messages import BoardReport, BoardRow
    from tests.test_lifecycle_scenarios import _ack, _rig, _start_lesson

    r = _rig(monkeypatch)
    await _start_lesson(r)
    st = r.mgr.state
    _ack(r, 0)                                      # ledger now holds step 0's WRITE row

    tracker = lambda: r.mgr.lesson_runner.active.ctx.row_tracker  # noqa: E731

    stale = BoardReport(page_id="other_page", rows=[BoardRow(row_id="w9", text="stale")], rows_remaining=5)
    r.mgr.on_board_report(stale)                    # a foreign sub-page report is ignored
    assert tracker().visible_rows == []

    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, typed_text="why row 0?", marks=[])
    await settle()
    assert [row["row_id"] for row in tracker().visible_rows] == ["w1"]

    r.session.latest().release.set()
    await settle()
    r.mgr.on_board_report(stale)
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    assert [row["row_id"] for row in tracker().visible_rows] == ["w1"]
    assert [row["text"] for row in tracker().visible_rows] == ["row 0"]


def test_row_trackers_isolated():
    """Each run parses with its own tracker; the allocator keeps ids unique board-wide."""
    from app.tutor.board_rows import BoardRowTracker, RowIdAllocator
    from app.tutor.stream_parser import StreamParser

    alloc = RowIdAllocator()
    t1 = BoardRowTracker(allocator=alloc)
    t2 = BoardRowTracker(allocator=alloc)
    t1.record_board_report("p1", [{"row_id": "w1", "text": "old row"}], 10)
    t2.record_board_report("p2", [{"row_id": "w1", "text": "other page row"}], 10)

    p1 = StreamParser(turn_id="t1", generation=1, turn_kind="lesson", row_tracker=t1, diagram=None)
    p2 = StreamParser(turn_id="t2", generation=1, turn_kind="lesson", row_tracker=t2, diagram=None)
    steps1 = [s for s, _tts in p1.append(
        "[STEP]Look at the board now. [WRITE:a = 1] [PAGE_BREAK:Part 2] [WRITE:b = 2][/STEP]")]
    steps2 = [s for s, _tts in p2.append("[STEP]Look at the board now. [WRITE:c = 3][/STEP]")]

    row_ids = [op.row_id for st in steps1 + steps2 for op in st.ops if op.kind == "WRITE"]
    assert len(row_ids) == len(set(row_ids)) == 3, "ids are unique across concurrent runs"

    # t1's page break reset its own tracker only.
    assert t1.visible_rows == []
    assert [r["row_id"] for r in t2.visible_rows] == ["w1"]
    assert [r["text"] for r in t2.visible_rows] == ["other page row"]


@pytest.mark.asyncio
async def test_parked_run_survives_generation_bump(monkeypatch):
    """A parked lesson run keeps generating across the doubt's generation bump."""
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(runner_mod, "send_event", EventRecorder())
    monkeypatch.setattr(graph_mod, "iter_turn_steps",
                        scripted_producer({"lesson": ["one.", "two.", "three."]}, delay=0.01))
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="lesson", generation=0, turn_id="t_lesson", question="q")
    page = PageRecord(board_id="b", page_id="page1", lesson_question="q")
    mgr.state.page = page
    mgr.state.lesson_id = "L1"
    mgr.state.active_turn_id = "t_lesson"
    mgr.state.active_turn_kind = "lesson"
    run = mgr.lesson_runner.new_run(req, page)
    mgr.lesson_runner.active = run

    mgr.lesson_runner.park_active()
    mgr.state.generation += 1                    # the doubt turn bumps the generation
    await settle()

    assert run.mode == "parked"
    assert run.production == "done"
    assert len(run.stream.items) == 3


@pytest.mark.asyncio
async def test_park_evicts_oldest(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(runner_mod, "send_event", EventRecorder())
    monkeypatch.setattr(graph_mod, "iter_turn_steps", scripted_producer({"lesson": ["one."]}, delay=0.05))
    mgr = StateMachineManager(session=None)

    runs = []
    for i in range(3):
        req = TurnRequest(kind="lesson", generation=0, turn_id=f"t{i}", question="q")
        page = PageRecord(board_id="b", page_id=f"page{i}", lesson_question="q")
        run = mgr.lesson_runner.new_run(req, page)
        mgr.lesson_runner.active = run
        mgr.lesson_runner.park_active()
        runs.append(run)

    parked = [r for r in mgr.lesson_runner.runs.values() if r.mode == "parked"]
    assert len(parked) == settings.MAX_PARKED_RUNS
    assert runs[0].mode == "discarded"
    assert runs[1].mode == "parked" and runs[2].mode == "parked"


@pytest.mark.asyncio
async def test_ack_on_parked_ignored(monkeypatch):
    from app.contracts.messages import StepProgress

    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(runner_mod, "send_event", EventRecorder())
    monkeypatch.setattr(graph_mod, "iter_turn_steps", scripted_producer({"lesson": ["one."]}, delay=0.05))
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="lesson", generation=0, turn_id="t_lesson", question="q")
    page = PageRecord(board_id="b", page_id="page1", lesson_question="q")
    run = mgr.lesson_runner.new_run(req, page)
    mgr.lesson_runner.active = run
    mgr.lesson_runner.park_active()
    mgr.lesson_runner.active = None
    mgr.state.active_turn_id = "t_doubt"          # the doubt turn is active now

    mgr.on_step_progress(StepProgress(turn_id="t_lesson", generation=0, step_index=3,
                                      event="completed", completed_by="words",
                                      started_up_to=3, heard_up_to=3))

    assert run.heard_upto == -1, "a parked run never moves its cursor on a stale ack"
    assert mgr.state.heard_step_index == -1


# ---------------------------------------------------------------------------------------------
# Resume by activation

def _parked_rig(monkeypatch, lesson_steps: int = 6, fail_after: int | None = None):
    """The lifecycle rig with FEATURE_PARKED_RESUME on and producer call counting."""
    from tests.test_lifecycle_scenarios import _rig

    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    scripts = {"lesson": [f"Lesson step {i}." for i in range(lesson_steps)],
               "doubt": ["Doubt answer."], "resume": ["regenerated resume"]}
    r = _rig(monkeypatch, scripts=scripts, delay=0.01)
    calls: dict[str, int] = {}
    inner = scripted_producer(scripts, delay=0.01, fail_after=fail_after)

    async def counting(request, **kw):
        calls[request.kind] = calls.get(request.kind, 0) + 1
        async for item in inner(request, **kw):
            yield item

    monkeypatch.setattr(graph_mod, "iter_turn_steps", counting)
    return r, calls


def _feed_progress(mgr, completed: list[int], started: int | None = None):
    from app.contracts.messages import StepProgress

    st = mgr.state
    for idx in completed:
        step = st.steps_sent[idx]
        mgr.on_step_progress(StepProgress(
            turn_id=st.active_turn_id, generation=st.generation, step_index=idx,
            event="completed", completed_by="words", started_up_to=idx, heard_up_to=idx,
            drawn_op_ids=[op.op_id for op in step.ops]))
    if started is not None:
        mgr.on_step_progress(StepProgress(
            turn_id=st.active_turn_id, generation=st.generation, step_index=started,
            event="started", started_up_to=started, heard_up_to=(completed[-1] if completed else -1)))


@pytest.mark.asyncio
async def test_resume_replays_parked_stream_from_cursor(monkeypatch):
    from tests.test_lifecycle_scenarios import _say_to_tutor, _start_lesson

    r, calls = _parked_rig(monkeypatch)
    await _start_lesson(r, "Teach me triangles")
    st = r.mgr.state
    lesson_turn = st.active_turn_id
    _feed_progress(r.mgr, completed=[0, 1], started=2)

    await _say_to_tutor(r, "why is that", "doubt")
    assert st.paused_lesson.run_id is not None
    assert st.paused_lesson.resume_cursor == 2
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)   # the doubt answer starts
    r.session.latest().release.set()                     # doubt answer finishes
    await settle()
    assert st.conv_state == ConvState.IDLE

    before = len(r.rec.of("step"))
    await _say_to_tutor(r, "got it thanks", "affirmation")
    assert st.conv_state == ConvState.RESUMING
    started = r.rec.of("turn_started")[-1]
    assert started.kind == "resume" and started.source_run_id == st.paused_lesson.run_id

    published = [e.step for e in r.rec.of("step")[before:]]
    assert [s.step_index for s in published] == [0, 1, 2, 3]
    assert [s.source_step_index for s in published] == [2, 3, 4, 5]
    assert published[0].ops[0].op_id.startswith(f"{lesson_turn}:2:"), "op ids never change"
    assert calls == {"lesson": 1, "doubt": 1}, "resume must not spawn a producer"


@pytest.mark.asyncio
async def test_three_doubts_then_resume_exact_cursor(monkeypatch):
    from tests.test_lifecycle_scenarios import _say_to_tutor, _start_lesson

    r, calls = _parked_rig(monkeypatch)
    await _start_lesson(r, "Teach me triangles")
    st = r.mgr.state
    _feed_progress(r.mgr, completed=[0, 1], started=2)

    for k in range(3):
        await _say_to_tutor(r, f"why number {k}", "doubt")
        assert st.paused_lesson.resume_cursor == 2
        await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
        r.session.latest().release.set()             # doubt answer finishes
        await settle()
        assert st.conv_state == ConvState.IDLE
        await _say_to_tutor(r, "got it", "affirmation")
        assert st.conv_state == ConvState.RESUMING
        # The resume bridge is still playing: the next doubt interrupts it.
        assert st.paused_lesson.resume_cursor == 2

    session = r.session
    session.latest().release.set()                   # bridge ends -> step speech starts
    await settle()
    session.latest().release.set()                   # resumed speech finishes
    await settle()

    assert st.conv_state == ConvState.IDLE and st.paused_lesson is None
    assert [e.step.source_step_index for e in r.rec.of("step")[-4:]] == [2, 3, 4, 5]
    assert calls == {"lesson": 1, "doubt": 3}


@pytest.mark.asyncio
async def test_continue_during_doubt_answer(monkeypatch):
    from tests.test_lifecycle_scenarios import _say_to_tutor, _start_lesson

    r, calls = _parked_rig(monkeypatch, lesson_steps=4)
    await _start_lesson(r)
    st = r.mgr.state
    _feed_progress(r.mgr, completed=[0, 1])

    await _say_to_tutor(r, "why?", "doubt")
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    assert st.conv_state == ConvState.AGENT_SPEAKING
    assert not r.session.latest().done()             # the doubt answer is still playing

    before = len(r.rec.of("step"))
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    assert st.conv_state == ConvState.RESUMING
    assert [e.step.source_step_index for e in r.rec.of("step")[before:]] == [2, 3]
    assert r.session.latest().source == RESUME_BRIDGE_LINE
    spoken = " ".join("".join(h.spoken) for h in r.session.handles)
    assert "regenerated resume" not in spoken, "no producer ran for the resume"


@pytest.mark.asyncio
async def test_resume_speech_starts_after_bridge(monkeypatch):
    from tests.test_lifecycle_scenarios import _say_to_tutor, _start_lesson

    r, _calls = _parked_rig(monkeypatch)
    await _start_lesson(r)
    _feed_progress(r.mgr, completed=[0, 1], started=2)
    await _say_to_tutor(r, "why?", "doubt")
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()
    await _say_to_tutor(r, "got it", "affirmation")

    bridge = r.session.latest()
    assert bridge.source == RESUME_BRIDGE_LINE
    assert r.rec.of("aside")[-1].phase == "start"
    handles_before = len(r.session.handles)

    bridge.release.set()
    await settle()

    assert r.rec.of("aside")[-1].phase == "end"
    assert len(r.session.handles) == handles_before + 1
    assert "".join(r.session.latest().spoken).startswith("Lesson step 2.")


@pytest.mark.asyncio
async def test_bridge_superseded_does_not_start_speech(monkeypatch):
    from tests.test_lifecycle_scenarios import _say_to_tutor, _start_lesson

    r, _calls = _parked_rig(monkeypatch)
    await _start_lesson(r)
    _feed_progress(r.mgr, completed=[0, 1], started=2)
    await _say_to_tutor(r, "why?", "doubt")
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()
    await _say_to_tutor(r, "got it", "affirmation")
    assert r.session.latest().source == RESUME_BRIDGE_LINE

    n_before = len(r.session.handles)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="new question instead")
    await settle()
    n_mid = len(r.session.handles)
    assert n_mid == n_before + 1                     # the new lesson's own speech only

    for h in list(r.session.handles):
        h.release.set()
    await settle()
    spoken = ["".join(h.spoken) for h in r.session.handles[n_mid:]]
    assert not any(s.startswith("Lesson step 2.") for s in spoken)


@pytest.mark.asyncio
async def test_resume_fallback_when_run_failed(monkeypatch):
    """A failed parked run with nothing left regenerates a new resume run."""
    r, calls = _parked_rig(monkeypatch, lesson_steps=6, fail_after=2)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me")
    await settle()
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    st = r.mgr.state
    _feed_progress(r.mgr, completed=[0, 1])
    r.session.latest().release.set()
    await settle()
    assert st.conv_state == ConvState.IDLE

    failed_run = r.mgr.lesson_runner.run_for(st.paused_lesson.run_id)
    assert failed_run.production == "failed" and len(failed_run.stream.items) == 2

    before = len(r.rec.of("step"))
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    assert st.conv_state == ConvState.RESUMING
    started = r.rec.of("turn_started")[-1]
    assert started.kind == "resume" and not started.source_run_id
    assert [e.step.source_step_index for e in r.rec.of("step")[before:]] == [0]
    assert calls.get("resume", 0) == 1

    new_run = r.mgr.lesson_runner.active
    assert new_run.run_id.endswith(":r1") and new_run.run_id != failed_run.run_id


@pytest.mark.asyncio
async def test_resume_fallback_when_evicted(monkeypatch):
    """An evicted (discarded) parked run falls back to regeneration, not activation."""
    from tests.test_lifecycle_scenarios import _say_to_tutor, _start_lesson

    r, _calls = _parked_rig(monkeypatch)
    await _start_lesson(r)
    st = r.mgr.state
    _feed_progress(r.mgr, completed=[0, 1], started=2)
    await _say_to_tutor(r, "why?", "doubt")           # parks the lesson run
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()
    lesson_run_id = st.paused_lesson.run_id

    # Park two more runs: with MAX_PARKED_RUNS=2 the lesson run is evicted.
    for i in range(2):
        req = TurnRequest(kind="lesson", generation=st.generation, turn_id=f"rk{i:04d}", question="q")
        page = PageRecord(board_id="b", page_id=f"page_extra{i}", lesson_question="q")
        extra = r.mgr.lesson_runner.new_run(req, page)
        r.mgr.lesson_runner.active = extra
        r.mgr.lesson_runner.park_active()
        r.mgr.lesson_runner.active = None
    assert r.mgr.lesson_runner.run_for(lesson_run_id).mode == "discarded"

    before = len(r.rec.of("step"))
    await _say_to_tutor(r, "got it", "affirmation")
    assert st.conv_state == ConvState.RESUMING
    started = r.rec.of("turn_started")[-1]
    assert started.kind == "resume" and not started.source_run_id
    assert [e.step.source_step_index for e in r.rec.of("step")[before:]] == [0]
    active = r.mgr.lesson_runner.active
    assert active.run_id.endswith(":r1") and active.run_id != lesson_run_id


@pytest.mark.asyncio
async def test_resume_nothing_left_completes_lesson(monkeypatch):
    """Every step heard and the parked run done: resume completes the lesson, no new turn."""
    from tests.test_lifecycle_scenarios import _say_to_tutor, _start_lesson

    r, calls = _parked_rig(monkeypatch, lesson_steps=4)
    await _start_lesson(r)
    st = r.mgr.state
    _feed_progress(r.mgr, completed=[0, 1, 2, 3])
    await _say_to_tutor(r, "why?", "doubt")
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    r.session.latest().release.set()
    await settle()

    before = len(r.rec.of("turn_started"))
    await _say_to_tutor(r, "got it", "affirmation")

    assert st.conv_state == ConvState.IDLE
    assert st.paused_lesson.lesson_completed is True
    assert not r.mgr.can_continue()
    assert [e.kind for e in r.rec.of("turn_started")[before:]] == []
    assert calls.get("resume", 0) == 0


def test_history_uses_heard_not_started(monkeypatch):
    """History uses the contiguous heard cursor, not started_up_to."""
    from app.contracts.messages import StepProgress

    # The second half asserts history internals with FEATURE_MEMORY off (flag-on equivalent:
    # test_memory.py::test_history_uses_heard_steps).
    monkeypatch.setattr(settings, "FEATURE_MEMORY", False)
    mgr, run = _progress_rig()
    mgr.on_step_progress(StepProgress(
        turn_id="t1", generation=0, step_index=0, event="completed", completed_by="words",
        started_up_to=1, heard_up_to=0))

    heard = mgr._heard_steps()
    assert [s.spoken_text for s in heard] == ["one"], "step 1 was only started, not heard"

    req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="q", student_text="why?")
    mgr._close_history(req, heard)
    assert [(t.role, t.content) for t in mgr.state.history] == [
        ("student", "why?"), ("tutor", "one"),
    ]
