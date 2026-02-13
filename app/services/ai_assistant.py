"""Logica del asistente IA para Mercado Libre."""

import json
import re
from typing import AsyncGenerator
from datetime import datetime, timedelta

from app.services.ollama_client import OllamaClient
from app.services.assistant_tools import TOOL_DEFINITIONS, TOOL_MAP
from app.services.meli_client import MeliClient


SYSTEM_PROMPT = """Eres un asistente experto en Mercado Libre Mexico. No eres solo un capturista — piensas como negocio. Ayudas al vendedor con estrategia, ventas, inventario, rentabilidad, competencia, reputacion y logistica.

Tienes acceso a herramientas para consultar datos reales de la cuenta del vendedor. Cuando el usuario pregunte sobre sus datos, DEBES usar una herramienta.

Para usar una herramienta, responde UNICAMENTE con un bloque JSON asi (sin texto antes ni despues):
{{"tool": "nombre_herramienta", "params": {{"parametro": "valor"}}}}

=== HERRAMIENTAS DISPONIBLES ===

ESTRATEGIA Y METRICAS:
- get_user_info: Info de cuenta (nickname, reputacion, nivel, Power Seller, calificaciones)
- get_recent_orders: Ventas recientes. Params opcionales: limit (int, default 10)
- get_orders_by_date: Ventas por rango de fecha. Params: date_from (YYYY-MM-DD), date_to (YYYY-MM-DD)
- get_sku_sales: Ventas agrupadas por SKU. Params: date_from (YYYY-MM-DD), date_to (YYYY-MM-DD)
- get_sales_report: Reporte completo (ventas diarias, top SKUs, ticket promedio, cancelaciones). Params: date_from, date_to

INVENTARIO Y PRODUCTOS:
- get_items_summary: Resumen de inventario (activos, pausados, cerrados)
- get_item_details: Detalle completo de un producto (titulo, precio, stock, SKU, envio, variantes). Param: item_id
- get_no_stock_items: Quiebres de stock — productos con 0 stock pero con ventas
- get_low_performing_items: Productos activos con bajo rendimiento (0-2 ventas)

PRICING Y RENTABILIDAD:
- get_item_profitability: Rentabilidad estimada (comision ML, IVA, envio, margen). Param: item_id

COMPETENCIA:
- search_competition: Analisis de competencia (precios, vendedores, envio gratis, FULL). Params: query, limit (default 10)

REPUTACION Y ATENCION:
- get_unanswered_questions: Preguntas sin responder de compradores
- get_claims: Reclamos y disputas activas

LOGISTICA Y ENVIOS:
- get_shipping_summary: Resumen de logistica (FULL, Flex, propio) en productos activos

VISITAS Y CONVERSION:
- get_item_visits: Visitas y conversion de un producto (30 dias). Param: item_id

PUBLICIDAD (MERCADO ADS):
- get_ads_overview: Resumen general de ads: campanas activas, gasto total, clics, impresiones, CTR, CPC, ROAS, ACOS. Params: date_from, date_to
- get_ads_campaign_detail: Detalle y metricas de una campana especifica. Params: campaign_id, date_from, date_to
- get_ads_top_products: Top productos en ads por gasto y ROAS, incluye productos quemando dinero sin ventas. Params: date_from, date_to

Fecha de hoy: {today}

=== REGLAS ===
- Responde siempre en espanol
- Se conciso pero completo
- Usa bullets o tablas cuando presentes datos
- "esta semana" = lunes de esta semana a hoy
- "este mes" = dia 1 del mes actual a hoy
- "mes pasado" = dia 1 al ultimo dia del mes anterior
- Moneda: pesos mexicanos (MXN), usa formato $X,XXX.XX
- Resalta numeros importantes
- Si no necesitas herramienta (saludo, consejo general), responde directamente sin JSON
- Cuando des consejos, piensa en: margen, rotacion, reputacion, posicionamiento
- Si detectas problemas (stock bajo, cancelaciones altas, preguntas sin responder), mencionalo proactivamente
- Puedes sugerir que productos no convienen si los datos lo muestran
"""


