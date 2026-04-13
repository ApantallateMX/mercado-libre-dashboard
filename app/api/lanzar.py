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
_video_cache: dict[str, bytes] = {}

# Background video jobs — permite retornar job_id inmediatamente y evitar timeout de Railway
_video_jobs: dict = {}   # job_id → {"status": "processing"|"done"|"error", "video_url": None, "script": "", "has_audio": False, "error": None}

# Directorio de persistencia en disco (sobrevive múltiples requests en mismo container)
_VIDEO_DIR = __import__("pathlib").Path("/tmp/lanzar_videos")


def _persist_video(vid_id: str, data: bytes) -> None:
    """Guarda video en disco para sobrevivir múltiples requests."""
    try:
        _VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        (_VIDEO_DIR / f"{vid_id}.mp4").write_bytes(data)
    except Exception as e:
        logger.warning(f"No se pudo persistir video {vid_id}: {e}")


def _load_video(vid_id: str) -> bytes | None:
    """Carga video desde memoria o disco."""
    if vid_id in _video_cache:
        return _video_cache[vid_id]
    disk_path = _VIDEO_DIR / f"{vid_id}.mp4"
    if disk_path.exists():
        data = disk_path.read_bytes()
        _video_cache[vid_id] = data  # re-cargar en memoria
        return data
    return None

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
    """Fetch MTY/CDMX stock para un SKU usando condiciones correctas según sufijo.
    Usa Get_GlobalStock_InventoryBySKU_Warehouse para desglose por almacen y
    get_available_qty (Get_GlobalStock_InventoryBySKU CONCEPTID=1, LOCATIONID=47,62,68) para AvailableQTY real.
    InventoryBySKUAndCondicion_Quantity esta ROTO en el servidor (SQL binid error).
    """
    from app.services.binmanager_client import get_shared_bm
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
        bm_cli = await get_shared_bm()
        r_wh, avail = await asyncio.gather(
            http.post(_BM_WAREHOUSE_URL, json=wh_payload, headers=_BM_AJAX, timeout=15),
            bm_cli.get_available_qty(base),
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
        if isinstance(avail, Exception):
            avail = 0
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
            # Gap = SKU en BM con stock que NO está publicado en ESTA cuenta.
            # Cada cuenta se evalúa de forma independiente — un SKU publicado en
            # otra cuenta NO excluye que sea un gap en ésta.
            current_bm_skus = set(bm_map.keys())

            global_gaps_base = []  # datos base sin user_id/nickname — filtro ML se aplica por cuenta
            for base_sku, prod in bm_map.items():
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
            logger.info(f"[Fase2] {total_gaps_before_verify} BM SKUs con stock — evaluando por cuenta")

            # ── FASE 2b: Verificación seller_sku — safety net por cuenta ──────
            # La Fase 1 puede fallar en extraer SKUs de items inactivos/cerrados.
            # Búsqueda INVERSA por seller_sku en ESTA cuenta únicamente.
            # Un SKU encontrado en otra cuenta NO se excluye aquí — cada cuenta es independiente.
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

            # verified_not_gaps_per_account: por cada cuenta, SKUs encontrados via seller_sku
            # que existen en ML en esa cuenta (safety net para items que Phase 1 no capturó).
            verified_not_gaps_per_account: dict[str, set[str]] = {}

            all_bm_skus_list = [g["sku"] for g in global_gaps_base]
            _verify_total = max(len(all_bm_skus_list) * len(accounts), 1)
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

            for _acct in accounts:
                _uid = _acct["user_id"]
                _nick = _acct.get("nickname") or _uid
                _own_skus = account_ml_data[_uid]["meli_skus"]
                # Solo verificar candidatos que Phase 1 no capturó para esta cuenta
                _acct_candidates = [s for s in all_bm_skus_list if s not in _own_skus]
                _prog(50, "verifying",
                      f"Verificando {len(_acct_candidates)} SKUs en {_nick}...",
                      f"0/{len(_acct_candidates)} verificados")
                _cli = await get_meli_client(user_id=_uid)
                if not _cli:
                    verified_not_gaps_per_account[_uid] = set()
                    continue
                _acct_not_gaps: set[str] = set()
                try:
                    _tasks = [_sku_exists_tracked(sku, _uid, _cli) for sku in _acct_candidates]
                    _flags = await asyncio.gather(*_tasks, return_exceptions=True)
                    for sku, flag in zip(_acct_candidates, _flags):
                        if flag is True:
                            _acct_not_gaps.add(sku)
                            logger.info(f"[Fase2b] {sku} encontrado en {_nick} via seller_sku — no es gap para {_nick}")
                finally:
                    await _cli.close()
                verified_not_gaps_per_account[_uid] = _acct_not_gaps
                logger.info(f"[Fase2b] {_nick}: {len(_acct_not_gaps)} SKUs extra encontrados via seller_sku")

            logger.info(f"[Fase2] {total_gaps_before_verify} BM SKUs evaluados — gaps finales se calculan por cuenta")
            _prog(88, "saving", "Guardando resultados en base de datos...",
                  f"{total_gaps_before_verify} BM SKUs procesados")

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

                    # Gaps para esta cuenta = BM SKUs con stock que NO están en ML de esta cuenta
                    _acct_own_skus    = account_ml_data[user_id]["meli_skus"]
                    _acct_not_by_api  = verified_not_gaps_per_account.get(user_id, set())
                    account_gaps = [
                        g for g in global_gaps_base
                        if g["sku"] not in _acct_own_skus and g["sku"] not in _acct_not_by_api
                    ]
                    logger.info(f"  {nickname}: {len(account_gaps)} gaps para esta cuenta")

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
                    # 2. Eliminar gaps que ahora están publicados en ESTA cuenta
                    _skus_to_remove = _acct_own_skus | _acct_not_by_api
                    if _skus_to_remove:
                        for chunk_start in range(0, len(_skus_to_remove), 500):
                            chunk = list(_skus_to_remove)[chunk_start:chunk_start + 500]
                            await db.execute(
                                """DELETE FROM bm_sku_gaps
                                   WHERE user_id=? AND status='unlaunched'
                                   AND sku IN ({})""".format(",".join("?" * len(chunk))),
                                [user_id] + chunk
                            )
                    # 3. Upsert gaps de esta cuenta
                    for g_base in account_gaps:
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
    status_filter = status if status in ("unlaunched", "ignored", "launched") else "unlaunched"

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
        launched_cur = await db.execute(
            "SELECT COUNT(*) FROM bm_sku_gaps WHERE user_id=? AND status='launched'",
            (user_id,)
        )
        launched_total = (await launched_cur.fetchone())[0]

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
        "user_id":        user_id,
        "total":          total,
        "badge_total":    badge_total,
        "launched_total": launched_total,
        "page":           page,
        "per_page":       per_page,
        "pages":          max(1, -(-total // per_page)),
        "items":          items,
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
            "-stream_loop", "-1",  # loop slideshow hasta que termine el audio
            "-i", slideshow_raw,
            "-i", aud_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",            # corta cuando termina el audio
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
    """Inicia el pipeline de comercial en background y retorna job_id inmediatamente.
    Usar GET /video-job/{job_id} para verificar el estado.
    """
    import uuid as _uuid
    from app.services import replicate_client

    if not replicate_client.is_available():
        return JSONResponse({"error": "REPLICATE_API_KEY no configurada"}, status_code=503)

    body = await request.json()
    job_id = str(_uuid.uuid4())
    _video_jobs[job_id] = {"status": "processing", "video_url": None, "script": "", "has_audio": False, "error": None}
    asyncio.ensure_future(_run_video_pipeline(job_id, body))
    return {"job_id": job_id, "status": "processing"}


@router.get("/video-job/{job_id}")
async def video_job_status(job_id: str):
    """Retorna el estado de un job de generación de video."""
    job = _video_jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return job


async def _run_video_pipeline(job_id: str, body: dict):
    """Pipeline completo de comercial en español (corre en background):
    1. Claude genera guion en español de México (30-40 palabras)
    2. ElevenLabs convierte guion a voz profesional en español
    3. Si hay imágenes AI: slideshow con ffmpeg; si no: minimax/video-01
    4. ffmpeg combina audio + video en un solo MP4
    Actualiza _video_jobs[job_id] cuando termina.
    """
    import json as _json
    import subprocess as _sp
    import tempfile as _tf
    import uuid as _uuid
    import os as _os
    from app.services import replicate_client, elevenlabs_client

    try:
        brand          = body.get("brand", "")
        model          = body.get("model", "")
        title          = body.get("title", "") or body.get("product_title", "")
        category       = body.get("category", "")
        size           = str(body.get("size", "") or "").strip()
        first_frame    = (body.get("first_frame_image") or "").strip()
        ai_image_urls  = [u for u in (body.get("ai_image_urls") or []) if isinstance(u, str) and u.startswith("http")]
        script_override = (body.get("script_override") or "").strip()

        # ── Step 1: Script — use override if provided, else generate with Claude ──
        script = ""
        scenes: list = []
    
        if script_override:
            script = script_override
            logger.info(f"Using script_override ({len(script.split())} words): {script[:80]}...")
        else:
            script = ""
    
        # Always generate cinematic scene descriptions with Claude for text-to-video
        # (scenes are visual prompts — separate from the narration script)
        import json as _json_inner, re as _re
        product_desc = " ".join(filter(None, [brand, model])).strip() or title
        claude_system = (
            "You are a world-class TV commercial director creating a premium 30-second ad for Mercado Libre México.\n"
            "The product can be ANYTHING — adapt every scene specifically to THIS product and its actual use case.\n\n"
            "Respond ONLY with valid JSON (no markdown, no backticks, no extra text):\n"
            '{"script": "...", "scenes": ["scene1", "scene2", "scene3"]}\n\n'
            "SCRIPT rules (if empty, generate one):\n"
            "- Mexican Spanish, MINIMUM 100 words, maximum 120 words (CRITICAL: under 100 words = video too short)\n"
            "- Exciting aspirational tone — describe benefits, lifestyle, emotions, use cases\n"
            "- Use varied sentence rhythm: short punchy lines mixed with longer flowing descriptions\n"
            "- Never mention model numbers, SKU codes, or technical specs directly\n"
            "- End with: Disponible ahora en Mercado Libre.\n\n"
            "SCENES rules — 3 items, each max 55 words, in English:\n"
            "- Each scene is a TEXT PROMPT for an AI video model — describe ONLY what the camera sees\n"
            "- Show the product being USED in REAL LIFE by real people — NOT product photography\n"
            "- Be extremely specific to this product category and its use case\n"
            "- Each scene must be VISUALLY DIFFERENT (location, action, lighting, camera angle)\n"
            "- Include: camera movement (slow push-in / orbit / pan), lighting (golden hour / soft natural / dramatic), mood\n"
            "- Make it aspirational: beautiful settings, happy people, satisfying moments\n"
            "- NO product logos, NO text overlays, NO watermarks in scene descriptions\n"
            "- Premium cinematic quality, 4K photorealistic commercial look\n"
            "Example for food containers: 'Slow push-in on a woman's hands elegantly organizing vibrant colorful salads "
            "into clear glass containers on white marble countertop, warm morning kitchen light, shallow depth of field, "
            "satisfying and clean aesthetic'\n"
            "Example for TV mount: 'Smiling family sitting on cozy modern sofa watching a large mounted TV together, "
            "golden evening light through large windows, slow wide-angle pull-back revealing organized living room'"
        )
        claude_user = (
            f"Producto: {title}\n"
            f"Marca: {brand}\n"
            f"Categoria: {category}\n"
            f"Guion existente: {script or 'generar uno nuevo'}\n\n"
            "Genera las 3 escenas cinematicas EN INGLES y el guion EN ESPANOL."
        )
        try:
            # Use Vision when product images are available — Claude SEES the product → specific scenes
            if ai_image_urls:
                vision_prompt = (
                    f"Producto: {title}\nMarca: {brand}\nCategoria: {category}\n"
                    f"Guion existente: {script or 'generar uno nuevo'}\n\n"
                    "Miras las imágenes reales del producto. "
                    "Genera las 3 escenas cinematicas EN INGLES y el guion EN ESPANOL.\n"
                    "Las escenas deben reflejar EXACTAMENTE este producto: su apariencia, color, tamaño y uso real."
                )
                logger.info(f"Claude Vision: analizando {len(ai_image_urls)} imágenes del producto...")
                raw = (await claude_client.generate_with_images(
                    prompt=vision_prompt, image_urls=ai_image_urls[:3],
                    system=claude_system, max_tokens=800
                )).strip()
            else:
                raw = (await claude_client.generate(prompt=claude_user, system=claude_system, max_tokens=800)).strip()
            if "```" in raw:
                raw = raw[raw.index("```") + 3:]
                if raw.startswith("json"): raw = raw[4:]
                raw = raw[:raw.index("```")] if "```" in raw else raw
            raw = raw.strip()
            if not raw.startswith("{"):
                m = _re.search(r'\{[\s\S]*\}', raw)
                if m: raw = m.group(0)
            parsed  = _json_inner.loads(raw)
            scenes  = [s.strip() for s in (parsed.get("scenes") or []) if isinstance(s, str) and s.strip()]
            if not script:
                script = parsed.get("script", "").strip().strip('"').strip("'")
            logger.info(f"Claude {'Vision' if ai_image_urls else 'text'} scenes: {len(scenes)} | script: {len(script.split())} words")
        except Exception as e:
            logger.warning(f"Claude scenes failed: {e}")
            scenes = []
    
        # Fallback scenes if Claude failed — generic but product-focused
        if len(scenes) < 3:
            prod = product_desc or title or "product"
            scenes = [
                f"Professional lifestyle scene showing {prod} being used in a modern home, warm natural lighting, "
                f"slow cinematic push-in, beautiful and satisfying, premium commercial quality",
                f"Close-up detail shot of {prod} in use, soft bokeh background, warm studio lighting, "
                f"elegant hands interacting with it, macro photography style, aspirational",
                f"Happy family or person enjoying the benefits of {prod}, cozy modern home setting, "
                f"golden hour light through windows, slow pull-back wide shot, authentic and aspirational",
            ]
        if not script:
            product_line = " ".join(filter(None, [brand, model, title])).strip()
            script = (
                f"{product_line} — calidad y funcionalidad para tu vida diaria. "
                f"Disenado para hacer tu vida mas facil, mas organizada, mas feliz. "
                f"Disponible ahora en Mercado Libre."
            )
    
        # Obtener ruta absoluta del binario ffmpeg (imageio-ffmpeg lo bundlea)
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            ffmpeg_bin = get_ffmpeg_exe()
            logger.info(f"ffmpeg bin: {ffmpeg_bin}")
        except Exception as _e:
            ffmpeg_bin = "ffmpeg"
            logger.warning(f"imageio-ffmpeg no disponible ({_e}), usando 'ffmpeg' del PATH")
    
        vid_id = str(_uuid.uuid4())
    
        # 10 prompts — SOLO movimiento de cámara, sin describir escenas ni objetos
        # Esto evita que Minimax "invente" contenido distinto al producto real de la foto
        _motion_prompts = [
            "slow cinematic camera zoom-out, product stays centered and unchanged, studio lighting",
            "gentle slow camera pan right, product fully visible and sharp, warm studio light",
            "slow camera push-in toward the product, product remains clear and undistorted, professional lighting",
            "slow camera pan left to right, product centered and unchanged, clean background, commercial quality",
            "smooth slow camera orbit around the product, product stays fully visible, warm ambient light",
            "camera slowly tilts up revealing the product from bottom to top, product unchanged, studio lighting",
            "slow camera pull back showing product in full, no scene change, premium commercial look",
            "gentle camera drift forward, product sharp and unchanged in frame, cinematic lighting",
            "slow camera arc from side to front angle, product stays clear and intact, warm studio light",
            "smooth slow camera zoom-in to product details, product sharp and unchanged, editorial lighting",
        ]
    
        # ────────────────────────────────────────────────────────────────────────────
        # Helpers comunes a ambos paths
        # ────────────────────────────────────────────────────────────────────────────
        import re as _re_dur
    
        def _probe_dur(path: str) -> float:
            r = _sp.run([ffmpeg_bin, "-i", path], capture_output=True, timeout=15)
            m = _re_dur.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)",
                               r.stderr.decode(errors="replace"))
            if m:
                return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
            return 5.0
    
        def _xfade_and_combine(norm_paths, clip_durs, audio_bytes, has_audio, tmpdir):
            """xfade + tpad/audio combine. Devuelve bytes del video final."""
            FADE       = 0.5
            concat_txt = _os.path.join(tmpdir, "concat.txt")
            xfade_path = _os.path.join(tmpdir, "xfaded.mp4")
            aud_path   = _os.path.join(tmpdir, "audio.mp3")
            out_path   = _os.path.join(tmpdir, "output.mp4")
    
            if len(norm_paths) == 1:
                xfade_path = norm_paths[0]
            else:
                inputs_args = []
                for p in norm_paths:
                    inputs_args += ["-i", p]
                fc_parts = []
                prev   = "[0:v]"
                offset = 0.0
                for i in range(1, len(norm_paths)):
                    offset += max(clip_durs[i - 1] - FADE, 0.1)
                    lbl = f"[v{i}]" if i < len(norm_paths) - 1 else "[vout]"
                    fc_parts.append(f"{prev}[{i}:v]xfade=transition=fade:duration={FADE}:offset={offset:.3f}{lbl}")
                    prev = lbl
                xr = _sp.run(
                    [ffmpeg_bin, "-y"] + inputs_args + [
                        "-filter_complex", ";".join(fc_parts), "-map", "[vout]",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                        "-r", "25", "-pix_fmt", "yuv420p", xfade_path,
                    ],
                    capture_output=True, timeout=300,
                )
                if xr.returncode != 0:
                    logger.warning(f"xfade falló, concat simple: {xr.stderr.decode(errors='replace')[:150]}")
                    with open(concat_txt, "w") as cf:
                        for p in norm_paths:
                            cf.write(f"file '{p}'\n")
                    _sp.run([ffmpeg_bin, "-y", "-f", "concat", "-safe", "0",
                             "-i", concat_txt, "-c", "copy", xfade_path],
                            capture_output=True, timeout=300)
                else:
                    logger.info(f"xfade OK: {len(norm_paths)} clips → {_os.path.getsize(xfade_path)//1024} KB")
    
            if has_audio and audio_bytes:
                with open(aud_path, "wb") as af:
                    af.write(audio_bytes)
                # Solo usar stream_loop si el video es más corto que el audio estimado
                # (evita loops visibles cuando tenemos 3 clips ~30s ≥ audio ~28s)
                total_video_dur = sum(clip_durs) - FADE * max(0, len(norm_paths) - 1)
                est_audio_dur = len(audio_bytes) / (128 * 1024 / 8)  # aprox a 128kbps
                use_loop = total_video_dur < (est_audio_dur - 1.0)
                loop_flag = ["-stream_loop", "-1"] if use_loop else []
                logger.info(f"xfade+audio: video={total_video_dur:.1f}s audio≈{est_audio_dur:.1f}s loop={use_loop}")
                proc = _sp.run(
                    [
                        ffmpeg_bin, "-y",
                    ] + loop_flag + [
                        "-i", xfade_path,
                        "-i", aud_path,
                        "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                        "-c:a", "aac", "-b:a", "128k",
                        "-shortest", "-movflags", "+faststart",
                        out_path,
                    ],
                    capture_output=True, timeout=180,
                )
                final = out_path if proc.returncode == 0 else xfade_path
                if proc.returncode != 0:
                    logger.warning(f"Audio combine err: {proc.stderr.decode(errors='replace')[:150]}")
            else:
                final = xfade_path
    
            with open(final, "rb") as vf:
                return vf.read()
    
        # ────────────────────────────────────────────────────────────────────────────
        # PRIMARY: T2V con escenas específicas del producto (generadas por Claude Vision)
        # Las imágenes se usan como REFERENCIA para Claude — no para animar
        # → video real con movimiento, personas, lifestyle — NO foto estática animada
        # FALLBACK: zoompan ffmpeg (solo si T2V falla y hay imágenes disponibles)
        # ────────────────────────────────────────────────────────────────────────────
        logger.info(f"=== T2V PRIMARY: {len(scenes)} escenas {'(Vision)' if ai_image_urls else '(text)'} ===")

        _t2v_ok = False

        async def _gen_t2v_clip(idx: int):
            _t2v_pool = scenes if scenes else _motion_prompts
            return await replicate_client.generate_video_t2v(_t2v_pool[idx % len(_t2v_pool)])

        _all_t2v = await asyncio.gather(
            elevenlabs_client.generate_audio(script),
            *[_gen_t2v_clip(i) for i in range(3)],   # 3 clips paralelos — menos carga en Replicate
            return_exceptions=True,
        )
        audio_result     = _all_t2v[0]
        clip_url_results = list(_all_t2v[1:])
        has_audio        = isinstance(audio_result, bytes) and bool(audio_result)

        # Log failures for debugging
        for _ci, _cr in enumerate(clip_url_results):
            if isinstance(_cr, Exception):
                logger.error(f"T2V clip {_ci} falló: {type(_cr).__name__}: {str(_cr)[:300]}")

        clip_urls = [r for r in clip_url_results if isinstance(r, str) and r.startswith("http")]
        logger.info(f"T2V clips OK: {len(clip_urls)}/{len(clip_url_results)}")

        # Reintentar secuencialmente hasta tener 3 clips — evita loops visibles en el video final
        _retry_idx = len(clip_url_results)
        _retry_max = 3  # máximo 3 reintentos extra
        while len(clip_urls) < 3 and _retry_max > 0:
            _retry_max -= 1
            logger.info(f"Solo {len(clip_urls)} clips — reintentando clip extra secuencial (idx={_retry_idx})...")
            try:
                extra_url = await _gen_t2v_clip(_retry_idx)
                if isinstance(extra_url, str) and extra_url.startswith("http"):
                    clip_urls.append(extra_url)
                    logger.info(f"Clip extra OK ({len(clip_urls)} total): {extra_url[:60]}")
                else:
                    logger.warning(f"Clip extra idx={_retry_idx} devolvió respuesta inválida")
                    break
            except Exception as _ex_err:
                logger.warning(f"Clip extra idx={_retry_idx} falló: {_ex_err}")
                break
            _retry_idx += 1

        if clip_urls:
            try:
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
                        raise RuntimeError("T2V: todos los clips fallaron al descargar")

                    norm_paths = []
                    for ci, cp in enumerate(clip_paths):
                        norm_path = _os.path.join(tmpdir, f"norm_{ci:02d}.mp4")
                        nr = _sp.run(
                            [
                                ffmpeg_bin, "-y", "-i", cp, "-an",
                                "-filter_complex", (
                                    "[0:v]split=2[bg][fg];"
                                    "[bg]scale=720:1280:force_original_aspect_ratio=increase,"
                                    "crop=720:1280,setsar=1,boxblur=40:5[blurred];"
                                    "[fg]scale=720:1280:force_original_aspect_ratio=decrease,setsar=1[sharp];"
                                    "[blurred][sharp]overlay=(W-w)/2:(H-h)/2[out]"
                                ),
                                "-map", "[out]",
                                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                                "-r", "25", "-pix_fmt", "yuv420p", norm_path,
                            ],
                            capture_output=True, timeout=120,
                        )
                        if nr.returncode == 0 and _os.path.exists(norm_path) and _os.path.getsize(norm_path) > 1000:
                            norm_paths.append(norm_path)

                    if not norm_paths:
                        raise RuntimeError("T2V: todos los clips fallaron al normalizar")

                    clip_durs   = [_probe_dur(p) for p in norm_paths]
                    video_bytes = _xfade_and_combine(norm_paths, clip_durs, audio_result, has_audio, tmpdir)
                    _video_cache[vid_id] = video_bytes

                _persist_video(vid_id, _video_cache[vid_id])
                out_mb = len(_video_cache[vid_id]) / 1_048_576
                logger.info(f"T2V video listo: {vid_id} ({out_mb:.1f} MB) clips={len(norm_paths)}")
                _video_jobs[job_id] = {"status": "done", "video_url": f"/api/lanzar/video-file/{vid_id}", "script": script, "has_audio": has_audio, "error": None, "method": "t2v"}
                _t2v_ok = True
                return

            except Exception as _t2v_err:
                logger.error(f"T2V pipeline falló: {_t2v_err}", exc_info=True)

        # ────────────────────────────────────────────────────────────────────────────
        # FALLBACK: zoompan ffmpeg (solo si hay imágenes del producto)
        # ────────────────────────────────────────────────────────────────────────────
        if not ai_image_urls:
            _video_jobs[job_id] = {"status": "error", "video_url": None, "script": script, "has_audio": False, "error": "T2V: sin clips y sin imágenes para fallback"}
            return

        logger.info(f"=== ZOOMPAN FALLBACK: {len(ai_image_urls)} fotos del producto ===")

        FPS   = 25
        DUR_S = 5.0   # segundos por clip

        # 10 efectos de cámara: solo movimiento, el producto real siempre visible
        _ZP = [
            "zoompan=z='zoom+0.0006':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='1.05':x='(iw-iw/zoom)*on/124':y='ih/2-(ih/zoom/2)':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='1.05':x='(iw-iw/zoom)*(1-on/124)':y='ih/2-(ih/zoom/2)':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='1.05':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*on/124':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='1.05':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(1-on/124)':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='zoom+0.0004':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='1.07':x='(iw-iw/zoom)*on/124':y='(ih-ih/zoom)*on/124':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='zoom+0.0005':x='min((iw-iw/zoom)*on/124,iw-iw/zoom)':y='ih/2-(ih/zoom/2)':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='1.07':x='(iw-iw/zoom)*(1-on/124)':y='(ih-ih/zoom)*(1-on/124)':fps=25,scale=720:1280,setsar=1",
            "zoompan=z='zoom+0.0007':x='iw/2-(iw/zoom/2)':y='max(ih*0.05-(ih/zoom/2),0)':fps=25,scale=720:1280,setsar=1",
        ]

        # Filtro portrait: fondo desenfocado 9:16 + producto centrado nítido
        _PFC = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,setsar=1,boxblur=40:5[blurred];"
            "[fg]scale=720:1280:force_original_aspect_ratio=decrease,setsar=1[sharp];"
            "[blurred][sharp]overlay=(W-w)/2:(H-h)/2[norm];"
        )

        async def _dl_photo(url: str):
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as _c:
                    r = await _c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    return r.content if r.status_code == 200 and len(r.content) > 100 else None
            except Exception as _e:
                logger.warning(f"Photo DL error: {_e}")
                return None

        # Descargar TTS + fotos en paralelo
        _all_dl = await asyncio.gather(
            elevenlabs_client.generate_audio(script),
            *[_dl_photo(u) for u in ai_image_urls[:5]],
            return_exceptions=True,
        )
        audio_result = _all_dl[0]
        photos       = [p for p in _all_dl[1:] if isinstance(p, bytes) and len(p) > 100]
        has_audio    = isinstance(audio_result, bytes) and bool(audio_result)
        logger.info(f"TTS: {'OK' if has_audio else 'X'}, fotos: {len(photos)}")

        if not photos:
            _video_jobs[job_id] = {"status": "error", "video_url": None, "script": script, "has_audio": False, "error": "No se pudieron descargar fotos del producto"}
            return

        _ev_loop = asyncio.get_event_loop()

        def _make_zp_clip(pbytes: bytes, eff_idx: int, ci: int, tdir: str):
            ph  = _os.path.join(tdir, f"ph_{ci:02d}.jpg")
            out = _os.path.join(tdir, f"norm_{ci:02d}.mp4")
            with open(ph, "wb") as _f:
                _f.write(pbytes)
            fc = _PFC + f"[norm]{_ZP[eff_idx % len(_ZP)]}[out]"
            cmd = [
                ffmpeg_bin, "-y",
                "-loop", "1", "-i", ph, "-t", str(DUR_S),
                "-filter_complex", fc, "-map", "[out]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p", out,
            ]
            r = _sp.run(cmd, capture_output=True, timeout=90)
            if r.returncode == 0 and _os.path.exists(out) and _os.path.getsize(out) > 1000:
                logger.info(f"ZP clip {ci} (eff {eff_idx}): {_os.path.getsize(out)//1024} KB")
                return out
            logger.error(f"ZP clip {ci} falló: {r.stderr.decode(errors='replace')[:200]}")
            return None

        try:
            with _tf.TemporaryDirectory() as tmpdir:
                zp_tasks = [
                    _ev_loop.run_in_executor(
                        None, _make_zp_clip,
                        photos[(ci // 2) % len(photos)],
                        ci, ci, tmpdir,
                    )
                    for ci in range(10)
                ]
                zp_results = await asyncio.gather(*zp_tasks, return_exceptions=True)
                norm_paths = [p for p in zp_results if isinstance(p, str)]

                if not norm_paths:
                    raise RuntimeError("Todos los clips zoompan fallaron")

                logger.info(f"Clips zoompan OK: {len(norm_paths)}/10")
                clip_durs = [DUR_S] * len(norm_paths)

                video_bytes = _xfade_and_combine(norm_paths, clip_durs, audio_result, has_audio, tmpdir)
                _video_cache[vid_id] = video_bytes

            _persist_video(vid_id, _video_cache[vid_id])
            out_mb = len(_video_cache[vid_id]) / 1_048_576
            logger.info(f"Zoompan video listo: {vid_id} ({out_mb:.1f} MB) clips={len(norm_paths)}")
            _video_jobs[job_id] = {"status": "done", "video_url": f"/api/lanzar/video-file/{vid_id}", "script": script, "has_audio": has_audio, "error": None, "method": "zoompan"}
            return

        except Exception as _zp_err:
            logger.error(f"Zoompan pipeline falló: {_zp_err}", exc_info=True)
            _video_jobs[job_id] = {"status": "error", "video_url": None, "script": script, "has_audio": False, "error": str(_zp_err)}
            return

    except Exception as _outer_e:
        logger.error(f"_run_video_pipeline error inesperado: {_outer_e}", exc_info=True)
        _video_jobs[job_id] = {"status": "error", "video_url": None, "script": "", "has_audio": False, "error": str(_outer_e)}


@router.get("/video-file/{vid_id}")
async def serve_video_file(vid_id: str):
    """Sirve el video comercial combinado (audio + video) desde caché o disco."""
    from fastapi.responses import Response
    data = _load_video(vid_id)
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


@router.post("/mark-launched/{sku}")
async def mark_launched_sku(sku: str, request: Request):
    """Marca un SKU como lanzado con datos externos (publicado fuera del wizard)."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    body = await request.json()
    item_id   = body.get("item_id", "")
    ml_title  = body.get("title", "")
    ml_price  = float(body.get("price", 0))
    permalink = body.get("permalink", "")
    condition = body.get("condition", "new")

    if not item_id:
        return JSONResponse({"error": "item_id required"}, status_code=400)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """UPDATE bm_sku_gaps SET
               status='launched',
               ml_item_id=?, ml_title=?, ml_price=?,
               ml_permalink=?, ml_condition=?, launched_at=CURRENT_TIMESTAMP
               WHERE user_id=? AND sku=?""",
            (item_id, ml_title, ml_price, permalink, condition, user_id, sku.upper())
        )
        await db.commit()
    return {"ok": True}


@router.post("/relaunch/{sku}")
async def relaunch_sku(sku: str, request: Request):
    """Resetea un SKU lanzado a unlaunched para poder relanzarlo desde el wizard."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """UPDATE bm_sku_gaps SET
               status='unlaunched',
               ml_item_id='', ml_title='', ml_price=0,
               ml_permalink='', ml_condition='', launched_at=NULL
               WHERE user_id=? AND sku=?""",
            (user_id, sku.upper())
        )
        await db.commit()
    return {"ok": True}


@router.post("/delete-launched/{sku}")
async def delete_launched_sku(sku: str, request: Request):
    """Cierra el listing en ML (best effort) y resetea el SKU a unlaunched."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    # Obtener ml_item_id de la DB
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT ml_item_id FROM bm_sku_gaps WHERE user_id=? AND sku=?",
            (user_id, sku.upper())
        )
        row = await cur.fetchone()

    ml_item_id = row[0] if row else None

    # Intentar cerrar en ML (best effort — puede que ya no exista)
    ml_closed = False
    if ml_item_id:
        try:
            client = await get_meli_client()
            if client:
                await client.put(f"/items/{ml_item_id}", json={"status": "closed"})
                ml_closed = True
                logger.info(f"Listing {ml_item_id} cerrado en ML para SKU {sku}")
        except Exception as e:
            logger.warning(f"No se pudo cerrar {ml_item_id} en ML (puede ya no existir): {e}")

    # Resetear DB independientemente del resultado de ML
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """UPDATE bm_sku_gaps SET
               status='unlaunched',
               ml_item_id='', ml_title='', ml_price=0,
               ml_permalink='', ml_condition='', launched_at=NULL
               WHERE user_id=? AND sku=?""",
            (user_id, sku.upper())
        )
        await db.commit()

    return {"ok": True, "ml_closed": ml_closed}


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
1. TÍTULO: ENTRE 55-60 caracteres (OBLIGATORIO — nunca menos de 55). Usa TODO el espacio disponible. 59 chars > 49 chars.
   Formato: Marca + Tipo de producto + Tecnología/Característica clave + Tamaño/Capacidad.
   - SIN número de modelo (va en ficha técnica)
   - SIN signos de puntuación ni mayúsculas innecesarias
   - SIN palabras como "nuevo", "oferta", "envío gratis"
   - Ejemplo correcto (60 chars): "Samsung Televisor QLED 4K Smart HDR 65 Pulgadas Google TV"
   - Si el título queda corto, agrega características adicionales del producto hasta llegar a 55-60 chars.
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


