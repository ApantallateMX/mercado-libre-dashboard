"""API endpoints para comparar SKUs de inventario con MeLi."""
import asyncio
import io
import csv
from typing import Optional
from fastapi import APIRouter, Query, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from app.services.meli_client import get_meli_client
from app.services.product_researcher import research_product

router = APIRouter(prefix="/api/sku-inventory", tags=["sku-inventory"])

# BinManager API endpoints
BINMANAGER_FULLFILLMENT_URL = "https://binmanager.mitechnologiesinc.com/FullFillment/FullFillment/GetQtysFromWebSKU"
BINMANAGER_INVENTORY_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
BINMANAGER_COMPANY_ID = 1
BINMANAGER_CONCEPT_ID = 8

# SKU suffix handling
_GR_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC")
_IC_SUFFIXES = ("-ICB", "-ICC")
_ALL_SUFFIXES = _GR_SUFFIXES + _IC_SUFFIXES


def _get_item_sku(item: dict) -> str:
    """Get effective SKU from item: seller_custom_field or SELLER_SKU attribute."""
    scf = (item.get("seller_custom_field") or "").upper()
    if scf:
        return scf
    for a in item.get("attributes", []):
        if a.get("id") == "SELLER_SKU":
            return (a.get("value_name") or "").upper()
    return ""


def _get_variation_sku(variation: dict) -> str:
    """Get effective SKU from variation: seller_custom_field or SELLER_SKU attribute."""
    scf = (variation.get("seller_custom_field") or "").upper()
    if scf:
        return scf
    for a in variation.get("attributes", []):
        if a.get("id") == "SELLER_SKU":
            return (a.get("value_name") or "").upper()
    return ""


def _extract_base_sku(sku: str) -> str:
    """Devuelve el SKU base sin sufijo de variante."""
    upper = sku.upper()
    for sfx in _ALL_SUFFIXES:
        if upper.endswith(sfx):
            return sku[:-len(sfx)]
    return sku


