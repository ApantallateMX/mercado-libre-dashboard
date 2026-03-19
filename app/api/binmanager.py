"""
BinManager API Router
=====================
Endpoints para precios en tiempo real de BinManager WMS.

GET  /api/bm/prices               — precios actuales de todos los SKUs watcheados
GET  /api/bm/history              — historial de cambios detectados
GET  /api/bm/stream               — SSE: notificaciones en vivo de cambios de precio
POST /api/bm/watch/{sku}          — agregar SKU al monitor
DELETE /api/bm/watch/{sku}        — quitar SKU del monitor
GET  /api/bm/skus                 — lista de SKUs monitoreados
POST /api/bm/refresh              — forzar fetch inmediato de precios
POST /api/bm/retail-ph-batch      — RetailPrice PH para lista de SKUs
GET  /api/bm/price-health         — semáforo: % productos sobre/bajo RetailPrice PH
"""
import asyncio
import json
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List

from app.services.price_monitor import price_monitor
from app.services.binmanager_client import BinManagerClient

router = APIRouter(prefix="/api/bm", tags=["binmanager"])


@router.get("/prices")
async def get_prices():
    """Precios actuales (LastRetailPricePurchaseHistory) de todos los SKUs."""
    return {
        "prices": price_monitor.get_current_prices(),
        "poll_interval_seconds": price_monitor.poll_interval,
    }


@router.get("/history")
async def get_history(limit: int = 50):
    """Historial de cambios de precio (más reciente primero)."""
    return {"history": price_monitor.get_price_history(limit)}


@router.get("/skus")
async def get_watched_skus():
    """Lista de SKUs monitoreados actualmente."""
    return {"skus": price_monitor.get_watched_skus()}


@router.post("/watch/{sku}")
async def watch_sku(sku: str):
    """Agrega un SKU al monitor de precios."""
    added = price_monitor.add_sku(sku)
    if added:
        # Fetch inmediato del nuevo SKU en background
        asyncio.create_task(price_monitor._check_prices(initial=True))
    return {
        "sku": sku.upper(),
        "watching": True,
        "added": added,
        "message": f"{'Agregado' if added else 'Ya estaba en lista'}: {sku.upper()}",
    }


@router.delete("/watch/{sku}")
async def unwatch_sku(sku: str):
    """Quita un SKU del monitor de precios."""
    removed = price_monitor.remove_sku(sku)
    return {
        "sku": sku.upper(),
        "watching": False,
        "removed": removed,
        "message": f"{'Removido' if removed else 'No estaba en lista'}: {sku.upper()}",
    }


@router.post("/refresh")
async def force_refresh():
    """Fuerza un fetch inmediato de todos los precios."""
    asyncio.create_task(price_monitor._check_prices())
    return {"message": "Refresh iniciado en background"}


@router.get("/stream")
async def price_stream():
    """
    SSE endpoint — notificaciones en tiempo real de cambios de precio.

    Eventos emitidos:
      type: init    — precios actuales al conectar
      type: change  — cambio detectado {sku, old_price, new_price, delta, timestamp}
      : keepalive   — ping cada 25s para mantener conexión
    """
    q = price_monitor.subscribe()

    async def event_generator():
        try:
            # Evento inicial con todos los precios actuales
            init_data = json.dumps({
                "type": "init",
                "prices": price_monitor.get_current_prices(),
                "poll_interval": price_monitor.poll_interval,
            })
            yield f"data: {init_data}\n\n"

            while True:
                try:
                    change = await asyncio.wait_for(q.get(), timeout=25)
                    payload = json.dumps({"type": "change", **change})
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            price_monitor.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Caché local para retail-ph-batch ─────────────────────────────────────────
_ph_batch_cache: dict[str, tuple[float, float]] = {}  # sku -> (ts, price_usd)
_PH_BATCH_TTL = 1800  # 30 min
_bm_client = BinManagerClient()
_bm_client_ready = False


class SkuBatchRequest(BaseModel):
    skus: List[str]


@router.post("/retail-ph-batch")
async def retail_ph_batch(body: SkuBatchRequest):
    """
    RetailPrice PH para una lista de SKUs.
    Usa caché de 30 min. Primera llamada hace login automático.
    Respuesta: {sku: price_usd | null}
    """
    global _bm_client_ready
    if not _bm_client_ready:
        _bm_client_ready = await _bm_client.login()

    now = time.time()
    result = {}
    to_fetch = []

    for sku in body.skus:
        sku_up = sku.upper()
        cached = _ph_batch_cache.get(sku_up)
        if cached and (now - cached[0]) < _PH_BATCH_TTL:
            result[sku_up] = cached[1]
        else:
            to_fetch.append(sku_up)

    # Fetch en paralelo con semáforo
    sem = asyncio.Semaphore(10)

    async def fetch_one(sku):
        async with sem:
            price = await _bm_client.get_retail_price_ph(sku)
            _ph_batch_cache[sku] = (now, price)
            return sku, price

    if to_fetch:
        fetched = await asyncio.gather(*[fetch_one(s) for s in to_fetch])
        for sku, price in fetched:
            result[sku] = price

    return {"prices": result, "cached": len(body.skus) - len(to_fetch)}


@router.get("/price-health")
async def price_health():
    """
    Semáforo de salud de precios: compara precios de los SKUs watcheados.
    Retorna cuántos están sobre/bajo RetailPrice PH y el promedio de margen.

    Diseñado para el widget del dashboard principal.
    """
    prices = price_monitor.get_current_prices()
    if not prices:
        return {"status": "no_data", "above": 0, "below": 0, "no_ph": 0, "total": 0}

    above = sum(1 for p in prices if p.get("price") and p["price"] > 0)
    no_ph = sum(1 for p in prices if not p.get("price") or p["price"] == 0)
    below = len(prices) - above - no_ph

    return {
        "total": len(prices),
        "above": above,
        "below": below,
        "no_ph": no_ph,
        "above_pct": round(above / len(prices) * 100, 1) if prices else 0,
        "below_pct": round(below / len(prices) * 100, 1) if prices else 0,
        "prices": prices,
    }
