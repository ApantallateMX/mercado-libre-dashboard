# DECISIONS.md
# Registro de POR QUÉ se tomó una decisión de arquitectura/implementación no trivial.
# Distinto de DEVLOG.md (que registra QUÉ se hizo).

## 2026-09-09 — No escribir room_type/size/recommended_uses_for_product/item_depth_width_height
## en amz_product_type_templates.defaults para ELECTRIC_FAN (BIRTMAN BT-42i, ExclusiveBulbs)

**Contexto**: la plantilla ELECTRIC_FAN (ATVPDKIKX0DER) tenía `"valor_requerido"` como
placeholder basura en 4 atributos, arrastrado de intentos fallidos antes del fix de
`_fetch_schema_enums()` de hoy mismo. Se pidió corregir esos valores en la plantilla.

**Alternativas consideradas**: escribir directamente los valores de negocio propuestos
(`room_type`, `size`, `recommended_uses_for_product`) y los reales de producto
(`item_depth_width_height`) en `defaults`, igual que se hizo con `electric_fan_design`.

**Por qué NO se hizo así — 2 bugs reales confirmados contra el schema real de Amazon**:

1. **`create_listing()` (app/api/amazon_lanzar.py ~línea 2164-2166) y
   `auto_fix_errors()` (~línea 2532-2538)** — el mecanismo que aplica `defaults` de la
   plantilla SIEMPRE los envuelve como `{"value": val, "marketplace_id": ...}`, sin
   `language_tag`. El JSON Schema real de ELECTRIC_FAN (confirmado vía
   `fetch_product_type_schema` + descarga directa del link S3) exige
   `"required": ["language_tag", "value"]` para `room_type`, `size` y
   `recommended_uses_for_product` (no así para `electric_fan_design`, que solo requiere
   `["value"]` — por eso ESE sí se pudo corregir de forma segura). Escribir un valor
   "correcto" en estos 3 atributos vía `defaults` habría fallado la validación de Amazon
   de todos modos, solo que con un error distinto (falta `language_tag`) en vez del
   error de valor inválido — un ciclo más de Publish fallido sin necesidad.

2. **`item_depth_width_height` vs `item_length_width_height`** — el schema real de
   ELECTRIC_FAN solo reconoce `item_depth_width_height` (confirmado: `item_length_width_height`
   NO existe en absoluto en su lista de atributos). Pero el código de dimensiones en
   `create_listing()` (línea ~1731) tiene: `_dim_attr_name = "item_length_width_height"
   if product_type not in ("TELEVISION","COMPUTER_MONITOR") else "item_depth_width_height"`
   — para CUALQUIER product_type que no sea TV/monitor (incluido ELECTRIC_FAN) manda el
   atributo equivocado, que Amazon simplemente ignora, y el atributo real requerido
   nunca se llena vía el formulario de dimensiones del wizard. Además la forma de
   `item_depth_width_height` en el schema NO anida bajo `"value"` (depth/width/height
   son propiedades directas del item), así que tampoco es expresable vía el mecanismo
   genérico de `defaults` sin un caso especial.

3. **Bug adicional encontrado de paso** (no corregido, solo documentado):
   `auto_fix_errors()` guarda cualquier atributo "corregido" en `defaults` sin checar
   si ESE atributo específico quedó en `remaining_errs` — solo revisa que el patch tenga
   forma de lista. Así fue como `"valor_requerido"` terminó persistido en la plantilla
   en primer lugar: se guardó aunque Amazon lo hubiera rechazado.

**Decisión**: la plantilla se actualizó SOLO con lo que el mecanismo actual puede
expresar correctamente (`electric_fan_design: floor_fan`, `indoor_outdoor_usage: indoor`
—ambos con `required: ["value"]` sin `language_tag` en el schema real, forma segura—,
más limpieza de atributos basura `design`/`fan_design`/`item_depth_unit`/
`item_depth_unit_of_measure` que no existen en el schema real). Los valores
objetivos/de negocio para los otros 3 atributos (`room_type`, `size`,
`recommended_uses_for_product`, todos con `language_tag` requerido) y
`item_depth_width_height` (atributo complejo, tampoco expresable vía `defaults`) se
entregaron a Jovan en el resumen de la sesión, junto con la corrección de código
necesaria (aprobación pendiente) antes de que tenga sentido reintentar el Publish.

