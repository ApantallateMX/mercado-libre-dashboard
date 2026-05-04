"""
BinManager HTTP Client
======================
Gestiona sesión persistente con BinManager WMS.
Re-login automático cuando la sesión expira.
"""
import logging
import os
from typing import Optional

import asyncio
import httpx

logger = logging.getLogger(__name__)

# ── Cola global de peticiones BM ─────────────────────────────────────────────
# Solo 1 request activo a la vez hacia BM. La siguiente petición no sale
# hasta que la anterior respondió + 200ms de pausa.
# Compartido por TODAS las instancias de BinManagerClient en el proceso.
_BM_GLOBAL_SEM: Optional[asyncio.Semaphore] = None
_BM_REQUEST_DELAY = 0.2  # segundos entre requests


def _get_bm_sem() -> asyncio.Semaphore:
    """Lazy init del semáforo — requiere event loop activo."""
    global _BM_GLOBAL_SEM
    if _BM_GLOBAL_SEM is None:
        _BM_GLOBAL_SEM = asyncio.Semaphore(1)
    return _BM_GLOBAL_SEM

_BM_BASE = "https://binmanager.mitechnologiesinc.com"
_BM_USER = os.getenv("BM_USER", "jovan.rodriguez@mitechnologiesinc.com")
_BM_PASS = os.getenv("BM_PASS", "aquemamonxD!")

_AJAX_HEADERS = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}

# Payload base para Get_GlobalStock_InventoryBySKU con NEEDRETAILPRICEPH=True
_GS_BASE_PAYLOAD = {
    "COMPANYID": 1, "CATEGORYID": None, "WAREHOUSEID": None,
    "LOCATIONID": None, "BINID": None, "CONDITION": None,
    "FORINVENTORY": None, "BUSCADOR": False, "BRAND": None,
    "MODEL": None, "SIZE": None, "LCN": None, "CONCEPTID": 8,
    "OPENCELL": False, "OCCOMPTABILITY": False,
    "NEEDRETAILPRICE": False, "NEEDFLOORPRICE": False,
    "NEEDIPS": False, "NEEDTIER": False, "NEEDFILE": False,
    "NEEDVIRTUALQTY": False, "NEEDINCOMINGQTY": False, "NEEDAVGCOST": False,
    "NUMBERPAGE": 1, "RECORDSPAGE": 5,
    "ORDERBYNAME": None, "ORDERBYTYPE": None,
    "PorcentajeFloor": 20, "StatusConcept": None,
    "RetailBalance": None, "RetailAvailable": None,
    "MaxQty": None, "MinQty": None, "NameQty": None, "Tier": None,
    "NEEDRETAILPRICEPH": True,
    "TAGS": None, "TVL": False, "NEEDPORCENTAGE": False,
    "NEEDUPC": False, "filterUPC": None, "IsComplete": None,
    "NEEDSALES": False, "StartDate": None, "EndDate": None,
    "SUPPLIERS": None, "TAGSNOTIN": None,
    "NEEDLASTREPORTEDSALESPRICE": False, "SALESPRICE": None,
    "Jsonfilter": "[]",
    "Arrayfilters_Condition": None, "Namefilters_Condition": None,
    "Arrayfilters_Brand": None, "Namefilters_Brand": None,
    "Arrayfilters_Model": None, "Namefilters_Model": None,
    "Arrayfilters_Size": None, "Namefilters_Size": None,
    "Arrayfilters_Category": None, "Namefilters_Category": None,
    "Arrayfilters_Tags": None, "Namefilters_Tags": None,
    "Arrayfilters_Tags_Exclude": None, "Namefilters_Tags_Exlude": None,
    "Arrayfilters_Supplier": None, "Namefilters_Supplier": None,
}