@router.post("/estimate-dimensions")
async def estimate_dimensions_endpoint(request: Request):
    """Estima dimensiones del paquete por tipo/tamaño de producto.
    Para TVs usa tabla de referencia por pulgadas. Para otros, usa Claude.
    """
    import re as _re
    body     = await request.json()
    brand    = (body.get("brand") or "").strip()
    model    = (body.get("model") or "").strip()
    title    = (body.get("title") or body.get("product_title") or "").strip()
    category = (body.get("category") or "").strip()
    size     = str(body.get("size") or "").strip()

    cat_lower = category.lower()
    is_tv = any(w in cat_lower for w in ("television", "televisor", "tv"))
    if not is_tv:
        # Check title/model for TV hints
        combined = f"{title} {model}".lower()
        is_tv = any(w in combined for w in ("tv", "televisor", "television", "pfl", "pus", "qled", "oled", "uled"))

    # ── TV: lookup table por tamaño en pulgadas ───────────────────────────────
    if is_tv:
        size_in = None
        # Try size field first
        m = _re.search(r'(\d{2,3})', size)
        if m:
            size_in = int(m.group(1))
        # Fallback: search title/model for inches — with or without unit suffix
        if not size_in:
            # Pattern 1: explicit unit suffix ("40 pulgadas", '43"', etc.)
            for src in [title, model, brand]:
                m2 = _re.search(r'\b(\d{2,3})\s*(?:"|pulgadas|pulg|inches?|in\b)', src, _re.I)
                if m2:
                    size_in = int(m2.group(1))
                    break
        if not size_in:
            # Pattern 2: TV model numbers embed screen size digits anywhere in the string
            # e.g. 40PQF7446, K-55S20M2, XBR-65X900H, UN55TU7000, OLED55C2PSA
            for src in [model, title]:
                for m3 in _re.finditer(r'(\d{2,3})[A-Z]', src.upper().strip()):
                    candidate = int(m3.group(1))
                    if 24 <= candidate <= 100:  # sane TV size range
                        size_in = candidate
                        break
                if size_in:
                    break

        # Tabla: dimensiones de caja de embalaje incluyendo espuma (cm) + peso (kg)
        _tv = {
            24: (70,  46,  12,  5.0),
            32: (90,  57,  12,  7.0),
            40: (110, 67,  14, 10.0),
            43: (116, 72,  14, 12.0),
            50: (130, 82,  15, 16.0),
            55: (144, 90,  16, 20.0),
            58: (152, 94,  16, 22.0),
            65: (170, 106, 17, 28.0),
            70: (181, 114, 18, 33.0),
            75: (193, 120, 19, 38.0),
            85: (218, 136, 21, 48.0),
        }
        if size_in:
            closest = min(_tv.keys(), key=lambda x: abs(x - size_in))
            h, w, l, wt = _tv[closest]
            return {
                "height_cm": h, "width_cm": w, "length_cm": l, "weight_kg": wt,
                "confidence": "table", "note": f"TV {closest}\" — caja de embalaje típica"
            }
        # TV pero sin tamaño conocido — retorna tabla para que el frontend use defaults
        return {"height_cm": 130, "width_cm": 82, "length_cm": 15, "weight_kg": 16.0,
                "confidence": "default", "note": "TV sin tamaño — promedio 50\""}

    # ── No-TV: usar Claude para estimación rápida ─────────────────────────────
    try:
        product_desc = " ".join(filter(None, [brand, model, title])).strip() or title
        prompt = (
            f"Producto: {product_desc}\nCategoría: {category}\n\n"
            "Estima las dimensiones del PAQUETE/CAJA DE ENVÍO de este producto (incluyendo embalaje).\n"
            "Responde SOLO con JSON válido sin markdown:\n"
            '{"height_cm": X, "width_cm": X, "length_cm": X, "weight_kg": X, "note": "breve explicación"}'
        )
        raw = await claude_client.generate(prompt, max_tokens=150)
        import json as _json2, re as _re2
        raw = _re2.sub(r'```[a-z]*\n?', '', raw.strip()).strip('`').strip()
        data = _json2.loads(raw)
        data["confidence"] = "estimated"
        return data
    except Exception as e:
        logger.warning(f"estimate-dimensions Claude fallback failed: {e}")
        return {"height_cm": None, "width_cm": None, "length_cm": None, "weight_kg": None,
                "confidence": "unknown", "note": "No se pudo estimar"}


