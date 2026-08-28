# Developer Manual — mercado-libre-dashboard

Diccionario de datos autoritativo del dashboard de Apantallate MX. Documenta el
schema REAL de `tokens.db` (SQLite, WAL) tal como existe hoy en `init_db()`
(`app/services/token_store.py`) y `app/services/user_store.py`, extraído
directamente del código -- no es un diseño aspiracional.

Este documento cubre a propósito solo las tablas de mayor relevancia operativa
(órdenes/ventas, catálogo/stock, reclamos/reputación, mensajes, sesión, y las
tablas de documentación/changelog). El resto del schema (~57 tablas más,
sobre todo cachés y colas de sync internas) está listado al final en
"Pendiente de documentar" -- sin inventar contenido para ellas.

Convenciones que aplican a TODO el schema (ver también CLAUDE.md):
- Migraciones nuevas son `ALTER TABLE ... ADD COLUMN` envueltas en
  `try/except: pass` -- SQLite no soporta `ADD COLUMN IF NOT EXISTS`, así que
  el patrón real del proyecto es "intentar y tragar el error si ya existe".
  No es un anti-patrón a corregir, es la única forma idiomática en SQLite.
- `platform` casi siempre distingue `'ml'` vs `'amazon'` (a veces con
  variantes de nombre de columna: `seller_id` en tablas Amazon-only vs
  `account_id`/`user_id` en tablas ML o compartidas).
- Timestamps: `REAL` epoch (`time.time()`) en tablas operativas nuevas,
  `TEXT` (`datetime('now')` o ISO) en tablas más viejas o de cara al usuario
  (fechas de facturación, etc.) -- ambos formatos conviven, no hay
  normalización retroactiva.

---

## 1. Órdenes, Ventas y Finanzas

### `order_history`
Historial de órdenes ML + Amazon con snapshot de costo/retail/margen calculado
AL MOMENTO de la venta (no se recalcula después, aunque el costo BM cambie).
- Clave real: `UNIQUE(order_id, item_id, platform)` -- así se evita doble
  conteo en resyncs (`upsert`, no `insert` puro).
- Columnas de dinero: `unit_price`, `sale_fee`, `neto_plat` (neto de la
  plataforma), `costo_usd`/`costo_mxn`, `ganancia_neta`, `margen_pct`,
  `recup_retail_pct` (qué % del retail sugerido se recuperó), `fx_rate`
  (tipo de cambio usado ESE día, no el actual).
- `shipping_cost_mxn` (migración 2026-08-13): costo de envío real por orden,
  reemplaza un estimado fijo que usaba `_calc_margins()` antes.
- `buyer_id`/`buyer_nickname` (migración 2026-08-27): identidad del comprador
  para la feature "Oportunidades Mayoreo". Amazon nunca llena `buyer_id`
  (sin RDT no hay id estable de comprador) -- agregación cae a
  `buyer_nickname`.
- `ship_state_code`/`ship_zone` (ML only, sin backfill retroactivo): zona
  geográfica del comprador para cruzar demanda vs almacén físico (feature de
  Transferencias Sugeridas). Amazon requiere un Restricted Data Token (PII)
  que Jovan debe aprobar en Seller Central -- columna queda vacía hasta
  entonces.
- Índices: `idx_oh_sku`, `idx_oh_account (account_id, platform)`,
  `idx_oh_month`, `idx_oh_buyer`.

### `stagnation_cascade`
Memoria de la cascada de descuentos escalonados para SKUs estancados (SKUs
Estancados feature, 2026-08-26). Un row por `(sku, account_id, platform)` --
`PRIMARY KEY` compuesta, no autoincrement.
- `cycle`: 0 = sin cascada, 1-4 = escalones activos, 5 = agotada (requiere
  decisión de negocio humana, `color='purple'`).
- Se escribe **solo** cuando un humano aprueba aplicar un escalón -- nunca
  automático, regla dura del proyecto (ver Hallazgo #4 de reglas de
  colaboración: nunca auto-aplicar cambios de precio en producción sin
  confirmación).

### `supplier_debt_ledger` / `supplier_debt_payments` / `supplier_debt_settings`
Deuda semanal con la empresa proveedora -- % fijo del retail por unidad
vendida (`category_rate`, distinto para TVs `is_tv=1` vs el resto).
- `supplier_debt_ledger`: un row por `(order_id, item_id, platform)`, generado
  desde `upsert_order_history` -- el mismo `UNIQUE` + `INSERT OR IGNORE` que
  usa `order_history` evita doble conteo en resyncs.
