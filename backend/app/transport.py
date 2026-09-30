# app/transport.py
"""Transport layer for LiveKit events, RPC methods, and client report stream.

- send_event sends on topic 'tutor.events'.
- register_rpcs:
  - submit_question -> TYPED_QUESTION
  - submit_doubt -> MARKED_DOUBT
  - continue_lesson -> CONTINUE
  - marker_armed -> MARKER_ARMED
  - marker_disarmed -> MARKER_DISARMED
  - end_session -> END
  - set_speed -> updates mgr.state.tts_speed
- register_report_stream_handler on topic 'tutor.report':
  - step_ack -> updates last_acked_step_index, acked_op_ids, current_step_index
  - board_report -> updates st.rows
  - Stale generation reports are ignored.
"""
import asyncio
import json
from typing import Any
from uuid import uuid4

from app.contracts.agent_state import RowMirror
from app.contracts.base import CamelModel
from app.contracts.messages import (
    BoardReport,
    PageTurned,
    ResyncRequest,
    RpcAck,
    RpcContinueLesson,
    RpcEndSession,
    RpcHold,
    RpcMarkerArmed,
    RpcMarkerDisarmed,
    RpcSetSpeed,
    RpcSubmitDoubt,
    RpcSubmitQuestion,
    StepAck,
    StepProgress,
)
from app.config import settings
from app.observability import log_event, logger
from app.state_machine.states import ConvEvent


_active_room: Any = None
# Legacy (FEATURE_OUTBOX=False) event book: direct epoch/seq stamping without a ring buffer.
_epoch = uuid4().hex[:8]
_seq = 0


def set_active_room(room: Any) -> None:
    """Set the active LiveKit room; a new room starts a new epoch/sequence (and outbox)."""
    global _active_room, _epoch, _seq
    _active_room = room
    _epoch = uuid4().hex[:8]
    _seq = 0
    from app.outbox import reset_outbox
    reset_outbox()


def get_active_room() -> Any:
    global _active_room
    return _active_room


# LiveKit caps a single reliable data packet at ~15 KiB; leave headroom for the envelope.
PACKET_LIMIT_BYTES = 14 * 1024


def _resolve_room(room: Any) -> Any:
    r = room or _active_room
    if r is None:
        try:
            from livekit.agents import get_job_context
            ctx = get_job_context(required=False)
            if ctx:
                r = ctx.room
        except Exception:
            pass
    return r


async def send_event(evt: CamelModel, room: Any = None) -> str:
    """Publish a server event on topic 'tutor.events'.

    Exactly ONE path per event: <= 14 KiB -> publish_data, larger -> send_text. Every event
    carries a per-job (epoch, seq) so the client can order and de-duplicate.

    FEATURE_OUTBOX=False keeps the direct sender (stamp + one path, no ring buffer, no resync);
    True routes through the outbox.
    """
    if settings.FEATURE_OUTBOX:
        from app.outbox import send_event as _outbox_send
        return await _outbox_send(evt, room)
    return await _legacy_send_event(evt, room)


async def _legacy_send_event(evt: CamelModel, room: Any = None) -> str:
    """Direct send: stamp (epoch, seq), route by size, no buffer."""
    global _seq
    updates: dict[str, Any] = {}
    if hasattr(evt, "seq") and getattr(evt, "seq", None) is None:
        _seq += 1
        updates["seq"] = _seq
    if hasattr(evt, "epoch") and getattr(evt, "epoch", None) is None:
        updates["epoch"] = _epoch
    if updates:
        try:
            evt = evt.model_copy(update=updates)
        except Exception:
            pass
    r = _resolve_room(room)
    if not (r and getattr(r, "local_participant", None)):
        log_event("transport_send_event_offline", event_type=type(evt).__name__)
        return "offline"
    payload_str = evt.model_dump_json(by_alias=True, exclude_none=True)
    payload_bytes = payload_str.encode("utf-8")
    try:
        if len(payload_bytes) <= PACKET_LIMIT_BYTES:
            await r.local_participant.publish_data(payload_bytes, topic="tutor.events", reliable=True)
            return "packet"
    except Exception as e:
        logger.warning(f"publish_data failed for {type(evt).__name__} ({len(payload_bytes)} B): {e}; using text stream")
    try:
        await r.local_participant.send_text(payload_str, topic="tutor.events")
        log_event("transport_large_event_streamed", event_type=type(evt).__name__, bytes=len(payload_bytes))
        return "stream"
    except Exception as e:
        logger.error(f"Failed to publish event {type(evt).__name__}: {e}")
        log_event("transport_send_failed", event_type=type(evt).__name__, bytes=len(payload_bytes), error=str(e))
        return "failed"


