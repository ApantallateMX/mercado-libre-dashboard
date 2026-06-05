"""
amazon_listing_sync.py — Sincronización de listings Amazon → DB local

Estrategia:
  - Arranque: sync completo para todas las cuentas Amazon (60s después del arranque)
  - Cada 10 min: qty-only sync — solo available_qty (rápido, mantiene stock fresco)
  - 1x/día (8 PM México ≈ 02:00 UTC): full reconciliation completa + gap scan
  - Manual: trigger via POST /api/listings/refresh

Solo descarga datos — cero escrituras a Amazon Seller Central.
"""
import asyncio
import logging
import time as _time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_FULL_INTERVAL      = 24 * 3600  # 24 horas entre syncs completos (1x/día)
_QTY_SYNC_INTERVAL  = 10 * 60   # 10 minutos entre qty-only syncs
_GAP_SCAN_INTERVAL  = 6 * 3600  # 6 horas entre gap scans automáticos
_sync_running       = False
_qty_sync_running   = False
_last_sync_ts       = 0.0
_last_qty_sync_ts   = 0.0
_last_gap_scan_ts   = 0.0
_sync_error    = ""
_sync_status: dict = {"accounts_synced": 0, "items_total": 0, "last_run_iso": None, "elapsed_s": 0}


def _listing_to_row(item: dict, seller_id: str) -> dict | None:
    """Convierte un listing de Amazon Listings API a row de amazon_listings."""
    sku = item.get("sku", "")
    if not sku:
        return None

    summaries = item.get("summaries") or [{}]
    summary   = summaries[0] if summaries else {}

    fa  = item.get("fulfillmentAvailability") or [{}]
    fa0 = fa[0] if fa else {}
    # Amazon puede devolver "fulfillmentChannelCode" como lista o como string
    _fc = fa0.get("fulfillmentChannelCode") or ""
    channel = (_fc if isinstance(_fc, str) else (_fc[0] if _fc else "")).upper()
    qty     = int(fa0.get("quantity") or 0)

    price = 0.0
    for offer in (item.get("offers") or []):
        if offer.get("offerType") == "B2C":
            try:
                price = float(offer.get("price", {}).get("amount") or 0)
            except (TypeError, ValueError):
                pass
            break

    # "status" también puede llegar como lista en algunos endpoints Amazon
    _st = summary.get("status") or "ACTIVE"
    status = (_st if isinstance(_st, str) else (_st[0] if _st else "ACTIVE")).upper()
    asin      = summary.get("asin") or ""
    item_name = (summary.get("itemName") or "")[:200]

    is_fba = channel == "AMAZON_NA"
    is_flx = "-FLX" in sku.upper()
    can_update = 0 if (is_fba or is_flx) else 1

    try:
        from app.services.sku_utils import normalize_to_bm_sku
        base_sku = normalize_to_bm_sku(sku) or sku[:10]
    except Exception:
        base_sku = sku[:10]

    return {
        "seller_id":    seller_id,
        "sku":          sku,
        "base_sku":     base_sku,
        "asin":         asin,
        "title":        item_name,
        "status":       status,
        "price":        price,
        "available_qty": qty,
        "can_update":   can_update,
        "fulfillment":  channel,
        "synced_at":    _time.time(),
    }


