"""
OpenRouter Client
=================
Wrapper para OpenRouter API con retry automático y cascade de modelos.

Cascade en caso de 429/error:
  1. google/gemma-3-27b-it:free               (gratis, Google)
  2. meta-llama/llama-3.3-70b-instruct:free   (gratis, Meta — distinto proveedor)
  3. mistralai/mistral-small-3.1-24b-instruct:free  (gratis, Mistral)
  4. Claude Haiku vía Anthropic directo  (~$0.001/call, fallback final)

NOTA: Los modelos :free de OpenRouter cambian frecuentemente.
Si todos devuelven 404, actualizar _FREE_MODELS con modelos vigentes en:
  https://openrouter.ai/models?q=:free
"""
import asyncio
import json
import logging
import os
import time
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

_OR_BASE  = "https://openrouter.ai/api/v1"
_OR_KEY   = os.getenv("OPENROUTER_API_KEY", "")
_OR_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

# Cascade de modelos gratuitos — verificar disponibilidad en https://openrouter.ai/models?q=:free
_FREE_MODELS = [
    "google/gemma-3-27b-it:free",                      # primario — Google
    "meta-llama/llama-3.3-70b-instruct:free",          # backup — Meta, distinto proveedor
    "mistralai/mistral-small-3.1-24b-instruct:free",   # backup 2 — Mistral
]

_HEADERS = {
    "Authorization":  f"Bearer {_OR_KEY}",
    "Content-Type":   "application/json",
    "HTTP-Referer":   "https://mercado-libre-dashboard.railway.app",
    "X-Title":        "MeLi Dashboard",
}

# Circuit breaker: modelos marcados como muertos (404) se saltan por _DEAD_TTL segundos
_dead_models: dict[str, float] = {}
_DEAD_TTL = 3600  # 1 hora

# Cache del listado dinámico de modelos gratuitos desde la API de OpenRouter
_dyn_free_models: list[str] = []
_dyn_cache_ts: float = 0.0
_DYN_TTL = 3600  # refrescar cada hora


def _mark_dead(model: str) -> None:
    _dead_models[model] = time.time()
    logger.warning(f"[OpenRouter] 404 — modelo no disponible, saltando por 1h: {model}")


def _is_dead(model: str) -> bool:
    ts = _dead_models.get(model)
    if ts is None:
        return False
    if time.time() - ts > _DEAD_TTL:
        _dead_models.pop(model, None)
        return False
    return True


async def _get_free_models() -> list[str]:
    """
    Obtiene la lista actual de modelos gratuitos desde la API de OpenRouter (cache 1h).
    Si falla, usa _FREE_MODELS como fallback estático.
    """
    global _dyn_free_models, _dyn_cache_ts
    if time.time() - _dyn_cache_ts < _DYN_TTL and _dyn_free_models:
        return _dyn_free_models
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_OR_BASE}/models",
                headers={"Authorization": f"Bearer {_OR_KEY}"},
            )
            if resp.is_success:
                data = resp.json().get("data", [])
                free = [
                    m["id"] for m in data
                    if isinstance(m.get("id"), str)
                    and m["id"].endswith(":free")
                    and (m.get("context_length") or 0) >= 8000
                ]
                if free:
                    _dyn_free_models = free[:10]
                    _dyn_cache_ts = time.time()
                    logger.info(f"[OpenRouter] {len(_dyn_free_models)} modelos gratuitos disponibles: {_dyn_free_models[:3]}...")
                    return _dyn_free_models
    except Exception as e:
        logger.warning(f"[OpenRouter] No se pudo obtener lista de modelos, usando fallback estático: {e}")
    return _FREE_MODELS


def is_available() -> bool:
    return bool(_OR_KEY and _OR_KEY.startswith("sk-or-"))


def _build_messages(prompt: str, system: str) -> list[dict]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


async def _call_anthropic_haiku_fallback(prompt: str, system: str, max_tokens: int) -> str:
    """Last-resort fallback: Claude Haiku via Anthropic direct API."""
    import os as _os, base64 as _b64
    _p1 = _os.getenv("AI_KEY_P1", "")
    _p2 = _os.getenv("AI_KEY_P2", "")
    api_key = ""
    if _p1 and _p2:
        try:
            api_key = _b64.b64decode(_p1 + _p2).decode().strip()
        except Exception:
            pass
    if not api_key:
        api_key = _os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("No Anthropic API key available for fallback")

    messages = [{"role": "user", "content": prompt or "Hola"}]
    body: dict = {
        "model": "claude-haiku-4-5",
        "max_tokens": max(max_tokens, 50),
        "messages": messages,
    }
    if system and system.strip():
        body["system"] = system.strip()

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        if not resp.is_success:
            body_text = resp.text[:500]
            logger.error(f"[Anthropic fallback] {resp.status_code} error: {body_text}")
            raise RuntimeError(f"Anthropic {resp.status_code}: {body_text}")
        data = resp.json()
        return data["content"][0]["text"]


