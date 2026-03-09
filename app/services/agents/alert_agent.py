"""
app/services/agents/alert_agent.py

Proactive Alert Agent — monitor 24/7 que genera alertas críticas del negocio
consultando a todos los agentes especializados.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

if TYPE_CHECKING:
    pass


class AlertAgent(BaseAgent):
    name = "alert_monitor"
    emoji = "🚨"
    description = "Monitor proactivo que genera alertas críticas del negocio"

    def __init__(self, agents: dict | None = None):
        """
        Args:
            agents: Dict de agentes especializados ya instanciados.
                    Esperado: {
                        "sales": SalesAgent,
                        "inventory": InventoryAgent,
                        "health": HealthAgent,
                        "pricing": PricingAgent,
                        "ads": AdsAgent,
                        "listing": ListingAgent,
                        "qa": QAAgent,
                    }
        """
        super().__init__(memory_manager=memory_manager)
        self.agents = agents or {}

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        # AlertAgent no usa Claude directamente para el run_full_scan —
        # orquesta los demás agentes. Pero puede usarse en modo run() normal.
        return [
            {
                "name": "get_active_alerts",
                "description": (
                    "Recupera todas las alertas no leídas almacenadas en memoria, "
                    "ordenadas por criticidad y fecha."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de alertas a retornar (default 50)",
                            "default": 50,
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "create_alert",
                "description": (
                    "Crea y persiste una nueva alerta en el sistema de memoria."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "level": {
                            "type": "string",
                            "enum": ["info", "warning", "critical"],
                            "description": "Nivel de severidad de la alerta",
                        },
                        "title": {
                            "type": "string",
                            "description": "Título corto de la alerta (max 100 chars)",
                        },
                        "message": {
                            "type": "string",
                            "description": "Descripción detallada de la alerta",
                        },
                    },
                    "required": ["level", "title", "message"],
                },
            },
        ]

    def _get_system_prompt(self) -> str:
        return (
            "Eres el monitor central 24/7 de un negocio de marketplace en MeLi y Amazon Mexico. "
            "Tu función es detectar situaciones que requieren atención INMEDIATA del dueño. "
            "Criterios para alerta CRÍTICA: stock=0 en productos top ventas, "
            "reclamo >48h sin respuesta, caída de ventas >30% vs día anterior, "
            "pérdida de buybox en producto principal, discrepancia de stock >20 unidades. "
            "Criterios para alerta WARNING: stock <5 en productos con demanda, "
            "pregunta sin respuesta >24h, ROAS <2x en campaña activa, "
            "precio por debajo del costo en cualquier producto. "
            "Sé conciso y directo. Cada alerta debe tener: qué pasó, qué perdes si no actúas, "
            "y exactamente qué hacer. Responde siempre en español."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "get_active_alerts":
                limit = tool_input.get("limit", 50)
                alerts = await memory_manager.get_alerts(unread_only=True, limit=limit)
                if not alerts:
                    return "No hay alertas pendientes."
                return json.dumps(alerts, ensure_ascii=False, indent=2, default=str)

            elif tool_name == "create_alert":
                level = tool_input.get("level", "info")
                title = tool_input.get("title", "")
                message = tool_input.get("message", "")
                if not title or not message:
                    return "Error: title y message son requeridos"
                alert_id = await memory_manager.create_alert(
                    agent_name=self.name,
                    level=level,
                    title=title,
                    message=message,
                    data={},
                )
                return f"Alerta creada con ID {alert_id}: [{level.upper()}] {title}"

            else:
                return f"Herramienta desconocida: {tool_name}"

        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"

    # ── Helper: run single agent safely ─────────────────────────────────────

    async def _run_agent_safe(
        self, agent_key: str, method_name: str, **kwargs
    ) -> AgentResult:
        """Run an agent method safely, capturing all exceptions as failed results."""
        agent = self.agents.get(agent_key)
        if not agent:
            return AgentResult(
                success=False,
                agent_name=f"missing:{agent_key}",
                agent_emoji="❓",
                summary=f"Agente '{agent_key}' no disponible",
                error=f"Agente '{agent_key}' no registrado en AlertAgent",
            )
        try:
            method = getattr(agent, method_name)
            if kwargs:
                result = await method(**kwargs)
            else:
                result = await method()
            return result
        except Exception as e:
            return AgentResult(
                success=False,
                agent_name=agent_key,
                agent_emoji=getattr(agent, "emoji", "❓"),
                summary=f"Error ejecutando {method_name} en {agent_key}: {e}",
                error=str(e),
            )

    # ── Specialized method ───────────────────────────────────────────────────

    async def run_full_scan(self) -> list[AgentResult]:
        """
        Ejecuta un scan completo de todos los agentes en paralelo.
        Consolida y persiste las alertas más críticas.
        Retorna lista de AgentResult, uno por agente.
        """
        t0 = time.monotonic()

        # Define which method to call on each agent
        scan_tasks = [
            ("sales", "analyze_daily", {}),
            ("inventory", "check_critical_stock", {"threshold": 5}),
            ("health", "daily_health_check", {}),
            ("ads", "analyze_campaigns", {}),
            ("qa", "run_integrity_check", {}),
        ]

        # Run all scans in parallel
        coros = []
        for agent_key, method_name, kwargs in scan_tasks:
            coros.append(self._run_agent_safe(agent_key, method_name, **kwargs))

        results: list[AgentResult] = list(await asyncio.gather(*coros, return_exceptions=False))

        # --- Consolidate critical findings ---
        critical_items: list[str] = []
        warning_items: list[str] = []

        for result in results:
            if not result.success:
                warning_items.append(f"{result.agent_emoji} {result.agent_name}: {result.error[:100]}")
                continue
            msg_lower = result.summary.lower()
            if any(kw in msg_lower for kw in ["crítico", "sin stock", "rojo", "0 stock", "bloqueante", "urgente"]):
                critical_items.append(f"{result.agent_emoji} {result.agent_name}: {result.summary[:150]}")
            elif any(kw in msg_lower for kw in ["amarillo", "warning", "bajo", "quemando", "discrepancia"]):
                warning_items.append(f"{result.agent_emoji} {result.agent_name}: {result.summary[:150]}")

        # Create consolidated alert if critical issues
        elapsed = time.monotonic() - t0
        if critical_items:
            await memory_manager.create_alert(
                agent_name=self.name,
                level="critical",
                title=f"Scan completo: {len(critical_items)} problema(s) crítico(s)",
                message="\n".join(critical_items),
                data={
                    "scan_duration_s": round(elapsed, 2),
                    "agents_run": len(scan_tasks),
                    "critical_count": len(critical_items),
                    "warning_count": len(warning_items),
                },
            )
        elif warning_items:
            await memory_manager.create_alert(
                agent_name=self.name,
                level="warning",
                title=f"Scan completo: {len(warning_items)} advertencia(s)",
                message="\n".join(warning_items),
                data={
                    "scan_duration_s": round(elapsed, 2),
                    "agents_run": len(scan_tasks),
                    "critical_count": 0,
                    "warning_count": len(warning_items),
                },
            )

        # Persist scan metadata
        await memory_manager.remember(self.name, "last_full_scan", {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": round(elapsed, 2),
            "agents_run": len(scan_tasks),
            "critical_count": len(critical_items),
            "warning_count": len(warning_items),
            "all_ok": not critical_items and not warning_items,
        })

        return results

    async def get_active_alerts(self, limit: int = 50) -> list[dict]:
        """Retorna alertas no leídas del memory_manager."""
        return await memory_manager.get_alerts(unread_only=True, limit=limit)

    async def create_alert_direct(
        self, level: str, title: str, message: str, data: dict | None = None
    ) -> int:
        """Crea una alerta directamente en memoria sin pasar por Claude."""
        return await memory_manager.create_alert(
            agent_name=self.name,
            level=level,
            title=title,
            message=message,
            data=data or {},
        )
