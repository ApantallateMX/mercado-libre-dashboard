"""
Productos API — Ciclo de vida unificado de publicaciones MeLi
=============================================================
GET  /api/productos           — lista paginada (activos + pausados) + BM stock + score
GET  /api/productos/stats     — conteos por estado (rápido)
GET  /api/productos/candidates — SKUs en BM sin publicar (bm_sku_gaps)
GET  /api/productos/{item_id} — detalle completo para panel lateral
POST /api/productos/{item_id}/clip  — sube video clip a ML Clips
"""
import asyncio
import logging
import aiosqlite
import httpx

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.services.meli_client import get_meli_client, _active_user_id as _ctx
from app.services.token_store import (
    DATABASE_PATH, save_product_video, update_clip_status, get_videos_for_items
)

router = APIRouter(prefix="/api/productos", tags=["productos"])
logger = logging.getLogger(__name__)

# ── BM endpoints ───────────────────────────────────────────────────────────────
_BM_WH_URL   = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
_BM_AVAIL_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"
_BM_COMPANY   = 1
_BM_LOCS      = "47,62,68"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bm_conditions(sku: str) -> str:
    upper = sku.upper()
    if upper.endswith("-ICB") or upper.endswith("-ICC"):
        return "GRA,GRB,GRC,ICB,ICC,NEW"
    return "GRA,GRB,GRC,NEW"


def _base_sku(sku: str) -> str:
    for sfx in ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC"):
        if sku.upper().endswith(sfx):
            return sku[:-len(sfx)]
    return sku


def _parse_wh(rows) -> tuple:
    mty = cdmx = tj = 0
    for r in (rows or []):
        qty = r.get("QtyTotal", 0) or 0
        wn = (r.get("WarehouseName") or "").lower()
        if "monterrey" in wn or "maxx" in wn:
            mty += qty
        elif "autobot" in wn or "cdmx" in wn or "ebanistas" in wn:
            cdmx += qty
        else:
            tj += qty
    return mty, cdmx, tj


async def _bm_stock(sku: str, hclient: httpx.AsyncClient) -> dict:
    base = _base_sku(sku)
    cond = _bm_conditions(sku)
    wh_payload = {
        "COMPANYID": _BM_COMPANY, "SKU": base, "WarehouseID": None,
        "LocationID": _BM_LOCS,  "BINID": None, "Condition": cond,
        "SUPPLIERS": None, "ForInventory": 0,
    }
    avail_payload = {
        "COMPANYID": _BM_COMPANY, "TYPEINVENTORY": 0, "WAREHOUSEID": None,
        "LOCATIONID": _BM_LOCS, "BINID": None, "PRODUCTSKU": base,
        "CONDITION": cond, "SUPPLIERS": None, "LCN": None, "SEARCH": base,
    }
    try:
        r_wh, r_av = await asyncio.gather(
            hclient.post(_BM_WH_URL,   json=wh_payload,    timeout=12.0),
            hclient.post(_BM_AVAIL_URL, json=avail_payload, timeout=12.0),
            return_exceptions=True,
        )
        rows_wh = r_wh.json()  if not isinstance(r_wh, Exception) and r_wh.status_code == 200 else []
        rows_av = r_av.json()  if not isinstance(r_av, Exception) and r_av.status_code == 200 else []
        mty, cdmx, tj = _parse_wh(rows_wh)
        avail = sum(r.get("Available", 0) or 0 for r in rows_av)
        return {"mty": mty, "cdmx": cdmx, "tj": tj, "avail": avail, "total": mty + cdmx + tj}
    except Exception:
        return {"mty": 0, "cdmx": 0, "tj": 0, "avail": 0, "total": 0}


