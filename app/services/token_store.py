import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
from app.config import DATABASE_PATH


async def init_db():
    """Inicializa la base de datos SQLite."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY,
                user_id TEXT UNIQUE,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def save_tokens(user_id: str, access_token: str, refresh_token: str, expires_in: int):
    """Guarda o actualiza los tokens de un usuario."""
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO tokens (user_id, access_token, refresh_token, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at
        """, (user_id, access_token, refresh_token, expires_at))
        await db.commit()


async def get_tokens(user_id: str) -> Optional[dict]:
    """Obtiene los tokens de un usuario."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tokens WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def get_any_tokens() -> Optional[dict]:
    """Obtiene cualquier token almacenado (para app single-user)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tokens LIMIT 1")
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def delete_tokens(user_id: str):
    """Elimina los tokens de un usuario."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
        await db.commit()


async def is_token_expired(user_id: str) -> bool:
    """Verifica si el token ha expirado."""
    tokens = await get_tokens(user_id)
    if not tokens:
        return True
    expires_at = datetime.fromisoformat(tokens["expires_at"])
    return datetime.utcnow() >= expires_at