def _report_entry_to_row(entry: dict, seller_id: str) -> dict | None:
    """Convierte una entrada del Reports API a row de amazon_listings.
    Reports incluye title, price, quantity (a diferencia de entradas de gap scan que solo tienen sku/asin/status/channel).
    """
    sku = entry.get("sku", "")
    if not sku:
        return None

    channel = (entry.get("channel") or "DEFAULT").upper()
    status  = (entry.get("status") or "ACTIVE").upper()
    asin    = entry.get("asin") or ""
    title   = (entry.get("title") or "")[:200]

    try:
        price = float(entry.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0

    try:
        quantity = int(entry.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0

    is_fba = "AMAZON" in channel
    is_flx = "-FLX" in sku.upper()
    can_update = 0 if (is_fba or is_flx) else 1

    try:
        from app.services.sku_utils import normalize_to_bm_sku
        base_sku = normalize_to_bm_sku(sku) or sku[:10]
    except Exception:
        base_sku = sku[:10]

    return {
        "seller_id":     seller_id,
        "sku":           sku,
        "base_sku":      base_sku,
        "asin":          asin,
        "title":         title,
        "status":        status,
        "price":         price,
        "available_qty": quantity,
        "can_update":    can_update,
        "fulfillment":   channel,
        "synced_at":     _time.time(),
    }


async def _sync_account_full(seller_id: str, client) -> tuple[int, str]:
    """Descarga todos los listings de una cuenta Amazon y los guarda en DB.
    Intenta Reports API primero (sin límite de SKUs), con fallback a Listings API.
    Retorna (count, error_msg)."""
    from app.services import token_store
    try:
        rows = []
        use_report = True

        try:
            report_entries = await client.get_merchant_listings_report()
            rows = [_report_entry_to_row(e, seller_id) for e in report_entries]
            rows = [r for r in rows if r]
            logger.info(f"[AMZ-LISTING-SYNC] seller={seller_id}: Reports API → {len(rows)} listings")
        except Exception as _rpt_err:
            logger.warning(
                f"[AMZ-LISTING-SYNC] Reports API falló seller={seller_id}: {_rpt_err}. "
                f"Fallback a Listings API."
            )
            use_report = False
            listings = await client.get_all_listings()
            logger.info(f"[AMZ-LISTING-SYNC] seller={seller_id}: Listings API → {len(listings)} items")
            rows = [_listing_to_row(item, seller_id) for item in listings]
            rows = [r for r in rows if r]

        if rows:
            if use_report:
                await token_store.upsert_amazon_listings_report(rows)
            else:
                await token_store.upsert_amazon_listings(rows)
        logger.info(f"[AMZ-LISTING-SYNC] seller={seller_id}: {len(rows)} listings guardados en DB")

        # ── Detección automática de parents de variaciones ─────────────────
        if rows:
            try:
                parent_result = await token_store.detect_and_mark_parents(seller_id)
                if parent_result["marked"] > 0:
                    logger.info(f"[AMZ-LISTING-SYNC] seller={seller_id}: {parent_result['marked']} parents de variaciones detectados")
            except Exception as _pe:
                logger.warning(f"[AMZ-LISTING-SYNC] parent detection error: {_pe}")

        # ── Detección de huérfanos ──────────────────────────────────────────
        try:
            fresh_skus = {r["sku"] for r in rows}
            db_skus_qtys = await token_store.get_amazon_skus_and_qtys(seller_id)
            db_skus = {s for s, _ in db_skus_qtys}
            orphan_skus = db_skus - fresh_skus
            await token_store.clear_orphans_for_account("amz", seller_id)
            if orphan_skus:
                # Buscar title desde db
                amz_db = await token_store.get_amazon_listings_for_account(seller_id)
                db_map = {r.get("sku", ""): r for r in amz_db}
                orphan_entries = [
                    {
                        "platform":   "amz",
                        "account_id": seller_id,
                        "item_id":    sku,
                        "title":      db_map.get(sku, {}).get("title", ""),
                        "sku":        sku,
                    }
                    for sku in orphan_skus
                ]
                await token_store.save_orphan_listings(orphan_entries)
                logger.info(f"[AMZ-LISTING-SYNC] seller={seller_id}: {len(orphan_skus)} listings huérfanos detectados")
        except Exception as _oe:
            logger.warning(f"[AMZ-LISTING-SYNC] Error detectando huérfanos seller={seller_id}: {_oe}")

        return len(rows), ""
    except Exception as e:
        err = str(e)[:200]
        logger.warning(f"[AMZ-LISTING-SYNC] Error seller={seller_id}: {err}")
        return 0, err


async def run_amazon_listing_sync() -> dict:
    """Ejecuta sync completo para todas las cuentas Amazon registradas."""
    global _sync_running, _last_sync_ts, _sync_error, _sync_status

    if _sync_running:
        return {"status": "already_running"}

    _sync_running = True
    _sync_error   = ""
    t0 = _time.time()
    total_items   = 0
    accounts_done = 0

    try:
        from app.services import token_store
        from app.services.amazon_client import get_amazon_client

        amz_accounts = await token_store.get_all_amazon_accounts()
        if not amz_accounts:
            logger.info("[AMZ-LISTING-SYNC] Sin cuentas Amazon registradas")
            return {"status": "no_accounts"}

        first_error = ""
        for acc in amz_accounts:
            sid = acc.get("seller_id", "")
            if not sid:
                continue
            try:
                client = await get_amazon_client(seller_id=sid)
                if not client:
                    logger.warning(f"[AMZ-LISTING-SYNC] No se pudo crear cliente para {sid}")
                    continue
                # Guardar count actual como "prev" antes de modificar la DB
                try:
                    _amz_prev = await token_store.count_amazon_listings(sid)
                    await token_store.snapshot_listings_count("amz", sid, _amz_prev)
                except Exception:
                    pass
                n, err = await _sync_account_full(sid, client)
                total_items   += n
                accounts_done += 1
                if err and not first_error:
                    first_error = f"{sid}: {err}"
            except Exception as e:
                err = str(e)[:200]
                logger.warning(f"[AMZ-LISTING-SYNC] Error cuenta {sid}: {err}")
                if not first_error:
                    first_error = f"{sid}: {err}"
        if first_error:
            _sync_error = first_error

        _last_sync_ts = _time.time()
        # Invalidar cache de productos para que el próximo request reconstruya desde DB fresca
        try:
            from app.api.amazon_products import invalidate_listings_cache
            invalidate_listings_cache()   # todas las cuentas
        except Exception:
            pass
        from datetime import datetime
        _sync_status = {
            "accounts_synced": accounts_done,
            "items_total":     total_items,
            "last_run_iso":    datetime.utcnow().isoformat(),
            "elapsed_s":       round(_time.time() - t0, 1),
        }
        logger.info(
            f"[AMZ-LISTING-SYNC] Completado: {accounts_done} cuentas, "
            f"{total_items} listings, {_sync_status['elapsed_s']}s"
        )
        return {"status": "ok", **_sync_status}

    except Exception as e:
        _sync_error = str(e)[:200]
        logger.exception(f"[AMZ-LISTING-SYNC] Fatal: {e}")
        return {"status": "error", "error": _sync_error}
    finally:
        _sync_running = False


_QTY_LARGE_CATALOG = 1000  # umbral para usar FBA Inventory en lugar de Listings API


async def _sync_qty_only_account(seller_id: str, client) -> int:
    """Sync ligero de qty para listings conocidos.
    Catalogo grande (>1000 SKUs en DB): usa FBA Inventory API (sin límite de páginas).
    Catálogo pequeño: usa Listings API con fulfillmentAvailability.
    Retorna número de listings cuya qty cambió en DB.
    """
    from app.services import token_store
    try:
        current = {sku: qty for sku, qty in await token_store.get_amazon_skus_and_qtys(seller_id)}
        if not current:
            return 0  # Sin listings en DB — esperar al full sync

        updates: list[tuple[str, str, int]] = []

        if len(current) > _QTY_LARGE_CATALOG:
            # Catálogo grande: FBA Inventory API
            try:
                fba_items = await client.get_fba_inventory_all()
                for fba in fba_items:
                    sku = fba.get("sellerSku") or ""
                    if not sku or sku not in current:
                        continue
                    details = fba.get("inventoryDetails") or {}
                    qty = int(details.get("fulfillableQuantity") or 0)
                    if current.get(sku, -1) != qty:
                        updates.append((seller_id, sku, qty))
                logger.debug(
                    f"[AMZ-QTY] seller={seller_id}: FBA inventory {len(fba_items)} items, "
                    f"{len(updates)} cambios"
                )
            except Exception as _fba_err:
                logger.warning(f"[AMZ-QTY] FBA inventory falló seller={seller_id}: {_fba_err}")
                return 0
        else:
            # Catálogo pequeño: Listings API (capped at 1000, suficiente)
            items = await client.get_all_listings(included_data=["fulfillmentAvailability"])
            for item in items:
                sku = item.get("sku", "")
                if not sku or sku not in current:
                    continue
                fa  = (item.get("fulfillmentAvailability") or [{}])[0]
                qty = int(fa.get("quantity") or 0)
                if current.get(sku, -1) != qty:
                    updates.append((seller_id, sku, qty))

        if not updates:
            return 0

        changed = await token_store.update_amazon_qty_batch(updates)
        if changed > 0:
            logger.info(f"[AMZ-QTY] seller={seller_id}: {changed}/{len(updates)} listings qty actualizada")
            # Invalidar cache de productos para que el próximo request vea qty actualizada
            try:
                from app.api.amazon_products import invalidate_listings_cache
                invalidate_listings_cache(seller_id)
            except Exception:
                pass
        return changed
    except Exception as e:
        logger.warning(f"[AMZ-QTY] Error seller={seller_id}: {e}")
        return 0


async def run_amazon_qty_sync() -> dict:
    """Sync ligero de qty para todas las cuentas Amazon."""
    global _qty_sync_running, _last_qty_sync_ts
    if _qty_sync_running or _sync_running:
        return {"status": "skipped"}
    _qty_sync_running = True
    t0 = _time.time()
    total_changed = 0
    try:
        from app.services import token_store
        from app.services.amazon_client import get_amazon_client
        accounts = await token_store.get_all_amazon_accounts()
        for acc in (accounts or []):
            sid = acc.get("seller_id", "")
            if not sid:
                continue
            client = await get_amazon_client(seller_id=sid)
            if not client:
                continue
            n = await _sync_qty_only_account(sid, client)
            total_changed += n
        _last_qty_sync_ts = _time.time()
        logger.debug(f"[AMZ-QTY] {total_changed} cambios en {round(_time.time()-t0,1)}s")
        return {"status": "ok", "changed": total_changed, "elapsed_s": round(_time.time() - t0, 1)}
    except Exception as e:
        logger.warning(f"[AMZ-QTY] Fatal: {e}")
        return {"status": "error", "error": str(e)[:200]}
    finally:
        _qty_sync_running = False


async def _run_gap_scan_background() -> None:
    """Dispara gap scan para todas las cuentas en background, sin bloquear el loop."""
    global _last_gap_scan_ts
    try:
        from app.api.amazon_lanzar import run_gap_scan_all_accounts
        logger.info("[AMZ-AUTO-SCAN] Iniciando gap scan automático para todas las cuentas")
        await run_gap_scan_all_accounts()
        _last_gap_scan_ts = _time.time()
        logger.info("[AMZ-AUTO-SCAN] Gap scan automático completado")
    except Exception as e:
        logger.error(f"[AMZ-AUTO-SCAN] Error: {e}")


def _next_8pm_mexico_secs() -> float:
    """Segundos hasta las 20:00 hora México (CST = UTC-6)."""
    mex = datetime.now(timezone.utc) + timedelta(hours=-6)
    target = mex.replace(hour=20, minute=0, second=0, microsecond=0)
    if mex >= target:
        target += timedelta(days=1)
    return (target - mex).total_seconds()


async def _loop():
    """Loop periódico:
    - Arranque (60s): full sync → gap scan
    - Cada 10 min: qty-only sync (mantiene stock fresco en DB)
    - 1x/día a las 8 PM México: full reconciliation → gap scan
    - Cada 6h (si no hubo full sync): gap scan (cambios de stock BM)
    """
    global _last_gap_scan_ts
    await asyncio.sleep(60)   # esperar a que el servidor esté listo
    await run_amazon_listing_sync()
    # Gap scan inicial (después del primer full sync)
    await asyncio.sleep(5)
    await _run_gap_scan_background()

    while True:
        await asyncio.sleep(_QTY_SYNC_INTERVAL)   # despertar cada 10 min

        # qty-only sync (siempre — mantiene available_qty fresco en DB)
        try:
            await run_amazon_qty_sync()
        except Exception as e:
            logger.error(f"[AMZ-QTY-LOOP] Error: {e}")

        # full sync 1x/día si pasaron ≥24h → seguido de gap scan
        if (_time.time() - _last_sync_ts) >= _FULL_INTERVAL:
            try:
                await run_amazon_listing_sync()
                await asyncio.sleep(5)
                await _run_gap_scan_background()
            except Exception as e:
                logger.error(f"[AMZ-LISTING-SYNC-LOOP] Error: {e}")
        # gap scan independiente si pasaron ≥6h (sin full sync)
        elif (_time.time() - _last_gap_scan_ts) >= _GAP_SCAN_INTERVAL:
            try:
                await _run_gap_scan_background()
            except Exception as e:
                logger.error(f"[AMZ-GAP-SCAN-LOOP] Error: {e}")


def start_amazon_listing_sync():
    """Inicia el loop en background. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_loop())
    logger.info(
        f"[AMZ-LISTING-SYNC] Iniciado — qty c/{_QTY_SYNC_INTERVAL//60}min, "
        f"full sync 1x/día, gap scan c/{_GAP_SCAN_INTERVAL//3600}h"
    )


def get_sync_status() -> dict:
    return {
        "running":           _sync_running,
        "error":             _sync_error,
        "last_sync_ts":      _last_sync_ts,
        "last_gap_scan_ts":  _last_gap_scan_ts,
        **_sync_status,
    }
