"""
sales.py — API externa v1: Ventas reales por período

Endpoints:
  GET /api/v1/sales/daily?from=YYYY-MM-DD&to=YYYY-MM-DD
  GET /api/v1/sales/weekly?from=YYYY-MM-DD&to=YYYY-MM-DD
  GET /api/v1/sales/monthly?year=2026
  GET /api/v1/sales/summary?days=30

Auth requerido: Header X-API-Key: sk_live_xxx

Fuentes de datos:
  - MeLi: Orders API → órdenes con status=paid, revenue neto = total - comisión - IVA comisión
  - Amazon: Sales API (orderMetrics) → OPS real (mismo número que Seller Central)

Nota sobre neto MeLi:
  net = total_amount - sale_fee - (sale_fee * 0.16)
  No incluye costo de envío (requeriría una llamada por orden — demasiado lento).
  Para envíos gratuitos para el comprador, el neto es exacto.
"""

import asyncio
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import order_net_revenue
from app.api.v1.auth import require_api_key
from app.services import token_store
from app.services.meli_client import get_meli_client

router = APIRouter(prefix="/api/v1/sales", tags=["v1-sales"])

# ─── Cache simple (evita golpear APIs en cada request) ───────────────────────
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 5 * 60  # 5 minutos


def _cache_get(key: str):
    if key in _cache:
        ts, data = _cache[key]
        if _time.time() - ts < _CACHE_TTL:
            return data
    return None


def _cache_set(key: str, data: dict):
    _cache[key] = (_time.time(), data)


# ─── Helpers de fechas ────────────────────────────────────────────────────────

def _parse_date(s: str, param: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Formato inválido para '{param}'. Usar YYYY-MM-DD")


def _validate_range(d_from: date, d_to: date):
    if d_from > d_to:
        raise HTTPException(status_code=422, detail="'from' debe ser anterior o igual a 'to'")
    if (d_to - d_from).days > 366:
        raise HTTPException(status_code=422, detail="Rango máximo: 366 días por request")


def _week_label(d: date) -> str:
    """Semana ISO: '2026-W10'"""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_range(d: date) -> tuple[str, str]:
    """Lunes y domingo de la semana ISO."""
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


# ─── Fetch MeLi orders para TODOS los usuarios en rango de fechas ─────────────

async def _fetch_meli_orders_range(date_from: date, date_to: date) -> list[dict]:
    """Retorna todas las órdenes pagadas de todas las cuentas MeLi en el rango."""
    accounts = await token_store.get_all_tokens()
    if not accounts:
        return []

    all_orders = []

    async def _fetch_account(acc: dict):
        uid = acc.get("user_id", "")
        nickname = acc.get("nickname", uid)
        try:
            client = await get_meli_client(user_id=uid)
            date_from_str = date_from.strftime("%Y-%m-%dT00:00:00.000Z")
            date_to_str   = date_to.strftime("%Y-%m-%dT23:59:59.000Z")

            offset = 0
            limit  = 50
            while True:
                data = await client.get(
                    "/orders/search",
                    params={
                        "seller":                     uid,
                        "order.date_created.from":    date_from_str,
                        "order.date_created.to":      date_to_str,
                        "order.status":               "paid",
                        "offset":                     offset,
                        "limit":                      limit,
                        "sort":                       "date_asc",
                    },
                )
                results = data.get("results", [])
                for o in results:
                    o["_account_nickname"] = nickname
                    o["_account_uid"] = uid
                all_orders.extend(results)

                total = data.get("paging", {}).get("total", 0)
                offset += limit
                if offset >= total or not results:
                    break

                # Rate limit suave
                await asyncio.sleep(0.2)

        except Exception as e:
            print(f"[API-v1] Error fetching MeLi orders for {uid}: {e}")

    await asyncio.gather(*[_fetch_account(a) for a in accounts])
    return all_orders


def _meli_order_to_day_entry(order: dict) -> dict:
    """Extrae fecha, gross, net y fees de una orden MeLi."""
    dt_str = order.get("date_created", "")
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).replace(tzinfo=None)
        day = dt.strftime("%Y-%m-%d")
    except Exception:
        day = dt_str[:10]

    gross = order.get("total_amount", 0) or 0
    fee   = sum(item.get("sale_fee", 0) or 0 for item in order.get("order_items", []))
    iva   = round(fee * 0.16, 2)
    net   = round(gross - fee - iva, 2)

    return {
        "date":    day,
        "order_id": str(order.get("id", "")),
        "gross":   round(gross, 2),
        "fee":     round(fee, 2),
        "iva_fee": iva,
        "net":     net,
        "account": order.get("_account_nickname", ""),
    }


