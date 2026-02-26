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
import re as _re
import time as _time
from datetime import datetime, timedelta
from typing import Optional
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

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
_sku_sales_cache:  dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, {sku: {units,revenue}})}
_sku_sales_locks:  dict[str, asyncio.Lock] = {}

_LISTINGS_TTL = 300   # 5 minutos
_FBA_TTL      = 300   # 5 minutos
_BUYBOX_TTL   = 600   # 10 minutos
_SKU_SALES_TTL = 1800  # 30 minutos (costo alto: get_order_items por cada orden)

# ─── BinManager (para tab Inventario) ────────────────────────────────────────
_BM_WH_URL    = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
_BM_AVAIL_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"
_BM_LOC_IDS   = "47,62,68"
_bm_amz_cache: dict[str, tuple[float, dict]] = {}
_BM_AMZ_TTL   = 900   # 15 min
_AMZ_BM_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")


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


async def _get_sku_sales_cached(client) -> dict:
    """
    Retorna {sku: {"units": int, "revenue": float}} para los últimos 30 días.

    Usa Orders API (30d) + Order Items API por cada orden.
    Cacheado 30 min (operación costosa: 1 req por orden).
    """
    now = _time.time()
    key = client.seller_id

    if key in _sku_sales_cache:
        ts, data = _sku_sales_cache[key]
        if now - ts < _SKU_SALES_TTL:
            return data

    if key not in _sku_sales_locks:
        _sku_sales_locks[key] = asyncio.Lock()

    async with _sku_sales_locks[key]:
        # Double-check dentro del lock
        if key in _sku_sales_cache:
            ts, data = _sku_sales_cache[key]
            if now - ts < _SKU_SALES_TTL:
                return data

        created_after = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            orders = await client.get_orders(created_after)
        except Exception as e:
            logger.warning(f"[Amazon SKU Sales] Error obteniendo órdenes: {e}")
            _sku_sales_cache[key] = (_time.time(), {})
            return {}

        valid_orders = [
            o for o in orders
            if o.get("OrderStatus") not in ("Cancelled", "Pending")
        ]

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
                logger.debug(f"[Amazon SKU Sales] Error en orden {order_id}: {e}")

        # Procesar en lotes — máximo 12 segundos para no bloquear la UI
        batch_size = 5
        _loop_start = _time.time()
        for i in range(0, len(valid_orders), batch_size):
            # Cortocircuito si tardamos más de 12 segundos
            if _time.time() - _loop_start > 12.0:
                logger.warning(
                    f"[Amazon SKU Sales] Timeout parcial tras 12s "
                    f"({i}/{len(valid_orders)} órdenes procesadas)"
                )
                break
            batch = valid_orders[i:i + batch_size]
            await asyncio.gather(*[_fetch_items(o) for o in batch])
            if i + batch_size < len(valid_orders):
                await asyncio.sleep(0.5)  # Rate limit SP-API

        _sku_sales_cache[key] = (_time.time(), sku_data)
        return sku_data


# ─────────────────────────────────────────────────────────────────────────────
# BINMANAGER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _amz_base_sku(sku: str) -> str:
    """Limpia el SKU de Amazon para consultar BinManager (quita sufijos de condición)."""
    if not sku:
        return ""
    s = _re.split(r'\s*[/+]\s*', sku)[0].strip()
    s = _re.sub(r'\(\d+\)', '', s).strip()
    s = _re.sub(r'[()]', '', s).strip()
    up = s.upper()
    for sfx in _AMZ_BM_SUFFIXES:
        if up.endswith(sfx):
            s = s[:-len(sfx)]
            break
    return s


def _amz_bm_conditions(sku: str) -> str:
    """Condiciones BinManager según el sufijo del SKU de Amazon."""
    up = sku.upper()
    if up.endswith("-ICB") or up.endswith("-ICC"):
        return "GRA,GRB,GRC,ICB,ICC,NEW"
    return "GRA,GRB,GRC,NEW"


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