def register_rpcs(local_participant: Any, manager: Any) -> None:
    """Register all client RPC handlers on the local participant.

    An RPC answers whether its transition actually applied. `ok` is true only when a
    transition rule ran; the client reverts its optimistic UI on `ok=false`.
    """

    async def _dispatch(event: ConvEvent, **kwargs: Any) -> RpcAck:
        from app.contracts.agent_state import ConvState
        if getattr(manager.state, "conv_state", None) == ConvState.TASK_CANCELLED:
            return RpcAck(ok=False, reason="session_ended")
        _, matched = await manager.handle_event_ex(event, **kwargs)
        return RpcAck(ok=matched, reason=None if matched else "not_now")

    async def _handle_submit_question(data: Any) -> str:
        try:
            req = RpcSubmitQuestion.model_validate_json(data.payload or "{}")
            # A new lesson starts for intent new/topic or when no lesson is on the board;
            # otherwise the text is classified like speech.
            if req.intent in ("new", "topic") or getattr(manager.state, "page", None) is None:
                ack = await _dispatch(ConvEvent.TYPED_QUESTION, text=req.text, intent=req.intent)
            else:
                ack = await _dispatch(ConvEvent.TYPED_TEXT, text=req.text)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling submit_question: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_submit_doubt(data: Any) -> str:
        try:
            req = RpcSubmitDoubt.model_validate_json(data.payload or "{}")
            ack = await _dispatch(ConvEvent.MARKED_DOUBT, typed_text=req.typed_text, marks=req.marks)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling submit_doubt: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_continue_lesson(data: Any) -> str:
        try:
            RpcContinueLesson.model_validate_json(data.payload or "{}")
            from app.contracts.agent_state import ConvState
            if getattr(manager.state, "conv_state", None) == ConvState.TASK_CANCELLED:
                return RpcAck(ok=False, reason="session_ended").model_dump_json(by_alias=True)
            if hasattr(manager, "can_continue") and not manager.can_continue():
                return RpcAck(ok=False, reason="nothing_to_continue").model_dump_json(by_alias=True)
            ack = await _dispatch(ConvEvent.CONTINUE)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling continue_lesson: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_marker_armed(data: Any) -> str:
        try:
            RpcMarkerArmed.model_validate_json(data.payload or "{}")
            ack = await _dispatch(ConvEvent.MARKER_ARMED)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling marker_armed: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_marker_disarmed(data: Any) -> str:
        try:
            RpcMarkerDisarmed.model_validate_json(data.payload or "{}")
            ack = await _dispatch(ConvEvent.MARKER_DISARMED)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling marker_disarmed: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_hold(data: Any) -> str:
        try:
            req = RpcHold.model_validate_json(data.payload or "{}")
            ack = await _dispatch(ConvEvent.MARKER_ARMED, reason=req.reason)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling hold: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_release(data: Any) -> str:
        try:
            req = RpcHold.model_validate_json(data.payload or "{}")
            ack = await _dispatch(ConvEvent.MARKER_DISARMED, reason=req.reason)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling release: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_set_speed(data: Any) -> str:
        try:
            req = RpcSetSpeed.model_validate_json(data.payload or "{}")
            manager.state.tts_speed = req.speed
            applied = manager.set_speed(req.speed) if hasattr(manager, "set_speed") else False
            log_event("tts_speed_updated", speed=req.speed, applied=applied)
            if not applied:
                return RpcAck(ok=False, reason="speed_unsupported").model_dump_json(by_alias=True)
            return RpcAck(ok=True).model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling set_speed: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_end_session(data: Any) -> str:
        try:
            RpcEndSession.model_validate_json(data.payload or "{}")
            ack = await _dispatch(ConvEvent.END)
            return ack.model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling end_session: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    async def _handle_set_pointer_context(data: Any) -> str:
        try:
            payload = json.loads(data.payload or "{}")
            if hasattr(manager, "set_pointer_context"):
                manager.set_pointer_context(
                    target=payload.get("target"),
                    marks=payload.get("marks", []),
                )
            return RpcAck(ok=True).model_dump_json(by_alias=True)
        except Exception as e:
            logger.error(f"Error handling set_pointer_context: {e}")
            return RpcAck(ok=False).model_dump_json(by_alias=True)

    local_participant.register_rpc_method("submit_question", _handle_submit_question)
    local_participant.register_rpc_method("submit_doubt", _handle_submit_doubt)
    local_participant.register_rpc_method("continue_lesson", _handle_continue_lesson)
    local_participant.register_rpc_method("hold", _handle_hold)
    local_participant.register_rpc_method("release", _handle_release)
    local_participant.register_rpc_method("marker_armed", _handle_marker_armed)
    local_participant.register_rpc_method("marker_disarmed", _handle_marker_disarmed)
    local_participant.register_rpc_method("set_speed", _handle_set_speed)
    local_participant.register_rpc_method("end_session", _handle_end_session)
    local_participant.register_rpc_method("set_pointer_context", _handle_set_pointer_context)
    log_event("rpcs_registered")


