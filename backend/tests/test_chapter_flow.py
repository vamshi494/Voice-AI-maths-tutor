# backend/tests/test_chapter_flow.py
"""Scene join budget, reference figures and the board-prep line. Offline."""
import asyncio
import time

import pytest

import app.agents.graph as graph_mod
import app.state_machine.lesson_runner as runner_mod
from app.agents.graph import scene_plan_compile_node
from app.agents.graph_state import RunContext
from app.config import settings
from app.contracts.agent_state import AgentState, PageRecord, TurnRequest
from app.contracts.diagram import VerifiedDiagram
from app.prompts.registry import BOARD_PREP_LINE
from app.state_machine.manager import StateMachineManager
from app.tutor.board_rows import BoardRowTracker
from tests.fakes import EventRecorder, scripted_producer, settle


def _diagram() -> VerifiedDiagram:
    return VerifiedDiagram(name="tri", commands=[], anchors=[], reveals=[], prompt_addon="")


def _page() -> PageRecord:
    return PageRecord(board_id="b", page_id="p1", lesson_question="q")


def _run_rig(monkeypatch, delay=0.03, scripts=None):
    rec = EventRecorder()
    monkeypatch.setattr(runner_mod, "send_event", rec)
    monkeypatch.setattr(graph_mod, "iter_turn_steps",
                        scripted_producer(scripts or {"lesson": ["one.", "two."]}, delay=delay))
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="q")
    page = _page()
    run = mgr.lesson_runner.new_run(req, page)
    mgr.lesson_runner.active = run
    mgr.state.active_turn_id = "t1"
    mgr.state.active_turn_kind = "lesson"
    mgr.state.page = page
    return mgr, run, page, rec


@pytest.mark.asyncio
async def test_scene_join_budget_starts_text_only(monkeypatch):
    async def slow_scene(question, plan, namespace="", gw=None, **kw):
        await asyncio.sleep(5)
        return None, None, None, "text_only"

    monkeypatch.setattr(graph_mod, "plan_and_compile_scene", slow_scene)
    monkeypatch.setattr(settings, "SCENE_JOIN_BUDGET_S", 0.05)

    st = AgentState(session_id="s", user_id="u", board_id="b")
    req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="q")
    ctx = RunContext(run_id="r1", page_record=_page(), row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)

    out = await scene_plan_compile_node(
        {"request": req, "plan": None, "gw": None, "agent_state": st, "run_ctx": ctx})

    assert out["visual_status"] == "text_only" and out["diagram"] is None
    assert out["late_scene"] is ctx.late_scene
    out["late_scene"].cancel()


@pytest.mark.asyncio
async def test_late_scene_committed_as_reference(monkeypatch):
    # The legacy diagram_commit reference event; under FEATURE_PAGE_COMMIT the same
    # commit_figure branch sends a page_commit with reference=True (flag-on path pinned by
    # test_late_scene_committed_as_page_commit_reference).
    monkeypatch.setattr(settings, "FEATURE_PAGE_COMMIT", False)
    mgr, run, page, rec = _run_rig(monkeypatch)
    diagram = _diagram()

    async def late():
        await asyncio.sleep(0.01)
        return None, None, diagram, "validated"

    run.ctx.late_scene = asyncio.create_task(late())
    await settle()

    assert page.diagram is diagram
    commits = rec.of("diagram_commit")
    assert commits and commits[-1].reference is True


@pytest.mark.asyncio
async def test_late_scene_committed_as_page_commit_reference(monkeypatch):
    """With FEATURE_PAGE_COMMIT on, a late scene is committed as a page_commit with
    reference=True (the client reveals everything at stage time)."""
    monkeypatch.setattr(settings, "FEATURE_PAGE_COMMIT", True)
    mgr, run, page, rec = _run_rig(monkeypatch)
    diagram = _diagram()

    async def late():
        await asyncio.sleep(0.01)
        return None, None, diagram, "validated"

    run.ctx.late_scene = asyncio.create_task(late())
    await settle()

    assert page.diagram is diagram
    commits = rec.of("page_commit")
    assert commits, "the late figure commits as a page_commit"
    assert commits[-1].reference is True
    assert not rec.of("diagram_commit")


@pytest.mark.asyncio
async def test_late_scene_cancelled_with_run(monkeypatch):
    mgr, run, _page, _rec = _run_rig(monkeypatch, delay=5.0)

    async def late():
        await asyncio.sleep(30)

    task = asyncio.create_task(late())
    run.ctx.late_scene = task
    mgr.lesson_runner.discard_run(run.run_id)
    await settle()

    assert task.cancelled()


@pytest.mark.asyncio
async def test_scene_failure_text_only_notice(monkeypatch):
    _mgr, _run, page, rec = _run_rig(monkeypatch)
    page.visual_status = "retry_required"

    await settle()

    notices = [n for n in rec.of("notice") if "describe the figure" in n.message]
    assert len(notices) == 1, "the student is told once that the figure comes in words"


@pytest.mark.asyncio
async def test_page_commit_before_first_step(monkeypatch):
    """With FEATURE_PAGE_COMMIT the publisher sends a one-block page_commit
    before the first step; with the flag off the legacy diagram_commit keeps the same spot."""
    from app.contracts.messages import Rect

    def rig():
        rec = EventRecorder()
        monkeypatch.setattr(runner_mod, "send_event", rec)
        monkeypatch.setattr(graph_mod, "iter_turn_steps",
                            scripted_producer({"lesson": ["one.", "two."]}, delay=0.02))
        mgr = StateMachineManager(session=None)
        req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="q")
        page = _page()
        page.diagram = _diagram()           # set before the producer's first yield
        run = mgr.lesson_runner.new_run(req, page)
        mgr.lesson_runner.active = run
        mgr.state.active_turn_id = "t1"
        mgr.state.active_turn_kind = "lesson"
        mgr.state.page = page
        return rec

    monkeypatch.setattr(settings, "FEATURE_PAGE_COMMIT", True)
    rec = rig()
    await settle()
    commits = rec.of("page_commit")
    steps = rec.of("step")
    assert commits and steps, "the figure commit must precede the first step"
    assert rec.events.index(commits[0]) < rec.events.index(steps[0])
    assert not rec.of("diagram_commit"), "the flag replaces the legacy event"
    pc = commits[0]
    assert pc.page_id == "p1" and pc.commit_id == "c_p1_t1"
    assert pc.work_rect == Rect(x=40, y=72, width=340, height=608)
    assert len(pc.blocks) == 1 and pc.blocks[0].role == "figure"
    assert pc.blocks[0].rect == Rect(x=420, y=40, width=720, height=600)

    monkeypatch.setattr(settings, "FEATURE_PAGE_COMMIT", False)
    rec = rig()
    await settle()
    assert rec.of("diagram_commit"), "flag off = legacy diagram_commit"
    assert not rec.of("page_commit")


@pytest.mark.asyncio
async def test_board_prep_line_once(monkeypatch):
    from tests.test_lifecycle_scenarios import _rig, _start_lesson

    monkeypatch.setattr(settings, "BOARD_PREP_LINE_AFTER_MS", 50)
    r = _rig(monkeypatch, delay=0.25)
    h = await _start_lesson(r)
    await settle()
    r.session.latest().release.set()
    await settle()

    assert "".join(h.spoken).count(BOARD_PREP_LINE) == 1


# ---------------------------------------------------------------------------------------------
# The outline gate, the outline call and the chapter switch
# ---------------------------------------------------------------------------------------------
from app.agents.nodes.outline import wants_outline                      # noqa: E402
from app.contracts.lesson import LessonPlan                             # noqa: E402
from app.state_machine.states import ConvEvent                          # noqa: E402
from tests.test_lifecycle_scenarios import _rig                         # noqa: E402


def _lesson_plan(pages: int = 2, scope: str = "topic") -> LessonPlan:
    raw = {
        "scope": scope,
        "title": "Basic Proportionality Theorem",
        "pages": [
            {
                "title": f"Page {i}",
                "objective": f"objective {i}",
                "keyPoints": ["ratio", "parallel"],
                "numericTask": None,
                "blocks": [{"id": f"fig{i}", "role": "figure", "brief": "triangle ABC with DE || BC"}]
                if i == 0 else [],
                "stepBudget": 8,
            }
            for i in range(pages)
        ],
    }
    return LessonPlan.model_validate(raw)