def _calc_score(body: dict) -> tuple:
    score = 100
    problems = []
    pics = body.get("pictures", [])
    if len(pics) == 0:
        score -= 50; problems.append("Sin fotos")
    elif len(pics) < 5:
        score -= 30; problems.append(f"Solo {len(pics)} fotos")
    elif len(pics) < 8:
        score -= 15; problems.append(f"Solo {len(pics)} fotos")
    if not body.get("video_id"):
        score -= 10; problems.append("Sin video")
    shipping = body.get("shipping", {})
    free = shipping.get("free_shipping", False) or shipping.get("logistic_type") == "fulfillment"
    if not free:
        score -= 15; problems.append("Sin envío gratis")
    if body.get("status") == "paused":
        score -= 40; problems.append("Pausado")
    if body.get("available_quantity", 0) == 0:
        score -= 30; problems.append("Sin stock")
    if len(body.get("title", "")) < 30:
        score -= 20; problems.append("Título corto")
    return max(score, 0), problems


def _extract_sku(body: dict) -> str:
    sku = body.get("seller_custom_field") or ""
    if not sku:
        for attr in body.get("attributes", []):
            if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                sku = attr["value_name"]
                break
    if not sku and body.get("variations"):
        for var in body["variations"]:
            if var.get("seller_custom_field"):
                sku = var["seller_custom_field"]
                break
    return sku


def _item_row(body: dict, video_rec=None, bm=None) -> dict:
    score, problems = _calc_score(body)
    sku = _extract_sku(body)
    bm = bm or {}
    return {
        "item_id":       body.get("id", ""),
        "title":         body.get("title", "-"),
        "status":        body.get("status", "-"),
        "sku":           sku,
        "price":         body.get("price", 0),
        "stock_ml":      body.get("available_quantity", 0),
        "sold_quantity": body.get("sold_quantity", 0),
        "score":         score,
        "score_category": "bueno" if score >= 70 else ("necesita_trabajo" if score >= 40 else "critico"),
        "problems":      problems,
        "thumbnail":     body.get("thumbnail", ""),
        "permalink":     body.get("permalink", ""),
        "pics_count":    len(body.get("pictures", [])),
        "has_ml_video":  bool(body.get("video_id")),
        "free_shipping": body.get("shipping", {}).get("free_shipping", False),
        "bm_mty":        bm.get("mty", 0),
        "bm_cdmx":       bm.get("cdmx", 0),
        "bm_tj":         bm.get("tj", 0),
        "bm_total":      bm.get("total", 0),
        "bm_avail":      bm.get("avail", 0),
        "has_clip_video": bool(video_rec),
        "video_id":       video_rec.get("video_id") if video_rec else None,
        "clip_status":    video_rec.get("clip_status") if video_rec else None,
        "clip_uuid":      video_rec.get("clip_uuid") if video_rec else None,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats():
    """Conteo rápido de activos / pausados / críticos / candidatos."""
    user_id = _ctx.get()
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    try:
        active_data, paused_data = await asyncio.gather(
            client.get_items(offset=0, limit=1, status="active"),
            client.get_items(offset=0, limit=1, status="paused"),
            return_exceptions=True,
        )
        active_total = (
            active_data.get("paging", {}).get("total", 0)
            if not isinstance(active_data, Exception) else 0
        )
        paused_total = (
            paused_data.get("paging", {}).get("total", 0)
            if not isinstance(paused_data, Exception) else 0
        )
        async with aiosqlite.connect(DATABASE_PATH) as db:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM bm_sku_gaps WHERE status='unlaunched' AND user_id=?",
                (user_id,)
            )).fetchone()
            candidates = row[0] if row else 0
        return {
            "active":     active_total,
            "paused":     paused_total,
            "criticos":   None,   # computed client-side from score_category filter
            "candidates": candidates,
            "total":      active_total + paused_total,
        }
    finally:
        await client.close()


