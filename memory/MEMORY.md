# Mercado Libre Dashboard - Key Learnings

## STARTUP (ejecutar al inicio de cada sesion)
1. `cd /c/Users/Marketing/Desktop/mercado-libre-dashboard`
2. `taskkill //F //IM python3.13.exe 2>/dev/null`
3. `nohup python3.13 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload > uvicorn.log 2>&1 &`
4. `ngrok http 8000 --log=stdout > ngrok.log 2>&1 &`
5. Verificar: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/`
- Si token expirado (401 en APIs): usuario debe re-autenticar via /login

## Changelog
- Ver `memory/CHANGELOG.md` para historial detallado de cambios dia/hora
- SIEMPRE actualizar changelog al hacer cambios significativos

## Deploy (OBLIGATORIO)
- SIEMPRE hacer commit + push a `main` despues de cualquier cambio
- Railway auto-deploya en cada push a `main`
- NO esperar a que el usuario lo pida — es tarea fija automatica

## Git Config
- user: ApantallateML / Jovan.Rodriguez@mitechnologiesinc.com
- Primer commit: 98b17ba (2026-02-12)

## Project Stack
- FastAPI + Jinja2 + HTMX + Tailwind CSS
- Uvicorn with --reload on port 8000
- ngrok for external access

## Jinja2 Gotcha: dict key "items"
- Jinja2's `order.items` resolves to `dict.items()` method, NOT the "items" key
- Solution: Use `SimpleNamespace` objects instead of dicts when passing to templates, OR rename the key to avoid conflicts with dict methods
- Same issue applies to `keys`, `values`, `get`, `update`, etc.

## MeLi API Patterns
- `get_ads_items` returns 503 intermittently; `get_ads_campaigns` is reliable
- Use "campaigns first" pattern: fetch campaigns before items to avoid rate limiting
- Order net revenue = total_amount - sale_fee - IVA_fee - shipping_cost - IVA_shipping
- `order_net_revenue()` helper in `app/__init__.py` uses `_shipping_cost`/`_iva_shipping` keys when enriched
- Shipping cost from `/shipments/{id}/costs` -> `senders[0].cost`
- `enrich_orders_with_shipping(orders)` on MeliClient adds `_shipping_cost` and `_iva_shipping` to each order
- Must call `enrich_orders_with_shipping` before `order_net_revenue` for full net calc

## Windows/Git Bash Issues
- `taskkill /PID` fails in Git Bash (interprets as path) - use `taskkill //PID //F`
- Python exe is `python3.13.exe` NOT `python.exe` — use `taskkill //F //IM python3.13.exe`
- Add `encoding='utf-8'` to file opens to avoid UnicodeDecodeError
- Uvicorn reload may not pick up changes; sometimes need full process restart

## Architecture Notes
- Enriched order data uses `SimpleNamespace` for Jinja2 template compatibility
- Ads fallback: campaigns data shown when items API fails (3 states: normal, fallback, unavailable)
- All revenue figures show NET (after MeLi commission + IVA + shipping + IVA shipping)

## MeLi Batch Fetch — include_attributes=all (CRITICO)
- `GET /items?ids=X,Y&attributes=...variations...` sin `include_attributes=all`:
  → `variations[].attributes` llega como array VACÍO — SELLER_SKU invisible
- Con `include_attributes=all`: variaciones incluyen SELLER_SKU, GTIN, etc.
- `get_items_details()` en meli_client.py YA incluye `include_attributes=all`
- Sin este fix, items con SKU a nivel variación NO aparecen en el dashboard (ej: MLM843288099/SNTV002033)
- Aplica a TODAS las cuentas — muchos items tienen SKU en variations, no en item root

## MeLi catalog_listing — NO bloquear Sync (CRITICO — CORREGIDO)
- `catalog_listing: true` = item vinculado al catálogo de MeLi para SEO/atributos
  PERO no significa que el stock sea inmanejable — aplica al ~97% de los items
- SOLO bloquear Sync para `logistic_type: fulfillment` (FULL verdadero)
- `sub_status: ["out_of_stock"]` = pausado automáticamente por stock=0 (NO es pausa manual)
  MeLi muestra ambos como "Pausada" en SC — la diferencia está en el sub_status
