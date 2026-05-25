"""
Amazon Lanzar — wizard de creación de listings desde gaps BM → Amazon.

Flujo 1: SKU con ASIN existente → LISTING_OFFER_ONLY (solo agrega oferta)
Flujo 2: SKU sin ASIN            → LISTING (crea producto, Amazon asigna ASIN)
Flujo 3: Lanzados                → editar/actualizar listing activo
"""
import asyncio
import json
import logging
import math
import time as _time
from datetime import datetime
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import DATABASE_PATH, ANTHROPIC_API_KEY
from app.services.amazon_client import get_amazon_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/amazon/lanzar", tags=["amazon-lanzar"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# ─────────────────────────────────────────────────────────────────────────────
# SCAN BACKGROUND — Gap BM → Amazon (mismo patrón que ML Lanzador)
# ─────────────────────────────────────────────────────────────────────────────

_amz_scan_locks: dict[str, asyncio.Lock] = {}

_AMZ_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC", "-FLX01", "-FLX02")


def _amz_base(sku: str) -> str:
    u = (sku or "").upper()
    for s in _AMZ_SUFFIXES:
        if u.endswith(s):
            return u[:-len(s)]
    return u


async def _run_amz_gap_scan(seller_id: str) -> None:
    """Background task: compara BM stock vs Amazon activos → actualiza amz_sku_gaps."""
    if seller_id not in _amz_scan_locks:
        _amz_scan_locks[seller_id] = asyncio.Lock()
    lock = _amz_scan_locks[seller_id]
    if lock.locked():
        return

    async with lock:
        logger.info(f"[AMZ Gap Scan] Iniciando para seller {seller_id}")
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """INSERT INTO amz_gap_scan_status (seller_id, status, started_at, finished_at, error)
                   VALUES (?, 'running', ?, NULL, NULL)
                   ON CONFLICT(seller_id) DO UPDATE SET
                       status='running', started_at=excluded.started_at,
                       finished_at=NULL, error=NULL""",
                (seller_id, datetime.utcnow().isoformat()),
            )
            await db.commit()

        try:
            client = await get_amazon_client(seller_id=seller_id)
            if not client:
                raise Exception("Cuenta Amazon no encontrada")

            from app.services.binmanager_client import get_shared_bm
            bm_cli = await get_shared_bm()

            # Fetch en paralelo
            bm_items, listings = await asyncio.gather(
                bm_cli.get_bulk_stock(conditions="GRA,GRB,GRC,NEW,ICB,ICC"),
                client.get_all_listings(),
            )

            # Construir set de base-SKUs activos en Amazon
            amazon_base_skus: set[str] = set()
            for listing in listings:
                sku = listing.get("sku", "")
                if not sku:
                    continue
                summaries = listing.get("summaries", [])
                is_active = any(s.get("status") == "ACTIVE" for s in summaries)
                if is_active:
                    amazon_base_skus.add(sku.upper())
                    base = _amz_base(sku)
                    if base:
                        amazon_base_skus.add(base)

            amazon_active = sum(
                1 for l in listings
                if any(s.get("status") == "ACTIVE" for s in l.get("summaries", []))
            )

            FX = 18.5
            now_iso = datetime.utcnow().isoformat()
            gaps: list[dict] = []
            current_bm_skus: set[str] = set()

            for item in bm_items:
                bm_sku = (item.get("SKU") or "").strip()
                if not bm_sku:
                    continue
                avail_qty = int(item.get("AvailableQTY") or item.get("TotalQty") or 0)
                if avail_qty <= 0:
                    continue

                bm_sku_up = bm_sku.upper()
                current_bm_skus.add(bm_sku_up)

                already = bm_sku_up in amazon_base_skus or any(
                    (bm_sku_up + sfx) in amazon_base_skus for sfx in _AMZ_SUFFIXES
                )
                if already:
                    continue

                cost_usd = float(
                    item.get("LastRetailPricePurchaseHistory") or item.get("AvgCostQTY") or 0
                )
                cost_mxn = round(cost_usd * FX, 2) if cost_usd > 0 else 0
                price_sug = round(cost_mxn / 0.62, 0) if cost_mxn > 0 else 0
                margin_pct = None
                if cost_mxn > 0 and price_sug > 0:
                    margin_pct = round(
                        (price_sug - cost_mxn - price_sug * 0.18) / price_sug * 100, 1
                    )

                gaps.append({
                    "seller_id":      seller_id,
                    "sku":            bm_sku,
                    "product_title":  (item.get("Title") or item.get("Description") or "")[:120],
                    "brand":          item.get("Brand") or "",
                    "model":          item.get("Model") or "",
                    "category":       item.get("CategoryName") or "",
                    "image_url":      item.get("ImageURL") or "",
                    "upc":            item.get("UPC") or item.get("Upc") or "",
                    "avail_qty":      avail_qty,
                    "cost_usd":       round(cost_usd, 2),
                    "cost_mxn":       cost_mxn,
                    "suggested_price": price_sug,
                    "margin_pct":     margin_pct,
                    "last_scan":      now_iso,
                })

            # ── Guardar en DB ──────────────────────────────────────────────
            async with aiosqlite.connect(DATABASE_PATH) as db:
                # 1. Eliminar unlaunched que ya no tienen stock en BM
                if current_bm_skus:
                    placeholders = ",".join("?" * len(current_bm_skus))
                    await db.execute(
                        f"""DELETE FROM amz_sku_gaps
                            WHERE seller_id=? AND status='unlaunched'
                            AND UPPER(sku) NOT IN ({placeholders})""",
                        [seller_id] + list(current_bm_skus),
                    )

                # 2. Eliminar unlaunched que ahora están activos en Amazon
                for chunk_s in range(0, len(amazon_base_skus), 500):
                    chunk = list(amazon_base_skus)[chunk_s:chunk_s + 500]
                    if chunk:
                        ph = ",".join("?" * len(chunk))
                        await db.execute(
                            f"""DELETE FROM amz_sku_gaps
                                WHERE seller_id=? AND status='unlaunched'
                                AND UPPER(sku) IN ({ph})""",
                            [seller_id] + chunk,
                        )

                # 3. Upsert gaps (no toca los ignored ni launched)
                for g in gaps:
                    await db.execute(
                        """INSERT INTO amz_sku_gaps
                               (seller_id, sku, product_title, brand, model, category,
                                image_url, upc, avail_qty, cost_usd, cost_mxn,
                                suggested_price, margin_pct, last_scan, status)
                           VALUES
                               (:seller_id,:sku,:product_title,:brand,:model,:category,
                                :image_url,:upc,:avail_qty,:cost_usd,:cost_mxn,
                                :suggested_price,:margin_pct,:last_scan,'unlaunched')
                           ON CONFLICT(seller_id, sku) DO UPDATE SET
                               product_title=excluded.product_title,
                               brand=excluded.brand, model=excluded.model,
                               category=excluded.category, image_url=excluded.image_url,
                               upc=excluded.upc, avail_qty=excluded.avail_qty,
                               cost_usd=excluded.cost_usd, cost_mxn=excluded.cost_mxn,
                               suggested_price=excluded.suggested_price,
                               margin_pct=excluded.margin_pct, last_scan=excluded.last_scan
                           WHERE amz_sku_gaps.status='unlaunched'""",
                        g,
                    )

                # 4. Actualizar estado del scan
                await db.execute(
                    """INSERT INTO amz_gap_scan_status
                           (seller_id, status, finished_at, bm_total, amazon_active, gaps_found)
                       VALUES (?, 'done', ?, ?, ?, ?)
                       ON CONFLICT(seller_id) DO UPDATE SET
                           status='done', finished_at=excluded.finished_at,
                           bm_total=excluded.bm_total, amazon_active=excluded.amazon_active,
                           gaps_found=excluded.gaps_found, error=NULL""",
                    (seller_id, datetime.utcnow().isoformat(),
                     len(bm_items), amazon_active, len(gaps)),
                )
                await db.commit()

            logger.info(
                f"[AMZ Gap Scan] {seller_id}: {len(gaps)} gaps, "
                f"{len(bm_items)} BM SKUs, {amazon_active} Amazon activos"
            )

        except Exception as exc:
            logger.exception(f"[AMZ Gap Scan] Error para seller {seller_id}")
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.execute(
                    """INSERT INTO amz_gap_scan_status (seller_id, status, finished_at, error)
                       VALUES (?, 'error', ?, ?)
                       ON CONFLICT(seller_id) DO UPDATE SET
                           status='error', finished_at=excluded.finished_at, error=excluded.error""",
                    (seller_id, datetime.utcnow().isoformat(), str(exc)[:300]),
                )
                await db.commit()


