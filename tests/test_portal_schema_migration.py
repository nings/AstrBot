"""Guards for additive schema growth in the portal module's own database.

``init_portal_db`` only called ``metadata.create_all``, which creates missing
tables but never alters existing ones. A database file created before a column
was added keeps its old shape, and the next query referencing that column fails
with "no such column" -- which is exactly what happens to any portal.db created
by an earlier phase once a later phase grows a model. ``_add_missing_columns``
reconciles the additive case.

The related quota-atomicity guard lives in tests/test_portal_upload_policy.py
(``test_concurrent_uploads_cannot_exceed_the_daily_quota``), where it can
exercise the real service and route rather than just the SQL shape.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from astrbot.portal.db import engine as portal_engine_mod
from astrbot.portal.db.models import PortalBaseModel


# --------------------------------------------------------------------------
# 2. Additive schema reconciliation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_column_is_added_to_an_existing_table(tmp_path):
    """A database created without uploaded_bytes gains it instead of erroring.

    Simulates the real scenario: a portal.db created by an earlier phase, then
    opened by code whose models have grown a column.
    """
    db_path = tmp_path / "legacy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)

    # Build the table WITHOUT uploaded_bytes, as an earlier phase would have.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                'CREATE TABLE portal_usage_counters ('
                "  id INTEGER PRIMARY KEY,"
                "  user_id VARCHAR NOT NULL,"
                "  window_start DATETIME NOT NULL,"
                "  request_count INTEGER NOT NULL DEFAULT 0,"
                "  concurrent_runs INTEGER NOT NULL DEFAULT 0"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO portal_usage_counters "
                "(user_id, window_start, request_count, concurrent_runs) "
                "VALUES ('u-legacy', '2026-07-25', 7, 0)"
            )
        )

    async with engine.begin() as conn:
        cols_before = {
            r[1] for r in (await conn.execute(text("PRAGMA table_info(portal_usage_counters)")))
        }
        assert "uploaded_bytes" not in cols_before, "fixture must start without the column"

        await conn.run_sync(PortalBaseModel.metadata.create_all)
        await conn.run_sync(portal_engine_mod._add_missing_columns)

        cols_after = {
            r[1] for r in (await conn.execute(text("PRAGMA table_info(portal_usage_counters)")))
        }
        assert "uploaded_bytes" in cols_after, (
            "create_all does not ALTER existing tables; _add_missing_columns must"
        )

        # The pre-existing row survived and the new column is readable.
        row = (
            await conn.execute(
                text(
                    "SELECT request_count, uploaded_bytes FROM portal_usage_counters "
                    "WHERE user_id = 'u-legacy'"
                )
            )
        ).one()
        assert row[0] == 7, "existing data must not be lost"
        assert row[1] == 0, "the added column must be readable with its default"

    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent(tmp_path):
    """Running the reconciler twice must not fail with 'duplicate column name'."""
    db_path = tmp_path / "idempotent.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
        await conn.run_sync(portal_engine_mod._add_missing_columns)
        await conn.run_sync(portal_engine_mod._add_missing_columns)
    await engine.dispose()