def _recording_producer(seen: list):
    """Records every TurnRequest the graph would run; slow enough that the outline wins."""
    async def _iter(request, graph=None, row_tracker=None, agent_state=None, history=None,
                    stored_turns=None, gw=None, run_ctx=None, **kw):
        seen.append(request)
        await asyncio.sleep(0.2)
        return
        yield  # pragma: no cover
    return _iter


@pytest.mark.asyncio
async def test_topic_request_builds_outline(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    plan = _lesson_plan(2)
    calls: list[str] = []

    async def fake_outline(question, memory_summary, gw=None):
        calls.append(question)
        return plan

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(manager_mod, "outline_lesson", fake_outline)

    r = _rig(monkeypatch)
    seen: list = []
    monkeypatch.setattr(graph_mod, "iter_turn_steps", _recording_producer(seen))

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION,
                             text="Teach me Basic Proportionality Theorem", intent="topic")
    await settle()

    assert calls == ["Teach me Basic Proportionality Theorem"]
    assert r.mgr.lesson_runner.chapter_plan is plan
    assert r.mgr.lesson_runner.lesson_id == r.mgr.state.lesson_id
    # request 1 = the provisional single-page lesson; request 2 = chapter page 0
    assert len(seen) == 2
    page_req = seen[1]
    assert page_req.lesson_id == r.mgr.state.lesson_id
    assert page_req.page_index == 0 and page_req.page_count == 2
    assert page_req.page_plan is plan.pages[0]
    assert page_req.intent == "topic"
    starts = r.rec.of("turn_started")
    assert starts and starts[-1].page_index == 0 and starts[-1].new_page is True
    assert r.rec.of("turn_cancelled"), "the provisional lesson is superseded"
    assert r.mgr.state.published_upto < 0, "nothing of the provisional lesson is published"
    # the chapter plan is in the active run path: page 0 is the active run
    assert r.mgr.state.page_index == 0


@pytest.mark.asyncio
async def test_outline_failure_falls_back_to_problem(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)

    async def failing_outline(question, memory_summary, gw=None):
        raise RuntimeError("outline down")

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(manager_mod, "outline_lesson", failing_outline)

    r = _rig(monkeypatch, delay=0.02)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle()

    assert r.mgr.lesson_runner.chapter_plan is None
    assert r.mgr.state.active_turn_kind == "lesson"
    assert r.rec.of("step"), "the single-page lesson still teaches"
    assert not r.rec.of("turn_cancelled"), "no provisional lesson was cut"


@pytest.mark.asyncio
async def test_outline_stale_after_supersede_ignored(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    plan = _lesson_plan(2)

    async def slow_outline(question, memory_summary, gw=None):
        await asyncio.sleep(0.3)
        return plan

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(manager_mod, "outline_lesson", slow_outline)

    r = _rig(monkeypatch, delay=0.01)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle(cycles=3)
    # A new question arrives before the outline resolves: the outline must die with the lesson.
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Solve 2x + 3 = 11", intent="new")
    await settle()

    assert r.mgr.lesson_runner.chapter_plan is None
    assert r.mgr.state.active_turn_id is not None
    starts = [s for s in r.rec.of("turn_started") if s.kind == "lesson"]
    assert len(starts) == 2, "both questions started a lesson"


@pytest.mark.asyncio
async def test_outline_late_ignored_when_lesson_started(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    plan = _lesson_plan(2)

    async def slow_outline(question, memory_summary, gw=None):
        await asyncio.sleep(0.25)
        return plan

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(manager_mod, "outline_lesson", slow_outline)

    r = _rig(monkeypatch, delay=0.01)      # producer publishes steps before the outline lands
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle()

    assert r.mgr.lesson_runner.chapter_plan is None
    assert r.rec.of("step"), "the fast lesson taught its steps"
    assert len([s for s in r.rec.of("turn_started") if s.kind == "lesson"]) == 1


def test_outline_page_clamp():
    plan = LessonPlan.model_validate({
        "scope": "topic", "title": "Big",
        "pages": [{
            "title": f"p{i}", "objective": "o", "keyPoints": list("abcdef"),
            "numericTask": "", "stepBudget": 99,
            "blocks": [
                {"id": f"b{j}", "role": "sketch" if j == 0 else "figure", "brief": "x", "sticky": j < 4}
                for j in range(5)
            ],
        } for i in range(9)],
    })
    assert len(plan.pages) == settings.MAX_CHAPTER_PAGES == 6
    assert all(len(p.blocks) == settings.PLANNER_BLOCK_BUDGET == 2 for p in plan.pages)
    assert all(len(p.key_points) == 5 for p in plan.pages)
    assert all(p.step_budget == settings.STEP_BUDGET_PAGE_MAX == 10 for p in plan.pages)
    assert sum(1 for p in plan.pages for b in p.blocks if b.sticky) == settings.MAX_STICKY_BLOCKS == 2
    assert plan.pages[0].blocks[0].role == "figure"       # unknown role coerced
    assert plan.pages[0].numeric_task is None


def test_topic_gate():
    assert wants_outline("Teach me Basic Proportionality Theorem", "auto") is True
    assert wants_outline("anything at all", "topic") is True
    assert wants_outline("Solve 2x + 3 = 11", "auto") is False
    assert wants_outline("explain why x = 5", "auto") is False
    assert wants_outline("what is the area of a circle", "auto") is True
    assert wants_outline("teach me 2x^2 - 5x + 3 = 0", "auto") is False
    assert wants_outline("", "auto") is False


@pytest.mark.asyncio
async def test_chapters_flag_off_no_outline(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", False)
    calls: list[str] = []

    async def fake_outline(question, memory_summary, gw=None):
        calls.append(question)
        return _lesson_plan(2)

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(manager_mod, "outline_lesson", fake_outline)

    r = _rig(monkeypatch, delay=0.01)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle()

    assert calls == []
    assert r.mgr.lesson_runner.chapter_plan is None
    assert r.rec.of("step")


# ---------------------------------------------------------------------------------------------
# The multi-page runner (prefetch, gap, advance, interrupts, last page)
# ---------------------------------------------------------------------------------------------
from app.contracts.agent_state import ConvState                            # noqa: E402
from app.contracts.messages import StepProgress  # noqa: E402
from app.prompts.registry import CHAPTER_STOP_LINE, PAGE_FILLER_LINES  # noqa: E402
from tests import fakes                                                 # noqa: E402
from tests.test_lifecycle_scenarios import _say_to_tutor                # noqa: E402


async def _start_chapter(r, plan=None, monkeypatch=None) -> None:
    """Drive a topic request through the real manager and stop at page 0 speaking."""
    plan = plan or _lesson_plan(2)
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)

    async def fake_outline(question, memory_summary, gw=None):
        return plan

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(manager_mod, "outline_lesson", fake_outline)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle()
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    await settle()


def _ack_started(r, idx=0):
    st = r.mgr.state
    r.mgr.on_step_progress(StepProgress(
        turn_id=st.active_turn_id, generation=st.generation, step_index=idx,
        event="started", started_up_to=idx, heard_up_to=-1))


def _op_ids(rec):
    return [op.op_id for e in rec.of("step") for op in e.step.ops]


@pytest.mark.asyncio
async def test_prefetch_parks_next_page(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 30)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    assert r.mgr.state.page.page_id == f"{r.mgr.state.lesson_id}_p0"

    _ack_started(r, 0)
    run1 = r.mgr.lesson_runner.runs.get(f"{r.mgr.state.lesson_id}:p1")
    assert run1 is not None and run1.mode == "parked"
    assert run1.page_id == f"{r.mgr.state.lesson_id}_p1"
    assert not [op for op in _op_ids(r.rec) if op.startswith(run1.origin_turn_id)], \
        "a parked run publishes nothing"

    _ack_started(r, 0)
    assert list(r.mgr.lesson_runner.runs).count(f"{r.mgr.state.lesson_id}:p1") == 1


@pytest.mark.asyncio
async def test_page_advance_uses_prefetched_stream(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 20)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    run1 = r.mgr.lesson_runner.runs.get(f"{r.mgr.state.lesson_id}:p1")
    assert run1 is not None and run1.mode == "parked"
    origin = run1.origin_turn_id

    r.session.latest().release.set()                    # page 0 speech ends -> rule 29
    await settle()

    starts = r.rec.of("turn_started")
    assert starts[-1].page_index == 1 and starts[-1].new_page is False
    assert starts[-1].page_title == "Page 1"
    ends = r.rec.of("turn_ended")
    assert ends[-1].page_only is True
    assert any(op.startswith(origin) for op in _op_ids(r.rec)), \
        "page 1 is published from the prefetched stream with its original op ids"
    assert r.mgr.state.page.page_id.endswith("_p1")
    assert r.mgr.lesson_runner.page_index == 1
    assert run1.mode == "active"


@pytest.mark.asyncio
async def test_page_advance_waits_with_filler(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01, hold=False)
    await _start_chapter(r, monkeypatch=monkeypatch)
    # The prefetched page 1 takes long to generate: no items while the gap runs out.
    run1 = r.mgr.lesson_runner.runs.get(f"{r.mgr.state.lesson_id}:p1")
    run1.stream.items.clear()
    run1.production = "running"

    r.session.latest().release.set()
    await settle()

    assert PAGE_FILLER_LINES[1] in r.session.said_text, "page 1 spoke the filler while generating"
    assert r.mgr.state.page.page_id.endswith("_p1"), "the page activated after the filler"


@pytest.mark.asyncio
async def test_interrupt_in_page_gap(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 5000)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)

    r.session.latest().release.set()
    await settle(cycles=6)
    assert r.mgr.lesson_runner.advance_pending is True
    task = r.mgr.lesson_runner.advance_task
    assert task is not None and not task.done()

    await r.mgr.handle_event(ConvEvent.VAD_START)      # rule 4 cancels the gap
    await settle(cycles=3)
    assert task.cancelled() or task.done(), "the barge-in cancelled the gap timer"
    assert r.mgr.state.conv_state == ConvState.INTERRUPT_DETECTED


@pytest.mark.asyncio
async def test_backchannel_in_gap_resumes_advance(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 5000)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle(cycles=6)
    await r.mgr.handle_event(ConvEvent.VAD_START)
    await settle(cycles=3)

    # A backchannel returns to where we were and re-spawns the page advance.
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    await _say_to_tutor(r, "hmm", "backchannel")
    await settle()

    starts = r.rec.of("turn_started")
    assert starts[-1].page_index == 1 and starts[-1].new_page is False
    assert r.mgr.lesson_runner.advance_pending is False


@pytest.mark.asyncio
async def test_gap_timer_runs_outside_lock(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 2000)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle(cycles=6)
    assert r.mgr.lesson_runner.advance_pending is True

    start = time.monotonic()
    await r.mgr.handle_event(ConvEvent.VAD_START)      # must not wait for the 2 s gap
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"VAD waited {elapsed:.3f}s on the gap timer"
    await settle(cycles=3)


@pytest.mark.asyncio
async def test_prefetch_discarded_with_doubt_when_not_parking(monkeypatch):
    """With FEATURE_PARKED_RESUME off the lesson cannot come back; its prefetched
    page must not keep generating in the dark."""
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", False)   # pins the flag-off branch
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    run1 = r.mgr.lesson_runner.runs[f"{r.mgr.state.lesson_id}:p1"]
    assert run1.mode == "parked"

    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, marks=[])
    await settle()
    assert run1.mode == "discarded"
    assert run1.task is not None and run1.task.cancelled() or run1.task.done()


