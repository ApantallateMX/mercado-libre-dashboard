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
        for col, definition in [
            ("upc",           "TEXT NOT NULL DEFAULT ''"),
            ("size",          "TEXT NOT NULL DEFAULT ''"),
            ("ml_item_id",    "TEXT NOT NULL DEFAULT ''"),
            ("ml_title",      "TEXT NOT NULL DEFAULT ''"),
            ("ml_price",      "REAL NOT NULL DEFAULT 0"),
            ("ml_category_id","TEXT NOT NULL DEFAULT ''"),
            ("ml_permalink",  "TEXT NOT NULL DEFAULT ''"),
            ("ml_condition",  "TEXT NOT NULL DEFAULT ''"),
            ("launched_at",   "TIMESTAMP DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE bm_sku_gaps ADD COLUMN {col} {definition}")
                await db.commit()
            except Exception:
                pass  # column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS item_sku_cache (
                item_id   TEXT NOT NULL DEFAULT '',
                user_id   TEXT NOT NULL DEFAULT '',
                sku       TEXT NOT NULL DEFAULT '',
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (item_id, sku)
            )
        """)
        # ─── Migración: item_sku_cache v2 — PRIMARY KEY (item_id, sku) ──────────
        # La versión anterior tenía item_id TEXT PRIMARY KEY, lo que causaba que
        # SKUs combinados ("SNTV006296 / SNWM000001") perdieran el primer SKU al
        # hacer ON CONFLICT UPDATE con el segundo. Se migra a composite PK y se
        # limpia la cache corrompida para que el siguiente scan repopule correctamente.
        try:
            cur = await db.execute("SELECT COUNT(*) FROM pragma_table_info('item_sku_cache') WHERE pk=1 AND name='item_id' AND (SELECT COUNT(*) FROM pragma_table_info('item_sku_cache') WHERE pk>0) = 1")
            row = await cur.fetchone()
            if row and row[0] == 1:
                # Old schema detected (single PK on item_id) — migrate
                await db.execute("DROP TABLE item_sku_cache")
                await db.execute("""
                    CREATE TABLE item_sku_cache (
                        item_id   TEXT NOT NULL DEFAULT '',
                        user_id   TEXT NOT NULL DEFAULT '',
                        sku       TEXT NOT NULL DEFAULT '',
                        synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (item_id, sku)
                    )
                """)
        except Exception:
            pass
        # ─── Migración: limpiar entradas con SKU combinado (ej. "SKU1 / SKU2") ──────
        # Cuando ML almacena seller_custom_field o SELLER_SKU attribute como valor
        # combinado, el código antiguo lo guardaba tal cual. Ahora _primary() extrae
        # solo el primer SKU de 10 chars, pero si el entry corrupto ya estaba en cache
        # el item no se re-fetcheaba. Se eliminan entradas con separadores para forzar
        # re-fetch en el siguiente scan.
        try:
            await db.execute(
                "DELETE FROM item_sku_cache WHERE sku LIKE ? OR sku LIKE ? OR sku LIKE ? OR sku LIKE ?",
                ("% / %", "% + %", "% \\ %", "%/%")
            )
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────
        # TABLA: product_videos — asocia videos generados con listings ML
        # Permite mostrar botón "Subir Clip" en cada listing donde hay video
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS product_videos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT '',
                sku         TEXT NOT NULL DEFAULT '',
                video_id    TEXT NOT NULL,
                clip_status TEXT NOT NULL DEFAULT 'pending',
                clip_uuid   TEXT DEFAULT NULL,
                clip_error  TEXT DEFAULT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(item_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amazon_vel_cache (
                days         INTEGER PRIMARY KEY,
                data_json    TEXT NOT NULL DEFAULT '{}',
                computed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ── Multi-platform stock sync ──────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sku_platform_rules (
                user_id     TEXT NOT NULL DEFAULT '',
                sku         TEXT NOT NULL,
                platform_id TEXT NOT NULL,
                enabled     INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, sku, platform_id)
            )
        """)
        # Migración: agregar user_id si la tabla ya existía sin esa columna
        try:
            await db.execute("ALTER TABLE sku_platform_rules ADD COLUMN user_id TEXT DEFAULT ''")
        except Exception:
            pass  # columna ya existe
        await db.execute("""
            CREATE TABLE IF NOT EXISTS multi_stock_sync_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               REAL NOT NULL,
                skus_processed   INTEGER DEFAULT 0,
                updates          INTEGER DEFAULT 0,
                errors           INTEGER DEFAULT 0,
                results_json     TEXT DEFAULT '[]',
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: ml_listings — caché local de listings ML
        # Sincronizado en background; permite leer Stock tab sin llamar API
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ml_listings (
                item_id        TEXT PRIMARY KEY,
                account_id     TEXT NOT NULL,
                title          TEXT DEFAULT '',
                status         TEXT DEFAULT 'active',
                price          REAL DEFAULT 0,
                available_qty  INTEGER DEFAULT 0,
                sold_qty       INTEGER DEFAULT 0,
                sku            TEXT DEFAULT '',
                logistic_type  TEXT DEFAULT '',
                catalog_listing INTEGER DEFAULT 0,
                is_full        INTEGER DEFAULT 0,
                last_updated   TEXT DEFAULT '',
                synced_at      REAL DEFAULT 0
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ml_listings_account ON ml_listings(account_id, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ml_listings_sku ON ml_listings(sku)"
        )
        # Migration: add data_json column (full item body for fast prewarm from DB)
        try:
            await db.execute("ALTER TABLE ml_listings ADD COLUMN data_json TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists
        # Migration: add base_sku column (normalized BM SKU for gap scan without API calls)
        try:
            await db.execute("ALTER TABLE ml_listings ADD COLUMN base_sku TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists
        try:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ml_listings_base_sku ON ml_listings(account_id, base_sku)"
            )
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────
        # TABLA: amazon_listings — caché local de listings Amazon
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amazon_listings (
                seller_id     TEXT NOT NULL,
                sku           TEXT NOT NULL,
                base_sku      TEXT DEFAULT '',
                asin          TEXT DEFAULT '',
                title         TEXT DEFAULT '',
                status        TEXT DEFAULT 'ACTIVE',
                price         REAL DEFAULT 0,
                available_qty INTEGER DEFAULT 0,
                can_update    INTEGER DEFAULT 1,
                fulfillment   TEXT DEFAULT '',
                synced_at     REAL DEFAULT 0,
                PRIMARY KEY (seller_id, sku)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_amz_listings_seller ON amazon_listings(seller_id)"
        )
        # ─────────────────────────────────────────────────────────────────
        # TABLA: bm_stock_cache — persiste el caché de BM entre reinicios
        # Permite que el prewarm lea BM en <100ms después de un restart
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_stock_cache (
                sku       TEXT PRIMARY KEY,
                data_json TEXT NOT NULL DEFAULT '{}',
                synced_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS return_flags (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL DEFAULT '',
                item_id    TEXT NOT NULL,
                flag_type  TEXT NOT NULL DEFAULT 'review',
                note       TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                resolved   INTEGER DEFAULT 0
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_return_flags_item ON return_flags(item_id)"
        )
        # Migración: agregar user_id si la tabla ya existía sin esa columna
        try:
            await db.execute("ALTER TABLE return_flags ADD COLUMN user_id TEXT DEFAULT ''")
        except Exception:
            pass  # columna ya existe
        # Índice sobre user_id — se crea después de asegurar que la columna existe
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_return_flags_user ON return_flags(user_id)")
        except Exception:
            pass

        # ─────────────────────────────────────────────────────────────────
        # TABLAS: Módulo de Facturación
        # billing_requests   — solicitud creada por el equipo interno
        # billing_fiscal_data— datos fiscales llenados por el cliente
        # billing_invoices   — PDF de factura subido por contabilidad
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS billing_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                token        TEXT UNIQUE NOT NULL,
                ml_user_id   TEXT NOT NULL DEFAULT '',
                platform     TEXT NOT NULL DEFAULT 'mercadolibre',
                order_number TEXT NOT NULL DEFAULT '',
                client_ref   TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending_data',
                order_data   TEXT NOT NULL DEFAULT '{}',
                created_by   TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL DEFAULT '',
                notes        TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS billing_fiscal_data (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id       INTEGER UNIQUE NOT NULL,
                rfc              TEXT NOT NULL DEFAULT '',
                razon_social     TEXT NOT NULL DEFAULT '',
                cfdi_use         TEXT NOT NULL DEFAULT '',
                fiscal_regime    TEXT NOT NULL DEFAULT '',
                zip_code         TEXT NOT NULL DEFAULT '',
                forma_pago       TEXT NOT NULL DEFAULT '',
                email            TEXT NOT NULL DEFAULT '',
                phone            TEXT NOT NULL DEFAULT '',
                street           TEXT NOT NULL DEFAULT '',
                constancia_data  BLOB,
                constancia_name  TEXT NOT NULL DEFAULT '',
                submitted_at     TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS billing_invoices (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id   INTEGER UNIQUE NOT NULL,
                filename     TEXT NOT NULL DEFAULT '',
                file_data    BLOB NOT NULL,
                xml_filename TEXT NOT NULL DEFAULT '',
                xml_data     BLOB,
                uploaded_by  TEXT NOT NULL DEFAULT '',
                uploaded_at  TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migration: add XML columns if table already exists without them
        try:
            await db.execute("ALTER TABLE billing_invoices ADD COLUMN xml_filename TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE billing_invoices ADD COLUMN xml_data BLOB")
        except Exception:
            pass
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_requests_token ON billing_requests(token)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_requests_status ON billing_requests(status)"
        )
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
    """Retorna {item_id: sku} para los item_ids que están en caché (sku no vacío).
    Con la PK compuesta (item_id, sku) puede haber múltiples filas por item,
    pero en la práctica siempre es 1 (BM SKUs son exactamente 10 chars)."""
    if not item_ids:
        return {}
    result: dict[str, str] = {}
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
                # First row wins — only 1 SKU per item expected
                if row["item_id"] not in result:
                    result[row["item_id"]] = row["sku"]
    return result


async def save_skus_cache(entries: list) -> None:
    """Guarda [{item_id, user_id, sku}] en caché. Soporta múltiples SKUs por item_id.
    Ignora entradas con sku vacío. PK compuesta (item_id, sku) — no sobreescribe."""
    valid = [e for e in entries if e.get("sku") and e.get("item_id")]
    if not valid:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for e in valid:
            await db.execute(
                """INSERT INTO item_sku_cache (item_id, user_id, sku, synced_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(item_id, sku) DO UPDATE SET
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


# ─── PRODUCT VIDEOS — asocia videos generados con listings ML ─────────────────

async def save_product_video(item_id: str, user_id: str, sku: str, video_id: str) -> None:
    """Guarda o actualiza la asociación video_id ↔ item_id para un usuario."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO product_videos (item_id, user_id, sku, video_id, clip_status)
            VALUES (?, ?, ?, ?, 'pending')
            ON CONFLICT(item_id, user_id) DO UPDATE SET
                video_id   = excluded.video_id,
                sku        = CASE WHEN excluded.sku != '' THEN excluded.sku ELSE product_videos.sku END,
                clip_status = 'pending',
                clip_uuid  = NULL,
                clip_error = NULL,
                updated_at = CURRENT_TIMESTAMP
        """, (item_id, user_id, sku, video_id))
        await db.commit()


async def get_product_video(item_id: str, user_id: str) -> Optional[dict]:
    """Retorna el registro de video para un item, o None si no existe."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM product_videos WHERE item_id=? AND user_id=?",
            (item_id, user_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_clip_status(
    item_id: str, user_id: str, status: str,
    clip_uuid: str = None, error: str = None
) -> None:
    """Actualiza el estado del clip tras upload a ML."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            UPDATE product_videos
            SET clip_status=?, clip_uuid=?, clip_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE item_id=? AND user_id=?
        """, (status, clip_uuid, error, item_id, user_id))
        await db.commit()


async def get_videos_for_items(item_ids: list, user_id: str) -> dict:
    """Retorna {item_id: record} para una lista de item_ids."""
    if not item_ids:
        return {}
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(item_ids))
        rows = await (await db.execute(
            f"SELECT * FROM product_videos WHERE item_id IN ({placeholders}) AND user_id=?",
            item_ids + [user_id]
        )).fetchall()
        return {r["item_id"]: dict(r) for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-PLATFORM STOCK SYNC — reglas y log
# ─────────────────────────────────────────────────────────────────────────────

async def get_all_sku_platform_rules(user_id: str = "") -> dict:
    """
    Retorna {sku_upper: [platform_id, ...]} donde enabled=1 para este user_id.
    Si un SKU no tiene reglas → no aparece aquí → todas las plataformas habilitadas.
    platform_id: "ml_{user_id}" o "amz_{seller_id}"
    Para stock sync global (user_id="") retorna reglas de todos los usuarios.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            rows = await (await db.execute(
                "SELECT sku, platform_id FROM sku_platform_rules WHERE user_id=? AND enabled=1",
                (user_id,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT sku, platform_id FROM sku_platform_rules WHERE enabled=1"
            )).fetchall()
    result: dict = {}
    for row in rows:
        result.setdefault(row["sku"].upper(), []).append(row["platform_id"])
    return result


async def set_sku_platform_rule(user_id: str, sku: str, platform_id: str, enabled: bool) -> None:
    """Habilita o deshabilita una plataforma para un SKU específico, por cuenta."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO sku_platform_rules (user_id, sku, platform_id, enabled)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, sku, platform_id) DO UPDATE SET enabled = excluded.enabled""",
            (user_id, sku.upper(), platform_id, 1 if enabled else 0),
        )
        await db.commit()


async def save_multi_sync_log(
    ts: float,
    skus_processed: int,
    updates: int,
    errors: int,
    results: list,
) -> None:
    """Guarda el resultado de un ciclo de sync en multi_stock_sync_log."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO multi_stock_sync_log
               (ts, skus_processed, updates, errors, results_json)
               VALUES (?, ?, ?, ?, ?)""",
            (ts, skus_processed, updates, errors, json.dumps(results)),
        )
        # Mantener solo los últimos 200 registros
        await db.execute(
            """DELETE FROM multi_stock_sync_log WHERE id NOT IN (
               SELECT id FROM multi_stock_sync_log ORDER BY id DESC LIMIT 200)"""
        )
        await db.commit()


