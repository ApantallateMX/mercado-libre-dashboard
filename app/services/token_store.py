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
        # TABLA: listings_count_prev — snapshot del count ANTES de cada sync
        # Permite calcular el delta (↑↓=) comparado con el sync anterior.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS listings_count_prev (
                platform    TEXT NOT NULL,
                account_id  TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                recorded_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (platform, account_id)
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: orphan_listings — listings presentes en DB pero eliminados
        # de la plataforma. Detectados en cada full sync.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orphan_listings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                platform    TEXT NOT NULL,
                account_id  TEXT NOT NULL,
                item_id     TEXT NOT NULL,
                title       TEXT DEFAULT '',
                sku         TEXT DEFAULT '',
                detected_at REAL NOT NULL DEFAULT 0,
                UNIQUE(platform, account_id, item_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orphan_listings_acct "
            "ON orphan_listings(platform, account_id)"
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
        # ─────────────────────────────────────────────────────────────────
        # TABLA: bm_sync_log — historial de ejecuciones del prewarm BM
        # Muestra en UI cuándo se actualizó el caché, cuántos SKUs, duración.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_sync_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at  REAL    NOT NULL DEFAULT 0,
                sku_count  INTEGER NOT NULL DEFAULT 0,
                elapsed_s  REAL    NOT NULL DEFAULT 0,
                source     TEXT    NOT NULL DEFAULT 'auto'
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: stock_issues_cache — persiste alertas/stock pre-computados
        # Sobrevive deploys de Railway: el Stock tab muestra datos inmediatos
        # en lugar de "Calculando..." mientras corre el prewarm en background.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_issues_cache (
                cache_key TEXT PRIMARY KEY,
                ts        REAL NOT NULL,
                data_json TEXT NOT NULL,
                saved_at  REAL NOT NULL DEFAULT 0
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
                metodo_pago      TEXT NOT NULL DEFAULT '',
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
        # Migration: add metodo_pago to billing_fiscal_data
        try:
            await db.execute("ALTER TABLE billing_fiscal_data ADD COLUMN metodo_pago TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_requests_token ON billing_requests(token)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_requests_status ON billing_requests(status)"
        )
        # Índice para filtros: (platform, order_number) — mejora queries de filtrado y anti-dup
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_requests_platform_order "
            "ON billing_requests(platform, order_number)"
        )
        # ─────────────────────────────────────────────────────────────────
        # TABLA: bm_product_catalog — info estática de SKUs desde BM
        # retail_ph, brand, model, title — actualizada 1x/semana (domingo 9pm MTY)
        # Sobrevive deploys, reinicios y resets de cache en memoria.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_product_catalog (
                sku        TEXT PRIMARY KEY,
                retail_ph  REAL NOT NULL DEFAULT 0,
                brand      TEXT NOT NULL DEFAULT '',
                model      TEXT NOT NULL DEFAULT '',
                title      TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS item_sync_log (
                item_id    TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                synced_qty INTEGER NOT NULL DEFAULT 0,
                synced_at  REAL NOT NULL,
                synced_by  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (item_id, user_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_item_sync_log_at
            ON item_sync_log (synced_at)
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: account_stock_rules — reglas de distribución por cuenta
        # pct_full   = % del stock BM cuando hay ≥ umbral unidades
        # pct_scarce = % del stock BM cuando hay < umbral (modo escasez)
        # scarce_enabled = si esta cuenta recibe stock en modo escasez
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS account_stock_rules (
                user_id        TEXT PRIMARY KEY,
                nickname       TEXT NOT NULL DEFAULT '',
                priority       INTEGER NOT NULL DEFAULT 99,
                pct_full       REAL NOT NULL DEFAULT 1.0,
                pct_scarce     REAL NOT NULL DEFAULT 1.0,
                scarce_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at     REAL NOT NULL DEFAULT 0
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: stock_distribution_settings — umbrales globales
        # scarce_threshold_units  = unidades mínimas para modo "normal"
        # scarce_threshold_days   = días de supply mínimos para modo "normal"
        # safety_buffer_units     = unidades nunca expuestas (siempre en BM)
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_distribution_settings (
                id                     INTEGER PRIMARY KEY CHECK (id = 1),
                scarce_threshold_units INTEGER NOT NULL DEFAULT 10,
                scarce_threshold_days  INTEGER NOT NULL DEFAULT 7,
                safety_buffer_units    INTEGER NOT NULL DEFAULT 2,
                updated_at             REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO stock_distribution_settings (id) VALUES (1)"
        )
        # ─────────────────────────────────────────────────────────────────
        # TABLA: account_deal_config — precios para deals por cuenta
        # deal_buffer_pct  = % que se añade al precio para absorber el descuento del deal
        # retail_target_pct = % del retail BM que se quiere recuperar tras el deal
        # Distintos por cuenta → competencia/ML no detecta que son el mismo vendedor
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS account_deal_config (
                user_id           TEXT PRIMARY KEY,
                deal_buffer_pct   REAL NOT NULL DEFAULT 0.15,
                retail_target_pct REAL NOT NULL DEFAULT 1.0,
                updated_at        REAL NOT NULL DEFAULT 0
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: order_history — historial de ventas por SKU / cuenta / plataforma
        # Crece automáticamente: cada vez que se fetchan órdenes se hace upsert.
        # data_source: 'estimated' = neto calculado con fórmula; 'real' = de /collections ML
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_history (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id         TEXT NOT NULL,
                account_id       TEXT NOT NULL,
                platform         TEXT NOT NULL DEFAULT 'ml',
                item_id          TEXT NOT NULL DEFAULT '',
                sku              TEXT NOT NULL DEFAULT '',
                unit_price       REAL NOT NULL DEFAULT 0,
                quantity         INTEGER NOT NULL DEFAULT 1,
                sale_fee         REAL NOT NULL DEFAULT 0,
                neto_plat        REAL NOT NULL DEFAULT 0,
                costo_usd        REAL NOT NULL DEFAULT 0,
                costo_mxn        REAL NOT NULL DEFAULT 0,
                retail_ph_usd    REAL NOT NULL DEFAULT 0,
                ganancia_neta    REAL NOT NULL DEFAULT 0,
                margen_pct       REAL NOT NULL DEFAULT 0,
                recup_retail_pct REAL NOT NULL DEFAULT 0,
                fx_rate          REAL NOT NULL DEFAULT 17.0,
                currency         TEXT NOT NULL DEFAULT 'MXN',
                order_date       TEXT NOT NULL DEFAULT '',
                order_month      TEXT NOT NULL DEFAULT '',
                status           TEXT NOT NULL DEFAULT '',
                data_source      TEXT NOT NULL DEFAULT 'estimated',
                created_at       REAL NOT NULL DEFAULT 0,
                UNIQUE(order_id, item_id, platform)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_oh_sku ON order_history(sku)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_oh_account ON order_history(account_id, platform)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_oh_month ON order_history(order_month)")
        # ─────────────────────────────────────────────────────────────────
        # TABLA: item_history — auditoría de cambios por listing
        # field: price | title | description | stock | status | shipping | pictures | attributes
        # old_value/new_value: TEXT (serializado) para cualquier tipo
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS item_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '',
                field      TEXT NOT NULL,
                old_value  TEXT NOT NULL DEFAULT '',
                new_value  TEXT NOT NULL DEFAULT '',
                changed_by TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ih_item ON item_history(item_id, created_at)"
        )
        # ─────────────────────────────────────────────────────────────────
        # TABLA: suggestions — notificaciones cruzadas entre cuentas
        # Propuestas de acción desde el análisis de competencia
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                from_account TEXT NOT NULL,
                to_account   TEXT NOT NULL,
                item_id      TEXT NOT NULL DEFAULT '',
                sku          TEXT NOT NULL DEFAULT '',
                item_title   TEXT NOT NULL DEFAULT '',
                action       TEXT NOT NULL,
                reason       TEXT NOT NULL DEFAULT '',
                created_at   REAL NOT NULL DEFAULT 0,
                status       TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_suggestions_to ON suggestions(to_account, status)"
        )
        # ─────────────────────────────────────────────────────────────────
        # TABLA: amz_sku_gaps — SKUs con stock BM sin lanzar en Amazon
        # Persiste status (unlaunched/launched/ignored) + ASIN capturado al crear
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_sku_gaps (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id        TEXT NOT NULL,
                sku              TEXT NOT NULL,
                asin             TEXT NOT NULL DEFAULT '',
                product_title    TEXT NOT NULL DEFAULT '',
                brand            TEXT NOT NULL DEFAULT '',
                image_url        TEXT NOT NULL DEFAULT '',
                avail_qty        INTEGER NOT NULL DEFAULT 0,
                cost_usd         REAL NOT NULL DEFAULT 0,
                cost_mxn         REAL NOT NULL DEFAULT 0,
                suggested_price  REAL NOT NULL DEFAULT 0,
                upc              TEXT NOT NULL DEFAULT '',
                status           TEXT NOT NULL DEFAULT 'unlaunched',
                launched_price   REAL NOT NULL DEFAULT 0,
                launched_at      TIMESTAMP DEFAULT NULL,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(seller_id, sku)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_amz_sku_gaps_seller ON amz_sku_gaps(seller_id, status)"
        )
        # TABLA: amz_gap_scan_status — estado del scan background por seller_id
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_gap_scan_status (
                seller_id    TEXT PRIMARY KEY,
                status       TEXT NOT NULL DEFAULT 'idle',
                started_at   TEXT DEFAULT NULL,
                finished_at  TEXT DEFAULT NULL,
                bm_total     INTEGER DEFAULT 0,
                amazon_active INTEGER DEFAULT 0,
                gaps_found   INTEGER DEFAULT 0,
                error        TEXT DEFAULT NULL
            )
        """)
        # Columnas adicionales en amz_sku_gaps (pueden ya existir — ignorar error)
        for _col_sql in [
            "ALTER TABLE amz_sku_gaps ADD COLUMN category TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE amz_sku_gaps ADD COLUMN model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE amz_sku_gaps ADD COLUMN margin_pct REAL DEFAULT NULL",
            "ALTER TABLE amz_sku_gaps ADD COLUMN last_scan TEXT DEFAULT NULL",
        ]:
            try:
                await db.execute(_col_sql)
            except Exception:
                pass  # columna ya existe
        # TABLA: amz_catalog_cache — SKUs confirmados en Amazon por seller_id
        # Evita re-verificar el mismo SKU en cada scan (TTL 24h)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_catalog_cache (
                seller_id    TEXT NOT NULL,
                sku_upper    TEXT NOT NULL,
                found        INTEGER NOT NULL DEFAULT 0,
                checked_at   TEXT NOT NULL,
                PRIMARY KEY (seller_id, sku_upper)
            )
        """)
        # TABLA: amz_product_specs_cache — specs investigadas por brand+model (TTL 30 dias)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_product_specs_cache (
                cache_key  TEXT PRIMARY KEY,
                specs_json TEXT NOT NULL DEFAULT '{}',
                cached_at  REAL NOT NULL DEFAULT 0
            )
        """)
        # TABLA: amz_listing_status_cache — estado post-publicacion por sku+seller
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_listing_status_cache (
                seller_id   TEXT NOT NULL,
                sku         TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                asin        TEXT DEFAULT NULL,
                issues_json TEXT DEFAULT '[]',
                checked_at  REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (seller_id, sku)
            )
        """)
        # TABLA: amz_product_type_schemas -- schema de atributos por tipo (TTL 30 dias)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_product_type_schemas (
                cache_key   TEXT PRIMARY KEY,
                schema_json TEXT NOT NULL DEFAULT '{}',
                cached_at   REAL NOT NULL DEFAULT 0
            )
        """)
        # TABLA: amz_product_type_templates — templates validados por tipo Amazon
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_product_type_templates (
                product_type   TEXT NOT NULL,
                marketplace_id TEXT NOT NULL DEFAULT 'ATVPDKIKX0DER',
                required_attrs TEXT NOT NULL DEFAULT '[]',
                quality_attrs  TEXT NOT NULL DEFAULT '[]',
                bonus_attrs    TEXT NOT NULL DEFAULT '[]',
                defaults_json  TEXT NOT NULL DEFAULT '{}',
                ai_hints       TEXT NOT NULL DEFAULT '',
                validated      INTEGER NOT NULL DEFAULT 0,
                validated_at   TEXT DEFAULT NULL,
                launch_count   INTEGER NOT NULL DEFAULT 0,
                updated_at     TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (product_type, marketplace_id)
            )
        """)
        # TABLA: sku_upc_map — UPC internos generados por SKU
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sku_upc_map (
                sku        TEXT PRIMARY KEY,
                upc        TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'generated',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate: add is_parent column to amazon_listings if missing
        try:
            await db.execute('ALTER TABLE amazon_listings ADD COLUMN is_parent INTEGER DEFAULT 0')
            await db.commit()
        except Exception:
            pass  # already exists
        # Migrate: add parent_asin column
        try:
            await db.execute('ALTER TABLE amazon_listings ADD COLUMN parent_asin TEXT DEFAULT ""')
            await db.commit()
        except Exception:
            pass  # already exists
        # Migrate: add field_defs_json to amz_product_type_templates
        try:
            await db.execute('ALTER TABLE amz_product_type_templates ADD COLUMN field_defs_json TEXT NOT NULL DEFAULT "[]"')
            await db.commit()
        except Exception:
            pass  # already exists
        # TABLA: amz_launched_listings — productos lanzados via wizard para monitoreo post-publicación
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_launched_listings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id   TEXT NOT NULL,
                sku         TEXT NOT NULL,
                asin        TEXT DEFAULT NULL,
                product_type TEXT DEFAULT NULL,
                title       TEXT DEFAULT NULL,
                price       REAL DEFAULT 0,
                currency    TEXT DEFAULT 'MXN',
                launched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                check_status TEXT DEFAULT 'pending',
                check_result TEXT DEFAULT NULL,
                checked_at  TEXT DEFAULT NULL,
                UNIQUE(seller_id, sku)
            )
        """)
        # TABLA: amz_listing_actions — historial de acciones cierre/eliminacion
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_listing_actions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id    TEXT NOT NULL,
                sku          TEXT NOT NULL,
                asin         TEXT DEFAULT '',
                action       TEXT NOT NULL,  -- close | delete | archive
                reason       TEXT DEFAULT '',
                performed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute('CREATE INDEX IF NOT EXISTS idx_amz_actions_seller ON amz_listing_actions(seller_id,performed_at)')
        # TABLA: amz_repricing_rules — reglas de repricing por seller/sku
        # TABLA: amz_product_types_cache — tipos de producto Amazon por marketplace (TTL 7 días)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_product_types_cache (
                marketplace_id TEXT PRIMARY KEY,
                types_json     TEXT NOT NULL DEFAULT '[]',
                cached_at      REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amz_repricing_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id TEXT NOT NULL,
                sku TEXT NOT NULL DEFAULT '*',
                rule_type TEXT NOT NULL DEFAULT 'match_buybox',
                beat_pct REAL NOT NULL DEFAULT 0.0,
                min_price REAL NOT NULL DEFAULT 0.0,
                max_price REAL NOT NULL DEFAULT 0.0,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(seller_id, sku)
            )
        """)
        await db.commit()