@pytest.mark.asyncio
async def test_page_advance_new_page_false(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle()
    starts = r.rec.of("turn_started")
    assert starts[0].new_page is True and starts[0].page_index == 0
    assert starts[-1].new_page is False and starts[-1].page_index == 1
    assert starts[-1].page_title == "Page 1"
    assert r.mgr.state.has_next_page is False, "2-page chapter: page 1 is the last"


@pytest.mark.asyncio
async def test_last_page_completes_lesson(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle()
    r.session.latest().release.set()                    # finish page 1
    await settle()

    ends = r.rec.of("turn_ended")
    assert ends[-1].page_only is False and ends[-1].status == "complete"
    assert r.mgr.state.page.lesson_completed is True
    assert r.mgr.state.conv_state == ConvState.IDLE
    assert r.mgr.lesson_runner.advance_pending is False


@pytest.mark.asyncio
async def test_hold_at_page_transition_advances_once(monkeypatch):
    """A hold before SPEECH_ENDED stops the deliberate end; release replays, then the
    page advances exactly once."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    await r.mgr.handle_event(ConvEvent.MARKER_ARMED, reason="user_pause")
    await settle()
    assert r.mgr.lesson_runner.advance_pending is False, "held speech cannot end the page"

    await r.mgr.handle_event(ConvEvent.MARKER_DISARMED, reason="user_pause")
    await settle()
    r.session.latest().release.set()                    # the replay finishes -> rule 29
    await settle()
    assert r.mgr.state.page.page_id.endswith("_p1")
    page1_starts = [s for s in r.rec.of("turn_started") if s.page_index == 1]
    assert len(page1_starts) == 1


@pytest.mark.asyncio
async def test_prefetch_failure_regenerates_once(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.0, hold=False)
    page1_turns: list[str] = []

    async def flaky_producer(request, graph=None, row_tracker=None, agent_state=None,
                             history=None, stored_turns=None, gw=None, run_ctx=None, **kw):
        if request.page_index == 0:
            await asyncio.sleep(0.05)       # let the outline resolve before page 0 publishes
        if request.page_index == 1:
            page1_turns.append(request.turn_id)
            if len(page1_turns) == 1:
                raise RuntimeError("prefetch failed")
        yield fakes.make_step(request.turn_id, request.generation, 0, "Step.", write="r"), "Step."

    async def fake_outline(question, memory_summary, gw=None):
        return _lesson_plan(2)

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    monkeypatch.setattr(manager_mod, "outline_lesson", fake_outline)
    monkeypatch.setattr(graph_mod, "iter_turn_steps", flaky_producer)

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle()
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    await settle()
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle()

    assert len(page1_turns) == 2, "the failed prefetch is regenerated once"
    assert r.mgr.state.page.page_id.endswith("_p1")
    assert r.rec.of("turn_ended"), "the chapter kept going"


@pytest.mark.asyncio
async def test_chapter_stop_second_failure_checkpoint(monkeypatch):
    """A second prefetch failure stops the chapter with Continue offered, no dead page."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.0, hold=False)
    page1_turns: list[str] = []

    async def always_fail_producer(request, graph=None, row_tracker=None, agent_state=None,
                                   history=None, stored_turns=None, gw=None, run_ctx=None, **kw):
        if request.page_index == 0:
            await asyncio.sleep(0.05)       # let the outline resolve before page 0 publishes
        if request.page_index == 1:
            page1_turns.append(request.turn_id)
            raise RuntimeError("prefetch failed")
        yield fakes.make_step(request.turn_id, request.generation, 0, "Step.", write="r"), "Step."

    async def fake_outline(question, memory_summary, gw=None):
        return _lesson_plan(2)

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    monkeypatch.setattr(manager_mod, "outline_lesson", fake_outline)
    monkeypatch.setattr(graph_mod, "iter_turn_steps", always_fail_producer)

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle()
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    await settle()
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle()

    assert len(page1_turns) == 2
    pl = r.mgr.state.paused_lesson
    assert pl is not None and pl.page_index == 1 and pl.run_id is None
    assert pl.lesson_plan is not None
    assert CHAPTER_STOP_LINE in r.session.said_text
    assert r.mgr.can_continue() is True
    starts = r.rec.of("turn_started")
    assert all(s.page_index == 0 for s in starts), "page 1 never activated as a dead page"


# ---------------------------------------------------------------------------------------------
# The page pipeline (route "page", multi-block commit, teaching.page.v1)
# ---------------------------------------------------------------------------------------------
from app.agents.graph import page_prepare_node                                # noqa: E402
from app.contracts.lesson import PagePlan                                      # noqa: E402
from app.contracts.scene import SceneDocument                                  # noqa: E402
from app.scene_engine.compile import compile_scene_document                     # noqa: E402
from app.scene_engine.layout import FIGURE_REGION, WORK_RECT_DIAGRAM            # noqa: E402
from app.scene_engine.verified_diagram import build_verified_diagram            # noqa: E402
from tests.make_frontend_fixture import PLAN as FIXTURE_PLAN, SCENES            # noqa: E402


def _fixture_scene():
    scene, report = compile_scene_document(SceneDocument.model_validate(SCENES["triangle_and_circle"]),
                                           plan=FIXTURE_PLAN)
    assert report.valid, [e.message for e in report.errors]
    return scene, report


@pytest.mark.asyncio
async def test_page_pipeline_builds_commit(monkeypatch):
    monkeypatch.setattr(settings, "PLANNER_BLOCK_BUDGET", 4)     # this test needs all block roles
    scene, report = _fixture_scene()

    async def fake_scene(question, plan, namespace="", gw=None, **kw):
        return scene, report, build_verified_diagram(scene, plan=FIXTURE_PLAN, namespace=namespace), "validated"

    monkeypatch.setattr(graph_mod, "plan_and_compile_scene", fake_scene)

    page = _page()
    st = AgentState(session_id="s", user_id="u", board_id="b")
    st.page = page
    ctx = RunContext(run_id="L_x:p1", page_record=page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)
    page_plan = PagePlan.model_validate({
        "title": "Proof", "objective": "prove BPT", "keyPoints": ["ratio", "parallel"],
        "numericTask": None, "stepBudget": 8,
        "blocks": [
            {"id": "fig_a", "role": "figure", "brief": "triangle ABC with DE || BC"},
            {"id": "fig_b", "role": "figure", "brief": "circle with tangent"},
            {"id": "tbl", "role": "table", "text": "Side | Length\nAD | 1.5 cm\nDB | 3 cm"},
            {"id": "txt", "role": "text", "text": "AD/DB = AE/EC"},
        ],
    })
    req = TurnRequest(kind="lesson", generation=0, turn_id="tp", question="Proof",
                      lesson_id="L_x", page_index=0, page_count=2,
                      page_titles=["What BPT says", "Proof"], page_plan=page_plan, intent="topic")
    out = await page_prepare_node({"request": req, "gw": None, "agent_state": st, "run_ctx": ctx})

    commit = ctx.page_commit
    assert commit is not None and out["page_commit"] is commit
    assert [b.role for b in commit.blocks] == ["figure", "figure", "table", "text"]
    assert commit.work_rect is not None and commit.work_rect.width == WORK_RECT_DIAGRAM.width
    for block in commit.blocks:
        assert block.rect.width > 0 and block.rect.height > 0
        assert block.rect.x >= FIGURE_REGION.x - 0.5
        assert block.rect.x + block.rect.width <= FIGURE_REGION.x + FIGURE_REGION.width + 0.5
    assert commit.blocks[2].text_lines == ["Side | Length", "AD | 1.5 cm", "DB | 3 cm"]
    assert commit.blocks[3].text_lines == ["AD/DB = AE/EC"]
    assert page.diagram is not None and page.visual_status == "validated"
    assert page.figure_drawn is True


@pytest.mark.asyncio
async def test_page_pipeline_text_only_page(monkeypatch):
    """A page with no figure still commits its work rect and teaches."""
    page = _page()
    st = AgentState(session_id="s", user_id="u", board_id="b")
    st.page = page
    ctx = RunContext(run_id="L_x:p0", page_record=page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)
    page_plan = PagePlan.model_validate({"title": "Idea", "objective": "o", "keyPoints": ["k"],
                                         "blocks": [], "stepBudget": 8})
    req = TurnRequest(kind="lesson", generation=0, turn_id="tp2", question="Idea",
                      lesson_id="L_x", page_index=0, page_count=1, page_plan=page_plan)
    out = await page_prepare_node({"request": req, "gw": None, "agent_state": st, "run_ctx": ctx})
    assert out["page_commit"] is not None and out["page_commit"].blocks == []
    assert out["page_commit"].work_rect.width == 1100          # text-only work rect
    assert page.visual_status == "text_only"


@pytest.mark.asyncio
async def test_chapter_page_commit_before_first_step(monkeypatch):
    """The multi-block commit is the page commit; it is published before the first step."""
    from tests.make_frontend_fixture import _one_figure_commit

    rec = EventRecorder()
    monkeypatch.setattr(runner_mod, "send_event", rec)
    monkeypatch.setattr(graph_mod, "iter_turn_steps",
                        scripted_producer({"lesson": ["one.", "two."]}, delay=0.02))
    monkeypatch.setattr(settings, "FEATURE_PAGE_COMMIT", False)
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="Proof",
                      lesson_id="L_x", page_index=1, page_count=2)
    page = PageRecord(board_id="b", page_id="L_x_p1", lesson_question="BPT")
    run = mgr.lesson_runner.new_run(req, page)
    run.ctx.page_commit = _one_figure_commit()
    mgr.lesson_runner.active = run
    mgr.state.active_turn_id = "t1"
    mgr.state.active_turn_kind = "lesson"
    mgr.state.page = page
    await settle()

    commits = rec.of("page_commit")
    steps = rec.of("step")
    assert commits and steps
    assert rec.events.index(commits[0]) < rec.events.index(steps[0])
    assert not rec.of("diagram_commit"), "the chapter commit replaces the legacy event"
    assert len(commits[0].blocks) == 1
    assert commits[0].page_id == "L_x_p1"
    assert commits[0].commit_id.startswith("c_L_x_p1_")


# ---------------------------------------------------------------------------------------------
# Sticky carry (injection, ledger reveal state, never dropped, redeclaration)
# ---------------------------------------------------------------------------------------------
def _chapter_with_sticky(name: str = "fig_tri"):
    return LessonPlan.model_validate({
        "scope": "topic", "title": "Basic Proportionality Theorem",
        "pages": [
            {"title": "Page 0", "objective": "o", "keyPoints": ["k"], "stepBudget": 8,
             "blocks": [{"id": name, "role": "figure", "brief": "triangle ABC", "sticky": True}]},
            {"title": "Page 1", "objective": "o", "keyPoints": ["k"], "stepBudget": 8,
             "blocks": [{"id": "own_a", "role": "figure", "brief": "circle"},
                        {"id": "own_b", "role": "figure", "brief": "square"}]},
        ],
    })


@pytest.mark.asyncio
async def test_sticky_cannot_be_dropped(monkeypatch):
    monkeypatch.setattr(settings, "PLANNER_BLOCK_BUDGET", 4)
    scene, report = _fixture_scene()

    async def fake_scene(question, plan, namespace="", gw=None, **kw):
        return scene, report, build_verified_diagram(scene, plan=FIXTURE_PLAN, namespace=namespace), "validated"

    monkeypatch.setattr(graph_mod, "plan_and_compile_scene", fake_scene)
    plan = _chapter_with_sticky()
    mgr = StateMachineManager(session=None)
    mgr.lesson_runner.start_chapter(plan, "L_x")
    mgr.lesson_runner.scene_cache["fig_tri"] = (scene, "pg0_")

    carried, revealed = mgr.lesson_runner._carried_stickies(1)
    assert [b.id for b, _entry in carried] == ["fig_tri"]
    assert revealed == {"fig_tri": []}

    page = PageRecord(board_id="b", page_id="L_x_p1", lesson_question="BPT")
    ctx = RunContext(run_id="L_x:p1", page_record=page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)
    req = TurnRequest(kind="lesson", generation=0, turn_id="tp", question="Page 1",
                      lesson_id="L_x", page_index=1, page_count=2, page_plan=plan.pages[1],
                      page_titles=["Page 0", "Page 1"],
                      carried_stickies=carried, carried_revealed=revealed)
    out = await page_prepare_node({"request": req, "gw": None, "agent_state": None, "run_ctx": ctx})
    commit = out["page_commit"]
    assert [b.id for b in commit.blocks] == ["fig_tri", "own_a", "own_b"], \
        "the sticky is injected before the page's own blocks, never dropped"
    assert commit.carried_ids == ["fig_tri"]
    assert all(b.rect.width > 0 for b in commit.blocks)


def test_sticky_revealed_ids_from_ledger(monkeypatch):
    """Acked FOCUS on a reveal group travels to the carried block's revealedIds."""
    from app.contracts.board_ops import BoardOp, Step as StepModel

    scene, _report = _fixture_scene()
    plan = _chapter_with_sticky()
    mgr = StateMachineManager(session=None)
    mgr.lesson_runner.start_chapter(plan, "L_x")
    mgr.lesson_runner.scene_cache["fig_tri"] = (scene, "pg0_")
    diagram = build_verified_diagram(scene, plan=FIXTURE_PLAN, namespace="pg0_")
    target = diagram.reveals[0].target_id
    op = BoardOp(op_id="t0:0:0", kind="FOCUS", at_word=0, entity_id=target)
    mgr.ledger.on_published("L_x_p0", StepModel(turn_id="t0", generation=0, step_index=0,
                                                spoken_text="look", words=["look"], ops=[op]))

    _carried, revealed = mgr.lesson_runner._carried_stickies(1)
    assert revealed == {"fig_tri": []}, "unacked FOCUS does not count"

    mgr.ledger.on_acked(["t0:0:0"])
    _carried, revealed = mgr.lesson_runner._carried_stickies(1)
    assert revealed == {"fig_tri": [target]}


def test_sticky_redeclared_ignored_and_missing_degrades(monkeypatch):
    """A redeclared sticky is ignored (first wins); an uncompiled sticky is skipped."""
    raw = _chapter_with_sticky().model_dump(by_alias=True)
    raw["pages"][1]["blocks"].insert(0, {"id": "fig_tri", "role": "figure",
                                         "brief": "triangle again", "sticky": True})
    plan = LessonPlan.model_validate(raw)
    mgr = StateMachineManager(session=None)
    mgr.lesson_runner.start_chapter(plan, "L_x")
    assert plan.pages[1].blocks[0].sticky is False, "first declaration wins"

    # No compiled scene yet for the (single remaining) sticky: degrade, never crash.
    carried, revealed = mgr.lesson_runner._carried_stickies(1)
    assert carried == [] and revealed == {}


# ---------------------------------------------------------------------------------------------
# Chapter resume (checkpoint page_index, prefetched next page kept parked)
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_mid_chapter_keeps_prefetch(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, plan=_lesson_plan(3), monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    _ack_started(r, 0)
    p1 = r.mgr.lesson_runner.runs[f"{lesson_id}:p1"]
    r.session.latest().release.set()                 # page 0 ends -> page 1 activates
    await settle()
    assert r.mgr.state.page.page_id.endswith("_p1")
    _ack_started(r, 0)                               # page 1 starts -> prefetch page 2
    p2 = r.mgr.lesson_runner.runs[f"{lesson_id}:p2"]
    assert p2.mode == "parked"

    # A doubt on page 1 parks it; the prefetched page 2 stays parked.
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, marks=[])
    await settle()
    assert p1.mode == "parked" and p2.mode == "parked"
    pl = r.mgr.state.paused_lesson
    assert pl.page_index == 1 and pl.run_id == p1.run_id
    assert pl.lesson_plan is not None
    assert pl.completed_page_ids == [f"{lesson_id}_p0"]

    # Continue activates the parked page run (no regeneration).
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)   # the doubt answer starts speaking
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    assert r.mgr.state.page.page_id.endswith("_p1")
    assert p1.mode == "active"
    starts = r.rec.of("turn_started")
    assert starts[-1].page_index == 1 and starts[-1].new_page is False


