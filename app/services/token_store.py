import asyncio
import json
import logging
import os
import aiosqlite
from datetime import datetime, timedelta, date
from typing import Optional
from pathlib import Path
from app.config import DATABASE_PATH
from app.services.sku_utils import clean_bm_title

logger = logging.getLogger(__name__)

# Nicknames conocidos de cuentas propias — fallback cuando la ML API rate-limita
# durante el arranque (o cualquier otra falla) y una cuenta queda con nickname
# vacío en DB. Única fuente de verdad — usado tanto al escribir (seeding) como
# al leer (get_tokens/get_any_tokens/get_all_tokens), para que el dropdown de
# cuentas NUNCA muestre un ID numérico crudo para una cuenta ya conocida,
# sin importar qué tan mal haya salido el seeding de esa corrida.
KNOWN_ML_NICKNAMES: dict = {
    "523916436": "APANTALLATEMX",
    "292395685": "AUTOBOT MEXICO",
    "391393176": "BLOWTECHNOLOGIES",
    "515061615": "LUTEMAMEXICO",
}


def _with_nickname_fallback(row: dict) -> dict:
    if row is not None and not row.get("nickname"):
        fallback = KNOWN_ML_NICKNAMES.get(str(row.get("user_id", "")))
        if fallback:
            row["nickname"] = fallback
    return row


# Mismo patrón que KNOWN_ML_NICKNAMES pero para las 3 cuentas Amazon — agregado
# 2026-07-24 tras un reporte de un usuario viendo el nickname de OTRA cuenta
# en el banner de Amazon. La investigación no encontró una fila con nickname
# incorrecto en ese momento, pero confirmó que el lado Amazon no tenía ningún
# respaldo contra la misma carrera de datos (Railway borra el SQLite en cada
# redeploy) que sí causó el bug de nicknames ML ese mismo día — este fallback
# cierra ese hueco preventivamente, sin importar si fue o no la causa exacta.
KNOWN_AMAZON_NICKNAMES: dict = {
    "A20NFIUQNEYZ1E": "VECKTOR IMPORTS",
    "A252KSQ687FNRO": "AUTOBOT AMZ MX",
    "A22XNR713HGDVG": "ExclusiveBulbs",
}


def _with_amazon_nickname_fallback(row: dict) -> dict:
    if row is not None and not row.get("nickname"):
        fallback = KNOWN_AMAZON_NICKNAMES.get(str(row.get("seller_id", "")))
        if fallback:
            row["nickname"] = fallback
    return row