async def _fetch_suffix_stock(base_sku: str, suffix: str, http: httpx.AsyncClient) -> dict | None:
    """Query FullFillment for a single SKU+suffix. Returns raw row or None."""
    try:
        resp = await http.post(
            f"{BINMANAGER_FULLFILLMENT_URL}?WEBSKU={base_sku}{suffix}",
            content="",
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
    except Exception:
        pass
    return None


async def _fetch_sellable_stock(sku: str, http: httpx.AsyncClient) -> dict:
    """Consulta stock vendible en BinManager por sufijos.

    Queries FullFillment for each sellable suffix (-NEW,-GRA,-GRB,-GRC,-ICB,-ICC),
    deduplicates by ProductSKU, and returns two groups:
      stock_gr: NEW/GRA/GRB/GRC (condicion buena)
      stock_ic: ICB/ICC (incompleto)
    Each group has: mty, cdmx, tj, total
    """
    base = _extract_base_sku(sku)

    # Query all 6 suffixes in parallel
    tasks = []
    suffix_labels = []
    for sfx in _GR_SUFFIXES:
        tasks.append(_fetch_suffix_stock(base, sfx, http))
        suffix_labels.append(("gr", sfx))
    for sfx in _IC_SUFFIXES:
        tasks.append(_fetch_suffix_stock(base, sfx, http))
        suffix_labels.append(("ic", sfx))

    rows = await asyncio.gather(*tasks)

    # Deduplicate by ProductSKU globally, classify by actual ProductSKU suffix
    seen = set()
    gr = {"mty": 0, "cdmx": 0, "tj": 0, "total": 0}
    ic = {"mty": 0, "cdmx": 0, "tj": 0, "total": 0}

    for (group, sfx), row in zip(suffix_labels, rows):
        if row is None:
            continue
        product_sku = (row.get("ProductSKU") or "").upper()
        if not product_sku:
            product_sku = f"{base}{sfx}".upper()

        if product_sku in seen:
            continue
        seen.add(product_sku)

        # Classify by actual ProductSKU suffix, not by which query found it
        psku_upper = product_sku.upper()
        actual_group = "gr"  # default
        for ic_sfx in _IC_SUFFIXES:
            if psku_upper.endswith(ic_sfx):
                actual_group = "ic"
                break

        mty = row.get("MainQtyMTY", 0) or 0
        cdmx = row.get("MainQtyCDMX", 0) or 0
        tj = row.get("MainQtyTJ", 0) or 0
        target = gr if actual_group == "gr" else ic
        target["mty"] += mty
        target["cdmx"] += cdmx
        target["tj"] += tj
        # TJ es solo informativo, no cuenta para total vendible
        target["total"] += mty + cdmx

    sellable_total = gr["total"] + ic["total"]

    # NOTA: InventoryReport.AvailableQTY NO es el stock real disponible.
    # Para SNTV001763 devuelve 4971, para SNTV001863 devuelve 9124 — valores absurdos.
    # El único dato confiable es MainQty del FullFillment (ya consultado arriba).
    # Si ningún sufijo vendible tiene stock, el inventario real es 0.

    return {
        "stock_gr": gr,
        "stock_ic": ic,
        "stock_other": 0,
        "total_stock": sellable_total,
    }


async def _fetch_binmanager_product_info(sku: str, http: httpx.AsyncClient) -> dict:
    """Fetch product info (Brand, Model, Title) from BinManager InventoryReport."""
    try:
        payload = {
            "COMPANYID": BINMANAGER_COMPANY_ID,
            "SEARCH": sku,
            "CONCEPTID": BINMANAGER_CONCEPT_ID,
            "NUMBERPAGE": 1,
            "RECORDSPAGE": 10
        }
        resp = await http.post(
            BINMANAGER_INVENTORY_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15.0
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                row = data[0]
                for item in data:
                    if item.get("SKU", "").upper() == sku.upper():
                        row = item
                        break
                return {
                    "brand": row.get("Brand", "") or "",
                    "model": row.get("Model", "") or "",
                    "title": row.get("Title", "") or "",
                    "upc": row.get("UPC", "") or "",
                    "category": row.get("CategoryName", "") or "",
                    "retail_price": row.get("RetailPrice", 0) or 0,
                    "avg_cost": row.get("AvgCostQTY", 0) or 0,
                    "description": row.get("Description", "") or "",
                    "weight": row.get("Weight", 0) or 0,
                    "image_url": row.get("ImageURL", "") or "",
                    "color": row.get("Color", "") or "",
                }
    except Exception:
        pass
    return {}


def _calculate_listing_score(meli_item: dict) -> int:
    """Calculate quality score (0-100) for a MeLi listing."""
    if not meli_item:
        return 0

    score = 0

    # Title: 25 pts
    title = meli_item.get("title", "")
    if len(title) >= 40:
        score += 25
    elif len(title) >= 20:
        score += 15
    elif len(title) >= 10:
        score += 5

    # Pictures: 30 pts
    pics = meli_item.get("pictures", [])
    pic_count = len(pics) if isinstance(pics, list) else 0
    if pic_count >= 6:
        score += 30
    elif pic_count >= 3:
        score += 20
    elif pic_count >= 1:
        score += 10

    # Attributes: 25 pts
    attrs = meli_item.get("attributes", [])
    filled = sum(1 for a in attrs if a.get("value_name")) if isinstance(attrs, list) else 0
    if filled >= 10:
        score += 25
    elif filled >= 5:
        score += 15
    elif filled >= 1:
        score += 5

    # Price: 10 pts
    if meli_item.get("price", 0) and meli_item["price"] > 0:
        score += 10

    # Has video or good shipping: 10 pts
    if meli_item.get("video_id"):
        score += 10
    elif meli_item.get("shipping", {}).get("free_shipping"):
        score += 5

    return min(score, 100)


@router.post("/parse-skus")
async def parse_skus(
    file: Optional[UploadFile] = File(None),
    text_skus: Optional[str] = Form(None)
):
    """
    Parsea SKUs desde un archivo CSV/Excel o texto.
    Retorna lista de SKUs unicos.
    """
    skus = set()

    # Procesar archivo si se subio
    if file and file.filename:
        content = await file.read()
        try:
            # Intentar decodificar como texto (CSV)
            text = content.decode("utf-8-sig")  # utf-8-sig para manejar BOM
            reader = csv.reader(io.StringIO(text))
            for row in reader:
                if row:
                    # Tomar la primera columna como SKU
                    sku = row[0].strip()
                    if sku and sku.upper() != "SKU":  # Ignorar header
                        skus.add(sku.upper())
        except Exception as e:
            # Si falla, intentar Excel
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
                ws = wb.active
                for row in ws.iter_rows(min_row=1, max_col=1, values_only=True):
                    if row[0]:
                        sku = str(row[0]).strip()
                        if sku.upper() != "SKU":
                            skus.add(sku.upper())
            except ImportError:
                pass
            except Exception:
                return JSONResponse(
                    {"error": "No se pudo leer el archivo. Asegurate de que sea CSV o Excel valido."},
                    status_code=400
                )

    # Procesar texto si se envio
    if text_skus:
        # Soportar comas, punto y coma, saltos de linea, tabs
        import re
        parts = re.split(r"[,;\n\t]+", text_skus)
        for part in parts:
            sku = part.strip().upper()
            if sku:
                skus.add(sku)

    if not skus:
        return JSONResponse(
            {"error": "No se encontraron SKUs en el archivo o texto proporcionado."},
            status_code=400
        )

    return {"skus": sorted(list(skus)), "count": len(skus)}


@router.post("/compare")
async def compare_skus(skus: list[str]):
    """
    Compara lista de SKUs contra BinManager (stock) y MeLi (publicaciones).

    Clasificacion:
    - not_published: Con stock en BinManager, sin publicacion en MeLi
    - paused: Con stock, publicacion pausada
    - active: Con stock, publicacion activa
    - no_stock: Sin stock en BinManager
    """
    if not skus:
        return {"error": "Lista de SKUs vacia"}

    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado en MeLi"}

    try:
        results = []
        sem = asyncio.Semaphore(10)

        async def process_sku(sku: str, http: httpx.AsyncClient):
            async with sem:
                # 1. Consultar BinManager por sufijos vendibles
                bm = await _fetch_sellable_stock(sku, http)

                # 2. Buscar TODOS los listings en MeLi por SKU
                meli_items = await client.search_all_items_by_sku(sku)

                # 3. Clasificar basado en el mejor listing
                total_stock = bm["total_stock"]
                best_item = None
                best_score = -1
                items_summary = []

                for mi in meli_items:
                    score = _calculate_listing_score(mi)
                    variations = mi.get("variations", [])

                    # Determine matching variations for this SKU
                    matching_vars = []
                    for v in variations:
                        v_sku = _get_variation_sku(v)
                        if v_sku == sku.upper():
                            combos = ", ".join(
                                f"{ac.get('name','')}: {ac.get('value_name','')}"
                                for ac in v.get("attribute_combinations", [])
                            )
                            matching_vars.append({
                                "id": v.get("id"),
                                "sku": v_sku,
                                "stock": v.get("available_quantity", 0),
                                "attrs": combos or "Sin atributos",
                            })

                    # Detect how SKU is linked: item-level vs variation-level
                    item_sku = _get_item_sku(mi)
                    sku_level = "item" if item_sku == sku.upper() else ("variation" if matching_vars else "attr")

                    is_variation = len(variations) > 1
                    total_item_stock = (
                        sum(v.get("available_quantity", 0) for v in variations)
                        if variations
                        else mi.get("available_quantity", 0)
                    )

                    items_summary.append({
                        "id": mi.get("id"),
                        "title": mi.get("title"),
                        "price": mi.get("price"),
                        "status": mi.get("status"),
                        "permalink": mi.get("permalink"),
                        "score": score,
                        "type": "variacion" if is_variation else "unica",
                        "variation_count": len(variations),
                        "matching_variations": matching_vars,
                        "total_meli_stock": total_item_stock,
                        "sku_level": sku_level,
                    })
                    if score > best_score:
                        best_score = score
                        best_item = mi

                if total_stock == 0:
                    status = "no_stock"
                elif not meli_items:
                    status = "not_published"
                elif best_item and best_item.get("status") == "paused":
                    status = "paused"
                elif best_item and best_item.get("status") == "active":
                    status = "active"
                else:
                    status = best_item.get("status", "unknown") if best_item else "not_published"

                return {
                    "sku": sku,
                    "base_sku": _extract_base_sku(sku),
                    "stock_gr": bm["stock_gr"],
                    "stock_ic": bm["stock_ic"],
                    "stock_other": bm.get("stock_other", 0),
                    "total_stock": total_stock,
                    "meli_status": status,
                    "item_id": best_item.get("id") if best_item else None,
                    "best_item_id": best_item.get("id") if best_item else None,
                    "items": items_summary,
                    "item_title": best_item.get("title") if best_item else None,
                    "item_price": best_item.get("price") if best_item else None,
                    "meli_stock": sum(it.get("total_meli_stock", 0) for it in items_summary) if items_summary else None,
                    "permalink": best_item.get("permalink") if best_item else None,
                    "listing_score": best_score if best_item else None,
                }

        async with httpx.AsyncClient() as http:
            tasks = [process_sku(sku, http) for sku in skus]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                results.append(result)

        # Ordenar: candidatos a lanzar primero, luego pausados, luego activos, luego sin stock
        priority = {"not_published": 0, "paused": 1, "active": 2, "no_stock": 3}
        results.sort(key=lambda r: (priority.get(r["meli_status"], 4), -r["total_stock"]))

        # Resumen
        summary = {
            "total": len(results),
            "not_published": len([r for r in results if r["meli_status"] == "not_published"]),
            "paused": len([r for r in results if r["meli_status"] == "paused"]),
            "active": len([r for r in results if r["meli_status"] == "active"]),
            "no_stock": len([r for r in results if r["meli_status"] == "no_stock"]),
        }

        return {"summary": summary, "results": results}

    finally:
        await client.close()


@router.post("/research")
async def research_sku(body: dict):
    """Auto-research a SKU: BinManager product info + web search + MeLi competitors.

    Receives {sku, stock} and returns pre-filled listing data.
    """
    sku = body.get("sku", "").strip()
    if not sku:
        return JSONResponse({"error": "SKU requerido"}, status_code=400)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "No autenticado en MeLi"}, status_code=401)

    try:
        # Fetch product info from BinManager (Brand, Model, Title)
        bm_info = {}
        async with httpx.AsyncClient() as http:
            bm_info = await _fetch_binmanager_product_info(sku, http)

        result = await research_product(sku, client, bm_info=bm_info)
        return result
    except Exception as e:
        return JSONResponse({"error": f"Error en investigacion: {str(e)}"}, status_code=500)
    finally:
        await client.close()


