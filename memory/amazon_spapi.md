# Amazon SP-API — Knowledge Base

## Autenticación
- LWA token: POST https://api.amazon.com/auth/o2/token
  - grant_type=refresh_token + client_id + client_secret + refresh_token
  - access_token válido 3600s — renovar automáticamente
- Header requerido: `x-amz-access-token: <access_token>`
- SigV4 NO requerido con LWA para SP-API (solo x-amz-access-token funciona)
- Base URL: https://sellingpartnerapi-na.amazon.com (NA = MX + US + CA)
- Sandbox: https://sandbox.sellingpartnerapi-na.amazon.com
- Marketplace MX: A1AM78C64UM0Y8

## Listings Items API v2021-08-01
- Single: GET /listings/2021-08-01/items/{sellerId}/{sku}
- All:    GET /listings/2021-08-01/items/{sellerId}?marketplaceIds=A1AM78C64UM0Y8
- includedData opciones: summaries, offers, fulfillmentAvailability, issues, relationships
- Status en summaries: BUYABLE (activo+vendible), DISCOVERABLE (visible pero no vendible)
- Rate: 5 req/s, burst 5
- Paginación via `pagination.nextToken`

## Catalog Items API v2022-04-01
- Single ASIN: GET /catalog/2022-04-01/items/{asin}?marketplaceIds=A1AM78C64UM0Y8
- Search: GET /catalog/2022-04-01/items?marketplaceIds=...&identifiers=...&identifiersType=ASIN
- includedData: summaries, images, attributes, dimensions, salesRanks, identifiers
- salesRanks → BSR (Best Seller Rank) en categorías
- Rate: 2 req/s single, 5 req/s search

## FBA Inventory API v1
- GET /fba/inventory/v1/summaries
- Params: granularityType=Marketplace, granularityId=A1AM78C64UM0Y8, marketplaceIds=..., details=true
- Campos clave:
  - sellerSku, asin, fnSku, condition, productName
  - inventoryDetails.fulfillableQuantity → vendible
  - inventoryDetails.reservedQuantity.pendingCustomerOrderQuantity → en órdenes
  - inventoryDetails.unfulfillableQuantity.totalUnfulfillableQuantity → dañado
  - inventoryDetails.inboundWorkingQuantity + inboundShippedQuantity → en camino
  - totalQuantity → todo el inventario físico
- Rate: 2 req/s, burst 2

## Orders API v0
- GET /orders/v0/orders
- Params: MarketplaceIds (repetido), CreatedAfter, CreatedBefore, OrderStatuses (repetido)
- OrderStatuses válidos (MX): Shipped, Unshipped, PartiallyShipped
  - InvoiceUnconfirmed SOLO Brasil — causa 400 en MX
- CreatedBefore DEBE ser al menos 2 min antes de now (usar now-5min para hoy)
- Paginación via NextToken
- GET /orders/v0/orders/{orderId}/orderItems → items por orden
- Rate: baja, usar Semaphore(2)

## Product Pricing API v0
- Competitive pricing: GET /products/pricing/v0/competitivePrice
  - Params: MarketplaceId, Asins, ItemType=Asin
  - Rate: 0.5 req/s, burst 1 — MUY LENTO, máx 20 ASINs por sesión
- Listing offers: GET /products/pricing/v0/listings/{SellerSKU}/offers
  - Rate: 1 req/s, burst 2
  - Respuesta: BuyBoxPrices, IsBuyBoxWinner, TotalOfferCount
- Batch: POST /batches/products/pricing/v0/itemOffers (hasta 20 ASINs)
  - Rate: 0.1 req/s — usar solo para análisis profundo

## Reports API v2021-06-30 (proceso asíncrono)
1. POST /reports/2021-06-30/reports → {reportId}
2. GET /reports/2021-06-30/reports/{reportId} → status DONE/IN_PROGRESS
3. GET /reports/2021-06-30/documents/{reportDocumentId} → URL de descarga S3
- GET_MERCHANT_LISTINGS_ALL_DATA → TSV de todos los listings (SKU, ASIN, precio, stock)
- GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA → inventario FBA vendible
- Rate: createReport 1/min, burst 15

