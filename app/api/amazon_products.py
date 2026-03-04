"""
amazon_products.py — Centro de Productos Amazon

PROPÓSITO:
    Endpoints que alimentan la página /amazon/products con 4 tabs:
    1. Resumen   — KPIs del catálogo + órdenes recientes
    2. Catálogo  — Todos los listings con precio, stock FBA, estado
    3. FBA Stock — Breakdown detallado: disponible, reservado, dañado, en camino
    4. Buy Box   — Análisis competitivo y estado del Buy Box

FUENTES DE DATOS:
    - Listings Items API v2021-08-01  → catálogo del vendedor
    - FBA Inventory API v1            → stock en warehouses Amazon
    - Orders API v0                   → ventas recientes (sin por-SKU breakdown)
    - Product Pricing API v0          → Buy Box (rate-limited, top ASINs only)

CACHÉ:
    Los datos de Amazon son costosos de obtener (rate limits estrictos).
    Se usa caché agresivo:
      - Listings + FBA inventory: 5 minutos
      - Buy Box pricing: 10 minutos
    Clave de caché: "{seller_id}:{date_from}:{date_to}" o "{seller_id}:{tab}"
"""

import asyncio
import logging
import math
import re as _re
import time as _time
from datetime import datetime, timedelta
from typing import Optional
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel

from app.services.amazon_client import get_amazon_client
from app.api.metrics import _get_cached_order_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/amazon", tags=["amazon-products"])

# Templates — misma carpeta que el resto del dashboard
_templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# ─────────────────────────────────────────────────────────────────────────────
# CACHÉ en memoria
# ─────────────────────────────────────────────────────────────────────────────
_listings_cache:   dict[str, tuple[float, list]] = {}  # {seller_id: (ts, items)}
_fba_cache:        dict[str, tuple[float, list]] = {}  # {seller_id: (ts, summaries)}
_buybox_cache:     dict[str, tuple[float, dict]] = {}  # {seller_id:sku: (ts, data)}
_sku_sales_cache:      dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, {sku: {units,revenue}})}
_sku_sales_refreshing: set = set()  # seller_ids con refresh BG activo

_LISTINGS_TTL = 300   # 5 minutos
_FBA_TTL      = 300   # 5 minutos
_BUYBOX_TTL   = 600   # 10 minutos
_SKU_SALES_TTL = 1800  # 30 minutos (costo alto: get_order_items por cada orden)

# ─── Onsite Stock (Amazon Reports API) ────────────────────────────────────────
_onsite_stock_cache:  dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, {sku: qty})}
_onsite_stock_locks:  dict[str, asyncio.Lock] = {}
_ONSITE_STOCK_TTL = 1800  # 30 minutos (generación de reporte es costosa)

# Estado del sync en background (no bloquear el request principal)
_onsite_sync_state: dict[str, str] = {}  # {seller_id: "idle"/"syncing"/"done"/"error"}
_onsite_sync_count: dict[str, int] = {}  # {seller_id: skus_found}

# ─── Helpers de lectura de caché Onsite ──────────────────────────────────────

def _flx_cache_read(seller_id: str, sku: str) -> tuple[int, int]:
    """
    Lee (avail, reserved) del caché Onsite para un SKU.
    Retorna (0, 0) si el caché está vacío, expirado o el SKU no está.
    Soporta formato nuevo {sku: {"avail":x,"reserved":y}} y el antiguo {sku: qty}.
    """
    cached = _onsite_stock_cache.get(seller_id)
    if not cached:
        return 0, 0
    ts_o, onsite_map = cached
    if _time.time() - ts_o >= _ONSITE_STOCK_TTL:
        return 0, 0
    entry = onsite_map.get(sku)
    if entry is None:
        return 0, 0
    if isinstance(entry, dict):
        return int(entry.get("avail", 0)), int(entry.get("reserved", 0))
    return int(entry), 0  # formato antiguo (int directo)


def _flx_cache_valid(seller_id: str) -> bool:
    """True si el caché Onsite existe y no ha expirado."""
    cached = _onsite_stock_cache.get(seller_id)
    return bool(cached) and (_time.time() - cached[0] < _ONSITE_STOCK_TTL)


# ─── BinManager (para tab Inventario) ────────────────────────────────────────
_BM_WH_URL    = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
_BM_AVAIL_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"
_BM_LOC_IDS   = "47,62,68"
_bm_amz_cache: dict[str, tuple[float, dict]] = {}
_BM_AMZ_TTL   = 900   # 15 min
_bm_all_refreshing:   set   = set()  # "bm_all" cuando BG pre-fetch activo
_bm_all_last_refresh: float = 0.0    # timestamp del último BG refresh completo

# ─── FLX Stock real-time (FBA Inventory API — query por SKU específico) ──────
# El scan general de FBA no devuelve items Seller Flex; la query por sellerSkus sí.
_flx_stock_cache: dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, {sku: data})}
_FLX_STOCK_TTL     = 120  # 2 min — datos en tiempo real (inventario cambia con órdenes)
_flx_stock_refreshing: set = set()  # seller_ids con refresh BG activo (evita doble tarea)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_price(offers: list) -> float:
    """Extrae el precio de venta de la lista de offers de un listing."""
    for offer in (offers or []):
        if offer.get("offerType") == "B2C":
            price = offer.get("price", {})
            try:
                return float(price.get("amount") or price.get("listingPrice", {}).get("amount", 0) or 0)
            except (TypeError, ValueError):
                pass
    return 0.0


def _parse_deal_info(offers: list, attributes: dict = None) -> dict:
    """
    Detecta si hay una sale/deal activa en el listing.

    Estrategia (en orden de prioridad):
    1. attributes.purchasable_offer[0].discounted_price vs our_price
       (es el campo más confiable cuando hay un sale activo en Seller Central)
    2. offers.price.landedPrice vs listingPrice
       (landedPrice refleja el precio real con promociones)
    3. offers.price.amount vs listingPrice
       (fallback genérico)
    """
    # ── 1. Attributes: purchasable_offer.discounted_price ─────────────────────
    if attributes:
        po_list = attributes.get("purchasable_offer", [])
        if isinstance(po_list, list) and po_list:
            po = po_list[0]
            try:
                our_sched = (po.get("our_price") or [{}])[0].get("schedule") or [{}]
                our_price = float((our_sched[0] if our_sched else {}).get("value_with_tax") or 0)
                disc_sched = (po.get("discounted_price") or [{}])[0].get("schedule") or [{}]
                disc_price = float((disc_sched[0] if disc_sched else {}).get("value_with_tax") or 0)
                if our_price > 0 and disc_price > 0 and our_price > disc_price * 1.01:
                    pct = round((1 - disc_price / our_price) * 100)
                    return {"is_deal": True, "deal_price": disc_price, "list_price": our_price, "deal_pct": pct}
            except (TypeError, ValueError, IndexError):
                pass

    # ── 2. Offers: landedPrice vs listingPrice ────────────────────────────────
    for offer in (offers or []):
        if offer.get("offerType") == "B2C":
            price = offer.get("price", {})
            try:
                list_price = float((price.get("listingPrice") or {}).get("amount") or 0)
                landed = float((price.get("landedPrice") or {}).get("amount") or 0)
                amount = float(price.get("amount") or 0)
                if list_price > 0 and landed > 0 and list_price > landed * 1.01:
                    pct = round((1 - landed / list_price) * 100)
                    return {"is_deal": True, "deal_price": landed, "list_price": list_price, "deal_pct": pct}
                if amount > 0 and list_price > 0 and list_price > amount * 1.01:
                    pct = round((1 - amount / list_price) * 100)
                    return {"is_deal": True, "deal_price": amount, "list_price": list_price, "deal_pct": pct}
            except (TypeError, ValueError):
                pass

    return {"is_deal": False, "deal_price": 0.0, "list_price": 0.0, "deal_pct": 0}


def _parse_fba_stock(fulfillment_avail: list) -> int:
    """Extrae el stock MFN/FBA del fulfillmentAvailability de un listing."""
    for fa in (fulfillment_avail or []):
        return int(fa.get("quantity") or 0)
    return 0


def _listing_status(summaries: list) -> str:
    """
    Determina el estado visible de un listing.
    BUYABLE     = activo y vendible (verde)
    DISCOVERABLE = visible pero no se puede comprar (amarillo)
    SUPPRESSED  = suprimido por Amazon (rojo)
    INACTIVE    = otro estado (gris)
    """
    for s in (summaries or []):
        statuses = s.get("status", [])
        if "BUYABLE" in statuses:
            return "ACTIVE"
        if "DISCOVERABLE" in statuses:
            return "DISCOVERABLE"
    return "INACTIVE"


def _build_fba_index(fba_summaries: list) -> dict:
    """
    Convierte la lista de FBA inventory en un dict indexado por sellerSku.
    Facilita el cruce rápido con los listings.
    """
    index = {}
    for s in fba_summaries:
        sku = s.get("sellerSku", "")
        if sku:
            index[sku] = s
    return index


async def _get_listings_cached(client) -> list:
    """Obtiene listings del caché o los descarga si expiró."""
    now = _time.time()
    key = client.seller_id
    if key in _listings_cache:
        ts, data = _listings_cache[key]
        if now - ts < _LISTINGS_TTL:
            return data
    data = await client.get_all_listings()
    _listings_cache[key] = (now, data)
    return data


async def _get_fba_cached(client) -> list:
    """Obtiene FBA inventory del caché o lo descarga si expiró."""
    now = _time.time()
    key = client.seller_id
    if key in _fba_cache:
        ts, data = _fba_cache[key]
        if now - ts < _FBA_TTL:
            return data
    data = await client.get_fba_inventory_all()
    _fba_cache[key] = (now, data)
    return data


async def _refresh_flx_stock_bg(client, flx_skus: list) -> None:
    """
    Tarea BG: descarga FLX stock de FBA API y actualiza caché.
    Nunca bloquea requests — se lanza con asyncio.create_task().
    """
    key = client.seller_id
    try:
        result: dict = {}
        unique_skus = list(dict.fromkeys(s for s in flx_skus if s))
        for i in range(0, len(unique_skus), 50):
            batch = unique_skus[i:i + 50]
            params = [
                ("granularityType", "Marketplace"),
                ("granularityId",   client.marketplace_id),
                ("marketplaceIds",  client.marketplace_id),
                ("details",         "true"),
            ]
            for sku in batch:
                params.append(("sellerSkus", sku))
            try:
                data = await client._request("GET", "/fba/inventory/v1/summaries", params=params)
                for s in (data.get("payload", {}).get("inventorySummaries") or []):
                    sku  = s.get("sellerSku", "")
                    det  = s.get("inventoryDetails", {}) or {}
                    res  = det.get("reservedQuantity", {}) or {}
                    result[sku] = {
                        "fulfillable": int(det.get("fulfillableQuantity") or 0),
                        "reserved":    int(res.get("totalReservedQuantity") or 0),
                        "inbound":     (
                            int(det.get("inboundWorkingQuantity") or 0)
                            + int(det.get("inboundShippedQuantity") or 0)
                            + int(det.get("inboundReceivingQuantity") or 0)
                        ),
                        "total":       int(s.get("totalQuantity") or 0),
                    }
            except Exception as exc:
                logger.warning(f"[FLX-BG] Error lote {i}: {exc}")
            if i + 50 < len(unique_skus):
                await asyncio.sleep(0.3)
        logger.info(f"[FLX-BG] {len(result)}/{len(unique_skus)} FLX SKUs actualizados en background")
        _flx_stock_cache[key] = (_time.time(), result)
    finally:
        _flx_stock_refreshing.discard(key)


