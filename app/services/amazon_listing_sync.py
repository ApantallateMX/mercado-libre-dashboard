"""
amazon_listing_sync.py — Sincronización de listings Amazon → DB local

Estrategia:
  - Arranque: sync completo para todas las cuentas Amazon (60s después del arranque)
  - Cada 10 min: qty-only sync — solo fulfillmentAvailability para listings conocidos
  - Cada 6h: reconciliación completa automática
  - Manual: trigger via POST /api/listings/refresh

Solo descarga datos — cero escrituras a Amazon Seller Central.
"""
import asyncio
import logging
import time as _time

logger = logging.getLogger(__name__)

_FULL_INTERVAL     = 6 * 3600   # 6 horas entre syncs completos
_QTY_SYNC_INTERVAL = 10 * 60   # 10 minutos entre qty-only syncs
_sync_running      = False
_qty_sync_running  = False
_last_sync_ts      = 0.0
_last_qty_sync_ts  = 0.0
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


async def _sync_account_full(seller_id: str, client) -> tuple[int, str]:
    """Descarga todos los listings de una cuenta Amazon y los guarda en DB.
    Retorna (count, error_msg)."""
    from app.services import token_store
    try:
        listings = await client.get_all_listings()
        logger.info(f"[AMZ-LISTING-SYNC] seller={seller_id}: get_all_listings devolvió {len(listings)} items")
        rows = [_listing_to_row(item, seller_id) for item in listings]
        rows = [r for r in rows if r]
        if rows:
            await token_store.upsert_amazon_listings(rows)
        logger.info(f"[AMZ-LISTING-SYNC] seller={seller_id}: {len(rows)} listings guardados en DB")

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


async def _sync_qty_only_account(seller_id: str, client) -> int:
    """Sync ligero: solo fulfillmentAvailability para listings conocidos de esta cuenta.
    Usa get_all_listings con includedData=fulfillmentAvailability únicamente.
    Retorna número de listings cuya qty cambió en DB.
    """
    from app.services import token_store
    try:
        current = {sku: qty for sku, qty in await token_store.get_amazon_skus_and_qtys(seller_id)}
        if not current:
            return 0  # Sin listings en DB — esperar al full sync

        items = await client.get_all_listings(included_data=["fulfillmentAvailability"])
        updates: list[tuple[str, str, int]] = []
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


async def _loop():
    """Loop periódico:
    - Arranque (60s): full sync
    - Cada 10 min: qty-only sync
    - Cada 6h: full reconciliation
    """
    await asyncio.sleep(60)   # esperar a que el servidor esté listo
    await run_amazon_listing_sync()
    while True:
        await asyncio.sleep(_QTY_SYNC_INTERVAL)   # despertar cada 10 min
        # qty-only sync (siempre)
        try:
            await run_amazon_qty_sync()
        except Exception as e:
            logger.error(f"[AMZ-QTY-LOOP] Error: {e}")
        # full sync si pasaron ≥6h
        if (_time.time() - _last_sync_ts) >= _FULL_INTERVAL:
            try:
                await run_amazon_listing_sync()
            except Exception as e:
                logger.error(f"[AMZ-LISTING-SYNC-LOOP] Error: {e}")


def start_amazon_listing_sync():
    """Inicia el loop en background. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_loop())
    logger.info(
        f"[AMZ-LISTING-SYNC] Iniciado — qty cada {_QTY_SYNC_INTERVAL//60}min, "
        f"full sync cada {_FULL_INTERVAL//3600}h"
    )


def get_sync_status() -> dict:
    return {
        "running":      _sync_running,
        "error":        _sync_error,
        "last_sync_ts": _last_sync_ts,
        **_sync_status,
    }
