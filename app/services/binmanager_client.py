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

    async def get_available_qty(self, sku: str) -> int:
        """Retorna AvailableQTY para un SKU filtrado a LocationID=47,62,68 (MTY+CDMX).
        Usa Get_GlobalStock_InventoryBySKU con el payload exacto que BM usa en su UI:
          - CONCEPTID=1, LOCATIONID="47,62,68", CONDITION="GRA,GRB,GRC,ICB,ICC,NEW"
        El campo AvailableQTY = TotalQty - Reserve (calculado por BM server-side).
        Verificado: SNTV001764 → TotalQty=214, Reserve=1, AvailableQTY=213.
        """
        if not self._logged_in:
            if not await self.login():
                return 0
        c = self._client()

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
            "CONDITION": "GRA,GRB,GRC,ICB,ICC,NEW",
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
                    if isinstance(data, list) and data:
                        # 1. Coincidencia exacta de SKU base (caso normal: item en condición NEW)
                        match = next(
                            (x for x in data if (x.get("SKU") or "").upper() == base.upper()),
                            None
                        )
                        if match is not None:
                            avail = match.get("AvailableQTY")
                            return int(avail) if avail is not None else 0
                        # 2. Sin match exacto: buscar variantes de condición del mismo base SKU.
                        #    Caso real: SNTV004196 solo existe en GRB → BM retorna "SNTV004196-GRB"
                        #    en el campo SKU, no "SNTV004196". Sin este fallback retornaría 0
                        #    aunque hay 14 unidades físicas → falsa alerta de sobreventa.
                        _COND_SFXS = ("-GRA", "-GRB", "-GRC", "-ICB", "-ICC", "-NEW")
                        variants = [
                            x for x in data
                            if (x.get("SKU") or "").upper().startswith(base.upper() + "-")
                            and any((x.get("SKU") or "").upper().endswith(s) for s in _COND_SFXS)
                        ]
                        if variants:
                            return sum(int(x.get("AvailableQTY") or 0) for x in variants)
                        return 0  # SKU no encontrado — NO caer al data[0] (puede ser otro SKU)
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