- `_build_product_list` captura `catalog_listing: bool(body.get("catalog_listing"))` (solo info)
- cross_docking items con `shipping.lost_me1_by_user` warning: MeLi acepta PUT pero revierte en ~3s
  `update_item_stock` detecta este warning y lanza MeliApiError(422, "me1_required")

## MeLi SKU Storage (CRITICAL)
- SKU stored in TWO places: `seller_custom_field` (old) and `attributes[].SELLER_SKU` (new)
- `/users/{id}/items/search?seller_sku=X` finds BOTH — it's MeLi-indexed and reliable
- `/items?ids=` batch fetch does NOT always include `attributes` array (less data than `/items/{id}`)
- Most items use `SELLER_SKU` attribute, NOT `seller_custom_field` — always check both
- `_item_has_sku()` helper in `meli_client.py` checks both places at item + variation level
- `_get_item_sku()` / `_get_variation_sku()` helpers in `sku_inventory.py`
- `seller_sku` search results are trusted (no post-filter needed); `q` keyword results need verification

## MeLi Item Creation Notes
- Some categories require `family_name` in the item payload (catalog integration)
- `family_name` = short product family descriptor, generated from category + cleaned product name
- MeLi `/items/validate` and `/items` return 400 with error details in response body
- Attribute format: SELECT sends `value_id`, INPUT sends `value_name`
- SEO titles: strip ALL part numbers/model codes, use: Brand + Category Type + Descriptive Words
- MeLi exchange rate API: `/currency_conversions/search?from=USD&to=MXN`
- Category search: `/sites/MLM/search?q=keyword` returns `available_filters[id=category]` facets

## BinManager Auth (PENDIENTE — para cuando se requiera)
- Login: `POST /User/LoginUserByEmail` con `{"Email":"jovan.rodriguez@mitechnologiesinc.com","Password":"123456","COMPANYID":1}`
- Retorna JSON de usuario + setea cookie `ASP.NET_SessionId`
- **RetailPrice PH** = "Purchase History" — precio retail del manifest de compra del proveedor
- Endpoint: `POST /InventoryReport/InventoryReport/GetRetailPriceHistoryBySku`
  - Payload: `{"SKU": base_sku, "COMPANYID": 1, "Condition": null}`
  - **Requiere sesión autenticada** (cookie de login)
  - Retorna lista de registros con `PurchaseRetail`, `LoadDate`, `Supplier`, `MITLoadID`
  - UI muestra el más reciente como "RetailPrice PH"
  - SNAF000022 devuelve `[]` (sin historial); SNTV001763 devuelve $198.00 por unidad
- Credenciales BM: usar `BM_EMAIL`/`BM_PASSWORD` en `.env` cuando se implemente

## BinManager API (CRITICO — dos endpoints)
- **Endpoint para MTY/CDMX/TJ breakdown** (totales físicos): `Get_GlobalStock_InventoryBySKU_Warehouse`
  - `POST .../InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse`
  - Payload: `{"COMPANYID":1,"SKU":base_sku,"WarehouseID":null,"LocationID":"47,62,68","BINID":null,"Condition":"...","ForInventory":0,"SUPPLIERS":null}`
  - Retorna filas con `WarehouseName` y `QtyTotal` (incluye reservados)
- **Endpoint para DISPONIBLE REAL** (excluye reservados): `InventoryBySKUAndCondicion_Quantity`
  - `POST .../InventoryReport/InventoryBySKUAndCondicion_Quantity`
  - Payload (campos DISTINTOS!): `{"COMPANYID":1,"TYPEINVENTORY":0,"WAREHOUSEID":null,"LOCATIONID":"47,62,68","BINID":null,"PRODUCTSKU":base_sku,"CONDITION":"...","SUPPLIERS":null,"LCN":null,"SEARCH":base_sku}`
  - Retorna filas con `Available` (libre para vender), `Required` (reservado órdenes), `TotalQty`
  - **avail_total** = suma de `Available` de todas las filas por condición
  - **ForInventory:1 DESCARTADO** — era poco confiable (SNAF000022: devolvía 44, debía ser 0)
