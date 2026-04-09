from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
import httpx
import asyncio
from app.services.meli_client import get_meli_client, MeliApiError
import app.main as _main_module


def _invalidate_user_products_cache(user_id: str):
    """Invalida las entradas de cache de productos para un usuario especifico."""
    prefix = f"{user_id}:"
    keys_to_del = [k for k in _main_module._products_cache if k.startswith(prefix)]
    for k in keys_to_del:
        del _main_module._products_cache[k]
    # Tambien limpiar sale_price_cache ya que precios cambian
    sp_keys = [k for k in _main_module._sale_price_cache if k.startswith(prefix)]
    for k in sp_keys:
        del _main_module._sale_price_cache[k]

BM_WAREHOUSE_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
BM_AVAIL_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"
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


def _parse_wh_rows_items(rows):
    """Suma QtyTotal por almacen. Retorna (mty, cdmx, tj)."""
    mty = cdmx = tj = 0
    for row in (rows or []):
        qty = row.get("QtyTotal", 0) or 0
        wname = (row.get("WarehouseName") or "").lower()
        if "monterrey" in wname or "maxx" in wname:
            mty += qty
        elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
            cdmx += qty
        else:
            tj += qty
    return mty, cdmx, tj


async def _bm_warehouse_qty(sku: str, client: httpx.AsyncClient) -> dict | None:
    """Consulta en paralelo:
    1) Warehouse endpoint → MTY/CDMX/TJ totales físicos
    2) get_available_qty → AvailableQTY real (Get_GlobalStock_InventoryBySKU, excluye reservados)
    """
    from app.services.binmanager_client import get_shared_bm
    base, _ = _get_base_and_type(sku)
    conditions = _bm_conditions(sku)
    wh_payload = {
        "COMPANYID": BM_COMPANY_ID, "SKU": base, "WarehouseID": None,
        "LocationID": BM_LOCATION_IDS, "BINID": None,
        "Condition": conditions, "SUPPLIERS": None, "ForInventory": 0,
    }
    try:
        bm_cli = await get_shared_bm()
        r_wh, avail_total = await asyncio.gather(
            client.post(BM_WAREHOUSE_URL, json=wh_payload, timeout=15.0),
            bm_cli.get_available_qty(base),
            return_exceptions=True,
        )
        rows_wh = r_wh.json() if not isinstance(r_wh, Exception) and r_wh.status_code == 200 else []
        if isinstance(avail_total, Exception): avail_total = 0
        mty, cdmx, tj = _parse_wh_rows_items(rows_wh)
        if mty + cdmx + tj > 0 or avail_total > 0:
            return {
                "MainQtyMTY": mty, "MainQtyCDMX": cdmx, "MainQtyTJ": tj,
                "AvailTotal": avail_total,
                "WebSKU": sku, "ProductSKU": base,
            }
    except Exception:
        pass
    return None


class PriceUpdate(BaseModel):
    price: float


class StockUpdate(BaseModel):
    quantity: int


class VariationStockUpdate(BaseModel):
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
    listing_type_id: Optional[str] = None


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
                score, problems, _ = _calculate_health_score(body)
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


