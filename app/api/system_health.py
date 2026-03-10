"""
system_health.py — Monitor automático del estado del sistema


Verifica cada 30 minutos:
  1. db          — SQLite operacional
  2. meli_tokens — Tokens MeLi válidos por cuenta
  3. binmanager  — BinManager API accesible
  4. stock_sync  — Sync de stock corrió en las últimas 6 horas
  5. revenue     — Endpoint de métricas responde con datos
  6. amazon      — Tokens Amazon válidos (si hay cuenta)
  7. endpoints   — Páginas críticas responden 200

Endpoints:
  GET  /api/system-health/status      → JSON con todos los checks
  GET  /api/system-health/widget      → HTML widget para el dashboard
  POST /api/system-health/run         → Ejecutar checks ahora
"""

import asyncio
import os
import time
import httpx
from datetime import datetime, timedelta
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse


def _str_token(tok) -> str:
    """Convierte token a string limpio — evita 'Illegal header value b\'Bearer\''.
    Maneja dos casos:
      1. tok es bytes reales  →  decodifica a utf-8
      2. tok es string "b'xxx'"  →  extrae el contenido entre comillas
    """
    if isinstance(tok, bytes):
        tok = tok.decode("utf-8", errors="ignore")
    tok = (tok or "").strip()
    # Caso 2: string que parece bytes por str(b'...') — ej: "b'eyJ0eXAi...'"
    if tok.startswith("b'") and tok.endswith("'"):
        tok = tok[2:-1]
    elif tok.startswith('b"') and tok.endswith('"'):
        tok = tok[2:-1]
    return tok

router = APIRouter(prefix="/api/system-health", tags=["system-health"])

# ─── Estado global en memoria ────────────────────────────────────────────────
_INTERVAL = 10 * 60   # 10 minutos
_TIMEOUT  = 10.0      # segundos por check

_state: dict = {
    "last_run":  None,
    "running":   False,
    "overall":   "unknown",   # "ok" | "warning" | "error" | "unknown"
    "checks": {
        "db":          {"status": "unknown", "msg": "Sin datos", "ms": 0},
        "meli_tokens": {"status": "unknown", "msg": "Sin datos", "ms": 0},
        "binmanager":  {"status": "unknown", "msg": "Sin datos", "ms": 0},
        "stock_sync":  {"status": "unknown", "msg": "Sin datos", "ms": 0},
        "revenue":     {"status": "unknown", "msg": "Sin datos", "ms": 0},
        "amazon":      {"status": "unknown", "msg": "Sin datos", "ms": 0},
        "endpoints":   {"status": "unknown", "msg": "Sin datos", "ms": 0},
    },
}

_STATUS_PRIORITY = {"error": 3, "warning": 2, "ok": 1, "unknown": 0}


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _ok(msg: str, ms: int) -> dict:
    return {"status": "ok", "msg": msg, "ms": ms}


def _warn(msg: str, ms: int) -> dict:
    return {"status": "warning", "msg": msg, "ms": ms}


def _err(msg: str, ms: int) -> dict:
    return {"status": "error", "msg": msg, "ms": ms}


# ─── Checks individuales ─────────────────────────────────────────────────────

async def _check_db() -> dict:
    t0 = time.monotonic()
    try:
        from app.config import DATABASE_PATH
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute("SELECT count(*) FROM tokens")
            row = await cursor.fetchone()
            count = row[0] if row else 0
        return _ok(f"{count} cuentas MeLi en DB", _elapsed_ms(t0))
    except Exception as e:
        return _err(f"DB error: {str(e)[:80]}", _elapsed_ms(t0))