@router.post("/search-upc")
async def search_upc_endpoint(request: Request):
    """Busca el UPC/GTIN de un producto por marca + modelo + título usando Open UPC API."""
    body  = await request.json()
    brand = (body.get("brand") or "").strip()
    model = (body.get("model") or "").strip()
    title = (body.get("title") or body.get("product_title") or "").strip()

    query = " ".join(filter(None, [brand, model])).strip() or title
    if not query:
        return JSONResponse({"error": "brand/model requeridos"}, status_code=400)

    try:
        # Open UPC ItemDB — free tier, no key required, returns GTIN/EAN/UPC
        search_url = f"https://api.upcitemdb.com/prod/trial/search?s={query}&type=product&match_mode=0"
        async with httpx.AsyncClient(timeout=15.0) as cl:
            resp = await cl.get(search_url, headers={"User-Agent": "Mozilla/5.0"})

        if resp.status_code == 200:
            data  = resp.json()
            items = data.get("items") or []
            for item in items:
                ean = (item.get("ean") or "").strip()
                upc = (item.get("upc") or "").strip()
                gtin = ean or upc
                if gtin and len(gtin) >= 12:
                    logger.info(f"UPC encontrado para '{query}': {gtin}")
                    return {"upc": gtin, "source": "upcitemdb", "title": item.get("title", "")}

        logger.info(f"UPC no encontrado para '{query}' (status {resp.status_code})")
        return {"upc": None, "source": None}

    except Exception as e:
        logger.warning(f"search-upc error: {e}")
        return {"upc": None, "source": None}


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