async def get_multi_sync_last_runs(limit: int = 10) -> list:
    """Retorna los últimos N ciclos de sync con resumen (sin results_json completo)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT id, ts, skus_processed, updates, errors, created_at
               FROM multi_stock_sync_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        )).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# ML LISTINGS CACHE — caché local de listings para evitar llamadas repetidas
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_ml_listings(rows: list[dict]) -> None:
    """Inserta o actualiza listings ML en la tabla local."""
    if not rows:
        return
    from app.services.sku_utils import normalize_to_bm_sku
    for row in rows:
        if not row.get("base_sku"):
            row["base_sku"] = normalize_to_bm_sku(row.get("sku", "")) or ""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO ml_listings
               (item_id, account_id, title, status, price, available_qty, sold_qty,
                sku, base_sku, logistic_type, catalog_listing, is_full, last_updated, synced_at, data_json)
               VALUES (:item_id,:account_id,:title,:status,:price,:available_qty,:sold_qty,
                       :sku,:base_sku,:logistic_type,:catalog_listing,:is_full,:last_updated,:synced_at,
                       :data_json)""",
            rows,
        )
        await db.commit()


async def get_ml_listings(account_id: str, statuses: list[str] | None = None) -> list[dict]:
    """Retorna listings de una cuenta desde la DB local. statuses=None → todos."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            rows = await (await db.execute(
                f"SELECT * FROM ml_listings WHERE account_id=? AND status IN ({placeholders})",
                [account_id] + list(statuses),
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT * FROM ml_listings WHERE account_id=?",
                [account_id],
            )).fetchall()
    return [dict(r) for r in rows]


async def get_ml_listings_all_accounts(statuses: list[str] | None = None) -> list[dict]:
    """Retorna todos los listings de todas las cuentas."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            rows = await (await db.execute(
                f"SELECT * FROM ml_listings WHERE status IN ({placeholders})",
                list(statuses),
            )).fetchall()
        else:
            rows = await (await db.execute("SELECT * FROM ml_listings")).fetchall()
    return [dict(r) for r in rows]


