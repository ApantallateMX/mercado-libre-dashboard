"""
Replicate Client — FLUX Image Generation
=========================================
Genera imágenes de producto usando FLUX 1.1 Pro via Replicate API.
Usa Prefer: wait para respuesta síncrona (evita polling).

Modelo: black-forest-labs/flux-1.1-pro  (~$0.04/imagen, calidad superior)
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_REPLICATE_KEY    = os.getenv("REPLICATE_API_KEY") or os.getenv("REPLICATE_API_TOKEN", "")
_FLUX_PRO_URL     = "https://api.replicate.com/v1/models/black-forest-labs/flux-1.1-pro/predictions"

_HEADERS = {
    "Authorization": f"Bearer {_REPLICATE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "wait",   # Respuesta síncrona (hasta 60s)
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
    use_dev: bool = False,   # kept for backwards compat, ignored
) -> str:
    """
    Genera una imagen con FLUX 1.1 Pro y retorna la URL pública.
    Lanza RuntimeError si falla.
    """
    payload = {
        "input": {
            "prompt":        prompt,
            "aspect_ratio":  aspect_ratio,
            "output_format": "webp",
            "output_quality": quality,
            "prompt_upsampling": True,   # FLUX 1.1 Pro feature — mejora coherencia
        }
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(_FLUX_PRO_URL, json=payload, headers=_HEADERS)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Replicate error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()

        # Respuesta síncrona (Prefer: wait)
        if data.get("status") == "succeeded":
            output = data.get("output", [])
            if output:
                return output[0] if isinstance(output, list) else output

        # Polling si todavía está procesando
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Replicate no retornó ID: {data}")

        logger.info(f"Replicate prediction {pred_id} — esperando resultado...")
        poll_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
        poll_headers = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

        import asyncio
        for _ in range(40):           # máx 40 intentos × 3s = 120s
            await asyncio.sleep(3)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            if pd.get("status") == "succeeded":
                output = pd.get("output", [])
                return output[0] if isinstance(output, list) else output
            if pd.get("status") in ("failed", "canceled"):
                raise RuntimeError(f"Replicate falló: {pd.get('error', 'unknown')}")

        raise RuntimeError("Replicate timeout — imagen no generada en 120s")