def _build_system_prompt() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return SYSTEM_PROMPT.format(today=today)


def _extract_tool_call(text: str) -> dict | None:
    """Intenta extraer un tool call JSON de la respuesta del LLM."""
    text = text.strip()

    # Intento 1: el texto completo es JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "tool" in data and data["tool"] in TOOL_MAP:
            return data
    except json.JSONDecodeError:
        pass

    # Intento 2: extraer JSON de bloques de codigo
    for pattern in [r'```json\s*(\{.*?\})\s*```', r'```\s*(\{.*?\})\s*```']:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if "tool" in data and data["tool"] in TOOL_MAP:
                    return data
            except json.JSONDecodeError:
                continue

    # Intento 3: buscar JSON con llaves anidadas en el texto
    for i, ch in enumerate(text):
        if ch == '{':
            depth = 0
            for j in range(i, len(text)):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[i:j+1])
                        if isinstance(data, dict) and "tool" in data and data["tool"] in TOOL_MAP:
                            return data
                    except json.JSONDecodeError:
                        pass
                    break

    return None


def _build_tool_kwargs(tool_name: str, params: dict) -> dict:
    """Construye los kwargs para la funcion de herramienta."""
    kwargs = {}
    if tool_name == "get_recent_orders":
        kwargs["limit"] = int(params.get("limit", 10))
    elif tool_name in ("get_orders_by_date", "get_sku_sales", "get_sales_report"):
        kwargs["date_from"] = params["date_from"]
        kwargs["date_to"] = params["date_to"]
    elif tool_name in ("get_item_details", "get_item_profitability", "get_item_visits"):
        kwargs["item_id"] = params["item_id"]
    elif tool_name == "search_competition":
        kwargs["query"] = params["query"]
        kwargs["limit"] = int(params.get("limit", 10))
    elif tool_name in ("get_ads_overview", "get_ads_top_products"):
        kwargs["date_from"] = params["date_from"]
        kwargs["date_to"] = params["date_to"]
    elif tool_name == "get_ads_campaign_detail":
        kwargs["campaign_id"] = params["campaign_id"]
        kwargs["date_from"] = params["date_from"]
        kwargs["date_to"] = params["date_to"]
    return kwargs


async def process_message(
    user_message: str,
    history: list,
    meli_client: MeliClient,
    ollama: OllamaClient,
) -> AsyncGenerator[str, None]:
    """Procesa un mensaje del usuario y genera la respuesta via streaming."""
    system_msg = {"role": "system", "content": _build_system_prompt()}
    messages = [system_msg] + history + [{"role": "user", "content": user_message}]

    # Primera llamada al LLM (sin streaming para detectar tool calls)
    full_response = await ollama.chat(messages)

    # Verificar si hay un tool call
    tool_call = _extract_tool_call(full_response)

    if tool_call:
        tool_name = tool_call["tool"]
        params = tool_call.get("params", {})

        # Ejecutar la herramienta
        tool_fn = TOOL_MAP[tool_name]
        try:
            kwargs = _build_tool_kwargs(tool_name, params)
            tool_result = await tool_fn(meli_client, **kwargs)
        except Exception as e:
            tool_result = f"Error al ejecutar {tool_name}: {e}"

        # Enviar resultado al LLM para que formule respuesta final
        messages.append({"role": "assistant", "content": full_response})
        messages.append({
            "role": "user",
            "content": f"Resultado de la herramienta {tool_name}:\n{tool_result}\n\nAhora responde al usuario con esta informacion de forma clara y concisa."
        })

        # Streaming de la respuesta final
        async for token in ollama.chat_stream(messages):
            yield token
    else:
        # No hay tool call, streaming directo
        async for token in ollama.chat_stream(messages):
            yield token