@pytest.mark.asyncio
async def test_doubt_in_page_gap_targets_next_page(monkeypatch):
    """A doubt during the gap checkpoints page i+1 from its start; the prefetched
    run is kept parked and Continue activates it."""
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 5000)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, plan=_lesson_plan(2), monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    _ack_started(r, 0)
    p1 = r.mgr.lesson_runner.runs[f"{lesson_id}:p1"]
    r.session.latest().release.set()
    await settle(cycles=6)
    assert r.mgr.lesson_runner.advance_pending is True

    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, marks=[])
    await settle()
    pl = r.mgr.state.paused_lesson
    assert pl.page_index == 1 and pl.run_id == p1.run_id and pl.resume_cursor == 0
    assert p1.mode == "parked", "the prefetched next page is kept parked"

    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)   # the doubt answer starts speaking
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    assert r.mgr.state.page.page_id.endswith("_p1")
    assert p1.mode == "active"
    starts = r.rec.of("turn_started")
    assert starts[-1].page_index == 1


@pytest.mark.asyncio
async def test_resume_done_page_moves_to_next_page(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, plan=_lesson_plan(3), monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    _ack_started(r, 0)
    p1 = r.mgr.lesson_runner.runs[f"{lesson_id}:p1"]
    r.session.latest().release.set()
    await settle()
    assert r.mgr.state.page.page_id.endswith("_p1")
    # Every step of page 1 was heard: the checkpoint's run has nothing left.
    p1.heard_upto = len(p1.stream.items) - 1
    p1.production = "done"

    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, marks=[])
    await settle()
    pl = r.mgr.state.paused_lesson
    assert pl.page_index == 1

    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    assert r.mgr.state.page.page_id.endswith("_p2"), "resume moved to the next page"
    p2 = r.mgr.lesson_runner.runs[f"{lesson_id}:p2"]
    assert pl.run_id == p2.run_id