# ─── Fetch Amazon métricas diarias ────────────────────────────────────────────

async def _fetch_amazon_daily(date_from: date, date_to: date) -> list[dict]:
    """Retorna métricas diarias reales de Amazon Sales API (OPS)."""
    amazon_accounts = await token_store.get_all_amazon_accounts()
    if not amazon_accounts:
        return []

    all_days = []

    for acc in amazon_accounts:
        sid  = acc.get("seller_id", "")
        nick = acc.get("nickname", sid)
        try:
            from app.services.amazon_client import get_amazon_client
            client = await get_amazon_client(seller_id=sid)
            if not client:
                continue

            date_to_excl = (date_to + timedelta(days=1)).strftime("%Y-%m-%d")
            data = await client.get_order_metrics(
                date_from=date_from.strftime("%Y-%m-%d"),
                date_to_exclusive=date_to_excl,
                granularity="Day",
            )

            for item in (data or []):
                interval = item.get("interval", "")
                day = interval[:10] if interval else ""
                sales = item.get("totalSales", {})
                gross = float(sales.get("amount", 0) or 0)
                all_days.append({
                    "date":    day,
                    "orders":  item.get("orderCount", 0),
                    "units":   item.get("unitCount", 0),
                    "gross":   round(gross, 2),
                    "account": nick,
                })
        except Exception as e:
            print(f"[API-v1] Error fetching Amazon metrics for {sid}: {e}")

    return all_days


# ─── Agregadores ──────────────────────────────────────────────────────────────

def _aggregate_daily(meli_orders: list[dict], amazon_days: list[dict],
                     date_from: date, date_to: date) -> list[dict]:
    """Agrupa datos por día. Incluye todos los días del rango aunque no haya ventas."""
    # MeLi: agrupar por fecha
    meli_by_day: dict[str, dict] = defaultdict(lambda: {"orders": 0, "gross": 0.0, "fee": 0.0, "iva_fee": 0.0, "net": 0.0})
    for o in meli_orders:
        d = o["date"]
        meli_by_day[d]["orders"] += 1
        meli_by_day[d]["gross"]  += o["gross"]
        meli_by_day[d]["fee"]    += o["fee"]
        meli_by_day[d]["iva_fee"] += o["iva_fee"]
        meli_by_day[d]["net"]    += o["net"]

    # Amazon: agrupar por fecha (puede haber varios sellers)
    amz_by_day: dict[str, dict] = defaultdict(lambda: {"orders": 0, "units": 0, "gross": 0.0})
    for a in amazon_days:
        d = a["date"]
        amz_by_day[d]["orders"] += a["orders"]
        amz_by_day[d]["units"]  += a["units"]
        amz_by_day[d]["gross"]  += a["gross"]

    result = []
    current = date_from
    while current <= date_to:
        ds = current.strftime("%Y-%m-%d")
        m  = meli_by_day.get(ds, {})
        az = amz_by_day.get(ds, {})

        meli_net   = round(m.get("net", 0.0), 2)
        amazon_net = round(az.get("gross", 0.0), 2)

        result.append({
            "date": ds,
            "meli": {
                "orders": m.get("orders", 0),
                "gross":  round(m.get("gross", 0.0), 2),
                "fees":   round(m.get("fee", 0.0) + m.get("iva_fee", 0.0), 2),
                "net":    meli_net,
            },
            "amazon": {
                "orders": az.get("orders", 0),
                "units":  az.get("units", 0),
                "gross":  amazon_net,
            },
            "total_net": round(meli_net + amazon_net, 2),
        })
        current += timedelta(days=1)

    return result


