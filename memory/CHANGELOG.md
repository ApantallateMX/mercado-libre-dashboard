# Changelog - Mercado Libre Dashboard

## 2026-03-09 — feat: infraestructura core de sistema de agentes IA

### Archivos creados
- `app/services/agents/__init__.py` — módulo vacío
- `app/services/agents/base.py` — `AgentResult` dataclass + `BaseAgent` ABC con tool-use loop completo
- `app/services/memory_manager.py` — `MemoryManager` con tablas `agent_memory`, `agent_conversations`, `agent_alerts`; singleton `memory_manager`
- `app/services/scheduler_service.py` — `SchedulerService` wrapper de APScheduler + tabla `agent_jobs`; singleton `scheduler_service`; graceful no-op si APScheduler no está instalado

### Detalles técnicos
- `BaseAgent.run()` implementa el loop tool-use de Claude (máx 10 iteraciones)
- Llama a Anthropic API via httpx directo (sin SDK), modelo `claude-sonnet-4-6`
- `MemoryManager`: upsert semántico para kv, historial por `session_id`, alertas con niveles info/warning/critical
- `SchedulerService`: soporta triggers `cron` e `interval`; `record_run()` para actualizar last_run/last_result

## 2026-03-05 (4) — Fix: separar detección FULL vs variaciones en update_item_stock (150a14e)

### Diagnóstico MLM4688917228
- Item actualmente tiene `logistic_type: cross_docking` (NO FULL), PUT retorna 200 sin errores
- El "Es FULL" anterior ocurrió porque el item WAS FULL en el momento del test del usuario
- Bug confirmado: `_VAR_ERROR_KEYWORDS` incluía "not_modifiable" → items FULL entraban al path de variaciones

### Fix: meli_client.py `update_item_stock`
- Separado `_FULL_KEYWORDS` ("logistic_type.not_modifiable", "fulfillment") → eleva `full_item` error claro
- `_VAR_ERROR_KEYWORDS` ahora solo tiene keywords de variaciones genuinas (quitado "not_modifiable")
- FULL items ya NO intentan actualizar variaciones inútilmente

### Fix: items.py `update_stock` endpoint
- Nuevo handler para `error: "full_item"` → HTTP 400 con mensaje claro de FULL
- JS detecta "not_modifiable" o "fulfillment" en el mensaje → muestra "Es FULL" correctamente

## 2026-03-05 (3) — Docs: Agentes actualizados con lecciones de la sesión

### mercadolibre-strategist.md — Nueva sección 9: Comportamiento técnico API MeLi
- catalog_listing:true → NO sync manual (stock por depósito MeLi)
- cross_docking + lost_me1_by_user → MeLi revierte stock en ~3s sin ME1 activo
- sub_status: out_of_stock = auto-pausa, NO igual a pausa manual
- SKU en variaciones requiere include_attributes=all en batch fetch
- Listings sincronizados con otro → solo un listing activo recibe depósito
- logistic_type: fulfillment no modificable via API
- Tabla 4 cuentas activas (APANTALLATEMX, AUTOBOT, BLOWTECHNOLOGIES, LUTEMAMEXICO)

### binmanager-specialist.md — Nueva sección: Restricciones sync con MeLi
- Tabla tipo listing vs posibilidad de sync desde BM
- Protocolo lost_me1_by_user: acepta PUT pero revierte en 3s
- Variaciones: consultar siempre por SKU específico de la variación

## 2026-03-05 (2) — Fix: ME1 warning detection en sync stock MeLi

### Bug Fix — Sync Stock cross_docking items
- `meli_client.update_item_stock`: detecta warning `lost_me1_by_user` en response de MeLi
  Items removidos de FULL quedan en cross_docking; sin ME1 activo, MeLi revierte el stock en ~3s
  Ahora lanza MeliApiError(422, "me1_required") en lugar de retornar ok silencioso
- `items.py /stock`: maneja `me1_required` con HTTP 422 y mensaje descriptivo
- `items.html fmSyncStock`: detecta error ME1 → muestra aviso inline en modal con link a SC
  (no toast generico — mensaje persistente con contexto completo)