@router.post("/scan", response_class=JSONResponse)
async def trigger_amz_scan(
    seller_id: Optional[str] = Query(None),
):
    """Lanza el escaneo de gaps BM→Amazon en background."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    sid = client.seller_id
    if sid in _amz_scan_locks and _amz_scan_locks[sid].locked():
        return JSONResponse({"status": "running"})
    asyncio.create_task(_run_amz_gap_scan(sid))
    return JSONResponse({"status": "started", "seller_id": sid})


@router.get("/scan/status", response_class=JSONResponse)
async def get_amz_scan_status(
    seller_id: Optional[str] = Query(None),
):
    """Polling del estado del scan actual."""
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"status": "no_account"})
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM amz_gap_scan_status WHERE seller_id=?",
            (client.seller_id,),
        )
        row = await cur.fetchone()
    if not row:
        return JSONResponse({"status": "never", "seller_id": client.seller_id})
    return JSONResponse(dict(row))


# ── helpers ──────────────────────────────────────────────────────────────────

async def _get_gap_status(seller_id: str) -> dict:
    """Devuelve dict sku→status para el seller."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT sku, status, asin FROM amz_sku_gaps WHERE seller_id=?",
            (seller_id,),
        )
        rows = await cur.fetchall()
    return {r["sku"].upper(): {"status": r["status"], "asin": r["asin"]} for r in rows}