def _aggregate_weekly(daily: list[dict]) -> list[dict]:
    """Agrupa días en semanas ISO."""
    weeks: dict[str, dict] = {}
    for day in daily:
        d = datetime.strptime(day["date"], "%Y-%m-%d").date()
        wk = _week_label(d)
        if wk not in weeks:
            start, end = _week_range(d)
            weeks[wk] = {
                "week": wk, "week_start": start, "week_end": end,
                "meli":   {"orders": 0, "gross": 0.0, "fees": 0.0, "net": 0.0},
                "amazon": {"orders": 0, "units": 0,   "gross": 0.0},
                "total_net": 0.0,
            }
        w = weeks[wk]
        w["meli"]["orders"] += day["meli"]["orders"]
        w["meli"]["gross"]  += day["meli"]["gross"]
        w["meli"]["fees"]   += day["meli"]["fees"]
        w["meli"]["net"]    += day["meli"]["net"]
        w["amazon"]["orders"] += day["amazon"]["orders"]
        w["amazon"]["units"]  += day["amazon"]["units"]
        w["amazon"]["gross"]  += day["amazon"]["gross"]
        w["total_net"]        += day["total_net"]

    # Redondear
    result = []
    for wk in sorted(weeks.keys()):
        w = weeks[wk]
        w["meli"]   = {k: round(v, 2) if isinstance(v, float) else v for k, v in w["meli"].items()}
        w["amazon"] = {k: round(v, 2) if isinstance(v, float) else v for k, v in w["amazon"].items()}
        w["total_net"] = round(w["total_net"], 2)
        result.append(w)
    return result


def _aggregate_monthly(daily: list[dict]) -> list[dict]:
    """Agrupa días en meses."""
    months: dict[str, dict] = {}
    for day in daily:
        mo = day["date"][:7]  # "YYYY-MM"
        if mo not in months:
            months[mo] = {
                "month": mo,
                "meli":   {"orders": 0, "gross": 0.0, "fees": 0.0, "net": 0.0},
                "amazon": {"orders": 0, "units": 0,   "gross": 0.0},
                "total_net": 0.0,
            }
        m = months[mo]
        m["meli"]["orders"] += day["meli"]["orders"]
        m["meli"]["gross"]  += day["meli"]["gross"]
        m["meli"]["fees"]   += day["meli"]["fees"]
        m["meli"]["net"]    += day["meli"]["net"]
        m["amazon"]["orders"] += day["amazon"]["orders"]
        m["amazon"]["units"]  += day["amazon"]["units"]
        m["amazon"]["gross"]  += day["amazon"]["gross"]
        m["total_net"]        += day["total_net"]

    result = []
    for mo in sorted(months.keys()):
        m = months[mo]
        m["meli"]   = {k: round(v, 2) if isinstance(v, float) else v for k, v in m["meli"].items()}
        m["amazon"] = {k: round(v, 2) if isinstance(v, float) else v for k, v in m["amazon"].items()}
        m["total_net"] = round(m["total_net"], 2)
        result.append(m)
    return result


def _build_totals(daily: list[dict]) -> dict:
    """Calcula totales del período completo."""
    meli_orders = sum(d["meli"]["orders"] for d in daily)
    meli_gross  = round(sum(d["meli"]["gross"] for d in daily), 2)
    meli_fees   = round(sum(d["meli"]["fees"] for d in daily), 2)
    meli_net    = round(sum(d["meli"]["net"] for d in daily), 2)
    amz_orders  = sum(d["amazon"]["orders"] for d in daily)
    amz_units   = sum(d["amazon"]["units"] for d in daily)
    amz_gross   = round(sum(d["amazon"]["gross"] for d in daily), 2)
    total_net   = round(meli_net + amz_gross, 2)
    days_active = sum(1 for d in daily if d["total_net"] > 0)

    return {
        "meli":   {"orders": meli_orders, "gross": meli_gross, "fees": meli_fees, "net": meli_net},
        "amazon": {"orders": amz_orders, "units": amz_units, "gross": amz_gross},
        "combined": {
            "total_net":   total_net,
            "total_orders": meli_orders + amz_orders,
            "days_active": days_active,
            "days_total":  len(daily),
        },
    }


