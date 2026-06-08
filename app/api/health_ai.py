"""AI-powered endpoints for the Health section (questions, claims, messages)."""

import asyncio
import json
import logging
import os
import re
import urllib.parse
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

logger = logging.getLogger(__name__)

from app.services import claude_client, openrouter_client
from app.services.health_ai import (
    build_question_answer_prompt,
    build_claim_response_prompt,
    build_claim_analysis_prompt,
    build_message_reply_prompt,
    parse_claim_analysis,
)

_BM_INVENTORY_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
_BM_COMPANY_ID = 1
_BM_CONCEPT_ID = 8


async def _fetch_bm_product(sku: str) -> dict:
    """Fetch Brand, Model, Title, Description from BinManager for a given SKU.
    Returns empty dict on failure or missing SKU.
    """
    if not sku:
        return {}
    from app.services.binmanager_client import bm_post as _bm_post_hai
    try:
        resp = await _bm_post_hai(_BM_INVENTORY_URL, {
            "COMPANYID": _BM_COMPANY_ID,
            "SEARCH": sku,
            "CONCEPTID": _BM_CONCEPT_ID,
            "NUMBERPAGE": 1,
            "RECORDSPAGE": 10,
        }, timeout=10.0)
        if resp and resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list):
                row = data[0]
                for item in data:
                    if item.get("SKU", "").upper() == sku.upper():
                        row = item
                        break
                return {
                    "brand": row.get("Brand", "") or "",
                    "model": row.get("Model", "") or "",
                    "title": row.get("Title", "") or "",
                    "description": row.get("Description", "") or "",
                    "upc": row.get("UPC", "") or "",
                    "category": row.get("CategoryName", "") or "",
                }
    except Exception:
        pass
    return {}

async def _get_ml_token(request: Request) -> str:
    """Return the active ML access_token from DB, or '' if unavailable."""
    import aiosqlite
    from app.services.token_store import DATABASE_PATH
    uid = request.cookies.get("active_account_id", "")
    if not uid:
        return ""
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute("SELECT access_token FROM tokens WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


async def _fetch_ml_item_details(item_id: str, ml_token: str) -> dict:
    """
    Fetch full ML item: attributes + description + catalog product specs.
    Returns {"attributes": [...], "description": "...", "catalog_attrs": [...]} or {}.
    """
    if not item_id or not ml_token:
        return {}
    headers = {"Authorization": f"Bearer {ml_token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            item_r, desc_r = await asyncio.gather(
                client.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers),
                client.get(f"https://api.mercadolibre.com/items/{item_id}/descriptions", headers=headers),
                return_exceptions=True,
            )

        attrs = []
        catalog_product_id = None
        if not isinstance(item_r, Exception) and item_r.is_success:
            item_data = item_r.json()
            attrs = item_data.get("attributes", [])
            catalog_product_id = item_data.get("catalog_product_id")

        desc_text = ""
        if not isinstance(desc_r, Exception) and desc_r.is_success:
            descs = desc_r.json()
            if isinstance(descs, list) and descs:
                desc_text = (descs[0].get("plain_text") or "")[:2000]

        # Fetch ML catalog product for comprehensive manufacturer specs
        catalog_attrs = []
        if catalog_product_id:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    cat_r = await client.get(
                        f"https://api.mercadolibre.com/products/{catalog_product_id}",
                        headers=headers,
                    )
                if cat_r.is_success:
                    cat_data = cat_r.json()
                    catalog_attrs = cat_data.get("attributes", [])
                    # Also add catalog description if item description is empty
                    if not desc_text:
                        for d in cat_data.get("descriptions", []):
                            if d.get("content"):
                                desc_text = d["content"][:2000]
                                break
                    logger.info(f"[ML catalog] {catalog_product_id}: {len(catalog_attrs)} attrs")
            except Exception as e:
                logger.warning(f"[ML catalog] {catalog_product_id}: {e}")

        # Merge: catalog attrs first (more complete), then item attrs for any extras
        merged_ids = {a.get("id") for a in catalog_attrs}
        for a in attrs:
            if a.get("id") not in merged_ids:
                catalog_attrs.append(a)

        return {
            "attributes": catalog_attrs or attrs,
            "description": desc_text,
            "catalog_product_id": catalog_product_id,
        }
    except Exception as e:
        logger.warning(f"[ML item fetch] {item_id}: {e}")
        return {}


