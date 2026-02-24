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

            resp.raise_for_status()
            return resp.json()

    # ─────────────────────────────────────────────────────────────────────
    # ÓRDENES
    # ─────────────────────────────────────────────────────────────────────

    async def get_orders(
        self,
        created_after: str,
        created_before: str = None,
        marketplace_ids: list = None,
    ) -> list:
        """
        Obtiene órdenes del marketplace.

        Args:
            created_after:  Fecha ISO 8601 (ej. "2026-01-01T00:00:00Z")
            created_before: Fecha ISO 8601 opcional (default: ahora)
            marketplace_ids: Lista de IDs de marketplace (default: el de la instancia)

        Returns:
            Lista de órdenes con campos: AmazonOrderId, OrderStatus,
            PurchaseDate, OrderTotal, NumberOfItemsShipped, etc.

        Notas:
            - Paginación automática via NextToken
            - Solo incluye órdenes en estado Shipped/Unshipped/PartiallyShipped
            - Pending y Cancelled se excluyen del conteo de ventas
        """
        if marketplace_ids is None:
            marketplace_ids = [self.marketplace_id]

        params = {
            "MarketplaceIds": ",".join(marketplace_ids),
            "CreatedAfter": created_after,
            "OrderStatuses": "Shipped,Unshipped,PartiallyShipped,InvoiceUnconfirmed",
        }
        if created_before:
            params["CreatedBefore"] = created_before

        async with _ORDERS_SEMAPHORE:
            result = await self._request("GET", "/orders/v0/orders", params=params)

        orders = result.get("payload", {}).get("Orders", [])

        # Paginación: Amazon devuelve NextToken cuando hay más resultados
        next_token = result.get("payload", {}).get("NextToken")
        while next_token:
            async with _ORDERS_SEMAPHORE:
                next_result = await self._request(
                    "GET",
                    "/orders/v0/orders",
                    params={
                        "NextToken": next_token,
                        "MarketplaceIds": ",".join(marketplace_ids),
                    },
                )
            orders.extend(next_result.get("payload", {}).get("Orders", []))
            next_token = next_result.get("payload", {}).get("NextToken")

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

    async def fetch_orders_range(
        self,
        date_from: str,
        date_to: str,
    ) -> list:
        """
        Obtiene TODAS las órdenes de un rango de fechas (paginación incluida).

        Equivalente a fetch_all_orders() de meli_client.py pero para Amazon.

        Args:
            date_from: "YYYY-MM-DD" — inicio del rango (inclusive)
            date_to:   "YYYY-MM-DD" — fin del rango (inclusive, hasta las 23:59:59)

        Returns:
            Lista completa de órdenes del período.

        Proceso:
            1. Convierte YYYY-MM-DD a ISO 8601 que exige SP-API
            2. Llama a get_orders con paginación interna
            3. Incluye todos los estados (Shipped, Unshipped, Delivered, etc.)
        """
        # Convertir a ISO 8601 con zona UTC (SP-API lo requiere)
        created_after  = f"{date_from}T00:00:00Z"
        created_before = f"{date_to}T23:59:59Z"

        return await self.get_orders(
            created_after=created_after,
            created_before=created_before,
        )


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
        # Usar el primer account disponible (el default)
        accounts = await token_store.get_all_amazon_accounts()
        account = accounts[0] if accounts else None

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
    Siembra cuentas Amazon desde variables de entorno al arrancar el servidor.

    Equivalente a _seed_tokens() de meli_client.py para Railway:
    - Lee AMAZON_SELLER_ID, AMAZON_CLIENT_ID, etc. desde .env.production
    - Las guarda en amazon_accounts table si no existen
    - Permite que el server arranque con credenciales incluso si la DB está vacía

    Llamar desde main.py en el startup event, igual que _seed_tokens().
    """
    from app.config import (
        AMAZON_CLIENT_ID, AMAZON_CLIENT_SECRET, AMAZON_SELLER_ID,
        AMAZON_REFRESH_TOKEN, AMAZON_MARKETPLACE_ID, AMAZON_MARKETPLACE_NAME,
        AMAZON_APP_SOLUTION_ID, AMAZON_NICKNAME,
    )
    from app.services import token_store

    if not AMAZON_SELLER_ID or not AMAZON_CLIENT_ID:
        logger.debug("[Amazon] No hay credenciales Amazon en .env — skip seed")
        return

    existing = await token_store.get_amazon_account(AMAZON_SELLER_ID)

    # Solo hacer upsert si hay datos nuevos (no sobreescribir refresh_token real con vacío)
    if not existing or (AMAZON_REFRESH_TOKEN and not existing.get("refresh_token")):
        await token_store.save_amazon_account(
            seller_id=AMAZON_SELLER_ID,
            nickname=AMAZON_NICKNAME or "VECKTOR IMPORTS",
            client_id=AMAZON_CLIENT_ID,
            client_secret=AMAZON_CLIENT_SECRET,
            refresh_token=AMAZON_REFRESH_TOKEN,
            marketplace_id=AMAZON_MARKETPLACE_ID,
            marketplace_name=AMAZON_MARKETPLACE_NAME,
            app_solution_id=AMAZON_APP_SOLUTION_ID,
        )
        logger.info(f"[Amazon] Cuenta sembrada: {AMAZON_SELLER_ID} ({AMAZON_NICKNAME})")
