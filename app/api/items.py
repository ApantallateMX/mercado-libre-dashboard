from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from typing import Optional, List
import httpx
import asyncio
from app.services.meli_client import get_meli_client, MeliApiError
from app.services import user_store as _user_store
from app.services import token_store as _token_store
import app.main as _main_module


def _derive_section(path: str) -> str:
    """Mapea un URL path a un nombre de sección legible para el audit log."""
    p = path.lower()
    if any(x in p for x in ("/items", "/products", "/lanzar", "/sin-lanzar")):
        return "Productos"
    if any(x in p for x in ("/orders", "orders-table")):
        return "Órdenes"
    if "/ads" in p:
        return "Ads"
    if any(x in p for x in ("/health", "/claims")):
        return "Reclamos"
    if "/returns" in p:
        return "Devoluciones"
    if any(x in p for x in ("/stock", "/inventory", "/distribucion")):
        return "Stock"
    if "/amazon" in p:
        return "Amazon"
    if any(x in p for x in ("/users", "/auditoria")):
        return "Admin"
    if any(x in p for x in ("/billing", "/facturacion")):
        return "Facturación"
    return "Dashboard"


async def _get_ml_account_name(account_id: str) -> str:
    """Devuelve el nickname de ML dado el user_id numérico. Consulta DB local."""
    if not account_id:
        return ""
    try:
        import aiosqlite as _aio
        from app.config import DATABASE_PATH as _DB
        async with _aio.connect(_DB) as db:
            row = await (await db.execute(
                "SELECT nickname FROM tokens WHERE user_id = ?", (account_id,)
            )).fetchone()
        return row[0] if row else account_id
    except Exception:
        return account_id


async def _audit(request: Request, action: str, item_id: str = None, detail: dict = None):
    """Fire-and-forget audit log. Nunca interrumpe la respuesta principal."""
    try:
        du = getattr(request.state, "dashboard_user", None)
        if du:
            account_id = request.cookies.get("active_account_id", "")
            ml_account = await _get_ml_account_name(account_id)
            section = _derive_section(request.url.path)
            await _user_store.log_action(
                username=du["username"],
                user_id=du.get("id"),
                action=action,
                item_id=item_id,
                detail=detail,
                ip=request.headers.get("X-Forwarded-For", request.client.host if request.client else None),
                ml_account=ml_account,
                section=section,
            )
    except Exception:
        pass


def _get_changed_by(request: Request) -> str:
    du = getattr(request.state, "dashboard_user", None)
    return du["username"] if du else ""


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


def _bm_catalog_brand_model(main_mod, base: str) -> dict:
    """Busca Brand/Model en los bulks ya cacheados en memoria (GR/ALL/TJ) --
    sin llamadas nuevas a BM. FIX 2026-08-10: Jovan pidio priorizar el
    Brand/Model REAL de BM sobre lo que la IA adivine para esos atributos
    (la IA a veces sugiere algo distinto a lo que BM ya sabe con certeza)."""
    for _cache_name in ("_bm_bulk_gr_cache", "_bm_bulk_all_cache", "_bm_bulk_loctj_cache"):
        _c = getattr(main_mod, _cache_name, None) if main_mod else None
        if not _c:
            continue
        for _row in _c[1]:
            _row_base, _ = _get_base_and_type((_row.get("SKU") or "").upper())
            if _row_base == base:
                _brand = (_row.get("Brand") or "").strip()
                _model = (_row.get("Model") or "").strip()
                if _brand or _model:
                    return {"Brand": _brand, "Model": _model}
    return {}


