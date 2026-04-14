"""
user_store.py — Gestión de usuarios del dashboard, sesiones y auditoría.

Tablas:
  dashboard_users  — cuentas de acceso al dashboard (admin/editor/viewer)
  user_sessions    — tokens de sesión activos
  audit_log        — registro de acciones por usuario
"""

import hashlib
import secrets
import time
import json
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
from app.config import DATABASE_PATH


# ─── Roles disponibles ───────────────────────────────────────────────────────
ROLES = {
    "admin":                "Administrador",
    "editor":               "Editor (MeLi + Amazon)",
    "editor_meli":          "Editor MeLi",
    "editor_amazon":        "Editor Amazon",
    "editor_facturacion":   "Editor Facturación",
    "viewer":               "Solo Lectura",
}

ROLE_CAN_WRITE_MELI       = {"admin", "editor", "editor_meli"}
ROLE_CAN_WRITE_AMAZON     = {"admin", "editor", "editor_amazon"}
ROLE_CAN_FACTURACION      = {"admin", "editor", "editor_meli", "editor_facturacion"}
ROLE_CAN_ADMIN            = {"admin"}

# ─── Secciones disponibles (para control de acceso por sección) ───────────────
ALL_SECTIONS = [
    ("dashboard",    "Dashboard"),
    ("ventas",       "Ventas"),
    ("productos",    "Productos"),
    ("sku",          "SKU"),
    ("ads",          "Ads"),
    ("salud",        "Salud"),
    ("devoluciones", "Devoluciones"),
    ("planning",     "Planning"),
    ("facturacion",  "Facturación"),
    ("sync",         "Sync Stock"),
    ("amazon",       "Amazon"),
]


# ─── Password hashing ────────────────────────────────────────────────────────
def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return h, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    h, _ = hash_password(password, salt)
    return h == stored_hash


# ─── Inicialización DB ───────────────────────────────────────────────────────
def _parse_allowed_sections(raw) -> list:
    """Convierte el campo allowed_sections de DB (JSON string o None) a lista Python."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


async def init_user_db(admin_password: str = "010817xD"):
    """Crea las tablas y el usuario admin inicial si no existe."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_users (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                username          TEXT UNIQUE NOT NULL,
                display_name      TEXT NOT NULL DEFAULT '',
                password_hash     TEXT,
                password_salt     TEXT,
                role              TEXT NOT NULL DEFAULT 'viewer',
                active            INTEGER NOT NULL DEFAULT 1,
                must_change_pw    INTEGER NOT NULL DEFAULT 0,
                created_by        TEXT DEFAULT 'system',
                created_at        TEXT DEFAULT (datetime('now')),
                last_login        TEXT,
                allowed_sections  TEXT DEFAULT NULL
            )
        """)
        # Migración: agregar allowed_sections si la tabla ya existía sin esa columna
        try:
            await db.execute("ALTER TABLE dashboard_users ADD COLUMN allowed_sections TEXT DEFAULT NULL")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                token       TEXT UNIQUE NOT NULL,
                ip          TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                expires_at  TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                username    TEXT NOT NULL DEFAULT 'system',
                action      TEXT NOT NULL,
                item_id     TEXT,
                detail      TEXT,
                ip          TEXT,
                ts          TEXT DEFAULT (datetime('now'))
            )
        """)
        # Crear admin inicial si no existe
        cur = await db.execute("SELECT id FROM dashboard_users WHERE username = 'admin'")
        row = await cur.fetchone()
        if not row:
            ph, salt = hash_password(admin_password)
            await db.execute("""
                INSERT INTO dashboard_users (username, display_name, password_hash, password_salt, role, active, must_change_pw)
                VALUES ('admin', 'Administrador', ?, ?, 'admin', 1, 0)
            """, (ph, salt))
        await db.commit()