async def _check_meli_tokens() -> dict:
    """Usa get_meli_client() que maneja tokens internamente — evita problemas de encoding."""
    t0 = time.monotonic()
    try:
        from app.services import token_store
        from app.services.meli_client import get_meli_client
        accounts = await token_store.get_all_tokens()
        if not accounts:
            return _warn("Sin cuentas MeLi registradas", _elapsed_ms(t0))
        ok_count = 0
        fail_msgs = []
        for acc in accounts:
            uid = acc.get("user_id", "")
            nickname = acc.get("nickname", uid)
            try:
                client = await get_meli_client(user_id=uid)
                # Llamada liviana: solo verifica que el token funciona
                result = await client.get(f"/users/{uid}", params={"attributes": "id,nickname"})
                if result.get("id") or result.get("nickname"):
                    ok_count += 1
                else:
                    fail_msgs.append(f"{nickname}:RESP_VACIA")
            except Exception as e:
                code = getattr(e, "status_code", None)
                if code == 401:
                    fail_msgs.append(f"{nickname}:TOKEN_EXPIRADO")
                else:
                    fail_msgs.append(f"{nickname}:{str(e)[:40]}")
        ms = _elapsed_ms(t0)
        if fail_msgs:
            return _warn(f"{ok_count}/{len(accounts)} OK — {', '.join(fail_msgs)}", ms)
        return _ok(f"{ok_count}/{len(accounts)} cuentas con token válido", ms)
    except Exception as e:
        return _err(f"Error: {str(e)[:80]}", _elapsed_ms(t0))


async def _check_binmanager() -> dict:
    t0 = time.monotonic()
    BM_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            # Usamos un SKU conocido (liviano — solo ping al API)
            r = await http.post(BM_URL, json={
                "COMPANYID": 1, "TYPEINVENTORY": 0, "WAREHOUSEID": None,
                "LOCATIONID": "47,62,68", "BINID": None,
                "PRODUCTSKU": "PING_TEST", "CONDITION": "GRA",
                "SUPPLIERS": None, "LCN": None, "SEARCH": "PING_TEST"
            })
        ms = _elapsed_ms(t0)
        # BM devuelve [] para SKU desconocido (200 OK) — eso es suficiente para confirmar acceso
        if r.status_code in (200, 204):
            return _ok(f"BinManager accesible ({ms}ms)", ms)
        return _warn(f"BinManager respondió {r.status_code}", ms)
    except httpx.TimeoutException:
        return _warn(f"BinManager timeout (>{_TIMEOUT}s)", _elapsed_ms(t0))
    except Exception as e:
        return _err(f"BinManager error: {str(e)[:80]}", _elapsed_ms(t0))


async def _check_stock_sync() -> dict:
    t0 = time.monotonic()
    try:
        from app.services import token_store
        accounts = await token_store.get_all_tokens()
        if not accounts:
            return _warn("Sin cuentas para verificar sync", _elapsed_ms(t0))
        ok_users, stale_users, no_run = [], [], []
        for acc in accounts:
            uid = acc.get("user_id", "")
            nick = acc.get("nickname", uid)
            status = await token_store.get_sync_status(uid)
            if not status or not status.get("last_run"):
                no_run.append(nick)
                continue
            # Verificar que corrió en las últimas 6 horas
            try:
                last = datetime.fromisoformat(status["last_run"])
                if datetime.utcnow() - last < timedelta(hours=6):
                    ok_users.append(nick)
                else:
                    stale_users.append(nick)
            except Exception:
                stale_users.append(nick)
        ms = _elapsed_ms(t0)
        if no_run and not ok_users:
            return _warn(f"Sync aún no corrió para: {', '.join(no_run)}", ms)
        if stale_users:
            return _warn(f"Sync desactualizado: {', '.join(stale_users)}", ms)
        return _ok(f"Sync reciente para {len(ok_users)} cuenta(s)", ms)
    except Exception as e:
        return _err(f"Error: {str(e)[:80]}", _elapsed_ms(t0))