async def save_item_sync(item_id: str, user_id: str, synced_qty: int, synced_by: str = "") -> None:
    """Registra que un item fue sincronizado ahora.
    Cross-user: cualquier cuenta que consulte get_recently_synced_ids verá este registro.
    TTL de supresión: 60 min — tiempo suficiente para que ML confirme el qty nuevo.
    """
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO item_sync_log (item_id, user_id, synced_qty, synced_at, synced_by)
               VALUES (?, ?, ?, ?, ?)""",
            (item_id, user_id, synced_qty, _t.time(), synced_by),
        )
        # Limpiar registros > 2 horas para no crecer indefinidamente
        await db.execute("DELETE FROM item_sync_log WHERE synced_at < ?", (_t.time() - 7200,))
        await db.commit()


async def get_recently_synced_ids(user_id: str, ttl_seconds: int = 3600) -> set[str]:
    """Retorna item_ids sincronizados en los últimos ttl_seconds — GLOBAL, sin filtro de cuenta.
    Si cualquier usuario sincronizó un item, se suprime de las alertas de TODAS las cuentas
    para evitar acciones duplicadas entre usuarios. El item reaparece al siguiente ciclo BM.
    """
    import time as _t
    cutoff = _t.time() - ttl_seconds
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await (await db.execute(
            "SELECT item_id FROM item_sync_log WHERE synced_at > ?",
            (cutoff,),
        )).fetchall()
    return {r[0] for r in rows}


async def upsert_bm_catalog_batch(rows: list[dict]) -> int:
    """Guarda info de producto BM en bm_product_catalog.
    rows: list of {sku, retail_ph, brand, model, title}
    Retorna cantidad de rows insertadas/actualizadas.
    """
    if not rows:
        return 0
    now = __import__("time").time()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO bm_product_catalog
               (sku, retail_ph, brand, model, title, updated_at)
               VALUES (:sku, :retail_ph, :brand, :model, :title, :updated_at)""",
            [{**r, "updated_at": now} for r in rows],
        )
        await db.commit()
    return len(rows)


async def get_bm_catalog_all() -> list[dict]:
    """Lee toda la tabla bm_product_catalog. Usado al arrancar para popular cache en memoria."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT sku, retail_ph, brand, model, title, updated_at FROM bm_product_catalog"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_bm_catalog_last_sync() -> float:
    """Retorna el timestamp de la última sincronización del catálogo, o 0 si nunca."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT MAX(updated_at) FROM bm_product_catalog"
        ) as cur:
            row = await cur.fetchone()
    val = row[0] if row else None
    return float(val) if val else 0.0


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