@router.get("/suggest-category")
async def suggest_category(title: str = Query(..., min_length=3)):
    """Sugiere categorias de MeLi basadas en el titulo del producto."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        import re
        # Clean title: remove model/part numbers that confuse category prediction
        # e.g. "LUTEMA MP3FM-M95F-WH-005 M95Fi Artic White Disposable Face Mask"
        # -> "LUTEMA Artic White Disposable Face Mask"
        clean_title = title
        # Remove tokens that look like part numbers (letters+digits+dashes, 5+ chars)
        clean_title = re.sub(r'\b[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9-]+\b', '', clean_title)  # multi-dash codes
        clean_title = re.sub(r'\b[A-Z]{1,3}\d{3,}[A-Z]*\b', '', clean_title)  # codes like MP3FM, M95Fi
        clean_title = re.sub(r'\b\d{3,}-\d+\b', '', clean_title)  # numeric codes like 824-16
        clean_title = re.sub(r'\s+', ' ', clean_title).strip()

        # Use cleaned title if it still has meaningful words, otherwise fallback to original
        search_title = clean_title if len(clean_title) >= 5 else title

        # Try with cleaned title first, then original if no results
        prediction = await client.predict_category(search_title)
        suggestions = await client.suggest_category(search_title)

        # If no results with cleaned title, retry with original
        if not prediction and not suggestions and search_title != title:
            prediction = await client.predict_category(title)
            suggestions = await client.suggest_category(title)

        # Combinar resultados
        categories = []

        # Agregar prediccion principal si existe
        if prediction and prediction.get("id"):
            categories.append({
                "id": prediction.get("id"),
                "name": prediction.get("name", ""),
                "path": " > ".join(p.get("name", "") for p in prediction.get("path_from_root", [])),
                "confidence": 1.0,
                "source": "predictor"
            })

        # Agregar sugerencias de domain discovery
        for sug in suggestions[:5]:
            cat_id = sug.get("category_id") or sug.get("id")
            if cat_id and not any(c["id"] == cat_id for c in categories):
                categories.append({
                    "id": cat_id,
                    "name": sug.get("category_name") or sug.get("name", ""),
                    "path": sug.get("category_path", ""),
                    "confidence": sug.get("score", 0.5),
                    "source": "discovery"
                })

        return {"categories": categories[:6]}

    finally:
        await client.close()


@router.get("/search-categories")
async def search_categories_by_keyword(q: str = Query(..., min_length=2)):
    """Busca categorias por palabra clave (ej: 'cubrebocas', 'funda iphone').

    Usa el search de MeLi para encontrar categorias relevantes con IDs.
    """
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        import re
        categories = []

        # 1. Search categories via product search (category facets)
        keyword_cats = await client.search_categories(q)
        for kc in keyword_cats:
            cat_id = kc.get("id", "")
            if cat_id and not any(c["id"] == cat_id for c in categories):
                categories.append({
                    "id": cat_id,
                    "name": kc.get("name", ""),
                    "path": "",
                    "results": kc.get("results", 0),
                    "source": "search"
                })

        # 2. Also try predict + suggest for completeness
        prediction = await client.predict_category(q)
        if prediction and prediction.get("id"):
            cat_id = prediction["id"]
            if not any(c["id"] == cat_id for c in categories):
                categories.insert(0, {
                    "id": cat_id,
                    "name": prediction.get("name", ""),
                    "path": " > ".join(p.get("name", "") for p in prediction.get("path_from_root", [])),
                    "results": 0,
                    "source": "predictor"
                })

        suggestions = await client.suggest_category(q)
        for sug in suggestions[:5]:
            cat_id = sug.get("category_id") or sug.get("id")
            if cat_id and not any(c["id"] == cat_id for c in categories):
                categories.append({
                    "id": cat_id,
                    "name": sug.get("category_name") or sug.get("name", ""),
                    "path": sug.get("category_path", ""),
                    "results": 0,
                    "source": "discovery"
                })

        return {"categories": categories[:10]}

    finally:
        await client.close()


@router.get("/category-attributes/{category_id}")
async def get_category_attributes(category_id: str):
    """Obtiene los atributos de una categoria (requeridos y opcionales)."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        attrs = await client.get_category_attributes(category_id)

        # Separar en requeridos, recomendados y opcionales
        required = []
        recommended = []
        optional = []

        for attr in attrs:
            attr_id = attr.get("id", "")
            tags = attr.get("tags", {})
            values = attr.get("values", [])

            attr_data = {
                "id": attr_id,
                "name": attr.get("name", attr_id),
                "type": attr.get("value_type", "string"),
                "values": [{"id": v.get("id"), "name": v.get("name")} for v in values[:50]],
                "required": tags.get("required", False),
                "catalog_required": tags.get("catalog_required", False),
                "allow_custom": attr.get("attribute_group_id") != "OTHERS",
                "group_name": attr.get("attribute_group_name", "Otros"),
                "group_id": attr.get("attribute_group_id", ""),
            }

            if tags.get("required"):
                required.append(attr_data)
            elif tags.get("catalog_required") or attr_id in ["BRAND", "MODEL", "MPN", "GTIN"]:
                recommended.append(attr_data)
            else:
                optional.append(attr_data)

        return {
            "category_id": category_id,
            "required": required,
            "recommended": recommended,
            "optional": optional,
            "all_count": len(required) + len(recommended) + len(optional),
        }

    finally:
        await client.close()


