"""
Lanzador Inteligente
====================
Scanner nocturno que compara inventario BinManager vs publicaciones activas en MeLi.
Identifica SKUs con stock disponible que no están publicados en cada cuenta,
y presenta los gaps con pricing, datos del producto e IA para generar listings.
"""
import asyncio
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


async def _get_meli_sku_set(user_id: str, nickname: str) -> set[str]:
    """Return set of base SKUs active in a MeLi account.
    Uses item_sku_cache to avoid re-fetching already-known SKUs.
    """
    from app.services.meli_client import get_meli_client
    import re as _re

    _ALL_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")

    def _base(sku: str) -> str:
        u = sku.upper()
        for s in _ALL_SUFFIXES:
            if u.endswith(s):
                return u[:-len(s)]
        return u

    # Use user_id directly — context var pattern is unreliable in background tasks
    client = await get_meli_client(user_id=user_id)
    if not client:
        return set()

    try:
        # 1. Fetch all item IDs (active + paused to be thorough about what's listed)
        item_ids = []
        offset = 0
        while True:
            try:
                resp = await client.get(
                    f"/users/{user_id}/items/search",
                    params={"limit": 100, "offset": offset},
                )
                ids = resp.get("results", [])
                item_ids.extend(ids)
                paging = resp.get("paging", {})
                if len(ids) < 100 or offset + 100 >= paging.get("total", 0):
                    break
                offset += 100
                if offset > 8000:
                    break
            except Exception as e:
                logger.warning(f"items/search error {nickname} offset={offset}: {e}")
                break

        if not item_ids:
            return set()

        # 2. Check cache first — skip items we already know the SKU for
        from app.services.token_store import get_cached_skus, save_skus_cache
        cached = await get_cached_skus(item_ids)
        cached_skus = {_base(v) for v in cached.values() if v}
        needs_fetch = [iid for iid in item_ids if iid not in cached]

        # 3. Batch fetch only uncached items — include attributes & variations
        new_entries = []
        sku_set = set(cached_skus)
        for i in range(0, len(needs_fetch), 20):
            batch = needs_fetch[i:i+20]
            try:
                details = await client.get(
                    f"/items?ids={','.join(batch)}"
                    f"&attributes=id,seller_custom_field,attributes,variations"
                )
                if isinstance(details, list):
                    for entry in details:
                        body = entry.get("body", {}) if isinstance(entry, dict) else {}
                        iid  = str(body.get("id", ""))
                        # seller_custom_field
                        scf = (body.get("seller_custom_field") or "").upper()
                        if scf:
                            scf = _re.split(r'\s*[/+]\s*', scf)[0].strip()
                            sku_set.add(_base(scf))
                            if iid:
                                new_entries.append({"item_id": iid, "user_id": user_id, "sku": scf})
                        # attributes SELLER_SKU
                        for a in (body.get("attributes") or []):
                            if a.get("id") == "SELLER_SKU":
                                v = (a.get("value_name") or "").upper()
                                if v:
                                    sku_set.add(_base(v))
                        # variations
                        for var in (body.get("variations") or []):
                            vscf = (var.get("seller_custom_field") or "").upper()
                            if vscf:
                                sku_set.add(_base(vscf))
                            for a in (var.get("attributes") or []):
                                if a.get("id") == "SELLER_SKU":
                                    v = (a.get("value_name") or "").upper()
                                    if v:
                                        sku_set.add(_base(v))
            except Exception as e:
                logger.warning(f"Batch SKU fetch error {nickname}: {e}")

        # 4. Persist new SKUs to cache for future scans
        if new_entries:
            try:
                await save_skus_cache(new_entries)
            except Exception:
                pass

        return sku_set
    finally:
        await client.close()


def _priority_score(stock_total: int, retail_usd: float, cost_usd: float) -> int:
    """Score 0–100: más stock y mayor margen = mayor prioridad."""
    stock_score = min(stock_total / 5, 40)   # max 40 pts (200+ units)
    price_score  = min(retail_usd / 25, 30)  # max 30 pts ($750+ USD)
    margin = (retail_usd - cost_usd) / retail_usd if retail_usd > 0 else 0
    margin_score = min(margin * 100, 30)     # max 30 pts (100% margin)
    return int(stock_score + price_score + margin_score)


