import sys
from pathlib import Path

# Add backend directory to sys.path so app modules import cleanly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from livekit.agents import (
    JobContext,
    WorkerOptions,
    cli,
)
from app.observability import log_event, setup_observability
from app.config import settings
from app.persistence.db import init_db
from app.prompts.registry import greeting_line, welcome_back_line
from app.state_machine.manager import StateMachineManager
from app.state_machine.states import ConvEvent
from app.voice.session import prewarm, start_session

setup_observability()


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit agent worker entrypoint function."""
    await ctx.connect()
    try:
        await init_db()
    except Exception as e:
        log_event("db_init_failed", error=str(e))
    log_event("worker_connected_to_room", room=ctx.room.name if ctx.room else "unknown")
    if ctx.room:
        from app.transport import set_active_room
        set_active_room(ctx.room)

    # Construct state machine manager
    manager = StateMachineManager()
    manager.room = ctx.room

    # Start voice session
    session = await start_session(ctx, manager)
    manager.session = session

    # Wire session event listeners for conversation state changes
    session.on(
        "user_state_changed",
        lambda e: manager.fire(ConvEvent.VAD_START) if getattr(e, "new_state", None) == "speaking" else None,
    )
    session.on(
        "agent_state_changed",
        lambda e: manager.fire(ConvEvent.SPEECH_STARTED) if getattr(e, "new_state", None) == "speaking" else None,
    )
    # An unrecoverable TTS error switches the turn to captions.
    session.on("error", manager.on_session_error)

    # Flush the in-flight turn when the job ends (student closed the tab / room emptied).
    try:
        ctx.add_shutdown_callback(manager.shutdown)
    except Exception as e:
        log_event("shutdown_hook_unavailable", error=str(e))

    log_event("agent_ready", room=ctx.room.name if ctx.room else "unknown")

    # Speak greeting and restore board when student connects
    restored = False

    async def _rehydrate_board(board_id: str, user_name: str = "") -> bool:
        """Lease + document → manager.rehydrate → board_snapshot → welcome aside.

        Returns False when no persisted state exists, so the caller keeps the legacy
        greeting path.
        """
        from app.persistence import board_state as bs
        from app.persistence.db import async_session
        from app.transport import send_event

        job = getattr(ctx, "job", None)
        manager.lease_owner = str(getattr(job, "id", "") or manager.lease_owner)
        manager.state.board_id = board_id
        doc = None
        pages: list = []
        try:
            await manager.start_lease()
            async with async_session() as session:
                doc = (await bs.load_board_state(session, board_id)
                       or await bs.build_legacy_doc(session, board_id))
                if doc is not None:
                    pages = await bs.load_pages(session, board_id)
        except Exception as e:
            log_event("board_rehydrate_error", error=str(e))
        if doc is None:
            return False
        manager.rehydrate(doc, pages)
        await manager._send_board_snapshot()
        pl = manager.state.paused_lesson
        if manager.can_continue() and pl is not None:
            title = next((p.get("title") for p in pages
                          if p.get("page_id") == doc.current_page_id and p.get("title")), "")
            await manager.speak_aside(
                welcome_back_line(title or pl.lesson_question[:80], user_name), "welcome")
        else:
            await manager.speak_aside(greeting_line(user_name, returning=True), "greeting")
        log_event("board_rehydrated", board_id=board_id, page_id=doc.current_page_id)
        return True

    async def _on_participant_ready(participant=None):
        nonlocal restored
        if restored:
            return
        restored = True
        try:
            import json
            from app.contracts.messages import ErrorNotice, PageRestore
            from app.transport import send_event

            await asyncio.sleep(0.8)
            board_id = None
            user_name = ""
            if participant and getattr(participant, "metadata", None):
                try:
                    meta = json.loads(participant.metadata)
                    board_id = meta.get("board_id")
                    user_name = meta.get("user_name") or ""
                except Exception:
                    pass

            if not board_id and ctx.room and ctx.room.remote_participants:
                for p in ctx.room.remote_participants.values():
                    if getattr(p, "metadata", None):
                        try:
                            meta = json.loads(p.metadata)
                            board_id = meta.get("board_id")
                            user_name = user_name or meta.get("user_name") or ""
                            if board_id:
                                break
                        except Exception:
                            pass

            if board_id and settings.FEATURE_REHYDRATE:
                if await _rehydrate_board(board_id, user_name):
                    return

            has_history = False
            if board_id:
                manager.state.board_id = board_id
                try:
                    from app.persistence.db import async_session
                    from app.persistence.repo import get_board, get_stored_ops_for_page
                    async with async_session() as s:
                        board_record = await get_board(s, board_id)
                        if board_record and board_record.turns:
                            latest_turn = board_record.turns[-1]
                            artifacts = latest_turn.get_validated_artifacts()
                            if artifacts and artifacts.page_id:
                                ops = await get_stored_ops_for_page(s, artifacts.page_id)
                                diagram = artifacts.verified_diagram
                                await send_event(PageRestore(
                                    generation=manager.state.generation,
                                    page_id=artifacts.page_id,
                                    diagram=diagram,
                                    ops=ops,
                                ))
                                has_history = True
                                log_event("board_auto_restored", board_id=board_id, page_id=artifacts.page_id, ops_count=len(ops))
                except Exception as e:
                    log_event("board_restore_error", error=str(e))

            if has_history:
                await send_event(ErrorNotice(
                    generation=manager.state.generation,
                    message="Welcome back! Your previous board has been restored. You can ask a new question or continue.",
                ))
                await manager.speak_aside(greeting_line(user_name, returning=True), "welcome")
            else:
                await send_event(ErrorNotice(
                    generation=manager.state.generation,
                    message="Welcome to AI Math Tutor! Ask any math question by speaking or typing below.",
                ))
                await manager.speak_aside(greeting_line(user_name), "greeting")
        except Exception as e:
            log_event("greeting_error", error=str(e))

    if ctx.room and ctx.room.remote_participants:
        asyncio.create_task(_on_participant_ready())
    elif ctx.room:
        ctx.room.on("participant_connected", lambda p: asyncio.create_task(_on_participant_ready(p)))


def main() -> None:
    """Start worker process with CLI runner."""
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )


if __name__ == "__main__":
    main()