async def _check_revenue() -> dict:
    """Verifica que la API de órdenes responde usando get_meli_client()."""
    t0 = time.monotonic()
    try:
        from app.services import token_store
        from app.services.meli_client import get_meli_client
        accounts = await token_store.get_all_tokens()
        if not accounts:
            return _warn("Sin cuentas para verificar revenue", _elapsed_ms(t0))
        uid = accounts[0].get("user_id", "")
        client = await get_meli_client(user_id=uid)
        date_from = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000Z")
        result = await client.get("/orders/search", params={
            "seller": uid,
            "order.date_created.from": date_from,
            "order.status": "paid",
            "limit": 1,
        })
        ms = _elapsed_ms(t0)
        total = result.get("paging", {}).get("total", 0)
        return _ok(f"Orders API OK — {total} órdenes en 7d", ms)
    except Exception as e:
        code = getattr(e, "status_code", None)
        if code == 401:
            return _err("Token inválido para Orders API", _elapsed_ms(t0))
        return _err(f"Error: {str(e)[:80]}", _elapsed_ms(t0))


async def _check_amazon() -> dict:
    """Verifica Amazon haciendo un call REAL a SP-API — no solo crear el cliente."""
    t0 = time.monotonic()
    try:
        from app.services import token_store
        amazon_accounts = await token_store.get_all_amazon_accounts()
        if not amazon_accounts:
            return _ok("Sin cuentas Amazon configuradas", _elapsed_ms(t0))
        from app.services.amazon_client import get_amazon_client
        ok_count = 0
        fail_msgs = []
        for acc in amazon_accounts:
            sid = acc.get("seller_id", "")
            nick = acc.get("nickname", sid)
            try:
                client = await get_amazon_client(seller_id=sid)
                if not client:
                    fail_msgs.append(f"{nick}:SIN_CLIENT")
                    continue
                # Call real liviano — verifica que LWA token funciona de verdad
                result = await client.get_today_orders()
                # Si llega aquí sin excepción, el token funciona
                ok_count += 1
            except Exception as e:
                err_str = str(e)
                if "403" in err_str or "Unauthorized" in err_str or "LWA" in err_str:
                    fail_msgs.append(f"{nick}:TOKEN_INVALIDO(403)")
                elif "401" in err_str:
                    fail_msgs.append(f"{nick}:TOKEN_EXPIRADO(401)")
                else:
                    fail_msgs.append(f"{nick}:{err_str[:50]}")
        ms = _elapsed_ms(t0)
        if fail_msgs:
            joined = ", ".join(fail_msgs)
            return _err(f"{ok_count}/{len(amazon_accounts)} OK — {joined}", ms)
        return _ok(f"{ok_count} cuenta(s) Amazon operacional(es)", ms)
    except Exception as e:
        return _warn(f"Amazon check error: {str(e)[:60]}", _elapsed_ms(t0))


async def _check_endpoints() -> dict:
    """Smoke test: verifica que las páginas principales carguen."""
    t0 = time.monotonic()
    # Railway asigna $PORT dinámicamente; localmente suele ser 8000
    port = os.getenv("PORT", "8000")
    BASE = f"http://127.0.0.1:{port}"
    pages = ["/", "/dashboard", "/items"]
    fail = []
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as http:
            for page in pages:
                try:
                    r = await http.get(f"{BASE}{page}")
                    if r.status_code not in (200, 302):
                        fail.append(f"{page}:{r.status_code}")
                except Exception as e:
                    fail.append(f"{page}:ERR")
        ms = _elapsed_ms(t0)
        if fail:
            return _warn(f"Páginas con error: {', '.join(fail)}", ms)
        return _ok(f"{len(pages)} endpoints respondieron 200/302", ms)
    except Exception as e:
        return _err(f"Error: {str(e)[:80]}", _elapsed_ms(t0))


# ─── Runner principal ─────────────────────────────────────────────────────────