async def generate(
    prompt: str,
    system: str = "",
    max_tokens: int = 512,
    model: str = "",
) -> str:
    """
    Genera respuesta completa con retry automático y cascade de modelos.
    429/404 → siguiente modelo → Claude Haiku como último recurso.
    Los modelos con 404 se marcan como muertos por 1h (circuit breaker).
    """
    primary = model or _OR_MODEL
    all_free = await _get_free_models()
    cascade_all = [primary] + [m for m in all_free if m != primary]
    cascade = [m for m in cascade_all if not _is_dead(m)] or cascade_all

    last_error = None
    for attempt_model in cascade:
        payload = {
            "model":      attempt_model,
            "messages":   _build_messages(prompt, system),
            "max_tokens": max_tokens,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{_OR_BASE}/chat/completions",
                    json=payload,
                    headers=_HEADERS,
                )
                if resp.status_code == 429:
                    logger.warning(f"[OpenRouter] 429 rate-limit on {attempt_model}, trying next")
                    last_error = f"Rate limit on {attempt_model}"
                    await asyncio.sleep(0.5)
                    continue
                if resp.status_code == 404:
                    _mark_dead(attempt_model)
                    last_error = f"Error 404 — modelo no disponible: {attempt_model}"
                    continue
                if resp.status_code != 200:
                    logger.warning(f"[OpenRouter] {resp.status_code} on {attempt_model}: {resp.text[:200]}")
                    last_error = f"Error {resp.status_code} on {attempt_model}"
                    continue
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if attempt_model != primary:
                    logger.info(f"[OpenRouter] Succeeded with fallback model: {attempt_model}")
                return content
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[OpenRouter] Exception on {attempt_model}: {e}")
            continue

    # All free models failed → try Anthropic Haiku
    logger.warning(f"[OpenRouter] All free models failed, falling back to Claude Haiku. Last error: {last_error}")
    try:
        result = await _call_anthropic_haiku_fallback(prompt, system, max_tokens)
        logger.info("[OpenRouter] Claude Haiku fallback succeeded")
        return result
    except Exception as e:
        raise RuntimeError(f"[OpenRouter] All models failed. Last free error: {last_error}. Haiku error: {e}")


async def generate_stream(
    prompt: str,
    system: str = "",
    max_tokens: int = 512,
    model: str = "",
) -> AsyncGenerator[str, None]:
    """
    Genera respuesta en streaming con retry en cascade de modelos.
    Si todos los modelos gratuitos fallan → Claude Haiku (non-streaming → yield único).
    """
    primary = model or _OR_MODEL
    all_free = await _get_free_models()
    cascade_all = [primary] + [m for m in all_free if m != primary]
    cascade = [m for m in cascade_all if not _is_dead(m)] or cascade_all

    last_error = None
    for attempt_model in cascade:
        payload = {
            "model":      attempt_model,
            "messages":   _build_messages(prompt, system),
            "max_tokens": max_tokens,
            "stream":     True,
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{_OR_BASE}/chat/completions",
                    json=payload,
                    headers=_HEADERS,
                ) as resp:
                    if resp.status_code == 429:
                        await resp.aread()
                        logger.warning(f"[OpenRouter stream] 429 on {attempt_model}, trying next")
                        last_error = f"Rate limit on {attempt_model}"
                        await asyncio.sleep(0.5)
                        continue
                    if resp.status_code == 404:
                        await resp.aread()
                        _mark_dead(attempt_model)
                        last_error = f"Error 404 — modelo no disponible: {attempt_model}"
                        continue
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.warning(f"[OpenRouter stream] {resp.status_code} on {attempt_model}")
                        last_error = f"Error {resp.status_code}"
                        continue

                    if attempt_model != primary:
                        logger.info(f"[OpenRouter stream] Using fallback: {attempt_model}")

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            return
                        try:
                            event = json.loads(data_str)
                            delta = event.get("choices", [{}])[0].get("delta", {})
                            text  = delta.get("content", "")
                            if text:
                                yield text
                        except Exception:
                            continue
                    return  # stream completed successfully
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[OpenRouter stream] Exception on {attempt_model}: {e}")
            continue

    # All free models failed for streaming → Haiku non-streaming, yield as single chunk
    logger.warning(f"[OpenRouter stream] All free models failed, using Claude Haiku. Last error: {last_error}")
    try:
        text = await _call_anthropic_haiku_fallback(prompt, system, max_tokens)
        yield text
    except Exception as e:
        yield f"[ERROR] Todos los modelos fallaron. Último error: {last_error}. Haiku: {e}"