def handle_report_payload(payload_str: str, manager: Any) -> None:
    """Parse and apply report payload synchronously/in-task."""
    try:
        data = json.loads(payload_str)
        report_type = data.get("type")

        if report_type == "step_ack":
            ack = StepAck.model_validate(data)
            if hasattr(manager, "on_step_ack"):
                manager.on_step_ack(ack)           # generation + turn guarded inside
            elif ack.generation == manager.current_generation:
                manager.state.last_acked_step_index = max(manager.state.last_acked_step_index, ack.step_index)
                manager.state.acked_op_ids.update(ack.drawn_op_ids)
            log_event("step_ack_processed", step_index=ack.step_index, drawn_count=len(ack.drawn_op_ids))

        elif report_type == "step_progress":
            progress = StepProgress.model_validate(data)
            if hasattr(manager, "on_step_progress"):
                manager.on_step_progress(progress)  # generation + turn guarded inside
            log_event("step_progress_processed", event=progress.event,
                      step_index=progress.step_index, heard_up_to=progress.heard_up_to)

        elif report_type == "resync_request":
            req = ResyncRequest.model_validate(data)
            if settings.FEATURE_OUTBOX and hasattr(manager, "on_resync_request"):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    spawn = getattr(manager, "_spawn", None)
                    if spawn is not None:
                        spawn(manager.on_resync_request(req))
                    else:
                        loop.create_task(manager.on_resync_request(req))
                else:
                    # data_received normally fires on the loop; never leak an un-awaited coro.
                    log_event("resync_schedule_failed", reason="no_running_loop")
            log_event("resync_requested", reason=req.reason, last_seq=req.last_seq)

        elif report_type == "page_turned":
            turned = PageTurned.model_validate(data)
            if hasattr(manager, "on_page_turned"):
                manager.on_page_turned(turned)
            log_event("page_turned_processed", cause=turned.cause, to_sub=turned.to_sub_id,
                      at_op=turned.at_op_id)

        elif report_type == "board_report":
            rpt = BoardReport.model_validate(data)
            if hasattr(manager, "on_board_report"):
                manager.on_board_report(rpt)
            else:
                manager.state.rows = RowMirror(page_id=rpt.page_id, rows=[r.model_dump() for r in rpt.rows],
                                               rows_remaining=rpt.rows_remaining)
            log_event("board_report_processed", page_id=rpt.page_id, rows_count=len(rpt.rows))

        elif report_type == "pointer_focus":
            target = data.get("target")
            marks = data.get("marks", [])
            if hasattr(manager, "set_pointer_context"):
                manager.set_pointer_context(target=target, marks=marks)
            log_event(
                "pointer_focus_processed",
                target_id=target.get("id") if target else None,
                marks_count=len(marks),
            )

    except Exception as e:
        logger.error(f"Error handling report payload: {e}")


def register_report_stream_handler(room: Any, manager: Any) -> None:
    """Register report handlers for 'tutor.report' topic (both data packets and text streams)."""

    # 1. Data packet listener (handles publishData from browser)
    try:
        @room.on("data_received")
        def _on_data_received(packet: Any) -> None:
            try:
                topic = getattr(packet, "topic", None)
                if topic == "tutor.report":
                    raw_data = getattr(packet, "data", b"")
                    if isinstance(raw_data, bytes):
                        payload_str = raw_data.decode("utf-8")
                    else:
                        payload_str = str(raw_data)
                    handle_report_payload(payload_str, manager)
            except Exception as e:
                logger.error(f"Error handling tutor.report data packet: {e}")
    except Exception as e:
        logger.warning(f"Could not register data_received handler: {e}")

    # 2. Text stream listener (handles streamText if used)
    try:
        async def _handle_stream(reader: Any, participant_identity: str) -> None:
            try:
                content = await reader.read_all()
                handle_report_payload(content, manager)
            except Exception as e:
                logger.error(f"Error reading tutor.report stream: {e}")

        def _sync_handler(reader: Any, participant_identity: str) -> None:
            asyncio.create_task(_handle_stream(reader, participant_identity))

        if hasattr(room, "register_text_stream_handler"):
            room.register_text_stream_handler("tutor.report", _sync_handler)
    except Exception as e:
        logger.warning(f"Could not register text_stream handler: {e}")

    log_event("report_handlers_registered", topic="tutor.report")