async def get_skus_from_listings(item_ids: list) -> dict:
    """Retorna {item_id: sku} consultando ml_listings para los item_ids dados.
    Fallback para items que no están en item_sku_cache pero sí en ml_listings."""
    if not item_ids:
        return {}
    result: dict[str, str] = {}
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        for i in range(0, len(item_ids), 500):
            chunk = item_ids[i:i+500]
            placeholders = ",".join("?" * len(chunk))
            cursor = await db.execute(
                f"SELECT item_id, sku FROM ml_listings WHERE item_id IN ({placeholders}) AND sku != '' AND sku IS NOT NULL",
                chunk,
            )
            for row in await cursor.fetchall():
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


async def get_amazon_velocity_from_db(days: int) -> dict:
    """Consulta order_history para velocidad Amazon — fuente primaria rápida en planeación.
    Retorna {SKU_UPPER: {units, units_7d, revenue, accounts}} sin llamar SP-API.
    """
    from datetime import datetime, timedelta
    date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_7d   = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT
                sku,
                GROUP_CONCAT(DISTINCT account_id) AS accounts_csv,
                SUM(quantity)                      AS units,
                SUM(unit_price * quantity)         AS revenue,
                SUM(CASE WHEN order_date >= ? THEN quantity ELSE 0 END) AS units_7d
            FROM order_history
            WHERE platform = 'amazon'
              AND order_date >= ?
              AND sku != ''
              AND LOWER(status) NOT IN ('cancelled', 'pending')
            GROUP BY sku
        """, (date_7d, date_from))
        rows = await cursor.fetchall()
    result = {}
    for row in rows:
        sku = (row["sku"] or "").upper().strip()
        if not sku:
            continue
        accounts = [a.strip() for a in (row["accounts_csv"] or "").split(",") if a.strip()]
        result[sku] = {
            "units":    int(row["units"] or 0),
            "units_7d": int(row["units_7d"] or 0),
            "revenue":  float(row["revenue"] or 0),
            "accounts": accounts,
        }
    return result


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


async def upsert_amazon_listings_report(rows: list[dict]) -> None:
    """Upsert de listings Amazon desde Reports API.
    Preserva price y available_qty existentes cuando los nuevos valores son 0
    (Reports API no siempre incluye precio/qty actualizados para FBA).
    """
    if not rows:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany(
            """INSERT INTO amazon_listings
               (seller_id, sku, base_sku, asin, title, status, price,
                available_qty, can_update, fulfillment, synced_at)
               VALUES (:seller_id,:sku,:base_sku,:asin,:title,:status,:price,
                       :available_qty,:can_update,:fulfillment,:synced_at)
               ON CONFLICT(seller_id, sku) DO UPDATE SET
                   base_sku   = excluded.base_sku,
                   asin       = CASE WHEN excluded.asin != '' THEN excluded.asin ELSE amazon_listings.asin END,
                   title      = CASE WHEN excluded.title != '' THEN excluded.title ELSE amazon_listings.title END,
                   status     = excluded.status,
                   price      = CASE WHEN excluded.price > 0 THEN excluded.price ELSE amazon_listings.price END,
                   available_qty = CASE WHEN excluded.available_qty > 0 THEN excluded.available_qty ELSE amazon_listings.available_qty END,
                   can_update = excluded.can_update,
                   fulfillment = excluded.fulfillment,
                   synced_at  = excluded.synced_at""",
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


async def update_ml_qty_batch(updates: list[tuple[str, int]]) -> int:
    """Actualiza available_qty en lote para items ML conocidos.
    updates = [(item_id, new_qty), ...]
    Solo actualiza filas cuyo qty realmente cambió. Retorna nº de filas cambiadas.
    """
    if not updates:
        return 0
    import time as _t
    ts = _t.time()
    changed = 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for item_id, new_qty in updates:
            cur = await db.execute(
                "UPDATE ml_listings SET available_qty=?, synced_at=? "
                "WHERE item_id=? AND available_qty!=?",
                (new_qty, ts, item_id, new_qty),
            )
            changed += cur.rowcount
        await db.commit()
    return changed


async def get_amazon_listings_for_account(seller_id: str) -> list[dict]:
    """Retorna [{sku, title, asin, available_qty}] para una cuenta Amazon."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT sku, title, asin, available_qty FROM amazon_listings WHERE seller_id=?",
            [seller_id],
        )).fetchall()
    return [dict(r) for r in rows]


async def get_amazon_skus_and_qtys(seller_id: str) -> list[tuple[str, int]]:
    """Retorna [(sku, available_qty), ...] para el qty-only sync de Amazon."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await (await db.execute(
            "SELECT sku, available_qty FROM amazon_listings WHERE seller_id=?",
            [seller_id],
        )).fetchall()
    return [(r[0], r[1]) for r in rows]


async def update_amazon_qty_batch(updates: list[tuple[str, str, int]]) -> int:
    """Actualiza available_qty en lote para listings Amazon.
    updates = [(seller_id, sku, new_qty), ...]
    Solo actualiza filas cuyo qty realmente cambió. Retorna nº de filas cambiadas.
    """
    if not updates:
        return 0
    import time as _t
    ts = _t.time()
    changed = 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for seller_id, sku, new_qty in updates:
            cur = await db.execute(
                "UPDATE amazon_listings SET available_qty=?, synced_at=? "
                "WHERE seller_id=? AND sku=? AND available_qty!=?",
                (new_qty, ts, seller_id, sku, new_qty),
            )
            changed += cur.rowcount
        await db.commit()
    return changed


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

        # Prev counts para calcular delta (↑↓=)
        prev_rows = await (await db.execute(
            "SELECT platform, account_id, count FROM listings_count_prev"
        )).fetchall()
        prev_map = {(r["platform"], r["account_id"]): r["count"] for r in prev_rows}

        # ML: nicknames
        tokens_rows = await (await db.execute("SELECT user_id, nickname FROM tokens")).fetchall()
        ml_accounts = []
        for t in tokens_rows:
            uid = t["user_id"]
            info = ml_counts.get(uid, {"count": 0, "last_ts": 0.0})
            prev = prev_map.get(("ml", uid))
            ml_accounts.append({
                "account_id":  uid,
                "nickname":    t["nickname"] or uid,
                "platform":    "ml",
                "count":       info["count"],
                "prev_count":  prev,
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
            prev = prev_map.get(("amz", sid))
            amz_accounts.append({
                "account_id":  sid,
                "nickname":    t["nickname"] or sid,
                "platform":    "amz",
                "count":       info["count"],
                "prev_count":  prev,
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


async def delete_bm_stock_skus(skus: list[str]) -> int:
    """Elimina SKUs de bm_stock_cache en DB. Retorna cuántos se borraron."""
    if not skus:
        return 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        deleted = 0
        for s in skus:
            cur = await db.execute("DELETE FROM bm_stock_cache WHERE sku = ?", [s.upper()])
            deleted += cur.rowcount
        await db.commit()
    return deleted


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


# ─── listings_count_prev helpers ─────────────────────────────────────────────

async def snapshot_listings_count(platform: str, account_id: str, count: int) -> None:
    """Guarda el count actual como 'prev' ANTES de que corra el sync.
    Llamar desde run_ml_listing_sync / run_amazon_listing_sync antes del upsert."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO listings_count_prev (platform, account_id, count, recorded_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(platform, account_id) DO UPDATE SET "
            "count=excluded.count, recorded_at=excluded.recorded_at",
            (platform, account_id, count, _t.time()),
        )
        await db.commit()


async def get_listings_count_prevs() -> dict:
    """Retorna {(platform, account_id): prev_count} para todos los registros."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await (await db.execute(
            "SELECT platform, account_id, count FROM listings_count_prev"
        )).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


# ─── orphan_listings helpers ─────────────────────────────────────────────────

async def save_orphan_listings(entries: list[dict]) -> int:
    """Inserta o actualiza listings huérfanos. entries: [{platform,account_id,item_id,title,sku}]
    Retorna el número de filas insertadas/actualizadas."""
    if not entries:
        return 0
    import time as _t
    now = _t.time()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany(
            "INSERT INTO orphan_listings (platform, account_id, item_id, title, sku, detected_at) "
            "VALUES (:platform, :account_id, :item_id, :title, :sku, :detected_at) "
            "ON CONFLICT(platform, account_id, item_id) DO UPDATE SET "
            "title=excluded.title, sku=excluded.sku, detected_at=excluded.detected_at",
            [{**e, "detected_at": now} for e in entries],
        )
        # Limpiar huérfanos que ya no existen (se re-detectan en cada full sync)
        # — si ya no está en la lista fresca del mismo account, lo dejamos, se limpia al confirmar
        await db.commit()
    return len(entries)


async def clear_orphans_for_account(platform: str, account_id: str) -> None:
    """Limpia los huérfanos detectados previamente para una cuenta (antes de re-detectar)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM orphan_listings WHERE platform=? AND account_id=?",
            (platform, account_id),
        )
        await db.commit()