async def _web_search_product_specs(
    brand: str, model: str, category: str = "", product_title: str = ""
) -> str:
    """
    Fetch product specs from the web via Jina Reader (r.jina.ai).
    Sources:
      1. rtings.com — specs técnicas completas para TVs/monitores
      2. Amazon MX search — funciona para cualquier tipo de producto
    Returns raw text (max 5000 chars) or '' on failure.
    """
    JINA_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/plain",
        "X-Return-Format": "text",
    }
    results = []

    async with httpx.AsyncClient(timeout=12.0, headers=JINA_HEADERS) as client:
        tasks = []

        # Source 1: rtings.com — solo para TVs/pantallas
        is_tv = any(kw in (category + " " + product_title).lower()
                    for kw in ["tv", "television", "pantalla", "qled", "oled",
                               "uhd", "smart tv", "monitor", "display"])
        if is_tv and brand and model:
            model_slug = re.sub(r"^\d+", "", model).lower()  # 55Q550G → q550g
            rtings_url = (
                f"https://r.jina.ai/https://www.rtings.com/tv/reviews"
                f"/{brand.lower()}/{model_slug}"
            )
            tasks.append(("rtings", client.get(rtings_url)))

        # Source 2: Amazon MX search — funciona para cualquier producto
        search_q = f"{brand} {model}".strip() if (brand or model) else product_title[:80]
        if search_q:
            amz_url = (
                f"https://r.jina.ai/https://www.amazon.com.mx/s"
                f"?k={urllib.parse.quote(search_q)}"
            )
            tasks.append(("amazon_mx", client.get(amz_url)))

        # Fire all requests concurrently
        for name, coro in tasks:
            try:
                resp = await asyncio.wait_for(coro, timeout=10.0)
                if resp.is_success and len(resp.text) > 300:
                    results.append(f"[{name}]\n{resp.text[:2500]}")
                    logger.info(f"[WebSearch] {name}: OK {len(resp.text)} chars for {brand} {model}")
                else:
                    logger.warning(f"[WebSearch] {name}: short/empty ({len(resp.text)} chars)")
            except Exception as e:
                logger.warning(f"[WebSearch] {name}: {e}")

    combined = "\n\n".join(results)
    return combined[:5000] if combined else ""


router = APIRouter(prefix="/api/health-ai", tags=["health-ai"])

_UNAVAILABLE_MSG = "API de IA no disponible. Configura OPENROUTER_API_KEY o ANTHROPIC_API_KEY."


def _ai_available() -> bool:
    """True si OpenRouter O Claude están disponibles."""
    return openrouter_client.is_available() or claude_client.is_available()


@router.get("/debug-key")
async def debug_key():
    """Diagnose Anthropic API key configuration (masked for security)."""
    from app.config import ANTHROPIC_API_KEY
    key = ANTHROPIC_API_KEY or ""
    raw_env = os.getenv("ANTHROPIC_API_KEY", "")
    p1 = os.getenv("AI_KEY_P1", "")
    p2 = os.getenv("AI_KEY_P2", "")
    masked = (key[:8] + "..." + key[-4:]) if len(key) > 12 else ("(vacía)" if not key else "(muy corta: " + str(len(key)) + " chars)")
    source = "ANTHROPIC_API_KEY env" if raw_env else ("AI_KEY_P1+P2 reconstruida" if (p1 and p2) else "no configurada")

    # Key from claude_client._get_key() — what generate_stream actually uses
    client_key = claude_client._get_key()
    client_masked = (client_key[:8] + "..." + client_key[-4:]) if len(client_key) > 12 else ("(vacía)" if not client_key else f"(corta: {len(client_key)})")
    keys_match = (key == client_key)

    # Quick test call using config key
    test_result = None
    if key and len(key) > 10:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 5, "messages": [{"role": "user", "content": "ping"}]},
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                )
                if r.status_code == 200:
                    test_result = "✅ Key válida — API responde OK"
                else:
                    try:
                        err = r.json().get("error", {}).get("message", r.text[:200])
                    except Exception:
                        err = r.text[:200]
                    test_result = f"❌ Error {r.status_code}: {err}"
        except Exception as e:
            test_result = f"❌ Error de conexión: {e}"
    else:
        test_result = "❌ Key no configurada o muy corta"

    # Test generate_stream directly (what the AI buttons use)
    stream_test = None
    try:
        chunks = []
        async for chunk in claude_client.generate_stream("di hola", max_tokens=10):
            chunks.append(chunk)
            if len(chunks) >= 5:
                break
        stream_test = "✅ Stream OK: " + "".join(chunks)
    except Exception as e:
        stream_test = f"❌ Stream error: {e}"

    return {
        "config_key_masked": masked,
        "client_key_masked": client_masked,
        "keys_match": keys_match,
        "source": source,
        "length": len(key),
        "available": claude_client.is_available(),
        "test": test_result,
        "stream_test": stream_test,
    }


async def _sse_stream(system: str, prompt: str, max_tokens: int):
    """Yield SSE events — OpenRouter primario, Claude fallback."""
    try:
        if openrouter_client.is_available():
            async for chunk in openrouter_client.generate_stream(prompt, system=system, max_tokens=max_tokens):
                yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        else:
            async for chunk in claude_client.generate_stream(prompt, system=system, max_tokens=max_tokens):
                yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        # Si OpenRouter falla, intentar Claude como fallback
        if openrouter_client.is_available() and claude_client.is_available():
            try:
                async for chunk in claude_client.generate_stream(prompt, system=system, max_tokens=max_tokens):
                    yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            except Exception:
                pass
        yield f"data: [ERROR] {e}\n\n"