async def init_db():
    """Inicializa la base de datos SQLite. Crea el directorio si no existe (Railway Volume)."""
    db_path = Path(DATABASE_PATH)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        # FIX 2026-08-08: WAL mode -- por default SQLite usa "rollback journal",
        # que bloquea TODAS las lecturas mientras hay una escritura en curso
        # (causó "database is locked" reales en producción, ej. 2026-08-07:
        # varias peticiones GET fallaron con 500 durante ~6s por una escritura
        # concurrente). WAL permite lecturas concurrentes con una escritura en
        # curso -- solo sigue habiendo un escritor a la vez, pero deja de
        # bloquear lectores. journal_mode=WAL se persiste en el header del
        # archivo .db, así que basta fijarlo una vez aquí (init_db corre al
        # arranque) para que TODAS las conexiones futuras -- las de este
        # archivo y las decenas de aiosqlite.connect() sueltos en main.py --
        # queden en WAL sin tener que tocar cada una. synchronous=NORMAL es
        # la combinación estándar recomendada con WAL (segura ante crash de la
        # app, ligeramente menos estricta que FULL ante un corte de energía
        # real -- aceptable para este caso de uso).
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
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
            CREATE TABLE IF NOT EXISTS ml_message_views (
                pack_id    TEXT NOT NULL,
                account_id TEXT NOT NULL,
                viewed_by  TEXT NOT NULL,
                viewed_at  REAL NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (pack_id, account_id)
            )
        """)
        # Migración 2026-08-12: "Seguimiento" -- marcar un mensaje/hilo (ML o
        # Amazon, misma tabla reusada con prefijos) que YA se respondió pero
        # falta enviar algo después (guía, foto, dato que no se tenía a la
        # mano). Ortogonal a `status` -- un hilo puede estar 'resolved' Y
        # needs_followup=1 al mismo tiempo, son cosas independientes.
        for _col, _def in (
            ("needs_followup",     "INTEGER NOT NULL DEFAULT 0"),
            ("follow_up_note",     "TEXT NOT NULL DEFAULT ''"),
            ("followup_marked_at", "REAL NOT NULL DEFAULT 0"),
        ):
            try:
                await db.execute(f"ALTER TABLE ml_message_views ADD COLUMN {_col} {_def}")
                await db.commit()
            except Exception:
                pass  # columna ya existe
        # Índice local de conversaciones ML (2026-08-04) — reemplaza el escaneo en
        # vivo de "50 órdenes más recientes" de get_messages() (meli_client.py),
        # que perdía prácticamente todas las conversaciones reales con cualquier
        # volumen de órdenes decente (ver DEVLOG). Se llena por webhook (topic
        # "messages") + backfill inicial — la pestaña Mensajes ahora LEE de aquí
        # en vez de escanear órdenes cada vez que se abre.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ml_messages_index (
                pack_id           TEXT NOT NULL,
                account_id        TEXT NOT NULL,
                order_id          TEXT NOT NULL DEFAULT '',
                last_message_from TEXT NOT NULL DEFAULT '',
                last_message_text TEXT NOT NULL DEFAULT '',
                last_message_date TEXT NOT NULL DEFAULT '',
                total_messages    INTEGER NOT NULL DEFAULT 0,
                updated_at        REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (pack_id, account_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ml_messages_index_account
            ON ml_messages_index (account_id, last_message_date)
        """)
        # Firma de quién respondió cada mensaje ML (2026-08-11) -- ML no
        # distingue empleados, solo sabe que respondió "la cuenta". Jovan
        # reportó (y ya lo había pedido antes) que necesita ver qué persona
        # de su equipo contestó cada conversación. Se llena en send_message
        # (health.py) al momento de enviar; solo cubre mensajes enviados
        # desde la app de aquí en adelante -- no hay forma de saber quién
        # mandó algo directo desde ML o antes de este cambio.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ml_message_sent_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pack_id    TEXT NOT NULL,
                account_id TEXT NOT NULL,
                sent_by    TEXT NOT NULL,
                sent_at    REAL NOT NULL,
                text       TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ml_message_sent_log_pack
            ON ml_message_sent_log (pack_id, account_id)
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
        # ─────────────────────────────────────────────────────────────────
        # TABLAS: listing_snapshots / listing_change_log — "Vigilancia"
        # (idea tomada de Helium10 Alerts, 2026-07-23). Snapshot actual por
        # listing (título/precio/imagen/si somos ganador de catálogo o Buy
        # Box) + timeline append-only de cambios detectados entre snapshots.
        # not_winning_since: NULL si ganamos (o no se sabe) — se marca la
        # primera vez que dejamos de ganar, para saber cuánto tiempo lleva.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS listing_snapshots (
                platform           TEXT NOT NULL,
                account_id         TEXT NOT NULL,
                item_id            TEXT NOT NULL,
                sku                TEXT NOT NULL DEFAULT '',
                title              TEXT NOT NULL DEFAULT '',
                price              REAL NOT NULL DEFAULT 0,
                main_image_url     TEXT NOT NULL DEFAULT '',
                is_winner          INTEGER,
                total_competitors  INTEGER NOT NULL DEFAULT 0,
                not_winning_since  REAL,
                last_checked       REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (platform, account_id, item_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS listing_change_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                platform      TEXT NOT NULL,
                account_id    TEXT NOT NULL,
                item_id       TEXT NOT NULL,
                sku           TEXT NOT NULL DEFAULT '',
                field         TEXT NOT NULL,
                old_value     TEXT NOT NULL DEFAULT '',
                new_value     TEXT NOT NULL DEFAULT '',
                detected_at   REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_lcl_account ON listing_change_log(platform, account_id, detected_at)")
        # ─────────────────────────────────────────────────────────────────
        # TABLA: coverage_price_alerts — sugerencia de precio por cobertura
        # de stock (días de supply). reason: 'escasez' (subir precio, se
        # está agotando) | 'sobrestock' (bajar precio, lleva mucho parado).
        # Se recalcula completo cada ciclo de prewarm — nunca auto-aplica,
        # el usuario confirma vía /sync-price (mismo mecanismo que
        # ml_price_alerts, que ya hace el PUT real con auditoría).
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coverage_price_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL,
                item_id         TEXT NOT NULL,
                sku             TEXT NOT NULL DEFAULT '',
                product_title   TEXT NOT NULL DEFAULT '',
                current_price   REAL NOT NULL DEFAULT 0,
                suggested_price REAL NOT NULL DEFAULT 0,
                reason          TEXT NOT NULL DEFAULT '',
                days_supply     REAL,
                units_30d       INTEGER NOT NULL DEFAULT 0,
                last_scan       REAL NOT NULL DEFAULT 0,
                UNIQUE(user_id, item_id)
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: stock_issue_streaks — cuántas horas seguidas lleva un SKU
        # con un problema (ej. 'imbalanced' = Desbalance). El snapshot de
        # alertas se sobreescribe cada ciclo (~15 min) sin memoria de cuánto
        # lleva — esta tabla SÍ la guarda, para distinguir "recién detectado"
        # de "posible error de configuración BM persistente" (ver caso
        # LocationID 62/63 resuelto 2026-07-21).
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_issue_streaks (
                account_id     TEXT NOT NULL,
                sku            TEXT NOT NULL,
                issue_type     TEXT NOT NULL DEFAULT 'imbalanced',
                product_title  TEXT NOT NULL DEFAULT '',
                first_seen_ts  REAL NOT NULL,
                last_seen_ts   REAL NOT NULL,
                PRIMARY KEY (account_id, sku, issue_type)
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
        # Migration: señales dinámicas del score (idea tomada de Helium10 Listing
        # Analyzer, 2026-07-23) — antes el score era 100% estático (título/fotos/
        # atributos/precio). Guardar el desglose para mostrarlo en el detalle de UI.
        for _col, _def in (
            ("stock_score", "INTEGER NOT NULL DEFAULT 0"),
            ("price_comp_score", "INTEGER NOT NULL DEFAULT 0"),
            ("claims_score", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                await db.execute(f"ALTER TABLE ml_listing_quality ADD COLUMN {_col} {_def}")
                await db.commit()
            except Exception:
                pass
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
        # TABLA: bm_bulk_fetch_log — histórico de CADA intento real de bajar
        # el bulk de BM (éxito, vacío o error), no solo los éxitos como
        # bm_sync_log. FEATURE 2026-08-18 (pedido por Jovan tras el
        # incidente de 25h de bulk sin refrescar por timeouts silenciosos
        # de GR): sin esto, una racha de fallos no deja ningún rastro
        # consultable -- bm_sync_log solo se escribe cuando SÍ hubo éxito.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_bulk_fetch_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                bulk_name     TEXT    NOT NULL,
                status        TEXT    NOT NULL,
                rows_count    INTEGER NOT NULL DEFAULT 0,
                elapsed_s     REAL    NOT NULL DEFAULT 0,
                error_message TEXT    NOT NULL DEFAULT ''
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bm_bulk_fetch_log_ts ON bm_bulk_fetch_log(ts)")
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
        # Migration: sacar PDF/XML de BLOB en SQLite a archivos en disco
        # (uploads/invoices/) — cada factura nueva reescribía el archivo de la
        # DB completo, contribuyó al incidente de disk-full de 2026-07-18.
        # file_data/xml_data se dejan de escribir pero no se borran (evita
        # migración destructiva de schema); pdf_path/xml_path guardan la ruta.
        try:
            await db.execute("ALTER TABLE billing_invoices ADD COLUMN pdf_path TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE billing_invoices ADD COLUMN xml_path TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # Migración: facturas nuevas van a MinIO/S3 (MI2) en vez de uploads/invoices/
        # en disco — mismo motivo que claim_photos (crisis de disco Railway).
        try:
            await db.execute("ALTER TABLE billing_invoices ADD COLUMN storage TEXT NOT NULL DEFAULT 'local'")
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
        # bm_product_catalog: congelada y DROP-eada 2026-08-13 (fusionada en
        # bm_sku_master desde antes, ver upsert_bm_catalog_batch) — respaldo en
        # backups/bm_frozen_tables/. NO recrear el CREATE TABLE aquí.
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
            "INSERT OR IGNORE INTO stock_distribution_settings "
            "(id, scarce_threshold_units, scarce_threshold_days, safety_buffer_units, updated_at) "
            "VALUES (1, 3, 7, 1, 0)"
        )
        # ─────────────────────────────────────────────────────────────────
        # TABLA: seasonal_events — boost temporal al punto de reorden antes
        # de eventos de temporada alta (Buen Fin, Hot Sale, Navidad, etc).
        # lead_days = cuántos días ANTES de start_date ya se aplica el boost
        # (para que llegue reposición a tiempo). category_filter vacío = todas.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seasonal_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL,
                start_date       TEXT NOT NULL,
                end_date         TEXT NOT NULL,
                lead_days        INTEGER NOT NULL DEFAULT 14,
                multiplier       REAL NOT NULL DEFAULT 1.5,
                category_filter  TEXT NOT NULL DEFAULT '',
                active           INTEGER NOT NULL DEFAULT 1,
                created_at       REAL NOT NULL DEFAULT 0
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: reply_templates — respuestas rápidas/plantillas para
        # Mensajes de Compradores (ML + Amazon). platform: 'ml'|'amz'|'all'.
        # account_id: '' = todas las cuentas de esa plataforma; si no, user_id
        # ML o seller_id Amazon — cada cuenta puede necesitar responder distinto.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reply_templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL,
                body_text   TEXT NOT NULL,
                platform    TEXT NOT NULL DEFAULT 'all',
                account_id  TEXT NOT NULL DEFAULT '',
                created_by  TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL DEFAULT 0
            )
        """)
        try:
            await db.execute("ALTER TABLE reply_templates ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # Column already exists
        # ─────────────────────────────────────────────────────────────────
        # TABLAS: sku_bundles / sku_bundle_components — bundle real (SKU
        # combinado "SKU1 / SKU2" tal cual aparece en ML) con stock = mínimo
        # de sus componentes y margen real, en vez del atajo de tomar solo
        # el primer componente. bundle_sku debe coincidir EXACTO (tras
        # strip()) con el SKU tal como aparece en el listing.
        # own_price_mxn nulo = usar el precio actual de ML del listing.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sku_bundles (
                bundle_sku     TEXT PRIMARY KEY,
                own_price_mxn  REAL,
                created_at     REAL NOT NULL DEFAULT 0,
                updated_at     REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sku_bundle_components (
                bundle_sku     TEXT NOT NULL,
                component_sku  TEXT NOT NULL,
                qty_per_bundle INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (bundle_sku, component_sku)
            )
        """)
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
        # Migración 2026-08-13: costo de envío REAL por orden (ML: get_shipment_costs()
        # ya se llamaba pero se descartaba tras el cálculo; Amazon: costo por item ya
        # calculado en _save_amazon_items_history_bg). Antes _calc_margins() usaba un
        # estimado fijo/escalonado (envio=150 o por tramo de retail) para TODOS los
        # SKUs -- ahora se puede promediar el costo real histórico por SKU+plataforma
        # (ver get_avg_shipping_cost_map) y usar ESE promedio en vez del estimado.
        try:
            await db.execute("ALTER TABLE order_history ADD COLUMN shipping_cost_mxn REAL NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Migration: zona geográfica del comprador (ship_state_code = "MX-NLE" etc,
        # ship_zone = "MTY"/"CDMX"/"TJ") — para cruzar demanda por zona vs almacén
        # físico (feature de transferencias sugeridas). Solo ML por ahora: Amazon
        # requiere un Restricted Data Token (PII) que Jovan debe solicitar/aprobar
        # en Seller Central — ver DEVLOG. Nullable: se llena poco a poco, no hay
        # backfill retroactivo automático de órdenes viejas.
        try:
            await db.execute("ALTER TABLE order_history ADD COLUMN ship_state_code TEXT DEFAULT ''")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE order_history ADD COLUMN ship_zone TEXT DEFAULT ''")
            await db.commit()
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────
        # TABLAS: deuda semanal con la empresa proveedora — % fijo del retail
        # por unidad vendida (teles vs otras categorías). Un row del ledger
        # por (order_id, item_id, platform) generado desde upsert_order_history
        # — el UNIQUE + INSERT OR IGNORE evita doble conteo en resyncs.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS supplier_debt_ledger (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id       TEXT NOT NULL,
                item_id        TEXT NOT NULL,
                platform       TEXT NOT NULL,
                account_id     TEXT NOT NULL DEFAULT '',
                sku            TEXT NOT NULL DEFAULT '',
                is_tv          INTEGER NOT NULL DEFAULT 0,
                category_rate  REAL NOT NULL DEFAULT 0,
                quantity       INTEGER NOT NULL DEFAULT 1,
                retail_ph_usd  REAL NOT NULL DEFAULT 0,
                fx_rate        REAL NOT NULL DEFAULT 17.0,
                amount_mxn     REAL NOT NULL DEFAULT 0,
                order_date     TEXT NOT NULL DEFAULT '',
                iso_week       TEXT NOT NULL DEFAULT '',
                created_at     REAL NOT NULL DEFAULT 0,
                UNIQUE(order_id, item_id, platform)
            )
        """)
        # Migración 2026-08-13: reversa de deuda cuando la orden se cancela.
        # Antes la deuda se registraba una vez y quedaba para siempre, sin
        # importar si la orden se canceló después -- ver reverse_cancelled_debt().
        try:
            await db.execute("ALTER TABLE supplier_debt_ledger ADD COLUMN reversed_at REAL NOT NULL DEFAULT 0")
        except Exception:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sdl_week ON supplier_debt_ledger(iso_week)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS supplier_debt_payments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_date  TEXT NOT NULL,
                amount_mxn    REAL NOT NULL DEFAULT 0,
                reference     TEXT NOT NULL DEFAULT '',
                notes         TEXT NOT NULL DEFAULT '',
                created_by    TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS supplier_debt_settings (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                rate_tv     REAL NOT NULL DEFAULT 0.80,
                rate_other  REAL NOT NULL DEFAULT 0.50
            )
        """)
        await db.execute("INSERT OR IGNORE INTO supplier_debt_settings (id, rate_tv, rate_other) VALUES (1, 0.80, 0.50)")
        # ─────────────────────────────────────────────────────────────────
        # TABLA: reputation_snapshots — foto diaria de seller_reputation por
        # cuenta ML. Sin esto la reputación solo se ve como valor puntual
        # (¿estoy en verde/amarillo AHORA?) sin poder detectar que se está
        # deteriorando ANTES de cruzar a la zona roja. UNIQUE(account_id,
        # captured_date) + INSERT OR IGNORE -- se llena gratis desde
        # stock_sync_multi.py, que ya llama get_user_info() cada ciclo
        # (cada 5 min) para el rep_factor; aquí solo se guarda 1x/día.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reputation_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id    TEXT NOT NULL,
                level_id      TEXT NOT NULL DEFAULT '',
                claims_rate   REAL NOT NULL DEFAULT 0,
                cancel_rate   REAL NOT NULL DEFAULT 0,
                delay_rate    REAL NOT NULL DEFAULT 0,
                captured_date TEXT NOT NULL,
                captured_at   REAL NOT NULL DEFAULT 0,
                UNIQUE(account_id, captured_date)
            )
        """)
        # bm_stock_snapshot: congelada y DROP-eada 2026-08-13 (fusionada en
        # bm_sku_master desde antes, ver upsert_bm_stock_snapshot_batch) —
        # respaldo en backups/bm_frozen_tables/. NO recrear el CREATE TABLE aquí.
        # NOTA 2026-08-10: la tabla stock_issues_snapshot que iba aqui era un
        # duplicado -- la tabla real (con esa misma finalidad, sobrevivir
        # deploys) ya existe mas arriba como stock_issues_cache. Eliminada.
        # ─────────────────────────────────────────────────────────────────
        # TABLA: bm_bulk_cache_snapshot — foto en disco de los bulks CRUDOS de
        # BM (_bm_bulk_gr_cache, _bm_bulk_all_cache, _bm_bulk_loctj_cache,
        # _bm_bulk_loc47_cache, _bm_bulk_loc68_cache -- todos dict en memoria).
        # FIX 2026-08-10: mismo problema que stock_issues_snapshot pero un
        # nivel mas abajo -- cada deploy (y hoy hubo muchos) vacia estos bulks,
        # y features que dependen de ellos (ej. fallback de Tijuana para
        # "SKU no encontrado en BM", prellenado de Brand/Model) se quedan sin
        # datos hasta que el prewarm los reconstruye desde cero, con multiples
        # llamadas reales a BM de por medio -- puede tardar varios minutos por
        # deploy. Persistir esto permite recargar la ultima version buena de
        # inmediato al arrancar, igual que ya se hace con _bm_stock_cache.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_bulk_cache_snapshot (
                cache_name  TEXT PRIMARY KEY,
                ts          REAL NOT NULL DEFAULT 0,
                data_json   TEXT NOT NULL DEFAULT '[]'
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: bm_sku_master — maestro único de BM (fusiona bm_product_catalog
        # + bm_stock_snapshot). Fuente única de verdad para alertas, sugerencias
        # y lanzamientos. Dos timestamps porque título/retail/costo se refrescan
        # 1x/semana y stock cada ~10 min — cada bloque guarda su propia frescura.
        # bm_product_catalog y bm_stock_snapshot DROP-eadas 2026-08-13, sin
        # lectores/escritores reales desde la migración — respaldo en
        # backups/bm_frozen_tables/.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_sku_master (
                sku                TEXT PRIMARY KEY,
                title              TEXT NOT NULL DEFAULT '',
                brand              TEXT NOT NULL DEFAULT '',
                model              TEXT NOT NULL DEFAULT '',
                retail_ph          REAL NOT NULL DEFAULT 0,
                cost_usd           REAL NOT NULL DEFAULT 0,
                available_qty      INTEGER NOT NULL DEFAULT 0,
                reserve_qty        INTEGER NOT NULL DEFAULT 0,
                total_qty          INTEGER NOT NULL DEFAULT 0,
                catalog_updated_at REAL NOT NULL DEFAULT 0,
                stock_updated_at   REAL NOT NULL DEFAULT 0
            )
        """)
        # Migración: tamaño de pantalla (campo "Size" real de BM, ej. 55/65/75
        # pulgadas) — usado para sugerencias de reemplazo por SKU sin stock,
        # SIN esto se sugería el mismo tamaño equivocado (marca+precio nomás).
        try:
            await db.execute("ALTER TABLE bm_sku_master ADD COLUMN size INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Migración 2026-08-10: columnas para que bm_sku_master sea la FUENTE
        # UNICA de las alertas de stock (antes solo era snapshot de durabilidad
        # -- ver project_bm_sku_master.md). mty/cdmx/tj: desglose por almacen
        # (Transferencias Sugeridas + alertas TV). no_vendible_qty: unidades en
        # revision/dañadas que BM reporta aparte. verified=1 significa "el ciclo
        # de sync actual confirmo este dato contra BM" (0 = viene de un ciclo
        # anterior, dato stale que un fetch fallido no debe pisar con ceros --
        # mismo guard "Fix A" que ya existia en el pipeline viejo por-cuenta).
        for _col, _ddl in (
            ("mty_qty", "INTEGER NOT NULL DEFAULT 0"),
            ("cdmx_qty", "INTEGER NOT NULL DEFAULT 0"),
            ("tj_qty", "INTEGER NOT NULL DEFAULT 0"),
            ("no_vendible_qty", "INTEGER NOT NULL DEFAULT 0"),
            ("verified", "INTEGER NOT NULL DEFAULT 0"),
            # FEATURE 2026-08-17 (pedido por Jovan): available_qty es la SUMA
            # de todas las condiciones (GRA+GRB+GRC+NEW) -- nunca dice en CUÁL
            # condición específica hay stock real. best_condition_sku/qty
            # guardan el SKU exacto con sufijo (ej. SNTV007447-GRB) de la
            # condición con más stock, calculado en _bm_master_sync_once_inner
            # (ya tiene acceso a las filas exactas por condición del bulk).
            ("best_condition_sku", "TEXT NOT NULL DEFAULT ''"),
            ("best_condition_qty", "INTEGER NOT NULL DEFAULT 0"),
            # FEATURE 2026-08-19 (pedido por Jovan): bm_sku_master no tenía
            # categoría ni UPC -- se completan desde la corrida diaria del
            # gap scan (ConfColumns_Conditions_Excel, app/api/lanzar.py), que
            # SÍ trae esos campos y ya se descarga 1x/día sin costo extra.
            # No se fusiona con bm_sku_gaps -- cada tabla conserva su propio
            # ritmo (10 min para stock aquí, 1x/día para el catálogo
            # completo allá); esto solo completa 2 columnas que no cambian
            # con esa frecuencia.
            ("category", "TEXT NOT NULL DEFAULT ''"),
            ("upc", "TEXT NOT NULL DEFAULT ''"),
            # FEATURE 2026-08-19 (pedido por Jovan: mover alertas en tiempo real +
            # sustitutos/sugerencias a bm_sku_master): las alertas y el modal
            # "Sustituir" necesitaban CADA condición con stock real (GRA/GRB/
            # GRC/NEW/ICB/ICC), no solo la mejor -- lista JSON [{condition, qty,
            # sku}] ordenada por qty desc, mismo shape que ya devolvía
            # _bm_bulk_real_conditions() del bulk viejo, para reemplazo directo.
            ("conditions_json", "TEXT NOT NULL DEFAULT ''"),
            # FEATURE 2026-08-20 (directiva de Jovan: consolidación total --
            # nada fuera del loop de categorías debe llamar a BM, ni siquiera
            # el gap scan de Amazon/ML ni "/products/sin-bm"). Esos consumos
            # necesitaban ImageURL, que el bulk YA trae (NEEDFILE=True) pero
            # nunca se guardaba aquí.
            ("image_url", "TEXT NOT NULL DEFAULT ''"),
            # FEATURE 2026-08-21 (pedido explícito de Jovan): PNP ("Plug and
            # Play") solo se procesa en MTY -- pnp_mty_available/pnp_mty_novendible
            # son los mismos AvailableQTY/NoVendibleQty que BM muestra en su
            # propia UI (confirmado con captura real de Jovan: SNTV008001
            # Disponible=4, No Vendible=379), consulta CONDITION=PNP,
            # LOCATIONID=68. pnp_other_locations_qty es la suma de PNP
            # encontrado en CDMX/Tijuana -- anomalía real (TJ solo reabastece
            # con producto YA terminado, PNP no debería aparecer ahí). Solo
            # se llena para category="Televisions" (único caso con volumen
            # confirmado hoy) -- 0 en cualquier otra categoría.
            ("pnp_mty_available", "INTEGER NOT NULL DEFAULT 0"),
            ("pnp_mty_novendible", "INTEGER NOT NULL DEFAULT 0"),
            ("pnp_other_locations_qty", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                await db.execute(f"ALTER TABLE bm_sku_master ADD COLUMN {_col} {_ddl}")
            except Exception:
                pass
        # ─────────────────────────────────────────────────────────────────
        # TABLA: bm_sku_changes — historial de cambios detectados en cada sync
        # (mismo dato que BM ya nos manda, no llamadas nuevas). Retail/costo:
        # cualquier cambio se loguea. Stock: solo transiciones que importan
        # (se quedó en 0 / se resurtió) — ver _diff_and_log_stock_change en
        # el código que escribe esta tabla, para no llenarla de micro-ruido.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bm_sku_changes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sku         TEXT NOT NULL,
                field       TEXT NOT NULL,
                old_value   REAL,
                new_value   REAL,
                changed_at  REAL NOT NULL,
                source      TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bsc_sku ON bm_sku_changes(sku)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bsc_changed_at ON bm_sku_changes(changed_at)")
        # Migración única bm_product_catalog/bm_stock_snapshot -> bm_sku_master
        # ya completada y esas 2 tablas DROP-eadas 2026-08-13 (respaldo en
        # backups/bm_frozen_tables/) -- eliminada de aquí, referenciaba tablas
        # que ya no existen.
        # ─────────────────────────────────────────────────────────────────
        # TABLA: realtime_stock_alerts — feed de órdenes individuales sin stock,
        # detectadas al momento vía webhook de ML (Fase 1 — Amazon pendiente,
        # requiere infra AWS). UNIQUE evita duplicados si ML reenvía la misma
        # notificación (retries).
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS realtime_stock_alerts (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id               TEXT NOT NULL,
                item_id                TEXT NOT NULL DEFAULT '',
                platform               TEXT NOT NULL DEFAULT 'ml',
                account_id             TEXT NOT NULL DEFAULT '',
                sku                    TEXT NOT NULL DEFAULT '',
                quantity               INTEGER NOT NULL DEFAULT 1,
                available_qty_at_check INTEGER,
                order_date             TEXT NOT NULL DEFAULT '',
                detected_at            REAL NOT NULL DEFAULT 0,
                UNIQUE(order_id, sku, platform)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rsa_detected_at ON realtime_stock_alerts(detected_at)")
        # Migración: shipping_id — permite al loop de reconciliación revisar
        # el estado real del envío sin tener que re-resolver la orden completa.
        try:
            await db.execute("ALTER TABLE realtime_stock_alerts ADD COLUMN shipping_id TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # FIX 2026-08-18 (bug real confirmado por Jovan con evidencia): `sku`
        # ya viene normalizado (normalize_to_bm_sku le quita bundles/condición,
        # ej. "SNTV006485 / SNWM000001" -> "SNTV006485") -- necesario para
        # comparar contra bm_sku_master, PERO BM indexa su propio WebSKU con
        # el string CRUDO tal cual lo manda ML. Usar el normalizado para
        # resolver el ProductSKU real en BM llevaba a un producto DISTINTO Y
        # EQUIVOCADO cuando el SKU real tenía bundle/condición. sku_raw
        # preserva el valor original para ese caso -- sin él, no hay forma de
        # recuperarlo después.
        try:
            await db.execute("ALTER TABLE realtime_stock_alerts ADD COLUMN sku_raw TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────
        # TABLA: stock_alert_resolutions — registro de qué se hizo con cada
        # orden sin stock (Alertas de Stock): se sustituyó por otro SKU, o
        # se puso stock=0 en todas las cuentas por falta de inventario.
        # reactivated_at marca cuándo se atendió el aviso de "ya hay stock
        # de nuevo" (ver get_pending_restock_watches) — hasta entonces el
        # SKU sigue en la lista de reactivación pendiente si BM ya tiene
        # disponible > 0.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_alert_resolutions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id            TEXT NOT NULL,
                platform            TEXT NOT NULL DEFAULT 'ml',
                account_id          TEXT NOT NULL DEFAULT '',
                original_sku        TEXT NOT NULL,
                resolution_type     TEXT NOT NULL,
                substitute_sku      TEXT NOT NULL DEFAULT '',
                note                TEXT NOT NULL DEFAULT '',
                username            TEXT NOT NULL DEFAULT '',
                user_id             INTEGER,
                ts                  REAL NOT NULL DEFAULT 0,
                reactivated_at      REAL,
                reactivated_by      TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sar_sku ON stock_alert_resolutions(original_sku)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sar_ts ON stock_alert_resolutions(ts)")
        # FEATURE 2026-08-17 (pedido por Jovan): registrar si la sustitución
        # SI se inyecto en BinManager (no solo la nota interna de siempre) y
        # si despues se borro de BM desde nuestro historial.
        for _col_sql in [
            "ALTER TABLE stock_alert_resolutions ADD COLUMN bm_status TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE stock_alert_resolutions ADD COLUMN bm_message TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE stock_alert_resolutions ADD COLUMN bm_deleted_at REAL DEFAULT NULL",
            "ALTER TABLE stock_alert_resolutions ADD COLUMN bm_deleted_by TEXT NOT NULL DEFAULT ''",
            # FEATURE 2026-08-17 (pedido por Jovan): antes, en cuanto se
            # aplicaba en BM la sustitución quedaba "cerrada" en el
            # historial sin confirmar que el almacén de verdad la envió --
            # si el stock del sustituto se agotaba entre la promesa y el
            # envío real, nadie se enteraba. fulfillment_status: ''/'pendiente_envio'
            # (recién aplicada, esperando que la orden avance) ->
            # 'completado' (la orden ya se imprimió/envió) o 'cancelada'
            # (la orden se canceló -- el mapeo de BM se borra automático).
            "ALTER TABLE stock_alert_resolutions ADD COLUMN fulfillment_status TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE stock_alert_resolutions ADD COLUMN last_stock_check_at REAL DEFAULT NULL",
            "ALTER TABLE stock_alert_resolutions ADD COLUMN last_stock_check_qty INTEGER DEFAULT NULL",
            "ALTER TABLE stock_alert_resolutions ADD COLUMN shipment_resolved_at REAL DEFAULT NULL",
            # FIX 2026-08-18 (bug real confirmado por Jovan): original_sku ya
            # viene normalizado -- original_sku_raw preserva el seller_sku
            # CRUDO de ML (con bundle/condición si los tiene) para poder
            # resolver el ProductSKU real en BM con el WebSKU correcto, no uno
            # normalizado que puede apuntar a un producto distinto.
            "ALTER TABLE stock_alert_resolutions ADD COLUMN original_sku_raw TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                await db.execute(_col_sql)
            except Exception:
                pass  # columna ya existe
        # ─────────────────────────────────────────────────────────────────
        # TABLA: amazon_buyer_messages — mensajes reales de compradores Amazon
        # (Buyer-Seller Messaging) capturados vía el buzón Gmail dedicado que
        # Amazon reenvía en Notification Preferences → Messaging → Buyer
        # Messages (NO existe vía SP-API, ver reference_amazon_sp_api_docs).
        # message_id (header Message-ID del correo) es único — evita duplicar
        # el mismo mensaje si el poller vuelve a verlo. reply_to_addr es la
        # dirección tokenizada de Amazon (nombre@marketplace.amazon.com.mx) a
        # la que hay que enviar la respuesta para que Amazon la relance al
        # comprador real.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amazon_buyer_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id       TEXT NOT NULL,
                direction       TEXT NOT NULL DEFAULT 'inbound',
                order_id        TEXT NOT NULL DEFAULT '',
                asin            TEXT NOT NULL DEFAULT '',
                product_title   TEXT NOT NULL DEFAULT '',
                buyer_name      TEXT NOT NULL DEFAULT '',
                subject         TEXT NOT NULL DEFAULT '',
                body_text       TEXT NOT NULL DEFAULT '',
                reply_to_addr   TEXT NOT NULL DEFAULT '',
                message_id      TEXT NOT NULL DEFAULT '',
                in_reply_to     TEXT NOT NULL DEFAULT '',
                ts              REAL NOT NULL DEFAULT 0,
                read_at         REAL,
                replied_by      TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_abm_message_id ON amazon_buyer_messages(message_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_abm_seller_ts ON amazon_buyer_messages(seller_id, ts)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_abm_order ON amazon_buyer_messages(order_id)")
        # Watermark de IMAP UID por cuenta (2026-08-04) — antes cada ciclo de poll
        # (5 min) volvía a descargar por completo los últimos 200 correos de cada
        # buzón, sin importar si ya se habían visto — 60-80s por cuenta, secuencial
        # entre cuentas (ciclo real ~8-9 min, no 5). Con el watermark, cada poll
        # después del primero solo trae UIDs nuevos (típicamente 0-5), casi
        # instantáneo. UID de IMAP (no sequence number) — estable entre sesiones.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amazon_buyer_inbox_state (
                seller_id   TEXT PRIMARY KEY,
                last_uid    INTEGER NOT NULL DEFAULT 0,
                last_poll_ts REAL NOT NULL DEFAULT 0
            )
        """)
        # ─────────────────────────────────────────────────────────────────
        # TABLA: claims_history — reclamos/devoluciones persistidos por SKU/cuenta.
        # A diferencia de order_history, esto SOLO existe para ML por ahora — Amazon
        # no expone reason codes ni fotos vía SP-API (solo refund $ via Finances API).
        # sku se resuelve desde la orden asociada (resource_id) al momento del sync.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claims_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id       TEXT NOT NULL,
                platform       TEXT NOT NULL DEFAULT 'ml',
                account_id     TEXT NOT NULL DEFAULT '',
                order_id       TEXT NOT NULL DEFAULT '',
                item_id        TEXT NOT NULL DEFAULT '',
                sku            TEXT NOT NULL DEFAULT '',
                reason_id      TEXT NOT NULL DEFAULT '',
                stage          TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT '',
                quantity       INTEGER NOT NULL DEFAULT 1,
                amount_mxn     REAL NOT NULL DEFAULT 0,
                buyer_comment  TEXT NOT NULL DEFAULT '',
                date_created   TEXT NOT NULL DEFAULT '',
                synced_at      REAL NOT NULL DEFAULT 0,
                UNIQUE(claim_id, platform)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ch_sku ON claims_history(sku)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ch_account ON claims_history(account_id, platform)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ch_date ON claims_history(date_created)")
        # Migración 2026-08-13: dato de resolución del reclamo -- necesario para
        # reversar supplier_debt_ledger cuando el reembolso pasa DESPUÉS de
        # enviado (status de la orden no cambia en ese caso, la única señal
        # real es resolution.reason=="payment_refunded" del reclamo). Ver
        # reverse_debt_for_refunded_claims().
        for _col, _def in (
            ("resolution_reason", "TEXT NOT NULL DEFAULT ''"),
            ("refunded_buyer", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                await db.execute(f"ALTER TABLE claims_history ADD COLUMN {_col} {_def}")
            except Exception:
                pass
        # ─────────────────────────────────────────────────────────────────
        # TABLA: claim_photos — fotos de reclamos, mirror local en /app/data/claim_photos/
        # (Railway Volume persistente — ver reference_railway_volume_persistence).
        # Se guardan porque las URLs originales de ML pueden expirar/archivarse.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claim_photos (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id     TEXT NOT NULL,
                platform     TEXT NOT NULL DEFAULT 'ml',
                local_path   TEXT NOT NULL DEFAULT '',
                original_url TEXT NOT NULL DEFAULT '',
                from_role    TEXT NOT NULL DEFAULT '',
                downloaded_at REAL NOT NULL DEFAULT 0,
                UNIQUE(claim_id, local_path)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cp_claim ON claim_photos(claim_id)")
        # Migración: fotos nuevas van a MinIO/S3 (MI2) en vez del disco de Railway
        # (crisis de disco 2026-07-31, ver project_disk_crisis). storage='local'
        # para todo lo histórico ya en disco; 'local_path' sigue siendo la key S3
        # cuando storage='s3' (evita duplicar columna).
        try:
            await db.execute("ALTER TABLE claim_photos ADD COLUMN storage TEXT NOT NULL DEFAULT 'local'")
        except Exception:
            pass
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
        # TABLA: seller_flex_stock — stock real de Amazon Onsite/Seller Flex por
        # nodo (SYGL/SYQJ/SOKA/etc), fuente = query GetInventoryViewBySku de
        # sellerflex.amazon.com.mx (SP-API no expone este stock -- ver memoria
        # project_seller_flex_portal_and_qty_gap.md). Se llena via ingesta manual
        # disparada por Claude/Jovan desde el navegador (script en la pestaña ya
        # autenticada -- nunca se guardan credenciales ni cookies de sesión aquí).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seller_flex_stock (
                node         TEXT NOT NULL,
                warehouse    TEXT NOT NULL DEFAULT '',
                seller_id    TEXT NOT NULL DEFAULT '',
                sku          TEXT NOT NULL,
                asin         TEXT DEFAULT '',
                sellable_qty INTEGER NOT NULL DEFAULT 0,
                bound_qty    INTEGER NOT NULL DEFAULT 0,
                synced_at    REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (node, sku)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_seller_flex_stock_sku ON seller_flex_stock(sku)")
        # Migrate: bin real del contenedor (2026-08-22, pedido de Jovan) --
        # viene de la query GraphQL GetInventoryViewByBin, no del reporte
        # oficial (ese no trae bin). NULL/'' si el snapshot se cargó antes
        # de este cambio o no se pudo determinar.
        try:
            await db.execute('ALTER TABLE seller_flex_stock ADD COLUMN bin TEXT DEFAULT ""')
            await db.commit()
        except Exception:
            pass  # ya existe
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
        # ─────────────────────────────────────────────────────────────────
        # TABLA: amazon_seller_feedback — calificación del comprador AL VENDEDOR
        # (GET_SELLER_FEEDBACK_DATA). NO trae SKU directo de Amazon — order_sku
        # se llena cruzando order_id contra order_history al sincronizar.
        # feedback_key es el dedupe (Amazon no da un id único de feedback).
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS amazon_seller_feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id    TEXT NOT NULL DEFAULT '',
                seller_id     TEXT NOT NULL DEFAULT '',
                order_id      TEXT NOT NULL DEFAULT '',
                order_sku     TEXT NOT NULL DEFAULT '',
                rating        TEXT NOT NULL DEFAULT '',
                comment       TEXT NOT NULL DEFAULT '',
                rater_email   TEXT NOT NULL DEFAULT '',
                date_created  TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'pending',
                feedback_key  TEXT NOT NULL DEFAULT '',
                synced_at     REAL NOT NULL DEFAULT 0,
                UNIQUE(feedback_key)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_asf_account ON amazon_seller_feedback(account_id, status)")
        # Migración: ASIN + título + link directo — antes el tab de Feedback
        # solo mostraba "(sin SKU)" sin decir a qué producto pertenecía ni dar
        # forma de verlo en Amazon (Jovan lo reportó como "mocho" 2026-07-31).
        try:
            await db.execute("ALTER TABLE amazon_seller_feedback ADD COLUMN order_asin TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE amazon_seller_feedback ADD COLUMN order_title TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE amazon_seller_feedback ADD COLUMN asin_url TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────
        # TABLA: ml_item_reviews — reseñas de producto (rate 1-5 + comentario)
        # via GET /reviews/item/{id}. Ligado a item_id (no a order_id como
        # Amazon) — el SKU sale directo de ml_listings.base_sku por item_id.
        # ─────────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ml_item_reviews (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id    TEXT NOT NULL DEFAULT '',
                item_id       TEXT NOT NULL DEFAULT '',
                sku           TEXT NOT NULL DEFAULT '',
                review_id     TEXT NOT NULL DEFAULT '',
                rate          INTEGER NOT NULL DEFAULT 0,
                title         TEXT NOT NULL DEFAULT '',
                comment       TEXT NOT NULL DEFAULT '',
                date_created  TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'pending',
                synced_at     REAL NOT NULL DEFAULT 0,
                UNIQUE(review_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mir_account ON ml_item_reviews(account_id, status)")
        # Migración: título del PRODUCTO (distinto de `title`, que es el
        # encabezado de la reseña, ej. "Excelente") + link directo a la
        # publicación — mismo motivo que amazon_seller_feedback arriba.
        try:
            await db.execute("ALTER TABLE ml_item_reviews ADD COLUMN product_title TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE ml_item_reviews ADD COLUMN permalink TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        await db.commit()


async def save_item_sync(item_id: str, user_id: str, synced_qty: int, synced_by: str = "") -> None:
    """Registra que un item fue sincronizado ahora.
    Cross-user: cualquier cuenta que consulte get_recently_synced_ids verá este registro.
    TTL de supresión: 60 min — tiempo suficiente para que ML confirme el qty nuevo.
    """
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        rows = await (await db.execute(
            "SELECT item_id FROM item_sync_log WHERE synced_at > ?",
            (cutoff,),
        )).fetchall()
    return {r[0] for r in rows}


async def upsert_bm_catalog_batch(rows: list[dict]) -> int:
    """Guarda título/retail/costo en el maestro bm_sku_master (antes escribía
    en bm_product_catalog, ahora fusionada). Loguea en bm_sku_changes
    cualquier cambio de retail_ph/cost_usd — bajo volumen (sync semanal),
    cada cambio es relevante para alertas de precio.
    rows: list of {sku, retail_ph, cost_usd, brand, model, title, size,
    category, upc, image_url}. category/upc/image_url agregados 2026-08-20
    (pedido por Jovan): este sync corre 1x/día para TODO el catálogo de BM
    (con o sin stock) -- antes esos 3 campos solo los llenaba el loop de
    categorías, que únicamente toca SKUs que aparecen con stock actual.
    Usa COALESCE(NULLIF(...)) igual que _update_bm_master_for_category para
    no pisar un valor bueno ya escrito por el loop de categorías con uno
    vacío si esta fila viniera parcial.
    Retorna cantidad de rows insertadas/actualizadas.
    """
    if not rows:
        return 0
    now = __import__("time").time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        skus = [r["sku"] for r in rows]
        # Chunks de 500 — evita el límite de variables SQL de SQLite en catálogos grandes
        existing = {}
        for i in range(0, len(skus), 500):
            chunk = skus[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"SELECT sku, retail_ph, cost_usd FROM bm_sku_master WHERE sku IN ({placeholders})", chunk
            )
            existing.update({r["sku"]: r for r in await cur.fetchall()})

        changes = []
        for r in rows:
            old = existing.get(r["sku"])
            if not old:
                continue
            new_retail = r.get("retail_ph", 0) or 0
            new_cost = r.get("cost_usd", 0) or 0
            if round(old["retail_ph"] or 0, 4) != round(new_retail, 4):
                changes.append((r["sku"], "retail_ph", old["retail_ph"], new_retail, now, "catalog_sync"))
            if round(old["cost_usd"] or 0, 4) != round(new_cost, 4):
                changes.append((r["sku"], "cost_usd", old["cost_usd"], new_cost, now, "catalog_sync"))

        await db.executemany(
            """INSERT INTO bm_sku_master (
                   sku, title, brand, model, retail_ph, cost_usd, size,
                   category, upc, image_url, catalog_updated_at
               )
               VALUES (
                   :sku, :title, :brand, :model, :retail_ph, :cost_usd, :size,
                   :category, :upc, :image_url, :updated_at
               )
               ON CONFLICT(sku) DO UPDATE SET
                   title = excluded.title, brand = excluded.brand, model = excluded.model,
                   retail_ph = excluded.retail_ph, cost_usd = excluded.cost_usd,
                   size = excluded.size,
                   category = COALESCE(NULLIF(excluded.category, ''), bm_sku_master.category),
                   upc = COALESCE(NULLIF(excluded.upc, ''), bm_sku_master.upc),
                   image_url = COALESCE(NULLIF(excluded.image_url, ''), bm_sku_master.image_url),
                   catalog_updated_at = excluded.catalog_updated_at""",
            [{**r, "cost_usd": r.get("cost_usd", 0), "size": r.get("size", 0),
              "category": r.get("category", ""), "upc": r.get("upc", ""),
              "image_url": r.get("image_url", ""), "updated_at": now} for r in rows],
        )
        if changes:
            await db.executemany(
                "INSERT INTO bm_sku_changes (sku, field, old_value, new_value, changed_at, source) VALUES (?,?,?,?,?,?)",
                changes,
            )
        await db.commit()
    return len(rows)


async def get_bm_retail_ph(sku: str) -> float | None:
    """RetailPH (USD) de un solo SKU desde bm_sku_master (ya sincronizado,
    sin llamada a BM) -- usado por /api/items/{id}/suggested-price (2026-08-14,
    pedido por Jovan: precio de lista sugerido para recuperar 80%/60%)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("SELECT retail_ph FROM bm_sku_master WHERE sku = ?", (sku,))
        row = await cur.fetchone()
    if not row or not row[0]:
        return None
    return float(row[0])


async def get_bm_catalog_all() -> list[dict]:
    """Lee el maestro bm_sku_master (título/retail/costo). Usado al arrancar
    para popular cache en memoria."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT sku, retail_ph, cost_usd, brand, model, title, catalog_updated_at AS updated_at FROM bm_sku_master"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_bm_catalog_last_sync() -> float:
    """Retorna el timestamp de la última sincronización del catálogo, o 0 si nunca."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT MAX(catalog_updated_at) FROM bm_sku_master"
        ) as cur:
            row = await cur.fetchone()
    val = row[0] if row else None
    return float(val) if val else 0.0


# NOTA 2026-08-10: save_stock_issues_snapshot()/load_all_stock_issues_snapshots()
# YA EXISTIAN mas abajo en este archivo (tabla stock_issues_cache, no
# stock_issues_snapshot) desde antes -- diagnostique mal el 2026-08-09 y agregue
# un segundo par duplicado aqui con el mismo nombre pero tabla distinta. Python
# resuelve al ULTIMO definido en el modulo, asi que estas nunca corrieron (dead
# code inofensivo, pero confuso) -- las funciones reales que main.py siempre
# llamo son las de mas abajo (buscar "stock_issues_cache helpers"). Se eliminan
# aqui para no dejar 2 pares con el mismo nombre en el codigo.


async def upsert_bm_stock_snapshot_batch(rows: list[dict]) -> int:
    """Guarda el stock actual en el maestro bm_sku_master (antes escribía en
    bm_stock_snapshot, ahora fusionada) — NO es una llamada nueva a BM, solo
    persiste lo que el prewarm ya trajo. Loguea en bm_sku_changes SOLO
    transiciones de available_qty que cruzan cero (se quedó en 0 / se
    resurtió) — evita llenar el historial de micro-fluctuaciones cada ~10 min.
    rows: list of {sku, available_qty, reserve_qty, total_qty}
    """
    if not rows:
        return 0
    now = __import__("time").time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        skus = [r["sku"] for r in rows]
        # Chunks de 500 — evita el límite de variables SQL de SQLite en catálogos grandes
        existing = {}
        for i in range(0, len(skus), 500):
            chunk = skus[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"SELECT sku, available_qty FROM bm_sku_master WHERE sku IN ({placeholders})", chunk
            )
            existing.update({r["sku"]: (r["available_qty"] or 0) for r in await cur.fetchall()})

        changes = []
        for r in rows:
            sku = r["sku"]
            if sku not in existing:
                continue
            old_avail = existing[sku]
            new_avail = r.get("available_qty", 0) or 0
            crossed_out = old_avail > 0 and new_avail <= 0
            crossed_in = old_avail <= 0 and new_avail > 0
            if crossed_out or crossed_in:
                changes.append((sku, "available_qty", old_avail, new_avail, now, "stock_prewarm"))

        await db.executemany(
            """INSERT INTO bm_sku_master (sku, available_qty, reserve_qty, total_qty, stock_updated_at)
               VALUES (:sku, :available_qty, :reserve_qty, :total_qty, :updated_at)
               ON CONFLICT(sku) DO UPDATE SET
                   available_qty = excluded.available_qty,
                   reserve_qty = excluded.reserve_qty,
                   total_qty = excluded.total_qty,
                   stock_updated_at = excluded.stock_updated_at""",
            [{**r, "updated_at": now} for r in rows],
        )
        if changes:
            await db.executemany(
                "INSERT INTO bm_sku_changes (sku, field, old_value, new_value, changed_at, source) VALUES (?,?,?,?,?,?)",
                changes,
            )
        await db.commit()
    return len(rows)


async def upsert_bm_stock_full_batch(rows: list[dict]) -> int:
    """FIX 2026-08-10 — Fase B del rediseño "bm_sku_master como fuente unica"
    (pedido por Jovan: 1 sola sincronizacion de BM en vez de 4 por-cuenta
    redundantes, ver project_bm_sku_master.md y la auditoria del mismo dia).

    Version completa de upsert_bm_stock_snapshot_batch() -- escribe TAMBIEN
    el desglose mty/cdmx/tj, no_vendible y el flag verified. Usada
    EXCLUSIVAMENTE por el nuevo job independiente _bm_master_sync_loop() en
    main.py -- el pipeline viejo por-cuenta sigue usando la funcion original
    sin tocarla, para no arriesgar las alertas ya en produccion mientras se
    verifica el nuevo camino en paralelo (Fase C).

    rows: list of {sku, available_qty, reserve_qty, total_qty, mty_qty,
    cdmx_qty, tj_qty, no_vendible_qty, verified}. verified=1 = este ciclo
    confirmo el dato contra BM; verified=0 = SKU no encontrado en el ciclo
    actual (caller decide si igual lo manda con el ultimo valor conocido
    para no perder el dato, o lo omite -- ver guard "bulk parece caido" en
    el caller, mismo criterio que el pipeline viejo)."""
    if not rows:
        return 0
    now = __import__("time").time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        skus = [r["sku"] for r in rows]
        existing = {}
        for i in range(0, len(skus), 500):
            chunk = skus[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"SELECT sku, available_qty FROM bm_sku_master WHERE sku IN ({placeholders})", chunk
            )
            existing.update({r["sku"]: (r["available_qty"] or 0) for r in await cur.fetchall()})

        changes = []
        for r in rows:
            sku = r["sku"]
            if sku not in existing:
                continue
            old_avail = existing[sku]
            new_avail = r.get("available_qty", 0) or 0
            crossed_out = old_avail > 0 and new_avail <= 0
            crossed_in = old_avail <= 0 and new_avail > 0
            if crossed_out or crossed_in:
                changes.append((sku, "available_qty", old_avail, new_avail, now, "bm_master_sync"))

        await db.executemany(
            """INSERT INTO bm_sku_master
                   (sku, available_qty, reserve_qty, total_qty,
                    mty_qty, cdmx_qty, tj_qty, no_vendible_qty, verified,
                    best_condition_sku, best_condition_qty, stock_updated_at)
               VALUES (:sku, :available_qty, :reserve_qty, :total_qty,
                       :mty_qty, :cdmx_qty, :tj_qty, :no_vendible_qty, :verified,
                       :best_condition_sku, :best_condition_qty, :updated_at)
               ON CONFLICT(sku) DO UPDATE SET
                   available_qty   = excluded.available_qty,
                   reserve_qty     = excluded.reserve_qty,
                   total_qty       = excluded.total_qty,
                   mty_qty         = excluded.mty_qty,
                   cdmx_qty        = excluded.cdmx_qty,
                   tj_qty          = excluded.tj_qty,
                   no_vendible_qty = excluded.no_vendible_qty,
                   verified        = excluded.verified,
                   best_condition_sku = excluded.best_condition_sku,
                   best_condition_qty = excluded.best_condition_qty,
                   stock_updated_at = excluded.stock_updated_at""",
            [{
                "sku": r["sku"],
                "available_qty": r.get("available_qty", 0) or 0,
                "reserve_qty": r.get("reserve_qty", 0) or 0,
                "total_qty": r.get("total_qty", 0) or 0,
                "mty_qty": r.get("mty_qty", 0) or 0,
                "cdmx_qty": r.get("cdmx_qty", 0) or 0,
                "tj_qty": r.get("tj_qty", 0) or 0,
                "no_vendible_qty": r.get("no_vendible_qty", 0) or 0,
                "verified": 1 if r.get("verified") else 0,
                "best_condition_sku": r.get("best_condition_sku", "") or "",
                "best_condition_qty": r.get("best_condition_qty", 0) or 0,
                "updated_at": now,
            } for r in rows],
        )
        if changes:
            await db.executemany(
                "INSERT INTO bm_sku_changes (sku, field, old_value, new_value, changed_at, source) VALUES (?,?,?,?,?,?)",
                changes,
            )
        await db.commit()
    return len(rows)


async def get_all_known_base_skus() -> list[str]:
    """Union de base_sku (ya normalizado, columna existente) de ml_listings +
    amazon_listings, activos/pausados/inactivos -- el universo de SKUs que
    el nuevo job independiente _bm_master_sync_loop() debe mantener
    sincronizado en bm_sku_master. Sin llamadas a BM, solo SQLite local."""
    out: set = set()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "SELECT DISTINCT base_sku FROM ml_listings "
            "WHERE base_sku != '' AND status IN ('active', 'paused', 'inactive')"
        )
        out.update(r[0] for r in await cur.fetchall() if r[0])
        cur = await db.execute(
            "SELECT DISTINCT base_sku FROM amazon_listings WHERE base_sku != ''"
        )
        out.update(r[0] for r in await cur.fetchall() if r[0])
    return list(out)


async def get_bm_master_sync_meta() -> dict:
    """Estado del nuevo job independiente _bm_master_sync_loop() -- para el
    diag de comparacion (Fase C) y para exponer 'hace cuanto se sincronizo'
    sin depender del ciclo viejo por-cuenta."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) AS n, SUM(verified) AS nv, MAX(stock_updated_at) AS last_ts FROM bm_sku_master"
        )
        row = await cur.fetchone()
    return {
        "total_skus": row["n"] or 0,
        "verified_skus": row["nv"] or 0,
        "last_sync_ts": row["last_ts"] or 0,
    }


async def upsert_seller_flex_stock(node: str, warehouse: str, seller_id: str, items: list[dict], synced_at: float) -> int:
    """Reemplaza el snapshot de un nodo Seller Flex (node=SYGL/SYQJ/SOKA/etc)
    con los items recibidos [{sku, asin, sellable, bound}]. Borra primero las
    filas viejas del mismo node para no dejar SKUs fantasma que ya no
    reportó el último scan (un SKU que salió de la lista significa que ya
    no tiene stock ni bound -- el ingest solo manda items con stock>0)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM seller_flex_stock WHERE node = ?", (node,))
        rows = [
            (node, warehouse, seller_id, it["sku"], it.get("asin", ""),
             int(it.get("sellable", 0)), int(it.get("bound", 0)), synced_at, it.get("bin", ""))
            for it in items
        ]
        if rows:
            await db.executemany(
                """INSERT INTO seller_flex_stock (node, warehouse, seller_id, sku, asin, sellable_qty, bound_qty, synced_at, bin)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        await db.commit()
        return len(rows)


async def update_seller_flex_stock_row(node: str, sku: str, sellable_qty: int, synced_at: float) -> bool:
    """Actualiza UNA sola fila (node, sku) sin tocar el resto del snapshot del
    node -- para cuando se confirma en vivo que un ajuste puntual sí se
    aplicó (ver project_seller_flex_receive_adjust_mechanics.md), evita
    tener que re-escanear los ~800 SKUs del node solo para corregir 1."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "UPDATE seller_flex_stock SET sellable_qty = ?, synced_at = ? WHERE node = ? AND sku = ?",
            (sellable_qty, synced_at, node, sku),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_seller_flex_bins_for_node(node: str) -> list[str]:
    """Bines realmente en uso para un nodo (derivado del snapshot cargado --
    Seller Flex no tiene un catálogo fijo de bines, cualquier string es un
    bin válido; esto es 'los bines que ya usamos', no 'todos los posibles').
    Usado para poblar el dropdown de bines disponibles al recibir/eliminar."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "SELECT DISTINCT bin FROM seller_flex_stock WHERE node = ? AND bin != '' ORDER BY bin",
            (node,),
        )
        return [r[0] for r in await cur.fetchall()]


async def get_seller_flex_stock_for_skus(skus: list[str]) -> dict:
    """Lee seller_flex_stock para un set de SKUs -- SELECT puro. Retorna
    {sku: [{node, warehouse, sellable_qty, bound_qty, synced_at}, ...]}
    porque el mismo SKU puede existir en más de un nodo/almacén."""
    if not skus:
        return {}
    out: dict = {}
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        uniq = list(set(skus))
        for i in range(0, len(uniq), 500):
            chunk = uniq[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"""SELECT node, warehouse, sku, sellable_qty, bound_qty, synced_at, bin
                    FROM seller_flex_stock WHERE sku IN ({placeholders})""",
                chunk,
            )
            for r in await cur.fetchall():
                out.setdefault(r["sku"], []).append(dict(r))
    return out


async def get_seller_flex_stock_for_base_skus(base_skus: list[str]) -> dict:
    """Agrega seller_flex_stock por base_sku (primeros 10 chars del SKU
    Amazon, mismo formato SKU BM que usa el resto de la app) -- para vistas
    que trabajan a nivel BM SKU (Cobertura, Riesgo Sobreventa) en vez de SKU
    Amazon exacto. Retorna {base_sku: {total_sellable, total_bound,
    by_warehouse: {MTY: n, CDMX: n, TJ: n}}}."""
    if not base_skus:
        return {}
    out: dict = {}
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        uniq = list(set(b for b in base_skus if b))
        for i in range(0, len(uniq), 400):
            chunk = uniq[i:i + 400]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"""SELECT substr(sku, 1, 10) AS base_sku, warehouse,
                           SUM(sellable_qty) AS sellable, SUM(bound_qty) AS bound
                    FROM seller_flex_stock
                    WHERE substr(sku, 1, 10) IN ({placeholders})
                    GROUP BY substr(sku, 1, 10), warehouse""",
                chunk,
            )
            for r in await cur.fetchall():
                entry = out.setdefault(r["base_sku"], {"total_sellable": 0, "total_bound": 0, "by_warehouse": {}})
                entry["total_sellable"] += r["sellable"] or 0
                entry["total_bound"] += r["bound"] or 0
                wh = r["warehouse"] or "?"
                entry["by_warehouse"][wh] = entry["by_warehouse"].get(wh, 0) + (r["sellable"] or 0)
    return out


async def get_bm_master_rows_for_skus(skus: list[str]) -> dict:
    """Lee bm_sku_master para un set de SKUs normalizados -- SELECT puro, sin
    llamadas a BM. Usado por el nuevo camino de alertas (Fase C) para hacer
    el JOIN listings-vs-maestro en vez del merge en vivo que usa el pipeline
    viejo. Retorna {sku: {available_qty, reserve_qty, total_qty, mty_qty,
    cdmx_qty, tj_qty, no_vendible_qty, verified, stock_updated_at, retail_ph,
    cost_usd, title, brand, model, category, upc, image_url}}. retail_ph/
    cost_usd agregados 2026-08-20 (directiva de Jovan de consolidar TODO lo
    que hoy llama a BM fuera del loop de categorías -- ver _fetch_base en
    amazon_products.py). title/brand/model/category/upc/image_url agregados
    el mismo día (2da vuelta) para que el gap scan de Amazon también pueda
    leer de aquí sin llamar a BM."""
    if not skus:
        return {}
    out = {}
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        uniq = list(set(skus))
        for i in range(0, len(uniq), 500):
            chunk = uniq[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"""SELECT sku, available_qty, reserve_qty, total_qty, mty_qty, cdmx_qty,
                           tj_qty, no_vendible_qty, verified, stock_updated_at, retail_ph, cost_usd,
                           title, brand, model, category, upc, image_url
                    FROM bm_sku_master WHERE sku IN ({placeholders})""",
                chunk,
            )
            for r in await cur.fetchall():
                out[r["sku"]] = dict(r)
    return out


async def get_bm_master_all_as_bulk_rows(min_qty: int = 1) -> list[dict]:
    """FEATURE 2026-08-20 (directiva de Jovan: 'todo debe apuntar a nuestro
    maestro, nada a BM por el momento') -- reemplaza cualquier llamada en
    vivo a bm_cli.get_bulk_stock() SIN category_id (catálogo completo) por
    una lectura pura de bm_sku_master, ya mantenido fresco por el loop de
    categorías (_update_bm_master_for_category, main.py).

    Devuelve las filas con las MISMAS claves que el BM bulk viejo (SKU,
    AvailableQTY, Reserve, TotalQty, Title, Brand, Model, CategoryName,
    ImageURL, UPC, LastRetailPricePurchaseHistory) para que los consumidores
    existentes (amazon_lanzar.py gap scan, amazon_products.py /sin-bm,
    stock_sync_multi._fetch_bm_avail) no necesiten reescribir su lógica de
    parseo, solo cambiar de dónde viene la lista.

    Usa best_condition_sku como 'SKU' (con sufijo de condición real) cuando
    existe -- igual que antes, para que la inyección de sustitutos en BM
    siga teniendo un ProductSKU válido; si no hay condición identificada
    (SKU agregado sin sufijo), usa el SKU base tal cual.

    min_qty: solo SKUs con available_qty >= este valor (default 1 -- mismo
    criterio 'avail_qty <= 0: continuar' que ya aplicaban los consumidores)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT sku, best_condition_sku, available_qty, reserve_qty, total_qty,
                      title, brand, model, category, upc, image_url, retail_ph,
                      cost_usd, size
               FROM bm_sku_master WHERE available_qty >= ?""",
            (min_qty,),
        )
        rows = await cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "SKU": r["best_condition_sku"] or r["sku"],
            "AvailableQTY": r["available_qty"],
            "Reserve": r["reserve_qty"],
            "TotalQty": r["total_qty"],
            "Title": r["title"],
            "Description": r["title"],
            "Brand": r["brand"],
            "Model": r["model"],
            "CategoryName": r["category"],
            "ImageURL": r["image_url"],
            "UPC": r["upc"],
            "LastRetailPricePurchaseHistory": r["retail_ph"],
            "RetailPrice": r["retail_ph"],
            "AvgCostQTY": r["cost_usd"],
            "Size": r["size"],
        })
    return out


async def get_pnp_data_for_skus(skus: list[str]) -> dict[str, dict]:
    """FEATURE 2026-08-21 (pedido explícito de Jovan): datos PNP ("Plug and
    Play", solo procesado en MTY) para la tabla de Cobertura -- SELECT puro
    sobre bm_sku_master, sin llamar a BM. Solo tiene datos reales para
    category="Televisions" (único caso confirmado hoy); para el resto de
    categorías estas columnas quedan en 0.

    Retorna {sku: {pnp_mty_available, pnp_mty_novendible, pnp_other_locations_qty}}
    -- los mismos AvailableQTY/NoVendibleQty que BM muestra en su propia UI
    (confirmado con captura real de Jovan)."""
    if not skus:
        return {}
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(skus))
        cur = await db.execute(
            f"""SELECT sku, pnp_mty_available, pnp_mty_novendible, pnp_other_locations_qty
                FROM bm_sku_master WHERE sku IN ({placeholders})""",
            skus,
        )
        rows = await cur.fetchall()
    return {
        r["sku"]: {
            "pnp_mty_available": r["pnp_mty_available"],
            "pnp_mty_novendible": r["pnp_mty_novendible"],
            "pnp_other_locations_qty": r["pnp_other_locations_qty"],
        }
        for r in rows
    }


async def get_tj_only_transfer_candidates() -> list[dict]:
    """FEATURE 2026-08-21 (pedido explícito de Jovan: "Transferencias
    Sugeridas Entre Almacenes" debe mostrar un indicador claro de qué
    productos tienen stock en Tijuana y CERO stock vendible en CDMX/MTY --
    para disparar un requerimiento de envío a almacén lo antes posible).

    tj_qty se alimenta desde 2026-08-21 en _update_bm_master_for_category
    (main.py) -- segunda llamada por categoría a Get_GlobalStock_InventoryBySKU
    con LOCATIONID=45,69,43,42 (Tijuana). available_qty ya es CDMX+Cuautitlán+MTY
    combinado (el vendible real, LOCATIONID=47,62,68) -- por eso "sin stock
    vendible" es simplemente available_qty=0, sin necesitar desglose MTY vs
    CDMX (Apantallate decide el almacén destino, no BM).

    AMPLIACIÓN 2026-08-21 #2 (Jovan: "ponme las ventas para saber a qué le
    damos prioridad" -- recomendación de planning-specialist aplicada tal
    cual): agrega units_12m (ventas reales de los últimos 365 días,
    ML+Amazon combinado vía order_history) y un badge de prioridad. Ventana
    de 12 meses (no 90d/lifetime) porque estos SKUs llevan tiempo en 0
    vendible -- una ventana corta mediría "cuánto stock tuvimos", no
    demanda real, y penalizaría injustamente a un SKU que vendía bien antes
    de quedarse sin stock. Orden: badge (alta→media→baja→sin historial),
    dentro de cada badge por units_12m descendente, empate por
    tj_qty*retail_ph descendente (valor total desbloqueado)."""
    from datetime import datetime as _dt_tj, timedelta as _td_tj
    _cutoff = (_dt_tj.utcnow() - _td_tj(days=365)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT bsm.sku, bsm.title, bsm.brand, bsm.model, bsm.category,
                      bsm.tj_qty, bsm.retail_ph, COALESCE(s.units_12m, 0) AS units_12m,
                      s.last_sale_date
               FROM bm_sku_master bsm
               LEFT JOIN (
                   SELECT sku, SUM(quantity) AS units_12m, MAX(order_date) AS last_sale_date
                   FROM order_history
                   WHERE order_date >= ?
                     AND LOWER(status) NOT IN ('cancelled', 'payment_required', 'payment_in_process', 'pending')
                   GROUP BY sku
               ) s ON s.sku = bsm.sku
               WHERE bsm.tj_qty > 0 AND bsm.available_qty = 0""",
            (_cutoff,),
        )
        rows = await cur.fetchall()

    def _badge(units: int) -> str:
        if units >= 50:
            return "alta"
        if units >= 10:
            return "media"
        if units >= 1:
            return "baja"
        return "sin_historial"

    _badge_rank = {"alta": 0, "media": 1, "baja": 2, "sin_historial": 3}
    items = [
        {
            "sku": r["sku"], "title": r["title"], "brand": r["brand"],
            "model": r["model"], "category": r["category"],
            "tj_qty": r["tj_qty"], "retail_ph": r["retail_ph"],
            "units_12m": r["units_12m"], "sales_badge": _badge(r["units_12m"]),
            "last_sale_date": r["last_sale_date"],
        }
        for r in rows
    ]
    items.sort(key=lambda i: (
        _badge_rank[i["sales_badge"]], -i["units_12m"], -(i["tj_qty"] * (i["retail_ph"] or 0)),
    ))
    return items


async def get_categories_ordered_by_sales(days: int = 90) -> list[dict]:
    """FEATURE 2026-08-19 (pedido por Jovan, plan de migración a
    ConfColumns_Conditions_Excel por categoría): ordena las categorías BM
    (bm_sku_master.category, ya se completa desde el gap scan diario) por
    ingresos reales de order_history en los últimos N días -- la prioridad
    real para refrescar primero. SELECT puro, cero llamadas externas.

    Categorías con category='' (SKUs conocidos que aún no pasaron por el
    gap scan, o sin stock en el último scan y por eso nunca enriquecidos)
    quedan en su propio renglón 'sin categoría' -- no se descartan, solo no
    se pueden priorizar por categoría todavía."""
    date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT COALESCE(bsm.category, '') AS category,
                   SUM(oh.unit_price * oh.quantity) AS revenue_mxn,
                   SUM(oh.quantity) AS units,
                   COUNT(DISTINCT bsm.sku) AS skus_with_sales
            FROM order_history oh
            LEFT JOIN bm_sku_master bsm ON bsm.sku = oh.sku
            WHERE oh.order_date >= ?
              AND oh.sku != ''
              AND LOWER(oh.status) NOT IN ('cancelled', 'payment_required', 'payment_in_process', 'pending')
            GROUP BY category
            ORDER BY revenue_mxn DESC
        """, (date_from,))
        rows = [dict(r) for r in await cur.fetchall()]
    return rows


async def get_all_known_categories() -> list[str]:
    """Universo COMPLETO de categorías (bm_sku_master.category) para los
    SKUs conocidos (ml_listings+amazon_listings) -- a diferencia de
    get_categories_ordered_by_sales(), NO depende de que haya vendido algo
    en los últimos N días. FIX 2026-08-19: _conf_columns_longtail_loop
    usaba solo categorías con venta en 90d para su 'resto', dejando
    huérfanas (nunca refrescadas) categorías de rotación nula -- este es
    el universo real que debe cubrir para que ningún SKU conocido quede
    fuera de los 2 loops."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("""
            SELECT DISTINCT bsm.category
            FROM bm_sku_master bsm
            WHERE bsm.category != ''
              AND bsm.sku IN (
                  SELECT DISTINCT base_sku FROM ml_listings
                  WHERE base_sku != '' AND status IN ('active', 'paused', 'inactive')
                  UNION
                  SELECT DISTINCT base_sku FROM amazon_listings WHERE base_sku != ''
              )
        """)
        return [r[0] for r in await cur.fetchall() if r[0]]


async def get_bm_stock_snapshot_last_update() -> float:
    """Timestamp de la foto de stock más reciente en disco, o 0 si nunca se ha tomado."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        async with db.execute("SELECT MAX(stock_updated_at) FROM bm_sku_master") as cur:
            row = await cur.fetchone()
    val = row[0] if row else None
    return float(val) if val else 0.0


async def get_bm_sku_changes(days: int = 7, field: str = "", sku: str = "", limit: int = 200) -> list[dict]:
    """Historial de cambios detectados (bm_sku_changes) — base para alertas
    tipo 'este SKU bajó de precio' / 'se quedó sin stock' / 'se resurtió'."""
    import time as _t
    cutoff = _t.time() - days * 86400
    query = "SELECT sku, field, old_value, new_value, changed_at, source FROM bm_sku_changes WHERE changed_at >= ?"
    params = [cutoff]
    if field:
        query += " AND field = ?"
        params.append(field)
    if sku:
        query += " AND sku = ?"
        params.append(sku)
    query += " ORDER BY changed_at DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = [dict(r) for r in await cur.fetchall()]
    return rows


async def get_orders_without_stock(days: int = 14) -> dict:
    """Cruza order_history (ventana reciente, paid/delivered, las 7 cuentas)
    contra bm_sku_master (maestro con stock actual de BM) para detectar
    SKUs vendidos que hoy tienen AvailableQTY <= 0 — o que no aparecen en el
    maestro en absoluto (sin dato, no se asume que sí hay stock).
    Agrupado por SKU, ordenado por unidades vendidas en riesgo (desc).
    """
    from datetime import datetime, timedelta as _td
    cutoff = (datetime.utcnow() - _td(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
                oh.sku AS sku,
                COALESCE(bsm.title, '') AS titulo,
                GROUP_CONCAT(DISTINCT oh.platform || ':' || oh.account_id) AS cuentas,
                COUNT(DISTINCT oh.order_id) AS ordenes,
                SUM(oh.quantity) AS unidades_vendidas,
                bsm.available_qty AS available_qty,
                bsm.reserve_qty AS reserve_qty,
                bsm.stock_updated_at AS stock_updated_at
            FROM order_history oh
            LEFT JOIN bm_sku_master bsm ON bsm.sku = oh.sku
            WHERE oh.order_date >= ? AND oh.sku != ''
            GROUP BY oh.sku
            HAVING bsm.sku IS NULL OR bsm.available_qty <= 0
            ORDER BY unidades_vendidas DESC
        """, (cutoff,))
        rows = [dict(r) for r in await cur.fetchall()]
    last_snapshot = await get_bm_stock_snapshot_last_update()
    return {"days": days, "cutoff": cutoff, "rows": rows, "stock_snapshot_updated_at": last_snapshot}


async def get_recent_paid_ml_orders(hours: int = 24) -> list[dict]:
    """Órdenes ML pagadas recientes (order_id, account_id), para el barrido
    periódico de _realtime_stock_reconcile_loop() -- red de seguridad que
    re-evalúa órdenes que el webhook ya procesó una vez pero nunca vuelve a
    tocar si ML no reenvía otra notificación de esa misma orden (ver
    DEVLOG 2026-08-14). Usa order_history en vez de volver a pedirle a ML
    la lista completa -- ya la tenemos guardada del webhook original."""
    import time as _t
    cutoff = _t.time() - hours * 3600
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT DISTINCT order_id, account_id
            FROM order_history
            WHERE platform = 'ml' AND status = 'paid' AND created_at >= ?
        """, (cutoff,))
        return [dict(r) for r in await cur.fetchall()]


async def record_realtime_stock_alert(
    order_id: str, item_id: str, platform: str, account_id: str,
    sku: str, quantity: int, available_qty: int | None, order_date: str,
    shipping_id: str = "", sku_raw: str = "",
) -> None:
    """Registra una orden individual detectada sin stock al momento (vía
    webhook). Idempotente — UNIQUE(order_id, sku, platform) absorbe reenvíos
    duplicados de la notificación sin generar 2 alertas. shipping_id se
    guarda para que el loop de reconciliación pueda revisar el estado real
    del envío sin tener que re-resolver la orden completa. sku_raw: el
    seller_sku/seller_custom_field CRUDO de ML (antes de normalize_to_bm_sku)
    -- necesario para resolver el ProductSKU real en BM cuando hay bundle o
    condición en el SKU real (ver FIX 2026-08-18, bug confirmado por Jovan)."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("""
            INSERT OR IGNORE INTO realtime_stock_alerts
                (order_id, item_id, platform, account_id, sku, quantity,
                 available_qty_at_check, order_date, detected_at, shipping_id, sku_raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (order_id, item_id, platform, account_id, sku, quantity,
              available_qty, order_date, _t.time(), shipping_id, sku_raw))
        await db.commit()


async def delete_realtime_stock_alerts_for_order(order_id: str, platform: str = "ml") -> int:
    """Elimina alertas de una orden que ya no es accionable (se envió, se
    entregó, se canceló, o resultó ser FULL) — se llama cuando el webhook
    recibe una actualización posterior de esa misma orden. Retorna filas
    borradas."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "DELETE FROM realtime_stock_alerts WHERE order_id = ? AND platform = ?",
            (order_id, platform),
        )
        await db.commit()
        return cur.rowcount


async def delete_realtime_stock_alerts_for_order_except_skus(
    order_id: str, platform: str, valid_skus: list[str],
) -> int:
    """Limpieza dirigida (2026-08-18, Amazon): borra SOLO las filas de esta
    orden cuyo SKU YA NO debería alertar (ej. resultó no ser catálogo BM)
    -- a diferencia de delete_realtime_stock_alerts_for_order() (borra
    TODAS), esto preserva detected_at de las filas que SÍ siguen siendo
    válidas en vez de re-insertarlas con timestamp nuevo cada ciclo."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        if valid_skus:
            placeholders = ",".join("?" * len(valid_skus))
            cur = await db.execute(
                f"DELETE FROM realtime_stock_alerts WHERE order_id = ? AND platform = ? AND sku NOT IN ({placeholders})",
                (order_id, platform, *valid_skus),
            )
        else:
            cur = await db.execute(
                "DELETE FROM realtime_stock_alerts WHERE order_id = ? AND platform = ?",
                (order_id, platform),
            )
        await db.commit()
        return cur.rowcount


async def delete_realtime_stock_alert_for_order_sku(order_id: str, platform: str, sku: str) -> int:
    """FIX 2026-08-19 (pedido por Jovan, urgente): _evaluate_order_stock_alert
    solo CREABA/ACTUALIZABA la alerta cuando avail<=0 -- nunca la borraba si
    el stock se corregía después (ej. el fix de hoy de bm_sku_master, que
    pasó decenas de SKUs de "0 falso" a stock real). Sin esto, una alerta
    quedaba viva para siempre aunque el SKU ya tuviera stock real. Borra
    SOLO la fila de este SKU en esta orden -- no toca otros SKUs de la
    misma orden que sí sigan sin stock real."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "DELETE FROM realtime_stock_alerts WHERE order_id = ? AND platform = ? AND sku = ?",
            (order_id, platform, sku),
        )
        await db.commit()
        return cur.rowcount


async def get_all_realtime_alerts_for_reconcile() -> list[dict]:
    """Todas las alertas activas — para el loop periódico que revisa el
    estado REAL de cada envío (no depende de que llegue una notificación
    nueva; ML no siempre reavisa en cada cambio de sub-estado)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, order_id, platform, account_id, sku, shipping_id FROM realtime_stock_alerts"
        )
        return [dict(r) for r in await cur.fetchall()]


async def delete_realtime_stock_alert_by_id(alert_id: int) -> None:
    """Borra una alerta puntual por su id (usado por el loop de reconciliación)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM realtime_stock_alerts WHERE id = ?", (alert_id,))
        await db.commit()


async def update_realtime_stock_alert_shipping_id(alert_id: int, shipping_id: str) -> None:
    """Backfill de shipping_id para alertas creadas antes de que existiera
    la columna — el loop de reconciliación lo completa la primera vez que
    revisa una fila vieja."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE realtime_stock_alerts SET shipping_id = ? WHERE id = ?",
            (shipping_id, alert_id),
        )
        await db.commit()


async def get_replacement_sku_suggestions(
    sku: str, brand: str, retail_ph: float, size: int = 0, limit: int = 3
) -> list[dict]:
    """Sugiere SKUs de reemplazo con stock disponible: misma marca + precio
    parecido (retail_ph) como filtro de candidatos plausibles, priorizados
    por MARGEN real dentro de ese grupo (FIX 2026-08-20, auditoría de
    alertas — antes solo ordenaba por cercanía de precio, ignorando por
    completo `cost_usd` aunque ya vive en la misma tabla; dos sustitutos al
    mismo precio pueden diferir mucho en margen y el sistema era indiferente
    a eso). Pura lectura de bm_sku_master (ya sincronizado) — sin llamadas
    nuevas a BM.

    Si el SKU original tiene "size" (tamaño de pantalla en pulgadas, campo
    real de BM — ej. TVs) > 0, la búsqueda EXIGE el mismo tamaño exacto —
    nunca sugiere un tamaño distinto aunque el precio sea parecido, para no
    generar una queja de cliente por mandar un reemplazo del tamaño
    equivocado. Si no hay tamaño (producto sin pantalla) cae a marca+precio.
    """
    if not brand or not retail_ph:
        return []
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        size_clause = "AND size = ?" if size and size > 0 else ""
        # Ventana de candidatos plausibles por precio (más amplia que `limit`
        # final) -- el reordenamiento por margen ocurre DENTRO de este grupo,
        # nunca sugiere un sustituto a un precio descabellado solo porque
        # tuviera mejor margen.
        _candidate_n = max(limit * 4, 8)
        params = [retail_ph, brand, sku] + ([size] if size and size > 0 else []) + [_candidate_n]
        cur = await db.execute(f"""
            SELECT sku, title, model, retail_ph, cost_usd, available_qty, size,
                   best_condition_sku, best_condition_qty,
                   ABS(retail_ph - ?) AS price_diff
            FROM bm_sku_master
            WHERE brand = ? AND sku != ? AND available_qty > 0 AND retail_ph > 0 {size_clause}
            ORDER BY price_diff ASC
            LIMIT ?
        """, params)
        rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            _cost = row.get("cost_usd") or 0
            _rph = row.get("retail_ph") or 0
            # margin_pct=None (no cost_usd confiable) cae al final, ordenado
            # por cercanía de precio entre sí -- nunca se inventa un margen.
            row["_margin_pct"] = ((_rph - _cost) / _rph) if (_cost > 0 and _rph > 0) else None
        rows.sort(key=lambda r: (
            r["_margin_pct"] is None,       # False (tiene margen real) ordena antes que True
            -(r["_margin_pct"] or 0),       # mayor margen primero
            r["price_diff"],                # tie-break / fallback: precio más cercano
        ))
        rows = rows[:limit]
        for row in rows:
            row.pop("_margin_pct", None)
            row.pop("cost_usd", None)
    # FEATURE 2026-08-17 (pedido por Jovan): available_qty es la suma de
    # TODAS las condiciones -- no dice en cuál hay stock real. Si se pudo
    # resolver la condición ganadora (ver _bm_master_sync_once_inner), se
    # usa ESE sku (con sufijo -GRB/-GRC/-GRA/-NEW) y SU cantidad específica
    # en vez del agregado -- para no sugerir "disp. 3" cuando en realidad
    # son 3 condiciones distintas con 1 unidad cada una.
    for row in rows:
        best_sku = row.pop("best_condition_sku", "") or ""
        best_qty = row.pop("best_condition_qty", 0) or 0
        if best_sku:
            row["sku"] = best_sku
            row["available_qty"] = best_qty
        # FIX 2026-08-18 (título sucio de BM, ver clean_bm_title): brand
        # viene como parámetro de la función (ya filtra por marca), model
        # viene en el propio row.
        row["title"] = clean_bm_title(row.get("title", ""), brand, row.get("model", ""))
    return rows


async def get_realtime_stock_alert_raw_sku(order_id: str, platform: str, sku: str) -> str:
    """Devuelve el sku_raw (crudo, tal como lo manda ML) guardado para esta
    alerta -- usado al registrar una sustitución para inyectar en BM con el
    WebSKU real en vez del normalizado (ver FIX 2026-08-18). '' si no se
    encuentra (alertas viejas de antes de este fix, o platform sin este
    feed) -- el caller debe usar el sku normalizado como fallback."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "SELECT sku_raw FROM realtime_stock_alerts WHERE order_id = ? AND platform = ? AND sku = ?",
            (order_id, platform, sku),
        )
        row = await cur.fetchone()
        return (row[0] if row else "") or ""


async def get_realtime_stock_alerts(limit: int = 100) -> list[dict]:
    """Feed cronológico (más reciente primero) de órdenes individuales
    detectadas sin stock al momento — reemplaza la vista agregada por SKU.
    Cada fila incluye sugerencias de reemplazo (misma marca, precio parecido,
    con stock).

    FIX 2026-08-18 (reportado por Jovan): una orden ya sustituida (viviendo
    en "Pendientes de Envío") seguía apareciendo aquí pidiendo "Sustituir"
    otra vez -- confuso, parecía que no se había hecho nada. Se excluye
    cualquier orden con una sustitución activa (aplicándose o ya aplicada
    y pendiente de envío/completada) -- vuelve a aparecer aquí solo si se
    "reabre" explícitamente (sustituto sin stock) o si la inyección a BM
    falló de verdad (necesita una decisión nueva).

    FEATURE 2026-08-21 (pedido explícito de Jovan): agrega reserve_qty
    (actual, de bm_sku_master -- no el snapshot congelado de
    available_qty_at_check) junto al disponible. Cuando BM recibe una
    orden reserva la unidad y available_qty baja a 0 para no sobrevender
    -- eso es correcto del lado de BM, pero esta vista lo marcaba igual
    que "sin stock real" y ofrecía sustituto sin distinguir "0 porque no
    hay nada" de "0 porque ya está reservado". Mostrar reserve_qty le da
    a quien revisa la alerta el contexto real antes de sustituir -- no se
    cambia la lógica de sugerencia/alerta automáticamente, solo se expone
    el número (mismo criterio que "Solo en Tijuana"/PNP: mostrar el dato
    crudo, la persona decide)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
                rsa.order_id, rsa.platform, rsa.account_id, rsa.sku,
                COALESCE(bsm.title, '') AS titulo,
                COALESCE(bsm.brand, '') AS brand,
                COALESCE(bsm.model, '') AS model,
                COALESCE(bsm.retail_ph, 0) AS retail_ph,
                COALESCE(bsm.size, 0) AS size,
                COALESCE(bsm.reserve_qty, 0) AS reserve_qty,
                rsa.quantity, rsa.available_qty_at_check, rsa.order_date, rsa.detected_at
            FROM realtime_stock_alerts rsa
            LEFT JOIN bm_sku_master bsm ON bsm.sku = rsa.sku
            WHERE NOT EXISTS (
                SELECT 1 FROM stock_alert_resolutions sar
                WHERE sar.order_id = rsa.order_id
                  AND sar.resolution_type = 'substitution'
                  AND (
                      sar.fulfillment_status IN ('pendiente_envio', 'completado')
                      OR (sar.fulfillment_status = '' AND sar.bm_status IN ('pending', 'success'))
                  )
            )
            ORDER BY rsa.detected_at DESC
            LIMIT ?
        """, (limit,))
        rows = [dict(r) for r in await cur.fetchall()]

    for row in rows:
        _retail_ph = row["retail_ph"]
        _brand = row.pop("brand")
        _model = row.pop("model")
        # FIX 2026-08-18 (título sucio de BM, ver clean_bm_title): limpia la
        # duplicación "Marca Modelo Marca+Modelo..." antes de mandarlo al
        # frontend (Alertas de Stock ML + Amazon comparten este feed).
        row["titulo"] = clean_bm_title(row["titulo"], _brand, _model)
        row["sugerencias"] = await get_replacement_sku_suggestions(
            row["sku"], _brand, _retail_ph, row.pop("size"), limit=3
        )
        # Se mantiene bajo otro nombre (no se borra) — Jovan quiere comparar
        # el precio del original contra el de la sugerencia en la misma vista.
        row["retail_ph"] = _retail_ph
    return rows


# ─── stock_alert_resolutions helpers ─────────────────────────────────────────

async def record_stock_alert_resolution(
    order_id: str, platform: str, account_id: str, original_sku: str,
    resolution_type: str, substitute_sku: str, note: str,
    username: str, user_id: int | None,
    bm_status: str = "", bm_message: str = "", original_sku_raw: str = "",
) -> int:
    """Registra cómo se resolvió una orden sin stock — sustitución de
    producto o stock puesto en 0. resolution_type: 'substitution' |
    'zeroed_stock'.

    FEATURE 2026-08-17: bm_status ('success'/'failed'/'') y bm_message
    (el MessageReturn crudo de BM) — para que el historial muestre si la
    sustitución de verdad se aplicó en BinManager, no solo que se registró
    aquí. original_sku_raw (FIX 2026-08-18): el seller_sku CRUDO de ML,
    necesario para resolver el ProductSKU real en BM con el WebSKU correcto."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("""
            INSERT INTO stock_alert_resolutions
                (order_id, platform, account_id, original_sku, resolution_type,
                 substitute_sku, note, username, user_id, ts, bm_status, bm_message, original_sku_raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (order_id, platform, account_id, original_sku, resolution_type,
              substitute_sku, note, username, user_id, _t.time(), bm_status, bm_message, original_sku_raw))
        await db.commit()
        return cur.lastrowid


async def get_stock_alert_resolutions(limit: int = 50, closed_only: bool = False) -> list[dict]:
    """Todas las resoluciones, más reciente primero. Usada tanto por la
    pestaña 'Historial' como por reopen/retry-bm/delete-from-bm para
    buscar una resolución puntual por id -- por eso `closed_only` es
    opt-in (default False, comportamiento sin cambios para esos 3
    callers que necesitan encontrar CUALQUIER fila, no solo las cerradas).

    FIX 2026-08-19: closed_only=True excluye lo que sigue en proceso
    ("historial es solamente cuando algo ya fue confirmado y enviado",
    Jovan) -- ver también partición de 3 vías más abajo.

    FIX 2026-08-19 #2 (mismo día, corrección tras ver el resultado en
    vivo): un intento de sustitución que YA falló contra BM (bm_status=
    'failed') o que sigue sin resolverse (bm_status='pending') no es
    "pendiente de envío" -- ya tiene un dictamen (falló) o simplemente no
    se sabe qué pasó, ninguno de los dos es "todo funcionó bien esperando
    enviarse". Jovan: "si marca un error... no debería pasar a ningún
    lado". Partición real de 3 vías para 'substitution' (nunca en más de
    una, nunca en ninguna de las 2 vistas para pending/failed -- el
    pedido en sí sigue vivo en 'En vivo' para reintentarlo desde cero):
      - bm_status='success' AND fulfillment_status IN ('','pendiente_envio')
        -> Pendientes de Envío (get_pending_shipment_resolutions)
      - bm_status='success' AND fulfillment_status IN ('completado','cancelada','reabierta'),
        o resolution_type != 'substitution', o bm_deleted_at IS NOT NULL,
        o bm_status='' (nota de Amazon, sin tracking async)
        -> Historial (closed_only=True, aquí)
      - bm_status IN ('pending','failed') -> en NINGUNA de las 2 vistas."""
    closed_sql = ""
    if closed_only:
        closed_sql = """
            AND (
                bm_deleted_at IS NOT NULL
                OR resolution_type != 'substitution'
                OR bm_status = ''
                OR (bm_status = 'success' AND fulfillment_status IN ('completado', 'cancelada', 'reabierta'))
            )
        """
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"""
            SELECT id, order_id, platform, account_id, original_sku,
                   resolution_type, substitute_sku, note, username, ts,
                   reactivated_at, reactivated_by, bm_status, bm_message,
                   bm_deleted_at, bm_deleted_by, fulfillment_status,
                   last_stock_check_at, last_stock_check_qty, shipment_resolved_at,
                   original_sku_raw
            FROM stock_alert_resolutions
            WHERE 1=1 {closed_sql}
            ORDER BY ts DESC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def update_stock_alert_resolution_bm_status(resolution_id: int, bm_status: str, bm_message: str) -> None:
    """Actualiza bm_status/bm_message de un registro ya guardado -- usado por
    el background task de _inject_bm_alter_sku (FIX 2026-08-17: la inyección
    a BM ya no bloquea el request original, ver resolve_stock_alert_substitution).

    Reintentos: este write corre en background sin nadie esperándolo -- si
    choca con "database is locked" (contención real observada en pruebas,
    otros writers del proceso ya la sufren) y no se reintenta, el registro se
    queda en bm_status='pending' PARA SIEMPRE sin ningún error visible, el
    mismo tipo de silencio que este fix completo buscaba eliminar."""
    # Verificado en pruebas locales: con ráfagas reales de contención (varios
    # syncs completos de cuenta corriendo a la vez al arrancar) 4 intentos con
    # timeout=15 no bastaron -- el write seguía chocando con "database is
    # locked" más de 2 minutos después. Como nadie espera este resultado
    # (corre en background), es preferible esperar más de la cuenta a
    # rendirse rápido: timeout largo por intento + más intentos.
    # FEATURE 2026-08-17: en cuanto bm_status pasa a 'success', arranca el
    # seguimiento de "¿de verdad se envió?" (fulfillment_status) -- ver
    # _substitution_fulfillment_loop. Guard con fulfillment_status='' para
    # no pisar un estado terminal (completado/cancelada) si esto se
    # re-dispara después por alguna razón.
    import asyncio as _asyncio
    last_err: Exception | None = None
    for attempt in range(6):
        try:
            async with aiosqlite.connect(DATABASE_PATH, timeout=45) as db:
                await db.execute(
                    """UPDATE stock_alert_resolutions SET bm_status = ?, bm_message = ?,
                           fulfillment_status = CASE
                               WHEN ? = 'success' AND fulfillment_status = '' THEN 'pendiente_envio'
                               ELSE fulfillment_status
                           END
                       WHERE id = ?""",
                    (bm_status, bm_message, bm_status, resolution_id),
                )
                await db.commit()
                return
        except Exception as e:
            last_err = e
            await _asyncio.sleep(5 * (attempt + 1))
    logger.error(f"[BM-ALTER-SKU] no se pudo actualizar bm_status de resolution_id={resolution_id} tras reintentos: {last_err}")


async def get_pending_shipment_resolutions() -> list[dict]:
    """Sustituciones ya aplicadas en BM (bm_status='success') cuya orden
    todavía no se ha enviado -- ver _substitution_fulfillment_loop
    (main.py). fulfillment_status vacío cuenta como 'pendiente_envio'
    (filas viejas de antes de esta feature, o la primera vez que
    bm_status pasó a success sin haber corrido aún el loop).

    FIX 2026-08-19 #2 (corregido tras verlo en vivo en la UI -- Jovan:
    "si marca un error... no debería pasar a ningún lado"): hubo una
    versión intermedia el mismo día que amplió esto a bm_status IN
    ('pending','failed') -- se DESHIZO porque un intento ya fallido tiene
    su propio dictamen (no es "pendiente"), y uno sin resolver tampoco es
    "todo funcionó bien esperando enviarse". Esas filas ahora no
    aparecen en NINGUNA vista (ni aquí ni en Historial, ver
    get_stock_alert_resolutions(closed_only=True)) -- el pedido en sí
    sigue vivo en 'En vivo' para reintentarlo desde cero."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        # FEATURE 2026-08-18 (pedido por Jovan): la tarjeta solo mostraba
        # el número de orden a secas, sin decir QUÉ producto es -- se le
        # agrega el título real (mismo JOIN que usa "En vivo", ver
        # get_realtime_stock_alerts) tanto del SKU original como del
        # sustituto para poder identificar la orden de un vistazo.
        cur = await db.execute("""
            SELECT sar.id, sar.order_id, sar.platform, sar.account_id,
                   sar.original_sku, sar.substitute_sku,
                   sar.username, sar.ts, sar.fulfillment_status,
                   sar.bm_status, sar.bm_message, sar.note,
                   sar.last_stock_check_at, sar.last_stock_check_qty,
                   sar.original_sku_raw,
                   COALESCE(bsm_o.title, '') AS titulo,
                   COALESCE(bsm_o.brand, '') AS _o_brand,
                   COALESCE(bsm_o.model, '') AS _o_model,
                   COALESCE(bsm_s.title, '') AS substitute_titulo,
                   COALESCE(bsm_s.brand, '') AS _s_brand,
                   COALESCE(bsm_s.model, '') AS _s_model
            FROM stock_alert_resolutions sar
            LEFT JOIN bm_sku_master bsm_o ON bsm_o.sku = sar.original_sku
            LEFT JOIN bm_sku_master bsm_s ON bsm_s.sku = sar.substitute_sku
            WHERE sar.resolution_type = 'substitution'
              AND sar.bm_status = 'success'
              AND sar.fulfillment_status IN ('', 'pendiente_envio')
              AND sar.bm_deleted_at IS NULL
            ORDER BY sar.ts ASC
        """)
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        # FIX 2026-08-18 (título sucio de BM, ver clean_bm_title): limpia
        # tanto el título del SKU original como el del sustituto.
        row["titulo"] = clean_bm_title(row["titulo"], row.pop("_o_brand"), row.pop("_o_model"))
        row["substitute_titulo"] = clean_bm_title(row["substitute_titulo"], row.pop("_s_brand"), row.pop("_s_model"))
    return rows


async def mark_resolution_fulfillment(
    resolution_id: int, status: str, stock_qty: int | None = None, checked_at: float | None = None,
) -> None:
    """Actualiza el seguimiento de envío de una sustitución. status:
    'pendiente_envio' (sigue esperando, guarda el último chequeo de stock)
    | 'completado' (la orden ya se imprimió/envió) | 'cancelada' (la orden
    se canceló -- el caller ya debe haber borrado el mapeo de BM)."""
    import time as _t
    now = checked_at if checked_at is not None else _t.time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        if status == "pendiente_envio":
            await db.execute(
                """UPDATE stock_alert_resolutions
                   SET fulfillment_status = 'pendiente_envio', last_stock_check_at = ?, last_stock_check_qty = ?
                   WHERE id = ?""",
                (now, stock_qty, resolution_id),
            )
        else:
            await db.execute(
                """UPDATE stock_alert_resolutions
                   SET fulfillment_status = ?, last_stock_check_at = ?, last_stock_check_qty = ?, shipment_resolved_at = ?
                   WHERE id = ?""",
                (status, now, stock_qty, now, resolution_id),
            )
        await db.commit()


async def mark_stock_alert_resolution_bm_deleted(resolution_id: int, deleted_by: str) -> None:
    """Marca una resolución como borrada de BinManager desde nuestro
    historial (ver /api/stock/alerts/resolutions/{id}/delete-from-bm)."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE stock_alert_resolutions SET bm_deleted_at = ?, bm_deleted_by = ? WHERE id = ?",
            (_t.time(), deleted_by, resolution_id),
        )
        await db.commit()


async def get_pending_restock_watches() -> list[dict]:
    """SKUs que se pusieron en 0 por falta de stock y que YA tienen stock
    disponible de nuevo en BM (bm_sku_master, sincronizado periódicamente
    — no es llamada en vivo a BM) — para el aviso 'Ya hay stock, reactivar'.
    Solo el evento de zeroed_stock más reciente por SKU que no se haya
    marcado como reactivado.

    FIX 2026-08-14: si el listing de esa cuenta ya está activo con stock
    real (reactivado por otra vía — restock normal, edición manual en ML —
    sin pasar por el botón "Descartar" de aquí), se auto-marca como
    reactivado en vez de seguir mostrando un aviso obsoleto. Reportado por
    Jovan con evidencia real: SNTV004097 ya tenía 12 listings activos con
    stock real en las 4 cuentas y el aviso seguía apareciendo."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        # Auto-resolver primero: si el listing de esa cuenta ya tiene stock
        # real activo, ya no hace falta que un humano lo confirme a mano.
        await db.execute("""
            UPDATE stock_alert_resolutions
            SET reactivated_at = ?, reactivated_by = 'auto (listing ya activo)'
            WHERE resolution_type = 'zeroed_stock'
              AND reactivated_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM ml_listings ml
                  WHERE ml.base_sku = stock_alert_resolutions.original_sku
                    AND ml.account_id = stock_alert_resolutions.account_id
                    AND ml.status = 'active'
                    AND ml.available_qty > 0
              )
        """, (_t.time(),))
        await db.commit()

        cur = await db.execute("""
            SELECT sar.id, sar.original_sku, sar.order_id, sar.username, sar.ts,
                   sar.account_id,
                   COALESCE(bsm.title, '') AS titulo,
                   COALESCE(bsm.available_qty, 0) AS bm_available_qty
            FROM stock_alert_resolutions sar
            JOIN bm_sku_master bsm ON bsm.sku = sar.original_sku
            WHERE sar.resolution_type = 'zeroed_stock'
              AND sar.reactivated_at IS NULL
              AND bsm.available_qty > 0
              AND sar.id = (
                  SELECT MAX(id) FROM stock_alert_resolutions
                  WHERE original_sku = sar.original_sku
                    AND resolution_type = 'zeroed_stock'
              )
            ORDER BY sar.ts DESC
        """)
        return [dict(r) for r in await cur.fetchall()]


async def mark_resolution_reactivated(resolution_id: int, username: str) -> None:
    """Marca un aviso de reactivación como atendido — deja de aparecer en
    get_pending_restock_watches aunque BM siga con stock > 0."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE stock_alert_resolutions SET reactivated_at = ?, reactivated_by = ? WHERE id = ?",
            (_t.time(), username, resolution_id),
        )
        await db.commit()


# ─── amazon_buyer_messages helpers ───────────────────────────────────────────

async def get_buyer_inbox_watermark(seller_id: str) -> int:
    """Último UID de IMAP ya procesado para esta cuenta — 0 si nunca se ha
    corrido (primer poll real, cae al comportamiento viejo de escanear los
    últimos 200 por UID)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "SELECT last_uid FROM amazon_buyer_inbox_state WHERE seller_id = ?", (seller_id,),
        )
        row = await cur.fetchone()
    return row[0] if row else 0


async def set_buyer_inbox_watermark(seller_id: str, last_uid: int) -> None:
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT INTO amazon_buyer_inbox_state (seller_id, last_uid, last_poll_ts)
               VALUES (?, ?, ?)
               ON CONFLICT(seller_id) DO UPDATE SET last_uid=excluded.last_uid, last_poll_ts=excluded.last_poll_ts""",
            (seller_id, last_uid, _t.time()),
        )
        await db.commit()


async def insert_buyer_message(msg: dict) -> int | None:
    """Inserta un mensaje (inbound u outbound) parseado del buzón dedicado.
    INSERT OR IGNORE por message_id — el poller puede volver a ver el mismo
    correo en cada pasada sin duplicar filas. Retorna None si ya existía."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("""
            INSERT OR IGNORE INTO amazon_buyer_messages
                (seller_id, direction, order_id, asin, product_title, buyer_name,
                 subject, body_text, reply_to_addr, message_id, in_reply_to, ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            msg["seller_id"], msg.get("direction", "inbound"),
            msg.get("order_id", ""), msg.get("asin", ""), msg.get("product_title", ""),
            msg.get("buyer_name", ""), msg.get("subject", ""), msg.get("body_text", ""),
            msg.get("reply_to_addr", ""), msg.get("message_id", ""),
            msg.get("in_reply_to", ""), msg.get("ts", 0.0),
        ))
        await db.commit()
        return cur.lastrowid if cur.rowcount else None


async def backfill_buyer_message_product_title(seller_id: str, asin: str, product_title: str) -> None:
    """FEATURE 2026-08-16 (pedido por Jovan: preguntas de Amazon solo mostraban
    el ASIN, sin titulo del producto, cuando el correo de Amazon no traia esa
    linea en el formato exacto que espera el parser — ver
    buyer_messages_client.py _PRODUCT_LINE_RE). Cuando se resuelve el titulo
    real via Catalog Items API (SP-API), se guarda aqui de una vez para TODAS
    las filas de ese asin+seller que quedaron con product_title vacio — asi
    la proxima carga de la bandeja ya no necesita volver a consultar Amazon
    para el mismo ASIN."""
    if not asin or not product_title:
        return
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("""
            UPDATE amazon_buyer_messages SET product_title = ?
            WHERE seller_id = ? AND asin = ? AND (product_title IS NULL OR product_title = '')
        """, (product_title, seller_id, asin))
        await db.commit()


async def get_buyer_messages(seller_id: str, days: int = 30, limit: int = 50) -> list[dict]:
    """Mensajes (in+outbound) de una cuenta, más reciente primero, para la
    sección 'Mensajes de Compradores' de Salud y Retornos Amazon. limit
    acota el feed a lo reciente/accionable — el buzón dedicado puede ya
    traer años de historial (reenvío de Amazon activo desde antes de esta
    feature), no tiene caso cargar todo eso de una vez."""
    import time as _t
    cutoff = _t.time() - days * 86400
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, seller_id, direction, order_id, asin, product_title, buyer_name,
                   subject, body_text, reply_to_addr, message_id, in_reply_to,
                   ts, read_at, replied_by
            FROM amazon_buyer_messages
            WHERE seller_id = ? AND ts >= ?
            ORDER BY ts DESC
            LIMIT ?
        """, (seller_id, cutoff, limit))
        return [dict(r) for r in await cur.fetchall()]


async def mark_buyer_messages_read(message_ids: list[int]) -> None:
    """Marca varios mensajes como leídos en una sola transacción — evita el
    problema de disparar N updates individuales (contención en SQLite) al
    renderizar una lista con muchos mensajes sin leer de una vez."""
    if not message_ids:
        return
    import time as _t
    now = _t.time()
    placeholders = ",".join("?" * len(message_ids))
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            f"UPDATE amazon_buyer_messages SET read_at = ? WHERE id IN ({placeholders}) AND read_at IS NULL",
            (now, *message_ids),
        )
        await db.commit()


async def get_buyer_message(message_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM amazon_buyer_messages WHERE id = ?", (message_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


# ─── ml_message_views helpers ────────────────────────────────────────────────

async def register_message_view(pack_id: str, account_id: str, viewed_by: str) -> None:
    """Registra quién abrió primero un mensaje (INSERT OR IGNORE — no sobreescribe)."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT OR IGNORE INTO ml_message_views (pack_id, account_id, viewed_by, viewed_at, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (pack_id, account_id, viewed_by, _t.time()),
        )
        await db.commit()


async def take_message(pack_id: str, account_id: str, taken_by: str) -> None:
    """Asigna explícitamente un mensaje a un usuario (sobreescribe cualquier vista previa)."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT OR REPLACE INTO ml_message_views (pack_id, account_id, viewed_by, viewed_at, status)
               VALUES (?, ?, ?, ?, 'in_progress')""",
            (pack_id, account_id, taken_by, _t.time()),
        )
        await db.commit()