def _get_flx_stock_cached(client, flx_skus: list) -> dict:
    """
    Stale-while-revalidate — NUNCA bloquea el request.

    • Cache fresco  → retorna datos inmediatamente.
    • Cache stale   → retorna datos stale + lanza refresh BG.
    • Sin cache     → retorna {} + lanza refresh BG (FLX muestra ··· en primera carga).

    El BG task (_refresh_flx_stock_bg) actualiza _flx_stock_cache cuando termina.
    """
    now = _time.time()
    key = client.seller_id
    cached = _flx_stock_cache.get(key)
    if cached:
        ts, data = cached
        if (now - ts) >= _FLX_STOCK_TTL and key not in _flx_stock_refreshing:
            _flx_stock_refreshing.add(key)
            asyncio.create_task(_refresh_flx_stock_bg(client, flx_skus))
            logger.info(f"[FLX] Cache stale ({int(now - ts)}s) — refresh BG iniciado")
        return data  # siempre retorna inmediatamente (fresco o stale)

    # Primera carga: sin caché todavía
    if key not in _flx_stock_refreshing:
        _flx_stock_refreshing.add(key)
        asyncio.create_task(_refresh_flx_stock_bg(client, flx_skus))
        logger.info(f"[FLX] Primera carga — refresh BG iniciado ({len(flx_skus)} FLX SKUs)")
    return {}


