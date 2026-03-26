import json
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
        # ─────────────────────────────────────────────────────────────────
        # TABLA: tokens (cuentas de Mercado Libre)
        # Almacena access_token + refresh_token por user_id de MeLi.
        # El refresh_token se usa para renovar el access_token cuando expira.
        # ─────────────────────────────────────────────────────────────────
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
            CREATE TABLE IF NOT EXISTS amazon_settings (
                seller_id     TEXT PRIMARY KEY,
                stock_threshold INTEGER NOT NULL DEFAULT 5,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: amazon_accounts (cuentas de Amazon Seller)
        # Almacena credenciales LWA (Login with Amazon) para SP-API.
        #
        # Campos clave:
        #   seller_id       → Merchant Token de Amazon (ej. A20NFIUQNEYZ1E)
        #   client_id       → ID de la app LWA (amzn1.application-oa2-client.XXX)
        #   client_secret   → Secret de la app LWA
        #   refresh_token   → Token de larga duración para renovar access_token
        #   access_token    → Token de corta duración (1 hora), se renueva automáticamente
        #   marketplace_id  → ID del marketplace (México = A1AM78C64UM0Y8)
        #   marketplace_name→ Código legible (MX, US, CA)
        #
        # La tabla se separa de 'tokens' (MeLi) para mantener claridad
        # entre plataformas — las estructuras de auth son distintas.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amazon_accounts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id        TEXT UNIQUE NOT NULL,
                nickname         TEXT NOT NULL DEFAULT '',
                client_id        TEXT NOT NULL DEFAULT '',
                client_secret    TEXT NOT NULL DEFAULT '',
                refresh_token    TEXT NOT NULL DEFAULT '',
                access_token     TEXT DEFAULT NULL,
                token_expires_at TIMESTAMP DEFAULT NULL,
                marketplace_id   TEXT NOT NULL DEFAULT 'A1AM78C64UM0Y8',
                marketplace_name TEXT NOT NULL DEFAULT 'MX',
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migración: agregar columna app_id si ya existe la tabla sin ella
        # (para instancias de Railway que ya tienen la tabla creada)
        try:
            await db.execute("ALTER TABLE amazon_accounts ADD COLUMN app_solution_id TEXT DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # Columna ya existe, ignorar

        # ─────────────────────────────────────────────────────────────────
        # TABLA: stock_concentration_log (historial de concentraciones)
        # ─────────────────────────────────────────────────────────────────
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
        # ─────────────────────────────────────────────────────────────────
        # TABLA: sync_alerts (alertas proactivas de sobreventa)
        # Registra items con stock activo en MeLi pero BM disponible = 0
        # Generado por el scheduler automático cada 4 horas
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL DEFAULT '',
                meli_stock INTEGER NOT NULL DEFAULT 0,
                bm_avail INTEGER NOT NULL DEFAULT 0,
                alert_type TEXT NOT NULL DEFAULT 'oversell',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, item_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                user_id TEXT PRIMARY KEY,
                last_run TIMESTAMP DEFAULT NULL,
                last_result TEXT DEFAULT '',
                alerts_count INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: bm_sku_gaps (SKUs con stock en BM pero no lanzados en MeLi)
        # Generado por el scanner nocturno (3am Mexico = 9am UTC)
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_sku_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL, product_title TEXT NOT NULL DEFAULT '',
                brand TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
                stock_mty INTEGER NOT NULL DEFAULT 0, stock_cdmx INTEGER NOT NULL DEFAULT 0,
                stock_total INTEGER NOT NULL DEFAULT 0,
                retail_price_usd REAL NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unlaunched', priority_score INTEGER NOT NULL DEFAULT 0,
                suggested_price_mxn REAL NOT NULL DEFAULT 0, cost_price_mxn REAL NOT NULL DEFAULT 0,
                competitor_price REAL NOT NULL DEFAULT 0, competitor_count INTEGER NOT NULL DEFAULT 0,
                deal_price REAL NOT NULL DEFAULT 0, listing_type_rec TEXT NOT NULL DEFAULT 'gold_special',
                last_scan TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, sku)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_gap_scan_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL DEFAULT 'idle',
                started_at TIMESTAMP DEFAULT NULL, finished_at TIMESTAMP DEFAULT NULL,
                total_skus INTEGER DEFAULT 0, gaps_found INTEGER DEFAULT 0,
                error TEXT DEFAULT NULL
            )
        """)
        await db.execute("INSERT OR IGNORE INTO bm_gap_scan_status (id, status) VALUES (1, 'idle')")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_reactivations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL,
                item_id TEXT NOT NULL,
                product_title TEXT NOT NULL DEFAULT '',
                stock_bm INTEGER NOT NULL DEFAULT 0,
                retail_price_usd REAL NOT NULL DEFAULT 0,
                suggested_price_mxn REAL NOT NULL DEFAULT 0,
                ml_status TEXT NOT NULL DEFAULT 'inactive',
                last_scan TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, item_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ml_price_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL,
                item_id TEXT NOT NULL,
                product_title TEXT NOT NULL DEFAULT '',
                ml_price REAL NOT NULL DEFAULT 0,
                bm_suggested_mxn REAL NOT NULL DEFAULT 0,
                diff_pct REAL NOT NULL DEFAULT 0,
                last_scan TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, item_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ml_listing_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL,
                item_id TEXT NOT NULL,
                product_title TEXT NOT NULL DEFAULT '',
                ml_price REAL NOT NULL DEFAULT 0,
                quality_score INTEGER NOT NULL DEFAULT 0,
                pics_count INTEGER NOT NULL DEFAULT 0,
                has_gtin INTEGER NOT NULL DEFAULT 0,
                has_brand INTEGER NOT NULL DEFAULT 0,
                title_len INTEGER NOT NULL DEFAULT 0,
                last_scan TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, item_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ml_competition_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL,
                item_id TEXT NOT NULL,
                product_title TEXT NOT NULL DEFAULT '',
                ml_price REAL NOT NULL DEFAULT 0,
                competitor_price REAL NOT NULL DEFAULT 0,
                diff_pct REAL NOT NULL DEFAULT 0,
                last_scan TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, item_id)
            )
        """)
        # Migrations — add columns if not present (SQLite doesn't support IF NOT EXISTS on columns)
        for col, definition in [("upc", "TEXT NOT NULL DEFAULT ''"), ("size", "TEXT NOT NULL DEFAULT ''")]:
            try:
                await db.execute(f"ALTER TABLE bm_sku_gaps ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS item_sku_cache (
                item_id   TEXT PRIMARY KEY,
                user_id   TEXT NOT NULL DEFAULT '',
                sku       TEXT NOT NULL DEFAULT '',
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amazon_vel_cache (
                days         INTEGER PRIMARY KEY,
                data_json    TEXT NOT NULL DEFAULT '{}',
                computed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


# ═══════════════════════════════════════════════════════════════════════════
# AMAZON ACCOUNTS — Funciones CRUD para cuentas de Amazon Seller
#
# Separadas completamente de las funciones de Mercado Libre para
# mantener claridad. Amazon usa LWA (Login with Amazon) mientras que
# MeLi usa OAuth 2.0 + PKCE — son flujos distintos.
# ═══════════════════════════════════════════════════════════════════════════

async def save_amazon_account(
    seller_id: str,
    nickname: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    marketplace_id: str = "A1AM78C64UM0Y8",
    marketplace_name: str = "MX",
    app_solution_id: str = "",
):
    """
    Guarda o actualiza una cuenta de Amazon Seller.

    Se llama en dos momentos:
    1. Al hacer bootstrap desde .env.production (solo seller_id + credenciales)
    2. Después del callback OAuth (ya con refresh_token real)

    El access_token NO se guarda aquí — se renueva en memoria por AmazonClient.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO amazon_accounts
                (seller_id, nickname, client_id, client_secret, refresh_token,
                 marketplace_id, marketplace_name, app_solution_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(seller_id) DO UPDATE SET
                nickname         = CASE WHEN excluded.nickname != '' THEN excluded.nickname ELSE amazon_accounts.nickname END,
                client_id        = CASE WHEN excluded.client_id != '' THEN excluded.client_id ELSE amazon_accounts.client_id END,
                client_secret    = CASE WHEN excluded.client_secret != '' THEN excluded.client_secret ELSE amazon_accounts.client_secret END,
                refresh_token    = CASE WHEN excluded.refresh_token != '' THEN excluded.refresh_token ELSE amazon_accounts.refresh_token END,
                marketplace_id   = excluded.marketplace_id,
                marketplace_name = excluded.marketplace_name,
                app_solution_id  = CASE WHEN excluded.app_solution_id != '' THEN excluded.app_solution_id ELSE amazon_accounts.app_solution_id END
        """, (seller_id, nickname, client_id, client_secret, refresh_token,
              marketplace_id, marketplace_name, app_solution_id))
        await db.commit()


async def get_amazon_account(seller_id: str) -> Optional[dict]:
    """
    Obtiene los datos de una cuenta Amazon por su seller_id.
    Retorna None si no existe.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM amazon_accounts WHERE seller_id = ?", (seller_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_amazon_accounts() -> list:
    """
    Devuelve todas las cuentas Amazon configuradas.
    Cada elemento incluye: seller_id, nickname, marketplace_id, marketplace_name.
    Usado por el selector de cuentas en el header del dashboard.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT seller_id, nickname, marketplace_id, marketplace_name FROM amazon_accounts ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_amazon_account(seller_id: str):
    """Elimina una cuenta Amazon de la base de datos."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM amazon_accounts WHERE seller_id = ?", (seller_id,))
        await db.commit()


async def get_all_accounts() -> dict:
    """
    Devuelve TODAS las cuentas de TODAS las plataformas en un solo dict.

    Estructura retornada:
    {
        "meli":   [{"user_id": "...", "nickname": "...", "platform": "meli"}, ...],
        "amazon": [{"seller_id": "...", "nickname": "...", "platform": "amazon", ...}, ...]
    }

    Usado por el dropdown de cuentas en el header del dashboard para
    mostrar secciones separadas: "MERCADO LIBRE" y "AMAZON".
    """
    meli_accounts = await get_all_tokens()
    # Agregar campo platform a cada cuenta MeLi para que el template sepa el ícono
    for acc in meli_accounts:
        acc["platform"] = "meli"

    amazon_accounts = await get_all_amazon_accounts()
    for acc in amazon_accounts:
        acc["platform"] = "amazon"

    return {
        "meli": meli_accounts,
        "amazon": amazon_accounts,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SYNC ALERTS — Alertas proactivas de sobreventa
# ─────────────────────────────────────────────────────────────────────────────

async def save_sync_alerts(user_id: str, alerts: list):
    """Reemplaza las alertas actuales del user_id con la nueva lista.
    alerts: lista de dicts con keys: item_id, title, sku, meli_stock, bm_avail, alert_type
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM sync_alerts WHERE user_id = ?", (user_id,))
        for a in alerts:
            await db.execute("""
                INSERT OR REPLACE INTO sync_alerts
                    (user_id, item_id, title, sku, meli_stock, bm_avail, alert_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                a.get("item_id", ""),
                a.get("title", "")[:200],
                a.get("sku", ""),
                a.get("meli_stock", 0),
                a.get("bm_avail", 0),
                a.get("alert_type", "oversell"),
            ))
        await db.commit()


async def get_sync_alerts(user_id: str) -> list:
    """Retorna las alertas actuales para user_id."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_alerts WHERE user_id = ? ORDER BY meli_stock DESC",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_sync_alerts() -> list:
    """Retorna todas las alertas de todos los usuarios."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_alerts ORDER BY user_id, meli_stock DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def save_sync_status(user_id: str, alerts_count: int, result: str = "ok"):
    """Actualiza el estado del último sync para user_id."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO sync_status (user_id, last_run, last_result, alerts_count, updated_at)
            VALUES (?, datetime('now'), ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                last_run = excluded.last_run,
                last_result = excluded.last_result,
                alerts_count = excluded.alerts_count,
                updated_at = excluded.updated_at
        """, (user_id, result, alerts_count))
        await db.commit()


async def get_sync_status(user_id: str) -> Optional[dict]:
    """Retorna el estado del último sync para user_id."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_status WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_amazon_stock_threshold(seller_id: str) -> int:
    """Retorna el umbral de stock bajo configurado para la cuenta."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT stock_threshold FROM amazon_settings WHERE seller_id = ?",
            (seller_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 5

# ─── ITEM SKU CACHE ───────────────────────────────────────────────────────────

async def get_cached_skus(item_ids: list) -> dict:
    """Retorna {item_id: sku} para los item_ids que están en caché (sku no vacío)."""
    if not item_ids:
        return {}
    result = {}
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        # SQLite limit: 999 variables per query — chunk to be safe
        for i in range(0, len(item_ids), 500):
            chunk = item_ids[i:i+500]
            placeholders = ",".join("?" * len(chunk))
            cursor = await db.execute(
                f"SELECT item_id, sku FROM item_sku_cache WHERE item_id IN ({placeholders}) AND sku != ''",
                chunk,
            )
            for row in await cursor.fetchall():
                result[row["item_id"]] = row["sku"]
    return result


async def save_skus_cache(entries: list) -> None:
    """Guarda [{item_id, user_id, sku}] en caché. Ignora entradas con sku vacío."""
    valid = [e for e in entries if e.get("sku") and e.get("item_id")]
    if not valid:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for e in valid:
            await db.execute(
                """INSERT INTO item_sku_cache (item_id, user_id, sku, synced_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(item_id) DO UPDATE SET
                       sku = excluded.sku,
                       user_id = excluded.user_id,
                       synced_at = CURRENT_TIMESTAMP""",
                (e["item_id"], e.get("user_id", ""), e["sku"]),
            )
        await db.commit()


async def get_amazon_vel_cache(days: int, max_age_hours: int = 2) -> Optional[dict]:
    """Retorna caché de velocidad Amazon si existe y no expiró. None si no hay."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT data_json FROM amazon_vel_cache "
            "WHERE days = ? AND computed_at > datetime('now', ? || ' hours')",
            (days, f"-{max_age_hours}"),
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None


async def save_amazon_vel_cache(days: int, data: dict) -> None:
    """Guarda/actualiza caché de velocidad Amazon para N días."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO amazon_vel_cache (days, data_json) VALUES (?, ?) "
            "ON CONFLICT(days) DO UPDATE SET "
            "data_json=excluded.data_json, computed_at=CURRENT_TIMESTAMP",
            (days, json.dumps(data)),
        )
        await db.commit()


async def set_amazon_stock_threshold(seller_id: str, threshold: int) -> None:
    """Guarda el umbral de stock bajo para la cuenta."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO amazon_settings (seller_id, stock_threshold, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(seller_id) DO UPDATE SET
                stock_threshold = excluded.stock_threshold,
                updated_at = CURRENT_TIMESTAMP
        """, (seller_id, threshold))
        await db.commit()
