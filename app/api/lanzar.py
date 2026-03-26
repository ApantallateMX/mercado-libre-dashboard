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
    """Return set of base SKUs published in a MeLi account (active + paused).
    Uses item_sku_cache to skip re-fetching already-known items.

    KNOWN LIMITATION: ML items/search caps at ~1000 items via offset pagination.
    For accounts with many items some may be missed — use scan logs to detect.
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
        return set()

    try:
        # ── Step 1: Collect all item IDs ──────────────────────────────────────
        # items/search returns active + paused + recently closed by default.
        # ML hard-caps offset pagination at ~1000 items; for larger accounts
        # some items near the tail may be missed.
        item_ids: list[str] = []
        offset = 0
        while True:
            try:
                resp = await client.get(
                    f"/users/{user_id}/items/search",
                    params={"limit": 100, "offset": offset},
                )
                ids = resp.get("results", [])
                if not ids:
                    break
                item_ids.extend(str(i) for i in ids)
                paging = resp.get("paging", {})
                total  = paging.get("total", 0)
                if len(ids) < 100 or offset + 100 >= total:
                    break
                offset += 100
                if offset >= 10000:
                    logger.warning(f"{nickname}: items/search offset cap hit at 10k")
                    break
            except Exception as e:
                logger.warning(f"items/search error {nickname} offset={offset}: {e}")
                break

        logger.info(f"{nickname}: {len(item_ids)} item IDs from ML items/search")
        if not item_ids:
            return set()

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

        for i in range(0, len(needs_fetch), 20):
            batch = needs_fetch[i:i + 20]
            try:
                # Embed IDs directly in URL to avoid httpx percent-encoding commas
                details = await client.get(f"/items?ids={','.join(batch)}")

                items_list = details if isinstance(details, list) else []
                batch_found = 0
                for entry in items_list:
                    if not isinstance(entry, dict):
                        continue
                    body = entry.get("body") or {}
                    if not isinstance(body, dict):
                        continue
                    iid  = str(body.get("id", ""))
                    skus = _skus_from_body(body)
                    for raw_sku in skus:
                        base = _base(raw_sku)
                        sku_set.add(base)
                        if iid:
                            new_entries.append({"item_id": iid, "user_id": user_id, "sku": raw_sku})
                    if skus:
                        batch_found += 1

                if batch_found == 0 and items_list:
                    # Whole batch had no SKUs — log first item for debugging
                    sample = items_list[0].get("body", {}) if items_list else {}
                    logger.warning(
                        f"{nickname}: batch {i//20} — {len(items_list)} items, 0 with SKU. "
                        f"Sample keys: {list(sample.keys())[:10]}"
                    )
            except Exception as e:
                logger.warning(f"{nickname}: batch {i//20} fetch error: {e}")

        logger.info(f"{nickname}: {len(sku_set)} unique base SKUs detected in ML")

        # ── Step 4: Persist new mappings to cache ─────────────────────────────
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
                        "upc": prod.get("UPC", "") or prod.get("Upc", "") or "",
                        "size": prod.get("Size", "") or prod.get("ScreenSize", "") or "",
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
                                brand=excluded.brand,
                                model=excluded.model,
                                image_url=excluded.image_url,
                                category=excluded.category,
                                upc=excluded.upc,
                                size=excluded.size,
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

    return {
        "user_id":     user_id,
        "total":       total,          # filtered count
        "badge_total": badge_total,    # unfiltered count for the badge
        "page":        page,
        "per_page":    per_page,
        "pages":       max(1, -(-total // per_page)),  # ceiling division
        "items":       [dict(r) for r in rows],
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


@router.post("/generate-image")
async def generate_image_endpoint(request: Request):
    """Genera imagen de producto con FLUX Schnell via Replicate."""
    from app.services import replicate_client

    if not replicate_client.is_available():
        return JSONResponse({"error": "REPLICATE_API_KEY no configurada"}, status_code=503)

    body = await request.json()
    prompt = replicate_client.build_product_prompt(
        brand    = body.get("brand", ""),
        model    = body.get("model", ""),
        title    = body.get("title", "") or body.get("product_title", ""),
        category = body.get("category", ""),
    )

    # Permitir prompt personalizado desde el frontend
    custom_prompt = (body.get("custom_prompt") or "").strip()
    if custom_prompt:
        prompt = custom_prompt

    try:
        image_url = await replicate_client.generate_image(prompt)
        return {"image_url": image_url, "prompt": prompt}
    except Exception as e:
        logger.error(f"generate-image error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


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

    try:
        result = await client.post("/items", json=item_payload)
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
