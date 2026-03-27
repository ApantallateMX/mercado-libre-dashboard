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


# ─── Category-specific prompt builders ─────────────────────────────────────────

def _build_tv_prompts(brand: str, model: str, size: str, title: str) -> list[str]:
    """4 prompts de fotografía comercial para televisores.
    Diseñados para generar imágenes de calidad marketing que conviertan ventas en ML.
    """
    brand   = (brand or "Premium").strip()
    size_s  = f"{size}-inch " if size else ""
    model_s = model.strip() if model else ""
    product = f"{brand} {size_s}4K Smart TV" + (f" {model_s}" if model_s else "")

    return [
        # 1. Hero shot — portada principal: TV frontal sobre fondo blanco con contenido 4K vibrante
        (
            f"Hyper-realistic commercial product photograph of a {product}. "
            "Ultra-thin bezels, premium brushed aluminum stand, pristine condition. "
            f"The {size_s}screen displays a breathtaking 4K HDR scene: vibrant aerial view of a "
            "tropical turquoise ocean coastline with deep blacks and brilliant highlights, "
            "showcasing perfect OLED contrast. "
            "Pure white seamless background, centered front-facing composition, "
            "soft diffused professional studio lighting from both sides and top, "
            "slight downward camera angle. No reflections on background. "
            "8K commercial advertising photography, razor-sharp focus, photorealistic."
        ),
        # 2. Lifestyle — TV en sala moderna y elegante
        (
            f"Photorealistic interior design photograph of a luxury modern living room at dusk "
            f"with a large {product} as the centerpiece, mounted on a minimalist floating wood media console. "
            "Warm amber ambient lighting, floor-to-ceiling windows showing city skyline at night, "
            "premium light gray sectional sofa, designer coffee table, indoor plants. "
            f"The TV displays a stunning 4K nature documentary — lush emerald rainforest with "
            "golden sunlight filtering through canopy. "
            "Cinematic wide-angle architectural photography, f/8 aperture, golden hour interior, "
            "8K photorealistic render, magazine-quality."
        ),
        # 3. Perfil ultra-delgado — diseño premium
        (
            f"Professional product photography of a {product} from a dramatic 3/4 front-left angle, "
            "revealing its impossibly thin OLED panel profile — less than 5mm thick. "
            "The screen displays vivid red and orange abstract 4K art with deep black borders. "
            "Pure white background with subtle gradient, precision studio lighting casting soft shadow "
            "on the left to emphasize the ultra-slim depth. "
            "The premium stand and cable management system clearly visible. "
            "8K commercial photography, tack-sharp, photorealistic, luxury product feel."
        ),
        # 4. Panel de conectividad — puertos y características técnicas
        (
            f"Professional close-up macro photography of {brand} {model_s} TV rear connectivity panel. "
            "Clearly visible and labeled: 4x HDMI 2.1 ports (one with eARC label), "
            "3x USB-A ports, 1x optical digital audio S/PDIF output, "
            "1x LAN ethernet port, RF coaxial antenna input, headphone jack. "
            "Soft directional studio lighting on dark charcoal matte background, "
            "shallow depth of field with all ports in sharp focus, "
            "port labels crisp and readable. "
            "Product detail photography, 8K, photorealistic, tech review quality."
        ),
    ]


def _build_generic_prompts(brand: str, model: str, category: str, title: str) -> list[str]:
    """Prompts genéricos para productos no-TV."""
    product = " ".join(filter(None, [brand, model])).strip() or title or "electronic product"
    cat     = category or "consumer electronics"

    return [
        (
            f"Professional commercial product photography of {product}, {cat}. "
            "Pure white seamless background, soft diffused studio lighting from both sides. "
            "Front-facing centered composition, 8K resolution, razor-sharp focus, photorealistic."
        ),
        (
            f"Photorealistic product photograph of {product} from a 3/4 front-left angle. "
            "Pure white background, premium studio lighting, deep shadows for depth. "
            "8K commercial photography, tack-sharp, luxury product feel."
        ),
        (
            f"Lifestyle photograph of {product} in an elegant modern home setting. "
            "Warm ambient lighting, minimalist interior design, premium furniture. "
            "The product is the hero of the scene. 8K photorealistic, magazine-quality."
        ),
        (
            f"Professional close-up detail photography of {product} showing ports, buttons and build quality. "
            "Soft macro studio lighting, dark matte background, shallow depth of field. "
            "8K photorealistic, tech review quality."
        ),
    ]


def build_product_prompt(
    brand: str = "",
    model: str = "",
    title: str = "",
    category: str = "",
    size: str = "",
) -> str:
    """Construye el prompt principal (imagen #1) según la categoría del producto."""
    cat_lower = (category or "").lower()
    if "television" in cat_lower or "tv" in cat_lower or "televisor" in cat_lower:
        return _build_tv_prompts(brand, model, size, title)[0]
    product = " ".join(filter(None, [brand, model])).strip() or title or "electronic product"
    cat_hint = f", {category}" if category else ""
    return (
        f"Professional commercial product photography of {product}{cat_hint}. "
        "Pure white seamless background, soft diffused studio lighting, "
        "front-facing centered composition, 8K resolution, razor-sharp focus, photorealistic."
    )


def build_batch_prompts(
    brand: str = "",
    model: str = "",
    title: str = "",
    category: str = "",
    size: str = "",
    count: int = 4,
) -> list[str]:
    """Retorna `count` prompts para generación por lote, adaptados a la categoría."""
    cat_lower = (category or "").lower()
    if "television" in cat_lower or "tv" in cat_lower or "televisor" in cat_lower:
        prompts = _build_tv_prompts(brand, model, size, title)
    else:
        prompts = _build_generic_prompts(brand, model, category, title)
    return prompts[:count]


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
            "prompt":            prompt,
            "aspect_ratio":      aspect_ratio,
            "output_format":     "webp",
            "output_quality":    90,
            "safety_tolerance":  5,
            "prompt_upsampling": True,
        }
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
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
        for _ in range(60):           # máx 60 intentos × 3s = 180s
            await asyncio.sleep(3)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            if pd.get("status") == "succeeded":
                output = pd.get("output", [])
                return output[0] if isinstance(output, list) else output
            if pd.get("status") in ("failed", "canceled"):
                raise RuntimeError(f"Replicate falló: {pd.get('error', 'unknown')}")

        raise RuntimeError("Replicate timeout — imagen no generada en 180s")