- MLM3848757056: verificado que tiene `sub_status: out_of_stock` (auto-pausa por sin stock,
  NO pausa manual) — estado correcto, no requiere restauracion

## 2026-03-05 — Feat: Fulfillment Management Universal Amazon + Agentes IA

### Fulfillment Management
- Boton ⚡ universal en TODAS las filas del tab Inventario Amazon (no solo filas con issue)
- Reemplaza botones condicionales `{% if action_needed %}` — ahora disponible siempre
- Modal `fm-modal` con 4 acciones: Pausar, Cambiar a Merchant, Actualizar qty FBM, Reactivar FBA
- Alerta visual: boton rojo+pulso cuando BM disponible=0 y Amazon stock>0
- "Usar BM" autocompleta cantidad desde `_bm_avail` del producto
- Recarga automatica del tab despues de 1.5s al ejecutar accion exitosa

### Backend nuevo
- `app/services/amazon_client.py`: `update_listing_fulfillment(sku, action, quantity)`
  - `pause` / `set_merchant` / `set_qty` → DEFAULT channel
  - `reactivate_fba` → AMAZON_NA channel
  - productType obtenido dinamicamente del listing actual
- `app/api/amazon_products.py`: `POST /api/amazon/products/{sku}/fulfillment-action`
  - `FulfillmentActionBody(action, quantity)` + `_VALID_FA` validation
  - Invalida `_listings_cache`, `_fba_cache`, `_flx_stock_cache` post-accion

### Agentes IA creados
- `Agentes/amazon-specialist.md` — Seccion 8: Fulfillment Management completa
- `Agentes/binmanager-specialist.md` — BM endpoints, LocationIDs, Available vs Reserved
- `Agentes/financial-analyst.md` — Revenue MeLi/Amazon, margenes, P&L
- `Agentes/mercadolibre-strategist.md` — existente (sera actualizado con scraper)

### Scrapers y update scripts creados
- `scraper_amazon_help.py` — Playwright, SC MX help (~40 secciones) + SP-API docs (~70 paginas)
- `scraper_meli_docs.py` — httpx+BS4, ~130 URLs de developers.mercadolibre.cl
- `scraper_binmanager.py` — Playwright + auto-login BM, intercepta API interna
- `update_agent_with_knowledge.py` — inyecta 160K chars en amazon-specialist
- `update_meli_agent.py` — inyecta 120K chars en mercadolibre-strategist
- `update_bm_agent.py` — inyecta 100K chars en binmanager-specialist

### Archivo maestro
- `mercado-libre-dashboard.md` — creado con TODOS los logins, credenciales, endpoints, estado
- `.gitignore` — agregado `mercado-libre-dashboard.md` y `memory/` (proteccion credenciales)

### Archivos modificados
- `app/templates/partials/amazon_products_inventario.html` — tabla w-full text-xs, modal fulfillment
- `app/services/amazon_client.py` — update_listing_fulfillment()
- `app/api/amazon_products.py` — /fulfillment-action endpoint
- `Agentes/amazon-specialist.md` — seccion 8 fulfillment management
- `mercado-libre-dashboard.md` — secciones 11/12/13 actualizadas

---

## 2026-02-20 — Fix: 4 cuentas MeLi persistidas en .env.production

### Cuentas registradas (permanentes)
- Slot 1: APANTALLATEMX (523916436)
- Slot 2: AUTOBOT MEXICO (292395685)
- Slot 3: BLOWTECHNOLOGIES (391393176)
- Slot 4: LUTEMAMEXICO (515061615)
- Tokens frescos al 2026-02-20 — se auto-actualizan en cada uso
- Commit: `5eadb5b`

---

## 2026-02-20 — Feat: Multi-cuenta dinámico (N cuentas sin límite)

