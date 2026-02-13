"""Herramientas que el asistente IA puede ejecutar para obtener datos de MeLi."""

from datetime import datetime, timedelta
from app.services.meli_client import MeliClient
from app import order_net_revenue


TOOL_DEFINITIONS = [
    # --- 1. Estrategia y metricas ---
    {
        "name": "get_user_info",
        "description": "Informacion de la cuenta: nickname, reputacion, nivel, calificaciones, ventas completadas/canceladas.",
    },
    {
        "name": "get_recent_orders",
        "description": "Ventas/ordenes mas recientes. Parametro opcional: limit (default 10).",
        "parameters": {"limit": "int (default 10)"},
    },
    {
        "name": "get_orders_by_date",
        "description": "Ventas en un rango de fechas con totales e ingresos. Params: date_from (YYYY-MM-DD), date_to (YYYY-MM-DD).",
        "parameters": {"date_from": "str YYYY-MM-DD", "date_to": "str YYYY-MM-DD"},
    },
    {
        "name": "get_sku_sales",
        "description": "Ventas agrupadas por SKU con cantidad e ingresos. Params: date_from (YYYY-MM-DD), date_to (YYYY-MM-DD).",
        "parameters": {"date_from": "str YYYY-MM-DD", "date_to": "str YYYY-MM-DD"},
    },
    {
        "name": "get_sales_report",
        "description": "Reporte completo de ventas: resumen diario, top SKUs, ingresos totales. Params: date_from (YYYY-MM-DD), date_to (YYYY-MM-DD).",
        "parameters": {"date_from": "str YYYY-MM-DD", "date_to": "str YYYY-MM-DD"},
    },
    # --- 2. Inventario y productos ---
    {
        "name": "get_items_summary",
        "description": "Resumen de inventario: total de productos activos, pausados, cerrados.",
    },
    {
        "name": "get_item_details",
        "description": "Detalle completo de un producto: titulo, precio, stock, ventas, SKU, tipo de envio, categoria. Param: item_id (ej: MLM1234567890).",
        "parameters": {"item_id": "str"},
    },
    {
        "name": "get_no_stock_items",
        "description": "Productos con 0 stock que tienen ventas registradas (quiebres de stock).",
    },
    {
        "name": "get_low_performing_items",
        "description": "Productos activos con bajo rendimiento: pocas o cero ventas.",
    },
    # --- 3. Pricing y rentabilidad ---
    {
        "name": "get_item_profitability",
        "description": "Calcula rentabilidad estimada de un producto: precio, comision ML, costo envio, margen. Param: item_id.",
        "parameters": {"item_id": "str"},
    },
    # --- 4. Competencia ---
    {
        "name": "search_competition",
        "description": "Busca productos similares en MeLi para analisis de competencia: precios, vendedores, envios. Param: query (palabras clave), limit (default 10).",
        "parameters": {"query": "str", "limit": "int (default 10)"},
    },
    # --- 5. Reputacion y atencion al cliente ---
    {
        "name": "get_unanswered_questions",
        "description": "Preguntas sin responder de compradores. Muestra producto, pregunta y fecha.",
    },
    {
        "name": "get_claims",
        "description": "Reclamos y disputas activas. Muestra motivo, estado y recurso asociado.",
    },
    # --- 6. Logistica y envios ---
    {
        "name": "get_shipping_summary",
        "description": "Resumen de tipos de envio en productos activos: cuantos usan FULL, Flex, mercado_envios, etc.",
    },
    # --- 7. Visitas y conversion ---
    {
        "name": "get_item_visits",
        "description": "Visitas de un producto en los ultimos 30 dias. Param: item_id.",
        "parameters": {"item_id": "str"},
    },
]


# ============================================================
# Funciones de herramientas
# ============================================================

def _extract_order_sku(order_item: dict) -> str:
    """Extrae SKU de un order_item, buscando en todos los campos posibles."""
    item = order_item.get("item", {})
    # 1. seller_sku directo del order_item
    sku = item.get("seller_sku") or ""
    # 2. seller_custom_field del item
    if not sku:
        sku = item.get("seller_custom_field") or ""
    # 3. variation_attributes (cuando se vende una variante)
    if not sku and order_item.get("item", {}).get("variation_id"):
        for attr in item.get("variation_attributes", []):
            if attr.get("id") == "SELLER_SKU":
                sku = attr.get("value_name", "")
                break
    return sku or "SIN SKU"


