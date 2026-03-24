"""
BinManager HTTP Client
======================
Gestiona sesión persistente con BinManager WMS.
Re-login automático cuando la sesión expira.
"""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_BM_BASE = "https://binmanager.mitechnologiesinc.com"
_BM_USER = os.getenv("BM_USER", "jovan.rodriguez@mitechnologiesinc.com")
_BM_PASS = os.getenv("BM_PASS", "123456")

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

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=True, timeout=30)
        return self._http

    def _session_expired(self, r: httpx.Response) -> bool:
        """True si la sesión expiró (redirige a login)."""
        return "User/Index" in str(r.url) or r.status_code == 401

    async def login(self) -> bool:
        c = self._client()
        try:
            await c.get(f"{_BM_BASE}/User/Index", timeout=15)
            r = await c.post(
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

        c = self._client()
        payload = {**_GS_BASE_PAYLOAD, "SEARCH": sku}

        for attempt in range(2):
            try:
                r = await c.post(
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
        c = self._client()
        payload = {"StartDate": start_date, "EndDate": end_date, "excludedhv": 0, "needtv": 0}
        for attempt in range(2):
            try:
                r = await c.post(
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

    async def get_global_inventory(self, page: int = 1, per_page: int = 50, min_qty: int = 1) -> list:
        """Get global inventory page from BinManager (all SKUs with stock)."""
        if not self._logged_in:
            if not await self.login():
                return []
        c = self._client()
        payload = {
            "COMPANYID": 1, "SEARCH": None, "CONCEPTID": 8,
            "NUMBERPAGE": page, "RECORDSPAGE": per_page,
            "MinQty": min_qty, "NEEDRETAILPRICEPH": True,
            "CATEGORYID": None, "WAREHOUSEID": None, "LOCATIONID": None,
            "BINID": None, "CONDITION": None, "FORINVENTORY": None,
            "BUSCADOR": False, "BRAND": None, "MODEL": None,
            "ORDERBYNAME": None, "ORDERBYTYPE": None,
        }
        for attempt in range(2):
            try:
                r = await c.post(
                    f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU",
                    json=payload, headers=_AJAX_HEADERS, timeout=25,
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

    async def get_sku_stock(self, sku: str) -> dict:
        """Get stock and product info for a specific SKU."""
        if not self._logged_in:
            if not await self.login():
                return {}
        c = self._client()
        payload = {
            "COMPANYID": 1, "SEARCH": sku, "CONCEPTID": 8,
            "NUMBERPAGE": 1, "RECORDSPAGE": 5, "NEEDRETAILPRICEPH": True,
        }
        for attempt in range(2):
            try:
                r = await c.post(
                    f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU",
                    json=payload, headers=_AJAX_HEADERS, timeout=10,
                )
                if self._session_expired(r):
                    self._logged_in = False
                    if attempt == 0:
                        await self.login()
                        continue
                    return {}
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        row = next((x for x in data if (x.get("SKU") or "").upper() == sku.upper()), data[0])
                        return {
                            "stock": row.get("QtyTotal", 0) or 0,
                            "retail_price": row.get("RetailPrice") or row.get("LastRetailPricePurchaseHistory") or 0,
                            "brand": row.get("Brand", ""),
                            "model": row.get("Model", ""),
                            "category": row.get("CategoryName", "") or row.get("Category", ""),
                            "title": row.get("Title", "") or row.get("Model", ""),
                        }
                return {}
            except Exception as e:
                logger.error(f"BinManager get_sku_stock error {sku}: {e}")
                if attempt == 0:
                    continue
                return {}
        return {}

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None
        self._logged_in = False
