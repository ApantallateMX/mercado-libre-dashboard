"""
app/services/agents/listing_agent.py

Listing Quality Agent — audita y optimiza la calidad de publicaciones
en MeLi y Amazon.
"""

from __future__ import annotations

import json
import time

import httpx

from app.services.agents.base import BaseAgent, AgentResult
from app.services.memory_manager import memory_manager

BASE_URL = "http://localhost:8000"


class ListingAgent(BaseAgent):
    name = "listing_quality"
    emoji = "✍️"
    description = "Audita y optimiza la calidad de publicaciones en MeLi y Amazon"

    def __init__(self):
        super().__init__(memory_manager=memory_manager)

    # ── Tool definitions ─────────────────────────────────────────────────────

    def _define_tools(self) -> list:
        return [
            {
                "name": "get_items_needing_work",
                "description": (
                    "Obtiene publicaciones de MeLi que necesitan mejora: "
                    "títulos cortos, descripción vacía, pocas fotos, atributos faltantes, "
                    "salud de publicación baja."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_item_details",
                "description": (
                    "Obtiene el detalle completo de una publicación en MeLi: "
                    "título, descripción, fotos, atributos, categoría, salud."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ID del item en MeLi (ej: MLM1234567890)",
                        },
                    },
                    "required": ["item_id"],
                },
            },
            {
                "name": "optimize_item_title",
                "description": (
                    "Lanza la optimización automática de título, descripción y atributos "
                    "de un item en MeLi usando IA. El sistema genera y aplica mejoras."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ID del item a optimizar (ej: MLM1234567890)",
                        },
                    },
                    "required": ["item_id"],
                },
            },
            {
                "name": "get_amazon_products",
                "description": (
                    "Obtiene el catálogo de productos de Amazon con su estado de calidad: "
                    "títulos, bullet points, descripciones, imágenes, score de contenido."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "get_not_published",
                "description": (
                    "Obtiene productos del inventario BinManager que existen en el sistema "
                    "pero NO están publicados en Amazon (oportunidades de publicación)."
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
            "Eres un especialista en SEO y calidad de contenido para marketplaces MeLi y Amazon Mexico. "
            "Tu objetivo es mejorar títulos, descripciones y atributos para aumentar la tasa de conversión "
            "y el posicionamiento orgánico. "
            "Principios para títulos MeLi: Brand + Tipo de Producto + Características Clave + Modelo. "
            "Máximo 60 caracteres, sin signos especiales, sin precio ni stock. "
            "Principios para Amazon: incluir keywords de búsqueda, máximo 150 caracteres, "
            "Brand + Keywords principales + Diferenciadores. "
            "Para categorizar la urgencia: "
            "- CRÍTICO: sin imágenes, título genérico <30 chars, sin descripción. "
            "- ALTA: descripciones incompletas, pocos atributos. "
            "- MEDIA: optimización de keywords y posicionamiento. "
            "Siempre estima el impacto en conversión de cada mejora. "
            "Responde en español con recomendaciones específicas y ejemplos concretos."
        )

    # ── Tool executor ────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "get_items_needing_work":
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/items/needs-work")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al obtener items que necesitan trabajo"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "get_item_details":
                item_id = tool_input.get("item_id", "")
                if not item_id:
                    return "Error: item_id es requerido"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/inventory/item-details/{item_id}")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al obtener detalles de {item_id}"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    return self._html_to_text(resp.text)

            elif tool_name == "optimize_item_title":
                item_id = tool_input.get("item_id", "")
                if not item_id:
                    return "Error: item_id es requerido"
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(f"{BASE_URL}/api/inventory/optimize/{item_id}")
                if resp.status_code in (200, 201):
                    try:
                        data = resp.json()
                        return f"Optimización completada para {item_id}: {json.dumps(data, ensure_ascii=False)[:500]}"
                    except Exception:
                        return f"Optimización completada para {item_id}"
                return f"Error HTTP {resp.status_code} al optimizar {item_id}: {resp.text[:500]}"

            elif tool_name == "get_amazon_products":
                return await self._fetch_partial(f"{BASE_URL}/partials/amazon-products-catalog")

            elif tool_name == "get_not_published":
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{BASE_URL}/api/amazon/products/sin-publicar")
                if resp.status_code != 200:
                    return f"Error HTTP {resp.status_code} al obtener productos sin publicar"
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)[:6000]
                except Exception:
                    return self._html_to_text(resp.text)

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

    async def audit_catalog(self) -> AgentResult:
        """
        Auditoría completa de calidad del catálogo MeLi y Amazon.
        Identifica publicaciones que necesitan mejora y prioriza por impacto.
        """
        t0 = time.monotonic()
        task = (
            "Realiza una auditoría completa de calidad del catálogo de publicaciones. "
            "1. Obtén publicaciones que necesitan trabajo en MeLi (get_items_needing_work). "
            "2. Revisa el catálogo de Amazon (get_amazon_products). "
            "3. Verifica productos sin publicar en Amazon (get_not_published). "
            "Genera un reporte de auditoría con: "
            "- SCORE GENERAL: porcentaje del catálogo con calidad aceptable. "
            "- CRÍTICOS MeLi: items con problemas graves que afectan visibilidad/conversión. "
            "- CRÍTICOS Amazon: listings con contenido incompleto o suprimidos. "
            "- OPORTUNIDADES AMAZON: top 5 productos sin publicar con mayor potencial. "
            "- PLAN DE ACCIÓN: ordenado por impacto estimado en ventas. "
            "Para cada item crítico, sugiere el título mejorado específicamente. "
            "Responde en español con formato claro y accionable."
        )
        try:
            result = await self.run(task)
            elapsed = time.monotonic() - t0

            await memory_manager.remember(self.name, "last_catalog_audit", {
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
                summary=f"Error en audit_catalog: {e}",
                error=str(e),
                elapsed_seconds=time.monotonic() - t0,
            )
