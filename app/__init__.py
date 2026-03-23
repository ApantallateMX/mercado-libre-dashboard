# Mercado Libre Dashboard App

IVA_RATE = 0.16  # IVA Mexico


def order_net_revenue(order: dict) -> float:
    """Retorna el ingreso neto de una orden (igual que el Total en MeLi).

    Si la orden fue enriquecida con _net_received_amount (de /collections API),
    usa ese valor que incluye todos los cargos: comisión, envío e impuestos.
    """
    # Usar neto real de MeLi si está disponible
    if order.get("_net_received_amount"):
        return order["_net_received_amount"]

    # Fallback: cálculo aproximado (puede no incluir todos los impuestos)
    total = order.get("total_amount", 0) or 0
    total_fees = 0.0
    for item in order.get("order_items", []):
        fee = item.get("sale_fee", 0) or 0
        total_fees += fee
    iva_on_fees = total_fees * IVA_RATE
    shipping_cost = order.get("_shipping_cost", 0) or 0
    iva_shipping = order.get("_iva_shipping", 0) or 0
    return total - total_fees - iva_on_fees - shipping_cost - iva_shipping
