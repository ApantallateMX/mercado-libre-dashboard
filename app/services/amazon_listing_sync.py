"""
amazon_listing_sync.py — Sincronización de listings Amazon → DB local

Estrategia:
  - Arranque: sync completo para todas las cuentas Amazon (60s después del arranque)
  - Cada 6h: reconciliación completa automática
  - Manual: trigger via POST /api/listings/refresh

Solo descarga datos — cero escrituras a Amazon Seller Central.
"""
import asyncio
import logging
import time as _time

logger = logging.getLogger(__name__)

_FULL_INTERVAL = 6 * 3600   # 6 horas entre syncs automáticos
_sync_running  = False
_last_sync_ts  = 0.0
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
    channel = (fa0.get("fulfillmentChannelCode") or "").upper()
    qty     = int(fa0.get("quantity") or 0)

    price = 0.0
    for offer in (item.get("offers") or []):
        if offer.get("offerType") == "B2C":
            try:
                price = float(offer.get("price", {}).get("amount") or 0)
            except (TypeError, ValueError):
                pass
            break

    status    = (summary.get("status") or "ACTIVE").upper()
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


async def _loop():
    """Loop periódico: sync al arranque + cada 6h."""
    await asyncio.sleep(60)   # esperar a que el servidor esté listo
    await run_amazon_listing_sync()
    while True:
        await asyncio.sleep(_FULL_INTERVAL)
        try:
            await run_amazon_listing_sync()
        except Exception as e:
            logger.error(f"[AMZ-LISTING-SYNC-LOOP] Error: {e}")


def start_amazon_listing_sync():
    """Inicia el loop en background. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_loop())
    logger.info(f"[AMZ-LISTING-SYNC] Iniciado — full sync cada {_FULL_INTERVAL // 3600}h")


def get_sync_status() -> dict:
    return {
        "running":      _sync_running,
        "error":        _sync_error,
        "last_sync_ts": _last_sync_ts,
        **_sync_status,
    }