async def count_ml_listings_synced(account_id: str) -> int:
    """Retorna cuántos listings tiene la cuenta en DB (0 si nunca se ha sincronizado)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            "SELECT COUNT(*) FROM ml_listings WHERE account_id=? AND synced_at > 0",
            [account_id],
        )).fetchone()
    return row[0] if row else 0


async def get_ml_listings_for_gap_scan(account_id: str) -> tuple[set, dict, dict]:
    """Lee ml_listings DB para construir las mismas estructuras que _get_meli_sku_set.

    Retorna:
        (skus_set, inactive_map, active_prices_map)
        - skus_set: set de base_skus de todos los listings (todos los estados)
        - inactive_map: base_sku → [item_id, ...] para items inactive/paused/closed
        - active_prices_map: base_sku → [{item_id, price, title, pics, has_gtin, has_brand, quality_score}]

    Sustituye las llamadas a ML API en Phase 1 del gap scan, eliminando ~1000+ llamadas HTTP.
    Fallback: si la DB está vacía para la cuenta, la llamada original a _get_meli_sku_set sigue disponible.
    """
    import json as _json

    skus_set: set = set()
    inactive_map: dict = {}
    active_prices_map: dict = {}

    _REACTIVATABLE = {"inactive", "paused", "closed"}

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT item_id, status, price, title, base_sku, data_json "
            "FROM ml_listings WHERE account_id=? AND base_sku != ''",
            [account_id],
        )).fetchall()

    for r in rows:
        base = r["base_sku"]
        if not base:
            continue
        skus_set.add(base)
        status = (r["status"] or "").lower()
        iid = r["item_id"]

        if status in _REACTIVATABLE:
            inactive_map.setdefault(base, [])
            if iid not in inactive_map[base]:
                inactive_map[base].append(iid)

        elif status == "active":
            price = float(r["price"] or 0)
            if price > 0:
                title = r["title"] or ""
                pics, has_gtin, has_brand = 0, False, False
                try:
                    body = _json.loads(r["data_json"] or "{}")
                    pics = len(body.get("pictures") or [])
                    attrs = body.get("attributes") or []
                    has_gtin  = any(a.get("id") in ("GTIN", "EAN", "UPC") for a in attrs)
                    has_brand = any(a.get("id") == "BRAND" for a in attrs)
                except Exception:
                    pass
                title_score   = min(len(title), 60) / 60 * 25
                pics_score    = min(pics, 6) / 6 * 25
                attr_score    = (10 if has_brand else 0) + (15 if has_gtin else 0)
                quality_score = int(title_score + pics_score + attr_score + (25 if price > 0 else 0))
                active_prices_map.setdefault(base, [])
                if not any(e["item_id"] == iid for e in active_prices_map[base]):
                    active_prices_map[base].append({
                        "item_id": iid, "price": price, "title": title,
                        "pics": pics, "has_gtin": has_gtin, "has_brand": has_brand,
                        "quality_score": quality_score,
                    })

    return skus_set, inactive_map, active_prices_map


async def get_ml_listings_max_synced_at(account_id: str) -> float:
    """Retorna el timestamp del item más recientemente sincronizado para la cuenta."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            "SELECT MAX(synced_at) FROM ml_listings WHERE account_id=? AND data_json != ''",
            [account_id],
        )).fetchone()
    return float(row[0]) if row and row[0] else 0.0


