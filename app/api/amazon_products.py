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
import json
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel

from app.services.amazon_client import get_amazon_client
from app.services import openrouter_client as _or_client
from app.api.metrics import _get_cached_order_metrics
from app.services import user_store as _user_store
from app.services.sku_utils import target_coverage_days_for_sku as _target_coverage_days

logger = logging.getLogger(__name__)


async def _audit(request: Request, action: str, item_id: str = None, detail: dict = None):
    """Fire-and-forget audit log. Nunca interrumpe la respuesta principal."""
    try:
        du = getattr(request.state, "dashboard_user", None)
        if du:
            await _user_store.log_action(
                username=du["username"],
                user_id=du.get("id"),
                action=action,
                item_id=item_id,
                detail=detail,
                ip=request.headers.get("X-Forwarded-For", request.client.host if request.client else None),
            )
    except Exception:
        pass

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

# ─── Deals & Competitive Pricing ─────────────────────────────────────────────
_deals_cache:      dict[str, tuple[float, dict]] = {}  # {seller_id:status: (ts, data)}
_comp_price_cache: dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, data)}
_deal_cands_cache: dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, data)}
_DEALS_TTL      = 300   # 5 min
_COMP_PRICE_TTL = 600   # 10 min
_DEAL_CANDS_TTL = 900   # 15 min

_LISTINGS_TTL    = 600   # 10 min — DB se actualiza c/qty-sync (10 min)
_LISTINGS_TTL_API = 300  # 5 min para API fallback (primer run / DB sparse)
_FBA_TTL      = 1800  # 30 minutos (costoso — paginación completa, stale-while-revalidate)
_fba_refreshing:      set = set()  # seller_ids con BG refresh activo
_listings_loading:   set = set()  # seller_ids con BG fetch de listings activo


def invalidate_listings_cache(seller_id: str | None = None) -> None:
    """Limpia el cache de listings para forzar rebuild desde DB en el próximo request.
    Llamado por el sync de qty/listings cuando hay cambios.
    """
    if seller_id:
        _listings_cache.pop(seller_id, None)
    else:
        _listings_cache.clear()
_BUYBOX_TTL   = 600   # 10 minutos
_SKU_SALES_TTL = 1800  # 30 minutos (costo alto: get_order_items por cada orden)

# ─── BSR (Catalog Items API) ──────────────────────────────────────────────────
_bsr_cache: dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, {asin: (rank, category)})}
_BSR_TTL = 1800  # 30 minutos (cambia poco)

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
# FIX 2026-08-20 (directiva de Jovan): _enrich_bm_amz ya no llama a BM en vivo
# (lee bm_sku_master) -- las URLs/LOCATIONID que este bloque usaba quedaron
# sin uso, se quitan. InventoryBySKUAndCondicion_Quantity está roto (SQL
# "Invalid column name 'binid'") y GlobalStock_InventoryBySKU_Condition
# también (status siempre "Otro") -- por eso nunca se llamaron directo aquí.
_bm_amz_cache: dict[str, tuple[float, dict]] = {}
_BM_AMZ_TTL   = 900   # 15 min
_bm_all_refreshing:   set   = set()  # "bm_all" cuando BG pre-fetch activo
_bm_all_last_refresh: float = 0.0    # timestamp del último BG refresh completo

# ─── Seller Flex: nodo real por (seller_id, almacén) ─────────────────────────
# Confirmado en vivo el 2026-08-22 explorando el portal sellerflex.amazon.com.mx
# con Jovan (ver memoria project_seller_flex_portal_and_qty_gap.md). Necesario
# para filtrar seller_flex_stock por CUENTA -- el mismo texto de SKU puede
# existir como listing separado de VECKTOR y de AUTOBOT (regla del proyecto:
# "SCOPE DE CUENTA — NUNCA MEZCLAR").
_NODE_BY_SELLER_WAREHOUSE = {
    ("A20NFIUQNEYZ1E", "MTY"):  "SYGL",
    ("A20NFIUQNEYZ1E", "CDMX"): "SYQJ",
    ("A252KSQ687FNRO", "MTY"):  "SOKA",
    ("A252KSQ687FNRO", "CDMX"): "SBBQ",
    ("A252KSQ687FNRO", "TJ"):   "SHDN",
}

# ─── FLX Stock real-time (FBA Inventory API — query por SKU específico) ──────
# El scan general de FBA no devuelve items Seller Flex; la query por sellerSkus sí.
_flx_stock_cache: dict[str, tuple[float, dict]] = {}  # {seller_id: (ts, {sku: data})}
# FIX 2026-08-24 (Jovan reportó "actualizando stock" sin fin visible, medido en
# vivo: 2,311 SKUs Onsite en VECKTOR tardó 20+ minutos): el comentario viejo
# ("~30-60s con 1000 SKUs") estaba muy desactualizado para cuentas con
# catálogos grandes -- la mayoría de esos SKUs necesita consulta individual
# (Fase 2, ~0.6-1s cada uno por rate limit real de Amazon, no evitable).
# BUG REAL 2026-08-24 (2da vuelta, Jovan preguntó "es un ciclo infinito?"):
# con TTL=300s (5 min) y un ciclo real de 20-40 min, el caché SIEMPRE
# quedaba "vencido" para cuando terminaba de correr -- la próxima carga de
# página disparaba OTRO ciclo completo de inmediato, por siempre, sin
# descanso. Subido a 3h -- esta vista ya se documenta como "snapshot
# manual, no en vivo" (ver banner en la plantilla), no necesita
# refrescarse sola cada 5 min. El progreso y el resultado sobreviven un
# reinicio del proceso -- ver amz_flx_stock_cache/amz_flx_sync_meta.
_FLX_STOCK_TTL     = 10800
_flx_stock_refreshing: set = set()  # seller_ids con refresh BG activo (evita doble tarea)
_flx_progress: dict[str, tuple[int, int]] = {}  # {seller_id: (skus_procesados, total)} -- progreso en vivo del refresh activo


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


def _is_amz_onsite(item: dict) -> bool:
    """
    True si el item es Seller Flex / Amazon Onsite.
    Detecta por SKU (-FLX, -FLX1, -FLX4 etc.) O por fulfillmentChannelCode AMAZON_NA.
    Algunos productos usan Amazon Onsite sin el sufijo -FLX en el SKU.
    """
    if "-FLX" in (item.get("sku") or "").upper():
        return True
    for fa in (item.get("fulfillmentAvailability") or []):
        if (fa.get("fulfillmentChannelCode") or "").upper() == "AMAZON_NA":
            return True
    return False


def _fulfillment_channel_of(item: dict) -> str:
    """
    Canal real de fulfillment de un listing: 'FBM' (merchant, editable via
    set_qty por la Listings Items API) o 'FBA' (Amazon-fulfilled o Seller Flex,
    NO editable con set_qty directo — requiere enviar inventario a FBA).

    Mismo criterio que amazon_listing_sync.can_update: cualquier cosa que
    _is_amz_onsite() marque como no editable (AMAZON_NA o sufijo -FLX) es FBA.
    """
    return "FBA" if _is_amz_onsite(item) else "FBM"


def _db_status_to_api(status_str: str) -> list:
    """Convierte status de DB ('Active','Inactive'...) al formato de la Listings API (['BUYABLE',...])."""
    s = (status_str or "").strip().upper()
    if s in ("ACTIVE", "BUYABLE"):
        return ["BUYABLE"]
    if s == "DISCOVERABLE":
        return ["DISCOVERABLE"]
    return [s] if s else ["INACTIVE"]


def _build_listings_from_rows(rows, key: str, now: float) -> list:
    """Convierte filas DB → lista de listing dicts compatible con todos los tabs."""
    data = []
    for _r in rows:
        _ts   = _r["synced_at"] or 0
        _lu   = (datetime.fromtimestamp(_ts).strftime("%Y-%m-%dT%H:%M:%SZ") if _ts else "")
        _sta  = _db_status_to_api(_r["status"])
        _chan = (_r["fulfillment"] or "DEFAULT").upper()
        _qty  = _r["available_qty"] or 0
        _price = float(_r["price"] or 0)
        data.append({
            "sku": _r["sku"],
            "summaries": [{"asin": _r["asin"] or "", "itemName": _r["title"] or _r["sku"],
                           "status": _sta, "lastUpdatedDate": _lu}],
            "offers":    ([{"offerType": "B2C", "price": {"amount": _price,
                            "listingPrice": {"amount": _price}}}] if _price else []),
            "fulfillmentAvailability": [{"fulfillmentChannelCode": _chan, "quantity": _qty}],
            "issues":    [],
            "attributes": {},
        })
    return data


async def _refresh_listings_bg(client) -> None:
    """Descarga listings en background — no bloquea el request handler.

    Intenta DB-first (el startup sync puede haber completado mientras esperábamos).
    Fallback a API (máx 1000 items) si DB sigue vacía.
    """
    key = client.seller_id
    try:
        now = _time.time()
        try:
            import aiosqlite as _aio
            from app.services.token_store import DATABASE_PATH as _DB_PATH
            async with _aio.connect(_DB_PATH) as _db:
                _db.row_factory = _aio.Row
                _cnt = (await (await _db.execute(
                    "SELECT COUNT(*) FROM amazon_listings WHERE seller_id=?", (key,)
                )).fetchone())[0]
                if _cnt >= 500:
                    _rows = await (await _db.execute(
                        "SELECT sku, asin, title, status, price, available_qty, fulfillment, synced_at "
                        "FROM amazon_listings WHERE seller_id=?", (key,)
                    )).fetchall()
                    data = _build_listings_from_rows(_rows, key, now)
                    _listings_cache[key] = (now, data)
                    logger.info("[Listings-BG] DB-first seller=%s rows=%d", key, len(data))
                    return
        except Exception as _e:
            logger.warning("[Listings-BG] DB check failed seller=%s: %s", key, _e)
        # API fallback (max 1000 items)
        data = await client.get_all_listings()
        _listings_cache[key] = (now - (_LISTINGS_TTL - _LISTINGS_TTL_API), data)
        logger.info("[Listings-BG] API fallback seller=%s items=%d", key, len(data))
    except Exception as e:
        logger.warning("[Listings-BG] Error seller=%s: %s", key, e)
    finally:
        _listings_loading.discard(key)


async def _get_listings_cached(client) -> list:
    """Stale-while-revalidate: NUNCA bloquea el request handler.

    - Caché fresco  → devuelve datos inmediatamente
    - Caché stale   → devuelve datos viejos + lanza BG refresh
    - DB ≥500 filas → carga desde DB (rápido, sin API call)
    - Sin caché/DB  → devuelve [] + lanza BG fetch; inventario muestra banner
                      "sincronizando" hasta que _invBgPoll detecte que ready=true
    """
    now = _time.time()
    key = client.seller_id
    if key in _listings_cache:
        ts, data = _listings_cache[key]
        if now - ts < _LISTINGS_TTL:
            return data
        # Stale: devolver datos viejos y refrescar en BG
        if key not in _listings_loading:
            _listings_loading.add(key)
            asyncio.create_task(_refresh_listings_bg(client))
        return data

    # ── DB-first path ──
    try:
        import aiosqlite as _aio
        from app.services.token_store import DATABASE_PATH as _DB_PATH
        async with _aio.connect(_DB_PATH) as _db:
            _db.row_factory = _aio.Row
            _cnt = (await (await _db.execute(
                "SELECT COUNT(*) FROM amazon_listings WHERE seller_id=?",
                (client.seller_id,)
            )).fetchone())[0]

            if _cnt >= 500:
                _rows = await (await _db.execute(
                    "SELECT sku, asin, title, status, price, available_qty, fulfillment, synced_at "
                    "FROM amazon_listings WHERE seller_id=?",
                    (client.seller_id,)
                )).fetchall()
                data = _build_listings_from_rows(_rows, key, now)
                _listings_cache[key] = (now, data)
                logger.info("[Amazon Products] listings DB-first seller=%s rows=%d", client.seller_id, len(data))
                return data
    except Exception as _e:
        logger.warning("[Amazon Products] DB-first listings failed, falling back to BG: %s", _e)

    # ── Cold start: no hay caché ni DB suficiente → BG fetch, devolver [] inmediatamente ──
    if key not in _listings_loading:
        _listings_loading.add(key)
        asyncio.create_task(_refresh_listings_bg(client))
    return []


async def _refresh_fba_bg(client) -> None:
    """Descarga FBA inventory completo en background — no bloquea requests."""
    try:
        data = await client.get_fba_inventory_all()
        _fba_cache[client.seller_id] = (_time.time(), data)
        logger.info(f"[FBA-BG] {len(data)} summaries para {client.seller_id}")
    except Exception as _e:
        logger.warning(f"[FBA-BG] Error: {_e}")
    finally:
        _fba_refreshing.discard(client.seller_id)


async def _get_fba_cached(client) -> list:
    """Stale-while-revalidate: NUNCA bloquea el request.

    - Caché fresco  → devuelve datos inmediatamente
    - Caché stale   → devuelve datos viejos + lanza BG refresh
    - Sin caché     → devuelve [] + lanza BG refresh (Inventario carga con FBA vacío,
                       se rellena cuando el BG termina — el usuario recarga o espera el poll)
    """
    now = _time.time()
    key = client.seller_id
    if key in _fba_cache:
        ts, data = _fba_cache[key]
        if now - ts < _FBA_TTL:
            return data
        # Stale: devolver lo que hay y refrescar en BG
        if key not in _fba_refreshing:
            _fba_refreshing.add(key)
            asyncio.create_task(_refresh_fba_bg(client))
        return data
    # Cold start: no hay datos, iniciar BG y devolver lista vacía
    if key not in _fba_refreshing:
        _fba_refreshing.add(key)
        asyncio.create_task(_refresh_fba_bg(client))
    return []


async def _get_bsr_cached(client, asins: list) -> dict:
    """Fetches BSR (Best Seller Rank) from Catalog API for a list of ASINs.

    Returns {asin: {"rank": int, "category": str}} — empty dict if unavailable.
    Uses a 30-min cache keyed by seller_id. Fetches up to 40 ASINs with semaphore-5.
    """
    now = _time.time()
    key = client.seller_id
    cached = _bsr_cache.get(key)
    if cached and (now - cached[0]) < _BSR_TTL:
        return cached[1]

    result: dict = {}
    # Limit to first 40 ASINs to avoid long waits
    target = [a for a in asins if a][:40]
    if not target:
        return result

    # Catalog Items API rate limit = 2 req/s → semaphore-2 + 0.6s delay
    sem = asyncio.Semaphore(2)

    async def _fetch_one(asin: str):
        async with sem:
            await asyncio.sleep(0.6)  # respect 2 req/s
            try:
                data = await client.get_catalog_item(asin)
                if not data:
                    return
                # Handle potential payload wrapper (some API versions)
                if "payload" in data and isinstance(data["payload"], dict):
                    data = data["payload"]
                ranks = data.get("salesRanks", [])
                if not ranks:
                    logger.debug(
                        "[BSR] ASIN %s: no salesRanks. Keys present: %s",
                        asin, list(data.keys())[:8],
                    )
                    return
                # First rank group — displayGroupRanks has the primary BSR
                group = ranks[0]
                rank_list = group.get("displayGroupRanks") or group.get("classificationRanks") or []
                if rank_list:
                    top = rank_list[0]
                    result[asin] = {
                        "rank": top.get("rank"),
                        "category": top.get("title") or top.get("displayGroupName") or "",
                    }
            except Exception as exc:
                logger.warning("[BSR] Error fetching ASIN %s: %s", asin, exc)

    await asyncio.gather(*[_fetch_one(a) for a in target])
    _bsr_cache[key] = (now, result)
    return result


async def _refresh_flx_stock_bg(client, flx_skus: list, merge: bool = False) -> None:
    """
    Tarea BG: descarga FLX stock de FBA API y actualiza caché.
    Nunca bloquea requests — se lanza con asyncio.create_task().

    IMPORTANTE — Quirk Amazon Onsite (Seller Flex):
    La FBA Inventory API en modo batch NO devuelve items Onsite de forma confiable,
    aunque sí los devuelve en queries individuales por sellerSku.
    Por eso usamos dos fases:
      Fase 1: batch de 20 SKUs (captura FBA puro y algunos Onsite)
      Fase 2: retry individual para SKUs no retornados por el batch

    FEATURE 2026-08-24 (Jovan reportó "actualizando stock" sin fin visible,
    medido en vivo: 2,311 SKUs Onsite en VECKTOR tardó 20+ min -- Fase 2 es
    ~0.6-1s por SKU, límite real de rate-limit de Amazon, no evitable):
    - Progreso (`_flx_progress`) y resultado se escriben incrementalmente en
      BD (`amz_flx_stock_cache`) por lote/cada 25 SKUs -- un reinicio a media
      corrida (ej. un deploy) ya NO pierde todo el avance.
    - `merge=True`: parte del caché YA existente (memoria) y solo consulta
      `flx_skus` (ej. un puñado de SKUs nuevos detectados) en vez de
      re-escanear el catálogo completo -- usado por el trigger de "SKU
      nuevo no visto antes" para no disparar 20+ min de trabajo por 1 SKU.
    - Un SKU que falla la consulta (timeout/429/etc, no "Amazon confirmó 0")
      se marca is_error=True -- cuenta como "ya intentado" así no vuelve a
      disparar un re-escaneo completo en cada carga de página, pero
      tampoco se muestra como stock=0 falso (fulfillable=None).
    """
    key = client.seller_id
    from app.services import token_store as _ts_flx
    try:
        result: dict = dict(_flx_stock_cache.get(key, (0, {}))[1]) if merge else {}
        unique_skus = list(dict.fromkeys(s for s in flx_skus if s))
        total = len(unique_skus)
        _flx_progress[key] = (0, total)
        _pending_db_batch: list = []

        async def _flush_db():
            if _pending_db_batch:
                await _ts_flx.upsert_amz_flx_stock_batch(key, _pending_db_batch)
                _pending_db_batch.clear()

        # ── Fase 1: Batch queries ─────────────────────────────────────────────
        for i in range(0, len(unique_skus), 20):
            batch = unique_skus[i:i + 20]
            next_tok = None

            # Paginación dentro del batch — Amazon puede devolver nextToken
            for _page in range(10):  # máx 10 páginas por batch (safety)
                params = [
                    ("granularityType", "Marketplace"),
                    ("granularityId",   client.marketplace_id),
                    ("marketplaceIds",  client.marketplace_id),
                    ("details",         "true"),
                ]
                for sku in batch:
                    params.append(("sellerSkus", sku))
                if next_tok:
                    params.append(("nextToken", next_tok))

                try:
                    data    = await client._request("GET", "/fba/inventory/v1/summaries", params=params)
                    payload = data.get("payload", {}) or {}
                    for s in (payload.get("inventorySummaries") or []):
                        s_sku = s.get("sellerSku", "")
                        det   = s.get("inventoryDetails", {}) or {}
                        res   = det.get("reservedQuantity", {}) or {}
                        item  = {
                            "sku":         s_sku,
                            "fulfillable": int(det.get("fulfillableQuantity") or 0),
                            "reserved":    int(res.get("totalReservedQuantity") or 0),
                            "inbound":     (
                                int(det.get("inboundWorkingQuantity") or 0)
                                + int(det.get("inboundShippedQuantity") or 0)
                                + int(det.get("inboundReceivingQuantity") or 0)
                            ),
                            "total":       int(s.get("totalQuantity") or 0),
                        }
                        result[s_sku] = item
                        _pending_db_batch.append(item)
                    next_tok = payload.get("nextToken")
                    if not next_tok:
                        break  # sin más páginas
                    await asyncio.sleep(0.55)  # rate limit entre páginas

                except Exception as exc:
                    logger.warning(f"[FLX-BG] Error batch {i} pág {_page}: {exc}")
                    break  # pasar al siguiente batch

            _flx_progress[key] = (min(i + 20, total), total)
            await _flush_db()
            # Pausa entre batches: 0.55s = ~1.8 req/s (bajo el límite de 2 req/s)
            if i + 20 < len(unique_skus):
                await asyncio.sleep(0.55)

        # ── Fase 2: Retry individual para SKUs omitidos por el batch ─────────
        # Amazon Onsite (Seller Flex) no aparece de forma confiable en queries batch.
        # Los retentamos uno por uno para capturar su inventory real.
        missing_skus = [s for s in unique_skus if s not in result]
        if missing_skus:
            logger.info(
                f"[FLX-BG] Fase 2: {len(missing_skus)} SKUs omitidos por batch → retry individual"
            )
            _phase1_done = len(unique_skus) - len(missing_skus)
            for idx, sku in enumerate(missing_skus):
                try:
                    params = [
                        ("granularityType", "Marketplace"),
                        ("granularityId",   client.marketplace_id),
                        ("marketplaceIds",  client.marketplace_id),
                        ("details",         "true"),
                        ("sellerSkus",      sku),
                    ]
                    data    = await client._request("GET", "/fba/inventory/v1/summaries", params=params)
                    payload = data.get("payload", {}) or {}
                    summaries = payload.get("inventorySummaries") or []
                    if summaries:
                        for s in summaries:
                            s_sku = s.get("sellerSku", "")
                            if s_sku:
                                det = s.get("inventoryDetails", {}) or {}
                                res = det.get("reservedQuantity", {}) or {}
                                item = {
                                    "sku":         s_sku,
                                    "fulfillable": int(det.get("fulfillableQuantity") or 0),
                                    "reserved":    int(res.get("totalReservedQuantity") or 0),
                                    "inbound":     (
                                        int(det.get("inboundWorkingQuantity") or 0)
                                        + int(det.get("inboundShippedQuantity") or 0)
                                        + int(det.get("inboundReceivingQuantity") or 0)
                                    ),
                                    "total":       int(s.get("totalQuantity") or 0),
                                }
                                result[s_sku] = item
                                _pending_db_batch.append(item)
                    else:
                        # API confirma 0 para este SKU — guardamos para no re-consultar
                        item = {"sku": sku, "fulfillable": 0, "reserved": 0, "inbound": 0, "total": 0}
                        result[sku] = item
                        _pending_db_batch.append(item)
                    await asyncio.sleep(0.6)  # rate limit: ~1.6 req/s

                except Exception as exc:
                    logger.warning(f"[FLX-BG] Error retry individual {sku}: {exc}")
                    # FIX 2026-08-24: antes este SKU quedaba permanentemente ausente
                    # de `result` -- eso lo marcaba como "nuevo" en CADA carga de
                    # página futura, disparando un re-escaneo completo del catálogo
                    # una y otra vez por un solo fallo puntual. Se marca is_error
                    # (fulfillable=None, no un 0 falso) para que cuente como
                    # "ya intentado" -- se reintentará en el próximo ciclo normal
                    # (TTL de 5 min), no en cada page load.
                    item = {"sku": sku, "fulfillable": None, "reserved": None, "inbound": None, "total": None, "is_error": True}
                    result[sku] = item
                    _pending_db_batch.append(item)

                if (idx + 1) % 25 == 0 or idx == len(missing_skus) - 1:
                    _flx_progress[key] = (min(_phase1_done + idx + 1, total), total)
                    await _flush_db()

        await _flush_db()
        if not merge:
            # Solo marcar "ciclo completo" cuando de verdad se barrió TODO el
            # catálogo -- un refresh merge=True (unos pocos SKUs nuevos) no
            # debe hacer creer que el catálogo entero está fresco.
            await _ts_flx.mark_amz_flx_full_sync_done(key, len(unique_skus))

        # ── Resumen final ─────────────────────────────────────────────────────
        still_missing = [s for s in unique_skus if s not in result]
        with_stock    = [s for s, v in result.items() if (v.get("fulfillable") or 0) > 0]
        zero_skus     = [s for s, v in result.items() if v.get("fulfillable") == 0 and v.get("total") == 0]
        errored       = [s for s, v in result.items() if v.get("is_error")]
        logger.info(
            f"[FLX-BG] Completado: {len(result)}/{len(unique_skus)} SKUs en caché. "
            f"Con stock: {len(with_stock)}. "
            f"fulfillable=0: {len(zero_skus)}. "
            f"Con error (reintenta en {int(_FLX_STOCK_TTL/60)}min): {len(errored)}. "
            f"Sin datos: {len(still_missing)}."
        )
        if with_stock:
            logger.info(f"[FLX-BG] SKUs con stock Onsite: {with_stock}")
        # FIX 2026-08-24: en merge=True (solo unos SKUs nuevos) NO se debe
        # resetear el reloj de frescura del catálogo completo -- si no, un
        # puñado de SKUs nuevos podía posponer indefinidamente el próximo
        # refresh real de TODO el catálogo (TTL nunca se cumplía).
        _prev_ts = _flx_stock_cache.get(key, (0, {}))[0]
        _flx_stock_cache[key] = (_prev_ts if merge else _time.time(), result)
    finally:
        _flx_stock_refreshing.discard(key)
        _flx_progress.pop(key, None)


