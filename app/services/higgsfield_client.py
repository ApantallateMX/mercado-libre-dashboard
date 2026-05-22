"""
higgsfield_client.py — Cliente async para Higgsfield AI (generación de imagen/video)

BASE URL: https://platform.higgsfield.ai
AUTH:     Authorization: Key {KEY_ID}:{SECRET}

Modelos principales:
  soul/standard          → texto → imagen  (~8 créditos)
  higgsfield-ai/dop/lite → imagen → video 5s (~6 créditos)
"""

import asyncio
import logging
import httpx

from app.config import HIGGSFIELD_KEY_ID, HIGGSFIELD_SECRET

logger = logging.getLogger(__name__)

_BASE = "https://platform.higgsfield.ai"
_TIMEOUT = 30.0
_POLL_DELAY = 3.0
_POLL_MAX   = 60   # máx 60 intentos = 3 min


def _auth() -> str:
    return f"Key {HIGGSFIELD_KEY_ID}:{HIGGSFIELD_SECRET}"


def _headers() -> dict:
    return {
        "Authorization": _auth(),
        "Content-Type":  "application/json",
    }


async def check_credits() -> bool:
    """Devuelve True si hay créditos disponibles (hace un submit real y detecta not_enough_credits)."""
    if not HIGGSFIELD_KEY_ID or not HIGGSFIELD_SECRET:
        return False
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            r = await client.post(
                f"{_BASE}/higgsfield-ai/soul/standard",
                headers=_headers(),
                json={"prompt": "__credit_check__"},
            )
            data = r.json()
            # Si tiene request_id → créditos OK (cancelamos inmediatamente)
            if "request_id" in data:
                rid = data["request_id"]
                asyncio.create_task(_cancel(rid))
                return True
            # not_enough_credits → sin créditos
            return data.get("detail") != "not_enough_credits"
        except Exception:
            return False


async def _cancel(request_id: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(f"{_BASE}/requests/{request_id}/cancel", headers=_headers())
        except Exception:
            pass


async def generate_image(prompt: str, aspect_ratio: str = "1:1") -> str:
    """
    Texto → imagen con soul/standard.
    Retorna request_id (generación asíncrona).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"{_BASE}/higgsfield-ai/soul/standard",
            headers=_headers(),
            json={"prompt": prompt},
        )
        r.raise_for_status()
        data = r.json()
        if "detail" in data:
            raise ValueError(data["detail"])
        return data["request_id"]


async def generate_video(image_url: str, prompt: str) -> str:
    """
    Imagen → video 5s con dop/lite.
    Retorna request_id.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"{_BASE}/higgsfield-ai/dop/lite",
            headers=_headers(),
            json={"prompt": prompt, "image_url": image_url},
        )
        r.raise_for_status()
        data = r.json()
        if "detail" in data:
            raise ValueError(data["detail"])
        return data["request_id"]


async def get_status(request_id: str) -> dict:
    """
    Consulta el estado de una generación.
    Retorna dict con keys: status, result_url (None si aún no termina), error.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{_BASE}/requests/{request_id}/status",
            headers=_headers(),
        )
        r.raise_for_status()
        data = r.json()

    status = data.get("status", "unknown")
    result_url = None

    if status == "completed":
        # Imagen
        images = data.get("images", [])
        if images:
            result_url = images[0].get("url")
        # Video
        videos = data.get("videos", [])
        if videos:
            result_url = videos[0].get("url")

    return {
        "status":     status,
        "result_url": result_url,
        "raw":        data,
    }


async def upload_from_url(image_url: str) -> str:
    """
    Descarga una imagen desde image_url y la sube al CDN de Higgsfield.
    Retorna public_url (usable como input para generate_video).
    """
    async with httpx.AsyncClient(timeout=30) as client:
        # Descargar imagen
        img_r = await client.get(image_url, follow_redirects=True)
        img_r.raise_for_status()
        img_bytes = img_r.content
        content_type = img_r.headers.get("content-type", "image/jpeg").split(";")[0]

        # Pedir URL pre-firmada de Higgsfield
        r = await client.post(
            f"{_BASE}/files/generate-upload-url",
            headers=_headers(),
            json={"content_type": content_type},
        )
        r.raise_for_status()
        urls = r.json()
        public_url = urls["public_url"]
        upload_url  = urls["upload_url"]

        # Subir al S3 de Higgsfield
        await client.put(
            upload_url,
            content=img_bytes,
            headers={"Content-Type": content_type},
        )

    return public_url


def build_image_prompt(title: str, custom: str = "") -> str:
    """Construye prompt optimizado para foto de producto a partir del título del listing."""
    base = f"Professional studio product photo, white background, soft even lighting, {title}"
    if custom:
        base += f", {custom}"
    return base


def build_video_prompt(title: str, custom: str = "") -> str:
    """Construye prompt para animación de video de producto."""
    base = "Slow cinematic zoom in, studio product lighting, smooth motion"
    if custom:
        base = custom
    return base