@router.get("/candidates")
async def get_candidates(
    q:      str = Query(""),
    offset: int = Query(0, ge=0),
    limit:  int = Query(50, ge=1, le=100),
):
    """SKUs en BM con stock pero sin publicar en ML (bm_sku_gaps)."""
    user_id = _ctx.get()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        base_q = "FROM bm_sku_gaps WHERE status='unlaunched' AND user_id=?"
        params: list = [user_id]
        if q:
            base_q += " AND (sku LIKE ? OR product_title LIKE ?)"
            params += [f"%{q}%", f"%{q}%"]

        count_row = await (await db.execute(f"SELECT COUNT(*) {base_q}", params)).fetchone()
        total = count_row[0] if count_row else 0

        rows = await (await db.execute(
            f"SELECT * {base_q} ORDER BY priority_score DESC, stock_total DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        )).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            items.append({
                "item_id":        None,
                "status":         "candidate",
                "sku":            d.get("sku", ""),
                "title":          d.get("product_title", ""),
                "brand":          d.get("brand", ""),
                "model":          d.get("model", ""),
                "thumbnail":      d.get("image_url", ""),
                "price":          d.get("suggested_price_mxn", 0),
                "bm_mty":         d.get("stock_mty", 0),
                "bm_cdmx":        d.get("stock_cdmx", 0),
                "bm_tj":          0,
                "bm_total":       d.get("stock_total", 0),
                "bm_avail":       d.get("stock_total", 0),
                "score":          0,
                "score_category": "candidate",
                "problems":       [],
                "stock_ml":       0,
                "sold_quantity":  0,
                "has_clip_video": False,
                "video_id":       None,
                "clip_status":    None,
                "has_ml_video":   False,
                "free_shipping":  False,
                "permalink":      "",
                "pics_count":     0,
            })
        return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("")
