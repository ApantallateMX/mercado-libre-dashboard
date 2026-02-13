import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from app.services.meli_client import get_meli_client, MeliApiError

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/counts")
async def health_counts():
    """Conteos ligeros para polling de notificaciones."""
    client = await get_meli_client()
    if not client:
        return {"ok": False}
    try:
        async def _q():
            try:
                r = await client.get_questions(status="UNANSWERED", limit=1)
                return r.get("paging", {}).get("total", 0)
            except Exception:
                return 0

        async def _c():
            try:
                r = await client.get_claims(limit=1, status="opened")
                return r.get("paging", {}).get("total", 0)
            except Exception:
                return 0

        async def _m():
            try:
                r = await client.get_messages(limit=1)
                return r.get("paging", {}).get("total", 0)
            except Exception:
                return 0

        questions, claims, messages = await asyncio.gather(_q(), _c(), _m())
        return {
            "ok": True,
            "unanswered_questions": questions,
            "open_claims": claims,
            "unread_messages": messages,
            "total": questions + claims + messages,
        }
    finally:
        await client.close()


class AnswerRequest(BaseModel):
    text: str


class ClaimResponse(BaseModel):
    action: str
    text: str


class MessageRequest(BaseModel):
    text: str


@router.get("/summary")
async def health_summary():
    """KPIs de salud: reputacion, reclamos, preguntas, mensajes."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        user = await client.get_user_info()
        reputation = user.get("seller_reputation", {})

        # Preguntas sin responder
        try:
            questions = await client.get_questions(status="UNANSWERED", limit=1)
            unanswered_questions = questions.get("paging", {}).get("total", 0)
        except Exception:
            unanswered_questions = 0

        # Reclamos abiertos
        try:
            claims = await client.get_claims(limit=1, status="opened")
            open_claims = claims.get("paging", {}).get("total", 0)
        except Exception:
            open_claims = 0

        # Mensajes sin leer
        try:
            messages = await client.get_messages(limit=1)
            unread_messages = messages.get("paging", {}).get("total", 0)
        except Exception:
            unread_messages = 0

        # Reputacion
        level = reputation.get("level_id", "unknown")
        transactions = reputation.get("transactions", {})
        ratings = transactions.get("ratings", {})
        metrics = reputation.get("metrics", {})

        cancellations = metrics.get("cancellations", {})
        claims_metric = metrics.get("claims", {})
        delayed = metrics.get("delayed_handling_time", {})

        return {
            "reputation_level": level,
            "power_seller_status": reputation.get("power_seller_status", None),
            "transactions_completed": transactions.get("completed", 0),
            "transactions_canceled": transactions.get("canceled", 0),
            "ratings": {
                "positive": ratings.get("positive", 0),
                "negative": ratings.get("negative", 0),
                "neutral": ratings.get("neutral", 0),
            },
            "cancellation_rate": cancellations.get("rate", 0),
            "claims_rate": claims_metric.get("rate", 0),
            "delayed_rate": delayed.get("rate", 0),
            "open_claims": open_claims,
            "unanswered_questions": unanswered_questions,
            "unread_messages": unread_messages,
        }
    finally:
        await client.close()


@router.get("/claims")
async def list_claims(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query("", description="Filter by status"),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    """Lista reclamos con paginacion."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        params_status = status if status else None
        df = date_from or None
        dt = date_to or None
        data = await client.get_claims(offset=offset, limit=limit, status=params_status,
                                       date_from=df, date_to=dt)
        return data
    finally:
        await client.close()


@router.get("/claims/{claim_id}")
async def get_claim(claim_id: str):
    """Detalle de un reclamo."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        data = await client.get_claim_detail(claim_id)
        return data
    finally:
        await client.close()


@router.post("/claims/{claim_id}/respond")
async def respond_claim(claim_id: str, body: ClaimResponse):
    """Responder a un reclamo."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.respond_claim(claim_id, body.action, body.text)
        return result
    except MeliApiError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=str(e))
    finally:
        await client.close()


@router.get("/questions")
async def list_questions(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query("UNANSWERED"),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    """Lista preguntas."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        df = date_from or None
        dt = date_to or None
        data = await client.get_questions(status=status, offset=offset, limit=limit,
                                          date_from=df, date_to=dt)
        return data
    finally:
        await client.close()


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: int, body: AnswerRequest):
    """Responder una pregunta."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.answer_question(question_id, body.text)
        return result
    except MeliApiError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=str(e))
    finally:
        await client.close()


@router.delete("/questions/{question_id}")
async def delete_question(question_id: int):
    """Eliminar una pregunta."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.delete_question(question_id)
        return {"ok": True, "id": question_id}
    except MeliApiError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=str(e))
    finally:
        await client.close()


@router.get("/messages")
async def list_messages(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    """Lista conversaciones/mensajes."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        df = date_from or None
        dt = date_to or None
        data = await client.get_messages(offset=offset, limit=limit,
                                         date_from=df, date_to=dt)
        return data
    finally:
        await client.close()


@router.post("/messages/{pack_id}/send")
async def send_message(pack_id: str, body: MessageRequest):
    """Enviar mensaje en una conversacion."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.send_message(pack_id, body.text)
        return result
    except MeliApiError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=str(e))
    finally:
        await client.close()
