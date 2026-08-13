import hashlib, hmac, base64, json, time, os, sys

# FIX 2026-08-11: el payload usaba "sub"/"role" sueltos, que NO coinciden con
# el shape real de _jwt_verify/create_session (user_store.py) -- uid/username/
# dn/role/mcp/sec. Esto escondia el bug real de user.get("sub") en main.py/
# health.py (siempre caia a "?" en produccion) porque el JWT local de prueba
# SI tenia "sub" y por casualidad coincidia con el codigo viejo (tambien
# roto). Ahora simula una sesion real para que las pruebas locales detecten
# este tipo de bug en vez de esconderlo.
#
# FIX 2026-08-13: usa la MISMA SECRET_KEY resuelta por app.config (antes
# tenía su propio fallback hardcodeado, tracked en el repo público).
from app.config import SECRET_KEY as key
username = sys.argv[1] if len(sys.argv) > 1 else "admin"
payload = {
    "uid": 1, "username": username, "dn": username, "role": "admin",
    "mcp": 0, "sec": [], "exp": int(time.time()) + 86400 * 30,
}
body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
sig = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
print(f"{body}.{sig}")
