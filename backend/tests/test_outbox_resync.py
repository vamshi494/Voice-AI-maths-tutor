# backend/tests/test_outbox_resync.py
"""Outbox/resync tests; cumulative step_progress starts here."""
import pytest

from app.contracts.messages import StepProgress
from tests.test_resume_parked import _progress_rig


@pytest.mark.asyncio
async def test_progress_is_cumulative():
    mgr, run = _progress_rig()
    for heard in (1, 3, 0):
        mgr.on_step_progress(StepProgress(turn_id="t1", generation=0, step_index=0, event="completed",
                                          completed_by="words", started_up_to=heard, heard_up_to=heard))
    assert run.heard_upto == 3, "a later smaller report never regresses the cursor"
    assert mgr.state.heard_step_index == 3


@pytest.mark.asyncio
async def test_duplicate_progress_idempotent():
    mgr, run = _progress_rig()
    report = StepProgress(turn_id="t1", generation=0, step_index=2, event="completed",
                          completed_by="words", started_up_to=2, heard_up_to=2)
    mgr.on_step_progress(report)
    mgr.on_step_progress(report)
    assert run.heard_upto == 2
    assert mgr.state.heard_step_index == 2


# ---------------------------------------------------------------------------------------------
# Outbox and resync

import json

import app.state_machine.manager as mgr_mod
from app.contracts.agent_state import PageRecord, TurnRequest
from app.contracts.board_ops import BoardOp, Step
from app.contracts.messages import ErrorNotice, ResyncRequest
from app.outbox import Outbox, get_outbox
from app.state_machine.manager import StateMachineManager
from app.state_machine.states import ConvState
from app.tutor.board_rows import BoardRowTracker
from app.tutor.stream_parser import normalize_words
from tests.fakes import EventRecorder, settle


class _FakeParticipant:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.texts: list[str] = []
        self.fail = fail

    async def publish_data(self, data: bytes, topic=None, reliable=None) -> None:
        if self.fail:
            raise RuntimeError("send failure")
        self.sent.append(data.decode("utf-8"))

    async def send_text(self, text: str, topic=None) -> None:
        if self.fail:
            raise RuntimeError("send failure")
        self.texts.append(text)


class _FakeRoom:
    def __init__(self, fail: bool = False) -> None:
        self.local_participant = _FakeParticipant(fail)


@pytest.mark.asyncio
async def test_resync_replays_missing():
    outbox = Outbox()
    room = _FakeRoom()
    for i in range(3):
        await outbox.send(ErrorNotice(generation=1, message=f"m{i}"), room=room)
    room.local_participant.sent.clear()

    assert await outbox.resend_from(2, room=room) is True
    assert [json.loads(p)["seq"] for p in room.local_participant.sent] == [2, 3]


@pytest.mark.asyncio
async def test_resync_falls_back_to_snapshot(monkeypatch):
    rec = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec)
    mgr = StateMachineManager(session=None)
    page = PageRecord(board_id="b", page_id="p1", lesson_question="q")
    mgr.state.page = page
    w1 = BoardOp(op_id="t:0:0", kind="WRITE", at_word=0, text="a = 1", row_id="w1")
    step = Step(turn_id="t", generation=1, step_index=0, spoken_text="a",
                words=normalize_words("a"), ops=[w1])
    mgr.ledger.on_published("p1", step)
    mgr.ledger.on_acked(["t:0:0"])

    await mgr.on_resync_request(ResyncRequest(epoch="deadbeef", last_seq=0))

    snaps = rec.of("board_snapshot")
    assert len(snaps) == 1
    assert snaps[0].current.page_id == "p1"
    assert [op.op_id for sp in snaps[0].current.sub_pages for op in sp.ops] == ["t:0:0"]


@pytest.mark.asyncio
async def test_failed_send_kept_in_outbox():
    outbox = Outbox()
    room = _FakeRoom(fail=True)
    assert await outbox.send(ErrorNotice(generation=1, message="x"), room=room) == "failed"
    assert [seq for seq, _p in outbox.buf] == [1]

    room.local_participant.fail = False
    assert await outbox.resend_from(1, room=room) is True
    assert len(room.local_participant.sent) == 1


