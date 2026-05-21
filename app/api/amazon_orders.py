"""
amazon_orders.py — Historial de Órdenes Amazon

PROPÓSITO:
    Endpoints para la página /amazon/orders:
    1. GET /api/amazon/orders              → HTML partial con stats + tabla de órdenes (TTL 5 min)
    2. GET /api/amazon/orders/{id}/items   → HTML partial con items de la orden (TTL 10 min)

CACHÉ:
    _orders_cache:  {seller_id:date_from:date_to → (ts, (orders, stats))} TTL 300s
    _items_cache:   {order_id → (ts, list)}                                TTL 600s
"""

import asyncio
import time as _time
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.services.amazon_client import get_amazon_client


def _save_amazon_items_history_bg(
    order_id: str, seller_id: str, items: list,
    order_date_str: str, status_es: str, currency: str,
    real_fees: dict | None = None,
) -> None:
    """Fire-and-forget: persiste items de orden Amazon en order_history."""
    async def _do():
        try:
            from app.services import token_store as _ts
            from app.services.sku_utils import normalize_to_bm_sku as _norm
            # Intentar leer FX y mapas de costo/retail de main (lazy import — evita circular)
            try:
                from app.main import _last_fx_rate, _sku_cost_map, _sku_retail_map, _PARTNER_COMMISSION_PCT
                fx = _last_fx_rate or 17.0
            except Exception:
                fx, _sku_cost_map, _sku_retail_map, _PARTNER_COMMISSION_PCT = 17.0, {}, {}, 0.07
            # Fecha: "YYYY-MM-DD HH:MM" → "YYYY-MM-DD"
            order_date = (order_date_str or "")[:10]
            order_month = order_date[:7] if len(order_date) >= 7 else ""
            rows = []
            for it in items:
                sku_raw    = (it.get("sku") or "").strip()
                if sku_raw in ("—", "-", ""):
                    sku_raw = ""
                sku        = _norm(sku_raw) if sku_raw else ""
                unit_price = float(it.get("unit_price") or 0)
                qty        = int(it.get("qty") or 1)
                asin       = it.get("asin") or ""
                if unit_price <= 0:
                    continue
                subtotal   = unit_price * qty
                ship       = float(it.get("shipping") or 0)
                taxes      = float(it.get("tax") or 0)
                if real_fees and not real_fees.get("is_estimated"):
                    # Distribuir fees reales proporcionalmente por item
                    ratio    = (unit_price / max(sum(i.get("unit_price",0) for i in items), 1))
                    sale_fee = round((real_fees["referral_fee"] + real_fees["fba_fee"] + real_fees["other_fees"]) * ratio, 2)
                    data_src = "finances_api"
                else:
                    sale_fee = subtotal * 0.10  # estimado 10%
                    data_src = "estimated"
                neto_plat  = subtotal - sale_fee - taxes - ship
                costo_mxn  = _sku_cost_map.get(sku, 0) if sku else 0
                retail_mxn = _sku_retail_map.get(sku, 0) if sku else 0
                retail_ph_usd = round(retail_mxn / fx, 2) if (retail_mxn > 0 and fx > 0) else 0
                costo_usd  = round(costo_mxn / fx, 2) if (costo_mxn > 0 and fx > 0) else 0
                ganancia   = neto_plat * (1 - _PARTNER_COMMISSION_PCT) - costo_mxn
                margen_pct = round(ganancia / unit_price * 100, 1) if unit_price > 0 else 0
                recup      = round(neto_plat / retail_mxn * 100, 1) if retail_mxn > 0 else 0
                rows.append({
                    "order_id": order_id, "account_id": seller_id, "platform": "amazon",
                    "item_id": asin, "sku": sku,
                    "unit_price": unit_price, "quantity": qty,
                    "sale_fee": round(sale_fee, 2), "neto_plat": round(neto_plat, 2),
                    "costo_usd": costo_usd, "costo_mxn": round(costo_mxn, 2),
                    "retail_ph_usd": retail_ph_usd,
                    "ganancia_neta": round(ganancia, 2),
                    "margen_pct": margen_pct, "recup_retail_pct": recup,
                    "fx_rate": round(fx, 4), "currency": currency or "MXN",
                    "order_date": order_date, "order_month": order_month,
                    "status": status_es, "data_source": data_src,
                })
            if rows:
                await _ts.upsert_order_history(rows)
        except Exception as _e:
            logging.getLogger(__name__).warning(f"[ORDER-HIST] Amazon error: {_e}")
    try:
        asyncio.create_task(_do())
    except Exception:
        pass

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/amazon", tags=["amazon-orders"])

_templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# ─────────────────────────────────────────────────────────────────────────────
# CACHÉ en memoria
# ─────────────────────────────────────────────────────────────────────────────
_orders_cache:     dict[str, tuple[float, tuple]] = {}
_items_cache:      dict[str, tuple[float, list]]  = {}
_financials_cache: dict[str, tuple[float, dict]]  = {}

_ORDERS_TTL     = 300    # 5 minutos
_ITEMS_TTL      = 600    # 10 minutos
_FINANCIALS_TTL = 3600   # 1 hora (fees no cambian una vez liquidados)

# ─────────────────────────────────────────────────────────────────────────────
# ESTADO → ESPAÑOL + CSS badge
# ─────────────────────────────────────────────────────────────────────────────
_STATUS_ES: dict[str, tuple[str, str]] = {
    "Pending":            ("Pendiente",     "bg-gray-100 text-gray-600"),
    "Unshipped":          ("Por enviar",    "bg-yellow-100 text-yellow-700"),
    "PartiallyShipped":   ("Parcial",       "bg-blue-100 text-blue-700"),
    "Shipped":            ("Enviado",       "bg-green-100 text-green-700"),
    "Canceled":           ("Cancelado",     "bg-red-100 text-red-700"),
    "Cancelled":          ("Cancelado",     "bg-red-100 text-red-700"),
    "InvoiceUnconfirmed": ("Sin confirmar", "bg-gray-100 text-gray-600"),
    "Unfulfillable":      ("No entregable", "bg-red-100 text-red-700"),
}
_STATUS_DEFAULT = ("Desconocido", "bg-gray-100 text-gray-500")

_CANCELED_STATUSES = {"Canceled", "Cancelled"}