async def _get_seller_nickname(request: Request) -> str:
    """Devuelve el nickname del vendedor activo desde la DB, o '' si no se encuentra."""
    import aiosqlite
    from app.services.token_store import DATABASE_PATH
    uid = request.cookies.get("active_account_id", "")
    if not uid:
        return ""
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute("SELECT nickname FROM tokens WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


@router.post("/suggest-answer")
async def suggest_answer(request: Request):
    """SSE stream: suggest an answer for a buyer question."""
    if not _ai_available():
        return JSONResponse({"error": _UNAVAILABLE_MSG}, status_code=503)
    body = await request.json()
    sku = body.get("sku", "")
    item_id = body.get("item_id", "")

    # Fetch BM product + ML token concurrently (fast local ops)
    bm_product, ml_token, seller_name = await asyncio.gather(
        _fetch_bm_product(sku),
        _get_ml_token(request),
        _get_seller_nickname(request),
    )

    brand = (bm_product or {}).get("brand", "")
    model = (bm_product or {}).get("model", "")

    question_text = body.get("question_text", "")

    category = (bm_product or {}).get("category", "")
    product_title = body.get("product_title", "")

    # Fetch ML full attrs+description AND web search in parallel
    ml_details, web_specs = await asyncio.gather(
        _fetch_ml_item_details(item_id, ml_token),
        _web_search_product_specs(brand, model, category, product_title),
    )

    # Merge ML attributes: prefer full set from API over what came from the frontend
    ml_attrs = ml_details.get("attributes") or body.get("product_attributes", [])
    ml_description = ml_details.get("description", "")

    system, prompt, max_tokens = build_question_answer_prompt(
        question_text,
        body.get("product_title", ""),
        body.get("product_price", 0),
        body.get("product_stock", 0),
        body.get("elapsed", ""),
        buyer_history=body.get("buyer_history", []),
        user_context=body.get("user_context", ""),
        bm_product=bm_product,
        product_permalink=body.get("product_permalink", ""),
        product_attributes=ml_attrs,
        same_item_history=body.get("same_item_history", []),
        related_listings=body.get("related_listings", []),
        seller_name=seller_name,
        ml_description=ml_description,
        web_specs=web_specs,
    )
    return StreamingResponse(
        _sse_stream(system, prompt, max_tokens),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/suggest-claim-response")
async def suggest_claim_response(request: Request):
    """SSE stream: suggest a response message for a claim."""
    if not _ai_available():
        return JSONResponse({"error": _UNAVAILABLE_MSG}, status_code=503)
    body = await request.json()
    sku = body.get("sku", "")
    bm_product = await _fetch_bm_product(sku)
    system, prompt, max_tokens = build_claim_response_prompt(
        body.get("claim_id", ""),
        body.get("reason_id", ""),
        body.get("reason_desc", ""),
        body.get("product_title", ""),
        body.get("days_open", 0),
        body.get("issues", []),
        body.get("suggestions", []),
        bm_product=bm_product,
    )
    return StreamingResponse(
        _sse_stream(system, prompt, max_tokens),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/claim-analysis")
async def claim_analysis(request: Request):
    """JSON: structured analysis of a claim (recommendation, financials, pros/cons)."""
    if not _ai_available():
        return JSONResponse({"error": _UNAVAILABLE_MSG}, status_code=503)
    body = await request.json()
    sku = body.get("sku", "")
    bm_product = await _fetch_bm_product(sku)
    system, prompt, max_tokens = build_claim_analysis_prompt(
        body.get("reason_desc", ""),
        body.get("product_title", ""),
        body.get("product_price", 0),
        body.get("days_open", 0),
        body.get("claims_rate", 0),
        body.get("claims_status", ""),
        body.get("sale_fee", 0),
        body.get("shipping_cost", 0),
        bm_product=bm_product,
    )
    try:
        if openrouter_client.is_available():
            raw = await openrouter_client.generate(prompt, system=system, max_tokens=max_tokens)
        else:
            raw = await claude_client.generate(prompt, system=system, max_tokens=max_tokens)
        analysis = parse_claim_analysis(raw)
        return JSONResponse(analysis)
    except Exception as e:
        # Fallback
        try:
            raw = await claude_client.generate(prompt, system=system, max_tokens=max_tokens)
            return JSONResponse(parse_claim_analysis(raw))
        except Exception:
            return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/suggest-message")
async def suggest_message(request: Request):
    """SSE stream: suggest a reply for a post-sale message thread."""
    if not _ai_available():
        return JSONResponse({"error": _UNAVAILABLE_MSG}, status_code=503)
    body = await request.json()
    system, prompt, max_tokens = build_message_reply_prompt(
        body.get("thread_messages", []),
        body.get("last_buyer_message", ""),
    )
    return StreamingResponse(
        _sse_stream(system, prompt, max_tokens),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