async def _bm_warehouse_qty(sku: str) -> dict | None:
    """Stock BM desde caché en memoria (prewarm). Sin llamadas HTTP."""
    import sys as _sys
    _main = _sys.modules.get("app.main")
    _cache = getattr(_main, "_bm_stock_cache", {}) if _main else {}
    base, _ = _get_base_and_type(sku)
    if not base:
        return None
    _bm_catalog = _bm_catalog_brand_model(_main, base.upper())
    entry = _cache.get(base.upper())
    if entry:
        _, data = entry
        mty   = data.get("mty", 0)
        cdmx  = data.get("cdmx", 0)
        tj    = data.get("tj", 0)
        avail = data.get("avail_total", 0)
        if mty + cdmx + tj > 0 or avail > 0:
            return {
                "MainQtyMTY": mty, "MainQtyCDMX": cdmx, "MainQtyTJ": tj,
                "AvailTotal": avail,
                "WebSKU": sku, "ProductSKU": base,
                **_bm_catalog,
            }
    # FIX 2026-08-10: Jovan reporto (con screenshot de BM) SNMC000525 mostrando
    # "SKU no encontrado en BinManager" cuando SI existe -- confirmado con
    # binmanager-specialist: 40 unidades reales, pero en LocationID 42
    # (Tijuana/MITIJ), excluida del stock vendible desde 2026-08-05. El SKU
    # nunca entra a _bm_stock_cache (que solo indexa el bulk de 47/62/68), asi
    # que el mensaje "no encontrado" es incorrecto -- deberia decir "existe,
    # pero solo tiene stock en Tijuana (no vendible online)". Se agrega un
    # fallback de solo-lectura contra el bulk de Tijuana YA CACHEADO en
    # memoria (mismo bulk que ya se usa para el desglose MTY/CDMX/TJ de todos
    # los SKUs, no una llamada nueva a BM) para distinguir "no existe" de
    # "existe, solo en Tijuana".
    _tj_cache = getattr(_main, "_bm_bulk_loctj_cache", None) if _main else None
    if _tj_cache:
        for _row in _tj_cache[1]:
            _row_sku = (_row.get("SKU") or "").upper()
            _row_base, _ = _get_base_and_type(_row_sku)
            if _row_base == base.upper():
                _tj_qty = int(_row.get("AvailableQTY") or 0)
                if _tj_qty > 0:
                    return {
                        "MainQtyMTY": 0, "MainQtyCDMX": 0, "MainQtyTJ": _tj_qty,
                        "AvailTotal": 0, "WebSKU": sku, "ProductSKU": base,
                        "tj_only": True,
                        **_bm_catalog,
                    }
                break
    if _bm_catalog:
        # Ni sellable ni Tijuana tienen stock, pero SI conocemos Brand/Model
        # por catalogo -- util para prellenar atributos aunque no haya stock.
        return {
            "MainQtyMTY": 0, "MainQtyCDMX": 0, "MainQtyTJ": 0, "AvailTotal": 0,
            "WebSKU": sku, "ProductSKU": base, "catalog_only": True,
            **_bm_catalog,
        }
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

        # _price_comp_cache solo cubre top 20 por ventas (ver dashboard_price_competition
        # en main.py) -- no hay dato para el resto del catalogo, y no se llama a la API
        # de precios por item aqui (seria cientos de llamadas extra en este loop bulk).
        _price_delta_by_id: dict = {}
        _pc_entry = _main_module._price_comp_cache.get(str(client.user_id))
        if _pc_entry:
            for _pi in _pc_entry.get("data", {}).get("items", []):
                if _pi.get("id") and _pi.get("delta_pct") is not None:
                    _price_delta_by_id[_pi["id"]] = _pi["delta_pct"]

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
                score, problems, _ = _calculate_health_score(
                    body, price_delta_pct=_price_delta_by_id.get(body["id"])
                )
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

    results = {}
    for sku in sku_list:
        data = await _bm_warehouse_qty(sku)
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

    # Consultar UNA sola vez por base SKU (usando el propio SKU tal cual) — serializado via bm_post
    async def fetch_one(query_sku: str):
        data = await _bm_warehouse_qty(query_sku)
        return query_sku, data

    base_data = {}
    tasks = [fetch_one(sku_list_for_base[0]) for sku_list_for_base in base_to_skus.values()]
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
        data = await _bm_warehouse_qty(web_sku)
        if data:
            if data.get("tj_only"):
                data["warning"] = (
                    f"Existe en BinManager con {data['MainQtyTJ']} uds, pero SOLO en Tijuana "
                    "(reabastecimiento, no vendible online) — 0 unidades vendibles en CDMX/MTY."
                )
            return data
        # FIX 2026-08-10: los caches en memoria (vendible + bulk Tijuana) solo
        # cubren los almacenes de las reglas de negocio de "vendible" — un SKU
        # con stock real en cualquier OTRO almacén de BM (hay ~20 activos) caía
        # aquí como falso "no encontrado". Jovan lo verificó directamente en la
        # UI de BM. Este panel es puramente informativo (no decide cuánto
        # publicar en ML), así que como último recurso hacemos UNA consulta en
        # vivo sin restricción de ubicación/condición — ver
        # get_existence_anywhere() en binmanager_client.py. Es on-demand (un
        # click), nunca se usa en los paths bulk (get_inventory_bulk/
        # get_inventory_sku_sales) para no meter llamadas HTTP en loops.
        from app.services.binmanager_client import get_shared_bm
        _base, _ = _get_base_and_type(web_sku)
        bm_cli = await get_shared_bm()
        anywhere = await bm_cli.get_existence_anywhere(_base or web_sku)
        if anywhere and anywhere.get("found_anywhere"):
            return {
                "WebSKU": web_sku, "ProductSKU": _base or web_sku,
                "MainQtyMTY": 0, "MainQtyCDMX": 0, "MainQtyTJ": 0, "AvailTotal": 0,
                "found_elsewhere": True,
                "total_qty_elsewhere": anywhere.get("total_qty", 0),
                "by_condition": anywhere.get("by_condition", []),
                "locations": anywhere.get("locations", []),
                "warning": (
                    f"Existe en BinManager con {anywhere.get('total_qty', 0)} uds, pero en "
                    "almacenes fuera de las reglas de venta en línea (no vendible/no Tijuana) "
                    "— 0 unidades vendibles."
                ),
            }
        # anywhere puede ser None (fallo de red, dato desconocido) o
        # found_anywhere=False (BM no valida contra su maestro de catálogo en
        # este endpoint — no se puede distinguir "existe con 0 stock en todo
        # lado" de "SKU nunca registrado", ver get_existence_anywhere()).
        return {"error": "Sin stock registrado en BinManager en ningún almacén ahora mismo "
                          "(no se puede confirmar si el SKU está o no dado de alta en BM)",
                "WebSKU": web_sku, "MainQtyMTY": 0, "MainQtyCDMX": 0, "MainQtyTJ": 0}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando BinManager: {str(e)}")


