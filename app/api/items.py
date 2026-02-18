from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
import httpx
import asyncio
from app.services.meli_client import get_meli_client

BM_WAREHOUSE_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
BM_COMPANY_ID = 1
BM_LOCATION_IDS = "47,62,68"
BM_CONDITIONS_GR = "GRA,GRB,GRC,NEW"           # SKUs sin sufijo IC: solo condicion buena
BM_CONDITIONS_ALL = "GRA,GRB,GRC,ICB,ICC,NEW"  # SKUs con sufijo ICB/ICC: todas las condiciones

router = APIRouter(prefix="/api/items", tags=["items"])


def _bm_conditions(sku: str) -> str:
    """Retorna el string de condiciones BM segun el sufijo del SKU.
    SKUs publicados como ICB/ICC incluyen todo el stock.
    SKUs normales (GR o sin sufijo) excluyen ICB/ICC — son producto dañado, no vendible.
    """
    upper = sku.upper()
    if upper.endswith("-ICB") or upper.endswith("-ICC"):
        return BM_CONDITIONS_ALL
    return BM_CONDITIONS_GR


async def _bm_warehouse_qty(sku: str, client: httpx.AsyncClient) -> dict | None:
    """Consulta stock real de BinManager via Warehouse endpoint.
    Usa condiciones GR-only para SKUs normales, todas las condiciones para SKUs IC.
    Retorna dict con MainQtyMTY, MainQtyCDMX, MainQtyTJ (para compatibilidad con templates).
    """
    base, _ = _get_base_and_type(sku)
    payload = {
        "COMPANYID": BM_COMPANY_ID,
        "SKU": base,
        "WarehouseID": None,
        "LocationID": BM_LOCATION_IDS,
        "BINID": None,
        "Condition": _bm_conditions(sku),
        "ForInventory": 0,
        "SUPPLIERS": None,
    }
    try:
        resp = await client.post(BM_WAREHOUSE_URL, json=payload, timeout=15.0)
        if resp.status_code == 200:
            rows = resp.json() or []
            mty = cdmx = tj = 0
            for row in rows:
                qty = row.get("QtyTotal", 0) or 0
                wname = (row.get("WarehouseName") or "").lower()
                if "monterrey" in wname or "maxx" in wname:
                    mty += qty
                elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                    cdmx += qty
                else:
                    tj += qty
            if mty + cdmx + tj > 0:
                return {"MainQtyMTY": mty, "MainQtyCDMX": cdmx, "MainQtyTJ": tj,
                        "WebSKU": sku, "ProductSKU": base}
    except Exception:
        pass
    return None


class PriceUpdate(BaseModel):
    price: float


class StockUpdate(BaseModel):
    quantity: int


class TitleUpdate(BaseModel):
    title: str


class DescriptionUpdate(BaseModel):
    plain_text: str


class StatusUpdate(BaseModel):
    status: str  # active | paused


class ShippingUpdate(BaseModel):
    free_shipping: Optional[bool] = None
    local_pick_up: Optional[bool] = None
    logistic_type: Optional[str] = None


class PicturesUpdate(BaseModel):
    pictures: list


class AttributesUpdate(BaseModel):
    attributes: list


class GenericUpdate(BaseModel):
    updates: dict


class BatchUpdate(BaseModel):
    title: Optional[str] = None
    plain_text: Optional[str] = None
    price: Optional[float] = None
    quantity: Optional[int] = None
    status: Optional[str] = None
    free_shipping: Optional[bool] = None
    pictures: Optional[list] = None
    attributes: Optional[list] = None


