# backend/tests/test_board_state_persistence.py
"""Board-state tests for the PageLedger mirror and the persisted board document."""
import pytest

import app.state_machine.manager as mgr_mod
from app.config import settings
from app.contracts.agent_state import ConvState
from app.contracts.board_ops import BoardOp, Step
from app.state_machine.page_ledger import PageLedger
from app.tutor.stream_parser import normalize_words
from tests.fakes import EventRecorder, settle


def _step(turn_id: str, idx: int, ops: list[BoardOp]) -> Step:
    return Step(turn_id=turn_id, generation=1, step_index=idx, spoken_text=f"step {idx}",
                words=normalize_words(f"step {idx}"), ops=ops)


def test_snapshot_groups_subpages():
    ledger = PageLedger()
    w1 = BoardOp(op_id="t:0:0", kind="WRITE", at_word=0, text="a = 1", row_id="w1")
    br = BoardOp(op_id="t:1:0", kind="PAGE_BREAK", at_word=0, page_title="Part 2")
    w2 = BoardOp(op_id="t:1:1", kind="WRITE", at_word=0, text="b = 2", row_id="w2")

    ledger.on_published("root", _step("t", 0, [w1]))
    ledger.on_published("root", _step("t", 1, [br, w2]))

    snap = ledger.snapshot_page("root")
    assert [sp.sub_id for sp in snap.sub_pages] == ["root", "root_p2"]
    assert snap.sub_pages[0].ops == [], "unacked ops are not in the snapshot"
    assert ledger.current_sub("root") == "root_p2"

    ledger.on_acked(["t:0:0", "t:1:1"])
    snap = ledger.snapshot_page("root")
    assert [op.op_id for op in snap.sub_pages[0].ops] == ["t:0:0"]
    assert [op.op_id for op in snap.sub_pages[1].ops] == ["t:1:1"]
    assert [op.op_id for op in ledger.acked_ops("root")] == ["t:0:0", "t:1:1"]

    # rows_for_prompt lists only the visible sub-page's acked WRITE rows
    assert ledger.rows_for_prompt("root") == [{"row_id": "w2", "text": "b = 2"}]


def test_overflow_split_moves_tail_ops():
    from app.contracts.messages import PageTurned

    ledger = PageLedger()
    w1 = BoardOp(op_id="t:0:0", kind="WRITE", at_word=0, text="a = 1", row_id="w1")
    w2 = BoardOp(op_id="t:0:1", kind="WRITE", at_word=0, text="b = 2", row_id="w2")
    w3 = BoardOp(op_id="t:1:0", kind="WRITE", at_word=0, text="c = 3", row_id="w3")
    ledger.on_published("root", _step("t", 0, [w1, w2]))
    ledger.on_published("root", _step("t", 1, [w3]))
    ledger.on_acked(["t:0:0", "t:0:1", "t:1:0"])

    ledger.on_page_turned(PageTurned(
        root_page_id="root", from_sub_id="root", to_sub_id="root_p2",
        cause="overflow", at_op_id="t:1:0"))

    snap = ledger.snapshot_page("root")
    assert [sp.sub_id for sp in snap.sub_pages] == ["root", "root_p2"]
    assert [op.op_id for op in snap.sub_pages[0].ops] == ["t:0:0", "t:0:1"]
    assert [op.op_id for op in snap.sub_pages[1].ops] == ["t:1:0"]
    assert ledger.current_sub("root") == "root_p2"
    assert ledger.rows_for_prompt("root") == [{"row_id": "w3", "text": "c = 3"}]


# ---------------------------------------------------------------------------------------------
# New tables are additive

@pytest.mark.asyncio
async def test_create_all_adds_new_tables_only():
    """A fresh engine with only the older tables gains the new tables; old rows survive."""
    from sqlalchemy import inspect, select
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.persistence.models import Base, Board, Segment, Turn

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Board.__table__.create)
            await conn.run_sync(Turn.__table__.create)
            await conn.run_sync(Segment.__table__.create)
        async with engine.begin() as conn:
            await conn.execute(Board.__table__.insert().values(
                id="b_old", user_id="u1", title="legacy", preview="2+2"))
            await conn.execute(Turn.__table__.insert().values(
                id="t_old", board_id="b_old", user_id="u1", order_index=0,
                question="What is 2+2?", raw_response="[STEP]It is 4.[/STEP]"))

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
            board = (await conn.execute(
                select(Board.preview, Board.title).where(Board.id == "b_old"))).one()
            turn_q = (await conn.execute(
                select(Turn.question).where(Turn.id == "t_old"))).scalar_one()
    finally:
        await engine.dispose()

    assert {"boards", "turns", "segments"} <= set(names)
    assert {"board_state", "pages", "turn_metrics"} <= set(names)
    assert tuple(board) == ("2+2", "legacy")
    assert turn_q == "What is 2+2?"


