"""
OpenRouter Client
=================
Wrapper para OpenRouter API (compatible con OpenAI).
Proveedor PRIMARIO de IA para Salud → Preguntas, Reclamos y Mensajes.
Fallback: claude_client.py cuando OpenRouter no responde.

Modelos recomendados (en orden de preferencia):
  - meta-llama/llama-3.3-70b-instruct:free  (gratis, excelente calidad)
  - mistralai/mistral-small-3.1-24b-instruct:free  (gratis, rápido)
  - anthropic/claude-3-haiku  (de pago, vía OR routing)
"""
import json
import logging
import os
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

_OR_BASE  = "https://openrouter.ai/api/v1"
_OR_KEY   = os.getenv(
    "OPENROUTER_API_KEY",
    "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",
)
_OR_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

_HEADERS = {
    "Authorization":  f"Bearer {_OR_KEY}",
    "Content-Type":   "application/json",
    "HTTP-Referer":   "https://mercado-libre-dashboard.railway.app",
    "X-Title":        "MeLi Dashboard",
}


def is_available() -> bool:
    return bool(_OR_KEY and _OR_KEY.startswith("sk-or-"))


def _build_messages(prompt: str, system: str) -> list[dict]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


async def generate(
    prompt: str,
    system: str = "",
    max_tokens: int = 512,
    model: str = "",
) -> str:
    """Genera respuesta completa (no streaming) vía OpenRouter."""
    payload = {
        "model":      model or _OR_MODEL,
        "messages":   _build_messages(prompt, system),
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_OR_BASE}/chat/completions",
            json=payload,
            headers=_HEADERS,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def generate_stream(
    prompt: str,
    system: str = "",
    max_tokens: int = 512,
    model: str = "",
) -> AsyncGenerator[str, None]:
    """Genera respuesta en streaming (SSE) vía OpenRouter."""
    payload = {
        "model":      model or _OR_MODEL,
        "messages":   _build_messages(prompt, system),
        "max_tokens": max_tokens,
        "stream":     True,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{_OR_BASE}/chat/completions",
            json=payload,
            headers=_HEADERS,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"OpenRouter error {resp.status_code}: {body.decode()[:300]}")

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                    delta = event.get("choices", [{}])[0].get("delta", {})
                    text  = delta.get("content", "")
                    if text:
                        yield text
                except Exception:
                    continue