def _format_order(order: dict) -> str:
    """Formatea una orden para el LLM."""
    items_info = []
    for oi in order.get("order_items", []):
        item = oi.get("item", {})
        sku = _extract_order_sku(oi)
        items_info.append(f"  - {item.get('title', '-')} | SKU: {sku} | Cant: {oi.get('quantity', 1)} | ${oi.get('unit_price', 0):.2f}")
    items_str = "\n".join(items_info) if items_info else "  (sin items)"
    return (
        f"Orden #{order.get('id', '-')} | Fecha: {order.get('date_created', '-')[:10]} | "
        f"Estado: {order.get('status', '-')} | Neto: ${order_net_revenue(order):.2f} (Bruto: ${order.get('total_amount', 0):.2f})\n{items_str}"
    )


def _extract_sku(body: dict) -> str:
    """Extrae SKU de un item body."""
    sku = body.get("seller_custom_field") or ""
    if not sku and body.get("attributes"):
        for attr in body["attributes"]:
            if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                sku = attr["value_name"]
                break
    if not sku and body.get("variations"):
        for var in body["variations"]:
            if var.get("seller_custom_field"):
                sku = var["seller_custom_field"]
                break
    return sku or "Sin SKU"


async def _fetch_all_orders(client: MeliClient, date_from: str = None, date_to: str = None, max_pages: int = 20) -> list:
    """Obtiene todas las ordenes paginando."""
    all_orders = []
    offset = 0
    for _ in range(max_pages):
        data = await client.get_orders(offset=offset, limit=50, date_from=date_from, date_to=date_to)
        results = data.get("results", [])
        if not results:
            break
        all_orders.extend(results)
        total = data.get("paging", {}).get("total", 0)
        offset += 50
        if offset >= total:
            break
    return all_orders


async def _fetch_all_item_ids(client: MeliClient, statuses=("active", "paused")) -> list:
    """Obtiene todos los IDs de items del vendedor."""
    all_ids = []
    for status in statuses:
        offset = 0
        while True:
            try:
                data = await client.get_items(offset=offset, limit=50, status=status)
            except Exception:
                break
            ids = data.get("results", [])
            if not ids:
                break
            all_ids.extend(ids)
            total = data.get("paging", {}).get("total", 0)
            offset += 50
            if offset >= total:
                break
    return all_ids


async def _fetch_items_details(client: MeliClient, item_ids: list) -> list:
    """Obtiene detalles de items en batches de 20."""
    all_details = []
    for i in range(0, len(item_ids), 20):
        batch = item_ids[i:i + 20]
        try:
            details = await client.get_items_details(batch)
            all_details.extend(details)
        except Exception:
            continue
    return all_details


# --- 1. Estrategia y metricas ---

async def tool_get_user_info(client: MeliClient) -> str:
    user = await client.get_user_info()
    rep = user.get("seller_reputation", {})
    level = rep.get("level_id", "-")
    power_status = rep.get("power_seller_status") or "No"
    transactions = rep.get("transactions", {})
    completed = transactions.get("completed", 0)
    canceled = transactions.get("canceled", 0)
    ratings = transactions.get("ratings", {})
    metrics = rep.get("metrics", {})

    lines = [
        f"Cuenta: {user.get('nickname', '-')}",
        f"ID: {user.get('id', '-')}",
        f"Nivel de vendedor: {level}",
        f"Power Seller: {power_status}",
        f"Ventas completadas: {completed}",
        f"Ventas canceladas: {canceled}",
        f"Calificaciones: +{ratings.get('positive', 0)} / ~{ratings.get('neutral', 0)} / -{ratings.get('negative', 0)}",
    ]

    # Metricas de reputacion si existen
    for metric_name, metric_data in metrics.items():
        if isinstance(metric_data, dict) and "value" in metric_data:
            lines.append(f"Metrica {metric_name}: {metric_data['value']} (periodo: {metric_data.get('period', '-')})")

    return "\n".join(lines)


async def tool_get_recent_orders(client: MeliClient, limit: int = 10) -> str:
    data = await client.get_orders(limit=min(limit, 50))
    orders = data.get("results", [])
    total = data.get("paging", {}).get("total", 0)
    if not orders:
        return "No hay ventas recientes."
    lines = [f"Total de ordenes registradas: {total}", f"Mostrando las ultimas {len(orders)}:", ""]
    for o in orders:
        lines.append(_format_order(o))
    return "\n".join(lines)


async def tool_get_orders_by_date(client: MeliClient, date_from: str, date_to: str) -> str:
    all_orders = await _fetch_all_orders(client, date_from, date_to)

    paid = [o for o in all_orders if o.get("status") in ("paid", "delivered")]
    canceled = [o for o in all_orders if o.get("status") == "cancelled"]
    revenue = sum(order_net_revenue(o) for o in paid)

    lines = [
        f"Periodo: {date_from} a {date_to}",
        f"Total ordenes encontradas: {len(all_orders)}",
        f"Ordenes pagadas/entregadas: {len(paid)}",
        f"Ordenes canceladas: {len(canceled)}",
        f"Ingresos netos: ${revenue:,.2f}",
        "",
    ]
    for o in all_orders[:20]:
        lines.append(_format_order(o))
    if len(all_orders) > 20:
        lines.append(f"... y {len(all_orders) - 20} ordenes mas.")
    return "\n".join(lines)


