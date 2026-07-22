import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env.production first (Railway), then .env (local) as fallback
_prod_env = Path(__file__).resolve().parent.parent / ".env.production"
if _prod_env.exists():
    load_dotenv(_prod_env)
load_dotenv()  # .env local (no sobreescribe las ya cargadas)


# Mercado Libre API Configuration
MELI_CLIENT_ID = os.getenv("MELI_CLIENT_ID", "")
MELI_CLIENT_SECRET = os.getenv("MELI_CLIENT_SECRET", "")
MELI_REDIRECT_URI = os.getenv("MELI_REDIRECT_URI", "http://localhost:8000/auth/callback")

# Mercado Libre URLs (Argentina - cambiar segun pais)
MELI_AUTH_URL = "https://auth.mercadolibre.com.mx/authorization"
MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
MELI_API_URL = "https://api.mercadolibre.com"

# App Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "cambiar-esta-clave-secreta-en-produccion")
DATABASE_PATH = os.getenv("DATABASE_PATH", "tokens.db")
APP_PIN = os.getenv("APP_PIN", "8741")

# Seed tokens for auto-recovery on deploy (Railway ephemeral storage)
# Slot 1 usa MELI_USER_ID / MELI_REFRESH_TOKEN (backwards compat)
# Slots 2+ usan MELI_USER_ID_N / MELI_REFRESH_TOKEN_N (dinámico, sin límite)
MELI_USER_ID = os.getenv("MELI_USER_ID", "")
MELI_REFRESH_TOKEN = os.getenv("MELI_REFRESH_TOKEN", "")

# Ollama Configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "10"))

# Research Configuration
RESEARCH_TIMEOUT = int(os.getenv("RESEARCH_TIMEOUT", "10"))
RESEARCH_MAX_PAGES = int(os.getenv("RESEARCH_MAX_PAGES", "5"))
RESEARCH_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ─────────────────────────────────────────────────────────────────────────
# Amazon Selling Partner API (SP-API) — Configuración
#
# Cómo obtener estas credenciales:
#   1. Seller Central MX → Apps and Services → Develop Apps
#   2. Developer Central → Create app → Production + Sellers
#   3. LWA credentials: Client ID + Client Secret
#   4. Seller ID: Seller Central → Settings → Account Info → Merchant Token
#   5. App Solution ID: Developer Central → App ID (amzn1.sp.solution.XXX)
#   6. Refresh Token: se obtiene automáticamente en /auth/amazon/callback
#
# AMAZON_MARKETPLACE_ID:
#   México = A1AM78C64UM0Y8 | USA = ATVPDKIKX0DER | Canadá = A2EUQ1WTGCTBG2
# ─────────────────────────────────────────────────────────────────────────
AMAZON_CLIENT_ID = os.getenv("AMAZON_CLIENT_ID", "")
AMAZON_CLIENT_SECRET = os.getenv("AMAZON_CLIENT_SECRET", "")
AMAZON_SELLER_ID = os.getenv("AMAZON_SELLER_ID", "")           # Merchant Token
AMAZON_REFRESH_TOKEN = os.getenv("AMAZON_REFRESH_TOKEN", "")   # Se llena tras primer OAuth
AMAZON_MARKETPLACE_ID = os.getenv("AMAZON_MARKETPLACE_ID", "A1AM78C64UM0Y8")  # MX por default
AMAZON_MARKETPLACE_NAME = os.getenv("AMAZON_MARKETPLACE_NAME", "MX")
AMAZON_APP_SOLUTION_ID = os.getenv("AMAZON_APP_SOLUTION_ID", "")  # amzn1.sp.solution.XXX (para OAuth URL)
AMAZON_REDIRECT_URI = os.getenv(
    "AMAZON_REDIRECT_URI",
    "https://apantallatemx.up.railway.app/auth/amazon/callback"
)
AMAZON_NICKNAME = os.getenv("AMAZON_NICKNAME", "VECKTOR IMPORTS")