async def _warm_flx_cache_from_db_then_refresh(client, flx_skus: list) -> None:
    """FIX 2026-08-24 (Jovan reportó "actualizando stock" sin fin visible):
    al primer request tras un reinicio del proceso (deploy), el caché en
    memoria siempre estaba vacío y esto forzaba un escaneo completo contra
    Amazon desde cero -- 20+ min en catálogos grandes, aunque el dato en BD
    tuviera solo minutos de antigüedad. Ahora calienta primero desde BD
    (rápido, sin llamar a Amazon) y solo dispara el refresh en vivo si el
    dato persistido está vencido o no existe. `_flx_stock_refreshing[key]`
    ya viene marcado por el caller (_get_flx_stock_cached).

    FIX 2026-08-24 (2da vuelta -- confirmado en vivo: un deploy interrumpió
    un escaneo YA COMPLETO justo antes de su último paso, `mark_amz_flx_
    full_sync_done` nunca se ejecutó, así que `meta` quedaba None aunque
    `amz_flx_stock_cache` ya tuviera el 100% de los SKUs -- eso se leía
    como "sin fecha" (last_sync=0) y disparaba OTRO escaneo completo desde
    cero, un ciclo real). Ahora la decisión de re-escanear en vivo se basa
    en la COBERTURA real de datos persistidos, no solo en si existe una
    marca formal de "ciclo completo" -- si la BD ya cubre ~todo lo que se
    necesita, se usa tal cual (y se escribe la marca retroactivamente) en
    vez de volver a empezar.
    """
    key = client.seller_id
    from app.services import token_store as _ts_warm
    try:
        db_data = await _ts_warm.get_amz_flx_stock_from_db(key)
        meta = await _ts_warm.get_amz_flx_sync_meta(key)
        last_sync = meta["last_full_sync_at"] if meta else 0
        if db_data:
            _flx_stock_cache[key] = (last_sync, db_data)
            logger.info(f"[FLX] Calentado desde BD: {len(db_data)} SKUs (edad: {int(_time.time() - last_sync)}s)")

        unique_skus = list(dict.fromkeys(s for s in flx_skus if s))
        covered = sum(1 for s in unique_skus if s in db_data)
        coverage_ok = bool(unique_skus) and (covered / len(unique_skus)) >= 0.98

        if not db_data or (not coverage_ok and (_time.time() - last_sync) >= _FLX_STOCK_TTL):
            await _refresh_flx_stock_bg(client, flx_skus)  # limpia _flx_stock_refreshing en su finally
        else:
            if db_data and not meta:
                # Datos ya casi/completamente cubiertos pero sin marca formal
                # (ej. un deploy interrumpió el ciclo justo antes de escribirla)
                # -- se escribe ahora para no repetir este razonamiento en
                # cada carga de página futura.
                await _ts_warm.mark_amz_flx_full_sync_done(key, len(unique_skus))
            _flx_stock_refreshing.discard(key)
    except Exception as exc:
        logger.warning(f"[FLX] Error calentando desde BD: {exc}")
        _flx_stock_refreshing.discard(key)


def _get_flx_stock_cached(client, flx_skus: list) -> dict:
    """
    Stale-while-revalidate — NUNCA bloquea el request.

    • Cache fresco  → retorna datos inmediatamente.
    • Cache stale   → retorna datos stale + lanza refresh BG.
    • Sin cache     → retorna {} + calienta desde BD (rápido) y decide desde
      ahí si hace falta ir en vivo contra Amazon (ver
      _warm_flx_cache_from_db_then_refresh, FIX 2026-08-24).

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

    # Primera carga tras un reinicio: sin caché EN MEMORIA todavía (puede
    # que sí haya dato persistido en BD -- ver _warm_flx_cache_from_db_then_refresh).
    if key not in _flx_stock_refreshing:
        _flx_stock_refreshing.add(key)
        asyncio.create_task(_warm_flx_cache_from_db_then_refresh(client, flx_skus))
        logger.info(f"[FLX] Primera carga tras reinicio — calentando desde BD ({len(flx_skus)} FLX SKUs)")
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
# TAB 2: CATÁLOGO
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/catalog", response_class=HTMLResponse)
async def amazon_products_catalog(
    request: Request,
    status_filter: str = Query("all", description="all | active | inactive | suppressed"),
    sort_by:       str = Query("fba_stock", description="fba_stock | price | title | revenue30d"),
    sort_dir:      str = Query("desc", description="asc | desc"),
    seller_id:     Optional[str] = Query(None),
):
    """
    Catálogo completo de listings Amazon.

    Combina:
    - Listings Items API: SKU, ASIN, título, precio, estado
    - FBA Inventory: stock disponible, reservado, dañado, en camino
    - Issues del listing: alertas de calidad por Amazon
    - SKU Sales (30d): unidades y revenue por SKU desde caché

    Columnas: Imagen · Título/SKU · ASIN · Precio · FBA · Uds 30d · $ 30d ·
              Estado · Issues · Acciones
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return _render_no_account(request, "amazon_products_catalog.html")

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        fba_index = _build_fba_index(fba_summaries)

        # ── SKU Sales (non-blocking, from cache or trigger BG refresh) ─────
        sku_sales, sku_sales_loading = _get_sku_sales_cached(client)

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

            sale_data    = sku_sales.get(sku, {})
            units_30d    = int(sale_data.get("units", 0) or 0)
            revenue_30d  = round(float(sale_data.get("revenue", 0) or 0), 0)

            # ── Margen estimado usando costo BM (si está en caché) ─────────
            bm           = _bm_from_cache(sku)
            cost_usd     = bm.get("_bm_retail_ph") or bm.get("_bm_avg_cost") or 0
            FX           = 18.5  # MXN/USD approximate
            cost_mxn     = cost_usd * FX if cost_usd > 0 else 0
            amz_fee_rate = 0.18   # ~18% Amazon referral + FBA est.
            margin_pct: float | None = None
            if cost_mxn > 0 and price > 0:
                net = price - cost_mxn - price * amz_fee_rate
                margin_pct = round(net / price * 100, 1)

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
                "units_30d":     units_30d,
                "revenue_30d":   revenue_30d,
                "cost_mxn":      round(cost_mxn, 0) if cost_mxn > 0 else None,
                "margin_pct":    margin_pct,
                "amazon_url":    f"https://www.amazon.com.mx/dp/{asin}" if asin else "",
            })

        # ── Trigger BG BM refresh if cache is cold ─────────────────────────
        bm_needs_refresh = not any(_bm_from_cache(e["sku"]).get("_bm_retail_ph") for e in enriched[:5])
        if bm_needs_refresh and enriched:
            active = [e for e in enriched if e["status"] == "ACTIVE"][:50]
            asyncio.create_task(_refresh_bm_all_bg(
                [{"sku": e["sku"], "summaries": [{"asin": e.get("asin","")}]} for e in active]
            ))

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
        elif sort_by == "revenue30d":
            enriched.sort(key=lambda x: x["revenue_30d"], reverse=reverse)
        else:  # fba_stock (default)
            enriched.sort(key=lambda x: x["fba_stock"], reverse=reverse)

        ctx = {
            "listings":           enriched,
            "total":              len(enriched),
            "status_filter":      status_filter,
            "sort_by":            sort_by,
            "sort_dir":           sort_dir,
            "nickname":           client.nickname,
            "marketplace":        client.marketplace_name,
            "seller_id":          client.seller_id,
            "sku_sales_loading":  sku_sales_loading,
        }
        return _templates.TemplateResponse(request, "partials/amazon_products_catalog.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en catalog")
        return _render_error(request, "amazon_products_catalog.html", str(e))



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
            "buybox_results":      buybox_results,
            "bb_won":              bb_won,
            "bb_lost":             bb_lost,
            "solo":                solo,
            "total_analyzed":      len(buybox_results),
            "total_opportunity":   round(total_opportunity, 2),
            "nickname":            client.nickname,
            "marketplace":         client.marketplace_name,
        }
        return _templates.TemplateResponse(request, "partials/amazon_products_buybox.html", ctx)

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
        await _audit(request, "amz_price_update", sku, {"price": price})
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

    # Bullet points from attributes → bullet_point
    bullet_points: list[str] = []
    bp_attr = attributes.get("bullet_point", [])
    if isinstance(bp_attr, list):
        bullet_points = [bp.get("value", "") for bp in bp_attr if bp.get("value")]

    # Description from attributes → product_description
    description = ""
    desc_attr = attributes.get("product_description", [])
    if isinstance(desc_attr, list) and desc_attr:
        description = desc_attr[0].get("value", "")

    return {
        "sku": sku,
        "asin": asin,
        "title": title,
        "price": price,
        "qty": qty,
        "fulfillment_type": fulfillment_type,
        "product_type": listing.get("productType", ""),
        "bullet_points": bullet_points,
        "description": description,
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
    price         = body.get("price")
    title         = body.get("title")
    qty           = body.get("qty")
    bullet_points = body.get("bullet_points")   # list[str] | None
    description   = body.get("description")     # str | None

    if all(v is None for v in (price, title, qty, bullet_points, description)):
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

    if bullet_points is not None and isinstance(bullet_points, list):
        try:
            await client.update_listing_bullets(sku, bullet_points)
            results["bullet_points"] = "ok"
        except Exception as e:
            errors.append(f"Bullets: {e}")

    if description is not None and str(description).strip():
        try:
            await client.update_listing_description(sku, str(description).strip())
            results["description"] = "ok"
        except Exception as e:
            errors.append(f"Descripción: {e}")

    # Invalidar caché
    _listings_cache.pop(client.seller_id, None)
    _buybox_cache.pop(f"{client.seller_id}:{sku}", None)

    if results:
        await _audit(request, "amz_listing_update", sku, {"fields": list(results.keys())})

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

        # getOrderItems: 0.5 req/s, burst 30 — secuencial con 2s de espera
        # Procesar máx 150 órdenes (≈5 min); suficiente para Top 10 representativo
        _cap = 150
        for i, order in enumerate(valid_orders[:_cap]):
            await _fetch_items(order)
            if i < min(_cap, len(valid_orders)) - 1:
                await asyncio.sleep(2.0)

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




_BM_EMPTY = {"bm_mty": 0, "bm_cdmx": 0, "bm_tj": 0, "bm_avail": 0, "bm_reserved": 0,
             "_bm_retail_ph": 0, "_bm_avg_cost": 0}


def _bm_from_cache(sku: str) -> dict:
    """Lee BM data del caché sin hacer API calls. Retorna _BM_EMPTY si no está cacheado."""
    cached = _bm_amz_cache.get(sku.upper())
    if cached and (_time.time() - cached[0]) < _BM_AMZ_TTL:
        return cached[1]
    return _BM_EMPTY


async def _enrich_bm_amz(items: list, timeout_s: float | None = None) -> None:
    """
    Enriquece items in-place con datos de bm_sku_master (bm_mty, bm_cdmx, bm_tj, bm_avail, bm_reserved).

    - Deduplica por SKU base: SNFN000941-NEW-02 y SNFN000941-FLX01 → 1 sola lectura.
    - Caché 15 min por Amazon SKU (igual que antes, aunque ya no hay costo de
      red que ahorrar -- se conserva para no recalcular en cada render).
    - FIX 2026-08-20: antes hacía 3 llamadas EN VIVO a BM por SKU base
      (warehouse + stock + info); ahora es una sola consulta SQL a
      bm_sku_master para todos los bases a la vez -- cero llamadas a BM.
      timeout_s queda como parámetro sin efecto real (la consulta SQL es
      demasiado rápida para necesitarlo) -- se conserva por compatibilidad
      con los callers existentes, no se tocó su firma.
    """
    if not items:
        return

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

    # FIX 2026-08-20 (directiva de Jovan tras el bloqueo/incidente de BM del
    # mismo día): esto hacía 3 llamadas EN VIVO a BM por cada SKU base
    # (warehouse breakdown + stock CONCEPTID=1 + info CONCEPTID=8), disparado
    # por _refresh_bm_all_bg para el catálogo Amazon COMPLETO -- exactamente
    # el mismo patrón (mecanismo automático fuera del loop de categorías)
    # que ya se corrigió en app/main.py el mismo día. Ahora lee bm_sku_master
    # (ya lo mantiene fresco el loop de categorías) en UNA sola consulta SQL
    # para todos los SKUs base a la vez -- cero llamadas nuevas a BM.
    logger.info(f"[BM-AMZ] Consultando {len(base_to_amz_skus)} SKUs base (bm_sku_master): {list(base_to_amz_skus)}")
    from app.services import token_store as _ts_amz
    master_rows = await _ts_amz.get_bm_master_rows_for_skus(list(base_to_amz_skus))
    ts_now = _time.time()
    for base, amz_skus in base_to_amz_skus.items():
        row = master_rows.get(base)
        if row and row.get("verified"):
            inv = {
                "bm_mty": row.get("mty_qty", 0) or 0,
                "bm_cdmx": row.get("cdmx_qty", 0) or 0,
                "bm_tj": row.get("tj_qty", 0) or 0,
                "bm_avail": row.get("available_qty", 0) or 0,
                "bm_reserved": row.get("reserve_qty", 0) or 0,
                "_bm_retail_ph": row.get("retail_ph", 0) or 0,
                "_bm_avg_cost": row.get("cost_usd", 0) or 0,
            }
        else:
            inv = _BM_EMPTY
        for amz_sku in amz_skus:
            _bm_amz_cache[amz_sku.upper()] = (ts_now, inv)
            for item in sku_to_items.get(amz_sku, []):
                item.update(inv)


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
    if not listings:
        return  # Sin listings (catálogo aún cargando) — no iniciar BM prefetch
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

@router.get("/products/listings", response_class=HTMLResponse)
async def amazon_products_listings_shell(request: Request):
    """Shell estático — Listings ya no es tab propio, se fusionó aquí como
    subtab de Productos. Sin datos server-side: loadListingsTab() (JS) hace
    todo el fetch/render vía /api/amazon/listing-quality, igual que cuando
    vivía en /amazon?tab=listings — solo cambió dónde se monta el shell."""
    return _templates.TemplateResponse(request, "partials/amazon_products_listings.html", {})


@router.get("/products/deals", response_class=HTMLResponse)
async def amazon_products_deals_shell(request: Request):
    """Shell estático — Deals ya no es tab propio, se fusionó aquí como
    subtab de Productos. Sin datos server-side: loadDealsSection()/
    loadCompPricingSection()/loadDealCandidates() (JS) hacen todo el
    fetch/render, igual que cuando vivía en /amazon?tab=deals."""
    return _templates.TemplateResponse(request, "partials/amazon_products_deals.html", {})


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
        return _templates.TemplateResponse(request, "partials/amazon_products_resumen.html", ctx)

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
        # FIX 2026-08-24 (Jovan: "le doy a recargar y reinicia todo, es un
        # ciclo infinito?"): este botón es la "Recarga rápida" (~3 seg,
        # según su propio comentario original) -- pero también tronaba el
        # caché de FLX/Onsite, que NO es rápido de reconstruir (20-40 min,
        # consulta individual por SKU a Amazon). Cada click aquí destruía
        # el progreso ya hecho y forzaba un ciclo completo nuevo desde
        # cero. FLX ya tiene su propio mecanismo de refresco (TTL +
        # progreso persistido en BD) -- no se toca desde este botón.

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        sku_sales, sku_sales_loading = _get_sku_sales_cached(client)  # sync, nunca bloquea
        fba_index = _build_fba_index(fba_summaries)

        # FLX real-time: stale-while-revalidate — retorna caché inmediatamente (o {}) + BG refresh
        # Incluye: items con -FLX en SKU O con fulfillmentChannelCode AMAZON_NA
        flx_skus = [item.get("sku", "") for item in listings if _is_amz_onsite(item)]
        flx_stock_index = _get_flx_stock_cached(client, flx_skus)   # sync, nunca bloquea
        flx_loading     = client.seller_id in _flx_stock_refreshing  # True = BG activo

        # Edge case: listings nuevos que aún no están en el caché FLX vigente.
        # Si el caché existe pero no tiene un SKU FLX (listing creado después del último refresh),
        # forzar un refresh inmediato -- SOLO de los SKUs faltantes (merge=True), no de TODO
        # el catálogo. FIX 2026-08-24: antes re-escaneaba flx_skus completo (20+ min en
        # catálogos grandes) por solo 1-2 SKUs nuevos; además, un SKU que falla la consulta
        # ahora queda marcado is_error (ver _refresh_flx_stock_bg) así que ya NO vuelve a
        # aparecer aquí como "nuevo" en cada carga de página -- solo los genuinamente nuevos.
        if flx_stock_index and not flx_loading:
            new_flx = [s for s in flx_skus if s and s not in flx_stock_index]
            if new_flx:
                _key = client.seller_id
                _flx_stock_refreshing.add(_key)
                asyncio.create_task(_refresh_flx_stock_bg(client, new_flx, merge=True))
                flx_loading = True
                logger.info(f"[FLX] {len(new_flx)} SKUs nuevos detectados (no en caché) → refresh forzado (solo estos)")

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
            listing_fa   = item.get("fulfillmentAvailability", [])
            is_flx_item  = _is_amz_onsite(item)   # -FLX SKU ó AMAZON_NA channel
            stock_fba    = fba_stock_fba           # de la FBA Inventory API general
            stock_fbm    = 0                       # canal DEFAULT (merchant fulfilled)
            stock_flx    = 0                       # Seller Flex / Amazon Onsite

            for fa_entry in listing_fa:
                channel = (fa_entry.get("fulfillmentChannelCode") or "").upper()
                qty     = int(fa_entry.get("quantity") or 0)
                if channel == "DEFAULT":
                    stock_fbm = qty

            # Seller Flex / Amazon Onsite: stock real de FBA API (query por SKU específico).
            # Aplica a items con -FLX en SKU Y a items con fulfillmentChannelCode AMAZON_NA
            # (algunos productos usan Amazon Onsite sin sufijo -FLX en el nombre del SKU).
            flx_reserved = 0
            flx_inbound  = 0
            if is_flx_item:
                flx_data     = flx_stock_index.get(sku, {})
                stock_flx    = flx_data.get("fulfillable", 0)
                flx_reserved = flx_data.get("reserved", 0)
                flx_inbound  = flx_data.get("inbound", 0)
                # Fallback: si el per-SKU FBA API no devolvió datos pero el scan
                # general sí tiene el SKU (algunos items aparecen en ambos APIs)
                if stock_flx == 0 and not flx_data and fba_stock_fba > 0:
                    stock_flx = fba_stock_fba
                stock_fba = 0  # Onsite → columna FLX, no FBA

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
                fulfillment_type = "FLX" if is_flx_item else ("FBA" if bool(fba_d) else "FBM")

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
                "flx_reserved":  flx_reserved,
                "flx_inbound":   flx_inbound,
            })

        # ── Pre-enrich FLX con BM (solo para columnas BM Disp/Res/MTY/CDMX/TJ) ─
        # BM es inventario físico total en bodega — informativo ÚNICAMENTE.
        # NUNCA usar BM como sustituto del stock FLX/Onsite (son fuentes diferentes).
        # FLX = asignado al programa Amazon Onsite (dato de Amazon FBA API)
        # BM  = total bodega propia (dato de BinManager, puede incluir stock no asignado a Amazon)
        flx_pre = [e for e in enriched if e.get("fulfillment_type") == "FLX"]
        if flx_pre:
            await _enrich_bm_amz(flx_pre, timeout_s=8.0)

        # ── Filtrar ────────────────────────────────────────────────────────
        if filter == "fba":
            enriched = [e for e in enriched if e["stock_fba"] > 0 or e["is_fba"]]
        elif filter == "fbm":
            enriched = [e for e in enriched if e["stock_fbm"] > 0]
        elif filter == "flx":
            # Incluye -FLX en SKU Y items con fulfillment_type FLX (AMAZON_NA)
            enriched = [e for e in enriched if e["fulfillment_type"] == "FLX"]
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

        # ── BM: solo para la página actual — timeout 5s para no bloquear el request ──
        await _enrich_bm_amz(page_items, timeout_s=5.0)

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
                    _e["action_needed"] = "set_qty_zero"
                else:
                    _e["action_needed"] = "warn_fba_nobm"
                _e["add_qty"] = 0
            else:
                _e["action_needed"] = None
                _e["add_qty"]       = 0

        # Timestamp del caché de listings (para badge de frescura en la UI)
        cache_ts = int(_listings_cache.get(client.seller_id, (_time.time(), None))[0])

        ctx = {
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
            "flx_progress":      _flx_progress.get(client.seller_id),  # (procesados, total) o None -- FIX 2026-08-24
            "sku_sales_loading": sku_sales_loading,  # True = BG refresh de ventas activo
            "bm_loading":        bm_loading,         # True = BG pre-fetch BM activo
            "fba_loading":       client.seller_id in _fba_refreshing,   # True = FBA BG activo
            "listings_loading":  client.seller_id in _listings_loading,  # True = catálogo BG
        }
        return _templates.TemplateResponse(request, "partials/amazon_products_inventario.html", ctx)

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
    fba_active       = sid in _fba_refreshing
    listings_active  = sid in _listings_loading
    return JSONResponse({
        "ready":     not any([sku_sales_active, bm_active, flx_active, fba_active, listings_active]),
        "sku_sales": sku_sales_active,
        "bm":        bm_active,
        "flx":       flx_active,
        "fba":       fba_active,
        "listings":  listings_active,
    })


