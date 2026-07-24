"""
user_store.py — Gestión de usuarios del dashboard, sesiones y auditoría.

Tablas:
  dashboard_users  — cuentas de acceso al dashboard (admin/editor/viewer)
  user_sessions    — tokens de sesión activos
  audit_log        — registro de acciones por usuario
"""

import hashlib
import hmac
import base64
import secrets
import time
import json
import os
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
from app.config import DATABASE_PATH

# JWT signing key — stable across container restarts if set as Railway env var.
# If not set, derive a deterministic fallback from DB path so at least all
# processes on the same host share the same key.
_SECRET_KEY = os.getenv("SECRET_KEY") or (
    hashlib.sha256(f"apantallate-dash:{DATABASE_PATH}".encode()).hexdigest()
)
_SESSION_DAYS = 30


def _jwt_sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    sig = hmac.new(_SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _jwt_verify(token: str) -> Optional[dict]:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "==").decode())
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


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
ROLE_CAN_FACTURACION      = {"admin", "editor", "editor_meli", "editor_amazon", "editor_facturacion"}
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

# ─── Árbol de permisos jerárquico (tab → subtabs), por plataforma ─────────────
# Cada tab puede otorgarse completo ("ml.salud") o por subtab específico
# ("ml.salud.messages"). Tabs sin dict de subtabs son "todo o nada".
# Mismo árbol usado por: middleware de acceso, filtro de nav, checkboxes del
# panel de usuarios y gating de partials/endpoints de subtabs.
PERMISSION_TREE = {
    "ml": {
        "dashboard":    {"label": "Dashboard",    "subtabs": None},
        "ventas":       {"label": "Ventas",       "subtabs": None},
        "productos":    {"label": "Productos",    "subtabs": {
            "summary": "Resumen", "inventory": "Inventario", "stock": "Stock",
            "deals": "Deals", "listings": "Listings", "full-candidates": "Candidatos FULL",
        }},
        "ads":          {"label": "Ads",          "subtabs": {
            "campaigns": "Campañas", "performance": "Rendimiento", "by-category": "Por categoría",
            "brand": "Brand Ads", "burning": "Quemando presupuesto", "no-ads": "Sin ads",
            "asignar": "Asignar", "suggestions": "Sugerencias",
        }},
        "salud":        {"label": "Salud",        "subtabs": {
            "claims": "Reclamos", "questions": "Preguntas", "messages": "Mensajes",
            "reputation": "Reputación", "vigilancia": "Vigilancia", "scores": "Score",
        }},
        "devoluciones": {"label": "Devoluciones",  "subtabs": None},
        "planning":     {"label": "Planning",      "subtabs": None},
        "facturacion":  {"label": "Facturación",   "subtabs": None},
        "sync":         {"label": "Sync Stock",    "subtabs": {
            "ejecutar": "Ejecutar", "configurar": "Configurar",
        }},
    },
    "amz": {
        "dashboard":    {"label": "Dashboard",    "subtabs": None},
        "ventas":       {"label": "Ventas",       "subtabs": {
            "resumen": "Resumen", "sku": "Por SKU", "finanzas": "Finanzas",
        }},
        "productos":    {"label": "Productos",    "subtabs": {
            "listings": "Listings", "deals": "Deals",
        }},
        "salud":        {"label": "Salud",        "subtabs": {
            "resumen": "Resumen", "mensajes": "Mensajes de Compradores", "vigilancia": "Vigilancia",
        }},
        "fba":          {"label": "FBA & Stock",  "subtabs": {
            "reabastecimiento": "Reabastecimiento", "catalogo": "Catálogo",
        }},
        "returns":      {"label": "Retornos",     "subtabs": None},
    },
}

# Consolidación 2026-07-24: Finanzas/Listings/Deals/Operaciones (Amazon) dejaron
# de ser tabs de nivel superior — quedaron fusionados como subtabs de Ventas/
# Productos/FBA & Stock respectivamente (mismo patrón que ML ya usaba). Mapeo
# de las claves viejas (emitidas el mismo día, antes de esta consolidación) a
# sus equivalentes nuevos.
_LEGACY_AMZ_TAB_MAP = {
    "amz.finanzas": "amz.ventas.finanzas",
    "amz.listings": "amz.productos.listings",
    "amz.deals": "amz.productos.deals",
    "amz.operaciones": "amz.fba.catalogo",
}