@router.post("/upload-clip/{item_id}")
async def upload_clip_endpoint(item_id: str, request: Request):
    """Sube un video comercial como Clip de ML al item indicado.
    Body: {"video_id": "uuid-from-video-cache"}  ← usa el video_url generado
    Endpoint ML: POST /marketplace/items/{item_id}/clips/upload (multipart)
    Requisitos: 10-60s, formato MP4/MOV, máx 280MB, orientación vertical.
    """
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    body = await request.json()
    video_id = body.get("video_id", "").strip()
    if not video_id:
        return JSONResponse({"error": "video_id requerido"}, status_code=400)

    video_bytes = _load_video(video_id)
    if not video_bytes:
        return JSONResponse({"error": "video_id no encontrado — regenera el video"}, status_code=404)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    try:
        result = await client.post(
            f"/marketplace/items/{item_id}/clips/upload",
            files={"file": ("commercial.mp4", video_bytes, "video/mp4")},
        )
        clip_uuid = result.get("clip_uuid") or result.get("id") or result.get("uuid")
        status    = result.get("status", "")
        logger.info(f"Clip upload {item_id}: clip_uuid={clip_uuid} status={status}")
        return {"ok": True, "clip_uuid": clip_uuid, "status": status, "raw": result}
    except Exception as e:
        logger.error(f"upload-clip error: {e}")
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


