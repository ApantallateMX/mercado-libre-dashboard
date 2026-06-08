"""
Amazon Lanzar — wizard de creación de listings desde gaps BM → Amazon.

Flujo 1: SKU con ASIN existente → LISTING_OFFER_ONLY (solo agrega oferta)
Flujo 2: SKU sin ASIN            → LISTING (crea producto, Amazon asigna ASIN)
Flujo 3: Lanzados                → editar/actualizar listing activo
"""
import asyncio
import json
import logging
import math
import re as _re
import time as _time
from datetime import datetime
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import DATABASE_PATH
from app.services.amazon_client import get_amazon_client
from app.services import openrouter_client as _or_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/amazon/lanzar", tags=["amazon-lanzar"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# ─────────────────────────────────────────────────────────────────────────────
# SCAN BACKGROUND — Gap BM → Amazon (mismo patrón que ML Lanzador)
# ─────────────────────────────────────────────────────────────────────────────

_amz_scan_locks: dict[str, asyncio.Lock] = {}

_AMZ_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC", "-FLX01", "-FLX02")
# Matches: SKU-FBA, SKU_FBA, SKU-FBA-0, SKU_FBA_0, SKU-FBM, etc.
_AMZ_FBA_RE = _re.compile(r'^(.+?)[-_](?:FBA|FBM)(?:[-_]\d+)?$', _re.IGNORECASE)


def _amz_base(sku: str) -> str:
    u = (sku or "").upper()
    for s in _AMZ_SUFFIXES:
        if u.endswith(s):
            return u[:-len(s)]
    m = _AMZ_FBA_RE.match(u)
    if m:
        return m.group(1)
    return u


_AMZ_CACHE_TTL_H = 24  # horas de validez del cache por SKU

# Variantes FBA/FBM a probar cuando el SKU base no se encuentra
_AMZ_CHECK_SUFFIXES = ("-FBA", "_FBA_0", "-FBA-0", "-FBM")


async def _verify_bm_skus_individually(client, seller_id: str, bm_items: list) -> set:
    """
    Verifica cada BM SKU individualmente en Amazon vía getListingsItem.
    Usa cache en DB (amz_catalog_cache, TTL 24h) para evitar re-verificar.
    Retorna set de base-SKUs que SÍ existen en Amazon (en cualquier estado).
    """
    from datetime import timedelta
    found_skus: set[str] = set()
    sem = asyncio.Semaphore(5)  # max 5 calls concurrentes
    cutoff = (datetime.utcnow() - timedelta(hours=_AMZ_CACHE_TTL_H)).isoformat()

    # Cargar cache existente
    cache_found: set[str] = set()
    cache_not_found: set[str] = set()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT sku_upper, found FROM amz_catalog_cache WHERE seller_id=? AND checked_at > ?",
            (seller_id, cutoff),
        )
        rows = await cur.fetchall()
        for r in rows:
            if r["found"]:
                cache_found.add(r["sku_upper"])
            else:
                cache_not_found.add(r["sku_upper"])

    bm_skus = [(item.get("SKU") or "").strip().upper() for item in bm_items if item.get("SKU")]
    to_check = [s for s in bm_skus if s and s not in cache_found and s not in cache_not_found]

    logger.info(
        f"[AMZ Gap Scan] Verificación individual: {len(bm_skus)} SKUs BM, "
        f"{len(cache_found)} en caché (found), {len(cache_not_found)} en caché (not found), "
        f"{len(to_check)} a verificar via API"
    )

    # Add cached found SKUs immediately
    found_skus.update(cache_found)

    async def _check_one(bm_sku: str) -> tuple[str, bool]:
        async with sem:
            # Try base SKU first
            result = await client.get_listing_item(bm_sku)
            if result is not None:
                return bm_sku, True
            # Try FBA/FBM variants
            for sfx in _AMZ_CHECK_SUFFIXES:
                result = await client.get_listing_item(bm_sku + sfx)
                if result is not None:
                    return bm_sku, True
            return bm_sku, False

    if to_check:
        tasks = [_check_one(s) for s in to_check]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        new_cache_entries = []
        now_iso = datetime.utcnow().isoformat()
        for res in results:
            if isinstance(res, Exception):
                continue
            sku, is_found = res
            if is_found:
                found_skus.add(sku)
            new_cache_entries.append((seller_id, sku, 1 if is_found else 0, now_iso))

        if new_cache_entries:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.executemany(
                    """INSERT INTO amz_catalog_cache (seller_id, sku_upper, found, checked_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(seller_id, sku_upper) DO UPDATE SET
                           found=excluded.found, checked_at=excluded.checked_at""",
                    new_cache_entries,
                )
                await db.commit()

    return found_skus


async def _run_amz_gap_scan(seller_id: str) -> None:
    """Background task: compara BM stock vs Amazon activos → actualiza amz_sku_gaps."""
    if seller_id not in _amz_scan_locks:
        _amz_scan_locks[seller_id] = asyncio.Lock()
    lock = _amz_scan_locks[seller_id]
    if lock.locked():
        return

    async with lock:
        logger.info(f"[AMZ Gap Scan] Iniciando para seller {seller_id}")
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """INSERT INTO amz_gap_scan_status (seller_id, status, started_at, finished_at, error)
                   VALUES (?, 'running', ?, NULL, NULL)
                   ON CONFLICT(seller_id) DO UPDATE SET
                       status='running', started_at=excluded.started_at,
                       finished_at=NULL, error=NULL""",
                (seller_id, datetime.utcnow().isoformat()),
            )
            await db.commit()

        try:
            client = await get_amazon_client(seller_id=seller_id)
            if not client:
                raise Exception("Cuenta Amazon no encontrada")

            logger.info(
                f"[AMZ Gap Scan] seller={client.seller_id} | "
                f"marketplace_id={client.marketplace_id} | "
                f"marketplace_name={client.marketplace_name} | "
                f"nickname={client.nickname}"
            )

            from app.services.binmanager_client import get_shared_bm
            bm_cli = await get_shared_bm()

            # Construir set de base-SKUs conocidos en Amazon
            amazon_base_skus: set[str] = set()
            amazon_active = 0

            # ── Paso 1: Verificar cobertura de DB ─────────────────────────────
            _DB_MIN_COVERAGE = 500  # SKUs mínimos en DB para confiar en ella
            db_count = 0
            async with aiosqlite.connect(DATABASE_PATH) as _dbcheck:
                _row = await (await _dbcheck.execute(
                    "SELECT COUNT(*) FROM amazon_listings WHERE seller_id=?",
                    (seller_id,)
                )).fetchone()
                db_count = _row[0] if _row else 0

            db_first = db_count >= _DB_MIN_COVERAGE

            if db_first:
                # ── DB-first: sin llamadas API para descubrimiento de SKUs ──────
                logger.info(f"[AMZ Gap Scan] DB-first: {db_count} listings en DB para {seller_id}")
                async with aiosqlite.connect(DATABASE_PATH) as _adb:
                    _adb.row_factory = aiosqlite.Row
                    _cur = await _adb.execute(
                        "SELECT sku, base_sku FROM amazon_listings WHERE seller_id=?",
                        (seller_id,)
                    )
                    for _r in await _cur.fetchall():
                        sku_u = (_r["sku"] or "").upper()
                        base_u = (_r["base_sku"] or "").upper()
                        amazon_base_skus.add(sku_u)
                        if base_u:
                            amazon_base_skus.add(base_u)
                        base_derived = _amz_base(sku_u)
                        if base_derived:
                            amazon_base_skus.add(base_derived)
                        amazon_active += 1
                logger.info(
                    f"[AMZ Gap Scan] DB: {amazon_active} listings, "
                    f"{len(amazon_base_skus)} base-SKUs únicos"
                )
            else:
                # ── Sparse/empty DB: usar API directamente ────────────────────
                logger.info(
                    f"[AMZ Gap Scan] API-first: DB tiene {db_count} listings < {_DB_MIN_COVERAGE}. "
                    f"Usando API para descubrimiento."
                )
                listings = await client.get_all_listings()
                _LISTINGS_PAGE_CAP = 990
                if listings and len(listings) >= _LISTINGS_PAGE_CAP:
                    logger.warning(
                        f"[AMZ Gap Scan] get_all_listings() devolvió {len(listings)} SKUs "
                        f"(truncado) → usando Reports API"
                    )
                    listings = []

                if listings:
                    for listing in listings:
                        sku = listing.get("sku", "")
                        if not sku:
                            continue
                        amazon_base_skus.add(sku.upper())
                        base = _amz_base(sku)
                        if base:
                            amazon_base_skus.add(base)
                    amazon_active = sum(
                        1 for l in listings
                        if any(s.get("status") == "ACTIVE" for s in l.get("summaries", []))
                    )
                    logger.info(f"[AMZ Gap Scan] Listings API: {amazon_active} activos de {len(listings)} total")
                else:
                    try:
                        report_skus = await client.get_merchant_listings_report()
                        for entry in report_skus:
                            sku = entry.get("sku") or ""
                            if not sku:
                                continue
                            sku_up = sku.upper()
                            amazon_base_skus.add(sku_up)
                            base = _amz_base(sku)
                            if base:
                                amazon_base_skus.add(base)
                            if entry.get("status", "").upper() == "ACTIVE":
                                amazon_active += 1
                        logger.info(
                            f"[AMZ Gap Scan] Reports API: {amazon_active} activos de {len(report_skus)} total"
                        )
                    except Exception as _rpt_err:
                        logger.error(f"[AMZ Gap Scan] Reports API falló: {_rpt_err}", exc_info=True)
                        try:
                            fba_items = await client.get_fba_inventory_all()
                            for fba in fba_items:
                                sku = fba.get("sellerSku") or ""
                                if sku:
                                    amazon_base_skus.add(sku.upper())
                                    base = _amz_base(sku)
                                    if base:
                                        amazon_base_skus.add(base)
                            amazon_active = len({f.get("sellerSku") for f in fba_items if f.get("sellerSku")})
                            logger.info(f"[AMZ Gap Scan] FBA fallback: {amazon_active} SKUs")
                        except Exception as _fba_err:
                            logger.warning(f"[AMZ Gap Scan] FBA fallback también falló: {_fba_err}")

            # ── Paso 2: Augmentar amazon_base_skus con confirmaciones previas del cache ──
            # Esto asegura que SKUs ya verificados como "found" en scans anteriores
            # se traten como "ya lanzados" incluso si Reports API no los devolvió.
            # Es lo que permite que el cleanup borre filas viejas de amz_sku_gaps.
            from datetime import timedelta
            _cache_cutoff = (datetime.utcnow() - timedelta(hours=_AMZ_CACHE_TTL_H)).isoformat()
            async with aiosqlite.connect(DATABASE_PATH) as _caug:
                _caug.row_factory = aiosqlite.Row
                _caug_cur = await _caug.execute(
                    "SELECT sku_upper FROM amz_catalog_cache WHERE seller_id=? AND found=1 AND checked_at>?",
                    (seller_id, _cache_cutoff),
                )
                for _cr in await _caug_cur.fetchall():
                    _cu = _cr["sku_upper"]
                    amazon_base_skus.add(_cu)
                    _cb = _amz_base(_cu)
                    if _cb:
                        amazon_base_skus.add(_cb)
            logger.debug(
                f"[AMZ Gap Scan] amazon_base_skus tras augmentar cache: {len(amazon_base_skus)} entradas"
            )

            # Fetch BM stock (BM es rápido por caché)
            bm_items = await bm_cli.get_bulk_stock(conditions="GRA,GRB,GRC,NEW,ICB,ICC")

            FX = 18.0  # FX fijo USD → MXN
            now_iso = datetime.utcnow().isoformat()
            gaps: list[dict] = []
            current_bm_skus: set[str] = set()

            for item in bm_items:
                bm_sku = (item.get("SKU") or "").strip()
                if not bm_sku:
                    continue
                avail_qty = int(item.get("AvailableQTY") or item.get("TotalQty") or 0)
                if avail_qty <= 0:
                    continue

                bm_sku_up = bm_sku.upper()
                current_bm_skus.add(bm_sku_up)

                already = bm_sku_up in amazon_base_skus or any(
                    (bm_sku_up + sfx) in amazon_base_skus for sfx in _AMZ_SUFFIXES
                )
                if already:
                    continue

                # Retail price — NUNCA usar AvgCostQTY (centinela 9999.99)
                retail_usd = float(item.get("LastRetailPricePurchaseHistory") or 0)
                if retail_usd >= 9000:
                    retail_usd = 0.0
                has_price = retail_usd > 0
                retail_mxn = round(retail_usd * FX, 2) if has_price else 0
                # Recuperar 100% retail tras Amazon 18% + socio 7% = 25% fees
                price_sug = round(retail_mxn / 0.75, 0) if has_price else 0
                margin_pct = 100.0 if has_price else None
                gap_status = "unlaunched" if has_price else "sin_precio"

                gaps.append({
                    "seller_id":       seller_id,
                    "sku":             bm_sku,
                    "product_title":   (item.get("Title") or item.get("Description") or "")[:120],
                    "brand":           item.get("Brand") or "",
                    "model":           item.get("Model") or "",
                    "category":        item.get("CategoryName") or "",
                    "image_url":       item.get("ImageURL") or "",
                    "upc":             item.get("UPC") or item.get("Upc") or "",
                    "avail_qty":       avail_qty,
                    "cost_usd":        round(retail_usd, 2),
                    "cost_mxn":        retail_mxn,
                    "suggested_price": price_sug,
                    "margin_pct":      margin_pct,
                    "last_scan":       now_iso,
                    "_gap_status":     gap_status,
                })

            # ── Verificación individual de gaps ───────────────────────────────────
            # Siempre corre (tanto DB-first como API-first).
            # Con DB-first: DB+cache ya eliminaron casi todos → pocos llegan aquí → pocas API calls.
            # Con API-first: verifica todos usando cache primero, luego API si necesario.
            if gaps:
                logger.info(
                    f"[AMZ Gap Scan] Verificando {len(gaps)} gaps individualmente "
                    f"(db_first={db_first}, db_count={db_count})"
                )
                sem_gap = asyncio.Semaphore(5)
                now_cache_iso = datetime.utcnow().isoformat()

                # Recargar cache: found=1 (ya en Amazon) y found=0 (gap confirmado, TTL 6h)
                _gap_not_found_ttl = (datetime.utcnow() - timedelta(hours=6)).isoformat()
                gap_cache_found: set[str] = set()
                gap_cache_not_found: set[str] = set()
                async with aiosqlite.connect(DATABASE_PATH) as _cdb:
                    _cdb.row_factory = aiosqlite.Row
                    _cur = await _cdb.execute(
                        "SELECT sku_upper, found FROM amz_catalog_cache WHERE seller_id=? AND ("
                        "  (found=1 AND checked_at>?) OR (found=0 AND checked_at>?)"
                        ")",
                        (seller_id, _cache_cutoff, _gap_not_found_ttl),
                    )
                    for _r in await _cur.fetchall():
                        if _r["found"] == 1:
                            gap_cache_found.add(_r["sku_upper"])
                        else:
                            gap_cache_not_found.add(_r["sku_upper"])
                logger.info(
                    f"[AMZ Gap Scan] Cache: {len(gap_cache_found)} found=1, "
                    f"{len(gap_cache_not_found)} found=0 (gaps conocidos)"
                )

                async def _check_gap(g: dict):
                    bm_sku_u = g["sku"].upper()
                    if bm_sku_u in gap_cache_found:
                        # Cache: confirmado en Amazon → agregar a base_skus para que cleanup lo borre de gaps DB
                        amazon_base_skus.add(bm_sku_u)
                        return None
                    if bm_sku_u in gap_cache_not_found:
                        # Cache: confirmado como gap → no hacer llamada API, retornar directamente
                        return g
                    async with sem_gap:
                        variants = [bm_sku_u] + [bm_sku_u + sfx for sfx in _AMZ_CHECK_SUFFIXES]
                        for variant in variants:
                            try:
                                res = await client.get_listing_item(variant)
                            except Exception as _e:
                                # Error no-404 (429, 403, red) → benefit of doubt: asumir que existe
                                # Agregamos a base_skus para que cleanup borre fila vieja de amz_sku_gaps
                                logger.warning(
                                    f"[AMZ Gap Scan] _check_gap({bm_sku_u}) variante={variant} "
                                    f"error no-404 → benefit of doubt, asumiendo existe: {_e}"
                                )
                                amazon_base_skus.add(bm_sku_u)
                                return None  # No confirmar como gap
                            if res is not None:
                                async with aiosqlite.connect(DATABASE_PATH) as _db2:
                                    await _db2.execute(
                                        """INSERT INTO amz_catalog_cache (seller_id,sku_upper,found,checked_at)
                                           VALUES(?,?,1,?) ON CONFLICT(seller_id,sku_upper) DO UPDATE SET
                                           found=1,checked_at=excluded.checked_at""",
                                        (seller_id, bm_sku_u, now_cache_iso),
                                    )
                                    await _db2.commit()
                                amazon_base_skus.add(bm_sku_u)
                                logger.info(f"[AMZ Gap Scan] {bm_sku_u} confirmado en Amazon via {variant}")
                                return None
                        # Gap confirmado → cachear found=0 (TTL 6h) para no re-verificar en próximo scan
                        async with aiosqlite.connect(DATABASE_PATH) as _db3:
                            await _db3.execute(
                                """INSERT INTO amz_catalog_cache (seller_id,sku_upper,found,checked_at)
                                   VALUES(?,?,0,?) ON CONFLICT(seller_id,sku_upper) DO UPDATE SET
                                   found=0,checked_at=excluded.checked_at""",
                                (seller_id, bm_sku_u, now_cache_iso),
                            )
                            await _db3.commit()
                        logger.info(
                            f"[AMZ Gap Scan] {bm_sku_u} CONFIRMADO GAP "
                            f"(marketplace={client.marketplace_id}, variantes probadas={variants})"
                        )
                        return g

                gap_results = await asyncio.gather(*[_check_gap(g) for g in gaps], return_exceptions=True)
                gaps = [r for r in gap_results if r is not None and not isinstance(r, Exception)]
                logger.info(f"[AMZ Gap Scan] Gaps confirmados tras verificación individual: {len(gaps)}")

            # ── Guardar en DB ──────────────────────────────────────────────
            async with aiosqlite.connect(DATABASE_PATH) as db:
                # 1. Eliminar unlaunched/sin_precio que ya no tienen stock en BM
                if current_bm_skus:
                    placeholders = ",".join("?" * len(current_bm_skus))
                    await db.execute(
                        f"""DELETE FROM amz_sku_gaps
                            WHERE seller_id=? AND status IN ('unlaunched','sin_precio')
                            AND UPPER(sku) NOT IN ({placeholders})""",
                        [seller_id] + list(current_bm_skus),
                    )

                # 2. Eliminar unlaunched/sin_precio que ahora están activos en Amazon
                for chunk_s in range(0, len(amazon_base_skus), 500):
                    chunk = list(amazon_base_skus)[chunk_s:chunk_s + 500]
                    if chunk:
                        ph = ",".join("?" * len(chunk))
                        await db.execute(
                            f"""DELETE FROM amz_sku_gaps
                                WHERE seller_id=? AND status IN ('unlaunched','sin_precio')
                                AND UPPER(sku) IN ({ph})""",
                            [seller_id] + chunk,
                        )

                # 3. Upsert gaps (no toca los ignored ni launched)
                for g in gaps:
                    gap_status = g.pop("_gap_status", "unlaunched")
                    await db.execute(
                        """INSERT INTO amz_sku_gaps
                               (seller_id, sku, product_title, brand, model, category,
                                image_url, upc, avail_qty, cost_usd, cost_mxn,
                                suggested_price, margin_pct, last_scan, status)
                           VALUES
                               (:seller_id,:sku,:product_title,:brand,:model,:category,
                                :image_url,:upc,:avail_qty,:cost_usd,:cost_mxn,
                                :suggested_price,:margin_pct,:last_scan,:status)
                           ON CONFLICT(seller_id, sku) DO UPDATE SET
                               product_title=excluded.product_title,
                               brand=excluded.brand, model=excluded.model,
                               category=excluded.category, image_url=excluded.image_url,
                               upc=excluded.upc, avail_qty=excluded.avail_qty,
                               cost_usd=excluded.cost_usd, cost_mxn=excluded.cost_mxn,
                               suggested_price=excluded.suggested_price,
                               margin_pct=excluded.margin_pct, last_scan=excluded.last_scan,
                               status=excluded.status
                           WHERE amz_sku_gaps.status IN ('unlaunched','sin_precio')""",
                        {**g, "status": gap_status},
                    )

                # 4. Actualizar estado del scan
                await db.execute(
                    """INSERT INTO amz_gap_scan_status
                           (seller_id, status, finished_at, bm_total, amazon_active, gaps_found)
                       VALUES (?, 'done', ?, ?, ?, ?)
                       ON CONFLICT(seller_id) DO UPDATE SET
                           status='done', finished_at=excluded.finished_at,
                           bm_total=excluded.bm_total, amazon_active=excluded.amazon_active,
                           gaps_found=excluded.gaps_found, error=NULL""",
                    (seller_id, datetime.utcnow().isoformat(),
                     len(bm_items), amazon_active, len(gaps)),
                )
                await db.commit()

            logger.info(
                f"[AMZ Gap Scan] {seller_id}: {len(gaps)} gaps, "
                f"{len(bm_items)} BM SKUs, {amazon_active} Amazon activos"
            )

        except Exception as exc:
            logger.exception(f"[AMZ Gap Scan] Error para seller {seller_id}")
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.execute(
                    """INSERT INTO amz_gap_scan_status (seller_id, status, finished_at, error)
                       VALUES (?, 'error', ?, ?)
                       ON CONFLICT(seller_id) DO UPDATE SET
                           status='error', finished_at=excluded.finished_at, error=excluded.error""",
                    (seller_id, datetime.utcnow().isoformat(), str(exc)[:300]),
                )
                await db.commit()