# ---------------------------------------------------------------------------------------------
# Board-state documents

@pytest.mark.asyncio
async def test_board_state_roundtrip(_offline):
    from app.contracts.memory import BoardStateDoc, Exchange, SessionMemory
    from app.persistence.board_state import load_board_state, save_board_state

    doc = BoardStateDoc(
        board_id="b_mem",
        memory=SessionMemory(
            rolling_summary="Learnt BPT.",
            page_summaries={"p_mem": "DE || BC ⇒ AD/DB = AE/EC"},
            dialogue=[Exchange(kind="doubt", student="why?", tutor="because they stand on DE")],
        ),
        paused_lesson={"board_id": "b_mem", "page_id": "p_mem",
                       "lesson_question": "Teach me BPT", "resume_cursor": 2},
        lesson_id="L_mem",
        current_page_id="p_mem",
        last_generation=4,
    )
    async with _offline() as s:
        await save_board_state(s, doc, owner="w1")
    async with _offline() as s:
        got = await load_board_state(s, "b_mem")
    assert got == doc
    assert got.memory.page_summaries == {"p_mem": "DE || BC ⇒ AD/DB = AE/EC"}


@pytest.mark.asyncio
async def test_legacy_board_builds_doc(_offline):
    from app.contracts.agent_state import PausedLesson
    from app.persistence.board_state import build_legacy_doc, load_board_state
    from app.persistence.repo import save_turn

    async with _offline() as s:
        await save_turn(
            s, turn_id="t_legacy", board_id="b_legacy", user_id="u1",
            question="Teach me similarity", page_id="p_legacy",
            kind="lesson", status="complete",
        )
    async with _offline() as s:
        assert await load_board_state(s, "b_legacy") is None    # no board_state row: legacy board
        doc = await build_legacy_doc(s, "b_legacy")

    assert doc is not None and doc.board_id == "b_legacy"
    assert doc.current_page_id == "p_legacy"
    assert doc.memory.rolling_summary == ""
    paused = PausedLesson.model_validate(doc.paused_lesson)
    assert paused.page_id == "p_legacy"
    assert paused.lesson_question == "Teach me similarity"
    assert paused.lesson_completed is True                  # nothing to resume
    assert paused.lesson_turn_id == "t_legacy"


# ---------------------------------------------------------------------------------------------
# Board lease and order-index retry

@pytest.mark.asyncio
async def test_lease_blocks_second_writer(_offline):
    from app.contracts.memory import BoardStateDoc
    from app.persistence.board_state import (
        LeaseLost, acquire_lease, load_board_state, save_board_state,
    )

    async with _offline() as s:
        assert await acquire_lease(s, "b_lease", "writer_A") is True
    async with _offline() as s:
        assert await acquire_lease(s, "b_lease", "writer_B", wait_s=0, steal=False) is False
    async with _offline() as s:
        with pytest.raises(LeaseLost):
            await save_board_state(s, BoardStateDoc(board_id="b_lease"), owner="writer_B")
    async with _offline() as s:
        await save_board_state(s, BoardStateDoc(board_id="b_lease", last_generation=7),
                               owner="writer_A")
        doc = await load_board_state(s, "b_lease")
    assert doc is not None and doc.last_generation == 7


@pytest.mark.asyncio
async def test_lease_steal_after_wait(_offline):
    from app.persistence.board_state import acquire_lease, renew_lease

    async with _offline() as s:
        assert await acquire_lease(s, "b_lease2", "writer_A") is True
    async with _offline() as s:
        # Waits (briefly) instead of stealing immediately, then takes the board anyway.
        assert await acquire_lease(s, "b_lease2", "writer_B", wait_s=0.1) is True
        assert await renew_lease(s, "b_lease2", "writer_B") is True
        assert await renew_lease(s, "b_lease2", "writer_A") is False


