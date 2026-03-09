"""
app/services/memory_manager.py

Persistent memory layer for AI agents backed by SQLite (via aiosqlite).

Tables created:
  - agent_memory        : key-value store per agent (upsert semantics)
  - agent_conversations : timestamped chat history per session
  - agent_alerts        : actionable alerts with read/unread state

Usage:
    from app.services.memory_manager import memory_manager

    await memory_manager.init_db()
    await memory_manager.remember("stock_agent", "last_check", {"ts": "2026-03-09"})
    value = await memory_manager.recall("stock_agent", "last_check")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import DATABASE_PATH

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    Async manager for agent memory, conversation history, and alerts.

    All methods open a fresh aiosqlite connection per call to stay
    compatible with FastAPI's async request lifecycle.
    """

    def __init__(self, db_path: str = DATABASE_PATH):
        self._db_path = db_path

    # ── Schema init ─────────────────────────────────────────────────────────

    async def init_db(self) -> None:
        """Create all agent tables if they do not exist."""
        db_path = Path(self._db_path)
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self._db_path) as db:
            # ── agent_memory ──────────────────────────────────────────────
            await db.execute("""
                CREATE TABLE IF NOT EXISTS agent_memory (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name  TEXT    NOT NULL,
                    key         TEXT    NOT NULL,
                    value       TEXT    NOT NULL DEFAULT '{}',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (agent_name, key)
                )
            """)

            # ── agent_conversations ───────────────────────────────────────
            await db.execute("""
                CREATE TABLE IF NOT EXISTS agent_conversations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT    NOT NULL,
                    agent_name  TEXT    NOT NULL,
                    role        TEXT    NOT NULL,
                    content     TEXT    NOT NULL,
                    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_session "
                "ON agent_conversations (session_id, ts)"
            )

            # ── agent_alerts ──────────────────────────────────────────────
            await db.execute("""
                CREATE TABLE IF NOT EXISTS agent_alerts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name  TEXT    NOT NULL,
                    level       TEXT    NOT NULL DEFAULT 'info',
                    title       TEXT    NOT NULL DEFAULT '',
                    message     TEXT    NOT NULL DEFAULT '',
                    data        TEXT    NOT NULL DEFAULT '{}',
                    read        INTEGER NOT NULL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_read "
                "ON agent_alerts (read, created_at)"
            )

            await db.commit()
        logger.debug("MemoryManager: tables verified/created")

    # ── Key-value memory ────────────────────────────────────────────────────

    async def remember(self, agent_name: str, key: str, value: Any) -> None:
        """
        Persist a value for an agent key (upsert).

        Value is JSON-serialized, so any JSON-compatible Python object works.
        """
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                INSERT INTO agent_memory (agent_name, key, value, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (agent_name, key) DO UPDATE SET
                    value      = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (agent_name, key, serialized))
            await db.commit()

    async def recall(self, agent_name: str, key: str, default: Any = None) -> Any:
        """
        Retrieve a stored value.  Returns *default* if the key does not exist.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT value FROM agent_memory WHERE agent_name = ? AND key = ?",
                (agent_name, key),
            )
            row = await cursor.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            return default

    async def recall_all(self, agent_name: str) -> dict[str, Any]:
        """Return a dict of all key → value pairs stored for *agent_name*."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT key, value FROM agent_memory WHERE agent_name = ?",
                (agent_name,),
            )
            rows = await cursor.fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, KeyError):
                result[row["key"]] = row["value"]
        return result

    async def forget(self, agent_name: str, key: str) -> None:
        """Delete a specific key for an agent."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM agent_memory WHERE agent_name = ? AND key = ?",
                (agent_name, key),
            )
            await db.commit()

    # ── Conversation history ─────────────────────────────────────────────────

    async def save_conversation(
        self, session_id: str, agent_name: str, role: str, content: str
    ) -> None:
        """Append a message to a session's conversation history."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO agent_conversations (session_id, agent_name, role, content) "
                "VALUES (?, ?, ?, ?)",
                (session_id, agent_name, role, content),
            )
            await db.commit()

    async def get_conversation(self, session_id: str, limit: int = 20) -> list[dict]:
        """
        Return the last *limit* messages for a session, ordered chronologically
        (oldest first — ready to pass directly to the Claude messages list).
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT role, content, agent_name, ts
                FROM agent_conversations
                WHERE session_id = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
        # Reverse so oldest message is first
        return [dict(r) for r in reversed(rows)]

    async def clear_conversation(self, session_id: str) -> None:
        """Delete all messages for a session."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM agent_conversations WHERE session_id = ?",
                (session_id,),
            )
            await db.commit()

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def create_alert(
        self,
        agent_name: str,
        level: str,
        title: str,
        message: str,
        data: Any = None,
    ) -> int:
        """
        Create an alert record.

        Args:
            agent_name: Agent that generated the alert.
            level:      Severity — one of: 'info', 'warning', 'critical'.
            title:      Short headline.
            message:    Detailed description.
            data:       Optional JSON-serializable payload for extra context.

        Returns:
            The new alert's row id.
        """
        if level not in ("info", "warning", "critical"):
            level = "info"
        serialized_data = json.dumps(data or {}, ensure_ascii=False, default=str)

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "INSERT INTO agent_alerts (agent_name, level, title, message, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_name, level, title, message, serialized_data),
            )
            await db.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def get_alerts(
        self, unread_only: bool = False, limit: int = 50
    ) -> list[dict]:
        """
        Return alerts, newest first.

        Args:
            unread_only: If True, only return alerts where read == 0.
            limit:       Maximum number of records to return.
        """
        where_clause = "WHERE read = 0" if unread_only else ""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT id, agent_name, level, title, message, data, read, created_at
                FROM agent_alerts
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()

        result: list[dict] = []
        for row in rows:
            r = dict(row)
            try:
                r["data"] = json.loads(r.get("data") or "{}")
            except (json.JSONDecodeError, TypeError):
                r["data"] = {}
            r["read"] = bool(r["read"])
            result.append(r)
        return result

    async def mark_alert_read(self, alert_id: int) -> None:
        """Mark a single alert as read."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE agent_alerts SET read = 1 WHERE id = ?", (alert_id,)
            )
            await db.commit()

    async def mark_all_alerts_read(self) -> None:
        """Mark every unread alert as read."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE agent_alerts SET read = 1 WHERE read = 0")
            await db.commit()


# ── Module-level singleton ───────────────────────────────────────────────────
memory_manager = MemoryManager()
