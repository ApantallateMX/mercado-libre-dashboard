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

from app.config import DATABASE_PATH, ANTHROPIC_API_KEY
from app.services.amazon_client import get_amazon_client

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


# ── 2. Generar contenido IA — claude-sonnet-4-6, prompt completo ─────────────

@router.post("/generate-content")
async def generate_content(request: Request):
    body      = await request.json()
    title     = body.get("title", "")
    brand     = body.get("brand", "")
    model_num = (body.get("model") or "").strip()
    category  = body.get("category", "")
    upc       = (body.get("upc") or "").strip()
    price_mxn = float(body.get("price_mxn") or 0)

    ctx_parts = []
    if model_num:
        ctx_parts.append(f"Modelo: {model_num}")
    if upc:
        ctx_parts.append(f"UPC/EAN: {upc}")
    if price_mxn > 0:
        ctx_parts.append(f"Precio de venta: ${price_mxn:,.0f} MXN")
    extra_ctx = "\n".join(ctx_parts)

    try:
        import os, base64 as _b64
        _p1 = os.getenv("AI_KEY_P1", "")
        _p2 = os.getenv("AI_KEY_P2", "")
        api_key = (_b64.b64decode(_p1 + _p2).decode() if (_p1 and _p2) else (ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY", "")))
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY no configurada")

        prompt = f"""Eres un experto en optimización de listings para Amazon México con dominio de SEO, CRO y las políticas de Amazon MX 2024.

Crea contenido completo y de alta conversión para este producto:

Título catálogo: {title}
Marca: {brand}
Categoría: {category}
{extra_ctx}
Marketplace: Amazon México (amazon.com.mx) — compradores en español mexicano

━━━ REGLAS CRÍTICAS (cumplirlas al pie de la letra) ━━━

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
• Bullet 5: Garantía / soporte / certificaciones / por qué elegirlo

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

PRODUCT_TYPE:
• Elige el tipo Amazon MX más específico y correcto (SCREAMING_SNAKE_CASE)
• Ejemplos: TELEVISION, LIGHT_BULB, AIR_CONDITIONER, COMPUTER_MONITOR, FITNESS_TRACKER, MEDICAL_GLOVE
• Para TVs: TELEVISION. Para monitores: COMPUTER_MONITOR. Para bocinas: SPEAKER.
• Basa tu elección en la categoría y el título del producto

COLOR:
• Color principal del producto en inglés (ej: Black, White, Silver, Blue)
• Si no aplica o no está claro: ""

━━━ RESPONDE SOLO CON JSON VÁLIDO (sin markdown, sin texto extra) ━━━
{{
  "title": "...",
  "bullets": ["...", "...", "...", "...", "..."],
  "description": "...",
  "keywords_backend": "...",
  "product_type": "...",
  "color": "..."
}}"""

        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=45) as _http:
            _resp = await _http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            _resp.raise_for_status()
            text = _resp.json()["content"][0]["text"].strip()

        start = text.index("{")
        end   = text.rindex("}") + 1
        data  = json.loads(text[start:end])
        # Asegurar que keywords_backend no supere 249 chars
        if len(data.get("keywords_backend", "")) > 249:
            data["keywords_backend"] = data["keywords_backend"][:249].rsplit(" ", 1)[0]
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
    product_type     = body.get("product_type") or "PRODUCT"
    photo_urls       = [u.strip() for u in (body.get("photo_urls") or []) if (u or "").strip()]
    # Atributos adicionales
    brand            = (body.get("brand") or "").strip()
    model_number     = (body.get("model_number") or "").strip()
    color            = (body.get("color") or "").strip()
    weight_kg        = float(body.get("weight_kg") or 0)
    display_size_in  = float(body.get("display_size_in") or 0)

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
                "currency": "MXN",
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
                "currency": "MXN",
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
        if model_number:
            attributes["model_number"] = [{"value": model_number, "marketplace_id": client.marketplace_id}]
        if color:
            attributes["color"] = [{"value": color, "marketplace_id": client.marketplace_id}]
        if weight_kg > 0:
            attributes["item_weight"] = [{"value": weight_kg, "unit": "kilograms", "marketplace_id": client.marketplace_id}]
        if display_size_in > 0:
            attributes["display_size"] = [{"value": display_size_in, "unit": "inches", "marketplace_id": client.marketplace_id}]
        if fulfillment == "FBM" and quantity > 0:
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": quantity,
            }]
        elif fulfillment == "FBA":
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "AMAZON_NA",
            }]

    try:
        result = await client.create_listing_full(sku, product_type, attributes, requirements)
    except Exception as e:
        logger.exception("[AMZ Lanzar] create_listing_full error")
        return JSONResponse({"error": str(e)[:300]}, status_code=500)

    # Extraer ASIN de la respuesta (si Amazon lo asignó)
    status_resp = result.get("status", "")
    issues = result.get("issues") or []
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    if errors:
        return JSONResponse({
            "error": errors[0].get("message", "Error de validación Amazon"),
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

    return {"ok": True, "asin": new_asin, "status": status_resp, "sku": sku}


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