# Orden de prioridad para el sort: no-pendientes primero
_STATUS_PRIORITY: dict[str, int] = {
    "Shipped":            1,
    "Unshipped":          2,
    "PartiallyShipped":   3,
    "InvoiceUnconfirmed": 4,
    "Unfulfillable":      5,
    "Pending":            6,
    "Canceled":           7,
    "Cancelled":          7,
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _purchase_date_cst(iso_date: str) -> str:
    """Convierte PurchaseDate (UTC) a hora CST (UTC-6) y devuelve 'YYYY-MM-DD HH:MM'."""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%dT%H:%M:%SZ")
        dt_cst = dt - timedelta(hours=6)
        return dt_cst.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_date[:10] if iso_date else ""


def _normalize_order(order: dict) -> dict:
    """Convierte un dict crudo de la SP-API al formato que usa la plantilla."""
    order_total = order.get("OrderTotal") or {}
    try:
        amount = float(order_total.get("Amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0

    currency     = order_total.get("CurrencyCode", "MXN")
    fulfillment  = order.get("FulfillmentChannel", "")
    canal        = "FBA" if fulfillment == "AFN" else "FBM"
    canal_css    = "bg-orange-100 text-orange-700" if canal == "FBA" else "bg-blue-100 text-blue-700"

    status_raw   = order.get("OrderStatus", "")
    label, badge = _STATUS_ES.get(status_raw, _STATUS_DEFAULT)

    shipped   = int(order.get("NumberOfItemsShipped")   or 0)
    unshipped = int(order.get("NumberOfItemsUnshipped") or 0)
    units     = shipped + unshipped

    # Mostrar el total si Amazon lo provee; Pending puede tener OrderTotal disponible
    is_pending     = status_raw == "Pending"
    amount_display = f"${amount:,.2f}" if amount > 0 else "—"

    return {
        "order_id":        order.get("AmazonOrderId", ""),
        "date":            _purchase_date_cst(order.get("PurchaseDate", "")),
        "canal":           canal,
        "canal_css":       canal_css,
        "units":           units,
        "amount":          amount,
        "amount_display":  amount_display,
        "is_pending":      is_pending,
        "currency":        currency,
        "status":          label,
        "status_css":      badge,
        "status_raw":      status_raw,
        "status_priority": _STATUS_PRIORITY.get(status_raw, 9),
    }


def _calc_stats(orders: list) -> dict:
    canceled   = [o for o in orders if o["status_raw"] in _CANCELED_STATUSES]
    non_cancel = [o for o in orders if o["status_raw"] not in _CANCELED_STATUSES]
    return {
        "total_orders":   len(orders),
        "total_units":    sum(o["units"] for o in non_cancel),
        "total_revenue":  round(sum(o["amount"] for o in non_cancel), 2),
        "total_canceled": len(canceled),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 1: Tabla de órdenes (stats + filas)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/orders", response_class=HTMLResponse)
async def get_amazon_orders(
    request:   Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to:   str = Query("", description="YYYY-MM-DD"),
):
    """
    Devuelve el HTML del banner de stats + tabla de órdenes Amazon.
    Llamado desde amazon_orders.html vía HTMX al cargar la página o cambiar filtros.
    """
    active_amazon_id = request.cookies.get("active_amazon_id")
    client = await get_amazon_client(active_amazon_id)

    if not client:
        return HTMLResponse(
            "<p class='text-red-500 p-6 text-center'>No hay cuenta Amazon configurada.</p>"
        )

    # Fechas por defecto: últimos 7 días (hora México CST = UTC-6)
    now_mx = datetime.utcnow() - timedelta(hours=6)
    if not date_to:
        date_to = now_mx.strftime("%Y-%m-%d")
    if not date_from:
        date_from = (now_mx - timedelta(days=6)).strftime("%Y-%m-%d")

    cache_key = f"{client.seller_id}:{date_from}:{date_to}"
    cached = _orders_cache.get(cache_key)
    if cached and (_time.time() - cached[0]) < _ORDERS_TTL:
        orders, stats = cached[1]
    else:
        try:
            # SP-API no permite mezclar Pending con otros estados → 2 llamadas
            normal  = await client.fetch_orders_range(date_from, date_to)
            pending = await client.fetch_orders_range(date_from, date_to, statuses=["Pending"])

            # Deduplicar por AmazonOrderId
            merged = {o["AmazonOrderId"]: o for o in (normal + pending)}
            orders = [_normalize_order(o) for o in merged.values()]
            # Sort estable en 2 pasos: primero fecha desc, luego prioridad asc
            # Resultado: dentro de cada grupo de estado, las más recientes aparecen primero
            orders.sort(key=lambda x: x["date"], reverse=True)
            orders.sort(key=lambda x: x["status_priority"])
        except Exception as exc:
            logger.error("[Amazon Orders] Error fetching orders: %s", exc)
            orders = []

        stats = _calc_stats(orders)
        _orders_cache[cache_key] = (_time.time(), (orders, stats))

    return _templates.TemplateResponse(
        request, "partials/amazon_orders_table.html", {            "orders":    orders,
            "stats":     stats,
            "date_from": date_from,
            "date_to":   date_to,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 2: Items de una orden (lazy — click en botón "Ver")
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(obj: dict, key: str = "Amount") -> float:
    """Lee Amount de un sub-dict SP-API de forma segura."""
    try:
        return float((obj or {}).get("Amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_fees_from_events(events: dict) -> dict | None:
    """
    Extrae fees reales de FinancialEvents (Finances API).
    Returns None si la orden aún no tiene eventos (Pending / no liquidada).
    Tipos conocidos: Commission (referral), FBAPerUnitFulfillmentFee, etc.
    Los amounts vienen negativos — Amazon los deduce del pago al seller.
    """
    shipment_events = events.get("ShipmentEventList", [])
    if not shipment_events:
        return None
    referral = fba = other = 0.0
    for event in shipment_events:
        for item in event.get("ShipmentItemList", []):
            for fee in item.get("ItemFeeList", []):
                ftype = fee.get("FeeType", "")
                amt   = abs(_safe_float(fee.get("FeeAmount") or {}))
                if ftype == "Commission":
                    referral += amt
                elif "FBA" in ftype or "Fulfillment" in ftype:
                    fba += amt
                else:
                    other += amt
    if referral + fba + other == 0:
        return None
    return {"referral_fee": round(referral, 2), "fba_fee": round(fba, 2),
            "other_fees": round(other, 2), "is_estimated": False}


def _estimate_fees(revenue: float, canal: str) -> dict:
    """Fees estimados cuando Finances API no tiene datos aún (orden Pending/reciente)."""
    return {
        "referral_fee": round(revenue * 0.15, 2),  # 15% conservador
        "fba_fee":      0.0,                        # no estimamos FBA — muy variable
        "other_fees":   0.0,
        "is_estimated": True,
    }


def _build_finanzas(
    items: list, desglose: dict, fees: dict, canal: str, currency: str
) -> dict:
    """Construye el bloque de rentabilidad para el template."""
    revenue  = desglose["subtotal"] + desglose["shipping"] - desglose["discount"]
    tax      = desglose["tax"]
    total_fees = fees["referral_fee"] + fees["fba_fee"] + fees["other_fees"] + tax
    neto     = round(revenue - total_fees, 2)

    # Pct referral para mostrar en UI
    ref_pct  = round(fees["referral_fee"] / revenue * 100, 1) if revenue > 0 else 0.0

    # Costo desde caché BM (lazy import)
    costo_usd = None
    try:
        from app.main import _sku_cost_map, _last_fx_rate, _PARTNER_COMMISSION_PCT  # type: ignore
        from app.services.sku_utils import normalize_to_bm_sku as _norm
        fx = float(_last_fx_rate or 17.0)
        commission_pct = float(_PARTNER_COMMISSION_PCT or 0.07)
        total_cost = 0.0
        for it in items:
            sku_bm = _norm(it["sku"]) if it["sku"] not in ("—", "", None) else ""
            if sku_bm:
                costo_mxn = _sku_cost_map.get(sku_bm, 0) or 0
                if costo_mxn > 0 and fx > 0:
                    total_cost += (costo_mxn / fx) * int(it.get("qty") or 1)
        if total_cost > 0:
            costo_usd = round(total_cost, 2)
    except Exception:
        commission_pct = 0.07

    ganancia = margen_pct = None
    if costo_usd is not None and neto > 0:
        ganancia  = round(neto * (1 - commission_pct) - costo_usd, 2)
        margen_pct = round(ganancia / revenue * 100, 1) if revenue > 0 else None

    return {
        "revenue":      round(revenue, 2),
        "referral_fee": fees["referral_fee"],
        "referral_pct": ref_pct,
        "fba_fee":      fees["fba_fee"],
        "other_fees":   fees["other_fees"],
        "tax":          tax,
        "neto":         neto,
        "costo_usd":    costo_usd,
        "ganancia":     ganancia,
        "margen_pct":   margen_pct,
        "is_estimated": fees["is_estimated"],
        "currency":     currency,
    }


@router.get("/orders/{order_id}/items", response_class=HTMLResponse)
async def get_order_items_partial(
    request:   Request,
    order_id:  str,
    # Datos de la orden pasados desde el JS (data-* del botón)
    amount:    float = Query(0.0,  description="OrderTotal.Amount"),
    status:    str   = Query("",   description="Estado en español"),
    canal:     str   = Query("",   description="FBA o FBM"),
    date:      str   = Query("",   description="Fecha CST formateada"),
    units:     int   = Query(0,    description="Unidades totales"),
    currency:  str   = Query("MXN"),
):
    """
    Devuelve HTML de 3 columnas (Productos | Desglose | Info Orden).
    Se inserta en la fila expandida al hacer click en "Ver ▼".
    """
    active_amazon_id = request.cookies.get("active_amazon_id")
    client = await get_amazon_client(active_amazon_id)

    if not client:
        return HTMLResponse("<p class='text-red-500 p-2'>Error: sin cliente Amazon.</p>")

    cached = _items_cache.get(order_id)
    if cached and (_time.time() - cached[0]) < _ITEMS_TTL:
        items = cached[1]
    else:
        try:
            raw_items = await client.get_order_items(order_id)
            items = []
            for item in raw_items:
                ip  = item.get("ItemPrice")         or {}
                sp  = item.get("ShippingPrice")     or {}
                tax = item.get("ItemTax")            or {}
                promo = item.get("PromotionDiscount") or {}
                qty = int(item.get("QuantityOrdered") or 0)
                unit_price = _safe_float(ip, "Amount")
                items.append({
                    "title":      (item.get("Title") or ""),
                    "sku":        item.get("SellerSKU") or "—",
                    "asin":       item.get("ASIN")      or "—",
                    "unit_price": unit_price,
                    "qty":        qty,
                    "total":      round(unit_price * qty, 2),
                    "shipping":   _safe_float(sp,    "Amount"),
                    "tax":        _safe_float(tax,   "Amount"),
                    "discount":   _safe_float(promo, "Amount"),
                    "currency":   ip.get("CurrencyCode", "MXN"),
                })
            _items_cache[order_id] = (_time.time(), items)
        except Exception as exc:
            logger.error("[Amazon Orders] Error fetching items for %s: %s", order_id, exc)
            items = []

    # Totales calculados de los items
    desglose = {
        "subtotal":    round(sum(i["total"]    for i in items), 2),
        "tax":         round(sum(i["tax"]      for i in items), 2),
        "shipping":    round(sum(i["shipping"] for i in items), 2),
        "discount":    round(sum(i["discount"] for i in items), 2),
        "order_total": round(amount, 2),
        "currency":    currency,
    }

    # ── Fees reales (Finances API) o estimados ────────────────────────────
    fin_cached = _financials_cache.get(order_id)
    if fin_cached and (_time.time() - fin_cached[0]) < _FINANCIALS_TTL:
        fees = fin_cached[1]
    else:
        fees = None
        try:
            events = await client.get_order_financial_events(order_id)
            fees   = _parse_fees_from_events(events)
        except Exception:
            pass
        if fees is None:
            fees = _estimate_fees(desglose["subtotal"] + desglose["shipping"] - desglose["discount"], canal)
        # Solo cachear datos reales; estimados se recalculan siempre
        if not fees["is_estimated"]:
            _financials_cache[order_id] = (_time.time(), fees)

    finanzas = _build_finanzas(items, desglose, fees, canal, currency)

    # Persistir en order_history (fire-and-forget, con fees reales si están disponibles)
    _save_amazon_items_history_bg(
        order_id, client.seller_id, items, date, status, currency,
        real_fees=None if fees["is_estimated"] else fees,
    )

    order_ctx = {
        "order_id": order_id,
        "amount":   amount,
        "status":   status,
        "canal":    canal,
        "date":     date,
        "units":    units,
        "currency": currency,
    }

    return _templates.TemplateResponse(
        request, "partials/amazon_order_items.html", {
            "items":     items,
            "desglose":  desglose,
            "finanzas":  finanzas,
            "order_ctx": order_ctx,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 3: Preview de producto (para poblar columna Producto en la tabla)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/orders/{order_id}/preview")
async def get_order_preview(request: Request, order_id: str):
    """
    Devuelve JSON con el primer item de la orden para mostrar en la fila de la tabla.
    Usa el mismo caché de items que el endpoint principal.
    Retorna: {title, sku, asin, items_count}
    """
    active_amazon_id = request.cookies.get("active_amazon_id")
    client = await get_amazon_client(active_amazon_id)
    if not client:
        return JSONResponse({"error": "no client"}, status_code=401)

    cached = _items_cache.get(order_id)
    if cached and (_time.time() - cached[0]) < _ITEMS_TTL:
        items = cached[1]
    else:
        try:
            raw_items = await client.get_order_items(order_id)
            items = []
            for item in raw_items:
                ip    = item.get("ItemPrice")         or {}
                sp    = item.get("ShippingPrice")     or {}
                tax   = item.get("ItemTax")            or {}
                promo = item.get("PromotionDiscount") or {}
                qty   = int(item.get("QuantityOrdered") or 0)
                unit_price = _safe_float(ip, "Amount")
                items.append({
                    "title":      (item.get("Title") or ""),
                    "sku":        item.get("SellerSKU") or "—",
                    "asin":       item.get("ASIN")      or "—",
                    "unit_price": unit_price,
                    "qty":        qty,
                    "total":      round(unit_price * qty, 2),
                    "shipping":   _safe_float(sp,    "Amount"),
                    "tax":        _safe_float(tax,   "Amount"),
                    "discount":   _safe_float(promo, "Amount"),
                    "currency":   ip.get("CurrencyCode", "MXN"),
                })
            _items_cache[order_id] = (_time.time(), items)
        except Exception as exc:
            logger.warning("[Amazon Preview] Error para %s: %s", order_id, exc)
            items = []

    if not items:
        return JSONResponse({"title": "—", "sku": "—", "asin": "—", "items_count": 0, "items_total": 0.0})

    first = items[0]
    items_total = round(sum(i["total"] for i in items), 2)
    return JSONResponse({
        "title":       first["title"],
        "sku":         first["sku"],
        "asin":        first["asin"],
        "items_count": len(items),
        "items_total": items_total,   # total calculado de items (útil para Pending sin OrderTotal)
    })