async def _get_onsite_stock_cached(client) -> dict:
    """
    Obtiene el stock de Amazon Onsite (Seller Flex) desde el Reports API.
    El reporte GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA incluye afn-fulfillable-quantity
    para todos los SKUs FBA, incluyendo los de Seller Flex en bodega propia.

    Caché de 30 min — generar el reporte tarda 30-90 seg.
    Retorna {sku: afn_fulfillable_quantity}.
    """
    now = _time.time()
    key = client.seller_id
    if key in _onsite_stock_cache:
        ts, data = _onsite_stock_cache[key]
        if now - ts < _ONSITE_STOCK_TTL:
            return data
    if key not in _onsite_stock_locks:
        _onsite_stock_locks[key] = asyncio.Lock()
    async with _onsite_stock_locks[key]:
        # Double-check bajo el lock
        if key in _onsite_stock_cache:
            ts, data = _onsite_stock_cache[key]
            if now - ts < _ONSITE_STOCK_TTL:
                return data
        try:
            data = await client.get_onsite_inventory_report()
        except Exception as e:
            logger.warning(f"[Onsite Stock] Error obteniendo reporte: {e}")
            data = {}
        _onsite_stock_cache[key] = (_time.time(), data)
        return data


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1: RESUMEN
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/summary", response_class=HTMLResponse)
async def amazon_products_summary(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to:   str = Query("", description="YYYY-MM-DD"),
):
    """
    Resumen del catálogo Amazon:
    - KPIs: listings activos, suprimidos, FBA units, revenue 30d
    - Top 10 listings por stock FBA
    - Distribución de estados (activos/inactivos/suprimidos)
    - Alertas críticas (sin stock, suprimidos, sin Buy Box estimado)
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_summary.html")

    try:
        # Cargar listings + FBA inventory en paralelo
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )

        fba_index = _build_fba_index(fba_summaries)

        # ── Calcular KPIs ──────────────────────────────────────────────────
        total_listings  = len(listings)
        active_count    = 0
        inactive_count  = 0
        suppressed_count = 0
        total_fba_units  = 0
        total_fba_value  = 0.0
        low_stock_count  = 0   # < 5 units
        no_stock_count   = 0   # 0 units en FBA

        enriched = []
        for item in listings:
            sku        = item.get("sku", "")
            summaries  = item.get("summaries", [{}])
            offers     = item.get("offers", [])
            fa         = item.get("fulfillmentAvailability", [])
            issues     = item.get("issues", [])

            status = _listing_status(summaries)
            price  = _parse_price(offers)

            # Stock: primero desde el listing, luego desde FBA inventory
            listing_stock = _parse_fba_stock(fa)
            fba_data      = fba_index.get(sku, {})
            fba_details   = fba_data.get("inventoryDetails", {})
            fba_stock     = fba_data.get("inventoryDetails", {}).get("fulfillableQuantity", listing_stock)
            unfulfillable = (fba_details.get("unfulfillableQuantity") or {}).get("totalUnfulfillableQuantity", 0)
            inbound       = (fba_details.get("inboundWorkingQuantity") or 0) + (fba_details.get("inboundShippedQuantity") or 0)

            # Metadata del listing
            summary_0 = summaries[0] if summaries else {}
            title     = summary_0.get("itemName", sku)
            asin      = fba_data.get("asin") or (summary_0.get("asin") or "")
            image_url = (summary_0.get("mainImage") or {}).get("link", "")

            if status == "ACTIVE":
                active_count += 1
            elif status in ("INACTIVE", "DISCOVERABLE"):
                inactive_count += 1
            else:
                suppressed_count += 1

            total_fba_units += fba_stock
            total_fba_value += fba_stock * price

            if fba_stock == 0:
                no_stock_count += 1
            elif fba_stock < 5:
                low_stock_count += 1

            enriched.append({
                "sku":          sku,
                "asin":         asin,
                "title":        title[:80],
                "price":        price,
                "status":       status,
                "fba_stock":    fba_stock,
                "unfulfillable": unfulfillable,
                "inbound":      inbound,
                "image_url":    image_url,
                "issues_count": len(issues),
            })

        # Ordenar por stock FBA descendente para el top
        enriched.sort(key=lambda x: x["fba_stock"], reverse=True)
        top_listings = enriched[:10]

        # Alertas críticas
        alerts = []
        if no_stock_count > 0:
            alerts.append({
                "type": "danger",
                "icon": "⚠️",
                "msg": f"{no_stock_count} listings sin stock en FBA — revisa reabastecimiento",
                "tab": "inventory",
            })
        if suppressed_count > 0:
            alerts.append({
                "type": "warning",
                "icon": "🚫",
                "msg": f"{suppressed_count} listings suprimidos por Amazon — requieren atención",
                "tab": "catalog",
            })
        items_with_issues = sum(1 for e in enriched if e["issues_count"] > 0)
        if items_with_issues > 0:
            alerts.append({
                "type": "info",
                "icon": "📋",
                "msg": f"{items_with_issues} listings con issues reportados por Amazon",
                "tab": "catalog",
            })

        ctx = {
            "request":         request,
            "nickname":        client.nickname,
            "marketplace":     client.marketplace_name,
            # KPIs
            "total_listings":  total_listings,
            "active_count":    active_count,
            "inactive_count":  inactive_count,
            "suppressed_count": suppressed_count,
            "total_fba_units": total_fba_units,
            "total_fba_value": round(total_fba_value, 2),
            "low_stock_count": low_stock_count,
            "no_stock_count":  no_stock_count,
            # Listas
            "top_listings":    top_listings,
            "alerts":          alerts,
        }
        return _templates.TemplateResponse("partials/amazon_products_summary.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en summary")
        return _render_error(request, "amazon_products_summary.html", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2: CATÁLOGO
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/catalog", response_class=HTMLResponse)
async def amazon_products_catalog(
    request: Request,
    status_filter: str = Query("all", description="all | active | inactive | suppressed"),
    sort_by:       str = Query("fba_stock", description="fba_stock | price | title"),
    sort_dir:      str = Query("desc", description="asc | desc"),
):
    """
    Catálogo completo de listings Amazon.

    Combina:
    - Listings Items API: SKU, ASIN, título, precio, estado
    - FBA Inventory: stock disponible, reservado, dañado, en camino
    - Issues del listing: alertas de calidad por Amazon

    Columnas: Imagen · Título/SKU · ASIN · Precio · FBA Disp. · Reservado ·
              Entrante · Dañado · Estado · Issues · Acciones
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_catalog.html")

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        fba_index = _build_fba_index(fba_summaries)

        # ── Enriquecer cada listing con datos FBA ──────────────────────────
        enriched = []
        for item in listings:
            sku       = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            offers    = item.get("offers", [])
            fa        = item.get("fulfillmentAvailability", [])
            issues    = item.get("issues", [])

            status    = _listing_status(summaries)
            price     = _parse_price(offers)

            fba_data     = fba_index.get(sku, {})
            fba_details  = fba_data.get("inventoryDetails", {})
            fba_stock    = int(fba_details.get("fulfillableQuantity") or _parse_fba_stock(fa))
            reserved     = int((fba_details.get("reservedQuantity") or {}).get("pendingCustomerOrderQuantity") or 0)
            unfulfillable = int((fba_details.get("unfulfillableQuantity") or {}).get("totalUnfulfillableQuantity") or 0)
            inbound      = int((fba_details.get("inboundWorkingQuantity") or 0)) + int((fba_details.get("inboundShippedQuantity") or 0))

            summary_0 = summaries[0] if summaries else {}
            title     = summary_0.get("itemName", sku)
            asin      = fba_data.get("asin") or summary_0.get("asin") or ""
            image_url = (summary_0.get("mainImage") or {}).get("link", "")
            condition = summary_0.get("conditionType", "new_new")

            # Issues con mensajes legibles
            issue_list = [
                {
                    "severity": i.get("severity", "ERROR"),
                    "message":  i.get("message", "Issue desconocido"),
                    "code":     i.get("code", ""),
                }
                for i in issues
            ]

            # Sugerencia automática de mejora
            suggestion = _get_listing_suggestion(fba_stock, status, unfulfillable, issue_list)

            enriched.append({
                "sku":           sku,
                "asin":          asin,
                "title":         title[:90],
                "price":         price,
                "status":        status,
                "condition":     condition,
                "fba_stock":     fba_stock,
                "reserved":      reserved,
                "unfulfillable": unfulfillable,
                "inbound":       inbound,
                "image_url":     image_url,
                "issues":        issue_list,
                "suggestion":    suggestion,
                "amazon_url":    f"https://www.amazon.com.mx/dp/{asin}" if asin else "",
            })

        # ── Filtrar por estado ─────────────────────────────────────────────
        if status_filter == "active":
            enriched = [e for e in enriched if e["status"] == "ACTIVE"]
        elif status_filter == "inactive":
            enriched = [e for e in enriched if e["status"] in ("INACTIVE", "DISCOVERABLE")]
        elif status_filter == "suppressed":
            enriched = [e for e in enriched if e["status"] == "INACTIVE" and e["issues"]]

        # ── Ordenar ────────────────────────────────────────────────────────
        reverse = (sort_dir == "desc")
        if sort_by == "price":
            enriched.sort(key=lambda x: x["price"], reverse=reverse)
        elif sort_by == "title":
            enriched.sort(key=lambda x: x["title"].lower(), reverse=reverse)
        else:  # fba_stock (default)
            enriched.sort(key=lambda x: x["fba_stock"], reverse=reverse)

        ctx = {
            "request":       request,
            "listings":      enriched,
            "total":         len(enriched),
            "status_filter": status_filter,
            "sort_by":       sort_by,
            "sort_dir":      sort_dir,
            "nickname":      client.nickname,
            "marketplace":   client.marketplace_name,
            "seller_id":     client.seller_id,
        }
        return _templates.TemplateResponse("partials/amazon_products_catalog.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en catalog")
        return _render_error(request, "amazon_products_catalog.html", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3: FBA INVENTORY
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/inventory", response_class=HTMLResponse)
async def amazon_products_inventory(
    request: Request,
    filter_type: str = Query("all", description="all | low | damaged | inbound"),
):
    """
    Breakdown detallado del inventario FBA por SKU.

    Muestra el estado real de cada unidad en Amazon:
    - Fulfillable: disponible para vender
    - Reserved: en órdenes activas (ya vendido, no enviado aún)
    - Unfulfillable: dañado, defectuoso, expirado (no se puede vender)
    - Inbound: en camino a warehouse Amazon (aún no disponible)

    Alertas automáticas:
    - Stock bajo: < 5 unidades disponibles
    - Stock crítico: 0 unidades disponibles
    - Unidades dañadas: > 0 en unfulfillable
    - Sin inventario FBA: el listing existe pero no tiene stock en Amazon
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_inventory.html")

    try:
        fba_summaries = await _get_fba_cached(client)

        # ── Enriquecer y calcular KPIs ─────────────────────────────────────
        items = []
        total_fulfillable    = 0
        total_reserved       = 0
        total_unfulfillable  = 0
        total_inbound        = 0
        low_stock_skus       = 0
        damaged_skus         = 0
        zero_stock_skus      = 0

        for s in fba_summaries:
            sku     = s.get("sellerSku", "")
            asin    = s.get("asin", "")
            name    = s.get("productName", sku)[:80]
            details = s.get("inventoryDetails", {})

            fulfillable   = int(details.get("fulfillableQuantity") or 0)
            inbound_w     = int(details.get("inboundWorkingQuantity") or 0)
            inbound_s     = int(details.get("inboundShippedQuantity") or 0)
            inbound_r     = int(details.get("inboundReceivingQuantity") or 0)
            inbound       = inbound_w + inbound_s + inbound_r

            res_data      = details.get("reservedQuantity") or {}
            reserved      = int(res_data.get("totalReservedQuantity") or 0)
            pending_cust  = int(res_data.get("pendingCustomerOrderQuantity") or 0)

            unf_data      = details.get("unfulfillableQuantity") or {}
            unfulfillable = int(unf_data.get("totalUnfulfillableQuantity") or 0)
            damaged       = int(unf_data.get("customerDamagedQuantity") or 0) + \
                            int(unf_data.get("warehouseDamagedQuantity") or 0) + \
                            int(unf_data.get("defectiveQuantity") or 0)

            total_qty     = s.get("totalQuantity", fulfillable + reserved + unfulfillable)

            # Acumuladores globales
            total_fulfillable   += fulfillable
            total_reserved      += reserved
            total_unfulfillable += unfulfillable
            total_inbound       += inbound

            if fulfillable == 0:
                zero_stock_skus += 1
            elif fulfillable < 5:
                low_stock_skus += 1

            if unfulfillable > 0:
                damaged_skus += 1

            # Estado del SKU en FBA
            if fulfillable == 0 and inbound == 0:
                fba_status = "empty"
            elif fulfillable == 0 and inbound > 0:
                fba_status = "incoming"
            elif fulfillable < 5:
                fba_status = "low"
            else:
                fba_status = "ok"

            items.append({
                "sku":           sku,
                "asin":          asin,
                "name":          name,
                "fulfillable":   fulfillable,
                "reserved":      reserved,
                "pending_cust":  pending_cust,
                "unfulfillable": unfulfillable,
                "damaged":       damaged,
                "inbound":       inbound,
                "total_qty":     total_qty,
                "fba_status":    fba_status,
                "amazon_url":    f"https://www.amazon.com.mx/dp/{asin}" if asin else "",
                "last_updated":  s.get("lastUpdatedTime", "")[:10],
            })

        # ── Filtros ────────────────────────────────────────────────────────
        if filter_type == "low":
            items = [i for i in items if 0 < i["fulfillable"] < 5]
        elif filter_type == "damaged":
            items = [i for i in items if i["unfulfillable"] > 0]
        elif filter_type == "inbound":
            items = [i for i in items if i["inbound"] > 0]
        elif filter_type == "zero":
            items = [i for i in items if i["fulfillable"] == 0]

        # Ordenar por fulfillable asc para ver problemas primero
        items.sort(key=lambda x: (x["fulfillable"], -x["unfulfillable"]))

        ctx = {
            "request":              request,
            "items":                items,
            "total_items":          len(fba_summaries),
            "filter_type":          filter_type,
            # KPIs
            "total_fulfillable":    total_fulfillable,
            "total_reserved":       total_reserved,
            "total_unfulfillable":  total_unfulfillable,
            "total_inbound":        total_inbound,
            "low_stock_skus":       low_stock_skus,
            "damaged_skus":         damaged_skus,
            "zero_stock_skus":      zero_stock_skus,
            "nickname":             client.nickname,
            "marketplace":          client.marketplace_name,
        }
        return _templates.TemplateResponse("partials/amazon_products_inventory.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en FBA inventory")
        return _render_error(request, "amazon_products_inventory.html", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# TAB 4: BUY BOX
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/buybox", response_class=HTMLResponse)
async def amazon_products_buybox(request: Request):
    """
    Análisis del Buy Box para los top listings del vendedor.

    El Buy Box en Amazon es el botón "Añadir al carrito". Solo 1 seller
    lo tiene a la vez. Ganar el Buy Box = ~90% de las ventas del ASIN.

    Obtiene datos de Buy Box para los top 15 SKUs por stock FBA usando
    la Product Pricing API (rate-limited: 1 req/s).

    Métricas por listing:
    - ¿Tenemos el Buy Box?
    - Precio del Buy Box (si lo tiene otro)
    - Precio actual nuestro
    - Diferencia: cuánto bajar/subir para ganar el Buy Box
    - Número total de competidores

    Sugerencias de repricer:
    - Si el precio propio > Buy Box price: bajar X% para competir
    - Si somos el único seller: podemos subir precio sin perder ventas
    - Si tenemos FBA y competidor es MFN: ventaja, podemos cobrar más
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_buybox.html")

    try:
        # Obtenemos los listings con más stock (más relevantes para Buy Box)
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        fba_index = _build_fba_index(fba_summaries)

        # Seleccionar top 15 por stock FBA (los más importantes para el negocio)
        candidates = []
        for item in listings:
            sku      = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            offers    = item.get("offers", [])
            status    = _listing_status(summaries)
            if status != "ACTIVE":
                continue
            price     = _parse_price(offers)
            fba_data  = fba_index.get(sku, {})
            fba_stock = int((fba_data.get("inventoryDetails") or {}).get("fulfillableQuantity") or 0)
            summary_0 = summaries[0] if summaries else {}
            title     = summary_0.get("itemName", sku)[:70]
            asin      = fba_data.get("asin") or summary_0.get("asin") or ""
            candidates.append({
                "sku": sku, "asin": asin, "title": title,
                "our_price": price, "fba_stock": fba_stock,
            })

        candidates.sort(key=lambda x: x["fba_stock"], reverse=True)
        top_skus = candidates[:15]

        # ── Obtener Buy Box para cada SKU (rate-limited: 1 req/s) ─────────
        buybox_results = []
        now_ts = _time.time()

        for c in top_skus:
            sku = c["sku"]
            cache_key = f"{client.seller_id}:{sku}"

            # Revisar caché
            if cache_key in _buybox_cache:
                ts, cached = _buybox_cache[cache_key]
                if now_ts - ts < _BUYBOX_TTL:
                    buybox_results.append(cached)
                    continue

            # Fetch desde la API
            data = await client.get_listing_offers(sku)
            await asyncio.sleep(1.1)  # Rate limit: 1 req/s

            result = _parse_buybox_result(c, data)
            _buybox_cache[cache_key] = (_time.time(), result)
            buybox_results.append(result)

        # ── KPIs del Buy Box ───────────────────────────────────────────────
        bb_won   = sum(1 for r in buybox_results if r.get("bb_won"))
        bb_lost  = sum(1 for r in buybox_results if not r.get("bb_won") and r.get("bb_price"))
        solo     = sum(1 for r in buybox_results if r.get("competitors") == 0)
        total_opportunity = sum(
            max(0, r.get("our_price", 0) - r.get("bb_price", 0))
            for r in buybox_results if r.get("bb_price") and not r.get("bb_won")
        )

        ctx = {
            "request":             request,
            "buybox_results":      buybox_results,
            "bb_won":              bb_won,
            "bb_lost":             bb_lost,
            "solo":                solo,
            "total_analyzed":      len(buybox_results),
            "total_opportunity":   round(total_opportunity, 2),
            "nickname":            client.nickname,
            "marketplace":         client.marketplace_name,
        }
        return _templates.TemplateResponse("partials/amazon_products_buybox.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en buybox")
        return _render_error(request, "amazon_products_buybox.html", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE DE PRECIO (acción inline desde la tabla de catálogo)
# ─────────────────────────────────────────────────────────────────────────────

@router.put("/products/{sku}/price")
async def update_amazon_price(sku: str, request: Request):
    """
    Actualiza el precio de un listing Amazon via Listings Items API (PATCH).

    El sku es el SellerSKU exacto del listing.
    Body JSON: {"price": 12999.00}
    """
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon")

    body = await request.json()
    price = float(body.get("price", 0))
    if price <= 0:
        raise HTTPException(status_code=400, detail="Precio inválido")

    try:
        result = await client.update_listing_price(sku, price)
        # Invalidar caché de listings
        _listings_cache.pop(client.seller_id, None)
        _buybox_cache.pop(f"{client.seller_id}:{sku}", None)
        return {"ok": True, "sku": sku, "price": price, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# DETALLES DE LISTING (para modal de edición)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/{sku}/details")
async def amazon_product_details(sku: str, request: Request):
    """
    Retorna los campos editables de un listing para el modal de edición.
    Incluye: título, precio, stock FBM, ASIN, productType y fulfillment_type.
    """
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon")

    listing = await client.get_listing(sku)
    if not listing:
        raise HTTPException(status_code=404, detail="SKU no encontrado")

    summaries = listing.get("summaries", [{}])
    summary_0 = summaries[0] if summaries else {}
    attributes = listing.get("attributes", {})
    fa = listing.get("fulfillmentAvailability", [])

    # Título desde attributes → item_name, o fallback a summaries
    title = ""
    item_name_attr = attributes.get("item_name", [])
    if isinstance(item_name_attr, list) and item_name_attr:
        title = item_name_attr[0].get("value", "")
    if not title:
        title = summary_0.get("itemName", "")

    # Precio desde attributes → purchasable_offer
    price = 0.0
    po_list = attributes.get("purchasable_offer", [])
    if isinstance(po_list, list) and po_list:
        our_price = po_list[0].get("our_price", [])
        if isinstance(our_price, list) and our_price:
            schedule = our_price[0].get("schedule", [])
            if isinstance(schedule, list) and schedule:
                price = float(schedule[0].get("value_with_tax") or 0)

    # Stock y tipo de fulfillment
    qty = 0
    fulfillment_type = "FBA"
    for f in fa:
        channel = (f.get("fulfillmentChannelCode") or "").upper()
        if channel == "DEFAULT":
            qty = int(f.get("quantity") or 0)
            fulfillment_type = "FBM"

    asin = summary_0.get("asin", "")

    return {
        "sku": sku,
        "asin": asin,
        "title": title,
        "price": price,
        "qty": qty,
        "fulfillment_type": fulfillment_type,
        "product_type": listing.get("productType", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# EDICIÓN DE LISTING (precio + título + stock FBM)
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/products/{sku}")
async def update_amazon_listing(sku: str, request: Request):
    """
    Actualiza uno o más campos de un listing Amazon via Listings Items API PATCH.
    Body JSON: {"price": float, "title": str, "qty": int}  — todos opcionales.
    """
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon")

    body = await request.json()
    price = body.get("price")
    title = body.get("title")
    qty   = body.get("qty")

    if price is None and title is None and qty is None:
        raise HTTPException(status_code=400, detail="Sin campos para actualizar")

    results = {}
    errors = []

    if price is not None:
        try:
            await client.update_listing_price(sku, float(price))
            results["price"] = "ok"
        except Exception as e:
            errors.append(f"Precio: {e}")

    if title is not None and str(title).strip():
        try:
            await client.update_listing_title(sku, str(title).strip())
            results["title"] = "ok"
        except Exception as e:
            errors.append(f"Título: {e}")

    if qty is not None:
        try:
            await client.update_listing_quantity(sku, int(qty))
            results["qty"] = "ok"
        except Exception as e:
            errors.append(f"Cantidad: {e}")

    # Invalidar caché
    _listings_cache.pop(client.seller_id, None)
    _buybox_cache.pop(f"{client.seller_id}:{sku}", None)

    if errors and not results:
        raise HTTPException(status_code=500, detail=" | ".join(errors))

    return {"ok": not errors, "results": results, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS INTERNOS
# ─────────────────────────────────────────────────────────────────────────────

def _get_listing_suggestion(fba_stock: int, status: str, unfulfillable: int, issues: list) -> Optional[str]:
    """
    Genera una sugerencia de mejora para el listing basada en su estado.
    Retorna None si el listing está en buen estado.
    """
    if status != "ACTIVE" and issues:
        return f"Corregir {len(issues)} issue(s) para reactivar el listing"
    if status != "ACTIVE":
        return "Listing inactivo — revisar en Seller Central"
    if fba_stock == 0 and unfulfillable > 0:
        return f"Crear orden de remoción o reemplazo para {unfulfillable} und. dañadas"
    if fba_stock == 0:
        return "Sin stock en FBA — enviar inventario para reactivar ventas"
    if fba_stock < 5:
        return f"Stock crítico ({fba_stock} uds) — considera enviar más inventario"
    if unfulfillable > 0:
        return f"{unfulfillable} uds dañadas en Amazon — considera order de remoción"
    return None


def _parse_buybox_result(candidate: dict, api_data: Optional[dict]) -> dict:
    """
    Parsea la respuesta de getListingOffers para extraer info del Buy Box.
    Retorna un dict con: sku, title, our_price, bb_price, bb_won,
                          competitors, suggestion, fba_stock
    """
    result = {
        "sku":        candidate["sku"],
        "asin":       candidate["asin"],
        "title":      candidate["title"],
        "our_price":  candidate["our_price"],
        "fba_stock":  candidate["fba_stock"],
        "bb_price":   None,
        "bb_won":     False,
        "competitors": 0,
        "is_fba_dominant": False,
        "suggestion": None,
        "amazon_url": f"https://www.amazon.com.mx/dp/{candidate['asin']}" if candidate["asin"] else "",
    }

    if not api_data:
        result["suggestion"] = "No se pudo obtener info de Buy Box"
        return result

    payload = api_data.get("payload", {})
    summary = payload.get("Summary", {})

    # Precio del Buy Box
    bb_prices = summary.get("BuyBoxPrices", [])
    if bb_prices:
        bb_amount = bb_prices[0].get("LandedPrice", {}).get("Amount") or \
                    bb_prices[0].get("ListingPrice", {}).get("Amount")
        result["bb_price"] = float(bb_amount) if bb_amount else None

    # Número de competidores
    result["competitors"] = summary.get("TotalOfferCount", 0)

    # ¿Tenemos nosotros el Buy Box?
    offers = payload.get("Offers", [])
    for offer in offers:
        if offer.get("IsBuyBoxWinner"):
            result["bb_won"] = True
        if offer.get("IsFulfilledByAmazon") and offer.get("IsBuyBoxWinner"):
            result["is_fba_dominant"] = True

    # Generar sugerencia de repricing
    our = result["our_price"]
    bb  = result["bb_price"]
    comps = result["competitors"]

    if result["bb_won"]:
        if comps == 0:
            result["suggestion"] = f"Eres el único vendedor — considera subir precio para maximizar margen"
        else:
            result["suggestion"] = f"✅ Tienes el Buy Box ({comps} competidor{'es' if comps!=1 else ''})"
    elif bb and our and our > bb:
        diff = our - bb
        pct  = diff / our * 100
        result["suggestion"] = f"Bajar ${diff:,.0f} ({pct:.1f}%) para alcanzar el Buy Box (actualmente en ${bb:,.0f})"
    elif bb and our and our < bb * 0.9:
        result["suggestion"] = f"Precio muy bajo vs Buy Box (${bb:,.0f}) — puedes subir y mantener ventaja"
    elif not bb:
        result["suggestion"] = "No hay Buy Box activo — primera en ganerlo"
    else:
        result["suggestion"] = "Competencia en Buy Box detectada"

    return result


async def _refresh_sku_sales_bg(client) -> None:
    """
    Tarea BG: descarga ventas 30d (Orders + Items) y actualiza caché.
    Nunca bloquea requests — se lanza con asyncio.create_task().
    Sin timeout — puede procesar todas las órdenes sin prisa.
    """
    key = client.seller_id
    try:
        created_after = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            orders = await client.get_orders(created_after)
        except Exception as e:
            logger.warning(f"[SKU-Sales-BG] Error obteniendo órdenes: {e}")
            return

        valid_orders = [
            o for o in orders
            if o.get("OrderStatus") not in ("Cancelled", "Pending")
        ]
        logger.info(f"[SKU-Sales-BG] {len(valid_orders)} órdenes a procesar para ventas 30d")

        sku_data: dict = {}

        async def _fetch_items(order):
            order_id = order.get("AmazonOrderId", "")
            if not order_id:
                return
            try:
                items = await client.get_order_items(order_id)
                for item in items:
                    sku = item.get("SellerSKU", "").strip()
                    qty = int(item.get("QuantityOrdered", 0))
                    try:
                        price = float((item.get("ItemPrice") or {}).get("Amount", 0) or 0)
                    except (TypeError, ValueError):
                        price = 0.0
                    if sku and qty > 0:
                        if sku not in sku_data:
                            sku_data[sku] = {"units": 0, "revenue": 0.0}
                        sku_data[sku]["units"] += qty
                        sku_data[sku]["revenue"] += price
            except Exception as e:
                logger.debug(f"[SKU-Sales-BG] Error en orden {order_id}: {e}")

        batch_size = 5
        for i in range(0, len(valid_orders), batch_size):
            batch = valid_orders[i:i + batch_size]
            await asyncio.gather(*[_fetch_items(o) for o in batch])
            if i + batch_size < len(valid_orders):
                await asyncio.sleep(0.5)

        logger.info(f"[SKU-Sales-BG] Listo — {len(sku_data)} SKUs con ventas en 30d")
        _sku_sales_cache[key] = (_time.time(), sku_data)
    finally:
        _sku_sales_refreshing.discard(key)


def _get_sku_sales_cached(client) -> tuple[dict, bool]:
    """
    Stale-while-revalidate — NUNCA bloquea el request.

    Retorna (sku_data, loading):
    • Cache fresco  → (datos, False)
    • Cache stale   → (datos stale, True)  + lanza refresh BG
    • Sin cache     → ({}, True)           + lanza refresh BG

    El BG task actualiza _sku_sales_cache cuando termina.
    """
    now = _time.time()
    key = client.seller_id
    cached = _sku_sales_cache.get(key)
    if cached:
        ts, data = cached
        if (now - ts) >= _SKU_SALES_TTL and key not in _sku_sales_refreshing:
            _sku_sales_refreshing.add(key)
            asyncio.create_task(_refresh_sku_sales_bg(client))
            logger.info(f"[SKU-Sales] Cache stale ({int(now - ts)}s) — refresh BG iniciado")
        loading = key in _sku_sales_refreshing
        return data, loading  # siempre retorna inmediatamente

    # Sin cache — lanzar BG y retornar vacío
    if key not in _sku_sales_refreshing:
        _sku_sales_refreshing.add(key)
        asyncio.create_task(_refresh_sku_sales_bg(client))
        logger.info("[SKU-Sales] Primera carga — refresh BG iniciado")
    return {}, True


# ─────────────────────────────────────────────────────────────────────────────
# BINMANAGER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _amz_base_sku(sku: str) -> str:
    """
    Extrae el SKU base de Amazon para consultar BinManager.

    Patrón: los SKUs de Amazon tienen la forma BASE-SUFIJO donde BASE son los
    primeros 10 caracteres (ej: SNFN000941-FLX01 → SNFN000941).
    Se corta en el PRIMER guion para obtener el SKU base de BinManager.

    También limpia sufijos MeLi-style (/, +, paréntesis) por compatibilidad.
    """
    if not sku:
        return ""
    # Tomar primera parte antes de " / " o " + " (packs multi-SKU)
    s = _re.split(r'\s*[/+]\s*', sku)[0].strip()
    # Quitar sufijos de cantidad entre paréntesis: (2), (18), etc.
    s = _re.sub(r'\(\d+\)', '', s).strip()
    s = _re.sub(r'[()]', '', s).strip()
    # Cortar en el PRIMER guion: SNFN000941-FLX01 → SNFN000941
    if '-' in s:
        s = s.split('-', 1)[0]
    return s




def _parse_wh_rows_amz(rows: list) -> tuple:
    """Parsea filas del Warehouse endpoint. Retorna (mty, cdmx, tj)."""
    mty = cdmx = tj = 0
    for row in (rows or []):
        qty = row.get("QtyTotal", 0) or 0
        wname = (row.get("WarehouseName") or "").lower()
        if "monterrey" in wname or "maxx" in wname:
            mty += qty
        elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
            cdmx += qty
        else:
            tj += qty
    return mty, cdmx, tj


_BM_EMPTY = {"bm_mty": 0, "bm_cdmx": 0, "bm_tj": 0, "bm_avail": 0, "bm_reserved": 0}


def _bm_from_cache(sku: str) -> dict:
    """Lee BM data del caché sin hacer API calls. Retorna _BM_EMPTY si no está cacheado."""
    cached = _bm_amz_cache.get(sku.upper())
    if cached and (_time.time() - cached[0]) < _BM_AMZ_TTL:
        return cached[1]
    return _BM_EMPTY


async def _enrich_bm_amz(items: list) -> None:
    """
    Enriquece items in-place con datos BinManager (bm_mty, bm_cdmx, bm_tj, bm_avail, bm_reserved).

    - Condiciones siempre: GRA,GRB,GRC,NEW (aplica para todos los SKUs Amazon).
    - Deduplica por SKU base: SNFN000941-NEW-02 y SNFN000941-FLX01 → 1 sola llamada BM.
    - Caché 15 min por Amazon SKU.
    - Logging completo para diagnóstico en Railway.
    """
    if not items:
        return

    _BM_COND = "GRA,GRB,GRC,NEW"
    now = _time.time()

    # 1. Mapear Amazon SKU → lista de items (varios items pueden tener el mismo SKU)
    sku_to_items: dict[str, list] = {}
    for item in items:
        sku = item.get("sku", "")
        if not sku:
            item.update(_BM_EMPTY)
            continue
        sku_to_items.setdefault(sku, []).append(item)

    # 2. Revisar caché; agrupar los no cacheados por base_sku (deduplicar llamadas BM)
    base_to_amz_skus: dict[str, list[str]] = {}
    for sku, item_list in sku_to_items.items():
        cached = _bm_amz_cache.get(sku.upper())
        if cached and (now - cached[0]) < _BM_AMZ_TTL:
            for item in item_list:
                item.update(cached[1])
        else:
            base = _amz_base_sku(sku)
            if not base:
                for item in item_list:
                    item.update(_BM_EMPTY)
                continue
            base_to_amz_skus.setdefault(base, []).append(sku)
            for item in item_list:
                item.update(_BM_EMPTY)   # placeholder hasta que llegue BM

    if not base_to_amz_skus:
        return

    logger.info(f"[BM-AMZ] Consultando {len(base_to_amz_skus)} SKUs base: {list(base_to_amz_skus)}")
    sem = asyncio.Semaphore(15)

    async def _fetch_base(base: str, amz_skus: list[str], http: httpx.AsyncClient) -> None:
        wh_payload = {
            "COMPANYID": 1, "SKU": base, "WarehouseID": None,
            "LocationID": _BM_LOC_IDS, "BINID": None,
            "Condition": _BM_COND, "SUPPLIERS": None, "ForInventory": 0,
        }
        av_payload = {
            "COMPANYID": 1, "TYPEINVENTORY": 0, "WAREHOUSEID": None,
            "LOCATIONID": _BM_LOC_IDS, "BINID": None,
            "PRODUCTSKU": base, "CONDITION": _BM_COND,
            "SUPPLIERS": None, "LCN": None, "SEARCH": base,
        }
        wh_rows: list = []
        av_rows: list = []
        async with sem:
            try:
                r_wh, r_av = await asyncio.gather(
                    http.post(_BM_WH_URL,    json=wh_payload, timeout=15.0),
                    http.post(_BM_AVAIL_URL, json=av_payload, timeout=15.0),
                    return_exceptions=True,
                )
                if not isinstance(r_wh, Exception):
                    if r_wh.status_code == 200:
                        wh_rows = r_wh.json()
                    else:
                        logger.warning(f"[BM-AMZ] WH HTTP {r_wh.status_code} para base={base}")
                else:
                    logger.warning(f"[BM-AMZ] WH excepcion para base={base}: {r_wh}")
                if not isinstance(r_av, Exception):
                    if r_av.status_code == 200:
                        av_rows = r_av.json()
                    else:
                        logger.warning(f"[BM-AMZ] AV HTTP {r_av.status_code} para base={base}")
                else:
                    logger.warning(f"[BM-AMZ] AV excepcion para base={base}: {r_av}")
            except Exception as exc:
                logger.warning(f"[BM-AMZ] Error al conectar BM para base={base}: {exc}")

        mty, cdmx, tj = _parse_wh_rows_amz(wh_rows)
        avail    = sum(row.get("Available", 0) or 0 for row in av_rows)
        reserved = sum(row.get("Required",  0) or 0 for row in av_rows)
        inv = {"bm_mty": mty, "bm_cdmx": cdmx, "bm_tj": tj,
               "bm_avail": avail, "bm_reserved": reserved}
        logger.info(
            f"[BM-AMZ] {base} => mty={mty} cdmx={cdmx} tj={tj} "
            f"avail={avail} res={reserved}  (SKUs: {amz_skus})"
        )
        # Cachear y aplicar resultado a TODOS los Amazon SKUs que comparten este base
        ts_now = _time.time()
        for amz_sku in amz_skus:
            _bm_amz_cache[amz_sku.upper()] = (ts_now, inv)
            for item in sku_to_items.get(amz_sku, []):
                item.update(inv)

    async with httpx.AsyncClient(timeout=30.0) as http:
        await asyncio.gather(
            *[_fetch_base(base, skus, http) for base, skus in base_to_amz_skus.items()],
            return_exceptions=True,
        )


async def _refresh_bm_all_bg(listings: list) -> None:
    """
    Tarea BG: pre-calienta caché BM para todos los listings.
    Permite filtrar/ordenar por BM stock en toda la tabla (no solo la página actual).
    """
    global _bm_all_last_refresh
    try:
        # Items dummy (solo "sku") para triggerar el caché sin tocar datos reales
        items_for_bm = [{"sku": item.get("sku", "")} for item in listings if item.get("sku")]
        total = len(items_for_bm)
        for i in range(0, total, 50):
            chunk = items_for_bm[i:i + 50]
            await _enrich_bm_amz(chunk)
            if i + 50 < total:
                await asyncio.sleep(1.0)  # pace suave para no saturar BM API en BG
        logger.info(f"[BM-ALL-BG] Pre-fetch completo: {total} SKUs en caché BM")
        _bm_all_last_refresh = _time.time()
    finally:
        _bm_all_refreshing.discard("bm_all")


def _trigger_bm_prefetch(listings: list) -> None:
    """Lanza BG pre-fetch de BM si el caché global está frío o stale."""
    now = _time.time()
    if "bm_all" not in _bm_all_refreshing and (now - _bm_all_last_refresh) > _BM_AMZ_TTL:
        _bm_all_refreshing.add("bm_all")
        asyncio.create_task(_refresh_bm_all_bg(listings))
        logger.info(f"[BM-ALL] Pre-fetch BG iniciado ({len(listings)} listings)")


# ─────────────────────────────────────────────────────────────────────────────
# NUEVOS TABS (Centro de Productos v2) — Espejo de MeLi
# Los endpoints antiguos (summary, catalog, inventory, buybox) se conservan
# porque amazon_dashboard.html los sigue usando.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/resumen", response_class=HTMLResponse)
async def amazon_products_resumen(request: Request):
    """
    Resumen del catálogo Amazon v2 — con revenue 30d (Sales API), top 5 por unidades
    y acciones rápidas hacia las demás secciones.
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_resumen.html")

    try:
        now = datetime.utcnow()
        date_from_30d = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        sku_sales, _sku_loading_resumen = _get_sku_sales_cached(client)  # sync, nunca bloquea

        # Revenue y unidades del Sales API (OPS exacto — igual a Seller Central)
        try:
            metrics_data = await _get_cached_order_metrics(client, date_from_30d, date_to)
            revenue_30d = sum(
                float((m.get("totalSales") or {}).get("amount", 0) or 0)
                for m in metrics_data
            )
            units_30d_api = sum(int(m.get("unitCount", 0) or 0) for m in metrics_data)
        except Exception as e:
            logger.warning(f"[Amazon Resumen] Error en Sales API: {e}")
            revenue_30d = 0.0
            units_30d_api = sum(v["units"] for v in sku_sales.values())

        fba_index = _build_fba_index(fba_summaries)

        active_count = inactive_count = suppressed_count = 0
        for item in listings:
            status = _listing_status(item.get("summaries", []))
            if status == "ACTIVE":
                active_count += 1
            elif status in ("INACTIVE", "DISCOVERABLE"):
                inactive_count += 1
            else:
                suppressed_count += 1

        units_30d = units_30d_api or sum(v["units"] for v in sku_sales.values())
        unique_skus_sold = len(sku_sales)
        avg_ticket = revenue_30d / units_30d if units_30d > 0 else 0

        # Top 5 por unidades (30d) — enriquecidos con título del listing
        listings_by_sku = {item.get("sku", ""): item for item in listings}
        top_5_raw = sorted(sku_sales.items(), key=lambda x: x[1]["units"], reverse=True)[:5]
        top_5 = []
        for sku, data in top_5_raw:
            item = listings_by_sku.get(sku, {})
            summaries = item.get("summaries", [{}])
            summary_0 = summaries[0] if summaries else {}
            fba_d = fba_index.get(sku, {})
            asin = fba_d.get("asin") or summary_0.get("asin") or ""
            top_5.append({
                "sku": sku,
                "title": summary_0.get("itemName", sku)[:65],
                "units": data["units"],
                "revenue": round(data["revenue"], 2),
                "asin": asin,
                "amazon_url": f"https://www.amazon.com.mx/dp/{asin}" if asin else "",
            })

        # Acciones rápidas — contadores de urgencia
        no_stock_count = sum(
            1 for s in fba_summaries
            if int((s.get("inventoryDetails") or {}).get("fulfillableQuantity") or 0) == 0
            and sku_sales.get(s.get("sellerSku", ""), {}).get("units", 0) > 0
        )
        low_stock_count = sum(
            1 for s in fba_summaries
            if 0 < int((s.get("inventoryDetails") or {}).get("fulfillableQuantity") or 0) < 10
            and sku_sales.get(s.get("sellerSku", ""), {}).get("units", 0) > 0
        )
        sin_publicar_count = inactive_count + suppressed_count

        ctx = {
            "request": request,
            "nickname": client.nickname,
            "marketplace": client.marketplace_name,
            "revenue_30d": round(revenue_30d, 2),
            "units_30d": units_30d,
            "active_count": active_count,
            "inactive_count": inactive_count,
            "suppressed_count": suppressed_count,
            "total_listings": len(listings),
            "unique_skus_sold": unique_skus_sold,
            "avg_ticket": round(avg_ticket, 2),
            "top_5": top_5,
            "no_stock_count": no_stock_count,
            "low_stock_count": low_stock_count,
            "sin_publicar_count": sin_publicar_count,
            "date_from": date_from_30d,
            "date_to": date_to,
            "sku_sales_loading": _sku_loading_resumen,  # True = BG refresh activo
        }
        return _templates.TemplateResponse("partials/amazon_products_resumen.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en resumen v2")
        return _render_error(request, "amazon_products_resumen.html", str(e))


@router.get("/products/inventario", response_class=HTMLResponse)
async def amazon_products_inventario(
    request: Request,
    sort:     str  = Query("units", description="units|flx|stock|fbm|revenue|price|bm|mty|cdmx|supply"),
    sort_dir: str  = Query("desc",  description="asc|desc"),
    filter:   str  = Query("all",   description="all|fba|top|low|nostock"),
    q:        str  = Query("",      description="Búsqueda por SKU, ASIN o título"),
    page:     int  = Query(1,       description="Página actual"),
    per_page: int  = Query(20,      description="Items por página"),
    force:    bool = Query(False,   description="True = ignorar caché y forzar fetch fresco"),
):
    """
    Inventario completo con ventas 30d, días supply y stock BinManager por SKU.
    Filtros: Todos / FBA / Top Ventas / Baja Venta / Sin Stock
    Paginación: 20/pág (server-side). BM enriquece solo la página actual.
    force=True limpia el caché de listings y FBA antes de cargar.
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_inventario.html")

    if force:
        _listings_cache.pop(client.seller_id, None)
        _fba_cache.pop(client.seller_id, None)
        _flx_stock_cache.pop(client.seller_id, None)

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        sku_sales, sku_sales_loading = _get_sku_sales_cached(client)  # sync, nunca bloquea
        fba_index = _build_fba_index(fba_summaries)

        # FLX real-time: stale-while-revalidate — retorna caché inmediatamente (o {}) + BG refresh
        flx_skus = [item.get("sku", "") for item in listings
                    if "-FLX" in (item.get("sku", "") or "").upper()]
        flx_stock_index = _get_flx_stock_cached(client, flx_skus)   # sync, nunca bloquea
        flx_loading     = client.seller_id in _flx_stock_refreshing  # True = BG activo

        # BM pre-fetch BG: calienta caché BM para todos los SKUs (permite filtrar/ordenar por BM)
        _trigger_bm_prefetch(listings)
        bm_loading = "bm_all" in _bm_all_refreshing

        enriched = []
        for item in listings:
            sku = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            offers = item.get("offers", [])

            status = _listing_status(summaries)
            price = _parse_price(offers)
            attributes = item.get("attributes") or {}
            deal = _parse_deal_info(offers, attributes)
            summary_0 = summaries[0] if summaries else {}
            fba_d = fba_index.get(sku, {})
            asin = fba_d.get("asin") or summary_0.get("asin") or ""
            fba_details = fba_d.get("inventoryDetails", {})

            # Stock FBA (Fulfilled by Amazon) — de la FBA Inventory API
            fba_stock_fba = int(fba_details.get("fulfillableQuantity") or 0)
            fba_reserved  = int((fba_details.get("reservedQuantity") or {}).get("pendingCustomerOrderQuantity") or 0)
            inbound = (
                int(fba_details.get("inboundWorkingQuantity") or 0)
                + int(fba_details.get("inboundShippedQuantity") or 0)
            )

            # ── Stock por canal de fulfillment ─────────────────────────────
            listing_fa = item.get("fulfillmentAvailability", [])
            stock_fba  = fba_stock_fba   # de la FBA Inventory API
            stock_fbm  = 0               # canal DEFAULT (merchant fulfilled)
            stock_flx  = 0               # Seller Flex / Amazon Onsite

            for fa_entry in listing_fa:
                channel = (fa_entry.get("fulfillmentChannelCode") or "").upper()
                qty     = int(fa_entry.get("quantity") or 0)
                if channel == "DEFAULT":
                    stock_fbm = qty

            # Seller Flex: stock real de FBA API (query por SKU específico)
            # fulfillableQuantity coincide exactamente con Seller Central.
            flx_reserved = 0
            flx_inbound  = 0
            if "-FLX" in sku.upper():
                flx_data     = flx_stock_index.get(sku, {})
                stock_flx    = flx_data.get("fulfillable", 0)
                flx_reserved = flx_data.get("reserved", 0)
                flx_inbound  = flx_data.get("inbound", 0)
                stock_fba    = 0   # FLX no aparece en columna FBA

            # Stock principal para días supply: FBA > FLX > FBM
            if stock_fba > 0:
                disp_stock       = stock_fba
                fulfillment_type = "FBA"
            elif stock_flx > 0:
                disp_stock       = stock_flx
                fulfillment_type = "FLX"
            elif stock_fbm > 0:
                disp_stock       = stock_fbm
                fulfillment_type = "FBM"
            else:
                disp_stock = 0
                fulfillment_type = "FLX" if "-FLX" in sku.upper() else ("FBA" if bool(fba_d) else "FBM")

            sales = sku_sales.get(sku, {"units": 0, "revenue": 0.0})
            units_30d   = sales["units"]
            revenue_30d = sales["revenue"]
            vel_dia     = units_30d / 30.0
            dias_supply = round(disp_stock / vel_dia, 1) if vel_dia > 0 else None

            if dias_supply is None:
                supply_color = "gray"
            elif dias_supply < 14:
                supply_color = "red"
            elif dias_supply < 30:
                supply_color = "yellow"
            else:
                supply_color = "green"

            enriched.append({
                "sku":              sku,
                "asin":             asin,
                "title":            summary_0.get("itemName", sku)[:65],
                "price":            price,
                "status":           status,
                "fba_stock":        disp_stock,       # stock principal (para días supply)
                "fba_stock_fba":    stock_fba,        # solo FBA puro (para filtro "fba")
                "fulfillment_type": fulfillment_type, # "FBA" | "FBM" | "FLX"
                "stock_fba":        stock_fba,        # Amazon FBA warehouse
                "stock_fbm":        stock_fbm,        # Merchant Fulfilled (bodega propia FBM)
                "stock_flx":        stock_flx,        # Seller Flex / Amazon Onsite
                "fba_reserved":     fba_reserved,
                "inbound":          inbound,
                "units_30d":        units_30d,
                "revenue_30d":      round(revenue_30d, 2),
                "vel_dia":          round(vel_dia, 2),
                "dias_supply":      dias_supply,
                "supply_color":     supply_color,
                "is_fba":           bool(fba_d) or fba_stock_fba > 0,
                "is_top":           units_30d >= 5,
                "is_low":           0 < units_30d < 2,
                # Deal/Sale activo
                "is_deal":          deal["is_deal"],
                "deal_price":       deal["deal_price"],
                "list_price":       deal["list_price"],
                "deal_pct":         deal["deal_pct"],
                "amazon_url":   f"https://www.amazon.com.mx/dp/{asin}" if asin else "",
                "sc_url": (
                    f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={asin}"
                    if asin else "https://sellercentral.amazon.com.mx/inventory"
                ),
                # BM — lee del caché si disponible (permite filtrar/ordenar por BM en toda la tabla)
                # _enrich_bm_amz sobreescribirá con datos frescos para la página actual
                **_bm_from_cache(sku),
                "flx_reserved": flx_reserved,
                "flx_inbound":  flx_inbound,
            })

        # ── Pre-enrich FLX con BM (solo para columnas BM Disp/Res/MTY/CDMX/TJ) ─
        # stock_flx ya viene correcto de FBA API — solo necesitamos datos BM de bodega.
        flx_pre = [e for e in enriched if "-FLX" in e["sku"].upper()]
        if flx_pre:
            await _enrich_bm_amz(flx_pre)

        # ── Filtrar ────────────────────────────────────────────────────────
        if filter == "fba":
            enriched = [e for e in enriched if e["stock_fba"] > 0 or e["is_fba"]]
        elif filter == "fbm":
            enriched = [e for e in enriched if e["stock_fbm"] > 0]
        elif filter == "flx":
            enriched = [e for e in enriched if "-FLX" in e["sku"].upper()]
        elif filter == "top":
            enriched = [e for e in enriched if e["is_top"]]
        elif filter == "low":
            enriched = [e for e in enriched if e["is_low"]]
        elif filter == "nostock":
            # FLX sin stock = sin stock en Amazon Onsite (stock_flx=0)
            enriched = [
                e for e in enriched
                if e["stock_fba"] == 0 and e["stock_fbm"] == 0
                and (e["stock_flx"] == 0 if "-FLX" in e["sku"].upper() else True)
            ]
        elif filter == "hasbm":
            # Con Stock BM: BM disponible > 0 (hay inventario en bodega)
            enriched = [e for e in enriched if e["bm_avail"] > 0]
        elif filter == "nobm":
            # Sin Stock BM: BM disponible = 0 (bodega vacía — necesita reposición)
            enriched = [e for e in enriched if e["bm_avail"] == 0]

        # ── Ordenar ────────────────────────────────────────────────────────
        desc = (sort_dir != "asc")
        if sort == "flx":
            enriched.sort(key=lambda x: (x["stock_flx"] + x["flx_reserved"]), reverse=desc)
        elif sort == "stock":
            enriched.sort(key=lambda x: (x["fba_stock"], x["fba_stock_fba"]), reverse=desc)
        elif sort == "fbm":
            enriched.sort(key=lambda x: x["stock_fbm"], reverse=desc)
        elif sort == "revenue":
            enriched.sort(key=lambda x: x["revenue_30d"], reverse=desc)
        elif sort == "price":
            enriched.sort(key=lambda x: x["price"], reverse=desc)
        elif sort == "bm":
            enriched.sort(key=lambda x: x["bm_avail"], reverse=desc)
        elif sort == "mty":
            enriched.sort(key=lambda x: x["bm_mty"], reverse=desc)
        elif sort == "cdmx":
            enriched.sort(key=lambda x: x["bm_cdmx"], reverse=desc)
        elif sort == "supply":
            _none_val = -1 if desc else float("inf")
            enriched.sort(key=lambda x: x["dias_supply"] if x["dias_supply"] is not None else _none_val, reverse=desc)
        else:  # units (default)
            enriched.sort(key=lambda x: x["units_30d"], reverse=desc)

        # ── Búsqueda ───────────────────────────────────────────────────────
        if q:
            q_low = q.strip().lower()
            enriched = [
                e for e in enriched
                if q_low in e["sku"].lower()
                or q_low in e["title"].lower()
                or q_low in e["asin"].lower()
            ]

        # ── Paginación ─────────────────────────────────────────────────────
        total      = len(enriched)
        per_page   = max(10, min(100, per_page))
        total_pages = max(1, (total + per_page - 1) // per_page)
        page       = max(1, min(page, total_pages))
        start      = (page - 1) * per_page
        page_items = enriched[start: start + per_page]

        # ── BM: solo para la página actual ────────────────────────────────
        await _enrich_bm_amz(page_items)

        # ── action_needed: calculado con BM fresco post-enrich ─────────────
        for _e in page_items:
            _no_amz  = _e["stock_fba"] == 0 and _e["stock_fbm"] == 0 and _e["stock_flx"] == 0
            _has_bm  = _e["bm_avail"] > 0
            _add_qty = max(1, math.ceil(_e["bm_avail"] * 0.4)) if _has_bm else 0
            if _no_amz and _has_bm:
                _e["action_needed"] = "add_fbm"
                _e["add_qty"]       = _add_qty
            elif not _no_amz and not _has_bm:
                if _e["fulfillment_type"] == "FBM":
                    _e["action_needed"] = "pause_fbm"
                else:
                    _e["action_needed"] = "warn_fba_nobm"
                _e["add_qty"] = 0
            else:
                _e["action_needed"] = None
                _e["add_qty"]       = 0

        # Timestamp del caché de listings (para badge de frescura en la UI)
        cache_ts = int(_listings_cache.get(client.seller_id, (_time.time(), None))[0])

        ctx = {
            "request":          request,
            "listings":         page_items,
            "total":            total,
            "total_pages":      total_pages,
            "page":             page,
            "per_page":         per_page,
            "sort":             sort,
            "sort_dir":         sort_dir,
            "filter":           filter,
            "q":                q,
            "nickname":         client.nickname,
            "marketplace":      client.marketplace_name,
            "last_updated_ts":   cache_ts,
            "flx_loading":       flx_loading,        # True = BG refresh de FLX activo
            "sku_sales_loading": sku_sales_loading,  # True = BG refresh de ventas activo
            "bm_loading":        bm_loading,         # True = BG pre-fetch BM activo
        }
        return _templates.TemplateResponse("partials/amazon_products_inventario.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en inventario v2")
        return _render_error(request, "amazon_products_inventario.html", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# BG-STATUS — estado de los refreshes en background del tab Inventario
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/inventario/bg-status")
async def inventario_bg_status(request: Request):
    """
    Retorna si los refreshes BG (sku_sales, BM, FLX) están activos.
    El frontend hace polling cada 5s y recarga la tabla cuando todo termina.
    """
    client = await get_amazon_client()
    if not client:
        return JSONResponse({"ready": True})
    sid = client.seller_id
    sku_sales_active = sid in _sku_sales_refreshing
    bm_active        = "bm_all" in _bm_all_refreshing
    flx_active       = sid in _flx_stock_refreshing
    return JSONResponse({
        "ready":     not sku_sales_active and not bm_active and not flx_active,
        "sku_sales": sku_sales_active,
        "bm":        bm_active,
        "flx":       flx_active,
    })


# ─────────────────────────────────────────────────────────────────────────────
# STOCK ACTION — actualiza qty de un listing FBM desde el tab Inventario
# ─────────────────────────────────────────────────────────────────────────────

class StockActionBody(BaseModel):
    action:   str        # "add_fbm" | "pause"
    quantity: int = 0


@router.post("/products/{sku}/stock-action")
async def amazon_stock_action(sku: str, body: StockActionBody, request: Request):
    """
    Actualiza el stock FBM de un listing directamente desde el tab Inventario.
    - action="add_fbm": pone body.quantity unidades como FBM
    - action="pause":   pone qty=0 (pausa el listing sin eliminarlo)
    Invalida la caché de listings y FBA para que la próxima carga muestre datos frescos.
    """
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon")

    qty = body.quantity if body.action == "add_fbm" else 0
    try:
        result = await client.update_listing_quantity(sku, qty)
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as exc:
        logger.exception("[StockAction] Error al actualizar qty de %s", sku)
        raise HTTPException(status_code=500, detail=str(exc))

    # Invalidar caché para que la próxima carga muestre datos actualizados
    _listings_cache.pop(client.seller_id, None)
    _fba_cache.pop(client.seller_id, None)

    logger.info("[StockAction] SKU=%s action=%s qty=%d → ok", sku, body.action, qty)
    return {"ok": True, "sku": sku, "action": body.action, "quantity": qty}


@router.get("/products/stock", response_class=HTMLResponse)
async def amazon_products_stock_alerts(request: Request):
    """
    Alertas de stock clasificadas por urgencia:
    - Sin Stock: fulfillable=0 con ventas activas (ventas perdidas)
    - Stock Bajo: fulfillable<10 con ventas activas (riesgo inminente)
    - Restock Urgente: dias_supply<14 (basado en velocidad de ventas)
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_stock.html")

    try:
        fba_summaries, listings = await asyncio.gather(
            _get_fba_cached(client),
            _get_listings_cached(client),
        )
        sku_sales, _sku_loading_stock = _get_sku_sales_cached(client)  # sync, nunca bloquea

        # Índice de listings por SKU — título, ASIN y status
        listings_idx = {}
        for item in listings:
            sku = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            summary_0 = summaries[0] if summaries else {}
            listings_idx[sku] = {
                "title": summary_0.get("itemName", sku)[:65],
                "asin": summary_0.get("asin") or "",
                "status": _listing_status(summaries),
            }

        sin_stock = []
        stock_bajo = []
        restock_urgente = []

        for s in fba_summaries:
            sku = s.get("sellerSku", "")
            asin = s.get("asin", "")
            name = s.get("productName", sku)[:65]
            details = s.get("inventoryDetails", {})
            fulfillable = int(details.get("fulfillableQuantity") or 0)
            inbound = (
                int(details.get("inboundWorkingQuantity") or 0)
                + int(details.get("inboundShippedQuantity") or 0)
            )

            sales = sku_sales.get(sku, {"units": 0, "revenue": 0.0})
            units_30d = sales["units"]
            vel_dia = units_30d / 30.0

            listing = listings_idx.get(sku, {"title": name, "asin": asin, "status": "ACTIVE"})
            title = listing["title"] or name
            listing_asin = listing["asin"] or asin
            sc_url = (
                f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={listing_asin}"
                if listing_asin else "https://sellercentral.amazon.com.mx/inventory"
            )

            base = {
                "sku": sku,
                "asin": listing_asin,
                "title": title,
                "fulfillable": fulfillable,
                "inbound": inbound,
                "units_30d": units_30d,
                "vel_dia": round(vel_dia, 2) if vel_dia > 0 else None,
                "sc_url": sc_url,
            }

            # ── Sin Stock: fulfillable = 0 (sin condición de ventas)
            if fulfillable == 0:
                sin_stock.append(base)

            # ── Stock Bajo: 1–10 uds en FBA (sin condición de ventas)
            elif 0 < fulfillable <= 10:
                dias_hasta_0 = round(fulfillable / vel_dia, 1) if vel_dia > 0 else None
                entry = {**base, "dias_hasta_0": dias_hasta_0}
                entry["recomendacion"] = (
                    f"Enviar pronto — ~{round(vel_dia * 30)} uds/mes"
                    if vel_dia > 0 else "Reabastece FBA — activa ventas para calcular velocidad"
                )
                stock_bajo.append(entry)

            # ── Restock Urgente: >10 uds pero se agotan en <14 días según velocidad
            elif vel_dia > 0 and fulfillable > 10:
                dias_supply = fulfillable / vel_dia
                if dias_supply < 14:
                    sugeridas = max(0, round(vel_dia * 60) - fulfillable - inbound)
                    restock_urgente.append({
                        **base,
                        "dias_supply": round(dias_supply, 1),
                        "sugeridas": sugeridas,
                    })

        # Ordenar: sin stock por título, stock_bajo por días hasta 0, restock por días supply
        sin_stock.sort(key=lambda x: x["title"])
        stock_bajo.sort(key=lambda x: (x.get("dias_hasta_0") or 9999))
        restock_urgente.sort(key=lambda x: x.get("dias_supply", 9999))

        ctx = {
            "request": request,
            "sin_stock": sin_stock,
            "stock_bajo": stock_bajo,
            "restock_urgente": restock_urgente,
            "nickname": client.nickname,
            "marketplace": client.marketplace_name,
        }
        return _templates.TemplateResponse("partials/amazon_products_stock.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en stock-alerts")
        return _render_error(request, "amazon_products_stock.html", str(e))


@router.get("/products/sin-publicar", response_class=HTMLResponse)
async def amazon_products_sin_publicar(request: Request):
    """
    Listings no activos: Suprimidos (INACTIVE+issues), Inactivos (INACTIVE sin issues),
    y activos con issues que necesitan atención.
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_sin_publicar.html")

    try:
        listings = await _get_listings_cached(client)

        suprimidos = []
        inactivos = []
        con_issues = []

        for item in listings:
            sku = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            issues = item.get("issues", [])
            status = _listing_status(summaries)
            summary_0 = summaries[0] if summaries else {}
            asin = summary_0.get("asin") or ""
            title = summary_0.get("itemName", sku)[:65]
            last_updated = ""
            raw_date = summary_0.get("lastUpdatedDate") or ""
            if raw_date:
                last_updated = raw_date[:10]

            sc_url = (
                f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={asin}"
                if asin else "https://sellercentral.amazon.com.mx/inventory"
            )
            issue_list = [
                {"severity": i.get("severity", "ERROR"), "message": i.get("message", "Issue desconocido")}
                for i in issues[:3]
            ]
            item_data = {
                "sku": sku,
                "asin": asin,
                "title": title,
                "status": status,
                "last_updated": last_updated,
                "issues": issue_list,
                "sc_url": sc_url,
            }

            if status == "ACTIVE":
                if issues:
                    con_issues.append(item_data)
            elif issues:
                suprimidos.append(item_data)
            else:
                inactivos.append(item_data)

        ctx = {
            "request": request,
            "suprimidos": suprimidos,
            "inactivos": inactivos,
            "con_issues": con_issues,
            "nickname": client.nickname,
            "marketplace": client.marketplace_name,
        }
        return _templates.TemplateResponse("partials/amazon_products_sin_publicar.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en sin-publicar")
        return _render_error(request, "amazon_products_sin_publicar.html", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# SELLER FLEX — inventario en bodega propia + generador CSV para carga en lote
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/seller-flex", response_class=HTMLResponse)
async def amazon_products_seller_flex(
    request: Request,
    q:     str  = Query("", description="Búsqueda por SKU o título"),
    force: bool = Query(False, description="Limpia caché antes de cargar"),
):
    """
    Muestra todos los listings con sufijo -FLX (Seller Flex / Amazon Onsite).
    Cruza con FBA inventory para ver stock actual y con BinManager para
    saber cuánto hay disponible en bodega para recibir.
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_seller_flex.html")

    if force:
        _listings_cache.pop(client.seller_id, None)
        _fba_cache.pop(client.seller_id, None)
        _flx_stock_cache.pop(client.seller_id, None)

    try:
        listings = await _get_listings_cached(client)

        # FLX stock real-time — stale-while-revalidate (no bloquea)
        all_flx_skus = [item.get("sku", "") for item in listings
                        if "-FLX" in (item.get("sku", "") or "").upper()]
        flx_stock_index = _get_flx_stock_cached(client, all_flx_skus)  # sync, nunca bloquea

        # Filtrar solo SKUs -FLX
        flx_items = []
        for item in listings:
            sku = item.get("sku", "")
            if "-FLX" not in sku.upper():
                continue

            summaries = item.get("summaries", [{}])
            summary_0 = summaries[0] if summaries else {}
            offers    = item.get("offers", [])

            asin   = summary_0.get("asin") or ""
            title  = summary_0.get("itemName", sku)
            price  = _parse_price(offers)
            status = _listing_status(summaries)

            # Stock real desde FBA API — coincide exactamente con Seller Central
            flx_data  = flx_stock_index.get(sku, {})
            fba_stock = flx_data.get("fulfillable", 0)
            flx_res   = flx_data.get("reserved", 0)
            flx_inbd  = flx_data.get("inbound", 0)

            flx_items.append({
                "sku":          sku,
                "asin":         asin,
                "title":        title[:70],
                "title_full":   title,
                "price":        price,
                "status":       status,
                "fba_stock":    fba_stock,
                "flx_reserved": flx_res,
                "unfulfill":    0,
                "inbound":      flx_inbd,
                # BinManager — rellenado abajo
                "bm_avail":    0,
                "bm_mty":      0,
                "bm_cdmx":     0,
                "sc_url": (
                    f"https://sellercentral.amazon.com.mx/inventory"
                    f"?searchField=ASIN&searchValue={asin}"
                    if asin else "https://sellercentral.amazon.com.mx/inventory"
                ),
            })

        # Filtro de búsqueda
        if q:
            ql = q.lower()
            flx_items = [
                i for i in flx_items
                if ql in i["sku"].lower() or ql in i["title"].lower()
            ]

        # Enriquecer con BinManager (reutiliza la función del tab inventario)
        await _enrich_bm_amz(flx_items)

        # FLX loading state — para mostrar "···" en template si BG activo
        flx_loading = client.seller_id in _flx_stock_refreshing

        # Ordenar: primero los que tienen stock Amazon, luego por bm_avail desc
        flx_items.sort(key=lambda x: (
            0 if (x["status"] == "ACTIVE" and (x["fba_stock"] > 0 or x["bm_avail"] > 0)) else (1 if x["status"] == "ACTIVE" else 2),
            -(x["fba_stock"] + x["flx_reserved"]),
        ))

        ctx = {
            "request":     request,
            "items":       flx_items,
            "total":       len(flx_items),
            "q":           q,
            "flx_loading": flx_loading,
        }
        return _templates.TemplateResponse(
            "partials/amazon_products_seller_flex.html", ctx
        )

    except Exception as e:
        logger.exception("[Amazon Products] Error en Seller Flex")
        return _render_error(request, "amazon_products_seller_flex.html", str(e))


async def _run_onsite_sync(client) -> None:
    """Genera el reporte FBA MYI y actualiza caché + estado. Usable desde cualquier contexto."""
    seller_id = client.seller_id
    try:
        logger.info(f"[Onsite Sync] Iniciando reporte para seller {seller_id}")
        data = await client.get_onsite_inventory_report(max_wait_secs=180)
        _onsite_stock_cache[seller_id] = (_time.time(), data)
        _onsite_sync_count[seller_id] = len(data)
        _onsite_sync_state[seller_id] = "done"
        logger.info(f"[Onsite Sync] Reporte listo: {len(data)} SKUs con stock")
    except Exception as e:
        logger.error(f"[Onsite Sync] ERROR: {type(e).__name__}: {e}", exc_info=True)
        _onsite_sync_state[seller_id] = "error"


@router.get("/products/seller-flex/raw-listing")
async def inspect_raw_listing(request: Request, sku: str = Query("", description="SKU a inspeccionar")):
    """
    Debug: Obtiene el listing raw de Amazon para un SKU específico vía getListingsItem.
    Útil para ver qué devuelve fulfillmentAvailability para items Seller Flex.
    Ejemplo: /api/amazon/products/seller-flex/raw-listing?sku=SNEE000054-FLX01
    """
    client = await get_amazon_client()
    if not client:
        return {"error": "Sin cuenta Amazon"}

    if not sku:
        # Mostrar muestra de FLX items del caché de listings
        cached = _listings_cache.get(client.seller_id)
        if not cached:
            return {"error": "Sin caché de listings. Visita el tab Inventario primero."}
        _, listings = cached
        flx_sample = [
            {
                "sku": item.get("sku"),
                "fulfillmentAvailability": item.get("fulfillmentAvailability", []),
                "summaries_status": [s.get("status") for s in item.get("summaries", [])],
            }
            for item in listings
            if "-FLX" in (item.get("sku") or "").upper()
        ][:10]
        return {"flx_sample_from_listings_cache": flx_sample, "total_flx": len([i for i in listings if "-FLX" in (i.get("sku") or "").upper()])}

    # Fetch individual listing
    try:
        params = [
            ("marketplaceIds", client.marketplace_id),
            ("includedData", "summaries,attributes,offers,fulfillmentAvailability,issues"),
        ]
        result = await client._request(
            "GET",
            f"/listings/2021-08-01/items/{client.seller_id}/{sku}",
            params=params,
        )
        attrs = result.get("attributes") or {}
        return {
            "sku": sku,
            "fulfillmentAvailability": result.get("fulfillmentAvailability", []),
            "attr_fulfillment_availability": attrs.get("fulfillment_availability", "NOT_FOUND"),
            "attr_purchasable_offer": attrs.get("purchasable_offer", "NOT_FOUND"),
            "summaries_status": (result.get("summaries") or [{}])[0].get("status", []),
            "attributes_keys": list(attrs.keys()),
            "raw_keys": list(result.keys()),
        }
    except Exception as e:
        return {"error": str(e), "sku": sku}


@router.get("/products/seller-flex/cache-inspect")
async def inspect_onsite_cache(request: Request):
    """
    Debug: Inspecciona el caché de stock Onsite (Seller Flex).
    Útil para diagnosticar si el reporte FBA MYI devuelve datos de SKUs -FLX.
    Retorna JSON con estado del caché, muestra de SKUs y SKUs FLX específicamente.
    """
    client = await get_amazon_client()
    if not client:
        return {"error": "Sin cuenta Amazon configurada"}

    seller_id = client.seller_id
    cached = _onsite_stock_cache.get(seller_id)

    if not cached:
        return {
            "cache_status": "empty",
            "sync_state":   _onsite_sync_state.get(seller_id, "idle"),
            "total_skus":   0,
            "flx_skus":     {},
            "sample_skus":  {},
        }

    ts, data = cached
    age_s = int(_time.time() - ts)
    flx_data  = {k: v for k, v in data.items() if "-FLX" in k.upper()}
    sample    = dict(list(data.items())[:20])

    return {
        "cache_status":   "valid" if age_s < _ONSITE_STOCK_TTL else "expired",
        "cache_age_s":    age_s,
        "cache_ts":       ts,
        "sync_state":     _onsite_sync_state.get(seller_id, "idle"),
        "total_skus":     len(data),
        "flx_skus_count": len(flx_data),
        "flx_skus":       flx_data,
        "sample_skus":    sample,
    }


@router.post("/products/seller-flex/start-sync")
async def start_seller_flex_sync(request: Request):
    """
    Inicia la generación del reporte FBA MYI en BACKGROUND y retorna INMEDIATAMENTE.
    El reporte tarda 30-90 seg — no bloquear la conexión HTTP (Railway corta a 60 seg).

    Retorna: {started: bool, status: str}
    El front-end debe hacer polling a /sync-status cada 5 seg.
    """
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon conectada")

    seller_id = client.seller_id

    # Si ya hay un sync corriendo, no lanzar otro
    if _onsite_sync_state.get(seller_id) == "syncing":
        return {"started": False, "status": "syncing", "msg": "Sincronización ya en curso"}

    # Marcar como syncing y lanzar tarea en background
    _onsite_sync_state[seller_id] = "syncing"
    _onsite_sync_count[seller_id] = 0
    _onsite_stock_cache.pop(seller_id, None)
    asyncio.create_task(_run_onsite_sync(client))
    return {"started": True, "status": "syncing"}


@router.get("/products/seller-flex/sync-status")
async def get_seller_flex_sync_status(request: Request):
    """
    Retorna el estado actual del sync en background.
    El front-end llama este endpoint cada 5 seg mientras status == "syncing".

    Retorna: {status: "idle"/"syncing"/"done"/"error", skus_found: int, report_ts: str}
    """
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401)

    seller_id = client.seller_id
    raw_status = _onsite_sync_state.get(seller_id, "idle")

    # Extraer estado limpio y mensaje de error si aplica
    if raw_status.startswith("error:"):
        status = "error"
        error_msg = raw_status[6:]
    else:
        status = raw_status
        error_msg = ""

    skus_found = _onsite_sync_count.get(seller_id, 0)

    report_ts = ""
    if seller_id in _onsite_stock_cache:
        ts_o, cached_data = _onsite_stock_cache[seller_id]
        skus_found = skus_found or len(cached_data)
        from datetime import datetime as _dt
        report_ts = _dt.fromtimestamp(ts_o).strftime("%d/%m %H:%M")

    return {
        "status":     status,
        "skus_found": skus_found,
        "report_ts":  report_ts,
        "error_msg":  error_msg,
    }


@router.post("/products/seller-flex/csv")
async def generate_seller_flex_csv(request: Request):
    """
    Genera el CSV de carga en lote para onsite.amazon.com → Recibir → Carga en lote.

    Body JSON:
    {
      "bin":   "A101",          # BIN por defecto (se puede sobrescribir por item)
      "items": [
        {"sku": "SNMC000484-FLX", "quantity": 10, "bin": "A101",
         "disposition": "GOOD",  "exp_date": "", "mfg_date": ""}
      ]
    }

    Retorna el CSV como descarga directa.
    """
    import io
    import csv
    from fastapi.responses import StreamingResponse

    body = await request.json()
    default_bin = (body.get("bin") or "").strip()
    items = body.get("items", [])

    if not items:
        raise HTTPException(status_code=400, detail="Sin items para generar CSV")

    output = io.StringIO()
    writer = csv.writer(output)

    # Cabecera exacta que exige el portal
    writer.writerow([
        "BIN",
        "Merchant SKU",
        "Quantity",
        "Disposition (GOOD/BAD)",
        "Expiration Date (DD/MM/YYYY)",
        "Manufacturing Date (DD/MM/YYYY)",
    ])

    for item in items:
        sku      = str(item.get("sku", "")).strip()
        quantity = int(item.get("quantity") or 0)
        bin_loc  = str(item.get("bin") or default_bin or "").strip()
        disp     = str(item.get("disposition") or "GOOD").upper()
        exp_date = str(item.get("exp_date") or "").strip()
        mfg_date = str(item.get("mfg_date") or "").strip()

        if not sku or quantity <= 0:
            continue

        writer.writerow([bin_loc, sku, quantity, disp, exp_date, mfg_date])

    output.seek(0)
    csv_content = output.getvalue()

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="seller_flex_recibir.csv"'
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND PERIODIC SYNC — mantiene el caché Onsite siempre fresco
# ─────────────────────────────────────────────────────────────────────────────

async def _onsite_periodic_sync_loop() -> None:
    """
    Loop de sync periódico — actualmente DESACTIVADO.

    NOTA: GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA devuelve FATAL para cuentas
    que usan exclusivamente Seller Flex (Amazon Onsite MX) sin FBA tradicional.
    El SP-API no expone el inventario de Amazon Onsite a través de ningún
    endpoint o reporte público disponible.

    El inventario FLX se muestra usando el stock BM (BinManager) como proxy,
    que es la fuente de verdad del inventario en bodega del vendedor.

    Si en el futuro Amazon expone este dato via SP-API, reactivar este loop.
    """
    logger.info("[Onsite AutoSync] Loop DESACTIVADO — GET_FBA_MYI_UNSUPPRESSED devuelve FATAL "
                "para cuentas Seller Flex sin FBA. Usando datos BM como proxy.")
    # No hacer nada — el loop se inicia pero inmediatamente termina


def start_onsite_background_sync() -> None:
    """Registra el loop de sync periódico. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_onsite_periodic_sync_loop())


def _render_no_account(request: Request, template: str) -> HTMLResponse:
    """Template de error cuando no hay cuenta Amazon configurada."""
    return _templates.TemplateResponse(
        f"partials/{template}",
        {"request": request, "error": "Sin cuenta Amazon", "no_account": True},
    )


def _render_error(request: Request, template: str, msg: str) -> HTMLResponse:
    """Template de error genérico."""
    return _templates.TemplateResponse(
        f"partials/{template}",
        {"request": request, "error": msg, "no_account": False},
    )