# ─────────────────────────────────────────────────────────────────────────────
# FLX DEBUG — consulta directa a FBA API para uno o varios SKUs
# GET /api/amazon/products/flx-debug?skus=SKU1,SKU2
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/flx-debug")
async def flx_debug(request: Request, skus: str = Query("", description="Comma-separated seller SKUs")):
    """
    Diagnóstico: consulta la FBA Inventory API directamente para los SKUs dados.
    Muestra la respuesta raw + lo que hay en caché.
    Solo para debug — no usa caché, siempre llama a Amazon.
    """
    client = await get_amazon_client()
    if not client:
        return JSONResponse({"error": "Sin cuenta Amazon"}, status_code=401)

    sku_list = [s.strip() for s in skus.split(",") if s.strip()]
    if not sku_list:
        return JSONResponse({"error": "Parámetro 'skus' requerido. Ej: ?skus=SNTV006829-FLX4,SNTV006138-FLX4"})

    # Lo que hay en caché ahora
    cached = _flx_stock_cache.get(client.seller_id)
    cache_age = None
    cache_hits = {}
    if cached:
        ts, data = cached
        cache_age = int(_time.time() - ts)
        cache_hits = {sku: data.get(sku) for sku in sku_list}

    # Consulta directa a la FBA API (sin caché)
    params = [
        ("granularityType", "Marketplace"),
        ("granularityId",   client.marketplace_id),
        ("marketplaceIds",  client.marketplace_id),
        ("details",         "true"),
    ]
    for sku in sku_list:
        params.append(("sellerSkus", sku))

    try:
        raw = await client._request("GET", "/fba/inventory/v1/summaries", params=params)
        summaries = raw.get("payload", {}).get("inventorySummaries", []) or []
        next_tok   = raw.get("payload", {}).get("nextToken")

        parsed = {}
        for s in summaries:
            det = s.get("inventoryDetails", {}) or {}
            res = det.get("reservedQuantity", {}) or {}
            sku = s.get("sellerSku", "")
            parsed[sku] = {
                "asin":               s.get("asin"),
                "fulfillable":        det.get("fulfillableQuantity"),
                "reserved_total":     res.get("totalReservedQuantity"),
                "reserved_fc":        res.get("fcProcessingQuantity"),
                "reserved_customer":  res.get("pendingCustomerOrderQuantity"),
                "inbound_working":    det.get("inboundWorkingQuantity"),
                "inbound_shipped":    det.get("inboundShippedQuantity"),
                "inbound_receiving":  det.get("inboundReceivingQuantity"),
                "total":              s.get("totalQuantity"),
                "condition":          s.get("condition"),
                "fulfillmentChannel": s.get("fulfillmentChannelCode") or s.get("inventoryType"),
            }

        # Diagnóstico claro por SKU consultado
        diagnosis = {}
        for sku in sku_list:
            if sku in parsed:
                d = parsed[sku]
                fulfillable = d["fulfillable"] or 0
                reserved    = d["reserved_total"] or 0
                inbound     = (d["inbound_working"] or 0) + (d["inbound_shipped"] or 0) + (d["inbound_receiving"] or 0)
                if fulfillable > 0:
                    status = f"✅ ENCONTRADO — fulfillable={fulfillable}, reservado={reserved}, inbound={inbound}"
                elif reserved > 0 or inbound > 0:
                    status = f"⚠️ ENCONTRADO pero fulfillable=0 — reservado={reserved}, inbound={inbound}"
                else:
                    status = "❌ ENCONTRADO en Amazon pero todas las cantidades = 0"
            else:
                status = "🚫 NO ENCONTRADO — Amazon FBA/Onsite API no reconoce este SKU"
            diagnosis[sku] = status

        return JSONResponse({
            "diagnosis":      diagnosis,
            "api_raw":        parsed,
            "has_next_page":  bool(next_tok),
            "cache_age_sec":  cache_age,
            "cache_hits":     cache_hits,
            "explanation": {
                "fulfillable":  "Unidades disponibles para enviar ahora (lo que muestra el dashboard en columna FLX)",
                "reserved":     "Unidades con orden activa pero aún no enviadas",
                "inbound":      "Unidades que Amazon está recibiendo/procesando",
                "NO_ENCONTRADO": "Amazon FBA/Onsite API no tiene registro de este SKU — puede ser un SKU puramente Seller Flex no sincronizado, o el SKU en Seller Central difiere del SKU en el listing",
            }
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc), "queried_skus": sku_list}, status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# ASIN INSPECT — diagnóstico completo de todas las fuentes para un ASIN
# GET /api/amazon/products/asin-inspect?asin=B0GR27VX4S
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/asin-inspect")
async def asin_inspect(request: Request, asin: str = Query(..., description="ASIN de Amazon")):
    """
    Consulta TODAS las fuentes de datos disponibles en SP-API para un ASIN dado.
    Fuentes consultadas:
      1. Cache de listings     — SKU, estado, fulfillmentAvailability, issues
      2. Listings API (directo) — datos completos del listing vía SP-API
      3. FBA Inventory API per-SKU — fulfillable, reserved, inbound (Seller Flex + FBA)
      4. FBA general scan cache — si aparece en el escaneo general
      5. FLX stock cache        — lo que el dashboard tiene almacenado
      6. Catalog API            — título, categoría, BSR, imágenes
      7. Listing Offers API     — buybox, precio competencia
      8. Ventas 30d (cache)     — unidades y revenue del período
    """
    client = await get_amazon_client()
    if not client:
        return JSONResponse({"error": "Sin cuenta Amazon"}, status_code=401)

    asin = asin.strip().upper()
    result = {"asin": asin, "sources": {}}

    # ── 1. Listings SP-API — carga real si caché vacío ───────────────────────
    # _get_listings_cached() descarga listings si el caché está frío.
    # Es bloqueante pero es un endpoint de diagnóstico — se puede esperar.
    all_listings = await _get_listings_cached(client)
    found_skus = []
    listing_from_cache = None
    for item in all_listings:
        summaries = item.get("summaries") or []
        item_asin  = next((s.get("asin") for s in summaries if s.get("asin")), None)
        if item_asin == asin:
            found_skus.append(item.get("sku", ""))
            listing_from_cache = item

    listings_cache_data = _listings_cache.get(client.seller_id)
    cache_age = int(_time.time() - listings_cache_data[0]) if listings_cache_data else 0
    result["sources"]["listings_sp_api"] = {
        "total_listings": len(all_listings),
        "cache_age_sec":  cache_age,
        "found_skus":     found_skus,
        "listing_raw":    listing_from_cache,
        "nota": (
            "ASIN encontrado en listings del seller" if found_skus
            else "ASIN NO encontrado entre los listings activos de este seller — puede no estar publicado"
        ),
    }

    # SKU principal para las consultas siguientes
    sku = found_skus[0] if found_skus else None
    result["sku_detectado"] = sku

    # ── 2. Listings API directo (SP-API) ──────────────────────────────────────
    if sku:
        try:
            listing_direct = await client.get_listing(sku)
            result["sources"]["listings_api_direct"] = listing_direct
        except Exception as e:
            result["sources"]["listings_api_direct"] = {"error": str(e)}
    else:
        result["sources"]["listings_api_direct"] = {"status": "OMITIDO — no se encontró SKU en caché"}

    # ── 3. FBA Inventory API por SKU (directo, sin caché) ─────────────────────
    if sku:
        try:
            params = [
                ("granularityType", "Marketplace"),
                ("granularityId",   client.marketplace_id),
                ("marketplaceIds",  client.marketplace_id),
                ("details",         "true"),
                ("sellerSkus",      sku),
            ]
            fba_raw = await client._request("GET", "/fba/inventory/v1/summaries", params=params)
            fba_summaries = (fba_raw.get("payload", {}) or {}).get("inventorySummaries", []) or []
            fba_parsed = []
            for s in fba_summaries:
                det = s.get("inventoryDetails", {}) or {}
                res = det.get("reservedQuantity",  {}) or {}
                fba_parsed.append({
                    "sellerSku":          s.get("sellerSku"),
                    "asin":               s.get("asin"),
                    "condition":          s.get("condition"),
                    "fulfillmentChannel": s.get("fulfillmentChannelCode") or s.get("inventoryType"),
                    "totalQuantity":      s.get("totalQuantity"),
                    "fulfillable":        det.get("fulfillableQuantity"),
                    "reserved_total":     res.get("totalReservedQuantity"),
                    "reserved_fc":        res.get("fcProcessingQuantity"),
                    "reserved_customer":  res.get("pendingCustomerOrderQuantity"),
                    "inbound_working":    det.get("inboundWorkingQuantity"),
                    "inbound_shipped":    det.get("inboundShippedQuantity"),
                    "inbound_receiving":  det.get("inboundReceivingQuantity"),
                    "unfulfillable":      det.get("unfulfillableQuantity"),
                    "researching":        det.get("researchingQuantity"),
                })
            result["sources"]["fba_inventory_per_sku"] = {
                "found": bool(fba_parsed),
                "items": fba_parsed,
                "nextToken": (fba_raw.get("payload", {}) or {}).get("nextToken"),
            }
        except Exception as e:
            result["sources"]["fba_inventory_per_sku"] = {"error": str(e)}
    else:
        result["sources"]["fba_inventory_per_sku"] = {"status": "OMITIDO — no se encontró SKU"}

    # ── 4. FBA general scan cache (escaneo sin filtro de SKU) ─────────────────
    fba_cache_data = _fba_cache.get(client.seller_id)
    if fba_cache_data:
        ts, fba_all = fba_cache_data
        in_general_scan = [s for s in fba_all if s.get("asin") == asin or s.get("sellerSku") == sku]
        result["sources"]["fba_general_scan_cache"] = {
            "age_sec":       int(_time.time() - ts),
            "found":         bool(in_general_scan),
            "items":         in_general_scan,
        }
    else:
        result["sources"]["fba_general_scan_cache"] = {"status": "VACÍO — no hay caché FBA general"}

    # ── 5. FLX stock cache (per-SKU, fondo de pantalla) ───────────────────────
    flx_cache_data = _flx_stock_cache.get(client.seller_id)
    if flx_cache_data and sku:
        ts, flx_data = flx_cache_data
        flx_hit = flx_data.get(sku)
        result["sources"]["flx_stock_cache"] = {
            "age_sec": int(_time.time() - ts),
            "found":   bool(flx_hit),
            "data":    flx_hit,
        }
    else:
        result["sources"]["flx_stock_cache"] = {"status": "VACÍO o SKU no encontrado en caché FLX"}

    # ── 6. Catalog API ────────────────────────────────────────────────────────
    try:
        catalog = await client.get_catalog_item(asin)
        if catalog:
            summaries_cat = catalog.get("summaries", [])
            sales_ranks   = catalog.get("salesRanks",  [])
            images_cat    = catalog.get("images",       [])
            cat_summary = summaries_cat[0] if summaries_cat else {}
            result["sources"]["catalog_api"] = {
                "itemName":    cat_summary.get("itemName"),
                "brand":       cat_summary.get("brand"),
                "manufacturer":cat_summary.get("manufacturer"),
                "modelNumber": cat_summary.get("modelNumber"),
                "color":       cat_summary.get("color"),
                "itemClassificationSalesRank": cat_summary.get("itemClassificationSalesRank"),
                "salesRanks":  sales_ranks[:3],
                "images_count":len(images_cat[0].get("images", []) if images_cat else []),
                "raw_summaries": summaries_cat[:1],
            }
        else:
            result["sources"]["catalog_api"] = {"status": "Sin datos — ASIN no en catálogo MX"}
    except Exception as e:
        result["sources"]["catalog_api"] = {"error": str(e)}

    # ── 7. Listing Offers API (buybox) ────────────────────────────────────────
    if sku:
        try:
            offers = await client.get_listing_offers(sku)
            if offers:
                payload_off = offers.get("payload", {}) or {}
                summary_off = payload_off.get("Summary", {}) or {}
                offers_list = payload_off.get("Offers", []) or []
                result["sources"]["listing_offers"] = {
                    "totalOfferCount":  summary_off.get("TotalOfferCount"),
                    "buyBoxPrices":     summary_off.get("BuyBoxPrices"),
                    "buyBoxEligibleOffers": summary_off.get("BuyBoxEligibleOffers"),
                    "lowestPrices":     summary_off.get("LowestPrices"),
                    "myOffer": next(
                        (o for o in offers_list if o.get("IsBuyBoxWinner") or o.get("MyOffer")),
                        None
                    ),
                    "isBuyBoxWinner": any(o.get("IsBuyBoxWinner") for o in offers_list),
                }
            else:
                result["sources"]["listing_offers"] = {"status": "Sin datos"}
        except Exception as e:
            result["sources"]["listing_offers"] = {"error": str(e)}
    else:
        result["sources"]["listing_offers"] = {"status": "OMITIDO — no se encontró SKU"}

    # ── 8. Ventas 30d (cache local) ───────────────────────────────────────────
    sku_sales_cache_data = _sku_sales_cache.get(client.seller_id)
    if sku_sales_cache_data and sku:
        ts, sales_data = sku_sales_cache_data
        sales_hit = sales_data.get(sku)
        result["sources"]["sku_sales_30d_cache"] = {
            "age_sec": int(_time.time() - ts),
            "found":   bool(sales_hit),
            "data":    sales_hit,
        }
    else:
        result["sources"]["sku_sales_30d_cache"] = {"status": "VACÍO o SKU no encontrado"}

    # ── Resumen ejecutivo ─────────────────────────────────────────────────────
    fba_items    = result["sources"].get("fba_inventory_per_sku", {}).get("items", [])
    fulfillable  = sum(i.get("fulfillable") or 0 for i in fba_items)
    reserved_tot = sum(i.get("reserved_total") or 0 for i in fba_items)
    inbound_tot  = sum((i.get("inbound_working") or 0) + (i.get("inbound_shipped") or 0) + (i.get("inbound_receiving") or 0) for i in fba_items)
    listing_status = None
    if listing_from_cache:
        sums = listing_from_cache.get("summaries") or []
        listing_status = next((s.get("status") for s in sums), None)
    fa_list = listing_from_cache.get("fulfillmentAvailability", []) if listing_from_cache else []
    fa_channels = {fa.get("fulfillmentChannelCode"): fa.get("quantity") for fa in fa_list} if fa_list else {}

    result["resumen"] = {
        "sku":             sku,
        "listing_status":  listing_status,
        "fulfillmentChannels": fa_channels,
        "fba_api_found":   bool(fba_items),
        "fba_fulfillable": fulfillable,
        "fba_reserved":    reserved_tot,
        "fba_inbound":     inbound_tot,
        "in_general_scan": result["sources"].get("fba_general_scan_cache", {}).get("found", False),
        "flx_cache_data":  result["sources"].get("flx_stock_cache", {}).get("data"),
    }

    return JSONResponse(result)


# ─────────────────────────────────────────────────────────────────────────────
# STOCK ACTION — actualiza qty de un listing FBM desde el tab Inventario
# ─────────────────────────────────────────────────────────────────────────────

class StockActionBody(BaseModel):
    action:   str        # "add_fbm" | "set_qty_zero"
    quantity: int = 0


@router.post("/products/{sku}/stock-action")
async def amazon_stock_action(sku: str, body: StockActionBody, request: Request):
    """Legacy — mantiene compatibilidad con botones existentes."""
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon")
    qty = body.quantity if body.action == "add_fbm" else 0
    try:
        result = await client.update_listing_quantity(sku, qty)
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    _listings_cache.pop(client.seller_id, None)
    _fba_cache.pop(client.seller_id, None)
    return {"ok": True, "sku": sku, "action": body.action, "quantity": qty}


# ─────────────────────────────────────────────────────────────────────────────
# FULFILLMENT ACTION — gestión universal FBA / FBM / FLX
# ─────────────────────────────────────────────────────────────────────────────

_VALID_FA = {"pause", "set_qty_zero", "set_merchant", "set_qty", "reactivate_fba"}


class FulfillmentActionBody(BaseModel):
    action:   str       # set_qty_zero | set_merchant | set_qty | reactivate_fba
    quantity: int = 0


@router.post("/products/{sku}/fulfillment-action")
async def amazon_fulfillment_action(sku: str, body: FulfillmentActionBody, request: Request):
    """
    Gestión universal de fulfillment para cualquier tipo de listing (FBA / FBM / FLX).

    Acciones:
      set_qty_zero   → FBM qty=0. Deja de vender sin eliminar listing ni perder ranking.
      set_merchant   → Convierte a FBM con quantity indicada (útil FBA/FLX → FBM).
      set_qty        → Actualiza qty en un listing ya FBM.
      reactivate_fba → Devuelve a FBA (AMAZON_NA). Amazon retoma control del stock.

    SP-API: PATCH /listings/2021-08-01/items/{sellerId}/{sku}
    """
    if body.action not in _VALID_FA:
        raise HTTPException(status_code=400, detail=f"Acción inválida. Válidas: {_VALID_FA}")
    if body.action in ("set_merchant", "set_qty") and body.quantity < 0:
        raise HTTPException(status_code=400, detail="quantity debe ser >= 0")

    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon")

    try:
        await client.update_listing_fulfillment(sku, body.action, body.quantity)
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as exc:
        logger.exception("[FulfillmentAction] SKU=%s action=%s", sku, body.action)
        raise HTTPException(status_code=500, detail=str(exc))

    # Invalidar caches
    _listings_cache.pop(client.seller_id, None)
    _fba_cache.pop(client.seller_id, None)
    _flx_stock_cache.pop(sku, None)

    labels = {
        "pause":          "qty=0 (sin stock)",
        "set_qty_zero":   "qty=0 (sin stock)",
        "set_merchant":   f"→ Merchant ({body.quantity} uds)",
        "set_qty":        f"Stock → {body.quantity} uds",
        "reactivate_fba": "Reactivado en FBA",
    }
    logger.info("[FulfillmentAction] SKU=%s → %s", sku, labels[body.action])
    return {"ok": True, "sku": sku, "action": body.action, "label": labels[body.action], "quantity": body.quantity}