async def update_message_view_status(pack_id: str, account_id: str, status: str, viewed_by: str = "") -> None:
    """Actualiza (o crea) el estado de un mensaje: pending / in_progress / resolved.
    Refresca viewed_at para que refleje el último toque (usado por KPIs de
    'resuelto en las últimas 24h'), no solo la primera vez que se tomó.

    Antes era un UPDATE plano -- si nadie había 'tomado' la conversación
    todavía (sin fila en ml_message_views), marcar 'resuelto' no hacía NADA
    en silencio (0 filas afectadas, sin error). Encontrado 2026-08-06: Jovan
    pidió poder marcar como resuelta una conversación bloqueada por ML
    (mediación/orden cancelada) sin tener que tomarla primero. Ahora hace
    upsert -- crea la fila si no existía."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT INTO ml_message_views (pack_id, account_id, viewed_by, viewed_at, status)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(pack_id, account_id) DO UPDATE SET
                   status=excluded.status, viewed_at=excluded.viewed_at,
                   viewed_by=CASE WHEN excluded.viewed_by != '' THEN excluded.viewed_by ELSE ml_message_views.viewed_by END""",
            (pack_id, account_id, viewed_by or "sistema", _t.time(), status),
        )
        await db.commit()


async def get_message_views(pack_ids: list, account_id: str) -> dict:
    """Retorna dict {pack_id: {viewed_by, viewed_at, status, needs_followup,
    follow_up_note, followup_marked_at}} para los pack_ids dados."""
    if not pack_ids:
        return {}
    placeholders = ",".join("?" * len(pack_ids))
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"SELECT pack_id, viewed_by, viewed_at, status, needs_followup, follow_up_note, followup_marked_at "
            f"FROM ml_message_views WHERE pack_id IN ({placeholders}) AND account_id = ?",
            list(pack_ids) + [account_id],
        )).fetchall()
    return {r["pack_id"]: dict(r) for r in rows}