@pytest.mark.asyncio
async def test_lease_lost_refuses_writes(_offline):
    from app.contracts.memory import BoardStateDoc
    from app.persistence.board_state import (
        LeaseLost, acquire_lease, release_lease, renew_lease, save_board_state, save_pages,
    )

    async with _offline() as s:
        assert await acquire_lease(s, "b_lease3", "writer_A") is True
    async with _offline() as s:
        assert await acquire_lease(s, "b_lease3", "writer_B", wait_s=0.1) is True
    async with _offline() as s:
        assert await renew_lease(s, "b_lease3", "writer_A") is False
        with pytest.raises(LeaseLost):
            await save_board_state(s, BoardStateDoc(board_id="b_lease3"), owner="writer_A")
        with pytest.raises(LeaseLost):
            await save_pages(s, "b_lease3", "L1", [{"page_id": "p_lease3", "ops": []}],
                             owner="writer_A")
    async with _offline() as s:
        await release_lease(s, "b_lease3", "writer_B")
        # The lease is free again, so the old writer may take it.
        assert await acquire_lease(s, "b_lease3", "writer_A", wait_s=0.1) is True


@pytest.mark.asyncio
async def test_order_index_retry(_offline):
    from sqlalchemy.exc import IntegrityError

    from app.persistence.repo import get_or_create_board, save_turn

    class _FlushOnce:
        """First insert flush races another writer and hits the unique index."""

        def __init__(self, session) -> None:
            self._session = session
            self.flushes = 0

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def flush(self):
            self.flushes += 1
            if self.flushes == 1:
                raise IntegrityError(
                    "INSERT", {},
                    Exception("UNIQUE constraint failed: turns.board_id, turns.order_index"))
            await self._session.flush()

    async with _offline() as s:
        await get_or_create_board(s, "b_retry", "u1")
        await s.commit()
    async with _offline() as s:
        wrapped = _FlushOnce(s)
        first = await save_turn(wrapped, turn_id="t_retry", board_id="b_retry", user_id="u1",
                                question="q1", page_id="p_retry")
        second = await save_turn(s, turn_id="t_retry2", board_id="b_retry", user_id="u1",
                                 question="q2", page_id="p_retry")
        assert wrapped.flushes >= 2
    assert first.order_index == 0
    assert second.order_index == 1


# ---------------------------------------------------------------------------------------------
# Persist the ledger and the state document

@pytest.mark.asyncio
async def test_snapshot_only_acked_ops(_offline):
    from app.persistence.board_state import load_pages, load_snapshot, save_pages

    acked = BoardOp(op_id="t:0:0", kind="WRITE", at_word=0, text="drawn", row_id="w1")
    unacked = BoardOp(op_id="t:0:1", kind="WRITE", at_word=0, text="never drawn", row_id="w2")
    page = {
        "page_id": "p_snap", "root_page_id": "p_snap", "sub_index": 1, "title": "",
        "ops": [{"op": acked.model_dump(by_alias=True, exclude_none=True), "acked": True},
                {"op": unacked.model_dump(by_alias=True, exclude_none=True), "acked": False}],
    }
    async with _offline() as s:
        await save_pages(s, "b_snap", "L1", [page], owner="writer")
        snap = await load_snapshot(s, "b_snap", "p_snap")
        pages = await load_pages(s, "b_snap")

    assert snap is not None
    assert [op.op_id for op in snap.sub_pages[0].ops] == ["t:0:0"]
    assert [entry["acked"] for entry in pages[0]["ops"]] == [True, False], \
        "the row keeps both ops with their acked flags"


@pytest.mark.asyncio
async def test_end_persists_board_state(monkeypatch, _offline):
    from app.persistence import board_state as bs
    from tests.test_lifecycle_scenarios import _ack, _rig, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)          # no summary calls needed here
    h = await _start_lesson(r)
    _ack(r, 0)
    h.release.set()
    await settle()

    board_id = r.mgr.state.board_id
    page_id = r.mgr.state.page.page_id
    async with _offline() as s:
        doc = await bs.load_board_state(s, board_id)
        pages = await bs.load_pages(s, board_id)
        snap = await bs.load_snapshot(s, board_id, page_id)

    assert doc is not None and doc.board_id == board_id
    assert doc.current_page_id == page_id
    assert doc.lesson_id == r.mgr.state.lesson_id
    assert doc.memory.dialogue, "the closed turn is persisted in memory"
    assert {p["root_page_id"] for p in pages} == {page_id}
    persisted = [o for p in pages for o in p["ops"]]
    assert any(o["acked"] for o in persisted)
    assert snap is not None and all(o.op_id != "" for o in snap.sub_pages[0].ops)
    assert len(snap.sub_pages[0].ops) < len(persisted), "unacked ops stay out of the snapshot"