# ---------------------------------------------------------------------------------------------
# Task teardown on shutdown and end_session
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shutdown_mid_chapter_cancels_tasks_and_keeps_checkpoint(monkeypatch):
    """Shutdown cancels the gap task and prefetch producers, flushes before
    releasing the lease, and leaves a resumable checkpoint at the pending page."""
    import app.state_machine.manager as manager_mod

    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 5000)

    async def slow_summary(**kw):
        await asyncio.sleep(30)
        return None

    monkeypatch.setattr(manager_mod, "summarize", slow_summary)

    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, plan=_lesson_plan(3), monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    _ack_started(r, 0)
    p1 = r.mgr.lesson_runner.runs[f"{lesson_id}:p1"]
    r.session.latest().release.set()                     # page 0 ends -> gap starts
    await settle(cycles=6)
    assert r.mgr.lesson_runner.advance_pending is True
    gap_task = r.mgr.lesson_runner.advance_task
    assert gap_task is not None and not gap_task.done()

    order: list[str] = []

    async def fake_persist() -> None:
        order.append("persist")

    async def fake_release() -> None:
        order.append("release")

    monkeypatch.setattr(r.mgr, "_persist_board_state", fake_persist)
    monkeypatch.setattr(r.mgr, "_release_lease", fake_release)
    await r.mgr.shutdown()
    await settle(cycles=3)                               # let the cancellations land

    assert gap_task.cancelled() or gap_task.done(), "the page gap is cancelled on shutdown"
    assert p1.mode == "discarded", "prefetched producers do not survive shutdown"
    assert p1.task is not None and (p1.task.cancelled() or p1.task.done())
    assert order == ["persist", "release"], "the lease is released only after the final flush"
    pl = r.mgr.state.paused_lesson
    assert pl is not None and pl.page_index == 1
    assert pl.run_id == p1.run_id and pl.resume_cursor == 0
    assert pl.lesson_plan is not None
    assert pl.completed_page_ids == [f"{lesson_id}_p0"]
    assert all(t.cancelled() or t.done() for t in list(r.mgr._tasks)), \
        "no task spawned by the job survives shutdown"


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_outline_task(monkeypatch):
    import app.state_machine.manager as manager_mod

    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    started = asyncio.Event()

    async def slow_outline(question, memory_summary, gw=None):
        started.set()
        await asyncio.sleep(30)
        return None

    monkeypatch.setattr(manager_mod, "outline_lesson", slow_outline)
    r = _rig(monkeypatch, delay=0.01)
    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await asyncio.wait_for(started.wait(), 1.0)
    outline_task = r.mgr._outline_task
    assert outline_task is not None and not outline_task.done()

    await r.mgr.shutdown()
    assert outline_task.cancelled(), "the outline task dies with the job"


@pytest.mark.asyncio
async def test_end_session_discards_prefetch_runs(monkeypatch):
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, plan=_lesson_plan(2), monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    _ack_started(r, 0)
    p1 = r.mgr.lesson_runner.runs[f"{lesson_id}:p1"]
    assert p1.mode == "parked"

    await r.mgr.end_session(say_goodbye=False)
    await settle()

    assert p1.mode == "discarded", "an ended session leaves no prefetch producer running"
    assert p1.task is not None and (p1.task.cancelled() or p1.task.done())
    assert r.mgr.state.conv_state == ConvState.TASK_CANCELLED