class ExtractImagesBody(BaseModel):
    url: str


@router.post("/extract-images")
async def extract_images(body: ExtractImagesBody):
    """Extrae URLs de fotos candidatas de una pagina externa que el usuario
    indica (2026-08-10, pedido por Jovan: en vez de pagar una API de busqueda
    de imagenes, el pega el link de una pagina que YA tiene las fotos reales
    del producto -- fabricante, Home Depot, Amazon, etc -- y las tomamos de
    ahi). Solo lectura de la pagina externa, nada se sube a ML hasta que el
    usuario elija cuales fotos agregar desde el modal."""
    url = (body.url or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL invalida — debe empezar con http:// o https://")
    from app.services.product_researcher import extract_page_images
    try:
        images = await extract_page_images(url)
        if not images:
            return {"images": [], "message": "No se encontraron imagenes en esa pagina"}
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error leyendo la pagina: {str(e)}")


class SearchImagesBody(BaseModel):
    query: str


@router.post("/search-images")
async def search_images(body: SearchImagesBody):
    """Busca fotos candidatas de un producto POR TEXTO (titulo o marca+modelo),
    sin que el usuario tenga que pegar ningun link (2026-08-10, pedido por
    Jovan: reusar el mismo mecanismo gratuito -- DuckDuckGo + scrapeo -- que
    YA usa el Wizard de nueva publicacion en research_product(), en vez de
    depender solo de la busqueda manual por URL de arriba). Solo lectura,
    sin costo de API, nada se sube a ML hasta que el usuario elija fotos."""
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query requerido")
    from app.services.product_researcher import search_product_images
    try:
        images = await search_product_images(query)
        if not images:
            return {"images": [], "message": "No se encontraron imagenes para esa busqueda"}
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error buscando imagenes: {str(e)}")


@router.delete("/{item_id}")
async def close_item(item_id: str, request: Request):
    """Cierra (finaliza) una publicacion de MeLi poniendo status=closed.
    MeLi no permite eliminar items via API; 'closed' es el estado final disponible.
    """
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item(item_id, {"status": "closed"})
        _invalidate_user_products_cache(str(client.user_id))
        await _audit(request, "ml_item_closed", item_id)
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
async def update_price(item_id: str, data: PriceUpdate, request: Request):
    """Actualiza el precio de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        result = await client.update_item_price(item_id, data.price)
        _invalidate_user_products_cache(str(client.user_id))
        await _audit(request, "ml_price_update", item_id, {"price": data.price})
        asyncio.create_task(_token_store.save_item_change(
            item_id, str(client.user_id), "price", str(data.price), changed_by=_get_changed_by(request)
        ))
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


@router.put("/{item_id}/stock")
async def update_stock(item_id: str, data: StockUpdate, request: Request):
    """Actualiza el stock de un item.

    FIX 2026-08-08 (barrido final de fuentes duplicadas): esta ruta y
    `main.py:update_item_stock_api` (mismo path `PUT /api/items/{id}/stock`)
    eran DOS registros para el mismo endpoint -- FastAPI hace first-match-wins
    por orden de registro, y como este router se incluye ANTES que el
    decorador directo de main.py, ESTA versión siempre ganaba. La de
    main.py (nunca ejecutada, código muerto) tenía protección BM-caído,
    evicción quirúrgica del item de _stock_issues_cache (para que
    desaparezca de las alertas de inmediato, no hasta el próximo prewarm
    de ~15min), auto-reactivación si estaba pausado por sin-stock, y
    limpieza de sync_alert -- nada de eso corría nunca. Se porta aquí y
    se elimina el duplicado en main.py."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    # Protección BM caído: bloquear qty=0 cuando BM está confirmado down.
    # Evita que un trabajador ponga listings en 0 basándose en alertas
    # falsas generadas por datos stale de BM. qty>0 siempre se permite.
    if data.quantity == 0 and _main_module._bm_is_confirmed_down():
        await client.close()
        raise HTTPException(
            status_code=503,
            detail="BinManager está caído. No se puede poner en 0 hasta que BM responda — las alertas pueden ser incorrectas.",
        )

    try:
        result = await client.update_item_stock(item_id, data.quantity)
        uid = str(client.user_id)
        _invalidate_user_products_cache(uid)
        await _audit(request, "ml_stock_update", item_id, {"qty": data.quantity})
        asyncio.create_task(_token_store.save_item_change(
            item_id, uid, "stock", str(data.quantity), changed_by=_get_changed_by(request)
        ))
        asyncio.create_task(_token_store.update_ml_listing_qty(item_id, data.quantity))
        asyncio.create_task(_main_module._safe_bg(
            _token_store.save_item_sync(item_id, uid, data.quantity), "save_item_sync/update_stock"
        ))
        # Si el listing quedó pausado por out_of_stock, reactivarlo.
        asyncio.create_task(_main_module._reactivate_if_oos_bg(item_id, uid))
        # Evicción inmediata de _stock_issues_cache -- sin esto, el item
        # seguía apareciendo en Reabastecer/Riesgo/Activar hasta el
        # próximo prewarm (~15min) aunque el stock ya estuviera corregido.
        _main_module._evict_item_from_alerts(uid, item_id)
        if data.quantity == 0:
            asyncio.create_task(_token_store.delete_sync_alert(uid, item_id))
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
    except ValueError as e:
        # Item tiene variaciones y no se pudo resolver automáticamente --
        # rechazar con flag explícito (el frontend ya evita este caso
        # pre-chequeando has_variations, pero por robustez ante datos
        # stale se preserva el flag en la respuesta).
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"ok": False, "has_variations": True, "detail": str(e)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


@router.put("/{item_id}/variations/{variation_id}/stock")
async def update_variation_stock(item_id: str, variation_id: str, data: VariationStockUpdate, request: Request):
    """Actualiza el stock de una variacion especifica sin afectar las demas."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_variation_stocks_directly(
            item_id, [{"id": int(variation_id), "available_quantity": data.quantity}]
        )
        _invalidate_user_products_cache(str(client.user_id))
        await _audit(request, "ml_variation_stock", item_id, {"variation_id": variation_id, "qty": data.quantity})
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
async def update_title(item_id: str, data: TitleUpdate, request: Request):
    """Actualiza el titulo de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_title(item_id, data.title)
        await _audit(request, "ml_title_update", item_id, {"title": data.title[:80]})
        asyncio.create_task(_token_store.save_item_change(
            item_id, str(client.user_id), "title", data.title[:200], changed_by=_get_changed_by(request)
        ))
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await client.close()


@router.put("/{item_id}/description")
async def update_description(item_id: str, data: DescriptionUpdate, request: Request):
    """Actualiza la descripcion de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_description(item_id, data.plain_text)
        await _audit(request, "ml_description_update", item_id)
        asyncio.create_task(_token_store.save_item_change(
            item_id, str(client.user_id), "description",
            (data.plain_text[:200] + "…") if len(data.plain_text) > 200 else data.plain_text,
            changed_by=_get_changed_by(request)
        ))
        return result
    finally:
        await client.close()


@router.put("/{item_id}/status")
async def update_status(item_id: str, data: StatusUpdate, request: Request):
    """Cambia el estado de un item (active/paused)."""
    if data.status not in ("active", "paused"):
        raise HTTPException(status_code=400, detail="Status debe ser 'active' o 'paused'")
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_status(item_id, data.status)
        _invalidate_user_products_cache(str(client.user_id))
        await _audit(request, "ml_status_update", item_id, {"status": data.status})
        asyncio.create_task(_token_store.save_item_change(
            item_id, str(client.user_id), "status", data.status, changed_by=_get_changed_by(request)
        ))
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
async def update_shipping(item_id: str, data: ShippingUpdate, request: Request):
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
        await _audit(request, "ml_shipping_update", item_id, {k: v for k, v in shipping.items() if v is not None})
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
async def update_pictures(item_id: str, data: PicturesUpdate, request: Request):
    """Actualiza las fotos de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_pictures(item_id, data.pictures)
        await _audit(request, "ml_pictures_update", item_id, {"count": len(data.pictures)})
        return result
    finally:
        await client.close()


@router.put("/{item_id}/attributes")
async def update_attributes(item_id: str, data: AttributesUpdate, request: Request):
    """Actualiza los atributos de un item."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.update_item_attributes(item_id, data.attributes)
        await _audit(request, "ml_attributes_update", item_id)
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


@router.get("/{item_id}/history")
async def get_item_history(item_id: str, limit: int = Query(50, ge=1, le=200)):
    """Retorna el historial de cambios de un item (precio, título, stock, estado, descripción)."""
    rows = await _token_store.get_item_history(item_id, limit=limit)
    return {"item_id": item_id, "history": rows, "count": len(rows)}


def _calculate_health_score(body: dict, description: str | None = "", price_delta_pct: float | None = None) -> tuple:
    """Calcula health score (0-100), lista de problemas y breakdown detallado.

    price_delta_pct: % del precio actual vs. precio sugerido/top-3 de categoria
    (de _price_comp_cache, solo disponible para los top 20 items por ventas --
    el resto del catalogo no tiene este dato y el check simplemente se omite,
    no se penaliza por falta de dato). Antes el score no consideraba precio en
    absoluto: un listing podia sacar 95/100 y estar 25% caro, quemando
    presupuesto de Ads en clics que no convierten sin que el score lo reflejara.

    description=None (2026-08-14, unificacion con el "Quality Score ML" del
    gap scan, ver lanzar.py _process_item_body): significa "dato no
    disponible en este contexto" (el multiget de items NO trae descripcion,
    es un endpoint aparte por item -- pedirla en bulk para miles de items
    seria caro) -- el check se OMITE sin penalizar, mismo patron que
    price_delta_pct=None. description="" (string vacio, el default de
    siempre) SI penaliza -- ahi el caller SI tiene el dato y de verdad esta
    vacia."""
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

    # Descripcion -- None = dato no disponible en este contexto, se omite sin penalizar
    if description is None:
        breakdown.append({"label": "Descripcion (no verificada)", "impact": 0, "ok": True, "tip": "", "key": ""})
    else:
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

    # Precio vs competencia (solo si hay dato disponible -- top 20 por ventas,
    # ver _price_comp_cache). Un score alto con precio caro es engañoso: el
    # listing puede estar listo para escalar en fotos/titulo/etc. y aun asi
    # quemar presupuesto de Ads porque no convierte por precio.
    if price_delta_pct is not None:
        if price_delta_pct > 25:
            score -= 20; problems.append(f"Precio {price_delta_pct:.0f}% sobre el sugerido/competencia")
            breakdown.append({"label": f"Precio vs competencia (+{price_delta_pct:.0f}%)", "impact": -20, "ok": False,
                               "tip": "Precio muy por arriba del top-3 de categoria -- revisar antes de invertir en Ads"})
        elif price_delta_pct > 10:
            score -= 15; problems.append(f"Precio {price_delta_pct:.0f}% sobre el sugerido/competencia")
            breakdown.append({"label": f"Precio vs competencia (+{price_delta_pct:.0f}%)", "impact": -15, "ok": False,
                               "tip": "Precio arriba del top-3 de categoria -- puede estar limitando conversion"})
        else:
            breakdown.append({"label": "Precio vs competencia", "impact": 0, "ok": True, "tip": ""})

    return max(score, 0), problems, breakdown


def _classify_score(score: int) -> str:
    if score < 40:
        return "critico"
    elif score <= 70:
        return "necesita_trabajo"
    return "bueno"


def _suggest_list_price(retail_mxn: float, is_tv: bool, deal_discount_pct: float = 0.20,
                         shipping_est: float | None = None) -> dict | None:
    """Precio de LISTA sugerido -- pedido explicito de Jovan 2026-08-14:
    "para televisores recuperando el 80% como minimo sumandole un 20% que
    serian los deals... para las otras categorias 60%... con las mismas
    condiciones del 20% extra". Es decir: el precio de LISTA debe quedar
    lo bastante arriba para que, SI se le aplica despues un deal de 20% de
    descuento, el neto resultante (mismo calculo que _calc_margins: fee ML
    escalonado + retenciones fiscales 9.05% + envio + 7% comision de socio)
    siga recuperando el 80% (TVs, SKU empieza con SNTV) o 60% (todo lo
    demas) del retail real.

    Busqueda binaria en vez de despejar algebraicamente porque _ml_fee()
    es escalonado por tramo de precio -- converge sin problema porque el
    fee % NUNCA sube al subir el precio (167-175: 18%->16%->14%->12%),
    asi que recup% siempre crece con el precio de lista, sin zonas planas
    ni saltos hacia atras.
    """
    if retail_mxn <= 0:
        return None
    target_pct = _main_module._RECOVERY_TARGET_TV if is_tv else _main_module._RECOVERY_TARGET_OTHER
    if shipping_est is None:
        if retail_mxn >= 5000:
            shipping_est = 400
        elif retail_mxn >= 2500:
            shipping_est = 250
        elif retail_mxn >= 1000:
            shipping_est = 150
        else:
            shipping_est = 100

    def _recup_pct_at(list_price: float) -> float:
        sale_price = list_price * (1 - deal_discount_pct)
        fee_pct = _main_module._ml_fee(sale_price)
        net_ml = sale_price * (1 - fee_pct - 0.0905) - shipping_est
        neto = net_ml * (1 - _main_module._PARTNER_COMMISSION_PCT)
        return (neto / retail_mxn) * 100

    lo, hi = retail_mxn * 0.3, retail_mxn * 25
    for _ in range(80):
        mid = (lo + hi) / 2
        if _recup_pct_at(mid) < target_pct:
            lo = mid
        else:
            hi = mid
    suggested = round(hi, 2)
    sale_price_after_deal = round(suggested * (1 - deal_discount_pct), 2)
    return {
        "suggested_list_price": suggested,
        "sale_price_after_deal": sale_price_after_deal,
        "deal_discount_pct": deal_discount_pct * 100,
        "target_recovery_pct": target_pct,
        "actual_recovery_pct_after_deal": round(_recup_pct_at(suggested), 1),
        "retail_mxn": round(retail_mxn, 2),
        "shipping_est_mxn": shipping_est,
        "is_tv": is_tv,
    }


@router.get("/{item_id}/suggested-price")
async def get_suggested_price(item_id: str, sku: str = Query(..., description="SKU BM del item")):
    """Precio de lista sugerido para que, tras un deal del 20%, se siga
    recuperando el 80% (TVs) / 60% (otras categorias) del retail real.
    Ver _suggest_list_price() para el detalle del calculo."""
    from app.services.sku_utils import normalize_to_bm_sku
    base_sku = normalize_to_bm_sku(sku)
    if not base_sku:
        return JSONResponse({"error": "sku requerido"}, status_code=400)

    retail_usd = await _token_store.get_bm_retail_ph(base_sku)
    if not retail_usd:
        return JSONResponse({"error": f"Sin RetailPH en BinManager para {base_sku}"}, status_code=404)

    fx = _main_module._manual_fx_rate if _main_module._manual_fx_rate > 0 else _main_module._last_fx_rate
    retail_mxn = retail_usd * fx
    is_tv = base_sku.upper().startswith("SNTV")

    result = _suggest_list_price(retail_mxn, is_tv)
    if not result:
        return JSONResponse({"error": "No se pudo calcular (retail invalido)"}, status_code=400)
    result["sku"] = base_sku
    result["retail_usd"] = round(retail_usd, 2)
    result["fx_rate"] = round(fx, 4)
    return result