async def _enrich_bm_amz(items: list) -> None:
    """
    Enriquece items in-place con datos BinManager (bm_mty, bm_cdmx, bm_tj, bm_avail, bm_reserved).
    Usa caché _bm_amz_cache (TTL 15 min). Solo fetchea SKUs no cacheados.
    """
    now = _time.time()
    to_fetch: list[str] = []

    for item in items:
        sku = item.get("sku", "")
        if not sku:
            item.update(_BM_EMPTY)
            continue
        cached = _bm_amz_cache.get(sku.upper())
        if cached and (now - cached[0]) < _BM_AMZ_TTL:
            item.update(cached[1])
        else:
            to_fetch.append(sku)
            item.update(_BM_EMPTY)

    if not to_fetch:
        return

    sem = asyncio.Semaphore(15)

    async def _fetch_one(sku: str, http: "httpx.AsyncClient") -> None:
        base = _amz_base_sku(sku)
        cond = _amz_bm_conditions(sku)
        wh_payload = {
            "COMPANYID": 1, "SKU": base, "WarehouseID": None,
            "LocationID": _BM_LOC_IDS, "BINID": None,
            "Condition": cond, "SUPPLIERS": None, "ForInventory": 0,
        }
        av_payload = {
            "COMPANYID": 1, "TYPEINVENTORY": 0, "WAREHOUSEID": None,
            "LOCATIONID": _BM_LOC_IDS, "BINID": None,
            "PRODUCTSKU": base, "CONDITION": cond,
            "SUPPLIERS": None, "LCN": None, "SEARCH": base,
        }
        wh_rows, av_rows = [], []
        async with sem:
            try:
                r_wh, r_av = await asyncio.gather(
                    http.post(_BM_WH_URL, json=wh_payload, timeout=15.0),
                    http.post(_BM_AVAIL_URL, json=av_payload, timeout=15.0),
                    return_exceptions=True,
                )
                if not isinstance(r_wh, Exception) and r_wh.status_code == 200:
                    wh_rows = r_wh.json()
                if not isinstance(r_av, Exception) and r_av.status_code == 200:
                    av_rows = r_av.json()
            except Exception:
                pass
        mty, cdmx, tj = _parse_wh_rows_amz(wh_rows)
        avail    = sum(row.get("Available", 0) or 0 for row in av_rows)
        reserved = sum(row.get("Required", 0)  or 0 for row in av_rows)
        inv = {"bm_mty": mty, "bm_cdmx": cdmx, "bm_tj": tj,
               "bm_avail": avail, "bm_reserved": reserved}
        _bm_amz_cache[sku.upper()] = (_time.time(), inv)
        for item in items:
            if item.get("sku", "").upper() == sku.upper():
                item.update(inv)

    async with httpx.AsyncClient(timeout=30.0) as http:
        await asyncio.gather(*[_fetch_one(s, http) for s in to_fetch], return_exceptions=True)


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
        sku_sales = await _get_sku_sales_cached(client)

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
        }
        return _templates.TemplateResponse("partials/amazon_products_resumen.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en resumen v2")
        return _render_error(request, "amazon_products_resumen.html", str(e))