async def run_gap_scan_all_accounts() -> None:
    """Ejecuta el gap scan para TODAS las cuentas Amazon registradas.
    Llamar desde el loop automático (amazon_listing_sync) o desde endpoints manuales.
    Cada cuenta corre secuencialmente para no saturar la API ni BM.
    """
    try:
        from app.services import token_store
        accounts = await token_store.get_all_amazon_accounts()
        if not accounts:
            return
        for acc in accounts:
            sid = acc.get("seller_id", "")
            if not sid:
                continue
            # Respetar lock: si ya hay un scan en progreso para esta cuenta, saltar
            if sid in _amz_scan_locks and _amz_scan_locks[sid].locked():
                logger.debug(f"[AMZ-AUTO-SCAN] seller={sid} ya tiene scan en progreso, saltando")
                continue
            try:
                await _run_amz_gap_scan(sid)
            except Exception as _e:
                logger.warning(f"[AMZ-AUTO-SCAN] Error en seller={sid}: {_e}")
    except Exception as e:
        logger.warning(f"[AMZ-AUTO-SCAN] Error global: {e}")


@router.post("/scan", response_class=JSONResponse)
async def trigger_amz_scan(
    seller_id: Optional[str] = Query(None),
):
    """Lanza el escaneo de gaps BM→Amazon en background."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    sid = client.seller_id
    if sid in _amz_scan_locks and _amz_scan_locks[sid].locked():
        return JSONResponse({"status": "running"})
    asyncio.create_task(_run_amz_gap_scan(sid))
    return JSONResponse({"status": "started", "seller_id": sid})


@router.get("/scan/status", response_class=JSONResponse)
async def get_amz_scan_status(
    seller_id: Optional[str] = Query(None),
):
    """Polling del estado del scan actual."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"status": "no_account"})
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM amz_gap_scan_status WHERE seller_id=?",
            (client.seller_id,),
        )
        row = await cur.fetchone()
    if not row:
        return JSONResponse({"status": "never", "seller_id": client.seller_id})
    return JSONResponse(dict(row))


# ── helpers ──────────────────────────────────────────────────────────────────

async def _get_gap_status(seller_id: str) -> dict:
    """Devuelve dict sku→status para el seller."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT sku, status, asin FROM amz_sku_gaps WHERE seller_id=?",
            (seller_id,),
        )
        rows = await cur.fetchall()
    return {r["sku"].upper(): {"status": r["status"], "asin": r["asin"]} for r in rows}


# ── 1. Buscar ASIN en catálogo Amazon ────────────────────────────────────────

@router.get("/search-catalog")
async def search_catalog(
    q: str = Query("", description="Keyword o título"),
    upc: str = Query("", description="UPC/EAN del producto"),
    seller_id: Optional[str] = Query(None),
):
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    try:
        if upc:
            items = await client.search_catalog(identifiers=[upc.strip()])
        elif q:
            items = await client.search_catalog(keyword=q.strip())
        else:
            return JSONResponse({"items": []})
        return {"items": items}
    except Exception as e:
        logger.exception("[AMZ Lanzar] search-catalog error")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ── Helpers: product types cache ─────────────────────────────────────────────

async def _get_valid_product_types(seller_id: str) -> list:
    """Returns cached product types for seller's marketplace. Refreshes if >7 days old."""
    import time as _time
    from app.services.token_store import get_product_types_cache, save_product_types_cache

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return []

    marketplace_id = client.marketplace_id
    types, cached_at = await get_product_types_cache(marketplace_id)

    if not types or (_time.time() - cached_at) > 7 * 86400:
        logger.info(f"[AMZ Types] Cache miss for {marketplace_id} — fetching from SP-API")
        types = await client.fetch_product_types()
        if types:
            await save_product_types_cache(marketplace_id, types)
        else:
            logger.warning(f"[AMZ Types] SP-API returned empty list for {marketplace_id}")

    return types


@router.get("/product-types")
async def get_product_types_endpoint(seller_id: str = ""):
    """Returns valid Amazon product types for the seller's marketplace (with 7-day cache)."""
    types = await _get_valid_product_types(seller_id)
    return {"product_types": types, "count": len(types), "marketplace": seller_id}


@router.post("/product-types/refresh")
async def refresh_product_types(request: Request):
    """Force-refreshes the product types cache for a seller's marketplace."""
    body = await request.json()
    seller_id = (body.get("seller_id") or "").strip()
    from app.services.token_store import save_product_types_cache

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)

    types = await client.fetch_product_types()
    if types:
        await save_product_types_cache(client.marketplace_id, types)
        return {"ok": True, "count": len(types), "marketplace_id": client.marketplace_id}
    return JSONResponse({"error": "SP-API returned empty list"}, status_code=500)


# ── UPC generation ────────────────────────────────────────────────────────────

def _generate_internal_upc(sku: str) -> str:
    """Generate deterministic UPC-A (12 digits) from SKU.
    Prefix 888 + 8 digits (SHA-256 hash of SKU) + 1 check digit (UPC-A Luhn).
    These are internal tracking codes — use with supplier_declared_has_product_identifier_exemption=true.
    """
    import hashlib as _hl
    h = int(_hl.sha256(sku.encode()).hexdigest(), 16)
    middle = str(h % 100_000_000).zfill(8)
    body = "888" + middle  # 11 digits
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    check = (10 - (total % 10)) % 10
    return body + str(check)


@router.post("/generate-upc")
async def generate_upc_endpoint(request: Request):
    """Generate (or retrieve) a deterministic internal UPC-A for a SKU."""
    body = await request.json()
    sku = (body.get("sku") or "").strip()
    if not sku:
        return JSONResponse({"error": "SKU requerido"}, status_code=400)
    from app.services.token_store import get_sku_upc, save_sku_upc
    existing = await get_sku_upc(sku)
    if existing:
        return {"upc": existing, "source": "existing", "sku": sku}
    upc = _generate_internal_upc(sku)
    await save_sku_upc(sku, upc, source="generated")
    logger.info(f"[UPC] Generated internal UPC {upc} for SKU {sku}")
    return {"upc": upc, "source": "generated", "sku": sku}


# ── 1b. Research product specs ───────────────────────────────────────────────

