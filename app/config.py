import os
from dotenv import load_dotenv

load_dotenv()

# Debug: log env vars at startup (Railway)
print(f"[CONFIG] MELI_CLIENT_ID from env: '{os.environ.get('MELI_CLIENT_ID', '<NOT SET>')}'")
print(f"[CONFIG] All MELI_ env vars: {[k for k in os.environ if k.startswith('MELI_')]}")
print(f"[CONFIG] RAILWAY_ vars: {[k for k in os.environ if k.startswith('RAILWAY')]}")
print(f"[CONFIG] PORT: {os.environ.get('PORT', '<NOT SET>')}")
print(f"[CONFIG] Total env vars: {len(os.environ)}")

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
