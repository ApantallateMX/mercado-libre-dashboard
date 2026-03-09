"""
app/services/agents/health_agent.py

Health & Reputation Agent — gestiona reputación, claims, preguntas y mensajes
de compradores en MeLi.
"""

from __future__ import annotations

import json
import time

import httpx

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

BASE_URL = "http://localhost:8000"


class HealthAgent(BaseAgent):
    name = "health_reputation"
    emoji = "🏥"
    description = "Gestiona reputación, claims, preguntas y mensajes de compradores"

    def __init__(self):
        super().__init__(memory_manager=memory_manager)

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        return [
            {
                "name": "get_health_summary",
                "description": (
                    "Obtiene el resumen de salud de la cuenta MeLi: nivel de reputación, "
                    "tasa de reclamos, métricas de entrega, calificaciones de compradores."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_pending_claims",
                "description": (
                    "Obtiene todos los reclamos pendientes de resolución: motivo, estado, "
                    "días abierto, producto involucrado, impacto en reputación."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_pending_questions",
                "description": (
                    "Obtiene preguntas sin responder de compradores en MeLi: "
                    "texto de la pregunta, producto, tiempo sin respuesta."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_messages",
                "description": (
                    "Obtiene mensajes pendientes de compradores post-venta: "
                    "conversaciones abiertas, último mensaje del comprador, tiempo sin respuesta."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "answer_question",
                "description": (
                    "Responde una pregunta de un comprador en MeLi. "
                    "Requiere el ID de la pregunta y el texto de la respuesta."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question_id": {
                            "type": "string",
                            "description": "ID de la pregunta a responder",
                        },
                        "text": {
                            "type": "string",
                            "description": "Texto de la respuesta al comprador",
                        },
                    },
                    "required": ["question_id", "text"],
                },
            },
            {
                "name": "respond_claim",
                "description": (
                    "Envía un mensaje de respuesta a un reclamo activo en MeLi. "
                    "Requiere el ID del reclamo y el mensaje para el comprador."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "claim_id": {
                            "type": "string",
                            "description": "ID del reclamo en MeLi",
                        },
                        "message": {
                            "type": "string",
                            "description": "Mensaje de respuesta para el comprador",
                        },
                    },
                    "required": ["claim_id", "message"],
                },
            },
        ]

    def _get_system_prompt(self) -> str:
        return (
            "Eres un gestor experto de reputación para MeLi Mexico. "
            "Tu misión es proteger la calificación del vendedor y resolver situaciones críticas. "
            "Prioridades: "
            "1) CRÍTICO: reclamos abiertos >2 días (afectan reputación directamente). "
            "2) URGENTE: preguntas sin respuesta >24h (afectan conversión y posicionamiento). "
            "3) IMPORTANTE: mensajes post-venta sin respuesta >12h (afectan calificación). "
            "Para preguntas, enfócate en las que tienen mayor probabilidad de convertir en venta. "
            "Para reclamos, recomienda siempre la solución que proteja la reputación primero. "
            "Responde en español de forma clara y con urgencia apropiada a cada situación."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "get_health_summary":
                return await self._fetch_partial(f"{BASE_URL}/partials/health-summary")

            elif tool_name == "get_pending_claims":
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/health/claims")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al obtener reclamos"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "get_pending_questions":
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/health/questions")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al obtener preguntas"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "get_messages":
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/health/messages")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al obtener mensajes"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "answer_question":
                question_id = tool_input.get("question_id", "")
                text = tool_input.get("text", "")
                if not question_id or not text:
                    return "Error: question_id y text son requeridos"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{BASE_URL}/api/health/questions/{question_id}/answer",
                        json={"text": text},
                    )
                if resp.status_code in (200, 201):
                    return f"Pregunta {question_id} respondida exitosamente"
                return f"Error HTTP {resp.status_code} respondiendo pregunta {question_id}: {resp.text[:500]}"

            elif tool_name == "respond_claim":
                claim_id = tool_input.get("claim_id", "")
                message = tool_input.get("message", "")
                if not claim_id or not message:
                    return "Error: claim_id y message son requeridos"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{BASE_URL}/api/health/claims/{claim_id}/respond",
                        json={"message": message},
                    )
                if resp.status_code in (200, 201):
                    return f"Reclamo {claim_id} respondido exitosamente"
                return f"Error HTTP {resp.status_code} respondiendo reclamo {claim_id}: {resp.text[:500]}"

            else:
                return f"Herramienta desconocida: {tool_name}"

        except httpx.TimeoutException:
            return f"Timeout al ejecutar {tool_name}"
        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"

    async def _fetch_partial(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return f"Error HTTP {resp.status_code} al obtener {url}"
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            try:
                return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
            except Exception:
                pass
        return self._html_to_text(resp.text)

    @staticmethod
    def _html_to_text(html: str, max_chars: int = 6000) -> str:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "head"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            lines = [l for l in text.splitlines() if l.strip()]
            return "\n".join(lines)[:max_chars]
        except Exception as e:
            return f"[HTML parse error: {e}]"

    # ── Specialized method ───────────────────────────────────────────────────

    async def daily_health_check(self) -> AgentResult:
        """
        Revisión diaria completa de salud de cuenta: reputación, claims,
        preguntas sin responder y mensajes pendientes.
        """
        t0 = time.monotonic()
        task = (
            "Realiza la revisión diaria completa de salud de la cuenta MeLi. "
            "1. Obtén el resumen de salud (get_health_summary). "
            "2. Revisa reclamos pendientes (get_pending_claims). "
            "3. Revisa preguntas sin responder (get_pending_questions). "
            "4. Revisa mensajes pendientes (get_messages). "
            "Genera un reporte con: "
            "- SEMÁFORO: VERDE (todo bien) / AMARILLO (hay pendientes) / ROJO (crítico). "
            "- Reclamos activos: cuántos, cuántos días llevan, impacto en reputación. "
            "- Preguntas sin responder: cuántas, cuánto tiempo llevan, las más urgentes. "
            "- Mensajes pendientes: conversaciones abiertas sin respuesta. "
            "- ACCIONES INMEDIATAS: lista ordenada por urgencia. "
            "Responde en español con formato ejecutivo."
        )
        try:
            result = await self.run(task)
            elapsed = time.monotonic() - t0

            await memory_manager.remember(self.name, "last_health_check", {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary": result.message[:300],
            })

            # Create alert if reputation at risk
            msg_lower = result.message.lower()
            level = "info"
            if "rojo" in msg_lower or "crítico" in msg_lower:
                level = "critical"
            elif "amarillo" in msg_lower or "urgente" in msg_lower:
                level = "warning"

            if level != "info":
                await memory_manager.create_alert(
                    agent_name=self.name,
                    level=level,
                    title="Alerta de salud de cuenta MeLi",
                    message=result.message[:500],
                    data={},
                )

            return AgentResult(
                success=result.success,
                agent_name=self.name,
                agent_emoji=self.emoji,
                summary=result.message[:200],
                details=result.message,
                data=result.data,
                actions=result.actions,
                elapsed_seconds=elapsed,
                error=result.message if not result.success else "",
            )
        except Exception as e:
            return AgentResult(
                success=False,
                agent_name=self.name,
                agent_emoji=self.emoji,
                summary=f"Error en daily_health_check: {e}",
                error=str(e),
                elapsed_seconds=time.monotonic() - t0,
            )
