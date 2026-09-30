# app/api/routes_token.py
"""Token generation route for LiveKit room access.

- POST /token
- Body {board_id?: str}
- Authenticated user_id
- Creates board if absent
- Returns {token, room, board_id, user_id, user_name}
- Name login: {name} -> a stable user id; the user's latest board is reused (so the worker
  rehydrates its memory, checkpoint and pages), {new_board: true} starts a fresh one
- room = session_id (uuid)
- Participant metadata contains board_id
"""
import json
import re
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import livekit.api as lk_api
from app.config import settings
from app.observability import log_event
from app.persistence.db import get_db_session
from app.persistence.repo import get_latest_board_for_user, get_or_create_board

router = APIRouter()


class TokenRequest(BaseModel):
    board_id: str | None = None
    name: str | None = None          # name login (no password: a demo identity, not security)
    new_board: bool = False


class TokenResponse(BaseModel):
    token: str
    room: str
    board_id: str
    user_id: str = ""
    user_name: str = ""


NAME_MAX_LEN = 40


def clean_display_name(name: str | None) -> str:
    """Letters, digits, spaces and . ' - only; collapsed; at most NAME_MAX_LEN characters."""
    text = re.sub(r"[^\w .'-]", " ", name or "", flags=re.UNICODE)
    return " ".join(text.split())[:NAME_MAX_LEN].strip()


def user_id_for_name(name: str) -> str:
    """Stable id for a display name: case/spacing-insensitive, so "Vamshi L" == "vamshi  l"."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"u_{slug or 'guest'}"


@router.post("/token", response_model=TokenResponse)
async def generate_token(
    req: TokenRequest,
    x_user_id: str | None = Header(default="demo_student", alias="X-User-Id"),
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    user_name = ""
    if req.name is not None:
        user_name = clean_display_name(req.name)
        if not user_name:
            raise HTTPException(status_code=400, detail="Please enter your name.")
        user_id = user_id_for_name(user_name)
        board_id = None
        if req.board_id and not req.new_board:
            owned = await get_or_create_board(session, board_id=req.board_id, user_id=user_id)
            board_id = owned.id if owned.user_id == user_id else None   # never another user's board
        if board_id is None and not req.new_board:
            latest = await get_latest_board_for_user(session, user_id)
            board_id = latest.id if latest is not None else None
        board_id = board_id or f"b_{uuid4().hex[:8]}"
    else:
        user_id = x_user_id or "demo_student"
        board_id = req.board_id or f"b_{uuid4().hex[:8]}"
    room_id = f"room_{uuid4().hex[:12]}"

    # Ensure board exists in DB
    board = await get_or_create_board(session, board_id=board_id, user_id=user_id)
    if user_name:
        board.updated_at = datetime.now(timezone.utc)   # "last used"
    await session.commit()

    api_key = settings.LIVEKIT_API_KEY or "devkey"
    api_secret = settings.LIVEKIT_API_SECRET or "secret_key_needs_32_characters_minimum_len!"

    token = (
        lk_api.AccessToken(api_key, api_secret)
        .with_identity(user_id)
        .with_name(user_name or f"Student_{user_id[:6]}")
        .with_grants(lk_api.VideoGrants(room_join=True, room=room_id))
        .with_metadata(json.dumps({"board_id": board_id, "user_id": user_id, "user_name": user_name}))
        .to_jwt()
    )

    log_event("token_issued", user_id=user_id, board_id=board_id, room=room_id)
    return TokenResponse(
        token=token,
        room=room_id,
        board_id=board_id,
        user_id=user_id,
        user_name=user_name,
    )
