from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
from app.services.meli_client import get_meli_client
from app.services.amazon_client import get_amazon_client
from app.services import token_store
from app import order_net_revenue
import time as _time
import asyncio

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

# Templates para partials de Amazon
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Cache para Amazon daily sales — clave: "{seller_id}:{YYYY-MM-DD}"
_amazon_daily_cache: dict[str, tuple[float, dict]] = {}
_AMAZON_DAILY_TTL = 180  # 3 minutos (rate limits de Amazon son estrictos)


@router.get("/goal")
async def get_goal(request: Request):
    """Obtiene la meta diaria de la cuenta activa."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    await client.close()
    goal = await token_store.get_daily_goal(client.user_id)
    return {"user_id": client.user_id, "daily_goal": goal}


@router.post("/goal")
async def set_goal(request: Request):
    """Guarda la meta diaria de la cuenta activa."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    await client.close()
    body = await request.json()
    goal = float(body.get("daily_goal", 500000))
    await token_store.set_daily_goal(client.user_id, goal)
    return {"user_id": client.user_id, "daily_goal": goal}


@router.get("")
async def get_metrics():
    """Obtiene metricas del vendedor: ventas, ingresos, productos."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        # Obtener info del usuario
        user = await client.get_user_info()

        # Obtener ordenes recientes (ultimos 30 dias)
        orders_data = await client.get_orders(limit=50)
        orders = orders_data.get("results", [])

        # Calcular metricas
        total_orders = orders_data.get("paging", {}).get("total", 0)

        # Ventas del mes actual
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        monthly_orders = [
            o for o in orders
            if datetime.fromisoformat(o["date_created"].replace("Z", "+00:00")).replace(tzinfo=None) >= month_start
        ]

        monthly_revenue = sum(
            order_net_revenue(o) for o in monthly_orders
            if o.get("status") in ["paid", "delivered"]
        )

        monthly_sales_count = len([
            o for o in monthly_orders
            if o.get("status") in ["paid", "delivered"]
        ])

        # Obtener cantidad de productos activos
        items_data = await client.get_items(limit=1)
        active_items = items_data.get("paging", {}).get("total", 0)

        # Ultimas 5 ventas
        recent_orders = orders[:5]

        return {
            "user": {
                "id": user.get("id"),
                "nickname": user.get("nickname"),
                "seller_reputation": user.get("seller_reputation", {})
            },
            "summary": {
                "total_orders": total_orders,
                "monthly_sales": monthly_sales_count,
                "monthly_revenue": monthly_revenue,
                "active_items": active_items
            },
            "recent_orders": recent_orders
        }
    finally:
        await client.close()


@router.get("/sales-chart")
async def get_sales_chart(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    """Obtiene datos para el grafico de ventas con paginacion completa."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        now = datetime.utcnow()
        if not date_from:
            date_from = now.replace(day=1).strftime("%Y-%m-%d")
        if not date_to:
            date_to = now.strftime("%Y-%m-%d")

        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)
        chart_data, group_by = _build_chart_data(all_orders, date_from, date_to)

        return {"data": chart_data, "group_by": group_by}
    finally:
        await client.close()