# ---------------------------------------------------------------------------------------------
# Prefetch deferral during a doubt
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prefetch_deferred_during_doubt(monkeypatch):
    """While a doubt turn is the active run, `maybe_prefetch` must not start the next page:
    the producer semaphore has to stay free for the doubt's answer."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, plan=_lesson_plan(3), monkeypatch=monkeypatch)
    runner = r.mgr.lesson_runner
    lesson_id = r.mgr.state.lesson_id
    assert f"{lesson_id}:p1" not in runner.runs

    started: list[int] = []
    real_start = runner._start_page_run

    def spy(index, parked=True, turn_id=None):
        started.append(index)
        return real_start(index, parked=parked, turn_id=turn_id)

    monkeypatch.setattr(runner, "_start_page_run", spy)

    req = TurnRequest(kind="doubt", generation=r.mgr.state.generation, turn_id="t_doubt",
                      question="why?", student_text="why?")
    doubt_page = PageRecord(board_id="b", page_id="d1", lesson_question="why?")
    doubt_run = runner.new_run(req, doubt_page)
    runner.active = doubt_run
    try:
        runner.maybe_prefetch()
        assert started == [], "no page run starts while a doubt is producing"
        assert f"{lesson_id}:p1" not in runner.runs
    finally:
        runner.discard_run(doubt_run.run_id)
        runner.active = None


@pytest.mark.asyncio
async def test_new_question_mid_chapter_discards_lesson(monkeypatch):
    """A new question mid-chapter discards the lesson (pages stay persisted), never
    leaves a half-dead chapter running."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, plan=_lesson_plan(3), monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    _ack_started(r, 0)
    runs_before = [rid for rid, run in r.mgr.lesson_runner.runs.items()
                   if run.lesson_id == lesson_id]
    assert runs_before

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Solve 2x + 3 = 11", intent="new")
    await settle()

    assert r.mgr.lesson_runner.chapter_plan is None
    for rid in runs_before:
        assert r.mgr.lesson_runner.runs[rid].mode == "discarded"
    assert r.mgr.state.paused_lesson is None
    assert r.rec.of("turn_started")[-1].kind == "lesson"