async def run_all_checks():
    """Ejecuta todos los checks en paralelo y actualiza _state."""
    global _state
    if _state["running"]:
        return
    _state["running"] = True
    try:
        results = await asyncio.gather(
            _check_db(),
            _check_meli_tokens(),
            _check_binmanager(),
            _check_stock_sync(),
            _check_revenue(),
            _check_amazon(),
            _check_endpoints(),
            return_exceptions=True,
        )
        keys = ["db", "meli_tokens", "binmanager", "stock_sync", "revenue", "amazon", "endpoints"]
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                _state["checks"][key] = _err(str(result)[:100], 0)
            else:
                _state["checks"][key] = result

        # Calcular overall
        statuses = [c["status"] for c in _state["checks"].values()]
        if "error" in statuses:
            _state["overall"] = "error"
        elif "warning" in statuses:
            _state["overall"] = "warning"
        else:
            _state["overall"] = "ok"

        _state["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        print(f"[HEALTH] Check completo — overall: {_state['overall']}")
    finally:
        _state["running"] = False


async def _health_loop():
    """Loop background: corre checks cada 10 minutos."""
    await asyncio.sleep(90)  # Esperar 90s al arranque
    while True:
        await run_all_checks()
        await asyncio.sleep(_INTERVAL)


def start_health_check_loop():
    asyncio.create_task(_health_loop())


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_health_status():
    """Retorna el estado actual de todos los checks como JSON."""
    return _state


@router.post("/run")
async def trigger_health_check():
    """Dispara los checks inmediatamente (no bloquea)."""
    asyncio.create_task(run_all_checks())
    return {"status": "triggered"}


@router.post("/fix-tokens")
async def fix_meli_tokens():
    """Re-siembra los tokens MeLi desde .env.production — resuelve tokens expirados o corruptos."""
    try:
        from app.main import _seed_tokens
        asyncio.create_task(_seed_tokens())
        return {"status": "ok", "message": "Renovando tokens MeLi... verificando en 8 segundos"}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)[:100]}"}


@router.post("/fix-amazon")
async def fix_amazon_token():
    """Re-siembra la cuenta Amazon desde .env.production y verifica el token contra LWA."""
    try:
        from app.services.amazon_client import _seed_amazon_accounts, get_amazon_client
        from app.services import token_store

        # 1. Leer token del archivo y guardar en DB
        await _seed_amazon_accounts()

        # 2. Intentar obtener access_token real para confirmar que funciona
        accounts = await token_store.get_all_amazon_accounts()
        if not accounts:
            return {"status": "error", "message": "Sin cuentas Amazon — .env.production puede estar vacío"}

        seller_id = accounts[0].get("seller_id", "")
        client = await get_amazon_client(seller_id=seller_id)
        if not client:
            return {"status": "error", "message": "No se pudo crear cliente Amazon"}

        # Forzar refresh del access_token (invalida caché para que use el token nuevo del DB)
        client._access_token = None
        client._token_expires_at = 0
        await client._get_access_token()
        token_prefix = (client._access_token or "")[:20]
        return {
            "status": "ok",
            "message": f"Amazon token OK — acceso verificado ({seller_id}) | token: {token_prefix}...",
        }
    except Exception as e:
        err = str(e)
        if "expired" in err.lower() or "invalid" in err.lower() or "403" in err:
            return {"status": "error", "message": f"Token inválido en .env.production: {err[:120]}"}
        return {"status": "error", "message": f"Error: {err[:120]}"}


_ICON = {
    "ok":      ('<svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
                '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>'),
    "warning": ('<svg class="w-4 h-4 text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
                '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
                'd="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>'),
    "error":   ('<svg class="w-4 h-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
                '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
                'd="M6 18L18 6M6 6l12 12"/></svg>'),
    "unknown": ('<svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
                '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
                'd="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>'),
}

_LABEL = {
    "db":          "Base de datos",
    "meli_tokens": "Tokens MeLi",
    "binmanager":  "BinManager",
    "stock_sync":  "Sync de Stock",
    "revenue":     "Orders API",
    "amazon":      "Amazon SP-API",
    "endpoints":   "Páginas web",
}

