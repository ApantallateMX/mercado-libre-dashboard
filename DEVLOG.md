# DEVLOG — mercado-libre-dashboard

Log de actualizaciones, errores, soluciones y mejoras del proyecto.
Formato: `[FECHA] [TIPO] descripción`

Tipos: `FIX` `FEAT` `BUG` `DECISION` `OPERACION`

---

## 2026-06-25 — FIX: SHIL000026 mostrando BM=549 en Activar — bulk-miss stale guard

### Commit `e5d8a58` — subido a Railway + Coolify

**Problema:** SHIL000026 mostraba BM Disponible=549 en la sección Activar aunque BM
tiene 0 unidades en LOC47+LOC68. MTY/CDMX/TJ mostraban 0 correctamente.

**Root cause — cadena de bugs:**
1. SHIL000026 tiene stock=0 en BM. BM no incluye SKUs con stock=0 en el bulk response.
2. Al ser "bulk miss" (`_bulk_miss_set`), `_store_wh` se llamaba con `avail_ok=False`
3. `verified = False` → Fix A detecta entrada DB con `_v=True, avail_total=549`
   (valor de la era LOC47,62,68 donde TJ tenía 549) → **return early** sin actualizar
4. Cache queda con 549. El retry per-SKU (línea 5202) solo corre cuando `not _used_bulk`
   pero el bulk SÍ corrió → SHIL000026 nunca se re-verifica

**Fix:**
- Loop bulk: si SKU en `_bulk_miss_set`, skip `_store_wh` completamente (evita Fix A)
- Post-bulk: `_do_bulk_miss_retry()` lanza `get_stock_with_reserve()` per-SKU en
  background (5s delay + 1s entre SKUs) para todos los bulk misses. Retorna valor
  real de BM (0 para SHIL000026 en LOC47+LOC68).

**Comportamiento esperado después del fix:**
Prewarm → bulk → SHIL000026 no en bulk → skip _store_wh → background retry →
get_stock_with_reserve devuelve (0,0) → cache = avail_total=0, _v=True →
Activar filter `(p.get("_bm_avail") or 0) > 0` = False → SHIL000026 desaparece de Activar.

---

## 2026-06-25 — FIX: Riesgo Sobreventa falso positivo por distribución escasez

### Commit `50c1d8b` — subido a Railway + Coolify

**Problema:** SKUs con regla de distribución activa (badge ⚡ ESCASEZ) y 0 ventas en 30 días
aparecían en Riesgo Sobreventa aunque BM tuviera stock real disponible.

Root cause: `_dist_apply_pool()` calcula `_bm_avail=0` cuando `scarce_enabled=False` +
0 ventas → daily_rate=0 → is_scarce=True → pool bloqueado. Pero `_bm_avail_raw=3` (BM tiene
unidades). El filtro `oversell_risk` verificaba `_bm_avail==0` — detectaba el 0 de distribución
como si BM estuviera vacío → falso positivo.

**Fix 1 — oversell_risk:** Agrega `and (p.get("_bm_avail_raw") or 0) == 0`. Solo entran
productos donde BM genuinamente tiene 0 unidades. Distribución-bloqueados con raw>0 quedan fuera.

**Fix 2 — imbalanced:** Cambia `_bm_avail` → `_bm_avail_raw` para detectar el gap real
MeLi vs BM. SNTV005362 (MeLi=18, BM raw=3) ahora aparece en Desbalance con el gap correcto
en lugar de en Riesgo Sobreventa.

SKUs corregidos: SNTV005362 (MTY Available=3), SNMC000198 (CDMX Available=3).

---

## 2026-06-25 — FIX: Alertas de stock — 4 bugs de ciclo y visibilidad

### Commits `a4b5117`, `b4e88ef` — subidos a Railway + Coolify

**Problema 1 — Cache BM stale sobrevive Railway redeploy (SHIL000026 = 549 persistía):**
La DB persiste entradas BM con `avail_total > 0`. Al reiniciar Railway con nuevo LOCATIONID,
la entrada cargaba con su timestamp original → `_cache_is_valid()` la declaraba fresca → el
bulk con `47,68` nunca corría para ese SKU → usuario seguía viendo 549 hasta que expiraba TTL.

Fix: `_load_bm_cache_from_db()` carga DB con `timestamp=0` (siempre expirado) → prewarm
re-fetcha TODOS los SKUs con la config actual del código → datos correctos en ~1-2 min.

**Problema 2 — TTLs demasiado largos:**
- `_BM_CACHE_TTL`: 900 → 420s (7 min) — ciclos de refresh más frecuentes
- `_STOCK_ISSUES_TTL`: 1800 → 900s (15 min) — alertas más responsivas

**Problema 3 — Synced items solo se ocultaban en la cuenta que hizo el sync:**
`get_recently_synced_ids` filtraba `WHERE user_id = ?`. Si APANTALLATEMX activaba un SKU,
AUTOBOT/LUTEMA seguían viéndolo en sus alertas → acciones duplicadas entre usuarios.

Fix: quitar filtro `user_id` de la query → cualquier sync de cualquier cuenta suprime el
item en alertas de TODAS las cuentas hasta el próximo ciclo BM.

**Problema 4 — "Actualizar ahora" solo refrescaba la cuenta activa:**
`force_prewarm` ejecutaba `_prewarm_caches(user_id=active_uid)` → solo una cuenta.
Las otras 3 cuentas seguían con alertas desactualizadas 15-30 min más.

Fix: `force_prewarm` encadena prewarm para TODAS las cuentas (activa primero, luego las demás).

---

## 2026-06-25 — FIX: Riesgo Sobreventa — falsos positivos por cache BM incoherente

### Commits `ac6af2d`, `7ea6125` — subidos a Railway + Coolify

**Bug:** SNTV005362 aparecía en "Riesgo Sobreventa" con BM=0 (Res:1) y MeLi=18,
pero BM en vivo confirmaba Available=3, Reserve=1. Múltiples listings del mismo SKU
afectados simultáneamente.

**Root cause:** El bulk BM devolvió un snapshot incoherente: `{AvailableQTY:0, Reserve:1}`
sin `TotalQty` para SNTV005362. Este triplete es físicamente imposible (TotalQty siempre
debe ser >= Reserve). La función `_cache_is_valid()` lo aceptaba como válido (`_v=True`)
y nunca disparaba retry per-SKU. El dato incorrecto quedaba congelado en cache hasta el
próximo bulk refresh.

**Fix 1 — `_cache_is_valid()` anti-ghost** (`app/main.py`):
```python
if data.get("reserved_total", 0) > 0 and data.get("total", 0) == 0 and data.get("avail_total", 0) == 0:
    return False  # snapshot incoherente → forzar retry per-SKU
```
Ahora cualquier entrada con reserve>0 pero sin total ni avail dispara un retry per-SKU
que obtiene los valores reales de BM (Available=3 correcto para SNTV005362).

**Fix 2 — Stagnant excluye `_synced_ids`** (`app/main.py`):
Productos recién sincronizados (últ. 60 min) ya no aparecen como "estancados"
antes del primer prewarm post-sync (tenían `units=0` aún sin actualizar).

**Auditoría completa de secciones:** Reabastecer, Activar, Critical, Full No Stock,
Price Risk, Imbalanced — lógica correcta. Sección Riesgo Sobreventa tiene guard correcto
`"_bm_avail" in p` que previene falsas alertas cuando BM no responde.

---

## 2026-06-25 — FEAT: NoVendibleQty display informativo

### Commit `b6c5680` — subido a Railway + Coolify

BM introdujo un nuevo campo `NoVendibleQty` (unidades en bodega físicamente presentes pero no disponibles para venta: dañadas, cuarentena, etc.). Confirmado via BM web: para SNTV001764, Total=1,601, Available=1,600, Not Sellable=845 — son campos **independientes**, BM ya calcula AvailableQTY correctamente.

#### Cambios implementados

**`app/main.py`:**
- `_EMPTY_BM`: añadido `"no_vendible": 0` al dict vacío
- `_store_wh()`: parámetro `no_vendible_direct=0`, almacenado en `inv` y en `_bm_stock_cache`
- `_lookup_diag()`: extrae `NoVendibleQty` de filas bulk BM, retorna 3-tupla `(avail, reserve, no_vendible)`
- Loop de fetch: desempaca `_avail, _res, _nvq`, pasa `no_vendible_direct=_nvq` a `_store_wh()`
- `_apply_bm_stock()`: propagado `p["_bm_no_vendible"]` en los 3 ramos (simple, variation parent, variation fallback)

**`app/templates/partials/products_inventory.html`:**
- Toggle "No Vendible" en toolbar de columnas (amber, oculto por defecto)
- Header `<th data-col="no_vendible">No Vend.</th>` oculto por defecto
- Celda por producto con valor en amber (solo muestra número si > 0)
- Mobile: badge "⚠ NoVend: N" en amber junto a Disp/Res, solo cuando > 0
- colspan de fila de variaciones: 18 → 19

#### Decisión de diseño
`NoVendibleQty` **NO** se resta de `AvailableQTY`. Es solo informativo. BM server-side ya calcula el Available correctamente; el campo Not Sellable es contexto adicional para el operador.

---

## 2026-06-25 — FIX: Auditoría lógica de stock — 3 bugs corregidos

### Commits `67d6103`, `84437ce` — subidos a Railway

Auditoría completa de la lógica de inventario reveló 3 bugs reales (de 7 hallazgos).

**Bug #2 — Fallback TotalQty activaba con AvailableQTY=0 genuino (sobreventa)**
- `_lookup()` y `_lookup_diag()` usaban TotalQty como fallback cuando `avail==0 AND reserve==0`
- Problema: si BM devolvía AvailableQTY=0 real (sin stock) pero con Reserve>0 no reportado,
  el sistema asumía `avail = TotalQty` → riesgo directo de sobreventa
- Fix: solo activar fallback si `ALL(AvailableQTY is None)` — campo genuinamente ausente de BM

**Bug #6 — Gap sync no normalizaba SKU antes de buscar en caché**
- `_sync_gap_stock_from_cache()` buscaba en `_bm_stock_cache` con el SKU raw (`SNTV001764-GRB`)
- El caché está indexado por `normalize_to_bm_sku()` (`SNTV001764`) → nunca hacía match
- Fix: `_bm_stock_cache.get(normalize_to_bm_sku(_sk))` — una línea, bug persistía silenciosamente

**Bug #4 — Distribución equitativa de pool ignoraba velocidad de venta por variación**
- `_apply_bm_stock()` dividía el pool por igual entre variaciones del mismo BM key
- Variación que vende 30/mes recibía mismo stock que una que vende 5/mes
- Fix: distribución proporcional por `sold_quantity` (lifetime ML de cada variación)
- La última variación absorbe el resto del redondeo para conservar todas las unidades
- Fallback a split equitativo para listings nuevos sin historial (sold_quantity=0)

**Bugs descartados (comportamiento correcto o intencional):**
- Bug #5: Tijuana excluida de `_bm_total` — intencional (LOC62 = informativo, no vendible)
- Bug #1: ICB/ICC solo para SNTV — correcto por diseño de categorías
- Bug #7 (NoVendibleQty): pendiente verificar si BM ya lo descuenta de AvailableQTY

---

## 2026-06-24 — FIX: BM bulk stock restaurado — payload corregido

### Commits `63ca079`, `b190a4f`, `6b2864d` — subidos a Railway

**Problema:** SNHT000293 mostraba 0 unidades en dashboard aunque BM web mostraba 37.
Diagnóstico: `Get_GlobalStock_InventoryBySKU` retornaba HTTP 500 para TODAS las variaciones
de payload que enviábamos. BM web (que funciona) usa exactamente el mismo endpoint.

