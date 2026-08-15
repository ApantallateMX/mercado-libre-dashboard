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


async def generate_image(
    prompt: str,
    image_reference_url: str = "",
    batch_size: int = 1,
    aspect_ratio: str = "1:1",
) -> str:
    """
    Genera imagen(es) de producto.

    FIX 2026-08-15 (pedido por Jovan: la foto generada podia verse
    "parecida" pero no identica al producto real -- riesgo de reclamos si
    el cliente recibe algo distinto a lo que ve en la foto): si se pasa
    image_reference_url, usa soul/reference (foto real como referencia
    OBLIGATORIA, hasta 4 imagenes por llamada via batch_size) en vez de
    soul/standard (solo texto, sin ninguna garantia de que se parezca al
    producto real). Sin referencia, cae al comportamiento anterior
    (soul/standard, batch_size fijo en 1 -- ese modelo no soporta batch).

    Retorna request_id (generación asíncrona).
    """
    if image_reference_url:
        payload = {
            "prompt": prompt,
            "image_reference_url": image_reference_url,
            "batch_size": min(max(batch_size, 1), 4),
            "aspect_ratio": aspect_ratio,
        }
        endpoint = "soul/reference"
    else:
        payload = {"prompt": prompt}
        endpoint = "soul/standard"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"{_BASE}/higgsfield-ai/{endpoint}",
            headers=_headers(),
            json=payload,
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
    Retorna dict con keys: status, result_url (primera imagen/video, para
    compatibilidad con el modo video de un solo resultado), result_urls
    (TODAS las imagenes -- FIX 2026-08-15: antes se descartaban todas
    menos la primera aunque batch_size pidiera varias, asi que aun
    pidiendo 4 con soul/reference solo se veia 1 en el dashboard), error.
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
    result_urls = []

    if status == "completed":
        # Imagen(es)
        images = data.get("images", [])
        if images:
            result_urls = [im.get("url") for im in images if im.get("url")]
            result_url = result_urls[0] if result_urls else None
        # Video
        videos = data.get("videos", [])
        if videos:
            result_url = videos[0].get("url")

    return {
        "status":      status,
        "result_url":  result_url,
        "result_urls": result_urls,
        "raw":         data,
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
    """Construye prompt para foto lifestyle de producto.

    FIX 2026-08-15: se usa junto con image_reference_url (soul/reference,
    ver generate_image) -- el prompt ahora exige explicitamente preservar
    la apariencia REAL del producto de la foto de referencia (color,
    forma, marca, proporciones) sin alterarla, para evitar que la IA
    genere algo "parecido" pero distinto al producto real que se vende
    (riesgo de reclamos si el cliente recibe algo distinto a la foto)."""
    base = (
        f"Professional lifestyle product photography of this exact {title}. "
        f"Keep the product's real appearance, color, shape, proportions and "
        f"branding completely unchanged and identical to the reference "
        f"image -- do not alter, redesign, or reinterpret the product. "
        f"Realistic setting, natural lighting, high detail, commercial quality"
    )
    if custom:
        base += f", {custom}"
    return base


def build_video_prompt(title: str, custom: str = "") -> str:
    """Construye prompt para animación de video de producto."""
    base = "Slow cinematic zoom in, studio product lighting, smooth motion"
    if custom:
        base = custom
    return base
