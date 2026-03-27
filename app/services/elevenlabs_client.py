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
_EL_KEY      = ""   # Se resuelve dinámicamente; no se cachea en módulo
_VOICE_ID_ES = "pNInz6obpgDQGcFmaJgB"   # Adam — multilingual, excelente en español
_EL_MODEL    = "eleven_multilingual_v2"

# ─── Replicate Bark (fallback, usa REPLICATE_API_KEY existente) ──────────────
_BARK_URL    = "https://api.replicate.com/v1/models/suno-ai/bark/predictions"
_BARK_VOICE  = "es_speaker_3"   # Voz masculina española, clara y profesional


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


async def _generate_bark(text: str) -> bytes:
    """TTS en español con Replicate Bark (usa REPLICATE_API_KEY existente).
    Bark produce audio WAV; lo retornamos como bytes para ffmpeg.
    """
    from app.services.replicate_client import _REPLICATE_KEY

    if not _REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_KEY no configurada")

    headers_no_wait = {
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
        # Bark no soporta Prefer:wait — siempre async
        resp = await client.post(_BARK_URL, json=payload, headers=headers_no_wait)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Bark {resp.status_code}: {resp.text[:200]}")
        data    = resp.json()
        pred_id = data.get("id")
        if not pred_id:
            raise RuntimeError(f"Bark no retornó ID: {data}")

    # Polling
    poll_url     = f"https://api.replicate.com/v1/predictions/{pred_id}"
    poll_headers = {"Authorization": f"Bearer {_REPLICATE_KEY}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(40):          # máx 40 × 5s = 200s
            await asyncio.sleep(5)
            pr  = await client.get(poll_url, headers=poll_headers)
            pd  = pr.json()
            st  = pd.get("status")
            if st == "succeeded":
                output = pd.get("output") or {}
                # Bark retorna {"audio_out": "https://..."} o string URL
                audio_url = (
                    output.get("audio_out")
                    if isinstance(output, dict)
                    else (output if isinstance(output, str) else None)
                )
                if not audio_url:
                    raise RuntimeError(f"Bark output inesperado: {output}")
                # Descargar audio
                dl = await client.get(audio_url)
                dl.raise_for_status()
                return dl.content
            if st in ("failed", "canceled"):
                raise RuntimeError(f"Bark falló: {pd.get('error', 'unknown')}")

    raise RuntimeError("Bark timeout — audio no generado en 200s")


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