@pytest.mark.asyncio
async def test_reload_chapter_restores_plan_from_checkpoint(monkeypatch):
    """A fresh job has no runner chapter; the checkpoint's lesson_plan rebuilds it and
    resume starts the next page."""
    monkeypatch.setattr(settings, "FEATURE_PARKED_RESUME", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    plan = _lesson_plan(3)
    await _start_chapter(r, plan=plan, monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle()
    assert r.mgr.state.page.page_id.endswith("_p1")
    await r.mgr.handle_event(ConvEvent.MARKED_DOUBT, marks=[])
    await settle()

    # Simulate a new job: no runs, no chapter plan; only the checkpoint survives.
    for run in list(r.mgr.lesson_runner.runs.values()):
        r.mgr.lesson_runner.discard_run(run.run_id)
    r.mgr.lesson_runner.runs.clear()
    r.mgr.lesson_runner.chapter_plan = None
    r.mgr.lesson_runner.lesson_id = None
    r.mgr.state.active_turn_id = None
    r.mgr.state.active_turn_kind = None
    r.mgr.state.conv_state = ConvState.IDLE

    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()
    assert r.mgr.lesson_runner.chapter_plan is not None, "the plan came from the checkpoint"
    assert r.mgr.state.page.page_id.endswith("_p1")
    starts = r.rec.of("turn_started")
    assert starts[-1].page_index == 1


# ---------------------------------------------------------------------------------------------
# Caption mode across a page turn
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_caption_mode_across_page_turn(monkeypatch):
    """With audio_mode=captions the caption pacer drives steps; a page's completion
    starts the next page and its own pacer, with no voice handle at all."""
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    monkeypatch.setattr(settings, "FEATURE_CAPTIONS", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)

    r = _rig(monkeypatch, delay=0.01)
    plan = _lesson_plan(2)
    r.mgr.lesson_runner.start_chapter(plan, "L_cap")
    r.mgr.state.lesson_id = "L_cap"
    r.mgr.state.audio_mode = "captions"
    r.mgr.state.conv_state = ConvState.GRAPH_RUNNING   # the caller normally sets this in the dispatcher
    await r.mgr.run_turn(r.mgr.lesson_runner.page_request(0))
    await settle()

    assert r.session.handles == [], "captions never call session.say"
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING

    def ack_all() -> bool:
        st = r.mgr.state
        run = r.mgr.lesson_runner.active
        if run is None or not st.active_turn_id or not run.stream.items:
            return False
        n = len(run.stream.items)
        r.mgr.on_step_progress(StepProgress(
            turn_id=st.active_turn_id, generation=st.generation, step_index=n - 1,
            event="completed", completed_by="caption", heard_up_to=n - 1, started_up_to=n - 1))
        return True

    async def ack_until(pred, tries: int = 120) -> bool:
        for _ in range(tries):
            ack_all()
            if pred():
                return True
            await asyncio.sleep(0.02)
        return pred()

    assert await ack_until(lambda: r.mgr.state.page.page_id.endswith("_p1"))
    ends = r.rec.of("turn_ended")
    assert ends and ends[0].page_only is True, "page 0 ended at the page boundary, no flush"
    p1 = r.mgr.lesson_runner.runs["L_cap:p1"]
    assert p1.mode == "active"
    assert r.session.handles == [], "the next page is paced by its own caption pacer"

    assert await ack_until(lambda: r.mgr.state.conv_state == ConvState.IDLE)
    assert r.mgr.state.page.lesson_completed is True
    assert r.rec.of("turn_ended")[-1].page_only is False
    assert r.session.handles == []


def test_outline_reply_recovered_from_prose_and_fences():
    """Malformed outline replies are handled below the prompt: the gateway's
    extract_json_object recovers the object from fences/prose and the LessonPlan coercers
    repair unknown roles, missing ids and stringified keyPoints."""
    from app.contracts.lesson import LessonPlan
    from app.gateway.groq_client import extract_json_object

    raw = ('Here is the plan:\n```json\n'
           '{"scope":"topic","title":"BPT","pages":[{"title":"P1","keyPoints":"ratio; parallel",'
           '"blocks":[{"role":"sketch","brief":"triangle"}]}]}'
           '\n```\nHope this helps!')
    plan = LessonPlan.model_validate(extract_json_object(raw))
    assert plan.scope == "topic" and len(plan.pages) == 1
    assert plan.pages[0].key_points == ["ratio", "parallel"]
    block = plan.pages[0].blocks[0]
    assert block.role == "figure", "an unknown role coerces to figure"
    assert block.id, "a missing block id is repaired"


# ---------------------------------------------------------------------------------------------
# Rule 30 (question amendment) must not orphan the old chapter
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_amend_mid_chapter_discards_old_lesson_runs(monkeypatch):
    """A doubt before the first heard step amends the question; the old chapter plan and its
    producers are discarded instead of lingering under the new lesson."""
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=5.0)          # nothing published: lesson_unheard stays True
    await _start_chapter(r, plan=_lesson_plan(3), monkeypatch=monkeypatch)
    lesson_id = r.mgr.state.lesson_id
    runner = r.mgr.lesson_runner
    old_runs = [rid for rid, run in runner.runs.items() if run.lesson_id == lesson_id]
    assert old_runs and r.mgr.state.published_upto < 0

    await _say_to_tutor(r, "wait, actually find x in 2x + 3 = 11", "doubt")
    await settle()

    assert runner.chapter_plan is None, "the stale chapter plan dies with the amended lesson"
    for rid in old_runs:
        assert runner.runs[rid].mode == "discarded"
    assert r.mgr.state.active_turn_kind == "lesson"
    assert r.mgr.state.lesson_id != lesson_id, "the amended lesson is a fresh lesson"


# ---------------------------------------------------------------------------------------------
# plan_and_compile_scene must return the compiled RenderScene (not the SceneDocument)
# ---------------------------------------------------------------------------------------------
class _SceneDocGateway:
    """Fake gateway: every scene.plan call returns the fixture SceneDocument."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.calls = 0

    async def complete_json_ex(self, **kwargs):
        self.calls += 1
        return SceneDocument.model_validate(self.raw), None


@pytest.mark.asyncio
async def test_plan_and_compile_scene_returns_render_scene():
    from app.agents.nodes.scene import plan_and_compile_scene
    from app.scene_engine.compile import RenderScene
    from tests.make_frontend_fixture import SCENES as FIXTURE_SCENES

    gw = _SceneDocGateway(FIXTURE_SCENES["triangle_and_circle"])
    scene, report, diagram, status = await plan_and_compile_scene(
        "triangle ABC", FIXTURE_PLAN, namespace="pg1_", gw=gw)
    assert status == "validated" and diagram is not None
    assert isinstance(scene, RenderScene), f"slot 0 must be the RenderScene, got {type(scene).__name__}"
    # The page pipeline projects slot 0 for the block aspect: must not raise.
    from app.scene_engine.project import project_scene_to_commands
    assert project_scene_to_commands(scene, namespace="")[2] > 0


@pytest.mark.asyncio
async def test_page_pipeline_real_scene_compile_builds_figure_block(monkeypatch):
    """End to end: page_prepare_node with the REAL plan_and_compile_scene (only the LLM is
    faked) commits the figure block instead of failing the producer with
    AttributeError: 'list' object has no attribute 'values'."""
    from tests.make_frontend_fixture import PAGE_SCENES

    gw = _SceneDocGateway(PAGE_SCENES["wide_segments"])
    page = _page()
    st = AgentState(session_id="s", user_id="u", board_id="b")
    st.page = page
    ctx = RunContext(run_id="L_x:p1", page_record=page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)
    page_plan = PagePlan.model_validate({
        "title": "Law of Cosines", "objective": "o", "keyPoints": ["k"], "stepBudget": 8,
        "blocks": [{"id": "fig_full_triangle", "role": "figure", "sticky": True,
                    "brief": "Triangle ABC with sides a,b,c opposite angles A,B,C respectively."}],
    })
    req = TurnRequest(kind="lesson", generation=0, turn_id="tp3", question="Law of Cosines",
                      lesson_id="L_x", page_index=1, page_count=2, page_plan=page_plan)
    out = await page_prepare_node({"request": req, "gw": gw, "agent_state": st, "run_ctx": ctx})

    assert out["visual_status"] == "validated"
    assert [b.id for b in out["page_commit"].blocks] == ["fig_full_triangle"]
    assert "fig_full_triangle" in ctx.sticky_scenes, "the sticky cache holds a RenderScene"


# ---------------------------------------------------------------------------------------------
# The filler aside's own audio must not strand the page turn
# ---------------------------------------------------------------------------------------------
def _speaking_session(r):
    """Model main.py's wiring: every say() makes LiveKit's agent state "speaking", which fires
    SPEECH_STARTED into the manager — asides (greeting, filler) included."""
    real_say = r.session.say

    def say(text, **kw):
        h = real_say(text, **kw)
        r.mgr.fire(ConvEvent.SPEECH_STARTED)
        return h

    r.session.say = say


@pytest.mark.asyncio
async def test_filler_aside_audio_does_not_drop_page_turn(monkeypatch):
    """The filler aside's speech moves GRAPH_RUNNING -> AGENT_SPEAKING (rule 2); its
    done-callback then sees a non-GRAPH_RUNNING state and must still turn the page —
    otherwise page 1 never starts and the tutor falls silent."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01, hold=True)
    base = graph_mod.iter_turn_steps
    page1_gate = asyncio.Event()

    async def slow_page1(request, **kw):
        if request.page_index == 1:
            await page1_gate.wait()             # page 1 still generating -> filler path
        async for item in base(request, **kw):
            yield item

    monkeypatch.setattr(graph_mod, "iter_turn_steps", slow_page1)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    _speaking_session(r)

    r.session.latest().release.set()            # page 0 speech ends -> rule 29 -> gap
    await settle()
    filler = next(h for h in r.session.handles if PAGE_FILLER_LINES[1] in "".join(h.spoken))
    assert r.mgr.state.conv_state == ConvState.AGENT_SPEAKING, "precondition: filler audio seen"
    filler.release.set()                        # the filler finishes
    await settle()
    page1_gate.set()
    await settle()

    assert r.mgr.state.page.page_id.endswith("_p1"), "page 1 activated after the filler"
    assert [s.page_index for s in r.rec.of("turn_started")][-1] == 1


@pytest.mark.asyncio
async def test_speech_started_during_gap_does_not_drop_advance(monkeypatch):
    """Same class: any SPEECH_STARTED in the gap (a late agent_state_changed for the page's own
    audio) must not make advance_page log page_advance_stale_dropped."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 400)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)
    r.session.latest().release.set()
    await settle(cycles=6)
    assert r.mgr.lesson_runner.advance_pending is True
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)    # GRAPH_RUNNING -> AGENT_SPEAKING
    await asyncio.sleep(0.5)                               # the gap runs out
    await settle()

    assert r.mgr.state.page.page_id.endswith("_p1")
    assert r.mgr.lesson_runner.advance_pending is False


@pytest.mark.asyncio
async def test_page_figure_block_not_regated_by_question_regex():
    """The outline already declared role=figure; a brief without a geometry noun from
    QUESTION_IS_GEOMETRIC_REGEX ("Line L: ax+by+c=0, point P(x0,y0).") must still be compiled
    instead of returning text_only before any LLM call."""
    from tests.make_frontend_fixture import PAGE_SCENES

    gw = _SceneDocGateway(PAGE_SCENES["wide_segments"])
    page = _page()
    st = AgentState(session_id="s", user_id="u", board_id="b")
    st.page = page
    ctx = RunContext(run_id="L_x:p0", page_record=page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)
    page_plan = PagePlan.model_validate({
        "title": "Concept Overview", "objective": "o", "keyPoints": ["k"], "stepBudget": 8,
        "blocks": [{"id": "fig_line_point", "role": "figure", "sticky": True,
                    "brief": "Line L: ax+by+c=0, point P(x0,y0)."}],
    })
    req = TurnRequest(kind="lesson", generation=0, turn_id="tp4", question="Concept Overview",
                      lesson_id="L_x", page_index=0, page_count=2, page_plan=page_plan)
    out = await page_prepare_node({"request": req, "gw": gw, "agent_state": st, "run_ctx": ctx})

    assert gw.calls >= 1, "the declared figure block reached the scene planner"
    assert out["visual_status"] == "validated"
    assert [b.id for b in out["page_commit"].blocks] == ["fig_line_point"]


# ---------------------------------------------------------------------------------------------
# A page that ends inside an interrupt window must still turn the page
# ---------------------------------------------------------------------------------------------
async def _page_ends_while_student_talks(r, label: str, utterance: str = "Continue.") -> None:
    """The student starts talking over the page's last words (rule 4); the page's speech finishes
    while the utterance is being classified (rule 24 remembers it); then the label arrives."""
    r.labels.append(label)
    await r.mgr.handle_event(ConvEvent.VAD_START)
    r.session.latest().release.set()                    # SPEECH_ENDED lands in the window
    await settle(cycles=2)
    assert r.mgr.state.conv_state == ConvState.INTERRUPT_DETECTED
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text=utterance)
    await settle()


@pytest.mark.asyncio
async def test_affirmation_after_page_end_in_window_turns_page(monkeypatch):
    """_return_from_interrupt must not treat _speech_finished as a FULL lesson end
    (lesson_completed, turn_ended{pageOnly:false}, IDLE) in the middle of a chapter;
    otherwise page 1 never starts and every later "continue" is a no-op in IDLE."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)

    await _page_ends_while_student_talks(r, "affirmation")

    ends = r.rec.of("turn_ended")
    assert ends and all(e.page_only for e in ends), "page 0 ended as a page, not the lesson"
    assert r.mgr.state.page.page_id.endswith("_p1"), "the chapter moved on to page 1"
    assert r.mgr.state.page.lesson_completed is False


@pytest.mark.asyncio
async def test_backchannel_after_page_end_in_window_turns_page(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)

    await _page_ends_while_student_talks(r, "backchannel", "hmm okay yes")

    assert r.mgr.state.page.page_id.endswith("_p1")


@pytest.mark.asyncio
async def test_redirect_after_page_end_in_window_turns_page(monkeypatch):
    """Same class in _after_redirect: an off-topic remark over the page's last words."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    _ack_started(r, 0)

    await _page_ends_while_student_talks(r, "off_topic", "did you watch the cricket match")
    r.session.latest().release.set()                    # the redirect aside finishes
    await settle()

    assert all(e.page_only for e in r.rec.of("turn_ended"))
    assert r.mgr.state.page.page_id.endswith("_p1")


@pytest.mark.asyncio
async def test_voice_continue_in_idle_resumes_stopped_chapter(monkeypatch):
    """After a chapter stop (checkpoint + Continue offered) the student says
    "continue"; rule 9 in IDLE must resume the page — a silent no-op would leave only the
    on-screen button working."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    r.mgr._write_chapter_checkpoint(1)                     # what chapter_stop leaves behind
    r.mgr.state.conv_state = ConvState.IDLE
    assert r.mgr.can_continue()
    assert r.rec.of("turn_started")[-1].page_index == 0

    r.labels.append("affirmation")
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="Continue.")
    await settle()

    started = r.rec.of("turn_started")[-1]
    assert started.page_id.endswith("_p1"), "the voice cue resumed the checkpointed page"


