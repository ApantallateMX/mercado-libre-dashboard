"""
conftest.py — Configuración global de la suite de tests

Uso:
  pytest tests/                     # todos los tests
  pytest tests/test_smoke.py        # solo smoke tests
  pytest tests/ -m "not slow"       # excluir tests lentos
  pytest tests/ -v                  # verbose

Variables de entorno opcionales:
  TEST_BASE_URL   — URL del servidor (default: http://localhost:8000)
  TEST_SESSION    — Cookie dash_session para autenticación
"""

import os
import pytest
import httpx
import asyncio

# ─── Configuración ────────────────────────────────────────────────────────────
BASE_URL    = os.getenv("TEST_BASE_URL", "http://localhost:8000")
SESSION_COK = os.getenv("TEST_SESSION", "")   # dash_session cookie (obtener del browser)


def get_cookies() -> dict:
    if SESSION_COK:
        return {"dash_session": SESSION_COK}
    return {}


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def cookies():
    return get_cookies()


@pytest.fixture(scope="session")
def client(base_url, cookies):
    """Cliente HTTP síncrono con cookies de sesión."""
    with httpx.Client(
        base_url=base_url,
        cookies=cookies,
        follow_redirects=True,
        timeout=30.0,
    ) as c:
        yield c


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def async_client(base_url, cookies):
    """Cliente HTTP asíncrono para tests concurrentes."""
    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=cookies,
        follow_redirects=True,
        timeout=30.0,
    ) as c:
        yield c