## Bugs conocidos y fixes
- get_all_amazon_accounts() NO devuelve refresh_token (solo campos UI)
  → Usar get_amazon_account(seller_id) para credenciales completas
- OrderStatuses CSV causa 400 → usar lista de tuplas en httpx params
- CreatedBefore=hoy T23:59:59Z causa 400 → usar now-5min si date_to >= today
- _seed_amazon_accounts() siempre debe hacer upsert (sin condición)
  → El SQL preserva refresh_token existente si el nuevo está vacío

## Errores de Autorización — Códigos y Soluciones
| Código | Causa | Solución |
|--------|-------|----------|
| MD1000 | App en Draft, OAuth en producción | Agregar `&version=beta` a la URL de autorización |
| MD5101 | redirect_uri no coincide con la registrada | Verificar redirect_uri exacta en Developer Portal |
| MD5110 | redirect_uri tiene fragmentos (#) | Quitar # de la redirect_uri |
| MD9100 | App sin login URI ni redirect URI configurada | Configurar URIs en Solution Provider Portal — O usar self-authorization correcta |
| SPDC8143 | Usuario no es el principal de la cuenta | Usar credenciales del usuario principal de Seller Central |

## Error 403 "The LWA secret token you provided has expired."
- El refresh_token fue REVOCADO (Amazon lo invalida si el vendedor revoca la app)
- El access_token se obtuvo correctamente (LWA devuelve 200) pero no tiene permisos
- Solución: generar nuevo refresh_token vía self-authorization (ver abajo)
- NOTA: Generar un nuevo token NO invalida los anteriores (pero Amazon sí puede revocarlos externamente)

## Self-Authorization — Flujo Correcto (app privada del desarrollador)
El flujo `/apps/authorize/consent` con OAuth es para THIRD-PARTY (otras cuentas autorizando tu app).
Para self-authorization (desarrollador = vendedor) el flujo es DIFERENTE:

### Pasos para obtener nuevo refresh_token (self-auth):
1. Entrar a Seller Central MX como usuario PRINCIPAL: https://sellercentral.amazon.com.mx/
2. Ir a: Apps y Servicios → Desarrollar apps (o "Develop Apps")
   URL directa: https://sellercentral.amazon.com.mx/apps/develop
3. Buscar la app (VeKtorClaude / App ID: amzn1.sp.solution.edc432e9...)
4. Hacer clic en "Authorize app" / "Autorizar app"
5. Se genera un nuevo refresh_token — COPIARLO inmediatamente
6. Actualizar AMAZON_REFRESH_TOKEN en .env.production + hacer commit

### Por qué falla el URL /apps/authorize/consent:
- Ese endpoint es para OAuth de terceros
- Si la app no tiene redirect_uri configurada en Developer Portal → MD9100
- Para self-auth, usar Seller Central → Develop Apps (sin flujo OAuth)

## Grant Types
- `refresh_token`: Para operaciones del vendedor (Orders, Listings, FBA) — requiere refresh_token del vendedor
- `client_credentials`: Para operaciones grantless (Notifications, etc.) — no requiere seller auth

## Ciclo de vida del refresh_token
- NO expira por tiempo (a diferencia del access_token)
- Se invalida si: vendedor revoca en Seller Central, o Amazon lo revoca por seguridad
- Generar nuevo token NO invalida los anteriores
- Guardar de forma segura — NUNCA en código público

## Arquitectura del cliente
- Clase: AmazonClient(seller_id, client_id, client_secret, refresh_token, marketplace_id, nickname, marketplace_name)
- Factory: get_amazon_client(seller_id=None) → usa primer account de DB
- _seed_amazon_accounts(): siempre upsert al arrancar para mantener credenciales frescas
- Caché en memoria por instancia: _access_token + _token_expires_at

## Rate Limits — Resumen
| API | Rate | Burst |
|-----|------|-------|
| Listings search | 5/s | 5 |
| Catalog single | 2/s | 2 |
| FBA Inventory | 2/s | 2 |
| Orders | ~2/s | ~15 |
| Pricing competitive | 0.5/s | 1 |
| Pricing listing offers | 1/s | 2 |
| Reports create | 1/min | 15 |
