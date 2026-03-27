"""
TTS Client — Voz en español de México
======================================
Genera audio en español usando:
  1. ElevenLabs (si ELEVENLABS_API_KEY disponible) — calidad premium
  2. Replicate Bark como fallback (usa REPLICATE_API_KEY existente, siempre disponible)

No requiere ninguna clave adicional — Bark usa el REPLICATE_API_KEY ya configurado.
"""
import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# ─── ElevenLabs (opcional, calidad superior) ────────────────────────────────
_VOICE_ID_ES = "pNInz6obpgDQGcFmaJgB"   # Adam — multilingual, excelente en español
_EL_MODEL    = "eleven_multilingual_v2"

# ─── Replicate Bark ──────────────────────────────────────────────────────────
_BARK_URL   = "https://api.replicate.com/v1/models/suno-ai/bark/predictions"
_BARK_VOICE = "es_speaker_3"   # Voz masculina española, clara y profesional


def is_available() -> bool:
    """Siempre True — Bark usa el REPLICATE_API_KEY que ya está configurado."""
    from app.services.replicate_client import is_available as _repl_ok
    return _repl_ok()


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


def _extract_audio_url(output) -> str | None:
    """Extrae URL de audio del output de Bark — maneja todos los formatos posibles."""
    if not output:
        return None
    # Dict con clave "audio_out" (formato más común de Bark en Replicate)
    if isinstance(output, dict):
        return output.get("audio_out") or output.get("audio") or output.get("url")
    # String directo (URL)
    if isinstance(output, str) and output.startswith("http"):
        return output
    # Lista — tomar primer elemento
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str) and first.startswith("http"):
            return first
        if isinstance(first, dict):
            return first.get("audio_out") or first.get("audio") or first.get("url")
    return None


async def _generate_bark(text: str) -> bytes:
    """TTS en español con Replicate Bark (usa REPLICATE_API_KEY existente)."""
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
            raise RuntimeError(f"Bark submit {resp.status_code}: {resp.text[:300]}")
        data    = resp.json()
        pred_id = data.get("id")
        logger.info(f"Bark prediction id={pred_id} status={data.get('status')}")
        if not pred_id:
            raise RuntimeError(f"Bark no retornó ID: {data}")

    # Polling — máx 72 × 5s = 360s (6 min) — Bark puede tardar bastante
    poll_url     = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_headers = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(72):
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            st = pd.get("status")
            logger.debug(f"Bark poll #{attempt+1}: status={st}")

            if st == "succeeded":
                output    = pd.get("output")
                logger.info(f"Bark succeeded — output type={type(output).__name__} raw={str(output)[:200]}")
                audio_url = _extract_audio_url(output)
                if not audio_url:
                    raise RuntimeError(f"Bark output formato desconocido: {output}")
                logger.info(f"Bark audio_url={audio_url[:80]}")
                dl = await client.get(audio_url, timeout=60.0)
                dl.raise_for_status()
                logger.info(f"Bark audio descargado: {len(dl.content)} bytes")
                return dl.content

            if st in ("failed", "canceled"):
                raise RuntimeError(f"Bark falló (status={st}): {pd.get('error', 'unknown')}")

    raise RuntimeError("Bark timeout — audio no generado en 360s")


async def generate_audio(text: str) -> bytes:
    """
    Genera audio en español de México.
    Intenta ElevenLabs primero (si ELEVENLABS_API_KEY disponible),
    luego cae a Replicate Bark (usa REPLICATE_API_KEY existente).
    Retorna bytes de audio (MP3 o WAV).
    """
    if _el_key():
        try:
            logger.info("TTS: usando ElevenLabs")
            return await _generate_elevenlabs(text)
        except Exception as e:
            logger.warning(f"ElevenLabs falló, usando Bark: {e}")

    logger.info("TTS: usando Replicate Bark (español)")
    return await _generate_bark(text)