async def _run_gap_scan():
    """Core scan: compare BM inventory vs MeLi per account, store gaps."""
    if _scan_lock.locked():
        return
    async with _scan_lock:
        logger.info("BM gap scan iniciando...")
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE bm_gap_scan_status SET status='running', started_at=?, finished_at=NULL, error=NULL WHERE id=1",
                (datetime.utcnow().isoformat(),)
            )
            await db.commit()

        try:
            # 1. Get all MeLi accounts
            accounts = await token_store.get_all_tokens()
            if not accounts:
                raise Exception("No MeLi accounts configured")

            # 2a. Get real USD→MXN FX rate from first MeLi account
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
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as bm_http:
                bm_logged_in = await _bm_login(bm_http)
                if not bm_logged_in:
                    raise Exception("BinManager login failed — verifica BM_USER/BM_PASS")
                bm_products = await _bm_fetch_all_skus_with_stock(bm_http)

            logger.info(f"BM gap scan: {len(bm_products)} SKUs con stock en BM")

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

            # 3. For each account, find gaps
            total_gaps = 0
            now_iso = datetime.utcnow().isoformat()

            for account in accounts:
                user_id = account["user_id"]
                nickname = account.get("nickname") or user_id

                logger.info(f"Checking gaps for {nickname} ({user_id})...")
                meli_skus = await _get_meli_sku_set(user_id, nickname)
                logger.info(f"  {nickname}: {len(meli_skus)} SKUs activos en MeLi")

                gaps = []
                for base_sku, prod in bm_map.items():
                    if base_sku in meli_skus:
                        continue
                    retail = float(prod.get("RetailPrice", 0) or prod.get("LastRetailPricePurchaseHistory", 0) or 0)
                    cost   = float(prod.get("AvgCostQTY", 0) or 0)
                    stock  = _bm_qty(prod)
                    score  = _priority_score(stock, retail, cost)
                    suggested = round(retail * fx * 1.3, 0) if retail > 0 else 0
                    cost_mxn  = round(cost * fx, 0) if cost > 0 else 0
                    gaps.append({
                        "user_id": user_id,
                        "nickname": nickname,
                        "sku": base_sku,
                        "product_title": prod.get("Title", "") or "",
                        "brand": prod.get("Brand", "") or "",
                        "model": prod.get("Model", "") or "",
                        "image_url": prod.get("ImageURL", "") or "",
                        "category": prod.get("CategoryName", "") or "",
                        "stock_total": stock,
                        "stock_mty": 0,   # will batch later if needed
                        "stock_cdmx": 0,
                        "retail_price_usd": retail,
                        "cost_usd": cost,
                        "priority_score": score,
                        "suggested_price_mxn": suggested,
                        "cost_price_mxn": cost_mxn,
                        "last_scan": now_iso,
                    })

                logger.info(f"  {nickname}: {len(gaps)} gaps encontrados")
                total_gaps += len(gaps)

                # Upsert gaps into DB (preserve status if already exists)
                async with aiosqlite.connect(DATABASE_PATH) as db:
                    for g in gaps:
                        await db.execute("""
                            INSERT INTO bm_sku_gaps
                                (user_id, nickname, sku, product_title, brand, model,
                                 image_url, category, stock_total, stock_mty, stock_cdmx,
                                 retail_price_usd, cost_usd, priority_score,
                                 suggested_price_mxn, cost_price_mxn, last_scan)
                            VALUES
                                (:user_id,:nickname,:sku,:product_title,:brand,:model,
                                 :image_url,:category,:stock_total,:stock_mty,:stock_cdmx,
                                 :retail_price_usd,:cost_usd,:priority_score,
                                 :suggested_price_mxn,:cost_price_mxn,:last_scan)
                            ON CONFLICT(user_id, sku) DO UPDATE SET
                                nickname=excluded.nickname,
                                product_title=excluded.product_title,
                                brand=excluded.brand,
                                model=excluded.model,
                                image_url=excluded.image_url,
                                category=excluded.category,
                                stock_total=excluded.stock_total,
                                stock_mty=excluded.stock_mty,
                                stock_cdmx=excluded.stock_cdmx,
                                retail_price_usd=excluded.retail_price_usd,
                                cost_usd=excluded.cost_usd,
                                priority_score=excluded.priority_score,
                                suggested_price_mxn=excluded.suggested_price_mxn,
                                cost_price_mxn=excluded.cost_price_mxn,
                                last_scan=excluded.last_scan
                            WHERE bm_sku_gaps.status != 'ignored'
                        """, g)
                    # Remove SKUs that are now launched (in meli_skus) but still in DB as unlaunched
                    if meli_skus:
                        await db.execute(
                            """DELETE FROM bm_sku_gaps
                               WHERE user_id=? AND status='unlaunched'
                               AND sku IN ({})""".format(",".join("?" * len(meli_skus))),
                            [user_id] + list(meli_skus)
                        )
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
            logger.info(f"BM gap scan completado: {total_gaps} gaps en {len(accounts)} cuentas")

        except BaseException as e:
            tb = traceback.format_exc()
            err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.error(f"BM gap scan error: {tb}")
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