@router.get("/dashboard-data")
async def get_dashboard_data(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    """Endpoint unificado: metricas + chart en una sola llamada (1x fetch_all_orders)."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        now = datetime.utcnow()
        if not date_from:
            date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        if not date_to:
            date_to = now.strftime("%Y-%m-%d")

        # UNA sola llamada para todas las ordenes (con cache + concurrencia)
        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)

        # Productos activos (1 sola llamada rapida, limit=1)
        items_data = await client.get_items(limit=1)

        # Metricas — misma definición que ML "Ventas brutas": todo excepto cancelado/pendiente pago
        _EXCLUDED = {"cancelled", "payment_required", "payment_in_process"}
        paid_orders = [o for o in all_orders if o.get("status") not in _EXCLUDED]
        # NO llamar enrich_orders_with_shipping aquí — haría 1 llamada API por orden (>2000).
        # Los KPIs usan el fallback de order_net_revenue (total - sale_fee - IVA), suficientemente preciso.
        metrics = {
            "total_orders": len(all_orders),
            "period_sales": len(paid_orders),
            "period_revenue": sum(order_net_revenue(o) for o in paid_orders),
            "active_items": items_data.get("paging", {}).get("total", 0)
        }

        # Chart
        chart_data, group_by = _build_chart_data(all_orders, date_from, date_to)

        return {
            "metrics": metrics,
            "chart": {"data": chart_data, "group_by": group_by}
        }
    finally:
        await client.close()


@router.get("/daily-sales")
async def get_daily_sales(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    goal: float = Query(0, description="Meta diaria en MXN (0 = leer de DB)"),
):
    """Ventas agrupadas por dia con % de meta."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        now = datetime.utcnow()
        # México CST = UTC-6 permanente (DST eliminado en 2022).
        # Sin este ajuste, "hoy" se corta a las 6 PM México porque UTC ya es el día siguiente.
        now_mx = now - timedelta(hours=6)
        if not date_from:
            date_from = (now_mx - timedelta(days=29)).strftime("%Y-%m-%d")
        if not date_to:
            date_to = now_mx.strftime("%Y-%m-%d")
        # Leer meta de DB si no se pasa como parámetro
        if goal <= 0:
            goal = await token_store.get_daily_goal(client.user_id)

        # +1 día en date_to para capturar órdenes de tarde/noche México
        # que la API de ML ve como "mañana UTC" (p.ej. 11 PM CST = 5 AM UTC siguiente día).
        # El filtro por date_key en buckets descarta cualquier orden fuera del rango real.
        _fetch_date_to = (
            datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=_fetch_date_to)

        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")

        buckets = {}
        cur = start
        while cur <= end:
            buckets[cur.strftime("%Y-%m-%d")] = {"units": 0, "revenue_gross": 0.0, "revenue_net": 0.0}
            cur += timedelta(days=1)

        _EXCL = {"cancelled", "payment_required", "payment_in_process"}
        for order in all_orders:
            if order.get("status") in _EXCL:
                continue
            order_date_utc = datetime.fromisoformat(
                order["date_created"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            # Convertir a hora México (CST UTC-6) — igual que multi-account dashboard
            order_date_mx = order_date_utc - timedelta(hours=6)
            date_key = order_date_mx.strftime("%Y-%m-%d")
            if date_key in buckets:
                buckets[date_key]["units"] += sum(
                    oi.get("quantity", 1) for oi in order.get("order_items", [])
                )
                buckets[date_key]["revenue_gross"] += order.get("total_amount", 0) or 0
                buckets[date_key]["revenue_net"] += order_net_revenue(order)

        daily_data = []
        for date_key in sorted(buckets.keys(), reverse=True):
            data = buckets[date_key]
            pct = (data["revenue_gross"] / goal * 100) if goal > 0 else 0
            daily_data.append({
                "date": date_key,
                "units": data["units"],
                "revenue_gross": round(data["revenue_gross"], 2),
                "revenue_net": round(data["revenue_net"], 2),
                "pct_of_goal": round(pct, 1),
            })

        total_units = sum(d["units"] for d in daily_data)
        total_gross = sum(d["revenue_gross"] for d in daily_data)
        total_net = sum(d["revenue_net"] for d in daily_data)
        days_met = sum(1 for d in daily_data if d["pct_of_goal"] >= 100)
        avg_pct = sum(d["pct_of_goal"] for d in daily_data) / len(daily_data) if daily_data else 0

        return {
            "daily_data": daily_data,
            "goal": goal,
            "totals": {
                "units": total_units,
                "revenue_gross": round(total_gross, 2),
                "revenue_net": round(total_net, 2),
                "days_met": days_met,
                "avg_pct": round(avg_pct, 1),
                "total_days": len(daily_data),
            }
        }
    finally:
        await client.close()


@router.get("/day-breakdown")
async def get_day_breakdown(date: str = Query(..., description="YYYY-MM-DD")):
    """Top SKUs vendidos en un día específico vs promedio de los 7 días anteriores."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        target = datetime.strptime(date, "%Y-%m-%d")
        date_from = (target - timedelta(days=14)).strftime("%Y-%m-%d")
        _fetch_to = (target + timedelta(days=1)).strftime("%Y-%m-%d")
        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=_fetch_to)

        _EXCL = {"cancelled", "payment_required", "payment_in_process"}
        target_str = target.strftime("%Y-%m-%d")
        prev_dates = {(target - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)}

        day_skus: dict = {}
        prev_skus: dict = {}

        for order in all_orders:
            if order.get("status") in _EXCL:
                continue
            order_date_utc = datetime.fromisoformat(
                order["date_created"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            order_date_mx = order_date_utc - timedelta(hours=6)
            date_key = order_date_mx.strftime("%Y-%m-%d")

            for item in order.get("order_items", []):
                raw_sku = (item.get("item", {}).get("seller_sku") or "").strip()
                if not raw_sku:
                    raw_sku = item.get("item", {}).get("id", "SIN-SKU")
                title = (item.get("item", {}).get("title") or raw_sku)[:45]
                qty = item.get("quantity", 1)
                revenue = (item.get("unit_price") or 0) * qty

                if date_key == target_str:
                    if raw_sku not in day_skus:
                        day_skus[raw_sku] = {"units": 0, "revenue": 0.0, "title": title}
                    day_skus[raw_sku]["units"] += qty
                    day_skus[raw_sku]["revenue"] += revenue
                elif date_key in prev_dates:
                    if raw_sku not in prev_skus:
                        prev_skus[raw_sku] = {}
                    if date_key not in prev_skus[raw_sku]:
                        prev_skus[raw_sku][date_key] = {"units": 0, "revenue": 0.0}
                    prev_skus[raw_sku][date_key]["units"] += qty
                    prev_skus[raw_sku][date_key]["revenue"] += revenue

        all_skus = set(list(day_skus.keys()) + list(prev_skus.keys()))
        results = []
        for sku in all_skus:
            td = day_skus.get(sku, {"units": 0, "revenue": 0.0, "title": sku})
            pd_data = prev_skus.get(sku, {})
            prev_units = sum(v["units"] for v in pd_data.values())
            prev_rev = sum(v["revenue"] for v in pd_data.values())
            avg_units = prev_units / 7
            avg_rev = prev_rev / 7
            delta_pct = ((td["units"] - avg_units) / avg_units * 100) if avg_units > 0 else None
            results.append({
                "sku": sku,
                "title": td.get("title", sku),
                "today_units": td["units"],
                "today_revenue": round(td["revenue"], 2),
                "avg_units": round(avg_units, 1),
                "avg_revenue": round(avg_rev, 2),
                "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
            })

        results.sort(key=lambda x: x["today_units"], reverse=True)
        return {"date": date, "skus": results[:20]}
    finally:
        await client.close()


@router.get("/low-stock-alerts")
async def get_low_stock_alerts(threshold: int = Query(5, description="Umbral de stock bajo")):
    """Top 10 SKUs más vendidos (últimos 30 días) con stock BM bajo."""
    from app.api.productos import _bm_stock
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        now_mx = datetime.utcnow() - timedelta(hours=6)
        date_from = (now_mx - timedelta(days=29)).strftime("%Y-%m-%d")
        _fetch_to = (now_mx + timedelta(days=1)).strftime("%Y-%m-%d")
        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=_fetch_to)

        _EXCL = {"cancelled", "payment_required", "payment_in_process"}
        sku_sales: dict = {}
        for order in all_orders:
            if order.get("status") in _EXCL:
                continue
            for item in order.get("order_items", []):
                raw_sku = (item.get("item", {}).get("seller_sku") or "").strip()
                if not raw_sku:
                    continue
                title = (item.get("item", {}).get("title") or raw_sku)[:45]
                qty = item.get("quantity", 1)
                revenue = (item.get("unit_price") or 0) * qty
                if raw_sku not in sku_sales:
                    sku_sales[raw_sku] = {"units": 0, "revenue": 0.0, "title": title}
                sku_sales[raw_sku]["units"] += qty
                sku_sales[raw_sku]["revenue"] += revenue

        top_skus = sorted(sku_sales.items(), key=lambda x: x[1]["units"], reverse=True)[:10]
        results = []
        for sku, data in top_skus:
            stock = await _bm_stock(sku)
            avail = stock.get("avail", 0)
            velocity = data["units"] / 30
            days_rem = round(avail / velocity) if velocity > 0 else None
            results.append({
                "sku": sku,
                "title": data["title"],
                "units_30d": data["units"],
                "revenue_30d": round(data["revenue"], 2),
                "daily_velocity": round(velocity, 2),
                "bm_stock": avail,
                "days_remaining": int(days_rem) if days_rem is not None else None,
                "alert": avail <= threshold,
            })

        return {
            "threshold": threshold,
            "alerts": [r for r in results if r["alert"]],
            "all_skus": results,
        }
    finally:
        await client.close()


@router.get("/top-products")
async def get_top_products(days: int = Query(30, description="Período en días: 7, 15, 30 o 90")):
    """Top 20 productos más vendidos en el período con stock y status actual de ML."""
    days = max(7, min(90, days))
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        now_mx = datetime.utcnow() - timedelta(hours=6)
        date_from = (now_mx - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        _fetch_to = (now_mx + timedelta(days=1)).strftime("%Y-%m-%d")
        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=_fetch_to)

        _EXCL = {"cancelled", "payment_required", "payment_in_process"}
        item_sales: dict = {}
        for order in all_orders:
            if order.get("status") in _EXCL:
                continue
            for oi in order.get("order_items", []):
                item = oi.get("item", {})
                item_id = item.get("id", "")
                if not item_id:
                    continue
                sku = (item.get("seller_sku") or "").strip()
                title = (item.get("title") or item_id)[:55]
                qty = oi.get("quantity", 1)
                revenue = (oi.get("unit_price") or 0) * qty
                if item_id not in item_sales:
                    item_sales[item_id] = {"units": 0, "revenue": 0.0, "title": title, "sku": sku}
                item_sales[item_id]["units"] += qty
                item_sales[item_id]["revenue"] += revenue

        top_items = sorted(item_sales.items(), key=lambda x: x[1]["units"], reverse=True)[:20]
        top_ids = [iid for iid, _ in top_items]

        # Batch fetch: status, available_quantity, thumbnail por item
        item_details: dict = {}
        if top_ids:
            try:
                resp = await client.get(
                    f"/items?ids={','.join(top_ids)}"
                    f"&attributes=id,status,available_quantity,thumbnail,seller_custom_field"
                )
                entries = resp if isinstance(resp, list) else []
                for entry in entries:
                    body = entry.get("body", {}) if isinstance(entry, dict) and "body" in entry else entry
                    if isinstance(body, dict) and body.get("id"):
                        item_details[body["id"]] = body
            except Exception:
                pass

        # BM stock: leer del caché bulk en memoria — NUNCA llamar BM en vivo
        try:
            from app.main import _bm_stock_cache as _tp_bm_cache, normalize_to_bm_sku as _tp_norm
        except Exception:
            _tp_bm_cache = {}
            _tp_norm = lambda s: s.upper()[:10]

        results = []
        for item_id, data in top_items:
            detail = item_details.get(item_id, {})
            status = detail.get("status", "unknown")
            ml_stock = detail.get("available_quantity")
            thumbnail = (detail.get("thumbnail") or "").replace("http://", "https://")
            sku = data["sku"] or detail.get("seller_custom_field") or ""
            bm_avail = None
            if sku:
                cached = _tp_bm_cache.get(_tp_norm(sku))
                if cached:
                    bm_avail = cached[1].get("avail_total")
            results.append({
                "item_id": item_id,
                "sku": sku,
                "title": data["title"],
                "units": data["units"],
                "revenue": round(data["revenue"], 2),
                "status": status,
                "ml_stock": ml_stock,
                "bm_avail": bm_avail,
                "thumbnail": thumbnail,
            })

        return {"days": days, "products": results}
    finally:
        await client.close()


_ACCOUNT_NAMES = {
    "523916436": "APANTALLATEMX",
    "292395685": "AUTOBOT",
    "391393176": "BLOWTECHNOLOGIES",
    "515061615": "LUTEMAMEXICO",
}
_OWN_ACCOUNT_IDS = {int(k) for k in _ACCOUNT_NAMES}


@router.get("/competition")
async def get_competition_analysis(item_id: str = Query(...)):
    """Análisis de competencia para un item_id propio: nuestros listings + externos en catálogo."""
    import aiosqlite as _aio
    from app.config import DATABASE_PATH as _DB_P
    from app.services.meli_client import MeliApiError as _MErr
    from app.main import _bm_stock_cache as _bsc, normalize_to_bm_sku as _norm

    # ── 1. Buscar item en DB y obtener SKU base ───────────────────────────────
    async with _aio.connect(_DB_P) as db:
        db.row_factory = _aio.Row
        row = await (await db.execute(
            "SELECT sku, account_id FROM ml_listings WHERE item_id = ? LIMIT 1", (item_id,)
        )).fetchone()
    if not row:
        raise HTTPException(404, "Item no encontrado en DB")

    raw_sku = row["sku"] or ""
    base_sku = raw_sku.split("/")[0].split("-")[0].strip()[:10] if raw_sku else ""

    # ── 2. Todos nuestros listings con el mismo SKU base ─────────────────────
    async with _aio.connect(_DB_P) as db:
        db.row_factory = _aio.Row
        listings = await (await db.execute(
            """SELECT item_id, title, account_id, status, available_qty,
                      catalog_listing, logistic_type, price, sku
               FROM ml_listings
               WHERE sku LIKE ? AND status != 'closed'
               ORDER BY catalog_listing DESC, price ASC""",
            (f"%{base_sku}%",)
        )).fetchall()

    # ── 3. Datos de ventas 30d por listing ───────────────────────────────────
    cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    async with _aio.connect(_DB_P) as db:
        db.row_factory = _aio.Row
        sales_rows = await (await db.execute(
            """SELECT item_id, COUNT(*) as orders, SUM(quantity) as units,
                      SUM(neto_plat) as revenue, AVG(margen_pct) as avg_margin,
                      AVG(recup_retail_pct) as avg_recup, AVG(unit_price) as avg_price,
                      AVG(fx_rate) as avg_fx, AVG(retail_ph_usd) as retail_ph_usd
               FROM order_history
               WHERE sku LIKE ? AND platform='ml' AND order_date >= ? AND status != 'cancelled'
               GROUP BY item_id""",
            (f"%{base_sku}%", cutoff)
        )).fetchall()
    sales_map = {r["item_id"]: dict(r) for r in sales_rows}

    # ── 4. BM stock desde caché ──────────────────────────────────────────────
    bm_key = _norm(base_sku) if base_sku else ""
    bm_cached = _bsc.get(bm_key)
    bm_avail = bm_cached[1].get("avail_total") if bm_cached else None
    # Retail PH: tomar del primer order que lo tenga, o del caché
    retail_ph_usd = None
    retail_ph_mxn = None
    for s in sales_map.values():
        if s.get("retail_ph_usd"):
            retail_ph_usd = s["retail_ph_usd"]
            retail_ph_mxn = round(retail_ph_usd * (s.get("avg_fx") or 17.0), 2)
            break

    # ── 5. Batch fetch precios frescos de ML (incluye sale_price si hay deal) ──
    all_item_ids = [dict(l)["item_id"] for l in listings]
    ml_prices: dict = {}  # item_id -> {price, deal_price, original_price}
    try:
        price_client = await get_meli_client()
        if price_client and all_item_ids:
            chunk = all_item_ids[:20]
            raw_prices = await price_client.get(
                f"/items?ids={','.join(chunk)}&attributes=id,price,sale_price,original_price"
            )
            items_list = raw_prices if isinstance(raw_prices, list) else []
            for entry in items_list:
                body = entry.get("body", entry) if isinstance(entry, dict) else {}
                iid = body.get("id")
                if not iid:
                    continue
                base_price = body.get("price")
                sale = body.get("sale_price") or {}
                orig = body.get("original_price")
                deal_price = sale.get("amount") if isinstance(sale, dict) else None
                ml_prices[iid] = {
                    "price": base_price,
                    "deal_price": deal_price,
                    "original_price": orig or base_price,
                }
    except Exception:
        pass

    # ── 6. price_to_win por cada listing de catálogo (paralelo) ─────────────
    catalog_ids = [dict(l)["item_id"] for l in listings if dict(l)["catalog_listing"] == 1]

    async def _ptw(iid, acct_id):
        try:
            cl = await get_meli_client(user_id=acct_id)
            if not cl:
                return iid, None
            data = await cl.get(f"/items/{iid}/price_to_win?siteId=MLM&version=v2")
            return iid, data
        except _MErr:
            return iid, None
        except Exception:
            return iid, None

    ptw_tasks = [_ptw(iid, dict(l)["account_id"]) for l in listings
                 if dict(l)["catalog_listing"] == 1 for iid in [dict(l)["item_id"]]]
    ptw_results = await asyncio.gather(*ptw_tasks)
    ptw_map = {iid: data for iid, data in ptw_results if data}

    # ── 6. Competidores externos (1 sola llamada al catalog_product_id) ──────
    catalog_product_id = None
    for _, data in ptw_map.items():
        if data and data.get("catalog_product_id"):
            catalog_product_id = data["catalog_product_id"]
            break

    external_sellers = []
    if catalog_product_id:
        try:
            client = await get_meli_client()
            if client:
                raw = await client.get(f"/products/{catalog_product_id}/items?limit=30")
                for it in (raw.get("results", []) if isinstance(raw, dict) else []):
                    sid = (it.get("seller") or {}).get("id")
                    if sid and int(sid) not in _OWN_ACCOUNT_IDS:
                        external_sellers.append({
                            "item_id": it.get("item_id") or it.get("id"),
                            "price": it.get("price"),
                            "buy_box_winner": it.get("buy_box_winner", False),
                            "full": (it.get("shipping") or {}).get("logistic_type") == "fulfillment",
                            "seller_id": sid,
                        })
                external_sellers.sort(key=lambda x: x.get("price") or 999999)
        except Exception:
            pass

    # ── 7. Ensamblar listings ────────────────────────────────────────────────
    our_listings = []
    for l in listings:
        ld = dict(l)
        iid = ld["item_id"]
        s = sales_map.get(iid, {})
        ptw = ptw_map.get(iid, {})
        fresh = ml_prices.get(iid, {})
        base_price = fresh.get("price") or ld["price"]
        deal_price = fresh.get("deal_price")
        original_price = fresh.get("original_price") or base_price
        our_listings.append({
            "item_id": iid,
            "title": ld["title"],
            "account": _ACCOUNT_NAMES.get(str(ld["account_id"]), str(ld["account_id"])),
            "status": ld["status"],
            "catalog": bool(ld["catalog_listing"]),
            "logistic": ld["logistic_type"],
            "price": original_price,
            "deal_price": deal_price,
            "has_deal": deal_price is not None,
            "ml_stock": ld["available_qty"],
            "sales_30d": int(s.get("units") or 0),
            "revenue_30d": round(float(s.get("revenue") or 0), 2),
            "margen_pct": round(float(s.get("avg_margin") or 0), 1),
            "recup_pct": round(float(s.get("avg_recup") or 0), 1),
            "avg_price": round(float(s.get("avg_price") or 0), 2),
            "ptw_status": (ptw.get("status") or "").lower() if ptw else None,
            "ptw_price": ptw.get("price_to_win") if ptw else None,
            "winner_price": (ptw.get("winner") or {}).get("price") if ptw else None,
            "winner_item": (ptw.get("winner") or {}).get("item_id") if ptw else None,
        })

    # ── 8. Recomendación ────────────────────────────────────────────────────
    best = max((l for l in our_listings if l["sales_30d"] > 0),
               key=lambda x: x["margen_pct"], default=None)
    cheapest_ext = external_sellers[0]["price"] if external_sellers else None
    rec = ""
    if best and cheapest_ext:
        gap = round(best["avg_price"] - cheapest_ext, 0)
        if gap > 0:
            rec = (f"{best['account']} ({best['logistic']}, ${best['avg_price']:,.0f}) "
                   f"tiene el mejor margen activo ({best['margen_pct']}%). "
                   f"Externo más barato: ${cheapest_ext:,.0f} (${gap:,.0f} menos). "
                   f"Mantener precio actual — bajar no justifica la pérdida de margen.")
        else:
            rec = (f"{best['account']} ya es competitivo. "
                   f"Externo más barato: ${cheapest_ext:,.0f}. Buen posicionamiento.")
    elif best:
        rec = f"{best['account']} ({best['logistic']}, ${best['avg_price']:,.0f}) — sin competidores externos en catálogo detectados."
    elif our_listings:
        rec = "Sin ventas en los últimos 30 días en ningún listing de este SKU."

    return {
        "item_id": item_id,
        "sku": base_sku,
        "catalog_product_id": catalog_product_id,
        "bm_avail": bm_avail,
        "retail_ph_usd": retail_ph_usd,
        "retail_ph_mxn": retail_ph_mxn,
        "our_listings": our_listings,
        "external_sellers": external_sellers,
        "recommendation": rec,
    }


def _build_chart_data(all_orders: list, date_from: str, date_to: str):
    """Construye los datos del chart a partir de ordenes ya obtenidas."""
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    delta_days = (end - start).days

    if delta_days <= 31:
        group_by = "day"
        buckets = defaultdict(lambda: {"count": 0, "amount": 0})
        for i in range(delta_days + 1):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            buckets[d]

        _EXCL_C = {"cancelled", "payment_required", "payment_in_process"}
        for order in all_orders:
            if order.get("status") in _EXCL_C:
                continue
            order_date = datetime.fromisoformat(
                order["date_created"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            date_key = order_date.strftime("%Y-%m-%d")
            if date_key in buckets:
                buckets[date_key]["count"] += 1
                buckets[date_key]["amount"] += order_net_revenue(order)

        chart_data = [
            {"date": date, "count": data["count"], "amount": data["amount"]}
            for date, data in sorted(buckets.items())
        ]
    else:
        group_by = "month"
        buckets = defaultdict(lambda: {"count": 0, "amount": 0})
        current = start.replace(day=1)
        while current <= end:
            buckets[current.strftime("%Y-%m")]
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

        _EXCL_M = {"cancelled", "payment_required", "payment_in_process"}
        for order in all_orders:
            if order.get("status") in _EXCL_M:
                continue
            order_date = datetime.fromisoformat(
                order["date_created"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            month_key = order_date.strftime("%Y-%m")
            if month_key in buckets:
                buckets[month_key]["count"] += 1
                buckets[month_key]["amount"] += order_net_revenue(order)

        chart_data = [
            {"date": date, "count": data["count"], "amount": data["amount"]}
            for date, data in sorted(buckets.items())
        ]

    return chart_data, group_by


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON — Ventas diarias del vendedor activo
# Usa la SP-API Orders v0 para obtener órdenes por rango de fechas.
# Retorna HTML parcial (partial) listo para inyectarse con HTMX en el dashboard.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/amazon-daily-sales", response_class=HTMLResponse)
async def get_amazon_daily_sales(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    """Ventas diarias de Amazon agrupadas por día — devuelve HTML parcial para HTMX."""

    # ── 1. Obtener cliente Amazon activo ──────────────────────────────────────
    client = await get_amazon_client()
    if not client:
        # Sin cuenta Amazon configurada → partial vacío con mensaje
        return _templates.TemplateResponse(
            request, "partials/amazon_daily_card.html", {                "not_connected": True,
                "daily_data": [],
                "totals": {},
                "date_from": date_from,
                "date_to": date_to,
                "nickname": "",
                "marketplace": "",
            },
        )

    # ── 2. Rango de fechas por defecto: últimos 30 días ───────────────────────
    now = datetime.utcnow()
    if not date_from:
        date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = now.strftime("%Y-%m-%d")

    # ── 3. Revisar caché (clave: seller_id + rango) ───────────────────────────
    cache_key = f"{client.seller_id}:{date_from}:{date_to}"
    if cache_key in _amazon_daily_cache:
        ts, cached = _amazon_daily_cache[cache_key]
        if _time.time() - ts < _AMAZON_DAILY_TTL:
            return _templates.TemplateResponse(request,"partials/amazon_daily_card.html", cached)

    # ── 4. Obtener órdenes via SP-API ─────────────────────────────────────────
    try:
        # fetch_orders_range obtiene TODAS las órdenes del rango (paginando internamente)
        orders = await client.fetch_orders_range(date_from=date_from, date_to=date_to)
    except Exception as exc:
        return _templates.TemplateResponse(
            request, "partials/amazon_daily_card.html", {                "error": str(exc),
                "daily_data": [],
                "totals": {},
                "date_from": date_from,
                "date_to": date_to,
                "nickname": client.nickname,
                "marketplace": client.marketplace_name,
                "seller_id": client.seller_id,
            },
        )

    # ── 5. Agrupar órdenes por día ────────────────────────────────────────────
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end   = datetime.strptime(date_to,   "%Y-%m-%d")

    # Inicializar buckets vacíos para cada día del rango
    buckets: dict[str, dict] = {}
    cur = start
    while cur <= end:
        buckets[cur.strftime("%Y-%m-%d")] = {"units": 0, "revenue": 0.0, "orders": 0}
        cur += timedelta(days=1)

    # Distribuir cada orden en su bucket correspondiente
    for order in orders:
        # Amazon usa PurchaseDate en ISO-8601 con zona horaria
        raw_date = order.get("PurchaseDate", "")
        if not raw_date:
            continue
        try:
            order_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue

        date_key = order_dt.strftime("%Y-%m-%d")
        if date_key not in buckets:
            continue

        # Solo órdenes pagadas / enviadas (no pendientes ni canceladas)
        status = order.get("OrderStatus", "")
        if status not in ("Shipped", "Delivered", "Unshipped"):
            continue

        buckets[date_key]["orders"] += 1

        # Monto: OrderTotal.Amount (string) en la moneda del marketplace
        total_raw = order.get("OrderTotal", {})
        try:
            amount = float(total_raw.get("Amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        buckets[date_key]["revenue"] += amount

        # Unidades: NumberOfItemsShipped + NumberOfItemsUnshipped
        shipped   = int(order.get("NumberOfItemsShipped", 0) or 0)
        unshipped = int(order.get("NumberOfItemsUnshipped", 0) or 0)
        buckets[date_key]["units"] += shipped + unshipped

    # ── 6. Construir lista ordenada (más reciente primero) ────────────────────
    daily_data = []
    for date_key in sorted(buckets.keys(), reverse=True):
        d = buckets[date_key]
        daily_data.append({
            "date":    date_key,
            "orders":  d["orders"],
            "units":   d["units"],
            "revenue": round(d["revenue"], 2),
        })

    # ── 7. Totales del período ────────────────────────────────────────────────
    totals = {
        "orders":  sum(d["orders"]  for d in daily_data),
        "units":   sum(d["units"]   for d in daily_data),
        "revenue": round(sum(d["revenue"] for d in daily_data), 2),
        "total_days": len(daily_data),
        "days_with_sales": sum(1 for d in daily_data if d["orders"] > 0),
    }

    # ── 8. Guardar en caché y renderizar partial ──────────────────────────────
    ctx = {
        "daily_data":   daily_data,
        "totals":       totals,
        "date_from":    date_from,
        "date_to":      date_to,
        "nickname":     client.nickname,
        "marketplace":  client.marketplace_name,
        "seller_id":    client.seller_id,
        "not_connected": False,
        "error":        None,
    }
    # Guardar sin `request` (no serializable)
    _amazon_daily_cache[cache_key] = (_time.time(), {k: v for k, v in ctx.items() if k != "request"})

    return _templates.TemplateResponse(request, "partials/amazon_daily_card.html", ctx)


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON — Nuevos endpoints para el dashboard rediseñado
# ─────────────────────────────────────────────────────────────────────────────

# Caché de órdenes crudas — compartida entre todos los endpoints Amazon
_amazon_orders_cache: dict[str, tuple[float, list]] = {}
_amazon_orders_locks: dict[str, asyncio.Lock] = {}
_AMAZON_ORDERS_TTL = 300  # 5 minutos

# Caché para Sales API — métricas diarias (totalSales OPS, unitCount, orderCount)
_amazon_metrics_cache: dict[str, tuple[float, list]] = {}
_amazon_metrics_locks: dict[str, asyncio.Lock] = {}
_AMAZON_METRICS_TTL = 300  # 5 minutos


async def _get_cached_amazon_orders(client, date_from: str, date_to: str) -> list:
    """Obtiene y cachea la lista cruda de órdenes Amazon para un rango de fechas.

    Usa double-check lock per cache_key para evitar que llamadas simultáneas
    (dashboard-data + daily-sales-data + recent-orders) lancen múltiples
    requests a SP-API y provoquen 429 QuotaExceeded.
    """
    cache_key = f"raw:{client.seller_id}:{date_from}:{date_to}"

    # Fast path — sin lock si ya está en caché
    if cache_key in _amazon_orders_cache:
        ts, orders = _amazon_orders_cache[cache_key]
        if _time.time() - ts < _AMAZON_ORDERS_TTL:
            return orders

    # Crear lock solo si no existe (seguro en asyncio single-thread)
    if cache_key not in _amazon_orders_locks:
        _amazon_orders_locks[cache_key] = asyncio.Lock()

    async with _amazon_orders_locks[cache_key]:
        # Verificar de nuevo dentro del lock — otra coroutine pudo haber llenado el caché
        if cache_key in _amazon_orders_cache:
            ts, orders = _amazon_orders_cache[cache_key]
            if _time.time() - ts < _AMAZON_ORDERS_TTL:
                return orders

        # ── Call 1: Shipped + Unshipped + PartiallyShipped ────────────────
        orders_active = await client.fetch_orders_range(date_from=date_from, date_to=date_to)

        # ── Call 2: Pending por separado ──────────────────────────────────
        # SP-API quirk: mezclar Pending+Shipped en mismo query devuelve SOLO Pending.
        # Por eso se hace una segunda llamada separada y se mergean los resultados.
        # Esto permite que "Hoy" refleje todas las órdenes nuevas (aún en Pending).
        try:
            orders_pending = await client.fetch_orders_range(
                date_from=date_from, date_to=date_to, statuses=["Pending"]
            )
            # Deduplicar por AmazonOrderId (algunos pueden aparecer en ambas listas)
            active_ids = {o.get("AmazonOrderId") for o in orders_active}
            new_pending = [o for o in orders_pending if o.get("AmazonOrderId") not in active_ids]
            all_orders = orders_active + new_pending
        except Exception:
            # Si el fetch de Pending falla (429, etc.) usar solo los activos
            all_orders = orders_active

        _amazon_orders_cache[cache_key] = (_time.time(), all_orders)
        return all_orders


async def _get_cached_order_metrics(client, date_from: str, date_to: str) -> list:
    """Obtiene y cachea métricas del Sales API con granularidad=Day.

    date_from / date_to son INCLUSIVE (se agrega 1 día a date_to internamente
    porque Sales API usa intervalos exclusivos en el extremo derecho).

    Retorna lista de dicts con: interval, orderCount, unitCount, totalSales.
    El campo totalSales.amount = Ordered Product Sales (OPS) = lo que muestra
    Amazon Seller Central. Mucho más preciso que sumar OrderTotal de Orders API.
    """
    cache_key = f"metrics:{client.seller_id}:{date_from}:{date_to}"

    # Fast path — sin lock si ya está en caché
    if cache_key in _amazon_metrics_cache:
        ts, data = _amazon_metrics_cache[cache_key]
        if _time.time() - ts < _AMAZON_METRICS_TTL:
            return data

    if cache_key not in _amazon_metrics_locks:
        _amazon_metrics_locks[cache_key] = asyncio.Lock()

    async with _amazon_metrics_locks[cache_key]:
        if cache_key in _amazon_metrics_cache:
            ts, data = _amazon_metrics_cache[cache_key]
            if _time.time() - ts < _AMAZON_METRICS_TTL:
                return data

        # date_to es inclusivo → Sales API espera extremo derecho exclusivo
        date_to_exclusive = (
            datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")

        data = await client.get_order_metrics(
            date_from=date_from,
            date_to_exclusive=date_to_exclusive,
            granularity="Day",
        )
        _amazon_metrics_cache[cache_key] = (_time.time(), data)
        return data


def _build_amazon_chart_from_metrics(metrics_data: list, date_from: str, date_to: str):
    """Construye datos de chart desde respuesta del Sales API (granularity=Day).

    Cada item en metrics_data tiene un campo `interval` como:
      "2026-02-24T00:00:00-08:00--2026-02-25T00:00:00-08:00"
    Los primeros 10 caracteres son la fecha PST del día.
    """
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    delta_days = (end - start).days

    group_by = "day" if delta_days <= 31 else "month"

    buckets: dict = {}
    if group_by == "day":
        for i in range(delta_days + 1):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            buckets[d] = {"orders": 0, "revenue": 0.0}
    else:
        current = start.replace(day=1)
        while current <= end:
            buckets[current.strftime("%Y-%m")] = {"orders": 0, "revenue": 0.0}
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

    for item in metrics_data:
        interval = item.get("interval", "")
        if not interval:
            continue
        date_str = interval[:10]  # "2026-02-24"
        key = date_str if group_by == "day" else date_str[:7]
        if key in buckets:
            buckets[key]["orders"] += int(item.get("orderCount", 0) or 0)
            try:
                buckets[key]["revenue"] += float(
                    (item.get("totalSales") or {}).get("amount", 0) or 0
                )
            except (TypeError, ValueError):
                pass

    chart_data = [
        {"date": d, "orders": v["orders"], "revenue": round(v["revenue"], 2)}
        for d, v in sorted(buckets.items())
    ]
    return chart_data, group_by


def _build_amazon_chart_data(orders: list, date_from: str, date_to: str):
    """Construye datos de chart (órdenes + revenue) agrupados por día o mes."""
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    delta_days = (end - start).days

    if delta_days <= 31:
        group_by = "day"
        buckets: dict = {}
        for i in range(delta_days + 1):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            buckets[d] = {"orders": 0, "revenue": 0.0}

        for order in orders:
            raw_date = order.get("PurchaseDate", "")
            if not raw_date:
                continue
            try:
                order_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                continue
            date_key = order_dt.strftime("%Y-%m-%d")
            if date_key in buckets:
                buckets[date_key]["orders"] += 1
                try:
                    amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
                except (TypeError, ValueError):
                    amount = 0.0
                buckets[date_key]["revenue"] += amount

        chart_data = [
            {"date": d, "orders": v["orders"], "revenue": round(v["revenue"], 2)}
            for d, v in sorted(buckets.items())
        ]
    else:
        group_by = "month"
        buckets = {}
        current = start.replace(day=1)
        while current <= end:
            buckets[current.strftime("%Y-%m")] = {"orders": 0, "revenue": 0.0}
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

        for order in orders:
            raw_date = order.get("PurchaseDate", "")
            if not raw_date:
                continue
            try:
                order_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                continue
            month_key = order_dt.strftime("%Y-%m")
            if month_key in buckets:
                buckets[month_key]["orders"] += 1
                try:
                    amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
                except (TypeError, ValueError):
                    amount = 0.0
                buckets[month_key]["revenue"] += amount

        chart_data = [
            {"date": d, "orders": v["orders"], "revenue": round(v["revenue"], 2)}
            for d, v in sorted(buckets.items())
        ]

    return chart_data, group_by


@router.get("/amazon-dashboard-data")
async def get_amazon_dashboard_data(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    seller_id: str = Query("", description="Seller ID (vacío = primera cuenta)"),
):
    """Métricas + chart de Amazon — usa Sales API para revenue exacto (OPS = Seller Central)."""
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon configurada")

    now = datetime.utcnow()
    if not date_from:
        date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = now.strftime("%Y-%m-%d")

    async def _safe_active_listings():
        try:
            from app.api.amazon_products import _get_listings_cached as _glc
            ls = await _glc(client)
            return sum(1 for l in ls if (l.get("status") or "").lower() == "active")
        except Exception:
            return None

    try:
        # Sales API + listings count en paralelo
        metrics_data, active_listings = await asyncio.gather(
            _get_cached_order_metrics(client, date_from, date_to),
            _safe_active_listings(),
        )
    except Exception as exc:
        empty_chart, _ = _build_amazon_chart_from_metrics([], date_from, date_to)
        return {
            "metrics": {"total_orders": 0, "total_units": 0, "total_revenue": 0.0,
                        "avg_per_order": 0.0, "active_listings": None},
            "chart": {"data": empty_chart, "group_by": "day"},
            "error": str(exc)[:300],
        }

    # Agregar totales desde datos por día del Sales API
    total_orders = sum(int(d.get("orderCount", 0) or 0) for d in metrics_data)
    total_units  = sum(int(d.get("unitCount",  0) or 0) for d in metrics_data)
    total_revenue = sum(
        float((d.get("totalSales") or {}).get("amount", 0) or 0)
        for d in metrics_data
    )
    avg_per_order = (total_revenue / total_orders) if total_orders > 0 else 0.0

    # ── Período anterior para tendencias ──────────────────────────────────────
    try:
        start_dt  = datetime.strptime(date_from, "%Y-%m-%d")
        end_dt    = datetime.strptime(date_to,   "%Y-%m-%d")
        period_len = (end_dt - start_dt).days + 1
        prev_to_dt   = start_dt - timedelta(days=1)
        prev_from_dt = prev_to_dt - timedelta(days=period_len - 1)
        prev_from = prev_from_dt.strftime("%Y-%m-%d")
        prev_to   = prev_to_dt.strftime("%Y-%m-%d")
        prev_data = await _get_cached_order_metrics(client, prev_from, prev_to)
        prev_orders  = sum(int(d.get("orderCount", 0) or 0) for d in prev_data)
        prev_units   = sum(int(d.get("unitCount",  0) or 0) for d in prev_data)
        prev_revenue = sum(float((d.get("totalSales") or {}).get("amount", 0) or 0) for d in prev_data)

        def _pct_change(cur, prev):
            if prev == 0:
                return 100.0 if cur > 0 else 0.0
            return round((cur - prev) / prev * 100, 1)

        trend = {
            "orders_pct":  _pct_change(total_orders, prev_orders),
            "units_pct":   _pct_change(total_units,  prev_units),
            "revenue_pct": _pct_change(total_revenue, prev_revenue),
        }
    except Exception:
        trend = {"orders_pct": 0.0, "units_pct": 0.0, "revenue_pct": 0.0}

    metrics = {
        "total_orders":   total_orders,
        "total_units":    total_units,
        "total_revenue":  round(total_revenue, 2),
        "avg_per_order":  round(avg_per_order, 2),
        "active_listings": active_listings,
        "net_revenue_est": round(total_revenue * 0.85, 2),
        "trend": trend,
    }

    chart_data, group_by = _build_amazon_chart_from_metrics(metrics_data, date_from, date_to)

    return {
        "metrics": metrics,
        "chart": {"data": chart_data, "group_by": group_by},
    }


@router.get("/amazon-goal")
async def get_amazon_goal(
    request: Request,
    seller_id: str = Query("", description="Seller ID (vacío = primera cuenta)"),
):
    """Obtiene la meta diaria de Amazon de la cuenta activa."""
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon")
    goal = await token_store.get_daily_goal(f"amz_{client.seller_id}")
    return {"seller_id": client.seller_id, "daily_goal": goal}


@router.post("/amazon-goal")
async def set_amazon_goal(request: Request):
    """Guarda la meta diaria de Amazon de la cuenta activa."""
    body = await request.json()
    sid = body.get("seller_id", "") or ""
    client = await get_amazon_client(seller_id=sid or None)
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon")
    goal = float(body.get("daily_goal", 50000))
    await token_store.set_daily_goal(f"amz_{client.seller_id}", goal)
    return {"seller_id": client.seller_id, "daily_goal": goal}


@router.get("/amazon-daily-sales-data")
async def get_amazon_daily_sales_data(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    goal: float = Query(0, description="Meta diaria (0 = leer de DB)"),
    seller_id: str = Query("", description="Seller ID (vacío = primera cuenta)"),
):
    """Ventas diarias de Amazon — usa Sales API para revenue exacto (OPS = Seller Central)."""
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon")

    now = datetime.utcnow()
    if not date_from:
        date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = now.strftime("%Y-%m-%d")
    if goal <= 0:
        goal = await token_store.get_daily_goal(f"amz_{client.seller_id}")

    try:
        # Sales API con granularidad=Day — un item por día con orderCount, unitCount, totalSales
        metrics_data = await _get_cached_order_metrics(client, date_from, date_to)
    except Exception as exc:
        return {
            "daily_data": [],
            "goal": goal,
            "totals": {"orders": 0, "units": 0, "revenue": 0.0, "days_met": 0,
                       "avg_pct": 0.0, "total_days": 0, "days_with_sales": 0},
            "error": str(exc)[:300],
        }

    # Crear buckets vacíos para cada día del rango
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    buckets: dict[str, dict] = {}
    cur = start
    while cur <= end:
        buckets[cur.strftime("%Y-%m-%d")] = {"orders": 0, "units": 0, "revenue": 0.0}
        cur += timedelta(days=1)

    # Mapear datos del Sales API a buckets por fecha
    for item in metrics_data:
        interval = item.get("interval", "")
        if not interval:
            continue
        date_str = interval[:10]  # "2026-02-24" desde "2026-02-24T00:00:00-08:00--..."
        if date_str not in buckets:
            continue
        buckets[date_str]["orders"] += int(item.get("orderCount", 0) or 0)
        buckets[date_str]["units"]  += int(item.get("unitCount",  0) or 0)
        try:
            buckets[date_str]["revenue"] += float(
                (item.get("totalSales") or {}).get("amount", 0) or 0
            )
        except (TypeError, ValueError):
            pass

    # ── Fallback real-time para hoy (Sales API tiene lag de 2-4 horas) ────────
    # Si el bucket de hoy tiene 0 órdenes, consultar Orders API que sí es real-time.
    try:
        import zoneinfo as _zi
        _la = _zi.ZoneInfo("America/Los_Angeles")
        _offset_h = int(datetime.now(_la).utcoffset().total_seconds() // 3600)
    except Exception:
        _offset_h = -7  # PDT fallback (abril–octubre)

    now_pac = now + timedelta(hours=_offset_h)
    today_pac = now_pac.strftime("%Y-%m-%d")

    if today_pac in buckets and buckets[today_pac]["orders"] == 0:
        try:
            # Medianoche Pacific → UTC
            midnight_pac = now_pac.replace(hour=0, minute=0, second=0, microsecond=0)
            midnight_utc_str = (midnight_pac - timedelta(hours=_offset_h)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            today_orders = await client.get_orders(
                created_after=midnight_utc_str,
                order_statuses=["Shipped", "Unshipped", "PartiallyShipped"],
            )
            if today_orders:
                buckets[today_pac]["orders"] = len(today_orders)
                buckets[today_pac]["units"] = sum(
                    int(o.get("NumberOfItemsShipped", 0) or 0)
                    + int(o.get("NumberOfItemsUnshipped", 0) or 0)
                    for o in today_orders
                )
                buckets[today_pac]["revenue"] = round(
                    sum(
                        float(o.get("OrderTotal", {}).get("Amount", 0) or 0)
                        for o in today_orders
                        if o.get("OrderStatus") not in ("Cancelled",)
                    ),
                    2,
                )
        except Exception:
            pass  # fallback silencioso — peor caso sigue mostrando 0

    daily_data = []
    for date_key in sorted(buckets.keys(), reverse=True):
        d = buckets[date_key]
        pct = (d["revenue"] / goal * 100) if goal > 0 else 0
        daily_data.append({
            "date": date_key,
            "orders": d["orders"],
            "units": d["units"],
            "revenue": round(d["revenue"], 2),
            "net_est": round(d["revenue"] * 0.85, 2),
            "pct_of_goal": round(pct, 1),
        })

    total_orders  = sum(d["orders"]  for d in daily_data)
    total_units   = sum(d["units"]   for d in daily_data)
    total_revenue = round(sum(d["revenue"] for d in daily_data), 2)
    days_met = sum(1 for d in daily_data if d["pct_of_goal"] >= 100)
    avg_pct = round(sum(d["pct_of_goal"] for d in daily_data) / len(daily_data), 1) if daily_data else 0

    return {
        "daily_data": daily_data,
        "goal": goal,
        "totals": {
            "orders":          total_orders,
            "units":           total_units,
            "revenue":         total_revenue,
            "days_met":        days_met,
            "avg_pct":         avg_pct,
            "total_days":      len(daily_data),
            "days_with_sales": sum(1 for d in daily_data if d["orders"] > 0),
        },
    }


@router.get("/amazon-debug-today")
async def get_amazon_debug_today():
    """Diagnóstico: compara Sales API vs Orders API para el día de hoy."""
    client = await get_amazon_client()
    if not client:
        return {"error": "Sin cuenta Amazon"}

    now = datetime.utcnow()
    # Hoy en Pacific (PDT=UTC-7 en abril)
    try:
        import zoneinfo as _zi
        _la = _zi.ZoneInfo("America/Los_Angeles")
        _offset_h = int(datetime.now(_la).utcoffset().total_seconds() // 3600)
    except Exception:
        _offset_h = -8
    now_pac = now + timedelta(hours=_offset_h)
    today_pac = now_pac.strftime("%Y-%m-%d")

    # Sales API (OPS diario)
    try:
        metrics = await _get_cached_order_metrics(client, today_pac, today_pac)
        sales_orders = sum(int(d.get("orderCount", 0) or 0) for d in metrics)
        sales_units  = sum(int(d.get("unitCount", 0) or 0) for d in metrics)
        sales_rev    = sum(float((d.get("totalSales") or {}).get("amount", 0) or 0) for d in metrics)
        sales_error  = None
    except Exception as e:
        sales_orders = sales_units = sales_rev = 0
        sales_error = str(e)[:200]

    # Orders API (tiempo real)
    try:
        today_str_utc = f"{today_pac}T00:00:00Z"
        now_str_utc   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        orders_raw = await client.get_orders(
            created_after=today_str_utc,
            created_before=now_str_utc,
            order_statuses=["Shipped", "Unshipped", "PartiallyShipped", "Pending"],
        )
        ord_orders = len(orders_raw)
        ord_units  = sum(int(o.get("NumberOfItemsShipped", 0) or 0) +
                         int(o.get("NumberOfItemsUnshipped", 0) or 0) for o in orders_raw)
        ord_rev    = sum(float(o.get("OrderTotal", {}).get("Amount", 0) or 0) for o in orders_raw)
        ord_error  = None
    except Exception as e:
        ord_orders = ord_units = ord_rev = 0
        ord_error = str(e)[:200]

    return {
        "seller_id":   client.seller_id,
        "marketplace": client.marketplace_id,
        "today_pac":   today_pac,
        "utc_now":     now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pacific_offset_h": _offset_h,
        "sales_api": {
            "orders": sales_orders, "units": sales_units,
            "revenue": round(sales_rev, 2), "error": sales_error,
            "raw_intervals": len(metrics) if not sales_error else 0,
        },
        "orders_api": {
            "orders": ord_orders, "units": ord_units,
            "revenue": round(ord_rev, 2), "error": ord_error,
        },
    }


@router.get("/amazon-recent-orders", response_class=HTMLResponse)
async def get_amazon_recent_orders(
    request: Request,
    seller_id: str = Query("", description="Seller ID (vacío = primera cuenta)"),
):
    """Últimas 5 órdenes de Amazon — HTML partial para el dashboard."""
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        return HTMLResponse(
            '<p class="text-center text-gray-400 py-6 text-sm">Sin cuenta Amazon conectada</p>'
        )

    now = datetime.utcnow()
    # IMPORTANTE: usar el mismo rango que amazon-dashboard-data (29 días)
    # para que compartan el mismo cache key y evitar 429 QuotaExceeded
    date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    try:
        orders = await _get_cached_amazon_orders(client, date_from, date_to)
    except Exception as exc:
        return HTMLResponse(
            f'<p class="text-center text-red-400 py-6 text-sm">Error: {str(exc)[:120]}</p>'
        )

    valid = [o for o in orders if o.get("OrderStatus") in ("Shipped", "Delivered", "Unshipped", "Pending")]
    valid.sort(key=lambda o: o.get("PurchaseDate", ""), reverse=True)
    recent = valid[:5]

    # Enriquecer con items (ASIN, SKU, Title, Qty, Price) — en paralelo
    async def _fetch_items(order: dict) -> dict:
        try:
            items = await client.get_order_items(order.get("AmazonOrderId", ""))
            order = dict(order)
            order["_items"] = items
        except Exception:
            order = dict(order)
            order["_items"] = []
        return order

    recent = list(await asyncio.gather(*[_fetch_items(o) for o in recent]))

    return _templates.TemplateResponse(
        request, "partials/amazon_recent_orders.html", {"orders": recent},
    )


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON — Salud de la cuenta (tab Salud del dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def _amazon_health_alerts(cancel_rate: float, unshipped: int, unfulfillable: int) -> list:
    alerts = []
    if cancel_rate >= 5:
        alerts.append({"level": "error", "msg": f"Tasa de cancelación crítica: {cancel_rate:.1f}% (límite Amazon: 2.5%)"})
    elif cancel_rate >= 2.5:
        alerts.append({"level": "warning", "msg": f"Tasa de cancelación elevada: {cancel_rate:.1f}% (recomendado: <2.5%)"})
    if unshipped >= 10:
        alerts.append({"level": "warning", "msg": f"{unshipped} órdenes pendientes sin enviar"})
    if unfulfillable > 0:
        alerts.append({"level": "warning", "msg": f"{unfulfillable} unidades no vendibles en FBA (dañadas/vencidas)"})
    if not alerts:
        alerts.append({"level": "success", "msg": "Cuenta saludable — sin alertas activas en los últimos 30 días"})
    return alerts


@router.get("/amazon-health-data")
async def get_amazon_health_data(
    seller_id: str = Query("", description="Seller ID (vacío = primera cuenta)"),
):
    """Métricas de salud de la cuenta Amazon: órdenes, FBA, cancelaciones."""
    client = await get_amazon_client(seller_id=seller_id or None)
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon configurada")

    now = datetime.utcnow()
    date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    # ── 1. Órdenes de los últimos 30 días (desde caché compartida) ────────
    try:
        orders = await _get_cached_amazon_orders(client, date_from, date_to)
    except Exception:
        orders = []  # Soft fallback — show zeroed health instead of 503

    by_status: dict[str, int] = {}
    for o in orders:
        s = o.get("OrderStatus", "Unknown")
        by_status[s] = by_status.get(s, 0) + 1

    total_orders = len(orders)
    shipped   = by_status.get("Shipped", 0) + by_status.get("Delivered", 0)
    unshipped = by_status.get("Unshipped", 0)
    pending   = by_status.get("Pending", 0)
    canceled  = by_status.get("Canceled", 0)
    cancel_rate = round(canceled / total_orders * 100, 1) if total_orders > 0 else 0.0

    # ── Late Shipment Rate — count Unshipped orders past LatestShipDate ───
    late_ship_count = 0
    now_utc = now.replace(tzinfo=None)
    for o in orders:
        if o.get("OrderStatus") == "Unshipped":
            lsd = o.get("LatestShipDate", "")
            if lsd:
                try:
                    lsd_dt = datetime.strptime(lsd[:19], "%Y-%m-%dT%H:%M:%S")
                    if now_utc > lsd_dt:
                        late_ship_count += 1
                except ValueError:
                    pass
    # Denominator: shipped + unshipped orders (exclude Pending/Canceled)
    fulfillment_base = shipped + unshipped + late_ship_count
    late_ship_rate = round(late_ship_count / fulfillment_base * 100, 1) if fulfillment_base > 0 else 0.0

    # ── 2. Inventario FBA (primer página ≤ 50 SKUs — rápido) ─────────────
    try:
        fba_items = await client.get_fba_inventory()
    except Exception:
        fba_items = []

    fulfillable   = 0
    unfulfillable = 0
    reserved      = 0
    inbound       = 0
    for item in fba_items:
        inv = item.get("inventoryDetails", {}) or {}
        fulfillable   += inv.get("fulfillableQuantity", 0) or 0
        unf = inv.get("unfulfillableQuantity", {}) or {}
        unfulfillable += unf.get("totalUnfulfillableQuantity", 0) or 0
        res = inv.get("reservedQuantity", {}) or {}
        reserved  += res.get("pendingCustomerOrderQuantity", 0) or 0
        inbound   += (inv.get("inboundWorkingQuantity", 0) or 0) + (inv.get("inboundShippedQuantity", 0) or 0)

    # ── 3. Score de salud (0–100) ─────────────────────────────────────────
    # Cancelaciones (40 pts): 0% → 40, 10% → 0
    cancel_score      = max(0, int(40 - cancel_rate * 4))
    # Órdenes sin enviar (30 pts): 0% → 30, 15% → 0
    unshipped_rate    = unshipped / max(total_orders, 1) * 100
    unshipped_score   = max(0, int(30 - unshipped_rate * 2))
    # Unidades no vendibles FBA (30 pts): 0 → 30, ≥30 → 0
    unfulfillable_score = max(0, 30 - min(unfulfillable, 30))
    health_score = cancel_score + unshipped_score + unfulfillable_score

    return {
        "orders": {
            "total_30d":       total_orders,
            "shipped":         shipped,
            "unshipped":       unshipped,
            "pending":         pending,
            "canceled":        canceled,
            "cancel_rate":     cancel_rate,
            "late_ship_count": late_ship_count,
            "late_ship_rate":  late_ship_rate,
            "by_status":       by_status,
        },
        "fba": {
            "sku_count":     len(fba_items),
            "fulfillable":   fulfillable,
            "unfulfillable": unfulfillable,
            "reserved":      reserved,
            "inbound":       inbound,
        },
        "health_score": health_score,
        "alerts": _amazon_health_alerts(cancel_rate, unshipped, unfulfillable),
        "nickname": client.nickname,
        "marketplace": client.marketplace_name,
    }


@router.get("/account-balance")
async def get_account_balance():
    """Obtiene el balance disponible en Mercado Pago y fondos pendientes en Amazon."""
    meli_task = asyncio.create_task(_get_meli_balance())
    amazon_task = asyncio.create_task(_get_amazon_balance())
    meli_result, amazon_result = await asyncio.gather(meli_task, amazon_task, return_exceptions=True)

    if isinstance(meli_result, Exception):
        meli_result = {"error": str(meli_result)}
    if isinstance(amazon_result, Exception):
        amazon_result = {"error": str(amazon_result)}

    return {
        "mercadolibre": meli_result,
        "amazon": amazon_result,
    }


async def _get_meli_balance() -> dict:
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado en Mercado Libre"}
    try:
        balance = await client.get_account_balance()
        return balance
    finally:
        await client.close()


async def _get_amazon_balance() -> dict:
    client = await get_amazon_client()
    if not client:
        return {"error": "No autenticado en Amazon"}
    return await client.get_account_balance()
