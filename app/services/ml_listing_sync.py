"""
ml_listing_sync.py — Sincronización incremental de listings ML → DB local

Estrategia:
  - Arranque: sync completo de active+paused para todas las cuentas (background)
  - Cada 10 min: sync incremental (top-50 por last_updated) — solo lo nuevo/cambiado
  - Cada 6h: reconciliación completa para capturar cerrados/inactivos

Beneficio: el tab Stock lee de DB local (instantáneo) en lugar de llamar ML API
cada vez (60-150s). El spinner desaparece para siempre.
"""
import asyncio
import logging
import time as _time
from types import SimpleNamespace

from app.services.sku_utils import extract_item_sku

logger = logging.getLogger(__name__)

_INCREMENTAL_INTERVAL = 10 * 60   # 10 minutos
_FULL_RECONCILE_INTERVAL = 6 * 3600  # 6 horas
_sync_running = False
_last_full_sync_ts = 0.0
_last_incremental_ts = 0.0
_sync_error = ""
_sync_status = {"accounts_synced": 0, "items_total": 0, "last_run_iso": None}


def _item_to_row(item: dict, account_id: str) -> dict:
    """Convierte un body de ML item a un row de ml_listings."""
    sku = extract_item_sku(item)

    shipping = item.get("shipping") or {}
    logistic_type = shipping.get("logistic_type", "")
    is_full = 1 if logistic_type == "fulfillment" else 0

    return {
        "item_id":        str(item.get("id", "")),
        "account_id":     account_id,
        "title":          (item.get("title") or "")[:200],
        "status":         item.get("status", "active"),
        "price":          float(item.get("price") or 0),
        "available_qty":  int(item.get("available_quantity") or 0),
        "sold_qty":       int(item.get("sold_quantity") or 0),
        "sku":            sku,
        "logistic_type":  logistic_type,
        "catalog_listing": 1 if item.get("catalog_listing") else 0,
        "is_full":        is_full,
        "last_updated":   item.get("last_updated") or item.get("date_created") or "",
        "synced_at":      _time.time(),
    }


async def _sync_account_full(uid: str, client) -> int:
    """Sync completo active+paused para una cuenta. Retorna items sincronizados."""
    from app.services import token_store

    try:
        item_ids = await client.get_all_item_ids_by_statuses(["active", "paused"])
        if not item_ids:
            return 0

        rows = []
        sem = asyncio.Semaphore(5)

        async def _batch(ids):
            async with sem:
                try:
                    return await client.get_items_details(ids)
                except Exception as e:
                    logger.warning(f"[ML-SYNC] batch error uid={uid}: {e}")
                    return []

        batches = [item_ids[i:i+20] for i in range(0, len(item_ids), 20)]
        results = await asyncio.gather(*[_batch(b) for b in batches], return_exceptions=True)

        for batch_result in results:
            if isinstance(batch_result, Exception):
                continue
            for entry in (batch_result or []):
                item = entry.get("body") if isinstance(entry, dict) and "body" in entry else entry
                if isinstance(item, dict) and item.get("id"):
                    rows.append(_item_to_row(item, uid))

        if rows:
            await token_store.upsert_ml_listings(rows)
        logger.info(f"[ML-SYNC] Full sync uid={uid}: {len(rows)} items")
        return len(rows)
    except Exception as e:
        logger.warning(f"[ML-SYNC] Full sync error uid={uid}: {e}")
        return 0