@router.post("/validate-item")
async def validate_item(payload: dict):
    """Valida un item sin publicarlo."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        result = await client.validate_item(payload)
        return result
    finally:
        await client.close()


@router.post("/create-item")
async def create_item(payload: dict):
    """Crea un nuevo item en Mercado Libre."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        # Asegurar campos minimos
        if "site_id" not in payload:
            payload["site_id"] = "MLM"
        if "currency_id" not in payload:
            payload["currency_id"] = "MXN"
        if "buying_mode" not in payload:
            payload["buying_mode"] = "buy_it_now"

        # Compliance defaults: warranty
        if "warranty" not in payload and "sale_terms" not in payload:
            payload["sale_terms"] = [
                {
                    "id": "WARRANTY_TYPE",
                    "value_name": "Garantía del vendedor"
                },
                {
                    "id": "WARRANTY_TIME",
                    "value_name": "12 meses"
                }
            ]

        # Compliance defaults: shipping
        if "shipping" not in payload:
            payload["shipping"] = {
                "mode": "me2",
                "free_shipping": True
            }

        result = await client.create_item(payload)
        return result
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        await client.close()


@router.get("/listing-types")
async def get_listing_types():
    """Obtiene los tipos de listado disponibles."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        types = await client.get_listing_types()
        # Filtrar los mas comunes
        common_types = ["gold_special", "gold_pro", "gold", "silver", "bronze", "free"]
        filtered = [t for t in types if t.get("id") in common_types]
        return {"listing_types": filtered or types}
    finally:
        await client.close()


@router.put("/reactivate/{item_id}")
async def reactivate_item(item_id: str):
    """Reactiva un item pausado."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        result = await client.update_item_status(item_id, "active")
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        await client.close()


