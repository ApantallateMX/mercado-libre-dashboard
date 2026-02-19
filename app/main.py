import asyncio
import json
from types import SimpleNamespace
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from app.config import APP_PIN, MELI_USER_ID, MELI_REFRESH_TOKEN, MELI_USER_ID_2, MELI_REFRESH_TOKEN_2
from app.auth import router as auth_router
from app.api.orders import router as orders_router
from app.api.items import router as items_router
from app.api.metrics import router as metrics_router
from app.api.health import router as health_router
from app.api.sku_inventory import router as sku_inventory_router
from app.api.health_ai import router as health_ai_router
from app.services import token_store
from app.services.meli_client import get_meli_client, _active_user_id as _meli_user_id_ctx
from app import order_net_revenue

# ---------- SKU suffix helpers ----------
_GR_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC")
_IC_SUFFIXES = ("-ICB", "-ICC")
_ALL_SUFFIXES = _GR_SUFFIXES + _IC_SUFFIXES


def _extract_base_sku(sku: str) -> str:
    """Devuelve el SKU base sin sufijo de variante."""
    upper = sku.upper()
    for sfx in _ALL_SUFFIXES:
        if upper.endswith(sfx):
            return sku[:-len(sfx)]
    return sku


def _bm_conditions_for_sku(sku: str) -> str:
    """Retorna condiciones BM segun sufijo del SKU.
    SKUs publicados como ICB/ICC incluyen todo el stock (producto dañado permitido).
    SKUs normales (GR o sin sufijo) excluyen ICB/ICC — no son vendibles en listings regulares.
    """
    upper = sku.upper()
    if upper.endswith("-ICB") or upper.endswith("-ICC"):
        return "GRA,GRB,GRC,ICB,ICC,NEW"
    return "GRA,GRB,GRC,NEW"


import re as _re

def _clean_sku_for_bm(sku: str) -> str:
    """Limpia SKU de MeLi para consultar BinManager.
    Quita: (N), / segunda_parte, + segunda_parte, espacios extra, etc."""
    if not sku:
        return ""
    # Tomar primera parte antes de " / " o " + " (MeLi concatena SKUs en packs)
    s = _re.split(r'\s*[/+]\s*', sku)[0].strip()
    # Quitar sufijos entre parentesis: (18), (2), etc.
    s = _re.sub(r'\(\d+\)', '', s).strip()
    # Quitar parentesis sobrantes
    s = _re.sub(r'[()]', '', s).strip()
    return s


async def _get_usd_to_mxn(client) -> float:
    """Obtiene tipo de cambio USD->MXN de la API de MeLi."""
    try:
        fx_data = await client.get("/currency_conversions/search", params={"from": "USD", "to": "MXN"})
        return fx_data.get("ratio", 20.0)
    except Exception:
        return 20.0


def _calc_margins(products: list, usd_to_mxn: float):
    """Calcula _costo_mxn, _retail_mxn, _ganancia_est, _margen_pct para productos con BM data."""
    for p in products:
        avg_cost = p.get("_bm_avg_cost", 0) or 0
        retail = p.get("_bm_retail_price", 0) or 0
        # Flag: BM tiene registro aunque costos sean sentinel (0 o 9999)
        p["_bm_has_data"] = bool(p.get("_bm_brand") or avg_cost > 0 or retail > 0)
        p["_costo_mxn"] = round(avg_cost * usd_to_mxn, 2) if (avg_cost > 0 and avg_cost < 9999) else 0
        p["_retail_mxn"] = round(retail * usd_to_mxn, 2) if (retail > 0 and retail < 9999) else 0
        price = p.get("price", 0)
        if price > 0 and p["_costo_mxn"] > 0:
            comision = price * 0.17
            iva_comision = comision * 0.16
            envio = 150
            ganancia = price - p["_costo_mxn"] - comision - iva_comision - envio
            p["_ganancia_est"] = round(ganancia, 2)
            p["_margen_pct"] = round((ganancia / price) * 100, 1)
        else:
            p["_ganancia_est"] = None
            p["_margen_pct"] = None


async def _seed_one(user_id: str, refresh_token: str, label: str):
    """Intenta recuperar tokens para una cuenta via refresh_token."""
    import httpx
    from app.config import MELI_TOKEN_URL, MELI_CLIENT_ID, MELI_CLIENT_SECRET
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(MELI_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": MELI_CLIENT_ID,
                "client_secret": MELI_CLIENT_SECRET,
                "refresh_token": refresh_token,
            })
            if resp.status_code == 200:
                data = resp.json()
                await token_store.save_tokens(
                    user_id,
                    data["access_token"],
                    data["refresh_token"],
                    data.get("expires_in", 21600),
                )
                print(f"[SEED] Tokens recovered for {label} (user {user_id})")
            else:
                print(f"[SEED] Token refresh failed for {label}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[SEED] Error recovering tokens for {label}: {e}")