async def _research_product_specs(brand: str, model: str, upc: str, title: str, api_key: str) -> dict:
    """
    Uses Claude haiku + UPC ItemDB to research accurate product specs.
    Returns structured dict: weight_kg, dims_cm, connectivity, components, msrp, country, features.
    """
    import time as _time, re as _re
    import httpx as _hx
    import asyncio as _aio
    from app.services.token_store import get_product_specs_cache, save_product_specs_cache

    cache_key = f"{brand.lower()}|{model.lower()}".strip("|")
    specs, cached_at = await get_product_specs_cache(cache_key)
    if specs and (_time.time() - cached_at) < 30 * 86400:
        return specs

    found: dict = {}

    # ── Source 1: UPC ItemDB ───────────────────────────────────────────────────
    async def _upc_lookup() -> dict:
        q = upc or f"{brand} {model}"
        url = (f"https://api.upcitemdb.com/prod/trial/lookup?upc={upc}" if upc
               else f"https://api.upcitemdb.com/prod/trial/search?s={q.replace(' ','+')}&type=product")
        try:
            async with _hx.AsyncClient(timeout=6) as cl:
                r = await cl.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    items = (r.json().get("items") or [])
                    if items:
                        it = items[0]
                        res = {}
                        # Parse weight → kg
                        wraw = (it.get("weight") or "").strip().lower()
                        if wraw:
                            wm = _re.search(r"([\d.]+)\s*(lb|oz|kg|g)\b", wraw)
                            if wm:
                                v, u = float(wm.group(1)), wm.group(2)
                                res["weight_kg"] = round(v*0.453592 if u=="lb" else v*0.0283495 if u=="oz" else v/1000 if u=="g" else v, 2)
                        # Parse dimension → cm
                        draw = (it.get("dimension") or "").strip().lower()
                        if draw:
                            parts = _re.findall(r"([\d.]+)\s*(in|cm|mm)\b", draw)
                            if len(parts) >= 3:
                                def _cm(v, u): return round(float(v)*(2.54 if u=="in" else 0.1 if u=="mm" else 1), 1)
                                res["length_cm"] = _cm(*parts[0])
                                res["width_cm"]  = _cm(*parts[1])
                                res["height_cm"] = _cm(*parts[2])
                        if it.get("lowest_recorded_price"):
                            res["msrp_usd"] = float(it["lowest_recorded_price"])
                        if it.get("images"):
                            res["images"] = (it["images"] or [])[:3]
                        return res
        except Exception:
            pass
        return {}

    # ── Source 2: Claude haiku as research agent ───────────────────────────────
    async def _claude_research() -> dict:
        if not api_key or not (brand and model):
            return {}
        research_prompt = f"""You are a product data specialist. Research EXACT specs for: {brand} {model}

Use your training knowledge to provide precise specifications.

Return ONLY valid JSON (no markdown, no text):
{{
  "weight_kg": <decimal or null>,
  "length_cm": <decimal or null>,
  "width_cm": <decimal or null>,
  "height_cm": <decimal or null>,
  "connectivity": <["Wi-Fi","Bluetooth","HDMI","USB"] or []>,
  "included_components": <["Remote Control","Power Cable"] or []>,
  "special_features": <["Smart TV","HDR"] or []>,
  "msrp_usd": <decimal or null>,
  "country_of_origin": <"China" or null>,
  "display_size_in": <decimal or null>,
  "display_type": <"LED"/"QLED"/"OLED" or null>,
  "resolution": <"720p"/"1080p"/"4K"/"8K" or null>,
  "refresh_rate_hz": <integer or null>,
  "smart_device": <true/false/null>,
  "hdmi_ports": <integer or null>,
  "usb_ports": <integer or null>,
  "watts": <decimal or null>,
  "voltage_v": <"120V" or "220V" or "100-240V" or "120/240V" or null — research exact spec>,
  "model_year": <integer or null>,
  "color": <"Black" or null>
}}"""
        try:
            text = await _or_client.generate(research_prompt, max_tokens=600, model=_or_client.get_premium_model())
            m = _re.search(r'\{.*\}', text, _re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception:
            pass
        return {}

    # Run both in parallel
    upc_data, claude_data = await _aio.gather(_upc_lookup(), _claude_research())

    # Merge: UPC ItemDB takes priority for dimensions/weight (real data), Claude fills gaps
    for k, v in claude_data.items():
        if v is not None and k not in found:
            found[k] = v
    for k, v in upc_data.items():
        if v is not None:  # UPC data overwrites (more reliable)
            found[k] = v

    if found:
        await save_product_specs_cache(cache_key, found)
    return found


async def _get_product_schema(product_type: str, seller_id: str) -> dict:
    """Fetches and caches Amazon attribute schema for a product type (30-day TTL)."""
    import time as _time
    from app.services.token_store import get_schema_cache, save_schema_cache

    if not product_type or product_type == "PRODUCT":
        return {}  # PRODUCT is generic, no specific schema

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return {}

    cache_key = f"{product_type}|{client.marketplace_id}"
    schema, cached_at = await get_schema_cache(cache_key)
    if schema and (_time.time() - cached_at) < 30 * 86400:
        return schema

    schema = await client.fetch_product_type_schema(product_type)
    if schema:
        await save_schema_cache(cache_key, schema)
    return schema


@router.get("/product-schema/{product_type}")
async def get_product_schema_endpoint(product_type: str, seller_id: str = ""):
    """
    Returns the attribute schema for an Amazon product type.
    Includes: required attributes, optional attributes, property groups.
    Cached 30 days per type + marketplace.
    """
    schema = await _get_product_schema(product_type.upper(), seller_id)
    if not schema:
        return {"product_type": product_type, "required": [], "optional": [], "groups": {}}
    return schema


@router.get("/templates")
async def get_templates(marketplace_id: str = "ATVPDKIKX0DER"):
    """Returns all saved product type templates for a marketplace."""
    from app.services.token_store import list_product_type_templates
    return {"templates": await list_product_type_templates(marketplace_id)}


@router.get("/templates/{product_type}")
async def get_template(product_type: str, marketplace_id: str = "ATVPDKIKX0DER"):
    """Returns the template for a specific product type."""
    from app.services.token_store import get_product_type_template
    tmpl = await get_product_type_template(product_type.upper(), marketplace_id)
    if not tmpl:
        return JSONResponse({"error": "Template not found"}, status_code=404)
    return tmpl


@router.put("/templates/{product_type}")
async def upsert_template(product_type: str, request: Request):
    """Save or update a product type template manually."""
    from app.services.token_store import save_product_type_template
    body = await request.json()
    marketplace_id = body.get("marketplace_id", "ATVPDKIKX0DER")
    await save_product_type_template(product_type.upper(), marketplace_id, body)
    return {"ok": True, "product_type": product_type.upper()}


@router.post("/research-product")
async def research_product(request: Request):
    """Research product specs from UPC ItemDB + Claude knowledge base. Cached 30 days."""
    import os, base64 as _b64
    body = await request.json()
    brand = (body.get("brand") or "").strip()
    model = (body.get("model") or "").strip()
    upc   = (body.get("upc") or "").strip()
    title = (body.get("title") or "").strip()

    specs = await _research_product_specs(brand, model, upc, title, "")
    return {"specs": specs, "brand": brand, "model": model}


@router.get("/listing-status/{sku}")
async def get_listing_status_endpoint(sku: str, seller_id: str = ""):
    """Check the current status of a listing on Amazon SP-API."""
    from app.services.token_store import save_listing_status, get_listing_status
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)

    result = await client.get_listing_status(sku)
    # Cache the status
    await save_listing_status(
        seller_id=client.seller_id, sku=sku,
        status=result.get("status", "pending"),
        asin=result.get("asin"),
        issues=result.get("issues", []),
    )
    return result


# ── 2. Generar contenido IA — claude-sonnet-4-6, prompt completo ─────────────