@router.get("/item-details/{item_id}")
async def get_item_details(item_id: str):
    """Obtiene detalles completos de un item para optimizacion."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}

    try:
        # Retry up to 2 times on transient errors (400/5xx)
        item = None
        for _attempt in range(3):
            try:
                item = await client.get_item(item_id)
                break
            except Exception:
                if _attempt == 2:
                    raise
                import asyncio
                await asyncio.sleep(1)

        desc = {}
        try:
            desc = await client.get_item_description(item_id)
        except Exception:
            pass

        score = _calculate_listing_score(item)

        # Build improvement tips
        tips = []
        title = item.get("title", "")
        if len(title) < 40:
            tips.append({"field": "title", "msg": "Titulo corto (" + str(len(title)) + " chars) - recomendado 40+"})

        pics = item.get("pictures", [])
        if len(pics) < 6:
            tips.append({"field": "pictures", "msg": "Solo " + str(len(pics)) + " foto(s) - recomendado 6+"})
        if len(pics) == 0:
            tips.append({"field": "pictures", "msg": "Sin fotos - agrega al menos 1"})

        attrs = item.get("attributes", [])
        filled = sum(1 for a in attrs if a.get("value_name"))
        if filled < 10:
            tips.append({"field": "attributes", "msg": str(filled) + " atributos - llena mas para mejor posicionamiento"})

        if not item.get("video_id"):
            tips.append({"field": "video", "msg": "Sin video - agrega uno para +10% en score"})

        if not desc.get("plain_text") and not desc.get("text"):
            tips.append({"field": "description", "msg": "Sin descripcion - agrega una detallada"})

        return {
            "item": item,
            "description": desc.get("plain_text") or desc.get("text") or "",
            "score": score,
            "tips": tips,
            "pictures_count": len(pics),
            "attributes_filled": filled,
        }

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        await client.close()


@router.put("/optimize/{item_id}")
async def optimize_item(item_id: str, body: dict):
    """Actualiza campos de un item existente para optimizarlo."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    try:
        results = {}
        errors = []

        # Update title
        if "title" in body and body["title"]:
            try:
                results["title"] = await client.update_item_title(item_id, body["title"])
            except Exception as e:
                errors.append(f"Titulo: {e}")

        # Update description
        if "description" in body and body["description"]:
            try:
                results["description"] = await client.update_item_description(item_id, body["description"])
            except Exception as e:
                errors.append(f"Descripcion: {e}")

        # Update pictures (add new ones)
        if "pictures" in body and body["pictures"]:
            try:
                results["pictures"] = await client.update_item_pictures(item_id, body["pictures"])
            except Exception as e:
                errors.append(f"Fotos: {e}")

        # Update attributes
        if "attributes" in body and body["attributes"]:
            try:
                results["attributes"] = await client.update_item_attributes(item_id, body["attributes"])
            except Exception as e:
                errors.append(f"Atributos: {e}")

        # Re-fetch item to get new score
        try:
            item = await client.get_item(item_id)
            new_score = _calculate_listing_score(item)
        except Exception:
            new_score = None

        resp = {"ok": len(errors) == 0, "new_score": new_score, "results": results}
        if errors:
            resp["errors"] = errors
        return resp

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        await client.close()


