# Changelog - Mercado Libre Dashboard

## 2026-02-18 — Fix crítico: Stock BinManager siempre de MainQty (SOLO)

### Problema raíz
`InventoryReport.AvailableQTY` NO es stock real disponible para venta.
- SNTV001763 → AvailableQTY=4971 (absurdo para un TV)
- SNTV001863 → AvailableQTY=9124 (igualmente absurdo)
Estos valores son conteos históricos de registros, no unidades físicas en almacén.

### Archivos modificados

**`app/api/sku_inventory.py`** — `_fetch_sellable_stock()`
- ELIMINADO: fallback a InventoryReport cuando `sellable_total == 0`
- AHORA: si ningún sufijo vendible tiene stock en FullFillment → stock=0 (correcto)
- `stock_other` siempre 0 (InventoryReport no aplica)

**`app/main.py`** — `_get_bm_stock_cached()` / Fase 2
- ELIMINADO: Fase 2 completa que consultaba InventoryReport con `AvailableQTY`
- AHORA: SKUs sin datos de FullFillment → `_store_empty(sku)` → total=0

**`app/api/items.py`** — `GET /inventory-bulk` y `GET /inventory/{web_sku}`
- MEJORADO: Si la consulta directa no retorna datos, intenta con sufijos vendibles
  (NEW, GRA, GRB, GRC, ICB, ICC) hasta encontrar datos válidos
- Garantiza que se obtenga el dato correcto de MainQty para cualquier variante de SKU

### Regla definitiva
**SOLO usar `MainQtyMTY` + `MainQtyCDMX` del FullFillment API**
- TJ es solo informativo, excluido de totales vendibles
- AltQty y TotalQty: NUNCA usar (mezclan stock de otros productos)
- InventoryReport.AvailableQTY: NUNCA usar para stock (dato histórico, no real)

---

## 2026-02-17 — Revertir ratio AltQty

- Revertido: ratio same-base dio 12 para SNTV003592 cuando el real era 6
- MainQty = único dato confiable. Sin ratios, sin estimaciones.
