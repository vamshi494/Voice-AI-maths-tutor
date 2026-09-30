# app/persistence/db.py
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

logger = logging.getLogger("ai_math_tutor.db")


def sqlite_fallback_url() -> str:
    """Absolute SQLite URL so worker and API share one file."""
    p = settings.SQLITE_FALLBACK_PATH or str(Path(__file__).resolve().parents[3] / "math_tutor.db")
    return f"sqlite+aiosqlite:///{p}"

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Initialize database tables. Falls back to SQLite if PostgreSQL is unavailable or read-only."""
    global engine, async_session
    from app.persistence.models import Base

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized successfully with primary engine")
    except Exception as e:
        fallback_url = sqlite_fallback_url()
        logger.warning(
            f"Primary database ({settings.DATABASE_URL}) failed: {e}. "
            f"Falling back to local SQLite ({fallback_url})."
        )
        engine = create_async_engine(
            fallback_url,
            echo=False,
            future=True,
            connect_args={"timeout": 15},
        )
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, record):
            try:
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()
            except Exception:
                pass

        async_session.configure(bind=engine)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Local SQLite database initialized successfully in WAL mode.")


async def get_db_session() -> AsyncIterator[AsyncSession]:
    global engine, async_session
    try:
        async with async_session() as session:
            yield session
    except Exception as e:
        # If the primary database failed during session creation/execution
        if "sqlite" not in str(engine.url):
            logger.warning(f"Database error ({e}), switching to SQLite fallback...")
            await init_db()
            async with async_session() as session:
                yield session
        else:
            raise
