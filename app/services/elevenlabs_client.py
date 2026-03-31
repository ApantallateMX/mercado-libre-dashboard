"""
TTS Client — Voz en español de México
======================================
Cadena de fallbacks (mejor a peor calidad):
  1. ElevenLabs      — requiere ELEVENLABS_API_KEY (calidad premium)
  2. gTTS            — Google TTS via librería, gratis (~1s)
  3. Google TTS HTTP — Google TTS directo via httpx, sin librería, sin WebSocket
  4. edge-tts        — Microsoft Edge TTS, voz MX natural (~2s), requiere WebSocket
  5. Replicate Bark  — lento (~5 min) pero usa REPLICATE_API_KEY existente
"""
import asyncio
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

# ─── ElevenLabs (opcional, calidad superior) ────────────────────────────────
_VOICE_ID_ES = "pNInz6obpgDQGcFmaJgB"   # Adam — multilingual v2
_EL_MODEL    = "eleven_multilingual_v2"

# ─── edge-tts (Microsoft, gratis, sin API key) ──────────────────────────────
_EDGE_VOICE  = "es-MX-JorgeNeural"

# ─── Replicate Bark (fallback final) ─────────────────────────────────────────
_BARK_URL   = "https://api.replicate.com/v1/models/suno-ai/bark/predictions"
_BARK_VOICE = "es_speaker_3"


def is_available() -> bool:
    return True


def _el_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "").strip()


async def _generate_elevenlabs(text: str) -> bytes:
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
    """Google TTS via librería gTTS (HTTP puro, sin WebSocket)."""
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


def _split_text(text: str, max_len: int = 180) -> list[str]:
    """Divide texto en chunks ≤ max_len en fronteras de oraciones."""
    # Dividir en oraciones
    sentences = re.split(r'(?<=[.!?¿¡,;]) +', text)
    chunks: list[str] = []
    current = ""
    for s in sentences:
        if len(current) + len(s) + 1 <= max_len:
            current = (current + " " + s).strip()
        else:
            if current:
                chunks.append(current)
            # Si la oración sola excede max_len, cortarla en palabras
            if len(s) > max_len:
                words = s.split()
                current = ""
                for w in words:
                    if len(current) + len(w) + 1 <= max_len:
                        current = (current + " " + w).strip()
                    else:
                        if current:
                            chunks.append(current)
                        current = w
            else:
                current = s
    if current:
        chunks.append(current)
    return chunks or [text[:max_len]]


async def _generate_google_tts_direct(text: str) -> bytes:
    """Google TTS vía httpx directo — sin librería, funciona en cualquier entorno."""
    chunks = _split_text(text, max_len=180)
    logger.info(f"Google TTS direct: {len(chunks)} chunk(s)")
    parts: list[bytes] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for i, chunk in enumerate(chunks):
            resp = await client.get(
                "https://translate.google.com/translate_tts",
                params={
                    "ie":       "UTF-8",
                    "q":        chunk,
                    "tl":       "es",
                    "tld":      "com.mx",
                    "client":   "tw-ob",
                },
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://translate.google.com/",
                },
                follow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.content) > 100:
                parts.append(resp.content)
                logger.info(f"Google TTS direct chunk {i+1}/{len(chunks)}: {len(resp.content)} bytes")
            else:
                raise RuntimeError(
                    f"Google TTS direct chunk {i+1}: HTTP {resp.status_code}, "
                    f"bytes={len(resp.content)}"
                )
    if not parts:
        raise RuntimeError("Google TTS direct: sin audio")
    combined = b"".join(parts)
    logger.info(f"Google TTS direct OK: {len(combined)} bytes total")
    return combined


async def _generate_edge_tts(text: str) -> bytes:
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
    from app.services.replicate_client import _REPLICATE_KEY
    if not _REPLICATE_KEY:
        raise RuntimeError("REPLICATE_API_KEY no configurada")
    headers = {
        "Authorization": f"Bearer {_REPLICATE_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {"input": {"prompt": text, "history_prompt": _BARK_VOICE}}
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
        for attempt in range(72):
            await asyncio.sleep(5)
            pr = await client.get(poll_url, headers=poll_headers)
            pd = pr.json()
            st = pd.get("status")
            logger.debug(f"Bark poll #{attempt+1}: {st}")
            if st == "succeeded":
                output    = pd.get("output")
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
    Genera audio TTS en español de México.
    Cadena de fallbacks (mejor calidad primero):
      1. ElevenLabs (premium, si hay API key)
      2. edge-tts Microsoft JorgeNeural es-MX (natural, gratis)
      3. gTTS librería (Google, HTTP)
      4. Google TTS directo via httpx (sin librería)
      5. Replicate Bark (lento, último recurso)
    """
    # 1. ElevenLabs (si hay key)
    if _el_key():
        try:
            logger.info("TTS: ElevenLabs")
            return await _generate_elevenlabs(text)
        except Exception as e:
            logger.warning(f"ElevenLabs falló: {e}")

    # 2. edge-tts — Microsoft JorgeNeural es-MX, suena humano y natural
    try:
        logger.info("TTS: edge-tts (es-MX-JorgeNeural)")
        return await _generate_edge_tts(text)
    except Exception as e:
        logger.warning(f"edge-tts falló: {e}")

    # 3. gTTS
    try:
        logger.info("TTS: gTTS (Google, es-MX)")
        return await _generate_gtts(text)
    except Exception as e:
        logger.warning(f"gTTS falló: {e}")

    # 4. Google TTS directo (httpx, sin librería)
    try:
        logger.info("TTS: Google TTS directo (httpx)")
        return await _generate_google_tts_direct(text)
    except Exception as e:
        logger.warning(f"Google TTS directo falló: {e}")

    # 5. Bark
    logger.info("TTS: Replicate Bark (último recurso)")
    return await _generate_bark(text)