@pytest.mark.asyncio
async def test_large_event_routing():
    outbox = Outbox()
    room = _FakeRoom()
    path = await outbox.send(ErrorNotice(generation=1, message="x" * 20000), room=room)
    assert path == "stream"
    assert room.local_participant.texts and not room.local_participant.sent


@pytest.mark.asyncio
async def test_reconnect_during_speech_replays_cursor(monkeypatch):
    rec = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec)
    mgr = StateMachineManager(session=None)
    req = TurnRequest(kind="lesson", generation=0, turn_id="t1", question="q")
    page = PageRecord(board_id="b", page_id="p1", lesson_question="q")
    run = mgr.lesson_runner.new_run(req, page)   # producer fails offline; silence it
    mgr.lesson_runner.active = run
    mgr.state.active_turn_id = "t1"
    mgr.state.active_turn_kind = "lesson"
    mgr.state.conv_state = ConvState.AGENT_SPEAKING

    await mgr.on_resync_request(ResyncRequest(epoch=get_outbox().epoch, last_seq=10**6,
                                              reason="reconnected"))

    assert rec.of("board_snapshot") == []
    assert [e.step_index for e in rec.of("replay_from_step")] == [0]


@pytest.mark.asyncio
async def test_buffer_eviction():
    outbox = Outbox(capacity=2, max_bytes=10**9)
    room = _FakeRoom()
    for i in range(3):
        await outbox.send(ErrorNotice(generation=1, message=f"m{i}"), room=room)

    assert [seq for seq, _p in outbox.buf] == [2, 3]
    assert await outbox.resend_from(1) is False, "an evicted event makes a resend impossible"
    assert await outbox.resend_from(2) is True


@pytest.mark.asyncio
async def test_resync_request_report_is_scheduled(monkeypatch):
    """Regression: the report handler must schedule the async manager handler."""
    import app.transport as transport
    from app.config import settings

    monkeypatch.setattr(settings, "FEATURE_OUTBOX", True)
    rec = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec)
    transport.set_active_room(None)
    mgr = StateMachineManager(session=None)
    mgr.state.page = PageRecord(board_id="b", page_id="p1", lesson_question="q")

    transport.handle_report_payload(
        '{"type":"resync_request","epoch":"deadbeef","lastSeq":0,"reason":"gap"}', mgr)
    await settle()

    assert len(rec.of("board_snapshot")) == 1


@pytest.mark.asyncio
async def test_resync_request_ignored_without_flag(monkeypatch):
    """Regression: FEATURE_OUTBOX=False keeps the plain send path (no resync handling)."""
    import app.transport as transport
    from app.config import settings

    monkeypatch.setattr(settings, "FEATURE_OUTBOX", False)
    rec = EventRecorder()
    monkeypatch.setattr(mgr_mod, "send_event", rec)
    transport.set_active_room(None)
    mgr = StateMachineManager(session=None)
    mgr.state.page = PageRecord(board_id="b", page_id="p1", lesson_question="q")

    transport.handle_report_payload(
        '{"type":"resync_request","epoch":"deadbeef","lastSeq":0,"reason":"gap"}', mgr)
    await settle()

    assert rec.of("board_snapshot") == []


@pytest.mark.asyncio
async def test_outbox_flag_routes_events(monkeypatch):
    """Regression: with the flag on, send_event stamps and buffers through the outbox."""
    import app.transport as transport
    from app.config import settings

    monkeypatch.setattr(settings, "FEATURE_OUTBOX", True)
    room = _FakeRoom()
    transport.set_active_room(room)
    outbox = get_outbox()

    await transport.send_event(ErrorNotice(generation=1, message="one"), room=room)

    payload = json.loads(room.local_participant.sent[-1])
    assert payload["seq"] == 1 and len(payload["epoch"]) == 8
    assert [seq for seq, _p in outbox.buf] == [1]