### Cambio
- Sistema hardcodeado para 2 cuentas → dinámico para N cuentas
- Slot 1: `MELI_USER_ID` / `MELI_REFRESH_TOKEN` (backwards compat)
- Slots 2+: `MELI_USER_ID_N` / `MELI_REFRESH_TOKEN_N` (auto-descubiertos)
- `/auth/connect` en una nueva cuenta crea automáticamente su slot en `.env.production`
- Railway restaura TODAS las cuentas en cada redeploy sin intervención manual
- Commits: `6d36c7b`

---

## 2026-02-19 — Feat: Stock Reservado vs Disponible BinManager

### Problema
- BinManager UI muestra "Reserve" (físico total) y "Available" (no comprometido a órdenes)
- El código usaba QtyTotal (ForInventory:0 = Reserve) para actualizar MeLi → sobreventas
- SNAF000022: Reserve=19, Available=0 → se empujaban 19 a MeLi cuando debía ser 0

### Solución implementada
- `_get_bm_stock_cached()` ahora hace 2 llamadas paralelas por SKU: ForInventory:0 + ForInventory:1
- Cache almacena `avail_total` además de `total` (mty/cdmx/tj)
- `_apply_bm_stock()` propaga `_bm_avail` a todos los productos y variaciones
- Dashboard muestra columna "BM Disp./Total": disponible en verde/rojo + total/reservados en gris
- Botones Sync usan `_bm_avail` en lugar de `_bm_total`
- Si `avail=0` y `total>0`: badge "⚠️ Todo reservado" bloquea el sync
- Eliminado buffer 0.6 en todos los sync (innecesario con Available real)

### Archivos modificados
- `app/main.py` (funciones _get_bm_stock_cached, _apply_bm_stock, 2 lecturas cache)
- `app/api/items.py` (_bm_warehouse_qty con doble llamada paralela)
- `app/templates/partials/products_inventory.html` (UI + JS)
- `app/templates/items.html` (quickSyncBM sin buffer)

### Git tag backup
- `backup-pre-reserved-fix` → estado anterior funcional

### Nota pendiente
- ForInventory:1 es un proxy de "Available" — validar con BinManager IT si coincide exactamente
- Para SNAF000022: ForInventory:1=44 no coincide con Available=0 en UI → puede haber discrepancia en algunos casos

## 2026-02-19 — Feat: Migración completa Product Ads API v2 (Feb 2026)

### Causa raíz bloqueante (no es código)
- App "CLAUDE" (ID: 7997483236761265) tiene `certification_status: not_certified`
- MeLi bloquea TODOS los writes en Product Ads para apps no certificadas
- Permisos OAuth correctos, portal DevCenter correcto — la certificación es el único bloqueante

### Cambios en `app/services/meli_client.py`
- Añadidos helpers `_ads_get()`, `_ads_put()`, `_ads_post()` con header `api-version: 2`
- `get_ads_campaigns()` → nuevo endpoint `/marketplace/advertising/MLM/advertisers/{adv_id}/product_ads/campaigns/search` con `roas` en metrics
- `get_ads_campaign_detail()` → `/advertising/MLM/product_ads/campaigns/{id}`
- `update_campaign()` → `/marketplace/advertising/MLM/product_ads/campaigns/{id}` (sin advertiser_id), acepta `roas_target`
- `create_campaign()` → `/marketplace/advertising/MLM/advertisers/{adv_id}/product_ads/campaigns`, añade `strategy`, `channel: marketplace`, `roas_target`
- `assign_items_to_campaign()` → bulk PUT `/marketplace/advertising/MLM/advertisers/{adv_id}/product_ads/ads` (hasta 10k items), con fallback individual
- `get_ads_items()` → `/advertising/MLM/advertisers/{adv_id}/product_ads/ads/search` con `roas` en metrics
- `get_all_ads_item_ids()` → mismo endpoint sin metrics (no requiere fechas)
- NUEVO `update_ad_item_status()` → PUT `/marketplace/advertising/MLM/product_ads/ads/{item_id}` con `status` + opcional `campaign_id`

