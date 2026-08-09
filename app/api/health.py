import asyncio

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel
from app.services.meli_client import get_meli_client, MeliApiError
from app.services import token_store as _ts
from app.services import user_store as _us


async def _log_history(request: Request, username: str, action: str, item_id: str, detail: dict, section: str = "Salud") -> None:
    """Registro best-effort en audit_log — nunca debe tumbar la acción principal."""
    try:
        user = getattr(request.state, "dashboard_user", {}) or {}
        ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else None)
        await _us.log_action(
            username=username, user_id=user.get("id"),
            action=action, item_id=item_id, detail=detail, ip=ip, section=section,
        )
    except Exception:
        pass

router = APIRouter(prefix="/api/health", tags=["health"])

# Cache de conversation_status por pack_id -- evita re-consultar ML en cada
# poll del KPI "Mensajes". TTL 10 min: el estado bloqueado/reclamo no cambia
# tan seguido como para justificar una llamada en vivo por cada poll.
_conv_status_cache: dict = {}
_CONV_STATUS_TTL = 600.0


async def _count_ml_pending_excluding_blocked(client) -> int:
    """Cuenta 'Mensajes pendientes' real: candidatos de la DB local (buyer +
    no resuelto) MENOS los que ya están bloqueados/movidos a Reclamos en ML
    (mediación, orden cancelada, etc.) -- esos ya NO son accionables desde
    Mensajes y ya se cuentan en el KPI de Reclamos aparte.

    Encontrado 2026-08-06: Jovan reportó que ML no mostraba NADA pendiente
    en su bandeja de Mensajes, pero nuestro KPI marcaba 37+. Confirmado con
    /api/diag/ml-pending-list?live_check=1: las conversaciones "pendientes"
    de nuestro conteo tenían conversation_status.status=='blocked' (mediación
    / orden cancelada) -- ya no viven en Mensajes para ML, solo para
    nuestro índice local desactualizado. No se puede filtrar esto con una
    query SQL pura (el estado bloqueado solo existe en vivo en ML), así que
    se hace un chequeo en vivo acotado con caché de 10 min para no golpear
    la API de ML en cada poll."""
    import time as _t
    acc = str(client.user_id)
    rows, _total = await _ts.get_message_index(acc, offset=0, limit=1000)
    views = await _ts.get_message_views([r["pack_id"] for r in rows], acc) if rows else {}

    from datetime import datetime as _dt

    def _iso_ts(iso_date):
        if not iso_date:
            return 0.0
        try:
            return _dt.fromisoformat(iso_date.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    candidates = []
    for r in rows:
        if r["last_message_from"] != "buyer":
            continue
        vi = views.get(r["pack_id"])
        resolved_info = vi if vi and vi.get("status") == "resolved" else None
        reopened = bool(resolved_info and _iso_ts(r["last_message_date"]) > (resolved_info.get("viewed_at") or 0))
        already_resolved = bool(resolved_info) and not reopened
        if not already_resolved:
            candidates.append(r["pack_id"])

    now = _t.time()
    to_check = [
        p for p in candidates
        if p not in _conv_status_cache or (now - _conv_status_cache[p][0]) > _CONV_STATUS_TTL
    ]
    if to_check:
        sem = asyncio.Semaphore(10)

        async def _check(pack_id):
            async with sem:
                try:
                    thread = await client.get_message_thread(pack_id)
                    status = (thread.get("conversation_status") or {}).get("status")
                except Exception:
                    status = None  # falla la consulta -> no se descarta, se cuenta como pendiente
                _conv_status_cache[pack_id] = (now, status)

        await asyncio.gather(*[_check(p) for p in to_check])

    blocked = sum(1 for p in candidates if _conv_status_cache.get(p, (0, None))[1] == "blocked")
    return len(candidates) - blocked


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
            # Igual que en /summary: cuenta pendientes reales (respeta
            # "Marcar resuelto" Y excluye bloqueadas/movidas a Reclamos).
            try:
                return await _count_ml_pending_excluding_blocked(client)
            except Exception:
                return 0

        questions, claims, messages = await asyncio.gather(_q(), _c(), _m())
        return {
            "ok": True,
            "unanswered_questions": questions,
            "open_claims": claims,
            "unread_messages": messages,
            "total": questions + claims + messages,
            # FIX 2026-08-08 (barrido final de fuentes duplicadas): este
            # endpoint colisionaba con otro `GET /api/health/counts` en
            # main.py (código muerto, nunca corría por first-match-wins de
            # FastAPI/orden de registro). base.html (badge global de
            # notificaciones) y dashboard.html (franja de alertas) se
            # escribieron esperando las claves del backend MUERTO
            # (claims/questions/messages) -- con el backend real (este)
            # devolviendo otros nombres, ambos elementos de UI llevaban
            # tiempo silenciosamente rotos (siempre 0/oculto). Se agregan
            # como alias sin quitar los nombres nuevos, por si algún otro
            # consumidor ya los usa.
            "claims": claims,
            "questions": questions,
            "messages": messages,
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
    account_id: str = ""  # opcional — solo lo manda la bandeja unificada "Todas las cuentas"


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

        # Mensajes pendientes -- respeta "Marcar resuelto" Y excluye
        # conversaciones bloqueadas/movidas a Reclamos (antes usaba el total
        # crudo de ML vía get_messages(limit=1), y luego solo un conteo local
        # que no sabía de bloqueos -- ver DEVLOG ambos casos).
        try:
            unread_messages = await _count_ml_pending_excluding_blocked(client)
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


@router.post("/messages/{pack_id}/take")
async def take_message(pack_id: str, request: Request, account_id: str = Query("", description="Solo lo manda la bandeja unificada")):
    """Asigna explícitamente esta conversación al usuario actual."""
    client = await get_meli_client(user_id=account_id or None)
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        user = getattr(request.state, "dashboard_user", {}) or {}
        username = user.get("sub") or user.get("name") or "?"
        acc = account_id or str(client.user_id)
        await _ts.take_message(pack_id, acc, username)
        await _log_history(request, username, "ml_message_take", pack_id, {"account_id": acc})
        return {"ok": True, "taken_by": username}
    finally:
        await client.close()


class MessageStatusRequest(BaseModel):
    status: str  # pending | in_progress | resolved
    account_id: str = ""  # opcional — solo lo manda la bandeja unificada


@router.post("/messages/{pack_id}/status")
async def update_message_status(pack_id: str, body: MessageStatusRequest, request: Request):
    """Actualiza el estado interno de una conversación."""
    client = await get_meli_client(user_id=body.account_id or None)
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    if body.status not in ("pending", "in_progress", "resolved"):
        raise HTTPException(status_code=400, detail="Status inválido")
    try:
        acc = body.account_id or str(client.user_id)
        user = getattr(request.state, "dashboard_user", {}) or {}
        username = user.get("sub") or user.get("name") or "?"
        await _ts.update_message_view_status(pack_id, acc, body.status, viewed_by=username)
        await _log_history(request, username, "ml_message_status", pack_id, {"account_id": acc, "status": body.status})
        return {"ok": True, "status": body.status}
    finally:
        await client.close()


@router.post("/claims/{claim_id}/take")
async def take_claim(claim_id: str, request: Request):
    """Asigna explícitamente este reclamo al usuario actual."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        user = getattr(request.state, "dashboard_user", {}) or {}
        username = user.get("sub") or user.get("name") or "?"
        account_id = str(client.user_id)
        await _ts.take_claim(claim_id, account_id, username)
        await _log_history(request, username, "ml_claim_take", f"claim:{claim_id}", {"account_id": account_id})
        return {"ok": True, "taken_by": username}
    finally:
        await client.close()


class ClaimStatusRequest(BaseModel):
    status: str  # pending | in_progress | resolved


@router.post("/claims/{claim_id}/status")
async def update_claim_status(claim_id: str, body: ClaimStatusRequest, request: Request):
    """Actualiza el estado interno de seguimiento de un reclamo."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    if body.status not in ("pending", "in_progress", "resolved"):
        raise HTTPException(status_code=400, detail="Status inválido")
    try:
        account_id = str(client.user_id)
        await _ts.update_claim_view_status(claim_id, account_id, body.status)
        user = getattr(request.state, "dashboard_user", {}) or {}
        username = user.get("sub") or user.get("name") or "?"
        await _log_history(request, username, "ml_claim_status", f"claim:{claim_id}", {"account_id": account_id, "status": body.status})
        return {"ok": True, "status": body.status}
    finally:
        await client.close()


@router.post("/messages/{pack_id}/send")
async def send_message(pack_id: str, body: MessageRequest, request: Request):
    """Enviar mensaje en una conversacion. account_id (opcional) permite
    responder desde la bandeja unificada sin depender de cuál cuenta esté
    'activa' en el navegador — sin él, se comporta exactamente igual que
    antes (cuenta activa vía cookie)."""
    client = await get_meli_client(user_id=body.account_id or None)
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        result = await client.send_message(pack_id, body.text)
        # FIX 2026-08-09: Jovan reporto con screenshot que una conversacion ya
        # respondida seguia apareciendo "Pendiente" en la bandeja -- el envio
        # nunca marcaba la conversacion como resuelta, se quedaba asi hasta el
        # proximo resync del indice desde la API de ML (que puede tardar).
        # Al responder, se marca resuelta de inmediato -- la logica de
        # _reopened_after_resolve (ya existente, ver health_messages_partial)
        # la reabre sola si el comprador vuelve a escribir despues de esto.
        try:
            acc = body.account_id or str(client.user_id)
            user = getattr(request.state, "dashboard_user", {}) or {}
            username = user.get("sub") or user.get("name") or "?"
            await _ts.update_message_view_status(pack_id, acc, "resolved", viewed_by=username)
        except Exception:
            pass  # best-effort -- nunca debe tumbar el envio ya exitoso
        return result
    except MeliApiError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=str(e))
    finally:
        await client.close()


@router.get("/messages/attachment/{filename}")
async def get_message_attachment(filename: str, account_id: str = Query("")):
    """Proxy de un adjunto de mensaje (foto que manda el comprador) -- el
    navegador no puede pedirle esto directo a ML (requiere Bearer token de
    la cuenta), así que lo bajamos nosotros y lo servimos. Encontrado
    2026-08-06: Jovan reportó que las fotos que manda el comprador (ej.
    screenshot de un error) no se veían en el hilo -- solo mostrábamos
    text.plain, y un mensaje que es solo una imagen no trae texto. El campo
    real es message_attachments[].filename (no "attachments")."""
    client = await get_meli_client(user_id=account_id or None)
    if not client:
        return Response(status_code=401)
    try:
        content = await client.download_binary(
            f"/messages/attachments/{filename}?tag=post_sale&site_id=MLM"
        )
        if not content:
            return Response(status_code=404)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "pdf": "application/pdf", "txt": "text/plain",
        }.get(ext, "application/octet-stream")
        return Response(content=content, media_type=content_type)
    finally:
        await client.close()


