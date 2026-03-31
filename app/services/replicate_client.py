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
_KONTEXT_URL      = "https://api.replicate.com/v1/models/black-forest-labs/flux-kontext-pro/predictions"
_MINIMAX_URL      = "https://api.replicate.com/v1/models/minimax/video-01/predictions"
_MINIMAX_LIVE_URL = "https://api.replicate.com/v1/models/minimax/video-01-live/predictions"
# Image-to-video models (ordered by preference)
_WAN_I2V_URL      = "https://api.replicate.com/v1/models/wavespeedai/wan-2.1-i2v-480p/predictions"
_SVD_XT_URL       = "https://api.replicate.com/v1/models/lucataco/stable-video-diffusion-img2vid-xt-1-1/predictions"

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
    Cada prompt inicia con descripción visual inequívoca: pantalla plana rectangular grande.
    """
    brand   = (brand or "Premium").strip()
    size_s  = f"{size}-inch " if size else ""
    model_s = model.strip() if model else ""
    # Descripción base inequívoca — siempre incluye "flat-screen television" para que FLUX
    # nunca confunda el producto con una cámara, monitor, o cualquier otro dispositivo Sony/LG/Samsung
    base    = f"{brand} {size_s}flat-screen television" + (f" {model_s}" if model_s else "")

    return [
        # 1. Hero shot — portada principal
        (
            f"Hyper-realistic commercial product photograph of a {base}. "
            f"Large {size_s}rectangular flat panel TV display facing forward, ultra-thin bezels, "
            "premium aluminum stand, pristine new condition. "
            "The screen shows a stunning 4K HDR scene: vibrant tropical turquoise ocean coastline, "
            "deep blacks and brilliant highlights. "
            "Pure white seamless studio background, centered front-facing composition, "
            "soft diffused professional lighting from both sides. "
            "8K commercial advertising photography, razor-sharp focus, photorealistic."
        ),
        # 2. Lifestyle — sala de lujo al anochecer
        (
            f"Photorealistic interior design photograph of a luxury modern living room at dusk, "
            f"with a large {base} as the centerpiece, wall-mounted on a minimalist floating media console. "
            "Warm amber ambient lighting, floor-to-ceiling windows, city skyline at night, "
            "premium light gray sectional sofa, designer coffee table, indoor plants. "
            f"The flat-screen TV displays a stunning 4K nature documentary — lush emerald rainforest. "
            "Wide-angle architectural photography, f/8 aperture, 8K photorealistic, magazine-quality."
        ),
        # 3. Perfil 3/4 — grosor ultra-delgado
        (
            f"Professional product photography of a {base} from a dramatic 3/4 front-left angle. "
            f"Large flat-panel television screen, {size_s}rectangular display, ultra-thin side profile. "
            "Screen shows vivid red and orange abstract 4K art with deep black borders. "
            "Pure white background with subtle gradient, precision studio lighting casting soft shadow "
            "on the left to emphasize the ultra-slim panel depth. "
            "8K commercial photography, tack-sharp, photorealistic, luxury product feel."
        ),
        # 4. Panel trasero — puertos de conectividad
        (
            f"Professional close-up macro photography of the rear connectivity panel of a {base}. "
            "Flat television back panel showing clearly labeled ports: "
            "4x HDMI 2.1 ports (one with eARC label), 3x USB-A ports, "
            "1x optical audio output, 1x LAN ethernet, RF coaxial antenna input. "
            "Soft directional studio lighting on dark charcoal matte background, "
            "shallow depth of field, all ports in sharp focus. "
            "Product detail photography, 8K, photorealistic, tech review quality."
        ),
        # 5. Vista cenital / overhead
        (
            f"Commercial product photograph of a {base} from a dramatic overhead top-down angle. "
            f"Large rectangular flat-panel TV screen visible from above, {size_s}display glowing "
            "with deep blue cosmic nebula 4K content. "
            "Premium aluminum stand centered, ultra-thin bezel from above view. "
            "Pure white seamless background, soft studio lighting from all sides. "
            "8K advertising photography, razor-sharp, photorealistic, award-winning composition."
        ),
        # 6. Close-up pantalla — calidad de imagen OLED/QLED
        (
            f"Extreme close-up macro photograph of the {base} screen surface. "
            f"Large flat television display showing micro-pixel precision: "
            "vibrant HDR10+ content — deep ocean blue transitioning to vivid coral orange, "
            "perfect contrast from deep black to brilliant white. "
            "Ultra-shallow depth of field, soft bokeh background, studio macro lighting. "
            "8K resolution, photorealistic, premium display technology."
        ),
        # 7. Lifestyle — dormitorio/home office escandinavo
        (
            f"Photorealistic lifestyle photograph of a {base} mounted on a clean white bedroom wall. "
            f"Large flat-screen television as the focal point of a modern minimalist Scandinavian home office. "
            "Light oak desk, ergonomic chair, warm Edison pendant light, plants on windowsill. "
            "The TV screen shows a colorful 4K movie scene. "
            "Daylight through sheer white curtains, 8K photorealistic, magazine editorial quality."
        ),
        # 8. Setup completo — TV con soundbar y accesorios
        (
            f"Professional product photography of a complete {base} home theater setup. "
            f"Large flat-screen television centered on pure white background, "
            f"premium soundbar positioned below the TV, "
            f"sleek {brand} remote control placed elegantly in front, "
            "HDMI cable connected at the back. "
            "Screen displays a vibrant 4K movie scene. "
            "Soft diffused studio lighting, slight 3/4 angle, all accessories in sharp focus. "
            "8K commercial photography, photorealistic, retail-ready."
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


def _build_tv_kontext_prompts(size: str) -> list[str]:
    """7 prompts lifestyle para FLUX Kontext (img2img) — escenas lujosas y hermosas.
    Se usan cuando hay una imagen de referencia del producto real.
    El prompt describe la ESCENA; el TV viene de la imagen de referencia.
    """
    size_s = f"{size}-inch " if size else ""
    tv     = f"this {size_s}flat-screen television"

    return [
        # 1. Penthouse dorado — sala de lujo al atardecer
        (
            f"Place {tv} wall-mounted in a breathtaking luxury penthouse living room. "
            "Golden sunset light pours through panoramic floor-to-ceiling windows overlooking a Mediterranean coastline. "
            "Floating marble fireplace below the TV, cream bouclé sectional sofa, "
            "oversized abstract oil painting, fresh white orchids, Murano glass pendant lights. "
            "The screen glows with stunning 4K HDR content. "
            "Architectural Digest editorial photography, cinematic wide angle, warm golden hour, 8K photorealistic."
        ),
        # 2. Home theater oscuro dramático
        (
            f"Show {tv} as the glowing centerpiece of a sophisticated private home theater. "
            "Deep charcoal acoustic wall panels, three rows of premium dark velvet recliners, "
            "warm amber LED strip lighting casting a cinematic halo around the screen. "
            "Popcorn and drinks on side tables, movie credits rolling on screen. "
            "Dramatic moody cinema atmosphere, professional interior photography, 8K photorealistic."
        ),
        # 3. Sala escandinava — mañana luminosa
        (
            f"Place {tv} in a serene Scandinavian minimalist living room on a bright morning. "
            "Pale birch wood floors, warm white walls, a single large fiddle-leaf fig plant, "
            "natural linen sofa with cream throw, a steaming cup of coffee on oak side table. "
            "Soft diffused morning light through sheer curtains creates an ethereal glow. "
            "Kinfolk magazine editorial photography, calm and beautiful, 8K photorealistic."
        ),
        # 4. Suite de hotel boutique de lujo
        (
            f"Show {tv} wall-mounted in an ultra-luxurious boutique hotel suite bedroom. "
            "King bed with 1000-thread-count ivory linen, silk throw pillows in champagne gold, "
            "warm bedside lamps, sheer curtains with a twinkling city skyline at night beyond. "
            "Fresh roses in crystal vase on the dresser, soft romantic atmosphere. "
            "Luxury hotel editorial photography, intimate warm glow, 8K photorealistic."
        ),
        # 5. Terraza rooftop tropical — sunset
        (
            f"Place {tv} in an outdoor luxury rooftop terrace entertainment area at sunset. "
            "Dramatic fiery orange-pink sky over a tropical ocean panorama in the background. "
            "Teak outdoor furniture with thick white cushions, woven string lights overhead, "
            "outdoor fire pit glowing warmly, tropical plants framing the scene. "
            "The screen shows vivid sports content. "
            "Luxury resort lifestyle photography, golden sunset, 8K photorealistic."
        ),
        # 6. Home office ejecutivo premium
        (
            f"Show {tv} as the main display in an exceptional executive home office. "
            "Floating walnut desk with designer lamp, glass walls revealing rain on a dramatic city skyline. "
            "Custom built-in shelves with curated books and art objects, "
            "premium leather ergonomic chair, subtle indoor plants adding life. "
            "Architectural Digest productivity aesthetic, cool rainy daylight, 8K photorealistic."
        ),
        # 7. Sala familiar acogedora — noche mágica
        (
            f"Place {tv} in a warm and inviting family living room on a cozy winter evening. "
            "Crackling fireplace visible to the side casting warm dancing light, "
            "built-in bookshelves filled with books and framed photos flanking the TV, "
            "deep navy velvet sectional with colorful knit blankets and throw pillows. "
            "The screen shows a beloved animated movie, children's laughter implied. "
            "Cozy warm lifestyle photography, magical family atmosphere, 8K photorealistic."
        ),
    ]


def build_batch_prompts(
    brand: str = "",
    model: str = "",
    title: str = "",
    category: str = "",
    size: str = "",
    count: int = 8,
    use_kontext: bool = False,
) -> list[str]:
    """Retorna `count` prompts para generación por lote, adaptados a la categoría.
    Si use_kontext=True y es TV: retorna 7 prompts lifestyle Kontext + 1 técnico texto puro.
    """
    cat_lower = (category or "").lower()
    is_tv = "television" in cat_lower or "tv" in cat_lower or "televisor" in cat_lower

    if is_tv and use_kontext:
        # 7 lifestyle (Kontext img2img) + 1 técnico (texto puro)
        brand_s = (brand or "Premium").strip()
        model_s = (model or "").strip()
        lifestyle = _build_tv_kontext_prompts(size)
        tech_ports = (
            f"Professional close-up macro photography of the rear connectivity panel of a {brand_s} flat-screen television"
            + (f" {model_s}" if model_s else "") + ". "
            "Clearly labeled ports: 4x HDMI 2.1 (one eARC), 3x USB-A, optical audio, ethernet LAN, RF coaxial. "
            "Dark charcoal matte background, directional studio lighting, all ports in sharp focus. "
            "8K photorealistic, tech review quality."
        )
        prompts = lifestyle[:7] + [tech_ports]
        return prompts[:count]

    if is_tv:
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
        base = f"{brand} {size_s}flat-screen television" + (f" {model_s}" if model_s else "")
        return (
            f"Cinematic commercial product video of a {base}. "
            f"A large {size_s}rectangular flat-panel TV display on a premium aluminum stand. "
            "The camera starts at a dramatic 3/4 left angle and slowly orbits 180 degrees "
            "to front-facing center, then continues to a 3/4 right angle, "
            "revealing the ultra-thin side profile of the flat television panel. "
            "The screen displays stunning 4K HDR content throughout: "
            "vibrant tropical ocean coastline transitioning to a glowing space nebula. "
            "Pure white studio background with subtle floor reflection, "
            "professional broadcast-quality lighting. "
            "Smooth slow-motion cinematic camera movement, photorealistic, TV commercial quality."
        )
    product = " ".join(filter(None, [brand, model_s])).strip() or title or "consumer electronics product"
    return (
        f"Cinematic commercial product video of {product}, consumer electronics. "
        "The camera slowly orbits 180 degrees around the product, "
        "starting from front-facing center, moving to 3/4 angle, revealing depth and build quality. "
        "Pure white studio background, professional diffused lighting, "
        "smooth slow cinematic movement. "
        "Commercial advertising quality, photorealistic."
    )


async def submit_image_job(
    prompt: str,
    aspect_ratio: str = "1:1",
    input_image: str = "",   # si se provee → Kontext img2img
) -> dict:
    """Envía un job de generación de imagen a Replicate y retorna pred_id inmediatamente.
    Nunca usa Prefer:wait — siempre async para evitar timeout de Railway (60s Nginx).
    Retry automático con backoff exponencial en caso de 429 (rate limit).
    Retorna {"pred_id": str, "image_url": None} o {"pred_id": None, "image_url": str} si ya resolvió.
    """
    import asyncio

    headers_async = {
        "Authorization": f"Bearer {_REPLICATE_KEY}",
        "Content-Type":  "application/json",
    }

    if input_image:
        url = _KONTEXT_URL
        # Kontext Pro: solo los campos documentados — output_format/quality/safety no son soportados
        payload = {
            "input": {
                "prompt":       prompt,
                "input_image":  input_image,
                "aspect_ratio": aspect_ratio,
            }
        }
    else:
        url = _FLUX_PRO_URL
        # FLUX 1.1 Pro: solo prompt + aspect_ratio — los campos opcionales varían por versión
        # y suelen causar 422 si el schema cambió
        payload = {
            "input": {
                "prompt":       prompt,
                "aspect_ratio": aspect_ratio,
            }
        }

    # Retry con backoff exponencial para 429 (rate limit de Replicate)
    max_retries = 6
    for attempt in range(max_retries):
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers_async)

        if resp.status_code == 429:
            wait = min(10 * (2 ** attempt), 120)   # 10s, 20s, 40s, 80s, 120s max
            logger.warning(f"Replicate 429 rate limit — esperando {wait}s (intento {attempt+1}/{max_retries})")
            await asyncio.sleep(wait)
            continue

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Replicate submit {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        logger.info(f"Replicate submit → id={data.get('id')} status={data.get('status')}")

        # Si ya resolvió de forma síncrona (raro sin Prefer:wait, pero puede pasar)
        if data.get("status") == "succeeded":
            output = data.get("output", [])
            image_url = output[0] if isinstance(output, list) else output
            return {"pred_id": None, "image_url": image_url}

        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Replicate no retornó ID: {data}")

        return {"pred_id": pred_id, "image_url": None}

    raise RuntimeError("Replicate rate limit — demasiadas solicitudes. Espera 2 minutos y reintenta.")


async def check_prediction(pred_id: str) -> dict:
    """Consulta el estado de una predicción de Replicate.
    Retorna {"status": "processing"|"succeeded"|"failed", "image_url": str|None, "error": str|None}.
    """
    poll_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
    headers  = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(poll_url, headers=headers)
        data   = resp.json()
        status = data.get("status", "processing")

        if status == "succeeded":
            output    = data.get("output", [])
            image_url = output[0] if isinstance(output, list) else output
            return {"status": "succeeded", "image_url": image_url, "error": None}

        if status in ("failed", "canceled"):
            return {"status": "failed", "image_url": None, "error": data.get("error", "Prediction failed")}

        return {"status": status, "image_url": None, "error": None}


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


async def generate_image_with_reference(
    prompt: str,
    input_image: str,
    aspect_ratio: str = "1:1",
) -> str:
    """
    Genera una imagen de lifestyle usando FLUX Kontext Pro (img2img).
    Mantiene la apariencia visual del producto en input_image mientras aplica el prompt de escena.
    Lanza RuntimeError si falla.
    """
    import asyncio

    payload = {
        "input": {
            "prompt":          prompt,
            "input_image":     input_image,
            "aspect_ratio":    aspect_ratio,
            "output_format":   "webp",
            "output_quality":  90,
            "safety_tolerance": 5,
        }
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(_KONTEXT_URL, json=payload, headers=_HEADERS)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Kontext error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()

        if data.get("status") == "succeeded":
            output = data.get("output", [])
            if output:
                return output[0] if isinstance(output, list) else output

        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Kontext no retornó ID: {data}")

        logger.info(f"Kontext prediction {pred_id} — polling...")
        poll_url     = f"https://api.replicate.com/v1/predictions/{pred_id}"
        poll_headers = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

        for _ in range(60):
            await asyncio.sleep(3)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            if pd.get("status") == "succeeded":
                output = pd.get("output", [])
                return output[0] if isinstance(output, list) else output
            if pd.get("status") in ("failed", "canceled"):
                raise RuntimeError(f"Kontext falló: {pd.get('error', 'unknown')}")

    raise RuntimeError("Kontext timeout — imagen no generada en 180s")


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


# ─── Image → Video AI (Wan2.1 primero, SVD como fallback) ─────────────────────

async def generate_video_img2vid(image_url: str, prompt: str = "") -> str:
    """
    Convierte una imagen de producto en un video AI de 4-5s.
    Pipeline: Wan2.1 (alta calidad) → SVD XT (fallback confiable).
    Retorna URL pública del video generado.
    """
    import asyncio

    # Intento 1: Wan2.1 image-to-video (alta calidad cinematica, ~60s)
    try:
        logger.info(f"img2vid: intentando Wan2.1 para {image_url[:60]}...")
        return await _img2vid_wan(image_url, prompt)
    except Exception as e:
        logger.warning(f"Wan2.1 img2vid falló ({e.__class__.__name__}: {str(e)[:120]}), usando SVD...")

    # Intento 2: Stable Video Diffusion XT (fallback, ~45s)
    logger.info(f"img2vid: usando SVD XT para {image_url[:60]}...")
    return await _img2vid_svd(image_url)


async def _img2vid_wan(image_url: str, prompt: str) -> str:
    import asyncio
    vid_prompt = (
        prompt or
        "professional product commercial, smooth slow cinematic camera movement, "
        "warm studio lighting, premium quality, 4K, no watermark"
    )
    payload = {
        "input": {
            "image":             image_url,
            "prompt":            vid_prompt,
            "negative_prompt":   "blurry, low quality, distorted, watermark, text overlay, logo",
            "num_frames":        81,
            "sample_steps":      25,
            "frames_per_second": 16,
            "guide_scale":       5.0,
        }
    }
    hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_WAN_I2V_URL, json=payload, headers=hdrs)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Wan2.1 submit {resp.status_code}: {resp.text[:200]}")
        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Wan2.1 no pred_id: {data}")

    logger.info(f"Wan2.1 prediction {pred_id} — polling...")
    poll_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(80):   # max 80 × 5s = 400s
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_hdrs)
            pd = pr.json()
            status = pd.get("status")
            if status == "succeeded":
                out = pd.get("output")
                if isinstance(out, str) and out.startswith("http"):
                    return out
                if isinstance(out, list) and out:
                    first = out[0]
                    return first if isinstance(first, str) else first.get("url", "")
                raise RuntimeError(f"Wan2.1 output inesperado: {out}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"Wan2.1 falló: {pd.get('error', 'unknown')}")
    raise RuntimeError("Wan2.1 timeout — 400s sin resultado")


async def _img2vid_svd(image_url: str) -> str:
    """Stable Video Diffusion XT — fallback confiable."""
    import asyncio
    payload = {
        "input": {
            "input_image":       image_url,
            "sizing_strategy":   "maintain_aspect_ratio",
            "frames_per_second": 6,
            "num_frames":        25,
            "motion_bucket_id":  127,
            "cond_aug":          0.02,
        }
    }
    hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_SVD_XT_URL, json=payload, headers=hdrs)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"SVD submit {resp.status_code}: {resp.text[:200]}")
        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"SVD no pred_id: {data}")

    logger.info(f"SVD XT prediction {pred_id} — polling...")
    poll_url  = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(60):   # max 60 × 5s = 300s
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_hdrs)
            pd = pr.json()
            status = pd.get("status")
            if status == "succeeded":
                out = pd.get("output")
                if isinstance(out, str) and out.startswith("http"):
                    return out
                if isinstance(out, list) and out:
                    first = out[0]
                    return first if isinstance(first, str) else ""
                raise RuntimeError(f"SVD output inesperado: {out}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"SVD falló: {pd.get('error', 'unknown')}")
    raise RuntimeError("SVD timeout — 300s sin resultado")


# ─── Minimax video-01-live: imagen real → video (sin distorsión) ─────────────

async def generate_video_minimax_live(image_url: str, prompt: str = "") -> str:
    """
    Convierte una foto real del producto en un video de 5-6s usando minimax/video-01-live.
    Descarga la imagen y la envía como base64 para evitar bloqueos de CDN (ej. ML).
    Probado: funciona con Replicate key sin UNAUTHORIZED, sin distorsión.
    Retorna URL pública del video generado.
    """
    import asyncio
    import base64

    motion_prompt = prompt or (
        "smooth slow cinematic camera movement, warm commercial lighting, "
        "premium product video quality, elegant natural motion"
    )

    # Descargar imagen y codificar como base64 (evita bloqueos de CDN externos)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as dl:
        img_resp = await dl.get(image_url, headers={"User-Agent": "Mozilla/5.0"})
        if img_resp.status_code != 200 or not img_resp.content:
            raise RuntimeError(f"No se pudo descargar imagen: {img_resp.status_code}")
        ctype   = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        img_b64 = base64.b64encode(img_resp.content).decode()
        image_data_uri = f"data:{ctype};base64,{img_b64}"
        logger.info(f"Imagen descargada: {len(img_resp.content)} bytes ({ctype})")

    payload = {
        "input": {
            "first_frame_image": image_data_uri,
            "prompt":            motion_prompt,
            "prompt_optimizer":  False,   # False = usa nuestro prompt exacto sin reescritura
        }
    }
    hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(_MINIMAX_LIVE_URL, json=payload, headers=hdrs)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Minimax Live submit {resp.status_code}: {resp.text[:300]}")
        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Minimax Live no pred_id: {data}")
        logger.info(f"Minimax Live prediction {pred_id} — polling...")

    poll_url  = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(80):   # max 80 × 5s = 400s
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_hdrs)
            pd = pr.json()
            status = pd.get("status")
            logger.debug(f"Minimax Live poll: {status}")
            if status == "succeeded":
                out = pd.get("output")
                if isinstance(out, str) and out.startswith("http"):
                    return out
                if isinstance(out, list) and out:
                    return out[0]
                raise RuntimeError(f"Minimax Live output inesperado: {out}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"Minimax Live falló: {pd.get('error', 'unknown')}")
    raise RuntimeError("Minimax Live timeout — 400s sin resultado")


# ─── Text → Video (sin imágenes — evita distorsiones) ─────────────────────────

_LTX_URL     = "https://api.replicate.com/v1/models/lightricks/ltx-video/predictions"
_WAN_T2V_URL = "https://api.replicate.com/v1/models/wavespeedai/wan-2.1-t2v-480p/predictions"


async def generate_video_t2v(prompt: str) -> str:
    """
    Text-to-video profesional. No usa imágenes del producto (evita distorsiones).
    Pipeline: LTX-Video (9:16 nativo) → Wan2.1 t2v fallback.
    Retorna URL del video generado.
    """
    import asyncio

    # Intento 1: LTX-Video de Lightricks — genera 9:16 nativo, alta calidad
    try:
        logger.info(f"t2v: LTX-Video — '{prompt[:60]}...'")
        return await _t2v_ltx(prompt)
    except Exception as e:
        logger.warning(f"LTX-Video falló ({e.__class__.__name__}: {str(e)[:120]}), usando Wan2.1...")

    # Intento 2: Wan2.1 text-to-video
    logger.info(f"t2v: Wan2.1 — '{prompt[:60]}...'")
    return await _t2v_wan(prompt)


async def _t2v_ltx(prompt: str) -> str:
    """LTX-Video — alta calidad, genera video vertical 9:16 nativo."""
    import asyncio
    payload = {
        "input": {
            "prompt":               prompt,
            "negative_prompt":      "blurry, low quality, distorted, watermark, text overlay, logo, amateur, shaky camera",
            "width":                480,
            "height":               848,
            "num_frames":           97,
            "frame_rate":           24,
            "guidance_scale":       3.5,
            "num_inference_steps":  40,
        }
    }
    hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_LTX_URL, json=payload, headers=hdrs)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"LTX-Video submit {resp.status_code}: {resp.text[:200]}")
        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"LTX-Video no pred_id: {data}")

    logger.info(f"LTX-Video prediction {pred_id} — polling...")
    poll_url  = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(60):   # max 60 × 5s = 300s
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_hdrs)
            pd = pr.json()
            status = pd.get("status")
            if status == "succeeded":
                out = pd.get("output")
                if isinstance(out, str) and out.startswith("http"):
                    return out
                if isinstance(out, list) and out:
                    first = out[0]
                    return first if isinstance(first, str) else first.get("url", "")
                raise RuntimeError(f"LTX-Video output inesperado: {out}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"LTX-Video falló: {pd.get('error', 'unknown')}")
    raise RuntimeError("LTX-Video timeout — 300s sin resultado")


async def _t2v_wan(prompt: str) -> str:
    """Wan 2.1 text-to-video fallback."""
    import asyncio
    payload = {
        "input": {
            "prompt":          prompt,
            "negative_prompt": "blurry, low quality, distorted, watermark, text overlay, amateur",
            "num_frames":      81,
            "fps":             16,
            "guide_scale":     5.0,
            "sample_steps":    25,
        }
    }
    hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_WAN_T2V_URL, json=payload, headers=hdrs)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Wan2.1 t2v submit {resp.status_code}: {resp.text[:200]}")
        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Wan2.1 t2v no pred_id: {data}")

    logger.info(f"Wan2.1 t2v prediction {pred_id} — polling...")
    poll_url  = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_hdrs = {"Authorization": f"Bearer {_REPLICATE_KEY}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(80):   # max 80 × 5s = 400s
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_hdrs)
            pd = pr.json()
            status = pd.get("status")
            if status == "succeeded":
                out = pd.get("output")
                if isinstance(out, str) and out.startswith("http"):
                    return out
                if isinstance(out, list) and out:
                    first = out[0]
                    return first if isinstance(first, str) else first.get("url", "")
                raise RuntimeError(f"Wan2.1 t2v output inesperado: {out}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"Wan2.1 t2v falló: {pd.get('error', 'unknown')}")
    raise RuntimeError("Wan2.1 t2v timeout — 400s sin resultado")
