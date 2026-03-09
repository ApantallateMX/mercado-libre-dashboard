"""
auth.py — Autenticación por API Key para la API externa v1

Uso:
  Header requerido en cada request:
    X-API-Key: sk_live_xxxxxxxxxx

La API Key se configura en Railway (variable de entorno) como:
  CASHFLOW_API_KEY=apx_xxxxxxxxxx
"""

from fastapi import HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from app.config import CASHFLOW_API_KEY

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: str = Security(_api_key_header)) -> str:
    """Dependencia FastAPI — valida el header X-API-Key."""
    if not CASHFLOW_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="API Key no configurada en el servidor. Agregar CASHFLOW_API_KEY en .env.production"
        )
    if not key or key != CASHFLOW_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="API Key inválida o faltante. Incluir header: X-API-Key: sk_live_xxx"
        )
    return key