@router.get("/gaps")
async def get_gaps(
    request: Request,
    status: str = Query("unlaunched"),
    sort: str = Query("priority"),
    limit: int = Query(100),
    offset: int = Query(0),
):
    """Lista SKUs no lanzados para la cuenta activa."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()
    if not user_id:
        return JSONResponse({"error": "no_account"}, status_code=401)

    order = "priority_score DESC" if sort == "priority" else "product_title ASC"
    status_filter = status if status in ("unlaunched", "ignored", "all") else "unlaunched"

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status_filter == "all":
            cursor = await db.execute(
                f"SELECT * FROM bm_sku_gaps WHERE user_id=? ORDER BY {order} LIMIT ? OFFSET ?",
                (user_id, limit, offset)
            )
        else:
            cursor = await db.execute(
                f"SELECT * FROM bm_sku_gaps WHERE user_id=? AND status=? ORDER BY {order} LIMIT ? OFFSET ?",
                (user_id, status_filter, limit, offset)
            )
        rows = await cursor.fetchall()
        total_cursor = await db.execute(
            "SELECT COUNT(*) FROM bm_sku_gaps WHERE user_id=? AND status=?",
            (user_id, "unlaunched")
        )
        total = (await total_cursor.fetchone())[0]

    return {
        "user_id": user_id,
        "total": total,
        "items": [dict(r) for r in rows]
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
    """Estado del último scan."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM bm_gap_scan_status WHERE id=1")
        row = await cursor.fetchone()
    return dict(row) if row else {"status": "idle"}


@router.get("/debug-scan")
async def debug_scan():
    """Diagnóstico: muestra cuántos SKUs encontró BM, cuántos tiene ML, y los primeros gaps."""
    from app.services.meli_client import _active_user_id as _ctx
    user_id = _ctx.get()

    result: dict = {"user_id": user_id, "bm": {}, "ml": {}, "sample_gaps": [], "error": None}

    # 1. BM login + first page
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as bm_http:
            logged_in = await _bm_login(bm_http)
            result["bm"]["login"] = logged_in
            if logged_in:
                page1 = await _bm_fetch_all_skus_with_stock(bm_http)
                result["bm"]["total_skus"] = len(page1)
                # Show full first row so we can see EXACT field names BM returns
                result["bm"]["first_row_raw"] = page1[0] if page1 else {}
                result["bm"]["sample"] = [
                    {"sku": p.get("SKU"), "qty_total": p.get("QtyTotal"), "qty": p.get("QTY"), "retail": p.get("RetailPrice"),
                     "all_keys": list(p.keys())}
                    for p in page1[:3]
                ]
    except Exception as e:
        result["bm"]["error"] = str(e)

    # 2. ML SKU set for active account
    if user_id:
        try:
            meli_skus = await _get_meli_sku_set(user_id, user_id)
            result["ml"]["sku_count"] = len(meli_skus)
            result["ml"]["sample"] = list(meli_skus)[:10]
        except Exception as e:
            result["ml"]["error"] = str(e)

    # 3. DB state
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT * FROM bm_gap_scan_status WHERE id=1")
        status_row = await cursor.fetchone()
        result["scan_status"] = dict(zip([d[0] for d in cursor.description], status_row)) if status_row else {}
        cursor2 = await db.execute("SELECT COUNT(*), user_id FROM bm_sku_gaps GROUP BY user_id")
        result["gaps_in_db"] = [{"count": r[0], "user_id": r[1]} for r in await cursor2.fetchall()]

    return result


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

## TÍTULO (max 60 caracteres, incluye marca, modelo, beneficio clave)
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