async def bulk_update_ml_listing_qtys(updates: list[tuple[str, int]]) -> None:
    """Actualiza available_qty + data_json en batch tras un ciclo de stock sync.
    updates = [(item_id, new_qty), ...]. Usa 2 queries SQL sin importar el tamaño del lote."""
    if not updates:
        return
    import json as _json, time as _t
    ts = _t.time()
    qty_map = {item_id: new_qty for item_id, new_qty in updates}
    async with aiosqlite.connect(DATABASE_PATH) as db:
        placeholders = ",".join("?" * len(qty_map))
        rows = await (await db.execute(
            f"SELECT item_id, data_json FROM ml_listings WHERE item_id IN ({placeholders})",
            list(qty_map.keys()),
        )).fetchall()
        json_rows: list = []
        simple_rows: list = []
        for item_id, data_json in rows:
            new_qty = qty_map[item_id]
            if data_json:
                try:
                    data = _json.loads(data_json)
                    data["available_quantity"] = new_qty
                    json_rows.append((new_qty, _json.dumps(data, ensure_ascii=False), ts, item_id))
                    continue
                except Exception:
                    pass
            simple_rows.append((new_qty, ts, item_id))
        if json_rows:
            await db.executemany(
                "UPDATE ml_listings SET available_qty=?, data_json=?, synced_at=? WHERE item_id=?",
                json_rows,
            )
        if simple_rows:
            await db.executemany(
                "UPDATE ml_listings SET available_qty=?, synced_at=? WHERE item_id=?",
                simple_rows,
            )
        await db.commit()


