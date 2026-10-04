from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    text_path TEXT,
    astrbot_attachment_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner);
CREATE TABLE IF NOT EXISTS chat_usage (
    owner TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (owner, day)
);
"""

PUBLIC_FILE_FIELDS = ("file_id", "name", "kind", "mime_type", "size", "created_at")


class Store:
    """SQLite store for file ownership and per-user chat usage.

    Every file query is filtered by `owner`, so a caller can never read or
    delete another user's file by guessing its ID.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def add_file(self, record: dict) -> None:
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        await self.db.execute(
            f"INSERT INTO files ({columns}) VALUES ({placeholders})",
            tuple(record.values()),
        )
        await self.db.commit()

    async def get_files(self, owner: str, file_ids: list[str]) -> list[dict]:
        """Fetch files owned by `owner`, keeping the order of `file_ids`.

        Args:
            owner: Gateway username of the caller.
            file_ids: Requested file IDs.

        Returns:
            The owned files that exist. IDs owned by someone else are omitted.
        """
        if not file_ids:
            return []
        placeholders = ", ".join("?" for _ in file_ids)
        cursor = await self.db.execute(
            f"SELECT * FROM files WHERE owner = ? AND file_id IN ({placeholders})",
            (owner, *file_ids),
        )
        rows = {row["file_id"]: dict(row) for row in await cursor.fetchall()}
        return [rows[file_id] for file_id in file_ids if file_id in rows]

    async def list_files(self, owner: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM files WHERE owner = ? ORDER BY created_at DESC", (owner,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def delete_file(self, owner: str, file_id: str) -> None:
        await self.db.execute(
            "DELETE FROM files WHERE owner = ? AND file_id = ?", (owner, file_id)
        )
        await self.db.commit()

    async def storage_used(self, owner: str) -> int:
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM files WHERE owner = ?", (owner,)
        )
        return (await cursor.fetchone())[0]

    async def expired_files(self, ttl_seconds: float) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM files WHERE created_at < ?", (time.time() - ttl_seconds,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def consume_chat(self, owner: str, limit: int | None) -> bool:
        """Count one chat request for today, unless the daily limit is reached.

        The check and increment are a single UPSERT, so concurrent requests
        cannot exceed the limit.

        Args:
            owner: Gateway username of the caller.
            limit: Daily limit, or None for unlimited.

        Returns:
            True if the request is allowed, False if the limit is reached.
        """
        day = datetime.now(timezone.utc).date().isoformat()
        cursor = await self.db.execute(
            "INSERT INTO chat_usage (owner, day, count) VALUES (?, ?, 1) "
            "ON CONFLICT (owner, day) DO UPDATE SET count = count + 1 "
            "WHERE count < ?",
            (owner, day, limit if limit is not None else 2**62),
        )
        await self.db.commit()
        return cursor.rowcount == 1