@router.get("")
async def get_items(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    status: str = Query("active")
):
    """Lista los items del vendedor con paginacion."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        # Obtener IDs de items
        items_search = await client.get_items(offset=offset, limit=limit, status=status)
        item_ids = items_search.get("results", [])

        if not item_ids:
            return {"results": [], "paging": items_search.get("paging", {})}

        # Obtener detalles de los items (en batches de 20)
        all_items = []
        for i in range(0, len(item_ids), 20):
            batch_ids = item_ids[i:i + 20]
            items_details = await client.get_items_details(batch_ids)
            all_items.extend(items_details)

        return {
            "results": all_items,
            "paging": items_search.get("paging", {})
        }
    finally:
        await client.close()


@router.get("/needs-work")
async def get_items_needs_work():
    """Obtiene items con health score bajo."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        all_item_ids = []
        for status in ["active", "paused"]:
            offset = 0
            while True:
                try:
                    items_data = await client.get_items(offset=offset, limit=50, status=status)
                except Exception:
                    break
                ids = items_data.get("results", [])
                if not ids:
                    break
                all_item_ids.extend(ids)
                total = items_data.get("paging", {}).get("total", 0)
                offset += 50
                if offset >= total:
                    break

        items_with_scores = []
        for i in range(0, len(all_item_ids), 20):
            batch = all_item_ids[i:i+20]
            try:
                details = await client.get_items_details(batch)
            except Exception:
                continue
            for item in details:
                body = item.get("body", item)
                if not body or not body.get("id"):
                    continue
                score, problems = _calculate_health_score(body)
                # Extract SELLER_SKU
                seller_sku = body.get("seller_custom_field") or ""
                if not seller_sku and body.get("attributes"):
                    for attr in body["attributes"]:
                        if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                            seller_sku = attr["value_name"]
                            break
                if not seller_sku and body.get("variations"):
                    for var in body["variations"]:
                        if var.get("seller_custom_field"):
                            seller_sku = var["seller_custom_field"]
                            break
                items_with_scores.append({
                    "id": body.get("id", ""),
                    "title": body.get("title", "-"),
                    "thumbnail": body.get("thumbnail", ""),
                    "price": body.get("price", 0),
                    "status": body.get("status", "-"),
                    "available_quantity": body.get("available_quantity", 0),
                    "sold_quantity": body.get("sold_quantity", 0),
                    "score": score,
                    "problems": problems,
                    "category": _classify_score(score),
                    "pictures_count": len(body.get("pictures", [])),
                    "has_video": bool(body.get("video_id")),
                    "free_shipping": body.get("shipping", {}).get("free_shipping", False),
                    "permalink": body.get("permalink", ""),
                    "seller_sku": seller_sku,
                })

        items_with_scores.sort(key=lambda x: x["score"])
        return {"items": items_with_scores}
    finally:
        await client.close()


@router.get("/inventory-bulk")
async def get_inventory_bulk(skus: str = Query(..., description="Comma-separated SKUs")):
    """Consulta inventario BinManager para multiples SKUs en paralelo.
    Usa MainQty (dato real). Si la consulta directa falla, intenta con sufijos vendibles.
    """
    sku_list = [s.strip() for s in skus.split(",") if s.strip()]
    if not sku_list:
        return {}

    async def fetch_one(sku: str, client: httpx.AsyncClient):
        """Consulta BM Warehouse endpoint. Condiciones segun sufijo del SKU."""
        data = await _bm_warehouse_qty(sku, client)
        return sku, data

    results = {}
    async with httpx.AsyncClient() as client:
        tasks = [fetch_one(sku, client) for sku in sku_list]
        for coro in asyncio.as_completed(tasks):
            sku, data = await coro
            if data:
                results[sku] = data
    return results


GR_SUFFIXES = ["-NEW", "-GRA", "-GRB", "-GRC"]
IC_SUFFIXES = ["-ICB", "-ICC"]
ALL_SUFFIXES = GR_SUFFIXES + IC_SUFFIXES


def _get_base_and_type(sku: str):
    """Retorna (base_sku, 'ic'|'gr') segun el sufijo."""
    upper = sku.upper()
    for sfx in IC_SUFFIXES:
        if upper.endswith(sfx):
            return sku[:-len(sfx)], "ic"
    for sfx in GR_SUFFIXES:
        if upper.endswith(sfx):
            return sku[:-len(sfx)], "gr"
    return sku, "gr"  # sin sufijo = tratar como GR