**Impacto si no se corrige el código**: reintentar Publish hoy fallaría de nuevo por
`room_type`/`size`/`recommended_uses_for_product` faltantes (siguen sin valor en la
plantilla) y por `item_depth_width_height` ausente — pero al menos ya no por
`electric_fan_design` inválido ni por los atributos basura.

## 2026-09-10 — ganancia_neta/margen_pct en order_history: poner en 0 explícito en vez de
## borrar la columna o intentar "arreglar" el cálculo con otra fórmula basada en costo

**Contexto**: bug real (ver DEVLOG) -- `_save_ml_orders_history_bg()` y
`_save_amazon_items_history_bg()` seguían calculando `ganancia_neta = neto_plat*(1-comisión)
- costo_mxn`, con `costo_mxn` viniendo de `AvgCostQTY` de BinManager, confirmado NO confiable
por Jovan desde 2026-08-13. Varios reportes (CSV, comparativa ML vs Amazon, diag de ventas,
Oportunidades Mayoreo) sumaban/promediaban ese campo como si fuera dinero real.

**Alternativas consideradas**:
1. Borrar las columnas `ganancia_neta`/`margen_pct`/`costo_mxn`/`costo_usd` de `order_history`.
2. Dejar `ganancia_neta` con una fórmula "aproximada" que combine `neto_plat` y
   `recup_retail_pct` (ej. `neto_plat * recup_retail_pct/100`) para no romper consumidores
   que ya leen ese nombre de columna.
3. (Elegida) Poner `ganancia_neta=0.0`/`margen_pct=0.0` explícitos al escribir, mantener
   `costo_mxn`/`costo_usd` como snapshot informativo (no usado para "ganancia"), y migrar
   todos los consumidores a `neto_plat` (dinero real, antes de costo) + `recup_retail_pct`
   (% de salud de precio) con una categoría 🟢/🟡/🔴 (`_recup_category()`).

**Por qué NO la 1 (borrar columnas)**: es una migración de schema real (`ALTER TABLE DROP
COLUMN` no es trivial en SQLite -- requiere recrear la tabla), y varias filas ya escritas
en producción tienen `data_source='real'` con `sale_fee`/`neto_plat` reales que sí valen la
pena conservar aunque `ganancia_neta` no. No se justificaba el riesgo para este fix.

**Por qué NO la 2 (fórmula aproximada)**: `neto_plat * recup_retail_pct/100` es matemáticamente
circular -- `recup_retail_pct` YA es `neto_plat/retail_mxn*100`, así que esa "ganancia
aproximada" sería `neto_plat² / retail_mxn`, un número sin significado de negocio real, solo
para poder seguir llenando una columna. Habría sido peor que el bug original: un número
inventado con apariencia de precisión, en vez de un 0 honesto que fuerza a mirar el indicador
correcto (`recup_retail_pct`).

**Por qué SÍ la 3**: coincide con el patrón que YA existía en el código
(`_save_amazon_orders_bg`, el sync masivo de Amazon, ya escribía `ganancia_neta=0.0` desde
antes de este fix) -- "preferir la solución simple ya usada en el bulk" en vez de inventar una
nueva. Un 0 explícito es auto-descriptivo para cualquiera que lea `order_history` directo
(diag endpoints, `sqlite3` a mano): "esto no se calculó, no calcula un negativo falso ni un
positivo inflado". No requiere migración de schema, no rompe ningún consumidor (todos se
actualizaron el mismo día para leer `neto_plat`/`recup_retail_pct`/`recup_category` en su
lugar).

**Decisión secundaria -- meta de recuperación para agregados multi-SKU** (Oportunidades
Mayoreo, un comprador puede tener SKUs TV y no-TV mezclados): en vez de elegir una sola meta
fija (60% u 80%), tanto `recup_retail_pct` como su meta efectiva se ponderan por el ingreso
de cada línea de orden (`sum(revenue_i * recup_i) / sum(revenue_i)` y lo mismo para la meta
60/80 según prefijo de SKU). Matemáticamente consistente con cómo ya se pondera todo lo demás
en ese reporte (qty, revenue), evita el sesgo de una meta fija que penalizaría injustamente a
compradores mayoritariamente-TV o mayoritariamente-otros.

**Impacto/pendiente**: filas ya escritas en `order_history` ANTES de este fix conservan el
valor viejo contaminado hasta que el sync normal las vuelva a tocar (el UPSERT sobreescribe
`ganancia_neta`/`margen_pct` sin condición) o hasta un backfill explícito -- no ejecutado,
pendiente de aprobación de Jovan (ver DEVLOG).