- `amount_mxn = retail_ph_usd * fx_rate * category_rate * quantity`.
- Reversa real de deuda cuando hay reembolso (`reverse_debt_for_refunded_claims()`,
  ver `claims_history.resolution_reason` abajo) -- no solo cuando la orden
  cambia de estatus, también cuando el reembolso llega después del envío.
- `supplier_debt_payments`/`supplier_debt_settings`: pagos aplicados contra el
  ledger y configuración de la tasa/ciclo (no releídas línea por línea en
  este documento -- ver `/deuda-empresa`, admin-only).

### `billing_requests` / `billing_fiscal_data` / `billing_invoices`
Portal de facturación para clientes finales (rutas públicas `/factura/{token}`,
sin autenticación -- el `token` es el control de acceso).
- `billing_requests`: una solicitud de factura por pedido, `token UNIQUE`,
  `status` (`pending_data` → llenado por cliente → facturado), `order_data`
  como JSON crudo (snapshot de la orden al momento de crear la solicitud).
- `billing_fiscal_data`: datos fiscales del cliente (RFC, razón social, uso de
  CFDI, etc.), `request_id UNIQUE` (1:1 con la solicitud).
  - `constancia_data` (BLOB) es **legacy** -- migración 2026-08-28 mueve la
    Constancia de Situación Fiscal (PDF hasta 5MB) a S3/MinIO (`s3_key`),
    mismo patrón que `amazon_buyer_message_attachments`, por el incidente de
    disco lleno de Railway. `constancia_data` queda nullable solo por
    compatibilidad con filas ya insertadas -- no se borra retroactivamente.
- `billing_invoices`: PDF/XML de la factura emitida. Mismo patrón de
  migración a S3 en dos pasos: primero a disco (`pdf_path`/`xml_path`, tras
  el incidente de disco de 2026-07-18), luego a `storage='local'|'s3'`.
  `file_data`/`xml_data` (BLOB) legacy, no se reescriben para filas nuevas.

**Regla de negocio real**: cualquier tabla nueva que guarde binarios de
usuarios (fotos, PDFs) DEBE ir a S3/MinIO desde el día uno, no a BLOB SQLite
ni a disco de Railway -- dos incidentes de disco lleno reales (2026-07-18,
2026-07-31) y un tercero de BLOB (2026-08-27, `amazon_buyer_message_attachments`)
motivaron este patrón. Ver `app/services/s3_storage.py`.

---

## 2. Catálogo y Stock

### `bm_sku_master`
Maestro único de BinManager -- fuente de verdad para alertas, sugerencias y
lanzamientos. Fusiona lo que antes eran `bm_product_catalog` (metadata:
título, marca, retail, costo) + `bm_stock_snapshot` (stock), ambas
DROP-eadas 2026-08-13 (respaldo en `backups/bm_frozen_tables/`).
- `sku` es `PRIMARY KEY` (no autoincrement -- un row por SKU real de BM).
- Dos timestamps de frescura independientes: `catalog_updated_at` (título/
  retail/costo, se refresca ~1x/semana) y `stock_updated_at` (stock, cada
  ~10 min) -- reflejan la cadencia real de cada fuente en BM, no un capricho
  de diseño.
- `available_qty` = suma de TODAS las condiciones vendibles (GRA+GRB+GRC+NEW,
  o ICB/ICC solo si `category='Televisions'`) en las ubicaciones vendibles
  (`47,62,68` -- ver CLAUDE.md, Tijuana excluida desde 2026-08-05).
  **`available_qty` NO es lo mismo que "vendible real"** -- ver
  `.claude/memory/project_gap_scan_cadence_and_bm_vendible_semantics.md`.
- `mty_qty`/`cdmx_qty`/`tj_qty`: desglose por almacén (para Transferencias
  Sugeridas Entre Almacenes) -- Tijuana se sigue consultando aquí aunque esté
  excluida del total vendible.
- `verified`: 1 si el ciclo de sync ACTUAL confirmó el dato contra BM; 0 si
  viene de un ciclo anterior (dato stale). Un fetch fallido nunca debe pisar
  un dato bueno con ceros -- por eso este flag existe, no un simple
  "última vez visto".
