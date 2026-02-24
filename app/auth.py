"""
auth.py — Rutas de autenticación OAuth para todas las plataformas

MERCADO LIBRE:
    Flujo: OAuth 2.0 + PKCE (Proof Key for Code Exchange)
    - /auth/connect      → genera code_verifier + challenge → redirige a MeLi
    - /auth/callback     → recibe code → intercambia por tokens → guarda en DB

AMAZON:
    Flujo: LWA (Login with Amazon) — OAuth 2.0 sin PKCE
    - /auth/amazon/connect   → construye URL de autorización con App Solution ID
    - /auth/amazon/callback  → recibe spapi_oauth_code → intercambia → guarda refresh_token

DIFERENCIAS CLAVE entre MeLi y Amazon:
    - MeLi usa PKCE + state firmado con HMAC para anti-CSRF
    - Amazon usa state simple (nonce) firmado con SECRET_KEY
    - MeLi callback recibe: ?code=X&state=Y
    - Amazon callback recibe: ?spapi_oauth_code=X&state=Y&selling_partner_id=Z
    - MeLi refresh_token expira y se renueva en cada uso
    - Amazon refresh_token NO expira (hasta que se revoca manualmente)
"""

import secrets
import hashlib
import hmac
import base64
import json
import httpx
import logging
from urllib.parse import urlencode
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from app.config import (
    MELI_AUTH_URL, MELI_TOKEN_URL, MELI_API_URL,
    MELI_CLIENT_ID, MELI_CLIENT_SECRET, MELI_REDIRECT_URI, SECRET_KEY,
    MELI_USER_ID,
    AMAZON_CLIENT_ID, AMAZON_CLIENT_SECRET, AMAZON_REDIRECT_URI,
    AMAZON_APP_SOLUTION_ID, AMAZON_SELLER_ID, AMAZON_MARKETPLACE_ID,
    AMAZON_MARKETPLACE_NAME, AMAZON_NICKNAME,
)
from app.services import token_store

logger = logging.getLogger(__name__)

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
    # Sistema dinámico: busca el slot existente del user o crea uno nuevo (cuentaN)
    if new_refresh:
        import re as _re, os as _os
        for env_file in (".env.production", ".env"):
            path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), env_file)
            if not _os.path.exists(path):
                continue
            try:
                text = open(path, encoding="utf-8").read()
                # Parsear env actual para encontrar slots existentes
                env_vars = {}
                for line in text.splitlines():
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, _, v = line.partition('=')
                        env_vars[k.strip()] = v.strip()

                # Buscar si este user_id ya tiene un slot asignado
                rt_key = None
                uid_key = None
                if env_vars.get("MELI_USER_ID") == uid:
                    rt_key = "MELI_REFRESH_TOKEN"
                else:
                    n = 2
                    while True:
                        k_uid = f"MELI_USER_ID_{n}"
                        if k_uid not in env_vars:
                            break
                        if env_vars[k_uid] == uid:
                            rt_key = f"MELI_REFRESH_TOKEN_{n}"
                            break
                        n += 1

                if rt_key:
                    # Slot existente — solo actualizar refresh token
                    text = _re.sub(rf"(?m)^{rt_key}=.*$", f"{rt_key}={new_refresh}", text)
                else:
                    # Cuenta nueva — encontrar próximo slot disponible
                    n = 2
                    while f"MELI_USER_ID_{n}" in env_vars:
                        n += 1
                    uid_key = f"MELI_USER_ID_{n}"
                    rt_key = f"MELI_REFRESH_TOKEN_{n}"
                    text += f"\n{uid_key}={uid}\n{rt_key}={new_refresh}\n"
                    print(f"[AUTH] Nueva cuenta registrada en slot {n}: {uid} ({nickname})")

                open(path, "w", encoding="utf-8").write(text)
                print(f"[AUTH] Tokens updated for user {uid} ({nickname}) in {env_file}")
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


# ═══════════════════════════════════════════════════════════════════════════
# AMAZON — Rutas de autenticación LWA (Login with Amazon)
# ═══════════════════════════════════════════════════════════════════════════