# Migración de secciones "planas" (esquema viejo, previo al árbol tab→subtab)
# a claves nuevas "plataforma.tab". "amazon" (acceso total a Amazon) se
# expande a todos los tabs de la plataforma "amz". "deuda" no es parte del
# árbol (es admin_only real) y se deja pasar tal cual.
_LEGACY_ML_MAP = {
    "dashboard": "ml.dashboard", "ventas": "ml.ventas", "sku": "ml.ventas",
    "productos": "ml.productos", "ads": "ml.ads", "salud": "ml.salud",
    "devoluciones": "ml.devoluciones", "planning": "ml.planning",
    "facturacion": "ml.facturacion", "sync": "ml.sync",
}


def _expand_legacy_sections(sections: list) -> list:
    """Convierte claves del esquema plano viejo (ej. 'salud', 'amazon') a las
    claves nuevas 'plataforma.tab' del árbol de permisos. Claves ya en formato
    nuevo (contienen '.') o especiales ('deuda') pasan sin cambio. Idempotente
    — puede aplicarse sobre una lista ya expandida sin efecto adicional."""
    if not sections:
        return []
    out = set()
    for s in sections:
        if not s:
            continue
        if s in _LEGACY_AMZ_TAB_MAP:
            out.add(_LEGACY_AMZ_TAB_MAP[s])
        elif "." in s:
            out.add(s)
        elif s == "amazon":
            out.update(f"amz.{tab}" for tab in PERMISSION_TREE["amz"])
        elif s in _LEGACY_ML_MAP:
            out.add(_LEGACY_ML_MAP[s])
        else:
            out.add(s)  # ej. "deuda" — passthrough, no forma parte del árbol
    return sorted(out)


def has_tab_access(sections: list, platform: str, tab: str) -> bool:
    """True si el usuario tiene acceso a TODO o a AL MENOS UN subtab del tab."""
    exp = _expand_legacy_sections(sections)
    whole = f"{platform}.{tab}"
    if whole in exp:
        return True
    prefix = whole + "."
    return any(s.startswith(prefix) for s in exp)


def has_subtab_access(sections: list, platform: str, tab: str, subtab: str) -> bool:
    """True si el usuario tiene el tab completo O ese subtab específico."""
    exp = _expand_legacy_sections(sections)
    return f"{platform}.{tab}" in exp or f"{platform}.{tab}.{subtab}" in exp


def get_allowed_subtabs(sections: list, platform: str, tab: str) -> list:
    """Lista de keys de subtabs permitidos para ese tab. Si el tab no tiene
    subtabs definidos en el árbol, o el usuario tiene el tab completo,
    retorna TODAS las keys de subtabs (o [] si el tab no tiene subtabs)."""
    all_subtabs = (PERMISSION_TREE.get(platform, {}).get(tab, {}) or {}).get("subtabs")
    if not all_subtabs:
        return []
    exp = _expand_legacy_sections(sections)
    if f"{platform}.{tab}" in exp:
        return list(all_subtabs.keys())
    prefix = f"{platform}.{tab}."
    return [s[len(prefix):] for s in exp if s.startswith(prefix) and s[len(prefix):] in all_subtabs]


def describe_section_key(key: str) -> str:
    """Etiqueta legible para una clave del árbol ('ml.salud.messages' →
    'ML · Salud → Mensajes'). Usada en los chips del panel de usuarios."""
    if not key or "." not in key:
        return {"deuda": "Deuda"}.get(key, key)
    parts = key.split(".")
    platform_label = "ML" if parts[0] == "ml" else "Amazon"
    tab_meta = PERMISSION_TREE.get(parts[0], {}).get(parts[1], {})
    tab_label = tab_meta.get("label", parts[1])
    if len(parts) == 2:
        return f"{platform_label} · {tab_label}"
    subtab_label = (tab_meta.get("subtabs") or {}).get(parts[2], parts[2])
    return f"{platform_label} · {tab_label} → {subtab_label}"