- `best_condition_sku`/`best_condition_qty`: el SKU exacto con sufijo de
  condición (ej. `SNTV007447-GRB`) con más stock -- `available_qty` solo
  dice CUÁNTO hay, no en qué condición específica.
- `conditions_json`: lista completa `[{condition, qty, sku}]` por SKU,
  ordenada por qty desc -- usada por alertas en tiempo real y el modal
  "Sustituir".
- `pnp_mty_available`/`pnp_mty_novendible`/`pnp_other_locations_qty`:
  "Plug and Play", solo se procesa para `category='Televisions'` en MTY.
  `pnp_other_locations_qty` > 0 es una anomalía real (PNP no debería
  aparecer fuera de MTY).

### `bm_sku_changes`
Historial de cambios detectados en cada sync de BM (mismo dato que BM ya
manda, sin llamadas extra). Precio/costo: cualquier cambio se loguea. Stock:
solo transiciones que importan (se quedó en 0 / se resurtió) -- evita
llenar la tabla de micro-ruido de +1/-1 unidad.

### `bm_sku_gaps` (ML) / `amz_sku_gaps` (Amazon)
SKUs con stock real en BM pero SIN publicación en la plataforma ("Sin
publicar" / gaps). Un row por `(user_id, sku)` en ML / `(seller_id, sku)` en
Amazon -- **por cuenta**, nunca global (regla de scope: SKU en Autobot no es
gap para Lutema).
- `status`: `unlaunched` hasta que se lanza (`launched_at`/`launched_price`
  en la versión Amazon).
- `priority_score` (ML) ordena qué lanzar primero.
- `stock_mty`/`stock_cdmx` (ML) reflejan el mismo desglose por almacén que
  `bm_sku_master`.

### `ml_listings` / `amazon_listings`
Caché local del catálogo sincronizado de cada plataforma -- fuente para
prewarm rápido sin llamar a la API en cada carga de página.
- `ml_listings.item_id` es `PRIMARY KEY`; `amazon_listings` usa
  `PRIMARY KEY (seller_id, sku)` (Amazon no tiene un id de listing único
  cross-cuenta comparable a `item_id` de ML).
- `base_sku` (ambas, migración): SKU normalizado (sin sufijo de bundle/
  condición) usado por el gap scan para cruzar contra BM sin llamadas API
  extra.
- `data_json` (ML): body completo del item, para prewarm rápido desde DB en
  vez de esperar la API en cada arranque.
- `catalog_listing`/`is_full` (ML): flags reales de ML -- publicación por
  catálogo (afecta cómo se edita el título, ver
  `reference_ml_catalog_listing_vs_family_name.md`) y si usa FULL (afecta
  atribución de Experiencia de Compra).

### `listing_snapshots` / `listing_change_log`
`listing_snapshots`: foto más reciente de cada listing para detectar quién
"gana" la publicación (ML: catalog winner; Amazon: Buy Box) --
`PRIMARY KEY (platform, account_id, item_id)`, se sobrescribe cada ciclo.
`not_winning_since` marca desde cuándo se perdió la posición ganadora (para
alertas de "llevas X días sin ganar").
`listing_change_log`: histórico append-only de cambios detectados
(`old_value`/`new_value` como texto crudo) -- a diferencia del snapshot, este
nunca se sobrescribe.

### `coverage_price_alerts`
Sugerencia de precio calculada por cobertura de stock (días de supply).
`reason`: `'escasez'` (subir precio, se agota) | `'sobrestock'` (bajar
precio). Se recalcula completo cada ciclo de prewarm. **Nunca auto-aplica**
-- el usuario confirma vía `/sync-price`, que sí hace el PUT real a la
plataforma con auditoría (mismo mecanismo que `ml_price_alerts`).

### `seller_flex_stock`
Stock de Amazon Seller Flex/Onsite (nodos VECKTOR), `PRIMARY KEY (node, sku)`.
`sellable_qty` vs `bound_qty` (comprometido/reservado). `bin` (migración
2026-08-22): ubicación física real dentro del nodo, viene de GraphQL
`GetInventoryViewByBin` -- el reporte oficial de Amazon no trae bin.

---

## 3. Reclamos y Reputación

### `claims_history`
Reclamos/devoluciones de ML persistidos por SKU/cuenta -- **solo ML por
ahora**: Amazon no expone reason codes ni fotos vía SP-API (solo el monto de
reembolso vía Finances API). `sku` se resuelve desde la orden asociada
(`resource_id`) al momento del sync, no viene directo del reclamo.
- `UNIQUE(claim_id, platform)`.
- `resolution_reason`/`refunded_buyer` (migración 2026-08-13): necesarias
  para reversar `supplier_debt_ledger` cuando el reembolso llega DESPUÉS del
  envío -- en ese caso el estatus de la orden no cambia, la única señal real
  es `resolution.reason == "payment_refunded"` del reclamo mismo.

### `claim_photos`
Fotos de reclamos, mirror local en `/app/data/claim_photos/` (Railway Volume
persistente) porque las URLs originales de ML pueden expirar. `UNIQUE(claim_id,
local_path)`. Migración a MinIO/S3 tras la crisis de disco 2026-07-31 --
`local_path` sigue siendo el nombre de columna incluso cuando en realidad
guarda la S3 key (evita duplicar columna, distinguido por `storage`).

### `item_experience_snapshots`
Foto diaria de Experiencia de Compra (`reputation.value`/`color` real de ML)
y Calidad (score de `get_item_health`) **por listing individual**, no por
cuenta. `UNIQUE(item_id, captured_date)`.
- Objetivo real: detectar que un listado se deteriora (verde→amarillo→rojo)
  ANTES de que bloquee un deal -- el caso que motivó esto fue
  SNEE000054/MLM5479436194, descubierto en rojo (30/100) hasta que
  PRICE_DISCOUNT/SELLER_CAMPAIGN ya no tenían candidato.
- Acotado a listings activos con ≥1 venta histórica real (`get_active_items_
  with_sales_history`) -- ML no tiene endpoint bulk para esto, es 1-2
  llamadas por item; correrlo sobre TODO el catálogo arriesgaría rate limit
  sin beneficio (SKUs sin venta no son candidatos de promoción de todos
  modos).

### `reputation_snapshots`
Igual que `item_experience_snapshots` pero a nivel CUENTA completa
(`level_id`, `claims_rate`, `cancel_rate`, `delay_rate` -- las métricas
reales que ML usa para el "termómetro" de reputación). `UNIQUE(account_id,
captured_date)`.

---

## 4. Mensajes de Compradores

### `amazon_buyer_messages` / `amazon_buyer_message_attachments` / `amazon_buyer_inbox_state`
Mensajería de compradores Amazon vía IMAP (Amazon no tiene una API de
mensajería equivalente a ML) -- ver `reference_amazon_sp_api_docs.md`.
- `amazon_buyer_messages`: `UNIQUE INDEX` en `message_id` (idempotencia real
  contra reprocesar el mismo correo). `direction` (`inbound`/`outbound`),
  `attachments_checked` (migración 2026-08-27) marca que ya se re-consultó
  ese correo por IMAP buscando adjuntos, para no repetir el backfill si se
  corre más de una vez.
- `amazon_buyer_message_attachments`: imágenes que el comprador adjunta.
  **Nunca se escribe a disco Railway** (2 incidentes reales de disco lleno)
  -- se sirve on-demand. `data` (BLOB) es legacy; desde el fix del mismo día
  (2026-08-27) todo adjunto nuevo va a `s3_key`. Retención: adjuntos con más
  de 6 meses se purgan (solo el binario, no la fila del mensaje).
- `amazon_buyer_inbox_state`: watermark de UID de IMAP por `seller_id`
  (2026-08-04) -- antes cada poll de 5 min re-descargaba los últimos 200
  correos completos (60-80s/cuenta, secuencial); con el watermark solo trae
  UIDs nuevos, casi instantáneo. UID de IMAP (estable entre sesiones), no
  sequence number.

### `ml_message_views` / `ml_messages_index` / `ml_message_sent_log`
Equivalente ML: `ml_message_views` marca qué packs de mensajes ya se vieron
(`PRIMARY KEY (pack_id, account_id)`, `status` incluye el estado de
"Seguimiento" añadido 2026-08-12 -- un hilo respondido pero que sigue
esperando algo del comprador).

---

## 5. Sesión y Usuarios

### `dashboard_users` (en `app/services/user_store.py`, NO en `token_store.py`
-- única tabla de sesión que vive en un módulo separado)
Usuarios del dashboard interno (equipo de Jovan), NO usuarios de ML/Amazon.
- `username UNIQUE`, `password_hash`/`password_salt` (nunca texto plano),
  `role` (`'admin'` es el único rol con privilegios elevados reales -- ver
  todos los checks `du.get("role") != "admin"` repartidos en `main.py`).
  `must_change_pw`: fuerza cambio de contraseña en el próximo login.