def _build_amazon_state() -> str:
    """
    Genera un state anti-CSRF firmado para el flujo OAuth de Amazon.

    A diferencia de MeLi (que embebe el code_verifier en el state),
    Amazon no usa PKCE — solo necesitamos un nonce para anti-CSRF.

    Formato: base64(json({nonce})).hmac_signature[:20]
    """
    nonce = secrets.token_urlsafe(16)
    payload = json.dumps({"n": nonce}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()[:20]
    return f"{payload_b64}.{sig}"


def _verify_amazon_state(state: str) -> bool:
    """
    Verifica que el state del callback de Amazon sea válido (no manipulado).

    Returns:
        True si la firma es válida, False si posible CSRF.
    """
    try:
        payload_b64, sig = state.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()[:20]
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


@router.get("/amazon/connect")
async def amazon_connect():
    """
    Inicia el flujo OAuth de Amazon SP-API.

    Construye la URL de autorización de Seller Central y redirige al vendedor
    para que autorice nuestra app (VeKtorClaude) a acceder a su cuenta.

    URL de autorización:
        https://sellercentral.amazon.com.mx/apps/authorize/consent
        ?application_id={AMAZON_APP_SOLUTION_ID}
        &state={SIGNED_STATE}
        &version=beta    ← requerido para apps Draft

    Después de que el vendedor autoriza, Amazon redirige a AMAZON_REDIRECT_URI
    con: ?spapi_oauth_code=XXX&state=YYY&selling_partner_id=ZZZ
    """
    if not AMAZON_APP_SOLUTION_ID:
        raise HTTPException(
            status_code=500,
            detail="AMAZON_APP_SOLUTION_ID no configurado. Obtenerlo desde Developer Central → App ID."
        )
    if not AMAZON_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="AMAZON_CLIENT_ID no configurado. Revisar .env.production"
        )

    state = _build_amazon_state()

    # URL de Seller Central México para autorizar la app
    # version=beta es requerido para apps en estado Draft (no publicadas)
    auth_url = (
        f"https://sellercentral.amazon.com.mx/apps/authorize/consent"
        f"?application_id={AMAZON_APP_SOLUTION_ID}"
        f"&state={state}"
        f"&version=beta"
    )

    logger.info(f"[Amazon OAuth] Iniciando autorización → {auth_url[:80]}...")
    return RedirectResponse(url=auth_url)


