# app/persistence/repo.py
"""Database repository for Boards, Turns, and Segments.

- save_turn with idempotency guard (turn_id) and SELECT FOR UPDATE / atomic ordering.
- Only segments whose ops were acknowledged by step_ack are written.
- SceneArtifacts validation on write and read.
- Board reopening helpers.
"""
from typing import Any, Literal
from uuid import uuid4
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts.agent_state import AgentState
from app.contracts.board_ops import Step
from app.contracts.diagram import VerifiedDiagram
from app.contracts.scene import SceneDocument, ValidationReport
from app.contracts.turn_plan import TurnPlan
from app.observability import log_event, logger
from app.persistence.db import async_session
from app.persistence.models import Board, SceneArtifacts, Segment, Turn, TurnMetrics


async def get_or_create_board(session: AsyncSession, board_id: str, user_id: str, title: str = "new board") -> Board:
    stmt = select(Board).where(Board.id == board_id)
    res = await session.execute(stmt)
    board = res.scalar_one_or_none()
    if board is not None:
        return board

    try:
        async with session.begin_nested():
            board = Board(id=board_id, user_id=user_id, title=title)
            session.add(board)
            await session.flush()
        return board
    except Exception:
        res = await session.execute(stmt)
        board = res.scalar_one_or_none()
        if board is not None:
            return board
        raise


async def get_latest_board_for_user(session: AsyncSession, user_id: str) -> Board | None:
    """The user's most recently USED, non-archived board (name login; the token route stamps
    updated_at on every login, with sub-second precision)."""
    stmt = (select(Board).where(Board.user_id == user_id, Board.archived_at.is_(None))
            .order_by(Board.updated_at.desc(), Board.created_at.desc()).limit(1))
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def save_turn(
    session: AsyncSession,
    *,
    turn_id: str,
    board_id: str,
    user_id: str,
    question: str,
    raw_response: str = "",
    speed_multiplier: float = 1.0,
    visual_status: Literal["validated", "text_only", "retry_required"] = "text_only",
    kind: Literal["lesson", "doubt", "resume"] = "lesson",
    page_id: str,
    continues_board: bool = False,
    status: Literal["complete", "partial"] = "complete",
    turn_plan: TurnPlan | None = None,
    solver_projection: dict[str, Any] | None = None,
    verified_diagram: VerifiedDiagram | None = None,
    paused_note: str | None = None,
    scene_document: SceneDocument | None = None,
    validation_report: ValidationReport | None = None,
    trace_id: str | None = None,
    segments_data: list[dict[str, Any]] | None = None,
) -> Turn:
    """Save a completed or partial turn idempotently."""
    # 1. Idempotency guard
    existing_stmt = select(Turn).where(Turn.id == turn_id)
    existing_res = await session.execute(existing_stmt)
    existing_turn = existing_res.scalar_one_or_none()
    if existing_turn is not None:
        logger.info(f"Turn {turn_id} already saved, skipping duplicate save")
        return existing_turn

    # 2. Ensure board exists
    board = await get_or_create_board(session, board_id, user_id)

    # 3. Determine next order_index and insert. Two concurrent turns can pick the same
    # index; the insert retries the (board_id, order_index) unique violation up to 3 attempts.
    artifacts = SceneArtifacts(
        kind=kind,
        page_id=page_id,
        continues_board=continues_board,
        status=status,
        turn_plan=turn_plan,
        solver_projection=solver_projection,
        verified_diagram=verified_diagram,
        paused_note=paused_note,
    )
    idx_stmt = select(func.coalesce(func.max(Turn.order_index), -1) + 1).where(Turn.board_id == board_id)
    turn: Turn | None = None
    for attempt in range(3):
        try:
            async with session.begin_nested():
                idx_res = await session.execute(idx_stmt)
                order_index = idx_res.scalar_one()

                turn = Turn(
                    id=turn_id,
                    board_id=board_id,
                    user_id=user_id,
                    order_index=order_index,
                    question=question,
                    raw_response=raw_response,
                    speed_multiplier=speed_multiplier,
                    trace_id=trace_id,
                    visual_status=visual_status,
                )
                turn.validate_and_set_artifacts(artifacts)
                if scene_document:
                    turn.validate_and_set_scene_document(scene_document)
                if validation_report:
                    turn.validate_and_set_validation_report(validation_report)

                session.add(turn)
                if segments_data:
                    for idx, seg in enumerate(segments_data):
                        segment = Segment(
                            id=str(uuid4()),
                            turn_id=turn_id,
                            order_index=idx,
                            narration=seg.get("narration", ""),
                            spoken_text=seg.get("spoken_text", ""),
                            command=seg.get("command", []),
                            duration_ms=seg.get("duration_ms"),
                            timings=seg.get("timings"),
                        )
                        session.add(segment)
                await session.flush()
            break
        except IntegrityError as e:
            if "order_index" not in str(e) or attempt == 2:
                raise
            log_event("order_index_retry", board_id=board_id, attempt=attempt + 1)

    assert turn is not None
    board.preview = question[:100]
    await session.commit()
    log_event("turn_saved", turn_id=turn_id, status=status, order_index=turn.order_index)
    return turn