async def update_ml_listing_qty(item_id: str, new_qty: int) -> None:
    """Actualiza available_qty y data_json tras sincronizar stock a ML.
    Evita que la DB sirva datos stale (0) cuando ML ya tiene el stock nuevo."""
    import json as _json, time as _time2
    async with aiosqlite.connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            "SELECT data_json FROM ml_listings WHERE item_id=?", [item_id]
        )).fetchone()
        if row and row[0]:
            try:
                data = _json.loads(row[0])
                data["available_quantity"] = new_qty
                new_json = _json.dumps(data, ensure_ascii=False)
                await db.execute(
                    "UPDATE ml_listings SET available_qty=?, data_json=?, synced_at=? WHERE item_id=?",
                    [new_qty, new_json, _time2.time(), item_id],
                )
            except Exception:
                await db.execute(
                    "UPDATE ml_listings SET available_qty=?, synced_at=? WHERE item_id=?",
                    [new_qty, _time2.time(), item_id],
                )
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON LISTINGS CACHE
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_amazon_listings(rows: list[dict]) -> None:
    """Inserta o actualiza listings Amazon en la tabla local."""
    if not rows:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO amazon_listings
               (seller_id, sku, base_sku, asin, title, status, price,
                available_qty, can_update, fulfillment, synced_at)
               VALUES (:seller_id,:sku,:base_sku,:asin,:title,:status,:price,
                       :available_qty,:can_update,:fulfillment,:synced_at)""",
            rows,
        )
        await db.commit()


async def count_amazon_listings(seller_id: str) -> int:
    """Retorna cuántos listings tiene la cuenta Amazon en DB."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            "SELECT COUNT(*) FROM amazon_listings WHERE seller_id=? AND synced_at > 0",
            [seller_id],
        )).fetchone()
    return row[0] if row else 0