### Cambios en `app/main.py`
- NUEVO `GET /api/ads/check-write-permission` → verifica `certification_status` + prueba PUT real
- NUEVO `POST /api/ads/item/{item_id}/status` → pausa/activa/reactiva un item individual en ads
- Fix: `check-write-permission` ahora usa `MELI_CLIENT_ID` de `app.config` (no `os.environ`)

### Cambios en `app/templates/ads.html`
- `#asignar-permisos-aviso`: reescrito para mostrar error de certificación con pasos claros
- NUEVO `toggleAdItem()`: pausa/activa item con feedback visual, detecta error de certificación
- `verificarPermisoAds()`: llama check-write-permission, actualiza UI con resultado
- `selectEstrategia()`: usa `profitability`/`increase`/`visibility` (antes `rentabilidad`/etc.) y ROAS (7x/4x/2x)
- `submitCrearCampana()`: envía `roas_target` y `strategy` (antes `acos_target`)
- `crearCampanaTopProductos()`: corregido para usar `profitability` (antes `rentabilidad`)
- Input `modal-camp-acos` → `modal-camp-roas` (range 1-35)

### Cambios en `app/templates/partials/ads_performance.html`
- Nueva columna "Accion" en tabla desktop y tarjetas mobile
- Botones Pausar/Activar por item (llaman `toggleAdItem()`)
- `id="perf-row-{item_id}"` en cada fila para actualización futura

### Endpoints deprecados por MeLi (efectivo Feb 26 2026) — YA MIGRADOS
- `GET /advertising/advertisers/{adv_id}/product_ads/items` → migrado
- `GET /advertising/product_ads/items/{item_id}` → migrado
- `GET /advertising/product_ads/campaigns/{id}/metrics` → migrado
- `GET /advertising/product_ads/ads/search` → migrado

### Acción pendiente del usuario
1. Crear nueva app en DevCenter bajo cuenta APANTALLATEMX, O
2. Certificar la app "CLAUDE" (7997483236761265) con MeLi
3. Actualizar `.env` con nuevas credenciales → re-autenticar via /auth/connect

---

## 2026-02-19 — Feat: Ads — Tab "Asignar a Campana" completo + check-write-permission

### Cambios adicionales (sesion 2)
- **`app/main.py`**: nuevo endpoint `GET /api/ads/check-write-permission`
  - Hace PUT inocuo (status=idle, campaign_id=0) sobre MLM1346239567
  - Retorna `write_enabled: true/false` segun si el error es 401 de permisos
  - Cualquier otro error (400/404) se interpreta como "write funciona" (problema diferente)
- **`app/templates/ads.html`**: funcion `verificarPermisoAds()` implementada
  - Llama al endpoint y muestra feedback instantaneo en la UI
  - Si permiso activo: oculta aviso de error y muestra "✓ Permiso ACTIVO"
  - Si no activo: muestra "✗ Permiso NO activo. Sigue los pasos indicados."

### Diagnostico confirmado (sesion 1 + 2)
- Token OK: `APP_USR-7997483236761265-021914-ccf2903eda6b7d146d...`
- Scopes OAuth correctos: `urn:ml:mktp:ads:/read-write` incluido
- GET funciona: item MLM1346239567 = status:idle, campaign_id:0
- PUT da 401 siempre: `com.mercadolibre.ads_search_pads_core.api.exceptions.UnauthorizedException`
- Causa raiz: permiso funcional "Advertising > Write" NO habilitado en portal MeLi

### Accion requerida por el usuario (NO es codigo)
1. Ir a https://developers.mercadolibre.com.mx/devcenter
2. Editar app "CLAUDE" (ID: 7997483236761265)
3. En Permisos/Scopes: cambiar Publicidad de "Solo lectura" a "Lectura y escritura"
4. Guardar cambios
5. Re-autenticar en /auth/connect
6. Usar boton "Verificar permisos ahora" en tab "Asignar a Campana"
7. Una vez verificado, asignar MLM1346239567 a campana 351749769 "001 TV 55 DWN Visibilidad"

## 2026-02-19 — Feat: Ads — Nuevo tab "Asignar a Campana" + endpoints API v2