async def get_orphan_listings(platform: str = None, account_id: str = None) -> list[dict]:
    """Retorna listings huérfanos. Filtra por platform y/o account_id si se especifican."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        where, params = [], []
        if platform:
            where.append("platform=?"); params.append(platform)
        if account_id:
            where.append("account_id=?"); params.append(account_id)
        sql = "SELECT * FROM orphan_listings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY detected_at DESC"
        rows = await (await db.execute(sql, params)).fetchall()
    return [dict(r) for r in rows]


async def delete_orphan_listings(ids: list[int]) -> int:
    """Elimina de DB local los listings huérfanos Y los registros en ml_listings/amazon_listings."""
    if not ids:
        return 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Leer las filas antes de borrar para saber qué limpiar en ml_listings/amazon_listings
        placeholders = ",".join("?" for _ in ids)
        rows = await (await db.execute(
            f"SELECT platform, account_id, item_id FROM orphan_listings WHERE id IN ({placeholders})",
            ids,
        )).fetchall()
        # Borrar de orphan_listings
        await db.execute(
            f"DELETE FROM orphan_listings WHERE id IN ({placeholders})", ids
        )
        # Borrar de ml_listings / amazon_listings
        for r in rows:
            if r["platform"] == "ml":
                await db.execute(
                    "DELETE FROM ml_listings WHERE item_id=? AND account_id=?",
                    (r["item_id"], r["account_id"]),
                )
            else:
                await db.execute(
                    "DELETE FROM amazon_listings WHERE seller_id=? AND sku=?",
                    (r["account_id"], r["item_id"]),
                )
        await db.commit()
    return len(rows)


# ─── bm_sync_log helpers ────────────────────────────────────────────────────

async def log_bm_sync_event(sku_count: int, elapsed_s: float, source: str = "auto") -> None:
    """Registra una ejecución del prewarm BM en el historial.
    Mantiene solo los últimos 50 registros para no crecer indefinidamente.
    """
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO bm_sync_log (synced_at, sku_count, elapsed_s, source) VALUES (?, ?, ?, ?)",
            (_t.time(), sku_count, round(elapsed_s, 1), source),
        )
        # Limpiar entradas viejas — conservar solo los 50 más recientes
        await db.execute(
            "DELETE FROM bm_sync_log WHERE id NOT IN "
            "(SELECT id FROM bm_sync_log ORDER BY id DESC LIMIT 50)"
        )
        await db.commit()


async def get_bm_sync_log(limit: int = 10) -> list[dict]:
    """Retorna los últimos `limit` eventos del historial BM, del más reciente al más antiguo."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, synced_at, sku_count, elapsed_s, source "
            "FROM bm_sync_log ORDER BY id DESC LIMIT ?",
            [limit],
        )).fetchall()
    return [dict(r) for r in rows]


# ─── stock_issues_cache helpers ─────────────────────────────────────────────

async def save_stock_issues_snapshot(key: str, ts: float, data: dict) -> None:
    """Persiste un resultado de prewarm (alertas + stock) en SQLite.
    Sobrevive deploys de Railway: el Stock tab muestra datos sin esperar el prewarm.
    """
    import json as _json, time as _t
    try:
        data_str = _json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return  # no persistir si no es serializable
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO stock_issues_cache (cache_key, ts, data_json, saved_at)
               VALUES (?, ?, ?, ?)""",
            (key, ts, data_str, _t.time()),
        )
        await db.commit()


async def load_all_stock_issues_snapshots() -> dict:
    """Carga todos los snapshots de stock_issues_cache desde DB.
    Retorna dict[cache_key, (ts, data)] — mismo formato que _stock_issues_cache en memoria.
    """
    import json as _json
    result: dict = {}
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT cache_key, ts, data_json FROM stock_issues_cache"
        )).fetchall()
    for r in rows:
        try:
            data = _json.loads(r["data_json"])
            result[r["cache_key"]] = (float(r["ts"]), data)
        except Exception:
            pass
    return result


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


async def list_billing_requests(
    status: str = None,
    platform: str = None,
    ml_user_id: str = None,
    created_by: str = None,
    sort: str = "date_desc",
) -> list:
    conditions, params = [], []
    if status:
        conditions.append("status=?"); params.append(status)
    if platform:
        conditions.append("platform=?"); params.append(platform)
    if ml_user_id:
        conditions.append("ml_user_id=?"); params.append(ml_user_id)
    if created_by:
        conditions.append("created_by=?"); params.append(created_by)
    where     = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order_dir = "ASC" if sort == "date_asc" else "DESC"
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT * FROM billing_requests {where} ORDER BY created_at {order_dir}",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_billing_request_by_order(platform: str, order_number: str) -> Optional[dict]:
    """Retorna la solicitud más reciente para (platform, order_number) o None.
    Usado para detectar duplicados antes de crear una nueva solicitud."""
    if not order_number or not order_number.strip():
        return None
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM billing_requests WHERE platform=? AND order_number=? ORDER BY id DESC LIMIT 1",
            (platform, order_number.strip()),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


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
    metodo_pago: str = "",
) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO billing_fiscal_data
               (request_id, rfc, razon_social, cfdi_use, fiscal_regime, zip_code,
                forma_pago, metodo_pago, email, phone, street, constancia_data, constancia_name, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_id) DO UPDATE SET
                 rfc=excluded.rfc, razon_social=excluded.razon_social,
                 cfdi_use=excluded.cfdi_use, fiscal_regime=excluded.fiscal_regime,
                 zip_code=excluded.zip_code, forma_pago=excluded.forma_pago,
                 metodo_pago=excluded.metodo_pago,
                 email=excluded.email, phone=excluded.phone, street=excluded.street,
                 constancia_data=excluded.constancia_data,
                 constancia_name=excluded.constancia_name,
                 submitted_at=excluded.submitted_at""",
            (
                request_id, rfc, razon_social, cfdi_use, fiscal_regime, zip_code,
                forma_pago, metodo_pago, email, phone, street, constancia_data, constancia_name, now,
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


# ══════════════════════════════════════════════════════════════════
# Distribución de stock multi-cuenta
# ══════════════════════════════════════════════════════════════════

async def get_distribution_rule(user_id: str) -> dict | None:
    """Retorna la regla de distribución para una cuenta, o None si no tiene."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM account_stock_rules WHERE user_id = ?", (user_id,)
        )).fetchone()
    return dict(row) if row else None


async def get_all_distribution_rules() -> list[dict]:
    """Retorna todas las reglas de distribución, ordenadas por prioridad."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM account_stock_rules ORDER BY priority ASC, nickname ASC"
        )).fetchall()
    return [dict(r) for r in rows]


async def upsert_distribution_rule(
    user_id: str, nickname: str, priority: int,
    pct_full: float, pct_scarce: float, scarce_enabled: bool,
) -> None:
    """Crea o actualiza la regla de distribución de una cuenta."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO account_stock_rules
               (user_id, nickname, priority, pct_full, pct_scarce, scarce_enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, nickname, priority, pct_full, pct_scarce, int(scarce_enabled), _t.time()),
        )
        await db.commit()


async def get_distribution_settings() -> dict:
    """Retorna los umbrales globales de distribución."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM stock_distribution_settings WHERE id = 1"
        )).fetchone()
    if row:
        return dict(row)
    return {"scarce_threshold_units": 10, "scarce_threshold_days": 7, "safety_buffer_units": 2}


async def upsert_distribution_settings(
    scarce_threshold_units: int, scarce_threshold_days: int, safety_buffer_units: int,
) -> None:
    """Actualiza los umbrales globales de distribución."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO stock_distribution_settings
               (id, scarce_threshold_units, scarce_threshold_days, safety_buffer_units, updated_at)
               VALUES (1, ?, ?, ?, ?)""",
            (scarce_threshold_units, scarce_threshold_days, safety_buffer_units, _t.time()),
        )
        await db.commit()


async def get_account_sold_history(user_id: str) -> dict:
    """Retorna {base_sku: sold_qty} para todos los SKUs con ventas históricas en esta cuenta.
    Usado para la excepción histórica: cuentas sin scarce_enabled pero con historial de ventas
    siguen recibiendo stock en modo escasez.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await (await db.execute(
            """SELECT base_sku, SUM(sold_qty) as total
               FROM ml_listings
               WHERE account_id = ? AND sold_qty > 0 AND base_sku != ''
               GROUP BY base_sku""",
            (user_id,),
        )).fetchall()
    return {r[0]: r[1] for r in rows}


async def get_deal_config(user_id: str) -> dict:
    """Retorna la config de precios deal para una cuenta. Defaults: 15% buffer, 100% retail."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM account_deal_config WHERE user_id = ?", (user_id,)
        )).fetchone()
    if row:
        return dict(row)
    return {"user_id": user_id, "deal_buffer_pct": 0.15, "retail_target_pct": 1.0}


