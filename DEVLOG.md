# DEVLOG — mercado-libre-dashboard

Log de actualizaciones, errores, soluciones y mejoras del proyecto.
Formato: `[FECHA] [TIPO] descripción`

Tipos: `FIX` `FEAT` `BUG` `DECISION` `OPERACION`

---

## 2026-08-21 — INCIDENTE CRÍTICO: sesión BM colgada zereó ~2,590 SKUs reales en las 5 categorías top-5

Jovan reportó con 2 casos reales (SHIL000522, SNTV007241) que "Riesgo
Sobreventa" y "Alertas de Stock" mostraban 0 disponible cuando BM (su
propia UI) mostraba cientos/miles de unidades reales.

**Causa raíz real (confirmada con logs de producción):** a las 20:03 las
5 categorías de mayor venta (Televisions, Air Conditioners, Interior
Lighting, Home Power Tools, Exercise Equipment) empezaron a fallar
SIMULTÁNEAMENTE con respuestas vacías en ~0.2-0.3s -- demasiado rápido
para ser BM procesando de verdad (una categoría real tarda 3-7s+). La
sesión compartida con BM (`_shared_bm`) se invalidó del lado del
servidor sin que `get_shared_bm()` lo detectara (HTTP 200 con lista
vacía, no un 401/redirect que dispare `_session_expired()`) -- el flag
`_logged_in` se quedó en `True` para siempre, reusando una sesión muerta.
El mecanismo de "2 fallas seguidas = 0 confirmado" (diseñado para
bloqueos reales de BM, ver incidente SNMP000002 del 2026-08-20) confirmó
en falso el 0 para las 5 categorías a las 20:18 (~2,590 SKUs) y volvió a
re-confirmar Interior Lighting a las 20:33.

**Investigación previa descartada correctamente:** antes de encontrar la
causa real, se investigó (con `binmanager-specialist`, datos MCP reales)
si era un problema de condiciones/ubicación como el caso PNP -- se
descartó con evidencia real: SHIL000522 SÍ tiene 1,201 uds vendibles
reales bajo NEW en MTY (LocationID 68), la misma combinación que ya
filtrábamos correctamente.

**Fix 1 (commit `8819c72`):** nuevo endpoint `POST /api/diag/bm-force-relogin`
para resetear el cliente BM compartido a demanda.

**Fix 2 (commit `125f4db`, el importante):** `_update_bm_master_for_category`
ahora detecta respuestas vacías sospechosamente rápidas (`elapsed_s < 1.5`)
como firma de sesión rota (no de BM genuinamente vacío) -- fuerza
re-login del cliente compartido y NO cuenta hacia la racha de
confirmación, evitando que el mismo problema de sesión vuelva a
confirmar un 0 falso en el futuro.

**Verificado en producción, ambos casos exactos de Jovan:**
- SHIL000522: `avail=1201, reserve=1` (antes: 0/0) — coincide con la UI de BM.
- SNTV007241: `avail=59, reserve=54` (antes: 0/0) — coincide con la UI de BM.

Las 5 categorías afectadas se re-corrieron manualmente y quedaron con
datos reales (Televisions 455, Air Conditioners 10, Interior Lighting
466, Home Power Tools 58, Exercise Equipment 21 filas — mismo orden de
magnitud que antes del incidente, 19:47).

---

## 2026-08-21 — FEAT: mostrar "reservado" (BM) en Alertas de Stock

Jovan explicó un caso real: cuando entra una orden, BM reserva la unidad
y pone `AvailableQTY` en 0 para no sobrevender -- correcto del lado de
BM, pero "Alertas de Stock" (feed en vivo de órdenes sin stock) no
distinguía eso de "no hay stock real", y ofrecía sustituto/marcaba "sin
stock" igual en ambos casos.

Fix: `get_realtime_stock_alerts()` (`token_store.py`) agrega
`reserve_qty` (actual, del mismo JOIN con `bm_sku_master` que ya trae
title/brand/retail_ph -- sin llamadas nuevas a BM). `orders.html` muestra
"reservado N" junto a "Disp. BM" cuando aplica (escritorio + tarjeta
móvil, mismo `var avail` compartido). Solo se expone el número -- no se
cambia la lógica de sugerencia/alerta automáticamente, la persona decide
con el dato real enfrente (mismo criterio de "Solo en Tijuana"/PNP).

Verificado en producción: 18 alertas activas revisadas, todas con
`reserve_qty=0` ahora mismo (cruzado con `/api/diag/sku` para SNTV007447:
avail=0, reserve=0, total=10 -- coincide) -- es decir, el lote actual son
quiebres reales, no falsos positivos por reserva. El mecanismo queda
listo para mostrar el número real la próxima vez que sí aplique.

---

## 2026-08-21 — FEAT: indicador PNP (MTY) en Cobertura de Stock — solo Televisions

Jovan pidió priorizar qué productos con alta demanda meter primero a la
línea de proceso cuando tienen stock en condición PNP ("Plug and Play" —
unidades que esperan prueba de encendido antes de su grado final GRA/GRB/
GRC). Aclaró: PNP solo se procesa en MTY (Tijuana solo reabastece con
producto YA terminado, nunca debería tener PNP).

Investigación previa con `binmanager-specialist` encontró una discrepancia
real entre 2 fuentes de datos PNP para el mismo SKU (91 vs 4 uds) y ningún
endpoint HTTP confirmado detrás de la herramienta MCP "workcenter" usada
para investigar. Jovan mismo resolvió el bloqueo: capturó con DevTools la
llamada real de la UI de BM buscando SNTV008001 con `CONDITION=PNP` y
encontró que es el **mismo endpoint que ya usamos** (`Get_GlobalStock_
InventoryBySKU`, el de `tj_qty`) — trae `AvailableQTY` (4) y `NoVendibleQty`
(379, el "Not Sellable" que BM muestra en su UI), sin necesitar ningún
endpoint nuevo ni preguntarle a Alberto.

Implementado:
- `_update_bm_master_for_category`: 2 llamadas extra SOLO para
  category="Televisions" — `CONDITION=PNP LOCATIONID=68` (MTY, la
  cantidad real para priorizar proceso) y `CONDITION=PNP
  LOCATIONID=47,62,45,69,43,42` (CDMX+Tijuana combinado, para detectar la
  anomalía real de PNP fuera de MTY).
- 3 columnas nuevas en `bm_sku_master`: `pnp_mty_available`,
  `pnp_mty_novendible`, `pnp_other_locations_qty` — 0 para cualquier
  categoría que no sea Televisions.
- `/api/planning/coverage` agrega estos 3 campos por SKU.
- `planning.html`: columna "PNP (MTY)" en Cobertura (mismos términos que
  BM: Disponible / No Vendible) + badge "⚠ PNP fuera MTY" cuando aplica.

Verificado en producción con datos reales: 13/50 productos del top de
demanda tienen PNP real en MTY (ej. SNTV001764: 1 disponible, 145 no
vendible/necesita proceso). 149 SKUs de TV con PNP en total, 700 unidades
encontradas fuera de MTY (anomalía real — Jovan la va a cuestionar
directo con producción).

**Actualización:** Jovan buscó la columna en "Velocidad de Ventas — ML +
Amazon por SKU" (bloque distinto de "Cobertura de Stock & Orden de
Separación", donde se implementó primero) y confirmó que tiene sentido
agregarla también ahí. Mismo helper (`get_pnp_data_for_skus`, sin
llamadas a BM), ahora también en `/api/planning/velocity`. Verificado en
producción: SNTV001764 (22.43 uds/día ML+Amazon, el #1 en su lista) muestra
1 disponible / 145 no vendible.

---

## 2026-08-21 — FIX CRÍTICO: "Error al calcular stock" (UnboundLocalError, no timeout real)

Jovan reportó con captura real de producción: cuenta BLOWTECHNOLOGIES,
Productos > Inventario > Stock, "Error al calcular stock" con traceback
mostrando `_prewarm_caches` (main.py:7746) → `asyncio.wait_for(timeout=600.0)`
— parecía un timeout real de 600s.

**Causa real (NO era timeout):** el commit `9be175a` de hoy mismo
(rediseño de Transferencias Sugeridas, ~01:37am) eliminó el bloque que
definía y poblaba `transfer_suggestions` dentro de `_do_prewarm()`, pero
dejó 2 referencias colgantes a esa variable al construir `_sic_data`
(líneas 7637-7638). Como ya no se asignaba en ningún punto,
**cada corrida de `_do_prewarm()` (las 4 cuentas ML) lanzaba
`UnboundLocalError`** de inmediato — no relacionado con demora real
(BM ya no hace ninguna llamada en vivo en este flujo desde el
2026-08-20). Solo se hizo visible en BLOWTECHNOLOGIES porque esa cuenta
no tenía snapshot cacheado al que la UI pudiera hacer fallback
silencioso — las otras 3 cuentas seguían mostrando datos viejos sin
exponer el error de fondo. El `UnboundLocalError` no es
`asyncio.TimeoutError`, así que no lo capturaba el `except` específico
de timeout — se propagaba hasta el `except Exception` externo, y el
traceback (cortado en la captura) engañaba a simple vista.

Fix: eliminadas las 2 líneas colgantes (`"transfer_suggestions"` /
`"transfer_suggestions_count"`) de `_sic_data` (commit `1b281a1`).
Verificado local (force-prewarm sin error) y en producción específicamente
para BLOWTECHNOLOGIES: `/api/stock/prewarm-status` → `ready:true,
error:""`, y `/partials/products-stock-issues` renderiza contenido real
("Activar Todos (2)"), cero "Error al calcular stock".

**Nota de proceso:** bug introducido por mí mismo ~50 min antes, en el
mismo commit del rediseño de Transferencias — quedó sin detectar porque
la verificación de esa feature se centró en el endpoint nuevo
(`/api/planning/tj-only-transfer`) y no se re-probó el flujo completo de
`_do_prewarm()` para las 4 cuentas tras remover código relacionado.

---

## 2026-08-21 — FEAT: rediseño completo de Transferencias Sugeridas — prioridad de ventas, filtros, paginación

Jovan vio la primera versión (333 filas sin paginar) y dio feedback directo:
"entre bodegas de MTY y CDMX no debemos mover nada, solo es de TJ" +
"ponme las ventas para saber a qué le damos prioridad" + "filtrar por
categorías" + "usa a los expertos siempre... y sobre todo el diseñador de
la página" (crítica explícita por no haber pasado la feature por
`uxui-designer` antes de mostrarla).

Se invocaron 2 especialistas en paralelo:
- `uxui-designer` — diseñó tabla en escritorio + tarjetas en móvil (mismo
  patrón responsive de `products_listings.html`), búsqueda por SKU/título,
  filtros de categoría/prioridad, 3 modos de orden, paginación de
  10/página. Prototipo navegable verificado antes de implementar.
- `planning-specialist` — recomendó ventana de **12 meses** (no 90d/
  lifetime) para medir ventas: estos SKUs llevan tiempo en 0 vendible, una
  ventana corta subestimaría demanda real (censura por el propio quiebre).
  Badges: alta ≥50 uds/12m, media 10-49, baja 1-9, sin_historial 0. Orden:
  badge → units_12m desc → (tj_qty×retail_ph) desc como desempate de valor.

Cambios:
- Eliminado por completo `_suggest_transfer`, `get_zone_demand_by_sku` y
  `/api/planning/transfer-suggestions` (el rebalanceo MTY↔CDMX) — ya no
  aplica, "Solo en Tijuana" es el único indicador.
- `get_tj_only_transfer_candidates()` (`token_store.py`) ahora hace JOIN
  con `order_history` (365 días) y devuelve `units_12m`, `sales_badge`,
  `last_sale_date` por SKU.
- `planning.html` reescrito: tabla/tarjetas responsive, búsqueda, filtros,
  orden, paginación, "Copiar lista" respeta el filtro activo.

Verificado en producción con los 333 SKUs reales: distribución de badges
3 alta / 23 media / 66 baja / 241 sin historial. El #1 en prioridad
(RMTC008308, control remoto TV) tiene 6,776 uds en Tijuana **y** 103
ventas reales en 12 meses — exactamente el tipo de priorización que Jovan
pidió. Página `/planning` responde 200 OK post-deploy.

Ver `.claude/memory/feedback_usar_agentes_especializados.md` (quinta
reincidencia registrada) y `.claude/memory/reference_sales_priority_window_transfers.md`.

---

## 2026-08-21 — FEAT: indicador "Solo en Tijuana" en Transferencias Sugeridas — tj_qty revivido

Jovan pidió pulir "Transferencias Sugeridas Entre Almacenes" (Planeación)
con un indicador claro: productos con stock real en Tijuana y CERO stock
vendible en CDMX/MTY, para disparar un requerimiento de envío a almacén
lo antes posible ("nosotros definimos el destino").

Hallazgo antes de tocar código: `bm_sku_master.tj_qty` (la columna que
alimenta justo este dato) estaba **congelada desde el 2026-08-19** — el
mecanismo que la llenaba (`_bm_master_sync_loop`) se pausó ese día a
propósito porque en ese momento "no era prioridad", y su fuente
(`_bm_bulk_loc47/68/loctj_cache`) además quedó muerta con la consolidación
del 2026-08-20 (mismo patrón del bug del badge "794m").

Fix/feature:
- `_update_bm_master_for_category` (`app/main.py`) ahora hace una segunda
  llamada por categoría, SOLO Tijuana (`get_bulk_stock(location_id="45,69,43,42")`
  — el parámetro ya existía, documentado para este uso exacto), y guarda
  `tj_qty` en el UPSERT existente. Duplica el tiempo por categoría
  (aceptado explícitamente: prioridad es el dato, no la velocidad).
- `token_store.get_tj_only_transfer_candidates()`: SELECT puro
  (`tj_qty > 0 AND available_qty = 0`), sin llamar a BM — instantáneo.
- Nuevo endpoint `GET /api/planning/tj-only-transfer`.
- `planning.html`: sección dividida en 2 bloques — 🔴 "Solo en Tijuana"
  (urgente, con botón "Copiar lista" para mandar el requerimiento a
  almacén) y 🔵 el rebalanceo por demanda que ya existía, ahora secundario.

Verificado local y en producción: categoría "Air Fryers" — 3 SKUs con
stock real en Tijuana (42/9/1 uds) y 0 vendible, refrescados correctamente
tras el deploy. Full-resync de las 59 categorías completado en producción
(sin interrupciones, sin más pushes de por medio hasta terminar).

**Resultado real del resync:** 333 SKUs, 39,062 unidades totales en
Tijuana sin ningún stock vendible en CDMX/MTY — concentrado sobre todo en
"Remote Control - TV" y "Cables - Power" (controles remotos/cables de
repuesto con miles de unidades cada uno, ej. RMTC008308 con 6,776 uds).
Hallazgo de negocio real, no solo técnico: buena parte del inventario de
refacciones vive hoy invisible para venta en línea.

Jovan reportó (captura) que "Activar" recomendaba SNFN000095 (Ventilador
de Torre Vornado, categoría "Fans") con 332 unidades "BM Disponible". El
fix del 2026-08-20 (pedir siempre GRA,GRB,GRC,ICB,ICC,NEW para toda
categoría, motivado por el caso Fan Heater/SNFH000004) violó la HARD RULE
ya documentada en CLAUDE.md ("ICB/ICC solo para SNTV*/Televisions") y
causó daño real: esas 332 unidades vienen 100% de condiciones ICB/ICC, y
la fila cruda de BM trae el tag `"FAN REPAIR"` — son unidades en
reparación, no stock vendible.

Fix: `_update_bm_master_for_category` y `/api/diag/raw-category-rows`
(`app/main.py`) vuelven a usar `GRA,GRB,GRC,ICB,ICC,NEW` solo cuando
`category_id == "Televisions"`, y `GRA,GRB,GRC,NEW` para todo lo demás.
Verificado local: Fans/SNFN000095 → 0 filas reales (confirma que las 332
eran puro ICB/ICC de reparación). Televisions → sigue trayendo sus ~455
filas con ICB/ICC intacto. Deploy Railway SUCCESS (commit `c1765a4`). Se
disparó un full-resync de las 59 categorías en producción para corregir
los valores de `bm_sku_master` ya inflados durante el día que el bug
estuvo activo (afecta potencialmente cualquier categoría no-TV con stock
etiquetado como reparación/incompleto).

Ver `.claude/memory/project_bm_icb_icc_category_rule_revert_2026-08-21.md`
y `.claude/memory/feedback_no_generalizar_regla_desde_un_solo_caso.md`
(lección: no generalizar una regla de negocio documentada a partir de un
solo caso confirmado sin preguntar explícitamente).

**Actualización:** el primer full-resync se cortó a medias (24/59) porque
un push de solo-DEVLOG disparó un redeploy de Railway que reinició el
proceso y perdió el progreso en memoria (`_full_resync_progress` no
sobrevive un restart). Se re-disparó desde cero sin más pushes de por
medio -- 59/59 categorías completadas. Verificado en producción:
`SNFN000095` → `avail=0` (antes 332, ahora correcto -- las 332 siguen en
`total` porque son unidades reales, solo que no vendibles), `Televisions`
sigue trayendo sus 455 filas con ICB/ICC intacto, y otras categorías de
alto stock (`Interior Lighting`, `Personal Protective Equipment`, `Home
Power Tools`) confirmadas usando `GRA,GRB,GRC,NEW` sin ICB/ICC.

---

## 2026-08-21 — FIX: badge "Sync Stock" (794m) crecía sin parar — fuente muerta

Jovan reportó el nav tab "Sync Stock" con badge rojo "794m" (minutos).
Causa raíz: ese badge (`bm-cache-age`, `base.html:136`) viene de
`bulk_age_s` en `/api/stock/prewarm-status`, calculado antes a partir de
`_bm_bulk_gr_cache`/`_bm_bulk_all_cache`. Esos 2 caches dejaron de
repoblarse en vivo desde el fix del 2026-08-20 ("todo debe apuntar a
nuestro maestro, nada a BM") — quedaron congelados desde el warm-start de
disco al arrancar el proceso, así que su edad solo podía crecer para
siempre. El mismo `bulk_age_s` alimenta también la alerta crítica global
("Datos de inventario BM sin actualizar... contacta al administrador"),
así que era un falso positivo permanente, no solo un número feo en el nav.

Fix: `bulk_age_s` ahora se calcula desde la frescura real de
`bm_sku_master` (`token_store.get_bm_master_sync_meta()`), la fuente que
sí se sigue actualizando (loop de categorías top-5 cada 15 min). Verificado
local (2500s, bounded) y en producción post-deploy (501s ≈ 8 min,
coherente con el ciclo real del loop). Ver
`.claude/memory/project_bm_call_consolidation_2026-08-20.md` para el
contexto completo de por qué el cache viejo quedó muerto.

Archivo: `app/main.py` — endpoint `prewarm_status()`, ~línea 16038-16058.

---

## 2026-08-20 — FIX CRÍTICO: "Activar" seguía recomendando SNMP000002 (75 uds) cuando BM real es 0

Jovan reportó que, después de confirmar con el filtro vendible real de BM
que "Microphones-JLab" está genuinamente en 0, la alerta "Activar" seguía
mostrando SNMP000002 con "75 disponibles" — dato viejo (>24h) que nunca
se pudo corregir porque la regla "0 filas = no tocar nada" (protección
contra el incidente de bloqueo de BM) NO distinguía "0 real confirmado"
de "posible fallo" — así que un 0 genuino nunca se aceptaba, dejando el
valor viejo atorado para siempre.

Nuevo criterio en `_update_bm_master_for_category` (`main.py`):
`_bm_category_zero_confirm_streak` — 0 filas UNA vez sigue sin tocar nada
(protege contra un fallo puntual real de BM); 0 filas **2 veces
seguidas** (llamadas separadas en el tiempo) se acepta como confirmado
real y SÍ actualiza `bm_sku_master` a 0 para todos los SKUs conocidos de
esa categoría.

Verificado local: 1ra llamada a "Microphones-JLab" → `racha 1/2, sin
confirmar`; 2da llamada → `ok:true, skus_confirmed_zero:7`. SNMP000002
quedó en `avail=0, reserve=0, total=0` — coincide exacto con BM.

---

## 2026-08-20 — FIX: ICB/ICC ya no se restringen a categoría "Televisions"

Jovan encontró el caso real: SNFH000004 (Fan Heater) tiene su única unidad
vendible en condición **ICB** — como el loop de categorías solo pedía
GRA/GRB/GRC/NEW para categorías que no son "Televisions", nunca se
encontraba, y la categoría completa "Fan Heater" se veía como "0 filas"
(indistinguible de un bloqueo/fallo de BM). Verificado contra BM real
(filtro vendible de Jovan en la UI de BM: Available=1, Reserve=0, Not
Sellable=1 — coincide exacto).

`_update_bm_master_for_category` y `/api/diag/raw-category-rows`
(`main.py`) ahora piden siempre las 6 condiciones (GRA,GRB,GRC,ICB,ICC,NEW)
para TODAS las categorías, no solo Televisions. Verificado local: "Fan
Heater" pasó de 0 filas a 1 fila (`AvailableQTY:1, Reserve:0,
NoVendibleQty:1`), coincide exacto con BM.

**Lección de la sesión (correcta observación de Jovan):** "0 resultados
bajo un criterio angosto" y "BM bloqueado/con problema" son 2 cosas
distintas — no hay que asumir bloqueo solo porque una consulta con
condiciones incompletas no encuentra nada. El SKU existía y tenía stock
real todo este tiempo, solo preguntábamos la condición equivocada.

---

## 2026-08-20 — FEAT: botón "Reducir Todos" para Desbalance Peligroso

Jovan notó que "Desbalance Peligroso" era la única sección con acción
uniforme por fila (reducir stock MeLi al nivel BM, ya implementada y
probada en `syncOneImbalanced`) que no tenía su botón masivo equivalente
-- a diferencia de Reabastecer/Riesgo Sobreventa/Stock Crítico/Activar/
Listings Eliminados, que sí lo tienen. Auditoría de las 10 secciones de
`products_stock_issues.html` confirmó que es la única inconsistencia
real: FULL Sin Stock, Inventario Estancado y Margen Real Insuficiente NO
tienen ninguna acción (ni individual ni masiva) por diseño -- requieren
juicio humano (qué precio nuevo, cuánto descuento) o no son tocables vía
API (FULL).

Nuevo `bulkReduceImbalanced()` (`products_stock_issues.html`) -- mismo
patrón que `bulkActivateAll`: filtra items visibles en DOM, confirm(),
llama `PUT /api/items/{id}/stock` con `quantity=_bm_avail` por cada uno
(la misma llamada que ya hacía el botón individual), progreso con
contador de errores. Cero lógica nueva de negocio, solo repite la acción
individual ya probada en bucle.

Verificado local: template Jinja válido, botón renderiza "Reducir Todos
(49)" con el conteo real de la sesión.

---

## 2026-08-20 — DECISION: loop longtail de categorías bajado de 4h a 2h de rezago máximo

Continuación de la entrada de arriba (investigación de "BM Disponible"
inflado por staleness, no bug de cálculo). Jovan decidió bajar el
margen de rezago máximo de categorías de cola larga de 4h a 2h
(`_CONF_COLUMNS_LONGTAIL_INTERVAL_S`, `main.py`) para reducir la ventana
en la que un cambio real de reserva en BM puede quedar desactualizado en
`bm_sku_master`. Las top-5 categorías (15 min) no cambian.

Full-resync manual de las 59 categorías corrido en producción antes de
este cambio (mismo día) para corregir de inmediato todo lo que estaba
desactualizado, sin esperar al primer ciclo con el nuevo intervalo.

---

## 2026-08-20 — INVESTIGACIÓN: "BM Disponible" inflado en Activar (SNWM000001: 3899 vs 3383 real) — NO es bug de agregación, es staleness normal de categorías longtail

Jovan reportó con capturas (dashboard vs BM UI real) que "Activar" mostraba
`BM Disponible: 3899` para SNWM000001 mientras BM mostraba
`Available: 3383 / Reserve: 516 / Not Sellable: 8621` — 3899 = 3383+516,
como si estuviéramos sumando available+reserve.

Nuevo `GET /api/diag/raw-category-rows` (category_id + sku opcional) —
llama el MISMO endpoint que usa el loop de categorías
(`get_bulk_stock`/`Get_GlobalStock_InventoryBySKU`, CONCEPTID=1, "el
correcto" que confirmado antes SÍ excluye tránsito) y devuelve la fila
CRUDA sin agregar, para comparar contra lo guardado.

**Resultado: la fila cruda que BM devuelve ya trae `AvailableQTY:3383,
Reserve:516, NoVendibleQty:8621` — exactos.** `_bulk_stock_rows_to_master_fields`
suma correctamente (`avail_total += AvailableQTY`, no
`AvailableQTY+Reserve`) — no hay bug de agregación, ni hoy ni en el
código que ya existía antes de los cambios de hoy. Al forzar
`bm-master-update-category` para "Wall Mounts" (fuera del ciclo de 4h del
loop longtail), `bm_sku_master` quedó en `available_qty=3383,
reserve_qty=516` -- correcto de inmediato.

**Causa real: staleness normal.** "Wall Mounts" es categoría longtail
(no top-5 por ventas), se refresca cada 4h -- el dato que Jovan vio venía
de un ciclo anterior a que BM registrara la reserva de 516 unidades (algo
cambió en BM entre ese ciclo y el momento de la captura). No es que el
número esté mal calculado, es que llegó tarde.

**Mitigación aplicada:** correr `/api/diag/bm-master-full-resync` en
producción para refrescar TODAS las categorías de inmediato en vez de
esperar hasta 4h por categoría.

**Pendiente de decisión de Jovan:** si el margen de hasta 4h de rezago en
categorías longtail es aceptable (tradeoff ya conocido, ver
project_bm_call_consolidation_2026-08-20) o si se debe bajar ese
intervalo para SKUs con mucha rotación de reservas.

---

## 2026-08-20 — OPERACION: nuevo trigger manual para refrescar alertas "Sin Publicar" sin esperar al cron de 3am

Jovan pidió actualizar las alertas que ven los usuarios tras los fixes de
`bm_sku_master` del mismo día (ver 3 entradas de abajo). El scan de gaps
("Sin Publicar" ML + Amazon) que alimenta esas alertas solo corre 1x/día
(3am México) — sin un trigger, los usuarios seguirían viendo datos
calculados con la lógica VIEJA (filtrada por "SKU conocido") hasta la
próxima madrugada.

Nuevo `POST /api/diag/trigger-gap-scan-all` (`main.py`) — dispara
`_run_gap_scan` (ML, todas las cuentas) + `_run_amz_gap_scan` (Amazon,
cada cuenta) en background, respetando los mismos locks que los botones
reales (`/api/lanzar/scan-all`, `/api/amazon/lanzar/scan`) — no duplica
lógica, solo evita necesitar sesión admin para dispararlo remotamente.

Verificado local: ML scan completo sin errores (1955 SKUs con stock,
category/upc enriquecido para 1832 SKUs). Amazon dio 400 de OAuth local
(token de Amazon no funciona fuera de Railway, limitación conocida del
entorno local) — maneja el error con gracia ("benefit of doubt", no
marca falsos gaps), se dispara igual en producción donde el auth sí
funciona.

---

## 2026-08-20 — FEAT: catálogo diario de BM ahora completa category/upc/image_url para TODO SKU (con o sin stock)

Cierre del punto #1 pendiente de la revisión de "todo debe apuntar a
nuestro maestro" (ver 2 entradas de abajo, mismo día). Jovan explicó el
diseño correcto: el sync diario (`_sync_bm_product_catalog`, 3am
Monterrey, `ConfColumns_Conditions_Excel` — el mismo endpoint que
"cuenta" tránsito, sugerido por Alberto) sirve para bajar el catálogo
COMPLETO de BM de un jalón; no importa que sus números de stock mezclen
tránsito porque **nunca se usan** — el stock real siempre viene de
`bm_sku_master` alimentado por el loop de categorías (que sí excluye
tránsito). Este catálogo es lo que permite saber "qué existe en BM" para
definir qué está lanzado y qué no, independientemente de si tiene stock
hoy.

Confirmado en código: `upsert_bm_catalog_batch` (`token_store.py`) ya
hacía `INSERT ... ON CONFLICT` sin filtro por SKU conocido — ya cubría
TODO el catálogo por título/marca/modelo/costo/tamaño. Solo faltaban 3
campos que antes solo llenaba el loop de categorías (y ese solo toca
SKUs con stock actual): `category`, `upc`, `image_url`. Agregados con el
mismo patrón `COALESCE(NULLIF(...))` que ya usa el loop de categorías —
nunca pisa un valor bueno con uno vacío.

Confirmado que corre 1x/día automático (`_weekly_catalog_sync`, 3am
Monterrey) + 2 triggers manuales existentes (`/api/diag/trigger-catalog-sync`,
`/api/health/...`) — no cambia la cadencia, solo qué campos guarda.

**Verificado local:** sync manual disparado contra BM real — 9,266 filas,
16.8s. `SNWA000002`/`SNWA000003`/`RMTC001968` (0 stock hoy) pasaron de
`category=''` a tener categoría real ("Window Type Air Conditioner",
"Remote Control - Sound Bar") — exactamente el hueco que se buscaba
cerrar. 9,227/9,266 SKUs con categoría después del sync.

---

## 2026-08-20 — FEAT + DECISION: revisión completa de lo que quedaba llamando a BM en vivo (2da vuelta)

Jovan pidió explícitamente revisar TODO lo que faltaba tras la 1ra vuelta
(ver entrada de abajo, mismo día). Mapeo exhaustivo de cada `get_shared_bm`/
`bm_post`/`bm._post` que quedaba en `main.py`.

### Migrado a `bm_sku_master`
- `/api/bm/launch-opportunities` (feature "Sin Lanzar" invertido: BM con
  stock → qué falta publicar en ML) — antes `get_bulk_stock()` en vivo,
  catálogo completo, cada vez que el caché de 15 min vencía. Verificado
  local con sesión real: 200 OK, 1.8s, 189 oportunidades reales sobre
  11,797 SKUs de BM vs 5,670 ya publicados en ML.

### Código muerto eliminado
- `_fetch_tv_wh_breakdown()` (~200 líneas) — cero callers confirmado (solo
  se programaba desde el bloque inalcanzable que se borró en la 1ra
  vuelta de hoy).

### 7 endpoints de diagnóstico más desactivados (410, no borrados)
Todos experimentos empíricos de una sola vez (2026-08-19/2026-08-20) sobre
parámetros de BM (`InventoryType`, `BinTypeID`, payload exacto del
browser) — preguntas ya respondidas y decisiones ya adoptadas en
producción: `globalstock-category-test`, `confcolumns-bintype-test`,
`globalstock-bintype-test`, `bm-sku-probe`, `bm-bulk-test`,
`bm-web-payload`, `bm-category-bulk-probe`.

### 3 casos con recomendación, pendientes de decisión de Jovan (NO tocados)
1. **`_sync_bm_product_catalog`** — sync diario (3am, 1 sola llamada
   ConfColumns para TODO el catálogo) que alimenta title/brand/model/
   retail_ph/cost_usd/size — ahora parcialmente redundante con lo que el
   loop de categorías ya captura por su cuenta, pero cubre SKUs con 0
   stock (que el loop de categorías nunca ve) y trae `NEEDSALES`. Mismo
   patrón de riesgo identificado el 2026-08-19 (1 llamada gigante puede
   degradar bm_sku_master mientras corre), mitigado hoy solo por correr a
   las 3am con poco tráfico concurrente.
2. **`_check_bm_health`/`_bm_health_loop`** — ping real a BM cada 2 min
   (`GET /User/Index`, sin datos, muy liviano). Migrar a "frescura de
   bm_sku_master" (como ya se hizo con el circuit breaker de Sync Stock y
   system_health.py) daría una señal PEOR en el escenario exacto de hoy
   (BM alcanzable pero un endpoint específico con bug HTTP 500 — el ping
   lo hubiera reportado "BM ok" correctamente; la frescura del maestro
   hubiera dicho "caído" incorrectamente). También alimenta un panel
   real en Sync Stock con latencia en vivo que perdería sentido.
3. **`/api/planning/production-kpis`** — KPIs de Operaciones de BM
   (FFT/Sorting/Recycle/Shipped) — dominio distinto a `bm_sku_master`
   (stock/catálogo por SKU); no existe maestro equivalente para migrar.

**Verificado antes de push:** compile OK, servidor local, endpoint
desactivado confirmado (410), `/api/bm/launch-opportunities` con sesión
real (200, datos correctos).

---

## 2026-08-20 — DECISION + FEAT: bm_sku_master pasa a ser espejo COMPLETO del catálogo BM; ~15 vías más migradas de "llamar a BM en vivo" a leer el maestro

**Directiva textual de Jovan** (continuación del incidente de bloqueo de BM
del mismo día, ver [[project_bm_call_consolidation_2026-08-20]]): "todo debe
apuntar a nuestro maestro, nada a BM por el momento" — sin excepción, ni
siquiera herramientas de diagnóstico de un solo SKU.

### Cambio de fondo: `bm_sku_master` ya no filtra por "SKU conocido"

`_update_bm_master_for_category` (`app/main.py`) filtraba cada fila de BM
contra `get_all_known_base_skus()` (SKUs ya publicados en ML o Amazon)
antes de guardarla — esto hacía **estructuralmente imposible** usar
`bm_sku_master` para detectar gaps reales (productos con stock en BM que
NUNCA se han publicado en ninguna plataforma, por definición ausentes de
"conocidos"). Se quitó el filtro: ahora se hace UPSERT de TODA fila que
BM devuelva para la categoría (`INSERT ... ON CONFLICT(sku) DO UPDATE`,
con `COALESCE(NULLIF(...))` para no pisar título/marca/categoría/etc. con
vacíos si una fila viene parcial). Con el loop cubriendo TODAS las
categorías conocidas (top-5 cada 15 min + longtail cada 4h),
`bm_sku_master` se vuelve un espejo real y completo del catálogo vendible
de BM en unas horas.

Se agregó columna `image_url` (BM ya la traía vía `NEEDFILE=True`, nunca
se guardaba) y `_bulk_stock_rows_to_master_fields` ahora también captura
`title`/`brand`/`model`/`retail_ph` de cada fila (antes solo `category`/
`upc`) — completa lo que antes solo llegaba 1x/semana vía el sync de
catálogo aparte.

Nueva función `token_store.get_bm_master_all_as_bulk_rows(min_qty)`:
adapta filas de `bm_sku_master` al mismo shape que devolvía el bulk viejo
de BM (`SKU`/`AvailableQTY`/`Reserve`/`TotalQty`/`Title`/`Brand`/`Model`/
`CategoryName`/`ImageURL`/`UPC`/`LastRetailPricePurchaseHistory`/
`RetailPrice`/`AvgCostQTY`/`Size`) — los consumidores existentes no
necesitaron reescribir su lógica de parseo, solo cambiar de dónde viene
la lista.

### Vías migradas (ya no llaman a BM en ningún caso)

**Automatizadas/background (las que de verdad generaban carga):**
- `amazon_lanzar.py` — gap scan nocturno Amazon (antes `get_bulk_stock()`
  sin `category_id`, catálogo completo).
- `lanzar.py` (`_bm_fetch_all_skus_with_stock`) — gap scan ML, mismo
  patrón (antes ConfColumns_Conditions_Excel completo, hasta 120s).
- `amazon_products.py` (`/products/sin-bm`) — refrescaba cada 15 min con
  catálogo completo en vivo.
- `stock_sync_multi.py` (`_fetch_bm_avail`) — usado por "Sync Stock"
  manual, catálogo completo en vivo + 2 "canarios" que probaban BM con un
  SKU fijo (`SNTV001764`) — reemplazados por un circuit breaker que mide
  frescura de `bm_sku_master` (`get_bm_master_sync_meta`, umbral 45 min =
  3 ciclos perdidos del loop top).
- `system_health.py` (`_check_binmanager`, corría cada 10 min
  automáticamente) — mismo cambio: ping en vivo → frescura del maestro.

**On-demand (1 SKU, gatillado por una acción humana) — migradas también
porque la directiva no hizo excepción:**
- `main.py` — "Sync Var." de variaciones/bundles (lectura y la del botón
  real `sync-variation-stocks`), `/api/items/{id}/bm-cost`,
  `/partials/item-deal/{id}` (modal de deals).
- `sku_inventory.py` — `/api/sku-inventory/compare` (batch, hasta 30
  llamadas concurrentes con Semaphore(10) — ahora 1 sola consulta SQL) y
  `/api/sku-inventory/research`.
- `health_ai.py` — contexto de producto para respuestas IA de reclamos/
  preguntas.
- `binmanager.py` — `/api/bm/retail-ph-batch`.
- `items.py` (`get_inventory`) — el último-recurso "buscar en cualquier
  almacén" quedó desactivado (no hay maestro equivalente que cubra bins
  no-vendibles/Tijuana; se documenta como limitación temporal).

**Herramientas de diagnóstico obsoletas, desactivadas (no borradas)** con
`return ... status_code=410` antes del código viejo: 2 comparativas
ConfColumns ya superadas (`confcolumns-location-check`,
`bm-master-confcolumns-compare`) y 2 pruebas puntuales de una sola vez
del 2026-08-19 (`confcolumns-location-param-test`,
`warehouse-endpoint-raw-test`).

**Limpieza de código muerto:** ~808 líneas inalcanzables dentro de
`_get_bm_stock_cached` (el bloque completo después del `return` que el
fix de hoy más temprano ya había insertado) + `_bm_verify_sku_direct`
(cero callers desde el mismo fix) — eliminadas por completo, no solo
comentadas.

### Pendiente/decisión abierta

Quedan ~15-20 endpoints `/api/diag/*` históricos (experimentos de una
sola sesión pasada, ej. `bm-web-payload`, `bm-bulk-test`,
`globalstock-category-test`) que técnicamente aún podrían llamar a BM en
vivo si alguien los invoca manualmente con el token — nunca se ejecutan
solos (no hay loop ni UI que los llame), así que no contribuyen a
tráfico/carga real. No se tocaron por costo/beneficio (bajo riesgo,
herramientas de investigación que podrían servir en el futuro) — Jovan
puede pedir que se desactiven también si prefiere cero excepciones
literales.

**Verificado antes de push:** compilación de los 11 archivos, servidor
local levantado, `POST /api/diag/bm-master-update-category?category_id=Televisions`
contra BM real vía el nuevo UPSERT (`ok:true`, 455/455 filas actualizadas
-- antes 402/455 por el filtro de "conocido" ya quitado), `/api/diag/sku`
con la sección `master` nueva mostrando title/brand/model reales, y
`get_bm_master_all_as_bulk_rows()` ejercitado directo contra la DB local
(1,955 SKUs con stock de 8,989 totales).

---

## 2026-08-20 — FIX CRÍTICO: loop infinito de redirects (ERR_TOO_MANY_REDIRECTS) para usuarios con permisos huérfanos

**Archivos:** `app/services/user_store.py`, `app/main.py`.

Incidente en producción: 2 usuarios no podían entrar al dashboard,
navegador mostraba "This page isn't working... redirected you too many
times". Causa: un usuario (no-admin) tenía `allowed_sections=["ml.sync"]`
— dato viejo, previo a esta sesión de trabajo. `/stock-sync` rechaza a
cualquier no-admin (`stock_sync_page`, redirige a `/dashboard`), pero el
árbol de permisos SÍ le daba acceso a "sync" — el middleware entonces lo
mandaba de vuelta a `/stock-sync` vía `first_allowed_location()`. Loop
infinito entre esas 2 páginas. El fix inmediatamente anterior (permisos
frescos en cada request, sin necesitar relogin) expuso este dato huérfano
de inmediato — antes, el JWT viejo de ese usuario simplemente nunca
reflejaba ese permiso stray hasta su próximo login.

Dos fixes complementarios:
1. `user_store.py`: `_ADMIN_ONLY_TABS = {"ml.sync"}` — "Sync Stock" exige
   `role=="admin"` a nivel de PÁGINA, así que `has_tab_access`/
   `get_allowed_subtabs`/`first_allowed_location` ahora lo ignoran por
   completo, sin importar qué diga `allowed_sections`.
2. `main.py` (`AuthMiddleware`): red de seguridad general — si
   `first_allowed_location` no encuentra ningún destino válido, o el
   destino calculado es la MISMA página que ya se negó, ya NO redirige
   (eso es exactamente lo que causa un loop) — muestra un mensaje inline
   "no tienes acceso a ninguna sección" (403) en su lugar. Esto previene
   cualquier futuro caso similar, no solo el de "ml.sync".

Probado localmente antes de desplegar: `has_tab_access(["ml.sync"], "ml",
"sync")` → `False`, `first_allowed_location(["ml.sync"])` → `(None, None,
None)`.

---

## 2026-08-20 — FIX: permisos ya no requieren logout/login, se releen frescos en cada request

**Archivo:** `app/services/user_store.py`.

Jovan otorgó el permiso "Sin Stock" a Said, refrescó, y seguía sin verlo
aunque el checkbox ya estaba bien guardado en DB (confirmado con
`/api/diag/user-permissions`). Causa real: el JWT embebía rol/
allowed_sections/can_zero_stock congelados desde el momento del login —
un cambio de permisos no se reflejaba hasta que el token expirara (30
días) o el usuario cerrara sesión manualmente. Jovan fue claro: "los
cambios deben aplicar con un refresh solamente".

`get_session()` ahora usa el JWT SOLO para probar identidad (uid + firma
válida) y relee rol/allowed_sections/can_zero_stock/must_change_pw
FRESCOS de `dashboard_users` en cada request. `create_session()` ya no
embebe esos campos (quedan solo username/dn/role como metadata legible
del token, nunca usados para autorizar). `update_user()` sigue llamando
`delete_user_sessions()` al cambiar rol/secciones, pero ahora es solo
limpieza de las filas legacy — no hace falta para que el cambio tome
efecto.

Probado localmente: mismo token de sesión (sin relogin), un permiso
otorgado vía `update_user()` toma efecto en la siguiente llamada a
`get_session()` — exactamente el comportamiento de "un refresh basta".

---

## 2026-08-20 — FEAT: permiso otorgable "⛔ Sin Stock" (ML) separado del rol admin

**Archivos:** `app/services/user_store.py`, `app/main.py`, `app/templates/orders.html`, `app/templates/usuarios.html`.

Jovan reportó que Said y Alex ya no veían el botón "⛔ Sin stock" (Ventas →
Alertas de Stock ML) y pidió poder dárselo desde permisos — pero ese botón
nació admin-only desde su creación (2026-07-21, ver entrada de esa fecha),
sin ningún checkbox en `/usuarios` para otorgarlo a nadie más. No fue un
bug de código: fue una funcionalidad que faltaba (delegar el permiso).

- `user_store.can_zero_stock(du)`: nueva función, `True` si `role=="admin"`
  o si el usuario tiene el flag `can_zero_stock` en su sesión.
- El permiso se guarda como marcador `ZERO_STOCK_ACTION_KEY =
  "action:zero_stock_ml"` dentro de la misma columna `allowed_sections`
  (reusa la infraestructura existente, sin migración de DB), pero
  deliberadamente NO es parte del árbol tab/subtab — otorgar un subtab de
  Ventas no debe implicar esta acción destructiva, y viceversa.
- **Bug evitado durante el diseño**: si el marcador se dejara mezclado en
  `allowed_sections` tal cual, un usuario SIN ninguna otra restricción
  (`allowed_sections` vacío = acceso legacy total) que recibiera solo este
  checkbox habría quedado con `allowed_sections=["action:zero_stock_ml"]`
  (ya no vacío) — y el resto del código (`AuthMiddleware`,
  `amazon_dashboard()`) lo habría tratado como "tiene restricciones de
  sección", perdiendo TODO su acceso real por error. Fix: `create_session`/
  `get_session` separan el marcador a su propio campo `can_zero_stock`
  (columna `zst` en el JWT) antes de exponer `allowed_sections`, así el
  árbol de permisos nunca ve ese marcador.
- `/usuarios`: nuevo checkbox "⛔ Puede poner Sin Stock en ML" en los
  modales de crear/editar usuario (fuera del árbol de checkboxes de
  secciones, para que "Todas"/"Ninguna" no lo toquen por accidente).
- `zero-stock-preview`/`zero-stock` (antes `role != "admin"`) y el gate del
  botón en `orders.html` (`IS_ADMIN_ALERTAS`) ahora usan
  `user_store.can_zero_stock(du)`.

Probado localmente (3 sesiones JWT: admin, viewer sin permiso, viewer con
el checkbox otorgado): 200/403/200 en el endpoint y `true`/`false`/`true`
en `IS_ADMIN_ALERTAS`, respectivamente. Verificado además con un usuario
de prueba real (creado y borrado en la DB local) que otorgar SOLO este
permiso no restringe el resto de su acceso (`allowed_sections` queda
vacío, `can_zero_stock=True`).

---

## 2026-08-20 — CIERRE DEL INCIDENTE: causa real era un HTTP 500 de BM (no bloqueo) + 5ta vía corregida

### Diagnóstico real vía logs de Railway

Con la visibilidad de progreso recién agregada (`bm-category-loop-status`),
la primera corrida del loop top mostró las 5 categorías con `ok:false`.
Jovan confirmó con BinManager que NO hay ningún bloqueo puesto a la cuenta
`Claude.Jovan@mitechnologiesinc.com` (mandó captura de BM con datos reales
en su propia sesión).

Se consultó la API GraphQL de Railway (`deploymentLogs`, con filtro de
texto y ventana de tiempo — primera vez que se usa esta vía) y se
encontró la causa real: `[BM] get_bulk_stock pág 1 HTTP no-200 --
status=500 body[:300]='...<title>Object reference not set to an
instance of an object.</title>...'` — BM devuelve un **NullReferenceException
real de su propio servidor .NET**, no un rechazo de acceso. Confirmado
además de forma independiente vía el MCP de BinManager (solo lectura):
"Televisions" SÍ tiene stock real y fresco ahora mismo. BM en general
funciona; el error 500 es específico del endpoint/parámetros que usa
nuestra app.

`get_bulk_stock()` ya maneja el HTTP 500 correctamente (retorna lista
vacía) y el fix del mismo día ("0 filas = no tocar nada") ya lo cubre —
no hay corrupción, solo falta de datos frescos hasta que BinManager
corrija su servidor. No hay más acción de código posible de nuestro lado
para esto.

### 5ta vía encontrada y corregida (commit `92f5152`)

Los mismos logs revelaron `_enrich_bm_amz`/`_fetch_base`
(`app/api/amazon_products.py`) — 3 llamadas EN VIVO por SKU base de
Amazon (warehouse + stock + info), disparado por `_refresh_bm_all_bg`
para el catálogo Amazon completo cada 15 min. No se detectó en el primer
barrido porque vive en otro archivo. Corregido: `get_bm_master_rows_
for_skus()` (`token_store.py`) extendida con `retail_ph`/`cost_usd`, y
`_enrich_bm_amz` reemplazado por una sola consulta SQL batched. Limpieza
de código muerto asociado (`_parse_wh_rows_amz`, URLs sin uso).

### Herramienta nueva para diagnóstico

Railway GraphQL `deploymentLogs(deploymentId, startDate, endDate, filter,
limit)` — busca logs reales de producción por texto/ventana de tiempo.
Mucho más preciso que especular con diag endpoints agregados. Usar esto
la próxima vez que algo falle sin explicación clara antes de asumir
causa.

Memoria actualizada: `.claude/memory/project_bm_call_consolidation_2026-08-20.md`.

---

## 2026-08-20 — CONTINUACIÓN DEL INCIDENTE + FEAT: auto-halt del loop de categorías top ante patrón de 0 filas

### Segunda corrupción el mismo día

Tras confirmar "ya estamos desbloqueados" y reactivar `DISABLE_BM_MONITOR=false`,
una prueba manual a `/api/diag/bm-master-update-category?category_id=Televisions`
volvió a traer 0 filas de BM (HTTP 200 vacío) — el fix de "no tocar en 0
filas" (ver entrada de abajo) todavía no estaba desplegado en ese momento,
así que puso en 0 el `available_qty` real de TVs otra vez (SNTV001764,
SNTV007270, SNTV005362). Re-pausado `DISABLE_BM_MONITOR=true` de
inmediato, restaurado desde `bm_sku_master_backup_20260820_043936` (misma
backup que la primera vez), desplegado el fix real
(`_update_bm_master_for_category`: `rows=[]` se trata igual que
`rows=None`, nunca zerea nada), y forzado un restart extra para que
`_bm_master_mem` (el espejo en memoria de las alertas en tiempo real)
también se recargara con los datos restaurados.

### FEAT: auto-halt ante patrón "0,0,0" en categoría de alta venta (commit `d368e17`)

Directiva explícita de Jovan: "si detectas un patrón que es 0, 0, 0 de
categorías muy vendibles debes parar y alertar."

- `_bm_top_category_empty_streak` cuenta "0 filas" consecutivas por
  categoría top (las 5 de mayor venta, refrescadas cada 15 min); se
  resetea en cuanto una respuesta trae datos reales.
- Al llegar a 3, `_bm_category_loop_halt_reason` se llena y
  `_conf_columns_top_categories_loop` deja de llamar a BM en cada ciclo
  (solo duerme y vuelve a chequear) hasta limpiarse manualmente.
- El motivo se reusa en el campo `error` de `/api/stock/prewarm-status`
  — se muestra automáticamente en la fila de estado de Sync Stock al
  abrir la página, sin HTML nuevo.
- Nuevos endpoints: `GET /api/diag/bm-category-loop-status` (ver estado)
  y `POST /api/diag/bm-category-loop-resume` (limpiar, solo tras
  confirmar con `bm-master-update-category` que BM ya responde con datos
  reales).
- Nota: el estado vive en memoria del proceso — un restart de Railway lo
  resetea a 0/limpio, igual que otros flags similares del proyecto.

Verificado en producción: `bm-category-loop-status` responde
`{"halted":false}`, TVs restaurados siguen con stock real tras el
restart, monitor reactivado.

Memoria actualizada: `.claude/memory/project_bm_call_consolidation_2026-08-20.md`
(ampliada con este segundo incidente y el auto-halt), `.claude/agents/binmanager-specialist.md`.

---

## 2026-08-20 — INCIDENTE CRÍTICO + FIX: BinManager bloqueó el acceso real — 4 mecanismos automáticos fuera del loop de categorías

### Contexto

Jovan compartió captura del log real de BM (LogID/ExecutionDate/
P_LocationID/P_Condition) mostrando ~1 llamada cada 3-5 segundos sostenida
por más de un minuto bajo la cuenta de servicio, con LocationID/condición
mezclados en la misma ventana. Confirmó que BM bloqueó el acceso.

### Root cause (confirmado leyendo código, no suposición)

NO fue un script suelto (como el incidente del 2026-08-19). Fueron **4
mecanismos automáticos independientes** en `app/main.py`, cada uno
llamando a BM por su cuenta en cada ciclo de prewarm (×4 cuentas ML):

1. `_fetch_activate_wh` — 1 llamada EN VIVO por SKU (hasta 60, con 1s de
   pausa entre cada una) para la lista "Activar".
2. Bloque bulk dentro de `_get_bm_stock_cached()` (modo `retry_stale=True`)
   — GR + LOC47 + LOC68 + Tijuana + per-SKU warehouse, hasta 5+ llamadas
   de catálogo completo por invocación.
3. `_fetch_tv_wh_breakdown()` — desglose MTY/CDMX/TJ de TVs, programado
   desde dentro del mismo bloque del punto 2.
4. `_check_one_substitution_fulfillment` — verificaba el sustituto
   llamando a BM cada 10 min.

Los 4 duplicaban lo que el loop de categorías
(`_conf_columns_top_categories_loop`/`_conf_columns_longtail_loop` →
`_update_bm_master_for_category`, cutover del 2026-08-19) ya mantenía
fresco en `bm_sku_master` — ninguno leía esa tabla.

### Directiva de Jovan (regla absoluta, cita textual)

"NADA debe pegar a BM, SOLO cuando traemos las categorías con toda la
información, y de esa información debemos alimentar TODO."

### Fix (commit `fdef53d`)

- `_fetch_activate_wh`: reemplazado el loop per-SKU+sleep por una sola
  consulta `token_store.get_bm_master_rows_for_skus()`.
- `_get_bm_stock_cached`: `return` insertado justo antes del bloque de
  llamadas en vivo — ahora lee `bm_sku_master` en una consulta y retorna;
  el código viejo queda inalcanzable (no se borró, por si hace falta
  revertir). Esto también apagó `_fetch_tv_wh_breakdown` de forma
  indirecta (solo se programaba desde ese bloque muerto) — coherente con
  la decisión ya tomada por Jovan el 2026-08-19 de que ese desglose no es
  prioridad.
- `_check_one_substitution_fulfillment`: reemplazado por lectura de
  `bm_sku_master`.

### Mitigación operativa

`DISABLE_BM_MONITOR=true` en Railway mientras se desplegaba el fix (pausa
TODO tráfico automático, incluyendo el loop de categorías bueno) →
reactivado (`false`) tras confirmar el deploy en producción, dejando
activo solo el loop de categorías.

### Pendiente (menor prioridad, no contribuyó al bloqueo de hoy por ser 1×/día)

`app/api/amazon_lanzar.py:299` (gap scan nocturno de Amazon) tiene su
propio `get_bulk_stock()` separado del catálogo de ML — requiere más
cuidado porque usa `ImageURL`, campo que `bm_sku_master` no guarda.

Memoria actualizada: `.claude/memory/project_bm_call_consolidation_2026-08-20.md`,
`.claude/agents/binmanager-specialist.md` (nueva sección al inicio, por
encima de la Regla de Oro de stock).

---

## 2026-08-20 — AUDITORÍA + FIX: 8 correcciones de alto impacto/bajo esfuerzo sobre la lógica de alertas

### Contexto

Jovan pidió una auditoría a fondo de "todas las alertas... ventas, ordenes,
stock, sobreventa, no vendibles, no listados, venta con poco margen, riesgo
de sobre vender, etc." — no de los bugs de datos (ya resueltos el día
anterior), sino de si la LÓGICA de negocio de cada sistema maximiza ventas
y ganancia. Se lanzaron 6 revisiones con agentes especializados
(marketplace-strategist / marketplace-ads-strategist / planning-specialist)
cubriendo 7 sistemas: Alertas de Stock, Sobreventa (3 mecanismos),
Sin Publicar/No Lanzados, Restock Watch + Activar, Márgenes/Deals/
coverage-price-alerts, Stock Crítico + Inventario Estancado, FULL Sin
Stock + huérfanos. Reporte consolidado publicado como Artifact.

Patrón repetido en casi todos: el dato correcto (margen real, días de
cobertura, cantidad recomendada por IA) ya estaba calculado en el mismo
ciclo — el filtro/orden simplemente no lo usaba. Segundo patrón: casi todo
ordenaba por unidades, no por dinero en riesgo.

### Los 8 fixes de "Empieza aquí" (aprobados por Jovan, implementados y desplegados — commit `a3dc790`)

1. **Fórmula de precio sugerido para gaps de ML** (`app/api/lanzar.py`,
   nueva `_ml_suggested_price_mxn`): reemplaza `retail_usd×18×1.20` (ignoraba
   comisión real de ML + envío, nacía con margen NEGATIVO en casi todos los
   casos) por una fórmula que resuelve el precio que recupera 100% del
   costo — EXACTAMENTE el mismo modelo que `_neto_ml`/`_calc_margins` en
   `app/main.py` (fee escalonado 12-18%, retención 9.05%, envío por tramo,
   7% comisión de socio). También usa el FX real ya obtenido (antes
   hardcodeaba 18). Verificado numéricamente: recupera exactamente 100% del
   costo en los 5 tramos probados. Aplicado retroactivamente a los 30,937
   gaps ya en DB vía `/api/lanzar/recalc-prices`.
2. **"Precio < Retail PH" → "Margen Real Insuficiente"** (`price_risk`,
   `app/main.py`): sustituido por `_recup_below_target`/`_neto_ml_negative`
   (mismo criterio ya validado en Deals) en vez de comparar contra
   RetailPrice PH crudo — medía la variable equivocada, con 265 falsos
   positivos probables. Verificado en producción: ahora muestra casos
   reales de margen negativo (ej. SNHT000171, $100 MXN, neto real -$25).
3. **"Activar Todos" y "Sync Reabastecer" (bulk)**
   (`products_stock_issues.html`): usan `_rec_qty` real (ventas de 30d)
   cuando existe, ya no un 60% plano del stock BM crudo — mismo riesgo del
   incidente ya documentado de SHIL000531.
4. **"Stock Crítico" excluye SKUs con 0 ventas** (`critical`,
   `app/main.py`) — ya no aparece simultáneamente en "comprar ya" y
   "Estancado" (liquidar) para el mismo SKU.
5. **Sugerencias de sustituto de Alertas de Stock priorizadas por margen**
   (`get_replacement_sku_suggestions`) — ventana de candidatos plausibles
   por precio, reordenados por margen real (`cost_usd` ya vivía en la
   tabla, antes ignorado).
6. **3 mecanismos de sobreventa ordenan por dinero en riesgo**, no unidades
   — `oversell_risk`, `imbalanced` (×`price`) y `_compute_oversell_exposure`
   (×`retail_ph`, expone `exposure_usd` por fila). Verificado en producción.
7. **Calculadora de margen del wizard de lanzamiento** (`lanzar_gaps.html`):
   tenía el % de comisión INVERTIDO (Premium/gold_pro default = 8%,
   Clásica = 12.5%), sin retención fiscal, envío fijo $150 sin importar
   tamaño. Ahora usa la misma fórmula que `_neto_ml`.
8. **"Deal con margen negativo" ya no depende de tener RetailPH** — nuevo
   campo `_neto_ml_negative` en `_calc_margins`, independiente de
   `_retail_mxn>0`. Antes un deal con pérdida real sin RetailPH en BM nunca
   disparaba la alerta.

Ninguno automatiza escrituras nuevas a ML/Amazon/BM — solo corrige qué se
sugiere/prioriza/ordena; las acciones siguen 100% gateadas por click
humano. Deploy Railway `SUCCESS`, verificado contra producción (oversell
audit ordenado por `exposure_usd`, Stock Crítico bajó de 222 a 1 para la
cuenta admin tras excluir 0-ventas, price_risk mostrando neto real
negativo). Resto de recomendaciones de la auditoría (conectar sobreventa
con velocidad de venta, notificaciones proactivas, unificar Restock Watch/
Reabastecer, extender coverage-price-alerts a Amazon, etc.) quedan
pendientes de aprobación — ver
`.claude/memory/project_alerts_audit_2026-08-20.md`.

---

## 2026-08-19 — FIX: respaldo getOrder puntual para alertas Amazon que getOrders (lista) no devuelve

Tercer y último hallazgo de la revisión del lado Amazon pedida por Jovan
(mismo día, ver entrada de abajo para los 2 primeros). De las 7 alertas
VECKTOR fantasma, 3 órdenes (`701-0674967-4481812`, `701-6812787-3779429`,
`702-3480491-5024235`) resultaron estar YA en status `Shipped` (confirmado
con `getOrder` puntual vía `/api/diag/amazon-order-status`), pero seguían
sin limpiarse porque `getOrders` (la lista, con la MISMA ventana de fecha y
status) simplemente no las devolvía — el mismo quirk de SP-API ya
documentado 2026-08-18 para esta cuenta (`/api/diag/amazon-orders-list`),
nunca cerrado del todo en ese momento.

Verificación independiente de que estos 6 SKUs no son catálogo BM en
absoluto (más allá de `/api/diag/sku`): las tools MCP de BinManager
(`sc_exists_lpn_or_sku` + `inventory_snapshot`) confirman `Is_SKU=false`,
`Is_LPN=false`, 0 filas para los 6 — BM no los conoce por ningún camino.

`_run_amazon_stock_reconcile_pass` (`app/main.py`) se refactorizó: la
evaluación por-orden vive ahora en `_process_one_amazon_order()` (compartida
por el camino normal vía `getOrders` y por el respaldo, para no duplicar el
criterio en 2 lugares que puedan divergir). Al final de cada pasada, cualquier
orden con una alerta Amazon abierta que la lista NO devolvió se revisa con
`AmazonClient.get_order()` (nuevo wrapper de `GET /orders/v0/orders/{id}`,
`amazon_client.py`) antes de cerrar el ciclo — cierra el hueco sin depender
de que Amazon corrija su propia lista.

Commit `fadeedf`, deploy Railway `SUCCESS`.

---

## 2026-08-19 — FIX: alertas de stock Amazon (VECKTOR) — mismo bug de auto-heal del lado ML + filas fantasma de SKUs no-BM

### Contexto

Tras cerrar el bug de ConfColumns/TRANSITO (entrada de abajo) y el self-heal
de alertas ML (misma fecha), Jovan pidió revisar también el lado Amazon:
las 12 alertas restantes de la reconciliación de esa tarde incluían 7 de
VECKTOR con SKUs numéricos (8508943, 8508614, 8508883, 8508808, 8508515,
8508800) — nunca confirmadas como "genuinamente sin stock", solo como
pendiente de revisar.

### 2 bugs reales encontrados en `_run_amazon_stock_reconcile_pass`

**1. Mismo bug de auto-heal que ML** — esta pasada solo CREABA una alerta
cuando `avail<=0`, nunca la borraba cuando el stock volvía a estar
disponible. Fix: mismo patrón que ML, `delete_realtime_stock_alert_for_order_sku`
en el `else`.

**2. Filas fantasma en `bm_sku_master` para SKUs que BM nunca reconoció.**
Los 7 SKUs de VECKTOR resultaron ser productos dropship (códigos de
fabricante, no convención SN/SH) que BinManager JAMÁS ha confirmado en
ningún ciclo — verificado de forma independiente con `/api/diag/sku`
(cache/bulk/BM en vivo, los 3 en `found: false`) y con las tools MCP de
BinManager (`sc_exists_lpn_or_sku` + `inventory_snapshot`, ambas sin
resultado para los 6 SKUs). Mientras tanto, Amazon SÍ mostraba 48-50
piezas reales en las variantes efectivamente vendidas (`8508943-B-1`,
`8508943-W-1`, etc.) — el filtro "es catálogo BM" agregado el 2026-08-18
solo exigía que la fila EXISTIERA en `bm_sku_master`, y estas filas
fantasma (`available_qty=0, category='', verified=0`) la satisfacían
igual que un SKU real. Fix: el filtro ahora exige también `verified=1`
(BM confirmó el dato en algún ciclo real de sync), no solo presencia de
la fila — limpia de inmediato las 7 alertas fantasma en el siguiente
ciclo de reconciliación (cada 5 min), sin script de limpieza aparte.

Commit `31af9f1`, deploy Railway `SUCCESS`.

---

## 2026-08-19 — BUG CRÍTICO + ARQUITECTURA: ConfColumns mezclaba bins TRANSITO como vendible — `bm_sku_master` migró de vuelta a `Get_GlobalStock_InventoryBySKU`

### Contexto — reversa de la migración logueada arriba en el mismo día

Después de completar y automatizar la migración a `ConfColumns_Conditions_Excel`
(entradas de arriba, mismo día), se detectó que ese endpoint filtra
`LOCATIONID` correctamente pero **no excluye stock físicamente sentado en
un bin de `BinTypeID=1 "TRANSITO"`** (bins temporales, ej. `TRMXB2B002`,
`TRANSFR001/002/003` en Autobot CDMX) aunque la condición sea vendible
(GRA/GRB/GRC/NEW). Prueba en la categoría "Headphones-JLab": **117 de 123
SKUs conocidos mostraban disponibilidad falsa** por esta causa — sistémico,
no un caso raro (detectado al investigar SNHP000093/SNHP000097 a pedido
de Jovan).

Se probaron 2 nombres de parámetro para filtrar tránsito
(`BINTYPEID`, y `InventoryType` — sugerido directamente por Alberto,
desarrollador de BinManager) en `ConfColumns_Conditions_Excel` Y en
`Get_GlobalStock_InventoryBySKU`: **ambos endpoints ignoran el parámetro
silenciosamente**, sin excepción, en las 4 combinaciones probadas
(`/api/diag/confcolumns-bintype-test`, `/api/diag/globalstock-bintype-test`).

### Fix real: no fue un parámetro, fue volver a cambiar de fuente

`Get_GlobalStock_InventoryBySKU` (CONCEPTID=1, el mismo endpoint que ya
usa `_refresh_bm_avail_live` para verificación puntual) **ya excluye
tránsito correctamente sin ningún parámetro extra** — confirmado con
`inventory_no_vendible` (BinManager MCP) y con pruebas cruzadas en los
SKUs afectados. Jovan confirmó explícitamente: "ENtonces pdemos usar este
como el maestro para todo?" → sí, cutover completo:

- `binmanager_client.get_bulk_stock()`: nuevo param `category_id`, ya no
  hardcodeado a `None` — permite scoping por categoría igual que
  ConfColumns.
- `app/main.py` — `_update_bm_master_for_category()` reescrito para
  llamar `get_bulk_stock(category_id=..., conditions=...)` en vez de
  ConfColumns (Televisions sigue usando `GRA,GRB,GRC,ICB,ICC,NEW`, el
  resto `GRA,GRB,GRC,NEW`).
- Nueva `_bulk_stock_rows_to_master_fields()` reemplaza
  `_conf_columns_row_to_master_fields()` (dejada en el código, marcada
  DEPRECATED, no se borra por si algún diag viejo la referencia).
- Backup de `bm_sku_master` antes del cutover (`/api/diag/bm-master-backup`)
  + full-resync de las 59 categorías (`/api/diag/bm-master-full-resync`),
  verificado categoría por categoría.

### FIX relacionado — "Activar" mostraba falsos "0 disponible"

Reporte real de Jovan con capturas: la página Activar (Productos) mostraba
0 disponible para SKUs que sí tenían stock real. Root cause:
`_refresh_bm_avail_live()` leía de `_bm_stock_cache`, alimentado por
`InventoryBySKUAndCondicion_Quantity` — endpoint confirmado roto
server-side (error SQL "Invalid column name 'binid'", ya documentado en
varios archivos de `app/api/`) que a veces regresa HTTP 200 con datos
corruptos marcados como éxito. Fix: `_refresh_bm_avail_live` ahora lee de
`_bm_master_mem` (requiere `entry.get("verified")`), la misma fuente ya
corregida arriba. Verificado: SNHP000114 pasó de 0 → 468 piezas reales.

### FIX — alertas de stock en tiempo real nunca se auto-resolvían

`_evaluate_order_stock_alert()` solo creaba/actualizaba la alerta cuando
`avail<=0`, nunca la borraba cuando el stock volvía a estar disponible
(ni por el cutover de arriba ni por ningún otro evento). Fix: nueva
`token_store.delete_realtime_stock_alert_for_order_sku()`, llamada en el
`else` de `_evaluate_order_stock_alert` cuando `avail>0`. **Solo
bookkeeping interno en DB — no automatiza ninguna acción de marketplace**
(Activar/Ajustar/Sustituir siguen 100% gateados por botón humano, sin
excepción — confirmado explícitamente con Jovan). Ya corre dentro de los
loops de reconciliación existentes (`_realtime_stock_reconcile_loop`
cada 5 min + `_realtime_stock_reconcile_wide_loop` cada 2h) — no hizo
falta crear una cadencia nueva de 15 min, ya existía una más frecuente.
Verificado con 3 pasadas de reconciliación (ventanas 48h→336h): alertas
18→13→12, las 12 restantes confirmadas como genuinamente sin stock real.

### FIX — sustitución BM rechazada cuando el "sustituto" es el producto real ya registrado

`_inject_bm_alter_sku()`: nueva validación que rechaza de inmediato
(antes de llamar BM) cuando `substitute_sku == real_product_sku` ya
resuelto — antes BM devolvía el mismo "Payload Error!" genérico que otros
2 casos distintos, sin indicar la causa real (caso SNTV002236-GRB, que
era el ProductSKU real registrado, no una condición alterna válida). Ver
`.claude/memory/project_bm_alter_sku_mapping.md` para el detalle completo
de los 4 bugs de esta feature.

### FIX UI — tooltip nativo feo en Alertas de Stock

`orders.html`, tabla Alertas de Stock, columnas Sugerencia y Título:
reemplazado el `title=""` nativo del navegador (se veía cortado/mal
posicionado en captura real de Jovan) por un tooltip custom (Tailwind
`relative group` + `hidden group-hover:block absolute`, fondo oscuro).
Solo aplicado en estas 2 columnas — el mismo patrón nativo existe en
~30 templates más del proyecto, pendiente como tarea aparte si se pide.

Todo lo anterior verificado en producción (Railway) y push a `origin` +
`mi2`. Memoria del proyecto actualizada: `.claude/agents/
binmanager-specialist.md` (Regla de Oro + LOCATIONID vendible corregido
a `47,62,68`, Tijuana excluida desde 2026-08-05 — esa corrección estaba
pendiente de reflejarse ahí) y memoria personal
(`project_bm_confcolumns_transito_bug_and_master_cutover.md`).

---

## 2026-08-19 — FEAT: automatizados los 2 loops de ConfColumns por categoría (cierre del plan de BinManager)

Cierre del plan pedido por BinManager (ver entrada anterior del mismo
día, Fases 1-2). Extraída la lógica de escritura segura de
`/api/diag/bm-master-update-category` a `_update_bm_master_for_category()`
(compartida) y conectada a 2 loops nuevos:

- `_conf_columns_top_categories_loop()` — top 5 categorías por ventas
  reales (Televisions/Air Conditioners/etc.), cada **15 min**.
- `_conf_columns_longtail_loop()` — el resto de categorías conocidas,
  cada **4 horas**.

Ambos con 10s de pausa real entre cada categoría (nunca ráfaga) y las
mismas 2 reglas de seguridad validadas hoy (fetch fallido no toca nada;
ausencia confirmada = 0 solo dentro de la misma categoría).

**Corren EN PARALELO al mecanismo viejo** (`_bm_master_sync_loop`/
`Get_GlobalStock_InventoryBySKU`) -- todavía sin apagar, deliberadamente:
primero validar en producción real por un tiempo antes de cortar el
cable al camino viejo. Deploy Railway `SUCCESS` -- el loop top arranca
solo ~10 min después del deploy, el de cola larga ~30 min después, sin
necesidad de disparar nada a mano.

**Pendiente real para retomar**: una vez confirmado estable, decidir
cuándo apagar `get_bulk_stock`/`Get_GlobalStock_InventoryBySKU` para
`bm_sku_master` -- ese mecanismo también alimenta el desglose MTY/CDMX/TJ
(Transferencias Sugeridas), que ConfColumns no provee -- ese desglose
necesita su propio plan aparte antes de poder apagar el viejo por
completo.

---

## 2026-08-19 — FEAT: migración a ConfColumns_Conditions_Excel por categoría (pedido explícito de BinManager) — Fase 1 y 2 validadas

BinManager pidió (vía Jovan) dejar de usar `Get_GlobalStock_InventoryBySKU`
("pagedata") para el sync automatizado de alto volumen y migrar a
`ConfColumns_Conditions_Excel` ("el excel"). Diseño acordado tras varias
vueltas: por categoría, en orden de ventas reales (no todo el catálogo
de un jalón — probado hoy mismo que eso puede tardar >120s de forma
impredecible y degradar todo lo demás mientras el semáforo global de
BM está ocupado).

**Confirmado por BinManager**: este endpoint no expone `Reserve` por
separado -- `Available` ya viene neto. Decisión de Jovan: `reserve_qty=0`
fijo para datos de esta fuente (ya no se muestra el desglose reservado
para estos SKUs).

**Fase 1 — orden de categorías por ventas** (`get_categories_ordered_by_sales()`,
`token_store.py`, solo lectura): Televisiones domina con **$56.3M MXN**
en 90 días (13,727 unidades), Aires Acondicionados segundo con $3.78M —
todo lo demás es cola larga. Nuevo diag `/api/diag/categories-by-sales`.

**Fase 2 — actualización segura por categoría** (`get_conf_columns_catalog()`
corregido + `/api/diag/bm-master-update-category`, POST): 2 reglas de
seguridad (aprendidas a la fuerza hoy mismo con 2 bugs reales del mismo
tipo): (1) fetch fallido (`None`) nunca toca `bm_sku_master`; (2) fetch
exitoso pero un SKU conocido de la MISMA categoría ausente = confirmado
en 0 (mismo criterio "ausencia en bulk = 0" ya usado en el resto del
sistema), acotado por categoría para no zerear SKUs de otra categoría
por error.

**Probado con la categoría #1 (Televisions), 1 sola llamada real**: 1,750
filas en ~15s, 1,234 SKUs conocidos actualizados, 507 confirmados en 0.
Verificado `SNTV001764`: `available_qty=39` (correcto, solo condiciones
vendibles) vs `total_qty=4346` (bruto, incluye no-vendibles) -- la
distinción funciona bien.

**Pendiente (siguiente sesión o continuación)**: automatizar esto en un
loop recurrente (por ahora son 2 pasos manuales via diag), decidir
cadencia por nivel de prioridad (categorías top más seguido, cola larga
más espaciado), y solo entonces apagar el mecanismo viejo
(`Get_GlobalStock_InventoryBySKU`/`get_bulk_stock`) que sigue corriendo
en paralelo sin cambios.

---

## 2026-08-19 — BUG CRÍTICO + FIX: scan de gaps borró "No Lanzados" de las 4 cuentas ML + cadencia bajada a 1x/día

Jovan reportó "No Lanzados" en 0 para la cuenta Autobot. Verificado
contra producción (solo lectura, `/api/lanzar/gaps-summary` por cuenta):
**las 4 cuentas ML** (APANTALLATEMX, BLOWTECHNOLOGIES, LUTEMAMEXICO,
AUTOBOT) estaban en 0, no solo Autobot.

**Causa raíz**: el scan global de las 19:01-19:06 de hoy recibió `0`
SKUs de BM (`bm_gap_scan_status.total_skus=0`) — un fallo silencioso de
`ConfColumns_Conditions_Excel` (posiblemente relacionado con la
inestabilidad de sesión de BM del mismo día, ver entradas anteriores de
hoy). El código de limpieza de gaps obsoletos (`app/api/lanzar.py`) usa
`DELETE FROM bm_sku_gaps WHERE ... sku NOT IN (<lista de BM>)` — con la
lista vacía, esa condición es verdadera para CUALQUIER sku real, así
que **borró todos los gaps 'unlaunched' de las 4 cuentas** de un jalón,
sin marcar error (`status: 'done'`, no `'error'`).

**Fix**: nuevo guard — si BM devuelve menos de 1,000 SKUs con stock (el
catálogo real siempre tiene miles, confirmado: solo "Televisions" trae
~800), se aborta ANTES de tocar `bm_sku_gaps` y se marca `status='error'`
en vez de `'done'`. Los gaps existentes quedan intactos ante cualquier
fallo futuro de este tipo.

**Además (pedido por Jovan, dado el reporte real de carga de
BinManager de hoy)**: cadencia del scan bajada de cada 3h a **1 vez al
día, 3am hora México** (fijo, sin horario de verano). Se evaluó hacer
la llamada categoría por categoría (~120 llamadas), pero el catálogo de
categorías de BM (vía MCP) resultó incompleto/inconsistente contra lo
que `ConfColumns` realmente etiqueta por SKU — una lista fija de
categorías arriesgaba omitir categorías reales en silencio, el mismo
tipo de bug que se acaba de corregir. Se mantiene la llamada única ya
probada (todo el catálogo de un jalón), seguro ahora sin importar la
frecuencia gracias al guard nuevo.

**Recuperación**: disparado 1 scan manual (`POST /api/lanzar/scan-all`,
vía el mecanismo propio de la app) tras el deploy — restauró datos
reales para las 4 cuentas: 7,012-8,168 gaps por cuenta, ~1M unidades de
stock cada una, potencial de ingresos $2,200-2,350M MXN por cuenta.
`total_skus: 9294` (muy por encima del umbral de 1,000 — confirma que
el fetch estaba sano esta vez). Deploy Railway `SUCCESS`.

---

## 2026-08-19 — FEAT: "No Lanzados" de Amazon igualado al desglose por categoría que ya tenía ML

Continuación de la exploración de `ConfColumns_Conditions_Excel`
(entrada anterior del mismo día). Jovan preguntó cómo decidir en qué
categorías vale la pena invertir esfuerzo, señalando que basarse solo en
ventas actuales es sesgado (una categoría puede no vender simplemente
porque nunca se lanzó bien, no porque no haya demanda) — pidió mejor
mirar "stock real pero sin publicar" por categoría.

**Hallazgo: esa feature ya existía, solo para ML.** El gap scan de ML
(`_run_gap_scan`, cada ~3h, `app/api/lanzar.py`) ya usa
`ConfColumns_Conditions_Excel` con un payload simple (sin `CATEGORYID`)
que sí trae las ~120 categorías completas de BM en una sola llamada
(la prueba anterior había fallado por un payload propio con demasiados
campos, no por una limitación real del endpoint). `GET /api/lanzar/filters`
ya devuelve categoría+conteo+stock total, usado en la página "No
Lanzados" (`/bm/unlaunched`) con un dropdown "Categoría (N)".

**Lo que faltaba: Amazon no tenía el mismo desglose.** `amz_sku_gaps` ya
guardaba `category` por SKU (el scan de Amazon también la captura), pero
`GET /products/sin-lanzar` (`app/api/amazon_products.py`) solo exponía
una lista plana de nombres, sin conteo ni stock. Igualado con la misma
regla de ML: `GROUP BY category` con `COUNT(*)` y `SUM(avail_qty)`,
ordenado por conteo descendente. También se agregó un badge de
categoría por fila en la tabla (`app/templates/partials/amazon_sin_lanzar.html`),
igual al que ya tenía ML.

Verificado contra producción (solo lectura de `amz_sku_gaps`, cero
llamadas a BM/Amazon en vivo): 585 SKUs sin publicar, con categorías
reales como "Televisions" (131, 181 uds), "Personal Protective
Equipment" (111, 10,573 uds), "Kits Parts TVs" (87, 488 uds). Deploy
Railway `SUCCESS`.

---

## 2026-08-19 — EXPLORACIÓN: probado `ConfColumns_Conditions_Excel` (endpoint sugerido por BinManager) — sirve por categoría, NO para "todas de un jalón"

Un programador de BinManager le compartió a Jovan este endpoint como
alternativa para bajar un lote completo por categoría en vez de SKU por
SKU. Se corrigió primero una idea equivocada: `NEEDFILE` no genera un
Excel descargable, es para incluir fotos — el endpoint devuelve JSON
normal.

**2 llamadas de prueba, controladas, vía el diag nuevo
`/api/diag/bm-category-bulk-probe`** (respeta `_BM_GLOBAL_SEM`, no scripts
sueltos — ver [[feedback_no_scripts_sueltos_contra_bm]]):

1. `CATEGORYID=None` ("todas las categorías de un jalón") — **falló**
   (excepción sin mensaje, probablemente demasiado pesado para BM sin
   paginación — el payload no tiene `NUMBERPAGE`/`RECORDSPAGE` a
   diferencia de `Get_GlobalStock_InventoryBySKU`).
2. `CATEGORYID="Televisions"` — **funcionó**, 789 filas en ~19s. Formato
   genuinamente distinto y más rico que nuestro bulk actual: **una fila
   por SKU base**, con cada condición como su propia columna
   (`GRA`,`GRB`,`GRC`,`NEW`,`ICB`,`ICC`,`ICX`,`BOX`,`DMT`, etc. — ~30
   columnas de condición), más `Brand`/`Model`/`Title`/`UPC`/`Size`/
   `LastRetailPricePurchaseHistory`/`Tier`/`Available`/`TotalQty`. (Ojo:
   `RetailPrice` viene en 0 con `RetailPriceAvailable:false` — coincide
   con lo ya documentado en CLAUDE.md, usar `LastRetailPricePurchaseHistory`.)

**Conclusión sobre "todas las categorías" (lo que pidió Jovan):** NO es
viable en una sola llamada, y bajarlo categoría por categoría (~120
categorías reales en BM, confirmado con el catálogo del MCP) tampoco es
viable cada 10 min (~19s × 120 ≈ 38 min por ciclo, secuencial por el
semáforo — justo el tipo de carga que generó la queja de BinManager
esta misma sesión). Si se quiere usar, tendría más sentido acotado a
categorías puntuales de alto valor (ej. solo "Televisions", que ya tiene
manejo especial ICB/ICC) o como herramienta de auditoría ocasional, no
como reemplazo del ciclo de 10 min actual.

Diag dejado en el repo (solo lectura, gated por `DIAG_TOKEN`, no se
llama automáticamente) para retomar si se decide explorar más.

---

## 2026-08-19 — FIX: Pendientes de Envío seguía mostrando sustituciones fallidas (el revert anterior nunca se aplicó de verdad)

Jovan reportó con captura que 2 sustituciones ya fallidas (`SNWA000090-NEW`
y `SNWA000001-NEW`, orden `2000018008535734`) seguían apareciendo en
"Pendientes de Envío" después del fix anterior del mismo día.

**Causa: error propio, no del sistema.** El commit `7914aad` (más
temprano hoy) DECÍA en su mensaje "Pendientes de Envío revertido a su
alcance original" pero nunca aplicó ese cambio real —
`get_pending_shipment_resolutions` (`app/services/token_store.py`)
seguía con la condición ampliada (`bm_status IN ('pending','failed')`)
de la versión intermedia que ya se había descartado en la conversación.
Afirmé haber hecho un cambio que no hice.

Corregido ahora sí: `WHERE resolution_type='substitution' AND
bm_status='success' AND fulfillment_status IN ('','pendiente_envio')
AND bm_deleted_at IS NULL` — de vuelta a su forma original. Verificado
contra producción tras el deploy: la lista quedó en 2 filas reales
(`SNTV003147-GRB`, `SNTV007885-GRB`, ambas `bm_status='success'`), las
2 fallidas ya no aparecen.

Sin llamadas a BM involucradas en este fix (cambio puro de SQL/DB).

---

## 2026-08-19 — INCIDENTE + FIX: BinManager reportó carga alta — llamadas puntuales en vivo eliminadas del flujo de sustitución

BinManager avisó (vía Jovan, con log real de ejecuciones de BM) que la
app le estaba pegando duro — ráfaga de ~18 consultas en <2 min desde
`Claude.Jovan@mitechnologiesinc.com`.

**Causa real, no la app en producción operando normal:** durante el
diagnóstico del caso SNWA000024 de esta misma sesión, se corrieron
varios scripts de Python sueltos (`py -c "..."`) que cada uno abre su
propia sesión con BM — **no pasan por el semáforo global
`_BM_GLOBAL_SEM`** (pensado para 1 sola request activa en TODA la app),
porque cada script es un proceso aparte sin coordinación con nada. Sumado
al tráfico normal de la app, generó la ráfaga.

**Cambio de fondo, más allá del incidente puntual:** los 2 endpoints
nuevos de hoy (`/api/stock/substitute-conditions` y la validación en
`_inject_bm_alter_sku`) llamaban a `get_existence_anywhere()` EN VIVO —
uno por cada SKU tecleado en el modal "Sustituir", el otro por cada
intento de sustitución. Jovan pidió explícitamente reemplazarlos por el
mismo bulk que YA se descarga cada `_BM_BULK_TTL` (10 min) para
`bm_sku_master` — mismo principio que
`feedback_preferir_solucion_simple_del_bulk`. Nuevo helper compartido
`_bm_bulk_real_conditions()` (`app/main.py`) — cero llamadas nuevas a BM
en todo el flujo de sustitución.

**2 bugs reales encontrados al reemplazar (no solo "funcionó a la
primera")**:
1. La versión anterior de `/api/stock/substitute-conditions` tenía una
   variable (`bm_cli`) sin asignar — por eso fallaba rápido y en
   silencio (un `except Exception` genérico lo capturaba), no era
   degradación de sesión de BM como se sospechó al investigar.
2. BM a veces guarda el SKU base SIN sufijo de condición como su propia
   fila en el bulk (mismo caso ya conocido y descartado en
   `/api/stock/live-check`, "un resolved_sku sin sufijo no aporta nada
   nuevo") — sin filtrarlo, aparecía como opción de condición vacía.

**Además (pedido explícito de Jovan, corrección de diseño):**
`Pendientes de Envío` vuelve a su alcance original
(`bm_status='success'` únicamente) — el ensanche de la entrada anterior
del mismo día (incluir `pending`/`failed`) fue un error de diseño: un
intento fallido ya tiene su dictamen, uno sin resolver no es "todo
funcionó bien esperando enviarse", ninguno de los 2 pertenece ahí. Esas
órdenes siguen vivas en "En vivo" para reintentarse desde cero (ahora sin
riesgo de repetir un SKU inválido, gracias al selector de condiciones).

Verificado contra producción: `/api/stock/substitute-conditions`
responde en milisegundos leyendo solo caché en memoria, sin ninguna
llamada nueva a BM. Deploy Railway `SUCCESS`.

---

## 2026-08-19 — FEAT: Historial solo-cerrado + Pendientes ampliado + selector de condiciones reales

Cierre de los 2 pendientes de diseño que quedaron aprobados (no
implementados) en la entrada anterior del mismo día:

1. **Historial = solo cerrado.** `get_stock_alert_resolutions(closed_only=True)`
   (`app/services/token_store.py`) excluye ahora lo que sigue en proceso
   (verificando con BM, fallido, o aplicado-sin-enviar) — "Historial es
   solamente cuando algo ya fue confirmado y enviado" (Jovan). El
   parámetro es opt-in porque 3 endpoints más (reopen/retry-bm/
   delete-from-bm) siguen necesitando encontrar CUALQUIER fila por id.
2. **Pendientes de Envío ampliado.** `get_pending_shipment_resolutions`
   ya no solo cubre `bm_status='success'` esperando envío — también
   `pending`/`failed`, con botón "🔁 Reintentar" visible ahí mismo
   (`app/templates/orders.html`). Es el complemento EXACTO de la
   condición de Historial (misma fórmula negada) para que cada
   resolución viva en un solo lado, nunca en ambos ni en ninguno.
3. **Selector de condiciones reales.** Nuevo endpoint
   `GET /api/stock/substitute-conditions` (`get_existence_anywhere` en
   vivo, filtrado a condiciones de venta válidas por tipo de SKU) — al
   escribir un SKU base sin sufijo en el modal "Sustituir", se muestran
   botones tipo "SNWA000001-GRC (1 disp.)" en vez de dejar el campo
   incompleto como pasó con la resolución #39. De paso, `_inject_bm_alter_sku`
   ahora RECHAZA un sustituto sin condición en vez de saltarse la
   validación (hueco real que dejó pasar exactamente ese caso).

**Bug real encontrado al verificar contra producción (no solo local)
tras el primer deploy**: la resolución #39 (reabierta mientras
`bm_status` seguía en `'pending'`) no aparecía en NINGUNA de las 2
vistas — el guard de `fulfillment_status='reabierta'` solo se había
agregado al lado `success` de la condición de Pendientes, no al lado
`pending`/`failed`. Corregido en ambas queries (mismo guard, mismo
complemento exacto) y reverificado en producción: `pending-shipment` ya
no trae el id 39, `resolutions` (Historial) sí lo trae.

Deploy Railway `SUCCESS` (2 despliegues: fix inicial + corrección del
gap encontrado al verificar).

---

## 2026-08-19 — OPERACION + FEAT: resolución #39 atorada (orden 2000018008535734) + Historial paginado

Jovan reportó otro detalle del mismo caso: la resolución #39 (Alex,
sustituto `SNWA000001` sin condición) llevaba 11.5 min en
`bm_status='pending'` mostrada en Historial. Verificado contra
producción (sesión real, no supuesto) y contra BM en vivo
(`/api/diag/bm-alter-sku-groups`): nunca se intentó de verdad contra
BM — los 3 AlterSKUs de `SNWA000024-GRC` siguen siendo los mismos de
abril 2025 (GRB/NEW/GRA), no rechazada, simplemente atorada (el
background task nunca corrió, mismo patrón ya documentado de "perdió su
turno"). Por instrucción explícita de Jovan, NO se inyectó ese SKU
incompleto a BM — se usó `/api/stock/alerts/resolutions/39/reopen`
(reabre la orden en "En vivo" con sugerencias frescas de sustitutos con
stock real: `SNAC000045`, `SNPA000019`, `SNDH000031`).

**Pendiente de diseño (aprobado por Jovan, sin implementar aún):**
1. Selector de condiciones reales (GRA/GRB/GRC/NEW etc.) al escribir un
   SKU base sin sufijo en el modal de "Sustituir" — hoy el auto-resuelve
   (`_resolve_bm_condition_sku`) se rinde si hay 0 o >1 grupos y deja el
   campo tal cual (esto fue lo que causó el `SNWA000001` sin condición).
2. `Historial` debe mostrar solo lo cerrado (`bm_status='success' AND
   fulfillment_status IN ('completado','cancelada')`, o borrado de BM);
   todo lo demás (verificando, fallido, aplicado-sin-enviar) debe vivir
   solo en `Pendientes de Envío`. Hoy Historial muestra todo sin filtro.

**Hecho ya:** Historial paginado a 20 filas/página (`app/templates/orders.html`,
mismo patrón `_applySkuPage`/`_renderSkuPagination` ya usado en "Por
SKU") — la tabla se estaba haciendo muy larga con 100 filas de golpe.
Deploy Railway `SUCCESS`.

---

## 2026-08-19 — PERF/DECISION: TTL de cache bulk de stock BM bajado de 15 a 10 min + aclaración fuente MCP vs. fuente propia

Mismo caso de `SNWA000024` destapó una segunda confusión: Jovan preguntó
por qué le reporté 15 unidades disponibles cuando BM en vivo mostraba
mucho menos, y si la tabla que consulté era "la misma que usamos para
todo".

**No lo era.** Son dos sistemas separados:
- El conector MCP de BinManager (usado por mí para diagnosticar en el
  chat) tiene su PROPIA tabla materializada (`BM.MCP_InventorySnapshot_ByLocation`),
  en infraestructura de BM, fuera de nuestro control — ahí fue el error,
  no en nuestro pipeline.
- Nuestra fuente real (`bm_sku_master`, la que alimenta Alertas de Stock,
  gaps, todo) ya tenía el número correcto (`available_qty: 0`) desde
  antes de esta conversación — verificado con `sp_Get_GlobalStock_InventoryBySKU_Condition`
  en vivo (2 llamadas, resultados idénticos, mismos seriales/bins): 0
  unidades reales en GRA/GRB/GRC/NEW, todo lo demás en Pendiente de Caja/
  Reparación/Dañado, y 1 unidad "Producto Vendible" pero en condición ICC
  — que por regla del proyecto (ICB/ICC solo para SNTV*, TVs) no cuenta
  como vendible para este SKU (no es TV).

**Cambio real aprobado por Jovan:** bajar el TTL de nuestras 5 cachés
bulk de stock BM (`_bm_bulk_gr_cache`/`_all`/`_loc47`/`_loc68`/`_loctj`,
que alimentan `bm_sku_master`) de 900s a 600s (15→10 min). Se descartó
5 min: un ciclo de refresco completo (GR+LOC47+LOC68+LOCTJ+ALL,
secuencial, mismo semáforo global) puede tardar hasta ~10-14 min bajo BM
degradado — los incidentes reales del 12-ago y 18-ago fueron causados
exactamente por este tipo de presión de frecuencia. Nueva constante
compartida `_BM_BULK_TTL` (`app/main.py` ~6352), reemplaza el literal
900 hardcodeado en 4 lugares (GR, LOC, ALL/TVs, desglose MTY/CDMX/TJ
para TVs).

**Hacia adelante:** para cualquier número de stock que le reporte a
Jovan, uso `inventory_by_sku_condition` (SP en vivo, sin caché) del MCP
de BM, o nuestro propio `/api/diag/sku` — nunca `inventory_by_sku` (la
tabla materializada de 15 min que causó este error).

Verificado localmente antes de subir. Deploy Railway `76e92c95` —
`SUCCESS`.

---

## 2026-08-19 — FIX: sustitución de SKU a BinManager fallaba en silencio con AlterSKU inexistente (orden 2000018008535734)

Jovan reportó que varias sustituciones seguían fallando ("Falló en BM") y
"Reintentar" nunca lograba nada — caso puntual: orden `2000018008535734`
(BLOWTECHNOLOGIES), SKU original `SNWA000024`, intentos de sustituto
`SNWA000001-NEW` y `SNWA000090-NEW`.

Diagnóstico contra BM real (BM MCP + `/api/diag/order-lookup` +
`/api/diag/bm-alter-sku-groups`, sin tocar nada): **no era un bug de
timing ni de conexión** — `SNWA000001` solo tiene las condiciones
`BOX,DMB,DMT,GRB,GRC,ICC,ICX` y `SNWA000090` solo `BOX,GRB,ICX`. Ninguno
de los dos tiene condición `-NEW`. BM rechaza `AddAlterSKUMappingByWebSKU`
con HTTP 200 cuando el AlterSKU pedido no es un ProductSKU real (mismo
patrón ya documentado en `project_bm_alter_sku_mapping.md`, bug #3) —
"Reintentar" jamás podía funcionar porque el par (ProductSKU, AlterSKU)
nunca iba a existir. El motivo real sí se guardaba en `bm_message`, pero
solo se veía al pasar el mouse sobre el badge "✗ Falló en BM" (nadie lo
revisaba).

**Fix (2 partes, `app/main.py` ~16899 / `app/templates/orders.html`
~712-733):**
1. `_inject_bm_alter_sku` ahora valida el `substitute_sku` contra
   `BinManagerClient.get_existence_anywhere()` (mismo helper ya usado
   para el panel informativo de `items.py`) ANTES de llamar a BM — si la
   condición pedida no existe, rechaza de inmediato con las condiciones
   reales disponibles para ese SKU, sin gastar un intento contra BM.
2. El historial de Alertas de Stock ahora muestra el `bm_message` real
   como texto visible debajo del badge "Falló en BM", no solo en tooltip.

Para la orden puntual: `SNWA000024-GRB` (15 uds en MTY) ya está
configurado como alternativa `GLOBAL` en BM desde 2025-04-07 — cubre esta
orden sin necesidad de crear ningún mapeo nuevo (verificado en vivo,
`already_existed: true`).

Verificado localmente (`py -m uvicorn`, llamada directa a
`_inject_bm_alter_sku` contra BM real) antes de subir: rechazo correcto
para `SNWA000090-NEW`, y `already_existed` correcto para
`SNWA000024-GRB` sin ningún POST de escritura. Deploy Railway
`4b1cb697` — `SUCCESS` (2026-08-19 16:54 UTC).

---

## 2026-08-18 — FIX: títulos de producto duplicados/"mocha" en Alertas de Stock (dato sucio de BM, limpieza en display)

Jovan reportó que los títulos en "Alertas de Stock" se veían mal (ej.
"Hampton Bay HDP99180BRN Hampton BayHDP99180BRNKelford 18 in..."). Se
confirmó con evidencia real que **no es un bug de nuestro código** —
`bm_sku_master.title` se toma directo del campo `Title` de BinManager sin
tocarlo (`_row.get("Title") or ""`, `main.py` ~línea 5946); BM mismo
guarda el título duplicado para varios SKUs (prefijo `SH..`, parece un
feed de proveedor tipo Home Depot dropship). No se puede corregir el
dato dentro de BinManager desde este repo — la solución es limpiarlo
solo para mostrarlo.

Implementado por `uxui-designer` (agente especializado, a petición
explícita de Jovan): nueva `clean_bm_title(title, brand, model)` en
`app/services/sku_utils.py` — en vez de adivinar la marca/modelo desde el
propio texto (riesgo de falso positivo), usa los campos `brand`/`model`
YA CONOCIDOS del mismo renglón de `bm_sku_master` como fuente de verdad;
solo colapsa el título si empieza exacto con `"{brand} {model}"` y justo
después viene otra copia pegada de `"{brand}{model}"`. Sin match exacto,
el título se devuelve intacto — la mayoría de SKUs (sin este problema) no
se tocan en absoluto.

Aplicado en TODOS los puntos donde este título llega a una vista (no solo
donde se reportó): `get_realtime_stock_alerts`, `get_replacement_sku_suggestions`,
`get_pending_shipment_resolutions` (`token_store.py`); búsqueda de
órdenes, gap scan/"Sin publicar", `/api/planning/unlaunched`,
`/bm/unlaunched`, Top 30 TVs (`main.py`). Nunca se modifica la columna
`bm_sku_master.title` en la base (se conserva el dato crudo) ni los
endpoints `/api/diag/*` (deliberadamente muestran el dato sin procesar).

Verificado con los 2 ejemplos reales de Jovan + un 3er caso real
(Samsung) no incluido en la prueba original — los 3 quedaron limpios;
títulos normales sin el patrón quedan exactamente igual.

---

## 2026-08-18 — FIX DE RAÍZ: mismo bug "'str' object has no attribute 'get'" en 3 lugares (no solo el ya corregido) — orden real 2000018003864808 se quedaba "pending" para siempre

Jovan reportó una orden nueva (`2000018003864808`) atorada en "Verificando en BM" y pidió explícitamente NO parchar una cosa a la vez sino encontrar la solución final, adaptando lo nuevo a lo ya resuelto. Tenía razón: el fix de hoy más temprano (`'str' object has no attribute 'get'`, commit `d3848d3`) solo cubrió el sitio donde apareció esa vez — pero el mismo patrón (cada caller re-parseando `response` a mano, asumiendo que siempre es un dict) estaba duplicado en otros 3 lugares, y uno de ellos es el más usado de todos.

**Encontrados con evidencia real** (traceback completo de Railway, no supuesto):
1. `retry_stock_alert_resolution_bm` (botón "Reintentar" manual) — línea ~17163.
2. `delete_stock_alert_resolution_from_bm` (botón "🗑 Borrar de BM") — línea ~17204.
3. **`_inject_bm_alter_sku_background`** (el reintento automático que corre en CADA "Sustituir", vía `asyncio.create_task`) — el más grave: al no tener ningún try/except envolvente en ese tramo, la excepción se perdía en silencio dentro del task y la resolución se quedaba en `bm_status='pending'` **para siempre**, sin ningún error visible — exactamente el síntoma reportado.

**Solución final (no un 4to parche puntual):**
- Nueva función `_safe_bm_message(response_data, fallback="")` — UN solo lugar que sabe extraer el mensaje de BM sin asumir que `response` es dict.
- `_inject_bm_alter_sku` y `_delete_bm_alter_sku` ahora devuelven `"message"` ya calculado con esa función — los 3 callers se simplificaron a leer `bm_result.get("message")` en vez de reimplementar el parseo cada uno por su cuenta.
- `_inject_bm_alter_sku_background` además se envolvió COMPLETO en try/except: cualquier error futuro (no solo este) ahora termina en `bm_status='failed'` con el error real en `bm_message`, nunca más en `'pending'` zombie sin explicación.

Verificado que ML sigue sano de este lado antes y después del fix (cuenta ML es numérica, nunca dispara el bug; 3 sustituciones reales recientes con `bm_status=success` confirmado contra producción).

---

## 2026-08-18 — FEAT: "Alertas de Stock" para Amazon — Fase 3 (pestaña UI)

Cierra el porteo a Amazon (ver entrada de Fase 0/1 más abajo). Nueva
sub-pestaña "⚠️ Alertas de Stock" dentro de Ventas → Amazon (junto a
Resumen/Por SKU/Finanzas), mismo patrón visual que ML: sub-vistas En vivo
/ Pendientes de Envío / Historial + modal "Sustituir".

- `user_store.py`: agregado `alertas_stock` a `PERMISSION_TREE["amz"]["ventas"]["subtabs"]`.
- `amazon_dashboard.html`: markup de la sub-vista + modal `#amz-substitution-modal`
  (IDs con prefijo `amz-` para no chocar con el modal de ML, que vive en
  otro template).
- `amazon_dashboard.js`: `setAmzAlertasSubView`, `loadAmzAlertasLive/Pending/History`,
  `openAmzSubstitutionModal`/`submitAmzSubstitution`. Reusa los mismos
  endpoints de ML (ya platform-agnostic) con `&platform=amazon` nuevo en
  los 3 (`/api/stock/realtime-alerts`, `/pending-shipment`, `/resolutions`)
  — la vista de ML sigue sin filtrar (default `platform=""` = todas, sin
  cambiar su comportamiento actual).
- **Mejora pedida por Jovan**: "En vivo" agrupa por orden en vez de repetir
  la tarjeta completa por cada SKU (una orden Amazon con 5 productos sin
  stock mostraba 5 tarjetas idénticas salvo el SKU) — ahora 1 tarjeta con
  la orden arriba y la lista de SKUs debajo, cada uno con su botón
  "Sustituir" independiente. El modelo de datos no cambió (sigue 1 fila
  por order_id+sku, igual que ML) — es solo agrupación visual.
- El aviso "Fase 2 bloqueada" queda visible arriba de la pestaña y dentro
  del modal — "Registrar" guarda la nota pero no inyecta a BM hasta que
  BinManager/MI2 resuelva el problema de esquema (`ProfileID`/`SiteAccountID`
  bigint, ver entrada anterior).

Probado localmente: página `/amazon?tab=ventas` renderiza sin errores de
Jinja, los 3 endpoints con `&platform=amazon` responden 200, JS validado
con `node --check`.

---

## 2026-08-18 — FEAT: "Alertas de Stock" portado a Amazon — Fase 1 (detección) en producción, Fase 2 (sustitución BM) BLOQUEADA por BinManager

Jovan pidió portar a Amazon la misma operación completa de "sin stock"
que ya existe en ML. Se investigó ANTES de escribir código (con los
especialistas, no manualmente — pedido explícito de Jovan): primero
`marketplace-strategist` leyó documentación real de Amazon SP-API
(Notifications API/SNS, ciclo de vida de `OrderStatus`, rate limits),
después `binmanager-specialist` verificó contra BM en vivo si las cuentas
Amazon ya tenían un ProfileID/SiteAccountID configurado.

**Fase 0 (verificación BM) — bloqueo real, no de código.** `ProfileID`/
`SiteAccountID` en BM son columnas SQL `bigint` — el Seller ID
alfanumérico de Amazon (ej. `A20NFIUQNEYZ1E`) rompe el cast con un error
SQL real (`Error converting data type nvarchar to bigint`), no un simple
"no encontrado" (se comprobó la diferencia con un ID numérico inventado,
que sí da "Not Found" limpio). **La Fase 2 (sustitución real + inyección
a BM) queda bloqueada hasta que BinManager/MI2 defina cómo identificar
una cuenta Amazon en ese esquema** — no es algo resoluble con más código
de este lado.

**Fase 1 (detección, SÍ implementada) — polling, no webhook.** Amazon no
tiene Notifications API/SNS conectado (confirmado con documentación
real: existe `ORDER_CHANGE` vía SQS, pero requiere infra AWS nueva).
Recomendación del especialista con números reales: polling cada 5 min
alcanza de sobra (rate limit real 0.0167 req/s sostenido = 1 cada 60s;
necesitamos 1 cada 300s, con margen de 5x, y cada cuenta tiene su propio
balde por usar su propia app SP-API). Nuevas funciones en `main.py`:
`_run_amazon_stock_reconcile_pass` (compartida por `_amazon_stock_reconcile_loop`
cada 5 min/3 días y `_amazon_stock_reconcile_wide_loop` cada 2h/30 días) —
mismo patrón que el reconcile de ML, reusa `_bm_bulk_available_qty` (cero
llamadas nuevas a BM) y las tablas ya platform-agnostic
(`realtime_stock_alerts`/`stock_alert_resolutions`).

**2 bugs reales encontrados probando contra las 3 cuentas Amazon reales
antes de desplegar (no se guessed, se corrió el loop de verdad):**
1. `AmazonClient` no tiene `.close()` (a diferencia de `MeliClient`) —
   cada request abre su propio `httpx.AsyncClient` con context manager,
   no hay conexión persistente que cerrar. Se quitó la llamada que
   tronaba en el `finally`.
2. **ExclusiveBulbs (A22XNR713HGDVG) excluida del alcance por ahora** —
   confirmado por Jovan que opera "más por FBA que Merchant"; se agregó
   el filtro `FulfillmentChannel == "AFN"` (equivalente exacto al
   `logistic_type=fulfillment` que ML ya excluye) para toda cuenta, PERO
   la única orden Merchant real de esa cuenta detectada en la prueba
   (`SellerSKU` con convención `ARBVYNL...`) tampoco tiene match en BM —
   catálogo aparentemente no gestionado ahí. Se agregó
   `_AMAZON_STOCK_ALERT_ENABLED_SELLERS = {VECKTOR, AUTOBOT}` (allowlist
   explícita) para no generar falsas alertas "sin stock" en esa cuenta
   hasta aclarar cómo se gestiona su inventario.

**3er bug real, encontrado en producción justo después del primer
deploy:** la primera corrida SÍ detectó una orden real de VECKTOR, pero
**los 5 SKUs de esa orden (ej. "8512899") no existen en absoluto en
`bm_sku_master`** — no eran "sin stock", eran de un catálogo que no es
de BM. Se verificó con datos reales: VECKTOR solo tiene ~48% de su
catálogo Amazon en convención SN/SH (el resto son códigos de fabricante/
dropship), AUTOBOT ~94%. "SKU no encontrado en el bulk" puede significar
2 cosas distintas y el código solo distinguía una. Fix: antes de evaluar
stock, se exige que el SKU exista en `bm_sku_master` (catálogo maestro
persistido) — mismo criterio de "no es catálogo BM" ya usado para excluir
ExclusiveBulbs, aplicado ahora a nivel SKU dentro de cualquier cuenta, no
solo a nivel cuenta completa. Con este fix, la orden falsa de VECKTOR se
habría saltado por completo (verificado).

**Nota de corrección:** el "hallazgo" original de que VECKTOR tenía el
refresh token OAuth roto era un falso positivo del entorno LOCAL de
prueba (credenciales locales desactualizadas) — en producción el token
de VECKTOR funciona bien, confirmado con la detección real de arriba.

También: `_account_display_name(platform, account_id)` nuevo — antes
`/api/stock/realtime-alerts` y `/api/stock/alerts/pending-shipment`
resolvían nombre de cuenta SOLO contra el diccionario de nicknames de ML,
mostrando el seller_id crudo para cualquier fila Amazon.

Pendiente (Fase 3, no bloqueada): pestaña "Alertas de Stock" en
`amazon_orders.html` — hoy los datos ya se generan pero no hay UI para
verlos del lado Amazon (sí aparecen en "En vivo" de la vista de ML,
mezclados con las alertas ML, gracias a que la tabla es platform-agnostic).

---

## 2026-08-18 — FIX: sustitución fallaba SIEMPRE con "'str' object has no attribute 'get'" (orden real 2000017985070200, reportado por Jovan)

Jovan preguntó por qué una sustitución seguía cayendo directo a
"Historial" con "✗ Falló en BM" en cada reintento, sin nunca llegar a
"Pendientes de Envío". No era un problema de orden de flujo — el
`bm_message` guardado decía literalmente `'str' object has no attribute
'get'`, un error real de código que hacía fallar la inyección SIEMPRE
para ese SKU, sin importar cuántas veces se reintentara.

**Causa:** `GetAlterSKUMappingByWebSKU` (BM) a veces devuelve `JSONData`
como una lista con entradas que NO son objetos (strings u otros
valores), en vez de los grupos `{ProductSKU, AlterSKUs}` esperados —
también el endpoint de creación (`AddAlterSKUMappingByWebSKU`) a veces
responde con un string JSON plano en vez de `{MessageReturn: ...}`.
Cuatro funciones (`_resolve_bm_condition_sku`, `_bm_alter_sku_covers_order`,
`_find_bm_alter_sku_listing_id`, `_inject_bm_alter_sku`) asumían sin
verificar que cada entrada/respuesta siempre era un dict y llamaban
`.get()` directo — con una entrada no-dict, eso revienta con
`AttributeError` y NUNCA llega a "success" ni a "Pendientes de Envío".

**Fix:** las 4 funciones ahora filtran/verifican `isinstance(..., dict)`
antes de llamar `.get()` (descartan entradas raras en vez de asumir que
siempre vienen bien formadas), y `_inject_bm_alter_sku` maneja el caso de
`data` siendo un string plano. No cambia ninguna regla de negocio — es
manejo defensivo de una variabilidad real de formato de BM, no una
suposición nueva sobre su comportamiento.

Archivo: `app/main.py`. Pendiente: Jovan reintentará esta orden después
del deploy para confirmar que ahora sí completa.

---

## 2026-08-18 — El bulk GR seguía sin refrescar tras subir el timeout: el cuello de botella real estaba 1 nivel más abajo (60s por página, no el wrapper de 150s)

Después del fix "timeout GR 90s→150s" (2 entradas más abajo), el navbar
seguía en rojo (`bulk_gr_age_s` sin bajar). Se monitoreó producción en
vivo tras el deploy y apareció evidencia nueva y distinta:

- Primero: `[BM-CACHE] GR bulk fetch devolvió vacío` (HTTP 200 sin filas)
  — se agregó log de la respuesta cruda (`get_bulk_stock`,
  `binmanager_client.py`) para el próximo caso, sin adivinar la causa.
- Con el deploy de ese log, apareció el dato real: `BinManager
  get_bulk_stock pág 1 error:` (mensaje vacío = `asyncio.TimeoutError`) —
  el timeout que de verdad importa es un **60s hardcodeado POR PÁGINA
  dentro de `get_bulk_stock()`**, completamente independiente del wrapper
  externo de 150s que se había subido antes (ese wrapper solo pone un
  techo al total de páginas/reintentos, no cambia el timeout individual).

**Fix:** timeout interno por página subido de 60s a 100s
(`binmanager_client.py`), wrapper externo subido de 150s a 250s para que
alcancen 2 intentos de 100s + margen (`main.py`). También se corrigió el
log de esa excepción para mostrar `type(e).__name__` (antes se veía
"error: " vacío, mismo problema que ya se había corregido en main.py).

**Nota honesta:** BM tardando >60s en responder la página 1 de un reporte
de inventario, con el ping liviano de salud reportando "sano", apunta a
que el endpoint específico (`Get_GlobalStock_InventoryBySKU`) está lento
del lado de BM ahora mismo — subir el timeout le da más margen pero no
arregla una lentitud real de su servidor. Si esto persiste después de
este fix, vale la pena reportarlo al equipo de BinManager/MI2 en vez de
seguir subiendo timeouts a ciegas.

---

## 2026-08-18 — FIX: modal "Sustituir" ya no perdía la condición real del SKU (bug real, reportado por Jovan tras el fix del colgado)

El fix del colgado de 300s/502 (entrada de abajo) cambió `/api/stock/live-check`
a leer solo el bulk en memoria — pero esto rompió la resolución de
condición: Jovan probó con `SNTV003147` y el campo se quedaba tal cual
(sin `-GRB`) en vez de completarse solo como antes.

**Causa:** BM a veces guarda el SKU BASE, sin ningún sufijo de condición,
como su propia fila en el bulk (con stock real) — el lookup lo tomaba
como "coincidencia exacta" y devolvía el mismo SKU que el usuario ya
había escrito, sin aportar la condición real.

**Fix:** se descarta ese "match exacto sin condición" (no aporta nada) y
se vuelve a resolver la condición EN VIVO vía `_resolve_bm_condition_sku`
(`GetAlterSKUMappingByWebSKU`) — pero con techo duro de 10s
(`asyncio.wait_for`), y es una consulta puntual por SKU (ligera), no el
reporte bulk completo (pesado) que causó el colgado original. La
cantidad disponible se queda saliendo del bulk (instantánea). Si la
resolución en vivo no responde en 10s, no bloquea — se sigue con lo que
ya haya salido del bulk.

---

## 2026-08-18 — FEAT: histórico de fallos del bulk BM + navbar deja de mentir frescura + timeout GR subido a 150s

Cierra el pendiente real de las 2 entradas anteriores: Jovan pidió "llevar
un control e histórico para que no pase eso". Se investigó con evidencia
real de los logs de Railway (no supuesta) por qué el bulk llevaba 25h sin
refrescar pese a que el loop de 10 min SÍ estaba corriendo:

**Causa raíz encontrada:** el fetch GR (`bm_cli.get_bulk_stock`) lleva
fallando por **timeout de 90s de forma silenciosa y repetida** (log real:
`[BM-CACHE] GR bulk fetch error:  — usando stale`, mensaje vacío = firma
de `asyncio.TimeoutError`, cuyo `str()` es ''). Cuando GR falla fresco, el
circuit breaker existente (`_gr_fresh_attempt_failed`) salta LOC47/LOC68/
LOC-TJ/ALL ese mismo ciclo — así que UN solo endpoint lento tumba los 5
fetches, ciclo tras ciclo, indefinidamente. Y nadie lo veía porque el
badge verde de "Inventario actualizado" en el navbar mide la edad de
`stock_issues_cache` (un caché derivado que se puede "completar" un ciclo
usando bulk viejo) — no la edad real del bulk.

**3 cambios (todos aprobados por Jovan, "adelante con todos"):**

1. **Tabla nueva `bm_bulk_fetch_log`** (`token_store.py`) — registra CADA
   intento fresco real (éxito/vacío/error) de los 5 fetches (gr/all/loc47/
   loc68/loctj), con duración y mensaje de error real (se agregó
   `type(e).__name__` porque `TimeoutError` sin eso se veía vacío en el
   log). A diferencia de `bm_sync_log` (solo éxitos), esto deja rastro de
   una racha de fallos como la de hoy.
2. **Navbar ya no usa el proxy que mentía frescura** — `/api/stock/prewarm-status`
   ahora expone `bulk_age_s` (edad real de `_bm_bulk_gr_cache`/`_bm_bulk_all_cache`,
   el máximo de los dos) y `base.html` (`_checkCacheAge`) lo usa en vez de
   `last_updated_s` para el punto verde/amarillo/rojo y la alerta global.
3. **Timeout de GR subido de 90s a 150s** (igual que ALL/LOC-TJ, que ya se
   subieron el 2026-08-10 por el mismo motivo: la cola del semáforo global
   se acumula fetch tras fetch). No garantiza resolver la causa si BM está
   lento de verdad de su lado, pero le da más margen real.

Archivos: `app/services/token_store.py` (tabla + helpers), `app/main.py`
(instrumentación de los 5 bloques de fetch, timeout GR, campo `bulk_age_s`),
`app/templates/base.html` (`_checkCacheAge`). Probado localmente: tabla e
inserts funcionan, endpoint devuelve `bulk_age_s` correctamente.

---

## 2026-08-18 — Corrección propia: SÍ existía un loop de refresco fijo del bulk BM (ya a 10 min, no "ninguno") — bajado a 5 min

Después del fix de `/api/stock/live-check` (ver entrada siguiente), se
diagnosticó por qué el bulk (`_bm_bulk_gr_cache`) tenía ~25h de
antigüedad en producción pese a BM estar sano. Le dije a Jovan que "no
existe ningún proceso con temporizador fijo" — **esto era INCORRECTO**:
`_startup_prewarm()` (arranca en el startup del proceso, `if not
_BM_DISABLED`) YA corría cada 10 min (bajado de 15 min el 2026-08-14).
Confirmado que `DISABLE_BM_MONITOR=false` en Railway (el loop sí corre).

La causa real de los 25h: el diagnóstico se corrió ~1 minuto después de
un redeploy — cada redeploy reinicia el proceso, y este loop tarda 90s +
1 ciclo completo (multi-cuenta, puede tardar varios minutos) antes de su
primer refresh; en una sesión con varios deploys seguidos (como la de
hoy) el ciclo nunca llega a completar antes del siguiente restart, así
que sigue sirviendo el snapshot persistido en DB de la última vez que un
ciclo completo SÍ terminó (posiblemente un día antes). Bajar el
intervalo no elimina ese reinicio por deploy (inevitable), pero acorta la
ventana de "sin refrescar" el resto del tiempo entre deploys.

Jovan pidió bajarlo a "10 o 5 minutos" — se eligió **5 min** (bajado de
10). Cambio de una sola línea en `_startup_prewarm()` (`app/main.py`,
`_sleep = 120 if _auto_fail_streak > 0 else 300`).

---

## 2026-08-18 — FIX CRÍTICO: modal "Sustituir" se colgaba hasta 5 min (502 de Railway) — /api/stock/live-check ya no llama a BM en vivo

Jovan reportó que "Verificando disponibilidad en vivo..." se quedaba
pegado en el modal. Se probó directo contra producción (no se asumió):
la llamada real tardó **300s exactos y Railway devolvió 502 "upstream
error"** — colgada de verdad, no solo lenta.

**Causa:** el endpoint hacía 2 llamadas EN VIVO secuenciales a BM
(`_resolve_bm_condition_sku` + `get_stock_with_reserve`), cada una con su
propio reintento (2 intentos) y timeout de hasta 20s, TODAS pasando por
`_BM_GLOBAL_SEM` (semáforo de 1 sola petición BM a la vez para TODA la
app) — si BM andaba lento o cualquier otro ciclo (prewarm, fulfillment
loop) tenía el semáforo ocupado, el peor caso se iba a 60-90s+ solo para
esta llamada, encolado detrás de lo que sea que ya estuviera esperando.

**Fix — mismo principio de [[feedback_preferir_solucion_simple_del_bulk]]
("ausencia en bulk = 0, sin excepciones") aplicado aquí:** se reemplazó
por `_bm_bulk_stock_lookup()`, que lee `_bm_bulk_gr_cache`/`_bm_bulk_all_cache`
(el mismo bulk ya en memoria, refrescado ~cada 15 min por el prewarm) —
cero llamadas a BM, responde en milisegundos (probado: 0.34s local vs
300s+/502 antes). Se pierde la garantía de "reflejado hace 1 segundo" a
cambio de nunca más colgarse; el frontend ahora muestra la antigüedad del
dato ("caché de hace X min") en vez de decir "en vivo"/"ahora mismo". La
verificación que sí necesita ser 100% en vivo (justo antes de escribir en
BM, dentro de `_inject_bm_alter_sku`) no se tocó — sigue siendo real.

Archivos: `app/main.py` (`_bm_bulk_stock_lookup`, `stock_live_check`),
`app/templates/orders.html` (`_subModalLiveCheck`, `submitSubstitution`).

---

## 2026-08-18 — FEAT/FIX UI: título de producto en "Pendientes de Envío" + modal "Sustituir" autocompleta el SKU con condición real

Dos pedidos de Jovan sobre la UI de Alertas de Stock:

1. **"Pendientes de Envío" no se veía bien** — la tarjeta solo mostraba el
   número de orden a secas (sin decir qué producto es), a diferencia de
   "En vivo" que ya muestra el título. Se agregó el mismo JOIN con
   `bm_sku_master` (por `original_sku` y por `substitute_sku`) en
   `get_pending_shipment_resolutions()` (`token_store.py`) y se
   reordenó la tarjeta en `orders.html` para mostrar el título en grande
   arriba y la orden como link secundario debajo (mismo patrón visual
   que "En vivo"), más el título del sustituto junto al SKU.

2. **Modal "Sustituir" solo avisaba la condición real en un texto aparte
   (ej. "(condición real: SNTV003147-GRB)"), tapado visualmente por el
   siguiente campo** — Jovan pidió que el campo mismo quede con el SKU
   completo. `_subModalLiveCheck()` ahora, en cuanto `GetAlterSKUMappingByWebSKU`
   resuelve una condición distinta a lo escrito, actualiza el VALOR del
   input a ese SKU completo (base+condición) — lo que se ve en la
   pantalla es exactamente lo que se va a registrar y mandar a BM, sin
   nota aparte que se pueda no ver.

Verificado localmente (`/api/stock/alerts/pending-shipment` responde 200
JSON válido con el JOIN nuevo; `py_compile` limpio).

---

## 2026-08-18 — FIX DE RAÍZ: usar el seller_sku CRUDO de ML para resolver el ProductSKU real en BM (cierra el bug de la entrada anterior)

Cierra de raíz el bug de la entrada anterior (2 Products distintos bajo
el mismo WebSKU). Jovan preguntó por qué no usábamos directo el SKU
"completo" de la orden en vez de tratar de adivinarlo — se probó la
hipótesis directo contra BM (no se asumió) y resultó ser la solución
correcta: **BM guarda su WebSKU tal cual el seller_sku CRUDO que manda
ML, incluyendo el bundle** (ej. `"SNTV006485 / SNWM000001"`, TV + soporte
de pared). El código normalizaba ese SKU (quitaba el bundle) ANTES de
preguntarle a BM cuál era el producto — eso lo mandaba a un Product
DISTINTO sin relación con la orden real.

Confirmado en vivo con `/api/diag/bm-alter-sku-groups`:
- `WebSKU="SNTV006485"` (normalizado) → resuelve a `SNTV006485-GRB`
  (un listing sin relación, `ListingID 29791`) — el bug.
- `WebSKU="SNTV006485 / SNWM000001"` (crudo, sin tocar) → resuelve a
  `SNTV006485-ICB`, el correcto — confirmado idéntico a la captura real
  de BM (mismas alternativas ICC/GRC/GRA/GRB).

**Fix:** se preserva el seller_sku crudo desde que se detecta la alerta
(`realtime_stock_alerts.sku_raw`) hasta que se registra la sustitución
(`stock_alert_resolutions.original_sku_raw`) — `_inject_bm_alter_sku`/
`_delete_bm_alter_sku` intentan PRIMERO con el crudo, y solo caen al
normalizado si eso falla (compatibilidad con resoluciones viejas que no
tienen el dato). Las sustituciones nuevas de aquí en adelante deberían
resolver el producto correcto automáticamente, sin necesitar que Jovan
mande captura de BM cada vez.

**Pendiente de confirmar con un caso real nuevo** (no se pudo probar
todavía un caso 100% end-to-end con un SKU con bundle real después del
deploy — el próximo caso similar que aparezca en "Alertas de Stock"
confirma si quedó resuelto de verdad).

## 2026-08-18 — FIX CRÍTICO: BM puede tener 2 Products distintos bajo el mismo WebSKU + orden duplicada en En vivo/Pendientes

Jovan reportó (con captura real de BM) que una sustitución aplicada
("Aplicado en BM") no aparecía en el Mapping real de la orden en BM.
Investigación con evidencia real (no supuestos) reveló un caso NUEVO,
distinto a los bugs de ayer: **BM puede tener DOS "Product" registrados
por separado bajo el mismo WebSKU** (`SNTV006485-ICB`, el real para esta
orden según su Channel/Listing, Y `SNTV006485-GRB`, un producto distinto
con su propio Listing) — `GetAlterSKUMappingByWebSKU` (usado por
`_resolve_bm_condition_sku`) solo devuelve UNO de los dos, sin garantía
de ser el correcto para una orden puntual. El sistema adivinó el
equivocado.

**Investigado y descartado con datos reales:** se revisó si el
`seller_sku` crudo de la orden en ML ya trae la condición real — NO, es
solo el bundle (`"SNTV006485 / SNWM000001"`), la condición (ICB/GRB/etc.)
es un dato 100% interno de BM que ML nunca conoce. No hay atajo posible
desde el lado de ML.

**Solución aplicada para el caso puntual:** nuevos endpoints
`bm-alter-sku-create-exact`/`delete-exact` (crean/borran un mapeo con el
ProductSKU EXACTO que el usuario ya confirmó visualmente en BM, sin
ninguna resolución automática) — se limpió el mapeo mal ubicado y se creó
el correcto bajo `SNTV006485-ICB`, confirmado por Jovan directamente en
BM. **Pendiente real, no resuelto todavía**: la resolución automática
general (para sustituciones futuras vía "Sustituir") sigue usando el
mecanismo ambiguo — se necesita capturar con DevTools el endpoint real
que usa "Status Orders" de BM al buscar por orden, para resolver el
Product correcto de forma determinista sin depender de que Jovan mande
captura cada vez.

**Bug relacionado, también real, encontrado al revisar la captura:** una
orden ya sustituida (viviendo en "Pendientes de Envío") seguía
apareciendo en "En vivo" pidiendo "Sustituir" otra vez — confuso, parecía
que no se había hecho nada. `get_realtime_stock_alerts` ahora excluye
cualquier orden con sustitución activa (aplicándose o ya aplicada,
pendiente/completada) — vuelve a aparecer solo si se reabre
explícitamente o si la inyección a BM falló de verdad. Verificado en
producción: la orden ya no aparece en "En vivo", sigue correcta en
"Pendientes de Envío".

## 2026-08-18 — FIX (2x): reintento automático en "Sustituir" + Productos ya no llama BM en vivo

Continuación directa del fix crítico de `bm_sku_master` (entrada de abajo).
Jovan pidió analizar dónde más aplicar el mismo criterio ("preferir el
archivo completo sobre llamadas puntuales"). Encontré dos sitios reales:

**1. Reintento automático en la inyección de BM ("Sustituir"):** una
orden nueva (`2000017991930258`) volvió a quedarse en "Verificando en
BM" — investigar mostró que `_inject_bm_alter_sku_background` intentaba
UNA sola vez; si BM estaba lento en ese momento puntual (confirmado real
y frecuente ese mismo día), se quedaba pegado hasta un clic manual en
"Reintentar". Se agregó reintento automático (hasta 4 intentos, espera
creciente) ANTES de rendirse — seguro porque `_inject_bm_alter_sku` ya
valida anti-duplicado en cada intento. (La orden reportada en el momento
ya se había resuelto sola para cuando se revisó — confirma que la mayoría
de estos fallos son transitorios, no permanentes.)

**2. Página de Productos sin llamadas a BM:** análisis de dónde más se
hacían consultas puntuales a BM reveló que `/partials/items-grid` hacía
**2 llamadas EN VIVO a BM por cada producto visible** (hasta 100 por
carga de página), sin revisar ningún caché — mismo riesgo que el
incidente de `bm_sku_master`. Se reemplazó por una sola consulta a
`bm_sku_master` (ya con MTY/CDMX/TJ/avail/reserve, refrescado cada ~2 min
desde el fix anterior) — cero llamadas a BM en esta vista. Verificado en
producción: carga de página en <1s.

**Nota del análisis:** se investigó también el prewarm principal por
cuenta (sospecha inicial de "600 llamadas individuales por ciclo") — al
rastrear el código completo (no solo la función aislada) se confirmó que
ese mismo problema ya se había corregido ahí en 2026-08-11 con el mismo
criterio — no era un riesgo activo, corrección de mi análisis inicial.

## 2026-08-18 — FIX CRÍTICO: reconciliación de bm_sku_master atorada 4.5+ horas (0/150 en 6 ciclos consecutivos) — solución de Jovan

Jovan siguió preguntando por qué la sugerencia de sustituto mostraba
"SNTV007447 — disp. 1" cuando BM mostraba 0 en varios almacenes. Investigar
a fondo (en vez de repetir el número sin verificar, error que cometí antes
en esta misma conversación) reveló un incidente real de horas, confirmado
con logs de Railway:

**Causa raíz:** `_bm_master_sync_once_inner` (main.py) reconciliaba los
SKUs "ausentes del bulk" (no encontrados en el fetch masivo) con hasta 150
llamadas INDIVIDUALES a BM por ciclo. Bajo la carga real del día, el
endpoint puntual de BM (`Get_GlobalStock_InventoryBySKU` para un solo SKU)
falló **0/150 en 6 ciclos consecutivos** (~38 minutos cada uno, cada uno
de los 150 intentos agotando su timeout) — de 01:54 a 06:21. Con ~28,000
SKUs "ausentes del bulk" en el universo total y avance real de CERO, SKUs
como `SNTV007447` quedaron con un valor de **hace 6 días** sin que nada lo
corrigiera ni avisara que estaba desactualizado.

**Solución (propuesta por Jovan, no por mí):** "si no aparece en el bulk,
es 0 — bájalo completo y consulta sobre ese archivo, sin verificar uno por
uno." Es exactamente la misma regla ya usada y verificada para las alertas
en tiempo real (`_bm_bulk_available_qty`, DEVLOG 2026-08-14: "BM omite del
bulk cualquier SKU con stock=0 — por diseño"). Se eliminó por completo el
paso de reconciliación individual — los "misses" se escriben directo con
`available_qty=0, verified=True`, puro trabajo en memoria sin ninguna
llamada nueva a BM. Se eliminó `get_reconciliation_priority_skus()` (ya
sin uso).

**Resultado verificado en producción:** el ciclo completo pasó de ~38 min
(150/29998 SKUs, con 0% de éxito real) a **0.5 segundos cubriendo el 100%
del universo (29998/29998)**. `SNTV007447` corregido a `available_qty=0`
de inmediato; la sugerencia falsa para la orden `2000017984576896`
desapareció (`sugerencias: []`, ya no ofrece un SKU sin stock real).

**Lección de esta sesión:** cuando Jovan preguntó "por qué no piensas más
lógicamente", tenía razón — la solución correcta ya existía en el propio
código (el mismo criterio de `_bm_bulk_available_qty`) y era más simple
que el mecanismo de reconciliación individual que se había construido
antes. La complejidad (llamadas puntuales con reintentos y rotación de
prioridad) fue la causa del problema, no la solución.

## 2026-08-17 — FIX (2x): "Pendientes de Envío" no actualizaba stock + botón "Buscar otro sustituto" se quedaba atorado

Dos bugs reales encontrados por Jovan usando la feature recién lanzada
(entrada de abajo), mismo día:

**1. Las tarjetas seguían en "sin verificar todavía" sin actualizar
nunca.** Causa real: `get_stock_with_reserve()` usaba timeout=7s fijo
(pensado para ciclos que revisan 150 SKUs seguidos, ej. `bm_sku_master`),
insuficiente para estos 2 SKUs puntuales bajo la carga real de pruebas
del día (confirmado en logs: timeout de 7s×2 intentos repetido varias
veces). Fix: `get_stock_with_reserve()`/`_query_bm_stock()` ganan
parámetro `timeout` (default 7s sin cambio para los ciclos existentes);
el live-check del modal y `_substitution_fulfillment_loop` — ambos
consultan 1-5 SKUs nada más — ahora esperan 15-20s antes de rendirse.
Verificado en producción: `SHHP000048`→`SHHP000060-NEW` resultó tener
**115 disponibles** reales (no 0 como parecía); `SNTV007263`→
`SNTV007447-GRB` confirmó **0 real**.

**2. El botón "🔄 Buscar otro sustituto" se quedaba en "Reabriendo..."
para siempre.** Mismo patrón exacto del incidente de "Sustituir" de la
sesión anterior: el endpoint esperaba 2-3 llamadas SEGUIDAS a BM (buscar
+ borrar el mapeo viejo) antes de responder — podía tardar tanto que se
cortaba por el timeout del proxy de Railway, dejando el botón atorado sin
error y sin haber hecho nada (confirmado: la fila seguía intacta en DB).
Fix: se marca "reabierta" de inmediato (responde en <1s) y el borrado del
mapeo viejo en BM corre en background — es solo limpieza, el sustituto ya
está en 0, no hay urgencia real de esperarlo.

Verificado en producción de punta a punta: reopen respondió en 0.25s,
trajo sugerencias frescas, y el borrado en background confirmó
`bm_deleted_at` set correctamente segundos después.

## 2026-08-17 — FEAT: "Pendientes de Envío" — sustituciones ya no quedan cerradas hasta que la orden se envía

Jovan planteó el hueco real detrás de todo el incidente del día: hasta
ahora, en cuanto una sustitución quedaba "Aplicada en BM" pasaba
directamente al Historial como si estuviera resuelta — pero eso solo
confirma que BM SABE del sustituto, no que el almacén ya lo empacó y
envió. Si el stock del sustituto se agota entre la promesa y el envío
real (como pasó hoy mismo con `SNTV007447-GRB`: 1 unidad al sustituir,
0 unas horas después), nadie se entera hasta que es tarde.

**Nuevo paso intermedio:** `fulfillment_status` en `stock_alert_resolutions`
(`''`/`pendiente_envio` → `completado` o `cancelada`). Loop en background
cada 10 min (`_substitution_fulfillment_loop`) revisa cada sustitución
aplicada en BM que siga pendiente:
- Si la orden ML ya avanzó (se imprimió/envió — misma lógica de
  `_shipment_should_alert` que ya usa el feed de alertas en vivo, sin
  reinventar el criterio) → `completado`.
- Si la orden se canceló → borra el mapeo de BM automáticamente
  (autorizado por Jovan explícitamente) y marca `cancelada`.
- Si sigue pendiente → verifica el stock del sustituto EN VIVO (mismo
  mecanismo del live-check del modal) y lo deja marcado — bandera roja
  si ya es 0, visible en la nueva vista y en el Historial.

**Nueva vista "🚚 Pendientes de Envío"** en Alertas de Stock (junto a "En
vivo" y "Historial") — solo muestra sustituciones en este estado
intermedio, con el último stock verificado. El Historial también gana un
badge de estado de envío para que quede el registro completo de todo
(pedido explícito de Jovan: "llevando un historial de todo").

Decisiones tomadas vía AskUserQuestion: bandera visual solamente (sin
notificación activa nueva) y borrado automático del mapeo de BM cuando
una orden se cancela.

Verificado en producción real (`/api/diag/trigger-substitution-fulfillment`,
sin esperar el ciclo de 10 min) contra las 3 sustituciones reales del
día: una ya se había enviado y pasó a `completado` sola; la de hoy quedó
correctamente marcada `pendiente_envio` con stock real `0` (el caso
exacto que motivó el pedido); la tercera sigue pendiente de verificar
(timeout real de BM para ese SKU puntual, manejado con gracia).

## 2026-08-17 — OPERACIÓN: cierre del incidente de sustitución BM — auditoría completa + reasignación autorizada

Continuación directa de la entrada de abajo. Audité el historial completo
de `stock_alert_resolutions` (7 filas totales) — la feature de inyección a
BM solo se había usado 3 veces desde que se construyó (todas hoy mismo),
las demás filas son de antes de que existiera esta feature (nunca
intentaron aplicar nada en BM). Sin más falsos positivos escondidos.

**Caso pendiente resuelto** (`SHHP000048-NEW`→`SHHP000060-NEW`, orden de
Vanessa `2000017981754294`): BM no permite "actualizar" el scope de un
mapeo existente — confirmado que la única forma es borrar (Actions=3) +
crear (Actions=1). Antes de tocar nada se verificó que la orden vieja que
tenía el mapeo (`2000017944072810`, 14-ago) ya no aparece en el feed de
alertas activas (señal de que ya se resolvió) — Jovan autorizó
explícitamente la reasignación. Nuevo endpoint puntual
`POST /api/diag/bm-alter-sku-reassign` (no automatizado a propósito,
requiere los 5 parámetros a mano) ejecutó el borrado+creación. Verificado
contra BM real: `SiteOrderID` ahora es la orden nueva.

**Dato nuevo para `project_bm_alter_sku_mapping.md`:** la respuesta de BM
trae también un campo booleano `WasSuccess` (además de `MessageReturn`) —
en los éxitos reales confirmados hoy siempre `WasSuccess: true` +
`MessageReturn: "Success"` juntos. Podría ser una verificación aún más
robusta que solo el texto de `MessageReturn` — evaluar en el futuro si se
repiten casos ambiguos.

Las 3 sustituciones reales de hoy quedan confirmadas y aplicadas en BM:
`2000017984576896`, `2000017956308828`, `2000017981754294`.

## 2026-08-17 — BUG CRÍTICO: la sustitución "Aplicado en BM" NUNCA se aplicó de verdad (3 bugs encadenados, todos corregidos y verificados contra BM real)

Jovan verificó a mano en el Fulfillment Dashboard de BM (Map de
`SNTV007263`) que el sustituto `SNTV007447-GRB` — que la app mostraba en
verde "✓ Aplicado en BM" tras el botón "Reintentar" de la entrada
anterior — **no existía ahí**. Investigación con el nuevo endpoint
`GET /api/diag/bm-alter-sku-groups` (solo lectura, temporal, admin-gated
por `DIAG_TOKEN`) reveló que **ninguna sustitución hecha por esta feature
se había aplicado realmente en BM desde que se construyó**, incluidas 2
que Vanessa ya daba por buenas.

**Bug 1 — ProductSKU sin condición:** `_inject_bm_alter_sku` mandaba el
SKU BASE (`SNTV007263`) como `ProductSKU` a `AddAlterSKUMappingByWebSKU`,
pero BM indexa el mapeo por el ProductSKU CON condición real
(`SNTV007263-GRB` — confirmado el único producto registrado en ese
WebSKU). BM respondía 200/"Success" sin crear nada real. Fix:
`_resolve_bm_condition_sku()` resuelve el ProductSKU real vía
`GetAlterSKUMappingByWebSKU` antes de crear/buscar — si hay 0 o más de 1
producto (ambiguo), rechaza en vez de adivinar.

**Bug 2 — anti-duplicado ciego al scope:** al corregir el bug 1, la
sustitución de Vanessa (`SHHP000048`→`SHHP000060-NEW`, orden
`2000017981754294`) seguía sin aplicarse: ya existía un mapeo MANUAL del
mismo par de SKUs pero para OTRA orden (`2000017944072810`, creado por
Vanessa el 15-ago). El chequeo "¿ya existe?" ignoraba a qué orden
pertenecía el mapeo encontrado. Fix: `_bm_alter_sku_covers_order()` solo
cuenta como "ya existe para esta orden" si `Scope=GLOBAL` o el
`SiteOrderID` coincide exacto — cualquier otro caso debe crear su propia
entrada.

**Bug 3 — BM puede rechazar con HTTP 200:** al reintentar esa misma
sustitución con el fix 2 aplicado, BM respondió 200 con
`MessageReturn: "Insert Alternative SKU: SHHP000060-NEW exists for
SHHP000048-NEW!"` — un RECHAZO real (BM solo permite UN mapeo por par
ProductSKU+AlterSKU, sin importar el scope; el mapeo de Vanessa del 15-ago
ya "tenía tomado" ese par). El código viejo marcaba éxito con cualquier
200. Fix: `ok` ahora exige además `MessageReturn == "Success"` (el único
texto visto en los 2 casos de éxito real confirmados hoy).

**Resultado final, verificado contra BM real con el diag endpoint (no solo
confiado en el mensaje de la API):**
- Orden `2000017984576896` (`SNTV007263`→`SNTV007447-GRB`, hoy) — **RESUELTO
  DE VERDAD**, confirmado con `SiteOrderID` correcto en el Map de BM.
- Orden `2000017956308828` (`SNTV007446`→`SNTV004388`, Vanessa) — **RESUELTO
  DE VERDAD** al reintentar, confirmado igual.
- Orden `2000017981754294` (`SHHP000048`→`SHHP000060-NEW`, Vanessa) —
  **NO SE PUDO auto-resolver** — BM no permite 2 mapeos para el mismo par
  de SKUs. Queda marcado `bm_status=failed` con el mensaje real (antes
  decía "Aplicado en BM" en falso). Pendiente de decisión de Jovan/Vanessa:
  reasignar el mapeo existente del 15-ago a esta orden nueva (si esa orden
  vieja ya se resolvió) o elegir un SKU sustituto distinto para esta orden.

**Lección de esta sesión:** verificar contra el sistema real (no solo
confiar en que la API devuelva 200 o un mensaje de texto) fue lo que
destapó los 3 bugs — cada uno solo salió a la luz al probar el siguiente
caso real, nunca se hubiera encontrado con solo revisar el código.

## 2026-08-17 — FEAT: botón "Reintentar" para sustituciones atoradas en BinManager (incidente real)

Horas después del fix anterior (inyección async), Jovan reportó en vivo un
caso real: la orden `2000017984576896` (sustituto `SNTV007447-GRB`,
autorizado por Jovan) se quedó **15+ minutos** en "⏳ Verificando en BM".

**Diagnóstico con logs reales de Railway (no supuesto):** en la ventana
exacta de la sustitución, el servidor estaba procesando una ráfaga de
~3,000 llamadas a `/messages/packs/...` en menos de 2 minutos — el
diagnóstico `ml-messages-audit` corriendo contra BLOWTECHNOLOGIES (revisa
TODAS las órdenes de la cuenta, no solo las últimas 50). En los 9+ minutos
posteriores a crear la sustitución, **cero intentos reales de llamar a
BinManager** aparecen en los logs — el background task (`create_task` del
fix anterior) no falló, simplemente **nunca llegó a tener turno** en el
event loop, saturado por esa ráfaga. Confirmado además en el propio panel
de BM (Fulfillment Dashboard → Map de `SNTV007263`): el mapeo a
`SNTV007447-GRB` nunca se había creado.

**Fix:** nuevo endpoint `POST /api/stock/alerts/resolutions/{id}/retry-bm`
que llama a BM de forma síncrona (aceptable aquí porque es un clic
explícito del usuario, no el flujo automático que debe responder rápido)
para resoluciones en `pending`/`failed`. Botón "🔁 Reintentar" en el
historial de Alertas de Stock junto a "🗑 Borrar de BM".

Verificado en producción de punta a punta contra el caso real: se disparó
el reintento para la resolución atorada y quedó `bm_status: success` de
inmediato — confirmado también que el mapeo ya aparece en el Map de BM.

**Nota para más adelante (no resuelta hoy):** la causa raíz de fondo —que
una tarea en background pueda quedarse sin turno indefinidamente cuando
corre un diagnóstico pesado como `ml-messages-audit`— sigue viva. El botón
de reintento es la mitigación inmediata; si este patrón se repite seguido,
vale la pena revisar si `ml-messages-audit` necesita throttling propio
(sleep entre lotes) para no acaparar el event loop.

## 2026-08-17 — FIX: inyección a BinManager ya no bloquea "Sustituir" (timeout de Railway)

Jovan probó el flujo de sustitución en producción (orden `2000017956308828`,
sustituto `SNTV004388-GRB`) y el botón se quedó atorado ~2 min y terminó en
error de conexión — **sin quedar nada guardado** en el historial (ni éxito
ni error). Vanessa confirmó en BM que ese SKU alternativo nunca se llegó a
crear, así que era seguro corregir sin riesgo de duplicado.

**Causa raíz:** `resolve_stock_alert_substitution` esperaba la respuesta de
BM (`_inject_bm_alter_sku`, síncrono) *antes* de guardar el registro. La
llamada a BM comparte el semáforo global (`_BM_GLOBAL_SEM`, un solo request
a la vez en todo el proceso) con TODO el demás tráfico a BM — si había algo
más en cola, el total podía superar el timeout del proxy de Railway
(~100s) y la conexión se cortaba sin dejar rastro.

**Fix (`app/main.py`, `app/services/token_store.py`, `app/templates/orders.html`):**
1. El registro se guarda PRIMERO (`bm_status='pending'` si aplica) y el
   endpoint responde de inmediato — ya no espera a BM.
2. La inyección real a BM corre como tarea en background
   (`asyncio.create_task`) que actualiza el registro cuando termina
   (`success`/`failed`), sin bloquear al usuario.
3. `_inject_bm_alter_sku` ahora verifica primero si el mapeo ya existe
   (`_find_bm_alter_sku_listing_id`) antes de crear uno — evita duplicados
   si un intento previo sí llegó a aplicarse en BM pero la conexión se
   cortó antes de confirmarlo.
4. `orders.html`: nuevo badge "⏳ Verificando en BM" + refresco automático
   del historial (6s/16s/30s) para que el badge final se vea sin recargar.

**Bug encontrado durante la prueba (y corregido antes de dar el fix por
bueno):** el primer intento de retry en `update_stock_alert_resolution_bm_status`
(4 intentos, timeout=15s) no aguantó una ráfaga real de contención de
SQLite local (varios syncs completos de cuenta corriendo a la vez al
arrancar) — el registro se quedó pegado en `pending` para siempre, sin
error visible, el mismo síntoma que este fix buscaba eliminar. Como este
write corre en background sin que nadie lo espere, se subió a 6 intentos
con timeout=45s cada uno — verificado que con eso sí resuelve
correctamente incluso bajo esa contención extrema.

Verificado en local con SKUs reales (`SNTV007414-GRB` → `SNTV007730-GRB`):
respuesta del endpoint en 4-10s incluso con el servidor saturado (muy por
debajo del límite de Railway), estado pasa de `pending` a `success`
solo, y el chequeo anti-duplicado detectó correctamente un mapeo ya
existente de una prueba anterior y no creó uno nuevo.

## 2026-08-17 — FEAT: "Sustituir" en Alertas de Stock inyecta y borra directo en BinManager

Jovan pidió: al dar clic en "Sustituir" en Alertas de Stock, que el SKU de
reemplazo se mande directo a BinManager en vez de repetir el trabajo a mano
(Fulfillment Dashboard → buscar orden → Map → +Alternative → escribir SKU
→ marcar "Only Order" → Save). Después pidió también poder borrar ese
mapeo desde nuestro propio historial si ya no hace falta.

**Investigación (sin adivinar ningún endpoint):** intenté primero con
`binmanager-specialist` — bloqueado por 3 vías distintas el mismo día
(acceso HTTP directo bloqueado, navegador bloqueado por el clasificador de
permisos, conector MCP de BinManager con token expirado). La vía que sí
funcionó: Jovan capturó el tráfico real con DevTools → Network mientras
hacía el flujo una vez a mano, dándonos el contrato exacto en vez de una
hipótesis.

**Contrato confirmado (mismo endpoint para las 3 operaciones, un campo
`Actions` decide cuál):**
```
POST /FullFillMent/FullFillMent/AddAlterSKUMappingByWebSKU
  Actions=1 → crear   (ListingId: null)
  Actions=3 → borrar  (ListingId: <ID de esa alternativa específica>)

POST /FullFillMent/FullFillMent/GetAlterSKUMappingByWebSKU
  → devuelve, por cada AlterSKU, su propio ListingID interno (distinto
    del ListingID del producto principal) — necesario antes de poder
    borrar, porque crear no lo regresa en la respuesta.
```
`OrderScope` siempre lleva el `order_id` real (equivalente a "Only Order"
marcado) porque este flujo siempre resuelve una orden puntual, nunca un
mapeo general de SKU.

**Crear (`app/main.py`):** `_inject_bm_alter_sku()` vía `bm_post()` (mismo
semáforo global obligatorio para BM, sin cambios de arquitectura) +
`sku_utils.base_sku()` (ya existía) para derivar `WebSKU`.
`resolve_stock_alert_substitution` ahora llama a BM además de guardar el
registro interno de siempre (solo `platform=ml` — Amazon no tiene este
feed de alertas en tiempo real todavía).

**Historial con estado real:** `token_store.py` — 4 columnas nuevas
(`bm_status`, `bm_message`, `bm_deleted_at`, `bm_deleted_by`). La pestaña
"Historial" de Alertas de Stock ahora muestra un badge por fila (✓
Aplicado en BM / ✗ Falló en BM / 🗑 Borrado de BM) en vez de solo "se
registró aquí".

**Borrar:** `_find_bm_alter_sku_listing_id()` (decodifica `JSONData`, que
viene como string con JSON adentro, dos veces) + `_delete_bm_alter_sku()`
(`Actions=3`). Nuevo endpoint `POST /api/stock/alerts/resolutions/{id}/delete-from-bm`
+ botón "🗑 Borrar de BM" en el historial (solo visible para filas ya
aplicadas y no borradas, con confirmación antes de ejecutar).

Verificado con ciclo completo real, dos veces: una vez con las funciones
sueltas, otra vez a través de los endpoints reales de la app (crear →
"Aplicado en BM" → borrar con un clic → BM confirma → "Borrado de BM").
Ya en uso real en producción (un registro con `bm_status=success` de un
uso genuino, no de pruebas).

---

## 2026-08-16 — FIX: video comercial de 15s a ~26-28s (guion ya no se corta) + FEAT: título real en preguntas de Amazon sin título

Dos pedidos de Jovan el mismo día, investigados y resueltos por separado.

### Video: duración real vs. requisitos oficiales de ML

El video del fix anterior mejoró en calidad pero dura solo 15 segundos,
y el guion narrado se cortaba antes de terminar ("no cuadra con el
video"). Investigado con `ecommerce-creative-director` contra la
documentación oficial de ML ("Working with Clips"): el Clip de una
publicación acepta **10 a 61 segundos** (no 60 exacto), MP4/MOV/MPEG/AVI,
máx 280MB, resolución mínima 360×640px vertical.

- `_N_CLIPS` pasa de 3 a 5 → ~26-28s de video real, dentro del rango
  oficial con margen.
- El guion se recalibra de 100-120 palabras (pensado para un video de
  30-40s que nunca se generaba) a 55-65 palabras exactas, calibrado a la
  duración real del video de 5 clips.
- **Causa raíz real del guion cortado** (independiente de la duración):
  el cálculo de "cuánto dura el audio" en `_xfade_and_combine` usaba una
  estimación por bytes/128kbps asumiendo mp3 de bitrate constante — pero
  la cadena de fallback de TTS (ElevenLabs → edge-tts → gTTS → Google TTS
  HTTP → Bark) puede devolver audio a bitrates muy distintos. Cuando el
  fallback real no era ElevenLabs, la fórmula subestimaba la duración
  real, y `-shortest` cortaba la narración a la fuerza. Reemplazado por
  la duración REAL medida con ffprobe (mismo `_probe_dur` ya usado para
  los clips de video) — nunca más una estimación para esto.

**Nota sobre el error "UNAUTHORIZED" al subir clips a ML** (visto en la
misma captura de Jovan): reproducido con un video 100% válido — es
Mercado Libre rechazando la subida por su propio sistema de políticas
(`PA_UNAUTHORIZED_RESULT_FROM_POLICIES`), no un bug de este código.
Pendiente: revisar en el panel de vendedores si "Clips" está habilitado
para la cuenta, o contactar soporte de ML con ese código exacto.

### Amazon: título real en preguntas sin título

Jovan reportó (con 2 capturas reales) que las preguntas de compradores
en Amazon solo mostraban el ASIN, sin nombre del producto. Causa raíz:
`product_title` se parsea del texto del correo de notificación de Amazon
con una regex que espera el formato "N / Título | ... [ASIN: XXX]" —
formato que solo aparece cuando la pregunta está ligada a una línea de
orden. Preguntas de producto sueltas (como las de las capturas) solo
traen "ASIN: XXX" sin esa línea, dejando el título vacío.

En vez de parchar un parser de texto de un email que Amazon no
documenta, se resuelve el título real vía Catalog Items API (SP-API) —
mismo método (`get_catalog_item`) ya usado en `amazon_products.py`. Se
persiste de una vez (`backfill_buyer_message_product_title`) para que la
próxima carga de la bandeja no vuelva a pedirlo.

**Bug de concurrencia encontrado al probar esto en vivo** (no
hipotético): pedir varios ASIN en paralelo sobre el mismo cliente Amazon
dispara una renovación de token por cada llamada simultánea cuando el
token aún no está en caché — 5 llamadas concurrentes de prueba fallaron
las 5 con `400 invalid_grant`. Cambiado a resolución secuencial.

Verificado en producción con los ASIN exactos de las capturas de Jovan:
`B0H12X52GB` → "LUBL Televisor Philips de 32 Pulgadas..." y
`B0FDGZNLGG` → "Hisense Smart TV de 43 Pulgadas...". 22 de 22 hilos con
ASIN de la cuenta VECKTOR ahora muestran título real.

---

## 2026-08-15 (cont. 2) — FIX: video comercial usa minimax (producto real, sin distorsión) + prompt de imágenes no-competitivo

Jovan reportó, justo después del fix anterior (cont. 1, mismo día): "que
bueno por que las imagenes que se generaron horribles y el video peor"
— confirmando que arreglar el bug técnico no bastó, la calidad/fidelidad
real seguía mal. Se usó el nuevo agente `ecommerce-creative-director`
para diagnosticar, y luego se verificó todo en vivo, frame a frame, antes
de decidir la solución final (2 intentos descartados con evidencia real
antes de llegar al que funciona).

**Diagnóstico del agente (verificado en código):** el pipeline de video
comercial (`_run_video_pipeline`) nunca ancló el video a la foto real del
producto — el fix de la mañana (cont. de ayer) solo hizo que el
texto-a-video "funcionara técnicamente", pero seguía imaginando el
producto desde cero. Además, el prompt de imágenes de Higgsfield le
pedía al modelo dos cosas que compiten: "mantén el producto igual" Y
"ponlo en una escena nueva", en la misma instrucción — mismo patrón ya
resuelto en otro lado de este proyecto (`generate_product_prompts_endpoint`,
modo Kontext) que no se había reutilizado aquí.

**Video — 2 intentos antes de la solución real:**
1. Cambiar a `generate_video_img2vid` (LTX-Video/Wan2.1, imagen-a-video):
   verificado en vivo, frame a frame, que el producto arranca correcto
   pero se distorsiona gravemente ya en el frame ~20 de 97 — peor que el
   texto-a-video en algunos sentidos (ni siquiera se mantiene coherente
   todo el clip). Descartado con evidencia visual real, no solo hipótesis.
2. **Solución real: `minimax/video-01`** (`replicate_client.generate_video()`,
   ya usado correctamente en el endpoint más simple `/generate-video` de
   este mismo archivo — nunca conectado al pipeline de comercial completo).
   Verificado en vivo, frame a frame (0, 40, 120 de ~150+), que el
   producto se mantiene 100% intacto durante todo el clip, con movimiento
   de cámara real. Pipeline completo probado end-to-end (guion + narración
   ElevenLabs + 3 clips + combine): producto reconocible en las 3 escenas,
   ~16s de comercial real. Nota honesta: una de las 3 escenas mostró un
   tinte de iluminación más cálido/dorado del real — no una distorsión de
   forma/marca, pero vale la pena afinar el prompt si se nota mucho.

**Imágenes — prompt rediseñado (patrón Kontext, ya probado en este
proyecto):** ya no se describe el producto en el prompt (la foto de
referencia lo aporta) — solo se describe la escena/ambiente/iluminación,
con la instrucción de preservar el producto al final, corta, sin competir
con la descripción principal. De paso: `aspect_ratio` cambia de 1:1 a 3:4
(vertical, el más cercano a fotografía lifestyle real — 4:5 no es un
valor válido del enum real de Higgsfield, se confirmó al probarlo y
corregirlo) y `resolution` se especifica explícito en 1080p (antes
quedaba en el default 720p sin control del código). Verificado con
generación real: imagen notablemente más nítida y mejor iluminada.

Archivos: `app/services/replicate_client.py`, `app/services/higgsfield_client.py`,
`app/api/lanzar.py` (`_run_video_pipeline`), `app/templates/partials/item_edit_modal.html`
(etiqueta i2v/t2v/zoompan corregida — antes cualquier método que no fuera
exactamente "t2v" se mostraba como "Slideshow", ocultando el nuevo método
bueno).

---

## 2026-08-15 — FIX+FEAT: video comercial real (no slideshow) + galería de imágenes IA ancladas a la foto real

Jovan reportó (de nuevo, ya lo había mencionado antes) que el video generado
seguía siendo un "slideshow" tipo collage en vez de un video real con
movimiento, y que la generación de imágenes con IA solo daba 1 foto —
debía dar mínimo 5 — y no se basaba en la foto real del producto (riesgo
real de reclamos si el cliente recibe algo distinto a lo que ve).

**VIDEO — causa raíz:** `_t2v_ltx` (`app/services/replicate_client.py`)
usaba un payload obsoleto (`width`/`height`/`num_frames`/`frame_rate`/
`guidance_scale`/`num_inference_steps`) contra la ruta corta
`/v1/models/{owner}/{name}/predictions` — ese modelo (LTX-Video) no es
"oficial" en Replicate, esa ruta le devuelve 404 siempre, y ninguno de
esos campos existe ya en su schema real. Por eso el pipeline de video
comercial (guion en español + narración con ElevenLabs + 3 escenas
cinematográficas con personas usando el producto) SIEMPRE fallaba en
silencio y caía al respaldo de zoompan/slideshow — nunca se había
generado un video real por este camino, posiblemente desde que se
construyó esta feature.

Corregido a `/v1/predictions` con `version` explícita + el schema real
(`target_size`/`aspect_ratio`/`length`/`cfg`/`steps`). Verificado con una
predicción real completa antes de tocar código, y de nuevo end-to-end a
través de `generate_video_t2v()` ya corregido: video real con movimiento
de cámara generado con éxito, dos veces. De paso corregido `_t2v_wan`
(el fallback) al schema real también, aunque ese modelo devolvió un
error interno de Replicate (E002) en las pruebas — anotado por si
persiste, no parece relacionado a nuestros parámetros.

**IMÁGENES — causa raíz:** el modo "imagen" de Higgsfield
(`higgsfield_client.py`) generaba 1 sola foto usando `soul/standard`
(solo texto) — sin ninguna referencia real del producto. Investigado el
API real de Higgsfield (OpenAPI spec): existe `soul/reference`, que
exige una foto de referencia real y permite hasta 4 imágenes por lote.

Cambiado a `soul/reference` con la foto real del producto como
referencia obligatoria + 2 lotes de 4 (8 imágenes, sobre el "mínimo 5"
pedido) vía un nuevo endpoint tipo job (`POST /api/higgsfield/generate-images`
+ `GET /image-job/{id}`, mismo patrón que `_video_jobs` en `lanzar.py`).
Nueva galería en `higgsfield_modal.html`: grid de miniaturas clickeables
(mismo patrón ya usado en "Buscar fotos"/"Buscar automáticamente"), cada
clic agrega la foto directo al listing — antes no existía forma de
"usar" el resultado sin descargarlo y volver a subirlo a mano.

Verificado con datos reales (local y producción): 8 imágenes generadas
para el mismo producto de prueba, inspección visual confirma que el
resultado es virtualmente idéntico al producto real (mismo logo, forma,
botón) — no un producto "parecido" genérico.

---

## 2026-08-14 (cont. 14) — FIX: guardar título en items con `family_name` (PUT al endpoint de familia de ML, no al item directo)

Después de arreglar cuándo se muestra el botón de editar título (cont.
11), Jovan intentó guardar el título real de `MLM3322101329` y ML lo
siguió rechazando con el mismo error de siempre — la corrección de hoy
resolvía CUÁNDO se debería poder editar, pero el guardado seguía yendo
al endpoint equivocado.

Investigado de nuevo con `marketplace-strategist` (tercera vuelta sobre
el mismo tema el mismo día) + 2 pruebas reales autorizadas por Jovan
(payload no-destructivo, mismo valor actual, sin cambio de contenido):

- `PUT /items/{id}` con `title` **o** `family_name` directo: SIEMPRE
  falla (400, cause 374, "BODY_INVALID_FIELDS") para items con
  `family_name`, sin importar `sold_quantity`.
- `PUT /user-products-families/{family_id}` con `family_name`: **201
  Created** — este es el camino real, documentado oficialmente (página
  "Price per variation" de ML Developers), y coincide con la URL que usa
  la propia consola de vendedores de ML (`MLMU...` = `user_product_id`).

**Fix (`app/services/meli_client.py`, `update_item_title`):** ahora
detecta si el item tiene `family_name`; si es así, resuelve
`user_product_id → family_id` y manda el nuevo texto como `family_name`
al endpoint de familia en vez de `title` al item. Items sin `family_name`
siguen el camino directo de siempre, sin cambio.

Nota para el futuro: el título final visible = lo que guardemos +
atributos de variación que ML agrega después (ej. color) — no está bajo
control exacto nuestro, es el mismo comportamiento que tiene la consola
real de ML.

Verificado con prueba real no-destructiva a través del método ya
corregido (no solo un script suelto): 200/201 en todo el flujo, sin
alterar el contenido real del item.

---

## 2026-08-14 (cont. 13) — FEAT: título IA se ensambla por palabras priorizadas, ya no corta a media palabra

Jovan reportó un título real cortado a mitad de palabra ("...Triple Fi"
en vez de "...Triple Filtro") y pidió explícitamente la mejor solución,
no otro parche.

**Causa real:** la IA sí escribía una frase completa (63 caracteres),
pero el frontend le aplicaba encima un `.slice(0, 60)` ciego por
caracter — cortando lo que fuera que cayera justo en la posición 60,
palabra completa o no.

**Solución (cambio de arquitectura, no un trim más agresivo):** un LLM no
cuenta caracteres de forma confiable aunque se le pida un rango exacto,
así que se dejó de pedirle "escribe el título final de 55-60
caracteres". Ahora se le pide un **arreglo de palabras/frases cortas ya
priorizadas** de más a menos esenciales (Marca > Tipo > Característica
clave real > Tamaño > relleno opcional). El ensamblado final lo hace
código determinista (`_pack_title_from_chunks`, `app/api/sku_inventory.py`):
suma chunks en ese orden hasta topar el límite real de 60, **omitiendo
entero** cualquier chunk que no quepa completo — nunca corta uno a la
mitad, y sigue probando el siguiente (más corto) para aprovechar el
espacio que quede.

Fallback en 2 capas si la IA no responde JSON válido (parsea línea por
línea como palabras sueltas; si un candidato empaca muy corto, reintenta
palabra por palabra en vez de chunk por chunk) — nunca se cae de vuelta
al corte crudo de caracteres.

Frontend (`item_edit_modal.html`, `sku_inventory.js`): el `.slice(0,60)`
se reemplazó por `_trimTitleWordSafe()` (corta a la última palabra
completa) como red de seguridad adicional, ya no como mecanismo
principal.

Verificado: función aislada con el caso real reportado + generación real
end-to-end en local y contra producción ya desplegada — títulos de
50-58 caracteres, todos con palabras completas, incluyendo un caso que
sí logró meter "Triple Filtro" completo.

---

## 2026-08-14 (cont. 12) — FEAT: garantía estandarizada a 3 meses (defectos de fábrica) en descripción IA

Jovan notó en una descripción generada que la IA prometía "garantía de
12 meses respaldada por Eufy" — un compromiso que la empresa no puede
garantizar al no ser el fabricante. Pidió estandarizar a 3 meses por
defectos de fábrica, respaldada por el vendedor (no por la marca).

Regla agregada al prompt de descripción (`app/api/sku_inventory.py`):
siempre 3 meses por defectos de fábrica, ofrecida por el vendedor —
nunca mencionar marca/fabricante ni otro plazo, aunque la investigación
real del producto encuentre un dato distinto (ese dato solo aplica a la
sección de características técnicas).

Verificado con generación real, en local y contra producción ya
desplegada: la sección de garantía ahora dice exactamente "garantía de 3
meses por defectos de fábrica directamente por el vendedor", sin
mencionar la marca.

Solo aplica a Mercado Libre por ahora — Amazon no tiene esta feature de
descripción con IA conectada todavía (si se conecta en el futuro, aplicar
la misma regla ahí, por la regla de CLAUDE.md de features en todas las
plataformas).

---

## 2026-08-14 (cont. 11) — FIX: el título se bloqueaba por `family_name`, el gate real de ML es `sold_quantity>0`

Jovan corrigió directamente un hallazgo de hoy (cont. 9): mandó capturas
de la consola real de vendedores ML mostrando que SÍ pudo editar y
guardar el título de un listing con `family_name` — algo que nuestro
código (y el especialista, basado en un doc leído fuera de contexto)
había concluido como imposible.

Re-investigado con `marketplace-strategist`, con la nueva evidencia como
punto de partida (no descartando la investigación anterior, corrigiéndola).
Confirmado con la documentación oficial de ML Developers ("Sync and
modify listings"): el bloqueo real es `sold_quantity > 0` — una vez que
un listing vendió, se bloquea Título/Condición/Buying mode, tenga o no
`family_name`. El error histórico "cannot modify the title if the item
has a family_name" que motivó la conclusión de hoy casi seguro ocurrió en
un item que TAMBIÉN tenía ventas al mismo tiempo — `family_name` solo
indica de dónde sale el título sugerido, no bloquea su edición.

`catalog_listing: true` (buy-box real, distinto de `family_name`-only)
sigue siendo un bloqueo aparte y sí es correcto tal como estaba.

**Fix (`item_edit_modal.html`):** el botón/input/mensaje de título ahora
gatean con `sold_quantity > 0 or catalog_listing` en vez de solo
`family_name`. El mensaje de advertencia distingue "ya tiene ventas" de
"es catálogo real de ML".

Verificado: 4 escenarios (family_name+0 ventas, family_name+ventas,
catalog_listing real, item normal) con Jinja directo — los 4 se
comportan correctamente. Item real de Jovan (`MLM3322101329`,
`family_name` presente, `sold_quantity: 0` confirmado vía API) renderiza
con el botón de título visible tras el fix, coincidiendo con lo que él
demostró en la consola real de ML.

**Lección para el especialista** (ya aplicada en su archivo y en memoria
del proyecto): una fuente oficial citada fuera de contexto puede llevar
a una regla incorrecta aunque suene bien fundamentada — cuando el dueño
del negocio aporta evidencia empírica directa que contradice un hallazgo
"investigado", esa evidencia gana, y toca re-investigar con esa pista
en vez de descartarla.

---

## 2026-08-14 (cont. 10) — FIX: descripción/guion de video generados por IA se truncaban al primer párrafo

Jovan reportó que la descripción generada por IA se veía "patética" —
un renglón corto tipo eslogan, sin las características/specs que se
supone la IA debía investigar y detallar (feature de búsqueda web real
del mismo día, cont. 6/7). Investigado antes de tocar código: la IA SÍ
estaba investigando y escribiendo bien — el problema era de transporte,
no de contenido.

**Causa raíz:** el streaming SSE (`app/api/sku_inventory.py`, campos
`description` y `video_script`) mandaba el texto generado tal cual en
una sola línea `data: {texto}`. Cuando ese texto trae saltos de línea
internos (siempre — párrafo + lista de características + contenido del
paquete + garantía, tal como pide el prompt), el formato SSE se rompe:
solo la primera línea física lleva el prefijo `data: ` que el parser del
frontend reconoce, y todo lo que sigue se descarta en silencio.

Reproducido con datos reales antes de proponer el fix: la IA generaba
~2000 caracteres completos y correctos (specs reales del producto,
2000Pa, BoostIQ, etc.) para el item MLM3322101329 (Aspiradora Eufy con
fotos reales), pero el frontend solo mostraba ~600 (un párrafo).

**Fix:** empacar cada fragmento en JSON (`json.dumps({"text": ...})`)
antes de mandarlo por SSE — mismo patrón ya usado correctamente en
`health_ai.py` (Mensajes/Reclamos) — en los 3 puntos del backend con el
bug (descripción con imágenes, descripción con búsqueda web sin
imágenes, guion de video) y sus 3 consumidores en frontend
(`item_edit_modal.html`, `productos.js`, `sku_inventory.js`) que ahora
hacen `JSON.parse(data).text` en vez de concatenar el texto crudo.

Verificado: la misma petición real re-ejecutada tras el fix, tanto en
local como contra producción (Railway) ya desplegada, entrega el texto
completo (~1500-2000 caracteres, con "Características técnicas" /
"Contenido del paquete" / "Garantía" incluidos) en vez de cortarse al
primer párrafo.

**Nota:** el panel de sugerencias de título en `sku_inventory.js` (línea
~880-919) tiene un bug NO relacionado — llama a `field: 'title'` como si
fuera streaming SSE, pero ese campo del backend responde JSON plano, no
un stream. No se tocó porque está fuera del alcance aprobado hoy; queda
pendiente si se vuelve a reportar.

---

## 2026-08-14 (cont. 9) — FIX: "Optimizar Todo" truena sin avisar en listings con `family_name` (catálogo/User Product)

Jovan reportó que después del fix anterior (cont. 8), "Optimizar Todo"
seguía sin funcionar: la primera vez se quedaba pensando, la segunda no
hacía nada — y exigió explícitamente que antes de seguir parchando se
investigara bien cómo funciona ML en vez de adivinar código a ciegas.

**Investigación previa (agente `marketplace-strategist`, antes de tocar
código):** ML tiene 2 mecanismos de listing distintos, con reglas de
edición diferentes:
- `catalog_listing: true` + `catalog_product_id` — buy-box real
  compartido entre varios vendedores. ML documenta (Help Center) que
  bloquea título, descripción, fotos y ficha técnica por completo.
- Solo `family_name` en la raíz (`catalog_listing: false`) — "User
  Product". ML solo bloquea el **título** (doc oficial: *"the title
  field should not be sent by the seller... automatically completed
  based on domain, attributes, family_name"*, y confirmado por un error
  real histórico de este mismo código: *"You cannot modify the title if
  the item has a family_name"*). La descripción SÍ es editable.

**Causa raíz real:** `editModalAiTitle`/`editModalAiDesc` hacían
`btn.disabled = true` sobre `document.getElementById('btn-ai-title'/'btn-ai-desc')`
**antes** de entrar a su propio `try/catch`. Para un item con
`family_name` el botón de título ni se renderiza (guardia `{% if not
item.family_name %}` ya existente), así que `btn` era `null` y
`btn.disabled = true` lanzaba un `TypeError` sin capturar. Como
`editModalOptimizeAll` solo tenía `try { ... } finally { ... }` (sin
`catch`), la excepción mataba la secuencia completa: paso 1 (título)
truena de inmediato — de ahí el "se queda pensando" del primer intento —
y los pasos 2 y 3 (descripción, atributos) nunca llegaban a ejecutarse —
de ahí el "no hace nada" del segundo intento.

**Verificación en vivo (antes de dar por bueno el fix):** se consultó
directo contra la API real de ML el item reportado por Jovan
(`MLM3322101329`): `family_name` presente, `catalog_listing: False` —
confirma que es un "User Product", no catálogo real, y que su
descripción sí debería ser editable. Se renderizó el template real con
esos datos y se simuló la secuencia completa de "Optimizar Todo" con
jsdom: ya no truena, título se omite con mensaje explicativo, descripción
y atributos sí corren, el botón se reactiva correctamente al final.

**Fix (`item_edit_modal.html`):**
- `editModalAiTitle`/`editModalAiDesc`: guardia `if (!btn || ...) return;`
  antes de tocar cualquier elemento del DOM.
- `editModalOptimizeAll`: detecta con `!!document.getElementById(...)`
  qué pasos aplican a este item concreto, y si un paso se omite muestra
  el motivo real al usuario ("Título omitido: ML lo genera solo...",
  "Descripción omitida: catálogo real de ML...") en vez de fallar en
  silencio.
- Botón de descripción ahora solo se oculta cuando `catalog_listing` es
  `true` de verdad (antes no distinguía este caso del `family_name`-only,
  dejando pasar un botón roto en el peor caso y bloqueando de más en el
  mejor).

---

## 2026-08-14 (cont. 8) — FEAT: precio de lista sugerido (recuperación real 80%/60% después de deal 20%) + fix "Optimizar Todo" poco claro

**"Optimizar Todo" parecía atorado**: Jovan reportó que el botón se
quedaba en "Optimizando..." sin entender qué hacía. Son 3 pasos
secuenciales (título → descripción → atributos), cada uno con búsqueda
web real (agregada hoy), 20-40s en total. Agregado: tooltip explicando
qué hace, texto de progreso por paso ("1/3 Generando título...", "2/3
Buscando info y generando descripción...", "3/3 Rellenando atributos..."),
y `try/finally` para garantizar que el botón siempre se reactive aunque
algún paso falle.

**Precio de lista sugerido**: Jovan pidió calcular el precio de LISTA
para TVs de forma que, si se le pone un deal del 20% de descuento
después, el neto siga recuperando el 80% del retail real (después de fee
ML + retenciones fiscales 9.05% + envío + 7% comisión de socio) — 60%
para las demás categorías, mismas condiciones.

- `_suggest_list_price()` (`app/api/items.py`) — búsqueda binaria (no
  álgebra directa porque `_ml_fee()` es escalonado por tramo de precio,
  pero SIEMPRE baja al subir el precio, nunca sube — converge sin
  problema) que encuentra el precio de lista tal que, tras el descuento
  del 20%, el % de recuperación real quede exacto en la meta (80%/60%
  según si el SKU empieza con SNTV). Reusa exactamente la misma fórmula
  de neto que `_calc_margins()` (fee escalonado + 9.05% retenciones +
  envío + 7% socio) — mismo criterio en todo el sistema.
- `token_store.get_bm_retail_ph(sku)` — nuevo helper puntual (lee
  `bm_sku_master.retail_ph`, ya sincronizado, sin llamada nueva a BM).
- `GET /api/items/{item_id}/suggested-price?sku=X` — nuevo endpoint.
- Botón "💰 Sugerir precio (recuperación real)" en el modal de edición,
  junto al campo de precio — muestra el precio sugerido, el precio con
  deal aplicado, y el % de recuperación real, con un enlace "Usar este
  precio" para aplicarlo directo al campo (sin guardar automático, el
  usuario confirma con el botón "Guardar" de siempre).

Verificado: matemática aislada converge exacto a 80.0%/60.0% en 3 casos
(TV retail bajo, categoría normal, TV retail alto); endpoint HTTP real
con 2 SKUs reales de BM (SNTV007716 TV, SHIL000030 categoría normal)
confirma el mismo resultado con RetailPH y FX reales.

---

## 2026-08-14 (cont. 7) — FIX: unificación de los 2 Quality Score + consistencia entre campos generados por IA

Jovan pidió resolver los 2 pendientes que quedaron señalados (no solo
documentados): la duplicidad de Score de Calidad, y la inconsistencia de
la búsqueda web entre campos.

**Unificación del Score de Calidad**: confirmado que el multiget del gap
scan (`/items?ids=...` en `_get_meli_sku_set()`, `lanzar.py`) YA trae el
body completo del item (fotos/video/envío/atributos/status/tipo) sin
llamadas extra — lo único que ese contexto no tiene es la descripción
real (endpoint aparte por item, pedirla para miles de items en bulk
sería caro) y el precio-vs-competencia (cache aparte, solo top-20).

- `_calculate_health_score()` (`app/api/items.py:971`) ahora acepta
  `description=None` como "dato no disponible, omitir sin penalizar"
  (distinto de `description=""`, que SÍ penaliza como antes — cambio
  retrocompatible, ningún caller existente pasaba `None`).
- `_process_item_body()` (`app/api/lanzar.py`) ya NO calcula su propia
  fórmula reducida (solo título/fotos/GTIN+BRAND/precio>0) — ahora llama
  a la MISMA `_calculate_health_score()` que ve el usuario en el modal de
  edición, con `description=None, price_delta_pct=None`. Cero llamadas
  nuevas a la API de ML.
- Corregido de paso un bug real que se habría introducido con la
  unificación ingenua: la fórmula vieja reescalaba el estático a 70 pts
  a propósito, para dejar 30 pts de margen a las señales dinámicas
  (stock BM real/precio-vs-competencia/reclamos) sin pasarse de 100. Con
  el estático ahora en escala completa 0-100, sumar+clamp ocultaba
  señales dinámicas malas detrás de un estático ya perfecto (ej.
  contenido 100/100 + SIN stock real + reclamo abierto seguía dando
  100/100). Cambiado a promedio ponderado 70/30 real (ambos lados en
  escala 0-100) en vez de suma+`min(100,...)`.

**Consistencia entre campos generados por IA**: en vez de que cada campo
(título/atributos/guion de video) dispare su propia búsqueda web
independiente (pudiendo encontrar cifras distintas para el mismo
producto — visto en pruebas: "8,000 Pa" en descripción vs "15,000 Pa" en
guion de video), ahora el frontend (`item_edit_modal.html`,
`productos.js`) manda el contenido de la descripción YA generada como
`existing_description` en el contexto. Si hay una descripción sustancial
ya generada (>80 caracteres), título/atributos/guion la usan como ancla
de hechos confirmados en vez de disparar una búsqueda nueva (`web_search`
se desactiva automáticamente en ese caso — también ahorra costo).
Verificado: con una descripción real mencionando "HydroJet" y "3.6
horas", el título generado después usó esos MISMOS datos, no cifras
nuevas.

No elimina el 100% del riesgo (si el usuario genera título ANTES que
descripción, título sigue haciendo su propia búsqueda) pero cubre el
flujo natural de edición (descripción primero, luego el resto).

---

## 2026-08-14 (cont. 6) — FEAT: mismo criterio de "info real, no inventada" en título, atributos, GTIN, video e imágenes IA

Continuación del pedido de Jovan: "analiza todas las opciones al crear/
modificar un listado, buscando mejores prácticas ML y dando solución a
cada una — mejor título, descripción, bullets, características, y si no
hay imágenes buscarlas o generarlas."

Mapeo previo (agente Explore, solo lectura) confirmó: existen 2 sistemas
de Score de Calidad paralelos y no unificados —
`_calculate_health_score()` (`app/api/items.py:971`, el completo:
fotos/video/envío/título/descripción/GTIN/SKU/tipo, usado por el modal
de edición) y el de `_run_gap_scan()`/`ml_listing_quality`
(`app/api/lanzar.py`, más simple, usado en el radar de gaps). **No se
unificaron** — es una decisión de arquitectura aparte, señalada a Jovan,
no resuelta hoy.

**Agregado (mismo patrón `web_search=True`, sin tocar nada existente):**
- **Título** (`ai_improve`, campo `title`): ahora busca la ficha técnica
  real antes de elegir palabras clave. Filtro de seguridad nuevo: el
  modelo a veces agrega una "nota" con cita de fuente como 4ta línea —
  se descarta cualquier candidato >70 chars o con `[`/`http` antes de
  devolver los títulos (verificado en vivo: sin el filtro, esa nota se
  colaba como si fuera un 4to título).
- **Atributos** (`ai_improve`, campo `attributes`): mismo tratamiento —
  busca specs reales antes de sugerir valores.
- **Guion de video** (`ai_improve`, campo `video_script`): mismo
  tratamiento — los 2-3 beneficios que menciona ahora se basan en specs
  reales encontradas, no genéricas.
- `openrouter_client.generate()` (no-streaming, la usan título/atributos):
  mismo parámetro `web_search=False` por default que ya se agregó a
  `generate_stream()` para descripción.

**GTIN real** (`item_edit_modal.html`): nuevo botón "🔍 Buscar GTIN real"
junto a "✦ Rellenar con IA" de atributos — reusa `POST /api/lanzar/
search-upc` (UPCItemDB, ya existía pero solo en el wizard de publicación
nueva) para llenar el campo GTIN con un código real verificado, no
generado por IA. Verificado con el producto real del reporte: GTIN
0194644035570 encontrado y confirmado contra el título real del
fabricante.

**Generar foto/video con IA en listing ya publicado**: nuevo botón
"✨ Generar foto o video con IA" en la sección de fotos del modal de
edición — conecta el modal global de Higgsfield (`openHiggsfieldModal()`,
ya existía en `base.html`/wizard de Amazon/gaps, nunca alcanzable desde
la edición de un listing ya publicado) sin reimplementar nada.

**Nota importante, limitación conocida**: la búsqueda web no es 100%
determinista — en las pruebas, la descripción encontró "8,000 Pa" de
succión para el Eufy Omni S1 Pro y el guion de video encontró
"15,000 Pa" para el mismo producto en una búsqueda distinta. Es un
riesgo inherente de depender de resultados de búsqueda variables, no
algo que se pueda eliminar por completo — vale la pena que quien revise
el contenido generado lo tenga en cuenta antes de publicar.

Verificado end-to-end con datos reales (Eufy Omni S1 Pro): título con 3
opciones limpias y specs reales (HydroJet, 8000Pa, WiFi), atributos con
valores reales de fuente, guion de video sin asteriscos ni citas, GTIN
real confirmado, template del modal de edición renderiza correctamente
con los 5 elementos nuevos presentes.

---

## 2026-08-14 (cont. 5) — FEAT: descripción de producto con búsqueda web real (no más texto genérico)

Jovan reportó (screenshot) que la descripción generada por "✦ IA" en la
edición de un producto salía pobre/genérica ("patético") — solo se le
daba marca/modelo/título a la IA, sin buscar nada real del producto.

Antes de reusar el scraper de DuckDuckGo que ya existe (`product_researcher.py`,
usado hoy solo en el wizard de publicación nueva), Jovan preguntó por
mejores opciones. Investigado: OpenRouter (el proveedor que ya usamos)
tiene un plugin nativo de búsqueda web (`plugins: [{"id":"web"}]`, ~$4 por
1000 resultados vía Exa) — más confiable que mantener scraping propio, sin
dar de alta un proveedor nuevo. Se evaluó también APIs de UPC (Go-UPC,
Barcode Lookup) — mejor para datos estructurados exactos, pero requieren
cuenta nueva; quedó fuera de alcance por ahora.

**Agregado (sin tocar nada existente, pedido explícito de Jovan
"agrega, no cambies")**:
- `openrouter_client.generate_stream()`: nuevo parámetro `web_search: bool
  = False` — activa el plugin de búsqueda de OpenRouter. Default False,
  cero impacto en cualquier otro caller existente.
- `sku_inventory.py` (`ai_improve`, rama `description`, sin imágenes):
  `web_search=True` + instrucción explícita de buscar la ficha técnica
  real antes de escribir.
- Reglas reforzadas: no citar fuentes en el texto final (una vez se filtró
  "Fuente: [eufy.com](url)" en el resultado) y prohibición explícita de
  markdown (`**negrita**`) — MeLi no lo renderiza.
- `_strip_markdown_noise()` nueva (mismo patrón que `_title_case_ml`: red
  de seguridad server-side) — limpia `**`/`__`/enlaces markdown aunque el
  modelo no siga la instrucción al 100%.

Verificado end-to-end con el producto real del reporte (Eufy Omni S1
Pro): la descripción pasó de un encabezado vacío a incluir specs reales
y verificables (succión 8,000 Pa, tecnología HydroJet™, capacidades de
tanques, autonomía, dimensiones exactas, contenido del paquete) — sin
asteriscos ni citas de fuente en el texto final.

Pendiente (pedido más amplio de Jovan, mismo día): aplicar el mismo
criterio de "mejores prácticas ML + buscar información real" a TODOS los
campos de un listado (título, atributos/bullets, e imágenes — buscar en
línea o generar si faltan) — queda como conversación de alcance/prioridad
para una siguiente sesión, no implementado todavía.

---

## 2026-08-14 (cont. 4) — FEAT: instrucciones personalizadas para IA en Reclamos (mismo patrón de Mensajes)

Jovan pidió que "Sugerir Respuesta" en Reclamos permita agregar
instrucciones extra a la IA, igual que ya existe en Mensajes/Preguntas.
Replicado el patrón exacto (`ai-context-*` input + `user_context` en el
payload + bloque "MANDATO DEL VENDEDOR" en el prompt):

- `app/templates/partials/health_claims.html`: input de texto junto al
  botón "Sugerir Respuesta".
- `app/static/js/health_ai.js` (`suggestClaimResponse`): lee el input y
  lo agrega al payload.
- `app/api/health_ai.py` (`suggest_claim_response`): pasa `user_context`
  a `build_claim_response_prompt`.
- `app/services/health_ai.py` (`build_claim_response_prompt`): nuevo
  parámetro `user_context`, mismo bloque de mandato que
  `build_message_reply_prompt`.

No hay equivalente en Amazon (A-to-z Claims no tiene sugerencia de IA
hoy) — fuera de alcance de lo pedido, no se inventó nada nuevo ahí.

Verificado end-to-end en local: instrucción "ofrece devolución total y
discúlpate por la demora" → la IA generó la respuesta siguiendo
exactamente esa instrucción.

---

## 2026-08-14 (cont. 3) — FIX: barrido de 24h no alcanzaba órdenes atoradas varios días

Jovan preguntó por qué 2 órdenes reales (SNTV007716-GRB, order_date
2026-08-11) seguían sin alerta pese al fix de reconciliación. Causa: esas
órdenes ya tenían ~68h de antigüedad — fuera de la ventana de 24h del
loop rápido, porque nunca recibieron una nueva notificación de ML que las
hiciera re-entrar por el webhook.

Barrido manual de verificación (`hours=96`, 846 órdenes, 0 errores)
confirmó ambas órdenes + una tercera de 4 días atrás (SNTV007618) nunca
antes vista. Como una pasada de ese tamaño tarda varios minutos, no es
viable repetirla cada 5 min.

Fix: extraída `_run_stock_reconcile_pass(hours)` (compartida por ambos
loops + el diag manual). Nuevo `_realtime_stock_reconcile_wide_loop()` —
barrido de 7 días cada 2h, además del loop rápido de 24h/5min que ya
existía. Cubre el caso de una orden atorada por días sin sobrecargar el
ciclo frecuente.

---

## 2026-08-14 (cont. 2) — FIX: aviso "Ya hay stock — reactivar" quedaba obsoleto para siempre

Jovan reportó (captura real) que el aviso de reactivación seguía
mostrando SNTV004097 aunque el producto YA tenía stock. Verificado contra
producción (`/api/diag/bm-sku-master-lookup`): SNTV004097 tenía **12
listings activos con stock real** (1-3 unidades c/u) en las 4 cuentas ML.

Causa: `get_pending_restock_watches()` solo deja de mostrar un aviso si
alguien hace clic en "Descartar" (marca `reactivated_at`) — si el SKU se
reactiva por CUALQUIER otra vía (sync normal de restock, edición manual
en ML), nada limpia el registro y el aviso queda obsoleto para siempre.

Fix: antes de listar los avisos pendientes, se auto-marca como
reactivado cualquier registro cuya cuenta YA tenga un listing activo con
`available_qty > 0` para ese SKU — sin esperar a que un humano lo
confirme a mano. Self-healing: la próxima vez que se pida la lista, el
dato ya está limpio.

---

## 2026-08-14 (cont.) — FIX: faltaba la red de seguridad — el webhook nunca vuelve a evaluar una orden sin nueva notificación de ML

Tras el fix anterior (mismo día), Jovan reportó con captura real que la
pantalla de "Alertas de Stock" seguía en 0. Causa: el webhook de ML es
100% reactivo a eventos — si una orden ya se procesó una vez (antes o
después del fix) y ML no vuelve a notificar un cambio de esa MISMA orden
(status se queda "pending" sin cambiar), nunca se re-evalúa. El botón
"Actualizar" de la pantalla solo re-lee lo que ya está guardado en DB, no
dispara una revisión nueva contra BM.

- Extraída la lógica de "evaluar 1 orden" del webhook a
  `_evaluate_order_stock_alert(order_id, user_id, client)` — compartida.
- Nuevo `_realtime_stock_reconcile_loop()`: cada 5 min, re-evalúa las
  órdenes ML pagadas de las últimas 24h (leídas de `order_history`, sin
  gastar una llamada nueva de búsqueda a ML) con la misma lógica ya
  corregida. Como `_bm_bulk_available_qty()` es 100% en memoria, esto no
  agrega carga a BM — solo las llamadas normales a la API de ML
  (`resolve_order`/`get_shipment`, semáforo de concurrencia 3).
- `token_store.get_recent_paid_ml_orders(hours=24)` — nueva, lee
  order_id/account_id de `order_history` en la ventana.
- Diag temporal `/api/diag/trigger-stock-reconcile?hours=N` para forzar
  una pasada manual sin esperar el ciclo de 5 min (útil para verificar
  sin esperar, y como palanca manual futura si hace falta).

---

## 2026-08-14 — FIX: alerta "Sin Stock" (webhook ML) no detectaba órdenes realmente agotadas

Jovan reportó (con captura de BinManager "Problem Items Today") 6 órdenes
reales con problemas de fulfillment el mismo día que nuestra pantalla
"Alertas de Stock" mostraba "0 alertas, todo con stock disponible".
Investigación con datos reales (no supuestos) contra producción vía
`/api/diag/order-lookup` y `/api/diag/ml-webhook-activity`:

- Los 4 webhooks de ML SÍ llegaban y procesaban órdenes normalmente
  (94/81/18/16 en 24h) — el hueco no era de recepción.
- Causa raíz real: `_process_ml_order_webhook()` decidía si alertar
  leyendo `bm_sku_master.available_qty` — una tabla donde solo 1,772 de
  37,106 SKUs (4.8%) estaban realmente verificados/frescos en el ciclo
  más reciente. El resto depende de una cola de reconciliación limitada
  a 150 SKUs cada 120s (BM omite del archivo bulk cualquier SKU con
  stock=0, así que un SKU recién agotado puede tardar horas — verificado
  hasta 47h en 2 casos reales — en que su fila deje de mostrar el valor
  viejo).
- 2 de las 3 órdenes reales "Sin Stock" (SNTV007716, SNTV007884) SÍ
  estaban ausentes del archivo bulk que ya tenemos en memoria en el
  momento de verificar — la 3ra (SHIL000030-NEW) mostraba stock agregado
  (4, sumando todas las condiciones) aunque BM falló al pickear
  específicamente la condición NEW. Decisión de Jovan: ese 3er caso queda
  fuera a propósito — es un límite operativo de BM (no poder sustituir
  condición al pickear), no algo que debamos adivinar o replicar,
  "no confío en la lógica de BM".
- Se investigó también si BinManager expone directamente ese mismo
  reporte "Problem Items Today" vía API/MCP para jalarlo tal cual en vez
  de reconstruirlo — confirmado que NO existe tal endpoint, y que el log
  crudo de BM ni siquiera distingue "Sin Stock" de "No Mapeado" (mismo
  status interno `"Not Stock 0"` para ambos) — refuerza la decisión de no
  depender de la categorización de BM.

**Fix**: `_bm_bulk_available_qty()` (`app/main.py`, nueva función) busca
el SKU directo en el archivo bulk que ya está en memoria
(`_bm_bulk_gr_cache`/`_bm_bulk_all_cache`, el mismo que usa
`_bm_master_sync_once_inner()`) — si no aparece, se trata como sin stock
(por diseño de BM, "ausente del bulk" = "sin stock"). Cero llamadas
nuevas a BM. Reemplaza el uso de `token_store.get_bm_sku_available_qty()`
en la línea de decisión del webhook (única llamada a esa función en todo
el proyecto — se eliminó por quedar muerta).

De paso, bajado el intervalo del loop de prewarm (`_startup_prewarm`) de
15 a 10 minutos (pedido explícito de Jovan) para que el archivo bulk se
refresque más seguido — nota: ese loop hace mucho más que solo el bulk
(sync de gaps, vigilancia Buy Box Amazon, backfill de zonas), así que se
consideró pero se descartó separar el refresco del bulk en un loop
independiente por ahora (mayor alcance de cambio sin necesidad real hoy).

Verificado: test unitario aislado (mock de `_bm_bulk_gr_cache`/
`_bm_bulk_all_cache` con los datos reales de producción) confirma
ausente→None, presente→valor correcto, suma de variantes de condición, y
cache vacío→None sin tronar. Servidor local levantado sin errores,
`/partials/products-deals` responde 200, `/api/diag/sku` funcionando.

---

## 2026-08-14 — OPERACION: remote `mi2` migrado de PAT a deploy key SSH (resuelve bloqueo del 2026-07-21)

`git push mi2 main` fallaba desde el 21-jul por PAT expirado. Amir
Tafreshi autorizó a `coolify-manager` (bot de infra) emitir una deploy
key SSH ed25519 en vez de renovar el PAT — mejor en 3 ejes: no expira,
no puede filtrarse en la URL del remote (el PAT viejo SÍ estaba embebido
en texto plano en `.git/config`, el mismo patrón que causó 32 clones
comprometidos el 12-ago según el bot), y es revocable sola sin afectar
`origin` ni ningún otro repo.

- `~/.ssh/ecomops_deploy` (600) + `~/.ssh/config` (600, alias
  `github-ecomops`) + `known_hosts` con la host key real de github.com
  vía `ssh-keyscan` — todo fuera del repo, no versionado.
- `git remote set-url mi2 git@github-ecomops:mi2-apps/ecomops.git` —
  reemplaza la URL HTTPS con token embebido.
- Verificado: `ssh -T git@github-ecomops` → `Hi mi2-apps/ecomops!`
  (confirma que la llave solo alcanza ese repo). `git push mi2 main`
  puso a `mi2` al día de un jalón tras casi un mes bloqueado.

Nota de proceso: la llave privada llegó por un enlace de un solo uso
(cifrado en el navegador, la clave nunca toca el servidor) — Jovan lo
abrió él mismo y me pasó el contenido; no lo abrí yo directamente porque
mis herramientas no ejecutan el JS de descifrado del lado del cliente y
arriesgaba quemar el enlace de un solo uso sin conseguir nada.

Detalle completo en `.claude/memory/project_mi2_token_expired.md`.

---

## 2026-08-14 — FEAT: Centro de notificaciones unificado (base.html)

Consolida 3 banners globales sueltos (BM desactualizado, disco del servidor,
salud del sistema) + la campana de sugerencias cruzadas ML en un solo
dropdown accesible desde una campana en el nav — recomendado por el
especialista UX en la auditoría de lógica de negocio del 2026-08-08,
aprobado por Jovan tras ver un mockup (Artifact) antes de tocar código.

- Nuevo agregador compartido (`window._setSysAlert`/`_updateNotifBadge`/
  `_notifCheckEmpty`) al inicio de `base.html` — cada fuente (BM, disco,
  salud) sigue con su propio fetch/cadencia de siempre (sin cambios),
  solo cambia dónde se pinta el resultado: en vez de su propio banner fijo,
  ahora registra un item con severidad (crítico/advertencia) en el
  agregador, que renderiza agrupado dentro del panel de la campana.
- Campana movida fuera del bloque "solo ML" — ahora aparece en ML Y
  Amazon (antes Amazon no tenía ninguna campana, solo los banners sueltos
  que sí eran cross-platform). La sección "Sugerencias" del panel sigue
  siendo ML-only (`{% if active_platform != "amz" %}`), con guards
  `if (!_list) return;` en las funciones JS para que no truene en Amazon.
- Badge de la campana ahora suma alertas de sistema + sugerencias
  pendientes en un solo contador.
- Eliminados los 3 divs de banner fijo (`#bm-stale-banner`,
  `#disk-stale-banner`, `#global-health-banner`) — sin referencias muertas
  en el resto del proyecto (verificado con grep).
- El badge del tab "Salud" (nav superior) y la franja "Alertas de Stock"
  del Dashboard NO se tocaron — quedan igual, fuera de esta consolidación
  (así se acordó en el mockup).

Verificado en 2 niveles porque no había navegador conectado en la sesión:
(1) sintaxis Jinja + JS extraído validada con `node --check`; (2) prueba
funcional real con jsdom simulando el DOM de las páginas ML y Amazon
renderizadas en local — alertas de sistema se agrupan y cuentan bien,
badge combina ambas fuentes correctamente, estado vacío se calcula sobre
las 2 listas, acciones de sugerencias no truenan, y en Amazon (sin
`#notif-list`) nada se rompe. Sin acceso a navegador real en esta sesión
para una verificación visual — pendiente que Jovan le eche un ojo en vivo.

---

## 2026-08-13 (cont. 15) — FEAT: reversa de deuda de proveedor por reembolso real (ML + Amazon), no solo por cancelación

Cierra el pendiente #4 de "3 4 5": la deuda de proveedor (`supplier_debt_ledger`)
ya se revertía cuando la orden se cancelaba (`upsert_order_history()`,
`_DEBT_CANCEL_STATUSES`), pero NO cuando el reembolso ocurre DESPUÉS de
enviado — ahí el `status` de la orden nunca cambia a "cancelled", así que
ese caso quedaba sin cubrir y la deuda seguía viva aunque el proveedor ya
no debiera cobrarse.

**ML**: el dato ya existía en el claim, solo faltaba extraerlo y usarlo.
`_process_claim()` (`app/main.py`) ahora lee `resolution.reason` del claim
real de ML y marca `refunded_buyer=1` cuando `resolution.reason ==
"payment_refunded"` (confirmado con datos reales, no supuesto). Columnas
nuevas `resolution_reason`/`refunded_buyer` en `claims_history`.
`upsert_claims_history()` (`token_store.py`) revierte automáticamente
(`amount_mxn=0, reversed_at=<ts>`) cualquier fila de `supplier_debt_ledger`
con ese `order_id` que siga con deuda activa.

Nota de corrección: al investigar esto sospeché que `claims_history.order_id`
podía traer mal el `shipment_id` para reclamos tipo "shipment" (un bug real
en potencia). Al revisar con más cuidado el código de persistencia real
(`app/main.py` línea 2549) confirmé que YA filtra correctamente
(`resource_id if c.get("resource") == "order" else ""`) — no había bug ahí,
solo en 2 funciones de display separadas que no tocan la reversa de deuda
(cosmético, no bloqueante, no se tocó).

**Amazon**: no hay un campo "resolution" como en ML — la señal real es la
Finances API (`RefundEventList`), ya expuesta por
`amazon_client.get_refunds_detail(days)` y cacheada 3h vía
`_fetch_amazon_refunds_cached()` en `main.py`. Función nueva genérica
`reverse_debt_by_order_ids(order_ids, platform)` en `token_store.py`
(reusa el mismo patrón idempotente: solo toca filas `amount_mxn>0 AND
reversed_at=0`). Caller nuevo `_run_debt_reversal_background()` en
`amazon_listing_sync.py`: recorre todas las cuentas Amazon, junta
`order_id` de reembolsos de los últimos 90 días, y llama la reversa —
conectado al mismo ciclo de gap scan (cada 3h, ver cont. 13) para no
agregar un scheduler nuevo.

Verificado end-to-end con datos sintéticos (`reverse_debt_by_order_ids`):
inserta deuda con `amount_mxn=1234.56`, reversa → `amount_mxn=0` +
`reversed_at` con timestamp real, segunda corrida sobre la misma orden
reversa 0 filas (idempotente, no doble-cuenta). Servidor local levantado
sin errores nuevos, `/partials/products-deals` responde 200.

---

## 2026-08-13 (cont. 14) — FEAT: envío promedio histórico real por SKU (reemplaza estimado fijo/escalonado)

Cierra el pendiente de "otros hallazgos" (cont. 3): el envío en Deals
(`_calc_margins()`) usaba un estimado fijo ($150) o escalonado por tramo
de retail (400/250/150/100) — nunca el costo real. Ambas plataformas YA
calculaban el costo REAL de envío por orden (ML: `get_shipment_costs()`,
usado en `/api/orders` para el neto real pero descartado después sin
persistir; Amazon: costo por item ya calculado en
`_save_amazon_items_history_bg`) — solo faltaba guardarlo y promediarlo.

- Columna nueva `shipping_cost_mxn` en `order_history` (migración).
- `upsert_order_history()` la guarda ahora (ON CONFLICT prefiere el valor
  no-cero, mismo patrón que `costo_mxn`).
- ML (`app/main.py`, endpoint `/api/orders`): `_eo.shipping_cost` (ya
  calculado ahí) se prorratea por item con el mismo ratio que `neto_plat`
  y se persiste.
- Amazon (`amazon_orders.py`): el `ship` por item (ya calculado) se
  persiste directo.
- `get_avg_shipping_cost_map(skus, platform, days=90, min_samples=3)` en
  `token_store.py`: promedia `shipping_cost_mxn` real de los últimos 90
  días por SKU, requiere mínimo 3 órdenes reales — si no hay suficiente
  historial, el SKU simplemente no aparece en el mapa y el caller cae al
  estimado de siempre (SKU nuevo, sin ventas todavía).
- `_calc_margins()` acepta `shipping_avg_map` opcional — lo usa en los 2
  lugares que antes tenían el estimado fijo/escalonado. Conectado en el
  endpoint de Deals (`products_deals_partial`) — los otros 3 call-sites
  de `_calc_margins()` (production-kpis auxiliar, sku-history, etc.)
  siguen con el estimado de siempre por ahora (parámetro opcional,
  compatible hacia atrás).

Verificado end-to-end con datos sintéticos: promedio de 4 órdenes reales
calcula bien (126.25), 1 sola orden correctamente NO alcanza el mínimo de
3 muestras (mapa vacío, cae al estimado), y `_calc_margins()` usa el
valor correcto en ambas ramas cuando el mapa trae datos. Probado también
en vivo local: `/partials/products-deals` responde 200 sin errores
nuevos.

Pendiente (no parte de este cambio, señalado a Jovan): Amazon no tiene
un desglose de costo de envío tan limpio como ML para casos FBM —
`_build_finanzas()` (rentabilidad por orden Amazon) no se tocó porque su
fórmula ya usa fees reales de Finances API (`fba_fee`) de forma distinta
y agregar el promedio ahí podría duplicar el descuento de envío.

---

## 2026-08-13 (cont. 13) — INVESTIGACIÓN + FIX: gap scan (Sin Publicar) subía solo 1x/día en ambas plataformas

Jovan reportó 2 SKUs reales (SNVC000743, SNVC000747) que no aparecían en
"Sin publicar" en ML pese a tener stock real en BM. Investigación con
datos reales (BinManager MCP, `inventory_by_sku`, `okf_get`) antes de
tocar código:

1. **Confirmado real y corregido**: el gap scan de ML (`_nightly_gap_scan_loop`,
   `app/api/lanzar.py`) corría 1x/día (3am hora México) — un SKU con stock
   nuevo en BM podía tardar hasta 24h en aparecer como candidato a lanzar.
   Confirmado en vivo: SNVC000743 apareció de inmediato al forzar
   "Escanear ahora" manual. Cambiado a cada 3h (acordado con Jovan).

2. **Hallazgo descartado tras investigar más a fondo (documentado para no
   repetir la duda)**: inicialmente sospeché que `ConfColumns_Conditions_Excel`
   (fuente de stock del gap scan) subestimaba el stock real al no sumar
   bien varias condiciones/almacenes — un análisis de 40 SKUs mostró 35%
   con "discrepancia". Consultando la documentación oficial de BinManager
   (`concepts/inventory-states`, `concepts/inventory-conditions` vía OKF)
   se confirmó que el análisis estaba mal planteado: `Available` (campo
   crudo por ubicación) **no es lo mismo que "vendible"** — condiciones
   como `PNP`/`DMT` están explícitamente marcadas "no vendible como
   producto terminado" (estados técnicos/reparación), e `ICD` es vendible
   pero NO en línea (solo B2B). El endpoint actual y `get_bulk_stock()`
   (el mecanismo ya confiable del resto de la app) coinciden en el mismo
   número — ninguno de los dos subestima, mi comparación inicial sumaba
   unidades que BM ya excluye correctamente. **No se tocó la fórmula de
   stock** — habría sido un cambio real hacia PEOR (inflar el stock
   mostrado con unidades no vendibles).

3. **De paso**: Amazon ya corría su gap scan cada 6h (no 1x/día como
   pensaba al inicio) — encontré una función muerta (`_next_8pm_mexico_secs()`,
   nunca llamada) y un docstring desactualizado en `amazon_listing_sync.py`
   que decían "1x/día a las 8pm" cuando la lógica real ya era por intervalo
   transcurrido. Eliminada la función muerta, corregidos los docstrings,
   y bajado el intervalo de Amazon de 6h a 3h para igualar el ritmo con ML.

Verificado: ambos módulos importan limpio, servidor local arranca sin
errores nuevos.

---

## 2026-08-13 (cont. 12) — FEAT: reversa de deuda de proveedor en cancelaciones + alerta temprana de reputación

Cierra los 2 hallazgos restantes de "otros hallazgos" (cont. 3), con
"adelante con todos" de Jovan:

**1. Reversa de deuda de proveedor** (`supplier_debt_ledger`): antes, una
vez registrada la deuda de una venta (% del retail BM), quedaba para
siempre sin importar si la orden se cancelaba después — cero mecanismo
de reversa. Agregada columna `reversed_at`; `upsert_order_history()`
ahora revierte (`amount_mxn=0`) cualquier deuda ya registrada cuando la
fila trae un status de cancelación (`cancelled`/`Cancelled`/`Canceled`,
cubre ML+Amazon). **Alcance limitado, documentado**: solo cubre
cancelaciones (antes de envío) — un reembolso DESPUÉS de enviado no
cambia el `OrderStatus` en Amazon (vive en Finances API, reembolsos
separado) ni el `status` en ML; cubrir eso es una investigación aparte,
no incluida aquí. Probado end-to-end con datos sintéticos (orden
"paid" → genera deuda → misma orden "cancelled" → deuda se revierte a 0).

**2. Alerta temprana de reputación** (`reputation_snapshots`, tabla
nueva): antes solo existía el estado ACTUAL (badge verde/amarillo/rojo +
distancia al siguiente umbral), sin poder detectar que se está
deteriorando ANTES de cruzar a una zona peor. Snapshot diario
(`UNIQUE(account_id, captured_date)`, `INSERT OR IGNORE`) alimentado
gratis desde `stock_sync_multi.py` — ya llama `get_user_info()` cada
ciclo (5 min) para el `rep_factor`, ahora también guarda 1 snapshot/día
sin gastar ninguna llamada extra a la API. `get_reputation_trend()`
compara el snapshot más viejo disponible en 14 días contra el más
reciente; `worsening=True` si el `level_id` bajó de rango O cualquier
rate (claims/cancelaciones/demoras) subió ≥1pp — umbral conservador para
no alertar con ruido normal. Mostrado como banner naranja en la parte de
arriba de Salud (`health_summary.html`) cuando hay tendencia negativa.
Probado end-to-end (deterioro real detectado, caso estable NO marcado
como falso positivo).

Ambas features probadas con datos sintéticos limpiados después (no
quedó nada de prueba en `tokens.db`).

---

## 2026-08-13 (cont. 11) — FEAT: margen reemplaza costo BM (no confiable) por % de recuperación de retail

Jovan retomó la discusión de "otros hallazgos" pausada antes (ver cont. 3):
el margen mostrado en Deals y en Amazon usaba `AvgCost`/`cost_usd` de BM,
que Jovan confirma no es confiable. Regla nueva acordada: el margen se
mide como % del retail de BM (`retail_ph`) que se recupera DESPUÉS de
TODOS los gastos (fee ML/Amazon escalonado, retenciones fiscales 9.05%,
envío, comisión de socio 7%) — meta mínima 80% para TVs (`SNTV*`), 60%
para las demás categorías.

**Deals** (`app/main.py`): ya existía casi toda la fórmula
(`_recup_retail_pct`, `_calc_margins()` líneas ~231-254) sin usarse para
ninguna alerta. Agregado `_RECOVERY_TARGET_TV=80.0`/`_RECOVERY_TARGET_OTHER=60.0`
y los campos `_recup_target_pct`/`_recup_below_target` por producto. La
alerta "deal con margen negativo" (antes `_margen_pct < 0`, basada en
costo BM) ahora usa `_recup_below_target` — ya no depende de AvgCost en
absoluto.

**Amazon** (`app/api/amazon_orders.py`, `_build_finanzas()`): de paso se
encontró un bug real de unidades — mezclaba `costo_mxn` (ya en MXN) con
`neto` (MXN) después de "convertirlo" a USD dividiendo por FX, restando
USD de un monto MXN sin reconvertir. Reemplazado por el mismo criterio de
recuperación de retail (usa `_sku_retail_map`, ya en MXN, sin conversión
de unidades). Template `amazon_order_items.html` actualizado: ya no
muestra "Costo producto (BM)" sino "Retail BM (referencia)" + "Recupera
X% del retail (meta Y%)".

Verificado con datos sintéticos realistas (no con orden real — la orden
de prueba no tenía items vía API en local): TV con retail_ph=$400 USD
vendiendo a $9500 MXN → recupera 82.5%, no se marca (sano); vendiendo a
$7000 MXN → recupera 59.6%, se marca (por debajo de meta 80%). Categoría
detectada correctamente por prefijo `SNTV`.

**Nota importante**: `assistant_tools.py` (`tool_get_item_profitability`)
NO se tocó — se descubrió que es código muerto, ningún archivo lo
importa. De paso se confirmó que `claude_client.py`/Anthropic directo ya
no se usa en absoluto (migrado a OpenRouter desde 2026-06-08 y
2026-07-16, exactamente por quedarse sin crédito — ver DEVLOG de esas
fechas) — la clave de Anthropic hardcodeada (pendiente de rotar, ver
cont. 3) ya no tiene ningún impacto funcional, solo el riesgo de
seguridad en sí.

Pendiente (no parte de este cambio): envío sigue siendo estimado por
tramo fijo (400/250/150/100 según retail), no el histórico real por
SKU/plataforma que se platicó — ese es el siguiente paso.

---

## 2026-08-13 (cont. 10) — DROP TABLE bm_product_catalog/bm_stock_snapshot (respaldadas primero)

Jovan pidió respaldo antes de borrar, aunque ya estaba confirmado que
ambas tablas no tenían lectores ni escritores reales (cont. 8). Flujo:

1. Endpoint temporal `/api/diag/frozen-tables-size`: producción real —
   `bm_product_catalog` 11,124 filas / 1.74 MB, `bm_stock_snapshot`
   1,653 filas / 0.05 MB. Poco espacio (el problema de disco fue
   `audit_log`, ya resuelto antes), pero limpieza sin riesgo.
2. Endpoint temporal `/api/diag/frozen-tables-export`: dump completo de
   ambas tablas → `backups/bm_frozen_tables/backup_2026-08-13.json`
   (gitignored). Verificado: conteo de filas en el archivo coincide
   exacto con el conteo real (11,124 + 1,653).
3. Endpoint temporal `/api/diag/frozen-tables-drop` (con confirmación
   explícita `expected_gone=si-ya-respalde`, mismo patrón de seguridad
   que `audit-log-purge`): ejecutado en producción.
4. Verificado sano post-drop: login, `/api/amazon/products/sin-publicar`,
   `/api/diag/cache-health` — todos 200.
5. Limpieza de código: los 3 endpoints temporales eliminados; los
   `CREATE TABLE IF NOT EXISTS` de ambas tablas en `token_store.py`
   eliminados (si no, se recrean vacías en cada arranque); la migración
   de backfill `bm_product_catalog/bm_stock_snapshot → bm_sku_master`
   (guardada por `if bm_sku_master vacío`, ya nunca vuelve a correr)
   eliminada también — referenciaba tablas que ya no existen, hubiera
   tronado si alguna vez se hubiera vuelto a disparar.

Cierra el pendiente de [[project_security_hardening_2026-08-13]] sobre
la decisión de DROP.

---

## 2026-08-13 (cont. 9) — OPERACIÓN: refresh_token de Amazon (VECKTOR) rotado por exposure window de 6 meses

Continuación del hallazgo `APP_PIN` (cont. 3): dado que el PIN protegía
el refresh_token real de Amazon (VECKTOR IMPORTS) y estuvo expuesto en
el repo público ~6 meses, se decidió rotar el token real, no solo el PIN.

Jovan revocó el acceso de la app "VeKtorClaude" desde Seller Central
(Manage Your Apps → Disable Authorization) — esto invalida el token
viejo de inmediato sin importar quién más lo tuviera. Un primer intento
de re-autorizar usando el botón "Re-Authorize" de Seller Central no
guardó el token nuevo correctamente (confirmado con un intercambio LWA
directo contra Amazon: `invalid_grant`) — probablemente por un
redirect_uri distinto al que espera nuestro callback. Segundo intento
usando el link directo (`/auth/amazon/connect`) sí funcionó.

Verificado con una llamada LWA real (`grant_type=refresh_token` directo
contra `api.amazon.com`, sin pasar por nuestro código): el token nuevo
sí intercambia por un access_token válido, el viejo confirmado muerto.
`AMAZON_REFRESH_TOKEN` actualizado en Railway (para el seed de
auto-recovery en redeploys) y en `.env.production` local. Verificado en
producción real tras el restart: llamada real a Amazon para VECKTOR
(`/api/amazon/products/sin-publicar`) responde 200.

Cierra el pendiente de [[project_security_hardening_2026-08-13]] sobre
la decisión de re-autorizar VECKTOR.

---

## 2026-08-13 (cont. 8) — FIX: 3 queries leían precio BM de tabla congelada (bm_product_catalog) en vez de bm_sku_master

Investigando si `bm_product_catalog`/`bm_stock_snapshot` seguían vigentes
(pendiente "decidir si tirarlas" de la auditoría del 2026-08-08): confirmé
que ambas tablas están genuinamente congeladas — nada las escribe ya
(`upsert_bm_catalog_batch`/`upsert_bm_stock_snapshot_batch` escriben en
`bm_sku_master` a pesar de conservar esos nombres legacy). Pero encontré
**3 lecturas activas que seguían apuntando a la tabla congelada** en vez
de a `bm_sku_master`:

- `token_store.get_deletion_candidates()` (candidatos a borrar en Amazon
  por falta de venta) — `LEFT JOIN bm_product_catalog` → ahora
  `bm_sku_master`. El precio BM mostrado para decidir si borrar un
  listing podía estar desactualizado desde antes del corte Fase D.
- `amazon_products.py` — 2 queries del listado de productos Amazon
  (suprimidos + inactivos) YA hacían join con `bm_sku_master` para el
  stock, pero seguían jalando el precio (`bm_price`) de la tabla
  congelada — inconsistencia real dentro de la misma query. Corregido
  para usar `bm_sku_master` también para el precio (una sola fuente).

Verificado en vivo: `get_deletion_candidates()` corre sin error contra
producción local; endpoint `/api/amazon/products/sin-publicar` responde
200 tras el cambio.

**Pendiente, no ejecutado — requiere tu aprobación explícita**: ambas
tablas (`bm_product_catalog`, `bm_stock_snapshot`) ya no tienen ningún
lector ni escritor real — son candidatas limpias para `DROP TABLE` y
liberar espacio en el disco de Railway (recordar
[[project_disk_crisis_2026-07-31]]). No las borré porque es una acción
destructiva/irreversible — la dejo para que decidas.

---

## 2026-08-13 (cont. 7) — LIMPIEZA: production-kpis con cliente BM inconsistente + logging en 7 fallos silenciosos

1. **`/api/planning/production-kpis`** creaba su propio `BinManagerClient()`
   y lo cerraba en un `finally` — inconsistente con el resto de la app
   (todo lo demás usa `get_shared_bm()`, el singleton compartido que ya
   maneja login/re-login). Corregido para usar el singleton, sin
   `.close()` explícito (cerrar el compartido rompería a otros callers
   concurrentes). Verificado en vivo localmente con datos reales.

2. **7 `except Exception: pass` silenciosos con impacto real en negocio**,
   ahora con `logger.warning(...)` (mismo comportamiento, solo visibilidad):
   - `_get_usd_to_mxn()`: si falla el tipo de cambio real de ML, cae a
     20.0 fijo sin dejar rastro — afecta TODOS los cálculos de margen.
   - `get_order_sale_fee` (reporte de ventas): fee=0.0 en silencio si
     falla — infla el margen mostrado sin avisar.
   - `get_shipment_costs` (reporte de ventas): costo de envío ausente
     en silencio si falla.
   - 4 escrituras de `audit_log` (`stock_order_substitution`,
     `amazon_buyer_message_take`, `amazon_buyer_message_status`,
     `ml_concentration`): la acción real SÍ se ejecuta, pero si el
     registro de auditoría fallaba, se perdía sin ningún rastro —
     ahora al menos queda en los logs de Railway.

---

## 2026-08-13 (cont. 6) — LIMPIEZA: BM_USER/BM_PASS con default roto + 4 constantes LocationIDs muertas

Arquitectura/tech-debt de la auditoría del 2026-08-08. Dos hallazgos:

1. **`BM_USER`/`BM_PASS` con default apuntando a cuentas rotas**:
   `binmanager_client.py` caía a `claudio.suarez@...` (HTTP 500 en todo,
   ver CLAUDE.md "Cuentas BM") y `app/api/lanzar.py` caía a
   `Carlos.Herrera@...` (IsFirstUse=true, retorna `[]` siempre) — dos
   defaults DISTINTOS, ambos apuntando a cuentas conocidas como no
   funcionales. `lanzar.py` ahora importa `_BM_USER/_BM_PASS/_BM_BASE`
   desde `binmanager_client.py` (única fuente de verdad) en vez de
   redeclararlos. Default de `binmanager_client.py` cambiado a
   `Claude.Jovan@...` (la cuenta de servicio ACTIVA). `BM_PASS` sin
   fallback (antes "123456", literal débil en el repo). Además: `BM_USER`/
   `BM_PASS` no estaban en `.env` local — agregados (copiados de Railway)
   para que las pruebas locales de BM funcionen. Verificado en vivo:
   login real contra BinManager exitoso con las credenciales corregidas.

2. **4 constantes de LocationIDs vendibles (`"47,62,68"`) muertas** —
   declaradas y nunca usadas en `lanzar.py`, `stock_sync_multi.py`,
   `items.py`, `productos.py` (cada una con su propio comentario "sin uso
   directo hoy"). Eliminadas. El valor real sigue viviendo como default
   de parámetro en los métodos de `binmanager_client.py`
   (`get_bulk_stock`, `get_stock_with_reserve`, `_query_bm_stock`) — esa
   ya es la única fuente de verdad funcional; los usos activos restantes
   en `amazon_products.py`/`sku_inventory.py` tienen semántica distinta
   (desglose con Tijuana incluida) y se dejaron intactos.

Verificado: los 4 módulos importan limpio, servidor local arranca sin
errores nuevos.

---

## 2026-08-13 (cont. 5) — Verificado y cerrado: admin ya NO tiene la contraseña default

Endpoint temporal `/api/diag/admin-pw-check` (solo devolvía booleano,
nunca hash/salt) confirmó contra producción real: el usuario `admin` ya
no tiene la contraseña default de `init_user_db` (`010817xD`) — Jovan ya
la cambió en algún momento. Sin acción pendiente ahí. Endpoint eliminado
tras usarlo (era de un solo uso, según su propio docstring).

---

## 2026-08-13 (cont. 4) — FEAT: rate-limiting en /login/verify

`/login/verify` no tenía ningún límite de intentos — fuerza bruta viable
sin restricción. Agregado lockout en memoria (`_login_attempts`, 1 solo
worker uvicorn por Procfile, mismo patrón que `_bm_stock_cache`): 5
intentos fallidos por username en 15 minutos → bloqueo de 15 minutos.
Se limpia en cualquier login exitoso. No afecta a otros usuarios (la
llave es el username, no global). Probado en vivo localmente: 6 intentos
fallidos con un usuario de prueba → el 6to ya viene bloqueado; un login
real con `admin` en paralelo no se ve afectado.

---

## 2026-08-13 (cont. 3) — SEGURIDAD CRÍTICA: APP_PIN protegía el refresh_token real de Amazon con el mismo valor hardcodeado en el repo público

Mientras unificaba `SECRET_KEY` encontré algo más grave que el DIAG_TOKEN:
`APP_PIN` (default hardcodeado `"8741"` en `app/config.py`, repo público)
protege `/auth/amazon/export-token` y `/api/system-health/amazon-token-full`
— endpoints que devuelven el **`refresh_token` real de Amazon (VECKTOR
IMPORTS)**, una credencial viva con acceso a SP-API, no un endpoint de
diagnóstico interno. Confirmé contra la API de Railway que el valor real
configurado en producción era **exactamente el mismo `"8741"`** que el
default impreso en GitHub — cualquiera podía pedir
`https://apantallatemx.up.railway.app/auth/amazon/export-token?pin=8741`
y recibir el refresh_token completo.

Confirmé que no hay ninguna UI que pida este PIN tecleado a mano
(`pin.html` existe pero no está referenciado por ninguna ruta — vestigial)
así que no hay que mantenerlo corto/numérico. Rotado a un secreto largo
random, seteado en Railway vía API, `.env`/`.env.production` actualizados.
Verificado en vivo: PIN nuevo 200, PIN viejo (`8741`) 401.

De paso: unifiqué las 3 fórmulas distintas de fallback de `SECRET_KEY`
(`config.py`, `user_store.py`, `make_jwt2.py` — este último SÍ estaba
tracked en git con su propio literal hardcodeado) en una sola fuente de
verdad en `app/config.py`. En producción (`IS_PRODUCTION=True` vía
`RAILWAY_ENVIRONMENT`) ahora es obligatorio — sin fallback, para nunca
firmar sesiones reales con una clave adivinable si algún día se borra la
variable de Railway por error. Confirmé que el `SECRET_KEY` real en
Railway ya era un valor random fuerte (no el literal expuesto) — no hubo
compromiso activo de sesiones, solo el riesgo de "si un día falta la
variable, cae a algo débil". `.env.production` local también corregido
(tenía un valor desincronizado de Railway, solo afectaba pruebas locales).

Verificado en vivo tras cada cambio (servidor local): login real
(`/login/verify`) sigue firmando/verificando bien con la clave unificada.

---

## 2026-08-13 (cont. 2) — SEGURIDAD: DIAG_TOKEN rotado + bypass real de env var en 22 endpoints + cookies secure

De la auditoría de seguridad del 2026-08-12: el token que abre ~60
endpoints de diagnóstico sin login (`/api/diag/*`) tenía un default
hardcodeado (`dk_b55c...`) que además estaba **impreso tal cual en
CLAUDE.md**, y CLAUDE.md vive en el repo público
`github.com/ApantallateMX/mercado-libre-dashboard` (confirmado público,
HTTP 200 sin auth). Mismo tipo de exposición que la clave de Anthropic
pendiente, pero esta no depende de que Jovan haga nada — la puedo rotar
yo solo.

Al investigar encontré algo peor que el default expuesto: **22 endpoints
individuales redeclaraban `_DIAG_TOKEN`/`_DT` como variable local con el
valor hardcodeado**, ignorando por completo la variable de entorno
`DIAG_TOKEN` — es decir, aunque alguien configurara la variable en
Railway, esos 22 endpoints seguían aceptando el token viejo/expuesto sin
importar qué. Bug real, no solo "falta rotar", eran candados que decían
leer la config pero no la leían.

**Fix**: token nuevo generado (`secrets.token_hex`), seteado en Railway
vía API (`variableUpsert`), en `.env`/`.env.production` locales
(gitignored). Los 22 endpoints ahora usan la constante global
`_DIAG_TOKEN` (que sí lee `DIAG_TOKEN` del entorno) — se eliminaron las
redeclaraciones locales. `_DEBUG_KEY` (2 endpoints de debug legacy,
menor severidad, no estaba expuesto en git) también movido a env var.
Referencias al token viejo en `CLAUDE.md`, 2 agentes (`marketplace-strategist.md`,
`planning-specialist.md`) y 1 `.bak` reemplazadas por `<DIAG_TOKEN>` +
nota de "no escribir el valor real, repo público". `scripts/archive_audit_log.py`
(corre 1x/noche vía Task Scheduler, purga audit_log) también tenía el
token hardcodeado — ahora lo lee de `.env`/`.env.production` con
`python-dotenv`.

Evalué convertir los endpoints destructivos (`ml-item-stock-fix`,
`clear-bm-sku`, etc.) de GET a POST — decisión: NO, el modelo de auth
aquí es un token estático en query string, no cookie de sesión, así que
CSRF no aplica y el método HTTP no cambia el riesgo real. El único
candado real es el token, ya rotado y ya sin bypass.

**Además**: las 8 llamadas `set_cookie` (dash_session, active_account_id,
active_amazon_id, last_platform) no tenían `secure=True` — la sesión se
podía enviar sobre HTTP plano si alguna vez hubiera una ruta no-HTTPS.
Agregado `IS_PRODUCTION` en `app/config.py` (`bool(os.getenv("RAILWAY_ENVIRONMENT"))`,
Railway la inyecta solo, sin config manual) y `secure=IS_PRODUCTION` en
las 8 cookies — en local sigue sin `Secure` (HTTP, login probado y
funcionando), en Railway ahora sí la lleva.

Verificado en vivo antes de subir: token nuevo acepta 200 en
`/api/diag/cache-health` y en los 2 endpoints que tenían el bypass
(`amazon-accounts`, `mlmu`); token viejo rechazado con 403 en los tres.
Login real probado localmente (`/login/verify`), cookie sin `Secure` en
HTTP local como se espera.

---

## 2026-08-13 — FIX: stock_concentrator.py sin ponderación de reputación

Del reporte de auditoría de lógica de negocio (5 especialistas, 2026-08-08,
ver DEVLOG cont.), quedaba pendiente el hallazgo más directamente análogo:
`stock_sync_multi.py._score()` ya pondera el reparto de stock por reputación
real de la cuenta (`_REPUTATION_FACTOR`, para no seguir favoreciendo a una
cuenta en crisis como BLOWTECHNOLOGIES solo por ventas históricas), pero
`stock_concentrator.py.preview_concentration()` — la función hermana que
decide el GANADOR al concentrar stock manualmente desde Alertas de Stock —
no tenía esa ponderación: elegía ganador solo por `sold_30d`/`sold_total`
crudos, sin importar si esa cuenta está en amarillo/rojo hoy.

Fix: `enrich_with_sales()` ahora también lee `seller_reputation.level_id`
por cuenta (mismo `client.get_user_info()` que ya usa `stock_sync_multi.py`)
y calcula `rep_factor` con el mismo diccionario `_REPUTATION_FACTOR`
(importado directamente, una sola fuente de verdad). `preview_concentration()`
pondera `sold_30d_weighted`/`sold_total_weighted` = ventas × rep_factor para
elegir ganador. El mensaje y el diálogo de confirmación en
`products_stock_issues.html` muestran `rep=0.5x` etc. cuando el factor no es
1.0, para que quede visible por qué se eligió esa cuenta.

Verificado en vivo (SKU real SNWM000001, servidor local): BLOWTECHNOLOGIES
(FULL, reputación amarilla `3_yellow`) aparece con `rep_factor: 0.5` en la
respuesta — 48 ventas históricas pesan como 28.5, evitando que gane la
concentración solo por historial viejo de una cuenta hoy deteriorada.

---

## 2026-08-12 (cont. 7) — FIX DEFINITIVO: reclamos abiertos viejos invisibles (auditoría "mismo patrón en otras secciones")

Tras el fix de Mensajes, Jovan pidió auditar si el mismo patrón ("ventana
de fecha en vez de estado real de la plataforma") se repetía en otras
secciones. Se lanzaron 2 especialistas en paralelo: inventario completo
de mecanismos "hechos a mano" en el código (loops de background, scans
por ventana) + revisión de qué endpoints dedicados ofrecen ML/Amazon para
cada sección. Resultado: **Reclamos tenía exactamente el mismo bug.**

`fetch_all_claims()` (`app/services/meli_client.py`) ya usaba `status`
como filtro primario (correcto, v2 de Claims API), pero seguía cortando
la paginación de `status="opened"` en cuanto encontraba un claim más
viejo que `date_from` — un reclamo puede seguir genuinamente abierto
(mediación/recontact) mucho más tiempo que cualquier ventana razonable.
Confirmado en vivo ANTES de tocar código: reclamo real id `5143152874`
(APANTALLATEMX), status `opened`, stage `recontact`, `date_created`
2022-08-24 — **4 años de antigüedad, genuinamente abierto ahora mismo**,
invisible por completo para el sistema.

Fix (commit `c494438`): "opened" nunca se corta por fecha — un reclamo
abierto es accionable sin importar qué tan viejo sea. "closed" sigue
respetando `date_from`/`date_to` (acotar histórico de cerrados sí tiene
sentido, no son accionables). Verificado en vivo tras el fix: el reclamo
de 2022 aparece, los 24 reclamos abiertos reales de la cuenta se traen
completos.

**Revisado y descartado en la misma auditoría (sin cambios necesarios)**:
- Preguntas ML: ya filtra por `status=UNANSWERED` desde el diseño
  original — sin ventana de fecha, sin bug.
- Devoluciones ML: dependen de Reclamos (no es un recurso independiente
  en la API de ML) — se benefician indirectamente del fix de arriba.
- Amazon Returns/Feedback: Amazon no ofrece nada mejor que reportes por
  lote (confirmado con documentación oficial 2026) — no es un endpoint
  que se nos haya pasado, es el límite real de la plataforma.
- Amazon Buyer Messages: mismo síntoma conocido ("se detiene sin error
  visible") ya documentado en `project_ml_amazon_messages_backup_polling.md`
  — no requirió acción nueva hoy.

Deploy Railway SUCCESS, verificado en producción.

---

## 2026-08-12 (cont. 6) — FIX DEFINITIVO: mensajes ML invisibles — reemplazado el esquema de ventanas de días por el endpoint real de ML

Jovan reportó (con capturas reales, mismas cuenta en ambas) un mensaje de
comprador visible en ML pero invisible en la app: APANTALLATEMX, pedido
#2000014378634069 (pack real), comprador preguntando por una TV que se
prende sola. Primero se hizo un parche rápido (ampliar la ventana de
`_ml_messages_new_orders_scan_loop` de 4 a 10 días, commit `c2dcad3`) —
Jovan lo rechazó explícitamente ("arregla el problema de raíz, dejate de
parchar") y tenía razón: ampliar un número solo mueve el hueco, no lo
cierra.

**Causa raíz real, confirmada en vivo antes de tocar código de nuevo:**
la orden tenía 7 días (creada 2026-08-05) y nunca había tenido mensajes
hasta hoy — cayó exactamente en el hueco estructural entre PARTE 1
(4→10 días) y PARTE 3 (180 días, pero 1 sola vez al día). Cualquier
combinación de ventanas fijas de fecha SIEMPRE va a tener un hueco así
en algún punto entre la ventana más rápida y la más lenta.

**Solución de raíz**: investigada la documentación oficial de ML +
confirmado en vivo contra las 4 cuentas reales — existe
`GET /messages/unread?tag=post_sale`, que lista TODAS las conversaciones
sin leer de la cuenta **sin importar la fecha de la orden**. Probado en
vivo: trajo exactamente el pack reportado, sin adivinar ninguna ventana.

Nuevo loop `_ml_messages_unread_poll_loop()` (commit `7cb126f`): cada
2 min, 1 llamada barata por cuenta, indexa cada pack sin leer al momento.
Encontrado en el camino y corregido antes de subir: el endpoint también
devuelve hilos donde la cuenta es la COMPRADORA (no la vendedora) —
verificado en vivo que sin filtrar por `sellers/{uid}` propio, 0/18, 1/7
y 2/4 packs fallaban al indexar (403/404, seller_id equivocado en la
URL); con el filtro, 100% de los packs propios se indexaron
correctamente en las 4 cuentas.

Las PARTES 1/2/3 (order-scan por ventana) se quedan activas como
respaldo secundario — completan `order_id` real y cubren el caso raro de
un mensaje marcado leído fuera de la app antes de que la app lo viera —
pero esta vía nueva es ahora la principal y no depende de la edad de la
orden en absoluto. Se indexó también, manualmente vía diag ya existente,
el pack puntual reportado, sin esperar el deploy.

Deploy Railway SUCCESS, verificado en producción tras el deploy.

---

## 2026-08-12 (cont. 5) — FIX: badge global de Salud nunca contaba mensajes pendientes

Jovan reportó (molesto, "de nuevo") un mensaje real de comprador (BLOW,
Miguel Ángel Plascencia, "le falta el conector") visible en ML pero no en
la app. Verificado en producción con diag dedicados ANTES de asumir nada:

- El mensaje SÍ estaba correctamente indexado — BLOW tiene 161 mensajes
  pendientes reales en nuestro sistema ahora mismo, confirmados en vivo
  contra ML (`live_conversation_status`, `live_last_message` coinciden
  exacto con lo que ML muestra).
- La segunda captura de Jovan tenía la cuenta **ML APANTALLATE**
  seleccionada en el dashboard, no BLOW — el tile "Mensajes: 0" de esa
  página es correcto PARA ESA cuenta (otra cuenta, sin relación con el
  mensaje reportado). No es un bug de datos.

Investigando de todos modos se encontró un bug real y separado:
**`base.html` línea ~109** — el numerito junto a "Salud" en el menú
superior (visible en CUALQUIER pestaña, sin entrar a Salud) solo sumaba
`questions + claims`, nunca `messages` — el mismo bug de nombres que ya
se había corregido en `dashboard.html` (franja de alertas) el 2026-08-08
nunca se replicó aquí. Aunque Jovan hubiera tenido la cuenta correcta
seleccionada, ese indicador global jamás habría avisado del mensaje.
Fix: una línea, sumar `d.messages || 0` también.

Deploy Railway SUCCESS.

---

## 2026-08-12 (cont. 4) — FIX: Deals ML mostraba falsos "deal activo" + margen mal calculado

Jovan reportó que Deals "no está funcionando de forma correcta" (sin
detalles) y pidió meter especialistas a verificar mejores prácticas
oficiales de ML. Se lanzaron 2 en paralelo: uno mapeó el código actual
(`app/main.py`), otro (marketplace-ads-strategist) investigó la Central
de Promociones oficial. Antes de tocar código se verificó EN VIVO contra
las 4 cuentas reales (`get_promotion_items`) para confirmar cada hallazgo
con datos reales, no solo teoría.

**BUG 1 (causa raíz real, confirmado con datos reales):** el ciclo de vida
de un ítem dentro de una promoción es `candidate → pending → started →
finished`. "candidate" = elegible pero nunca aceptado — sin descuento
corriendo (para DEAL/LIGHTNING ni siquiera trae `price`, solo un RANGO
`min/max/suggested_discounted_price` para elegir). El código marcaba
TODOS los ítems de una campaña "started" como deal activo sin mirar el
status de cada ítem — verificado: la misma campaña DEAL "started" trae
cientos de ítems "candidate" mezclados con los genuinamente activos.
Fix: filtrar `item.get("status") == "started"` antes de construir el
mapa de promos (`app/main.py`), y quitar "pending" del allowlist en
`_enrich_with_promotions` (confirmado con datos reales: un LIGHTNING
"pending" es un deal programado para el día siguiente, no corriendo hoy).

**BUG 2:** `_margen_pct`/`_ganancia_est` se calculaban sobre `price`
(precio de lista) en vez de `_promo_deal_price` (precio real del deal) —
mismo tipo de bug que ya se corrigió en Ads (2026-08-11), aquí nunca se
tocó. Consecuencia: la alerta "deal con margen negativo" nunca disparaba
para un deal que de verdad pierde dinero. Verificado en local contra
datos reales: pasó de no disparar (roto) a detectar 131 casos reales tras
el fix. De paso se corrigió `_margen_real_pct` (aportación de ML,
`_meli_contribution_mxn`) para la misma consistencia — ya existía
calculado pero nunca se mostraba en ningún lado (código a medias, sin
consumidor); se deja correcto por si se conecta a futuro.

**BUG 3 (parcial):** de los 12 tipos de promoción de ML, faltaban 3:
`PRICE_MATCHING`, `UNHEALTHY_STOCK`, `VOLUME`. Verificado con datos
reales que PRICE_MATCHING y UNHEALTHY_STOCK sí tienen precio comparable
(ML los cofinancia/calcula igual que SMART) — agregados a `_auto_types` y
al mapa de colores. `VOLUME` y `SELLER_COUPON_CAMPAIGN` NO tienen un
campo de precio simple (son por cantidad/cupón, confirmado con datos
reales: `buy_quantity`/`discount_percentage` y `fixed_amount`) — se
excluyen explícitamente del flujo de precio único en vez de generar filas
con precio en blanco.

**Descartado tras verificar:** el límite de descuento máximo (ML subió de
70% a 80%) — el código ya decía "max 80%" (`products_deals.html:1025`),
no había nada que corregir ahí.

Deploy Railway SUCCESS, verificado en local contra datos reales de las 4
cuentas antes de subir.

---

## 2026-08-12 (cont. 3) — FIX DEFINITIVO: Stock tab colgado 7+ min — Fase D del rediseño bm_sku_master

Jovan reportó (molesto, con screenshot) el Stock tab colgado en "Calculando
stock en background... 450s" para AUTOBOT MEX. En vivo se confirmó que no
era lento — estaba genuinamente atorado en 0/1552 SKUs. Se reinició el
servicio para desatorarlo de inmediato, y después Jovan pidió rediseñar de
fondo todo el flujo BM→alertas (propuso un flujo de 4 pasos él mismo).

**Investigación con 3 especialistas en paralelo** (mapeo de arquitectura +
viabilidad técnica BM + validación de lógica de negocio) confirmó algo
importante: ~70-80% de lo propuesto por Jovan YA estaba construido —
`bm_sku_master` (maestro catálogo+stock, Fase B/C del rediseño de
2026-08-10/11) corría en producción **en paralelo, sin conectarse nunca a
las alertas reales**. Ese "cutover" pendiente (Fase D) es la causa raíz
real del colgón: `_do_prewarm()` seguía esperando (await) inline hasta 5
fetches bulk secuenciales a BM (90-150s c/u: GR+LOC47+LOC68+LOC-TJ+ALL)
antes de poder calcular una sola alerta — si BM está lento, ese `await`
se lleva al usuario con él.

**Fix implementado** (`app/main.py`, commit `86e55b6`):
1. Nueva `_bm_map_from_master()` — las 8 alertas de Stock (restock,
   oversell_risk, activate, critical, full_no_stock, imbalanced, stagnant,
   price_risk) ahora leen `bm_sku_master` (lectura local pura, JAMÁS llama
   a BM), reusando `get_bm_master_rows_for_skus()` que ya existía sin
   consumidor real. Solo confía en filas `verified=1` — mismo criterio
   "mejor vacío que datos stale" que ya usaba el pipeline viejo.
2. `_bm_bulk_ok()` ahora se basa en `bm_map` (ya filtrado por verified) en
   vez de releer `_bm_stock_cache` directamente — necesario porque ese
   cache ya no se llena de forma sincrónica con el cálculo de alertas.
3. El refresco de los caches bulk de BM (que `_bm_master_sync_loop`
   consume cada ~2 min) sigue disparándose desde `_do_prewarm()`, pero
   ahora `asyncio.create_task()` (fire-and-forget) con guard
   `_bm_bulk_refresh_running` — si BM está lento, corre en background sin
   que ningún usuario lo espere.
4. Circuit breaker en la cadena de bulk fetches: si el fetch fresco de GR
   ya falló este ciclo, se saltan los intentos frescos de LOC47/68/TJ/ALL
   en vez de intentarlos los 4 de todos modos (~90+90+90+150+150s peor
   caso = exactamente el patrón del incidente).
5. `/api/catalog/status` decía "domingo 9pm" (texto viejo) pero el sync
   real corre a diario (3am MTY) desde hace tiempo — confirmado con Jovan
   que diario está bien, solo se corrigió el texto/cálculo.

**Descartado tras investigar más a fondo**: el "delta inmediato" para SKU
nuevo no visto en catálogo (parte del plan original) no hizo falta
construirlo aparte — `get_reconciliation_priority_skus()` (ya existente)
prioriza "nunca visto en bm_sku_master" como máxima prioridad, y el ciclo
de `_bm_master_sync_loop` corre cada ~2 min (no semanal) — el hueco que
preocupaba ya estaba cerrado por diseño existente, agregar algo nuevo solo
habría reintroducido riesgo de llamar a BM en vivo desde las alertas.

Verificado antes de subir: local con BM real (login OK, bulk GR/LOC47/LOC68
OK, bm_master_sync verificó 1616 SKUs, Stock tab respondió en ~1.2s en vez
de colgarse) y contra producción (`/api/diag/bm-master-compare`: 100% de
coincidencias, 0 discrepancias, antes y después del deploy). Post-deploy:
las 4 cuentas ML con `stock_issues_cache` fresco (2-3 min de antigüedad)
sin que nadie tuviera que esperar un solo prewarm colgado.

---

## 2026-08-12 (cont. 2) — FIX: Mensajes mostraba hora UTC (ML) / hora del navegador (Amazon), no CDMX real

Jovan pidió confirmar qué horario se maneja en Mensajes -- "debemos trabajar
con los mismos horarios de ML que son de CDMX". Investigado con un agente
Explore antes de tocar nada: **ninguna de las dos plataformas anclaba la
hora mostrada a CDMX**.

- **ML**: `conv.date`/`msg.time` hacían slice literal del ISO8601 crudo que
  manda `message_date` de la API (viene en UTC, mismo patrón confirmado que
  `date_created` de órdenes) -- 6h adelantado, sin ninguna conversión. Caso
  real encontrado al verificar: un mensaje a las 04:49 UTC (22:49 CDMX del
  día anterior) se mostraba con la fecha del día siguiente -- error de
  fecha real, no solo de hora. Fix: `_ml_msg_dt_mx()` (nuevo,
  `app/main.py`), mismo ajuste -6h fijo que ya usa `_order_mx_date` para
  órdenes (México eliminó el horario de verano en la mayoría del país
  desde 2022, así que el offset es constante todo el año).
- **Amazon**: `_amzMsgsFmtDate()` llamaba `toLocaleDateString` sin
  `timeZone` -- usaba la zona configurada en la PC de quien abriera el
  dashboard, no una fija. Dos personas en equipos distintos podían ver
  horas distintas para el mismo mensaje. Fix: `timeZone:
  'America/Mexico_City'` explícito.
- El "hace Xh" de ambas plataformas ya calculaba bien (aritmética de
  timestamps con offset/epoch UTC, no manipulación de texto) -- no se tocó,
  no tenía el bug.

Verificado en local contra datos reales antes de subir: el caso de cruce
de medianoche (pack 2000014427962141, 04:49 UTC) pasó de mostrar "12 de
agosto" a "11 de agosto" correctamente, y las horas de cada mensaje dentro
del hilo (traídas en vivo de la API de ML) coinciden con el ajuste -6h.
Deploy Railway SUCCESS, `/api/diag/cache-health` 200 en producción.

---

## 2026-08-12 (cont.) — FEAT: pestaña "Seguimiento" en Mensajes (ML + Amazon)

Jovan pidió poder marcar un mensaje que YA se respondió pero al que le
falta enviar algo después (guía de devolución, foto, un dato que no se
tenía a la mano en el momento) — hoy esos casos se perdían entre los
"Resueltos". Investigado primero con un agente Explore para no romper el
mecanismo de filtrado ya existente (Pendientes/Todos) en ninguna de las
dos plataformas.

Nuevo estado `needs_followup`/`follow_up_note` en `ml_message_views`
(3 columnas nuevas, ALTER TABLE con DEFAULT — mismo patrón que
`ship_state_code`/`ship_zone`), **ortogonal** a `status`: un mensaje puede
estar `resolved` y `needs_followup=1` al mismo tiempo, no son excluyentes.
Como esa tabla ya se reusa con prefijos (`claim:` para reclamos, `amz:`
para Amazon), el mismo cambio de esquema y las mismas funciones de
`token_store.py` sirven para las 3 colas sin duplicar nada — solo se
expuso el botón en Mensajes ML y Mensajes Amazon (no en Reclamos, no se
pidió ahí).

Piezas nuevas:
- `token_store.set_message_followup()` + `get_message_index(only_followup=True)`
  — esta última con su propia query (JOIN directo, sin depender del LIMIT
  de paginación normal), porque un mensaje marcado para seguimiento suele
  estar ya resuelto y podría quedar fuera de la primera página como pasó
  antes con el bug de conversaciones reabiertas (2026-08-11).
- ML: botón "🔖 Seguimiento" (con nota) / "✅ Ya se envió" en cada tarjeta
  + pestaña nueva junto a Pendientes/Todos (`health_messages.html`,
  `health.html`), endpoint `POST /api/health/messages/{pack_id}/followup`.
- Amazon: mismo patrón en `amazon_dashboard.js`/`amazon_dashboard.html`,
  endpoint `POST /api/amazon/buyer-messages/followup`. Cuidado encontrado
  al diseñarlo: la bandeja Amazon normalmente solo trae `days=365` y
  filtra por `only_pending` — un hilo resuelto+marcado se hubiera quedado
  fuera si no se forzaba `full_history=True` en la vista Seguimiento
  (mismo riesgo que ML, resuelto igual).

Probado de punta a punta en local (marcar → aparece con la nota → "Ya se
envió" → desaparece) para ambas plataformas antes de subir. Deploy
Railway SUCCESS, verificado `/api/diag/cache-health` 200 en producción
tras el deploy.

---

## 2026-08-12 — OPERACION: rol RDT "Direct-to-Consumer Shipping" agregado a VektorClaude, pendiente aprobación de Amazon

Retomado el pendiente de RDT (dirección de envío Amazon para zonas/
transferencias, solo cubría ML hasta hoy). Jovan navegó Developer Central
paso a paso: el rol restringido ya no es un checkbox separado, quedó bajo
"Will you delegate access to PII..." → Yes → sub-checkbox "Direct-to-
Consumer Shipping", marcado en la app VektorClaude (cubre VECKTOR IMPORTS +
AUTOBOT AMZ MX, comparten client_id) + re-autorizado en Seller Central.

Se agregó `/api/diag/amazon-rdt-probe` (`app/main.py`, commit `a8c898b`,
Railway SUCCESS) para probar `createRestrictedDataToken` contra producción
sin depender de la copia local de `tokens.db`. Confirmado que el
`refresh_token` de VECKTOR rotó tras el reauth, pero la llamada real sigue
devolviendo `InvalidInput: Application does not have access to ...
[buyerInfo, shippingAddress]` — el rol quedó bien configurado pero Amazon
aún no lo aprueba/activa del lado del API (su propia doc dice que roles
restringidos pasan por revisión, no es instantáneo). Sin acción pendiente
de Jovan; re-probar más adelante con el mismo endpoint. Falta repetir el
mismo cambio en la app de ExclusiveBulbs cuando esto quede aprobado.

---

## 2026-08-11 (cont. 7) — Auditoría de Ads + margen real + fix endpoint muerto + motor de recomendaciones + bm_sku_master atascado resuelto

**Auditoría de Ads** (especialista marketplace-ads-strategist, 2 sesiones):
1. Sin acceso a métricas en vivo hoy (`/api/ads/*` requiere sesión de
   dashboard, tokens locales expirados) -- honesto, no se inventaron cifras.
2. Investigación de la documentación oficial de Mercado Ads: Product Ads es
   100% lectura pública; Brand Ads tiene doc de escritura pero protegida con
   contraseña; Display Ads es 100% manual, gestionado por el equipo
   comercial de ML, nunca self-serve.
3. **Hallazgo clave**: el código de escritura para Product Ads (pausar,
   presupuesto, asignar SKUs) YA EXISTE desde feb-2026
   (`meli_client.py:838-935`) pero está bloqueado porque la app "CLAUDE" no
   está `certified` por ML -- no es un gap de código, es un permiso de
   plataforma. Pendiente que Jovan revise la sección de certificación en el
   Devcenter (no se puede confirmar desde afuera si es el Developer Partner
   Program pesado o algo más ligero específico de Ads).

**3 mejoras aprobadas por Jovan, implementadas y verificadas contra datos reales de BLOWTECHNOLOGIES:**

1. **Ads cruzado con margen real** (`ads_performance_partial`, main.py):
   antes clasificaba "TOP/MEDIO/BAJO" solo por ROAS/ACOS genérico, sin ver
   si el SKU realmente deja utilidad. Ahora resuelve SKU real por item_id
   (cache local), cruza contra costo de catálogo BM (`_bm_cost_cache`,
   catálogo completo -- NO depende de stock actual como
   `_enrich_with_bm_product_info`, que solo cubre SKUs con stock en el bulk)
   y calcula margen neto real. Nuevos tiers QUEMA MARGEN/RIESGO/RENTABLE;
   TOP/MEDIO/BAJO quedan como fallback visible en el tooltip cuando no se
   pudo resolver SKU o costo. Verificado: margen real 56.7% en un item con
   ACOS 4.9% → correctamente RENTABLE.

2. **Fix endpoint muerto "Por Categoría"**: `GET /advertising/advertisers/
   {adv}/product_ads/items` descontinuado por ML desde el 26-feb-2026 (404)
   -- el `except Exception: break` lo tragaba en silencio, este tab mostraba
   "0 categorías" sin ningún error visible desde esa fecha (~6 meses).
   Migrado al endpoint que ya usan `get_ads_items`/`get_campaign_items`.
   Verificado: ahora devuelve categorías reales con $20K-$226K de gasto/
   ingresos en vez de vacío.

3. **Motor de recomendaciones** (solo lectura, no ejecuta nada): banner con
   los top 5 productos que más gasto queman por encima de su margen real,
   con botón de pausar por item (reusa el flujo existente, bloqueado hoy por
   la certificación de ML igual que el resto de escritura).

---

## 2026-08-11 (cont. 6) — FIX DEFINITIVO: "No leídos" de ML nunca bajaba aunque ya respondiéramos

Jovan reportó, molesto, un caso nuevo (pack 2000014458358269, BLOWTECHNOLOGIES,
comprador Adolfo Loeza) que ML marcaba "1 no leído" hace 50 min y la app
mostraba "0" -- exigió una solución definitiva, no más backfills manuales.

Investigando ESTE caso puntual se encontró que ya estaba correctamente
indexado (ya habíamos respondido, `last_message_from: seller`) -- no era un
problema de indexado tardío como los de hoy más temprano. La causa real,
mucho más de fondo: cada mensaje de ML trae `message_date.read` (null = no
leído por el vendedor). `get_message_thread()` SIEMPRE llama con
`mark_as_read=false` en TODO el código -- responder un mensaje vía API
**nunca** le dice a ML que ya se leyó. Verificado en vivo: forzar
`mark_as_read=true` cambió el campo de `null` a un timestamp real al
instante. Sin este cambio, "No leídos" de ML solo baja si alguien abre la
conversación directo en mercadolibre.com -- nunca al responder desde
ninguna app externa (la nuestra incluida, hasta ahora).

Fix: `get_message_thread()` acepta `mark_as_read` (default False, los loops
de fondo/diag no cambian). `_fetch_enriched_ml_conversations` (la función
que renderiza la pestaña Mensajes para un humano) ahora llama con
`mark_as_read=True` -- abrir Mensajes en la app ya tiene el mismo efecto que
abrirlo en el sitio de ML. Esto es la causa real detrás de buena parte del
patrón "ML muestra más que nosotros" de toda la sesión de hoy, no solo del
caso de Adolfo.

---

## 2026-08-11 (cont. 5) — FEAT: adjuntar fotos/guías de devolución al responder mensajes (ML + Amazon)

Jovan pidió poder mandar fotos o guías de devolución a compradores desde
Mensajes -- send_message (ML) solo mandaba texto. Investigando se encontró
que **Amazon ya lo tenía completo** (backend `send_reply`/`amazon_buyer_message_reply`
+ input de archivo en `amazon_dashboard.js` línea 3236) -- solo faltaba en ML.

**ML**: flujo de 2 pasos (ML lo requiere así, a diferencia de Amazon que es
un solo POST multipart por email):
1. Subir el archivo (`meli_client.upload_message_attachment` →
   `POST /messages/attachments?tag=post_sale&site_id=MLM`) -- verificado en
   vivo contra la API real: la respuesta es `{"id": "..."}`, NO `{"filename":
   ...}` como dice la documentación pública de ML.
2. Mandar ese id en `attachments=[...]` al enviar el mensaje
   (`send_message`, ahora acepta el parámetro).

Nuevo endpoint `/api/health/messages/{pack_id}/upload-attachment` + input de
archivo en `health_messages.html` (mismo patrón que Amazon). Verificado en
vivo el paso de subida (no le llega nada a ningún comprador); el paso de
envío con el adjunto ya referenciado no se probó contra un comprador real --
pendiente de que Jovan lo confirme en su primer uso.

---

## 2026-08-11 (cont. 4) — FIX: "quién tomó/resolvió/respondió" mostraba "?" desde 2026-08-09

Al probar la firma nueva de mensajes, Jovan reportó que su respuesta se veía
firmada como "? 12:16" en vez de su nombre. Causa: `user.get("sub") or
user.get("name") or "?"` en `main.py`/`health.py` (9 lugares) -- ninguna de
esas dos claves existe en el shape real de sesión (`user_store.get_session`
retorna `id/username/display_name/role/...`, del JWT con claims
`uid/username/dn/role/mcp/sec`). Bug foundational desde el fix de "Marcar
resuelto" del 2026-08-09 -- SIEMPRE caía a "?", afectando Tomar, Marcar
resuelto, Reabrir, reclamos, feedback, y ahora también la firma nueva.

Nunca se detectó en pruebas locales porque `make_jwt2.py` (la herramienta
oficial del proyecto para JWT local, ver CLAUDE.md regla #2) generaba un
token con `{"sub": ..., "role": ...}` que por casualidad coincidía con el
mismo bug -- las pruebas "pasaban" mostrando algo coherente sin ejercitar
el camino real. Se corrige `make_jwt2.py` también, para simular una sesión
real (`uid/username/dn/role/mcp/sec`) y que este tipo de bug sí se detecte
en pruebas locales futuras.

---

## 2026-08-11 (cont. 3) — FIX: conversaciones reabiertas invisibles + filtro Pendientes + FEAT firma por empleado

1. **`get_message_index()` no detectaba conversaciones reabiertas para
   ordenar la pagina** -- el ORDER BY solo miraba `status != 'resolved'`,
   sin comparar fechas. Una conversacion marcada resuelta que el comprador
   reabrio despues (escribio de nuevo) se enterraba entre miles de filas
   mas recientes -- el badge (que si compara fechas en Python) la contaba
   bien, pero la lista con "Pendientes" activo mostraba vacio. Caso real
   verificado: resuelta 4-ago, reabierta 10-ago (pack 2000013983536133,
   BLOWTECHNOLOGIES). Ahora el ORDER BY tambien compara
   `last_message_date > viewed_at` via SQL (`strftime`).

2. **"Todas las cuentas"/busqueda/limpiar busqueda no reaplicaban el
   filtro "Pendientes"** tras el swap de htmx -- mostraba TODOS los
   mensajes (respondidos o no) aunque el boton se viera "Pendientes"
   seleccionado (el template siempre lo renderiza asi por default, pero el
   filtro real es JS del lado del cliente que nunca se reinvocaba tras el
   swap). Se agrega `window._reapplyMsgFilter()` a los 3 flujos.

3. **FEAT: firma de quien respondio cada mensaje** (Jovan: "esto ya lo
   habia requerido..." -- pedido repetido, no implementado la primera vez).
   ML no distingue empleados, solo sabe que respondio "la cuenta" -- nueva
   tabla `ml_message_sent_log` registra quien envia cada mensaje desde la
   app (`send_message`, health.py); se muestra cruzando por texto exacto
   contra el hilo en vivo. Solo cubre envios hechos desde la app de aqui en
   adelante, no retroactivo ni mensajes mandados directo desde ML.

---

## 2026-08-11 (cont.) — FIX: UI de enviar mensaje no se actualizaba + busqueda de Mensajes rota + PARTE 3 automatizada

Continuacion de la sesion de mensajes ML de mas arriba, con casos nuevos que
reporto Jovan sobre la marcha:

1. **Enviar mensaje no actualizaba la platica ni el badge "Pendiente" en
   pantalla** (`app/templates/health.html` `sendChatMessage`): el backend ya
   guardaba y marcaba resuelta la conversacion (fix previo 2026-08-09), pero
   el usuario seguia viendo el estado de antes del envio hasta recargar la
   pagina completa. Se agrega la burbuja del mensaje enviado y se actualiza
   el badge del lado del cliente sin esperar un reload.

2. **Busqueda de "Mensajes" (por orden/comprador/producto) solo buscaba
   dentro de la pagina chica ya cargada** (20-50 filas mas pendientes/
   recientes), nunca contra el historico completo de la cuenta. Una
   conversacion vieja y ya respondida (ej. orden de 25 dias) nunca aparecia
   sin importar que el numero estuviera bien escrito. `get_message_index()`
   ahora acepta `q` y filtra pack_id/order_id/texto via SQL contra TODO el
   historico; comprador/producto (no viven en la tabla) siguen buscandose
   solo en la pagina normal, para no regresar ese caso puntual.

3. **Caso "orden vieja (meses) que nunca se indexo, de repente recibe un
   mensaje nuevo"** — confirmado con 2 casos reales del mismo dia (uno de
   ellos una orden de FEBRERO, 6 meses atras). Ni la PARTE 1 (ordenes
   creadas <4 dias) ni la PARTE 2 (packs YA indexados, <21 dias) de los
   loops automaticos de mensajes cubren este caso -- queda invisible
   indefinidamente hasta que alguien lo reporta. Se agrega:
   - `/api/diag/ml-force-index-pack` — indexa un pack puntual al momento
     (usado para resolver los 2 casos reales de hoy).
   - **PARTE 3** (`_ml_messages_wide_backfill_loop`) — barrido automatico
     lento, 1x/dia, ventana de 180 dias, las 4 cuentas. Decision de Jovan:
     "recomiendo algo automatizado" en vez de depender de reportes manuales.

**Nota aparte, NO es un bug**: se confirmo con datos reales que "No leídos"
de ML y nuestro "Pendiente" miden cosas distintas -- ML marca "no leído"
segun si alguien ABRIO esa notificacion dentro de su propio panel, no segun
si ya se respondio. Un caso real (Tania Melissa Olvera) ya tenia nuestra
respuesta correctamente registrada (`last_message_from: seller`) pero ML
seguia mostrandolo "no leído" porque nadie lo abrio del lado de ML. No hay
endpoint de ML para replicar ese marcado -- no se puede igualar 1:1.

---

## 2026-08-11 — FIX: mensajes ML de ordenes con reembolso parcial invisibles en el indice (las 4 cuentas)

Jovan reporto que ML Seller Central mostraba "4 no leidos" para BLOWTECHNOLOGIES
mientras la app mostraba "2". Investigando el caso concreto (Erick Jesus Maya,
pack 2000013808236145, orden real 2000017213436544) se confirmo contra la API
de ML en vivo que el mensaje existia y tenia actividad de hace <1h, pero
`fila_en_nuestro_indice_que_matchea_ese_numero` era `null` incluso tras un
backfill exhaustivo de 45 dias — la conversacion nunca habia entrado al
indice.

**Causa raiz**: `_ml_messages_scan_and_index` (compartida por
`_ml_messages_new_orders_scan_loop` y el backfill manual) usaba
`/orders/search/recent`. Ese endpoint omite ordenes con status
`partially_refunded` (y probablemente otros de reembolso/cancelacion) —
justo las que generan los mensajes post-venta mas sensibles (reclamos de
producto dañado/incompleto, devoluciones). El bug es estructural, existe
desde que se construyo este pipeline, no es una regresion de hoy.

**Fix**: cambiar a `/orders/search` (mismo endpoint que ya usan otras partes
del codigo para rangos de fecha, ej. planeacion). Verificado en local contra
el caso real antes de deployar.

**Efecto del backfill (45 dias, las 4 cuentas, tras el fix)** — cuantas
conversaciones "buyer + no resuelto" habia invisibles, y cuantas siguen
realmente activas hoy (no bloqueadas/movidas a Reclamos) segun chequeo en
vivo contra ML:
- BLOWTECHNOLOGIES: 42 candidatos nuevos → 12 activos reales, 30 ya bloqueados/en Reclamos
- AUTOBOT MEXICO: 10 candidatos → 1 activo real, 9 bloqueados
- LUTEMAMEXICO: 3 candidatos → 0 activos, 3 bloqueados
- APANTALLATEMX: 14 candidatos → 0 activos, 14 bloqueados

Es decir, buena parte del backlog historico recien descubierto ya estaba
resuelto/movido a Reclamos en ML — solo nunca se habia visto localmente. El
numero real de mensajes con reembolso que de verdad necesitaban atencion y
estaban invisibles era mucho menor (~13 entre las 4 cuentas), pero el hueco
en si es real y ahora esta cerrado hacia adelante (loop automatico de 10 min
ya usa el endpoint correcto).

Tambien se amplio `/api/diag/ml-pending-list?live_check=1` de 5 a 50 packs
para poder verificar backlogs grandes sin tener que llamarlo repetidas veces.

---

## 2026-08-09 — FIX: BM lento/cascada de reintentos, persistencia de stock_issues_cache, activacion sin historial, y borrado masivo inseguro removido

Sesion de fixes reactivos en produccion, cada uno con verificacion real
antes/despues del deploy (diag endpoints, unit tests, o ambos):

1. **`_bm_avail`/`_bm_avail_raw` congelados hasta el proximo prewarm**:
   SHIL000026 seguia mostrando 623 en "Activar" cuando la cache real
   (`_bm_stock_cache`) ya tenia 2 -- las 9 listas de alertas son una foto
   tomada por prewarm (~15-30min), sin refresco al momento de renderizar.
   Fix: `_refresh_bm_avail_live()` corre en cada request y ajusta a la
   baja contra la cache viva (nunca sube, para no reintroducir riesgo de
   sobreventa).
2. **Cascada de reintentos cuando BM esta lento**: el bulk de BM (5 tipos:
   GR/LOC47/LOC68/LOC-TJ/ALL) es UNA sola llamada compartida entre las 4
   cuentas en el caso normal (confirmado, era la intuicion correcta de
   Jovan) -- pero si esa llamada fallaba, cada cuenta del loop de prewarm
   reintentaba la MISMA llamada fallida en cascada (hasta 5 x 270s x 4 =
   90 min en el peor caso). Fix: timeout 270s->90s + tracker dedicado de
   "fallo reciente" (independiente del health-check general, que puede
   resetear el contador compartido con su propio ping liviano) -- si un
   tipo de bulk fallo hace <3min, las siguientes cuentas van directo a
   stale sin reintentar.
3. **Persistencia en disco de `_stock_issues_cache`**: cada deploy borraba
   la caché de alertas en memoria, mostrando "Calculando..." aunque el
   sistema ya tuviera la info minutos antes. Hallazgo: la logica completa
   para esto YA ESTABA en main.py (carga al arrancar + guardado a disco al
   final de cada prewarm) pero llamaba a 2 funciones de token_store.py que
   nunca se implementaron -- fallaba en silencio siempre. Implementadas
   (`save_stock_issues_snapshot`/`load_all_stock_issues_snapshots` + tabla
   `stock_issues_snapshot`), verificado con test de round-trip real.
4. **Activacion sin historial de ventas escalaba mal con stock real**:
   SHIL000531 (400+ uds reales) se activaba con solo 1-2 unidades por el
   tope fijo `min(2, cap)`. Aprobado por Jovan (pregunta directa, 3
   opciones): ahora 10% del stock real, piso 2, techo 20. Bug propio
   detectado y corregido ANTES de deploy (la primera version del piso
   podia recomendar mas que el stock real si cap<2 -- riesgo de
   sobreventa que no llego a produccion).
5. **Borrado masivo inseguro en "SKU no en catalogo BM" -- removido**:
   Jovan verifico manualmente en BM que el primer SKU de la lista de 24
   candidatos a "Eliminar Todos" SI es un producto real (Hisense
   AW1422CW1W, catalogado con Brand/Model/UPC/RetailPrice), solo que nunca
   tuvo stock/movimientos -- nuestra tabla local de catalogo se alimenta
   solo del bulk de STOCK, que omite filas en 0. Confirmado con
   binmanager-specialist (2 rondas) que NINGUN mecanismo disponible hoy
   distingue "no existe" de "existe con 0 stock desde siempre". Se quito
   el boton "Eliminar Todos" (bulk, irreversible) y el "Eliminar"
   individual de la tabla desktop -- la version mobile de la misma
   seccion ya tenia el texto correcto ("Dar de alta en BinManager", sin
   accion destructiva). Hallazgo documentado permanentemente en
   `.claude/agents/binmanager-specialist.md` para que no se repita la
   misma verificacion equivocada (el especialista dio falso negativo con
   4 metodos distintos, todos basados en movimientos/inventario, no
   catalogo puro).

Ver memoria del proyecto: `project_no_bm_sku_deletion_risk.md`,
`project_business_logic_audit_2026-08-08.md` (sesion anterior, relacionada).

---

## 2026-08-08 — FIX/FEAT: auditoria de logica de negocio con 5 especialistas en paralelo -- 9 cambios reales desplegados

Jovan pidio, con toda la experiencia acumulada del proyecto, analizar si la
LOGICA de cada automatizacion tiene sentido de negocio (no solo si el
codigo corre bien) y usar a todos los especialistas de dominio. Se lanzaron
5 agentes en paralelo (mercadolibre-strategist, amazon-specialist,
binmanager-specialist, planning-specialist, uxui-designer), cada uno
auditando su dominio de forma independiente. Con "vamos por todos" se
implemento, verifico y desplego cada hallazgo real, en orden de prioridad.

**Implementados (9, todos con compile-check + smoke test local + deploy
Railway SUCCESS + verificacion en produccion antes de pasar al siguiente):**

1. **Reputacion en reparto de stock ML** (`stock_sync_multi._score()`):
   el algoritmo que decide que cuenta se queda con stock escaso solo
   pesaba precio_neto x velocidad_30d historica -- no sabia si una cuenta
   tiene reputacion deteriorada. Se agrego `rep_factor` (verde=1.0 /
   light_green=0.85 / amarillo=0.5 / naranja=0.25 / rojo=0.15) leido de
   `seller_reputation.level_id` real por cuenta, protege a cuentas en
   crisis (ej. BLOWTECHNOLOGIES) de seguir recibiendo stock preferente
   solo por historial de ventas previo a la crisis.
2. **Doble conservadurismo al activar/reabastecer**: `_rec_qty` (ventana
   14d) se pasaba a las funciones JS de sync, que volvian a multiplicar
   por 0.6 pensando que recibian el total crudo de BM -- respondia
   directamente la pregunta que Jovan hizo en la sesion ("por que al
   activar solo pones 1?"). Las 3 funciones JS dejaron de multiplicar;
   Jinja es ahora la unica fuente de la cantidad final en los 6
   call-sites de un solo item. Tambien: `_target_coverage_days_for_sku()`
   sube la ventana a 30 dias solo para SNTV* (TVs, lead time real de
   importacion 20-45d) -- no se adivinaron otros prefijos sin confirmar
   su taxonomia.
3. **FX hardcodeado unificado**: 2 formulas de precio sugerido usaban
   constantes fijas (18 y 17.5) en vez de `_last_fx_rate`/
   `_manual_fx_rate` (mismo patron ya usado en otros 3 lugares del
   archivo).
4. **Riesgo Sobreventa Amazon redefinido**: comparaba `fulfillable`
   (stock YA fisico en FBA, fuera de nuestro control) contra BM -- eso
   es el resultado NORMAL de enviar a FBA, no un riesgo; mientras que la
   sobreventa real y accionable (SKU FBM publicado con mas cantidad de
   la que BM tiene) nunca se detectaba. Nueva categoria informativa
   "Discrepancia BM vs FBA" (auditoria, no alerta) + Riesgo Sobreventa
   real ahora filtrado a FBM. Implementado por amazon-specialist,
   revisado y optimizado por mi (fusione 2 llamadas a BM en 1 antes de
   deploy -- BM esta limitado a 1 sesion a la vez).
5. **Listing Quality Score ML + precio vs competencia**: el score nunca
   consideraba precio -- un listing podia sacar 95/100 y estar 25% caro,
   quemando presupuesto de Ads sin que el score lo reflejara. Nuevo
   parametro `price_delta_pct`: en el loop bulk (`/needs-work`) se reusa
   el cache existente de top-20 (`_price_comp_cache`, sin llamadas
   nuevas a la API); en la vista de un solo item se consulta en vivo.
6. **FBA Reimbursements cruzado contra devoluciones**: el endpoint v1
   solo mostraba reembolsos YA aprobados -- el propio codigo documentaba
   el hueco (dinero real sin reclamar). Ahora cruza devoluciones vs
   reembolsos por (order_id, sku); cualquier devolucion >45 dias sin
   reembolso entra a "candidatas a revisar" (heuristico, no reclamos
   garantizados). Verificado con datos reales de VECKTOR.
7. **Dashboard reestructurado**: 3 herramientas de analisis (busqueda
   de producto, CVR funnel, precio vs competencia) competian
   visualmente con las alertas urgentes reales. Se agruparon en un
   `<details>` colapsado por default, reordenadas para quedar despues
   de "Ultimas Ventas". El fetch de CVR (el unico que se auto-disparaba)
   ahora espera a que se abra la seccion.

**Correcciones sobre los propios hallazgos de los especialistas (verificar
antes de actuar, no confiar ciegamente):**
- BinManager: el hallazgo #1 original ("migrar a `inventory_snapshot`")
  resulto NO viable -- un segundo especialista confirmo con evidencia
  cruzada que esa tabla es exclusiva del servidor MCP, sin ruta HTTP
  alcanzable por el codigo de produccion. Nuestro propio
  `bm_stock_snapshot` en SQLite YA es el equivalente casero. Accion real:
  pedirle a BinManager que expongan un endpoint HTTP (agregado a la
  solicitud pendiente en #support-binmanager).
- UX: el tab de nav "Inv.Global" que el especialista marco como "muerto"
  NO lo esta -- Jovan lo oculto a proposito el 2026-07-18 (ruta viva para
  admins via URL directa). No se toco.
- Amazon: el hallazgo de "SLA/antiguedad en mensajes" ya estaba
  implementado -- el especialista solo leyo `health_messages.html`
  (de ML) y no vio `amazon_dashboard.js`, donde vive un sistema de
  tiers de urgencia (urgent/warn/ok) mas completo que lo recomendado.

**Pendiente, decision explicita de Jovan (AskUserQuestion):** consolidar
los 6 mecanismos de notificacion independientes (badge de nav, campana
ML, banner BM stale, banner disco, banner global de errores, franja
Dashboard) en un centro unico -- Jovan eligio "solo lo seguro": no tocar
`base.html` (carga en cada pagina, sin poder verificar visualmente en
esta sesion). Documentado para retomar en sesion con navegador.

---

## 2026-08-08 — FIX: barrido final de fuentes de datos duplicadas/independientes (raiz del patron "datos viejos")

Jovan planteo la queja de fondo: "siempre me dices son datos viejos, algo
atorado, no actualice, etc y siempre son los mismos problemas. Busca de
raiz da una solucion definitiva." Correcto -- el patron real no era
"cache viejo" repetido, sino que existian VARIAS fuentes independientes
calculando lo mismo (o codigo muerto por colision de rutas) que se
desincronizaban entre si. Se aprobo un barrido final (Explore agent) mas
alla de Stock/Productos: Dashboard, Planning, Lanzador, stock_sync_multi,
Amazon. Encontro 2 bugs activos graves + varios de menor prioridad.

Corregidos hoy:

1. **`metrics.py` -- ImportError activo en cada request**: `_bm_stock`
   (renombrado a `_bm_stock_from_cache` en un cleanup anterior el mismo
   dia) nunca se actualizo en este import diferido -- `/api/metrics/
   low-stock-alerts` fallaba con 500 siempre. Corregido nombre + quitado
   un `await` sobrante (la funcion es sincrona).
2. **Colision de rutas `PUT /api/items/{item_id}/stock`**: FastAPI hace
   match por orden de registro (primero gana); `app/api/items.py` ganaba
   siempre y la version de `main.py` -- con proteccion BM-down,
   eviccion inmediata de cache de alertas y auto-reactivacion -- era
   100% codigo muerto. Se fusiono toda la logica en `items.py` (la que
   realmente corre) y se elimino el duplicado de `main.py`.
3. **`/api/dashboard/morning-briefing` leia una tabla de alertas legacy**
   (`get_all_sync_alerts()`, ciclo de 4h ya reemplazado) en vez de
   `_stock_issues_cache` (la fuente unificada real). Corregido.
4. **Colision de rutas `GET /api/health/counts`** + bug adicional
   encontrado en el camino: `app/api/health.py` gana la colision (misma
   causa que el punto 2), pero devuelve `open_claims/unanswered_questions/
   unread_messages` -- mientras que `base.html` (badge global de
   notificaciones) y `dashboard.html` (franja de alertas del Dashboard)
   fueron escritos contra los nombres del backend MUERTO de main.py
   (`claims/questions/messages`). Resultado: **ambos elementos de UI
   llevaban tiempo silenciosamente rotos** -- el badge nunca mostraba
   numero, la franja nunca mostraba alertas -- sin importar cual de las
   dos rutas "ganara". Se agregaron los nombres viejos como alias en la
   respuesta de `health.py` (sin quitar los nuevos) y se elimino el
   duplicado muerto de `main.py`.

Pendiente (menor prioridad, documentado para retomar):
- `_bm_amz_cache` en `amazon_products.py`: cache paralelo de stock BM del
  lado Amazon, independiente del `_bm_stock_cache` de ML (7 sitios de uso).
  Refactor mas grande, no urgente.
- `stock_sync_multi._fetch_bm_avail()` no revisa frescura de
  `_bm_bulk_gr_cache`/`_bm_bulk_all_cache` antes de llamar `get_bulk_stock()`
  en vivo.
- `planning_unlaunched` (`main.py`) sin TTL/cache en su scan en vivo de
  `BinManagerClient().get_global_inventory()`.
- `amazon_lanzar.py`: su gap-scan tiene su propio `get_bulk_stock()` sin
  cache -- aceptable, solo anotado.

Verificado: compile check limpio, smoke test local (rutas resuelven con
302 auth-redirect, sin excepciones en logs), deploy Railway SUCCESS,
`/api/diag/cache-health` 200 en produccion.

---

## 2026-08-08 — FIX: auditoria completa del sistema de alertas de stock (ML+Amazon) -- 7 bugs reales corregidos

Jovan reporto con screenshot una contradiccion obvia: el banner superior de
`/items` decia "Inventario en orden -- sin alertas activas" mientras la
cuadricula de KPIs de la MISMA pagina mostraba "Total Alertas: 1662". Pidio
analizar a fondo como funcionan TODAS las alertas (ML y Amazon) y mejorar
tanto la logica como la forma de trabajar con ellas.

Un agente Explore mapeo las 9 categorias de ML + 7 de Amazon completas
(definicion exacta, fuente de datos, donde se muestran, acciones
disponibles) y encontro 16 problemas reales, varios mas graves que la
contradiccion original. Corregidos hoy (por orden de impacto):

1. **Banner vs cuadricula (el original)**: `get_stock_counts()` exigia
   datos de <15min o caia a un fallback casi vacio; la cuadricula SI
   tolera datos viejos. Ademas solo conocia 5 de las 9 categorias reales.
   Se quito el filtro de frescura y se agregaron las 6 categorias
   faltantes (`activate`/`full_no_stock`/`imbalanced`/`stagnant`/
   `price_risk`/`no_bm_sku`) con sus badges correspondientes.
2. **"Total Alertas" de ML contaba doble**: `restock_count` ya incluye
   `activate_count` (`len(restock)+len(activate)`), y la formula lo
   volvia a sumar aparte -- inflaba el total real.
3. **Checkbox "Auto qty=0" no hacia absolutamente nada**: se guardaba en
   un dict en memoria (`_auto_zero_enabled`) que ningun otro codigo leia
   jamas (confirmado con grep global) -- prometia una proteccion
   automatica contra sobreventa que no existia en ningun punto del
   sistema. Eliminado por completo (checkbox + JS + 2 endpoints + dict).
4. **Badge "Sin publicar" hardcodeado a 0**: nunca podia aparecer aunque
   hubiera SKUs BM reales sin publicar (dato ya disponible en
   `bm_sku_gaps`, la misma tabla de `/bm/unlaunched` y `/productos`).
   Conectado.
5. **Tile "Criticos" de `/productos` mostraba "—" siempre**: el backend
   devolvia `criticos: None` con el comentario "computed client-side",
   pero el JS nunca lo calculaba -- roto desde que existe el tile. Ahora
   se calcula server-side con `_calc_score()` (capado a 500 items para
   catalogos grandes, marcado como parcial con un asterisco si aplica).
6. **2 sistemas paralelos de "Riesgo Sobreventa" en ML**: el panel
   persistente + badge de pestana leian de `token_store.get_sync_alerts()`
   (tabla escrita por un loop legacy cada 4h), completamente
   independiente del KPI del tab Stock (que sale de `_stock_issues_cache`,
   recalculado cada ~15min por el prewarm) -- dos numeros distintos para
   el mismo concepto en la misma pantalla. Ambos endpoints (`/api/sync/
   alerts`, `/api/sync/alerts-count`) ahora leen la misma fuente que el
   KPI. Las acciones (Qty 0 individual y bulk) siguen igual, llaman al
   mismo endpoint real sin importar el origen de la lista.
7. **Amazon: mismo bug de conteo doble, peor**: `total_alertas` sumaba 7
   listas donde 3 (`riesgo_sobreventa`/`stock_critico`/`estancado`) no
   tienen exclusion mutua entre si ni con las otras 4 -- un SKU podia
   contarse hasta 4 veces. Corregido a SKUs unicos.
8. **Amazon: sobreventa con BM=0 era invisible**: `riesgo_sobreventa`
   exigia `bm_avail>0`, asi que el caso mas grave (Amazon vendible, BM
   literalmente en 0) no caia en ninguna de las 7 categorias. Corregido,
   marcado visualmente como "SIN RESPALDO BM".
9. **Amazon: ninguna alerta tenia accion real**, solo enlace a Seller
   Central. Agregado boton real "Sync stock BM" (escribe cantidad real
   via `fulfillment-action`/`set_qty`) en las 4 categorias FBM
   (Reabastecer/Riesgo Sobreventa/Stock Bajo/Restock Urgente) -- nunca
   visible en SKUs FBA puro, donde Amazon controla el stock directamente.

Implementado con `amazon-specialist` (agente) para el punto 7-9,
verificado por Jovan via 4 decisiones explicitas (dedup por SKU unico,
boton en las 4 categorias FBM, arreglar el hueco de BM=0 ya, sin
acciones "avanzadas" de conversion FBA/FBM por ahora).

**Pendiente, menor impacto** (documentado, no bloqueante): "SKU no en
catalogo BM" tiene 2 implementaciones que no coinciden (`no_bm_sku` del
tab Stock vs. `/productos/sin-bm`); banner "actualizando en background"
le miente a operadores (el refresh solo lo dispara un admin); texto de
`/stock-sync` dice "ciclo cada 5 min" cuando no existe ningun ciclo
automatico (decision explicita de Jovan, no es un bug); solapamiento
residual entre `critical`/`stagnant`/`price_risk`/`imbalanced` de ML
(no son mutuamente excluyentes); tile "Criticos" de `/productos` choca
de nombre con "Stock Critico" del tab Stock (conceptos distintos).

---

## 2026-08-07 — FEAT+FIX: correccion algoritmica de sobreventa cross-cuenta (ML+Amazon) -- de 724 a 56 SKUs, 3 bugs reales encontrados en el camino

Continuacion directa del fix de avail_total en TVs (entrada siguiente):
Jovan pidio confirmar que las demas alertas de Productos no tuvieran el
mismo problema. Se construyo `/api/diag/oversell-exposure-audit`
(comparacion de solo lectura: SUM(available_quantity) de TODOS los
listings activos ML+Amazon, todas las cuentas, vs. stock real de
`bm_sku_master`) y arrojo **724 SKUs** con la suma publicada excediendo
el stock real -- riesgo de sobreventa vigente, no historico (`synced_at`
de minutos, no datos obsoletos).

Causa raiz: `stock_sync_multi` (el reparto proporcional de stock entre
cuentas) calcula la cuota de cada cuenta de forma INDEPENDIENTE, sin un
tope global cross-cuenta -- si el mismo SKU esta publicado en varias
cuentas (y/o con publicaciones duplicadas dentro de una misma cuenta,
encontrado real: BLOWTECHNOLOGIES tenia 5 publicaciones para el mismo
SKU), la suma total puede exceder el stock real sin que nada lo
detecte, porque nada revisa el total agregado, solo cada cuenta por su
cuenta.

**Correccion construida** (`_run_oversell_correction`, nueva tabla
`oversell_correction_log` para auditoria completa): para cada SKU
sobre-expuesto, reparte PROPORCIONALMENTE el stock real de BM entre las
publicaciones editables (nunca sube, nunca pausa, nunca toca FULL/ML ni
FBA/Amazon -- esas cantidades se restan del presupuesto como
"bloqueadas", no se ignoran). `dry_run=true` por default, requiere
`confirm=true` para escribir de verdad. Corre en background con
progreso consultable via `/api/diag/oversell-correction-status`.

**3 bugs reales encontrados y corregidos ANTES de escalar a mas SKUs**
(cada uno via prueba real pequeña -> verificacion en vivo -> fix ->
prueba de nuevo, nunca se escalo sin validar primero):
1. `update_item_stock()` en items con variaciones pone la MISMA
   cantidad en CADA variacion (fallback documentado de MeLi cuando
   rechaza `available_quantity` a nivel item) -- eso INFLABA el total
   en vez de reducirlo (`MLM2890450220`: se pidio 26, el total quedo en
   134 con 6 variaciones). Fix: detectar variaciones ANTES de escribir
   y aplicar el mismo factor de escala a la cantidad ACTUAL de cada
   variacion (preserva su reparto relativo), via
   `update_variation_stocks_directly()`.
2. SKUs numericos tipo "8517331" (catalogo Amazon-only, sin equivalente
   real en BM) tenian `bm_sku_master.stock_updated_at=0` -- un
   placeholder de "nunca verificado", no "BM confirmo 0". Sin excluirlos
   se habria reducido inventario Amazon genuino pensando que el
   presupuesto real era 0. Fix: excluir `stock_updated_at=0` de
   auditoria y correccion.
3. Desfase de sincronizacion: la correccion escribe directo a las APIs
   reales de ML/Amazon, pero `ml_listings`/`amazon_listings` (las
   copias locales que lee la auditoria) solo se refrescan con el sync
   periodico de la app -- se agrego `/api/diag/force-qty-sync` para
   forzarlo sin esperar el loop.

**Resultado final, verificado en vivo en cada paso**: 724 -> 56 SKUs
con exposicion (92% resuelto), ~2,500 correcciones reales escritas en 8
lotes, 0 errores silenciosos (todo error fue un rechazo explicito y
seguro de la plataforma, nunca un dato corrupto). De paso, tambien se
corrigio manualmente el caso original que disparo todo esto
(SNTV007240/BLOWTECHNOLOGIES: reactivado + 4 publicaciones duplicadas
puestas en 0 para que el total volviera a coincidir con BM).

**Los 56 restantes NO son trabajo pendiente sin resolver** -- son un
limite arquitectonico real, confirmado con `logistic_type` de la API de
ML (no solo inferencia): ~20 son inventario **FULL** (Mercado Envios
Full gestiona esa cantidad, nuestra API correctamente se niega a
escribirla), el resto son brechas triviales (1-10 unidades) o
problemas puntuales de sync de catalogo (SKUs Amazon no encontrados en
su marketplace). Ver `.claude/memory/project_oversell_cross_account.md`
para el detalle completo y la lista de SKUs FULL pendientes de
verificacion fisica/logistica (Jovan, no es un fix de codigo).

Diag endpoints nuevos (todos gated por `_DIAG_TOKEN`): `ml-item-status`
(lectura, status/sub_status/variaciones/logistic_type real de un item),
`ml-item-variations-fix` (escritura puntual por variacion),
`oversell-exposure-audit`, `oversell-correction-run`/`-status`,
`force-qty-sync`.

**Fase 2 (Lanzador) y Fase 3 (stock_sync_multi) del plan de consolidacion
de arquitectura BM, cerradas el mismo dia:**

- **Fase 2**: `_bm_fetch_all_skus_with_stock()` en `lanzar.py` descartaba
  `AvailableQTY`/`Reserve` del row crudo de BM aunque el endpoint los
  trajera -- `_bm_qty()` siempre usaba `TotalQty` (bruto, incluye
  reservado) para gaps/priority_score/precio sugerido, sobre-estimando
  el stock vendible real. Corregido: se captura `AvailableQTY`/`Reserve`
  y se prefiere sobre `TotalQty` (con `is not None`, no OR-chaining, para
  no tratar un 0 genuino como "ausente").
- **Fase 3 -- hallazgo importante**: al leer `stock_sync_multi.py` para
  "unificarlo" con el resto, resultó que el algoritmo correcto de reparto
  YA EXISTE (`_plan()`: concentra en la cuenta ganadora si `bm_avail` está
  bajo el umbral dinámico, divide equitativamente si hay abundante,
  protección nocturna, detección de canibalización) -- **mejor** que la
  corrección construida hoy. El motivo real de los 724 SKUs oversold: el
  loop automático de 5 min está deshabilitado a propósito desde abril
  2026 (commit `ddb2552`, decisión explícita de Jovan, coincide con la
  regla de CLAUDE.md "sync automático que escribe en ML está prohibido")
  -- solo corre manual vía "Sync ahora". Nadie lo disparaba seguido, así
  que el stock real y lo publicado se desincronizaban con el tiempo.
  **No se reactivó el loop automático** (violaría esa regla explícita) --
  en su lugar, chequeo periódico de SOLO LECTURA (1h) que alimenta un
  banner en `/stock-sync` avisando cuándo conviene dar clic a "Sync
  ahora". Cierra el hueco de detección sin automatizar la escritura.

**Cierre final del mismo día — los ~20 SKUs "FULL" eran falsa alarma:**
Jovan preguntó por qué no verificábamos el stock Full nosotros mismos
por API en vez de pedirle que lo revisara a mano. Se delegó al agente
`mercadolibre-strategist`, que encontró y probó en vivo
`GET /inventories/{inventory_id}/stock/fulfillment` contra
BLOWTECHNOLOGIES real. Resultado: el `available_quantity` que ML
muestra en un item FULL YA ES su stock físico real (`not_available_quantity`
≈0 en todos los casos probados) -- no hay unidades ocultas. BM deja de
contar esas unidades en cuanto se envían a la bodega Full; comparar
`bm_avail` contra lo publicado en FULL siempre daba falsa sobreventa,
nunca fue sobreventa real. Fix: `is_full=1` (ML) / `can_update=0`
(Amazon FBA) ahora se EXCLUYEN por completo de la auditoría y de la
corrección (antes se restaban del presupuesto -- conservador pero de
más, nunca causó riesgo real). Resultado: **56 → 15 SKUs**, ninguno de
los TVs FULL de antes aparece ya. Nuevo método permanente:
`client.get_fulfillment_stock()` + diag `/api/diag/ml-fulfillment-stock`.

---

## 2026-08-07 — FIX DEFINITIVO: avail_total de TVs (SNTV*) se recalculaba en una tarea redundante y podía quedar mal con apariencia fresca

Jovan reportó SNTV007472 mostrando "BM Disp.: 10" en la app cuando BM (su propia UI) mostraba Reserve 1, Available 2 -- y exigió una solución definitiva, no un parche puntual, bajo la premisa "si falla en 1 falla en todas".

Causa raíz: `_fetch_tv_wh_breakdown()` corre 180s después del prewarm principal SOLO para calcular el desglose CDMX/MTY de televisiones, con su propio fetch independiente por ubicación (loc47/loc68/locTJ, condición ALL). Pero ese mismo bloque también usaba la suma de ese fetch para **sobreescribir** `avail_total` -- pisando el valor que el prewarm principal ya había fijado correctamente a T+0 desde `_bm_bulk_all_cache` (la misma fuente única que usa cualquier otro SKU, con su propio TTL/retry/cap de staleness ya probado). Si el fetch independiente de la tarea TV fallaba, sus ramas de fallback reusaban cache vieja (potencialmente de horas) sin checar edad, y aun así la entrada se re-timestampeaba como "recién verificada" -- quedaba con apariencia fresca pero dato incorrecto. Como esta ruta solo existe para SKUs SNTV*, el resto del catálogo (que siempre usó la fuente única) nunca mostró este síntoma -- de ahí que fallara sistemáticamente en TVs y en nada más.

Fix: `_fetch_tv_wh_breakdown()` ya NO toca `avail_total`. Solo calcula el desglose cdmx/mty/tj (para UI y Transferencias Sugeridas) y lo prorratea contra el `avail_total` ya confiable -- el mismo patrón que ya usan los SKUs no-SNTV en la segunda pasada del prewarm principal. Una sola fuente de verdad para `avail_total` en todos los tipos de SKU, sin excepción para TVs. Verificado en producción tras el deploy: SNTV007472 quedó estable en `avail_total: 2, reserved_total: 1`, exactamente igual a BM.

---

## 2026-08-06 — FIX: pendientes viejos enterrados fuera de la primera página, invisibles aunque el KPI los contara

Con el filtro de fecha ya ignorado (entrada siguiente), Jovan seguía sin ver
las 4 pendientes reales. Causa: `get_message_index()` ordenaba únicamente por
`last_message_date DESC` -- si hubo 20+ conversaciones más recientes desde el
último mensaje pendiente (posible fácilmente con mensajes de hace 1-2 semanas
en una cuenta activa), esas pendientes quedaban en la página 2, 3, etc.,
nunca visibles en la carga default (offset=0, limit=20) aunque el KPI (que
no pagina) sí las contara.

Fix: la query ahora hace LEFT JOIN contra `ml_message_views` y ordena
`CASE WHEN pendiente THEN 0 ELSE 1 END, last_message_date DESC` -- pendientes
reales (comprador + no resuelto) siempre primero, sin importar antigüedad,
así siempre caen dentro de la primera página.

---

## 2026-08-06 — FIX: paginación/filtros de Amazon Productos > Sin Publicar recargaban toda la página y volvían a Resumen

Jovan reportó: darle clic a "página 2" en Inactivos lo mandaba de vuelta al tab
Resumen en vez de avanzar de página. Causa: todos los links de paginación y
filtros de ese tab (Suprimidos, Inactivos, Con Stock/Sin Stock, filtro de días
y paginación de Candidatos a Eliminar) eran `<a href="?...">` planos -- una
navegación de página COMPLETA del navegador a `/amazon/products?...`, en vez
de una recarga AJAX del panel. Esa navegación completa vuelve a cargar el
shell de la página desde cero, que por default muestra Resumen e ignora el
query param de paginación.

El tab Inventario ya tenía el patrón correcto (`_loadInvTab`, fetch + swap de
`#amz-prod-tab-content` sin recargar la página) -- Sin Publicar simplemente
nunca lo adoptó. Fix: nueva función `window._loadSinPublicarTab()` (mismo
patrón que `_loadInvTab`) y los 12 enlaces del partial convertidos de
`href="?..."` a `onclick="return window._loadSinPublicarTab('...')"`.

---

## 2026-08-06 — FIX: "Stock BM" en Amazon Productos > Inactivos leía de Mercado Libre, no de BM

Jovan reportó SNHG000006 mostrando "Stock BM: 2040" en Amazon cuando el stock real
en BinManager es 0. Rastreado: eran 2 publicaciones de MERCADO LIBRE (no Amazon)
con `available_quantity=1020` cada una, congeladas desde antes del 2026-08-05
(cuando se excluyó Tijuana del vendible) y nunca vueltas a sincronizar -- se
corrigieron manualmente a 0 (ver `/api/diag/ml-item-stock-fix`, nuevo endpoint).

Pero el problema de fondo, señalado correctamente por Jovan, era de diseño: la
columna "Stock BM" del tab Inactivos (`amazon_products_sin_publicar()`) nunca
consultaba BinManager -- hacía `SUM(available_qty) FROM ml_listings WHERE
status='active'` y le llamaba "bm_stock" como atajo para evitar llamadas extra
a BM. Un listing de Amazon terminaba mostrando un número que depende de qué
tan actualizada esté una publicación de OTRA plataforma (Mercado Libre) — si
el SKU nunca se publicó en ML, o su publicación ML estaba desactualizada
(como en este caso), el dato mostrado no tenía relación real con BM.

Fix: las 3 queries de esa función ahora hacen `LEFT JOIN bm_sku_master` (el
maestro BM ya corregido para excluir Tijuana) en vez de `ml_listings` --
Amazon ya no depende de qué esté sincronizado en ML para mostrar su propio
stock BM. Confirmado que el resto de tabs de Amazon Productos (Inventario,
Stock, Resumen) ya usaban el camino correcto (`_fetch_base()`, consulta viva
a BM) -- el atajo por ml_listings estaba confinado solo a Inactivos.

---

## 2026-08-06 — FIX: KPI "Mensajes" marcaba 4 pero la lista salía vacía -- el filtro de fecha ocultaba pendientes reales

Con los dos fixes anteriores desplegados, Jovan reportó: KPI dice 4, pero la
lista de Pendientes no muestra nada. Confirmado con `/api/diag/ml-pending-list`:
las conversaciones pendientes reales de la cuenta eran todas de fechas
ANTERIORES al "desde" activo en el filtro global de Salud (ej. "desde
2026-07-31" mientras las pendientes eran del 24, 28, 30 de julio).

Causa: el KPI (`_count_ml_pending_excluding_blocked`) nunca aplicó filtro de
fecha (correcto, cuenta TODO lo pendiente sin importar antigüedad), pero la
LISTA (`_fetch_enriched_ml_conversations`, vía `health_messages_partial`)
sí respeta el filtro de fecha global de Salud -- diseñado para Reclamos/
Preguntas como reporte de un periodo, pero aplicado también sin querer a
Mensajes. Un mensaje sin responder de hace 2 semanas sigue siendo pendiente;
no debería desaparecer de la cola de acción solo por estar fuera de un
rango de fechas arbitrario.

Fix: `health_messages_partial()` y `health_messages_unified_partial()` ahora
ignoran el filtro de fecha global por completo -- Mensajes es una cola viva,
no un reporte de periodo, así que siempre muestra todo lo pendiente sin
importar antigüedad.

---

## 2026-08-06 — FIX: la pestaña Mensajes nunca aplicaba el filtro "Pendientes" al cargar

Con el conteo de KPI ya corregido (entrada siguiente), Jovan reportó que el número
bajó correctamente (7 → 4) pero la LISTA seguía mostrando conversaciones ya
marcadas como resueltas bajo "Pendientes".

Causa: el botón "Pendientes" se ve activo por una clase CSS fija en el template
del lado del servidor, pero `window.filterMessages('pending')` (la función que
realmente oculta las tarjetas con `data-needs-response="false"`) solo se
invocaba cuando el usuario le daba clic MANUALMENTE al botón. `loadTab()` en
`health.html` (usado para la carga inicial, refresh manual, cambio de pestaña
y aplicar/limpiar filtros de fecha) hacía `content.innerHTML = html` y nunca
llamaba a `filterMessages()` -- así que toda conversación (pendiente o
resuelta) quedaba visible hasta el primer clic manual en el filtro.

Fix: `loadTab()` ahora llama `window.filterMessages('pending')` automáticamente
después de cargar el HTML cuando la pestaña es 'messages'.

---

## 2026-08-06 — FIX (real, el número seguía en 37): tercera implementación duplicada del conteo de Mensajes

El fix anterior (mismo día, entrada siguiente) no bajó el número en la página real
-- seguía en 37 después de desplegar. Investigando por qué: encontré que el KPI
"Mensajes" que Jovan ve en `/health` NO viene de `/api/health/summary` (el que
arreglé) -- viene de `partials/health_summary.html`, renderizado por
`health_summary_partial()` en `app/main.py`, que tiene su PROPIA función
`_fetch_messages()` completamente separada y nunca tocada hasta ahora.

Esa función ni siquiera miraba "resuelto" -- solo contaba filas de
`ml_messages_index` con `last_message_from == 'buyer'`, sin JOIN a
`ml_message_views` ni chequeo de bloqueo. Por eso el número real (37) era
MAYOR que incluso el conteo sin filtrar de mi propio diagnóstico (30) -- ni
siquiera excluía las ya marcadas resueltas.

Tres implementaciones distintas del mismo KPI habían divergido con el tiempo:
`/api/health/summary` (JSON, `app/api/health.py`), `/api/health/counts`
(polling, mismo archivo), y esta tercera en `main.py` que resultó ser la que
realmente renderiza la página. Fix: `main.py` ahora importa y reutiliza
`_count_ml_pending_excluding_blocked()` de `app/api/health.py` (import de un
solo sentido, sin ciclo -- health.py no importa nada de main.py) en vez de
mantener una cuarta copia de la misma lógica.

---

## 2026-08-06 — FIX: KPI y lista de "Mensajes" contaban conversaciones ya movidas a Reclamos

Jovan preguntó, con razón: "en Mercado Libre no tenemos pendientes, ¿cómo es
posible que marque 37?". Investigado con `/api/diag/ml-pending-list?live_check=1`
comparando contra el thread real de ML: las conversaciones que contábamos como
"pendientes" tenían `conversation_status.status == "blocked"` (mediación, orden
cancelada) -- es decir, ML ya las movió fuera del canal de Mensajes hacia
Reclamos, que las cuenta aparte (KPI "Reclamos"). Nuestro conteo (basado solo en
`ml_messages_index`, que no sabe de bloqueos) las seguía contando como pendientes
de Mensajes, duplicando el conteo entre dos KPIs distintos.

No hay tabla local que trackee el estado de bloqueo en vivo, así que no se puede
filtrar con una query SQL pura sin consultar ML. Fix: `_count_ml_pending_excluding_blocked()`
en `app/api/health.py` -- toma los candidatos de la DB local (igual que antes)
y hace un chequeo en vivo acotado (semáforo de 10, caché de 10 min por pack_id)
para descartar los que ya están bloqueados. Se usa tanto en `/summary` como en
`/counts`. También se corrigió `_fetch_enriched_ml_conversations()` (la función
que arma la lista real de la pestaña Mensajes) para que `needs_response` excluya
bloqueadas de la misma forma -- ahí no hace falta chequeo extra, ya trae
`conversation_status` en el mismo fetch que usa para todo lo demás.

---

## 2026-08-06 — FIX: KPI "Mensajes" (badge/tab) no bajaba en vivo al marcar resuelto

Jovan marcó varias conversaciones como resueltas y el número "Mensajes: 37" (KPI
arriba y badge de la pestaña) se quedó exactamente igual.

Causa: ese número venía de `client.get_messages(limit=1).paging.total` -- el
conteo CRUDO de ML, que no tiene ninguna noción de nuestro estado interno de
"resuelto" (eso vive solo en nuestra tabla `ml_message_views`). Por diseño, ML
nunca iba a reflejar algo que solo nosotros trackeamos.

Fix: nueva función `token_store.count_ml_pending_messages(account_id)` -- cuenta
directo en SQLite (JOIN entre `ml_messages_index` y `ml_message_views`, sin
llamada a ML) con la misma lógica de reapertura que ya usa la lista real:
pendiente = último mensaje del comprador Y (no resuelto O el comprador escribió
después de la marca de resuelto). Reemplaza el conteo crudo tanto en
`/api/health/summary` (el KPI "Mensajes") como en `/api/health/counts` (badge
de notificaciones global) para que ambos sean consistentes entre sí y con la
lista de Mensajes.

---

## 2026-08-06 — FIX: "Marcar resuelto" tiraba "window._setMsgStatus is not a function" en consola

Jovan confirmó con la consola del navegador (F12) el error real: `Uncaught TypeError:
window._setMsgStatus is not a function at HTMLButtonElement.onclick`. Probé la función
del servidor directo (`token_store.update_message_view_status`) y funcionaba perfecto
-- confirmando que el problema era 100% del navegador, no del backend.

Causa: `partials/health_messages.html` definía `_setMsgStatus`, `_takeMsgConv`,
`filterMessages`, `setMsgScope`, `searchMessages`, `clearMsgSearch` dentro de su
PROPIO `<script>` embebido. Este partial se intercambia dentro de health.html vía
AJAX (htmx `innerHTML` swap) -- y un `<script>` embebido en HTML intercambiado así
no se re-ejecuta de forma confiable en cada swap. Funciones equivalentes que SÍ
funcionaban siempre (`sendChatMessage`, `respondClaim`) ya vivían en `health.html`
(la página persistente) precisamente por este motivo -- el patrón correcto ya
existía, solo que `health_messages.html` no lo seguía.

Fix: se movieron las 6 funciones a `health.html`. La única complicación:
`_msgBaseUrl()` dependía de `window._msgIsUnified`, una variable horneada por Jinja
en el partial (`{{ 'true' if unified else 'false' }}`) -- al vivir ahora en la
página persistente sin ese contexto, se cambió para leer `data-msg-unified` del DOM
(atributo agregado al contenedor superior del partial, que sí se re-renderiza en
cada swap aunque el script no se re-ejecute).

---

## 2026-08-06 — FIX: "Marcar resuelto" en mensajes ML no tenía ningún efecto persistente

Jovan reportó: el botón "Marcar resuelto" no parecía hacer nada — la conversación
debería desaparecer de Pendientes y guardarse en Histórico, y volver a Pendientes
solo si el comprador escribe de nuevo.

Causa: `needs_response` (el campo que decide si una conversación cuenta como
"pendiente", tanto para el badge visual como para el filtro "Pendientes") en
`_fetch_enriched_ml_conversations()` (`app/main.py`) se calculaba ÚNICAMENTE
de si el último mensaje era del comprador (`last_message_from == "buyer"`) —
sin mirar nunca el estado de "resuelto" guardado en `ml_message_views`. El
botón SÍ guardaba el resuelto en la base de datos correctamente y lo quitaba
de la vista al instante (animación JS), pero en cualquier recarga posterior
volvía a aparecer en Pendientes exactamente igual, porque el servidor nunca
consideraba ese estado al recalcular la lista.

Mismo patrón de bug que ya se había corregido para Amazon el mismo día (ver
entrada "hilos de mensajes Amazon marcados resuelto se quedaban ocultos para
siempre") — pero en la dirección inversa: ahí un resuelto se quedaba oculto
para siempre aunque el comprador respondiera; acá un resuelto nunca se
ocultaba porque el cálculo ni siquiera miraba el estado.

Fix: se trae `view_info` (misma tabla `ml_message_views`) ANTES de calcular
`needs_response`, replicando el patrón ya usado en Amazon —
`needs_response = last_from_buyer AND NOT (resuelto Y sin mensaje del
comprador posterior a la marca de resuelto)`. Un resuelto se reabre solo si
hay un mensaje nuevo del comprador después del timestamp de resuelto; si no,
se queda fuera de Pendientes de forma persistente (sigue visible en "Todos"
con el badge "✓ Resuelto").

---

## 2026-08-06 — FIX (real, cierre de la saga): "Unexpected exception parsing json string" al responder mensajes ML — "text" iba como objeto anidado, ML espera string plano

Cierre de la saga del día completo con este error, tras varios intentos previos
que ayudaron pero no eran la causa raíz. Camino completo:

1. Jovan probó una app de terceros (administrado.net) contra una conversación
   real (pack 2000014395529751, cuenta AUTOBOT) con el MISMO texto exacto que
   fallaba en nuestro sistema -- y ahí SÍ se envió. Eso descartó de raíz la
   hipótesis de bloqueo real de ML para esa conversación (la armada con el
   especialista de Mercado Libre vía "Reasons to communicate" / action_guide
   bloqueado -- resultó ser una señal no relacionada con el envío real).
2. Vía WebSearch se encontró el ejemplo oficial de ML del payload, que incluye
   "to" (user_id del comprador) -- campo que `send_message()` en
   `meli_client.py` JAMÁS enviaba. Se agregó, extrayendo el user_id del mismo
   thread que ya se obtiene para el chequeo de bloqueo (sin llamada extra).
   Ayudó pero NO resolvió el error en un caso nuevo (pack 2000014384279257,
   cuenta LUTEMA, conversación iniciada por el comprador).
3. Se probó castear `self.user_id`/buyer_id a int (ML los devuelve como
   entero en sus respuestas, nuestro cliente los maneja como string) --
   tampoco resolvió ese caso nuevo.
4. Se aisló con `/api/diag/ml-message-send-test` (nuevo endpoint que llama
   exactamente `send_message()` real y devuelve el error crudo de ML, no el
   ya envuelto) probando con un pack_id inventado: ML respondió un 404 limpio
   ("order_not_found"), NO el error genérico -- confirmando que el request
   sí llega a la lógica real de ML, descartando un problema de scope/permiso
   de la app a nivel general.
5. Comparando contra el ejemplo oficial de ML letra por letra: el payload
   real espera `"text": "string plano"`, NO `"text": {"plain": "..."}` como
   mandaba nuestro código desde siempre. Confirmado en vivo: con el objeto
   anidado fallaba el 100% de las veces (2 cuentas, conversaciones bloqueadas
   y no bloqueadas, con o sin "to"); cambiando a string plano funcionó de
   inmediato -- verificado enviando un mensaje real de prueba y luego la
   respuesta real y útil al comprador.

Explica por qué ninguna de las ~5 hipótesis de contenido probadas en días
anteriores (saltos de línea, acentos/UTF-8, ASCII puro, límite de caracteres,
bloqueo de conversación) encajaba con el patrón completo: nunca fue el
contenido ni el estado de la conversación, era la FORMA del payload.

Las causas reales encontradas antes en esta misma saga (bloqueo por mediación/
cancelación real, límite de 350 caracteres) siguen siendo válidas y se quedan
-- son restricciones reales de ML que aplican aparte, independientes de este
bug de formato. "to" + cast a int se quedan también, como buenas prácticas
correctas aunque no eran la causa.

---

## 2026-08-06 — FIX: cambiar de cuenta Amazon desde /dashboard se quedaba viendo el dashboard de ML

Jovan reportó: cambió de cuenta a "AUTOBOT AMZ MX" desde el selector, el nav sí cambió a
tema Amazon (tabs FBA & Stock, Sync Stock, etc.) pero el contenido debajo seguía siendo
el dashboard de ML con datos de APANTALLATEMX.

Causa: `/auth/switch-amazon` solo redirige a `/amazon` cuando la página actual está en
`_ML_ONLY_PATHS` (tabs SIN equivalente en Amazon, ej. Ads, Sync Stock). Pero "Dashboard"
SÍ tiene equivalente Amazon — solo que en una URL distinta (`/amazon?tab=dashboard`, no
`/dashboard`) — así que `_ML_ONLY_PATHS` no lo detectaba y el redirect se quedaba en la
misma URL `/dashboard`, que es una ruta 100% ML sin ninguna lógica de plataforma. Mismo
patrón afecta a Ventas, Productos, Salud y FBA (todas usan el dispatcher `/amazon?tab=`).

Fix: nuevo mapa `_ML_HREF_TO_AMZ_HREF` (ml_href → amz_href por tab) en `/auth/switch-amazon`
— si la URL actual mapea a una URL Amazon distinta, redirige ahí; si no tiene equivalente
en absoluto, cae al `/amazon` genérico como antes.

---

## 2026-08-06 — FIX: import roto impedía persistir el desglose MTY/CDMX de TVs en DB

Encontrado al verificar en producción el fix del deadlock de BM (ver entrada siguiente):
`[BM-TV-WH] Error persistiendo en DB: No module named 'app.db'`. `_fetch_tv_wh_breakdown()`
(`app/main.py:6626`) importaba `from app.db import token_store as _tv_ts` — módulo que no
existe (el correcto es `app.services.token_store`, ya importado a nivel de módulo como
`token_store`). El bare `except Exception` lo tragaba silenciosamente, así que este paso
llevaba fallando desde siempre sin que nadie lo notara: el desglose MTY/CDMX de TVs nunca
se guardaba en `bm_sku_master`, por lo que cada restart/deploy arrancaba TODOS los TVs en
frío (mty=0, cdmx=0, ts=0) hasta que el ciclo de prewarm los recalculaba desde cero otra
vez. Fix: usar el `token_store` ya importado, sin re-importar nada.

---

## 2026-08-06 — FIX: un solo request colgado a BM podía tumbar TODA la consulta de stock de la app

Jovan reportó que BinManager sí estaba respondiendo del lado de él (navegando directo),
mientras nuestro dashboard llevaba minutos sin poder refrescar ningún stock. Confirmado
con logs de Railway: `[BM-HEALTH] BM no responde — fallo #5`, `[BM-CACHE] BM DOWN — skip
fetch de 2487 SKUs`.

Causa raíz real: TODAS las llamadas a BM pasan por un único semáforo global
(`asyncio.Semaphore(1)` en `binmanager_client.py`, "solo 1 request activo a la vez").
Aunque cada llamada recibe un `timeout=` explícito para httpx, si una petición se
queda esperando sin que ese timeout llegue a dispararse (verificado: BM puede tardar
30s+ incluso sano en consultas pesadas, y bajo carga real esa cola puede crecer sin
límite), el semáforo nunca se libera — y CUALQUIER otra llamada a BM en toda la app,
sin relación alguna con la petición original, queda esperando detrás para siempre.
Reiniciar el servicio "arreglaba" el síntoma pero no la causa: podía volver a pasar
con cualquier lentitud futura de BM.

Fix definitivo (no reinicio): `_post()`/`_get()` en `binmanager_client.py` ahora
envuelven la llamada real a httpx con `asyncio.wait_for()` — una red de seguridad
independiente del timeout de httpx que garantiza, vía cancelación de asyncio, que el
semáforo se libere sin importar la causa del colgamiento. Ningún incidente futuro de
esta clase puede volver a bloquear el acceso a BM para toda la app.

También se corrigió `_check_bm_health()` (`app/main.py`): antes deducía "BM caído"
solo de si el bulk cache tenía filas, sin tocar BM — de ahí el falso "BM no responde"
mientras en realidad la petición estaba bloqueada detrás de otra. Ahora hace un ping
real y liviano (`GET /User/Index`, timeout 15s) protegido por el mismo hard-timeout.

---

## 2026-08-06 — FIX: BM Disp. mostraba stock alto atascado sin corregirse hacia abajo (riesgo de sobreventa en TVs)

Jovan reportó SNTV007472 (TCL 32"): dashboard mostraba "BM Disp. 18" pero BinManager
directo confirmaba Reserve=17, Available=1. Confirmado con `/api/diag/sku`: BM vivo y bulk
ya daban avail=1/reserve=17 correctamente (el fix de Tijuana del día anterior funcionaba
bien), pero la entrada en `_bm_stock_cache` seguía en avail_total=18, reserved_total=0.

Causa real (bug independiente, preexistente en el bloque de TVs que se tocó el día
anterior para excluir Tijuana, no causado por ese cambio): `_fetch_tv_wh_breakdown()`
(`app/main.py` ~6570) solo sobreescribía `avail_total` cuando el nuevo cálculo por
almacén (`_lsum`) era MAYOR al valor ya cacheado (`if _lsum > _avt`). Si ese valor quedaba
alto por cualquier motivo (dato viejo, glitch de BM), nunca se podía corregir hacia abajo
aunque llegaran reservas nuevas reales — un TV podía mostrar "disponible" stock que en
realidad ya estaba reservado para otras órdenes, con riesgo directo de sobreventa.

Fix: se quitó la condición — ahora `avail_total` siempre se actualiza al valor calculado
por almacén (`_cd["avail_total"] = _lsum`), en ambas direcciones. El guard existente que
aborta todo el bloque si las 3 consultas por ubicación fallan sigue protegiendo contra
pisar con ceros por una falla parcial de BM. Mitigación inmediata: se limpió la entrada de
caché de SNTV007472 vía `/api/diag/clear-bm-sku` para que muestre el dato correcto sin
esperar al próximo ciclo.

---

## 2026-08-05 — DECISION: stock de Tijuana excluido del "vendible online" — solo CDMX/MTY venden en línea

Jovan aclaró la regla de negocio: el producto en Tijuana es bueno y vendible, pero solo los
almacenes CDMX y Monterrey están autorizados operativamente para vender en línea. Tijuana
existe únicamente para reabastecer (transferir) a esos 2 almacenes, nunca para atender
demanda online directamente. Esto REVIERTE la regla documentada el 2026-07-21 que incluía
Tijuana (BM LocationIDs 45,69,43,42) como vendible tras una auditoría SKU por SKU — el dato
de esa auditoría seguía siendo correcto (stock físico real), lo que cambió es la regla de
qué almacenes pueden despachar venta en línea.

Set final de stock vendible: `47,62,68` (antes `47,62,68,45,69,43,42`).

Fix central: 3 defaults de `location_id` en `binmanager_client.py`
(`get_bulk_stock`, `get_stock_with_reserve`, `_query_bm_stock`) — la mayoría de call sites
de la app no pasan override, así que se corrigen automáticamente `/api/diag/sku`,
`bm_sku_master.available_qty`, `/api/diag/tv-stock-vs-sales`, el finder de "no lanzados",
y el prewarm de Stock/alertas.

Bug real encontrado aparte (no solo el default): el bloque de refresco de TVs (SNTV*) en
`app/main.py:6580` sumaba explícitamente `_cdmx + _mty + _tj` al sobreescribir
`avail_total` — fix independiente para que los TVs también dejen de contar TJ como vendible.

También: split de `_BM_LOC_IDS` en `amazon_products.py` (una constante para el desglose por
almacén que sigue incluyendo TJ, otra para el total vendible que ya no la incluye).
Verificado en `sku_inventory.py` que ya excluía TJ correctamente sin necesitar cambios.

Transferencias Sugeridas Entre Almacenes (tab Planeación) NO se tocó — sigue calculando
MTY/CDMX/TJ por separado con sus propios fetches dedicados, que es justo donde Tijuana
debe seguir contando.

Verificado localmente: SHLB000019 (stock físico solo en Tijuana) pasó de `avail=723` a
`avail=0` en `/api/diag/sku` tras el fix, confirmando que ya no se cuenta como vendible.

Ver `.claude/memory/project_bm_tijuana_exclusion.md` para el detalle completo por archivo.

---

## 2026-08-05 — FIX: hilos de mensajes Amazon marcados "resuelto" se quedaban ocultos para siempre

Jovan reportó el mensaje de Belum (Business Buyer, orden 701-1037142-0773817,
VECKTOR) pidiendo factura CFDI como ausente del dashboard. Al buscar la orden
directo (`/api/amazon/buyer-messages?order_id=...`) apareció el hilo completo
con 4 mensajes — SÍ estaba en el sistema. El problema real: Jorge respondió
con el link de facturación y el hilo quedó marcado "resuelto"; el cliente
escribió 2 veces más después ("me aparece error al facturar", "los valores
aparecen en cero") pero `needs_response` seguía en `false` porque la lógica
en `_fetch_amazon_threads_for_seller` (`app/main.py:19987-19988`) trataba
"resuelto" como un estado permanente, sin comparar contra la fecha del
mensaje más reciente del comprador.

Fix: un hilo resuelto se reabre automáticamente si hay un mensaje inbound
posterior al timestamp de la marca de resuelto (`viewed_at`). Verificado en
producción: Belum pasó a `needs_response: true`, y apareció un segundo caso
idéntico ya oculto (Krystal, orden 702-9351719-4658635) que se reabrió solo
con el mismo fix — confirmando que el bug afectaba más de un hilo, no solo
el reportado.

---

## 2026-08-05 — FIX: mensajes ML y Amazon seguían sin sincronizar pese a webhook/loop activos (mecanismo de respaldo)

Jovan mostró Posventa de ML con 2 mensajes sin responder (uno de hace 30 min,
Rafael Ascencion Torres, orden #2000014335644753) mientras el dashboard
mostraba 0 para esa cuenta. Y de nuevo la queja de Amazon "sin sincronizar",
pese al fix del día anterior.

**ML**: se confirmó con `/api/diag/backfill-ml-messages-index?account_id=...`
(búsqueda por `q=` en `/partials/health-messages`) que esa conversación —
12 mensajes reales — estaba completamente ausente de `ml_messages_index`.
El backfill del 2026-08-04 la había cubierto en su momento, pero el diseño
dependía 100% de que el webhook del topic `messages` mantuviera el índice
al día después — y ML simplemente no entrega esa notificación de forma
confiable (limitación conocida de la plataforma, no bug de nuestro código:
el handler `_process_ml_message_webhook` está correcto).

**Amazon**: `/api/diag/buyer-messages-status?live_poll=true` mostró mensajes
de hasta 4 días de antigüedad sin indexar en las 3 cuentas — y al forzar un
poll manual, `poll_account_inbox()` los encontró e insertó al instante (11,
2 y 1 nuevos). El `poll_loop()` de 5 min corre desde el fix de ayer y su
código se ve correcto (try/except bien puesto), pero en producción claramente
no se estaba ejecutando de forma confiable — sin ningún error visible en
logs porque no había heartbeat, solo logging en error.

**Fix — mismo patrón en ambas plataformas: no confiar solo en el mecanismo
"push" (webhook/loop), agregar un mecanismo de respaldo que se auto-repara:**

- ML: nueva función compartida `_ml_messages_scan_and_index()` (extraída del
  backfill manual, mismo código) + `_ml_messages_refresh_loop()` — corre cada
  10 min, re-escanea los últimos 4 días de órdenes de las 4 cuentas y
  re-sincroniza `ml_messages_index`. El backfill manual (`/api/diag/backfill-
  ml-messages-index`) sigue existiendo para historial más viejo.
- Amazon: heartbeat `logger.info` en cada ciclo de `poll_loop()` (antes solo
  logueaba en error — ahora se puede confirmar desde Railway logs que sigue
  vivo) + `trigger_opportunistic_poll()`: dispara un poll en background
  (cooldown 60s) cuando alguien abre la pestaña de Mensajes Amazon (ambas
  vistas, por cuenta y unificada) — red de seguridad que no depende de que
  el loop de fondo esté sano.

Verificado en producción post-deploy: el backfill de 4 días para APANTALLATEMX
indexó 7 conversaciones nuevas y la de Rafael Ascencion Torres ya aparece en
`/partials/health-messages`. Amazon: sin backlog nuevo acumulado tras el
deploy (edades de mensaje consistentes con antes de este fix).

---

## 2026-08-04 — PERF: mensajes de compradores Amazon — verificado sin pérdida real + poller optimizado

Jovan reportó que mensajes de Amazon "no sincronizan a tiempo". A diferencia
del caso de ML (mismo día, ver entrada siguiente), aquí la investigación
descartó pérdida real de datos:

- **VECKTOR y AUTOBOT AMZ MX**: sincronizando normal (41 y 85 min de lag,
  esperado con poll de 5 min).
- **ExclusiveBulbs**: parecía roto (68h sin mensaje nuevo) pero no lo estaba
  — verificado con un diag que compara la ventana de 200 correos más
  recientes por UID contra una búsqueda real por fecha (`SINCE`): los 13
  correos reales de los últimos 7 días SÍ caían dentro de lo que el poller
  revisa, y los 7 que fallan al parsear son todos de 2019-2020 (buzón
  reusado con historial viejo, irrelevante). Conclusión real: esta cuenta
  (la más chica, marketplace USA) simplemente recibe pocos mensajes.

**Ineficiencia real sí encontrada y corregida** (no pérdida de datos, pero sí
diseño incorrecto — Jovan lo pidió arreglar igual): cada ciclo de 5 min
volvía a descargar por completo los mismos ~200 correos de cada cuenta
(60-80s/cuenta) y las 3 cuentas se procesaban una tras otra (secuencial) —
ciclo real ~8-9 min en vez de 5, y una cuenta lenta retrasaba a las demás.

Fix: tabla `amazon_buyer_inbox_state` guarda el último UID de IMAP visto por
cuenta (UID de verdad vía `M.uid(...)`, no sequence number — estable entre
sesiones, a diferencia de lo que usaba el código viejo). Cada poll después
del primero solo trae UIDs nuevos. `poll_all_accounts()` corre las 3 cuentas
con `asyncio.gather` en vez de un for secuencial.

Verificado en producción con 2 pases seguidos del poll en vivo: VECKTOR pasó
de 105.6s (primer poll, sin watermark aún) a **1.9s** (segundo poll, con
watermark) — las 3 cuentas juntas bajaron de ~110s a ~5s totales.

## 2026-08-04 — FIX CRÍTICO: mensajes ML "no entraban" — causa raíz y reescritura completa

Jovan reportó que le avisaron que los mensajes de Mercado Libre "no están
entrando" y que la forma en que los manejamos "no es la correcta". Investigué
con un diag temporal (`/api/diag/ml-messages-audit`) antes de tocar nada.

**Causa raíz confirmada con datos reales de producción:** `get_messages()`
(`meli_client.py`) no tiene forma de listar mensajes directamente — ML no
expone ese endpoint — así que el código escaneaba las **50 órdenes más
recientes** (fijo, sin paginar, sin importar el rango de fechas pedido) y
revisaba cuáles tenían un pack de mensajes. Con el volumen real de órdenes
(APANTALLATEMX: ~540 órdenes/7 días), esas 50 órdenes más recientes cubren
apenas medio día. Resultado medido: de 17 conversaciones reales en 7 días
para APANTALLATEMX, **0 se mostraban** (16 necesitando respuesta, algunas de
hace 5 días). Para LUTEMAMEXICO (mucho menos volumen), 6 de 7 se perdían.

**Fix — reemplazo completo del mecanismo, no un parche:**
1. Tabla nueva `ml_messages_index` (`token_store.py`) — índice local de
   conversaciones (pack_id, último mensaje, quién escribió, fecha, total).
2. El webhook `/webhooks/ml/orders` (Fase 1, hasta hoy solo `orders_v2`/
   `shipments`) ahora acepta también el topic **`messages`** de ML — al
   llegar, trae SOLO ese pack (1 llamada) y actualiza el índice. Reemplaza
   por completo el escaneo de órdenes.
3. `/partials/health-messages` y `/partials/health-messages-unified` ahora
   LEEN del índice local (rápido, correcto, con total real para paginación)
   en vez de escanear en vivo — el detalle de cada conversación (últimos 5
   mensajes) se sigue pidiendo en vivo pero solo para la página actual (~20
   conversaciones), no para cientos de órdenes.
4. Backfill único (`/api/diag/backfill-ml-messages-index`, acotado/paginable
   igual que la migración de fotos a S3) para no perder pendientes ya
   existentes — corrido contra producción para las 4 cuentas ML, 30 días.

**Bug propio encontrado en el camino:** la fecha real de un mensaje ML viene
anidada en `message_date.created` (verificado en vivo con un pack real) —
NO en `date_created`/`date` de nivel superior como asumía el código viejo
(y como asumí yo mismo al escribir el fix inicial). Corregido con un helper
compartido `_ml_msg_date()` antes de correr el backfill en serio — el primer
intento de backfill guardó fechas vacías, se limpió y se corrió de nuevo.

Verificado local con Playwright (clic real en el tab "Mensajes", no solo el
partial por curl): conversación real renderizada, "hace 17h" calculado
correctamente, botones Tomar/Historial/Sugerir con IA intactos, 0 errores
de consola atribuibles al cambio.

**Pendiente real — requiere acción de Jovan:** agregar el topic `messages`
en el Notifications URL del DevCenter de ML (mismo lugar donde ya están
`orders_v2`/`shipments`) — sin eso, el índice solo se actualiza con el
backfill manual, no en tiempo real.

## 2026-08-03 — FEAT: pestañas visuales en "Gral" (Ventas / Rendimiento / Retornos)

Continuación directa del permiso por sección de arriba — Jovan probó como
admin y no vio ninguna pestaña (esperado, ya que con acceso completo la
página se veía igual que siempre) y pidió que sí fueran pestañas reales
arriba para "tener más limpio y poder mejorar" en vez de todo junto en un
solo scroll.

Se agregó una barra de pestañas (`Ventas | Rendimiento | Retornos`) justo
debajo del header — cada panel ya vivía separado en el HTML por los mismos
bloques `{% if %}` del permiso, solo hubo que envolverlos en `<div id="gral-
panel-X">` y agregar `setGralTab()` (toggle de `.hidden` + `history.
replaceState('?gtab=X')`, mismo patrón que el merge de Sync Stock/
Distribución). Los datos se siguen cargando todos de una vez al entrar (no
lazy-load por pestaña) — cambiar de pestaña es instantáneo, sin spinner.

La barra de pestañas solo aparece si el usuario tiene más de un panel
disponible — alguien con acceso solo a "Retornos" ve directo el widget sin
pestañas (nada que alternar), exactamente el caso que motivó todo esto.
Verificado con Playwright las 3 combinaciones de permiso (admin: 3 pestañas
+ toggle funcional; solo-ventas: 2 pestañas sin "Retornos"; solo-retornos: 0
pestañas, vista directa) — 0 errores de consola en las 3.

## 2026-08-03 — FEAT: permisos por sección en "Gral" — "Retornos" se puede dar sin exponer ventas

Jovan pidió poder dar acceso a "Top Retornos Global" a ciertos usuarios sin
que vean el dinero/ventas de "Gral" (`/multi-dashboard`). Propuso reorganizar
la página en tabs (Ventas, Retornos, Rendimiento) — se implementó con el
mecanismo de permisos jerárquico tab→subtab ya usado por Salud/Productos/Ads.

**Hallazgo importante antes de tocar código:** "Gral" compartía la misma
clave de permiso (`ml.dashboard`) que el Dashboard normal de una cuenta E
Inventario Global — dar un subtab de "Gral" habría regalado también acceso
completo a esas otras 2 páginas (que sí muestran ventas). Se le dio a "Gral"
su propia clave `ml.multidashboard`, con migración automática que preserva el
acceso completo a quien ya tenía "Gral" antes (jorge, sergiom en producción).

**Segundo hallazgo:** "Ventas" y "Rendimiento" del frontend de Gral se
alimentan de LA MISMA llamada `/api/dashboard/multi-account` (un solo fetch
trae ranking + gráfica + top productos + cards) — separarlos como permisos
independientes hubiera sido falsa granularidad. Quedaron 2 subtabs reales:
"Ventas y Rendimiento" y "Retornos" — la UI conserva las 3 secciones visuales
de siempre para quien tiene acceso completo, nada cambió para ellos.

Gating real en 2 capas (no solo ocultar HTML): el bloque no permitido ni se
renderiza server-side, Y los 5 endpoints que alimentan esos bloques
(`/api/dashboard/multi-account[-amazon|-launches]`, `/api/dashboard/
morning-briefing`, `/api/returns/unified-top`) ahora exigen el subtab
correspondiente vía `_require_subtab()` — antes ninguno validaba permisos,
solo pedían sesión válida.

Verificado local con 3 tokens JWT sintéticos (admin sin restricción, solo
"ventas", solo "retornos"): cada endpoint responde 200/403 exactamente como
se espera, el HTML de cada usuario solo contiene los bloques permitidos (0
fugas de datos reales, solo comentarios de JS muertos con el mismo texto),
y el panel de Usuarios ya muestra "Gral" con sus 2 checkboxes nuevos
("Retornos" / "Ventas y Rendimiento") separado del "Dashboard" normal, sin
tocar la plantilla de ese panel (ya renderiza el árbol de permisos
dinámicamente).

## 2026-08-03 — FEAT: reclamos sin comentario del comprador muestran el motivo prominente (Top Retornos Global)

Jovan reportó (con captura) que la tarjeta de detalle de un SKU en "Top
Retornos Global" (`/multi-dashboard`) mostraba varios reclamos con "Sin
comentario del comprador" y preguntó cuál era el motivo real detrás de eso.

Investigación en producción con `/api/diag/inspect-claim` y
`/api/diag/claims-raw`: el `reason_id` crudo que manda ML para estos
reclamos es tipo `PDD9949`/`PNR9513` (no los códigos limpios `PDD1`-`PDD6`
que `CLAIM_REASON_MAP` tiene mapeados) — son IDs internos del proceso de
mediación de ML, no un catálogo de tipo de defecto. Confirmado cruzando el
mismo código contra comentarios reales: `PDD9947` apareció tanto en un
reclamo real de pantalla ("no está bien sellada... manchas") como en un
mensaje que solo traía una dirección de envío — mismo código, problemas
distintos. Conclusión: no hay un motivo más granular escondido que
podamos extraer; lo más específico y confiable que tenemos es el pill de
`reason_label` que ya se mostraba (ej. "Defectuoso/Diferente").

Fix aplicado (`multi_dashboard.html`, `_renderClaimCards`): cuando no hay
`buyer_comment`, el pill de motivo ahora se muestra prominente (ámbar,
más grande, con nota explicando que es lo que el comprador eligió al
abrir el reclamo) en vez de perderse como un pill gris chico entre
metadata, seguido de un texto muerto "Sin comentario del comprador". Con
comentario real, el motivo queda secundario (gris, chico) y el comentario
manda. Verificado con Playwright local (screenshot + 0 errores de
consola) llamando `_renderClaimCards` directo con datos sintéticos
representando ambos casos.

---

## 2026-08-03 — FIX: "Gral" habilitado en el nav de Amazon (estaba mal bloqueado)

Jovan cuestionó mi explicación anterior de por qué "Gral" aparecía
deshabilitado en Amazon — con razón. Revisando el código real,
`multi_dashboard.html` YA renderiza una sección completa "Amazon —
Comparativa de Cuentas" (título de página: "Vista General — X Cuentas
MeLi + Y Amazon") desde antes — no era una diferencia real de plataforma,
era un error de configuración del nav (`amz_href=None` cuando debía tener
la misma URL que ML). `_accounts_ctx()` ya carga `amazon_accounts` sin
condicionarlo a la plataforma activa, así que no hizo falta tocar el
backend — solo `amz_href="/multi-dashboard"` en `_NAV_TAB_DEFS`
(`app/main.py`). Verificado local: el link aparece activo en el nav de
Amazon y la página carga (200).

"Inv.Global" (BM × 4 cuentas MeLi, cero Amazon en el código) y "Sync Stock"
(su función central es exclusiva de ML, Amazon ahí es solo monitoreo
FBA/FLX informativo) sí quedan confirmados como diferencias reales de
plataforma — Jovan decidió dejarlos como están.

---

## 2026-08-03 — FIX: tab "Distribución" muerto eliminado del nav

Jovan preguntó por qué "Gral", "Inv.Global", "Sync Stock" y "Distribución"
aparecían deshabilitados en el nav de Amazon. Los primeros 3 son diferencias
reales de plataforma (`amz_href=None` explícito — conceptos que no aplican
a Amazon tal cual: Gral es un tablero multi-cuenta ML, Sync Stock reparte
stock proporcionalmente entre las 4 cuentas ML). "Distribución" era distinto:
desde la fusión del 2026-07-18 ya no tenía `href` en NINGUNA plataforma
(`ml_href=None` y `amz_href=None`) — su funcionalidad real vive fusionada
dentro de Sync Stock (subvista "Configurar"). Se quitó el `dict` del tab de
`_NAV_TAB_DEFS` en `app/main.py` — la ruta `/distribucion` (redirect) sigue
viva, solo se quitó el tab que no iba a ningún lado.

Pendiente abierto (no implementado, solo anotado): Jovan preguntó si "Gral"
podría tener sentido para Amazon (tablero consolidado de sus 3 cuentas,
igual que ML tiene para sus 4) — decidió no priorizarlo por ahora.

---

## 2026-08-03 — FIX: batería de hallazgos de la auditoría de 4 agentes (fallas silenciosas + endpoints destructivos)

Jovan pidió una auditoría general ("que todo esté funcionando bien... dejarla
muy profesional"). 4 agentes en paralelo revisaron loops de background,
frescura de caches, y manejo de errores. **Importante:** 2 de los hallazgos
iniciales (catálogo BM "14 días stale", `bm_stock_snapshot` "13 días stale")
resultaron ser falsas alarmas — los agentes consultaron la copia LOCAL de
`tokens.db`, no producción. Verificado en vivo contra Railway: catálogo
corrió hace 9h (exitoso), `bm_sku_master` actualizado hace 23min. Ambos
sanos — lección para no repetir: siempre verificar contra producción antes
de "arreglar" algo basado en un query a la DB local.

**Arreglado de verdad (`app/main.py`, `app/services/token_store.py`,
`app/services/stock_sync_multi.py`, `app/services/buyer_messages_client.py`,
`app/services/meli_client.py`, `app/api/lanzar.py`, `app/api/amazon_lanzar.py`):**

- **Fotos huérfanas en `claim_photos`** — causa raíz confirmada: el `DELETE`
  tras evict comparaba `local_path` como string exacto, y filas escritas en
  Windows (`\`) vs Railway/Coolify Linux (`/`) conviven en la misma DB sin
  normalizar. Fix: `REPLACE(local_path,'\','/')` en ambos lados del DELETE +
  log si `rowcount` no coincide con lo esperado (antes silencioso).
- **Poller de mensajes Amazon** (`buyer_messages_client.py`) — fallaba
  totalmente silencioso si IMAP se rompía. Ahora loguea errores por cuenta.
- **`bulk_update_ml_listing_qtys`** — silencioso tras sync exitoso con
  ML/BM; ahora logueado (stock podía quedar desactualizado sin aviso).
- **`save_launched_listing`** — silencioso DESPUÉS de crear un listing real
  en Amazon; riesgo de relanzarlo duplicado sin este log.
- **`get_shipment_costs`** — caía a $0 silenciosamente, subestimando costo de
  envío en cálculos de rentabilidad; ahora logueado.
- **4 llamadas a `log_action`** (auditoría de precio/listing en `lanzar.py`)
  con `except: pass` — la escritura real en ML sí ocurría pero el rastro de
  auditoría se perdía sin dejar log.
- **VECKTOR (Amazon) unos días atrás en `order_history`** — `get_order_items`
  por orden fallaba silencioso (rate-limited, 429 frecuente); ahora logueado
  con el order_id específico para poder diagnosticar de qué órdenes falta.
- **2 endpoints destructivos por GET → POST**: `/api/diag/clear-realtime-alerts`
  y `/api/diag/emergency-clear-claim-photos` — un GET destructivo puede
  dispararse por accidente (link preview, crawler, precarga del navegador).

---

## 2026-08-03 — FIX: subconteo grave de devoluciones Amazon (7 en vez de 113+ reales)

**Archivos:** `app/main.py` (`_fetch_amazon_returns_report_cached`,
`_aggregate_amazon_returns_by_sku`, `/api/amazon/returns/top-skus`),
`app/templates/amazon_returns.html`.

Jovan reportó (con captura) que "Top SKUs por Retornos" mostraba 7
devoluciones para SNTV001764/B0G4B9MNCQ (ExclusiveBulbs, 90 días) cuando en
Seller Central hay muchas más. Dos bugs reales combinados:

1. `_fetch_amazon_returns_report_cached` recortaba en silencio cualquier
   ventana >60 días a los últimos 60 (`capped_days = min(days, 60)`) — Amazon
   limita cada reporte a 60 días, pero nunca se implementó pedir varios
   reportes para cubrir más. Fix: pide tantos reportes de 60 días como haga
   falta y combina deduplicando por (order_id, sku, return_date).
2. **El bug grande de verdad:** `_aggregate_amazon_returns_by_sku` usaba los
   refunds financieros (Financial Events) como fuente PRINCIPAL de conteo —
   pero no toda devolución física genera un refund financiero en la misma
   ventana (reembolso pendiente, cambio en vez de devolución, etc). Verificado
   en vivo: el reporte real de devoluciones FBA tenía **127** filas para ese
   SKU en la misma ventana, contra 6-7 vía refunds. Rediseñado: el reporte de
   devoluciones FBA (una fila = una devolución física real) es ahora la
   fuente principal; los refunds financieros sin fila correspondiente
   (típicamente MFN, que el reporte FBA no cubre) se agregan aparte para no
   perder esas devoluciones, sin duplicar las que ya vienen del reporte.
   Resultado verificado: 113 devoluciones reales para SNTV001764 (antes 6-7).

**De paso:** paginación real (10/página) + buscador por SKU/título + aviso
visible "solo FBA" en la tabla — antes eran hasta 50 filas de un jalón sin
paginar (Jovan lo reportó como "absurdo estar hasta abajo con scroll").

---

## 2026-08-03 — OPERACION: disco al 92.9% (3er acercamiento) — migración de fotos/facturas HISTÓRICAS a S3

**Archivo:** `app/main.py` (`POST /api/diag/migrate-historical-to-s3`, nuevo).

La migración a S3 del 2026-08-01/02 solo cubría fotos/facturas NUEVAS —
las 231 fotos (52.75MB) y 1,062 facturas (35.47MB) que ya estaban en disco
nunca se movieron (a propósito, quedó documentado así). El disco volvió a
92.9% (21.1MB libres) y Jovan lo reportó de nuevo.

Endpoint nuevo, por lotes (`limit`, default llamar repetido hasta
`remaining=0` — evita el timeout de ~30s de Railway): sube cada archivo a
S3, **verifica byte-a-byte releyendo de S3**, y solo entonces borra el
archivo local y actualiza la fila (`storage='s3'`). Para facturas (pdf+xml),
si cualquiera de las dos partes falla la verificación, no borra nada de ese
registro — evita dejar una fila apuntando a un archivo ya borrado.
Verificado localmente con un lote de prueba antes de correr contra
producción (foto migrada, archivo local confirmado borrado, servido
correcto desde S3 vía `storage=s3`).

---

## 2026-08-02 — FIX: `order_history` no tenía backfill inicial — 3 de 4 cuentas ML y 2 de 3 Amazon sin historial antes de mediados de julio

**Archivos:** `app/services/token_store.py` (`has_deep_order_history`, nueva),
`app/main.py` (`_supplier_debt_sync_loop`).

Encontrado mientras se armaba un reporte real de impacto de calidad para
directivos: la comparación "junio vs julio" salía con un falso "+126% de
crecimiento" que resultó ser un artefacto de datos, no un hecho de negocio.

**Causa raíz:** `_supplier_debt_sync_loop` (agregado ~2026-07-19 para el
ledger de deuda) es el único mecanismo que llena `order_history` sin
depender de que alguien visite Deals (ML) o Planeación→Velocidad (Amazon) —
pero solo trae una ventana de **3 días hacia atrás**, sin backfill. Cuentas
que nadie había visitado nunca en esas pestañas (AUTOBOT, LUTEMAMEXICO,
BLOWTECHNOLOGIES en ML; AUTOBOT y ExclusiveBulbs en Amazon) se quedaron sin
ningún historial anterior a cuando ese loop empezó a correr para ellas.

- `has_deep_order_history(account_id, platform, min_days=20)` — chequea si
  ya existe al menos una fila de hace >=20 días.
- El loop ahora pide 90 días (en vez de 3) la primera vez que detecta que a
  una cuenta le falta historial profundo — deja de aplicar automáticamente
  en cuanto esa cuenta ya tiene el backfill.
- Se corrió el backfill real una vez para las 3 cuentas ML afectadas
  (mayo–agosto, vía `client.fetch_all_orders` + `_save_ml_orders_history_bg`)
  antes de recalcular cualquier número del reporte.

**Impacto real revelado por el backfill:** BLOWTECHNOLOGIES pasó de vender
$300-668K/día (mayo) a ~$119K/día (promedio últimos 10 días) — caída real
de -82%. Es la única de las 4 cuentas ML en nivel de reputación **amarillo**
en Mercado Libre (las otras 3 están en verde), con tasa de reclamos más del
doble que cualquier otra cuenta y activamente en el programa de
"Recuperación de reputación" de ML (confirmado vía `get_seller_reputation()`
y `get_reputation_recovery_status()` en vivo). Afecta también Finanzas y
cualquier reporte futuro que dependa de `order_history` — no solo este
reporte puntual.

---

## 2026-08-02 — FEAT: facturas nuevas (PDF/XML) también se guardan en MinIO/S3 (MI2)

**Archivos:** `app/services/token_store.py` únicamente (`save_billing_invoice`,
`get_billing_invoice`, `delete_billing_request`, migración columna `storage`
en `billing_invoices`). `main.py` y `app/api/facturacion.py` no se tocaron —
ya estaban desacoplados, solo llaman a estas 3 funciones.

Segunda ronda de la migración a S3 (ver DEVLOG 2026-08-01, fotos de reclamos)
— mismo patrón exacto aplicado a `uploads/invoices/` (34.95MB). Diferencia
relevante: son documentos fiscales reales, no solo fotos de referencia — si
sube a S3 falla (ej. `SlowDownWrite` transitorio de MinIO, ya visto en
pruebas), cae a disco local automáticamente en vez de perder el archivo.

- Columna `storage` ('local'|'s3') en `billing_invoices`, default 'local'
  para todo lo histórico (se sigue sirviendo exactamente igual).
- Verificado end-to-end con un request_id de prueba: subida real a S3,
  lectura con bytes idénticos, y borrado confirmado directamente contra el
  bucket (`get_object_bytes` devuelve None tras `delete_billing_request`).
- Facturas ya existentes en disco: no se migran en esta ronda.

---

## 2026-08-01 — FEAT: fotos nuevas de reclamos ML se guardan en MinIO/S3 (MI2), no en disco de Railway

**Archivos:** `app/services/s3_storage.py` (nuevo), `app/services/token_store.py`
(migración `claim_photos.storage`, `save_claim_photos` acepta el campo),
`app/main.py` (`returns_claim_photo_proxy`, backfill de `returns_sku_claims_detail`,
`returns_claim_photo_file`, `returns_claims_zip`/export ZIP), `requirements.txt`
(`boto3`).

Consecuencia directa de la crisis de disco de Railway (ver DEVLOG 2026-07-31):
Jovan ya no quiere pagar más volumen y MI2 finalmente entregó las credenciales
MinIO (`s3.mi2.com.mx`, bucket `coolify-ecomops`) que llevaban semanas
pendientes. Las 6 env vars ya estaban en Railway desde antes de este cambio
(confirmado vía API de Railway) — este commit es el que las empieza a usar.

Hallazgo importante que cambió el diseño original: el bucket es **privado**
(GET anónimo → 403) y `MINIO_PUBLIC_URL` no tiene listener en :443 (timeout).
No existe una URL pública usable — toda lectura tiene que pasar por
`get_object_bytes()` con las credenciales de la app (`s3_storage.py`), nunca
por un link directo en el `<img src>`.

- Fotos nuevas de reclamos (proxy en vivo + backfill batch por SKU) se suben a
  `claim_photos/{claim_id}/0_{stem}.jpg` en el bucket, con fallback automático
  a disco local si `AWS_ENDPOINT_URL_S3` no está configurado (dev sin S3) o si
  la subida falla (ej. `SlowDownWrite` transitorio de MinIO, confirmado en
  pruebas locales — reintenta bien la próxima vez).
- Tabla `claim_photos` tiene columna nueva `storage` ('local'|'s3', default
  'local' — todo lo histórico se sirve exactamente igual que antes).
- `GET /api/returns/claim-photo-file` ahora acepta `?storage=s3` y lee vía
  `get_object_bytes()`; 404 limpio si MinIO no responde o el objeto no existe
  (no tumba la vista de reclamos).
- ZIP-export de reclamos por SKU también sabe leer de S3 cuando aplica.
- Fotos ya existentes en disco de Railway: **no se migran** en esta ronda,
  siguen sirviéndose local. Facturas (`uploads/invoices/`) quedan para una
  ronda separada.
- Verificado localmente: subida/lectura/borrado real contra `s3.mi2.com.mx`,
  servido HTTP real (200 + bytes idénticos), 404 limpio para key inexistente,
  y regresión confirmada en una foto local histórica real.

---

## 2026-07-31 — OPERACION: disco de Railway al 92.8% — retención automática de audit_log (18,337 filas archivadas)

**Archivos:** `app/main.py` (2 endpoints diag nuevos), `scripts/archive_audit_log.py`,
tarea programada de Windows `ApantallateMX-ArchiveAuditLog`.

Jovan vio el banner de disco (92.8%, 21.7MB libres) en una captura mientras
revisaba otra cosa. Investigado: el volumen de Railway (~430MB) está casi
lleno — la DB sola pesa 309MB (78%), fotos+facturas otros 88MB. `audit_log`
(21,638 filas) no tenía **ningún límite de retención** — crece para
siempre. No es la tabla más grande (`amazon_listings`/`order_history` son
mayores, pero esas SÍ hacen falta completas para cálculos de negocio
reales) pero sí la más segura de podar porque nadie depende del dato viejo.

Jovan no quiere pagar más Railway; ya mandó correo a MI2 pidiendo storage
MinIO hace 2 semanas, sin respuesta aún. Como solución inmediata sin costo
y sin esperar a MI2: usar el servidor dedicado (siempre encendido, no la
laptop personal) como respaldo local antes de purgar.

- `GET /api/diag/audit-log-export?before_days=N&token=...` — exporta (sin
  borrar) filas más viejas que N días.
- `POST /api/diag/audit-log-purge?before_days=N&expected_count=X&token=...`
  — borra esas mismas filas, solo si el count actual coincide con
  expected_count (evita perder filas nuevas que hayan entrado justo entre
  el export y el purge).
- `scripts/archive_audit_log.py` — exporta a `backups/audit_log/` (gitignored),
  verifica releyendo el archivo, y solo entonces purga. Nunca borra sin
  haber guardado primero.
- Tarea programada de Windows, diaria 3am, en este mismo servidor.

Corrida real ya ejecutada: 18,337 de 21,638 filas archivadas y purgadas
(quedan 3,305 recientes en producción). **Importante:** esto NO reduce el
tamaño del archivo `.db` hoy — VACUUM necesitaría ~2x espacio libre que no
tenemos — solo detiene el crecimiento futuro de esta tabla específica. La
crisis de disco de fondo sigue sin resolverse del todo; sigue pendiente la
respuesta de MI2 para MinIO (fotos+facturas, 88MB reales que sí se
liberarían del volumen).

## 2026-07-31 — TUNE: sync de Feedback más frecuente (24h→4h Amazon, 50→150 top-sellers ML)

Jovan preguntó qué se podía hacer de verdad ante los límites reales de
webhook (Amazon no tiene, ML necesita config de DevCenter). El intervalo
de 24h no tenía justificación técnica real — la quota de `createReport` de
Amazon aguanta 4h sin riesgo. Sube también `top_n_items` de ML (50→150)
para cubrir más catálogo por corrida.

## 2026-07-31 — FEAT: Monitoreo de feedback (Amazon seller feedback + reseñas ML negativas) con alerta por correo

**Archivos:** `app/services/amazon_client.py`, `app/services/token_store.py`,
`app/services/buyer_messages_client.py`, `app/api/health.py`,
`app/api/amazon_products.py`, `app/api/users.py`, `app/services/user_store.py`,
`app/main.py`, `app/templates/health.html`, `app/templates/amazon_dashboard.html`,
`app/templates/partials/health_feedback.html`, `app/templates/partials/health_summary.html`,
`app/static/js/amazon_dashboard.js`.

Jovan pidió poder monitorear feedback nuevo (Amazon y ML) día a día sin
entrar a cada plataforma a revisar a mano, con alerta cuando entra algo
nuevo. Feature nueva completa, en las 2 plataformas:

- **Amazon**: `GET_SELLER_FEEDBACK_DATA` vía Reports API — calificación del
  comprador al vendedor (envío/comunicación/condición), cruzada con SKU vía
  `order_history` (Amazon solo da Order ID, no SKU directo). Confirmado en
  vivo contra VECKTOR: las columnas de este reporte vienen **localizadas al
  idioma del marketplace** (español: Fecha/Rating/Comentarios/Número de
  pedido/Correo del evaluador) — a diferencia de los demás reportes
  flat-file de este archivo, que usan headers fijos en inglés. De paso se
  encontró y corrigió un bug de encoding en `download_report_document`:
  probaba UTF-8 a secas y los acentos salían como carácter de reemplazo (```
  Número``` → ```N�mero```) — ahora intenta UTF-8 estricto primero (no
  cambia nada para reportes en inglés) y cae a cp1252 solo si falla.
- **ML**: reseñas de producto negativas/neutras (rate≤3) vía
  `GET /reviews/item/{id}`, acotado a los top-N más vendidos activos por
  cuenta — NO todo el catálogo. La API de ML no tiene forma de filtrar por
  rating ni de pedir "solo las nuevas" (confirmado: sin parámetro de orden
  documentado), así que se dedupe por `review_id` y con el tiempo cubre el
  universo real de reseñas de los productos que más importan.
- Sync 1x/24h en background (`token_store.feedback_sync_loop`, con delay de
  10 min al arrancar — el createReport de Amazon tiene quota muy baja y ya
  compite con otros reportes al deploy; sin este delay cada restart
  disparaba el sync de inmediato, confirmado al probar localmente) + correo
  de alerta agrupado vía Gmail API (`buyer_messages_client.send_notification`,
  nuevo helper que prueba las cuentas Amazon configuradas en orden, sin
  forzar "Re:" como `send_reply`).
- Tab "Feedback" nuevo en Salud: ML dentro de `health.html` (mismo patrón
  que Vigilancia/Reclamos, con badge), Amazon dentro de la Salud de
  `amazon_dashboard.html` (mismo patrón client-rendered que Mensajes de
  Compradores). Ambos con botón "Marcar atendido", logueado en audit_log.
- 2 tablas nuevas: `amazon_seller_feedback`, `ml_item_reviews`.

Verificado en vivo contra las 3 cuentas Amazon reales y las 4 cuentas ML
reales: encontró 6 feedbacks negativos/neutros reales sin atender en
VECKTOR/ExclusiveBulbs (AUTOBOT AMZ MX falló con reporte CANCELLED, no
fatal, ya manejado con warning). 0 reseñas ML negativas en los top-sellers
muestreados de esta corrida — resultado válido, no error. **Nota:** esa
primera corrida de prueba probablemente ya disparó el correo real con esos
6 casos antes de agregar el delay de arranque — se le avisó a Jovan.

No completado (pendiente real, no forzado): Amazon no tiene forma de saber
"solo lo nuevo" vía webhook — la vía robusta sería el topic
`orders_feedback` de notificaciones, que depende de la misma configuración
de Notifications URL en DevCenter ML que ya estaba pendiente para el
webhook de órdenes en tiempo real.

## 2026-07-30 — FIX+FEAT: 6 hallazgos más de la auditoría (bulk Qty0 sin detalle, eliminar sin rol, puente órdenes-reclamos, alertas de stock confusas, writes sin manejo de error)

**Archivos:** `app/api/amazon_products.py`, `app/main.py`, `app/services/token_store.py`,
`app/static/js/amazon_dashboard.js`, `app/templates/health.html`,
`app/templates/multi_dashboard.html`, `app/templates/partials/amazon_products_sin_publicar.html`,
`app/templates/partials/orders_table.html`.

Continuación del cierre de hallazgos de la auditoría de sistema (bloque "E"):

1. **Bulk "Qty 0" (Amazon, tab Stock) no decía qué SKUs fallaron.** El backend
   ya regresaba el detalle completo (`d.results`) pero el frontend lo
   descartaba y solo mostraba un conteo genérico OK/error. Ahora se muestra
   la lista de SKUs seleccionados en la confirmación y el detalle de fallos
   si los hay.
2. **Eliminar un listing de Amazon (irreversible) no tenía ningún control de
   rol**, mientras "marcar mensajes atendidos" (reversible) sí requería
   admin — la protección estaba al revés del riesgo real. Ahora
   `POST /api/amazon/products/delete-listing` exige rol admin en backend, y
   el botón se oculta para no-admins en `amazon_products_sin_publicar.html`.
3. **Puente entre Ventas y Salud** — badge "🚩 Reclamo" en la fila de la
   orden cuando tiene un reclamo ML abierto, con link que salta directo a
   Salud y dispara la búsqueda por ese número de orden (antes no había forma
   de saber desde Ventas si una orden tenía un reclamo sin cambiar de
   pestaña y buscar a mano). Nueva query
   `token_store.get_order_ids_with_open_claims()` + auto-búsqueda en
   `health.html` vía `?search=`.
4. **Las 2 fuentes de "alerta de stock" (Dashboard: top-sellers con bajo
   stock; Morning Briefing: sobreventa ya detectada) se etiquetaban igual**
   ("N en riesgo"), pareciendo contradictorias para la misma cuenta. Ahora
   Morning Briefing dice explícitamente "sobreventa detectada". De paso se
   corrigió un link roto a un ancla `#stock-section` que ya no existe desde
   que Stock pasó a ser pestaña (`data-tab`) — ahora usa `/items?tab=stock`.
5. **5 escrituras fire-and-forget** (`asyncio.create_task(save_item_sync)`)
   sin ningún try/except — un fallo bajo contención (ej. "database is
   locked") quedaba como excepción huérfana invisible en la UI. Nuevo
   helper `_safe_bg()` que loguea el error en vez de perderlo, aplicado en
   los 5 sitios (mark-synced, update-stock, variation-stock, y los 2 de
   concentración de stock).

Verificado en vivo: rol admin ve botón Eliminar (10 ocurrencias), rol viewer
no lo ve (0) y el endpoint regresa 403 si lo intenta igual. Query de puente
reclamos verificada directo contra la DB.

No completado en este bloque (quedan documentados como pendientes reales,
no forzados):
- Bulk-edit de precio ML: no existe ni siquiera bulk de stock en
  `items.html` hoy — sería una feature nueva, no un ajuste.
- Bundle piloto TV+soporte: los SKUs de soporte sugeridos por la auditoría
  ni siquiera están publicados en ML, y uno de ellos (RMTC008173) resultó
  ser un soporte para un TV de 85" — no aplica al TV de 32" sugerido.
  Necesita que el negocio confirme qué soporte real va con cada TV antes de
  publicar nada.

## 2026-07-30 — FIX: 5 hallazgos de la auditoría de sistema (JOIN roto, locks SQLite, disco sin monitorear, prompt() de precio, confirmación invertida)

**Archivos:** `app/services/token_store.py`, `app/main.py`, `app/services/user_store.py`,
`app/api/amazon_products.py`, `app/auth.py`, `app/api/lanzar.py`, `app/api/health_ai.py`,
`app/api/productos.py`, `app/api/facturacion.py`, `app/api/system_health.py`,
`app/api/amazon_lanzar.py`, `app/templates/base.html`, `app/templates/amazon_products.html`,
`app/templates/items.html`.

Continuación de la auditoría de sistema (4 agentes en paralelo). 5 fixes de código:

1. **"Candidatos a Eliminar" (Amazon) marcaba el 100% del catálogo.** El JOIN
   comparaba `order_history.account_id` (nickname) contra
   `amazon_listings.seller_id` (ID real) — nunca coincidían, así que la fecha
   de última venta siempre salía NULL. Se agregó JOIN con `amazon_accounts`
   para resolver nickname→seller_id antes de comparar. Verificado en vivo:
   3,400 candidatos reales (antes 3,866 = catálogo completo).
2. **261 conexiones SQLite sin timeout explícito** (default 5s de Python) en
   11 archivos, bajo contención de varios loops de fondo escribiendo a la
   vez tras cada deploy. Subido a `timeout=15` — mismo cambio mecánico en
   los 261 sitios.
3. **Cero monitoreo de espacio en disco.** Los 2 incidentes de disco lleno
   (fotos, luego facturas) se detectaron por reporte de Jovan, nunca por
   alerta del sistema. Nuevo `GET /api/system/disk-usage` + banner global
   si el volumen pasa 85%. Importante: `uploads/invoices/` NO se toca con
   eviction — son facturas fiscales reales (CFDI), no caché; aquí el fix es
   solo monitoreo/alerta, nunca borrado automático.
4. **Precio de Amazon con `prompt()` nativo** — publicaba con el primer
   "Aceptar" sin mostrar antes/después. Modal compartido nuevo
   (`window.showAmzPriceModal`) en los 2 sitios que lo usaban.
5. **Fricción de confirmación invertida** — la edición inline de stock en
   Productos ML (el camino que se usa todo el día) guardaba directo sin
   avisar, mientras el modal completo sí confirmaba. Se agregó un `confirm()`
   ligero, solo cuando el valor realmente cambia.

Verificado en vivo: `disk-usage` devuelve datos reales del volumen, y
Sin Publicar/Amazon confirma 3,400 candidatos (no 3,866).

## 2026-07-30 — OPERACION: Gmail de VECKTOR revocado — reautorizado + causa raíz confirmada

**Archivos:** ninguno (solo Railway env var).

Jovan reportó error real en producción al responder un mensaje de comprador
Amazon: `invalid_grant: Token has been expired or revoked` sobre el Gmail
dedicado de VECKTOR. Confirmado probando los 3 refresh tokens directo
contra Google: solo el de VECKTOR fallaba (AUTOBOT y ExclusiveBulbs OK).
Jovan reautorizó vía `/auth/gmail/connect`, el nuevo token se subió a
`AMAZON_GMAIL_REFRESH_TOKEN` en Railway. Google confirmó con el propio
token (`refresh_token_expires_in: 604690`s ≈ 7 días) que la causa raíz es
que el proyecto de Google Cloud de VECKTOR sigue en modo **Testing** (no
Production) — por diseño de Google, esos tokens expiran solos cada 7 días.
**Pendiente real:** publicar ese proyecto a Production (o crear un cliente
nuevo, mismo patrón que ya se hizo para AUTOBOT/ExclusiveBulbs) para que
esto deje de repetirse — Jovan pidió que se le recuerde más tarde.

## 2026-07-30 — FIX urgente: "Pausar" en Productos ML sí pausaba de verdad + FEAT: historial de auditoría en mensajes/reclamos

**Archivos:** `app/static/js/productos.js`, `app/api/health.py`, `app/main.py`,
`app/services/user_store.py`, `app/api/users.py`, `app/templates/base.html`,
`app/templates/partials/health_messages.html`, `app/templates/partials/health_claims.html`,
`app/static/js/amazon_dashboard.js`.

Una auditoría completa del sistema (4 agentes en paralelo: UX, estrategia
ML, estrategia Amazon, estabilidad técnica) encontró que el botón "⏸
Pausar" de `/productos` mandaba `status:"paused"` REAL a Mercado Libre vía
`PUT /api/items/{id}/status` — violación activa de la regla dura de nunca
pausar (penaliza el algoritmo de ML). Ya existía el patrón correcto en
`items_health.html` (pone `available_quantity:0` en vez de status) que
nunca se replicó aquí. Corregido: "Pausar" ahora usa `PUT /stock
{quantity:0}`; "Activar" sigue usando `status:active` (seguro — nunca crea
una pausa nueva, solo revierte una existente).

De la misma auditoría, Jovan preguntó puntualmente por la orden Amazon
702-2485113-2487435: un mensaje se marcó "resuelto" pero no había forma de
saber qué usuario lo hizo. Causa: `update_message_view_status()` /
`update_claim_view_status()` nunca guardaban quién ejecutaba la acción, y
la tabla `ml_message_views` solo guarda la ÚLTIMA vista (una fila por
hilo, se sobreescribe) — no un historial real. Se conectaron los 6
botones de Tomar/Resolver (mensajes ML, reclamos ML, mensajes de
compradores Amazon) a la tabla `audit_log` ya existente (usada en otras
partes de la app) vía `user_store.log_action()`. Nuevo endpoint `GET
/api/users/audit/item-history?item_id=...` (cualquier usuario logueado,
no solo admin) + botón "🕘 Historial" en las 3 vistas, con un modal
compartido en `base.html` (`window.showItemHistory`). Es hacia adelante
— no recupera quién hizo acciones pasadas antes de este fix.

Verificado en local: los 6 endpoints escriben a `audit_log` correctamente,
el historial regresa la lista en orden correcto (más reciente primero), y
los 3 partials renderizan el botón sin errores de sintaxis Jinja/JS.

---

## 2026-07-29 — OPERACION: Returns Board ML vista Global — verificado sano

**Archivos:** ninguno (solo verificación, sin cambios de código).

Pendiente viejo (de sesiones 2026-06-10/07-13) nunca confirmado con datos
reales tras el fix de agregación de esa época (commit 11f669e). Se golpeó
`/api/returns/unified-top?days=30` en local: `total:950` (`ml_total:832`,
`amz_total:118`), las 4 cuentas ML (APANTALLATEMX/AUTOBOT MEXICO/
BLOWTECHNOLOGIES/LUTEMAMEXICO) y las 3 Amazon (VECKTOR IMPORTS/AUTOBOT
AMZ MX/ExclusiveBulbs) aparecen agregadas por SKU sin duplicados ni
"Sin título". Pendiente removido de CLAUDE.md y `.claude/memory/project_wip.md`.

---

## 2026-07-29 — FIX+FEAT: deuda de bajo impacto de auditoría responsive (hover KPI, cards mobile, paginación, 2 bugs chiquitos)

**Archivos:** `app/static/js/sku_inventory.js`, `app/templates/partials/amazon_products_sin_publicar.html`,
`app/templates/orders.html`, `app/templates/partials/amazon_products_buybox.html`,
`app/templates/partials/amazon_products_devoluciones.html`,
`app/templates/partials/amazon_products_inventario.html`, y ~23 templates/JS
más con la clase `.kpi-card` (Retornos ML/Amazon, Salud, Productos, Ventas,
Amazon Productos, Ads, Planeación, Sync Stock, dashboard de Amazon).

Cierra los 4 pendientes de bajo impacto que quedaron documentados en
`project_responsive_audit_2026-07-21` sin activar nunca:

1. **Bug real:** `sku_inventory.js:297` usaba `colspan="12"` para una tabla
   con 11 `<th>` reales.
2. **Bug real:** `amazon_products_sin_publicar.html` tenía dos `<th>` con el
   mismo texto "Sin venta" — la 1ª corresponde a `item.last_sale` (fecha),
   ahora dice "Última venta"; la 2ª (`item.days_no_sale`) se queda igual.
3. **Hover `.kpi-card`** aplicado en ~23 ubicaciones que no lo tenían
   (investigado con agente Explore, aplicado con un agente separado en
   paralelo — solo se agregó la clase, sin tocar onclick/lógica/texto).
4. **Cards mobile** en 6 de 8 tablas revisadas de Amazon Productos (Buy Box,
   Devoluciones, Suprimidos/Inactivos/Historial de Sin Publicar, Inventario
   18 cols). **Catálogo y Candidatos a Eliminar se dejaron intactos**
   (solo scroll horizontal, sin cards) — un agente de investigación
   confirmó que su JS (`initBulkSelection`/`clearBulkSelection` en
   `amazon_dashboard.js`, `_amzCandCheckAll`/`_amzBulkAction` en
   `amazon_products.html`) usa `querySelectorAll` sin scope de visibilidad
   sobre checkboxes de bulk-action — duplicar el markup en una card oculta
   habría duplicado cierres/eliminaciones reales de listings en Amazon.
5. **Paginación estandarizada** en `orders.html`: "Por SKU" pasó de
   mostrar/ocultar filas (`classList.toggle('hidden')`) a arreglo+slice+
   rerender (mismo patrón que `_renderPaginated()` del resto de la app,
   capturando el HTML ya renderizado por Jinja al cargar en vez de
   duplicar la plantilla en JS — cero riesgo de drift). "Comparativa" **no
   tenía ninguna paginación** (renderizaba todos los SKUs filtrados de un
   jalón) — ahora pagina 20/página igual que Por SKU.

Verificado: sintaxis Jinja de los 27 templates (`jinja2.Environment.parse`)
+ `node --check` de los 2 JS tocados directamente + los 6 JS con `kpi-card`
agregado. Los 4 endpoints de Amazon Productos (buybox/devoluciones/
sin-publicar/inventario) y `/orders` (vistas `sku`/`compare`) responden 200
sin traceback contra datos reales en local. **No se pudo verificar
visualmente en browser** — la extensión de Chrome no conectó en esta
sesión, queda pendiente una verificación visual con Playwright/Chrome la
próxima vez que esté disponible.

---

## 2026-07-27 — FEAT: plantillas de respuesta acotadas a UNA cuenta específica

**Archivos:** `app/services/token_store.py` (`reply_templates` + `account_id`),
`app/main.py` (`/api/reply-templates`, nuevo `/api/reply-templates/accounts`),
`app/templates/partials/reply_templates_modal.html`,
`app/templates/partials/health_messages.html`, `app/static/js/amazon_dashboard.js`.

Jovan notó que las plantillas solo se separaban por plataforma (ML/Amazon/
ambas) pero no por cuenta — BLOW, AUTOBOT, Lutema y Apantallate pueden
necesitar responder distinto (tono, políticas) y no había forma de acotar
una plantilla a una sola cuenta.

- `reply_templates` gana `account_id` (`''` = todas las cuentas de esa
  plataforma, o un user_id ML / seller_id Amazon específico).
- `get_reply_templates(platform, account_id)`: con `account_id` dado
  (viene del hilo activo que se está respondiendo), incluye las de "todas
  las cuentas" + las de esa cuenta exacta, EXCLUYE las atadas a otra cuenta
  distinta. Sin `account_id` (modo gestión, sin hilo activo) lista todas,
  para no esconder nada al editar/borrar.
- Nuevo `GET /api/reply-templates/accounts` — lista ML+Amazon para poblar
  el selector de cuenta en el formulario.
- Modal compartido: selector de cuenta que se repuebla según la plataforma
  elegida (deshabilitado si es "ambas plataformas"), badge de alcance en
  cada plantilla de la lista (nickname de cuenta o "todas las cuentas").
- `openTemplatesModal`/`insertTemplateInto` ahora reciben también
  `accountId` — actualizado en los 2 call sites (ML y Amazon) para pasar
  la cuenta activa del hilo que se está respondiendo.

Verificado en local con cuentas reales: una plantilla atada a BLOW aparece
al responder en BLOW pero NO en APANTALLATEMX; una plantilla "todas las
cuentas ML" aparece en ambas; el modo gestión (sin cuenta activa) las ve
todas.

---

## 2026-07-27 — FIX: mensajes de compradores Amazon se quedaban truncados sin forma de ver el resto

**Archivos:** `app/static/js/amazon_dashboard.js` (`threadHtml`).

Jovan reportó un mensaje de solicitud de CFDI (con RFC, código postal, etc.)
cortado a la mitad ("Código Pos...") sin poder ver el resto para responder.
Causa: el botón "Ver conversación completa" (que revela el texto íntegro,
sin el corte de 180 caracteres del preview) solo aparecía si el hilo tenía
MÁS DE 1 mensaje. Un hilo nuevo con un solo mensaje largo — el caso más común,
primer mensaje de un comprador antes de cualquier respuesta — nunca mostraba
ese botón, dejando el texto truncado permanentemente. Revisado el lado ML
(`health_messages.html`) y no tiene este patrón, no aplica ahí.

Fix: el botón ahora también aparece cuando el preview está truncado,
sin importar cuántos mensajes tenga el hilo ("Ver mensaje completo" para 1
solo mensaje, "Ver conversación completa (N mensajes)" para varios).

---

## 2026-07-27 — FIX: fotos de reclamo no se bajaban si el comentario ya venía del sync + DECISION: comprimir fotos y subir presupuesto de disco

**Archivos:** `app/main.py` (`_compress_claim_photo`, `sku-claims-detail`,
`claim-photo-proxy`, nuevos diags `inspect-claim` y `claim-photos-capacity`),
`requirements.txt` (Pillow).

Jovan reportó un caso puntual con orden/reclamo reales (2000017397188926 /
5547962959, cuenta BLOW): ML sí muestra 3 fotos + texto del comprador en el
hilo, nuestro dashboard solo traía el texto. Diagnosticado con el endpoint
`inspect-claim`: `get_claim_messages` en vivo SÍ trae los 3 attachments,
`buyer_comment` SÍ estaba bien guardado, pero `claim_photos` estaba vacío.

**Causa raíz:** `_save_ml_claims_bg` (sync automático de fondo, cada hora)
llena `buyer_comment` pero A PROPÓSITO nunca baja fotos (por el incidente de
disco lleno de 2026-07-15). `sku-claims-detail` solo intentaba bajar fotos
para reclamos con comentario VACÍO — en cuanto el sync de fondo le ganaba la
carrera al usuario abriendo el SKU, el reclamo quedaba marcado "resuelto" y
sus fotos nunca se volvían a intentar. Fix: la lista de reclamos a
reprocesar ahora también incluye los que ya tienen comentario pero CERO
fotos guardadas.

**Pregunta de seguimiento de Jovan:** pidió bajar TODAS las fotos y guardar
1 mes completo, con estimación de capacidad, o alternativa de comprimir si
no alcanzaba. Con datos reales vía el nuevo diag `claim-photos-capacity`:
DB en 309MB de los 500MB del volumen de Railway (solo ~190MB libres), ~850
reclamos ML/mes (4 cuentas), ~64% tipo "Defectuoso" (los que suelen traer
foto), fotos originales ~1.3MB promedio → guardar 1 mes sin comprimir
hubiera necesitado 500MB-1GB+, imposible en el espacio disponible (un
presupuesto de solo 120MB SIN comprimir ya había tumbado el disco el
2026-07-18).

**Decisión implementada:** comprimir. Nueva `_compress_claim_photo()` —
redimensiona a máx. 1280px de lado largo + recodifica JPEG calidad 75 antes
de guardar (si Pillow falla por cualquier razón, guarda el original sin
comprimir, nunca se pierde la foto). Reducción real ~10-27x según contenido.
Presupuesto de disco subido de 40MB a 80MB — con fotos ~10x más chicas, esto
cubre 1 mes completo con margen, sin tocar el tamaño del volumen de Railway.
Pillow agregado a `requirements.txt` (antes solo estaba disponible local por
una dependencia transitiva, no garantizado en el build de Railway).

**Pendiente de limpiar:** quedan 3 endpoints de diagnóstico temporales vivos
en producción (`/api/diag/inspect-claim`, `/api/diag/claim-photos-capacity`,
`/api/diag/fix-claims-account-id`) — se eliminarán cuando se cierre por
completo el hilo de investigación de Retornos.

---

## 2026-07-27 — FIX: Análisis de IA de retornos se truncaba y caía al fallback genérico

**Archivos:** `app/main.py` (`/api/returns/ai-analysis`).

Jovan probó el popup ya funcionando (fix de arriba) y reportó que "no trae
toda la información" — el bloque "Patrón Detectado" mostraba JSON crudo
cortado a la mitad, y "Recomendaciones" solo tenía el placeholder genérico
"Revisar análisis completo". Causa: el prompt pide 8 secciones (root_cause,
pattern, severity, quality_score, recommendations x3, listing_improvements x2,
prevention_checklist x2, summary_whatsapp, priority_action) pero `max_tokens`
estaba en 1200 — insuficiente, los modelos gratuitos de OpenRouter cortaban el
JSON a la mitad, `json.loads` fallaba, y el código caía al fallback que
muestra el texto crudo truncado como si fuera el análisis real.

Subido `max_tokens` a 2200 + extracción de JSON más robusta (busca el bloque
`{...}` más externo con regex, no solo pela fences \`\`\`json, por si el
modelo agrega texto extra pese a la instrucción). Verificado en local con el
SKU real del reporte (SNTV008016) — antes se truncaba, ahora regresa las 8
secciones completas y bien formadas.

**Nota aparte, no corregida:** en una de varias pruebas locales un modelo
gratuito del cascade devolvió acentos con mojibake (`patrÃ³n` en vez
de `patrón`) — no se repitió en 3 intentos más, parece un problema puntual de
ese modelo específico, no sistemático. Queda documentado por si reaparece.

---

## 2026-07-27 — FEAT+FIX: Comentarios/fotos por cuenta en `/returns` + bug de botones que no respondían

**Archivos:** `app/main.py` (`/api/returns/sku-claims-detail` acepta `account_id`
opcional), `app/templates/returns.html`.

Jovan pidió que el "Ranking rápido" de `/returns` (por cuenta) tuviera el mismo
análisis de comentarios/fotos/IA que ya tenía el widget Global — hoy solo
mostraba el análisis de IA (quality score, causa raíz, etc.) pero nunca los
comentarios ni fotos de compradores. Se extendió `/api/returns/sku-claims-detail`
con un `account_id` opcional (sin él sigue igual — usado por el widget Global de
`/multi-dashboard`; con él, filtra a una sola cuenta, nunca mezcla — CLAUDE.md
regla #4) y el modal de "Analizar IA" ahora también carga esa sección debajo
del análisis, filtrada a la cuenta activa y al mismo rango de fechas del
período analizado.

**Bug encontrado al probarlo (no relacionado con el feature de arriba):**
ningún botón de "Ranking rápido" (Analizar IA, Compartir, etc.) respondía al
clic, en NINGUNA fila. Causa: el JSON de `reasons` se insertaba crudo dentro
del atributo `onclick="..."` — `JSON.stringify` siempre trae comillas dobles
(ej. `{"Defectuoso/Diferente":3}`), y el navegador corta el atributo
doble-comillado en la primera comilla del JSON, truncando el `onclick` →
`SyntaxError: Invalid or unexpected token` en consola y el botón no hacía
nada — en TODA fila, porque todo producto tiene al menos un `reason`. El
título ya escapaba comillas dobles a `&quot;` pero se les olvidó aplicar lo
mismo a `_encReasons`, en las dos rutas donde se genera (fila del ranking +
botón "Compartir" del modal de IA). Reproducido con un script standalone
(`new Function()` sobre el JS resultante) antes y después del fix — antes
lanza el mismo SyntaxError, después parsea limpio.

**Pendiente de decidir (mismo patrón, no tocado hoy):** se encontraron 2 lugares
más con el mismo patrón de riesgo (`JSON.stringify(...)` crudo dentro de un
`onclick="..."`, sin escapar comillas) — `stock_sync.html:2205`
(`applyCoverageAlert`) y `planning.html:1109` (`_copyNoSkuList`). No se tocaron
porque son páginas/features distintas a las de hoy — están fuera de alcance de
este fix, quedan documentados para decidir si se corrigen.

---

## 2026-07-27 — FIX: Retornos Global — `account_id` legacy + modal de comentarios desincronizado del conteo

**Archivos:** `app/main.py` (endpoint `/api/diag/fix-claims-account-id`, reemplaza al
temporal `/api/diag/inspect-sku-claims`), `app/templates/multi_dashboard.html`
(`loadRetDetailComments`).

Jovan reportó (con screenshot) que el modal de detalle de "Top Retornos Global"
mostraba muchos reclamos con "Sin comentario del comprador" pese a haber plática
real (incluso con foto) con el comprador. Pidió analizar bien y presentar un plan
antes de tocar nada — se investigó primero, se presentó el diagnóstico, y se
implementó tras su aprobación ("vamos con lo que es mejor").

**Causa raíz 1 — `account_id` legacy:** la primerísima versión de
`_save_ml_claims_bg` (2026-07-15) guardaba `account_id` = nickname de la cuenta
("BLOWTECHNOLOGIES") en vez del user_id numérico. Se corrigió en el código al
día siguiente (commit `086614c`), pero los reclamos ya guardados con el dato
malo nunca se tocaron — como los syncs solo cubren una ventana reciente (30-180
días), esas filas viejas quedaron congeladas para siempre.
`get_meli_client(user_id="BLOWTECHNOLOGIES")` no encuentra token → devuelve
`None` → el backfill de comentario/foto en `sku-claims-detail` se salta esas
filas en silencio, sin error visible. Se corrió una limpieza única en
producción: **2,296 filas corregidas** (BLOWTECHNOLOGIES 1052, APANTALLATEMX
837, LUTEMAMEXICO 174, AUTOBOT MEXICO 233) — el backfill normal ahora las toma
solas la próxima vez que alguien abra ese SKU.

**Causa raíz 2 — modal con dos alcances distintos sin avisar:** el encabezado
del modal muestra el conteo scoped a `days` del widget (ej. "25 retornos
totales"), pero `loadRetDetailComments` llamaba a `sku-claims-detail` SIN fecha,
trayendo hasta 200 reclamos de TODO el historial (95 en el caso reportado) —
mostrando muchos más comentarios de los que el encabezado prometía, la mayoría
reclamos viejos fuera de ventana (más propensos al bug #1). Ahora pasa el mismo
rango de fechas que usa el widget, igual que ya hacía `searchRetSku`.

**No es bug (documentado, sin cambio):** las fotos de reclamo se borran de disco
pasados 30 días (`_CLAIM_PHOTOS_MAX_AGE_DAYS`) — medida deliberada tras el
incidente de disco lleno del Railway Volume (2026-07-15). Con el fix #2 el
detalle ya no mezcla reclamos tan viejos, así que se ven menos "0 fotos", pero
la política de 30 días se deja igual a propósito.

Confirmado: `returns.html` (ML por cuenta) y `amazon_returns.html` (Amazon por
cuenta) ya tienen vistas equivalentes o más ricas (Quality Score, Ranking
rápido, Top SKUs, Timeline, Comentarios) — no hace falta duplicar nada ahí, el
problema real estaba en el widget Global.

---

## 2026-07-27 — FIX: `/api/diag/gmail-setup-filter` tenía el nombre de etiqueta fijo en "Vektor Amazon"

**Archivos:** `app/main.py`.

Jovan pidió aplicar a AUTOBOT y ExclusiveBulbs el mismo filtro de Gmail
(etiqueta + archivar automático de correos de Amazon) que ya tenía
VECKTOR, para mantener limpia la bandeja de cada cuenta. El endpoint que
lo crea vía API de Gmail tenía el nombre de la etiqueta hardcodeado a
"Vektor Amazon" — se hubiera creado esa misma etiqueta (incorrecta) en
las otras 2 cuentas. Corregido para derivar el nombre del nickname real
de cada cuenta (`AMAZON_BUYER_INBOX_ACCOUNTS`).

---

## 2026-07-25 — FEAT: Bandeja unificada de Mensajes (ML + Amazon) + Plantillas de respuesta

**Archivos:** `app/main.py`, `app/api/health.py`, `app/services/token_store.py`,
`app/templates/health.html`, `app/templates/partials/health_messages.html`,
`app/templates/amazon_dashboard.html`, `app/static/js/amazon_dashboard.js`,
`app/templates/base.html`, `app/templates/partials/reply_templates_modal.html` (nuevo).

Jovan pidió poder ver y responder mensajes pendientes de TODAS las cuentas
de una plataforma sin tener que cambiar de cuenta una por una, con ML y
Amazon separados (para no confundir de dónde viene cada mensaje) y
respuestas rápidas/plantillas reusables. Sin quitar la vista por cuenta
que ya existía — es 100% adicional, con un toggle "Esta cuenta / Todas
las cuentas".

### ML — antes NO era seguro, ahora sí
Investigación previa confirmó que `send_message`/`take_message`/
`update_message_status` (ML) resolvían la cuenta 100% desde la cookie
`active_account_id` vía `_active_user_id` ContextVar — nunca por un
parámetro explícito. Responder un mensaje de la Cuenta B mientras la
Cuenta A estaba "activa" en el navegador hubiera usado el `MeliClient`
equivocado. Se agregó `account_id` opcional a los 3 endpoints
(`app/api/health.py`) — si no se manda, se comporta exactamente igual
que antes (compatibilidad total con la vista por cuenta).

- `_fetch_enriched_ml_conversations()` (extraído de `health_messages_partial`,
  sin duplicar lógica) + `GET /partials/health-messages-unified`: fan-out
  sobre las 4 cuentas ML vía `get_meli_client(user_id=uid)` (mismo patrón
  ya usado en `_compute_unified_returns`), cada conversación tageada con
  `account_id`/`account_nickname`.
- Cada `msg-conv-card` lleva `data-account-id` — Tomar/Resolver/Enviar
  ahora se lo pasan a los 3 endpoints en vez de depender de la cookie.

### Amazon — el endpoint de responder YA era seguro; Tomar/Resolver necesitaban el fix
`POST /api/amazon/buyer-messages/{message_id}/reply` ya resuelve todo por
`message_id` (lee `seller_id` de la fila en BD) — cero cambios ahí. Pero
`takeAmzThread`/`setAmzThreadStatus` (frontend) mandaban
`window.amzActiveSellerId` (la cuenta activa global) en vez del
`seller_id` real del hilo — se corrigió leyendo `data-seller-id` de la
tarjeta del hilo.

- `_fetch_amazon_threads_for_seller()` + `_compute_amazon_thread_stats()`
  (extraídos de `amazon_buyer_messages_list`) + `GET
  /api/amazon/buyer-messages-unified`: fan-out sobre las 3 cuentas Amazon,
  cada thread tageado con `seller_id`/`seller_nickname`.

### Plantillas de respuesta (nuevo, compartido ML + Amazon)
Tabla `reply_templates` (label, body_text, platform 'ml'/'amz'/'all') +
CRUD (`GET/POST /api/reply-templates`, `DELETE /api/reply-templates/{id}`)
siguiendo el mismo patrón que `seasonal_events`. Modal compartido
(`partials/reply_templates_modal.html`, incluido una vez en `base.html`)
usable desde cualquier textarea de respuesta — botón "📋 Plantillas" junto
al de "Sugerir con IA" en ambas plataformas, más un botón de solo-gestión
en la barra de filtros de Mensajes.

### Verificado localmente contra datos reales
Bandeja ML unificada mostró conversaciones reales de 3 cuentas distintas
(AUTOBOT MEXICO, BLOWTECHNOLOGIES, LUTEMAMEXICO) en una sola lista con su
badge de cuenta correcto. Bandeja Amazon unificada: 50 mensajes/5 hilos
pendientes reales agregados. Permisos verificados: usuario sin acceso a
Salud→Mensajes → 403 en ambos endpoints unificados; usuario con acceso →
200. JS de ambas plataformas + el modal de plantillas pasan `node --check`.

---

## 2026-07-25 — BUG FIX real: AUTOBOT/ExclusiveBulbs no podían responder mensajes ("unauthorized_client")

**Archivos:** `app/config.py`, `app/services/buyer_messages_client.py`.

Al crear los clientes OAuth `_2`/`_3` (ver entrada de ayer, "segundo/tercer
cliente OAuth de Gmail") solo se actualizó el flujo de **conexión**
(`/auth/gmail/connect` en `auth.py`) para usar el client_id/secret correcto
de cada cuenta — pero **el envío real de respuestas**
(`buyer_messages_client.py:_gmail_access_token`, llamado por `send_reply()`
en cada mensaje contestado) seguía usando el ÚNICO cliente original
(el de VECKTOR) para las 3 cuentas. Un refresh_token solo es válido bajo
el client_id/secret que lo emitió — usar el de VECKTOR para renovar el
token de AUTOBOT/ExclusiveBulbs da exactamente `401 unauthorized_client`,
el error reportado.

Fix: cada entrada de `AMAZON_BUYER_INBOX_ACCOUNTS` (`config.py`) ahora
carga también su propio `gmail_client_id`/`gmail_client_secret` (el que
corresponde a su refresh_token), y `_gmail_access_token()`/
`setup_organization_filter()` los reciben como parámetro en vez de usar
las constantes globales. Verificado localmente: las 3 cuentas renuevan
su access_token de Gmail correctamente con su propio cliente.

---

## 2026-07-24 — FIX preventivo: blindar cuentas Amazon contra la misma carrera de datos que ya afectó a ML

**Archivos:** `app/services/token_store.py`, `app/main.py`.

Arely reportó ver el nickname de AUTOBOT en el banner al tener VECKTOR
seleccionado. Investigación a fondo (código completo del flujo cookie →
`_accounts_ctx` → ruta → template → dropdown, más consulta directa a la
DB) **no encontró un bug determinístico reproducible** — la DB tenía el
nickname correcto en cada cuenta Amazon en el momento de revisar, las 2
cookies (`active_account_id` ML / `active_amazon_id` Amazon) están
correctamente separadas sin ningún cruce en el código, y el formulario
de cada fila del dropdown solo puede mandar su propio `seller_id`.

Sí se confirmó un hueco real: el lado Amazon **no tenía ninguna
protección** contra la misma causa que sí se confirmó y arregló hoy más
temprano para ML (`202867e` — Railway borra el SQLite en cada redeploy,
uvicorn acepta requests antes de que el re-seed de fondo termine, una
cuenta puede quedar con nickname vacío momentáneamente). Se aplicó el
mismo patrón preventivamente:

- `KNOWN_AMAZON_NICKNAMES` + `_with_amazon_nickname_fallback` en
  `token_store.py`, aplicado en `get_amazon_account`/
  `get_all_amazon_accounts` — igual que `KNOWN_ML_NICKNAMES` para ML.
- `Cache-Control: no-store` en las 4 páginas Amazon (`/amazon`,
  `/amazon/products`, `/amazon/orders`, `/amazon/returns`) — su
  contenido depende de qué cuenta esté activa (cookie), no deberían
  cachearse en el navegador; una de las hipótesis no descartadas era que
  el banner mostrara una copia vieja del DOM.
- Log de advertencia en `_accounts_ctx` cuando la cookie `active_amazon_id`
  no coincide con ninguna cuenta cargada y se cae a la primera — antes
  pasaba en silencio, ahora queda registrado para poder correlacionar si
  se repite.

No se puede confirmar al 100% que esto haya sido la causa exacta de lo
que vio Arely, pero cierra el hueco estructural real que sí se confirmó,
con el mismo patrón ya probado hoy para ML.

---

## 2026-07-24 — FIX: "Marcar todo como atendido" (Mensajes Amazon) restringido a admin

**Archivos:** `app/templates/amazon_dashboard.html`, `app/main.py`.

Arely reportó que veía este botón (borra de un jalón el historial acumulado
de mensajes) sin ser admin — Jovan pidió que solo lo vea el rol admin.
Ocultado en el template (`{% if dashboard_user.role == 'admin' %}`) Y
reforzado en el endpoint `POST /api/amazon/buyer-messages/mark-all-resolved`
(antes solo exigía estar logueado, cualquier rol podía llamarlo
directamente aunque no viera el botón).

---

## 2026-07-24 — FEAT: consolidación de tabs Amazon (Finanzas/Listings/Deals/Operaciones → subtabs)

**Archivos:** `app/templates/amazon_dashboard.html`, `app/templates/amazon_products.html`,
`app/static/js/amazon_dashboard.js`, `app/api/amazon_products.py`,
`app/templates/partials/amazon_products_listings.html` (nuevo),
`app/templates/partials/amazon_products_deals.html` (nuevo),
`app/services/user_store.py`, `app/main.py`.

Análisis pedido por Jovan: revisar todos los tabs de Amazon y ML para ver
cuáles se pueden fusionar como subtabs de otro y reducir el nav. ML ya
estaba consolidado (Listings/Deals dentro de Productos, Finanzas dentro de
Ventas, Distribución dentro de Sync Stock); Amazon tenía 4 candidatos para
copiar el mismo patrón:

- **Finanzas → subtab de Ventas** (💰 Finanzas, 3ra sub-vista junto a
  Resumen/Por SKU) — mismo patrón que ML's Ventas ya usa.
- **Listings + Deals → subtabs de Productos** (`/amazon/products`) — mismo
  patrón que ML's Productos ya usa. Cross-page: se crearon 2 shells
  estáticos server-side (`amazon_products_listings.html`/`_deals.html`,
  endpoints `GET /api/amazon/products/listings`/`/deals`) que solo montan
  el HTML — el fetch/render real lo siguen haciendo las MISMAS funciones
  JS de siempre (`loadListingsTab`/`loadDealsTab`/etc, sin reescribir
  nada), reusadas incluyendo `amazon_dashboard.js` en la página de
  Productos. Ese archivo tenía código de "carga inicial" sin guardar que
  hubiera roto la página de Productos (referencias a elementos que solo
  existen en `/amazon`) — se guardó detrás de
  `if (document.getElementById('amz-tab-dashboard'))`.
- **Operaciones (huérfano, sin nav propio, solo alcanzable por links de
  alertas) → subtab "Catálogo" de FBA & Stock** (junto a "Reabastecimiento",
  el contenido que ya tenía FBA).

Amazon pasó de 11 tabs de nivel superior a 8 (Dashboard, Ventas, Productos,
Salud, Retornos, Planeación, Facturación, FBA & Stock), quedando
estructuralmente simétrico con ML.

**Permisos**: `PERMISSION_TREE["amz"]` actualizado — `ventas.subtabs` gana
`finanzas`, `productos.subtabs` gana `listings`/`deals` (antes `None`),
`fba.subtabs` gana `reabastecimiento`/`catalogo` (antes `None`);
`finanzas`/`listings`/`deals`/`operaciones` desaparecen como tabs propios
del árbol. Migración de claves viejas (emitidas el mismo día, antes de
esta consolidación) vía `_LEGACY_AMZ_TAB_MAP`. Los botones de subtab de
Ventas y FBA ahora sí respetan permisos (antes de este cambio se
calculaba el contexto pero nunca se usó para ocultar botones — bug
encontrado al probar con un usuario restringido a un solo subtab: se
veían los 3 botones de Ventas en vez de solo el permitido). Corregido
igual que Salud: subtabs no permitidos no se renderizan, el subtab
default (primero permitido) se muestra sin `hidden` y su botón sale
resaltado, y el JS de "carga inicial" dispara el loader correcto según
`window.amzVentasDefaultSubtab`/`window.amzFbaDefaultSubtab` en vez de
asumir siempre Resumen/Reabastecimiento.

Compat: `/amazon?tab=finanzas|operaciones` redirige a
`/amazon?tab=ventas|fba`; `/amazon?tab=listings|deals` redirige a
`/amazon/products` (sin selección automática de subtab en el redirect,
limitación aceptada — caso raro de links viejos).

Verificado localmente: usuario admin ve los 3 subtabs de Ventas y los 2 de
FBA + Listings/Deals en Productos; usuario restringido a un subtab
específico solo ve ese botón y ese panel visible; los divs viejos
(`amz-tab-operaciones`/`amz-tab-finanzas`/`amz-tab-listings`/`amz-tab-deals`)
ya no existen en el HTML; JS de ambas páginas pasa `node --check`.

---

## 2026-07-24 — FEAT+FIX: Mensajes de Compradores Amazon — Fase 2 (AUTOBOT + ExclusiveBulbs)

**Archivos:** `app/config.py`, `app/auth.py`, `app/services/buyer_messages_client.py`.

Se completó la Fase 2 pendiente (ver `.claude/memory/project_amazon_buyer_messages_plan.md`):
conectar los buzones dedicados de AUTOBOT AMZ MX y ExclusiveBulbs, igual que
VECKTOR (Fase 1).

### Bloqueante nuevo: 1 solo cliente OAuth de Gmail no alcanzaba
El cliente OAuth original (VECKTOR) quedó en modo "Testing" bajo una cuenta
de Google que no se pudo recuperar/identificar a tiempo — cualquier Gmail
nuevo agregado a "Test users" ahí habría funcionado, pero sin acceso a esa
cuenta no se podía. Se resolvió creando un proyecto de Google Cloud nuevo
por cada cuenta (uno para AUTOBOT, otro para ExclusiveBulbs) en vez de
insistir en rastrear la cuenta original. `_gmail_oauth_client_for(env_var)`
en `auth.py` generaliza el flujo OAuth para elegir el Client ID/Secret
correcto según qué cuenta se está autorizando (`GMAIL_OAUTH_CLIENT_ID_2/3`
+ `_SECRET_2/3` en Railway) — Vecktor sigue con el cliente original,
intacto.

### Bug real encontrado en producción: parser de mensajes solo reconocía español
AUTOBOT y ExclusiveBulbs también tenían activo **Replyco** (servicio de
terceros) en el campo "Reply-To Email"/Customer Service de Seller Central —
Jovan confirmó migrar completamente a nuestro sistema, se actualizó ese
campo a los Gmail dedicados en las 3-4 tiendas de cada cuenta.

Al conectar ExclusiveBulbs (vende en MX/CA/US/BR) apareció un bug real:
`_MSG_RE` en `buyer_messages_client.py` solo reconocía el formato en
español que trae VECKTOR ("Mensaje:"/"Finalizar mensaje") — verificado
contra 200 mensajes reales del buzón de ExclusiveBulbs, el parser
descartaba silenciosamente TODOS (0/200) porque Amazon manda esta
notificación en el idioma de cada marketplace: inglés ("Message"/"End
message"), portugués ("Mensagem"/"Encerrar mensagem"), y una variante en
español distinta a la de Vecktor ("Iniciar mensaje"/"Finalizar mensaje").
Corregido con alternancia de idiomas en el regex + un patrón de respaldo
(`_MSG_FALLBACK_RE`) para mensajes que traen el cierre pero no el marcador
de apertura. Verificado en vivo: 0/200 → 191/200 (95.5%) parseados
correctamente contra el buzón real. Quedan ~9 casos raros sin resolver
(mensajes sin referencia de producto/orden y sin marcador de apertura, o
correos sin parte de texto plano) — documentados en el código, no se
persiguieron más por rendimiento decreciente.

### Estado final
Las 3 cuentas Amazon (VECKTOR, AUTOBOT, ExclusiveBulbs) con Mensajes de
Compradores completo: lectura (IMAP) + respuesta (Gmail API vía OAuth) +
sin Replyco.

---

## 2026-07-24 — FIX DE RAÍZ: cuentas ML mostrando ID numérico en vez de nickname (recurrente)

**Archivos:** `app/services/token_store.py`, `app/services/meli_client.py`, `app/main.py`.

### Por qué seguía pasando después de "arreglarlo" la vez anterior
La vez pasada solo corrí el diag endpoint (`/api/diag/refresh-ml-tokens`) para
reparar el síntoma en caliente. La causa real nunca se tocó: existían **3
implementaciones distintas** de "refrescar token + traer nickname de la API de
ML + guardar", cada una copy-pasteada por separado:
1. `main.py:_seed_one`/`_backfill_nickname` (arranque, con fallback a un
   diccionario de nicknames conocidos + reintentos 12 min).
2. `meli_client.py:_auto_seed_from_env` (path "de emergencia", disparado por
   CUALQUIERA de los 225 call sites de `get_meli_client()` — **sin fallback
   alguno**, si la llamada a `/users/{id}` fallaba el nickname quedaba `""`
   para siempre).
3. El diag endpoint manual (una tercera copia, con fallback pero solo
   ejecutable a mano).

Railway borra el SQLite en cada redeploy. Uvicorn acepta requests ~2s después
de arrancar, mientras el seeding de fondo apenas empieza — cualquier request
real en esa ventana dispara el path #2 (sin red de seguridad) en paralelo al
path #1, duplicando las llamadas a la API de ML justo cuando es más probable
que rate-limite (429). Además, `_seed_tokens_with_retry` daba por "completo"
el seeding con solo contar filas en la tabla, sin verificar que tuvieran
nickname — así que ni siquiera reintentaba por esto.

### Fix de raíz (no otro parche manual)
- `token_store.KNOWN_ML_NICKNAMES` — única fuente de verdad (antes vivía
  duplicado en `main.py`), con fallback aplicado directamente dentro de
  `get_tokens`/`get_any_tokens`/`get_all_tokens` — **el dropdown nunca más
  puede mostrar un ID crudo para una de las 4 cuentas conocidas**, sin
  importar qué tan mal salga el seeding de esa corrida (defensa en la capa de
  lectura, no depende de que el seeding haya sido perfecto).
- `_auto_seed_from_env()` (el path sin red de seguridad) ahora también aplica
  el mismo fallback al escribir en DB — arregla el dato en origen, no solo en
  la lectura.
- `_seed_tokens_with_retry()` ahora exige nicknames completos (no solo el
  conteo de filas) para darse por terminado — sigue reintentando si falta
  alguno.
- `_nickname_healing_loop()` (nuevo): barrido cada 5 min, para siempre (no
  solo los primeros 12 min tras el arranque) — cubre el caso de una cuenta
  NUEVA (no en el diccionario conocido) cuyo nickname falle justo en el
  rate-limit del arranque; antes se quedaba así permanentemente hasta un
  diag manual.

### Verificado localmente
Fila con nickname vacío forzado por SQL directo (bypaseando toda la lógica
de guardado) → `get_tokens`/`get_all_tokens` devuelven el nickname correcto
igual. Servidor arranca sin errores con los 3 archivos modificados.

---

## 2026-07-23 — FEAT: Permisos jerárquicos por tab/subtab (ML + Amazon) + fix bug de redirección

**Archivos:** `app/services/user_store.py`, `app/main.py`, `app/api/users.py`,
`app/api/metrics.py`, `app/templates/health.html`,
`app/templates/amazon_dashboard.html`, `app/templates/usuarios.html`,
`app/static/js/amazon_dashboard.js`.

### Bug reportado
Jorge Sepúlveda con acceso otorgado a Amazon era redirigido a `/health`
(ML) al intentar ver Salud/Mensajes de Compradores de Amazon. Causa
raíz: el esquema viejo de permisos era una lista plana de "secciones" —
`"salud"` (ML) y `"amazon"` (TODO Amazon, sin distinguir tabs internos)
eran independientes; sin `"amazon"` marcado, cualquier vista de Amazon
rebotaba. Además, los permisos van embebidos en el JWT de sesión — un
cambio de permisos no aplicaba hasta que el usuario recargaba sesión.

### Rediseño (pedido explícito: permisos por tab, con drill-down a subtabs,
para ambas plataformas)
- `user_store.PERMISSION_TREE`: árbol `{ml: {tab: {label, subtabs}}, amz: {...}}`
  — cubre Dashboard/Ventas/Productos/Ads/Salud/Devoluciones/Planning/
  Facturación/Sync (ML) y Dashboard/Ventas/Productos/Salud/Operaciones/
  Finanzas/FBA/Listings/Deals/Retornos (Amazon), con subtabs reales donde
  existen (ej. ML Salud: claims/questions/messages/reputation/vigilancia/
  scores; Amazon Salud: resumen/mensajes/vigilancia).
- Claves nuevas tipo `"ml.salud"` (tab completo) o `"ml.salud.messages"`
  (solo ese subtab) reemplazan las claves planas viejas. Migración
  transparente (`_expand_legacy_sections`): claves viejas como `"salud"`
  o `"amazon"` se expanden al vuelo a las nuevas — sin script de
  migración de DB, sin romper usuarios ya configurados.
- `has_tab_access`/`has_subtab_access`/`get_allowed_subtabs`/
  `first_allowed_location`: helpers de chequeo, usados por
  `AuthMiddleware` (gating de página completa + `/amazon?tab=`),
  `_build_nav_tabs` (filtro del nav) y `_require_subtab` (gating de
  partials/endpoints de subtabs individuales — antes NINGÚN partial
  tenía gating, solo la página contenedora).
- Gating de subtab aplicado end-to-end en Salud (ML: 5 partials + scores;
  Amazon: buyer-messages, vigilancia, amazon-health-data). Otros tabs con
  subtabs (Productos/Ads/Sync en ML, Ventas en Amazon) ya están
  representados en el árbol y en el panel de usuarios con la misma
  granularidad tab/subtab — su gating a nivel de endpoint individual
  queda pendiente si se necesita (mecanismo ya existe, es repetir el
  patrón de `_require_subtab`).
- `health.html`/`amazon_dashboard.html`: solo se renderizan los botones
  de subtab permitidos; el subtab por defecto al entrar es el primero
  permitido (no siempre "Reclamos"/"Resumen").
- `usuarios.html`: checkboxes rediseñados como árbol (ML/Amazon ×
  tab, con "▸ subtabs" expandible) en vez de la grilla plana de 10
  secciones — fuente única de verdad (`PERMISSION_TREE`) inyectada desde
  el backend, no duplicada en JS.
- **Fix de staleness de JWT**: `user_store.update_user()` ahora invalida
  las sesiones activas del usuario (`delete_user_sessions`) cuando cambia
  `role` o `allowed_sections` — un cambio de permisos aplica de inmediato
  (el usuario debe re-loguearse), en vez de esperar hasta 30 días.

### Verificado localmente (JWT sintético + curl, 8 escenarios)
Usuario restringido a `["ml.salud.messages","amz.salud.mensajes"]`:
`/health` 200 (solo botón Mensajes), `/partials/health-messages` 200,
`/partials/health-claims` 403, `/amazon?tab=salud` 200,
`/amazon?tab=ventas` → redirect a `/health`, `/api/amazon/buyer-messages`
200, `/api/metrics/amazon-health-data` 403, `/api/amazon/vigilancia` 403.
Admin y usuario sin restricción: acceso completo sin cambios.

---

## 2026-07-23 — FEAT: Listing Quality Score dinámico ML+Amazon (Feature 1/4 de "ideas Helium10")

**Archivos:** `app/api/lanzar.py`, `app/api/amazon_products.py`,
`app/services/token_store.py`, `app/main.py`.

Jovan mandó 9 links de Helium10 (suite de vendedores Amazon) y pidió
analizar qué tomar para AMBAS plataformas, integrado en tabs que ya
existen (sin tabs nuevos). Consultado con `amazon-specialist` +
`mercadolibre-strategist` en paralelo. Investigación de código confirmó
que Listings ML y Amazon YA tienen un quality score (no se creó nada
desde cero) — el hueco real, señalado por ambos especialistas: el score
era 100% estático (título/fotos/atributos/precio), sin señales de
mercado (precio vs competencia, stock, reclamos).

- **ML** (`lanzar.py:_process_item_body` + bloque de persistencia):
  estático reescalado 100→70, + `stock_score` (0/7/15, BM real vía
  `_bm_stock_cache`), `price_comp_score` (0/5/10, reusa
  `ml_competition_alerts` ya calculado, sin duplicar el cálculo),
  `claims_score` (0/5, reclamos abiertos en `claims_history.status=
  'opened'`). 3 columnas nuevas en `ml_listing_quality` para guardar el
  desglose (migración `ALTER TABLE` con el patrón try/except ya usado).
- **Amazon** (`amazon_products.py:/listing-quality`): estático reescalado
  100→85 + `stock_score` (0/7/15, mismo `_bm_stock_cache` vía import
  dinámico para evitar ciclo con main.py). `price_comp_score` (depende
  de Buy Box, aún no implementado — Feature 3 de este mismo roadmap) y
  `claims_score` (depende de cruzar con Retornos) quedan pendientes a
  propósito, documentados en el código, no fingidos con un valor fijo.
- Sin tabs nuevos: se sigue mostrando en `/items?tab=listings` (ML) y
  `#amz-tab-listings` (Amazon) — la UI ya renderiza genéricamente
  `issues[]`/score, solo se agregaron mensajes nuevos ("Sin stock en
  BM", "Precio no competitivo", "Reclamo abierto").

Verificado: migración de columnas confirmada, fórmula reescalada
probada con datos de ejemplo (máx. estático 70 + dinámico 30 = 100,
nunca excede el tope).

---

## 2026-07-23 — FEAT: Mercado Ads — sugerencias de presupuesto (Feature 2/4 de "ideas Helium10")

**Archivos:** `app/main.py`, `app/templates/ads.html`,
`app/templates/partials/ads_suggestions.html`.

Hallazgo de la investigación: Mercado Ads YA es un tab completo y rico
(campañas, ROAS, IS%, Brand Ads, `update_campaign` con
`check-write-permission`) — no había que construir nada desde cero,
solo agregar una capa de sugerencias encima, como nuevo subtab
"Sugerencias" dentro de `ads.html` (mismo patrón `showTab`/`loadTab` que
las demás pestañas del tab, sin tabs nuevos de primer nivel).

- Reusa `_enrich_campaigns()` (mismos datos que la pestaña Campañas, sin
  llamadas extra a MeLi) — regla v1 sobre el rango de fechas
  seleccionado (no rolling de 3 días, no hay granularidad diaria por
  campaña sin fetches adicionales, documentado como simplificación):
  ROAS &lt; 85% de la meta → sugerir bajar presupuesto 15%;
  &gt;20% de impresiones perdidas por presupuesto Y ROAS en meta o sano
  → sugerir subir 15%.
- Nunca auto-aplica: botón "Aplicar" reusa el mismo
  `POST /api/ads/campaigns/{id}` que ya usa el resto del tab (con su
  gate de `check-write-permission` ya existente).
- Sin tabla nueva — se calcula en vivo en cada carga del subtab (barato,
  mismos datos que ya trae Campañas).

Verificado local contra datos reales de producción (VECKTOR): 5
sugerencias reales encontradas correctamente, Playwright 375px/1920px
0 overflow / 0 errores de consola.

---

## 2026-07-23 — FEAT: Vigilancia — posición ganadora + timeline de cambios (Feature 3/4 de "ideas Helium10")

**Archivos:** `app/services/token_store.py`, `app/services/meli_client.py`,
`app/services/amazon_client.py`, `app/main.py`,
`app/templates/health.html`, `app/templates/partials/health_vigilancia.html`,
`app/templates/amazon_dashboard.html`, `app/static/js/amazon_dashboard.js`.

La más nueva en términos de API de las 4 mejoras — 2 integraciones nunca
tocadas antes en este repo. Se verificó el formato REAL contra producción
antes de construir el parseo final (mismo cuidado que con el shipment de
ML en el round anterior):

- **ML — ganador de catálogo**: confirmado en vivo que un item con
  `catalog_product_id` se puede consultar vía
  `GET /products/{catalog_product_id}/items`, que regresa la lista de
  listings que compiten por ese producto — el que trae el tag
  `kvs_primary` es el que ML muestra por default. Con 0-1 competidores
  reales se considera ganador automático (no hay a quién perderle).
  Nuevo método `get_catalog_winner_status()`.
- **Amazon — Buy Box**: confirmado en vivo contra
  `GET /products/pricing/v0/items/{asin}/offers` (Product Pricing API) —
  cada oferta trae `SellerId` + `IsBuyBoxWinner`. Nuevo método
  `get_buy_box_status()`. **Bug encontrado y corregido durante la
  verificación**: el `pageSize` máximo de
  `/listings/2021-08-01/items/{sellerId}` es 20 (200 tira HTTP 400
  InvalidInput) — se agregó paginación real vía `pageToken` (hasta 5
  páginas = 100 SKUs de pool de rotación).
- Tablas nuevas `listing_snapshots` (snapshot actual: título/precio/
  imagen/si ganamos + `not_winning_since`) y `listing_change_log`
  (timeline append-only de cambios detectados). Comparación se hace en
  `sync_listing_snapshot()` antes de sobreescribir el snapshot.
- **Nunca revisa todo el catálogo de un jalón** — rotación LRU acotada a
  20 listings/cuenta/ciclo de 15 min (`get_snapshot_check_candidates()`,
  mismo cuidado de rate-limit que el resto de la app), vía
  `_check_ml_winner_status_bg()`/`_check_amazon_buy_box_status_bg()`
  enganchados al loop periódico ya existente.
- UI: nuevo subtab "Vigilancia" en Salud de AMBAS plataformas — ML
  (`health.html`, patrón `data-tab`/`loadTab()` ya existente) y Amazon
  (`amazon_dashboard.html`, patrón `amz-salud-subtab-*` ya existente de
  Mensajes de Compradores) — sin tabs nuevos de primer nivel.

Verificado: unit tests de detección de cambios/rotación LRU/recuperación
de "ganador", y corrida real contra producción (ML 20 listings
revisados, Amazon 20 ASINs revisados incl. 1 SKU real perdiendo Buy Box
detectado — SNTV005362-GRA3), Playwright 375px/1920px en ambas
plataformas, 0 overflow / 0 errores.

---

## 2026-07-23 — FEAT: Reembolsos FBA ya aprobados (Feature 4/4 de "ideas Helium10" — CIERRA la iniciativa)

**Archivos:** `app/services/amazon_client.py`, `app/main.py`,
`app/templates/amazon_returns.html`.

Última de las 4 mejoras — la de mayor incertidumbre de API, nunca antes
tocada en este repo. Idea tomada de Helium10 Managed Refund Service
(cobra 15-18% de comisión por presentar el reclamo) — se detecta gratis
con el mismo reporte que Amazon ya expone.

- Nuevo método `get_reimbursements_report()` (`amazon_client.py`), mismo
  patrón exacto que `get_returns_report()` (Reports API: request → poll
  → download → parse). **Verificado en vivo contra producción antes de
  construir el parseo final** (VECKTOR, 60 días): `GET_FBA_
  REIMBURSEMENTS_DATA` confirma columnas `approval-date, reimbursement-
  id, amazon-order-id, reason, sku, asin, product-name, amount-total,
  quantity-reimbursed-cash, ...`. Razones reales vistas: `CustomerReturn`,
  `Lost_Warehouse`, `Reimbursement_Reversal`, `CustomerServiceIssue`.
- Cache de 6h (mismo patrón que el reporte de devoluciones — generar el
  reporte es lento, ~1-2 min de polling).
- Endpoint `GET /api/amazon/returns/reimbursements` + nueva sección
  "Reembolsos FBA" en `amazon_returns.html` (mismo estilo que Comentarios
  de Clientes/Top SKUs ya existentes — sin tabs nuevos).
- **Alcance v1, documentado como tal**: solo muestra reembolsos YA
  aprobados por Amazon (monto, motivo, fecha) — NO cruza contra el
  Inventory Ledger para detectar inventario dañado/perdido que AÚN NO se
  ha reembolsado (requeriría un reporte adicional y lógica de cruce más
  compleja, queda fuera de esta ronda a propósito en vez de construir una
  reconciliación poco confiable bajo presión de tiempo).

Verificado end-to-end contra producción real (VECKTOR, 30 y 60 días): 45
y 99 reembolsos reales respectivamente, $121,477.80 y $297,265.15 MXN
recuperados, incluido el desglose "Lost_Warehouse" que es justo la señal
de inventario perdido que se buscaba. Playwright 375px/1920px, 0
overflow / 0 errores.

**Cierre de la iniciativa "ideas Helium10":** con esta, las 4 mejoras
identificadas al comparar contra Helium10 (con `amazon-specialist` +
`mercadolibre-strategist`) quedan implementadas y en producción: Listing
Quality Score dinámico, Mercado Ads sugerido, Vigilancia (Buy Box/
ganador + timeline), y Reembolsos FBA — todas integradas dentro de tabs
ya existentes, sin agregar tabs nuevos de primer nivel, tal como pidió
Jovan.

---

## 2026-07-23 — FEAT: Boost estacional automático al punto de reorden (Feature 1/4 de "ideas Zoho")

**Archivos:** `app/services/token_store.py`, `app/main.py`,
`generate_purchase_order.py`, `app/templates/stock_sync.html`,
`app/templates/partials/products_stock_issues.html`.

Primera de 4 mejoras inspiradas en comparar el sistema contra Zoho
Inventory (análisis con binmanager-specialist + planning-specialist).
Jovan ajustaba a mano el punto de reorden antes de Buen Fin/Hot Sale/
Navidad — ahora es configurable y se aplica solo.

- Tabla `seasonal_events` (nombre, fechas, `lead_days` de anticipación,
  multiplicador, categoría opcional por texto libre, activo/inactivo).
- `get_active_seasonal_boost()`/CRUD en `token_store.py`, junto a
  `stock_distribution_settings`.
- Inyectado en la recomendación de qty a sincronizar (`_rec_qty`/`_cap`,
  `main.py` dentro de `_do_prewarm()`) — multiplica el target de demanda
  proyectada mientras un evento esté vigente (con anticipación). Si hay
  varios eventos traslapados, gana el multiplicador más alto (nunca se
  suman). Badge nuevo indica "(temporada: nombre)" cuando aplica.
- También se aplica en `generate_purchase_order.py` (script standalone)
  vía `seasonal_boost()`, mismo criterio, consulta directa a SQLite sync.
- Endpoints `GET/POST /api/seasonal-events`, `DELETE
  /api/seasonal-events/{id}`.
- UI: nueva tarjeta "Eventos Estacionales" en Sync Stock → Configurar
  (crear/editar/eliminar eventos) + banner de transparencia en el Stock
  tab cuando un boost está activo ("📈 Boost de temporada activo: ...").
- Nunca toca disponibilidad publicada en ML/Amazon — solo sube la
  recomendación de compra/sync, que sigue siendo manual.

Verificado local: CRUD completo por curl, Playwright 375px/1920px sobre
Sync Stock → Configurar (crear evento, 0 overflow, 0 errores de consola).

---

## 2026-07-23 — FEAT: Bundles reales con precio propio (Feature 2/4 de "ideas Zoho")

**Archivos:** `app/services/token_store.py`, `app/main.py`,
`app/templates/stock_sync.html`.

El atajo actual para SKUs combinados ("SKU1 / SKU2" → toma solo el primer
componente) estaba duplicado en 3 variantes (`sku_utils.py:normalize_to_bm_sku`,
`_clean_sku_for_bm` sin cortar a 10, y 5 `sku.split("/")[0]` literales) y
solo servía para la clave de stock BM — nunca calculaba el stock/margen del
bundle como entidad propia, lo que podía ocultar quiebres del segundo
componente y subestimar el costo real.

- Tablas `sku_bundles` (SKU combinado + precio propio opcional) y
  `sku_bundle_components` (componentes + cantidad por bundle).
- `_apply_bundle_stock_override()`: para SKUs que coinciden exacto con un
  bundle definido, stock = `min(componente_avail // qty)` entre todos sus
  componentes reales — reemplaza el "solo el primero".
- `_apply_bundle_margin_override()`: costo/retail = suma de componentes
  (vía `_sku_cost_map`/`_sku_retail_map`, ya poblados por prewarm) contra
  el precio propio del bundle (o el precio ML actual si no se definió).
- Los componentes de cada bundle se agregan como candidatos extra al
  bulk BM normal del prewarm — nunca una llamada BM aparte por componente.
- Aplicado en 3 puntos: el ciclo principal de prewarm (Stock tab/alertas —
  donde el bug se diagnosticó originalmente), el listado de Productos
  (`page_products`) y Deals (`all_to_enrich`). **Alcance consciente:** no
  se propagó a 3-4 sitios más marginales (`sku_sales`, algunos endpoints
  de sincronización puntual) — pendiente si se necesita en el futuro.
- UI: tarjeta "Bundles" en Sync Stock → Configurar (crear/editar/eliminar,
  componentes dinámicos).
- Endpoints `GET/POST /api/bundles`, `POST /api/bundles/delete` (no DELETE
  con path param — `bundle_sku` suele traer "/").

Verificado local: CRUD completo por curl (incl. bundle_sku con "/"),
prueba unitaria directa de ambas funciones de override con caché
simulado (stock min correcto, margen con costo sumado correcto),
Playwright 375px/1920px sobre Sync Stock → Configurar (crear bundle,
0 overflow, 0 errores).

---

## 2026-07-23 — FEAT: Precio sugerido por cobertura de stock (Feature 3/4 de "ideas Zoho")

**Archivos:** `app/services/token_store.py`, `app/main.py`,
`app/api/lanzar.py`, `app/templates/stock_sync.html`.

`_days_supply`/`_is_scarce` ya se calculaban (distribución de pool,
alertas restock/stagnant) pero ninguna regla los conectaba con una
sugerencia de precio. Ahora sí, siguiendo el mismo molde ya probado de
`ml_price_alerts` (sugerir → confirmar manual → aplicar).

- Tabla `coverage_price_alerts` (mismo patrón que `ml_price_alerts`):
  precio actual, sugerido, razón (`escasez`/`sobrestock`), días de
  supply, unidades 30d. Se recalcula completa cada ciclo de prewarm
  (reemplaza todo, no acumula).
- **Regla v1, conservadora** (documentado como punto de partida a
  ajustar con datos reales, no la versión final): `days_supply < 7` y
  hay venta reciente → sugerir +8%; `days_supply > 90` → sugerir -12%,
  nunca por debajo de `_precio_piso` ya calculado. Se descartó cruzar
  "velocidad acelerando vs. histórica" (lo que proponía el especialista
  de planeación) porque no hay ventana de velocidad histórica separada
  ya calculada — quedó pendiente como refinamiento futuro si hace falta.
- Nunca auto-aplica: el usuario confirma cada sugerencia con "Aplicar en
  ML", que reusa `POST /api/lanzar/sync-price` (el mismo PUT real con
  auditoría que ya usan las alertas de precio existentes) — se extendió
  ese endpoint para también limpiar `coverage_price_alerts`, no solo
  `ml_price_alerts`.
- Endpoint `GET /api/coverage-price-alerts`.
- UI: nueva tarjeta en Sync Stock → Ejecutar, con badge rojo (escasez)
  o azul (sobrestock) y botones Aplicar/Ignorar por sugerencia.

Verificado local: CRUD de la tabla por script directo, endpoint probado
con datos de prueba en las 4 cuentas ML, Playwright 375px/1920px
(0 overflow, 0 errores).

---

## 2026-07-23 — FEAT: Zonas de almacén reales + transferencias sugeridas + drift de catálogo (Feature 4/4 de "ideas Zoho")

**Archivos:** `app/services/token_store.py`, `app/services/mx_zones.py`
(nuevo), `app/main.py`, `app/templates/partials/products_stock_issues.html`.

La más grande de las 4 mejoras — Jovan pidió explícitamente hacerla
completa desde el inicio en vez de partirla en rondas, tras conocer que
era más grande de lo estimado originalmente. 4 sub-partes:

**4a — Bulk BM real para Tijuana (arregla dato vestigial):** `_bm_tj`
siempre estaba en 0 — el fetch per-SKU que lo llenaba fue deshabilitado
hace tiempo ("reduce concurrencia BM"), y `_bm_total` excluía TJ a
propósito en 5 sitios. Se agregó un TERCER bulk BM scoped por LocationID
`45,69,43,42` (Tijuana vendible), mismo patrón ya usado para MTY (LOC68)
y CDMX (LOC47) — una llamada bulk más por ciclo, NO llamadas per-SKU
(evita el problema de concurrencia BM que vivimos esta sesión con el
bulk colgado). Aplicado tanto en el flujo GR principal como en el
desglose específico de TVs (`_fetch_tv_wh_breakdown`, condiciones
ICB/ICC). `_bm_total` ahora sí suma mty+cdmx+tj en los 5 sitios donde
antes se excluía TJ (incluida una función propia de Feature 2/bundles).

**4b — Alerta de drift de catálogo:** tabla `stock_issue_streaks`
(cuenta+SKU+tipo de problema → primera vez visto, última vez visto),
actualizada cada ciclo de prewarm cuando se calcula "Desbalance". Alerta
nueva cuando un SKU lleva ≥24h seguidas en Desbalance — señal de que
probablemente es un error de configuración BM (como el caso LocationID
62/63 ya resuelto), no un problema de venta real que cambia rápido.

**4c — Geolocalización de demanda (solo ML por ahora):** columnas
`ship_state_code`/`ship_zone` en `order_history`. Confirmado en vivo
(llamada real a `/shipments/{id}`) que `receiver_address.state` viene
como `{"id": "MX-NLE", "name": "Nuevo León"}` — mapeo nuevo
`app/services/mx_zones.py` de código de estado → zona (MTY/CDMX/TJ),
heurística de negocio documentada como tal (cercanía aproximada, no
logística exacta). **Backfill deliberadamente acotado**: máximo 15
órdenes por cuenta por ciclo (~15 min) resuelven su zona vía `GET
/orders/{id}` + `GET /shipments/{id}` — nunca todas de golpe, para no
saturar la API de ML (mismo cuidado de concurrencia que con BM). Tarda
varios ciclos en cubrir el historial reciente.
**Amazon queda bloqueado**: SP-API restringe `ShippingAddress` desde
2021 — requiere un Restricted Data Token (RDT) que Jovan debe
solicitar/aprobar en Seller Central (similar al consentimiento OAuth que
ya hicimos para Gmail). El schema ya está listo; la columna de zona
simplemente queda vacía para Amazon hasta que se apruebe ese acceso.

**4d — Transferencia sugerida entre almacenes:** cruza demanda por zona
(de `order_history.ship_zone`, agregada por SKU) contra dónde está el
stock físico (mty/cdmx/tj, ya reales gracias a 4a). Heurística v1,
conservadora a propósito: solo sugiere si el desbalance demanda-vs-stock
por zona es grande (≥30 puntos porcentuales) y hay historial mínimo de
demanda (≥5 unidades) — evita sugerir con datos ruidosos o escasos al
principio, mientras el backfill de 4c se va llenando. Solo lectura/
sugerencia — el movimiento físico real se hace en BinManager.

**Verificación:** unit tests directos de `zone_for_state_code`, drift
alerts (streak creada/rota correctamente), tracking de zona de orden
(missing→resuelta); heurística de transferencia verificada a mano con
números de ejemplo; Playwright 375px/1920px sobre `/items?tab=stock` con
datos sintéticos de las 3 secciones nuevas inyectados en el snapshot
cacheado — 0 overflow, 0 errores (el overflow de 987px visto en el
primer intento era de un snapshot viejo/stale sin estos campos, no de
este código — confirmado al no aparecer ninguno de los bloques nuevos en
esa respuesta).

**Cierre de la iniciativa "ideas Zoho":** con esta, las 4 mejoras
identificadas al comparar contra Zoho Inventory (con
binmanager-specialist + planning-specialist) quedan implementadas y en
producción: boost estacional, bundles reales, precio por cobertura, y
zonas de almacén + transferencias + drift.

---

## 2026-07-23 — FIX: Alertas de Stock en 0 tras cada deploy — snapshot bueno sobreescrito por bulk BM fallido

**Archivo:** `app/main.py` (~línea 5670-5715, dentro de `_do_prewarm()`).

Jovan reportó (con screenshot) que justo después del deploy anterior, la
pestaña Productos → Stock de BLOWTECHNOLOGIES mostraba TODAS las alertas en
0 (Sin Stock, Revenue Perdido, Riesgo Sobreventa, Oportunidad Activar, Stock
BM Disponible, Stock Crítico), mientras que métricas no dependientes de BM
(Precio < Retail PH, Listings Eliminados) seguían normales. Pidió una
solución definitiva, no reiniciar el caché a mano cada vez.

**Diagnóstico:**
- El sistema YA persiste `_stock_issues_cache` en SQLite
  (`save_stock_issues_snapshot`/`load_all_stock_issues_snapshots`) y lo
  recarga al arrancar (`_load_stock_issues_from_db()`, `lifespan()`) — esto
  ya sobrevive deploys, no es nuevo.
- **El bug real:** cuando el prewarm vuelve a correr después del restart y
  el bulk de BM falla, se cuelga, o no verifica ningún SKU (confirmado en
  vivo: una prueba directa contra el bulk de BM quedó colgada varios
  minutos mientras una consulta puntual por SKU respondía normal — Jovan
  confirmó que BM funciona bien logueándose directo, así que el cuelgue es
  del lado de nuestra app, no de BM en sí), TODOS los conteos de alertas
  dependen de `_bm_bulk_ok()` (requiere verificación de ESTE ciclo, no basta
  con el dato cargado de DB) — si el bulk no verificó nada, todo sale 0. Ese
  resultado de puros ceros se escribía sin condición sobre el snapshot bueno
  anterior, tanto en memoria como en SQLite.

**Fix:** antes de sobreescribir `_stock_issues_cache[key]` y persistir a
SQLite, se cuenta cuántos SKUs candidatos quedaron verificados por el bulk
de esta corrida. Si ese número es 0 (con candidatos > 0) y ya existe un
snapshot anterior en memoria, se conserva el snapshot anterior (con su
timestamp real, no se resetea) en vez de escribir el resultado en ceros —
solo se loguea una advertencia. Si el bulk sí verificó algo (aunque sea
parcial) o no había snapshot previo, se escribe normal como antes.

**Riesgo señalado:** el fix corrige que esto se REPITA en futuros
deploys/reinicios — no repara retroactivamente el snapshot ya corrompido en
memoria de este incidente (ese necesita que el bulk de BM logre correr
exitosamente al menos una vez más, o un "Actualizar BM" manual una vez BM
responda).

---

## 2026-07-23 — FEAT/FIX: Rediseño UX de Mensajes de Compradores (KPIs + urgencia) + bug de hoisting en toggle "solo pendientes"

**Archivos:** `app/main.py`, `app/services/token_store.py`, `app/templates/amazon_dashboard.html`, `app/static/js/amazon_dashboard.js`.

Jovan pidió rediseñar la sección porque la veía "confusa/rara" y quería
métricas (cuántos pendientes, cuánto lleva sin responder un mensaje, etc.).
Se consultó al especialista `uxui-designer` para la dirección visual
(mockup navegable con toggle Antes/Después) y con ese insumo se armó el
plan técnico leyendo el código real antes de tocar nada.

**Diagnóstico del especialista, confirmado en el código:** las acciones
(Tomar/Marcar resuelto) se renderizaban antes que el mensaje del comprador,
no había ninguna señal de urgencia (solo una fecha gris de 11px), el badge
de estado mezclaba "quién lo tocó" con "si el comprador sigue esperando", y
`_renderAmzBuyerMessages` generaba **dos copias completas del DOM por
hilo** (`cards` móvil + `table` escritorio con IDs distintos) — si escribías
una respuesta y la ventana cruzaba el breakpoint `md`, se perdía porque
saltabas a la copia oculta.

**Cambios:**
- `main.py` (`GET /api/amazon/buyer-messages`): nuevo bloque `stats`
  calculado sobre todos los hilos (antes del filtro `only_pending`, para
  que no dependa del toggle) — pendientes, desglose de urgencia
  (`<24h`/`24-72h`/`>72h`), hilo más antiguo sin respuesta, tiempo promedio
  de respuesta (par inbound→outbound), resueltos en últimas 24h.
- `token_store.update_message_view_status`: ahora también refresca
  `viewed_at` al cambiar de status (antes solo se ponía al "Tomar"), para
  que el KPI de "resueltos" sea preciso. Compartido con Mensajes ML — bajo
  riesgo, hoy nada en esa UI usa ese timestamp.
- `amazon_dashboard.html`: barra de 4 tarjetas KPI arriba de la toolbar
  existente.
- `amazon_dashboard.js`: se eliminó la duplicación cards/table (una sola
  estructura responsive por hilo), se reordenó el contenido (vista previa
  del mensaje primero, acciones secundarias al final, un solo CTA
  dominante "Responder"), se agregó chip de urgencia por hilo y
  agrupación en secciones (Urgente / Por atender / Recientes / Resueltos
  colapsado).

**Bug real encontrado al verificar con Playwright:** las variables
`amzMsgsOnlyPending`/`amzMsgsOrderSearch` se declaraban en medio del
archivo (junto al resto del código de Mensajes), pero `loadAmzSaludTab()`
puede dispararse más arriba en el mismo script al navegar directo a
`?tab=salud` — por *hoisting* de `var`, ese primer fetch mandaba
`only_pending=false` aunque el botón dijera "solo pendientes". Se movieron
ambas declaraciones al inicio del archivo, antes de cualquier disparo de
tab. Confirmado con un patch de `window.fetch` en Playwright: antes del fix
la variable llegaba `undefined` al momento del primer fetch; después,
`true` como se esperaba.

**Verificación:** Playwright en 375px y 1920px — 0 overflow, 0 errores de
consola, KPIs con datos reales, expand/collapse de hilo y de sección
"Resueltos" funcionando.

---

## 2026-07-22 — FEAT/FIX: "Marcar todo como atendido" (borrón y cuenta nueva) + bug real de status ignorado en filtro pendientes

**Archivos:** `app/main.py`, `app/services/token_store.py`, `app/templates/amazon_dashboard.html`, `app/static/js/amazon_dashboard.js`.

Jovan mostró un caso concreto (Martha, orden 702-4735302-6793818) donde
Seller Central mostraba "Resolved on Jul 21" con una respuesta real (de un
compañero llamado Jorge, vía plantilla de factura) pero el dashboard lo
seguía marcando como "sin responder". Se investigó a fondo contra el buzón
real: **confirmado de nuevo (segunda vez, ya con Edgar antes) que Amazon no
comparte ninguna copia de respuestas dadas directo en Seller Central** — ni
por email ni por API. Se le presentó la opción de automatizar un navegador
contra Seller Central (con los riesgos reales: fragilidad, zona gris de
TOS, credenciales guardadas) — Jovan prefirió no tomar ese riesgo.

**Decisión: "borrón y cuenta nueva".** Limpiar el historial acumulado de
una vez (marcando todo como atendido) y de ahí en adelante usar
Tomar/Marcar resuelto para lo nuevo, ya que eso SÍ lo puede rastrear el
dashboard con certeza (lo que pasa a través de él mismo).

- `token_store.bulk_mark_resolved()`: marca varios hilos resueltos en una
  sola transacción.
- `POST /api/amazon/buyer-messages/mark-all-resolved`: trae todos los
  hilos actuales de la cuenta y los marca resueltos de un jalón.
- Botón **"Marcar todo como atendido"** en la pestaña Salud, con
  confirmación explicando que es una limpieza de una sola vez.

**Bug real encontrado al probar esto**: el filtro `only_pending` (agregado
en la entrada anterior) solo miraba si el último mensaje era del comprador
(`needs_response`), **ignorando por completo si alguien ya lo había
marcado resuelto a mano** — exactamente el problema que reportó Jovan con
Martha. Corregido: `needs_response` ahora es "último mensaje es del
comprador **y** no está marcado resuelto".

Verificado en local: tras `mark-all-resolved`, "solo pendientes" pasa de
36 a 0; insertando un mensaje nuevo simulado, solo ese vuelve a aparecer
como pendiente (los otros 36 siguen ocultos, correctamente).

---

## 2026-07-22 — FEAT/FIX: Firma real, filtro de pendientes, búsqueda por orden y mover Mensajes de Compradores a Salud

**Archivos:** `app/main.py`, `app/services/buyer_messages_client.py`, `app/templates/amazon_returns.html`, `app/templates/amazon_dashboard.html`, `app/static/js/amazon_dashboard.js`.

Tras el fix de envío, Jovan encontró 3 problemas más usando la feature en vivo:

1. **Header plegado rompía el envío**: al responder salía "Header values may
   not contain linefeed or carriage return characters". El Subject original
   de Amazon viene "plegado" (RFC 5322 — headers largos continúan en la
   siguiente línea con `\r\n` + espacio) y no se normalizaba al guardarlo.
   Se corrigió en `_decode_header_value()` (colapsa cualquier whitespace a
   un espacio) + defensivo en `_build_mime_message()` para filas ya
   guardadas. Nuevo `/api/diag/fix-buyer-message-subjects` limpió 105 de 201
   filas ya guardadas en producción (el poller no las iba a re-procesar
   solo, `INSERT OR IGNORE` por `message_id`).
2. **Firma real al responder** — nunca "admin" (Jovan: "no sería correcto").
   Se usa `display_name` o `username` de la sesión, se agrega al final del
   correo antes de enviarlo (y se guarda el texto CON firma en el historial).
3. **Ocultar por default los ya respondidos** — Jovan veía mensajes ya
   contestados mezclados con pendientes. Nuevo campo `needs_response` por
   hilo (true si el último mensaje es del comprador) + parámetro
   `only_pending` (default true) en `/api/amazon/buyer-messages`. Botón para
   alternar "solo pendientes"/"todos".
4. **Buscar histórico completo por número de orden** — nuevo parámetro
   `order_id` que ignora el filtro de pendientes y la ventana de días.

**Limitación real descubierta (no arreglable)**: Jovan preguntó por qué
mensajes ya respondidos por un compañero directo en Seller Central se veían
"sin responder". Investigado a fondo contra el buzón real (Edgar, orden
702-8392732-7983458): **Amazon no reenvía ninguna copia de las respuestas
dadas desde Seller Central** — de los ~13,600 correos de `donotreply@amazon.com`
en la bandeja, ninguno confirma "ya respondiste a X" con el contenido. Es
una limitación de origen, no un bug del parser — no hay forma de detectar
eso vía email ni SP-API. Mitigación: usar "Tomar"/"Marcar resuelto" de aquí
en adelante para que el sistema quede preciso.

**Reorganización**: Jovan señaló que en ML los "Mensajes" post-venta viven
en la pestaña Salud (`health_messages.html`), no en Retornos — se movió
"Mensajes de Compradores" de `amazon_returns.html` a la pestaña Salud de
`amazon_dashboard.html` (mismo patrón entre plataformas). Verificado con
Playwright: `/amazon/returns` ya no tiene la sección (resto intacto),
`/amazon?tab=salud` la tiene funcionando (36 hilos, Tomar/IA, 0 overflow).

---

## 2026-07-22 — FIX CRÍTICO: Responder en Mensajes de Compradores no funcionaba en producción — migración de SMTP a Gmail API/OAuth

**Archivos:** `app/services/buyer_messages_client.py`, `app/auth.py`, `app/config.py`, `app/main.py`.

Jovan reportó que el botón "Responder" se quedaba pegado en "Enviando..." sin
confirmar si el correo se mandó o no. Diagnóstico paso a paso:

1. Se agregó `/api/diag/smtp-test` (probando puertos 465 SSL y 587 STARTTLS)
   contra producción → **ambos fallan con "Network is unreachable"** (10-12s,
   tanto IPv4 como IPv6). **Railway bloquea el egress a los puertos de envío
   de correo** — política anti-spam estándar de la mayoría de hosts en la
   nube (Railway, Heroku, Render, etc.), no algo arreglable con
   configuración de red. Antes de este fix, `smtplib.SMTP_SSL` no tenía
   timeout, así que el bloqueo silencioso (sin RST) colgaba la conexión
   indefinidamente en vez de fallar rápido — de ahí el "Enviando..." eterno.
2. La LECTURA (IMAP, puerto 993) sí funciona en producción — el poller ya
   venía importando mensajes reales. Solo el ENVÍO estaba roto.

**Solución — migrar el envío a la API de Gmail (HTTPS, nunca bloqueado),
autenticado vía OAuth** (mismo tipo de flujo ya usado para Mercado Libre y
las 3 cuentas Amazon en este proyecto):

- `app/config.py`: `GMAIL_OAUTH_CLIENT_ID`/`CLIENT_SECRET` (una sola app
  OAuth de Google Cloud sirve para las 3 cuentas) + `AMAZON_GMAIL_REFRESH_TOKEN`/
  `AMAZON2_.../AMAZON3_...` (uno por cuenta, se obtiene autorizando cada
  buzón por separado).
- `app/auth.py`: nuevos `/auth/gmail/connect` (inicia el consentimiento OAuth)
  y `/auth/gmail/callback` (intercambia el code, muestra el refresh_token
  una vez para copiarlo a Railway — sin tabla nueva, mismo criterio que
  `AMAZON_INBOX_EMAIL`/`APP_PASSWORD`).
- `app/services/buyer_messages_client.py`: `send_reply()` ya no usa
  `smtplib` — construye el mismo MIME (incluye adjunto si viene), refresca
  un access_token con el refresh_token guardado, y lo manda vía POST HTTPS
  a `gmail.googleapis.com/.../messages/send`. El poller de LECTURA
  (`imaplib`) no se tocó.

**Scopes OAuth — 3 idas y vueltas hasta dar con la combinación correcta**
(cada uno es un recurso distinto en la API de Gmail, no hay uno solo que
cubra todo): `gmail.send` (enviar), `gmail.settings.basic` (filtros —
*no* alcanza para etiquetas), `gmail.labels` (etiquetas — confirmado con el
error real `ACCESS_TOKEN_SCOPE_INSUFFICIENT` en `users.labels.list` aun
con `settings.basic` correctamente presente en el token, verificado con
`/tokeninfo`). También se descubrió que la pantalla de reconsentimiento de
Google se puede saltar el checkbox del scope nuevo si la app ya tenía
acceso previo — hay que **revocar el acceso completo** en
`myaccount.google.com/permissions` antes de volver a autorizar para que
Google muestre la pantalla completa con las 3 casillas.

**Bono — Jovan pidió organizar el correo real** (etiquetar y archivar
automático los mensajes de `marketplace.amazon.com.mx` para no ensuciar su
bandeja) y explícitamente **"hazlo tú"** en vez de crear el filtro a mano en
Gmail: `setup_organization_filter()` crea la etiqueta + filtro vía la misma
API de Gmail (`POST .../settings/filters`, `removeLabelIds: ["INBOX"]` =
Skip Inbox), disparado desde `/api/diag/gmail-setup-filter`. Esto expuso un
bug latente: el poller solo buscaba en `INBOX` — un mensaje archivado
automáticamente ya no aparece ahí. Se corrigió buscando en el folder
"Todos los correos" (detectado dinámicamente vía el atributo IMAP `\All`,
no por nombre — la cuenta está en español, se llama `[Gmail]/Todos`, no
"All Mail"; esto además deja el código listo para cuentas en otros idiomas).

**Verificado en producción:** filtro y etiqueta creados exitosamente vía
API; envío de correo de prueba confirmado (200 OK, entregado). El
mecanismo de respuesta ahora funciona de punta a punta en Railway.

---

## 2026-07-22 — FEAT: Mensajes de Compradores Amazon — control de atención, adjuntos y respuesta con IA

**Archivos:** `app/main.py`, `app/services/buyer_messages_client.py`,
`app/services/health_ai.py`, `app/api/health_ai.py`, `app/static/js/health_ai.js`,
`app/templates/amazon_returns.html`.

Sobre la base de "Mensajes de Compradores" (entrada anterior), Jovan pidió 3
mejoras: adjuntar archivos al responder, respuesta sugerida por IA (mismo
patrón ya usado en Salud/Retornos ML), y que antes de sugerir se sepa si ya
hay respuestas previas en el hilo o si otro compañero ya está atendiendo a
ese comprador — pidió explícitamente el mismo sistema de "Tomar"/"Atendiendo:
fulano"/"Resuelto" que ya existe en Mensajes ML.

Las 3 piezas reusan mecanismos ya existentes para ML en vez de inventar algo
nuevo:

- **Control de atención**: se reusa la tabla genérica `ml_message_views`
  (`pack_id`/`account_id` → `viewed_by`/`status`) con el mismo truco de
  prefijo que ya usan los reclamos (`"claim:"`) — ahora `"amz:{reply_to_addr}"`.
  **Cero cambios de schema.** Nuevos endpoints `POST /api/amazon/buyer-messages/take`
  y `/status`, mismo patrón que `app/api/health.py:250-283` (ML).
- **Cambio de forma necesario**: `GET /api/amazon/buyer-messages` pasó de
  devolver una lista plana de mensajes a **agrupar por hilo** (`reply_to_addr`,
  la dirección tokenizada de Amazon — identificador estable de "esta
  conversación con este comprador"). Sin esto, Tomar/Atendiendo/IA-con-historial
  no tenían sentido. `amazon_returns.html` se reescribió para pintar una
  tarjeta por hilo (badges Sin abrir/Abrió/Atendiendo/Resuelto + botones
  Tomar/Marcar resuelto), igual que Mensajes ML.
- **IA**: `build_buyer_message_reply_prompt()` nueva en `health_ai.py`,
  copiada de `build_message_reply_prompt` (ML) pero cubre MX+USA (responde
  en el idioma del comprador, no siempre español) y advierte a la IA si otro
  compañero ya atendió el hilo (no repetir/contradecir). Endpoint
  `POST /api/health-ai/suggest-buyer-message-reply` reusa `_sse_stream` y
  `openrouter_client` (cascada cost-aware ya implementada) tal cual. Frontend
  reusa `streamAiResponse` (100% genérico) y `useAiSuggestion` ganó una rama
  `'buyer_message'`. **Nunca auto-envía** — solo llena el textarea, igual
  que en ML.
- **Adjuntos**: `POST .../{id}/reply` pasó de JSON a `multipart/form-data`
  (`Form`+`UploadFile`). El archivo se lee en memoria y se manda directo por
  SMTP (`EmailMessage.add_attachment`) — **no se persiste en disco a
  propósito** (este proyecto ya tuvo 2 incidentes de disco lleno por guardar
  archivos sin límite).

Verificado en vivo contra VECKTOR (50 mensajes reales agrupados en 36 hilos):
Tomar/Marcar resuelto/Reabrir actualizan el badge y persisten en
`ml_message_views` con prefijo `amz:`. Botón "Sugerir con IA" probado con
clic real en la UI (no solo el endpoint) — sugerencia real generada y "Usar
respuesta" la copia correctamente al textarea. Adjunto probado de punta a
punta contra la propia cuenta (nunca un comprador real): el archivo llega
intacto en el correo recibido.

**Riesgo conocido, no resuelto:** no está confirmado que Amazon preserve el
adjunto al relanzar la respuesta a un comprador real (nuestro canal es el
reenvío de correo, no la API oficial de Seller Central que sí soporta
adjuntos documentadamente) — se manda de todos modos, pendiente de
confirmar con un caso real.

---

## 2026-07-22 — FEAT: Mensajes de Compradores Amazon — leer y responder desde el dashboard (sin SP-API)

**Archivos:** `app/config.py`, `app/services/buyer_messages_client.py` (nuevo), `app/services/token_store.py`, `app/main.py`, `app/templates/amazon_returns.html`.

Jovan pidió ver, ligado a la orden, si un comprador Amazon escribió un mensaje
post-venta (como el panel "Buyer Messages" de Seller Central) y poder
responder desde el dashboard. Investigación SP-API (misma sesión, ver entrada
anterior de "mapa de motivos") ya había confirmado que **no existe ningún
endpoint de lectura** — la Messaging API es solo de salida. Jovan mostró
Replyco como prueba de que sí se puede, cuestionando la conclusión.

Investigación adicional confirmó el mecanismo real de Replyco/eDesk/
ChannelReply: **ninguna API de Amazon** — usan el reenvío de correo que
Amazon ofrece en Seller Central (Notification Preferences → Messaging →
Buyer Messages) hacia un buzón propio, más "Approved Senders" para poder
responder por email y que Amazon relance la respuesta al comprador de forma
anónima. Plan completo aprobado en `.claude/plans/stateful-marinating-marshmallow.md`.

- **Buzón dedicado**: Gmail nuevo por cuenta Amazon (`vecktordiez@gmail.com`
  para VECKTOR), con verificación en 2 pasos + contraseña de aplicación.
  `AMAZON_INBOX_EMAIL`/`AMAZON_INBOX_APP_PASSWORD` (+ `AMAZON2_.../AMAZON3_...`)
  en `config.py` — cuenta sin buzón configurado se salta sola sin romper nada.
- **`buyer_messages_client.py`** (nuevo): poller IMAP (`imaplib` + `asyncio.to_thread`,
  cada 5 min) + envío SMTP para responder. El parser se construyó viendo
  correos REALES de la bandeja de VECKTOR (que ya tenía años de reenvío
  activo) en vez de adivinar el formato: el texto del comprador vive entre
  `------------- Mensaje: -------------` y `------------- Finalizar mensaje -------------`;
  el order_id sale de `# XXX-XXXXXXX-XXXXXXX:`; ASIN y título del producto ya
  vienen embebidos en el cuerpo (no hace falta join a ninguna tabla); la
  dirección de respuesta es la misma del From/Reply-To, tokenizada por Amazon
  (`nombre@marketplace.amazon.com.mx`).
- **`token_store.py`**: tabla `amazon_buyer_messages` (UNIQUE por `message_id`
  — el poller puede re-ver el mismo correo sin duplicar), helpers
  `insert_buyer_message` / `get_buyer_messages` (con límite) /
  `mark_buyer_messages_read` (bulk) / `get_buyer_message`.
- **`main.py`**: `GET /api/amazon/buyer-messages`, `POST .../mark-read` (bulk),
  `POST .../{id}/reply` (envía el correo real, registra el outbound, log_action).
  Poll loop registrado al arrancar la app junto a los demás loops de fondo.
- **`amazon_returns.html`**: sección nueva "Mensajes de Compradores" — mismo
  patrón cards mobile + tabla desktop que "Comentarios de Clientes", con
  textarea + botón "Responder" por mensaje sin leer.

**2 bugs reales encontrados y corregidos en la verificación local (antes de
push, no en producción):**
1. El buzón de VECKTOR ya traía años de historial real — el primer poll trajo
   200 mensajes de una. `get_buyer_messages` sin límite hubiera devuelto un
   feed enorme sin sentido — se acotó a 50 más recientes.
2. El frontend marcaba como leído con **un fetch por mensaje** — con 200
   mensajes sin leer eso son 200 peticiones simultáneas, generando
   contención real en SQLite (la segunda pestaña de prueba se quedó
   colgada en "Cargando..." varios segundos). Se reemplazó por un solo
   endpoint bulk (`mark-read` con lista de IDs).
3. Cards mobile y tabla desktop reusaban el mismo `id` (`msg-card-N`,
   `reply-text-N`) — el botón "Responder" en escritorio hubiera leído el
   textarea oculto de mobile (siempre vacío) por `getElementById` devolver
   el primer match. IDs ahora prefijados por variante (`m-`/`d-`).

Verificado en vivo contra VECKTOR (única cuenta con buzón configurado hasta
ahora): 50 mensajes reales renderizados con título/ASIN/orden/texto correctos
(acentos correctos confirmados, ej. "Recibí la pantalla"), 0 overflow, 0
errores de consola en 375/1920px. Mecanismo de respuesta probado de punta a
punta contra el endpoint real (fila de prueba con `reply_to_addr` apuntando a
la propia cuenta, nunca a un comprador real) — entrega SMTP confirmada, fila
outbound registrada, original marcado como leído.

**Pendiente (Fase 2, bloqueada en Jovan):** repetir el setup manual (Gmail
dedicado + Seller Central: Notification Preferences, Customer Service/
Reply-To Email, Approved Senders) para AUTOBOT AMZ MX y ExclusiveBulbs — el
código ya es genérico por `seller_id`, no requiere cambios.

---

## 2026-07-21 — FIX: Retornos Amazon mostraba SKU sin nombre de producto (Quality Score + Top SKUs)

**Archivos:** `app/main.py`, `app/templates/amazon_returns.html`.

Jovan lo reportó con captura tras el fix de arriba: "el sku sin nada de
información sobre el producto eso es una jalada, en varias secciones
tienes así". Causa raíz: `_aggregate_amazon_returns_by_sku()` nunca se
enriquecía con el título real — el campo `title` se inicializaba como
el propio SKU (fallback permanente, nunca se sobreescribía). Quedaba
oculto mientras `get_returns_report()` tenía el bug del reportType
equivocado (fix anterior en esta misma sesión) porque no había datos
reales fluyendo por ahí; al corregirlo, el hueco de título se volvió
visible en producción.

- `main.py`: se construye `title_by_order` (desde `product_name` del
  reporte de devoluciones, título exacto de esa orden) y `title_by_sku`
  (respaldo desde el catálogo de listings de la cuenta, mismo patrón de
  `amazon_sku_sales_table`) — se usa el primero disponible en vez de
  caer siempre al SKU crudo. Beneficia de forma compartida a Quality
  Score, Top SKUs, y la vista global cross-cuenta (`_compute_unified_returns`
  usa la misma función).
- `amazon_returns.html`: Quality Score ahora muestra el título en una
  línea secundaria (antes `s.sku || s.title` nunca mostraba el título
  porque sku siempre está presente). Top SKUs ganó cards mobile +
  columna "SKU / Producto" en la tabla desktop (antes la tabla ni
  siquiera tenía columna de producto en el HTML).

Verificado en vivo contra cuenta real (VECKTOR): ambas secciones
muestran títulos reales ("LUBL Smart TV 2025...", "Samsung 55-Inch
Class Crystal UHD...") en vez de solo el SKU. 0 overflow, 0 errores de
consola en 375/1920px.

**Pendiente de confirmar con Jovan:** ExclusiveBulbs (cuenta USA) no
existe en el token store LOCAL de desarrollo (solo VECKTOR y AUTOBOT
MEXICO) — confirmado que SÍ está conectada y con datos reales en
producción (Jovan mandó captura del dashboard con 3552 órdenes). La
feature de comentarios de clientes y este fix de título no tienen
ningún filtro por cuenta en el código — deberían funcionar igual para
ExclusiveBulbs, pero no se pudo verificar en vivo contra esa cuenta
específica por no tener sus tokens en el entorno local.

---

## 2026-07-22 — FIX: mapa de motivos de devolución Amazon incompleto + entorno local ahora prueba las 3 cuentas (incluye ExclusiveBulbs USA)

**Archivos:** `app/main.py` (`_AMZ_REASON_MAP`), `.env.production` (local, gitignored).

Jovan cerró el pendiente de arriba de forma tajante: *"puedes debes
hacer que puedas probar y que todo funcione igual así que busca todo y
generaliza"*. Se resolvió sin depender de que él verificara nada a mano:

- Se sacaron las credenciales `AMAZON3_*` (ExclusiveBulbs, ya existían
  en Railway prod) vía la API GraphQL de Railway y se agregaron al
  `.env.production` LOCAL (gitignored, confirmado con
  `git check-ignore -v`) para poder levantar el servidor local con las
  3 cuentas Amazon activas en vez de solo 2.
- Con ExclusiveBulbs corriendo en local, se probó `top-skus` y
  `customer-comments` contra datos reales (208 devoluciones, 24 con
  comentario) y aparecieron códigos de motivo sin traducir:
  `DAMAGED_BY_FC`, `UNDELIVERABLE_UNKNOWN`, `UNWANTED_ITEM`,
  `ORDERED_WRONG_ITEM`, `APPAREL_TOO_SMALL` — `_AMZ_REASON_MAP` solo
  cubría 12 códigos, los que habían aparecido en la muestra original de
  VECKTOR/AUTOBOT.
- Se amplió el mapa a ~25 códigos oficiales de Amazon (no solo los 5
  que faltaban, para no volver a parchar reactivamente la próxima vez
  que aparezca una cuenta o categoría de producto distinta).

Verificado en vivo (Playwright, 375px y 1920px, ambas cuentas): VECKTOR
(MX) 10 motivos distintos traducidos; ExclusiveBulbs (USA, marketplace
`ATVPDKIKX0DER`, distinto de MX) 9 motivos distintos traducidos, título
real, comentario real. 0 overflow, 0 errores de consola. Confirma que
el bug era solamente de traducción — el reporte, parseo y caché ya
funcionaban igual sin importar cuenta o marketplace.

---

## 2026-07-21 — FEAT + BUG: comentarios reales de clientes en Retornos Amazon (FBA) — y fix de bug preexistente que dejaba sin datos reales a Quality Score/Top SKUs desde Fase 2.1

**Archivos:** `app/services/amazon_client.py`, `app/main.py`, `app/templates/amazon_returns.html`.

Jovan preguntó si se puede ver la orden + lo que el cliente escribe
cuando reporta un problema en Amazon. Ya se había confirmado con el
agente `amazon-specialist` (investigación de la doc oficial SP-API,
ver `.claude/memory/reference_amazon_sp_api_docs.md`) que Buyer
Messages (la bandeja de "Actions" de Seller Central) **no es accesible
por API** — Messaging API es solo de salida. La alternativa real: el
reporte de devoluciones FBA sí trae un campo de texto libre escrito
por el cliente (`customer-comments`) — pero solo para FBA, Amazon no
expone esto para FBM (envío propio).

**Bug encontrado y corregido en el camino** (no solo la feature nueva):
`get_returns_report()` en `amazon_client.py` pedía el reporte
EQUIVOCADO desde que se escribió — `GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE`
en vez de `GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA`. Esos dos
reportes tienen columnas totalmente distintas (Title Case sin
comentario vs. lowercase-con-guión con `customer-comments`) — el
parseo existente (`row.get("sku")`, `row.get("customer-comments")`)
nunca hacía match contra las columnas del reporte viejo, así que el
método **siempre devolvía 0 filas**, silenciosamente. Confirmado en
vivo contra cuenta real (VECKTOR): 0 filas con el reporte viejo, 61
devoluciones reales (16 con comentario) con el correcto. Esto también
dejaba sin datos reales el "Quality Score" y las "Razones" de Top SKUs
en Retornos Amazon desde que se implementó esa feature (Fase 2.1,
2026-07-17) — quedaron arreglados de paso, no solo la feature nueva.

- `amazon_client.py`: `reportType` corregido + se agrega `product_name`
  al parseo (viene directo en el reporte, más preciso que cruzar con
  título de BM).
- `main.py`: nuevo endpoint `/api/amazon/returns/customer-comments` —
  reusa el cache existente (`_fetch_amazon_returns_report_cached`, sin
  llamadas nuevas a Amazon), filtra a devoluciones con comentario no
  vacío, enriquece con producto y motivo legible.
- `amazon_returns.html`: nueva sección "Comentarios de Clientes" (cards
  mobile + tabla desktop) debajo de Top SKUs, con aviso honesto de que
  solo cubre FBA.

Verificado en vivo contra cuenta real (VECKTOR): encoding UTF-8
correcto (acentos/eñes verificados leyendo el archivo, no solo en
consola — la consola de Windows los mostraba mal por el codepage, no
por corrupción de datos real), 0 overflow, 0 errores de consola en
375/1920px, Quality Score y Top SKUs confirmados mostrando razones
reales después del fix.

---

## 2026-07-21 — FEAT: registro de resoluciones en Alertas de Stock (sustitución + poner en 0) y aviso de reactivación

**Archivos:** `app/services/token_store.py`, `app/services/stock_concentrator.py`, `app/main.py`, `app/api/users.py`, `app/templates/orders.html`.

Jovan pidió llevar un récord de qué orden se resolvió sustituyendo el
producto por otro (con quién lo autorizó) y un botón para poner
stock=0 en las 4 cuentas ML de un jalón cuando de plano no hay
inventario. Durante el diseño, agregó un tercer requisito: cuando ese
SKU puesto en 0 vuelve a tener stock, las alertas deben avisar para
reactivarlo — "por eso es importante que las alertas estén funcionando
al 100%".

- Tabla nueva `stock_alert_resolutions`: order_id, sku original, tipo
  (`substitution`|`zeroed_stock`), sku sustituto, nota, usuario,
  timestamp, reactivated_at. Cada resolución también se escribe en el
  `audit_log` ya existente (acciones nuevas `stock_order_substitution`/
  `stock_bulk_zero` en `ACTION_META` de `users.py`), así aparece
  automático en Auditoría sin duplicar esfuerzo.
- `stock_concentrator.py` gana `preview_zero_all()`/`execute_zero_all()`
  — mismo motor que ya usa Concentración de Stock
  (`find_sku_across_accounts` + `update_item_stock`), pero sin ganador:
  zerea TODO lo no-FULL en todas las cuentas donde exista el SKU.
- 6 endpoints nuevos: `resolve-substitution` (cualquier usuario
  logueado, nota obligatoria como respaldo de que el cliente aceptó),
  `zero-stock-preview`/`zero-stock` (**admin-only**, con vista previa
  antes de ejecutar — decisión explícita de Jovan sobre permisos),
  `resolutions` (historial), `restock-watch`/`restock-dismiss`.
- Aviso de reactivación: cuando un SKU en 0 ya tiene `available_qty > 0`
  en `bm_sku_master` (sincronizado periódicamente, **no** es llamada en
  vivo a BM), aparece un banner "Ya hay stock disponible" con link
  directo a Concentración de Stock — en vez de reinventar cómo repartir
  el stock entre cuentas, se reutiliza la herramienta que ya hace
  exactamente eso.
- Frontend: 2 botones por alerta (🔁 Sustituir / ⛔ Sin stock, el
  segundo solo visible para admin vía `IS_ADMIN_ALERTAS`), 2 modales
  (sustitución con nota obligatoria; poner-en-0 con preview de qué
  cuentas/publicaciones se van a afectar antes de confirmar), toggle
  En vivo/Historial, banner de reactivación pendiente.

Verificado en local: 403 para no-admin en zero-stock, 200 para
sustitución con cualquier usuario, inserción correcta en DB, aparece en
Auditoría con ícono/etiqueta propios, modales y toggle funcionan sin
errores de consola (0 overflow). Prueba con SKU inexistente confirmó
que no toca cuentas ML reales cuando no encuentra publicaciones antes
de probar con datos reales.

**Pendiente — push a mi2/ecomops (Coolify) falló por token expirado**
(`fatal: Authentication failed for 'https://github.com/mi2-apps/ecomops.git/'`
— push a origin sí tuvo éxito, Railway ya tiene el deploy). Coolify ya
estaba en pausa por el exit 137 pendiente de Amir, así que no es
bloqueante hoy, pero el PAT de ese remote necesita renovarse — Jovan
tiene que generar un token nuevo en GitHub (mi2-apps/ecomops) y
actualizarlo en la URL del remote `mi2` local.

---

## 2026-07-21 — FIX: contenedor principal fluido (max-w-7xl → max-w-[1920px]) + auditoría responsive completa de la app

**Archivos:** `app/templates/base.html`, `productos.html`, `ml_sin_bm.html`, `facturacion.html`.

Jovan volvió a reportar espacio vacío desperdiciado en monitores anchos
(misma queja de fondo que el rediseño de Alertas de Stock de más abajo,
pero esta vez pidió analizar TODA la app, no una sola pestaña). Se
lanzaron 8 agentes `uxui-designer` en paralelo, cada uno auditando un
módulo distinto (Ventas/Dashboard, Productos ML, Deals/Stock Issues, Ads,
Salud/Retornos, Amazon core, Amazon Productos+wizard, Stock/Planeación/
Facturación) — cobertura completa de los ~100 templates/partials del
proyecto, pestaña por pestaña. Resultado: **148 hallazgos** documentados
en un reporte consolidado (artifact), organizados en 5 fases por
prioridad. Ver `.claude/memory/project_responsive_audit_2026-07-21.md`
para el detalle completo de los 148 hallazgos y el plan de fases.

**Causa raíz confirmada y corregida en esta sesión (Fase 0 del plan):**
`base.html:727` — el `<main>` heredado por las 23 páginas tenía
`max-w-7xl mx-auto` (tope de 1280px, centrado). El nav ya se había hecho
`w-full` en un fix anterior (commit 8dc596a, 2026-07-17) pero el
contenedor de CONTENIDO nunca se tocó — de ahí el síntoma "nav ancho
arriba, contenido angosto abajo". Cambiado a
`max-w-[1920px] w-full mx-auto` con padding progresivo
(`px-2 sm:px-4 lg:px-6`).

Además, 3 páginas tenían su propio wrapper `max-w-7xl mx-auto` DUPLICADO
anidado dentro del `<main>` (`productos.html:4`, `ml_sin_bm.html:4`,
`facturacion.html:4`) — sin quitarlos, el fix del shell no se hubiera
notado ahí. Se les quitó el tope, dejando solo el padding.

Verificado con Playwright local (375/1024/1920px, 5 páginas): el overflow
horizontal detectado a 375px en `orders.html` es preexistente (tab bar de
5 sub-vistas sin wrap, ya documentado como hallazgo de Fase 1, no
introducido por este cambio) — confirmado comparando contra el commit
anterior vía `git stash`. 0 errores de consola nuevos.

**Pendiente — Fases 1-4** (23 hallazgos críticos + ~110 de menor
severidad: tablas sin fallback mobile, grids de KPI con saltos de
breakpoint, tap targets bajos, headers sin flex-wrap) — plan completo en
el artifact/memoria, a ejecutar por módulo en sesiones siguientes.

**Actualización misma sesión — limpieza de código muerto + Fase 1 Amazon
Productos (commits c459925, bab2eb7, Railway SUCCESS):**

- Eliminados 4 templates + sus 4 endpoints backend, confirmados sin
  ningún caller en templates/JS antes de tocar nada: `ads_best.html`,
  `ads_products.html` (Ads), `amazon_products_summary.html`,
  `amazon_products_inventory.html` legacy (Amazon Productos — reemplazados
  hace tiempo por `_resumen`/`_inventario` sin quitar los viejos).
- Fase 1 completa para el módulo Amazon Productos (4 hallazgos críticos):
  `amazon_products_inventario.html` (tabla 18 cols, `overflow-hidden` →
  `overflow-x-auto`), `amazon_products_repricing.html` (toolbar sin
  flex-wrap que podía esconder el botón "Aplicar seleccionados"),
  `amazon_products_seller_flex.html` (MTY/CDMX ocultas en mobile para
  priorizar columnas editables — **no** se duplicó la fila en cards
  porque el JS usa `querySelectorAll`/`querySelector` sin scope por
  visibilidad, duplicar inputs hubiera doble-contado selecciones/CSV),
  `amazon_lanzar_wizard.html` Paso 3 (3 grids de atributos
  `grid-cols-2` fijo → `grid-cols-1 sm:grid-cols-2 lg:grid-cols-3`).

Hallazgo incidental de paso: la barra de 9 tabs de `amazon_products.html`
(`.amz-prod-tab`) tiene el mismo problema de overflow sin wrap/scroll ya
visto en `orders.html`/`health.html` — no estaba en el alcance de este
batch, queda para una próxima pasada de tab bars.

## 2026-07-21 — FIX: auditoría responsive completa — Fase 1 (23 críticos) + parte de Fase 2-4 cerradas

**"Termina todo"** — Jovan pidió cerrar el resto de la auditoría en la
misma sesión. Se completaron los 19 hallazgos críticos restantes de
Fase 1 (más los 4 de Amazon Productos ya cerrados antes = 23/23), la
mayoría de Fase 2 (grids de KPI) de paso mientras se tocaba cada
archivo, y 1 ítem de Fase 4. Commits: `5e20ff3` (nav compartido),
`535c9cb` (Ventas), `42cb705` (Dashboard), `7288dc0` (Amazon core),
`9415fbd` (Productos), `a7b021e` (Ads), `f1d9595` (Auditoría/Salud/
Retornos), `7f6caa3` (Planeación/Facturación/Inventario), `2ceb7bf`
(wizard stepper mobile) — todos con Railway SUCCESS.

**Hallazgo no documentado en el audit original, encontrado en
verificación, alto impacto:** el cluster derecho del nav compartido por
las 23 páginas (USD/MXN, campana, selector de cuenta, badge admin,
botón Salir) se desbordaba 111px en mobile — exactamente lo que se veía
en la captura original de Jovan. Corregido en `base.html` (commit
`5e20ff3`): FX widget oculto bajo `sm:`, nombre de cuenta truncado a
70px en mobile, badge admin/rol oculto bajo `sm:`.

**Patrón recurrente encontrado 3 veces** (orders.html, amazon_products.html,
items.html): tab bars con `overflow-x-auto` ya presente pero **roto**
por botones `flex-1 md:flex-none` — el `flex-1` hace que los botones
compitan por espacio con contenido `whitespace-nowrap`, forzando overflow
de página pese al `overflow-x-auto` del contenedor. Fix: `flex-none`
uniforme (sin `flex-1`), dejando que el contenedor scrollee limpio.

**Decisión repetida — NO duplicar en cards cuando el JS no tiene
scope por visibilidad:** en 2 casos (`amazon_products_seller_flex.html`
antes, y ahora `planning.html` "Orden de Separación") el JS que recolecta
datos de la tabla usa `document.querySelectorAll('.clase')` sin filtrar
por elemento visible. Duplicar la fila en una card con la misma clase
hubiera hecho que ese JS procesara el dato DOS VECES (una desde la tabla
oculta, otra desde la card oculta según el viewport) — en `planning.html`
esto habría duplicado productos en el WhatsApp/CSV exportado. Se aplicó
el fix seguro (`overflow-x-auto` sin cards) en ambos casos en vez de
arriesgar un bug funcional por priorizar el diseño.

**Bug de Chart.js encontrado en `returns.html`:** el doughnut "Distribución
por Estado" (dentro de un sidebar fijo `lg:w-60`) no se redimensionaba
pese a tener `maintainAspectRatio:false` y un wrapper `position:relative`
correcto — el canvas quedaba en su tamaño default del navegador (300x150)
y desbordaba 52px incluso en 1920px de ancho. Se agregó `overflow-hidden`
al wrapper como clip defensivo (no se identificó la causa raíz exacta del
comportamiento de Chart.js en ese contexto específico, pero el clip
garantiza 0 overflow de página).

**Pendiente real, explícitamente fuera de esta sesión (bajo impacto):**
- Column-hiding → cards completas en 5 partials de Amazon Productos
  (catalog, buybox, devoluciones, inventory legacy, sin_publicar x3) —
  patrón ya "aceptable" per el audit original, no bloqueante.
- `.kpi-card` (hover lift) aplicado solo en ~3 de 20+ generadores de KPI.
- Paginación de "Por SKU"/"Comparativa" en orders.html con firmas
  distintas a `_renderPaginated()` — deuda de consistencia, no bug.
- Conversión completa a cards de `amazon_products_inventario.html`
  (18 columnas) — solo se aplicó el fix mínimo (`overflow-x-auto`).

Verificado con Playwright las 23 páginas × 375px/1920px (46 combos):
0 overflow horizontal, 0 errores de consola nuevos en todas.

## 2026-07-21 — FEAT: Alertas de Stock — rediseño de aprovechamiento de espacio (agente uxui-designer)

**Archivos:** `app/templates/orders.html`.

Jovan reportó que la sub-vista "Alertas de Stock" (recién movida a Ventas)
se veía "mocha" — con pocas filas (feed en vivo, a veces 0-2 órdenes) la
tabla ocupaba una fracción mínima de la pantalla y el resto quedaba vacío,
sin sensación de diseño intencional. Pidió usar el agente de diseño
(`uxui-designer`) en vez de un parche improvisado.

Spec del agente (prototipo interactivo incluido) diagnosticó que el
problema no era la tabla — era que arriba de ella solo había texto, sin
ningún elemento con "peso" visual que compitiera por espacio. Implementado:

1. **Franja de 5 KPIs** siempre presente (alertas activas, última
   detectada, cuentas afectadas, con sugerencia lista, unidades afectadas)
   — calculados 100% client-side sobre el mismo array que ya se descarga,
   sin endpoint nuevo.
2. **Barra de contexto** (chips por cuenta + chip "SKU ×N" si se repite),
   solo si hay filas.
3. **Empty state dedicado** (check verde + mensaje + indicador en vivo) en
   vez de una tabla con una sola fila de texto gris flotando.
4. **Densidad de fila auto-ajustada** al volumen real: 1-4 filas =
   cómoda (py-4, clamp 3 líneas), 5-14 = estándar (como antes), 15+ =
   compacta (py-2, clamp 1 línea) — no un mínimo de altura artificial.
5. **Pie de tabla siempre presente** ("Mostrando X–Y de Z alertas" +
   indicador en vivo), paginación ‹Ant/Sig› solo aparece con >10 filas —
   antes terminaba de golpe sin cierre visual con pocas filas.

Verificado con Playwright (instalado en esta sesión, no existía antes) +
JWT local: capturas en 1920×1080 con 0 filas (empty state) y con 4 filas
de prueba insertadas directo en `tokens.db` (borradas después de verificar).
Durante la verificación se encontró y corrigió de paso un bug real
preexistente — la columna "Disp. BM / Precio" (90px) era muy angosta para
su propio título de encabezado y se superponía visualmente con
"Sugerencia"; se amplió a 130px.

---

## 2026-07-21 — FIX: LocationID 62 no era Tijuana (era Cuautitlán CDMX) — nuevo set de stock vendible incluye Tijuana real

**Archivos:** `app/services/binmanager_client.py`, `app/api/amazon_products.py`,
`app/api/items.py`, `app/api/productos.py`, `app/api/lanzar.py`,
`app/api/sku_inventory.py`, `app/services/stock_sync_multi.py`, `app/main.py`,
`.claude/agents/binmanager-specialist.md`, `CLAUDE.md`.

Reinvestigando un análisis de la categoría "Home Power Tools" (Jovan
contradijo el hallazgo original con conocimiento directo de que sí había
stock en Tijuana), se descubrió que **LocationID 62 nunca fue Tijuana** —
es Warehouse 13 "Cuautitlán CDMX" (código `CDMX-B2B`), físicamente en el
Estado de México. El Tijuana real es LocationID 63 (Warehouse 14), que
resultó no tener stock propio (0 registros) — el producto vendible de
Tijuana vive en Warehouse 2 "MITIJ", nunca antes consultado por el sistema.

**Bug adicional encontrado:** casi todo el código de producción (pricing
Amazon, `productos.py`, `lanzar.py`/wizard, `sku_inventory.py`, y sobre
todo `binmanager_client.get_bulk_stock()`/`get_stock_with_reserve()` —
la función raíz que alimenta pricing, el sync que empuja cantidad a
listings ML/Amazon, gaps y reorden) usaba el default `"47,68"`, excluyendo
también Cuautitlán del cálculo de vendible.

**Clasificación de las 44 ubicaciones de MITIJ** (agente `binmanager-specialist`,
consultando cada LocationID individualmente contra datos reales de BM, no
por nombre): **45, 69, 43, 42** confirmadas como vendible real (~410K
unidades en TVs, TV Stands, Cables, Remote Controls, Iluminación, Home
Goods, Ventiladores). El resto — incluyendo las 10 "To Mexico - Bin 01-10"
(tránsito), defectuosos, consignación, y el bin de 340K que resultó ser
98.6% material de empaque (cinta/bolsas/cajas) — quedó excluido a propósito.

**Set final de stock vendible implementado:** `47,62,68,45,69,43,42`
(antes `47,68` en la mayoría del código real).

Jovan aprobó hacer ambas correcciones juntas en un solo push ("vamos por
todo"), aceptando que `stock_sync_multi.py` va a subir la cantidad
disponible en listings ML/Amazon de golpe para varios SKUs (más preciso,
pero visible en producción). Verificado en local antes de push:
`/api/diag/sku` con SKU sin stock en las nuevas ubicaciones (sin cambio),
SKU con stock real confirmado en Tijuana/MITIJ (pasó de 0 a valor real), y
el SKU de referencia histórico (SNTV001764, sin regresión).

Ver `.claude/memory/project_bm_locationid_62_63_swap.md` para el detalle completo.

---

## 2026-07-21 — FEAT: Alertas de Stock se mueve de Sync Stock (admin_only) a Ventas (accesible a todos)

**Archivos:** `app/main.py`, `app/templates/orders.html`, `app/templates/stock_sync.html`.

Jovan reportó que muchos usuarios no tienen acceso a la pestaña Sync
Stock (admin_only), donde vivía el feed de "Alertas de Stock" — se movió
completo a Ventas (`admin_only=False`), no se dejó duplicado en los dos
lugares.

- Nueva ruta `/alertas-stock` (mismo patrón que `/finanzas`, `/sku-sales`,
  `/sku-compare` — alias de `/orders` con una sub-vista pre-seleccionada
  sobre el template unificado `orders.html`).
- `/api/stock/realtime-alerts` ya no exige `role=admin` — solo sesión
  activa, igual que el resto de Ventas.
- Se quitó por completo de Sync Stock (botón, HTML, JS) y esa página
  volvió a su ancho normal (el full-bleed se había puesto específicamente
  para la tabla ancha de Alertas).

---

## 2026-07-20 — FIX: Alertas de Stock perdía órdenes por race condition orden↔envío + FULL/entregado + FX + layout

**Archivos:** `app/main.py`, `app/templates/stock_sync.html`.

Sesión larga de fixes reales sobre la feature de webhook ML del mismo día,
todos reportados por Jovan con capturas/casos concretos, ninguno adivinado:

1. **Órdenes FULL/ya entregadas aparecían como alerta.** El objeto de la
   orden de ML no trae el estado de envío embebido, solo el id del
   shipment — había que pedir `/shipments/{id}` aparte. Confirmado con la
   orden real que reportó Jovan (`2000017373456614`): era `delivered` +
   `logistic_type=fulfillment` (FULL). Ahora solo alerta si el envío sigue
   pendiente (`pending`/`handling`/`ready_to_ship`) y NO es FULL. Si una
   orden ya alertada pasa a FULL o se envía, se limpia sola en la
   siguiente notificación de esa misma orden.
2. **FX rate de respaldo (20.0) en vez del real (~17.5).** El cliente ML
   se cerraba antes de pedir el tipo de cambio — el error se tragaba
   silenciosamente por el fallback interno de `_get_usd_to_mxn()`.
   Corregido manteniendo el cliente abierto hasta terminar todo el
   procesamiento del webhook.
3. **Sugerencia de reemplazo por tamaño real, no por título.** Se agregó
   el campo `Size` real de BM (pulgadas, 99.9% de los TVs lo tienen bien
   poblado) a `bm_sku_master`. Antes solo comparaba marca+precio, lo que
   podía sugerir un TV de tamaño distinto — motivo válido de queja de
   cliente. Ahora exige tamaño EXACTO cuando el SKU original lo tiene.
4. **Layout roto** — contenedor a ancho completo (antes dejaba media
   pantalla vacía con `max-w-5xl`), tabla con columnas proporcionales +
   scroll horizontal solo como respaldo en pantallas angostas, sugerencia
   de reemplazo a una sola línea (antes apilaba 3 completas, se veía roto).
5. **Bug más profundo, encontrado con 2 casos reales que BinManager sí
   marcaba "Sin Stock" pero nuestro feed no mostraba**: la notificación
   `orders_v2` de ML a veces llega ANTES de que ML termine de asignar el
   shipment (`shipping.id` vacío en ese momento exacto) — la orden se
   guardaba en `order_history` pero nunca generaba alerta, y como no
   estábamos suscritos al topic `shipments`, ML nunca reavisaba cuando el
   envío quedaba listo. Fix de 2 capas: reintento corto (8s) si
   `shipping.id` viene vacío, Y suscripción también al topic `shipments`
   (el shipment trae su propio `order_id`, se resuelve la orden completa
   desde ahí). Verificado con las 2 órdenes reales de Jovan
   (`2000017513795808` Autobot, `2000017515937288` Blowtechnologies) —
   ambas generaron la alerta correctamente tras el fix, confirmado en
   producción vía `/api/diag/order-lookup`.

**Pendiente que Jovan debe hacer:** activar el topic **"Shipments"**
(además de "Orders_v2", ya activo) en el DevCenter de la app
APANTALLATEMX — mismo lugar donde se configuró la URL de notificaciones.
Sin esto, el reintento de 8s sigue funcionando como red de seguridad para
la mayoría de los casos, pero el topic de shipments es la cobertura
completa para cuando el envío tarda más en asignarse.

Se agregaron 3 diag endpoints nuevos: `/api/diag/clear-realtime-alerts`
(limpieza de datos contaminados por el bug, usado una vez — 12 filas
borradas en producción), `/api/diag/order-lookup?order_id=X` (investigar
una orden específica sin adivinar — el que resolvió este caso).

---

## 2026-07-20 — FEAT: webhook ML en tiempo real — feed de órdenes sin stock (Fase 1)

**Archivos:** `app/main.py`, `app/services/token_store.py`, `app/templates/stock_sync.html`.

Jovan dijo que la vista agregada (por SKU, ventana de días) no le servía —
quería identificar EN EL MOMENTO qué órdenes entran sin stock. Se
reemplazó por un feed cronológico alimentado por webhooks reales de ML
(eligió esta opción sobre polling más frecuente).

`POST /webhooks/ml/orders` (público, sin login — necesario para que ML le
pegue desde afuera) recibe la notificación del topic `orders_v2`, valida
que el `user_id` sea una de las 4 cuentas conocidas (contra `get_all_tokens()`,
no hardcodeado), responde `200` de inmediato y procesa en background:
resuelve la orden real (`resolve_order`), la guarda en `order_history`
reutilizando `_save_ml_orders_history_bg` (cero lógica duplicada — mismo
choke point que ya alimenta deuda), y chequea el SKU contra `bm_sku_master`.
Si `available_qty <= 0` o sin dato → nueva tabla `realtime_stock_alerts`
(idempotente vía `UNIQUE(order_id, sku, platform)` — reenvíos de ML no
duplican). Nuevo endpoint `GET /api/stock/realtime-alerts` + UI con
auto-refresh cada 30s.

**Amazon queda como Fase 2** — SP-API Notifications requiere una cuenta de
AWS propia (SQS/EventBridge); no se confirmó si ya existe una, así que se
avanzó primero con ML (no depende de nada externo más que la config de
ML DevCenter).

**Pendiente que Jovan debe hacer (fuera de este código):** entrar al
DevCenter de cada una de las 4 apps ML y poner como Notifications URL
`https://apantallatemx.up.railway.app/webhooks/ml/orders` con el topic
`orders_v2` activado. Sin esto, el endpoint está vivo pero no recibe
tráfico real de ML todavía.

Verificado localmente de punta a punta antes de subir: orden con stock
real → correctamente NO genera alerta; orden con SKU en 0 → SÍ genera
alerta; reenvío del mismo webhook → no duplica. Deploy confirmado
(commit `9cd445b`, status SUCCESS) — endpoint probado en vivo en
producción respondiendo correctamente a un `user_id` desconocido.

---

## 2026-07-20 — FEAT: maestro único BM (bm_sku_master) + historial de cambios

**Archivos:** `app/services/token_store.py`, `app/main.py`.

Jovan pidió tener todos los SKUs de BM guardados como "maestro" — solo
actualizar las líneas que cambian, no reescribir todo — y usarlo como base
para alertas, sugerencias y lanzamientos futuros.

Se fusionaron `bm_product_catalog` (título/retail/costo, semanal) y
`bm_stock_snapshot` (stock, ~10 min) en una sola tabla `bm_sku_master` —
una fila por SKU, con `catalog_updated_at`/`stock_updated_at` separados
porque cada bloque se refresca a ritmo distinto. Migración automática al
arrancar (backfill desde las 2 tablas viejas, que se dejan de escribir
pero NO se borran — rollback seguro).

Nueva tabla `bm_sku_changes`: cada sync que ya corre hoy compara el valor
nuevo contra el guardado antes de sobreescribir y loguea el cambio real
(no cualquier re-guardado idéntico). Retail/costo: cualquier cambio se
loguea (bajo volumen, semanal). Stock: solo transiciones que cruzan cero
(se quedó en 0 / se resurtió) — evita llenar el historial de
micro-fluctuaciones de cada ciclo de 10 min. Cero llamadas nuevas a BM.

Todas las funciones existentes (`upsert_bm_catalog_batch`,
`get_bm_catalog_all`, `get_orders_without_stock`,
`get_supplier_debt_export_data`, los diag de `/api/diag/supplier-debt` y
`/api/diag/bm-stock-snapshot`) se mantuvieron con el mismo nombre/firma
pero ahora leen/escriben en el maestro unificado — cero call sites nuevos
en `main.py`. Verificado localmente (migración de 8800+ SKUs, detección
de un cambio de precio real, chunks de 500 para no pegarle al límite de
variables SQL de SQLite) y en producción tras el deploy (commit
`9811319`, status SUCCESS): 11,271 SKUs migrados, `/api/diag/supplier-debt`
y `/api/diag/bm-stock-snapshot` funcionando correctamente sobre la tabla
nueva.

---

## 2026-07-20 — FIX: costo USD del export de deuda salía mayor al retail (data-quality)

**Archivos:** `app/main.py`, `app/services/token_store.py`, `app/api/supplier_debt.py`.

Jovan notó en el Excel de deuda que "Costo (USD)" a veces salía MAYOR que
"Retail (USD)" — imposible en un negocio normal. Causa raíz: `_sku_cost_map`
(en memoria) solo se llenaba con SKUs de `bm_candidates` (subconjunto
angosto usado para scoring de Deals) y **nunca se limpiaba** — arrastraba
valores viejos/parciales de semanas atrás, mientras `_sku_retail_map` (el
que usaba "Retail") sí se reconstruía completo cada ciclo desde el catálogo
BM entero. No era un error de fórmula, era un desfase de frescura entre
ambos datos.

Confirmé antes de tocar nada que el mismo request semanal que ya trae
retail (`ConfColumns_Conditions_Excel`) YA trae `AvgCostQTY` por SKU para
el catálogo completo — se descartaba sin guardar. Fix: se agrega columna
`cost_usd` a `bm_product_catalog` (igual que ya existe para retail),
nuevo `_bm_cost_cache` poblado en los mismos 2 puntos donde ya se puebla
`_bm_retail_ph_cache`, y `_prewarm_caches` ahora reconstruye `_sku_cost_map`
por completo (`.clear()` + rebuild) igual que ya hacía con retail — sin
ninguna llamada nueva a BM.

De paso, el export de deuda ahora también muestra "Costo (MXN)" y "Costo
Total (MXN)" (usando el tipo de cambio activo — manual si está seteado,
si no el último detectado). Verificado localmente: de 2625 SKUs con costo,
solo 16 siguen mostrando costo > retail (modelos descontinuados/datos BM
obsoletos — razonable), vs. antes donde el problema era generalizado.
Deploy confirmado vía Railway GraphQL API (commit `a5e4adc`, status SUCCESS).

**Corrección post-deploy (commits `98252b0`, `67dc358`):** el deploy agregó
la columna `cost_usd` vía `ALTER TABLE ... DEFAULT 0`, pero eso NO la
llena — Jovan bajó el Excel de producción y salió con Costo(USD)/Costo(MXN)
completamente vacíos porque el catálogo de producción no se había vuelto a
sincronizar desde el deploy (el cron semanal corre hasta el domingo). Se
agregó `GET /api/diag/trigger-catalog-sync?token=...` (mismo patrón que
el resto de `/api/diag/*`, sin sesión admin) para disparar el sync manual
en producción sin esperar el cron, y se extendió `/api/diag/supplier-debt`
con `skus_with_cost`/`ledger_skus_with_cost` para poder confirmar la
cobertura real sin necesitar login. Verificado: `skus_with_cost` pasó de
0 a 2624 tras el trigger manual. **Lección:** un deploy con status SUCCESS
solo confirma que el código corrió — cuando el fix depende de un dato que
vive en una tabla/cache que se sincroniza periódicamente (no en cada
deploy), hay que disparar y verificar ESA sincronización en producción
también, no solo en local.

---

## 2026-07-20 — FEAT: descargar Excel de la deuda por semana individual

**Archivos:** `app/api/supplier_debt.py`, `app/services/token_store.py`,
`app/templates/deuda_empresa.html`.

`GET /api/supplier-debt/export?week=2026-W29` filtra el export a solo esa
semana (`get_supplier_debt_export_data(iso_week=...)` ahora acepta el
filtro opcional). Nuevo link "↓ Excel" en cada fila de la tabla "Deuda por
Semana"; el botón de arriba se renombró a "Descargar Excel (todas las
semanas)" para diferenciarlo. Deploy confirmado vía Railway GraphQL API
(commit `897e2ec`, status SUCCESS).

---

## 2026-07-20 — FEAT+FIX: export a Excel de la deuda + rango de fechas por semana

**Archivos:** `app/api/supplier_debt.py`, `app/services/token_store.py`,
`app/templates/deuda_empresa.html`, `app/main.py`, `requirements.txt`.

Dos mejoras a `/deuda-empresa`:

- **Export a Excel** (`GET /api/supplier-debt/export`, botón "Descargar
  Excel"): una fila por SKU con título (join `bm_product_catalog`), retail
  USD, costo USD (de `order_history`), unidades vendidas y monto de deuda
  generado. `openpyxl` agregado explícitamente a `requirements.txt` (antes
  solo llegaba como dependencia transitiva, sin garantía en el build de
  Railway). `/api/diag/supplier-debt` ahora también reporta
  `bm_catalog_rows`/`skus_with_title` para verificar el join sin necesitar
  sesión admin.
- **Rango de fechas por semana**: Jovan notó que "2026-W29" no dice qué
  días cubre, necesario para cruzar contra pagos reales. Se agrega
  `week_range` (ej. "13-19 jul 2026", lunes a domingo) calculado desde el
  código ISO de semana, mostrado junto al código en la tabla.

**Verificación en producción del fix de auto-sanado (commit 6f968ce,
2026-07-19)**: confirmado exitoso vía `/api/diag/supplier-debt` — de 428
filas en $0 pasó a 798 filas con solo 184 aún en $0 (mayoría ya sanadas),
$1,707,618.04 MXN generados, ambas plataformas representadas (461 ML +
337 Amazon).

Deploy confirmado vía Railway GraphQL API (commit `a1f8ce7`, status
SUCCESS).

---

## 2026-07-19 — FIX: loop automático de captura de ventas para las 7 cuentas (deuda con la empresa)

**Archivo:** `app/main.py` (`_supplier_debt_sync_loop`, `start_supplier_debt_sync`).

Jovan reportó que `/deuda-empresa` mostraba $0 recién desplegado y preguntó
si debíamos llevar control de TODAS las ventas en TODAS las cuentas —
correcto. Causa real: `order_history` (que alimenta el ledger) nunca fue
un proceso que corriera solo — `_save_ml_orders_history_bg` solo se
disparaba al visitar Deals (ML) y `_save_amazon_orders_bg` solo al visitar
Planeación→Velocidad (Amazon), cuenta por cuenta.

Nuevo `_supplier_debt_sync_loop()` (mismo patrón que `_stock_sync_loop`,
arrancado en `lifespan()`): cada hora recorre las 4 cuentas ML (ventana de
3 días, `upsert_order_history` ya es idempotente) y dispara
`_save_amazon_orders_bg` (ya cubre las 3 cuentas Amazon internamente, con
su propio guard de 2h — no hubo que tocar su lógica). Probado localmente
con datos reales: el saldo pasó de $0 a $552,177.97 (425 entradas ML + 56
Amazon, 587 unidades) sin errores en ninguna cuenta. Deploy confirmado vía
Railway GraphQL API (commit `1f9b2fa`, status SUCCESS).

---

## 2026-07-19 — FEAT: ledger de deuda semanal con la empresa proveedora

**Archivos:** `app/services/token_store.py`, `app/main.py`. Nuevos:
`app/api/supplier_debt.py`, `app/templates/deuda_empresa.html`.

Jovan pidió llevar control de cuánto se le debe a la empresa proveedora por
cada producto vendido — % fijo del retail del SKU: **80% en teles (prefijo
SNTV), 50% en todo lo demás**, ambos configurables desde la UI. Arranca
desde hoy (sin backfill de ventas históricas), ML+Amazon combinado en un
solo saldo.

El cálculo se engancha en `upsert_order_history` — el único choke point
real compartido por ML y Amazon (ambas plataformas pasan por ahí en cada
resync de órdenes, no es un stream de una sola pasada). Reusa
`retail_ph_usd` y `fx_rate` que cada row de `order_history` ya guarda al
momento de la venta — cero llamadas nuevas a BM o al tipo de cambio.
Cancelaciones/reembolsos ya vienen filtrados antes de llegar aquí (`status
in ("paid","delivered")`), así que nunca se cuenta deuda de una venta que
no se concretó.

**Doble conteo resuelto con una constraint, no con lógica de aplicación**:
la tabla nueva `supplier_debt_ledger` tiene `UNIQUE(order_id, item_id,
platform)` y el insert es `INSERT OR IGNORE` — un resync del mismo pedido
simplemente no genera una segunda entrada, sin necesidad de un `SELECT` de
existencia previo (atómico, sin ventana de carrera). Verificado con test
aislado: 3 resyncs del mismo pedido → exactamente 1 entrada de deuda por
SKU, con el monto correcto para ambas categorías.

Nueva página `/deuda-empresa` (admin-only, mismo guard de rol que Sync
Stock): tarjeta de saldo (generado − pagado), tabla semanal (unidades
teles/otras + monto), formulario para registrar pagos (fecha/monto/
referencia/notas) con historial y opción de eliminar, y tarjeta de
configuración de las 2 tasas — cambiar la tasa no recalcula lo ya
generado, cada entrada del ledger guarda su propio `category_rate`.

Nuevo módulo `app/api/supplier_debt.py` (5 endpoints), mismo patrón que
`app/api/lanzar.py`. Verificado con Playwright (0 errores de consola,
registrar/eliminar pago actualiza el saldo correctamente). Deploy
confirmado vía Railway GraphQL API (commit `be2c82a`, status SUCCESS).

---

## 2026-07-18 — FEAT: Finanzas → 4ta sub-vista de Ventas, Distribución → sub-vista de Sync Stock

**Archivos:** `app/main.py`, `app/templates/orders.html`, `app/templates/stock_sync.html`.
Eliminados: `finanzas.html`, `distribucion.html`.

Continuación de la reducción de pestañas del nav. Ambos casos tenían la
misma señal que ya funcionó antes (`section` compartida con otro tab en
`_NAV_TAB_DEFS`):

- **Finanzas** (`section="ventas"`, igual que Deals/Listings antes) se
  fusionó como 4ta sub-vista de `/orders` (Por Orden / Por SKU / Comparativa
  / **Finanzas**). Sigue el patrón de ruta propia con `view` hardcodeado
  (igual que `/sku-sales`/`/sku-compare`, NO un redirect) — `/finanzas` sigue
  siendo una URL real. El endpoint `/api/ml/finanzas-summary` no cambió
  (sigue sin parámetros de fecha, cache de 30 min intacta). Amazon no se
  tocó — ya tenía Finanzas dentro de su propio dispatcher.
- **Distribución** (`section="sync"`, igual que Sync Stock) se fusionó como
  sub-vista "Configurar" de `/stock-sync` (la otra sub-vista, "Ejecutar", es
  todo el contenido que ya existía ahí, sin tocar). A diferencia de los
  merges anteriores, aquí el toggle sincroniza la URL vía
  `history.replaceState('/stock-sync?view=...')` — necesario porque Sync
  Stock ya dispara `location.reload()` automático al terminar un ciclo de
  sync, y sin esa sincronización ese reload hubiera regresado siempre a
  "Ejecutar" borrando la sub-vista activa a medio ajuste de reglas.
  Verificado con Playwright que un F5 en "Configurar" preserva la sub-vista.
  Todo el contenido portado de `distribucion.html` usa IDs/funciones con
  prefijo `dist` (`distSaveRule`, `#dist-toast`, etc.) porque Sync Stock ya
  tenía su propia `saveRule()`/`showToast()`/`#toast` para las reglas
  SKU/plataforma — confirmé el choque de nombres antes de portar, no fue
  solo precaución. `/distribucion` ahora redirige (302) a
  `/stock-sync?view=configurar` — de paso cierra un hueco real: esa ruta no
  tenía guard de admin en el backend (solo el nav la ocultaba), ahora
  hereda el guard de `stock_sync_page`.

Verificado con Playwright en ambos casos (0 errores de consola, toggles sin
recarga completa de página, nav sin las pestañas retiradas, Amazon
intacto). Deploy confirmado vía Railway GraphQL API (commits `ee32933` y
`69acbb9`, status SUCCESS).

---

## 2026-07-18 — FIX: ocultar "Inv.Global" del nav + revisión del resto de pestañas

**Archivo:** `app/main.py` (dict `"inventory_global"` en `_NAV_TAB_DEFS`).

Jovan pidió revisar si quedaba algo más por unificar tras Ventas/SKU y
Productos/Deals/Listings, y confirmó que "Inv.Global" no la usan. Investigué
el resto del nav (Gral, Dashboard, Salud, Retornos, Planeación, Finanzas,
Sync Stock, Distribución, FBA Amazon) — ninguno tiene traslape real, cada
uno hace algo genuinamente distinto (ver razonamiento en la conversación,
no vale la pena repetirlo aquí). Único hallazgo: Inv.Global no es duplicado
de Sync Stock/Distribución (esas automatizan reparto proporcional cada 5
min; Inv.Global es un escaneo manual con una función única — "Concentrar
Stock" a una sola cuenta ganadora — que no existe en ningún otro lado).

Se ocultó del nav de ML reusando el flag `ml_hidden` (mismo mecanismo que
Listings/Deals) — la ruta `/inventory-global` y su función de concentración
siguen intactas, solo dejaron de aparecer en el menú.

---

## 2026-07-18 — FEAT: fusión de Deals + Listings dentro de Productos (ML), retiro de tab "Oportunidades" duplicado

**Archivos:** `app/main.py`, `app/templates/items.html`. Eliminados:
`deals.html`, `listings.html`, `partials/products_not_published.html`.
Nuevo: `partials/products_listings.html`.

Jovan preguntó si Productos y Listings también se podían unificar, dado que
Productos ya tenía el concepto de "no lanzados". Investigación encontró un
problema más grande de lo esperado:

- **`/deals` era un duplicado literal**: un wrapper que solo llamaba a
  `/partials/products-deals` — el MISMO partial que ya era una de las 6
  pestañas internas de `/items`. Para funcionar solo, `deals.html` había
  copiado y pegado `switchProductTab`/`inlineStockSync`/`quickSyncBM`/
  `showStockEditor` de `items.html`.
- **La pestaña "Oportunidades" de `/items` estaba rota y era redundante**:
  mostraba SKUs de BM sin listing en ML calculado en vivo (sin cache), y su
  único CTA ("Lanzar" → `/sku-inventory?sku=...`) no funcionaba —
  `/sku-inventory` es un redirect 301 a `/productos` que descarta el
  parámetro `?sku=`. El mismo concepto ya vive completo y funcional en
  `/bm/unlaunched` (tabla persistente, wizard de publicación real, 7
  sub-pestañas) — ya enlazado un nivel arriba en `productos_subnav.html`. Se
  eliminó la pestaña en vez de arreglarla.
- **`/listings` (Quality Score A-D) no era duplicado de nada** — se movió
  como 6ª pestaña de `/items` reusando `/api/ml/listing-quality` sin cambios.
- **Amazon no necesitó ningún cambio** — `amazon_dashboard.html` ya tenía
  Listings y Deals como secciones del mismo dispatcher, no páginas separadas.

`/deals` y `/listings` ahora son redirects 302 a `/items?tab=deals` /
`/items?tab=listings` (no rompen bookmarks) — `items.html` ganó soporte de
`?tab=` en el query string para pre-seleccionar la pestaña correcta al
cargar. Nav: nuevo campo `ml_hidden`/`amz_hidden` en `_build_nav_tabs` para
ocultar una pestaña por completo en una plataforma (distinto de
deshabilitada/gris) — Listings/Deals ya no aparecen en el nav de ML pero
siguen funcionando igual en Amazon.

**Fuera de alcance a propósito** (encontrado en la investigación, pero son
iniciativas más grandes que merecen su propio plan): `productos.html`
(`/productos`) tiene su propio wizard de lanzamiento de 4 pasos — un 4º
sistema de "lanzar" distinto al de `lanzar_gaps.html`, solapa con
Resumen/Inventario de `/items` pero fusionarlo implica tocar un wizard de
producción real. `/bm/unlaunched` y `/productos/sin-bm` no se tocaron — son
las herramientas canónicas y ya funcionan bien cada una en su propia página.

Verificado localmente con Playwright: las 6 pestañas de `/items` cargan
igual que antes, `/deals`/`/listings` redirigen y pre-seleccionan su
pestaña, nav de ML sin Listings/Deals, nav de Amazon sin cambios, 0 errores
de consola reales (el único "error" en consola fue un 401 pre-existente de
`/api/ml/listing-quality` sin cuenta activa en el entorno de prueba local,
no una regresión). Deploy confirmado vía Railway GraphQL API (commit
`51fdbb7`, status SUCCESS).

---

## 2026-07-18 — FEAT: fusión de tabs Ventas + SKU en un solo tab (ML y Amazon)

**Archivos:** `app/main.py`, `app/templates/orders.html`, `app/templates/base.html`,
`app/templates/amazon_dashboard.html`, `app/static/js/amazon_dashboard.js`,
`app/services/user_store.py`, `app/templates/usuarios.html`.
Eliminados: `sku_sales.html`, `sku_compare.html`, `partials/sku_subnav.html`,
`amazon_sku_sales.html` (contenido migrado, no perdido).

Jovan notó que "Ventas" y "SKU" mostraban info parecida en pestañas separadas
con filtros de fecha duplicados. Se fusionaron en un solo tab "Ventas" con
3 sub-vistas cliente-side (sin recargar página):

- **ML** (`/orders`): "Por Orden" / "Por SKU" / "Comparativa". Las primeras
  dos comparten el filtro de fechas (presets Hoy/7d/15d/30d/Todo + rango
  custom) — antes "SKU" no tenía ni presets ni KPIs propios. Comparativa
  mantiene su selector de dos rangos independiente. `/sku-sales` y
  `/sku-compare` siguen funcionando como alias que pre-seleccionan su
  sub-vista en el mismo template — no se rompen bookmarks.
- **Amazon** (`/amazon?tab=ventas`): la pestaña SKU se convirtió en una
  sección "Por SKU" dentro de Ventas con el mismo estilo de pill-toggle que
  ya usaba FBA/FBM. Los 4 widgets existentes (Briefing, ASIN, Órdenes, Top
  Productos) quedan intactos bajo "Resumen". `/amazon/sku-sales` ahora
  redirige a `/amazon?tab=ventas`. Se eliminó una copia duplicada de
  `_renderPaginated` (quedó solo la de `amazon_dashboard.js`).
- **Nav**: se eliminó la entrada `"sku"` de `_NAV_TAB_DEFS` en ambas
  plataformas — ya no existe una pestaña "SKU" en ningún lado. El shortcut
  móvil "Comparativa" se movió de la pestaña SKU (ya no existe) a Ventas; el
  shortcut "Lanzar" (`/sku-inventory`, que ya redirigía a `/productos`) se
  movió a Productos.
- **Permisos**: la sección `"sku"` de `allowed_sections` se fusionó en
  `"ventas"` — migración automática en `init_user_db()` agrega `"ventas"` a
  cualquier usuario que tuviera `"sku"` suelto, para no perder acceso.

**Hallazgo incidental:** `loadInventoryForSkus()` en el `sku_sales.html` viejo
llamaba a un endpoint (`/api/items/inventory-sku-sales`) que ya no existe —
código muerto desde que la columna "Stock BM" pasó a venir server-side vía
Jinja. Se eliminó al portar la lógica, no se re-implementó.

Verificado localmente con Playwright (Chromium headless): las 3 sub-vistas
ML y las 2 de Amazon cambian sin recarga, el rango de fechas persiste entre
"Por Orden"/"Por SKU", las URLs viejas siguen sirviendo el sub-vista correcta,
0 errores de consola. Deploy confirmado vía Railway GraphQL API (commit
`d812892`, status SUCCESS).

---

## 2026-07-18 — FIX URGENTE: incidente de disk-full (segunda vez) — causa raíz de facturas + regla de 30 días para fotos

**Archivos:** `app/main.py`, `app/services/token_store.py`, `app/templates/facturacion.html`

**Incidente:** el volumen de Railway (500MB) se llenó por completo
(`sqlite3.OperationalError: database or disk is full`, confirmado en logs) y
tumbó TODA la app — incluido `/login/verify`. Reportado por Jovan primero como
"no puedo subir una factura" (error de JS "Unexpected token I, Internal S...
is not valid JSON") y luego como "no puedo ni entrar a la app".

**Mitigación inmediata:** `/api/diag/emergency-clear-claim-photos` (ya
existente del fix de ayer) — liberó 113.93MB, sitio restaurado en minutos.

**Causa raíz encontrada:** `billing_invoices` guardaba cada PDF/XML de factura
como BLOB directo dentro de SQLite (hasta 10MB c/u, 463 filas, sin tope
agregado) — cada factura nueva reescribía el archivo completo de la base de
datos. Es la peor forma posible de guardar binarios en SQLite y explica por
qué el `VACUUM` de la limpieza de emergencia falló (`vacuum_ok: false`).

**Fix de fondo:**
- `save_billing_invoice()`/`get_billing_invoice()` ahora escriben/leen
  `uploads/invoices/{request_id}.pdf|.xml` en disco en vez de columnas BLOB —
  misma firma y forma de retorno, cero cambios en los 6 call sites existentes
  (admin + cliente, upload + download + delete). Migración de las 463 filas
  existentes vía nuevo endpoint puntual `/api/diag/migrate-billing-invoices-to-disk`
  (idempotente) — corrida contra producción: 463 migradas, 0 errores.
- `facturacion.html`: `_uploadInvoice` ahora revisa `response.ok` antes de
  `.json()` — antes un 500 con cuerpo de texto plano tronaba como el
  `SyntaxError` que reportó Jovan; ahora muestra un mensaje entendible.
- `_CLAIM_PHOTOS_BUDGET_MB` bajado de 120 a 40 — el fix de ayer dejaba muy
  poco margen combinado con la DB de ~309MB.
- **Nueva regla, a petición de Jovan**: `claim_photos/` ahora también respeta
  un máximo de 30 días de antigüedad, ADEMÁS del tope de tamaño (no en vez de
  — un tope solo por días no protege contra ráfagas como la del 2026-07-15,
  835 fotos/1.5GB de golpe, todas con <30 días). Sin pérdida real de datos:
  las fotos son caché de lo que ya está en Mercado Libre, se recachean solas.

**Nota honesta:** el `VACUUM` post-migración sigue sin poder correr — necesita
~2x el tamaño de la DB (309MB) en espacio libre temporal, y el volumen de
500MB no da para eso. El archivo de la DB se queda en 309MB por ahora (no
baja), pero ya no va a seguir creciendo por facturas nuevas. Volumen total
post-fix: 341MB de 500MB (vs 423MB justo antes del incidente).

**Pendiente:** correo ya redactado (por Jovan, no por Claude — sin conector de
correo activo en esta sesión) a `coolify01@mi2.com.mx` pidiendo storage MinIO/S3
para sacar fotos y facturas del volumen de Railway por completo. Sin
respuesta aún.

---

## 2026-07-17 — FEAT: Fase 2 completa — Deals ML, Listings ML, Finanzas ML, SKU Amazon

**Archivos:** `app/main.py`, `app/services/amazon_client.py`, templates nuevos
`deals.html`, `listings.html`, `finanzas.html`, `amazon_sku_sales.html`

**Motivación:** cierra el roadmap de Fase 2 del nav unificado (Retornos Amazon ya
había salido antes) — las 4 pestañas restantes que quedaron deshabilitadas al
unificar el nav. Jovan pidió hacerlas todas, una por una, sin pausas. Ads queda
fuera (requiere alta de app nueva en Amazon Developer Central).

- **Deals ML** (`/deals`): el cálculo ya existía completo
  (`/partials/products-deals`, embebido en Productos) — solo se expuso como
  pestaña propia. Se portaron 3 funciones JS de `items.html`
  (`inlineStockSync`/`showStockEditor`/`quickSyncBM`) que el partial necesita para
  edición inline de stock. Verificado con datos reales: 208 deals activos, 52
  candidatos.
- **Listings ML** (`/listings`): `ml_listing_quality` ya tenía el score completo
  — nuevo endpoint `/api/ml/listing-quality` agrega grado A/B/C/D (mismo corte
  que ya usa Amazon: ≥85/≥70/≥55/resto) + `issues[]` + resumen, antes solo
  vivían client-side. Verificado: 958 publicaciones, avg score 88.9.
- **Finanzas ML** (`/finanzas`): a diferencia del ~20% estimado que usa Amazon,
  ML ya tenía el dato REAL de fees por orden (`order_net_revenue` +
  `enrich_orders_with_net_amount/shipping`, ya usados en `/partials/metrics`).
  Sin tabla de liquidaciones — ML no agrupa pagos en settlements como Amazon
  (paga por transacción), se omite en vez de inventarla. Cacheado 30min con
  refresh en background (stale-while-revalidate) — calcular 2 meses completos
  con `enrich_orders_with_shipping` (secuencial, 1 request/orden) puede tardar
  varios minutos en cuentas de alto volumen. Verificado con datos reales
  (AUTOBOT MEXICO): Jul 2026 $498,292/146 órdenes, Jun 2026 $3,641,850/774
  órdenes.
- **SKU Amazon** (`/amazon/sku-sales`): la única sin atajo barato — nuevo
  `get_sku_sales(date_from, date_to)` en `amazon_client.py` extiende el patrón
  N+1 de `get_sales_summary_30d()` con rango configurable + ingreso. **Bug real
  encontrado y corregido**: `CreatedBefore` con "hoy 23:59:59" cae en el futuro
  respecto al momento real → Amazon lo rechaza (SP-API exige ≥2min en el
  pasado) — se acota al menor entre fin-de-día solicitado y ahora-5min.
  Confirmado también que `getOrderItems` tiene rate limit propio estricto
  (429/QuotaExceeded incluso con Semaphore(2)) — el retry-con-backoff ya
  existente lo resuelve solo pero puede tardar; cache 6h (el más largo de los
  4, por ser el más caro de recalcular).

**Patrón común a las 4:** mismo registro `_NAV_TAB_DEFS` (solo cambiar
`ml_href`/`amz_href` de `None` a la ruta real + `amz_gated`/`section` según
corresponda), mismo criterio de honestidad que Retornos Amazon — donde una
plataforma no tiene un concepto real (Abiertos/Resueltos, settlements), se
omite en vez de inventarlo.

---

## 2026-07-17 — FIX: nav ya no hace scroll horizontal en pantallas anchas

**Archivos:** `app/templates/base.html`

**Motivación:** tras el nav unificado, Jovan reportó (con captura) que el nav ahora
mostraba scroll horizontal y espacio vacío a los lados en su monitor ancho — el
contenedor del nav estaba limitado a `max-w-7xl` (1280px) sin importar qué tan ancha
fuera la pantalla, mientras las 17 pestañas (unión ML+Amazon) desbordaban ese ancho.

**Fix:** el nav pasa de `max-w-7xl mx-auto` a `w-full` (usa todo el ancho disponible,
la barra de contenido principal más abajo sigue centrada en `max-w-7xl` sin cambios),
más padding/gap reducido en las pestañas. Verificado con Playwright headless a 1920px:
0px de overflow (antes desbordaba); a 1366px (laptop común) queda un desborde menor
esperado dado que ahora son 17 pestañas en vez de 13.

---

## 2026-07-17 — FEAT: Retornos Amazon (Fase 2.1 del nav unificado) — vista de una sola cuenta con razón real vía Reports API

**Archivos:** `app/main.py`, `app/templates/amazon_returns.html`,
`app/templates/partials/amazon_returns_summary.html`, `app/templates/base.html`

**Motivación:** primera de 5 features de Fase 2 acordadas con Jovan tras el nav
unificado — Retornos existía solo para ML; del lado Amazon la pestaña estaba
deshabilitada porque nunca se construyó una vista de una sola cuenta (solo existía
el agregado cross-cuenta del widget "Top Retornos Global").

**Decisiones de alcance (confirmadas con Jovan):**
- Sin tarjetas "Abiertos"/"Resueltos" — un refund Amazon es un evento financiero ya
  completado, no existe estado "abierto" en los datos (antes ni siquiera se calculaba,
  estaba hardcodeado a `closed`). Grid de 4 KPIs en vez de 6.
- Razón real del reembolso SÍ se integra ahora (no solo el fallback genérico que
  existía antes) vía `get_returns_report()` — Reports API
  `GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE`, ya definida pero nunca usada. Limitación
  real de Amazon: **solo cubre FBA** — FBM cae al fallback "Devolución Amazon",
  comunicado explícitamente en la UI.

**Backend nuevo:**
- `_fetch_amazon_returns_report_cached(seller_id, days)` — cache 6h (reportes son
  caros de generar, primera carga hace polling ~30-60s+).
- `_AMZ_REASON_SEVERITY` — tabla de severidad por código de razón Amazon (mismo
  formato que `_REASON_SEVERITY` de ML), solo penaliza cuando la razón real se conoce.
- `_aggregate_amazon_returns_by_sku(seller_id, days)` — helper compartido que
  combina refunds (Finances API, todas las órdenes) + reasons report (FBA) por
  `order_id`. **Reemplaza la lógica duplicada** que `_compute_unified_returns` tenía
  inline para su rama Amazon — el widget cross-cuenta ahora también se beneficia de
  razones reales para órdenes FBA.
- 4 endpoints nuevos: `/partials/amazon-returns-summary`, `/api/amazon/returns/timeline`,
  `/api/amazon/returns/quality-scores`, `/api/amazon/returns/top-skus`.
- Página `/amazon/returns` (mismo patrón que `/amazon/products`).
- `_NAV_TAB_DEFS`: tab `returns` ahora con `amz_href` + `amz_gated=True`.

**Bug encontrado y corregido en el mismo cambio:** `get_order_metrics(granularity="Total")`
retorna una **lista** de 1 elemento, no un dict — el chequeo `isinstance(metrics, dict)`
fallaba silenciosamente y "Tasa de Retorno" siempre mostraba 0%. Corregido antes de push.

**Validado con datos reales (VECKTOR IMPORTS):** 34 refunds/30d, tasa de retorno 3.3%
(34/1024 órdenes vía Sales API), Quality Score y Top SKUs poblados correctamente, sin
errores de consola (Playwright), gating por rol correcto en ambas plataformas.

**Pendiente Fase 2 (roadmap, no en este cambio):** SKU Amazon, Finanzas ML,
Listings/Deals promovidos a vista propia, FBA/Sync Stock/Distribución cross-platform.

---

## 2026-07-17 — FIX/FEAT: nav unificado ML ↔ Amazon + hotfix del switch de cuentas

**Archivos:** `app/main.py`, `app/templates/base.html`

**Motivación:** al cambiar de cuenta ML a Amazon (o viceversa) desde ciertas páginas
(ej. `/returns`), la app quedaba en un estado híbrido roto — el nav de arriba cambiaba
de plataforma pero el contenido de la página se quedaba de la plataforma anterior
("Cuenta activa: BLOWTECHNOLOGIES" con nav de Amazon y VECKTOR seleccionado arriba).

**Causa raíz (dos bugs):**
1. `_ML_ONLY_PATHS` (usado por `/auth/switch-amazon` para decidir si sacarte de la
   página ML actual) tenía nombres de ruta viejos en español (`/retornos`, `/ventas`,
   `/salud`, `/planeacion`, `/sync-stock`) que ya no existen — las rutas reales son
   `/returns`, `/orders`, `/health`, `/planning`, `/stock-sync`. El guard nunca disparaba
   para la mayoría de las páginas ML.
2. Los handlers `/amazon`, `/amazon/products` y `/amazon/orders` seteaban
   `ctx["active_platform"] = "amazon"` mientras que el cookie y **todos** los checks de
   `base.html` comparan contra `"amz"` — inconsistencia de string independiente del bug 1.

**Fix + nav unificado:** se construyó `_build_nav_tabs()` (main.py) — un registro único
de las 17 pestañas (13 ML + 8 Amazon, unión completa) que reemplaza los 4 bloques Jinja
duplicados (desktop/mobile × ML/Amazon) en `base.html` por un solo loop compartido.
Cada pestaña declara su href por plataforma; si no aplica a la plataforma activa se
muestra deshabilitada/gris con tooltip en vez de desaparecer o romper el layout.
`_ML_ONLY_PATHS`/`_AMZ_ONLY_PATHS` ahora se derivan de ese mismo registro — ya no puede
volver a desincronizarse. Bono: **Planeación** y **Facturación** ahora también son
accesibles desde el nav de Amazon (el backend de `/planning` ya combinaba ML+Amazon).

**Validado localmente:** admin en ambas plataformas (desktop+mobile), gating por rol
verificado directamente contra `_build_nav_tabs()` (editor con `allowed_sections`
limitado ve solo sus tabs permitidas en ambas plataformas), y reproducción exacta del
bug reportado — `POST /auth/switch-amazon` desde `/returns` ahora responde
`Location: /amazon` en vez de dejarte a medias.

**Pendiente (Fase 2, no en este cambio):** Retornos, SKU y Listings/Deals promovidos a
vista propia en la plataforma que aún no los tiene (los datos backend ya existen para
Amazon Retornos vía `get_refunds_30d`/`_compute_unified_returns`).

---

## 2026-07-17 — FIX: tope duro de disco en claim_photos/ (causa raíz de los incidentes recurrentes de "disk full")

**Archivos:** `app/main.py`, `app/services/token_store.py`

**Motivación:** el incidente de disco lleno (login roto en las 4 cuentas ML,
`refresh_token` fallando con "database or disk is full") se repitió el 2026-07-17 — esta
vez causado por mi propio feature del día anterior (backfill on-demand de fotos en el
buscador de SKU), que acumuló 113MB sin que nadie lo notara hasta que volvió a tumbar el
login. Cada request individual ya tenía un tope (30 fotos/request en el backfill, 1 en el
proxy de galería), pero **nada acotaba el total acumulado a lo largo del tiempo** —
Railway Volume es 500MB compartido con la DB (~310MB), así que cualquier crecimiento sin
límite en `claim_photos/` termina tumbando el volumen completo, sin importar qué tan
acotado esté cada request individual.

**Fix:** `_enforce_claim_photos_budget()` — presupuesto duro de 120MB para
`claim_photos/`. Si se pasa, evicta los archivos más viejos (por mtime, LRU) hasta bajar
al 80% del presupuesto (96MB), y limpia las filas correspondientes en `claim_photos` vía
el nuevo `token_store.delete_claim_photos_by_path()`. Se dispara en background
(`asyncio.create_task`, no bloquea la respuesta al usuario) después de cualquier descarga
de fotos — tanto en `/api/returns/claim-photo-proxy` (galería de un reclamo) como en el
backfill de `/api/returns/sku-claims-detail` (buscador de SKU).

**Validado localmente:** script standalone contra un directorio aislado (sin tocar el
caché real de dev) — 20 archivos sintéticos de 8MB (160MB total, mtimes escalonados) →
tras la eviction quedan 96MB exactos (el 80% objetivo), los 8 archivos más viejos
desaparecen del disco Y de la tabla `claim_photos`, los 12 más nuevos sobreviven intactos.
Verificado también que el endpoint de búsqueda sigue funcionando normal con datos reales
del caché local existente tras el cambio.

**Nota:** este es el fix de contención inmediata. La solución de fondo (sacar las fotos
del volumen de Railway a MinIO/S3 de MI2) quedó pendiente de un correo a
`coolify01@mi2.com.mx` — ver `.claude/memory/reference_mi2_portals.md`.

---

## 2026-07-16 — FIX: seller_custom_field vacío ocultaba SKUs reales que sí tenían seller_sku

**Archivos:** `app/main.py`

**Motivación:** usuario buscó SNTV002237 y faltaba un reclamo que sabía que existía.
Dio un número (`2000014019428725`) que resultó ser un **pack_id** de ML (no un
claim_id) — se investigó en vivo contra la API de ML (`/packs/{id}` → orden
`2000017417336020` → `/marketplace/v2/claims/search?resource_id=...` → claim real
`5544560372`, abierto hoy, reason `PDD9946`).

**Causa raíz:** el order item de ML tenía `seller_custom_field: null` pero
`seller_sku: "SNTV002237"` — el mismo dato en un campo distinto. La mayoría de las
funciones que resuelven SKU desde una orden (`_compute_unified_returns` del widget
Global, `returns_top_products`, `returns_global_top`, `returns_summary_partial`) solo
revisaban `seller_custom_field`, sin fallback a `seller_sku` — a diferencia de
`_save_ml_claims_bg`, que sí lo tenía desde antes. Cualquier listing con
`seller_custom_field` vacío (como el TV Onn 65" del hallazgo anterior, y este TV Onn
70") quedaba con SKU sin resolver en el widget/búsqueda, aunque el dato correcto sí
existía en ML.

**Fix:** agregado `or item.get("seller_sku")` como fallback en los 4 sitios que
resuelven SKU desde `order_items[].item` (incluyendo el fallback vía `/items/{id}`,
que ahora también pide el atributo `seller_sku`).

**Validado localmente:** tras el fix, un cómputo fresco del widget (days=2) resuelve
correctamente el claim `5544560372` bajo SNTV002237.

---

## 2026-07-16 — FEAT: buscador de SKU en "Top Retornos Global" (todas las cuentas, ML+Amazon)

**Archivos:** `app/main.py`, `app/templates/multi_dashboard.html`

**Motivación:** el usuario quería poder buscar un SKU específico (no solo los del
Top 5/10/20 visible) y ver todos sus reclamos/discusiones en todas las cuentas,
respetando el mismo selector de días ya establecido en el widget.

**Backend:** `/api/returns/sku-claims-detail` ahora también devuelve un resumen
agregado (`title`, `accounts`, `reasons`, `amazon: {count, refund_usd, reasons}`)
además de la lista de reclamos — el mismo endpoint sirve tanto al clic en una fila
del ranking como a una búsqueda libre. Nuevo parámetro `days` opcional: si se pasa,
agrega reembolsos Amazon del mismo SKU en esa ventana (reutiliza
`_fetch_amazon_refunds_cached`, mismo cache 3h que el widget).

**Bug encontrado y corregido en el camino:** el `return` temprano cuando no había
reclamos ML (`if not claims: return {...}`) cortaba ANTES de calcular el agregado de
Amazon — un SKU que solo se vende/retorna en Amazon (sin reclamos ML) mostraba
falsamente "sin nada" en vez de sus reembolsos Amazon. Se quitó el corte temprano;
el resto de la función ya maneja listas vacías correctamente.

**Frontend:** input "Buscar SKU específico..." en los controles del widget (Enter o
botón), usa el mismo selector de días ya visible. Reutiliza el modal de detalle ya
existente — se extrajo el render de tarjetas de reclamo a `_renderClaimCards()` para
no duplicar código entre "clic en fila del ranking" y "búsqueda libre".

---

## 2026-07-16 — FEAT: fotos de compradores también se auto-descargan on-demand por SKU

**Archivos:** `app/main.py`

**Motivación:** un comprador decía "adjunto foto de la falla" pero el modal no
mostraba ninguna foto — el backfill on-demand solo resolvía texto, no adjuntos.

**Fix:** el mismo `get_claim_messages()` que ya se pedía para el comentario trae los
`attachments` de cada mensaje — no hace falta una llamada extra. Ahora se descargan y
cachean (mismo patrón ya probado en `/api/returns/claim-photo-proxy`) durante el
backfill de `sku-claims-detail`, con un **tope duro de 30 fotos por request**. Esto es
deliberadamente distinto del incidente de disco lleno de esta misma sesión (bajaba
TODAS las fotos de TODOS los reclamos del negocio de una sola vez): aquí solo se tocan
los reclamos de UN SKU que alguien está viendo activamente, con límite explícito.

**Validado localmente:** el reclamo con "adjunto foto de la falla..." ahora trae 1
foto (249KB JPEG, servida correctamente vía `/api/returns/claim-photo-file`); varios
reclamos más del mismo SKU también resolvieron sus fotos en la misma pasada.

---

## 2026-07-16 — FIX: comentarios de reclamos traían plantillas de ML + HTML crudo sin limpiar

**Archivos:** `app/main.py`

**Motivación:** el usuario vio un "comentario del comprador" que en realidad era una
plantilla de ML dirigida al VENDEDOR ("Hola, BLOW... gracias por tu excelente
disposición...", el cierre administrativo de un caso ganado), con etiquetas HTML
crudas (`<p dir="ltr"><strong class="coco-editor-textBold">...`) sin limpiar.

**Causa:** el filtro de mensajes incluía `sender_role in ("complainant", "mediator")`
— la idea original era que "mediator" es un resumen útil que arma el asistente de ML,
pero en la práctica también incluye plantillas de cierre de caso dirigidas al
vendedor, indistinguibles del resumen útil solo por el rol. Además el texto de ML
viene compuesto en su editor enriquecido (clases `coco-editor-*`) y nunca se limpiaba
el HTML antes de guardarlo.

**Fix:**
- Nuevo helper `_strip_html_msg()` — quita etiquetas, decodifica entidades, normaliza
  saltos de línea.
- Filtro de mensajes ahora es **solo `complainant`** (el comprador mismo) en los dos
  lugares que extraen comentarios (`_save_ml_claims_bg` y el backfill on-demand de
  `sku-claims-detail`), con `_strip_html_msg()` aplicado al texto.
- Nuevo `GET /api/diag/reset-claim-comments?token=...` — limpia los `buyer_comment` ya
  contaminados en producción (el `ON CONFLICT` de `upsert_claims_history` no pisa un
  comentario existente con uno vacío, así que sin este reset la basura ya guardada
  se hubiera quedado ahí para siempre). Corrido una vez en producción tras el deploy.

---

## 2026-07-16 — FIX: claims_history dependía de un botón manual desconectado del widget

**Archivos:** `app/main.py`

**Motivación:** el usuario notó algo que no cuadraba: el widget Global YA sabía que
un TV tenía 33 retornos (los resolvió live vía `_compute_unified_returns`), pero el
modal de detalle decía "sin comentarios sincronizados — corre Actualizar reclamos" —
dos fuentes de datos del mismo negocio, desconectadas entre sí, una de las cuales
requería que alguien recordara ir a otra página y apretar un botón manualmente.

**Fix — una sola fuente de verdad, auto-mantenida:**
- `_compute_unified_returns` ahora persiste a `claims_history` como efecto secundario
  de su propio cálculo (que de todas formas ya resuelve cada reclamo ML vía su orden)
  — cada refresh del cache (cada 3h) deja el registro base (claim_id, cuenta, SKU,
  item_id, motivo, fecha, monto) sin necesidad de ningún botón. `buyer_comment` queda
  vacío en este paso — pedirlo (`get_claim_messages`) es una llamada extra por reclamo,
  no se justifica para reclamos que nadie va a mirar todavía.
- `/api/returns/sku-claims-detail` ahora hace ese backfill de comentario **on-demand,
  acotado a los reclamos que realmente se están mostrando** (máx. 60 por request, con
  Semaphore(5)) — la primera vez que alguien abre el modal de un SKU, los comentarios
  se resuelven en vivo y quedan guardados para la próxima. El botón "Actualizar
  reclamos" en `/returns` sigue existiendo (sigue siendo útil para un refresh manual
  amplio de 180 días), pero ya no es un requisito para que el modal funcione.

**Validado localmente:** con el cache del widget frío, tras un refresh `claims_history`
pasó de 0 a 44 filas para el TV Onn 65" (antes vacío) con `item_id` e `sku` correctos;
al abrir el detalle, el backfill trajo un comentario real de comprador en la primera
consulta, sin tocar ningún botón.

---

## 2026-07-16 — FIX: Análisis IA sin crédito + reclamos sin SKU no se podían consultar

**Archivos:** `app/main.py`, `app/services/token_store.py`

**Motivación:** el usuario probó "Analizar con IA" en el modal nuevo y salió "credit
balance too low" — y notó que un TV con 33 retornos no mostraba comentarios porque
"no tiene SKU resuelto", aunque claramente sabemos cuál es (tiene item_id de ML).

**Fix 1 — IA sin crédito:** `/api/returns/ai-analysis` usaba `app/services/claude_client`
(Anthropic API directa, key fija sin crédito). El proyecto ya tiene
`app/services/openrouter_client` — cascade de modelos GRATUITOS (Gemma, Llama, Mistral
vía OpenRouter) que solo cae a Anthropic directo como último recurso — y ya lo usan 5
otras features (Wizard, health_ai, lanzar, etc.). Mismo interfaz `generate(prompt,
max_tokens=...)`, swap de un import. Confirmado que `OPENROUTER_API_KEY` está seteada
en Railway producción (consultado vía Railway GraphQL API) — el cliente puede resolver
gratis igual que las demás features de IA del dashboard.

**Fix 2 — reclamos sin SKU no identificables:** `claims_history.item_id` se guardaba
SIEMPRE vacío en `_save_ml_claims_bg` (bug — nunca capturaba `item_d.get("id")` de la
orden, aunque ya estaba disponible ahí mismo). Sin `sku` NI `item_id` poblados, un
listing sin `seller_custom_field` (no todos los productos tienen SKU formato BM) quedaba
imposible de identificar en `claims_history`, aunque sus reclamos sí estaban guardados.
- `_save_ml_claims_bg` ahora captura `item_id` del order item.
- `upsert_claims_history` ON CONFLICT ahora también refresca `item_id` (mismo patrón
  del fix de `account_id` — sin esto un re-sync no sana las filas viejas).
- `get_claims_history()` acepta filtro `item_id` opcional.
- `/api/returns/sku-claims-detail` acepta `item_id` como alternativa a `sku`; el modal
  en `multi_dashboard.html` usa `item_id` como fallback cuando `p.sku` viene vacío.

**Pendiente:** correr "Actualizar reclamos" otra vez en producción para que las filas
de `claims_history` ya sincronizadas ganen su `item_id` (mismo caveat que el fix de
`account_id` — un re-sync corrige lo viejo).

---

## 2026-07-16 — FEAT: Modal de detalle por SKU en "Top Retornos Global" (comentarios, fotos, IA)

**Archivos:** `app/main.py`, `app/templates/multi_dashboard.html`

**Motivación:** con el ranking global ya corregido (ver entrada anterior), el usuario
pidió poder hacer clic en un SKU y ver el detalle — comentarios de compradores, fotos,
y una recomendación — sin tener que ir a `/returns` de cada cuenta por separado.

**Nuevo endpoint `GET /api/returns/sku-claims-detail?sku=X`:** lee `claims_history` +
`claim_photos` por SKU **sin filtrar por cuenta** — excepción legítima porque este
endpoint solo lo consume el widget Global (ver CLAUDE.md regla #4). Devuelve, por cada
reclamo: cuenta (resuelve nickname desde `user_id`, con fallback si aún quedan filas
viejas en formato nickname), fecha, motivo, comentario del comprador y fotos ya
cacheadas localmente. Solo ML — Amazon no expone esto vía su API.

**Modal nuevo en `multi_dashboard.html`:** clic en cualquier fila del widget → abre
`ret-detail-modal` con:
- Header: conteo total, SKU, link a ML, pills de cuentas y motivos (datos que el
  widget ya tenía en memoria, sin request adicional).
- Botón "🤖 Analizar con IA" — reutiliza `/api/returns/ai-analysis` tal cual (ya era
  agnóstico de cuenta/plataforma, solo necesita título/razones/conteo). Manual, no
  automático, para no gastar la API en cada clic.
- Sección de comentarios/fotos vía el endpoint nuevo; nota clara cuando el SKU es
  solo-Amazon (sin comentarios/fotos posibles) o cuando no hay SKU resuelto.
- Link "📦 Paquete proveedor" — reutiliza `/api/returns/supplier-package` sin
  `account_id` (junta reclamos de TODAS las cuentas para ese SKU, coherente con que
  el mismo producto puede venderse en varias cuentas).

---

## 2026-07-16 — FIX: Sesgo ML vs Amazon en "Top Retornos Global" (widget /multi-dashboard)

**Archivos:** `app/main.py`, `app/templates/multi_dashboard.html`

**Motivación:** usuario notó que el widget mostraba a Amazon dominando el ranking de
SKUs más retornados aunque ML tenía 8x más reclamos totales (983 vs 125 en 30 días) —
y que varias entradas solo mostraban el SKU sin título.

**Causa raíz del sesgo:** `/api/returns/unified-top` necesita el detalle de CADA orden
ML para resolver su SKU (el reclamo no lo trae directo — 1 request por orden). Estaba
capado a `max(limit*3, 30)` órdenes **por cuenta** para no tardar demasiado. Con 4
cuentas y cientos de reclamos, solo se resolvía ~12% de los reclamos ML; el resto caía
en buckets de 1 (`orden_X`) y nunca se agregaba a ningún SKU. Amazon no tenía este
problema — el refund trae el SKU directo, sin request extra — así que su volumen
(mucho menor) sí se agregaba correctamente y parecía "ganar".

**Causa del título faltante:** ni las entradas ML sin resolver ni las de Amazon hacían
fallback al catálogo local (`bm_product_catalog`) cuando no había título de la orden/
refund.

**Fix:**
- Nueva función `_compute_unified_returns(days)` resuelve TODAS las órdenes ML sin cap
  (antes limitado a ~30/cuenta) + fallback de título vía `bm_product_catalog`.
- Como resolver cientos de órdenes toma 30s+ y Railway mata requests a los ~30s (ver
  incidentes previos en este DEVLOG), el cálculo **nunca corre en línea con el
  request**: `_fetch_unified_returns_cached` (cache 3h, mismo patrón que
  `_fetch_amazon_refunds_cached`) sirve el cache si está fresco; si está viejo o no
  existe, dispara un refresh en background (`asyncio.create_task`, con guard
  `_unified_returns_inflight` para no duplicar) y devuelve el cache stale si hay uno,
  o `{"computing": true}` si es la primera vez. El widget en `multi_dashboard.html`
  muestra "Calculando por primera vez" y reintenta una sola vez a los 15s.
- `_merge_return_counts()` nuevo — el cache guarda `ml_counts`/`amz_counts` por
  separado (nunca mezclados) para que el filtro ML/Amazon/Todas del widget pueda
  servir el subset correcto sin arrastrar el conteo de la otra plataforma; "Todas"
  los fusiona en el momento de servir la respuesta.

**Validado localmente:** con la muestra completa (sin cap), el top real a 45 días es
un TV Onn 65" con 44 retornos (antes el cap dejaba ver máximo ~5 para cualquier SKU ML).

---

## 2026-07-16 — FIX: Reestructuración de /returns — scope de cuenta roto + UI sobrecargada

**Commits:** (pendiente al hacer commit) · **Archivos:** `app/main.py`, `app/templates/returns.html`, `app/templates/multi_dashboard.html`

**Motivación:** usuario reportó que la sección de Retornos "se ve fea" y "no funciona
correctamente". Auditoría encontró un bug real de scope, no solo un problema visual.

**Bug de scope (violación de la regla "NUNCA mezclar cuentas"):**
- `/api/returns/sku-claim-rate` y `/api/returns/supplier-package` (card "Tasa Real de
  Reclamos") nunca recibían `account_id` — agregaban `claims_history` de las 4 cuentas ML
  aunque el resto de la página (KPIs, timeline, top-products, quality-scores) sí estaba
  correctamente scoped. Si veías Retornos de LUTEMAMEXICO, esa card te mezclaba las 4
  cuentas sin avisar.
- **Causa raíz más profunda:** `_save_ml_claims_bg` (línea ~1722) guardaba
  `account_id = nickname` (ej. `"APANTALLATEMX"`) en vez de `user_id` (ej. `"523916436"`),
  el formato que usa el resto del app (`get_meli_client`, `retFilters.account_id`, etc.).
  Aunque se hubiera agregado el filtro `account_id` a los endpoints, nunca habría
  matcheado nada. Corregido en el write path — como `claims_history` está vacío en
  producción (ver incidente de abajo), no requiere migración, el próximo sync ya
  guarda el formato correcto.
- Fix: `account_id` agregado a `sku-claim-rate`, `supplier-package` y
  `_fetch_live_ml_sales_by_sku` (ahora acepta filtrar a una sola cuenta); JS de
  `returns.html` pasa `retFilters.account_id` en ambos fetches.

**Vista Global duplicada y mal implementada:**
- El toggle "🌐 Global (todas)" dentro de `/returns` (`setRetMode`) tenía un bug real:
  buscaba `#ret-timeline-card`, un ID que no existía, así que al activar Global la
  Tendencia/Quality Score y la card de Tasa Real (que además mezclaba cuentas) se
  quedaban visibles encima de la vista Global — dos scopes de datos mezclados en pantalla.
- Además duplicaba funcionalidad que ya existía en `/multi-dashboard` (widget "Top
  Retornos Global", que ya usaba `/api/returns/unified-top`).
- Fix: **se eliminó el toggle Global de `/returns`** — la página queda 100% por-cuenta,
  sin excepciones. El widget "Top Retornos Global" en `/multi-dashboard` se expandió con
  filtro ML/Amazon/Todas, selectors de días/límite, KPIs y las cards enriquecidas
  (razones, cuentas, riesgo MXN) que antes vivían en el toggle roto.

**Reorganización visual:**
- "Top SKUs Retornados" (comparación de 2 períodos + recomendaciones IA, fetch en vivo)
  y "Tasa Real de Reclamos" (tasa real ÷ ventas + paquete proveedor, requiere sync) eran
  dos cards apiladas rankeando SKUs de forma distinta — confuso cuál número creer. Se
  fusionaron visualmente en una sola card ("Análisis de Retornos por SKU") con 2 sub-tabs:
  "📊 Ranking rápido" y "📦 Tasa real + Proveedor". No se tocaron los pipelines de datos
  (fuentes distintas: fetch en vivo vs `claims_history` + ventas agregadas).
- Orden final de la página: Filtros → Alertas → KPIs → Tendencia+Calidad → Análisis SKU
  (card fusionada) → Tabla operativa+sidebar.

**Pendiente:** correr `/api/planning/sync-claims` en producción para poblar
`claims_history` con el formato de `account_id` corregido (el sync anterior nunca se
persistió — ver incidente 2026-07-15 abajo).

---

## 2026-07-15 — FIX URGENTE: Incidente de producción — disco lleno por sync masivo de fotos de reclamos

**Commits:** `30eccb7`, `5ff6c46` · **Archivos:** `app/main.py`

**Incidente:** el sync de reclamos ML (Fase 1, ver entrada de abajo) bajaba TODAS las
fotos de TODOS los reclamos de una vez. En producción esto llenó el Railway Volume
(500MB) — 835 fotos = 1.5GB — y SQLite dejó de poder escribir ("database or disk is
full"), tumbando el login de todo el dashboard (`get_current_user()` no podía refrescar
tokens ML). Ocurrió poco después de desplegar el botón "Actualizar reclamos" en la UI
de `/returns` (commit `712c9a9`), que dispara ese mismo sync.

**Fix 1 (`30eccb7`):** `_save_ml_claims_bg` ya no descarga fotos durante el sync — solo
texto/comentarios. Las fotos se siguen viendo normal: `/api/returns/claim-photos` ya
tenía un fallback que las cachea de forma perezosa (1 a la vez) cuando alguien abre la
galería de un reclamo específico. Se agregaron `GET /api/diag/db-size` (diagnóstico de
tamaño de DB/volumen/tablas) y `GET /api/diag/emergency-clear-claim-photos` (borra fotos
ya descargadas para liberar espacio — usado para recuperar producción).

**Fix 2 (`5ff6c46`):** el arranque mismo hacía un INSERT (`seed_product_type_templates`)
antes de aceptar requests; con disco lleno eso lanzaba `sqlite3.OperationalError` sin
capturar → "Application startup failed" → contenedor completo caído (502 total), sin
poder ni siquiera llegar al endpoint de emergencia para liberar espacio. Cada paso
esencial del arranque ahora está en try/except — bootea en modo degradado si algo falla,
en vez de no bootear nada.

**Verificado post-fix:** `db_file_mb: 309.36`, `claim_photos: 0 archivos` — producción
estable. Pero `claims_history: 0 filas` — el sync nunca llegó a persistir datos en
producción (solo se validó localmente). Ver entrada de arriba (2026-07-16) para el
fix de scope que se aplicó antes de volver a correr el sync en producción.

---

## 2026-07-15 — FEAT: Persistencia de reclamos ML (claims_history + claim_photos) — Fase 1 de mejora de Reclamos/Retornos

**Archivos:** `app/services/token_store.py`, `app/services/meli_client.py`, `app/main.py`

**Motivación:** El feature de Reclamos existente (`/returns`) nunca persistía nada — todo se
leía en vivo de la API de ML en cada carga de página. Sin historial no se puede armar un
paquete para proveedor, ni calcular tasa real de reclamos por SKU (unidades reclamadas ÷
vendidas). Amazon queda fuera de esta fase: SP-API no expone reason codes ni fotos, solo
refund $ vía Finances API (gap ya documentado, requiere `GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE`).

**Qué se agregó:**
- Tablas `claims_history` y `claim_photos` en `token_store.py` (mismo patrón que `order_history`).
- `_save_ml_claims_bg()` en `main.py` — job de background que descarga reclamos de las 4 cuentas
  ML, resuelve SKU vía la orden asociada, extrae comentarios y fotos de los mensajes, y persiste
  todo. Trigger manual: `POST /api/planning/sync-claims?days=N`.
- Fotos se descargan a `/app/data/claim_photos/` (Railway Volume — persiste entre deploys,
  confirmado vía Railway GraphQL API, ver `.claude/agents/devops-engineer.md`).
- `MeliClient.download_binary()` — nuevo helper para descargar adjuntos autenticados.

**Bugs encontrados y corregidos en el camino (afectaban también el feature `/returns` ya
existente, no solo lo nuevo):**
1. El código asumía `msg["from"]["role"]` — el campo real en `/marketplace/v2/claims/{id}/messages`
   es `msg["sender_role"]` (plano, sin `from`). Esto hacía que el filtro de comentarios del
   comprador nunca matcheara nada, ni en el sync nuevo ni en el endpoint viejo de fotos.
2. Los attachments de claims v2 **no traen `url`/`path`** — solo `filename`. La URL real de
   descarga hay que armarla: `/marketplace/v2/claims/{claim_id}/attachments/{filename}/download`
   (requiere Bearer auth, no es pública). El endpoint viejo `/api/returns/claim-photos/{claim_id}`
   nunca mostraba fotos en producción por esto — el `<img src>` apuntaba a una URL vacía.
3. Escrituras SQLite concurrentes desde `_process_claim` (hasta 5 en paralelo, cada una abriendo
   su propia conexión para guardar fotos) causaban "database is locked" silencioso — el claim
   completo se perdía del batch de upsert aunque el texto/fotos ya se hubieran calculado
   correctamente. Fix: acumular todo en memoria y hacer una sola escritura batched al final,
   igual que ya se hacía con `order_history`.

**Fix también aplicado al endpoint viejo:** `/api/returns/claim-photos/{claim_id}` ahora
prioriza el mirror local (`claim_photos`) y sirve vía `/api/returns/claim-photo-file`; si no
hay nada sincronizado aún, cae a `/api/returns/claim-photo-proxy` (descarga en vivo + cachea a
disco de paso). La galería de fotos de `/returns` que estaba silenciosamente rota ahora
funciona.

**Validado localmente:** sync de 30 días × 4 cuentas → 984 reclamos, 660 con SKU resuelto, 608
con comentario/resumen del comprador, 835 fotos descargadas y verificadas (JPEG válido).

**Pendiente (siguientes fases, no implementado aún):** tasa real de reclamos por SKU (cruce
contra `order_history`), rollup de impacto monetario, export/paquete para proveedor, Amazon
Reports API para reason codes reales.

---

## 2026-07-15 — FEAT: Tasa real de reclamos por SKU + paquete descargable para proveedor — Fase 2/4

**Archivos:** `app/main.py`

**Continuación de la Fase 1 (arriba, mismo día).** Dos endpoints nuevos:

- `GET /api/returns/sku-claim-rate?date_from&date_to` — unidades reclamadas ÷ vendidas por
  SKU, en vivo. **No usa `order_history` como denominador** — se descubrió que esa tabla
  está casi vacía para ML (20 filas totales), solo se llena de forma parcial/fire-and-forget
  desde otros flujos. En su lugar hace fetch en vivo de ventas (mismo patrón de paginación
  adaptativa que `generate_purchase_order.py`, con partición de rango de fechas si una cuenta
  supera el límite de ~10,000 resultados de la API de ML). Rango máximo 180 días (fetch en
  vivo, límite práctico antes del timeout de 30s de Railway).
  - "Unidades reclamadas" cuenta **órdenes distintas**, no reclamos crudos — ML a veces abre
    un `claim` y después una `mediation` como IDs separados para la MISMA orden; contar
    reclamos crudos infla el numerador.
  - Nota automática cuando la tasa pasa de 100%: el filtro de fecha es sobre cuándo se
    **abrió el reclamo**, no cuándo se hizo la venta — un reclamo de julio puede ser de una
    venta de marzo. Con ventanas cortas (30d) esto puede dar tasas >100% en SKUs con pocas
    ventas recientes pero reclamos viejos resurgiendo; es información real, solo hay que
    leerla con ese contexto.
- `GET /api/returns/supplier-package?sku=X&date_from&date_to` — ZIP descargable con
  `resumen.xlsx` (detalle de cada reclamo: fecha, motivo, comentario/resumen del comprador,
  monto — más una hoja de resumen ejecutivo) + carpeta `fotos/` con todas las fotos de
  clientes para ese SKU. Usa `openpyxl` (ya en requirements) + `zipfile` (stdlib).

**Validado localmente:** SNAC000046 → ZIP de 6.7MB, 5 reclamos, 5 órdenes distintas, $27,036.88
MXN, 5 fotos reales verificadas.

**Pendiente:** Amazon Reports API para reason codes reales (Amazon nunca va a tener fotos ni
comentarios de cliente — limitación de su API, no nuestra). UI en `/returns` para disparar
estos dos endpoints desde el navegador (hoy solo existen como API, se probaron por curl).

---

## 2026-07-13 — FIX: BM Disp inconsistente entre secciones del Stock tab (Stock Crítico=1, Reabastecer=7)

**Commits:** `631cd7e`, `4bf81fc`, `87c4d02`, `53a9810`, `1a4a39d`
**Archivos:** `app/main.py`

**Síntoma:** SNTV008016 mostraba BM Disp=1 en Stock Crítico pero BM Disp=7 en Reabastecer.
MTY/CDMX/TJ mostraban "—" en todas las secciones.

**Causa raíz (cadena de problemas):**

1. **BM bulk GR** (`GRA,GRB,GRC,NEW`) no incluye SNTV* — las TVs usan condición ICB/ICC.
   El bulk devolvía `avail_total=1` para SNTV008016 porque el ALL bulk (`GRA…ICC…NEW`)
   retorna el valor correcto (7+). Fix: `_fetch_tv_wh_breakdown` usa ALL bulk y actualiza
   `avail_total` con la suma real `mty+cdmx` cuando `lsum > avt`.

2. **Dos handlers corriendo para SNTV*:** el prewarm second pass (`_wh_phase`) Y el TV WH
   breakdown ejecutaban para SNTV*. El second pass pisaba `mty=0, _wh_fetched=False` sobre
   datos correctos. Fix: excluir SNTV* del prewarm second pass y del `_do_bulk_miss_retry`.

3. **DB warm-start siempre arrancaba con mty=0 para SNTV*:** el save al DB ocurría en T+0
   (antes del TV WH breakdown a T+180s). Fix: agregar save al DB al final del TV WH breakdown.

4. **`_stock_issues_cache` estancado con datos del prewarm T+0:** el cache se construía con
   `avail_total=1` y el TV WH breakdown actualizaba el BM cache pero NO el stock_issues_cache.
   Por eso Stock Crítico (lee del cache viejo) mostraba 1, mientras Reabastecer mostraba 7
   (el listing fue eviccionado individualmente y se reconstruyó fresco con datos nuevos).
   Fix: al finalizar TV WH breakdown, limpiar `_stock_issues_cache` y disparar prewarm de
   reconstrucción. El nuevo prewarm lee el BM cache corregido y todas las secciones muestran
   valores consistentes.

**Resultado final:** ~4 minutos después de cada arranque, Stock Crítico y Reabastecer muestran
el mismo BM Disp, y MTY/CDMX/TJ se pueblan correctamente en todas las secciones.

**Regla confirmada:** BM ya descuenta reservas de `AvailableQTY`. NUNCA restar reservas
manualmente — confiar en `avail_total` como valor final vendible.

---

## 2026-07-01 — FEAT: Alerta "SKU no en catálogo BM" + catalog sync diario

**Archivos:** `app/main.py`, `app/templates/partials/products_stock_issues.html`

**Problema previo resuelto:** La alerta anterior usaba `_v=False` en `_bm_stock_cache` como proxy
de "BM no conoce este SKU". Era incorrecto: BM omite productos con stock=0 del bulk response,
por lo que cualquier SNTV* sin stock era falso positivo. Esa implementación fue revertida.

**Solución correcta — fuente de verdad: `bm_product_catalog`:**
- La tabla `bm_product_catalog` (9,188 SKUs en Railway) es el catálogo descargado de BM.
  Si un SKU SN*/SHIL*/RMTC*/etc. NO aparece ahí → BM genuinamente no lo conoce.
- Si aparece en catálogo pero stock=0 → producto real, solo sin inventario → sin alerta.

**Cambio 1 — Sync diario del catálogo BM:**
- `_weekly_catalog_sync()` (~línea 613): cambiado de "domingo 9pm MTY = lunes 02:00 UTC"
  a **09:00 UTC diario (= 3am MTY CDT)**. El catálogo es fuente de verdad → debe estar fresco.

**Cambio 2 — Alert B en prewarm (`_prewarm_caches`):**
- Después de `imbalanced.sort()` (~línea 4726): carga `bm_product_catalog` SKUs via
  `token_store.get_bm_catalog_all()`. Para cada producto con SKU de formato BM (prefijos:
  SN, SHIL, RMTC, SHEL, SHFL, SHHP, SHLB) → verifica si `normalize_to_bm_sku(sku)` existe
  en el set de catálogo. Si NO existe → `no_bm_sku[]`. Deduplicado por SKU normalizado.
- `_sic_data` incluye: `no_bm_sku`, `no_bm_sku_count`, `bm_catalog_size`.

**Cambio 3 — UI (products_stock_issues.html):**
- KPI card violeta en el grid (solo si `no_bm_sku_count > 0`, click scroll a sección).
- Total Alertas incluye `no_bm_sku_count`.
- Sección tabla desktop + cards mobile: título, SKU, precio, estado ML, qty, ventas 30d.
- Paginación: `paginateTable('tbody-no-bm-sku', 'pager-no-bm-sku')`.

**Flujo de trabajo:** el equipo ve el listing → busca en BM → da de alta el SKU → próximo
catalog sync (~24h) confirma el alta y el alert desaparece.

---

## 2026-07-01 — REVERT: Alerta "Sin SKU en BM" basada en `_v=False` — falsos positivos

**Commit:** `cf69c66`

La implementación anterior usaba `_v=False` en `_bm_stock_cache` para detectar SKUs
desconocidos en BM. Generaba 100 falsos positivos: productos SNTV* válidos con stock=0
no aparecen en el bulk response de BM (BM omite ceros del bulk), por lo que tenían `_v=False`
aunque existían perfectamente en BinManager. Se revirtió completamente.

---

## 2026-07-01 — FIX: Reabastecer muestra stock 0 cuando ML tiene qty positiva

**Commit:** `05a80bc`

**Síntoma:** MLM4780362912 aparecía en "Reabastecer (MeLi=0, BM disponible)" con
Stock MeLi = 0, pero la API de ML confirmaba `available_quantity: 7`.

**Causa raíz:** `_invalidate_products_on_sync` solo limpiaba `_products_cache`.
El `_stock_issues_cache` (TTL 15 min) persistía con la qty stale. El qty-sync
actualizaba la DB cada 3 min pero no forzaba recalcular las alertas.

**Fix:** El qty-sync pasa `updates = [(item_id, new_qty)]` al callback. Si algún
item pasó de qty 0 → qty positiva, se evicta quirúrgicamente de las listas
`restock` y `activate` en `_stock_issues_cache` sin borrar el cache completo.

- Archivo: `app/services/ml_listing_sync.py` — `_on_listings_updated(uid, updates)`
- Archivo: `app/main.py` — `_invalidate_products_on_sync(uid, updates=None)` con evicción quirúrgica

**Resultado:** La corrección es visible en el próximo ciclo de qty-sync (≤3 min)
sin spinner y sin prewarm completo.

---

## 2026-06-30 — FEAT: Mejoras de Ventas #1 y #2 — Precio vs Competencia + CVR% Funnel

**Commit:** `ff6d7b6`

**Feature #2 — CVR% Funnel (widget en Dashboard, auto):**
- Widget "📈 Funnel CVR por Listing" en `/dashboard` — carga automática desde `_stock_issues_cache`
- Endpoint `/api/dashboard/cvr-funnel` — cero API calls extra, lee prewarm cache
- 3 tabs: Top CVR (mejores) / Bajo CVR (peores) / Sin tráfico (sin visitas)
- Badge CVR: verde ≥3%, amarillo 1-3%, rojo <1%
- CVR global del catálogo en header del widget

**Feature #1 — Precio vs Competencia (button-triggered, caché 30 min):**
- Widget "🏷️ Precio vs Competencia" en `/dashboard` — requiere click en "Analizar"
- Endpoint `/api/dashboard/price-competition` — llama `GET /items/{id}/price_suggestions` ML para top 20 activos
- Badge: verde (OK ±5%), amarillo (+5-15%), rojo (+15%+ arriba), azul (por debajo = oportunidad subir)
- `MeliClient.get_price_suggestions()` añadido a `meli_client.py`
- Caché 30 min en `_price_comp_cache` por cuenta

---

## 2026-06-30 — FEAT: Mejoras de Ventas #3, #4, #5, #6 — Score Salud, Candidatos FULL, Días Inventario, Preguntas Urgentes

**Commits:** `1a4c168` (feat #5 y #6), `b832a07` (feat #3), `17dcb29` (feat #4)

**Feature #5 — Candidatos FULL (tab en Productos):**
- Nuevo tab "🚀 Candidatos FULL" en `/items` — productos con ventas >2 en 30d, sin logística FULL, stock disponible
- Endpoint `/partials/products-full-candidates` reutiliza `products_inventory_partial(preset="full_candidates")`
- Ordena por mayor velocidad de ventas primero

**Feature #6 — Preguntas Urgentes (banner en Salud):**
- Banner de urgencia al tope de la pestaña Preguntas en `/health`
- Rojo animado si hay preguntas >12h sin respuesta, amarillo si 1-12h
- Recordatorio de penalización ML por preguntas no respondidas

**Feature #3 — Días de Inventario (tabla Velocidad de Ventas):**
- Columna "Días inv." en `/planning` — días restantes al ritmo de ventas actual
- Semáforo: rojo ≤7d, amarillo ≤14d, verde >14d
- Enriquece `/api/planning/velocity` con `bm_avail`+`dias_inventario` desde `_bm_stock_cache` (sin API calls)

**Feature #4 — Score de Salud por listing (tab Score en Salud):**
- Tab "📊 Score" en `/health` — hasta 100 listings ordenados peores primero
- Score 0-100: Ventas 30d (30), Stock BM (25), Stock ML (20), CVR (15), SKU (10)
- Badge rojo <40, amarillo 40-69, verde ≥70
- Lee datos de `_stock_issues_cache` prewarm (cero API calls adicionales)

---

## 2026-06-29 — FIX: Filtrar phantom stock BM en Activar con verificación per-SKU

**Commits:** `504eef9`, `1721215`

**Problema raíz confirmado:**
`SHIL000026` aparecía en Activar con 549 unidades pese a que BM per-SKU confirma 0.
BM bulk (SEARCH="") retorna 549 para SHIL000026 en LOC47+68 — bug server-side de BM.
BM per-SKU (SEARCH="SHIL000026") retorna 0 — este valor es el correcto.

**`_bm_avail_verified_zero(sku)` — nueva función (commit `1721215`):**
- Busca en `_bm_stock_cache` si hay entry reciente (<30 min) con `_v=True` y `avail_total=0`
- `_v=True` significa BM respondió con datos reales (no timeout ni falla)
- Si retorna True: el SKU fue verificado genuinamente en 0 por per-SKU
- Agregado como filtro al construir la lista Activar → SKU con `_bm_avail_verified_zero=True` nunca entra

**Flujo de corrección en producción:**
1. Primer prewarm: SHIL000026 entra a Activar (bulk=549)
2. `_fetch_activate_wh` corre ~3s después → per-SKU confirma 0 → `_v=True, avail=0` en cache → evicta del snapshot
3. Segundo prewarm: `_bm_avail_verified_zero("SHIL000026")=True` → nunca entra a Activar

**Botón Ignorar (commit `504eef9`) — NOTA:**
Implementado pero rechazado por el usuario como "parche". El fix real es `_bm_avail_verified_zero`.
El botón y endpoint `/api/items/{item_id}/suppress-activate` quedan en código pero no son la solución.

---

## 2026-06-29 — FIX: Reconciliación bulk BM — 3 correcciones de arquitectura

**Commit:** `20e15d4`

**Contexto / Problema:**
SHIL000026 (y potencialmente otros SKUs) persistían en la sección Activar con 545 unidades
aunque BM per-SKU confirmara 0 en LOC47+68. El ciclo era infinito: bulk retorna 545 →
snapshot guardado con 545 → verificación per-SKU da 0 → snapshot NO actualizado → siguiente
prewarm repite.

**3 correcciones en `app/main.py`:**

**1. `_fetch_activate_wh` — condición rota corregida:**
La condición `if _ae[0] > 0 and _ae[1].get("_wh_fetched")` siempre fallaba porque
`_wh_phase` guarda `wh_responded=False` (el WH breakdown fue eliminado). Resultado:
`_updated` nunca se modificaba — la función era un no-op.
Nuevo comportamiento:
- `_v=True AND avail>0` → actualiza ítem con avail real
- `_v=True AND avail=0` → evicta ítem de Activar (marcado None, filtrado al guardar)
- `_v=False` (BM falló) → conserva valor del bulk sin evictar

**2. `_do_bulk_miss_retry` — delete → update-to-0:**
Eliminado `_db_deletes` (código muerto — `_bm_entry` nunca es None después de `_wh_phase`).
SKUs confirmados en 0 ahora siempre se actualizan en DB, nunca se borran del sistema.
Los SKUs son permanentes en el sistema; solo cambia su cantidad.
Edge case (cache-miss raro): genera update explícito a 0 en lugar de DELETE.

**3. Reconciliación bulk-a-bulk — `_bm_prev_bulk_sku_set`:**
Nuevo global `set` que guarda los SKUs normalizados del bulk anterior.
En cada ciclo compara vs el bulk actual:
- Detecta SKUs que estaban en el bulk anterior con stock y desaparecieron del actual
- Los agrega a `_bulk_miss_set` para que el retry per-SKU los verifique
- Log `[BM-RECON]` muestra el diff (prev vs actual vs desaparecidos)
Garantiza que si BM deja de reportar un SKU, nuestro sistema lo actualiza a 0 automáticamente.

---

## 2026-06-29 — FIX: Evicción inmediata de alertas Stock tras acción de usuario

**Commit:** `0947296`

**Problema:** Al presionar "Qty 0" en cualquier alerta (Riesgo Sobreventa, Reabastecer,
Activar, Stock Crítico), el item permanecía visible en la lista hasta el próximo prewarm
porque `PUT /api/items/{item_id}/stock` no tocaba `_stock_issues_cache`.

**Solución — `_evict_item_from_alerts(uid, item_id)`:**
- Función nueva, síncrona, llamada justo antes del `return {"ok": True}` en `update_item_stock_api`
- Busca todas las keys de `_stock_issues_cache` con prefix `stock_issues:{uid}:` (independiente del threshold)
- Filtra el item de las 8 secciones: `oversell_risk`, `restock`, `activate`, `critical`, `full_no_stock`, `imbalanced`, `stagnant`, `price_risk`
- Recalcula todos los contadores derivados (risk_count, activate_count, etc.)
- Solo toca el cache del usuario afectado — otros usuarios no se ven afectados
- Log `[ALERTS-EVICT]` en Railway confirma la operación

**Resultado:** Item desaparece de la alerta en el momento en que el botón responde.
Si reaparece, es porque el prewarm encontró que el problema persiste con datos frescos de BM.

---

## 2026-06-28 — FEAT: TV MTY/CDMX background task desacoplado + mejoras BM flow

**Commits:** `b1dd818`

**Contexto:** Análisis exhaustivo del flujo BM reveló que per-location ALL bulks toman ~5s
en condiciones normales, pero el semáforo `_BM_GLOBAL_SEM(1)` y la acumulación de tasks
background (_do_bulk_miss_retry + _fetch_activate_wh) pueden causar queues de 9+ minutos
en producción si se incluyen en el prewarm crítico.

**Solución `_fetch_tv_wh_breakdown()`:**
- Task module-level lanzado con `asyncio.create_task()` — NO bloquea el prewarm
- Corre 180s después del prewarm (cuando semáforo está quieto y background tasks terminaron)
- Fetcha LOC47 ALL + LOC68 ALL con timeout 60s cada uno
- Actualiza solo SNTV* en `_bm_stock_cache` via mutación in-place (→ result_map se actualiza)
- Flag `_bm_tv_loc_running` previene instancias concurrentes
- Solo corre cuando `_need_all=True` (hay TVs en el batch) — sin overhead para cuentas sin TVs

**Mejora de logging:**
- Post-fetch pass ahora logea cuántos bulk-miss SKUs fueron filtrados (stale guard)
- Visible en Railway logs para debugging futuro

**Resultado esperado:** TVs muestran MTY=0/CDMX=0 al cargar el tab Stock, y se actualizan
con valores reales ~3 minutos después (sin afectar la velocidad del prewarm).

---

## 2026-06-28 — FIX DEFINITIVO: SHIL000026 stale + TVs MTY/CDMX reales

**Commits:** `5db7eb8`

**Investigación previa:** BinManager specialist estudió el API de A a Z con queries reales.
Findings críticos:
- LOCATIONID comma-separated (`"47,68"`) sí filtra correctamente en el bulk API
- SHIL000026 **no aparece** en el bulk con LOCATIONID="47,68" (genuinamente 0 en LOC47+68)
- El 545 en el dashboard era dato stale en memoria, NO un bug de BM
- Per-location ALL bulk (LOC47: ~1.1s, LOC68: ~3.8s) — completamente asumible en prewarm
- Los 9 min del incidente anterior fueron por semáforo acumulado, no por el endpoint

**Root cause SHIL000026:** El `post-fetch pass` (rellena result_map para SKUs deduplicados)
no excluía los bulk-miss SKUs. Aunque el bulk confirmaba que SHIL000026 no existe en
LOC47+68, el post-fetch pass agregaba su dato stale (545) al result_map desde `_bm_stock_cache`.

**Fix aplicados:**

1. **Post-fetch pass excluye bulk-miss SKUs** — `if sku in _bulk_miss_set: continue` en ambos
   loops del post-fetch pass. `_bulk_miss_set` se inicializa antes del bloque bulk para que
   esté disponible siempre (no solo cuando `to_fetch > 30`).

2. **TVs MTY/CDMX reales** — se agregan `_bm_bulk_loc47_all_cache` y `_bm_bulk_loc68_all_cache`
   con bulk ALL (GR+ICB+ICC) por ubicación. La segunda pasada usa estos para SNTV* y GR
   para no-TVs. TVs ya no muestran 0 por falta de ICB/ICC en el bulk GR por ubicación.

3. **avail_total corregido para no-TVs** — si la suma per-location (LOC47+LOC68) es menor que
   el combined bulk, se actualiza avail_total al valor real. LOC47+LOC68 son las únicas
   ubicaciones vendibles en ML; el combined puede incluir LOC62/LOC69/etc.

---

## 2026-06-25 — FIX: SHIL000026 persistente en Activar — root cause real encontrado y eliminado

**Commits:** `0818eac` `029fae2` `9b149de` `1d42c3f` `38d2c92` `39278c5`

**Problema:** SHIL000026 (Lampara tocador, MLM3042225518) aparecía en Activar con BM=549
durante múltiples sesiones y después de 6+ commits de "fix". El valor real de BM es 0.

**Root cause real (39278c5):** Dos bugs encadenados:

1. **`_fetch_activate_wh` restauraba items eliminados:** Esta task background (corre 3s después
   del prewarm para agregar datos MTY/CDMX/TJ) usaba `_updated = list(_act)` — copia de TODA
   la lista antigua de activate. Al final sobreescribía el snapshot con TODOS los items, aunque
   un prewarm más reciente o `clear-bm-sku` los hubiera eliminado.
   Para SHIL000026: BM retorna HTTP 500 → cliente BM convierte a None → `_store_wh(avail_ok=False)`
   → "Fix A" preservaba el valor stale (avail=549, _v=True) → `_wh_fetched=False`
   → `_updated[i]` mantenía el item original → SHIL000026 restaurado en cada ciclo.

2. **Fast-fail servía datos expirados indefinidamente:** El loop de fast-fail (BM DOWN,
   consecutive_failures≥2) chequeaba solo `ts > 0` pero NO verificaba TTL. Datos con 24h
   de antigüedad se servían como si fueran frescos.

**Fix (39278c5):**
- `_fetch_activate_wh`: antes de guardar el snapshot, filtrar `_updated` por IDs del snapshot
  ACTUAL. Items borrados por el prewarm o `clear-bm-sku` no se restauran.
- Fast-fail: aplicar TTL de 14 min (BM_CACHE_TTL×2) antes de servir datos expirados.

**Verificación:** 3 checks en 90s post-deploy, todos `activate_entries_removed=0`. Confirmado.

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