@pytest.mark.asyncio
async def test_persist_failure_retries_next_turn(monkeypatch, _offline):
    from app.persistence import board_state as bs
    from tests.test_lifecycle_scenarios import _rig, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)
    real = bs.save_board_state
    fails = {"left": 1}

    async def flaky(session, doc, owner):
        if fails["left"] > 0:
            fails["left"] -= 1
            raise RuntimeError("database is down")
        await real(session, doc, owner)

    monkeypatch.setattr(bs, "save_board_state", flaky)

    h = await _start_lesson(r, "question one")
    h.release.set()
    await settle()
    board_id = r.mgr.state.board_id
    async with _offline() as s:
        assert await bs.load_board_state(s, board_id) is None
    assert r.mgr._board_dirty is True

    h2 = await _start_lesson(r, "question two")
    h2.release.set()
    await settle()
    async with _offline() as s:
        assert await bs.load_board_state(s, board_id) is not None
    assert r.mgr._board_dirty is False


# ---------------------------------------------------------------------------------------------
# Rehydrate on join

@pytest.mark.asyncio
async def test_rehydrate_after_reload(monkeypatch, _offline):
    from app.persistence import board_state as bs
    from tests.test_lifecycle_scenarios import _ack, _rig, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)
    h = await _start_lesson(r)
    _ack(r, 0)
    h.release.set()
    await settle()
    board_id = r.mgr.state.board_id
    lesson_id = r.mgr.state.lesson_id
    acked_ops = [op.op_id for step in r.mgr.state.steps_sent[:1] for op in step.ops]

    async with _offline() as s:
        doc = await bs.load_board_state(s, board_id)
        pages = await bs.load_pages(s, board_id)
    assert doc is not None and len(pages) == 1

    r2 = _rig(monkeypatch)                          # a new job after the reload
    rec2 = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec2)
    r2.mgr.rehydrate(doc, pages)
    await r2.mgr._send_board_snapshot()

    assert r2.mgr.state.board_id == board_id
    assert r2.mgr.state.page is not None
    assert r2.mgr.state.page.page_id == doc.current_page_id
    assert r2.mgr.state.lesson_id == lesson_id
    assert r2.mgr.memory.dialogue and "Lesson step 0." in r2.mgr.memory.dialogue[-1].tutor
    assert not r2.mgr.can_continue(), "a completed lesson offers no Continue"
    snap = rec2.of("board_snapshot")[-1]
    restored = [op.op_id for sp in snap.current.sub_pages for op in sp.ops]
    assert restored == acked_ops, "only acked ops are restored"


@pytest.mark.asyncio
async def test_reload_during_doubt_offers_continue(monkeypatch, _offline):
    from app.persistence import board_state as bs
    from tests.test_lifecycle_scenarios import _ack, _rig, _say_to_tutor, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch, delay=0.01)
    r.mgr.gateway = FakeGateway(out=None)
    h = await _start_lesson(r)
    _ack(r, 0)
    _ack(r, 1)
    await _say_to_tutor(r, "why is that step true", "doubt")

    board_id = r.mgr.state.board_id
    async with _offline() as s:
        doc = await bs.load_board_state(s, board_id)
        pages = await bs.load_pages(s, board_id)
    assert doc is not None and doc.paused_lesson is not None
    assert doc.paused_lesson["resume_cursor"] == 2

    r2 = _rig(monkeypatch)                          # the reloaded tab
    rec2 = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec2)
    r2.mgr.rehydrate(doc, pages)
    assert r2.mgr.can_continue()
    pl = r2.mgr.state.paused_lesson
    assert pl is not None and pl.resume_cursor == 2 and pl.lesson_id == doc.lesson_id

    await r2.mgr._send_board_snapshot()
    snap = rec2.of("board_snapshot")[-1]
    assert snap.can_continue_lesson is True