@router.put("/update-stock/{item_id}")
async def update_meli_stock(item_id: str, body: dict):
    """Actualiza el stock disponible de un item en MeLi."""
    quantity = body.get("quantity")
    if quantity is None or not isinstance(quantity, int) or quantity < 0:
        return JSONResponse({"error": "Cantidad invalida"}, status_code=400)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    try:
        result = await client.update_item_stock(item_id, quantity)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        await client.close()


# =========================================================================
# AI Endpoints (Claude API)
# =========================================================================

@router.get("/ai-status")
async def ai_status():
    """Check if the Claude AI API is available."""
    from app.services import claude_client
    return {"available": claude_client.is_available()}


@router.post("/ai-improve")
async def ai_improve(body: dict):
    """
    AI-powered improvement for title, description, or attributes.
    Supports streaming for title and description.
    """
    from app.services import claude_client

    if not claude_client.is_available():
        return JSONResponse({"error": "AI not available - ANTHROPIC_API_KEY not configured"}, status_code=503)

    field = body.get("field", "")
    current_value = body.get("current_value", "")
    context = body.get("context", {})

    sku = context.get("sku", "")
    brand = context.get("brand", "")
    model = context.get("model", "")
    category = context.get("category", "")

    system_prompt = (
        "Eres un experto en optimizacion de publicaciones para Mercado Libre Mexico. "
        "Responde siempre en espanol. Se conciso y directo."
    )

    if field == "title":
        prompt = (
            f"Genera exactamente 3 titulos SEO optimizados para Mercado Libre Mexico.\n"
            f"Producto: {current_value}\n"
            f"Marca: {brand}\nModelo: {model}\nCategoria: {category}\n\n"
            f"Reglas:\n"
            f"- Max 60 caracteres cada uno\n"
            f"- Formato: Marca + Tipo de Producto + Specs descriptivas\n"
            f"- NO incluir numeros de parte ni codigos de modelo\n"
            f"- Palabras clave que buscan compradores en Mexico\n\n"
            f"Responde SOLO los 3 titulos, uno por linea, sin numeros ni viñetas."
        )

        async def title_stream():
            try:
                async for chunk in claude_client.generate_stream(prompt, system_prompt, max_tokens=300):
                    yield f"data: {chunk}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: [ERROR] {str(e)}\n\n"

        return StreamingResponse(title_stream(), media_type="text/event-stream")

    elif field == "description":
        prompt = (
            f"Genera una descripcion profesional para Mercado Libre Mexico.\n"
            f"Titulo: {current_value}\n"
            f"Marca: {brand}\nModelo: {model}\nCategoria: {category}\nSKU: {sku}\n\n"
            f"Estructura:\n"
            f"- Parrafo de apertura (beneficios principales)\n"
            f"- Caracteristicas tecnicas en lista\n"
            f"- Contenido del paquete\n"
            f"- Garantia y soporte\n\n"
            f"Reglas:\n"
            f"- Solo texto plano (MeLi no soporta HTML)\n"
            f"- Usa saltos de linea para separar secciones\n"
            f"- Maximo 800 palabras\n"
            f"- Tono profesional pero accesible"
        )

        async def desc_stream():
            try:
                async for chunk in claude_client.generate_stream(prompt, system_prompt, max_tokens=1500):
                    yield f"data: {chunk}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: [ERROR] {str(e)}\n\n"

        return StreamingResponse(desc_stream(), media_type="text/event-stream")

    elif field == "autocorrect":
        prompt = (
            f"Revisa y mejora el siguiente texto de una publicacion de Mercado Libre Mexico:\n\n"
            f"Titulo: {context.get('title', '')}\n"
            f"Descripcion:\n{current_value}\n\n"
            f"CORRIGE estos problemas:\n"
            f"1. Ortografia: faltas, acentos faltantes o incorrectos\n"
            f"2. Caracteres raros/corruptos: &#x27; Â etc -> reemplazar por el caracter correcto\n"
            f"3. Mayusculas/minusculas mal usadas\n"
            f"4. Espacios dobles, tabs, espacios de mas\n"
            f"5. Puntuacion mal colocada\n\n"
            f"MEJORA el formato de la DESCRIPCION para que sea facil de leer y tenga buen SEO:\n"
            f"- Agrega lineas en blanco entre secciones para que no se vea todo corrido\n"
            f"- Usa encabezados claros en MAYUSCULAS (ej: CARACTERISTICAS, INCLUYE, GARANTIA)\n"
            f"- Listas con viñetas usando - o * para especificaciones\n"
            f"- Parrafos cortos (max 3 lineas) para que no sea aburrido de leer\n"
            f"- Incluye palabras clave relevantes de forma natural (SEO)\n"
            f"- Tono profesional pero amigable para el comprador mexicano\n"
            f"- Si la descripcion esta vacia o muy corta, genera una completa basada en el titulo\n\n"
            f"Responde UNICAMENTE con JSON valido, sin backticks ni markdown:\n"
            f'{{"title": "titulo corregido (max 60 chars)", "description": "descripcion corregida y formateada con \\n para saltos de linea", "changes": ["cambio 1", "cambio 2"]}}'
        )

        try:
            result = await claude_client.generate(prompt, system_prompt, max_tokens=2500)
            return {"result": result}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    elif field == "attributes":
        prompt = (
            f"Dado este producto:\n"
            f"Titulo: {context.get('title', '')}\n"
            f"Marca: {brand}\nModelo: {model}\nCategoria: {category}\n\n"
            f"Sugiere valores para estos atributos vacios de Mercado Libre:\n"
            f"{current_value}\n\n"
            f"Responde SOLO en formato JSON: un array de objetos con {{\"id\": \"ATTR_ID\", \"value_name\": \"valor sugerido\"}}\n"
            f"Solo incluye atributos para los que tengas alta confianza. No inventes datos."
        )

        try:
            result = await claude_client.generate(prompt, system_prompt, max_tokens=800)
            return {"result": result}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    else:
        return JSONResponse({"error": f"Unknown field: {field}"}, status_code=400)
