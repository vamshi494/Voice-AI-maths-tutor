# backend/tests/test_transport.py
import json
from unittest.mock import AsyncMock, MagicMock
import pytest

import app.transport as transport
from app.contracts.agent_state import AgentState, ConvState, PausedLesson
from app.contracts.messages import (
    BoardReport,
    BoardRow,
    RpcAck,
    RpcContinueLesson,
    RpcEndSession,
    RpcMarkerArmed,
    RpcMarkerDisarmed,
    RpcSetSpeed,
    RpcSubmitDoubt,
    RpcSubmitQuestion,
    StepAck,
)
from app.state_machine.manager import StateMachineManager
from app.transport import handle_report_payload, register_rpcs


@pytest.mark.asyncio
async def test_rpc_registration_and_dispatch():
    mock_participant = MagicMock()
    rpc_handlers = {}

    def mock_register_rpc(name, handler):
        rpc_handlers[name] = handler

    mock_participant.register_rpc_method = mock_register_rpc

    manager = StateMachineManager()
    manager.handle_event_ex = AsyncMock(return_value=(ConvState.GRAPH_RUNNING, True))

    register_rpcs(mock_participant, manager)

    # 1. submit_question
    inv = MagicMock()
    inv.payload = RpcSubmitQuestion(text="Solve x^2 - 4 = 0").model_dump_json()
    resp = await rpc_handlers["submit_question"](inv)
    ack = RpcAck.model_validate_json(resp)
    assert ack.ok is True
    manager.handle_event_ex.assert_awaited()

    # 2. submit_doubt
    inv.payload = RpcSubmitDoubt(typed_text="Explain step 1", marks=[]).model_dump_json()
    resp = await rpc_handlers["submit_doubt"](inv)
    ack = RpcAck.model_validate_json(resp)
    assert ack.ok is True

    # 3. set_speed with no speed-capable TTS -> rejected
    inv.payload = RpcSetSpeed(speed=1.2).model_dump_json()
    resp = await rpc_handlers["set_speed"](inv)
    ack = RpcAck.model_validate_json(resp)
    assert ack.ok is False and ack.reason == "speed_unsupported"
    assert manager.state.tts_speed == 1.2

    # 4. continue_lesson with a checkpoint
    manager.state.paused_lesson = PausedLesson(
        board_id="b", page_id="p", lesson_question="q", turn_plan=None,
        solver_projection=None, diagram=None, figure_drawn=False,
        lesson_turn_id="", last_acked_step_index=-1,
    )
    inv.payload = RpcContinueLesson().model_dump_json()
    resp = await rpc_handlers["continue_lesson"](inv)
    ack = RpcAck.model_validate_json(resp)
    assert ack.ok is True

    # 5. marker_armed & disarmed
    inv.payload = RpcMarkerArmed().model_dump_json()
    resp = await rpc_handlers["marker_armed"](inv)
    assert RpcAck.model_validate_json(resp).ok is True

    inv.payload = RpcMarkerDisarmed().model_dump_json()
    resp = await rpc_handlers["marker_disarmed"](inv)
    assert RpcAck.model_validate_json(resp).ok is True

    # 6. end_session
    inv.payload = RpcEndSession().model_dump_json()
    resp = await rpc_handlers["end_session"](inv)
    assert RpcAck.model_validate_json(resp).ok is True


class _FakeParticipant:
    def __init__(self):
        self.sent: list[str] = []

    async def publish_data(self, data, topic=None, reliable=None):
        self.sent.append(data.decode("utf-8"))


class _FakeRoom:
    def __init__(self):
        self.local_participant = _FakeParticipant()


@pytest.mark.asyncio
async def test_events_carry_epoch_and_seq():
    from app.contracts.messages import ErrorNotice

    room = _FakeRoom()
    transport.set_active_room(room)
    await transport.send_event(ErrorNotice(generation=1, message="one"), room=room)
    await transport.send_event(ErrorNotice(generation=1, message="two"), room=room)

    first = json.loads(room.local_participant.sent[0])
    second = json.loads(room.local_participant.sent[1])
    assert len(first["epoch"]) == 8 and len(second["epoch"]) == 8
    assert first["epoch"] == second["epoch"]
    assert first["seq"] == 1 and second["seq"] == 2


@pytest.mark.asyncio
async def test_set_active_room_resets_seq_and_epoch():
    from app.contracts.messages import ErrorNotice

    room_a = _FakeRoom()
    transport.set_active_room(room_a)
    await transport.send_event(ErrorNotice(generation=1, message="one"), room=room_a)
    epoch_a = json.loads(room_a.local_participant.sent[0])["epoch"]

    room_b = _FakeRoom()
    transport.set_active_room(room_b)
    await transport.send_event(ErrorNotice(generation=1, message="one"), room=room_b)
    payload = json.loads(room_b.local_participant.sent[0])
    assert payload["seq"] == 1
    assert payload["epoch"] != epoch_a