@router.get("/products/stock", response_class=HTMLResponse)
async def amazon_products_stock_alerts(request: Request, warehouse: str = Query("MTY", description="Almacén para las sugerencias de alta/baja: MTY o CDMX")):
    """
    Alertas de stock Amazon — 7 categorías BM-correlacionadas (igual que ML)
    + 1 categoría informativa/de auditoría:
    - Sin Stock FBA: FBA=0, BM=0 también
    - Reabastecer:   FBA=0, BM>0 → enviar a FBA
    - Riesgo Sobreventa: cantidad PUBLICADA en el listing FBM > BM disponible
      → sobreventa real y accionable (solo aplica a FBM, el stock que
      controlamos nosotros — Amazon controla el stock FBA/FLX, no nosotros)
    - Stock Bajo:    FBA 1–10 uds
    - Restock Urgente: <14 días supply según velocidad
    - Stock Crítico BM: BM < 10 uds
    - Estancado:     BM>0, 0 ventas 30d, FBA>0
    - Discrepancia BM vs FBA (informativa, NO es riesgo): stock físico en FBA/FLX
      > BM disponible. Es el resultado NORMAL de haber enviado stock a FBA
      (deja de estar en BM) — sirve solo para auditar si BM decrementó bien
      al momento del envío, no requiere ninguna acción.
    """
    client = await get_amazon_client()
    if not client:
        return _render_no_account(request, "amazon_products_stock.html")

    try:
        fba_summaries, listings = await asyncio.gather(
            _get_fba_cached(client),
            _get_listings_cached(client),
        )
        sku_sales, _sku_loading_stock = _get_sku_sales_cached(client)

        # Índice de listings por SKU
        listings_idx: dict = {}
        for item in listings:
            sku = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            summary_0 = summaries[0] if summaries else {}
            price = 0.0
            for offer in (item.get("offers") or []):
                price = float((offer.get("price") or {}).get("amount") or 0)
                if price > 0:
                    break
            listings_idx[sku] = {
                "title":   summary_0.get("itemName", sku)[:65],
                "asin":    summary_0.get("asin") or "",
                "status":  _listing_status(summaries),
                "price":   price,
                "channel": _fulfillment_channel_of(item),
                "qty":     _parse_fba_stock(item.get("fulfillmentAvailability", [])),
            }

        # Construir lista unificada para enriquecer con BM
        all_items: list = []
        for s in fba_summaries:
            sku      = s.get("sellerSku", "")
            asin     = s.get("asin", "")
            name     = s.get("productName", sku)[:65]
            details  = s.get("inventoryDetails", {})
            fulfillable = int(details.get("fulfillableQuantity") or 0)
            inbound = (
                int(details.get("inboundWorkingQuantity") or 0)
                + int(details.get("inboundShippedQuantity") or 0)
            )
            sales     = sku_sales.get(sku, {"units": 0, "revenue": 0.0})
            units_30d = sales["units"]
            vel_dia   = units_30d / 30.0
            listing   = listings_idx.get(sku, {"title": name, "asin": asin, "status": "ACTIVE", "price": 0.0, "channel": "FBA"})
            title     = listing["title"] or name
            l_asin    = listing["asin"] or asin
            sc_url    = (
                f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={l_asin}"
                if l_asin else "https://sellercentral.amazon.com.mx/inventory"
            )
            all_items.append({
                "sku":        sku,
                "asin":       l_asin,
                "title":      title,
                "fulfillable": fulfillable,
                "inbound":    inbound,
                "units_30d":  units_30d,
                "vel_dia":    round(vel_dia, 2) if vel_dia > 0 else None,
                "sc_url":     sc_url,
                "price":      listing.get("price", 0.0),
                "channel":    listing.get("channel", "FBA"),
            })

        # ── FIX 2026-08-22 (Onsite/Seller Flex): la FBA Inventory API NO conoce
        # el stock de Onsite (nunca entra a un centro de Amazon) -- confirmado
        # en vivo, ver memoria project_seller_flex_portal_and_qty_gap.md. Un SKU
        # Onsite con stock real simplemente NO aparece en fba_summaries (no es
        # que salga en 0), así que quedaba invisible en esta página -- ni
        # "Sin Stock" ni "Reabastecer", solo ausente. Se completa con el
        # snapshot real de seller_flex_stock (extraído del portal Seller Flex,
        # única fuente real para este stock) para los SKUs AMAZON_NA que
        # fba_summaries no reportó, y se usa como fallback si algún SKU sí
        # aparece mal en 0.
        _all_items_skus = {it["sku"] for it in all_items}
        _onsite_missing_skus = [
            _sku for _sku, _l in listings_idx.items()
            if _l.get("channel") == "FBA" and _sku not in _all_items_skus
        ]
        from app.services import token_store as _ts_sf
        _sf_lookup_skus = list(_all_items_skus | set(_onsite_missing_skus))
        _sf_map_raw = await _ts_sf.get_seller_flex_stock_for_skus(_sf_lookup_skus) if _sf_lookup_skus else {}
        # FIX 2026-08-22 (mismo bug de mezcla de cuentas encontrado y corregido
        # en amazon_products_seller_flex()/adjust-xlsx): el mismo texto de SKU
        # puede existir como listing separado de VECTOR y AUTOBOT -- filtrar
        # SIEMPRE por los nodos reales de ESTA cuenta antes de sumar.
        _my_nodes_alerts = {
            n for wh in ("MTY", "CDMX", "TJ")
            if (n := _NODE_BY_SELLER_WAREHOUSE.get((client.seller_id, wh)))
        }
        _sf_map = {
            sku: [row for row in rows if row.get("node") in _my_nodes_alerts]
            for sku, rows in _sf_map_raw.items()
        }

        def _sf_qty(sku: str) -> int:
            return sum(n.get("sellable_qty", 0) for n in _sf_map.get(sku, []))

        def _sf_by_warehouse(sku: str) -> dict:
            out = {"MTY": 0, "CDMX": 0, "TJ": 0}
            for n in _sf_map.get(sku, []):
                wh = n.get("warehouse")
                if wh in out:
                    out[wh] += n.get("sellable_qty", 0)
            return out

        for item in all_items:
            if item["fulfillable"] == 0:
                _sfq = _sf_qty(item["sku"])
                if _sfq > 0:
                    item["fulfillable"] = _sfq
                    item["fulfillable_source"] = "seller_flex"

        for _sku in _onsite_missing_skus:
            _sfq = _sf_qty(_sku)
            if _sfq == 0:
                continue  # sin dato real disponible -- no inventar, se omite (mismo criterio que antes: invisible en vez de falso)
            _l = listings_idx[_sku]
            _sales = sku_sales.get(_sku, {"units": 0, "revenue": 0.0})
            _units_30d = _sales["units"]
            _vel_dia = _units_30d / 30.0
            _asin = _l.get("asin") or ""
            _sc_url = (
                f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={_asin}"
                if _asin else "https://sellercentral.amazon.com.mx/inventory"
            )
            all_items.append({
                "sku": _sku, "asin": _asin, "title": _l.get("title") or _sku,
                "fulfillable": _sfq, "fulfillable_source": "seller_flex",
                "inbound": 0, "units_30d": _units_30d,
                "vel_dia": round(_vel_dia, 2) if _vel_dia > 0 else None,
                "sc_url": _sc_url, "price": _l.get("price", 0.0), "channel": "FBA",
            })

        # ── Lista aparte para Riesgo Sobreventa real (FBM) ───────────────────
        # Los SKU FBM normalmente NO aparecen en fba_summaries (no tienen stock
        # físico en un FC de Amazon), así que sin este bloque quedan totalmente
        # fuera del análisis de esta página. La comparación correcta para ellos
        # es qty PUBLICADA en el listing vs BM disponible — ese es el único
        # stock que controlamos y podemos corregir hoy vía API (set_qty).
        fbm_items: list = []
        for _sku, _listing in listings_idx.items():
            if _listing.get("channel") != "FBM":
                continue
            _sales     = sku_sales.get(_sku, {"units": 0, "revenue": 0.0})
            _units_30d = _sales["units"]
            _vel_dia   = _units_30d / 30.0
            _asin      = _listing.get("asin") or ""
            _sc_url    = (
                f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={_asin}"
                if _asin else "https://sellercentral.amazon.com.mx/inventory"
            )
            fbm_items.append({
                "sku":       _sku,
                "asin":      _asin,
                "title":     _listing.get("title") or _sku,
                "qty":       int(_listing.get("qty") or 0),
                "units_30d": _units_30d,
                "vel_dia":   round(_vel_dia, 2) if _vel_dia > 0 else None,
                "sc_url":    _sc_url,
                "price":     _listing.get("price", 0.0),
                "channel":   "FBM",
            })

        # Enriquecer con BM en UNA sola llamada para all_items+fbm_items (bm_avail,
        # bm_reserved, bm_mty, bm_cdmx, bm_tj, _bm_retail_ph) -- FBA y FBM son
        # listings disjuntos (un SKU no está en ambas listas a la vez), así que
        # separarlo en 2 llamadas solo duplicaba el timeout/latencia contra BM
        # sin reducir el trabajo real; BM ya está limitado a 1 sesión a la vez.
        await _enrich_bm_amz(all_items + fbm_items, timeout_s=8.0)

        # FEATURE 2026-08-22 (pedido explícito de Jovan): sugerencias de
        # alta/baja para el botón "Generar ajuste" -- mismo cálculo que en
        # amazon_products_seller_flex() (delta real contra seller_flex_stock,
        # nunca el bruto de BM), para el almacén seleccionado (mismo selector
        # MTY/CDMX que ya existe en la pestaña Seller Flex).
        _wh_alerts = (warehouse or "MTY").strip().upper()
        if _wh_alerts not in ("MTY", "CDMX", "TJ"):
            _wh_alerts = "MTY"
        for item in all_items:
            _sf_wh = _sf_by_warehouse(item["sku"])
            _bm_wh_alerts = item.get(f"bm_{_wh_alerts.lower()}") or 0
            _sf_wh_val = _sf_wh.get(_wh_alerts, 0)
            item["suggest_receive"] = max(0, _bm_wh_alerts - _sf_wh_val)
            item["suggest_remove"]  = min(_sf_wh_val, max(0, _sf_wh_val - _bm_wh_alerts))

        # ── Clasificar en 7 categorías de alerta + 1 informativa ─────────────
        sin_stock         = []
        reabastecer       = []
        riesgo_sobreventa = []
        stock_bajo        = []
        restock_urgente   = []
        stock_critico     = []
        estancado         = []
        discrepancia_bm_fba = []

        for item in all_items:
            fulfillable  = item["fulfillable"]
            bm_avail     = int(item.get("bm_avail") or 0)
            vel_v        = item.get("vel_dia") or 0
            units_30d    = item["units_30d"]

            # Sin Stock FBA / Reabastecer
            if fulfillable == 0:
                if bm_avail > 0:
                    reabastecer.append(item)
                else:
                    sin_stock.append(item)

            # Stock Bajo (1–10 uds FBA)
            elif 0 < fulfillable <= 10:
                item["dias_hasta_0"] = round(fulfillable / vel_v, 1) if vel_v > 0 else None
                item["recomendacion"] = (
                    f"Enviar ~{round(vel_v * 30)} uds/mes" if vel_v > 0
                    else "Reabastece FBA pronto"
                )
                stock_bajo.append(item)

            # Restock Urgente (< lead time real del SKU)
            # FIX 2026-08-22 (auditoría de alertas): 14 días fijos para TODO el
            # catálogo, incluidas TVs -- el lead time real de importación (ver
            # CLAUDE.md, aduanas/pedimento) es de 20-45 días. Usa el mismo dato
            # real ya validado del lado ML (_target_coverage_days_for_sku, 30d
            # TVs/14d resto), no un umbral nuevo inventado.
            elif vel_v > 0 and fulfillable > 10:
                dias_s = fulfillable / vel_v
                _lead_days = _target_coverage_days(item.get("sku", ""))
                if dias_s < _lead_days:
                    item["dias_supply"] = round(dias_s, 1)
                    item["lead_days"]   = _lead_days
                    item["sugeridas"]   = max(0, round(vel_v * 60) - fulfillable - item["inbound"])
                    restock_urgente.append(item)

            # Discrepancia BM vs FBA (informativa, NO es riesgo): fulfillable es
            # stock YA físico en Amazon, fuera del control de BM. Que sea mayor
            # que bm_avail es el resultado NORMAL de haber enviado a FBA (el
            # stock salió de BM), no un riesgo de sobreventa. Solo audita si BM
            # decrementó bien al momento del envío. Se filtra a canal FBA/FLX
            # (channel=="FBA" según _fulfillment_channel_of) — para FBM la
            # comparación real vive en Riesgo Sobreventa, más abajo.
            if item.get("channel") == "FBA" and fulfillable > 0 and fulfillable > bm_avail:
                item["gap_units"] = fulfillable - bm_avail
                item["bm_zero"] = (bm_avail == 0)
                discrepancia_bm_fba.append(item)

            # Stock Crítico BM: BM > 0 pero < 10
            # FIX 2026-08-22 (auditoría de alertas): sin "units_30d > 0", un SKU sin
            # ventas caía aquí ("cómpralo ya") Y en Estancado ("liquídalo") al mismo
            # tiempo -- misma contradicción ya corregida en ML el 2026-08-20
            # (main.py, lista `critical`), replicada aquí para paridad de plataformas.
            if 0 < bm_avail < 10 and units_30d > 0:
                stock_critico.append(item)

            # Estancado: BM>0, 0 ventas 30d, FBA>0
            if bm_avail > 0 and units_30d == 0 and fulfillable > 0:
                estancado.append(item)

        # SKU no en catálogo BM — FEATURE 2026-08-22 (auditoría de alertas): ML
        # ya tenía esta alerta (main.py, lista `no_bm_sku`), Amazon no tenía
        # equivalente -- violaba la regla de CLAUDE.md de implementar features
        # en ambas plataformas. Mismo catálogo BM cacheado (nunca llamada en
        # vivo a BM) y mismos prefijos que el lado ML.
        from app.services import token_store as _ts_nobm
        from app.services.sku_utils import normalize_to_bm_sku as _norm_bm_amz
        _BM_PREFIXES_AMZ = ("SN", "SHIL", "RMTC", "SHEL", "SHFL", "SHHP", "SHLB")
        try:
            _cat_rows_amz = await _ts_nobm.get_bm_catalog_all()
            _bm_catalog_skus_amz = {(r.get("sku") or "").upper() for r in _cat_rows_amz if r.get("sku")}
        except Exception:
            _bm_catalog_skus_amz = set()
        no_bm_sku: list = []
        if _bm_catalog_skus_amz:
            _seen_no_bm_amz: set = set()
            for item in all_items:
                _raw = (item.get("sku") or "").upper()
                if not _raw:
                    # Sin SKU en absoluto -- se reporta siempre (mismo criterio
                    # que el lado ML tras el fix del mismo día, ver main.py).
                    no_bm_sku.append(item)
                    continue
                if not any(_raw.startswith(_px) for _px in _BM_PREFIXES_AMZ):
                    continue
                _norm = _norm_bm_amz(_raw)
                if _norm and _norm not in _bm_catalog_skus_amz and _norm not in _seen_no_bm_amz:
                    _seen_no_bm_amz.add(_norm)
                    no_bm_sku.append(item)
        no_bm_sku.sort(key=lambda x: x["title"])

        # Riesgo Sobreventa real: qty PUBLICADA (FBM) > BM disponible. Único
        # caso donde la sobreventa es accionable — nosotros controlamos ese
        # stock (vía set_qty), a diferencia de FBA/FLX que controla Amazon.
        # Reactivar: listing FBM en 0 con stock BM real disponible -- FEATURE
        # 2026-08-22 (auditoría de alertas, gap #7: Amazon no tenía equivalente
        # de "Activar" de ML). `fbm_items` ya incluye listings inactivos (no
        # filtra por status), solo nadie los mostraba antes. Reutiliza el
        # mismo botón/mecanismo "Sync stock BM" (set_qty) ya existente --
        # cero escritura nueva, confirmado que ya reactiva el listing.
        reactivar: list = []
        for item in fbm_items:
            qty_pub  = item["qty"]
            bm_avail = int(item.get("bm_avail") or 0)
            if qty_pub == 0 and bm_avail > 0:
                item["gap_units"] = bm_avail
                reactivar.append(item)
            elif qty_pub > 0 and qty_pub > bm_avail:
                item["gap_units"] = qty_pub - bm_avail
                item["bm_zero"] = (bm_avail == 0)
                riesgo_sobreventa.append(item)
        reactivar.sort(key=lambda x: -(x.get("bm_avail") or 0))

        # Ordenar
        sin_stock.sort(key=lambda x: x["title"])
        reabastecer.sort(key=lambda x: -(x.get("bm_avail") or 0))
        riesgo_sobreventa.sort(key=lambda x: -(x.get("gap_units") or 0))
        stock_bajo.sort(key=lambda x: (x.get("dias_hasta_0") or 9999))
        restock_urgente.sort(key=lambda x: x.get("dias_supply", 9999))
        stock_critico.sort(key=lambda x: x.get("bm_avail") or 0)

        # Racha de Estancado — antigüedad real, mismo mecanismo ya usado en ML
        # (`stock_issue_streaks`, ver main.py) para paridad de plataformas.
        # FEATURE 2026-08-22 (auditoría de alertas).
        from app.services import token_store as _ts_stag
        await _ts_stag.sync_stock_issue_streak(
            client.seller_id, "stagnant_amz",
            {it.get("sku", ""): it.get("title", "") for it in estancado if it.get("sku")},
        )
        _stagnant_hours_amz = {
            r["sku"]: r["hours_active"]
            for r in await _ts_stag.get_drift_alerts(client.seller_id, "stagnant_amz", min_hours=0.0)
        }
        for _it in estancado:
            _it["_days_stagnant"] = round(_stagnant_hours_amz.get(_it.get("sku", ""), 0.0) / 24, 1)
        estancado.sort(key=lambda x: (x.get("_days_stagnant") or 0, (x.get("bm_avail") or 0)), reverse=True)

        discrepancia_bm_fba.sort(key=lambda x: -(x.get("gap_units") or 0))

        # Margen Real Insuficiente — FEATURE 2026-08-22 (auditoría de alertas,
        # gap pendiente desde 2026-08-20: ML ya tenía esta alerta, Amazon no).
        # Reutiliza EXACTAMENTE la fórmula ya validada y en uso real para
        # márgenes de Amazon (ver amazon_orders.py: _save_amazon_items_history_bg
        # y compute_real_fees) -- comisión de socio 7% (_PARTNER_COMMISSION_PCT)
        # + fee estimado 10% (mismo criterio que ya se usa ahí cuando no hay fee
        # real de Finances API) + meta de recuperación 80% TV / 60% resto contra
        # RetailPH real (_sku_retail_map). Nada de esto es una fórmula inventada.
        # NOTA: el margin_pct que se muestra en el tab "Catálogo" (línea ~795)
        # usa una fórmula distinta y menos precisa (FX fijo, sin comisión de
        # socio) -- fuera de alcance hoy, no se toca en este fix.
        price_risk: list = []
        try:
            from app.main import _sku_retail_map, _sku_cost_map, _PARTNER_COMMISSION_PCT, _RECOVERY_TARGET_TV, _RECOVERY_TARGET_OTHER
        except Exception:
            _sku_retail_map, _sku_cost_map, _PARTNER_COMMISSION_PCT = {}, {}, 0.07
            _RECOVERY_TARGET_TV, _RECOVERY_TARGET_OTHER = 80.0, 60.0
        for item in all_items:
            _price = item.get("price") or 0
            if _price <= 0:
                continue
            _sku_bm = _norm_bm_amz(item.get("sku", ""))
            _retail_mxn = _sku_retail_map.get(_sku_bm, 0) or 0
            _cost_mxn = _sku_cost_map.get(_sku_bm, 0) or 0
            _neto_socio = (_price - _price * 0.10) * (1 - _PARTNER_COMMISSION_PCT)
            _below_target = False
            if _retail_mxn > 0:
                _recup_pct = round(_neto_socio / _retail_mxn * 100, 1)
                _target_pct = _RECOVERY_TARGET_TV if _sku_bm.upper().startswith("SNTV") else _RECOVERY_TARGET_OTHER
                item["_recup_pct"] = _recup_pct
                item["_recup_target_pct"] = _target_pct
                _below_target = _recup_pct < _target_pct
            _neto_negative = _cost_mxn > 0 and (_neto_socio - _cost_mxn) < 0
            if _below_target or _neto_negative:
                item["_neto_socio"] = round(_neto_socio, 0)
                item["_neto_negative"] = _neto_negative
                price_risk.append(item)
        price_risk.sort(key=lambda x: x.get("_recup_pct") if x.get("_recup_pct") is not None else 0)

        # Dedup por SKU único — un mismo SKU puede caer en varias categorías
        # (ej. Stock Bajo + Riesgo Sobreventa a la vez) y antes se contaba
        # doble en "Total Alertas". Discrepancia BM vs FBA es informativa, NO
        # una alerta accionable, así que no cuenta hacia total_alertas.
        _alert_skus: set = set()
        for _lst in (sin_stock, reabastecer, riesgo_sobreventa, stock_bajo,
                     restock_urgente, stock_critico, estancado, no_bm_sku,
                     reactivar, price_risk):
            for _it in _lst:
                _alert_skus.add(_it["sku"])
        total_alertas = len(_alert_skus)

        ctx = {
            "sin_stock":           sin_stock,
            "reabastecer":         reabastecer,
            "riesgo_sobreventa":   riesgo_sobreventa,
            "stock_bajo":          stock_bajo,
            "restock_urgente":     restock_urgente,
            "stock_critico":       stock_critico,
            "estancado":           estancado,
            "discrepancia_bm_fba": discrepancia_bm_fba,
            "no_bm_sku":           no_bm_sku,
            "reactivar":           reactivar,
            "price_risk":          price_risk,
            "total_alertas":       total_alertas,
            "sin_stock_count":     len(sin_stock),
            "reabastecer_count":   len(reabastecer),
            "riesgo_count":        len(riesgo_sobreventa),
            "bajo_count":          len(stock_bajo),
            "urgente_count":       len(restock_urgente),
            "critico_count":       len(stock_critico),
            "estancado_count":     len(estancado),
            "discrepancia_count":  len(discrepancia_bm_fba),
            "no_bm_sku_count":     len(no_bm_sku),
            "reactivar_count":     len(reactivar),
            "price_risk_count":    len(price_risk),
            "nickname":            client.nickname,
            "marketplace":         client.marketplace_name,
            "warehouse":           _wh_alerts,
        }
        return _templates.TemplateResponse(request, "partials/amazon_products_stock.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Products] Error en stock-alerts")
        return _render_error(request, "amazon_products_stock.html", str(e))