# ── 1. Buscar ASIN en catálogo Amazon ────────────────────────────────────────

@router.get("/search-catalog")
async def search_catalog(
    q: str = Query("", description="Keyword o título"),
    upc: str = Query("", description="UPC/EAN del producto"),
    seller_id: Optional[str] = Query(None),
):
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    try:
        if upc:
            items = await client.search_catalog(identifiers=[upc.strip()])
        elif q:
            items = await client.search_catalog(keyword=q.strip())
        else:
            return JSONResponse({"items": []})
        return {"items": items}
    except Exception as e:
        logger.exception("[AMZ Lanzar] search-catalog error")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ── 2. Generar contenido IA (título + bullets) ───────────────────────────────

@router.post("/generate-content")
async def generate_content(request: Request):
    body = await request.json()
    title    = body.get("title", "")
    brand    = body.get("brand", "")
    category = body.get("category", "")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Genera contenido optimizado para Amazon México para este producto:
Título BM: {title}
Marca: {brand}
Categoría: {category}

Responde SOLO con JSON válido, sin texto extra:
{{
  "title": "Título para Amazon MX (máx 200 chars, incluye marca, modelo, atributo clave)",
  "bullets": [
    "✓ Bullet 1 — característica + beneficio concreto",
    "✓ Bullet 2",
    "✓ Bullet 3",
    "✓ Bullet 4",
    "✓ Bullet 5"
  ],
  "description": "Descripción en español, 2-3 párrafos, orientada a conversión"
}}"""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        start = text.index("{")
        end   = text.rindex("}") + 1
        data  = json.loads(text[start:end])
        return data
    except Exception as e:
        logger.warning(f"[AMZ Lanzar] generate-content error: {e}")
        return {"title": title, "bullets": [], "description": "", "error": str(e)[:100]}


# ── 3. Crear listing (Flujo 1 o Flujo 2) ────────────────────────────────────

@router.post("/create")
async def create_listing(request: Request):
    body = await request.json()
    seller_id   = body.get("seller_id")
    sku         = (body.get("sku") or "").strip()
    asin        = (body.get("asin") or "").strip()
    price       = float(body.get("price") or 0)
    condition   = body.get("condition", "new_new")
    fulfillment = body.get("fulfillment", "FBM")
    quantity    = int(body.get("quantity") or 0)
    title       = (body.get("title") or "")[:200]
    bullets     = body.get("bullets") or []
    description = (body.get("description") or "")[:2000]
    product_type = body.get("product_type") or "PRODUCT"

    if not sku:
        return JSONResponse({"error": "SKU requerido"}, status_code=400)
    if price <= 0:
        return JSONResponse({"error": "Precio inválido"}, status_code=400)

    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)

    if asin:
        # ── Flujo 1 — match a ASIN existente ──────────────────────────────
        requirements = "LISTING_OFFER_ONLY"
        attributes: dict = {
            "condition_type": [{"value": condition, "marketplace_id": client.marketplace_id}],
            "purchasable_offer": [{
                "currency": "MXN",
                "our_price": [{"schedule": [{"value_with_tax": price}]}],
                "marketplace_id": client.marketplace_id,
            }],
            "merchant_suggested_asin": [{"value": asin, "marketplace_id": client.marketplace_id}],
        }
        if fulfillment == "FBM" and quantity > 0:
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": quantity,
            }]
        elif fulfillment == "FBA":
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "AMAZON_NA",
            }]
    else:
        # ── Flujo 2 — producto nuevo ───────────────────────────────────────
        if not title:
            return JSONResponse({"error": "Título requerido para producto nuevo"}, status_code=400)
        requirements = "LISTING"
        attributes = {
            "condition_type": [{"value": condition, "marketplace_id": client.marketplace_id}],
            "purchasable_offer": [{
                "currency": "MXN",
                "our_price": [{"schedule": [{"value_with_tax": price}]}],
                "marketplace_id": client.marketplace_id,
            }],
            "item_name": [{"value": title, "marketplace_id": client.marketplace_id}],
        }
        if bullets:
            attributes["bullet_point"] = [
                {"value": b, "marketplace_id": client.marketplace_id}
                for b in bullets[:5] if b
            ]
        if description:
            attributes["product_description"] = [
                {"value": description, "marketplace_id": client.marketplace_id}
            ]
        if fulfillment == "FBM" and quantity > 0:
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": quantity,
            }]
        elif fulfillment == "FBA":
            attributes["fulfillment_availability"] = [{
                "fulfillment_channel_code": "AMAZON_NA",
            }]

    try:
        result = await client.create_listing_full(sku, product_type, attributes, requirements)
    except Exception as e:
        logger.exception("[AMZ Lanzar] create_listing_full error")
        return JSONResponse({"error": str(e)[:300]}, status_code=500)

    # Extraer ASIN de la respuesta (si Amazon lo asignó)
    status_resp = result.get("status", "")
    issues = result.get("issues") or []
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    if errors:
        return JSONResponse({
            "error": errors[0].get("message", "Error de validación Amazon"),
            "issues": issues,
        }, status_code=400)

    # ASIN nuevo para Flujo 2
    new_asin = asin
    try:
        ids = result.get("identifiers") or []
        for id_block in ids:
            mp_ids = id_block.get("marketplaceIdentifiers") or {}
            mp_data = mp_ids.get(client.marketplace_id) or {}
            if mp_data.get("asin"):
                new_asin = mp_data["asin"]
                break
    except Exception:
        pass

    # Persistir como "launched" en DB
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO amz_sku_gaps (seller_id, sku, asin, status, launched_price, launched_at)
               VALUES (?, ?, ?, 'launched', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(seller_id, sku) DO UPDATE SET
                   asin=excluded.asin, status='launched',
                   launched_price=excluded.launched_price,
                   launched_at=CURRENT_TIMESTAMP""",
            (client.seller_id, sku.upper(), new_asin, price),
        )
        await db.commit()

    return {"ok": True, "asin": new_asin, "status": status_resp, "sku": sku}