### Investigacion realizada
- Explorado completamente el flujo de la seccion de ads (MeLi Product Ads)
- Item MLM1346239567 existe en ads con `status: idle` y `campaign_id: 0` (sin campana)
- GET en `/marketplace/advertising/MLM/product_ads/ads/{item_id}?api-version=2` funciona OK
- PUT da 401 "User does not have permission to write" — requiere re-autorizar la app
- La app tiene scope `urn:ml:mktp:ads:/read-write` configurado, pero el token existente no lo incluia

### Cambios
- **`app/templates/ads.html`**: nuevo tab "Asignar a Campana"
  - Busqueda por MLM ID con Enter key
  - Muestra titulo, precio, status actual, campana actual del item
  - Dropdown con todas las campanas disponibles
  - Boton Asignar con manejo de errores
  - Aviso claro cuando hay error de permisos con link a `/auth/connect`
- **`app/main.py`**: dos nuevos endpoints:
  - `GET /api/ads/item/{item_id}` — estado del item via marketplace API v2
  - `GET /api/ads/campaigns-list` — lista rapida de campanas (sin metricas)
- **`app/auth.py`**: scope OAuth actualizado con `urn:ml:mktp:ads:read-write`

### Para resolver el permiso de escritura
El usuario debe ir a `/auth/connect` para re-autorizar la app con los nuevos scopes.
El nuevo scope `urn:ml:mktp:ads:read-write` deberia incluirse en el nuevo token.

## 2026-02-18 — Fix: BM variaciones corregido → items multi-variacion ya no son falsos positivos en Riesgo

### Problema resuelto
MLM3018881010 aparecia en "Riesgo Sobreventa" con Stock MeLi=53 (suma total) y BM=0.
Pero Negro (SHIL000287) tenia BM=21 → no era riesgo real. El parent SKU (SHIL000286=Dorado)
solo tenia BM=0, ignorando las otras variaciones.

### Cambios
- **`app/main.py` `_get_bm_stock_cached`**: ahora fetcha BM de SKUs de CADA variacion para items con `has_variations=True`
- **`app/main.py` `_apply_bm_stock`**: para items con variaciones, suma BM de todas las variaciones individuales como `_bm_total`; cada `v["_bm_total"]` es el BM de esa variacion especifica
- **`app/templates/partials/products_stock_issues.html`**:
  - Risk desktop: columna Stock MeLi → "por var. 👁" para items con variaciones
  - Risk mobile: igual, muestra link en lugar del total confuso
  - `showVarStockPanel`: si variaciones tienen `_bm_total`, muestra columna BM+MeLi juntos

### Resultado
- Falso positivo eliminado: BM suma = 21 > 0 → item excluido de oversell_risk
- Items con TODAS variaciones BM=0 siguen apareciendo correctamente en Riesgo

---

## 2026-02-18 — Feat: Sync stock por variacion individual (items multi-variacion)

### Problema resuelto
MLM3018881010 (Dorado/Negro/Plateado): el boton "Sync 60%" consultaba el SKU del item
y distribuia proporcionalmente, resultando en 0 para todas las variaciones porque el SKU
del item no estaba en BM. Ahora cada variacion consulta su propio SKU.

### Cambios
- **`app/main.py`**: Nuevo endpoint `POST /api/items/{id}/sync-variation-stocks`
  → Consulta BM Warehouse por SKU de CADA variacion independientemente
  → Actualiza SOLO esa variacion (no toca las demas)
  → `PUT /api/items/{id}/stock` devuelve 409 si item tiene variaciones
- **`app/services/meli_client.py`**:
  → `update_item_stock()` lanza `ValueError` para items con variaciones
  → Nuevo `update_variation_stocks_directly(item_id, var_updates)`: PUT variaciones especificas
- **`app/templates/partials/products_stock_issues.html`**:
  → Boton "Sync Var. (N)" (indigo) en Restock y Risk para items con `has_variations=True`
  → Funcion `syncVariationStocks(itemId, btn)` muestra resultado por variacion en tooltip

---


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
