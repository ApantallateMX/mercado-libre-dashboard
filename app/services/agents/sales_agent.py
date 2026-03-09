"""
app/services/agents/sales_agent.py

Sales Intelligence Agent — analiza ventas MeLi + Amazon, detecta tendencias
y alerta sobre metas en riesgo.
"""

from __future__ import annotations

import json
import time

import httpx

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

BASE_URL = "http://localhost:8000"


class SalesAgent(BaseAgent):
    name = "sales_intelligence"
    emoji = "📊"
    description = "Analiza ventas, detecta tendencias y alerta sobre metas en riesgo"

    def __init__(self):
        super().__init__(memory_manager=memory_manager)

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        return [
            {
                "name": "get_sales_metrics",
                "description": (
                    "Obtiene métricas consolidadas del dashboard de ventas MeLi: "
                    "ventas del día, semana, mes, ingresos netos, órdenes pagadas/canceladas."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_amazon_metrics",
                "description": (
                    "Obtiene métricas del dashboard de Amazon: ventas, unidades, "
                    "OPS (Ordered Product Sales) del día y acumulado."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_daily_sales",
                "description": (
                    "Obtiene desglose de ventas diarias de MeLi: órdenes por día, "
                    "tendencia semanal, comparativa con semana anterior."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_recent_orders",
                "description": (
                    "Obtiene las órdenes recientes de Amazon con detalle de cada venta: "
                    "SKU, cantidad, precio, estado."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        ]

    def _get_system_prompt(self) -> str:
        return (
            "Eres un experto en análisis de ventas para marketplace MeLi y Amazon Mexico. "
            "Tu rol es detectar tendencias, comparar periodos, identificar productos estrella, "
            "y alertar sobre metas en riesgo. Siempre das recomendaciones específicas y "
            "accionables con números concretos. Responde siempre en español. "
            "Prioriza: 1) alertas críticas de caída de ventas, 2) oportunidades de crecimiento, "
            "3) productos con tendencia positiva a escalar. "
            "Cuando presentes datos, usa formato claro: bullet points y números formateados."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        url_map = {
            "get_sales_metrics": f"{BASE_URL}/api/metrics/dashboard-data",
            "get_amazon_metrics": f"{BASE_URL}/api/metrics/amazon-dashboard-data",
            "get_daily_sales": f"{BASE_URL}/api/metrics/daily-sales",
            "get_recent_orders": f"{BASE_URL}/api/metrics/amazon-recent-orders",
        }
        url = url_map.get(tool_name)
        if not url:
            return f"Herramienta desconocida: {tool_name}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return f"Error HTTP {resp.status_code} al llamar {tool_name}"
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type:
                return self._html_to_text(resp.text)
            try:
                data = resp.json()
                return json.dumps(data, ensure_ascii=False, indent=2)[:6000]
            except Exception:
                return resp.text[:4000]
        except httpx.TimeoutException:
            return f"Timeout al llamar {tool_name}"
        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"

    # ── BeautifulSoup helper (inherited pattern reimplemented inline) ─────────

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

    async def analyze_daily(self, context: dict | None = None) -> AgentResult:
        """
        Análisis autónomo del día: consolida métricas MeLi + Amazon,
        detecta anomalías y genera resumen ejecutivo con alertas.
        """
        t0 = time.monotonic()
        task = (
            "Realiza el análisis completo de ventas del día de hoy. "
            "1. Obtén métricas de MeLi y Amazon. "
            "2. Compara con tendencia reciente. "
            "3. Identifica productos o canales con caídas o picos inusuales. "
            "4. Lista alertas críticas si las hay (ej: caída >20% vs ayer). "
            "5. Da 3 recomendaciones accionables para hoy. "
            "Sé conciso pero completo. Termina con una calificación del día: "
            "EXCELENTE / BUENO / REGULAR / MALO con justificación de 1 línea."
        )
        try:
            result = await self.run(task, context=context or {})
            elapsed = time.monotonic() - t0

            # Persist last analysis timestamp
            await memory_manager.remember(self.name, "last_daily_analysis", {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary": result.message[:300],
            })

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
                summary=f"Error en analyze_daily: {e}",
                error=str(e),
                elapsed_seconds=time.monotonic() - t0,
            )
