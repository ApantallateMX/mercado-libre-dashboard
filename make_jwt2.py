import hashlib, hmac, base64, json, time, os, sys

key = os.getenv("SECRET_KEY") or "meli-dashboard-prod-2026-apantallate"
user_id = sys.argv[1] if len(sys.argv) > 1 else "admin"
payload = {"sub": user_id, "role": "admin", "exp": int(time.time()) + 86400 * 30}
body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
sig = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
print(f"{body}.{sig}")