@router.get("/amazon/callback")
async def amazon_callback(
    request: Request,
    spapi_oauth_code: str = None,
    state: str = None,
    selling_partner_id: str = None,
    error: str = None,
):
    """
    Callback de Amazon SP-API OAuth.

    Amazon redirige aquí después de que el vendedor autoriza la app.
    Parámetros recibidos:
        spapi_oauth_code:   Código temporal para intercambiar por tokens
        state:              Nuestro state firmado (anti-CSRF)
        selling_partner_id: Merchant Token del vendedor que autorizó

    Proceso:
        1. Verificar state (anti-CSRF)
        2. Intercambiar spapi_oauth_code por access_token + refresh_token
        3. Guardar refresh_token en DB (amazon_accounts)
        4. Persistir en .env.production para sobrevivir redeploys de Railway
        5. Redirigir al dashboard

    El refresh_token de Amazon NO expira — es de larga duración.
    Solo se invalida si el vendedor revoca el acceso manualmente.
    """
    if error:
        raise HTTPException(status_code=400, detail=f"Amazon rechazó la autorización: {error}")

    if not spapi_oauth_code:
        raise HTTPException(status_code=400, detail="No se recibió spapi_oauth_code")

    if not state or not _verify_amazon_state(state):
        raise HTTPException(status_code=400, detail="State inválido — posible CSRF")

    # selling_partner_id puede ser None si el vendedor solo tiene una cuenta
    # En ese caso usamos el AMAZON_SELLER_ID de .env como fallback
    effective_seller_id = selling_partner_id or AMAZON_SELLER_ID
    if not effective_seller_id:
        raise HTTPException(
            status_code=400,
            detail="No se pudo determinar el Seller ID. Configurar AMAZON_SELLER_ID en .env"
        )

    # ── Intercambiar código por tokens ──────────────────────────────────
    # Amazon LWA token endpoint (igual para todos los marketplaces)
    async with httpx.AsyncClient(timeout=15) as http:
        token_resp = await http.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "authorization_code",
                "code": spapi_oauth_code,
                "redirect_uri": AMAZON_REDIRECT_URI,
                "client_id": AMAZON_CLIENT_ID,
                "client_secret": AMAZON_CLIENT_SECRET,
            },
        )

        if token_resp.status_code != 200:
            error_data = token_resp.json()
            raise HTTPException(
                status_code=400,
                detail=f"Error al obtener tokens Amazon: {error_data.get('error_description', token_resp.text)}"
            )

        token_data = token_resp.json()

    refresh_token = token_data.get("refresh_token", "")
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="Amazon no devolvió refresh_token. Verificar que la app tenga scope offline_access."
        )

    logger.info(f"[Amazon OAuth] Tokens obtenidos para seller {effective_seller_id}")

    # ── Guardar en DB ────────────────────────────────────────────────────
    await token_store.save_amazon_account(
        seller_id=effective_seller_id,
        nickname=AMAZON_NICKNAME or "VECKTOR IMPORTS",
        client_id=AMAZON_CLIENT_ID,
        client_secret=AMAZON_CLIENT_SECRET,
        refresh_token=refresh_token,
        marketplace_id=AMAZON_MARKETPLACE_ID,
        marketplace_name=AMAZON_MARKETPLACE_NAME,
        app_solution_id=AMAZON_APP_SOLUTION_ID,
    )

    # ── Persistir refresh_token en .env.production (Railway) ────────────
    # Mismo patrón que MeLi: guardamos el refresh_token para que el server
    # pueda arrancar con él aunque la DB esté vacía (Railway ephemeral storage)
    if refresh_token:
        import re as _re, os as _os
        for env_file in (".env.production", ".env"):
            env_path = _os.path.join(
                _os.path.dirname(_os.path.dirname(__file__)), env_file
            )
            if not _os.path.exists(env_path):
                continue
            try:
                text = open(env_path, encoding="utf-8").read()
                # Actualizar o agregar AMAZON_REFRESH_TOKEN
                if "AMAZON_REFRESH_TOKEN=" in text:
                    text = _re.sub(
                        r"(?m)^AMAZON_REFRESH_TOKEN=.*$",
                        f"AMAZON_REFRESH_TOKEN={refresh_token}",
                        text,
                    )
                else:
                    text += f"\nAMAZON_REFRESH_TOKEN={refresh_token}\n"
                open(env_path, "w", encoding="utf-8").write(text)
                logger.info(f"[Amazon OAuth] refresh_token guardado en {env_file}")
            except Exception as e:
                logger.warning(f"[Amazon OAuth] No se pudo actualizar {env_file}: {e}")

    # ── Redirigir al dashboard Amazon ───────────────────────────────────
    # Seteamos la cookie active_amazon_id para que el dashboard Amazon
    # quede seleccionado automáticamente tras la re-autorización.
    response = RedirectResponse(url="/amazon", status_code=303)
    response.set_cookie("active_amazon_id", effective_seller_id, max_age=86400 * 30, httponly=False)
    return response


@router.post("/amazon/disconnect")
async def amazon_disconnect(request: Request):
    """
    Desconecta una cuenta de Amazon eliminando sus credenciales de la DB.

    No revoca el token en Amazon (el vendedor tendría que hacerlo en
    Seller Central → Apps → Manage Authorizations).
    """
    form = await request.form()
    seller_id = form.get("seller_id", "")
    if seller_id:
        await token_store.delete_amazon_account(seller_id)
        logger.info(f"[Amazon] Cuenta desconectada: {seller_id}")
    return RedirectResponse(url="/dashboard", status_code=303)
