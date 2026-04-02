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
                    json=payload, headers=_AJAX_HEADERS, timeout=45,
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
        # Use full base payload so BM returns all quantity/cost fields
        payload = {
            **_GS_BASE_PAYLOAD,
            "SEARCH": sku,
            "NEEDAVGCOST": True,
            "NEEDRETAILPRICE": True,
            "RECORDSPAGE": 10,
        }
        for attempt in range(2):
            try:
                r = await c.post(
                    f"{_BM_BASE}/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU",
                    json=payload, headers=_AJAX_HEADERS, timeout=20,
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
                        # BM field name varies by payload: TotalQty (global), QTY (per-SKU), QtyTotal (legacy)
                        stock = (row.get("TotalQty") or row.get("AvailableQTY")
                                 or row.get("QTY") or row.get("QtyTotal") or row.get("Qty") or 0)
                        return {
                            "stock": int(stock) if stock else 0,
                            "retail_price": row.get("RetailPrice") or row.get("LastRetailPricePurchaseHistory") or 0,
                            "avg_cost": row.get("AvgCostQTY") or 0,
                            "brand": row.get("BRAND") or row.get("Brand", ""),
                            "model": row.get("MODEL") or row.get("Model", ""),
                            "size": row.get("SIZE") or row.get("Size", ""),
                            "category": row.get("CategoryName", "") or row.get("Category", ""),
                            "title": row.get("Title", "") or row.get("MODEL") or row.get("Model", ""),
                        }
                return {}
            except Exception as e:
                logger.error(f"BinManager get_sku_stock error {sku}: {e}")
                if attempt == 0:
                    continue
                return {}
        return {}

    async def get_available_qty(self, sku: str) -> int:
        """Retorna stock vendible para un SKU (status 'Producto Vendible') en MTY+CDMX.
        Usa GlobalStock_InventoryBySKU_Condition con LocationID=47,62,68.
        Suma TotalQty donde status=='Producto Vendible' en Conditions_JSON.

        NOTA: Get_GlobalStock_InventoryBySKU con CONCEPTID=8 devuelve un contador
        contable que NO refleja stock físico real (e.g. 202 cuando hay 2 unidades).
        """
        import json as _json
        if not self._logged_in:
            if not await self.login():
                return 0
        c = self._client()

        # Extraer base SKU y condiciones según sufijo
        upper = sku.upper()
        base = sku
        for sfx in ("-ICB", "-ICC", "-NEW", "-GRA", "-GRB", "-GRC"):
            if upper.endswith(sfx):
                base = sku[:-len(sfx)]
                break
        if upper.endswith("-ICB") or upper.endswith("-ICC"):
            conditions = "GRA,GRB,GRC,ICB,ICC,NEW"
        else:
            conditions = "GRA,GRB,GRC,NEW"

        url = f"{_BM_BASE}/InventoryReport/InventoryReport/GlobalStock_InventoryBySKU_Condition"
        payload = {
            "COMPANYID": 1, "SKU": base, "WAREHOUSEID": None,
            "LOCATIONID": "47,62,68", "BINID": None,
            "CONDITION": conditions, "FORINVENTORY": 0, "SUPPLIERS": None,
        }
        for attempt in range(2):
            try:
                r = await c.post(url, json=payload, headers=_AJAX_HEADERS, timeout=20)
                if self._session_expired(r):
                    self._logged_in = False
                    if attempt == 0:
                        await self.login()
                        continue
                    return 0
                if r.status_code == 200:
                    data = r.json()
                    if not isinstance(data, list):
                        return 0
                    avail = 0
                    for row in data:
                        cj = row.get("Conditions_JSON") or []
                        if isinstance(cj, str):
                            try:
                                cj = _json.loads(cj)
                            except Exception:
                                cj = []
                        if isinstance(cj, list) and cj:
                            for cond in cj:
                                for item in (cond.get("SKUCondition_JSON") or []):
                                    qty = item.get("TotalQty", 0) or 0
                                    if item.get("status") == "Producto Vendible":
                                        avail += qty
                        else:
                            # Fallback: fila con status directo (estructura plana)
                            qty = row.get("TotalQty", 0) or 0
                            if row.get("status") == "Producto Vendible":
                                avail += qty
                    return avail
                return 0
            except httpx.TimeoutException:
                logger.warning(f"BinManager timeout get_available_qty {sku} (intento {attempt+1})")
                if attempt == 0:
                    continue
                return 0
            except Exception as e:
                logger.error(f"BinManager get_available_qty error {sku}: {e}")
                if attempt == 0:
                    continue
                return 0
        return 0

    async def post_inventory(self, url: str, payload: dict, timeout: float = 15.0):
        """POST autenticado a un endpoint de inventario BM. Maneja sesión expirada con re-login.
        Retorna response httpx o None si falla."""
        if not self._logged_in:
            if not await self.login():
                return None
        c = self._client()
        for attempt in range(2):
            try:
                r = await c.post(url, json=payload, headers=_AJAX_HEADERS, timeout=timeout)
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