@pytest.mark.asyncio
async def test_restored_board_doubt_is_doubt(monkeypatch, _offline):
    """After a reload the restored page makes a voice doubt classify as a doubt."""
    from app.persistence import board_state as bs
    from tests.test_lifecycle_scenarios import _rig, _say_to_tutor, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)
    h = await _start_lesson(r)
    h.release.set()
    await settle()
    board_id = r.mgr.state.board_id
    async with _offline() as s:
        doc = await bs.load_board_state(s, board_id)
        pages = await bs.load_pages(s, board_id)

    r2 = _rig(monkeypatch)
    r2.mgr.rehydrate(doc, pages)
    page_id = r2.mgr.state.page.page_id
    seen: dict = {}

    async def recording_classify(**kw):
        seen.update(kw)
        return "doubt"

    monkeypatch.setattr(mgr_mod, "classify_interrupt", recording_classify)
    await _say_to_tutor(r2, "explain that again please", "doubt")

    assert seen.get("lesson_on_board") is True
    assert r2.mgr.state.page.page_id == page_id, "the lesson was not replaced"
    assert r2.mgr.state.paused_lesson is not None, "the lesson stayed resumable"


@pytest.mark.asyncio
async def test_lease_lost_ends_session(monkeypatch, _offline):
    from app.persistence import board_state as bs
    from tests.fakes import FakeSession
    from tests.test_lifecycle_scenarios import _rig

    monkeypatch.setattr(settings, "LEASE_RENEW_S", 0.01)
    r = _rig(monkeypatch)
    board_id = r.mgr.state.board_id
    async with _offline() as s:
        assert await bs.acquire_lease(s, board_id, "other_tab", wait_s=0) is True

    r.mgr.lease_owner = "this_tab"
    r.mgr.session = FakeSession(hold=False)
    r.mgr._lease_task = r.mgr._spawn(r.mgr._renew_lease_loop())
    await settle()

    assert r.mgr.lease_lost is True
    assert r.mgr.state.conv_state == ConvState.TASK_CANCELLED
    ends = r.rec.of("session_ended")
    assert len(ends) == 1 and ends[0].reason == "opened_elsewhere"
    assert [e for e in r.rec.of("aside") if e.kind == "goodbye"] == []
    assert r.mgr.session.handles == [], "no goodbye audio on a stolen board"


# ---------------------------------------------------------------------------------------------
# JSON columns must never receive raw Pydantic models

def _make_diagram(name: str = "triangle") -> "VerifiedDiagram":
    from app.contracts.diagram import (
        DiagramAnchor, DiagramCommand, DiagramReveal, VerifiedDiagram,
    )

    return VerifiedDiagram(
        name=name,
        commands=[DiagramCommand(type="DRAW_LINE", params=[0.0, 0.0, 100.0, 100.0])],
        anchors=[DiagramAnchor(id="tri", labels=["A", "B", "C"], x=0, y=0, width=10, height=10)],
        reveals=[DiagramReveal(narration="base", command_indices=[0], target_id="rg1")],
        prompt_addon="a triangle",
    )


@pytest.mark.asyncio
async def test_save_pages_serializes_diagram(_offline):
    """A page with a diagram persists (the model must be dumped before JSON storage)."""
    from app.persistence.board_state import load_pages, save_pages

    async with _offline() as s:
        await save_pages(s, "b_diag", "L1", [{
            "page_id": "p_diag", "root_page_id": "p_diag", "sub_index": 1,
            "diagram": _make_diagram(), "ops": [],
        }], owner="writer")
    async with _offline() as s:
        pages = await load_pages(s, "b_diag")

    assert pages[0]["diagram"] is not None
    assert pages[0]["diagram"]["name"] == "triangle"
    assert pages[0]["diagram"]["anchors"][0]["id"] == "tri"


# ---------------------------------------------------------------------------------------------
# The acquire poll must see a voluntary lease release