- **Cache**: `_bm_stock_cache` almacena `{mty, cdmx, tj, total, avail_total}` — `_bm_avail` en productos
- **`_get_bm_stock_cached()`**: `_wh_phase()` llama ambos endpoints en paralelo por SKU
- **FullFillment API DESCARTADO** — colapsa condiciones GRA/GRB/GRC al mismo ProductSKU
- **LocationIDs fijos**: 47=CDMX (Autobot/Ebanistas), 62=TJ, 68=MTY (Monterrey MAXX)
- **Mapeo warehouses**: "Monterrey MAXX"→MTY, "Autobot"→CDMX, otros→TJ (informativo)
- **TJ solo informativo** — total vendible = MTY + CDMX
- **InventoryReport**: SOLO para METADATA (Brand, Model, AvgCostQTY, RetailPrice) — NUNCA para stock
- `_clean_sku_for_bm()`: split por `/` y `+`, quitar `(N)`, parentesis sueltos
- Verificado: SNAF000022 → Available:0 / Required:19 ✓; SNTV001763 → Available:18 ✓

## Stock Issues — Inventory Management (Fases 1-2)
- `_enrich_with_bm_product_info()` DEBE llamarse en stock-issues endpoint para `_bm_retail_price`
- Stock Crítico: `available_quantity > 0 AND 0 < _bm_avail <= threshold` (default=10, no FULL, tiene SKU)
- Cache key incluye threshold: `f"stock_issues:{client.user_id}:t{threshold}"`
- `_inventory_global_cache` + `_INVENTORY_GLOBAL_TTL = 900` para Global Inventory
- `GET /api/inventory/global-scan`: procesa BM en chunks de 25 (bypass límite interno de 30)
- `_enrich_with_bm_product_info` tiene límite interno `[:30]` — para >30 SKUs llamar en chunks

## Product Ads Write API — BLOQUEADO (CRITICO)
- `certification_status: not_certified` bloquea TODOS los writes de Product Ads
- Probados 7 patrones de endpoint — NINGUNO funciona para escribir
- 401 con `ads_search_pads_core.api.exceptions.UnauthorizedException` en todos los writes
- NO hay workaround via API — es bloqueo a nivel de microservicio MeLi
- Solución única: certificar la app APANTALLATEMX en MeLi DevCenter (proceso formal)
- Alternativa manual: gestionar ads en ads.mercadolibre.com.mx directamente
- Railway persistence fix: `.env.production` tiene MELI_REFRESH_TOKEN fresco → `_seed_tokens()` lo usa al iniciar
- `/auth/` está en `_PIN_EXEMPT` para que el callback OAuth funcione sin cookie de PIN
- Estado stateless de OAuth: code_verifier embebido en state firmado con SECRET_KEY (sin DB)

## Amazon Onsite/Seller Flex — FBA API POR SKU (VERIFICADO 2026-03-04)
- **FBA Inventory API por SKU específico SÍ funciona para Seller Flex**: `GET /fba/inventory/v1/summaries?sellerSkus=SKU`
  - Retorna `fulfillableQuantity` = Available en SC, `reservedQuantity.totalReservedQuantity` = Reserved en SC
  - Verificado: `RMTC006588-FLX02` → fulfillable=13, reserved=25 ✓ (coincide exactamente con SC)
  - `fcProcessingQuantity` = uds procesando en fulfillment center
- **FBA Inventory all-scan NO incluye FLX items**: `get_fba_inventory_all()` → solo retorna items FBA clásico (50 items, sin FLX)
  - Para FLX, SIEMPRE usar `sellerSkus` en la query, NO el all-scan
- **`_refresh_flx_stock_bg`**: queries FBA API con `sellerSkus` en batches de 50 → guarda `{fulfillable, reserved, inbound, total}` en `_flx_stock_cache`
- **`_FLX_STOCK_TTL = 120`** (2 min) — inventario cambia con órdenes frecuentes
- **`fulfillmentAvailability[AMAZON_NA].quantity`** → campo ausente en Listings API para Seller Flex (no usar para stock)
- **`GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA`** → TAMBIÉN funciona, incluye 1314 FLX SKUs con `afn-fulfillable-quantity`
  - Útil como backup pero slow (30-90s para generar). `_get_onsite_stock_cached()` lo usa cuando disponible
- **Template**: columna FLX muestra `stock_flx` (disponible) + `+X res` (reservado) + `+X in` (inbound) en subrenglones
- Debug endpoints: `/api/amazon/products/seller-flex/cache-inspect`, `/raw-listing`