@router.get("/feedback")
async def get_feedback(request: Request, status: str = Query("pending")):
    """Reseñas ML negativas/neutras de la cuenta ML ACTIVA — nunca mezcladas
    con otras cuentas (regla del proyecto). El feedback de Amazon vive en su
    propio endpoint (ver amazon_products.py), acotado por seller_id."""
    if status not in ("pending", "handled"):
        raise HTTPException(status_code=400, detail="status inválido")
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    uid = str(client.user_id)
    await client.close()
    return {"items": await _ts.get_ml_feedback_tab(uid, status)}


class FeedbackStatusRequest(BaseModel):
    platform: str  # amazon | ml
    status: str    # pending | handled


@router.post("/feedback/{feedback_id}/status")
async def update_feedback_status(feedback_id: int, body: FeedbackStatusRequest, request: Request):
    """Marca un feedback/reseña como atendido (o lo regresa a pendiente)."""
    if body.platform not in ("amazon", "ml"):
        raise HTTPException(status_code=400, detail="platform inválido")
    if body.status not in ("pending", "handled"):
        raise HTTPException(status_code=400, detail="status inválido")
    ok = await _ts.set_feedback_status(body.platform, feedback_id, body.status)
    if not ok:
        raise HTTPException(status_code=404, detail="No encontrado")
    user = getattr(request.state, "dashboard_user", {}) or {}
    username = user.get("sub") or user.get("name") or "?"
    await _log_history(request, username, f"{body.platform}_feedback_status", f"feedback:{feedback_id}", {"status": body.status})
    return {"ok": True, "status": body.status}