async def list_productos(
    status:         str = Query("all"),
    q:              str = Query(""),
    offset:         int = Query(0, ge=0),
    limit:          int = Query(50, ge=1, le=100),
    score_category: str = Query(""),
    sort_by:        str = Query(""),
):
    """Lista paginada de publicaciones ML + BM stock + health score.
    status: all | active | paused
    """
    user_id = _ctx.get()
    client  = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)

    try:
        statuses = ["active", "paused"] if status == "all" else [status]

        # 1. Recolectar todos los IDs de ML para los estados pedidos
        all_ids: list = []
        totals: dict  = {}
        for st in statuses:
            try:
                fetched_ids: list = []
                fetch_offset = 0
                while True:
                    data = await client.get_items(offset=fetch_offset, limit=200, status=st)
                    ids  = data.get("results", [])
                    tot  = data.get("paging", {}).get("total", 0)
                    totals[st] = tot
                    fetched_ids.extend(ids)
                    fetch_offset += len(ids)
                    if not ids or fetch_offset >= tot or fetch_offset >= 500:
                        break
                all_ids.extend(fetched_ids)
            except Exception as exc:
                logger.warning(f"get_items({st}) error: {exc}")

        grand_total = len(all_ids)

        # 2. Determinar qué IDs fetchear
        #    ml_search_ids  = IDs encontrados por ML Search API (pasan filtro automáticamente)
        #    pool_ids       = IDs del pool paginado (solo pasan si coinciden texto)
        ml_search_ids: set[str] = set()

        if q:
            # ML Search API: seller_sku (exacto por SKU) + q (por título/keyword)
            for params in [{"seller_sku": q, "limit": 50}, {"q": q, "limit": 50}]:
                try:
                    r = await client.get(
                        f"/users/{client.user_id}/items/search", params=params
                    )
                    ml_search_ids.update(r.get("results", []))
                except Exception:
                    pass

            # item_sku_cache local (fallback para SKUs sincronizados)
            try:
                async with aiosqlite.connect(DATABASE_PATH) as db:
                    cur = await db.execute(
                        "SELECT DISTINCT item_id FROM item_sku_cache "
                        "WHERE user_id=? AND sku LIKE ? LIMIT 50",
                        (user_id, f"%{q.upper()}%")
                    )
                    for row in await cur.fetchall():
                        ml_search_ids.add(row[0])
            except Exception:
                pass

            # fetch_ids = ML search results + primeros 200 del pool (para text match en título/id)
            # IMPORTANTE: los 200 del pool NO son "trusted" — pasan solo si coinciden texto
            fetch_ids = list(ml_search_ids) + [
                iid for iid in all_ids[:200] if iid not in ml_search_ids
            ]
        else:
            fetch_ids = all_ids[offset: offset + limit]

        if not fetch_ids:
            return {"items": [], "total": grand_total, "offset": offset, "limit": limit, "totals": totals}

        # 3. Fetch detalles en batches de 20
        all_items_raw: list = []
        for i in range(0, len(fetch_ids), 20):
            try:
                details = await client.get_items_details(fetch_ids[i:i+20])
                all_items_raw.extend(details)
            except Exception:
                pass

        # Para ítems del ML search que llegaron sin SKU (bug del bulk endpoint),
        # reintentar con GET /items/{id} individual
        if q and ml_search_ids:
            extracted_iids = {
                d.get("body", d).get("id", "")
                for d in all_items_raw
                if _extract_sku(d.get("body", d))
            }
            for iid in ml_search_ids:
                if iid not in extracted_iids:
                    try:
                        body = await client.get(f"/items/{iid}")
                        if isinstance(body, dict) and body.get("id"):
                            all_items_raw.append(body)
                    except Exception:
                        pass

        # 4. Deduplicar y extraer SKUs
        seen_bodies: set[str] = set()
        deduped_raw: list = []
        for d in all_items_raw:
            body = d.get("body", d)
            iid  = body.get("id", "")
            if iid and iid not in seen_bodies:
                seen_bodies.add(iid); deduped_raw.append(d)
        all_items_raw = deduped_raw

        sku_map: dict = {}
        for d in all_items_raw:
            body = d.get("body", d)
            iid  = body.get("id", "")
            if iid:
                sku_map[iid] = _extract_sku(body)

        unique_skus = list({s for s in sku_map.values() if s})

        # 5. Consulta BM en paralelo (cap 100 SKUs por batch)
        bm_results: dict = {}
        if unique_skus:
            async with httpx.AsyncClient() as hc:
                tasks = [(sku, _bm_stock(sku, hc)) for sku in unique_skus[:100]]
                raw   = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
                for (sku, _), res in zip(tasks, raw):
                    if not isinstance(res, Exception):
                        bm_results[sku] = res

        # 6. Video records desde DB
        video_recs = await get_videos_for_items(list(seen_bodies), user_id)

        # 7. Construir respuesta + filtro
        items = []
        for d in all_items_raw:
            body = d.get("body", d)
            if not body or not body.get("id"):
                continue
            iid  = body["id"]
            sku  = sku_map.get(iid, "")
            bm   = bm_results.get(sku)
            row  = _item_row(body, video_recs.get(iid), bm)

            if q:
                ql = q.lower()
                # Ítems de ML Search API: pasan directamente (ML ya los verificó)
                # Ítems del pool paginado: solo si coinciden por texto
                if iid not in ml_search_ids:
                    if not (ql in row["title"].lower()
                            or ql in row["sku"].lower()
                            or ql in row["item_id"].lower()):
                        continue
            if score_category and row.get("score_category") != score_category:
                continue
            items.append(row)

        # Apply sort_by
        if sort_by == "score_asc":
            items.sort(key=lambda x: x.get("score", 0))
        elif sort_by == "score_desc":
            items.sort(key=lambda x: x.get("score", 0), reverse=True)
        elif sort_by == "stock_asc":
            items.sort(key=lambda x: x.get("bm_total", 0))
        elif sort_by == "stock_desc":
            items.sort(key=lambda x: x.get("bm_total", 0), reverse=True)
        elif sort_by == "ventas_asc":
            items.sort(key=lambda x: x.get("sold_quantity", 0))
        elif sort_by == "ventas_desc":
            items.sort(key=lambda x: x.get("sold_quantity", 0), reverse=True)

        total_filtered = len(items) if q else grand_total
        if q:
            items = items[offset: offset + limit]

        return {"items": items, "total": total_filtered, "offset": offset, "limit": limit, "totals": totals}

    finally:
        await client.close()