def first_allowed_location(sections: list) -> tuple:
    """Retorna (platform, tab, subtab_o_None) del primer acceso permitido —
    usado para redirigir cuando se niega el acceso a una ruta. Prioriza ML
    sobre Amazon (mismo orden que la navegación) y sigue el orden del árbol."""
    exp = set(_expand_legacy_sections(sections))
    if "deuda" in exp:
        return ("ml", "deuda", None)
    for platform in ("ml", "amz"):
        for tab, meta in PERMISSION_TREE[platform].items():
            if has_tab_access(sections, platform, tab):
                subtabs = meta.get("subtabs")
                if subtabs:
                    allowed_sub = get_allowed_subtabs(sections, platform, tab)
                    if allowed_sub:
                        return (platform, tab, allowed_sub[0])
                return (platform, tab, None)
    return (None, None, None)


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
                ml_account  TEXT NOT NULL DEFAULT '',
                section     TEXT NOT NULL DEFAULT '',
                ts          TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migrations: agregar columnas a audit_log existente
        for _col, _def in [("ml_account", "TEXT NOT NULL DEFAULT ''"), ("section", "TEXT NOT NULL DEFAULT ''")]:
            try:
                await db.execute(f"ALTER TABLE audit_log ADD COLUMN {_col} {_def}")
            except Exception:
                pass
        # Tabla: presencia activa de usuarios (actualizada en cada request)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_last_seen (
                username     TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                last_seen    REAL NOT NULL DEFAULT 0,
                last_url     TEXT NOT NULL DEFAULT '',
                section      TEXT NOT NULL DEFAULT '',
                ml_account   TEXT NOT NULL DEFAULT '',
                ip           TEXT NOT NULL DEFAULT ''
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
        # Migración: la sección "sku" se fusionó en "ventas" (tab unificado Ventas/SKU) —
        # cualquier usuario con "sku" en allowed_sections gana "ventas" si no la tenía ya.
        cur = await db.execute("SELECT id, allowed_sections FROM dashboard_users")
        for uid, raw_sections in await cur.fetchall():
            sections = _parse_allowed_sections(raw_sections)
            if "sku" in sections and "ventas" not in sections:
                sections.append("ventas")
                await db.execute(
                    "UPDATE dashboard_users SET allowed_sections = ? WHERE id = ?",
                    (json.dumps(sections), uid),
                )
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
    if "allowed_sections" in fields or "role" in fields:
        # Los permisos van embebidos en el JWT de sesión — si no se invalida,
        # el usuario sigue con los permisos viejos hasta que el token expire
        # (30 días) o cierre sesión manualmente. Forzar re-login inmediato.
        await delete_user_sessions(user_id)
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
    """Genera un JWT firmado con los datos del usuario embebidos.
    Sobrevive reinicios del contenedor mientras SECRET_KEY sea estable."""
    user = await get_user_by_id(user_id)
    exp = int(time.time()) + _SESSION_DAYS * 86400
    payload = {
        "uid": user_id,
        "exp": exp,
        "username": user.get("username", "") if user else "",
        "dn": user.get("display_name", "") if user else "",
        "role": user.get("role", "viewer") if user else "viewer",
        "mcp": user.get("must_change_pw", 0) if user else 0,
        "sec": _parse_allowed_sections(user.get("allowed_sections")) if user else [],
    }
    token = _jwt_sign(payload)
    # Persistir en DB para auditoría y soporte de logout (best-effort)
    expires = (datetime.utcnow() + timedelta(days=_SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "INSERT INTO user_sessions (user_id, token, ip, expires_at) VALUES (?, ?, ?, ?)",
                (user_id, token, ip, expires),
            )
            await db.commit()
    except Exception:
        pass  # La sesión funciona igual sin DB gracias al JWT
    return token


async def get_session(token: str) -> Optional[dict]:
    """Valida la sesión. Primero intenta verificar el JWT (sin DB).
    Si no es JWT válido, intenta lookup en DB (tokens legacy)."""
    if not token:
        return None
    # Verificar JWT — no requiere DB, sobrevive reinicios
    payload = _jwt_verify(token)
    if payload:
        return {
            "id": payload.get("uid"),
            "username": payload.get("username", ""),
            "display_name": payload.get("dn", ""),
            "role": payload.get("role", "viewer"),
            "must_change_pw": payload.get("mcp", 0),
            "allowed_sections": payload.get("sec", []),
        }
    # Fallback DB para tokens opacos legacy
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
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
    except Exception:
        return None


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
    ml_account: str = "",
    section: str = "",
):
    detail_str = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else (detail or "")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO audit_log (user_id, username, action, item_id, detail, ip, ml_account, section)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, username, action, item_id, detail_str, ip, ml_account or "", section or ""))
        await db.commit()


