# app/persistence/board_state.py
"""Board-state documents: load/save, page upserts and the legacy-document builder.

Every write carries the writer's lease owner: when another owner holds a live lease
the write raises `LeaseLost` instead of touching the board.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.agent_state import PausedLesson
from app.contracts.board_ops import BoardOp
from app.contracts.diagram import VerifiedDiagram
from app.contracts.memory import BoardStateDoc
from app.contracts.messages import PageCommit, SnapshotPage, SnapshotSubPage
from app.observability import log_event, logger
from app.persistence.models import BoardState, PageRow, Turn


class LeaseLost(Exception):
    """The writer does not own the board lease."""


def _lease_blocks(row: BoardState | None, owner: str) -> bool:
    """True when a live lease held by a different owner guards the row."""
    if row is None or row.lease_owner is None or row.lease_owner == owner:
        return False
    return _lease_live(row)


def _lease_live(row: BoardState) -> bool:
    until = row.lease_until
    if until is None:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def _lease_until() -> datetime:
    from app.config import settings

    return datetime.now(timezone.utc) + timedelta(seconds=settings.LEASE_TTL_S)


async def _get_row(session: AsyncSession, board_id: str) -> BoardState | None:
    res = await session.execute(select(BoardState).where(BoardState.board_id == board_id))
    return res.scalar_one_or_none()


def _check_lease(row: BoardState | None, board_id: str, owner: str) -> None:
    if _lease_blocks(row, owner):
        log_event("board_write_refused_lease", board_id=board_id, owner=owner,
                  lease_owner=row.lease_owner if row is not None else None)
        raise LeaseLost(f"board {board_id} is leased to another writer")


async def load_board_state(session: AsyncSession, board_id: str) -> BoardStateDoc | None:
    row = await _get_row(session, board_id)
    if row is None or row.state is None:
        return None
    try:
        return BoardStateDoc.model_validate(row.state)
    except Exception as e:
        logger.warning(f"board_state doc invalid for {board_id}: {e}")
        log_event("board_state_load_failed", board_id=board_id, error=str(e))
        return None


async def save_board_state(session: AsyncSession, doc: BoardStateDoc, owner: str) -> None:
    """Upsert the document. Raises `LeaseLost` when another owner holds the lease."""
    row = await _get_row(session, doc.board_id)
    if row is None:
        row = BoardState(board_id=doc.board_id, version=doc.version)
        session.add(row)
        await session.flush()
    _check_lease(row, doc.board_id, owner)
    row.version = doc.version
    row.state = doc.model_dump(by_alias=True, exclude_none=True, mode="json")
    await session.commit()


async def save_pages(session: AsyncSession, board_id: str, lesson_id: str,
                     pages: list[dict[str, Any]], owner: str) -> None:
    """Upsert sub-page rows by `page_id`; `ops` keeps its acked flags."""
    row = await _get_row(session, board_id)
    _check_lease(row, board_id, owner)
    for page in pages:
        page_id = page["page_id"]
        existing = await session.get(PageRow, page_id)
        if existing is None:
            existing = PageRow(page_id=page_id, board_id=board_id, ops=[])
            session.add(existing)
        existing.lesson_id = page.get("lesson_id", lesson_id)
        existing.root_page_id = page.get("root_page_id", page_id)
        existing.page_index = int(page.get("page_index", 0) or 0)
        existing.sub_index = int(page.get("sub_index", 1) or 1)
        existing.title = page.get("title", "") or ""
        existing.layout = _jsonable(page.get("layout"))
        existing.diagram = _jsonable(page.get("diagram"))
        existing.ops = page.get("ops", [])
        if "summary" in page:
            existing.summary = page.get("summary", "") or ""
    await session.commit()


def _jsonable(value: Any) -> Any:
    """JSON columns must hold plain data: dump Pydantic models before assigning."""
    if value is not None and hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    return value


async def load_pages(session: AsyncSession, board_id: str) -> list[dict[str, Any]]:
    """All persisted sub-page rows of a board, root/page/sub order."""
    res = await session.execute(
        select(PageRow).where(PageRow.board_id == board_id)
        .order_by(PageRow.root_page_id, PageRow.page_index, PageRow.sub_index))
    return [
        {
            "page_id": r.page_id,
            "root_page_id": r.root_page_id,
            "lesson_id": r.lesson_id,
            "page_index": r.page_index,
            "sub_index": r.sub_index,
            "title": r.title,
            "diagram": r.diagram,
            "layout": r.layout,
            "ops": r.ops or [],
            "summary": r.summary,
        }
        for r in res.scalars().all()
    ]


async def load_snapshot(session: AsyncSession, board_id: str, root_page_id: str) -> SnapshotPage | None:
    """A page's acked ops as a `BoardSnapshot` body; unacked ops are never restored."""
    res = await session.execute(
        select(PageRow).where(PageRow.board_id == board_id, PageRow.root_page_id == root_page_id)
        .order_by(PageRow.sub_index))
    rows = list(res.scalars().all())
    if not rows:
        return None
    sub_pages = [
        SnapshotSubPage(
            sub_id=r.page_id,
            ops=[BoardOp.model_validate(o["op"]) for o in (r.ops or []) if o.get("acked")],
        )
        for r in rows
    ]
    snap = SnapshotPage(page_id=root_page_id, title=rows[0].title or None, sub_pages=sub_pages)
    if rows[0].diagram:
        try:
            snap.diagram = VerifiedDiagram.model_validate(rows[0].diagram)
        except Exception as e:
            log_event("board_state_diagram_invalid", page_id=root_page_id, error=str(e))
    if rows[0].layout:
        # The persisted page commit (figures/tables/text blocks) for the notes drawer.
        try:
            data = dict(rows[0].layout)
            # workRect is required on the wire and dropped by exclude_none when null.
            if "workRect" not in data and "work_rect" not in data:
                data["workRect"] = None
            snap.commit = PageCommit.model_validate(data)
        except Exception as e:
            log_event("board_state_commit_invalid", page_id=root_page_id, error=str(e))
    return snap


