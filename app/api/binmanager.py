"""
BinManager API Router
=====================
Endpoints para precios en tiempo real de BinManager WMS.

GET  /api/bm/prices          — precios actuales de todos los SKUs watcheados
GET  /api/bm/history         — historial de cambios detectados
GET  /api/bm/stream          — SSE: notificaciones en vivo de cambios de precio
POST /api/bm/watch/{sku}     — agregar SKU al monitor
DELETE /api/bm/watch/{sku}   — quitar SKU del monitor
GET  /api/bm/skus            — lista de SKUs monitoreados
POST /api/bm/refresh         — forzar fetch inmediato de precios
"""
import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.services.price_monitor import price_monitor

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