@router.post("/generate-content")
async def generate_content(request: Request):
    body      = await request.json()
    title     = body.get("title", "")
    brand     = body.get("brand", "")
    model_num = (body.get("model") or "").strip()
    category  = body.get("category", "")
    upc       = (body.get("upc") or "").strip()
    price_val = float(body.get("price_mxn") or body.get("price") or 0)
    currency  = (body.get("currency") or "MXN").upper()
    seller_id = (body.get("seller_id") or "").strip()
    is_us     = currency == "USD"

    # ── Pre-research: get accurate specs BEFORE calling LLM for content ──────
    import asyncio as _aio
    researched: dict = {}
    try:
        researched = await _aio.wait_for(
            _research_product_specs(brand, model_num, upc, title, ""),
            timeout=15,
        )
    except Exception as _re:
        logger.warning(f"[generate-content] research failed: {_re}")

    # Build research context string for the prompt
    _res_lines = []
    if researched.get("weight_kg"):      _res_lines.append(f"Weight: {researched['weight_kg']} kg")
    if researched.get("length_cm"):      _res_lines.append(f"Dimensions: {researched['length_cm']} × {researched.get('width_cm',0)} × {researched.get('height_cm',0)} cm")
    if researched.get("display_size_in"):_res_lines.append(f"Screen: {researched['display_size_in']} inches")
    if researched.get("display_type"):   _res_lines.append(f"Display: {researched['display_type']}")
    if researched.get("resolution"):     _res_lines.append(f"Resolution: {researched['resolution']}")
    if researched.get("refresh_rate_hz"):_res_lines.append(f"Refresh rate: {researched['refresh_rate_hz']} Hz")
    if researched.get("connectivity"):   _res_lines.append(f"Connectivity: {', '.join(researched['connectivity'])}")
    if researched.get("hdmi_ports"):     _res_lines.append(f"HDMI ports: {researched['hdmi_ports']}")
    if researched.get("usb_ports"):      _res_lines.append(f"USB ports: {researched['usb_ports']}")
    if researched.get("included_components"): _res_lines.append(f"Includes: {', '.join(researched['included_components'])}")
    if researched.get("special_features"):    _res_lines.append(f"Features: {', '.join(researched['special_features'])}")
    if researched.get("msrp_usd"):       _res_lines.append(f"MSRP USD: {researched['msrp_usd']}")
    if researched.get("country_of_origin"): _res_lines.append(f"Country: {researched['country_of_origin']}")
    if researched.get("model_year"):     _res_lines.append(f"Model year: {researched['model_year']}")
    if researched.get("color"):          _res_lines.append(f"Color: {researched['color']}")
    _research_ctx = ("\n\nRESEARCHED SPECS (use these exact values — do not deviate):\n" + "\n".join(_res_lines)) if _res_lines else ""

    # Load product type template if product type already known (e.g. re-generation)
    _known_pt = (body.get("product_type") or "").strip().upper()
    _tmpl_hints = ""
    if _known_pt and _known_pt != "PRODUCT" and seller_id:
        try:
            from app.services.token_store import get_product_type_template as _get_tmpl
            from app.services.amazon_client import get_amazon_client as _gc
            _gc_tmp = await _gc(seller_id=seller_id)
            _mk = _gc_tmp.marketplace_id if _gc_tmp else "ATVPDKIKX0DER"
            _tmpl = await _aio.wait_for(_get_tmpl(_known_pt, _mk), timeout=4)
            if _tmpl and _tmpl.get("ai_hints"):
                _tmpl_hints = f"\n\nPRODUCT TYPE TEMPLATE HINTS ({_known_pt}):\n{_tmpl['ai_hints']}"
        except Exception:
            pass

    # Fetch valid product types from Amazon API (cached 7 days)
    valid_types: list = []
    if seller_id:
        try:
            valid_types = await _aio.wait_for(_get_valid_product_types(seller_id), timeout=8)
        except Exception as _te:
            logger.warning(f"[generate-content] product types fetch failed: {_te}")

    # Build product type instruction based on real Amazon types or fallback
    if valid_types:
        _types_str = ", ".join(valid_types)
        _pt_instruction_en = (
            f"PRODUCT_TYPE — Select EXACTLY one from this official Amazon list for this marketplace:\n"
            f"{_types_str}\n"
            "Rules: Use the EXACT string from the list. If multiple apply, pick the most specific. "
            "If nothing specific fits, use PRODUCT."
        )
        _pt_instruction_es = (
            f"PRODUCT_TYPE — Selecciona EXACTAMENTE uno de esta lista oficial Amazon para este marketplace:\n"
            f"{_types_str}\n"
            "Reglas: Usa la cadena EXACTA de la lista. Si varios aplican, elige el más específico. "
            "Si ninguno específico aplica, usa PRODUCT."
        )
    else:
        # Fallback hardcoded list
        _pt_instruction_en = """PRODUCT_TYPE — CRITICAL: must be an EXACT valid Amazon US product type string. Use ONLY these:
• TVs: TELEVISION | Monitors: COMPUTER_MONITOR | Vacuums (all kinds): VACUUM
• Bulbs: LIGHT_BULB | Fans (all kinds): FAN | ACs: AIR_CONDITIONER
• Speakers/soundbars: SPEAKER | Headphones/earbuds: HEADPHONES
• Laptops: LAPTOP | Desktops: PERSONAL_COMPUTER | Tablets: TABLET
• Coffee makers: COFFEE_MAKER | Blenders: BLENDER | Microwaves: MICROWAVE_OVEN
• Hair dryers/tools: HAIR_DRYER | Shavers/trimmers: ELECTRIC_SHAVER
• Cameras: CAMERA | Security cameras: SECURITY_CAMERA | Printers: PRINTER
• Smart watches: SMARTWATCH | Power banks: POWER_BANK | Keyboards: KEYBOARD | Mice: MOUSE
• Drills/power tools: POWER_DRILL | Light fixtures/lamps: LIGHT_FIXTURE
• Generic fallback: PRODUCT (ONLY if no specific type fits)
Choose the most specific valid type. NEVER invent or modify these strings."""
        _pt_instruction_es = """PRODUCT_TYPE — CRÍTICO: debe ser un tipo de producto Amazon MX exacto y válido (SCREAMING_SNAKE_CASE):
• TVs: TELEVISION | Monitores: COMPUTER_MONITOR | Aspiradoras (todo tipo): VACUUM
• Focos: LIGHT_BULB | Ventiladores (todo tipo): FAN | Climatizadores: AIR_CONDITIONER
• Bocinas/soundbars: SPEAKER | Audífonos/earbuds: HEADPHONES
• Laptops: LAPTOP | Computadoras: PERSONAL_COMPUTER | Tablets: TABLET
• Cafeteras: COFFEE_MAKER | Licuadoras: BLENDER | Microondas: MICROWAVE_OVEN
• Secadoras/plancha cabello: HAIR_DRYER | Rasuradores: ELECTRIC_SHAVER
• Cámaras: CAMERA | Cámaras seguridad: SECURITY_CAMERA | Impresoras: PRINTER
• Smartwatches: SMARTWATCH | Power banks: POWER_BANK | Teclados: KEYBOARD | Mouse: MOUSE
• Taladros/herramientas: POWER_DRILL | Lámparas/fixtures: LIGHT_FIXTURE
• Fallback genérico: PRODUCT (solo si ningún tipo específico aplica)
Elige el tipo más específico. NUNCA inventes ni modifiques estas cadenas."""

    ctx_parts = []
    if model_num:
        ctx_parts.append(f"Model: {model_num}" if is_us else f"Modelo: {model_num}")
    if upc:
        ctx_parts.append(f"UPC/EAN: {upc}")
    if price_val > 0:
        price_str = f"${price_val:.2f} USD" if is_us else f"${price_val:,.0f} MXN"
        ctx_parts.append(f"Sale price: {price_str}" if is_us else f"Precio de venta: {price_str}")
    extra_ctx = "\n".join(ctx_parts)

    try:
        if is_us:
            prompt = f"""You are an Amazon US listing optimization expert with deep knowledge of SEO, CRO, and Amazon US policies 2024.

Create complete, high-converting listing content for this product. ALL content (title, bullets, description, keywords) MUST be in ENGLISH.

Product title: {title}
Brand: {brand}
Category: {category}
{extra_ctx}{_research_ctx}{_tmpl_hints}
Marketplace: Amazon US (amazon.com) — English-speaking US buyers

━━━ CRITICAL RULES ━━━

⚠ WARRANTY — ABSOLUTE RULE:
• NEVER mention manufacturer warranty (1 year, official warranty, Sony/Samsung warranty, etc.)
• We are RESELLERS, not authorized distributors
• The warranty we offer is "90 days seller warranty"
• Correct: "90-day seller warranty with dedicated post-purchase support."
• PROHIBITED: "Official Sony 1-year warranty" / "Manufacturer warranty"

TITLE (max 200 chars):
• Format: [Brand] [Model] [Main Feature] [Use/Compatibility] [Key Spec]
• First 80 chars = highest search-volume keywords
• NO emojis, NO "deal/free/best/top", NO trailing punctuation, NO excessive CAPS
• Include units where applicable (Watts, Inches, Liters, etc.)
• Write in English only

BULLETS (exactly 5, max 200 chars each):
• Start in CAPS with the key feature
• Structure: KEY FEATURE: Measurable spec + concrete user benefit
• Bullet 1: Main differentiator / USP
• Bullet 2: Most-searched technical specs
• Bullet 3: Compatibility / use cases
• Bullet 4: Package contents / what's included
• Bullet 5: Support — WARRANTY RULE: "90-day seller warranty. Contact us before and after purchase."
• Write in English only

DESCRIPTION (max 2000 chars):
• Paragraph 1: Problem it solves + who needs it
• Paragraph 2: Key features and specifications in detail
• Paragraph 3: Call to action + final value proposition
• Natural, flowing English text — don't repeat title or bullets verbatim

BACKEND KEYWORDS (max 249 characters — count characters not bytes):
• ONLY words NOT appearing in title or bullets
• Mix of English search terms, synonyms, alternate uses, common misspellings
• Separated by spaces (NOT commas)

{_pt_instruction_en}

COLOR:
• Main product color in English (e.g., Black, White, Silver, Blue)
• If not applicable or unclear: ""

PHYSICAL SPECIFICATIONS (use your product knowledge to estimate):
• weight_kg: Weight in kilograms (decimal, e.g., 6.7). Estimate if uncertain.
• display_size_in: Screen size in inches (TVs, monitors, tablets). Integer or decimal.
• length_cm: Product length in cm (without packaging)
• width_cm: Width in cm
• height_cm: Height/depth in cm
• country_of_origin: Manufacturing country in English (e.g., "China", "Mexico", "South Korea", "Vietnam", "Taiwan", "United States")
• Use null for unknowns.

TECHNICAL ATTRIBUTES (for TELEVISION and COMPUTER_MONITOR product types):
• display_type: Screen technology — one of: "LED", "QLED", "OLED", "Mini LED", "LCD", "QNED", null
• resolution: Resolution — one of: "720p", "1080p", "4K", "8K", null
• smart_tv_flag: true if Smart TV, false if not, null if N/A
• refresh_rate: Refresh rate in Hz as integer (e.g., 60, 120, 240), null if N/A
• mounting_type: One of "Tabletop", "Wall Mount", "Tabletop, Wall Mount", null
• item_type_keyword: category keyword (e.g., "televisions", "computer-monitors", "light-bulbs", "speakers"), null if unknown
• total_hdmi_ports: Integer number of HDMI ports (e.g., 3), null if N/A
• usb_port_count: Integer number of USB ports (e.g., 2), null if N/A
• special_feature: List of key features (e.g., ["Smart TV", "Built-In WiFi", "HDR", "Dolby Vision"]), [] if N/A
• included_components: List of box contents (e.g., ["Remote Control", "Power Cable", "Stand", "User Manual"]), [] if N/A
• connectivity_technology: ONLY valid values: "Bluetooth", "Wi-Fi", "USB", "HDMI", "Infrared", "Auxiliary". For corded-only appliances (vacuums, fans) with NO wireless: return []. Connectivity = wireless tech only, NOT the power cord.
• model_year: Model year as integer (e.g., 2024), null if unknown
• warranty_description: "90 days seller warranty"
• list_price_msrp: Suggested MSRP in USD (e.g., if price is 533.32, MSRP might be 599.99), null if unable to estimate
• watts: Power consumption in watts as decimal (e.g., 500.0 for a vacuum), null if N/A
• voltage_v: Actual operating voltage string — research the specific model spec (e.g., "120V", "220V", "100-240V", "120/240V"). null only if truly unknown.

PRODUCT-SPECIFIC REQUIRED ATTRIBUTES — Fill ALL that apply to THIS product:
• surface_type: Floor/surface type the product works on. Vacuums (stored as surface_recommendation in Amazon): "Bare Floor"/"Carpet"/"Hard Floor"/"Hardwoods"/"Laminate"/"Ceramic tile". Use "Multi-Surface" for multi-surface vacuums. null if N/A.
• form_factor: Physical form. Vacuums: "Stick"/"Upright"/"Handheld"/"Robot"/"Canister". Fans: "Tower"/"Box"/"Desk". Speakers: "Smart Speaker"/"Portable". null if N/A.
• power_source_type: "Corded Electric"/"Battery Powered"/"Solar Powered"/"USB"/"AC Adapter". Required for most appliances.
• bag_type: "Bagless"/"Bagged". Vacuums only. null otherwise.
• filter_type: "HEPA"/"Foam"/"Washable"/"Replaceable Filter". Vacuums/purifiers only. null otherwise.
• specific_uses_for_product: List of use cases (e.g., ["Pet Hair", "Hard Floor", "Carpet", "Home Use"]). [] if N/A.
• material_type: Main construction material. For Amazon MX use SPANISH values: "Plástico"/"Metal"/"Aluminio"/"Acero inoxidable"/"Madera"/"Vidrio"/"Caucho"/"Silicona". Never use English for MX. Required for appliances, home goods, pest control, lighting.
• number_of_settings: Number of speed/power settings as integer (fans, mixers, etc.). null if N/A.
• noise_level_db: Noise level in decibels as decimal. null if unknown.
• item_form: Physical form of consumable product (e.g., "Liquid", "Powder", "Capsule"). null if N/A.
• capacity: Container/tank capacity as decimal. null if N/A.
• capacity_unit: Unit for capacity ("Liters", "Gallons", "Ounces", "Cups", "Quarts"). null if N/A.
• compatible_devices: List of compatible devices/systems (e.g., ["Alexa", "Google Home"]). [] if N/A.
• pattern: Design pattern (e.g., "Solid", "Striped"). null if N/A.
• finish_type: Surface finish (e.g., "Matte", "Glossy", "Brushed"). null if N/A.

━━━ RESPOND WITH VALID JSON ONLY (no markdown, no extra text) ━━━
{{
  "title": "...",
  "bullets": ["...", "...", "...", "...", "..."],
  "description": "...",
  "keywords_backend": "...",
  "product_type": "...",
  "color": "...",
  "weight_kg": null,
  "display_size_in": null,
  "length_cm": null,
  "width_cm": null,
  "height_cm": null,
  "country_of_origin": null,
  "display_type": null,
  "resolution": null,
  "smart_tv_flag": null,
  "refresh_rate": null,
  "mounting_type": null,
  "item_type_keyword": null,
  "total_hdmi_ports": null,
  "usb_port_count": null,
  "special_feature": [],
  "included_components": [],
  "connectivity_technology": [],
  "model_year": null,
  "warranty_description": null,
  "list_price_msrp": null,
  "watts": null,
  "voltage_v": null,
  "surface_type": null,
  "form_factor": null,
  "power_source_type": null,
  "bag_type": null,
  "filter_type": null,
  "specific_uses_for_product": [],
  "material_type": null,
  "number_of_settings": null,
  "noise_level_db": null,
  "item_form": null,
  "capacity": null,
  "capacity_unit": null,
  "compatible_devices": [],
  "pattern": null,
  "finish_type": null
}}"""
        else:
            prompt = f"""Eres un experto en optimización de listings para Amazon México con dominio de SEO, CRO y las políticas de Amazon MX 2024.

Crea contenido completo y de alta conversión para este producto:

Título catálogo: {title}
Marca: {brand}
Categoría: {category}
{extra_ctx}{_research_ctx}{_tmpl_hints}
Marketplace: Amazon México (amazon.com.mx) — compradores en español mexicano

━━━ REGLAS CRÍTICAS (cumplirlas al pie de la letra) ━━━

⚠ GARANTÍA — REGLA ABSOLUTA:
• NUNCA mencionar garantía del fabricante (1 año, garantía oficial, garantía Sony/Samsung/etc.)
• Somos REVENDEDORES, no distribuidores autorizados
• La garantía que ofrecemos es "3 meses directamente con el vendedor"
• Ejemplo correcto: "3 meses de garantía con el vendedor. Atención personalizada post-venta."
• Ejemplo PROHIBIDO: "Garantía oficial Sony 1 año" / "Garantía del fabricante"

TÍTULO (máx 200 chars):
• Formato: [Marca] [Modelo] [Característica principal] [Uso/Compatibilidad] [Especificación clave]
• Primeros 80 chars = keywords de mayor volumen de búsqueda
• NO emojis, NO "oferta/gratis/mejor/top", NO signos finales, NO mayúsculas excesivas
• Incluir unidades si aplica (Watts, Pulgadas, Litros, etc.)

BULLETS (exactamente 5, máx 200 chars c/u):
• Empiezan en MAYÚSCULAS con la feature clave
• Estructura: FEATURE CLAVE: Especificación medible + beneficio concreto al usuario
• Bullet 1: Diferenciador principal / USP
• Bullet 2: Especificaciones técnicas más buscadas
• Bullet 3: Compatibilidad / casos de uso
• Bullet 4: Contenido del paquete / lo que incluye
• Bullet 5: Por qué elegirlo / soporte al cliente / propuesta de valor — NO mencionar garantía del fabricante. Somos revendedores: la garantía es "3 meses directamente con el vendedor". Ejemplo: "SOPORTE GARANTIZADO: 3 meses de garantía directamente con el vendedor. Contáctanos para cualquier duda antes y después de tu compra."

DESCRIPCIÓN (máx 2000 chars):
• Párrafo 1: Problema que resuelve + quién lo necesita
• Párrafo 2: Características y especificaciones clave en detalle
• Párrafo 3: Call to action + propuesta de valor final
• Texto natural, sin repetir exactamente el título o bullets

KEYWORDS BACKEND (máx 249 caracteres, NO bytes — caracteres):
• Solo palabras que NO aparecen en título ni bullets
• Mezcla español + inglés + variaciones comunes
• Términos genéricos, sinónimos, usos alternativos, errores ortográficos frecuentes
• Separados por espacios (NO comas)

{_pt_instruction_es}

COLOR:
• Color principal del producto en inglés (ej: Black, White, Silver, Blue)
• Si no aplica o no está claro: ""

ESPECIFICACIONES FÍSICAS (usa tu conocimiento del modelo/marca para estimar):
• weight_kg: Peso en kilogramos (número decimal, ej: 6.7). Estima si no lo sabes con certeza.
• display_size_in: Tamaño de pantalla en pulgadas (TVs, monitores, tablets). Número entero o decimal.
• length_cm: Largo del producto en cm (sin empaque)
• width_cm: Ancho en cm
• height_cm: Alto/profundidad en cm
• country_of_origin: País de fabricación en inglés (ej: "China", "Mexico", "South Korea", "Vietnam", "Taiwan", "United States")
• Si no conoces un dato específico, usa null.

ATRIBUTOS TÉCNICOS (para product_type TELEVISION y COMPUTER_MONITOR):
• display_type: Tecnología de pantalla — uno de: "LED", "QLED", "OLED", "Mini LED", "LCD", "QNED", null
• resolution: Resolución — uno de: "720p", "1080p", "4K", "8K", null
• smart_tv_flag: true si es Smart TV, false si no, null si no aplica
• refresh_rate: Tasa de refresco en Hz como número entero (ej: 60, 120, 240), null si no aplica
• mounting_type: Uno de "Tabletop", "Wall Mount", "Tabletop, Wall Mount", null
• item_type_keyword: keyword de categoría (ej: "televisions", "computer-monitors", "light-bulbs", "speakers", "air-conditioners"), null si no sabes
• total_hdmi_ports: Número entero de puertos HDMI (ej: 3), null si no aplica
• usb_port_count: Número entero de puertos USB (ej: 2), null si no aplica
• special_feature: Lista de características destacadas (ej: ["Smart TV", "Built-In WiFi", "HDR", "Dolby Vision"]), [] si no aplica
• included_components: Lista de lo que incluye la caja (ej: ["Remote Control", "Power Cable", "Stand", "User Manual"]), [] si no aplica
• connectivity_technology: Solo valores válidos: "Bluetooth", "Wi-Fi", "USB", "HDMI", "Infrared", "Auxiliary". Para electrodomésticos solo alámbricos (aspiradoras, ventiladores) SIN inalámbrico: devolver []. Conectividad = tecnología inalámbrica únicamente, NO el cable de poder.
• model_year: Año del modelo como entero (ej: 2024), null si no sabes
• warranty_description: Descripción de garantía en inglés (ej: "90 days seller warranty"), null si no aplica
• list_price_msrp: Precio MSRP sugerido en la misma moneda (null si no puedes estimar)
• watts: Consumo en vatios (ej: 500.0), null si N/A
• voltage_v: Voltaje real del modelo — INVESTIGAR (ej: "120V", "220V", "100-240V"). null solo si desconocido.

ATRIBUTOS REQUERIDOS POR CATEGORÍA — Llena TODOS los que apliquen al producto:
• surface_type: Tipo de superficie. Aspiradoras: "Multi-Surface"/"Carpet"/"Hardwood". null si N/A.
• form_factor: Forma física. Aspiradoras: "Stick"/"Upright"/"Handheld"/"Robot". Ventiladores: "Tower"/"Box". null si N/A.
• power_source_type: "Corded Electric"/"Battery Powered"/"Solar Powered"/"USB"/"AC Adapter". Requerido para electrodomésticos.
• bag_type: "Bagless"/"Bagged". Solo aspiradoras. null si no aplica.
• filter_type: "HEPA"/"Foam"/"Washable"/"Replaceable Filter". Aspiradoras/purificadores. null si N/A.
• specific_uses_for_product: Lista de usos (ej: ["Pet Hair", "Hard Floor", "Home Use"]). [] si N/A.
• material_type: Material principal en ESPAÑOL para Amazon MX: "Plástico"/"Metal"/"Aluminio"/"Acero inoxidable"/"Madera"/"Vidrio"/"Caucho"/"Silicona". Requerido para electrodomésticos, hogar, control de plagas, iluminación.
• number_of_settings: Número de velocidades/niveles (ventiladores, batidoras). null si N/A.
• noise_level_db: Nivel de ruido en decibeles. null si desconocido.
• item_form: Forma del consumible ("Liquid", "Powder", "Capsule"). null si N/A.
• capacity: Capacidad del contenedor (decimal). null si N/A.
• capacity_unit: Unidad de capacidad ("Liters", "Gallons", "Cups"). null si N/A.
• compatible_devices: Dispositivos compatibles (ej: ["Alexa", "Google Home"]). [] si N/A.
• pattern: Patrón de diseño ("Solid", "Striped"). null si N/A.
• finish_type: Acabado superficial ("Matte", "Glossy", "Brushed"). null si N/A.

━━━ RESPONDE SOLO CON JSON VÁLIDO (sin markdown, sin texto extra) ━━━
{{
  "title": "...",
  "bullets": ["...", "...", "...", "...", "..."],
  "description": "...",
  "keywords_backend": "...",
  "product_type": "...",
  "color": "...",
  "weight_kg": null,
  "display_size_in": null,
  "length_cm": null,
  "width_cm": null,
  "height_cm": null,
  "country_of_origin": null,
  "display_type": null,
  "resolution": null,
  "smart_tv_flag": null,
  "refresh_rate": null,
  "mounting_type": null,
  "item_type_keyword": null,
  "total_hdmi_ports": null,
  "usb_port_count": null,
  "special_feature": [],
  "included_components": [],
  "connectivity_technology": [],
  "model_year": null,
  "warranty_description": null,
  "list_price_msrp": null,
  "watts": null,
  "voltage_v": null,
  "surface_type": null,
  "form_factor": null,
  "power_source_type": null,
  "bag_type": null,
  "filter_type": null,
  "specific_uses_for_product": [],
  "material_type": null,
  "number_of_settings": null,
  "noise_level_db": null,
  "item_form": null,
  "capacity": null,
  "capacity_unit": null,
  "compatible_devices": [],
  "pattern": null,
  "finish_type": null
}}"""

        text = await _or_client.generate(prompt, max_tokens=2048, model=_or_client.get_premium_model())

        start = text.index("{")
        end   = text.rindex("}") + 1
        data  = json.loads(text[start:end])
        # Asegurar que keywords_backend no supere 249 chars
        if len(data.get("keywords_backend", "")) > 249:
            data["keywords_backend"] = data["keywords_backend"][:249].rsplit(" ", 1)[0]
        # Merge researched specs: fill gaps the AI may have left null
        for _rk, _rv in researched.items():
            if _rv is not None and data.get(_rk) in (None, "", 0, []):
                data[_rk] = _rv
        # Expose researched data so wizard can pre-fill fields immediately
        data["_researched"] = researched
        # Fetch schema for the detected product type (async, 30-day cache)
        _pt = (data.get("product_type") or "").strip().upper()
        if _pt and _pt != "PRODUCT" and seller_id:
            try:
                import asyncio as _aio2
                _schema = await _aio2.wait_for(_get_product_schema(_pt, seller_id), timeout=10)
                data["_schema"] = _schema
            except Exception:
                data["_schema"] = {}
        return data
    except Exception as e:
        logger.warning(f"[AMZ Lanzar] generate-content error: {e}")
        return {
            "title": title, "bullets": [], "description": "",
            "keywords_backend": "", "product_type": "PRODUCT", "color": "",
            "error": str(e)[:200],
        }