async def _seed_tokens():
    """Auto-recover MeLi tokens from env vars after Railway deploy."""
    # Cuenta principal
    if MELI_REFRESH_TOKEN and MELI_USER_ID:
        existing = await token_store.get_tokens(MELI_USER_ID)
        if not existing:
            await _seed_one(MELI_USER_ID, MELI_REFRESH_TOKEN, "cuenta1")
    # Cuenta 2
    if MELI_REFRESH_TOKEN_2 and MELI_USER_ID_2:
        existing2 = await token_store.get_tokens(MELI_USER_ID_2)
        if not existing2:
            await _seed_one(MELI_USER_ID_2, MELI_REFRESH_TOKEN_2, "cuenta2")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al iniciar."""
    await token_store.init_db()
    await _seed_tokens()
    yield


app = FastAPI(title="Mercado Libre Dashboard", lifespan=lifespan)

# Static files y templates
BASE_PATH = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_PATH / "static"), name="static")
templates = Jinja2Templates(directory=BASE_PATH / "templates")

# ---------- PIN access middleware ----------
_PIN_EXEMPT = ("/pin", "/pin/verify", "/static", "/favicon.ico", "/auth/")


class PinMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(ex) for ex in _PIN_EXEMPT):
            return await call_next(request)
        if request.cookies.get("pin_ok") != "1":
            from urllib.parse import quote
            next_url = quote(str(request.url.path), safe="")
            return RedirectResponse(f"/pin?next={next_url}", status_code=302)
        return await call_next(request)


class AccountMiddleware(BaseHTTPMiddleware):
    """Setea el ContextVar de cuenta activa basado en la cookie active_account_id."""
    async def dispatch(self, request: Request, call_next):
        cookie_uid = request.cookies.get("active_account_id")
        if cookie_uid:
            tokens = await token_store.get_tokens(cookie_uid)
            if tokens:
                token = _meli_user_id_ctx.set(cookie_uid)
                try:
                    return await call_next(request)
                finally:
                    _meli_user_id_ctx.reset(token)
        return await call_next(request)


app.add_middleware(PinMiddleware)
app.add_middleware(AccountMiddleware)


# ---------- PIN routes ----------
@app.get("/pin", response_class=HTMLResponse)
async def pin_page(request: Request, error: str = "", next: str = "/dashboard"):
    return templates.TemplateResponse("pin.html", {
        "request": request,
        "error": error,
        "next": next,
    })


@app.post("/pin/verify")
async def pin_verify(request: Request):
    form = await request.form()
    pin = form.get("pin", "")
    next_url = form.get("next", "/dashboard")
    if pin == APP_PIN:
        response = RedirectResponse(next_url, status_code=302)
        response.set_cookie("pin_ok", "1", max_age=2592000, httponly=True, samesite="lax")
        # Pre-warm caches en background para que tabs carguen rapido
        global _prewarm_task
        if _prewarm_task is None or _prewarm_task.done():
            _prewarm_task = asyncio.create_task(_prewarm_caches())
        return response
    from urllib.parse import quote
    return RedirectResponse(f"/pin?error=1&next={quote(next_url, safe='')}", status_code=302)


# Routers
app.include_router(auth_router)
app.include_router(orders_router)
app.include_router(items_router)
app.include_router(metrics_router)
app.include_router(health_router)
app.include_router(sku_inventory_router)
app.include_router(health_ai_router)


# ---------- Account switcher ----------

@app.post("/auth/switch-account")
async def switch_account(request: Request):
    """Cambia la cuenta activa y setea la cookie active_account_id."""
    form = await request.form()
    uid = form.get("user_id", "")
    if uid:
        tokens = await token_store.get_tokens(uid)
        if tokens:
            referer = request.headers.get("referer", "/dashboard")
            response = RedirectResponse(referer, status_code=303)
            response.set_cookie("active_account_id", uid, max_age=2592000, httponly=True, samesite="lax")
            return response
    return RedirectResponse("/dashboard", status_code=303)


async def _accounts_ctx(request: Request) -> dict:
    """Contexto común de cuentas para templates de página."""
    accounts = await token_store.get_all_tokens()
    active_uid = request.cookies.get("active_account_id")
    # Si la cookie apunta a una cuenta inexistente, usar la primera disponible
    if active_uid and not any(a["user_id"] == active_uid for a in accounts):
        active_uid = None
    if not active_uid and accounts:
        active_uid = accounts[0]["user_id"]
    return {"accounts": accounts, "active_user_id": active_uid}


async def _enrich_with_sale_prices(client, products: list, id_key: str = "id", price_key: str = "price"):
    """Enriquece lista de productos con datos de /sale_price.
    Si sale_price muestra descuento (regular_amount > amount),
    actualiza original_price con regular_amount."""
    sem = asyncio.Semaphore(10)

    async def _fetch_sp(item_id: str):
        async with sem:
            return item_id, await client.get_item_sale_price(item_id)

    item_ids = [p[id_key] for p in products if p.get(id_key)]
    if not item_ids:
        return

    tasks = [_fetch_sp(iid) for iid in item_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sp_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if data and isinstance(data, dict):
            sp_map[iid] = data

    for p in products:
        iid = p.get(id_key)
        sp = sp_map.get(iid)
        if not sp:
            continue
        amount = sp.get("amount")
        regular = sp.get("regular_amount")
        if amount and regular and regular > amount:
            p[price_key] = amount
            p["original_price"] = regular


async def _enrich_with_meli_health(client, products: list, id_key="id"):
    """Consulta /items/{id}/health en paralelo y agrega _meli_health y _meli_health_level."""
    sem = asyncio.Semaphore(10)

    async def _fetch(item_id):
        async with sem:
            h = await client.get_item_health(item_id)
            return item_id, h

    ids = [p[id_key] for p in products if p.get(id_key)]
    if not ids:
        return
    results = await asyncio.gather(*[_fetch(i) for i in ids], return_exceptions=True)
    h_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if data and isinstance(data, dict):
            h_map[iid] = data
    for p in products:
        h = h_map.get(p.get(id_key))
        if h:
            p["_meli_health"] = h.get("health", 0)
            p["_meli_health_level"] = h.get("level", "basic")


async def _enrich_with_promotions(client, products: list, id_key="id"):
    """Consulta /seller-promotions/items/{id} en paralelo."""
    sem = asyncio.Semaphore(10)

    async def _fetch(item_id):
        async with sem:
            promos = await client.get_item_promotions(item_id)
            return item_id, promos

    ids = [p[id_key] for p in products if p.get(id_key)]
    if not ids:
        return
    results = await asyncio.gather(*[_fetch(i) for i in ids], return_exceptions=True)
    p_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if isinstance(data, list):
            p_map[iid] = data
    # Tipos que NO cuentan como deal del vendedor (MeLi los gestiona solo)
    _auto_types = {"SMART", "PRE_NEGOTIATED", "SELLER_COUPON_CAMPAIGN"}
    for p in products:
        promos = p_map.get(p.get(id_key), [])
        p["_promotions"] = promos
        active_promos = [
            pr for pr in promos
            if pr.get("status") in ("started", "active", "pending")
            and pr.get("type") not in _auto_types
        ]
        p["_has_deal"] = len(active_promos) > 0
        p["_deal_types"] = list(set(pr.get("type", "") for pr in active_promos))
        # Actualizar price/original_price desde la promo activa
        if active_promos:
            ap = active_promos[0]
            if ap.get("price") and ap["price"] > 0:
                p["price"] = ap["price"]
            if ap.get("original_price") and ap["original_price"] > 0:
                p["original_price"] = ap["original_price"]


async def _enrich_with_bm_product_info(products: list, sku_key="sku"):
    """Consulta BinManager InventoryReport para obtener RetailPrice, AvgCostQTY, Brand, etc.
    Deduplica por base SKU para minimizar llamadas."""
    import httpx
    BM_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
    sem = asyncio.Semaphore(15)

    # Deduplicate by base SKU (clean for BM)
    base_to_skus = {}
    for p in products:
        sku = p.get(sku_key, "")
        if not sku:
            continue
        clean = _clean_sku_for_bm(sku)
        if not clean:
            continue
        base = _extract_base_sku(clean).upper()
        base_to_skus.setdefault(base, []).append(sku)

    async def _fetch(base, http):
        async with sem:
            try:
                resp = await http.post(BM_URL, json={
                    "COMPANYID": 1,
                    "SEARCH": base,
                    "CONCEPTID": 8,
                    "NUMBERPAGE": 1,
                    "RECORDSPAGE": 10,
                }, headers={"Content-Type": "application/json"}, timeout=30.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list) and data:
                        for item in data:
                            if item.get("SKU", "").upper() == base:
                                return base, item
                        return base, data[0]
            except Exception:
                pass
            return base, None

    unique_bases = list(base_to_skus.keys())[:30]  # Limit to avoid BM overload
    async with httpx.AsyncClient() as http:
        results = await asyncio.gather(
            *[_fetch(b, http) for b in unique_bases],
            return_exceptions=True
        )

    # Map base -> data
    base_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        base, data = r
        if data:
            base_map[base] = data

    # Apply to all products that share each base SKU
    for p in products:
        sku = p.get(sku_key, "")
        if not sku:
            continue
        clean = _clean_sku_for_bm(sku)
        if not clean:
            continue
        base = _extract_base_sku(clean).upper()
        bm = base_map.get(base)
        if bm:
            p["_bm_retail_price"] = bm.get("RetailPrice", 0) or 0
            p["_bm_avg_cost"] = bm.get("AvgCostQTY", 0) or 0
            p["_bm_brand"] = bm.get("Brand", "")
            p["_bm_model"] = bm.get("Model", "")
            p["_bm_title"] = bm.get("Title", "")


async def _enrich_with_bm_stock(products: list, sku_key="sku"):
    """Consulta BinManager Warehouse endpoint para stock real por almacen (MTY/CDMX/TJ)."""
    import httpx
    BM_WH_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
    sem = asyncio.Semaphore(10)

    async def _fetch(sku, http):
        async with sem:
            clean_sku = _clean_sku_for_bm(sku)
            base = _extract_base_sku(clean_sku)
            if not base:
                return sku, None
            try:
                resp = await http.post(BM_WH_URL, json={
                    "COMPANYID": 1, "SKU": base, "WarehouseID": None,
                    "LocationID": "47,62,68", "BINID": None,
                    "Condition": _bm_conditions_for_sku(clean_sku), "ForInventory": 0, "SUPPLIERS": None,
                }, timeout=15.0)
                if resp.status_code == 200:
                    rows = resp.json() or []
                    mty = cdmx = tj = 0
                    for row in rows:
                        qty = row.get("QtyTotal", 0) or 0
                        wname = (row.get("WarehouseName") or "").lower()
                        if "monterrey" in wname or "maxx" in wname:
                            mty += qty
                        elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                            cdmx += qty
                        else:
                            tj += qty
                    return sku, {"mty": mty, "cdmx": cdmx, "tj": tj}
            except Exception:
                pass
            return sku, None

    async with httpx.AsyncClient() as http:
        tasks = [_fetch(p.get(sku_key, ""), http) for p in products if p.get(sku_key)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    bm_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        sku, data = r
        if data:
            bm_map[sku] = data

    for p in products:
        bm = bm_map.get(p.get(sku_key))
        if bm:
            p["_bm_mty"] = max(0, bm.get("mty", 0) or 0)
            p["_bm_cdmx"] = max(0, bm.get("cdmx", 0) or 0)
            p["_bm_tj"] = max(0, bm.get("tj", 0) or 0)
            p["_bm_total"] = p["_bm_mty"] + p["_bm_cdmx"]


def _aggregate_sales_by_item(orders: list) -> dict:
    """Agrupa ventas de ordenes por item_id. Retorna {item_id: {units, revenue, fees}}."""
    sales = {}
    for order in orders:
        if order.get("status") not in ("paid", "delivered"):
            continue
        for oi in order.get("order_items", []):
            item = oi.get("item", {})
            iid = item.get("id", "")
            if not iid:
                continue
            qty = oi.get("quantity", 0)
            unit_price = oi.get("unit_price", 0)
            fee = oi.get("sale_fee", 0) or 0
            sales.setdefault(iid, {"units": 0, "revenue": 0, "fees": 0})
            sales[iid]["units"] += qty
            sales[iid]["revenue"] += qty * unit_price
            sales[iid]["fees"] += fee
    return sales


def _get_item_sku(body: dict) -> str:
    """Extrae SKU de un item body (seller_custom_field o SELLER_SKU attribute)."""
    sku = body.get("seller_custom_field") or ""
    if not sku or sku == "None":
        sku = ""
        for attr in body.get("attributes", []):
            if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                sku = attr["value_name"]
                break
    if not sku and body.get("variations"):
        for var in body["variations"]:
            if var.get("seller_custom_field"):
                sku = var["seller_custom_field"]
                break
            for va in var.get("attributes", []):
                if va.get("id") == "SELLER_SKU" and va.get("value_name"):
                    sku = va["value_name"]
                    break
            if sku:
                break
    return sku


async def get_current_user():
    """Obtiene el usuario actual si hay sesion activa."""
    client = await get_meli_client()
    if not client:
        return None
    try:
        user = await client.get_user_info()
        return user
    except Exception:
        return None
    finally:
        await client.close()


# === Rutas de paginas ===

@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await get_current_user()
    if user:
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse("login.html", {"request": request, "user": None})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    # Pre-warm caches al entrar al dashboard
    global _prewarm_task
    if _prewarm_task is None or _prewarm_task.done():
        _prewarm_task = asyncio.create_task(_prewarm_caches())
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "active": "dashboard",
        **ctx
    })


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("orders.html", {
        "request": request,
        "user": user,
        "active": "orders",
        **ctx
    })


@app.get("/items", response_class=HTMLResponse)
async def items_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    # Pre-warm caches al entrar a Centro de Productos
    global _prewarm_task
    if _prewarm_task is None or _prewarm_task.done():
        _prewarm_task = asyncio.create_task(_prewarm_caches())
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("items.html", {
        "request": request,
        "user": user,
        "active": "items",
        **ctx
    })


@app.get("/sku-sales", response_class=HTMLResponse)
async def sku_sales_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("sku_sales.html", {
        "request": request,
        "user": user,
        "active": "sku_sales",
        **ctx
    })


@app.get("/sku-compare", response_class=HTMLResponse)
async def sku_compare_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("sku_compare.html", {
        "request": request,
        "user": user,
        "active": "sku_compare",
        **ctx
    })


@app.get("/sku-inventory", response_class=HTMLResponse)
async def sku_inventory_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("sku_inventory.html", {
        "request": request,
        "user": user,
        "active": "sku_inventory",
        **ctx
    })


@app.get("/api/sku-compare")
async def sku_compare_api(
    a_from: str = Query(..., description="Periodo A inicio YYYY-MM-DD"),
    a_to: str = Query(..., description="Periodo A fin YYYY-MM-DD"),
    b_from: str = Query(..., description="Periodo B inicio YYYY-MM-DD"),
    b_to: str = Query(..., description="Periodo B fin YYYY-MM-DD"),
):
    """Compara ventas por SKU entre dos periodos, con deteccion de stock."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        import asyncio
        orders_a_task = client.fetch_all_orders(date_from=a_from, date_to=a_to)
        orders_b_task = client.fetch_all_orders(date_from=b_from, date_to=b_to)
        orders_a, orders_b = await asyncio.gather(orders_a_task, orders_b_task)

        def aggregate_skus(orders):
            sku_map = {}
            for order in orders:
                if order.get("status") not in ["paid", "delivered"]:
                    continue
                for oi in order.get("order_items", []):
                    item = oi.get("item", {})
                    raw_sku = item.get("seller_sku") or item.get("seller_custom_field") or "SIN SKU"
                    base = _extract_base_sku(raw_sku)
                    title = item.get("title", "-")
                    item_id = item.get("id", "")
                    qty = oi.get("quantity", 1)
                    price = oi.get("unit_price", 0)
                    fee = oi.get("sale_fee", 0) or 0
                    net = price * qty - fee - fee * 0.16
                    if base not in sku_map:
                        sku_map[base] = {"sku": raw_sku, "title": title, "units": 0, "revenue": 0, "item_ids": set()}
                    sku_map[base]["units"] += qty
                    sku_map[base]["revenue"] += net
                    if item_id:
                        sku_map[base]["item_ids"].add(item_id)
            return sku_map

        skus_a = aggregate_skus(orders_a)
        skus_b = aggregate_skus(orders_b)

        all_skus = set(list(skus_a.keys()) + list(skus_b.keys()))

        rows = []
        for sku in all_skus:
            a = skus_a.get(sku, {"units": 0, "revenue": 0, "title": "", "item_ids": set()})
            b = skus_b.get(sku, {"units": 0, "revenue": 0, "title": "", "item_ids": set()})
            title = a.get("title") or b.get("title") or "-"
            units_a = a["units"]
            units_b = b["units"]
            rev_a = a["revenue"]
            rev_b = b["revenue"]
            unit_diff = units_a - units_b
            rev_diff = rev_a - rev_b
            pct = ((units_a - units_b) / units_b * 100) if units_b > 0 else (100.0 if units_a > 0 else 0.0)

            status = "equal"
            if units_a > units_b:
                status = "up"
            elif units_a < units_b:
                status = "down"
            if units_b > 0 and units_a == 0:
                status = "lost"
            if units_a > 0 and units_b == 0:
                status = "new"

            # Combinar item_ids de ambos periodos
            item_ids = a.get("item_ids", set()) | b.get("item_ids", set())

            rows.append({
                "sku": sku,
                "title": title,
                "units_a": units_a,
                "units_b": units_b,
                "unit_diff": unit_diff,
                "pct": round(pct, 1),
                "rev_a": round(rev_a, 2),
                "rev_b": round(rev_b, 2),
                "rev_diff": round(rev_diff, 2),
                "status": status,
                "item_ids": list(item_ids),
                "reason": "",
                "stock": None,
                "item_status": ""
            })

        # Obtener info de stock/estado de items para SKUs lost y down
        check_rows = [r for r in rows if r["status"] in ("lost", "down")]
        item_ids_to_check = set()
        for r in check_rows:
            for iid in r["item_ids"]:
                item_ids_to_check.add(iid)

        item_info = {}
        id_list = list(item_ids_to_check)
        for i in range(0, len(id_list), 20):
            batch = id_list[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if body:
                        item_info[body.get("id", "")] = {
                            "stock": body.get("available_quantity", 0),
                            "status": body.get("status", ""),
                        }
            except Exception:
                pass

        # Asignar razon a cada row lost/down
        for r in check_rows:
            stocks = []
            statuses = []
            for iid in r["item_ids"]:
                info = item_info.get(iid)
                if info:
                    stocks.append(info["stock"])
                    statuses.append(info["status"])

            total_stock = sum(stocks) if stocks else None
            r["stock"] = total_stock

            if statuses:
                r["item_status"] = statuses[0]

            if total_stock is not None and total_stock == 0:
                if any(s == "paused" for s in statuses):
                    r["reason"] = "sin_stock_pausado"
                else:
                    r["reason"] = "sin_stock"
            elif any(s == "paused" for s in statuses):
                r["reason"] = "pausado"
            elif any(s == "inactive" for s in statuses):
                r["reason"] = "inactivo"
            else:
                r["reason"] = "con_stock"

        # Limpiar item_ids del response (no necesario en frontend)
        for r in rows:
            del r["item_ids"]

        # Ordenar: perdidos primero, luego mayor caida
        priority = {"lost": 0, "down": 1, "equal": 2, "up": 3, "new": 4}
        rows.sort(key=lambda r: (priority.get(r["status"], 2), r["unit_diff"]))

        # Resumen
        total_a = sum(r["units_a"] for r in rows)
        total_b = sum(r["units_b"] for r in rows)
        rev_total_a = sum(r["rev_a"] for r in rows)
        rev_total_b = sum(r["rev_b"] for r in rows)
        lost_count = len([r for r in rows if r["status"] == "lost"])
        new_count = len([r for r in rows if r["status"] == "new"])
        down_count = len([r for r in rows if r["status"] == "down"])
        up_count = len([r for r in rows if r["status"] == "up"])
        stock_issue_count = len([r for r in rows if r["reason"] in ("sin_stock", "sin_stock_pausado")])
        paused_count = len([r for r in rows if r["reason"] == "pausado"])

        return {
            "summary": {
                "units_a": total_a,
                "units_b": total_b,
                "units_diff": total_a - total_b,
                "units_pct": round((total_a - total_b) / total_b * 100, 1) if total_b > 0 else 0,
                "rev_a": round(rev_total_a, 2),
                "rev_b": round(rev_total_b, 2),
                "rev_diff": round(rev_total_a - rev_total_b, 2),
                "lost": lost_count,
                "new": new_count,
                "down": down_count,
                "up": up_count,
                "stock_issue": stock_issue_count,
                "paused": paused_count,
            },
            "rows": rows
        }
    finally:
        await client.close()


@app.get("/ads", response_class=HTMLResponse)
async def ads_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("ads.html", {
        "request": request,
        "user": user,
        "active": "ads",
        **ctx
    })


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("health.html", {
        "request": request,
        "user": user,
        "active": "health",
        **ctx
    })


@app.get("/items-health", response_class=HTMLResponse)
async def items_health_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse("no_session.html", {"request": request})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse("items_health.html", {
        "request": request,
        "user": user,
        "active": "items_health",
        **ctx
    })


# === Partials para HTMX ===

@app.get("/partials/metrics", response_class=HTMLResponse)
async def metrics_partial(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        from datetime import datetime
        now = datetime.utcnow()
        if not date_from:
            date_from = now.replace(day=1).strftime("%Y-%m-%d")
        if not date_to:
            date_to = now.strftime("%Y-%m-%d")

        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)
        items_data = await client.get_items(limit=1)

        paid_orders = [o for o in all_orders if o.get("status") in ["paid", "delivered"]]

        # Enrich with net_received_amount for accurate revenue
        await client.enrich_orders_with_net_amount(paid_orders)

        metrics = {
            "summary": {
                "total_orders": len(all_orders),
                "monthly_sales": len(paid_orders),
                "monthly_revenue": sum(order_net_revenue(o) for o in paid_orders),
                "active_items": items_data.get("paging", {}).get("total", 0)
            }
        }

        return templates.TemplateResponse("partials/metrics_cards.html", {
            "request": request,
            "metrics": metrics
        })
    finally:
        await client.close()


@app.get("/partials/recent-orders", response_class=HTMLResponse)
async def recent_orders_partial(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        orders_data = await client.get_orders(
            limit=5,
            date_from=date_from or None,
            date_to=date_to or None
        )
        orders = orders_data.get("results", [])

        # Enrich with net_received_amount
        await client.enrich_orders_with_net_amount(orders)

        html = "<div class='divide-y divide-gray-200'>"
        for order in orders:
            title = order["order_items"][0]["item"]["title"][:35] + "..." if order.get("order_items") else "-"
            sku = ""
            if order.get("order_items"):
                item = order["order_items"][0]["item"]
                sku = item.get("seller_sku") or item.get("seller_custom_field") or "-"
            amount = order_net_revenue(order)
            order_id = order.get("id", "-")
            status_class = "bg-green-100 text-green-800" if order.get("status") == "paid" else "bg-gray-100 text-gray-800"
            html += f"""
            <div class='py-3 flex justify-between items-center'>
                <div>
                    <p class='text-sm font-medium text-gray-800'>{title}</p>
                    <p class='text-xs text-gray-500'>Orden: {order_id} | SKU: {sku} | {order['date_created'][:10]}</p>
                </div>
                <div class='text-right'>
                    <p class='font-semibold'>${amount:.2f}</p>
                    <span class='px-2 py-0.5 text-xs rounded-full {status_class}'>{order.get('status', '-')}</span>
                </div>
            </div>
            """
        html += "</div>"

        if not orders:
            html = "<p class='text-center py-4 text-gray-500'>No hay ventas recientes</p>"

        return HTMLResponse(html)
    finally:
        await client.close()


@app.get("/partials/orders-table", response_class=HTMLResponse)
async def orders_table_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    sort: str = Query("date_desc")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        user = await client.get_user_info()
        orders_data = await client.get_orders(offset=offset, limit=limit, sort=sort)
        raw_orders = orders_data.get("results", [])

        # Fetch net_received_amount from collections API for accurate totals
        net_amounts = {}  # order_id -> net_received_amount
        for o in raw_orders:
            payments = o.get("payments", [])
            total_net = 0.0
            for p in payments:
                pid = p.get("id")
                if pid:
                    try:
                        net = await client.get_payment_net_amount(str(pid))
                        if net is not None:
                            total_net += net
                    except Exception:
                        pass
            if total_net > 0:
                net_amounts[o.get("id")] = total_net

        # Fetch shipping costs sequentially (Windows select() FD limit)
        shipping_costs = {}  # order_id -> cost
        for o in raw_orders:
            ship = o.get("shipping", {})
            ship_id = ship.get("id") if isinstance(ship, dict) else None
            if ship_id:
                try:
                    cost = await client.get_shipment_costs(str(ship_id))
                    shipping_costs[o.get("id")] = cost
                except Exception:
                    pass

        enriched = []
        for o in raw_orders:
            total = o.get("total_amount", 0) or 0
            items = o.get("order_items", [])
            total_fees = 0.0
            items_detail = []
            for oi in items:
                item_info = oi.get("item", {})
                qty = oi.get("quantity", 1)
                unit_price = oi.get("unit_price", 0) or 0
                full_price = oi.get("full_unit_price", 0) or 0
                fee = oi.get("sale_fee", 0) or 0
                iva_fee = round(fee * 0.16, 2)
                subtotal = unit_price * qty
                total_fees += fee
                items_detail.append(SimpleNamespace(
                    title=item_info.get("title", "-"),
                    sku=item_info.get("seller_sku") or item_info.get("seller_custom_field") or "-",
                    item_id=item_info.get("id", "-"),
                    quantity=qty,
                    unit_price=unit_price,
                    full_unit_price=full_price,
                    discount=round((full_price - unit_price) * qty, 2) if full_price > unit_price else 0,
                    subtotal=subtotal,
                    sale_fee=fee,
                    iva_fee=iva_fee,
                    listing_type=oi.get("listing_type_id", "-"),
                ))
            total_iva = round(total_fees * 0.16, 2)

            # Shipping cost from API
            shipping = o.get("shipping", {})
            ship_cost = shipping_costs.get(o.get("id"), 0)
            iva_ship = round(ship_cost * 0.16, 2)

            # Use net_received_amount from MeLi if available (includes all taxes)
            if o.get("id") in net_amounts:
                net = net_amounts[o.get("id")]
            else:
                net = round(total - total_fees - total_iva - ship_cost - iva_ship, 2)

            # Payment info
            payments = o.get("payments", [])
            approved_payment = None
            for p in payments:
                if p.get("status") == "approved":
                    approved_payment = p
                    break
            payment_method = approved_payment.get("payment_method_id", "-") if approved_payment else "-"
            payment_type = approved_payment.get("payment_type", "-") if approved_payment else "-"
            installments = approved_payment.get("installments", 1) if approved_payment else 1

            buyer_raw = o.get("buyer") or {}
            # Calcular impuestos como MeLi: bruto - comisión - envío - neto
            # Esto incluye IVA sobre comisión, IVA sobre envío, y retenciones
            taxes = round(total - total_fees - ship_cost - net, 2)

            enriched.append(SimpleNamespace(
                id=o.get("id", "-"),
                date_created=o.get("date_created", "-"),
                status=o.get("status", "-"),
                buyer=SimpleNamespace(nickname=buyer_raw.get("nickname", "-")),
                total_amount=total,
                total_fees=round(total_fees, 2),
                total_iva=total_iva,
                shipping_cost=ship_cost,
                iva_shipping=iva_ship,
                taxes=taxes,
                net_amount=net,
                shipping_id=shipping.get("id"),
                payment_method=payment_method,
                payment_type=payment_type,
                installments=installments,
                order_items=items_detail,
                pack_id=o.get("pack_id"),
                tags=o.get("tags", []),
            ))

        return templates.TemplateResponse("partials/orders_table.html", {
            "request": request,
            "orders": enriched,
            "paging": orders_data.get("paging", {}),
            "offset": offset,
            "limit": limit,
            "seller_name": user.get("nickname", "-")
        })
    finally:
        await client.close()


@app.get("/partials/items-grid", response_class=HTMLResponse)
async def items_grid_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query("active"),
    sku: str = Query("", description="Buscar por SKU")
):
    import httpx as _httpx
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        user = await client.get_user_info()
        user_id = user["id"]

        # Si hay búsqueda por SKU, buscar directamente
        if sku and sku.strip():
            sku_clean = sku.strip().upper()
            search_result = await client.get(
                f"/users/{user_id}/items/search",
                params={"seller_sku": sku_clean, "limit": 50}
            )
            item_ids = search_result.get("results", [])
            items_search = {"paging": {"total": len(item_ids), "offset": 0, "limit": len(item_ids)}}
        else:
            # Búsqueda normal por estado
            if status:
                items_search = await client.get_items(offset=offset, limit=limit, status=status)
            else:
                # Todos los estados
                items_search = await client.get(
                    f"/users/{user_id}/items/search",
                    params={"offset": offset, "limit": limit}
                )
            item_ids = items_search.get("results", [])

        items = []
        if item_ids:
            for i in range(0, len(item_ids), 20):
                batch = item_ids[i:i+20]
                items.extend(await client.get_items_details(batch))

        # Enriquecer con sale_price para detectar deals
        bodies_for_sp = []
        for it in items:
            body = it.get("body") or it
            if body.get("id"):
                bodies_for_sp.append(body)
        await _enrich_with_sale_prices(client, bodies_for_sp, id_key="id", price_key="price")

        # Consultar inventario BinManager para cada item (Warehouse endpoint = stock real)
        BM_WH_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
        inventory_map = {}  # item_id -> {MTY, CDMX}
        sku_to_items = {}   # base -> {sku, item_ids}
        for it in items:
            body = it.get("body") or it
            sku = body.get("seller_custom_field") or ""
            if not sku or sku == "None":
                sku = ""
                for attr in body.get("attributes", []):
                    if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                        sku = attr["value_name"]
                        break
            item_id = body.get("id", "")
            if sku and item_id:
                base = _extract_base_sku(sku)
                sku_to_items.setdefault(base, {"sku": sku, "item_ids": []})
                sku_to_items[base]["item_ids"].append(item_id)

        if sku_to_items:
            sem = asyncio.Semaphore(10)
            async def _fetch_inv(base_sku: str, full_sku: str, http: _httpx.AsyncClient):
                async with sem:
                    try:
                        resp = await http.post(BM_WH_URL, json={
                            "COMPANYID": 1, "SKU": base_sku, "WarehouseID": None,
                            "LocationID": "47,62,68", "BINID": None,
                            "Condition": _bm_conditions_for_sku(full_sku), "ForInventory": 0, "SUPPLIERS": None,
                        }, timeout=15.0)
                        if resp.status_code == 200:
                            rows = resp.json() or []
                            mty = cdmx = tj = 0
                            for row in rows:
                                qty = row.get("QtyTotal", 0) or 0
                                wname = (row.get("WarehouseName") or "").lower()
                                if "monterrey" in wname or "maxx" in wname:
                                    mty += qty
                                elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                                    cdmx += qty
                                else:
                                    tj += qty
                            return base_sku, {"MTY": mty, "CDMX": cdmx, "TJ": tj, "total": mty + cdmx}
                    except Exception:
                        pass
                    return base_sku, None

            async with _httpx.AsyncClient() as http:
                tasks = [_fetch_inv(base, sku_to_items[base]["sku"], http) for base in sku_to_items.keys()]
                for coro in asyncio.as_completed(tasks):
                    queried_base, inv = await coro
                    if inv:
                        for b, info in sku_to_items.items():
                            if b.upper() == queried_base.upper():
                                for iid in info["item_ids"]:
                                    inventory_map[iid] = inv

        # Construir metadata por item (brand, model, variaciones)
        item_meta = {}
        for it in items:
            body = it.get("body") or it
            iid = body.get("id", "")
            if not iid:
                continue
            sku = body.get("seller_custom_field") or ""
            if not sku or sku == "None":
                sku = ""
                for attr in body.get("attributes", []):
                    if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                        sku = attr["value_name"]
                        break
            brand = ""
            model = ""
            for attr in body.get("attributes", []):
                aid = attr.get("id", "")
                if aid == "BRAND" and attr.get("value_name"):
                    brand = attr["value_name"]
                elif aid == "MODEL" and attr.get("value_name"):
                    model = attr["value_name"]
            variations = body.get("variations") or []
            shipping = body.get("shipping", {})
            logistic_type = shipping.get("logistic_type", "")
            item_meta[iid] = {
                "sku": sku,
                "brand": brand,
                "model": model,
                "has_variations": len(variations) > 1,
                "variation_count": len(variations),
                "is_full": logistic_type == "fulfillment",
                "logistic_type": logistic_type,
                "sold_qty": body.get("sold_quantity", 0),
            }

        return templates.TemplateResponse("partials/items_grid.html", {
            "request": request,
            "items": items,
            "paging": items_search.get("paging", {}),
            "offset": offset,
            "limit": limit,
            "status": status,
            "inventory_map": inventory_map,
            "item_meta": item_meta,
        })
    finally:
        await client.close()


@app.get("/api/items/{item_id}/bm-cost")
async def get_item_bm_cost(item_id: str):
    """Devuelve costo BM de un item on-demand (para modal de deals desde inventario)."""
    import httpx
    BM_INV_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
    client = await get_meli_client()
    if not client:
        return JSONResponse({"avg_cost_usd": 0, "retail_price_usd": 0, "error": "No autenticado"})
    try:
        data = await client.get(f"/items/{item_id}")
        sku = _get_item_sku(data) if data else ""
        if not sku:
            return JSONResponse({"avg_cost_usd": 0, "retail_price_usd": 0})
        base = _extract_base_sku(sku).upper()
        async with httpx.AsyncClient() as http:
            resp = await http.post(BM_INV_URL, json={
                "COMPANYID": 1, "SEARCH": base, "CONCEPTID": 8,
                "NUMBERPAGE": 1, "RECORDSPAGE": 10,
            }, headers={"Content-Type": "application/json"}, timeout=30.0)
            if resp.status_code == 200:
                items = resp.json()
                if items and isinstance(items, list):
                    for it in items:
                        if it.get("SKU", "").upper() == base:
                            return JSONResponse({
                                "avg_cost_usd": it.get("AvgCostQTY", 0) or 0,
                                "retail_price_usd": it.get("RetailPrice", 0) or 0,
                            })
                    if items:
                        return JSONResponse({
                            "avg_cost_usd": items[0].get("AvgCostQTY", 0) or 0,
                            "retail_price_usd": items[0].get("RetailPrice", 0) or 0,
                        })
        return JSONResponse({"avg_cost_usd": 0, "retail_price_usd": 0})
    finally:
        await client.close()


@app.get("/partials/items-no-stock", response_class=HTMLResponse)
async def items_no_stock_redirect(request: Request):
    """Legacy redirect -> stock issues."""
    return RedirectResponse("/partials/products-stock-issues", status_code=302)


@app.get("/partials/products-stock-issues", response_class=HTMLResponse)
async def products_stock_issues_partial(request: Request):
    """Stock tab: Reabastecer + Riesgo + Activar. Resultado cacheado 5 min."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        # Cache de resultado completo (evita re-computar cada vez)
        key = f"stock_issues:{client.user_id}"
        entry = _stock_issues_cache.get(key)
        if entry and (_time.time() - entry[0]) < _STOCK_ISSUES_TTL:
            ctx = entry[1].copy()
            # include_paused: traer items pausados para seccion Activar
            ctx["request"] = request
            return templates.TemplateResponse("partials/products_stock_issues.html", ctx)

        from datetime import datetime, timedelta
        now = datetime.utcnow()
        date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        all_bodies, all_orders = await asyncio.gather(
            _get_all_products_cached(client, include_all=True),
            _get_orders_cached(client, date_from, date_to),
        )
        sales_map = _aggregate_sales_by_item(all_orders)
        products = _build_product_list(all_bodies, sales_map)
        _enrich_sku_from_orders(products, all_orders)

        # BM stock para todos
        bm_map = await _get_bm_stock_cached(products)
        _apply_bm_stock(products, bm_map)

        # Seccion A: Reabastecer (MeLi=0, BM>0, tiene ventas)
        restock = [
            p for p in products
            if p.get("available_quantity", 0) == 0
            and (p.get("_bm_total") or 0) > 0
            and p.get("units", 0) > 0
        ]
        restock.sort(key=lambda x: x.get("units", 0), reverse=True)

        # Seccion B: Riesgo Sobreventa (MeLi>0, BM=0, no FULL)
        oversell_risk = [
            p for p in products
            if p.get("available_quantity", 0) > 0
            and (p.get("_bm_total") or 0) == 0
            and not p.get("is_full")
            and p.get("sku")
        ]
        oversell_risk.sort(key=lambda x: x.get("available_quantity", 0), reverse=True)

        # Seccion C: Activar (MeLi=0, BM>0, sin ventas)
        restock_ids = {p["id"] for p in restock}
        activate = [
            p for p in products
            if p.get("available_quantity", 0) == 0
            and (p.get("_bm_total") or 0) > 0
            and p["id"] not in restock_ids
        ]
        activate.sort(key=lambda x: x.get("_bm_total", 0), reverse=True)

        # KPIs
        restock_count = len(restock)
        lost_revenue = sum(p.get("revenue", 0) for p in restock)
        risk_count = len(oversell_risk)
        risk_stock = sum(p.get("available_quantity", 0) for p in oversell_risk)
        activate_count = len(activate)
        activate_stock = sum(p.get("_bm_total", 0) for p in activate)

        ctx = {
            "restock": restock,
            "oversell_risk": oversell_risk,
            "activate": activate,
            "restock_count": restock_count,
            "lost_revenue": lost_revenue,
            "risk_count": risk_count,
            "risk_stock": risk_stock,
            "activate_count": activate_count,
            "activate_stock": activate_stock,
        }
        _stock_issues_cache[key] = (_time.time(), ctx)
        ctx_with_req = ctx.copy()
        ctx_with_req["request"] = request
        return templates.TemplateResponse("partials/products_stock_issues.html", ctx_with_req)
    finally:
        await client.close()


# ---------- Product Intelligence: shared cache ----------

import time as _time

# Cache compartido: items details + BM stock (evita re-fetch entre tabs)
_products_cache: dict[str, tuple[float, list]] = {}
_bm_stock_cache: dict[str, tuple[float, dict]] = {}
_category_cache: dict[str, str] = {}  # category_id -> name
_PRODUCTS_CACHE_TTL = 900   # 15 min
_BM_CACHE_TTL = 900         # 15 min
_orders_cache: dict[str, tuple[float, list]] = {}
_ORDERS_CACHE_TTL = 900     # 15 min
_sale_price_cache: dict[str, tuple[float, dict | None]] = {}
_SALE_PRICE_CACHE_TTL = 300  # 5 min
_stock_issues_cache: dict[str, tuple[float, dict]] = {}
_STOCK_ISSUES_TTL = 300      # 5 min
_products_fetch_lock = asyncio.Lock()  # prevenir doble fetch concurrente
_synced_alert_items: set[str] = set()  # items ya sincronizados (excluidos de alertas hasta cache refresh)


_ALL_MELI_STATUSES = ["active", "paused", "closed", "inactive", "under_review"]

async def _get_all_products_cached(client, include_paused=False, include_all=False) -> list[dict]:
    """Devuelve todos los items, cacheado 15 min.
    include_all=True trae TODOS los statuses (active, paused, closed, inactive, under_review).
    include_paused=True trae active + paused.
    Lock previene doble fetch cuando multiples requests concurrentes."""
    if include_all:
        suffix = ":all_statuses"
    elif include_paused:
        suffix = ":with_paused"
    else:
        suffix = ""
    key = f"products:{client.user_id}{suffix}"
    entry = _products_cache.get(key)
    if entry and (_time.time() - entry[0]) < _PRODUCTS_CACHE_TTL:
        return entry[1]

    async with _products_fetch_lock:
        entry = _products_cache.get(key)
        if entry and (_time.time() - entry[0]) < _PRODUCTS_CACHE_TTL:
            return entry[1]

        if include_all:
            all_ids = await client.get_all_item_ids_by_statuses(_ALL_MELI_STATUSES)
        elif include_paused:
            all_ids = await client.get_all_item_ids_by_statuses(["active", "paused"])
        else:
            all_ids = await client.get_all_active_item_ids()
        all_details = []
        sem = asyncio.Semaphore(5)

        async def _batch(ids):
            async with sem:
                try:
                    return await client.get_items_details(ids)
                except Exception:
                    return []

        batches = [all_ids[i:i+20] for i in range(0, len(all_ids), 20)]
        results = await asyncio.gather(*[_batch(b) for b in batches])
        for batch_result in results:
            all_details.extend(batch_result)

        products = []
        for d in all_details:
            body = d.get("body", d)
            if body and body.get("id"):
                products.append(body)

        _products_cache[key] = (_time.time(), products)
        return products


async def _get_orders_cached(client, date_from: str, date_to: str) -> list:
    """Ordenes de 30 dias con cache."""
    key = f"orders:{client.user_id}:{date_from}"
    entry = _orders_cache.get(key)
    if entry and (_time.time() - entry[0]) < _ORDERS_CACHE_TTL:
        return entry[1]
    orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)
    _orders_cache[key] = (_time.time(), orders)
    return orders


async def _get_sale_prices_cached(client, item_ids: list[str]) -> dict[str, dict]:
    """Fetch /items/{id}/sale_price para detectar deals activos. Cache 5min.
    Retorna {item_id: {amount, regular_amount, ...}} solo para items CON descuento."""
    now = _time.time()
    result = {}
    to_fetch = []
    for iid in item_ids:
        entry = _sale_price_cache.get(iid)
        if entry and (now - entry[0]) < _SALE_PRICE_CACHE_TTL:
            if entry[1]:
                result[iid] = entry[1]
        else:
            to_fetch.append(iid)

    if not to_fetch:
        return result

    sem = asyncio.Semaphore(10)

    async def _fetch_one(iid):
        async with sem:
            try:
                data = await client.get_item_sale_price(iid)
                if data and data.get("regular_amount") and data.get("amount"):
                    if data["regular_amount"] > data["amount"]:
                        _sale_price_cache[iid] = (now, data)
                        return iid, data
                _sale_price_cache[iid] = (now, None)
                return iid, None
            except Exception:
                _sale_price_cache[iid] = (now, None)
                return iid, None

    results = await asyncio.gather(*[_fetch_one(iid) for iid in to_fetch])
    for iid, data in results:
        if data:
            result[iid] = data
    return result


# --- Background pre-warm ---
_prewarm_task = None

async def _prewarm_caches():
    """Pre-carga products + orders + BM stock + stock issues en background."""
    try:
        client = await get_meli_client()
        if not client:
            return
        # prewarm include_paused marker
        try:
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
            date_to = now.strftime("%Y-%m-%d")

            all_bodies, all_orders = await asyncio.gather(
                _get_all_products_cached(client, include_all=True),
                _get_orders_cached(client, date_from, date_to),
            )
            sales_map = _aggregate_sales_by_item(all_orders)
            products = _build_product_list(all_bodies, sales_map)
            _enrich_sku_from_orders(products, all_orders)
            bm_map = await _get_bm_stock_cached(products)
            _apply_bm_stock(products, bm_map)

            # Pre-computar stock issues result
            restock = [p for p in products if p.get("available_quantity", 0) == 0 and (p.get("_bm_total") or 0) > 0 and p.get("units", 0) > 0]
            restock.sort(key=lambda x: x.get("units", 0), reverse=True)
            oversell_risk = [p for p in products if p.get("available_quantity", 0) > 0 and (p.get("_bm_total") or 0) == 0 and not p.get("is_full") and p.get("sku")]
            oversell_risk.sort(key=lambda x: x.get("available_quantity", 0), reverse=True)
            restock_ids = {p["id"] for p in restock}
            activate = [p for p in products if p.get("available_quantity", 0) == 0 and (p.get("_bm_total") or 0) > 0 and p["id"] not in restock_ids]
            activate.sort(key=lambda x: x.get("_bm_total", 0), reverse=True)
            _stock_issues_cache[f"stock_issues:{client.user_id}"] = (_time.time(), {
                "restock": restock, "oversell_risk": oversell_risk, "activate": activate,
                "restock_count": len(restock), "lost_revenue": sum(p.get("revenue", 0) for p in restock),
                "risk_count": len(oversell_risk), "risk_stock": sum(p.get("available_quantity", 0) for p in oversell_risk),
                "activate_count": len(activate), "activate_stock": sum(p.get("_bm_total", 0) for p in activate),
            })
        finally:
            await client.close()
    except Exception:
        pass  # Background task — no debe romper nada


async def _get_bm_stock_cached(products: list, sku_key="sku") -> dict:
    """Devuelve {sku: {mty, cdmx, tj, total, avail_total}} para products, con cache.
    Usa Warehouse endpoint para desglose MTY/CDMX/TJ y InventoryBySKUAndCondicion_Quantity
    para stock verdaderamente disponible (Available, excluyendo reservados).
    """
    import httpx
    BM_WH_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
    BM_AVAIL_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"

    result_map = {}
    to_fetch = []
    for p in products:
        sku = p.get(sku_key, "")
        if not sku:
            continue
        cached = _bm_stock_cache.get(sku.upper())
        if cached and (_time.time() - cached[0]) < _BM_CACHE_TTL:
            result_map[sku] = cached[1]
        else:
            to_fetch.append(sku)

    # También incluir SKUs de variaciones para calcular BM correcto por variación
    seen_skus = set(s.upper() for s in to_fetch)
    seen_skus.update(s.upper() for s in result_map)
    for p in products:
        if not p.get("has_variations"):
            continue
        for v in p.get("variations", []):
            v_sku = v.get("sku", "")
            if not v_sku or v_sku.upper() in seen_skus:
                continue
            cached = _bm_stock_cache.get(v_sku.upper())
            if cached and (_time.time() - cached[0]) < _BM_CACHE_TTL:
                result_map[v_sku] = cached[1]
            else:
                to_fetch.append(v_sku)
            seen_skus.add(v_sku.upper())

    if not to_fetch:
        return result_map

    _EMPTY_BM = {"mty": 0, "cdmx": 0, "tj": 0, "total": 0, "avail_total": 0}

    def _parse_wh_rows(rows):
        """Suma QtyTotal por almacen. Retorna (mty, cdmx, tj)."""
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

    def _store_wh(sku, rows_wh, avail_rows=None):
        """Parsea filas del Warehouse endpoint (MTY/CDMX/TJ) y Condition endpoint (Available) y cachea."""
        mty, cdmx, tj = _parse_wh_rows(rows_wh)
        # avail_rows es de InventoryBySKUAndCondicion_Quantity → suma campo Available por condición
        avail_total = sum(row.get("Available", 0) or 0 for row in (avail_rows or []))
        inv = {"mty": mty, "cdmx": cdmx, "tj": tj, "total": mty + cdmx,
               "avail_total": avail_total}
        _bm_stock_cache[sku.upper()] = (_time.time(), inv)
        if inv["total"] > 0:
            result_map[sku] = inv
        return inv["total"] > 0

    def _store_empty(sku):
        _bm_stock_cache[sku.upper()] = (_time.time(), _EMPTY_BM)

    wh_sem = asyncio.Semaphore(20)

    async def _wh_phase(sku, http):
        """Consulta en paralelo:
        1) Warehouse endpoint (ForInventory:0) → MTY/CDMX/TJ breakdown (totales físicos)
        2) InventoryBySKUAndCondicion_Quantity → stock realmente disponible (Available),
           excluyendo unidades reservadas para órdenes pendientes.
        """
        clean = _clean_sku_for_bm(sku)
        if not clean:
            _store_empty(sku)
            return
        base = _extract_base_sku(clean)
        conditions = _bm_conditions_for_sku(clean)
        wh_payload = {
            "COMPANYID": 1, "SKU": base, "WarehouseID": None,
            "LocationID": "47,62,68", "BINID": None,
            "Condition": conditions, "SUPPLIERS": None, "ForInventory": 0,
        }
        avail_payload = {
            "COMPANYID": 1, "TYPEINVENTORY": 0, "WAREHOUSEID": None,
            "LOCATIONID": "47,62,68", "BINID": None,
            "PRODUCTSKU": base, "CONDITION": conditions,
            "SUPPLIERS": None, "LCN": None, "SEARCH": base,
        }
        async with wh_sem:
            try:
                r_wh, r_avail = await asyncio.gather(
                    http.post(BM_WH_URL, json=wh_payload, timeout=15.0),
                    http.post(BM_AVAIL_URL, json=avail_payload, timeout=15.0),
                    return_exceptions=True,
                )
                rows_wh = r_wh.json() if not isinstance(r_wh, Exception) and r_wh.status_code == 200 else []
                avail_rows = r_avail.json() if not isinstance(r_avail, Exception) and r_avail.status_code == 200 else []
                _store_wh(sku, rows_wh, avail_rows)
                return
            except Exception:
                pass
        _store_empty(sku)

    async with httpx.AsyncClient(timeout=30.0) as http:
        await asyncio.gather(
            *[_wh_phase(s, http) for s in to_fetch],
            return_exceptions=True
        )

    return result_map


def _apply_bm_stock(products: list, bm_map: dict, sku_key="sku"):
    """Aplica datos de stock BM a la lista de productos (total/reserve + available)."""
    for p in products:
        if p.get("has_variations"):
            # Para items con variaciones: sumar BM de cada variación individual (si tienen SKU propio)
            tot_mty = tot_cdmx = tot_tj = tot_avail = 0
            any_var_sku = False
            for v in p.get("variations", []):
                v_sku = v.get("sku", "")
                if v_sku:
                    any_var_sku = True
                inv = bm_map.get(v_sku) if v_sku else None
                v["_bm_total"] = inv["total"] if inv else 0
                v["_bm_avail"] = inv.get("avail_total", 0) if inv else 0
                if inv:
                    tot_mty += inv["mty"]
                    tot_cdmx += inv["cdmx"]
                    tot_tj += inv["tj"]
                    tot_avail += inv.get("avail_total", 0)
            if any_var_sku:
                # Variaciones con SKU individual → usar suma de sus BMs
                p["_bm_mty"] = tot_mty
                p["_bm_cdmx"] = tot_cdmx
                p["_bm_tj"] = tot_tj
                p["_bm_total"] = tot_mty + tot_cdmx
                p["_bm_avail"] = tot_avail
            else:
                # Variaciones sin SKU individual → fallback al SKU padre
                inv = bm_map.get(p.get(sku_key))
                if inv:
                    p["_bm_mty"] = inv["mty"]
                    p["_bm_cdmx"] = inv["cdmx"]
                    p["_bm_tj"] = inv["tj"]
                    p["_bm_total"] = inv["total"]
                    p["_bm_avail"] = inv.get("avail_total", 0)
        else:
            inv = bm_map.get(p.get(sku_key))
            if inv:
                p["_bm_mty"] = inv["mty"]
                p["_bm_cdmx"] = inv["cdmx"]
                p["_bm_tj"] = inv["tj"]
                p["_bm_total"] = inv["total"]
                p["_bm_avail"] = inv.get("avail_total", 0)


def _enrich_sku_from_orders(products: list, orders: list):
    """Enriquece SKU de productos desde ordenes (fallback para items sin SKU en datos)."""
    sku_map = {}
    for order in orders:
        for oi in order.get("order_items", []):
            it = oi.get("item", {})
            raw_sku = it.get("seller_sku") or it.get("seller_custom_field") or ""
            if raw_sku and it.get("id"):
                base = raw_sku.split("+")[0].strip()
                existing = sku_map.get(it["id"], "")
                if not existing or len(base) < len(existing):
                    sku_map[it["id"]] = base
    for p in products:
        if not p.get("sku") and p["id"] in sku_map:
            p["sku"] = sku_map[p["id"]]


def _get_var_sku(v: dict) -> str:
    """Extrae SKU de una variación (seller_custom_field o SELLER_SKU attribute)."""
    v_sku = v.get("seller_custom_field") or ""
    if not v_sku or v_sku == "None":
        v_sku = ""
        for va in v.get("attributes", []):
            if va.get("id") == "SELLER_SKU" and va.get("value_name"):
                v_sku = va["value_name"]
                break
    return v_sku


def _build_product_list(bodies: list, sales_map: dict = None) -> list[dict]:
    """Construye lista de productos desde item bodies con SKU y ventas."""
    products = []
    for body in bodies:
        iid = body.get("id", "")
        if not iid:
            continue
        sku = _get_item_sku(body)
        shipping = body.get("shipping", {})

        # Para items con variaciones, usar el stock de la variacion especifica del SKU,
        # no el total del item (que suma todas las variaciones).
        # Ejemplo: SHIL000286 (Dorado=0), SHIL000287 (Negro=10), SHIL000288 (Plateado=34)
        # item.available_quantity=44 (suma), pero SHIL000286 tiene 0.
        raw_vars = body.get("variations", [])
        avail_qty = body.get("available_quantity", 0)
        if raw_vars and sku:
            for v in raw_vars:
                v_sku = _get_var_sku(v)
                if v_sku and v_sku.upper() == sku.upper():
                    avail_qty = v.get("available_quantity", 0)
                    break

        p = {
            "id": iid,
            "title": body.get("title", ""),
            "thumbnail": body.get("thumbnail", ""),
            "price": body.get("price", 0),
            "original_price": body.get("original_price"),
            "available_quantity": avail_qty,
            "sku": sku,
            "permalink": body.get("permalink", ""),
            "pictures_count": len(body.get("pictures", [])),
            "has_video": body.get("video_id") is not None,
            "category_id": body.get("category_id", ""),
            "status": body.get("status", "active"),
            "is_full": shipping.get("logistic_type", "") == "fulfillment",
        }
        if sales_map:
            s = sales_map.get(iid, {"units": 0, "revenue": 0, "fees": 0})
            p["units"] = s["units"]
            p["units_30d"] = s["units"]
            p["revenue"] = s["revenue"]
            p["revenue_30d"] = s["revenue"]
            p["fees"] = s.get("fees", 0)
        else:
            p["units_30d"] = 0
            p["revenue_30d"] = 0

        # Extraer variaciones si hay mas de 1
        if len(raw_vars) > 1:
            variations = []
            for v in raw_vars:
                v_sku = _get_var_sku(v)
                combos = []
                for ac in v.get("attribute_combinations", []):
                    combos.append(f"{ac.get('name', '')}: {ac.get('value_name', '')}")
                variations.append({
                    "id": v.get("id", ""),
                    "sku": v_sku,
                    "stock": v.get("available_quantity", 0),
                    "price": v.get("price", p["price"]),
                    "combo": ", ".join(combos) if combos else f"Var {v.get('id', '')}",
                })
            p["variations"] = variations
            p["has_variations"] = True

        products.append(p)
    return products


async def _enrich_category_names(client, products: list):
    """Resuelve category_id -> nombre legible. Usa cache permanente."""
    cat_ids = set()
    for p in products:
        cid = p.get("category_id", "")
        if cid and cid not in _category_cache:
            cat_ids.add(cid)
    if cat_ids:
        sem = asyncio.Semaphore(10)
        async def _fetch_cat(cid):
            async with sem:
                try:
                    data = await client.get(f"/categories/{cid}")
                    return cid, data.get("name", cid)
                except Exception:
                    return cid, cid
        results = await asyncio.gather(*[_fetch_cat(c) for c in cat_ids])
        for cid, name in results:
            _category_cache[cid] = name
    for p in products:
        cid = p.get("category_id", "")
        p["category_name"] = _category_cache.get(cid, cid)


async def _enrich_variation_skus(client, products: list):
    """Para items con variaciones sin SKU, fetcha datos individuales para obtener SELLER_SKU."""
    # Identificar items que tienen variaciones con SKU vacios
    needs_fetch = []
    for p in products:
        vars_list = p.get("variations")
        if not vars_list:
            continue
        has_empty = any(not v.get("sku") for v in vars_list)
        if has_empty:
            needs_fetch.append(p["id"])

    if not needs_fetch:
        return

    # Limitar a 20 items para no sobrecargar
    needs_fetch = needs_fetch[:20]
    sem = asyncio.Semaphore(5)

    async def _fetch(item_id):
        async with sem:
            try:
                data = await client.get(f"/items/{item_id}")
                return item_id, data
            except Exception:
                return item_id, None

    results = await asyncio.gather(*[_fetch(iid) for iid in needs_fetch], return_exceptions=True)

    # Mapear variaciones actualizadas
    var_sku_map = {}  # {item_id: {var_id: sku}}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if not data or not isinstance(data, dict):
            continue
        for v in data.get("variations", []):
            vid = v.get("id")
            if not vid:
                continue
            v_sku = v.get("seller_custom_field") or ""
            if not v_sku or v_sku == "None":
                v_sku = ""
                for va in v.get("attributes", []):
                    if va.get("id") == "SELLER_SKU" and va.get("value_name"):
                        v_sku = va["value_name"]
                        break
            if v_sku:
                var_sku_map.setdefault(iid, {})[str(vid)] = v_sku

    # Aplicar SKUs encontrados
    for p in products:
        iid = p["id"]
        if iid not in var_sku_map:
            continue
        for v in p.get("variations", []):
            if not v.get("sku"):
                v["sku"] = var_sku_map[iid].get(str(v["id"]), "")


# ---------- Product Intelligence tabs ----------

@app.post("/api/cache/invalidate-products")
async def invalidate_products_cache():
    """Invalida cache de productos para forzar re-fetch desde MeLi."""
    cleared = 0
    for cache in (_products_cache, _orders_cache, _sale_price_cache):
        cleared += len(cache)
        cache.clear()
    return {"ok": True, "cleared": cleared}


@app.get("/partials/products-inventory", response_class=HTMLResponse)
async def products_inventory_partial(
    request: Request,
    preset: str = "all",
    search: str = "",
    sort_by: str = "",
    enrich: str = "basic",
    full_filter: str = "all",
    page: int = 1,
    per_page: int = 20,
    alert_days: int = 30,
):
    """Tab Inventario unificado: reemplaza all/top/stock/low/full."""
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        days = max(7, min(alert_days, 90))
        date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Fase 1: fetch paralelo (products + orders)
        all_bodies, all_orders = await asyncio.gather(
            _get_all_products_cached(client, include_all=True),
            _get_orders_cached(client, date_from, date_to),
        )
        sales_map = _aggregate_sales_by_item(all_orders)
        products = _build_product_list(all_bodies, sales_map)
        _enrich_sku_from_orders(products, all_orders)

        # --- Apply CACHED BM stock (instant, no API calls) ---
        # Only use whatever is already in the BM cache from prewarm/previous loads
        for p in products:
            sku = p.get("sku", "")
            if sku:
                cached = _bm_stock_cache.get(sku.upper())
                if cached and (_time.time() - cached[0]) < _BM_CACHE_TTL:
                    data = cached[1]
                    p["_bm_total"] = data.get("total", 0)
                    p["_bm_mty"] = data.get("mty", 0)
                    p["_bm_cdmx"] = data.get("cdmx", 0)
                    p["_bm_tj"] = data.get("tj", 0)
                    p["_bm_avail"] = data.get("avail_total", 0)
                    p["_bm_has_data"] = True

        # Recomendaciones
        for p in products:
            recs = []
            if p["pictures_count"] < 6:
                recs.append("Agregar mas fotos (minimo 8)")
            if not p["has_video"]:
                recs.append("Agregar video al listado")
            if p["available_quantity"] == 0:
                recs.append("Reponer stock para no perder posicionamiento")
            if p.get("units", 0) == 0 and p["available_quantity"] > 0:
                recs.append("Candidato para deal/promocion")
            p["recommendations"] = recs

        # --- Stock alerts from cached BM data (no waiting) ---
        stock_alerts = [
            p for p in products
            if p.get("units", 0) > 0
            and p.get("available_quantity", 0) == 0
            and (p.get("_bm_total") or 0) > 0
            and p.get("id") not in _synced_alert_items
        ]
        stock_alerts.sort(key=lambda x: x.get("units", 0), reverse=True)

        # --- Filtrado por preset ---
        if preset == "top":
            products = [p for p in products if p.get("units", 0) > 0]
        elif preset == "stock":
            products = [p for p in products if p.get("_bm_total", 0) > 0]
        elif preset == "low":
            products = [p for p in products if p.get("units", 0) <= 2]
        elif preset == "full":
            products = [p for p in products if p.get("is_full")]
        elif preset == "no_stock":
            products = [p for p in products if p.get("available_quantity", 0) == 0]

        # Filtro FULL adicional
        if full_filter == "full":
            products = [p for p in products if p.get("is_full")]
        elif full_filter == "not_full":
            products = [p for p in products if not p.get("is_full")]

        # Busqueda por texto (incluye SKUs de variaciones)
        # Ademas busca directamente en MeLi API para encontrar items que no esten en cache
        if search and search.strip():
            q = search.strip().lower()

            # 1. Busqueda directa en MeLi API por seller_sku y keyword
            existing_ids = {p["id"] for p in products}
            extra_ids = set()
            try:
                # Buscar por seller_sku (mas preciso)
                sku_results = await client.get(
                    f"/users/{client.user_id}/items/search",
                    params={"seller_sku": search.strip(), "limit": 50},
                )
                for rid in sku_results.get("results", []):
                    if rid not in existing_ids:
                        extra_ids.add(rid)
                # Buscar por keyword (titulo, etc)
                kw_results = await client.get(
                    f"/users/{client.user_id}/items/search",
                    params={"q": search.strip(), "limit": 50},
                )
                for rid in kw_results.get("results", []):
                    if rid not in existing_ids:
                        extra_ids.add(rid)
            except Exception:
                pass

            # 2. Fetch detalles de items extra encontrados
            if extra_ids:
                extra_list = list(extra_ids)
                sem = asyncio.Semaphore(5)
                async def _fbatch(ids):
                    async with sem:
                        try:
                            return await client.get_items_details(ids)
                        except Exception:
                            return []
                batches = [extra_list[i:i+20] for i in range(0, len(extra_list), 20)]
                batch_results = await asyncio.gather(*[_fbatch(b) for b in batches])
                extra_bodies = [b for br in batch_results for b in br]
                extra_products = _build_product_list(extra_bodies, sales_map)
                _enrich_sku_from_orders(extra_products, all_orders)
                # Aplicar BM cache
                for p in extra_products:
                    sku_val = p.get("sku", "")
                    if sku_val:
                        cached = _bm_stock_cache.get(sku_val.upper())
                        if cached and (_time.time() - cached[0]) < _BM_CACHE_TTL:
                            bm_data = cached[1]
                            p["_bm_total"] = bm_data.get("total", 0)
                            p["_bm_mty"] = bm_data.get("mty", 0)
                            p["_bm_cdmx"] = bm_data.get("cdmx", 0)
                            p["_bm_tj"] = bm_data.get("tj", 0)
                            p["_bm_avail"] = bm_data.get("avail_total", 0)
                            p["_bm_has_data"] = True
                products.extend(extra_products)
                existing_ids.update(extra_ids)

            # 3. Filtrar por texto
            def _matches(p):
                if q in p.get("id", "").lower():
                    return True
                if q in (p.get("sku") or "").lower():
                    return True
                if q in p.get("title", "").lower():
                    return True
                for v in p.get("variations", []):
                    if q in (v.get("sku") or "").lower():
                        return True
                return False
            products = [p for p in products if _matches(p)]

        total_count = len(products)

        # Ordenamiento
        if not sort_by:
            sort_by = {
                "top": "units_desc",
                "stock": "bm_desc",
                "low": "stock_desc",
                "full": "stock_desc",
                "no_stock": "units_desc",
            }.get(preset, "stock_desc")

        field, direction = (sort_by.rsplit("_", 1) + ["desc"])[:2]
        reverse = direction == "desc"
        sort_keys = {
            "stock": lambda p: p.get("available_quantity", 0),
            "units": lambda p: p.get("units", 0),
            "bm": lambda p: p.get("_bm_total", 0),
            "price": lambda p: p.get("price", 0),
            "revenue": lambda p: p.get("revenue", 0),
            "margin": lambda p: p.get("_margen_pct") if p.get("_margen_pct") is not None else -999,
            "photos": lambda p: p.get("pictures_count", 0),
        }
        products.sort(key=sort_keys.get(field, sort_keys["stock"]), reverse=reverse)

        # --- Pagination ---
        from math import ceil
        if per_page <= 0:
            per_page = 20
        if per_page >= total_count:
            total_pages = 1
            page = 1
            page_products = products
        else:
            total_pages = max(1, ceil(total_count / per_page))
            page = max(1, min(page, total_pages))
            start = (page - 1) * per_page
            page_products = products[start:start + per_page]

        # --- Trigger background BM prewarm for ALL products (non-blocking) ---
        # This fills the cache gradually so subsequent pages load with BM data
        _bg_key = f"bm_bg:{client.user_id}"
        if _bg_key not in _bm_stock_cache:
            _bm_stock_cache[_bg_key] = (_time.time(), {})
            asyncio.ensure_future(_get_bm_stock_cached(products))

        # --- Enrich ONLY page products (BM fresh + sale_price + variations) ---
        usd_to_mxn = 0.0
        page_ids = [p["id"] for p in page_products]
        enrichment_tasks = [
            _get_bm_stock_cached(page_products),
            _get_sale_prices_cached(client, page_ids),
            _enrich_variation_skus(client, page_products),
        ]
        if enrich == "full":
            enrichment_tasks.append(_enrich_with_bm_product_info(page_products))
            enrichment_tasks.append(_get_usd_to_mxn(client))

        enrich_results = await asyncio.gather(*enrichment_tasks)
        bm_map = enrich_results[0]
        sale_prices = enrich_results[1]
        # enrich_results[2] = variation SKUs (side-effect, no return needed)

        _apply_bm_stock(page_products, bm_map)

        if enrich == "full":
            usd_to_mxn = enrich_results[4] if len(enrich_results) > 4 else 0.0
            _calc_margins(page_products, usd_to_mxn)

        for p in page_products:
            sp = sale_prices.get(p["id"])
            if sp:
                p["original_price"] = sp["regular_amount"]
                p["price"] = sp["amount"]
                p["_has_deal"] = True
            else:
                p["_has_deal"] = False

        # --- Enrich alerts with estimated lost revenue ---
        for a in stock_alerts:
            units = a.get("units", 0)
            price = a.get("price", 0)
            d = alert_days or 30
            daily_avg = units / d if d > 0 else 0
            a["_alert_units"] = units
            a["_alert_revenue"] = round(units * price, 0)
            a["_est_lost_revenue"] = round(daily_avg * price * d * 0.5, 0)
        stock_alerts.sort(key=lambda x: x.get("_est_lost_revenue", 0), reverse=True)

        return templates.TemplateResponse("partials/products_inventory.html", {
            "request": request,
            "products": page_products,
            "preset": preset,
            "search": search,
            "sort_by": sort_by,
            "enrich": enrich,
            "full_filter": full_filter,
            "total_count": total_count,
            "usd_to_mxn": round(usd_to_mxn, 2),
            "stock_alerts": stock_alerts,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "alert_days": alert_days,
        })
    finally:
        await client.close()


# --- Mark alert item as synced (evita duplicar trabajo entre usuarios) ---

@app.post("/partials/mark-synced/{item_id}")
async def mark_alert_synced(item_id: str):
    """Registra un item como sincronizado; se excluye de alertas hasta que la cache expire."""
    _synced_alert_items.add(item_id)
    return Response(status_code=204)


# --- Legacy redirects (old tabs → new inventory endpoint) ---

@app.get("/partials/products-all", response_class=HTMLResponse)
async def products_all_partial(request: Request):
    return await products_inventory_partial(request, preset="all")


@app.get("/partials/products-summary", response_class=HTMLResponse)
async def products_summary_partial(request: Request):
    """KPIs — ultra rapido: solo ordenes (cached) + item count."""
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Ordenes (cached despues del 1er fetch) + item count: 2 calls en paralelo
        all_orders, items_data = await asyncio.gather(
            client.fetch_all_orders(date_from=date_from, date_to=date_to),
            client.get_items(limit=1, status="active"),
        )

        total_active = items_data.get("paging", {}).get("total", 0)
        sales_map = _aggregate_sales_by_item(all_orders)
        paid_orders = [o for o in all_orders if o.get("status") in ("paid", "delivered")]
        total_orders = len(paid_orders)
        total_units = sum(s["units"] for s in sales_map.values())
        revenue_30d = sum(s["revenue"] for s in sales_map.values())
        avg_ticket = revenue_30d / total_orders if total_orders > 0 else 0
        products_with_sales = len(sales_map)
        products_no_sales = max(0, total_active - products_with_sales)
        unique_skus = len(set(
            _extract_base_sku(
                oi.get("item", {}).get("seller_sku") or
                oi.get("item", {}).get("seller_custom_field") or "SIN_SKU"
            )
            for o in paid_orders
            for oi in o.get("order_items", [])
        ))

        # Top 5 desde order data (titulo ya viene en order_items, 0 API calls extras)
        top_ids = sorted(sales_map.keys(), key=lambda x: sales_map[x]["units"], reverse=True)[:5]
        # Construir top con titulos de las ordenes
        title_map = {}
        for o in paid_orders:
            for oi in o.get("order_items", []):
                item = oi.get("item", {})
                iid = item.get("id", "")
                if iid and iid not in title_map:
                    title_map[iid] = {
                        "title": item.get("title", iid),
                        "thumbnail": item.get("variation_attributes", [{}])[0].get("value_name", "") if item.get("variation_attributes") else "",
                    }

        top_products = []
        for iid in top_ids:
            info = title_map.get(iid, {})
            s = sales_map[iid]
            top_products.append({
                "title": info.get("title", iid),
                "thumbnail": "",  # No gastar API call en thumbnails
                "units": s["units"],
                "revenue": s["revenue"],
            })

        return templates.TemplateResponse("partials/products_summary.html", {
            "request": request,
            "revenue_30d": revenue_30d,
            "total_units": total_units,
            "total_orders": total_orders,
            "total_active": total_active,
            "products_with_sales": products_with_sales,
            "products_no_sales": products_no_sales,
            "unique_skus": unique_skus,
            "avg_ticket": avg_ticket,
            "top_products": top_products,
        })
    finally:
        await client.close()


@app.get("/partials/products-top-sellers", response_class=HTMLResponse)
async def products_top_sellers_partial(request: Request):
    return await products_inventory_partial(request, preset="top", enrich="full")


@app.get("/partials/products-high-stock", response_class=HTMLResponse)
async def products_high_stock_partial(request: Request):
    return await products_inventory_partial(request, preset="stock", enrich="full")


@app.get("/partials/products-low-sellers", response_class=HTMLResponse)
async def products_low_sellers_partial(request: Request):
    return await products_inventory_partial(request, preset="low")


@app.get("/partials/products-full", response_class=HTMLResponse)
async def products_full_partial(request: Request, stock_filter: str = "all"):
    return await products_inventory_partial(request, preset="full", enrich="full", full_filter=stock_filter)


@app.get("/partials/products-deals", response_class=HTMLResponse)
async def products_deals_partial(request: Request):
    """Deals: detecta deals por original_price + pricing con FX rate, margenes y ventas."""
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Items + orders en paralelo (ambos cached)
        all_bodies, all_orders = await asyncio.gather(
            _get_all_products_cached(client, include_all=True),
            client.fetch_all_orders(date_from=date_from, date_to=date_to),
        )

        sales_map = _aggregate_sales_by_item(all_orders)
        products = _build_product_list(all_bodies, sales_map)

        # Fase 1: clasificar por original_price del body (rapido, sin API extra)
        active_deals = []
        candidates = []
        for p in products:
            op = p.get("original_price")
            if op and op > p["price"]:
                p["_has_deal"] = True
                active_deals.append(p)
            elif p["available_quantity"] > 0:
                candidates.append(p)

        # Fase 2: verificar deals perdidos via promotions API en candidatos
        # (el body batch a veces no incluye original_price aunque haya deal activo)
        await _enrich_with_promotions(client, candidates, id_key="id")
        newly_found = [p for p in candidates if p.get("_has_deal")]
        if newly_found:
            active_deals.extend(newly_found)
            candidates = [p for p in candidates if not p.get("_has_deal")]

        candidates.sort(key=lambda p: p.get("available_quantity", 0), reverse=True)
        candidates = candidates[:60]

        # BM data + FX rate + variation SKUs en paralelo
        all_to_enrich = active_deals + candidates

        bm_map, _, usd_to_mxn, _, _ = await asyncio.gather(
            _get_bm_stock_cached(all_to_enrich),
            _enrich_with_bm_product_info(all_to_enrich),
            _get_usd_to_mxn(client),
            _enrich_variation_skus(client, all_to_enrich),
            _enrich_category_names(client, all_to_enrich),
        )
        _apply_bm_stock(all_to_enrich, bm_map)
        _calc_margins(all_to_enrich, usd_to_mxn)

        # KPIs resumen
        deals_revenue = sum(p.get("revenue_30d", 0) for p in active_deals)
        deals_units = sum(p.get("units_30d", 0) for p in active_deals)

        # Recomendaciones inteligentes
        recs = []
        # 1. Deals activos con margen negativo — revisar urgente
        neg_margin = [p for p in active_deals if p.get("_margen_pct") is not None and p["_margen_pct"] < 0]
        if neg_margin:
            neg_margin.sort(key=lambda p: p["_margen_pct"])
            recs.append({
                "type": "danger",
                "icon": "!",
                "title": f"{len(neg_margin)} deal(s) con margen negativo",
                "desc": "Estos deals pierden dinero en cada venta. Revisa si conviene desactivarlos o subir el precio.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"Margen {p['_margen_pct']:.1f}%"} for p in neg_margin[:5]],
            })
        # 2. Candidatos con alto stock + cero ventas — urge mover inventario
        high_stock_no_sales = [p for p in candidates if p.get("available_quantity", 0) >= 10 and p.get("units_30d", 0) == 0]
        if high_stock_no_sales:
            high_stock_no_sales.sort(key=lambda p: p["available_quantity"], reverse=True)
            recs.append({
                "type": "warning",
                "icon": "S",
                "title": f"{len(high_stock_no_sales)} producto(s) con alto stock sin ventas",
                "desc": "Mucho inventario parado. Un deal agresivo puede activar la demanda.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"Stock: {p['available_quantity']}"} for p in high_stock_no_sales[:5]],
            })
        # 3. Candidatos con buenas ventas que podrian vender mas con deal
        good_sellers_no_deal = [p for p in candidates if p.get("units_30d", 0) >= 3 and p.get("_margen_pct") is not None and p["_margen_pct"] >= 15]
        if good_sellers_no_deal:
            good_sellers_no_deal.sort(key=lambda p: p["units_30d"], reverse=True)
            recs.append({
                "type": "success",
                "icon": "^",
                "title": f"{len(good_sellers_no_deal)} producto(s) vendiendo bien con buen margen",
                "desc": "Ya venden sin deal y tienen margen para descuento. Un deal los puede catapultar.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"{p['units_30d']} uds, margen {p['_margen_pct']:.0f}%"} for p in good_sellers_no_deal[:5]],
            })
        # 4. Stock BM disponible pero poco stock en MeLi
        bm_available = [p for p in candidates if p.get("_bm_total") is not None and p["_bm_total"] > 20 and p["available_quantity"] <= 5]
        if bm_available:
            bm_available.sort(key=lambda p: p["_bm_total"], reverse=True)
            recs.append({
                "type": "info",
                "icon": "R",
                "title": f"{len(bm_available)} producto(s) con stock BM alto pero poco en MeLi",
                "desc": "Reabastecer MeLi y activar deal para impulsar rotacion.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"BM: {p['_bm_total']}, MeLi: {p['available_quantity']}"} for p in bm_available[:5]],
            })

        # Categorias unicas para filtro
        cat_counts = {}
        for p in candidates:
            cn = p.get("category_name", "")
            if cn:
                cat_counts[cn] = cat_counts.get(cn, 0) + 1
        categories = sorted(cat_counts.keys())

        return templates.TemplateResponse("partials/products_deals.html", {
            "request": request,
            "active_deals": active_deals,
            "candidates": candidates,
            "usd_to_mxn": round(usd_to_mxn, 2),
            "deals_revenue": deals_revenue,
            "deals_units": deals_units,
            "recommendations": recs,
            "categories": categories,
            "cat_counts": cat_counts,
            "total_no_deal_no_sales": len([p for p in candidates if p.get("units_30d", 0) == 0]),
        })
    finally:
        await client.close()


@app.get("/partials/products-not-published", response_class=HTMLResponse)
async def products_not_published_partial(request: Request):
    """SKUs en BM sin listing en MeLi. Usa cache de items."""
    import httpx
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        date_from = (now - timedelta(days=60)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Items (cached) + ordenes (cached) en paralelo
        all_bodies, all_orders = await asyncio.gather(
            _get_all_products_cached(client, include_all=True),
            client.fetch_all_orders(date_from=date_from, date_to=date_to),
        )

        # Recopilar SKUs conocidos
        known_skus = set()
        for body in all_bodies:
            sku = _get_item_sku(body)
            if sku:
                known_skus.add(_extract_base_sku(sku).upper())
        for order in all_orders:
            for oi in order.get("order_items", []):
                item = oi.get("item", {})
                raw_sku = item.get("seller_sku") or item.get("seller_custom_field") or ""
                if raw_sku:
                    known_skus.add(_extract_base_sku(raw_sku).upper())

        if not known_skus:
            return HTMLResponse('<p class="text-center py-8 text-gray-500">No se encontraron SKUs para comparar</p>')

        BM_INV_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
        sem = asyncio.Semaphore(15)

        # Fase 1: BM InventoryReport + exchange rate en paralelo
        async def _fetch_bm_inv(base_sku, http):
            async with sem:
                try:
                    resp = await http.post(BM_INV_URL, json={
                        "COMPANYID": 1,
                        "SEARCH": base_sku,
                        "CONCEPTID": 8,
                        "NUMBERPAGE": 1,
                        "RECORDSPAGE": 10,
                    }, headers={"Content-Type": "application/json"}, timeout=10.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data and isinstance(data, list) and data:
                            for item in data:
                                if item.get("SKU", "").upper() == base_sku.upper():
                                    return base_sku, item
                            return base_sku, data[0]
                except Exception:
                    pass
                return base_sku, None

        async with httpx.AsyncClient() as http:
            inv_tasks = [_fetch_bm_inv(sku, http) for sku in list(known_skus)[:100]]
            inv_results = await asyncio.gather(*inv_tasks, return_exceptions=True)

        bm_products = {}
        for r in inv_results:
            if isinstance(r, Exception) or r is None:
                continue
            base_sku, data = r
            if data:
                bm_products[base_sku] = data

        usd_to_mxn = await _get_usd_to_mxn(client)

        # Fase 2: BM Warehouse endpoint (stock real por almacen)
        BM_WH_URL2 = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"

        async def _check_base_wh(base_sku, http):
            """Consulta Warehouse endpoint para obtener stock real."""
            async with sem:
                try:
                    resp = await http.post(BM_WH_URL2, json={
                        "COMPANYID": 1, "SKU": base_sku, "WarehouseID": None,
                        "LocationID": "47,62,68", "BINID": None,
                        "Condition": _bm_conditions_for_sku(base_sku), "ForInventory": 0, "SUPPLIERS": None,
                    }, timeout=10.0)
                    if resp.status_code == 200:
                        rows = resp.json() or []
                        mty = cdmx = tj = 0
                        for row in rows:
                            qty = row.get("QtyTotal", 0) or 0
                            wname = (row.get("WarehouseName") or "").lower()
                            if "monterrey" in wname or "maxx" in wname:
                                mty += qty
                            elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                                cdmx += qty
                            else:
                                tj += qty
                        total = mty + cdmx
                        if total > 0:
                            return base_sku, {"mty": mty, "cdmx": cdmx, "tj": tj, "total": total}
                except Exception:
                    pass
                return base_sku, None

        async with httpx.AsyncClient() as http:
            ff_tasks = [_check_base_wh(sku, http) for sku in bm_products.keys()]
            ff_results = await asyncio.gather(*ff_tasks, return_exceptions=True)

        skus_with_stock = {}
        for r in ff_results:
            if isinstance(r, Exception) or r is None:
                continue
            base_sku, stock_data = r
            if stock_data:
                skus_with_stock[base_sku.upper()] = stock_data

        # Fase 3: Verificar cuales NO estan en MeLi
        # Buscar por SKU completo (con sufijo) y tambien por base SKU
        async def _check_meli(sku):
            async with sem:
                try:
                    result = await client.get(
                        f"/users/{client.user_id}/items/search",
                        params={"seller_sku": sku, "limit": 1}
                    )
                    if result.get("results"):
                        return sku, True
                    # Fallback: buscar por base SKU (sin sufijo)
                    base = _extract_base_sku(sku)
                    if base != sku:
                        result2 = await client.get(
                            f"/users/{client.user_id}/items/search",
                            params={"seller_sku": base, "limit": 1}
                        )
                        if result2.get("results"):
                            return sku, True
                    return sku, False
                except Exception:
                    return sku, False

        meli_checks = await asyncio.gather(
            *[_check_meli(sku) for sku in list(skus_with_stock.keys())[:100]],
            return_exceptions=True
        )

        meli_published = set()
        for r in meli_checks:
            if isinstance(r, Exception) or r is None:
                continue
            sku, found = r
            if found:
                meli_published.add(sku)

        not_published = []
        for sku_upper, stock_data in skus_with_stock.items():
            if sku_upper in meli_published:
                continue
            base = _extract_base_sku(sku_upper)
            bm_info = bm_products.get(base, {})
            retail_price = bm_info.get("RetailPrice", 0) or 0
            avg_cost = bm_info.get("AvgCostQTY", 0) or 0
            if retail_price > 0 and retail_price < 9999:
                estimated_price = round(retail_price * usd_to_mxn * 1.16, 0)
            elif avg_cost > 0 and avg_cost < 9999:
                estimated_price = round(avg_cost * usd_to_mxn * 2 * 1.16, 0)
            else:
                estimated_price = 0
            not_published.append({
                "sku": sku_upper,
                "base_sku": base,
                "title": bm_info.get("Title", ""),
                "brand": bm_info.get("Brand", ""),
                "retail_price_usd": retail_price,
                "avg_cost_usd": avg_cost,
                "estimated_price_mxn": estimated_price,
                "mty": stock_data["mty"],
                "cdmx": stock_data["cdmx"],
                "tj": stock_data["tj"],
                "total_stock": stock_data["total"],
            })

        not_published.sort(key=lambda x: x["total_stock"], reverse=True)

        return templates.TemplateResponse("partials/products_not_published.html", {
            "request": request,
            "products": not_published,
            "usd_to_mxn": usd_to_mxn,
        })
    finally:
        await client.close()


# ---------- Health helpers ----------

_MELI_THRESHOLDS = {
    "claims":        {"green": 0.02, "yellow": 0.04, "red": 0.07},
    "cancellations": {"green": 0.025, "yellow": 0.05, "red": 0.09},
    "delays":        {"green": 0.15, "yellow": 0.20, "red": 0.30},
}


def _metric_status(rate: float, key: str) -> str:
    t = _MELI_THRESHOLDS[key]
    if rate < t["green"]:
        return "green"
    elif rate < t["yellow"]:
        return "yellow"
    elif rate < t["red"]:
        return "orange"
    return "red"


_STATUS_LABELS = {"green": "Excelente", "yellow": "Atencion", "orange": "Riesgo", "red": "Critico"}


def _compute_health_score(claims_rate: float, cancel_rate: float, delay_rate: float,
                           open_claims: int, unanswered_q: int) -> int:
    """Compute a composite health score 0-100.
    Weights: claims 30%, cancellations 20%, delays 20%, open_claims 15%, questions 15%.
    Each sub-score is 100 when perfect and 0 when at/above red threshold."""
    t = _MELI_THRESHOLDS
    def _rate_score(rate, key):
        red = t[key]["red"]
        if rate <= 0:
            return 100
        if rate >= red:
            return 0
        return max(0, round((1 - rate / red) * 100))

    s_claims = _rate_score(claims_rate, "claims")
    s_cancel = _rate_score(cancel_rate, "cancellations")
    s_delays = _rate_score(delay_rate, "delays")
    # Open claims: 0 = 100, 5+ = 0
    s_open = max(0, round((1 - min(open_claims, 5) / 5) * 100))
    # Unanswered questions: 0 = 100, 10+ = 0
    s_unans = max(0, round((1 - min(unanswered_q, 10) / 10) * 100))

    score = round(s_claims * 0.30 + s_cancel * 0.20 + s_delays * 0.20 + s_open * 0.15 + s_unans * 0.15)
    return max(0, min(100, score))


def _classify_question(text: str) -> str:
    """Classify a question by type based on keywords."""
    t = (text or "").lower()
    if any(w in t for w in ["envio", "envío", "llega", "demora", "entrega", "shipping", "despacho", "tarda"]):
        return "envio"
    if any(w in t for w in ["stock", "disponible", "queda", "hay", "tienen", "unidades"]):
        return "stock"
    if any(w in t for w in ["compatible", "sirve para", "funciona con", "modelo", "medida", "tamaño", "talla"]):
        return "compatibilidad"
    if any(w in t for w in ["precio", "descuento", "oferta", "costo", "vale", "barato", "rebaja", "promocion"]):
        return "precio"
    if any(w in t for w in ["garantia", "garantía", "devolucion", "devolución", "cambio"]):
        return "garantia"
    if any(w in t for w in ["factura", "fiscal", "iva", "cfdi", "boleta"]):
        return "factura"
    return "general"


_QUESTION_TYPE_LABELS = {
    "envio": {"label": "Envio", "color": "bg-blue-100 text-blue-700"},
    "stock": {"label": "Stock", "color": "bg-green-100 text-green-700"},
    "compatibilidad": {"label": "Compat.", "color": "bg-purple-100 text-purple-700"},
    "precio": {"label": "Precio", "color": "bg-yellow-100 text-yellow-700"},
    "garantia": {"label": "Garantia", "color": "bg-orange-100 text-orange-700"},
    "factura": {"label": "Factura", "color": "bg-gray-100 text-gray-700"},
    "general": {"label": "General", "color": "bg-gray-100 text-gray-600"},
}

_QUESTION_TEMPLATES = {
    "envio": [
        "El envio se realiza por Mercado Envios. Una vez despachado, recibiras el numero de seguimiento para rastrear tu paquete.",
        "Los tiempos de entrega dependen de tu ubicacion. Puedes ver la fecha estimada antes de comprar.",
    ],
    "stock": [
        "Si, tenemos stock disponible. Puedes comprarlo directamente.",
        "Por el momento no tenemos stock. Te recomiendo agregar a favoritos para que te notifique cuando este disponible.",
    ],
    "compatibilidad": [
        "Este producto es compatible con los modelos indicados en la descripcion. Revisa la ficha tecnica para confirmar.",
        "Verificamos que es compatible. Puedes comprarlo con confianza.",
    ],
    "precio": [
        "El precio publicado es el precio final. Incluye envio gratis si tu compra supera el monto minimo.",
        "Por el momento no manejamos descuentos adicionales, pero el precio ya es competitivo.",
    ],
    "garantia": [
        "El producto cuenta con garantia del vendedor. Si tienes algun problema, puedes iniciar un reclamo desde tu compra.",
        "Ofrecemos devolucion gratis dentro de los 30 dias de recibido el producto.",
    ],
    "factura": [
        "Emitimos factura. Una vez realizada la compra, solicita la factura por mensaje y te la enviamos.",
        "La factura se genera automaticamente y la puedes descargar desde tu compra en MercadoLibre.",
    ],
    "general": [
        "Gracias por tu pregunta. Quedamos a tu disposicion para cualquier duda adicional.",
    ],
}


def _compute_metric_margin(rate: float, key: str) -> dict:
    """Compute how far the current rate is from each threshold and remaining margin."""
    t = _MELI_THRESHOLDS[key]
    status = _metric_status(rate, key)
    # How many percentage points until next worse threshold
    if rate < t["green"]:
        next_threshold = t["green"]
        margin_pct = round((next_threshold - rate) * 100, 2)
        margin_label = f"{margin_pct}pp antes de amarillo"
    elif rate < t["yellow"]:
        next_threshold = t["yellow"]
        margin_pct = round((next_threshold - rate) * 100, 2)
        margin_label = f"{margin_pct}pp antes de naranja"
    elif rate < t["red"]:
        next_threshold = t["red"]
        margin_pct = round((next_threshold - rate) * 100, 2)
        margin_label = f"{margin_pct}pp antes de rojo"
    else:
        margin_pct = 0
        margin_label = "En zona critica"
    # Position as percentage of the gauge (0-100 scale where red threshold = ~90%)
    max_val = t["red"] * 1.2  # give some space beyond red
    gauge_position = min(100, round((rate / max_val) * 100)) if max_val > 0 else 0
    return {
        "status": status,
        "label": _STATUS_LABELS[status],
        "margin_pct": margin_pct,
        "margin_label": margin_label,
        "gauge_position": gauge_position,
        "green_end": round((t["green"] / max_val) * 100),
        "yellow_end": round((t["yellow"] / max_val) * 100),
        "red_start": round((t["red"] / max_val) * 100),
    }


def _elapsed_str(iso_date: str) -> tuple:
    """Return (human string, total seconds) from an ISO date to now(UTC)."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            secs = 0
        if secs < 60:
            return (f"hace {secs}s", secs)
        mins = secs // 60
        if mins < 60:
            return (f"hace {mins}m", secs)
        hours = mins // 60
        if hours < 24:
            return (f"hace {hours}h", secs)
        days = hours // 24
        return (f"hace {days}d", secs)
    except Exception:
        return ("-", 0)


