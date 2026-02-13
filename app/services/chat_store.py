"""Almacenamiento de historial de chat en SQLite."""

import aiosqlite
import json
from datetime import datetime
from typing import Optional
from app.config import DATABASE_PATH


async def init_chat_db():
    """Crea las tablas de chat si no existen."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
            )
        """)
        await db.commit()


async def get_or_create_session(user_id: str) -> int:
    """Obtiene la sesion activa o crea una nueva."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM chat_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return row["id"]

        cursor = await db.execute(
            "INSERT INTO chat_sessions (user_id) VALUES (?)",
            (user_id,)
        )
        await db.commit()
        return cursor.lastrowid


async def save_message(session_id: int, role: str, content: str):
    """Guarda un mensaje en el historial."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content)
        )
        await db.commit()


async def get_history(session_id: int, limit: int = 10) -> list:
    """Obtiene los ultimos mensajes de una sesion."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit)
        )
        rows = await cursor.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def clear_session(user_id: str):
    """Elimina la sesion y mensajes del usuario."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM chat_sessions WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        for row in rows:
            await db.execute("DELETE FROM chat_messages WHERE session_id = ?", (row[0],))
        await db.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
        await db.commit()
