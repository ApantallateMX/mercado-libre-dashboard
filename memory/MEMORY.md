# Mercado Libre Dashboard - Key Learnings

## Deploy (OBLIGATORIO)
- SIEMPRE hacer commit + push a `main` después de cualquier cambio
- Railway auto-deploya en cada push a `main`
- URL: `https://apantallatemx.up.railway.app`
- NO esperar a que el usuario lo pida

## Git Config
- user: ApantallateML / Jovan.Rodriguez@mitechnologiesinc.com

## Project Stack
- FastAPI + Jinja2 + HTMX + Tailwind CSS
- Uvicorn port 8000, ngrok para acceso externo
- SQLite para tokens, listings cache, BM stock cache

## Changelog / DEVLOG
- Ver `memory/CHANGELOG.md` para historial de features grandes
- Ver `DEVLOG.md` para log detallado de bugs/fixes/decisiones
- SIEMPRE actualizar DEVLOG al hacer cambios significativos

## Jinja2 Gotcha: dict key "items"
- `order.items` → resuelve a `dict.items()` method, NO la key "items"
- Solución: usar `SimpleNamespace` o renombrar la key

## MeLi API Patterns
- `get_ads_items` devuelve 503 intermitente; usar "campaigns first"
- Order net revenue = total_amount - sale_fee - IVA_fee - shipping_cost - IVA_shipping
- `enrich_orders_with_shipping(orders)` agrega `_shipping_cost` y `_iva_shipping`
- Shipping cost: `/shipments/{id}/costs` → `senders[0].cost`

## MeLi Batch Fetch — include_attributes=all (CRITICO)
- `GET /items?ids=X,Y` sin `include_attributes=all` → `variations[].attributes` llega vacío → SELLER_SKU invisible
- `get_items_details()` en `meli_client.py` YA incluye `include_attributes=all`

## MeLi catalog_listing — NO bloquear Sync (CRITICO)
- `catalog_listing: true` NO significa stock inmanejable — aplica al ~97% de items
- SOLO bloquear Sync para `logistic_type: fulfillment` (FULL verdadero)
- `sub_status: ["out_of_stock"]` = pausado automático por stock=0 (NO pausa manual)
- cross_docking + `lost_me1_by_user` warning → MeLi acepta PUT pero revierte en ~3s

## MeLi SKU Storage (CRITICAL)
- SKU en DOS lugares: `seller_custom_field` (viejo) y `attributes[].SELLER_SKU` (nuevo)
- Para items con variaciones: SIEMPRE usar SKU de variación, NUNCA el `seller_custom_field` del padre
- `/users/{id}/items/search?seller_sku=X` — confiable, indexado por ML
- `_item_has_sku()` / `_get_item_sku()` helpers verifican ambos lugares

## BinManager Stock (CRITICO — 2026-04-02)
- **Endpoint correcto:** `Get_GlobalStock_InventoryBySKU` con CONCEPTID=1
- **AvailableQTY** = TotalQty - Reserve (calculado server-side) — campo correcto
- `GlobalStock_InventoryBySKU_Condition.status` siempre "Otro" — campo legacy, no confiable
- `get_available_qty()` en `binmanager_client.py` usa CONCEPTID=1 correctamente
- Condition-variant fallback: SKUs como SNTV004196 pueden existir como SNTV004196-GRB en BM
- Cache EMPTY (total=0, avail=0) fuerza re-fetch
- LocationIDs: 47=CDMX, 62=TJ, 68=MTY MAXX, 66=GDL (excluida)
- `bm_stock_cache` SQLite se precarga al arranque desde DB
- `_prewarm_queued` evita perder llamadas de prewarm cuando ya hay una corriendo
- "Sync ahora" espera a que prewarm complete antes de recargar UI

## BinManager RetailPrice
- `RetailPrice` con `SEARCH=` → siempre 0
- Campo correcto: `LastRetailPricePurchaseHistory` (requiere `NEEDRETAILPRICEPH: true`)
- `AvgCostQTY = 9999.99` = costo no configurado — filtrar cuando >= 9000

## ML Listings DB
- Tabla `ml_listings` con columna `data_json` (body completo del item)
- `ml_listing_sync.py`: sync completo al arranque (30s delay), incremental cada 10min
- `_get_all_products_cached` lee de DB si `synced_at < 1h` → <100ms vs 300 API calls

## Stock Issues — Reglas
- Stock Crítico: `available_quantity > 0 AND 0 < _bm_avail <= threshold` (default=10, no FULL, tiene SKU)
- oversell_risk: `_bm_avail == 0` (NO `_bm_total`) — usar disponible, no físico bruto
- FULL items (logistic_type=fulfillment): excluir de sync. Si BM>0 → alerta para cambiar a Merchant
- `_bm_avail` = vendible sin reservas; `_bm_total` = físico bruto

## Sync Stock — Reglas
- NUNCA pausar. BM=0 → qty=0 en activos. BM>0 → activar pausados + setear qty
- Si BM falla (timeout/5xx): skippear SKU en lugar de poner qty=0
- Bundle SKUs (separados por `/` o `+`): stock = mínimo entre componentes

## Product Ads (MeLi) — BLOQUEADO
- `certification_status: not_certified` bloquea TODOS los writes de Product Ads
- Workaround: gestionar en ads.mercadolibre.com.mx directamente

## MeLi Promotions API v2
- Endpoint: `/seller-promotions/items/{item_id}?app_version=v2`
- Campo correcto: `deal_price` (NO `price`, NO `discounted_price`)

## Amazon SP-API
- Credenciales: AMAZON_CLIENT_ID/SECRET, AMAZON_SELLER_ID=A20NFIUQNEYZ1E, MARKETPLACE=A1AM78C64UM0Y8
- LWA auto-refresh, SigV4 NO requerido
- FBA Inventory por SKU: `GET /fba/inventory/v1/summaries?sellerSkus=SKU` — funciona para Seller Flex
- FBA all-scan NO incluye FLX items — siempre usar `sellerSkus` para FLX
- Revenue exacto: `GET /sales/v1/orderMetrics` (no Orders API — incluye shipping/taxes)
- Timezone: Amazon SC usa PST (UTC-8) para límites de "hoy"

## Amazon Fulfillment Management
- `update_listing_fulfillment(sku, action, quantity)` en `amazon_client.py`
- Acciones: `pause` / `set_merchant` / `set_qty` (DEFAULT) / `reactivate_fba` (AMAZON_NA)

## Cuentas MeLi (4)
- APANTALLATEMX: 523916436
- AUTOBOT MEXICO: 292395685
- BLOWTECHNOLOGIES: 391393176
- LUTEMAMEXICO: 515061615
- Todos en `.env.production` → Railway los seedea en cada deploy

## Railway Deploy
- GitHub: `ApantallateMX/mercado-libre-dashboard` (privado)
- `.env.production` committeado (Railway V2 no inyecta service variables automáticamente)
- `ANTHROPIC_API_KEY` solo en Railway Variables (no en repo por seguridad)
- MeLi redirect URI: `https://apantallatemx.up.railway.app/auth/callback`

## Windows/Git Bash
- `taskkill //PID //F` (no `/PID`)
- Python exe: `python3.13.exe`
- `encoding='utf-8'` en file opens para evitar UnicodeDecodeError
