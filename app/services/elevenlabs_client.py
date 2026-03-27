"""
TTS Client — Voz en español de México
======================================
Cadena de fallbacks (mejor a peor calidad):
  1. ElevenLabs  — requiere ELEVENLABS_API_KEY (calidad premium)
  2. gTTS        — Google TTS, gratis, sin key, HTTP puro (~1s), muy confiable
  3. edge-tts    — Microsoft Edge TTS, gratis, sin key, voz MX natural (~2s)
  4. Replicate Bark — lento (~5 min) pero usa REPLICATE_API_KEY existente
"""
import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# ─── ElevenLabs (opcional, calidad superior) ────────────────────────────────
_VOICE_ID_ES = "pNInz6obpgDQGcFmaJgB"   # Adam — multilingual v2
_EL_MODEL    = "eleven_multilingual_v2"

# ─── edge-tts (Microsoft, gratis, sin API key) ──────────────────────────────
_EDGE_VOICE  = "es-MX-JorgeNeural"   # Voz masculina mexicana, profesional y clara

# ─── Replicate Bark (fallback final) ─────────────────────────────────────────
_BARK_URL   = "https://api.replicate.com/v1/models/suno-ai/bark/predictions"
_BARK_VOICE = "es_speaker_3"


def is_available() -> bool:
    """Siempre True — edge-tts no requiere keys."""
    return True


def _el_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "").strip()


async def _generate_elevenlabs(text: str) -> bytes:
    """TTS premium con ElevenLabs multilingual v2."""
    key = _el_key()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{_VOICE_ID_ES}"
    headers = {
        "Accept":        "audio/mpeg",
        "Content-Type":  "application/json",
        "xi-api-key":    key,
    }
    payload = {
        "text":     text,
        "model_id": _EL_MODEL,
        "voice_settings": {
            "stability":         0.55,
            "similarity_boost":  0.80,
            "style":             0.35,
            "use_speaker_boost": True,
        },
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs {resp.status_code}: {resp.text[:200]}")
        return resp.content


async def _generate_gtts(text: str) -> bytes:
    """TTS con Google TTS (gratis, HTTP puro, sin WebSocket, muy confiable en producción)."""
    import io
    from gtts import gTTS

    loop = asyncio.get_event_loop()

    def _sync() -> bytes:
        tts = gTTS(text=text, lang="es", tld="com.mx")
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        return buf.getvalue()

    data = await loop.run_in_executor(None, _sync)
    if not data:
        raise RuntimeError("gTTS no generó audio")
    logger.info(f"gTTS OK: {len(data)} bytes")
    return data


async def _generate_edge_tts(text: str) -> bytes:
    """TTS con Microsoft Edge TTS (gratis, sin API key, voz mexicana natural).
    Genera MP3 de alta calidad en ~1-3 segundos.
    """
    import edge_tts

    audio_data = b""
    communicate = edge_tts.Communicate(text, _EDGE_VOICE)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data += chunk["data"]

    if not audio_data:
        raise RuntimeError("edge-tts no generó audio")

    logger.info(f"edge-tts OK: {len(audio_data)} bytes")
    return audio_data


def _extract_audio_url(output) -> str | None:
    """Extrae URL de audio del output de Bark — maneja todos los formatos."""
    if not output:
        return None
    if isinstance(output, dict):
        return output.get("audio_out") or output.get("audio") or output.get("url")
    if isinstance(output, str) and output.startswith("http"):
        return output
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str) and first.startswith("http"):
            return first
        if isinstance(first, dict):
            return first.get("audio_out") or first.get("audio") or first.get("url")
    return None


async def _generate_bark(text: str) -> bytes:
    """TTS fallback con Replicate Bark (usa REPLICATE_API_KEY existente)."""
    from app.services.replicate_client import _REPLICATE_KEY

    if not _REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_KEY no configurada")

    headers = {
        "Authorization": f"Bearer {_REPLICATE_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "input": {
            "prompt":         text,
            "history_prompt": _BARK_VOICE,
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_BARK_URL, json=payload, headers=headers)
        logger.info(f"Bark submit: status={resp.status_code}")
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Bark {resp.status_code}: {resp.text[:300]}")
        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Bark no retornó ID: {data}")

    poll_url     = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_headers = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(72):   # máx 360s
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            st = pd.get("status")
            logger.debug(f"Bark poll #{attempt+1}: {st}")
            if st == "succeeded":
                output    = pd.get("output")
                logger.info(f"Bark output type={type(output).__name__} raw={str(output)[:200]}")
                audio_url = _extract_audio_url(output)
                if not audio_url:
                    raise RuntimeError(f"Bark output desconocido: {output}")
                dl = await client.get(audio_url, timeout=60.0)
                dl.raise_for_status()
                return dl.content
            if st in ("failed", "canceled"):
                raise RuntimeError(f"Bark falló: {pd.get('error', 'unknown')}")

    raise RuntimeError("Bark timeout — 360s sin resultado")


async def generate_audio(text: str) -> bytes:
    """
    Genera audio en español de México.
    Orden de preferencia:
    1. ElevenLabs (si ELEVENLABS_API_KEY disponible)
    2. edge-tts Microsoft (gratis, sin key, ~2s)
    3. Replicate Bark (lento, como último recurso)
    """
    # 1. ElevenLabs
    if _el_key():
        try:
            logger.info("TTS: intentando ElevenLabs")
            return await _generate_elevenlabs(text)
        except Exception as e:
            logger.warning(f"ElevenLabs falló: {e}")

    # 2. gTTS (primario gratuito — HTTP puro, confiable en Railway)
    try:
        logger.info("TTS: usando gTTS (Google, es-MX)")
        return await _generate_gtts(text)
    except Exception as e:
        logger.warning(f"gTTS falló: {e}")

    # 3. edge-tts (fallback secundario)
    try:
        logger.info("TTS: usando edge-tts (es-MX-JorgeNeural)")
        return await _generate_edge_tts(text)
    except Exception as e:
        logger.warning(f"edge-tts falló: {e}")

    # 4. Bark (último recurso)
    logger.info("TTS: último recurso — Replicate Bark")
    return await _generate_bark(text)
