# Mercado Libre Dashboard App

IVA_RATE = 0.16  # IVA Mexico


def order_net_revenue(order: dict) -> float:
    """Retorna el ingreso neto real del vendedor en una orden MeLi.

    Fórmula exacta (cuando hay datos de /collections):
      net = net_received_amount - sale_fee - shipping_cost
      donde net_received_amount = total_amount - impuestos_retenidos (IVA/ISR)

    Fallback (sin datos de collections):
      net = total - sale_fee - (sale_fee * 0.16)
    """
    net_received = order.get("_net_received_amount") or 0
    sale_fee = sum(float(i.get("sale_fee", 0) or 0) for i in order.get("order_items", []))
    shipping = order.get("_shipping_cost", 0) or 0

    if net_received > 0:
        # Cálculo exacto: net_received ya tiene impuestos descontados
        return round(net_received - sale_fee - shipping, 2)

    # Fallback: estimación cuando no hay datos de collections
    total = order.get("total_amount", 0) or 0
    iva_on_fees = sale_fee * IVA_RATE
    return round(total - sale_fee - iva_on_fees - shipping, 2)
