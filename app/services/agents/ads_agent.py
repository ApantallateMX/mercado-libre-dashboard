"""
app/services/agents/ads_agent.py

Ads Optimizer Agent — analiza rendimiento de campañas publicitarias de MeLi
y detecta oportunidades de optimización.

NOTA: Los writes de Product Ads están bloqueados por certification_status: not_certified.
Este agente opera en modo solo-análisis y recomendaciones.
"""

from __future__ import annotations

import json
import time

import httpx

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

BASE_URL = "http://localhost:8000"


class AdsAgent(BaseAgent):
    name = "ads_optimizer"
    emoji = "📢"
    description = "Analiza rendimiento de campañas publicitarias y detecta oportunidades"

    def __init__(self):
        super().__init__(memory_manager=memory_manager)

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        return [
            {
                "name": "get_campaigns",
                "description": (
                    "Obtiene la lista de campañas publicitarias de MeLi: nombre, estado, "
                    "presupuesto diario, tipo, métricas de gasto y rendimiento."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_ads_performance",
                "description": (
                    "Obtiene el rendimiento general de los anuncios: impresiones, clics, "
                    "CTR, CPC promedio, ACOS, ROAS del periodo actual."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_burning_ads",
                "description": (
                    "Obtiene productos con gasto en publicidad pero SIN ventas generadas: "
                    "presupuesto quemado, clics sin conversión, ROAS = 0."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_best_ads",
                "description": (
                    "Obtiene los productos con mejor rendimiento en publicidad: "
                    "mayor ROAS, mejor CTR, mayor retorno por peso invertido."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_no_ads_items",
                "description": (
                    "Obtiene productos activos que NO tienen publicidad activa: "
                    "candidatos para iniciar campaña con alto potencial de retorno."
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
            "Eres un experto en publicidad de Mercado Libre (Product Ads) Mexico. "
            "Tu especialidad es maximizar el ROAS (Return on Ad Spend) y detectar gasto sin retorno. "
            "CONTEXTO IMPORTANTE: Los writes de ads están actualmente bloqueados por "
            "certification_status: not_certified. Solo puedes analizar y recomendar. "
            "Para aplicar cambios, el usuario debe ir a ads.mercadolibre.com.mx directamente. "
            "Métricas clave: "
            "- ROAS >4x = excelente, escalar presupuesto. "
            "- ROAS 2-4x = aceptable, optimizar. "
            "- ROAS <2x = revisar urgente. "
            "- ROAS 0 (gasto sin ventas) = pausar inmediatamente. "
            "- ACOS ideal <20% para México. "
            "Siempre cuantifica el monto de dinero en riesgo/oportunidad. "
            "Responde en español con recomendaciones accionables específicas."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        url_map = {
            "get_campaigns": f"{BASE_URL}/partials/ads-campaigns",
            "get_ads_performance": f"{BASE_URL}/partials/ads-performance",
            "get_burning_ads": f"{BASE_URL}/partials/ads-burning",
            "get_best_ads": f"{BASE_URL}/partials/ads-best",
            "get_no_ads_items": f"{BASE_URL}/partials/ads-no-ads",
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
            if "json" in content_type:
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    pass
            return self._html_to_text(resp.text)
        except httpx.TimeoutException:
            return f"Timeout al ejecutar {tool_name}"
        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"

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

    async def analyze_campaigns(self) -> AgentResult:
        """
        Análisis completo de campañas publicitarias MeLi.
        Detecta gasto sin retorno, oportunidades de escala y brechas de cobertura.
        """
        t0 = time.monotonic()
        task = (
            "Realiza el análisis completo de las campañas publicitarias de MeLi. "
            "1. Obtén estado de campañas activas (get_campaigns). "
            "2. Revisa rendimiento general (get_ads_performance). "
            "3. Identifica anuncios quemando dinero sin ventas (get_burning_ads). "
            "4. Identifica los mejores anuncios para escalar (get_best_ads). "
            "5. Revisa productos sin publicidad que deberían tenerla (get_no_ads_items). "
            "Genera reporte con: "
            "- RESUMEN EJECUTIVO: gasto total, ventas generadas, ROAS global, ACOS global. "
            "- ALERTAS: anuncios con ROAS<2 o gasto sin ventas (cuánto dinero). "
            "- OPORTUNIDADES: top 5 productos para escalar (mayor ROAS). "
            "- BRECHAS: top 5 productos sin ads con potencial (por ventas orgánicas). "
            "- ACCIONES: lista priorizada (recordatorio: cambios deben hacerse en ads.mercadolibre.com.mx). "
            "Responde en español con formato claro y cifras en MXN."
        )
        try:
            result = await self.run(task)
            elapsed = time.monotonic() - t0

            await memory_manager.remember(self.name, "last_campaigns_analysis", {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary": result.message[:300],
            })

            # Alert if burning money detected
            msg_lower = result.message.lower()
            if any(kw in msg_lower for kw in ["quemando", "sin ventas", "roas 0", "roas: 0"]):
                await memory_manager.create_alert(
                    agent_name=self.name,
                    level="warning",
                    title="Presupuesto publicitario sin retorno detectado",
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
                summary=f"Error en analyze_campaigns: {e}",
                error=str(e),
                elapsed_seconds=time.monotonic() - t0,
            )
