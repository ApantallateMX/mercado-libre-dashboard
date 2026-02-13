import asyncio
import time
import httpx
from typing import Optional
from app.config import MELI_API_URL, MELI_TOKEN_URL, MELI_CLIENT_ID, MELI_CLIENT_SECRET, MELI_REDIRECT_URI
from app.services import token_store


class MeliApiError(Exception):
    """Error from MeLi API with status code and body details."""
    def __init__(self, status_code: int, endpoint: str, body):
        self.status_code = status_code
        self.endpoint = endpoint
        self.body = body
        # Extract human-readable message from MeLi error body
        if isinstance(body, dict):
            # MeLi uses "error" for detail, "message" for code
            error_detail = body.get("error", "")
            message_code = body.get("message", "")
            cause = body.get("cause", [])
            if isinstance(cause, list) and cause:
                details = "; ".join(
                    c.get("message", c.get("code", str(c))) for c in cause[:3]
                )
                msg = error_detail or message_code
                msg = f"{msg} ({details})" if msg else details
            else:
                # Use error detail if available, otherwise message code
                msg = error_detail or message_code or str(body)
        else:
            msg = str(body)
        super().__init__(msg)


# Cache global en memoria: key -> (timestamp, data)
_orders_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 300  # 5 minutos


def _cache_key(date_from: str, date_to: str) -> str:
    return f"orders:{date_from or ''}:{date_to or ''}"


def _get_cached(key: str):
    entry = _orders_cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _set_cached(key: str, data):
    _orders_cache[key] = (time.time(), data)
    # Limpiar entradas viejas (max 50 keys)
    if len(_orders_cache) > 50:
        oldest = sorted(_orders_cache.items(), key=lambda x: x[1][0])
        for k, _ in oldest[:len(oldest) - 30]:
            _orders_cache.pop(k, None)


def _item_has_sku(item: dict, sku_upper: str) -> bool:
    """Check if SKU exists in item via seller_custom_field or SELLER_SKU attribute."""
    # Check item-level seller_custom_field
    if (item.get("seller_custom_field") or "").upper() == sku_upper:
        return True
    # Check item-level SELLER_SKU attribute
    for a in item.get("attributes", []):
        if a.get("id") == "SELLER_SKU" and (a.get("value_name") or "").upper() == sku_upper:
            return True
    # Check variation-level
    for v in item.get("variations", []):
        if (v.get("seller_custom_field") or "").upper() == sku_upper:
            return True
        for va in v.get("attributes", []):
            if va.get("id") == "SELLER_SKU" and (va.get("value_name") or "").upper() == sku_upper:
                return True
    return False


