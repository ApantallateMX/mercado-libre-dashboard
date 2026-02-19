import secrets
import hashlib
import hmac
import base64
import json
import httpx
from urllib.parse import urlencode
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from app.config import (
    MELI_AUTH_URL, MELI_TOKEN_URL, MELI_API_URL,
    MELI_CLIENT_ID, MELI_CLIENT_SECRET, MELI_REDIRECT_URI, SECRET_KEY,
    MELI_USER_ID, MELI_USER_ID_2
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


def _build_state(code_verifier: str) -> str:
    """
    Codifica el code_verifier dentro del state (firmado con SECRET_KEY).
    Así no se necesita DB — funciona en Railway, local, o cualquier servidor.
    Formato: base64(json({nonce, cv})).hmac_signature
    """
    nonce = secrets.token_urlsafe(16)
    payload = json.dumps({"n": nonce, "cv": code_verifier}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()[:20]
    return f"{payload_b64}.{sig}"


def _parse_state(state: str) -> str | None:
    """
    Extrae el code_verifier del state firmado.
    Retorna None si la firma es invalida o el formato es incorrecto.
    """
    try:
        payload_b64, sig = state.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()[:20]
        if not hmac.compare_digest(sig, expected):
            return None
        # Añadir padding si falta
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload["cv"]
    except Exception:
        return None


@router.get("/connect")
async def connect():
    """Inicia el flujo OAuth redirigiendo a Mercado Libre."""
    if not MELI_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="MELI_CLIENT_ID no configurado. Revisa el archivo .env"
        )

    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    state = _build_state(code_verifier)

    params = {
        "response_type": "code",
        "client_id": MELI_CLIENT_ID,
        "redirect_uri": MELI_REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": "offline_access read write urn:ml:mktp:ads:read-write",
        "prompt": "consent",
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

    if not state:
        raise HTTPException(status_code=400, detail="State requerido")

    code_verifier = _parse_state(state)
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

    new_refresh = token_data.get("refresh_token", "")
    uid = str(user_data["id"])
    nickname = user_data.get("nickname", "")

    # Guardar tokens en DB con nickname
    await token_store.save_tokens(
        user_id=uid,
        access_token=token_data["access_token"],
        refresh_token=new_refresh,
        expires_in=token_data.get("expires_in", 21600),
        nickname=nickname,
    )

    # Persistir nuevo refresh_token en .env.production para sobrevivir redeploys de Railway
    if new_refresh:
        import re as _re, os as _os
        for env_file in (".env.production", ".env"):
            path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), env_file)
            if _os.path.exists(path):
                try:
                    text = open(path, encoding="utf-8").read()
                    if uid == MELI_USER_ID:
                        # Cuenta principal (1)
                        text = _re.sub(r"(?m)^MELI_REFRESH_TOKEN=.*$", f"MELI_REFRESH_TOKEN={new_refresh}", text)
                        if "MELI_REFRESH_TOKEN=" not in text:
                            text += f"\nMELI_REFRESH_TOKEN={new_refresh}\n"
                    else:
                        # Cuenta 2 (u otras)
                        if _re.search(r"(?m)^MELI_REFRESH_TOKEN_2=", text):
                            text = _re.sub(r"(?m)^MELI_REFRESH_TOKEN_2=.*$", f"MELI_REFRESH_TOKEN_2={new_refresh}", text)
                        else:
                            text += f"\nMELI_REFRESH_TOKEN_2={new_refresh}\n"
                        # Actualizar MELI_USER_ID_2 si esta vacio
                        if not MELI_USER_ID_2:
                            if _re.search(r"(?m)^MELI_USER_ID_2=", text):
                                text = _re.sub(r"(?m)^MELI_USER_ID_2=.*$", f"MELI_USER_ID_2={uid}", text)
                            else:
                                text += f"\nMELI_USER_ID_2={uid}\n"
                    open(path, "w", encoding="utf-8").write(text)
                    print(f"[AUTH] Tokens updated for user {uid} in {env_file}")
                except Exception as _e:
                    print(f"[AUTH] Could not update {env_file}: {_e}")

    # Setear cookie de cuenta activa
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("active_account_id", uid, max_age=2592000, httponly=True, samesite="lax")
    return response


@router.post("/logout")
async def logout():
    """Cierra la sesion eliminando los tokens."""
    tokens = await token_store.get_any_tokens()
    if tokens:
        await token_store.delete_tokens(tokens["user_id"])
    return RedirectResponse(url="/login", status_code=303)
