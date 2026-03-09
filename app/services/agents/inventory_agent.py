"""
app/services/agents/inventory_agent.py

Inventory Guard Agent — monitorea stock, detecta agotamiento y sincroniza inventario.
"""

from __future__ import annotations

import json
import time

import httpx

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

BASE_URL = "http://localhost:8000"


class InventoryAgent(BaseAgent):
    name = "inventory_guard"
    emoji = "📦"
    description = "Monitorea stock, detecta agotamiento y sincroniza inventario"

    def __init__(self):
        super().__init__(memory_manager=memory_manager)

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        return [
            {
                "name": "get_items_no_stock",
                "description": (
                    "Obtiene la lista de items de MeLi sin stock disponible que tienen "
                    "historial de ventas (quiebres de stock activos). "
                    "Retorna item_id, título, SKU y unidades vendidas previas."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_stock_issues",
                "description": (
                    "Obtiene productos con stock crítico bajo en BinManager comparado "
                    "con lo publicado en MeLi: diferencias, faltantes y riesgos de quiebre inminente."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_global_inventory",
                "description": (
                    "Escaneo completo del inventario global: stock en BinManager (MTY, CDMX, TJ) "
                    "vs stock publicado en MeLi. Operación lenta, usa solo cuando necesites "
                    "el panorama completo. Timeout de 60 segundos."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "update_item_stock",
                "description": (
                    "Actualiza el stock disponible de un item en MeLi. "
                    "Requiere el item_id (ej: MLM123456) y la cantidad nueva."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ID del item en MeLi (ej: MLM1234567890)",
                        },
                        "quantity": {
                            "type": "integer",
                            "description": "Nuevo stock disponible a asignar",
                            "minimum": 0,
                        },
                    },
                    "required": ["item_id", "quantity"],
                },
            },
        ]

    def _get_system_prompt(self) -> str:
        return (
            "Eres el guardián de inventario de un negocio en MeLi y Amazon Mexico. "
            "Tu misión es prevenir ventas sin stock y detectar riesgos de quiebre inminente. "
            "Prioridades: 1) Items con 0 stock que siguen activos (pérdida de ventas inmediata), "
            "2) Items con stock < 5 unidades y buena velocidad de venta (riesgo en <48h), "
            "3) Diferencias entre BinManager y MeLi que deben corregirse. "
            "Siempre cuantifica el impacto económico estimado de cada quiebre. "
            "Responde en español con acciones concretas y urgencia apropiada."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "get_items_no_stock":
                return await self._fetch_html_partial(f"{BASE_URL}/partials/items-no-stock")

            elif tool_name == "get_stock_issues":
                return await self._fetch_html_partial(f"{BASE_URL}/partials/products-stock-issues")

            elif tool_name == "get_global_inventory":
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/inventory/global-scan")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} en global-scan"
                try:
                    data = resp.json()
                    return json.dumps(data, ensure_ascii=False, indent=2)[:8000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "update_item_stock":
                item_id = tool_input.get("item_id", "")
                quantity = tool_input.get("quantity", 0)
                if not item_id:
                    return "Error: item_id es requerido"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{BASE_URL}/api/items/{item_id}/stock",
                        json={"available_quantity": quantity},
                    )
                if resp.status_code in (200, 201):
                    return f"Stock actualizado: {item_id} → {quantity} unidades"
                return f"Error HTTP {resp.status_code} al actualizar {item_id}: {resp.text[:500]}"

            else:
                return f"Herramienta desconocida: {tool_name}"

        except httpx.TimeoutException:
            return f"Timeout al ejecutar {tool_name}"
        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"

    async def _fetch_html_partial(self, url: str) -> str:
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

    async def check_critical_stock(self, threshold: int = 5) -> AgentResult:
        """
        Verifica el stock crítico de todos los productos.
        Genera alertas para items con stock <= threshold y items sin stock.
        """
        t0 = time.monotonic()
        task = (
            f"Revisa el estado completo del inventario con umbral crítico de {threshold} unidades. "
            "1. Obtén items sin stock (get_items_no_stock). "
            "2. Obtén problemas de stock (get_stock_issues). "
            "3. Clasifica en: CRÍTICO (0 stock), URGENTE (1-{threshold} unidades), ATENCIÓN (stock bajando). "
            "4. Para cada item crítico indica: ID, título, SKU, stock actual, ventas previas/día. "
            "5. Estima el impacto económico diario de los quiebres actuales. "
            "6. Lista acciones inmediatas ordenadas por impacto. "
            "Responde en español con formato claro."
        )
        try:
            result = await self.run(task)
            elapsed = time.monotonic() - t0

            # Persist last check
            await memory_manager.remember(self.name, "last_stock_check", {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "threshold": threshold,
                "summary": result.message[:300],
            })

            # Create alert if critical issues found
            msg_lower = result.message.lower()
            if any(kw in msg_lower for kw in ["crítico", "sin stock", "0 unidades", "quiebre"]):
                await memory_manager.create_alert(
                    agent_name=self.name,
                    level="critical",
                    title="Stock crítico detectado",
                    message=result.message[:500],
                    data={"threshold": threshold},
                )

            return AgentResult(
                success=result.success,
                agent_name=self.name,
                agent_emoji=self.emoji,
                summary=result.message[:200],
                details=result.message,
                data={"threshold": threshold, **result.data},
                actions=result.actions,
                elapsed_seconds=elapsed,
                error=result.message if not result.success else "",
            )
        except Exception as e:
            return AgentResult(
                success=False,
                agent_name=self.name,
                agent_emoji=self.emoji,
                summary=f"Error en check_critical_stock: {e}",
                error=str(e),
                elapsed_seconds=time.monotonic() - t0,
            )