# ─── Fetch compartido (con cache) ─────────────────────────────────────────────

async def _get_daily_data(date_from: date, date_to: date) -> list[dict]:
    cache_key = f"daily:{date_from}:{date_to}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    meli_orders, amazon_days = await asyncio.gather(
        _fetch_meli_orders_range(date_from, date_to),
        _fetch_amazon_daily(date_from, date_to),
    )

    # Convertir órdenes MeLi a entradas por día
    meli_entries = [_meli_order_to_day_entry(o) for o in meli_orders]
    daily = _aggregate_daily(meli_entries, amazon_days, date_from, date_to)
    _cache_set(cache_key, daily)
    return daily


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

@router.get("/daily", summary="Ventas reales por día")
async def sales_daily(
    from_date: str = Query(..., alias="from", description="Fecha inicio YYYY-MM-DD"),
    to_date:   str = Query(..., alias="to",   description="Fecha fin YYYY-MM-DD"),
    _key: str = Depends(require_api_key),
):
    """
    Ventas reales de MeLi + Amazon agrupadas por día.

    - **MeLi**: órdenes con status=paid. Net = total - comisión MeLi - IVA comisión.
    - **Amazon**: OPS (Ordered Product Sales) de la Sales API — mismo número que Seller Central.
    """
    d_from = _parse_date(from_date, "from")
    d_to   = _parse_date(to_date,   "to")
    _validate_range(d_from, d_to)

    daily = await _get_daily_data(d_from, d_to)

    return {
        "period":          {"from": str(d_from), "to": str(d_to)},
        "currency":        "MXN",
        "granularity":     "day",
        "totals":          _build_totals(daily),
        "data":            daily,
        "generated_at":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.get("/weekly", summary="Ventas reales por semana ISO")
async def sales_weekly(
    from_date: str = Query(..., alias="from", description="Fecha inicio YYYY-MM-DD"),
    to_date:   str = Query(..., alias="to",   description="Fecha fin YYYY-MM-DD"),
    _key: str = Depends(require_api_key),
):
    """
    Ventas reales agrupadas por semana ISO (lunes–domingo).
    """
    d_from = _parse_date(from_date, "from")
    d_to   = _parse_date(to_date,   "to")
    _validate_range(d_from, d_to)

    daily   = await _get_daily_data(d_from, d_to)
    weekly  = _aggregate_weekly(daily)

    return {
        "period":      {"from": str(d_from), "to": str(d_to)},
        "currency":    "MXN",
        "granularity": "week",
        "totals":      _build_totals(daily),
        "data":        weekly,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.get("/monthly", summary="Ventas reales por mes")
async def sales_monthly(
    year: int = Query(..., ge=2020, le=2030, description="Año, ej: 2026"),
    _key: str = Depends(require_api_key),
):
    """
    Ventas reales de todo un año, agrupadas por mes.
    """
    d_from = date(year, 1, 1)
    d_to   = min(date(year, 12, 31), date.today())

    daily   = await _get_daily_data(d_from, d_to)
    monthly = _aggregate_monthly(daily)

    return {
        "period":      {"from": str(d_from), "to": str(d_to)},
        "currency":    "MXN",
        "granularity": "month",
        "year":        year,
        "totals":      _build_totals(daily),
        "data":        monthly,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.get("/summary", summary="Resumen del período reciente")
async def sales_summary(
    days: int = Query(30, ge=1, le=90, description="Últimos N días (máx 90)"),
    _key: str = Depends(require_api_key),
):
    """
    Resumen rápido de los últimos N días reales. Default: 30 días.
    """
    d_to   = date.today()
    d_from = d_to - timedelta(days=days - 1)

    daily   = await _get_daily_data(d_from, d_to)
    totals  = _build_totals(daily)
    weekly  = _aggregate_weekly(daily)
    monthly = _aggregate_monthly(daily)

    return {
        "period":      {"from": str(d_from), "to": str(d_to), "days": days},
        "currency":    "MXN",
        "totals":      totals,
        "by_week":     weekly,
        "by_month":    monthly,
        "daily":       daily,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
