"""
ml_listing_sync.py — Sincronización incremental de listings ML → DB local

Estrategia:
  - Arranque: sync completo de active+paused para todas las cuentas (background)
  - Cada 3 min: qty-only sync — solo available_quantity para todos los item_ids conocidos
  - Cada 10 min: sync incremental (top-50 por last_updated) — actualiza metadata de los más recientes
  - Cada 6h: reconciliación completa para capturar listings nuevos/cerrados/inactivos

El qty-only sync (3 min) es ligero: 1 llamada ML API por lote de 20 items, solo trae
id+available_quantity. Detecta caídas de stock rápido para prevenir sobreventa.
"""
import asyncio
import json
import logging
import time as _time
from types import SimpleNamespace

from app.services.sku_utils import extract_item_sku

logger = logging.getLogger(__name__)

_QTY_SYNC_INTERVAL      = 3 * 60     # 3 minutos — qty-only (ligero)
_INCREMENTAL_INTERVAL   = 10 * 60    # 10 minutos — metadata de los más recientes
_FULL_RECONCILE_INTERVAL = 6 * 3600  # 6 horas — reconciliación completa

_sync_running      = False
_qty_sync_running  = False
_last_full_sync_ts        = 0.0
_last_incremental_ts      = 0.0
_last_qty_sync_ts         = 0.0
_sync_error  = ""
_sync_status = {"accounts_synced": 0, "items_total": 0, "last_run_iso": None}

# Callback registrado por main.py para invalidar _products_cache + _stock_issues_cache
# sin crear una dependencia circular.
_on_listings_updated = None


def register_listings_updated_callback(fn):
    """main.py llama esto en el lifespan para registrar la función de invalidación."""
    global _on_listings_updated
    _on_listings_updated = fn


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
        "data_json":      json.dumps(item, ensure_ascii=False),
    }


async def _sync_account_full(uid: str, client) -> int:
    """Sync completo active+paused para una cuenta. Retorna items sincronizados."""
    from app.services import token_store

    try:
        # "inactive" = "Inactiva sin stock" (ML auto-desactivó por qty=0) — incluir para alertas BM.
        item_ids = await client.get_all_item_ids_by_statuses(["active", "paused", "inactive"])
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
            if _on_listings_updated:
                try:
                    _on_listings_updated(uid)
                except Exception:
                    pass

        # ── Detección de huérfanos ──────────────────────────────────────────
        # Comparar item_ids devueltos por API vs los que están en DB para esta cuenta.
        # Los que están en DB pero no en la respuesta API = eliminados en ML.
        try:
            fresh_ids = {r["item_id"] for r in rows}
            db_rows = await token_store.get_ml_listings(uid)
            db_ids  = {r["item_id"] for r in db_rows}
            orphan_ids = db_ids - fresh_ids
            await token_store.clear_orphans_for_account("ml", uid)
            if orphan_ids:
                # Lookup title/sku de los huérfanos desde DB
                db_map = {r["item_id"]: r for r in db_rows}
                orphan_entries = [
                    {
                        "platform":   "ml",
                        "account_id": uid,
                        "item_id":    iid,
                        "title":      db_map.get(iid, {}).get("title", ""),
                        "sku":        db_map.get(iid, {}).get("sku", ""),
                    }
                    for iid in orphan_ids
                ]
                await token_store.save_orphan_listings(orphan_entries)
                logger.info(f"[ML-SYNC] uid={uid}: {len(orphan_ids)} listings huérfanos detectados")
        except Exception as _oe:
            logger.warning(f"[ML-SYNC] Error detectando huérfanos uid={uid}: {_oe}")

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
        headers = {"Authorization": f"Bearer {client.access_token}"}

        # Buscar active + inactive (re-activados post-sync no aparecen solo con active)
        item_ids = []
        async with httpx.AsyncClient(timeout=15.0) as http:
            for status in ("active", "inactive"):
                r = await http.get(url, params={"sort": "last_updated_date", "limit": 25, "status": status}, headers=headers)
                if r.status_code == 200:
                    item_ids.extend(r.json().get("results", []))
        # Deduplicar preservando orden
        seen: set = set()
        item_ids = [x for x in item_ids if not (x in seen or seen.add(x))]

        if not item_ids:
            return 0

        # Fetch detalles (top 40 más recientes entre active + inactive)
        entries = await client.get_items_details(item_ids[:40])
        rows = []
        for entry in (entries or []):
            item = entry.get("body") if isinstance(entry, dict) and "body" in entry else entry
            if isinstance(item, dict) and item.get("id"):
                rows.append(_item_to_row(item, uid))

        if rows:
            await token_store.upsert_ml_listings(rows)
            if _on_listings_updated:
                try:
                    _on_listings_updated(uid)
                except Exception:
                    pass
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


