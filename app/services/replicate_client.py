"""
Replicate Client — FLUX Image + Video Generation
=================================================
Genera imágenes de producto usando FLUX 1.1 Pro via Replicate API.
Genera video de producto usando minimax/video-01 via Replicate API.
Usa Prefer: wait para respuesta síncrona (evita polling).

Modelos:
  - black-forest-labs/flux-1.1-pro  (~$0.04/imagen, calidad superior)
  - minimax/video-01                 (~$0.50/video, 6s HD)
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_REPLICATE_KEY    = os.getenv("REPLICATE_API_KEY") or os.getenv("REPLICATE_API_TOKEN", "")
_FLUX_PRO_URL     = "https://api.replicate.com/v1/models/black-forest-labs/flux-1.1-pro/predictions"
_MINIMAX_URL      = "https://api.replicate.com/v1/models/minimax/video-01/predictions"

_HEADERS = {
    "Authorization": f"Bearer {_REPLICATE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "wait",   # Respuesta síncrona (hasta 60s)
}


def is_available() -> bool:
    return bool(_REPLICATE_KEY and _REPLICATE_KEY.startswith("r8_"))


# ─── Category-specific prompt builders ─────────────────────────────────────────

def _build_tv_prompts(brand: str, model: str, size: str, title: str) -> list[str]:
    """8 prompts de fotografía comercial para televisores.
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
        # 5. Vista cenital / overhead — ángulo superior premium
        (
            f"Commercial product photograph of a {product} from a dramatic overhead top-down angle, "
            "centered on pure white seamless background. "
            "The screen glows with deep blue cosmic nebula 4K content, creating beautiful upward light. "
            "Premium brushed aluminum stand perfectly centered, ultra-thin bezel from above. "
            "Soft studio lighting from all four sides, no harsh shadows. "
            "8K advertising photography, razor-sharp, photorealistic, award-winning composition."
        ),
        # 6. Close-up panel OLED — calidad de imagen extrema
        (
            f"Extreme close-up macro photograph of a {brand} {size_s}4K Smart TV screen surface. "
            "Showing micro-pixel precision: individual OLED sub-pixels visible in a gradient from "
            "deep rich black to brilliant white, demonstrating perfect contrast ratio. "
            "A section displays vibrant HDR10+ content — deep ocean blue and vivid coral orange. "
            "Ultra-shallow depth of field, soft bokeh background, studio macro lighting. "
            "8K resolution, photorealistic, premium technology feel."
        ),
        # 7. Lifestyle — dormitorio o home office moderno
        (
            f"Photorealistic lifestyle photograph of a {product} mounted on a clean white bedroom wall, "
            "serving as both TV and monitor in a modern minimalist home office. "
            "Scandinavian interior design: light oak desk, ergonomic chair, warm Edison pendant light. "
            "The screen shows a colorful productivity workspace with multiple windows. "
            "Daylight through sheer white curtains, plants on windowsill, award-winning interior photography. "
            "8K photorealistic, magazine editorial quality."
        ),
        # 8. Setup completo — TV con accesorios y control remoto
        (
            f"Professional product photography of a complete {product} home entertainment setup. "
            "TV centered on pure white background with: premium soundbar below, "
            f"sleek {brand} voice remote control placed elegantly in front, "
            "HDMI cable neatly connected, streaming device visible. "
            "The screen displays a vibrant movie title card in brilliant 4K HDR. "
            "Soft diffused studio lighting, slight 3/4 angle, all accessories in sharp focus. "
            "8K commercial photography, photorealistic, retail-ready image."
        ),
    ]