@router.get("/catalog-product/{product_id}")
async def catalog_product_endpoint(product_id: str):
    """Obtiene detalles de un producto del catálogo ML — incluye FAMILY_NAME allowed_values."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)
    try:
        raw = await client.get(f"/products/{product_id}")
        if not isinstance(raw, dict):
            return JSONResponse({"error": "not_found"}, status_code=404)
        # Extract FAMILY_NAME allowed values if present
        fn_values = []
        for attr in (raw.get("attributes") or []):
            if isinstance(attr, dict) and attr.get("id") == "FAMILY_NAME":
                fn_values = [
                    {"id": v.get("id"), "name": v.get("name")}
                    for v in (attr.get("allowed_values") or [])
                    if isinstance(v, dict) and v.get("id")
                ]
                break
        return {
            "id":         raw.get("id"),
            "name":       raw.get("name"),
            "status":     raw.get("status"),
            "attributes": {
                a["id"]: a.get("value_name") or (a.get("values") or [{}])[0].get("name", "")
                for a in (raw.get("attributes") or [])
                if isinstance(a, dict) and a.get("id")
            },
            "family_name_values": fn_values,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.get("/catalog-search")
async def catalog_search_endpoint(q: str = "", category: str = ""):
    """Busca productos en el catálogo de ML — retorna catalog_product_id."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)
    try:
        params: dict = {"site_id": "MLM", "q": q, "limit": 10}
        if category:
            params["category"] = category
        raw = await client.get("/products/search", params=params)
        results = raw.get("results") or [] if isinstance(raw, dict) else []
        simplified = [
            {
                "id":         r.get("id"),
                "name":       r.get("name"),
                "status":     r.get("status"),
                "attributes": {
                    a["id"]: a.get("value_name") or a.get("values", [{}])[0].get("name", "")
                    for a in (r.get("attributes") or [])
                    if isinstance(a, dict) and a.get("id")
                } if r.get("attributes") else {},
            }
            for r in results
            if isinstance(r, dict)
        ]
        return {"results": simplified, "total": len(simplified)}
    except Exception as e:
        logger.warning(f"catalog-search error: {e}")
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
    category_id        = body.get("category_id", "").strip()
    title              = body.get("title", "").strip()
    price              = body.get("price", 0)
    catalog_product_id = body.get("catalog_product_id", "").strip()
    family_name_body   = body.get("family_name", "").strip()
    # video_id opcional — si presente, se asocia al listing creado
    video_id_to_link   = body.get("video_id", "").strip()
    if not price:
        return JSONResponse({"error": "price es requerido"}, status_code=400)
    if not category_id:
        return JSONResponse({"error": "category_id es requerido"}, status_code=400)

    description = body.get("description", "")
    sku         = body.get("sku", "")
    brand_body  = body.get("brand", "").strip()
    model_body  = body.get("model", "").strip()
    pictures    = body.get("pictures", [])

    warranty_type = body.get("warranty_type", "")
    warranty_time = body.get("warranty_time", "")

    # ── Catalog offer (oferta de catálogo) ───────────────────────────────────
    if catalog_product_id:
        logger.info(f"Creating CATALOG OFFER for {catalog_product_id}")
        item_payload: dict = {
            "catalog_product_id": catalog_product_id,
            "price":              float(price),
            "currency_id":        "MXN",
            "available_quantity": int(body.get("available_quantity", 1)),
            "listing_type_id":    body.get("listing_type_id", "gold_special"),
            "condition":          body.get("condition", "new"),
            "buying_mode":        "buy_it_now",
        }
        if sku:
            item_payload["seller_custom_field"] = sku
        if warranty_type and warranty_time:
            item_payload["sale_terms"] = [
                {"id": "WARRANTY_TYPE", "value_name": warranty_type},
                {"id": "WARRANTY_TIME", "value_name": warranty_time},
            ]
    else:
        # ── Standard / User Products listing ─────────────────────────────────
        item_payload = {
            "category_id":        category_id,
            "price":              float(price),
            "currency_id":        "MXN",
            "available_quantity": int(body.get("available_quantity", 1)),
            "listing_type_id":    body.get("listing_type_id", "gold_special"),
            "condition":          body.get("condition", "new"),
            "buying_mode":        "buy_it_now",
        }
        if title:
            item_payload["title"] = title
        if pictures:
            item_payload["pictures"] = [{"id": p} if isinstance(p, str) else p for p in pictures]
        if sku:
            item_payload["seller_custom_field"] = sku
        if warranty_type and warranty_time:
            item_payload["sale_terms"] = [
                {"id": "WARRANTY_TYPE", "value_name": warranty_type},
                {"id": "WARRANTY_TIME", "value_name": warranty_time},
            ]
    # Merge attributes from body
    attrs = list(body.get("attributes") or [])
    # FAMILY_NAME va como campo raíz (item_payload["family_name"]) — quitarlo de attrs
    # para evitar duplicado que causa validation_error en ML
    attrs = [a for a in attrs if a.get("id") != "FAMILY_NAME"]

    # Ensure SELLER_SKU is always in attributes (visible como "Código de identificación" en ML)
    if sku and not catalog_product_id:
        has_seller_sku = any(a.get("id") == "SELLER_SKU" for a in attrs)
        if not has_seller_sku:
            attrs.append({"id": "SELLER_SKU", "value_name": sku})
    if not catalog_product_id and attrs:
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
            _body = getattr(exc, "body", {}) or {}
            # Construir error string legible desde el cuerpo ML (str(exc) puede quedar vacío)
            _err_str = (
                str(_body) if _body else (str(exc) or "meli_error")
            )
            return {"_meli_error": _err_str, "_meli_body": _body}

    # ── family_name (User Products API) — campo raíz, no un atributo ─────────
    family_name = family_name_body
    if not family_name and not catalog_product_id:
        # Usar el título IA como family_name — evita que ML muestre brand+model como nombre del listing
        family_name = title[:60] if title else ""
    if not family_name:
        family_name = title[:60]
    # NO incluir family_name en Intento 1 — permite que ML use nuestro título personalizado
    # (sin family_name → ML crea listing estándar con título del vendedor)
    logger.info(f"family_name preparado: {family_name!r} (se agrega solo si ML lo requiere)")

    logger.info(f"ML payload keys: {list(item_payload.keys())}")
    logger.info(f"ML attrs: {[a.get('id') for a in attrs]}")

    try:
        import copy as _copy

        # Intento 1: SIN family_name — ML crea listing estándar con nuestro título personalizado
        result = await _post_item(item_payload)
        logger.info(f"ML intento 1 (sin family_name): {'ok' if not result.get('_meli_error') else result['_meli_error'][:120]}")

        # Intento 2: family_name requerido — para categorías tipo catálogo (ej. MLM1002 Televisores)
        # ML usa family_name como TÍTULO del listing; el campo "title" es inválido cuando family_name está presente.
        # Estrategia: usar el título del wizard como family_name (se convierte en el título en ML).
        # ML normaliza capitalización (ej. "Sony TV" → "Sony Tv") pero preserva el contenido.
        if result.get("_meli_error"):
            err_lower = result["_meli_error"].lower()
            fn_required = "family_name" in err_lower
            if fn_required:
                import re as _re_fn
                # Limpiar modelo de caracteres especiales para extraer prefijo
                _model_clean = _re_fn.sub(r'[^A-Za-z0-9]', '', model_body or "")
                _mp = _re_fn.match(r'^([A-Za-z]+\d+)', _model_clean)
                _model_prefix = _mp.group(1).upper()[:8] if _mp else ""
                _fn_candidates = [c.strip() for c in [
                    (title or "")[:60],                                          # 1º: título wizard → se convierte en título ML
                    model_body,                                                  # "K-50S20M2" / "WR43QE2350"
                    _model_clean,                                                # "K50S20M2" (sin guiones)
                    _model_prefix,                                               # "K50" / "WR43"
                    brand_body,                                                  # "Sony" / "Westinghouse"
                    ((brand_body or "") + " " + _model_prefix).strip()[:30],   # "Sony K50"
                    family_name,                                                 # frontend: "BRAVIA 2", "ULED", etc.
                ] if c and c.strip() and len(c.strip()) >= 2]
                # Deduplicar preservando orden
                _seen_fn: set = set()
                _fn_candidates = [x for x in _fn_candidates if x not in _seen_fn and not _seen_fn.add(x)]  # type: ignore
                logger.info(f"ML intento 2: family_name candidatos (1º=wizard title): {_fn_candidates}")
                for _fn_cand in _fn_candidates:
                    _p2 = _copy.deepcopy(item_payload)
                    _p2["family_name"] = _fn_cand
                    # CRÍTICO: title es campo inválido cuando family_name está presente en categorías catálogo
                    # family_name SE CONVIERTE EN el título del listing en ML
                    _p2.pop("title", None)
                    result = await _post_item(_p2)
                    logger.info(f"ML intento 2 family_name={_fn_cand!r}: {'ok' if not result.get('_meli_error') else result['_meli_error'][:80]}")
                    if not result.get("_meli_error"):
                        break  # éxito — salir del loop

        # Intento 3: title inválido en categoría catálogo → family_name + sin title
        if result.get("_meli_error"):
            err_lower = result["_meli_error"].lower()
            title_invalid = "title" in err_lower and any(w in err_lower for w in ("invalid", "not valid", "not allowed"))
            if title_invalid:
                payload_fn_notitle = _copy.deepcopy(item_payload)
                payload_fn_notitle["family_name"] = family_name
                payload_fn_notitle.pop("title", None)
                logger.warning("ML intento 3: title inválido + family_name (ML User Products)")
                result = await _post_item(payload_fn_notitle)
                logger.info(f"ML intento 3: {'ok' if not result.get('_meli_error') else result['_meli_error'][:120]}")

        # Intento 4: family_name no aceptado → quitar family_name, mantener title
        if result.get("_meli_error"):
            err_lower = result["_meli_error"].lower()
            fn_invalid = "family_name" in err_lower and any(w in err_lower for w in ("not_allowed", "invalid", "not allowed", "unexpected"))
            if fn_invalid:
                payload_no_fn = _copy.deepcopy(item_payload)
                payload_no_fn.pop("family_name", None)
                logger.warning("ML intento 4: family_name no permitido, quitando")
                result = await _post_item(payload_no_fn)
                logger.info(f"ML intento 4: {'ok' if not result.get('_meli_error') else result['_meli_error'][:120]}")

        # Intento 5: título muy corto → enriquecer con marca + tipo de producto hasta 55 chars
        if result.get("_meli_error"):
            err_lower = result["_meli_error"].lower()
            if "minimum_length" in err_lower or ("title" in err_lower and "minimum" in err_lower):
                # Construir título enriquecido asegurando mínimo 35 chars
                _parts = [p for p in [brand_body, title, model_body] if p and p.lower() not in (title.lower() if title else "")]
                enriched_title = title
                for _p in _parts:
                    candidate = (enriched_title + " " + _p).strip()[:60]
                    if len(candidate) > len(enriched_title):
                        enriched_title = candidate
                if len(enriched_title) < 25 and brand_body:
                    enriched_title = (brand_body + " " + enriched_title).strip()[:60]
                payload_enriched = _copy.deepcopy(item_payload)
                payload_enriched["title"] = enriched_title
                logger.warning(f"ML intento 5: título corto, enriquecido: {enriched_title!r}")
                result = await _post_item(payload_enriched)
                logger.info(f"ML intento 5: {'ok' if not result.get('_meli_error') else result['_meli_error'][:120]}")

        if result.get("_meli_error"):
            return JSONResponse({"error": result["_meli_error"]}, status_code=400)

        item_id = result.get("id")
        if not item_id:
            err = result.get("message") or result.get("error") or str(result)
            return JSONResponse({"error": err}, status_code=400)

        # Título real en ML — cuando se usó family_name, ML devuelve el título normalizado en el POST
        # No intentar PUT /title porque ML lo rechaza ("cannot modify title if item has family_name")
        ml_actual_title = result.get("title") or title
        if ml_actual_title:
            logger.info(f"Título en ML para {item_id}: {ml_actual_title!r} (wizard: {title!r})")

        # Add description separately (ML doesn't accept it in the initial POST)
        if description:
            try:
                await client.post(
                    f"/items/{item_id}/description",
                    json={"plain_text": description},
                )
            except Exception as e:
                logger.warning(f"Description upload failed for {item_id}: {e}")

        # Mark gap as launched — save all published listing data
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """UPDATE bm_sku_gaps SET
                   status='launched',
                   ml_item_id=?, ml_title=?, ml_price=?,
                   ml_category_id=?, ml_permalink=?, ml_condition=?,
                   launched_at=CURRENT_TIMESTAMP
                   WHERE user_id=? AND sku=?""",
                (
                    item_id,
                    ml_actual_title,
                    float(price),
                    category_id,
                    result.get("permalink", ""),
                    result.get("condition", "new"),
                    user_id,
                    sku.upper(),
                ),
            )
            await db.commit()

        clip_status = None
        # Si había un video generado: guardar en DB + subir a ML como Clip
        if video_id_to_link:
            video_bytes = _load_video(video_id_to_link)
            if video_bytes:
                try:
                    from app.services.token_store import save_product_video
                    await save_product_video(item_id, user_id, sku, video_id_to_link)
                    logger.info(f"Video {video_id_to_link} asociado a {item_id} en DB")
                except Exception as ve:
                    logger.warning(f"No se pudo asociar video en DB: {ve}")
                # Subir clip a ML
                try:
                    clip_result = await client.post(
                        f"/marketplace/items/{item_id}/clips/upload",
                        files={"file": ("commercial.mp4", video_bytes, "video/mp4")},
                    )
                    clip_status = clip_result.get("status", "sent")
                    logger.info(f"Clip auto-uploaded para {item_id}: {clip_status}")
                except Exception as ce:
                    clip_status = f"error: {ce}"
                    logger.warning(f"Clip upload fallido para {item_id}: {ce}")

        logger.info(f"Listing created: {item_id} for SKU {sku} ({user_id})")
        # title_warning solo si ML aplicó un título COMPLETAMENTE diferente al wizard
        # (no por normalización de capitalización — ML siempre convierte a Title Case)
        title_warning = None
        if ml_actual_title and title and ml_actual_title.lower() != title.lower():
            title_warning = f"ML aplicó el título: \"{ml_actual_title}\" (en lugar de tu título)"
        return {
            "ok": True,
            "item_id": item_id,
            "permalink": result.get("permalink", ""),
            "title": title,
            "ml_actual_title": ml_actual_title,
            "title_warning": title_warning,
            "price": float(price),
            "status": result.get("status", ""),
            "clip_status": clip_status,
        }

    except Exception as e:
        logger.error(f"create-listing error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.post("/modify-listing")
async def modify_listing(request: Request):
    """PATCH a launched listing: title, price, and/or available stock."""
    from app.services.meli_client import MeliClient, _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    body = await request.json()
    item_id = (body.get("item_id") or "").strip()
    title   = (body.get("title")   or "").strip()
    price   = body.get("price")
    stock   = body.get("stock")
    sku     = (body.get("sku")     or "").strip().upper()

    if not item_id:
        return JSONResponse({"error": "item_id requerido"}, status_code=400)

    client = MeliClient(user_id)
    try:
        patch: dict = {}
        if title:
            patch["title"] = title
        if price is not None and float(price) > 0:
            patch["price"] = float(price)
        if stock is not None:
            patch["available_quantity"] = max(0, int(stock))

        if patch:
            resp = await client.put(f"/items/{item_id}", json=patch)
            if resp.get("error") or resp.get("_meli_error"):
                err = resp.get("message") or resp.get("error") or resp.get("_meli_error") or str(resp)
                return JSONResponse({"error": err}, status_code=400)

        # Update DB with new values
        if sku:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                fields, vals = [], []
                if title:
                    fields.append("ml_title=?"); vals.append(title)
                if price is not None and float(price) > 0:
                    fields.append("ml_price=?"); vals.append(float(price))
                if fields:
                    vals.extend([user_id, sku])
                    await db.execute(
                        f"UPDATE bm_sku_gaps SET {', '.join(fields)} WHERE user_id=? AND sku=?",
                        vals,
                    )
                    await db.commit()

        return {"ok": True, "item_id": item_id}

    except Exception as e:
        logger.error(f"modify-listing error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@router.post("/register-launched")
async def register_launched(request: Request):
    """Registra un SKU como lanzado en ML usando un Item ID existente.
    Útil para publicaciones que existían antes del sistema de tracking.
    Obtiene título, precio y permalink desde la API de ML automáticamente.
    """
    from app.services.meli_client import MeliClient, _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    body    = await request.json()
    sku     = (body.get("sku")     or "").strip().upper()
    item_id = (body.get("item_id") or "").strip().upper()

    if not sku or not item_id:
        return JSONResponse({"error": "sku e item_id requeridos"}, status_code=400)

    client = MeliClient(user_id)
    try:
        # Fetch item data from ML
        item = await client.get(f"/items/{item_id}")
        if item.get("error") or item.get("_meli_error"):
            err = item.get("message") or item.get("error") or item.get("_meli_error") or "Item no encontrado"
            return JSONResponse({"error": err}, status_code=400)

        ml_title     = item.get("title", "")
        ml_price     = float(item.get("price") or 0)
        ml_category  = item.get("category_id", "")
        ml_permalink = item.get("permalink", "")
        ml_condition = item.get("condition", "new")

        async with aiosqlite.connect(DATABASE_PATH) as db:
            # Update if row exists, insert if not
            await db.execute(
                """UPDATE bm_sku_gaps SET
                   status='launched',
                   ml_item_id=?, ml_title=?, ml_price=?,
                   ml_category_id=?, ml_permalink=?, ml_condition=?,
                   launched_at=CURRENT_TIMESTAMP
                   WHERE user_id=? AND sku=?""",
                (item_id, ml_title, ml_price, ml_category, ml_permalink, ml_condition,
                 user_id, sku),
            )
            rows_updated = db.total_changes
            await db.commit()

        if rows_updated == 0:
            return JSONResponse({"error": f"SKU {sku} no encontrado en la base de datos. Ejecuta un escaneo primero."}, status_code=404)

        logger.info(f"register-launched: {sku} → {item_id} ({user_id})")
        return {"ok": True, "item_id": item_id, "title": ml_title, "price": ml_price, "permalink": ml_permalink}

    except Exception as e:
        logger.error(f"register-launched error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()