@pytest.mark.asyncio
async def test_acquire_sees_released_lease(tmp_path):
    """The polling loop re-reads the lease row; a clean release is not forced to steal."""
    import asyncio
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.persistence.board_state import acquire_lease, release_lease
    from app.persistence.models import Base, BoardState

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}",
                                 connect_args={"timeout": 15.0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            assert await acquire_lease(s, "b_rel", "A", wait_s=0) is True

        start = asyncio.get_running_loop().time()
        async with factory() as s_b:
            task = asyncio.create_task(acquire_lease(s_b, "b_rel", "B", wait_s=0.6))
            await asyncio.sleep(0.15)                 # A closes its tab and releases cleanly
            async with factory() as s_r:
                await release_lease(s_r, "b_rel", "A")
            ok = await task
        elapsed = asyncio.get_running_loop().time() - start

        assert ok is True
        assert elapsed < 0.5, "a voluntary release must be seen before the deadline"
        async with factory() as s:
            row = (await s.execute(
                select(BoardState).where(BoardState.board_id == "b_rel"))).scalar_one()
        assert row.lease_owner == "B"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# Reload of a completed lesson restores the figure

@pytest.mark.asyncio
async def test_reload_restores_completed_lesson_diagram(monkeypatch, _offline):
    """paused_lesson is None after completion, so rehydrate must take the figure
    from the persisted pages row, not from pl.diagram."""
    from app.persistence import board_state as bs
    from tests.test_lifecycle_scenarios import _rig, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)
    h = await _start_lesson(r)
    r.mgr.state.page.diagram = _make_diagram()
    r.mgr.state.page.visual_status = "validated"
    h.release.set()
    await settle()

    board_id = r.mgr.state.board_id
    async with _offline() as s:
        doc = await bs.load_board_state(s, board_id)
        pages = await bs.load_pages(s, board_id)
    assert doc is not None and doc.paused_lesson is None, "a completed lesson has no checkpoint"
    assert pages and pages[0]["diagram"] is not None, "the figure is persisted with the page"

    r2 = _rig(monkeypatch)
    rec2 = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec2)
    r2.mgr.rehydrate(doc, pages)
    await r2.mgr._send_board_snapshot()

    snap = rec2.of("board_snapshot")[-1]
    assert snap.current.diagram is not None
    assert snap.current.diagram.name == "triangle"


# ---------------------------------------------------------------------------------------------
# Board-state flushes must be serialized

@pytest.mark.asyncio
async def test_board_flush_serialized(monkeypatch, _offline):
    """A background summary flush and a turn-end flush must not interleave;
    the second writer waits for the first instead of racing it."""
    import asyncio

    from app.contracts.agent_state import PageRecord
    from app.persistence import board_state as bs
    from app.state_machine.manager import StateMachineManager

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    mgr = StateMachineManager()
    mgr.state.page = PageRecord(board_id=mgr.state.board_id, page_id="p_flush",
                                lesson_question="q")
    entered = 0
    peak = 0
    gate = asyncio.Event()
    real = bs.save_board_state

    async def gated(session, doc, owner):
        nonlocal entered, peak
        entered += 1
        peak = max(peak, entered)
        await gate.wait()
        entered -= 1
        await real(session, doc, owner)

    monkeypatch.setattr(bs, "save_board_state", gated)
    first = asyncio.create_task(mgr._persist_board_state())
    await asyncio.sleep(0.05)                       # the first flush is inside its write
    second = asyncio.create_task(mgr._persist_board_state())
    await asyncio.sleep(0.05)
    assert peak == 1, "the second flush must wait, not write concurrently"
    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert peak == 1
    assert mgr._board_dirty is False


# ---------------------------------------------------------------------------------------------
# Raced first insert of the board_state row