@router.delete("/{item_id}")
async def close_item(item_id: str):
    """Cierra (finaliza) una publicacion de MeLi poniendo status=closed.
    MeLi no permite eliminar items via API; 'closed' es el estado final disponible.
    """
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item(item_id, {"status": "closed"})
        _invalidate_user_products_cache(str(client.user_id))
        return {"ok": True, "item_id": item_id, "status": "closed", "result": result}
    except MeliApiError as e:
        body = e.body
        detail = body.get("message") or body.get("error") or str(body) if isinstance(body, dict) else str(body)
        raise HTTPException(status_code=e.status_code, detail=f"MeLi: {detail}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


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
        _invalidate_user_products_cache(str(client.user_id))
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
        _invalidate_user_products_cache(str(client.user_id))
        # me1_warning: MeLi acepto el PUT pero puede revertir — devolver 200 con flag warning
        if isinstance(result, dict) and result.get("_me1_warning"):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=200, content={
                "ok": True,
                "warning": "me1",
                "message": (
                    "Stock actualizado en MeLi. ADVERTENCIA: este item usa cross_docking y "
                    "MeLi puede revertir el stock si ME1 (Mercado Envios) no esta habilitado. "
                    "Verifica en Seller Central que el cambio persiste."
                )
            })
        return result
    except MeliApiError as e:
        body = e.body
        if isinstance(body, dict):
            # full_item: item FULL — no se puede actualizar stock via API
            if body.get("error") == "full_item":
                raise HTTPException(status_code=400, detail=body.get("message", "logistic_type.not_modifiable"))
            detail = body.get("message") or body.get("error") or str(body)
        else:
            detail = str(body)
        raise HTTPException(status_code=e.status_code, detail=f"MeLi: {detail}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


@router.put("/{item_id}/variations/{variation_id}/stock")
async def update_variation_stock(item_id: str, variation_id: str, data: VariationStockUpdate):
    """Actualiza el stock de una variacion especifica sin afectar las demas."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_variation_stocks_directly(
            item_id, [{"id": int(variation_id), "available_quantity": data.quantity}]
        )
        _invalidate_user_products_cache(str(client.user_id))
        return {"ok": True, "item_id": item_id, "variation_id": variation_id, "quantity": data.quantity, "result": result}
    except MeliApiError as e:
        body = e.body
        detail = body.get("message") or body.get("error") or str(body) if isinstance(body, dict) else str(body)
        raise HTTPException(status_code=e.status_code, detail=f"MeLi: {detail}")
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
        _invalidate_user_products_cache(str(client.user_id))
        return result
    except MeliApiError as e:
        body = e.body
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or str(body)
        else:
            detail = str(body)
        raise HTTPException(status_code=e.status_code, detail=f"MeLi: {detail}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
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
        _invalidate_user_products_cache(str(client.user_id))
        return result
    except MeliApiError as e:
        body = e.body
        if isinstance(body, dict):
            causes = body.get("cause", [])
            cause_code = causes[0].get("code", "") if causes else ""
            cause_msg = causes[0].get("message", "") if causes else ""
            if cause_code == "item.shipping.logistic_type.not_modifiable" or "not_modifiable" in cause_msg:
                raise HTTPException(
                    status_code=422,
                    detail="logistic_type.not_modifiable: MeLi no permite cambiar la logistica de items FULL via API. Gestionalo desde Seller Central."
                )
            detail = body.get("message") or body.get("error") or cause_msg or str(body)
        else:
            detail = str(body)
        raise HTTPException(status_code=e.status_code, detail=f"MeLi: {detail}")
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

        # Listing type
        if data.listing_type_id is not None:
            valid_types = ("gold_pro", "gold_special", "gold_premium", "free")
            if data.listing_type_id not in valid_types:
                results["listing_type"] = {"ok": False, "error": f"Tipo invalido: {data.listing_type_id}"}
            else:
                try:
                    await client.update_item(item_id, {"listing_type_id": data.listing_type_id})
                    results["listing_type"] = {"ok": True}
                except Exception as e:
                    results["listing_type"] = {"ok": False, "error": str(e)}

        all_ok = all(r["ok"] for r in results.values())
        return {"ok": all_ok, "results": results}
    finally:
        await client.close()


def _calculate_health_score(body: dict, description: str = "") -> tuple:
    """Calcula health score (0-100), lista de problemas y breakdown detallado."""
    score = 100
    problems = []
    breakdown = []  # list of {label, impact, ok, tip}

    # Fotos
    pictures = body.get("pictures", [])
    n_pics = len(pictures)
    if n_pics == 0:
        score -= 50; problems.append("Sin fotos")
        breakdown.append({"label": "Fotos (0)", "impact": -50, "ok": False, "tip": "Agrega minimo 5 fotos"})
    elif n_pics < 5:
        score -= 30; problems.append(f"Solo {n_pics} fotos (min 5)")
        breakdown.append({"label": f"Fotos ({n_pics}/5)", "impact": -30, "ok": False, "tip": f"Faltan {5-n_pics} fotos"})
    elif n_pics < 8:
        score -= 15; problems.append(f"Solo {n_pics} fotos (ideal 8+)")
        breakdown.append({"label": f"Fotos ({n_pics}/8+)", "impact": -15, "ok": False, "tip": f"Agrega {8-n_pics} fotos mas"})
    else:
        breakdown.append({"label": f"Fotos ({n_pics})", "impact": 0, "ok": True, "tip": ""})

    # Video
    has_video = bool(body.get("video_id"))
    if not has_video:
        score -= 10; problems.append("Sin video clip")
    breakdown.append({"label": "Video/Clip ML", "impact": -10 if not has_video else 0, "ok": has_video,
                       "tip": "" if has_video else "Genera y sube un clip comercial", "key": "" if has_video else "video"})

    # Envio
    shipping = body.get("shipping", {})
    free = shipping.get("free_shipping", False) or shipping.get("logistic_type") == "fulfillment"
    if not free:
        score -= 15; problems.append("Sin envio gratis")
    breakdown.append({"label": "Envio gratis", "impact": -15 if not free else 0, "ok": free,
                       "tip": "" if free else "Activa envio gratis para mejor ranking"})

    # Estado
    is_paused = body.get("status") == "paused"
    if is_paused:
        score -= 40; problems.append("Publicacion pausada")
    breakdown.append({"label": "Estado activo", "impact": -40 if is_paused else 0, "ok": not is_paused,
                       "tip": "" if not is_paused else "Reactiva la publicacion"})

    # Stock
    qty = body.get("available_quantity", 0)
    if qty == 0:
        score -= 30; problems.append("Sin stock")
    breakdown.append({"label": f"Stock ({qty})", "impact": -30 if qty == 0 else 0, "ok": qty > 0,
                       "tip": "" if qty > 0 else "Agrega stock para reactivar"})

    # Titulo
    title = body.get("title", "")
    tlen = len(title)
    if tlen < 30:
        score -= 20; problems.append(f"Titulo muy corto ({tlen} chars)")
        breakdown.append({"label": f"Titulo ({tlen}/55 chars)", "impact": -20, "ok": False, "tip": "Usa el boton IA para generar un titulo SEO de 55-60 chars", "key": "title"})
    elif tlen < 45:
        score -= 10; problems.append(f"Titulo corto ({tlen} chars)")
        breakdown.append({"label": f"Titulo ({tlen}/55 chars)", "impact": -10, "ok": False, "tip": "Extiende el titulo a 55-60 chars con IA", "key": "title"})
    else:
        breakdown.append({"label": f"Titulo ({tlen} chars)", "impact": 0, "ok": True, "tip": "", "key": ""})

    # Descripcion
    desc_words = len(description.split()) if description and description.strip() else 0
    if desc_words < 50:
        score -= 10; problems.append(f"Descripcion muy corta ({desc_words} palabras)")
        breakdown.append({"label": f"Descripcion ({desc_words} palabras)", "impact": -10, "ok": False, "tip": "Genera descripcion con IA (min 200 palabras)", "key": "description"})
    elif desc_words < 150:
        score -= 5; problems.append(f"Descripcion corta ({desc_words} palabras)")
        breakdown.append({"label": f"Descripcion ({desc_words} palabras)", "impact": -5, "ok": False, "tip": "Ampliar descripcion a 200+ palabras mejora visibilidad", "key": "description"})
    else:
        breakdown.append({"label": f"Descripcion ({desc_words} palabras)", "impact": 0, "ok": True, "tip": "", "key": ""})

    # GTIN
    attrs = body.get("attributes", [])
    has_gtin = any(a.get("id") == "GTIN" and a.get("value_name") for a in attrs)
    if not has_gtin:
        score -= 10; problems.append("Sin GTIN (codigo de barras)")
    breakdown.append({"label": "GTIN (codigo barras)", "impact": -10 if not has_gtin else 0, "ok": has_gtin,
                       "tip": "" if has_gtin else "Agrega el codigo de barras EAN/UPC del producto"})

    # SELLER_SKU
    has_sku = (bool(body.get("seller_custom_field")) or
               any(a.get("id") == "SELLER_SKU" and a.get("value_name") for a in attrs))
    if not has_sku:
        score -= 5; problems.append("Sin SELLER_SKU")
    breakdown.append({"label": "SELLER_SKU", "impact": -5 if not has_sku else 0, "ok": has_sku,
                       "tip": "" if has_sku else "Agrega el SKU interno para sincronizacion con BinManager"})

    # Tipo de publicacion
    lt = body.get("listing_type_id", "")
    is_clasica = lt == "gold_special"
    if is_clasica:
        score -= 5; problems.append("Tipo Clasica (cambiar a Premium)")
    breakdown.append({"label": f"Tipo: {'Premium' if lt == 'gold_pro' else ('Clasica' if lt == 'gold_special' else lt)}",
                       "impact": -5 if is_clasica else 0, "ok": not is_clasica,
                       "tip": "" if not is_clasica else "Actualizar a gold_pro (Premium) para mejor exposicion y MSI"})

    return max(score, 0), problems, breakdown


def _classify_score(score: int) -> str:
    if score < 40:
        return "critico"
    elif score <= 70:
        return "necesita_trabajo"
    return "bueno"
