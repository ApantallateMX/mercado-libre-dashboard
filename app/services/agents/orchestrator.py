"""
app/services/agents/orchestrator.py

Orquestador central del sistema multi-agente.

Usa Claude (via httpx directo a la Anthropic Messages API) para:
  1. Analizar el mensaje del usuario y decidir qué agente(s) invocar.
  2. Ejecutar los agentes seleccionados (en paralelo con asyncio.gather).
  3. Consolidar los resultados en una respuesta cohesiva.
  4. Persistir la conversación en MemoryManager.

El Orquestador actúa como CEO de operaciones de un e-commerce en México
y coordina a su equipo de agentes especializados.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import AsyncGenerator

import httpx

from app.config import ANTHROPIC_API_KEY
from app.services.agents.base import ANTHROPIC_API_URL, ANTHROPIC_MODEL, ANTHROPIC_VERSION, AgentResult
from app.services.agents import build_agent_registry
from app.services.memory_manager import memory_manager

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Routing keywords → agent keys
# ─────────────────────────────────────────────────────────────────────────────

_ROUTING_RULES: list[tuple[list[str], str]] = [
    (["ventas", "revenue", "meta", "ingreso", "venta", "facturación", "facturacion"], "sales"),
    (["stock", "inventario", "agotado", "sin stock", "disponib", "bodega", "almacén", "almacen"], "inventory"),
    (["precio", "margen", "competencia", "competidor", "costo", "ganancia", "tarifa"], "pricing"),
    (["reclamo", "reclamación", "reclamacion", "pregunta", "reputación", "reputacion", "health", "mensaje", "calificación", "calificacion"], "health"),
    (["anuncio", "campaña", "campana", "publicidad", "ads", "cpc", "impresión", "impresion", "click"], "ads"),
    (["publicación", "publicacion", "título", "titulo", "listing", "descripción", "descripcion", "foto"], "listing"),
    (["datos", "discrepancia", "verificar", "qa", "calidad", "inconsistencia", "error de dato"], "qa"),
    (["alerta", "urgente", "hoy", "crítico", "critico", "importante", "prioridad"], "alert"),
]

_ALL_AGENTS = ["sales", "inventory", "pricing", "health", "ads", "listing", "qa", "alert"]

# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────

_ORCHESTRATOR_SYSTEM = """\
Eres el Director de Operaciones (COO) de un e-commerce en México que vende en Mercado Libre y Amazon.
Coordinas un equipo de agentes especializados y tienes acceso a sus reportes en tiempo real.

Tu equipo:
- SalesAgent     → ventas, revenue, metas, tendencias
- InventoryAgent → stock, reabastecimiento, productos agotados
- PricingAgent   → precios, márgenes, competencia
- HealthAgent    → reclamos, preguntas, reputación del vendedor
- AdsAgent       → campañas publicitarias, CPC, ROAS
- ListingAgent   → calidad de publicaciones, títulos, fotos
- QAAgent        → verificación de datos, inconsistencias
- AlertAgent     → alertas críticas y prioridades del día

Cuando presentas resultados:
- Sé directo y ejecutivo, como en un briefing matutino.
- Agrupa insights por importancia (crítico → importante → informativo).
- Incluye números concretos cuando los tienes.
- Termina siempre con 2-3 acciones recomendadas priorizadas.
- Usa formato markdown con encabezados para organizar la respuesta.
- Idioma: español mexicano, tono profesional pero directo.
- Si un agente falló, menciona la limitación y sigue con el resto.
"""

_CONSOLIDATION_SYSTEM = """\
Eres el Director de Operaciones (COO) de un e-commerce en México.
Recibirás los reportes de tus agentes especializados y debes consolidarlos
en una respuesta ejecutiva clara y accionable.

Formato de tu respuesta:
1. Resumen ejecutivo (2-3 líneas)
2. Hallazgos por área (solo las áreas consultadas)
3. Acciones recomendadas (máximo 3, ordenadas por prioridad)

