"""Endpoints del chat con asistente IA."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.ollama_client import OllamaClient
from app.services.meli_client import get_meli_client
from app.services import chat_store
from app.services.ai_assistant import process_message
from app.config import CHAT_HISTORY_LIMIT

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatMessage(BaseModel):
    message: str


@router.post("/message")
async def send_message(body: ChatMessage):
    """Recibe un mensaje y retorna la respuesta del asistente via SSE."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    ollama = OllamaClient()
    if not await ollama.is_available():
        await client.close()
        raise HTTPException(status_code=503, detail="Ollama no esta disponible. Asegurate de que este corriendo.")

    try:
        user = await client.get_user_info()
        user_id = str(user.get("id", "default"))

        # Obtener o crear sesion
        session_id = await chat_store.get_or_create_session(user_id)

        # Guardar mensaje del usuario
        await chat_store.save_message(session_id, "user", body.message)

        # Obtener historial
        history = await chat_store.get_history(session_id, limit=CHAT_HISTORY_LIMIT)
        # Quitar el ultimo mensaje (el que acabamos de guardar) del historial para no duplicar
        if history and history[-1]["role"] == "user" and history[-1]["content"] == body.message:
            history = history[:-1]

        async def event_stream():
            full_response = ""
            try:
                async for token in process_message(body.message, history, client, ollama):
                    full_response += token
                    # SSE format
                    yield f"data: {token}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                # Guardar respuesta del asistente
                if full_response:
                    await chat_store.save_message(session_id, "assistant", full_response)
                await client.close()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    except HTTPException:
        await client.close()
        raise
    except Exception as e:
        await client.close()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_history():
    """Retorna el historial de la sesion actual."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        user = await client.get_user_info()
        user_id = str(user.get("id", "default"))
        session_id = await chat_store.get_or_create_session(user_id)
        history = await chat_store.get_history(session_id, limit=50)
        return {"messages": history}
    finally:
        await client.close()


@router.post("/clear")
async def clear_history():
    """Limpia el historial de chat."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        user = await client.get_user_info()
        user_id = str(user.get("id", "default"))
        await chat_store.clear_session(user_id)
        return {"status": "ok"}
    finally:
        await client.close()


@router.get("/health")
async def chat_health():
    """Verifica si Ollama esta disponible."""
    ollama = OllamaClient()
    available = await ollama.is_available()
    return {"ollama_available": available}