_BADGE_COLOR = {
    "ok":      "bg-green-100 text-green-700",
    "warning": "bg-yellow-100 text-yellow-700",
    "error":   "bg-red-100 text-red-700",
    "unknown": "bg-gray-100 text-gray-500",
}

_OVERALL_COLOR = {
    "ok":      "border-green-400",
    "warning": "border-yellow-400",
    "error":   "border-red-500",
    "unknown": "border-gray-300",
}


@router.get("/widget", response_class=HTMLResponse)
async def health_widget():
    """HTML widget para insertar en el dashboard."""
    overall = _state["overall"]
    last = _state.get("last_run") or "Nunca"
    running = _state.get("running", False)

    # Botones de acción para checks con error/warning
    _FIX_ACTIONS = {
        "meli_tokens": ("Refrescar tokens MeLi", "fixMeliTokens()"),
        "revenue":     ("Refrescar tokens MeLi", "fixMeliTokens()"),
        "stock_sync":  ("Sync ahora", "fixStockSync()"),
        "amazon":      ("Reconectar Amazon", "fixAmazon()"),
        "endpoints":   (None, None),
        "binmanager":  (None, None),
        "db":          (None, None),
    }

    rows_html = ""
    for key, label in _LABEL.items():
        check = _state["checks"].get(key, {"status": "unknown", "msg": "", "ms": 0})
        st = check["status"]
        icon = _ICON.get(st, _ICON["unknown"])
        badge_cls = _BADGE_COLOR.get(st, _BADGE_COLOR["unknown"])
        ms_str = f"{check['ms']}ms" if check.get("ms") else ""
        msg = check.get("msg", "")

        # Botón de acción solo si hay error/warning Y existe una acción
        action_btn = ""
        if st in ("error", "warning"):
            action_label, action_fn = _FIX_ACTIONS.get(key, (None, None))
            if action_label and action_fn:
                action_btn = (
                    f'<button onclick="{action_fn}" '
                    f'class="text-[10px] px-2 py-0.5 rounded bg-blue-500 text-white '
                    f'hover:bg-blue-600 font-medium flex-shrink-0 ml-1">'
                    f'{action_label}</button>'
                )

        rows_html += f"""
        <div class="flex items-start gap-2 py-1.5 border-b border-gray-100 last:border-0">
            <div class="mt-0.5 flex-shrink-0">{icon}</div>
            <div class="flex-1 min-w-0">
                <span class="text-xs font-medium text-gray-700">{label}</span>
                <span class="text-[10px] text-gray-400 ml-1">{ms_str}</span>
                <p class="text-[10px] text-gray-500 truncate" title="{msg}">{msg[:120]}</p>
            </div>
            <div class="flex items-center gap-1 flex-shrink-0">
                {action_btn}
                <span class="text-[10px] px-1.5 py-0.5 rounded font-medium {badge_cls}">
                    {st.upper()}
                </span>
            </div>
        </div>"""

    overall_label = {
        "ok": "Sistema OK",
        "warning": "Atencion requerida",
        "error": "Error detectado",
        "unknown": "Sin datos aun",
    }.get(overall, overall)

    overall_icon = {
        "ok":      "text-green-600",
        "warning": "text-yellow-600",
        "error":   "text-red-600",
        "unknown": "text-gray-400",
    }.get(overall, "text-gray-400")

    border = _OVERALL_COLOR.get(overall, "border-gray-300")
    spinner = ' <span class="animate-spin inline-block w-3 h-3 border-2 border-gray-400 border-t-transparent rounded-full"></span>' if running else ""

    return HTMLResponse(f"""
<div class="bg-white rounded-xl shadow border-l-4 {border} p-4" id="health-widget-inner">
    <div class="flex items-center justify-between mb-3">
        <div class="flex items-center gap-2">
            <h3 class="text-sm font-semibold text-gray-700">Estado del Sistema</h3>
            <span class="text-xs font-bold {overall_icon}">{overall_label}</span>
            {spinner}
        </div>
        <div class="flex items-center gap-2">
            <span class="text-[10px] text-gray-400" id="health-last-run">{last}</span>
            <button onclick="triggerHealthCheck(this)"
                    class="text-[10px] text-blue-500 hover:text-blue-700 underline">
                Verificar ahora
            </button>
        </div>
    </div>
    <div id="health-action-msg" class="hidden mb-2 text-xs text-blue-600 font-medium"></div>
    <div>{rows_html}</div>
</div>
<script>
function _healthReload() {{
    fetch('/api/system-health/widget')
        .then(function(r){{ return r.text(); }})
        .then(function(html){{
            var el = document.getElementById('system-health-widget');
            if (el) el.innerHTML = html;
        }}).catch(function(){{}});
}}
// Auto-polling cada 5 minutos — sin necesidad de clicks
if (!window._healthPollStarted) {{
    window._healthPollStarted = true;
    setInterval(function(){{ _healthReload(); }}, 5 * 60 * 1000);
}}
function triggerHealthCheck(btn) {{
    if (btn) {{ btn.textContent = 'Verificando...'; btn.disabled = true; }}
    var msg = document.getElementById('health-action-msg');
    if (msg) {{ msg.textContent = 'Ejecutando checks...'; msg.classList.remove('hidden'); }}
    fetch('/api/system-health/run', {{method:'POST'}})
        .then(function(){{
            setTimeout(function(){{ _healthReload(); }}, 7000);
        }}).catch(function(){{}});
}}
function fixMeliTokens() {{
    var msg = document.getElementById('health-action-msg');
    if (msg) {{ msg.textContent = 'Refrescando tokens MeLi...'; msg.classList.remove('hidden'); }}
    fetch('/api/system-health/fix-tokens', {{method:'POST'}})
        .then(function(r){{ return r.json(); }})
        .then(function(d){{
            if (msg) msg.textContent = d.message || 'Proceso iniciado. Verificando en 8s...';
            setTimeout(function(){{
                fetch('/api/system-health/run', {{method:'POST'}})
                    .then(function(){{ setTimeout(_healthReload, 7000); }});
            }}, 2000);
        }}).catch(function(){{}});
}}
function fixStockSync() {{
    var msg = document.getElementById('health-action-msg');
    if (msg) {{ msg.textContent = 'Iniciando sync de stock...'; msg.classList.remove('hidden'); }}
    fetch('/api/sync/trigger', {{method:'POST'}})
        .then(function(r){{ return r.json(); }})
        .then(function(d){{
            if (msg) msg.textContent = 'Sync iniciado. Verificando en 10s...';
            setTimeout(function(){{
                fetch('/api/system-health/run', {{method:'POST'}})
                    .then(function(){{ setTimeout(_healthReload, 7000); }});
            }}, 3000);
        }}).catch(function(){{}});
}}
function fixAmazon() {{
    var msg = document.getElementById('health-action-msg');
    if (msg) {{ msg.textContent = 'Reconectando Amazon desde .env.production...'; msg.classList.remove('hidden'); }}
    fetch('/api/system-health/fix-amazon', {{method:'POST'}})
        .then(function(r){{ return r.json(); }})
        .then(function(d){{
            if (msg) msg.textContent = d.message || 'Proceso completado.';
            if (d.status === 'ok') {{
                setTimeout(function(){{
                    fetch('/api/system-health/run', {{method:'POST'}})
                        .then(function(){{ setTimeout(_healthReload, 7000); }});
                }}, 1000);
            }}
        }}).catch(function(e){{
            if (msg) msg.textContent = 'Error al reconectar Amazon: ' + e;
        }});
}}
</script>
""")