async def set_message_followup(pack_id: str, account_id: str, needs_followup: bool, note: str = "", marked_by: str = "") -> None:
    """Marca/desmarca un mensaje para 'Seguimiento' -- ya se respondió pero
    falta enviar algo después (guía, foto, dato que no se tenía a la mano).
    Ortogonal a `status`: no lo toca, no requiere que el mensaje esté
    tomado/resuelto de antemano (upsert, igual que update_message_view_status)."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT INTO ml_message_views (pack_id, account_id, viewed_by, viewed_at, status,
                                               needs_followup, follow_up_note, followup_marked_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
               ON CONFLICT(pack_id, account_id) DO UPDATE SET
                   needs_followup=excluded.needs_followup,
                   follow_up_note=excluded.follow_up_note,
                   followup_marked_at=excluded.followup_marked_at""",
            (pack_id, account_id, marked_by or "sistema", _t.time(),
             1 if needs_followup else 0, (note or "")[:300], _t.time()),
        )
        await db.commit()


async def bulk_mark_resolved(pack_ids: list, account_id: str, marked_by: str) -> int:
    """Marca varios hilos como resueltos de una sola vez — 'borrón y cuenta
    nueva' del historial acumulado (Jovan: Amazon no comparte respuestas
    dadas directo en Seller Central, así que el historial viejo no se puede
    saber con certeza qué ya se atendió; se limpia manualmente una vez y de
    ahí en adelante se usa Tomar/Resuelto normal)."""
    if not pack_ids:
        return 0
    import time as _t
    now = _t.time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO ml_message_views (pack_id, account_id, viewed_by, viewed_at, status)
               VALUES (?, ?, ?, ?, 'resolved')""",
            [(pid, account_id, marked_by, now) for pid in pack_ids],
        )
        await db.commit()
    return len(pack_ids)


# ─── ml_messages_index helpers — índice local, no escanea ML en vivo ─────────

async def upsert_message_index(pack_id: str, account_id: str, order_id: str,
                                last_message_from: str, last_message_text: str,
                                last_message_date: str, total_messages: int) -> None:
    """Actualiza (o crea) el índice local de una conversación — llamado por el
    webhook de topic 'messages', por el backfill/refresh de órdenes nuevas y
    por el refresh de conversaciones ya indexadas.

    order_id="" preserva el order_id ya guardado (COALESCE contra el valor
    existente en vez de sobreescribir con excluded.order_id). Necesario
    porque el webhook y el refresh de conversaciones ya indexadas NO siempre
    tienen a mano el order_id real de la orden — antes el webhook guardaba
    pack_id como si fuera order_id (bug encontrado 2026-08-05, DEVLOG), y
    mostraba un número de orden que en realidad era el pack_id."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT INTO ml_messages_index
               (pack_id, account_id, order_id, last_message_from, last_message_text, last_message_date, total_messages, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pack_id, account_id) DO UPDATE SET
                   order_id=CASE WHEN excluded.order_id != '' THEN excluded.order_id ELSE ml_messages_index.order_id END,
                   last_message_from=excluded.last_message_from,
                   last_message_text=excluded.last_message_text, last_message_date=excluded.last_message_date,
                   total_messages=excluded.total_messages, updated_at=excluded.updated_at""",
            (pack_id, account_id, order_id, last_message_from, last_message_text,
             last_message_date, total_messages, _t.time()),
        )
        await db.commit()


async def get_message_index(account_id: str, offset: int = 0, limit: int = 20,
                             date_from: str = "", date_to: str = "", q: str = "",
                             only_followup: bool = False) -> tuple:
    """Lista conversaciones indexadas de UNA cuenta. Pendientes reales primero
    (último mensaje del comprador y no resuelto), luego por fecha más reciente.
    Retorna (rows, total) — total antes de paginar, para 'X de Y'.

    Antes ordenaba solo por last_message_date DESC -- un mensaje sin responder
    de hace 2 semanas podía quedar enterrado más allá de la primera página
    (offset=0, limit=20) si hubo 20+ conversaciones más recientes desde
    entonces, aunque el KPI de pendientes (que no pagina) sí lo contara.
    Jovan reportó 2026-08-06 que el KPI marcaba pendientes reales que la
    lista nunca mostraba en ninguna página visible por default.

    q: búsqueda por pack_id/order_id/texto del último mensaje CONTRA TODO EL
    HISTÓRICO de la cuenta -- antes health_messages_partial() traía solo esta
    misma página (20-50 filas) y filtraba el texto DESPUÉS, así que una
    conversación vieja y ya respondida (fuera de esa página chica) nunca
    aparecía sin importar que el número de orden estuviera bien escrito.
    Jovan reportó 2026-08-11 con un caso real (orden de hace 25 días).

    El ORDER BY también compara last_message_date contra v.viewed_at (no solo
    status != 'resolved') -- caso real 2026-08-11: conversación marcada
    'resuelta' el 4 de agosto que el comprador reabrió el 10 (escribió de
    nuevo) se quedaba enterrada entre miles de filas más recientes porque
    status seguía siendo 'resolved' en la tabla; el badge (que sí hace este
    mismo chequeo de fecha en Python, ver _count_ml_pending_excluding_blocked)
    la contaba bien pero la lista nunca la mostraba en ninguna página.

    only_followup=True: ignora date_from/date_to/q/paginación -- devuelve
    TODAS las conversaciones marcadas needs_followup=1 de la cuenta
    (típicamente ya 'resolved', por eso no puede depender del ORDER BY de
    pendientes ni de un LIMIT chico, o una marca vieja se perdería fuera de
    la primera página igual que el bug de 2026-08-11 arriba)."""
    if only_followup:
        async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                """SELECT idx.pack_id, idx.order_id, idx.last_message_from, idx.last_message_text,
                          idx.last_message_date, idx.total_messages
                   FROM ml_message_views v
                   JOIN ml_messages_index idx
                       ON idx.pack_id = v.pack_id AND idx.account_id = v.account_id
                   WHERE v.account_id = ? AND v.needs_followup = 1
                   ORDER BY v.followup_marked_at DESC""",
                (account_id,),
            )).fetchall()
        rows = [dict(r) for r in rows]
        return rows, len(rows)
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        where = "WHERE idx.account_id = ?"
        params: list = [account_id]
        if date_from:
            where += " AND idx.last_message_date >= ?"
            params.append(date_from)
        if date_to:
            where += " AND idx.last_message_date <= ?"
            params.append(date_to + "T23:59:59")
        if q:
            where += " AND (idx.pack_id LIKE ? OR idx.order_id LIKE ? OR idx.last_message_text LIKE ?)"
            like_term = f"%{q}%"
            params.extend([like_term, like_term, like_term])
        cur = await db.execute(f"SELECT COUNT(*) AS n FROM ml_messages_index idx {where}", params)
        total = (await cur.fetchone())["n"]
        rows = await (await db.execute(
            f"""SELECT idx.pack_id, idx.order_id, idx.last_message_from, idx.last_message_text,
                       idx.last_message_date, idx.total_messages
                FROM ml_messages_index idx
                LEFT JOIN ml_message_views v
                    ON v.pack_id = idx.pack_id AND v.account_id = idx.account_id
                {where}
                ORDER BY
                    CASE WHEN idx.last_message_from = 'buyer' AND (
                             COALESCE(v.status, '') != 'resolved'
                             OR CAST(strftime('%s', substr(idx.last_message_date, 1, 19)) AS REAL) > COALESCE(v.viewed_at, 0)
                         ) THEN 0 ELSE 1 END,
                    idx.last_message_date DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )).fetchall()
    return [dict(r) for r in rows], total


