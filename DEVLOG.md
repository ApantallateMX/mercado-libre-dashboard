# DEVLOG — mercado-libre-dashboard

Log de actualizaciones, errores, soluciones y mejoras del proyecto.
Formato: `[FECHA] [TIPO] descripción`

Tipos: `FIX` `FEAT` `BUG` `DECISION` `OPERACION`

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
