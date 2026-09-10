"""mattermost_bot.py — Cliente genérico de Mattermost (API real /api/v4) para que
el dashboard (o Claude, vía los endpoints /api/diag/mattermost-*) responda con
identidad propia y en hilo real, en vez de depender del MCP compartido (que no
soporta hilos) o de la sesión de navegador de un usuario.

FEATURE 2026-09-10 (pedido explícito de Jovan): usar por ahora el bot que ya
existe (@ecomops-agent, MM_BOT_TOKEN/MM_URL, mismas variables que
marketplace_alerts.py) y poder cambiar a un bot nuevo (@conmify-agent, pedido
en #support-mattermost-manager el mismo día) sin tocar código -- basta con
setear MM_DASHBOARD_BOT_TOKEN en Railway cuando llegue el token nuevo; si no
está seteado, cae a MM_BOT_TOKEN (el compartido) automáticamente.
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

MM_URL = os.getenv("MM_URL", "")
MM_DASHBOARD_BOT_TOKEN = os.getenv("MM_DASHBOARD_BOT_TOKEN") or os.getenv("MM_BOT_TOKEN", "")
MM_TEAM_NAME = os.getenv("MM_TEAM_NAME", "mi-technologies")

_channel_id_cache: dict[str, str] = {}


_last_error: dict = {}


async def get_channel_id(channel_name: str, team_name: str = "") -> str:
    """Resuelve un nombre de canal (ej. 'requerimientos-dashboard') a su channel_id
    real de Mattermost. Cachea en memoria (los channel_id no cambian) para no
    pegarle a la API en cada mensaje. Retorna "" si no está configurado o falla
    -- nunca lanza. Guarda el detalle del último error en _last_error (debug)."""
    global _last_error
    if not MM_URL:
        _last_error = {"step": "config", "detail": "MM_URL vacío"}
        return ""
    if not MM_DASHBOARD_BOT_TOKEN:
        _last_error = {"step": "config", "detail": "MM_DASHBOARD_BOT_TOKEN/MM_BOT_TOKEN vacío"}
        return ""
    if channel_name in _channel_id_cache:
        return _channel_id_cache[channel_name]
    team = team_name or MM_TEAM_NAME
    url = f"{MM_URL}/api/v4/teams/name/{team}/channels/name/{channel_name}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {MM_DASHBOARD_BOT_TOKEN}"})
            if r.status_code != 200:
                _last_error = {"step": "get_channel_id", "url": url, "status": r.status_code, "body": r.text[:300]}
                logger.warning(f"[MattermostBot] get_channel_id({channel_name}) -> {r.status_code}: {r.text[:200]}")
                return ""
            channel_id = r.json().get("id", "")
            if channel_id:
                _channel_id_cache[channel_name] = channel_id
            return channel_id
    except Exception as e:
        _last_error = {"step": "get_channel_id", "url": url, "exception": str(e)}
        logger.warning(f"[MattermostBot] Error resolviendo channel_id de {channel_name}: {e}")
        return ""


async def post_message(channel_name: str, text: str, root_id: str = "") -> dict:
    """Publica un mensaje en un canal por NOMBRE (resuelve el channel_id solo).
    Si root_id viene lleno, el mensaje queda como respuesta EN HILO real de
    Mattermost (root_id = el "id" del post original, no el permalink).
    Retorna el post creado (incluye "id", útil como root_id para la siguiente
    respuesta del mismo hilo) o {} si falla -- nunca lanza."""
    if not (MM_URL and MM_DASHBOARD_BOT_TOKEN):
        logger.info(f"[MattermostBot] MM_URL/token no configurado -- mensaje no enviado: {text[:120]}")
        return {}
    global _last_error
    channel_id = await get_channel_id(channel_name)
    if not channel_id:
        logger.warning(f"[MattermostBot] No se pudo resolver channel_id de {channel_name}")
        return {}
    payload = {"channel_id": channel_id, "message": text}
    if root_id:
        payload["root_id"] = root_id
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{MM_URL}/api/v4/posts",
                headers={"Authorization": f"Bearer {MM_DASHBOARD_BOT_TOKEN}"},
                json=payload,
            )
            if r.status_code not in (200, 201):
                _last_error = {"step": "post_message", "status": r.status_code, "body": r.text[:300]}
                logger.warning(f"[MattermostBot] post_message -> {r.status_code}: {r.text[:200]}")
                return {}
            return r.json()
    except Exception as e:
        _last_error = {"step": "post_message", "exception": str(e)}
        logger.warning(f"[MattermostBot] Error posteando a {channel_name}: {e}")
        return {}


def get_last_error() -> dict:
    """Debug: detalle del último fallo de get_channel_id/post_message."""
    return _last_error


async def get_channel_posts(channel_name: str, limit: int = 20) -> list:
    """Trae los últimos posts REALES de un canal (con "id" real de Mattermost,
    a diferencia del MCP que solo da texto/autor sin id usable como root_id).
    Necesario para poder resolver a qué post_id responder en hilo cuando el
    mensaje original no vino de una respuesta nuestra (ej. Jovan inicia la
    conversación). Retorna lista de {id, user_id, message, create_at,
    root_id} ordenada más reciente primero, o [] si falla."""
    global _last_error
    if not (MM_URL and MM_DASHBOARD_BOT_TOKEN):
        return []
    channel_id = await get_channel_id(channel_name)
    if not channel_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{MM_URL}/api/v4/channels/{channel_id}/posts",
                headers={"Authorization": f"Bearer {MM_DASHBOARD_BOT_TOKEN}"},
                params={"per_page": limit},
            )
            if r.status_code != 200:
                _last_error = {"step": "get_channel_posts", "status": r.status_code, "body": r.text[:300]}
                return []
            data = r.json()
            order = data.get("order") or []
            posts = data.get("posts") or {}
            return [
                {
                    "id": pid,
                    "user_id": posts[pid].get("user_id"),
                    "message": posts[pid].get("message"),
                    "create_at": posts[pid].get("create_at"),
                    "root_id": posts[pid].get("root_id"),
                }
                for pid in order if pid in posts
            ]
    except Exception as e:
        _last_error = {"step": "get_channel_posts", "exception": str(e)}
        return []


async def get_username(user_id: str) -> str:
    """Resuelve un user_id de Mattermost a su username real."""
    if not (MM_URL and MM_DASHBOARD_BOT_TOKEN and user_id):
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{MM_URL}/api/v4/users/{user_id}",
                headers={"Authorization": f"Bearer {MM_DASHBOARD_BOT_TOKEN}"},
            )
            if r.status_code != 200:
                return ""
            return r.json().get("username", "")
    except Exception:
        return ""