async def get_audit_log(
    limit: int = 200,
    offset: int = 0,
    username: str = None,
    action: str = None,
    date_from: str = None,
    ml_account: str = None,
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
    if ml_account:
        conditions.append("ml_account = ?")
        params.append(ml_account)
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


async def get_audit_users_summary(days: int = 7) -> list[dict]:
    """Estadísticas de actividad por usuario para el panel de auditoría."""
    from datetime import datetime, timedelta
    date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
                username,
                COUNT(*) as total,
                SUM(CASE WHEN action IN ('ml_item_created','ml_item_reactivated','ml_mark_launched') THEN 1 ELSE 0 END) as launches,
                SUM(CASE WHEN action IN ('ml_price_update','amz_price_update','ml_price_synced') THEN 1 ELSE 0 END) as prices,
                SUM(CASE WHEN action IN ('ml_stock_update','ml_variation_stock','amz_stock_update','amz_listing_update') THEN 1 ELSE 0 END) as stocks,
                MAX(ts) as last_action
            FROM audit_log
            WHERE ts >= ? AND username != 'system'
            GROUP BY username
            ORDER BY total DESC
        """, (date_from,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_audit_user_timeline(
    username: str,
    days: int = 7,
    action_filter: str = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Timeline de actividad de un usuario específico con estadísticas."""
    from datetime import datetime, timedelta
    date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    conditions = ["username = ?", "ts >= ?"]
    params: list = [username, date_from]

    if action_filter:
        conditions.append("action = ?")
        params.append(action_filter)

    where = "WHERE " + " AND ".join(conditions)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN action IN ('ml_item_created','ml_item_reactivated','ml_mark_launched') THEN 1 ELSE 0 END) as launches,
                SUM(CASE WHEN action IN ('ml_price_update','amz_price_update','ml_price_synced') THEN 1 ELSE 0 END) as prices,
                SUM(CASE WHEN action IN ('ml_stock_update','ml_variation_stock','amz_stock_update') THEN 1 ELSE 0 END) as stocks
            FROM audit_log {where}
        """, params)
        stats_row = await cur.fetchone()
        stats = dict(stats_row) if stats_row else {"total": 0, "launches": 0, "prices": 0, "stocks": 0}

        cur = await db.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cur.fetchall()
        return {"stats": stats, "rows": [dict(r) for r in rows]}


async def update_last_seen(
    username: str,
    display_name: str,
    url: str,
    section: str,
    ml_account: str,
    ip: str,
    is_page: bool = True,
) -> None:
    """Registra la última actividad de un usuario. Fire-and-forget desde middleware.

    is_page=True  → navegación real (actualiza sección, cuenta, URL visible)
    is_page=False → poll de API en background (solo actualiza timestamp e IP,
                    preserva la última página/sección conocida)
    """
    import time as _time
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if is_page:
            await db.execute("""
                INSERT INTO user_last_seen (username, display_name, last_seen, last_url, section, ml_account, ip)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen    = excluded.last_seen,
                    last_url     = excluded.last_url,
                    section      = excluded.section,
                    ml_account   = excluded.ml_account,
                    ip           = excluded.ip
            """, (username, display_name, _time.time(), url[:200], section, ml_account, ip))
        else:
            # Solo bump del timestamp — no pisar la sección/cuenta que se fijó en la última carga de página
            await db.execute("""
                INSERT INTO user_last_seen (username, display_name, last_seen, last_url, section, ml_account, ip)
                VALUES (?, ?, ?, '', '', '', ?)
                ON CONFLICT(username) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    ip        = excluded.ip
            """, (username, display_name, _time.time(), ip))
        await db.commit()


async def get_online_users(active_minutes: int = 5) -> list:
    """Retorna todos los usuarios con last_seen, marcando quién está activo ahora."""
    import time as _time
    cutoff = _time.time() - active_minutes * 60
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM user_last_seen ORDER BY last_seen DESC"
        )).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_online"] = d["last_seen"] > cutoff
        result.append(d)
    return result