@router.get("/inventory-sku-sales")
async def get_inventory_sku_sales(skus: str = Query(..., description="Comma-separated SKUs from sales")):
    """Consulta inventario por locacion para SKUs de ventas.

    BinManager ya devuelve los totales agregados de todas las variantes
    para cualquier SKU que se consulte, asi que solo necesitamos UNA
    consulta por SKU (no por cada variante).
    """
    sku_list = [s.strip() for s in skus.split(",") if s.strip()]
    if not sku_list:
        return {}

    # Deduplicar por base para no consultar el mismo producto varias veces
    base_to_skus = {}  # base -> [original_skus]
    for sku in sku_list:
        base, _ = _get_base_and_type(sku)
        base_to_skus.setdefault(base, []).append(sku)

    # Consultar UNA sola vez por base SKU (usando el propio SKU tal cual)
    sem = asyncio.Semaphore(10)

    async def fetch_one(query_sku: str, client: httpx.AsyncClient):
        async with sem:
            data = await _bm_warehouse_qty(query_sku, client)
            return query_sku, data

    base_data = {}
    async with httpx.AsyncClient() as client:
        tasks = [fetch_one(sku_list_for_base[0], client)
                 for sku_list_for_base in base_to_skus.values()]
        for coro in asyncio.as_completed(tasks):
            queried_sku, data = await coro
            if data:
                base, _ = _get_base_and_type(queried_sku)
                base_data[base] = data

    # Asignar resultado a cada SKU original
    results = {}
    for base, original_skus in base_to_skus.items():
        d = base_data.get(base)
        inv = {
            "MTY": (d.get("MainQtyMTY", 0) or 0) if d else 0,
            "CDMX": (d.get("MainQtyCDMX", 0) or 0) if d else 0,
            "TJ": (d.get("MainQtyTJ", 0) or 0) if d else 0,
        }
        for sku in original_skus:
            results[sku] = inv

    return results


@router.get("/inventory/{web_sku}")
async def get_inventory(web_sku: str):
    """Consulta inventario BinManager para un SKU via Warehouse endpoint (stock real).
    SKUs con sufijo ICB/ICC incluyen todo el stock. SKUs GR/sin sufijo excluyen ICB/ICC.
    """
    try:
        async with httpx.AsyncClient() as client:
            data = await _bm_warehouse_qty(web_sku, client)
            if data:
                return data
            return {"error": "SKU no encontrado en BinManager", "WebSKU": web_sku,
                    "MainQtyMTY": 0, "MainQtyCDMX": 0, "MainQtyTJ": 0}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando BinManager: {str(e)}")


@router.get("/{item_id}")
async def get_item(item_id: str):
    """Obtiene el detalle de un item especifico."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        item = await client.get_item(item_id)
        return item
    finally:
        await client.close()


@router.put("/{item_id}/price")
async def update_price(item_id: str, data: PriceUpdate):
    """Actualiza el precio de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        result = await client.update_item_price(item_id, data.price)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


@router.put("/{item_id}/stock")
async def update_stock(item_id: str, data: StockUpdate):
    """Actualiza el stock de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        result = await client.update_item_stock(item_id, data.quantity)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


@router.put("/{item_id}/title")
async def update_title(item_id: str, data: TitleUpdate):
    """Actualiza el titulo de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_title(item_id, data.title)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


@router.put("/{item_id}/description")
async def update_description(item_id: str, data: DescriptionUpdate):
    """Actualiza la descripcion de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_description(item_id, data.plain_text)
        return result
    finally:
        await client.close()


@router.put("/{item_id}/status")
async def update_status(item_id: str, data: StatusUpdate):
    """Cambia el estado de un item (active/paused)."""
    if data.status not in ("active", "paused"):
        raise HTTPException(status_code=400, detail="Status debe ser 'active' o 'paused'")
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_status(item_id, data.status)
        return result
    finally:
        await client.close()


@router.put("/{item_id}/shipping")
async def update_shipping(item_id: str, data: ShippingUpdate):
    """Actualiza configuracion de envio."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        shipping = {}
        if data.free_shipping is not None:
            shipping["free_shipping"] = data.free_shipping
        if data.local_pick_up is not None:
            shipping["local_pick_up"] = data.local_pick_up
        if data.logistic_type is not None:
            shipping["logistic_type"] = data.logistic_type
        result = await client.update_item_shipping(item_id, shipping)
        return result
    finally:
        await client.close()


