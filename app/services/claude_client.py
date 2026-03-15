"""
Async wrapper for Anthropic Claude API using httpx (no SDK dependency).
Used for AI-powered title/description/attributes improvement.
"""

import httpx
from typing import AsyncGenerator

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


def _get_key() -> str:
    """Always read key fresh from config to avoid stale module-level imports."""
    from app.config import ANTHROPIC_API_KEY
    return ANTHROPIC_API_KEY or ""


def is_available() -> bool:
    """Check if the Anthropic API key is configured."""
    k = _get_key()
    return bool(k and len(k) > 10)


async def generate(prompt: str, system: str = "", max_tokens: int = 1024) -> str:
    """Generate a complete response (non-streaming)."""
    key = _get_key()
    if not key or len(key) <= 10:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    async with httpx.AsyncClient(timeout=60.0) as client:
        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        resp = await client.post(
            ANTHROPIC_API_URL,
            json=payload,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message", resp.text)
            except Exception:
                msg = resp.text
            raise RuntimeError(f"Anthropic API error: {msg}")
        data = resp.json()
        content = data.get("content", [])
        return "".join(block.get("text", "") for block in content if block.get("type") == "text")


async def generate_stream(prompt: str, system: str = "", max_tokens: int = 1024) -> AsyncGenerator[str, None]:
    """Generate a streaming response (SSE). Yields text chunks."""
    key = _get_key()
    if not key or len(key) <= 10:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    async with httpx.AsyncClient(timeout=120.0) as client:
        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        async with client.stream(
            "POST",
            ANTHROPIC_API_URL,
            json=payload,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                try:
                    import json as _json
                    err = _json.loads(body)
                    msg = err.get("error", {}).get("message", body.decode())
                except Exception:
                    msg = body.decode()
                raise RuntimeError(f"Anthropic API error: {msg}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    import json
                    event = json.loads(data_str)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            yield text
                except Exception:
                    continue
