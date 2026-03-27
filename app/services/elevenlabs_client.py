"""
ElevenLabs TTS Client — Voz en español de México
=================================================
Genera audio MP3 con voz profesional en español usando ElevenLabs multilingual v2.
Free tier: 10,000 caracteres/mes (~33 comerciales de 30s).

Configuración:
  ELEVENLABS_API_KEY=<key>  en Railway environment variables
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_EL_KEY  = os.getenv("ELEVENLABS_API_KEY", "")

# Voz masculina latinoamericana — multilingual v2 produce español excelente
_VOICE_ID_ES = "pNInz6obpgDQGcFmaJgB"   # Adam — cálido, profesional, funciona perfecto en español
_MODEL_ID    = "eleven_multilingual_v2"


def is_available() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY", "").strip())


async def generate_audio(text: str, voice_id: str = _VOICE_ID_ES) -> bytes:
    """
    Genera audio MP3 a partir de texto en español de México.
    Retorna bytes del archivo MP3.
    Lanza RuntimeError si falla.
    """
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY no configurada")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "Accept":        "audio/mpeg",
        "Content-Type":  "application/json",
        "xi-api-key":    key,
    }
    payload = {
        "text":     text,
        "model_id": _MODEL_ID,
        "voice_settings": {
            "stability":        0.55,
            "similarity_boost": 0.80,
            "style":            0.35,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs error {resp.status_code}: {resp.text[:300]}"
            )
        return resp.content
