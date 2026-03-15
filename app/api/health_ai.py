"""AI-powered endpoints for the Health section (questions, claims, messages)."""

import json
import os
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from app.services import claude_client
from app.services.health_ai import (
    build_question_answer_prompt,
    build_claim_response_prompt,
    build_claim_analysis_prompt,
    build_message_reply_prompt,
    parse_claim_analysis,
)

router = APIRouter(prefix="/api/health-ai", tags=["health-ai"])

_UNAVAILABLE_MSG = "API de IA no disponible. Configura ANTHROPIC_API_KEY en el servidor."


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
    """Yield SSE events from a Claude streaming response."""
    try:
        async for chunk in claude_client.generate_stream(prompt, system=system, max_tokens=max_tokens):
            yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        yield f"data: [ERROR] {e}\n\n"


@router.post("/suggest-answer")
async def suggest_answer(request: Request):
    """SSE stream: suggest an answer for a buyer question."""
    if not claude_client.is_available():
        return JSONResponse({"error": _UNAVAILABLE_MSG}, status_code=503)
    body = await request.json()
    system, prompt, max_tokens = build_question_answer_prompt(
        body.get("question_text", ""),
        body.get("product_title", ""),
        body.get("product_price", 0),
        body.get("product_stock", 0),
        body.get("elapsed", ""),
        buyer_history=body.get("buyer_history", []),
        user_context=body.get("user_context", ""),
    )
    return StreamingResponse(
        _sse_stream(system, prompt, max_tokens),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/suggest-claim-response")
async def suggest_claim_response(request: Request):
    """SSE stream: suggest a response message for a claim."""
    if not claude_client.is_available():
        return JSONResponse({"error": _UNAVAILABLE_MSG}, status_code=503)
    body = await request.json()
    system, prompt, max_tokens = build_claim_response_prompt(
        body.get("claim_id", ""),
        body.get("reason_id", ""),
        body.get("reason_desc", ""),
        body.get("product_title", ""),
        body.get("days_open", 0),
        body.get("issues", []),
        body.get("suggestions", []),
    )
    return StreamingResponse(
        _sse_stream(system, prompt, max_tokens),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/claim-analysis")
async def claim_analysis(request: Request):
    """JSON: structured analysis of a claim (recommendation, financials, pros/cons)."""
    if not claude_client.is_available():
        return JSONResponse({"error": _UNAVAILABLE_MSG}, status_code=503)
    body = await request.json()
    system, prompt, max_tokens = build_claim_analysis_prompt(
        body.get("reason_desc", ""),
        body.get("product_title", ""),
        body.get("product_price", 0),
        body.get("days_open", 0),
        body.get("claims_rate", 0),
        body.get("claims_status", ""),
        body.get("sale_fee", 0),
        body.get("shipping_cost", 0),
    )
    try:
        raw = await claude_client.generate(prompt, system=system, max_tokens=max_tokens)
        analysis = parse_claim_analysis(raw)
        return JSONResponse(analysis)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/suggest-message")
async def suggest_message(request: Request):
    """SSE stream: suggest a reply for a post-sale message thread."""
    if not claude_client.is_available():
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