async def tool_get_sku_sales(client: MeliClient, date_from: str, date_to: str) -> str:
    all_orders = await _fetch_all_orders(client, date_from, date_to)

    sku_map = {}
    for order in all_orders:
        if order.get("status") not in ("paid", "delivered"):
            continue
        for oi in order.get("order_items", []):
            item = oi.get("item", {})
            sku = _extract_order_sku(oi)
            qty = oi.get("quantity", 1)
            price = oi.get("unit_price", 0)
            if sku not in sku_map:
                sku_map[sku] = {"title": item.get("title", "-"), "quantity": 0, "revenue": 0}
            sku_map[sku]["quantity"] += qty
            sku_map[sku]["revenue"] += price * qty

    if not sku_map:
        return f"No hay ventas pagadas en el periodo {date_from} a {date_to}."

    sorted_skus = sorted(sku_map.items(), key=lambda x: x[1]["quantity"], reverse=True)
    total_qty = sum(v["quantity"] for _, v in sorted_skus)
    total_rev = sum(v["revenue"] for _, v in sorted_skus)

    lines = [f"Ventas por SKU ({date_from} a {date_to}):", ""]
    for sku, info in sorted_skus:
        lines.append(f"SKU: {sku} | {info['title'][:40]} | Unidades: {info['quantity']} | Ingresos: ${info['revenue']:,.2f}")
    lines.append(f"\nTotal: {total_qty} unidades | ${total_rev:,.2f}")
    return "\n".join(lines)


async def tool_get_sales_report(client: MeliClient, date_from: str, date_to: str) -> str:
    """Reporte completo: ventas por dia, top SKUs, cancelaciones."""
    all_orders = await _fetch_all_orders(client, date_from, date_to)

    paid = [o for o in all_orders if o.get("status") in ("paid", "delivered")]
    canceled = [o for o in all_orders if o.get("status") == "cancelled"]
    revenue = sum(order_net_revenue(o) for o in paid)

    # Ventas por dia
    daily = {}
    for o in paid:
        day = o.get("date_created", "")[:10]
        if day not in daily:
            daily[day] = {"count": 0, "revenue": 0}
        daily[day]["count"] += 1
        daily[day]["revenue"] += order_net_revenue(o)

    # Top SKUs
    sku_map = {}
    for o in paid:
        for oi in o.get("order_items", []):
            item = oi.get("item", {})
            sku = _extract_order_sku(oi)
            qty = oi.get("quantity", 1)
            price = oi.get("unit_price", 0)
            fee = oi.get("sale_fee", 0) or 0
            net = price * qty - fee - fee * 0.16
            if sku not in sku_map:
                sku_map[sku] = {"title": item.get("title", "-"), "quantity": 0, "revenue": 0}
            sku_map[sku]["quantity"] += qty
            sku_map[sku]["revenue"] += net

    top_skus = sorted(sku_map.items(), key=lambda x: x[1]["revenue"], reverse=True)[:10]
    avg_ticket = revenue / len(paid) if paid else 0

    lines = [
        f"=== REPORTE DE VENTAS: {date_from} a {date_to} ===",
        "",
        f"Total ordenes: {len(all_orders)}",
        f"Pagadas/entregadas: {len(paid)}",
        f"Canceladas: {len(canceled)}",
        f"Tasa de cancelacion: {len(canceled)/len(all_orders)*100:.1f}%" if all_orders else "Tasa de cancelacion: 0%",
        f"Ingresos brutos: ${revenue:,.2f}",
        f"Ticket promedio: ${avg_ticket:,.2f}",
        "",
        "--- Ventas por dia ---",
    ]
    for day in sorted(daily.keys()):
        d = daily[day]
        lines.append(f"{day}: {d['count']} ventas | ${d['revenue']:,.2f}")

    lines.append("")
    lines.append("--- Top 10 SKUs por ingresos ---")
    for sku, info in top_skus:
        lines.append(f"SKU: {sku} | {info['title'][:35]} | Uds: {info['quantity']} | ${info['revenue']:,.2f}")

    return "\n".join(lines)


# --- 2. Inventario y productos ---

async def tool_get_items_summary(client: MeliClient) -> str:
    counts = {}
    for status in ("active", "paused", "closed"):
        try:
            data = await client.get_items(limit=1, status=status)
            counts[status] = data.get("paging", {}).get("total", 0)
        except Exception:
            counts[status] = 0

    return (
        f"Resumen de inventario:\n"
        f"Productos activos: {counts['active']}\n"
        f"Productos pausados: {counts['paused']}\n"
        f"Productos cerrados: {counts['closed']}\n"
        f"Total: {sum(counts.values())}"
    )