async def get_all_pending_candidates(account_id: str) -> list:
    """Todas las conversaciones 'buyer + no resuelto/reabierto' de una cuenta,
    SIN el LIMIT de paginación de get_message_index() -- usado por el KPI real
    (_count_ml_pending_excluding_blocked) y por el diag de pendientes.

    Bug real 2026-08-11: ambos llamaban get_message_index(..., limit=1000).
    Mientras el histórico total era ~2,300 filas eso alcanzaba de sobra, pero
    tras el barrido automático de 180 días (PARTE 3) el histórico de
    BLOWTECHNOLOGIES creció a 9,436 -- de repente pudo haber más de 1,000
    conversaciones con last_message_from='buyer' (cualquier orden vieja que
    terminó con un "gracias" del comprador sin marcar resuelto entra en ese
    bucket), y las más viejas quedaban cortadas por el LIMIT antes de que la
    lógica de bloqueado/reabierto siquiera las viera. Caso real: Carlos
    Gerardo Meza (9 de agosto) desaparecía del conteo pese a estar indexado
    correctamente. Filtrar por last_message_from='buyer' en el WHERE (no
    como criterio de orden) reduce el universo real a un pequeño porcentaje
    del total -- no necesita paginación."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT idx.pack_id, idx.order_id, idx.last_message_from, idx.last_message_text,
                       idx.last_message_date, idx.total_messages
                FROM ml_messages_index idx
                LEFT JOIN ml_message_views v
                    ON v.pack_id = idx.pack_id AND v.account_id = idx.account_id
                WHERE idx.account_id = ? AND idx.last_message_from = 'buyer'
                  AND (
                      COALESCE(v.status, '') != 'resolved'
                      OR CAST(strftime('%s', substr(idx.last_message_date, 1, 19)) AS REAL) > COALESCE(v.viewed_at, 0)
                  )
                ORDER BY idx.last_message_date DESC""",
            (account_id,),
        )).fetchall()
    return [dict(r) for r in rows]


async def log_sent_message(pack_id: str, account_id: str, sent_by: str, text: str) -> None:
    """Registra quién envió un mensaje ML desde la app -- ver ml_message_sent_log
    arriba. Llamado por send_message (health.py) justo después de un envío
    exitoso. Best-effort: nunca debe tumbar el envío ya confirmado por ML."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "INSERT INTO ml_message_sent_log (pack_id, account_id, sent_by, sent_at, text) VALUES (?, ?, ?, ?, ?)",
            (pack_id, account_id, sent_by, _t.time(), text[:500]),
        )
        await db.commit()


async def get_sent_by_log(pack_ids: list, account_id: str) -> dict:
    """Retorna dict {pack_id: [{sent_by, sent_at, text}, ...]} para los
    pack_ids dados -- se cruzan por texto contra los mensajes del hilo en
    vivo (ver _fetch_enriched_ml_conversations, main.py) porque ML no expone
    ningún id de mensaje estable para hacer join directo."""
    if not pack_ids:
        return {}
    placeholders = ",".join("?" * len(pack_ids))
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"SELECT pack_id, sent_by, sent_at, text FROM ml_message_sent_log "
            f"WHERE pack_id IN ({placeholders}) AND account_id = ? ORDER BY sent_at",
            list(pack_ids) + [account_id],
        )).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["pack_id"], []).append(dict(r))
    return out



async def get_message_index_all_accounts(account_ids: list, date_from: str = "", date_to: str = "") -> list:
    """Todas las conversaciones indexadas de varias cuentas (bandeja unificada
    'Todas las cuentas') — una sola query, sin N llamadas a ML."""
    if not account_ids:
        return []
    placeholders = ",".join("?" * len(account_ids))
    where = f"WHERE account_id IN ({placeholders})"
    params: list = list(account_ids)
    if date_from:
        where += " AND last_message_date >= ?"
        params.append(date_from)
    if date_to:
        where += " AND last_message_date <= ?"
        params.append(date_to + "T23:59:59")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"""SELECT pack_id, account_id, order_id, last_message_from, last_message_text,
                       last_message_date, total_messages
                FROM ml_messages_index {where}
                ORDER BY last_message_date DESC""",
            params,
        )).fetchall()
    return [dict(r) for r in rows]


# ─── ml_claim_views helpers (reusa ml_message_views con prefijo "claim:") ─────

async def take_claim(claim_id: str, account_id: str, taken_by: str) -> None:
    """Asigna explícitamente un reclamo a un usuario (sobreescribe)."""
    import time as _t
    _key = f"claim:{claim_id}"
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT OR REPLACE INTO ml_message_views (pack_id, account_id, viewed_by, viewed_at, status)
               VALUES (?, ?, ?, ?, 'in_progress')""",
            (_key, account_id, taken_by, _t.time()),
        )
        await db.commit()


async def update_claim_view_status(claim_id: str, account_id: str, status: str) -> None:
    """Actualiza el estado interno de un reclamo: pending / in_progress / resolved."""
    _key = f"claim:{claim_id}"
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE ml_message_views SET status = ? WHERE pack_id = ? AND account_id = ?",
            (status, _key, account_id),
        )
        await db.commit()


async def get_claim_views(claim_ids: list, account_id: str) -> dict:
    """Retorna dict {claim_id: {viewed_by, viewed_at, status}} para los claim_ids dados."""
    if not claim_ids:
        return {}
    _keys = [f"claim:{cid}" for cid in claim_ids]
    placeholders = ",".join("?" * len(_keys))
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"SELECT pack_id, viewed_by, viewed_at, status FROM ml_message_views "
            f"WHERE pack_id IN ({placeholders}) AND account_id = ?",
            list(_keys) + [account_id],
        )).fetchall()
    return {r["pack_id"].replace("claim:", "", 1): dict(r) for r in rows}