async def set_deal_config(user_id: str, deal_buffer_pct: float, retail_target_pct: float) -> None:
    """Guarda o actualiza la config de precios deal para una cuenta."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO account_deal_config (user_id, deal_buffer_pct, retail_target_pct, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   deal_buffer_pct = excluded.deal_buffer_pct,
                   retail_target_pct = excluded.retail_target_pct,
                   updated_at = excluded.updated_at""",
            (user_id, deal_buffer_pct, retail_target_pct, _t.time()),
        )
        await db.commit()


async def upsert_order_history(rows: list[dict]) -> int:
    """Guarda/actualiza historial de ventas. ON CONFLICT actualiza con el dato más preciso.
    data_source='real' prevalece sobre 'estimated'; sale_fee y neto_plat toman el mayor valor.
    """
    import time as _t
    if not rows:
        return 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for r in rows:
            await db.execute("""
                INSERT INTO order_history
                    (order_id, account_id, platform, item_id, sku,
                     unit_price, quantity, sale_fee, neto_plat,
                     costo_usd, costo_mxn, retail_ph_usd,
                     ganancia_neta, margen_pct, recup_retail_pct,
                     fx_rate, currency, order_date, order_month,
                     status, data_source, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(order_id, item_id, platform) DO UPDATE SET
                    unit_price       = excluded.unit_price,
                    sale_fee         = CASE WHEN excluded.data_source = 'real' THEN excluded.sale_fee ELSE MAX(order_history.sale_fee, excluded.sale_fee) END,
                    neto_plat        = CASE WHEN excluded.data_source = 'real' THEN excluded.neto_plat ELSE MAX(order_history.neto_plat, excluded.neto_plat) END,
                    costo_usd        = CASE WHEN excluded.costo_usd > 0 THEN excluded.costo_usd ELSE order_history.costo_usd END,
                    costo_mxn        = CASE WHEN excluded.costo_mxn > 0 THEN excluded.costo_mxn ELSE order_history.costo_mxn END,
                    retail_ph_usd    = CASE WHEN excluded.retail_ph_usd > 0 THEN excluded.retail_ph_usd ELSE order_history.retail_ph_usd END,
                    ganancia_neta    = excluded.ganancia_neta,
                    margen_pct       = excluded.margen_pct,
                    recup_retail_pct = excluded.recup_retail_pct,
                    status           = excluded.status,
                    data_source      = CASE WHEN excluded.data_source = 'real' THEN 'real' ELSE order_history.data_source END
            """, (
                r.get("order_id", ""), r.get("account_id", ""), r.get("platform", "ml"),
                r.get("item_id", ""), r.get("sku", ""),
                r.get("unit_price", 0), r.get("quantity", 1), r.get("sale_fee", 0),
                r.get("neto_plat", 0), r.get("costo_usd", 0), r.get("costo_mxn", 0),
                r.get("retail_ph_usd", 0), r.get("ganancia_neta", 0),
                r.get("margen_pct", 0), r.get("recup_retail_pct", 0),
                r.get("fx_rate", 17.0), r.get("currency", "MXN"),
                r.get("order_date", ""), r.get("order_month", ""),
                r.get("status", ""), r.get("data_source", "estimated"),
                _t.time(),
            ))
        await db.commit()
    return len(rows)


async def get_sku_price_history(
    sku: str,
    platform: str = None,
    account_id: str = None,
    months: int = None,
    limit: int = 500,
) -> list[dict]:
    """Retorna historial de ventas para un SKU (búsqueda exacta o parcial).
    Ordenado por fecha descendente. Filtra por plataforma/cuenta/meses si se pasan.
    """
    import time as _t
    conditions = ["(sku = ? OR sku LIKE ?)"]
    params: list = [sku.upper(), f"%{sku.upper()}%"]
    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    if account_id:
        conditions.append("account_id = ?")
        params.append(account_id)
    if months and months > 0:
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
        conditions.append("order_date >= ?")
        params.append(cutoff)
    params.append(limit)
    where = " AND ".join(conditions)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"SELECT * FROM order_history WHERE {where} ORDER BY order_date DESC LIMIT ?",
            params,
        )).fetchall()
    return [dict(r) for r in rows]


async def get_sku_history_summary(sku: str, platform: str = None) -> dict:
    """Stats agregados del historial: % retail recuperado (con 7% comisión), neto neto, precio."""
    conditions = ["(sku = ? OR sku LIKE ?)"]
    params: list = [sku.upper(), f"%{sku.upper()}%"]
    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    where = " AND ".join(conditions)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(f"""
            SELECT
                COUNT(*)                                          AS total_orders,
                SUM(quantity)                                     AS total_units,
                AVG(unit_price)                                   AS avg_price,
                MIN(unit_price)                                   AS min_price,
                MAX(unit_price)                                   AS max_price,
                AVG(neto_plat * 0.93)                            AS avg_neto_neto,
                MIN(neto_plat * 0.93)                            AS min_neto_neto,
                MAX(neto_plat * 0.93)                            AS max_neto_neto,
                AVG(CASE WHEN retail_ph_usd > 0 AND fx_rate > 0
                    THEN (neto_plat * 0.93) / (retail_ph_usd * fx_rate) * 100
                    ELSE NULL END)                                AS avg_recup_neto,
                MIN(CASE WHEN retail_ph_usd > 0 AND fx_rate > 0
                    THEN (neto_plat * 0.93) / (retail_ph_usd * fx_rate) * 100
                    ELSE NULL END)                                AS min_recup_neto,
                MAX(CASE WHEN retail_ph_usd > 0 AND fx_rate > 0
                    THEN (neto_plat * 0.93) / (retail_ph_usd * fx_rate) * 100
                    ELSE NULL END)                                AS max_recup_neto,
                MIN(order_date)                                   AS first_sale,
                MAX(order_date)                                   AS last_sale
            FROM order_history WHERE {where}
        """, params)).fetchone()
    return dict(row) if row else {}


async def get_sku_sales_by_account(base_sku: str) -> list[dict]:
    """Retorna ventas por cuenta para un SKU base.
    Usado en el score dinámico: {user_id, nickname, sold_qty, available_qty}.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT m.account_id as user_id,
                      COALESCE(t.nickname, m.account_id) as nickname,
                      SUM(m.sold_qty) as sold_qty,
                      SUM(m.available_qty) as available_qty,
                      COUNT(*) as listing_count
               FROM ml_listings m
               LEFT JOIN tokens t ON t.user_id = m.account_id
               WHERE m.base_sku = ? AND m.status = 'active'
               GROUP BY m.account_id
               ORDER BY sold_qty DESC""",
            (base_sku,),
        )).fetchall()
    return [dict(r) for r in rows]


# ── Amazon Product Types Cache ────────────────────────────────────────────────

async def get_product_types_cache(marketplace_id: str) -> tuple:
    """Returns (types_list, cached_at_timestamp). Empty list + 0.0 if not cached."""
    import json as _j
    async with aiosqlite.connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            "SELECT types_json, cached_at FROM amz_product_types_cache WHERE marketplace_id = ?",
            (marketplace_id,),
        )).fetchone()
    if not row:
        return [], 0.0
    try:
        return _j.loads(row[0]), float(row[1])
    except Exception:
        return [], 0.0


async def save_product_types_cache(marketplace_id: str, types: list) -> None:
    """Saves product types list to DB cache."""
    import time as _t, json as _j
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO amz_product_types_cache (marketplace_id, types_json, cached_at) VALUES (?, ?, ?)",
            (marketplace_id, _j.dumps(sorted(types)), _t.time()),
        )
        await db.commit()


async def get_product_specs_cache(cache_key: str) -> tuple:
    """Returns (specs_dict, cached_at). Empty dict + 0.0 if not cached."""
    import json as _j
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            'SELECT specs_json, cached_at FROM amz_product_specs_cache WHERE cache_key = ?',
            (cache_key,),
        )).fetchone()
    if not row:
        return {}, 0.0
    try:
        return _j.loads(row[0]), float(row[1])
    except Exception:
        return {}, 0.0


async def save_product_specs_cache(cache_key: str, specs: dict) -> None:
    import time as _t, json as _j
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO amz_product_specs_cache (cache_key, specs_json, cached_at) VALUES (?, ?, ?)',
            (cache_key, _j.dumps(specs), _t.time()),
        )
        await db.commit()


async def save_listing_status(seller_id: str, sku: str, status: str, asin: str = None, issues: list = None) -> None:
    import time as _t, json as _j
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO amz_listing_status_cache (seller_id, sku, status, asin, issues_json, checked_at) VALUES (?, ?, ?, ?, ?, ?)',
            (seller_id, sku, status, asin, _j.dumps(issues or []), _t.time()),
        )
        await db.commit()


async def get_listing_status(seller_id: str, sku: str) -> dict:
    import json as _j
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            'SELECT status, asin, issues_json, checked_at FROM amz_listing_status_cache WHERE seller_id = ? AND sku = ?',
            (seller_id, sku),
        )).fetchone()
    if not row:
        return {}
    return {
        'status': row[0], 'asin': row[1],
        'issues': _j.loads(row[2] or '[]'), 'checked_at': row[3],
    }