# ── 3. Crear listing (Flujo 1 o Flujo 2) ────────────────────────────────────

@router.post("/create")
async def create_listing(request: Request):
    body = await request.json()
    seller_id        = body.get("seller_id")
    sku              = (body.get("sku") or "").strip()
    asin             = (body.get("asin") or "").strip()
    price            = float(body.get("price") or 0)
    condition        = body.get("condition", "new_new")
    fulfillment      = body.get("fulfillment", "FBM")
    quantity         = int(body.get("quantity") or 0)
    title            = (body.get("title") or "")[:200]
    bullets          = body.get("bullets") or []
    description      = (body.get("description") or "")[:2000]
    keywords_backend = (body.get("keywords_backend") or "")[:249]
    _PT_RAW          = (body.get("product_type") or "PRODUCT").strip().upper()
    # Normalize invalid/variant product type strings to valid Amazon SP-API values
    _PT_MAP = {
        "STICK_VACUUM":"VACUUM","STICK_VACUUM_CLEANER":"VACUUM",
        "UPRIGHT_VACUUM":"VACUUM","UPRIGHT_VACUUM_CLEANER":"VACUUM",
        "ROBOT_VACUUM":"VACUUM","ROBOT_VACUUM_CLEANER":"VACUUM",
        "HANDHELD_VACUUM":"VACUUM","CANISTER_VACUUM":"VACUUM","WET_DRY_VACUUM":"VACUUM",
        "CORDLESS_VACUUM":"VACUUM","BAGLESS_VACUUM":"VACUUM",
        "CEILING_FAN":"FAN","TOWER_FAN":"FAN","BOX_FAN":"FAN",
        "TABLE_FAN":"FAN","FLOOR_FAN":"FAN","WINDOW_FAN":"FAN","DESK_FAN":"FAN",
        "MONITOR":"COMPUTER_MONITOR","TV":"TELEVISION","SMART_TV":"TELEVISION",
        "WINDOW_AIR_CONDITIONER":"AIR_CONDITIONER","PORTABLE_AIR_CONDITIONER":"AIR_CONDITIONER",
        "ESPRESSO_MACHINE":"COFFEE_MAKER","DRIP_COFFEE_MAKER":"COFFEE_MAKER","POD_COFFEE_MAKER":"COFFEE_MAKER",
        "SOUNDBAR":"SPEAKER","SUBWOOFER":"SPEAKER","SMART_SPEAKER":"SPEAKER","BLUETOOTH_SPEAKER":"SPEAKER",
        "EARBUDS":"HEADPHONES","EARPHONES":"HEADPHONES","WIRELESS_HEADPHONES":"HEADPHONES","GAMING_HEADSET":"HEADPHONES",
        "FITNESS_TRACKER":"SMARTWATCH","SMART_WATCH":"SMARTWATCH",
        "POWER_BANK":"POWER_BANK","PORTABLE_CHARGER":"POWER_BANK",
        "SECURITY_CAMERA":"SECURITY_CAMERA","DOORBELL_CAMERA":"SECURITY_CAMERA",
        "ELECTRIC_TOOTHBRUSH":"ELECTRIC_SHAVER","HAIR_STRAIGHTENER":"HAIR_DRYER","CURLING_IRON":"HAIR_DRYER",
    }
    product_type     = _PT_MAP.get(_PT_RAW, _PT_RAW) or "PRODUCT"
    photo_urls       = [u.strip() for u in (body.get("photo_urls") or []) if (u or "").strip()]
    currency         = (body.get("currency") or "MXN").upper()
    # Atributos adicionales
    brand            = (body.get("brand") or "").strip()
    model_number     = (body.get("model_number") or "").strip()
    color            = (body.get("color") or "").strip()
    weight_kg        = float(body.get("weight_kg") or 0)
    display_size_in  = float(body.get("display_size_in") or 0)
    length_cm        = float(body.get("length_cm") or 0)
    width_cm         = float(body.get("width_cm") or 0)
    height_cm        = float(body.get("height_cm") or 0)
    # Atributos requeridos por Amazon (comprehensive)
    # Amazon TELEVISION acepta códigos ISO 3166-1 alpha-2, no nombres completos
    _COUNTRY_ISO = {
        "china": "CN", "cn": "CN",
        "mexico": "MX", "mx": "MX",
        "south korea": "KR", "korea": "KR", "kr": "KR",
        "vietnam": "VN", "vn": "VN",
        "taiwan": "TW", "tw": "TW",
        "united states": "US", "usa": "US", "us": "US",
        "japan": "JP", "jp": "JP",
        "thailand": "TH", "th": "TH",
        "india": "IN", "in": "IN",
        "germany": "DE", "de": "DE",
        "malaysia": "MY", "my": "MY",
        "indonesia": "ID", "id": "ID",
        "philippines": "PH", "ph": "PH",
    }
    _raw_country = (body.get("country_of_origin") or "China").strip()
    country_of_origin = _COUNTRY_ISO.get(_raw_country.lower(), _raw_country)
    item_type_keyword    = (body.get("item_type_keyword") or "").strip()
    display_type         = (body.get("display_type") or "").strip()
    resolution           = (body.get("resolution") or "").strip()
    refresh_rate         = body.get("refresh_rate")     # int or None
    mounting_type        = (body.get("mounting_type") or "").strip()
    special_feature      = body.get("special_feature") or []
    included_components  = body.get("included_components") or []
    total_hdmi_ports     = body.get("total_hdmi_ports") or body.get("hdmi_port_count")
    usb_port_count       = body.get("usb_port_count")
    connectivity_tech    = body.get("connectivity_technology") or []
    model_year           = body.get("model_year")
    warranty_desc        = (body.get("warranty_description") or "").strip()
    list_price_msrp      = float(body.get("list_price_msrp") or 0)
    aspect_ratio         = (body.get("aspect_ratio") or "").strip()
    # Package dims/weight (separate from product dims — includes box)
    pkg_weight_kg        = float(body.get("pkg_weight_kg") or 0)
    pkg_length_cm        = float(body.get("pkg_length_cm") or 0)
    pkg_width_cm         = float(body.get("pkg_width_cm") or 0)
    pkg_height_cm        = float(body.get("pkg_height_cm") or 0)
    upc                  = (body.get("upc") or "").strip()
    # Extended product-type-specific attributes from AI
    surface_type         = (body.get("surface_type") or "").strip()
    form_factor          = (body.get("form_factor") or "").strip()
    power_source_type    = (body.get("power_source_type") or "").strip()
    bag_type             = (body.get("bag_type") or "").strip()
    filter_type_attr     = (body.get("filter_type") or "").strip()
    specific_uses        = body.get("specific_uses_for_product") or []
    material_type        = (body.get("material_type") or "").strip()
    num_settings         = body.get("number_of_settings")
    noise_level_db       = body.get("noise_level_db")
    capacity_val         = body.get("capacity")
    capacity_unit_attr   = (body.get("capacity_unit") or "").strip()
    compatible_devs      = body.get("compatible_devices") or []
    pattern_attr         = (body.get("pattern") or "").strip()
    finish_type_attr     = (body.get("finish_type") or "").strip()
    voltage_v            = (body.get("voltage_v") or "").strip()

    if not sku:
        return JSONResponse({"error": "SKU requerido"}, status_code=400)
    if price <= 0:
        return JSONResponse({"error": "Precio inválido"}, status_code=400)

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)

    if asin:
        # ── Flujo 1 — match a ASIN existente ──────────────────────────────
        requirements = "LISTING_OFFER_ONLY"
        attributes: dict = {
            "condition_type": [{"value": condition, "marketplace_id": client.marketplace_id}],
            "purchasable_offer": [{
                "currency": currency,
                "our_price": [{"schedule": [{"value_with_tax": price}]}],
                "marketplace_id": client.marketplace_id,
            }],
            "merchant_suggested_asin": [{"value": asin, "marketplace_id": client.marketplace_id}],
        }
        if fulfillment == "FBM" and quantity > 0:
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": quantity,
            }]
        elif fulfillment == "FBA":
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "AMAZON_NA",
            }]
    else:
        # ── Flujo 2 — producto nuevo ───────────────────────────────────────
        if not title:
            return JSONResponse({"error": "Título requerido para producto nuevo"}, status_code=400)
        requirements = "LISTING"
        attributes = {
            "condition_type": [{"value": condition, "marketplace_id": client.marketplace_id}],
            "purchasable_offer": [{
                "currency": currency,
                "our_price": [{"schedule": [{"value_with_tax": price}]}],
                "marketplace_id": client.marketplace_id,
            }],
            "item_name": [{"value": title, "marketplace_id": client.marketplace_id}],
        }
        if bullets:
            attributes["bullet_point"] = [
                {"value": b, "marketplace_id": client.marketplace_id}
                for b in bullets[:5] if b
            ]
        if description:
            attributes["product_description"] = [
                {"value": description, "marketplace_id": client.marketplace_id}
            ]
        if keywords_backend:
            attributes["generic_keyword"] = [
                {"value": keywords_backend, "marketplace_id": client.marketplace_id}
            ]
        if photo_urls:
            attributes["main_product_image_locator"] = [
                {"media_location": photo_urls[0], "marketplace_id": client.marketplace_id}
            ]
            for _i, _url in enumerate(photo_urls[1:8], 1):
                if _url:
                    attributes[f"other_product_image_locator_{_i}"] = [
                        {"media_location": _url, "marketplace_id": client.marketplace_id}
                    ]
        if brand:
            attributes["brand"] = [{"value": brand, "marketplace_id": client.marketplace_id}]
            attributes["manufacturer"] = [{"value": brand, "marketplace_id": client.marketplace_id}]
        if model_number:
            attributes["model_number"] = [{"value": model_number, "marketplace_id": client.marketplace_id}]
            attributes["part_number"] = [{"value": model_number, "marketplace_id": client.marketplace_id}]
        if upc:
            attributes["externally_assigned_product_identifier"] = [{
                "type": "upc",
                "value": upc,
                "marketplace_id": client.marketplace_id,
            }]
        if color:
            attributes["color"] = [{"value": color, "marketplace_id": client.marketplace_id}]
        # Unit selection: VACUUM_CLEANER US marketplace requires inches/pounds
        # TELEVISION/MONITOR: centimeters/kilograms work fine
        _IS_US_APPLIANCE = product_type in ("VACUUM_CLEANER","VACUUM","FAN","AIR_CONDITIONER",
            "COFFEE_MAKER","BLENDER","MICROWAVE_OVEN","HAIR_DRYER","ELECTRIC_SHAVER") \
            and client.marketplace_id == "ATVPDKIKX0DER"
        _weight_unit = "pounds" if _IS_US_APPLIANCE else "kilograms"
        _weight_val  = round(weight_kg * 2.20462, 2) if _IS_US_APPLIANCE else weight_kg
        _dim_unit    = "inches" if _IS_US_APPLIANCE else "centimeters"
        _l_val = round(length_cm / 2.54, 2) if _IS_US_APPLIANCE else length_cm
        _w_val = round(width_cm  / 2.54, 2) if _IS_US_APPLIANCE else width_cm
        _h_val = round(height_cm / 2.54, 2) if _IS_US_APPLIANCE else height_cm

        if weight_kg > 0:
            attributes["item_weight"] = [{"value": _weight_val, "unit": _weight_unit, "marketplace_id": client.marketplace_id}]
        # ── Dimensiones del producto ──────────────────────────────────────────
        if length_cm > 0 and width_cm > 0 and height_cm > 0:
            attributes["item_dimensions"] = [{
                "length": {"value": _l_val, "unit": _dim_unit},
                "width":  {"value": _w_val, "unit": _dim_unit},
                "height": {"value": _h_val, "unit": _dim_unit},
                "marketplace_id": client.marketplace_id,
            }]
            _dim_attr_name = "item_length_width_height" if product_type not in ("TELEVISION","COMPUTER_MONITOR") else "item_depth_width_height"
            attributes[_dim_attr_name] = [{
                "length" if product_type not in ("TELEVISION","COMPUTER_MONITOR") else "depth":
                    {"value": _w_val, "unit": _dim_unit},
                "width":  {"value": _l_val, "unit": _dim_unit},
                "height": {"value": _h_val, "unit": _dim_unit},
                "marketplace_id": client.marketplace_id,
            }]

        # ── is_refurbished ────────────────────────────────────────────────────
        attributes["is_refurbished"] = [{"value": condition == "refurbished_refurbished", "marketplace_id": client.marketplace_id}]

        # ── Atributos universales requeridos por Amazon ───────────────────────
        attributes["country_of_origin"] = [{"value": country_of_origin or "CN", "marketplace_id": client.marketplace_id}]
        attributes["supplier_declared_has_product_identifier_exemption"] = [{"value": True, "marketplace_id": client.marketplace_id}]
        attributes["supplier_declared_dg_hz_regulation"] = [{"value": "not_applicable", "marketplace_id": client.marketplace_id}]
        attributes["number_of_items"] = [{"value": 1, "marketplace_id": client.marketplace_id}]
        attributes["batteries_required"] = [{"value": False, "marketplace_id": client.marketplace_id}]
        attributes["batteries_included"] = [{"value": False, "marketplace_id": client.marketplace_id}]
        # item_type_keyword
        _ITK_DEFAULTS = {
            "TELEVISION": "televisions", "COMPUTER_MONITOR": "computer-monitors",
            "VACUUM": "household-vacuums", "VACUUM_CLEANER": "household-vacuums",
            "LIGHT_BULB": "light-bulbs", "SPEAKER": "speakers",
            "HEADPHONES": "headphones", "FAN": "fans",
            "AIR_CONDITIONER": "air-conditioners", "COFFEE_MAKER": "coffee-makers",
            "BLENDER": "blenders", "MICROWAVE_OVEN": "microwave-ovens",
            "LAPTOP": "laptops", "PERSONAL_COMPUTER": "personal-computers",
            "TABLET": "tablets", "CAMERA": "cameras", "PRINTER": "printers",
            "POWER_DRILL": "power-drills", "LIGHT_FIXTURE": "light-fixtures",
            "SMARTWATCH": "smart-watches", "KEYBOARD": "keyboards", "MOUSE": "mice",
            "HAIR_DRYER": "hair-dryers", "ELECTRIC_SHAVER": "electric-shavers",
            "SECURITY_CAMERA": "security-cameras", "SMART_SPEAKER": "smart-speakers",
            "POWER_BANK": "portable-power-banks", "BACKPACK": "backpacks",
            "PEST_CONTROL_DEVICE": "electronic-pest-control",
            "ELECTRIC_LANTERN": "lanterns",
        }
        _itk = item_type_keyword or _ITK_DEFAULTS.get(product_type, "")
        if _itk:
            attributes["item_type_keyword"] = [{"value": _itk, "marketplace_id": client.marketplace_id}]

        # Only send display/screen-specific attributes for TV and monitor product types
        _IS_DISPLAY_TYPE = product_type in ("TELEVISION", "COMPUTER_MONITOR", "MONITOR", "TV")

        # ── Display/screen attributes — ONLY for TV and monitor product types ──
        if _IS_DISPLAY_TYPE:
            _RESOLUTION_MAX = {"720p": "1280 x 720", "1080p": "1920 x 1080", "4K": "3840 x 2160", "8K": "7680 x 4320"}
            _RESOLUTION_STR = {"720p": "1280 x 720 pixels", "1080p": "1920 x 1080 pixels",
                                "4K": "3840 x 2160 pixels", "8K": "7680 x 4320 pixels"}
            display_obj: dict = {"marketplace_id": client.marketplace_id}
            _has_display = False
            if display_size_in > 0:
                display_obj["size"] = [{"value": display_size_in, "unit": "inches"}]
                _has_display = True
            _dt = display_type or ("LED" if product_type == "TELEVISION" else "")
            if _dt:
                display_obj["type"] = [{"value": _dt, "language_tag": "en_US"}]
                _has_display = True
                _tech_map = {"LED": "TFT active matrix", "QLED": "TFT active matrix",
                             "Mini LED": "TFT active matrix", "LCD": "TFT active matrix",
                             "OLED": "OLED", "QNED": "TFT active matrix"}
                display_obj["technology"] = [{"value": _tech_map.get(_dt, "TFT active matrix"), "language_tag": "en_US"}]
            if resolution:
                display_obj["resolution_maximum"] = [{"value": _RESOLUTION_MAX.get(resolution, resolution), "unit": "pixels", "language_tag": "en_US"}]
                _has_display = True
            if refresh_rate:
                try:
                    display_obj["refresh_rate_in_hertz"] = [{"value": int(refresh_rate)}]
                except (ValueError, TypeError):
                    pass
            if _has_display:
                attributes["display"] = [display_obj]
            if resolution:
                attributes["resolution"] = [{"value": _RESOLUTION_STR.get(resolution, resolution), "marketplace_id": client.marketplace_id}]
            _ar = aspect_ratio or ("16:9" if (display_size_in > 0 or display_type) else "")
            if _ar:
                attributes["image_aspect_ratio"] = [{"value": _ar, "marketplace_id": client.marketplace_id}]
            if refresh_rate:
                try:
                    attributes["refresh_rate"] = [{"value": int(refresh_rate), "unit": "hertz", "marketplace_id": client.marketplace_id}]
                except (ValueError, TypeError):
                    pass
            if mounting_type:
                _mt_map = {"Tabletop, Wall Mount": "Wall Mount", "Tabletop": "Table Mount", "Wall Mount": "Wall Mount"}
                attributes["mounting_type"] = [{"value": _mt_map.get(mounting_type, mounting_type), "marketplace_id": client.marketplace_id}]
            try:
                _hdmi = int(total_hdmi_ports) if total_hdmi_ports is not None else 0
            except (ValueError, TypeError):
                _hdmi = 0
            if _hdmi <= 0 and product_type == "TELEVISION":
                _hdmi = 2  # TV default
            if _hdmi > 0:
                attributes["total_hdmi_ports"] = [{"value": _hdmi, "marketplace_id": client.marketplace_id}]

        # ── Features, components, connectivity — universal ────────────────────
        _sf = special_feature or (["High Definition"] if product_type == "TELEVISION" else [])
        if _sf:
            attributes["special_feature"] = [
                {"value": f, "marketplace_id": client.marketplace_id}
                for f in _sf[:5] if f
            ]
        _ic = included_components or (["Remote Control", "Stand", "Power Cable"] if product_type == "TELEVISION" else [])
        if _ic:
            attributes["included_components"] = [
                {"value": c, "marketplace_id": client.marketplace_id}
                for c in _ic[:10] if c
            ]

        # ── Connectivity — ONLY valid Amazon enum values ──────────────────────
        # Amazon VACUUM_CLEANER valid values: Auxiliary, Bluetooth, Infrared, USB, Wi-Fi
        # "Corded Electric" is NOT a valid connectivity_technology value — it belongs to power_source_type
        _VALID_CONNECTIVITY = {"Auxiliary","Bluetooth","Infrared","USB","Wi-Fi","HDMI",
                               "Wi-Fi,Bluetooth","802.11"}
        _ct_default = (["HDMI","Wi-Fi","Bluetooth"] if product_type == "TELEVISION" else [])
        # Filter connectivity_tech to only include valid values (remove "Corded Electric" etc.)
        _ct_raw = connectivity_tech or _ct_default
        _ct = [c for c in _ct_raw if c and any(v.lower() in c.lower() for v in _VALID_CONNECTIVITY)]
        if _ct:
            attributes["connectivity_technology"] = [
                {"value": c, "marketplace_id": client.marketplace_id}
                for c in _ct if c
            ]
        _CORDED_TYPES = {"VACUUM_CLEANER","VACUUM","FAN","AIR_CONDITIONER","COFFEE_MAKER",
                         "BLENDER","MICROWAVE_OVEN","HAIR_DRYER","ELECTRIC_SHAVER","TOASTER"}

        # ── Universal attributes always required ──────────────────────────────
        # model_name (separate from model_number — marketing name)
        if model_number:
            attributes["model_name"] = [{"value": model_number, "marketplace_id": client.marketplace_id}]

        # item_length standalone — only for product types that accept it (not VACUUM_CLEANER)
        if length_cm > 0 and product_type not in ("VACUUM_CLEANER", "VACUUM"):
            attributes["item_length"] = [{"value": length_cm, "unit": "centimeters", "marketplace_id": client.marketplace_id}]

        # ── Product-type-specific extended attributes ─────────────────────────
        # These are filled by AI based on product research and sent when available
        def _attr(val): return [{"value": val, "marketplace_id": client.marketplace_id}] if val else None
        def _attr_list(lst): return [{"value": v, "marketplace_id": client.marketplace_id} for v in lst if v] if lst else None

        if surface_type:
            attributes["surface_type"]      = _attr(surface_type)
        elif product_type in ("VACUUM_CLEANER","VACUUM"):
            attributes["surface_type"]      = _attr("Multi-Surface")  # sensible default
        if form_factor:           attributes["form_factor"]           = _attr(form_factor)
        # power_source_type — default "Corded Electric" for appliances if not provided
        _pst = power_source_type
        if not _pst:
            if product_type in _CORDED_TYPES:
                _pst = "Corded Electric"
            elif product_type in ("PEST_CONTROL_DEVICE", "ELECTRIC_LANTERN"):
                _pst = "Energía solar"  # default for Amazon MX solar zappers/lanterns
        if _pst:                  attributes["power_source_type"]     = _attr(_pst)
        if bag_type:              attributes["bag_type"]              = _attr(bag_type)
        if filter_type_attr:      attributes["filter_type"]           = _attr(filter_type_attr)
        if specific_uses:         attributes["specific_uses_for_product"] = _attr_list(specific_uses)
        # material_type — Amazon MX requires Spanish values for MX marketplace
        _mat = material_type
        if not _mat and product_type in ("PEST_CONTROL_DEVICE", "ELECTRIC_LANTERN"):
            _mat = "Plástico"  # default from schema; most of these are plastic
        if not _mat and power_source_type and "solar" in power_source_type.lower():
            _mat = "Plástico"  # solar products are typically plastic
        if _mat:              attributes["material_type"]         = _attr(_mat)
        if pattern_attr:          attributes["pattern"]               = _attr(pattern_attr)
        if finish_type_attr:      attributes["finish_type"]           = _attr(finish_type_attr)
        if compatible_devs:       attributes["compatible_devices"]    = _attr_list(compatible_devs)
        if num_settings is not None:
            try: attributes["number_of_settings"] = [{"value": int(num_settings), "marketplace_id": client.marketplace_id}]
            except (ValueError, TypeError): pass
        if noise_level_db is not None:
            try: attributes["noise_level_db"] = [{"value": float(noise_level_db), "unit": "decibels", "marketplace_id": client.marketplace_id}]
            except (ValueError, TypeError): pass
        if capacity_val is not None and capacity_unit_attr:
            try: attributes["capacity"] = [{"value": float(capacity_val), "unit": capacity_unit_attr.lower(), "marketplace_id": client.marketplace_id}]
            except (ValueError, TypeError): pass

        # ── VACUUM_CLEANER / VACUUM — correct attribute names from Amazon schema ──
        # Schema source: GET /definitions/2020-09-01/productTypes/VACUUM_CLEANER ATVPDKIKX0DER
        if product_type in ("VACUUM_CLEANER", "VACUUM"):
            # is_cordless (NOT wireless_capability — that doesn't exist)
            _is_corded = not (power_source_type or "").lower().startswith("battery")
            attributes["is_cordless"] = [{"value": not _is_corded, "marketplace_id": client.marketplace_id}]
            # surface_recommendation (NOT surface_type — that doesn't exist in VACUUM_CLEANER)
            # Valid values: Bare Floor, Carpet, Ceramic tile, Hard Floor, Hardwoods, Laminate, etc.
            _surf = surface_type or "Bare Floor"  # surface_type field reused as surface_recommendation
            # Amazon VACUUM_CLEANER allows max 1 occurrence for surface_recommendation
            _surf_val = "Bare Floor" if _surf.lower() in ("multi-surface", "multi surface", "hard floor", "bare floor") else _surf
            attributes["surface_recommendation"] = [{"value": _surf_val, "marketplace_id": client.marketplace_id}]
            # filter_type — valid: Cartridge, Cloth, Cyclonic, Disk, Foam, HEPA Filter, Multi-Stage Filtration System
            _ft = filter_type_attr or "Foam"
            if _ft.lower() in ("hepa", "hepa filter"):
                _ft = "HEPA Filter"
            elif _ft.lower() in ("multi-stage", "multi stage"):
                _ft = "Multi-Stage Filtration System"
            attributes["filter_type"] = [{"value": _ft, "marketplace_id": client.marketplace_id}]
            # form_factor — valid: Cannister, Handheld, Robotic, Stick, Upright
            _ff = form_factor or "Stick"  # default Stick for HV200-type
            if _ff.lower() == "canister": _ff = "Cannister"
            if _ff.lower() in ("stick",): _ff = "Stick"
            if _ff.lower() in ("upright",): _ff = "Upright"
            attributes["form_factor"] = [{"value": _ff, "marketplace_id": client.marketplace_id}]
            # power_source_type — valid: AC DC Adapter, Battery Powered, Corded Electric, Hybrid
            _pst2 = power_source_type or "Corded Electric"
            if "battery" in _pst2.lower(): _pst2 = "Battery Powered"
            elif "hybrid" in _pst2.lower(): _pst2 = "Hybrid (Corded And Cordless)"
            else: _pst2 = "Corded Electric"
            attributes["power_source_type"] = [{"value": _pst2, "marketplace_id": client.marketplace_id}]
            # item_type_keyword for vacuum — use form_factor to pick the right value
            _ff_lower = _ff.lower()
            _vac_itk = {
                "stick": "household-stick-vacuums",
                "upright": "household-upright-vacuums",
                "handheld": "household-handheld-vacuums",
                "cannister": "household-canister-vacuums",
                "robotic": "household-vacuums",
            }.get(_ff_lower, "household-vacuums")
            # Override item_type_keyword only if not already set or generic
            if not item_type_keyword or item_type_keyword in ("vacuum-cleaners", "household-vacuums"):
                attributes["item_type_keyword"] = [{"value": _vac_itk, "marketplace_id": client.marketplace_id}]
            # capacity (dust cup) with correct unit_of_measure key
            _cap_v2 = float(capacity_val or 0) or 0.5  # 0.5L default for stick vac
            _cap_u2 = (capacity_unit_attr or "liters").lower()
            attributes["capacity"] = [{"value": _cap_v2, "unit_of_measure": _cap_u2, "marketplace_id": client.marketplace_id}]
            # Compliance declarations — required for VACUUM_CLEANER
            attributes["supplier_declared_material_regulation"] = [{"value": "not_applicable", "marketplace_id": client.marketplace_id}]
            # required_product_compliance_certificate — Title Case required by Amazon schema
            attributes["required_product_compliance_certificate"] = [{"value": "Not Applicable", "marketplace_id": client.marketplace_id}]
            # capacity with correct unit key (confirmed by successful HV200 launch)
            _cap_v2 = float(capacity_val or 0) or 0.5
            _cap_u2 = (capacity_unit_attr or "liters").lower()
            attributes["capacity"] = [{"value": _cap_v2, "unit": _cap_u2, "marketplace_id": client.marketplace_id}]

        # ── Voltage — required for appliances by Amazon ───────────────────────
        _NEEDS_VOLTAGE = {"VACUUM_CLEANER","VACUUM","FAN","AIR_CONDITIONER","COFFEE_MAKER",
                          "BLENDER","MICROWAVE_OVEN","HAIR_DRYER","ELECTRIC_SHAVER","TOASTER",
                          "POWER_DRILL","LIGHT_FIXTURE","LIGHT_BULB"}
        if product_type in _NEEDS_VOLTAGE:
            # Use the AI-researched voltage — NEVER hardcode blindly
            _voltage_str = (body.get("voltage_v") or "").strip()
            import re as _rev
            _voltage_num = None
            if _voltage_str:
                # Parse first numeric value from strings like "120V", "120/240V", "100-240V"
                _vm = _rev.search(r"(\d+)", _voltage_str)
                if _vm:
                    _voltage_num = int(_vm.group(1))
            # Fallback only when AI didn't provide voltage
            if not _voltage_num:
                _is_us_market = (currency == "USD" or client.marketplace_id == "ATVPDKIKX0DER")
                _voltage_num = 120 if _is_us_market else 127
                logger.info(f"[AMZ Lanzar] Voltage not in payload for {product_type}, using fallback {_voltage_num}V")
            attributes["voltage"] = [{"value": _voltage_num, "unit": "volts", "marketplace_id": client.marketplace_id}]
            # Wattage if available
            _watts = float(body.get("watts") or 0)
            if _watts > 0:
                attributes["wattage"] = [{"value": _watts, "unit": "watts", "marketplace_id": client.marketplace_id}]

        # ── Precio de lista (MSRP), año del modelo, garantía ─────────────────
        if list_price_msrp > 0:
            attributes["list_price"] = [{
                "currency": currency,
                "value": list_price_msrp,
                "marketplace_id": client.marketplace_id,
            }]
        # model_year — requerido para TELEVISION, default año actual
        import datetime as _dt
        _my = model_year or (str(_dt.datetime.now().year) if product_type == "TELEVISION" else None)
        if _my:
            try:
                attributes["model_year"] = [{"value": int(_my), "marketplace_id": client.marketplace_id}]
            except (ValueError, TypeError):
                pass
        _warranty = warranty_desc or "90 days seller warranty"
        attributes["warranty_description"] = [{"value": _warranty, "marketplace_id": client.marketplace_id}]

        # ── Peso y dimensiones del paquete ────────────────────────────────────
        _pkg_w = pkg_weight_kg or (round(weight_kg * 1.25, 1) if weight_kg > 0 else 0)
        if _pkg_w > 0:
            attributes["item_package_weight"] = [{"value": _pkg_w, "unit": "kilograms", "marketplace_id": client.marketplace_id}]
        _pl = pkg_length_cm or (round(length_cm + 10, 1) if length_cm > 0 else 0)
        _pw = pkg_width_cm  or (round(width_cm  + 5,  1) if width_cm  > 0 else 0)
        _ph = pkg_height_cm or (round(height_cm + 10, 1) if height_cm > 0 else 0)
        if _pl > 0 and _pw > 0 and _ph > 0:
            attributes["item_package_dimensions"] = [{
                "length": {"value": _pl, "unit": "centimeters"},
                "width":  {"value": _pw, "unit": "centimeters"},
                "height": {"value": _ph, "unit": "centimeters"},
                "marketplace_id": client.marketplace_id,
            }]

        if fulfillment == "FBM" and quantity > 0:
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": quantity,
            }]
        elif fulfillment == "FBA":
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "AMAZON_NA",
            }]

    # Fix UPC: EAN-13 (13 chars) must be sent as type "ean", not "upc"
    if upc and len(upc) == 13:
        for attr_block in attributes.get("externally_assigned_product_identifier") or []:
            if attr_block.get("type") == "upc":
                attr_block["type"] = "ean"
    # Truncate UPC to 12 chars if too long (some UPCs have leading zeros added)
    if upc and len(upc) > 13 and "externally_assigned_product_identifier" in attributes:
        del attributes["externally_assigned_product_identifier"]

    def _is_attr_validation_error(issues_list):
        """True if errors are attribute validation issues (not product type)."""
        for i in issues_list:
            if i.get("severity") == "ERROR":
                msg = (i.get("message") or "").lower()
                # Attribute-level errors (not product type errors)
                if ("se requiere" in msg or "is required" in msg or
                        "máximo permitido" in msg or "maximum" in msg or
                        "valor no válido" in msg or "invalid value" in msg or
                        "pero falta" in msg or "is missing" in msg):
                    return True
        return False

    def _is_product_type_error(issues_list):
        """Returns True if the error is specifically about an invalid/incompatible product type."""
        for i in issues_list:
            if i.get("severity") != "ERROR":
                continue
            attrs = [str(a).lower() for a in (i.get("attributeNames") or [])]
            msg   = (i.get("message") or "").lower()
            if ("producttype" in attrs or "product_type" in attrs or
                    "product type" in msg or "producttype" in msg or
                    "not valid or compatible" in msg or "no es válido o no es compatible" in msg):
                return True
        return False

    # Minimal attribute set for PRODUCT fallback (only universal attrs)
    _SPECIFIC_ONLY_ATTRS = {
        "display","resolution","image_aspect_ratio","refresh_rate","total_hdmi_ports",
        "mounting_type","wireless_capability","cpsc_compliant","surface_type",
        "filter_type","bag_type","form_factor","power_source_type","capacity",
        "voltage","wattage","noise_level_db","compliance_media",
    }

    try:
        result = await client.create_listing_full(sku, product_type, attributes, requirements)
        issues = result.get("issues") or []
        errors_now = [i for i in issues if i.get("severity") == "ERROR"]

        # Retry 1: product type invalid → retry with PRODUCT
        if _is_product_type_error(issues) and product_type not in ("PRODUCT", "TELEVISION", "COMPUTER_MONITOR"):
            logger.info(f"[AMZ Lanzar] Product type '{product_type}' rejected, retrying with PRODUCT")
            product_type = "PRODUCT"
            attributes = {k: v for k, v in attributes.items() if k not in _SPECIFIC_ONLY_ATTRS}
            result = await client.create_listing_full(sku, product_type, attributes, requirements)
            issues = result.get("issues") or []
            errors_now = [i for i in issues if i.get("severity") == "ERROR"]

        # Retry 2: attribute errors, NOT new product creation (PRODUCT type is invalid for LISTING)
        # Only retry with PRODUCT for LISTING_OFFER_ONLY (ASIN match flow)
        elif (errors_now and _is_attr_validation_error(issues)
              and product_type not in ("PRODUCT", "TELEVISION", "COMPUTER_MONITOR")
              and requirements == "LISTING_OFFER_ONLY"):
            logger.info(f"[AMZ Lanzar] Attribute errors for {product_type} ({len(errors_now)} errors), "
                        f"retrying with PRODUCT type to bypass attribute requirements")
            product_type = "PRODUCT"
            # Keep only universally-safe attributes for PRODUCT type
            _SAFE_FOR_PRODUCT = {
                "condition_type","purchasable_offer","item_name","bullet_point",
                "product_description","generic_keyword","main_product_image_locator",
                "other_product_image_locator_1","other_product_image_locator_2",
                "other_product_image_locator_3","other_product_image_locator_4",
                "other_product_image_locator_5","other_product_image_locator_6",
                "other_product_image_locator_7","brand","manufacturer","model_number",
                "model_name","part_number","color","item_weight","item_dimensions",
                "item_depth_width_height","item_length_width_height","is_refurbished","country_of_origin",
                "supplier_declared_has_product_identifier_exemption",
                "supplier_declared_dg_hz_regulation","number_of_items",
                "batteries_required","batteries_included","item_type_keyword",
                "list_price","model_year","warranty_description","item_package_weight",
                "item_package_dimensions","fulfillment_availability",
                "externally_assigned_product_identifier","merchant_suggested_asin",
                "special_feature","included_components","connectivity_technology",
                "item_length","material_type","specific_uses_for_product",
            }
            attributes = {k: v for k, v in attributes.items() if k in _SAFE_FOR_PRODUCT}
            result = await client.create_listing_full(sku, product_type, attributes, requirements)
            issues = result.get("issues") or []

    except Exception as e:
        logger.exception("[AMZ Lanzar] create_listing_full error")
        return JSONResponse({"error": str(e)[:300]}, status_code=500)

    # Extraer ASIN de la respuesta (si Amazon lo asignó)
    status_resp = result.get("status", "")
    issues = result.get("issues") or []
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    if errors:
        # Show ALL errors at once, not just the first one
        all_msgs = " | ".join(
            e.get("message", "Error de validación") for e in errors
        )
        logger.warning(f"[AMZ Lanzar] Amazon validation errors ({len(errors)}): {all_msgs}")
        return JSONResponse({
            "error": all_msgs,
            "error_count": len(errors),
            "issues": issues,
        }, status_code=400)

    # ASIN nuevo para Flujo 2
    new_asin = asin
    try:
        ids = result.get("identifiers") or []
        for id_block in ids:
            mp_ids = id_block.get("marketplaceIdentifiers") or {}
            mp_data = mp_ids.get(client.marketplace_id) or {}
            if mp_data.get("asin"):
                new_asin = mp_data["asin"]
                break
    except Exception:
        pass

    # Persistir como "launched" en DB
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO amz_sku_gaps (seller_id, sku, asin, status, launched_price, launched_at)
               VALUES (?, ?, ?, 'launched', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(seller_id, sku) DO UPDATE SET
                   asin=excluded.asin, status='launched',
                   launched_price=excluded.launched_price,
                   launched_at=CURRENT_TIMESTAMP""",
            (client.seller_id, sku.upper(), new_asin, price),
        )
        await db.commit()

    # Auto-increment template launch count + mark as validated on success
    if product_type not in ("PRODUCT",):
        try:
            from app.services.token_store import increment_template_launch as _itl
            await _itl(product_type, client.marketplace_id)
        except Exception:
            pass

    return {"ok": True, "asin": new_asin, "status": status_resp, "sku": sku, "product_type": product_type}


# ── 3b. Búsqueda de imágenes reales del producto (DuckDuckGo) ────────────────

@router.get("/search-product-images")
async def search_product_images(
    q: str = Query("", description="Búsqueda: marca + modelo"),
    brand: str = Query("", description="Marca del producto"),
    model: str = Query("", description="Modelo del producto"),
):
    """Busca imágenes reales del producto usando DuckDuckGo y filtra por fuentes confiables."""
    import urllib.parse as _up
    import httpx as _hx
    import re as _re

    query = q.strip() or f"{brand} {model}".strip()
    if not query:
        return JSONResponse({"images": []})

    # DDG image search: first get vqd token, then fetch images
    try:
        async with _hx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Step 1: get vqd token
            r1 = await client.post(
                "https://duckduckgo.com/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            vqd_match = _re.search(r'vqd=(["\'])([^"\']+)\1', r1.text) or _re.search(r'vqd=([\d-]+)', r1.text)
            if not vqd_match:
                return JSONResponse({"images": [], "error": "no_token"})
            vqd = vqd_match.group(2) if vqd_match.lastindex >= 2 else vqd_match.group(1)

            # Step 2: fetch images JSON
            params = {
                "q": query, "vqd": vqd, "p": "1",
                "f": ",,,,,", "l": "us-en", "o": "json", "s": "0",
            }
            r2 = await client.get(
                "https://duckduckgo.com/i.js",
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://duckduckgo.com/",
                },
            )
            data = r2.json()

        results = data.get("results") or []
        # Trusted sources: manufacturer sites, major retailers, press images
        trusted_domains = (
            brand.lower().replace(" ", "") if brand else "",
        )
        images = []
        for item in results[:30]:
            url = item.get("image") or ""
            if not url or not url.startswith("http"):
                continue
            # Skip sketchy/low-res sources; prefer manufacturer / retailer domains
            images.append({
                "url": url,
                "thumb": item.get("thumbnail") or url,
                "width": item.get("width") or 0,
                "height": item.get("height") or 0,
                "source": item.get("source") or "",
            })
            if len(images) >= 9:
                break

        return JSONResponse({"images": images, "query": query})

    except Exception as _e:
        logger.warning(f"[search-product-images] Error: {_e}")
        return JSONResponse({"images": [], "error": str(_e)[:100]})


# ── 3b2. Scraper de URL de producto — extrae imágenes y specs ────────────────

@router.get("/scrape-product-url")
async def scrape_product_url(url: str = Query("", description="URL de la página del producto")):
    """
    Extrae imágenes de un producto usando estrategias en cascada:
    1. Shopify /products/{handle}.json  (westinghouse.com, etc.)
    2. WooCommerce product JSON embed
    3. og:image / twitter:image / JSON-LD
    4. Next.js __NEXT_DATA__ / Nuxt __NUXT__ state
    5. Cloudinary / imgix / CDN pattern matching
    6. img tags con lazy-load y srcset
    """
    import httpx as _hx
    import re as _re
    import urllib.parse as _up
    import json as _json

    url = url.strip()
    if not url or not url.startswith("http"):
        return JSONResponse({"images": [], "specs": {}, "error": "URL inválida"})

    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
    }

    parsed = _up.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path_clean = parsed.path.rstrip("/")  # without query params

    images_seen: set = set()
    images: list = []
    strategies_tried: list = []

    def _abs(src: str) -> str:
        if not src:
            return ""
        src = src.strip()
        if src.startswith("//"):
            return f"{parsed.scheme}:{src}"
        if src.startswith("/"):
            return f"{origin}{src}"
        if src.startswith("http"):
            return src
        return ""

    def _is_skip(src: str) -> bool:
        low = src.lower()
        if any(low.endswith(ext) for ext in (".svg", ".gif", ".ico", ".webp.html")):
            return True
        if "data:" in src or len(src) < 10:
            return True
        skip_kw = ("placeholder", "spinner", "loading", "blank", "pixel", "spacer",
                   "icon", "logo", "avatar", "badge", "button", "arrow", "star-",
                   "rating", "flag-", "payment", "social-", "share-", "cart-icon")
        return any(kw in low for kw in skip_kw)

    def _add(src: str, priority: int = 0, source: str = ""):
        abs_src = _abs(src)
        if not abs_src or abs_src in images_seen:
            return
        if _is_skip(abs_src):
            return
        # Upscale Shopify thumbnails to full size (remove _100x, _200x, _480x suffixes)
        abs_src = _re.sub(r'_(\d+)x(\d+)?\.(jpg|jpeg|png|webp)', r'.\3', abs_src, flags=_re.IGNORECASE)
        images_seen.add(abs_src)
        images.append({"url": abs_src, "priority": priority, "source": source or parsed.netloc})

    def _harvest_json(obj, priority: int = 0):
        """Recursively extract image URLs from a JSON object."""
        if isinstance(obj, str):
            if obj.startswith("http") and any(ext in obj.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
                _add(obj, priority)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in ("src", "url", "image", "images", "photo", "photos",
                                  "thumbnail", "contenturl", "imageurl", "picture",
                                  "full", "large", "original", "zoom"):
                    _harvest_json(v, priority)
                else:
                    _harvest_json(v, priority - 1)
        elif isinstance(obj, list):
            for item in obj:
                _harvest_json(item, priority)

    try:
        async with _hx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS) as _c:

            # ── Strategy 1: Shopify /products/{handle}.json ───────────────────
            # Shopify exposes a public JSON API for every product page
            if "/products/" in path_clean:
                try:
                    json_url = f"{origin}{path_clean}.json"
                    strategies_tried.append("shopify_json")
                    rj = await _c.get(json_url, headers={**_HEADERS, "Accept": "application/json"})
                    if rj.status_code == 200:
                        pdata = rj.json().get("product", {})
                        for img in pdata.get("images", []):
                            src = img.get("src") or ""
                            _add(src, priority=20, source="shopify")
                        # Also check variants for images
                        for v in pdata.get("variants", []):
                            fi = v.get("featured_image") or {}
                            _add(fi.get("src", ""), priority=18, source="shopify")
                except Exception:
                    pass

            # ── Strategy 2: WooCommerce /{slug}/?format=json ──────────────────
            if not images:
                try:
                    woo_url = f"{origin}/wp-json/wc/v3/products"
                    # Try the REST API with slug extracted from path
                    slug = path_clean.split("/")[-1]
                    rw = await _c.get(f"{woo_url}?slug={slug}", headers={**_HEADERS, "Accept": "application/json"})
                    if rw.status_code == 200:
                        strategies_tried.append("woocommerce_api")
                        for prod in rw.json()[:1]:
                            for img in prod.get("images", []):
                                _add(img.get("src", ""), priority=19, source="woocommerce")
                except Exception:
                    pass

            # ── Strategy 3: Fetch HTML and parse ─────────────────────────────
            strategies_tried.append("html_parse")
            rh = await _c.get(url)
            rh.raise_for_status()
            html = rh.text
            final_url = str(rh.url)
            final_parsed = _up.urlparse(final_url)

            # 3a) og:image / twitter:image
            for pat in [
                r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
            ]:
                for m in _re.finditer(pat, html, _re.IGNORECASE):
                    _add(m.group(1), priority=15, source="og:image")

            # 3b) JSON-LD structured data (Product, ImageObject)
            for jld in _re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, _re.IGNORECASE | _re.DOTALL):
                try:
                    _harvest_json(_json.loads(jld.group(1)), priority=14)
                except Exception:
                    pass

            # 3c) Next.js __NEXT_DATA__ (React SSR apps)
            nd = _re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, _re.IGNORECASE | _re.DOTALL)
            if nd:
                try:
                    _harvest_json(_json.loads(nd.group(1)), priority=13)
                    strategies_tried.append("nextjs")
                except Exception:
                    pass

            # 3d) Nuxt.js __NUXT__ state
            nuxt = _re.search(r'window\.__NUXT__\s*=\s*(\{.+?\});?\s*</script>', html, _re.DOTALL)
            if nuxt:
                try:
                    _harvest_json(_json.loads(nuxt.group(1)), priority=13)
                    strategies_tried.append("nuxt")
                except Exception:
                    pass

            # 3e) Generic window.__INITIAL_STATE__ / window.__STATE__ / window.PRODUCT
            for pat in [r'window\.__(?:INITIAL_STATE|STATE|STORE|APP_STATE|preloadedState)__\s*=\s*(\{.+?\});\s*</script>',
                        r'window\.(?:PRODUCT|product|catalog)\s*=\s*(\{.+?\});\s*(?:</script>|var )',
                        r'var\s+(?:product|item|__data)\s*=\s*(\{.+?\});\s*(?:</script>|var |\n)']:
                for m in _re.finditer(pat, html, _re.DOTALL | _re.IGNORECASE):
                    try:
                        _harvest_json(_json.loads(m.group(1)), priority=12)
                    except Exception:
                        pass

            # 3f) Shopify CDN images referenced anywhere in HTML/JS
            for m in _re.finditer(r'(https://cdn\.shopify\.com/s/files/[^\s"\'?]+\.(?:jpg|jpeg|png|webp))', html, _re.IGNORECASE):
                _add(m.group(1), priority=11, source="shopify_cdn")

            # 3g) Common CDN patterns (Cloudinary, imgix, Scene7/Adobe, FastLy)
            cdn_patterns = [
                r'(https://res\.cloudinary\.com/[^\s"\'?]+\.(?:jpg|jpeg|png|webp)[^\s"\']*)',
                r'(https://[^/]+\.imgix\.net/[^\s"\'?]+\.(?:jpg|jpeg|png|webp)[^\s"\']*)',
                r'(https://[^/]+\.scene7\.com/is/image/[^\s"\'?]+)',
                r'(https://[^/]+\.akamaized\.net/[^\s"\'?]+\.(?:jpg|jpeg|png|webp)[^\s"\']*)',
            ]
            for cpat in cdn_patterns:
                for m in _re.finditer(cpat, html, _re.IGNORECASE):
                    _add(m.group(1), priority=10, source="cdn")

            # 3h) img tags — lazy-load attributes, prefer large
            for m in _re.finditer(r'<img[^>]+>', html, _re.IGNORECASE):
                tag = m.group(0)
                src = ""
                for attr in ("data-zoom-image", "data-large-image", "data-full-image",
                             "data-original", "data-src", "data-lazy-src", "data-bg",
                             "data-lazy", "data-image", "src"):
                    am = _re.search(rf'\b{attr}=["\']([^"\']+)["\']', tag, _re.IGNORECASE)
                    if am and am.group(1).startswith("http"):
                        src = am.group(1)
                        break
                if not src:
                    continue
                w_m = _re.search(r'\bwidth=["\']?(\d+)', tag, _re.IGNORECASE)
                w = int(w_m.group(1)) if w_m else 0
                if w and w < 150:
                    continue
                priority = 8 if w >= 800 else (6 if w >= 400 else 3)
                _add(src, priority=priority, source="img_tag")

            # 3i) srcset — grab highest resolution
            for m in _re.finditer(r'srcset=["\']([^"\']+)["\']', html, _re.IGNORECASE):
                parts = [p.strip() for p in m.group(1).split(",")]
                # Sort by width descriptor desc, take largest
                best = ""
                best_w = 0
                for p in parts:
                    pieces = p.split()
                    if len(pieces) >= 2 and pieces[1].endswith("w"):
                        try:
                            w = int(pieces[1][:-1])
                            if w > best_w:
                                best_w = w
                                best = pieces[0]
                        except ValueError:
                            pass
                    elif pieces:
                        best = best or pieces[0]
                if best:
                    _add(best, priority=4 if best_w >= 800 else 2, source="srcset")

        # Final sort by priority, deduplicate, return top 9
        images.sort(key=lambda x: -x["priority"])
        top = [{"url": img["url"], "source": img["source"]} for img in images[:9]]

        # Extract meta description for specs
        desc_m = _re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{20,})["\']', html, _re.IGNORECASE)
        specs = {}
        if desc_m:
            specs["description"] = desc_m.group(1)[:300]

        if not top:
            return JSONResponse({
                "images": [],
                "specs": specs,
                "strategies_tried": strategies_tried,
                "error": "No se encontraron imágenes. La página puede requerir JavaScript o estar protegida por CAPTCHA.",
            })

        return JSONResponse({"images": top, "specs": specs, "strategies_tried": strategies_tried, "source_url": url})

    except _hx.TimeoutException:
        return JSONResponse({"images": [], "specs": {}, "error": "Tiempo de espera agotado. La página tardó demasiado."})
    except _hx.HTTPStatusError as e:
        code = e.response.status_code
        msg = {403: "Acceso denegado (403) — el sitio bloquea scrapers. Copia las URLs manualmente.",
               404: "Página no encontrada (404). Verifica la URL.",
               429: "Demasiadas solicitudes (429). Espera unos minutos e intenta de nuevo.",
               }.get(code, f"Error HTTP {code}")
        return JSONResponse({"images": [], "specs": {}, "error": msg})
    except Exception as _e:
        logger.warning(f"[scrape-product-url] {_e}")
        return JSONResponse({"images": [], "specs": {}, "error": str(_e)[:200]})


# ── 3c. Photo prompts (Claude → 6 Higgsfield prompts específicos del producto) ──

@router.post("/photo-prompts")
async def photo_prompts(request: Request):
    """Genera 6 prompts optimizados para Higgsfield IA basados en el producto."""
    import os, base64 as _b64
    import httpx as _httpx

    body = await request.json()
    title    = (body.get("title") or "").strip()
    brand    = (body.get("brand") or "").strip()
    model    = (body.get("model") or "").strip()
    category = (body.get("category") or "Electronics").strip()
    has_img  = bool((body.get("thumbnail_url") or "").strip())

    product_desc = " ".join(filter(None, [brand, model, title])) or title

    fallback = [
        f"Professional Amazon listing photo, {product_desc}, pure white background #FFFFFF, product fills 85% of frame, studio lighting, no text overlay",
        f"{product_desc}, 3/4 angle showing all sides, white background, professional product photography",
        f"Close-up detail of {product_desc} showing key features and build quality, white background, macro studio shot",
        f"{product_desc} being used in a modern well-lit home, lifestyle photography, natural warm lighting, person interacting with product",
        f"{product_desc} in a clean modern living space, aspirational lifestyle shot, warm ambient lighting, product prominently featured",
        f"{product_desc} size scale reference, shown in real home environment, clean modern interior, product in foreground",
    ]

    if not _or_client.is_available():
        return JSONResponse({"prompts": fallback})

    try:
        prompt_text = (
            f"Product: {product_desc}\nBrand: {brand}\nModel: {model}\nCategory: {category}\n\n"
            "Generate exactly 6 highly specific Higgsfield AI image prompts for Amazon product listing.\n"
            "CRITICAL: Each prompt MUST describe THIS EXACT PRODUCT visually — its shape, color, form factor, distinctive features.\n"
            "Do NOT describe a generic product. Describe the actual appearance of this specific brand/model.\n\n"
            "Prompt types (in order):\n"
            "1. Hero shot — white background, product centered fills 85% of frame, Amazon listing compliant\n"
            "2. Angle shot — 3/4 view showing all sides, white background\n"
            "3. Detail close-up — macro shot of key feature, control panel, or distinctive design element\n"
            "4. Lifestyle — product being ACTIVELY USED by a person in a modern home, showing its real use case\n"
            "5. Lifestyle ambient — product displayed in aspirational home setting, no person, warm lighting\n"
            "6. Scale context — product shown in real environment to convey size, clean modern space\n\n"
            "Rules: photorealistic, professional photography, no watermarks, no text, no logos.\n"
            "Output ONLY a valid JSON array of exactly 6 strings, nothing else."
        )
        raw = await _or_client.generate(prompt_text, max_tokens=800, model=_or_client.get_premium_model())
        # Extract JSON array
        import re as _re
        m = _re.search(r'\[.*\]', raw, _re.DOTALL)
        if m:
            prompts = json.loads(m.group())
            if isinstance(prompts, list) and len(prompts) >= 6:
                return JSONResponse({"prompts": [str(p) for p in prompts[:6]]})
    except Exception as _e:
        logger.warning(f"[photo-prompts] Claude error: {_e}")

    return JSONResponse({"prompts": fallback})


# ── 4. Ignorar gap ────────────────────────────────────────────────────────────

@router.post("/ignore/{sku}")
async def ignore_gap(sku: str, request: Request):
    body = await request.json()
    seller_id = body.get("seller_id")
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO amz_sku_gaps (seller_id, sku, status)
               VALUES (?, ?, 'ignored')
               ON CONFLICT(seller_id, sku) DO UPDATE SET status='ignored'""",
            (client.seller_id, sku.upper()),
        )
        await db.commit()
    return {"ok": True}