@router.get("/products/sin-publicar", response_class=HTMLResponse)
async def amazon_products_sin_publicar(
    request: Request,
    seller_id: Optional[str] = Query(None),
    show_parents: bool = Query(False),
    bm_filter: str = Query("all"),   # all | with_stock | no_stock
):
    """
    Listings no activos: Inactivos y Suprimidos.
    SIEMPRE filtrado por la cuenta activa (seller_id).
    Lee de amazon_listings DB — respuesta <100ms.
    """
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        return _render_no_account(request, "amazon_products_sin_publicar.html")

    try:
        import aiosqlite as _aio
        from app.services.token_store import DATABASE_PATH as _DB

        suprimidos     = []
        inactivos      = []
        db_total       = 0
        synced_at      = None
        sup_total      = 0
        inac_total     = 0
        _PER_PAGE      = 20
        sup_page       = int(request.query_params.get("sup_page", 1))
        inac_page      = int(request.query_params.get("inac_page", 1))

        async with _aio.connect(_DB) as _db:
            _db.row_factory = _aio.Row

            _tot = await (await _db.execute(
                "SELECT COUNT(*) FROM amazon_listings WHERE seller_id=?", (client.seller_id,)
            )).fetchone()
            db_total = _tot[0] if _tot else 0

            if db_total > 0:
                _ts_row = await (await _db.execute(
                    "SELECT MAX(synced_at) FROM amazon_listings WHERE seller_id=?", (client.seller_id,)
                )).fetchone()
                if _ts_row and _ts_row[0]:
                    synced_at = datetime.fromtimestamp(_ts_row[0]).strftime("%Y-%m-%d %H:%M")

                # Counts for pagination
                _cnt_rows = await (await _db.execute(
                    """SELECT status, COUNT(*) as cnt FROM amazon_listings
                       WHERE seller_id=? AND UPPER(status) != 'ACTIVE'
                       GROUP BY UPPER(status)""", (client.seller_id,)
                )).fetchall()
                for _cr in _cnt_rows:
                    if (_cr["status"] or "").upper() in ("INCOMPLETE", "SUPPRESSED"):
                        sup_total += _cr["cnt"]
                    else:
                        inac_total += _cr["cnt"]

                # Filter parents by default (show_parents=True to include them)
                _parent_filter = "" if show_parents else "AND (al.is_parent IS NULL OR al.is_parent = 0)"

                # Enhanced query: includes price, qty, bm_price
                # BM stock leído directo de bm_sku_master -- el mismo maestro
                # ya corregido para excluir Tijuana (ver
                # project_bm_tijuana_exclusion.md). Antes leía de ml_listings
                # (SUM de available_qty de publicaciones ML activas) como
                # "proxy" del stock BM -- Jovan señaló correctamente 2026-08-06
                # que Amazon no tiene por qué depender de qué esté sincronizado
                # en Mercado Libre; si un SKU nunca se publicó en ML, o su
                # publicación ML estaba desactualizada, el número mostrado acá
                # era ajeno a la realidad de BM.
                _base_q = f"""
                    SELECT al.sku, al.asin, al.title, al.status,
                           al.price, al.available_qty, al.synced_at,
                           COALESCE(bsm.retail_ph, 0) as bm_price,
                           COALESCE(al.is_parent, 0) as is_parent,
                           COALESCE(bsm.available_qty, 0) as bm_stock
                    FROM amazon_listings al
                    LEFT JOIN bm_sku_master bsm ON bsm.sku = al.base_sku
                    WHERE al.seller_id=? AND UPPER(al.status) = ? {_parent_filter}
                    ORDER BY al.title LIMIT ? OFFSET ?"""

                def _build_items(rows):
                    items = []
                    for _r in rows:
                        _asin = _r["asin"] or ""
                        _status = (_r["status"] or "INACTIVE").upper()
                        items.append({
                            "sku":   _r["sku"],
                            "asin":  _asin,
                            "title": (_r["title"] or _r["sku"])[:70],
                            "status": _status,
                            "price": _r["price"] or 0,
                            "qty":      _r["available_qty"] or 0,   # Amazon QTY
                            "bm_stock": _r["bm_stock"] or 0,       # BM stock real (bm_sku_master)
                            "bm_price": _r["bm_price"] or 0,
                            "issues": [],
                            "sc_url": (
                                f"https://sellercentral.amazon.com/inventory?searchField=ASIN&searchValue={_asin}"
                                if _asin else "https://sellercentral.amazon.com/inventory"
                            ),
                        })
                    return items

                # bm_filter: all | with_stock | no_stock
                _bm_having = {
                    'with_stock': 'AND COALESCE(bsm2.available_qty,0) > 0',
                    'no_stock':   'AND COALESCE(bsm2.available_qty,0) = 0',
                }.get(bm_filter, '')

                for _status_filter in ("SUPPRESSED", "INCOMPLETE"):
                    _rows = await (await _db.execute(
                        _base_q, (client.seller_id, _status_filter, _PER_PAGE, (sup_page-1)*_PER_PAGE)
                    )).fetchall()
                    suprimidos.extend(_build_items(_rows))

                _inac_q = f"""
                    SELECT al.sku, al.asin, al.title, al.status,
                           al.price, al.available_qty, al.synced_at,
                           COALESCE(bsm2.retail_ph, 0) as bm_price,
                           COALESCE(al.is_parent, 0) as is_parent,
                           COALESCE(bsm2.available_qty, 0) as bm_stock
                    FROM amazon_listings al
                    LEFT JOIN bm_sku_master bsm2 ON bsm2.sku = al.base_sku
                    WHERE al.seller_id=?
                      AND UPPER(al.status) NOT IN ('ACTIVE','SUPPRESSED','INCOMPLETE')
                      {_parent_filter} {_bm_having}"""

                # Count with/without BM stock for filter pills
                _cws = await (await _db.execute(
                    f"""SELECT COUNT(*) FROM amazon_listings al
                        LEFT JOIN bm_sku_master bsm3 ON bsm3.sku = al.base_sku
                        WHERE al.seller_id=? AND UPPER(al.status) NOT IN ('ACTIVE','SUPPRESSED','INCOMPLETE') {_parent_filter}
                        AND COALESCE(bsm3.available_qty,0) > 0""", (client.seller_id,)
                )).fetchone()
                inac_with_stock = _cws[0] if _cws else 0

                _inac_rows = await (await _db.execute(
                    _inac_q + " ORDER BY bm_stock DESC, al.title LIMIT ? OFFSET ?",
                    (client.seller_id, _PER_PAGE, (inac_page-1)*_PER_PAGE)
                )).fetchall()
                inactivos = _build_items(_inac_rows)

        # Count parents for display
        parents_count = 0
        try:
            _pc = await (await _db.execute(
                "SELECT COUNT(*) FROM amazon_listings WHERE seller_id=? AND is_parent=1",
                (client.seller_id,)
            )).fetchone()
            parents_count = _pc[0] if _pc else 0
        except Exception:
            pass

        # Candidatos a eliminar + historial — wrapped to not break on error
        cand_days = int(request.query_params.get("days", 365))
        cand_page = int(request.query_params.get("page", 1))
        candidatos_data = {"items": [], "total": 0, "page": 1, "pages": 1, "per_page": 10, "days": cand_days}
        historial       = []
        try:
            from app.services.token_store import get_deletion_candidates, get_listing_actions
            candidatos_data = await get_deletion_candidates(
                client.seller_id, days_no_sale=cand_days, page=cand_page, per_page=10
            )
            historial = await get_listing_actions(client.seller_id, limit=50)
        except Exception as _e2:
            logger.warning(f"[Inactivos] candidatos/historial error: {_e2}")

        ctx = {
            "suprimidos":  suprimidos,
            "sup_total":   sup_total,
            "sup_page":    sup_page,
            "sup_pages":   max(1, (sup_total + _PER_PAGE - 1) // _PER_PAGE),
            "inactivos":        inactivos,
            "inac_total":       inac_total,
            "inac_page":        inac_page,
            "inac_pages":       max(1, (inac_total + _PER_PAGE - 1) // _PER_PAGE),
            "inac_with_stock":  inac_with_stock if "inac_with_stock" in dir() else 0,
            "bm_filter":        bm_filter,
            "con_issues":  [],
            "candidatos":  candidatos_data.get("items", []),
            "cand_total":  candidatos_data.get("total", 0),
            "cand_page":   candidatos_data.get("page", 1),
            "cand_pages":  candidatos_data.get("pages", 1),
            "cand_days":   candidatos_data.get("days", 365),
            "historial":   historial,
            "db_total":    db_total,
            "synced_at":   synced_at,
            "nickname":    client.nickname,
            "marketplace": client.marketplace_name,
            "seller_id":     client.seller_id,
            "show_parents":  show_parents,
            "parents_count": parents_count,
            "is_admin":      bool((getattr(request.state, "dashboard_user", None) or {}).get("role") == "admin"),
        }
        return _templates.TemplateResponse(request, "partials/amazon_products_sin_publicar.html", ctx)

    except Exception as e:
        logger.exception(f"[Amazon Products] Error en sin-publicar: {type(e).__name__}: {e}")
        return _render_error(request, "amazon_products_sin_publicar.html", str(e))


# ── Candidatos a Eliminar ────────────────────────────────────────────────────

@router.post("/products/detect-parents")
async def detect_parents_endpoint(
    request: Request,
    seller_id: Optional[str] = Query(None),
    use_catalog: bool = Query(False),
):
    """
    Runs parent ASIN detection for a seller's listings.
    Heuristic: price=0 AND qty=0 AND status INACTIVE/SUPPRESSED.
    use_catalog=true: also verifies via Amazon Catalog Items API (slower).
    """
    from app.services.token_store import detect_and_mark_parents
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    result = await detect_and_mark_parents(client.seller_id, use_catalog_api=use_catalog)
    return result


@router.get("/products/sin-publicar-debug")
async def debug_sin_publicar(request: Request):
    """Debug — render template with real data and return error if it fails."""
    import traceback as _tb
    client = await get_amazon_client()
    if not client:
        return JSONResponse({"error": "no_account"})
    try:
        from app.services.token_store import get_deletion_candidates, get_listing_actions
        candidatos = await get_deletion_candidates(client.seller_id, days_no_sale=365)
        historial  = await get_listing_actions(client.seller_id, limit=50)
        ctx = {
            "suprimidos": [], "inactivos": [], "con_issues": [],
            "candidatos": candidatos, "historial": historial,
            "db_total": 100, "synced_at": "2026-06-05 10:00",
            "nickname": client.nickname, "marketplace": client.marketplace_name,
            "seller_id": client.seller_id, "error": None, "no_account": False,
        }
        try:
            resp = _templates.TemplateResponse(request, "partials/amazon_products_sin_publicar.html", ctx)
            return JSONResponse({"template_ok": True, "candidatos": len(candidatos), "historial": len(historial)})
        except Exception as te:
            return JSONResponse({"template_error": str(te), "tb": _tb.format_exc()})
    except Exception as e:
        return JSONResponse({"fatal_error": str(e), "tb": _tb.format_exc()})


@router.get("/products/candidatos-eliminar")
async def get_candidatos_eliminar(
    request: Request,
    seller_id: Optional[str] = Query(None),
    days: int = 365,
):
    """Returns deletion candidates for the ACTIVE account only."""
    from app.services.token_store import get_deletion_candidates
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    candidates = await get_deletion_candidates(client.seller_id, days)
    return {"candidates": candidates, "days": days, "count": len(candidates)}


@router.post("/products/close-listing")
async def close_listing_endpoint(request: Request):
    """Set listing qty=0 (close without deleting)."""
    from app.services.token_store import save_listing_action
    body = await request.json()
    sku       = (body.get("sku") or "").strip()
    seller_id = (body.get("seller_id") or "").strip()
    reason    = (body.get("reason") or "Cerrado manualmente").strip()
    if not sku:
        return JSONResponse({"error": "sku requerido"}, status_code=400)
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    result = await client.close_listing(sku)
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, status_code=400)
    # Save to DB and update local listings table
    asin = (body.get("asin") or "")
    await save_listing_action(client.seller_id, sku, asin, "close", reason)
    async with __import__("aiosqlite").connect(__import__("app.services.token_store", fromlist=["DATABASE_PATH"]).DATABASE_PATH) as db:
        await db.execute("UPDATE amazon_listings SET available_qty=0 WHERE seller_id=? AND sku=?", (client.seller_id, sku))
        await db.commit()
    logger.info(f"[Amazon] Listing closed: {sku} ({client.seller_id})")
    return {"ok": True, "sku": sku, "action": "close"}


@router.post("/products/delete-listing")
async def delete_listing_endpoint(request: Request):
    """Permanently delete a listing from Amazon. Requiere admin — a diferencia
    de "Cerrar" (reversible, qty=0), esto borra el listing sin vuelta atrás y
    no tenía ningún control de rol pese a ser la acción más irreversible de
    toda esta vista."""
    from app.services.token_store import save_listing_action
    du = getattr(request.state, "dashboard_user", None) or {}
    if du.get("role") != "admin":
        return JSONResponse({"error": "Se requiere rol Administrador para eliminar permanentemente"}, status_code=403)
    body = await request.json()
    sku       = (body.get("sku") or "").strip()
    seller_id = (body.get("seller_id") or "").strip()
    reason    = (body.get("reason") or "Eliminado manualmente").strip()
    if not sku:
        return JSONResponse({"error": "sku requerido"}, status_code=400)
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    asin = (body.get("asin") or "")
    result = await client.delete_listing(sku)
    # Amazon DELETE returns 200 with no body on success
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, status_code=400)
    await save_listing_action(client.seller_id, sku, asin, "delete", reason)
    async with __import__("aiosqlite").connect(__import__("app.services.token_store", fromlist=["DATABASE_PATH"]).DATABASE_PATH) as db:
        await db.execute("DELETE FROM amazon_listings WHERE seller_id=? AND sku=?", (client.seller_id, sku))
        await db.commit()
    logger.info(f"[Amazon] Listing deleted: {sku} ({client.seller_id})")
    return {"ok": True, "sku": sku, "action": "delete"}


@router.get("/products/historial-acciones")
async def get_historial_acciones(request: Request, seller_id: str = ""):
    """Returns history of close/delete actions for a seller."""
    from app.services.token_store import get_listing_actions
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    actions = await get_listing_actions(client.seller_id)
    return {"actions": actions}


# ─────────────────────────────────────────────────────────────────────────────
# SELLER FLEX — inventario en bodega propia + generador CSV para carga en lote
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/seller-flex/flx-progress", response_class=JSONResponse)
async def amazon_products_seller_flex_flx_progress():
    """FEATURE 2026-08-24 (Jovan: "tengo que darle al botón de refrescar
    para que el número cambie, no cambia por sí solo, eso está mal"):
    endpoint chico para que el indicador "actualizando stock (X/Y)" se
    actualice solo, sin recargar toda la tabla (evita perder inputs de
    cantidad que alguien pueda estar llenando en otras filas mientras
    tanto)."""
    client = await get_amazon_client()
    if not client:
        return JSONResponse({"loading": False, "done": None, "total": None})
    loading = client.seller_id in _flx_stock_refreshing
    prog = _flx_progress.get(client.seller_id)
    return JSONResponse({
        "loading": loading,
        "done":    prog[0] if prog else None,
        "total":   prog[1] if prog else None,
    })


@router.get("/products/seller-flex", response_class=HTMLResponse)
async def amazon_products_seller_flex(
    request: Request,
    q:         str  = Query("", description="Búsqueda por SKU o título"),
    force:     bool = Query(False, description="Limpia caché antes de cargar"),
    warehouse: str  = Query("MTY", description="Almacén físico del archivo a preparar: MTY o CDMX"),
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
        # FIX 2026-08-24 (Jovan: "le doy a recargar y reinicia todo, es un
        # ciclo infinito?"): este botón es la "Recarga rápida" (~3 seg) --
        # pero también tronaba el caché de FLX/Onsite, que NO es rápido de
        # reconstruir (20-40 min, consulta individual por SKU a Amazon).
        # Cada click aquí destruía el progreso ya hecho y forzaba un ciclo
        # completo nuevo desde cero. FLX ya tiene su propio mecanismo de
        # refresco (TTL + progreso persistido en BD) -- no se toca desde
        # este botón.

    try:
        listings = await _get_listings_cached(client)

        # FLX stock real-time — stale-while-revalidate (no bloquea)
        # Incluye: -FLX en SKU Y items con fulfillmentChannelCode AMAZON_NA
        all_flx_skus = [item.get("sku", "") for item in listings if _is_amz_onsite(item)]
        flx_stock_index = _get_flx_stock_cached(client, all_flx_skus)  # sync, nunca bloquea

        # Filtrar solo items Seller Flex / Amazon Onsite
        flx_items = []
        for item in listings:
            sku = item.get("sku", "")
            if not _is_amz_onsite(item):
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
        await _enrich_bm_amz(flx_items, timeout_s=8.0)

        # FIX 2026-08-22 (pedido explícito de Jovan, tras confirmar que la FBA
        # Inventory API NO reporta el stock real de Onsite y que la sugerencia
        # de "cantidad a recibir" usaba bm_avail COMPLETO -- ignorando lo que
        # Seller Flex YA tenía registrado, causando sobre-recepción/doble
        # conteo real). Se reemplaza con el snapshot real de seller_flex_stock
        # (ver memoria project_seller_flex_portal_and_qty_gap.md) y la
        # cantidad sugerida ahora es el DELTA real contra ese almacén
        # específico, nunca el total bruto de BM.
        from app.services import token_store as _ts_sfx
        _wh = (warehouse or "MTY").strip().upper()
        if _wh not in ("MTY", "CDMX", "TJ"):
            _wh = "MTY"
        # FIX 2026-08-22 (bug real encontrado al agregar bin: SKUs con el
        # MISMO texto exacto existen como listings separados de VECTOR y
        # AUTOBOT en sus propios nodos -- sumar por "warehouse" a secas
        # mezclaba el stock de las 2 cuentas. Regla del proyecto: "SCOPE DE
        # CUENTA — NUNCA MEZCLAR". Se filtra SIEMPRE por los nodos reales de
        # ESTE seller_id, nunca por nombre de almacén a secas.
        _my_node_mty  = _NODE_BY_SELLER_WAREHOUSE.get((client.seller_id, "MTY"), "")
        _my_node_cdmx = _NODE_BY_SELLER_WAREHOUSE.get((client.seller_id, "CDMX"), "")
        _my_node_tj   = _NODE_BY_SELLER_WAREHOUSE.get((client.seller_id, "TJ"), "")
        _sfx_skus = [it["sku"] for it in flx_items if it.get("sku")]
        _sfx_map_raw = await _ts_sfx.get_seller_flex_stock_for_skus(_sfx_skus) if _sfx_skus else {}
        _my_nodes_set = {n for n in (_my_node_mty, _my_node_cdmx, _my_node_tj) if n}
        _sfx_map = {
            sku: [row for row in rows if row.get("node") in _my_nodes_set]
            for sku, rows in _sfx_map_raw.items()
        }
        for item in flx_items:
            _nodes = _sfx_map.get(item["sku"], [])
            item["sf_mty"]  = sum(n["sellable_qty"] for n in _nodes if n.get("node") == _my_node_mty)
            item["sf_cdmx"] = sum(n["sellable_qty"] for n in _nodes if n.get("node") == _my_node_cdmx)
            item["sf_tj"]   = sum(n["sellable_qty"] for n in _nodes if n.get("node") == _my_node_tj)
            item["sf_total"] = item["sf_mty"] + item["sf_cdmx"] + item["sf_tj"]
            item["sf_synced_at"] = max((n.get("synced_at", 0) for n in _nodes), default=0)
            # FIX 2026-08-22 (reportado por Jovan: mostraba 122 cuando lo real
            # era 20): la FBA Inventory API puede devolver un número viejo NO
            # CERO (no solo 0 en Onsite) -- confirmado en vivo, ver memoria.
            # Si YA tenemos un snapshot real de Seller Flex para este SKU
            # (aunque sea 0), se usa SIEMPRE sobre fba_stock -- es la fuente
            # verificada, fba_stock es el fallback solo para SKUs que nunca
            # se han escaneado todavía.
            if _nodes:
                item["fba_stock"] = item["sf_total"]
                item["fba_stock_source"] = "seller_flex"
            elif _flx_cache_valid(client.seller_id):
                # FEATURE 2026-08-22: fallback automático (reporte oficial
                # GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA, reactivado con
                # reintentos -- ver _onsite_periodic_sync_loop) para SKUs sin
                # snapshot manual todavía. No trae desglose MTY/CDMX (solo
                # total por cuenta), así que no llena sf_mty/sf_cdmx, solo
                # corrige el número visible en vez de dejar el de la FBA
                # Inventory API rota.
                _auto_avail, _auto_reserved = _flx_cache_read(client.seller_id, item["sku"])
                if _auto_avail > 0 or _auto_reserved > 0:
                    item["fba_stock"] = _auto_avail
                    item["flx_reserved"] = _auto_reserved
                    item["fba_stock_source"] = "onsite_report_auto"
            _bm_wh = item.get("bm_mty", 0) if _wh == "MTY" else item.get("bm_cdmx", 0)
            _sf_wh = item.get(f"sf_{_wh.lower()}", 0)
            item["suggest_receive"] = max(0, _bm_wh - _sf_wh)
            item["suggest_remove"]  = min(_sf_wh, max(0, _sf_wh - _bm_wh))
            item["listing_ok_for_receive"] = item["status"] == "ACTIVE"
            # Bin real donde está recibido (2026-08-22, pedido de Jovan) --
            # viene del snapshot GraphQL GetInventoryViewByBin, no existe un
            # catálogo fijo de bines en Seller Flex (ver memoria).
            _bin_match = next((n["bin"] for n in _nodes if n.get("warehouse") == _wh and n.get("bin")), "")
            item["sf_bin"] = _bin_match

        # Bines disponibles para el dropdown (2026-08-22, pedido de Jovan) --
        # Seller Flex no tiene catálogo fijo de bines, así que "disponibles"
        # = los que ya se han usado en el snapshot para el nodo de este
        # almacén/cuenta. Mapeo node<->almacén confirmado en vivo el
        # 2026-08-22 (ver memoria project_seller_flex_portal_and_qty_gap.md).
        _node_for_wh = _NODE_BY_SELLER_WAREHOUSE.get((client.seller_id, _wh), "")
        available_bins = await _ts_sfx.get_seller_flex_bins_for_node(_node_for_wh) if _node_for_wh else []

        # FLX loading state — para mostrar "···" en template si BG activo
        flx_loading = client.seller_id in _flx_stock_refreshing

        # Ordenar: primero los que tienen stock Amazon, luego por bm_avail desc
        flx_items.sort(key=lambda x: (
            0 if (x["status"] == "ACTIVE" and (x["fba_stock"] > 0 or x["bm_avail"] > 0)) else (1 if x["status"] == "ACTIVE" else 2),
            -(x["fba_stock"] + x["flx_reserved"]),
        ))

        ctx = {
            "items":       flx_items,
            "total":       len(flx_items),
            "q":           q,
            "flx_loading": flx_loading,
            "flx_progress": _flx_progress.get(client.seller_id),  # (procesados, total) o None -- FIX 2026-08-24
            "warehouse":   _wh,
            "available_bins": available_bins,
        }
        return _templates.TemplateResponse(request, "partials/amazon_products_seller_flex.html", ctx)

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


@router.post("/products/seller-flex/adjust-xlsx")
async def generate_seller_flex_adjust_xlsx(request: Request):
    """
    Genera el XLSX de "Ajuste en lote" (Ajustes -> Eliminar) para dar de baja
    stock que ya no existe -- mismo formato exacto que exige el portal Seller
    Flex (columnas ASIN/SKU/FNSKU, EXPIRY_DATE, MANUFACTURING_DATE,
    ADJUSTMENT_TYPE, SOURCE_BIN_ID, DESTINATION_BIN_ID, SOURCE_INVENTORY_TYPE,
    DESTINATION_INVENTORY_TYPE, QUANTITY -- ver memoria
    project_seller_flex_receive_adjust_mechanics.md).

    Manual oficial de Amazon (Seller Flex 2023, sección 4.2): "Al eliminar
    el inventario, elija Eliminar. No seleccione Perdido. Use Perdido solo
    cuando los artículos no estén dentro del contenedor/almacén." -- por eso
    ADJUSTMENT_TYPE es siempre "REMOVE " (baja contable), nunca "LOSE".

    FIX central 2026-08-22 (causa raíz real de los errores "Items not found
    for this disposition and quantity" que bodega ya sufría a diario):
    la cantidad a eliminar SIEMPRE se topa server-side contra
    seller_flex_stock (el snapshot real más reciente que tenemos), nunca
    contra lo que el usuario haya escrito en el formulario -- así el archivo
    generado nunca puede pedir eliminar más de lo que Seller Flex reporta
    que existe en este momento.

    FIX 2026-08-22 (b): el tope SIEMPRE se calcula solo contra el nodo real
    de ESTA cuenta+almacén -- el mismo texto de SKU puede existir como
    listing separado de VECKTOR y de AUTOBOT (ver
    _NODE_BY_SELLER_WAREHOUSE), sumar sin filtrar por cuenta permitiría un
    tope inflado con stock de OTRA cuenta (violaría "SCOPE DE CUENTA —
    NUNCA MEZCLAR" y volvería a generar el mismo error de cantidad).

    Body JSON: {"source_bin": "A1", "warehouse": "MTY", "items": [{"sku": "...", "quantity": N}]}
    """
    import io
    import openpyxl

    from app.services import token_store as _ts_sfx_adj

    body = await request.json()
    source_bin = (body.get("source_bin") or "A1").strip()
    items = body.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="Sin items para generar el archivo de ajuste")

    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=401, detail="Sin cuenta Amazon conectada")
    _wh = (body.get("warehouse") or "MTY").strip().upper()
    if _wh not in ("MTY", "CDMX", "TJ"):
        _wh = "MTY"
    _my_node = _NODE_BY_SELLER_WAREHOUSE.get((client.seller_id, _wh), "")

    skus = [str(it.get("sku", "")).strip() for it in items if it.get("sku")]
    _sfx_map_raw = await _ts_sfx_adj.get_seller_flex_stock_for_skus(skus) if skus else {}
    _sfx_map = {
        sku: [row for row in rows if row.get("node") == _my_node]
        for sku, rows in _sfx_map_raw.items()
    }

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append([
        "ASIN/SKU/FNSKU", "EXPIRY_DATE (dd/mm/yyyy)", "MANUFACTURING_DATE (dd/mm/yyyy)",
        "ADJUSTMENT_TYPE", "SOURCE_BIN_ID", "DESTINATION_BIN_ID",
        "SOURCE_INVENTORY_TYPE", "DESTINATION_INVENTORY_TYPE", "QUANTITY",
    ])

    skipped: list = []
    rows_written = 0
    for it in items:
        sku = str(it.get("sku", "")).strip()
        requested = int(it.get("quantity") or 0)
        if not sku or requested <= 0:
            continue
        real_available = sum(n.get("sellable_qty", 0) for n in _sfx_map.get(sku, []))
        capped = min(requested, real_available)
        if capped <= 0:
            skipped.append({"sku": sku, "reason": "sin stock real registrado en Seller Flex (0 disponible)"})
            continue
        if capped < requested:
            skipped.append({"sku": sku, "reason": f"pedido {requested}, topado a {capped} (stock real disponible)"})
        ws.append([sku, None, None, "REMOVE ", source_bin, None, "PRIME", None, capped])
        rows_written += 1

    if rows_written == 0:
        raise HTTPException(status_code=400, detail="Ningún item tiene stock real que dar de baja según seller_flex_stock")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    resp = StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="seller_flex_ajustes_eliminar.xlsx"',
            "X-Rows-Written": str(rows_written),
            "X-Rows-Skipped": str(len(skipped)),
        },
    )
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND PERIODIC SYNC — mantiene el caché Onsite siempre fresco
# ─────────────────────────────────────────────────────────────────────────────