@app.get("/partials/health-summary", response_class=HTMLResponse)
async def health_summary_partial(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        errors = []
        df = date_from or None
        dt = date_to or None

        # Parallel fetch: user info + counts
        async def _fetch_questions():
            # Sin filtro de fecha: una pregunta sin responder requiere atencion sin importar cuando fue hecha
            try:
                r = await client.get_questions(status="UNANSWERED", limit=1)
                # MeLi Questions API devuelve 'total' en raiz, no en 'paging'
                return r.get("total", r.get("paging", {}).get("total", 0))
            except Exception:
                return 0

        async def _fetch_claims():
            # Reintentar una vez si falla (API intermitente)
            for attempt in range(2):
                try:
                    r = await client.get_claims(limit=1, status="opened")
                    return r.get("paging", {}).get("total", 0)
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            return 0

        async def _fetch_messages():
            # Cuenta conversaciones con mensajes en ordenes recientes
            try:
                r = await client.get_messages(limit=0)
                return r.get("paging", {}).get("total", 0)
            except Exception:
                return 0

        user_task = asyncio.ensure_future(client.get_user_info())
        q_task = asyncio.ensure_future(_fetch_questions())
        c_task = asyncio.ensure_future(_fetch_claims())
        m_task = asyncio.ensure_future(_fetch_messages())

        user, unanswered_questions, open_claims, unread_messages = await asyncio.gather(
            user_task, q_task, c_task, m_task
        )

        reputation = user.get("seller_reputation", {})
        metrics = reputation.get("metrics", {})

        claims_rate = metrics.get("claims", {}).get("rate", 0) or 0
        cancel_rate = metrics.get("cancellations", {}).get("rate", 0) or 0
        delay_rate = metrics.get("delayed_handling_time", {}).get("rate", 0) or 0

        claims_status = _metric_status(claims_rate, "claims")
        cancel_status = _metric_status(cancel_rate, "cancellations")
        delay_status = _metric_status(delay_rate, "delays")

        claims_value = metrics.get("claims", {}).get("value", 0) or 0
        cancel_value = metrics.get("cancellations", {}).get("value", 0) or 0
        delay_value = metrics.get("delayed_handling_time", {}).get("value", 0) or 0

        urgent_count = open_claims + unanswered_questions

        health_score = _compute_health_score(claims_rate, cancel_rate, delay_rate,
                                              open_claims, unanswered_questions)

        sales_period = metrics.get("claims", {}).get("period", "60 days")
        claims_margin = _compute_metric_margin(claims_rate, "claims")
        cancel_margin = _compute_metric_margin(cancel_rate, "cancellations")
        delay_margin = _compute_metric_margin(delay_rate, "delays")

        summary = SimpleNamespace(
            reputation_level=reputation.get("level_id", "unknown"),
            power_seller_status=reputation.get("power_seller_status", None),
            open_claims=open_claims,
            unanswered_questions=unanswered_questions,
            unread_messages=unread_messages,
            urgent_count=urgent_count,
            health_score=health_score,
            sales_period=sales_period,
            claims_rate=claims_rate,
            claims_pct=round(claims_rate * 100, 2),
            claims_status=claims_status,
            claims_label=_STATUS_LABELS[claims_status],
            claims_value=claims_value,
            claims_margin=claims_margin,
            cancellation_rate=cancel_rate,
            cancellation_pct=round(cancel_rate * 100, 2),
            cancellation_status=cancel_status,
            cancellation_label=_STATUS_LABELS[cancel_status],
            cancellation_value=cancel_value,
            cancel_margin=cancel_margin,
            delayed_rate=delay_rate,
            delayed_pct=round(delay_rate * 100, 2),
            delayed_status=delay_status,
            delayed_label=_STATUS_LABELS[delay_status],
            delayed_value=delay_value,
            delay_margin=delay_margin,
        )

        return templates.TemplateResponse("partials/health_summary.html", {
            "request": request,
            "summary": summary,
            "thresholds": _MELI_THRESHOLDS,
            "errors": errors,
            "date_from": date_from,
            "date_to": date_to,
        })
    finally:
        await client.close()


@app.get("/partials/health-claims", response_class=HTMLResponse)
async def health_claims_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query(""),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    order_id: str = Query("", description="Filter by order/resource ID"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        filter_order_id = order_id.strip() if order_id else ""
        params_status = status if status else None
        df = date_from or None
        dt = date_to or None

        if filter_order_id:
            # Fetch all claims WITHOUT date filter — the claim may predate the active range
            try:
                all_claims = await client.fetch_all_claims(status=params_status,
                                                            date_from=None, date_to=None)
                raw_claims = [c for c in all_claims
                              if str(c.get("resource_id", "")) == filter_order_id]
            except Exception:
                raw_claims = []
            paging = {"total": len(raw_claims), "offset": 0, "limit": len(raw_claims) or limit}
            # Apply offset/limit manually
            raw_claims = raw_claims[offset:offset + limit]
        else:
            try:
                data = await client.get_claims(offset=offset, limit=limit, status=params_status,
                                               date_from=df, date_to=dt)
            except Exception:
                data = {"results": [], "paging": {"total": 0, "offset": 0, "limit": limit}}
            raw_claims = data.get("results", [])
            paging = data.get("paging", {"total": len(raw_claims), "offset": offset, "limit": limit})

        # --- Refresh status of "opened" claims via individual endpoint ---
        # The search API can return stale status; the detail endpoint is authoritative
        opened_ids = [c for c in raw_claims if c.get("status") == "opened"]
        if opened_ids:
            sem_refresh = asyncio.Semaphore(5)

            async def _refresh_status(claim):
                async with sem_refresh:
                    try:
                        detail = await client.get_claim_detail(str(claim.get("id", "")))
                        if isinstance(detail, dict) and detail.get("status"):
                            claim["status"] = detail["status"]
                            if detail.get("stage"):
                                claim["stage"] = detail["stage"]
                            # Also refresh players (for due_date / actions)
                            if detail.get("players"):
                                claim["players"] = detail["players"]
                    except Exception:
                        pass  # Keep original status if detail fetch fails

            await asyncio.gather(*[_refresh_status(c) for c in opened_ids[:30]],
                                 return_exceptions=True)

        # Reason code mapping (PDD = producto defectuoso, PNR = no recibido)
        REASON_MAP = {
            "PNR": ("No recibido", "not_received"),
            "PDD": ("Producto defectuoso/diferente", "defective"),
        }

        # Batch fetch order info for product titles
        order_ids = list({str(c.get("resource_id", "")) for c in raw_claims
                          if c.get("resource") == "order" and c.get("resource_id")})
        orders_map = {}
        if order_ids:
            try:
                for oid in order_ids[:20]:
                    try:
                        order = await client.get(f"/orders/{oid}")
                        oi = order.get("order_items", [])
                        if oi:
                            item = oi[0].get("item", {})
                            orders_map[oid] = {
                                "title": item.get("title", ""),
                                "price": order.get("total_amount", 0),
                                "item_id": item.get("id", ""),
                            }
                    except Exception:
                        pass
            except Exception:
                pass

        # Parallel fetch: claim messages + shipment tracking for opened claims
        opened_claims = [c for c in raw_claims if c.get("status") == "opened"]
        claim_messages_map = {}  # claim_id -> list of messages
        shipment_tracking_map = {}  # order_id -> tracking info

        if opened_claims:
            sem = asyncio.Semaphore(5)

            async def _fetch_claim_msgs(claim_id):
                async with sem:
                    try:
                        msgs = await client.get_claim_messages(str(claim_id))
                        if isinstance(msgs, list):
                            return str(claim_id), msgs
                        return str(claim_id), msgs.get("results", msgs.get("messages", []))
                    except Exception:
                        return str(claim_id), []

            async def _fetch_shipment_tracking(order_id):
                async with sem:
                    try:
                        order = await client.get(f"/orders/{order_id}")
                        ship_id = order.get("shipping", {}).get("id")
                        if ship_id:
                            ship = await client.get_shipment(str(ship_id))
                            return str(order_id), {
                                "status": ship.get("status", ""),
                                "substatus": ship.get("substatus", ""),
                                "tracking_number": ship.get("tracking_number", ""),
                                "tracking_url": ship.get("tracking_url", ""),
                                "carrier": ship.get("logistic_type", ""),
                            }
                    except Exception:
                        pass
                    return str(order_id), {}

            # Fetch messages for up to 20 opened claims
            msg_tasks = [_fetch_claim_msgs(c.get("id", "")) for c in opened_claims[:20]]
            # Fetch tracking for PNR (not received) claims
            pnr_claims = [c for c in opened_claims
                          if (c.get("reason_id", "")[:3] == "PNR") and c.get("resource_id")]
            track_tasks = [_fetch_shipment_tracking(str(c.get("resource_id", ""))) for c in pnr_claims[:20]]

            all_results = await asyncio.gather(*msg_tasks, *track_tasks, return_exceptions=True)
            for r in all_results[:len(msg_tasks)]:
                if isinstance(r, tuple):
                    claim_messages_map[r[0]] = r[1]
            for r in all_results[len(msg_tasks):]:
                if isinstance(r, tuple):
                    shipment_tracking_map[r[0]] = r[1]

        enriched = []
        for c in raw_claims:
            date_created = c.get("date_created", "")
            elapsed_str, elapsed_secs = _elapsed_str(date_created)
            days_open = elapsed_secs // 86400 if elapsed_secs else 0

            c_status = c.get("status", "")
            stage = c.get("stage", "")

            # Due date for mandatory action (compute first for urgency)
            due_date_raw = ""
            due_date = ""
            for player in c.get("players", []):
                if player.get("role") == "respondent":
                    for a in player.get("available_actions", []):
                        if a.get("mandatory") and a.get("due_date"):
                            due_date_raw = a["due_date"]
                            due_date = due_date_raw[:10]
                            break
                    break

            # Compute countdown hours until due_date
            countdown_hours = None
            if due_date_raw and c_status == "opened":
                from datetime import datetime, timezone
                try:
                    due_dt = datetime.fromisoformat(due_date_raw.replace("Z", "+00:00"))
                    remaining = due_dt - datetime.now(timezone.utc)
                    countdown_hours = max(0, round(remaining.total_seconds() / 3600, 1))
                except Exception:
                    pass

            # Urgency based on countdown (more precise than days_open)
            if c_status == "opened":
                if countdown_hours is not None:
                    if countdown_hours < 8:
                        urgency = "red"
                    elif countdown_hours < 24:
                        urgency = "yellow"
                    else:
                        urgency = "green"
                else:
                    if days_open > 7:
                        urgency = "red"
                    elif days_open > 3:
                        urgency = "yellow"
                    else:
                        urgency = "green"
            else:
                urgency = "gray"

            reason_id = c.get("reason_id", "")
            reason_prefix = reason_id[:3] if reason_id else ""
            reason_info = REASON_MAP.get(reason_prefix, ("Reclamo", "other"))
            reason_desc = reason_info[0]
            reason_type = reason_info[1]

            # Get seller available actions
            seller_actions = []
            for player in c.get("players", []):
                if player.get("role") == "respondent":
                    seller_actions = [a.get("action", "") for a in player.get("available_actions", [])]
                    break

            issues = []
            suggestions = []

            if reason_type == "not_received":
                issues.append("Comprador reporta no haber recibido el producto")
                suggestions.append("Verificar tracking del envio y confirmar entrega con la paqueteria")
                suggestions.append("Contactar al comprador para confirmar direccion de entrega")
            elif reason_type == "defective":
                issues.append("Comprador reporta producto defectuoso o diferente")
                suggestions.append("Solicitar fotos del defecto al comprador")
                suggestions.append("Ofrecer reemplazo o devolucion inmediata")
            else:
                issues.append(f"Reclamo: {reason_id}")
                suggestions.append("Contactar al comprador para entender el problema")

            if days_open > 7:
                issues.append(f"Reclamo abierto hace {days_open} dias - URGENTE")
                suggestions.append("Resolver HOY: reclamos abiertos > 7 dias impactan fuerte la reputacion")
            elif days_open > 3 and c_status == "opened":
                issues.append(f"Reclamo abierto hace {days_open} dias")
                suggestions.append("Resolver pronto para evitar impacto negativo en metricas")

            if stage == "dispute":
                issues.append("En disputa con mediacion de MeLi")
                suggestions.append("Responder al mediador con evidencia clara")

            resource_id = str(c.get("resource_id", ""))
            order_info = orders_map.get(resource_id, {})

            # Conversation messages
            claim_id_str = str(c.get("id", ""))
            raw_msgs = claim_messages_map.get(claim_id_str, [])
            conversation = []
            for msg in raw_msgs[-10:]:
                sender = msg.get("sender_role", msg.get("role", ""))
                text = msg.get("text", msg.get("message", ""))
                msg_date = msg.get("date_created", "")
                conversation.append({
                    "sender": sender,
                    "text": text,
                    "date": msg_date[:16].replace("T", " ") if msg_date else "",
                })

            # Tracking info for PNR claims
            tracking = shipment_tracking_map.get(resource_id, {})

            enriched.append(SimpleNamespace(
                id=c.get("id", ""),
                order_id=resource_id,
                status=c_status,
                stage=stage,
                date_created=date_created[:10] if date_created else "-",
                _sort_date=date_created or "",
                elapsed=elapsed_str,
                days_open=days_open,
                urgency=urgency,
                countdown_hours=countdown_hours,
                reason_desc=reason_desc,
                reason_id=reason_id,
                reason_type=reason_type,
                product_title=order_info.get("title", ""),
                product_price=order_info.get("price", 0),
                seller_actions=seller_actions,
                due_date=due_date,
                issues=issues,
                suggestions=suggestions,
                conversation=conversation,
                tracking=tracking,
            ))

        # Sort: urgency first (red > yellow > green > gray), then by date
        _urgency_order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
        enriched.sort(key=lambda c: (_urgency_order.get(c.urgency, 3), not c._sort_date, c._sort_date), reverse=False)
        # Reverse date within same urgency (newest first) — already handled by tuple sort

        return templates.TemplateResponse("partials/health_claims.html", {
            "request": request,
            "claims": enriched,
            "paging": paging,
            "offset": offset,
            "limit": limit,
            "status": status,
            "filter_order_id": filter_order_id,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando reclamos: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-questions", response_class=HTMLResponse)
async def health_questions_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query("UNANSWERED"),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        # MeLi Questions API does not support date_from/date_to — ignore them
        try:
            data = await client.get_questions(status=status, offset=offset, limit=limit,
                                              date_from=date_from or None, date_to=date_to or None)
        except Exception:
            # Retry without dates (MeLi rejects date params on questions)
            data = await client.get_questions(status=status, offset=offset, limit=limit)
        raw_questions = data.get("questions", data.get("results", []))
        # MeLi Questions API devuelve 'total' en raiz, no dentro de 'paging'
        paging = data.get("paging") or {
            "total": data.get("total", len(raw_questions)),
            "offset": offset,
            "limit": limit,
        }

        # Batch fetch product info for all unique item_ids
        item_ids = list({q.get("item_id") for q in raw_questions if q.get("item_id")})
        items_map = {}
        if item_ids:
            try:
                # Fetch in batches of 20
                for i in range(0, len(item_ids), 20):
                    batch = item_ids[i:i+20]
                    details = await client.get_items_details(batch)
                    for d in details:
                        body = d.get("body", d) if isinstance(d, dict) else {}
                        iid = body.get("id", "")
                        if iid:
                            items_map[iid] = {
                                "title": body.get("title", ""),
                                "thumbnail": body.get("thumbnail", body.get("secure_thumbnail", "")),
                                "price": body.get("price", 0),
                                "stock": body.get("available_quantity", 0),
                            }
            except Exception:
                pass

        # Fetch buyer history for UNANSWERED questions only
        buyer_history_map = {}  # buyer_id -> list of past questions
        if status == "UNANSWERED":
            unanswered_buyer_ids = list({
                str(q.get("from", {}).get("id", ""))
                for q in raw_questions
                if q.get("status", "UNANSWERED") == "UNANSWERED" and q.get("from", {}).get("id")
            })
            if unanswered_buyer_ids:
                sem_bh = asyncio.Semaphore(5)
                async def _fetch_buyer_hist(bid):
                    async with sem_bh:
                        return bid, await client.get_buyer_questions(bid)
                bh_tasks = [_fetch_buyer_hist(bid) for bid in unanswered_buyer_ids]
                bh_results = await asyncio.gather(*bh_tasks, return_exceptions=True)
                for r in bh_results:
                    if isinstance(r, tuple):
                        buyer_history_map[r[0]] = r[1]

        enriched = []
        for q in raw_questions:
            date_created = q.get("date_created", "")
            elapsed_str, elapsed_secs = _elapsed_str(date_created)

            q_status = q.get("status", "UNANSWERED")
            if q_status == "UNANSWERED":
                hours = elapsed_secs / 3600
                if hours > 12:
                    urgency = "red"
                elif hours > 1:
                    urgency = "yellow"
                else:
                    urgency = "green"
            else:
                urgency = "gray"

            item_id = q.get("item_id", "")
            prod = items_map.get(item_id, {})

            answer_data = q.get("answer")
            answer = None
            if answer_data:
                answer = SimpleNamespace(
                    text=answer_data.get("text", "-"),
                    date_created=answer_data.get("date_created", ""),
                )

            # Buyer history
            buyer_id = str(q.get("from", {}).get("id", ""))
            current_qid = q.get("id", "")
            raw_history = buyer_history_map.get(buyer_id, [])
            # Exclude current question from history
            buyer_history = []
            for hq in raw_history:
                if hq.get("id") == current_qid:
                    continue
                ans = hq.get("answer")
                buyer_history.append({
                    "qid": hq.get("id", ""),
                    "text": hq.get("text", ""),
                    "status": hq.get("status", ""),
                    "date_created": (hq.get("date_created", "") or "")[:10],
                    "item_id": hq.get("item_id", ""),
                    "answer_text": ans.get("text", "") if ans else "",
                })

            # Pre-serialized JSON for data attribute (max 5, truncated text)
            bh_for_json = []
            for entry in buyer_history[:5]:
                bh_for_json.append({
                    "text": entry["text"][:150],
                    "status": entry["status"],
                    "date": entry["date_created"],
                    "item_id": entry["item_id"],
                    "answer": entry["answer_text"][:150] if entry["answer_text"] else "",
                })

            # Classify question type
            q_text = q.get("text", "")
            q_type = _classify_question(q_text)
            q_type_info = _QUESTION_TYPE_LABELS.get(q_type, _QUESTION_TYPE_LABELS["general"])

            # Get quick templates for this type
            quick_templates = _QUESTION_TEMPLATES.get(q_type, _QUESTION_TEMPLATES["general"])

            enriched.append(SimpleNamespace(
                id=q.get("id", ""),
                text=q_text or "-",
                status=q_status,
                date_created=date_created[:10] if date_created else "-",
                _sort_date=date_created or "",
                elapsed=elapsed_str,
                urgency=urgency,
                item_id=item_id,
                product_title=prod.get("title", ""),
                product_thumbnail=prod.get("thumbnail", ""),
                product_price=prod.get("price", 0),
                product_stock=prod.get("stock", 0),
                answer=answer,
                buyer_id=buyer_id,
                buyer_history=buyer_history,
                buyer_question_count=len(buyer_history),
                buyer_history_json=json.dumps(bh_for_json, ensure_ascii=False) if bh_for_json else "[]",
                q_type=q_type,
                q_type_label=q_type_info["label"],
                q_type_color=q_type_info["color"],
                quick_templates=quick_templates,
            ))

        # Sort: urgency-first for UNANSWERED (red > yellow > green > gray), then newest
        _urgency_order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
        enriched.sort(key=lambda q: (_urgency_order.get(q.urgency, 3), q._sort_date), reverse=False)
        # Within same urgency, newest first — fix by making date descending
        enriched.sort(key=lambda q: (_urgency_order.get(q.urgency, 3), ""), reverse=False)
        # Stable sort by urgency, then reverse date within group
        from itertools import groupby
        sorted_enriched = []
        enriched_by_urg = sorted(enriched, key=lambda q: _urgency_order.get(q.urgency, 3))
        for _, group in groupby(enriched_by_urg, key=lambda q: q.urgency):
            grp = sorted(list(group), key=lambda q: q._sort_date, reverse=True)
            sorted_enriched.extend(grp)
        enriched = sorted_enriched

        return templates.TemplateResponse("partials/health_questions.html", {
            "request": request,
            "questions": enriched,
            "paging": paging,
            "offset": offset,
            "limit": limit,
            "status": status,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando preguntas: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-search", response_class=HTMLResponse)
async def health_search_partial(
    request: Request,
    q: str = Query("", description="Search query"),
):
    """Global search: order ID, claim ID, or keyword search."""
    query = (q or "").strip()
    if not query:
        return HTMLResponse('<p class="text-center py-4 text-gray-400 text-sm">Ingresa un termino de busqueda</p>')

    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        results = []
        result_type = "unknown"

        # Detect query type
        is_numeric = query.replace("-", "").isdigit()
        is_long_number = is_numeric and len(query.replace("-", "")) >= 8

        if is_long_number:
            # Try as Order ID and Claim ID in parallel
            async def _try_order():
                try:
                    order = await client.get(f"/orders/{query}")
                    if order and order.get("id"):
                        return order
                except Exception:
                    return None

            async def _try_claim():
                try:
                    claim = await client.get(f"/post-purchase/v1/claims/{query}")
                    if claim and claim.get("id"):
                        return claim
                except Exception:
                    return None

            # Search claims that match this order (resource_id) — fetch ALL claims, no date filter
            async def _find_claims_for_order(order_id):
                try:
                    all_cl = await client.fetch_all_claims(status=None, date_from=None, date_to=None)
                    return [cl for cl in all_cl
                            if str(cl.get("resource_id", "")) == str(order_id)]
                except Exception:
                    return []

            order_result, claim_result = await asyncio.gather(
                _try_order(), _try_claim(), return_exceptions=True
            )
            order_result = order_result if not isinstance(order_result, Exception) else None
            claim_result = claim_result if not isinstance(claim_result, Exception) else None

            if order_result and order_result.get("id"):
                order = order_result
                oi = order.get("order_items", [])
                item_info = oi[0].get("item", {}) if oi else {}
                ship = order.get("shipping", {})
                claims_for_order = await _find_claims_for_order(order["id"])
                # Refresh status of claims from search API (may be stale)
                if claims_for_order:
                    sem_r = asyncio.Semaphore(5)
                    async def _refresh_cl(cl):
                        async with sem_r:
                            try:
                                d = await client.get_claim_detail(str(cl.get("id", "")))
                                if isinstance(d, dict) and d.get("status"):
                                    cl["status"] = d["status"]
                                    if d.get("stage"):
                                        cl["stage"] = d["stage"]
                            except Exception:
                                pass
                    await asyncio.gather(*[_refresh_cl(cl) for cl in claims_for_order[:10]],
                                         return_exceptions=True)
                results.append({
                    "type": "order",
                    "id": order.get("id", ""),
                    "status": order.get("status", ""),
                    "date": (order.get("date_created", "") or "")[:10],
                    "buyer": order.get("buyer", {}).get("nickname", ""),
                    "buyer_id": order.get("buyer", {}).get("id", ""),
                    "product_title": item_info.get("title", ""),
                    "product_id": item_info.get("id", ""),
                    "total_amount": order.get("total_amount", 0),
                    "currency": order.get("currency_id", ""),
                    "shipping_status": ship.get("status", ""),
                    "shipping_id": ship.get("id", ""),
                    "claims": claims_for_order,
                })
                result_type = "order"

            if claim_result and claim_result.get("id"):
                # Add claim as a separate result (even if order was also found)
                claim = claim_result
                results.append({
                    "type": "claim",
                    "id": claim.get("id", ""),
                    "status": claim.get("status", ""),
                    "reason_id": claim.get("reason_id", ""),
                    "date": (claim.get("date_created", "") or "")[:10],
                    "order_id": claim.get("resource_id", ""),
                    "stage": claim.get("stage", ""),
                })
                if not result_type:
                    result_type = "claim"

        # Keyword search via orders
        if not results:
            try:
                seller_id = client.user_id
                data = await client.get(f"/orders/search?seller={seller_id}&q={query}&sort=date_desc&limit=10")
                for order in data.get("results", []):
                    oi = order.get("order_items", [])
                    item_info = oi[0].get("item", {}) if oi else {}
                    ship = order.get("shipping", {})
                    results.append({
                        "type": "order",
                        "id": order.get("id", ""),
                        "status": order.get("status", ""),
                        "date": (order.get("date_created", "") or "")[:10],
                        "buyer": order.get("buyer", {}).get("nickname", ""),
                        "buyer_id": order.get("buyer", {}).get("id", ""),
                        "product_title": item_info.get("title", ""),
                        "product_id": item_info.get("id", ""),
                        "total_amount": order.get("total_amount", 0),
                        "currency": order.get("currency_id", ""),
                        "shipping_status": ship.get("status", ""),
                        "shipping_id": ship.get("id", ""),
                        "claims": [],
                    })
                result_type = "search"
            except Exception:
                pass

        return templates.TemplateResponse("partials/health_search_results.html", {
            "request": request,
            "results": results,
            "query": query,
            "result_type": result_type,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error en busqueda: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-messages", response_class=HTMLResponse)
async def health_messages_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        df = date_from or None
        dt = date_to or None
        try:
            data = await client.get_messages(offset=offset, limit=limit,
                                             date_from=df, date_to=dt)
        except Exception:
            data = {"results": [], "paging": {"total": 0, "offset": 0, "limit": limit}}
        raw_messages = data.get("results", data if isinstance(data, list) else [])
        paging = data.get("paging", {"total": len(raw_messages), "offset": offset, "limit": limit})
        seller_id = str(client.user_id)

        # Collect order_ids for context enrichment
        order_context_map = {}  # order_id -> {product, amount, buyer}
        order_ids_for_context = []
        for msg in raw_messages:
            oid = msg.get("order_id", msg.get("resource_id", ""))
            if oid:
                order_ids_for_context.append(str(oid))

        if order_ids_for_context:
            sem_oc = asyncio.Semaphore(5)
            async def _fetch_order_ctx(oid):
                async with sem_oc:
                    try:
                        order = await client.get(f"/orders/{oid}")
                        oi = order.get("order_items", [])
                        item_info = oi[0].get("item", {}) if oi else {}
                        return oid, {
                            "product_title": item_info.get("title", ""),
                            "total_amount": order.get("total_amount", 0),
                            "currency": order.get("currency_id", ""),
                            "buyer": order.get("buyer", {}).get("nickname", ""),
                            "status": order.get("status", ""),
                        }
                    except Exception:
                        return oid, {}
            oc_tasks = [_fetch_order_ctx(oid) for oid in list(set(order_ids_for_context))[:20]]
            oc_results = await asyncio.gather(*oc_tasks, return_exceptions=True)
            for r in oc_results:
                if isinstance(r, tuple):
                    order_context_map[r[0]] = r[1]

        enriched = []
        for msg in raw_messages:
            messages_list = msg.get("messages", [])
            last_5 = messages_list[-5:] if messages_list else []

            # Determine who wrote last and elapsed time
            last_msg = messages_list[-1] if messages_list else None
            last_from_buyer = False
            last_elapsed = "-"
            needs_response = False
            if last_msg:
                from_id = str(last_msg.get("from", {}).get("user_id", ""))
                last_from_buyer = from_id != seller_id
                needs_response = last_from_buyer
                ts = last_msg.get("date_created", last_msg.get("date", ""))
                if ts:
                    last_elapsed, _ = _elapsed_str(ts)

            conv_date = msg.get("date_created", msg.get("date", ""))

            # Enrich individual messages
            enriched_msgs = []
            for m in last_5:
                from_id = str(m.get("from", {}).get("user_id", ""))
                is_seller = from_id == seller_id
                text_raw = m.get("text", "")
                if isinstance(text_raw, dict):
                    text = text_raw.get("plain", str(text_raw))
                else:
                    text = str(text_raw) if text_raw else "-"
                msg_date = m.get("date_created", m.get("date", ""))
                msg_time = msg_date[11:16] if msg_date and len(msg_date) > 16 else ""
                enriched_msgs.append(SimpleNamespace(
                    text=text,
                    is_seller=is_seller,
                    time=msg_time,
                ))

            pack_id = msg.get("id", msg.get("pack_id", ""))
            oid = str(msg.get("order_id", msg.get("resource_id", "")))
            order_ctx = order_context_map.get(oid, {})

            enriched.append(SimpleNamespace(
                pack_id=pack_id,
                order_id=oid,
                date=conv_date[:10] if conv_date else "-",
                _sort_date=conv_date or "",
                last_from_buyer=last_from_buyer,
                last_elapsed=last_elapsed,
                needs_response=needs_response,
                messages=enriched_msgs,
                order_product=order_ctx.get("product_title", ""),
                order_amount=order_ctx.get("total_amount", 0),
                order_currency=order_ctx.get("currency", ""),
                order_buyer=order_ctx.get("buyer", ""),
                order_status=order_ctx.get("status", ""),
            ))

        # Sort: needs_response first, then newest first
        enriched.sort(key=lambda m: (0 if m.needs_response else 1, m._sort_date), reverse=False)
        # Within each group, sort by date descending
        from itertools import groupby as _grp
        sorted_msgs = []
        enriched_by_nr = sorted(enriched, key=lambda m: 0 if m.needs_response else 1)
        for _, group in _grp(enriched_by_nr, key=lambda m: m.needs_response):
            grp = sorted(list(group), key=lambda m: m._sort_date, reverse=True)
            sorted_msgs.extend(grp)
        enriched = sorted_msgs

        return templates.TemplateResponse("partials/health_messages.html", {
            "request": request,
            "conversations": enriched,
            "paging": paging,
            "offset": offset,
            "limit": limit,
            "seller_id": seller_id,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando mensajes: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-reputation", response_class=HTMLResponse)
async def health_reputation_partial(request: Request):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        user = await client.get_user_info()
        rep = user.get("seller_reputation", {})
        metrics = rep.get("metrics", {})
        transactions = rep.get("transactions", {})
        ratings = transactions.get("ratings", {})

        claims_rate = metrics.get("claims", {}).get("rate", 0) or 0
        cancel_rate = metrics.get("cancellations", {}).get("rate", 0) or 0
        delay_rate = metrics.get("delayed_handling_time", {}).get("rate", 0) or 0

        level = rep.get("level_id", "unknown")

        # Compute margin data
        claims_margin = _compute_metric_margin(claims_rate, "claims")
        cancel_margin = _compute_metric_margin(cancel_rate, "cancellations")
        delay_margin = _compute_metric_margin(delay_rate, "delays")

        # Sales count for context
        total_sales = metrics.get("claims", {}).get("value", 0) or 0
        period = metrics.get("claims", {}).get("period", "60 days")

        # Compute how many more incidents before next threshold
        def _margin_count(rate, key, total):
            t = _MELI_THRESHOLDS[key]
            if rate < t["green"]:
                remaining = (t["green"] - rate) * max(total, 100)
                return int(remaining), "verde"
            elif rate < t["yellow"]:
                remaining = (t["yellow"] - rate) * max(total, 100)
                return int(remaining), "naranja"
            elif rate < t["red"]:
                remaining = (t["red"] - rate) * max(total, 100)
                return int(remaining), "rojo"
            return 0, "critico"

        claims_remaining, claims_next_zone = _margin_count(claims_rate, "claims", total_sales)
        cancel_remaining, cancel_next_zone = _margin_count(cancel_rate, "cancellations", total_sales)
        delay_remaining, delay_next_zone = _margin_count(delay_rate, "delays", total_sales)

        tips = []
        if claims_rate >= 0.02:
            tips.append({
                "text": "Reducir reclamos: responder preguntas proactivamente y mejorar descripciones",
                "severity": "red" if claims_rate >= 0.04 else "yellow",
                "tab": "claims",
            })
        if cancel_rate >= 0.025:
            tips.append({
                "text": "Reducir cancelaciones: mantener stock actualizado y verificar antes de publicar",
                "severity": "red" if cancel_rate >= 0.05 else "yellow",
                "tab": "claims",
            })
        if delay_rate >= 0.15:
            tips.append({
                "text": "Reducir demoras: usar Fulfillment o enviar el mismo dia del pago",
                "severity": "red" if delay_rate >= 0.20 else "yellow",
                "tab": "reputation",
            })
        if level != "5_green":
            tips.append({
                "text": "Para subir a MercadoLider: mantener las 3 metricas en verde y aumentar volumen",
                "severity": "blue",
                "tab": "reputation",
            })
        if not tips:
            tips.append({
                "text": "Excelente! Todas las metricas estan en rango optimo",
                "severity": "green",
                "tab": "",
            })

        reputation = SimpleNamespace(
            level=level,
            power_seller=rep.get("power_seller_status", None),
            completed=transactions.get("completed", 0),
            canceled=transactions.get("canceled", 0),
            positive=ratings.get("positive", 0),
            negative=ratings.get("negative", 0),
            neutral=ratings.get("neutral", 0),
            claims_rate=claims_rate,
            claims_pct=round(claims_rate * 100, 2),
            claims_status=_metric_status(claims_rate, "claims"),
            claims_margin=claims_margin,
            claims_remaining=claims_remaining,
            claims_next_zone=claims_next_zone,
            cancellation_rate=cancel_rate,
            cancellation_pct=round(cancel_rate * 100, 2),
            cancellation_status=_metric_status(cancel_rate, "cancellations"),
            cancel_margin=cancel_margin,
            cancel_remaining=cancel_remaining,
            cancel_next_zone=cancel_next_zone,
            delayed_rate=delay_rate,
            delayed_pct=round(delay_rate * 100, 2),
            delayed_status=_metric_status(delay_rate, "delays"),
            delay_margin=delay_margin,
            delay_remaining=delay_remaining,
            delay_next_zone=delay_next_zone,
            improvement_tips=tips,
            period=period,
        )

        return templates.TemplateResponse("partials/health_reputation.html", {
            "request": request,
            "reputation": reputation,
            "thresholds": _MELI_THRESHOLDS,
        })
    finally:
        await client.close()


@app.get("/partials/item-edit/{item_id}", response_class=HTMLResponse)
async def item_edit_partial(request: Request, item_id: str):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        from app.api.items import _calculate_health_score
        item = await client.get_item(item_id)

        # Enriquecer con sale_price para detectar deals
        sp = await client.get_item_sale_price(item_id)
        if sp and isinstance(sp, dict):
            amount = sp.get("amount")
            regular = sp.get("regular_amount")
            if amount and regular and regular > amount:
                item["price"] = amount
                item["original_price"] = regular

        # Obtener descripcion
        try:
            desc_data = await client.get_item_description(item_id)
            description = desc_data.get("plain_text", desc_data.get("text", ""))
        except Exception:
            description = ""

        score, problems = _calculate_health_score(item)

        # Extract seller_sku
        seller_sku = item.get("seller_custom_field") or ""
        if not seller_sku and item.get("attributes"):
            for attr in item["attributes"]:
                if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                    seller_sku = attr["value_name"]
                    break
        if not seller_sku and item.get("variations"):
            for var in item["variations"]:
                if var.get("seller_custom_field"):
                    seller_sku = var["seller_custom_field"]
                    break

        return templates.TemplateResponse("partials/item_edit_modal.html", {
            "request": request,
            "item": item,
            "description": description,
            "score": score,
            "problems": problems,
            "seller_sku": seller_sku,
        })
    finally:
        await client.close()


@app.get("/partials/item-deal/{item_id}", response_class=HTMLResponse)
async def item_deal_partial(request: Request, item_id: str):
    """Deal management partial: loads inside edit-modal for a single item."""
    import httpx
    BM_INV_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        item = await client.get_item(item_id)
        sku = _get_item_sku(item) if item else ""
        price = item.get("price", 0)
        original_price = item.get("original_price") or 0

        # Sale price enrichment
        sp = await client.get_item_sale_price(item_id)
        if sp and isinstance(sp, dict):
            amt = sp.get("amount")
            reg = sp.get("regular_amount")
            if amt and reg and reg > amt:
                price = amt
                original_price = reg

        has_deal = original_price and original_price > price

        # BM cost + FX rate in parallel
        usd_to_mxn = 20.0
        bm_cost_usd = 0
        bm_retail_usd = 0
        async def _get_bm():
            if not sku:
                return 0, 0
            base = _extract_base_sku(sku).upper()
            async with httpx.AsyncClient() as http:
                resp = await http.post(BM_INV_URL, json={
                    "COMPANYID": 1, "SEARCH": base, "CONCEPTID": 8,
                    "NUMBERPAGE": 1, "RECORDSPAGE": 10,
                }, headers={"Content-Type": "application/json"}, timeout=30.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list):
                        for it in data:
                            if it.get("SKU", "").upper() == base:
                                return it.get("AvgCostQTY", 0) or 0, it.get("RetailPrice", 0) or 0
                        if data:
                            return data[0].get("AvgCostQTY", 0) or 0, data[0].get("RetailPrice", 0) or 0
            return 0, 0

        (bm_cost_usd, bm_retail_usd), usd_to_mxn = await asyncio.gather(
            _get_bm(),
            _get_usd_to_mxn(client),
        )

        return templates.TemplateResponse("partials/item_deal_modal.html", {
            "request": request,
            "item_id": item_id,
            "title": item.get("title", ""),
            "thumbnail": item.get("thumbnail", ""),
            "sku": sku,
            "price": price,
            "original_price": original_price,
            "has_deal": has_deal,
            "bm_cost_usd": bm_cost_usd,
            "bm_retail_usd": bm_retail_usd,
            "usd_to_mxn": round(usd_to_mxn, 2),
        })
    finally:
        await client.close()


@app.get("/partials/sku-sales-table", response_class=HTMLResponse)
async def sku_sales_table_partial(
    request: Request,
    date_from: str = Query("", description="Fecha inicio YYYY-MM-DD"),
    date_to: str = Query("", description="Fecha fin YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        all_orders = await client.fetch_all_orders(
            date_from=date_from or None,
            date_to=date_to or None
        )

        # Paso 1: recolectar ventas por SKU crudo (tal cual viene de la orden)
        raw_map = {}
        for order in all_orders:
            if order.get("status") not in ["paid", "delivered"]:
                continue

            for order_item in order.get("order_items", []):
                item = order_item.get("item", {})
                sku = item.get("seller_sku") or item.get("seller_custom_field") or "SIN SKU"
                title = item.get("title", "-")
                quantity = order_item.get("quantity", 1)
                unit_price = order_item.get("unit_price", 0)
                sale_fee = order_item.get("sale_fee", 0) or 0
                iva_fee = sale_fee * 0.16
                net = unit_price * quantity - sale_fee - iva_fee

                if sku not in raw_map:
                    raw_map[sku] = {"sku": sku, "title": title, "quantity": 0, "revenue": 0}
                raw_map[sku]["quantity"] += quantity
                raw_map[sku]["revenue"] += net

        # Paso 2: agrupar variantes por SKU base
        # - SKU base / sufijo GR (-NEW,-GRA,-GRB,-GRC): suma solo variantes GR
        # - Sufijo IC (-ICC,-ICB): suma TODAS las variantes (GR + IC)
        base_groups = {}  # base -> {raw_skus: set, has_ic: bool}
        for sku in raw_map:
            base = _extract_base_sku(sku)
            if base not in base_groups:
                base_groups[base] = {"raw_skus": set(), "has_ic": False}
            base_groups[base]["raw_skus"].add(sku)
            upper = sku.upper()
            if any(upper.endswith(sfx) for sfx in _IC_SUFFIXES):
                base_groups[base]["has_ic"] = True

        sku_map = {}
        for base, group in base_groups.items():
            has_ic = group["has_ic"]
            for raw_sku in group["raw_skus"]:
                upper = raw_sku.upper()
                is_ic = any(upper.endswith(sfx) for sfx in _IC_SUFFIXES)
                is_gr = any(upper.endswith(sfx) for sfx in _GR_SUFFIXES)
                is_base = (not is_ic and not is_gr and raw_sku != "SIN SKU")

                # Si hay variantes IC, los GR y base se fusionan en la fila IC
                if has_ic:
                    # Todos van a la misma fila
                    key = base + "_ALL"
                else:
                    # Solo variantes GR y base se fusionan
                    if is_gr or is_base:
                        key = base + "_GR"
                    else:
                        # SKU sin sufijo conocido, dejar individual
                        key = raw_sku

                data = raw_map[raw_sku]
                if key not in sku_map:
                    sku_map[key] = {"sku": data["sku"], "title": data["title"], "quantity": 0, "revenue": 0}
                sku_map[key]["quantity"] += data["quantity"]
                sku_map[key]["revenue"] += data["revenue"]

        # Ordenar por cantidad vendida descendente
        sku_sales = sorted(sku_map.values(), key=lambda x: x["quantity"], reverse=True)
        total_quantity = sum(s["quantity"] for s in sku_sales)
        total_revenue = sum(s["revenue"] for s in sku_sales)

        return templates.TemplateResponse("partials/sku_sales_table.html", {
            "request": request,
            "sku_sales": sku_sales,
            "total_quantity": total_quantity,
            "total_revenue": total_revenue
        })
    finally:
        await client.close()


# === Ads API y Partials ===

def _default_dates(date_from, date_to):
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    return (
        date_from or (now - timedelta(days=7)).strftime("%Y-%m-%d"),
        date_to or now.strftime("%Y-%m-%d"),
    )


def _extract_metrics(data: dict) -> dict:
    """Extrae metricas de la respuesta de Product Ads (soporta summary y results)."""
    # La API puede retornar metrics_summary o results con metricas embebidas
    summary = data.get("metrics_summary", {})
    if summary:
        return {
            "cost": summary.get("cost", 0) or 0,
            "clicks": summary.get("clicks", 0) or 0,
            "prints": summary.get("prints", 0) or 0,
            "cpc": summary.get("cpc", 0) or 0,
            "acos": summary.get("acos", 0) or 0,
            "units": summary.get("units_quantity", 0) or 0,
            "revenue": summary.get("total_amount", 0) or 0,
        }
    # Fallback: sumar de results
    results = data.get("results", data if isinstance(data, list) else [])
    if isinstance(results, list):
        total = {"cost": 0, "clicks": 0, "prints": 0, "units": 0, "revenue": 0}
        for r in results:
            m = r.get("metrics", r)
            total["cost"] += (m.get("cost", 0) or 0)
            total["clicks"] += (m.get("clicks", 0) or 0)
            total["prints"] += (m.get("prints", 0) or 0)
            total["units"] += (m.get("units_quantity", 0) or 0)
            total["revenue"] += (m.get("total_amount", 0) or 0)
        return total
    return {"cost": 0, "clicks": 0, "prints": 0, "units": 0, "revenue": 0}


def _enrich_campaigns(campaigns_data) -> list:
    """Convierte respuesta de campanas en lista enriquecida con metricas calculadas."""
    results = campaigns_data.get("results", campaigns_data if isinstance(campaigns_data, list) else [])
    enriched = []
    for c in results:
        metrics = c.get("metrics", {})
        cost = metrics.get("cost", 0) or 0
        clicks = metrics.get("clicks", 0) or 0
        prints = metrics.get("prints", 0) or 0
        revenue = metrics.get("total_amount", 0) or 0
        units = metrics.get("units_quantity", 0) or 0
        budget_data = c.get("budget", {})
        daily_budget = budget_data.get("amount", 0) if isinstance(budget_data, dict) else (budget_data or 0)
        enriched.append({
            "id": c.get("id", "-"),
            "name": c.get("name", c.get("id", "-")),
            "status": c.get("status", "-"),
            "daily_budget": daily_budget,
            "cost": cost,
            "clicks": clicks,
            "impressions": prints,
            "revenue": revenue,
            "units": units,
            "ctr": clicks / prints * 100 if prints > 0 else 0,
            "roas": (revenue / cost) if cost > 0 else 0,
            "cpc": cost / clicks if clicks > 0 else 0,
            "acos": (cost / revenue * 100) if revenue > 0 else 0,
        })
    return enriched


@app.get("/api/ads/metrics")
async def ads_metrics_api(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        m = None
        # Intentar get_ads_items primero
        try:
            data = await client.get_ads_items(date_from, date_to)
            m = _extract_metrics(data)
        except Exception:
            pass

        # Fallback: usar metricas agregadas de campanas
        if m is None or (m["cost"] == 0 and m["clicks"] == 0):
            try:
                campaigns_data = await client.get_ads_campaigns(date_from, date_to)
                m_camp = _extract_metrics(campaigns_data)
                # Si campanas tiene datos, usarlos
                if m_camp["cost"] > 0 or m_camp["clicks"] > 0:
                    m = m_camp
                elif m is None:
                    m = m_camp
            except Exception as e:
                if m is None:
                    return {"error": f"No se pudieron obtener metricas de Ads: {e}"}

        total_cost = m["cost"]
        total_clicks = m["clicks"]
        total_prints = m["prints"]
        total_units = m["units"]
        total_revenue = m.get("revenue", 0)

        cpc = total_cost / total_clicks if total_clicks > 0 else 0
        ctr = total_clicks / total_prints * 100 if total_prints > 0 else 0
        roas = total_revenue / total_cost if total_cost > 0 else 0
        acos = (total_cost / total_revenue * 100) if total_revenue > 0 else 0

        return {
            "total_cost": f"{total_cost:.2f}",
            "total_revenue": f"{total_revenue:.2f}",
            "clicks": total_clicks,
            "impressions": total_prints,
            "sales": total_units,
            "cpc": f"{cpc:.2f}",
            "ctr": f"{ctr:.2f}",
            "acos": f"{acos:.1f}",
            "roas": f"{roas:.2f}",
        }
    finally:
        await client.close()


@app.get("/partials/ads-campaigns", response_class=HTMLResponse)
async def ads_campaigns_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
        except Exception as e:
            return HTMLResponse(f"<p class='p-6 text-center text-gray-500'>Error obteniendo campanas: {e}</p>")

        enriched = _enrich_campaigns(campaigns_data)
        enriched.sort(key=lambda x: x["cost"], reverse=True)

        return templates.TemplateResponse("partials/ads_campaigns.html", {
            "request": request,
            "campaigns": enriched,
        })
    finally:
        await client.close()


@app.get("/partials/ads-products", response_class=HTMLResponse)
async def ads_products_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # Obtener campanas primero (siempre funciona) como fallback seguro
        camps = None
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
        except Exception:
            pass

        # Intentar items para detalle por producto
        try:
            data = await client.get_ads_items(date_from, date_to)
            results = data.get("results", data if isinstance(data, list) else [])
            products = []
            for item in results:
                metrics = item.get("metrics", item)
                cost = metrics.get("cost", 0) or 0
                clicks = metrics.get("clicks", 0) or 0
                prints = metrics.get("prints", 0) or 0
                units = metrics.get("units_quantity", 0) or 0
                revenue = metrics.get("total_amount", 0) or 0
                roas = (revenue / cost) if cost > 0 else 0
                products.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "cost": cost,
                    "clicks": clicks,
                    "impressions": prints,
                    "sales": units,
                    "revenue": revenue,
                    "roas": roas,
                })
            products.sort(key=lambda x: x["cost"], reverse=True)
            top = products[:20]
            return templates.TemplateResponse("partials/ads_products.html", {
                "request": request,
                "products": top,
                "total_cost": sum(p["cost"] for p in products),
                "total_revenue": sum(p["revenue"] for p in products),
            })
        except Exception:
            pass

        # Fallback: mostrar campanas ordenadas por gasto
        if camps:
            camps_with_cost = [c for c in camps if c["cost"] > 0]
            camps_with_cost.sort(key=lambda x: x["cost"], reverse=True)
            return templates.TemplateResponse("partials/ads_products.html", {
                "request": request,
                "products": [],
                "total_cost": sum(c["cost"] for c in camps_with_cost),
                "total_revenue": sum(c["revenue"] for c in camps_with_cost),
                "fallback_campaigns": camps_with_cost,
            })

        return templates.TemplateResponse("partials/ads_products.html", {
            "request": request,
            "products": [],
            "total_cost": 0,
            "total_revenue": 0,
            "unavailable": True,
        })
    finally:
        await client.close()


@app.get("/partials/ads-burning", response_class=HTMLResponse)
async def ads_burning_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # Obtener campanas primero (fiable) como fallback
        camps = None
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
        except Exception:
            pass

        # Intentar items para detalle por producto
        items_ok = False
        try:
            data = await client.get_ads_items(date_from, date_to)
            items_ok = True
        except Exception:
            pass

        if not items_ok:
            if camps is not None:
                burning_camps = [c for c in camps if c["cost"] > 0 and c["units"] == 0]
                burning_camps.sort(key=lambda x: x["cost"], reverse=True)
                return templates.TemplateResponse("partials/ads_burning.html", {
                    "request": request,
                    "burning": [],
                    "total_burned": sum(c["cost"] for c in burning_camps),
                    "fallback_campaigns": burning_camps,
                })
            return templates.TemplateResponse("partials/ads_burning.html", {
                "request": request,
                "burning": [],
                "total_burned": 0,
                "unavailable": True,
            })

        results = data.get("results", data if isinstance(data, list) else [])

        burning = []
        for item in results:
            metrics = item.get("metrics", item)
            cost = metrics.get("cost", 0) or 0
            units = metrics.get("units_quantity", 0) or 0
            if cost > 0 and units == 0:
                clicks = metrics.get("clicks", 0) or 0
                prints = metrics.get("prints", 0) or 0
                burning.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "cost": cost,
                    "clicks": clicks,
                    "impressions": prints,
                    "ctr": round(clicks / prints * 100, 3) if prints > 0 else 0,
                })

        burning.sort(key=lambda x: x["cost"], reverse=True)
        total_burned = sum(p["cost"] for p in burning)

        # Enriquecer con detalles del listado para diagnostico
        item_ids = [p["item_id"] for p in burning if p["item_id"] != "-"]
        item_details = {}
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if body:
                        item_details[body.get("id", "")] = body
            except Exception:
                pass

        for p in burning:
            body = item_details.get(p["item_id"], {})
            p["price"] = body.get("price", 0)
            p["original_price"] = body.get("original_price") or None
            p["stock"] = body.get("available_quantity", 0)
            p["sold"] = body.get("sold_quantity", 0)
            p["status"] = body.get("status", "unknown")
            p["photos"] = len(body.get("pictures", []))
            p["has_video"] = bool(body.get("video_id"))
            p["catalog"] = bool(body.get("catalog_product_id"))
            shipping = body.get("shipping", {})
            p["free_shipping"] = shipping.get("free_shipping", False) or shipping.get("logistic_type") == "fulfillment"
            p["logistic"] = shipping.get("logistic_type", "")
            p["permalink"] = body.get("permalink", "")

            # Generar diagnostico y sugerencias
            issues = []
            suggestions = []

            if p["status"] == "paused":
                issues.append("Producto PAUSADO — Ads sigue cobrando sin posibilidad de venta")
                suggestions.append("Pausar campana de Ads inmediatamente para dejar de perder dinero")
            if p["stock"] == 0 and p["status"] != "paused":
                issues.append("Sin stock disponible")
                suggestions.append("Reponer stock o pausar Ads hasta tener inventario")
            if p["photos"] < 5:
                issues.append(f"Solo {p['photos']} fotos — insuficiente para generar confianza")
                suggestions.append("Subir minimo 8-10 fotos: frente, perfil, detalle, producto en uso, empaque")
            elif p["photos"] < 8:
                issues.append(f"{p['photos']} fotos — por debajo del promedio competitivo")
                suggestions.append("Agregar fotos de contexto/ambientacion y detalles del producto")
            if not p["has_video"]:
                issues.append("Sin video en el listado")
                suggestions.append("Agregar video corto mostrando el producto en uso")
            if p["ctr"] < 0.1 and p["impressions"] > 1000:
                issues.append(f"CTR muy bajo ({p['ctr']}%) — el anuncio no atrae clics")
                suggestions.append("Mejorar foto principal, titulo y precio para atraer mas clics")
            elif p["ctr"] > 0.3 and p["clicks"] > 100:
                issues.append(f"CTR alto ({p['ctr']}%) pero 0 ventas — el listado no convierte")
                suggestions.append("Revisar precio vs competencia, mejorar descripcion y fotos del listado")
            if p["clicks"] > 300 and p["price"] > 5000:
                issues.append(f"Muchos clics ({p['clicks']}) sin conversion en producto de ${p['price']:,.0f}")
                suggestions.append("Verificar competitividad de precio — posiblemente esta por encima del mercado")
            if not p["free_shipping"]:
                issues.append("No ofrece envio gratis")
                suggestions.append("Activar envio gratis — es casi obligatorio para competir en MeLi")
            if p["catalog"] and p["clicks"] > 200:
                issues.append("Producto en catalogo — puede no estar ganando el Buy Box")
                suggestions.append("Verificar si estas ganando el Buy Box; ajustar precio o mejorar reputacion/envio")

            if not issues:
                issues.append("No se detectaron problemas evidentes en el listado")
                suggestions.append("Considerar pausar Ads y evaluar si el producto tiene demanda real")

            p["issues"] = issues
            p["suggestions"] = suggestions

        # Enriquecer con sale_price para detectar deals
        await _enrich_with_sale_prices(client, burning, id_key="item_id", price_key="price")

        return templates.TemplateResponse("partials/ads_burning.html", {
            "request": request,
            "burning": burning,
            "total_burned": total_burned,
        })
    finally:
        await client.close()


@app.get("/api/ads/spend-timeline")
async def ads_spend_timeline_api(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    group_by: str = Query("day", description="day|month")
):
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        from datetime import datetime, timedelta

        date_from, date_to = _default_dates(date_from, date_to)
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")

        timeline = []

        async def _get_period_metrics(df: str, dt: str):
            """Intenta get_ads_items, fallback a get_ads_campaigns para un periodo."""
            try:
                data = await client.get_ads_items(df, dt)
                m = _extract_metrics(data)
                if m["cost"] > 0 or m["clicks"] > 0:
                    return m
            except Exception:
                pass
            try:
                data = await client.get_ads_campaigns(df, dt)
                return _extract_metrics(data)
            except Exception:
                return {"cost": 0, "clicks": 0, "prints": 0, "units": 0, "revenue": 0}

        if group_by == "month":
            # Agrupar por mes (max 12 meses)
            current = start.replace(day=1)
            count = 0
            while current <= end and count < 12:
                month_start = current
                if current.month == 12:
                    month_end = current.replace(year=current.year + 1, month=1, day=1) - timedelta(days=1)
                else:
                    month_end = current.replace(month=current.month + 1, day=1) - timedelta(days=1)
                if month_end > end:
                    month_end = end

                m = await _get_period_metrics(
                    month_start.strftime("%Y-%m-%d"),
                    month_end.strftime("%Y-%m-%d")
                )
                timeline.append({
                    "date": month_start.strftime("%Y-%m"),
                    "cost": m["cost"],
                    "clicks": m["clicks"],
                    "impressions": m["prints"],
                    "units": m["units"],
                    "revenue": m["revenue"],
                })

                if current.month == 12:
                    current = current.replace(year=current.year + 1, month=1)
                else:
                    current = current.replace(month=current.month + 1)
                count += 1
        else:
            # Agrupar por dia (max 31 dias)
            delta = (end - start).days
            if delta > 31:
                start = end - timedelta(days=31)

            current = start
            while current <= end:
                day_str = current.strftime("%Y-%m-%d")
                m = await _get_period_metrics(day_str, day_str)
                timeline.append({
                    "date": day_str,
                    "cost": m["cost"],
                    "clicks": m["clicks"],
                    "impressions": m["prints"],
                    "units": m["units"],
                    "revenue": m["revenue"],
                })
                current += timedelta(days=1)

        return {"timeline": timeline}
    finally:
        await client.close()


@app.get("/partials/ads-best", response_class=HTMLResponse)
async def ads_best_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # Obtener campanas primero (fiable) como fallback
        camps = None
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
        except Exception:
            pass

        # Intentar items para detalle por producto
        items_ok = False
        try:
            data = await client.get_ads_items(date_from, date_to)
            items_ok = True
        except Exception:
            pass

        if not items_ok:
            if camps is not None:
                best_camps = [c for c in camps if c["units"] > 0 and c["cost"] > 0]
                best_camps.sort(key=lambda x: x["roas"], reverse=True)
                return templates.TemplateResponse("partials/ads_best.html", {
                    "request": request,
                    "best": [],
                    "total_cost": sum(c["cost"] for c in best_camps),
                    "total_units": sum(c["units"] for c in best_camps),
                    "total_revenue": sum(c["revenue"] for c in best_camps),
                    "fallback_campaigns": best_camps,
                })
            return templates.TemplateResponse("partials/ads_best.html", {
                "request": request,
                "best": [],
                "total_cost": 0,
                "total_units": 0,
                "total_revenue": 0,
                "unavailable": True,
            })

        results = data.get("results", data if isinstance(data, list) else [])

        best = []
        for item in results:
            metrics = item.get("metrics", item)
            cost = metrics.get("cost", 0) or 0
            clicks = metrics.get("clicks", 0) or 0
            prints = metrics.get("prints", 0) or 0
            units = metrics.get("units_quantity", 0) or 0
            revenue = metrics.get("total_amount", 0) or 0
            roas = (revenue / cost) if cost > 0 else 0

            if units > 0:
                best.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "cost": cost,
                    "clicks": clicks,
                    "units": units,
                    "revenue": revenue,
                    "roas": roas,
                })

        # Ordenar por ROAS descendente
        best.sort(key=lambda x: x["roas"], reverse=True)
        top = best[:15]
        total_cost = sum(p["cost"] for p in top)
        total_units = sum(p["units"] for p in top)
        total_revenue = sum(p["revenue"] for p in top)

        return templates.TemplateResponse("partials/ads_best.html", {
            "request": request,
            "best": top,
            "total_cost": total_cost,
            "total_units": total_units,
            "total_revenue": total_revenue,
        })
    finally:
        await client.close()


@app.get("/partials/ads-performance", response_class=HTMLResponse)
async def ads_performance_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
    sort: str = Query("cost"),
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1),
    category: str = Query(""),
    tier: str = Query("all"),
):
    """Tabla unificada de rendimiento por producto con paginacion, tiers y filtro categoria."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)
        sort_key = sort if sort in ("cost", "roas", "revenue", "clicks", "units", "acos") else "cost"
        show_all = per_page >= 9999
        valid_tiers = ("all", "top", "medio", "bajo", "sin_venta")
        tier_filter = tier if tier in valid_tiers else "all"

        # Obtener campanas como fallback
        camps = None
        camp_name_map = {}  # campaign_id -> name
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
            # Mapear campaign_id -> name para enriquecer items
            for c in campaigns_data.get("results", []):
                cid = str(c.get("id", c.get("campaign_id", "")))
                camp_name_map[cid] = c.get("name", cid)
        except Exception:
            pass

        # Intentar items para detalle por producto
        try:
            data = await client.get_ads_items(date_from, date_to)
            results = data.get("results", data if isinstance(data, list) else [])
            products = []
            for item in results:
                metrics = item.get("metrics", item)
                cost = metrics.get("cost", 0) or 0
                clicks = metrics.get("clicks", 0) or 0
                units = metrics.get("units_quantity", 0) or 0
                revenue = metrics.get("total_amount", 0) or 0
                roas = (revenue / cost) if cost > 0 else 0
                acos = (cost / revenue * 100) if revenue > 0 else 0
                # Tier classification
                if units == 0:
                    tier_val = "sin_venta"
                elif roas >= 5:
                    tier_val = "top"
                elif roas >= 2:
                    tier_val = "medio"
                else:
                    tier_val = "bajo"
                camp_id = str(item.get("campaign_id", ""))
                products.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "category_id": item.get("category_id", ""),
                    "category_name": item.get("category_name", ""),
                    "campaign_name": camp_name_map.get(camp_id, ""),
                    "cost": cost,
                    "clicks": clicks,
                    "units": units,
                    "revenue": revenue,
                    "roas": roas,
                    "acos": acos,
                    "tier": tier_val,
                })

            # Enriquecer category_name si falta (batch fetch)
            missing_cat_ids = {p["category_id"] for p in products if p["category_id"] and not p["category_name"]}
            if missing_cat_ids:
                for cid in missing_cat_ids:
                    if cid in _category_cache:
                        continue
                    try:
                        cat = await client.get(f"/categories/{cid}")
                        _category_cache[cid] = cat.get("name", cid)
                    except Exception:
                        _category_cache[cid] = cid
                for p in products:
                    if p["category_id"] and not p["category_name"]:
                        p["category_name"] = _category_cache.get(p["category_id"], p["category_id"])

            # Contar tiers antes de filtrar
            tier_counts = {
                "top": sum(1 for p in products if p["tier"] == "top"),
                "medio": sum(1 for p in products if p["tier"] == "medio"),
                "bajo": sum(1 for p in products if p["tier"] == "bajo"),
                "sin_venta": sum(1 for p in products if p["tier"] == "sin_venta"),
            }

            # Aplicar filtro de tier
            if tier_filter != "all":
                products = [p for p in products if p["tier"] == tier_filter]

            # Aplicar filtro de categoria
            if category:
                products = [p for p in products if p["category_id"] == category or p["category_name"] == category]

            products.sort(key=lambda x: x[sort_key], reverse=True)
            total_count = len(products)
            all_products_cost = sum(p["cost"] for p in products)
            all_products_revenue = sum(p["revenue"] for p in products)

            if show_all:
                page_products = products
                total_pages = 1
                page = 1
            else:
                total_pages = max(1, (total_count + per_page - 1) // per_page)
                page = min(page, total_pages)
                start = (page - 1) * per_page
                page_products = products[start:start + per_page]

            # Categorias unicas para el filtro dropdown
            all_categories = sorted(
                {(p["category_id"], p["category_name"]) for p in products if p["category_id"]},
                key=lambda x: x[1]
            )

            return templates.TemplateResponse("partials/ads_performance.html", {
                "request": request,
                "products": page_products,
                "total_cost": all_products_cost,
                "total_revenue": all_products_revenue,
                "current_sort": sort_key,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "total_count": total_count,
                "show_all": show_all,
                "tier_filter": tier_filter,
                "category_filter": category,
                "tier_counts": tier_counts,
                "all_categories": all_categories,
            })
        except Exception:
            pass

        # Fallback: campanas ordenadas
        if camps:
            camps_with_cost = [c for c in camps if c["cost"] > 0]
            camps_with_cost.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
            return templates.TemplateResponse("partials/ads_performance.html", {
                "request": request,
                "products": [],
                "total_cost": sum(c["cost"] for c in camps_with_cost),
                "total_revenue": sum(c["revenue"] for c in camps_with_cost),
                "fallback_campaigns": camps_with_cost,
                "current_sort": sort_key,
                "page": 1,
                "per_page": per_page,
                "total_pages": 1,
                "total_count": len(camps_with_cost),
                "show_all": False,
                "tier_filter": "all",
                "category_filter": "",
                "tier_counts": {},
                "all_categories": [],
            })

        return templates.TemplateResponse("partials/ads_performance.html", {
            "request": request,
            "products": [],
            "total_cost": 0,
            "total_revenue": 0,
            "unavailable": True,
            "current_sort": "cost",
            "page": 1,
            "per_page": per_page,
            "total_pages": 1,
            "total_count": 0,
            "show_all": False,
            "tier_filter": "all",
            "category_filter": "",
            "tier_counts": {},
            "all_categories": [],
        })
    finally:
        await client.close()


@app.get("/partials/ads-no-ads", response_class=HTMLResponse)
async def ads_no_ads_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    """Productos activos que NO están en ninguna campaña de Ads."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # 1. Obtener todos los item IDs activos
        try:
            all_active_ids = await client.get_all_active_item_ids()
        except Exception as e:
            return HTMLResponse(f"<p class='p-6 text-center text-gray-500'>Error obteniendo items activos: {e}</p>")

        # 2. Obtener TODOS los items con ads (sin filtro de fechas)
        #    Esto evita el bug de mostrar items que SI tienen ads pero sin metricas recientes
        ads_item_ids = set()
        try:
            ads_item_ids = await client.get_all_ads_item_ids()
        except Exception:
            pass

        # Fallback: si el metodo paginado fallo, intentar via campañas (sin filtro de fecha)
        if not ads_item_ids:
            try:
                campaigns_data = await client.get_ads_campaigns()
                results = campaigns_data.get("results", [])
                for c in results:
                    for item in c.get("items", []):
                        iid = item.get("id", "")
                        if iid:
                            ads_item_ids.add(iid)
            except Exception:
                pass

        # 3. Diferencia: activos sin ads
        no_ads_ids = [iid for iid in all_active_ids if iid not in ads_item_ids]

        # 4. Fetch detalles en batches (primeros 40)
        no_ads_ids = no_ads_ids[:40]
        products = []
        category_ids = set()
        for i in range(0, len(no_ads_ids), 20):
            batch = no_ads_ids[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if not body:
                        continue
                    cat_id = body.get("category_id", "")
                    if cat_id:
                        category_ids.add(cat_id)
                    products.append({
                        "id": body.get("id", ""),
                        "title": body.get("title", "-"),
                        "price": body.get("price", 0),
                        "original_price": body.get("original_price") or None,
                        "stock": body.get("available_quantity", 0),
                        "sold_quantity": body.get("sold_quantity", 0),
                        "thumbnail": body.get("thumbnail", body.get("secure_thumbnail", "")),
                        "permalink": body.get("permalink", ""),
                        "category_id": cat_id,
                        "category_name": "",
                    })
            except Exception:
                pass

        # Enriquecer con sale_price para detectar deals
        await _enrich_with_sale_prices(client, products, id_key="id", price_key="price")

        # 4b. Fetch nombres de categorias (pocos unicos)
        cat_names = {}
        for cat_id in category_ids:
            try:
                cat = await client.get(f"/categories/{cat_id}")
                cat_names[cat_id] = cat.get("name", cat_id)
            except Exception:
                cat_names[cat_id] = cat_id
        for p in products:
            p["category_name"] = cat_names.get(p["category_id"], p["category_id"])

        # 5. Ordenar por sold_quantity desc (mayor oportunidad primero)
        products.sort(key=lambda x: x["sold_quantity"], reverse=True)

        # 6. Obtener campanas activas para el selector
        campaigns = []
        try:
            camp_data = await client.get_ads_campaigns()
            for c in camp_data.get("results", []):
                if c.get("status") == "active":
                    campaigns.append({
                        "id": c.get("campaign_id", c.get("id", "")),
                        "name": c.get("name", ""),
                    })
        except Exception:
            pass
        # Fallback: si no obtuvimos campanas, intentar con fechas
        if not campaigns:
            try:
                camp_data = await client.get_ads_campaigns(date_from, date_to)
                for c in camp_data.get("results", []):
                    if c.get("status") == "active":
                        campaigns.append({
                            "id": c.get("campaign_id", c.get("id", "")),
                            "name": c.get("name", ""),
                        })
            except Exception:
                pass

        return templates.TemplateResponse("partials/ads_no_ads.html", {
            "request": request,
            "products": products,
            "total_active": len(all_active_ids),
            "total_with_ads": len(ads_item_ids),
            "campaigns": campaigns,
        })
    finally:
        await client.close()


@app.post("/api/ads/assign-to-campaign")
async def assign_items_to_campaign_api(request: Request):
    """Asigna items a una campana de Product Ads."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        item_ids = body.get("item_ids", [])
        campaign_id = body.get("campaign_id")
        if not item_ids or not campaign_id:
            return JSONResponse({"detail": "item_ids y campaign_id requeridos"}, status_code=400)
        result = await client.assign_items_to_campaign(item_ids, int(campaign_id))
        return JSONResponse({"ok": True, "result": result})
    except MeliApiError as e:
        if e.status_code == 401 or "UnauthorizedException" in str(e):
            return JSONResponse({
                "detail": "Tu app de MeLi no tiene permiso de escritura para Product Ads. "
                          "Activa el permiso en developers.mercadolibre.com.mx > Tu App > "
                          "Permisos, o re-autenticate en /auth/connect"
            }, status_code=403)
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/ads/item/{item_id}")
async def get_ad_item_status_api(item_id: str):
    """Estado de un item en Product Ads via marketplace API v2."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        resp = await client._request_raw(
            "GET",
            f"/marketplace/advertising/MLM/product_ads/ads/{item_id}",
            extra_headers={"api-version": "2"},
        )
        return JSONResponse(resp)
    except MeliApiError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.post("/api/ads/item/{item_id}/status")
async def update_ad_item_status_api(item_id: str, request: Request):
    """Activa, pausa o remueve un item de Product Ads."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        status = body.get("status")  # "active", "paused", "idle"
        campaign_id = body.get("campaign_id")
        if not status:
            return JSONResponse({"detail": "Campo 'status' requerido"}, status_code=400)
        result = await client.update_ad_item_status(item_id, status, campaign_id)
        return JSONResponse({"ok": True, "result": result})
    except MeliApiError as e:
        is_cert = e.status_code == 401
        return JSONResponse({
            "detail": str(e),
            "requires_certification": is_cert
        }, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/ads/campaigns-list")
async def get_campaigns_list():
    """Lista todas las campanas con ID y nombre (sin metricas, rapido)."""
    from app.services.meli_client import MeliApiError
    from datetime import date, timedelta
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        data = await client.get_ads_campaigns(date_from=week_ago, date_to=today)
        results = data.get("results", [])
        campaigns = [
            {"id": c.get("id"), "name": c.get("name"), "status": c.get("status")}
            for c in results
        ]
        return JSONResponse({"campaigns": campaigns})
    except MeliApiError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/ads/debug-assign/{item_id}/{campaign_id}")
async def debug_ads_assign(item_id: str, campaign_id: str):
    """Prueba TODOS los patrones de endpoint para asignar item a campaña. Retorna raw MeLi responses."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    try:
        token = client.access_token
        adv_id = await client._get_advertiser_id()
        cid = int(campaign_id)
        results = {}

        async def try_req(label, method, url, **kwargs):
            try:
                r = await client._client.request(method, url, **kwargs)
                try:
                    body = r.json()
                except Exception:
                    body = r.text[:300]
                results[label] = {"status": r.status_code, "body": body}
            except Exception as ex:
                results[label] = {"status": "error", "body": str(ex)}

        h2 = {"api-version": "2"}
        h1 = {"Api-Version": "1"}

        await try_req("PUT_marketplace_v2",
            "PUT", f"https://api.mercadolibre.com/marketplace/advertising/MLM/product_ads/ads/{item_id}",
            params={"channel": "marketplace"}, json={"status": "active", "campaign_id": cid}, headers=h2)

        await try_req("PUT_advertising_v2",
            "PUT", f"https://api.mercadolibre.com/advertising/product_ads/ads/{item_id}",
            json={"status": "active", "campaign_id": cid}, headers=h2)

        await try_req("PUT_with_advertiser",
            "PUT", f"https://api.mercadolibre.com/advertising/MLM/advertisers/{adv_id}/product_ads/ads/{item_id}",
            json={"status": "active", "campaign_id": cid}, headers=h2)

        await try_req("POST_campaign_ads",
            "POST", f"https://api.mercadolibre.com/advertising/product_ads_2/campaigns/{campaign_id}/ads",
            json={"item_id": item_id, "status": "active"}, headers=h2)

        await try_req("POST_ads_direct",
            "POST", f"https://api.mercadolibre.com/advertising/product_ads/ads",
            json={"item_id": item_id, "campaign_id": cid, "status": "active"}, headers=h2)

        await try_req("PUT_bulk_advertiser",
            "PUT", f"https://api.mercadolibre.com/marketplace/advertising/MLM/advertisers/{adv_id}/product_ads/ads",
            params={"channel": "marketplace"},
            json={"target": [item_id], "payload": {"status": "active", "campaign_id": cid}}, headers=h2)

        await try_req("PUT_v1",
            "PUT", f"https://api.mercadolibre.com/advertising/advertisers/{adv_id}/product_ads/items/{item_id}",
            json={"status": "active", "campaign_id": cid}, headers=h1)

        working = {k: v for k, v in results.items() if v.get("status") not in (401, 403, "error")}
        return JSONResponse({"advertiser_id": adv_id, "item": item_id, "campaign": campaign_id,
                             "working_patterns": working, "all_results": results})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/token-refresh")
async def get_refresh_token():
    """Muestra el refresh token actual para configurar Railway Variables."""
    tokens = await token_store.get_any_tokens()
    if not tokens:
        return JSONResponse({"error": "No autenticado — ve primero a /auth/connect"}, status_code=401)
    rt = tokens.get("refresh_token", "")
    return JSONResponse({
        "refresh_token": rt,
        "user_id": tokens.get("user_id"),
        "instrucciones": "Copia el refresh_token y agrégalo como variable MELI_REFRESH_TOKEN en Railway Variables panel"
    })


@app.get("/api/ads/check-write-permission")
async def check_ads_write_permission():
    """Verifica certification_status de la app y si tiene permiso de escritura en Product Ads."""
    from app.services.meli_client import MeliApiError
    from app.config import MELI_CLIENT_ID as _app_id
    client = await get_meli_client()
    if not client:
        return JSONResponse({"write_enabled": False, "error": "not_authenticated"}, status_code=401)
    try:
        app_id = _app_id
        # 1. Verificar certification_status de la app
        cert_status = "unknown"
        try:
            app_info = await client._request_raw("GET", f"/applications/{app_id}")
            cert_status = app_info.get("certification_status", "unknown")
        except Exception:
            pass

        # 2. Intentar el PUT de todas formas para comprobar si ya funciona
        try:
            await client._request_raw(
                "PUT",
                "/marketplace/advertising/MLM/product_ads/ads/MLM1346239567",
                extra_headers={"api-version": "2"},
                json={"status": "idle", "campaign_id": 0},
            )
            return JSONResponse({"write_enabled": True, "certification_status": cert_status})
        except MeliApiError as e:
            if e.status_code == 401:
                return JSONResponse({
                    "write_enabled": False,
                    "error": "not_certified",
                    "certification_status": cert_status,
                    "detail": "La app requiere certificacion MeLi para escribir en Product Ads"
                })
            # Cualquier otro error != 401 significa que el write ya funciona
            return JSONResponse({"write_enabled": True, "certification_status": cert_status})
    except Exception as e:
        return JSONResponse({"write_enabled": False, "error": str(e)}, status_code=500)
    finally:
        await client.close()


@app.post("/api/ads/campaigns/{campaign_id}")
async def update_campaign_api(request: Request, campaign_id: str):
    """Actualiza una campaña de Product Ads (status, budget, acos_target)."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        body = await request.json()
        result = await client.update_campaign(
            campaign_id,
            status=body.get("status"),
            budget=body.get("budget"),
            acos_target=body.get("acos_target"),
        )
        return {"ok": True, "result": result}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.close()


@app.post("/api/ads/campaigns")
async def create_campaign_api(request: Request):
    """Crea una nueva campaña de Product Ads."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        body = await request.json()
        name = body.get("name", "")
        budget = body.get("budget", 0)
        if not name or not budget:
            return {"error": "name y budget son requeridos"}
        result = await client.create_campaign(
            name=name,
            budget=float(budget),
            acos_target=body.get("acos_target"),
            status=body.get("status", "active"),
        )
        return {"ok": True, "result": result}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.close()


# Cache para ads-by-category: key -> (timestamp, data)
_ads_category_cache: dict[str, tuple[float, list]] = {}
_ADS_CATEGORY_CACHE_TTL = 1800  # 30 minutos


@app.post("/api/ads/campaigns-with-items")
async def create_campaign_with_items_api(request: Request):
    """Crea campaña y asigna items TOP en un solo flujo."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        name = body.get("name", "")
        budget = body.get("budget", 0)
        acos_target = body.get("acos_target")
        item_ids = body.get("item_ids", [])
        if not name or not budget:
            return JSONResponse({"detail": "name y budget son requeridos"}, status_code=400)
        # 1. Crear campaña
        campaign = await client.create_campaign(
            name=name,
            budget=float(budget),
            acos_target=acos_target,
            status="active",
        )
        campaign_id = campaign.get("id", campaign.get("campaign_id"))
        assigned_count = 0
        errors = []
        # 2. Asignar items en lotes de 50
        if item_ids and campaign_id:
            for i in range(0, len(item_ids), 50):
                batch = item_ids[i:i+50]
                try:
                    result = await client.assign_items_to_campaign(batch, int(campaign_id))
                    assigned_count += len(result.get("results", []))
                    errors.extend(result.get("errors", []))
                except Exception as e:
                    errors.append({"batch_start": i, "error": str(e)})
        return JSONResponse({
            "ok": True,
            "campaign_id": campaign_id,
            "campaign_name": name,
            "assigned_count": assigned_count,
            "errors": errors,
        })
    except MeliApiError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/partials/ads-by-category", response_class=HTMLResponse)
async def ads_by_category_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
):
    """Agrupacion de metricas de ads por categoria de producto."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        import time as _time_mod
        date_from, date_to = _default_dates(date_from, date_to)
        cache_key = f"ads_cat:{date_from}:{date_to}"
        cached = _ads_category_cache.get(cache_key)
        if cached and (_time_mod.time() - cached[0]) < _ADS_CATEGORY_CACHE_TTL:
            categories = cached[1]
            return templates.TemplateResponse("partials/ads_by_category.html", {
                "request": request,
                "categories": categories,
                "from_cache": True,
            })

        # 1. Obtener todos los items con metricas (paginado)
        adv_id = await client._get_advertiser_id()
        all_items = []
        offset = 0
        limit = 100
        while True:
            params = {
                "metrics": "clicks,prints,cost,units_quantity,total_amount",
                "limit": limit,
                "offset": offset,
            }
            if date_from:
                params["date_from"] = date_from
            if date_to:
                params["date_to"] = date_to
            try:
                data = await client.get(
                    f"/advertising/advertisers/{adv_id}/product_ads/items",
                    params=params,
                )
            except Exception:
                break
            results = data.get("results", data if isinstance(data, list) else [])
            if not results:
                break
            all_items.extend(results)
            if len(results) < limit:
                break
            offset += limit

        # 2. Extraer item_ids únicos con métricas
        item_id_to_metrics = {}
        for item in all_items:
            iid = item.get("item_id", item.get("id", ""))
            if not iid:
                continue
            metrics = item.get("metrics", item)
            item_id_to_metrics[iid] = {
                "cost": metrics.get("cost", 0) or 0,
                "clicks": metrics.get("clicks", 0) or 0,
                "units": metrics.get("units_quantity", 0) or 0,
                "revenue": metrics.get("total_amount", 0) or 0,
                "title": item.get("title", ""),
            }

        # 3. Fetch category_id via batch (grupos de 20)
        item_ids = list(item_id_to_metrics.keys())
        item_cat_map = {}  # item_id -> category_id
        item_title_map = {}  # item_id -> title (from MeLi item data)
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if not body:
                        continue
                    iid = body.get("id", "")
                    if iid:
                        item_cat_map[iid] = body.get("category_id", "")
                        item_title_map[iid] = body.get("title", item_id_to_metrics.get(iid, {}).get("title", ""))
            except Exception:
                pass

        # 4. Lookup category names via cache
        missing_cats = {cid for cid in item_cat_map.values() if cid and cid not in _category_cache}
        for cid in missing_cats:
            try:
                cat = await client.get(f"/categories/{cid}")
                _category_cache[cid] = cat.get("name", cid)
            except Exception:
                _category_cache[cid] = cid

        # 5. Agrupar por category_id
        cat_groups: dict[str, dict] = {}
        for iid, met in item_id_to_metrics.items():
            cat_id = item_cat_map.get(iid, "")
            cat_name = _category_cache.get(cat_id, cat_id) if cat_id else "Sin categoría"
            key = cat_id or "unknown"
            if key not in cat_groups:
                cat_groups[key] = {
                    "category_id": cat_id,
                    "category_name": cat_name,
                    "count": 0,
                    "cost": 0.0,
                    "revenue": 0.0,
                    "units": 0,
                    "clicks": 0,
                    "best_title": "",
                    "best_revenue": 0.0,
                }
            g = cat_groups[key]
            g["count"] += 1
            g["cost"] += met["cost"]
            g["revenue"] += met["revenue"]
            g["units"] += met["units"]
            g["clicks"] += met["clicks"]
            title = item_title_map.get(iid, met.get("title", ""))
            if met["revenue"] > g["best_revenue"]:
                g["best_revenue"] = met["revenue"]
                g["best_title"] = title

        # 6. Calcular ROAS, ACOS, CTR
        categories = []
        for g in cat_groups.values():
            cost = g["cost"]
            revenue = g["revenue"]
            g["roas"] = round(revenue / cost, 2) if cost > 0 else 0.0
            g["acos"] = round(cost / revenue * 100, 1) if revenue > 0 else 0.0
            categories.append(g)

        # Ordenar por ingresos desc
        categories.sort(key=lambda x: x["revenue"], reverse=True)

        # Guardar en cache
        _ads_category_cache[cache_key] = (_time_mod.time(), categories)

        return templates.TemplateResponse("partials/ads_by_category.html", {
            "request": request,
            "categories": categories,
            "from_cache": False,
        })
    finally:
        await client.close()


# === Promotions API ===

@app.get("/api/items/{item_id}/promotions")
async def get_item_promotions_api(item_id: str):
    """Consulta promociones disponibles para un item."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        promos = await client.get_item_promotions(item_id)
        import logging
        logging.getLogger("api").info(f"promotions({item_id}): {promos}")
        return {"promotions": promos, "error": None}
    except Exception as e:
        import logging
        logging.getLogger("api").warning(f"promotions({item_id}) error: {e}")
        return {"promotions": [], "error": str(e)}
    finally:
        await client.close()


@app.post("/api/items/{item_id}/promotions/activate")
async def activate_item_promotion_api(item_id: str, request: Request):
    """Activa una promocion para un item."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        deal_price = body.get("deal_price")
        promotion_type = body.get("promotion_type")
        if not deal_price or not promotion_type:
            return JSONResponse({"detail": "deal_price y promotion_type requeridos"}, status_code=400)
        kwargs = {}
        if body.get("start_date"):
            kwargs["start_date"] = body["start_date"]
        if body.get("finish_date"):
            kwargs["finish_date"] = body["finish_date"]
        if body.get("original_price"):
            kwargs["original_price"] = float(body["original_price"])
        if body.get("promotion_id"):
            kwargs["promotion_id"] = body["promotion_id"]
        if body.get("is_modification"):
            kwargs["is_modification"] = True
        result = await client.activate_item_promotion(
            item_id, float(deal_price), promotion_type, **kwargs
        )
        return {"ok": True, "result": result}
    except Exception as e:
        import logging, json as _json
        error_body = getattr(e, "body", None)
        logging.getLogger("api").warning(f"activate_promotion({item_id}) error: {e} body={error_body}")
        # Build detailed error message from MeLi response
        detail = str(e)
        if isinstance(error_body, dict):
            parts = []
            if error_body.get("error"):
                parts.append(error_body["error"])
            if error_body.get("message"):
                parts.append(error_body["message"])
            cause = error_body.get("cause", [])
            if isinstance(cause, list):
                for c in cause[:5]:
                    if isinstance(c, dict):
                        parts.append(c.get("message") or c.get("code") or str(c))
                    else:
                        parts.append(str(c))
            detail = " | ".join(parts) if parts else detail
        elif isinstance(error_body, str) and error_body:
            detail = error_body
        return JSONResponse({"ok": False, "detail": detail, "meli_body": error_body}, status_code=400)
    finally:
        await client.close()


@app.delete("/api/items/{item_id}/promotions/{promotion_type}")
async def delete_item_promotion_api(item_id: str, promotion_type: str):
    """Desactiva una promocion de un item."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        await client.delete_item_promotion(item_id, promotion_type)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    finally:
        await client.close()


@app.put("/api/items/{item_id}/stock")
async def update_item_stock_api(item_id: str, request: Request):
    """Actualiza el stock de un item SIN variaciones.
    Para items con variaciones, usa /api/items/{item_id}/sync-variation-stocks."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        quantity = int(body.get("quantity", 0))
        result = await client.update_item_stock(item_id, quantity)
        return {"ok": True, "quantity": quantity}
    except ValueError as e:
        # Item tiene variaciones — rechazar con mensaje claro
        return JSONResponse({"ok": False, "has_variations": True, "detail": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    finally:
        await client.close()


@app.post("/api/items/{item_id}/sync-variation-stocks")
async def sync_variation_stocks_api(item_id: str, request: Request):
    """Sincroniza stock de CADA variacion de un item multi-variacion con su propio SKU en BinManager.

    Para cada variacion:
    1. Obtiene su SKU (SELLER_SKU o seller_custom_field)
    2. Consulta BinManager con ese SKU base
    3. Actualiza SOLO ESA variacion con floor(bm_stock * pct)
       No toca las demas variaciones.

    Body (optional): { "pct": 0.6 }  — porcentaje del stock BM a usar (default 60%)
    Returns: { ok, item_id, results: [{variation_id, sku, combo, bm_total, meli_qty, updated}] }
    """
    import httpx
    BM_WH_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"

    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        pct = float(body.get("pct", 0.6))
        pct = max(0.0, min(1.0, pct))
        dry_run = bool(body.get("dry_run", False))  # Si True: consulta BM pero NO actualiza MeLi

        # 1. Obtener variaciones con SKUs
        # El batch fetch (GET /items?ids=) no devuelve attributes por variacion.
        # GET /items/{id}/variations/{var_id} SI devuelve seller_custom_field real.
        client_vars = body.get("variations", [])  # [{id, sku, stock, combo}, ...] para combos
        client_var_map = {str(cv.get("id")): cv for cv in client_vars}

        # Fetch item individual para obtener variation IDs y attribute_combinations
        item = await client.get(f"/items/{item_id}")
        raw_vars_base = item.get("variations", [])
        if not raw_vars_base:
            return JSONResponse({"ok": False, "detail": "El item no tiene variaciones"}, status_code=400)

        # Fetch detalle de cada variacion via /items/{id}/variations/{var_id}
        # Este endpoint devuelve seller_custom_field por variacion (el batch no lo hace)
        access_token = client.access_token

        async def _fetch_variation_detail(var_id, http_client):
            try:
                resp = await http_client.get(
                    f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
            return None

        async with httpx.AsyncClient() as http_pre:
            var_details = await asyncio.gather(
                *[_fetch_variation_detail(v.get("id"), http_pre) for v in raw_vars_base]
            )

        # Construir raw_vars enriquecidas: SKU real del detail + combo/stock del cliente
        raw_vars = []
        for base_v, detail in zip(raw_vars_base, var_details):
            merged = dict(base_v)
            if detail:
                scf = detail.get("seller_custom_field")
                if scf and scf != "None":
                    merged["seller_custom_field"] = scf
                if detail.get("attributes"):
                    merged["attributes"] = detail["attributes"]
            cv = client_var_map.get(str(base_v.get("id")), {})
            if cv.get("combo"):
                merged["_combo_override"] = cv["combo"]
            raw_vars.append(merged)

        # 2. Para cada variacion: obtener SKU y consultar BM
        async def _fetch_var_bm(v: dict, http: httpx.AsyncClient):
            v_sku = _get_var_sku(v)
            if v.get("_combo_override"):
                combo_str = v["_combo_override"]
            else:
                combos = []
                for ac in v.get("attribute_combinations", []):
                    combos.append(f"{ac.get('name','')}: {ac.get('value_name','')}")
                combo_str = ", ".join(combos) if combos else f"Var {v.get('id','')}"
            result = {
                "variation_id": v.get("id"),
                "sku": v_sku,
                "combo": combo_str,
                "meli_stock": v.get("available_quantity", 0),
                "bm_mty": 0,
                "bm_cdmx": 0,
                "bm_total": 0,
                "meli_qty": 0,
                "updated": False,
                "error": None,
            }
            if not v_sku:
                result["error"] = "Sin SKU en variacion"
                return result
            base_sku = _extract_base_sku(v_sku)
            clean_sku = _clean_sku_for_bm(base_sku)
            if not clean_sku:
                result["error"] = "SKU no mapeable a BM"
                return result
            try:
                resp = await http.post(BM_WH_URL, json={
                    "COMPANYID": 1, "SKU": clean_sku, "WarehouseID": None,
                    "LocationID": "47,62,68", "BINID": None,
                    "Condition": _bm_conditions_for_sku(v_sku), "ForInventory": 0, "SUPPLIERS": None,
                }, headers={"Content-Type": "application/json"}, timeout=15.0)
                if resp.status_code == 200:
                    rows = resp.json() or []
                    mty = cdmx = tj = 0
                    for row in rows:
                        qty = row.get("QtyTotal", 0) or 0
                        wname = (row.get("WarehouseName") or "").lower()
                        if "monterrey" in wname or "maxx" in wname:
                            mty += qty
                        elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                            cdmx += qty
                        else:
                            tj += qty
                    result["bm_mty"] = mty
                    result["bm_cdmx"] = cdmx
                    result["bm_total"] = mty + cdmx
            except Exception as ex:
                result["error"] = f"BM error: {ex}"
            return result

        async with httpx.AsyncClient() as http:
            var_results = await asyncio.gather(*[_fetch_var_bm(v, http) for v in raw_vars])

        # 3. Actualizar cada variacion con su propio stock BM
        var_updates = []
        for r in var_results:
            qty = int(r["bm_total"] * pct)
            r["meli_qty"] = qty
            if r["error"]:
                continue  # No actualizar variaciones con error en BM
            var_updates.append({"id": r["variation_id"], "available_quantity": qty})

        if var_updates and not dry_run:
            try:
                await client.update_variation_stocks_directly(item_id, var_updates)
                # Marcar como actualizadas
                updated_ids = {u["id"] for u in var_updates}
                for r in var_results:
                    if r["variation_id"] in updated_ids:
                        r["updated"] = True
                # Invalidar cache de stock issues para reflejar cambio
                _synced_alert_items.add(item_id)
                _stock_issues_cache.clear()
            except Exception as ex:
                for r in var_results:
                    if not r["error"]:
                        r["error"] = f"MeLi update error: {ex}"

        return JSONResponse({
            "ok": True,
            "item_id": item_id,
            "pct": pct,
            "dry_run": dry_run,
            "results": list(var_results),
            "updated_count": sum(1 for r in var_results if r["updated"]),
        })
    finally:
        await client.close()


@app.get("/api/items/{item_id}/debug-variations")
async def debug_item_variations(item_id: str, request: Request):
    """Debug: retorna estructura cruda de variaciones (batch vs individual) para diagnosticar SKU lookup."""
    import httpx as _httpx
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        item = await client.get(f"/items/{item_id}")
        variations = item.get("variations", [])
        debug = []
        async with _httpx.AsyncClient() as http:
            for v in variations:
                var_id = v.get("id")
                # Fetch individual
                individual = None
                try:
                    r = await http.get(
                        f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}",
                        headers={"Authorization": f"Bearer {client.access_token}"},
                        timeout=10.0,
                    )
                    if r.status_code == 200:
                        individual = r.json()
                except Exception as ex:
                    individual = {"error": str(ex)}

                debug.append({
                    "id": var_id,
                    "from_batch": {
                        "available_quantity": v.get("available_quantity"),
                        "seller_custom_field": v.get("seller_custom_field"),
                        "attribute_combinations": v.get("attribute_combinations", []),
                        "attributes": v.get("attributes", []),
                        "_get_var_sku_result": _get_var_sku(v),
                    },
                    "from_individual": {
                        "seller_custom_field": individual.get("seller_custom_field") if individual else None,
                        "attributes": individual.get("attributes", []) if individual else [],
                        "_get_var_sku_result": _get_var_sku(individual) if individual and "id" in individual else "N/A",
                    } if individual else "fetch failed",
                })
        return JSONResponse({"item_id": item_id, "variation_count": len(variations), "variations": debug})
    finally:
        await client.close()


@app.put("/api/items/{item_id}/status")
async def update_item_status_api(item_id: str, request: Request):
    """Cambia el estado de un item (active/paused)."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        status = body.get("status", "active")
        if status not in ("active", "paused"):
            return JSONResponse({"detail": "Status invalido"}, status_code=400)
        result = await client.update_item_status(item_id, status)
        return {"ok": True, "status": status}
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    finally:
        await client.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