async def _sync_qty_only_account(uid: str, client) -> int:
    """Sync ligero: solo available_quantity para todos los item_ids conocidos de esta cuenta.
    Usa GET /items?ids=...&attributes=id,available_quantity — 1 llamada por lote de 20.
    Retorna número de items cuya qty cambió en DB.
    """
    from app.services import token_store
    try:
        rows = await token_store.get_ml_listings(uid)
        if not rows:
            return 0

        current_qtys = {r["item_id"]: r["available_qty"] for r in rows}
        item_ids = list(current_qtys.keys())
        updates: list[tuple[str, int]] = []

        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i + 20]
            ids_str = ",".join(batch)
            try:
                resp = await client.get(f"/items?ids={ids_str}&attributes=id,available_quantity")
                entries = resp if isinstance(resp, list) else []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    body = entry.get("body") if "body" in entry else entry
                    if not isinstance(body, dict):
                        continue
                    iid = str(body.get("id", ""))
                    qty = int(body.get("available_quantity") or 0)
                    if iid and current_qtys.get(iid, -1) != qty:
                        updates.append((iid, qty))
            except Exception as e:
                logger.warning(f"[ML-QTY] batch error uid={uid} offset={i}: {e}")

        if not updates:
            return 0

        changed = await token_store.update_ml_qty_batch(updates)
        if changed > 0:
            logger.info(f"[ML-QTY] uid={uid}: {changed}/{len(updates)} items qty actualizada")
            if _on_listings_updated:
                try:
                    _on_listings_updated(uid)
                except Exception:
                    pass
        return changed
    except Exception as e:
        logger.warning(f"[ML-QTY] Error cuenta {uid}: {e}")
        return 0


async def run_ml_qty_sync() -> dict:
    """Sync ligero de qty para todas las cuentas ML.
    No corre si ya hay un sync completo/incremental en progreso.
    """
    global _qty_sync_running, _last_qty_sync_ts
    if _qty_sync_running or _sync_running:
        return {"status": "skipped"}
    _qty_sync_running = True
    t0 = _time.time()
    total_changed = 0
    accounts_done = 0
    try:
        from app.services import token_store
        from app.services.meli_client import get_meli_client
        accounts = await token_store.get_all_tokens()
        for acc in accounts:
            uid = acc.get("user_id", "")
            if not uid:
                continue
            client = await get_meli_client(user_id=uid)
            if not client:
                continue
            try:
                n = await _sync_qty_only_account(uid, client)
                total_changed += n
                accounts_done += 1
            finally:
                await client.close()
        _last_qty_sync_ts = _time.time()
        logger.debug(f"[ML-QTY] {accounts_done} cuentas, {total_changed} cambios, {round(_time.time()-t0,1)}s")
        return {"status": "ok", "changed": total_changed, "elapsed_s": round(_time.time() - t0, 1)}
    except Exception as e:
        logger.warning(f"[ML-QTY] Fatal: {e}")
        return {"status": "error", "error": str(e)[:200]}
    finally:
        _qty_sync_running = False


async def _loop():
    """Loop periódico del sync.
    - Cada 3 min: qty-only (ligero)
    - Cada 10 min: incremental (metadata top-50)
    - Cada 6h: full reconciliation
    """
    # Primer run: full sync al arranque (delay de 30s para que el servidor esté listo)
    await asyncio.sleep(30)
    await run_ml_listing_sync(full=True)

    while True:
        await asyncio.sleep(_QTY_SYNC_INTERVAL)   # despertar cada 3 min
        # qty-only sync (siempre)
        try:
            await run_ml_qty_sync()
        except Exception as e:
            logger.error(f"[ML-QTY-LOOP] Error: {e}")
        # incremental sync si pasaron ≥10 min desde el último
        if (_time.time() - _last_incremental_ts) >= _INCREMENTAL_INTERVAL:
            try:
                await run_ml_listing_sync(full=False)
            except Exception as e:
                logger.error(f"[ML-SYNC-LOOP] Error: {e}")


def start_ml_listing_sync():
    """Inicia el loop en background. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_loop())
    logger.info(
        f"[ML-SYNC] Iniciado — qty cada {_QTY_SYNC_INTERVAL//60}min, "
        f"incremental cada {_INCREMENTAL_INTERVAL//60}min, "
        f"full cada {_FULL_RECONCILE_INTERVAL//3600}h"
    )


def get_sync_status() -> dict:
    return {
        "running": _sync_running,
        "error": _sync_error,
        "last_incremental_ts": _last_incremental_ts,
        "last_full_sync_ts": _last_full_sync_ts,
        **_sync_status,
    }
