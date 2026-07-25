"""Async SQLite engine for the AstrBot WebUI module's independent database.

Follows the same recipe as ``astrbot/core/knowledge_base/kb_db_sqlite.py``
(own ``create_async_engine`` + ``async_sessionmaker`` against its own file,
``metadata.create_all`` for schema creation) plus the WAL/busy-timeout
pragmas ``astrbot/core/db/sqlite.py`` applies to the core database, since
this module is just as exposed to SQLite's single-writer limitation.

The database file lives under the AstrBot data directory (resolved via
``astrbot.core.utils.astrbot_path.get_astrbot_data_path``, the same helper
every other AstrBot data path -- including the knowledge base's -- is
derived from) at ``<data_dir>/webui/webui.db``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.webui.db.models import WebUiBaseModel

WEBUI_DB_DIRNAME = "webui"
WEBUI_DB_FILENAME = "webui.db"


def get_webui_db_path() -> str:
    """Return the absolute path to the webui module's own SQLite database file."""
    return str(Path(get_astrbot_data_path()) / WEBUI_DB_DIRNAME / WEBUI_DB_FILENAME)


_db_path = get_webui_db_path()
Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

WEBUI_DATABASE_URL = f"sqlite+aiosqlite:///{_db_path}"

# SQLite only supports a single writer at a time; without a busy timeout the
# driver raises "database is locked" instantly on write contention. Setting
# connect-level timeout=30 (seconds) mirrors astrbot.core.db.BaseDatabase's
# handling of the same issue for the core database.
webui_engine = create_async_engine(
    WEBUI_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={"timeout": 30},
)

webui_async_sessionmaker = async_sessionmaker(
    webui_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
"""``async_sessionmaker`` bound to the webui module's own engine/database.

Prefer the ``get_webui_db_session`` FastAPI dependency in route handlers;
use this directly only for one-off scripts/background jobs.
"""

_initialized = False


async def init_webui_db() -> None:
    """Create all webui tables and apply SQLite pragmas.

    Idempotent: safe to call on every startup. ``CREATE TABLE IF NOT
    EXISTS``-equivalent semantics come from SQLAlchemy's
    ``metadata.create_all``; the in-process ``_initialized`` guard avoids
    re-running the PRAGMA statements needlessly on repeated calls within
    the same process.
    """
    global _initialized
    if _initialized:
        return

    async with webui_engine.begin() as conn:
        await conn.run_sync(WebUiBaseModel.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA cache_size=20000"))
        await conn.execute(text("PRAGMA temp_store=MEMORY"))
        await conn.execute(text("PRAGMA mmap_size=134217728"))
        await conn.execute(text("PRAGMA optimize"))
        await conn.commit()

    _initialized = True
    logger.info(f"[webui] database initialized at {_db_path}")


async def get_webui_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an ``AsyncSession`` bound to the webui database.

    Usage::

        @router.get("/whatever")
        async def handler(session: AsyncSession = Depends(get_webui_db_session)):
            ...
    """
    async with webui_async_sessionmaker() as session:
        yield session
