import os
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from app.config import DATABASE_PATH


async def init_db():
    """Inicializa la base de datos SQLite. Crea el directorio si no existe (Railway Volume)."""
    db_path = Path(DATABASE_PATH)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY,
                user_id TEXT UNIQUE,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                nickname TEXT DEFAULT ''
            )
        """)
        # Migration: add nickname column if table already exists without it
        try:
            await db.execute("ALTER TABLE tokens ADD COLUMN nickname TEXT DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # Column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                code_verifier TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS account_settings (
                user_id TEXT PRIMARY KEY,
                daily_goal REAL NOT NULL DEFAULT 500000,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_concentration_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_sku TEXT NOT NULL,
                trigger TEXT NOT NULL,
                winner_user_id TEXT NOT NULL,
                winner_nickname TEXT NOT NULL DEFAULT '',
                winner_item_id TEXT NOT NULL DEFAULT '',
                winner_units_30d INTEGER NOT NULL DEFAULT 0,
                total_bm_avail INTEGER NOT NULL DEFAULT 0,
                accounts_zeroed TEXT NOT NULL DEFAULT '[]',
                dry_run INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'ok',
                notes TEXT DEFAULT '',
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def save_oauth_state(state: str, code_verifier: str):
    """Guarda el state OAuth en DB para sobrevivir reinicios del servidor."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO oauth_states (state, code_verifier) VALUES (?, ?)",
            (state, code_verifier)
        )
        # Limpiar states viejos (más de 10 minutos)
        await db.execute(
            "DELETE FROM oauth_states WHERE created_at < datetime('now', '-10 minutes')"
        )
        await db.commit()


async def pop_oauth_state(state: str) -> Optional[str]:
    """Obtiene y elimina el code_verifier para un state dado. Retorna None si no existe."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT code_verifier FROM oauth_states WHERE state = ?", (state,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        await db.commit()
        return row["code_verifier"]


async def save_tokens(user_id: str, access_token: str, refresh_token: str, expires_in: int, nickname: str = ""):
    """Guarda o actualiza los tokens de un usuario."""
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO tokens (user_id, access_token, refresh_token, expires_at, nickname)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at,
                nickname = CASE WHEN excluded.nickname != '' THEN excluded.nickname ELSE tokens.nickname END
        """, (user_id, access_token, refresh_token, expires_at, nickname))
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


async def get_all_tokens() -> list:
    """Devuelve todas las cuentas almacenadas (user_id + nickname)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id, nickname FROM tokens ORDER BY created_at")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_daily_goal(user_id: str) -> float:
    """Obtiene la meta diaria de una cuenta. Default: 500,000."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT daily_goal FROM account_settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return float(row["daily_goal"]) if row else 500000.0


async def set_daily_goal(user_id: str, goal: float):
    """Guarda la meta diaria de una cuenta."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO account_settings (user_id, daily_goal, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET daily_goal=excluded.daily_goal, updated_at=excluded.updated_at
        """, (user_id, goal))
        await db.commit()


async def update_nickname(user_id: str, nickname: str):
    """Actualiza el nickname de una cuenta existente."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE tokens SET nickname = ? WHERE user_id = ?",
            (nickname, user_id)
        )
        await db.commit()


async def delete_tokens(user_id: str):
    """Elimina los tokens de un usuario."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
        await db.commit()


async def log_concentration(
    base_sku: str, trigger: str, winner_user_id: str, winner_nickname: str,
    winner_item_id: str, winner_units_30d: int, total_bm_avail: int,
    accounts_zeroed: list, dry_run: bool = True, status: str = "ok", notes: str = ""
):
    """Registra una concentración de stock (real o simulada)."""
    import json as _json
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO stock_concentration_log
            (base_sku, trigger, winner_user_id, winner_nickname, winner_item_id,
             winner_units_30d, total_bm_avail, accounts_zeroed, dry_run, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            base_sku, trigger, winner_user_id, winner_nickname, winner_item_id,
            winner_units_30d, total_bm_avail,
            _json.dumps(accounts_zeroed, ensure_ascii=False),
            1 if dry_run else 0, status, notes
        ))
        await db.commit()


async def get_concentration_log(limit: int = 50) -> list:
    """Obtiene el historial de concentraciones."""
    import json as _json
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM stock_concentration_log ORDER BY executed_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            r = dict(row)
            try:
                r["accounts_zeroed"] = _json.loads(r.get("accounts_zeroed") or "[]")
            except Exception:
                r["accounts_zeroed"] = []
            result.append(r)
        return result


async def last_concentration_for_sku(base_sku: str, hours: int = 24) -> Optional[dict]:
    """Verifica si ya se concentró este SKU en las últimas N horas (para evitar duplicados)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM stock_concentration_log
            WHERE base_sku = ? AND dry_run = 0 AND status = 'ok'
              AND executed_at >= datetime('now', ?)
            ORDER BY executed_at DESC LIMIT 1
        """, (base_sku, f"-{hours} hours"))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_concentrated_skus(days: int = 30) -> list:
    """Retorna lista de SKUs concentrados exitosamente en los últimos N días."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("""
            SELECT DISTINCT base_sku
            FROM stock_concentration_log
            WHERE dry_run = 0 AND status = 'ok'
              AND executed_at >= datetime('now', ?)
            ORDER BY base_sku
        """, (f"-{days} days",))
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def is_token_expired(user_id: str) -> bool:
    """Verifica si el token ha expirado."""
    tokens = await get_tokens(user_id)
    if not tokens:
        return True
    expires_at = datetime.fromisoformat(tokens["expires_at"])
    return datetime.utcnow() >= expires_at