_ONSITE_AUTOSYNC_INTERVAL_S = 3 * 3600  # 3 horas entre vueltas completas (las 3 cuentas)


async def _onsite_periodic_sync_loop() -> None:
    """
    Loop de sync periódico REACTIVADO 2026-08-22 (aprobado por Jovan).

    Historia: se creía que GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA devolvía
    FATAL de forma PERMANENTE para cuentas Seller Flex sin FBA tradicional
    -- un desarrollador anterior lo probó una vez, le falló, y desactivó
    este loop para siempre. Verificado en vivo hoy (ver memoria
    project_seller_flex_portal_and_qty_gap.md, actualización (3)): es
    INTERMITENTE, no permanente. VECTOR funcionó a la primera con 100% de
    coincidencia contra el snapshot manual; AUTOBOT fue intermitente.
    get_onsite_inventory_report() ahora reintenta 2 veces más (3 intentos
    totales, 45s entre cada uno -- el límite real de creación de reportes)
    antes de rendirse.

    Corre para las 3 cuentas Amazon configuradas, una tras otra (mismo
    respeto al rate limit), y repite cada _ONSITE_AUTOSYNC_INTERVAL_S.
    Resultado va a _onsite_stock_cache (mismo caché que ya leen
    amazon_products_seller_flex, amazon_products_stock_alerts vía
    _flx_cache_read, y el resto de la página Seller Flex) -- no requiere
    que nadie tenga una pestaña de Amazon abierta.

    NO reemplaza el snapshot manual seller_flex_stock (ese trae desglose
    por almacén MTY/CDMX y bin real -- este reporte solo da el total por
    cuenta) -- son fuentes complementarias, seller_flex_stock manda cuando
    existe, este reporte es el nuevo fallback automático en vez de la FBA
    Inventory API rota."""
    from app.services import token_store as _ts_onsite_auto
    from app.services.amazon_client import get_amazon_client as _get_amz_auto

    await asyncio.sleep(60)  # deja terminar el arranque de la app antes del primer ciclo
    while True:
        try:
            accounts = await _ts_onsite_auto.get_all_amazon_accounts()
        except Exception as e:
            logger.error(f"[Onsite AutoSync] No se pudo leer cuentas Amazon: {e}")
            accounts = []

        for acct in accounts:
            seller_id = acct.get("seller_id", "")
            if not seller_id:
                continue
            try:
                client = await _get_amz_auto(seller_id)
                if not client:
                    continue
                logger.info(f"[Onsite AutoSync] Sync para {seller_id}…")
                await _run_onsite_sync(client)
            except Exception as e:
                logger.error(f"[Onsite AutoSync] Error con {seller_id}: {type(e).__name__}: {e}")
            await asyncio.sleep(45)  # respeta el límite de 1 reporte/45s entre cuentas también

        logger.info(f"[Onsite AutoSync] Vuelta completa terminada, durmiendo {_ONSITE_AUTOSYNC_INTERVAL_S}s")
        await asyncio.sleep(_ONSITE_AUTOSYNC_INTERVAL_S)


def start_onsite_background_sync() -> None:
    """Registra el loop de sync periódico. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_onsite_periodic_sync_loop())


def _render_no_account(request: Request, template: str) -> HTMLResponse:
    """Template de error cuando no hay cuenta Amazon configurada."""
    return _templates.TemplateResponse(
        request, f"partials/{template}",
        {"error": "Sin cuenta Amazon", "no_account": True},
    )


def _render_error(request: Request, template: str, msg: str, extra: dict = None) -> HTMLResponse:
    """Template de error genérico."""
    ctx = {"error": msg, "no_account": False,
           "candidatos": [], "cand_total": 0, "cand_page": 1, "cand_pages": 1, "cand_days": 365,
           "historial": [], "seller_id": "", "show_parents": False, "parents_count": 0,
           "suprimidos": [], "sup_total": 0, "sup_page": 1, "sup_pages": 1,
           "inactivos": [],  "inac_total": 0, "inac_page": 1, "inac_pages": 1,
           "con_issues": [], "db_total": 0, "synced_at": None, "nickname": "", "marketplace": ""}
    if extra:
        ctx.update(extra)
    return _templates.TemplateResponse(request, f"partials/{template}", ctx)


# ─── Alertas críticas consolidadas ──────────────────────────────────────────

@router.get("/alerts")
async def get_amazon_alerts(
    seller_id: Optional[str] = Query(None),
    request: Request = None,
):
    """
    Retorna alertas críticas de la cuenta: listings suprimidos, stock bajo,
    stock en cero con listing activo. Reutiliza caché existente — costo cero
    si el caché está fresco.
    """
    from app.services import token_store as _ts

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cliente Amazon"}, status_code=400)

    sid = client.seller_id
    threshold = await _ts.get_amazon_stock_threshold(sid)

    # Reutilizar caché existente (no fuerza nuevas llamadas a SP-API)
    listings     = await _get_listings_cached(client)
    fba_list     = await _get_fba_cached(client)
    fba_index    = _build_fba_index(fba_list)

    suppressed_items = []
    low_stock_items  = []
    no_stock_items   = []

    for item in listings:
        sku       = item.get("sku") or item.get("sellerSku") or ""
        summaries = item.get("summaries", [{}])
        summary_0 = summaries[0] if summaries else {}
        title     = summary_0.get("itemName") or sku
        issues    = item.get("issues") or []
        status    = _listing_status(summaries)
        fba_data  = fba_index.get(sku, {})
        fba_units = fba_data.get("inventoryDetails", {}).get("fulfillableQuantity", 0) or 0

        if status in ("INACTIVE", "SUPPRESSED") and issues:
            suppressed_items.append({
                "sku": sku,
                "title": title,
                "issues_count": len(issues),
                "first_issue": issues[0].get("message", "") if issues else "",
            })
        elif status == "ACTIVE" and fba_units == 0:
            no_stock_items.append({"sku": sku, "title": title})
        elif status == "ACTIVE" and 0 < fba_units < threshold:
            low_stock_items.append({
                "sku": sku,
                "title": title,
                "fba_stock": fba_units,
                "threshold": threshold,
            })

    return JSONResponse({
        "suppressed":      suppressed_items,
        "low_stock":       low_stock_items,
        "no_stock_active": no_stock_items,
        "threshold":       threshold,
        "total_alerts":    len(suppressed_items) + len(low_stock_items) + len(no_stock_items),
    })


# ─── Bulk set qty=0 / reactivate ─────────────────────────────────────────────

@router.post("/products/bulk-action")
async def bulk_listing_action(
    payload: dict,
    seller_id: Optional[str] = Query(None),
):
    """
    Pone qty=0 o reactiva múltiples listings.
    Body: {"skus": ["SKU1", "SKU2"], "action": "set_qty_zero" | "reactivate_fba"}
    Acepta también "pause" como alias de "set_qty_zero" para compatibilidad.
    """
    import asyncio as _aio

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cliente Amazon"}, status_code=400)

    skus   = payload.get("skus", [])
    action = payload.get("action", "")
    # Normalizar alias
    if action == "pause":
        action = "set_qty_zero"

    if action not in ("set_qty_zero", "reactivate_fba"):
        return JSONResponse({"error": "action debe ser 'set_qty_zero' o 'reactivate_fba'"}, status_code=400)
    if not skus:
        return JSONResponse({"error": "Lista de SKUs vacía"}, status_code=400)

    results: dict = {}
    for sku in skus:
        try:
            await client.update_listing_fulfillment(sku=sku, action=action)
            results[sku] = "ok"
        except Exception as e:
            results[sku] = f"error: {str(e)[:80]}"
        await _aio.sleep(0.6)  # rate limit SP-API

    # Invalidar caché de listings
    sid = client.seller_id
    _listings_cache.pop(sid, None)

    succeeded = sum(1 for v in results.values() if v == "ok")
    return JSONResponse({
        "ok": succeeded == len(skus),
        "results": results,
        "succeeded": succeeded,
        "failed": len(skus) - succeeded,
    })


# ─── Stock threshold settings ────────────────────────────────────────────────

@router.get("/settings/stock-threshold")
async def get_stock_threshold(seller_id: Optional[str] = Query(None)):
    from app.services import token_store as _ts

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"threshold": 5})
    threshold = await _ts.get_amazon_stock_threshold(client.seller_id)
    return JSONResponse({"threshold": threshold, "seller_id": client.seller_id})


@router.post("/settings/stock-threshold")
async def set_stock_threshold(
    payload: dict,
    seller_id: Optional[str] = Query(None),
):
    from app.services import token_store as _ts

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cliente Amazon"}, status_code=400)

    threshold = int(payload.get("threshold", 5))
    if threshold < 0 or threshold > 9999:
        return JSONResponse({"error": "Threshold fuera de rango (0-9999)"}, status_code=400)

    await _ts.set_amazon_stock_threshold(client.seller_id, threshold)
    return JSONResponse({"ok": True, "threshold": threshold})


# ─── Finances summary ────────────────────────────────────────────────────────

_finances_cache: dict = {}
_FINANCES_TTL = 1800  # 30 minutos

@router.get("/finances/summary")
async def get_finances_summary(seller_id: Optional[str] = Query(None)):
    """
    Retorna: grupos de liquidación recientes + métricas mensuales (OPS, fees est., neto est.).
    Mes actual y mes anterior. TTL: 30 min.
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cliente Amazon"}, status_code=400)

    sid = client.seller_id
    cached = _finances_cache.get(sid)
    if cached and (_time.time() - cached["ts"]) < _FINANCES_TTL:
        return JSONResponse(cached["data"])

    # ── 1. Settlement groups ─────────────────────────────────────────────────
    groups = await client.get_financial_event_groups(max_results=12)

    # ── 2. Monthly OPS from Sales API ────────────────────────────────────────
    from app.api.metrics import _get_cached_order_metrics
    now = datetime.utcnow()
    # Current month: 1st of this month → today
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    today_str   = now.strftime("%Y-%m-%d")
    # Previous month: 1st → last day of prev month
    first_this  = now.replace(day=1)
    last_prev   = first_this - timedelta(days=1)
    prev_start  = last_prev.replace(day=1).strftime("%Y-%m-%d")
    prev_end    = last_prev.strftime("%Y-%m-%d")

    async def _month_ops(d_from, d_to):
        try:
            metrics = await _get_cached_order_metrics(client, d_from, d_to)
            total = sum(float((m.get("totalSales") or {}).get("amount") or 0) for m in metrics)
            units = sum(int(m.get("unitCount") or 0) for m in metrics)
            orders = sum(int(m.get("orderCount") or 0) for m in metrics)
            return {"sales": round(total, 2), "units": units, "orders": orders}
        except Exception:
            return {"sales": 0, "units": 0, "orders": 0}

    cur, prev, refunds = await asyncio.gather(
        _month_ops(month_start, today_str),
        _month_ops(prev_start, prev_end),
        client.get_refunds_30d(),
    )

    # Pending payout = most recent Open settlement group total
    pending_payout = 0.0
    pending_currency = "MXN"
    for g in groups:
        if g.get("status") == "Open":
            pending_payout = g.get("converted_total") or g.get("original_total") or 0
            pending_currency = g.get("currency", "MXN")
            break

    # Fee estimate: Amazon MX referral ~15% + FBA ~5% ≈ 20% of OPS
    FEE_RATE = 0.20
    fees_est    = round(cur["sales"] * FEE_RATE, 2)
    net_est     = round(cur["sales"] - fees_est, 2)
    fees_prev   = round(prev["sales"] * FEE_RATE, 2)
    net_prev    = round(prev["sales"] - fees_prev, 2)

    payload = {
        "groups": groups,
        "count": len(groups),
        "current_month": {
            "label": now.strftime("%B %Y"),
            "sales": cur["sales"],
            "units": cur["units"],
            "orders": cur["orders"],
            "fees_est": fees_est,
            "net_est": net_est,
        },
        "prev_month": {
            "label": last_prev.strftime("%B %Y"),
            "sales": prev["sales"],
            "units": prev["units"],
            "orders": prev["orders"],
            "fees_est": fees_prev,
            "net_est": net_prev,
        },
        "pending_payout": pending_payout,
        "pending_currency": pending_currency,
        "currency": "MXN",
        "refunds_30d": {
            "count":    refunds.get("count", 0),
            "total":    refunds.get("total", 0),
            "currency": refunds.get("currency", "MXN"),
            "rate_pct": round(refunds.get("total", 0) / cur["sales"] * 100, 1) if cur["sales"] > 0 else 0.0,
        },
    }
    _finances_cache[sid] = {"ts": _time.time(), "data": payload}
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# NEW ENDPOINTS
# ---------------------------------------------------------------------------


@router.get("/restock-report")
async def get_restock_report(seller_id: Optional[str] = Query(None)):
    """
    Calcula días de cobertura de stock FBA basado en velocidad de ventas.
    Fórmula: días_cobertura = fba_stock / velocidad_diaria_30d
    Estado: OK (>30d), WARNING (10-30d), CRITICAL (<10d), OUT (0 stock)
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cliente Amazon"}, status_code=400)

    listings = await _get_listings_cached(client)
    fba_list = await _get_fba_cached(client)
    fba_index = _build_fba_index(fba_list)

    # Get SKU sales from cache
    sku_sales = {}
    cached_sales = _sku_sales_cache.get(client.seller_id)
    if cached_sales and (_time.time() - cached_sales[0]) < _SKU_SALES_TTL:
        _, sku_sales = cached_sales

    items = []
    for listing in listings:
        summaries = listing.get("summaries", [])
        if _listing_status(summaries) != "ACTIVE":
            continue

        sku = listing.get("sku", "")
        title = summaries[0].get("itemName", "") if summaries else ""

        fba_entry = fba_index.get(sku, {})
        inv_details = fba_entry.get("inventoryDetails", {})
        fba_units = inv_details.get("fulfillableQuantity", 0) or 0
        inbound = inv_details.get("inboundReceivingQuantity", 0) or 0
        reserved = inv_details.get("reservedQuantity", 0) or 0

        sale_data = sku_sales.get(sku, {})
        units_30d = sale_data.get("units", 0) or 0
        velocity_daily = round(units_30d / 30, 2)

        if velocity_daily > 0:
            days_coverage = fba_units / velocity_daily
        else:
            days_coverage = None  # will treat as 999 for sorting

        if fba_units == 0:
            status = "out"
        elif days_coverage is None:
            status = "ok"
        elif days_coverage < 10:
            status = "critical"
        elif days_coverage < 30:
            status = "warning"
        else:
            status = "ok"

        restock_qty = max(0, int(30 * velocity_daily) - fba_units - inbound)

        items.append({
            "sku": sku,
            "title": title,
            "fba_stock": fba_units,
            "inbound": inbound,
            "reserved": reserved,
            "units_30d": units_30d,
            "velocity_daily": velocity_daily,
            "days_coverage": round(days_coverage, 1) if days_coverage is not None else None,
            "status": status,
            "restock_qty": restock_qty,
        })

    # Sort: None/999 at end, ascending by days_coverage
    def _sort_key(item):
        dc = item["days_coverage"]
        return dc if dc is not None else 999

    items.sort(key=_sort_key)

    summary = {
        "total_active": len(items),
        "critical": sum(1 for i in items if i["status"] == "critical"),
        "warning": sum(1 for i in items if i["status"] == "warning"),
        "ok": sum(1 for i in items if i["status"] == "ok"),
        "out_of_stock": sum(1 for i in items if i["status"] == "out"),
        "has_sales_data": bool(sku_sales),
    }

    return JSONResponse({"items": items, "summary": summary})


@router.get("/listing-quality")
async def get_listing_quality(seller_id: Optional[str] = Query(None)):
    """
    Calcula un score de calidad (0-100) para cada listing.
    Basado en: issues, estado, datos básicos del listing.
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cliente Amazon"}, status_code=400)

    listings = await _get_listings_cached(client)

    # ── Pre-fetch BSR from Catalog API (cached 30 min) ───────────────────────
    # The Listings Items API does NOT include sales rank — it comes from Catalog API.
    asin_list = []
    for listing in listings:
        summaries = listing.get("summaries", [])
        asin = (summaries[0].get("asin") if summaries else None) or ""
        if asin:
            asin_list.append(asin)
    bsr_index = await _get_bsr_cached(client, asin_list)

    # Señal dinámica: stock real en BM (idea tomada de Helium10 Listing Analyzer,
    # 2026-07-23). Vía import dinámico para evitar import circular con main.py
    # (main.py importa este router). price_comp_score (vs Buy Box) y claims_score
    # quedan pendientes: el primero depende de la Feature "Vigilancia"/Buy Box
    # (aún no implementada) y el segundo de cruzar con datos de Retornos — no se
    # agregan todavía para no fingir una señal que no existe.
    import sys as _sys_lq
    from app.services.sku_utils import normalize_to_bm_sku as _norm_lq
    _main_mod_lq = _sys_lq.modules.get("app.main")
    _bm_cache_lq = getattr(_main_mod_lq, "_bm_stock_cache", {}) if _main_mod_lq else {}

    items = []
    for listing in listings:
        summaries = listing.get("summaries", [])
        sku = listing.get("sku", "")
        title = summaries[0].get("itemName", "") if summaries else ""
        asin = (summaries[0].get("asin") if summaries else None) or ""
        issues = listing.get("issues", [])
        status = _listing_status(summaries)

        # Estático, reescalado a 85 pts (antes 100) — deja 15 pts para stock real.
        static_score = 100
        issues_count = len(issues)
        static_score -= 15 * min(issues_count, 4)
        if status in ("INACTIVE", "SUPPRESSED"):
            static_score -= 30
        elif status == "DISCOVERABLE":
            static_score -= 10
        static_score = max(0, static_score) * 0.85

        _bm_entry = _bm_cache_lq.get(_norm_lq(sku)) if sku else None
        _avail = (_bm_entry[1].get("avail_total", 0) if _bm_entry else 0) or 0
        stock_score = 15 if _avail >= 5 else (7 if _avail > 0 else 0)
        score = min(100, int(static_score + stock_score))

        if score >= 85:
            grade = "A"
        elif score >= 70:
            grade = "B"
        elif score >= 55:
            grade = "C"
        else:
            grade = "D"

        issue_messages = []
        for issue in issues[:3]:
            msg = issue.get("message") or issue.get("code") or str(issue)
            issue_messages.append(msg)
        if stock_score == 0:
            issue_messages.append("Sin stock en BM")

        # BSR — from Catalog API (bsr_index built above)
        bsr_data = bsr_index.get(asin, {})
        bsr_rank = bsr_data.get("rank")
        bsr_category = bsr_data.get("category")

        items.append({
            "sku": sku,
            "asin": asin,
            "title": title,
            "score": score,
            "grade": grade,
            "status": status,
            "issues_count": issues_count,
            "issues": issue_messages,
            "bsr_rank": bsr_rank,
            "bsr_category": bsr_category,
            "stock_score": stock_score,
        })

    items.sort(key=lambda x: x["score"])

    total = len(items)
    avg_score = round(sum(i["score"] for i in items) / total, 1) if total > 0 else 0.0
    summary = {
        "avg_score": avg_score,
        "grade_A": sum(1 for i in items if i["grade"] == "A"),
        "grade_B": sum(1 for i in items if i["grade"] == "B"),
        "grade_C": sum(1 for i in items if i["grade"] == "C"),
        "grade_D": sum(1 for i in items if i["grade"] == "D"),
        "total": total,
    }

    return JSONResponse({"items": items, "summary": summary})