# -- Amazon Product Type Schema Cache -----------------------------------------

async def get_schema_cache(cache_key: str) -> tuple:
    import json as _j
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            'SELECT schema_json, cached_at FROM amz_product_type_schemas WHERE cache_key = ?',
            (cache_key,),
        )).fetchone()
    if not row:
        return {}, 0.0
    try:
        return _j.loads(row[0]), float(row[1])
    except Exception:
        return {}, 0.0


async def save_schema_cache(cache_key: str, schema: dict) -> None:
    import time as _t, json as _j
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO amz_product_type_schemas (cache_key, schema_json, cached_at) VALUES (?, ?, ?)',
            (cache_key, _j.dumps(schema), _t.time()),
        )
        await db.commit()


# == Amazon Product Type Templates ============================================

_SEED_TEMPLATES = {
    ("TELEVISION", "ATVPDKIKX0DER"): {
        "validated": 1, "validated_at": "2026-05-28", "launch_count": 10,
        "required_attrs": ["item_name","brand","condition_type","purchasable_offer",
            "bullet_point","product_description","generic_keyword","country_of_origin",
            "supplier_declared_dg_hz_regulation","item_type_keyword"],
        "quality_attrs": ["display","resolution","refresh_rate","image_aspect_ratio",
            "total_hdmi_ports","mounting_type","item_weight","item_dimensions",
            "item_length_width_height","special_feature","included_components",
            "connectivity_technology","warranty_description","model_year","list_price",
            "model_number","model_name","color"],
        "bonus_attrs": ["item_package_weight","item_package_dimensions","voltage","wattage"],
        "defaults": {
            "item_type_keyword": "televisions", "display_type": "LED",
            "supplier_declared_dg_hz_regulation": "not_applicable",
            "supplier_declared_has_product_identifier_exemption": True,
            "batteries_required": False, "batteries_included": False,
            "number_of_items": 1, "warranty_description": "90 days seller warranty",
            "total_hdmi_ports": 2, "image_aspect_ratio": "16:9"
        },
        "ai_hints": "TELEVISION: item_type_keyword=televisions. special_feature enum: Smart TV, Built-In WiFi, HDR, Dolby Vision, 4K, QLED, OLED. display.type: LED/QLED/OLED/Mini LED/LCD/QNED. resolution: 720p/1080p/4K/8K.",
        "field_defs": [
            {"key": "display_type", "label": "Tipo de pantalla", "type": "select", "required": True,
             "options": ["LED", "QLED", "OLED", "Mini LED", "QNED", "LCD"], "default": "LED"},
            {"key": "resolution", "label": "Resolución", "type": "select", "required": True,
             "options": ["4K", "1080p", "8K", "720p"], "default": "4K"},
            {"key": "display_size_in", "label": "Tamaño pantalla (pulg)", "type": "number", "required": True, "default": 0},
            {"key": "refresh_rate_hz", "label": "Refresco (Hz)", "type": "number", "required": False, "default": 60},
            {"key": "total_hdmi_ports", "label": "Puertos HDMI", "type": "number", "required": True, "default": 2},
            {"key": "usb_port_count", "label": "Puertos USB", "type": "number", "required": False, "default": 1},
            {"key": "model_year", "label": "Año del modelo", "type": "number", "required": True, "default": 2024},
            {"key": "mounting_type", "label": "Tipo de montaje", "type": "select", "required": False,
             "options": ["Wall Mount", "Tabletop", "Tabletop, Wall Mount"], "default": "Wall Mount"},
            {"key": "color", "label": "Color", "type": "text", "required": False, "default": "Negro"},
            {"key": "special_feature", "label": "Características", "type": "multi_select",
             "options": ["Smart TV", "Built-In WiFi", "HDR", "Dolby Vision", "4K", "QLED", "OLED", "Voice Control"],
             "default": ["Smart TV", "4K"]},
            {"key": "connectivity_technology", "label": "Conectividad", "type": "multi_select",
             "options": ["Wi-Fi", "Bluetooth", "HDMI", "USB", "Ethernet"], "default": ["Wi-Fi", "Bluetooth"]},
            {"key": "voltage_v", "label": "Voltaje", "type": "text", "required": False, "default": "120V"},
            {"key": "warranty_description", "label": "Garantía", "type": "text", "required": True, "default": "90 days seller warranty"},
            {"key": "country_of_origin", "label": "País de origen", "type": "select", "required": True,
             "options": ["CN", "MX", "KR", "VN", "TW", "US"], "default": "CN"},
            {"key": "list_price_msrp", "label": "MSRP (USD)", "type": "number", "required": True, "default": 0},
        ],
    },
    ("PEST_CONTROL_DEVICE", "A1AM78C64UM0Y8"): {
        "validated": 1, "validated_at": "2026-06-09", "launch_count": 0,
        "required_attrs": ["item_name","brand","condition_type","purchasable_offer",
            "bullet_point","product_description","generic_keyword","country_of_origin",
            "supplier_declared_dg_hz_regulation","material_type","power_source_type",
            "item_type_keyword","warranty_description","is_assembly_required",
            "regulatory_compliance_certification","number_of_pieces"],
        "quality_attrs": ["specific_uses_for_product","color","item_weight",
            "item_length_width_height","special_feature","included_components",
            "model_year","list_price","model_number","model_name","recommended_browse_nodes"],
        "bonus_attrs": ["item_package_weight","item_package_dimensions","wattage","voltage"],
        "defaults": {
            "material_type": "Plástico",
            "power_source_type": "Alimentado por energía solar",
            "item_type_keyword": "electronic-pest-control",
            "supplier_declared_dg_hz_regulation": "not_applicable",
            "supplier_declared_has_product_identifier_exemption": True,
            "batteries_required": False, "batteries_included": False,
            "number_of_items": 1, "number_of_pieces": 1,
            "is_assembly_required": False,
            "regulatory_compliance_type": "cofepris_registration_num",
            "regulatory_compliance_value": "N/A",
            "country_of_origin": "CN",
            "warranty_description": "90 días garantía del vendedor",
            "recommended_browse_nodes": [{"marketplace_id": "A1AM78C64UM0Y8", "value": "23536384011"}],
        },
        "ai_hints": (
            "PEST_CONTROL_DEVICE (Amazon MX): material_type MUST be in Spanish: 'Plástico'/'Metal'/'Aluminio'/"
            "'Acero inoxidable'. power_source_type en español con language_tag es_MX: 'Alimentado por energía solar'/"
            "'Con Alimentación de Batería'/'Cable eléctrico'. "
            "is_assembly_required=false, number_of_pieces=1 SIEMPRE requeridos. "
            "regulatory_compliance_certification: regulation_type='cofepris_registration_num', value='N/A'. "
            "item_type_keyword: 'electronic-pest-control'. Browse node MX: 23536384011 "
            "(Repelente Eléctrico de Insectos). specific_uses: ['Mosquitos','Mosca','Exterior']. "
            "GTIN exemption: supplier_declared_has_product_identifier_exemption=true."
        ),
        "field_defs": [
            {"key": "material_type", "label": "Material", "type": "select", "required": True,
             "options": ["Plástico", "Metal", "Aluminio", "Acero inoxidable"], "default": "Plástico"},
            {"key": "power_source_type", "label": "Fuente de energía", "type": "select", "required": True,
             "options": ["Alimentado por energía solar", "Con Alimentación de Batería", "Cable eléctrico"],
             "default": "Alimentado por energía solar"},
            {"key": "is_assembly_required", "label": "¿Requiere montaje?", "type": "boolean", "required": True, "default": False},
            {"key": "number_of_pieces", "label": "Número de piezas", "type": "number", "required": True, "default": 1},
            {"key": "color", "label": "Color", "type": "text", "required": False, "default": ""},
            {"key": "specific_uses_for_product", "label": "Usos específicos", "type": "multi_select",
             "options": ["Mosquitos", "Mosca", "Mariposas de noche", "Exterior", "Interior", "Jardín"],
             "default": ["Mosquitos", "Exterior"]},
            {"key": "special_feature", "label": "Características especiales", "type": "multi_select",
             "options": ["Solar", "Impermeable", "Portátil", "Sin químicos", "Silencioso"], "default": []},
            {"key": "warranty_description", "label": "Garantía", "type": "text", "required": True,
             "default": "90 días garantía del vendedor"},
            {"key": "country_of_origin", "label": "País de origen", "type": "select", "required": True,
             "options": ["CN", "MX", "US", "VN", "TW"], "default": "CN"},
        ],
    },
    ("ELECTRIC_LANTERN", "A1AM78C64UM0Y8"): {
        "validated": 1, "validated_at": "2026-06-08", "launch_count": 0,
        "required_attrs": ["item_name","brand","condition_type","purchasable_offer",
            "bullet_point","product_description","generic_keyword","country_of_origin",
            "supplier_declared_dg_hz_regulation","material","power_source_type",
            "item_type_keyword","warranty_description"],
        "quality_attrs": ["color","item_weight","item_length_width_height","special_feature",
            "included_components","model_year","list_price","model_number","model_name",
            "wattage","light_source","recommended_browse_nodes"],
        "bonus_attrs": ["item_package_weight","item_package_dimensions","voltage","mounting_type"],
        "defaults": {
            "material_type": "Plástico",
            "power_source_type": "Energía solar",
            "item_type_keyword": "lanterns",
            "supplier_declared_dg_hz_regulation": "not_applicable",
            "supplier_declared_has_product_identifier_exemption": True,
            "batteries_required": False, "batteries_included": False,
            "number_of_items": 1, "country_of_origin": "CN",
            "warranty_description": "90 días garantía del vendedor",
            "recommended_browse_nodes": [{"marketplace_id": "A1AM78C64UM0Y8", "value": "23536384011"}],
        },
        "ai_hints": (
            "ELECTRIC_LANTERN (Amazon MX): material_type MUST be in Spanish: 'Plástico'/'Metal'/'Aluminio'. "
            "power_source_type: 'Energía solar'/'Batería'/'Cable eléctrico'. "
            "item_type_keyword: 'lanterns'. GTIN exemption supported."
        ),
        "field_defs": [
            {"key": "material_type", "label": "Material", "type": "select", "required": True,
             "options": ["Plástico", "Metal", "Aluminio", "Acero inoxidable"], "default": "Plástico"},
            {"key": "power_source_type", "label": "Fuente de energía", "type": "select", "required": True,
             "options": ["Energía solar", "Batería", "Cable eléctrico"], "default": "Energía solar"},
            {"key": "color", "label": "Color", "type": "text", "required": False, "default": ""},
            {"key": "wattage", "label": "Vatios (W)", "type": "number", "required": False, "default": 0},
            {"key": "special_feature", "label": "Características", "type": "multi_select",
             "options": ["Solar", "Impermeable", "Portátil", "Recargable", "LED"], "default": []},
            {"key": "warranty_description", "label": "Garantía", "type": "text", "required": True,
             "default": "90 días garantía del vendedor"},
            {"key": "country_of_origin", "label": "País de origen", "type": "select", "required": True,
             "options": ["CN", "MX", "US", "VN", "TW"], "default": "CN"},
        ],
    },
    ("VACUUM_CLEANER", "ATVPDKIKX0DER"): {
        "validated": 1, "validated_at": "2026-06-05", "launch_count": 2,
        "required_attrs": ["item_name","brand","condition_type","purchasable_offer",
            "bullet_point","product_description","generic_keyword","country_of_origin",
            "supplier_declared_dg_hz_regulation","item_type_keyword","item_dimensions"],
        "quality_attrs": ["surface_recommendation","is_cordless","form_factor",
            "filter_type","power_source_type","capacity","special_feature",
            "included_components","warranty_description","model_year","list_price",
            "voltage","item_weight","required_product_compliance_certificate",
            "model_number","model_name","color","item_length_width_height"],
        "bonus_attrs": ["cleaning_path_width","noise_level","recommended_uses_for_product",
            "bag_type","specific_uses_for_product"],
        "defaults": {
            "required_product_compliance_certificate": "Not Applicable",
            "surface_recommendation": "Bare Floor", "is_cordless": False,
            "form_factor": "Stick", "filter_type": "Foam",
            "power_source_type": "Corded Electric", "capacity_value": 0.5,
            "capacity_unit": "liters", "item_type_keyword": "household-stick-vacuums",
            "supplier_declared_dg_hz_regulation": "not_applicable",
            "supplier_declared_material_regulation": "not_applicable",
            "supplier_declared_has_product_identifier_exemption": True,
            "voltage_v": "120V", "batteries_required": False, "batteries_included": False,
            "number_of_items": 1, "warranty_description": "90 days seller warranty"
        },
        "ai_hints": "VACUUM_CLEANER: surface_recommendation max 1 value: Bare Floor/Carpet/Hard Floor/Hardwoods/Laminate. form_factor: Cannister/Handheld/Robotic/Stick/Upright. filter_type: Foam/HEPA Filter/Cartridge/Cloth/Cyclonic. special_feature from enum only: Anti-Allergen, Bagless, Compact, Cordless, HEPA, Lightweight, Washable Filter. connectivity_technology: NEVER Corded Electric.",
        "field_defs": [
            {"key": "form_factor", "label": "Tipo de aspiradora", "type": "select", "required": True,
             "options": ["Stick", "Upright", "Robotic", "Handheld", "Cannister"], "default": "Stick"},
            {"key": "power_source_type", "label": "Fuente de energía", "type": "select", "required": True,
             "options": ["Corded Electric", "Battery Powered", "Hybrid (Corded And Cordless)"],
             "default": "Corded Electric"},
            {"key": "filter_type", "label": "Tipo de filtro", "type": "select", "required": True,
             "options": ["Foam", "HEPA Filter", "Cartridge", "Cloth", "Cyclonic"], "default": "Foam"},
            {"key": "surface_recommendation", "label": "Superficie recomendada", "type": "select", "required": True,
             "options": ["Bare Floor", "Carpet", "Hard Floor", "Hardwoods", "Laminate"], "default": "Bare Floor"},
            {"key": "color", "label": "Color", "type": "text", "required": False, "default": ""},
            {"key": "special_feature", "label": "Características", "type": "multi_select",
             "options": ["Anti-Allergen", "Bagless", "Compact", "Cordless", "HEPA", "Lightweight", "Washable Filter"],
             "default": []},
            {"key": "voltage_v", "label": "Voltaje", "type": "text", "required": True, "default": "120V"},
            {"key": "warranty_description", "label": "Garantía", "type": "text", "required": True,
             "default": "90 days seller warranty"},
            {"key": "country_of_origin", "label": "País de origen", "type": "select", "required": True,
             "options": ["CN", "MX", "US", "VN", "TW"], "default": "CN"},
        ],
    },
}