async def tool_get_item_details(client: MeliClient, item_id: str) -> str:
    try:
        item = await client.get_item(item_id)
    except Exception as e:
        return f"No se pudo obtener el item {item_id}: {e}"

    sku = _extract_sku(item)

    # Tipo de envio
    shipping = item.get("shipping", {})
    logistic = shipping.get("logistic_type", "-")
    free_shipping = shipping.get("free_shipping", False)

    # Categoria
    cat_id = item.get("category_id", "-")

    # Tipo de publicacion
    listing_type = item.get("listing_type_id", "-")

    # Salud de la publicacion
    health = item.get("health", None)

    lines = [
        f"Producto: {item.get('title', '-')}",
        f"ID: {item.get('id', '-')}",
        f"SKU: {sku}",
        f"Precio: ${item.get('price', 0):,.2f}",
        f"Stock disponible: {item.get('available_quantity', 0)}",
        f"Unidades vendidas: {item.get('sold_quantity', 0)}",
        f"Estado: {item.get('status', '-')}",
        f"Tipo de publicacion: {listing_type}",
        f"Categoria: {cat_id}",
        f"Tipo de envio: {logistic}",
        f"Envio gratis: {'Si' if free_shipping else 'No'}",
        f"Link: {item.get('permalink', '-')}",
    ]

    if health:
        lines.append(f"Salud de publicacion: {health}")

    # Variantes
    variations = item.get("variations", [])
    if variations:
        lines.append(f"Variantes: {len(variations)}")
        for v in variations[:5]:
            v_sku = v.get("seller_custom_field") or "-"
            v_stock = v.get("available_quantity", 0)
            combos = ", ".join([f"{ac.get('name','')}: {ac.get('value_name','')}" for ac in v.get("attribute_combinations", [])])
            lines.append(f"  - {combos} | SKU: {v_sku} | Stock: {v_stock}")

    return "\n".join(lines)


async def tool_get_no_stock_items(client: MeliClient) -> str:
    all_item_ids = await _fetch_all_item_ids(client)
    details = await _fetch_items_details(client, all_item_ids)

    no_stock = []
    for item in details:
        body = item.get("body", {})
        if not body:
            continue
        stock = body.get("available_quantity", 0)
        sold = body.get("sold_quantity", 0)
        if stock == 0 and sold > 0:
            no_stock.append({
                "id": body.get("id", ""),
                "title": body.get("title", "-"),
                "sku": _extract_sku(body),
                "sold": sold,
                "price": body.get("price", 0),
            })

    if not no_stock:
        return "No hay productos sin stock que tengan ventas."

    no_stock.sort(key=lambda x: x["sold"], reverse=True)
    lines = [f"QUIEBRES DE STOCK: {len(no_stock)} productos sin stock con ventas", ""]
    for item in no_stock:
        lines.append(f"ID: {item['id']} | {item['title'][:40]} | SKU: {item['sku']} | Vendidos: {item['sold']} | Precio: ${item['price']:,.2f}")
    return "\n".join(lines)


async def tool_get_low_performing_items(client: MeliClient) -> str:
    """Productos activos con bajo rendimiento (pocas ventas)."""
    item_ids = await _fetch_all_item_ids(client, statuses=("active",))
    details = await _fetch_items_details(client, item_ids)

    low_perf = []
    for item in details:
        body = item.get("body", {})
        if not body:
            continue
        sold = body.get("sold_quantity", 0)
        stock = body.get("available_quantity", 0)
        if sold <= 2 and stock > 0:
            low_perf.append({
                "id": body.get("id", ""),
                "title": body.get("title", "-"),
                "sku": _extract_sku(body),
                "sold": sold,
                "stock": stock,
                "price": body.get("price", 0),
            })

    if not low_perf:
        return "No se encontraron productos con bajo rendimiento."

    low_perf.sort(key=lambda x: x["sold"])
    lines = [f"PRODUCTOS CON BAJO RENDIMIENTO: {len(low_perf)} productos activos con 0-2 ventas", ""]
    for item in low_perf[:30]:
        lines.append(
            f"ID: {item['id']} | {item['title'][:35]} | SKU: {item['sku']} | "
            f"Vendidos: {item['sold']} | Stock: {item['stock']} | Precio: ${item['price']:,.2f}"
        )
    if len(low_perf) > 30:
        lines.append(f"... y {len(low_perf) - 30} productos mas.")
    return "\n".join(lines)


# --- 3. Pricing y rentabilidad ---