async def save_oauth_state(state: str, code_verifier: str):
    """Guarda el state OAuth en DB para sobrevivir reinicios del servidor."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tokens WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return _with_nickname_fallback(dict(row))
        return None


async def get_any_tokens() -> Optional[dict]:
    """Obtiene cualquier token almacenado (para app single-user)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tokens LIMIT 1")
        row = await cursor.fetchone()
        if row:
            return _with_nickname_fallback(dict(row))
        return None


async def get_all_tokens() -> list:
    """Devuelve todas las cuentas almacenadas (user_id + nickname)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id, nickname FROM tokens ORDER BY created_at")
        rows = await cursor.fetchall()
        return [_with_nickname_fallback(dict(row)) for row in rows]


async def get_daily_goal(user_id: str) -> float:
    """Obtiene la meta diaria de una cuenta. Default: 500,000."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT daily_goal FROM account_settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return float(row["daily_goal"]) if row else 500000.0


async def set_daily_goal(user_id: str, goal: float):
    """Guarda la meta diaria de una cuenta."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("""
            INSERT INTO account_settings (user_id, daily_goal, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET daily_goal=excluded.daily_goal, updated_at=excluded.updated_at
        """, (user_id, goal))
        await db.commit()


async def update_nickname(user_id: str, nickname: str):
    """Actualiza el nickname de una cuenta existente."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE tokens SET nickname = ? WHERE user_id = ?",
            (nickname, user_id)
        )
        await db.commit()


async def delete_tokens(user_id: str):
    """Elimina los tokens de un usuario."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
        await db.commit()


async def log_concentration(
    base_sku: str, trigger: str, winner_user_id: str, winner_nickname: str,
    winner_item_id: str, winner_units_30d: int, total_bm_avail: int,
    accounts_zeroed: list, dry_run: bool = True, status: str = "ok", notes: str = ""
):
    """Registra una concentración de stock (real o simulada)."""
    import json as _json
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM amazon_accounts WHERE seller_id = ?", (seller_id,)
        )
        row = await cursor.fetchone()
        return _with_amazon_nickname_fallback(dict(row)) if row else None


async def get_all_amazon_accounts() -> list:
    """
    Devuelve todas las cuentas Amazon configuradas.
    Cada elemento incluye: seller_id, nickname, marketplace_id, marketplace_name.
    Usado por el selector de cuentas en el header del dashboard.
    """
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT seller_id, nickname, marketplace_id, marketplace_name FROM amazon_accounts ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [_with_amazon_nickname_fallback(dict(row)) for row in rows]


async def delete_amazon_account(seller_id: str):
    """Elimina una cuenta Amazon de la base de datos."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_alerts WHERE user_id = ? ORDER BY meli_stock DESC",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_sync_alerts() -> list:
    """Retorna todas las alertas de todos los usuarios."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_alerts ORDER BY user_id, meli_stock DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def delete_sync_alert(user_id: str, item_id: str):
    """Elimina un item específico de sync_alerts para user_id."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "DELETE FROM sync_alerts WHERE user_id = ? AND item_id = ?",
            (str(user_id), str(item_id))
        )
        await db.commit()


async def get_activate_suppressed(user_id: str) -> set:
    """Retorna set de item_ids suprimidos de Activar para este usuario."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activate_suppressed
            (user_id TEXT, item_id TEXT, PRIMARY KEY (user_id, item_id))
        """)
        cursor = await db.execute(
            "SELECT item_id FROM activate_suppressed WHERE user_id = ?", (str(user_id),)
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}


async def add_activate_suppressed(user_id: str, item_id: str):
    """Suprime permanentemente un item de la sección Activar para este usuario."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activate_suppressed
            (user_id TEXT, item_id TEXT, PRIMARY KEY (user_id, item_id))
        """)
        await db.execute(
            "INSERT OR IGNORE INTO activate_suppressed (user_id, item_id) VALUES (?, ?)",
            (str(user_id), str(item_id))
        )
        await db.commit()


async def remove_activate_suppressed(user_id: str, item_id: str):
    """Restaura un item suprimido para que vuelva a aparecer en Activar."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "DELETE FROM activate_suppressed WHERE user_id = ? AND item_id = ?",
            (str(user_id), str(item_id))
        )
        await db.commit()


async def save_sync_status(user_id: str, alerts_count: int, result: str = "ok"):
    """Actualiza el estado del último sync para user_id."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_status WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_amazon_stock_threshold(seller_id: str) -> int:
    """Retorna el umbral de stock bajo configurado para la cuenta."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cursor = await db.execute(
            "SELECT data_json FROM amazon_vel_cache "
            "WHERE days = ? AND computed_at > datetime('now', ? || ' hours')",
            (days, f"-{max_age_hours}"),
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None


async def save_amazon_vel_cache(days: int, data: dict) -> None:
    """Guarda/actualiza caché de velocidad Amazon para N días."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT sku, title, asin, available_qty FROM amazon_listings WHERE seller_id=?",
            [seller_id],
        )).fetchall()
    return [dict(r) for r in rows]


async def get_amazon_skus_and_qtys(seller_id: str) -> list[tuple[str, int]]:
    """Retorna [(sku, available_qty), ...] para el qty-only sync de Amazon."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "INSERT INTO listings_count_prev (platform, account_id, count, recorded_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(platform, account_id) DO UPDATE SET "
            "count=excluded.count, recorded_at=excluded.recorded_at",
            (platform, account_id, count, _t.time()),
        )
        await db.commit()


async def get_listings_count_prevs() -> dict:
    """Retorna {(platform, account_id): prev_count} para todos los registros."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "DELETE FROM orphan_listings WHERE platform=? AND account_id=?",
            (platform, account_id),
        )
        await db.commit()


async def get_orphan_listings(platform: str = None, account_id: str = None) -> list[dict]:
    """Retorna listings huérfanos. Filtra por platform y/o account_id si se especifican."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, synced_at, sku_count, elapsed_s, source "
            "FROM bm_sync_log ORDER BY id DESC LIMIT ?",
            [limit],
        )).fetchall()
    return [dict(r) for r in rows]


# ─── bm_bulk_fetch_log helpers ──────────────────────────────────────────────
# FEATURE 2026-08-18 (pedido por Jovan): a diferencia de bm_sync_log (solo
# éxitos), esto registra CADA intento real de fetch al bulk de BM -- para
# que una racha de timeouts silenciosos (como el incidente real: GR bulk
# fallando por 25h sin que nada quedara registrado) deje rastro consultable
# en vez de perderse en logs efímeros de Railway.

async def log_bm_bulk_fetch_attempt(
    bulk_name: str, status: str, rows_count: int = 0, elapsed_s: float = 0.0, error_message: str = "",
) -> None:
    """status: 'success' | 'empty' | 'error'. Solo se llama para intentos
    FRESCOS reales (no para cache-hit ni para el skip de <3min sin reintentar
    -- esos no son intentos, serían ruido). Conserva los últimos 300."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "INSERT INTO bm_bulk_fetch_log (ts, bulk_name, status, rows_count, elapsed_s, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_t.time(), bulk_name, status, rows_count, round(elapsed_s, 1), (error_message or "")[:300]),
        )
        await db.execute(
            "DELETE FROM bm_bulk_fetch_log WHERE id NOT IN "
            "(SELECT id FROM bm_bulk_fetch_log ORDER BY id DESC LIMIT 300)"
        )
        await db.commit()


async def get_bm_bulk_fetch_log(limit: int = 30, only_failures: bool = False) -> list[dict]:
    """Últimos `limit` intentos de fetch bulk, más reciente primero."""
    where = "WHERE status != 'success'" if only_failures else ""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"SELECT ts, bulk_name, status, rows_count, elapsed_s, error_message "
            f"FROM bm_bulk_fetch_log {where} ORDER BY id DESC LIMIT ?",
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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


# ─── bm_bulk_cache_snapshot helpers ──────────────────────────────────────────
# FIX 2026-08-10: los bulks crudos de BM (_bm_bulk_gr_cache, _bm_bulk_all_cache,
# _bm_bulk_loctj_cache, _bm_bulk_loc47_cache, _bm_bulk_loc68_cache) no tenian
# NINGUN respaldo en disco (a diferencia de _bm_stock_cache y stock_issues_cache,
# que si sobreviven deploys) -- verificado que esto es nuevo, no un duplicado de
# algo que ya existiera (aprendiendo del error de arriba).

async def save_bm_bulk_cache(cache_name: str, ts: float, rows: list) -> None:
    """Persiste un bulk crudo de BM a disco. Best-effort -- nunca debe tumbar
    el prewarm si falla."""
    import json as _json
    try:
        data_str = _json.dumps(rows, default=str, ensure_ascii=False)
    except Exception:
        return
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bm_bulk_cache_snapshot (cache_name, ts, data_json) VALUES (?, ?, ?)",
            (cache_name, ts, data_str),
        )
        await db.commit()


async def load_bm_bulk_cache(cache_name: str) -> tuple[float, list] | None:
    """Carga un bulk crudo de BM desde disco -- usado al arrancar el proceso
    para repoblar el cache en memoria antes de que corra el primer prewarm."""
    import json as _json
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ts, data_json FROM bm_bulk_cache_snapshot WHERE cache_name = ?", (cache_name,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        return (float(row["ts"]), _json.loads(row["data_json"]))
    except Exception:
        return None


# ─── return_flags helpers ────────────────────────────────────────────────────

async def save_return_flag(user_id: str, item_id: str, flag_type: str, note: str = "") -> None:
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM return_flags WHERE user_id=? AND resolved=0 ORDER BY created_at DESC",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_flagged_item_ids(user_id: str) -> set:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT DISTINCT item_id FROM return_flags WHERE user_id=? AND resolved=0",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def resolve_return_flag(user_id: str, item_id: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    order_data: str = "{}",
) -> int:
    """Crea una nueva solicitud de facturación. Retorna el id."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cursor = await db.execute(
            """INSERT INTO billing_requests
               (token, ml_user_id, platform, order_number, client_ref, created_by, created_at, notes, order_data)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (token, ml_user_id, platform, order_number, client_ref, created_by, now, notes, order_data),
        )
        await db.commit()
        return cursor.lastrowid


async def get_billing_request_by_token(token: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM billing_requests WHERE token=?", (token,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_billing_request_by_id(request_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM billing_requests WHERE platform=? AND order_number=? ORDER BY id DESC LIMIT 1",
            (platform, order_number.strip()),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_billing_status(request_id: int, status: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE billing_requests SET status=? WHERE id=?", (status, request_id)
        )
        await db.commit()


async def update_billing_order_data(request_id: int, order_data_json: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT constancia_name, constancia_data FROM billing_fiscal_data WHERE request_id=?",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row and row["constancia_data"]:
            return (row["constancia_name"] or "constancia.pdf", bytes(row["constancia_data"]))
        return None


def _invoices_dir() -> Path:
    """uploads/invoices/ junto a tokens.db — mismo patrón que claim_photos/.
    Se crea al vuelo; nunca se le aplica eviction (documentos reales, no caché)."""
    d = Path(DATABASE_PATH).resolve().parent / "uploads" / "invoices"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def save_billing_invoice(
    request_id: int, filename: str, file_data: bytes, uploaded_by: str,
    xml_filename: str = "", xml_data: Optional[bytes] = None,
) -> None:
    """Escribe el PDF/XML a MinIO/S3 (MI2) si está configurado; si no, cae a
    uploads/invoices/ en disco (comportamiento histórico, o fallback si la
    subida a S3 falla — mismo patrón que claim_photos, ver s3_storage.py).
    Firma y comportamiento externo sin cambios — ningún caller necesita tocarse."""
    from app.services import s3_storage as _s3_inv

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    storage = "local"

    pdf_path_rel = ""
    xml_path_rel = ""
    if _s3_inv.is_configured():
        try:
            if file_data:
                pdf_path_rel = f"invoices/{request_id}.pdf"
                _s3_inv.upload_bytes(pdf_path_rel, file_data, "application/pdf")
            if xml_data:
                xml_path_rel = f"invoices/{request_id}.xml"
                _s3_inv.upload_bytes(xml_path_rel, xml_data, "application/xml")
            storage = "s3"
        except Exception:
            logger.warning(f"[S3] fallo al subir factura {request_id} (MinIO caído?), cae a disco local")
            pdf_path_rel = ""
            xml_path_rel = ""
            storage = "local"

    if storage == "local":
        inv_dir = _invoices_dir()
        if file_data:
            pdf_path_rel = f"uploads/invoices/{request_id}.pdf"
            (inv_dir / f"{request_id}.pdf").write_bytes(file_data)
        if xml_data:
            xml_path_rel = f"uploads/invoices/{request_id}.xml"
            (inv_dir / f"{request_id}.xml").write_bytes(xml_data)

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            # file_data es NOT NULL en el schema legacy — se guarda b"" (no NULL)
            # para no violar la constraint; el lado de lectura ya trata bytes
            # vacíos igual que NULL (falsy) así que no cae al BLOB legacy.
            """INSERT INTO billing_invoices
                 (request_id, filename, file_data, xml_filename, xml_data,
                  pdf_path, xml_path, uploaded_by, uploaded_at, storage)
               VALUES (?,?,?,?,NULL,?,?,?,?,?)
               ON CONFLICT(request_id) DO UPDATE SET
                 filename=excluded.filename, file_data=?,
                 xml_filename=excluded.xml_filename, xml_data=NULL,
                 pdf_path=excluded.pdf_path, xml_path=excluded.xml_path,
                 uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at,
                 storage=excluded.storage""",
            (request_id, filename, b"", xml_filename or "", pdf_path_rel, xml_path_rel, uploaded_by, now, storage, b""),
        )
        await db.commit()


async def get_billing_invoice(request_id: int) -> Optional[dict]:
    """Retorna dict con pdf y xml, o None si no existe ninguno. Lee de MinIO/S3
    o de uploads/invoices/ según la columna storage; para filas viejas sin
    pdf_path cae al BLOB legacy (file_data)."""
    from app.services import s3_storage as _s3_inv

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT filename, file_data, xml_filename, xml_data, pdf_path, xml_path, storage "
            "FROM billing_invoices WHERE request_id=?",
            (request_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        is_s3 = row["storage"] == "s3"
        base_dir = Path(DATABASE_PATH).resolve().parent
        pdf_data = None
        if row["pdf_path"]:
            if is_s3:
                pdf_data = _s3_inv.get_object_bytes(row["pdf_path"])
            else:
                fp = base_dir / row["pdf_path"]
                if fp.is_file():
                    pdf_data = fp.read_bytes()
        elif row["file_data"]:
            pdf_data = bytes(row["file_data"])

        if not pdf_data:
            return None

        xml_data = None
        if row["xml_path"]:
            if is_s3:
                xml_data = _s3_inv.get_object_bytes(row["xml_path"])
            else:
                fp = base_dir / row["xml_path"]
                if fp.is_file():
                    xml_data = fp.read_bytes()
        elif row["xml_data"]:
            xml_data = bytes(row["xml_data"])

        return {
            "pdf_filename": row["filename"] or "factura.pdf",
            "pdf_data":     pdf_data,
            "xml_filename": row["xml_filename"] or "",
            "xml_data":     xml_data,
        }


async def delete_billing_request(request_id: int) -> None:
    from app.services import s3_storage as _s3_inv

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT pdf_path, xml_path, storage FROM billing_invoices WHERE request_id=?", (request_id,)
        )
        row = await cursor.fetchone()

    if row and row["storage"] == "s3":
        for path in (row["pdf_path"], row["xml_path"]):
            if path:
                _s3_inv.delete_object(path)
    else:
        inv_dir = _invoices_dir()
        for ext in ("pdf", "xml"):
            fp = inv_dir / f"{request_id}.{ext}"
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM billing_fiscal_data WHERE request_id=?", (request_id,))
        await db.execute("DELETE FROM billing_invoices WHERE request_id=?", (request_id,))
        await db.execute("DELETE FROM billing_requests WHERE id=?", (request_id,))
        await db.commit()


# ══════════════════════════════════════════════════════════════════
# Distribución de stock multi-cuenta
# ══════════════════════════════════════════════════════════════════

async def get_distribution_rule(user_id: str) -> dict | None:
    """Retorna la regla de distribución para una cuenta, o None si no tiene."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM account_stock_rules WHERE user_id = ?", (user_id,)
        )).fetchone()
    return dict(row) if row else None


async def get_all_distribution_rules() -> list[dict]:
    """Retorna todas las reglas de distribución, ordenadas por prioridad."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT OR REPLACE INTO account_stock_rules
               (user_id, nickname, priority, pct_full, pct_scarce, scarce_enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, nickname, priority, pct_full, pct_scarce, int(scarce_enabled), _t.time()),
        )
        await db.commit()


async def get_distribution_settings() -> dict:
    """Retorna los umbrales globales de distribución."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT OR REPLACE INTO stock_distribution_settings
               (id, scarce_threshold_units, scarce_threshold_days, safety_buffer_units, updated_at)
               VALUES (1, ?, ?, ?, ?)""",
            (scarce_threshold_units, scarce_threshold_days, safety_buffer_units, _t.time()),
        )
        await db.commit()


async def get_seasonal_events() -> list:
    """Retorna todos los eventos estacionales, más recientes primero."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM seasonal_events ORDER BY start_date DESC"
        )).fetchall()
    return [dict(r) for r in rows]


async def upsert_seasonal_event(
    name: str, start_date: str, end_date: str, lead_days: int,
    multiplier: float, category_filter: str = "", active: bool = True,
    event_id: int = None,
) -> int:
    """Crea o actualiza un evento estacional. Retorna el id."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        if event_id:
            await db.execute(
                """UPDATE seasonal_events SET name=?, start_date=?, end_date=?,
                   lead_days=?, multiplier=?, category_filter=?, active=?
                   WHERE id=?""",
                (name, start_date, end_date, lead_days, multiplier,
                 category_filter, int(active), event_id),
            )
            await db.commit()
            return event_id
        cur = await db.execute(
            """INSERT INTO seasonal_events
               (name, start_date, end_date, lead_days, multiplier, category_filter, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, start_date, end_date, lead_days, multiplier,
             category_filter, int(active), _t.time()),
        )
        await db.commit()
        return cur.lastrowid


async def delete_seasonal_event(event_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM seasonal_events WHERE id = ?", (event_id,))
        await db.commit()


# ─── Plantillas de respuesta (Mensajes de Compradores ML + Amazon) ───────────
async def get_reply_templates(platform: str = "", account_id: str = "") -> list:
    """Retorna las plantillas — si platform se da ('ml'/'amz'), incluye esas
    más las genéricas ('all'). Si además se da account_id (cuenta activa del
    hilo que se está respondiendo), excluye plantillas atadas a OTRA cuenta
    específica — deja las de "todas las cuentas de esta plataforma"
    (account_id='') y las de esta cuenta exacta. Sin account_id (modo gestión,
    sin hilo activo) se listan TODAS las de esa plataforma sin importar a qué
    cuenta estén atadas, para no esconder nada del CRUD."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        if platform and account_id:
            rows = await (await db.execute(
                "SELECT * FROM reply_templates "
                "WHERE (platform = ? OR platform = 'all') AND (account_id = '' OR account_id = ?) "
                "ORDER BY label",
                (platform, account_id),
            )).fetchall()
        elif platform:
            rows = await (await db.execute(
                "SELECT * FROM reply_templates WHERE platform = ? OR platform = 'all' ORDER BY label",
                (platform,),
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT * FROM reply_templates ORDER BY platform, label"
            )).fetchall()
    return [dict(r) for r in rows]


async def upsert_reply_template(
    label: str, body_text: str, platform: str = "all", account_id: str = "",
    created_by: str = "", template_id: int = None,
) -> int:
    """Crea o actualiza una plantilla de respuesta. Retorna el id."""
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        if template_id:
            await db.execute(
                "UPDATE reply_templates SET label=?, body_text=?, platform=?, account_id=? WHERE id=?",
                (label, body_text, platform, account_id, template_id),
            )
            await db.commit()
            return template_id
        cur = await db.execute(
            """INSERT INTO reply_templates (label, body_text, platform, account_id, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (label, body_text, platform, account_id, created_by, _t.time()),
        )
        await db.commit()
        return cur.lastrowid


async def delete_reply_template(template_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM reply_templates WHERE id = ?", (template_id,))
        await db.commit()


async def get_active_seasonal_boost(category: str = "") -> dict:
    """Retorna el boost estacional vigente HOY (considerando lead_days) para
    la categoría dada, o {"multiplier": 1.0, "event_name": None} si no hay
    ninguno activo. Si hay varios eventos traslapados, gana el multiplier
    más alto (nunca se suman)."""
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.utcnow().date()
    events = await get_seasonal_events()
    best = {"multiplier": 1.0, "event_name": None}
    for ev in events:
        if not ev.get("active"):
            continue
        cat_filter = (ev.get("category_filter") or "").strip()
        if cat_filter and category and cat_filter.upper() != category.upper():
            continue
        try:
            start = _dt.strptime(ev["start_date"], "%Y-%m-%d").date()
            end = _dt.strptime(ev["end_date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        lead = _td(days=int(ev.get("lead_days") or 0))
        if (start - lead) <= today <= end and ev["multiplier"] > best["multiplier"]:
            best = {"multiplier": ev["multiplier"], "event_name": ev["name"]}
    return best


async def get_all_bundles() -> dict:
    """Retorna {bundle_sku: {own_price_mxn, components: [{sku, qty}]}} para
    TODOS los bundles definidos — pensado para cargarse una vez por ciclo
    de prewarm, no por producto."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        bundles = await (await db.execute(
            "SELECT bundle_sku, own_price_mxn FROM sku_bundles"
        )).fetchall()
        components = await (await db.execute(
            "SELECT bundle_sku, component_sku, qty_per_bundle FROM sku_bundle_components"
        )).fetchall()
    result = {
        b["bundle_sku"]: {"own_price_mxn": b["own_price_mxn"], "components": []}
        for b in bundles
    }
    for c in components:
        if c["bundle_sku"] in result:
            result[c["bundle_sku"]]["components"].append(
                {"sku": c["component_sku"], "qty": c["qty_per_bundle"]}
            )
    return result


async def upsert_bundle(bundle_sku: str, own_price_mxn: float | None, components: list) -> None:
    """Crea o reemplaza por completo la definición de un bundle (borra
    componentes viejos e inserta los nuevos — el formulario siempre manda
    la lista completa)."""
    import time as _t
    now = _t.time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("SELECT created_at FROM sku_bundles WHERE bundle_sku = ?", (bundle_sku,))
        existing = await cur.fetchone()
        await db.execute(
            """INSERT OR REPLACE INTO sku_bundles (bundle_sku, own_price_mxn, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (bundle_sku, own_price_mxn, existing[0] if existing else now, now),
        )
        await db.execute("DELETE FROM sku_bundle_components WHERE bundle_sku = ?", (bundle_sku,))
        await db.executemany(
            """INSERT INTO sku_bundle_components (bundle_sku, component_sku, qty_per_bundle)
               VALUES (?, ?, ?)""",
            [(bundle_sku, c["sku"], int(c.get("qty", 1) or 1)) for c in components if c.get("sku")],
        )
        await db.commit()


async def delete_bundle(bundle_sku: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM sku_bundles WHERE bundle_sku = ?", (bundle_sku,))
        await db.execute("DELETE FROM sku_bundle_components WHERE bundle_sku = ?", (bundle_sku,))
        await db.commit()


async def replace_coverage_price_alerts(user_id: str, alerts: list) -> None:
    """Reemplaza TODAS las alertas de precio-por-cobertura de esta cuenta
    con el resultado fresco del ciclo de prewarm actual (igual que
    stock_issues_cache — se recalcula completo, no se acumula)."""
    import time as _t
    now = _t.time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("DELETE FROM coverage_price_alerts WHERE user_id = ?", (user_id,))
        await db.executemany(
            """INSERT INTO coverage_price_alerts
               (user_id, item_id, sku, product_title, current_price, suggested_price,
                reason, days_supply, units_30d, last_scan)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (user_id, a["item_id"], a.get("sku", ""), a.get("product_title", ""),
                 a["current_price"], a["suggested_price"], a["reason"],
                 a.get("days_supply"), a.get("units_30d", 0), now)
                for a in alerts
            ],
        )
        await db.commit()


