"""
Lanzador Inteligente
====================
Scanner nocturno que compara inventario BinManager vs publicaciones activas en MeLi.
Identifica SKUs con stock disponible que no están publicados en cada cuenta,
y presenta los gaps con pricing, datos del producto e IA para generar listings.
"""
import asyncio
import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import DATABASE_PATH
from app.services import token_store
from app.services.meli_client import get_meli_client
from app.services import claude_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lanzar", tags=["lanzar"])

# In-memory video cache — videos combinados (audio+video) listos para servir
# Ephemeral: se pierde al reiniciar Railway (ok, el usuario descarga inmediatamente)
_video_cache: dict[str, bytes] = {}

# BM endpoints (same as sku_inventory.py)
_BM_BASE = "https://binmanager.mitechnologiesinc.com"
_BM_USER = __import__("os").getenv("BM_USER", "jovan.rodriguez@mitechnologiesinc.com")
_BM_PASS = __import__("os").getenv("BM_PASS", "123456")
_BM_INVENTORY_URL = f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
_BM_WAREHOUSE_URL = f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
_BM_AJAX = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
_BM_COMPANY = 1
_BM_CONCEPT = 8
_BM_LOCATIONS = "47,62,68"

# ─── Background scan state ────────────────────────────────────────────────────
_scan_lock = asyncio.Lock()

# In-memory progress — updated during scan, read by /scan-status
_scan_progress: dict = {
    "pct":   0,
    "phase": "idle",
    "label": "",
    "detail": "",
}


async def _bm_login(http: httpx.AsyncClient) -> bool:
    """Login to BinManager and return True on success."""
    try:
        await http.get(f"{_BM_BASE}/User/Index", timeout=15)
        r = await http.post(
            f"{_BM_BASE}/User/LoginUser",
            json={"USRNAME": _BM_USER, "PASS": _BM_PASS},
            headers=_BM_AJAX,
            timeout=15,
        )
        return r.status_code == 200 and bool(r.json().get("Id"))
    except Exception as e:
        logger.error(f"BM login error: {e}")
        return False


async def _bm_fetch_all_skus_with_stock(http: httpx.AsyncClient) -> list[dict]:
    """Fetch all SKUs from BinManager that have stock > 0.
    Paginates through all pages, returns list of product dicts.
    """
    results = []
    page = 1
    per_page = 200
    while True:
        payload = {
            "COMPANYID": _BM_COMPANY,
            "CATEGORYID": None,
            "WAREHOUSEID": None,
            "LOCATIONID": _BM_LOCATIONS,
            "BINID": None,
            "CONDITION": "GRA,GRB,GRC,NEW,ICB,ICC",
            "FORINVENTORY": 0,
            "BUSCADOR": False,
            "BRAND": None,
            "MODEL": None,
            "SIZE": None,
            "LCN": None,
            "CONCEPTID": _BM_CONCEPT,
            "OPENCELL": False,
            "OCCOMPTABILITY": False,
            "NEEDRETAILPRICE": True,
            "NEEDFLOORPRICE": False,
            "NEEDIPS": False,
            "NEEDTIER": False,
            "NEEDFILE": False,
            "NEEDVIRTUALQTY": False,
            "NEEDINCOMINGQTY": False,
            "NEEDAVGCOST": True,
            "NUMBERPAGE": page,
            "RECORDSPAGE": per_page,
            "ORDERBYNAME": None,
            "ORDERBYTYPE": None,
            "PorcentajeFloor": 20,
            "StatusConcept": None,
            "RetailBalance": None,
            "RetailAvailable": None,
            "MaxQty": None,
            "MinQty": 1,
            "NameQty": "QtyTotal",
            "Tier": None,
            "NEEDRETAILPRICEPH": True,
            "TAGS": None,
            "TVL": False,
            "NEEDPORCENTAGE": False,
            "NEEDUPC": True,
            "filterUPC": None,
            "IsComplete": None,
            "NEEDSALES": False,
            "StartDate": None,
            "EndDate": None,
            "SUPPLIERS": None,
            "TAGSNOTIN": None,
            "NEEDLASTREPORTEDSALESPRICE": False,
            "SALESPRICE": None,
            "Jsonfilter": "[]",
            "Arrayfilters_Condition": None,
            "Namefilters_Condition": None,
            "Arrayfilters_Brand": None,
            "Namefilters_Brand": None,
            "Arrayfilters_Model": None,
            "Namefilters_Model": None,
            "Arrayfilters_Size": None,
            "Namefilters_Size": None,
            "Arrayfilters_Category": None,
            "Namefilters_Category": None,
            "Arrayfilters_Tags": None,
            "Namefilters_Tags": None,
            "Arrayfilters_Tags_Exclude": None,
            "Namefilters_Tags_Exlude": None,
            "Arrayfilters_Supplier": None,
            "Namefilters_Supplier": None,
            "SEARCH": None,
        }
        try:
            r = await http.post(_BM_INVENTORY_URL, json=payload, headers=_BM_AJAX, timeout=30)
            # Redireccion a login = sin acceso — salir
            if "User/Index" in str(r.url) or r.status_code == 401:
                logger.warning("BM requiere autenticacion en pagina %d — saliendo", page)
                break
            if r.status_code != 200:
                break
            data = r.json()
            if not data or not isinstance(data, list):
                break
            results.extend(data)
            if len(data) < per_page:
                break
            page += 1
            if page > 500:  # safety limit
                logger.warning("BM pagination hit 500-page safety limit")
                break
        except Exception as e:
            logger.error(f"BM fetch page {page} error: {e}")
            break
    return results


_ALL_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")


def _bm_conditions_for_sku(sku: str) -> str:
    """Condiciones BM según sufijo — igual que main.py y sku_inventory.py."""
    u = (sku or "").upper()
    if u.endswith("-ICB") or u.endswith("-ICC"):
        return "GRA,GRB,GRC,ICB,ICC,NEW"
    return "GRA,GRB,GRC,NEW"


def _wh_name_to_zone(name: str) -> str:
    n = name.lower()
    if "monterrey" in n or "maxx" in n:
        return "mty"
    if "autobot" in n or "cdmx" in n or "ebanistas" in n:
        return "cdmx"
    return "tj"


async def _bm_fetch_warehouse_stock(sku: str, http: httpx.AsyncClient) -> dict:
    """Fetch MTY/CDMX stock para un SKU usando condiciones correctas según sufijo."""
    _BM_AVAIL_URL = f"{_BM_BASE}/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"
    try:
        base = sku.upper()
        for sfx in _ALL_SUFFIXES:
            if base.endswith(sfx):
                base = base[:-len(sfx)]
                break
        conditions = _bm_conditions_for_sku(sku)
        wh_payload = {
            "COMPANYID": _BM_COMPANY,
            "SKU": base,
            "WarehouseID": None,
            "LocationID": _BM_LOCATIONS,
            "BINID": None,
            "Condition": conditions,
            "ForInventory": 0,
            "SUPPLIERS": None,
        }
        avail_payload = {
            "COMPANYID": _BM_COMPANY,
            "TYPEINVENTORY": 0,
            "WAREHOUSEID": None,
            "LOCATIONID": _BM_LOCATIONS,
            "BINID": None,
            "PRODUCTSKU": base,
            "CONDITION": conditions,
            "SUPPLIERS": None,
            "LCN": None,
            "SEARCH": base,
        }
        r_wh, r_avail = await asyncio.gather(
            http.post(_BM_WAREHOUSE_URL, json=wh_payload, headers=_BM_AJAX, timeout=15),
            http.post(_BM_AVAIL_URL, json=avail_payload, headers=_BM_AJAX, timeout=15),
            return_exceptions=True,
        )
        mty = cdmx = 0
        if not isinstance(r_wh, Exception) and r_wh.status_code == 200:
            for row in (r_wh.json() or []):
                qty = row.get("QtyTotal", 0) or 0
                zone = _wh_name_to_zone(row.get("WarehouseName") or "")
                if zone == "mty":
                    mty += qty
                elif zone == "cdmx":
                    cdmx += qty
        avail = 0
        if not isinstance(r_avail, Exception) and r_avail.status_code == 200:
            avail = sum(row.get("Available", 0) or 0 for row in (r_avail.json() or []))
        return {"mty": mty, "cdmx": cdmx, "avail": avail or mty + cdmx}
    except Exception:
        return {"mty": 0, "cdmx": 0, "avail": 0}


async def _get_meli_sku_set(user_id: str, nickname: str) -> tuple[set[str], dict[str, list[str]], dict[str, list[dict]]]:
    """Return (all_sku_set, inactive_sku_to_item_ids, active_prices_map).

    all_sku_set: base SKUs published in ML (any status — active, paused, inactive, etc.)
    inactive_sku_to_item_ids: base_sku → [item_id, ...] for items currently inactive/paused.
      These are candidates for reactivation when BM has stock.
    active_prices_map: base_sku → [{item_id, price, title}, ...] for active items.
    Uses item_sku_cache to skip re-fetching already-known items.
    """
    import re as _re

    _ALL_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")

    def _base(sku: str) -> str:
        u = sku.upper()
        for s in _ALL_SUFFIXES:
            if u.endswith(s):
                return u[:-len(s)]
        return u

    def _skus_from_body(body: dict) -> list[str]:
        """Extract all SKUs from a ML item body dict."""
        found = []
        # seller_custom_field (most common — SKU set by seller at listing time)
        scf = (body.get("seller_custom_field") or "").strip().upper()
        if scf:
            for part in _re.split(r'\s*[/+,]\s*', scf):
                if part.strip():
                    found.append(part.strip())
        # attributes array → SELLER_SKU id
        for a in (body.get("attributes") or []):
            if a.get("id") == "SELLER_SKU":
                v = (a.get("value_name") or "").strip().upper()
                if v:
                    found.append(v)
        # variations (multi-variant listings)
        for var in (body.get("variations") or []):
            vscf = (var.get("seller_custom_field") or "").strip().upper()
            if vscf:
                found.append(vscf)
            for a in (var.get("attributes") or []):
                if a.get("id") == "SELLER_SKU":
                    v = (a.get("value_name") or "").strip().upper()
                    if v:
                        found.append(v)
        return found

    client = await get_meli_client(user_id=user_id)
    if not client:
        return set(), {}, {}

    try:
        # ── Step 1: Collect all item IDs ──────────────────────────────────────
        # Must query all statuses separately — ML /items/search only returns
        # active+paused by default. Inactive (sin stock) and closed items are
        # excluded unless explicitly requested with status=inactive/closed.
        # SNTV007278-style items go inactive when stock hits 0 but still exist.
        _ML_STATUSES = ("active", "paused", "inactive", "closed", "under_review", "not_yet_active")

        async def _fetch_ids_for_status(status: str) -> list[str]:
            ids_out: list[str] = []
            off = 0
            while True:
                try:
                    resp = await client.get(
                        f"/users/{user_id}/items/search",
                        params={"limit": 100, "offset": off, "status": status},
                    )
                    batch = resp.get("results", [])
                    if not batch:
                        break
                    ids_out.extend(str(i) for i in batch)
                    paging = resp.get("paging", {})
                    total  = paging.get("total", 0)
                    if len(batch) < 100 or off + 100 >= total:
                        break
                    off += 100
                    if off >= 10000:
                        logger.warning(f"{nickname}: items/search status={status} offset cap at 10k")
                        break
                except Exception as e:
                    logger.warning(f"items/search error {nickname} status={status} offset={off}: {e}")
                    break
            return ids_out

        seen_ids: set[str] = set()
        item_ids: list[str] = []
        # Track which item IDs came from inactive/paused status (reactivation candidates)
        _REACTIVATABLE_STATUSES = {"inactive", "paused", "closed"}
        reactivatable_ids: set[str] = set()
        active_ids: set[str] = set()

        for _status in _ML_STATUSES:
            batch_ids = await _fetch_ids_for_status(_status)
            for iid in batch_ids:
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    item_ids.append(iid)
                if _status in _REACTIVATABLE_STATUSES:
                    reactivatable_ids.add(iid)
                if _status == "active":
                    active_ids.add(iid)
            logger.info(f"{nickname}: status={_status} → {len(batch_ids)} items")

        logger.info(f"{nickname}: {len(item_ids)} item IDs total across all statuses, {len(reactivatable_ids)} reactivatable")
        if not item_ids:
            return set(), {}, {}

        # ── Step 2: Check cache ───────────────────────────────────────────────
        from app.services.token_store import get_cached_skus, save_skus_cache
        cached       = await get_cached_skus(item_ids)
        cached_skus  = {_base(v) for v in cached.values() if v}
        needs_fetch  = [iid for iid in item_ids if iid not in cached]
        logger.info(f"{nickname}: {len(cached)} cached SKUs, {len(needs_fetch)} items to fetch")

        # ── Step 3: Batch-fetch uncached items ───────────────────────────────
        # CRITICAL: Do NOT add ?attributes= filter here.
        # The ML multi-item endpoint can silently drop seller_custom_field
        # when attribute filtering is active, causing false "sin publicar" gaps.
        new_entries: list[dict] = []
        sku_set = set(cached_skus)
        # Map: base_sku → list of item_ids that are reactivatable (inactive/paused)
        inactive_sku_to_items: dict[str, list[str]] = {}
        # Map: base_sku → list of {item_id, price, title} for active items
        active_prices_map: dict[str, list[dict]] = {}
        # Track which item IDs had their SKU successfully extracted (for fallback)
        extracted_iids: set[str] = set()

        def _process_item_body(body: dict, iid: str) -> None:
            """Extract SKU and metadata from a single item body dict. Mutates outer scope collections."""
            skus  = _skus_from_body(body)
            title = body.get("title", "")
            for raw_sku in skus:
                base = _base(raw_sku)
                sku_set.add(base)
                if iid:
                    new_entries.append({"item_id": iid, "user_id": user_id, "sku": raw_sku})
                    if iid in reactivatable_ids:
                        inactive_sku_to_items.setdefault(base, [])
                        if iid not in inactive_sku_to_items[base]:
                            inactive_sku_to_items[base].append(iid)
                    if iid in active_ids:
                        price = float(body.get("price") or 0)
                        pics  = len(body.get("pictures") or [])
                        attrs = body.get("attributes") or []
                        has_gtin  = any(a.get("id") in ("GTIN", "EAN", "UPC") for a in attrs)
                        has_brand = any(a.get("id") == "BRAND" for a in attrs)
                        title_score  = min(len(title), 60) / 60 * 25
                        pics_score   = min(pics, 6) / 6 * 25
                        attr_score   = (10 if has_brand else 0) + (15 if has_gtin else 0)
                        price_score  = 25 if price > 0 else 0
                        quality_score = int(title_score + pics_score + attr_score + price_score)
                        if price > 0:
                            active_prices_map.setdefault(base, [])
                            if not any(e["item_id"] == iid for e in active_prices_map[base]):
                                active_prices_map[base].append({
                                    "item_id": iid, "price": price, "title": title,
                                    "pics": pics, "has_gtin": has_gtin, "has_brand": has_brand,
                                    "quality_score": quality_score,
                                })
            if skus:
                extracted_iids.add(iid)

        for i in range(0, len(needs_fetch), 20):
            batch = needs_fetch[i:i + 20]
            try:
                details = await client.get(f"/items?ids={','.join(batch)}")
                items_list = details if isinstance(details, list) else []
                batch_found = 0
                for entry in items_list:
                    if not isinstance(entry, dict):
                        continue
                    body = entry.get("body") or {}
                    if not isinstance(body, dict):
                        continue
                    iid = str(body.get("id", ""))
                    if not iid:
                        continue
                    _process_item_body(body, iid)
                    if iid in extracted_iids:
                        batch_found += 1

                if batch_found == 0 and items_list:
                    sample = items_list[0].get("body", {}) if items_list else {}
                    logger.warning(
                        f"{nickname}: batch {i//20} — {len(items_list)} items, 0 with SKU. "
                        f"Sample keys: {list(sample.keys())[:10]}"
                    )
            except Exception as e:
                logger.warning(f"{nickname}: batch {i//20} fetch error: {e}")

        # ── Step 3b: Individual fallback for items where batch returned no SKU ──
        # ML's multi-item endpoint can silently omit seller_custom_field for
        # inactive/closed listings. Individual GET /items/{id} is more reliable.
        no_sku_iids = [iid for iid in needs_fetch if iid not in extracted_iids]
        if no_sku_iids:
            logger.info(
                f"{nickname}: {len(no_sku_iids)} items with no SKU from batch — "
                f"retrying individually (max 300)"
            )
            for iid in no_sku_iids[:300]:
                try:
                    body = await client.get(f"/items/{iid}")
                    if isinstance(body, dict) and body.get("id"):
                        _process_item_body(body, iid)
                        if iid in extracted_iids:
                            logger.info(f"{nickname}: recovered SKU for {iid} via individual fetch")
                except Exception as e:
                    logger.warning(f"{nickname}: individual fetch error for {iid}: {e}")

        # ── Step 3c: /attributes fallback — last resort ───────────────────────
        # GET /items/{id}/attributes returns the attributes array independently.
        # This endpoint can return SELLER_SKU even when the main body endpoint
        # omits seller_custom_field (known ML API quirk for inactive items).
        still_no_sku = [iid for iid in no_sku_iids if iid not in extracted_iids]
        if still_no_sku:
            logger.info(
                f"{nickname}: {len(still_no_sku)} items still with no SKU — "
                f"trying /attributes endpoint (max 200)"
            )
            for iid in still_no_sku[:200]:
                try:
                    attrs = await client.get(f"/items/{iid}/attributes")
                    if isinstance(attrs, list):
                        for a in attrs:
                            if not isinstance(a, dict):
                                continue
                            if a.get("id") == "SELLER_SKU":
                                v = (a.get("value_name") or "").strip().upper()
                                if v:
                                    base = _base(v)
                                    sku_set.add(base)
                                    new_entries.append({"item_id": iid, "user_id": user_id, "sku": v})
                                    extracted_iids.add(iid)
                                    if iid in reactivatable_ids:
                                        inactive_sku_to_items.setdefault(base, [])
                                        if iid not in inactive_sku_to_items[base]:
                                            inactive_sku_to_items[base].append(iid)
                                    logger.info(
                                        f"{nickname}: recovered SKU {v} for {iid} "
                                        f"via /attributes endpoint"
                                    )
                except Exception as e:
                    logger.warning(f"{nickname}: /attributes fetch error for {iid}: {e}")

        # Also map cached items that are reactivatable
        for iid, raw_sku in cached.items():
            if iid in reactivatable_ids and raw_sku:
                base = _base(raw_sku)
                inactive_sku_to_items.setdefault(base, [])
                if iid not in inactive_sku_to_items[base]:
                    inactive_sku_to_items[base].append(iid)

        logger.info(f"{nickname}: {len(sku_set)} unique base SKUs, {len(inactive_sku_to_items)} reactivatable SKUs, {len(active_prices_map)} SKUs with active prices")

        # ── Step 4: Persist new mappings to cache ─────────────────────────────
        if new_entries:
            try:
                await save_skus_cache(new_entries)
            except Exception:
                pass

        return sku_set, inactive_sku_to_items, active_prices_map
    finally:
        await client.close()