# ─── Usuarios ─────────────────────────────────────────────────────────────────
async def get_user_by_username(username: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM dashboard_users WHERE username = ? AND active = 1", (username,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["allowed_sections"] = _parse_allowed_sections(d.get("allowed_sections"))
        return d


async def get_user_by_id(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM dashboard_users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_users() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, username, display_name, role, active, must_change_pw, created_by, created_at, last_login, allowed_sections "
            "FROM dashboard_users ORDER BY id"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def create_user(
    username: str, display_name: str, role: str, created_by: str,
    allowed_sections: list = None,
) -> int:
    """Crea usuario sin contraseña (must_change_pw=1). Retorna el nuevo user_id."""
    sections_json = json.dumps(allowed_sections) if allowed_sections else None
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
            INSERT INTO dashboard_users (username, display_name, role, must_change_pw, created_by, allowed_sections)
            VALUES (?, ?, ?, 1, ?, ?)
        """, (username, display_name, role, created_by, sections_json))
        await db.commit()
        return cur.lastrowid


async def update_user(user_id: int, **kwargs) -> bool:
    """Actualiza campos del usuario. Campos válidos: display_name, role, active, allowed_sections."""
    allowed = {"display_name", "role", "active", "allowed_sections"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    # Serializar allowed_sections a JSON si viene como lista
    if "allowed_sections" in fields:
        val = fields["allowed_sections"]
        fields["allowed_sections"] = json.dumps(val) if isinstance(val, list) else val
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            f"UPDATE dashboard_users SET {sets} WHERE id = ?",
            (*fields.values(), user_id)
        )
        await db.commit()
    return True


async def set_password(user_id: int, password: str) -> bool:
    """Guarda nueva contraseña y quita el flag must_change_pw."""
    ph, salt = hash_password(password)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            UPDATE dashboard_users SET password_hash=?, password_salt=?, must_change_pw=0
            WHERE id=?
        """, (ph, salt, user_id))
        await db.commit()
    return True


async def update_last_login(user_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE dashboard_users SET last_login=datetime('now') WHERE id=?", (user_id,)
        )
        await db.commit()


async def delete_user(user_id: int):
    """Desactiva (soft delete) un usuario."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE dashboard_users SET active=0 WHERE id=?", (user_id,))
        await db.commit()


# ─── Sesiones ─────────────────────────────────────────────────────────────────
async def create_session(user_id: int, ip: str = None) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO user_sessions (user_id, token, ip, expires_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, token, ip, expires))
        await db.commit()
    return token


async def get_session(token: str) -> Optional[dict]:
    """Retorna el usuario asociado al token si la sesión es válida y no expiró."""
    if not token:
        return None
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT u.id, u.username, u.display_name, u.role, u.must_change_pw, u.allowed_sections
            FROM user_sessions s
            JOIN dashboard_users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ? AND u.active = 1
        """, (token, now))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["allowed_sections"] = _parse_allowed_sections(d.get("allowed_sections"))
        return d


async def delete_session(token: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
        await db.commit()


async def delete_user_sessions(user_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
        await db.commit()


# ─── Auditoría ────────────────────────────────────────────────────────────────
async def log_action(
    username: str,
    action: str,
    item_id: str = None,
    detail: dict | str = None,
    ip: str = None,
    user_id: int = None,
):
    detail_str = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else (detail or "")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO audit_log (user_id, username, action, item_id, detail, ip)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username, action, item_id, detail_str, ip))
        await db.commit()


async def get_audit_log(
    limit: int = 200,
    offset: int = 0,
    username: str = None,
    action: str = None,
    date_from: str = None,
) -> list[dict]:
    conditions = []
    params = []
    if username:
        conditions.append("username = ?")
        params.append(username)
    if action:
        conditions.append("action = ?")
        params.append(action)
    if date_from:
        conditions.append("ts >= ?")
        params.append(date_from)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_audit_users() -> list[str]:
    """Lista de usuarios únicos en el log."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT username FROM audit_log ORDER BY username"
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]