async def get_listings_summary() -> dict:
    """Retorna conteo de listings por cuenta — ML + Amazon — para el card de Sync Stock."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ML: conteo + último sync por cuenta
        ml_counts: dict = {}
        for r in await (await db.execute(
            "SELECT account_id, COUNT(*) as cnt, MAX(synced_at) as last_ts "
            "FROM ml_listings GROUP BY account_id"
        )).fetchall():
            ml_counts[r["account_id"]] = {"count": r["cnt"], "last_ts": float(r["last_ts"] or 0)}

        # ML: nicknames
        tokens_rows = await (await db.execute("SELECT user_id, nickname FROM tokens")).fetchall()
        ml_accounts = []
        for t in tokens_rows:
            uid = t["user_id"]
            info = ml_counts.get(uid, {"count": 0, "last_ts": 0.0})
            ml_accounts.append({
                "account_id":  uid,
                "nickname":    t["nickname"] or uid,
                "platform":    "ml",
                "count":       info["count"],
                "last_sync_ts": info["last_ts"],
            })

        # Amazon: conteo + último sync por cuenta
        amz_counts: dict = {}
        try:
            for r in await (await db.execute(
                "SELECT seller_id, COUNT(*) as cnt, MAX(synced_at) as last_ts "
                "FROM amazon_listings GROUP BY seller_id"
            )).fetchall():
                amz_counts[r["seller_id"]] = {"count": r["cnt"], "last_ts": float(r["last_ts"] or 0)}
        except Exception:
            pass

        # Amazon: nicknames
        amz_rows = await (await db.execute(
            "SELECT seller_id, nickname FROM amazon_accounts"
        )).fetchall()
        amz_accounts = []
        for t in amz_rows:
            sid = t["seller_id"]
            info = amz_counts.get(sid, {"count": 0, "last_ts": 0.0})
            amz_accounts.append({
                "account_id":  sid,
                "nickname":    t["nickname"] or sid,
                "platform":    "amz",
                "count":       info["count"],
                "last_sync_ts": info["last_ts"],
            })

    all_accounts = ml_accounts + amz_accounts
    all_ts = [a["last_sync_ts"] for a in all_accounts if a["last_sync_ts"]]
    return {
        "accounts":       all_accounts,
        "last_sync_ts":   max(all_ts) if all_ts else 0,
        "total_listings": sum(a["count"] for a in all_accounts),
    }


# ─────────────────────────────────────────────────────────────────────────────
# BM STOCK CACHE — persiste el caché de BinManager entre reinicios del servidor
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_bm_stock_batch(entries: list[tuple]) -> None:
    """Persiste entradas de BM stock a DB. entries = [(sku, data_dict, synced_at), ...]"""
    if not entries:
        return
    rows = [{"sku": s.upper(), "data_json": json.dumps(d), "synced_at": t}
            for s, d, t in entries]
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO bm_stock_cache (sku, data_json, synced_at) "
            "VALUES (:sku, :data_json, :synced_at)",
            rows,
        )
        await db.commit()


async def load_bm_stock_cache(max_age_s: float = 1800.0) -> list[dict]:
    """Carga entradas de BM stock desde DB. Solo las que tienen menos de max_age_s segundos."""
    import time as _t
    min_ts = _t.time() - max_age_s
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT sku, data_json, synced_at FROM bm_stock_cache WHERE synced_at >= ?",
            [min_ts],
        )).fetchall()
    return [dict(r) for r in rows]


# ─── return_flags helpers ────────────────────────────────────────────────────

async def save_return_flag(user_id: str, item_id: str, flag_type: str, note: str = "") -> None:
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO return_flags (user_id, item_id, flag_type, note, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (user_id, item_id, flag_type, note, _t.time()),
        )
        await db.execute(
            """UPDATE return_flags SET flag_type=?, note=?, created_at=?, resolved=0
               WHERE user_id=? AND item_id=? AND resolved=0""",
            (flag_type, note, _t.time(), user_id, item_id),
        )
        await db.commit()


async def get_return_flags(user_id: str) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM return_flags WHERE user_id=? AND resolved=0 ORDER BY created_at DESC",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_flagged_item_ids(user_id: str) -> set:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT item_id FROM return_flags WHERE user_id=? AND resolved=0",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def resolve_return_flag(user_id: str, item_id: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE return_flags SET resolved=1 WHERE user_id=? AND item_id=?",
            (user_id, item_id)
        )
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO DE FACTURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

async def create_billing_request(
    token: str,
    ml_user_id: str,
    platform: str,
    order_number: str,
    client_ref: str,
    created_by: str,
    notes: str = "",
) -> int:
    """Crea una nueva solicitud de facturación. Retorna el id."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO billing_requests
               (token, ml_user_id, platform, order_number, client_ref, created_by, created_at, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (token, ml_user_id, platform, order_number, client_ref, created_by, now, notes),
        )
        await db.commit()
        return cursor.lastrowid


