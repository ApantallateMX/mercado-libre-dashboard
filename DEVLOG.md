# DEVLOG — mercado-libre-dashboard

Log de actualizaciones, errores, soluciones y mejoras del proyecto.
Formato: `[FECHA] [TIPO] descripción`

Tipos: `FIX` `FEAT` `BUG` `DECISION` `OPERACION`

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