class BinManagerClient:
    """Cliente HTTP para BinManager WMS con sesión persistente."""

    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None
        self._logged_in = False
        self._login_lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=True, timeout=30)
        return self._http

    def _session_expired(self, r: httpx.Response) -> bool:
        """True si la sesión expiró (redirige a login)."""
        return "User/Index" in str(r.url) or r.status_code == 401

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        """POST a BM a través de la cola global — máx 1 request activo a la vez."""
        async with _get_bm_sem():
            r = await self._client().post(url, **kwargs)
            await asyncio.sleep(_BM_REQUEST_DELAY)
            return r

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        """GET a BM a través de la cola global — máx 1 request activo a la vez."""
        async with _get_bm_sem():
            r = await self._client().get(url, **kwargs)
            await asyncio.sleep(_BM_REQUEST_DELAY)
            return r

    async def login(self) -> bool:
        async with self._login_lock:
            # Si otra coroutine ya completó el login mientras esperábamos, no repetir
            if self._logged_in:
                return True
            try:
                await self._get(f"{_BM_BASE}/User/Index", timeout=15)
                r = await self._post(
                    f"{_BM_BASE}/User/LoginUser",
                    json={"USRNAME": _BM_USER, "PASS": _BM_PASS},
                    headers=_AJAX_HEADERS,
                    timeout=15,
                )
                if r.status_code == 200 and r.json().get("Id"):
                    self._logged_in = True
                    logger.info("BinManager login OK")
                    return True
            except Exception as e:
                logger.error(f"BinManager login error: {e}")
            self._logged_in = False
            return False

    async def get_retail_price_ph(self, sku: str) -> Optional[float]:
        """
        Retorna LastRetailPricePurchaseHistory para un SKU.
        Usa Get_GlobalStock_InventoryBySKU con SEARCH=sku y NEEDRETAILPRICEPH=True.
        Respuesta ~1600 bytes, sin timeout.
        Retorna None si el SKU no existe o hay error.
        """
        if not self._logged_in:
            if not await self.login():
                return None

        payload = {**_GS_BASE_PAYLOAD, "SEARCH": sku}

        for attempt in range(2):
            try:
                r = await self._post(
                    f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU",
                    json=payload,
                    headers=_AJAX_HEADERS,
                    timeout=30,
                )
                if self._session_expired(r):
                    self._logged_in = False
                    if attempt == 0:
                        await self.login()
                        continue
                    return None

                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        # Buscar coincidencia exacta de SKU
                        match = next((x for x in data if x.get("SKU") == sku), data[0])
                        return match.get("LastRetailPricePurchaseHistory")
                return None

            except httpx.TimeoutException:
                logger.warning(f"BinManager timeout para SKU {sku} (intento {attempt + 1})")
                if attempt == 0:
                    continue
                return None
            except Exception as e:
                logger.error(f"BinManager error SKU {sku}: {e}")
                if attempt == 0 and not self._logged_in:
                    await self.login()
                    continue
                return None

        return None

    async def get_operations_kpis(self, start_date: str, end_date: str) -> Optional[dict]:
        """Get KPIs from BinManager Operations Dashboard (MTY MAXX Plant Report)."""
        if not self._logged_in:
            if not await self.login():
                return None
        payload = {"StartDate": start_date, "EndDate": end_date, "excludedhv": 0, "needtv": 0}
        for attempt in range(2):
            try:
                r = await self._post(
                    f"{_BM_BASE}/ReportsBinManager/OperationsDashboard/GetDashboardKPIs",
                    json=payload, headers=_AJAX_HEADERS, timeout=45,
                )
                if self._session_expired(r):
                    self._logged_in = False
                    if attempt == 0:
                        await self.login()
                        continue
                    return None
                if r.status_code == 200:
                    data = r.json()
                    return data[0] if isinstance(data, list) and data else None
                return None
            except Exception as e:
                logger.error(f"BinManager GetDashboardKPIs error: {e}")
                if attempt == 0:
                    continue
                return None
        return None

    async def get_global_inventory(self, page: int = 1, per_page: int = 500, min_qty: int = 0) -> list:
        """Retorna inventario global de BM paginando de 500 en 500.

        CONCEPTID=1 (Producto Vendible) — mismo que get_stock_with_reserve.
        SEARCH="" retorna todos los SKUs (post-mantenimiento: SEARCH=null ya no funciona).
        per_page máximo efectivo = 500 (límite BM post-mantenimiento 2026-04-16).
        Si per_page=500 (default), trae UNA página. Para todos los SKUs usar get_bulk_stock().
        La respuesta incluye: SKU, CategoryName, Brand, Model, Title,
          TotalQty, AvailableQTY, Reserve, AvgCostQTY, LastRetailPricePurchaseHistory.
        """
        if not self._logged_in:
            if not await self.login():
                return []
        _PAGE_SIZE = min(per_page, 500)  # BM no acepta más de 500 por página
        payload = {
            "COMPANYID": 1, "SEARCH": "", "CONCEPTID": 1,
            "NUMBERPAGE": page, "RECORDSPAGE": _PAGE_SIZE,
            "MinQty": min_qty if min_qty > 0 else None,
            "NEEDRETAILPRICEPH": False,
            "CATEGORYID": None, "WAREHOUSEID": None, "LOCATIONID": None,
            "BINID": None, "CONDITION": None, "FORINVENTORY": None,
            "BUSCADOR": False, "BRAND": None, "MODEL": None,
            "ORDERBYNAME": None, "ORDERBYTYPE": None,
            "SIZE": None, "LCN": None,
            "NEEDAVGCOST": True,
            "NEEDLASTREPORTEDSALESPRICE": None,
            "Jsonfilter": "[]",
        }
        for attempt in range(2):
            try:
                r = await self._post(
                    f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU",
                    json=payload, headers=_AJAX_HEADERS, timeout=60,
                )
                if self._session_expired(r):
                    self._logged_in = False
                    if attempt == 0:
                        await self.login()
                        continue
                    return []
                if r.status_code == 200:
                    data = r.json()
                    return data if isinstance(data, list) else []
                return []
            except Exception as e:
                logger.error(f"BinManager get_global_inventory error: {e}")
                if attempt == 0:
                    continue
                return []
        return []

    async def get_bulk_stock(self, conditions: str = "GRA,GRB,GRC,NEW") -> list:
        """Retorna TODOS los SKUs vendibles paginando de 500 en 500.

        LOCATIONID=47,62,68 + CONCEPTID=1.
        conditions: qué condiciones incluir. Default GRA,GRB,GRC,NEW (excluye ICB/ICC).
        Para SKUs con sufijo -ICB/-ICC pasar "GRA,GRB,GRC,ICB,ICC,NEW".
        Incluye AvgCostQTY y LastRetailPricePurchaseHistory.

        Cambio post-mantenimiento BM (2026-04-16):
          - SEARCH=null ya no funciona → usar SEARCH=""
          - RECORDSPAGE>500 retorna 0 → paginar con RECORDSPAGE=500
        """
        if not self._logged_in:
            if not await self.login():
                return []
        _BM_PAGE_SIZE = 500   # límite impuesto por BM post-mantenimiento
        _BM_MAX_PAGES = 20    # tope de seguridad (20×500 = 10,000 SKUs máx)
        url = f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
        base_payload = {
            "COMPANYID": 1, "SEARCH": "", "CONCEPTID": 1,
            "LOCATIONID": "47,62,68",
            "CONDITION": conditions,
            "FORINVENTORY": 0, "BUSCADOR": False,
            "RECORDSPAGE": _BM_PAGE_SIZE,
            "NEEDAVGCOST": True, "NEEDRETAILPRICEPH": True,
            "CATEGORYID": None, "WAREHOUSEID": None, "BINID": None,
            "BRAND": None, "MODEL": None, "SIZE": None, "LCN": None,
            "OPENCELL": "", "OCCOMPTABILITY": "",
            "NEEDRETAILPRICE": False, "NEEDFLOORPRICE": False,
            "NEEDIPS": False, "NEEDTIER": False, "NEEDFILE": False,
            "NEEDVIRTUALQTY": False, "NEEDINCOMINGQTY": False,
            "NEEDSALES": False, "NEEDUPC": False, "NEEDPORCENTAGE": False,
            "ORDERBYNAME": None, "ORDERBYTYPE": None,
            "PorcentajeFloor": 20, "StatusConcept": None,
            "RetailBalance": None, "RetailAvailable": None,
            "MaxQty": None, "MinQty": None, "NameQty": None, "Tier": None,
            "TAGS": None, "TVL": False, "TAGSNOTIN": None,
            "SUPPLIERS": None, "filterUPC": None,
            "NEEDLASTREPORTEDSALESPRICE": None, "StartDate": None, "EndDate": None,
            "Jsonfilter": "[]",
            "Arrayfilters_Condition": None, "Namefilters_Condition": None,
            "Arrayfilters_Brand": None, "Namefilters_Brand": None,
            "Arrayfilters_Model": None, "Namefilters_Model": None,
            "Arrayfilters_Size": None, "Namefilters_Size": None,
            "Arrayfilters_Category": None, "Namefilters_Category": None,
            "Arrayfilters_Tags": None, "Namefilters_Tags": None,
            "Arrayfilters_Tags_Exclude": None, "Namefilters_Tags_Exlude": None,
            "Arrayfilters_Supplier": None, "Namefilters_Supplier": None,
        }
        all_rows: list = []
        for page in range(1, _BM_MAX_PAGES + 1):
            payload = {**base_payload, "NUMBERPAGE": page}
            fetched = False
            for attempt in range(2):
                try:
                    r = await self._post(url, json=payload, headers=_AJAX_HEADERS, timeout=60)
                    if self._session_expired(r):
                        self._logged_in = False
                        if attempt == 0:
                            await self.login()
                            continue
                        return all_rows  # devolver lo que tenemos
                    if r.status_code == 200:
                        data = r.json()
                        page_rows = data if isinstance(data, list) else []
                        all_rows.extend(page_rows)
                        fetched = True
                        if len(page_rows) < _BM_PAGE_SIZE:
                            # Última página — menos registros de los esperados
                            logger.info(f"[BM] get_bulk_stock: {len(all_rows)} SKUs en {page} páginas")
                            return all_rows
                        break  # página completa, continuar con la siguiente
                    else:
                        return all_rows  # error HTTP — devolver lo acumulado
                except Exception as e:
                    logger.error(f"BinManager get_bulk_stock pág {page} error: {e}")
                    if attempt == 0:
                        continue
                    return all_rows
            if not fetched:
                return all_rows
        logger.info(f"[BM] get_bulk_stock: {len(all_rows)} SKUs en {_BM_MAX_PAGES} páginas (límite)")
        return all_rows

    async def get_stock_with_reserve(self, sku: str, conditions: str = "GRA,GRB,GRC,NEW") -> tuple[int, int] | None:
        """Retorna (AvailableQTY, Reserve) para un SKU filtrado a LOCATIONID=47,62,68 (MTY+CDMX).
        Usa Get_GlobalStock_InventoryBySKU CONCEPTID=1 — única fuente correcta de stock vendible.
          - AvailableQTY = stock vendible (TotalQty - Reserve, calculado por BM server-side)
          - Reserve      = unidades reservadas para órdenes pendientes
          - None         = fallo de sesión/red — dato desconocido (NO confundir con 0 genuino)
        conditions: GRA,GRB,GRC,NEW por default. Pasar GRA,GRB,GRC,ICB,ICC,NEW para SKUs IC.
        Verificado: SNTV001764 → AvailableQTY=213, Reserve=2 (TotalQty=215)
        """
        return await self._query_bm_stock(sku, conditions=conditions)

    async def get_available_qty(self, sku: str, conditions: str = "GRA,GRB,GRC,NEW") -> int:
        """Retorna solo AvailableQTY (stock vendible). Ver get_stock_with_reserve() para ambos.
        Usa Get_GlobalStock_InventoryBySKU CONCEPTID=1, LOCATIONID=47,62,68.
        Retorna 0 tanto para stock genuino 0 como para fallos — usar get_stock_with_reserve()
        si necesitas distinguir entre 0 real y fallo de red.
        Verificado: SNTV001764 → TotalQty=215, Reserve=2, AvailableQTY=213.
        """
        result = await self._query_bm_stock(sku, conditions=conditions)
        return result[0] if result is not None else 0

    async def _query_bm_stock(self, sku: str, conditions: str = "GRA,GRB,GRC,NEW") -> tuple[int, int] | None:
        """Consulta BM y retorna (AvailableQTY, Reserve) con CONCEPTID=1 + LOCATIONID=47,62,68 (MTY+CDMX).
        Método interno compartido por get_available_qty() y get_stock_with_reserve().
        Maneja condición-variantes: si SKU no tiene match exacto, suma variantes -GRA/-GRB/etc.
        Verificado: SNTV001764 → AvailableQTY=213, Reserve=2.
        """
        if not self._logged_in:
            if not await self.login():
                return 0, 0

        # Extraer base SKU (sin sufijo de condición)
        upper = sku.upper()
        base = sku
        for sfx in ("-ICB", "-ICC", "-NEW", "-GRA", "-GRB", "-GRC"):
            if upper.endswith(sfx):
                base = sku[:-len(sfx)]
                break

        url = f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
        payload = {
            "COMPANYID": 1,
            "CATEGORYID": None, "WAREHOUSEID": None,
            "LOCATIONID": "47,62,68",
            "BINID": None,
            "SEARCH": base,
            "CONDITION": conditions,
            "FORINVENTORY": 0,
            "BUSCADOR": False,
            "BRAND": None, "MODEL": None, "SIZE": None, "LCN": None,
            "CONCEPTID": 1,
            "OPENCELL": "", "OCCOMPTABILITY": "",
            "NEEDRETAILPRICE": False, "NEEDFLOORPRICE": False,
            "NEEDIPS": False, "NEEDTIER": False, "NEEDFILE": False,
            "NEEDVIRTUALQTY": False, "NEEDINCOMINGQTY": False,
            "NEEDAVGCOST": False, "NEEDRETAILPRICEPH": False,
            "NEEDSALES": False, "NEEDUPC": False, "NEEDPORCENTAGE": False,
            "NUMBERPAGE": 1, "RECORDSPAGE": 10,
            "ORDERBYNAME": None, "ORDERBYTYPE": None,
            "PorcentajeFloor": 20, "StatusConcept": None,
            "RetailBalance": None, "RetailAvailable": None,
            "MaxQty": None, "MinQty": None, "NameQty": None, "Tier": None,
            "TAGS": None, "TVL": False, "TAGSNOTIN": None,
            "SUPPLIERS": None, "filterUPC": None,
            "NEEDLASTREPORTEDSALESPRICE": None,
            "StartDate": None, "EndDate": None,
            "Jsonfilter": "[]",
            "Arrayfilters_Condition": None, "Namefilters_Condition": None,
            "Arrayfilters_Brand": None, "Namefilters_Brand": None,
            "Arrayfilters_Model": None, "Namefilters_Model": None,
            "Arrayfilters_Size": None, "Namefilters_Size": None,
            "Arrayfilters_Category": None, "Namefilters_Category": None,
            "Arrayfilters_Tags": None, "Namefilters_Tags": None,
            "Arrayfilters_Tags_Exclude": None, "Namefilters_Tags_Exlude": None,
            "Arrayfilters_Supplier": None, "Namefilters_Supplier": None,
        }
        _COND_SFXS = ("-GRA", "-GRB", "-GRC", "-ICB", "-ICC", "-NEW")
        for attempt in range(2):
            try:
                r = await self._post(url, json=payload, headers=_AJAX_HEADERS, timeout=7)
                if self._session_expired(r):
                    self._logged_in = False
                    if attempt == 0:
                        await self.login()
                        continue
                    return None  # Sesión expirada tras retry — dato desconocido
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        # 1. Coincidencia exacta de SKU base
                        match = next(
                            (x for x in data if (x.get("SKU") or "").upper() == base.upper()),
                            None
                        )
                        if match is not None:
                            avail   = int(match.get("AvailableQTY") or 0)
                            reserve = int(match.get("Reserve") or 0)
                            return avail, reserve
                        # 2. Sin match exacto: sumar variantes de condición del mismo base SKU
                        #    Ej: SNTV004196 solo existe en BM como SNTV004196-GRB
                        variants = [
                            x for x in data
                            if (x.get("SKU") or "").upper().startswith(base.upper() + "-")
                            and any((x.get("SKU") or "").upper().endswith(s) for s in _COND_SFXS)
                        ]
                        if variants:
                            avail   = sum(int(x.get("AvailableQTY") or 0) for x in variants)
                            reserve = sum(int(x.get("Reserve") or 0) for x in variants)
                            return avail, reserve
                        return 0, 0  # HTTP 200 — BM respondió, SKU sin stock (0 genuino)
                    return 0, 0  # HTTP 200 — BM respondió con lista vacía (SKU inexistente → 0 genuino)
                return None  # HTTP no-200 (503, 401, etc.) — fallo de servidor, dato desconocido
            except httpx.TimeoutException:
                logger.warning(f"BinManager timeout _query_bm_stock {sku} (intento {attempt+1})")
                if attempt == 0:
                    continue
                return None  # Timeout — no sabemos el stock real
            except Exception as e:
                logger.error(f"BinManager _query_bm_stock error {sku}: {e}")
                if attempt == 0:
                    continue
                return None  # Excepción — no sabemos el stock real
        return None  # Agotados los intentos sin respuesta válida

    async def post_inventory(self, url: str, payload: dict, timeout: float = 15.0):
        """POST autenticado a un endpoint de inventario BM. Maneja sesión expirada con re-login.
        Retorna response httpx o None si falla."""
        if not self._logged_in:
            if not await self.login():
                return None
        for attempt in range(2):
            try:
                r = await self._post(url, json=payload, headers=_AJAX_HEADERS, timeout=timeout)
                if self._session_expired(r):
                    self._logged_in = False
                    if attempt == 0:
                        await self.login()
                        continue
                    return None
                return r
            except Exception as e:
                logger.warning(f"BinManager post_inventory error (intento {attempt+1}): {e}")
                if attempt == 0:
                    continue
                return None
        return None

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None
        self._logged_in = False


# ── Singleton compartido — usado por main.py, stock_sync_multi.py, etc. ─────
_shared_bm: Optional[BinManagerClient] = None


async def get_shared_bm() -> BinManagerClient:
    """Retorna el cliente BM global con sesión activa. Login automático si es necesario."""
    global _shared_bm
    if _shared_bm is None:
        _shared_bm = BinManagerClient()
    if not _shared_bm._logged_in:
        await _shared_bm.login()
    return _shared_bm