@router.get("/top-products")
async def get_top_products(
    seller_id: Optional[str] = Query(None),
    limit: int = Query(10),
    sort_by: str = Query("revenue"),  # "revenue" or "units"
):
    """
    Retorna los top N productos por revenue o unidades vendidas en 30 días.
    Usa el caché de SKU sales existente (_sku_sales_cache).
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cliente Amazon"}, status_code=400)

    listings = await _get_listings_cached(client)
    title_index = {}
    for listing in listings:
        summaries = listing.get("summaries", [])
        sku = listing.get("sku", "")
        title = summaries[0].get("itemName", "") if summaries else ""
        title_index[sku] = title

    fba_list = await _get_fba_cached(client)
    fba_index = _build_fba_index(fba_list)

    sku_sales = {}
    has_data = False
    cached_sales = _sku_sales_cache.get(client.seller_id)
    if cached_sales and (_time.time() - cached_sales[0]) < _SKU_SALES_TTL:
        _, sku_sales = cached_sales
        has_data = bool(sku_sales)

    sort_key = "revenue" if sort_by != "units" else "units"
    effective_limit = min(limit, 20)

    sorted_skus = sorted(
        sku_sales.items(),
        key=lambda x: x[1].get(sort_key, 0),
        reverse=True,
    )[:effective_limit]

    total_revenue = sum(v.get("revenue", 0) for v in sku_sales.values())
    total_units = sum(v.get("units", 0) for v in sku_sales.values())

    items = []
    for rank, (sku, data) in enumerate(sorted_skus, start=1):
        fba_entry = fba_index.get(sku, {})
        inv_details = fba_entry.get("inventoryDetails", {})
        fba_stock = inv_details.get("fulfillableQuantity", 0) or 0

        rev = data.get("revenue", 0)
        units = data.get("units", 0)

        if sort_by == "units":
            share_pct = round((units / total_units * 100), 1) if total_units > 0 else 0.0
        else:
            share_pct = round((rev / total_revenue * 100), 1) if total_revenue > 0 else 0.0

        items.append({
            "rank": rank,
            "sku": sku,
            "title": title_index.get(sku, ""),
            "units_30d": units,
            "revenue_30d": round(rev, 2),
            "fba_stock": fba_stock,
            "share_pct": share_pct,
        })

    return JSONResponse({
        "items": items,
        "total_revenue_30d": round(total_revenue, 2),
        "total_units_30d": total_units,
        "has_data": has_data,
        "period_days": 30,
    })


@router.post("/ai-advisor")
async def amazon_ai_advisor(
    payload: dict,
    seller_id: Optional[str] = Query(None),
):
    """
    Consulta al AI Advisor usando Claude con streaming SSE.
    Body: {"question": "...", "mode": "general|restock|listings|pricing|strategy"}
    """
    if not _or_client.is_available():
        return JSONResponse({"error": "OpenRouter no configurado"}, status_code=500)

    client = await get_amazon_client(seller_id=seller_id)
    context_lines = []
    if client:
        listings = await _get_listings_cached(client)
        fba_list = await _get_fba_cached(client)
        fba_index = _build_fba_index(fba_list)
        total_active = sum(1 for l in listings if _listing_status(l.get("summaries", [])) == "ACTIVE")
        total_fba_units = sum(
            s.get("inventoryDetails", {}).get("fulfillableQuantity", 0) or 0
            for s in fba_list
        )

        sku_sales_data = _sku_sales_cache.get(client.seller_id)
        if sku_sales_data and (_time.time() - sku_sales_data[0]) < _SKU_SALES_TTL:
            _, sku_map = sku_sales_data
            total_rev = sum(v.get("revenue", 0) for v in sku_map.values())
            total_units = sum(v.get("units", 0) for v in sku_map.values())
            top_skus = sorted(sku_map.items(), key=lambda x: x[1].get("revenue", 0), reverse=True)[:5]
            context_lines.append(f"- Ventas 30d: {total_units} unidades, ${total_rev:,.2f} MXN revenue")
            context_lines.append(f"- Top 5 SKUs por revenue: {', '.join(s for s, _ in top_skus)}")

        context_lines.append(f"- Listings activos: {total_active}")
        context_lines.append(f"- Stock FBA disponible total: {total_fba_units} unidades")
        context_lines.append(f"- Marketplace: Amazon {getattr(client, 'marketplace_id', None) or 'MX'}")

    mode = payload.get("mode", "general")
    question = payload.get("question", "").strip()[:1000]

    mode_instructions = {
        "general": "Eres un experto en Amazon Seller Central MX con 10 años de experiencia. Das consejos prácticos y accionables.",
        "restock": "Eres un experto en gestión de inventario FBA Amazon MX. Analizas datos de stock y ventas para recomendar reabastecimiento óptimo.",
        "listings": "Eres un experto en optimización de listings Amazon MX. Analizas calidad de listings y das recomendaciones para mejorar visibilidad y conversión.",
        "pricing": "Eres un experto en estrategia de precios y Buy Box en Amazon MX. Das recomendaciones basadas en datos de competencia y márgenes.",
        "strategy": "Eres un consultor estratégico de Amazon MX especializado en crecimiento de ventas. Das planes de acción concretos con KPIs medibles.",
    }
    system_prompt = mode_instructions.get(mode, mode_instructions["general"])
    system_prompt += "\n\nResponde siempre en español. Sé directo y conciso. Usa datos específicos cuando estén disponibles."

    context_str = "\n".join(context_lines) if context_lines else "No hay datos de contexto disponibles."
    user_message = f"CONTEXTO DE MI TIENDA AMAZON:\n{context_str}\n\nPREGUNTA: {question}"

    async def generate():
        try:
            async for chunk in _or_client.generate_stream(
                user_message, system=system_prompt, max_tokens=1024,
                model=_or_client.get_premium_model(),
            ):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────────────────────────
# DEALS & PROMOCIONES
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/deals")
async def amazon_deals(
    seller_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),  # RUNNING | UPCOMING | ENDED | None=todos
):
    """Deals activos y próximos (Lightning Deals + Best Deals)."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cuenta Amazon configurada"}, status_code=401)

    cache_key = f"{client.seller_id}:{status or 'all'}"
    now = _time.time()
    cached = _deals_cache.get(cache_key)
    if cached and (now - cached[0]) < _DEALS_TTL:
        return JSONResponse(cached[1])

    try:
        if status:
            raw = await client.get_deals(status=status)
        else:
            results = await asyncio.gather(
                client.get_deals(status="RUNNING"),
                client.get_deals(status="UPCOMING"),
                return_exceptions=True,
            )
            raw = []
            for r in results:
                if isinstance(r, list):
                    raw.extend(r)

        deals = []
        for d in raw:
            start_raw = d.get("startTime") or d.get("dealStartTime") or ""
            end_raw   = d.get("endTime")   or d.get("dealEndTime")   or ""
            dp = d.get("dealPrice") or d.get("price") or {}
            op = d.get("originalPrice") or d.get("listingPrice") or {}
            deal_price = float(dp.get("amount") or dp.get("Amount") or 0) or None
            orig_price = float(op.get("amount") or op.get("Amount") or 0) or None
            disc_pct = 0
            if deal_price and orig_price and orig_price > deal_price:
                disc_pct = round((1 - deal_price / orig_price) * 100)
            deals.append({
                "deal_id":       d.get("dealId", ""),
                "title":         (d.get("title") or d.get("dealTitle") or "")[:80],
                "deal_type":     d.get("dealType", ""),
                "status":        d.get("status", ""),
                "start_time":    start_raw[:16].replace("T", " ") if start_raw else "",
                "end_time":      end_raw[:16].replace("T", " ")   if end_raw   else "",
                "item_count":    d.get("itemCount") or 0,
                "deal_price":    deal_price,
                "original_price": orig_price,
                "discount_pct":  disc_pct,
                "units_sold":    d.get("unitsSoldInDeal"),
                "progress_pct":  d.get("dealProgressPercent"),
            })

        running_count  = sum(1 for x in deals if x["status"] == "RUNNING")
        upcoming_count = sum(1 for x in deals if x["status"] == "UPCOMING")
        no_deals_msg = None
        if not deals:
            no_deals_msg = (
                "No hay Lightning Deals ni Best Deals activos o próximos. "
                "Los deals se crean en Seller Central → Publicidad → Deals."
            )

        resp = {
            "deals": deals,
            "total": len(deals),
            "running_count": running_count,
            "upcoming_count": upcoming_count,
            "no_deals_msg": no_deals_msg,
        }
        _deals_cache[cache_key] = (_time.time(), resp)
        return JSONResponse(resp)

    except Exception as e:
        logger.exception("[Amazon Deals] Error")
        if cached:
            return JSONResponse({**cached[1], "stale": True})
        return JSONResponse(
            {"deals": [], "total": 0, "running_count": 0, "upcoming_count": 0,
             "no_deals_msg": None, "error": str(e)[:150]},
            status_code=200,  # error suave — el tab no debe explotar
        )


@router.get("/competitive-pricing")
async def amazon_competitive_pricing(
    seller_id: Optional[str] = Query(None),
    limit: int = Query(default=20, ge=1, le=40),
):
    """
    Precios de nuestros top SKUs comparados contra el Buy Box actual.
    Primera carga tarda ~22s (rate limit Pricing API 1 req/s).
    Cache: 10 minutos.
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cuenta Amazon"}, status_code=401)

    cache_key = f"{client.seller_id}:cp:{limit}"
    now = _time.time()
    cached = _comp_price_cache.get(cache_key)
    if cached and (now - cached[0]) < _COMP_PRICE_TTL:
        return JSONResponse({**cached[1], "cached": True})

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        fba_index = _build_fba_index(fba_summaries)

        # Construir candidatos ACTIVOS con ASIN
        candidates = []
        for item in listings:
            sku       = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            offers    = item.get("offers", [])
            if _listing_status(summaries) != "ACTIVE":
                continue
            summary_0 = summaries[0] if summaries else {}
            fba_data  = fba_index.get(sku, {})
            asin = (fba_data.get("asin") or summary_0.get("asin") or
                    (item.get("attributes") or {}).get("asin", [{}])[0].get("value") or "")
            if not asin:
                continue
            fba_stock = int(
                ((fba_data.get("inventoryDetails") or {}).get("fulfillableQuantity") or 0)
            )
            our_price = _parse_price(offers)
            title     = (summary_0.get("itemName") or sku)[:70]
            candidates.append({
                "sku": sku, "asin": asin, "title": title,
                "our_price": our_price, "fba_stock": fba_stock,
            })

        # Priorizar por stock FBA
        candidates.sort(key=lambda x: x["fba_stock"], reverse=True)
        top = candidates[:limit]

        # Consultar Buy Box para cada ASIN (rate-limited)
        items_out = []
        above_bb = at_bb = below_bb = no_bb = 0

        for c in top:
            pd = await client.get_competitive_price(c["asin"])
            await asyncio.sleep(1.1)  # respetar 1 req/s

            bb = pd.get("buybox_price")
            our = c["our_price"]
            gap = gap_pct = 0.0
            status_str = "no_bb"
            action = "Sin Buy Box activo para este ASIN"

            if bb and our > 0:
                gap     = round(our - bb, 2)
                gap_pct = round(gap / our * 100, 1)
                if abs(gap_pct) <= 2:
                    status_str = "at_bb";   action = "Precio competitivo — mantener"; at_bb += 1
                elif our > bb:
                    status_str = "above_bb"; action = f"Bajar ${abs(gap):,.0f} ({abs(gap_pct):.1f}%) para alcanzar Buy Box"; above_bb += 1
                else:
                    status_str = "below_bb"; action = f"Puedes subir hasta ${abs(gap):,.0f} ({abs(gap_pct):.1f}%) sin perder Buy Box"; below_bb += 1
            else:
                no_bb += 1

            items_out.append({
                "sku":           c["sku"],
                "asin":          c["asin"],
                "title":         c["title"],
                "our_price":     our,
                "buybox_price":  bb,
                "gap":           gap,
                "gap_pct":       gap_pct,
                "num_competitors": pd.get("num_offers", 0),
                "fba_stock":     c["fba_stock"],
                "status":        status_str,
                "action":        action,
                "amazon_url":    f"https://www.amazon.com.mx/dp/{c['asin']}" if c["asin"] else "",
            })

        total_items = above_bb + at_bb + below_bb + no_bb
        buybox_win_pct = round(at_bb / total_items * 100, 1) if total_items > 0 else 0.0

        resp = {
            "items": items_out, "total": len(items_out),
            "above_bb_count": above_bb, "at_bb_count": at_bb,
            "below_bb_count": below_bb, "no_bb_count": no_bb,
            "buybox_win_pct": buybox_win_pct,
            "cached": False,
        }
        _comp_price_cache[cache_key] = (_time.time(), resp)
        return JSONResponse(resp)

    except Exception as e:
        logger.exception("[Amazon CompPricing] Error")
        if cached:
            return JSONResponse({**cached[1], "cached": True, "stale_error": str(e)[:100]})
        return JSONResponse(
            {"items": [], "total": 0, "above_bb_count": 0, "at_bb_count": 0,
             "below_bb_count": 0, "no_bb_count": 0, "error": str(e)[:150], "cached": False},
            status_code=200,
        )


@router.get("/deal-candidates")
async def amazon_deal_candidates(
    seller_id: Optional[str] = Query(None),
    min_stock: int = Query(default=3, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
):
    """
    Productos candidatos a deal: stock alto + ventas bajas.
    No hace llamadas nuevas a la API — usa caches existentes.
    Instantáneo si los tabs Catálogo/FBA ya cargaron.
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "Sin cuenta Amazon"}, status_code=401)

    cache_key = f"{client.seller_id}:dc:{min_stock}"
    now = _time.time()
    cached = _deal_cands_cache.get(cache_key)
    if cached and (now - cached[0]) < _DEAL_CANDS_TTL:
        return JSONResponse(cached[1])

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        fba_index = _build_fba_index(fba_summaries)

        # Ventas 30d del cache (sin forzar refresh)
        sku_sales_map = {}
        no_sales_data = True
        entry = _sku_sales_cache.get(client.seller_id)
        if entry and (now - entry[0]) < _SKU_SALES_TTL:
            _, sku_sales_map = entry
            no_sales_data = False

        candidates = []
        on_deal_count = 0

        for item in listings:
            sku       = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            offers    = item.get("offers", [])
            attributes = item.get("attributes") or {}
            if _listing_status(summaries) != "ACTIVE":
                continue

            fba_data  = fba_index.get(sku, {})
            fba_stock = int(
                ((fba_data.get("inventoryDetails") or {}).get("fulfillableQuantity") or 0)
            )
            if fba_stock < min_stock:
                continue

            summary_0 = summaries[0] if summaries else {}
            asin      = (fba_data.get("asin") or summary_0.get("asin") or "")
            title     = (summary_0.get("itemName") or sku)[:70]
            our_price = _parse_price(offers)
            if our_price <= 0:
                continue

            # Detectar deal activo
            deal_info  = _parse_deal_info(offers, attributes)
            is_on_deal = deal_info.get("is_deal", False)
            if is_on_deal:
                on_deal_count += 1

            # Ventas 30d
            sku_entry  = sku_sales_map.get(sku, {})
            units_30d  = int(sku_entry.get("units", 0) if isinstance(sku_entry, dict) else sku_entry)

            deal_price = round(our_price * 0.85, 2)
            daily_rate = units_30d / 30 if units_30d > 0 else 0
            days_inv   = round(fba_stock / daily_rate, 1) if daily_rate > 0 else None
            deal_score = round((fba_stock / max(units_30d, 1)) * 10)

            if is_on_deal:
                reason = f"Ya tiene {deal_info.get('deal_pct', 0)}% de descuento activo"
            elif units_30d == 0 and not no_sales_data:
                reason = f"Sin ventas en 30d con {fba_stock}u en FBA — deal puede activar demanda"
            elif days_inv and days_inv > 90:
                reason = f"{int(days_inv)} días de inventario — deal para rotar stock"
            elif fba_stock >= 20 and units_30d < 5:
                reason = f"Stock alto ({fba_stock}u) con ventas bajas ({units_30d}u/30d)"
            else:
                reason = f"{fba_stock}u en FBA, {units_30d}u vendidas en 30d"

            candidates.append({
                "sku":                   sku,
                "asin":                  asin,
                "title":                 title,
                "our_price":             our_price,
                "deal_price_suggestion": deal_price,
                "discount_pct":          15,
                "fba_stock":             fba_stock,
                "units_sold_30d":        units_30d,
                "days_inventory":        days_inv,
                "deal_score":            deal_score,
                "is_on_deal":            is_on_deal,
                "current_deal_pct":      deal_info.get("deal_pct", 0) if is_on_deal else 0,
                "reason":                reason,
                "amazon_url":            f"https://www.amazon.com.mx/dp/{asin}" if asin else "",
            })

        candidates.sort(key=lambda x: (-x["is_on_deal"], -x["deal_score"]))
        candidates = candidates[:limit]

        resp = {
            "candidates":    candidates,
            "total":         len(candidates),
            "on_deal_count": on_deal_count,
            "no_sales_data": no_sales_data,
        }
        _deal_cands_cache[cache_key] = (_time.time(), resp)
        return JSONResponse(resp)

    except Exception as e:
        logger.exception("[Amazon DealCandidates] Error")
        return JSONResponse(
            {"candidates": [], "total": 0, "on_deal_count": 0,
             "no_sales_data": True, "error": str(e)[:150]},
            status_code=200,
        )


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON LANZADOR — Lee gaps desde DB (poblada por scan background)
# El scan se lanza desde /api/amazon/lanzar/scan
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/products/sin-lanzar", response_class=HTMLResponse)
async def amazon_sin_lanzar(
    request: Request,
    seller_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
    q: str = Query("", description="Filtro por SKU o título"),
    category: str = Query("", description="Filtro por categoría BM"),
    show_sin_precio: bool = Query(False, description="Mostrar SKUs sin retail price"),
):
    """Sin Publicar: lee gaps desde DB. Escaneo en background vía /api/amazon/lanzar/scan."""
    import aiosqlite as _aiosqlite
    from app.services.token_store import DATABASE_PATH as _DB

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return _render_no_account(request, "amazon_sin_lanzar.html")

    sid = client.seller_id

    async with _aiosqlite.connect(_DB) as db:
        db.row_factory = _aiosqlite.Row

        # Scan status
        cur = await db.execute(
            "SELECT * FROM amz_gap_scan_status WHERE seller_id=?", (sid,)
        )
        scan_row = await cur.fetchone()
        scan_status = dict(scan_row) if scan_row else {}

        # Build filter
        tab_status = "sin_precio" if show_sin_precio else "unlaunched"
        where = "seller_id=? AND status=?"
        params: list = [sid, tab_status]
        q_s = q.strip()
        cat_s = category.strip()
        if q_s:
            ql = f"%{q_s.upper()}%"
            where += " AND (UPPER(sku) LIKE ? OR UPPER(product_title) LIKE ? OR UPPER(brand) LIKE ?)"
            params += [ql, ql, ql]
        if cat_s:
            where += " AND category=?"
            params.append(cat_s)

        cnt = (await (await db.execute(
            f"SELECT COUNT(*) FROM amz_sku_gaps WHERE {where}", params
        )).fetchone())[0]

        pages = max(1, math.ceil(cnt / per_page))
        page  = max(1, min(page, pages))
        off   = (page - 1) * per_page

        rows = await (await db.execute(
            f"""SELECT * FROM amz_sku_gaps WHERE {where}
                ORDER BY avail_qty DESC LIMIT ? OFFSET ?""",
            params + [per_page, off],
        )).fetchall()

        # FEATURE 2026-08-19 (pedido por Jovan, "dejar todo al 100"): antes
        # era solo una lista de nombres sin conteo -- se iguala al mismo
        # desglose que ya tiene ML en /api/lanzar/filters (get_gap_filters,
        # lanzar.py) para que "No Lanzados" de Amazon muestre cuántos SKUs
        # y cuánto stock hay por categoría, no solo el nombre.
        cat_rows = await (await db.execute(
            """SELECT category, COUNT(*) as cnt, SUM(avail_qty) as total_stock
               FROM amz_sku_gaps
               WHERE seller_id=? AND status='unlaunched' AND category!=''
               GROUP BY category ORDER BY cnt DESC""",
            (sid,),
        )).fetchall()
        categories = [{"category": r[0], "count": r[1], "total_stock": r[2] or 0} for r in cat_rows]

        # Conteo sin precio
        sin_precio_cnt = (await (await db.execute(
            "SELECT COUNT(*) FROM amz_sku_gaps WHERE seller_id=? AND status='sin_precio'",
            (sid,),
        )).fetchone())[0]

    # Map DB fields → template fields
    gaps_page = []
    for r in rows:
        d = dict(r)
        d["title"]      = d.get("product_title", "")
        d["price_sug"]  = d.get("suggested_price", 0)
        d["retail_usd"] = d.get("cost_usd", 0)
        d["retail_mxn"] = d.get("cost_mxn", 0)
        gaps_page.append(d)

    # Enriquecer con MTY/CDMX/TJ (timeout para no bloquear la página)
    try:
        await _enrich_bm_amz(gaps_page, timeout_s=5.0)
    except Exception:
        pass

    # Calcular tiempo desde último scan
    cached_ago = 0
    finished_at = scan_status.get("finished_at", "")
    if finished_at:
        try:
            scan_dt = datetime.fromisoformat(finished_at)
            cached_ago = int((datetime.utcnow() - scan_dt).total_seconds())
        except Exception:
            pass

    ctx = {
        "gaps":            gaps_page,
        "total":           cnt,
        "page":            page,
        "pages":           pages,
        "per_page":        per_page,
        "q":               q_s,
        "category":        cat_s,
        "categories":      categories,
        "show_sin_precio": show_sin_precio,
        "sin_precio_cnt":  sin_precio_cnt,
        "bm_total":        scan_status.get("bm_total", 0),
        "amazon_active":   scan_status.get("amazon_active", 0),
        "cached_ago":      cached_ago,
        "scan_status":     scan_status.get("status", "never"),
        "scan_error":      scan_status.get("error") or "",
        "force":           False,
        "marketplace":     client.marketplace_name,
        "marketplace_id":  client.marketplace_id,
        "seller_id":       sid,
        "is_usd":          (
            client.marketplace_id != "A1AM78C64UM0Y8"
            or (client.marketplace_name or "MX").upper() in ("US", "CA", "UK", "JP", "DE", "FR", "AU", "BR", "IN")
        ),
    }
    return _templates.TemplateResponse(request, "partials/amazon_sin_lanzar.html", ctx)