async def tool_get_item_profitability(client: MeliClient, item_id: str) -> str:
    """Calcula rentabilidad estimada de un producto."""
    try:
        item = await client.get_item(item_id)
    except Exception as e:
        return f"No se pudo obtener el item {item_id}: {e}"

    price = item.get("price", 0)
    cat_id = item.get("category_id", "")
    listing_type = item.get("listing_type_id", "gold_special")
    shipping = item.get("shipping", {})
    logistic = shipping.get("logistic_type", "-")
    free_ship = shipping.get("free_shipping", False)

    # Intentar obtener comisiones
    fee_pct = 0
    fee_amount = 0
    try:
        fees_data = await client.get_listing_fees(cat_id, price, listing_type)
        if isinstance(fees_data, list):
            for fee_item in fees_data:
                if fee_item.get("listing_type_id") == listing_type:
                    fee_amount = fee_item.get("sale_fee_amount", 0)
                    fee_pct = (fee_amount / price * 100) if price > 0 else 0
                    break
    except Exception:
        # Estimacion default: 17.5% para gold_special en Mexico
        fee_pct = 17.5 if listing_type == "gold_special" else 13.0
        fee_amount = price * fee_pct / 100

    # IVA sobre comision
    iva_comision = fee_amount * 0.16

    # Estimacion de envio (si es gratis para el comprador, lo paga el vendedor)
    shipping_cost = 0
    if free_ship:
        # Estimacion basica por tipo de logistica
        if logistic == "fulfillment":
            shipping_cost = price * 0.05  # ~5% estimado para FULL
        else:
            shipping_cost = price * 0.08  # ~8% estimado mercado envios

    total_costs = fee_amount + iva_comision + shipping_cost
    margin = price - total_costs
    margin_pct = (margin / price * 100) if price > 0 else 0

    lines = [
        f"=== RENTABILIDAD ESTIMADA: {item.get('title', '-')[:50]} ===",
        f"ID: {item_id}",
        f"SKU: {_extract_sku(item)}",
        "",
        f"Precio de venta: ${price:,.2f}",
        f"Comision ML ({fee_pct:.1f}%): -${fee_amount:,.2f}",
        f"IVA sobre comision (16%): -${iva_comision:,.2f}",
        f"Costo envio estimado: -${shipping_cost:,.2f}" if free_ship else "Costo envio: $0 (lo paga el comprador)",
        f"Tipo logistica: {logistic}",
        f"Tipo publicacion: {listing_type}",
        "",
        f"TOTAL COSTOS ML: -${total_costs:,.2f}",
        f"MARGEN BRUTO: ${margin:,.2f} ({margin_pct:.1f}%)",
        "",
        "NOTA: Este es un calculo estimado. No incluye tu costo de producto, empaque, ni otros gastos operativos. "
        "Resta tu costo de producto al margen bruto para obtener tu ganancia neta real.",
    ]
    return "\n".join(lines)


# --- 4. Competencia ---

async def tool_search_competition(client: MeliClient, query: str, limit: int = 10) -> str:
    """Busca competencia en MeLi."""
    try:
        data = await client.search_items(query, limit=min(limit, 20))
    except Exception as e:
        return f"Error buscando competencia: {e}"

    results = data.get("results", [])
    if not results:
        return f"No se encontraron resultados para: {query}"

    lines = [
        f"ANALISIS DE COMPETENCIA: '{query}'",
        f"Resultados encontrados: {data.get('paging', {}).get('total', 0)}",
        f"Mostrando {len(results)}:",
        "",
    ]

    prices = []
    for r in results:
        price = r.get("price", 0)
        prices.append(price)
        seller = r.get("seller", {})
        shipping = r.get("shipping", {})
        lines.append(
            f"- {r.get('title', '-')[:50]}\n"
            f"  Precio: ${price:,.2f} | Vendidos: {r.get('sold_quantity', 0)} | "
            f"Vendedor: {seller.get('nickname', '-')} | "
            f"Envio gratis: {'Si' if shipping.get('free_shipping') else 'No'} | "
            f"FULL: {'Si' if shipping.get('logistic_type') == 'fulfillment' else 'No'}"
        )

    if prices:
        lines.append("")
        lines.append(f"Precio minimo: ${min(prices):,.2f}")
        lines.append(f"Precio maximo: ${max(prices):,.2f}")
        lines.append(f"Precio promedio: ${sum(prices)/len(prices):,.2f}")

    return "\n".join(lines)


# --- 5. Reputacion y atencion al cliente ---

async def tool_get_unanswered_questions(client: MeliClient) -> str:
    """Preguntas sin responder."""
    try:
        data = await client.get_questions(status="UNANSWERED")
    except Exception as e:
        return f"Error obteniendo preguntas: {e}"

    questions = data.get("questions", [])
    total = data.get("total", len(questions))

    if not questions:
        return "No hay preguntas sin responder. Todo al dia."

    lines = [f"PREGUNTAS SIN RESPONDER: {total}", ""]
    for q in questions[:20]:
        lines.append(
            f"- Item: {q.get('item_id', '-')}\n"
            f"  Pregunta: {q.get('text', '-')}\n"
            f"  Fecha: {q.get('date_created', '-')[:10]}"
        )
    if total > 20:
        lines.append(f"... y {total - 20} preguntas mas.")

    lines.append("")
    lines.append("IMPORTANTE: Responder rapido mejora tu reputacion y posicionamiento.")
    return "\n".join(lines)


