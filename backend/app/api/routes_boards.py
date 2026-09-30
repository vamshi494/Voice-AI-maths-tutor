# app/api/routes_boards.py
"""Board listing and board reopening routes.

- GET /boards: [{id, title, preview, updated_at}]
- GET /boards/{id}: {pages: [{page_id, diagram, ops}], can_continue_lesson, last_lesson_question}
  Built from turns in order_index; every JSON column is validated on read.
"""
from datetime import datetime
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.diagram import VerifiedDiagram
from app.contracts.messages import SnapshotPage
from app.observability import log_event
from app.persistence.board_state import load_snapshot
from app.persistence.db import get_db_session
from app.persistence.models import PageRow
from app.persistence.repo import get_board, list_boards

router = APIRouter()


class BoardSummary(BaseModel):
    id: str
    title: str
    preview: str
    updated_at: datetime | None = None


class BoardPageReopen(BaseModel):
    page_id: str
    diagram: VerifiedDiagram | None = None
    ops: list[dict[str, Any]] = []


class BoardReopenResponse(BaseModel):
    pages: list[BoardPageReopen]
    can_continue_lesson: bool
    last_lesson_question: str | None = None


@router.get("/boards", response_model=list[BoardSummary])
async def get_user_boards(
    x_user_id: str | None = Header(default="demo_student", alias="X-User-Id"),
    session: AsyncSession = Depends(get_db_session),
) -> list[BoardSummary]:
    user_id = x_user_id or "demo_student"
    boards = await list_boards(session, user_id)
    return [
        BoardSummary(
            id=b.id,
            title=b.title,
            preview=b.preview,
            updated_at=b.updated_at,
        )
        for b in boards
    ]


@router.get("/boards/{board_id}/pages/{page_id}", response_model=SnapshotPage)
async def get_page_snapshot(
    board_id: str,
    page_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> SnapshotPage:
    """A stored page (root or sub id) as a read-only SnapshotPage."""
    row = await session.get(PageRow, page_id)
    if row is not None:
        if row.board_id != board_id:
            raise HTTPException(status_code=404, detail="Page not found")
        root = row.root_page_id or row.page_id
    else:
        root = page_id
    snap = await load_snapshot(session, board_id, root)
    if snap is None:
        raise HTTPException(status_code=404, detail="Page not found")
    log_event("page_snapshot_read", board_id=board_id, page_id=root,
              sub_pages=len(snap.sub_pages))
    return snap


@router.get("/boards/{board_id}", response_model=BoardReopenResponse)
async def reopen_board(
    board_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> BoardReopenResponse:
    board = await get_board(session, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Board not found")

    pages_map: dict[str, dict[str, Any]] = {}
    can_continue = False
    last_lesson_question: str | None = None

    # Sort turns by order_index
    sorted_turns = sorted(board.turns, key=lambda t: t.order_index)

    for turn in sorted_turns:
        # Validate artifacts on read
        artifacts = turn.get_validated_artifacts()
        if artifacts is None:
            continue

        page_id = artifacts.page_id
        if page_id not in pages_map:
            pages_map[page_id] = {
                "page_id": page_id,
                "diagram": artifacts.verified_diagram,
                "ops": [],
            }

        # Collect acked ops from segments
        sorted_segs = sorted(turn.segments, key=lambda s: s.order_index)
        for seg in sorted_segs:
            if seg.command and isinstance(seg.command, list):
                pages_map[page_id]["ops"].extend(seg.command)

        if artifacts.kind == "lesson":
            last_lesson_question = turn.question
            can_continue = (artifacts.status == "partial")
        elif artifacts.kind == "resume":
            if artifacts.status == "complete":
                can_continue = False

    pages_list = [
        BoardPageReopen(
            page_id=p["page_id"],
            diagram=p["diagram"],
            ops=p["ops"],
        )
        for p in pages_map.values()
    ]

    log_event("board_reopened", board_id=board_id, page_count=len(pages_list))
    return BoardReopenResponse(
        pages=pages_list,
        can_continue_lesson=can_continue,
        last_lesson_question=last_lesson_question,
    )
