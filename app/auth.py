import secrets
import hashlib
import base64
import httpx
from urllib.parse import urlencode
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from app.config import (
    MELI_AUTH_URL, MELI_TOKEN_URL, MELI_API_URL,
    MELI_CLIENT_ID, MELI_CLIENT_SECRET, MELI_REDIRECT_URI
)
from app.services import token_store

router = APIRouter(prefix="/auth", tags=["auth"])


def _generate_code_verifier() -> str:
    """Genera un code_verifier aleatorio para PKCE."""
    return secrets.token_urlsafe(64)[:128]


def _generate_code_challenge(code_verifier: str) -> str:
    """Genera el code_challenge a partir del code_verifier (S256)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@router.get("/connect")
async def connect():
    """Inicia el flujo OAuth redirigiendo a Mercado Libre."""
    if not MELI_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="MELI_CLIENT_ID no configurado. Revisa el archivo .env"
        )

    state = secrets.token_urlsafe(32)
    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)

    await token_store.save_oauth_state(state, code_verifier)

    params = {
        "response_type": "code",
        "client_id": MELI_CLIENT_ID,
        "redirect_uri": MELI_REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": "offline_access read write urn:ml:mktp:ads:read-write"
    }

    auth_url = f"{MELI_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def callback(code: str = None, state: str = None, error: str = None):
    """Callback de OAuth - intercambia code por tokens."""
    if error:
        raise HTTPException(status_code=400, detail=f"Error de autorizacion: {error}")

    if not code:
        raise HTTPException(status_code=400, detail="No se recibio codigo de autorizacion")

    code_verifier = await token_store.pop_oauth_state(state)
    if not code_verifier:
        raise HTTPException(status_code=400, detail="State invalido - posible CSRF")

    # Intercambiar code por access_token
    async with httpx.AsyncClient() as client:
        payload = {
            "grant_type": "authorization_code",
            "client_id": MELI_CLIENT_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "code": code,
            "redirect_uri": MELI_REDIRECT_URI,
            "code_verifier": code_verifier
        }
        print(f"[AUTH DEBUG] Sending token request with code_verifier: {code_verifier[:10]}...")
        response = await client.post(
            MELI_TOKEN_URL,
            headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
            content=urlencode(payload)
        )

        if response.status_code != 200:
            error_data = response.json()
            raise HTTPException(
                status_code=400,
                detail=f"Error al obtener token: {error_data.get('message', response.text)}"
            )

        token_data = response.json()

    # Obtener info del usuario
    async with httpx.AsyncClient() as client:
        user_response = await client.get(
            f"{MELI_API_URL}/users/me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        )
        user_data = user_response.json()

    # Guardar tokens
    await token_store.save_tokens(
        user_id=str(user_data["id"]),
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token", ""),
        expires_in=token_data.get("expires_in", 21600)
    )

    return RedirectResponse(url="/dashboard")


@router.post("/logout")
async def logout():
    """Cierra la sesion eliminando los tokens."""
    tokens = await token_store.get_any_tokens()
    if tokens:
        await token_store.delete_tokens(tokens["user_id"])
    return RedirectResponse(url="/login", status_code=303)