def test_handle_report_payload_step_ack():
    manager = StateMachineManager()
    manager.state.generation = 1
    manager.state.active_turn_id = "t1"     # acks are bound to the ACTIVE turn

    # Valid matching generation
    ack_payload = StepAck(
        turn_id="t1",
        generation=1,
        step_index=2,
        drawn_op_ids=["op_1", "op_2"],
    ).model_dump_json(by_alias=True)

    handle_report_payload(ack_payload, manager)
    assert manager.state.last_acked_step_index == 2
    assert "op_1" in manager.state.acked_op_ids
    assert "op_2" in manager.state.acked_op_ids

    # Stale generation is ignored
    stale_payload = StepAck(
        turn_id="t1",
        generation=0,
        step_index=5,
        drawn_op_ids=["op_99"],
    ).model_dump_json(by_alias=True)

    handle_report_payload(stale_payload, manager)
    # Shouldn't be updated
    assert manager.state.last_acked_step_index == 2
    assert "op_99" not in manager.state.acked_op_ids


def test_handle_report_payload_board_report():
    manager = StateMachineManager()
    rpt_payload = BoardReport(
        page_id="p1",
        rows=[BoardRow(row_id="w1", text="x = 5")],
        rows_remaining=12,
    ).model_dump_json(by_alias=True)

    handle_report_payload(rpt_payload, manager)
    assert manager.state.rows is not None
    assert manager.state.rows.page_id == "p1"
    assert manager.state.rows.rows_remaining == 12
    assert len(manager.state.rows.rows) == 1


# ---------------------------------------------------------------------------------------------
# A large page_commit over the text-stream path, in order, and after resync
# ---------------------------------------------------------------------------------------------
class _RecordingParticipant:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def publish_data(self, data, topic=None, reliable=None):
        self.calls.append(("packet", json.loads(data.decode("utf-8"))))

    async def send_text(self, text, topic=None):
        self.calls.append(("stream", json.loads(text)))


class _RecordingRoom:
    def __init__(self) -> None:
        self.local_participant = _RecordingParticipant()


def _large_page_commit():
    from app.contracts.diagram import DiagramCommand
    from app.contracts.messages import Block, PageCommit, Rect

    commands = [DiagramCommand(type="DRAW_LINE",
                               params=[float(i), float(i + 1), float(i + 2), float(i + 3)])
                for i in range(400)]
    blocks = [Block(id=f"fig{i}", role="figure",
                    rect=Rect(x=0, y=0, width=100, height=100), commands=commands, anchors=[])
              for i in range(4)]
    return PageCommit(generation=1, turn_id="t", page_id="L_p0", commit_id="c1",
                      work_rect=Rect(x=40, y=72, width=340, height=608), blocks=blocks)


@pytest.mark.asyncio
async def test_large_page_commit_streams_before_steps_and_resyncs(monkeypatch):
    from app import outbox as outbox_mod
    from app.config import settings
    from app.contracts.messages import ResyncRequest, StepEvt
    from tests.fakes import make_step

    monkeypatch.setattr(settings, "FEATURE_OUTBOX", True)
    room = _RecordingRoom()
    transport.set_active_room(room)

    commit = _large_page_commit()
    size = len(commit.model_dump_json(by_alias=True, exclude_none=True).encode())
    assert size > transport.PACKET_LIMIT_BYTES, "the fixture must exceed the packet cap"

    path = await transport.send_event(commit, room=room)
    await transport.send_event(StepEvt(generation=1, step=make_step("t", 1, 0, "look", write="row")),
                               room=room)

    assert path == "stream"
    assert [c[0] for c in room.local_participant.calls] == ["stream", "packet"], \
        "the commit goes by text stream and arrives before the first step"
    assert [c[1]["type"] for c in room.local_participant.calls] == ["page_commit", "step"]
    assert [c[1]["seq"] for c in room.local_participant.calls] == [1, 2]

    # A gap resync from seq 1 replays both in order, on the same paths (manager path included).
    room.local_participant.calls.clear()
    mgr = StateMachineManager(session=None)
    await mgr.on_resync_request(ResyncRequest(epoch=outbox_mod.get_outbox().epoch, last_seq=0,
                                              reason="gap"))
    assert [c[0] for c in room.local_participant.calls] == ["stream", "packet"]
    assert [c[1]["seq"] for c in room.local_participant.calls] == [1, 2]
