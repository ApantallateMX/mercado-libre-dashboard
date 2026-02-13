from fastapi import APIRouter, HTTPException, Query
from datetime import datetime, timedelta
from collections import defaultdict
from app.services.meli_client import get_meli_client
from app import order_net_revenue

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


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
    goal: float = Query(500000, description="Meta diaria en MXN"),
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

        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)
        # Enriquecer con net_received_amount real de MeLi (incluye todos los cargos e impuestos)
        await client.enrich_orders_with_net_amount(all_orders)

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