# ── 4. Ignorar gap ────────────────────────────────────────────────────────────

@router.post("/ignore/{sku}")
async def ignore_gap(sku: str, request: Request):
    body = await request.json()
    seller_id = body.get("seller_id")
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO amz_sku_gaps (seller_id, sku, status)
               VALUES (?, ?, 'ignored')
               ON CONFLICT(seller_id, sku) DO UPDATE SET status='ignored'""",
            (client.seller_id, sku.upper()),
        )
        await db.commit()
    return {"ok": True}


# ── 5. Restaurar gap ignorado ─────────────────────────────────────────────────

@router.post("/restore/{sku}")
async def restore_gap(sku: str, request: Request):
    body = await request.json()
    seller_id = body.get("seller_id")
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return JSONResponse({"error": "no_account"}, status_code=401)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE amz_sku_gaps SET status='unlaunched' WHERE seller_id=? AND sku=?",
            (client.seller_id, sku.upper()),
        )
        await db.commit()
    return {"ok": True}


# ── 6. Tab Lanzados ───────────────────────────────────────────────────────────

@router.get("/launched", response_class=HTMLResponse)
async def get_launched(
    request: Request,
    seller_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
    q: str = Query(""),
):
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return HTMLResponse("<div class='p-6 text-red-500 text-center font-semibold'>Sin cuenta Amazon conectada</div>")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT g.*, l.title as live_title, l.status as live_status, l.price as live_price, l.asin as live_asin
               FROM amz_sku_gaps g
               LEFT JOIN amazon_listings l ON l.seller_id=g.seller_id AND UPPER(l.sku)=g.sku
               WHERE g.seller_id=? AND g.status='launched'
               ORDER BY g.launched_at DESC""",
            (client.seller_id,),
        )
        rows_all = [dict(r) for r in await cur.fetchall()]

    q_lower = q.strip().lower()
    if q_lower:
        rows_all = [r for r in rows_all if q_lower in r["sku"].lower() or q_lower in (r.get("product_title") or "").lower()]

    total = len(rows_all)
    pages = max(1, math.ceil(total / per_page))
    page  = max(1, min(page, pages))
    rows  = rows_all[(page - 1) * per_page: page * per_page]

    return _templates.TemplateResponse(request, "partials/amazon_lanzados.html", {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "q": q,
        "marketplace": client.marketplace_name,
        "seller_id": client.seller_id,
    })


# ── 7. Tab Ignorados ──────────────────────────────────────────────────────────

@router.get("/ignored", response_class=HTMLResponse)
async def get_ignored(
    request: Request,
    seller_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20),
):
    client = await get_amazon_client(seller_id=seller_id)
    if not client:
        return HTMLResponse("<div class='p-6 text-red-500 text-center font-semibold'>Sin cuenta Amazon conectada</div>")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM amz_sku_gaps WHERE seller_id=? AND status='ignored' ORDER BY created_at DESC",
            (client.seller_id,),
        )
        rows_all = [dict(r) for r in await cur.fetchall()]

    total = len(rows_all)
    pages = max(1, math.ceil(total / per_page))
    page  = max(1, min(page, pages))
    rows  = rows_all[(page - 1) * per_page: page * per_page]

    return _templates.TemplateResponse(request, "partials/amazon_ignorados.html", {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "seller_id": client.seller_id,
    })