def _priority_score(stock_total: int, retail_usd: float, cost_usd: float) -> int:
    """Score 0–100: más stock y mayor margen = mayor prioridad."""
    stock_score = min(stock_total / 5, 40)   # max 40 pts (200+ units)
    price_score  = min(retail_usd / 25, 30)  # max 30 pts ($750+ USD)
    margin = (retail_usd - cost_usd) / retail_usd if retail_usd > 0 else 0
    margin_score = min(margin * 100, 30)     # max 30 pts (100% margin)
    return int(stock_score + price_score + margin_score)


def _prog(pct: int, phase: str, label: str, detail: str = "") -> None:
    """Actualiza progreso in-memory del scan."""
    _scan_progress.update({"pct": pct, "phase": phase, "label": label, "detail": detail})


async def _run_gap_scan():
    """Core scan: compare BM inventory vs MeLi per account, store gaps."""
    if _scan_lock.locked():
        return
    async with _scan_lock:
        logger.info("BM gap scan iniciando...")
        _prog(0, "starting", "Iniciando scan...", "")
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE bm_gap_scan_status SET status='running', started_at=?, finished_at=NULL, error=NULL WHERE id=1",
                (datetime.utcnow().isoformat(),)
            )
            await db.commit()

        try:
            # 1. Get all MeLi accounts
            _prog(3, "accounts", "Cargando cuentas MeLi...", "")
            accounts = await token_store.get_all_tokens()
            if not accounts:
                raise Exception("No MeLi accounts configured")

            # 2a. Get real USD→MXN FX rate from first MeLi account
            _prog(6, "fx", "Obteniendo tipo de cambio USD→MXN...", "")
            fx = 17.5  # fallback
            if accounts:
                from app.services.meli_client import _active_user_id as _ctx_fx
                first_uid = accounts[0]["user_id"]
                token_fx = _ctx_fx.set(first_uid)
                try:
                    fx_client = await get_meli_client()
                    if fx_client:
                        fx_data = await fx_client.get(
                            "/currency_conversions/search",
                            params={"from": "USD", "to": "MXN"}
                        )
                        fx = float(fx_data.get("ratio", 17.5) or 17.5)
                        logger.info(f"FX USD→MXN: {fx}")
                except Exception as e:
                    logger.warning(f"FX fetch failed, usando 17.5: {e}")
                finally:
                    _ctx_fx.reset(token_fx)

            # 2b. Fetch all BM SKUs with stock — must login first
            _prog(10, "bm_login", "Conectando a BinManager...", "")
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as bm_http:
                bm_logged_in = await _bm_login(bm_http)
                if not bm_logged_in:
                    raise Exception("BinManager login failed — verifica BM_USER/BM_PASS")
                _prog(15, "bm_fetch", "Descargando inventario BinManager...", "Página 1...")
                bm_products = await _bm_fetch_all_skus_with_stock(bm_http)

            logger.info(f"BM gap scan: {len(bm_products)} SKUs con stock en BM")
            _prog(28, "bm_done", "Inventario BM cargado", f"{len(bm_products)} SKUs con stock")

            # Build BM map: base_sku → product info
            _ALL_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")
            def _base(sku):
                u = (sku or "").upper()
                for s in _ALL_SUFFIXES:
                    if u.endswith(s):
                        return u[:-len(s)]
                return u

            def _bm_qty(prod: dict) -> int:
                """BM returns stock in TotalQty (global inventory endpoint).
                Fallbacks for other endpoint variants: AvailableQTY, QtyTotal, QTY."""
                v = (prod.get("TotalQty") or prod.get("AvailableQTY")
                     or prod.get("QtyTotal") or prod.get("QTY") or 0)
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0

            bm_map = {}
            for prod in bm_products:
                raw_sku = prod.get("SKU") or ""
                if not raw_sku:
                    continue
                base = _base(raw_sku)
                qty = _bm_qty(prod)
                if qty <= 0:
                    continue
                # Keep the one with most stock if duplicate base SKU
                if base not in bm_map or qty > _bm_qty(bm_map[base]):
                    bm_map[base] = prod

            # ── FASE 1: Recolectar datos ML de TODAS las cuentas ──────────────
            # Clave: los gaps se calculan contra el set GLOBAL (union de todas
            # las cuentas). Un SKU publicado en Autobot NO debe aparecer como
            # gap en Apantállate y viceversa. BM es inventario compartido.
            now_iso = datetime.utcnow().isoformat()

            account_ml_data: dict = {}  # user_id → {meli_skus, inactive_map, active_prices, nickname}
            global_meli_skus: set = set()  # union de TODAS las cuentas

            for _ai, account in enumerate(accounts):
                user_id  = account["user_id"]
                nickname = account.get("nickname") or user_id
                _pct_ml = 30 + int(_ai / max(len(accounts), 1) * 15)
                _prog(_pct_ml, "ml_fetch", f"Leyendo publicaciones ML — {nickname}...",
                      f"Cuenta {_ai+1} de {len(accounts)}")
                logger.info(f"[Fase1] Obteniendo items ML de {nickname} ({user_id})...")
                skus, inactive_map, active_prices = await _get_meli_sku_set(user_id, nickname)
                account_ml_data[user_id] = {
                    "nickname":      nickname,
                    "meli_skus":     skus,
                    "inactive_map":  inactive_map,
                    "active_prices": active_prices,
                }
                global_meli_skus |= skus
                logger.info(f"  {nickname}: {len(skus)} SKUs en ML, {len(inactive_map)} reactivables")

            logger.info(
                f"[Fase1] Completo — {len(global_meli_skus)} SKUs publicados en total "
                f"({len(accounts)} cuentas). BM tiene {len(bm_map)} SKUs con stock."
            )
            _prog(45, "gaps_calc", "Calculando gaps de inventario...",
                  f"{len(global_meli_skus)} SKUs en ML, {len(bm_map)} en BM")

            # ── FASE 2: Calcular gaps globales y guardar por cuenta ────────────
            # Gap = SKU en BM con stock que NO está publicado en NINGUNA cuenta.
            current_bm_skus = set(bm_map.keys())

            global_gaps_base = []  # datos base sin user_id/nickname
            for base_sku, prod in bm_map.items():
                if base_sku in global_meli_skus:
                    continue  # publicado en alguna cuenta → no es gap
                retail    = float(prod.get("RetailPrice", 0) or prod.get("LastRetailPricePurchaseHistory", 0) or 0)
                stock     = _bm_qty(prod)
                score     = _priority_score(stock, retail, 0)
                # Precio sugerido: retail_usd × 18 (FX) × 1.20 (20% margen)
                suggested = round(retail * 18 * 1.20, 0) if retail > 0 else 0
                # Costo BM en MXN = retail_usd × 18 (el retail de BM ES nuestro costo de adquisición)
                cost_mxn  = round(retail * 18, 0) if retail > 0 else 0
                global_gaps_base.append({
                    "sku":               base_sku,
                    "product_title":     prod.get("Title", "") or "",
                    "brand":             prod.get("Brand", "") or "",
                    "model":             prod.get("Model", "") or "",
                    "image_url":         prod.get("ImageURL", "") or "",
                    "category":          prod.get("CategoryName", "") or "",
                    "upc":               prod.get("UPC", "") or prod.get("Upc", "") or "",
                    "size":              prod.get("Size", "") or prod.get("ScreenSize", "") or "",
                    "stock_total":       stock,
                    "stock_mty":         0,
                    "stock_cdmx":        0,
                    "retail_price_usd":  retail,
                    "cost_usd":          retail,   # costo = retail BM (nuestro precio de adquisición)
                    "priority_score":    score,
                    "suggested_price_mxn": suggested,
                    "cost_price_mxn":    cost_mxn,
                    "last_scan":         now_iso,
                })

            total_gaps_before_verify = len(global_gaps_base)
            logger.info(f"[Fase2] {total_gaps_before_verify} gap candidates antes de verificación seller_sku")

            # ── FASE 2b: Verificación seller_sku — safety net definitivo ──────
            # La Fase 1 puede fallar en extraer SKUs de items inactivos/cerrados
            # porque el endpoint /items?ids= omite seller_custom_field para esos.
            # Aquí hacemos la búsqueda INVERSA: para cada candidato a gap,
            # buscamos ese SKU directamente en ML. Si ML lo tiene publicado en
            # CUALQUIER estado en CUALQUIER cuenta → no es gap.
            _verify_sem = asyncio.Semaphore(5)  # max 5 búsquedas concurrentes por cuenta

            # Build search SKU set per gap: base SKU + raw BM SKU (might have suffix like -NEW)
            # If ML has seller_custom_field="SNTV007910-NEW", searching for base "SNTV007910" misses it.
            gap_sku_variants: dict[str, set[str]] = {}  # base_sku → {sku, raw_sku, ...}
            for g in global_gaps_base:
                base = g["sku"]
                variants = {base}
                raw_bm = (bm_map.get(base, {}).get("SKU") or base).upper().strip()
                if raw_bm and raw_bm != base:
                    variants.add(raw_bm)
                gap_sku_variants[base] = variants

            async def _sku_exists_in_account(base_sku: str, uid: str, cli) -> bool:
                """True si el SKU (o alguna variante) existe en ML para esta cuenta.
                Si encontrado, cachea el item_id→sku para que futuros scans usen Phase 1.
                """
                async with _verify_sem:
                    try:
                        skus_to_try = list(gap_sku_variants.get(base_sku, {base_sku}))
                        # For each SKU variant, search all relevant statuses
                        search_calls = []
                        for s in skus_to_try:
                            search_calls += [
                                cli.get(f"/users/{uid}/items/search",
                                        params={"seller_sku": s, "limit": 3}),
                                cli.get(f"/users/{uid}/items/search",
                                        params={"seller_sku": s, "status": "inactive", "limit": 3}),
                                cli.get(f"/users/{uid}/items/search",
                                        params={"seller_sku": s, "status": "closed", "limit": 3}),
                            ]
                        results = await asyncio.gather(*search_calls, return_exceptions=True)
                        for r, s in zip(results, [s for s in skus_to_try for _ in range(3)]):
                            if isinstance(r, dict) and r.get("results"):
                                # Self-heal: cache item_id→sku so Phase 1 works next scan
                                entries = [
                                    {"item_id": str(iid), "user_id": uid, "sku": s}
                                    for iid in r["results"][:3]
                                ]
                                try:
                                    await token_store.save_skus_cache(entries)
                                except Exception:
                                    pass
                                return True
                        return False
                    except Exception:
                        return False

            verified_not_gaps: set[str] = set()
            gap_skus_list = [g["sku"] for g in global_gaps_base]

            logger.info(
                f"[Fase2b] Verificando {len(gap_skus_list)} candidates via seller_sku "
                f"en {len(accounts)} cuentas..."
            )
            _verify_total = max(len(gap_skus_list) * len(accounts), 1)
            _verify_done  = 0

            async def _sku_exists_tracked(sku, uid, cli):
                nonlocal _verify_done
                result = await _sku_exists_in_account(sku, uid, cli)
                _verify_done += 1
                _pct_v = 50 + int((_verify_done / _verify_total) * 38)
                _prog(_pct_v, "verifying",
                      f"Verificando SKUs en MeLi ({_verify_done}/{_verify_total})...",
                      f"{_verify_done} verificados de {_verify_total}")
                return result

            _prog(50, "verifying",
                  f"Verificando {len(gap_skus_list)} SKUs en MeLi...",
                  f"0/{_verify_total} verificados")
            for _acct in accounts:
                _uid = _acct["user_id"]
                _nick = _acct.get("nickname") or _uid
                _cli = await get_meli_client(user_id=_uid)
                if not _cli:
                    continue
                try:
                    _tasks = [_sku_exists_tracked(sku, _uid, _cli) for sku in gap_skus_list]
                    _flags = await asyncio.gather(*_tasks, return_exceptions=True)
                    for sku, flag in zip(gap_skus_list, _flags):
                        if flag is True:
                            verified_not_gaps.add(sku)
                            logger.info(f"[Fase2b] {sku} encontrado en {_nick} via seller_sku — no es gap")
                finally:
                    await _cli.close()

            if verified_not_gaps:
                logger.info(
                    f"[Fase2b] {len(verified_not_gaps)} SKUs removidos de gaps "
                    f"(encontrados via seller_sku): {sorted(verified_not_gaps)[:20]}"
                )
                global_gaps_base = [g for g in global_gaps_base if g["sku"] not in verified_not_gaps]
                global_meli_skus |= verified_not_gaps  # asegurar limpieza en Fase 2

            total_gaps = len(global_gaps_base)
            logger.info(f"[Fase2] {total_gaps} gaps reales confirmados (de {total_gaps_before_verify} candidates)")
            _prog(88, "saving", "Guardando resultados en base de datos...",
                  f"{total_gaps} gaps confirmados")

            # Guardar los mismos gaps para cada cuenta (el lanzamiento va a la cuenta activa)
            # + datos per-cuenta: reactivaciones, precios, calidad, competencia
            for account in accounts:
                user_id  = account["user_id"]
                acct     = account_ml_data[user_id]
                nickname = acct["nickname"]
                meli_skus        = acct["meli_skus"]
                inactive_sku_map = acct["inactive_map"]
                active_prices_map = acct["active_prices"]

                async with aiosqlite.connect(DATABASE_PATH) as db:

                    # ── Gaps: purgar obsoletos y upsert ────────────────────────
                    # 1. Eliminar gaps cuyo SKU ya no tiene stock en BM
                    await db.execute(
                        """DELETE FROM bm_sku_gaps
                           WHERE user_id=? AND status='unlaunched'
                           AND sku NOT IN ({})""".format(
                            ",".join("?" * len(current_bm_skus)) if current_bm_skus else "''"
                        ),
                        [user_id] + list(current_bm_skus)
                    )
                    # 2. Eliminar gaps que ahora están publicados en CUALQUIER cuenta
                    if global_meli_skus:
                        for chunk_start in range(0, len(global_meli_skus), 500):
                            chunk = list(global_meli_skus)[chunk_start:chunk_start + 500]
                            await db.execute(
                                """DELETE FROM bm_sku_gaps
                                   WHERE user_id=? AND status='unlaunched'
                                   AND sku IN ({})""".format(",".join("?" * len(chunk))),
                                [user_id] + chunk
                            )
                    # 3. Upsert gaps globales para esta cuenta
                    for g_base in global_gaps_base:
                        g = {**g_base, "user_id": user_id, "nickname": nickname}
                        await db.execute("""
                            INSERT INTO bm_sku_gaps
                                (user_id, nickname, sku, product_title, brand, model,
                                 image_url, category, upc, size,
                                 stock_total, stock_mty, stock_cdmx,
                                 retail_price_usd, cost_usd, priority_score,
                                 suggested_price_mxn, cost_price_mxn, last_scan)
                            VALUES
                                (:user_id,:nickname,:sku,:product_title,:brand,:model,
                                 :image_url,:category,:upc,:size,
                                 :stock_total,:stock_mty,:stock_cdmx,
                                 :retail_price_usd,:cost_usd,:priority_score,
                                 :suggested_price_mxn,:cost_price_mxn,:last_scan)
                            ON CONFLICT(user_id, sku) DO UPDATE SET
                                nickname=excluded.nickname,
                                product_title=excluded.product_title,
                                brand=excluded.brand, model=excluded.model,
                                image_url=excluded.image_url, category=excluded.category,
                                upc=excluded.upc, size=excluded.size,
                                stock_total=excluded.stock_total,
                                stock_mty=excluded.stock_mty, stock_cdmx=excluded.stock_cdmx,
                                retail_price_usd=excluded.retail_price_usd,
                                cost_usd=excluded.cost_usd,
                                priority_score=excluded.priority_score,
                                suggested_price_mxn=excluded.suggested_price_mxn,
                                cost_price_mxn=excluded.cost_price_mxn,
                                last_scan=excluded.last_scan
                            WHERE bm_sku_gaps.status != 'ignored'
                        """, g)

                    # ── Reactivaciones (per-cuenta) ────────────────────────────
                    reactivation_count = 0
                    await db.execute("DELETE FROM bm_reactivations WHERE user_id=?", (user_id,))
                    for base_sku, item_ids_list in inactive_sku_map.items():
                        prod = bm_map.get(base_sku)
                        if not prod:
                            continue
                        stock = _bm_qty(prod)
                        if stock <= 0:
                            continue
                        retail    = float(prod.get("RetailPrice", 0) or prod.get("LastRetailPricePurchaseHistory", 0) or 0)
                        suggested = round(retail * 18 * 1.20, 0) if retail > 0 else 0
                        title     = prod.get("Title", "") or ""
                        for iid in item_ids_list:
                            await db.execute("""
                                INSERT OR REPLACE INTO bm_reactivations
                                    (user_id, nickname, sku, item_id, product_title,
                                     stock_bm, retail_price_usd, suggested_price_mxn, last_scan)
                                VALUES (?,?,?,?,?,?,?,?,?)
                            """, (user_id, nickname, base_sku, iid, title,
                                  stock, retail, suggested, now_iso))
                            reactivation_count += 1
                    logger.info(f"  {nickname}: {reactivation_count} candidatos de reactivacion")

                    # ── Alertas de precio (per-cuenta) ────────────────────────
                    PRICE_DRIFT_THRESHOLD = 0.10
                    price_alert_count = 0
                    await db.execute("DELETE FROM ml_price_alerts WHERE user_id=?", (user_id,))
                    for base_sku, item_list in active_prices_map.items():
                        prod = bm_map.get(base_sku)
                        if not prod:
                            continue
                        retail    = float(prod.get("RetailPrice", 0) or prod.get("LastRetailPricePurchaseHistory", 0) or 0)
                        suggested = round(retail * 18 * 1.20, 0)
                        if retail <= 0 or suggested <= 0:
                            continue
                        for item_info in item_list:
                            ml_price = item_info.get("price", 0)
                            if ml_price <= 0:
                                continue
                            diff_pct = (ml_price - suggested) / suggested
                            if abs(diff_pct) < PRICE_DRIFT_THRESHOLD:
                                continue
                            title = item_info.get("title", "") or prod.get("Title", "")
                            await db.execute("""
                                INSERT OR REPLACE INTO ml_price_alerts
                                    (user_id, nickname, sku, item_id, product_title,
                                     ml_price, bm_suggested_mxn, diff_pct, last_scan)
                                VALUES (?,?,?,?,?,?,?,?,?)
                            """, (user_id, nickname, base_sku, item_info["item_id"], title,
                                  ml_price, suggested, round(diff_pct * 100, 1), now_iso))
                            price_alert_count += 1
                    logger.info(f"  {nickname}: {price_alert_count} alertas de precio")

                    # ── Calidad de listings (per-cuenta) ──────────────────────
                    quality_count = 0
                    await db.execute("DELETE FROM ml_listing_quality WHERE user_id=?", (user_id,))
                    for base_sku, item_list in active_prices_map.items():
                        for item_info in item_list:
                            prod  = bm_map.get(base_sku) or {}
                            title = item_info.get("title", "") or prod.get("Title", "")
                            await db.execute("""
                                INSERT OR REPLACE INTO ml_listing_quality
                                    (user_id, nickname, sku, item_id, product_title, ml_price,
                                     quality_score, pics_count, has_gtin, has_brand, title_len, last_scan)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            """, (user_id, nickname, base_sku, item_info["item_id"], title,
                                  item_info.get("price", 0), item_info.get("quality_score", 0),
                                  item_info.get("pics", 0), 1 if item_info.get("has_gtin") else 0,
                                  1 if item_info.get("has_brand") else 0, len(title), now_iso))
                            quality_count += 1
                    logger.info(f"  {nickname}: {quality_count} scores de calidad")

                    # ── Alertas de competencia (per-cuenta) ───────────────────
                    comp_alert_count = 0
                    await db.execute("DELETE FROM ml_competition_alerts WHERE user_id=?", (user_id,))
                    for base_sku, item_list in active_prices_map.items():
                        prod       = bm_map.get(base_sku) or {}
                        comp_price = float(prod.get("CompetitorPrice", 0) or 0)
                        if comp_price <= 0:
                            continue
                        for item_info in item_list:
                            ml_price = item_info.get("price", 0)
                            if ml_price <= 0:
                                continue
                            diff_pct = (ml_price - comp_price) / comp_price * 100
                            if diff_pct <= 15:
                                continue
                            title = item_info.get("title", "") or prod.get("Title", "")
                            await db.execute("""
                                INSERT OR REPLACE INTO ml_competition_alerts
                                    (user_id, nickname, sku, item_id, product_title,
                                     ml_price, competitor_price, diff_pct, last_scan)
                                VALUES (?,?,?,?,?,?,?,?,?)
                            """, (user_id, nickname, base_sku, item_info["item_id"], title,
                                  ml_price, comp_price, round(diff_pct, 1), now_iso))
                            comp_alert_count += 1
                    logger.info(f"  {nickname}: {comp_alert_count} alertas de competencia")

                    await db.commit()

            # Update scan status
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.execute(
                    """UPDATE bm_gap_scan_status SET
                       status='done', finished_at=?, total_skus=?, gaps_found=?
                       WHERE id=1""",
                    (datetime.utcnow().isoformat(), len(bm_map), total_gaps)
                )
                await db.commit()
            _prog(100, "done", "Scan completado", f"{total_gaps} gaps encontrados")
            logger.info(f"BM gap scan completado: {total_gaps} gaps en {len(accounts)} cuentas")

        except BaseException as e:
            tb = traceback.format_exc()
            err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.error(f"BM gap scan error: {tb}")
            _prog(_scan_progress["pct"], "error", f"Error: {err_msg[:80]}", "")
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.execute(
                    "UPDATE bm_gap_scan_status SET status='error', finished_at=?, error=? WHERE id=1",
                    (datetime.utcnow().isoformat(), err_msg)
                )
                await db.commit()


