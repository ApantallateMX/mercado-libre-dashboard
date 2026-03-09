"""
app/api/agents_router.py

Router FastAPI para el sistema de agentes AI.

Endpoints:
  GET  /agents/                     → Página HTML del dashboard de agentes
  POST /agents/chat                 → Chat con el orquestador (SSE streaming)
  GET  /agents/status               → Estado JSON de todos los agentes y alertas
  POST /agents/run/{agent_name}     → Ejecutar un agente específico
  GET  /agents/history/{session_id} → Historial de conversación (HTML partial)
  GET  /agents/alerts               → Alertas como HTML partial
  POST /agents/alerts/{id}/read     → Marcar alerta como leída
  POST /agents/alerts/read-all      → Marcar todas las alertas como leídas
  POST /agents/scan                 → Dispara scan completo en background
  GET  /agents/scheduler/jobs       → Lista jobs del scheduler
  POST /agents/scheduler/jobs/{id}/toggle → Pausa/reanuda un job

Autenticación: el AuthMiddleware de main.py ya setea request.state.dashboard_user
en cada request autenticado. Todos los endpoints llaman a _get_user() que
levanta 401 si no hay sesión activa.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.services.agents.orchestrator import orchestrator
from app.services.agents import build_agent_registry
from app.services.memory_manager import memory_manager
from app.services.scheduler_service import scheduler_service

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Router + Templates
# ─────────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/agents", tags=["agents"])

_templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# ─────────────────────────────────────────────────────────────────────────────
# Auth helper — same pattern as app/api/users.py
# ─────────────────────────────────────────────────────────────────────────────

def _get_user(request: Request) -> dict:
    """
    Extrae el usuario de request.state.dashboard_user (puesto por AuthMiddleware).
    Lanza HTTP 401 si no hay sesión activa.
    """
    du = getattr(request.state, "dashboard_user", None)
    if not du:
        raise HTTPException(status_code=401, detail="No autenticado")
    return du


# ─────────────────────────────────────────────────────────────────────────────
# Level badge helper (HTML)
# ─────────────────────────────────────────────────────────────────────────────

_LEVEL_CSS = {
    "critical": "bg-red-100 text-red-700 border border-red-200",
    "warning":  "bg-yellow-100 text-yellow-700 border border-yellow-200",
    "info":     "bg-blue-50 text-blue-700 border border-blue-200",
}
_LEVEL_ICON = {
    "critical": "🚨",
    "warning":  "⚠️",
    "info":     "ℹ️",
}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def agents_page(request: Request):
    """
    Renderiza la página principal del dashboard de agentes.
    Pasa al template: user, status (quick_status), y un session_id nuevo.
    """
    user = _get_user(request)

    # Estado rápido para el render inicial
    try:
        status = await orchestrator.quick_status()
    except Exception as exc:
        logger.warning("quick_status error: %s", exc)
        status = {
            "alerts_count": 0,
            "last_scan": None,
            "agents_status": {},
            "recommendations": [],
        }

    # Genera un session_id para esta sesión de chat si no existe en la request
    # El frontend puede sobreescribir esto con un valor almacenado en localStorage
    session_id = str(uuid.uuid4())

    return _templates.TemplateResponse("agents.html", {
        "request": request,
        "user": user,
        "status": status,
        "session_id": session_id,
        "active_tab": "agents",
    })


@router.post("/chat")
async def chat(request: Request):
    """
    Recibe JSON: {message: str, session_id: str}
    Retorna StreamingResponse con Server-Sent Events.

    Formato SSE:
      data: {"type": "chunk",  "content": "texto parcial"}
      data: {"type": "done"}
      data: {"type": "error",  "content": "mensaje de error"}
    """
    user = _get_user(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    message: str = (body.get("message") or "").strip()
    session_id: str = (body.get("session_id") or "").strip()

    if not message:
        raise HTTPException(status_code=400, detail="El campo 'message' es requerido")
    if not session_id:
        session_id = str(uuid.uuid4())

    user_id = user.get("username", "default")

    async def event_generator():
        try:
            gen = await orchestrator.handle(
                message=message,
                session_id=session_id,
                user_id=user_id,
                stream=True,
            )
            async for chunk in gen:  # type: ignore[union-attr]
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as exc:
            logger.error("Chat SSE error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering for SSE
        },
    )


@router.get("/status")
async def get_status(request: Request):
    """
    Retorna JSON con estado de todos los agentes y alertas pendientes.

    Response:
    {
        "alerts_count": int,
        "last_scan": str | null,
        "agents_status": {name: "ready"|"error"|"unavailable"},
        "recommendations": list[str],
        "recent_alerts": list[{id, level, title, message, created_at}]
    }
    """
    _get_user(request)

    try:
        status = await orchestrator.quick_status()
    except Exception as exc:
        logger.error("Status error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )

    # Añadir alertas recientes (últimas 5)
    try:
        recent_alerts = await memory_manager.get_alerts(unread_only=False, limit=5)
        status["recent_alerts"] = recent_alerts
    except Exception:
        status["recent_alerts"] = []

    return JSONResponse(content=status)


@router.post("/run/{agent_name}")
async def run_agent(agent_name: str, request: Request):
    """
    Ejecuta un agente específico con su tarea de análisis estándar.

    Args (JSON body, todos opcionales):
        task: str — tarea personalizada (default: análisis general del agente)

    Returns:
        {success, message, data, actions, agent_name}
    """
    _get_user(request)

    # Tarea opcional del body
    task = ""
    try:
        body = await request.json()
        task = (body.get("task") or "").strip()
    except Exception:
        pass  # Body vacío o no-JSON es válido

    if not task:
        task = f"Realiza tu análisis estándar y reporta el estado actual."

    # Validar que el agente existe
    try:
        registry = build_agent_registry()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error construyendo registry: {exc}")

    agent = registry.get(agent_name)
    if not agent:
        available = list(registry.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Agente '{agent_name}' no encontrado. Disponibles: {available}",
        )

    try:
        result = await agent.run(task)
    except Exception as exc:
        logger.error("Agent '%s' raised: %s", agent_name, exc)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": str(exc),
                "data": {},
                "actions": [],
                "agent_name": agent_name,
            },
        )

    return JSONResponse(content={
        "success": result.success,
        "message": result.message,
        "data": result.data,
        "actions": result.actions,
        "agent_name": result.agent_name,
    })


@router.get("/history/{session_id}", response_class=HTMLResponse)
async def get_history(session_id: str, request: Request):
    """
    Retorna el historial de conversación de una sesión como HTML partial.
    Cada mensaje se renderiza como una burbuja de chat.
    """
    _get_user(request)

    try:
        messages = await memory_manager.get_conversation(session_id, limit=50)
    except Exception as exc:
        logger.error("History error: %s", exc)
        messages = []

    if not messages:
        return HTMLResponse(
            '<div class="text-center text-gray-400 py-8 text-sm">'
            'No hay mensajes en esta sesión.</div>'
        )

    html_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        ts = msg.get("ts", "")

        if role == "user":
            bubble_cls = "ml-8 bg-blue-600 text-white rounded-2xl rounded-tr-sm"
            label = "Tú"
            label_cls = "text-right text-xs text-gray-400 mb-1"
            wrapper_cls = "flex flex-col items-end"
        else:
            bubble_cls = "mr-8 bg-white border border-gray-200 text-gray-800 rounded-2xl rounded-tl-sm"
            label = "Asistente"
            label_cls = "text-left text-xs text-gray-400 mb-1"
            wrapper_cls = "flex flex-col items-start"

        # Escape básico del contenido para HTML
        safe_content = (
            content
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )

        html_parts.append(f"""
        <div class="{wrapper_cls} mb-4">
            <div class="{label_cls}">{label} · {ts[:16] if ts else ''}</div>
            <div class="px-4 py-3 max-w-[85%] shadow-sm {bubble_cls} text-sm leading-relaxed">
                {safe_content}
            </div>
        </div>
        """)

    return HTMLResponse("".join(html_parts))


@router.get("/alerts", response_class=HTMLResponse)
async def get_alerts(request: Request):
    """
    Retorna las alertas recientes (últimas 20) como HTML partial.
    """
    _get_user(request)

    try:
        alerts = await memory_manager.get_alerts(unread_only=False, limit=20)
    except Exception as exc:
        logger.error("Alerts error: %s", exc)
        return HTMLResponse(
            '<div class="text-center text-gray-400 py-4 text-sm">Error cargando alertas.</div>'
        )

    if not alerts:
        return HTMLResponse(
            '<div class="text-center text-gray-400 py-8 text-sm">'
            'No hay alertas registradas.</div>'
        )

    html_parts: list[str] = []
    for alert in alerts:
        level = alert.get("level", "info")
        css = _LEVEL_CSS.get(level, _LEVEL_CSS["info"])
        icon = _LEVEL_ICON.get(level, "•")
        read_cls = "opacity-50" if alert.get("read") else ""
        alert_id = alert.get("id", 0)
        title = alert.get("title", "")
        message = alert.get("message", "")
        created_at = str(alert.get("created_at", ""))[:16]
        agent_name = alert.get("agent_name", "")

        html_parts.append(f"""
        <div id="alert-{alert_id}"
             class="flex items-start gap-3 p-3 rounded-lg {css} {read_cls} transition-opacity">
            <span class="text-lg flex-shrink-0">{icon}</span>
            <div class="flex-1 min-w-0">
                <div class="flex items-center justify-between gap-2 flex-wrap">
                    <span class="font-semibold text-sm">{title}</span>
                    <div class="flex items-center gap-2">
                        <span class="text-xs opacity-70">{agent_name} · {created_at}</span>
                        {'<button onclick="markAlertRead(' + str(alert_id) + ')" '
                         'class="text-xs underline opacity-70 hover:opacity-100 whitespace-nowrap">'
                         'Marcar leída</button>'
                         if not alert.get("read") else ''}
                    </div>
                </div>
                <p class="text-xs mt-1 opacity-80 line-clamp-2">{message}</p>
            </div>
        </div>
        """)

    return HTMLResponse("\n".join(html_parts))


@router.post("/alerts/{alert_id}/read")
async def mark_alert_read(alert_id: int, request: Request):
    """Marca una alerta específica como leída."""
    _get_user(request)
    try:
        await memory_manager.mark_alert_read(alert_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content={"ok": True, "alert_id": alert_id})


@router.post("/alerts/read-all")
async def mark_all_read(request: Request):
    """Marca todas las alertas como leídas."""
    _get_user(request)
    try:
        await memory_manager.mark_all_alerts_read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content={"ok": True})


@router.post("/scan")
async def run_full_scan(request: Request, background_tasks: BackgroundTasks):
    """
    Dispara un scan completo de todos los agentes en background.
    Retorna HTTP 202 Accepted inmediatamente sin esperar el resultado.
    El resultado se persiste en MemoryManager (clave: orchestrator/last_scan_summary).
    """
    user = _get_user(request)

    async def _do_scan():
        try:
            result = await orchestrator.run_scheduled_scan()
            logger.info(
                "Full scan triggered by %s completed: %d ok / %d errors",
                user.get("username", "?"),
                result.get("success_count", 0),
                result.get("error_count", 0),
            )
        except Exception as exc:
            logger.error("Full scan error: %s", exc)

    background_tasks.add_task(_do_scan)

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "message": "Scan iniciado en background. Los resultados estarán disponibles en /agents/status.",
        },
    )


@router.get("/scheduler/jobs")
async def get_scheduler_jobs(request: Request):
    """
    Lista todos los jobs registrados en el scheduler con su estado actual.

    Returns:
        list[{job_id, agent_name, task, schedule, enabled, last_run,
               last_result, next_run_time, paused}]
    """
    _get_user(request)
    try:
        jobs = await scheduler_service.get_jobs()
    except Exception as exc:
        logger.error("Scheduler jobs error: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})
    return JSONResponse(content={"jobs": jobs, "scheduler_running": scheduler_service.is_running})


@router.post("/scheduler/jobs/{job_id}/toggle")
async def toggle_job(job_id: str, request: Request):
    """
    Pausa o reanuda un job del scheduler según su estado actual.
    Si está habilitado → lo pausa; si está pausado → lo reanuda.

    Returns:
        {ok, job_id, action: "paused"|"resumed"}
    """
    _get_user(request)

    try:
        jobs = await scheduler_service.get_jobs()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    job = next((j for j in jobs if j["job_id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' no encontrado")

    is_enabled = bool(job.get("enabled", False))

    try:
        if is_enabled:
            success = await scheduler_service.pause_job(job_id)
            action = "paused"
        else:
            success = await scheduler_service.resume_job(job_id)
            action = "resumed"
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not success:
        raise HTTPException(
            status_code=503,
            detail="APScheduler no está disponible. Instala: pip install apscheduler",
        )

    return JSONResponse(content={"ok": True, "job_id": job_id, "action": action})