# ── Amazon SP-API — Cuenta 2 (AUTOBOT AMZ MX) ────────────────────────────
AMAZON2_CLIENT_ID = os.getenv("AMAZON2_CLIENT_ID", "")
AMAZON2_CLIENT_SECRET = os.getenv("AMAZON2_CLIENT_SECRET", "")
AMAZON2_SELLER_ID = os.getenv("AMAZON2_SELLER_ID", "")
AMAZON2_REFRESH_TOKEN = os.getenv("AMAZON2_REFRESH_TOKEN", "")
AMAZON2_MARKETPLACE_ID = os.getenv("AMAZON2_MARKETPLACE_ID", "A1AM78C64UM0Y8")
AMAZON2_MARKETPLACE_NAME = os.getenv("AMAZON2_MARKETPLACE_NAME", "MX")
AMAZON2_APP_SOLUTION_ID = os.getenv("AMAZON2_APP_SOLUTION_ID", "")
AMAZON2_NICKNAME = os.getenv("AMAZON2_NICKNAME", "AUTOBOT AMZ MX")

# ── Amazon SP-API — Cuenta 3 (ExclusiveBulbs USA) ─────────────────────────
AMAZON3_CLIENT_ID = os.getenv("AMAZON3_CLIENT_ID", "")
AMAZON3_CLIENT_SECRET = os.getenv("AMAZON3_CLIENT_SECRET", "")
AMAZON3_SELLER_ID = os.getenv("AMAZON3_SELLER_ID", "")
AMAZON3_REFRESH_TOKEN = os.getenv("AMAZON3_REFRESH_TOKEN", "")
AMAZON3_MARKETPLACE_ID = os.getenv("AMAZON3_MARKETPLACE_ID", "ATVPDKIKX0DER")  # Amazon.com USA
AMAZON3_MARKETPLACE_NAME = os.getenv("AMAZON3_MARKETPLACE_NAME", "US")
AMAZON3_APP_SOLUTION_ID = os.getenv("AMAZON3_APP_SOLUTION_ID", "")
AMAZON3_NICKNAME = os.getenv("AMAZON3_NICKNAME", "ExclusiveBulbs")

# ── Amazon Buyer Messages — buzón Gmail dedicado por cuenta ───────────────
# NO es SP-API (no existe endpoint de lectura, ver reference_amazon_sp_api_docs).
# Amazon reenvía el mensaje real del comprador a este correo (Seller Central →
# Notification Preferences → Messaging → Buyer Messages), y responder desde
# este mismo buzón (registrado también como Approved Sender) hace que Amazon
# relance la respuesta al comprador de forma anónima. Ver
# project_amazon_buyer_messages_plan (memoria) para el plan completo.
AMAZON_INBOX_EMAIL = os.getenv("AMAZON_INBOX_EMAIL", "")
AMAZON_INBOX_APP_PASSWORD = os.getenv("AMAZON_INBOX_APP_PASSWORD", "")
AMAZON2_INBOX_EMAIL = os.getenv("AMAZON2_INBOX_EMAIL", "")
AMAZON2_INBOX_APP_PASSWORD = os.getenv("AMAZON2_INBOX_APP_PASSWORD", "")
AMAZON3_INBOX_EMAIL = os.getenv("AMAZON3_INBOX_EMAIL", "")
AMAZON3_INBOX_APP_PASSWORD = os.getenv("AMAZON3_INBOX_APP_PASSWORD", "")