async def tool_get_claims(client: MeliClient) -> str:
    """Reclamos activos."""
    try:
        data = await client.get_claims()
    except Exception as e:
        return f"Error obteniendo reclamos: {e}"

    claims = data.get("data", data.get("results", []))
    if not claims:
        return "No hay reclamos activos. Excelente."

    lines = [f"RECLAMOS ACTIVOS: {len(claims)}", ""]
    for c in claims[:15]:
        lines.append(
            f"- ID: {c.get('id', '-')} | Tipo: {c.get('type', '-')} | Estado: {c.get('status', '-')}\n"
            f"  Recurso: {c.get('resource_id', '-')} | Razon: {c.get('reason_id', '-')}\n"
            f"  Fecha: {c.get('date_created', '-')[:10]}"
        )

    return "\n".join(lines)


# --- 6. Logistica y envios ---

async def tool_get_shipping_summary(client: MeliClient) -> str:
    """Resumen de tipos de envio en productos activos."""
    item_ids = await _fetch_all_item_ids(client, statuses=("active",))
    details = await _fetch_items_details(client, item_ids)

    shipping_types = {}
    free_count = 0
    total_items = 0

    for item in details:
        body = item.get("body", {})
        if not body:
            continue
        total_items += 1
        shipping = body.get("shipping", {})
        logistic = shipping.get("logistic_type", "sin_envio")
        shipping_types[logistic] = shipping_types.get(logistic, 0) + 1
        if shipping.get("free_shipping"):
            free_count += 1

    lines = [
        f"RESUMEN DE LOGISTICA ({total_items} productos activos):",
        "",
    ]
    for stype, count in sorted(shipping_types.items(), key=lambda x: x[1], reverse=True):
        label = {
            "fulfillment": "FULL (Mercado Libre lo envia)",
            "xd_drop_off": "Flex / Drop-off",
            "self_service": "Envio propio",
            "cross_docking": "Cross docking",
            "not_specified": "No especificado",
        }.get(stype, stype)
        pct = count / total_items * 100 if total_items else 0
        lines.append(f"  {label}: {count} ({pct:.0f}%)")

    lines.append("")
    lines.append(f"Con envio gratis: {free_count} ({free_count/total_items*100:.0f}%)" if total_items else "Con envio gratis: 0")

    if shipping_types.get("fulfillment", 0) / total_items < 0.5 if total_items else False:
        lines.append("")
        lines.append("SUGERENCIA: Menos del 50% de tus productos estan en FULL. Subir mas productos a FULL mejora tu posicionamiento y velocidad de entrega.")

    return "\n".join(lines)


# --- 7. Visitas y conversion ---

async def tool_get_item_visits(client: MeliClient, item_id: str) -> str:
    """Visitas de un producto."""
    try:
        data = await client.get_item_visits(item_id, "", "")
    except Exception as e:
        return f"Error obteniendo visitas de {item_id}: {e}"

    # Obtener tambien datos del item para calcular conversion
    try:
        item = await client.get_item(item_id)
        sold = item.get("sold_quantity", 0)
        title = item.get("title", "-")
    except Exception:
        sold = 0
        title = item_id

    if isinstance(data, list):
        total_visits = sum(d.get("total", 0) for d in data)
        lines = [
            f"VISITAS: {title[:50]}",
            f"ID: {item_id}",
            f"Visitas ultimos 30 dias: {total_visits}",
            f"Unidades vendidas (historico): {sold}",
            "",
            "Detalle por dia:",
        ]
        for d in data[-14:]:  # ultimos 14 dias
            lines.append(f"  {d.get('date', '-')}: {d.get('total', 0)} visitas")
    elif isinstance(data, dict):
        total_visits = data.get("total_visits", 0)
        lines = [
            f"VISITAS: {title[:50]}",
            f"ID: {item_id}",
            f"Visitas: {total_visits}",
            f"Unidades vendidas (historico): {sold}",
        ]
    else:
        return f"Formato de visitas no reconocido para {item_id}."

    if total_visits > 0 and sold > 0:
        conversion = sold / total_visits * 100
        lines.append(f"\nConversion estimada: {conversion:.2f}%")
        if conversion < 1:
            lines.append("NOTA: Conversion baja. Revisa titulo, fotos y precio.")

    return "\n".join(lines)


# --- 8. Publicidad (Mercado Ads) ---

