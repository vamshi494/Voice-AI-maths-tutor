# backend/tests/test_api_routes.py
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.server import app
from app.persistence.db import get_db_session
from app.persistence.models import Base
from app.persistence.repo import save_turn

# Test in-memory SQLite async engine
_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db_session():
    async with _session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
def setup_db():
    async def _init():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _clean():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    asyncio.run(_init())
    app.dependency_overrides[get_db_session] = override_get_db_session
    yield
    app.dependency_overrides.clear()
    asyncio.run(_clean())


@pytest.mark.asyncio
async def test_health_check():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_post_token_creates_board_and_jwt():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/token", json={}, headers={"X-User-Id": "student_alice"})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert "room" in data
        assert "board_id" in data
        assert data["board_id"].startswith("b_")


@pytest.mark.asyncio
async def test_get_boards_and_reopen_board():
    board_id = f"b_test_{uuid4().hex[:6]}"
    user_id = "student_bob"
    turn_id = str(uuid4())

    # Save a turn in DB using the test session
    async with _session_factory() as session:
        await save_turn(
            session,
            turn_id=turn_id,
            board_id=board_id,
            user_id=user_id,
            question="Find x if 2x + 10 = 20",
            kind="lesson",
            page_id="p_test",
            status="partial",
            segments_data=[
                {
                    "narration": "Let's subtract 10",
                    "spoken_text": "Let's subtract 10",
                    "command": [{"kind": "write", "text": "2x = 10", "row_id": "w1"}],
                }
            ],
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. GET /boards
        resp = await ac.get("/boards", headers={"X-User-Id": user_id})
        assert resp.status_code == 200
        boards = resp.json()
        assert any(b["id"] == board_id for b in boards)

        # 2. GET /boards/{id}
        reopen_resp = await ac.get(f"/boards/{board_id}")
        assert reopen_resp.status_code == 200
        reopen_data = reopen_resp.json()
        assert reopen_data["can_continue_lesson"] is True
        assert reopen_data["last_lesson_question"] == "Find x if 2x + 10 = 20"
        assert len(reopen_data["pages"]) == 1
        page = reopen_data["pages"][0]
        assert page["page_id"] == "p_test"
        assert len(page["ops"]) == 1
        assert page["ops"][0]["text"] == "2x = 10"


@pytest.mark.asyncio
async def test_reopen_nonexistent_board_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/boards/non_existent_board_12345")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_page_snapshot():
    """GET /boards/{id}/pages/{page_id} returns the stored read-only SnapshotPage."""
    from app.contracts.messages import Block, PageCommit, Rect
    from app.persistence.models import PageRow

    board_id = f"b_notes_{uuid4().hex[:6]}"
    commit = PageCommit(
        generation=0, turn_id="t", page_id="L_x_p0", commit_id="c1",
        work_rect=Rect(x=40, y=72, width=340, height=608),
        blocks=[Block(id="fig_a", role="figure", rect=Rect(x=444, y=64, width=324, height=552),
                      commands=[], anchors=[])],
    ).model_dump(by_alias=True, exclude_none=True)
    async with _session_factory() as session:
        session.add(PageRow(
            page_id="L_x_p0", board_id=board_id, lesson_id="L_x", root_page_id="L_x_p0",
            page_index=0, sub_index=1, title="What BPT says", layout=commit,
            ops=[{"op": {"opId": "t:0:0", "kind": "WRITE", "atWord": 0, "text": "AD/DB",
                         "rowId": "w1"}, "acked": True},
                 {"op": {"opId": "t:0:1", "kind": "WRITE", "atWord": 0, "text": "unacked",
                         "rowId": "w2"}, "acked": False}],
        ))
        session.add(PageRow(
            page_id="L_x_p0_p2", board_id=board_id, lesson_id="L_x", root_page_id="L_x_p0",
            page_index=0, sub_index=2, title="", layout=None, ops=[],
        ))
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/boards/{board_id}/pages/L_x_p0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pageId"] == "L_x_p0"
        assert data["title"] == "What BPT says"
        assert data["commit"]["commitId"] == "c1"
        assert data["commit"]["blocks"][0]["id"] == "fig_a"
        assert [sp["subId"] for sp in data["subPages"]] == ["L_x_p0", "L_x_p0_p2"]
        assert len(data["subPages"][0]["ops"]) == 1, "only acked ops"

        # A sub-page id resolves to its root.
        sub = await ac.get(f"/boards/{board_id}/pages/L_x_p0_p2")
        assert sub.status_code == 200
        assert sub.json()["pageId"] == "L_x_p0"

        missing = await ac.get(f"/boards/{board_id}/pages/nope")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_ocr_route_hides_internal_errors_and_keeps_400(monkeypatch):
    """The client shows `detail`, so it must not carry str(e) (provider/stack text); an empty
    upload must keep its HTTPException(400) instead of coming back as a 500 from the blanket
    try."""
    import app.api.routes_ocr as ocr_mod

    async def boom(content):
        raise RuntimeError("OpenCode connection error: https://opencode.ai/zen internal detail")

    monkeypatch.setattr(ocr_mod, "process_image_bytes", boom)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/ocr", files={"file": ("q.jpg", b"\xff\xd8data", "image/jpeg")})
        assert res.status_code == 500
        detail = res.json()["detail"]
        assert "OpenCode" not in detail and "http" not in detail

        empty = await client.post("/ocr", files={"file": ("q.jpg", b"", "image/jpeg")})
        assert empty.status_code == 400


@pytest.mark.asyncio
async def test_name_login_reuses_the_users_board():
    """Name login: the same name (any case/spacing) gets back the same board, so the worker
    rehydrates that user's memory, checkpoint and pages; another name gets its own board."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        first = (await ac.post("/token", json={"name": "Vamshi L"})).json()
        again = (await ac.post("/token", json={"name": "  vamshi   l "})).json()
        other = (await ac.post("/token", json={"name": "Interviewer"})).json()
    assert first["user_id"] == again["user_id"] == "u_vamshi_l"
    assert first["user_name"] == "Vamshi L"
    assert again["board_id"] == first["board_id"]
    assert other["board_id"] != first["board_id"] and other["user_id"] == "u_interviewer"


@pytest.mark.asyncio
async def test_name_login_new_board_and_foreign_board():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        a1 = (await ac.post("/token", json={"name": "Asha"})).json()
        a2 = (await ac.post("/token", json={"name": "Asha", "new_board": True})).json()
        a3 = (await ac.post("/token", json={"name": "Asha"})).json()
        b1 = (await ac.post("/token", json={"name": "Bala", "board_id": a1["board_id"]})).json()
        empty = await ac.post("/token", json={"name": "  <>  "})
    assert a2["board_id"] != a1["board_id"]
    assert a3["board_id"] == a2["board_id"], "the latest board is the one resumed"
    assert b1["board_id"] != a1["board_id"], "never another user's board"
    assert empty.status_code == 400