async def build_legacy_doc(session: AsyncSession, board_id: str) -> BoardStateDoc | None:
    """A document for a board with no stored board state: the latest lesson Turn's artifacts."""
    res = await session.execute(
        select(Turn).where(Turn.board_id == board_id).order_by(Turn.order_index))
    for turn in reversed(list(res.scalars().all())):
        try:
            artifacts = turn.get_validated_artifacts()
        except Exception:
            continue
        if artifacts is None or not artifacts.page_id or artifacts.kind != "lesson":
            continue
        paused = PausedLesson(
            board_id=board_id,
            page_id=artifacts.page_id,
            lesson_question=turn.question or "",
            turn_plan=artifacts.turn_plan,
            solver_projection=artifacts.solver_projection,
            diagram=artifacts.verified_diagram,
            lesson_turn_id=str(turn.id),
            lesson_completed=artifacts.status == "complete",
        )
        return BoardStateDoc(
            board_id=board_id,
            paused_lesson=paused.model_dump(mode="json"),
            current_page_id=artifacts.page_id,
        )
    return None


# ---------------------------------------------------------------------------
# Lease: acquire/renew/release. The caller's owner id is the job id.
# ---------------------------------------------------------------------------

async def acquire_lease(session: AsyncSession, board_id: str, owner: str, *,
                        wait_s: float | None = None, steal: bool = True) -> bool:
    """Conditional acquire; while blocked, poll every 250 ms up to `wait_s`
    (default `LEASE_WAIT_S`), then steal unconditionally (logged `lease_stolen`).

    `steal=False` only ever returns False when another owner's lease is live; the
    tests use it to pin the blocking case.
    """
    from app.config import settings

    deadline = asyncio.get_running_loop().time() + (settings.LEASE_WAIT_S if wait_s is None else wait_s)
    row = await _get_row(session, board_id)
    if row is None:
        try:
            async with session.begin_nested():
                row = BoardState(board_id=board_id, version=2)
                session.add(row)
                await session.flush()
        except IntegrityError:
            # Another tab inserted the first row between our read and the flush: use it.
            row = await _get_row(session, board_id)
    while True:
        # Drop the identity map so each poll re-reads the row (a clean release by the
        # other tab must be seen instead of waiting out the deadline and stealing).
        session.expire_all()
        if await renew_lease(session, board_id, owner):
            log_event("lease_acquired", board_id=board_id, owner=owner)
            return True
        now = asyncio.get_running_loop().time()
        if now >= deadline:
            break
        await asyncio.sleep(min(0.25, max(0.0, deadline - now)))
    if not steal:
        return False
    row = await _get_row(session, board_id)
    if row is None:
        return False
    row.lease_owner = owner
    row.lease_until = _lease_until()
    await session.commit()
    log_event("lease_stolen", board_id=board_id, owner=owner)
    return True


async def renew_lease(session: AsyncSession, board_id: str, owner: str) -> bool:
    """Take or extend the lease when it is free or ours; False when another owner's is live."""
    row = await _get_row(session, board_id)
    if row is None or _lease_blocks(row, owner):
        return False
    row.lease_owner = owner
    row.lease_until = _lease_until()
    await session.commit()
    return True


async def release_lease(session: AsyncSession, board_id: str, owner: str) -> None:
    """Drop the lease after the final flush (manager.shutdown)."""
    row = await _get_row(session, board_id)
    if row is None or row.lease_owner not in (None, owner):
        return
    row.lease_owner = None
    row.lease_until = None
    await session.commit()