@router.get("/products/inventario", response_class=HTMLResponse)
async def amazon_products_inventario(
    request: Request,
    sort:     str = Query("units", description="units|stock|revenue|price"),
    filter:   str = Query("all",   description="all|fba|top|low|nostock"),
    q:        str = Query("",      description="Búsqueda por SKU, ASIN o título"),
    page:     int = Query(1,       description="Página actual"),
    per_page: int = Query(20,      description="Items por página"),
):
    """
    Inventario completo con ventas 30d, días supply y stock BinManager por SKU.
    Filtros: Todos / FBA / Top Ventas / Baja Venta / Sin Stock
    Paginación: 20/pág (server-side). BM enriquece solo la página actual.
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_inventario.html")

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        sku_sales = await _get_sku_sales_cached(client)
        fba_index = _build_fba_index(fba_summaries)

        enriched = []
        for item in listings:
            sku = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            offers = item.get("offers", [])

            status = _listing_status(summaries)
            price = _parse_price(offers)
            summary_0 = summaries[0] if summaries else {}
            fba_d = fba_index.get(sku, {})
            asin = fba_d.get("asin") or summary_0.get("asin") or ""
            fba_details = fba_d.get("inventoryDetails", {})
            fba_stock = int(fba_details.get("fulfillableQuantity") or 0)
            fba_reserved = int((fba_details.get("reservedQuantity") or {}).get("pendingCustomerOrderQuantity") or 0)
            inbound = (
                int(fba_details.get("inboundWorkingQuantity") or 0)
                + int(fba_details.get("inboundShippedQuantity") or 0)
            )

            sales = sku_sales.get(sku, {"units": 0, "revenue": 0.0})
            units_30d   = sales["units"]
            revenue_30d = sales["revenue"]
            vel_dia     = units_30d / 30.0
            dias_supply = round(fba_stock / vel_dia, 1) if vel_dia > 0 else None

            if dias_supply is None:
                supply_color = "gray"
            elif dias_supply < 14:
                supply_color = "red"
            elif dias_supply < 30:
                supply_color = "yellow"
            else:
                supply_color = "green"

            enriched.append({
                "sku":          sku,
                "asin":         asin,
                "title":        summary_0.get("itemName", sku)[:65],
                "price":        price,
                "status":       status,
                "fba_stock":    fba_stock,
                "fba_reserved": fba_reserved,
                "inbound":      inbound,
                "units_30d":    units_30d,
                "revenue_30d":  round(revenue_30d, 2),
                "vel_dia":      round(vel_dia, 2),
                "dias_supply":  dias_supply,
                "supply_color": supply_color,
                "is_fba":       bool(fba_d),
                "is_top":       units_30d >= 5,
                "is_low":       0 < units_30d < 2,
                "amazon_url":   f"https://www.amazon.com.mx/dp/{asin}" if asin else "",
                "sc_url": (
                    f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={asin}"
                    if asin else "https://sellercentral.amazon.com.mx/inventory"
                ),
                # BM — se rellena por _enrich_bm_amz para la página actual
                "bm_avail":    0,
                "bm_reserved": 0,
                "bm_mty":      0,
                "bm_cdmx":     0,
                "bm_tj":       0,
            })

        # ── Filtrar ────────────────────────────────────────────────────────
        if filter == "fba":
            enriched = [e for e in enriched if e["is_fba"]]
        elif filter == "top":
            enriched = [e for e in enriched if e["is_top"]]
        elif filter == "low":
            enriched = [e for e in enriched if e["is_low"]]
        elif filter == "nostock":
            enriched = [e for e in enriched if e["fba_stock"] == 0]

        # ── Ordenar ────────────────────────────────────────────────────────
        if sort == "stock":
            enriched.sort(key=lambda x: x["fba_stock"], reverse=True)
        elif sort == "revenue":
            enriched.sort(key=lambda x: x["revenue_30d"], reverse=True)
        elif sort == "price":
            enriched.sort(key=lambda x: x["price"], reverse=True)
        else:  # units (default)
            enriched.sort(key=lambda x: x["units_30d"], reverse=True)

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

        ctx = {
            "request":     request,
            "listings":    page_items,
            "total":       total,
            "total_pages": total_pages,
            "page":        page,
            "per_page":    per_page,
            "sort":        sort,
            "filter":      filter,
            "q":           q,
            "nickname":    client.nickname,
            "marketplace": client.marketplace_name,
        }
        return _templates.TemplateResponse("partials/amazon_products_inventario.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en inventario v2")
        return _render_error(request, "amazon_products_inventario.html", str(e))


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
        sku_sales = await _get_sku_sales_cached(client)

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
