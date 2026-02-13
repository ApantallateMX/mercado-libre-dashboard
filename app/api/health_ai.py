"""AI-powered endpoints for the Health section (questions, claims, messages)."""

import json
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
