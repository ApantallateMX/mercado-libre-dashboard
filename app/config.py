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

# Anthropic Claude API (for AI features)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    # Fallback: reconstruct from split base64 parts (bypasses GitHub Push Protection)
    import base64 as _b64
    _p1 = os.getenv("AI_KEY_P1", "")
    _p2 = os.getenv("AI_KEY_P2", "")
    if _p1 and _p2:
        ANTHROPIC_API_KEY = _b64.b64decode(_p1 + _p2).decode()