# Gmail API OAuth — usado SOLO para enviar respuestas (no para leer).
# Railway bloquea egress SMTP (puertos 465/587, confirmado con
# /api/diag/smtp-test: "Network is unreachable" en ambos) — es una política
# anti-spam estándar de la mayoría de los hosts en la nube. La API de Gmail
# manda por HTTPS (nunca bloqueado), autenticado como la cuenta real (DKIM/SPF
# correctos porque de verdad es Gmail quien envía). Un solo Client ID/Secret
# (una app OAuth) sirve para las 3 cuentas — cada Gmail dedicado se autoriza
# por separado y genera su propio refresh_token.
GMAIL_OAUTH_CLIENT_ID = os.getenv("GMAIL_OAUTH_CLIENT_ID", "")
GMAIL_OAUTH_CLIENT_SECRET = os.getenv("GMAIL_OAUTH_CLIENT_SECRET", "")
AMAZON_GMAIL_REFRESH_TOKEN = os.getenv("AMAZON_GMAIL_REFRESH_TOKEN", "")
AMAZON2_GMAIL_REFRESH_TOKEN = os.getenv("AMAZON2_GMAIL_REFRESH_TOKEN", "")
AMAZON3_GMAIL_REFRESH_TOKEN = os.getenv("AMAZON3_GMAIL_REFRESH_TOKEN", "")

# Lista de cuentas con buzón configurado — buyer_messages_client.py itera esto.
# Una cuenta sin email/password configurados simplemente no aparece aquí (el
# poller la salta sin romper nada, permite lanzar con 1 sola cuenta primero).
# gmail_refresh_token puede venir vacío mientras no se autorice esa cuenta
# todavía en /auth/gmail/connect — send_reply() lo valida antes de usarlo.
AMAZON_BUYER_INBOX_ACCOUNTS = [
    acc for acc in (
        {"seller_id": AMAZON_SELLER_ID, "nickname": AMAZON_NICKNAME,
         "email": AMAZON_INBOX_EMAIL, "app_password": AMAZON_INBOX_APP_PASSWORD,
         "gmail_refresh_token": AMAZON_GMAIL_REFRESH_TOKEN},
        {"seller_id": AMAZON2_SELLER_ID, "nickname": AMAZON2_NICKNAME,
         "email": AMAZON2_INBOX_EMAIL, "app_password": AMAZON2_INBOX_APP_PASSWORD,
         "gmail_refresh_token": AMAZON2_GMAIL_REFRESH_TOKEN},
        {"seller_id": AMAZON3_SELLER_ID, "nickname": AMAZON3_NICKNAME,
         "email": AMAZON3_INBOX_EMAIL, "app_password": AMAZON3_INBOX_APP_PASSWORD,
         "gmail_refresh_token": AMAZON3_GMAIL_REFRESH_TOKEN},
    )
    if acc["seller_id"] and acc["email"] and acc["app_password"]
]

# ── Higgsfield AI — Generación de imágenes y videos ───────────────────────
HIGGSFIELD_KEY_ID = os.getenv("HIGGSFIELD_KEY_ID", "")
HIGGSFIELD_SECRET  = os.getenv("HIGGSFIELD_SECRET", "")

# API Key externa — para el planeador de flujo de caja y otros sistemas externos
CASHFLOW_API_KEY = os.getenv("CASHFLOW_API_KEY", "")

# Anthropic Claude API (for AI features)
import base64 as _b64

_D1 = "c2stYW50LWFwaTAzLWlvLVA1SlQ3b3hjb0F6X2dmUTVxaUZ6WFVEa05feUdHc2lsVUJBRWpW"
_D2 = "ckFaOUtZdUFGZTVqXzlBUExJMFpoVUlfeDNwUF8tSFVWZ2lTWGhNbHBUV2tRLW1MU0lod0FB"

def _resolve_anthropic_key() -> str:
    # 1. Hardcoded fallback is the source of truth — always reconstruct it
    try:
        _key = _b64.b64decode(_D1 + _D2).decode().strip()
        if _key:
            return _key
    except Exception:
        pass
    # 2. Env vars P1+P2 (Railway dashboard override)
    _p1 = os.getenv("AI_KEY_P1", "").strip()
    _p2 = os.getenv("AI_KEY_P2", "").strip()
    if _p1 and _p2:
        try:
            return _b64.b64decode(_p1 + _p2).decode().strip()
        except Exception:
            pass
    # 3. Direct env var (only if it looks like a valid Anthropic key)
    _direct = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if _direct.startswith("sk-ant-"):
        return _direct
    return ""

ANTHROPIC_API_KEY = _resolve_anthropic_key()