@pytest.mark.asyncio
async def test_plain_yes_in_idle_is_still_a_noop(monkeypatch):
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    await _start_chapter(r, monkeypatch=monkeypatch)
    r.mgr._write_chapter_checkpoint(1)
    r.mgr.state.conv_state = ConvState.IDLE
    n = len(r.rec.of("turn_started"))

    r.labels.append("affirmation")
    await r.mgr.handle_event(ConvEvent.USER_TURN_DONE, text="okay")
    await settle()
    assert len(r.rec.of("turn_started")) == n, "only an explicit continue cue resumes"


# ---------------------------------------------------------------------------------------------
# Continue after a chapter stop teaches the checkpointed page through the page pipeline
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_continue_after_chapter_stop_starts_the_page_run(monkeypatch):
    """A chapter stop leaves a checkpoint at page i+1 with run_id None and cursor 0. Continue
    must start the page pipeline for that page: the generic resume regeneration lacks the page
    plan (no figure blocks, resume.v3 prompt) and reports the stale page 0 with has_next_page
    False, so the chapter silently ends after that page."""
    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.01)
    seen: list[TurnRequest] = []
    base = graph_mod.iter_turn_steps

    async def recording(request, **kw):
        seen.append(request)
        async for item in base(request, **kw):
            yield item

    monkeypatch.setattr(graph_mod, "iter_turn_steps", recording)
    await _start_chapter(r, plan=_lesson_plan(3), monkeypatch=monkeypatch)
    r.mgr._write_chapter_checkpoint(1)                     # what chapter_stop leaves behind
    r.mgr.state.conv_state = ConvState.IDLE

    await r.mgr.handle_event(ConvEvent.CONTINUE)
    await settle()

    started = r.rec.of("turn_started")[-1]
    assert started.page_index == 1 and started.page_id.endswith("_p1")
    assert r.mgr.state.page_index == 1
    assert r.mgr.state.has_next_page is True, "page 2 still follows"
    assert seen[-1].page_plan is not None and seen[-1].page_index == 1, \
        "page 1 is generated by the page pipeline (its blocks and teaching.page prompt)"


# ---------------------------------------------------------------------------------------------
# A validated figure is committed even when the teaching stream yields no step
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_validated_figure_committed_when_stream_yields_no_steps(monkeypatch):
    """commit_figure must run even when the step loop yields nothing: a 0-step doubt
    (reasoning-only stream) would otherwise never send its compiled figure — yet the page
    record (and board_state) keep it, so the figure appears later as a ghost on refresh, or
    on an unrelated next turn."""
    monkeypatch.setattr(settings, "FEATURE_PAGE_COMMIT", False)
    rec = EventRecorder()
    monkeypatch.setattr(runner_mod, "send_event", rec)
    diagram = _diagram()

    async def zero_step_producer(request, run_ctx=None, **kw):
        run_ctx.page_record.diagram = diagram          # doubt_prepare_node validated a figure
        run_ctx.page_record.visual_status = "validated"
        return
        yield

    monkeypatch.setattr(graph_mod, "iter_turn_steps", zero_step_producer)
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="doubt", generation=0, turn_id="t_d", question="q", requires_new_figure=True)
    page = _page()
    run = mgr.lesson_runner.new_run(req, page)
    mgr.lesson_runner.active = run
    mgr.state.active_turn_id = "t_d"
    await settle()

    assert run.stream.done and not run.stream.items
    commits = rec.of("diagram_commit")
    assert commits and commits[-1].diagram is diagram, "the figure the page now holds is on the board"


# ---------------------------------------------------------------------------------------------
# A producer that ends with zero steps (no exception) is a failed production
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_zero_step_prefetch_is_regenerated_not_activated_empty(monkeypatch):
    """A prefetched page whose stream ends empty (e.g. a reasoning-only reply) must count as a
    failed production: marking it production="done" activates the empty page instead of
    regenerating it (only "failed" runs are), and the consumer speaks FALLBACK_LINE in the
    middle of the chapter."""
    from app.state_machine.manager import FALLBACK_LINE

    monkeypatch.setattr(settings, "PAGE_GAP_MS", 10)
    r = _rig(monkeypatch, delay=0.0, hold=False)
    page1_turns: list[str] = []

    async def empty_first_page1(request, graph=None, row_tracker=None, agent_state=None,
                                history=None, stored_turns=None, gw=None, run_ctx=None, **kw):
        if request.page_index == 0:
            await asyncio.sleep(0.05)
        if request.page_index == 1:
            page1_turns.append(request.turn_id)
            if len(page1_turns) == 1:
                return                                  # zero steps, no exception
        yield fakes.make_step(request.turn_id, request.generation, 0, "Step.", write="r"), "Step."

    async def fake_outline(question, memory_summary, gw=None):
        return _lesson_plan(2)

    import app.state_machine.manager as manager_mod
    monkeypatch.setattr(settings, "FEATURE_CHAPTERS", True)
    monkeypatch.setattr(manager_mod, "outline_lesson", fake_outline)
    monkeypatch.setattr(graph_mod, "iter_turn_steps", empty_first_page1)

    await r.mgr.handle_event(ConvEvent.TYPED_QUESTION, text="Teach me BPT", intent="topic")
    await settle()
    await r.mgr.handle_event(ConvEvent.SPEECH_STARTED)
    await settle()
    _ack_started(r, 0)
    await settle()
    await r.mgr.handle_event(ConvEvent.SPEECH_ENDED, generation=r.mgr.state.generation,
                             turn_id=r.mgr.state.active_turn_id)
    await settle()

    assert len(page1_turns) == 2, "the empty prefetch is regenerated once"
    assert not any(FALLBACK_LINE in t for t in r.session.said_text)


@pytest.mark.asyncio
async def test_page_text_blocks_have_no_latex_on_the_board():
    """Block eq_trig `\\sin\\theta=\\frac{\\text{opposite}}{\\text{hypotenuse}}` must reach the board
    stripped of LaTeX instead of being drawn on the canvas with its backslashes."""
    page = _page()
    st = AgentState(session_id="s", user_id="u", board_id="b")
    st.page = page
    ctx = RunContext(run_id="L_x:p2", page_record=page, row_tracker=BoardRowTracker(),
                     is_cancelled=lambda: False)
    page_plan = PagePlan.model_validate({
        "title": "Trigonometric Ratios", "objective": "o", "keyPoints": ["k"], "stepBudget": 8,
        "blocks": [{"id": "eq_trig", "role": "text",
                    "text": "\\sin\\theta=\\frac{\\text{opposite}}{\\text{hypotenuse}}\n$a^2+b^2=c^2$"}],
    })
    req = TurnRequest(kind="lesson", generation=0, turn_id="tp5", question="Trig",
                      lesson_id="L_x", page_index=2, page_count=5, page_plan=page_plan)
    out = await page_prepare_node({"request": req, "gw": None, "agent_state": st, "run_ctx": ctx})
    lines = out["page_commit"].blocks[0].text_lines
    assert lines == ["sin θ=opposite/hypotenuse", "a²+b²=c²"]
    assert not any("\\" in line or "$" in line for line in lines)
