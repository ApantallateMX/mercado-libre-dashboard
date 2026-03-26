"""
Replicate Client — FLUX Image Generation
=========================================
Genera imágenes de producto usando FLUX.1 Schnell via Replicate API.
Usa Prefer: wait para respuesta síncrona (evita polling).

Modelo: black-forest-labs/flux-schnell  (~$0.003/imagen, ~5s)
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_REPLICATE_KEY   = os.getenv("REPLICATE_API_KEY") or os.getenv("REPLICATE_API_TOKEN", "")
_FLUX_SCHNELL_URL = "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"
_FLUX_DEV_URL     = "https://api.replicate.com/v1/models/black-forest-labs/flux-dev/predictions"

_HEADERS = {
    "Authorization":  f"Bearer {_REPLICATE_KEY}",
    "Content-Type":   "application/json",
    "Prefer":         "wait",          # Respuesta síncrona (hasta 60s)
}


def is_available() -> bool:
    return bool(_REPLICATE_KEY and _REPLICATE_KEY.startswith("r8_"))


def build_product_prompt(
    brand: str = "",
    model: str = "",
    title: str = "",
    category: str = "",
) -> str:
    """Construye un prompt de fotografía de producto para FLUX."""
    product = " ".join(filter(None, [brand, model])).strip() or title or "electronic product"
    cat_hint = f", {category}" if category else ""
    return (
        f"Professional product photography of {product}{cat_hint}. "
        "Studio lighting, pure white background, sharp focus, high resolution, "
        "commercial product photo, 4K, no shadows, centered composition, "
        "retail ready image."
    )


async def generate_image(
    prompt: str,
    aspect_ratio: str = "1:1",
    quality: int = 90,
    use_dev: bool = False,
) -> str:
    """
    Genera una imagen con FLUX Schnell y retorna la URL pública.
    Lanza RuntimeError si falla.
    """
    url = _FLUX_DEV_URL if use_dev else _FLUX_SCHNELL_URL
    payload = {
        "input": {
            "prompt":        prompt,
            "num_outputs":   1,
            "aspect_ratio":  aspect_ratio,
            "output_format": "webp",
            "output_quality": quality,
        }
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, json=payload, headers=_HEADERS)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Replicate error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()

        # Si la respuesta es síncrona (Prefer: wait) viene con output directo
        if data.get("status") == "succeeded":
            output = data.get("output", [])
            if output:
                return output[0] if isinstance(output, list) else output

        # Si aún está en proceso — hacer polling manual
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Replicate no retornó ID: {data}")

        logger.info(f"Replicate prediction {pred_id} — esperando resultado...")
        poll_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
        poll_headers = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

        import asyncio
        for _ in range(30):           # máx 30 intentos × 2s = 60s
            await asyncio.sleep(2)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            if pd.get("status") == "succeeded":
                output = pd.get("output", [])
                return output[0] if isinstance(output, list) else output
            if pd.get("status") in ("failed", "canceled"):
                raise RuntimeError(f"Replicate falló: {pd.get('error', 'unknown')}")

        raise RuntimeError("Replicate timeout — imagen no generada en 60s")