@router.put("/{item_id}/pictures")
async def update_pictures(item_id: str, data: PicturesUpdate):
    """Actualiza las fotos de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_pictures(item_id, data.pictures)
        return result
    finally:
        await client.close()


@router.put("/{item_id}/attributes")
async def update_attributes(item_id: str, data: AttributesUpdate):
    """Actualiza los atributos de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_attributes(item_id, data.attributes)
        return result
    finally:
        await client.close()


@router.put("/{item_id}/update")
async def update_item_generic(item_id: str, data: GenericUpdate):
    """Actualiza campos genericos de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item(item_id, data.updates)
        return result
    finally:
        await client.close()


@router.put("/{item_id}/batch")
async def batch_update_item(item_id: str, data: BatchUpdate):
    """Actualiza multiples campos de un item en una sola peticion."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    results = {}
    try:
        # Title
        if data.title is not None:
            try:
                await client.update_item_title(item_id, data.title)
                results["title"] = {"ok": True}
            except Exception as e:
                results["title"] = {"ok": False, "error": str(e)}

        # Description (separate endpoint)
        if data.plain_text is not None:
            try:
                await client.update_item_description(item_id, data.plain_text)
                results["description"] = {"ok": True}
            except Exception as e:
                results["description"] = {"ok": False, "error": str(e)}

        # Price
        if data.price is not None:
            try:
                await client.update_item_price(item_id, data.price)
                results["price"] = {"ok": True}
            except Exception as e:
                results["price"] = {"ok": False, "error": str(e)}

        # Stock
        if data.quantity is not None:
            try:
                await client.update_item_stock(item_id, data.quantity)
                results["quantity"] = {"ok": True}
            except Exception as e:
                results["quantity"] = {"ok": False, "error": str(e)}

        # Status
        if data.status is not None:
            if data.status not in ("active", "paused"):
                results["status"] = {"ok": False, "error": "Status debe ser 'active' o 'paused'"}
            else:
                try:
                    await client.update_item_status(item_id, data.status)
                    results["status"] = {"ok": True}
                except Exception as e:
                    results["status"] = {"ok": False, "error": str(e)}

        # Shipping
        if data.free_shipping is not None:
            try:
                await client.update_item_shipping(item_id, {"free_shipping": data.free_shipping})
                results["shipping"] = {"ok": True}
            except Exception as e:
                results["shipping"] = {"ok": False, "error": str(e)}

        # Pictures
        if data.pictures is not None:
            try:
                await client.update_item_pictures(item_id, data.pictures)
                results["pictures"] = {"ok": True}
            except Exception as e:
                results["pictures"] = {"ok": False, "error": str(e)}

        # Attributes
        if data.attributes is not None:
            try:
                await client.update_item_attributes(item_id, data.attributes)
                results["attributes"] = {"ok": True}
            except Exception as e:
                results["attributes"] = {"ok": False, "error": str(e)}

        all_ok = all(r["ok"] for r in results.values())
        return {"ok": all_ok, "results": results}
    finally:
        await client.close()


def _calculate_health_score(body: dict) -> tuple:
    """Calcula health score (0-100) y lista de problemas."""
    score = 100
    problems = []

    pictures = body.get("pictures", [])
    if len(pictures) == 0:
        score -= 50
        problems.append("Sin fotos")
    elif len(pictures) < 5:
        score -= 30
        problems.append(f"Solo {len(pictures)} fotos (min 5)")
    elif len(pictures) < 8:
        score -= 15
        problems.append(f"Solo {len(pictures)} fotos (ideal 8+)")

    if not body.get("video_id"):
        score -= 10
        problems.append("Sin video")

    shipping = body.get("shipping", {})
    free = shipping.get("free_shipping", False) or shipping.get("logistic_type") == "fulfillment"
    if not free:
        score -= 15
        problems.append("Sin envio gratis")

    if body.get("status") == "paused":
        score -= 40
        problems.append("Pausado")

    if body.get("available_quantity", 0) == 0:
        score -= 30
        problems.append("Sin stock")

    title = body.get("title", "")
    if len(title) < 30:
        score -= 20
        problems.append(f"Titulo corto ({len(title)} chars)")

    return max(score, 0), problems


def _classify_score(score: int) -> str:
    if score < 40:
        return "critico"
    elif score <= 70:
        return "necesita_trabajo"
    return "bueno"