async def get_coverage_price_alerts(user_id: str) -> list:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM coverage_price_alerts WHERE user_id = ? ORDER BY reason, days_supply",
            (user_id,),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_snapshot_check_candidates(platform: str, account_id: str, all_ids: list, limit: int = 20) -> list:
    """De all_ids (todos los listings activos), prioriza cuáles revisar este
    ciclo para Vigilancia: primero los que nunca se han revisado, luego los
    de last_checked más antiguo (rotación tipo LRU) — nunca revisa todo el
    catálogo de un jalón, mismo cuidado de rate-limit que el resto de la app."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT item_id, last_checked FROM listing_snapshots WHERE platform=? AND account_id=?",
            (platform, account_id),
        )).fetchall()
    checked = {r["item_id"]: r["last_checked"] for r in rows}
    never_checked = [i for i in all_ids if i not in checked]
    already_checked_sorted = sorted((i for i in all_ids if i in checked), key=lambda i: checked[i])
    return (never_checked + already_checked_sorted)[:limit]


async def sync_listing_snapshot(
    platform: str, account_id: str, item_id: str, sku: str, title: str,
    price: float, main_image_url: str, is_winner: bool | None, total_competitors: int,
) -> None:
    """Compara contra el snapshot anterior de este listing; si título/precio/
    imagen cambiaron, lo registra en listing_change_log (timeline). Actualiza
    not_winning_since la primera vez que is_winner pasa de True/None a False,
    y lo limpia si vuelve a ganar."""
    import time as _t
    now = _t.time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        prev = await (await db.execute(
            "SELECT * FROM listing_snapshots WHERE platform=? AND account_id=? AND item_id=?",
            (platform, account_id, item_id),
        )).fetchone()
        prev = dict(prev) if prev else None

        changes = []
        if prev:
            if prev["title"] and title and prev["title"] != title:
                changes.append(("title", prev["title"], title))
            if prev["price"] and price and abs(prev["price"] - price) > 0.01:
                changes.append(("price", str(prev["price"]), str(price)))
            if prev["main_image_url"] and main_image_url and prev["main_image_url"] != main_image_url:
                changes.append(("image", prev["main_image_url"], main_image_url))
        for field, old_v, new_v in changes:
            await db.execute(
                """INSERT INTO listing_change_log
                   (platform, account_id, item_id, sku, field, old_value, new_value, detected_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (platform, account_id, item_id, sku, field, old_v, new_v, now),
            )

        not_winning_since = prev["not_winning_since"] if prev else None
        if is_winner is False:
            if not_winning_since is None:
                not_winning_since = now
        elif is_winner is True:
            not_winning_since = None
        # is_winner is None (desconocido) — conservar el valor previo tal cual

        await db.execute(
            """INSERT OR REPLACE INTO listing_snapshots
               (platform, account_id, item_id, sku, title, price, main_image_url,
                is_winner, total_competitors, not_winning_since, last_checked)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (platform, account_id, item_id, sku, title, price, main_image_url,
             None if is_winner is None else int(is_winner), total_competitors,
             not_winning_since, now),
        )
        await db.commit()


async def get_listing_change_log(platform: str, account_id: str, days: int = 14, limit: int = 50) -> list:
    import time as _t
    cutoff = _t.time() - days * 86400
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT * FROM listing_change_log
               WHERE platform=? AND account_id=? AND detected_at >= ?
               ORDER BY detected_at DESC LIMIT ?""",
            (platform, account_id, cutoff, limit),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_not_winning_listings(platform: str, account_id: str, min_hours: float = 24.0) -> list:
    """SKUs/items que llevan >= min_hours seguidas sin ser ganadores de
    catálogo (ML) / Buy Box (Amazon)."""
    import time as _t
    cutoff = _t.time() - min_hours * 3600
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT * FROM listing_snapshots
               WHERE platform=? AND account_id=? AND is_winner=0
               AND not_winning_since IS NOT NULL AND not_winning_since <= ?
               ORDER BY not_winning_since ASC""",
            (platform, account_id, cutoff),
        )).fetchall()
    now = _t.time()
    return [
        {**dict(r), "hours_not_winning": round((now - r["not_winning_since"]) / 3600, 1)}
        for r in rows
    ]


async def delete_coverage_price_alert(user_id: str, item_id: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "DELETE FROM coverage_price_alerts WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        )
        await db.commit()


async def sync_stock_issue_streak(account_id: str, issue_type: str, current: dict) -> None:
    """Actualiza las rachas de un tipo de problema (ej. 'imbalanced') para esta
    cuenta. `current` = {sku: product_title} de los SKUs con el problema EN
    ESTE ciclo. Los que ya no aparecen se borran (racha rota, problema
    resuelto o desapareció). Los nuevos se insertan con first_seen_ts=ahora;
    los que persisten solo actualizan last_seen_ts (first_seen_ts no cambia,
    así el llamador puede calcular cuánto tiempo lleva)."""
    import time as _t
    now = _t.time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        existing_rows = await (await db.execute(
            "SELECT sku FROM stock_issue_streaks WHERE account_id = ? AND issue_type = ?",
            (account_id, issue_type),
        )).fetchall()
        existing_skus = {r[0] for r in existing_rows}
        current_skus = set(current.keys())

        gone = existing_skus - current_skus
        if gone:
            await db.executemany(
                "DELETE FROM stock_issue_streaks WHERE account_id = ? AND issue_type = ? AND sku = ?",
                [(account_id, issue_type, s) for s in gone],
            )
        new_skus = current_skus - existing_skus
        if new_skus:
            await db.executemany(
                """INSERT INTO stock_issue_streaks
                   (account_id, sku, issue_type, product_title, first_seen_ts, last_seen_ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(account_id, s, issue_type, current[s], now, now) for s in new_skus],
            )
        still_there = current_skus & existing_skus
        if still_there:
            await db.executemany(
                """UPDATE stock_issue_streaks SET last_seen_ts = ?, product_title = ?
                   WHERE account_id = ? AND issue_type = ? AND sku = ?""",
                [(now, current[s], account_id, issue_type, s) for s in still_there],
            )
        await db.commit()


async def get_drift_alerts(account_id: str, issue_type: str = "imbalanced", min_hours: float = 24.0) -> list:
    """Retorna SKUs que llevan >= min_hours consecutivas con el mismo
    problema — probable error de configuración BM (ej. LocationID mal
    clasificado), no un problema de venta real que cambia rápido."""
    import time as _t
    cutoff = _t.time() - min_hours * 3600
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT sku, product_title, first_seen_ts, last_seen_ts
               FROM stock_issue_streaks
               WHERE account_id = ? AND issue_type = ? AND first_seen_ts <= ?
               ORDER BY first_seen_ts ASC""",
            (account_id, issue_type, cutoff),
        )).fetchall()
    now = _t.time()
    return [
        {**dict(r), "hours_active": round((now - r["first_seen_ts"]) / 3600, 1)}
        for r in rows
    ]


async def get_account_sold_history(user_id: str) -> dict:
    """Retorna {base_sku: sold_qty} para todos los SKUs con ventas históricas en esta cuenta.
    Usado para la excepción histórica: cuentas sin scarce_enabled pero con historial de ventas
    siguen recibiendo stock en modo escasez.
    """
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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


async def get_orders_missing_zone(account_id: str, platform: str, limit: int = 15) -> list:
    """Órdenes ML recientes sin ship_zone aún resuelto — para el backfill
    acotado (máx N por ciclo, nunca hammering de la API de ML). Solo el
    order_id/item_id más reciente por SKU no importa aquí, cualquier fila
    sirve para obtener el shipment_id... pero no lo tenemos guardado, así
    que este helper solo identifica QUÉ falta; el shipment_id se resuelve
    en vivo contra la orden actual en main.py."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT DISTINCT order_id FROM order_history
               WHERE account_id = ? AND platform = ? AND (ship_zone IS NULL OR ship_zone = '')
               ORDER BY created_at DESC LIMIT ?""",
            (account_id, platform, limit),
        )).fetchall()
    return [r["order_id"] for r in rows]


async def update_order_zone(order_id: str, ship_state_code: str, ship_zone: str) -> None:
    """Actualiza la zona geográfica para TODAS las filas de esta orden
    (una orden puede tener varios item_id, todas van al mismo domicilio)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE order_history SET ship_state_code = ?, ship_zone = ? WHERE order_id = ?",
            (ship_state_code, ship_zone, order_id),
        )
        await db.commit()


async def get_zone_demand(account_id: str, platform: str = "ml", days: int = 60) -> dict:
    """Retorna {zone: total_units} de ventas recientes con zona ya resuelta —
    insumo para cruzar demanda por zona vs. stock físico por zona."""
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.utcnow() - _td(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        rows = await (await db.execute(
            """SELECT ship_zone, SUM(quantity) FROM order_history
               WHERE account_id = ? AND platform = ? AND ship_zone != '' AND order_date >= ?
               GROUP BY ship_zone""",
            (account_id, platform, cutoff),
        )).fetchall()
    return {r[0]: r[1] for r in rows if r[0]}


# Statuses de cancelación que disparan reversa de supplier_debt_ledger.
# ML usa "cancelled" (raw API); Amazon usa OrderStatus "Canceled"/"Cancelled"
# (ambas grafías se ven en el código, ver _CANCELED_STATUSES en amazon_orders.py).
_DEBT_CANCEL_STATUSES = {"cancelled", "Cancelled", "Canceled"}


async def upsert_order_history(rows: list[dict]) -> int:
    """Guarda/actualiza historial de ventas. ON CONFLICT actualiza con el dato más preciso.
    data_source='real' prevalece sobre 'estimated'; sale_fee y neto_plat toman el mayor valor.

    También genera (si es la primera vez que se ve el sale) una entrada en
    supplier_debt_ledger — deuda con la empresa proveedora = % fijo del
    retail por unidad (teles vs otras categorías). Ver supplier_debt_settings.
    Si el status de esta fila es una cancelación (_DEBT_CANCEL_STATUSES),
    revierte (amount_mxn=0) cualquier deuda ya registrada para esa orden.
    """
    import time as _t
    if not rows:
        return 0
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        _rate_row = await (await db.execute("SELECT rate_tv, rate_other FROM supplier_debt_settings WHERE id = 1")).fetchone()
        _rate_tv, _rate_other = (_rate_row[0], _rate_row[1]) if _rate_row else (0.80, 0.50)
        for r in rows:
            await db.execute("""
                INSERT INTO order_history
                    (order_id, account_id, platform, item_id, sku,
                     unit_price, quantity, sale_fee, neto_plat,
                     costo_usd, costo_mxn, retail_ph_usd,
                     ganancia_neta, margen_pct, recup_retail_pct,
                     fx_rate, currency, order_date, order_month,
                     status, data_source, created_at, shipping_cost_mxn)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    data_source      = CASE WHEN excluded.data_source = 'real' THEN 'real' ELSE order_history.data_source END,
                    shipping_cost_mxn = CASE WHEN excluded.shipping_cost_mxn > 0 THEN excluded.shipping_cost_mxn ELSE order_history.shipping_cost_mxn END
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
                _t.time(), r.get("shipping_cost_mxn", 0),
            ))
            try:
                sku = (r.get("sku") or "").upper()
                is_tv = sku.startswith("SNTV")
                rate = _rate_tv if is_tv else _rate_other
                quantity = r.get("quantity", 1) or 0
                retail_ph_usd = r.get("retail_ph_usd", 0) or 0
                fx_rate = r.get("fx_rate", 17.0) or 0
                amount_mxn = round(quantity * retail_ph_usd * fx_rate * rate, 2)
                order_date = r.get("order_date", "")
                iso_week = ""
                if order_date:
                    _dt = datetime.strptime(order_date, "%Y-%m-%d")
                    _iso = _dt.isocalendar()
                    iso_week = f"{_iso[0]}-W{_iso[1]:02d}"
                await db.execute("""
                    INSERT INTO supplier_debt_ledger
                        (order_id, item_id, platform, account_id, sku, is_tv,
                         category_rate, quantity, retail_ph_usd, fx_rate,
                         amount_mxn, order_date, iso_week, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(order_id, item_id, platform) DO UPDATE SET
                        retail_ph_usd = CASE WHEN supplier_debt_ledger.amount_mxn = 0 AND excluded.retail_ph_usd > 0
                                             THEN excluded.retail_ph_usd ELSE supplier_debt_ledger.retail_ph_usd END,
                        fx_rate       = CASE WHEN supplier_debt_ledger.amount_mxn = 0 AND excluded.amount_mxn > 0
                                             THEN excluded.fx_rate ELSE supplier_debt_ledger.fx_rate END,
                        amount_mxn    = CASE WHEN supplier_debt_ledger.amount_mxn = 0 AND excluded.amount_mxn > 0
                                             THEN excluded.amount_mxn ELSE supplier_debt_ledger.amount_mxn END
                """, (
                    r.get("order_id", ""), r.get("item_id", ""), r.get("platform", "ml"),
                    r.get("account_id", ""), r.get("sku", ""), 1 if is_tv else 0,
                    rate, quantity, retail_ph_usd, fx_rate,
                    amount_mxn, order_date, iso_week, _t.time(),
                ))
                # Reversa: si esta fila trae un status de cancelación, la deuda ya
                # registrada para esta orden (si la había) se pone en 0. Cubre
                # cancelaciones (antes de envío) en ambas plataformas -- NO cubre
                # reembolsos DESPUÉS de enviado (Amazon no cambia OrderStatus en
                # ese caso, vive en Finances API por separado; ML tampoco cambia
                # `status` en un reembolso post-pago -- pendiente, requiere
                # investigación aparte de cada API de reembolsos).
                if (r.get("status") or "") in _DEBT_CANCEL_STATUSES:
                    cur_rev = await db.execute("""
                        UPDATE supplier_debt_ledger SET amount_mxn = 0, reversed_at = ?
                        WHERE order_id = ? AND item_id = ? AND platform = ?
                          AND amount_mxn > 0 AND reversed_at = 0
                    """, (_t.time(), r.get("order_id", ""), r.get("item_id", ""), r.get("platform", "ml")))
                    if cur_rev.rowcount:
                        logger.info(f"[SUPPLIER-DEBT] Reversada deuda de orden cancelada {r.get('order_id')}/{r.get('item_id')} ({r.get('platform')})")
            except Exception:
                pass  # el ledger de deuda nunca debe tumbar el guardado de order_history
        await db.commit()
    return len(rows)


async def get_avg_shipping_cost_map(skus: list[str], platform: str, days: int = 90, min_samples: int = 3) -> dict:
    """Promedio de costo REAL de envío (shipping_cost_mxn, ya persistido por
    orden real en ML/Amazon) por SKU, en la plataforma dada, sobre los
    últimos `days` días. Reemplaza el estimado fijo/escalonado que usaba
    _calc_margins() (envio=150 o por tramo de retail) -- Jovan, 2026-08-13:
    "agarrar un histórico de las ventas y poder definir un envío promedio
    para cada sku actualizando cada x tiempo".

    Requiere al menos `min_samples` órdenes reales con shipping_cost_mxn>0
    para ese SKU -- si no hay suficiente historial (SKU nuevo, o nunca se
    capturó un costo real), simplemente NO aparece en el dict devuelto y el
    caller debe caer a su estimado de siempre.

    Retorna {sku: avg_shipping_mxn}.
    """
    if not skus:
        return {}
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    result: dict = {}
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        uniq = list(set(skus))
        for i in range(0, len(uniq), 500):
            chunk = uniq[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"""SELECT sku, AVG(shipping_cost_mxn) AS avg_ship, COUNT(*) AS n
                    FROM order_history
                    WHERE platform = ? AND order_date >= ? AND shipping_cost_mxn > 0
                      AND sku IN ({placeholders})
                    GROUP BY sku
                    HAVING COUNT(*) >= ?""",
                [platform, cutoff] + chunk + [min_samples],
            )
            for sku, avg_ship, _n in await cur.fetchall():
                result[sku] = round(avg_ship, 2)
    return result


async def reverse_debt_by_order_ids(order_ids: list[str], platform: str) -> int:
    """Revierte (amount_mxn=0) la deuda de proveedor ya registrada para estos
    order_id en la plataforma dada -- usado por Amazon para reembolsos reales
    (Finances API RefundEventList, ver reverse_amazon_refund_debt() en
    amazon_orders.py) y reusable para cualquier otro caso futuro de "aquí hay
    una lista de órdenes confirmadas reembolsadas". Idempotente: solo toca
    filas con amount_mxn>0 y reversed_at=0 -- no re-reversa dos veces.
    Retorna cuántas filas se revirtieron."""
    if not order_ids:
        return 0
    import time as _t
    now = _t.time()
    reversed_count = 0
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        for i in range(0, len(order_ids), 500):
            chunk = order_ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = await db.execute(
                f"""UPDATE supplier_debt_ledger SET amount_mxn = 0, reversed_at = ?
                    WHERE platform = ? AND order_id IN ({placeholders})
                      AND amount_mxn > 0 AND reversed_at = 0""",
                [now, platform] + chunk,
            )
            reversed_count += cur.rowcount
        await db.commit()
    if reversed_count:
        logger.info(f"[SUPPLIER-DEBT] Reversadas {reversed_count} filas por reembolso confirmado ({platform})")
    return reversed_count


async def has_deep_order_history(account_id: str, platform: str, min_days: int = 20) -> bool:
    """True si order_history ya tiene al menos una fila de hace min_days días o más
    para esta cuenta — señal de que ya se hizo un backfill inicial y el loop de
    mantenimiento (ventana corta) puede seguir sin volver a traer historia completa."""
    cutoff = (datetime.utcnow() - timedelta(days=min_days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(
            "SELECT 1 FROM order_history WHERE account_id=? AND platform=? AND order_date <= ? LIMIT 1",
            (account_id, platform, cutoff),
        )
        return (await cur.fetchone()) is not None


_MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _iso_week_range_label(iso_week: str) -> str:
    """'2026-W29' -> 'Lun 13 - Dom 19 jul 2026' (rango real de la semana ISO,
    lunes a domingo, para que se pueda cruzar contra fechas reales de pago)."""
    try:
        year_s, week_s = iso_week.split("-W")
        year, week = int(year_s), int(week_s)
        monday = date.fromisocalendar(year, week, 1)
        sunday = date.fromisocalendar(year, week, 7)
        if monday.month == sunday.month:
            return f"{monday.day}-{sunday.day} {_MESES_ES[monday.month - 1]} {monday.year}"
        if monday.year == sunday.year:
            return f"{monday.day} {_MESES_ES[monday.month - 1]} - {sunday.day} {_MESES_ES[sunday.month - 1]} {monday.year}"
        return f"{monday.day} {_MESES_ES[monday.month - 1]} {monday.year} - {sunday.day} {_MESES_ES[sunday.month - 1]} {sunday.year}"
    except Exception:
        return ""


async def get_supplier_debt_summary() -> dict:
    """Total generado (ledger), total pagado, saldo, y desglose semanal."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COALESCE(SUM(amount_mxn), 0) AS total FROM supplier_debt_ledger")
        total_generated = (await cur.fetchone())["total"]
        cur = await db.execute("SELECT COALESCE(SUM(amount_mxn), 0) AS total FROM supplier_debt_payments")
        total_paid = (await cur.fetchone())["total"]
        cur = await db.execute("""
            SELECT iso_week,
                   SUM(CASE WHEN is_tv = 1 THEN quantity ELSE 0 END) AS units_tv,
                   SUM(CASE WHEN is_tv = 0 THEN quantity ELSE 0 END) AS units_other,
                   SUM(quantity) AS units_total,
                   SUM(amount_mxn) AS amount_mxn
            FROM supplier_debt_ledger
            GROUP BY iso_week
            ORDER BY iso_week DESC
        """)
        weekly = [dict(r) for r in await cur.fetchall()]
        for w in weekly:
            w["week_range"] = _iso_week_range_label(w["iso_week"])
    return {
        "total_generated": round(total_generated, 2),
        "total_paid": round(total_paid, 2),
        "balance": round(total_generated - total_paid, 2),
        "weekly": weekly,
    }


async def get_supplier_debt_export_data(iso_week: str = "") -> list[dict]:
    """Deuda agregada por SKU — título/costo (bm_sku_master, maestro BM) + retail
    (order_history, snapshot al momento de cada venta) + unidades + monto generado.
    Para el export Excel de /deuda-empresa. iso_week opcional (ej. '2026-W29')
    filtra a solo esa semana — vacío = todas."""
    where_clause = "WHERE sdl.iso_week = ?" if iso_week else ""
    params = [iso_week] if iso_week else []
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"""
            SELECT
                sdl.sku AS sku,
                COALESCE(bsm.title, '') AS titulo,
                AVG(NULLIF(sdl.retail_ph_usd, 0)) AS retail_usd,
                MAX(bsm.cost_usd) AS costo_usd,
                SUM(sdl.quantity) AS unidades,
                SUM(sdl.amount_mxn) AS monto_generado_mxn
            FROM supplier_debt_ledger sdl
            LEFT JOIN bm_sku_master bsm ON bsm.sku = sdl.sku
            {where_clause}
            GROUP BY sdl.sku
            ORDER BY monto_generado_mxn DESC
        """, params)
        return [dict(r) for r in await cur.fetchall()]


async def list_supplier_debt_payments() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM supplier_debt_payments ORDER BY payment_date DESC, id DESC")
        return [dict(r) for r in await cur.fetchall()]


async def add_supplier_debt_payment(payment_date: str, amount_mxn: float, reference: str, notes: str, created_by: str) -> int:
    import time as _t
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("""
            INSERT INTO supplier_debt_payments (payment_date, amount_mxn, reference, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (payment_date, amount_mxn, reference, notes, created_by, _t.time()))
        await db.commit()
        return cur.lastrowid


async def delete_supplier_debt_payment(payment_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("DELETE FROM supplier_debt_payments WHERE id = ?", (payment_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_supplier_debt_settings() -> dict:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT rate_tv, rate_other FROM supplier_debt_settings WHERE id = 1")
        row = await cur.fetchone()
        return dict(row) if row else {"rate_tv": 0.80, "rate_other": 0.50}


async def set_supplier_debt_settings(rate_tv: float, rate_other: float) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("""
            INSERT INTO supplier_debt_settings (id, rate_tv, rate_other) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET rate_tv = excluded.rate_tv, rate_other = excluded.rate_other
        """, (rate_tv, rate_other))
        await db.commit()


async def save_reputation_snapshot(account_id: str, seller_reputation: dict) -> None:
    """Guarda 1 snapshot diario de seller_reputation (level_id + rates de
    claims/cancelaciones/demoras) — INSERT OR IGNORE, solo la primera llamada
    del día por cuenta se queda. Alimenta get_reputation_trend() para poder
    avisar ANTES de que la cuenta cruce a una zona peor, no solo reaccionar
    cuando ya está mal."""
    metrics = (seller_reputation or {}).get("metrics", {}) or {}
    level_id = (seller_reputation or {}).get("level_id") or ""
    claims_rate = metrics.get("claims", {}).get("rate", 0) or 0
    cancel_rate = metrics.get("cancellations", {}).get("rate", 0) or 0
    delay_rate = metrics.get("delayed_handling_time", {}).get("rate", 0) or 0
    now = __import__("time").time()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute("""
            INSERT OR IGNORE INTO reputation_snapshots
                (account_id, level_id, claims_rate, cancel_rate, delay_rate, captured_date, captured_at)
            VALUES (?,?,?,?,?,?,?)
        """, (account_id, level_id, claims_rate, cancel_rate, delay_rate, today, now))
        await db.commit()


_REPUTATION_LEVEL_RANK = {"5_green": 5, "4_light_green": 4, "3_yellow": 3, "2_orange": 2, "1_red": 1}


async def get_reputation_trend(account_id: str, days: int = 14) -> dict | None:
    """Compara el snapshot más viejo disponible en la ventana de `days` contra
    el más reciente. Retorna None si no hay al menos 2 snapshots (cuenta nueva
    en el sistema, o el histórico todavía no se acumula). 'worsening'=True si
    el level_id bajó de rango O cualquiera de los 3 rates subió >= 1 punto
    porcentual — umbral deliberadamente conservador para no alertar con ruido
    normal día a día."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT level_id, claims_rate, cancel_rate, delay_rate, captured_date
            FROM reputation_snapshots
            WHERE account_id = ? AND captured_date >= ?
            ORDER BY captured_date ASC
        """, (account_id, cutoff))
        rows = await cur.fetchall()
    if len(rows) < 2:
        return None
    old, new = rows[0], rows[-1]
    old_rank = _REPUTATION_LEVEL_RANK.get(old["level_id"], 3)
    new_rank = _REPUTATION_LEVEL_RANK.get(new["level_id"], 3)
    delta_claims = round((new["claims_rate"] - old["claims_rate"]) * 100, 2)
    delta_cancel = round((new["cancel_rate"] - old["cancel_rate"]) * 100, 2)
    delta_delay = round((new["delay_rate"] - old["delay_rate"]) * 100, 2)
    worsening = (new_rank < old_rank) or max(delta_claims, delta_cancel, delta_delay) >= 1.0
    return {
        "days_compared": days,
        "old_date": old["captured_date"], "new_date": new["captured_date"],
        "old_level": old["level_id"], "new_level": new["level_id"],
        "level_dropped": new_rank < old_rank,
        "delta_claims_pp": delta_claims, "delta_cancel_pp": delta_cancel, "delta_delay_pp": delta_delay,
        "worsening": worsening,
    }


async def upsert_claims_history(rows: list[dict]) -> int:
    """Guarda/actualiza reclamos ML persistidos. ON CONFLICT actualiza status/stage/comentario
    (un claim puede seguir evolucionando — abrirse, cerrarse, cambiar de stage).

    resolution_reason/refunded_buyer (2026-08-13): si el reclamo trae
    resolution.reason=='payment_refunded' (reembolso real al comprador,
    confirmado con datos reales de ML), se marca refunded_buyer=1 y se
    revierte cualquier deuda de proveedor ya registrada para esa orden --
    ver reverse_debt_for_refunded_claims(), llamada al final de esta función.
    Cubre el caso que la reversa por cancelación (upsert_order_history) NO
    cubre: reembolso DESPUÉS de enviado, donde el status de la orden nunca
    cambia a 'cancelled'.
    """
    import time as _t
    if not rows:
        return 0
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        for r in rows:
            await db.execute("""
                INSERT INTO claims_history
                    (claim_id, platform, account_id, order_id, item_id, sku,
                     reason_id, stage, status, quantity, amount_mxn,
                     buyer_comment, date_created, synced_at,
                     resolution_reason, refunded_buyer)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_id, platform) DO UPDATE SET
                    account_id    = excluded.account_id,
                    stage         = excluded.stage,
                    status        = excluded.status,
                    amount_mxn    = CASE WHEN excluded.amount_mxn > 0 THEN excluded.amount_mxn ELSE claims_history.amount_mxn END,
                    buyer_comment = CASE WHEN excluded.buyer_comment != '' THEN excluded.buyer_comment ELSE claims_history.buyer_comment END,
                    sku           = CASE WHEN excluded.sku != '' THEN excluded.sku ELSE claims_history.sku END,
                    item_id       = CASE WHEN excluded.item_id != '' THEN excluded.item_id ELSE claims_history.item_id END,
                    synced_at     = excluded.synced_at,
                    resolution_reason = CASE WHEN excluded.resolution_reason != '' THEN excluded.resolution_reason ELSE claims_history.resolution_reason END,
                    refunded_buyer    = CASE WHEN excluded.refunded_buyer = 1 THEN 1 ELSE claims_history.refunded_buyer END
            """, (
                r.get("claim_id", ""), r.get("platform", "ml"), r.get("account_id", ""),
                r.get("order_id", ""), r.get("item_id", ""), r.get("sku", ""),
                r.get("reason_id", ""), r.get("stage", ""), r.get("status", ""),
                r.get("quantity", 1), r.get("amount_mxn", 0),
                r.get("buyer_comment", ""), r.get("date_created", ""), _t.time(),
                r.get("resolution_reason", ""), 1 if r.get("refunded_buyer") else 0,
            ))
            if r.get("refunded_buyer") and r.get("order_id"):
                try:
                    cur_rev = await db.execute("""
                        UPDATE supplier_debt_ledger SET amount_mxn = 0, reversed_at = ?
                        WHERE order_id = ? AND platform = ?
                          AND amount_mxn > 0 AND reversed_at = 0
                    """, (_t.time(), r.get("order_id", ""), r.get("platform", "ml")))
                    if cur_rev.rowcount:
                        logger.info(f"[SUPPLIER-DEBT] Reversada deuda por reembolso post-envío, orden {r.get('order_id')} ({r.get('platform')}, claim {r.get('claim_id')})")
                except Exception as _e_rev:
                    logger.warning(f"[SUPPLIER-DEBT] Error reversando por reembolso claim {r.get('claim_id')}: {_e_rev}")
        await db.commit()
    return len(rows)


async def get_order_ids_with_open_claims(order_ids: list) -> set:
    """De una lista de order_id, retorna el subconjunto que tiene al menos un
    reclamo con status='opened' en claims_history — usado para el badge
    '🚩 reclamo abierto' en la tabla de órdenes (antes no había ningún puente
    entre Ventas y Salud, había que buscar el order_id a mano)."""
    if not order_ids:
        return set()
    placeholders = ",".join("?" * len(order_ids))
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        rows = await (await db.execute(
            f"SELECT DISTINCT order_id FROM claims_history WHERE status='opened' AND order_id IN ({placeholders})",
            list(order_ids),
        )).fetchall()
    return {r[0] for r in rows}


async def sync_amazon_seller_feedback(seller_id: str, days: int = 60) -> list[dict]:
    """Jala GET_SELLER_FEEDBACK_DATA para una cuenta Amazon, cruza order_id
    contra order_history para sacar SKU/ASIN (y de ahí el título, vía
    amazon_listings/bm_sku_master), guarda solo Negative/Neutral (Positive
    no aporta nada accionable) y devuelve las filas REALMENTE nuevas (para
    disparar la alerta por correo). Si el order_id no está en order_history
    (cuenta con backfill incompleto, ej. ExclusiveBulbs), cae a un llamado
    en vivo a la Orders API (getOrderItems) para no dejar el feedback sin
    SKU/título/link — confirmado 2026-07-31 que ExclusiveBulbs mostraba
    todo "(sin SKU)" por esto. También reintenta enriquecer filas VIEJAS que
    quedaron sin esos datos en un sync anterior (ON CONFLICT UPDATE), no
    solo las nuevas."""
    import hashlib
    import time as _t
    from datetime import datetime as _dt, timedelta as _td
    from app.services.amazon_client import get_amazon_client

    account = await get_amazon_account(seller_id)
    if not account:
        return []
    nick = account.get("nickname") or seller_id
    # Por marketplace_id, no marketplace_name (varía "US"/"USA" según de dónde
    # se sembró la cuenta — confirmado en vivo que ExclusiveBulbs guarda "US",
    # no "USA", y con el check viejo el link salía apuntando a amazon.com.mx)
    domain = "amazon.com" if account.get("marketplace_id") == "ATVPDKIKX0DER" else "amazon.com.mx"

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return []
    try:
        date_to = _dt.utcnow().strftime("%Y-%m-%d")
        date_from = (_dt.utcnow() - _td(days=days)).strftime("%Y-%m-%d")
        raw_items = await client.get_seller_feedback_report(date_from, date_to)
    except Exception as e:
        logger.warning(f"[Feedback] Error jalando seller feedback de {nick}: {e}")
        return []

    # Solo Negative/Neutral — Positive no requiere acción de nadie
    negative_neutral = [
        it for it in raw_items
        if it.get("rating", "").strip().lower() not in ("positive", "positivo", "")
    ]
    if not negative_neutral:
        return []

    order_ids = [it["order_id"] for it in negative_neutral if it.get("order_id")]
    info_by_order = {}  # order_id -> {sku, asin, title}
    if order_ids:
        placeholders = ",".join("?" * len(order_ids))
        async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
            rows = await (await db.execute(
                f"""SELECT order_id, sku, item_id FROM order_history
                    WHERE platform='amazon' AND order_id IN ({placeholders}) AND sku != ''""",
                order_ids,
            )).fetchall()
        for r in rows:
            info_by_order[r[0]] = {"sku": r[1], "asin": r[2] or "", "title": ""}
        # Título: buscar por SKU en amazon_listings (título tal como está
        # publicado) y si no, en bm_sku_master (título del catálogo BM)
        skus_found = [v["sku"] for v in info_by_order.values() if v["sku"]]
        if skus_found:
            placeholders2 = ",".join("?" * len(skus_found))
            async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
                al_rows = await (await db.execute(
                    f"SELECT sku, title FROM amazon_listings WHERE seller_id=? AND sku IN ({placeholders2})",
                    [seller_id] + skus_found,
                )).fetchall()
                title_by_sku = {r[0]: r[1] for r in al_rows if r[1]}
                missing_skus = [s for s in skus_found if s not in title_by_sku]
                if missing_skus:
                    placeholders3 = ",".join("?" * len(missing_skus))
                    bm_rows = await (await db.execute(
                        f"SELECT sku, title FROM bm_sku_master WHERE sku IN ({placeholders3})",
                        missing_skus,
                    )).fetchall()
                    title_by_sku.update({r[0]: r[1] for r in bm_rows if r[1]})
            for v in info_by_order.values():
                if v["sku"] in title_by_sku:
                    v["title"] = title_by_sku[v["sku"]]

    # Fallback en vivo — solo para los order_id que NO tenían nada en
    # order_history (cuentas con backfill incompleto). Acotado a la Orders
    # API (rate limit separado de Reports API), y solo para negativo/neutral
    # de esta corrida — nunca todo el histórico de un jalón.
    missing_order_ids = [oid for oid in order_ids if oid not in info_by_order]
    for oid in missing_order_ids:
        try:
            items = await client.get_order_items(oid)
        except Exception as e:
            logger.warning(f"[Feedback] No se pudo obtener order_items en vivo para {oid} ({nick}): {e}")
            continue
        if items:
            first = items[0]
            info_by_order[oid] = {
                "sku": first.get("SellerSKU", "") or "",
                "asin": first.get("ASIN", "") or "",
                "title": first.get("Title", "") or "",
            }

    new_rows = []
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        for it in negative_neutral:
            oid = it.get("order_id", "")
            info = info_by_order.get(oid, {"sku": "", "asin": "", "title": ""})
            asin_url = f"https://www.{domain}/dp/{info['asin']}" if info["asin"] else ""
            key_raw = f"{seller_id}|{oid}|{it.get('date','')}|{it.get('comment','')}"
            feedback_key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()[:32]
            cur = await db.execute(
                "SELECT id FROM amazon_seller_feedback WHERE feedback_key = ?", (feedback_key,)
            )
            existing = await cur.fetchone()
            await db.execute("""
                INSERT INTO amazon_seller_feedback
                    (account_id, seller_id, order_id, order_sku, order_asin, order_title,
                     asin_url, rating, comment, rater_email, date_created, status,
                     feedback_key, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(feedback_key) DO UPDATE SET
                    order_sku   = CASE WHEN excluded.order_sku   != '' THEN excluded.order_sku   ELSE amazon_seller_feedback.order_sku   END,
                    order_asin  = CASE WHEN excluded.order_asin  != '' THEN excluded.order_asin  ELSE amazon_seller_feedback.order_asin  END,
                    order_title = CASE WHEN excluded.order_title != '' THEN excluded.order_title ELSE amazon_seller_feedback.order_title END,
                    asin_url    = CASE WHEN excluded.asin_url    != '' THEN excluded.asin_url    ELSE amazon_seller_feedback.asin_url    END
            """, (
                nick, seller_id, oid, info["sku"], info["asin"], info["title"],
                asin_url, it.get("rating", ""), it.get("comment", ""), it.get("rater_email", ""),
                it.get("date", ""), "pending", feedback_key, _t.time(),
            ))
            if not existing:
                new_rows.append({**it, "account_id": nick, "sku": info["sku"]})
        await db.commit()

    if new_rows:
        logger.info(f"[Feedback] {nick}: {len(new_rows)} feedback negativo/neutral nuevo")
    return new_rows


async def sync_ml_item_reviews(user_id: str, top_n_items: int = 150) -> list[dict]:
    """Jala reseñas (GET /reviews/item/{id}) solo de los top_n_items más
    vendidos activos de la cuenta — NO de todo el catálogo. La API de ML no
    da forma de filtrar por rating ni de pedir 'solo las nuevas' (no hay
    parámetro de orden documentado, confirmado 2026-07-31), así que se pide
    un límite razonable por item (20) y se deduplica por review_id — con el
    tiempo esto va cubriendo el universo real de reseñas negativas de los
    productos que más importan, sin generar cientos de llamadas por cuenta.
    Guarda solo rate<=3 (negativo/neutral). Devuelve las filas nuevas."""
    import time as _t
    from app.services.meli_client import MeliClient

    tok = await get_tokens(user_id)
    if not tok:
        return []
    nick = tok.get("nickname") or user_id

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT item_id, base_sku, sku, title, data_json FROM ml_listings
               WHERE account_id = ? AND status = 'active' AND sold_qty > 0
               ORDER BY sold_qty DESC LIMIT ?""",
            (user_id, top_n_items),
        )).fetchall()
    if not rows:
        return []

    client = MeliClient(tok["access_token"], tok["refresh_token"], user_id)
    new_rows = []
    sem = asyncio.Semaphore(5)

    async def _process_item(item_id: str, sku: str, product_title: str, permalink: str):
        async with sem:
            rv = await client.get_item_reviews(item_id, limit=20)
        reviews = rv.get("reviews", []) if isinstance(rv, dict) else []
        for rev in reviews:
            rate = rev.get("rate", 0)
            if rate == 0 or rate > 3:
                continue  # solo negativo/neutral
            review_id = str(rev.get("id", ""))
            if not review_id:
                continue
            async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
                cur = await db.execute(
                    "SELECT id FROM ml_item_reviews WHERE review_id = ?", (review_id,)
                )
                existing = await cur.fetchone()
                await db.execute("""
                    INSERT INTO ml_item_reviews
                        (account_id, item_id, sku, review_id, rate, title, comment,
                         product_title, permalink, date_created, status, synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(review_id) DO UPDATE SET
                        product_title = CASE WHEN excluded.product_title != '' THEN excluded.product_title ELSE ml_item_reviews.product_title END,
                        permalink     = CASE WHEN excluded.permalink     != '' THEN excluded.permalink     ELSE ml_item_reviews.permalink     END,
                        sku           = CASE WHEN excluded.sku           != '' THEN excluded.sku           ELSE ml_item_reviews.sku           END
                """, (
                    user_id, item_id, sku, review_id, rate,
                    rev.get("title", ""), rev.get("content", ""),
                    product_title, permalink,
                    (rev.get("date_created") or "")[:10], "pending", _t.time(),
                ))
                await db.commit()
            if not existing:
                new_rows.append({
                    "account_id": user_id, "nickname": nick, "item_id": item_id, "sku": sku,
                    "review_id": review_id, "rate": rate,
                    "title": rev.get("title", ""), "comment": rev.get("content", ""),
                    "product_title": product_title,
                })

    def _extract_permalink(data_json_raw: str) -> str:
        if not data_json_raw:
            return ""
        try:
            return json.loads(data_json_raw).get("permalink", "") or ""
        except Exception:
            return ""

    try:
        await asyncio.gather(*[
            _process_item(
                r["item_id"], r["base_sku"] or r["sku"],
                r["title"] or "", _extract_permalink(r["data_json"]),
            ) for r in rows
        ], return_exceptions=True)
    finally:
        await client.close()

    if new_rows:
        logger.info(f"[Feedback] {nick}: {len(new_rows)} reseñas negativas/neutras nuevas")
    return new_rows