# ── 5b. Ignorar toda una categoría ───────────────────────────────────────────

@router.post("/ignore-category")
async def ignore_category(request: Request):
    """Marca como 'ignored' todos los gaps unlaunched/sin_precio de una categoría."""
    body = await request.json()
    category = (body.get("category") or "").strip()
    sid_param = body.get("seller_id")
    if not category:
        return JSONResponse({"error": "category requerido"}, status_code=400)
    client = await get_amazon_client(seller_id=sid_param)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            """UPDATE amz_sku_gaps SET status='ignored'
               WHERE seller_id=? AND category=? AND status IN ('unlaunched','sin_precio')""",
            (client.seller_id, category),
        )
        await db.commit()
        affected = cur.rowcount
    return JSONResponse({"ok": True, "ignored": affected, "category": category})


# ── 5. Restaurar gap ignorado ─────────────────────────────────────────────────

@router.post("/restore/{sku}")
async def restore_gap(sku: str, request: Request):
    body = await request.json()
    seller_id = body.get("seller_id")
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE amz_sku_gaps SET status='unlaunched' WHERE seller_id=? AND sku=?",
            (client.seller_id, sku.upper()),
        )
        await db.commit()
    return {"ok": True}


# ── 6. Tab Lanzados ───────────────────────────────────────────────────────────