@pytest.mark.asyncio
async def test_acquire_survives_insert_race(monkeypatch, tmp_path):
    """Two tabs may both see "no board_state row"; the loser must not raise."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.persistence.board_state as bs
    from app.persistence.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}",
                                 connect_args={"timeout": 15.0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as s_a:
            assert await bs.acquire_lease(s_a, "b_race", "A", wait_s=0) is True

        async with factory() as s_b:
            orig = bs._get_row
            fake_used = {"used": False}

            async def fake_get_row(session, board_id):
                # Simulate the race: this tab read before A committed, so it saw no row.
                if session is s_b and board_id == "b_race" and not fake_used["used"]:
                    fake_used["used"] = True
                    return None
                return await orig(session, board_id)

            monkeypatch.setattr(bs, "_get_row", fake_get_row)
            ok = await bs.acquire_lease(s_b, "b_race", "B", wait_s=0, steal=False)
        assert ok is False, "A's live lease blocks B; the raced insert must not raise"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# Persist KPIs

@pytest.mark.asyncio
async def test_turn_metrics_saved(monkeypatch, _offline):
    from sqlalchemy import select

    from app.persistence.models import TurnMetrics
    from app.persistence.repo import save_turn_metrics
    from tests.test_lifecycle_scenarios import _ack, _rig, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)
    h = await _start_lesson(r)
    _ack(r, 0)
    h.release.set()
    await settle()

    turn_id = r.mgr.state.active_turn_id
    board_id = r.mgr.state.board_id
    async with _offline() as s:
        rows = list((await s.execute(select(TurnMetrics))).scalars().all())
    assert len(rows) == 1 and rows[0].turn_id == turn_id
    assert rows[0].board_id == board_id
    assert rows[0].kpis["steps"] == len(r.mgr.state.steps_sent) == 4
    assert rows[0].kpis["heardSteps"] == 1
    assert "turn_started" in rows[0].marks

    # Saving again for the same turn updates the row, it never duplicates it.
    async with _offline() as s:
        await save_turn_metrics(s, turn_id=turn_id, board_id=board_id,
                                marks={"turn_started": 1.0}, kpis={"steps": 0})
        rows = list((await s.execute(select(TurnMetrics))).scalars().all())
    assert len(rows) == 1 and rows[0].kpis["steps"] == 0


# ---------------------------------------------------------------------------------------------
# A refresh in the middle of a lesson restores the board and offers Continue
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refresh_mid_lesson_restores_board_and_continue(monkeypatch, _offline):
    """A refresh mid-lesson restores the board and offers Continue: heard progress flushes
    the board document (throttled) with a live checkpoint, so the new job reads a live
    checkpoint instead of the blank board + greeting a null board_state forces."""
    from app.contracts.messages import StepProgress
    from app.persistence import board_state as bs
    from tests.test_lifecycle_scenarios import _rig, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    monkeypatch.setattr(settings, "BOARD_FLUSH_THROTTLE_MS", 0, raising=False)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)
    await _start_lesson(r)                                   # speaking, held: no turn end
    st = r.mgr.state
    for idx in (0, 1):
        step = st.steps_sent[idx]
        r.mgr.on_step_progress(StepProgress(
            turn_id=st.active_turn_id, generation=st.generation, step_index=idx, event="completed",
            started_up_to=idx, heard_up_to=idx, drawn_op_ids=[op.op_id for op in step.ops],
            completed_by="words"))
    await settle()
    board_id = st.board_id
    acked = [op.op_id for step in st.steps_sent[:2] for op in step.ops]
    assert r.rec.of("turn_ended") == [], "precondition: the lesson is still playing"

    async with _offline() as s:                              # what the NEW job reads
        doc = await bs.load_board_state(s, board_id)
        pages = await bs.load_pages(s, board_id)
    assert doc is not None, "board_state written mid-turn"
    assert doc.paused_lesson is not None and doc.paused_lesson["resume_cursor"] == 2

    r2 = _rig(monkeypatch)
    rec2 = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec2)
    r2.mgr.rehydrate(doc, pages)
    await r2.mgr._send_board_snapshot()
    assert r2.mgr.can_continue(), "the reloaded tab offers Continue"
    snap = rec2.of("board_snapshot")[-1]
    assert [op.op_id for sp in snap.current.sub_pages for op in sp.ops] == acked
    assert r.mgr.state.paused_lesson is None, "the live job's own state is not paused"


@pytest.mark.asyncio
async def test_mid_turn_flush_is_throttled(monkeypatch, _offline):
    from app.contracts.messages import StepProgress
    from tests.test_lifecycle_scenarios import _rig, _start_lesson
    from tests.test_memory import FakeGateway

    monkeypatch.setattr(settings, "FEATURE_MEMORY", True)
    monkeypatch.setattr(settings, "BOARD_FLUSH_THROTTLE_MS", 60_000, raising=False)
    r = _rig(monkeypatch)
    r.mgr.gateway = FakeGateway(out=None)
    await _start_lesson(r)
    calls = {"n": 0}
    real = r.mgr._flush_board_state

    async def counting():
        calls["n"] += 1
        await real()

    r.mgr._flush_board_state = counting
    st = r.mgr.state
    for idx in range(3):
        r.mgr.on_step_progress(StepProgress(
            turn_id=st.active_turn_id, generation=st.generation, step_index=idx, event="completed",
            started_up_to=idx, heard_up_to=idx, drawn_op_ids=[], completed_by="words"))
    await settle()
    assert calls["n"] == 1, "one flush per throttle window, not one per report"
