"""
app/services/agents/qa_agent.py

QA & Data Integrity Agent — valida integridad de datos entre plataformas
y detecta discrepancias de precios, stock y SKUs.
"""

from __future__ import annotations

import json
import time

import httpx

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

BASE_URL = "http://localhost:8000"


class QAAgent(BaseAgent):
    name = "qa_integrity"
    emoji = "🔍"
    description = "Valida integridad de datos entre plataformas y detecta discrepancias"

    def __init__(self):
        super().__init__(memory_manager=memory_manager)

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        return [
            {
                "name": "get_sku_comparison",
                "description": (
                    "Compara datos de SKUs específicos entre MeLi, Amazon y BinManager: "
                    "precio, stock, estado de publicación, inconsistencias detectadas."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "skus": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de SKUs a comparar (ej: ['SNTV001763', 'SNAF000022'])",
                            "minItems": 1,
                            "maxItems": 50,
                        },
                    },
                    "required": ["skus"],
                },
            },
            {
                "name": "get_items_summary",
                "description": (
                    "Obtiene resumen del inventario de MeLi: total activos, pausados, "
                    "sin stock, con variaciones, distribución por tipo de envío."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_amazon_summary",
                "description": (
                    "Obtiene resumen del catálogo de Amazon: total SKUs, activos, "
                    "inactivos, FBA, Seller Flex, stock total disponible."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_inventory_global",
                "description": (
                    "Obtiene el inventario global del dashboard: stock en BinManager "
                    "(MTY, CDMX, TJ) y comparativo con lo publicado en MeLi y Amazon."
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
            "Eres un auditor de datos especializado en integridad de información entre "
            "MeLi, Amazon y BinManager (sistema de inventario interno). "
            "Tu misión es detectar inconsistencias que causan problemas operativos: "
            "- Stock publicado > stock real en BinManager (riesgo de vender sin existencia). "
            "- Precios diferentes entre MeLi y Amazon para el mismo SKU (oportunidad de arbitraje). "
            "- SKUs que existen en BinManager pero no están publicados en ningún marketplace. "
            "- Items activos en MeLi sin SKU asignado (invisibles para el sistema de inventario). "
            "- Precios desactualizados vs costo de BinManager (margen negativo). "
            "Clasifica cada discrepancia por severidad: BLOQUEANTE / ALTA / MEDIA / BAJA. "
            "Cuantifica el impacto económico de las discrepancias de precio y stock. "
            "Responde en español con tablas o listas estructuradas."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "get_sku_comparison":
                skus = tool_input.get("skus", [])
                if not skus:
                    return "Error: se requiere al menos un SKU para comparar"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{BASE_URL}/api/inventory/compare",
                        json={"skus": skus},
                    )
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al comparar SKUs: {resp.text[:500]}"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:7000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "get_items_summary":
                return await self._fetch_partial(f"{BASE_URL}/partials/products-summary")

            elif tool_name == "get_amazon_summary":
                return await self._fetch_partial(f"{BASE_URL}/partials/amazon-products-summary")

            elif tool_name == "get_inventory_global":
                return await self._fetch_partial(f"{BASE_URL}/partials/products-inventory")

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

    async def run_integrity_check(self) -> AgentResult:
        """
        Ejecuta una verificación completa de integridad de datos entre
        MeLi, Amazon y BinManager. Detecta y reporta todas las discrepancias.
        """
        t0 = time.monotonic()
        task = (
            "Ejecuta una verificación completa de integridad de datos. "
            "1. Obtén resumen de MeLi (get_items_summary). "
            "2. Obtén resumen de Amazon (get_amazon_summary). "
            "3. Obtén inventario global (get_inventory_global). "
            "4. Analiza las discrepancias entre plataformas. "
            "Busca y reporta: "
            "- Items con stock MeLi > stock BinManager disponible. "
            "- Items activos en MeLi sin SKU (no rastreables en inventario). "
            "- SKUs con precio MeLi ≠ precio Amazon (±10% diferencia). "
            "- Items publicados activos con 0 stock en BinManager. "
            "- Diferencias de totales entre resúmenes de cada plataforma. "
            "Para cada discrepancia: plataforma, item/SKU afectado, valor actual vs esperado, "
            "impacto económico estimado, acción recomendada. "
            "Ordena por severidad: BLOQUEANTE > ALTA > MEDIA > BAJA. "
            "Al final, calcula un score de integridad 0-100."
        )
        try:
            result = await self.run(task)
            elapsed = time.monotonic() - t0

            await memory_manager.remember(self.name, "last_integrity_check", {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary": result.message[:300],
            })

            # Create alert if blocking issues found
            msg_lower = result.message.lower()
            if "bloqueante" in msg_lower:
                await memory_manager.create_alert(
                    agent_name=self.name,
                    level="critical",
                    title="Discrepancias bloqueantes en integridad de datos",
                    message=result.message[:500],
                    data={},
                )
            elif any(kw in msg_lower for kw in ["alta", "discrepancia", "inconsistencia"]):
                await memory_manager.create_alert(
                    agent_name=self.name,
                    level="warning",
                    title="Discrepancias de datos detectadas",
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
                summary=f"Error en run_integrity_check: {e}",
                error=str(e),
                elapsed_seconds=time.monotonic() - t0,
            )
