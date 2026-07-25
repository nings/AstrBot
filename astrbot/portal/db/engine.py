"""Async SQLite engine for the AstrBot Portal module's independent database.

Follows the same recipe as ``astrbot/core/knowledge_base/kb_db_sqlite.py``
(own ``create_async_engine`` + ``async_sessionmaker`` against its own file,
``metadata.create_all`` for schema creation) plus the WAL/busy-timeout
pragmas ``astrbot/core/db/sqlite.py`` applies to the core database, since
this module is just as exposed to SQLite's single-writer limitation.

The database file lives under the AstrBot data directory (resolved via
``astrbot.core.utils.astrbot_path.get_astrbot_data_path``, the same helper
every other AstrBot data path -- including the knowledge base's -- is
derived from) at ``<data_dir>/portal/portal.db``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.portal.db.models import PortalBaseModel

PORTAL_DB_DIRNAME = "portal"
PORTAL_DB_FILENAME = "portal.db"


def get_portal_db_path() -> str:
    """Return the absolute path to the portal module's own SQLite database file."""
    return str(Path(get_astrbot_data_path()) / PORTAL_DB_DIRNAME / PORTAL_DB_FILENAME)


_db_path = get_portal_db_path()
Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

PORTAL_DATABASE_URL = f"sqlite+aiosqlite:///{_db_path}"

# SQLite only supports a single writer at a time; without a busy timeout the
# driver raises "database is locked" instantly on write contention. Setting
# connect-level timeout=30 (seconds) mirrors astrbot.core.db.BaseDatabase's
# handling of the same issue for the core database.
portal_engine = create_async_engine(
    PORTAL_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={"timeout": 30},
)

portal_async_sessionmaker = async_sessionmaker(
    portal_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
"""``async_sessionmaker`` bound to the portal module's own engine/database.

Prefer the ``get_portal_db_session`` FastAPI dependency in route handlers;
use this directly only for one-off scripts/background jobs.
"""

_initialized = False


def _add_missing_columns(connection) -> None:
    """Add columns that exist in the models but not yet in an existing table.

    ``metadata.create_all`` creates missing *tables* but never alters existing
    ones, so a database file created by an earlier phase of this module keeps
    its old shape and any column added since then is simply absent -- the next
    query referencing it fails with "no such column". This module has no
    migration framework (the knowledge-base module it is modelled on has none
    either), so schema growth is reconciled here.

    Deliberately limited to the one operation SQLite can do safely and
    idempotently on a populated table: ``ALTER TABLE ... ADD COLUMN`` for a
    column that is nullable or carries a default. Anything else -- a dropped
    or renamed column, a type change, a new NOT NULL column with no default --
    is logged as a warning and left alone rather than silently attempted,
    because getting it wrong means data loss. Those need a real migration.
    """
    inspector = sa_inspect(connection)
    existing_tables = set(inspector.get_table_names())

    for table in PortalBaseModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # just created by create_all, already current

        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            nullable = column.nullable
            has_default = column.default is not None or column.server_default is not None
            if not nullable and not has_default:
                logger.warning(
                    f"[portal] column {table.name}.{column.name} is missing from the "
                    "existing database and is NOT NULL without a default, so it "
                    "cannot be added automatically. A manual migration is required."
                )
                continue

            ddl_type = column.type.compile(dialect=connection.dialect)
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
            if not nullable:
                default = column.default.arg if column.default is not None else None
                literal = (
                    column.server_default.arg
                    if column.server_default is not None
                    else repr(default) if isinstance(default, str) else default
                )
                clause += f" NOT NULL DEFAULT {literal}"
            elif has_default and column.server_default is not None:
                clause += f" DEFAULT {column.server_default.arg}"

            connection.execute(text(clause))
            logger.info(f"[portal] schema reconciled: added {table.name}.{column.name}")


async def init_portal_db() -> None:
    """Create all portal tables and apply SQLite pragmas.

    Idempotent: safe to call on every startup. ``CREATE TABLE IF NOT
    EXISTS``-equivalent semantics come from SQLAlchemy's
    ``metadata.create_all``; the in-process ``_initialized`` guard avoids
    re-running the PRAGMA statements needlessly on repeated calls within
    the same process.
    """
    global _initialized
    if _initialized:
        return

    async with portal_engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA cache_size=20000"))
        await conn.execute(text("PRAGMA temp_store=MEMORY"))
        await conn.execute(text("PRAGMA mmap_size=134217728"))
        await conn.execute(text("PRAGMA optimize"))
        await conn.commit()

    _initialized = True
    logger.info(f"[portal] database initialized at {_db_path}")


async def get_portal_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an ``AsyncSession`` bound to the portal database.

    Usage::

        @router.get("/whatever")
        async def handler(session: AsyncSession = Depends(get_portal_db_session)):
            ...
    """
    async with portal_async_sessionmaker() as session:
        yield session