async def tool_get_ads_overview(client: MeliClient, date_from: str, date_to: str) -> str:
    """Resumen general de gasto y rendimiento en Mercado Ads."""
    # Obtener campanas con metricas
    try:
        campaigns_data = await client.get_ads_campaigns(date_from, date_to)
    except Exception as e:
        return f"Error obteniendo campanas de ads: {e}"

    campaigns = campaigns_data.get("results", campaigns_data if isinstance(campaigns_data, list) else [])
    active = [c for c in campaigns if c.get("status") == "active"]
    paused = [c for c in campaigns if c.get("status") == "paused"]

    # Obtener metricas totales de items
    try:
        items_data = await client.get_ads_items(date_from, date_to)
        summary = items_data.get("metrics_summary", {})
    except Exception:
        summary = {}

    total_cost = summary.get("cost", 0) or 0
    total_clicks = summary.get("clicks", 0) or 0
    total_prints = summary.get("prints", 0) or 0
    total_units = summary.get("units_quantity", 0) or 0
    acos = summary.get("acos", 0) or 0

    cpc = total_cost / total_clicks if total_clicks > 0 else 0
    ctr = total_clicks / total_prints * 100 if total_prints > 0 else 0
    roas = (1 / (acos / 100)) if acos > 0 else 0

    lines = [
        f"=== RESUMEN MERCADO ADS ({date_from} a {date_to}) ===",
        "",
        f"Campanas activas: {len(active)}",
        f"Campanas pausadas: {len(paused)}",
        f"Total campanas: {len(campaigns)}",
        "",
        "--- Metricas del periodo ---",
        f"Gasto total: ${total_cost:,.2f}",
        f"Impresiones: {total_prints:,}",
        f"Clics: {total_clicks:,}",
        f"CTR: {ctr:.2f}%",
        f"CPC promedio: ${cpc:,.2f}",
        f"Unidades vendidas por ads: {total_units}",
        f"ACOS: {acos:.1f}%",
        f"ROAS: {roas:.2f}x",
        "",
    ]

    if roas > 0 and roas < 3:
        lines.append("ALERTA: ROAS menor a 3x. Revisa campanas con bajo rendimiento.")
    if ctr > 0 and ctr < 0.5:
        lines.append("ALERTA: CTR muy bajo. Revisa titulos y fotos de los productos anunciados.")

    # Listar campanas activas con metricas
    if active:
        lines.append("")
        lines.append("--- Campanas activas ---")
        for c in active:
            m = c.get("metrics", {})
            budget = c.get("budget", {})
            budget_amt = budget.get("amount", 0) if isinstance(budget, dict) else (budget or 0)
            c_cost = m.get("cost", 0) or 0
            c_clicks = m.get("clicks", 0) or 0
            c_acos = m.get("acos", 0) or 0
            lines.append(f"- {c.get('name', c.get('id', '-'))} | ID: {c.get('id', '-')} | Presupuesto: ${budget_amt:,.2f} | Gasto: ${c_cost:,.2f} | Clics: {c_clicks} | ACOS: {c_acos:.1f}%")

    return "\n".join(lines)


async def tool_get_ads_campaign_detail(client: MeliClient, campaign_id: str, date_from: str, date_to: str) -> str:
    """Detalle y metricas de una campana especifica."""
    try:
        campaign = await client.get_ads_campaign_detail(campaign_id, date_from, date_to)
    except Exception as e:
        return f"Error obteniendo campana {campaign_id}: {e}"

    budget = campaign.get("budget", {})
    budget_amt = budget.get("amount", 0) if isinstance(budget, dict) else (budget or 0)

    lines = [
        f"=== CAMPANA: {campaign.get('name', campaign_id)} ===",
        f"ID: {campaign.get('id', '-')}",
        f"Estado: {campaign.get('status', '-')}",
        f"Presupuesto diario: ${budget_amt:,.2f}",
        f"Tipo: {campaign.get('type', '-')}",
        "",
    ]

    # Metricas vienen embebidas en la respuesta del campaign detail
    m = campaign.get("metrics", {})
    if m:
        total_cost = m.get("cost", 0) or 0
        total_clicks = m.get("clicks", 0) or 0
        total_prints = m.get("prints", 0) or 0
        total_units = m.get("units_quantity", 0) or 0
        c_acos = m.get("acos", 0) or 0
        c_cpc = m.get("cpc", 0) or 0

        ctr = total_clicks / total_prints * 100 if total_prints > 0 else 0
        roas = (1 / (c_acos / 100)) if c_acos > 0 else 0

        lines.extend([
            f"--- Metricas ({date_from} a {date_to}) ---",
            f"Gasto: ${total_cost:,.2f}",
            f"Impresiones: {total_prints:,}",
            f"Clics: {total_clicks:,}",
            f"CTR: {ctr:.2f}%",
            f"CPC: ${c_cpc:,.2f}",
            f"Unidades vendidas: {total_units}",
            f"ACOS: {c_acos:.1f}%",
            f"ROAS: {roas:.2f}x",
        ])
    else:
        lines.append("No se encontraron metricas para este periodo.")

    return "\n".join(lines)


