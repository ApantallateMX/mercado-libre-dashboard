"""Prompt builders and response parsers for Health AI features."""

import json
import re


def _bm_product_block(bm_product: dict) -> str:
    """Build a product info block from BinManager data for prompt injection."""
    if not bm_product:
        return ""
    parts = []
    if bm_product.get("brand"):
        parts.append(f"- Marca: {bm_product['brand']}")
    if bm_product.get("model"):
        parts.append(f"- Modelo: {bm_product['model']}")
    if bm_product.get("title"):
        parts.append(f"- Nombre comercial: {bm_product['title']}")
    if bm_product.get("category"):
        parts.append(f"- Categoria: {bm_product['category']}")
    if bm_product.get("description"):
        desc = bm_product["description"][:500]
        parts.append(f"- Descripcion: {desc}")
    if not parts:
        return ""
    return "\nInformacion real del producto (BinManager):\n" + "\n".join(parts) + "\n"


def build_question_answer_prompt(question_text, product_title, product_price, product_stock, elapsed, buyer_history=None, user_context=None, bm_product=None):
    system = (
        "Eres un vendedor profesional en Mercado Libre Mexico con alta tasa de conversion.\n\n"
        "ESTRUCTURA OBLIGATORIA de cada respuesta:\n"
        "1. SALUDO — breve y calido (ej: 'Hola! Gracias por tu pregunta.')\n"
        "2. RESPUESTA — directa, concreta, responde exactamente lo que preguntaron\n"
        "3. PROPUESTA — CTA sutil que invite a comprar (ej: 'Dale click en Comprar y te lo enviamos hoy mismo')\n"
        "4. DESPEDIDA — cordial, 1 linea (ej: 'Quedamos al pendiente, saludos!')\n\n"
        "TIPOS DE PREGUNTA y como manejarlas:\n"
        "- OPERATIVA (envio, garantia, factura): responde con certeza, menciona beneficios de MeLi (envio gratis, Compra Protegida)\n"
        "- TECNICA (especificaciones, compatibilidad): se preciso, si no estas seguro di 'te recomiendo verificar en la descripcion del producto'\n"
        "- PROPOSICION (ofertas, descuentos, combos): redirige a compra directa, no ofrezcas descuentos fuera de MeLi\n"
        "- COMPUESTA (multiples preguntas): responde cada punto numerado\n"
        "- STOCK (disponibilidad): confirma stock y agrega urgencia sutil si hay poco\n\n"
        "REGLAS ESTRICTAS:\n"
        "- NUNCA compartas datos de contacto externos (telefono, email, WhatsApp, redes sociales)\n"
        "- NUNCA digas solo 'no' — siempre sugiere alternativa o solucion\n"
        "- NO prometas tiempos de entrega exactos (depende de la paqueteria)\n"
        "- Maximo 2000 caracteres (usa lo que necesites, no te limites innecesariamente)\n"
        "- Tono profesional pero cercano y humano\n"
        "- Responde SOLO con el texto de la respuesta, sin explicaciones adicionales ni comillas\n"
        "- Si el comprador ya hizo preguntas antes, reconocelo brevemente y no repitas info ya dada"
    )

    user = f'Pregunta del comprador: "{question_text}"\n\n'

    # Buyer history context
    if buyer_history:
        user += "HISTORIAL DEL COMPRADOR (preguntas anteriores):\n"
        for i, h in enumerate(buyer_history[:5], 1):
            user += f"  {i}. [{h.get('date', '')}] \"{h.get('text', '')}\""
            if h.get("answer"):
                user += f" -> Respuesta: \"{h['answer']}\""
            if h.get("item_id"):
                user += f" (item: {h['item_id']})"
            user += "\n"
        user += (
            "INSTRUCCIONES PARA HISTORIAL:\n"
            "- Si es comprador recurrente, reconocelo brevemente (ej: 'Que gusto verte de nuevo')\n"
            "- No repitas informacion ya proporcionada en respuestas anteriores\n"
            "- Si sus preguntas previas sugieren intencion de compra, refuerza el cierre\n\n"
        )

    stock_note = ""
    if product_stock == 0:
        stock_note = " (SIN STOCK — sugiere que pregunte de nuevo pronto o vea productos similares)"
    elif product_stock <= 3:
        stock_note = " (POCO STOCK — menciona sutilmente que quedan pocas unidades)"
    bm_block = _bm_product_block(bm_product or {})
    user += (
        f"Datos del producto:\n"
        f"- Titulo MeLi: {product_title}\n"
        f"- Precio: ${product_price}\n"
        f"- Stock disponible: {product_stock} unidades{stock_note}\n"
    )
    if bm_block:
        user += bm_block
    user += f"- Tiempo desde la pregunta: {elapsed}\n"
    if elapsed:
        # Add urgency context
        user += "(Responde con tono acorde a la espera del comprador)\n"
    if user_context:
        user += f"\n\nINSTRUCCIONES DEL VENDEDOR: {user_context}\n(Incorpora estas instrucciones de forma natural en tu respuesta)"

    user += "\nGenera una respuesta profesional siguiendo la estructura Saludo+Respuesta+Propuesta+Despedida:"

    return system, user, 800