_FEEDBACK_SYNC_INTERVAL_SECONDS = 4 * 3600  # 4h — Amazon Reports API aguanta este ritmo sin quota (createReport es ~1/45seg sostenido, 4h da margen de sobra)
_FEEDBACK_NOTIFY_TO = "jovan.rodriguez@miglobal.com.mx"


async def _run_feedback_sync_once() -> None:
    """Corre los 2 syncs (Amazon seller feedback + reseñas ML) para todas las
    cuentas configuradas, y si hay algo genuinamente nuevo manda UN correo
    resumen (no uno por cada item — se agrupan) via Gmail API. Todo de
    lectura, cero riesgo de tocar listings."""
    all_new_amz: list = []
    all_new_ml: list = []

    try:
        accounts = await get_all_amazon_accounts()
        for acc in accounts:
            try:
                rows = await sync_amazon_seller_feedback(acc["seller_id"])
                all_new_amz.extend(rows)
            except Exception as e:
                logger.warning(f"[Feedback] Error sync Amazon {acc.get('nickname','?')}: {e}")
    except Exception as e:
        logger.warning(f"[Feedback] Error listando cuentas Amazon: {e}")

    try:
        tokens = await get_all_tokens()
        for tok in tokens:
            try:
                rows = await sync_ml_item_reviews(tok["user_id"])
                all_new_ml.extend(rows)
            except Exception as e:
                logger.warning(f"[Feedback] Error sync ML {tok.get('nickname','?')}: {e}")
    except Exception as e:
        logger.warning(f"[Feedback] Error listando cuentas ML: {e}")

    total_new = len(all_new_amz) + len(all_new_ml)
    if total_new == 0:
        return

    try:
        from app.services import buyer_messages_client as _bmc
        lines = [f"Nuevo feedback/reseñas negativas o neutras detectadas ({total_new} en total):", ""]
        if all_new_amz:
            lines.append(f"AMAZON ({len(all_new_amz)}):")
            for r in all_new_amz[:20]:
                lines.append(f"  - [{r.get('account_id','')}] SKU {r.get('sku') or '(sin SKU)'} | rating={r.get('rating','')} | \"{r.get('comment','')[:150]}\"")
            lines.append("")
        if all_new_ml:
            lines.append(f"MERCADO LIBRE ({len(all_new_ml)}):")
            for r in all_new_ml[:20]:
                lines.append(f"  - [{r.get('nickname') or r.get('account_id','')}] SKU {r.get('sku') or '(sin SKU)'} | {r.get('rate','')}★ | \"{r.get('title','')}\": \"{r.get('comment','')[:150]}\"")
            lines.append("")
        lines.append("Revisa el detalle completo en Salud > Feedback.")
        await _bmc.send_notification(
            _FEEDBACK_NOTIFY_TO,
            f"[Apantallate MX] {total_new} feedback/reseña nueva por revisar",
            "\n".join(lines),
        )
        logger.info(f"[Feedback] Correo de alerta enviado ({total_new} items nuevos)")
    except Exception as e:
        logger.warning(f"[Feedback] No se pudo enviar el correo de alerta: {e}")


async def feedback_sync_loop() -> None:
    """Loop de fondo — se lanza una vez al arrancar la app (main.py startup).
    Corre 1x/24h: el feedback/reseñas no cambian con más frecuencia que eso.
    Espera 10 min antes de la PRIMERA corrida (igual patrón que
    _startup_prewarm en main.py) — createReport de Amazon Reports API tiene
    quota muy baja y ya compite con otros reportes (inventory, financial) al
    arrancar; sin este delay, cada restart/deploy dispara el sync de
    inmediato — confirmado en vivo 2026-07-31 al probar localmente: cada
    reinicio de uvicorn re-consultaba el reporte de feedback de VECKTOR."""
    await asyncio.sleep(600)
    while True:
        try:
            await _run_feedback_sync_once()
        except Exception as e:
            logger.warning(f"[Feedback] Error en feedback_sync_loop: {e}")
        await asyncio.sleep(_FEEDBACK_SYNC_INTERVAL_SECONDS)


async def get_amazon_feedback_tab(seller_id: str, status: str = "pending") -> list:
    """Feedback de vendedor (GET_SELLER_FEEDBACK_DATA) de UNA cuenta Amazon —
    acotado por seller_id (misma convención que get_amazon_client), nunca
    mezclado con otras cuentas (regla del proyecto)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT id, account_id, order_id, order_sku AS sku, order_asin AS asin,
                      order_title AS product_title, asin_url, rating, comment,
                      date_created, status
               FROM amazon_seller_feedback WHERE seller_id = ? AND status = ?
               ORDER BY date_created DESC""",
            (seller_id, status),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_ml_feedback_tab(user_id: str, status: str = "pending") -> list:
    """Reseñas negativas/neutras de UNA cuenta ML — acotado por user_id
    (misma convención que get_not_winning_listings/vigilancia)."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT id, account_id, item_id, sku, rate, title, comment,
                      product_title, permalink,
                      date_created, status
               FROM ml_item_reviews WHERE account_id = ? AND status = ?
               ORDER BY date_created DESC""",
            (user_id, status),
        )).fetchall()
    return [dict(r) for r in rows]


async def set_feedback_status(platform: str, feedback_id: int, status: str) -> bool:
    """Marca un feedback (Amazon) o reseña (ML) como atendido/pendiente."""
    table = "amazon_seller_feedback" if platform == "amazon" else "ml_item_reviews"
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute(f"UPDATE {table} SET status = ? WHERE id = ?", (status, feedback_id))
        await db.commit()
        return cur.rowcount > 0


async def save_claim_photos(claim_id: str, platform: str, photos: list[dict]) -> int:
    """Registra fotos. `local_path` es la ruta en disco (storage='local') o la
    key S3 (storage='s3') — mismo campo, distinto significado según `storage`."""
    import time as _t
    if not photos:
        return 0
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        for p in photos:
            await db.execute("""
                INSERT OR IGNORE INTO claim_photos
                    (claim_id, platform, local_path, original_url, from_role, downloaded_at, storage)
                VALUES (?,?,?,?,?,?,?)
            """, (
                claim_id, platform, p.get("local_path", ""), p.get("original_url", ""),
                p.get("from_role", ""), _t.time(), p.get("storage", "local"),
            ))
        await db.commit()
    return len(photos)


async def get_claims_history(
    sku: str = None,
    item_id: str = None,
    account_id: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 1000,
) -> list[dict]:
    """Lee claims_history con filtros opcionales, más recientes primero.
    item_id es fallback cuando el listing no tiene seller_custom_field (sin SKU BM) —
    sigue siendo posible identificar sus reclamos por el ID de publicación ML."""
    conditions = []
    params: list = []
    if sku:
        conditions.append("sku = ?")
        params.append(sku.upper())
    if item_id:
        conditions.append("item_id = ?")
        params.append(item_id)
    if account_id:
        conditions.append("account_id = ?")
        params.append(account_id)
    if date_from:
        conditions.append("date_created >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date_created <= ?")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT * FROM claims_history {where} ORDER BY date_created DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_claim_photos(claim_id: str) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claim_photos WHERE claim_id = ? ORDER BY id", (claim_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_claim_photos_by_path(local_paths: list[str]) -> int:
    """Borra filas de claim_photos por local_path — usado por el evictor de presupuesto
    de disco (ver _enforce_claim_photos_budget en main.py) después de borrar el archivo
    físico, para que la DB no apunte a fotos que ya no existen.

    Compara normalizando `\\`→`/` en ambos lados (SQL REPLACE) — filas escritas en
    Windows (local_path con backslash) y en Railway/Coolify (Linux, forward slash)
    conviven en la misma DB; sin normalizar, el DELETE no hacía match contra filas
    del otro SO y quedaban huérfanas (archivo borrado, fila viva) sin ningún error
    visible (encontrado 2026-08-03, auditoría de sistema)."""
    if not local_paths:
        return 0
    norm_paths = [p.replace("\\", "/") for p in local_paths]
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        placeholders = ",".join("?" * len(norm_paths))
        cur = await db.execute(
            f"DELETE FROM claim_photos WHERE REPLACE(local_path, '\\', '/') IN ({placeholders})",
            norm_paths,
        )
        await db.commit()
        if cur.rowcount != len(local_paths):
            logger.warning(
                f"[CLAIM-PHOTOS-BUDGET] delete_claim_photos_by_path: se intentó borrar "
                f"{len(local_paths)} filas, solo {cur.rowcount} hicieron match — posibles "
                f"filas huérfanas restantes (archivo ya borrado, fila viva en DB)"
            )
        return cur.rowcount


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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
        LEFT JOIN amazon_accounts aa ON aa.seller_id = al.seller_id
        LEFT JOIN order_history oh
            ON oh.account_id = aa.nickname
            AND oh.platform IN ('amazon','amz','Amazon')
            AND (oh.sku = al.sku OR oh.sku = al.base_sku)
        LEFT JOIN bm_sku_master bc
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
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT INTO item_history
               (item_id, account_id, field, old_value, new_value, changed_by, changed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, account_id, field, old_value, new_value, changed_by, changed_at, now),
        )
        await db.commit()


async def get_item_history(item_id: str, limit: int = 50) -> list:
    """Retorna los últimos cambios de un item, del más reciente al más antiguo."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
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