async def get_billing_request_by_token(token: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM billing_requests WHERE token=?", (token,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_billing_request_by_id(request_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM billing_requests WHERE id=?", (request_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_billing_requests(status: str = None) -> list:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cursor = await db.execute(
                "SELECT * FROM billing_requests WHERE status=? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM billing_requests ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_billing_status(request_id: int, status: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE billing_requests SET status=? WHERE id=?", (status, request_id)
        )
        await db.commit()


async def update_billing_order_data(request_id: int, order_data_json: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE billing_requests SET order_data=? WHERE id=?",
            (order_data_json, request_id),
        )
        await db.commit()


async def save_billing_fiscal_data(
    request_id: int,
    rfc: str,
    razon_social: str,
    cfdi_use: str,
    fiscal_regime: str,
    zip_code: str,
    forma_pago: str,
    email: str,
    phone: str,
    street: str,
    constancia_data: bytes = None,
    constancia_name: str = "",
) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO billing_fiscal_data
               (request_id, rfc, razon_social, cfdi_use, fiscal_regime, zip_code,
                forma_pago, email, phone, street, constancia_data, constancia_name, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_id) DO UPDATE SET
                 rfc=excluded.rfc, razon_social=excluded.razon_social,
                 cfdi_use=excluded.cfdi_use, fiscal_regime=excluded.fiscal_regime,
                 zip_code=excluded.zip_code, forma_pago=excluded.forma_pago,
                 email=excluded.email, phone=excluded.phone, street=excluded.street,
                 constancia_data=excluded.constancia_data,
                 constancia_name=excluded.constancia_name,
                 submitted_at=excluded.submitted_at""",
            (
                request_id, rfc, razon_social, cfdi_use, fiscal_regime, zip_code,
                forma_pago, email, phone, street, constancia_data, constancia_name, now,
            ),
        )
        await db.commit()


async def get_billing_fiscal_data(request_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM billing_fiscal_data WHERE request_id=?", (request_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d.pop("constancia_data", None)  # never return binary in JSON context
        return d


async def get_billing_constancia(request_id: int) -> Optional[tuple]:
    """Retorna (filename, bytes) o None."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT constancia_name, constancia_data FROM billing_fiscal_data WHERE request_id=?",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row and row["constancia_data"]:
            return (row["constancia_name"] or "constancia.pdf", bytes(row["constancia_data"]))
        return None


async def save_billing_invoice(
    request_id: int, filename: str, file_data: bytes, uploaded_by: str,
    xml_filename: str = "", xml_data: Optional[bytes] = None,
) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO billing_invoices
                 (request_id, filename, file_data, xml_filename, xml_data, uploaded_by, uploaded_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(request_id) DO UPDATE SET
                 filename=excluded.filename, file_data=excluded.file_data,
                 xml_filename=excluded.xml_filename, xml_data=excluded.xml_data,
                 uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at""",
            (request_id, filename, file_data, xml_filename or "", xml_data, uploaded_by, now),
        )
        await db.commit()


async def get_billing_invoice(request_id: int) -> Optional[dict]:
    """Retorna dict con pdf y xml, o None si no existe ninguno."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT filename, file_data, xml_filename, xml_data FROM billing_invoices WHERE request_id=?",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row and row["file_data"]:
            return {
                "pdf_filename": row["filename"] or "factura.pdf",
                "pdf_data":     bytes(row["file_data"]),
                "xml_filename": row["xml_filename"] or "",
                "xml_data":     bytes(row["xml_data"]) if row["xml_data"] else None,
            }
        return None


async def delete_billing_request(request_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM billing_fiscal_data WHERE request_id=?", (request_id,))
        await db.execute("DELETE FROM billing_invoices WHERE request_id=?", (request_id,))
        await db.execute("DELETE FROM billing_requests WHERE id=?", (request_id,))
        await db.commit()