def build_claim_response_prompt(claim_id, reason_id, reason_desc, product_title, days_open, issues, suggestions, bm_product=None):
    system = (
        "Eres un gestor de reclamos profesional en Mercado Libre Mexico. "
        "Tu objetivo es resolver reclamos rapidamente manteniendo la reputacion del vendedor.\n"
        "REGLAS:\n"
        "- Tono empatico y profesional\n"
        "- Ofrecer una solucion concreta (devolucion, reemplazo, descuento, etc.)\n"
        "- Si la resolucion es en menos de 2 dias, NO afecta la reputacion\n"
        "- Maximo 500 caracteres\n"
        "- Responde SOLO con el texto del mensaje al comprador, sin explicaciones ni comillas"
    )
    issues_str = "; ".join(issues) if issues else "No especificados"
    suggestions_str = "; ".join(suggestions) if suggestions else "Ninguna"
    bm_block = _bm_product_block(bm_product or {})
    user = (
        f"Reclamo #{claim_id}\n"
        f"Razon: {reason_desc} (ID: {reason_id})\n"
        f"Producto MeLi: {product_title}\n"
        + (bm_block if bm_block else "")
        + f"Dias abierto: {days_open}\n"
        f"Problemas detectados: {issues_str}\n"
        f"Sugerencias del sistema: {suggestions_str}\n\n"
        "Genera un mensaje empatico y con solucion concreta para el comprador:"
    )
    return system, user, 300


def build_claim_analysis_prompt(reason_desc, product_title, product_price, days_open,
                                claims_rate, claims_status, sale_fee, shipping_cost, bm_product=None):
    system = (
        "Eres un consultor experto en Mercado Libre Mexico. "
        "Analiza reclamos y recomienda la mejor accion para el vendedor. "
        "Responde UNICAMENTE con un JSON valido (sin markdown, sin texto extra, sin backticks)."
    )
    bm_block = _bm_product_block(bm_product or {})
    user = (
        f"Analiza este reclamo y da tu recomendacion:\n\n"
        f"Razon: {reason_desc}\n"
        f"Producto MeLi: {product_title}\n"
        + (bm_block if bm_block else "")
        + f"Precio del producto: ${product_price}\n"
        f"Dias abierto: {days_open}\n"
        f"Tasa de reclamos actual: {claims_rate}% ({claims_status})\n"
        f"Comision de venta: ${sale_fee}\n"
        f"Costo de envio: ${shipping_cost}\n\n"
        "RECLAMOS QUE NO AFECTAN REPUTACION (no importa resultado):\n"
        "- Paquete danado por paqueteria\n"
        "- Arrepentimiento del comprador\n"
        "- Cambio de talla/color\n"
        "- Demora de paqueteria\n\n"
        "Responde con este JSON exacto:\n"
        "{\n"
        '  "recommendation": "devolver_total | devolver_parcial | reemplazar | mediar | rechazar",\n'
        '  "confidence": "alta | media | baja",\n'
        '  "affects_reputation": true o false,\n'
        '  "financial_impact": {\n'
        '    "refund_cost": 0.00,\n'
        '    "recovered_commission": 0.00,\n'
        '    "net_loss": 0.00\n'
        "  },\n"
        '  "pros": ["ventaja 1", "ventaja 2"],\n'
        '  "cons": ["desventaja 1", "desventaja 2"],\n'
        '  "summary": "Resumen breve de la recomendacion y por que"\n'
        "}"
    )
    return system, user, 800


def build_message_reply_prompt(thread_messages, last_buyer_message):
    system = (
        "Eres un vendedor profesional de post-venta en Mercado Libre Mexico. "
        "Responde mensajes de compradores de forma util y profesional.\n"
        "REGLAS:\n"
        "- NUNCA compartas datos de contacto externos\n"
        "- Maximo 500 caracteres\n"
        "- Tono amable y servicial\n"
        "- Si es un problema con el envio, sugiere revisar el tracking en la app de MeLi\n"
        "- Responde SOLO con el texto del mensaje, sin explicaciones ni comillas"
    )
    history = ""
    for msg in (thread_messages or []):
        sender = "Vendedor" if msg.get("is_seller") else "Comprador"
        history += f"  {sender}: {msg.get('text', '')}\n"
    user = (
        "Historial de conversacion reciente:\n"
        f"{history}\n"
        f'Ultimo mensaje del comprador: "{last_buyer_message}"\n\n'
        "Genera una respuesta profesional:"
    )
    return system, user, 300


def parse_claim_analysis(raw_text):
    """Parse claim analysis JSON from Claude response."""
    if not raw_text:
        return _fallback_analysis("Respuesta vacia del modelo")

    # Direct JSON parse
    try:
        return json.loads(raw_text.strip())
    except (json.JSONDecodeError, TypeError):
        pass

    # Extract from markdown code blocks
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # Find JSON object in text
    try:
        start = raw_text.index('{')
        end = raw_text.rindex('}') + 1
        return json.loads(raw_text[start:end])
    except (ValueError, json.JSONDecodeError):
        pass

    return _fallback_analysis(raw_text[:200])


def _fallback_analysis(summary_text):
    return {
        "recommendation": "mediar",
        "confidence": "baja",
        "affects_reputation": True,
        "financial_impact": {"refund_cost": 0, "recovered_commission": 0, "net_loss": 0},
        "pros": ["No se pudo analizar automaticamente"],
        "cons": ["Revisa manualmente el reclamo"],
        "summary": summary_text or "Error al analizar el reclamo",
    }
