"""
amazon_client.py — Cliente para Amazon Selling Partner API (SP-API)

PROPÓSITO:
    Encapsula toda la comunicación con Amazon SP-API.
    Equivalente a meli_client.py pero para Amazon.

AUTENTICACIÓN:
    Amazon usa LWA (Login with Amazon) — diferente a MeLi:
    - refresh_token (larga duración, no expira) → guardado en DB
    - access_token  (1 hora) → se renueva automáticamente en memoria

ENDPOINTS USADOS:
    - Orders API:           GET /orders/v0/orders
    - Order Items API:      GET /orders/v0/orders/{id}/orderItems
    - Listings Items API:   GET/PATCH /listings/2021-08-01/items/{sellerId}/{sku}
    - FBA Inventory API:    GET /fba/inventory/v1/summaries

REGIÓN:
    North America (NA) cubre México, USA y Canadá:
    Base URL: https://sellingpartnerapi-na.amazon.com

MARKETPLACE IDs:
    México  → A1AM78C64UM0Y8
    USA     → ATVPDKIKX0DER
    Canadá  → A2EUQ1WTGCTBG2

NOTAS IMPORTANTES:
    - FBA: Amazon gestiona el stock físico, no podemos reducirlo directamente
    - MFN: Stock gestionado por nosotros, podemos actualizar quantity
    - Para "apagar" un listing: PATCH fulfillment_availability → quantity: 0
    - Rate limits: los endpoints de Orders tienen límites estrictos (0.5 req/s)
      por eso se usa asyncio.Semaphore para controlar concurrencia
"""

import httpx
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────────────────────

# URL para obtener/renovar access tokens (LWA — Login with Amazon)
AMAZON_LWA_URL = "https://api.amazon.com/auth/o2/token"

# URL base del SP-API para región Norte América (incluye México)
AMAZON_SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"

# Marketplace IDs de América del Norte
MARKETPLACE_IDS = {
    "MX": "A1AM78C64UM0Y8",
    "US": "ATVPDKIKX0DER",
    "CA": "A2EUQ1WTGCTBG2",
}

# Rate limit seguro para Orders API (Amazon permite ~0.5 req/s)
_ORDERS_SEMAPHORE = asyncio.Semaphore(2)

# Rate limit para Sales API — conservador para evitar 429
_SALES_SEMAPHORE = asyncio.Semaphore(1)


# ─────────────────────────────────────────────────────────────────────────────
# CLASE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