async def _sync_account_incremental(uid: str, client) -> int:
    """Sync incremental: top-50 items por last_updated_date. Retorna items actualizados."""
    import httpx
    from app.services import token_store

    try:
        # Obtener los 50 items más recientemente modificados
        url = f"https://api.mercadolibre.com/users/{uid}/items/search"
        params = {"sort": "last_updated_date", "limit": 50, "status": "active"}
        headers = {"Authorization": f"Bearer {client.access_token}"}

        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.get(url, params=params, headers=headers)
            if r.status_code != 200:
                return 0
            data = r.json()
            item_ids = data.get("results", [])

        if not item_ids:
            return 0

        # Fetch detalles
        entries = await client.get_items_details(item_ids[:20])
        rows = []
        for entry in (entries or []):
            item = entry.get("body") if isinstance(entry, dict) and "body" in entry else entry
            if isinstance(item, dict) and item.get("id"):
                rows.append(_item_to_row(item, uid))

        if rows:
            await token_store.upsert_ml_listings(rows)
        return len(rows)
    except Exception as e:
        logger.warning(f"[ML-SYNC] Incremental error uid={uid}: {e}")
        return 0


async def run_ml_listing_sync(full: bool = False) -> dict:
    """Ejecuta sync para todas las cuentas ML."""
    global _sync_running, _last_full_sync_ts, _last_incremental_ts, _sync_error, _sync_status

    if _sync_running:
        return {"status": "already_running"}

    _sync_running = True
    _sync_error = ""
    t0 = _time.time()
    total_items = 0
    accounts_done = 0

    try:
        from app.services import token_store
        from app.services.meli_client import get_meli_client

        ml_accounts = await token_store.get_all_tokens()
        if not ml_accounts:
            return {"status": "no_accounts"}

        for acc in ml_accounts:
            uid = acc.get("user_id", "")
            if not uid:
                continue
            try:
                client = await get_meli_client(user_id=uid)
                if not client:
                    continue
                try:
                    # Full sync si: primer arranque, reconciliación periódica, o forzado
                    needs_full = full or (_time.time() - _last_full_sync_ts) > _FULL_RECONCILE_INTERVAL
                    db_count = await token_store.count_ml_listings_synced(uid)
                    if db_count == 0:
                        needs_full = True  # DB vacía → siempre full

                    if needs_full:
                        n = await _sync_account_full(uid, client)
                    else:
                        n = await _sync_account_incremental(uid, client)
                    total_items += n
                    accounts_done += 1
                finally:
                    await client.close()
            except Exception as e:
                logger.warning(f"[ML-SYNC] Error cuenta {uid}: {e}")

        if full or (_time.time() - _last_full_sync_ts) > _FULL_RECONCILE_INTERVAL:
            _last_full_sync_ts = _time.time()
        _last_incremental_ts = _time.time()

        from datetime import datetime
        _sync_status = {
            "accounts_synced": accounts_done,
            "items_total": total_items,
            "last_run_iso": datetime.utcnow().isoformat(),
            "elapsed_s": round(_time.time() - t0, 1),
        }
        logger.info(f"[ML-SYNC] Done: {accounts_done} cuentas, {total_items} items, {_sync_status['elapsed_s']}s")
        return {"status": "ok", **_sync_status}

    except Exception as e:
        _sync_error = str(e)[:200]
        logger.exception(f"[ML-SYNC] Fatal: {e}")
        return {"status": "error", "error": _sync_error}
    finally:
        _sync_running = False


async def _loop():
    """Loop periódico del sync."""
    # Primer run: full sync al arranque (delay de 30s para que el servidor esté listo)
    await asyncio.sleep(30)
    await run_ml_listing_sync(full=True)

    while True:
        await asyncio.sleep(_INCREMENTAL_INTERVAL)
        try:
            await run_ml_listing_sync(full=False)
        except Exception as e:
            logger.error(f"[ML-SYNC-LOOP] Error: {e}")


def start_ml_listing_sync():
    """Inicia el loop en background. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_loop())
    logger.info(f"[ML-SYNC] Iniciado — incremental cada {_INCREMENTAL_INTERVAL//60}min, full cada {_FULL_RECONCILE_INTERVAL//3600}h")


def get_sync_status() -> dict:
    return {
        "running": _sync_running,
        "error": _sync_error,
        "last_incremental_ts": _last_incremental_ts,
        "last_full_sync_ts": _last_full_sync_ts,
        **_sync_status,
    }
