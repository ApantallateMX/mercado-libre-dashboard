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

        # Metricas
        paid_orders = [o for o in all_orders if o.get("status") in ["paid", "delivered"]]
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
        if not date_from:
            date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        if not date_to:
            date_to = now.strftime("%Y-%m-%d")
        # Leer meta de DB si no se pasa como parámetro
        if goal <= 0:
            goal = await token_store.get_daily_goal(client.user_id)

        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)

        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")

        buckets = {}
        cur = start
        while cur <= end:
            buckets[cur.strftime("%Y-%m-%d")] = {"units": 0, "revenue_gross": 0.0, "revenue_net": 0.0}
            cur += timedelta(days=1)

        for order in all_orders:
            if order.get("status") not in ["paid", "delivered"]:
                continue
            order_date = datetime.fromisoformat(
                order["date_created"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            date_key = order_date.strftime("%Y-%m-%d")
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

        for order in all_orders:
            if order.get("status") not in ["paid", "delivered"]:
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

        for order in all_orders:
            if order.get("status") not in ["paid", "delivered"]:
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
            "partials/amazon_daily_card.html",
            {
                "request": request,
                "not_connected": True,
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
            cached["request"] = request  # request no es serializable — se reconstruye
            return _templates.TemplateResponse("partials/amazon_daily_card.html", cached)

    # ── 4. Obtener órdenes via SP-API ─────────────────────────────────────────
    try:
        # fetch_orders_range obtiene TODAS las órdenes del rango (paginando internamente)
        orders = await client.fetch_orders_range(date_from=date_from, date_to=date_to)
    except Exception as exc:
        return _templates.TemplateResponse(
            "partials/amazon_daily_card.html",
            {
                "request": request,
                "error": str(exc),
                "daily_data": [],
                "totals": {},
                "date_from": date_from,
                "date_to": date_to,
                "nickname": client.nickname,
                "marketplace": client.marketplace_name,
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
        "request":      request,
        "daily_data":   daily_data,
        "totals":       totals,
        "date_from":    date_from,
        "date_to":      date_to,
        "nickname":     client.nickname,
        "marketplace":  client.marketplace_name,
        "not_connected": False,
        "error":        None,
    }
    # Guardar sin `request` (no serializable)
    _amazon_daily_cache[cache_key] = (_time.time(), {k: v for k, v in ctx.items() if k != "request"})

    return _templates.TemplateResponse("partials/amazon_daily_card.html", ctx)


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON — Nuevos endpoints para el dashboard rediseñado
# ─────────────────────────────────────────────────────────────────────────────

# Caché de órdenes crudas — compartida entre todos los endpoints Amazon
_amazon_orders_cache: dict[str, tuple[float, list]] = {}
_amazon_orders_locks: dict[str, asyncio.Lock] = {}
_AMAZON_ORDERS_TTL = 300  # 5 minutos


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
        orders = await client.fetch_orders_range(date_from=date_from, date_to=date_to)
        _amazon_orders_cache[cache_key] = (_time.time(), orders)
        return orders


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
):
    """Métricas + chart de Amazon en una sola llamada."""
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon configurada")

    now = datetime.utcnow()
    if not date_from:
        date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = now.strftime("%Y-%m-%d")

    try:
        orders = await _get_cached_amazon_orders(client, date_from, date_to)
    except Exception as exc:
        empty_chart, _ = _build_amazon_chart_data([], date_from, date_to)
        return {
            "metrics": {"total_orders": 0, "shipped_orders": 0, "total_revenue": 0.0,
                        "avg_per_order": 0.0, "total_units": 0},
            "chart": {"data": empty_chart, "group_by": "day"},
            "error": str(exc)[:300],
        }

    # Excluir solo Cancelled — igual que Amazon Seller Central que muestra todos los demás
    CANCELLED = {"Canceled", "Cancelled"}
    valid_orders = [o for o in orders if o.get("OrderStatus") not in CANCELLED]

    # Órdenes "shipped" = ya procesadas (Shipped + Delivered + PartiallyShipped)
    shipped_statuses = {"Shipped", "Delivered", "PartiallyShipped"}
    shipped_orders = [o for o in valid_orders if o.get("OrderStatus") in shipped_statuses]

    total_revenue = 0.0
    total_units = 0
    for order in valid_orders:
        try:
            amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        total_revenue += amount
        total_units += int(order.get("NumberOfItemsShipped", 0) or 0)
        total_units += int(order.get("NumberOfItemsUnshipped", 0) or 0)

    avg_per_order = (total_revenue / len(valid_orders)) if valid_orders else 0.0

    metrics = {
        "total_orders": len(valid_orders),
        "shipped_orders": len(shipped_orders),
        "total_revenue": round(total_revenue, 2),
        "avg_per_order": round(avg_per_order, 2),
        "total_units": total_units,
    }

    chart_data, group_by = _build_amazon_chart_data(valid_orders, date_from, date_to)

    return {
        "metrics": metrics,
        "chart": {"data": chart_data, "group_by": group_by},
    }


@router.get("/amazon-goal")
async def get_amazon_goal(request: Request):
    """Obtiene la meta diaria de Amazon de la cuenta activa."""
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon")
    goal = await token_store.get_daily_goal(f"amz_{client.seller_id}")
    return {"seller_id": client.seller_id, "daily_goal": goal}


@router.post("/amazon-goal")
async def set_amazon_goal(request: Request):
    """Guarda la meta diaria de Amazon de la cuenta activa."""
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon")
    body = await request.json()
    goal = float(body.get("daily_goal", 50000))
    await token_store.set_daily_goal(f"amz_{client.seller_id}", goal)
    return {"seller_id": client.seller_id, "daily_goal": goal}


@router.get("/amazon-daily-sales-data")
async def get_amazon_daily_sales_data(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    goal: float = Query(0, description="Meta diaria (0 = leer de DB)"),
):
    """Ventas diarias de Amazon como JSON con % de meta — para el dashboard rediseñado."""
    client = await get_amazon_client()
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
        orders = await _get_cached_amazon_orders(client, date_from, date_to)
    except Exception as exc:
        return {
            "daily_data": [],
            "goal": goal,
            "totals": {"orders": 0, "units": 0, "revenue": 0.0, "days_met": 0,
                       "avg_pct": 0.0, "total_days": 0, "days_with_sales": 0},
            "error": str(exc)[:300],
        }

    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    buckets: dict[str, dict] = {}
    cur = start
    while cur <= end:
        buckets[cur.strftime("%Y-%m-%d")] = {"orders": 0, "units": 0, "revenue": 0.0}
        cur += timedelta(days=1)

    for order in orders:
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
        status = order.get("OrderStatus", "")
        # Excluir solo canceladas — igual que Amazon Seller Central
        if status in ("Canceled", "Cancelled"):
            continue
        buckets[date_key]["orders"] += 1
        try:
            amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        buckets[date_key]["revenue"] += amount
        buckets[date_key]["units"] += int(order.get("NumberOfItemsShipped", 0) or 0)
        buckets[date_key]["units"] += int(order.get("NumberOfItemsUnshipped", 0) or 0)

    daily_data = []
    for date_key in sorted(buckets.keys(), reverse=True):
        d = buckets[date_key]
        pct = (d["revenue"] / goal * 100) if goal > 0 else 0
        daily_data.append({
            "date": date_key,
            "orders": d["orders"],
            "units": d["units"],
            "revenue": round(d["revenue"], 2),
            "pct_of_goal": round(pct, 1),
        })

    total_orders = sum(d["orders"] for d in daily_data)
    total_units = sum(d["units"] for d in daily_data)
    total_revenue = round(sum(d["revenue"] for d in daily_data), 2)
    days_met = sum(1 for d in daily_data if d["pct_of_goal"] >= 100)
    avg_pct = round(sum(d["pct_of_goal"] for d in daily_data) / len(daily_data), 1) if daily_data else 0

    return {
        "daily_data": daily_data,
        "goal": goal,
        "totals": {
            "orders": total_orders,
            "units": total_units,
            "revenue": total_revenue,
            "days_met": days_met,
            "avg_pct": avg_pct,
            "total_days": len(daily_data),
            "days_with_sales": sum(1 for d in daily_data if d["orders"] > 0),
        },
    }


@router.get("/amazon-recent-orders", response_class=HTMLResponse)
async def get_amazon_recent_orders(request: Request):
    """Últimas 5 órdenes de Amazon — HTML partial para el dashboard."""
    client = await get_amazon_client()
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
        "partials/amazon_recent_orders.html",
        {"request": request, "orders": recent},
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
async def get_amazon_health_data():
    """Métricas de salud de la cuenta Amazon: órdenes, FBA, cancelaciones."""
    client = await get_amazon_client()
    if not client:
        raise HTTPException(status_code=404, detail="Sin cuenta Amazon configurada")

    now = datetime.utcnow()
    date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    # ── 1. Órdenes de los últimos 30 días (desde caché compartida) ────────
    try:
        orders = await _get_cached_amazon_orders(client, date_from, date_to)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

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
            "total_30d":   total_orders,
            "shipped":     shipped,
            "unshipped":   unshipped,
            "pending":     pending,
            "canceled":    canceled,
            "cancel_rate": cancel_rate,
            "by_status":   by_status,
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