@router.get("/{item_id}")
async def get_producto_detail(item_id: str):
    """Detalle completo de un item para el panel lateral."""
    user_id = _ctx.get()
    client  = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    try:
        body = await client.get(f"/items/{item_id}")
        if body.get("error"):
            return JSONResponse({"error": body.get("message", "not_found")}, status_code=404)

        # Descripción
        try:
            desc = await client.get(f"/items/{item_id}/description")
        except Exception:
            desc = {}

        # Video record
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM product_videos WHERE item_id=? AND user_id=?",
                (item_id, user_id)
            )).fetchone()
            video_rec = dict(row) if row else None

        sku = _extract_sku(body)
        bm  = {}
        if sku:
            async with httpx.AsyncClient() as hc:
                bm = await _bm_stock(sku, hc)

        return {
            **_item_row(body, video_rec, bm),
            "attributes":     body.get("attributes", []),
            "pictures":       body.get("pictures", []),
            "description":    desc.get("plain_text", ""),
            "category_id":    body.get("category_id", ""),
            "listing_type_id": body.get("listing_type_id", ""),
            "tags":           body.get("tags", []),
            "variations":     body.get("variations", []),
        }
    finally:
        await client.close()


@router.post("/{item_id}/clip")
async def upload_clip(item_id: str, request: Request):
    """Sube video clip a ML Clips para el item indicado.
    Body: {"video_id": "uuid", "sku": "SNTV007"}
    El video debe estar en _video_cache de lanzar.py.
    """
    from app.api.lanzar import _video_cache

    user_id = _ctx.get()
    body    = await request.json()
    video_id = body.get("video_id", "").strip()
    sku      = body.get("sku", "").strip()

    if not video_id:
        return JSONResponse({"error": "video_id requerido"}, status_code=400)

    video_bytes = _video_cache.get(video_id)
    if not video_bytes:
        # Intentar leer desde disco (persistencia entre requests)
        import os, pathlib
        disk_path = pathlib.Path(f"/tmp/lanzar_videos/{video_id}.mp4")
        if disk_path.exists():
            video_bytes = disk_path.read_bytes()
            _video_cache[video_id] = video_bytes  # re-cargar en memoria
        else:
            return JSONResponse({"error": "video_id no encontrado — regenera el video"}, status_code=404)

    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_meli_client"}, status_code=500)

    try:
        result = await client.post(
            f"/marketplace/items/{item_id}/clips/upload",
            files={"file": ("commercial.mp4", video_bytes, "video/mp4")},
        )
        clip_uuid   = result.get("clip_uuid") or result.get("id") or result.get("uuid")
        clip_status = result.get("status", "uploaded")
        logger.info(f"Clip upload {item_id}: uuid={clip_uuid} status={clip_status}")

        # Guardar en DB
        await save_product_video(item_id, user_id, sku, video_id)
        await update_clip_status(item_id, user_id, clip_status, clip_uuid=clip_uuid)

        return {"ok": True, "clip_uuid": clip_uuid, "status": clip_status, "raw": result}
    except Exception as e:
        err_str = str(e)
        logger.error(f"upload-clip error: {err_str}")
        # PolicyAgent error = la App no tiene permiso de Clips en ML Developer Portal
        if "PolicyAgent" in err_str or "UNAUTHORIZED" in err_str:
            err_str = (
                "La App no tiene permiso para subir clips. "
                "Ve a developers.mercadolibre.com.mx → tu App → Características → activa 'Video clips', "
                "luego vuelve a iniciar sesión en el dashboard."
            )
        await update_clip_status(item_id, user_id, "error", error=err_str)
        return JSONResponse({"error": err_str}, status_code=500)
    finally:
        await client.close()