**Root cause identificado** capturando el payload del browser web via DevTools:
`Arrayfilters_Condition: null` → BM lanza `NullReferenceException` (C# server-side).
BM requiere que `Arrayfilters_Condition` sea un **array de objetos** (`[{Condition, Name, selected}]`),
no `null`. Todos los demás `Arrayfilters_*` también deben ser `[]` (no `null`).

**Campos corregidos en el payload:**
- `Arrayfilters_Condition`: `null` → `[{Condition:"GRA", Name:"GRA", selected:true}, ...]`
- `Arrayfilters_Brand/Model/Size/Category/Tags/Supplier`: `null` → `[]`
- `NEEDFILE`: `false` → `true`
- `NEEDTIER`: `false` → `true`
- `OPENCELL`/`OCCOMPTABILITY`: `False` → `""` (string vacío, no bool)
- `Jsonfilter`: `"[]"` → `'[{"LRow":1,"FColumn":null,...}]'` (estructura completa)

**Archivos modificados:** `app/services/binmanager_client.py`
- `_GS_BASE_PAYLOAD` — base payload para `get_retail_price_ph()`
- `get_global_inventory()` — inventario sin filtro de ubicación
- `get_bulk_stock()` — bulk paginado (fuente del caché GR y ALL)
- `_query_bm_stock()` — consulta puntual por SKU

**Nuevo campo descubierto:** `NoVendibleQty` — unidades en stock pero no vendibles.
BM lo calcula server-side como campo independiente. No afecta `AvailableQTY`
(BM no lo descuenta de AvailableQTY). Queda disponible en los rows del bulk cache
para display futuro en dashboard.

**Verificado post-fix:**
- `SNHT000293`: avail=37, reserve=0 en caché y BM live ✅
- `SNTV001764`: avail=1602, reserve=0, NoVendibleQty=844 ✅
- Bulk cache GR: 1,827 filas | Bulk cache ALL: 1,984 filas ✅

---

## 2026-06-23 — FEAT: Admin Audit — Cuenta ML, Sección, Usuarios Activos

### Commit `2e2f3f2` — subido a Railway y Coolify

5 mejoras al módulo de auditoría de admin para control del personal remoto.

**1. Columna Cuenta ML en audit_log**
- `ALTER TABLE audit_log ADD COLUMN ml_account TEXT NOT NULL DEFAULT ''` (migration idempotente)
- `_audit()` en `items.py` llama `_get_ml_account_name()` → nickname desde tabla `tokens`
- `_render_timeline_rows` muestra la cuenta en columna amarilla `text-yellow-700`

**2. Columna Sección en audit_log**
- `ALTER TABLE audit_log ADD COLUMN section TEXT NOT NULL DEFAULT ''`
- `_derive_section(path)` en `items.py` mapea rutas a nombres: `/items/` → Items, `/ads` → Ads, etc.
- `AuthMiddleware` en `main.py` usa `_derive_audit_section()` para registrar en update_last_seen

**3. Timeline 7 columnas + alertas críticas**
- `_render_timeline_rows`: de 5 a 7 columnas (Cuenta ML + Sección entre Acción e Item/SKU)
- Acciones críticas (`ml_status_update`, `ml_item_closed`, `ml_concentration`) muestran fondo rojo + badge "CRÍTICO"
- `auditoria.html`: headers actualizados, `colspan="7"` en todos los TD vacíos, JS actualizado

**4. Panel "¿Quién está activo ahora?"**
- `user_last_seen` tabla: upsert por username con last_seen REAL, last_url, section, ml_account, ip
- `get_online_users(active_minutes=5)` con flag `is_online` (< 5 min) vs reciente
- `GET /api/users/audit/online` → HTML con tarjetas por usuario activo, dot verde/gris, cuenta, sección, tiempo hace
- `auditoria.html`: panel HTMX `hx-trigger="load, every 30s"` al tope de la página

**5. Filtro por Cuenta ML**
- `get_audit_log()` acepta parámetro `ml_account` → `WHERE ml_account = ?`
- `GET /api/users/audit/log`: parámetro `ml_account` nuevo
- `auditoria.html`: select "Todas las cuentas / APANTALLATEMX / AUTOBOT / BLOWTECHNOLOGIES / LUTEMAMEXICO"

---

## 2026-06-23 — FEAT: Auditoría dashboard — Batch 3 (últimas 4 mejoras)

### Commit `ca57252` — subido a Railway y Coolify

**H4.4 — Historial de cambios por item**
- Nueva tabla `item_history` en `token_store.py`: item_id, field, old_value, new_value, changed_by, changed_at
- `save_item_change()` + `get_item_history()` en `token_store.py`
- Hooks en `app/api/items.py`: endpoints price, title, description, stock, status registran cada cambio via `asyncio.create_task`
- `GET /api/items/{id}/history` — nuevo endpoint, devuelve últimos 50 cambios
- `item_edit_modal.html`: botón "Ver historial de cambios" con panel expandible + renderizado JS

**H3.2 — Mobile responsive fixes**
- `orders_table.html`: desktop table envuelta en `overflow-x-auto` + `min-w-[900px]`
- `ads_campaigns.html`: desktop table ídem
- Mobile order cards: añadido `net_pct` (% vs retail) con color coding verde/amarillo/rojo

**H4.1 — Widget ML vs Amazon (30d)**
- `GET /api/orders/platform-comparison` — JSON con revenue, órdenes, avg_margin, ganancia por plataforma
- `GET /partials/platform-comparison` — HTML listo: dos columnas, barra de distribución de ingresos
- `orders.html`: widget cargado con HTMX `hx-trigger="load"` + `outerHTML` swap

**H1.4 — Consistencia de colores**
- `dashboard.html`: active period buttons `bg-blue-600` → `bg-yellow-400` (HTML + JS)
- `returns.html`: `setGlobalPlatform` active state `bg-blue-500` → `bg-yellow-400`

---

## 2026-06-23 — FEAT: Auditoría dashboard — Batch 2 (mejoras con backend)

### Motivación
Segunda ronda de la auditoría del dashboard: mejoras que requieren backend (endpoints nuevos), un nuevo indicador visual global (barra de progreso HTMX), comparativa histórica de métricas, CTR en tabla de campañas, búsqueda en tabla, exportación CSV.

### Cambios — commit pending

**H1.3 — Comparativa histórica en P&L de órdenes**
- Nuevo endpoint `GET /api/orders/period-stats?date_from&date_to`
- Consulta `order_history` SQLite para el periodo actual Y el periodo anterior (misma duración, shifted back)
- Sin llamadas extra a ML API — es instantáneo
- JS en `orders_table.html` muestra badges ▲↑% / ▼↓% sobre las tarjetas de Ventas brutas y Neto MeLi
- Solo se activa cuando hay `date_from` y `date_to` definidos en la URL

**H2.1 — Barra de progreso HTMX global**
- `base.html`: `<div id="htmx-bar">` — línea de 2px en amarillo en el top del viewport
- Animación: 0→70% durante request, 70→100% al completar, fade out
- Se dispara en todos los `htmx:beforeRequest` / `htmx:afterRequest` del sitio

**H2.3 — CTR en tabla de campañas Ads (desktop)**
- `ads_campaigns.html`: columna CTR añadida al header y data row del desktop table
- Colores: verde si CTR>0.5%, amarillo si 0.2–0.5%, gris si bajo
- colspan fila expandida actualizado a 12

**H2.6 — Traducción status desktop en campañas**
- Fallback status en desktop ya usa dict `enabled/disabled/archived → español` (igual que mobile)

**H3.1 — Búsqueda en tabla de órdenes (client-side)**
- Input de búsqueda con ícono de lupa sobre la tabla de órdenes
- Filtra tanto tarjetas mobile (`data-order-card`) como filas desktop (`data-order-row`) mientras se escribe
- Solo filtra la página actual (server-side pagination no cambia)

**H4.2 — Exportar CSV de órdenes**
- Nuevo endpoint `GET /api/orders/export.csv?date_from&date_to`
- Consulta `order_history` (max 5000 filas) con BOM UTF-8 para Excel
- Botón "CSV" con ícono de descarga junto al buscador en la tabla

---

## 2026-06-23 — UX: Auditoría dashboard — 10 mejoras batch 1

### Motivación
Auditoría completa del dashboard identificó 20 mejoras agrupadas por prioridad. Se implementa Batch 1 (mejoras puras de frontend, sin backend requerido).

### Cambios (sin commit aún)

**H1.1 — `health.html` + `health_claims.html`: Confirmación 2 pasos en reclamos**
- `respondClaim()`: validación mín. 10 caracteres antes de enviar
- Primer clic → botón cambia a "¿Confirmar envío?" (naranja, timeout 6s)
- Segundo clic → envía (previene envíos accidentales de reclamos a ML)
- Contador de caracteres inline en el textarea (`claim-chars-{id}`)

**H1.2 — Empty states mejorados**
- `orders_table.html:141,398` (mobile + desktop): icono + contexto + CTA
- `health_claims.html:278`: icono verde checkmark + mensaje contextual + hint de fechas
- `returns_table.html:355`: icono verde + texto más específico + hint de cuenta/fechas

**H2.2 — `orders_table.html`: `hx-push-url="true"` en paginación**
- Botones Ant/Sig/números ahora actualizan la URL del browser al paginar
- Permite compartir enlace a página específica y usar el botón Atrás del navegador

**H2.4 — `orders_table.html`: Timestamp + botón Refrescar**
- Barra superior con hora de última carga (HH:MM) + botón "Refrescar" con ícono
- Recarga la tabla completa con los mismos filtros activos

**H2.5 — `item_edit_modal.html`: Validaciones + char counters**
- Descripción: contador `desc-len` actualizado en tiempo real (en car.)
- Precio: mínimo $1 (min="1"), bloquea guardado si ≤ 0, muestra error en español
- Price hint: muestra el valor formateado ($1,500.00 MXN) bajo el input al tipear

**H2.6 — Traducción de strings en inglés**
- `ads_campaigns.html`: fallback status en Jinja → dict `enabled/disabled/archived` → español
- `orders_table.html`: fallback status en Jinja → dict `pending/confirmed/payment_required/...` → español

**H3.3 — `items.html`: Toast responsivo**
- `showToast()`: detecta `window.innerWidth < 768` → mobile usa `bottom-4 left-4 right-4`, desktop usa `top-4 right-4 max-w-sm`

**H3.4 — `health_claims.html`: Scroll conversación**
- `scroll-smooth` añadido al div de conversación del reclamo (max-h-52)

**H4.3 — `products_stock_issues.html`: Confirmación bulk restock**
- `bulkSyncRestock()`: diálogo de confirmación antes de ejecutar con cantidad de productos afectados

---

## 2026-06-22 — UX: Deals LIGHTNING/DOD — banner informativo, advertencia precio, errores traducidos

### Motivación
El flujo de activación de LIGHTNING/DOD deals era confuso: el usuario no sabía qué precio exige ML, el error de "precio no creíble" llegaba en inglés técnico, y el campo de stock no explicaba qué significaba comprometer unidades. Se mejora toda la UX del panel expandible sin tocar backend.

### Cambios — Commit 07baf3a

**Frontend `products_deals.html`:**
- Banner amber en panel LIGHTNING/DOD: explica mecánica del deal, muestra precio asignado por ML y fechas del deal
- Label precio cambia a "Precio de participación" para LIGHTNING/DOD
- Advertencia naranja inline si el usuario cambia el precio más de 5% del precio asignado por ML
- `window.checkDealPrice(idx, mlDealPrice)`: función que activa/desactiva la advertencia al editar el precio
- Sección stock: label mejorado "Unidades a vender en el deal" + subtítulo explicativo de la mecánica
- Traducción de errores ML: `ERROR_CREDIBILITY_DISCOUNTED_PRICE` → mensaje claro en español; stock error, price_not_allowed, OFFER_ALREADY_EXISTS también traducidos

---

## 2026-06-22 — FEAT: Deals tab mejoras + Agente ML Ads actualizado

### Motivación
Mejorar la sección Deals dentro del tab Productos para tomar mejores decisiones: saber cuándo vencen los deals activos, ordenar candidatos por oportunidad real (no solo ventas), filtrar por margen mínimo. Actualizar el agente mercadolibre-strategist con knowledge de PADS/BADS escritura y match types.

### Cambios — Commit 16fbe47

**Backend `main.py` (`GET /partials/products-deals`):**
- `_days_remaining`: calcula días restantes para cada deal activo usando `_promo_finish`
- `_opp_score`: score de oportunidad para candidatos = `(ventas×3.0) + (max(0, margen-10)×0.8) + (min(bm_stock,60)×0.25)`
- Recomendación "deals por vencer en 5 días" insertada como prioridad máxima (index 0)

**Frontend `products_deals.html`:**
- Columna "Tipo / Vence" en tabla de deals activos: badge de tipo + badge días restantes (rojo <2d animado, naranja <5d, gris >5d)
- Mismo badge en tarjetas mobile de deals activos
- Botón "Score" (indigo, default activo) en sort controls de candidatos
- Select "Margen mínimo" (0%, 5%, 10%, 15%, 20%) para filtrar candidatos
- Columna "Score" en tabla candidatos con badge por tier (≥30 indigo, ≥15 azul, ≥5 gris, <5 gris claro)
- `data-score` y `data-margin-val` en cada fila `<tr>` de candidatos
- JS: `window.filterByMargin()` + `paginateTable` respeta `margin-filter` via `data-margin-val`

**Agente `.claude/agents/mercadolibre-strategist.md`:**
- PADS ESCRITURA: create/update campaigns, ad groups, pausa <30d preserva modelo
- BADS match types: BROAD/PHRASE/EXACT con estrategia por fases (lanzamiento → optimización → escala)
- BADS escritura: payload completo CREATE/UPDATE, advertencia migración jun-17-2026 retorna 204
- Catálogo PAds: identificación dual, diferencia subasta buy box vs resultados, family_id variantes
- Bonificaciones lifecycle: balance consumption, expiración, no-apilamiento, validación

---

## 2026-06-22 — FEAT: Ads tab mejoras 1-7 — ROAS primario, IS%, estrategia real, bonificaciones, Brand Ads tab, ops UX

### Motivación
Refactorización completa de la pestaña Mercado Ads para alinearse con la API 2026 (ROAS reemplaza ACOS deprecated Mar-2026), agregar columna IS% de impression share, estrategia real desde API, bonificaciones de créditos, tab Brand Ads con aviso de migración Jun-17-2026, y mejoras de operaciones (botón +20% budget, input ROAS objetivo).

### Cambios — Commit 643dd4e

**Backend `meli_client.py`:**
- `get_ads_campaigns`: agrega `acos` a métricas solicitadas
- Migración paths write: `update_campaign`, `create_campaign`, `assign_items_to_campaign` → `/advertising/MLM/` (sin `/marketplace/`)
- Nuevo `get_bads_campaigns()` — Brand Ads search con manejo de migración Jun-17-2026

**Backend `main.py`:**
- `_enrich_campaigns`: agrega `strategy` (PROFITABILITY/INCREASE/VISIBILITY con fallback por ACOS), `roas_target`, `acos` real de API
- `POST /api/ads/campaigns/{id}`: pasa `roas_target` al `update_campaign`
- `POST /api/ads/campaigns`: pasa `roas_target` + `strategy` al `create_campaign`
- `POST /api/ads/campaigns-with-items`: pasa `roas_target` + `strategy`
- Nuevo `GET /partials/ads-brand` — partial Brand Ads

**Frontend `ads.html`:**
- KPI bar: ROAS primero, ACOS marcado como legacy (opaco)
- `kpiCardAcos()` — variante opaca para KPIs deprecated
- `loadBonificaciones()` — carga async créditos de ads, muestra alerta si vencen ≤7 días, oculto si vacío
- Tab "Brand Ads" + panel `panel-brand`
- `saveRoasTarget(campaignId, inputId, btn)` — reemplaza `saveAcos()` (deprecated)
- `quickBudgetIncrease(campaignId, current, inputId, btn)` — aplica +20% al budget en un click

**Frontend `ads_campaigns.html`:**
- Desktop: nueva columna IS% (impression share %) con badge -X% budget si pierde >10%
- Desktop: celda ROAS muestra objetivo en pequeño debajo del valor real
- Badge Estrategia: usa campo real `c.strategy` (PROFITABILITY/INCREASE/VISIBILITY) desde API + fallback
- ACOS expandido: marcado como "(legacy)", fuente gris
- ROAS objetivo en expandido: campo dedicado
- ACOS Target input → ROAS Target input (min=1, max=35, step=0.5)
- Botón "+20%" junto a Guardar presupuesto
- Perdidas x Ranking en expanded row (lost_by_rank)

**Frontend `ads_brand.html`** (nuevo):
- Muestra aviso migración Jun-17-2026 si BADS retorna vacío
- Tabla de campanas BADS si existen datos activos

### Nota técnica
ML API campaigns endpoint rechaza `impression_share` como métrica (cambio de API). IS% column queda cargada con 0 hasta identificar endpoint correcto. El campo existe en `_enrich_campaigns` listo para cuando se resuelva.

---

## 2026-06-19 — FEAT: Dashboard ML completo — 8 mejoras de ventas (CVR, ratings, tendencias, best sellers, purchase experience, returns breakdown, atributos, mensajes)

### Motivación
Continuación del análisis de docs ML. Implementación de las 8 mejoras restantes del plan de optimización aprobado.

### Cambios — Commit 731c954

**CVR (Tasa de Conversión) por listing:**
- `meli_client.get_items_visits_bulk()` — `GET /items/visits?ids=...&date_from=...&date_to=...` en chunks de 50
- `products_inventory_partial`: agrega visits bulk como tarea [2] en parallel_tasks (siempre, no solo full)
- Calcula `_cvr = units_30d / visits_30d * 100`; `_visits_30d` guardado para contexto
- `products_inventory.html`: columna CVR con badge rojo (<1%), amarillo (<3%), verde (≥3%); visible en preset top/full/accion
- Mobile cards: badge CVR inline

**Ratings/Reseñas por listing (enrich=full):**
- `_get_page_reviews()` helper — fetcha ratings para toda la página en paralelo (sem=5)
- Agrega `_rating` (avg) y `_rating_count` al product
- `products_inventory.html`: columna Rating con estrellas (★★★★☆); visible en preset top/full

**Widget de tendencias ML en Dashboard:**
- `GET /partials/trends-widget` — renderiza top 20 búsquedas semanales con grid 2/4 cols
- `trends_widget.html`: rank dorado para top 3, grid responsivo
- `dashboard.html`: auto-carga con `delay:4s`

### Cambios — Commit 8d0b7ee

**Returns — Desglose por tipo:**
- `returns_summary_partial`: agrega `by_category` {pdd/pntr/other} y `by_stage` {claim/dispute} al namespace summary
- `returns_summary.html`: bloque "Desglose por tipo de reclamo" con barras visuales (categoría + stage)
- `returns_table_partial._refresh_status()`: guarda `claim_type`, `affects_reputation`, `has_incentive` del detail
- `returns_table.html`: badge "⚠ Afecta reputación" (rojo) y "Acción requerida" (naranja pulsante)

### Cambios — Commit 4e12fd4

**Purchase experience (penalización) por listing (enrich=full):**
- `meli_client.get_purchase_experience()` — `GET /marketplace/items/{id}/purchase_experience`
- `_get_page_purchase_experience()` — detecta listings penalizados, sets `_px_penalized`, `_px_penalties`
- Badge rojo "⚠ Penalizado" en columna ID/SKU (desktop y mobile)

**Best sellers Top 20 por categoría (enrich=full):**
- `meli_client.get_category_highlights()` — `GET /highlights/MLM/category/{category_id}`
- `_get_best_sellers_for_page()` — con cache 24h por category_id, marca `_is_bestseller`
- Badge dorado "🏆 Top 20" en columna ID/SKU si listing aparece en top 20 de su categoría

### Cambios — Commit 03d1db6

**Atributos incompletos — widget dashboard:**
- `GET /partials/attributes-widget` — llama `GET /users/{user_id}/attributes?v=3`
- `attributes_widget.html`: barras visuales por categoría con items_to_fill count
- `dashboard.html`: auto-carga con `delay:5s`

**Mensajes no leídos — nav badges funcionando:**
- `GET /api/health/counts` — **endpoint faltante implementado** (nav badges estaban silently failing)
- Retorna: `{claims: N_abiertos, questions: N_sin_responder, messages: N_no_leidos}`
- Badges en base.html ahora muestran contadores reales
- `GET /api/ml/unread-count` — endpoint standalone para mensajes

---

## 2026-06-19 — FEAT: Dashboard upgrades — neto real, /performance, ads throttle, reputación recovery, claims impact

### Motivación
Análisis completo de docs ML (mensajería, reclamos, reputación, tendencias, best sellers, calidad, visitas,
atributos, tienda oficial, Mercado Ads PADS/BADS) + auditoría del dashboard → 15 mejoras priorizadas.

### Cambios implementados (commit 17d5b9f)

**Bug crítico — Neto real en ML Analyzer:**
- Live order ahora llama `/collections/{payment_id}` para obtener `net_received_amount` real
- Si la orden search omite payments, hace fetch individual `/orders/{id}` para obtenerlos
- Neto proporcional por ítem (ratio = item_subtotal / total_order)
- `_live_row` ahora se llena con `data_source='real'` inmediatamente, sin esperar el tab Orders

**Bug crítico — Migración /health → /performance:**
- `get_item_health()` en meli_client ahora llama `/item/{item_id}/performance` (deprecado /items/{id}/health)
- Devuelve: `score` (0–100), `level_wording` (Profesional/Bueno/Regular/Malo), `buckets[]`
- `_enrich_with_meli_health()` expone `_meli_perf_score`, `_meli_perf_wording`, `_meli_perf_buckets`
- `get_item_health_actions()` reescrito: extrae `variables` con status bad/regular de cada bucket

**Ads — Budget throttling:**
- `get_ads_campaigns()` ahora pide `impression_share`, `lost_impression_share_by_budget`,
  `lost_impression_share_by_ad_rank` (campo `acos` deprecado mar-2026 → calculado localmente)
- `_enrich_campaigns()`: nuevo campo `throttled_by_budget` (True si >20% impresiones perdidas x budget)
- `ads_campaigns.html`: alerta roja si `throttled_by_budget`, badge "Budget" en nombre de campaña,
  grid expandido con share de impresiones y perdidas por budget

**Ads — Bonificaciones:**
- `get_ads_bonificaciones()` → `GET /advertising/advertisers/bonifications`
- Endpoint `GET /api/ads/bonificaciones`: retorna créditos con `days_remaining`, `balance`, `alert`
  (True si ≤7 días y saldo > 0)

**Tendencias:**
- `get_trends()` → `GET /trends/MLM[/{category_id}]`
- Endpoint `GET /api/trends[?category_id=]`: top 50 búsquedas semanales

**Salud — Recuperación de reputación:**
- `get_reputation_recovery_status()` → `GET /users/reputation/seller_recovery/status`
- `health_reputation_partial` llama en paralelo `get_user_info()` + `get_reputation_recovery_status()`
- `health_reputation.html`: banner azul si AVAILABLE ("¡Puedes activar protección!"),
  banner verde si ACTIVE (muestra fecha fin + días restantes)

**Salud — Claims con impacto real:**
- `_refresh_status()` guarda `affects_reputation` y `has_incentive` del detail endpoint
- `health_claims.html`: badge `⚠ Afecta reputación` (rojo) / `✓ No afecta rep.` (verde)
  + badge `Acción requerida` (naranja) cuando `has_incentive=True` y status=opened

**Nuevos métodos en meli_client.py:**
- `get_ads_bonificaciones()`, `get_reputation_recovery_status()`, `get_trends()`,
  `get_missed_feeds()`, `get_item_reviews()`

---

## 2026-06-18 — FEAT: Bridge Salud↔Retornos + tasa oficial ML en retornos

### Mejoras de integración entre Salud y Retornos

**Bridge Salud → Retornos:**
- Card "Tasa Reclamos" en Salud: muestra "Ver análisis en Retornos →" cuando status es yellow/orange/red
- Panel de alertas laterales: entry de claims rate orange/red incluye "Ver análisis detallado →"
- Links incluyen `?date_from=X&date_to=Y` del período activo en Salud para cargar el mismo rango en Retornos

**Tasa oficial ML en Retornos:**
- Card "Tasa de Retorno" ahora muestra dos tasas: empírica (claims/órdenes del período) + oficial ML (seller_reputation 60d)
- Tasa oficial usa misma escala de colores (≥5%=rojo, ≥2%=amarillo, <2%=verde)
- Backend: `get_user_info()` en paralelo con `_fetch_all_claims_cached` (mejora latencia, elimina llamada duplicada)

**URL param reading en Retornos:**
- `returns.html` ahora lee `date_from`/`date_to` de query params al cargar
- Si llega vía bridge desde Salud, precarga automáticamente el período correcto y marca preset 'custom'

**Commit:** d64ae78

---

## 2026-06-17 — FEAT: Returns — Fotos cliente, Análisis IA, Quality Score, Compartir equipo

### Cambios
**Backend (4 nuevos endpoints):**
- `GET /api/returns/claim-photos/{claim_id}`: extrae fotos adjuntas de mensajes ML (`get_claim_messages`)
- `POST /api/returns/ai-analysis`: análisis Claude — causa raíz, patrón, recomendaciones, score 0-100, checklist prevención, texto compartir
- `POST /api/returns/share-report`: genera texto WhatsApp + Slack formateado con datos + análisis IA
- `GET /api/returns/quality-scores`: score 0-100 por item (count×8 + severidad razón + open penalty), grados A/B/C/D/F

**Frontend (`returns.html` + `partials/returns_table.html`):**
- Botón "🤖 Analizar IA" en cada card Top SKUs → modal con análisis estructurado (causa raíz, patrón, recomendaciones, checklist)
- Botón "📤 Compartir" en Top SKUs → modal con WhatsApp/Slack copy-to-clipboard
- Botón "📷 Fotos" en cada reclamo de la tabla → galería inline bajo demanda
- Widget Quality Score en sidebar: top 5 SKUs peores + barra proporcional + grado A/B/C/D/F
- Panel de alertas en top página: crítico (grado F u opened>2) y advertencia (grado D)

**Commit:** 36444af

---

## 2026-06-16 — FEAT: Competencia + Veredicto + Última venta en Analizador ML

### Cambios
**Backend (`/api/ml/item-analysis`):**
- Captura `_comp_items_raw` de `/products/{id}/items` en el path de catálogo
- Para items regulares intenta via `catalog_product_id`
- Retorna `competition{}`: total_sellers, min/max price, winner_is_full, has_official_store, new/used sellers
- `real_sales` ahora incluye: last_price, last_fee_pct, last_net_ml, last_socio, last_neto_final

**Frontend (`orders.html`):**
- Línea "Última venta" bajo las 3 cards (precio real más reciente → neto → % → fecha)
- Bloque **Competencia**: N vendedores con color semáforo, rango de precios, badges FULL/tienda oficial/usados
- Bloque **Veredicto**: 4 semáforos (🟢🟡🔴) → Demanda, Competencia, Margen neto, Logística
- Resolución overall: ✅ Viable / ⚠️ Riesgo / 🚫 No recomendado

**Commits:** 5abcd58

---

## 2026-06-16 — FIX: Eliminar IVA sobre comisión del cálculo neto ML

### Problema
El analizador descontaba IVA sobre la comisión como cargo separado (fee × 16% ~2%).
El breakdown real de ML NO incluye ese IVA como deducción:
`Cargos (12.5%) + Impuestos (9.05%) + Envío` — sin IVA extra.
Esto causaba ~$112 de diferencia en neto para precio ~$5,600.

### Cambios
**Backend (`app/main.py` — `real_sales` block):**
- Eliminado `_avg_iva = fee × 16%` del cálculo
- `_avg_net_ml = price - fee - imp(9.05%) - shipping` (correcto)

**Frontend (`orders.html`):**
- Eliminado `displayIva` y referencia a `rs.avg_iva`
- Label "Comisión+IVA" → "Comisión ML"
- Calculadora `_calcMlProfit`: eliminado `ivaFee = feeAmt × 0.16` del `mlTotal`
- Texto desglose calculadora: eliminado "IVA fee" de la cadena

**Commits:** da25e29

---

## 2026-06-16 — FIX: Analizador ML usa datos reales de ventas (order_history)

### Problema
El analizador mostraba precio catálogo ($7,699) + envío plano $80. En una venta real de TCL 55":
precio real=$5,851 · envío real=$281 · **neto real=$4,007 vs $5,403 estimado (-$1,396 error)**

### Cambios
**Backend (`/api/ml/item-analysis`):**
- Cuando `in_our_catalog=True`, consulta `order_history` por `item_id` + `sku`
- Calcula promedios reales de las últimas 20 ventas: `avg_price`, `avg_fee_pct`, `avg_fee_amt`, `avg_imp`, `avg_ship_est`, `avg_net_ml`, `avg_socio`, `avg_neto_final`
- Tiers de envío mejorados: $80/<$1k, $130/<$2.5k, $200/<$5k, $300/<$8k, $400/≥$8k
- Retorna `real_sales: {...}` en la respuesta JSON

**Frontend (`orders.html`):**
- Cuando `real_sales.count >= 1`: usa precio real promedio en lugar de precio catálogo
- Precio real prominente + precio catálogo tachado cuando difieren >$50
- Badge verde "✓ X ventas reales" en header del producto
- Cards Fees ML y Neto recibido con valores reales
- Calculadora pre-llenada en verde con datos reales + banner de confirmación
- Cuando no hay datos reales: mantiene estimados con label "est."

Commit: `d436dde`

---

## 2026-06-16 — FEAT: Analizador ML — card Vendidas prominente + Neto recibido completo

### Contexto
Al analizar productos en el analizador ML, faltaba visibilidad inmediata de cuántas unidades se venden y cuánto neto se recibe realmente después de TODOS los descuentos.

### Cambios (orders.html — `_renderMlItem`)
- **Card "Vendidas"**: `sold_quantity` como número grande (text-xl font-black) + subtexto "en X días" o "en catálogo" + badge demanda + ud/día abajo
- **Card "Neto recibido"**: número verde grande + `X% del precio` + desglose completo:
  - `-Comisión+IVA` (fee real de ML)
  - `-Imp. ML (~9%)` estimado sobre precio
  - `-Socio (7%)` sobre neto post-impuestos
  - `-Envío absorbido` solo si `free_shipping=true`
- Eliminado el warning "imp. ML ~9% no incluidos" — ahora ya están incluidos
- Fórmula: `netML = precio - fees`, `impAmt = precio×9%`, `socioAmt = (netML-imp)×7%`, `netEst = netML - imp - socio`

### Ejemplo MLM59200042 (Robot Eufy E25, $14,999)
- Vendidas: **0 uds** en catálogo · Demanda Baja · 0.0 ud/día
- Neto recibido: **$9,538** (63.6%) — fees $3,393 + imp $1,350 + socio $718

Commit: `ca417e4`

---

## 2026-06-16 — FEAT: Neto ML con desglose completo — socio 7% + impuestos ML ~9%

### Contexto
El neto estimado solo restaba la comisión ML + IVA sobre comisión. Faltaban dos deducciones reales que sí aparecen en los extractos de ML:
- **Impuestos ML**: ~9% del precio de venta (ej. orden Samsung: $906.10 / $10,010 = 9.05%)
- **Comisión socio**: 7% del neto después de todos los descuentos ML

### Cambios (dashboard.html + orders.html)
- **Neto card** muestra desglose: Neto ML → -Socio 7% → Total neto (con nota "imp. ML ~9% no incluidos")
- **COGS Calculator** agrega dos campos nuevos: `Impuestos ML (%)` default 9, `Comisión socio (%)` default 7
- **`calcFor`** actualizado: `impAmt = price * impPct / 100`, `socioAmt = netML * socioPct / 100`
- **Desglose** al pie de la tabla muestra cadena completa: comisión + IVA + imp → neto ML → socio → neto proc
- **Precio editable** para catalog products (amber input) ya implementado en sesión anterior

### Fórmula
```
netML   = precio - (comisión + IVA_comisión + impuestos) - envío_absorbido
socio   = netML × 7%
netProc = netML - socio
```

Commit: `6f79b69`

---

## 2026-06-15 — FIX: Analizador ML soporta productos de catálogo (/p/MLM... URLs)

### Problema
`MLM59200042` (URL `/p/MLM...`) es un **catalog product**, no un listing individual.
`GET /items/{id}` devuelve 404. `GET /products/{id}` devuelve el catálogo pero sin precio.

### Solución verificada via diag endpoint
- Flujo: `GET /products/{id}` → nombre/categoría/imágenes, luego `GET /products/{id}/items` → `[{item_id, price, listing_type_id, ...}]`
- `GET /items/{item_id_del_listing}` da "access_denied" (item de otra empresa) → fallback a `price` del entry de `/products/{id}/items`
- Resultado confirmado con diag: precio `$17,999.01`, categoría `MLM120568`, tipo `gold_pro`
- Badge "Catálogo ML" (teal) distingue estos productos de listings directos
- Commits: `72a5a2b` (fix), `61ce262` (diag)

---

## 2026-06-14 — FEAT: Analizador ML + Filtro período en página Ventas (orders.html)

### Corrección de ubicación + mejoras
- **Análisis de Producto ML** movido a la página `/orders` (Ventas) donde corresponde — antes estaba solo en Dashboard
  - Input MLM/link completo, demanda, fees, neto estimado, calculadora COGS inversa — idéntico al del Dashboard
- **Filtro de período** (Hoy / 7d / 15d / 30d / Todo) + date pickers personalizados en barra de filtros de Ventas
  - Actualiza hidden inputs `date_from`/`date_to` y dispara `htmx ordersFilter`
  - `hx-include` de la tabla incluye ambos inputs → el backend filtra por fecha correctamente
- **Paginación preserva fechas**: `_date_params` inyectado en `_base` y `base_url` del template `orders_table.html`
- Commit: `a5310a1`

---

## 2026-06-14 — FEAT: Analizador de Producto ML + Rentabilidad por venta en Dashboard

### Nuevas funciones
- **Análisis de Producto ML** (nueva sección en dashboard):
  - Input acepta ID MLM (`MLM...`, `MLMU...`) o link completo — extrae el ID automáticamente
  - Demanda real: `sold_quantity / days_on_market` → velocidad exacta en ud/día + tier (Muy Alta/Alta/Media/Moderada/Baja)
  - Fees exactos desde ML API `/sites/MLM/listing_prices` + IVA 16% sobre comisión
  - Lookup en catálogo local (`ml_listings` DB): badge "En catálogo" o "No en catálogo"
  - Neto estimado = precio − comisión − IVA − envío estimado
  - **Calculadora COGS inversa**: igual que Amazon pero con fees reales de ML (sin estimaciones)
    - Inputs: precio venta, comisión %, costo envío, otros costos, margen objetivo %, aranceles %, flete/ud
    - Resultado: COGS máximo ex-fábrica + tabla 15/20/25/30% + desglose completo
- **Últimas Ventas** — filtros de período (Hoy/7d/15d/30d) en header de sección
- **Columna de rentabilidad** en cada fila de Últimas Ventas:
  - Monto neto destacado (verde si > 0)
  - Badge de margen % con semáforo (verde ≥20%, amarillo ≥10%, rojo <10%)
  - Monto bruto secundario
- Commit: `6311b2a`

---

## 2026-06-12 — FEAT: Filtros de período y fulfillment en Últimas Órdenes Amazon

### Cambios
- Nueva barra de filtros en el header de la sección: selector de días (Hoy/7d/15d/30d) + botones Todos/FBA/FBM
- Backend: parámetros `days` (1–30) y `fulfillment` (all/FBA/FBM); SP-API recibe `FulfillmentChannels=AFN/MFN`
- Número de órdenes mostradas escala con el período: 5/10/15/20 para 1/7/15/30 días
- Enriquecimiento de items limitado a 8 órdenes sin importar el período (evita timeouts)
- Cache key incluye seller+days+fulfillment para aislar resultados por combinación de filtros
- Commit: `9116fd8`

---

## 2026-06-12 — FEAT: Calculadora de Precio de Compra + Veredicto en ASIN search (v2)

### Cambios (reemplazo del simulador v1)
- Lógica invertida: dado precio de venta + margen objetivo → calcula **COGS máximo ex-fábrica** permitido
- Veredicto con 3 dimensiones: Buy Box (señal de brand owner), Demanda (BSR tier), Espacio de margen (ratio de fees)
- Resultado principal: precio tope destacado en grande (verde/rojo según viabilidad)
- Tabla de 4 escenarios: 15% / 20% / 25% / 30% margen → COGS máx en cada caso
- Auto-cálculo al abrir el simulador si hay precio de Buy Box disponible
- Todos los campos editables en tiempo real (precio venta, referral %, FBA fee, storage, aranceles %, flete/ud)

---

## 2026-06-12 — FEAT: Simulador de Rentabilidad FBA en ASIN search

### Cambios
- `app/static/js/amazon_dashboard.js`: nueva sección colapsable "Simulador de Rentabilidad FBA" dentro del card de resultado ASIN.
- **Inputs editables**: referral fee % (auto-detectado por categoría), FBA fulfillment fee, storage/mes, costo ex-fábrica, aranceles %, flete/ud.
- **Tabla de margen en tiempo real**: 3 escenarios (−10%, buy box actual, +5%) con Fees AMZ, Landed cost, Margen $ y Margen %.
- **Señal de viabilidad Buy Box**: detecta automáticamente si hay brand owner directo por review count > 5000 (verde/amarillo/rojo).
- **Recomendación de compra inicial**: 1ª orden (~6 semanas) y orden de validación (~3 meses) basada en BSR tier + competencia + share estimado.
- Sin cambios en backend — 100% JS frontend.

---

## 2026-06-12 — FIX: ASIN search 500 Internal Server Error — `AmazonClient.close()` no existe

### Causa raíz
El endpoint `/api/amazon/asin-search` tenía un bloque `finally: await client.close()` pero `AmazonClient` NO tiene método `close()` (usa httpx por solicitud, no conexión persistente). En Python, una excepción en `finally` descarta el `return` y propaga la excepción hacia arriba → FastAPI devolvía "Internal Server Error" en texto plano → el JS fallaba al parsear JSON.

La misma falla silenciosa existía en el helper de refunds (`_get_amazon_refunds_cached`): el `finally: await client.close()` en un inner-try era atrapado por el outer-except y siempre retornaba `[]`.

### Fixes aplicados
1. **ASIN search** (`main.py` ~9875): eliminado `finally: await client.close()` — el endpoint ahora retorna JSON correctamente.
2. **Refunds helper** (`main.py` ~13238): colapsado inner-try + finally en un único try/except — refunds ahora puede retornar datos reales.

### Probado localmente
`GET /api/amazon/asin-search?asin=B0GWRX14QJ&days=30` → HTTP 200, JSON válido.

---

## 2026-06-09 — FEAT: Wizard inteligente — campos dinámicos por categoría, auto-detect PT, web search

### Commit: 75f3513

### Cambios implementados
1. **Campos dinámicos por categoría**: Panel "Atributos requeridos por categoría" en paso 3 del wizard. Cuando se selecciona un product type, el wizard carga los `field_defs` del template validado (GET /template-fields) y renderiza los campos específicos de esa categoría con sus defaults correctos. Soporta select, boolean, number, multi_select, text.

2. **Auto-detect product type**: GET /detect-product-type mapea BM category → Amazon product type usando `_BM_CATEGORY_TO_PT` (30+ categorías) y `_SKU_PREFIX_TO_PT` (SNTV→TELEVISION). Se dispara automáticamente al entrar al paso 3 del wizard. Badge verde "✅ Auto-detectado desde categoría BM" cuando funciona.

3. **Web search 3 fuentes paralelas**: `_research_product_specs` ahora usa 3 fuentes en `asyncio.gather`: UPC ItemDB (real data) + AI knowledge base (antes deshabilitado por api_key guard) + DuckDuckGo+Jina Reader (web scraping). Merge: UPC > web > AI.

4. **category_attrs en create_listing**: El payload del wizard puede incluir `category_attrs` (dict) con los valores del panel dinámico. El backend los aplica correctamente incluyendo: `language_tag: es_MX` para power_source_type en MX, booleans, multi-select lists, country ISO mapping.

5. **9 fotos**: Wizard expandido de 3→5 URL inputs en sección fotos reales (total hasta 9 fotos = 5 reales + 3 lifestyle + BM checkbox).

6. **DB migrations**: `field_defs_json TEXT` en `amz_product_type_templates` + nueva tabla `amz_launched_listings` para tracking post-publicación.

7. **Templates actualizados**: TELEVISION, PEST_CONTROL_DEVICE, ELECTRIC_LANTERN, VACUUM_CLEANER con `field_defs` completos (15 campos TV, 9 PEST_CONTROL, 7 ELECTRIC_LANTERN, 9 VACUUM_CLEANER).

### Nuevos endpoints
- `GET /api/amazon/lanzar/template-fields?product_type=X&seller_id=Y`
- `GET /api/amazon/lanzar/detect-product-type?category=X&sku=Y&title=Z`
- `GET /api/amazon/lanzar/launched-listings?seller_id=X`

---

## 2026-06-09 — FEAT: PEST_CONTROL_DEVICE fix + sistema auto-fix de errores Amazon

### Contexto
SNHG000004 (Skeeter Hawk SKE-ZAP-1008) fallaba con 5 errores al lanzar en Amazon MX.
Investigación via SP-API Product Type Definitions API reveló los atributos exactos.

### 5 errores y sus fixes

| Error Seller Central MX | Atributo SP-API | Valor |
|---|---|---|
| Se requiere 'Requiere montaje' | `is_assembly_required` | `false` |
| Se requiere 'Núm. certificación pesticida' | `regulatory_compliance_certification` | `{regulation_type: cofepris_registration_num, value: N/A}` |
| Se requiere '¿Es eléctrico?' | `power_source_type` | `"Alimentado por energía solar"` + `language_tag: es_MX` |
| Se requiere 'Número de Piezas' | `number_of_pieces` | `1` |
| Se requiere 'Certificado conformidad producto' | mismo campo que error 2 | misma solución |

**Nota clave:** Errores 2 y 5 son el MISMO atributo (`regulatory_compliance_certification`). Amazon los reporta con dos nombres de display distintos. `power_source_type` requiere `language_tag: "es_MX"` en Amazon MX — sin este tag el campo se rechaza.

### Cambios

**`app/services/amazon_client.py`**
- Nuevo `patch_listing_attributes(sku, product_type, attr_patches)` — PATCH JSON (RFC 6902) para actualizar atributos individuales sin re-crear todo el listing.

**`app/api/amazon_lanzar.py`**
- Bloque PEST_CONTROL_DEVICE/ELECTRIC_LANTERN en `create_listing`: aplica los 4 atributos automáticamente.
- `_MX_ERROR_ATTR_MAP`: 23 entradas — fragmento de mensaje Seller Central MX → atributo SP-API + valor.
- `POST /auto-fix-errors`: recibe issues de Amazon, mapea → PATCH listing → guarda defaults al template. Fallback a IA para errores desconocidos.

**`app/services/token_store.py`**
- PEST_CONTROL_DEVICE template actualizado: 3 nuevos required_attrs, defaults corregidos.
- `seed_product_type_templates`: ahora siempre actualiza templates con `validated=1` (antes solo si no existía).

**`app/templates/partials/amazon_lanzar_wizard.html`**
- Botón "🤖 Auto-corregir con IA" en step 4 — aparece cuando Amazon retorna errores estructurados.
- Guarda `_wiz._lastPayload` y `_wiz._lastIssues` al recibir error para el retry.
- Si fix completo: muestra atributos corregidos + botón "Publicar ahora".

### Sistema de aprendizaje
Cada fix exitoso guarda los nuevos atributos como defaults del template en DB.
Próximos productos del mismo tipo ya no necesitan el fix — se lanzan directo.

---

## 2026-06-09 — FEAT: Búsqueda de imágenes multi-fuente (DDG + Bing en paralelo)

### Problema
Búsqueda anterior usaba solo DuckDuckGo — fuente única, resultados mixtos.
Se habían agregado 6 botones manuales (Google, Bing, BestBuy, Walmart, Wayfair, HD) como workaround → UX compleja, trabajo doble.

### Solución

**`app/api/amazon_lanzar.py`** — `search_product_images` reescrito:
- `asyncio.gather` lanza DDG + Bing en paralelo (~3-4s total, antes 2-3s solo DDG)
- Pooling: misma URL en ambas fuentes → +3 score (confianza cruzada)
- CDNs de retailers en top del ranking: `thdstatic.com`, `bbystatic.com` > homedepot/bestbuy > genéricos
- Response incluye `sources[]` y `total_candidates` para transparencia

**`app/templates/partials/amazon_lanzar_wizard.html`**:
- 6 botones de búsqueda manual → 1 solo botón "🔍 Buscar imágenes"
- Badge de fuentes: "Fuentes: DuckDuckGo + Bing · 38 candidatos → 9 mejores"
- Sección HD ID colapsada en `<details>` como opción avanzada

### Lección
Proponer la solución inteligente desde el inicio — no construir pasos intermedios que después se deshacen.

---

## 2026-06-09 — FEAT: Wizard fotos — Wayfair/HD buttons, HD ID scraper, BM auto-populate

### Cambios (`app/templates/partials/amazon_lanzar_wizard.html`)

- **Botones de búsqueda nuevos**: Wayfair y Home Depot agregados al toolbar de búsqueda de imágenes.
  Abren `wayfair.com/keyword.php?keyword=` y `homedepot.com/s/` en pestaña nueva con query del título.
- **Home Depot ID scraper**: input numérico + botón "🏠 Extraer fotos HD" — llama `GET /api/amazon/lanzar/scrape-homedepot?product_id={id}`,
  renderiza resultado en el mismo grid compartido de `_amzWizRenderScrapeImages`.
- **Auto-populate BM image**: al entrar al tab "Fotos", si `_wiz.image_url` existe y el primer
  input de URL está vacío, se auto-llena con la imagen de BinManager. Evita empezar con campo vacío.
- Fix: texto del botón "Auto-buscar imágenes" se restaura correctamente después de buscar
  (antes quedaba "Buscar imágenes reales del fabricante" — texto antiguo).

### Contexto
Producto SHIL000082 (Hampton Bay HB3678-34) tenía imagen en BM pero el wizard no la pre-cargaba.
El scraper HD usa endpoint `/scrape-homedepot` existente (Jina Reader → thdstatic CDN URLs).

---

## 2026-06-09 — FEAT: Amazon Wizard — PEST_CONTROL_DEVICE + UPC generation

### Cambios

**`app/services/token_store.py`**
- Nueva tabla `sku_upc_map (sku, upc, source, created_at)` — registro interno de UPCs generados.
- `get_sku_upc(sku)` y `save_sku_upc(sku, upc, source)` para CRUD.
- Template PEST_CONTROL_DEVICE (A1AM78C64UM0Y8) sembrado: material_type="Plástico",
  power_source_type="Energía solar", browse node 23536384011, item_type_keyword="electronic-pest-control".

**`app/api/amazon_lanzar.py`**
- `_generate_internal_upc(sku)`: genera UPC-A determinístico (prefix 888 + SHA-256 % 10^8 + check digit Luhn).
  Mismo SKU → mismo UPC siempre. Sin colisiones entre SKUs distintos.
- `POST /generate-upc`: verifica DB primero, genera si falta, guarda. Returns `{upc, source, is_new}`.
- Material defaulting para PEST_CONTROL_DEVICE/ELECTRIC_LANTERN: `material_type="Plástico"`,
  `power_source_type="Energía solar"` cuando no se proveen.
- AI prompt actualizado: valores de material en español para Amazon MX ("Plástico"/"Metal"/"Aluminio").
- `search_product_images`: acepta `title` param; resultados ordenados por calidad de dominio (trusted retailers primero).

**`app/templates/partials/amazon_lanzar_wizard.html`**
- Paso 4 checklist: campo `material` incluido en validación (`_fieldVals`/`_schemaLabels`).
- UPC field: botón 🏷️ `_amzWizGenerateUPC()` — llama `/generate-upc`, llena campo, muestra confirmación.
- UPC field: enlace GTIN exemption visible cuando UPC está vacío.
- `_amzWizSearchImages()`: pasa `title: _wiz.title` al endpoint (búsqueda con título completo).
- `_amzWizOpenSearch()`: usa `_wiz.title`; agrega opciones wayfair + homedepot.

### Fix: "Se requiere 'Material', pero falta"
Causa: Amazon MX exige `material_type` para PEST_CONTROL_DEVICE. BM no lo tiene.
Fix: backend defaultea "Plástico" cuando el campo falta + AI prompt fuerza valores en español.

---

## 2026-06-08 — FEAT: Migración completa de Anthropic API a OpenRouter

### Decisión
Eliminar toda dependencia de `platform.claude.com` (Anthropic API) del dashboard.
Cuenta individual con balance negativo y el Team plan de MI Technologies es solo para chat, no API.
100% de las llamadas de IA ahora corren por OpenRouter — sin gasto en Anthropic.

### Cambios

**`app/services/openrouter_client.py`**
- Nueva función `generate_with_images(prompt, image_urls, system, max_tokens)` — usa Gemini 2.5 Flash
  (`google/gemini-2.5-flash-preview-05-20`) para tareas con imágenes (vision).
  Fallback a `generate()` con premium model si la llamada vision falla.
- Nueva constante `_VISION_MODEL` para el modelo de visión.

**`app/api/amazon_products.py`**
- Bloque SSE `generate()` interno reemplazado: httpx Anthropic SSE → `_or_client.generate_stream()`
- Import `ANTHROPIC_API_KEY` y check de api_key eliminados.

**`app/api/amazon_lanzar.py`**
- 6 bloques de llamadas httpx Anthropic reemplazados con `_or_client.generate()` + `get_premium_model()`.

**`app/api/lanzar.py`**
- Import `claude_client` → `openrouter_client as _or_client`
- 6 llamadas `claude_client.*` reemplazadas: `generate`, `generate_with_images`, `generate_stream`
- `generate_with_images` ahora usa `_or_client.generate_with_images()` (Gemini 2.5 Flash)

**`app/api/sku_inventory.py`**
- Import `claude_client` → `_or_client` (lazy import en endpoint)
- 6 llamadas `claude_client.*` reemplazadas: `generate`, `generate_stream`, `generate_stream_with_images`
- `generate_stream_with_images`: reemplazado por `generate_with_images()` (completo, yield único)

**`app/api/health_ai.py`**
- Import `claude_client` eliminado — solo `openrouter_client`
- `_ai_available()`: ya no consulta claude_client
- `_sse_stream()`: eliminado fallback a claude_client; openrouter ya tiene cascade + circuit breaker interno
- `debug-key`: endpoint refactorizado para testear OpenRouter key (antes testeaba Anthropic)
- Claim analysis: eliminado fallback a claude_client

### Modelo premium
`deepseek/deepseek-chat` via OpenRouter para todas las tareas de alto valor (Wizard, listings, claims).
Costo ~15x menor que Sonnet 4.6 ($0.20/$0.80 vs $3/$15 por 1M tokens).

---

## 2026-06-07 — FEAT: Circuit breaker + descubrimiento dinámico de modelos OpenRouter

### Problema
Los modelos hardcoded en `_FREE_MODELS` se vuelven obsoletos sin aviso (OpenRouter elimina modelos :free frecuentemente).
Cada request intentaba los 3 modelos muertos antes de llegar a Haiku → latencia innecesaria + UX mala.

### Solución (`app/services/openrouter_client.py`)
- **Circuit breaker**: `_dead_models` dict — cuando un modelo devuelve 404, se marca como muerto por 1h.
  El cascade los salta automáticamente en requests subsecuentes. TTL de 1h para reintento automático.
- **Descubrimiento dinámico**: `_get_free_models()` consulta `GET /api/v1/models` de OpenRouter,
  filtra modelos `:free` con context ≥ 8K, cachea la lista por 1h. Fallback a `_FREE_MODELS` si falla.
- `generate()` y `generate_stream()` usan `_get_free_models()` + skip de dead models en cada llamada.

### Efecto
Si un modelo nuevo se vuelve 404: se marca muerto en el primer intento, los siguientes requests lo saltan.
OpenRouter publica modelos nuevos: se descubren automáticamente en la siguiente hora.
No se necesita intervención manual para actualizar `_FREE_MODELS`.

---

## 2026-06-07 — FIX: "Sugerir con IA" roto — modelos OpenRouter obsoletos + Haiku 400

### Síntoma
Feature "Sugerir con IA" en preguntas ML fallaba con:
`[ERROR] Todos los modelos fallaron. Último error: Error 404. Haiku: Client error '400 Bad Request'`

### Root cause
1. Los 3 modelos en `_FREE_MODELS` ya no existían en OpenRouter (`mistral-7b-instruct:free`,
   `gemma-2-9b-it:free`, `llama-3.3-70b-instruct:free`) → todos devuelven 404.
2. El fallback Anthropic Haiku usaba `"claude-haiku-4-5-20251001"` (ID con sufijo de fecha)
   → API devuelve 400. El ID correcto es `"claude-haiku-4-5"` (alias sin fecha).

### Solución (`app/services/openrouter_client.py`)
- `_FREE_MODELS` actualizado a modelos vigentes: `google/gemma-3-27b-it:free`,
  `meta-llama/llama-3.3-70b-instruct:free`, `mistralai/mistral-small-3.1-24b-instruct:free`
- Haiku model ID corregido: `"claude-haiku-4-5-20251001"` → `"claude-haiku-4-5"`
- Error logging mejorado en fallback Haiku: ahora loguea el body completo (500 chars) antes de raise
- Docstring actualizado con nota sobre volatilidad de modelos :free y URL para verificar

### Prevención
El docstring del módulo ahora incluye instrucción explícita:
`Si todos devuelven 404, actualizar _FREE_MODELS en https://openrouter.ai/models?q=:free`

---

## 2026-05-28 — FIX: Inventario skeleton infinito — listings stale-while-revalidate

### Problema
`_get_listings_cached` bloqueaba el request handler cuando la DB tenía <500 filas
(ocurre siempre en el primer boot tras cada deploy — Railway borra DB en redeploy).
La función llamaba `get_all_listings()` de forma sincrónica: 50 páginas × 0.2s + red = 15-30s.
Railway tiene un timeout de request de ~30s → el endpoint nunca respondía → skeleton infinito.

Mismo problema ya resuelto para FBA en commit 14e4656 (stale-while-revalidate).
El `loadAmzProdTab` tampoco llamaba `_invBgPoll()`, así que el auto-poll de BG tasks
nunca arrancaba al hacer click en el tab de Inventario.

### Solución
- `_listings_loading: set` rastrea fetches BG activos por seller_id
- `_refresh_listings_bg()`: BG fetch que reintenta DB-first, fallback API
- `_build_listings_from_rows()`: helper extraído para reusar lógica DB→listing
- `_get_listings_cached()`: stale-while-revalidate — cold start devuelve `[]`
  inmediatamente + lanza BG; stale devuelve datos viejos + lanza BG
- `bg-status` incluye `listings_active` en el check `ready`
- Contexto inventario incluye `listings_loading`
- Template: `data-bg-loading=true` cuando `listings_loading`; banner
  "Sincronizando catálogo…" en estado vacío con auto-poll de 5s
- `loadAmzProdTab`: ahora llama `_invBgPoll()` para el tab inventario (faltaba)
- `_trigger_bm_prefetch`: guard `if not listings: return` para no quemar
  `_bm_all_last_refresh` cuando catálogo está vacío

### Commit: 5274948

---

## 2026-05-28 — FIX: Amazon rate limits — Últimas Órdenes y Top 10 Productos

### Problema 1: Últimas Órdenes siempre mostraba "Rate limit Amazon SP-API"
`get_amazon_recent_orders` paginaba 29 días de historial (~17 páginas = 17 API calls).
El burst de `getOrders` SP-API es solo 20 requests → se agotaba en cold start.

### Solución 1
- `get_amazon_recent_orders` ahora usa ventana de **3 días** + **max_pages=1** (1 página = 100 órdenes, suficiente para mostrar las 5 más recientes)
- Solo 2 API calls (active + pending) independientemente del tamaño del catálogo
- Caché propio 10 min (`_amazon_recent_orders_cache`) separado del caché de 29 días del Dashboard
- `get_orders()` en el cliente acepta `max_pages` y añade `sleep(0.5s)` entre páginas

### Problema 2: Top 10 Productos tardaba 5+ min (o nunca cargaba)
`_refresh_sku_sales_bg` lanzaba 5 `getOrderItems` concurrentes (≈5 rps vs límite 0.5 rps).
Saturaba el burst en los primeros batches → 429 → datos incompletos.

### Solución 2
- Items fetched secuencialmente (1 a la vez) con 2s de delay → 0.5 req/s = respeta el rate limit
- Cap de 150 órdenes para el BG task inicial (~5 min) → suficiente para Top 10 representativo

### Archivos modificados
- `app/services/amazon_client.py`: `get_orders` + `max_pages` + sleep entre páginas
- `app/api/metrics.py`: `get_amazon_recent_orders` reescrito, nuevo caché dedicado
- `app/api/amazon_products.py`: `_refresh_sku_sales_bg` — items secuenciales, cap 150

---

## 2026-05-28 — FIX: Amazon Dashboard — stats cards y alertas solo en tab Dashboard

### Problema
`amz-stats-row` (Activos/Inactivos/Suprimidos/Sin Stock) y `amz-alerts-panel` estaban fuera de todos los tabs — se mostraban en Ventas, Salud y todos los demás tabs, duplicando información del Dashboard.

### Solución
Movidos ambos divs al interior de `amz-tab-dashboard`. El `switchAmzTab()` ya aplica `classList.toggle('hidden')` sobre el div padre, por lo que los cards desaparecen automáticamente al cambiar de tab.

### Archivos modificados
- `app/templates/amazon_dashboard.html`: `amz-stats-row` y `amz-alerts-panel` movidos dentro de `amz-tab-dashboard`

---

## 2026-05-28 — FEAT: Sin Publicar — botón "Nuevo Producto" (sin SKU BM)

### Problema
El wizard de lanzamiento solo podía abrirse desde un SKU de BM (gap scan). No había forma de lanzar un producto que no estuviera en BM (producto nuevo, test, compra directa de distribuidor).

### Solución
Botón **"➕ Nuevo Producto"** en la barra de búsqueda de la tab Sin Publicar. Abre un mini-modal con:
- Marca y Modelo (requeridos)
- Categoría, UPC/EAN, SKU (opcionales)
- Precio MXN (requerido)

Al confirmar, llama `openAmzLanzar()` y luego `_amzWizSkipAsin()` para saltar directamente al Paso 2 en Flujo 2 (crear nuevo — sin buscar ASIN). El wizard genera contenido AI, fotos y checklist de calidad igual que siempre.

### Archivos modificados
- `app/templates/partials/amazon_sin_lanzar.html`: botón + mini-modal + JS `_amzOpenNuevoProducto`, `_amzNuevoProductoLanzar`

---

## 2026-05-28 — FEAT: Amazon Lanzar Wizard v2 — SEO/CRO completo, fotos BM, Higgsfield, checklist

### Problemas resueltos
- Wizard anterior pedía datos mínimos (solo precio/ASIN/qty) — Amazon requiere ~15 atributos para ranking
- IA usaba `claude-haiku` → contenido genérico y pobre. Reemplazado por `claude-sonnet-4-6`
- Sin fotos: Amazon necesita imágenes de alta calidad para conversión
- Sin `generic_keyword` (backend keywords) — invisibles para SEO interno
- Sin `product_type` — Amazon no sabía en qué árbol categórico colocar el producto
- Sin revisión de calidad antes de publicar

### Nuevas funcionalidades

**Wizard v2 — 4 pasos**:
- **Paso 1**: Búsqueda ASIN o lanzamiento desde cero (igual que antes)
- **Paso 2**: Precio, condición, fulfillment (igual que antes)
- **Paso 3** *(Flujo 2 — nuevo)*: Contenido + Fotos en sub-tabs
  - **Tab Contenido**: Título (200 chars, counter con colores), 5 bullets (200 chars c/u), descripción (2000), keywords backend (249 chars), product type
  - **Tab Fotos**: checkbox para imagen BM, 4 inputs de URL extra, botón Higgsfield AI, indicador de fotos seleccionadas
- **Paso 4**: Checklist visual de calidad (✅/⚠️/❌) para 8 criterios antes de publicar

**AI content (claude-sonnet-4-6)**:
- Prompt con reglas Amazon SEO: título sin promo-text, bullets con beneficio primero, description HTML-ready, keywords sin repetir, product_type técnico
- Recibe: `sku`, `title_bm`, `brand`, `category`, `model`, `upc`, `price_mxn`
- Regresa: `title`, `bullets[5]`, `description`, `keywords_backend` (≤249), `product_type`

**Backend (`create_listing`)**:
- Nuevo campo `generic_keyword` → `generic_keyword[0].value`
- Nuevo campo `product_type` → usado en `put_listings_item`
- Nuevo campo `photo_urls[]` → `main_product_image_locator` + `other_product_image_locator_1..8`

### Archivos modificados
- `app/api/amazon_lanzar.py`: `generate_content` rewrite (model, prompt, response), `create_listing` (keywords, photos, product_type)
- `app/templates/partials/amazon_lanzar_wizard.html`: rewrite completo (~600 líneas), modal más ancho, contexto BM en header, 4 pasos, checklist
- `app/templates/partials/amazon_sin_lanzar.html`: `openAmzLanzar()` pasa `g.model` como 10° parámetro

---

## 2026-05-27 — FEAT: Gap scan automático en background (sin click "Escanear")

El gap scan ahora corre automáticamente, igual que el sync de listings. No es necesario hacer click manual.

### Horario
- **Arranque**: full sync → gap scan (60s después de iniciar)
- **Cada 6h**: full listing sync → gap scan (listings y gaps siempre frescos)
- **Cada 3h** (entre full syncs): gap scan solo (captura cambios de stock BM)
- **Manual**: el botón "Escanear" sigue disponible para forzar si se necesita

### Archivos modificados
- `app/api/amazon_lanzar.py`: nueva `run_gap_scan_all_accounts()` — itera todas las cuentas, respeta locks
- `app/services/amazon_listing_sync.py`: `_run_gap_scan_background()`, `_GAP_SCAN_INTERVAL`, loop actualizado, `last_gap_scan_ts` en status

---

## 2026-05-27 — FIX: gap scan — 3 bugs en persistencia de falsos positivos (SNAC000046)

### Bugs
1. **Cache hit sin limpieza**: `_check_gap()` regresaba `None` por cache hit pero no agregaba a `amazon_base_skus` → fila vieja en `amz_sku_gaps` no se borraba → SKU confirmado como lanzado seguía en UI
2. **Benefit-of-doubt sin limpieza**: excepción 429/403 hacía `raise` → gap filtrado de lista nueva (correcto) pero fila vieja nunca se borraba de `amz_sku_gaps`
3. **DB-first saltaba `_check_gap()`**: mi cambio anterior usaba `if gaps and not db_first:` → items confirmados en cache nunca limpiaban la gaps table

### Fix
- Antes del loop de gaps: augmentar `amazon_base_skus` con `amz_catalog_cache WHERE found=1` → garantiza que cleanup borre filas viejas
- Cache hit ahora agrega a `amazon_base_skus` explícitamente
- Benefit-of-doubt: en vez de `raise`, agrega a `amazon_base_skus` + return `None` → old row se borra via cleanup
- `_check_gap()` corre siempre (removido el guard `not db_first`)

---

## 2026-05-27 — FEAT: Amazon listing sync DB-first + Reports API full sync

### Problema
`amazon_listing_sync.py` usaba `get_all_listings()` (capped at 1000 SKUs) para el full sync. ExclusiveBulbs con 156K listings nunca se sincronizaba completo → DB vacía → gap scan veía todos los SKUs BM como gaps (falsos positivos masivos).

### Arquitectura nueva

**Full sync (`_sync_account_full`)**:
- Intenta `get_merchant_listings_report()` primero (Reports API, sin límite, incluye title/price/qty)
- Fallback a `get_all_listings()` si Reports falla (cuentas que no tienen Reports habilitado)
- Nuevo `_report_entry_to_row()` para convertir formato TSV a row DB
- Nueva función `upsert_amazon_listings_report()` en token_store — preserva price/qty existentes cuando el nuevo valor es 0 (para FBA items cuya qty viene de FBA Inventory API aparte)

**Qty sync (`_sync_qty_only_account`)**:
- Si cuenta tiene >1000 SKUs en DB → usa `get_fba_inventory_all()` (sin límite de páginas)
- Si cuenta tiene ≤1000 SKUs → sigue usando `get_all_listings(fulfillmentAvailability)` (suficiente)

**Gap scan (`_run_amz_gap_scan`)**:
- **DB-first**: si DB tiene ≥500 listings para este seller → construye `amazon_base_skus` desde DB local, sin ninguna llamada API de descubrimiento
- **Sin `_check_gap()`**: cuando DB-first, no hay verificación individual por SKU → scan instantáneo
- **API-fallback**: si DB tiene <500 listings (primer run) → mantiene flujo anterior (Listings API → Reports API → FBA inventory) + verificación individual con cache

### Resultado esperado
- ExclusiveBulbs: primer full sync (Reports API, ~30-60s wait) pobla los 156K SKUs
- Siguientes gap scans: consulta DB local, <1s para construir `amazon_base_skus`
- Sin más falsos positivos por SKUs perdidos en truncado de paginación

### Archivos modificados
- `app/services/amazon_client.py`: `get_merchant_listings_report()` — agrega title, price, quantity al TSV parse
- `app/services/token_store.py`: nueva `upsert_amazon_listings_report()` con ON CONFLICT preserve
- `app/services/amazon_listing_sync.py`: `_report_entry_to_row()`, `_sync_account_full()` Reports-first, `_sync_qty_only_account()` FBA-first para catálogos grandes
- `app/api/amazon_lanzar.py`: `_run_amz_gap_scan()` DB-first path, `_check_gap()` solo cuando DB sparse

---

## 2026-05-26 — FIX: Amazon Pending orders — mostrar precio venta con fees pendientes

### Problema
Órdenes Amazon con status "Pending" mostraban $0.00 — Amazon no libera `OrderTotal` hasta confirmar el pago. El dashboard no mostraba ningún valor.

### Fix
Enriquecimiento multi-capa en `get_amazon_recent_orders`:
1. `ItemPrice.Amount` (línea total, ya incluye qty) — disponible cuando pago está en verificación
2. Fallback DB: lookup por SKU exacto → SKU base → ASIN en `amazon_listings`
3. Fees estimados: Referral 15% mostrado como referencia

Sistema de colores: verde = OrderTotal confirmado, ámbar = precio pendiente/referencia, gris = sin precio.

### Archivos modificados
- `app/api/metrics.py`: `get_amazon_recent_orders()` — lógica de enriquecimiento
- `app/templates/partials/amazon_recent_orders.html` — rewrite completo con color system

---

## 2026-05-26 — FIX: get_listing_item benefit-of-doubt + diagnóstico gap falso (SNAC000046)

### Problema raíz
`get_listing_item` devolvía `None` para CUALQUIER error (403, 429, red), no solo 404. Esto causaba que SKUs como SNAC000046 se marcaran como gap aunque existieran en Amazon — cualquier error transitorio de API se confundía con "no existe".

### Fixes
1. **`get_listing_item` re-raise no-404** — ahora solo devuelve `None` para 404/NOT_FOUND. Cualquier otro error se relanza, y `_check_gap` lo captura en `asyncio.gather(return_exceptions=True)` → el SKU se descarta de gaps (beneficio de la duda).
2. **Logging detallado en scan** — log de `marketplace_id` + `nickname` al inicio del scan. Log cuando un SKU se confirma como gap con las variantes probadas y el marketplace usado.
3. **Endpoint diagnóstico** — `GET /api/amazon/diag/check-sku?sku=SKU&seller_id=ID` — prueba lookup en tiempo real con resultado completo: marketplace_id, variantes, errores exactos.

### Archivos modificados
- `app/services/amazon_client.py`: `get_listing_item` — re-raise en errores no-404 + log warning
- `app/api/amazon_lanzar.py`: `_check_gap` — manejo explícito de non-404, logging detallado
- `app/api/amazon_products.py`: nuevo endpoint `/api/amazon/diag/check-sku`

---

## 2026-05-26 — FIX: Verificación individual por SKU — solución definitiva ExclusiveBulbs

### Problema
`searchListingsItems` y FBA inventory devuelven 0 para ExclusiveBulbs (156K listings).
Resultado: todos los SKUs BM aparecen como "Sin Lanzar" aunque sí estén en Amazon.

### Arquitectura 3 capas
1. **Listings API** — si devuelve SKUs, usarlos. Ahora incluye activos E inactivos (out-of-stock ≠ gap).
2. **Reports API** (`GET_MERCHANT_LISTINGS_ALL_DATA`) — descarga catálogo completo (sin límite de paginación)
3. **Individual lookup** (fallback definitivo) — si amazon_base_skus sigue vacío:
   - Verifica cada BM SKU individualmente via `GET /listings/{sellerId}/{sku}`
   - Prueba variantes: base, -FBA, `_FBA_0`, -FBA-0, -FBM
   - Cache 24h en `amz_catalog_cache` (Semaphore 5, concurrente)
   - Primera vez: ~1951 API calls; siguiente scan: instantáneo desde cache

### Archivos modificados
- `app/api/amazon_lanzar.py`: `_verify_bm_skus_individually()` + integración en scan
- `app/services/amazon_client.py`: `get_listing_item(sku)` — lookup individual
- `app/services/token_store.py`: tabla `amz_catalog_cache` (seller_id, sku_upper, found, checked_at)

---

## 2026-05-26 — FIX: SKU matching — FBA suffix regex + Reports API para catálogos grandes

### Problema
SKUs como `SNAC000029-FBA` y `SNAC000029_FBA_0` no se reconocían como lanzados — `_amz_base()` no manejaba los sufijos FBA/FBM. Además, `get_all_listings()` tenía cap de 50 páginas × 20 ítems = 1000 SKUs, truncando silenciosamente catálogos grandes (ExclusiveBulbs: 156K listings).

### Fixes
1. **`_amz_base()` regex** — `_AMZ_FBA_RE` extrae base de `SKU-FBA`, `SKU_FBA_0`, `SKU-FBM`, etc.
2. **`get_all_listings()` logging** — detecta truncado, avisa en logs con `else` en el `for`
3. **`get_merchant_listings_report()`** — nuevo método que usa `GET_MERCHANT_LISTINGS_ALL_DATA` (Reports API) para descargar TODO el catálogo en un solo archivo TSV, sin límite de paginación
4. **`_run_amz_gap_scan()`** — si listings ≥ 990 (truncado) → descarta y usa Reports API; si Reports falla → FBA fallback

### Archivos modificados
- `app/api/amazon_lanzar.py`: `_amz_base()` regex, lógica de detección de truncado en scan
- `app/services/amazon_client.py`: `get_all_listings()` mejor logging, nuevo `get_merchant_listings_report()`

---

## 2026-05-25 — FEAT: Amazon Sin Publicar — background scan (BM vs Amazon gap detection)

### Resumen
Tab "🚀 Sin Publicar" migrado de carga síncrona a patrón background scan (igual que ML Lanzador):
- El escaneo corre en segundo plano (asyncio.Lock por seller_id), sin bloquear la UI
- Los gaps se persisten en `amz_sku_gaps` (DB); la tabla siempre sirve instantáneamente
- La UI muestra estado del scan: Nunca / En progreso / Error / hace X min
- Polling automático cada 3s mientras corre, recarga la tabla al terminar

### Flujo
1. Usuario abre tab → lee gaps de DB (instantáneo)
2. Pulsa "🔍 Escanear" → POST `/api/amazon/lanzar/scan`
3. Background: `_run_amz_gap_scan` — `get_bulk_stock` + `get_all_listings` en paralelo → diff → upsert `amz_sku_gaps`
4. JS polling cada 3s → al terminar, recarga el tab con datos frescos

### Estados de UI
- **Nunca**: CTA prominente "🔍 Escanear ahora"
- **En progreso**: banner naranja animado + spinner en tarjeta KPI + polling activo
- **Error**: banner rojo con mensaje + link "Reintentar"
- **Done**: "hace X min" en tarjeta KPI + tabla con gaps

### Archivos modificados
- `app/services/token_store.py`: tabla `amz_gap_scan_status` + columnas `category/model/margin_pct/last_scan` en `amz_sku_gaps`
- `app/api/amazon_lanzar.py`: `_run_amz_gap_scan()`, `POST /scan`, `GET /scan/status`
- `app/api/amazon_products.py`: reescritura `amazon_sin_lanzar` — lee DB, pasa `scan_status`/`scan_error`/`bm_total`/`amazon_active`
- `app/templates/partials/amazon_sin_lanzar.html`: KPI card scan + banners + empty states contextuales + JS trigger+polling

---

## 2026-05-22 — FEAT: Amazon — Repricing automático + Devoluciones por SKU + renombrar tabs

### Resumen
Tres mejoras en el Centro de Productos Amazon:
1. **Renombrado de tabs**: "Sin Publicar" → "⚠️ Inactivos", "Sin Lanzar" → "🚀 Sin Publicar" (claridad semántica)
2. **Tab Repricing (💲)**: reglas globales Match BB / Beat BB / Precio fijo con piso y techo, previsualización de cambios y aplicación en un clic con confirmación.
3. **Tab Devoluciones (🔄)**: historial de reembolsos de los últimos 7–90 días agrupado por SKU, con monto total y nivel de impacto.

### Archivos modificados
- `app/templates/amazon_products.html`: tabs renombrados + 2 nuevos tabs (repricing, devoluciones)
- `app/templates/partials/amazon_products_resumen.html`: texto "Sin Publicar" → "Inactivos"
- `app/templates/partials/amazon_ignorados.html`: referencia "Sin Lanzar" → "Sin Publicar"
- `app/services/token_store.py`: tabla `amz_repricing_rules` (seller_id, sku, rule_type, beat_pct, min_price, max_price, enabled)
- `app/services/amazon_client.py`: método `get_refunds_detail(days)` — devoluciones por SKU
- `app/api/amazon_products.py`: endpoints `GET /products/repricing`, `POST /products/repricing/rule`, `POST /products/repricing/apply`, `GET /products/devoluciones`
- `app/templates/partials/amazon_products_repricing.html` (nuevo): tabla con BB status + formulario de regla global + botón apply
- `app/templates/partials/amazon_products_devoluciones.html` (nuevo): KPIs + tabla por SKU con filtro de período

---

## 2026-05-22 — FEAT: Higgsfield AI — Generación de contenido visual en todas las plataformas

### Resumen
Integración completa de Higgsfield AI para generación de fotos lifestyle y videos de producto.
Botón ✨ IA disponible en todos los lugares donde aparecen productos:
ML Top Ventas, ML Todos los productos, Amazon Listings, Gaps/Sin publicar, y el Wizard de lanzamiento.

### Fases implementadas

**Phase 1 — Botón ✨ IA en tablas de producto**
- `app/services/higgsfield_client.py` (nuevo): cliente async — `check_credits()`, `generate_image()`,
  `generate_video()`, `get_status()`, `upload_from_url()`, prompt builders.
  URL base correcta: `https://platform.higgsfield.ai`. Auth: `Authorization: Key {id}:{secret}`.
- `app/api/higgsfield.py` (nuevo): router `/api/higgsfield` — `GET /check`, `POST /generate`,
  `GET /status/{id}`. Mode "image" usa `soul/standard`; mode "video" usa `dop/lite` (5s).
- `app/templates/partials/higgsfield_modal.html` (nuevo): modal global con selector de modo
  (Foto ~8cr / Video ~6cr), prompt customizable, polling cada 3s, descarga, "Otra versión".
- `app/templates/base.html`: incluye el modal antes de `</body>`.
- Botones ✨ agregados en: `products_top_sellers.html`, `products_full.html`,
  `amazon_products_catalog.html`, `app/static/js/productos.js` (renderActions).
- `app/config.py`: `HIGGSFIELD_KEY_ID` y `HIGGSFIELD_SECRET` desde env.
- Railway: vars `HIGGSFIELD_KEY_ID` + `HIGGSFIELD_SECRET` + `SECRET_KEY` seteadas via GraphQL API.

**Phase 2 — Botón ✨ IA en Gaps/Sin publicar**
- `lanzar_gaps.html` línea ~1112: botón `✨ IA` en la celda de acción de cada gap,
  abre `openHiggsfieldModal()` con `product_title`, `image_url`, `sku` del gap.

**Phase 3 — Higgsfield en wizard de lanzamiento (paso Fotos)**
- `lanzar_gaps.html` botón `🌟 Lifestyle` junto a "Generar 8 imágenes con IA".
- `window._wizGenHiggsfield()`: llama `/api/higgsfield/generate?mode=image`, hace polling,
  agrega la imagen generada a `_wiz.ai_images` y la inserta en `selected_images` como portada.
  Muestra spinner inline y error si falla.

### Modelos Higgsfield
- `higgsfield-ai/soul/standard` → foto lifestyle (imagen única, ~8 créditos)
- `higgsfield-ai/dop/lite` → video 5s desde imagen (requiere upload previo, ~6 créditos)

---

## 2026-05-21 — FIX: Amazon órdenes recientes — 429 QuotaExceeded

### Problema
La sección "Últimas Órdenes Amazon" en el tab Ventas mostraba HTTP 429 QuotaExceeded.
Causa raíz: `asyncio.gather` disparaba 5 llamadas simultáneas a `get_order_items` 
en SP-API. Las apps Draft tienen rate limits reducidos — el endpoint orderItems permite
0.5 rps, y 5 llamadas en paralelo lo superan instantáneamente.

### Solución
- `app/api/metrics.py`: reemplazado `asyncio.gather` por loop secuencial con
  `await asyncio.sleep(0.4)` entre cada llamada (~2.5 rps total, bajo el límite).
  Si llega 429, se detecta en el except y se hace `break` devolviendo las órdenes
  ya enriquecidas en lugar de fallar todo el endpoint.
- `app/static/js/amazon_dashboard.js`: `loadAmzRecentOrders` ahora detecta respuesta
  429 del servidor y muestra UI de countdown con backoff exponencial (15s → 30s → 60s)
  y botón "Reintentar ahora". Auto-reintenta hasta convergencia.

### Commits
- `86f5c2f` fix: Amazon órdenes recientes — sequential orderItems para evitar 429
- `29a2617` feat: Amazon órdenes — retry UI con countdown en rate limit 429

---

## 2026-05-21 — FEAT: Detalles financieros en órdenes Amazon

### Resumen
Las órdenes Amazon ahora muestran breakdown completo de precio, fees y ganancia —
equivalente a lo que ML ya mostraba. El expand de cada orden incluye tres columnas:
Productos (título, SKU, ASIN, qty, precio unitario), Finanzas (cobros al comprador,
fees Amazon, Neto Amazon, rentabilidad vs costo BM), e Info de Orden.

### Detalles técnicos
- `amazon_client.py`: nuevo método `get_order_financial_events(order_id)` — llama
  `GET /finances/v0/orders/{id}/financialEvents` protegido con `_ORDERS_SEMAPHORE`.
- `amazon_orders.py`: 
  - `_parse_fees_from_events()`: parsea ShipmentEventList → extracts Commission
    (referral fee), FBAPerUnitFulfillmentFee, otros. Devuelve None si no hay datos.
  - `_estimate_fees()`: fallback 15% referral cuando aún no hay liquidación (Pending).
  - `_build_finanzas()`: construye contexto P&L completo — revenue, fees, neto,
    costo BM (via `_sku_cost_map` de app.main), ganancia, margen %.
  - Badge "est." amarillo vs "real" verde según fuente de datos.
- `partials/amazon_order_items.html`: columna 2 completamente reescrita con las tres
  secciones de finanzas. ⚠ advertencia automática si fees son estimados.

---

## 2026-05-20 — FIX: Comparativa de cuentas removida del tab Ventas Amazon

### Problema
El widget "Comparativa de cuentas" aparecía en el tab Ventas de cada cuenta Amazon
individual — comportamiento inconsistente con ML, donde este tipo de vista está
reservada para el dashboard general, no por cuenta.

### Solución
- `amazon_dashboard.html`: removido bloque `{% if amazon_accounts|length > 1 %}` 
  que contenía el comparativa widget del tab `amz-tab-ventas`.
- `amazon_dashboard.js`: removida llamada `loadAmzCompare()` del handler del tab ventas.

---

## 2026-05-20 — FEAT: Navbar unificado ML + Amazon

### Problema
Al cambiar de cuenta ML a Amazon (o viceversa), el nav entero cambiaba de estilo:
ML = amarillo, Amazon = oscuro #232F3E. El usuario veía dos interfaces completamente
distintas y la experiencia era confusa.

### Solución
Eliminado el navbar oscuro de Amazon. Ahora existe **un único navbar amarillo** para
ambas plataformas. Los tabs cambian condicionalmente según `active_platform` ("amazon"
o ML), pero el fondo, la posición, el selector de cuentas y el botón de logout son
siempre iguales. El selector de cuentas ya era shared — ahora todo el nav lo es.

- `base.html`: eliminados 267 líneas del Amazon dark nav, reemplazado por tabs
  condicionales `{% if active_platform == "amazon" %}` dentro del mismo nav amarillo
- Amazon tabs en mobile: mismo estilo `bg-yellow-500` en activo en lugar de `bg-[#37475A]`
- El logo muestra `AMZ` badge naranja en modo Amazon, `MeLi` texto en modo ML
- FX widget y campana de sugerencias solo se muestran en modo ML

---

## 2026-05-20 — FIX: ExclusiveBulbs (AMAZON3) marketplace ID typo

### Problema
ExclusiveBulbs mostraba $0 ventas, 0 órdenes. El marketplace ID estaba mal:
`ATVPDKIKX0ER` en lugar de `ATVPDKIKX0DER` (Amazon.com USA). Amazon retornaba
200 OK con array vacío, haciendo el bug muy difícil de detectar.

### Solución
- `app/config.py` línea 85: `AMAZON3_MARKETPLACE_ID` default corregido a `ATVPDKIKX0DER`
- `app/services/amazon_client.py` línea 1540: mismo fix en el hardcode de fallback
- Railway rechazó actualización de env var (deploy paused), fix en código default
- La DB se actualiza automáticamente en próximo restart vía UPSERT incondicional

---

## 2026-05-20 — FIX: Sales API 403 → fallback a Orders API para ExclusiveBulbs

### Problema
`/sales/v1/orderMetrics` retorna 403 para la app Draft de Amazon porque no tiene
permiso de Sales API. ExclusiveBulbs no puede usar Sales API.

### Solución
`app/api/metrics.py`: función `_orders_api_fallback_metrics(client, date_from, date_to)`
que construye métricas diarias equivalentes desde Orders API (`/orders/v0/orders`)
cuando Sales API devuelve 403. La función retorna la misma estructura que Sales API:
`[{interval, orderCount, unitCount, totalSales}]`. El fallback es transparente para
el resto del código.

---

## 2026-05-20 — FIX: Inventario BM en sección Productos mostraba 0

### Problema
`SNTV004097` y otros SKUs mostraban BM=0 en la tabla de productos aunque el caché
tenía stock correcto. `_bm_stock()` hacía llamadas HTTP directas a BM en tiempo real,
violando la regla cache-first.

### Solución
Auditoría completa de llamadas BM en vivo + reemplazo con lecturas del `_bm_stock_cache`:
- `app/api/productos.py`: `_bm_stock()` → `_bm_stock_from_cache()`; columna `bm_total` → `bm_avail`
- `app/static/js/productos.js`: `item.bm_total` → `item.bm_avail` en tabla y detalle
- `app/api/items.py`: `_bm_warehouse_qty()` reescrito para leer cache; fix bug `fetch_one(sku, client)`
- `app/main.py`: `_enrich_with_bm_stock()` reescrito para leer `_bm_stock_cache` directamente
- `app/api/lanzar.py`: `_bm_fetch_warehouse_stock()` reescrito para leer cache
- 4 casos genuinamente necesarios de BM en vivo identificados (condición breakdown, catalog, costos)
  — todos user-initiated, single-SKU, pasan por Semaphore(1)

---

## 2026-05-19 — FIX: Nicknames y cuentas ML desaparecen tras redeploy Railway

### Problema
Tras cada redeploy Railway borra el SQLite. Al re-sembrar cuentas MeLi desde env vars,
el nickname se obtiene de ML API (`/users/{id}`). Si ML rate-limita esa llamada durante
startup (varias cuentas refrescando simultáneamente), el nickname queda vacío y el
selector muestra el raw `user_id` (ej. "523916436"). Además el refresh_token rotado
no sobrevivía al redeploy porque solo se guardaba en `.env.production` (efímero).

### Solución — 3 fixes

**Fix 1 — Nicknames desde env vars (fallback estático):**
- `_parse_env_slots()` ahora devuelve 4-tupla `(uid, rt, label, nick)` leyendo `MELI_NICKNAME_N`
- `_seed_tokens()` copia `MELI_NICKNAME_N` de Railway env vars al igual que hace con UID/RT
- `_seed_one()` acepta `nickname_hint`; si ML API falla → usa el hint
- En `_seed_tokens`: si cuenta sin nickname + hay hint de env var → `update_nickname()` directo
- **Jovan debe agregar en Railway:** `MELI_NICKNAME=<nick acct1>`, `MELI_NICKNAME_2=<nick acct2>`, etc.

**Fix 2 — Railway API update en ML callback:**
- El callback OAuth de ML ahora detecta el slot del usuario desde Railway env vars
  (fuente de verdad, independiente de archivos efímeros)
- Después de escribir `.env.production`, llama Railway GraphQL `variableUpsert` para
  persistir `MELI_REFRESH_TOKEN_N`, `MELI_USER_ID_N` (nuevas cuentas), `MELI_NICKNAME_N`
- Mismo patrón que ya existía para Amazon tokens — ahora aplica a ML también
- Requiere `RAILWAY_API_TOKEN` + `RAILWAY_SERVICE_ID` + `RAILWAY_ENVIRONMENT_ID` + `RAILWAY_PROJECT_ID`

**Fix 3 — Indicador "sincronizando" en nav:**
- `_updateCacheAge(s, running)` ahora recibe el flag `running` del endpoint `/api/stock/prewarm-status`
- Si `running=true`: muestra "↻" azul en el badge; oculta el banner de datos desactualizados
- Threshold del banner stale subido de 15 min → 25 min (reduce falsos positivos cuando BM responde lento)

### Cambios
- `app/main.py`: `_parse_env_slots` (4-tupla), `_seed_one` (nickname_hint), `_seed_tokens` (MELI_NICKNAME_N vars + fallback)
- `app/auth.py`: callback ML — detección de slot desde Railway env vars + Railway GraphQL API update
- `app/templates/base.html`: `_updateCacheAge(s, running)` + `_checkCacheAge` pasa `d.running`

---

## 2026-05-18 — FEAT: Amazon — Tercera cuenta ExclusiveBulbs USA (AMAZON3_*)

### Descripción
Se agregó soporte para una tercera cuenta de Amazon (`AMAZON3_*`) correspondiente
a ExclusiveBulbs, que opera en Amazon USA (marketplace `ATVPDKIKX0ER`).

A diferencia de AUTOBOT (cuenta 2, que comparte la app LWA de VECKTOR), ExclusiveBulbs
usa su propia app LWA "Claude Exclusive" (`amzn1.sp.solution.04590df7-...`) con
credenciales propias (AMAZON3_CLIENT_ID / AMAZON3_CLIENT_SECRET).

El refresh token fue generado directamente desde Solution Provider Portal → Create Token
(self-authorization).

### Datos de la cuenta
- **Seller ID**: A22XNR713HGDVG
- **Nickname**: ExclusiveBulbs
- **Marketplace**: ATVPDKIKX0ER (Amazon USA)
- **App Solution ID**: amzn1.sp.solution.04590df7-1d50-40bc-9088-f950711048ca

### Cambios
- `app/config.py`: bloque `AMAZON3_*` (8 vars, default marketplace USA)
- `app/auth.py`: importa vars AMAZON3_*; callback detecta `_is_acct3` por seller_id; rama `if/elif/else` para token exchange con credenciales correctas por cuenta
- `app/services/amazon_client.py`: bloque tercera cuenta en `_seed_amazon_accounts()` con sus propias credenciales LWA

### Railway env vars a configurar (Jovan)
```
AMAZON3_CLIENT_ID=<ver Railway / reference_amazon_developer.md>
AMAZON3_CLIENT_SECRET=<ver Railway / reference_amazon_developer.md>
AMAZON3_SELLER_ID=A22XNR713HGDVG
AMAZON3_REFRESH_TOKEN=<ver Railway / reference_amazon_developer.md>
AMAZON3_APP_SOLUTION_ID=amzn1.sp.solution.04590df7-1d50-40bc-9088-f950711048ca
AMAZON3_MARKETPLACE_ID=ATVPDKIKX0ER
AMAZON3_MARKETPLACE_NAME=US
AMAZON3_NICKNAME=ExclusiveBulbs
```

---

## 2026-05-15 — FIX: Lanzador — stock obsoleto en lista "Sin publicar"

### Problema
SKUs como SNTV007050 aparecían con 177 unidades disponibles en la lista de gaps
pero el caché BM confirmaba 0 unidades reales. La tabla `bm_sku_gaps.stock_total`
es una instantánea del último escaneo y puede quedarse desactualizada.

### Solución
En el endpoint de gaps (`GET /api/lanzar/gaps`), después de leer la página de resultados,
se hace un batch-query a `bm_stock_cache` para todos los SKUs de la página.
Si el caché tiene un valor diferente al del escaneo, se sobrescribe `stock_total` con
el valor real y se agrega `stock_stale: true` al item.

En el frontend (tabla de gaps), cuando `stock_stale=true` y `stock_total===0`,
se muestra una celda naranja "⚠ 0 / sin stock" en lugar del número obsoleto.

### Cambios
- `app/api/lanzar.py`: batch-query `bm_stock_cache` dentro del bloque `async with`; overlay de stock real + flag `stock_stale` en items loop
- `app/templates/partials/lanzar_gaps.html`: celda Stock con badge naranja "⚠ 0 sin stock" cuando `stock_stale && stock_total===0`

### Commit
`2ac72fa`

---

## 2026-05-14 — FEAT: Sistema de sugerencias cruzadas entre cuentas

### Descripción
Comunicación in-app entre cuentas (APANTALLATEMX, AUTOBOT, LUTEMA, BLOW).
Desde el drawer de Análisis de Competencia, cualquier anotación dirigida a otra cuenta
muestra un botón 📤. Al presionarlo, se guarda la sugerencia en DB y el responsable de
esa cuenta la ve en su campana 🔔 sin necesidad de email ni WhatsApp.

### Flujo
1. Usuario abre drawer ⚡ → ve anotaciones de todas las cuentas
2. Filas de otras cuentas → botón 📤 (propias no tienen el botón — no tendría sentido notificarse a uno mismo)
3. Click en 📤 → `POST /api/suggestions` → guarda en DB con `from_account`, `to_account`, `item_id`, `sku`, `item_title`, `action`, `reason`
4. Campana 🔔 en nav MeLi con badge rojo si hay sugerencias pendientes (polling cada 2 min)
5. Click en campana → panel lateral con lista: acción, SKU, título, quien mandó, tiempo transcurrido
6. Botones por sugerencia: `✓ Aplicado` / `⏳ En proceso` / `✕ Descartar`

### Cambios
- `app/services/token_store.py`: tabla `suggestions` con índice por `(to_account, status)`
- `app/main.py`: `POST /api/suggestions`, `GET /api/suggestions`, `PATCH /api/suggestions/{id}`
- `app/templates/base.html`: campana 🔔 en nav MeLi, panel `#notif-panel`, JS de polling/render/acciones
- `app/templates/dashboard.html`: `_compCurrentAccount` inyectado desde Jinja, botón 📤 en `_notes.forEach`, `window._sendSuggestion()`

---

## 2026-05-14 — FEAT: Competition drawer — Bloque Anotaciones por listing

### Descripción
Se agrega un nuevo bloque "Anotaciones" al fondo del competition drawer con propuestas
accionables por cada listing activo. Son sugerencias visuales — el usuario decide qué hacer.

### Lógica de anotaciones (JS frontend, sin cambios al backend)
- **Listing de catálogo + Ganando + ≥10 uds** → `MANTENER` (verde)
- **Listing de catálogo + Ganando + <10 uds** → `REVISAR` (amarillo) — pocas ventas pese a ganar
- **Listing de catálogo + Compitiendo** → `BAJAR PRECIO` (naranja) — incluye price to win si disponible
- **Listing único + 0 ventas + duplicado de misma cuenta** → `PAUSAR` (rojo)
- **Listing único + 0 ventas** → `SIN VENTAS` (rojo) — considera bajar precio
- **Listing único + ≥20 uds + %Rec ≥55%** → `SUBIR PRECIO` (verde) — hay margen
- **Listing único + ≤3 uds** → `VENTAS BAJAS` (amarillo)
- **Listing único + ventas normales** → `MANTENER` (gris)

Detección de duplicados: misma cuenta + mismo precio → el de 0 ventas se marca PAUSAR.

---

## 2026-05-14 — FEAT: Análisis de Competencia — drawer por producto

### Descripción
Botón ⚡ en cada fila del panel Top Productos. Abre un drawer lateral derecho con:
1. **Resumen SKU**: SKU BM, stock disponible, RetailPH USD/MXN
2. **Nuestros listings**: todos los listings del mismo SKU en todas las cuentas, con ventas 30d, margen real, % recuperado, posición en catálogo (WINNING/COMPETING/LOSING)
3. **Competidores externos**: precios de vendedores ajenos en el mismo catálogo ML, con indicador de buy box winner
4. **Recomendación automática**: basada en el listing con mejor margen activo vs precio del externo más barato

### Endpoint
`GET /api/metrics/competition?item_id=MLM...`
- Busca el item en `ml_listings` → extrae SKU base
- Agrupa todos nuestros listings del mismo SKU
- Lee ventas 30d de `order_history` (margen real, % recuperado)
- Llama `price_to_win` por cuenta para cada listing de catálogo
- Obtiene `catalog_product_id` → llama `/products/{id}/items` para externos
- Lee BM stock de `_bm_stock_cache` (sin llamar BM en vivo)

### UI
- Drawer deslizable desde la derecha (420px, con overlay)
- Filas en verde si tuvieron ventas en 30d
- Margen: verde ≥10%, amarillo ≥5%, gris sin datos

---

## 2026-05-14 — FEAT: Top Productos — columna BM Avail junto a ML Stock

### Descripción
Se agregó columna **BM** (stock disponible en BinManager) en el panel Top Productos,
junto a la columna existente de ML Stock. Los headers cambiaron a `BM` (azul) y `ML`.

### Cambios
- `app/api/metrics.py`: lookup BM por SKU en el loop de resultados del endpoint `/top-products`
  usando `_bm_stock()` (cache-first, no genera llamadas extra si el SKU ya está en caché)
- `app/templates/dashboard.html`: helper `stockBadge(val, noDataText)` reemplaza lógica inline;
  `bmStockHtml` y `mlStockHtml` generados con el mismo helper para consistencia visual
- Row alert: fondo naranja si `status=active && bm_avail===0`; amarillo si pausado con `bm_avail>0`
- SKU sin BM: muestra `?` en columna BM; sin SKU: `S/SKU` en gris

---

## 2026-05-14 — FEAT: Panel "Top Productos" — ranking de ventas al lado del heatmap semanal

### Descripción
Panel nuevo a la derecha del heatmap semanal (flex layout en desktop). Muestra el ranking
de los 20 productos más vendidos del período seleccionado con estado actual en ML.

### Features
- Selector de período: 7d / 15d / 30d / 90d (default 30d). Carga instantánea.
- Columnas: #, Foto + SKU/Producto, Uds vendidas, Neto MXN, BM Stock, ML Stock, Status
- Stock badge: verde >5 uds / naranja 1-5 uds / rojo 0 uds
- Alertas visuales por fila:
  - Fondo naranja: Activo en ML pero BM=0 (riesgo de oversell o pérdida de ventas)
  - Fondo amarillo: Pausado pero tiene stock BM (oportunidad no aprovechada)
- Scroll interno (max 320px) para no romper el layout de la card
- Heatmap: `flex-none` (ancho natural); Top Productos: `flex-1` (rellena el espacio)

### Endpoint
`GET /api/metrics/top-products?days=N`
- Fetcha órdenes ML del período
- Agrupa por item_id: suma unidades y revenue neto
- Batch fetch `/items?ids=...` para status, available_quantity y thumbnail
- Retorna top 20 ordenado por unidades

---

## 2026-05-14 — FIX: "Sesión no disponible" tras Railway restart + nicknames ML en dropdown

### Problema
Dashboard mostraba "Sesión no disponible / El servicio no está conectado" tras restart de Railway.
Adicionalmente el dropdown de cuentas mostraba user IDs numéricos en lugar de nombres (APANTALLATEMX, etc.).

### Root cause
Railway reinicia el contenedor ocasionalmente (mantenimiento). Al reiniciar, el SQLite DB en volumen
puede quedar vacío o los tokens ML pueden no sembrarse correctamente si ML rate-limita el token
endpoint durante el arranque (`_seed_tokens_with_retry` ya existe pero puede fallar si ML responde 429
por >12 minutos seguidos). Sin tokens → `get_any_tokens()` devuelve None → `get_current_user()` → None.

El `diag/refresh-ml-tokens` tampoco obtenía nickname de ML API al guardar tokens → DB guardaba
access/refresh tokens pero nickname vacío → dropdown mostraba user ID como fallback.

### Fix
1. **Lazy auto-seed**: `get_meli_client()` en `meli_client.py` ahora detecta si `get_any_tokens()`
   devuelve None y llama `_auto_seed_from_env()` automáticamente (cooldown 5 min para no spamear).
   `_auto_seed_from_env()` hace refresh de todos los slots de env vars + obtiene nickname de ML API.

2. **Nickname en diag/refresh**: `diag/refresh-ml-tokens` ahora también hace GET a `/users/{uid}`
   para obtener el nickname y lo guarda en DB. Solo fetcha si el nickname aún no existe en DB.

### Operación realizada
- `diag/refresh-ml-tokens` llamado manualmente para re-sembrar tokens tras el Railway restart
- Segunda llamada después del deploy para poblar nicknames

### Archivos modificados
- `app/services/meli_client.py` → `_auto_seed_from_env()` + lazy re-seed en `get_meli_client()`
- `app/main.py` → `diag/refresh-ml-tokens` ahora incluye nickname fetch

---

## 2026-05-13 — FIX: Activar variaciones ponía mismo stock a todas — usa sync-variation-stocks

### Problema
Botón "Sync + Activar" en secciones Stock/Activar llamaba `/api/items/{id}/stock` con el stock
total del producto. Para listings con variaciones (ej. MLM1375689664 con 16 colores), esto ponía
la misma cantidad a todas las variaciones en lugar del stock individual por SKU de BM.

### Root cause
`activateItem()` ignoraba si el producto tenía variaciones y siempre usaba el endpoint simple de
stock plano. El endpoint correcto para variaciones es `sync-variation-stocks` que:
1. Obtiene el `seller_custom_field` de cada variación via API ML
2. Consulta BM individualmente por SKU de variación
3. Actualiza cada variación con su stock propio

### Fix
- `activateItem(itemId, bmTotal, status, btn, hasVariations)` — nuevo parámetro `hasVariations`
  - Si `true`: llama `POST /sync-variation-stocks` con `pct=1.0`
  - Si `false`: comportamiento anterior (`PUT /stock`)
- `bulkActivateAll`: mismo split por `item.hasVars`
- Templates: botones pasan `has_variations` desde Jinja — `{{ 'true' if p.get('has_variations') else 'false' }}`
- Fix aplicado en 4 lugares: vista mobile + desktop en `stock_section_restock.html` y `products_stock_issues.html`

---

## 2026-05-13 — FIX: KPI "Sin Stock (con BM)" mostraba 0 — lógica restock_count corregida

### Problema
KPI "Sin Stock (con BM)" y "Revenue Perdido" en tab Stock mostraban 0 a pesar de que había
123 productos en "Oportunidad Activar" y 151 en "Stock Crítico". Lógicamente imposible.

### Root cause
`restock` (productos con MeLi=0 pero BM tiene stock) se filtraba con `p.get("units", 0) > 0`.
Un producto que lleva 30+ días sin stock en MeLi tiene `units=0` porque no puede vender — es
exactamente el problema que queremos detectar. El filtro excluía todos los candidatos válidos.

### Fix
- `restock_count`: ahora es `len(restock) + len(activate)` — incluye ambas listas (sin stock BM
  también con oportunidad de activar desde cero)
- `lost_revenue`: suma revenue de `restock` + estimado conservador para `activate`
  (`price * min(bm_avail, 3)` por producto)
- Subtitle KPI cambiado de "Con ventas recientes" → "MeLi=0, BM tiene stock" en ambos templates:
  `stock_section_restock.html` y `products_stock_issues.html`

### Archivos modificados
- `app/main.py` → línea ~3570: fórmula `restock_count` y `lost_revenue`
- `app/templates/partials/stock_section_restock.html` → subtitle KPI
- `app/templates/partials/products_stock_issues.html` → subtitle KPI

---

## 2026-05-13 — FEAT: Diagnóstico de ventas — heatmap semanal + desglose por día + alertas stock

### Contexto
Apantallate promedia 72% de meta diaria con alta variabilidad entre días. Se necesitaban herramientas
para identificar causas: ¿quiebre de stock? ¿patrón día-de-semana? ¿SKU que desapareció?

### Features implementadas (aplican por cuenta ML activa)

**1. Heatmap patrón semanal**
- Grid 4-5 semanas × 7 días (Lun-Dom) dentro de la sección Meta Diaria
- Colores: verde ≥90%, verde-lima 75-90%, naranja 50-75%, rojo <50%
- Cada celda es clickeable → abre desglose del día
- Detecta visualmente si ciertos días de la semana son sistemáticamente bajos

**2. Desglose por día (click en fila de tabla o celda del heatmap)**
- Endpoint: `GET /api/metrics/day-breakdown?date=YYYY-MM-DD`
- Muestra top SKUs vendidos ese día con comparativa vs promedio 7 días anteriores
- Columnas: SKU, Producto, Unidades hoy, Promedio 7d, Δ% vs promedio, Venta MXN
- Panel inline colapsable dentro de la sección Meta Diaria

**3. Alertas de Stock Crítico**
- Sección nueva entre Meta Diaria y Gráfico de Ventas
- Endpoint: `GET /api/metrics/low-stock-alerts?threshold=N`
- Top 10 SKUs por volumen (30 días) + stock BM en tiempo real
- Columnas: SKU, Producto, Stock BM, Velocidad/día, Días restantes, Ventas 30d
- Umbral configurable (default 5 uds) — banner rojo si hay SKUs en alerta
- Usuario activa manualmente con botón "Revisar" (BM calls on-demand)

### Commits
- `f39fe0e` feat: diagnóstico de ventas — heatmap semanal, desglose por día, alertas stock

---

## 2026-05-12 — FIX: SKU Ventas — columnas de costo removidas, Retail PH + % Recuperado

### Problema
Tab "SKU > Ventas" mostraba ROI -94.4%, Margen -2897%, Ganancia/u -$119,331 para todos los
productos. Root cause: `AvgCostQTY` de `Get_GlobalStock_InventoryBySKU` devuelve valores en MXN
(no USD) para algunos items; el código los trataba como USD y los multiplicaba por FX de nuevo
→ costo_mxn 17.77× inflado → ROI completamente negativo.

### Decisión
No manejamos costo de compra. La referencia de negocio es **Retail PH** (LastRetailPricePurchaseHistory
de BM). La métrica clave es cuánto % del Retail PH recuperamos como neto de ML. Meta ≥ 100%.

### Fix
- Eliminadas columnas: Costo (USD), ROI, Margen, Ganancia/u — todas dependían de costo inválido
- Retail PH: MXN primario (azul) + USD secundario (gris, debajo)
- Ingreso Total: MXN primario + USD secundario
- **% Recuperado**: neto ML real / (qty × RetailPH MXN) × 100
  - Verde ≥ 100% | Amarillo ≥ 80% | Rojo < 80%
- Regla de display establecida: en TODO el dashboard, dinero = MXN grande + USD pequeño debajo

### Commits
- `05b0544` fix: SKU Ventas — quitar columnas de costo, usar Retail PH + % Recuperado

---

## 2026-05-07 — FIX: Sesión dashboard perdida en cada redeploy Railway

### Problema
Cada push a Railway reiniciaba el contenedor → SQLite DB ephemeral → tabla `user_sessions`
borrada → cookie `dash_session` inválida → pantalla "Sesión no disponible" → todos los
operadores tenían que re-loguearse después de cada deploy.

### Root cause
`get_session()` hacía lookup en DB para validar el token. El token era opaco (`secrets.token_urlsafe(32)`),
sin datos propios. Al borrar la DB en cada deploy, el lookup fallaba aunque el usuario tuviera
cookie válida.

### Fix: JWT firmado en la cookie
- `create_session()`: genera un JWT (`body.sig`) con `{uid, exp, username, display_name, role, must_change_pw, allowed_sections}` firmado con HMAC-SHA256
- `get_session()`: valida la firma del JWT directamente — **sin tocar la DB**. DB solo se consulta
  como fallback para tokens opacos legacy
- Clave de firma: env var `SECRET_KEY` (Railway) o fallback determinista derivado de `DATABASE_PATH`
- La DB sigue usándose para guardar el token (auditoría, soporte logout), pero ya no es necesaria para validar la sesión
- Los JWTs vencen a los 30 días igual que antes

### Resultado
Tras redeploy: la cookie `dash_session` sigue siendo válida → sin re-login → operadores
no se interrumpen. El único token que requiere re-login ahora es cuando la `SECRET_KEY`
cambia (o si el usuario cierra sesión manualmente).

### Recomendación Railway
Agregar env var `SECRET_KEY=<random-hex-64>` en Railway para mayor seguridad (sin esto
usa un fallback determinista basado en DATABASE_PATH que funciona igual pero es predecible).

### Archivos
- `app/services/user_store.py` — `_jwt_sign()`, `_jwt_verify()`, `create_session()`, `get_session()`

---

## 2026-05-08 — FEAT: order_history — historial de precio de venta y ganancia neta

### Qué hace
Base de datos persistente de todas las ventas por SKU, cuenta y plataforma (ML + Amazon).
Crece automáticamente sin intervención manual.

### Schema: tabla `order_history`
`order_id | account_id | platform | item_id | sku | unit_price | quantity | sale_fee |
neto_plat | costo_usd | costo_mxn | retail_ph_usd | ganancia_neta | margen_pct |
recup_retail_pct | fx_rate | currency | order_date | order_month | status | data_source`

### Pipeline de datos
- **ML**: `_save_ml_orders_history_bg()` — al cargar tab Deals, guarda los últimos 30 días
  de órdenes paid/delivered con snapshot de costo/retail BM al momento de la venta.
  `data_source='estimated'` (se actualizará a 'real' cuando tab Ventas procese /collections).
- **Amazon**: `_save_amazon_items_history_bg()` — al expandir detalle de una orden Amazon
  guarda SKU + precio unitario + ganancia estimada (fee ~10%, retenciones ~9%).

### Endpoint de consulta
`GET /api/sku-history?sku=SNTV007322` — HTML con:
- Cards: total órdenes/unidades, P.Venta avg/min/max, Ganancia avg/peor caso, Margen avg/min/max
- Tabla: fecha, plataforma, cuenta, P.Venta, Qty, Neto, Ganancia, Margen, fuente (real/est.)

### Próximo paso: panel expandible en tab Deals
Cada fila de Deals podrá mostrar el historial del SKU sin salir de la pantalla.

### Archivos
- `app/services/token_store.py` — tabla + upsert + queries
- `app/main.py` — helper ML + llamada en deals flow + endpoint /api/sku-history
- `app/api/amazon_orders.py` — hook al expandir orden

### Commit
`10be394`

---

## 2026-05-08 — FIX: Deals — Retail BM y Neto ML más precisos

### Problema
Para SNTV007322 (TV Samsung 55") el tab Deals mostraba:
- Retail BM: $6,517 ($378 USD) vs $9,704 en vista Ventas (49% de diferencia)
- Neto ML: $4,583 vs $4,908 real de la orden (6.6% de diferencia)

### Root cause
1. `_enrich_with_bm_product_info` usaba `RetailPrice` como primera opción, pero `RetailPrice`
   en BM puede ser incorrecto o el costo de compra. `LastRetailPricePurchaseHistory` es el
   precio real de referencia y es el mismo campo que usa la vista Ventas (`_sku_retail_map`).
2. `_calc_margins` aplicaba factor fijo `0.7295` (asume fee ML=18% para TODOS los precios).
   Para items ≥$5,000 la tarifa ML es 12%, no 18% — error de ~6pp en neto estimado.
3. `_item_net_ratio_map` (ratio neto real/total de órdenes) solo se poblaba al abrir el tab
   Ventas. Si Deals se abría primero, usaba solo la fórmula estimada.

### Fix
- `_enrich_with_bm_product_info`: usa `LastRetailPricePurchaseHistory` como primary, fallback a `RetailPrice`
- `_calc_margins`: reemplaza factor 0.7295 con `_ml_fee(price)` por tramo de precio
- Nueva función `_preload_item_neto_ratios(orders)`: pre-carga ratios reales desde `all_orders`
  (ya disponible en el deals flow) en `_item_net_ratio_map` — usa `sale_fee` real si viene
  en la orden, estimado por `_ml_fee()` si no

### Resultado esperado SNTV007322
- Retail BM: ~$9,704 (antes $6,517) — alineado con vista Ventas
- Neto ML: ~$4,900 (antes $4,583) — más preciso con ratio real de 45 ventas del mes

### Archivos
- `app/main.py` — `_calc_margins()`, `_enrich_with_bm_product_info()`, nueva `_preload_item_neto_ratios()`

### Commit
`bf8f78d`

---

## 2026-05-08 — FIX: Deals Activos — P. Lista / P. Deal / Desc. incorrectos para MARKETPLACE_CAMPAIGN

### Problema
Items con campaña ML (tipo `MARKETPLACE_CAMPAIGN`, ej. MLM5239612118 cuenta BLOW $12,999 → $7,919):
- P. Lista mostraba `-` (en blanco)
- P. Deal mostraba $12,999 (precio de lista, NO el precio deal del comprador)
- Desc. mostraba badge "Campaña" en vez de `-39%`

### Root cause
Dos bugs combinados:
1. **API bulk** (`get_promotion_items`): para MARKETPLACE_CAMPAIGN devuelve `price = $12,999`
   (precio del vendedor, no el precio deal del comprador $7,919). Resultado: `_promo_deal_price = $12,999 = p.price` → sin reducción detectada.
2. **`_enrich_with_promotions`** (API per-item): SÍ devuelve el precio real del comprador ($7,919),
   pero (a) MARKETPLACE_CAMPAIGN no estaba en `_auto_types` → clasificado como seller promo → ignorado,
   y (b) solo se llamaba para top-25 candidatos, nunca para items ya en `active_deals`.

### Fix
- `_enrich_with_promotions`: agrega `MARKETPLACE_CAMPAIGN` a `_auto_types`
- En vez de sobrescribir `p["price"]` con el deal price (perdiendo el precio lista), ahora
  setea `p["_promo_deal_price"] = deal_price` y mantiene `p.price` como precio lista
- Agrega `_has_price_reduction` calculado desde per-item API
- Deals flow: corre `_enrich_with_promotions` sobre `active_deals[:100]` + `top25 candidatos`
  en paralelo → la per-item API sobreescribe y corrige el `_promo_deal_price` incorrecto del bulk

### Resultado
Para MLM5239612118 (y cualquier MARKETPLACE_CAMPAIGN):
- P. Lista: $12,999 tachado ✓
- P. Deal: $7,919 ✓
- Desc.: -39% ✓

### Archivos
- `app/main.py` — `_enrich_with_promotions()` (~línea 1052), deals flow (~línea 4846)

### Commit
`9555b8f`

---

## 2026-05-07 — FIX: Deals — P. Lista / P. Deal / Desc. incorrectos para deals con ML%

### Problema
Items con `_meli_promo_pct > 0` (ML subsidia X% del precio): el tipo de promo no es
`ML_Auto` → `_deal_is_ml_auto = False` → template solo mostraba `p.price` en P. Deal
(precio de lista), P. Lista en blanco, Desc. en blanco.

### Root cause
El template condicionaba P. Lista tachado y P. Deal real SOLO a `_deal_is_ml_auto`.
Para PRE_NEGOTIATED y similares donde ML cubre el descuento, `_deal_is_ml_auto = False`
aunque sí haya `_meli_promo_pct > 0`.

### Fix
- P. Lista tachado: ahora también se muestra cuando `_meli_promo_pct > 0`
- P. Deal: cuando `_meli_promo_pct > 0`, calcula `price × (1 − meli_pct/100)` (precio del comprador)
- Desc.: muestra `-meli_pct%` cuando `_meli_promo_pct > 0`
- Aplica a table (desktop) y cards (mobile) de Deals Activos

### Archivos
- `app/templates/partials/products_deals.html` — P. Lista, P. Deal, Desc. (desktop + mobile)

---

## 2026-05-07 — FIX: Deals — Neto ML y Retail BM en blanco para muchos items

### Problema 1: Neto ML en blanco para deals con price=0
Items de catálogo ML donde el precio es controlado por ML tienen `price=0` en el body.
Si además no tienen `_promo_deal_price`, `_sale_price=0` → `_neto_ml=None`.

**Fix:** `_sale_price = promo_deal_price or price or original_price` — usa `original_price` como
último fallback, que siempre tiene valor para items clasificados como deal (`original_price > 0`).

### Problema 2: Retail BM en blanco para items en catálogo DB
`_enrich_with_bm_product_info` solo buscaba en el bulk cache de la API BM. Si un SKU no
estaba en el bulk cache (prewarm distinto, cache expirado), no se encontraban datos aunque
el SKU existiera en los 8,552 SKUs del catálogo DB (`_bm_retail_ph_cache`).

**Fix:** Al no encontrar SKU en bulk cache, buscar en `_bm_retail_ph_cache` como fallback.
Esto permite mostrar Retail BM y calcular Neto ML / % Retail para la gran mayoría de items.

### Archivos modificados
- `app/main.py` — `_calc_margins` línea 175: fallback `original_price`
- `app/main.py` — `_enrich_with_bm_product_info` línea 1161: fallback DB catalog

---

## 2026-05-06 — FEAT: Deals — Neto ML y % Retail reemplazan Ganancia y Margen

### Cambio
Las columnas "Ganancia" y "Margen" (basadas en costo BM) fueron reemplazadas por métricas que no
requieren costo ya que no se tiene esa referencia:

- **Neto ML** (`_neto_ml`): monto que queda después de comisión ML (×1.16 IVA) y $150 envío.
  Fórmula: `deal_price × (1 − fee × 1.16) − 150`
- **% Retail** (`_recup_retail_pct`): Neto ML como % del Retail BM.
  Fórmula: `Neto ML / Retail BM × 100`. Ej: recibir $800 de un retail $1,000 = 80%.

Color coding % Retail: ≥100% verde · 80-99% amarillo · 60-79% naranja · <60% rojo.

Aplica en tablas desktop y mobile cards tanto para Deals Activos como Candidatos.
`data-margin` en filas también usa `_recup_retail_pct` para el sort correcto por % Retail.

### Archivos modificados
- `app/templates/partials/products_deals.html` — headers, celdas y badges en todas las vistas
- `app/main.py` — `_calc_margins` calcula `_neto_ml` y `_recup_retail_pct` (sesión anterior)

---

## 2026-05-06 — FIX: Deals — múltiples bugs resueltos (sesión anterior)

### Fixes aplicados
1. **ERROR_CREDIBILITY_DISCOUNTED_PRICE** — deal activation pre-verifica candidato ML y respeta `max_discounted_price`
2. **"Error: desconocido"** — frontend ahora muestra `data.error || data.detail || JSON.stringify(data)`
3. **"Error: Not Found"** — URL corregida `/bm/sync-price` → `/api/lanzar/sync-price`
4. **"original_price is not modifiable"** — `price_type` cambiado a `'price'`, botón "Subir base" → "Subir precio"
5. **Bug cross-account** — `_account_id` stampeado en cada item, embebido en `data-account`, pasado via `_rowAccount(btn)` al backend
6. **ML_Auto precio deal incorrecto** — `_promo_deal_price = promo_data["deal_price"]` almacenado y usado en P. Deal column

---

## 2026-05-05 — FIX: Deals — ERROR_CREDIBILITY_DISCOUNTED_PRICE al activar deal

### Problema
`applyAndActivateDeal` y `quickDealByPct` calculaban `deal_price` como `precio × (1 − buffer%)` sin verificar
el historial de precios que ML usa para validar credibilidad. Si el precio fue subido recientemente (ej.
MLM3872998748: $3,919 → $4,984), ML rechazaba con `ERROR_CREDIBILITY_DISCOUNTED_PRICE` porque el deal
resultante ($4,236) superaba el máximo histórico que ML acepta ($3,527).

### Fix
Ambas funciones JS ahora siguen un flujo de dos pasos:
1. `GET /api/items/{id}/promotions` → busca candidato PRICE_DISCOUNT con `suggested_discounted_price` / `max_discounted_price`
2. Si no hay candidato → error claro: "ML no tiene deal disponible, espera 1-3 días si el precio fue modificado"
3. Si hay candidato: compara precio deseado contra `max_discounted_price`:
   - Si supera el máximo → usa `suggested_discounted_price` de ML (precio históricamente creíble)
   - Si no supera → usa precio calculado normalmente
4. Activa el deal con `original_price = candidate.original_price` (precio histórico de ML, no el nuestro)

`quickDealByPct` muestra error específico cuando el % manual del usuario excede el techo de ML,
indicando el precio máximo permitido.

### Archivos modificados
- `app/templates/partials/products_deals.html` — reescritura de `applyAndActivateDeal` y `quickDealByPct`

---

## 2026-05-05 — FEAT: Precio deal por cuenta en Ventas/Órdenes (anti-detección)

### Feature
En la columna P. SUGERIDO de Ventas/Órdenes se añade un segundo precio (naranja): el precio
de lista para correr un deal manteniendo el target de recuperación de retail.

Fórmula: `deal_price = retail × retail_target_pct / ((1 − deal_buffer_pct) × net_ratio)`

Cada cuenta tiene buffer y target distintos → competencia y ML no detectan que son el mismo vendedor.

### Config por cuenta (vía POST /api/deal-config?user_id=XXX)
- APANTALLATEMX: 18% buffer, 99% retail target
- BLOWTECHNOLOGIES: 20% buffer, 101% retail target
- LUTEMAMEXICO: 15% buffer, 95.9% retail target
- AUTOBOT MEXICO: 22% buffer, 98% retail target

### Archivos modificados
- `app/services/token_store.py` — tabla `account_deal_config`, `get_deal_config()`, `set_deal_config()`
- `app/main.py` — fetch config pre-loop, cálculo `_precio_deal`, endpoints GET/POST `/api/deal-config`
- `app/templates/partials/orders_table.html` — segunda línea naranja con precio deal y %

### Commit: eb27e80

---

## 2026-05-05 — FIX: Serialización total de requests BM — bm_post() entrada única

### Problema
BM bloqueaba sesiones de usuarios (Carlos, Claudio) por requests HTTP paralelos. El semáforo
`_BM_GLOBAL_SEM = asyncio.Semaphore(1)` en `binmanager_client.py` solo protegía `_post()/_get()`
del cliente compartido. Más de 20 sitios en 9 archivos usaban `httpx.AsyncClient()` crudo o
`asyncio.Semaphore(15/10)` locales que **bypasseaban completamente** el semáforo global,
generando hasta 45 requests paralelos a BM (p.ej. `amazon_products.py`: 3 raw httpx × Semaphore(15)).

El patrón más grave: `asyncio.gather(http.post(BM_URL, ...), bm_cli.get_available_qty(...))` —
un raw httpx en paralelo con uno serializado, lo que siempre enviaba ≥2 requests simultáneos.

### Solución
Nueva función pública `bm_post(url, payload, timeout)` en `binmanager_client.py`:
- Llama `get_shared_bm()` → `post_inventory()` → `_post()` → `_BM_GLOBAL_SEM`
- **Punto de entrada único** para TODOS los POST a BM

Todos los `asyncio.gather` con BM convertidos a awaits secuenciales.
Todos los `httpx.AsyncClient()` crudos para BM eliminados.

### Archivos modificados (10 archivos, ~20+ sitios)
| Archivo | Cambio principal |
|---|---|
| `app/services/binmanager_client.py` | +`bm_post()` función pública |
| `app/api/lanzar.py` | `_bm_login`, `_bm_fetch_all_skus_with_stock`, `_bm_fetch_warehouse_stock`, BM-images |
| `app/main.py` | 9+ sitios: enrich batch, items grid, deal comparison, `_fetch_var_bm`, deal modal, catalog sync, 4 diag endpoints |
| `app/api/binmanager.py` | `retail-ph-batch`: eliminado `BinManagerClient()` separado + `Semaphore(10)` |
| `app/api/amazon_products.py` | `_fetch_base`: 3 parallel×Sem(15) → 3 sequential `bm_post()` |
| `app/api/items.py` | `_bm_warehouse_qty`, batch + single endpoints |
| `app/api/productos.py` | `_bm_stock` + 2 call sites |
| `app/api/sku_inventory.py` | `_fetch_sellable_stock`, `_fetch_binmanager_product_info`, `process_sku` |
| `app/api/health_ai.py` | `_fetch_bm_product` |
| `app/api/system_health.py` | `_check_binmanager` |

### Resultado
Máximo 1 request activo a BM en todo el proceso, siempre.
Commit: `04450c8` — pushed a Railway.

---

## 2026-04-29 — FIX: Impuestos en desglose de órdenes — fórmula per-pago correcta

### Problema
El campo "Impuestos" en el desglose de cobros de la tabla de órdenes era incorrecto.
La fórmula anterior usaba `taxes = total_amount - sum(net_received_amount)`, que incluía
tanto la comisión de ML (`marketplace_fee`) como las retenciones fiscales (IVA+ISR).
Resultado: para la orden 2000016202805920 mostraba $1,577.46 cuando lo correcto es $864.96.

`net_received_amount` de `/collections/{id}` ya tiene `marketplace_fee` descontado:
  `net_received = transaction_amount - marketplace_fee - retenciones_fiscales`

Por eso sumar todos los `net_received` y restarlos del total mezclaba comisión con impuestos.

### Solución

**`app/services/meli_client.py`** — nuevo método `get_payment_collection_details()`:
- Retorna `{net_received_amount, transaction_amount, marketplace_fee}` por pago
- `get_payment_net_amount()` sin cambios (sigue usándose en KPIs / `enrich_orders_with_net_amount`)

**`app/main.py`** — reemplazado el loop `net_amounts` con `payment_details`:
- Por cada pago: `taxes += transaction_amount - marketplace_fee - net_received_amount`
- `net = sum(net_received_amount) - shipping_cost`
  (marketplace_fee ya está descontado por ML en `net_received_amount`)

### Verificación con orden 2000016202805920
| Pago | transaction | fee | net_received | taxes_pago |
|------|------------|-----|--------------|-----------|
| 157006910990 | $4,199.00 | $712.50 | $2,757.41 | $729.09 |
| 157006932618 | $1,500.99 | $0.00 | $1,365.12 | $135.87 |
| **Total** | $5,699.99 | $712.50 | $4,122.53 | **$864.96** |

Antes: taxes=$1,577.46, net=$3,061.03 — Ahora: taxes=$864.96, net=$3,773.53

---

## 2026-04-28 — FEAT: Returns section — aislamiento por cuenta + filtro por tipo de reclamo

### Problema
Todos los endpoints de la sección de Retornos usaban `get_meli_client()` sin `user_id`,
lo que siempre traía los reclamos de la cuenta activa en sesión, no de la cuenta seleccionada.
Si el usuario tenía Autobot o Lutema como cuenta activa pero quería ver retornos de otra cuenta,
el selector no tenía efecto alguno.

### Solución

**Backend (`app/main.py`)** — 5 endpoints actualizados:
- `/partials/returns-summary` — nuevo param `account_id`
- `/partials/returns-table` — nuevos params `account_id` + `claim_type` (pdd/pntr/other)
- `/api/returns/analysis` — nuevo param `account_id`
- `/api/returns/top-products` — nuevo param `account_id`
- `/api/returns/timeline` — nuevo param `account_id`

Todos usan `get_meli_client(user_id=account_id or None)`. El helper `_fetch_all_claims_cached`
ya cacheaba por `client.user_id`, así que el caché también está aislado por cuenta.

**Frontend (`app/templates/returns.html`)**:
- `retFilters.account_id` inyectado desde `{{ active_user_id }}` (Jinja2)
- `_buildParams()` ahora incluye `account_id` y `claim_type` en todos los fetches
- `loadTopProducts()` también pasa `account_id`
- Badge de cuenta activa en el header de la página
- Filtros por tipo de reclamo: botones Todos / Defecto·Diferente / No recibido / Otros
- `setRetClaimType()` — nueva función que actualiza `retFilters.claim_type` y recarga tabla
- Sidebar "Estado de Reclamos": muestra abiertos + resueltos (poblado por `loadAnalysis()`)
- Sidebar "Acciones Rápidas": reemplaza los tips estáticos — botones directos a filtros y ML

### Resultado
Cada cuenta ML muestra sus propios reclamos/retornos. El filtro por tipo de reclamo permite
aislar defectos, no recibidos u otros en un solo clic.

---

## 2026-04-28 — FIX: PriceMonitor dejaba de golpear BM — usa _bm_retail_ph_cache

### Problema
`PriceMonitor` (app/services/price_monitor.py) creaba su propio `BinManagerClient()` y hacía poll
a BM cada 300 segundos (5 min) por cada SKU watcheado de forma individual. Esto:
- Generaba tráfico continuo a BM independiente del sistema de prewarm
- Mostraba sesión activa en el audit log de BM con usuario incorrecto en algunos entornos
- Causaba re-login en cada restart del servicio

### Solución (commit aae573b)

1. **`PriceMonitor.set_cache(dict)`** — nuevo método que conecta la caché local `_bm_retail_ph_cache`
   al monitor. Cuando está configurado, `_check_prices()` lee de memoria (cero hits a BM).

2. **`_check_prices()` usa caché local si disponible:**
   ```python
   if self._ext_cache is not None:
       entry = self._ext_cache.get(sku)
       price = entry[1] if entry and entry[1] > 0 else None
   else:
       price = await self._client.get_retail_price_ph(sku)  # fallback
   ```

3. **`start()` omite login BM** cuando `_ext_cache` está configurado.

4. **`main.py`** llama `price_monitor.set_cache(_bm_retail_ph_cache)` antes de `start()`,
   después de que `_load_catalog_from_db()` ya pobló la caché desde SQLite.

### Resultado
PriceMonitor sigue detectando cambios de precio pero lee del catálogo local semanal —
sin sesiones adicionales en BM, sin polling individual por SKU.

---

## 2026-04-27 — FIX: OOM Railway — slim BM caches + limpieza periódica de memoria

### Problema
Servicio crasheaba cada ~5 min en Railway con "Out of memory". Causa: tres fugas de memoria acumuladas.

### Causas y soluciones (commit 86f53b0)

1. **`_bm_bulk_gr_cache` / `_bm_bulk_all_cache` almacenaban rows BM completos (30+ campos)** → ahora solo 10 campos via `_slim_bulk_rows()` (~70% menos RAM por ciclo de prewarm)

2. **`_products_cache` nunca limpiaba entries expirados** → `_cleanup_memory_caches()` elimina entries con >2× TTL de antigüedad

3. **`_bm_stock_cache` crecía sin límite** → capeado a 12,000 entries; elimina los más viejos si se excede

4. **GC forzado después de cada ciclo** → `gc.collect()` libera objetos temporales del prewarm inmediatamente

### Hook
`_cleanup_memory_caches()` llamado al final de cada ciclo de `_startup_prewarm` (~cada 15 min)

---

## 2026-04-27 — FEAT: Distribución de stock multi-cuenta con reglas por cuenta

### Feature
Sistema completo para controlar qué porcentaje del stock BM expone cada cuenta de MercadoLibre, con modo normal y modo escasez basado en días de supply.

### Componentes (commit e300b2a)

**DB (token_store.py):**
- `account_stock_rules`: prioridad, pct_full (≥ umbral), pct_scarce (< umbral), scarce_enabled por cuenta
- `stock_distribution_settings`: umbrales globales (unidades=10, días=7, buffer=2)
- `get_account_sold_history()`, `get_sku_sales_by_account()` para excepción histórica y score

**Lógica prewarm (main.py):**
- `_dist_apply_pool()`: aplica pct según mode, con safety_buffer siempre retenido en BM
- Excepción histórica: si cuenta tiene scarce_enabled=False pero vendió el SKU antes → habilitada con mínimo 20%
- `_apply_bm_stock()` ahora produce `_days_supply`, `_is_scarce`, `_bm_avail_raw` en cada producto
- Prewarm fetcha rule + settings + sold_history antes de llamar a `_apply_bm_stock`

**API:**
- `GET /api/distribution/rules` — lista reglas de todas las cuentas
- `POST /api/distribution/rules/{user_id}` — upsert regla de cuenta
- `GET/POST /api/distribution/settings` — umbrales globales
- `GET /api/distribution/sku-score?sku=XXX` — ventas por cuenta + recomendación

**UI:**
- `/distribucion` — nueva página con sliders por cuenta, badge suma total (rojo si >105%)
- Columna días de supply en tablas de alertas de stock
- Badge ⚡ESCASEZ en SKUs en modo escasez
- Alerta urgente si days_supply < 3 días en stock crítico
- Enlace en nav (solo admins)

### Comportamiento por defecto
Si una cuenta no tiene regla configurada → `pct_full=1.0` (100% del stock, comportamiento legacy sin cambios).

---

## 2026-04-27 — FIX: Concentración de inventario no zeroaba otras cuentas (combo SKU)

### Bug
Al concentrar un SKU publicado como combo (ej. "SNTV003363 / SNWM000001"), el preview y el execute recibían el SKU completo con el slash, lo que hacía que `search_all_items_by_sku` solo encontrara la cuenta que tenía ese combo exacto — las demás cuentas con `SNTV003363` solo no aparecían y no se zeroaban.

### Síntomas
- Mensaje verde confirmando concentración pero las otras cuentas seguían con stock activo
- SKU desaparecía de alertas pero sin efecto real

### Fix (commit 12f2548)
- `preview` endpoint (`GET /api/stock/concentration/preview`): `sku = sku.split("/")[0].strip()` antes de llamar a `preview_concentration`
- `execute` endpoint (`POST /api/stock/concentration/execute`): mismo split
- `concentrateItem` JS: split en frontend también, antes de hacer fetch al preview
- UI confirmación: reemplazado `d.total_bm_avail` (undefined) → `bmAvail || d.total_stock`
- UI resultado: separar cuentas-otras zeroeadas vs duplicados del ganador

---

## 2026-04-27 — FIX: Gap scan "SKUs sin publicar en ML" retornaba 0 resultados

### Bug
Sección "SKUs sin publicar en ML/Amazon" mostraba 0 gaps en todas las cuentas.

### Causa raíz
`_bm_fetch_all_skus_with_stock` en `lanzar.py` paginaba `Get_GlobalStock_InventoryBySKU`
con `CONCEPTID=8`, que estaba bloqueado (rate-limiting por peticiones simultáneas) y
retornaba `[]` en todas las páginas → 0 SKUs BM → 0 gaps.

### Fix (`app/api/lanzar.py` — commit 1d1df47)
- `_bm_fetch_all_skus_with_stock` reemplazada: ya no pagina `Get_GlobalStock_InventoryBySKU`.
- Ahora hace 1 POST a `ConfColumns_Conditions_Excel` (igual que catalog sync), filtra
  `TotalQty > 0`, retorna misma estructura que espera el gap scan.
- `_BM_USER` default → `Carlos.Herrera@mitechnologiesinc.com` (cuenta dedicada app).

---

## 2026-04-25 — FIX: Catalog sync 0 precios por CONCEPTID incorrecto

### Bug
`_sync_bm_product_catalog` corría OK (1,550 SKUs, ~345s) pero retornaba **0 con precio**.

### Causa raíz
`_fetch_one` usaba `_GS_BASE_PAYLOAD` que tiene `CONCEPTID=8` y `LOCATIONID=None`.
El endpoint `Get_GlobalStock_InventoryBySKU` solo retorna `LastRetailPricePurchaseHistory`
cuando se llama con `CONCEPTID=1` + `LOCATIONID="47,62,68"` (igual que el bulk).

### Fix (`app/main.py` — commit 99a19f6)
- `_fetch_one` ahora construye payload inline con `CONCEPTID=1`, `LOCATIONID="47,62,68"`,
  `SEARCH=sku`, `NEEDRETAILPRICEPH=True` — mismo formato que el bulk pero para 1 SKU.
- Eliminado import de `_GS_BASE_PAYLOAD` (ya no se usa en catalog sync).

---

## 2026-04-25 — FEAT: Catálogo BM semanal + prewarm 10 min + VS REF% desde DB

### Problema
`LastRetailPricePurchaseHistory` no viene en bulk BM (SEARCH="" lo ignora).
VS REF% siempre mostraba "—". No podemos hacer 50+ llamadas individuales por
request (ya somos el usuario #1 en hits de BM con 8,169/día).

### Solución
- **`bm_product_catalog`** — nueva tabla SQLite: sku, retail_ph, brand, model, title
- **`_sync_bm_product_catalog()`** — descarga info de todos los SKUs del bulk cache
  con concurrencia=3. Guarda en DB. Actualiza cache en memoria inmediatamente.
- **Cron semanal** — domingo 9pm Monterrey (02:00 UTC lunes). 1 corrida/semana.
- **`_load_catalog_from_db()`** — al arrancar la app carga DB → `_bm_retail_ph_cache`
  en memoria. VS REF% funciona desde el primer request tras deploy.
- **`_BM_RETAIL_PH_TTL`** — subido a 7 días (fuente real es DB, no prewarm).
- **Prewarm** — ciclo normal bajado de 20 min a **10 min**.

### Archivos modificados
- `app/services/token_store.py` — tabla + helpers (upsert_bm_catalog_batch, get_bm_catalog_all)
- `app/main.py` — _sync_bm_product_catalog, _load_catalog_from_db, cron semanal, prewarm 10min

---

## 2026-04-25 — FIX: Concentrar — stock correcto al ganador + error check

### Problema
El botón "Concentrar" analizaba correctamente pero no asignaba el stock correcto al ganador.

### Causa raíz
- `concentrateItem`: enviaba `total_stock: d.total_bm_avail` pero el preview response no tiene ese campo (tiene `total_stock`). JS convierte `undefined` en omisión → backend recibe `total_stock=0` → ganador queda en 0.
- `bulkConcentrateCritical`: mismo bug con `d.total_bm_avail`.
- Check de éxito: solo revisaba `res.ok` (HTTP status) no `res.data.ok` → errores de negocio se mostraban como "OK".

### Fix (`products_stock_issues.html`)
- `concentrateItem`: `total_stock: bmAvail` (parámetro ya presente en la firma de la función, viene de `_bm_avail` del producto en BM)
- `bulkConcentrateCritical`: `total_stock: s.avail` (campo ya presente en el array de SKUs)
- Check de éxito: `if (!res.ok || !res.data.ok)` para capturar errores de negocio

---

## 2026-04-24 — FEAT: Planeación — Tendencia, ABC, vs Ref%, Stock Detenido

### Cambios

**Backend** (`app/main.py` — `planning_coverage`):
- Fetch `usd_to_mxn` sin nuevo endpoint: usa `_manual_fx_rate` override → ML API → fallback 20
- Lee `retail_ph_map` del `_bm_bulk_gr_cache` en memoria — sin llamadas directas a BM
- Calcula por SKU: `retail_ph_usd`, `avg_price_mxn` (revenue/units de órdenes ML), `recovery_pct`
- Expone `usd_to_mxn` en la respuesta para que el frontend pueda mostrarlo en tooltips

**Frontend** (`app/templates/planning.html`):
- **Badge ABC** en cada fila de cobertura: A≥1/día (rojo), B≥0.3 (amarillo), C<0.3 (gris)
- **Columna Tendencia**: ↑ si rate_7d > rate_30d×1.2, ↓ si rate_7d < rate_30d×0.8, → estable
- **Columna "vs Ref."**: `precio_prom_ml / (retail_ph_bm × TC) × 100%` — verde>120%, amarillo>80%, rojo<80%. Tooltip muestra precio absoluto en MXN.
- **Sección "Stock Detenido"** (bloque 4.5): aparece automáticamente al cargar cobertura si hay SKUs con `stock_bm>5 AND daily_rate<0.05`. Muestra acción sugerida según antigüedad: sin ventas 30d → liquidación, sin ventas 7d → revisar precio, muy lento → cupón digital.

### Archivos
- `app/main.py` — endpoint `/api/planning/coverage`
- `app/templates/planning.html` — tabla cobertura + sección stock detenido

---

## 2026-04-24 — AGENT: mercadolibre-strategist optimizado a versión Pro

### Cambios
Agente `mercadolibre-strategist` ampliado con 6 nuevas secciones (17-22):

- **17. WAR ROOM** — formato de output diario: top 50 SKUs → 5 acciones que mueven dinero. Criterios: stop bleeding → capture wins → fix leaks → plant seeds → clean house.
- **18. BULKY / TVs grandes** — decisión FULL vs Flex vs propio por tamaño de TV (32"→75"+). Fórmula de rentabilidad con flete BULKY. Reglas de stock mínimo en FULL por tamaño.
- **19. Cosas que casi nadie te dice de ML** — 10 lecciones de operación real no documentadas oficialmente (FULL ≠ 1er lugar, pausar ≠ perder historial, precio de referencia de 90 días, etc.).
- **20. Detección de stock detenido** — criterios (>5 uds, <1 venta/mes, >90 días), framework diagnóstico 4 pasos, árbol de decisión según antigüedad.
- **21. Explorador de oportunidades** — 5 tipos (A-E), score 0-100 con pesos, patrones de búsqueda web para detectar tendencias.
- **22. Score de salud de publicación** — 7 factores ponderados, umbrales para decidir cuándo hacer ads y cuándo optimizar primero.

### Archivo
- `.claude/agents/mercadolibre-strategist.md`

---

## 2026-04-24 — FEAT: Gral — Rendimiento por Cuenta (Ranking + Lanzamientos + Score)

### Cambios
**1. Inv.Global oculto para no-admin**
- Nav desktop y móvil: `Inv.Global` solo visible cuando `dashboard_user.role == 'admin'`
- Operadores por cuenta solo ven sus propias secciones

**2. Panel "Rendimiento por Cuenta" en `/multi-dashboard`**
Nuevo bloque con 3 tabs, visible debajo de las Account Cards:

- **Ranking** — tabla con todas las cuentas ordenadas por revenue del período. Incluye barras proporcionales (la cuenta líder = 100%), % del total, órdenes y unidades. Fila de totales al final.

- **Lanzamientos** — nuevo endpoint `GET /api/dashboard/multi-account-launches` que consulta `ML /users/{uid}/items/search?status=active&date_created.from=...&date_created.to=...&limit=1` para obtener `paging.total` (solo 1 request por cuenta, sin paginar). Muestra ranking visual con barras.

- **Score de Actividad** — índice compuesto: ventas(50%) + lanzamientos(30%) + órdenes(20%). Normalizado al 100% del total de todas las cuentas. Muestra quién contribuyó más al negocio en el período.

### Archivos
- `app/main.py` — endpoint `/api/dashboard/multi-account-launches`
- `app/templates/multi_dashboard.html` — nuevo panel HTML + JS completo

---

## 2026-04-22 — FEAT: Facturación — régimen 616 auto-fill + campo Método de Pago

### Cambio
Dos mejoras en `/facturacion`:

**1. Régimen 616 auto-fill**
Al seleccionar régimen fiscal 616 (Sin obligaciones fiscales):
- RFC se llena automáticamente con `XAXX010101000` y queda `readOnly`
- Razón Social se llena con `PÚBLICO EN GENERAL` y queda `readOnly`
- CFDI se fuerza a `S01` (Sin efectos fiscales) y queda `disabled`
- Bloque de constancia fiscal se oculta (`hidden`) — no es obligatoria para régimen 616
Al cambiar a otro régimen, todos los campos se restauran y constancia vuelve a ser requerida.

**2. Campo Método de Pago**
Nuevo campo `metodo_pago` (PUE/PPD) en el formulario, en grid de 3 columnas junto a CP y Forma de Pago.
- PUE = Pago en una sola exhibición
- PPD = Pago en parcialidades o diferido

### Archivos
- `app/templates/factura_cliente.html` — JS `_onRegimeChange()`, select Método de Pago, validación
- `app/api/facturacion.py` — `METODOS_PAGO` constant, expuesto en `/catalogs`
- `app/services/token_store.py` — nueva columna `metodo_pago` en `billing_fiscal_data` + migration

---

## 2026-04-22 — FIX: BM conditions ICB/ICC solo para SNTV* (no fans, snacks, otros)

### Problema
`_bm_conditions_for_sku()` aplicaba `GRA,GRB,GRC,ICB,ICC,NEW` a todos los SKUs (regla genérica). Las condiciones ICB e ICC son específicas para TVs Samsung/Hisense que se venden como open-box. Otros productos (fans, snacks, etc.) no tienen ICB/ICC en BM y el fetch devolvía 0.

### Solución
- SKUs `SNTV*` → `GRA,GRB,GRC,ICB,ICC,NEW`
- Todos los demás → `GRA,GRB,GRC,NEW`

Archivos: `app/main.py`, `.claude/agents/binmanager-specialist.md`

---

## 2026-04-22 — OPERACION: Migración Coolify — exit 137 + health check + tokens ML

### Contexto
Se levantó segundo ambiente en Coolify (`ecomops.mi2.com.mx`) como ambiente de pruebas. Railway sigue siendo el principal para todo el equipo.

### Problema 1: exit 137 al iniciar (Coolify mataba el contenedor)
`lifespan()` hacía decenas de llamadas HTTP (seed tokens, Amazon, BM cache, price recalc) antes del `yield`. Coolify tenía un timeout de startup y mandaba SIGKILL antes de que uvicorn emitiera "Application startup complete".

**Fix**: `yield` inmediato (<2s). Todo el trabajo pesado movido a `asyncio.create_task(_deferred_init())` que corre en background después de que el servidor ya está sirviendo.

```python
asyncio.create_task(_deferred_init())  # non-blocking
# periodic loops (non-blocking)...
yield  # uvicorn ready en <2s
```

### Problema 2: health check 404
AuthMiddleware interceptaba `/health` y devolvía redirect al login. Coolify marcaba el servicio como unhealthy.

**Fix**: nuevo endpoint `/api/ping` agregado a `_AUTH_EXEMPT`, siempre retorna `{"ok": True}`. Amir configuró Coolify para usar `/api/ping`.

### Problema 3: "Sesion no disponible" — tokens ML expirados
Los tokens en la DB de Coolify eran copia de Railway y expiraron (ML rota refresh tokens en cada uso). `_seed_tokens()` no refrescaba cuentas ya existentes.

**Fix**: `_seed_tokens()` detecta tokens expirados via `token_store.is_token_expired()` y los refresca con el RT disponible (env var tiene prioridad sobre DB).

### Resultado
Dashboard operativo en `ecomops.mi2.com.mx`. Cuentas conectadas via `/auth/connect`.

Archivos: `app/main.py`

---

## 2026-04-22 — FIX: Sync variaciones usaba conditions incorrectas y bulk cache equivocado (commit 5407251)

### Problema
Al hacer Sync BM en listings con variaciones, algunas variaciones recibían stock incorrecto — especialmente TVs (SNTV) con stock en condición ICB/ICC recibían 0, y bundles con un componente sin respuesta de BM recibían el stock del componente sano.

### Causa raíz (3 bugs)
- **Bug 1**: `_query_bm_avail` llamaba `get_available_qty(sku)` con conditions default `GRA,GRB,GRC,NEW`. Para SNTV con stock ICB/ICC, BM devuelve 0 (no encuentra GR stock).
- **Bug 2**: `_bulk_avail_map` usaba `_bm_bulk_gr_cache or _bm_bulk_all_cache` → siempre el GR bulk aunque el SKU fuera SNTV. Cache miss → caía a HTTP fallback con Bug 1.
- **Bug 3**: bundle `SKU_A / SKU_B` donde SKU_B daba error (-1): `min(valid_avails)` solo consideraba SKU_A (ignorando el error) → bundle recibía stock de SKU_A cuando debería ser 0.

### Solución
- **Fix 1**: `_query_bm_avail(sku, conds)` — pasa `conditions_primary` calculado del SKU completo de la variación
- **Fix 2**: mapas separados `_bulk_avail_map_gr` / `_bulk_avail_map_all`; cada variación elige el mapa según `_bm_conditions_for_sku(v_sku)`
- **Fix 3**: si algún componente del bundle retorna -1 → `bm_avail=0` (safe, no sobre-venta)

Archivos: `app/main.py`

---

## 2026-04-22 — FIX: Concentración no actualizaba DB ni cache en cuentas perdedoras (commit 0526b68)

### Problema
Después de ejecutar "Concentrar", ML API recibía qty=0 para los losers y el nuevo stock para el winner correctamente. Pero otros usuarios seguían viendo los losers con inventario activo en Stock Crítico porque `ml_listings` DB y `_stock_issues_cache` no se actualizaban.

### Causa raíz
`execute_concentration` retornaba el resultado directamente sin post-processing. `stock_concentration_execute_api` tampoco hacía nada después de recibir el resultado — no limpiaba cache, no actualizaba DB, no registraba en `_synced_alert_items`.

### Solución
En `stock_concentration_execute_api`, después de `execute_concentration` exitoso y `dry_run=False`:
1. `update_ml_listing_qty(loser_item_id, 0)` para cada loser OK
2. `update_ml_listing_qty(winner_item_id, total_stock)` para el winner
3. `_stock_issues_cache.clear()` — invalida para todos los usuarios
4. `_synced_alert_items[item_id] = timestamp` para winner + losers (excluye 10 min)

Archivos: `app/main.py`

---

## 2026-04-22 — FIX: Items sincronizados siguen visibles para otros usuarios (commit 9807cff)

### Problema
Cuando Usuario A hacía Sync en Reabastecer, el item desaparecía para A pero Usuario B seguía viéndolo como pendiente hasta el siguiente ciclo de prewarm (~30 min). Mismo problema en Activar, Crítico, Oversell y Stock Alerts.

### Causa raíz
`_synced_alert_items` era un `set` sin TTL. Solo filtraba `stock_alerts` — las otras 4 listas (restock, activate, critical, oversell_risk, full_no_stock) no lo consultaban. Además, al ser un set sin expiración, los items sincronizados quedaban excluidos permanentemente hasta reinicio del servidor.

### Solución
- `_synced_alert_items`: `set` → `dict[item_id, timestamp]` con TTL de 10 min
- Filtro `_synced_ids` aplicado a las 5 listas en `_do_prewarm`
- Limpieza automática de entradas expiradas en cada ciclo
- `stock_alerts` actualizado para respetar el TTL

Resultado: item desaparece para **todos** los usuarios inmediatamente tras el sync. Reaparece automáticamente en 10 min si no fue actualizado en ML (safety net).

Archivos: `app/main.py`

---

## 2026-04-22 — FIX: KPIs Stock tab todos en 0 por bulk BM retornando vacío sin excepción (commit a61088a)

### Problema
Todos los KPIs del tab Stock (Sin Stock, Revenue Perdido, Oportunidad Activar, Stock BM Disponible, Stock Crítico) mostraban 0 para todas las cuentas. Único KPI con valor: "Riesgo Sobreventa: 90" — pero eran falsos positivos.

### Causa raíz
`_get_bm_stock_cached` tiene 3 caminos para poplar `result_map` con datos BM:
1. Stale bulk cache (si `age < 900s` o BM detectado caído)
2. Fresh bulk fetch → actualiza cache → usa datos frescos
3. Fallback a stale en `except Exception`

El fallo ocurrió cuando el bulk cache tenía `age=27567s` (7.6h, > TTL de 900s) y BM respondía al health check (consecutive_failures < 2) pero `get_bulk_stock()` devolvía `None`/`[]` sin lanzar excepción. En ese caso:
- Condición 1: falsa (`age >= 900` y no `_bm_is_down_now`)
- Condición 2: `if _fresh_gr:` → False → `_bulk_gr_rows` queda None
- Condición 3: `except Exception` → no se ejecuta
- Resultado: `_used_bulk=False`, `result_map` vacío → `_apply_bm_stock({})` → `_bm_avail` nunca asignado → todos los KPIs con filtro `_bm_avail > 0` dan 0.

Los 90 falsos positivos de oversell: `_apply_bm_stock` para variaciones siempre escribe `p["_bm_avail"]=0` aunque `bm_map` esté vacío (el `else` del loop de variaciones asigna la key al padre), lo que bypasaba el guard `"_bm_avail" in p` del filtro de oversell.

### Solución (4 fixes en un commit)
- **Fix B1**: `else` clause para `if _fresh_gr` — cuando bulk GR retorna falsy sin excepción, usa stale + incrementa `consecutive_failures`
- **Fix B2**: mismo `else` para bulk ALL (`_fresh_all` falsy → stale de `_bm_bulk_all_cache`)
- **Fix B3**: `if not _used_bulk:` — en lugar de solo loggear, itera `to_fetch` y sirve `_bm_stock_cache` per-SKU aunque esté expirado (último recurso)
- **Fix B4**: `_apply_bm_stock` variaciones — flag `_any_inv_found`; solo asigna `p["_bm_avail"]` al padre cuando al menos una variación tuvo dato BM real (previene falsos positivos de oversell cuando bm_map está vacío)

Archivos: `app/main.py`

---

## 2026-04-21 — FIX: Planeación mostraba listings con SKU como "sin SKU" (commit 8b3bd42)

### Problema
La sección "Listings sin SKU" en Planeación incluía items que sí tenían SKU en ML (e.g. MLM4618869888 con SKU SNTV007615). El usuario lo detectó al verificar el listing directamente en ML.

### Causa raíz
`get_cached_skus` solo se invocaba para `top_ids[:100]` (los 100 items más vendidos). Items fuera del top 100 nunca recibían su SKU aunque estuvieran en `item_sku_cache`. La segunda fuente disponible (`ml_listings`, sincronizada cada 3 min) ni siquiera se consultaba.

Datos al momento del fix:
- `item_sku_cache`: 7,987 items con SKU
- `ml_listings`: 13,835 items con SKU
- Items en `ml_listings` con SKU pero sin entrada en `item_sku_cache`: **6,334**

### Solución
3 fuentes en cascada, todas locales (sin llamadas ML extra):
1. **Step 1a** — `item_sku_cache` consultado para **todos** los items (no solo top 100)
2. **Step 1b** (nuevo) — `ml_listings` como fallback para items aún sin SKU → resuelve los 6,334 restantes
3. **Step 2** — live ML API fetch sin cambios, solo para top 100 que ninguna fuente local pudo resolver

Archivos: `app/main.py`, `app/services/token_store.py` (nueva función `get_skus_from_listings`)

---

## 2026-04-21 — FEAT: orphan badge en navbar + banner por cuenta en Productos (commit 69e19ce)

### Problema
Las alertas de listings eliminados solo eran visibles en la página de Stock Sync. El usuario necesitaba visibilidad inmediata desde cualquier parte del dashboard, con aislamiento estricto por cuenta (no mostrar listings de una cuenta en otra).

### Solución
- **`base.html`**: badge rojo `#orphans-nav-badge` junto al link "Productos" en el navbar. Polling cada 2 min via `GET /api/listings/orphans`. Mismo patrón que el badge de salud existente.
- **`items.html`**: banner de alerta `#orphans-banner` (rojo, antes de la barra de alertas) filtrado **estrictamente por cuenta activa** usando `{{ active_user_id }}`. Fetch a `/api/listings/orphans?platform=ml&account_id=<uid>` — nunca muestra datos de otra cuenta. Incluye link "Ver y limpiar →" a `/stock-sync` y botón de dismiss.

---

## 2026-04-21 — FEAT: detección y limpieza de listings eliminados (commit 67d5d92)

### Problema
Listings eliminados de ML/Amazon seguían en la DB local indefinidamente. El sync solo hacía upserts, nunca deletions.

### Solución
- Nueva tabla `orphan_listings (platform, account_id, item_id, title, sku, detected_at)` con UNIQUE constraint
- Detección automática al final de cada **full sync** ML y Amazon:
  `orphans = set(item_ids en DB) - set(item_ids devueltos por API)`
- La detección limpia y re-genera la lista por cuenta en cada sync (siempre fresca)
- `GET /api/listings/orphans` — lista filtrable por platform/account_id
- `DELETE /api/listings/orphans` — body `{ids:[...]}` elimina de `orphan_listings` + `ml_listings`/`amazon_listings`
- UI en "Listings en cache":
  - Badge rojo **"N Eliminados"** aparece automáticamente si hay huérfanos
  - Modal con tabla (checkbox por fila + select-all)
  - Botón "Eliminar seleccionados de DB" con confirm() de confirmación
  - Badge se refresca al cargar página y al terminar Sync Listings

---

## 2026-04-21 — FIX+FEAT: BM sync log + botón sync por cuenta (commit 4bc416f)

### Fix: BM sync log "Sin datos"
- Causa raíz: `token_store.save_bm_stock_cache` no existía — función real es `upsert_bm_stock_batch`
- El `AttributeError` era capturado por `except Exception: pass` externo, impidiendo que `log_bm_sync_event` corriera
- Solución: separar el bloque de persistencia del bloque de log — el log ahora corre siempre, independiente de errores en el save

### Feat: Sync por cuenta individual
- Nueva función `run_single_account_stock_sync(platform, account_id)` en `stock_sync_multi.py`
  - Mismo circuit-breaker BM que el sync global
  - Recopila listings solo de esa cuenta
  - Actualiza únicamente la entrada de esa cuenta en `_last_sync_per_account`
- Nuevo endpoint `POST /api/stock/multi-sync/trigger-single` (body: `{platform, account_id}`)
- Botón **Sync** por fila en la tabla "Estado por cuenta"
  - Spinner visual mientras corre (botón deshabilitado con "...")
  - Polling cada 3s hasta que termina
  - Toast de confirmación al completar
  - Refresca la tabla automáticamente

---

## 2026-04-21 — FEAT: paginación 10 filas/página en tabla de Facturación (commit 5de64f2)

### Cambio
Tabla de solicitudes de facturación mostraba todos los resultados sin paginar (hasta 71 filas visibles simultáneamente).

### Solución
- `_allRows` guarda la respuesta completa del API
- `_renderFacPage(page)` renderiza el slice correcto (10 filas/página)
- Controles: ← Anterior / botones de página numerados (ventana ±2) / Siguiente →
- Info: "Mostrando X–Y de Z"
- Barra oculta si total ≤ 10 resultados
- Al cambiar filtros/sort se resetea a página 1 automáticamente

---

## 2026-04-21 — FIX: falsas alertas "Riesgo Sobreventa" por bulk miss BM (commit aacd186)

### Problema
8 SKUs (SHIL000098, SNTV007040, SNTV004196, SNWA000001, SNPE000295, SNSB000015, SNFN000930,
SNTY000018) mostraban "Riesgo Sobreventa" con BM=0 aunque BM tenía stock real. SHIL000098
confirmado: 67 unidades en BM (MTY:57, CDMX:10).

### Raíz del bug
En el path de bulk fetch (>30 SKUs — siempre en producción), `_lookup_diag` retornaba `(0,0)`
cuando un SKU no aparecía en el bulk de BM. Se llamaba `_store_wh(avail_ok=True)` aunque el
SKU simplemente no estaba en el bulk — no porque BM confirmara 0 stock. Consecuencia:
- `verified = avail_ok = True` → `_v=True`
- Cache: `{avail:0, _v:True}`
- `_cache_is_valid` → True (total=0, avail=0 pero _v=True)
- Stale retry: `_v=True` → no se retomaba
- `_store_wh` con `verified=True` → entraba en `result_map` con avail=0
- `_apply_bm_stock` seteaba `_bm_avail=0` → falso positivo permanente

### Solución (2 fixes)

**Fix 1** — Distinguir "no en bulk" de "BM confirmó 0":
- Nueva estructura `_bulk_miss_set: set` — SKUs no encontrados en bulk
- `_lookup_diag`: cuando `rows_to_sum` vacío, agrega a `_bulk_miss_set` (≤50)
- Main loop: `avail_ok=(_fsku not in _bulk_miss_set)` — bulk miss → `avail_ok=False`
- Con `avail_ok=False`: `verified=False` → `_v=False` → no en result_map → `_bm_avail` no seteado → **ninguna alerta oversell**
- Fix A (existente) previene sobreescribir entradas buenas `{avail:67, _v:True}`
- Stale retry detecta `_v:False` → per-SKU → retorna valor correcto

**Fix 2** — Stale retry cap: 30 → 100 SKUs (evita dejar bulk misses sin resolver)

### Nuevo diagnóstico
`_bm_bulk_stats` ahora incluye:
- `zero_in_bulk`: SKUs encontrados en bulk con AvailableQTY=0 (BM confirmó 0 — correcto)
- `not_in_bulk`: SKUs no en bulk → retried per-SKU (los problemáticos)
- `zero_skus`: ahora solo los `not_in_bulk_skus` (más accionables)
- Panel UI Sync Stock muestra split "BM confirmó 0 | No en bulk (retry)"

### Flujo post-fix
- Bulk miss → `_v:False` → no en result_map → sin alerta
- Stale retry (10s después, bg) → per-SKU → `{avail:67, _v:True}` en cache
- Siguiente prewarm: Fix A preserva `{avail:67}` si bulk sigue sin incluirlo

---

## 2026-04-20 — FIX: stock_issues_cache persiste en SQLite — sobrevive deploys Railway (commit 042ccc6)

### Problema
Cada deploy en Railway mata el proceso Python → todos los caches en memoria se pierden →
al reiniciar, el Stock tab muestra "Calculando stock en background..." durante 30-600s o
"Datos de inventario no disponibles" si el prewarm fallaba.

### Solución
- **`token_store.py`**: nueva tabla `stock_issues_cache (cache_key PK, ts, data_json, saved_at)`
- **`save_stock_issues_snapshot(key, ts, data)`**: serializa el resultado del prewarm a JSON + upsert en DB
- **`load_all_stock_issues_snapshots()`**: carga todos los snapshots al arrancar → dict[key, (ts, data)]
- **`main.py` lifespan**: `_load_stock_issues_from_db()` llamado antes del prewarm → popula `_stock_issues_cache` desde DB inmediatamente
- **`main.py` `_do_prewarm()`**: al terminar, guarda el nuevo snapshot en SQLite

### Resultado
Post-deploy: Stock tab muestra datos del último prewarm instantáneamente (badge "stale" existente indica actualización en curso). El prewarm refresca en background y sobrescribe con datos frescos.

---

## 2026-04-20 — PERF: Gap scan usa ml_listings DB — elimina ~1000+ llamadas ML API (commit 380dd1a)

### Problema
La página "No Lanzados en ML" tardaba ~2 min por scan porque Phase 1 llamaba la ML API
para cada cuenta (item IDs + item details) y Phase 2b verificaba cada SKU candidato via
seller_sku search (hasta N_SKUs × N_cuentas × 3 llamadas HTTP por SKU).

### Solución
- **`token_store.py`**: migración `base_sku TEXT DEFAULT ''` en `ml_listings` + índice
  `(account_id, base_sku)`. `upsert_ml_listings` ahora computa
  `base_sku = normalize_to_bm_sku(sku)` al insertar.
- **Nueva función `get_ml_listings_for_gap_scan(account_id)`**: lee DB y devuelve
  `(skus_set, inactive_map, active_prices_map)` — misma estructura que `_get_meli_sku_set`
  pero sin ninguna llamada HTTP. Calcula quality_score desde `data_json`.
- **`lanzar.py` Fase 1**: reemplazada llamada a `_get_meli_sku_set` por
  `token_store.get_ml_listings_for_gap_scan`. Fallback a API solo si DB vacía para la cuenta.
- **`lanzar.py` Fase 2b**: eliminada completamente — la DB cubre active/paused/inactive,
  la verificación API ya no es necesaria.

### Resultado
Scan pasa de ~2 min a ~20s para cuentas con caché DB poblada. Los gaps "No Lanzados en ML"
son per-cuenta (un SKU publicado en Autobot sigue siendo gap para Lutema).

---

## 2026-04-19 — FIX: Correr ciclo — circuit breaker timeout, badge bm_down, btn ID (commit 9aa8aec)

### Problema
Al presionar "▶ Correr ciclo" en Sync Stock, el ciclo mostraba "Iniciando..." y terminaba
inmediatamente con 0 SKUs, 0 updates, badge "Completado" — sin procesar nada.

### Causas raíz (3 bugs independientes)
1. **Circuit breaker timeout 5s** (`stock_sync_multi.py` línea ~668): BM responde lento (~10s).
   El probe `asyncio.wait_for(..., timeout=5.0)` siempre expiraba → sync abortaba con `status="bm_down"`.
2. **Badge Jinja2 y JS** (`stock_sync.html`): `status=="bm_down"` caía en el `else` → mostraba "Completado"
   en verde, sin indicar que el ciclo se había abortado.
3. **ID de botón incorrecto** (`stock_sync.html`): `pollStatus` y el bloque auto-start usaban
   `getElementById('btn-trigger')` pero el botón real tiene id `btn-run-now` → el botón nunca
   se rehabilitaba al terminar. También `_syncBtn` estaba indefinido.

### Fixes aplicados
- `stock_sync_multi.py`: timeout 5s → 20s (igual que health check)
- `stock_sync.html` Jinja2 badge: agrega caso `bm_down` → amber "BM caído"
- `stock_sync.html` `pollStatus` JS: maneja `bm_down` con badge amber + toast + mensaje en per-account
- `stock_sync.html`: corregido `btn-trigger` → `btn-run-now` en `pollStatus` y auto-start block
- `stock_sync.html`: reemplazado `_syncBtn` (nunca definido) con HTML inline del botón

---

## 2026-04-17 — FEAT: Preguntas AI — specs, historial mismo listing, cross-sell (commit f2c2aa0)

### Contexto de la mejora
La IA respondía sin conocer las especificaciones técnicas del listing ni si el
comprador ya había hecho preguntas sobre ese mismo producto. Tampoco podía
sugerir productos relacionados.

### Cambio 1: items_map enriquecido (`app/main.py`)
`items_map` ahora guarda `permalink` y `attributes` (resultado de `_extract_key_attributes`)
además de title/thumbnail/price/stock/seller_sku. Nuevo helper `_extract_key_attributes`
extrae hasta 20 specs de `body["attributes"]` omitiendo IDs de sistema (GTIN, SELLER_SKU, etc).

### Cambio 2: SimpleNamespace enriquecido (`app/main.py`)
Cada pregunta ahora lleva:
- `same_item_history`: Q&A anteriores respondidas del MISMO comprador en ESTE listing (max 5)
- `related_listings`: hasta 3 otros listings del mismo seller que coinciden por keyword con la pregunta
- `product_permalink`, `product_attributes`, `product_attributes_json`
- `same_item_history_json`, `related_listings_json`

### Cambio 3: UI historial mismo producto (`app/templates/partials/health_questions.html`)
Panel azul visible (siempre, sin accordion) encima del botón IA cuando existe
historial de este listing: muestra pregunta previa + respuesta del vendedor.
Botón IA recibe `data-permalink`, `data-attributes`, `data-same-item-history`, `data-related-listings`.

### Cambio 4: Payload JS (`app/static/js/health_ai.js`)
`suggestQuestionAnswer` incluye los 4 nuevos campos en el POST a `/api/health-ai/suggest-answer`.

### Cambio 5: API router (`app/api/health_ai.py`)
`suggest-answer` acepta y reenvía `product_permalink`, `product_attributes`,
`same_item_history`, `related_listings` a `build_question_answer_prompt`.

### Cambio 6: Prompt builder (`app/services/health_ai.py`)
- Sección de specs ML: lista hasta 20 especificaciones técnicas del listing
- Historial mismo listing tiene prioridad sobre historial general; instrucción explícita
  de NO repetir info ya respondida en este producto
- Cross-sell: listings relacionados incluidos en contexto solo si el comprador
  pregunta explícitamente por alternativas; la IA tiene instrucción de no mencionarlos
  de otra forma

---

## 2026-04-19 — FIX: Stock cache resilience cuando BM está caído (commit 1903ce5)

### Contexto
Cuando BM se caía temporalmente, el dashboard mostraba alertas falsas de oversell
y el banner de "Actualizando..." nunca paraba de girar. Los operadores no sabían
si los datos eran confiables ni a quién contactar.

### Cambio 1: `_STOCK_ISSUES_TTL` extendido a 1800s
El cache de stock issues ahora dura 30 min en lugar de 8. Los operadores trabajan
con datos del último prewarm del admin sin que expiren prematuramente.

### Cambio 2: Bulk cache GR+ALL sin expiración cuando BM caído
`_bm_bulk_gr_cache` y `_bm_bulk_all_cache` se reutilizan indefinidamente cuando
`consecutive_failures >= 2`, con log `[STALE-BM-DOWN]`. Si falla el fetch, hay
fallback automático al cache anterior en lugar de devolver vacío.

### Cambio 3: `_cache_is_valid` — TTL doble cuando BM caído (`app/main.py`)
La función interna `_cache_is_valid` (per-SKU) usa TTL efectivo de 30 min (doble)
cuando BM tiene 2+ fallos consecutivos. Evita re-intentar fetches individuales
que van a fallar por timeout y devolver 0 falso.

### Cambio 4: Stale banner inteligente (`app/templates/partials/products_stock_issues.html`)
- Cuando BM caído: para el polling, quita el spinner, cambia a color ámbar
- Si cache > 30 min: "Cache desactualizado — contacta al administrador para actualizar"
- Si cache reciente: "BinManager no disponible — mostrando datos del caché anterior"
- Límite de 5 reintentos máximo para no hacer polling infinito

---

## 2026-04-19 — FIX+FEAT: 4 mejoras definitivas de stock y permisos (commit 90e9b69)

### Fix 1: Bulk fallback TotalQty-Reserve
SKUs como SHIL*, SNMN*, SNAC* mostraban BM=0 porque el bulk de BM devuelve
`AvailableQTY=null` para algunos ítems (vs la consulta individual que sí lo computa).
`_lookup` ahora calcula `max(0, TotalQty-Reserve)` cuando `AvailableQTY=0`.
No toca el código de fetch de inventario — solo el helper de 6 líneas.

### Fix 2: Admin-only prewarm
`/dashboard`, `/items` y `products_stock_issues_partial` ya no disparan
`_prewarm_caches()` para operadores. Al cambiar de cuenta, operadores ven el cache
existente o mensaje "Datos no disponibles, contacta al administrador" si no hay cache.
Elimina el problema de "BinManager no responde" al cambiar de cuenta.

### Fix 3: SKU dual extraction documentada
`_get_item_sku` documenta explícitamente que NUNCA se reemplaza una fuente por otra —
siempre se encadenan las 4 fuentes ML como fallback en orden de prioridad.

### Fix 4: Panel de cobertura BM en Sync Stock
Después de cada bulk, `_bm_bulk_stats` registra cobertura completa: filas GR/ALL,
SKUs con stock, SKUs=0, fallbacks usados, lista de SKUs con 0.
El Sync Stock muestra el panel automáticamente tras completar el prewarm.

---

## 2026-04-18 — FIX: SNTV base SKUs mostraban BM=0 cuando stock era ICB/ICC

### Problema
SKUs SNTV sin sufijo explícito (ej. `SNTV003390`, `SNTV004117`) mostraban BM=0 aunque
BM tenía unidades en condición ICB/ICC. Causaba falsas alertas de sobreventa (21 items).

### Causa raíz (commit ceff49a)
`_bm_conditions_for_sku` solo devolvía `ALL` si el SKU contenía `"-ICB"`, `"-ICC"`, o `"/"`.
Para bundles como `"SNTV003390 / SNWM000001"`, `normalize_to_bm_sku` extrae los primeros
10 chars → `"SNTV003390"` — el `"/"` se pierde. Resultado: la función devolvía GR-only
y el lookup usaba `_bm_bulk_gr_cache`, que no tiene filas ICB/ICC.

### Fix (commit ceff49a)
`_bm_conditions_for_sku`: cualquier SKU que comience con `SNTV*` retorna ALL
(`GRA,GRB,GRC,ICB,ICC,NEW`). Los TVs pueden estar en cualquier condición
independientemente del formato del SKU en el listing.

---

## 2026-04-17 — FIX: SNWA000071 (y similares) mostraba stock ICB/ICC como vendible

### Problema
SKUs no-SNTV con unidades solo en condición ICB/ICC (ej. SNWA000071 = 25 unidades ICB)
aparecían como "Activar" en el dashboard con 25 unidades disponibles.

### Causa raíz (commit 1764ac3)
El bulk único con `GRA,GRB,GRC,ICB,ICC,NEW` retornaba la fila con el stock total incluyendo ICB/ICC.
El intento de post-filtrar con `r.get("Condition")` nunca funcionó porque BM retorna filas con
SKU-sufijo (ej. `SNWA000071-ICB`), no un campo `Condition` separado.
Al no haber ningún registro ICB/ICC en `_by_base_all`, todas las filas pasaban el filtro.

### Fix (commit 0209a98)
Reemplazado el único `_bm_bulk_cache` por dos caches separados:
- `_bm_bulk_gr_cache`  → `GRA,GRB,GRC,NEW` — para todo SKU no-SNTV-ICB/ICC
- `_bm_bulk_all_cache` → `GRA,GRB,GRC,ICB,ICC,NEW` — para SNTV-ICB/ICC/bundle

BM filtra server-side por CONDITION, por lo que no se necesita post-filtrar.
SNWA000071 ahora obtiene stock del `bulk_gr` (condición GR only) → 0 correcto.
SNTV con sufijo -ICB/-ICC o bundle "/" → usa `bulk_all` → incluye ICB/ICC stock.

### Archivos modificados
- `app/main.py`: `_bm_bulk_gr_cache` + `_bm_bulk_all_cache`, prewarm dual-bulk,
  `_enrich_with_bm_product_info`, variaciones, endpoints `/api/diag/sku` y `/api/diag/cache-health`

---

## 2026-04-16 — FIX: Stock BM = 0 en Inventario y Planeación para SKUs normales

### Problema
Las columnas "BM Disp." en Inventario ML y "Stock BM" en Planeación/Cobertura mostraban
**0 para la mayoría de SKUs** (ej. SNAC000046 con 1,423 unidades reales), marcándolos como "AGOTADO".

### Causa raíz
En `_get_bm_stock_cached()` el bulk fetch hace dos llamadas paralelas a BM:
- `bulk_gr` con condiciones `"GRA,GRB,GRC,NEW"`
- `bulk_all` con condiciones `"GRA,GRB,GRC,ICB,ICC,NEW"`

Para SKUs normales (sin sufijo -ICB/-ICC), el código buscaba **solo en `_exact_gr`**.
Si el SKU no aparecía en esos resultados (por paginación, variación en condiciones, etc.)
retornaba `(0, 0)` sin intentar buscar en `_exact_all`, donde el SKU sí existía.

### Fix (`app/main.py` líneas 3011-3015)
Agregado fallback: si la búsqueda en `_exact_gr` retorna `(0,0)`, se reintenta
con `_lookup(_exact_all, _by_base_all, _fbase)` antes de almacenar el resultado.

```python
_avail, _res = _lookup(_exact_gr, _by_base_gr, _fbase)
# Fallback: si no encontró en GR, buscar en ALL
if _avail == 0 and _res == 0:
    _avail, _res = _lookup(_exact_all, _by_base_all, _fbase)
```

### Impacto
Afectaba Inventario ML, Planeación/Cobertura, y cualquier otro widget que consuma `_bm_stock_cache`.

---

## 2026-04-16 — FEAT: Sistema de Auditoría por Usuario

### Descripción
Nuevo panel de auditoría que muestra actividad por usuario con vista de tarjetas
y detalle de timeline con filtros y paginación.

### Implementación
- **`app/services/user_store.py`**: `get_audit_users_summary()` y `get_audit_user_timeline()`
- **`app/api/users.py`**: 3 nuevos endpoints (`/api/users/audit/summary`, `/api/users/audit/user-timeline`, `/api/users/audit/user-stats`)
- **`app/templates/auditoria.html`**: Rediseño completo — tarjetas por usuario + detalle con KPIs y timeline
- **`app/api/items.py`**: Auditoría en 10 endpoints write de ML (price, stock, title, status, etc.)
- **`app/api/amazon_products.py`**: Auditoría en 2 endpoints write de Amazon
- **`app/api/lanzar.py`**: Auditoría en create_listing, reactivate, sync_price, mark_launched
- **`app/main.py`**: Auditoría en stock_concentration_execute

### 16 tipos de acción registrados
`ml_item_created`, `ml_item_reactivated`, `ml_mark_launched`, `ml_price_update`,
`ml_price_synced`, `ml_stock_update`, `ml_variation_stock`, `ml_title_update`,
`ml_status_update`, `ml_item_closed`, `ml_concentration`,
`amz_price_update`, `amz_listing_update`

---

## 2026-04-16 — FIX: Alerta bar mostraba "sin alertas activas" con 205 riesgos

### Problema
El banner de alertas mostraba "sin alertas activas" aunque había 205 productos con
riesgo de oversell. Al abrir la tab Stock se veían las alertas, pero el banner no refrescaba.

### Causa raíz
`loadAlertBar()` se llama al cargar la página, cuando `_stock_issues_cache` está vacío
→ retorna `riesgo=0`. La tab Stock popula el caché al cargarse, pero el banner no se actualizaba.

### Fix (`app/templates/items.html`)
Agregada una línea en `switchTab()`: cuando se carga exitosamente la tab `stock`,
se llama `loadAlertBar()` para refrescar el banner con el caché ya poblado.

---

## 2026-04-16 — FEAT: Modal "Ver lista" para productos sin SKU en Planeación

### Descripción
Botón "Ver lista" junto al aviso de N productos sin SKU excluidos en Planeación.
Muestra modal con tabla (título, item ID, unidades/30d, link a ML) y botón de copia TSV.

### Implementación
- Nuevo endpoint `GET /api/planning/no-sku-items` en `app/main.py`
- Botón y modal en `app/templates/planning.html`

---

## 2026-04-15 — FEAT: Tab "Sin BM" en ML y Amazon

### Descripción
Nueva sección disponible en ambas plataformas que muestra todos los listings
activos cuyo SKU no tiene registro en BinManager. Ayuda a identificar productos
que necesitan ser creados o corregidos en BM para tener trazabilidad completa.

### Implementación
- **ML**: `/productos/sin-bm` — nueva página bajo el subnav de Productos
  - Endpoint `GET /api/productos/sin-bm` en `app/api/productos.py`
  - Fetches todos los IDs activos (sin límite), luego detalles en batches de 20
  - Compara contra `get_bulk_stock()` de BM (1 sola llamada bulk)
  - Paginado 10 filas, búsqueda por SKU/título, filtro "SKU no en BM" vs "Sin SKU"
- **Amazon**: Tab "⚠️ Sin BM" en `/amazon/products`
  - Endpoint `GET /api/amazon/products/sin-bm` en `app/api/amazon_products.py`
  - Usa `_get_listings_cached()` + `get_bulk_stock()` en paralelo
  - Caché 15 min por seller_id, botón forzar recarga
  - Paginado 10 filas, búsqueda, link a Seller Central
- Ambas versiones aisladas por cuenta (no mezclan Lutema/Autobot)
- Motivo distingue: **"Sin SKU"** (campo vacío) vs **"SKU no en BM"** (no encontrado)

### Archivos nuevos
- `app/templates/ml_sin_bm.html`
- `app/templates/partials/ml_productos_sin_bm.html`
- `app/templates/partials/amazon_products_sin_bm.html`

---

## 2026-04-15 — FIX: BM DISP/BM RES siempre 0 en Amazon Inventario

### Problema
En la tab Inventario de Amazon, las columnas **BM DISP** y **BM RES** mostraban 0 para todos los SKUs,
aunque MTY/CDMX/TJ sí mostraban cantidades correctas.

### Causa raíz
`_enrich_bm_amz()` usaba el endpoint `GlobalStock_InventoryBySKU_Condition` y verificaba
`status == "Producto Vendible"` — pero ese campo **siempre retorna "Otro"** (bug de BM),
por lo que `avail` y `reserved` nunca sumaban nada.

### Fix (`app/api/amazon_products.py`)
- Reemplazado `_BM_COND_URL` + `cond_payload` con `_BM_INV_URL` + `stock_payload` (`CONCEPTID=1`)
- El endpoint `Get_GlobalStock_InventoryBySKU CONCEPTID=1` retorna `AvailableQTY` y `Reserve` directamente
- Parsing simplificado: buscar row con SKU == base, leer campos directamente (sin JSON anidado)
- Fallback al primer row si ninguno matchea exacto
- No se tocó código de ML en `main.py`

---

## 2026-04-15 — FEAT: Rediseño Amazon — misma estructura que MercadoLibre

### Cambios realizados
Amazon Dashboard rediseñado para tener la misma estructura visual y UX que la sección de MercadoLibre.

**Fase 1 — Stats cards** (`amazon_dashboard.html`):
- 4 tarjetas superiores: Activos, Inactivos, Suprimidos, Sin Stock / Low Stock
- Clickeables: llevan directo al filtro correspondiente en Operaciones
- Se cargan via `loadAmzStatsRow()` desde `/api/amazon/alerts`

**Fase 1 — Tab bar** (`amazon_dashboard.html`):
- Tabbar ahora dentro de card blanco `bg-white rounded-xl border overflow-hidden`
- Indicador activo: `border-b-2 border-orange-500 bg-orange-50 text-orange-700` (mismo estilo que ML pero en naranja)
- Todos los 7 tabs tienen el estado activo correcto (incluyendo fba, listings, deals que antes siempre aparecían inactivos)

**Fase 1 — Catálogo Operaciones** (`amazon_dashboard.html` + `amazon_products_catalog.html`):
- Reemplazado dropdown de filtro por tab bar al estilo ML: Todo | Activos | Inactivos | 🔴 Suprimidos
- Búsqueda inline con filtrado en cliente
- Contadores de estado en cada tab (`amz-cnt-all`, `amz-cnt-active`, etc.)
- Removidos filtros redundantes del partial `amazon_products_catalog.html`

**Fase 2 — Panel lateral** (`amazon_dashboard.html`):
- Panel deslizable desde la derecha (igual a ML)
- 5 subtabs: Info, Stock, Buy Box, Atributos, Imágenes
- Se abre al hacer click en cualquier fila del catálogo
- Buy Box hace lazy-load via API

**Fase 3 — JS externo** (`app/static/js/amazon_dashboard.js`):
- 1982 líneas extraídas de inline a archivo estático
- Template solo tiene 2 vars inline (`amzActiveTab`, `amzActiveSellerId`)
- HTML reducido de 2312 a 718 líneas

### Archivos modificados
- `app/templates/amazon_dashboard.html` (718 líneas, antes 2312)
- `app/templates/partials/amazon_products_catalog.html`
- `app/static/js/amazon_dashboard.js` (nuevo, 1982 líneas)

**Fase 4 — Separación Dashboard / Ventas** (commit `277f0ac`):
- Tab **Dashboard** (nuevo default `/amazon`): filtro de fechas, KPI metrics, meta diaria, gráfico de ventas
- Tab **Ventas**: morning briefing, fondos pendientes, comparativa multi-cuenta, últimas órdenes, top 10 productos
- `main.py`: `"dashboard"` agregado a tabs válidos; default de `/amazon` cambiado de `ventas` → `dashboard`
- `amazon_dashboard.js`: `loadAmzRecentOrders()` extraído de `loadAmazonDashboard()`; `switchAmzTab()` y carga inicial actualizados
- `base.html`: tab Dashboard agregado en nav desktop y mobile (grid 4+4); `/amazon` sin params → `/amazon?tab=dashboard`

---

## 2026-04-14 — FIX CRÍTICO: normalize_to_bm_sku en todos los lookups BM (7 ubicaciones)

### El problema
`_extract_base_sku` solo conoce sufijos estándar (`-NEW`, `-GRA`, `-GRB`, `-GRC`, `-ICB`, `-ICC`). SKUs con sufijos no estándar como `-NUEVO` o `(cantidad:2)` no se limpiaban correctamente, causando que BinManager retornara stock=0 y generando **falsas alertas de oversell** y **stock incorrecto** en todo el dashboard.

Ejemplos afectados:
- `SNPE000093-NUEVO` → BM recibía `SNPE000093-NUEVO` → 0 units → alerta falsa (real: 46 units)
- `SNHG000038 (cantidad:2)` → BM recibía `SNHG000038 cantidad:2` → 0 units → alerta falsa (real: 480 units)

### Root cause
Dos funciones auxiliares con el mismo bug:
1. `_extract_base_sku` en `main.py` y `sku_inventory.py` — tabla de sufijos incompleta
2. `_clean_sku_for_bm` — regex `\(\d+\)` solo removía paréntesis con dígitos puros

### Fix aplicado
Reemplazadas 7 llamadas a `_extract_base_sku` con `normalize_to_bm_sku` (que usa split en primer `-`/espacio → primeros 10 chars):

| Archivo | Función | Descripción |
|---------|---------|-------------|
| main.py | `_enrich_with_bm_base_data` | Fetch de precios + lookup en base_map |
| main.py | `_enrich_with_bm_stock` | Fetch warehouse + condiciones |
| main.py | warehouse-stock endpoint | Desglose MTY/CDMX/TJ |
| main.py | bm-cost endpoint | Costo/precio retail por item |
| main.py | `_run_global_scan` | Inventario global cross-cuenta |
| sku_inventory.py | `_fetch_sellable_stock` | Stock vendible en tab SKU |

También fijado `_clean_sku_for_bm`: regex `\(\d+\)` → `\([^)]*\)` para remover cualquier paréntesis.

### Archivos afectados
- `app/main.py` — commits `a207dbc`, `ff1469f`, `7cc5dce`
- `app/api/sku_inventory.py` — commit `7cc5dce`

### Acción requerida
Clic en **↺ Actualizar BM** para invalidar caché y que el sistema re-fetchee con el código corregido.

---

## 2026-04-14 — FEAT: Facturación admin — datos del pedido en modal detalle

### Qué se hizo
El modal de administración de facturación ahora muestra la sección **"Datos del pedido"** con todos los items de la venta: título del producto, SKU, cantidad y precio unitario. Al final de la lista se muestra el total del pedido. Aplica a todas las solicitudes existentes y nuevas que tengan `order_data` almacenado.

### Implementación
- `_renderDetail()` en `facturacion.html` — nueva sección entre la grilla de info y el link del cliente
- Itera `r.order_data.items[]` y renderiza tarjeta por producto con título, SKU (condicional), marca (condicional), modelo (condicional), cantidad y precio unitario
- Total del pedido en fila separada al pie

### Archivos afectados
- `app/templates/facturacion.html` — `_renderDetail()` — commit `14a7e85`

---

## 2026-04-14 — BUG CRÍTICO: item_sku_cache — SKUs múltiples por item se perdían

### El problema
SKUs como SNTV006296 aparecían en "Sin publicar" aunque la cuenta BLOWTECHNOLOGIES tenía 2 listings activos con ese SKU.

### Root cause
`item_sku_cache` tenía `item_id TEXT PRIMARY KEY` (solo 1 SKU por item). ML permite `seller_custom_field = "SNTV006296 / SNWM000001"` (dos SKUs combinados). El código hacía split correcto → 2 entries: `{item_id: MLM3637209388, sku: SNTV006296}` y `{item_id: MLM3637209388, sku: SNWM000001}`. Pero el segundo INSERT hacía `ON CONFLICT(item_id) DO UPDATE SET sku = SNWM000001`, sobreescribiendo el primero.

**Resultado:** En el siguiente scan, `MLM3637209388` ya estaba en cache con `SNWM000001` → no se re-fetcheaba → `SNTV006296` no entraba al `sku_set` de BLOW → false gap "Sin publicar".

### Fix
- `item_sku_cache` migrado a `PRIMARY KEY (item_id, sku)` — migración automática en `init_db()` que droppea la tabla antigua (datos corrompidos) y la recrea
- `save_skus_cache()`: `ON CONFLICT(item_id, sku)` en vez de `ON CONFLICT(item_id)` — ya no sobreescribe
- `get_cached_skus()`: retorna `{item_id: [sku1, sku2, ...]}` en vez de `{item_id: str}`
- `_get_meli_sku_set()`: comprehension actualizada para iterar listas
- Debug endpoint: reverse-map `cached_by_sku` actualizado

### Archivos afectados
- `app/services/token_store.py` — schema, migración, `get_cached_skus()`, `save_skus_cache()`
- `app/api/lanzar.py:378` — consumer de `get_cached_skus()` y debug endpoint

### Efecto post-deploy
Al arrancar, `init_db()` detecta el schema viejo y droppea la cache. El primer scan re-fetcha todos los items y popula correctamente con ambos SKUs por item. Los gaps falsos de SKUs combinados desaparecen.

---

## 2026-04-13 — BUG CRÍTICO: Pack_id vs Order_id en MeLi API

### El problema
Al buscar la orden `2000012456820431` desde el portal de facturación y desde el buscador del dashboard, el sistema devolvía "Orden no encontrada" aunque la orden sí existía en la cuenta Apantallate.

### Root cause
Lo que MeLi muestra en su dashboard (y lo que los compradores ven en sus pedidos) es un **PACK_ID**, no un ORDER_ID.

- `GET /orders/2000012456820431` → 404 (pack_id no funciona en este endpoint)
- `GET /packs/2000012456820431` → 200 con `orders[0].id = 2000015930795100`
- `GET /orders/2000015930795100` → 200 ✓ (el ORDER_ID real)

### Fix
Se agregó `resolve_order(display_id)` en `meli_client.py` que:
1. Intenta `GET /orders/{id}` primero (para order_ids reales)
2. Si 404 → intenta `GET /packs/{id}` → extrae `orders[0].id`
3. Llama `GET /orders/{real_order_id}`

### Archivos afectados
- `app/services/meli_client.py` — `get_pack()` y `resolve_order()` agregados
- `app/api/orders.py:33` — usa `resolve_order()` en lugar de `get_order()`
- `app/api/facturacion.py:124` — `_try_account()` usa `resolve_order()`
- `app/main.py:1240` — portal cliente `/factura/{token}/lookup` usa `resolve_order()`
- `app/main.py:4772` — buscador general de órdenes usa `resolve_order()`

### Aprendizaje
Documentado en `api-integration-specialist.md` — sección "Pack_id vs Order_id — TRAMPA CRÍTICA DE MELI". Todos los lookups de órdenes en el dashboard ahora pasan por `resolve_order()`.

---

## 2026-04-14 — FIX: Portal cliente Facturación — Amazon muestra SKU/precio/total

### Problema
Al buscar una orden Amazon desde el portal del cliente (`/factura/{token}`), el sistema mostraba "Pedido confirmado" pero sin descripción, SKU, precio unitario ni total. MeLi funcionaba correctamente.

### Root cause
- `factura_lookup_order()` solo ejecutaba la rama MeLi, ignorando plataforma Amazon
- `_build_order_summary()` solo parseaba el formato MeLi (`order_items`, `total_amount`, `date_closed`)
- El formato Amazon es completamente distinto: `AmazonOrderId`, `_items`, `OrderTotal`, `PurchaseDate`

### Fix
1. `_is_amazon_order_id()` — detecta automáticamente por regex `^\d{3}-\d{7}-\d{7}$`
2. `factura_lookup_order()` — si `platform == "amazon"`, usa `get_amazon_client()` + SP-API `/orders/v0/orders/{id}` y `/orderItems`; almacena ítems en `order["_items"]` y marca `order["_platform"] = "amazon"`
3. `_build_order_summary()` — rama Amazon extrae `Title`, `SellerSKU`, `ASIN`, `QuantityOrdered`, `ItemPrice.Amount` de cada item; extrae `OrderTotal.Amount` como total

### Archivos afectados
- `app/main.py` — `_is_amazon_order_id()`, `factura_lookup_order()`, `_build_order_summary()`

### Template
`factura_cliente.html` ya usaba `it.unit_price` y `summary.total` — no requirió cambios.

---

## 2026-04-14 — FEAT: Módulo de Facturación — portal self-service para clientes

### Qué se construyó
Portal completo para que los clientes soliciten su factura CFDI 4.0 sin intervención manual del equipo interno.

### Flujo
1. Admin crea solicitud en `/facturacion` → selecciona cuenta ML, plataforma, # de orden → obtiene link único (UUID)
2. Admin envía el link al cliente (copiar al portapapeles)
3. Cliente abre link → ingresa # de orden → sistema busca en ML y muestra resumen del producto
4. Cliente llena datos fiscales completos + sube Constancia Fiscal
5. Contabilidad ve solicitud en estado "Pendiente factura" → genera CFDI en su sistema → sube PDF
6. Estado cambia a "Factura lista" → cliente entra al mismo link → descarga PDF

### Archivos creados/modificados
- `app/services/token_store.py`: tablas `billing_requests`, `billing_fiscal_data`, `billing_invoices` + 10 funciones CRUD
- `app/api/facturacion.py`: router admin con catálogos SAT completos (15 CFDI, 19 regímenes, 21 formas de pago)
- `app/main.py`: rutas públicas `/factura/{token}/*`, ruta admin `/facturacion`, `/factura/` exento de auth
- `app/templates/facturacion.html`: dashboard admin con tabla filtrable, modales de creación y detalle
- `app/templates/factura_cliente.html`: página pública standalone (no hereda base.html), 3 estados visuales
- `app/templates/base.html`: link "◈ Facturación" en nav MeLi

### Campos del formulario del cliente
RFC, Razón Social, Régimen Fiscal, Uso CFDI, CP Fiscal, Forma de Pago, Email, Teléfono, Domicilio (opcional), Constancia Fiscal (upload PDF/imagen)

### Detalles técnicos
- Multi-cuenta: cada solicitud lleva `ml_user_id` — el lookup usa el token del seller correcto
- PDFs (factura + constancia) almacenados como BLOB en SQLite — sin dependencias externas
- Validación RFC (12-13 chars), CP (5 dígitos numéricos), campos requeridos en frontend y backend
- Constancia máx 5 MB; facturas máx 10 MB
- Admin puede eliminar solicitudes completas (cascada: datos fiscales + PDF)

---

## 2026-04-13 — FIX: Corte de día alineado con hora México (CST UTC-6)

### Problema
La tabla de ventas diarias cortaba el día a las 6 PM CDMX en lugar de medianoche. A partir de esa hora el dashboard mostraba "Hoy" vacío ($0.00) y el día actual sin etiqueta, porque:
1. `datetime.utcnow()` en defaults de fecha → a las 7 PM CDMX UTC ya es el día siguiente
2. Órdenes de 6 PM–medianoche CDMX se bucketean en fecha UTC (mañana), que no existe en el rango → se pierden
3. `new Date()` en JS usa `.toISOString()` (UTC) → `todayStr` incorrecto si navegador no es UTC-6

### Fix
- `metrics.py`: `now_mx = now - timedelta(hours=6)` para defaults de fecha
- `metrics.py`: fetch con `date_to+1` para capturar órdenes de noche México (igual que multi-account dashboard en main.py); `if date_key in buckets` filtra naturalmente el día extra
- `metrics.py`: bucketing convierte fecha de orden UTC→CST (`order_date_utc - timedelta(hours=6)`) antes de asignar `date_key`
- `dashboard.html`: `todayStr` y `setRange` usan `new Date(Date.now() - 6*3600*1000)` para obtener fecha CDMX via `.toISOString()`

### No tocado
- `meli_client.py` — offset `-00:00` en API call se compensa con el +1d trick
- `main.py` multi-account dashboard — ya estaba correcto con el mismo patrón

---

## 2026-04-13 — FIX: Aislamiento multi-cuenta — gaps, retornos y sync rules independientes por cuenta

### Problema
Audit completo reveló que varias operaciones mezclaban datos entre cuentas ML:
1. **Sin publicar (gaps)**: `global_meli_skus` era la unión de TODAS las cuentas. Un SKU publicado en Autobot quedaba excluido de "Sin publicar" en Lutema también. SNTV007841 (24 uds en MTY) no aparecía por este motivo.
2. **return_flags**: tabla sin `user_id` — flags de retornos eran globales entre cuentas.
3. **sku_platform_rules**: tabla sin `user_id` — reglas de sync visibles/modificables desde cualquier cuenta.
4. **Scan manual "Escanear ahora"**: corría para TODAS las cuentas aunque se iniciara desde Lutema.

### Fixes aplicados
- `lanzar.py _run_gap_scan`: `global_gaps_base` ahora incluye todos los BM SKUs (sin filtro global). El filtro se aplica per-cuenta usando `account_ml_data[user_id]["meli_skus"]`. FASE 2b verifica seller_sku solo contra la cuenta en cuestión → `verified_not_gaps_per_account`.
- `lanzar.py trigger_scan`: lee `_active_user_id` del ContextVar y pasa `user_id` al scan. Scan nocturno sigue siendo global (`user_id=None`).
- `token_store.py return_flags`: agrega columna `user_id` (con migración `ALTER TABLE`). Funciones `save/get/resolve_return_flag` ahora filtran por `user_id`.
- `token_store.py sku_platform_rules`: agrega `user_id` en schema y migración. `get_all_sku_platform_rules(user_id)` filtra por cuenta en UI; sin `user_id` sigue siendo global para el sync.
- Endpoints `/api/returns/*` y `/api/stock/multi-sync/rules`: pasan `_active_user_id` del ContextVar.

### Bugs resueltos en el proceso
- `NOT NULL` en `ALTER TABLE ADD COLUMN` no soportado en SQLite < 3.37 (Railway 3.31) → removido.
- `CREATE INDEX ON return_flags(user_id)` se ejecutaba antes del `ALTER TABLE` → reordenado.
- `NameError: total_gaps` en scan → renombrado a `total_gaps_before_verify`.

### Arquitectura multi-cuenta (resultado del audit)
El resto del dashboard (ventas, health, ads, productos, deals, planeación, Amazon) ya estaba correctamente aislado por cuenta mediante `ContextVar(_active_user_id)` + cookie `active_account_id`.

### Scan local vs. global (2026-04-13 — adición)
- `trigger_scan` (`/api/lanzar/scan-now`): escanea solo la cuenta activa (cookie `active_account_id`).
- Nuevo endpoint `/api/lanzar/scan-all`: escanea todas las cuentas (`user_id=None`). Solo accesible para `role=admin`.
- `lanzar_gaps.html`: botón "Escanear ahora" (amarillo) para cuenta activa. Botón "Scan Global" (púrpura) solo visible para admins. Ambos se re-habilitan al terminar polling.
- Root cause del scan all-accounts: `_nightly_gap_scan_loop` corría un scan inmediato 30s después del boot, bloqueando el `_scan_lock`. Removido — nightly loop solo corre en horario nocturno.

---

## 2026-04-12 — FEAT: PRE_NEGOTIATED promos visibles + ML contribution en ganancia

### Problema
MLM2517306551 (y similares) tiene una promo `PRE_NEGOTIATED` activa donde ML paga 6% del precio original. El dashboard no la mostraba como deal activo porque `_auto_types` en `_enrich_with_promotions` los filtraba. Además, el cálculo de ganancia no contabilizaba lo que ML subsidia.

### Fix
- `_enrich_with_promotions`: separa `active_seller` (PRICE_DISCOUNT/DEAL) de `active_auto` (PRE_NEGOTIATED/SMART). Si no hay seller promo, usar auto promo para `_has_deal=True`. Flag `_deal_is_ml_auto=True` identifica estos casos.
- Extrae `_meli_promo_pct` y `_seller_promo_pct` del objeto promo activo (ya existían en la API).
- `_calc_margins`: `_meli_contribution_mxn = original_price × meli_pct / 100`. Luego `_ganancia_real = ganancia_est + contribution` y `_margen_real_pct` usando precio efectivo.
- Template: badge "ML Auto" en azul + "+$XX ML" en ganancia column.
- JS `calcPromoMargin`: suma `meliContrib` a ganancia; `margen = ganancia_real / (dealPrice + meliContrib)`.

### Mecánica PRE_NEGOTIATED
- Seller lista a $799; ML aplica 6% descuento → cliente paga $751
- ML subsidia los $47.94 → vendedor efectivamente recibe ~$799 antes de comisión
- Comisión se cobra sobre deal_price ($751), no sobre original

---

## 2026-04-11 — FIX: Ganancia/Margen columnas — RetailPrice BM como costo fallback

### Root cause
- `_calc_margins()` usaba `_bm_avg_cost` (AvgCostQTY de BM) como único costo. Para la mayoría de productos, este campo es 0 → `_costo_mxn = 0` → `_ganancia_est = None` → columnas muestran `—`.

### Fix
- `_eff_cost = AvgCostQTY si >0, sino RetailPrice de BM`. El RetailPrice de BM = precio de adquisición (confirmado en comentario existente: "retail IS our acquisition cost").
- Agrega `_bm_eff_cost_usd` y `_cost_source` ("avg" | "retail" | None) por producto.
- Template muestra etiqueta "est." cuando costo viene de RetailPrice (no AvgCost), para informar al usuario.
- `data-bm-cost` en Deals tab ahora usa `_bm_eff_cost_usd` → calculadora JS correcta.
- Calculadora JS: reemplaza flat 17% de comisión por `mlFee(price)` escalonado (12-18% según precio).
- Aplica automáticamente a todos los endpoints que llaman `_calc_margins()`: Deals, Inventario, Top Sellers, etc.

---

## 2026-04-11 — FIX: Sony TVs (MLM1002) — family_name ES el título / listing live

### Root cause descubierto vía API directa
- Para MLM1002 (Televisores) en México, ML requiere `family_name` (campo raíz).
- **CUANDO family_name está presente, el campo `title` es INVÁLIDO** — ML lo rechaza con `body.invalid_fields: [title]`.
- Todos los intentos previos fallaban porque el payload tenía `title` + `family_name` simultáneamente.
- Después de crear el item: **"You cannot modify the title if the item has a family_name"** — el PUT de título también falla.
- `family_name` SE CONVIERTE en el título del listing en ML (con normalización de capitalización).

### Solución
- Intento 2 (family_name requerido): ahora elimina `title` del payload (`_p2.pop("title", None)`).
- Primer candidato = `title[:60]` (título del wizard) → ML lo usa directamente como título del listing.
- ML normaliza capitalización (ej. "Sony TV 4K" → "Sony Tv 4k") pero preserva el contenido.
- `ml_actual_title` = `result.get("title")` (del POST response, no de un PUT que ya falla).
- `title_warning` solo si los títulos difieren en contenido (ignorando mayúsculas).

### Nuevo endpoint
- `POST /api/lanzar/mark-launched/{sku}` — para marcar SKUs publicados fuera del wizard.

### Listing SNTV007911 publicado manualmente
- **ID**: MLM2858016657  
- **Título**: Sony Televisor Bravia 2 Led 4k Uhd Smart Google Tv 50 (wizard title normalizado)
- **URL**: https://articulo.mercadolibre.com.mx/MLM-2858016657-sony-televisor-bravia-2-led-4k-uhd-smart-google-tv-50-_JM

---

## 2026-04-10 — FIX: family_name rechazado aunque estaba en el payload

### Análisis del problema
- ML requiere `family_name` como identificador corto de línea de producto (NO texto libre largo).
- Enviábamos el título del draft (ej. "Televisor Westinghouse QLED 43 Pulgadas Smart TV Roku") como family_name → ML lo rechaza con `body.required_fields [family_name]` aunque el campo SÍ estaba en el payload.
- ML trata como "ausente" cualquier valor que no reconozca como identificador de familia.
- `_guessFamilyName` no tenía caso para Westinghouse → retornaba `''` → fallback era el título completo.

### Fix backend — Intento 2 restructurado (ciclo de candidatos)
Probamos en orden hasta que ML acepte:
1. model_body exacto: `"WR43QE2350"` (10 chars, muy específico)
2. prefijo del modelo: `"WR43"` (4 chars, extraído con regex `^([A-Za-z]+\d+)`)
3. brand_body: `"Westinghouse"` (12 chars)
4. brand + prefijo: `"Westinghouse WR43"` (17 chars)
5. family_name del frontend como último recurso
- Sale del loop al primer éxito, o si el error cambia (ya no es de family_name)

### Fix frontend — _guessFamilyName
- Nuevo caso: Westinghouse → extrae prefijo del modelo (WR43, etc.)
- Fallback universal: para cualquier marca no reconocida, extrae `[letters+digits]` del modelo (máx 8 chars)
- Último recurso: primera palabra de la marca

---

## 2026-04-10 — FIX: video 15s + title minimum_length ML

### FIX: Video solo duraba 15 segundos
- Claude generaba script de ~50-60 palabras (prompt decía "70-90").
- ElevenLabs a 140 wpm → ~50/140 × 60 = ~21s de audio → video cortado ahí.
- **Fix**: Prompt cambiado a MÍNIMO 100 palabras, máximo 120.
  → 100/140 = ~43s de audio → video siempre ≥40s sin importar ritmo del narrador.
- Enfatizado en mayúsculas "CRITICAL: under 100 words = video too short".

### FIX: ML item.title.minimum_length sigue fallando (intento 5)
- Agotados intentos 1-4, ninguno manejaba minimum_length.
- **Fix**: Intento 5: si ML devuelve minimum_length, enriquecer el título con brand + model
  hasta llegar a mínimo 25 chars descriptivos.
- Frontend ahora envía `brand` y `model` en el payload de create-listing para que el
  backend tenga los datos disponibles en este retry.

---

## 2026-04-10 — FIX: video 2 clips en loop + título corto al restaurar draft

### BUG: Video solo usaba 2 clips y los ciclaba
- `asyncio.gather` de 3 clips en paralelo: rate limiting en Replicate → solo 2 éxitos.
- 2 clips × 10s = 20s < audio 28s → `-stream_loop -1` rellenaba con loops visibles.
- Retry anterior solo disparaba si `len(clip_urls) == 1`, no con 2.
- **Fix A** (retry): loop while `len(clip_urls) < 3`, máx 3 reintentos secuenciales.
- **Fix B** (no-loop): `_xfade_and_combine` ahora estima duración de audio (`len(bytes)/bitrate`)
  y solo activa `-stream_loop` si video < audio - 1s. Con 3 clips ~30s vs audio ~28s: sin loop.

### BUG: Título corto de BM pasaba al publicar desde draft restaurado
- Draft guardado ANTES del fix del botón Next → tenía product_title "Westinghouse WR43QE2350" (22 chars).
- Al restaurar draft, el título corto llegaba a ML → `item.title.minimum_length`.
- **Fix**: en `_wizOpen`, si draft restaurado tiene título < 20 chars → tratar igual que sin draft
  (deshabilitar Next + auto-regenerar con IA).

---

## 2026-04-10 — FIX: Salir no redirigía al login + FAMILY_NAME bloqueaba publicación

### BUG: Botón "Salir" borraba nombres de cuentas pero no salía del dashboard
- Causa raíz: `auth.py` tiene `router = APIRouter(prefix="/auth")` con `POST /logout` → registrado como `POST /auth/logout`.
- FastAPI lo registra ANTES que el `@app.post("/auth/logout")` de `main.py` → el de auth.py gana.
- auth.py logout solo eliminaba tokens ML, NO la `dash_session` cookie.
- Al redirigir a `/login`, el middleware ve la cookie válida y manda de vuelta al dashboard.
- **Fix**: actualizar `auth.py` logout para aceptar `request: Request`, importar `user_store`,
  eliminar también la sesión del dashboard y borrar la cookie en la respuesta.

### BUG: Publicar bloqueado por FAMILY_NAME aunque el backend tiene fallback
- Frontend validaba `if (!_wiz.family_name)` y bloqueaba con error al usuario.
- El backend ya tiene `family_name = title[:60]` si llega vacío.
- **Fix**: quitar la validación dura. Agregar doble auto-fill antes de enviar:
  1. `_guessFamilyName(brand, model, title)` 
  2. Fallback: `draft.title.slice(0, 60)`

---

## 2026-04-09 — FIX: título corto de BM llegando a ML por race condition en wizard

### BUG: "Sony KD-50X85K" (14 chars) llegaba a ML en lugar del título IA aceptado
- Causa raíz: al abrir el wizard, `_wizGenDraft()` se auto-dispara con 400ms de delay y puede tardar 2-5s.
- Si el usuario clickeaba "Siguiente" antes de que terminara la API call, `wiz-f-title` todavía tenía el `product_title` corto de BM.
- `_wizNext()` tomaba ese valor y lo guardaba en `_wiz.draft.title` → ese título corto llegaba a ML.
- **Fix**: deshabilitar el botón "Siguiente" (wiz-btn-next) mientras `_wizGenDraft()` está en progreso.
- Al terminar (`.finally()`), el botón se re-habilita con el título IA ya en el campo.
- Sin bloqueo permanente — solo espera hasta que la IA llene el campo (< 5s normalmente).

---

## 2026-04-10 — FIX: clips T2V cortos (imagen fija) + título auto-fix sin bloqueo

### FIX: Video clips demasiado cortos — imagen congelada tras 10s de movimiento
- LTX-Video: 97 frames/24fps = ~4s/clip. 3 clips = ~12s real. Audio ~36s → imagen fija el resto.
- **Fix**: aumentar a 241 frames (LTX-Video) y 161 frames (Wan2.1) → ~10s/clip → 3 clips ≈ 30s de video real continuo.
- `-stream_loop -1` como safety net por si hay diferencia de duración entre video y audio.

### FIX: Error ML item.title.minimum_length — auto-construir título sin bloquear
- Título muy corto (ej. "Sony KD-50X85K" = 14 chars) cuando no se generó el borrador IA.
- **Fix backend**: si título < 25 chars, auto-construir desde brand + category + size + model.
- Sin bloqueo en frontend — el sistema se corrige solo antes de llamar a ML.

---

## 2026-04-10 — FIX: título lanzar + video 1 clip

### BUG CRÍTICO: ML mostraba "Hisense 55u75qg" (brand+model) en lugar del título IA
- El frontend no enviaba `family_name` como campo raíz del payload.
- El backend calculaba `family_name = brand + " " + model = "Hisense 55u75qg"` cuando family_name_body estaba vacío.
- ML recibía ese family_name y lo usaba como nombre del listing, ignorando el título IA.
- **Fix frontend**: agregar `family_name: _wiz.family_name || ''` al payload de create-listing.
- **Fix backend**: fallback `family_name = title[:60]` en vez de brand+model — así ML usa el título IA si family_name no viene del wizard.

### FIX: Video se generaba con solo 1 clip (se veía "1 movimiento y nada más")
- Se lanzaban 4 clips T2V en paralelo con asyncio.gather → Replicate bajo carga → 3 fallaban.
- Reducido a 3 clips paralelos para bajar la presión en Replicate.
- Si solo 1 clip tiene éxito, se intenta 1 clip extra secuencial antes de pasar al combinado.

---

## 2026-04-09 — FIX: AI título — reemplazar streaming+Vision por generate() JSON igual que lanzar

### BUG: Título IA generaba "No puedo generar los títulos sin ver las imágenes reales del"
- El enfoque anterior usaba streaming SSE + Claude Vision (URLs de imágenes ML), que fallaba silenciosamente o generaba respuestas inválidas.
- Solución: reemplazar por `claude_client.generate()` no-streaming + JSON, igual al endpoint `/api/lanzar/ai-draft-json` que ya funciona bien.
- Prompt con las mismas reglas MeLi 2026 (55-60 chars, formato Marca+Tipo+Tecnología).
- Frontend ahora consume `resp.json().titles[]` en lugar de parsear SSE stream.
- Se elimina también el envío de `image_urls` en este endpoint — no era necesario.

---

## 2026-04-09 — FIX: AI título — error silencioso en stream Vision + fallback texto

### BUG: `generate_stream_with_images` ignoraba errores del stream de Anthropic
- Cuando Anthropic retornaba un evento `{"type": "error"}` en el SSE stream (ej: URL de imagen inaccesible), el `except Exception: continue` lo comía silenciosamente.
- Resultado: `result` vacío → filtro `>= 40 chars` excluía todo → "No se generaron títulos".

### FIX aplicados
- `claude_client.py` (`generate_stream_with_images` y `generate_stream`): detectar evento `type=error` y propagar `RuntimeError` en lugar de ignorarlo.
- `sku_inventory.py` (`title_stream`): si Vision falla, fallback automático a `generate_stream` sin imágenes — el usuario obtiene títulos de todas formas.
- `item_edit_modal.html`: capturar `[ERROR]` en el stream y mostrar el mensaje real en lugar del genérico "No se generaron títulos".

---

## 2026-04-09 — FEAT: Panel Editar Inventario — Claude Vision, video polling, diagnóstico IA

### FEAT: Claude Vision en panel Editar
- `editModalAiTitle()` y `editModalAiDesc()` ahora recopilan las fotos del producto (thumbnails + pictures) y las envían a Claude Vision mediante `generate_stream_with_images()`.
- Indicador visual "👁 Analizando imágenes..." mientras Claude procesa las fotos.
- `autoApply=true` en title: aplica automáticamente el mejor título sin interacción del usuario.

### FIX: Video generation — polling correcto del job background
- `editModalGenVideo()` estaba haciendo `fetch()` y esperando `video_url` de inmediato, pero el endpoint devuelve `{job_id, status: "processing"}`.
- Reescrito con loop de polling cada 4s, mensajes de progreso dinámicos, timeout a 90 rondas (~6 min).
- Al terminar: preview de video en panel + auto-upload a ML vía `/api/lanzar/upload-clip/{item_id}`.

### FEAT: Botones de acción diagnóstico por ítem
- `_calculate_health_score()` ahora incluye campo `"key"` en cada ítem del breakdown (`title`, `description`, `video`).
- Template renderiza botón de acción inline junto a cada ítem diagnóstico que falló: "✦ Mejorar" (título/desc) o "✦ Generar" (video).

### FEAT: Botón "Optimizar Todo"
- Botón ⚡ visible cuando hay al menos un ítem de diagnóstico con problema.
- Ejecuta en secuencia: `editModalAiTitle(true)` → `editModalAiDesc()` → `editModalAiAttrs()`.
- Título se aplica automáticamente; descripción se genera en el textarea para revisión.

### Infraestructura
- `claude_client.py`: nueva función `generate_stream_with_images()` — Vision + SSE streaming.
- `sku_inventory.py` `/ai-improve`: acepta `image_urls[]` en body; usa Vision cuando hay imágenes.

---

## 2026-04-09 — FEAT: Tab Lanzados — datos de publicación + modal Modificar

### Funcionalidad: Guardar datos de publicación al lanzar
- `create-listing` ahora persiste `ml_item_id`, `ml_title`, `ml_price`, `ml_category_id`, `ml_permalink`, `ml_condition`, `launched_at` en `bm_sku_gaps`.
- Columnas agregadas via migración `ALTER TABLE` idempotente en `token_store.py`.

### FEAT: Tab Lanzados con vista de publicaciones
- Tab "✅ Lanzados" muestra: título publicado en ML, precio, fecha de lanzamiento, link directo al listing (`ml_permalink`) y botón "Modificar".
- Header de la tabla cambia dinámicamente al activar este tab (columnas diferentes vs. Sin publicar).
- `_gapsSetStatus('launched')` llama `_updateTableHeader()` antes de recargar.

### FEAT: Modal Modificar publicación
- Nuevo modal permite editar título, precio MXN y stock disponible de un listing ya publicado.
- Frontend envía `POST /api/lanzar/modify-listing` con `{item_id, title, price, stock, sku}`.
- Backend hace `PUT /items/{item_id}` a ML y actualiza la DB local (`ml_title`, `ml_price`).

---

## 2026-04-09 — FIX+FEAT: ML Lanzador — 4 mejoras wizard

### FIX 1: Título generado por IA < 55 chars (SEO subóptimo)
- Root cause: prompt decía "máx 60 chars" — Claude lo trataba como techo, no como objetivo.
- Fix: `lanzar.py:2929` — regla cambiada a "ENTRE 55-60 caracteres (OBLIGATORIO — nunca menos de 55). 59 chars > 49 chars."
- Agrega instrucción de relleno con características adicionales si el título queda corto.

### FIX 2: Video generado es slideshow (zoompan) en vez de video real
- Root cause: PATH A en `generate-video-commercial` usaba ffmpeg zoompan (imágenes estáticas con zoom/pan), no IA generativa de video.
- Fix: `lanzar.py:2343-2448` — nuevo orden de intento:
  1. **Minimax Live img2vid**: primer frame = imagen real del producto → video AI coherente
  2. **Wan2.1 i2v**: fallback img2vid de alta calidad
  3. **Zoompan ffmpeg**: último recurso si ambos fallan
- PATH B (sin imágenes, T2V) sin cambios.

### FIX 3: Error "The fields [title] are invalid for requested call" al publicar
- Root causa: `FAMILY_NAME` se enviaba duplicado — como campo raíz `family_name` Y como atributo `{id: "FAMILY_NAME"}` en la lista de attributes.
- Fix 1: `lanzar.py:3378` — filtrar `FAMILY_NAME` de attrs antes de construir el payload.
- Root causa 2: `str(exc)` para `MeliApiError` podía ser vacío (no llama `super().__init__()`).
- Fix 2: `lanzar.py:3399-3403` — `_post_item` ahora usa `str(exc.body)` para construir `_meli_error`.

### FEAT: Búsqueda de UPC online cuando BM no tiene el dato
- Nuevo endpoint `POST /api/lanzar/search-upc` — busca por brand+model en Open UPC ItemDB API.
- UI: botón 🔍 junto al campo GTIN en Step 2 del wizard (solo visible cuando el campo está vacío).
- Si BM ya tiene el UPC, el botón no hace nada (campo ya está lleno).

---

## 2026-04-09 — FEAT: Amazon listing management completo + Sin Lanzar + fixes header

### FIX 1: "Órdenes hoy: 0 / Unidades hoy: 0 / Revenue hoy: $0.00"
- Root cause: Sales API tiene lag de 2-4h para el día actual. El bucket de hoy siempre llegaba vacío.
- Fix: en `get_amazon_daily_sales_data` (metrics.py), después de llenar buckets con Sales API,
  si el bucket del día actual tiene 0 órdenes, hace fallback al Orders API (real-time) para obtener
  datos del día desde medianoche Pacific.
- El cálculo de medianoche Pacific usa `zoneinfo.ZoneInfo("America/Los_Angeles")` para ser DST-aware.

### FIX 2: BSR siempre mostrando "—"
- Root cause probable: semaphore-5 excedía el rate limit de 2 req/s del Catalog Items API.
  Los errores se tragaban silenciosamente con `except Exception: pass`.
- Fix: reducir semaphore a 2 + agregar `asyncio.sleep(0.6)` + logging explícito de errores.
- Además: manejar posible wrapper `{"payload": {...}}` en la respuesta del Catalog API.

### FEAT: Inline edit de listings — bullet points + descripción
- `amazon_client.py`: nuevos métodos `update_listing_bullets(sku, bullets)` y `update_listing_description(sku, desc)`.
  Usan Listings Items API PATCH con `bullet_point` y `product_description` attributes.
- `amazon_products.py` PATCH endpoint: acepta ahora `bullet_points: list[str]` y `description: str` en body.
- `amazon_product_details`: retorna ahora `bullet_points` y `description` además de título/precio/qty.
- `amazon_products.html`: modal extendido con pestañas "Básico" (título/precio/qty) y "Contenido" (5 bullets + descripción).
  Los campos se pre-llenan automáticamente al abrir el modal.

### FEAT: Tab "Sin Lanzar" — Amazon Lanzador (BM → Amazon gap analysis)
- Nuevo endpoint `GET /api/amazon/products/sin-lanzar` en amazon_products.py.
  - Usa `get_shared_bm().get_global_inventory(min_qty=1)` para obtener todos los SKUs de BM con stock.
  - Compara contra listings activos de Amazon (stripping condition suffixes).
  - Calcula precio sugerido = costo_mxn / 0.62 (covers 18% fees + ~20% margin).
  - Paginación server-side (20/pág), búsqueda por SKU/título/marca, caché 15min.
- Nueva template `partials/amazon_sin_lanzar.html` con tabla paginada + KPI header.
- `amazon_products.html`: nuevo tab "🚀 Sin Lanzar" + funciones JS `_loadAmzSinLanzar()` y `openAmzLanzar()`.

---

## 2026-04-04 — FIX: STALE persistente — 3 causas raíz resueltas

### Causa raíz final confirmada
`asyncio.gather(WH_endpoint, get_stock_with_reserve)` = 2 requests por SKU simultáneos.
Con `wh_sem=12`: 12 × 2 = **24 conexiones simultáneas** → httpx per-host limit = 20 → timeouts
→ ambos fallan → `verified=False` → STALE. Fix anterior (wh_sem=12) reducía *SKUs* paralelos
pero no reducía *requests* por SKU. El problema era el gather de 2 endpoints en _wh_phase.

### Fix 1 — Eliminar WH endpoint de `_wh_phase` (main.py)
`_wh_phase` ahora hace 1 solo request: `bm_cli.get_stock_with_reserve(base)`.
Con `wh_sem=12` + 1 request = 12 simultáneos máx — holgado bajo el límite de 20.
MTY/CDMX/TJ breakdown = 0 (no disponible sin WH endpoint — solo avail total importa).

### Fix 2 — `_query_bm_stock` retorna `None` en fallos (binmanager_client.py)
Antes: retornaba `(0,0)` tanto para "SKU con 0 stock genuino" como para fallos de sesión/red.
`_store_wh` no podía distinguirlos → STALE marcado como verified → falsos avisos.
Ahora: `None` = fallo (timeout, sesión expirada, non-200) / `(0,0)` = HTTP 200 sin match genuino.
`_store_wh` usa `avail_ok = _stock is not None` como señal de verificación.

### Fix 3 — `bm_candidates` incluye TODOS los pausados (main.py)
Antes: `paused AND units>0` → excluía pausados sin ventas recientes.
Resultado: SKUs como SNTV007283, SNTV003804-06, SNTV007241, etc. nunca entraban al cache.
Ahora: `status in ("active", "paused")` — base de datos completa de todos los SKUs lanzados.

### Impacto
- SKUs "No está en caché" post-prewarm: eliminados (todos los pausados ahora se consultan)
- STALE persistente: eliminado (1 request vs 2 → nunca supera límite httpx)
- `_query_bm_stock` None → `verified=False` → Fix A preserva datos buenos previos en cache

---

## 2026-04-04 — FIX: items "Inactiva sin stock" excluidos del prewarm (CAUSA RAÍZ REAL)

### Causa raíz confirmada
ML muestra "Inactiva sin stock" en la UI para listings que auto-desactivó por qty=0.
El API de ML devuelve estos items con `status: "inactive"` — NO `"paused"`.
El código solo buscaba `["active", "paused"]` en todos los lugares → items "inactive"
NUNCA entraban al prewarm ni al ml_listing_sync → BM nunca se consultaba → STALE perpetuo.

Afectados confirmados (BM tiene stock real):
SNTV007283=653, SNTV007867=300, SNTV003804=236, SNTV003805=104, SNTV003806=105,
SNTV003803=94, SNTV007241=92, SNTV007313=150, SNTV006829=43, SNTV007756=25

### Fix aplicado — 5 puntos
1. `main.py _get_all_products_cached` DB path: `["active","paused","inactive"]`
2. `main.py _get_all_products_cached` ML API fallback: idem
3. `main.py bm_candidates` filter: `status in ("active","paused","inactive")`
4. `main.py bm_launch_opportunities` ML SKU set: idem
5. `ml_listing_sync._sync_account_full`: `["active","paused","inactive"]` → DB ahora almacena inactive
6. `stock_sync_multi` fetch + skip guard: idem

### Flujo post-deploy
1. App reinicia → ml_listing_sync corre full sync con inactive → DB actualizada
2. 90s luego: prewarm lee DB → bm_candidates incluye SNTV007283+ → BM devuelve stock real
3. Alertas "Activar" / "Restock" se generan correctamente

---

## 2026-04-04 — FEAT: Corrida inversa — SKUs en BM sin listing en ML

### Nueva pantalla: /bm/unlaunched
Lista paginada de todos los SKUs de BinManager que NO están publicados en ninguna cuenta ML.
Accesible desde el subnav de Productos → "No Lanzados en ML".

### Implementación
- `GET /api/bm/launch-opportunities` refactorizado: 1 sola llamada BM (SEARCH=null, CONCEPTID=1,
  RECORDSPAGE=9999) → 8,706 SKUs en ~3s. Caché 15 min. Antes hacía paginación con CONCEPTID=8.
- Filtros: categoría, búsqueda SKU/marca/modelo, stock mínimo (min_qty).
- Paginación: 20 items/página con controles prev/next.
- Cruce ML: usa `_products_cache` directamente (activos + pausados de todas las cuentas).
  Si el caché está vacío (primer arranque), hace fetch fresco de todas las cuentas.
- Botón "↻ Actualizar BM" fuerza re-fetch ignorando caché.
- `get_global_inventory` actualizado: CONCEPTID 8→1, per_page=9999 por defecto.

### Datos mostrados por SKU
SKU, Categoría (badge por color), Marca/Modelo, Disponible, Reservado, Total, Costo USD, Retail USD

---

## 2026-04-04 — FIX: STALE BM cache persistente (SNAC000029 y similares)

### Causa raíz
Con `wh_sem=50`, los 50+ requests BM concurrentes saturaban la sesión BM → todos retornaban
(0,0) → STALE. Fix C (serial retry) no lo resolvía porque el prewarm de la 2ª cuenta ya
empezaba y re-saturaba la sesión antes de que Fix C completara los retries.

### Fix 1 — wh_sem: 50 → 12
Reduce el batch paralelo a 12 requests simultáneos. BM nunca se sobrecarga.
~7 batches × ~3s = ~25s total vs ~5-10s anterior — velocidad aceptable y sesión estable.

### Fix 2 — Fix C delays: +10s inicial + 2s entre retries
`_do_stale_retry` ahora espera 10s antes de iniciar (sesión estabilizada post-prewarm)
y pausa 2s entre cada SKU retry (breathing room para BM).
Con esto, Fix C garantiza que ningún STALE queda sin resolver después del prewarm.

---

## 2026-04-04 — FIX: Falso oversell_risk + botón Actualizando stuck

### Fix 1 — Falso oversell_risk cuando BM fetch falló (SNAC000029 y similares)

**Causa raíz**: `_store_wh` no agregaba entradas con `total=0, avail=0` a `result_map`,
incluso cuando BM respondió exitosamente confirmando 0 stock (`verified=True`).
Resultado: `_apply_bm_stock` no encontraba `inv`, `_bm_avail` quedaba como `None`,
y el filtro `(None or 0)==0` marcaba el producto como oversell_risk aunque BM tuviera stock.

**Fix**: `_store_wh` ahora incluye en `result_map` cualquier entrada con `verified=True`,
permitiendo distinguir "BM confirmó 0" de "BM no fue consultado / fetch falló".

**Fix**: Filtro `oversell_risk` ahora verifica `"_bm_avail" in p` (BM respondió)
antes de flaggear. Fetch fallido → `_bm_avail` no en dict → NO se flaggea. ✓

### Fix 2 — Botón "Actualizando..." stuck al cargar la página

**Causa raíz**: `_pollPrewarmStatus()` (llamada al cargar la página) actualizaba el botón
a "Actualizando..." si el prewarm estaba corriendo, pero NO arrancaba el timer de polling.
El botón nunca se reseteaba cuando terminaba el prewarm.

**Fix**: Si `d.running=true` y no hay timer activo, arranca `_prewarmPollTimer` automáticamente.

---

## 2026-04-04 — FEAT: SKU unificado cross-account + oportunidades de lanzamiento

### Proceso 1 — Stock unificado: ML → BM (una sola consulta por SKU único)

**normalize_to_bm_sku**: nueva función central que extrae el SKU base BM de cualquier variante ML.
- Regla: primeros 10 chars tras limpiar bundle, packs y sufijos (-GRA, -ICS, NEW, etc.)
- SNTV007270-ICS → SNTV007270; SNTV007270 / SNAC000029 → SNTV007270 (14/14 casos verificados)

**Cache key unificado**: `_bm_stock_cache` ahora indexado por `normalize_to_bm_sku(sku)` en lugar de `sku.upper()`.
- SNTV007270-GRA, SNTV007270 NEW y SNTV007270 de 3 cuentas distintas → 1 entrada en cache
- Reducción ~40-60% de requests a BM en prewarm → menos sesiones expiradas → menos STALE

**Prewarm unificado** (`_startup_prewarm`): ahora recolecta productos de TODAS las cuentas en paralelo,
deduplica por SKU base BM, hace UNA sola pasada BM para el universo completo, y luego corre
prewarm por cuenta usando los datos ya en cache (sin re-fetch).

**Post-fetch fill**: después del `asyncio.gather`, rellena `result_map` para SKUs que fueron
deduplicados (misma bm_key, distintos sufijos) usando el cache ya poblado.

### Proceso 2 — Oportunidades de lanzamiento: BM → ML (inverso)

**`/api/bm/launch-opportunities`**: escanea inventario BM completo (paginado), cruza con todos los
SKUs activos de todas las cuentas ML, devuelve los que no tienen listing → oportunidades de venta.

### Vista cross-account

**`/api/stock/unified`**: por cada SKU base BM, muestra BM avail + qty por cuenta + acción sugerida
(oversell_risk / zero_listing / low_stock / ok).

### UI

- Dos nuevas secciones en stock_sync.html: "Stock Unificado" y "Oportunidades de Lanzamiento"
- Tablas paginadas con resumen de acciones por categoría

**Archivos modificados:**
- `app/main.py`: `normalize_to_bm_sku`, `_get_bm_stock_cached`, `_store_wh`, `_store_empty`,
  `_startup_prewarm`, `/api/debug/bm-cache`, + 2 nuevos endpoints
- `app/templates/stock_sync.html`: 2 nuevas secciones + JS

---

## 2026-04-04 — FIX: STALE perpetuo por session failure bajo carga de prewarm (SNHG000004)

### BUG — SKUs con stock real (ej: SNHG000004 con 2146 uds) persisten como STALE y oversell_risk

**Root Cause confirmado por BM Agent:**
- SNHG000004 tiene 2,146 unidades en LocationID 47 (CDMX) + 68 (MTY) — ubicaciones vendibles correctas
- El prewarm con Semaphore(50) estresa el servidor BM → sesión expira mid-prewarm
- `get_stock_with_reserve` detecta expiración → intenta re-login → falla bajo carga → retorna `(0,0)` **tuple**
- El endpoint WH (httpx raw, sin session management) también devuelve HTML → `wh_responded=False`, `rows=[]`
- Ambos en cero: `warehouse_total=0`, `avail_total=0` → fallback `warehouse_total>0` nunca aplica
- `verified = False` → escribe `{avail_total:0, _v:False}` → **sobreescribe la entrada previa buena (avail=2146)**

**Fix A — Preservar datos buenos ante session-failure zeros** (`_store_wh` en `main.py`)
- Si `not verified AND avail_total==0 AND warehouse_total==0`, verificar si hay entrada previa con `_v=True` y `avail_total>0`
- Si existe → `return` sin sobreescribir → la entrada buena se preserva hasta que vence su TTL naturalmente
- Previene falso oversell_risk por sesión rota

**Fix B — Reducir concurrencia de prewarm: Semaphore(50) → Semaphore(15)**
- 50 requests simultáneos a BM estresa el server → más sesiones expiradas → más fetches fallidos
- 15 es el valor anterior estable; el prewarm tarda un poco más pero los datos son confiables

**Fix C — Retry serial post-prewarm para SKUs STALE**
- Tras el `asyncio.gather` principal, detectar SKUs que quedaron con `_v=False`
- Re-intentarlos uno a uno (serial, baja carga) con sesión ya establecida
- Cubre el caso donde no había entrada previa para Fix A (primer prewarm tras reinicio)

**Archivos modificados:**
- `app/main.py`: `_store_wh` (Fix A), `wh_sem` (Fix B), post-gather retry pass (Fix C)

---

## 2026-04-03 — FIX: BM stock data discarded on session expiry (intermittent BM=0)

### BUG — SNTV007283 y otros SKUs con stock real aparecen en Riesgo Sobreventa intermitentemente

**Root Cause 1 (primario):** `r_wh.json()` dentro del `try` general de `_wh_phase`
- Cuando BM session expira, `http.post(BM_WH_URL)` devuelve HTML (redirect a login, status=200)
- `r_wh.json()` lanza `JSONDecodeError` → except block → `_store_empty(sku)`
- El valor válido `avail_direct=653` ya calculado desde `get_stock_with_reserve` se descartaba completamente
- Fix: Envolver `r_wh.json()` en su propio try/except → `rows_wh=[]` en fallo, `_store_wh` siempre corre con `avail_direct` correcto

**Root Cause 2 (secundario):** Concurrent re-login sin lock
- Con Semaphore(50), hasta 50 coroutines detectan sesión expirada y llaman `login()` simultáneamente
- `BinManagerClient.login()` sin `asyncio.Lock` → 50 requests de login a BM en paralelo
- Fix: `asyncio.Lock` en `login()` → solo un re-login real; coroutines en espera detectan `_logged_in=True` y continúan

**Archivos modificados:**
- `app/main.py`: `_wh_phase` — JSON parse en try/except propio
- `app/services/binmanager_client.py`: `__init__` + `login()` — `asyncio.Lock`

---

## 2026-04-03 — FIX: BM cache false positives + Inventario blank columns + force prewarm tool

### BUG — Riesgo Sobreventa mostraba productos con BM stock real
Tres root causes identificadas y corregidas:

**Root cause 1 — Cache servía entradas 0-stock de fetches fallidos**
- `_get_bm_stock_cached`: entradas con `total=0, avail=0` sin `_v=True` se servían como datos válidos.
- Fix: `_cache_is_valid` ahora rechaza esas entradas → se re-fetchea en el siguiente prewarm.
- `_store_wh`: nuevo campo `_v` (verified = bool(rows_wh) OR avail_total>0 OR reserved_total>0).

**Root cause 2 — Fetch parcial almacenaba {total>0, avail=0}**
- `_wh_phase`: si `get_stock_with_reserve` lanzaba excepción (timeout), se almacenaba `avail=0` aunque WH breakdown era correcto.
- Fix: `_avail_ok = isinstance(_stock, tuple)` distingue excepción de respuesta genuina (0,0).
- `_store_wh`: fallback `if avail_total==0 AND warehouse_total>0 AND not avail_ok → avail_total = warehouse_total`.

**Root cause 3 — Prewarm excluía productos con MeLi stock=0**
- `bm_candidates` solo incluía productos con `meli_available > 0` → productos en "Activar" nunca se fetcheaban.
- Fix: `bm_candidates = [p for p in products if p.get("sku")]` — todos los SKUs.

### BUG — Columnas Inventario en blanco (TJ, Ventas 30d, Días, Revenue, Costo BM, Margen)
- **Fix A:** `_has_data` check en Phase 1 bloqueaba aplicar datos BM a productos con bm_avail=0.
- **Fix B:** `products_inventory.html` — 4 TDs (`días`, `revenue`, `costo_bm`, `margen`) tenían condición `_section != 'accion'` faltante → columnas ocultas en sección correcta.
- **Fix C:** `_enrich_with_bm_product_info` usaba `httpx.AsyncClient()` sin autenticación → respuestas HTML de login page.

### FEAT — Force prewarm + SKU diagnostic en tab Stock
- Botón "🔄 Actualizar ahora" en card "Caché de Stock BM" → `POST /api/stock/force-prewarm`
  - Limpia entradas stale (0-stock sin `_v` + partial failures `total>0, avail=0`)
  - Limpia `_stock_issues_cache` → alertas se recalculan fresh
  - Polling live con spinner hasta completar
- Campo SKU + botón "Consultar" → `GET /api/debug/bm-cache?sku=XXX`
  - Muestra: BM Avail, Total WH, Reserve, MTY/CDMX/TJ, edad/TTL, estado
  - Lista alertas activas en `_stock_issues_cache` donde aparece el SKU

---

## 2026-04-02 — FIX: BM columns show 0 instead of "-" + health banner only on errors

**BM columnas muestran 0 en vez de "-"** (todas las secciones):
- `products_inventory.html`: eliminado guard `_bm_total is not none` — BM Disp y BM Res siempre muestran valor (0 cuando sin datos). Mobile view también siempre visible.
- `products_top_sellers.html`: eliminado guard `_bm_avail is not none` — div siempre renderiza con `bm_avail = p.get('_bm_avail', 0) or 0`
- `products_low_sellers.html`: mismo fix
- `products_deals.html`: fix en card view (línea 446) y table view (línea 582) — ambos siempre muestran valor

**Health banner solo para errores reales**:
- `system_health.py`: `_check_stock_sync()` — arranque reciente devuelve `_ok("Primer ciclo pendiente...")` en vez de `_warn`. El sync auto-corre, no hay acción necesaria del usuario.
- `base.html`: banner global solo se muestra si `overall === 'error'`, no para `warning`. BM 503 y otros warnings temporales van al widget de health pero no al banner persistente.

---

## 2026-04-02 — DECISION: Endpoint BM definitivo para stock vendible

### DECISION — Get_GlobalStock_InventoryBySKU CONCEPTID=1 es el endpoint correcto

- **CONCEPTID=1** devuelve `AvailableQTY = TotalQty - Reserve` calculado server-side — correcto y verificado.
- **`GlobalStock_InventoryBySKU_Condition`.`status`** siempre retorna "Otro" — campo legacy sin usar. NO usar para filtrar.
- **`get_available_qty()`** en `binmanager_client.py` ya usa CONCEPTID=1 correctamente.
- **Condition-variant fallback:** SKUs como SNTV004196 existen solo como SNTV004196-GRB en BM → fallback suma variantes.
- **Cache EMPTY (total=0, avail=0):** fuerza re-fetch para evitar falsos negativos persistentes.
- **`_prewarm_queued`:** evita perder llamadas de prewarm cuando ya hay una corriendo.
- **"Sync ahora":** espera a que prewarm complete antes de recargar UI (no mostraba datos frescos antes).

---

## 2026-04-02 — PERF CRÍTICO: Stock tab tardaba 130s+ → carga instantánea desde DB

### BUG — Timeout 130s + loop infinito de reinicios
- **Root cause 1:** `_get_all_products_cached` llamaba ML API cada 15 min (~300 batch calls, ~15-25s) aunque `ml_listing_sync` ya tenía la DB actualizada. La DB nunca se leía.
- **Root cause 2:** `_get_bm_stock_cached` hacía 2400-3600 BM calls para 1200-1800 SKUs con Semaphore(20) → 60-120s solo en BM. Total = 75-145s → timeout.
- **Root cause 3:** Spinner en timeout hacía `setTimeout(reload, 3000)` → nuevo prewarm → nuevo timeout → loop infinito.
- **Root cause 4:** "Sync ahora" llamaba `_prewarm_caches()` pero si ya había un prewarm corriendo retornaba inmediato (no-op). El usuario veía "0 updates" y nada cambiaba.

### FIX — Fase A (fixes inmediatos)
- Spinner: eliminar auto-reload en error/timeout → botón manual "Reintentar"
- `_prewarm_caches`: agregar `_prewarm_queued` — si llaman mientras corre, encola y relanza al terminar
- `multi_sync_trigger`: no limpiar `_stock_issues_cache` si prewarm activo; usar `asyncio.create_task(_prewarm_caches())` que ahora hace cola
- BM Semaphore: 20 → 50 (reduce tiempo ~60%)
- ML fetch Semaphore: 5 → 10 (reduce tiempo ~50%)

### FIX — Fase B (caché persistente SQLite)
- `token_store.py`: migration `data_json` en `ml_listings`, nueva tabla `bm_stock_cache`, funciones `upsert_bm_stock_batch` / `load_bm_stock_cache` / `get_ml_listings_max_synced_at`
- `ml_listing_sync.py`: guardar `data_json` (body completo del item) en cada row
- `_get_all_products_cached`: leer de `ml_listings` DB si `synced_at < 1h` → <100ms en lugar de 300 API calls
- `_get_bm_stock_cached`: persistir nuevas entradas BM a DB (fire-and-forget); Semaphore 20→50
- `_load_bm_cache_from_db`: cargar BM desde DB al arrancar (entradas < 30 min)
- `lifespan`: `asyncio.create_task(_load_bm_cache_from_db())` en startup
- `_startup_prewarm` delay: 30s → 90s (espera que `ml_listing_sync` llene la DB primero)

### RESULTADO ESPERADO
- Primera carga post-restart: items de DB (<100ms) + BM de DB (<100ms) → prewarm en <10s
- "Sync ahora": funciona, encola prewarm si ya hay uno corriendo, muestra resultado correcto

---

## 2026-04-02 — Fix: Sync ahora no recargaba sección con datos frescos

### BUG — Sección "Riesgo Sobreventa" mostraba BM:0 aunque el fix ya estaba deployado
- **Root cause 1 (ya fijado):** `get_available_qty` en `binmanager_client.py` hacía exact match. SKUs como SNTV005362 solo existen como `SNTV005362-GRA`/`SNTV005362-GRB` en BM → retornaba 0 → `_bm_avail=0` → falsa alerta. Ya corregido con condition-variant fallback.
- **Root cause 2 (este fix):** `triggerStockSync` y `triggerSyncNow` paraban de pollear cuando multi-sync terminaba (`_sync_running=False`), PERO el prewarm que re-fetcha BM con datos frescos apenas empezaba. La sección nunca se recargaba → seguía mostrando el caché viejo con BM:0.
- **Fix:** Fase 2 de polling — después de multi-sync, esperar 3s para que prewarm arranque, luego pollear `/api/stock/prewarm-status` hasta `!running`. Cuando termina: recargar el tab activo (inventory/stock) con datos frescos.
- **Archivos:** `main.py` (triggerStockSync), `items.html` (triggerSyncNow)
- **Commit:** 4e2d115

---

## 2026-04-02 — Fix: "Sync ahora" (banner health) sin feedback visual

### FIX — Botón "Sync ahora" no mostraba nada al hacer click
- **Root cause:** `_globalHealthFix()` en `base.html` disparaba `_fixAction.fn()` sin ningún cambio visual. El usuario veía el botón estático y no sabía si algo pasó.
- **Fix:** Reescribir `_globalHealthFix()` para:
  1. Cambiar texto del botón a "Iniciando..." y deshabilitarlo inmediatamente al click
  2. Actualizar mensaje del banner a "Ejecutando sync..."
  3. Para `stock_sync`: pollear `/api/stock/multi-sync/status` cada 1s hasta `running=false` (máx 60s), mostrando contador de segundos
  4. Al terminar: mostrar toast verde "Sync completado ✓" (o rojo si hubo error en `last_result.error`)
  5. Re-ejecutar `_checkGlobalHealth()` para actualizar el banner con el estado real
  6. Para otras acciones (tokens, amazon): re-check tras 3s

---

## 2026-04-02 — Fix CRÍTICO: BM stock falso — Get_GlobalStock_InventoryBySKU devuelve contador contable, no stock físico

### BUG — get_available_qty() retornaba datos incorrectos (202 vs 2 real)
- **Root cause:** `Get_GlobalStock_InventoryBySKU` con CONCEPTID=8 devuelve un campo `AvailableQTY` que es un contador contable de nivel producto. NO refleja stock físico real. Verificado: SNTV006722 devuelve 202 cuando hay exactamente 2 unidades físicas (2x GRB en MTY MAXX bin P01-F055-01, seriales MTG23T0171 y MTG33T7519). El valor 202 es idéntico con CONCEPTID 1, 2, 3 y 8 — confirma que no es stock físico.
- **Endpoint correcto:** `GlobalStock_InventoryBySKU_Condition` con `LocationID=47,62,68` + suma `TotalQty` donde `status=="Producto Vendible"` en `Conditions_JSON`. Exactamente lo que `amazon_products.py` ya usaba correctamente.
- **Fix:** Reescribir `BinManagerClient.get_available_qty()` en `binmanager_client.py` para usar el endpoint correcto. Al ser centralizado, corrige automáticamente todos los callers: `main.py` (`_wh_phase`), `lanzar.py`, `sku_inventory.py`, `items.py`.
- **Stock real SNTV006722:** 2 unidades (MTY MAXX, GRB). Guadalajara tiene 6 más (LocationID 66, no incluida en 47,62,68).
- **Commit:** bbd887e

## 2026-04-02 — Feat: Sync stock individual por variacion desde BM

### FEAT — BM Disp. column + Sync button por variacion en panel detalle
- **Archivos:** `products_inventory.html`, `items.py`, `items.html`
- **Problema:** En el panel de variaciones solo se veía "Stock ML" sin columna BM, imposible saber si sincronizar cada hijo individualmente.
- **Solución:**
  1. `products_inventory.html`: columna "BM Disp." (azul si >0, gris si 0) + botón "Sync {qty}" por variacion. El botón llama `syncVariationStock(itemId, varId, bmQty, btn)`.
  2. `items.py`: nuevo endpoint `PUT /api/items/{item_id}/variations/{variation_id}/stock` usando `update_variation_stocks_directly` (solo modifica la variacion indicada, no las demás).
  3. `items.html`: nueva función JS `window.syncVariationStock()` con feedback visual OK/Error y auto-reset del botón.
- **Commit:** 9f482fa

## 2026-04-02 — Fix: Race condition BM=0 + Stock tab timeout

### BUG — BM=0 en tab Inventario (race condition variaciones)
- **Root cause:** `_get_bm_stock_cached` y `_enrich_variation_skus` corrían en PARALELO en asyncio.gather. BM fetcha SKUs antes que las variaciones tengan sus SKUs populados. `_apply_bm_stock` luego ve variaciones con SKUs específicos (e.g. SNTV001764-001) que no están en bm_map (que solo tiene SNTV001764 padre) → BM=0 para todos los productos con variaciones.
- **Fix:** Cambiar a ejecución SECUENCIAL: `await _enrich_variation_skus` primero, luego `_get_bm_stock_cached` (con variaciones ya populadas). BM y sale_prices siguen en paralelo entre sí.
- **Aplica a:** Todos los productos con variaciones (SNPE000218, SNTV001764, SNFA001259, etc.)

### BUG — Stock tab spinner eterno (prewarm timeout con 6374 productos)
- **Root cause:** Con 6374+ listings activos/pausados, `_get_bm_stock_cached(products)` intentaba fetchear BM para TODOS → ~300+ rounds con sem=20 → timeout a 150s → `_stock_issues_cache` nunca se popula → spinner eterno, "Sync ahora" no servía.
- **Fix:** Prewarm y background prefetch solo fetchean `bm_candidates` = productos con SKU + (ventas>0 OR stock_meli>0). Esto reduce de 6374 a ~200-500 productos → completa bien dentro de 150s.

### BUG — Mismos fixes aplicados a todos los archivos (InventoryBySKUAndCondicion_Quantity roto)
- `lanzar.py`, `sku_inventory.py`, `main.py` (deals, not-published, concentration/scan)

---

## 2026-04-02 — Fix: BM correcto endpoint + paginacion stock-issues

### FIX — BM stock=0 masivo (root cause final: endpoint roto server-side)
- **Root cause real:** `InventoryBySKUAndCondicion_Quantity` tiene bug SQL server-side ("Invalid column name 'binid'") — siempre devuelve lista vacía independientemente de parámetros.
- **Fix final:** Centralizar en `BinManagerClient.get_available_qty()` usando `Get_GlobalStock_InventoryBySKU` con CONCEPTID=8. Este endpoint devuelve `AvailableQTY = TotalQty - Reserve` calculado server-side. Verificado en Network tab de BM: SNTV006850 TotalQty=84, Reserve=80, AvailableQTY=4.
- **Archivos afectados:** `binmanager_client.py` (nuevo método), `main.py` (_wh_phase + _query_bm_avail + multi-sync-trigger), `stock_sync_multi.py` (_one), `items.py` (_bm_warehouse_qty), `productos.py` (_bm_stock).
- **Alertas stale:** prewarm loop ahora re-ejecuta `_run_stock_sync_for_user` después de cada ciclo. "Sync ahora" limpia caches + re-prewarm + re-alertas.
- **Commits:** serie 7d3b243

### FEAT — Paginacion max 20 filas en todas las secciones del tab Stock
- Agrega `<div id="pager-*">` en restock, risk, critical, activate, fullstock.
- JS `paginateTable()` ya estaba en el template — solo faltaban los divs target.
- **Commit:** 7d3b243

---

## 2026-04-02 — Fix: LOCATIONID=None en InventoryBySKUAndCondicion_Quantity (BM stock=0 masivo)

### BUG — Todos los productos mostraban BM Disponible=0, Res=N (stock físico entero marcado como reservado)
- **Síntoma:** SNAC000029 (BM: Reserve=0, Available=2471) aparecía como BM=0, Res:2468 en dashboard
- **Root cause:** `avail_payload` usaba `LOCATIONID: "47,62,68"`. Este filtro funciona en el WH endpoint pero `InventoryBySKUAndCondicion_Quantity` lo ignora y retorna lista vacía → avail_direct=0. La fórmula `reserved = warehouse_total(2468) - avail_direct(0) = 2468` incorrecta.
- **Fix:** `LOCATIONID: None` en avail_payload de `_wh_phase` (main.py) y `_one` (stock_sync_multi.py). BM devuelve total disponible global, mismo que muestra el UI sin filtro.
- **Commit:** 08bf6df

### FEAT — Performance: Stock tab ya no muestra spinner de 90 segundos
- **Cambio 1:** Loop de prewarm cada 10 min (antes: solo al arranque). Cache siempre caliente.
- **Cambio 2:** Cache expirada → mostrar datos stale inmediatamente + banner "Actualizando..." + refresh en BG. Elimina espera de 90s para usuario.

---

## 2026-04-02 — Fix: endpoint BM correcto — InventoryBySKUAndCondicion_Quantity

### BUG — Stock disponible no descuenta reservados (SNTV001763: Reserve=4, Available=0 pero mostraba BM=4)
- **Síntoma:** SNTV001763 muestra BM Disponible=4 en dashboard y Reabastecer. BM real: Reserve=4, Available=0. Generaría sobreventa si se sincroniza.
- **Root cause:** `GlobalStock_InventoryBySKU_Condition` devuelve `TotalQty` físico en condición "Producto Vendible" SIN descontar reservados para órdenes en proceso.
- **Fix:** Cambiar a `InventoryBySKUAndCondicion_Quantity` → campo `Available` ya excluye reservados. Este endpoint ya estaba siendo usado correctamente en `items.py` y `api/lanzar.py`.
- **Payload:** `{COMPANYID, TYPEINVENTORY:0, WAREHOUSEID, LOCATIONID, BINID, PRODUCTSKU, CONDITION, SUPPLIERS, LCN, SEARCH}`
- **Parsing:** `sum(row["Available"])` — eliminado el parsing complejo de `Conditions_JSON → SKUCondition_JSON → Producto Vendible`
- **Aplica en:** `_get_bm_stock_cached/_wh_phase` (main.py) + `_fetch_bm_avail` (stock_sync_multi.py)
- **Commit:** b0e5407

---

## 2026-04-02 — Fix CRÍTICO: BM auth — 150+ productos con BM=0 por llamadas sin sesión

### BUG ROOT CAUSE — _wh_phase y _fetch_bm_avail sin autenticación BM
- **Síntoma:** 150+ productos muestran BM=0 (incluyendo SNAC000029 con 2,467 unidades reales)
- **Root cause real:** `_wh_phase` (main.py) y `_fetch_bm_avail` (stock_sync_multi.py) usaban `httpx.AsyncClient` anónimo sin cookies de sesión. BM requiere autenticación (login con USRNAME/PASS + cookie de sesión). Sin auth, BM devuelve redirect a /User/Index (HTML) o 401. Intentar `.json()` sobre HTML lanza excepción → `except Exception: pass` silencioso → `_store_empty` → BM avail=0.
- **Porqué no se detectó antes:** el `except Exception: pass` tragaba el error sin logging. El sistema aparentaba funcionar (no crashes) pero guardaba 0 para todo silenciosamente.
- **Fix:** `binmanager_client.py` → agregar `post_inventory()` + singleton `get_shared_bm()` con login automático. `_get_bm_stock_cached` y `_fetch_bm_avail` usan `get_shared_bm()` en lugar de cliente anónimo. Logging explícito reemplaza `except Exception: pass`.
- **Commit:** fdcec54

### BUG INTRODUCIDO Y REVERTIDO — condiciones "NEW only" para SKUs simples
- Cambié `_bm_conditions_for_sku` a retornar "NEW" para SKUs simples pensando que overcounting era por mezcla de condiciones. Error: SNAC000029 tiene TODO su stock en GRA/GRB/GRC (0 en NEW). Revertido de inmediato.
- **Lección:** nunca asumir condición BM desde el nombre del SKU — siempre verificar con BM agent.

---

## 2026-04-02 — Fix condiciones BM por SKU — no mezclar NEW con GRA/GRB/GRC

### BUG — Stock BM sobreestimado en publicaciones NEW (SHIL000154: 557 en lugar de 228)
- **Síntoma:** Dashboard mostraba 557 BM para SHIL000154 (Lámpara de Tocador). BM real vendible era 228 NEW.
- **Root cause:** `_bm_conditions_for_sku` retornaba `"GRA,GRB,GRC,NEW"` para todos los SKUs simples. BM sumaba las 228 unidades NEW + 329 unidades GRA/GRB/GRC de publicaciones diferentes.
- **Fix main.py:** `_bm_conditions_for_sku` ahora retorna condición exacta: simple/sin sufijo → `"NEW"`, `-GRA` → `"GRA"`, `-GRB` → `"GRB"`, `-GRC` → `"GRC"`. ICB/ICC siguen con todas las condiciones.
- **Fix stock_sync_multi.py:**
  - `_listing_key(sku)`: nuevo helper que preserva sufijos de condición en la clave de agrupación. `SHIL000154` y `SHIL000154-GRA` son grupos separados (antes ambos colapsaban a `SHIL000154`).
  - `_cond_for_key(key)` / `_bm_base_for_key(key)`: helpers de condición por key.
  - `_fetch_bm_avail()`: ahora acepta `dict{key → conditions}` en lugar de lista plana.
  - `_collect_ml_listings()` / `_collect_amz_listings()`: usan `_listing_key()` en lugar de `_base_sku()`.
- **Efecto secundario positivo:** SNWM000004 (BM=0 persistente) también puede resolverse — sus 2,015 unidades son todas NEW, y antes la query mezclaba GRA (vacío) con NEW generando resultados ambiguos.
- **Commit:** 256b215

---

## 2026-04-02 — Fix Sync Var. variaciones bundle + 'str' object has no attribute 'get'

### BUG — sync_variation_stocks_api: 'str' object has no attribute 'get'
- **Síntoma:** Al hacer Sync Var. en items con variaciones, aparecía error "BM error: 'str' object has no attribute 'get'" en cada variación.
- **Root cause:** `r_avail.json() or []` — si BM devuelve un dict (no lista), el `or []` no aplica porque el dict es truthy. Luego `for row in avail_rows` iteraba sobre chars del dict y `.get()` fallaba. Mismo problema en `r_wh.json() or []`.
- **Fix:** Agregar `if isinstance(rows, dict): rows = [rows]` + `if not isinstance(rows, list): rows = []` en ambas respuestas.

### FEAT — Sync Var. bundle: stock = mínimo entre componentes (A / B)
- **Antes:** Para SKU compuesto `SNTV001763 / SNWM000001`, solo se consultaba el primer componente (`SNTV001763`). El segundo se ignoraba.
- **Ahora:** Se consultan TODOS los componentes del bundle en paralelo. `bm_avail = min(avail_A, avail_B)` — el cuello de botella determina cuántos bundles se pueden armar. Si BM falla para cualquier componente, se reporta error en lugar de usar dato incompleto.
- **Aplica a:** SKUs separados por `/` o `+` en el SELLER_SKU de la variación.

---

## 2026-04-02 — Fix regresión _bm_avail=0 (SNAC000029 y 130 productos más)

### BUG RAÍZ — _bm_avail=0 para todos los productos con stock real en BM (regresión Fase 1A)
- **Síntoma:** 131 items en "Riesgo sobreventa" incluyendo SNAC000029 (2,467 uds), SNAC000046 (1,622), SNTV001764 (301), SNFN000164 (256), etc. — todos con BM=0 aunque BM sí tiene stock.
- **Root cause (introducido por Fase 1A):** La Fase 1A cambió `oversell_risk` de `_bm_total==0` a `_bm_avail==0`. Esto expuso un bug pre-existente: `_wh_phase` en `_get_bm_stock_cached` calculaba `avail_total = warehouse_total - reserve_global` donde `reserve_global` venía de `Get_GlobalStock_InventoryBySKU` (CONCEPTID=8). Este endpoint devolvía `Reserve >= TotalQty` para muchos SKUs (e.g. SNAC000029: Reserve=2467, Total=2467), resultando en `avail_total = max(0, 2467-2467) = 0`. Con `_bm_total` el bug era invisible (warehouse_total era correcto); con `_bm_avail` el bug causaba falsos oversell_risk.
- **Fix (commit xxxx):** `_wh_phase` ahora llama `GlobalStock_InventoryBySKU_Condition` en paralelo junto con el Warehouse endpoint, en lugar de `Get_GlobalStock_InventoryBySKU`. Parsea `status == "Producto Vendible"` → `TotalQty` directamente, el mismo approach que `_fetch_bm_avail` en `stock_sync_multi.py` que ha sido verificado como correcto. `_store_wh` simplificado: recibe `avail_direct` y lo usa directamente sin fórmula de resta.
- **Lección:** Dos endpoints de BM para "stock disponible" producen resultados distintos. `GlobalStock_InventoryBySKU_Condition` con `status==Producto Vendible` es la fuente correcta. El endpoint `Get_GlobalStock_InventoryBySKU` (CONCEPTID=8) con SEARCH tiene campo `Reserve` inconsistente con el stock vendible real.

---

## 2026-04-02 — Plan estratégico Fase 1 + 2 + 3

### FIX (Fase 1A) — oversell_risk usaba _bm_total en vez de _bm_avail
- **Bug:** La alerta "Riesgo de overselling" en Stock Issues (y en el endpoint de alertas) filtraba con `_bm_total == 0` en lugar de `_bm_avail == 0`. Consecuencia: un item aparecía como "no hay stock" aunque hubiera unidades disponibles no-reservadas, o viceversa — items con todo el stock reservado no eran detectados como riesgo.
- **Fix:** Dos lugares en `main.py` (líneas 2022 y 2564) cambiados de `_bm_total` a `_bm_avail`. `_bm_avail` es el stock real vendible (excluye reservados), `_bm_total` es solo físico.

### FEAT (Fase 1C) — app/services/sku_utils.py: módulo canónico de extracción de SKU
- **Problema:** La lógica de extracción de SKU estaba duplicada en 5+ lugares: `main.py`, `stock_sync_multi.py`, `ml_listing_sync.py`, etc. Cada implementación tenía ligeras diferencias.
- **Solución:** Nuevo módulo `app/services/sku_utils.py` con:
  - `extract_variation_sku(variation)` — extrae de variación (seller_custom_field o SELLER_SKU attr)
  - `extract_item_sku(item)` — extrae de item ML completo (prioriza variaciones sobre padre)
  - `base_sku(sku)` — normaliza a SKU base: quita sufijo variante, extrae primer token de bundles
- `stock_sync_multi.py` y `ml_listing_sync.py` ahora usan este módulo; duplicados eliminados.

### FEAT (Fase 2) — ml_listings DB local + sync background (spinner de Stock → historia)
- **Problema:** Tab Stock tardaba 60-150s porque llamaba ML API en cada carga.
- **Solución:** Nueva tabla `ml_listings` en SQLite + servicio `ml_listing_sync.py`:
  - Al arranque (delay 30s): sync completo active+paused para todas las cuentas
  - Cada 10min: sync incremental (top-50 por last_updated)
  - Cada 6h: reconciliación completa para capturar cerrados
  - Las reads del tab Stock leen de DB local (instantáneo) en vez de llamar ML API
- Stock al registrar en token_store: `upsert_ml_listings`, `get_ml_listings`, `get_ml_listings_all_accounts`, `count_ml_listings_synced`
- `start_ml_listing_sync()` registrado en lifespan de FastAPI (main.py línea 319)

### FEAT (Fase 3A) — Tarifas ML dinámicas por precio (vs flat 17%)
- **Antes:** `_score()` en `stock_sync_multi.py` usaba `_ML_FEE = 0.17` flat para todos los productos.
- **Ahora:** `_ml_fee(price)` aplica tarifa diferenciada por bracket de precio MXN:
  - ≥ $5,000: 12% (TVs, laptops)
  - $1,500–$5,000: 14%
  - $500–$1,500: 16%
  - < $500: 18%
- El scoring ahora favorece correctamente a productos caros (menor tarifa relativa = mejor margen neto).

### FEAT (Fase 3B) — Umbral de concentración dinámico por valor de producto
- **Antes:** `STOCK_THRESHOLD = 10` fijo para todos los SKUs.
- **Ahora:** `_threshold_for(listings)` calcula umbral según precio promedio del SKU:
  - Precio medio ≥ $10,000: umbral=3 (TVs premium, rotan lento)
  - $2,000–$10,000: umbral=5
  - $500–$2,000: umbral=10 (default actual)
  - < $500: umbral=20 (artículos baratos rotan rápido, necesitan buffer)
- El plan de distribución llama `_threshold_for(updatable)` para cada SKU en tiempo real.

### FEAT (Fase 3C) — Detección de canibalización entre cuentas
- **Nuevo:** `_detect_cannibalization(ml_by_sku)` en `stock_sync_multi.py`
- Detecta SKUs con 2+ cuentas ML activas (qty>0) pero donde 0 o 1 cuentas tienen ventas históricas. Indica que las cuentas sin ventas consumen visibilidad del algoritmo ML sin convertir.
- El resultado se incluye en `summary["cannibalization"]` del sync y se loguea como warning.
- Próximo paso: mostrar en la UI como alerta de tipo "Canibalización multi-cuenta".

---

## 2026-04-02 (cont.)

### BUG — Sync multi-plataforma pone items en 0 cuando BM tiene error de API
- **Síntoma:** Items de ML quedaban en qty=0 después de cada ciclo de sync de 5 min, aunque el SKU sí tenía stock en BM. El usuario actualizaba manualmente → sync volvía a poner 0.
- **Root cause (commit a40a473):** `_fetch_bm_avail` en `stock_sync_multi.py` escribía `result[base.upper()] = 0` tanto en respuestas 200 con avail real=0 COMO en errores de BM (timeout, 429, 5xx). El caller no podía distinguir "BM dice 0" de "BM falló". En el segundo caso, el sync correctamente calculaba que debía poner qty=0 en ML y lo ejecutaba.
- **Diagnóstico adicional:** Los ML item IDs reportados (1336870147 y 892546286) devuelven 404 desde las 4 cuentas ML — los items ya no existen o son de otra sesión. La causa raíz aplica a cualquier SKU cuya consulta BM falle por cualquier razón.
- **Fix:** Al recibir error BM (non-200 o excepción), `_one()` hace `return` sin escribir al dict. El caller en `run_multi_stock_sync` skipea el SKU si no está en `bm_stock` con un `continue` en lugar de `bm_stock.get(base, 0)`. Solo se pone qty=0 cuando BM responde 200 con avail=0 real.
- **Impacto:** Cero riesgo de falsos positivos "sin stock" por errores transitorios de BM.

---

## 2026-04-03

### BUG — Stock tab spinner infinito (persistente, nunca cargaba en produccion)
- **Sintoma:** Tab Stock quedaba en "Calculando stock en background... Revisando cada 5 segundos..." indefinidamente en Railway.
- **Root cause (commit 68239b7):** `_prewarm_caches()` llamaba `_get_all_products_cached(include_all=True)` que descarga TODOS los statuses (activos + pausados + cerrados + inactivos + bajo_revision). Con miles de items historicos cerrados en ML, el fetch tardaba > 3 minutos. El JS hacia polling por max 3 min (36 intentos) y luego mostraba "Reintentar" — pero sin hacer nada automaticamente. El prewarm seguia corriendo sin cache poblado.
- **Fix:** (1) `include_all=True` → `include_paused=True`: stock issues solo necesita active+paused, los cerrados no requieren gestion de stock. (2) `asyncio.wait_for(timeout=150s)`: si el prewarm no termina en 150s, aborta con error claro. (3) JS: auto-recarga cuando hay error (antes solo boton manual); al agotar intentos (200s > 150s timeout) fuerza recarga para relanzar prewarm.
- **Razon de include_all original:** no habia, era excesivo desde el inicio.

### BUG — BM Disp=1 para SNTV006485 cuando la unidad esta reservada (MLM758116253)
- **Sintoma:** Inventario mostraba BM Disp=1 para Smart TV Hisense 50" (SKU SNTV006485), apareciendo en "Ventas Perdidas". BM tiene 1 unidad fisica en MTY con Reserve=1 — la unica unidad esta reservada, no disponible.
- **Root cause (commit ce9513d):** `_store_wh` detectaba "reserve excede vendible" con `old_formula == 0`. Pero `old_formula = max(0, fisica-reserve) = 0` cuando `fisica == reserve`, no solo cuando `reserve > fisica`. Para SNTV006485: fisica=1, res=1 -> old=0; formula asumia erroneamente que la reserva estaba en bins no-vendibles -> avail=min(1,384)=1 (incorrecto).
- **Fix:** Condicion cambiada a `reserve > warehouse_total` (estrictamente mayor). Solo cuando reserve EXCEDE el stock fisico vendible es imposible que toda la reserva este contra ese stock. En todos los demas casos (reserve <= fisica), formula conservadora: `max(0, fisica - reserve)`.
- **Casos verificados:** SNTV005554 (res>fisica), SNTV002033, SNTV001764, SNTV006485 (ahora=0), SNAC000029 — todos correctos.

### BUG — Sync pone en 0 items de bundle por SKU compuesto (MLM1336870147, MLM892546286)
- **Síntoma:** ML items de TV+accesorio quedaban en qty=0 después de cada sync, aunque SNTV001864 tiene stock en BM.
- **Root cause (commit 894857f):** `_base_sku()` hacía `sku.upper().split("-")[0]`. Para bundles con SELLER_SKU compuesto (`"SNTV001864 + SNPE000180"`, `"SNTV001864 / SNWM000001"`), el split por `-` no cambiaba nada y mandaba el string completo a BM. BM no encontraba ese SKU → devolvía 0 → sync ponía qty=0.
- **Fix:** `_base_sku()` ahora detecta separadores de bundle (espacio, `+`, `/`) y extrae el primer token SKU reconocible via regex `[A-Z]{2,8}\d{3,10}`. Casos simples y con sufijo `-FLX` no cambian.
- **Verificado:** todos los casos de prueba pasan: bundles `+`, `/`, espacio, sufijo `-FLX01`, SKU simple.

### BUG RAÍZ — BM Disp=0 en Inventario + Stock prewarm infinito (mismo bug)
- **Síntoma 1:** Tab Inventario mostraba BM Disp=0 para todos los items aunque BM tenía stock (ej: SNAC000029 tiene 2,467 unidades).
- **Síntoma 2:** Tab Stock quedaba en spinner infinito — el prewarm nunca completaba.
- **Root cause (commit 322f845):** `_get_bm_stock_cached` construía `to_fetch` sin deduplicar SKUs. Con 6413 productos donde SNAC000029 aparece 100+ veces, lanzaba 100+ llamadas concurrentes a BM para el MISMO SKU. BM rate-limitaba → todas fallaban → `_store_empty` escribía 0 → dato correcto perdido. El mismo flood causaba que el prewarm tardara eternamente o fallara.
- **Fix:** `_seen_to_fetch: set` en el loop de `_get_bm_stock_cached` — cada SKU se consulta en BM exactamente 1 vez. Con 6413 productos y ~300 SKUs únicos, pasa de 6413 → ~300 llamadas. Sin duplicados = sin race conditions = sin rate limiting.
- **Verificado localmente:** todos los productos con mismo SKU reciben el dato correcto porque `_apply_bm_stock` hace lookup por SKU en `result_map` que tiene 1 entrada por SKU único.

### BUG — Stock tab spinner infinito (nunca carga)
- **Síntoma:** Tab Stock mostraba el spinner "Calculando stock en background..." indefinidamente y nunca cargaba los datos, incluso después de minutos de espera.
- **Root cause:** El auto-retry (setTimeout 20s en el loading HTML) disparaba un nuevo `asyncio.create_task(_prewarm_caches())` sin verificar si ya había uno corriendo. Con retry cada 20s y prewarm que tarda 60-90s, se acumulaban 3+ prewarms concurrentes saturando BM API → rate-limit de BM → todos fallaban silenciosamente (`except Exception: pass`) → cache nunca se llenaba → spinner infinito.
- **Fix (commit 08084e4):**
  1. `_prewarm_running` flag global: solo 1 prewarm corre a la vez; si ya hay uno activo, `_prewarm_caches()` retorna inmediatamente sin saturar BM.
  2. `_prewarm_error` captura el traceback completo en lugar de `pass` silencioso.
  3. `GET /api/stock/prewarm-status`: endpoint de polling que devuelve `{running, ready, error}`.
  4. Loading HTML: polling activo cada 5s via `fetch()` en lugar de `setTimeout` ciego; cuando `ready=true` carga automáticamente; si hay error lo muestra con botón Reintentar.

### BUG — Stock tab HTTP 502 en cache fría
- **Síntoma:** Al abrir el tab Stock (especialmente tras reinicio en Railway) aparecía "Error: HTTP 502 — Reintentar" en lugar del contenido
- **Root cause:** El endpoint `/partials/products-stock-issues` solo devolvía loading state cuando el prewarm task estaba activo (`not _prewarm_task.done()`). Si el prewarm ya terminó pero la cache sigue vacía (prewarm falló o no había sesión al arrancar), el endpoint ejecutaba el cálculo completo sincrónicamente (60-90s) → Railway lo mataba al llegar al límite de 30s → 502.
- **Fix (commit 2ddff7f):** El endpoint ahora SIEMPRE devuelve loading state cuando no hay cache válida, lanza `_prewarm_caches()` en background, y espera a que el usuario recargue. Nunca hace el cálculo pesado dentro del request HTTP. Código muerto eliminado (110 líneas).

### FEAT — Sección E Stock Issues: FULL Sin Stock → alerta para cambiar a Merchant
- **Regla:** Los productos FULL se deben dejar en FULL. Solo si se quedan sin stock en ML pero hay disponible en BM → alerta para cambiar a Merchant y seguir vendiendo.
- **Fix lateral:** Secciones A (Reabastecer) y C (Activar) ahora excluyen FULL — esas secciones son solo para Merchant.
- **Nueva Sección E (commit 97b964b):** filtro `is_full=True AND ML=0 AND BM>0`
  - KPI card cyan en el header
  - Tabla desktop + cards mobile con badge FULL
  - Botón "Cambiar a Merchant →" abre el listing directamente en ML
  - No tiene acciones automáticas — requiere acción manual en panel ML
- **DECISION:** FULL items: mantener en FULL siempre. Si se agotan → cambiar a Merchant temporalmente para no dejar de vender el stock de bodega.

### FEAT — `_bm_conditions_for_sku`: bundle "/" usa GRA,GRB,GRC,ICB,ICC,NEW
- **Regla:** `SELLER_SKU` con "/" (ej: `SNTV002033 / SNWM000001`) = señal para usar condiciones completas. El SKU después del "/" es solo referencia, NO se consulta en BM.
- **Verificado MLM843286836:** VAR "Base de Pared" → física=88 (incluye 3 IC), avail=59 vs VAR "Base de Mesa" → física=85, avail=56
- **Fix (commit 50cb9f1):** `if "/" in upper: return "GRA,GRB,GRC,ICB,ICC,NEW"`

### FIX — Vista Deals: botón BM usa disponible neto, no físico bruto
- **Síntoma:** Botón `BM:86` en la vista de items/deals pre-llenaba el campo de stock con el físico total (incluía reservas). Podría causar oversell si se confirmaba sin revisar.
- **Fix (commit 7980552):**
  - `_fetch_inv` ahora hace llamada paralela a `Get_GlobalStock_InventoryBySKU` para obtener `Reserve` y `TotalQty`
  - Aplica fórmula híbrida idéntica a `_store_wh` → campo `avail` en `inventory_map`
  - Template `items_grid.html`: badge azul `Disp:X` aparece cuando disponible ≠ físico
  - Botón `BM:X` usa `avail` (neto) en lugar de `total` (bruto)

### FIX — Fórmula híbrida BM available: resuelve SNTV005554 y SNTV002033
- **Síntoma:** Dos comportamientos contradictorios en la misma fórmula:
  - SNTV005554: física=2, reserve_global=3 → old formula `max(0, 2-3)=0` ✗ (BM tiene 2, las 3 reservas son de bins no-vendibles)
  - SNTV002033: física=86, reserve_global=30 → new formula `min(86, 863-30)=86` ✗ (BM UI muestra 59, las 30 reservas son locales en los bins vendibles)
- **Root cause:** No existe un campo per-location reserve en la API de BM. El `Reserve` del endpoint global no distingue si las reservas están en bins vendibles o no-vendibles.
- **Fix (commit 753c144):** Fórmula híbrida en `_store_wh`:
  - `old = max(0, physical - reserve_global)`
  - Si `old == 0` y `global_avail > 0` → reservas están fuera de vendible → `avail = min(physical, global_avail)`
  - Si `old > 0` → reservas son locales → `avail = old` (resta directa)
- **Resultados:**
  - SNTV005554: `old=0, global_avail=397 > 0 → min(2, 397) = 2` ✓
  - SNTV002033: `old=56 > 0 → avail=56` (≈59 BM UI, diff de 3 por unidades ICB/ICC no contadas en GR-only)
  - SNTV001764: `old=217 > 0 → avail=217` (≈221 BM UI) ✓
- **Regla aprendida:** Cuando reserve_global > physical_vendible, las reservas DEBEN estar en bins no-vendibles (lógica de conservación física). Cuando reserve_global ≤ physical_vendible, asumimos reservas locales y restamos.

---

## 2026-04-02

### BUG — SKU incorrecto persistía en alertas Riesgo Sobreventa (dos lugares sin parchear)
- **Síntoma:** MLM1493302754 seguía mostrando SNTV002695 (padre) en lugar de SNTV005554 (variación) en el panel de alertas, a pesar de haberse "arreglado" en sesión anterior
- **Root cause:** El fix de `_get_item_sku` se aplicó en algunos lugares pero quedaron dos sin parchear:
  1. Loop de `_run_stock_sync_for_user` (~línea 7072): usaba `body_dict.get("seller_custom_field")` directo
  2. `item_edit_modal` (~línea 4353): misma extracción directa del padre
- **Fix (commit b9110c1):** Ambos reemplazados por `_get_item_sku(body_dict)` / `_get_item_sku(item)`
- **Regla aprendida:** Al corregir un bug, siempre hacer grep exhaustivo de TODAS las variantes del patrón defectuoso en el codebase completo antes de cerrar el fix

### BUG — Botón "Sync ahora" del panel rojo no hacía nada
- **Síntoma:** Clic en "Sync ahora" dentro del panel de alertas de sobreventa no producía ninguna acción visible
- **Root cause:** `triggerStockSync()` y `toggleAutoPause()` estaban declaradas como `function` normales en el script inline del panel. Cuando htmx re-ejecuta scripts vía `innerHTML` swap, las declaraciones `function` no quedan en el scope global y el `onclick` no las encuentra
- **Fix (commit de5fc73):** Cambiadas a `window.triggerStockSync = function()` y `window.toggleAutoPause = function()` para garantizar scope global

### BUG — Tab Stock quedaba con spinner infinito
- **Síntoma:** Al hacer clic en el tab "Stock", el spinner amarillo giraba indefinidamente sin mostrar contenido ni error
- **Root cause:** El `fetch()` del tab no tenía timeout — si el endpoint tardaba mucho (caches vacíos post-restart de Railway) o retornaba error HTTP, el spinner nunca se resolvía
- **Fix (commit de5fc73):** Agregado `AbortController` con timeout de 90s. Si el endpoint tarda más o da error, muestra mensaje descriptivo con botón **Reintentar** en lugar de spinner infinito

### DECISION — Patrón `function foo()` vs `window.foo = function()` en scripts htmx
- En scripts cargados por htmx via `innerHTML` swap, las declaraciones `function foo()` pueden no quedar en el scope global
- Para cualquier función que se llame desde `onclick` en HTML generado por htmx, siempre usar `window.foo = function()` para garantizar acceso global

---

## 2026-04-01

### OPERACION — Prueba de sincronización con SKU SNTV007398
- Consultado stock en BM: 9 unidades disponibles (8 GRB + 1 GRC en Monterrey MAXX, LocationID 68)
- RetailPrice PH = $248 USD (campo `LastRetailPricePurchaseHistory`)
- 9 listings encontrados en 4 cuentas ML: APANTALLATEMX, AUTOBOT MEXICO, BLOWTECHNOLOGIES, LUTEMAMEXICO
- 5 listings activados manualmente + qty=9 (los 4 pausados + 1 activo ajustado)
- BLOWTECHNOLOGIES (MLM2412984945, MLM2463319257): FULL (fulfillment) — ML controla stock, no se puede modificar vía API
- LUTEMAMEXICO (MLM4960428688, MLM4964264896): under_review — bloqueados por ML

### BUG CRÍTICO — sync multi apagaba todo (avail=0 siempre)
- **Síntoma:** el sync ponía qty=0 en todos los listings ML cada 5 minutos
- **Root cause:** `GlobalStock_InventoryBySKU_Condition` devuelve un objeto `{}` único,
  no una lista `[{}]`. El código hacía `if not isinstance(rows, list): rows = []`
  → avail siempre 0 → todo ML quedaba en qty=0
- **Fix (commit 3aeb338):** normalizar respuesta BM — si es `dict`, envolver en lista antes de iterar.
  También agregar manejo de `SKUCondition_JSON` como string doble-serializado.

### FEAT — sync multi recopila listings pausados + auto-activa
- **Commit c08c0df**
- `_collect_ml_listings`: ahora recopila `active` + `paused` (antes solo `active`)
- Detecta FULL items (`logistic_type=fulfillment`) → `can_update=False`
- `_execute`: si `new_qty > 0` y listing está `paused` → activa primero (`PUT status=active`), luego setea qty
- `_plan`: bm_avail=0 → skip pausados (ya apagados); concentrate loser pausado → skip
- Regla fija: NUNCA pausar. BM=0 → qty=0 en activos. BM>0 → activar pausados + setear qty.

### FIX — eliminar todos los botones "Pausar" de templates
- **Commit cb83082**
- `products_stock_issues.html`: eliminados `pauseItem()` y `bulkPauseRisk()`, fix `bulk-zero-msg` ID
- `items.html`: `triggerSyncNow()` apunta a `/api/stock/multi-sync/trigger` y `/status`
- `amazon_dashboard.html`: bulk action `'pause'` → `'set_qty_zero'`, label "Qty 0"
- `items_health.html`: toggle activo→apagado llama `PUT /api/items/{id}/stock {qty:0}` en lugar de status

### FIX — panel de alertas: reemplazar Pausar + mostrar SKU
- **Commit 1f602ee**
- Botón "Pausar" en alertas llamaba `closeItem()` = `DELETE /api/items/{id}` (cerraba permanentemente el listing)
- Reemplazado por "Qty 0" → `PUT /api/items/{id}/stock {quantity:0}`
- SKU ahora visible como badge naranja en cada fila de alerta

### FIX — BM retail/cost + sync conflicts + system health
- **Commit ac0a238**
- `_enrich_with_bm_product_info`: añadir `NEEDRETAILPRICEPH`, `NEEDRETAILPRICE`, `NEEDAVGCOST` al payload
- Fallback: `_bm_retail_price = retail_ph if retail_price == 0` (RetailPrice con SEARCH= siempre 0)
- Amazon `_enrich_bm_amz`: añadir 3ra call a InventoryReport para obtener `_bm_retail_ph` y `_bm_avg_cost`
- Sync viejo: eliminar auto-zero del `_stock_sync_loop` (evita conflicto con nuevo multi-sync)
- `system_health._check_stock_sync`: migrado a `get_sync_status()` del nuevo multi-sync

### DECISION — RetailPrice BM
- `RetailPrice` con query `SEARCH=` siempre devuelve 0 aunque el SKU tenga precio
- Campo correcto: `LastRetailPricePurchaseHistory` (requiere `NEEDRETAILPRICEPH: true`)
- Esto SÍ funciona con `SEARCH=` — verificado con SNTV007398 ($248 USD)
- `AvgCostQTY = 9999.99` es valor placeholder (sin costo real registrado)

### DECISION — NUNCA pausar listings en ML ni Amazon
- Pausar daña el algoritmo de ranking de ML y Amazon
- Siempre usar `PUT /api/items/{id}/stock {quantity: 0}` para "apagar" un listing
- Para Amazon: `update_listing_quantity(sku, 0)`
- Exception: FULL (fulfillment) — no se puede modificar vía API, ML controla el stock

### BUG — dashboard mostraba BM: 0 para todos los productos
- **Síntoma:** columna BM stock siempre 0 en inventario, alertas de "riesgo sobreventa" erróneas (65 falsos positivos)
- **Root cause 1:** `_get_bm_stock_cached` en `main.py` — mismo bug que sync multi:
  `GlobalStock_InventoryBySKU_Condition` devuelve `{}` (objeto), el código hacía
  `if not isinstance(cond_rows, list): cond_rows = []` → `avail_total = 0` siempre
- **Root cause 2:** `_store_wh` — cuando `SKUCondition_JSON` está ausente (BM lo omite en SKUs
  con muchas unidades), `avail_total = 0` aunque `TotalQty` por condición sí viniera
- **Fix (commit 7da669d):**
  - Normalizar `cond_rows`: si es `dict`, envolver en lista antes de iterar
  - Fallback en `_store_wh`: si `SKUCondition_JSON` vacío → usar `TotalQty` del nivel condición

### BUG CRÍTICO — SKU incorrecto en items con variaciones (riesgo de pérdidas)
- **Síntoma:** MLM1493302754 mostraba SKU=SNTV002695 (padre) pero las variaciones tienen SKU=SNTV005554
- **Root cause:** `_get_item_sku` y todos los puntos de extracción usaban `seller_custom_field` del
  padre primero — ML permite que el padre tenga un SKU distinto al de sus variaciones.
  El SKU del padre puede ser completamente equivocado (otro producto diferente).
- **Impacto potencial:** BM lookup con SKU incorrecto → stock=0 falso → sync podía poner qty=0 en
  listings con stock real → pérdidas, reclamos, cierre de cuenta.
- **Fix (commit 7b7f889):** en 4 lugares: `_get_item_sku`, items grid (x2), `_collect_ml_listings` sync.
  Lógica: si item tiene variaciones → SKU real en variaciones. `seller_custom_field` del padre ignorado.
- **Regla:** para items con variaciones SIEMPRE usar SKU de la primera variación, nunca el del padre.

### BUG — _bm_avail contaba reservados como disponibles (301 en lugar de 221)
- **Síntoma:** SNTV001764 mostraba 301 disponibles, BM UI muestra 221 (Reserve=84 son órdenes pendientes)
- **Root cause:** `_store_wh` sumaba stock físico total sin restar `Reserve`
- **Fix (commit 70a9bb9):**
  - `_wh_phase`: llamada paralela a `Get_GlobalStock_InventoryBySKU` para obtener `Reserve`
  - `avail_total = max(0, warehouse_physical - reserve_global)`
  - Eliminado Condition endpoint (redundante); resultado: 301-84=217 ≈ BM UI 221
- **Regla:** `_bm_avail` = stock vendible SIN reservas. `_bm_total` = stock físico bruto.

### OPERACION — Verificación SKU SNTV001764 (Onn 32" HD Roku Smart TV)
- BM UI muestra: Available=221, Reserve=84 (filtro LocationIDs 47/62/68), RetailPrice PH=$88 USD
- Dashboard mostraba BM=0 por bug → corregido 7da669d; luego reservas no restadas → corregido 70a9bb9

---

## 2026-06-10 — Returns Board: Unified Cross-Platform View + Fixes

### FIX — Order lookup cap too small → "Sin título" aggregation failure
- **Root cause:** `max_orders = max(limit * 2, 30)` = 30 cap; accounts with many claims had orders not fetched → each claim got title `"Sin título (Reclamo #...)"` → N unique entries instead of grouped SKUs
- **Fix:** `max_orders = max(limit * 6, 120)` in both `returns_top_products` (line ~12609) and `returns_global_top` (line ~12915)
- **Result:** Top SKUs now aggregate correctly by SKU key

### FEATURE — `/api/returns/unified-top` endpoint
- Combines ML claims + Amazon refunds across ALL accounts in a single response
- ML: fans out to all accounts (Semaphore 2), fetches claims + order info (Semaphore 3, cap 120), groups by SKU
- Amazon: fans out to all seller accounts (Semaphore 2), calls `get_refunds_detail()` via 3h cache
- Response per SKU: `{title, sku, count, opened, closed, reasons, accounts, platforms: {ml, amazon}, sale_amount_mxn, refund_usd, retail_ph_unit, pct_of_total}`
- Parameters: `days`, `limit`, `platform` (all/ml/amazon)
- Amazon cache: `_amz_refunds_cache` with `_AMZ_REFUNDS_TTL = 3600 * 3`
- Amazon reason map: `_AMZ_REASON_MAP` + `_amz_reason_label()` — ready for future Reports API integration

### FEATURE — Returns board redesign (returns.html)
- **Layout reorder:** KPI → Top SKUs (auto-load, no "click Analizar") → Global view → Timeline → Table
- **Platform toggle in global view:** Todas / ML / Amazon buttons → `_globalPlatform` state → `setGlobalPlatform(plat)` → re-fetches unified-top
- **Platform badges per SKU:** ML yellow `bg-yellow-100`, Amazon orange `bg-orange-100`
- **`setRetMode()`:** now uses element IDs (`ret-top-card`, `ret-timeline-card`) instead of class selectors
- **`loadGlobalTop()`:** calls `/api/returns/unified-top` instead of legacy `/api/returns/global-top`
- **`_renderGlobalProducts()`:** shows ML count + Amazon count badges + refund_usd when > 0

### FEATURE — Dashboard widget (multi_dashboard.html)
- `loadReturnsWidget()` now calls `/api/returns/unified-top?days=30&limit=5&platform=all`
- Badge shows `N ML · N AMZ` account counts
- Product cards show ML/Amazon platform badges

### ARCHITECTURE NOTE — Amazon reasons
- Financial Events API (`get_refunds_detail`) does NOT include return reason codes
- `_AMZ_REASON_MAP` is in place for when Reports API `GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE` is used
- Current Amazon entries show generic "Devolución Amazon" reason label

---

## 2026-06-10 — FEAT: ASIN Search en Amazon Ventas

### Commits: c0fdedc, 7c4540b

### Feature: Búsqueda por ASIN en la sección Ventas de Amazon

Nueva tarjeta de búsqueda en `amazon_dashboard.html` tab Ventas (antes de "Últimas Órdenes"):
- Input ASIN (10 chars) + selector días (7/15/30/90) + botón Buscar / Enter
- Routing automático al marketplace de la cuenta activa (MX → A1AM78C64UM0Y8, US → ATVPDKIKX0DER)
- Funciona para TODAS las cuentas (México y USA)

### Endpoint `GET /api/amazon/asin-search`
- `asin` (required), `seller_id` (optional, usa cuenta activa si vacío), `days` (7–365)
- Llama en paralelo via `asyncio.gather`:
  1. `client.get_catalog_item(asin)` → Catalog Items API v2022-04-01 (`summaries,images,attributes,dimensions,identifiers`)
  2. `client.get_order_metrics(..., granularity="Day", asin=asin)` → Sales API v1, desglose diario
  3. `client.get_order_metrics(..., granularity="Total", asin=asin)` → Sales API v1, totales del período
- DB lookup: `amazon_listings` para precio/estado actuales del ASIN (si está en catálogo)
- Respuesta: `{asin, days, seller_id, marketplace, product, listing, totals, daily}`

### Cambios en `amazon_client.py`
- `get_order_metrics()`: nuevos params opcionales `asin: str = None` y `sku: str = None` (pasan como query params al Sales API)
- `get_catalog_item()` (nueva versión): `includedData=summaries,images,attributes,dimensions,identifiers`; retorna `{}` en error (vs la versión anterior que retornaba `None`)

### Frontend `amazon_dashboard.js`
- `window.searchAsin()`: valida ASIN 10 chars, llama endpoint, muestra `_renderAsinResult(d)`
- `_renderAsinResult(d)`: tarjeta con imagen, título, marca, badge marketplace, badge "en catálogo", 4 chips KPI (Unidades/Órdenes/Revenue/Precio Prom.), tabla diaria con mini barras de progreso, links SC y Amazon.com

### BUG FIX — aiosqlite NameError → 500 en endpoint
- **Bug:** `aiosqlite.connect()` usado sin import local → `NameError` → 500 Internal Server Error → frontend recibía HTML → `r.json()` lanzaba "Unexpected token I, Internal S..."
- **Fix (commit 7c4540b):** `import aiosqlite as _aio_as` dentro del try block del endpoint

---

## 2026-06-12 — FEAT: ASIN Search v2 — Ofertas competitivas + BSR + tarjetas de decisión

### Commit: f3f70ad

### Contexto
Antes el ASIN search solo mostraba info del catálogo + ventas propias. El usuario quería ver el comportamiento del ASIN en todo Amazon (otros vendedores, demanda, precio). SP-API no provee ventas totales del marketplace, pero sí: ofertas competitivas (Pricing API) y BSR (Catalog API) como proxies de demanda.

### Nuevos datos en `/api/amazon/asin-search`
- **Pricing API** `get_item_offers(asin)`: buy box price, lista de todos los vendedores activos (precio, FBA/FBM, Prime, feedback, buy box winner)
- **BSR (salesRanks)** añadido a `includedData` de Catalog Items API: rank por categoría (classificationRanks) y display group (displayGroupRanks)
- Respuesta ahora incluye: `offers` (buy_box_price, total_offers, list_price, sellers[]) y `product.bsr[]`

### Nuevo método `get_item_offers()` en amazon_client.py
- `GET /products/pricing/v0/items/{asin}/offers` con `MarketplaceId` e `ItemCondition=New`
- Retorna `{}` en error (graceful)

### Rediseño frontend `_renderAsinResult()` en amazon_dashboard.js
1. **Header**: imagen, ASIN badge, badge MX/US, "En tu catálogo" si aplica, título, marca, modelo, P. lista
2. **BSR strip**: top ranks con badge color-coded por tier (verde/amarillo/rojo), estimado uds/mes
3. **4 KPI chips**: Buy Box price + descuento%, # vendedores + señal competencia, Tus uds, Tu revenue
4. **3 tarjetas de decisión**:
   - 📊 Demanda: tier (Muy alta/Alta/Media/Moderada/Baja) con BSR y estimado mensual
   - 🏆 Competencia: # vendedores, buy box price, badges FBA/Prime, reviews del winner
   - 🏬 Tu posición: publicado/no publicado, tu precio vs buy box, SKU
5. **Tabla vendedores**: precio, envío, FBA/FBM, buy box winner, Prime, feedback
6. **Tus ventas**: tabla diaria con mini barras (solo si hay ventas propias)
7. **Links**: Ver en Amazon + Ver en SC (si en catálogo)

### Helper functions añadidas
- `_bsrTier(rank)`: mapea BSR → { label, color, est } con estimados por rango
- `_tierCls(color)`: devuelve clases Tailwind para badge verde/amarillo/rojo/gris