async def get_product_type_template(product_type: str, marketplace_id: str = "ATVPDKIKX0DER") -> dict:
    import json as _j
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            "SELECT required_attrs,quality_attrs,bonus_attrs,defaults_json,ai_hints,validated,launch_count,validated_at,field_defs_json FROM amz_product_type_templates WHERE product_type=? AND marketplace_id=?",
            (product_type.upper(), marketplace_id),
        )).fetchone()
    if not row:
        return {}
    try:
        return {
            "product_type": product_type.upper(), "marketplace_id": marketplace_id,
            "required_attrs": _j.loads(row[0] or "[]"), "quality_attrs": _j.loads(row[1] or "[]"),
            "bonus_attrs": _j.loads(row[2] or "[]"), "defaults": _j.loads(row[3] or "{}"),
            "ai_hints": row[4] or "", "validated": bool(row[5]),
            "launch_count": row[6] or 0, "validated_at": row[7],
            "field_defs": _j.loads(row[8] or "[]"),
        }
    except Exception:
        return {}


async def save_product_type_template(product_type: str, marketplace_id: str, data: dict) -> None:
    import json as _j
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO amz_product_type_templates '
            '(product_type,marketplace_id,required_attrs,quality_attrs,bonus_attrs,defaults_json,ai_hints,validated,validated_at,launch_count,field_defs_json,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime("now"))',
            (
                product_type.upper(), marketplace_id,
                _j.dumps(data.get("required_attrs", [])),
                _j.dumps(data.get("quality_attrs", [])),
                _j.dumps(data.get("bonus_attrs", [])),
                _j.dumps(data.get("defaults", {})),
                data.get("ai_hints", ""),
                1 if data.get("validated") else 0,
                data.get("validated_at"),
                data.get("launch_count", 0),
                _j.dumps(data.get("field_defs", [])),
            ),
        )
        await db.commit()


async def list_product_type_templates(marketplace_id: str = None) -> list:
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        if marketplace_id:
            rows = await (await db.execute(
                "SELECT product_type,marketplace_id,validated,launch_count,validated_at,updated_at FROM amz_product_type_templates WHERE marketplace_id=? ORDER BY launch_count DESC",
                (marketplace_id,),
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT product_type,marketplace_id,validated,launch_count,validated_at,updated_at FROM amz_product_type_templates ORDER BY launch_count DESC"
            )).fetchall()
    return [
        {"product_type": r[0], "marketplace_id": r[1], "validated": bool(r[2]),
         "launch_count": r[3], "validated_at": r[4], "updated_at": r[5]}
        for r in rows
    ]


async def increment_template_launch(product_type: str, marketplace_id: str) -> None:
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        await db.execute(
            'UPDATE amz_product_type_templates SET launch_count=launch_count+1, validated=1, '
            'validated_at=COALESCE(validated_at,date("now")), updated_at=datetime("now") '
            "WHERE product_type=? AND marketplace_id=?",
            (product_type.upper(), marketplace_id),
        )
        await db.commit()


async def seed_product_type_templates() -> None:
    for (pt, mk), data in _SEED_TEMPLATES.items():
        existing = await get_product_type_template(pt, mk)
        # Always update templates that have validated=1 in seed (reflects new required attrs discovered)
        if not existing or data.get("validated"):
            await save_product_type_template(pt, mk, data)