class MeliClient:
    """Cliente HTTP para la API de Mercado Libre."""

    def __init__(self, access_token: str, refresh_token: str, user_id: str):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id = user_id
        self._advertiser_id: str | None = None
        self._client = httpx.AsyncClient(
            base_url=MELI_API_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0
        )

    async def close(self):
        await self._client.aclose()

    async def _refresh_token_if_needed(self):
        """Refresca el token si ha expirado."""
        if await token_store.is_token_expired(self.user_id):
            await self._do_refresh_token()

    async def _do_refresh_token(self):
        """Ejecuta el refresh del token."""
        async with httpx.AsyncClient() as client:
            response = await client.post(MELI_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": MELI_CLIENT_ID,
                "client_secret": MELI_CLIENT_SECRET,
                "refresh_token": self.refresh_token
            })

            if response.status_code == 200:
                data = response.json()
                self.access_token = data["access_token"]
                self.refresh_token = data["refresh_token"]
                self._client.headers["Authorization"] = f"Bearer {self.access_token}"

                await token_store.save_tokens(
                    self.user_id,
                    data["access_token"],
                    data["refresh_token"],
                    data["expires_in"]
                )

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Realiza una peticion a la API con retry en rate limit."""
        await self._refresh_token_if_needed()
        max_retries = 3
        for attempt in range(max_retries):
            response = await self._client.request(method, endpoint, **kwargs)
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 2 * (attempt + 1)))
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 400:
                # Include MeLi error details in the exception
                try:
                    error_body = response.json()
                except Exception:
                    error_body = response.text
                raise MeliApiError(response.status_code, endpoint, error_body)
            text = response.text.strip()
            if not text:
                return {}
            return response.json()
        raise MeliApiError(response.status_code, endpoint, "Max retries exceeded")

    async def get(self, endpoint: str, **kwargs) -> dict:
        return await self._request("GET", endpoint, **kwargs)

    async def get_public(self, endpoint: str, **kwargs) -> dict:
        """GET sin auth token (para endpoints publicos como domain_discovery)."""
        async with httpx.AsyncClient(
            base_url=MELI_API_URL, timeout=15.0
        ) as pub:
            response = await pub.get(endpoint, **kwargs)
            response.raise_for_status()
            text = response.text.strip()
            if not text:
                return {}
            return response.json()

    async def post(self, endpoint: str, **kwargs) -> dict:
        return await self._request("POST", endpoint, **kwargs)

    async def put(self, endpoint: str, **kwargs) -> dict:
        return await self._request("PUT", endpoint, **kwargs)

    async def delete(self, endpoint: str, **kwargs) -> dict:
        return await self._request("DELETE", endpoint, **kwargs)

    # === User ===

    async def get_user_info(self) -> dict:
        """Obtiene informacion del usuario autenticado."""
        return await self.get("/users/me")

    # === Orders ===

    async def get_orders(self, offset: int = 0, limit: int = 20, sort: str = "date_desc",
                         date_from: str = None, date_to: str = None) -> dict:
        """Obtiene las ordenes del vendedor."""
        params = {
            "seller": self.user_id,
            "sort": sort,
            "offset": offset,
            "limit": limit
        }
        if date_from:
            params["order.date_created.from"] = f"{date_from}T00:00:00.000-00:00"
        if date_to:
            params["order.date_created.to"] = f"{date_to}T23:59:59.000-00:00"
        return await self.get("/orders/search", params=params)

    async def fetch_all_orders(self, date_from: str = None, date_to: str = None) -> list:
        """Pagina TODAS las ordenes con paginacion concurrente + cache."""
        key = _cache_key(date_from, date_to)
        cached = _get_cached(key)
        if cached is not None:
            return cached

        # Primera pagina para saber el total
        first_page = await self.get_orders(
            offset=0, limit=50,
            date_from=date_from, date_to=date_to
        )
        results = first_page.get("results", [])
        total = first_page.get("paging", {}).get("total", 0)

        if not results or total <= 50:
            _set_cached(key, results)
            return results

        # Paginas restantes en paralelo (max 5 concurrentes para no saturar rate limit)
        all_orders = list(results)
        remaining_offsets = list(range(50, total, 50))

        sem = asyncio.Semaphore(5)

        async def fetch_page(offset):
            async with sem:
                data = await self.get_orders(
                    offset=offset, limit=50,
                    date_from=date_from, date_to=date_to
                )
                return data.get("results", [])

        tasks = [fetch_page(off) for off in remaining_offsets]
        pages = await asyncio.gather(*tasks, return_exceptions=True)

        for page in pages:
            if isinstance(page, list):
                all_orders.extend(page)

        _set_cached(key, all_orders)
        return all_orders

    async def get_order(self, order_id: str) -> dict:
        """Obtiene el detalle de una orden."""
        return await self.get(f"/orders/{order_id}")

    # === Items ===

    async def get_items(self, offset: int = 0, limit: int = 50, status: str = "active") -> dict:
        """Obtiene los items del vendedor."""
        return await self.get(
            f"/users/{self.user_id}/items/search",
            params={
                "status": status,
                "offset": offset,
                "limit": limit
            }
        )

    async def get_item(self, item_id: str) -> dict:
        """Obtiene el detalle de un item."""
        return await self.get(f"/items/{item_id}")

    async def get_items_details(self, item_ids: list) -> list:
        """Obtiene detalles de multiples items."""
        if not item_ids:
            return []
        ids = ",".join(item_ids[:20])  # Max 20 items por request
        return await self.get("/items", params={"ids": ids})

    async def get_item_sale_price(self, item_id: str) -> dict | None:
        """Obtiene precio de venta real (con promocion si existe).
        Retorna dict con 'amount' y 'regular_amount', o None si falla."""
        try:
            data = await self.get(f"/items/{item_id}/sale_price")
            return data
        except Exception:
            return None

    # === Questions ===

    async def get_questions(self, status: str = "UNANSWERED", offset: int = 0, limit: int = 50,
                            date_from: str = None, date_to: str = None) -> dict:
        """Obtiene preguntas del vendedor."""
        params = {
            "seller_id": self.user_id,
            "status": status,
            "offset": offset,
            "limit": limit,
        }
        if date_from:
            params["from"] = f"{date_from}T00:00:00.000-00:00"
        if date_to:
            params["to"] = f"{date_to}T23:59:59.000-00:00"
        return await self.get("/questions/search", params=params)

    async def fetch_all_questions(self, status: str = "UNANSWERED",
                                  date_from: str = None, date_to: str = None) -> list:
        """Pagina TODAS las preguntas con paginacion concurrente."""
        first_page = await self.get_questions(
            status=status, offset=0, limit=50,
            date_from=date_from, date_to=date_to
        )
        results = first_page.get("questions", [])
        total = first_page.get("paging", {}).get("total", 0)

        if not results or total <= 50:
            return results

        all_questions = list(results)
        remaining_offsets = list(range(50, total, 50))
        sem = asyncio.Semaphore(5)

        async def fetch_page(offset):
            async with sem:
                data = await self.get_questions(
                    status=status, offset=offset, limit=50,
                    date_from=date_from, date_to=date_to
                )
                return data.get("questions", [])

        tasks = [fetch_page(off) for off in remaining_offsets]
        pages = await asyncio.gather(*tasks, return_exceptions=True)
        for page in pages:
            if isinstance(page, list):
                all_questions.extend(page)

        return all_questions

    async def get_buyer_questions(self, buyer_id: str, item_id: str = None) -> list:
        """Obtiene las ultimas preguntas de un comprador hacia este vendedor."""
        try:
            params = {
                "seller_id": self.user_id,
                "from": buyer_id,
                "limit": 10,
                "sort_fields": "date_created",
                "sort_types": "DESC",
            }
            if item_id:
                params["item"] = item_id
            data = await self.get("/questions/search", params=params)
            return data.get("questions", [])
        except Exception:
            return []

    # === Claims ===

    async def get_claims(self, offset: int = 0, limit: int = 50, status: str = None,
                         date_from: str = None, date_to: str = None) -> dict:
        """Obtiene reclamos del vendedor via /post-purchase/v1/claims/search."""
        has_dates = bool(date_from or date_to)
        params = {
            # v1 API requires offset >= 1 when date_created filter is present
            "offset": max(offset, 1) if has_dates else offset,
            "limit": limit,
            "site_id": "MLM",
        }
        if status:
            params["status"] = status
        # v1 date filter uses single "date_created" param with range: "from,to"
        if date_from and date_to:
            params["date_created"] = f"{date_from}T00:00:00.000-04:00,{date_to}T23:59:59.000-04:00"
        elif date_from:
            params["date_created"] = f"{date_from}T00:00:00.000-04:00,2099-12-31T23:59:59.000-04:00"
        elif date_to:
            params["date_created"] = f"2000-01-01T00:00:00.000-04:00,{date_to}T23:59:59.000-04:00"
        params["sort"] = "date_created:desc"
        raw = await self.get("/post-purchase/v1/claims/search", params=params)
        # Normalize: v1 returns "data" key, map to "results" for compatibility
        if "data" in raw and "results" not in raw:
            raw["results"] = raw.pop("data")
        return raw

    async def fetch_all_claims(self, status: str = None,
                               date_from: str = None, date_to: str = None) -> list:
        """Pagina TODOS los reclamos con paginacion concurrente."""
        first_page = await self.get_claims(
            offset=0, limit=50, status=status,
            date_from=date_from, date_to=date_to
        )
        results = first_page.get("results", [])
        total = first_page.get("paging", {}).get("total", 0)

        if not results or total <= 50:
            return results

        all_claims = list(results)
        remaining_offsets = list(range(50, total, 50))
        sem = asyncio.Semaphore(5)

        async def fetch_page(offset):
            async with sem:
                data = await self.get_claims(
                    offset=offset, limit=50, status=status,
                    date_from=date_from, date_to=date_to
                )
                return data.get("results", [])

        tasks = [fetch_page(off) for off in remaining_offsets]
        pages = await asyncio.gather(*tasks, return_exceptions=True)
        for page in pages:
            if isinstance(page, list):
                all_claims.extend(page)

        return all_claims

    # === Shipping ===

    async def get_shipment(self, shipment_id: str) -> dict:
        """Obtiene detalle de un envio."""
        return await self.get(f"/shipments/{shipment_id}")

    async def get_shipment_costs(self, shipment_id: str) -> float:
        """Obtiene el costo de envio para el vendedor desde /shipments/{id}/costs.
        Retorna senders[0].cost o 0 si no se puede obtener."""
        try:
            data = await self.get(f"/shipments/{shipment_id}/costs")
            senders = data.get("senders", [])
            if senders:
                return float(senders[0].get("cost", 0) or 0)
        except Exception:
            pass
        return 0.0

    async def enrich_orders_with_shipping(self, orders: list) -> list:
        """Enriquece una lista de ordenes con _shipping_cost y _iva_shipping.
        Procesa secuencialmente para evitar agotar file descriptors en Windows."""
        for order in orders:
            shipping = order.get("shipping", {})
            ship_id = shipping.get("id") if isinstance(shipping, dict) else None
            if not ship_id:
                continue
            try:
                cost = await self.get_shipment_costs(str(ship_id))
                order["_shipping_cost"] = cost
                order["_iva_shipping"] = round(cost * 0.16, 2)
            except Exception:
                pass
        return orders

    async def get_payment_net_amount(self, payment_id: str) -> float | None:
        """Obtiene net_received_amount de un pago desde /collections/{id}."""
        try:
            data = await self.get(f"/collections/{payment_id}")
            return data.get("net_received_amount")
        except Exception:
            return None

    async def enrich_orders_with_net_amount(self, orders: list) -> list:
        """Enriquece ordenes con _net_received_amount desde la API de collections.
        Usa concurrencia limitada para no saturar la API."""
        sem = asyncio.Semaphore(10)

        async def fetch_net(order):
            payments = order.get("payments", [])
            if not payments:
                return
            total_net = 0.0
            for payment in payments:
                payment_id = payment.get("id")
                if not payment_id:
                    continue
                async with sem:
                    net = await self.get_payment_net_amount(str(payment_id))
                    if net is not None:
                        total_net += net
            order["_net_received_amount"] = total_net

        tasks = [fetch_net(o) for o in orders if o.get("status") in ["paid", "delivered"]]
        await asyncio.gather(*tasks, return_exceptions=True)
        return orders

    # === Visits ===

    async def get_item_visits(self, item_id: str, date_from: str, date_to: str) -> dict:
        """Obtiene visitas de un item en un rango de fechas."""
        return await self.get(f"/items/{item_id}/visits/time_window", params={
            "last": "30",
            "unit": "day",
        })

    # === Search (competencia) ===

    async def search_items(self, query: str, category: str = None, limit: int = 10) -> dict:
        """Busca items en MeLi (para analisis de competencia)."""
        params = {"q": query, "limit": limit, "site_id": "MLM"}
        if category:
            params["category"] = category
        return await self.get("/sites/MLM/search", params=params)

    # === Categories & Fees ===

    async def get_category(self, category_id: str) -> dict:
        """Obtiene informacion de una categoria."""
        return await self.get(f"/categories/{category_id}")

    async def get_listing_fees(self, category_id: str, price: float, listing_type: str = "gold_special") -> dict:
        """Obtiene las comisiones de venta para una categoria y precio."""
        return await self.get(f"/sites/MLM/listing_prices", params={
            "category_id": category_id,
            "price": price,
            "listing_type_id": listing_type,
        })

    # === Advertising (Mercado Ads / Product Ads) ===

    async def _get_advertiser_id(self) -> str:
        """Obtiene el advertiser_id del usuario (diferente al user_id)."""
        if self._advertiser_id:
            return self._advertiser_id
        data = await self.get("/advertising/advertisers", params={"product_id": "PADS"})
        advertisers = data.get("advertisers", [])
        if advertisers:
            self._advertiser_id = str(advertisers[0].get("advertiser_id", ""))
        if not self._advertiser_id:
            self._advertiser_id = self.user_id
        return self._advertiser_id

    async def get_ads_advertiser(self) -> dict:
        """Verifica si el usuario tiene Product Ads habilitado."""
        return await self.get("/advertising/advertisers", params={"product_id": "PADS"})

    async def get_ads_campaigns(self, date_from: str = None, date_to: str = None) -> dict:
        """Obtiene campanas con metricas."""
        adv_id = await self._get_advertiser_id()
        params = {
            "metrics": "clicks,prints,cost,cpc,acos,units_quantity,total_amount",
            "metrics_summary": "true",
        }
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return await self.get(f"/advertising/advertisers/{adv_id}/product_ads/campaigns", params=params)

    async def get_ads_campaign_detail(self, campaign_id: str, date_from: str = None, date_to: str = None) -> dict:
        """Obtiene detalle de una campana con metricas."""
        params = {
            "metrics": "clicks,prints,cost,cpc,acos,units_quantity,total_amount",
        }
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return await self.get(f"/advertising/product_ads/campaigns/{campaign_id}", params=params)

    async def get_all_active_item_ids(self) -> list[str]:
        """Obtiene todos los item_ids activos del seller usando scroll (sin limite de offset)."""
        all_ids: list[str] = []
        # Primera pagina con search_type=scan para obtener scroll_id
        data = await self.get(
            f"/users/{self.user_id}/items/search",
            params={"status": "active", "limit": 100, "search_type": "scan"},
        )
        all_ids.extend(data.get("results", []))
        total = data.get("paging", {}).get("total", 0)
        scroll_id = data.get("scroll_id")

        while scroll_id and len(all_ids) < total:
            data = await self.get(
                f"/users/{self.user_id}/items/search",
                params={"status": "active", "limit": 100, "search_type": "scan",
                         "scroll_id": scroll_id},
            )
            results = data.get("results", [])
            if not results:
                break
            all_ids.extend(results)
            scroll_id = data.get("scroll_id")

        return all_ids

    async def update_campaign(self, campaign_id: str, status: str = None,
                              budget: float = None, acos_target: float = None) -> dict:
        """Actualiza una campaña de Product Ads (status, budget, acos_target)."""
        adv_id = await self._get_advertiser_id()
        payload = {}
        if status is not None:
            payload["status"] = status
        if budget is not None:
            payload["budget"] = budget
        if acos_target is not None:
            payload["acos_target"] = max(3, min(500, acos_target))
        return await self.put(
            f"/advertising/advertisers/{adv_id}/product_ads/campaigns/{campaign_id}",
            json=payload
        )

    async def create_campaign(self, name: str, budget: float,
                              acos_target: float = None, status: str = "active") -> dict:
        """Crea una nueva campaña de Product Ads."""
        adv_id = await self._get_advertiser_id()
        payload = {
            "name": name,
            "status": status,
            "budget": budget,
        }
        if acos_target is not None:
            payload["acos_target"] = max(3, min(500, acos_target))
        return await self.post(
            f"/advertising/advertisers/{adv_id}/product_ads/campaigns",
            json=payload
        )

    async def assign_items_to_campaign(self, item_ids: list[str], campaign_id: int) -> dict:
        """Asigna uno o mas items a una campana de Product Ads (API V2)."""
        # PUT por cada item usando el endpoint documentado de V2
        # Requiere que la app tenga permisos de escritura en Product Ads
        results = []
        errors = []
        for item_id in item_ids[:50]:
            try:
                resp = await self._request_raw(
                    "PUT",
                    f"/marketplace/advertising/MLM/product_ads/ads/{item_id}",
                    params={"channel": "marketplace"},
                    json={"status": "active", "campaign_id": campaign_id},
                    extra_headers={"api-version": "2"},
                )
                results.append({"item_id": item_id, "status": "ok"})
            except MeliApiError as e:
                errors.append({"item_id": item_id, "error": str(e)})
        if not results and errors:
            raise MeliApiError(401, "product_ads/ads", errors[0]["error"])
        return {"results": results, "errors": errors}

    async def _request_raw(self, method: str, endpoint: str, extra_headers: dict = None, **kwargs):
        """Request con headers adicionales (para api-version etc)."""
        await self._refresh_token_if_needed()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if extra_headers:
            headers.update(extra_headers)
        response = await self._client.request(method, endpoint, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text
            raise MeliApiError(response.status_code, endpoint, error_body)
        text = response.text.strip()
        if not text:
            return {}
        return response.json()

    async def get_ads_items(self, date_from: str = None, date_to: str = None) -> dict:
        """Obtiene metricas de ads a nivel de item/producto."""
        adv_id = await self._get_advertiser_id()
        params = {
            "metrics": "clicks,prints,cost,cpc,acos,units_quantity,total_amount",
            "metrics_summary": "true",
            "limit": 100,
        }
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return await self.get(f"/advertising/advertisers/{adv_id}/product_ads/items", params=params)

    async def get_all_ads_item_ids(self) -> set[str]:
        """Obtiene TODOS los item_ids que tienen ads activos (sin filtro de fechas).
        Pagina a traves de todos los resultados para no perder items sin metricas recientes."""
        adv_id = await self._get_advertiser_id()
        all_ids: set[str] = set()
        offset = 0
        limit = 100
        while True:
            params = {"limit": limit, "offset": offset}
            try:
                data = await self.get(
                    f"/advertising/advertisers/{adv_id}/product_ads/items",
                    params=params,
                )
            except Exception:
                break
            results = data.get("results", data if isinstance(data, list) else [])
            if not results:
                break
            for item in results:
                iid = item.get("item_id", item.get("id", ""))
                if iid:
                    all_ids.add(iid)
            paging = data.get("paging", {})
            total = paging.get("total", 0)
            offset += limit
            if offset >= total or len(results) < limit:
                break
        return all_ids

    # === Price & Stock ===

    async def update_item_price(self, item_id: str, price: float) -> dict:
        """Actualiza el precio de un item."""
        return await self.put(f"/items/{item_id}", json={"price": price})

    async def update_item_stock(self, item_id: str, quantity: int) -> dict:
        """Actualiza el stock de un item."""
        return await self.put(f"/items/{item_id}", json={"available_quantity": quantity})

    # === Item Updates (Optimizar Listados) ===

    async def update_item(self, item_id: str, updates: dict) -> dict:
        """Actualiza campos genericos de un item (PUT /items/{id})."""
        return await self.put(f"/items/{item_id}", json=updates)

    async def update_item_title(self, item_id: str, title: str) -> dict:
        """Actualiza el titulo de un item."""
        return await self.put(f"/items/{item_id}", json={"title": title})

    async def update_item_description(self, item_id: str, plain_text: str) -> dict:
        """Actualiza la descripcion de un item (PUT /items/{id}/description)."""
        return await self.put(f"/items/{item_id}/description", json={"plain_text": plain_text})

    async def update_item_status(self, item_id: str, status: str) -> dict:
        """Cambia el estado de un item (active/paused)."""
        return await self.put(f"/items/{item_id}", json={"status": status})

    async def update_item_shipping(self, item_id: str, shipping: dict) -> dict:
        """Actualiza configuracion de envio de un item."""
        return await self.put(f"/items/{item_id}", json={"shipping": shipping})

    async def update_item_pictures(self, item_id: str, pictures: list) -> dict:
        """Actualiza las fotos de un item."""
        return await self.put(f"/items/{item_id}", json={"pictures": pictures})

    async def update_item_attributes(self, item_id: str, attributes: list) -> dict:
        """Actualiza los atributos de un item."""
        return await self.put(f"/items/{item_id}", json={"attributes": attributes})

    async def get_item_description(self, item_id: str) -> dict:
        """Obtiene la descripcion de un item (GET /items/{id}/description)."""
        return await self.get(f"/items/{item_id}/description")

    # === Seller Reputation ===

    async def get_seller_reputation(self) -> dict:
        """Obtiene la reputacion del vendedor desde user_info."""
        user = await self.get_user_info()
        return user.get("seller_reputation", {})

    # === Messages ===

    async def get_messages(self, offset: int = 0, limit: int = 20,
                           date_from: str = None, date_to: str = None) -> dict:
        """Obtiene conversaciones con mensajes via ordenes recientes.

        MeLi no tiene endpoint para listar todos los packs de mensajes.
        Se obtienen ordenes recientes y se revisa cuales tienen mensajes.
        """
        order_params: dict = {
            "seller": self.user_id,
            "limit": 50,
            "sort": "date_desc",
            "offset": 0,
        }
        if date_from:
            order_params["order.date_created.from"] = f"{date_from}T00:00:00.000-00:00"
        if date_to:
            order_params["order.date_created.to"] = f"{date_to}T23:59:59.000-00:00"

        orders_data = await self.get("/orders/search/recent", params=order_params)
        orders = orders_data.get("results", [])

        sem = asyncio.Semaphore(10)

        async def _fetch_pack(order):
            pack_id = str(order.get("pack_id") or order.get("id", ""))
            if not pack_id:
                return None
            async with sem:
                try:
                    r = await self.get(
                        f"/messages/packs/{pack_id}/sellers/{self.user_id}",
                        params={"tag": "post_sale", "mark_as_read": "false"},
                    )
                    total = r.get("paging", {}).get("total", 0)
                    if total > 0:
                        # Devolver en formato compatible con el endpoint existente
                        return {
                            "id": pack_id,
                            "pack_id": pack_id,
                            "messages": r.get("messages", []),
                            "date_created": order.get("date_created", ""),
                        }
                except Exception:
                    pass
            return None

        raw = await asyncio.gather(*[_fetch_pack(o) for o in orders])
        conversations = [r for r in raw if r is not None]

        # Ordenar del mas nuevo al mas viejo
        conversations.sort(key=lambda c: c.get("date_created", ""), reverse=True)

        total_convs = len(conversations)
        page = conversations[offset:offset + limit]

        return {
            "results": page,
            "paging": {"total": total_convs, "offset": offset, "limit": limit},
        }

    async def get_message_thread(self, pack_id: str) -> dict:
        """Obtiene un thread de mensajes."""
        return await self.get(f"/messages/packs/{pack_id}/sellers/{self.user_id}", params={
            "tag": "post_sale",
            "mark_as_read": "false",
        })

    async def send_message(self, pack_id: str, text: str) -> dict:
        """Envia un mensaje en una conversacion."""
        return await self.post(f"/messages/packs/{pack_id}/sellers/{self.user_id}", json={
            "from": {"user_id": self.user_id},
            "text": {"plain": text},
        })

    # === Questions (gestionar) ===

    async def answer_question(self, question_id: int, text: str) -> dict:
        """Responde una pregunta."""
        return await self.post("/answers", json={
            "question_id": question_id,
            "text": text,
        })

    async def delete_question(self, question_id: int) -> dict:
        """Elimina una pregunta (DELETE /questions/{id})."""
        return await self._request("DELETE", f"/questions/{question_id}")

    # === Claims (gestionar) ===

    async def get_claim_detail(self, claim_id: str) -> dict:
        """Obtiene el detalle de un reclamo."""
        return await self.get(f"/post-purchase/v1/claims/{claim_id}")

    async def get_claim_messages(self, claim_id: str) -> list:
        """Obtiene mensajes de un reclamo."""
        return await self.get(f"/post-purchase/v1/claims/{claim_id}/messages")

    async def respond_claim(self, claim_id: str, action: str, text: str) -> dict:
        """Envia mensaje al comprador en un reclamo."""
        return await self.post(f"/post-purchase/v1/claims/{claim_id}/messages", json={
            "receiver_role": "complainant",
            "message": text,
        })

    # === SKU Search & Item Creation ===

    async def search_item_by_sku(self, sku: str) -> dict | None:
        """Busca un item por seller_sku. Retorna el item o None si no existe."""
        try:
            result = await self.get(
                f"/users/{self.user_id}/items/search",
                params={"seller_sku": sku, "limit": 1}
            )
            item_ids = result.get("results", [])
            if item_ids:
                items = await self.get_items_details(item_ids[:1])
                if items:
                    return items[0].get("body", items[0])
        except Exception:
            pass
        return None

    async def search_all_items_by_sku(self, sku: str) -> list[dict]:
        """Busca TODOS los items que contengan este SKU (item-level o variation-level)."""
        trusted_ids: set[str] = set()
        extra_ids: set[str] = set()

        # Strategy 1: seller_sku param — MeLi indexed, trusted results
        try:
            r1 = await self.get(
                f"/users/{self.user_id}/items/search",
                params={"seller_sku": sku, "limit": 50}
            )
            for item_id in r1.get("results", []):
                trusted_ids.add(item_id)
        except Exception:
            pass

        # Strategy 2: keyword search on own listings (may include false positives)
        try:
            r2 = await self.get(
                f"/users/{self.user_id}/items/search",
                params={"q": sku, "limit": 50}
            )
            for item_id in r2.get("results", []):
                if item_id not in trusted_ids:
                    extra_ids.add(item_id)
        except Exception:
            pass

        all_ids = trusted_ids | extra_ids
        if not all_ids:
            return []

        # Batch fetch details
        all_items = []
        ids_list = list(all_ids)
        for i in range(0, len(ids_list), 20):
            batch = ids_list[i:i+20]
            details = await self.get_items_details(batch)
            for d in details:
                body = d.get("body", d)
                if body and body.get("id"):
                    all_items.append(body)

        # Post-filter: trusted IDs pass through; extra IDs need SKU verification
        verified = []
        sku_upper = sku.upper()
        for item in all_items:
            item_id = item.get("id", "")
            # seller_sku search results are already verified by MeLi
            if item_id in trusted_ids:
                verified.append(item)
                continue
            # For keyword results, verify SKU exists in seller_custom_field or SELLER_SKU attribute
            if _item_has_sku(item, sku_upper):
                verified.append(item)

        return verified

    async def search_items_by_skus(self, skus: list[str]) -> dict[str, dict]:
        """Busca items por lista de SKUs. Retorna {sku: item_data}."""
        result = {}
        sem = asyncio.Semaphore(5)

        async def fetch_one(sku: str):
            async with sem:
                item = await self.search_item_by_sku(sku)
                return sku, item

        tasks = [fetch_one(s) for s in skus]
        for coro in asyncio.as_completed(tasks):
            sku, item = await coro
            if item:
                result[sku] = item
        return result

    async def get_category_attributes(self, category_id: str) -> list:
        """Obtiene los atributos de una categoria (requeridos y opcionales)."""
        try:
            return await self.get(f"/categories/{category_id}/attributes")
        except Exception:
            return []

    async def suggest_category(self, title: str) -> list:
        """Sugiere categorias basadas en titulo usando domain discovery (GET ?q=)."""
        try:
            result = await self.get_public(
                "/sites/MLM/domain_discovery/search",
                params={"q": title}
            )
            return result if isinstance(result, list) else result.get("results", [])
        except Exception:
            return []

    async def predict_category(self, title: str) -> dict:
        """Predice la categoria mas probable para un producto usando domain discovery."""
        try:
            result = await self.get_public(
                "/sites/MLM/domain_discovery/search",
                params={"q": title}
            )
            if isinstance(result, list) and result:
                best = result[0]
                cat_id = best.get("category_id", "")
                if cat_id:
                    # Fetch full category path (public endpoint)
                    cat_info = await self.get_public(f"/categories/{cat_id}")
                    path_from_root = cat_info.get("path_from_root", [])
                    return {
                        "id": cat_id,
                        "name": best.get("category_name", ""),
                        "path_from_root": path_from_root,
                    }
            return {}
        except Exception:
            return {}

    async def search_categories(self, query: str) -> list:
        """Busca categorias por palabra clave en MeLi (sites/MLM/search con category facet)."""
        try:
            result = await self.get_public(
                "/sites/MLM/search",
                params={"q": query, "limit": 5}
            )
            # Extract category filters from facets
            categories = []
            for facet in result.get("available_filters", []):
                if facet.get("id") == "category":
                    for val in facet.get("values", [])[:10]:
                        categories.append({
                            "id": val.get("id", ""),
                            "name": val.get("name", ""),
                            "results": val.get("results", 0),
                        })
                    break
            return categories
        except Exception:
            return []

    async def create_item(self, payload: dict) -> dict:
        """Crea un nuevo item en Mercado Libre (POST /items).

        Returns the response body even on 400 errors so callers get
        MeLi's cause/message details.
        """
        await self._refresh_token_if_needed()
        response = await self._client.post("/items", json=payload)
        text = response.text.strip()
        if not text:
            if response.status_code >= 400:
                raise Exception(f"MeLi returned {response.status_code} with empty body")
            return {}
        data = response.json()
        if response.status_code >= 400:
            # Include MeLi's error details in the exception
            causes = data.get("cause", data.get("causes", []))
            msg = data.get("message", data.get("error", ""))
            details = []
            if msg:
                details.append(msg)
            for c in causes:
                if isinstance(c, dict):
                    details.append(c.get("message") or c.get("code", str(c)))
                else:
                    details.append(str(c))
            raise Exception(" | ".join(details) if details else f"Error {response.status_code}")
        return data

    # === Item Health ===

    async def get_item_health(self, item_id: str) -> dict | None:
        """GET /items/{item_id}/health — score oficial de MeLi."""
        try:
            return await self.get(f"/items/{item_id}/health")
        except Exception:
            return None

    async def get_item_health_actions(self, item_id: str) -> list:
        """GET /items/{item_id}/health/actions — acciones pendientes."""
        try:
            data = await self.get(f"/items/{item_id}/health/actions")
            return data.get("actions", [])
        except Exception:
            return []

    # === Seller Promotions ===

    async def get_user_promotions(self) -> list:
        """GET /seller-promotions/users/{user_id}?app_version=v2"""
        try:
            data = await self.get(
                f"/seller-promotions/users/{self.user_id}",
                params={"app_version": "v2"}
            )
            return data.get("results", data if isinstance(data, list) else [])
        except Exception:
            return []

    async def get_item_promotions(self, item_id: str) -> list:
        """GET /seller-promotions/items/{item_id}?app_version=v2"""
        try:
            data = await self.get(
                f"/seller-promotions/items/{item_id}",
                params={"app_version": "v2"}
            )
            # MeLi puede retornar: lista directa, dict con "results", o dict con otro key
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                if "results" in data:
                    return data["results"]
                # Fallback: buscar la primera lista en los values
                for v in data.values():
                    if isinstance(v, list):
                        return v
            return []
        except Exception as e:
            import logging
            logging.getLogger("meli").warning(f"get_item_promotions({item_id}) error: {e}")
            return []

    async def activate_item_promotion(self, item_id: str, deal_price: float,
                                       promotion_type: str, **kwargs) -> dict:
        """Activa promocion: PUT para deals de campana, POST para PRICE_DISCOUNT."""
        endpoint = f"/seller-promotions/items/{item_id}"
        params = {"app_version": "v2"}

        if promotion_type == "PRICE_DISCOUNT":
            # PRICE_DISCOUNT: POST con deal_price + start/finish dates
            body = {"deal_price": deal_price, "promotion_type": "PRICE_DISCOUNT"}
            if kwargs.get("original_price"):
                body["original_price"] = kwargs["original_price"]
            if kwargs.get("start_date"):
                body["start_date"] = kwargs["start_date"]
            if kwargs.get("finish_date"):
                body["finish_date"] = kwargs["finish_date"]
            return await self.post(endpoint, params=params, json=body)
        else:
            # DEAL/DOD/LIGHTNING/MARKETPLACE_CAMPAIGN: PUT con deal_price + promotion_type + promotion_id
            body = {"deal_price": deal_price, "promotion_type": promotion_type}
            if kwargs.get("promotion_id"):
                body["promotion_id"] = kwargs["promotion_id"]
            return await self.put(endpoint, params=params, json=body)

    async def delete_item_promotion(self, item_id: str, promotion_type: str) -> dict:
        """DELETE /seller-promotions/items/{item_id}?app_version=v2&promotion_type=TYPE"""
        return await self.delete(
            f"/seller-promotions/items/{item_id}",
            params={"app_version": "v2", "promotion_type": promotion_type}
        )

    # === Listing Types ===

    async def get_listing_types(self) -> list:
        """Obtiene los tipos de listado disponibles para MLM."""
        try:
            return await self.get("/sites/MLM/listing_types")
        except Exception:
            return []

    async def validate_item(self, payload: dict) -> dict:
        """Valida un item sin publicarlo (POST /items/validate).

        MeLi returns 400 with causes[] when validation fails - we need to
        read the response body instead of raising on status.
        """
        try:
            await self._refresh_token_if_needed()
            response = await self._client.post("/items/validate", json=payload)
            text = response.text.strip()
            if not text:
                return {"status": response.status_code}
            data = response.json()
            if response.status_code >= 400:
                # MeLi returns validation errors in the body (causes, message, etc.)
                if isinstance(data, dict):
                    return data
                return {"error": str(data), "status": response.status_code}
            return data
        except Exception as e:
            return {"error": str(e)}


async def get_meli_client() -> Optional[MeliClient]:
    """Factory para obtener un cliente MeLi con tokens almacenados."""
    tokens = await token_store.get_any_tokens()
    if not tokens:
        return None

    return MeliClient(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        user_id=tokens["user_id"]
    )