## Amazon SP-API Integration (completo — 2026-02-24)
- **Credenciales:** AMAZON_CLIENT_ID, AMAZON_CLIENT_SECRET, AMAZON_SELLER_ID=A20NFIUQNEYZ1E, AMAZON_MARKETPLACE_ID=A1AM78C64UM0Y8
- **App:** VeKtorClaude en Developer Central (Production, SP API, Sellers role)
- **tabla:** `amazon_accounts` en SQLite — separada de `tokens` (MeLi)
- **`amazon_client.py`:** AmazonClient con LWA auto-refresh, métodos orders/listings/stock/fba
- **Auth rutas:** `/auth/amazon/connect` → Seller Central OAuth → `/auth/amazon/callback` → guarda refresh_token
- **VERIFICADO:** Conexión LWA OK, SP-API OK — get_today_orders() devuelve órdenes reales de México
- **Marketplace IDs:** MX=A1AM78C64UM0Y8, US=ATVPDKIKX0DER, CA=A2EUQ1WTGCTBG2
- **LWA URL:** `https://api.amazon.com/auth/o2/token` (refresh token NO expira)
- **SP-API base:** `https://sellingpartnerapi-na.amazon.com` (región NA cubre MX+US+CA)

## Amazon Sales API (CRITICO — para revenue exacto)
- **Endpoint:** `GET /sales/v1/orderMetrics` — rate 0.5 req/s, burst 15
- **`totalSales.amount`** = Ordered Product Sales (OPS) = exactamente lo que muestra Amazon Seller Central
- **`OrderTotal.Amount`** de Orders API ≠ OPS: incluye shipping/taxes y NO disponible para Pending → NUNCA usar para revenue
- **Intervalo PST:** `{date}T00:00:00-08:00--{date+1}T00:00:00-08:00` (doble guión, extremo derecho exclusivo)
- **`granularityTimeZone=US/Pacific`** requerido cuando granularity=Day|Week|Month
- **Campos respuesta:** `interval`, `orderCount`, `unitCount`, `totalSales{currencyCode,amount}`, `averageUnitPrice`
- **`get_order_metrics(date_from, date_to_exclusive, granularity, tz)`** en AmazonClient
- **`_get_cached_order_metrics(client, date_from, date_to)`** en metrics.py — date_to es INCLUSIVE (se suma 1 día)
- **Caché:** `_amazon_metrics_cache` con double-check lock, TTL 5 min; clave `"metrics:{seller_id}:{date_from}:{date_to}"`
- amazon-dashboard-data y amazon-daily-sales-data usan Sales API (commit 75eb37b)

## Amazon Timezone Quirk (CRITICO)
- Amazon Seller Central usa **PST (UTC-8)** para límites de "hoy"
- PurchaseDate en Orders API está en UTC
- Multi-dashboard `today_str`: usar `now_mx = utcnow - timedelta(hours=6)` (CST = UTC-6 febrero)
- Sin fix: después de 6 PM CST (medianoche UTC) → today_str = mañana → 0 órdenes
- Commit fix timezone: 75eb37b

## MeLi Promotions API v2 (CRITICAL)
- Endpoint: `/seller-promotions/items/{item_id}?app_version=v2`
- Campo correcto: `deal_price` (NO `price`, NO `discounted_price`)
- DEAL/DOD/LIGHTNING: PUT con `{deal_price, promotion_type, promotion_id}`
- PRICE_DISCOUNT: POST con `{deal_price, promotion_type, start_date, finish_date}`
- SMART/PRE_NEGOTIATED: NO modificables via API (MeLi los gestiona automaticamente)
- DELETE: `/seller-promotions/items/{id}?app_version=v2&promotion_type=TYPE`
- Errores comunes: "DISCOUNT_GT_ALLOWED" = campo incorrecto; "Invalid promotion type" = SMART

## Amazon Ventas — Historial de Órdenes (2026-02-25)
- `/amazon/orders` → página con filtros fecha + stats banner + tabla
- `GET /api/amazon/orders` → HTML partial (stats 4 cards + tabla), caché 5min
- `GET /api/amazon/orders/{id}/items` → HTML partial lazy (click "Ver ▼"), caché 10min
- `app/api/amazon_orders.py` router prefix `/api/amazon`, active_amazon_tab="orders"
- SP-API quirk: Pending fetch separado (no mezclar con Shipped/Unshipped)
- Tab nav: Dashboard | 💸 Ventas | 💚 Salud | 📦 Productos (mobile grid 2x2)