# ─────────────────────────────────────────────────────────────────────────────
# SIN BM — Listings activos sin SKU en BinManager
# ─────────────────────────────────────────────────────────────────────────────
_sin_bm_amz_cache: dict[str, tuple[float, list]] = {}  # {seller_id: (ts, items)}
_SIN_BM_AMZ_TTL = 900  # 15 min


@router.get("/products/sin-bm", response_class=HTMLResponse)
async def amazon_products_sin_bm(
    request:    Request,
    seller_id:  Optional[str] = Query(None),
    page:       int = Query(1, ge=1),
    per_page:   int = Query(10, ge=5, le=50),
    q:          str = Query(""),
    force:      bool = Query(False),
):
    """Listings ACTIVOS en Amazon cuyo SKU base no existe en BinManager."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return _render_no_account(request, "partials/amazon_products_sin_bm.html")

    cache_key = client.seller_id
    now = _time.time()

    sin_bm_all: list = []
    if not force:
        cached = _sin_bm_amz_cache.get(cache_key)
        if cached and (now - cached[0]) < _SIN_BM_AMZ_TTL:
            sin_bm_all = cached[1]

    if not sin_bm_all:
        try:
            # FIX 2026-08-20 (directiva de Jovan: "todo debe apuntar a nuestro
            # maestro, nada a BM por el momento"): esto verifica EXISTENCIA
            # (no stock), así que min_qty=0 -- un SKU real de BM con 0
            # disponible sigue siendo "existe en BM", no debe aparecer aquí
            # como huérfano.
            from app.services import token_store

            listings, bm_rows = await asyncio.gather(
                _get_listings_cached(client),
                token_store.get_bm_master_all_as_bulk_rows(min_qty=0),
                return_exceptions=True,
            )
            if isinstance(listings, Exception):
                listings = []
            if isinstance(bm_rows, Exception):
                bm_rows = []

            # Build BM SKU set — BM puede retornar "SNTV000872-GRA"; agregar también la base
            _BM_SFX = ("-GRA", "-GRB", "-GRC", "-ICB", "-ICC", "-NEW")
            bm_skus: set[str] = set()
            for row in bm_rows:
                sk = (row.get("SKU") or "").strip().upper()
                if not sk:
                    continue
                bm_skus.add(sk)
                for sfx in _BM_SFX:
                    if sk.endswith(sfx):
                        bm_skus.add(sk[:-len(sfx)])
                        break

            for item in listings:
                sku      = item.get("sku", "")
                summaries = item.get("summaries", [{}])
                status    = _listing_status(summaries)
                if status != "ACTIVE":
                    continue

                base = _amz_base_sku(sku).upper() if sku else ""
                if base and base in bm_skus:
                    continue  # existe en BM → skip

                summary_0 = summaries[0] if summaries else {}
                asin      = summary_0.get("asin") or ""
                title     = summary_0.get("itemName") or sku or "—"
                price_val = 0.0
                for offer in (item.get("offers") or []):
                    p = offer.get("listingPrice", {})
                    price_val = float(p.get("amount") or 0)
                    break

                sin_bm_all.append({
                    "sku":     sku or "—",
                    "asin":    asin,
                    "title":   title[:80],
                    "price":   price_val,
                    "motivo":  "Sin SKU" if not sku else "SKU no en BM",
                    "sc_url":  (
                        f"https://sellercentral.amazon.com.mx/inventory?searchField=ASIN&searchValue={asin}"
                        if asin else "https://sellercentral.amazon.com.mx/inventory"
                    ),
                })

            _sin_bm_amz_cache[cache_key] = (_time.time(), sin_bm_all)

        except Exception as e:
            logger.exception("[Amazon SinBM] Error")
            return _templates.TemplateResponse(
                request, "partials/amazon_products_sin_bm.html",
                {"error": str(e)[:200], "items": [], "total": 0, "page": 1, "pages": 1,
                 "per_page": per_page, "q": q, "nickname": client.nickname,
                 "marketplace": client.marketplace_name}
            )

    # Filtro búsqueda
    q_lower = q.strip().lower()
    if q_lower:
        sin_bm_all = [
            i for i in sin_bm_all
            if q_lower in i["title"].lower()
            or q_lower in i["sku"].lower()
            or q_lower in (i.get("asin") or "").lower()
        ]

    total  = len(sin_bm_all)
    pages  = max(1, math.ceil(total / per_page))
    page   = min(page, pages)
    start  = (page - 1) * per_page
    page_items = sin_bm_all[start:start + per_page]

    ctx = {
        "items":       page_items,
        "total":       total,
        "page":        page,
        "pages":       pages,
        "per_page":    per_page,
        "q":           q,
        "force":       force,
        "nickname":    client.nickname,
        "marketplace": client.marketplace_name,
    }
    return _templates.TemplateResponse(request, "partials/amazon_products_sin_bm.html", ctx)


# ─────────────────────────────────────────────────────────────────────────────
# REPRICING — Reglas automáticas de precios
# ─────────────────────────────────────────────────────────────────────────────

class RepricingRuleIn(BaseModel):
    sku:       str = "*"
    rule_type: str = "match_buybox"   # match_buybox | beat_buybox | fixed
    beat_pct:  float = 1.0
    min_price: float = 0.0
    max_price: float = 0.0
    enabled:   bool = True


class RepricingApplyIn(BaseModel):
    updates: list[dict]  # [{sku, new_price}]


@router.get("/products/feedback", response_class=JSONResponse)
async def amazon_products_feedback(
    seller_id: Optional[str] = Query(None),
    status: str = Query("pending"),
):
    """Feedback de vendedor (GET_SELLER_FEEDBACK_DATA) de la cuenta Amazon
    ACTIVA — acotado por seller_id, nunca mezclado con otras cuentas."""
    if status not in ("pending", "handled"):
        return JSONResponse({"error": "status inválido"}, status_code=400)
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "No hay cuenta Amazon configurada"}, status_code=401)
    sid = client.seller_id
    from app.services import token_store
    items = await token_store.get_amazon_feedback_tab(sid, status)
    return {"items": items}


@router.get("/products/repricing", response_class=HTMLResponse)
async def amazon_products_repricing(
    request:   Request,
    seller_id: Optional[str] = Query(None),
):
    """Repricing: muestra listings activos con BB status y permite definir/aplicar reglas."""
    import aiosqlite
    from app.services.token_store import DATABASE_PATH

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return _render_no_account(request, "amazon_products_repricing.html")

    try:
        listings, fba_summaries = await asyncio.gather(
            _get_listings_cached(client),
            _get_fba_cached(client),
        )
        fba_index = _build_fba_index(fba_summaries)

        # Build candidate list (active only)
        candidates = []
        for item in listings:
            sku       = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            offers    = item.get("offers", [])
            if _listing_status(summaries) != "ACTIVE":
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

        # Use cached buy box data (don't refetch to avoid rate limits)
        now_ts = _time.time()
        buybox_rows = []
        for c in candidates:
            cache_key = f"{client.seller_id}:{c['sku']}"
            bb_price  = None
            bb_won    = False
            competitors = 0
            if cache_key in _buybox_cache:
                ts, cached = _buybox_cache[cache_key]
                if now_ts - ts < _BUYBOX_TTL * 3:  # longer TTL for repricing view
                    bb_price    = cached.get("bb_price")
                    bb_won      = cached.get("bb_won", False)
                    competitors = cached.get("competitors", 0)

            # Compute suggested price based on rule (applied below after loading rules)
            buybox_rows.append({
                **c,
                "bb_price":    bb_price,
                "bb_won":      bb_won,
                "competitors": competitors,
                "suggested":   None,
            })

        # Load rules from DB
        async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM amz_repricing_rules WHERE seller_id = ? ORDER BY sku",
                (client.seller_id,),
            )
            rules_rows = await cursor.fetchall()

        rules_by_sku: dict = {}
        global_rule:  dict = None
        for r in rules_rows:
            rd = dict(r)
            if rd["sku"] == "*":
                global_rule = rd
            else:
                rules_by_sku[rd["sku"]] = rd

        # Apply rules to compute suggested prices
        for row in buybox_rows:
            rule = rules_by_sku.get(row["sku"]) or global_rule
            if not rule or not rule.get("enabled"):
                continue
            bb  = row["bb_price"]
            our = row["our_price"]
            rt  = rule["rule_type"]

            if rt == "match_buybox" and bb:
                suggested = bb
            elif rt == "beat_buybox" and bb:
                pct = float(rule.get("beat_pct") or 1.0)
                suggested = round(bb * (1 - pct / 100), 2)
            elif rt == "fixed":
                suggested = float(rule.get("min_price") or our)
            else:
                continue

            # Apply floor/ceiling
            if rule.get("min_price") and suggested < rule["min_price"]:
                suggested = rule["min_price"]
            if rule.get("max_price") and suggested > rule["max_price"]:
                suggested = rule["max_price"]

            row["suggested"] = round(suggested, 2) if suggested != our else None
            row["rule_type"] = rt

        ctx = {
            "rows":        buybox_rows,
            "total":       len(buybox_rows),
            "global_rule": global_rule,
            "rules_by_sku": rules_by_sku,
            "seller_id":   client.seller_id,
            "nickname":    client.nickname,
            "marketplace": client.marketplace_name,
        }
        return _templates.TemplateResponse(request, "partials/amazon_products_repricing.html", ctx)

    except Exception as e:
        logger.exception("[Amazon Repricing] Error")
        return _templates.TemplateResponse(
            request, "partials/amazon_products_repricing.html",
            {"error": str(e)[:200], "rows": [], "total": 0,
             "seller_id": seller_id or "", "nickname": "", "marketplace": ""}
        )


@router.post("/products/repricing/rule", response_class=JSONResponse)
async def amazon_save_repricing_rule(
    request:   Request,
    seller_id: Optional[str] = Query(None),
    rule_in:   RepricingRuleIn = None,
):
    """Guarda o actualiza una regla de repricing para seller_id+sku."""
    import aiosqlite
    from app.services.token_store import DATABASE_PATH

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        raise HTTPException(status_code=400, detail="Cuenta no encontrada")

    if rule_in is None:
        body = await request.json()
        rule_in = RepricingRuleIn(**body)

    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        await db.execute(
            """INSERT INTO amz_repricing_rules
               (seller_id, sku, rule_type, beat_pct, min_price, max_price, enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(seller_id, sku) DO UPDATE SET
                   rule_type=excluded.rule_type,
                   beat_pct=excluded.beat_pct,
                   min_price=excluded.min_price,
                   max_price=excluded.max_price,
                   enabled=excluded.enabled,
                   updated_at=CURRENT_TIMESTAMP""",
            (client.seller_id, rule_in.sku, rule_in.rule_type,
             rule_in.beat_pct, rule_in.min_price, rule_in.max_price,
             1 if rule_in.enabled else 0),
        )
        await db.commit()

    return JSONResponse({"ok": True})


@router.post("/products/repricing/apply", response_class=JSONResponse)
async def amazon_apply_repricing(
    request:   Request,
    seller_id: Optional[str] = Query(None),
    body:      RepricingApplyIn = None,
):
    """Aplica precios nuevos a los SKUs seleccionados. Confirmación explícita requerida."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        raise HTTPException(status_code=400, detail="Cuenta no encontrada")

    if body is None:
        raw = await request.json()
        body = RepricingApplyIn(**raw)

    results = []
    for upd in body.updates:
        sku       = upd.get("sku", "")
        new_price = float(upd.get("new_price", 0))
        if not sku or new_price <= 0:
            results.append({"sku": sku, "ok": False, "error": "Datos inválidos"})
            continue
        try:
            resp = await client.update_listing_price(sku, new_price)
            ok   = resp.get("status") == "ACCEPTED" or "issues" not in resp
            results.append({"sku": sku, "ok": ok, "new_price": new_price})
            _buybox_cache.pop(f"{client.seller_id}:{sku}", None)
            await asyncio.sleep(0.5)
        except Exception as e:
            results.append({"sku": sku, "ok": False, "error": str(e)[:100]})

    await _audit(request, "repricing_apply",
                 detail={"seller_id": client.seller_id, "count": len(results)})
    return JSONResponse({"results": results})


# ─────────────────────────────────────────────────────────────────────────────
# DEVOLUCIONES — Historial de reembolsos por SKU
# ─────────────────────────────────────────────────────────────────────────────

_refunds_cache: dict[str, tuple[float, list]] = {}
_REFUNDS_TTL = 1800  # 30 min


@router.get("/products/devoluciones", response_class=HTMLResponse)
async def amazon_products_devoluciones(
    request:   Request,
    seller_id: Optional[str] = Query(None),
    days:      int = Query(30, ge=7, le=90),
    force:     bool = Query(False),
):
    """Devoluciones/reembolsos agrupados por SKU para los últimos N días."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return _render_no_account(request, "amazon_products_devoluciones.html")

    cache_key = f"{client.seller_id}:{days}"
    now_ts    = _time.time()
    detail    = []

    if not force:
        cached = _refunds_cache.get(cache_key)
        if cached:
            ts, detail = cached
            if now_ts - ts < _REFUNDS_TTL:
                detail = detail  # use cached
            else:
                detail = []

    if not detail:
        try:
            detail = await client.get_refunds_detail(days=days)
            _refunds_cache[cache_key] = (_time.time(), detail)
        except Exception as e:
            logger.exception("[Amazon Devoluciones] Error")
            return _templates.TemplateResponse(
                request, "partials/amazon_products_devoluciones.html",
                {"error": str(e)[:200], "by_sku": [], "total_count": 0,
                 "total_amount": 0, "currency": "MXN", "days": days,
                 "seller_id": client.seller_id, "nickname": client.nickname}
            )

    # Aggregate by SKU
    from collections import defaultdict
    sku_map: dict = defaultdict(lambda: {"count": 0, "qty": 0, "amount": 0.0,
                                          "currency": "MXN", "orders": set()})
    total_count  = 0
    total_amount = 0.0
    currency     = "MXN"

    for ev in detail:
        sku = ev.get("sku") or "—"
        sku_map[sku]["count"]  += 1
        sku_map[sku]["qty"]    += ev.get("qty", 0)
        sku_map[sku]["amount"] += ev.get("amount", 0)
        sku_map[sku]["currency"] = ev.get("currency", "MXN")
        sku_map[sku]["orders"].add(ev.get("order_id", ""))
        total_count  += 1
        total_amount += ev.get("amount", 0)
        currency      = ev.get("currency", "MXN")

    by_sku = sorted([
        {
            "sku":      sku,
            "count":    data["count"],
            "qty":      data["qty"],
            "amount":   round(data["amount"], 2),
            "currency": data["currency"],
            "orders":   len(data["orders"]),
        }
        for sku, data in sku_map.items()
    ], key=lambda x: x["amount"], reverse=True)

    # Enrich with listing title from BM/listings cache
    listing_titles: dict = {}
    try:
        listings = await _get_listings_cached(client)
        for item in listings:
            sku_key  = item.get("sku", "")
            summaries = item.get("summaries", [{}])
            title    = (summaries[0] if summaries else {}).get("itemName", "") if summaries else ""
            if sku_key and title:
                listing_titles[sku_key] = title[:70]
    except Exception:
        pass

    for row in by_sku:
        row["title"] = listing_titles.get(row["sku"], "")

    cached_ago = int(now_ts - _refunds_cache.get(cache_key, (now_ts,))[0])

    ctx = {
        "by_sku":       by_sku,
        "total_count":  total_count,
        "total_amount": round(total_amount, 2),
        "currency":     currency,
        "days":         days,
        "force":        force,
        "cached_ago":   cached_ago,
        "seller_id":    client.seller_id,
        "nickname":     client.nickname,
        "marketplace":  client.marketplace_name,
    }
    return _templates.TemplateResponse(request, "partials/amazon_products_devoluciones.html", ctx)


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNÓSTICO — Test lookup de SKU específico (para debug de gaps falsos)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/diag/flx-status", response_class=JSONResponse)
async def diag_flx_status(token: str = "", seller_id: Optional[str] = Query(None)):
    """DIAG 2026-08-24 (Jovan reportó "actualizando stock" sin fin visible en
    Seller Flex): expone el estado real del refresh BG de FLX/Onsite
    (_refresh_flx_stock_bg) -- si sigue corriendo, hace cuánto se completó
    el último ciclo, y cuántos SKUs quedaron SIN dato tras ese ciclo (los
    que probablemente disparan un refresh nuevo en cada carga de página,
    ver `new_flx` en amazon_products_seller_flex/inventario -- un SKU que
    nunca logra cachearse, aunque sea con fulfillable=0, reinicia el ciclo
    completo indefinidamente)."""
    try:
        from app.main import _DIAG_TOKEN
    except Exception:
        _DIAG_TOKEN = None
    if not _DIAG_TOKEN or token != _DIAG_TOKEN:
        return JSONResponse({"error": "token inválido"}, status_code=403)
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    key = client.seller_id
    is_refreshing = key in _flx_stock_refreshing
    cached = _flx_stock_cache.get(key)
    cache_age_s = round(_time.time() - cached[0]) if cached else None
    cached_count = len(cached[1]) if cached else 0
    listings = await _get_listings_cached(client)
    flx_skus = list(dict.fromkeys(item.get("sku", "") for item in listings if _is_amz_onsite(item) and item.get("sku")))
    missing = [s for s in flx_skus if not cached or s not in cached[1]]
    from app.services import token_store as _ts_diag_flx
    db_meta = await _ts_diag_flx.get_amz_flx_sync_meta(key)
    db_rows = await _ts_diag_flx.get_amz_flx_stock_from_db(key)
    return JSONResponse({
        "seller_id": key,
        "is_refreshing_now": is_refreshing,
        "cache_age_s": cache_age_s,
        "cache_ttl_s": _FLX_STOCK_TTL,
        "flx_skus_total": len(flx_skus),
        "flx_skus_cached": cached_count,
        "flx_skus_missing": len(missing),
        "missing_sample": missing[:20],
        "will_retrigger_on_next_load": bool(missing) and not is_refreshing,
        "db_meta": db_meta,
        "db_rows_count": len(db_rows),
    })


@router.get("/diag/check-sku", response_class=JSONResponse)
async def diag_check_sku(
    sku: str = Query(..., description="SKU a verificar en Amazon"),
    seller_id: Optional[str] = Query(None),
):
    """
    Diagnóstico: verifica si un SKU específico existe en Amazon.
    Retorna el resultado completo con marketplace_id, errores y variantes probadas.
    Accesible sin login para diagnóstico desde Railway logs.
    """
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)

    sku_up = sku.strip().upper()
    _AMZ_CHECK_SUFFIXES = ("-FBA", "_FBA_0", "-FBA-0", "-FBM")
    variants = [sku_up] + [sku_up + sfx for sfx in _AMZ_CHECK_SUFFIXES]

    results = []
    for variant in variants:
        try:
            res = await client.get_listing_item(variant)
            if res is not None:
                summaries = res.get("summaries", [{}])
                s = summaries[0] if summaries else {}
                results.append({
                    "variant": variant,
                    "found": True,
                    "asin": s.get("asin", ""),
                    "status": s.get("status", ""),
                    "product_type": res.get("productType", ""),
                })
            else:
                results.append({"variant": variant, "found": False, "reason": "404"})
        except Exception as e:
            results.append({"variant": variant, "found": False, "reason": str(e)[:200]})

    found_any = any(r["found"] for r in results)
    return JSONResponse({
        "sku": sku_up,
        "seller_id": client.seller_id,
        "marketplace_id": client.marketplace_id,
        "marketplace_name": client.marketplace_name,
        "nickname": client.nickname,
        "found": found_any,
        "variants": results,
    })