class AmazonClient:
    """
    Cliente asíncrono para Amazon SP-API.

    Uso básico:
        client = AmazonClient(
            seller_id="A20NFIUQNEYZ1E",
            client_id="amzn1.application-oa2-client.XXX",
            client_secret="amzn1.oa2-cs.v1.XXX",
            refresh_token="Atzr|XXX",
            marketplace_id="A1AM78C64UM0Y8"   # México
        )
        orders = await client.get_orders(created_after="2026-02-01T00:00:00Z")
    """

    def __init__(
        self,
        seller_id: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        marketplace_id: str = "A1AM78C64UM0Y8",
        nickname: str = "",
        marketplace_name: str = "MX",
    ):
        # Identificador del vendedor (Merchant Token de Seller Central)
        self.seller_id = seller_id

        # Credenciales LWA de la app (Developer Central → VeKtorClaude)
        self.client_id = client_id
        self.client_secret = client_secret

        # Token de larga duración para renovar access_token
        self.refresh_token = refresh_token

        # Marketplace donde opera (default: México)
        self.marketplace_id = marketplace_id

        # Nombre visible de la cuenta (para el UI)
        self.nickname = nickname or seller_id

        # Nombre del marketplace ("MX", "US", "CA") para mostrar en UI
        self.marketplace_name = marketplace_name or "MX"

        # Cache del access_token en memoria — se renueva automáticamente
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    # ─────────────────────────────────────────────────────────────────────
    # AUTENTICACIÓN LWA
    # ─────────────────────────────────────────────────────────────────────

    async def _get_access_token(self) -> str:
        """
        Obtiene un access token válido usando LWA.

        - Si ya tenemos uno en cache y no expiró → lo reutiliza
        - Si expiró o no existe → hace POST a LWA con el refresh_token
        - El nuevo access_token dura 1 hora (3600 seg)
        - Renueva 5 minutos antes para evitar carreras
        """
        # Verificar si el token en cache aún es válido
        if (
            self._access_token
            and self._token_expires_at
            and datetime.utcnow() < self._token_expires_at - timedelta(minutes=5)
        ):
            return self._access_token

        # Token expirado o inexistente → renovar via LWA
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                AMAZON_LWA_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Guardar en cache de instancia
        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        logger.debug(f"[Amazon] Token renovado para {self.seller_id}, expira en {expires_in}s")
        return self._access_token

    # ─────────────────────────────────────────────────────────────────────
    # HELPER DE REQUEST
    # ─────────────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict = None,
        json_body: dict = None,
        timeout: int = 30,
    ) -> dict:
        """
        Realiza una llamada autenticada al SP-API.

        El header 'x-amz-access-token' es el equivalente al
        'Authorization: Bearer' de MeLi — es lo que identifica al vendedor.

        Manejo de errores:
        - 429 Too Many Requests → Amazon te está limitando, esperar y reintentar
        - 403 Forbidden → Token expirado o permisos insuficientes
        - 404 Not Found → SKU/Order no existe en ese marketplace
        """
        token = await self._get_access_token()

        headers = {
            "x-amz-access-token": token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.request(
                method,
                f"{AMAZON_SP_API_BASE}{path}",
                headers=headers,
                params=params,
                json=json_body,
            )

            if resp.status_code == 429:
                # Rate limited — esperar 2s y reintentar una vez
                logger.warning(f"[Amazon] 429 rate limit en {path}, reintentando en 2s")
                await asyncio.sleep(2)
                resp = await http.request(
                    method,
                    f"{AMAZON_SP_API_BASE}{path}",
                    headers=headers,
                    params=params,
                    json=json_body,
                )

            if not resp.is_success:
                # Capturar el body del error para mostrar mensaje claro
                try:
                    err_body = resp.json()
                    errs = err_body.get("errors", [])
                    if errs:
                        details = errs[0].get("details") or errs[0].get("message", "")
                        code    = errs[0].get("code", "")
                        amz_msg = f"[{code}] {details}" if details else str(err_body)
                    else:
                        amz_msg = str(err_body)
                except Exception:
                    amz_msg = resp.text[:300]

                logger.error(f"[Amazon] HTTP {resp.status_code} en {path}: {amz_msg}")

                # Si el token está expirado, limpiarlo para forzar renovación
                if resp.status_code in (401, 403):
                    self._access_token = None
                    self._token_expires_at = None

                # Lanzar error con mensaje legible (incluye el mensaje de Amazon)
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} — {amz_msg}",
                    request=resp.request,
                    response=resp,
                )

            return resp.json()

    # ─────────────────────────────────────────────────────────────────────
    # ÓRDENES
    # ─────────────────────────────────────────────────────────────────────

    async def get_orders(
        self,
        created_after: str,
        created_before: str = None,
        marketplace_ids: list = None,
        order_statuses: list = None,
        fulfillment_channels: list = None,
        max_pages: int = 0,
    ) -> list:
        """
        Obtiene órdenes del marketplace.

        Args:
            created_after:   Fecha ISO 8601 (ej. "2026-01-01T00:00:00Z")
            created_before:  Fecha ISO 8601 opcional (default: ahora)
            marketplace_ids: Lista de IDs de marketplace (default: el de la instancia)
            order_statuses:  Lista de estados a filtrar (default: Shipped+Unshipped+PartiallyShipped)
                             NOTA: NO mezclar Pending con otros — SP-API quirk devuelve solo Pending.
                             Usar fetch_orders_range(statuses=["Pending"]) por separado.
            max_pages:       Límite de páginas (0 = sin límite). Usar max_pages=1 para "recent only".

        Returns:
            Lista de órdenes con campos: AmazonOrderId, OrderStatus,
            PurchaseDate, OrderTotal, NumberOfItemsShipped, etc.

        Notas:
            - Paginación automática via NextToken
            - getOrders rate limit: 0.0167 req/s, burst 20 → sleep entre páginas
        """
        if marketplace_ids is None:
            marketplace_ids = [self.marketplace_id]
        if order_statuses is None:
            order_statuses = ["Shipped", "Unshipped", "PartiallyShipped"]

        # SP-API exige parámetros repetidos para listas, no CSV
        params: list = (
            [("MarketplaceIds", mid) for mid in marketplace_ids]
            + [("CreatedAfter", created_after)]
            + [("OrderStatuses", s) for s in order_statuses]
            + ([("FulfillmentChannels", ch) for ch in fulfillment_channels] if fulfillment_channels else [])
        )
        if created_before:
            params.append(("CreatedBefore", created_before))

        async with _ORDERS_SEMAPHORE:
            result = await self._request("GET", "/orders/v0/orders", params=params)

        orders = result.get("payload", {}).get("Orders", [])

        # Paginación: Amazon devuelve NextToken cuando hay más resultados
        # Sleep entre páginas para no agotar el burst (20 req) en catálogos grandes
        next_token = result.get("payload", {}).get("NextToken")
        page = 1
        while next_token:
            if max_pages and page >= max_pages:
                break
            await asyncio.sleep(0.5)  # ~2 req/s — conservador frente al burst/20
            async with _ORDERS_SEMAPHORE:
                next_result = await self._request(
                    "GET",
                    "/orders/v0/orders",
                    params=[
                        ("NextToken", next_token),
                        *[("MarketplaceIds", mid) for mid in marketplace_ids],
                    ],
                )
            orders.extend(next_result.get("payload", {}).get("Orders", []))
            next_token = next_result.get("payload", {}).get("NextToken")
            page += 1

        return orders

    async def get_order_items(self, order_id: str) -> list:
        """
        Obtiene los productos (line items) de una orden específica.

        Returns:
            Lista con campos: ASIN, SellerSKU, QuantityOrdered, ItemPrice, etc.

        Nota: Este endpoint tiene rate limit separado y más estricto.
        Usar con moderación — preferir get_sales_30d() que lo agrupa.
        """
        async with _ORDERS_SEMAPHORE:
            result = await self._request(
                "GET", f"/orders/v0/orders/{order_id}/orderItems"
            )
        return result.get("payload", {}).get("OrderItems", [])

    async def get_order_financial_events(self, order_id: str) -> dict:
        """
        Retorna eventos financieros de una orden (Finances API v0).
        Disponible solo después de que Amazon shippe/liquide la orden.
        Returns {} vacío si la orden está Pending o aún no procesada.
        """
        try:
            async with _ORDERS_SEMAPHORE:
                result = await self._request(
                    "GET", f"/finances/v0/orders/{order_id}/financialEvents"
                )
            return result.get("payload", {}).get("FinancialEvents", {})
        except Exception:
            return {}

    async def get_sales_summary_30d(self) -> dict:
        """
        Calcula ventas por SKU en los últimos 30 días.

        Returns:
            Dict {sku: units_sold} — ej. {"SNAF000022-GRA": 15, "SNTV001763-GRB": 8}

        Proceso:
            1. Pedir órdenes de los últimos 30 días
            2. Para cada orden, pedir sus items (con rate limiting)
            3. Acumular unidades por SellerSKU

        ADVERTENCIA: Puede ser lento si hay muchas órdenes.
        Usar con caché — no llamar en cada request.
        """
        created_after = (datetime.utcnow() - timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        orders = await self.get_orders(created_after)

        sku_sales: dict = {}
        for order in orders:
            # Ignorar órdenes canceladas o pendientes de pago
            if order.get("OrderStatus") in ("Cancelled", "Pending"):
                continue

            order_id = order.get("AmazonOrderId", "")
            if not order_id:
                continue

            try:
                items = await self.get_order_items(order_id)
                for item in items:
                    sku = item.get("SellerSKU", "").strip()
                    qty = int(item.get("QuantityOrdered", 0))
                    if sku and qty > 0:
                        sku_sales[sku] = sku_sales.get(sku, 0) + qty
            except Exception as e:
                logger.warning(f"[Amazon] Error obteniendo items de orden {order_id}: {e}")

        return sku_sales

    async def get_sku_sales(self, date_from: str, date_to: str) -> dict:
        """
        Ventas por SKU en un rango de fechas arbitrario — igual que
        get_sales_summary_30d() pero con rango configurable y agregando
        ingreso además de unidades (para la vista SKU, equivalente a
        /partials/sku-sales-table de ML).

        Args:
            date_from, date_to: "YYYY-MM-DD"

        Returns:
            Dict {sku: {"units": int, "revenue": float, "orders": int}}

        ADVERTENCIA: mismo patrón N+1 que get_sales_summary_30d (1 request de
        items por orden) — usar SIEMPRE detrás de un caché con TTL, nunca en
        vivo por request (ver _fetch_amazon_sku_sales_cached en main.py).
        """
        created_after = f"{date_from}T00:00:00Z"
        # Amazon rechaza CreatedBefore si no es al menos ~2 min anterior a "ahora"
        # (falla incluso si el rango solicitado es "hasta hoy" y hoy 23:59:59 aún
        # no llegó) — se acota al menor entre fin-de-día solicitado y ahora-5min.
        _requested_before = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        _safe_before = min(_requested_before, datetime.utcnow() - timedelta(minutes=5))
        created_before = _safe_before.strftime("%Y-%m-%dT%H:%M:%SZ")
        orders = await self.get_orders(created_after, created_before=created_before)

        sku_sales: dict = {}
        for order in orders:
            if order.get("OrderStatus") in ("Cancelled", "Pending"):
                continue
            order_id = order.get("AmazonOrderId", "")
            if not order_id:
                continue
            try:
                items = await self.get_order_items(order_id)
            except Exception as e:
                logger.warning(f"[Amazon] Error obteniendo items de orden {order_id}: {e}")
                continue
            seen_skus_this_order = set()
            for item in items:
                sku = (item.get("SellerSKU") or "").strip()
                qty = int(item.get("QuantityOrdered", 0) or 0)
                if not sku or qty <= 0:
                    continue
                price = item.get("ItemPrice") or {}
                amount = float(price.get("Amount", 0) or 0)
                if sku not in sku_sales:
                    sku_sales[sku] = {"units": 0, "revenue": 0.0, "orders": 0}
                sku_sales[sku]["units"] += qty
                sku_sales[sku]["revenue"] = round(sku_sales[sku]["revenue"] + amount, 2)
                if sku not in seen_skus_this_order:
                    sku_sales[sku]["orders"] += 1
                    seen_skus_this_order.add(sku)

        return sku_sales

    # ─────────────────────────────────────────────────────────────────────
    # LISTINGS (PRODUCTOS)
    # ─────────────────────────────────────────────────────────────────────

    async def get_listing(self, sku: str) -> Optional[dict]:
        """
        Obtiene un listing de Amazon por SellerSKU.

        Returns:
            Dict con campos del listing, o None si no existe en este marketplace.

        Campos importantes en la respuesta:
            - productType: tipo de producto (ej. "TELEVISION", "HEADPHONES")
            - summaries[].status: "BUYABLE" = activo, "SUPPRESSED" = bloqueado
            - fulfillmentAvailability[].quantity: stock disponible (MFN)
            - attributes: atributos del producto (precio, título, etc.)
        """
        try:
            result = await self._request(
                "GET",
                f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
                params={
                    "marketplaceIds": self.marketplace_id,
                    "includedData": "summaries,fulfillmentAvailability,attributes",
                },
            )
            return result
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None  # SKU no existe en este marketplace
            raise

    async def update_listing_quantity(self, sku: str, quantity: int) -> dict:
        """
        Actualiza la cantidad disponible (stock) de un listing MFN.

        Proceso:
            1. Obtiene el listing para conocer su productType
            2. Hace PATCH con fulfillment_availability → quantity
            3. quantity=0 "apaga" el listing (out of stock)
            4. quantity>0 lo activa con ese stock

        IMPORTANTE:
            - Solo funciona para listings MFN (Fulfilled by Merchant)
            - Para FBA (Fulfilled by Amazon): el stock lo gestiona Amazon,
              para desactivar hay que crear una orden de remoción o cerrar el listing

        Args:
            sku:      SellerSKU exacto del listing
            quantity: Nueva cantidad disponible (0 = desactivar)

        Returns:
            Respuesta de la API con status del update
        """
        # Primero obtenemos el listing para saber el productType
        # (la API exige que coincida con el tipo real del producto)
        listing = await self.get_listing(sku)
        if not listing:
            raise ValueError(f"SKU '{sku}' no encontrado en marketplace {self.marketplace_id}")

        product_type = listing.get("productType", "PRODUCT")

        # PATCH con el atributo de disponibilidad
        # fulfillment_channel_code "DEFAULT" = MFN (el vendedor envía)
        # fulfillment_channel_code "AMAZON_NA" = FBA (Amazon envía)
        body = {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/fulfillment_availability",
                    "value": [
                        {
                            "fulfillment_channel_code": "DEFAULT",
                            "quantity": quantity,
                        }
                    ],
                }
            ],
        }

        return await self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={"marketplaceIds": self.marketplace_id},
            json_body=body,
        )

    async def update_listing_fulfillment(
        self,
        sku: str,
        action: str,          # "set_qty_zero" | "set_merchant" | "set_qty" | "reactivate_fba"
        quantity: int = 0,
    ) -> dict:
        """
        Gestiona el fulfillment y stock de cualquier listing (FBA, FBM, FLX).

        Acciones:
          set_qty_zero   → DEFAULT, qty=0  — pone stock en 0 (listing activo, sin stock)
          pause          → alias de set_qty_zero (compatibilidad)
          set_merchant   → DEFAULT, qty=N  — convierte a FBM con ese stock
          set_qty        → DEFAULT, qty=N  — actualiza qty (solo FBM existente)
          reactivate_fba → AMAZON_NA       — devuelve a FBA (Amazon maneja stock)

        Nota SP-API:
          - fulfillment_channel_code "DEFAULT"   = MFN/FBM (vendedor envía)
          - fulfillment_channel_code "AMAZON_NA" = FBA/AFN (Amazon envía, sin qty)
          - Para FBA el campo quantity se ignora; el stock lo controla Amazon
          - NUNCA pausar listings — siempre qty=0 para dejar de vender
        """
        # Normalizar alias
        if action == "pause":
            action = "set_qty_zero"

        listing = await self.get_listing(sku)
        if not listing:
            raise ValueError(f"SKU '{sku}' no encontrado en {self.marketplace_id}")

        product_type = listing.get("productType", "PRODUCT")

        if action == "reactivate_fba":
            fav = [{"fulfillment_channel_code": "AMAZON_NA"}]
        else:
            qty = quantity if action in ("set_merchant", "set_qty") else 0
            fav = [{"fulfillment_channel_code": "DEFAULT", "quantity": qty}]

        body = {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/fulfillment_availability",
                    "value": fav,
                }
            ],
        }

        return await self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={"marketplaceIds": self.marketplace_id},
            json_body=body,
        )

    async def update_listing_price(self, sku: str, price: float, currency: str = "MXN") -> dict:
        """
        Actualiza el precio de un listing.

        Args:
            sku:      SellerSKU exacto
            price:    Precio nuevo en la moneda especificada
            currency: Código ISO (MXN, USD, CAD)

        Returns:
            Respuesta de la API con status del update
        """
        listing = await self.get_listing(sku)
        if not listing:
            raise ValueError(f"SKU '{sku}' no encontrado en marketplace {self.marketplace_id}")

        product_type = listing.get("productType", "PRODUCT")

        body = {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/purchasable_offer",
                    "value": [
                        {
                            "currency": currency,
                            "our_price": [{"schedule": [{"value_with_tax": price}]}],
                            "marketplace_id": self.marketplace_id,
                        }
                    ],
                }
            ],
        }

        return await self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={"marketplaceIds": self.marketplace_id},
            json_body=body,
        )

    async def update_listing_title(self, sku: str, title: str) -> dict:
        """
        Actualiza el título (item_name) de un listing via Listings Items API PATCH.

        Args:
            sku:   SellerSKU exacto
            title: Nuevo título del producto (máx. 200 caracteres)

        Returns:
            Respuesta de la API con status del update
        """
        listing = await self.get_listing(sku)
        if not listing:
            raise ValueError(f"SKU '{sku}' no encontrado en marketplace {self.marketplace_id}")

        product_type = listing.get("productType", "PRODUCT")

        body = {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/item_name",
                    "value": [
                        {
                            "value": title[:200],
                            "marketplace_id": self.marketplace_id,
                            "language_tag": "es_MX",
                        }
                    ],
                }
            ],
        }

        return await self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={"marketplaceIds": self.marketplace_id},
            json_body=body,
        )

    async def update_listing_bullets(self, sku: str, bullets: list) -> dict:
        """
        Actualiza los bullet points (características) de un listing.

        Args:
            sku:     SellerSKU exacto
            bullets: Lista de hasta 5 strings (cada uno máx. 500 chars)
        """
        listing = await self.get_listing(sku)
        if not listing:
            raise ValueError(f"SKU '{sku}' no encontrado en marketplace {self.marketplace_id}")

        product_type = listing.get("productType", "PRODUCT")
        cleaned = [str(b).strip()[:500] for b in (bullets or []) if str(b).strip()][:5]

        body = {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/bullet_point",
                    "value": [
                        {
                            "value": bp,
                            "marketplace_id": self.marketplace_id,
                            "language_tag": "es_MX",
                        }
                        for bp in cleaned
                    ],
                }
            ],
        }

        return await self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={"marketplaceIds": self.marketplace_id},
            json_body=body,
        )

    async def update_listing_description(self, sku: str, description: str) -> dict:
        """
        Actualiza la descripción del producto de un listing.

        Args:
            sku:         SellerSKU exacto
            description: Descripción nueva (máx. 2000 chars)
        """
        listing = await self.get_listing(sku)
        if not listing:
            raise ValueError(f"SKU '{sku}' no encontrado en marketplace {self.marketplace_id}")

        product_type = listing.get("productType", "PRODUCT")

        body = {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/product_description",
                    "value": [
                        {
                            "value": description.strip()[:2000],
                            "marketplace_id": self.marketplace_id,
                            "language_tag": "es_MX",
                        }
                    ],
                }
            ],
        }

        return await self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={"marketplaceIds": self.marketplace_id},
            json_body=body,
        )

    # ─────────────────────────────────────────────────────────────────────
    # INVENTARIO FBA
    # ─────────────────────────────────────────────────────────────────────

    async def get_fba_inventory(self, skus: list = None) -> list:
        """
        Obtiene resumen de inventario FBA (Fulfilled by Amazon).

        Args:
            skus: Lista opcional de SellerSKUs para filtrar.
                  Si es None, devuelve todo el inventario FBA.

        Returns:
            Lista de summaries con campos:
            - sellerSku: SKU del vendedor
            - asin: ASIN de Amazon
            - inventoryDetails.fulfillableQuantity: disponible para venta
            - inventoryDetails.pendingOrdersQuantity: en órdenes pendientes
            - inventoryDetails.reservedQuantity: reservado

        Nota: Solo aplica a productos en FBA.
        Para MFN, usar get_listing(sku) → fulfillmentAvailability.
        """
        params = {
            "granularityType": "Marketplace",
            "granularityId": self.marketplace_id,
            "marketplaceIds": self.marketplace_id,
        }
        if skus:
            # Amazon acepta hasta 50 SKUs por llamada
            params["sellerSkus"] = ",".join(skus[:50])

        result = await self._request("GET", "/fba/inventory/v1/summaries", params=params)
        return result.get("payload", {}).get("inventorySummaries", [])

    # ─────────────────────────────────────────────────────────────────────
    # MÉTRICAS DEL DÍA (para dashboard)
    # ─────────────────────────────────────────────────────────────────────

    async def get_today_orders(self) -> list:
        """
        Obtiene las órdenes de hoy (desde medianoche UTC).

        Usado por el dashboard para mostrar ventas del día actual,
        equivalente a daily-sales de MeLi.

        Returns:
            Lista de órdenes de hoy con sus montos y estados.
        """
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        created_after = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        return await self.get_orders(created_after)

    async def get_revenue_today(self) -> float:
        """
        Calcula el revenue total del día (órdenes en Shipped/Unshipped).

        Returns:
            Total en la moneda del marketplace (MXN para México).

        Nota: Amazon incluye el precio con impuestos en OrderTotal.
        Para revenue neto habría que restar comisiones (15% típico).
        """
        orders = await self.get_today_orders()
        total = 0.0
        for order in orders:
            if order.get("OrderStatus") in ("Cancelled", "Pending"):
                continue
            order_total = order.get("OrderTotal", {})
            amount = float(order_total.get("Amount", 0))
            total += amount
        return total

    # ─────────────────────────────────────────────────────────────────────
    # CATÁLOGO — Todos los listings del vendedor
    # ─────────────────────────────────────────────────────────────────────

    async def get_all_listings(
        self,
        included_data: list = None,
        page_size: int = 20,
    ) -> list:
        """
        Obtiene TODOS los listings del vendedor con paginación automática.

        Usa el endpoint searchListingsItems de la Listings Items API v2021-08-01.
        Retorna todos los SKUs activos, pausados y suprimidos del marketplace.

        Args:
            included_data: Campos a incluir. Default: summaries, offers,
                           fulfillmentAvailability, issues.
            page_size: Items por página (máx 20 en Amazon).

        Returns:
            Lista de listings con: sku, summaries, offers, fulfillmentAvailability

        Rate limit: 5 req/s — seguro llamar con pageSize=20 y sleep 0.2s entre páginas.
        """
        if included_data is None:
            included_data = ["summaries", "attributes", "offers", "fulfillmentAvailability", "issues"]

        all_items = []
        page_token = None
        # Cap bajo intencionado: para catálogos grandes usar get_merchant_listings_report()
        max_pages = 50  # 50 páginas × 20 ítems = 1000 listings máx

        for page_num in range(max_pages):
            params: list = [
                ("marketplaceIds", self.marketplace_id),
                ("includedData", ",".join(included_data)),
                ("pageSize", str(page_size)),
            ]
            if page_token:
                params.append(("pageToken", page_token))

            try:
                result = await self._request(
                    "GET",
                    f"/listings/2021-08-01/items/{self.seller_id}",
                    params=params,
                )
            except Exception as e:
                logger.error(
                    f"[Amazon] Error en searchListingsItems página {page_num + 1}: {e}",
                    exc_info=True,
                )
                break

            items = result.get("items", [])
            all_items.extend(items)

            page_token = result.get("pagination", {}).get("nextToken")
            if not page_token:
                break

            await asyncio.sleep(0.2)  # Rate limit: 5 req/s
        else:
            # Loop completó max_pages sin agotar nextToken → catálogo truncado
            logger.warning(
                f"[Amazon] get_all_listings: límite {max_pages} páginas alcanzado para {self.seller_id}. "
                f"Catálogo truncado en ~{len(all_items)} ítems. "
                f"Usar get_merchant_listings_report() para catálogos grandes."
            )

        # Deduplicar por SKU (Amazon puede repetir items entre páginas)
        seen: dict = {}
        for item in all_items:
            sku = item.get("sku", "")
            if sku and sku not in seen:
                seen[sku] = item
        logger.info(
            f"[Amazon] get_all_listings: {len(seen)} SKUs únicos para {self.seller_id}"
        )
        return list(seen.values()) if seen else all_items

    async def get_fba_inventory_all(self) -> list:
        """
        Obtiene TODO el inventario FBA con paginación.

        Retorna todas las SKUs que tienen o tuvieron inventario en Amazon FBA,
        con breakdown detallado: disponible, reservado, dañado, en camino.

        Campos clave del inventoryDetails:
          - fulfillableQuantity: disponible para vender
          - reservedQuantity: en órdenes pendientes
          - unfulfillableQuantity: dañado/defectuoso
          - inboundWorkingQuantity + inboundShippedQuantity: envíos en camino

        Rate limit: 2 req/s — pausar 0.5s entre páginas.
        """
        all_summaries = []
        next_token = None
        max_pages = 100

        for _ in range(max_pages):
            params: list = [
                ("granularityType", "Marketplace"),
                ("granularityId", self.marketplace_id),
                ("marketplaceIds", self.marketplace_id),
                ("details", "true"),
            ]
            if next_token:
                params.append(("nextToken", next_token))

            try:
                result = await self._request(
                    "GET", "/fba/inventory/v1/summaries", params=params
                )
            except Exception as e:
                logger.warning(f"[Amazon] Error en FBA inventory: {e}")
                break

            summaries = result.get("payload", {}).get("inventorySummaries", [])
            all_summaries.extend(summaries)

            next_token = result.get("payload", {}).get("nextToken")
            if not next_token:
                break

            await asyncio.sleep(0.5)  # Rate limit: 2 req/s

        return all_summaries

    # ─────────────────────────────────────────────────────────────────────
    # REPORTS API — para obtener inventario Onsite (Seller Flex)
    # El FBA Inventory API solo cubre almacenes físicos de Amazon.
    # El Reports API genera un reporte "FBA Managed Inventory" que sí
    # incluye el stock de Amazon Onsite (inventario en bodega del vendedor).
    # ─────────────────────────────────────────────────────────────────────

    async def create_inventory_report(self) -> str:
        """
        Crea el reporte GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA.
        Incluye afn-fulfillable-quantity para todos los SKUs FBA/Onsite.
        Retorna el reportId.
        Rate limit: 0.0222 req/s (1 por 45 seg) — solo llamar en cache-miss.
        """
        body = {
            "reportType": "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
            "marketplaceIds": [self.marketplace_id],
        }
        result = await self._request("POST", "/reports/2021-06-30/reports", json_body=body)
        return result.get("reportId", "")

    async def get_report_status(self, report_id: str) -> dict:
        """
        Consulta el estado de un reporte.
        processingStatus: IN_QUEUE | IN_PROGRESS | DONE | FATAL | CANCELLED
        Cuando DONE incluye reportDocumentId.
        """
        return await self._request("GET", f"/reports/2021-06-30/reports/{report_id}")

    async def get_report_document_url(self, document_id: str) -> dict:
        """
        Obtiene la URL de descarga (pre-signed S3) del documento del reporte.
        Retorna dict con: url, compressionAlgorithm (opcional, "GZIP" si comprimido).
        """
        return await self._request("GET", f"/reports/2021-06-30/documents/{document_id}")

    async def download_report_document(self, url: str, compressed: bool) -> str:
        """
        Descarga el contenido del reporte desde la URL pre-signed de S3.
        IMPORTANTE: No usar headers de auth SP-API — la URL ya tiene auth embebida.
        """
        import gzip as _gzip
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            if compressed:
                return _gzip.decompress(resp.content).decode("utf-8", errors="replace")
            return resp.text

    async def get_onsite_inventory_report(self, max_wait_secs: int = 120) -> dict:
        """
        Genera y descarga el reporte FBA MYI completo.
        Retorna {seller_sku: afn_fulfillable_quantity} para todos los SKUs.

        El reporte GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA incluye:
        - Inventario FBA regular (en almacenes Amazon)
        - Inventario Amazon Onsite / Seller Flex (en bodega del vendedor)
        - Campo clave: afn-fulfillable-quantity

        Tiempo típico de generación: 30-90 segundos.
        """
        import csv, io as _io

        logger.info("[Amazon Reports] Creando reporte FBA MYI Inventory…")
        report_id = await self.create_inventory_report()
        if not report_id:
            raise ValueError("Amazon no devolvió reportId")

        # Pollinar hasta que esté listo
        wait_interval = 10  # segundos entre polls
        attempts = max(1, max_wait_secs // wait_interval)

        for attempt in range(attempts):
            await asyncio.sleep(wait_interval)
            status_data = await self.get_report_status(report_id)
            proc_status = status_data.get("processingStatus", "")
            logger.debug(f"[Amazon Reports] {report_id} → {proc_status} (intento {attempt+1}/{attempts})")

            if proc_status == "DONE":
                doc_id = status_data.get("reportDocumentId", "")
                if not doc_id:
                    raise ValueError("DONE pero sin reportDocumentId")
                doc_info = await self.get_report_document_url(doc_id)
                url = doc_info.get("url", "")
                compressed = doc_info.get("compressionAlgorithm", "") == "GZIP"
                content = await self.download_report_document(url, compressed)

                # Parsear TSV → {sku: {"avail": qty, "reserved": res}}
                result = {}
                reader = csv.DictReader(_io.StringIO(content), delimiter="\t")
                fieldnames = reader.fieldnames or []
                logger.info(f"[Amazon Reports] Columnas del TSV: {fieldnames}")

                for row in reader:
                    # Intentar varios nombres de columna SKU (varía por marketplace)
                    sku = (
                        row.get("sku")
                        or row.get("seller-sku")
                        or row.get("merchant-sku")
                        or ""
                    ).strip()
                    # afn-fulfillable-quantity = disponible para cumplir órdenes
                    qty_raw = (
                        row.get("afn-fulfillable-quantity")
                        or row.get("afn-warehouse-quantity")
                        or "0"
                    ) or "0"
                    res_raw = (row.get("afn-reserved-quantity") or "0") or "0"
                    try:
                        qty = int(float(qty_raw.strip()))
                        res = int(float(res_raw.strip()))
                    except (ValueError, TypeError):
                        qty = 0
                        res = 0
                    if sku:
                        result[sku] = {"avail": qty, "reserved": res}

                flx_count = sum(1 for k in result if "-FLX" in k.upper())
                logger.info(
                    f"[Amazon Reports] Reporte listo: {len(result)} SKUs totales, "
                    f"{flx_count} FLX — FBA MYI (avail+reserved)"
                )
                # Log muestra de SKUs FLX para diagnóstico
                sample_flx = {k: v for k, v in result.items() if "-FLX" in k.upper()}
                if sample_flx:
                    logger.info(f"[Amazon Reports] Muestra FLX: {dict(list(sample_flx.items())[:5])}")
                else:
                    logger.warning("[Amazon Reports] NO se encontraron SKUs -FLX en el reporte. "
                                   "Verificar si Seller Flex está incluido en GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA")
                return result

            elif proc_status in ("FATAL", "CANCELLED"):
                raise RuntimeError(f"Reporte {report_id} terminó con estado {proc_status}")

        # Timeout
        logger.warning(f"[Amazon Reports] Timeout esperando reporte {report_id} ({max_wait_secs}s)")
        return {}

    async def get_returns_report(self, date_from: str, date_to: str, max_wait_secs: int = 180) -> list:
        """
        Reporte GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA — devoluciones de inventario
        FBA con razón y COMENTARIO REAL DEL CLIENTE (columna customer-comments).

        NOTA HISTÓRICA (2026-07-21): este método pedía antes
        GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE por error — ese reporte trae
        columnas Title Case distintas (Order ID, Merchant SKU, Return Reason...)
        y NO tiene columna de comentario del cliente en absoluto. El parseo de
        abajo (row.get("sku"), row.get("customer-comments"), etc.) nunca hacía
        match contra esas columnas, así que este método SIEMPRE devolvía 0 items
        — confirmado en vivo contra cuenta real (VECKTOR): 0 filas con el reporte
        viejo, 59 filas con comentarios reales de clientes con el reporte correcto.
        Verificado que las columnas de GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA
        SÍ calzan exactamente con el parseo existente (return-date, order-id, sku,
        asin, product-name, quantity, detailed-disposition, reason,
        customer-comments) — no hace falta tocar el parseo, solo el reportType.

        IMPORTANTE — solo cubre FBA:
            Ventas MFN (envío propio, fulfillment-channel="DEFAULT" en amazon_listings)
            NO aparecen en este reporte — Amazon no expone reason/comentario para MFN
            sin Seller Fulfilled Prime. No hay fotos del cliente en ningún caso — no
            existe ese campo en ningún reporte de Amazon (a diferencia de ML).

        Args:
            date_from, date_to: "YYYY-MM-DD". Amazon limita a 60 días por reporte —
                                 si el rango es más amplio, pedir varios reportes.

        Returns:
            Lista de dicts: {order_id, sku, asin, product_name, return_date, reason,
                              customer_comments, disposition, quantity}
        """
        import csv, io as _io

        created_after = f"{date_from}T00:00:00Z"
        created_before = f"{date_to}T23:59:59Z"
        logger.info(f"[Amazon Reports] Creando reporte de devoluciones FBA {date_from}→{date_to}…")
        body = {
            "reportType": "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA",
            "marketplaceIds": [self.marketplace_id],
            "dataStartTime": created_after,
            "dataEndTime": created_before,
        }
        result = await self._request("POST", "/reports/2021-06-30/reports", json_body=body)
        report_id = result.get("reportId", "")
        if not report_id:
            raise ValueError(f"Amazon no devolvió reportId: {result}")

        wait_interval = 15
        attempts = max(4, max_wait_secs // wait_interval)

        for attempt in range(attempts):
            await asyncio.sleep(wait_interval)
            status_data = await self.get_report_status(report_id)
            proc_status = status_data.get("processingStatus", "")
            logger.debug(f"[Amazon Reports] returns {report_id} → {proc_status} (intento {attempt+1}/{attempts})")

            if proc_status == "DONE":
                doc_id = status_data.get("reportDocumentId", "")
                if not doc_id:
                    raise ValueError("DONE pero sin reportDocumentId")
                doc_info = await self.get_report_document_url(doc_id)
                url = doc_info.get("url", "")
                compressed = doc_info.get("compressionAlgorithm", "") == "GZIP"
                content = await self.download_report_document(url, compressed)

                items = []
                reader = csv.DictReader(_io.StringIO(content), delimiter="\t")
                logger.info(f"[Amazon Reports] Columnas returns TSV: {reader.fieldnames}")
                for row in reader:
                    sku = (row.get("sku") or row.get("merchant-sku") or "").strip()
                    if not sku:
                        continue
                    try:
                        qty = int(float(row.get("quantity") or 1))
                    except (ValueError, TypeError):
                        qty = 1
                    items.append({
                        "order_id": (row.get("order-id") or "").strip(),
                        "sku": sku,
                        "asin": (row.get("asin") or "").strip(),
                        "product_name": (row.get("product-name") or "").strip(),
                        "return_date": (row.get("return-date") or "")[:10],
                        "reason": (row.get("reason") or "").strip(),
                        "customer_comments": (row.get("customer-comments") or "").strip(),
                        "disposition": (row.get("detailed-disposition") or row.get("status") or "").strip(),
                        "quantity": qty,
                    })
                logger.info(f"[Amazon Reports] {len(items)} devoluciones FBA para {self.seller_id}")
                return items

            elif proc_status in ("FATAL", "CANCELLED"):
                raise RuntimeError(f"Reporte {report_id} terminó con estado {proc_status}")

        logger.warning(f"[Amazon Reports] Timeout esperando reporte de devoluciones {report_id} ({max_wait_secs}s)")
        return []

    async def get_merchant_listings_report(self, max_wait_secs: int = 300) -> list:
        """
        Obtiene TODOS los listings del vendedor via Reports API.

        Usa GET_MERCHANT_LISTINGS_ALL_DATA que incluye listings activos, inactivos
        y suprimidos — funciona para catálogos de cualquier tamaño (ej. 156K SKUs).

        Retorna lista de dicts: {sku, asin, status, channel}
        status: "Active" | "Inactive"
        channel: "DEFAULT" (merchant) | "AMAZON_NA" (FBA)

        Rate limit del report create: 0.0167 req/s (1 por minuto) — solo usar en cache-miss.
        """
        import csv, io as _io

        logger.info("[Amazon Reports] Creando reporte GET_MERCHANT_LISTINGS_ALL_DATA…")
        body = {
            "reportType": "GET_MERCHANT_LISTINGS_ALL_DATA",
            "marketplaceIds": [self.marketplace_id],
        }
        result = await self._request("POST", "/reports/2021-06-30/reports", json_body=body)
        report_id = result.get("reportId", "")
        if not report_id:
            raise ValueError(f"Amazon no devolvió reportId: {result}")

        attempts = max(6, max_wait_secs // 30)
        wait_interval = 30

        for attempt in range(attempts):
            await asyncio.sleep(wait_interval)
            status_data = await self.get_report_status(report_id)
            proc_status = status_data.get("processingStatus", "")
            logger.debug(
                f"[Amazon Reports] {report_id} → {proc_status} (intento {attempt + 1}/{attempts})"
            )

            if proc_status == "DONE":
                doc_id = status_data.get("reportDocumentId", "")
                if not doc_id:
                    raise ValueError("DONE pero sin reportDocumentId")
                doc_info = await self.get_report_document_url(doc_id)
                url = doc_info.get("url", "")
                compressed = doc_info.get("compressionAlgorithm", "") == "GZIP"
                content = await self.download_report_document(url, compressed)

                items = []
                reader = csv.DictReader(_io.StringIO(content), delimiter="\t")
                for row in reader:
                    sku = (
                        row.get("seller-sku")
                        or row.get("Seller SKU")
                        or row.get("item-id")
                        or ""
                    ).strip()
                    if not sku:
                        continue
                    asin = (
                        row.get("asin1") or row.get("ASIN") or row.get("asin") or ""
                    ).strip()
                    status = (
                        row.get("status") or row.get("Status") or ""
                    ).strip()
                    channel = (
                        row.get("fulfillment-channel")
                        or row.get("Fulfillment Channel")
                        or "DEFAULT"
                    ).strip()
                    title = (
                        row.get("item-name") or row.get("Item Name") or ""
                    ).strip()[:200]
                    try:
                        price = float(row.get("price") or row.get("Price") or 0)
                    except (ValueError, TypeError):
                        price = 0.0
                    try:
                        quantity = int(row.get("quantity") or row.get("Quantity") or 0)
                    except (ValueError, TypeError):
                        quantity = 0
                    items.append({
                        "sku": sku,
                        "asin": asin,
                        "status": status,
                        "channel": channel,
                        "title": title,
                        "price": price,
                        "quantity": quantity,
                    })

                logger.info(
                    f"[Amazon Reports] GET_MERCHANT_LISTINGS_ALL_DATA: {len(items)} listings para {self.seller_id}"
                )
                return items

            elif proc_status in ("FATAL", "CANCELLED"):
                raise RuntimeError(
                    f"Reporte {report_id} terminó con estado {proc_status}: "
                    f"{status_data.get('processingStatus', '')}"
                )

        logger.warning(
            f"[Amazon Reports] Timeout ({max_wait_secs}s) esperando reporte {report_id}"
        )
        return []

    async def get_listing_item(self, sku: str) -> Optional[dict]:
        """
        Verifica si un SKU específico existe como listing en Amazon.

        Usa GET /listings/{sellerId}/{sku} — endpoint directo que no requiere
        permisos especiales de búsqueda. Retorna dict si existe, None si no (404).
        Re-lanza cualquier error que NO sea 404 para que el caller pueda dar
        "benefit of doubt" (no marcar como gap si hay error de red/auth/rate-limit).

        Útil para confirmar si un BM SKU (o variante -FBA) está lanzado en Amazon,
        incluso si está out of stock o inactivo.

        Rate limit: 5 req/s.
        """
        try:
            result = await self._request(
                "GET",
                f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
                params=[
                    ("marketplaceIds", self.marketplace_id),
                    ("includedData", "summaries"),
                ],
            )
            return result
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "NOT_FOUND" in err_str.upper():
                return None
            # Re-lanzar errores NO 404 (403, 429, red, etc.) para que el caller
            # trate el SKU como "beneficio de la duda" (no confirmar como gap)
            logger.warning(f"[Amazon] get_listing_item({sku}) error no-404 marketplace={self.marketplace_id}: {err_str[:200]}")
            raise

    async def get_catalog_item(self, asin: str) -> Optional[dict]:
        """
        Obtiene datos del catálogo Amazon para un ASIN específico.

        Incluye: imágenes oficiales, BSR (Best Seller Rank), marca, modelo,
        dimensiones y clasificación en categorías de Browse.

        BSR alto = buena posición en su categoría.
        Múltiples imágenes mejoran la conversión si se usan en el listing.

        Rate limit: 2 req/s — usar solo para ASINs top.
        """
        try:
            result = await self._request(
                "GET",
                f"/catalog/2022-04-01/items/{asin}",
                params=[
                    ("marketplaceIds", self.marketplace_id),
                    ("includedData", "summaries,images,salesRanks"),
                ],
            )
            return result
        except Exception as e:
            logger.warning(f"[Amazon] Error obteniendo catalog para ASIN {asin}: {e}")
            return None

    async def search_catalog(self, keyword: str = "", identifiers: Optional[list] = None) -> list:
        """
        Busca productos en el catálogo Amazon por UPC/EAN o keyword.

        Flujo 1 (ASIN match): pasar identifiers=[upc] para encontrar el ASIN existente.
        Flujo 2 (producto nuevo): pasar keyword=título para verificar si ya existe.

        Rate limit: 2 req/s — Catalog Items API.
        """
        try:
            params: list = [
                ("marketplaceIds", self.marketplace_id),
                ("includedData", "summaries,images"),
            ]
            if identifiers:
                for id_ in identifiers:
                    params.append(("identifiers", id_))
                params.append(("identifiersType", "UPC"))
            else:
                params.append(("keywords", keyword[:200]))
                params.append(("pageSize", "10"))
            result = await self._request("GET", "/catalog/2022-04-01/items", params=params)
            items = result.get("items", []) if isinstance(result, dict) else []
            parsed = []
            for item in items[:10]:
                summaries = item.get("summaries", [{}])
                s = summaries[0] if summaries else {}
                img_url = ""
                for img_set in item.get("images", []):
                    for img in img_set.get("images", []):
                        if img.get("variant") == "MAIN":
                            img_url = img.get("link", "")
                            break
                    if img_url:
                        break
                parsed.append({
                    "asin":         item.get("asin", ""),
                    "title":        s.get("itemName", ""),
                    "brand":        s.get("brand", ""),
                    "product_type": s.get("productType", "PRODUCT"),
                    "image_url":    img_url,
                })
            return parsed
        except Exception as e:
            logger.warning(f"[Amazon] Error buscando catálogo '{keyword}': {e}")
            return []

    async def create_listing_full(
        self,
        sku: str,
        product_type: str,
        attributes: dict,
        requirements: str = "LISTING_OFFER_ONLY",
    ) -> dict:
        """
        Crea o actualiza un listing completo via Listings Items API (PUT).

        requirements:
          - "LISTING_OFFER_ONLY" → agrega oferta a ASIN existente (Flujo 1)
          - "LISTING"            → crea producto nuevo (Flujo 2, Amazon asigna ASIN)

        Respuesta incluye 'status' ('ACCEPTED'/'INVALID') e 'issues' si hay errores.
        El ASIN asignado llega en identifiers[marketplace_id].asin cuando status=ACCEPTED.
        """
        body = {
            "productType": product_type,
            "requirements": requirements,
            "attributes": attributes,
        }
        return await self._request(
            "PUT",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={
                "marketplaceIds": self.marketplace_id,
                "issueLocale": "es_MX",
            },
            json_body=body,
        )

    async def patch_listing_attributes(
        self,
        sku: str,
        product_type: str,
        attr_patches: dict,
    ) -> dict:
        """PATCH only specific attributes on an existing listing.
        attr_patches: {attr_name: [SP-API formatted value list]}
        Uses JSON Patch (RFC 6902) via Amazon Listings Items PATCH.
        """
        patches = [
            {"op": "replace", "path": f"/attributes/{attr_name}", "value": value}
            for attr_name, value in attr_patches.items()
        ]
        body = {
            "productType": product_type,
            "patches": patches,
        }
        return await self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={
                "marketplaceIds": self.marketplace_id,
                "issueLocale": "es_MX",
            },
            json_body=body,
        )

    async def fetch_product_type_schema(self, product_type: str) -> dict:
        """
        Fetches the attribute schema for a product type from Amazon Definitions API.
        Makes two calls: ENFORCED (required attrs) and NOT_ENFORCED (all attrs).
        Returns simplified dict: {required, optional, groups, group_titles}.
        """
        import asyncio as _aio

        async def _fetch(enforced: bool) -> dict:
            try:
                return await self._request(
                    "GET",
                    f"/definitions/2020-09-01/productTypes/{product_type}",
                    params={
                        "marketplaceIds": self.marketplace_id,
                        "requirements": "LISTING",
                        "requirementsEnforced": "ENFORCED" if enforced else "NOT_ENFORCED",
                        "locale": "en_US",
                    },
                )
            except Exception as e:
                logger.warning(f"[Amazon] schema fetch error ({product_type}, enforced={enforced}): {e}")
                return {}

        enforced_resp, all_resp = await _aio.gather(_fetch(True), _fetch(False))

        def _extract_props(resp: dict) -> tuple:
            groups = {}
            titles = {}
            props  = set()
            for gk, gv in (resp.get("propertyGroups") or {}).items():
                pnames = gv.get("propertyNames") or []
                groups[gk] = pnames
                titles[gk] = gv.get("title", gk)
                props.update(pnames)
            return groups, titles, props

        req_groups, req_titles, req_props = _extract_props(enforced_resp)
        all_groups, all_titles, all_props = _extract_props(all_resp)

        # Merge group info: use all_groups as base (more complete), mark required
        merged_groups = all_groups if all_groups else req_groups
        merged_titles = all_titles if all_titles else req_titles

        optional_props = all_props - req_props
        result = {
            "product_type": product_type,
            "marketplace_id": self.marketplace_id,
            "required": sorted(req_props),
            "optional": sorted(optional_props),
            "all": sorted(all_props),
            "groups": merged_groups,
            "group_titles": merged_titles,
        }
        logger.info(f"[Amazon] Schema for {product_type}: {len(req_props)} required, {len(optional_props)} optional")
        return result

    async def close_listing(self, sku: str) -> dict:
        """Set listing quantity to 0 (closes without deleting the SKU)."""
        try:
            return await self._request(
                "PATCH",
                f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
                params={"marketplaceIds": self.marketplace_id},
                json_body={
                    "productType": "PRODUCT",
                    "requirements": "LISTING_OFFER_ONLY",
                    "attributes": {
                        "fulfillment_availability": [{
                            "fulfillment_channel_code": "DEFAULT",
                            "quantity": 0,
                        }]
                    },
                },
            )
        except Exception as e:
            logger.warning(f"[Amazon] close_listing error for {sku}: {e}")
            return {"error": str(e)}

    async def delete_listing(self, sku: str) -> dict:
        """Permanently delete a listing (SKU) from Amazon Seller Central."""
        try:
            return await self._request(
                "DELETE",
                f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
                params={"marketplaceIds": self.marketplace_id},
            )
        except Exception as e:
            logger.warning(f"[Amazon] delete_listing error for {sku}: {e}")
            return {"error": str(e)}

    async def get_listing_status(self, sku: str) -> dict:
        """
        Gets current status of a listing from Amazon SP-API.
        Returns dict with: status (BUYABLE/DISCOVERABLE/DELETED/INCOMPLETE), asin, issues list.
        """
        try:
            result = await self._request(
                "GET",
                f"/listings/2021-08-01/items/{self.seller_id}/{sku}",
                params={
                    "marketplaceIds": self.marketplace_id,
                    "includedData": "summaries,issues",
                    "issueLocale": "en_US",
                },
            )
            summaries = result.get("summaries") or []
            issues    = result.get("issues") or []
            status = "pending"
            asin   = None
            if summaries:
                s = summaries[0]
                status = s.get("status", "pending")
                asin   = s.get("asin")
            return {"status": status, "asin": asin, "issues": issues, "raw": result}
        except Exception as e:
            logger.warning(f"[Amazon] get_listing_status error for {sku}: {e}")
            return {"status": "error", "asin": None, "issues": [], "error": str(e)}

    async def fetch_product_types(self) -> list:
        """
        Fetches all valid product type names for this marketplace from SP-API Definitions.
        Returns sorted list of strings, e.g. ["COMPUTER_MONITOR", "TELEVISION", "VACUUM", ...]
        """
        try:
            result = await self._request(
                "GET",
                "/definitions/2020-09-01/productTypes",
                params={"marketplaceIds": self.marketplace_id},
            )
            items = result.get("productTypes") or []
            types = sorted({pt["name"] for pt in items if pt.get("name")})
            logger.info(f"[Amazon] Fetched {len(types)} product types for {self.marketplace_id}")
            return types
        except Exception as e:
            logger.warning(f"[Amazon] fetch_product_types failed: {e}")
            return []

    async def get_listing_offers(self, sku: str) -> Optional[dict]:
        """
        Obtiene las ofertas competitivas de un listing propio (por SellerSKU).

        Retorna información del Buy Box: quién lo tiene, a qué precio, cuántos
        competidores hay, y si el propio seller tiene el Buy Box.

        El Buy Box es crítico en Amazon: ~90% de las ventas van al winner.
        Si perdemos el Buy Box, perdemos la mayoría de ventas aunque tengamos stock.

        Campos clave de la respuesta:
          - Summary.BuyBoxPrices: precio actual del Buy Box
          - Offers[].IsBuyBoxWinner: si este seller tiene el Buy Box
          - Summary.TotalOfferCount: cuántos competidores hay
          - ListPrice: precio de lista "tachado" en la página

        Rate limit: 1 req/s — pausar 1.1s entre llamadas.
        """
        try:
            result = await self._request(
                "GET",
                f"/products/pricing/v0/listings/{sku}/offers",
                params=[
                    ("MarketplaceId", self.marketplace_id),
                    ("ItemCondition", "New"),
                ],
            )
            return result
        except Exception as e:
            logger.warning(f"[Amazon] Error obteniendo offers para SKU {sku}: {e}")
            return None

    async def get_order_metrics(
        self,
        date_from: str,
        date_to_exclusive: str,
        granularity: str = "Total",
        tz: str = "US/Pacific",
        asin: str = None,
        sku: str = None,
    ) -> list:
        """
        Obtiene métricas de ventas usando el Sales API v1 (/sales/v1/orderMetrics).

        Retorna totalSales (OPS — igual a Seller Central), unitCount y orderCount
        por intervalo. Mucho más preciso que sumar OrderTotal de Orders API porque:
          - OrderTotal NO está disponible para órdenes Pending
          - OrderTotal incluye shipping/taxes, no es Ordered Product Sales (OPS)
          - totalSales = exactamente lo que muestra Amazon Seller Central

        Args:
            date_from:         "YYYY-MM-DD" — inicio del rango (PST, inclusive)
            date_to_exclusive: "YYYY-MM-DD" — fin del rango (PST, EXCLUSIVO)
            granularity:       "Total" | "Day" | "Week" | "Month"
            tz:                Zona horaria para granularity != Total.
                               Usar "US/Pacific" para coincidir con Amazon SC.
            asin:              Opcional — filtrar por ASIN específico.
            sku:               Opcional — filtrar por SKU específico.

        Returns:
            Lista de dicts con campos:
              - interval:         "2026-02-24T00:00:00-08:00--2026-02-25T00:00:00-08:00"
              - orderCount:       número de órdenes
              - unitCount:        unidades vendidas
              - averageUnitPrice: {currencyCode, amount}
              - totalSales:       {currencyCode, amount}  ← OPS, igual a SC

        Rate limit: 0.5 req/s, burst 15 — mucho más generoso que Orders API.
        """
        # Formato de intervalo: {inicio}--{fin} con doble guión (separador ISO 8601)
        # US/Pacific observa DST: PST=UTC-8 (nov-mar), PDT=UTC-7 (mar-nov)
        # Usar el offset correcto según la fecha para alinear con Seller Central.
        try:
            import zoneinfo
            _tz = zoneinfo.ZoneInfo("America/Los_Angeles")
            _ref = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=_tz)
            _offset_h = int(_ref.utcoffset().total_seconds() // 3600)
            _offset_str = f"{_offset_h:+03d}:00"  # e.g. "-07:00" or "-08:00"
        except Exception:
            _offset_str = "-08:00"  # fallback seguro
        interval = f"{date_from}T00:00:00{_offset_str}--{date_to_exclusive}T00:00:00{_offset_str}"

        params: list = [
            ("marketplaceIds", self.marketplace_id),
            ("interval", interval),
            ("granularity", granularity),
        ]
        if granularity != "Total":
            params.append(("granularityTimeZone", tz))
        if asin:
            params.append(("asin", asin.strip().upper()))
        if sku:
            params.append(("sku", sku.strip()))

        async with _SALES_SEMAPHORE:
            result = await self._request("GET", "/sales/v1/orderMetrics", params=params)

        return result.get("payload", [])

    async def get_catalog_item(self, asin: str) -> dict:
        """
        Obtiene info del producto desde Catalog Items API v2022-04-01.
        Retorna title, brand, images, category, dimensions, attributes, salesRanks (BSR).
        """
        try:
            result = await self._request(
                "GET",
                f"/catalog/2022-04-01/items/{asin.strip().upper()}",
                params=[
                    ("marketplaceIds", self.marketplace_id),
                    ("includedData", "summaries,images,attributes,dimensions,identifiers,salesRanks"),
                ],
            )
            return result
        except Exception as e:
            logger.warning(f"[AMZ-CATALOG] {asin}: {e}")
            return {}

    async def get_item_offers(self, asin: str, item_condition: str = "New") -> dict:
        """
        Obtiene ofertas competitivas para un ASIN via Product Pricing API.
        Retorna buy box price, lista de vendedores, FBA/FBM, Prime, feedback.
        Rate limit: 0.5 req/s, burst 1.
        """
        try:
            result = await self._request(
                "GET",
                f"/products/pricing/v0/items/{asin.strip().upper()}/offers",
                params={
                    "MarketplaceId": self.marketplace_id,
                    "ItemCondition": item_condition,
                },
            )
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.warning(f"[AMZ-OFFERS] {asin}: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────
    # FINANZAS — BALANCE DE CUENTA
    # ─────────────────────────────────────────────────────────────────────

    async def get_account_balance(self) -> dict:
        """
        Obtiene los fondos pendientes de disbursement de Amazon.

        Usa la Finances API v0 para obtener eventos financieros recientes
        y calcular el balance estimado pendiente de pago.

        Returns:
            Dict con:
              - pending_amount: monto estimado pendiente (suma de ShipmentEvents)
              - currency: moneda (MXN, USD, etc.)
              - error: mensaje si falla (opcional)
        """
        try:
            # Obtener eventos financieros del último período activo (30 días)
            posted_after = (datetime.utcnow() - timedelta(days=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            result = await self._request(
                "GET",
                "/finances/v0/financialEvents",
                params={
                    "PostedAfter": posted_after,
                    "MaxResultsPerPage": 100,
                },
            )
            payload = result.get("payload", {}).get("FinancialEvents", {})

            # Sumar ShipmentEvents (ventas) — principal fuente de ingresos
            total = 0.0
            currency = "MXN"
            for event in payload.get("ShipmentEventList", []):
                for item in event.get("ShipmentItemList", []):
                    for charge in item.get("ItemChargeList", []):
                        amt = charge.get("ChargeAmount", {})
                        amount = float(amt.get("CurrencyAmount", 0) or 0)
                        total += amount
                        if amt.get("CurrencyCode"):
                            currency = amt["CurrencyCode"]
                    # Restar fees
                    for fee in item.get("ItemFeeList", []):
                        amt = fee.get("FeeAmount", {})
                        total -= abs(float(amt.get("CurrencyAmount", 0) or 0))
                    # Restar promociones/descuentos del vendedor
                    for promo in item.get("PromotionList", []):
                        amt = promo.get("PromotionAmount", {})
                        total -= abs(float(amt.get("CurrencyAmount", 0) or 0))

            return {
                "pending_amount": round(total, 2),
                "currency": currency,
                "source": "finances_api_30d",
            }
        except Exception as e:
            return {"pending_amount": None, "currency": "MXN", "error": str(e)}

    async def get_buy_box_status(self, asin: str, marketplace_id: str) -> dict:
        """¿Somos el Buy Box winner de este ASIN? Product Pricing API,
        GET /products/pricing/v0/items/{asin}/offers. Confirmado contra
        producción 2026-07-23: payload.Offers[] trae SellerId + IsBuyBoxWinner
        por oferta, payload.Summary.TotalOfferCount = competidores totales."""
        try:
            resp = await self._request(
                "GET", f"/products/pricing/v0/items/{asin}/offers",
                params={"MarketplaceId": marketplace_id, "ItemCondition": "New"},
            )
        except Exception:
            return {"is_winner": None, "total_competitors": 0, "buy_box_price": None}
        payload = resp.get("payload", {}) or {}
        offers = payload.get("Offers", []) or []
        total = payload.get("Summary", {}).get("TotalOfferCount", len(offers))
        buy_box_prices = payload.get("Summary", {}).get("BuyBoxPrices", []) or []
        buy_box_price = buy_box_prices[0].get("ListingPrice", {}).get("Amount") if buy_box_prices else None
        if total <= 1:
            return {"is_winner": True, "total_competitors": total, "buy_box_price": buy_box_price}
        our_offer = next((o for o in offers if o.get("SellerId") == self.seller_id), None)
        is_winner = bool(our_offer.get("IsBuyBoxWinner")) if our_offer else None
        return {"is_winner": is_winner, "total_competitors": total, "buy_box_price": buy_box_price}

    async def get_refunds_30d(self) -> dict:
        """
        Obtiene devoluciones / reembolsos de los últimos 30 días.
        Endpoint: GET /finances/v0/financialEvents → RefundEventList
        Returns: {count, total_amount, currency, rate_pct (if sales provided)}
        """
        try:
            posted_after = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            result = await self._request(
                "GET", "/finances/v0/financialEvents",
                params={"PostedAfter": posted_after, "MaxResultsPerPage": 100},
            )
            payload  = result.get("payload", {}).get("FinancialEvents", {})
            refunds  = payload.get("RefundEventList", [])
            count    = 0
            total    = 0.0
            currency = "MXN"
            for event in refunds:
                for item in event.get("ShipmentItemAdjustmentList", []):
                    count += 1
                    for charge in item.get("ItemChargeAdjustmentList", []):
                        amt = charge.get("ChargeAmount", {})
                        total += abs(float(amt.get("CurrencyAmount", 0) or 0))
                        if amt.get("CurrencyCode"):
                            currency = amt["CurrencyCode"]
            return {"count": count, "total": round(total, 2), "currency": currency}
        except Exception as e:
            logger.warning(f"[Amazon] get_refunds_30d error: {e}")
            return {"count": 0, "total": 0, "currency": "MXN", "error": str(e)}

    async def get_refunds_detail(self, days: int = 30) -> list:
        """
        Devoluciones detalladas por SKU para los últimos N días.
        Returns: list of {sku, order_id, posted_date, qty, amount, currency, reason}
        """
        try:
            posted_after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            items_out = []
            next_token = None
            pages = 0
            while pages < 5:
                params: dict = {"PostedAfter": posted_after, "MaxResultsPerPage": 100}
                if next_token:
                    params["NextToken"] = next_token
                result = await self._request("GET", "/finances/v0/financialEvents", params=params)
                payload = result.get("payload", {}).get("FinancialEvents", {})
                for event in payload.get("RefundEventList", []):
                    order_id    = event.get("AmazonOrderId", "")
                    posted_date = event.get("PostedDate", "")[:10]
                    for item in event.get("ShipmentItemAdjustmentList", []):
                        sku     = item.get("SellerSKU", "")
                        qty     = int(item.get("QuantityShipped") or 0)
                        amount  = 0.0
                        currency = "MXN"
                        for charge in item.get("ItemChargeAdjustmentList", []):
                            amt = charge.get("ChargeAmount", {})
                            amount += abs(float(amt.get("CurrencyAmount", 0) or 0))
                            if amt.get("CurrencyCode"):
                                currency = amt["CurrencyCode"]
                        items_out.append({
                            "sku":         sku,
                            "order_id":    order_id,
                            "posted_date": posted_date,
                            "qty":         qty,
                            "amount":      round(amount, 2),
                            "currency":    currency,
                        })
                next_token = result.get("payload", {}).get("NextToken")
                pages += 1
                if not next_token:
                    break
            return items_out
        except Exception as e:
            logger.warning(f"[Amazon] get_refunds_detail error: {e}")
            return []

    async def get_financial_event_groups(self, max_results: int = 10) -> list:
        """
        Retorna los grupos de liquidación (períodos de pago) más recientes.
        Cada grupo representa un ciclo de liquidación de Amazon.
        Endpoint: GET /finances/v0/financialEventGroups
        """
        try:
            params = {
                "MaxResultsPerPage": max_results,
                "FinancialEventGroupStartedAfter": (
                    datetime.utcnow() - timedelta(days=180)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            data = await self._request(
                "GET", "/finances/v0/financialEventGroups", params=params
            )
            groups = (
                data.get("payload", {})
                    .get("FinancialEventGroupList", [])
            )
            result = []
            for g in groups:
                original = g.get("OriginalTotal") or {}
                converted = g.get("ConvertedTotal") or {}
                result.append({
                    "group_id": g.get("FinancialEventGroupId", ""),
                    "status": g.get("ProcessingStatus", ""),
                    "fund_transfer_date": g.get("FundTransferDate", ""),
                    "original_total": float(original.get("Amount") or 0),
                    "converted_total": float(converted.get("Amount") or 0),
                    "currency": converted.get("CurrencyCode") or original.get("CurrencyCode") or "MXN",
                    "beginning_balance": float((g.get("BeginningBalance") or {}).get("Amount") or 0),
                    "account_tail": g.get("AccountTail", ""),
                })
            return result
        except Exception as e:
            logger.warning(f"[Amazon] get_financial_event_groups error: {e}")
            return []

    async def fetch_orders_range(
        self,
        date_from: str,
        date_to: str,
        statuses: list = None,
    ) -> list:
        """
        Obtiene TODAS las órdenes de un rango de fechas (paginación incluida).

        Args:
            date_from: "YYYY-MM-DD" — inicio del rango (inclusive)
            date_to:   "YYYY-MM-DD" — fin del rango (inclusive, hasta las 23:59:59)
            statuses:  Lista de OrderStatuses a filtrar. Default: Shipped+Unshipped+PartiallyShipped.
                       Para Pending usar statuses=["Pending"] en llamada separada (SP-API quirk).
        """
        # Convertir a ISO 8601 con zona UTC (SP-API lo requiere)
        created_after = f"{date_from}T00:00:00Z"

        # Amazon exige que CreatedBefore sea al menos 2 min antes de "ahora".
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        if date_to >= today_str:
            created_before = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            created_before = f"{date_to}T23:59:59Z"

        return await self.get_orders(
            created_after=created_after,
            created_before=created_before,
            order_statuses=statuses,  # None = default Shipped+Unshipped+PartiallyShipped
        )

    async def get_deals(self, status: str = None, deal_type: str = None) -> list:
        """
        Obtiene Lightning Deals y Best Deals disponibles para el seller.
        Endpoint: GET /deals/v2024-11-19/deals
        Rate limit: 1 req/s, burst 5.
        Si el seller no tiene deals → retorna [] con HTTP 200 (comportamiento normal).
        Si la cuenta no tiene acceso al programa de Deals → 403, retorna [].
        """
        params = [("marketplaceIds", self.marketplace_id)]
        if status:
            params.append(("status", status))
        if deal_type:
            params.append(("dealType", deal_type))

        all_deals = []
        next_token = None

        for _ in range(10):  # máx 10 páginas
            page_params = list(params)
            if next_token:
                page_params.append(("nextToken", next_token))
            try:
                result = await self._request(
                    "GET", "/deals/v2024-11-19/deals",
                    params=page_params, timeout=20,
                )
            except Exception as e:
                err_str = str(e)
                if "403" in err_str or "ACCESS_DENIED" in err_str or "404" in err_str:
                    logger.warning(f"[Amazon Deals] No acceso a Deals API: {err_str[:100]}")
                    return []
                raise

            all_deals.extend(result.get("deals", []))
            next_token = (result.get("pagination") or {}).get("nextToken")
            if not next_token:
                break
            await asyncio.sleep(1.0)

        return all_deals

    async def get_competitive_price(self, asin: str) -> dict:
        """
        Obtiene el precio del Buy Box y número de competidores para un ASIN.
        Endpoint: GET /products/pricing/v0/price
        Rate limit: 1 req/s — llamar con asyncio.sleep(1.1) entre requests.
        """
        try:
            data = await self._request(
                "GET", "/products/pricing/v0/price",
                params=[
                    ("MarketplaceId", self.marketplace_id),
                    ("ItemType", "Asin"),
                    ("Asins", asin),
                ],
                timeout=15,
            )
            payload = (data.get("payload") or [])
            if not payload:
                return {}
            item = payload[0] if isinstance(payload, list) else payload
            comp = (item.get("Product") or {}).get("CompetitivePricing") or {}
            prices = comp.get("CompetitivePrices") or []
            bb_price = None
            for p in prices:
                if p.get("CompetitivePriceId") == "1":  # 1 = Buy Box
                    bb_price = float(
                        (p.get("Price") or {}).get("ListingPrice", {}).get("Amount") or 0
                    ) or None
                    break
            offers_list = comp.get("NumberOfOfferListings") or []
            num_new = sum(
                o.get("Count", 0) for o in offers_list
                if str(o.get("condition", "")).lower() in ("new", "1")
            )
            return {"buybox_price": bb_price, "num_offers": num_new}
        except Exception as e:
            logger.warning(f"[Amazon CompPrice] Error para ASIN {asin}: {e}")
            return {}


# ─────────────────────────────────────────────────────────────────────────────
# FACTORY — equivalente a get_meli_client() de meli_client.py
# ─────────────────────────────────────────────────────────────────────────────

async def get_amazon_client(seller_id: str = None) -> Optional[AmazonClient]:
    """
    Obtiene una instancia de AmazonClient para el seller especificado.

    Si seller_id es None, usa el primer seller configurado en DB.

    Proceso:
        1. Busca la cuenta en amazon_accounts DB
        2. Verifica que tenga refresh_token (ya completó el OAuth)
        3. Crea e instancia AmazonClient con las credenciales

    Returns:
        AmazonClient listo para hacer llamadas, o None si no hay cuenta configurada.

    Ejemplo:
        client = await get_amazon_client()  # usa cuenta default
        orders = await client.get_today_orders()
    """
    from app.services import token_store

    if seller_id:
        account = await token_store.get_amazon_account(seller_id)
    else:
        # Usar el primer account disponible.
        # IMPORTANTE: get_all_amazon_accounts() no incluye refresh_token por seguridad,
        # por eso usamos get_all_amazon_accounts() solo para obtener el seller_id
        # y luego get_amazon_account() para traer las credenciales completas.
        accounts = await token_store.get_all_amazon_accounts()
        if accounts:
            account = await token_store.get_amazon_account(accounts[0]["seller_id"])
        else:
            account = None

    if not account:
        logger.warning("[Amazon] No hay cuentas Amazon configuradas en DB")
        return None

    if not account.get("refresh_token"):
        logger.warning(f"[Amazon] Cuenta {account['seller_id']} sin refresh_token — completar OAuth primero")
        return None

    return AmazonClient(
        seller_id=account["seller_id"],
        client_id=account["client_id"],
        client_secret=account["client_secret"],
        refresh_token=account["refresh_token"],
        marketplace_id=account.get("marketplace_id", "A1AM78C64UM0Y8"),
        nickname=account.get("nickname", ""),
        marketplace_name=account.get("marketplace_name", "MX"),
    )


async def _seed_amazon_accounts():
    """
    Siembra cuentas Amazon leyendo .env.production directamente (igual que _seed_tokens de MeLi).
    Esto garantiza que el refresh_token completo del archivo siempre se use,
    sin depender de variables de Railway que pueden quedar truncadas o desactualizadas.
    """
    from pathlib import Path as _Path
    from app.config import (
        AMAZON_CLIENT_ID, AMAZON_CLIENT_SECRET, AMAZON_SELLER_ID,
        AMAZON_REFRESH_TOKEN, AMAZON_MARKETPLACE_ID, AMAZON_MARKETPLACE_NAME,
        AMAZON_APP_SOLUTION_ID, AMAZON_NICKNAME,
    )
    from app.services import token_store

    # Leer .env.production directamente (igual que _seed_tokens para MeLi)
    file_vars: dict = {}
    env_file = _Path(__file__).resolve().parent.parent.parent / ".env.production"
    logger.info(f"[Amazon seed] Buscando .env.production en: {env_file} | existe: {env_file.exists()}")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                file_vars[k.strip()] = v.strip()
        logger.info(f"[Amazon seed] Variables leídas del archivo: {list(file_vars.keys())}")

    # Railway env vars tienen prioridad; fallback a archivo .env.production
    def _g(key, default=""):
        import os as _os
        return _os.getenv(key) or file_vars.get(key) or default

    seller_id  = _g("AMAZON_SELLER_ID",       AMAZON_SELLER_ID)
    client_id  = _g("AMAZON_CLIENT_ID",        AMAZON_CLIENT_ID)
    client_sec = _g("AMAZON_CLIENT_SECRET",    AMAZON_CLIENT_SECRET)
    mkt_id     = _g("AMAZON_MARKETPLACE_ID",   AMAZON_MARKETPLACE_ID)
    mkt_name   = _g("AMAZON_MARKETPLACE_NAME", AMAZON_MARKETPLACE_NAME)
    app_sol_id = _g("AMAZON_APP_SOLUTION_ID",  AMAZON_APP_SOLUTION_ID)
    nickname   = _g("AMAZON_NICKNAME",         AMAZON_NICKNAME) or "VECKTOR IMPORTS"

    # Para refresh_token: limpiar espacios/newlines del env var (puede estar corrupto).
    # Prioridad: archivo (limpio, del git) sobre env var (puede tener \n embebidos).
    _rt_file = file_vars.get("AMAZON_REFRESH_TOKEN", "").strip()
    _rt_env  = (AMAZON_REFRESH_TOKEN or "").strip().replace("\n", "").replace("\r", "").replace(" ", "")
    # Usar el del archivo si existe y es válido, sino el env var limpio
    refresh_rt = _rt_file or _rt_env or ""

    rt_preview = (refresh_rt or "")[:30] + "..." if refresh_rt else "VACIO"
    logger.info(f"[Amazon seed] seller={seller_id} | client_id={client_id[:20] if client_id else 'VACIO'}... | token={rt_preview}")

    if not seller_id or not client_id or not refresh_rt:
        logger.warning("[Amazon] Credenciales Amazon incompletas — skip seed")
        return

    # Si la cuenta ya existe en DB con un refresh_token válido, preservarlo.
    # Esto evita sobreescribir el token recién obtenido por OAuth en cada startup.
    existing = await token_store.get_amazon_account(seller_id)
    if existing and existing.get("refresh_token", "").startswith("Atzr|"):
        # Cuenta con token válido — actualizar solo credenciales (no el token)
        await token_store.save_amazon_account(
            seller_id=seller_id,
            nickname=nickname,
            client_id=client_id,
            client_secret=client_sec,
            refresh_token="",  # cadena vacía = preservar token existente en DB
            marketplace_id=mkt_id,
            marketplace_name=mkt_name,
            app_solution_id=app_sol_id,
        )
        logger.info(f"[Amazon] Cuenta actualizada (token OAuth preservado): {seller_id}")
    else:
        # Sin cuenta o sin token — sembrar con token del archivo/env
        await token_store.save_amazon_account(
            seller_id=seller_id,
            nickname=nickname,
            client_id=client_id,
            client_secret=client_sec,
            refresh_token=refresh_rt,
            marketplace_id=mkt_id,
            marketplace_name=mkt_name,
            app_solution_id=app_sol_id,
        )
        logger.info(f"[Amazon] Cuenta sembrada: {seller_id} ({nickname})")

    # ── Segunda cuenta (AMAZON2_*) ──────────────────────────────────────────
    seller_id2  = _g("AMAZON2_SELLER_ID",       "")
    client_id2  = _g("AMAZON2_CLIENT_ID",        "")
    client_sec2 = _g("AMAZON2_CLIENT_SECRET",    "")
    mkt_id2     = _g("AMAZON2_MARKETPLACE_ID",   "A1AM78C64UM0Y8")
    mkt_name2   = _g("AMAZON2_MARKETPLACE_NAME", "MX")
    app_sol_id2 = _g("AMAZON2_APP_SOLUTION_ID",  "")
    nickname2   = _g("AMAZON2_NICKNAME",         "")
    _rt2_file   = file_vars.get("AMAZON2_REFRESH_TOKEN", "").strip()
    import os as _os2
    _rt2_env    = (_os2.getenv("AMAZON2_REFRESH_TOKEN") or "").strip().replace("\n","").replace("\r","").replace(" ","")
    refresh_rt2 = _rt2_file or _rt2_env or ""

    if seller_id2 and refresh_rt2:
        existing2 = await token_store.get_amazon_account(seller_id2)
        stored_rt2 = (existing2 or {}).get("refresh_token", "")
        # AMAZON2 siempre usa el env var como fuente de verdad.
        # El env var se actualiza manualmente cuando se re-autoriza la app con nuevos permisos.
        # Si el env var tiene un token DIFERENTE al almacenado → actualizar (nueva autorización).
        # Si son iguales → preservar (no overwrite innecesario).
        token_to_save2 = refresh_rt2 if refresh_rt2 != stored_rt2 else ""
        # IMPORTANTE: el refresh_token de AUTOBOT fue obtenido via OAuth con VeKtorClaude app
        # (AMAZON_CLIENT_ID/SECRET). Usar AMAZON2_CLIENT_ID/SECRET causaría 400 en LWA.
        # Siempre usamos las credenciales de la app que generó el token (cuenta 1).
        _lwa_client_id  = client_id  or client_id2
        _lwa_client_sec = client_sec or client_sec2
        await token_store.save_amazon_account(
            seller_id=seller_id2, nickname=nickname2,
            client_id=_lwa_client_id, client_secret=_lwa_client_sec,
            refresh_token=token_to_save2,
            marketplace_id=mkt_id2, marketplace_name=mkt_name2,
            app_solution_id=app_sol_id2,
        )
        if token_to_save2:
            logger.info(f"[Amazon] Cuenta2 token actualizado (nuevo env var): {seller_id2}")
        else:
            logger.info(f"[Amazon] Cuenta2 actualizada (token sin cambios): {seller_id2}")
    else:
        logger.info("[Amazon] AMAZON2_* no configurado — skip segunda cuenta")

    # ── Tercera cuenta (AMAZON3_* — ExclusiveBulbs USA) ───────────────────────
    seller_id3  = _g("AMAZON3_SELLER_ID",       "")
    client_id3  = _g("AMAZON3_CLIENT_ID",        "")
    client_sec3 = _g("AMAZON3_CLIENT_SECRET",    "")
    mkt_id3     = _g("AMAZON3_MARKETPLACE_ID",   "ATVPDKIKX0DER")
    mkt_name3   = _g("AMAZON3_MARKETPLACE_NAME", "US")
    app_sol_id3 = _g("AMAZON3_APP_SOLUTION_ID",  "")
    nickname3   = _g("AMAZON3_NICKNAME",         "ExclusiveBulbs")
    _rt3_file   = file_vars.get("AMAZON3_REFRESH_TOKEN", "").strip()
    import os as _os3
    _rt3_env    = (_os3.getenv("AMAZON3_REFRESH_TOKEN") or "").strip().replace("\n","").replace("\r","").replace(" ","")
    refresh_rt3 = _rt3_file or _rt3_env or ""

    if seller_id3 and refresh_rt3:
        existing3  = await token_store.get_amazon_account(seller_id3)
        stored_rt3 = (existing3 or {}).get("refresh_token", "")
        token_to_save3 = refresh_rt3 if refresh_rt3 != stored_rt3 else ""
        await token_store.save_amazon_account(
            seller_id=seller_id3, nickname=nickname3,
            client_id=client_id3, client_secret=client_sec3,
            refresh_token=token_to_save3,
            marketplace_id=mkt_id3, marketplace_name=mkt_name3,
            app_solution_id=app_sol_id3,
        )
        if token_to_save3:
            logger.info(f"[Amazon] Cuenta3 token actualizado: {seller_id3} ({nickname3})")
        else:
            logger.info(f"[Amazon] Cuenta3 actualizada (token sin cambios): {seller_id3}")
    else:
        logger.info("[Amazon] AMAZON3_* no configurado — skip tercera cuenta")
