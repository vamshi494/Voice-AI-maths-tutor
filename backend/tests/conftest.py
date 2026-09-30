# backend/tests/conftest.py
"""Offline test harness.

1. NETWORK IS DISABLED. Any TCP connect raises, so no test can make real HTTP calls to
   api.groq.com or open real Postgres connections on every persist.
2. PERSISTENCE uses a lazily created in-memory SQLite database (one per test, bound to the
   running event loop), wired into app.persistence.repo, so persist paths execute for real
   instead of failing on a swallowed connection error.
3. PROVIDER KEYS ARE BLANKED so no code path can authenticate against a real provider.
"""
import asyncio
import socket

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    if isinstance(address, (str, bytes)):          # AF_UNIX sockets used by asyncio internals
        return _real_connect(self, address)
    raise RuntimeError(f"Network access is disabled in tests (attempted connect to {address!r})")


def _guarded_create_connection(address, *args, **kwargs):
    raise RuntimeError(f"Network access is disabled in tests (attempted connect to {address!r})")


class MemoryDB:
    """async_session() replacement: in-memory SQLite created on first use in the current loop."""

    def __init__(self) -> None:
        self.engine = None
        self.factory = None
        self.loop = None

    async def _ensure(self) -> None:
        loop = asyncio.get_running_loop()
        if self.engine is None or self.loop is not loop:
            from app.persistence.models import Base
            self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
            self.loop = loop

    def __call__(self) -> "_SessionCtx":
        return _SessionCtx(self)


class _SessionCtx:
    def __init__(self, db: MemoryDB) -> None:
        self.db = db
        self.session = None

    async def __aenter__(self):
        await self.db._ensure()
        self.session = self.db.factory()
        return await self.session.__aenter__()

    async def __aexit__(self, *exc):
        return await self.session.__aexit__(*exc)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    from app.config import settings
    for k in ("GROQ_API_KEY", "GROQ_API_KEYS", "OPENROUTER_API_KEY", "OPENCODE_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY"):
        monkeypatch.setattr(settings, k, "", raising=False)
    monkeypatch.setattr(settings, "IDLE_QUESTION_SETTLE_MS", 0, raising=False)
    # 4. FEATURE FLAGS START AT THEIR CODE DEFAULTS. config.py loads the developer's .env, which
    #    may enable any flag, so a test that needs a flag sets it: background memory summaries,
    #    board flushes and chapter outlines would otherwise run inside settle()-timed tests
    #    (order-dependent flakes).
    for name, field in type(settings).model_fields.items():
        if name.startswith("FEATURE_"):
            monkeypatch.setattr(settings, name, field.default, raising=False)
    db = MemoryDB()
    import app.persistence.repo as repo
    import app.persistence.db as dbmod
    monkeypatch.setattr(repo, "async_session", db, raising=False)
    monkeypatch.setattr(dbmod, "async_session", db, raising=False)
    import app.transport as transport
    monkeypatch.setattr(transport, "_active_room", None, raising=False)
    yield db