async def tool_get_ads_top_products(client: MeliClient, date_from: str, date_to: str) -> str:
    """Top productos por rendimiento en ads: gasto, clics, ventas, ROAS."""
    try:
        data = await client.get_ads_items(date_from, date_to)
    except Exception as e:
        return f"Error obteniendo metricas por producto: {e}"

    items = data.get("results", data if isinstance(data, list) else [])
    if not items:
        return f"No hay datos de ads por producto en el periodo {date_from} a {date_to}."

    # Procesar y ordenar por gasto
    processed = []
    for item in items:
        m = item.get("metrics", {})
        cost = m.get("cost", 0) or 0
        clicks = m.get("clicks", 0) or 0
        impressions = m.get("prints", 0) or 0
        units = m.get("units_quantity", 0) or 0
        acos = m.get("acos", 0) or 0
        roas = (1 / (acos / 100)) if acos > 0 else 0
        item_id = item.get("item_id", "-")
        title = item.get("title", item_id)

        processed.append({
            "item_id": item_id,
            "title": title,
            "cost": cost,
            "clicks": clicks,
            "impressions": impressions,
            "sales": units,
            "revenue": 0,
            "roas": roas,
        })

    # Top por gasto
    by_cost = sorted(processed, key=lambda x: x["cost"], reverse=True)
    total_cost = sum(p["cost"] for p in processed)
    total_units = sum(p["sales"] for p in processed)

    lines = [
        f"=== TOP PRODUCTOS EN ADS ({date_from} a {date_to}) ===",
        f"Total productos anunciados: {len(processed)}",
        f"Gasto total: ${total_cost:,.2f}",
        f"Unidades vendidas total: {total_units}",
        "",
        "--- Top 15 por gasto ---",
    ]

    for p in by_cost[:15]:
        lines.append(
            f"- {p['title'][:40]} | Item: {p['item_id']}\n"
            f"  Gasto: ${p['cost']:,.2f} | Clics: {p['clicks']} | Uds vendidas: {p['sales']} | ROAS: {p['roas']:.2f}x"
        )

    # Productos quemando dinero (gasto > 0 pero sin ventas)
    burning = [p for p in processed if p["cost"] > 0 and p["sales"] == 0]
    if burning:
        burning.sort(key=lambda x: x["cost"], reverse=True)
        total_burned = sum(p["cost"] for p in burning)
        lines.append("")
        lines.append(f"--- PRODUCTOS SIN VENTAS CON GASTO ({len(burning)}) = ${total_burned:,.2f} quemados ---")
        for p in burning[:10]:
            lines.append(f"- {p['title'][:40]} | Gasto: ${p['cost']:,.2f} | Clics: {p['clicks']} | ROAS: 0x")
        lines.append("")
        lines.append("ACCION SUGERIDA: Pausar estos anuncios o mejorar las publicaciones (titulo, fotos, precio).")

    # Mejores ROAS
    top_roas = sorted([p for p in processed if p["roas"] > 0], key=lambda x: x["roas"], reverse=True)
    if top_roas:
        lines.append("")
        lines.append("--- Top 5 mejor ROAS (escalar estos) ---")
        for p in top_roas[:5]:
            lines.append(f"- {p['title'][:40]} | ROAS: {p['roas']:.2f}x | Gasto: ${p['cost']:,.2f} | Uds: {p['sales']}")

    return "\n".join(lines)


# ============================================================
# Mapa de herramientas
# ============================================================

TOOL_MAP = {
    "get_user_info": tool_get_user_info,
    "get_recent_orders": tool_get_recent_orders,
    "get_orders_by_date": tool_get_orders_by_date,
    "get_sku_sales": tool_get_sku_sales,
    "get_sales_report": tool_get_sales_report,
    "get_items_summary": tool_get_items_summary,
    "get_item_details": tool_get_item_details,
    "get_no_stock_items": tool_get_no_stock_items,
    "get_low_performing_items": tool_get_low_performing_items,
    "get_item_profitability": tool_get_item_profitability,
    "search_competition": tool_search_competition,
    "get_unanswered_questions": tool_get_unanswered_questions,
    "get_claims": tool_get_claims,
    "get_shipping_summary": tool_get_shipping_summary,
    "get_item_visits": tool_get_item_visits,
    "get_ads_overview": tool_get_ads_overview,
    "get_ads_campaign_detail": tool_get_ads_campaign_detail,
    "get_ads_top_products": tool_get_ads_top_products,
}
