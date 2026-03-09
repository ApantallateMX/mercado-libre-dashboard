"""
app/services/agents/pricing_agent.py

Pricing Strategist Agent — optimiza precios basado en márgenes, competencia
y velocidad de ventas.
"""

from __future__ import annotations

import json
import time

import httpx

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

BASE_URL = "http://localhost:8000"


class PricingAgent(BaseAgent):
    name = "pricing_strategist"
    emoji = "💰"
    description = "Optimiza precios basado en márgenes, competencia y velocidad de ventas"

    def __init__(self):
        super().__init__(memory_manager=memory_manager)

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        return [
            {
                "name": "get_products_margin",
                "description": (
                    "Obtiene el catálogo completo de productos con sus márgenes calculados: "
                    "precio de venta, costo, margen bruto, comisión MeLi, ingreso neto."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_buybox_data",
                "description": (
                    "Obtiene datos de buybox de Amazon: precio actual, precio de competidores, "
                    "si el seller tiene el buybox o lo perdió y a qué precio está el ganador."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_products_deals",
                "description": (
                    "Obtiene los productos actualmente en promociones/deals en MeLi: "
                    "precio normal, precio promocional, descuento, vigencia."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "update_meli_price",
                "description": (
                    "Actualiza el precio de un item en MeLi. "
                    "Requiere item_id y el nuevo precio como número decimal."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ID del item en MeLi (ej: MLM1234567890)",
                        },
                        "price": {
                            "type": "number",
                            "description": "Nuevo precio de venta en MXN",
                            "minimum": 1,
                        },
                    },
                    "required": ["item_id", "price"],
                },
            },
            {
                "name": "update_amazon_price",
                "description": (
                    "Actualiza el precio de un producto en Amazon por SKU. "
                    "Requiere el SKU del vendedor y el nuevo precio."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sku": {
                            "type": "string",
                            "description": "SKU del vendedor en Amazon",
                        },
                        "price": {
                            "type": "number",
                            "description": "Nuevo precio de venta en MXN",
                            "minimum": 1,
                        },
                    },
                    "required": ["sku", "price"],
                },
            },
        ]

    def _get_system_prompt(self) -> str:
        return (
            "Eres un estratega de precios experto para marketplaces MeLi y Amazon Mexico. "
            "Tu objetivo es maximizar el margen neto del negocio manteniendo competitividad. "
            "REGLAS ABSOLUTAS: "
            "1) NUNCA sugieras precios por debajo del costo del producto. "
            "2) Siempre calcula el margen neto (después de comisión MeLi + IVA + envío). "
            "3) En Amazon, el objetivo principal es ganar/mantener el Buybox. "
            "4) En MeLi, el equilibrio es precio competitivo + margen aceptable (>15% neto mínimo). "
            "Cuando detectes oportunidades, calcula el impacto mensual estimado en MXN. "
            "Responde siempre en español con cifras concretas."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "get_products_margin":
                return await self._fetch_partial(f"{BASE_URL}/partials/products-full")

            elif tool_name == "get_buybox_data":
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/amazon/products/buybox")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al obtener buybox"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "get_products_deals":
                return await self._fetch_partial(f"{BASE_URL}/partials/products-deals")

            elif tool_name == "update_meli_price":
                item_id = tool_input.get("item_id", "")
                price = tool_input.get("price")
                if not item_id or price is None:
                    return "Error: item_id y price son requeridos"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.put(
                        f"{BASE_URL}/api/items/{item_id}/price",
                        json={"price": float(price)},
                    )
                if resp.status_code in (200, 201):
                    return f"Precio MeLi actualizado: {item_id} → ${price:,.2f} MXN"
                return f"Error HTTP {resp.status_code} actualizando precio MeLi {item_id}: {resp.text[:500]}"

            elif tool_name == "update_amazon_price":
                sku = tool_input.get("sku", "")
                price = tool_input.get("price")
                if not sku or price is None:
                    return "Error: sku y price son requeridos"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.put(
                        f"{BASE_URL}/api/amazon/products/{sku}/price",
                        json={"price": float(price)},
                    )
                if resp.status_code in (200, 201):
                    return f"Precio Amazon actualizado: {sku} → ${price:,.2f} MXN"
                return f"Error HTTP {resp.status_code} actualizando precio Amazon {sku}: {resp.text[:500]}"

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

    async def find_pricing_opportunities(self) -> AgentResult:
        """
        Detecta oportunidades de optimización de precios en MeLi y Amazon.
        Analiza márgenes, buybox y deals activos para generar recomendaciones.
        """
        t0 = time.monotonic()
        task = (
            "Analiza los precios actuales y encuentra oportunidades de optimización. "
            "1. Obtén márgenes del catálogo MeLi (get_products_margin). "
            "2. Obtén datos de buybox Amazon (get_buybox_data). "
            "3. Revisa deals activos (get_products_deals). "
            "Identifica y clasifica oportunidades: "
            "- SUBIR PRECIO: productos con margen <15% que pueden subir sin perder competitividad. "
            "- BAJAR PRECIO: productos que perdieron buybox o tienen margen >40% con pocas ventas. "
            "- DEAL URGENTE: productos con stock alto y pocas ventas donde un deal temporal ayudaría. "
            "Para cada oportunidad indica: SKU/ID, precio actual, precio sugerido, "
            "impacto estimado en MXN/mes. "
            "NUNCA sugieras precios por debajo del costo. "
            "Ordena por impacto económico descendente."
        )
        try:
            result = await self.run(task)
            elapsed = time.monotonic() - t0

            await memory_manager.remember(self.name, "last_pricing_analysis", {
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
                summary=f"Error en find_pricing_opportunities: {e}",
                error=str(e),
                elapsed_seconds=time.monotonic() - t0,
            )