async def save_launched_listing(seller_id: str, sku: str, product_type: str,
                                title: str, price: float, currency: str, asin: str = None) -> None:
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO amz_launched_listings '
            '(seller_id,sku,asin,product_type,title,price,currency,launched_at,check_status) '
            'VALUES (?,?,?,?,?,?,?,datetime("now"),"pending")',
            (seller_id, sku, asin, product_type, title[:200] if title else "", price, currency),
        )
        await db.commit()


async def get_launched_listings(seller_id: str, limit: int = 50) -> list:
    import json as _j
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        rows = await (await db.execute(
            'SELECT sku,asin,product_type,title,price,currency,launched_at,check_status,check_result,checked_at '
            'FROM amz_launched_listings WHERE seller_id=? ORDER BY launched_at DESC LIMIT ?',
            (seller_id, limit),
        )).fetchall()
    return [
        {"sku": r[0], "asin": r[1], "product_type": r[2], "title": r[3],
         "price": r[4], "currency": r[5], "launched_at": r[6],
         "check_status": r[7], "check_result": r[8], "checked_at": r[9]}
        for r in rows
    ]


# == SKU ↔ UPC internal mapping ===============================================

async def get_sku_upc(sku: str) -> str:
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        row = await (await db.execute(
            "SELECT upc FROM sku_upc_map WHERE sku=?", (sku,)
        )).fetchone()
    return row[0] if row else ""


async def save_sku_upc(sku: str, upc: str, source: str = "generated") -> None:
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sku_upc_map (sku,upc,source,created_at) VALUES (?,?,?,datetime('now'))",
            (sku, upc, source),
        )
        await db.commit()


# == Amazon Listing Actions (close/delete history) ============================

async def save_listing_action(seller_id: str, sku: str, asin: str,
                               action: str, reason: str = '') -> None:
    import time as _t
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        await db.execute(
            'INSERT INTO amz_listing_actions (seller_id,sku,asin,action,reason,performed_at) VALUES (?,?,?,?,?,datetime("now"))',
            (seller_id, sku, asin or '', action, reason or ''))
        await db.commit()


async def get_listing_actions(seller_id: str, limit: int = 100) -> list:
    async with __import__('aiosqlite').connect(DATABASE_PATH) as db:
        db.row_factory = __import__('aiosqlite').Row
        rows = await (await db.execute(
            'SELECT sku,asin,action,reason,performed_at FROM amz_listing_actions WHERE seller_id=? ORDER BY performed_at DESC LIMIT ?',
            (seller_id, limit))).fetchall()
    return [dict(r) for r in rows]


async def get_deletion_candidates(
        seller_id: str,
        days_no_sale: int = 365,
        page: int = 1,
        per_page: int = 10,
) -> dict:
    """Returns deletion candidates with full decision data + pagination."""
    import aiosqlite as _aio
    offset = (page - 1) * per_page

    _base = """
        SELECT
            al.sku, al.asin, al.title, al.status, al.price, al.available_qty,
            MAX(oh.order_date)  AS last_sale,
            COUNT(DISTINCT oh.order_id) AS total_orders,
            CAST(
                (julianday('now') - julianday(COALESCE(MAX(oh.order_date),'2020-01-01')))
                AS INTEGER
            ) AS days_no_sale,
            COALESCE(bc.retail_ph, 0) AS bm_price,
            bc.brand AS bm_brand,
            COALESCE(bm_stk.bm_stock, 0) AS bm_stock
        FROM amazon_listings al
        LEFT JOIN order_history oh
            ON oh.account_id = al.seller_id
            AND oh.platform IN ('amazon','amz','Amazon')
            AND (oh.sku = al.sku OR oh.sku = al.base_sku)
        LEFT JOIN bm_product_catalog bc
            ON bc.sku = al.base_sku OR bc.sku = al.sku
        LEFT JOIN (
            SELECT base_sku, SUM(available_qty) as bm_stock
            FROM ml_listings WHERE status = 'active' GROUP BY base_sku
        ) bm_stk ON bm_stk.base_sku = al.base_sku
        WHERE al.seller_id = ?
        GROUP BY al.sku
        HAVING days_no_sale > ? OR last_sale IS NULL
    """

    async with _aio.connect(DATABASE_PATH) as db:
        db.row_factory = _aio.Row

        # Total count for pagination
        _cnt = await (await db.execute(
            f"SELECT COUNT(*) FROM ({_base}) sub",
            (seller_id, days_no_sale)
        )).fetchone()
        total = _cnt[0] if _cnt else 0

        # Paginated data
        rows = await (await db.execute(
            _base + " ORDER BY days_no_sale DESC LIMIT ? OFFSET ?",
            (seller_id, days_no_sale, per_page, offset)
        )).fetchall()

    return {
        "items":    [dict(r) for r in rows],
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "days":     days_no_sale,
    }




# == Amazon Listing Parents Detection =========================================

async def detect_and_mark_parents(seller_id: str, use_catalog_api: bool = False) -> dict:
    """
    Marks amazon_listings rows as is_parent=1 using heuristic + optional Catalog API.
    Heuristic: price=0 AND qty=0 AND status in (INACTIVE,SUPPRESSED,INCOMPLETE)
    Returns: {marked: N, verified_via_api: M, seller_id: ...}
    """
    result = {"seller_id": seller_id, "marked": 0, "verified_via_api": 0}

    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        cur = await db.execute(
            """UPDATE amazon_listings
               SET is_parent = 1
               WHERE seller_id = ?
                 AND (price IS NULL OR price = 0)
                 AND (available_qty IS NULL OR available_qty = 0)
                 AND UPPER(status) IN ('INACTIVE', 'SUPPRESSED', 'INCOMPLETE')
                 AND is_parent = 0""",
            (seller_id,),
        )
        result["marked"] = cur.rowcount
        await db.commit()

        if not use_catalog_api or result["marked"] == 0:
            return result

        rows = await (await db.execute(
            "SELECT sku, asin FROM amazon_listings WHERE seller_id=? AND is_parent=1 AND asin!='' LIMIT 100",
            (seller_id,),
        )).fetchall()

    if not rows:
        return result

    try:
        from app.services.amazon_client import get_amazon_client
        client = await get_amazon_client(seller_id=seller_id)
        if not client:
            return result

        not_parents = []
        for row in rows:
            asin = row[1]; sku = row[0]
            if not asin:
                continue
            try:
                catalog = await client._request(
                    "GET", f"/catalog/2022-04-01/items/{asin}",
                    params={"marketplaceIds": client.marketplace_id, "includedData": "relationships"},
                )
                rels = catalog.get("relationships") or []
                has_children = any(
                    rel.get("type") in ("VARIATION", "variation") and rel.get("childAsins")
                    for rel in rels
                )
                if has_children:
                    result["verified_via_api"] += 1
                else:
                    not_parents.append(sku)
            except Exception:
                pass

        if not_parents:
            async with __import__("aiosqlite").connect(DATABASE_PATH) as db2:
                for sku in not_parents:
                    await db2.execute(
                        "UPDATE amazon_listings SET is_parent=0 WHERE seller_id=? AND sku=?",
                        (seller_id, sku))
                await db2.commit()
    except Exception:
        pass

    return result


async def get_parent_listings(seller_id: str, page: int = 1, per_page: int = 20) -> dict:
    """Returns listings marked as parents (variation containers)."""
    offset = (page - 1) * per_page
    async with __import__("aiosqlite").connect(DATABASE_PATH) as db:
        db.row_factory = __import__("aiosqlite").Row
        cnt   = await (await db.execute(
            "SELECT COUNT(*) FROM amazon_listings WHERE seller_id=? AND is_parent=1", (seller_id,)
        )).fetchone()
        total = cnt[0] if cnt else 0
        rows  = await (await db.execute(
            "SELECT sku, asin, title, status FROM amazon_listings WHERE seller_id=? AND is_parent=1 ORDER BY title LIMIT ? OFFSET ?",
            (seller_id, per_page, offset),
        )).fetchall()
    return {
        "items": [dict(r) for r in rows], "total": total,
        "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


async def save_item_change(
    item_id: str,
    account_id: str,
    field: str,
    new_value: str,
    old_value: str = "",
    changed_by: str = "",
) -> None:
    """Registra un cambio de campo en item_history. Fire-and-forget desde endpoints de edición."""
    import time as _time
    now = _time.time()
    changed_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO item_history
               (item_id, account_id, field, old_value, new_value, changed_by, changed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, account_id, field, old_value, new_value, changed_by, changed_at, now),
        )
        await db.commit()


async def get_item_history(item_id: str, limit: int = 50) -> list:
    """Retorna los últimos cambios de un item, del más reciente al más antiguo."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT field, old_value, new_value, changed_by, changed_at
               FROM item_history
               WHERE item_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (item_id, limit),
        )).fetchall()
    return [dict(r) for r in rows]