## Amazon Centro de Productos v2 (2026-02-25)
- `/amazon/products` rediseñado: 4 tabs nuevos (Resumen, Inventario, Stock, Sin Publicar)
- Endpoints NUEVOS: `/api/amazon/products/resumen|inventario|stock|sin-publicar`
- Endpoints VIEJOS conservados: `summary|catalog|inventory|buybox` (usados por dashboard)
- `_get_sku_sales_cached()` → {sku: {units, revenue}} 30d, caché 30min, lotes de 5 órdenes
- Tab Resumen: revenue OPS via `_get_cached_order_metrics` (importado de metrics.py)
- Tab Inventario: subtabs via HTMX (filter+sort params), días supply con colores
- Tab Stock: Sin Stock / Bajo (<10) / Restock (<14d supply)
- Tab Sin Publicar: Suprimidos (INACTIVE+issues) / Inactivos / Activos con issues
- JS: `switchAmzProdTab()` global para navegar desde partials; `updateAmzPrice()` conservado

## Dashboard Architecture (post-rediseño 2026-02-20)
- Banner de cuenta en dashboard: color por user_id definido en JS (`ACC_COLORS` dict)
- CSS variable `--acc-color` para consistencia de color en toda la página
- `active_user_id` pasado desde Jinja2 al JS como `'{{ active_user_id }}'`
- Quick stats del día en el banner: cargados desde `daily-sales` API response
- Meta diaria: guardada en `account_settings` table (no localStorage)
- API: `GET /api/metrics/goal` y `POST /api/metrics/goal` → por cuenta activa
- `daily-sales` endpoint devuelve `goal` en la respuesta para sincronizar el input
- Mobile: selector de cuentas en menú hamburguesa (grid 2 cols al fondo del menú)

## Performance Dashboard (post-fix 2026-02-20)
- `enrich_orders_with_net_amount` REMOVIDO de daily-sales → era el cuello de botella principal
- Cache key de órdenes incluye `user_id`: `f"orders:{user_id}:{date_from}:{date_to}"`
- Carga típica del dashboard: 2-4 seg (antes: 15-30 seg)
- Fórmula net revenue (sin /collections): suficientemente precisa para dashboard

## Cuentas MeLi Registradas (4 cuentas)
- Slot 1: APANTALLATEMX — user_id 523916436
- Slot 2: AUTOBOT MEXICO — user_id 292395685
- Slot 3: BLOWTECHNOLOGIES — user_id 391393176
- Slot 4: LUTEMAMEXICO — user_id 515061615
- Todos en .env.production (git) → se seedean en cada Railway deploy automáticamente
- Para agregar más: /auth/connect → commit .env.production con nuevo slot

## Multi-Cuenta Architecture (ContextVar approach)
- `ContextVar _active_user_id` en `meli_client.py` — setea por request via `AccountMiddleware`
- `AccountMiddleware` (main.py): cookie `active_account_id` → valida DB → setea ContextVar
- `get_meli_client(user_id=None)`: sin user_id usa ContextVar → fallback `get_any_tokens()`
- NO fue necesario cambiar endpoints individuales — ContextVar lo hace automáticamente
- `POST /auth/switch-account`: setea cookie `active_account_id=uid` (30 días)
- `token_store.get_all_tokens()` → `[{user_id, nickname}]` para dropdown
- Helper `_accounts_ctx(request)` → `{accounts, active_user_id}` para templates
- OAuth callback guarda nickname; auto-detecta cuenta1 vs cuenta2 por user_id
- Flujo 2da cuenta: `/auth/connect` → OAuth → callback → cookie → dropdown

## Railway Deploy
- URL: `https://apantallatemx.up.railway.app`
- GitHub: `ApantallateMX/mercado-libre-dashboard` (privado)
- Auto-deploy en cada push a `main`
- Railway V2 runtime NO inyecta service variables → workaround: `.env.production` committeado
- ANTHROPIC_API_KEY solo en Railway Variables (GitHub Push Protection bloquea)
- `config.py` carga `.env.production` primero, luego `.env` como fallback
- MeLi redirect URI prod: `https://apantallatemx.up.railway.app/auth/callback`

## Uvicorn WatchFiles on Windows
- Changes to multiple files may only trigger ONE reload notification
- After reload, some routes may 404 until full process restart
- Best practice: kill all python processes and restart fresh when routes go missing
