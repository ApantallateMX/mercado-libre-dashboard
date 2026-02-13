import httpx
from typing import AsyncGenerator
from app.config import OLLAMA_BASE_URL, OLLAMA_MODEL


class OllamaClient:
    """Cliente async para Ollama API."""

    def __init__(self, base_url: str = None, model: str = None):
        self.base_url = base_url or OLLAMA_BASE_URL
        self.model = model or OLLAMA_MODEL

    async def is_available(self) -> bool:
        """Verifica si Ollama esta corriendo."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/api/tags", timeout=3.0)
                return resp.status_code == 200
        except Exception:
            return False

    async def chat_stream(self, messages: list, model: str = None) -> AsyncGenerator[str, None]:
        """Envia mensajes a Ollama y retorna tokens via streaming."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
        }
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120.0,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    import json
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if data.get("done", False):
                        break

    async def chat(self, messages: list, model: str = None) -> str:
        """Envia mensajes a Ollama y retorna la respuesta completa."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