- `allowed_sections` (migración): JSON de qué tabs puede ver un usuario
  no-admin -- `NULL` = sin restricción explícita (ver `has_tab_access()` en
  `user_store.py` y el filtro de `_build_nav_tabs()` en `main.py`).
  **Cuidado real**: `PERMISSION_TREE` (frontend) y los checks admin-only de
  cada endpoint pueden divergir -- ver
  `.claude/memory/project_permission_tree_vs_page_admin_check_inconsistency.md`,
  un caso real donde un tab estaba oculto en el nav pero el endpoint seguía
  respondiendo sin bloquear.

---

## 6. Sistema (documentación y novedades)

### `changelog_entries` / `changelog_dismissals`
Ver `/changelog` (MI2 §17b, implementado 2026-08-27/28). Subconjunto
curado y user-facing de DEVLOG.md -- NO lo reemplaza. `category`:
`'feature'|'improvement'|'bugfix'|'security'`. `changelog_dismissals` marca
qué entradas ya descartó cada usuario (`UNIQUE(username, changelog_id)`,
idempotente).

### `doc_categories` / `doc_pages`
Contenido del Manual de Usuario (`/manual`, MI2 §17a) -- ver sección
siguiente de este mismo documento para el detalle completo de columnas.
Nombradas `doc_*` (no `manual_*`) para que el nombre real de la tabla
coincida con la convención que el propio validador de MI2 busca en archivos
`.sql` (`doc_categories`/`doc_pages`) -- ver `docs/schema.sql`, que expone
este mismo `CREATE TABLE` en formato `.sql` como referencia de schema
(doble propósito: documentación real + conformance, no es un archivo falso).

