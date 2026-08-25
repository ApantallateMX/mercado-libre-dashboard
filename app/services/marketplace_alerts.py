"""marketplace_alerts.py — Alertas automáticas del dashboard hacia el equipo
responsable de cada cuenta/área, vía Mattermost (#alertas-marketplace).

FEATURE 2026-08-25 (pedido explícito de Jovan): Vianey (área, recibe TODO),
Arely (Amazon Vektor), Vanessa (Mercado Libre, todas las cuentas), Adrian
Espino (Amazon Exclusive USA), Alejandro Torres + Said (salud/reputación).
Piloto #1: solo reputación ML (verde/amarillo/rojo) -- mensajes sin
responder y órdenes con pérdida se agregan después ("poco a poco", palabras
de Jovan), no en la primera versión.

Requiere las variables de entorno MM_BOT_TOKEN / MM_CHANNEL_ID / MM_URL
(bot @ecomops-agent, canal #alertas-marketplace, provisionado por
mattermost-manager-agent el 2026-08-25). Si faltan, las funciones de este
módulo son no-op silenciosas -- nunca deben tumbar el resto de la app.
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

MM_URL = os.getenv("MM_URL", "")
MM_BOT_TOKEN = os.getenv("MM_BOT_TOKEN", "")
MM_CHANNEL_ID = os.getenv("MM_CHANNEL_ID", "")

AREA_LEAD = "@vianey.ramirez"
HEALTH_TEAM = ["@alejandro.torres", "@said.ramirez"]

# user_id ML / seller_id Amazon -> dueño directo de la cuenta.
# AUTOBOT AMZ MX es de Vianey directamente (ya está en AREA_LEAD, no se
# duplica aquí para no mandarle el mismo aviso dos veces).
ACCOUNT_OWNERS: dict[str, str] = {
    "523916436": "@vanessa.espino",    # APANTALLATEMX (ML)
    "292395685": "@vanessa.espino",    # AUTOBOT (ML)
    "391393176": "@vanessa.espino",    # BLOWTECHNOLOGIES (ML)
    "515061615": "@vanessa.espino",    # LUTEMAMEXICO (ML)
    "A20NFIUQNEYZ1E": "@arely.rodriguez",   # VECKTOR IMPORTS (Amazon)
    "A22XNR713HGDVG": "@adrian.espino",     # ExclusiveBulbs (Amazon, USA)
}

# ML level_id real (5_green/4_yellow/3_orange/2_orange/1_red) -> semáforo de
# 3 colores que pidió Jovan. orange se trata como advertencia intermedia,
# igual que yellow -- Jovan solo pidió verde/amarillo/rojo, no 5 niveles.
_LEVEL_TO_COLOR = {
    "5_green": "verde",
    "4_yellow": "amarillo",
    "3_orange": "amarillo",
    "2_orange": "amarillo",
    "1_red": "rojo",
}
_COLOR_EMOJI = {"verde": "🟢", "amarillo": "🟡", "rojo": "🔴", "desconocido": "⚪"}


def level_id_to_color(level_id: str) -> str:
    return _LEVEL_TO_COLOR.get(level_id or "", "desconocido")


async def post_marketplace_alert(text: str) -> bool:
    """POST puro a Mattermost -- nunca lanza, solo loguea si falla. No-op
    silencioso si el bot no está configurado (env vars ausentes)."""
    if not (MM_URL and MM_BOT_TOKEN and MM_CHANNEL_ID):
        logger.info("[MarketplaceAlerts] MM_* no configurado -- alerta no enviada: %s", text[:120])
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{MM_URL}/api/v4/posts",
                headers={"Authorization": f"Bearer {MM_BOT_TOKEN}"},
                json={"channel_id": MM_CHANNEL_ID, "message": text},
            )
            if r.status_code not in (200, 201):
                logger.warning("[MarketplaceAlerts] Mattermost respondió %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:
        logger.warning(f"[MarketplaceAlerts] Error posteando a Mattermost: {e}")
        return False


async def notify_reputation_change(account_id: str, nickname: str, old_color: str, new_color: str) -> None:
    owner = ACCOUNT_OWNERS.get(account_id)
    mentions = [AREA_LEAD] + HEALTH_TEAM
    if owner and owner not in mentions:
        mentions.append(owner)
    emoji_old, emoji_new = _COLOR_EMOJI.get(old_color, "⚪"), _COLOR_EMOJI.get(new_color, "⚪")
    text = (
        f"{' '.join(mentions)} — **reputación de {nickname}** cambió: "
        f"{emoji_old} {old_color} → {emoji_new} **{new_color}**\n"
        f"Revisar en el dashboard: pestaña Salud, cuenta {nickname}."
    )
    await post_marketplace_alert(text)