@router.get("/launched", response_class=HTMLResponse)
async def get_launched(
    request: Request,
    seller_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
    q: str = Query(""),
):
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return HTMLResponse("<div class='p-6 text-red-500 text-center font-semibold'>Sin cuenta Amazon conectada</div>")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT g.*, l.title as live_title, l.status as live_status, l.price as live_price, l.asin as live_asin
               FROM amz_sku_gaps g
               LEFT JOIN amazon_listings l ON l.seller_id=g.seller_id AND UPPER(l.sku)=g.sku
               WHERE g.seller_id=? AND g.status='launched'
               ORDER BY g.launched_at DESC""",
            (client.seller_id,),
        )
        rows_all = [dict(r) for r in await cur.fetchall()]

    q_lower = q.strip().lower()
    if q_lower:
        rows_all = [r for r in rows_all if q_lower in r["sku"].lower() or q_lower in (r.get("product_title") or "").lower()]

    total = len(rows_all)
    pages = max(1, math.ceil(total / per_page))
    page  = max(1, min(page, pages))
    rows  = rows_all[(page - 1) * per_page: page * per_page]

    return _templates.TemplateResponse(request, "partials/amazon_lanzados.html", {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "q": q,
        "marketplace": client.marketplace_name,
        "seller_id": client.seller_id,
    })


# ── 7. Tab Ignorados ──────────────────────────────────────────────────────────

@router.get("/ignored", response_class=HTMLResponse)
async def get_ignored(
    request: Request,
    seller_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
):
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return HTMLResponse("<div class='p-6 text-red-500 text-center font-semibold'>Sin cuenta Amazon conectada</div>")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM amz_sku_gaps WHERE seller_id=? AND status='ignored' ORDER BY created_at DESC",
            (client.seller_id,),
        )
        rows_all = [dict(r) for r in await cur.fetchall()]

    total = len(rows_all)
    pages = max(1, math.ceil(total / per_page))
    page  = max(1, min(page, pages))
    rows  = rows_all[(page - 1) * per_page: page * per_page]

    return _templates.TemplateResponse(request, "partials/amazon_ignorados.html", {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "seller_id": client.seller_id,
    })