---

## Pendiente de documentar

Las siguientes tablas existen en el schema real (`init_db()` en
`token_store.py`) pero no se documentaron en esta primera versión del
Developer Manual -- en su mayoría son cachés internos, colas de
sincronización, o configuración de baja frecuencia de consulta. No se
inventó contenido para ellas; se listan aquí para que quien las necesite
sepa que faltan y vaya directo al código:

`tokens`, `oauth_states`, `account_settings`, `amazon_settings`,
`amazon_accounts`, `stock_concentration_log`, `sync_alerts`, `sync_status`,
`ml_price_alerts`, `stock_issue_streaks`, `ml_listing_quality`,
`ml_competition_alerts`, `item_sku_cache`, `product_videos`,
`amazon_vel_cache`, `sku_platform_rules`, `multi_stock_sync_log`,
`bm_stock_cache`, `bm_sync_log`, `account_health_state`,
`bm_bulk_fetch_log`, `stock_issues_cache`, `return_flags`,
`bm_gap_scan_status`, `bm_reactivations`, `item_sync_log`,
`account_stock_rules`, `stock_distribution_settings`, `seasonal_events`,
`reply_templates`, `sku_bundles`, `sku_bundle_components`,
`account_deal_config`, `item_history`, `suggestions`, `amz_gap_scan_status`,
`amz_catalog_cache`, `amz_product_specs_cache`, `amz_listing_status_cache`,
`amz_product_type_schemas`, `amz_product_type_templates`, `sku_upc_map`,
`amz_flx_stock_cache`, `amz_flx_sync_meta`, `amz_launched_listings`,
`amz_listing_actions`, `amz_product_types_cache`, `amz_repricing_rules`,
`amazon_seller_feedback`, `ml_item_reviews`, `activate_suppressed`.

---

## Rutas de referencia (dónde vive cada cosa)

- Schema completo real: `app/services/token_store.py` → función `init_db()`
  (líneas ~86-2043) para las tablas compartidas/ML/BM/facturación/reclamos;
  `app/services/user_store.py` → `init_db()` (línea ~318) para
  `dashboard_users`.
- Reglas de negocio transversales (BM semáforo global, nunca pausar
  listings, scope por cuenta, etc.): `CLAUDE.md` (raíz del repo).
- Historial de decisiones y bugs reales ya resueltos: `.claude/memory/*.md`
  (un archivo por tema, indexado en `MEMORY.md`).
- Log técnico completo cronológico: `DEVLOG.md`.
