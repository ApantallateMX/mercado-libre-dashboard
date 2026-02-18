# Changelog - Mercado Libre Dashboard

## 2026-02-18 — Feat: Reestructuracion completa seccion Ads (6 tabs + tiers)

### Archivos modificados
- `app/main.py`: nuevo endpoint `/partials/ads-by-category` (paginado, cache 30min), nuevo `/api/ads/campaigns-with-items` (crea+asigna), ads-performance con params `category` y `tier`
- `app/templates/ads.html`: estructura de 6 tabs (Campanas/Rendimiento/Por Categoria/Diagnostico/Sin Publicidad), modal Nueva Campana con 3 estrategias
- `app/templates/partials/ads_performance.html`: tiers TOP/MEDIO/BAJO/SIN VENTA (ROAS-based), filtros de tier y categoria, boton crear campana desde TOP
- `app/templates/partials/ads_campaigns.html`: badge estrategia inferida por ACOS
- `app/templates/partials/ads_no_ads.html`: seccion Candidatos TOP (+10 ventas), badge Recomendado
- `app/templates/partials/ads_by_category.html`: NUEVO — tabla + Chart.js por categoria

### Tiers de ROAS
- TOP (verde): ROAS ≥ 5x
- MEDIO (amarillo): ROAS 2-5x
- BAJO (rojo): ROAS < 2x con ventas
- SIN VENTA (gris): units == 0

### Estrategias de campana
- Rentabilidad → ACOS 15% (productos ganadores)
- Crecimiento → ACOS 25% (potencial)
- Visibilidad → ACOS 40% (nuevos, awareness)

---

## 2026-02-18 — Fix: Excluir stock ICB/ICC en listings sin sufijo IC

### Regla implementada
- SKUs **sin** sufijo `-ICB`/`-ICC` (listings regulares): `Condition="GRA,GRB,GRC,NEW"`
  → Excluye unidades dañadas/incompletas del stock visible
- SKUs **con** sufijo `-ICB`/`-ICC` (listings específicos IC): `Condition="GRA,GRB,GRC,ICB,ICC,NEW"`
  → Incluye todas las condiciones (la publicación ES para productos IC)

### Ejemplo que motivó el fix
- SNFN000095: 368 unidades = 362 ICB + 6 ICC ("Incompleto y Dañado")
- Antes: listing regular mostraba 368 unidades (inflado con stock IC)
- Después: listing regular muestra 0 (correcto — ninguna unidad GR en almacén)

### Archivos modificados (7 puntos de consulta Warehouse)
- `app/main.py`: `_bm_conditions_for_sku()` helper + aplicado en `_enrich_with_bm_stock`,
  `_wh_phase`, `_fetch_inv` (items health), `_check_base_wh` (sku-deals)
- `app/api/items.py`: `_bm_conditions()` helper + `_bm_warehouse_qty()`
- `app/api/sku_inventory.py`: `_bm_conditions_for_sku()` helper + `_fetch_sellable_stock()`

---



## 2026-02-18 — Fix crítico #2: Reemplazar FullFillment con Warehouse endpoint

### Problema raíz
`FullFillment API` (`GetQtysFromWebSKU`) colapsa todas las condiciones (GRA/GRB/GRC) al
mismo ProductSKU canonical (GRB), perdiendo el stock de condiciones alternativas.
- SNTV001763: FullFillment=19, UI real=23 (GRB=19 + GRA=4 extra)
- SNTV002237: FullFillment=2, UI real=5

### Nuevo endpoint (correcto)
`Get_GlobalStock_InventoryBySKU_Warehouse`
- Payload: `COMPANYID=1, LocationID="47,62,68", Condition="GRA,GRB,GRC,ICB,ICC,NEW"`
- Mapeo warehouses: "Monterrey MAXX"→MTY, "Autobot"→CDMX, otros→TJ (informativo)
- Resultados verificados: SNTV001763=23✓, SNTV002237=5✓, SNTV001863=18✓

### Archivos modificados
- `app/api/items.py`: `_bm_warehouse_qty()` helper + actualizar inventory-bulk/sku/sku-sales
- `app/api/sku_inventory.py`: `_fetch_sellable_stock()` + condition endpoint para GR/IC split
- `app/main.py`: `_enrich_with_bm_stock()`, `_get_bm_stock_cached()`, items-health, SKU-deals

### LocationIDs confirmados (fijos)
- 47 = CDMX ALMACEN 2 Ebanistas (Autobot)
- 62 = TJ (Tijuana, informativo)
- 68 = MTY-02 MAXX (Monterrey MAXX)

---

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