async def persist_turn_from_state(
    state: AgentState,
    *,
    status: Literal["complete", "partial"],
    paused_note: str | None = None,
    marks: dict[str, float] | None = None,
    kpis: dict[str, Any] | None = None,
) -> bool:
    """Save turn from in-memory AgentState using async DB session."""
    if not state.active_turn_id:
        return False

    turn_id = state.active_turn_id
    page = state.page
    if not page:
        return False

    # Filter segments to only acknowledged steps/ops
    segments_data: list[dict[str, Any]] = []
    for step_idx, step in enumerate(state.steps_sent):
        if step_idx <= state.last_acked_step_index or status == "complete":
            # Filter ops to drawn/acked ops
            drawn_ops = [
                op.model_dump(by_alias=True)
                for op in step.ops
                if op.op_id in state.acked_op_ids or status == "complete"
            ]
            segments_data.append({
                "narration": step.spoken_text,
                "spoken_text": step.spoken_text,
                "command": drawn_ops,
            })

    req = state.pending_request
    question = (req.student_text if req and req.student_text else None) or page.lesson_question
    heard_steps = state.steps_sent[: state.last_acked_step_index + 1]
    raw_response = " ".join(s.spoken_text for s in heard_steps)
    visual_status = page.visual_status
    # The ACTIVE TURN's kind, not the page's: a same-board doubt never replaces the page, so
    # page.turn_kind stayed "lesson" and every doubt was stored as a lesson with
    # continues_board=False -- which inheritedTeachingContext Case B relies on to skip it.
    kind = state.active_turn_kind or page.turn_kind
    page_id = page.page_id
    continues_board = kind != "lesson"

    async with async_session() as session:
        try:
            await save_turn(
                session,
                turn_id=turn_id,
                board_id=state.board_id,
                user_id=state.user_id,
                question=question,
                speed_multiplier=state.tts_speed,
                visual_status=visual_status,
                kind=kind,
                page_id=page_id,
                continues_board=continues_board,
                status=status,
                raw_response=raw_response,
                turn_plan=page.turn_plan,
                solver_projection=page.solver_projection,
                verified_diagram=page.diagram,
                paused_note=paused_note,
                segments_data=segments_data,
            )
            page.saved = True
            if marks is not None or kpis is not None:
                await save_turn_metrics(session, turn_id=turn_id, board_id=state.board_id,
                                        marks=marks, kpis=kpis)
            return True
        except Exception as e:
            logger.error(f"Failed to persist turn {turn_id}: {e}")
            return False


async def save_turn_metrics(
    session: AsyncSession,
    *,
    turn_id: str,
    board_id: str,
    marks: dict[str, Any] | None = None,
    kpis: dict[str, Any] | None = None,
) -> None:
    """One `turn_metrics` row per turn; idempotent upsert by turn_id."""
    row = await session.get(TurnMetrics, turn_id)
    if row is None:
        row = TurnMetrics(turn_id=turn_id, board_id=board_id, marks=marks or {}, kpis=kpis or {})
        session.add(row)
    else:
        if marks is not None:
            row.marks = marks
        if kpis is not None:
            row.kpis = kpis
    await session.commit()


async def get_board(session: AsyncSession, board_id: str) -> Board | None:
    """Fetch board with all turns and segments loaded."""
    stmt = (
        select(Board)
        .where(Board.id == board_id)
        .options(selectinload(Board.turns).selectinload(Turn.segments))
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def list_boards(session: AsyncSession, user_id: str) -> list[Board]:
    """List boards for user, sorted by updated_at desc."""
    stmt = (
        select(Board)
        .where(Board.user_id == user_id, Board.archived_at.is_(None))
        .order_by(Board.updated_at.desc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_stored_ops_for_page(session: AsyncSession, page_id: str) -> list[dict[str, Any]]:
    """Retrieve all drawn BoardOps for a given page from saved segments."""
    # SceneArtifacts is stored with by_alias=True, so the JSON key is "pageId"; looking up
    # the snake_case "page_id" never matches.
    stmt = select(Turn).where(Turn.scene_artifacts["pageId"].as_string() == page_id).order_by(Turn.order_index)
    res = await session.execute(stmt)
    turns = list(res.scalars().all())
    ops: list[dict[str, Any]] = []
    for turn in turns:
        seg_stmt = select(Segment).where(Segment.turn_id == turn.id).order_by(Segment.order_index)
        seg_res = await session.execute(seg_stmt)
        for seg in seg_res.scalars().all():
            if seg.command and isinstance(seg.command, list):
                ops.extend(seg.command)
    return ops