async def _nightly_gap_scan_loop():
    """Runs nightly at 3am Mexico time (UTC-6 = 9am UTC). Runs immediately at startup too."""
    await asyncio.sleep(30)  # short startup delay
    # Run immediately on first start to populate data
    asyncio.create_task(_run_gap_scan())
    while True:
        now_utc = datetime.now(timezone.utc)
        # Target: 9:00 UTC = 3:00 AM Mexico City (CST)
        next_run = now_utc.replace(hour=9, minute=0, second=0, microsecond=0)
        if now_utc >= next_run:
            from datetime import timedelta as _td
            next_run = next_run + _td(days=1)
        wait_secs = (next_run - now_utc).total_seconds()
        logger.info(f"BM gap scan: próximo scan en {wait_secs/3600:.1f}h")
        await asyncio.sleep(wait_secs)
        asyncio.create_task(_run_gap_scan())


def start_gap_scan_loop():
    """Called from lifespan to start the nightly background loop."""
    asyncio.create_task(_nightly_gap_scan_loop())


# ─── API Endpoints ────────────────────────────────────────────────────────────

@router.get("/filters")
async def get_gap_filters(request: Request):
    """Devuelve categorías y marcas únicas disponibles en los gaps de la cuenta activa."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Categories with count
        cur = await db.execute(
            """SELECT category, COUNT(*) as cnt, SUM(stock_total) as total_stock
               FROM bm_sku_gaps WHERE user_id=? AND status='unlaunched' AND category != ''
               GROUP BY category ORDER BY cnt DESC""",
            (user_id,)
        )
        cats = [{"category": r[0], "count": r[1], "total_stock": r[2] or 0}
                for r in await cur.fetchall()]

        # Brands with count
        cur2 = await db.execute(
            """SELECT brand, COUNT(*) as cnt FROM bm_sku_gaps
               WHERE user_id=? AND status='unlaunched' AND brand != ''
               GROUP BY brand ORDER BY cnt DESC LIMIT 50""",
            (user_id,)
        )
        brands = [{"brand": r[0], "count": r[1]} for r in await cur2.fetchall()]

        # Summary stats
        cur3 = await db.execute(
            """SELECT COUNT(*), SUM(stock_total), SUM(suggested_price_mxn * stock_total)
               FROM bm_sku_gaps WHERE user_id=? AND status='unlaunched'""",
            (user_id,)
        )
        row = await cur3.fetchone()
        total_gaps   = row[0] or 0
        total_stock  = row[1] or 0
        revenue_pot  = row[2] or 0

    return {
        "total_gaps": total_gaps,
        "total_stock": total_stock,
        "revenue_potential_mxn": round(revenue_pot, 0),
        "categories": cats,
        "brands": brands,
    }


@router.get("/check-sku")
async def check_sku_endpoint(sku: str = Query(...)):
    """Check if a SKU is already published in ML (active or paused).
    Searches by seller_sku param and also checks item_sku_cache.
    Returns {exists: bool, item_ids: list[str]}.
    """
    from app.services.meli_client import _active_user_id as _ctx
    from app.services.token_store import DATABASE_PATH
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    _ALL_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")
    def _base(s: str) -> str:
        u = (s or "").upper()
        for sfx in _ALL_SUFFIXES:
            if u.endswith(sfx):
                return u[:-len(sfx)]
        return u

    base_sku = _base(sku)
    found_ids: list[str] = []

    # 1. Check item_sku_cache (fast — already fetched)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT item_id FROM item_sku_cache WHERE user_id=? AND (sku=? OR sku=?)",
            (user_id, sku.upper(), base_sku)
        )
        rows = await cur.fetchall()
        found_ids.extend(str(r["item_id"]) for r in rows)

    # 2. Live ML search by seller_sku (catches items not in cache)
    if not found_ids:
        try:
            client = await get_meli_client()
            if client:
                resp = await client.get(
                    f"/users/{user_id}/items/search",
                    params={"seller_sku": base_sku, "limit": 10},
                )
                ml_ids = resp.get("results", [])
                found_ids.extend(str(i) for i in ml_ids)
                # Also try with original sku if different
                if sku.upper() != base_sku:
                    resp2 = await client.get(
                        f"/users/{user_id}/items/search",
                        params={"seller_sku": sku.upper(), "limit": 10},
                    )
                    found_ids.extend(str(i) for i in resp2.get("results", []))
                await client.close()
        except Exception as e:
            logger.warning(f"check-sku ML search error: {e}")

    unique_ids = list(dict.fromkeys(found_ids))  # deduplicate, preserve order
    return {"exists": bool(unique_ids), "item_ids": unique_ids, "sku": sku, "base_sku": base_sku}


@router.get("/gaps")
async def get_gaps(
    request: Request,
    status:   str   = Query("unlaunched"),
    sort:     str   = Query("priority"),
    page:     int   = Query(1, ge=1),
    per_page: int   = Query(10, ge=5, le=100),
    category: str   = Query(""),
    brand:    str   = Query(""),
    search:   str   = Query(""),
    min_stock: int  = Query(0, ge=0),
    min_price: float = Query(0.0, ge=0),
):
    """Lista SKUs no lanzados para la cuenta activa con filtros, orden y paginación."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    _SORT_MAP = {
        "priority":    "priority_score DESC, stock_total DESC",
        "stock_desc":  "stock_total DESC",
        "stock_asc":   "stock_total ASC",
        "price_desc":  "retail_price_usd DESC",
        "price_asc":   "retail_price_usd ASC",
        "name_asc":    "product_title ASC",
    }
    order = _SORT_MAP.get(sort, "priority_score DESC, stock_total DESC")
    status_filter = status if status in ("unlaunched", "ignored") else "unlaunched"

    # Build WHERE clause dynamically
    conditions = ["user_id=?", "status=?"]
    params: list = [user_id, status_filter]

    if category:
        conditions.append("category=?")
        params.append(category)
    if brand:
        conditions.append("brand=?")
        params.append(brand)
    if search:
        conditions.append("(product_title LIKE ? OR sku LIKE ? OR model LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if min_stock > 0:
        conditions.append("stock_total >= ?")
        params.append(min_stock)
    if min_price > 0:
        conditions.append("retail_price_usd >= ?")
        params.append(min_price)

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        total_cur = await db.execute(f"SELECT COUNT(*) FROM bm_sku_gaps WHERE {where}", params)
        total = (await total_cur.fetchone())[0]

        rows_cur = await db.execute(
            f"SELECT * FROM bm_sku_gaps WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        )
        rows = await rows_cur.fetchall()

        # Total gaps for the status (unfiltered count for badge)
        badge_cur = await db.execute(
            "SELECT COUNT(*) FROM bm_sku_gaps WHERE user_id=? AND status='unlaunched'",
            (user_id,)
        )
        badge_total = (await badge_cur.fetchone())[0]

    items = []
    for r in rows:
        d = dict(r)
        # Ensure cost = retail (costo de adquisición = precio retail BM)
        retail = float(d.get("retail_price_usd") or 0)
        if retail > 0:
            d["cost_usd"]       = retail
            d["cost_price_mxn"] = round(retail * 18)
        else:
            d["cost_usd"]       = 0
            d["cost_price_mxn"] = 0
        items.append(d)

    return {
        "user_id":     user_id,
        "total":       total,          # filtered count
        "badge_total": badge_total,    # unfiltered count for the badge
        "page":        page,
        "per_page":    per_page,
        "pages":       max(1, -(-total // per_page)),  # ceiling division
        "items":       items,
    }


@router.post("/scan-now")
async def trigger_scan():
    """Dispara el scan manualmente (no espera a que termine)."""
    if _scan_lock.locked():
        return {"status": "already_running"}
    asyncio.create_task(_run_gap_scan())
    return {"status": "started"}


@router.get("/scan-status")
async def get_scan_status():
    """Estado del último scan, incluyendo progreso en tiempo real."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM bm_gap_scan_status WHERE id=1")
        row = await cursor.fetchone()
    base = dict(row) if row else {"status": "idle"}
    # Merge in-memory progress when running
    if base.get("status") == "running":
        base["progress"] = _scan_progress
    else:
        base["progress"] = {"pct": 100 if base.get("status") == "done" else 0,
                            "phase": base.get("status", "idle"),
                            "label": "", "detail": ""}
    return base


@router.get("/reactivations")
async def get_reactivations(request: Request):
    """Lista SKUs con stock en BM cuyo listing en ML está inactivo/pausado."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM bm_reactivations WHERE user_id=? ORDER BY stock_bm DESC",
            (user_id,)
        )
        rows = await cur.fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.post("/reactivate")
async def reactivate_listing(request: Request):
    """Reactiva un listing inactivo/pausado en ML actualizando stock."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    body      = await request.json()
    item_id   = body.get("item_id", "")
    stock_bm  = int(body.get("stock_bm", 1))
    price     = body.get("price")   # optional override

    if not item_id:
        return JSONResponse({"error": "item_id required"}, status_code=400)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    try:
        # Fetch current item to know its status
        item = await client.get(f"/items/{item_id}")
        current_status = item.get("status", "")

        update_payload: dict = {"available_quantity": stock_bm}
        if price:
            update_payload["price"] = float(price)
        # For paused items we need to explicitly set active
        if current_status == "paused":
            update_payload["status"] = "active"

        result = await client.put(f"/items/{item_id}", json=update_payload)
        err = result.get("error") or result.get("message")
        if err and result.get("status") not in (None, 200):
            return JSONResponse({"error": err}, status_code=400)

        # Remove from reactivations table
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "DELETE FROM bm_reactivations WHERE user_id=? AND item_id=?",
                (user_id, item_id)
            )
            await db.commit()

        permalink = item.get("permalink", "")
        return {"ok": True, "item_id": item_id, "new_status": "active", "permalink": permalink}
    except Exception as e:
        logger.error(f"reactivate error {item_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.get("/price-alerts")
async def get_price_alerts(request: Request):
    """Lista items con precio en ML que difiere >10% del precio sugerido por BM."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM ml_price_alerts WHERE user_id=? ORDER BY ABS(diff_pct) DESC",
            (user_id,)
        )
        rows = await cur.fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.post("/sync-price")
async def sync_price(request: Request):
    """Actualiza el precio de un item en ML al precio sugerido por BM."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    body    = await request.json()
    item_id = body.get("item_id", "")
    price   = body.get("price")

    if not item_id or not price:
        return JSONResponse({"error": "item_id and price required"}, status_code=400)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    try:
        result = await client.put(f"/items/{item_id}", json={"price": float(price)})
        err = result.get("error") or result.get("message")
        if err and result.get("status") not in (None, 200):
            return JSONResponse({"error": err}, status_code=400)

        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "DELETE FROM ml_price_alerts WHERE user_id=? AND item_id=?",
                (user_id, item_id)
            )
            await db.commit()

        return {"ok": True, "item_id": item_id, "new_price": float(price)}
    except Exception as e:
        logger.error(f"sync-price error {item_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.get("/listing-quality")
async def get_listing_quality(request: Request):
    """Lista scores de calidad de listings activos, ordenados de peor a mejor."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM ml_listing_quality WHERE user_id=? ORDER BY quality_score ASC",
            (user_id,)
        )
        rows = await cur.fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.get("/competition-alerts")
async def get_competition_alerts(request: Request):
    """Lista items donde el precio ML supera >15% al precio de la competencia."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM ml_competition_alerts WHERE user_id=? ORDER BY diff_pct DESC",
            (user_id,)
        )
        rows = await cur.fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.get("/gaps-summary")
async def get_gaps_summary(request: Request):
    """Resumen de gaps: potencial de ingresos, stock total sin publicar."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            """SELECT COUNT(*), SUM(stock_total), SUM(suggested_price_mxn * stock_total)
               FROM bm_sku_gaps WHERE user_id=? AND status='unlaunched'""",
            (user_id,)
        )
        row = await cur.fetchone()
    return {
        "total_gaps": row[0] or 0,
        "total_stock": row[1] or 0,
        "revenue_potential_mxn": round(row[2] or 0, 0),
    }


@router.get("/sales-velocity")
async def get_sales_velocity(request: Request, days: int = Query(30, ge=7, le=90)):
    """Calcula velocidad de ventas (uds/día) de items activos y días de stock en BM."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    try:
        from datetime import timedelta
        date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
        date_to   = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000-00:00")

        # Fetch orders for the period
        orders_resp = await client.get(
            f"/orders/search",
            params={
                "seller": user_id,
                "order.status": "paid",
                "order.date_created.from": date_from,
                "order.date_created.to": date_to,
                "limit": 50,
            }
        )
        orders = orders_resp.get("results", [])

        # Aggregate units sold per item_id
        item_units: dict = {}
        item_titles: dict = {}
        for order in orders:
            for ol in (order.get("order_items") or []):
                item = ol.get("item") or {}
                iid = str(item.get("id", ""))
                qty = int(ol.get("quantity", 0))
                if iid:
                    item_units[iid] = item_units.get(iid, 0) + qty
                    item_titles[iid] = item.get("title", "")

        # Cross with item_sku_cache to get SKUs
        result_rows = []
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            for iid, units in sorted(item_units.items(), key=lambda x: -x[1]):
                vel = round(units / days, 2)
                cur = await db.execute(
                    "SELECT sku FROM item_sku_cache WHERE item_id=? AND user_id=?",
                    (iid, user_id)
                )
                row = await cur.fetchone()
                sku = row["sku"] if row else ""
                # Get BM stock from gaps or reactivations
                bm_stock = 0
                if sku:
                    scur = await db.execute(
                        "SELECT stock_total FROM bm_sku_gaps WHERE user_id=? AND sku=?",
                        (user_id, sku.upper().split("-")[0])
                    )
                    srow = await scur.fetchone()
                    if srow:
                        bm_stock = srow["stock_total"]
                days_stock = round(bm_stock / vel, 0) if vel > 0 and bm_stock > 0 else None
                result_rows.append({
                    "item_id": iid,
                    "sku": sku,
                    "title": item_titles.get(iid, ""),
                    "units_sold": units,
                    "velocity_per_day": vel,
                    "bm_stock": bm_stock,
                    "days_of_stock": days_stock,
                    "alert": days_stock is not None and days_stock < 14,
                })

        return {"items": result_rows, "period_days": days, "total_items": len(result_rows)}
    except Exception as e:
        logger.error(f"sales-velocity error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.get("/debug-scan")
async def debug_scan(sku: str = ""):
    """Diagnóstico detallado del scan: BM, ML item IDs, batch fetch, caché."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()

    result: dict = {"user_id": user_id, "bm": {}, "ml": {}, "cache": {}, "scan_status": {}, "gaps_in_db": []}

    # 1. BM quick check
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as bm_http:
            logged_in = await _bm_login(bm_http)
            result["bm"]["login"] = logged_in
            if logged_in:
                page1 = await _bm_fetch_all_skus_with_stock(bm_http)
                result["bm"]["total_skus"] = len(page1)
                result["bm"]["first_row_keys"] = list(page1[0].keys()) if page1 else []
    except Exception as e:
        result["bm"]["error"] = str(e)

    # 2. ML items/search — show first 10 IDs and test batch fetch on them
    if user_id:
        client = await get_meli_client(user_id=user_id)
        if client:
            try:
                resp = await client.get(
                    f"/users/{user_id}/items/search",
                    params={"limit": 100, "offset": 0},
                )
                ids = resp.get("results", [])
                paging = resp.get("paging", {})
                result["ml"]["items_search_total"] = paging.get("total", 0)
                result["ml"]["first_10_ids"] = ids[:10]

                # Test batch fetch on first 5 items WITHOUT attribute filter
                if ids:
                    sample_batch = ids[:5]
                    try:
                        details = await client.get(f"/items?ids={','.join(sample_batch)}")
                        batch_result = []
                        for entry in (details if isinstance(details, list) else []):
                            body = entry.get("body", {}) or {}
                            batch_result.append({
                                "id": body.get("id"),
                                "seller_custom_field": body.get("seller_custom_field"),
                                "has_attributes": bool(body.get("attributes")),
                                "has_variations": bool(body.get("variations")),
                            })
                        result["ml"]["batch_fetch_sample"] = batch_result
                    except Exception as e:
                        result["ml"]["batch_fetch_error"] = str(e)

                # If a specific SKU is provided, check if its item is in the results
                if sku:
                    sku_upper = sku.upper()
                    result["ml"]["sku_check"] = {"sku": sku_upper, "found_in_item_ids": False}
                    # Check cache
                    from app.services.token_store import get_cached_skus
                    cached = await get_cached_skus(ids)
                    cached_by_sku = {v: k for k, v in cached.items()}
                    result["cache"]["total_cached_for_page1"] = len(cached)
                    if sku_upper in cached_by_sku:
                        result["ml"]["sku_check"]["in_cache"] = True
                        result["ml"]["sku_check"]["cached_item_id"] = cached_by_sku[sku_upper]

            except Exception as e:
                result["ml"]["items_search_error"] = str(e)
            finally:
                await client.close()

    # 3. DB state
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM bm_gap_scan_status WHERE id=1")
        row = await cursor.fetchone()
        result["scan_status"] = dict(row) if row else {}
        cursor2 = await db.execute("SELECT COUNT(*), user_id FROM bm_sku_gaps GROUP BY user_id")
        result["gaps_in_db"] = [{"count": r[0], "user_id": r[1]} for r in await cursor2.fetchall()]

        # Show specific SKU in gaps if provided
        if sku and user_id:
            cursor3 = await db.execute(
                "SELECT * FROM bm_sku_gaps WHERE sku=? AND user_id=?",
                (sku.upper(), user_id)
            )
            gap_row = await cursor3.fetchone()
            result["gap_record"] = dict(gap_row) if gap_row else None

    return result


@router.post("/recalc-prices")
async def recalc_prices():
    """Recalcula suggested_price_mxn y cost_price_mxn en la DB usando fórmula actual
    (retail × 18 × 1.20) sin necesidad de hacer un nuevo scan completo."""
    updated = 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await (await db.execute(
            "SELECT rowid, retail_price_usd, cost_usd FROM bm_sku_gaps"
        )).fetchall()
        for row in rows:
            rowid, retail, cost = row[0], float(row[1] or 0), float(row[2] or 0)
            new_suggested = round(retail * 18 * 1.20, 0) if retail > 0 else 0
            new_cost_mxn  = round(cost * 18, 0) if (0 < cost < 9000) else 0
            await db.execute(
                "UPDATE bm_sku_gaps SET suggested_price_mxn=?, cost_price_mxn=? WHERE rowid=?",
                (new_suggested, new_cost_mxn, rowid)
            )
            updated += 1
        await db.commit()
    return {"updated": updated, "formula": "retail × 18 × 1.20"}


@router.post("/clear-sku-cache")
async def clear_sku_cache(request: Request):
    """Limpia la caché de item→SKU para forzar re-fetch completo en el próximo scan."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()

    async with aiosqlite.connect(DATABASE_PATH) as db:
        if user_id:
            result = await db.execute(
                "DELETE FROM item_sku_cache WHERE user_id=?", (user_id,)
            )
            deleted = result.rowcount
        else:
            result = await db.execute("DELETE FROM item_sku_cache")
            deleted = result.rowcount
        await db.commit()

    return {"ok": True, "deleted": deleted}


@router.get("/search-product-image")
async def search_product_image_endpoint(brand: str = "", model: str = "", title: str = ""):
    """Busca imagen oficial del producto en DuckDuckGo Images. Retorna hasta 5 URLs."""
    import re as _re

    query = " ".join(filter(None, [brand, model])).strip() or title or ""
    if not query:
        return JSONResponse({"error": "brand/model requerido"}, status_code=400)

    search_q = f"{query} official product image -site:amazon -site:mercadolibre"
    headers  = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
            # Step 1: get vqd token from DuckDuckGo
            r1   = await client.get("https://duckduckgo.com/", params={"q": search_q, "iax": "images", "ia": "images"})
            vqd_match = _re.search(r'vqd=([\d-]+)', r1.text)
            if not vqd_match:
                return {"images": [], "query": search_q}
            vqd = vqd_match.group(1)

            # Step 2: fetch image results JSON
            r2 = await client.get(
                "https://duckduckgo.com/i.js",
                params={"q": search_q, "vqd": vqd, "o": "json", "l": "us-en", "p": "1", "f": ",,,,,"},
                headers={**headers, "Referer": "https://duckduckgo.com/"},
            )
            data    = r2.json()
            results = data.get("results", [])
            images  = [
                {"url": r["image"], "thumbnail": r.get("thumbnail", ""), "title": r.get("title", "")}
                for r in results[:8]
                if r.get("image") and r["image"].startswith("http")
            ]
        return {"images": images, "query": search_q}
    except Exception as e:
        logger.warning(f"search-product-image error: {e}")
        return {"images": [], "query": search_q, "error": str(e)}


@router.post("/submit-image")
async def submit_image_endpoint(request: Request):
    """Envía job de imagen a Replicate y retorna pred_id inmediatamente (< 5s).
    Evita timeout de Railway (60s Nginx). El frontend hace polling a /prediction/{pred_id}.
    """
    from app.services import replicate_client

    if not replicate_client.is_available():
        return JSONResponse({"error": "REPLICATE_API_KEY no configurada"}, status_code=503)

    body          = await request.json()
    custom        = (body.get("custom_prompt") or "").strip()
    prompt_index  = int(body.get("prompt_index", 0))
    reference_url = (body.get("reference_image_url") or "").strip()
    brand         = body.get("brand", "")
    model         = body.get("model", "")
    title         = body.get("title", "") or body.get("product_title", "")
    category      = body.get("category", "")
    size          = str(body.get("size", "") or "").strip()

    use_kontext = bool(reference_url) and prompt_index < 7

    if custom:
        prompt = custom
    else:
        prompts = replicate_client.build_batch_prompts(
            brand=brand, model=model, title=title, category=category, size=size,
            count=8, use_kontext=use_kontext,
        )
        prompt = prompts[prompt_index] if prompt_index < len(prompts) else prompts[-1]

    try:
        result = await replicate_client.submit_image_job(
            prompt=prompt,
            input_image=reference_url if use_kontext else "",
        )
        return {
            "pred_id":   result["pred_id"],
            "image_url": result["image_url"],
            "prompt":    prompt,
            "mode":      "kontext" if use_kontext else "flux",
        }
    except Exception as e:
        logger.error(f"submit-image error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/prediction/{pred_id}")
async def check_prediction_endpoint(pred_id: str):
    """Consulta el estado de una predicción de Replicate (imagen).
    Retorna {status, image_url, error}.
    """
    from app.services import replicate_client
    try:
        result = await replicate_client.check_prediction(pred_id)
        return result
    except Exception as e:
        logger.error(f"check-prediction error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/generate-image")
async def generate_image_endpoint(request: Request):
    """Genera imagen de producto con FLUX 1.1 Pro (texto) o FLUX Kontext (img2img lifestyle).
    Acepta prompt_index (0-7) y reference_image_url opcional para Kontext.
    Si prompt_index < 7 y hay reference_image_url → usa Kontext (lifestyle hermoso).
    Si prompt_index == 7 o no hay referencia → usa FLUX texto puro.
    """
    from app.services import replicate_client

    if not replicate_client.is_available():
        return JSONResponse({"error": "REPLICATE_API_KEY no configurada"}, status_code=503)

    body          = await request.json()
    brand         = body.get("brand", "")
    model         = body.get("model", "")
    title         = body.get("title", "") or body.get("product_title", "")
    category      = body.get("category", "")
    size          = str(body.get("size", "") or "").strip()
    custom        = (body.get("custom_prompt") or "").strip()
    prompt_index  = int(body.get("prompt_index", 0))
    reference_url = (body.get("reference_image_url") or "").strip()

    use_kontext = bool(reference_url) and prompt_index < 7

    if custom:
        prompt = custom
    else:
        prompts = replicate_client.build_batch_prompts(
            brand=brand, model=model, title=title, category=category, size=size,
            count=8, use_kontext=use_kontext,
        )
        prompt = prompts[prompt_index] if prompt_index < len(prompts) else prompts[-1]

    try:
        if use_kontext:
            image_url = await replicate_client.generate_image_with_reference(prompt, reference_url)
        else:
            image_url = await replicate_client.generate_image(prompt)
        return {"image_url": image_url, "prompt": prompt, "mode": "kontext" if use_kontext else "flux"}
    except Exception as e:
        logger.error(f"generate-image error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/generate-images-batch")
async def generate_images_batch_endpoint(request: Request):
    """Genera múltiples imágenes de producto en paralelo con distintos ángulos."""
    from app.services import replicate_client

    if not replicate_client.is_available():
        return JSONResponse({"error": "REPLICATE_API_KEY no configurada"}, status_code=503)

    body    = await request.json()
    brand   = body.get("brand", "")
    model   = body.get("model", "")
    title   = body.get("title", "") or body.get("product_title", "")
    category= body.get("category", "")
    size    = str(body.get("size", "") or "").strip()
    n       = min(int(body.get("count", 8)), 8)
    custom  = (body.get("custom_prompt") or "").strip()

    if custom:
        # If user typed a custom prompt, use it as base and generate N identical requests
        prompts = [custom] * n
    else:
        prompts = replicate_client.build_batch_prompts(
            brand=brand, model=model, title=title, category=category, size=size, count=n
        )

    tasks   = [replicate_client.generate_image(p) for p in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    images = []
    for i, (r, p) in enumerate(zip(results, prompts)):
        if isinstance(r, Exception):
            images.append({"error": str(r), "prompt": p, "index": i})
        else:
            images.append({"image_url": r, "prompt": p, "index": i})

    return {"images": images}


@router.post("/generate-video")
async def generate_video_endpoint(request: Request):
    """Genera video de producto con minimax/video-01 via Replicate (~2-4 min)."""
    from app.services import replicate_client

    if not replicate_client.is_available():
        return JSONResponse({"error": "REPLICATE_API_KEY no configurada"}, status_code=503)

    body            = await request.json()
    brand           = body.get("brand", "")
    model           = body.get("model", "")
    title           = body.get("title", "") or body.get("product_title", "")
    category        = body.get("category", "")
    size            = str(body.get("size", "") or "").strip()
    first_frame_url = (body.get("first_frame_image") or "").strip()
    custom_prompt   = (body.get("custom_prompt") or "").strip()

    prompt = custom_prompt or replicate_client.build_video_prompt(
        brand=brand, model=model, title=title, category=category, size=size
    )

    try:
        video_url = await replicate_client.generate_video(
            prompt=prompt,
            first_frame_image=first_frame_url,
        )
        return {"video_url": video_url, "prompt": prompt}
    except Exception as e:
        logger.error(f"generate-video error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/generate-product-prompts")
async def generate_product_prompts_endpoint(request: Request):
    """Usa Claude para generar 8 prompts únicos de fotografía comercial para el producto.
    Cada prompt cuenta una historia diferente y es específico al producto real.
    """
    import json as _json
    from app.services import replicate_client

    body          = await request.json()
    brand         = body.get("brand", "")
    model         = body.get("model", "")
    title         = body.get("title", "") or body.get("product_title", "")
    category      = body.get("category", "")
    size          = str(body.get("size", "") or "").strip()
    has_reference = bool(body.get("has_reference", False))

    # KONTEXT mode: reference image already carries the product — prompts describe the scene/environment.
    # FLUX mode: no reference — prompts must describe the product explicitly in each scene.
    if has_reference:
        mode_instruction = (
            "MODO KONTEXT (hay imagen de referencia del producto real):\n"
            "El TV ya está en la imagen de referencia — NO describas el TV en el prompt.\n"
            "Describe SOLO la escena, ambiente, iluminación y estilo de vida donde se colocará.\n"
            "Ejemplo correcto: 'Sophisticated modern Mexican penthouse living room at golden hour. "
            "Warm cinematic light, plush cream sofa, city skyline through floor-to-ceiling windows. "
            "Aspirational lifestyle photography, 8K, photorealistic.'\n"
            "Ejemplo INCORRECTO: 'A Samsung TV in a living room' — no menciones el producto.\n"
        )
    else:
        mode_instruction = (
            "MODO FLUX TEXTO (sin imagen de referencia):\n"
            "Menciona explícitamente el TV en cada prompt: "
            f"'large flat-screen {brand} television with ultra-thin bezels, metallic stand'.\n"
            "NUNCA uses palabras que puedan confundirse con una cámara o fotografía "
            "(no: 'lens', 'shot', 'camera', 'photograph').\n"
            "Sí usa: 'flat-screen television', 'large rectangular display', 'smart TV'.\n"
        )

    system = (
        "You are a creative director of commercial photography for Mercado Libre México.\n"
        "Generate 8 FLUX AI image prompts in ENGLISH ONLY that tell a visual story of the product.\n\n"
        + mode_instruction +
        "\nTHE 8 CHAPTERS (generate in this exact order):\n"
        "0. HERO — Pure white seamless studio background. Product perfectly centered facing forward. "
        "Beautiful vivid 4K content on screen (nature, ocean, or cinematic scene). "
        "Professional studio lighting from both sides. Ultra-clean retail-quality shot.\n"
        "1. PENTHOUSE — Breathtaking luxury penthouse living room at golden hour. "
        "Floor-to-ceiling panoramic windows with city skyline. Cream bouclé sofa, marble fireplace, "
        "orchids, Murano pendant lights. TV wall-mounted, screen glowing with 4K HDR content.\n"
        "2. FAMILY MOVIE NIGHT — Cozy warm family living room at night. "
        "Family of 4 on plush velvet sofa, popcorn bowls, warm amber lighting. "
        "TV screen showing a beloved animated film. Blankets, family joy, magical atmosphere.\n"
        "3. SPORT — Modern living room transformed into a stadium experience. "
        "Soccer match on screen, vivid green pitch, crowd energy, friends cheering with snacks. "
        "Vibrant colors, electric atmosphere, dynamic lifestyle.\n"
        "4. DARK CINEMA — Completely dark room, only the TV screen illuminates the space. "
        "Deep cinematic shadows, dramatic 4K movie on screen, couple on sofa silhouetted. "
        "Premium home theater atmosphere, IMAX-quality light.\n"
        "5. SMART TV UI — Minimalist modern living room, daytime. "
        "TV screen showing the smart interface with streaming apps grid (Netflix, YouTube icons visible). "
        "Clean architectural space, natural light, premium feel.\n"
        "6. PREMIUM DESIGN — Dramatic close-up angular shot of the TV from a 45-degree low angle. "
        "Ultra-thin bezel profile, premium brushed aluminum stand, dark elegant background. "
        "Macro detail of screen edge and materials. Luxury product aesthetic.\n"
        "7. NIGHT EXTERIOR — Exterior view of a modern luxury home at night. "
        "Through the floor-to-ceiling window, the TV is clearly visible glowing in a beautiful living room. "
        "Architectural exterior photography, warm interior light contrasting with dark garden.\n\n"
        "ABSOLUTE RULES:\n"
        "- ALL prompts MUST be written in ENGLISH only — no Spanish words at all\n"
        "- No visible text or logos in images\n"
        "- No external streaming devices (no sticks, dongles, external boxes)\n"
        "- Quality suffix for every prompt: 'cinematic photography, 8K ultra-realistic, professional commercial'\n"
        "- Each prompt: minimum 50 words, maximum 90 words\n"
        "- Describe REAL rooms with specific details (herringbone floor, built-in shelves, marble, etc.)\n\n"
        "Respond ONLY with a valid JSON array of exactly 8 strings. No markdown, no backticks, no explanations."
    )

    user = (
        f"Product: {title}\n"
        f"Brand: {brand}\n"
        f"Model: {model}\n"
        f"Category: {category}\n"
        f"Screen size: {size} inches\n\n"
        "Generate 8 lifestyle commercial photography prompts for this exact product."
    )

    try:
        raw = await claude_client.generate(prompt=user, system=system, max_tokens=3000)
        raw = raw.strip()
        # Strip markdown code fences if present
        if "```" in raw:
            parts = raw.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                try:
                    prompts = _json.loads(p)
                    if isinstance(prompts, list):
                        break
                except Exception:
                    continue
            else:
                prompts = _json.loads(raw)
        else:
            prompts = _json.loads(raw)

        if not isinstance(prompts, list) or not prompts:
            raise ValueError("Respuesta no es una lista")
        while len(prompts) < 8:
            prompts.append(prompts[-1])
        logger.info(f"Claude generó {len(prompts)} prompts para '{title}'")
        return {"prompts": prompts[:8], "source": "claude"}

    except Exception as e:
        logger.warning(f"generate-product-prompts Claude error — usando fallback: {e}")
        prompts = replicate_client.build_batch_prompts(
            brand=brand, model=model, title=title, category=category, size=size, count=8
        )
        return {"prompts": prompts, "source": "fallback"}


async def _create_slideshow_from_images(
    image_urls: list,
    audio_bytes,
    ffmpeg_bin: str,
    tmpdir: str,
) -> str:
    """Slideshow con múltiples crop-positions por imagen — no loop visible incluso con 2 imágenes.

    Genera clips ciclando imágenes con 8 posiciones de encuadre distintas (simula cambios de
    ángulo de cámara). Con 2 imágenes produce 16 clips × 5s = 80s de contenido único.
    -shortest corta exactamente al terminar el audio.
    """
    import os as _os2
    import subprocess as _sp2

    # 8 posiciones de crop sobre imagen escalada a 1664×936 → ventana 1280×720
    # (1664-1280=384, 936-720=216 → margen disponible para desplazar el encuadre)
    CROP_POSITIONS = [
        (192, 108, "center"),        # centro exacto
        (0,   0,   "top-left"),      # esquina superior izquierda
        (384, 216, "bot-right"),     # esquina inferior derecha
        (384, 0,   "top-right"),     # esquina superior derecha
        (0,   216, "bot-left"),      # esquina inferior izquierda
        (96,  54,  "near-center-tl"),
        (288, 162, "near-center-br"),
        (192, 0,   "top-center"),
    ]
    CLIP_DUR  = 5     # segundos por clip
    TARGET_S  = 50    # buffer total (superior a cualquier audio razonable)

    # ── Descargar imágenes ───────────────────────────────────────────────────
    img_paths: list = []
    async with httpx.AsyncClient(timeout=30.0) as dl_client:
        for i, url in enumerate(image_urls[:8]):
            try:
                r = await dl_client.get(url, follow_redirects=True)
                if r.status_code == 200 and len(r.content) > 1000:
                    p = _os2.path.join(tmpdir, f"img_{i}.jpg")
                    with open(p, "wb") as fh:
                        fh.write(r.content)
                    img_paths.append(p)
            except Exception as exc:
                logger.warning(f"Slideshow image {i} download failed: {exc}")

    if not img_paths:
        raise RuntimeError("No se pudieron descargar imágenes para el slideshow")

    # ── Generar clips suficientes para cubrir TARGET_S ───────────────────────
    clip_paths: list = []
    clip_idx = 0
    while clip_idx * CLIP_DUR < TARGET_S:
        img_path   = img_paths[clip_idx % len(img_paths)]
        cx, cy, _  = CROP_POSITIONS[clip_idx % len(CROP_POSITIONS)]
        clip_path  = _os2.path.join(tmpdir, f"clip_{clip_idx:03d}.mp4")
        # Scale to 1664×936, then crop a 1280×720 window at position (cx, cy)
        vf = (
            f"scale=1664:936:force_original_aspect_ratio=increase,"
            f"crop=1664:936,"
            f"crop=1280:720:{cx}:{cy},"
            f"setsar=1"
        )
        proc = _sp2.run(
            [
                ffmpeg_bin, "-y",
                "-loop", "1", "-i", img_path,
                "-t", str(CLIP_DUR),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-r", "25",
                clip_path,
            ],
            capture_output=True, timeout=30,
        )
        if proc.returncode == 0:
            clip_paths.append(clip_path)
        else:
            logger.warning(f"Clip {clip_idx} error: {proc.stderr.decode(errors='replace')[:200]}")
        clip_idx += 1

    if not clip_paths:
        raise RuntimeError("No se generaron clips para el slideshow")

    logger.info(f"Slideshow: {len(clip_paths)} clips ({len(img_paths)} imgs × posiciones)")

    # ── Concat clips ─────────────────────────────────────────────────────────
    concat_path   = _os2.path.join(tmpdir, "slide_concat.txt")
    slideshow_raw = _os2.path.join(tmpdir, "slideshow_raw.mp4")
    with open(concat_path, "w") as fh:
        for p in clip_paths:
            fh.write(f"file '{p}'\n")

    proc = _sp2.run(
        [ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
         "-c", "copy", slideshow_raw],
        capture_output=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Slideshow concat failed: {proc.stderr.decode(errors='replace')[:300]}")
    logger.info(f"Slideshow concat OK: {len(clip_paths)} clips")

    if not audio_bytes:
        return slideshow_raw

    # Combine with audio, cut at audio end
    aud_path = _os2.path.join(tmpdir, "slide_audio.mp3")
    out_path = _os2.path.join(tmpdir, "slideshow_final.mp4")
    with open(aud_path, "wb") as fh:
        fh.write(audio_bytes)
    proc = _sp2.run(
        [
            ffmpeg_bin, "-y",
            "-i", slideshow_raw,
            "-i", aud_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            out_path,
        ],
        capture_output=True, timeout=120,
    )
    if proc.returncode == 0:
        logger.info("Slideshow+audio combined OK")
        return out_path
    logger.warning(f"Slideshow+audio failed: {proc.stderr.decode(errors='replace')[:300]}, returning no-audio version")
    return slideshow_raw


@router.post("/generate-video-commercial")
async def generate_video_commercial_endpoint(request: Request):
    """Pipeline completo de comercial en español:
    1. Claude genera guion en español de México (30-40 palabras)
    2. ElevenLabs convierte guion a voz profesional en español
    3. Si hay imágenes AI: slideshow con ffmpeg; si no: minimax/video-01
    4. ffmpeg combina audio + video en un solo MP4
    Retorna URL del video combinado + el guion generado.
    """
    import json as _json
    import subprocess as _sp
    import tempfile as _tf
    import uuid as _uuid
    import os as _os
    from app.services import replicate_client, elevenlabs_client

    if not replicate_client.is_available():
        return JSONResponse({"error": "REPLICATE_API_KEY no configurada"}, status_code=503)

    body           = await request.json()
    brand          = body.get("brand", "")
    model          = body.get("model", "")
    title          = body.get("title", "") or body.get("product_title", "")
    category       = body.get("category", "")
    size           = str(body.get("size", "") or "").strip()
    first_frame    = (body.get("first_frame_image") or "").strip()
    ai_image_urls  = [u for u in (body.get("ai_image_urls") or []) if isinstance(u, str) and u.startswith("http")]

    # ── Step 1: Claude genera guion + video_prompt juntos (coherencia visual) ──
    script       = ""
    video_prompt = ""

    claude_system = (
        "You are the creative director of a TV commercial for Mercado Libre México.\n"
        "Create a narration script AND 5 distinct cinematic scene descriptions for the same commercial.\n\n"
        "Respond ONLY with this JSON (no markdown, no backticks, no explanations):\n"
        '{"script": "...", "scenes": ["scene1", "scene2", "scene3", "scene4", "scene5"]}\n\n'
        "SCRIPT field rules:\n"
        "- Narration text in Mexican Spanish (español de México), exactly 70-85 words (enough for 25-30 seconds of speech)\n"
        "- Exciting, aspirational tone — like a premium Mexican TV commercial\n"
        "- NEVER mention model numbers, SKU codes, or alphanumeric product codes\n"
        "- ONLY mention the brand name and screen size in inches (e.g. 'Samsung de 43 pulgadas')\n"
        "- Describe in detail: image quality, color vibrancy, sound experience, design elegance, smart features, family moments\n"
        "- Make the listener FEEL the experience — use sensory language\n"
        "- End EXACTLY with: Disponible ahora en Mercado Libre.\n\n"
        "SCENES array rules (5 items, each a 6-second AI video clip description):\n"
        "- Each scene is an ENGLISH visual description, maximum 50 words\n"
        "- Each scene must be VISUALLY DIFFERENT from the others\n"
        "- Scene 1: dramatic opening shot of the TV in a luxury room, golden hour\n"
        "- Scene 2: close-up of the vivid screen showing beautiful content\n"
        "- Scene 3: family or couple enjoying the TV, lifestyle shot\n"
        "- Scene 4: cinematic detail of the TV design, ultra-thin bezel, premium feel\n"
        "- Scene 5: wide shot of perfect evening atmosphere with TV as centerpiece\n"
        "- Describe: camera movement + what's visible + lighting + mood\n"
        "- ALWAYS include 'large flat-screen television' or 'big screen TV'\n"
        "- Premium commercial quality, 8K cinematic"
    )
    claude_user = (
        f"Producto: {title}\n"
        f"Marca: {brand}\n"
        f"Tamaño: {size} pulgadas\n"
        f"Categoría: {category}\n"
        f"Imagen de referencia disponible: {'sí' if first_frame else 'no'}\n\n"
        "Genera el script en español y los 5 scenes en inglés para el comercial."
    )
    try:
        import json as _json_inner
        raw = (await claude_client.generate(prompt=claude_user, system=claude_system, max_tokens=900)).strip()
        # Strip markdown fences
        if "```" in raw:
            raw = raw[raw.index("```") + 3:]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw[:raw.index("```")] if "```" in raw else raw
        parsed = _json_inner.loads(raw.strip())
        script = parsed.get("script", "").strip().strip('"').strip("'")
        scenes: list = [s.strip() for s in (parsed.get("scenes") or []) if isinstance(s, str) and s.strip()]
        logger.info(f"Script: {script[:80]}...")
        logger.info(f"Scenes: {len(scenes)} generadas")
    except Exception as e:
        logger.warning(f"Claude script+scenes failed: {e}")
        size_txt = f"{size} pulgadas " if size else ""
        script = (
            f"El {brand} {size_txt}— imagen brillante, sonido envolvente y "
            f"entretenimiento sin límites. Todo lo que tu familia merece en un solo televisor. "
            f"Disponible ahora en Mercado Libre."
        )
        scenes = []

    # Fallback scenes if Claude didn't generate them
    if len(scenes) < 3:
        base_prompt = replicate_client.build_video_prompt(
            brand=brand, model=model, title=title, category=category, size=size
        )
        scenes = [
            base_prompt,
            f"Close-up of large flat-screen {brand} TV displaying vivid 4K ocean scenery, brilliant colors, cinematic quality",
            f"Family laughing together watching {brand} large screen TV in cozy living room, warm evening light",
            f"Elegant ultra-thin bezel {brand} television profile shot, premium design, architectural beauty, dark background",
            f"Couple embracing on sofa with large flat-screen TV showing Netflix interface, perfect cinematic evening mood",
        ]

    # Obtener ruta absoluta del binario ffmpeg (imageio-ffmpeg lo bundlea)
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg_bin = get_ffmpeg_exe()
        logger.info(f"ffmpeg bin: {ffmpeg_bin}")
    except Exception as _e:
        ffmpeg_bin = "ffmpeg"
        logger.warning(f"imageio-ffmpeg no disponible ({_e}), usando 'ffmpeg' del PATH")

    vid_id = str(_uuid.uuid4())

    # ── Ken Burns slideshow desde imágenes AI (SIEMPRE funciona, 30s) ────────
    if len(ai_image_urls) >= 3:
        logger.info(f"=== SLIDESHOW desde {len(ai_image_urls)} imágenes AI ===")
        n_images  = min(len(ai_image_urls), 6)
        per_img_s = 5.0  # 6 imágenes × 5s = 30s

        # TTS en paralelo mientras preparamos imágenes
        tts_task = asyncio.ensure_future(elevenlabs_client.generate_audio(script))

        async def _dl_img(url: str):
            async with httpx.AsyncClient(timeout=60.0) as _c:
                r = await _c.get(url, follow_redirects=True)
                return r.content if r.status_code == 200 and len(r.content) > 500 else None

        img_results = await asyncio.gather(
            *[_dl_img(u) for u in ai_image_urls[:n_images]], return_exceptions=True
        )
        audio_result = await tts_task

        with _tf.TemporaryDirectory() as tmpdir:
            seg_paths = []
            for idx, img_data in enumerate(img_results):
                if not isinstance(img_data, bytes) or not img_data:
                    logger.warning(f"Imagen {idx} no descargada, saltando")
                    continue
                img_path = _os.path.join(tmpdir, f"img_{idx:02d}.jpg")
                with open(img_path, "wb") as fh:
                    fh.write(img_data)
                seg_path = _os.path.join(tmpdir, f"seg_{idx:02d}.mp4")
                seg_cmd = [
                    ffmpeg_bin, "-y",
                    "-loop", "1", "-framerate", "25", "-i", img_path,
                    "-vf", (
                        "scale=1280:720:force_original_aspect_ratio=decrease,"
                        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
                    ),
                    "-t", str(per_img_s),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    seg_path,
                ]
                sr = _sp.run(seg_cmd, capture_output=True, timeout=60)
                if sr.returncode == 0 and _os.path.exists(seg_path) and _os.path.getsize(seg_path) > 1000:
                    seg_paths.append(seg_path)
                    logger.info(f"Segmento {idx} OK ({_os.path.getsize(seg_path)} bytes)")
                else:
                    logger.error(f"Segmento {idx} falló (rc={sr.returncode}): {sr.stderr.decode(errors='replace')[:200]}")

            if not seg_paths:
                logger.error("Slideshow: ningún segmento generado, cayendo a minimax")
            else:
                concat_path = _os.path.join(tmpdir, "concat.txt")
                raw_cat     = _os.path.join(tmpdir, "raw_cat.mp4")
                aud_path    = _os.path.join(tmpdir, "audio.mp3")
                out_path    = _os.path.join(tmpdir, "output.mp4")

                with open(concat_path, "w") as cf:
                    for sp_path in seg_paths:
                        cf.write(f"file '{sp_path}'\n")

                cat_r = _sp.run([
                    ffmpeg_bin, "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_path, "-c", "copy", raw_cat,
                ], capture_output=True, timeout=120)
                if cat_r.returncode != 0:
                    logger.error(f"Concat slideshow error: {cat_r.stderr.decode(errors='replace')[:300]}")
                    # Fallback: usar primer segmento
                    import shutil as _shutil
                    _shutil.copy(seg_paths[0], raw_cat)
                    logger.warning("Concat falló, usando primer segmento como fallback")
                else:
                    logger.info(f"Concat slideshow OK: {len(seg_paths)} segs → {_os.path.getsize(raw_cat)} bytes")

                has_audio = isinstance(audio_result, bytes) and bool(audio_result)
                if has_audio:
                    with open(aud_path, "wb") as fh:
                        fh.write(audio_result)
                    mix_r = _sp.run([
                        ffmpeg_bin, "-y",
                        "-i", raw_cat, "-i", aud_path,
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                        "-shortest", "-movflags", "+faststart",
                        out_path,
                    ], capture_output=True, timeout=120)
                    final_path = out_path if mix_r.returncode == 0 else raw_cat
                    if mix_r.returncode != 0:
                        logger.warning(f"Audio mix error: {mix_r.stderr.decode(errors='replace')[:300]}")
                else:
                    final_path = raw_cat

                with open(final_path, "rb") as fh:
                    _video_cache[vid_id] = fh.read()

                out_mb = len(_video_cache[vid_id]) / 1_048_576
                logger.info(f"Slideshow listo: {vid_id} ({out_mb:.1f} MB) segs={len(seg_paths)} audio={has_audio}")
                return {"video_url": f"/api/lanzar/video-file/{vid_id}", "script": script, "has_audio": has_audio}

    # ── Generar TTS + 5 clips minimax EN PARALELO ────────────────────────────
    # Cada escena genera un clip distinto → sin loop visible, duración = audio
    logger.info(f"=== MULTI-SCENE VIDEO: {len(scenes)} escenas + TTS en paralelo ===")

    async def _gen_clip(scene_prompt: str, idx: int):
        ff = first_frame if idx == 0 else ""
        return await replicate_client.generate_video(prompt=scene_prompt, first_frame_image=ff)

    tts_coro   = elevenlabs_client.generate_audio(script)
    clip_coros = [_gen_clip(scenes[i], i) for i in range(min(len(scenes), 6))]

    all_results = await asyncio.gather(tts_coro, *clip_coros, return_exceptions=True)
    audio_result  = all_results[0]
    clip_url_results = list(all_results[1:])

    has_audio = isinstance(audio_result, bytes) and bool(audio_result)
    logger.info(f"TTS: {type(audio_result).__name__}, clips: {[type(r).__name__ for r in clip_url_results]}")

    # Filtrar clips exitosos
    clip_urls = [r for r in clip_url_results if isinstance(r, str) and r.startswith("http")]
    logger.info(f"Clips exitosos: {len(clip_urls)}/{len(clip_url_results)}")

    if not clip_urls:
        return JSONResponse({"error": "No se pudo generar ningún clip de video"}, status_code=500)

    try:
        # ── Descargar clips ──────────────────────────────────────────────────
        async with httpx.AsyncClient(timeout=120.0) as dl:
            async def _dl(url):
                r = await dl.get(url, follow_redirects=True)
                return r.content if r.status_code == 200 and len(r.content) > 1000 else None
            downloaded = await asyncio.gather(*[_dl(u) for u in clip_urls], return_exceptions=True)

        with _tf.TemporaryDirectory() as tmpdir:
            clip_paths = []
            for i, data in enumerate(downloaded):
                if isinstance(data, bytes) and data:
                    p = _os.path.join(tmpdir, f"clip_{i:02d}.mp4")
                    with open(p, "wb") as f:
                        f.write(data)
                    clip_paths.append(p)

            if not clip_paths:
                raise RuntimeError("Todos los clips fallaron al descargar")

            logger.info(f"Clips descargados: {len(clip_paths)}")

            concat_path = _os.path.join(tmpdir, "concat.txt")
            raw_cat     = _os.path.join(tmpdir, "raw_cat.mp4")
            aud_path    = _os.path.join(tmpdir, "audio.mp3")
            out_path    = _os.path.join(tmpdir, "output.mp4")

            # ── Paso 1: Normalizar cada clip individualmente ───────────────────
            # Elimina audio, fuerza resolución/fps/codec idénticos → concat seguro
            norm_paths = []
            for ci, cp in enumerate(clip_paths):
                norm_path = _os.path.join(tmpdir, f"norm_{ci:02d}.mp4")
                norm_cmd = [
                    ffmpeg_bin, "-y", "-i", cp,
                    "-an",  # eliminar pista de audio del clip
                    "-vf", (
                        "scale=1280:720:force_original_aspect_ratio=decrease,"
                        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
                    ),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", "25", "-pix_fmt", "yuv420p",
                    norm_path,
                ]
                nr = _sp.run(norm_cmd, capture_output=True, timeout=120)
                if nr.returncode == 0 and _os.path.exists(norm_path) and _os.path.getsize(norm_path) > 1000:
                    norm_paths.append(norm_path)
                    logger.info(f"Clip {ci} normalizado OK: {_os.path.getsize(norm_path)} bytes")
                else:
                    logger.error(f"Clip {ci} normalización falló (rc={nr.returncode}): "
                                 f"{nr.stderr.decode(errors='replace')[:200]}")

            if not norm_paths:
                raise RuntimeError("Todos los clips fallaron al normalizar")

            logger.info(f"Clips normalizados: {len(norm_paths)}/{len(clip_paths)}")

            # ── Paso 2: Concat demuxer (todos idénticos → -c copy sin re-encode) ─
            with open(concat_path, "w") as cf:
                for np in norm_paths:
                    cf.write(f"file '{np}'\n")

            cat_proc = _sp.run([
                ffmpeg_bin, "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_path,
                "-c", "copy",
                raw_cat,
            ], capture_output=True, timeout=300)

            if cat_proc.returncode != 0:
                cat_err = cat_proc.stderr.decode(errors="replace")[:400]
                logger.error(f"Concat demuxer error: {cat_err}")
                # Fallback: usar solo el primer clip normalizado
                import shutil as _shutil
                _shutil.copy(norm_paths[0], raw_cat)
                logger.warning("Fallback: raw_cat = primer clip normalizado")
            else:
                cat_size = _os.path.getsize(raw_cat)
                logger.info(f"Concat demuxer OK: {len(norm_paths)} clips → raw_cat ({cat_size} bytes)")

            if has_audio:
                with open(aud_path, "wb") as f:
                    f.write(audio_result)
                proc = _sp.run(
                    [
                        ffmpeg_bin, "-y",
                        "-i", raw_cat,
                        "-i", aud_path,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                        "-c:a", "aac", "-b:a", "128k",
                        "-shortest",
                        "-movflags", "+faststart",
                        out_path,
                    ],
                    capture_output=True, timeout=120,
                )
                final_path = out_path if proc.returncode == 0 else raw_cat
                if proc.returncode != 0:
                    logger.warning(f"Audio combine error: {proc.stderr.decode(errors='replace')[:300]}")
            else:
                final_path = raw_cat

            with open(final_path, "rb") as f:
                _video_cache[vid_id] = f.read()

        out_mb = len(_video_cache[vid_id]) / 1_048_576
        serve_url = f"/api/lanzar/video-file/{vid_id}"
        logger.info(f"Video multi-escena listo: {vid_id} ({out_mb:.1f} MB) clips={len(clip_paths)} audio={has_audio}")
        return {"video_url": serve_url, "script": script, "has_audio": has_audio}

    except Exception as e:
        logger.error(f"Pipeline multi-escena falló: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/video-file/{vid_id}")
async def serve_video_file(vid_id: str):
    """Sirve el video comercial combinado (audio + video) desde la caché en memoria."""
    from fastapi.responses import Response
    data = _video_cache.get(vid_id)
    if not data:
        return JSONResponse({"error": "Video no encontrado o expirado"}, status_code=404)
    return Response(
        content=data,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'inline; filename="comercial_{vid_id[:8]}.mp4"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/test-pipeline")
async def test_pipeline():
    """Diagnóstico: verifica que TTS y ffmpeg funcionan en este entorno."""
    import subprocess as _sp2
    results: dict = {}

    # 1. imageio-ffmpeg
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        ffbin = get_ffmpeg_exe()
        proc  = _sp2.run([ffbin, "-version"], capture_output=True, timeout=10)
        first_line = proc.stdout.decode(errors="replace").split("\n")[0]
        results["ffmpeg"] = {"ok": proc.returncode == 0, "version": first_line, "path": ffbin}
    except Exception as e:
        results["ffmpeg"] = {"ok": False, "error": str(e)}

    # 2. gTTS
    try:
        import io
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text="prueba", lang="es", tld="com.mx").write_to_fp(buf)
        results["gtts"] = {"ok": True, "bytes": len(buf.getvalue())}
    except Exception as e:
        results["gtts"] = {"ok": False, "error": str(e)}

    # 3. Google TTS directo
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://translate.google.com/translate_tts",
                params={"ie": "UTF-8", "q": "prueba", "tl": "es", "tld": "com.mx", "client": "tw-ob"},
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
            )
        results["google_tts_direct"] = {"ok": r.status_code == 200 and len(r.content) > 100, "status": r.status_code, "bytes": len(r.content)}
    except Exception as e:
        results["google_tts_direct"] = {"ok": False, "error": str(e)}

    # 4. edge-tts
    try:
        import edge_tts
        data = b""
        async for chunk in edge_tts.Communicate("prueba", "es-MX-JorgeNeural").stream():
            if chunk["type"] == "audio":
                data += chunk["data"]
        results["edge_tts"] = {"ok": len(data) > 0, "bytes": len(data)}
    except Exception as e:
        results["edge_tts"] = {"ok": False, "error": str(e)}

    all_ok = results.get("ffmpeg", {}).get("ok") and (
        results.get("gtts", {}).get("ok") or
        results.get("google_tts_direct", {}).get("ok") or
        results.get("edge_tts", {}).get("ok")
    )
    return {"status": "ok" if all_ok else "degraded", "components": results}


@router.post("/ignore/{sku}")
async def ignore_sku(sku: str, request: Request):
    """Marca un SKU como ignorado para no mostrarlo en el lanzador."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE bm_sku_gaps SET status='ignored' WHERE user_id=? AND sku=?",
            (user_id, sku.upper())
        )
        await db.commit()
    return {"ok": True}


@router.post("/restore/{sku}")
async def restore_sku(sku: str, request: Request):
    """Restaura un SKU ignorado a unlaunched."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE bm_sku_gaps SET status='unlaunched' WHERE user_id=? AND sku=?",
            (user_id, sku.upper())
        )
        await db.commit()
    return {"ok": True}


@router.get("/prepare/{sku}")
async def prepare_sku(sku: str, request: Request):
    """Retorna datos del SKU desde BM + análisis de competidores en MeLi."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    # 1. Get stored gap data
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM bm_sku_gaps WHERE user_id=? AND sku=?",
            (user_id, sku.upper())
        )
        row = await cursor.fetchone()

    gap = dict(row) if row else {}

    # 2. Competitor search on MeLi
    competitors = []
    try:
        client = await get_meli_client()
        if client:
            query = f"{gap.get('brand','')} {gap.get('model','')} {sku}".strip()
            search = await client.get("/sites/MLM/search", params={"q": query, "limit": 5})
            for item in (search.get("results") or []):
                competitors.append({
                    "title": item.get("title"),
                    "price": item.get("price"),
                    "currency": item.get("currency_id"),
                    "condition": item.get("condition"),
                    "sold_qty": item.get("sold_quantity", 0),
                    "permalink": item.get("permalink"),
                    "seller": item.get("seller", {}).get("nickname"),
                })
    except Exception as e:
        logger.warning(f"Competitor search error: {e}")

    comp_prices = [c["price"] for c in competitors if c.get("price")]
    avg_comp = round(sum(comp_prices) / len(comp_prices), 0) if comp_prices else 0
    min_comp = min(comp_prices) if comp_prices else 0

    # Update DB with competitor data
    if comp_prices:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """UPDATE bm_sku_gaps SET
                   competitor_price=?, competitor_count=?, deal_price=?
                   WHERE user_id=? AND sku=?""",
                (avg_comp, len(comp_prices), round(min_comp * 0.95, 0), user_id, sku.upper())
            )
            await db.commit()
        gap["competitor_price"] = avg_comp
        gap["competitor_count"] = len(comp_prices)
        gap["deal_price"] = round(min_comp * 0.95, 0)

    return {
        "gap": gap,
        "competitors": competitors,
    }


@router.post("/ai-draft")
async def ai_draft(request: Request):
    """Genera borrador de listing usando IA (streaming)."""
    body = await request.json()
    sku  = body.get("sku", "")
    brand = body.get("brand", "")
    model = body.get("model", "")
    title_bm = body.get("product_title", "")
    category = body.get("category", "")
    price_mxn = body.get("suggested_price_mxn", 0)
    comp_price = body.get("competitor_price", 0)
    stock = body.get("stock_total", 0)
    image_url = body.get("image_url", "")

    system = (
        "Eres un experto en Mercado Libre México. "
        "Creas listings que venden: títulos optimizados para búsqueda, "
        "descripciones persuasivas, y atributos completos. "
        "Responde en español mexicano natural."
    )
    prompt = f"""Crea un listing completo para Mercado Libre México con este producto:

SKU: {sku}
Marca: {brand}
Modelo: {model}
Titulo en sistema: {title_bm}
Categoría: {category}
Precio sugerido: ${price_mxn:,.0f} MXN
Precio promedio competencia: ${comp_price:,.0f} MXN
Stock disponible: {stock} unidades
Imagen: {image_url if image_url else "no disponible"}

Genera:

## TÍTULO (max 60 caracteres)
REGLAS: Incluye marca + tipo de producto + característica clave + tamaño/capacidad. NO incluyas el número de modelo (va en ficha técnica). Optimiza para búsqueda, no para SEO técnico.
EJEMPLO BUENO: "Samsung Televisor QLED 4K Smart 65 Pulgadas"
EJEMPLO MALO: "Samsung QN65Q60CDFXZA 65 QLED 4K Smart TV"
[título aquí]

## DESCRIPCIÓN (3-4 párrafos, beneficios, especificaciones, garantía)
[descripción aquí]

## PUNTOS CLAVE (5 bullets cortos para la descripción)
• [punto 1]
• [punto 2]
• [punto 3]
• [punto 4]
• [punto 5]

## PRECIO RECOMENDADO
- Precio regular: $X MXN
- Precio deal/oferta: $X MXN
- Justificación: [breve explicación]

## TIPO DE PUBLICACIÓN RECOMENDADO
[Gold Special / Gold Pro] — [razón]

## PALABRAS CLAVE PARA TÍTULO
[5-8 palabras clave separadas por coma]
"""

    async def stream():
        try:
            async for chunk in claude_client.generate_stream(prompt, system=system, max_tokens=1200):
                yield chunk
        except Exception as e:
            yield f"\n\n[Error al generar: {e}]"

    return StreamingResponse(stream(), media_type="text/plain")


@router.post("/ai-draft-json")
async def ai_draft_json_endpoint(request: Request):
    """Genera borrador de listing como JSON estructurado (para wizard de publicación)."""
    import json as _json, re as _re
    body = await request.json()
    sku        = body.get("sku", "")
    brand      = body.get("brand", "")
    model      = body.get("model", "")
    title_bm   = body.get("product_title", "")
    category   = body.get("category", "")
    price_mxn  = body.get("suggested_price_mxn", 0)
    comp_price = body.get("competitor_price", 0)
    stock      = body.get("stock_total", 0)
    size       = body.get("size", "")
    upc        = body.get("upc", "")

    system = (
        "Eres un experto en eCommerce y Mercado Libre México (2026). "
        "Conoces las mejores prácticas de SEO en MeLi, copywriting de conversión y políticas de la plataforma. "
        "Retorna ÚNICAMENTE JSON válido, sin markdown, sin texto extra, sin comentarios."
    )
    size_line = f"- Tamaño/Pantalla: {size}" if size else ""
    upc_line  = f"- UPC/GTIN: {upc}" if upc else ""
    prompt = f"""Crea un listing de alta calidad para Mercado Libre México.

PRODUCTO:
- SKU: {sku}
- Marca: {brand}
- Modelo: {model}
- Nombre en sistema: {title_bm}
- Categoría: {category}
{size_line}
{upc_line}
- Precio sugerido: ${price_mxn:,.0f} MXN
- Precio competencia: ${comp_price:,.0f} MXN
- Stock: {stock} unidades

REGLAS MeLi 2026:
1. TÍTULO: máx 60 chars. Formato: Marca + Tipo de producto + Tecnología/Característica clave + Tamaño/Capacidad.
   - SIN número de modelo (va en ficha técnica)
   - SIN signos de puntuación ni mayúsculas innecesarias
   - SIN palabras como "nuevo", "oferta", "envío gratis"
   - Ejemplo correcto: "Samsung Televisor QLED 4K Smart 65 Pulgadas"
2. DESCRIPCIÓN: mínimo 4 párrafos. Incluye: beneficios principales, tecnología destacada, conectividad, compatibilidad, uso recomendado. Texto natural, orientado a compra.
3. BULLETS: 5 puntos cortos y contundentes. Cada uno destaca UN beneficio/característica. Empiezan con sustantivo o verbo de acción.
4. KEYWORDS: 8 palabras clave que los compradores mexicanos buscan en MeLi para este producto. Sin repetir palabras del título.
5. GARANTÍA: sugiere tipo y tiempo de garantía apropiados para el producto.

Retorna SOLO este JSON (sin markdown, sin texto extra):
{{
  "title": "string max 60 chars",
  "description": "string 4+ párrafos separados por \\n\\n",
  "bullet_points": ["bullet 1", "bullet 2", "bullet 3", "bullet 4", "bullet 5"],
  "keywords": ["kw1", "kw2", "kw3", "kw4", "kw5", "kw6", "kw7", "kw8"],
  "warranty_type": "Garantía del vendedor",
  "warranty_time": "3 meses",
  "price_regular": {int(price_mxn) if price_mxn else 0},
  "price_deal": {int(price_mxn * 0.9) if price_mxn else 0},
  "listing_type": "gold_special",
  "condition": "new"
}}"""

    try:
        raw = await claude_client.generate(prompt, system=system, max_tokens=1400)
        raw = _re.sub(r'^```[a-z]*\n?', '', raw.strip(), flags=_re.MULTILINE)
        raw = _re.sub(r'\n?```$', '', raw.strip(), flags=_re.MULTILINE)
        data = _json.loads(raw.strip())
        return data
    except Exception as e:
        logger.error(f"ai-draft-json error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/predict-category")
async def predict_category_endpoint(request: Request):
    """Predice categoría ML usando domain_discovery/search."""
    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    try:
        result = await client.get(
            "/sites/MLM/domain_discovery/search",
            params={"q": title, "limit": 4},
        )
        suggestions = []
        for r in (result if isinstance(result, list) else []):
            suggestions.append({
                "category_id":   r.get("category_id"),
                "category_name": r.get("category_name"),
                "domain_id":     r.get("domain_id"),
                "domain_name":   r.get("domain_name"),
            })
        return {"suggestions": suggestions}
    except Exception as e:
        logger.error(f"predict-category error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.get("/category-attrs/{category_id}")
async def category_attrs_endpoint(category_id: str):
    """Atributos requeridos de una categoría ML (endpoint público)."""
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(
                f"https://api.mercadolibre.com/categories/{category_id}/attributes"
            )
            if r.status_code == 200:
                attrs = r.json()
                required = [a for a in attrs if a.get("tags", {}).get("required")]
                optional = [a for a in attrs[:40] if not a.get("tags", {}).get("required")]
                return {"required": required[:20], "optional": optional[:20]}
            return JSONResponse({"error": f"ML {r.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/bm-images/{sku}")
async def bm_images_endpoint(sku: str):
    """Fetch product images from BinManager via GlobalStock_GetPhotoBySKU."""
    _BM_PHOTOS_URL = f"{_BM_BASE}/InventoryReport/InventoryReport/GlobalStock_GetPhotoBySKU"
    _IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    dbg: dict = {"sku": sku}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            logged_in = await _bm_login(http)
            dbg["login"] = logged_in
            if not logged_in:
                return JSONResponse({"images": [], "_debug": dbg, "error": "BM login failed"})

            r = await http.post(
                _BM_PHOTOS_URL,
                json={"COMPANYID": _BM_COMPANY, "SKU": sku.upper()},
                headers=_BM_AJAX,
                timeout=20,
            )
            dbg["status"] = r.status_code
            dbg["body_preview"] = r.text[:400]
            if r.status_code != 200:
                return JSONResponse({"images": [], "_debug": dbg, "error": f"BM status {r.status_code}"})

            data = r.json()
            dbg["data_keys"] = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            raw = data.get("JSONSKUFiles") if isinstance(data, dict) else None
            dbg["raw_type"] = type(raw).__name__
            dbg["raw_len"] = len(raw) if raw else 0

            if not raw:
                return JSONResponse({"images": [], "_debug": dbg})

            try:
                files = json.loads(raw) if isinstance(raw, str) else raw
                dbg["files_count"] = len(files)
            except Exception as e:
                dbg["parse_error"] = str(e)
                dbg["raw_sample"] = str(raw)[:200]
                return JSONResponse({"images": [], "_debug": dbg})

            images = []
            for f in files:
                url = (f.get("PhotoWebURL") or f.get("URL") or f.get("ImageName") or "").strip()
                ext = (f.get("PhotoExtension") or "").lower()
                if not url or (ext and ext not in _IMAGE_EXTENSIONS):
                    continue
                if not any(url.lower().endswith(e) for e in _IMAGE_EXTENSIONS):
                    continue
                images.append({"url": url, "type_name": f.get("TypeName") or ""})
                if len(images) >= 12:
                    break

            return {"images": images}

    except Exception as e:
        dbg["exception"] = str(e)
        logger.error(f"bm-images error for {sku}: {e}")
        return JSONResponse({"images": [], "_debug": dbg, "error": str(e)})


@router.post("/upload-picture")
async def upload_picture_endpoint(request: Request):
    """Descarga imagen desde URL y la sube a ML. Retorna picture_id."""
    body = await request.json()
    image_url = body.get("image_url", "").strip()
    if not image_url:
        return JSONResponse({"error": "image_url required"}, status_code=400)

    # Download image bytes
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            img_resp = await http.get(image_url)
            if img_resp.status_code != 200:
                return JSONResponse({"error": f"Could not fetch image: {img_resp.status_code}"}, status_code=400)
            img_bytes = img_resp.content
            content_type = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    except Exception as e:
        return JSONResponse({"error": f"Image fetch error: {e}"}, status_code=400)

    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(content_type, "jpg")

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    try:
        result = await client.post(
            "/pictures/items/upload",
            files={"file": (f"product.{ext}", img_bytes, content_type)},
        )
        pic_id = result.get("id")
        if not pic_id:
            return JSONResponse({"error": f"ML upload no id: {result}"}, status_code=502)
        return {"picture_id": pic_id, "secure_url": result.get("secure_url") or result.get("url", "")}
    except Exception as e:
        logger.error(f"upload-picture error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.get("/category-attributes/{category_id}")
async def get_category_attributes(category_id: str):
    """Atributos de la categoría ML con sus valores permitidos — para resolver value_id."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)
    try:
        raw = await client.get(f"/categories/{category_id}/attributes")
        if not isinstance(raw, list):
            return {"attributes": []}
        result = []
        for attr in raw:
            if not isinstance(attr, dict):
                continue
            tags = attr.get("tags") or {}
            is_req = bool(tags.get("required") or tags.get("catalog_required"))
            allowed = [
                {"id": v.get("id"), "name": v.get("name")}
                for v in (attr.get("allowed_values") or [])
                if isinstance(v, dict) and v.get("id")
            ]
            result.append({
                "id":             attr.get("id"),
                "name":           attr.get("name"),
                "type":           attr.get("value_type"),
                "required":       is_req,
                "allowed_values": allowed[:80],
            })
        return {"attributes": result}
    except Exception as e:
        logger.warning(f"category-attributes error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.post("/create-listing")
async def create_listing_endpoint(request: Request):
    """Crea el listing en Mercado Libre y marca el gap como lanzado."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    body = await request.json()
    category_id = body.get("category_id", "").strip()
    title       = body.get("title", "").strip()
    price       = body.get("price", 0)
    if not category_id or not title or not price:
        return JSONResponse({"error": "category_id, title y price son requeridos"}, status_code=400)

    description = body.get("description", "")
    sku         = body.get("sku", "")
    pictures    = body.get("pictures", [])

    warranty_type = body.get("warranty_type", "")
    warranty_time = body.get("warranty_time", "")

    item_payload: dict = {
        "title":              title,
        "category_id":        category_id,
        "price":              float(price),
        "currency_id":        "MXN",
        "available_quantity": int(body.get("available_quantity", 1)),
        "listing_type_id":    body.get("listing_type_id", "gold_special"),
        "condition":          body.get("condition", "new"),
        "buying_mode":        "buy_it_now",
    }
    if pictures:
        item_payload["pictures"] = [{"id": p} if isinstance(p, str) else p for p in pictures]
    if sku:
        item_payload["seller_custom_field"] = sku
    # Merge attributes from body + auto-build from known fields
    attrs = list(body.get("attributes") or [])
    if warranty_type and warranty_time:
        item_payload["sale_terms"] = [
            {"id": "WARRANTY_TYPE", "value_name": warranty_type},
            {"id": "WARRANTY_TIME", "value_name": warranty_time},
        ]
    if attrs:
        item_payload["attributes"] = attrs

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    from app.services.meli_client import MeliApiError as _MeliErr

    async def _post_item(payload: dict) -> dict:
        """POST /items capturando MeliApiError como dict."""
        try:
            return await client.post("/items", json=payload)
        except _MeliErr as exc:
            return {"_meli_error": str(exc), "_meli_body": getattr(exc, "body", {})}

    def _attrs_without_fn(payload: dict) -> dict:
        """Retorna copia de payload sin FAMILY_NAME en attributes."""
        import copy as _copy
        p = _copy.deepcopy(payload)
        if "attributes" in p:
            p["attributes"] = [a for a in p["attributes"] if not (isinstance(a, dict) and a.get("id") == "FAMILY_NAME")]
        return p

    # ── FAMILY_NAME: siempre buscar valores permitidos del catálogo ──────────
    fn_attr = next((a for a in attrs if isinstance(a, dict) and a.get("id") == "FAMILY_NAME"), None)
    fn_allowed_values: list = []
    try:
        cat_attrs = await client.get(f"/categories/{category_id}/attributes")
        if isinstance(cat_attrs, list):
            for ca in cat_attrs:
                if isinstance(ca, dict) and ca.get("id") == "FAMILY_NAME":
                    fn_allowed_values = [
                        av for av in (ca.get("allowed_values") or [])
                        if isinstance(av, dict) and av.get("id")
                    ]
                    logger.info(f"FAMILY_NAME catalog: {len(fn_allowed_values)} valores")
                    break
    except Exception as _fn_err:
        logger.warning(f"FAMILY_NAME catalog lookup failed: {_fn_err}")

    # Resolver value_id si fn_attr existe en los attrs del body
    if fn_attr and fn_allowed_values:
        wanted = (fn_attr.get("value_name") or "").lower()
        for av in fn_allowed_values:
            if av.get("name", "").lower() == wanted:
                fn_attr["value_id"] = av["id"]
                fn_attr["value_name"] = av["name"]
                logger.info(f"FAMILY_NAME exact: {av['id']} ({av['name']})")
                break
        if not fn_attr.get("value_id") and wanted:
            for av in fn_allowed_values:
                av_l = av.get("name", "").lower()
                if av_l and (av_l in wanted or wanted in av_l):
                    fn_attr["value_id"] = av["id"]
                    fn_attr["value_name"] = av["name"]
                    logger.info(f"FAMILY_NAME partial: {av['id']} ({av['name']})")
                    break
        if not fn_attr.get("value_id") and fn_allowed_values:
            fn_attr["value_id"] = fn_allowed_values[0]["id"]
            fn_attr["value_name"] = fn_allowed_values[0].get("name", "")
            logger.info(f"FAMILY_NAME forzado primer valor: {fn_allowed_values[0]['id']}")

    logger.info(f"ML attrs being sent: {[a.get('id') for a in attrs]}")
    if fn_attr:
        logger.info(f"FAMILY_NAME: value_id={fn_attr.get('value_id')!r} value_name={fn_attr.get('value_name')!r}")
    logger.info(f"fn_allowed_values: {len(fn_allowed_values)} valores del catálogo")

    import copy as _copy

    def _payload_add_fn(base: dict, val: dict) -> dict:
        """Copia con FAMILY_NAME añadido o reemplazado."""
        p = _copy.deepcopy(base)
        a_list = [a for a in (p.get("attributes") or []) if not (isinstance(a, dict) and a.get("id") == "FAMILY_NAME")]
        a_list.append({"id": "FAMILY_NAME", "value_id": val["id"], "value_name": val.get("name", "")})
        p["attributes"] = a_list
        return p

    try:
        # Intento 1: payload tal como viene
        result = await _post_item(item_payload)
        logger.info(f"ML intento 1: {'ok' if not result.get('_meli_error') else result['_meli_error']}")

        # Intentos 2..N: probar cada valor del catálogo para FAMILY_NAME
        if result.get("_meli_error") and "family_name" in result["_meli_error"].lower():
            for idx, fv in enumerate(fn_allowed_values[:15]):
                logger.warning(f"ML intento {2+idx}: FAMILY_NAME={fv['id']} ({fv.get('name')})")
                result = await _post_item(_payload_add_fn(item_payload, fv))
                logger.info(f"ML intento {2+idx}: {'ok' if not result.get('_meli_error') else result['_meli_error']}")
                if not result.get("_meli_error"):
                    break

        # Último recurso: sin attributes
        if result.get("_meli_error") and "family_name" in result["_meli_error"].lower():
            logger.warning("ML último recurso: sin attributes")
            bare = _copy.deepcopy(item_payload)
            bare.pop("attributes", None)
            result = await _post_item(bare)
            logger.info(f"ML último recurso: {'ok' if not result.get('_meli_error') else result['_meli_error']}")

        if result.get("_meli_error"):
            return JSONResponse({"error": result["_meli_error"]}, status_code=400)

        item_id = result.get("id")
        if not item_id:
            err = result.get("message") or result.get("error") or str(result)
            return JSONResponse({"error": err}, status_code=400)

        # Add description separately (ML doesn't accept it in the initial POST)
        if description:
            try:
                await client.post(
                    f"/items/{item_id}/description",
                    json={"plain_text": description},
                )
            except Exception as e:
                logger.warning(f"Description upload failed for {item_id}: {e}")

        # Mark gap as launched
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE bm_sku_gaps SET status='launched' WHERE user_id=? AND sku=?",
                (user_id, sku.upper()),
            )
            await db.commit()

        logger.info(f"Listing created: {item_id} for SKU {sku} ({user_id})")
        return {"ok": True, "item_id": item_id, "permalink": result.get("permalink", ""), "status": result.get("status", "")}

    except Exception as e:
        logger.error(f"create-listing error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()