Sé conciso, usa datos concretos, evita relleno.
Idioma: español mexicano profesional.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Orquestador central del sistema multi-agente.

    Flujo por mensaje:
    1. Persiste el mensaje del usuario en conversación.
    2. Detecta qué agentes invocar según palabras clave (o todos si es general).
    3. Ejecuta los agentes en paralelo con asyncio.gather.
    4. Consolida los resultados con Claude en una respuesta ejecutiva.
    5. Persiste la respuesta en conversación.
    6. Retorna texto (o AsyncGenerator de chunks si stream=True).
    """

    def __init__(self):
        self._registry: dict | None = None
        self._logger = logging.getLogger("orchestrator")

    def _get_registry(self) -> dict:
        """Lazy-builds the agent registry (singleton per Orchestrator instance)."""
        if self._registry is None:
            self._registry = build_agent_registry()
        return self._registry

    # ── Routing ──────────────────────────────────────────────────────────────

    def _detect_agents(self, message: str) -> list[str]:
        """
        Retorna lista de agent keys a invocar según el mensaje.
        Si no hay match de palabras clave, retorna todos los agentes (scan general).
        """
        lower = message.lower()

        # Frases explícitas de "todo" o "qué debo hacer"
        general_triggers = [
            "qué debo hacer", "que debo hacer", "resumen general", "estado general",
            "dame un resumen", "cómo va el negocio", "como va el negocio",
            "todo", "overview", "briefing",
        ]
        if any(trigger in lower for trigger in general_triggers):
            return _ALL_AGENTS

        matched: list[str] = []
        for keywords, agent_key in _ROUTING_RULES:
            if any(kw in lower for kw in keywords):
                if agent_key not in matched:
                    matched.append(agent_key)

        return matched if matched else _ALL_AGENTS

    # ── Claude helpers ────────────────────────────────────────────────────────

    async def _call_claude(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 2048,
        stream: bool = False,
    ) -> dict | httpx.Response:
        """
        Llama a la Anthropic Messages API.
        Si stream=True retorna la respuesta httpx cruda (para iterar chunks SSE).
        Si stream=False retorna el JSON parseado.
        """
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY no está configurada")

        payload: dict = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if stream:
            payload["stream"] = True

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        if stream:
            # Retorna la respuesta sin cerrar para que el caller itere SSE
            client = httpx.AsyncClient(timeout=120.0)
            resp = await client.post(
                ANTHROPIC_API_URL, json=payload, headers=headers
            )
            if resp.status_code != 200:
                await client.aclose()
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", resp.text)
                except Exception:
                    msg = resp.text
                raise RuntimeError(f"Anthropic API error {resp.status_code}: {msg}")
            # Caller is responsible for closing client after consuming the stream
            resp._client_ref = client  # type: ignore[attr-defined]
            return resp

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(ANTHROPIC_API_URL, json=payload, headers=headers)

        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message", resp.text)
            except Exception:
                msg = resp.text
            raise RuntimeError(f"Anthropic API error {resp.status_code}: {msg}")

        return resp.json()

    def _extract_text(self, response: dict) -> str:
        """Extrae el texto de una respuesta Claude (stop_reason=end_turn)."""
        return "\n".join(
            block.get("text", "")
            for block in response.get("content", [])
            if block.get("type") == "text"
        ).strip()

    # ── Context helpers ───────────────────────────────────────────────────────

    async def _get_context(self, session_id: str) -> dict:
        """
        Retorna contexto para el orquestador:
        - alertas pendientes (unread)
        - últimos mensajes de la conversación
        - último scan almacenado en memoria
        """
        try:
            alerts = await memory_manager.get_alerts(unread_only=True, limit=10)
        except Exception:
            alerts = []

        try:
            history = await memory_manager.get_conversation(session_id, limit=10)
        except Exception:
            history = []

        try:
            last_scan = await memory_manager.recall("orchestrator", "last_scan_summary")
        except Exception:
            last_scan = None

        return {
            "unread_alerts": len(alerts),
            "top_alerts": [
                {"level": a["level"], "title": a["title"], "message": a["message"]}
                for a in alerts[:5]
            ],
            "recent_history_count": len(history),
            "last_scan": last_scan,
        }

    # ── Agent execution ───────────────────────────────────────────────────────

    async def _run_agent(self, agent_key: str, task: str) -> AgentResult:
        """Ejecuta un agente por su key y captura errores."""
        registry = self._get_registry()
        agent = registry.get(agent_key)
        if not agent:
            return AgentResult(
                success=False,
                message=f"Agente '{agent_key}' no encontrado en el registry.",
                agent_name=agent_key,
            )
        try:
            result = await agent.run(task)
            return result
        except Exception as exc:
            self._logger.error("Agent '%s' raised an exception: %s", agent_key, exc)
            return AgentResult(
                success=False,
                message=f"Error en agente {agent_key}: {exc}",
                agent_name=agent_key,
            )

    async def _run_agents_parallel(
        self, agent_keys: list[str], task: str
    ) -> list[AgentResult]:
        """Ejecuta múltiples agentes en paralelo con asyncio.gather."""
        coroutines = [self._run_agent(key, task) for key in agent_keys]
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        # Normaliza excepciones no capturadas en AgentResult
        normalized: list[AgentResult] = []
        for key, res in zip(agent_keys, results):
            if isinstance(res, Exception):
                normalized.append(AgentResult(
                    success=False,
                    message=f"Error inesperado en {key}: {res}",
                    agent_name=key,
                ))
            else:
                normalized.append(res)  # type: ignore[arg-type]
        return normalized

    # ── Consolidation ─────────────────────────────────────────────────────────

    async def _consolidate(
        self,
        user_message: str,
        agent_results: list[AgentResult],
        context: dict,
        stream: bool = False,
    ) -> str | AsyncGenerator[str, None]:
        """
        Usa Claude para consolidar los resultados de los agentes en una
        respuesta ejecutiva. Retorna str o AsyncGenerator según stream.
        """
        # Construir el contenido de resultados de agentes
        results_text = ""
        for res in agent_results:
            status = "OK" if res.success else "ERROR"
            results_text += f"\n### {res.agent_name.upper()} [{status}]\n{res.message}\n"

        # Contexto adicional (alertas, último scan)
        context_text = ""
        if context.get("unread_alerts", 0) > 0:
            context_text += f"\nAlertas sin leer: {context['unread_alerts']}\n"
            for alert in context.get("top_alerts", []):
                context_text += f"  - [{alert['level'].upper()}] {alert['title']}: {alert['message']}\n"

        user_content = (
            f"Pregunta del usuario: {user_message}\n\n"
            f"Reportes de agentes:{results_text}"
        )
        if context_text:
            user_content += f"\nContexto adicional:{context_text}"

        messages = [{"role": "user", "content": user_content}]

        if stream:
            return self._stream_consolidation(messages)

        try:
            response = await self._call_claude(
                system=_CONSOLIDATION_SYSTEM,
                messages=messages,
                max_tokens=2048,
                stream=False,
            )
            return self._extract_text(response)  # type: ignore[arg-type]
        except Exception as exc:
            self._logger.error("Consolidation error: %s", exc)
            # Fallback: devolver los resultados crudos
            fallback = f"Error al consolidar respuesta: {exc}\n\nResultados individuales:\n"
            for res in agent_results:
                fallback += f"\n**{res.agent_name}**: {res.message}\n"
            return fallback

    async def _stream_consolidation(
        self, messages: list[dict]
    ) -> AsyncGenerator[str, None]:
        """Genera chunks de texto SSE desde la API de Claude en modo streaming."""
        try:
            resp = await self._call_claude(
                system=_CONSOLIDATION_SYSTEM,
                messages=messages,
                max_tokens=2048,
                stream=True,
            )

            # resp es un httpx.Response en streaming
            async for line in resp.aiter_lines():  # type: ignore[union-attr]
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Anthropic stream events: content_block_delta con type=text_delta
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield text

        except Exception as exc:
            self._logger.error("Streaming error: %s", exc)
            yield f"\n\nError en streaming: {exc}"
        finally:
            try:
                client_ref = getattr(resp, "_client_ref", None)
                if client_ref:
                    await client_ref.aclose()
            except Exception:
                pass

    # ── Public interface ──────────────────────────────────────────────────────

    async def handle(
        self,
        message: str,
        session_id: str,
        user_id: str = "default",
        stream: bool = False,
    ) -> str | AsyncGenerator[str, None]:
        """
        Procesa un mensaje del usuario y retorna una respuesta.

        Flujo:
        1. Guarda mensaje en conversación.
        2. Obtiene contexto (alertas + historial + último scan).
        3. Detecta qué agentes invocar (routing por palabras clave).
        4. Ejecuta agentes seleccionados en paralelo.
        5. Consolida respuesta con Claude.
        6. Guarda respuesta en conversación.
        7. Retorna str (o AsyncGenerator si stream=True).

        Args:
            message:    Mensaje en lenguaje natural del usuario.
            session_id: ID de la sesión de conversación (para historial).
            user_id:    Identificador del usuario (para contexto/permisos).
            stream:     Si True, retorna AsyncGenerator de chunks de texto.

        Returns:
            str con la respuesta consolidada, o AsyncGenerator si stream=True.
        """
        # 1. Guardar mensaje del usuario
        try:
            await memory_manager.save_conversation(
                session_id=session_id,
                agent_name="orchestrator",
                role="user",
                content=message,
            )
        except Exception as exc:
            self._logger.warning("Could not save user message: %s", exc)

        # 2. Obtener contexto
        context = await self._get_context(session_id)

        # 3. Detectar agentes a invocar
        agent_keys = self._detect_agents(message)
        self._logger.info(
            "session=%s | user=%s | agents=%s | message=%.80s",
            session_id, user_id, agent_keys, message,
        )

        # 4. Ejecutar agentes en paralelo
        agent_results = await self._run_agents_parallel(agent_keys, message)

        # 5 & 6. Consolidar y (si no es stream) guardar respuesta
        if stream:
            # En modo stream: wrapeamos el generador para guardar la respuesta al final
            async def _stream_and_save() -> AsyncGenerator[str, None]:
                chunks: list[str] = []
                gen = await self._consolidate(message, agent_results, context, stream=True)
                async for chunk in gen:  # type: ignore[union-attr]
                    chunks.append(chunk)
                    yield chunk

                full_response = "".join(chunks)
                try:
                    await memory_manager.save_conversation(
                        session_id=session_id,
                        agent_name="orchestrator",
                        role="assistant",
                        content=full_response,
                    )
                except Exception as exc:
                    self._logger.warning("Could not save assistant response: %s", exc)

            return _stream_and_save()

        # Modo no-stream
        final_response = await self._consolidate(
            message, agent_results, context, stream=False
        )

        # 6. Guardar respuesta
        try:
            await memory_manager.save_conversation(
                session_id=session_id,
                agent_name="orchestrator",
                role="assistant",
                content=str(final_response),
            )
        except Exception as exc:
            self._logger.warning("Could not save assistant response: %s", exc)

        return final_response  # type: ignore[return-value]

    async def quick_status(self) -> dict:
        """
        Retorna estado rápido del negocio sin invocar agentes pesados.
        Usado para el dashboard inicial de agentes.

        Returns:
            {
                "alerts_count": int,
                "last_scan": str | None,
                "agents_status": {agent_key: "ready" | "error"},
                "recommendations": list[str],
            }
        """
        # Alertas sin leer
        try:
            alerts = await memory_manager.get_alerts(unread_only=True, limit=1)
            total_unread = len(await memory_manager.get_alerts(unread_only=True, limit=100))
        except Exception:
            alerts = []
            total_unread = 0

        # Último scan
        try:
            last_scan_data = await memory_manager.recall("orchestrator", "last_scan_summary")
            last_scan = last_scan_data.get("ts") if last_scan_data else None
        except Exception:
            last_scan = None

        # Estado de agentes (verifica que el registry se puede construir)
        agents_status: dict[str, str] = {}
        try:
            registry = self._get_registry()
            for key in _ALL_AGENTS:
                agents_status[key] = "ready" if key in registry else "unavailable"
        except Exception as exc:
            self._logger.error("Error building registry: %s", exc)
            agents_status = {k: "error" for k in _ALL_AGENTS}

        # Recomendaciones rápidas basadas en alertas
        recommendations: list[str] = []
        try:
            all_alerts = await memory_manager.get_alerts(unread_only=True, limit=5)
            for alert in all_alerts:
                if alert["level"] == "critical":
                    recommendations.append(f"[CRÍTICO] {alert['title']}")
                elif alert["level"] == "warning" and len(recommendations) < 3:
                    recommendations.append(f"[Atención] {alert['title']}")
        except Exception:
            pass

        if not recommendations:
            recommendations = ["Ejecuta un scan completo para obtener recomendaciones actualizadas."]

        return {
            "alerts_count": total_unread,
            "last_scan": last_scan,
            "agents_status": agents_status,
            "recommendations": recommendations[:3],
        }

    async def run_scheduled_scan(self) -> dict:
        """
        Ejecuta un scan completo de todos los agentes.
        Llamado por el scheduler de forma periódica.

        Returns:
            Resumen de hallazgos: {ts, agents_run, success_count, error_count, summary}
        """
        task = (
            "Ejecuta un análisis completo del estado del negocio. "
            "Revisa ventas del día, stock crítico, preguntas sin responder, "
            "reclamos abiertos, alertas activas y cualquier anomalía."
        )

        self._logger.info("Starting scheduled full scan at %s", datetime.utcnow().isoformat())

        results = await self._run_agents_parallel(_ALL_AGENTS, task)

        success_count = sum(1 for r in results if r.success)
        error_count = len(results) - success_count

        # Generar resumen breve con Claude
        results_text = "\n".join(
            f"- {r.agent_name}: {'OK' if r.success else 'ERROR'} — {r.message[:200]}"
            for r in results
        )
        try:
            consolidate_msg = [
                {"role": "user", "content": (
                    "Genera un resumen ejecutivo muy breve (máximo 5 puntos) "
                    "de los siguientes reportes de scan completo:\n\n" + results_text
                )}
            ]
            resp = await self._call_claude(
                system=_ORCHESTRATOR_SYSTEM,
                messages=consolidate_msg,
                max_tokens=512,
            )
            summary = self._extract_text(resp)  # type: ignore[arg-type]
        except Exception as exc:
            summary = f"Error generando resumen: {exc}"

        scan_result = {
            "ts": datetime.utcnow().isoformat(),
            "agents_run": [r.agent_name for r in results],
            "success_count": success_count,
            "error_count": error_count,
            "summary": summary,
        }

        # Persistir en memoria para quick_status
        try:
            await memory_manager.remember("orchestrator", "last_scan_summary", scan_result)
        except Exception as exc:
            self._logger.warning("Could not persist scan summary: %s", exc)

        self._logger.info(
            "Scheduled scan complete: %d ok, %d errors", success_count, error_count
        )
        return scan_result


# ── Module-level singleton ────────────────────────────────────────────────────
orchestrator = Orchestrator()