def _build_generic_prompts(brand: str, model: str, category: str, title: str) -> list[str]:
    """8 prompts genéricos para productos no-TV."""
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
        (
            f"Top-down overhead commercial product photograph of {product}, {cat}. "
            "Pure white seamless background, centered perfectly, all edges and surfaces visible. "
            "Soft even studio lighting from all sides, no harsh shadows. "
            "8K advertising photography, razor-sharp focus, photorealistic."
        ),
        (
            f"Professional product photograph of {product} from rear 3/4 angle showing back panel, "
            "ports, vents and build quality details. Dark charcoal matte background, "
            "directional studio lighting, shallow depth of field. "
            "8K photorealistic, tech review quality."
        ),
        (
            f"Minimalist editorial photograph of {product} on a clean marble surface. "
            "Sleek modern composition, natural daylight from left side window, "
            "soft long shadows, premium lifestyle brand aesthetic. "
            "8K photorealistic, Wallpaper magazine style."
        ),
        (
            f"Complete product setup photograph of {product} with all included accessories laid out flat. "
            "Pure white background, knolling-style top-down arrangement, "
            "all cables, manuals, remotes, and parts neatly organized. "
            "Studio lighting, 8K photorealistic, e-commerce product photography."
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
    count: int = 8,
) -> list[str]:
    """Retorna `count` prompts para generación por lote, adaptados a la categoría."""
    cat_lower = (category or "").lower()
    if "television" in cat_lower or "tv" in cat_lower or "televisor" in cat_lower:
        prompts = _build_tv_prompts(brand, model, size, title)
    else:
        prompts = _build_generic_prompts(brand, model, category, title)
    return prompts[:count]


def build_video_prompt(
    brand: str = "",
    model: str = "",
    title: str = "",
    category: str = "",
    size: str = "",
) -> str:
    """Construye prompt de video de producto según la categoría."""
    brand   = (brand or "Premium").strip()
    size_s  = f"{size}-inch " if size else ""
    model_s = (model or "").strip()
    cat_lower = (category or "").lower()

    if "television" in cat_lower or "tv" in cat_lower or "televisor" in cat_lower:
        product = f"{brand} {size_s}4K Smart TV" + (f" {model_s}" if model_s else "")
        return (
            f"Cinematic product showcase video of a {product}. "
            "The camera slowly orbits 180 degrees around the TV starting from a dramatic 3/4 left angle, "
            "moving to front-facing center, then to a 3/4 right angle. "
            "The screen displays stunning 4K HDR content: first a vibrant tropical ocean scene, "
            "then transitions to a space nebula, then a city skyline at night — "
            "showing perfect OLED contrast and color accuracy throughout. "
            "Pure white studio background with subtle floor reflection, "
            "professional broadcast-quality lighting that catches the ultra-thin bezels. "
            "Smooth, slow cinematic camera movement, photorealistic, commercial advertisement quality."
        )
    product = " ".join(filter(None, [brand, model_s])).strip() or title or "electronic product"
    return (
        f"Cinematic product showcase video of {product}. "
        "The camera slowly orbits around the product, starting from front, moving to 3/4 angle, "
        "then behind and back to front. "
        "Pure white studio background, professional diffused lighting, "
        "smooth cinematic camera movement. "
        "Commercial advertising quality, photorealistic."
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


async def generate_video(
    prompt: str,
    first_frame_image: str = "",   # optional URL of first frame
) -> str:
    """
    Genera un video de producto con minimax/video-01 y retorna la URL pública.
    Duración: ~6s, HD 720p. Tiempo estimado: 2-4 min.
    Lanza RuntimeError si falla.
    """
    import asyncio

    inp: dict = {
        "prompt": prompt,
        "prompt_optimizer": True,
    }
    if first_frame_image:
        inp["first_frame_image"] = first_frame_image

    payload = {"input": inp}

    # Video no soporta Prefer:wait — siempre async con polling
    headers_no_wait = {
        "Authorization": f"Bearer {_REPLICATE_KEY}",
        "Content-Type":  "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_MINIMAX_URL, json=payload, headers=headers_no_wait)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Replicate video error {resp.status_code}: {resp.text[:300]}")

        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Replicate video no retornó ID: {data}")

        logger.info(f"Replicate video prediction {pred_id} — polling...")

    # Polling con cliente nuevo (timeout más largo por request)
    poll_url     = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_headers = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(80):          # máx 80 × 6s = 480s (8 min)
            await asyncio.sleep(6)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            status = pd.get("status")
            if status == "succeeded":
                output = pd.get("output")
                if isinstance(output, list):
                    return output[0]
                if isinstance(output, str):
                    return output
                raise RuntimeError(f"Replicate video output inesperado: {output}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"Replicate video falló: {pd.get('error', 'unknown')}")

    raise RuntimeError("Replicate video timeout — video no generado en 8 min")
