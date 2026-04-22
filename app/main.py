import asyncio
import json
import logging
import time as _time_module

logger = logging.getLogger(__name__)
from datetime import datetime, timedelta
from types import SimpleNamespace
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Query, UploadFile, File, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from app.config import MELI_USER_ID, MELI_REFRESH_TOKEN
from app.auth import router as auth_router
from app.api.orders import router as orders_router
from app.api.items import router as items_router
from app.api.metrics import router as metrics_router
from app.api.health import router as health_router
from app.api.sku_inventory import router as sku_inventory_router
from app.api.health_ai import router as health_ai_router
from app.api.amazon_products import router as amazon_products_router
from app.api.amazon_orders import router as amazon_orders_router
from app.api.users import router as users_router
from app.api.system_health import router as system_health_router
from app.api.v1.sales import router as sales_v1_router
from app.api.binmanager import router as binmanager_router
from app.api.lanzar import router as lanzar_router, start_gap_scan_loop
from app.api.productos import router as productos_router
from app.api.facturacion import router as facturacion_router
from app.services.price_monitor import price_monitor
from app.services import token_store
from app.services import user_store
from app.services.meli_client import get_meli_client, _active_user_id as _meli_user_id_ctx
from app import order_net_revenue
from app.services.sku_utils import base_sku as _normalize_sku_imported  # canónica — base_sku maneja bundles, sufijos y paréntesis

# ---------- SKU suffix helpers ----------
_GR_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC")
_IC_SUFFIXES = ("-ICB", "-ICC")
_ALL_SUFFIXES = _GR_SUFFIXES + _IC_SUFFIXES


def _extract_base_sku(sku: str) -> str:
    """Devuelve el SKU base sin sufijo de variante."""
    upper = sku.upper()
    for sfx in _ALL_SUFFIXES:
        if upper.endswith(sfx):
            return sku[:-len(sfx)]
    return sku


def _bm_conditions_for_sku(sku: str) -> str:
    """Retorna condiciones BM según formato del SELLER_SKU y categoría.

    Reglas:
    - SNTV* → GRA,GRB,GRC,ICB,ICC,NEW
      (televisiones pueden tener stock en condición In-Carton B/C en cualquier SKU base;
       el "/" de bundles se elimina al normalizar el SKU antes de llamar esta función,
       por lo que no se puede depender del sufijo para detectar si hay ICB/ICC)
    - Todo lo demás → GRA,GRB,GRC,NEW
      (simple, -NEW, -GRA, -GRB, -GRC, -ICB/-ICC fuera de SNTV, otras categorías)
    """
    upper = sku.upper()
    if upper.startswith("SNTV"):
        return "GRA,GRB,GRC,ICB,ICC,NEW"
    return "GRA,GRB,GRC,NEW"


import re as _re


def normalize_to_bm_sku(sku: str) -> str:
    """Proxy a sku_utils.base_sku — fuente canónica en app/services/sku_utils.py."""
    return _normalize_sku_imported(sku)


def _extract_key_attributes(body: dict) -> list:
    """Extract up to 20 key spec attributes from an ML item body for AI context."""
    raw = body.get("attributes", []) or []
    result = []
    _skip = {"SELLER_SKU", "GTIN", "EAN", "ITEM_CONDITION", "ALPHANUMERIC_MODEL", "BRAND"}
    for attr in raw:
        if len(result) >= 20:
            break
        aid = (attr.get("id") or "").upper()
        if aid in _skip:
            continue
        name = attr.get("name") or attr.get("id") or ""
        vs = attr.get("value_struct") or {}
        value = attr.get("value_name") or (str(vs.get("number", "")) if vs.get("number") is not None else "")
        unit = vs.get("unit") or ""
        if name and value:
            result.append({"name": name, "value": f"{value}{(' ' + unit) if unit else ''}"})
    return result


def _clean_sku_for_bm(sku: str) -> str:
    """Limpia SKU de MeLi para consultar BinManager.
    Quita: (N), / segunda_parte, + segunda_parte, espacios extra, etc."""
    if not sku:
        return ""
    # Tomar primera parte antes de " / " o " + " (MeLi concatena SKUs en packs)
    s = _re.split(r'\s*[/+]\s*', sku)[0].strip()
    # Quitar cualquier sufijo entre paréntesis: (2), (cantidad:2), (18), etc.
    s = _re.sub(r'\s*\([^)]*\)', '', s).strip()
    return s


_manual_fx_rate: float = 0.0  # 0 = usar tasa MeLi API; >0 = override manual


async def _get_usd_to_mxn(client) -> float:
    """Obtiene tipo de cambio USD->MXN. Prefiere override manual si está configurado."""
    if _manual_fx_rate > 0:
        return _manual_fx_rate
    try:
        fx_data = await client.get("/currency_conversions/search", params={"from": "USD", "to": "MXN"})
        return fx_data.get("ratio", 20.0)
    except Exception:
        return 20.0


def _ml_fee(price: float) -> float:
    """Tarifa ML escalonada por precio (más precisa que flat 17%)."""
    if price >= 5000:
        return 0.12
    if price >= 1500:
        return 0.14
    if price >= 500:
        return 0.16
    return 0.18


def _calc_margins(products: list, usd_to_mxn: float):
    """Calcula costos, márgenes y comparativas vs RetailPrice PH para cada producto."""
    for p in products:
        avg_cost = p.get("_bm_avg_cost", 0) or 0
        retail = p.get("_bm_retail_price", 0) or 0
        retail_ph = p.get("_bm_retail_ph", 0) or 0

        # Flag: BM tiene registro aunque costos sean sentinel (0 o 9999)
        p["_bm_has_data"] = bool(p.get("_bm_brand") or avg_cost > 0 or retail > 0)

        # Costo efectivo: AvgCost tiene prioridad; RetailPrice de BM = precio de adquisición (fallback)
        _eff_cost = avg_cost if (0 < avg_cost < 9999) else retail
        p["_bm_eff_cost_usd"] = round(_eff_cost, 2) if (0 < _eff_cost < 9999) else 0
        p["_cost_source"] = "avg" if (0 < avg_cost < 9999) else ("retail" if (0 < retail < 9999) else None)

        # Conversiones USD → MXN
        p["_costo_mxn"] = round(_eff_cost * usd_to_mxn, 2) if (0 < _eff_cost < 9999) else 0
        p["_retail_mxn"] = round(retail * usd_to_mxn, 2) if (0 < retail < 9999) else 0
        p["_retail_ph_mxn"] = round(retail_ph * usd_to_mxn, 2) if (0 < retail_ph < 9999) else 0

        price = p.get("price", 0)

        # ── Ganancia/margen vs precio de venta actual ──────────────────────
        if price > 0 and p["_costo_mxn"] > 0:
            comision = price * _ml_fee(price)
            iva_comision = comision * 0.16
            envio = 150
            ganancia = price - p["_costo_mxn"] - comision - iva_comision - envio
            p["_ganancia_est"] = round(ganancia, 2)
            p["_margen_pct"] = round((ganancia / price) * 100, 1)
        else:
            p["_ganancia_est"] = None
            p["_margen_pct"] = None

        # ── Aportación MeLi (PRE_NEGOTIATED) — ML subsidia parte del descuento ──
        _meli_pct = p.get("_meli_promo_pct", 0) or 0
        _orig_p = p.get("original_price", 0) or 0
        p["_meli_contribution_mxn"] = round(_orig_p * _meli_pct / 100, 2) if (_meli_pct > 0 and _orig_p > 0) else 0
        # Ganancia real = ganancia_est + lo que ML aporta (gratis para el vendedor)
        if p["_ganancia_est"] is not None:
            p["_ganancia_real"] = round(p["_ganancia_est"] + p["_meli_contribution_mxn"], 2)
            _eff_price = price + p["_meli_contribution_mxn"]
            p["_margen_real_pct"] = round((p["_ganancia_real"] / _eff_price) * 100, 1) if _eff_price > 0 else None
        else:
            p["_ganancia_real"] = None
            p["_margen_real_pct"] = None

        # ── Comparativa vs RetailPrice PH ──────────────────────────────────
        rph = p["_retail_ph_mxn"]
        if rph > 0:
            # % diferencia entre precio actual de venta y Retail PH
            # > 0 → vendiendo sobre PH (bueno), < 0 → bajo PH (riesgo)
            p["_vs_retail_ph_pct"] = round((price / rph - 1) * 100, 1) if price > 0 else None

            # Precio sugerido: RetailPrice PH + 15% mínimo de margen sobre PH
            p["_precio_sugerido_ph"] = round(rph * 1.15, 2)

            # ROI potencial: (PH - costo) / costo × 100
            costo = p["_costo_mxn"]
            p["_roi_pct"] = round((rph - costo) / costo * 100, 1) if costo > 0 else None

            # Margen neto si se vendiera al precio PH
            if p["_costo_mxn"] > 0:
                comision_ph = rph * _ml_fee(rph)
                iva_ph = comision_ph * 0.16
                ganancia_ph = rph - p["_costo_mxn"] - comision_ph - iva_ph - 150
                p["_margen_ph_pct"] = round((ganancia_ph / rph) * 100, 1)
            else:
                p["_margen_ph_pct"] = None
        else:
            p["_vs_retail_ph_pct"] = None
            p["_precio_sugerido_ph"] = None
            p["_roi_pct"] = None
            p["_margen_ph_pct"] = None

        # Precio piso: mínimo para lograr 15% de margen después de comisión MeLi
        costo = p["_costo_mxn"]
        if costo > 0:
            # precio_piso = costo / (1 - fee*1.16 - margen_obj); fee dinámico según bracket
            # Estimación inicial → refinar una vez
            _fee0 = _ml_fee(costo * 1.6)
            _f0 = 1 - _fee0 * 1.16 - 0.15
            _piso0 = costo / _f0 if _f0 > 0 else costo * 3
            _fee1 = _ml_fee(_piso0)
            _f1 = 1 - _fee1 * 1.16 - 0.15
            p["_precio_piso"] = round(costo / _f1, 0) if _f1 > 0 else None
        else:
            p["_precio_piso"] = None


async def _seed_one(user_id: str, refresh_token: str, label: str):
    """Intenta recuperar tokens para una cuenta via refresh_token.
    También obtiene el nickname desde la API de MeLi para mostrarlo en el dropdown."""
    import httpx
    from app.config import MELI_TOKEN_URL, MELI_CLIENT_ID, MELI_CLIENT_SECRET, MELI_API_URL
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(MELI_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": MELI_CLIENT_ID,
                "client_secret": MELI_CLIENT_SECRET,
                "refresh_token": refresh_token,
            })
            if resp.status_code == 200:
                data = resp.json()
                access_token = data["access_token"]
                # Obtener nickname desde MeLi API
                nickname = ""
                try:
                    me_resp = await client.get(
                        f"{MELI_API_URL}/users/{user_id}",
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    if me_resp.status_code == 200:
                        nickname = me_resp.json().get("nickname", "")
                except Exception:
                    pass
                await token_store.save_tokens(
                    user_id,
                    access_token,
                    data["refresh_token"],
                    data.get("expires_in", 21600),
                    nickname=nickname,
                )
                print(f"[SEED] Tokens recovered for {label} (user {user_id}, nickname={nickname})")
            else:
                print(f"[SEED] Token refresh failed for {label}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[SEED] Error recovering tokens for {label}: {e}")


async def _backfill_nickname(user_id: str, access_token: str):
    """Rellena el nickname de una cuenta existente en DB que no lo tiene."""
    import httpx
    from app.config import MELI_API_URL
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{MELI_API_URL}/users/{user_id}",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if resp.status_code == 200:
                nickname = resp.json().get("nickname", "")
                if nickname:
                    await token_store.update_nickname(user_id, nickname)
                    print(f"[SEED] Nickname actualizado: {user_id} → {nickname}")
    except Exception as e:
        print(f"[SEED] Error obteniendo nickname para {user_id}: {e}")


def _parse_env_slots(env_vars: dict) -> list:
    """Devuelve lista de (uid, rt, label) para todos los slots encontrados en env_vars.
    Slot 1: MELI_USER_ID / MELI_REFRESH_TOKEN
    Slot N: MELI_USER_ID_N / MELI_REFRESH_TOKEN_N (sin límite)"""
    accounts = []
    uid = env_vars.get("MELI_USER_ID", "") or MELI_USER_ID
    rt = env_vars.get("MELI_REFRESH_TOKEN", "") or MELI_REFRESH_TOKEN
    if uid and rt:
        accounts.append((uid, rt, "cuenta1"))
    n = 2
    while True:
        uid = env_vars.get(f"MELI_USER_ID_{n}", "")
        rt = env_vars.get(f"MELI_REFRESH_TOKEN_{n}", "")
        if not uid or not rt:
            break
        accounts.append((uid, rt, f"cuenta{n}"))
        n += 1
    return accounts


async def _seed_tokens():
    """Auto-recover MeLi tokens. Lee Railway env vars primero, luego .env.production
    como fallback. Soporta N cuentas dinámicamente (cuenta1, cuenta2, cuenta3, ...)."""
    import os as _os
    from pathlib import Path as _Path

    # Leer .env.production como fallback (puede no existir en Railway)
    file_vars = {}
    env_file = _Path(__file__).resolve().parent.parent / ".env.production"
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                file_vars[k.strip()] = v.strip()

    # Railway env vars tienen prioridad sobre el archivo
    env_vars = {**file_vars}
    for key in ("MELI_USER_ID", "MELI_REFRESH_TOKEN",
                "MELI_USER_ID_2", "MELI_REFRESH_TOKEN_2",
                "MELI_USER_ID_3", "MELI_REFRESH_TOKEN_3",
                "MELI_USER_ID_4", "MELI_REFRESH_TOKEN_4",
                "MELI_USER_ID_5", "MELI_REFRESH_TOKEN_5"):
        val = _os.getenv(key)
        if val:
            env_vars[key] = val

    for uid, rt, label in _parse_env_slots(env_vars):
        existing = await token_store.get_tokens(uid)
        if not existing:
            await _seed_one(uid, rt, label)
        else:
            # Si el token está expirado, refrescar usando el refresh_token del DB
            # (más actualizado que el del archivo de env)
            is_expired = await token_store.is_token_expired(uid)
            if is_expired:
                db_rt = existing.get("refresh_token") or rt
                print(f"[SEED] Token expirado para {label} — refrescando...")
                await _seed_one(uid, db_rt, label)
            elif not existing.get("nickname"):
                # Cuenta existente sin nickname — rellenar desde MeLi API
                await _backfill_nickname(uid, existing.get("access_token", ""))

    # Backfill nickname para TODAS las cuentas en DB que aún no lo tienen
    # (incluye cuentas agregadas via OAuth que no están en env vars)
    try:
        import aiosqlite as _aio_seed
        from app.services.token_store import DATABASE_PATH as _DB_PATH_SEED
        async with _aio_seed.connect(_DB_PATH_SEED) as _db_seed:
            _db_seed.row_factory = _aio_seed.Row
            _cur = await _db_seed.execute(
                "SELECT user_id, access_token FROM tokens WHERE nickname IS NULL OR nickname = ''"
            )
            _missing = await _cur.fetchall()
        for _row in _missing:
            await _backfill_nickname(_row["user_id"], _row["access_token"] or "")
    except Exception as _e:
        print(f"[SEED] Backfill nicknames error: {_e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Inicializa la base de datos y siembra credenciales al arrancar el servidor.

    Orden de inicialización:
    1. init_db()           → Crea tablas si no existen (tokens, amazon_accounts, etc.)
    2. _seed_tokens()      → Siembra cuentas MeLi desde .env.production
    3. _seed_amazon_accounts() → Siembra cuentas Amazon desde .env.production
    """
    await token_store.init_db()
    await user_store.init_user_db()
    await _seed_tokens()
    # Sembrar cuentas Amazon desde .env.production (igual que MeLi)
    from app.services.amazon_client import _seed_amazon_accounts
    await _seed_amazon_accounts()
    # Sync periódico de Onsite (cada 25 min en background)
    from app.api.amazon_products import start_onsite_background_sync
    start_onsite_background_sync()
    # Sync periódico de stock MeLi vs BM (cada 4 horas) — alertas de sobreventa
    start_stock_sync()
    # Sync multi-plataforma BM → ML + Amazon (cada 5 min) — distribuye stock óptimo
    from app.services.stock_sync_multi import start_multi_stock_sync
    start_multi_stock_sync()
    # Auto-refresh de tokens MeLi cada 5 horas — evita expiración silenciosa
    start_token_refresh()
    # Health checker automático (cada 10 min) — verifica que todo el sistema funcione
    from app.api.system_health import start_health_check_loop
    start_health_check_loop()
    # Monitor de precios BinManager — detecta cambios en RetailPrice PH en vivo
    await price_monitor.start()
    # Cargar caché BM + stock_issues_cache desde DB de forma síncrona antes del prewarm.
    # await garantiza que los datos estén en memoria antes de que arranque _startup_prewarm
    # (que tiene 90s de delay, pero asyncio.create_task podría interleavearse de todas formas).
    await _load_bm_cache_from_db()
    await _load_stock_issues_from_db()
    # Pre-warm caches en background (90s delay — espera a que ml_listing_sync llene la DB primero)
    # Loop periódico: refresca cada 10 min para que el Stock tab nunca espere en frío.
    # Con la DB local de listings el prewarm tarda <10s en lugar de 130s+.
    async def _startup_prewarm():
        await asyncio.sleep(90)  # ml_listing_sync necesita ~60s para full sync inicial
        _auto_fail_streak = 0   # fallos consecutivos del loop automático
        while True:
            _prewarm_ok = False
            # Precalentar todas las cuentas — la deduplicación por normalize_to_bm_sku
            # garantiza que SKUs repetidos entre cuentas usen el cache existente.
            try:
                accounts = await token_store.get_all_tokens()
                for acc in accounts:
                    uid = acc.get("user_id", "")
                    if uid:
                        await _prewarm_caches(user_id=uid)
                        await asyncio.sleep(1)
                _prewarm_ok = True
                _auto_fail_streak = 0
            except Exception:
                _auto_fail_streak += 1
                logger.warning(f"[AUTO-PREWARM] Fallo #{_auto_fail_streak} en loop automático")
            # Refrescar alertas de sobreventa con datos BM actualizados
            if _prewarm_ok:
                try:
                    accounts = await token_store.get_all_tokens()
                    for acc in accounts:
                        uid = acc.get("user_id", "")
                        if uid:
                            await _run_stock_sync_for_user(uid)
                            await asyncio.sleep(2)
                except Exception:
                    pass
            # Retry rápido en fallo (2 min); ciclo normal 20 min
            _sleep = 120 if _auto_fail_streak > 0 else 1200
            await asyncio.sleep(_sleep)
    asyncio.create_task(_startup_prewarm())
    # Monitor de salud BinManager — chequea cada 2 min, fast-fail en prewarm si está caído
    asyncio.create_task(_bm_health_loop())
    # Lanzador Inteligente — scan nocturno BM vs MeLi (3am Mexico = 9am UTC)
    start_gap_scan_loop()
    # Sync incremental de listings ML → DB local (elimina spinner en Stock tab)
    from app.services.ml_listing_sync import start_ml_listing_sync, register_listings_updated_callback

    def _invalidate_products_on_sync(uid: str):
        """Limpia _products_cache cuando ml_listing_sync actualiza la DB.
        NO limpia _stock_issues_cache: el prewarm la reconstruye con datos frescos.
        Borrarla aquí causaba spinner en el tab Stock después de cada deploy."""
        uid_prefix = f"{uid}:"
        for k in [k for k in _products_cache if k.startswith(uid_prefix)]:
            del _products_cache[k]
        logger.debug(f"[ML-SYNC-CB] products_cache invalidado para uid={uid} post-sync")

    register_listings_updated_callback(_invalidate_products_on_sync)
    start_ml_listing_sync()
    # Sync de listings Amazon → DB local (descarga solo, sin modificar Amazon)
    from app.services.amazon_listing_sync import start_amazon_listing_sync
    start_amazon_listing_sync()
    # Callback post stock-sync: invalida _products_cache + _stock_issues_cache
    # para que el prewarm recalcule alertas con qty ML correctas (no stale)
    from app.services.stock_sync_multi import register_sync_complete_callback

    async def _invalidate_caches_post_sync():
        _products_cache.clear()
        # NO limpiar _stock_issues_cache: el prewarm post-sync la reconstruye con datos nuevos.
        # Limpiarla aquí causaba spinner en el tab Stock durante/después de un sync.
        logger.info("[SYNC-CB] products_cache invalidado post stock-sync")

    register_sync_complete_callback(_invalidate_caches_post_sync)
    # Recalcular precios sugeridos en DB con fórmula actual (retail × 18 × 1.20)
    from app.api.lanzar import router as _lanzar_router_ref
    try:
        import aiosqlite
        from app.config import DATABASE_PATH
        updated = 0
        async with aiosqlite.connect(DATABASE_PATH) as _db:
            _rows = await (await _db.execute(
                "SELECT rowid, retail_price_usd FROM bm_sku_gaps"
            )).fetchall()
            for _row in _rows:
                _rowid, _retail = _row[0], float(_row[1] or 0)
                _new_sug  = round(_retail * 18 * 1.20, 0) if _retail > 0 else 0
                _new_cost = round(_retail * 18, 0) if _retail > 0 else 0  # retail IS our acquisition cost
                await _db.execute(
                    "UPDATE bm_sku_gaps SET suggested_price_mxn=?, cost_price_mxn=?, cost_usd=? WHERE rowid=?",
                    (_new_sug, _new_cost, _retail, _rowid)
                )
                updated += 1
            await _db.commit()
        import logging as _logging
        _logging.getLogger(__name__).info(f"Startup: recalculated prices for {updated} gap records")
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning(f"Startup price recalc failed: {_e}")
    yield
    await price_monitor.stop()


app = FastAPI(title="Mercado Libre Dashboard", lifespan=lifespan)

# Static files y templates
BASE_PATH = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_PATH / "static"), name="static")
templates = Jinja2Templates(directory=BASE_PATH / "templates")

# Cache-bust token for static assets — changes on every deploy
import subprocess as _sp, time as _time
try:
    _BUILD_ID = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=BASE_PATH.parent, text=True).strip()
except Exception:
    _BUILD_ID = str(int(_time.time()))
templates.env.globals["build_id"] = _BUILD_ID

# ---------- Auth middleware ----------
# /api/v1/ usa su propio auth por API Key — exento del middleware de sesión de dashboard
_AUTH_EXEMPT = ("/login", "/set-password", "/static", "/favicon.ico", "/auth/", "/api/v1/", "/api/health-ai/debug-key", "/api/debug/item-stock", "/api/debug/test-merchant", "/factura/", "/api/diag/")

# Mapeo de rutas de página a sección (para control de acceso por sección)
_PATH_TO_SECTION: dict[str, str] = {
    "/dashboard":        "dashboard",
    "/multi-dashboard":  "dashboard",
    "/inventory-global": "dashboard",
    "/orders":           "ventas",
    "/items":            "productos",
    "/items-health":     "productos",
    "/productos":        "productos",
    "/sku-sales":        "sku",
    "/sku-compare":      "sku",
    "/sku-inventory":    "sku",
    "/ads":              "ads",
    "/health":           "salud",
    "/returns":          "devoluciones",
    "/planning":         "planning",
    "/facturacion":      "facturacion",
    "/stock-sync":       "sync",
    "/amazon":           "amazon",
}
_SECTION_FIRST_URL: dict[str, str] = {
    "dashboard":    "/dashboard",
    "ventas":       "/orders",
    "productos":    "/items",
    "sku":          "/sku-sales",
    "ads":          "/ads",
    "salud":        "/health",
    "devoluciones": "/returns",
    "planning":     "/planning",
    "facturacion":  "/facturacion",
    "sync":         "/stock-sync",
    "amazon":       "/amazon",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(ex) for ex in _AUTH_EXEMPT):
            return await call_next(request)
        token = request.cookies.get("dash_session")
        du = await user_store.get_session(token) if token else None
        if not du:
            from urllib.parse import quote
            next_url = quote(str(request.url.path), safe="")
            return RedirectResponse(f"/login?next={next_url}", status_code=302)
        # Si debe cambiar contraseña, redirigir a set-password (excepto si ya está allí)
        if du.get("must_change_pw") and path != "/set-password":
            return RedirectResponse("/set-password", status_code=302)
        # Control de acceso por sección (solo para usuarios no-admin con secciones restringidas)
        allowed_sections = du.get("allowed_sections") or []
        if allowed_sections and du.get("role") != "admin":
            # Solo aplicar a rutas de página HTML (no API, no static)
            if not path.startswith("/api/") and not path.startswith("/static/"):
                section = _PATH_TO_SECTION.get(path)
                if section and section not in allowed_sections:
                    # Redirigir a la primera sección permitida
                    first_url = _SECTION_FIRST_URL.get(allowed_sections[0], "/facturacion")
                    return RedirectResponse(first_url, status_code=302)
        request.state.dashboard_user = du
        return await call_next(request)


class AccountMiddleware(BaseHTTPMiddleware):
    """Setea el ContextVar de cuenta activa basado en la cookie active_account_id.
    Todos los roles pueden cambiar de cuenta — el rol controla qué pueden hacer en ella."""
    async def dispatch(self, request: Request, call_next):
        cookie_uid = request.cookies.get("active_account_id")
        if cookie_uid:
            tokens = await token_store.get_tokens(cookie_uid)
            if tokens:
                token = _meli_user_id_ctx.set(cookie_uid)
                try:
                    return await call_next(request)
                finally:
                    _meli_user_id_ctx.reset(token)
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(AccountMiddleware)


# ---------- Auth routes (login/logout/set-password) ----------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", next: str = "/dashboard", username: str = ""):
    # Si ya tiene sesión válida, redirigir
    token = request.cookies.get("dash_session")
    if token:
        du = await user_store.get_session(token)
        if du and not du.get("must_change_pw"):
            return RedirectResponse(next, status_code=302)
    return templates.TemplateResponse(request, "login_dash.html", {        "error": error,
        "next": next,
        "username": username,
    })


@app.post("/login/verify")
async def login_verify(request: Request):
    form = await request.form()
    username = form.get("username", "").strip().lower()
    password = form.get("password", "")
    next_url = form.get("next", "/dashboard")
    from urllib.parse import quote

    user = await user_store.get_user_by_username(username)
    if not user:
        return RedirectResponse(
            f"/login?error=Usuario+o+contrasena+incorrectos&next={quote(next_url, safe='')}&username={quote(username, safe='')}",
            status_code=302
        )
    # Usuario nuevo sin contraseña: crear sesión temporal para set-password
    if not user.get("password_hash") or user.get("must_change_pw"):
        # Verificar si tiene hash — si no tiene nunca ha seteado pw
        if not user.get("password_hash"):
            token = await user_store.create_session(user["id"], ip=request.client.host if request.client else "")
            response = RedirectResponse("/set-password", status_code=302)
            response.set_cookie("dash_session", token, max_age=3600, httponly=True, samesite="lax")
            return response
        # Tiene hash pero must_change_pw=1: validar pw actual primero
        if not user_store.verify_password(password, user["password_hash"], user["password_salt"]):
            return RedirectResponse(
                f"/login?error=Contrasena+incorrecta&next={quote(next_url, safe='')}&username={quote(username, safe='')}",
                status_code=302
            )
        token = await user_store.create_session(user["id"], ip=request.client.host if request.client else "")
        response = RedirectResponse("/set-password", status_code=302)
        response.set_cookie("dash_session", token, max_age=3600, httponly=True, samesite="lax")
        return response

    if not user_store.verify_password(password, user["password_hash"], user["password_salt"]):
        return RedirectResponse(
            f"/login?error=Usuario+o+contrasena+incorrectos&next={quote(next_url, safe='')}&username={quote(username, safe='')}",
            status_code=302
        )
    token = await user_store.create_session(user["id"], ip=request.client.host if request.client else "")
    await user_store.update_last_login(user["id"])
    await user_store.log_action(
        username=user["username"],
        action="login",
        ip=request.client.host if request.client else "",
        user_id=user["id"],
    )
    # Si el usuario tiene secciones restringidas y va al dashboard genérico,
    # redirigir a su primera sección permitida
    allowed_sections = user.get("allowed_sections") or []
    if allowed_sections and user.get("role") != "admin" and next_url in ("/dashboard", "/"):
        next_url = _SECTION_FIRST_URL.get(allowed_sections[0], "/facturacion")
    response = RedirectResponse(next_url, status_code=302)
    response.set_cookie("dash_session", token, max_age=2592000, httponly=True, samesite="lax")
    # Pre-warm caches
    global _prewarm_task
    if _prewarm_task is None or _prewarm_task.done():
        _prewarm_task = asyncio.create_task(_prewarm_caches())
    return response


@app.get("/set-password", response_class=HTMLResponse)
async def set_password_page(request: Request, error: str = ""):
    token = request.cookies.get("dash_session")
    if not token:
        return RedirectResponse("/login", status_code=302)
    du = await user_store.get_session(token)
    if not du:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "set_password.html", {        "username": du["username"],
        "error": error,
    })


@app.post("/set-password")
async def set_password_submit(request: Request):
    token = request.cookies.get("dash_session")
    if not token:
        return RedirectResponse("/login", status_code=302)
    du = await user_store.get_session(token)
    if not du:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    password = form.get("password", "")
    password2 = form.get("password2", "")
    from urllib.parse import quote
    if len(password) < 8:
        return RedirectResponse(f"/set-password?error={quote('Minimo 8 caracteres')}", status_code=302)
    if password != password2:
        return RedirectResponse(f"/set-password?error={quote('Las contrasenas no coinciden')}", status_code=302)
    await user_store.set_password(du["id"], password)
    await user_store.update_last_login(du["id"])
    await user_store.log_action(
        username=du["username"],
        action="login",
        ip=request.client.host if request.client else "",
        user_id=du["id"],
    )
    # Pre-warm caches
    global _prewarm_task
    if _prewarm_task is None or _prewarm_task.done():
        _prewarm_task = asyncio.create_task(_prewarm_caches())
    return RedirectResponse("/dashboard", status_code=302)


# Routers
app.include_router(auth_router)
app.include_router(orders_router)
app.include_router(items_router)
app.include_router(metrics_router)
app.include_router(health_router)
app.include_router(sku_inventory_router)
app.include_router(health_ai_router)
app.include_router(amazon_products_router)
app.include_router(amazon_orders_router)
app.include_router(users_router)
app.include_router(system_health_router)
app.include_router(sales_v1_router)
app.include_router(binmanager_router)
app.include_router(lanzar_router)
app.include_router(productos_router)
app.include_router(facturacion_router)


# ---------- Account switcher ----------

@app.post("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get("dash_session")
    if token:
        du = await user_store.get_session(token)
        if du:
            await user_store.log_action(
                username=du["username"],
                action="logout",
                ip=request.client.host if request.client else "",
                user_id=du["id"],
            )
        await user_store.delete_session(token)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("dash_session")
    return response


@app.post("/auth/switch-account")
async def switch_account(request: Request):
    """Cambia la cuenta activa y setea la cookie active_account_id."""
    form = await request.form()
    uid = form.get("user_id", "")
    if uid:
        tokens = await token_store.get_tokens(uid)
        if tokens:
            # Allow explicit redirect field; else use referer
            redirect_to = form.get("redirect", "")
            if not redirect_to:
                redirect_to = request.headers.get("referer", "/dashboard")
                # Si venimos desde cualquier página Amazon, ir al dashboard MeLi
                if "/amazon" in redirect_to:
                    redirect_to = "/dashboard"
            response = RedirectResponse(redirect_to, status_code=303)
            response.set_cookie("active_account_id", uid, max_age=2592000, httponly=True, samesite="lax")
            return response
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/auth/switch-amazon")
async def switch_amazon_account(request: Request):
    """
    Cambia la cuenta Amazon activa seteando la cookie active_amazon_id.

    Funciona igual que switch-account pero para cuentas Amazon.
    Cookie separada para no interferir con la cuenta MeLi activa.
    """
    form = await request.form()
    seller_id = form.get("seller_id", "")
    if seller_id:
        account = await token_store.get_amazon_account(seller_id)
        if account:
            # Prioridad: campo "next" > referer > /amazon (siempre va al dashboard Amazon)
            next_url = form.get("next") or "/amazon"
            response = RedirectResponse(next_url, status_code=303)
            response.set_cookie(
                "active_amazon_id", seller_id,
                max_age=2592000, httponly=True, samesite="lax"
            )
            return response
    return RedirectResponse("/amazon", status_code=303)


async def _accounts_ctx(request: Request) -> dict:
    """
    Contexto común de cuentas para todos los templates de página.

    Devuelve un dict con:
      accounts:          Lista de cuentas MeLi (user_id, nickname)
      active_user_id:    user_id de la cuenta MeLi activa (cookie)
      amazon_accounts:   Lista de cuentas Amazon (seller_id, nickname, marketplace_name)
      active_amazon_id:  seller_id de la cuenta Amazon activa (cookie)

    Las cuentas MeLi y Amazon se manejan con cookies separadas:
      - active_account_id   → MeLi user_id
      - active_amazon_id    → Amazon seller_id

    Así el cambio de cuenta Amazon NO afecta la cuenta MeLi activa
    y toda la funcionalidad MeLi existente queda intacta.
    """
    # ── Cuentas Mercado Libre ──────────────────────────────────────────
    accounts = await token_store.get_all_tokens()
    active_uid = request.cookies.get("active_account_id")
    if active_uid and not any(a["user_id"] == active_uid for a in accounts):
        active_uid = None
    if not active_uid and accounts:
        active_uid = accounts[0]["user_id"]

    # ── Cuentas Amazon ────────────────────────────────────────────────
    amazon_accounts = await token_store.get_all_amazon_accounts()
    active_amazon_id = request.cookies.get("active_amazon_id")
    if active_amazon_id and not any(a["seller_id"] == active_amazon_id for a in amazon_accounts):
        active_amazon_id = None
    if not active_amazon_id and amazon_accounts:
        active_amazon_id = amazon_accounts[0]["seller_id"]

    return {
        "accounts": accounts,
        "active_user_id": active_uid,
        "amazon_accounts": amazon_accounts,
        "active_amazon_id": active_amazon_id,
        "dashboard_user": getattr(request.state, "dashboard_user", None),
    }


async def _enrich_with_sale_prices(client, products: list, id_key: str = "id", price_key: str = "price"):
    """Enriquece lista de productos con datos de /sale_price.
    Si sale_price muestra descuento (regular_amount > amount),
    actualiza original_price con regular_amount."""
    sem = asyncio.Semaphore(10)

    async def _fetch_sp(item_id: str):
        async with sem:
            return item_id, await client.get_item_sale_price(item_id)

    item_ids = [p[id_key] for p in products if p.get(id_key)]
    if not item_ids:
        return

    tasks = [_fetch_sp(iid) for iid in item_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sp_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if data and isinstance(data, dict):
            sp_map[iid] = data

    for p in products:
        iid = p.get(id_key)
        sp = sp_map.get(iid)
        if not sp:
            continue
        amount = sp.get("amount")
        regular = sp.get("regular_amount")
        if amount and regular and regular > amount:
            p[price_key] = amount
            p["original_price"] = regular


async def _enrich_with_meli_health(client, products: list, id_key="id"):
    """Consulta /items/{id}/health en paralelo y agrega _meli_health y _meli_health_level."""
    sem = asyncio.Semaphore(10)

    async def _fetch(item_id):
        async with sem:
            h = await client.get_item_health(item_id)
            return item_id, h

    ids = [p[id_key] for p in products if p.get(id_key)]
    if not ids:
        return
    results = await asyncio.gather(*[_fetch(i) for i in ids], return_exceptions=True)
    h_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if data and isinstance(data, dict):
            h_map[iid] = data
    for p in products:
        h = h_map.get(p.get(id_key))
        if h:
            p["_meli_health"] = h.get("health", 0)
            p["_meli_health_level"] = h.get("level", "basic")


async def _enrich_with_promotions(client, products: list, id_key="id"):
    """Consulta /seller-promotions/items/{id} en paralelo."""
    sem = asyncio.Semaphore(10)

    async def _fetch(item_id):
        async with sem:
            promos = await client.get_item_promotions(item_id)
            return item_id, promos

    ids = [p[id_key] for p in products if p.get(id_key)]
    if not ids:
        return
    results = await asyncio.gather(*[_fetch(i) for i in ids], return_exceptions=True)
    p_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if isinstance(data, list):
            p_map[iid] = data
    _auto_types = {"SMART", "PRE_NEGOTIATED", "SELLER_COUPON_CAMPAIGN"}
    for p in products:
        promos = p_map.get(p.get(id_key), [])
        p["_promotions"] = promos
        # Promos que gestiona el vendedor (PRICE_DISCOUNT, DEAL)
        active_seller = [
            pr for pr in promos
            if pr.get("status") in ("started", "active", "pending")
            and pr.get("type") not in _auto_types
        ]
        # Promos que gestiona ML automáticamente (PRE_NEGOTIATED, SMART, etc.)
        active_auto = [
            pr for pr in promos
            if pr.get("status") in ("started", "active", "pending")
            and pr.get("type") in _auto_types
        ]
        # Seller tiene prioridad; si no hay seller, usar auto (PRE_NEGOTIATED cuenta como deal activo)
        active_promos = active_seller if active_seller else active_auto
        p["_has_deal"] = len(active_promos) > 0
        p["_deal_is_ml_auto"] = len(active_seller) == 0 and len(active_auto) > 0
        p["_deal_types"] = list(set(pr.get("type", "") for pr in active_promos))
        p["_meli_promo_pct"] = 0
        p["_seller_promo_pct"] = 0
        # Actualizar price/original_price y contribución ML desde la promo activa
        if active_promos:
            ap = active_promos[0]
            if ap.get("price") and ap["price"] > 0:
                p["price"] = ap["price"]
            if ap.get("original_price") and ap["original_price"] > 0:
                p["original_price"] = ap["original_price"]
            p["_meli_promo_pct"] = ap.get("meli_percentage", 0) or 0
            p["_seller_promo_pct"] = ap.get("seller_percentage", 0) or 0


async def _enrich_with_bm_product_info(products: list, sku_key="sku"):
    """Enriquece productos con RetailPrice, AvgCostQTY, Brand, etc. desde BM.

    Optimización: si _bm_bulk_gr_cache/_bm_bulk_all_cache está fresco (del prewarm), usa esos datos sin
    hacer ninguna request adicional. Solo hace requests individuales si no hay bulk cache.
    """
    # --- Intentar usar bulk cache del prewarm (gratis — ya fue consultado) ---
    base_map: dict = {}
    try:
        _enrich_bulk = _bm_bulk_gr_cache or _bm_bulk_all_cache
        if _enrich_bulk and (_time.time() - _enrich_bulk[0]) < _BM_CACHE_TTL:
            for _erow in _enrich_bulk[1]:
                _esk = ((_erow.get("SKU") or "")).upper().strip()
                if not _esk:
                    continue
                _ebase = _extract_base_sku(_esk)
                if _ebase not in base_map:
                    base_map[_ebase] = _erow
            logger.debug(f"[BM-ENRICH] Usando bulk cache — {len(base_map)} SKUs sin requests adicionales")
    except Exception as _ce:
        logger.warning(f"[BM-ENRICH] Error leyendo bulk cache: {_ce} — fallback per-SKU")
        base_map = {}

    if not base_map:
        # Fallback: requests individuales (sin bulk cache disponible)
        import httpx
        BM_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
        sem = asyncio.Semaphore(15)
        base_to_skus: dict = {}
        for p in products:
            sku = p.get(sku_key, "")
            if not sku:
                continue
            base = normalize_to_bm_sku(sku)
            if not base:
                continue
            base_to_skus.setdefault(base, []).append(sku)

        async def _fetch(base, http):
            async with sem:
                try:
                    resp = await http.post(BM_URL, json={
                        "COMPANYID": 1, "SEARCH": base, "CONCEPTID": 8,
                        "NUMBERPAGE": 1, "RECORDSPAGE": 10,
                        "NEEDRETAILPRICEPH": True, "NEEDRETAILPRICE": True, "NEEDAVGCOST": True,
                    }, headers={"Content-Type": "application/json"}, timeout=30.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data and isinstance(data, list) and data:
                            for item in data:
                                if item.get("SKU", "").upper() == base:
                                    return base, item
                            return base, data[0]
                except Exception:
                    pass
                return base, None

        unique_bases = list(base_to_skus.keys())[:30]
        from app.services.binmanager_client import get_shared_bm as _get_shared_bm
        _bm_cli = await _get_shared_bm()
        http = _bm_cli._client()
        results = await asyncio.gather(*[_fetch(b, http) for b in unique_bases], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) or r is None:
                continue
            base, data = r
            if data:
                base_map[base] = data

    # Aplicar datos a todos los productos
    for p in products:
        sku = p.get(sku_key, "")
        if not sku:
            continue
        base = normalize_to_bm_sku(sku)
        if not base:
            continue
        bm = base_map.get(base)
        if bm:
            retail_price = bm.get("RetailPrice", 0) or 0
            retail_ph    = bm.get("LastRetailPricePurchaseHistory", 0) or 0
            p["_bm_retail_price"] = retail_price if retail_price > 0 else retail_ph
            p["_bm_avg_cost"] = bm.get("AvgCostQTY", 0) or 0
            p["_bm_brand"] = bm.get("Brand", "")
            p["_bm_model"] = bm.get("Model", "")
            p["_bm_title"] = bm.get("Title", "")
            p["_bm_retail_ph"] = retail_ph


async def _enrich_with_bm_stock(products: list, sku_key="sku"):
    """Consulta BinManager Warehouse endpoint para stock real por almacen (MTY/CDMX/TJ)."""
    import httpx
    BM_WH_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
    sem = asyncio.Semaphore(10)

    async def _fetch(sku, http):
        async with sem:
            base = normalize_to_bm_sku(sku)
            if not base:
                return sku, None
            try:
                resp = await http.post(BM_WH_URL, json={
                    "COMPANYID": 1, "SKU": base, "WarehouseID": None,
                    "LocationID": "47,62,68", "BINID": None,
                    "Condition": _bm_conditions_for_sku(sku), "ForInventory": 0, "SUPPLIERS": None,
                }, timeout=15.0)
                if resp.status_code == 200:
                    rows = resp.json() or []
                    mty = cdmx = tj = 0
                    for row in rows:
                        qty = row.get("QtyTotal", 0) or 0
                        wname = (row.get("WarehouseName") or "").lower()
                        if "monterrey" in wname or "maxx" in wname:
                            mty += qty
                        elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                            cdmx += qty
                        else:
                            tj += qty
                    return sku, {"mty": mty, "cdmx": cdmx, "tj": tj}
            except Exception:
                pass
            return sku, None

    async with httpx.AsyncClient() as http:
        tasks = [_fetch(p.get(sku_key, ""), http) for p in products if p.get(sku_key)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    bm_map = {}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        sku, data = r
        if data:
            bm_map[sku] = data

    for p in products:
        bm = bm_map.get(p.get(sku_key))
        if bm:
            p["_bm_mty"] = max(0, bm.get("mty", 0) or 0)
            p["_bm_cdmx"] = max(0, bm.get("cdmx", 0) or 0)
            p["_bm_tj"] = max(0, bm.get("tj", 0) or 0)
            p["_bm_total"] = p["_bm_mty"] + p["_bm_cdmx"]


def _aggregate_sales_by_item(orders: list) -> dict:
    """Agrupa ventas de ordenes por item_id. Retorna {item_id: {units, revenue, fees}}."""
    sales = {}
    for order in orders:
        if order.get("status") not in ("paid", "delivered"):
            continue
        for oi in order.get("order_items", []):
            item = oi.get("item", {})
            iid = item.get("id", "")
            if not iid:
                continue
            qty = oi.get("quantity", 0)
            unit_price = oi.get("unit_price", 0)
            fee = oi.get("sale_fee", 0) or 0
            sales.setdefault(iid, {"units": 0, "revenue": 0, "fees": 0})
            sales[iid]["units"] += qty
            sales[iid]["revenue"] += qty * unit_price
            sales[iid]["fees"] += fee
    return sales


def _get_item_sku(body: dict) -> str:
    """Extrae SKU de un item body usando TODAS las fuentes disponibles en cascada.

    REGLA: nunca reemplazar una fuente por otra — siempre encadenar como fallback.
    ML expone el SKU en múltiples campos según el tipo de listing y cómo fue creado;
    eliminar una fuente rompe listings que solo usan esa fuente.

    Cadena de prioridad:
      1. seller_custom_field de la variación        (items con variaciones)
      2. atributo SELLER_SKU de la variación        (items con variaciones, campo alternativo)
      3. seller_custom_field del item padre          (items simples)
      4. atributo SELLER_SKU del item padre          (items simples, campo alternativo)
    """
    # Fuente 1 y 2: variaciones (el padre puede tener SKU incorrecto cuando hay variaciones)
    for var in (body.get("variations") or []):
        sku = (var.get("seller_custom_field") or "").strip()
        if sku and sku not in ("None", "none"):
            return sku
        for va in (var.get("attributes") or []):
            if va.get("id") == "SELLER_SKU" and va.get("value_name"):
                return va["value_name"].strip()
    # Fuente 3 y 4: item sin variaciones — campos del padre
    sku = (body.get("seller_custom_field") or "").strip()
    if sku and sku not in ("None", "none"):
        return sku
    for attr in (body.get("attributes") or []):
        if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
            return attr["value_name"].strip()
    return ""


async def get_current_user():
    """Obtiene el usuario actual si hay sesion activa."""
    client = await get_meli_client()
    if not client:
        return None
    try:
        user = await client.get_user_info()
        return user
    except Exception:
        return None
    finally:
        await client.close()


# === Rutas de paginas ===

@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/usuarios", response_class=HTMLResponse)
async def usuarios_page(request: Request):
    du = getattr(request.state, "dashboard_user", None)
    if not du or du.get("role") != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    ctx = await _accounts_ctx(request)
    user = await get_current_user()
    return templates.TemplateResponse(request, "usuarios.html", {        "user": user,
        "active": "usuarios",
        **ctx,
    })


@app.get("/auditoria", response_class=HTMLResponse)
async def auditoria_page(request: Request):
    du = getattr(request.state, "dashboard_user", None)
    if not du or du.get("role") != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    ctx = await _accounts_ctx(request)
    user = await get_current_user()
    return templates.TemplateResponse(request, "auditoria.html", {        "user": user,
        "active": "auditoria",
        **ctx,
    })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    # Pre-warm caches — solo admin. Operadores consumen el cache existente.
    global _prewarm_task
    _du_dash = getattr(request.state, "dashboard_user", None) or {}
    if _du_dash.get("role") == "admin" and (_prewarm_task is None or _prewarm_task.done()):
        _prewarm_task = asyncio.create_task(_prewarm_caches())
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "active": "dashboard",
        **ctx
    })


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "orders.html", {        "user": user,
        "active": "orders",
        **ctx
    })


@app.get("/items", response_class=HTMLResponse)
async def items_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    # Pre-warm caches — solo admin. Operadores consumen el cache existente.
    global _prewarm_task
    _du_items = getattr(request.state, "dashboard_user", None) or {}
    if _du_items.get("role") == "admin" and (_prewarm_task is None or _prewarm_task.done()):
        _prewarm_task = asyncio.create_task(_prewarm_caches())
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "items.html", {        "user": user,
        "active": "items",
        **ctx
    })


@app.get("/bm/unlaunched", response_class=HTMLResponse)
async def bm_unlaunched_page(request: Request):
    """Corrida inversa: SKUs en BM sin listing en ML, paginados por categoría."""
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "bm_unlaunched.html", {
        "user": user,
        "active": "bm_unlaunched",
        **ctx,
    })


@app.get("/facturacion", response_class=HTMLResponse)
async def facturacion_page(request: Request):
    """Dashboard de facturación — gestión de solicitudes."""
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    from app.api.facturacion import CFDI_USES, FISCAL_REGIMES, FORMAS_PAGO
    return templates.TemplateResponse(request, "facturacion.html", {
        "user": user,
        "active": "facturacion",
        "cfdi_uses": CFDI_USES,
        "fiscal_regimes": FISCAL_REGIMES,
        "formas_pago": FORMAS_PAGO,
        **ctx,
    })


# ── Rutas públicas del portal de facturación (sin autenticación) ──────────────

@app.get("/factura/{token}", response_class=HTMLResponse)
async def factura_cliente_page(request: Request, token: str):
    """Página pública para el cliente — sin login requerido."""
    req = await token_store.get_billing_request_by_token(token)
    if not req:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:2rem'>Enlace no válido o expirado.</h2>", status_code=404)
    from app.api.facturacion import CFDI_USES, FISCAL_REGIMES, FORMAS_PAGO
    fiscal = await token_store.get_billing_fiscal_data(req["id"])
    _inv = await token_store.get_billing_invoice(req["id"])
    has_invoice = bool(_inv and _inv.get("pdf_data"))
    has_xml     = bool(_inv and _inv.get("xml_data"))
    return templates.TemplateResponse(request, "factura_cliente.html", {
        "req": req,
        "fiscal": fiscal,
        "has_invoice": has_invoice,
        "has_xml": has_xml,
        "cfdi_uses": CFDI_USES,
        "fiscal_regimes": FISCAL_REGIMES,
        "formas_pago": FORMAS_PAGO,
    })


def _is_amazon_order_id(order_id: str) -> bool:
    import re
    return bool(re.match(r'^\d{3}-\d{7}-\d{7}$', order_id.strip()))


@app.post("/factura/{token}/lookup")
async def factura_lookup_order(request: Request, token: str):
    """Busca la orden en ML o Amazon y devuelve el resumen del producto."""
    req = await token_store.get_billing_request_by_token(token)
    if not req:
        return JSONResponse({"error": "Enlace no válido"}, status_code=404)
    if req["status"] != "pending_data":
        return JSONResponse({"error": "Esta solicitud ya no acepta datos"}, status_code=400)

    body = await request.json()
    order_number = (body.get("order_number") or "").strip()
    if not order_number:
        return JSONResponse({"error": "Ingresa tu número de orden"}, status_code=400)

    # Validar que coincide con la orden registrada (si el admin la especificó)
    if req["order_number"] and req["order_number"] != order_number:
        return JSONResponse({"error": "Número de orden no encontrado"}, status_code=404)

    import json as _json
    order_data = {}
    platform = req.get("platform") or ("amazon" if _is_amazon_order_id(order_number) else "mercadolibre")

    if platform == "amazon":
        # Buscar en Amazon
        try:
            from app.services.amazon_client import get_amazon_client
            seller_id = req.get("ml_user_id") or None
            client = await get_amazon_client(seller_id)
            if client:
                raw = await client._request("GET", f"/orders/v0/orders/{order_number}")
                order = raw.get("payload", {})
                if order and order.get("AmazonOrderId"):
                    items_raw = await client._request("GET", f"/orders/v0/orders/{order_number}/orderItems")
                    order["_items"] = items_raw.get("payload", {}).get("OrderItems", [])
                    order["_platform"] = "amazon"
                    order_data = order
        except Exception as e:
            order_data = {"_api_error": str(e)}
    elif req["ml_user_id"] and platform == "mercadolibre":
        # Buscar en MeLi
        try:
            from app.services.meli_client import get_meli_client
            client = await get_meli_client(user_id=req["ml_user_id"])
            order_data = await client.resolve_order(order_number)
            await client.close()
        except Exception as e:
            order_data = {"_api_error": str(e)}

    # Construir resumen normalizado y guardarlo (en vez del raw de la API)
    summary = _build_order_summary(order_data, order_number)
    await token_store.update_billing_order_data(req["id"], _json.dumps(summary))

    return JSONResponse({"ok": True, "summary": summary})


def _build_order_summary(order_data: dict, order_number: str) -> dict:
    """Extrae campos relevantes para mostrar al cliente. Soporta MeLi y Amazon."""
    if not order_data or order_data.get("_api_error") or (
        "error" in order_data and not order_data.get("AmazonOrderId")
    ):
        return {"order_number": order_number, "items": [], "total": None, "date": None}

    # ── Amazon ───────────────────────────────────────────────────────────────
    if order_data.get("_platform") == "amazon" or order_data.get("AmazonOrderId"):
        items = []
        for it in order_data.get("_items", []):
            price_obj = it.get("ItemPrice") or {}
            items.append({
                "title":      it.get("Title", ""),
                "sku":        it.get("SellerSKU", ""),
                "asin":       it.get("ASIN", ""),
                "quantity":   it.get("QuantityOrdered", 1),
                "unit_price": float(price_obj.get("Amount") or 0),
                "currency":   price_obj.get("CurrencyCode", "MXN"),
            })
        total_obj = order_data.get("OrderTotal") or {}
        return {
            "order_number": order_number,
            "platform":     "amazon",
            "items":        items,
            "total":        float(total_obj.get("Amount") or 0) or None,
            "currency":     total_obj.get("CurrencyCode", "MXN"),
            "date":         (order_data.get("PurchaseDate") or "")[:10],
            "status":       order_data.get("OrderStatus", ""),
        }

    # ── MercadoLibre ─────────────────────────────────────────────────────────
    items = []
    for oi in order_data.get("order_items", []):
        item = oi.get("item", {})
        items.append({
            "title":      item.get("title", ""),
            "sku":        item.get("seller_custom_field", "") or "",
            "quantity":   oi.get("quantity", 1),
            "unit_price": oi.get("unit_price") or oi.get("full_unit_price"),
            "currency":   order_data.get("currency_id", "MXN"),
        })
    return {
        "order_number": order_number,
        "platform":     "mercadolibre",
        "items":        items,
        "total":        order_data.get("total_amount"),
        "currency":     order_data.get("currency_id", "MXN"),
        "date":         (order_data.get("date_closed") or order_data.get("date_created") or "")[:10],
        "status":       order_data.get("status", ""),
    }


@app.post("/factura/{token}/submit")
async def factura_submit_datos(
    request: Request,
    token: str,
    rfc: str = Form(""),
    razon_social: str = Form(""),
    cfdi_use: str = Form(""),
    fiscal_regime: str = Form(""),
    zip_code: str = Form(""),
    forma_pago: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    street: str = Form(""),
    constancia: UploadFile = File(None),
):
    """El cliente envía sus datos fiscales."""
    req = await token_store.get_billing_request_by_token(token)
    if not req:
        return JSONResponse({"error": "Enlace no válido"}, status_code=404)
    if req["status"] not in ("pending_data",):
        return JSONResponse({"error": "Esta solicitud ya fue procesada"}, status_code=400)

    # Leer constancia si fue adjuntada
    constancia_data = None
    constancia_name = ""
    if constancia and constancia.filename:
        constancia_data = await constancia.read()
        constancia_name = constancia.filename
        if len(constancia_data) > 5 * 1024 * 1024:
            return JSONResponse({"error": "La constancia fiscal no debe superar 5 MB"}, status_code=413)

    await token_store.save_billing_fiscal_data(
        request_id=req["id"],
        rfc=rfc.strip().upper(),
        razon_social=razon_social.strip(),
        cfdi_use=cfdi_use,
        fiscal_regime=fiscal_regime,
        zip_code=zip_code.strip(),
        forma_pago=forma_pago,
        email=email.strip(),
        phone=phone.strip(),
        street=street.strip(),
        constancia_data=constancia_data,
        constancia_name=constancia_name,
    )
    await token_store.update_billing_status(req["id"], "data_received")
    return JSONResponse({"ok": True})


@app.get("/factura/{token}/invoice")
async def factura_download_invoice(request: Request, token: str):
    """Cliente descarga su factura PDF."""
    req = await token_store.get_billing_request_by_token(token)
    if not req:
        return JSONResponse({"error": "Enlace no válido"}, status_code=404)
    if req["status"] != "invoice_ready":
        return JSONResponse({"error": "Tu factura aún no está disponible"}, status_code=404)

    result = await token_store.get_billing_invoice(req["id"])
    if not result or not result.get("pdf_data"):
        return JSONResponse({"error": "Factura no encontrada"}, status_code=404)

    return Response(
        content=result["pdf_data"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{result["pdf_filename"]}"'},
    )


@app.get("/factura/{token}/invoice/xml")
async def factura_download_invoice_xml(request: Request, token: str):
    """Cliente descarga su factura XML."""
    req = await token_store.get_billing_request_by_token(token)
    if not req:
        return JSONResponse({"error": "Enlace no válido"}, status_code=404)
    if req["status"] != "invoice_ready":
        return JSONResponse({"error": "Tu factura aún no está disponible"}, status_code=404)

    result = await token_store.get_billing_invoice(req["id"])
    if not result or not result.get("xml_data"):
        return JSONResponse({"error": "XML no encontrado"}, status_code=404)

    return Response(
        content=result["xml_data"],
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{result["xml_filename"] or "factura.xml"}"'},
    )


@app.get("/sku-sales", response_class=HTMLResponse)
async def sku_sales_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "sku_sales.html", {        "user": user,
        "active": "sku_sales",
        **ctx
    })


@app.get("/sku-compare", response_class=HTMLResponse)
async def sku_compare_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "sku_compare.html", {        "user": user,
        "active": "sku_compare",
        **ctx
    })


@app.get("/sku-inventory")
async def sku_inventory_page(request: Request):
    return RedirectResponse("/productos", status_code=301)


@app.get("/productos", response_class=HTMLResponse)
async def productos_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "productos.html", {
        "user": user,
        "active": "productos",
        **ctx
    })


@app.get("/productos/sin-bm", response_class=HTMLResponse)
async def productos_sin_bm_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "ml_sin_bm.html", {
        "user": user,
        "active": "sin_bm",
        **ctx
    })


@app.get("/api/sku-compare")
async def sku_compare_api(
    a_from: str = Query(..., description="Periodo A inicio YYYY-MM-DD"),
    a_to: str = Query(..., description="Periodo A fin YYYY-MM-DD"),
    b_from: str = Query(..., description="Periodo B inicio YYYY-MM-DD"),
    b_to: str = Query(..., description="Periodo B fin YYYY-MM-DD"),
):
    """Compara ventas por SKU entre dos periodos, con deteccion de stock."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        import asyncio
        orders_a_task = client.fetch_all_orders(date_from=a_from, date_to=a_to)
        orders_b_task = client.fetch_all_orders(date_from=b_from, date_to=b_to)
        orders_a, orders_b = await asyncio.gather(orders_a_task, orders_b_task)

        def aggregate_skus(orders):
            _EXCL = {"cancelled", "payment_required", "payment_in_process"}
            sku_map = {}
            for order in orders:
                if order.get("status") in _EXCL:
                    continue
                for oi in order.get("order_items", []):
                    item = oi.get("item", {})
                    raw_sku = item.get("seller_sku") or item.get("seller_custom_field") or "SIN SKU"
                    base = _extract_base_sku(raw_sku)
                    title = item.get("title", "-")
                    item_id = item.get("id", "")
                    qty = oi.get("quantity", 1)
                    price = oi.get("unit_price", 0)
                    fee = oi.get("sale_fee", 0) or 0
                    net = price * qty - fee - fee * 0.16
                    if base not in sku_map:
                        sku_map[base] = {"sku": raw_sku, "title": title, "units": 0, "revenue": 0, "item_ids": set()}
                    sku_map[base]["units"] += qty
                    sku_map[base]["revenue"] += net
                    if item_id:
                        sku_map[base]["item_ids"].add(item_id)
            return sku_map

        skus_a = aggregate_skus(orders_a)
        skus_b = aggregate_skus(orders_b)

        all_skus = set(list(skus_a.keys()) + list(skus_b.keys()))

        rows = []
        for sku in all_skus:
            a = skus_a.get(sku, {"units": 0, "revenue": 0, "title": "", "item_ids": set()})
            b = skus_b.get(sku, {"units": 0, "revenue": 0, "title": "", "item_ids": set()})
            title = a.get("title") or b.get("title") or "-"
            units_a = a["units"]
            units_b = b["units"]
            rev_a = a["revenue"]
            rev_b = b["revenue"]
            unit_diff = units_a - units_b
            rev_diff = rev_a - rev_b
            pct = ((units_a - units_b) / units_b * 100) if units_b > 0 else (100.0 if units_a > 0 else 0.0)

            status = "equal"
            if units_a > units_b:
                status = "up"
            elif units_a < units_b:
                status = "down"
            if units_b > 0 and units_a == 0:
                status = "lost"
            if units_a > 0 and units_b == 0:
                status = "new"

            # Combinar item_ids de ambos periodos
            item_ids = a.get("item_ids", set()) | b.get("item_ids", set())

            rows.append({
                "sku": sku,
                "title": title,
                "units_a": units_a,
                "units_b": units_b,
                "unit_diff": unit_diff,
                "pct": round(pct, 1),
                "rev_a": round(rev_a, 2),
                "rev_b": round(rev_b, 2),
                "rev_diff": round(rev_diff, 2),
                "status": status,
                "item_ids": list(item_ids),
                "reason": "",
                "stock": None,
                "item_status": ""
            })

        # Obtener info de stock/estado de items para SKUs lost y down
        check_rows = [r for r in rows if r["status"] in ("lost", "down")]
        item_ids_to_check = set()
        for r in check_rows:
            for iid in r["item_ids"]:
                item_ids_to_check.add(iid)

        item_info = {}
        id_list = list(item_ids_to_check)
        for i in range(0, len(id_list), 20):
            batch = id_list[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if body:
                        item_info[body.get("id", "")] = {
                            "stock": body.get("available_quantity", 0),
                            "status": body.get("status", ""),
                        }
            except Exception:
                pass

        # Asignar razon a cada row lost/down
        for r in check_rows:
            stocks = []
            statuses = []
            for iid in r["item_ids"]:
                info = item_info.get(iid)
                if info:
                    stocks.append(info["stock"])
                    statuses.append(info["status"])

            total_stock = sum(stocks) if stocks else None
            r["stock"] = total_stock

            if statuses:
                r["item_status"] = statuses[0]

            if total_stock is not None and total_stock == 0:
                if any(s == "paused" for s in statuses):
                    r["reason"] = "sin_stock_pausado"
                else:
                    r["reason"] = "sin_stock"
            elif any(s == "paused" for s in statuses):
                r["reason"] = "pausado"
            elif any(s == "inactive" for s in statuses):
                r["reason"] = "inactivo"
            else:
                r["reason"] = "con_stock"

        # Limpiar item_ids del response (no necesario en frontend)
        for r in rows:
            del r["item_ids"]

        # Ordenar: perdidos primero, luego mayor caida
        priority = {"lost": 0, "down": 1, "equal": 2, "up": 3, "new": 4}
        rows.sort(key=lambda r: (priority.get(r["status"], 2), r["unit_diff"]))

        # Resumen
        total_a = sum(r["units_a"] for r in rows)
        total_b = sum(r["units_b"] for r in rows)
        rev_total_a = sum(r["rev_a"] for r in rows)
        rev_total_b = sum(r["rev_b"] for r in rows)
        lost_count = len([r for r in rows if r["status"] == "lost"])
        new_count = len([r for r in rows if r["status"] == "new"])
        down_count = len([r for r in rows if r["status"] == "down"])
        up_count = len([r for r in rows if r["status"] == "up"])
        stock_issue_count = len([r for r in rows if r["reason"] in ("sin_stock", "sin_stock_pausado")])
        paused_count = len([r for r in rows if r["reason"] == "pausado"])

        return {
            "summary": {
                "units_a": total_a,
                "units_b": total_b,
                "units_diff": total_a - total_b,
                "units_pct": round((total_a - total_b) / total_b * 100, 1) if total_b > 0 else 0,
                "rev_a": round(rev_total_a, 2),
                "rev_b": round(rev_total_b, 2),
                "rev_diff": round(rev_total_a - rev_total_b, 2),
                "lost": lost_count,
                "new": new_count,
                "down": down_count,
                "up": up_count,
                "stock_issue": stock_issue_count,
                "paused": paused_count,
            },
            "rows": rows
        }
    finally:
        await client.close()


@app.get("/ads", response_class=HTMLResponse)
async def ads_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "ads.html", {        "user": user,
        "active": "ads",
        **ctx
    })


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "health.html", {        "user": user,
        "active": "health",
        **ctx
    })


@app.get("/items-health")
async def items_health_page(request: Request):
    return RedirectResponse("/productos", status_code=301)


@app.get("/stock-sync", response_class=HTMLResponse)
async def stock_sync_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    _du_sync = getattr(request.state, "dashboard_user", None) or {}
    if _du_sync.get("role") != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    from app.services.stock_sync_multi import get_sync_status
    status = get_sync_status()
    history = await token_store.get_multi_sync_last_runs(limit=10)
    from app.services.meli_client import _active_user_id as _ctx_uid
    _active_uid = str(_ctx_uid.get() or "")
    rules = await token_store.get_all_sku_platform_rules(user_id=_active_uid)
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "stock_sync.html", {
        "user": user,
        "active": "stock_sync",
        "running": status.get("running", False),
        "last_sync_iso": status.get("last_sync_iso"),
        "last_result": status.get("last_result") or {},
        "interval_min": status.get("interval_min", 5),
        "threshold": status.get("threshold", 10),
        "history": history,
        "rules": rules,
        **ctx
    })


# === Partials para HTMX ===

@app.get("/partials/metrics", response_class=HTMLResponse)
async def metrics_partial(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        from datetime import datetime
        now = datetime.utcnow()
        if not date_from:
            date_from = now.replace(day=1).strftime("%Y-%m-%d")
        if not date_to:
            date_to = now.strftime("%Y-%m-%d")

        all_orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)
        items_data = await client.get_items(limit=1)

        paid_orders = [o for o in all_orders if o.get("status") in ["paid", "delivered"]]

        # Enrich with net_received_amount (total - taxes) and shipping cost
        await client.enrich_orders_with_net_amount(paid_orders)
        await client.enrich_orders_with_shipping(paid_orders)

        metrics = {
            "summary": {
                "total_orders": len(all_orders),
                "monthly_sales": len(paid_orders),
                "monthly_revenue": sum(order_net_revenue(o) for o in paid_orders),
                "active_items": items_data.get("paging", {}).get("total", 0)
            }
        }

        return templates.TemplateResponse(request, "partials/metrics_cards.html", {            "metrics": metrics
        })
    finally:
        await client.close()


@app.get("/partials/recent-orders", response_class=HTMLResponse)
async def recent_orders_partial(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        orders_data = await client.get_orders(
            limit=5,
            date_from=date_from or None,
            date_to=date_to or None
        )
        orders = orders_data.get("results", [])

        # Enrich with net_received_amount
        await client.enrich_orders_with_net_amount(orders)

        html = "<div class='divide-y divide-gray-200'>"
        for order in orders:
            title = order["order_items"][0]["item"]["title"][:35] + "..." if order.get("order_items") else "-"
            sku = ""
            if order.get("order_items"):
                item = order["order_items"][0]["item"]
                sku = item.get("seller_sku") or item.get("seller_custom_field") or "-"
            amount = order_net_revenue(order)
            order_id = order.get("id", "-")
            status_class = "bg-green-100 text-green-800" if order.get("status") == "paid" else "bg-gray-100 text-gray-800"
            html += f"""
            <div class='py-3 flex justify-between items-center'>
                <div>
                    <p class='text-sm font-medium text-gray-800'>{title}</p>
                    <p class='text-xs text-gray-500'>Orden: {order_id} | SKU: {sku} | {order['date_created'][:10]}</p>
                </div>
                <div class='text-right'>
                    <p class='font-semibold'>${amount:.2f}</p>
                    <span class='px-2 py-0.5 text-xs rounded-full {status_class}'>{order.get('status', '-')}</span>
                </div>
            </div>
            """
        html += "</div>"

        if not orders:
            html = "<p class='text-center py-4 text-gray-500'>No hay ventas recientes</p>"

        return HTMLResponse(html)
    finally:
        await client.close()


@app.get("/partials/orders-table", response_class=HTMLResponse)
async def orders_table_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    sort: str = Query("date_desc")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        user = await client.get_user_info()
        orders_data = await client.get_orders(offset=offset, limit=limit, sort=sort)
        raw_orders = orders_data.get("results", [])

        # Fetch net_received_amount from collections API
        # net_received = total - impuestos_retenidos (MeLi ya descontó IVA/ISR)
        # net_real_vendedor = net_received - sale_fee - shipping_cost
        net_amounts = {}  # order_id -> net_received_amount
        for o in raw_orders:
            payments = o.get("payments", [])
            total_net = 0.0
            for p in payments:
                pid = p.get("id")
                if pid:
                    try:
                        net = await client.get_payment_net_amount(str(pid))
                        if net is not None:
                            total_net += net
                    except Exception:
                        pass
            if total_net > 0:
                net_amounts[o.get("id")] = total_net

        # Fetch real sale_fee desde /orders/{id} (search devuelve 0 frecuentemente)
        fee_amounts = {}  # order_id -> total sale_fee
        for o in raw_orders:
            oid = o.get("id")
            # Intentar primero desde los resultados del search
            search_fee = sum(float(oi.get("sale_fee", 0) or 0) for oi in o.get("order_items", []))
            if search_fee > 0:
                fee_amounts[oid] = search_fee
            elif o.get("status") in ("paid", "delivered", "payment_required"):
                try:
                    fee_amounts[oid] = await client.get_order_sale_fee(str(oid))
                except Exception:
                    fee_amounts[oid] = 0.0

        # Fetch shipping costs sequentially (Windows select() FD limit)
        shipping_costs = {}  # order_id -> cost
        for o in raw_orders:
            ship = o.get("shipping", {})
            ship_id = ship.get("id") if isinstance(ship, dict) else None
            if ship_id:
                try:
                    cost = await client.get_shipment_costs(str(ship_id))
                    shipping_costs[o.get("id")] = cost
                except Exception:
                    pass

        enriched = []
        for o in raw_orders:
            total = o.get("total_amount", 0) or 0
            items = o.get("order_items", [])
            # Usar sale_fee real (de /orders/{id} si search devolvió 0)
            total_fees = fee_amounts.get(o.get("id"), 0)
            items_detail = []
            for oi in items:
                item_info = oi.get("item", {})
                qty = oi.get("quantity", 1)
                unit_price = oi.get("unit_price", 0) or 0
                full_price = oi.get("full_unit_price", 0) or 0
                # Distribuir el fee total proporcionalmente si hay varios items
                fee = float(oi.get("sale_fee", 0) or 0)
                iva_fee = round(fee * 0.16, 2)
                subtotal = unit_price * qty
                items_detail.append(SimpleNamespace(
                    title=item_info.get("title", "-"),
                    sku=item_info.get("seller_sku") or item_info.get("seller_custom_field") or "-",
                    item_id=item_info.get("id", "-"),
                    quantity=qty,
                    unit_price=unit_price,
                    full_unit_price=full_price,
                    discount=round((full_price - unit_price) * qty, 2) if full_price > unit_price else 0,
                    subtotal=subtotal,
                    sale_fee=fee,
                    iva_fee=iva_fee,
                    listing_type=oi.get("listing_type_id", "-"),
                ))

            # Shipping cost from API
            shipping = o.get("shipping", {})
            ship_cost = shipping_costs.get(o.get("id"), 0)
            iva_ship = round(ship_cost * 0.16, 2)

            # ─── Cálculo financiero corregido ────────────────────────────────
            # net_received (de /collections) = total - impuestos_retenidos
            #   Incluye IVA + retención ISR ya descontados por MeLi
            # net_real = net_received - sale_fee - shipping_cost
            # taxes    = total - net_received  (lo que MeLi retiene como impuestos)
            net_received = net_amounts.get(o.get("id"), 0)
            if net_received > 0:
                taxes = round(total - net_received, 2)
                net   = round(net_received - total_fees - ship_cost, 2)
            else:
                # Fallback: estimación con IVA conocido
                iva_fee = round(total_fees * 0.16, 2)
                taxes = round(iva_fee + iva_ship, 2)
                net   = round(total - total_fees - taxes - ship_cost, 2)

            # Payment info
            payments = o.get("payments", [])
            approved_payment = None
            for p in payments:
                if p.get("status") == "approved":
                    approved_payment = p
                    break
            payment_method = approved_payment.get("payment_method_id", "-") if approved_payment else "-"
            payment_type = approved_payment.get("payment_type", "-") if approved_payment else "-"
            installments = approved_payment.get("installments", 1) if approved_payment else 1

            buyer_raw = o.get("buyer") or {}
            total_iva = round(total_fees * 0.16, 2)  # IVA estimado sobre comisión

            # GAP 6: profit estimado = neto_meli - costo_bm (si disponible)
            total_cost_est = 0.0
            for oi in items:
                oi_sku = (oi.get("item", {}).get("seller_sku")
                          or oi.get("item", {}).get("seller_custom_field") or "")
                oi_sku_base = normalize_to_bm_sku(oi_sku) if oi_sku else ""
                cost_unit = _sku_cost_map.get(oi_sku_base, 0) or 0
                total_cost_est += cost_unit * oi.get("quantity", 1)
            ganancia_est = round(net - total_cost_est, 2) if total_cost_est > 0 else None
            margen_est = round(ganancia_est / total * 100, 1) if (ganancia_est is not None and total > 0) else None

            enriched.append(SimpleNamespace(
                id=o.get("id", "-"),
                date_created=o.get("date_created", "-"),
                status=o.get("status", "-"),
                buyer=SimpleNamespace(nickname=buyer_raw.get("nickname", "-")),
                total_amount=total,
                total_fees=round(total_fees, 2),
                total_iva=total_iva,
                shipping_cost=ship_cost,
                iva_shipping=iva_ship,
                taxes=taxes,
                net_amount=net,
                ganancia_est=ganancia_est,
                margen_est=margen_est,
                shipping_id=shipping.get("id"),
                payment_method=payment_method,
                payment_type=payment_type,
                installments=installments,
                order_items=items_detail,
                pack_id=o.get("pack_id"),
                tags=o.get("tags", []),
            ))

        # GAP 10: resumen P&L de la página actual
        active_orders = [o for o in enriched if o.status != "cancelled"]
        pl_revenue = sum(o.total_amount for o in active_orders)
        pl_net = sum(o.net_amount for o in active_orders)
        pl_ganancia = sum(o.ganancia_est for o in active_orders if o.ganancia_est is not None)
        pl_has_costs = any(o.ganancia_est is not None for o in active_orders)
        pl_margen = round(pl_net / pl_revenue * 100, 1) if pl_revenue > 0 else 0

        return templates.TemplateResponse(request, "partials/orders_table.html", {
            "orders": enriched,
            "paging": orders_data.get("paging", {}),
            "offset": offset,
            "limit": limit,
            "sort": sort,
            "seller_name": user.get("nickname", "-"),
            "pl_revenue": pl_revenue,
            "pl_net": pl_net,
            "pl_ganancia": pl_ganancia if pl_has_costs else None,
            "pl_margen": pl_margen,
            "pl_count": len(active_orders),
        })
    finally:
        await client.close()


@app.get("/partials/items-grid", response_class=HTMLResponse)
async def items_grid_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query("active"),
    sku: str = Query("", description="Buscar por SKU")
):
    import httpx as _httpx
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        user = await client.get_user_info()
        user_id = user["id"]

        # Si hay búsqueda por SKU, buscar directamente
        if sku and sku.strip():
            sku_clean = sku.strip().upper()
            search_result = await client.get(
                f"/users/{user_id}/items/search",
                params={"seller_sku": sku_clean, "limit": 50}
            )
            item_ids = search_result.get("results", [])
            items_search = {"paging": {"total": len(item_ids), "offset": 0, "limit": len(item_ids)}}
        else:
            # Búsqueda normal por estado
            if status:
                items_search = await client.get_items(offset=offset, limit=limit, status=status)
            else:
                # Todos los estados
                items_search = await client.get(
                    f"/users/{user_id}/items/search",
                    params={"offset": offset, "limit": limit}
                )
            item_ids = items_search.get("results", [])

        items = []
        if item_ids:
            for i in range(0, len(item_ids), 20):
                batch = item_ids[i:i+20]
                items.extend(await client.get_items_details(batch))

        # Enriquecer con sale_price para detectar deals
        bodies_for_sp = []
        for it in items:
            body = it.get("body") or it
            if body.get("id"):
                bodies_for_sp.append(body)
        await _enrich_with_sale_prices(client, bodies_for_sp, id_key="id", price_key="price")

        # Consultar inventario BinManager para cada item (Warehouse + Reserve → disponible real)
        BM_WH_URL  = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
        BM_INV_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
        inventory_map = {}  # item_id -> {MTY, CDMX, TJ, total, avail}
        sku_to_items = {}   # base -> {sku, item_ids}
        for it in items:
            body = it.get("body") or it
            sku = _get_item_sku(body)
            item_id = body.get("id", "")
            if sku and item_id:
                base = normalize_to_bm_sku(sku)
                sku_to_items.setdefault(base, {"sku": sku, "item_ids": []})
                sku_to_items[base]["item_ids"].append(item_id)

        if sku_to_items:
            from app.services.binmanager_client import get_shared_bm as _get_bm_cli
            sem = asyncio.Semaphore(10)
            async def _fetch_inv(base_sku: str, full_sku: str, http: _httpx.AsyncClient):
                async with sem:
                    try:
                        bm_cli = await _get_bm_cli()
                        # Warehouse: desglose MTY/CDMX/TJ (stock físico por almacén)
                        # get_stock_with_reserve: AvailableQTY + Reserve directo de BM
                        # CONCEPTID=1 + LOCATIONID=47,62,68 — única fuente correcta de stock vendible
                        _results = await asyncio.gather(
                            http.post(BM_WH_URL, json={
                                "COMPANYID": 1, "SKU": base_sku, "WarehouseID": None,
                                "LocationID": "47,62,68", "BINID": None,
                                "Condition": _bm_conditions_for_sku(full_sku), "ForInventory": 0, "SUPPLIERS": None,
                            }, timeout=15.0),
                            bm_cli.get_stock_with_reserve(base_sku),
                            return_exceptions=True,
                        )
                        r_wh = _results[0]
                        _stock = _results[1]
                        avail_qty, reserve_qty = _stock if isinstance(_stock, tuple) else (0, 0)
                        mty = cdmx = tj = 0
                        if not isinstance(r_wh, Exception) and r_wh.status_code == 200:
                            for row in (r_wh.json() or []):
                                qty = row.get("QtyTotal", 0) or 0
                                wname = (row.get("WarehouseName") or "").lower()
                                if "monterrey" in wname or "maxx" in wname:
                                    mty += qty
                                elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                                    cdmx += qty
                                else:
                                    tj += qty
                        warehouse_total = mty + cdmx
                        return base_sku, {"MTY": mty, "CDMX": cdmx, "TJ": tj, "total": warehouse_total, "avail": avail_qty, "reserved": reserve_qty}
                    except Exception:
                        pass
                    return base_sku, None

            async with _httpx.AsyncClient() as http:
                tasks = [_fetch_inv(base, sku_to_items[base]["sku"], http) for base in sku_to_items.keys()]
                for coro in asyncio.as_completed(tasks):
                    queried_base, inv = await coro
                    if inv:
                        for b, info in sku_to_items.items():
                            if b.upper() == queried_base.upper():
                                for iid in info["item_ids"]:
                                    inventory_map[iid] = inv

        # Construir metadata por item (brand, model, variaciones)
        item_meta = {}
        for it in items:
            body = it.get("body") or it
            iid = body.get("id", "")
            if not iid:
                continue
            sku = _get_item_sku(body)
            brand = ""
            model = ""
            for attr in body.get("attributes", []):
                aid = attr.get("id", "")
                if aid == "BRAND" and attr.get("value_name"):
                    brand = attr["value_name"]
                elif aid == "MODEL" and attr.get("value_name"):
                    model = attr["value_name"]
            variations = body.get("variations") or []
            shipping = body.get("shipping", {})
            logistic_type = shipping.get("logistic_type", "")
            item_meta[iid] = {
                "sku": sku,
                "brand": brand,
                "model": model,
                "has_variations": len(variations) > 1,
                "variation_count": len(variations),
                "is_full": logistic_type == "fulfillment",
                "logistic_type": logistic_type,
                "sold_qty": body.get("sold_quantity", 0),
            }

        return templates.TemplateResponse(request, "partials/items_grid.html", {            "items": items,
            "paging": items_search.get("paging", {}),
            "offset": offset,
            "limit": limit,
            "status": status,
            "inventory_map": inventory_map,
            "item_meta": item_meta,
        })
    finally:
        await client.close()


@app.get("/api/items/{item_id}/bm-cost")
async def get_item_bm_cost(item_id: str):
    """Devuelve costo BM de un item on-demand (para modal de deals desde inventario)."""
    import httpx
    BM_INV_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
    client = await get_meli_client()
    if not client:
        return JSONResponse({"avg_cost_usd": 0, "retail_price_usd": 0, "error": "No autenticado"})
    try:
        data = await client.get(f"/items/{item_id}")
        sku = _get_item_sku(data) if data else ""
        if not sku:
            return JSONResponse({"avg_cost_usd": 0, "retail_price_usd": 0})
        base = normalize_to_bm_sku(sku)
        async with httpx.AsyncClient() as http:
            resp = await http.post(BM_INV_URL, json={
                "COMPANYID": 1, "SEARCH": base, "CONCEPTID": 8,
                "NUMBERPAGE": 1, "RECORDSPAGE": 10,
            }, headers={"Content-Type": "application/json"}, timeout=30.0)
            if resp.status_code == 200:
                items = resp.json()
                if items and isinstance(items, list):
                    for it in items:
                        if it.get("SKU", "").upper() == base:
                            return JSONResponse({
                                "avg_cost_usd": it.get("AvgCostQTY", 0) or 0,
                                "retail_price_usd": it.get("RetailPrice", 0) or 0,
                            })
                    if items:
                        return JSONResponse({
                            "avg_cost_usd": items[0].get("AvgCostQTY", 0) or 0,
                            "retail_price_usd": items[0].get("RetailPrice", 0) or 0,
                        })
        return JSONResponse({"avg_cost_usd": 0, "retail_price_usd": 0})
    finally:
        await client.close()


@app.get("/partials/items-no-stock", response_class=HTMLResponse)
async def items_no_stock_redirect(request: Request):
    """Legacy redirect -> stock issues."""
    return RedirectResponse("/partials/products-stock-issues", status_code=302)


@app.get("/partials/products-stock-issues", response_class=HTMLResponse)
async def products_stock_issues_partial(request: Request, threshold: int = 10):
    """Stock tab: Reabastecer + Riesgo + Activar. Resultado cacheado 5 min."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        # Cache de resultado completo (evita re-computar cada vez)
        key = f"stock_issues:{client.user_id}:t{threshold}"
        entry = _stock_issues_cache.get(key)
        # Helper: añade orphan_count y active_user_id al ctx (siempre fresco, fuera del cache)
        async def _add_orphan_ctx(c: dict) -> dict:
            c["active_user_id"] = client.user_id
            try:
                from app.services import token_store as _ts
                _orows = await _ts.get_orphan_listings(platform="ml", account_id=client.user_id)
                c["orphan_count"] = len(_orows)
            except Exception:
                c["orphan_count"] = 0
            return c

        _du_stock = getattr(request.state, "dashboard_user", None) or {}
        _is_admin_stock = _du_stock.get("role") == "admin"

        if entry:
            _data_age_s = _time.time() - entry[0]
            ctx = await _add_orphan_ctx(entry[1].copy())
            # Stale banner solo si datos tienen más de 10 min — si no, todo perfecto
            ctx["stale"]        = _data_age_s > 600
            ctx["data_age_min"] = int(_data_age_s // 60)
            # Trigger prewarm en background solo si admin y datos > TTL (30 min)
            if _is_admin_stock and _data_age_s > _STOCK_ISSUES_TTL:
                asyncio.create_task(_prewarm_caches())
            return templates.TemplateResponse(request, "partials/products_stock_issues.html", ctx)
        # Sin cache en absoluto
        if not _is_admin_stock:
            # Operador sin cache disponible — no puede iniciar prewarm
            return HTMLResponse("""
<div class="text-center py-16 text-gray-400">
  <svg class="h-10 w-10 mx-auto mb-3 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
          d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
  </svg>
  <p class="text-sm font-medium text-gray-500">Datos de stock no disponibles aún</p>
  <p class="text-xs text-gray-400 mt-1">El administrador debe ejecutar el cache de stock primero.</p>
</div>
""")
        return HTMLResponse("""
<div id="stock-loading" class="text-center py-16 text-gray-500">
  <svg id="stock-spinner" class="animate-spin h-8 w-8 text-yellow-400 mx-auto mb-3" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
  </svg>
  <p id="stock-status-msg" class="text-sm font-medium text-gray-600">Calculando stock en background...</p>
  <p id="stock-status-sub" class="text-xs text-gray-400 mt-1">Revisando cada 5 segundos...</p>
  <p id="stock-error-msg" class="text-xs text-red-500 mt-2 hidden"></p>
</div>
<script>
(function() {
  var attempts = 0;
  var maxAttempts = 140;  // 140 x 5s = 700s (mas que el timeout de 600s del prewarm)
  function reload() {
    if (window.switchProductTab) window.switchProductTab('stock', '/partials/products-stock-issues');
    else location.reload();
  }
  function poll() {
    attempts++;
    fetch('/api/stock/prewarm-status')
      .then(function(r){ return r.json(); })
      .then(function(s) {
        var sub = document.getElementById('stock-status-sub');
        var err = document.getElementById('stock-error-msg');
        var msg = document.getElementById('stock-status-msg');
        if (s.ready) {
          if (msg) msg.textContent = 'Listo — cargando datos...';
          if (sub) sub.textContent = '';
          reload();
          return;
        }
        if (s.error) {
          // Error — mostrar mensaje y botón manual, NO auto-reload (evita loop infinito)
          var spinner = document.getElementById('stock-spinner');
          if (spinner) spinner.classList.add('hidden');
          if (err) { err.textContent = s.error; err.classList.remove('hidden'); }
          if (msg) msg.textContent = 'Error al calcular stock';
          if (sub) sub.innerHTML = '<button onclick="location.reload()" style="margin-top:8px;padding:4px 12px;background:#facc15;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;">Reintentar</button>';
          return;
        }
        // Si hay datos en cache (aunque sean viejos) cargarlos de inmediato
        if (s.stale_available) {
          if (msg) msg.textContent = 'Cargando datos del último ciclo...';
          if (sub) sub.textContent = '';
          reload();
          return;
        }
        // Sin ningún dato disponible aún
        if (!s.running) {
          var sp2 = document.getElementById('stock-spinner');
          if (sp2) sp2.classList.add('hidden');
          if (msg) msg.textContent = 'Datos de inventario no disponibles';
          if (sub) sub.innerHTML = '<span style="font-size:11px;color:#6b7280">El administrador debe actualizar el inventario desde Sync Stock.</span>';
          return;
        }
        var secs = attempts * 5;
        if (sub) sub.textContent = s.running ? ('Calculando... ' + secs + 's') : ('En espera... ' + secs + 's');
        if (attempts < maxAttempts) {
          setTimeout(poll, 5000);
        } else {
          // Tiempo agotado — mostrar botón manual, NO auto-reload
          var spinner2 = document.getElementById('stock-spinner');
          if (spinner2) spinner2.classList.add('hidden');
          if (msg) msg.textContent = 'El cálculo está tardando más de lo esperado';
          if (sub) sub.innerHTML = '<button onclick="location.reload()" style="margin-top:8px;padding:4px 12px;background:#facc15;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;">Reintentar</button>';
        }
      })
      .catch(function() {
        if (attempts < maxAttempts) setTimeout(poll, 5000);
      });
  }
  setTimeout(poll, 5000);
})();
</script>
""")

    finally:
        await client.close()


# ---------- Product Intelligence: shared cache ----------

import time as _time

# Cache compartido: items details + BM stock (evita re-fetch entre tabs)
_products_cache: dict[str, tuple[float, list]] = {}
_bm_stock_cache: dict[str, tuple[float, dict]] = {}
_sku_cost_map: dict[str, float] = {}  # sku -> costo MXN (del último prewarm)
_category_cache: dict[str, str] = {}  # category_id -> name
_PRODUCTS_CACHE_TTL = 900   # 15 min
_BM_CACHE_TTL = 900         # 15 min
_orders_cache: dict[str, tuple[float, list]] = {}
_ORDERS_CACHE_TTL = 900     # 15 min
# Cache RetailPrice PH por SKU base — TTL 30 min (cambia lentamente)
_bm_retail_ph_cache: dict[str, tuple[float, float]] = {}  # sku -> (ts, price_usd)
_BM_RETAIL_PH_TTL = 1800    # 30 min
# Cache para órdenes de Amazon — TTL corto porque es el dashboard del día
# Key: "{seller_id}:{date}" para invalidar automáticamente al cambiar de día
_amazon_daily_cache: dict[str, tuple[float, dict]] = {}
_AMAZON_DAILY_CACHE_TTL = 180  # 3 min — más fresco que MeLi porque Amazon tiene rate limits estrictos
_sale_price_cache: dict[str, tuple[float, dict | None]] = {}
_SALE_PRICE_CACHE_TTL = 300  # 5 min
_stock_issues_cache: dict[str, tuple[float, dict]] = {}
# Cache cross-account para dashboard general (independiente de cuenta activa)
_multi_account_cache: dict[str, tuple[float, dict]] = {}
_MULTI_ACCOUNT_CACHE_TTL = 300  # 5 minutos
_STOCK_ISSUES_TTL = 1800     # 30 min — operadores trabajan con cache; admin renueva; BM down = servir stale
_products_fetch_lock = asyncio.Lock()  # prevenir doble fetch concurrente
_synced_alert_items: dict[str, float] = {}  # item_id → timestamp de sync; TTL 10 min — excluidos de alertas hasta qty-sync ML


_ALL_MELI_STATUSES = ["active", "paused", "closed", "inactive", "under_review"]

_DB_PRODUCTS_MAX_AGE = 3600  # 1h — usar DB si el sync tiene < 1h de antigüedad

async def _get_all_products_cached(client, include_paused=False, include_all=False) -> list[dict]:
    """Devuelve todos los items, cacheado 15 min.
    OPTIMIZACIÓN: lee primero de ml_listings DB (sincronizado en background por ml_listing_sync).
    Si la DB tiene datos frescos (< 1h), los usa sin llamar ML API — carga instantánea.
    include_all=True trae TODOS los statuses (no usa DB — solo llamada directa a ML).
    Lock previene doble fetch cuando multiples requests concurrentes."""
    if include_all:
        suffix = ":all_statuses"
    elif include_paused:
        suffix = ":with_paused"
    else:
        suffix = ""
    key = f"products:{client.user_id}{suffix}"
    entry = _products_cache.get(key)
    if entry and (_time.time() - entry[0]) < _PRODUCTS_CACHE_TTL:
        return entry[1]

    async with _products_fetch_lock:
        entry = _products_cache.get(key)
        if entry and (_time.time() - entry[0]) < _PRODUCTS_CACHE_TTL:
            return entry[1]

        # --- Intentar leer de DB local (rápido, < 100ms) ---
        # Solo para active/paused — include_all siempre va a ML API (incluye cerrados)
        if not include_all:
            try:
                import json as _json
                max_synced = await token_store.get_ml_listings_max_synced_at(client.user_id)
                db_age = _time.time() - max_synced
                if max_synced > 0 and db_age < _DB_PRODUCTS_MAX_AGE:
                    # "inactive" = "Inactiva sin stock" en UI de ML — auto-desactivado por stock=0.
                    # Se incluye para poder detectar alertas de restock y re-activación.
                    statuses = ["active", "paused", "inactive"] if include_paused else ["active"]
                    db_rows = await token_store.get_ml_listings(client.user_id, statuses=statuses)
                    products_from_db = []
                    for r in db_rows:
                        dj = r.get("data_json") or ""
                        if dj:
                            try:
                                body = _json.loads(dj)
                                if body.get("id"):
                                    products_from_db.append(body)
                            except Exception:
                                pass
                    if products_from_db:
                        import logging as _log
                        _log.getLogger(__name__).info(
                            f"[PRODUCTS-CACHE] DB hit: {len(products_from_db)} items "
                            f"para uid={client.user_id} (edad DB: {db_age:.0f}s)"
                        )
                        _products_cache[key] = (_time.time(), products_from_db)
                        return products_from_db
            except Exception as _db_err:
                import logging as _log
                _log.getLogger(__name__).warning(f"[PRODUCTS-CACHE] DB fallback error: {_db_err}")
                # Continúa con fetch de ML API

        # --- Fallback: ML API (lento, solo si DB vacía o muy antigua) ---
        if include_all:
            all_ids = await client.get_all_item_ids_by_statuses(_ALL_MELI_STATUSES)
        elif include_paused:
            # "inactive" = "Inactiva sin stock" — ML auto-desactivó por qty=0; incluir para alertas.
            all_ids = await client.get_all_item_ids_by_statuses(["active", "paused", "inactive"])
        else:
            all_ids = await client.get_all_active_item_ids()
        all_details = []
        sem = asyncio.Semaphore(10)  # aumentado de 5 → 10 para reducir tiempo de fetch

        async def _batch(ids):
            async with sem:
                try:
                    return await client.get_items_details(ids)
                except Exception:
                    return []

        batches = [all_ids[i:i+20] for i in range(0, len(all_ids), 20)]
        results = await asyncio.gather(*[_batch(b) for b in batches])
        for batch_result in results:
            all_details.extend(batch_result)

        products = []
        for d in all_details:
            body = d.get("body", d)
            if body and body.get("id"):
                products.append(body)

        _products_cache[key] = (_time.time(), products)
        return products


async def _get_orders_cached(client, date_from: str, date_to: str) -> list:
    """Ordenes de 30 dias con cache."""
    key = f"orders:{client.user_id}:{date_from}"
    entry = _orders_cache.get(key)
    if entry and (_time.time() - entry[0]) < _ORDERS_CACHE_TTL:
        return entry[1]
    orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)
    _orders_cache[key] = (_time.time(), orders)
    return orders


async def _load_bm_cache_from_db():
    """Carga el caché BM desde DB al arrancar — evita refetch completo tras restart.
    Solo carga entradas con menos de 30 min de antigüedad."""
    import json as _json, logging as _log
    logger = _log.getLogger(__name__)
    try:
        rows = await token_store.load_bm_stock_cache(max_age_s=7200.0)  # 2h — solo hay >0 en DB
        loaded = 0
        for row in rows:
            sku = row["sku"].upper()
            data = _json.loads(row["data_json"])
            synced_at = float(row["synced_at"])
            # Solo cargar entradas con stock > 0 (DB solo debería tener >0, pero doble guarda)
            if sku not in _bm_stock_cache and data.get("avail_total", 0) > 0:
                _bm_stock_cache[sku] = (synced_at, data)
                loaded += 1
        logger.info(f"[BM-DB] Cargados {loaded} SKUs desde DB (de {len(rows)} disponibles)")
    except Exception as _e:
        import logging as _log2
        _log2.getLogger(__name__).warning(f"[BM-DB] Error cargando desde DB: {_e}")


async def _load_stock_issues_from_db():
    """Carga stock_issues_cache desde SQLite al arrancar.
    El Stock tab muestra datos inmediatamente post-deploy en vez de 'Calculando...'
    El prewarm sigue corriendo en background y sobreescribe con datos frescos al terminar.
    """
    try:
        snapshots = await token_store.load_all_stock_issues_snapshots()
        loaded = 0
        for key, (ts, data) in snapshots.items():
            if key not in _stock_issues_cache:
                _stock_issues_cache[key] = (ts, data)
                loaded += 1
        if loaded:
            logger.info(f"[STOCK-DB] {loaded} snapshots cargados desde DB (post-deploy, stale OK)")
        else:
            logger.info("[STOCK-DB] Sin snapshots en DB — esperando primer prewarm")
    except Exception as _e:
        logger.warning(f"[STOCK-DB] Error cargando snapshots: {_e}")


async def _get_sale_prices_cached(client, item_ids: list[str]) -> dict[str, dict]:
    """Fetch /items/{id}/sale_price para detectar deals activos. Cache 5min.
    Retorna {item_id: {amount, regular_amount, ...}} solo para items CON descuento."""
    now = _time.time()
    result = {}
    to_fetch = []
    for iid in item_ids:
        entry = _sale_price_cache.get(iid)
        if entry and (now - entry[0]) < _SALE_PRICE_CACHE_TTL:
            if entry[1]:
                result[iid] = entry[1]
        else:
            to_fetch.append(iid)

    if not to_fetch:
        return result

    sem = asyncio.Semaphore(10)

    async def _fetch_one(iid):
        async with sem:
            try:
                data = await client.get_item_sale_price(iid)
                if data and data.get("regular_amount") and data.get("amount"):
                    if data["regular_amount"] > data["amount"]:
                        _sale_price_cache[iid] = (now, data)
                        return iid, data
                _sale_price_cache[iid] = (now, None)
                return iid, None
            except Exception:
                _sale_price_cache[iid] = (now, None)
                return iid, None

    results = await asyncio.gather(*[_fetch_one(iid) for iid in to_fetch])
    for iid, data in results:
        if data:
            result[iid] = data
    return result


# --- Seller catalog cache (for cross-sell in questions AI) ---
# Keyed by str(client.user_id). Populated lazily from questions page + background refresh.
_seller_catalog: dict = {}     # {uid: {item_id: {title, permalink, price}}}
_seller_catalog_ts: dict = {}  # {uid: float (last refresh timestamp)}
_CATALOG_CACHE_TTL = 900       # 15 min before background refresh
_seller_catalog_refresh_tasks: set = set()  # active background tasks

async def _refresh_seller_catalog(uid: str, access_token: str, refresh_token: str = ""):
    """Background: fetch all active items for seller uid and populate _seller_catalog."""
    try:
        from app.services.meli_client import MeliClient
        cat_client = MeliClient(access_token=access_token, refresh_token=refresh_token, user_id=uid)
        try:
            # Fetch up to 200 active item IDs
            all_ids = []
            for offset in (0, 100):
                resp = await cat_client.get(
                    f"/users/{uid}/items/search",
                    params={"status": "active", "offset": offset, "limit": 100},
                )
                batch_ids = resp.get("results", [])
                all_ids.extend(batch_ids)
                if len(batch_ids) < 100:
                    break
            if not all_ids:
                return
            # Fetch details in batches of 20
            catalog = dict(_seller_catalog.get(uid, {}))
            for i in range(0, len(all_ids), 20):
                batch = all_ids[i:i+20]
                details = await cat_client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d) if isinstance(d, dict) else {}
                    iid = str(body.get("id", ""))
                    if iid and body.get("permalink"):
                        catalog[iid] = {
                            "title": body.get("title", ""),
                            "permalink": body.get("permalink", ""),
                            "price": body.get("price", 0),
                        }
            _seller_catalog[uid] = catalog
            _seller_catalog_ts[uid] = _time.time()
        finally:
            await cat_client.close()
    except Exception:
        pass
    finally:
        _seller_catalog_refresh_tasks.discard(uid)

# --- Background pre-warm ---
_prewarm_task = None
_prewarm_running: bool = False    # flag global — solo 1 prewarm a la vez
_prewarm_queued: bool = False     # si True, lanzar otro prewarm al terminar el actual
_prewarm_queued_uid: str | None = None  # user_id del prewarm en cola (None = default)
_prewarm_error: str = ""          # último error para mostrar en UI
_prewarm_progress: dict = {"done": 0, "total": 0, "started_at": 0.0}  # progreso en tiempo real
_prewarm_source: str = "auto"     # "auto" | "manual" — para el historial de BM sync log
# Estadísticas de cobertura BM del último bulk fetch — para diagnóstico en Sync Stock
_bm_bulk_stats: dict = {}  # {bulk_gr_rows, bulk_all_rows, found, zero, zero_skus, fallback_used}

# --- BM Bulk Caches (dos: GR-only y ALL-conditions) ---
_bm_bulk_gr_cache:  tuple[float, list] | None = None  # (timestamp, GRA/GRB/GRC/NEW rows)
_bm_bulk_all_cache: tuple[float, list] | None = None  # (timestamp, GRA/GRB/GRC/ICB/ICC/NEW rows)

# --- BM Health Monitor ---
_bm_health: dict = {
    "ok": None,           # None=nunca chequeado, True=up, False=down
    "latency_ms": 0,
    "last_check_ts": 0.0,
    "last_ok_ts": 0.0,
    "consecutive_failures": 0,
    "error_type": None,   # "timeout" | "auth" | "connection" | "error"
    "last_error": "",
}
_bm_health_log: list = []   # últimos 20 registros de health checks

async def _check_bm_health():
    """Verifica salud de BM testeando un endpoint de API real.
    Timeout 20s — evita falsos positivos cuando BM está lento pero funcional.
    Clasifica el error para diagnóstico: timeout / auth / connection / error.
    """
    global _bm_health, _bm_health_log
    t0 = _time.time()
    ok = False
    error_type: str | None = None
    error_msg = ""
    elapsed_ms = 0
    try:
        from app.services.binmanager_client import get_shared_bm as _get_shared_bm_health
        _bm_cli_health = await _get_shared_bm_health()
        _result = await asyncio.wait_for(
            _bm_cli_health.get_stock_with_reserve("SNTV001764"),
            timeout=20.0,
        )
        elapsed_ms = round((_time.time() - t0) * 1000)
        ok = True
    except asyncio.TimeoutError:
        elapsed_ms = round((_time.time() - t0) * 1000)
        ok = False
        error_type = "timeout"
        error_msg = f"Timeout después de {round(_time.time() - t0)}s (umbral: 20s)"
    except Exception as _exc:
        elapsed_ms = round((_time.time() - t0) * 1000)
        ok = False
        _estr = str(_exc).lower()
        if "401" in _estr or "403" in _estr or "unauthorized" in _estr or "login" in _estr:
            error_type = "auth"
        elif "connect" in _estr or "refused" in _estr or "unreachable" in _estr or "name or service" in _estr:
            error_type = "connection"
        else:
            error_type = "error"
        error_msg = str(_exc)[:200]

    # Registrar en log (máx 20 entradas)
    from datetime import datetime as _dt_h
    try:
        from zoneinfo import ZoneInfo as _ZI
        _tj_tz = _ZI("America/Tijuana")
        _time_label = _dt_h.now(_tj_tz).strftime("%H:%M:%S")
    except Exception:
        _time_label = _dt_h.utcfromtimestamp(_time.time()).strftime("%H:%M:%S") + " UTC"
    _bm_health_log.append({
        "ts": _time.time(),
        "time_label": _time_label,
        "ok": ok,
        "latency_ms": elapsed_ms,
        "error_type": error_type,
        "error_msg": error_msg,
    })
    if len(_bm_health_log) > 20:
        _bm_health_log.pop(0)

    _bm_health["last_check_ts"] = _time.time()
    if ok:
        _bm_health["ok"] = True
        _bm_health["latency_ms"] = elapsed_ms
        _bm_health["last_ok_ts"] = _time.time()
        _bm_health["consecutive_failures"] = 0
        _bm_health["error_type"] = None
        _bm_health["last_error"] = ""
    else:
        _bm_health["ok"] = False
        _bm_health["consecutive_failures"] = _bm_health.get("consecutive_failures", 0) + 1
        _bm_health["error_type"] = error_type
        _bm_health["last_error"] = error_msg
        logger.warning(f"[BM-HEALTH] BM no responde — fallo #{_bm_health['consecutive_failures']} ({elapsed_ms}ms) [{error_type}] {error_msg[:80]}")


def _bm_is_confirmed_down() -> bool:
    """True solo cuando el health loop confirmó que BM está caído (≥1 fallo conocido).
    None (nunca chequeado) → False (no bloqueamos por incertidumbre).
    True/False según último check real."""
    return _bm_health.get("ok") is False


async def _bm_health_loop():
    """Chequea BM cada 2 min en background."""
    await asyncio.sleep(10)   # primer check a los 10s del arranque
    while True:
        try:
            await _check_bm_health()
        except Exception as _e:
            logger.error(f"[BM-HEALTH] Error inesperado: {_e}")
        await asyncio.sleep(120)  # cada 2 minutos


async def _prewarm_caches(user_id: str = None):
    """Pre-carga products + orders + BM stock + stock issues en background.
    Solo corre una instancia a la vez. Si se llama mientras ya corre, marca
    _prewarm_queued=True y el prewarm activo lanza otro al terminar.
    user_id: cuenta a precalentar explícitamente. None = usar ContextVar / default."""
    global _prewarm_running, _prewarm_queued, _prewarm_queued_uid, _prewarm_error
    if _prewarm_running:
        _prewarm_queued = True
        if user_id:
            _prewarm_queued_uid = user_id
        return
    _prewarm_running = True
    _prewarm_queued = False
    _prewarm_error = ""
    try:
        client = await get_meli_client(user_id=user_id)
        if not client:
            _prewarm_error = "No hay cliente ML activo"
            return
        # Cargar caché BM desde DB antes del prewarm (usa datos del último sync)
        await _load_bm_cache_from_db()
        try:
            async def _do_prewarm():
                from datetime import datetime, timedelta
                now = datetime.utcnow()
                date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
                date_to = now.strftime("%Y-%m-%d")

                # IMPORTANTE: solo active+paused — cerrados/inactivos no necesitan gestión de stock.
                # include_all=True podría traer miles de items históricos y colgar el prewarm.
                all_bodies, all_orders = await asyncio.gather(
                    _get_all_products_cached(client, include_paused=True),
                    _get_orders_cached(client, date_from, date_to),
                )
                sales_map = _aggregate_sales_by_item(all_orders)
                products = _build_product_list(all_bodies, sales_map)
                _enrich_sku_from_orders(products, all_orders)

                # Fetchear BM para activos + pausados + inactivos.
                # "inactive" = "Inactiva sin stock" — ML auto-desactivó por qty=0.
                # Incluirlos garantiza que SNTV007283-tipo no queden STALE y generen alertas.
                bm_candidates = [
                    p for p in products
                    if p.get("sku") and p.get("status") in ("active", "paused", "inactive")
                ]
                _prewarm_progress["done"] = 0
                # Total = SKUs únicos normalizados (no productos totales).
                # Con include_paused=True puede haber miles de listings del mismo SKU base;
                # la deduplicación por normalize_to_bm_sku garantiza 1 sola consulta BM por SKU.
                _prewarm_progress["total"] = len(set(normalize_to_bm_sku(p["sku"]) for p in bm_candidates))
                _prewarm_progress["started_at"] = _time.time()
                bm_map = await _get_bm_stock_cached(bm_candidates, retry_stale=True)
                _apply_bm_stock(products, bm_map)

                # Persistir caché BM en DB para sobrevivir reinicios.
                # SOLO guardar entradas con avail_total > 0 — los ceros de bulk fallido
                # no deben sobrevivir un restart y generar falsas alarmas de sobreventa.
                # Los ceros genuinos se re-verifican en el primer prewarm post-restart.
                # Persistir entradas con stock > 0 en DB
                try:
                    _bm_entries = [
                        (s, d, t) for s, (t, d) in _bm_stock_cache.items()
                        if d.get("avail_total", 0) > 0
                    ]
                    if _bm_entries:
                        await token_store.upsert_bm_stock_batch(_bm_entries)
                except Exception as _save_exc:
                    logger.warning(f"[PREWARM] Error persistiendo caché BM: {_save_exc}")
                # Nota: log_bm_sync_event se escribe en el finally externo
                # para garantizar que se escriba incluso en timeout.

                # BM metadata: RetailPrice USD, AvgCost, Brand (solo para candidatos)
                await _enrich_with_bm_product_info(bm_candidates)

                # Calcular márgenes en candidatos (necesario para GAP 5, 7, 6)
                fx = await _get_usd_to_mxn(client)
                _calc_margins(bm_candidates, fx)

                # Actualizar mapa SKU→costo para cálculo de profit en órdenes (GAP 6)
                for _p in bm_candidates:
                    if _p.get("sku") and (_p.get("_costo_mxn") or 0) > 0:
                        _sku_cost_map[_p["sku"]] = _p["_costo_mxn"]

                # Pre-computar stock issues result — threshold default=10
                _DEFAULT_THRESHOLD = 10
                # Items sincronizados recientemente (TTL 10 min): excluirlos de todas las alertas
                # hasta que el qty-sync de ML (cada 3 min) actualice ml_listings y el siguiente
                # prewarm refleje el estado real. Evita que otros usuarios vean items ya gestionados.
                _SYNCED_TTL = 600
                _now_sync_ts = _time.time()
                _synced_ids = {iid for iid, ts in list(_synced_alert_items.items()) if _now_sync_ts - ts < _SYNCED_TTL}
                # Limpiar entradas expiradas para que el dict no crezca indefinidamente
                for _exp_id in [iid for iid, ts in list(_synced_alert_items.items()) if _now_sync_ts - ts >= _SYNCED_TTL]:
                    _synced_alert_items.pop(_exp_id, None)

                restock = [p for p in products if p.get("available_quantity", 0) == 0 and (p.get("_bm_avail") or 0) > 0 and p.get("units", 0) > 0 and not p.get("is_full") and p.get("id") not in _synced_ids]
                restock.sort(key=lambda x: x.get("units", 0), reverse=True)
                # "_bm_avail" in p: BM fue consultado y respondió (avail=0 confirmado por BM).
                # Sin esta guarda, productos sin dato BM (fetch fallido) se clasifican como riesgo
                # porque (None or 0)==0. Solo flaggear cuando BM confirmó explícitamente avail=0.
                oversell_risk = [p for p in products if p.get("available_quantity", 0) > 0 and "_bm_avail" in p and p.get("_bm_avail", 0) == 0 and not p.get("is_full") and p.get("sku") and p.get("id") not in _synced_ids]
                oversell_risk.sort(key=lambda x: x.get("available_quantity", 0), reverse=True)
                restock_ids = {p["id"] for p in restock}
                activate = [p for p in products if p.get("available_quantity", 0) == 0 and (p.get("_bm_avail") or 0) > 0 and p["id"] not in restock_ids and not p.get("is_full") and p["id"] not in _synced_ids]
                activate.sort(key=lambda x: x.get("_bm_avail", 0), reverse=True)
                critical = [
                    p for p in products
                    if p.get("available_quantity", 0) > 0
                    and 0 < (p.get("_bm_avail") or 0) <= _DEFAULT_THRESHOLD
                    and not p.get("is_full")
                    and p.get("sku")
                    and p.get("id") not in _synced_ids
                ]
                critical.sort(key=lambda x: x.get("_bm_avail", 0))
                full_no_stock = [p for p in products if p.get("is_full") and p.get("available_quantity", 0) == 0 and (p.get("_bm_avail") or 0) > 0 and p.get("id") not in _synced_ids]
                full_no_stock.sort(key=lambda x: x.get("_bm_avail", 0), reverse=True)
                # GAP 7: Inventario estancado — stock en ambos lados pero 0 ventas en 30d
                stagnant = [
                    p for p in products
                    if (p.get("_bm_avail") or 0) > 0
                    and p.get("available_quantity", 0) > 0
                    and p.get("units", 0) == 0
                    and not p.get("is_full")
                    and p.get("sku")
                ]
                stagnant.sort(
                    key=lambda x: (x.get("_bm_avail") or 0) * (x.get("price") or 1),
                    reverse=True
                )

                # GAP 5: Precio MeLi por debajo del RetailPrice PH de BM
                price_risk = [
                    p for p in bm_candidates
                    if p.get("price", 0) > 0
                    and p.get("_retail_ph_mxn", 0) > 0
                    and p["price"] < p["_retail_ph_mxn"]
                    and p.get("available_quantity", 0) > 0
                    and p.get("sku")
                ]
                price_risk.sort(
                    key=lambda x: x.get("_retail_ph_mxn", 0) - x.get("price", 0),
                    reverse=True
                )

                # Desbalance peligroso: MeLi publica más stock del que hay en BM
                imbalanced = [
                    p for p in products
                    if p.get("available_quantity", 0) > (p.get("_bm_avail") or 0) > 0
                    and not p.get("is_full")
                    and p.get("sku")
                ]
                imbalanced.sort(key=lambda x: x.get("available_quantity", 0) - (x.get("_bm_avail") or 0), reverse=True)
                # CLAVE: usar f"stock_issues:{uid}:t{threshold}" para que coincida con el endpoint
                _sic_key = f"stock_issues:{client.user_id}:t{_DEFAULT_THRESHOLD}"
                _sic_ts   = _time.time()
                _sic_data = {
                    "restock": restock, "oversell_risk": oversell_risk, "activate": activate,
                    "critical": critical, "full_no_stock": full_no_stock, "imbalanced": imbalanced,
                    "stagnant": stagnant, "price_risk": price_risk,
                    "restock_count": len(restock), "lost_revenue": sum(p.get("revenue", 0) for p in restock),
                    "risk_count": len(oversell_risk), "risk_stock": sum(p.get("available_quantity", 0) for p in oversell_risk),
                    "activate_count": len(activate), "activate_stock": sum(p.get("_bm_avail", 0) for p in activate),
                    "critical_count": len(critical), "critical_bm_total": sum(p.get("_bm_avail", 0) for p in critical),
                    "full_no_stock_count": len(full_no_stock), "full_no_stock_bm": sum(p.get("_bm_avail", 0) for p in full_no_stock),
                    "imbalanced_count": len(imbalanced), "imbalanced_gap": sum(p.get("available_quantity", 0) - (p.get("_bm_avail") or 0) for p in imbalanced),
                    "stagnant_count": len(stagnant), "stagnant_bm": sum(p.get("_bm_avail", 0) for p in stagnant),
                    "price_risk_count": len(price_risk), "price_risk_gap": sum(p.get("_retail_ph_mxn", 0) - p.get("price", 0) for p in price_risk),
                    "threshold": _DEFAULT_THRESHOLD,
                }
                _stock_issues_cache[_sic_key] = (_sic_ts, _sic_data)
                # Persistir en SQLite — sobrevive deploys de Railway
                try:
                    await token_store.save_stock_issues_snapshot(_sic_key, _sic_ts, _sic_data)
                except Exception as _e:
                    logger.warning(f"[STOCK-DB] Error guardando snapshot: {_e}")

            # Timeout de 600s — BM puede tener cientos de SKUs sin caché
            await asyncio.wait_for(_do_prewarm(), timeout=600.0)
        except asyncio.TimeoutError:
            _prewarm_error = "Timeout: el calculo tardo mas de 600s."
        finally:
            # Escribir BM sync log SIEMPRE — incluso en timeout o error parcial.
            # Fuera de _do_prewarm() para que sobreviva asyncio.TimeoutError.
            try:
                _bm_log_elapsed = round(_time.time() - _prewarm_progress.get("started_at", _time.time()), 1)
                _bm_log_skus    = len(_bm_stock_cache)
                logger.info(f"[PREWARM] BM sync log: skus={_bm_log_skus}, elapsed={_bm_log_elapsed}s, source={_prewarm_source}")
                await token_store.log_bm_sync_event(
                    sku_count=_bm_log_skus,
                    elapsed_s=_bm_log_elapsed,
                    source=_prewarm_source,
                )
                logger.info("[PREWARM] BM sync log escrito OK")
            except Exception as _bm_log_exc:
                logger.warning(f"[PREWARM] Error escribiendo BM sync log: {_bm_log_exc!r}")
            await client.close()
    except Exception as _e:
        import traceback as _tb
        _prewarm_error = _tb.format_exc()
    finally:
        _prewarm_running = False
        _prewarm_source  = "auto"   # reset siempre — incluso si hubo error/timeout
        # Si hubo un prewarm en cola (p.ej. de "Sync ahora"), lanzarlo preservando el user_id
        if _prewarm_queued:
            _prewarm_queued = False
            _queued_uid = _prewarm_queued_uid
            _prewarm_queued_uid = None
            asyncio.create_task(_prewarm_caches(user_id=_queued_uid))


async def _get_bm_stock_cached(products: list, sku_key="sku", retry_stale: bool = False) -> dict:
    """Devuelve {sku: {mty, cdmx, tj, total, avail_total}} para products, con cache.
    Usa Warehouse endpoint para desglose MTY/CDMX/TJ y InventoryBySKUAndCondicion_Quantity
    para stock verdaderamente disponible (Available, excluyendo reservados).
    retry_stale: True solo en prewarm — hace retry serial de SKUs STALE (puede tardar 60-120s).
                 False en requests del usuario — evita timeout de 90s.
    """
    import httpx, json as _json
    BM_WH_URL   = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
    BM_AVAIL_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"

    result_map = {}
    to_fetch = []
    # Deduplicar por SKU base BM (normalize_to_bm_sku): SNTV007270-GRA y SNTV007270 NEW
    # son el mismo producto en BM → 1 sola consulta, resultado compartido entre ambos.
    _seen_bm_keys: set = set()

    def _cache_is_valid(c):
        # BM caído → TTL doble para no re-intentar fetches que van a fallar
        _bm_down_now = _bm_health.get("consecutive_failures", 0) >= 2
        _effective_ttl = _BM_CACHE_TTL * 2 if _bm_down_now else _BM_CACHE_TTL
        if not c or (_time.time() - c[0]) >= _effective_ttl:
            return False
        data = c[1]
        # Entradas con total=0 y avail=0 solo son válidas si el fetch fue verificado (_v=True).
        # Sin _v (DB antigua, timeout) se re-fetchean para no servir 0s de fetches fallidos.
        if data.get("total", 0) == 0 and data.get("avail_total", 0) == 0:
            return bool(data.get("_v"))
        # total>0, avail=0, reserve=0 → matemáticamente incoherente.
        # Indica fallo silencioso de CONCEPTID=1 (devolvió (0,0) sin encontrar el SKU).
        if data.get("total", 0) > 0 and data.get("avail_total", 0) == 0 and data.get("reserved_total", 0) == 0:
            return False
        return True

    for p in products:
        sku = p.get(sku_key, "")
        if not sku:
            continue
        bm_key = normalize_to_bm_sku(sku)  # clave normalizada: primeros 10 chars
        cached = _bm_stock_cache.get(bm_key)
        if _cache_is_valid(cached):
            result_map[sku] = cached[1]
        elif bm_key not in _seen_bm_keys:
            to_fetch.append(sku)
            _seen_bm_keys.add(bm_key)
        # Si bm_key ya está en _seen_bm_keys (duplicado cross-account o variante),
        # no agregar a to_fetch. result_map se llenará en el post-fetch pass abajo.

    # También incluir SKUs de variaciones para calcular BM correcto por variación
    seen_skus = set(normalize_to_bm_sku(s) for s in to_fetch)
    seen_skus.update(normalize_to_bm_sku(s) for s in result_map)
    for p in products:
        if not p.get("has_variations"):
            continue
        for v in p.get("variations", []):
            v_sku = v.get("sku", "")
            if not v_sku:
                continue
            v_bm_key = normalize_to_bm_sku(v_sku)
            if v_bm_key in seen_skus:
                cached = _bm_stock_cache.get(v_bm_key)
                if _cache_is_valid(cached):
                    result_map[v_sku] = cached[1]
                continue
            cached = _bm_stock_cache.get(v_bm_key)
            if _cache_is_valid(cached):
                result_map[v_sku] = cached[1]
            else:
                to_fetch.append(v_sku)
            seen_skus.add(v_bm_key)

    if not to_fetch:
        return result_map

    # ── MODO CACHE-ONLY ────────────────────────────────────────────────────────
    # Solo el prewarm (retry_stale=True) hace llamadas reales a BM.
    # Requests de usuarios nunca tocan BM — sirven datos stale o vacíos del caché.
    # Esto evita que múltiples usuarios simultáneos saturen BM con llamadas directas.
    if not retry_stale:
        for _sku in to_fetch:
            _bk = normalize_to_bm_sku(_sku)
            _stale = _bm_stock_cache.get(_bk)
            if _stale:
                result_map[_sku] = _stale[1]   # dato antiguo — mejor que 0 falso
            # Si no hay nada en caché, result_map no tiene entrada → _bm_avail=None
            # → no se genera alerta de oversell por dato desconocido
        return result_map

    _EMPTY_BM = {"mty": 0, "cdmx": 0, "tj": 0, "total": 0, "avail_total": 0, "reserved_total": 0}

    def _parse_wh_rows(rows):
        """Suma QtyTotal por almacen. Retorna (mty, cdmx, tj)."""
        mty = cdmx = tj = 0
        for row in (rows or []):
            qty = row.get("QtyTotal", 0) or 0
            wname = (row.get("WarehouseName") or "").lower()
            if "monterrey" in wname or "maxx" in wname:
                mty += qty
            elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                cdmx += qty
            else:
                tj += qty
        return mty, cdmx, tj

    def _store_wh(sku, rows_wh, avail_direct=0, reserve_direct=0, avail_ok=True, wh_responded=True):
        """Parsea filas del Warehouse endpoint (MTY/CDMX/TJ) + avail/reserve directo de BM.

        avail_direct:   AvailableQTY de Get_GlobalStock_InventoryBySKU CONCEPTID=1+LOCATIONID=47,62,68
        reserve_direct: Reserve del mismo endpoint — unidades reservadas para órdenes pendientes.
        avail_ok:       True si get_stock_with_reserve respondió (tuple); False si fue excepción/timeout.
        wh_responded:   True si el WH endpoint devolvió JSON válido (aunque vacío []).
                        False si devolvió HTML (sesión expirada) — JSON parse falló.
                        [] vacío con wh_responded=True ES dato verificado (SKU sin stock en esas ubicaciones).
                        [] vacío con wh_responded=False significa que NO sabemos el estado real.

        Casos verificados:
          SNTV001764: TotalQty=215, Reserve=2, AvailableQTY=213 ✓
          SNAC000029: AvailableQTY=2467, Reserve directo de BM ✓
          SNTV006485: física=1, reservada=1 → AvailableQTY=0 ✓
        """
        mty, cdmx, tj = _parse_wh_rows(rows_wh)
        warehouse_total = mty + cdmx
        avail_total     = int(avail_direct or 0)
        reserved_total  = int(reserve_direct or 0)
        # Fallback avail: si avail=0 con stock físico real, usar warehouse_total.
        # Dos casos que justifican el fallback:
        #   1) avail_ok=False: get_stock_with_reserve lanzó excepción/timeout — dato desconocido
        #   2) avail_ok=True pero reserve=0: CONCEPTID=1 devolvió (0,0) sin encontrar el SKU.
        #      Si TotalQty=67 y Reserve=0, AvailableQTY debería ser 67 — el (0,0) es un fallo silencioso.
        #      Genuinamente reservado tendría reserve > 0 (ej: SNTV006485: total=1, reserve=1 → avail=0 correcto).
        if avail_total == 0 and warehouse_total > 0 and (not avail_ok or reserved_total == 0):
            avail_total = warehouse_total
        # _v=True: BM respondió con datos reales (incluyendo 0 genuino) o ya hay stock.
        # avail_ok=True: get_stock_with_reserve retornó tuple — respuesta verificada aunque sea (0,0).
        # avail_ok=False: retornó None (timeout/sesión) — no sabemos el estado real.
        verified = avail_ok or avail_total > 0 or reserved_total > 0

        # Fix A: si BM falló (verified=False, todo en cero), NO sobreescribir entrada válida.
        # Causa: bajo carga del prewarm, sesión BM expira → get_stock_with_reserve retorna None
        # → avail_ok=False, avail=0 → verified=False → sin esta guarda sobreescribiría la entrada
        # previa con datos buenos (ej: avail_total=2146) → falso oversell_risk hasta TTL.
        if not verified and avail_total == 0 and warehouse_total == 0:
            existing = _bm_stock_cache.get(normalize_to_bm_sku(sku))
            if existing:
                _ex_ts, _ex_data = existing
                if _ex_data.get("_v") and _ex_data.get("avail_total", 0) > 0:
                    return  # Preservar datos previos verificados — no clobber con session-failure zeros

        inv = {"mty": mty, "cdmx": cdmx, "tj": tj, "total": warehouse_total,
               "avail_total": avail_total, "reserved_total": reserved_total,
               "_v": verified}
        # Guardar bajo clave normalizada (base BM SKU, 10 chars) para que todas las variantes
        # del mismo producto (SNTV007270-GRA, SNTV007270 NEW, etc.) compartan una sola entrada.
        _bm_stock_cache[normalize_to_bm_sku(sku)] = (_time.time(), inv)
        # Agregar a result_map con el SKU original de MeLi para que _apply_bm_stock lo encuentre.
        # Incluir también entradas verificadas con stock=0: BM confirmó genuinamente 0 unidades.
        # Si no se incluyen aquí, _apply_bm_stock no puede distinguir "BM dijo 0" de "BM no consultado"
        # y el filtro oversell_risk usa (None or 0)==0 generando falsos positivos.
        if inv["total"] > 0 or avail_total > 0 or verified:
            result_map[sku] = inv
        return inv["total"] > 0 or avail_total > 0

    def _store_empty(sku):
        _bm_stock_cache[normalize_to_bm_sku(sku)] = (_time.time(), _EMPTY_BM)

    # sem=12 + 1 request por SKU = 12 simultáneos máx — holgado bajo el límite de 20 de httpx.
    # Con WH endpoint eliminado: de 2 requests por SKU → 1. Antes sem=12 = 24 simultáneos (WH+avail),
    # ahora sem=12 = 12 simultáneos. Sesión BM estable sin saturar el pool de conexiones.
    wh_sem = asyncio.Semaphore(12)

    async def _wh_phase(sku, http=None, _track_progress=True):
        """Consulta Get_GlobalStock_InventoryBySKU CONCEPTID=1 — fuente única de stock vendible.
        1 solo request HTTP por SKU (WH breakdown eliminado para reducir concurrencia BM).

        Con sem=12 + 1 request = 12 simultáneos máx → bien bajo el límite de 20 de httpx.
        MTY/CDMX/TJ breakdown = 0 (no disponible sin endpoint WH — solo avail total).
        """
        # normalize_to_bm_sku: split en primer espacio/guión → siempre 10 chars BM correctos.
        # _clean_sku_for_bm + _extract_base_sku NO manejan "(cantidad:2)" porque \(\d+\)
        # no matchea texto con letras/dos-puntos, dejando "SNHG000038 cantidad:2" → BM retorna 0.
        base = normalize_to_bm_sku(sku)
        if not base:
            _store_empty(sku)
            return
        async with wh_sem:
            try:
                # get_stock_with_reserve: CONCEPTID=1, LOCATIONID=47,62,68 — fuente única correcta.
                # Retorna (AvailableQTY, Reserve) cuando BM responde con datos reales (incluyendo 0,0).
                # Retorna None cuando hay fallo de sesión/red — diferente de 0 genuino.
                # timeout=8s: cubre re-login interno (3-5s) + latencia normal. Antes era 25s —
                # con BM lento por mantenimiento, 2 rondas × 25s = 50s → timeout de 90s en el tab.
                _stock = await asyncio.wait_for(
                    bm_cli.get_stock_with_reserve(base, conditions=_bm_conditions_for_sku(sku)),
                    timeout=8.0,
                )
                # _avail_ok=True: BM respondió (tuple) — dato verificado aunque sea (0,0) genuino.
                # _avail_ok=False: retornó None (timeout/sesión) — no sabemos el stock real.
                _avail_ok = _stock is not None
                avail_direct = _stock[0] if _avail_ok else 0
                reserve_direct = _stock[1] if _avail_ok else 0
                _store_wh(sku, [], avail_direct=avail_direct, reserve_direct=reserve_direct,
                          avail_ok=_avail_ok, wh_responded=False)
                if _track_progress:
                    _prewarm_progress["done"] = _prewarm_progress.get("done", 0) + 1
                return
            except Exception as _exc:
                import logging as _log
                _log.getLogger(__name__).warning(f"[BM-CACHE] Error para {sku}: {_exc}")
        _store_empty(sku)
        if _track_progress:
            _prewarm_progress["done"] = _prewarm_progress.get("done", 0) + 1

    # Fast-fail: si BM está caído (2+ fallos consecutivos), servir cache stale en lugar de
    # bloquear 600 × 25s = 10+ min esperando timeouts. El health loop detecta la caída.
    if _bm_health.get("consecutive_failures", 0) >= 2:
        logger.warning(f"[BM-CACHE] BM DOWN — skip fetch de {len(to_fetch)} SKUs, usando cache stale")
        for p in products:
            sku = p.get(sku_key, "")
            if not sku or sku in result_map:
                continue
            cached = _bm_stock_cache.get(normalize_to_bm_sku(sku))
            if cached:
                result_map[sku] = cached[1]
        return result_map

    from app.services.binmanager_client import get_shared_bm
    bm_cli = await get_shared_bm()

    if len(to_fetch) > 30:
        # BULK FETCH DUAL: dos caches separados por condición.
        # _bm_bulk_gr_cache  → GRA,GRB,GRC,NEW  — para todo SKU no-SNTV
        # _bm_bulk_all_cache → GRA,GRB,GRC,ICB,ICC,NEW — para SNTV* (cualquier TV puede ser ICB/ICC)
        # BM filtra server-side por CONDITION → no hace falta post-filtrar por campo.
        _BM_COND_GR  = "GRA,GRB,GRC,NEW"
        _BM_COND_ALL = "GRA,GRB,GRC,ICB,ICC,NEW"
        _used_bulk = False
        global _bm_bulk_gr_cache, _bm_bulk_all_cache

        _need_all = any(_bm_conditions_for_sku(s) == _BM_COND_ALL for s in to_fetch)

        # ── GR bulk (15 min TTL normal; ilimitado cuando BM está caído) ────────
        _bm_is_down_now = _bm_health.get("consecutive_failures", 0) >= 2
        _bulk_gr_rows = None
        if _bm_bulk_gr_cache:
            _age_gr = _time.time() - _bm_bulk_gr_cache[0]
            if _age_gr < 900 or _bm_is_down_now:
                # BM caído → servir cache sin importar antigüedad (mejor que 0 falso)
                _bulk_gr_rows = _bm_bulk_gr_cache[1]
                _age_label = f"{round(_age_gr)}s" + (" [STALE-BM-DOWN]" if _bm_is_down_now and _age_gr >= 900 else "")
                logger.info(f"[BM-CACHE] Reutilizando GR bulk cache ({_age_label})")
        if _bulk_gr_rows is None and not _bm_is_down_now:
            try:
                _fresh_gr = await asyncio.wait_for(
                    bm_cli.get_bulk_stock(conditions=_BM_COND_GR),
                    timeout=270.0,
                )
                if _fresh_gr:
                    _bm_bulk_gr_cache = (_time.time(), _fresh_gr)
                    _bm_health["ok"] = True
                    _bm_health["last_ok_ts"] = _time.time()
                    _bm_health["consecutive_failures"] = 0
                    _bulk_gr_rows = _fresh_gr
                    logger.info(f"[BM-CACHE] GR bulk fetch OK: {len(_fresh_gr)} filas")
                else:
                    # Fix B1: BM respondió sin excepción pero devolvió vacío/None — tratar como fallo.
                    # Sin este else, _bulk_gr_rows queda None y el except-stale no se ejecuta → KPIs en 0.
                    logger.warning("[BM-CACHE] GR bulk fetch devolvió vacío — fallback a stale cache")
                    if _bm_bulk_gr_cache:
                        _bulk_gr_rows = _bm_bulk_gr_cache[1]
                        logger.warning(f"[BM-CACHE] GR usando stale ({len(_bulk_gr_rows)} rows) tras bulk vacío")
                    _bm_health["consecutive_failures"] = _bm_health.get("consecutive_failures", 0) + 1
            except Exception as _bulk_err:
                logger.warning(f"[BM-CACHE] GR bulk fetch error: {_bulk_err} — usando stale")
                # Si falla el fetch, intentar usar cache aunque sea antiguo
                if _bm_bulk_gr_cache:
                    _bulk_gr_rows = _bm_bulk_gr_cache[1]
                    logger.warning(f"[BM-CACHE] GR fallback a cache stale tras error fetch")

        # ── ALL bulk (solo si hay SKUs SNTV-ICB/ICC/bundle) ──────────────────
        _bulk_all_rows = None
        if _need_all:
            if _bm_bulk_all_cache:
                _age_all = _time.time() - _bm_bulk_all_cache[0]
                if _age_all < 900 or _bm_is_down_now:
                    _bulk_all_rows = _bm_bulk_all_cache[1]
                    _age_label_all = f"{round(_age_all)}s" + (" [STALE-BM-DOWN]" if _bm_is_down_now and _age_all >= 900 else "")
                    logger.info(f"[BM-CACHE] Reutilizando ALL bulk cache ({_age_label_all})")
            if _bulk_all_rows is None and not _bm_is_down_now:
                try:
                    _fresh_all = await asyncio.wait_for(
                        bm_cli.get_bulk_stock(conditions=_BM_COND_ALL),
                        timeout=270.0,
                    )
                    if _fresh_all:
                        _bm_bulk_all_cache = (_time.time(), _fresh_all)
                        _bulk_all_rows = _fresh_all
                        logger.info(f"[BM-CACHE] ALL bulk fetch OK: {len(_fresh_all)} filas")
                    else:
                        # Fix B2: mismo caso que GR — bulk vacío sin excepción → stale fallback
                        logger.warning("[BM-CACHE] ALL bulk fetch devolvió vacío — fallback a stale cache")
                        if _bm_bulk_all_cache:
                            _bulk_all_rows = _bm_bulk_all_cache[1]
                except Exception as _bulk_err:
                    logger.warning(f"[BM-CACHE] ALL bulk fetch error: {_bulk_err} — usando stale")
                    if _bm_bulk_all_cache:
                        _bulk_all_rows = _bm_bulk_all_cache[1]

        if _bulk_gr_rows is not None or _bulk_all_rows is not None:
            def _build_lookup(rows):
                exact, by_base = {}, {}
                for _brow in (rows or []):
                    _sk = ((_brow.get("SKU") or "")).upper().strip()
                    if not _sk:
                        continue
                    exact[_sk] = _brow
                    by_base.setdefault(_extract_base_sku(_sk), []).append(_brow)
                return exact, by_base

            _exact_gr,  _by_base_gr  = _build_lookup(_bulk_gr_rows)  if _bulk_gr_rows  else ({}, {})
            _exact_all, _by_base_all = _build_lookup(_bulk_all_rows) if _bulk_all_rows else ({}, {})

            def _lookup(exact, by_base, fbase):
                row = exact.get(fbase)
                rows_to_sum = [row] if row is not None else by_base.get(fbase, [])
                if not rows_to_sum:
                    return 0, 0
                avail   = sum(int(v.get("AvailableQTY") or 0) for v in rows_to_sum)
                reserve = sum(int(v.get("Reserve")      or 0) for v in rows_to_sum)
                # Fallback: si BM devuelve AvailableQTY=null/0 pero TotalQty existe,
                # calcularlo nosotros (bulk a veces omite el campo para ítems en ciertos estados).
                if avail == 0 and reserve == 0:
                    total = sum(int(v.get("TotalQty") or 0) for v in rows_to_sum)
                    if total > 0:
                        avail = total  # reserve desconocido → asumir todo disponible
                return avail, reserve

            _diag_found = 0           # SKUs con avail > 0
            _diag_zero_in_bulk  = 0   # encontrados en bulk con avail=0 (BM confirmó 0)
            _diag_not_in_bulk   = 0   # no encontrados en bulk → retry per-SKU
            _diag_zero_in_bulk_skus: list = []
            _diag_not_in_bulk_skus:  list = []
            _diag_fallback = 0        # SKUs donde se usó TotalQty fallback
            _bulk_miss_set: set = set()  # SKUs no en bulk → avail_ok=False → _v=False → retry

            # Wrapper de _lookup con diagnóstico integrado
            def _lookup_diag(exact, by_base, fbase, fsku):
                nonlocal _diag_found, _diag_zero_in_bulk, _diag_not_in_bulk, _diag_fallback
                row = exact.get(fbase)
                rows_to_sum = [row] if row is not None else by_base.get(fbase, [])
                if not rows_to_sum:
                    # SKU no está en bulk — el bulk no lo incluyó, NO significa 0 stock.
                    # Marcarlo como miss: avail_ok=False → _v=False → stale retry hace per-SKU.
                    _diag_not_in_bulk += 1
                    if len(_diag_not_in_bulk_skus) < 50:
                        _diag_not_in_bulk_skus.append(fsku)
                    _bulk_miss_set.add(fsku)
                    return 0, 0
                avail   = sum(int(v.get("AvailableQTY") or 0) for v in rows_to_sum)
                reserve = sum(int(v.get("Reserve")      or 0) for v in rows_to_sum)
                if avail == 0 and reserve == 0:
                    total = sum(int(v.get("TotalQty") or 0) for v in rows_to_sum)
                    if total > 0:
                        avail = total
                        _diag_fallback += 1
                if avail > 0:
                    _diag_found += 1
                else:
                    # BM confirmó explícitamente 0 (SKU en bulk, AvailableQTY=0)
                    _diag_zero_in_bulk += 1
                    if len(_diag_zero_in_bulk_skus) < 30:
                        _diag_zero_in_bulk_skus.append(fsku)
                return avail, reserve

            for _fsku in to_fetch:
                _fbase = normalize_to_bm_sku(_fsku)
                if not _fbase:
                    _store_empty(_fsku)
                    _prewarm_progress["done"] = _prewarm_progress.get("done", 0) + 1
                    continue
                # Usar el bulk correcto según las condiciones del SKU
                if _bm_conditions_for_sku(_fsku) == _BM_COND_ALL:
                    _avail, _res = _lookup_diag(_exact_all, _by_base_all, _fbase, _fsku)
                else:
                    _avail, _res = _lookup_diag(_exact_gr, _by_base_gr, _fbase, _fsku)
                # avail_ok=True solo si BM encontró el SKU en bulk (confirmó el valor, sea 0 o >0).
                # avail_ok=False si no estaba en bulk → _v=False → stale retry hace per-SKU fetch.
                _store_wh(_fsku, [], avail_direct=_avail, reserve_direct=_res,
                          avail_ok=(_fsku not in _bulk_miss_set), wh_responded=False)
                _prewarm_progress["done"] = _prewarm_progress.get("done", 0) + 1

            # Guardar estadísticas de cobertura para diagnóstico en Sync Stock
            global _bm_bulk_stats
            _diag_zero = _diag_zero_in_bulk + _diag_not_in_bulk  # total para compat UI
            _bm_bulk_stats = {
                "bulk_gr_rows":       len(_bulk_gr_rows)  if _bulk_gr_rows  else 0,
                "bulk_all_rows":      len(_bulk_all_rows) if _bulk_all_rows else 0,
                "skus_total":         len(to_fetch),
                "found":              _diag_found,
                "zero":               _diag_zero,
                "zero_in_bulk":       _diag_zero_in_bulk,
                "not_in_bulk":        _diag_not_in_bulk,
                "fallback_used":      _diag_fallback,
                "zero_skus":          _diag_not_in_bulk_skus,    # los que serán retried per-SKU
                "zero_in_bulk_skus":  _diag_zero_in_bulk_skus,   # BM confirmó 0 (no retried)
                "ts":                 _time.time(),
            }
            logger.info(f"[BM-BULK] Cobertura: {_diag_found}/{len(to_fetch)} con stock, "
                        f"{_diag_zero_in_bulk} BM confirmó 0, {_diag_not_in_bulk} no en bulk (retry per-SKU), "
                        f"{_diag_fallback} via TotalQty fallback")
            _used_bulk = True

        if not _used_bulk:
            # Fix B3: último recurso — servir desde _bm_stock_cache per-SKU aunque esté expirado.
            # Cuando ambos bulk fallan (stale también ausente), mejor datos viejos que result_map vacío.
            # result_map vacío → _apply_bm_stock no puede set _bm_avail → KPIs todos en 0.
            logger.warning(f"[BM-CACHE] Bulk falló — sirviendo _bm_stock_cache stale per-SKU para {len(to_fetch)} SKUs. Retry en próximo ciclo.")
            for _fsku in to_fetch:
                _fbase = normalize_to_bm_sku(_fsku)
                _stale_cached = _bm_stock_cache.get(_fbase)
                if _stale_cached:
                    result_map[_fsku] = _stale_cached[1]
    else:
        # Lote pequeño (≤30 SKUs): per-SKU directo sigue siendo eficiente
        await asyncio.gather(*[_wh_phase(s) for s in to_fetch], return_exceptions=True)

    # Fix C: retry serial para SKUs STALE — fire-and-forget para no bloquear prewarm ni Stock tab.
    # Solo en prewarm (retry_stale=True). Se lanza como tarea background y prewarm continúa.
    if retry_stale:
        _stale_after_prewarm = [
            s for s in to_fetch
            if not _bm_stock_cache.get(normalize_to_bm_sku(s), (None, {}))[1].get("_v")
        ]
        _stale_to_retry = _stale_after_prewarm[:100]
        if _stale_to_retry:
            async def _do_stale_retry(_skus=_stale_to_retry):
                import logging as _log_retry
                _log_retry.getLogger(__name__).info(
                    f"[BM-CACHE] Retry serial BG: {len(_skus)} SKUs STALE — esperando 10s para sesión BM"
                )
                # Esperar 10s: deja que la sesión BM se estabilice tras el parallel prewarm
                # y que el prewarm de la próxima cuenta no compita con estos retries.
                await asyncio.sleep(10)
                for _retry_sku in _skus:
                    await _wh_phase(_retry_sku, _track_progress=False)
                    # 2s entre retries: breathing room para BM — evita saturar la sesión
                    await asyncio.sleep(2)
                _log_retry.getLogger(__name__).info(
                    f"[BM-CACHE] Retry serial BG completado: {len(_skus)} SKUs procesados"
                )
            asyncio.create_task(_do_stale_retry())

    # Post-fetch pass: llenar result_map para SKUs que fueron deduplicados (bm_key ya en
    # _seen_bm_keys pero result_map nunca se pobló). Ahora el cache sí tiene el dato.
    # Ejemplo: "SNTV007270-GRA" fue el SKU fetcheado; "SNTV007270 NEW" del mismo prewarm
    # fue omitido de to_fetch (misma bm_key) → aquí se sirve del cache.
    for p in products:
        sku = p.get(sku_key, "")
        if not sku or sku in result_map:
            continue
        bm_key = normalize_to_bm_sku(sku)
        cached = _bm_stock_cache.get(bm_key)
        if cached and _cache_is_valid(cached):
            result_map[sku] = cached[1]
    # Lo mismo para variaciones
    for p in products:
        if not p.get("has_variations"):
            continue
        for v in p.get("variations", []):
            v_sku = v.get("sku", "")
            if not v_sku or v_sku in result_map:
                continue
            v_bm_key = normalize_to_bm_sku(v_sku)
            cached = _bm_stock_cache.get(v_bm_key)
            if cached and _cache_is_valid(cached):
                result_map[v_sku] = cached[1]

    # Persistir nuevas entradas BM a DB (fire-and-forget — no bloquea la respuesta)
    if to_fetch:
        now_ts = _time.time()
        entries_to_persist = []
        for _sku in to_fetch:
            _bm_key = normalize_to_bm_sku(_sku)
            cached = _bm_stock_cache.get(_bm_key)
            if cached:
                entries_to_persist.append((_sku, cached[1], cached[0]))
        if entries_to_persist:
            async def _persist_bm():
                try:
                    await token_store.upsert_bm_stock_batch(entries_to_persist)
                except Exception as _e:
                    import logging as _log
                    _log.getLogger(__name__).debug(f"[BM-DB] persist error: {_e}")
            asyncio.create_task(_persist_bm())

    return result_map


def _apply_bm_stock(products: list, bm_map: dict, sku_key="sku"):
    """Aplica datos de stock BM a la lista de productos.
    _bm_avail = disponible real (excluye reservados para órdenes pendientes).
    _bm_reserved = unidades reservadas (Required en BM).
    _bm_total = stock físico total (incluye reservados) — solo para referencia.
    """
    for p in products:
        if p.get("has_variations"):
            any_var_sku = False
            # Group variations by their normalized BM key to avoid double-counting
            # when multiple ML variations map to the same BM SKU.
            from collections import defaultdict as _dd
            _bm_key_vars: dict = _dd(list)  # bm_key → [variation_dicts]
            for v in p.get("variations", []):
                v_sku = v.get("sku", "")
                if v_sku:
                    any_var_sku = True
                _bk = normalize_to_bm_sku(v_sku) if v_sku else ""
                _bm_key_vars[_bk].append(v)

            tot_mty = tot_cdmx = tot_tj = tot_avail = tot_reserved = 0
            _any_inv_found = False  # Fix B4: rastrear si al menos una variación tuvo dato BM real
            for _bk, _vars in _bm_key_vars.items():
                # Look up BM data: try each variation's raw SKU until we find a match
                inv = None
                if _bk:
                    for _v in _vars:
                        inv = bm_map.get(_v.get("sku", ""))
                        if inv:
                            break
                if inv:
                    _any_inv_found = True
                    _n = len(_vars)
                    _pool = inv.get("avail_total", 0)
                    _quot, _rem = divmod(_pool, _n)
                    # Deduplicated parent totals — add once per unique BM key
                    tot_mty += inv["mty"]
                    tot_cdmx += inv["cdmx"]
                    tot_tj += inv["tj"]
                    tot_avail += _pool
                    tot_reserved += inv.get("reserved_total", 0)
                    for i, v in enumerate(_vars):
                        v["_bm_total"] = inv["total"] // _n if _n > 1 else inv["total"]
                        v["_bm_avail"] = _quot + (1 if i == 0 and _rem else 0)
                        v["_bm_reserved"] = inv.get("reserved_total", 0) // _n
                else:
                    for v in _vars:
                        v["_bm_total"] = 0
                        v["_bm_avail"] = 0
                        v["_bm_reserved"] = 0

            # Fix B4: solo asignar _bm_avail al padre si al menos una variación tuvo dato BM real.
            # Sin esta guarda, _apply_bm_stock asigna _bm_avail=0 cuando bm_map está vacío (bulk falló),
            # lo cual hace que el filtro oversell_risk (que usa `"_bm_avail" in p` como guarda) genere
            # falsos positivos — productos con available_quantity>0 aparecen como riesgo de sobreventa.
            if any_var_sku and _any_inv_found:
                p["_bm_mty"] = tot_mty
                p["_bm_cdmx"] = tot_cdmx
                p["_bm_tj"] = tot_tj
                p["_bm_total"] = tot_mty + tot_cdmx
                p["_bm_avail"] = tot_avail
                p["_bm_reserved"] = tot_reserved
            else:
                inv = bm_map.get(p.get(sku_key))
                if inv:
                    p["_bm_mty"] = inv["mty"]
                    p["_bm_cdmx"] = inv["cdmx"]
                    p["_bm_tj"] = inv["tj"]
                    p["_bm_total"] = inv["total"]
                    p["_bm_avail"] = inv.get("avail_total", 0)
                    p["_bm_reserved"] = inv.get("reserved_total", 0)
        else:
            inv = bm_map.get(p.get(sku_key))
            if inv:
                p["_bm_mty"] = inv["mty"]
                p["_bm_cdmx"] = inv["cdmx"]
                p["_bm_tj"] = inv["tj"]
                p["_bm_total"] = inv["total"]
                p["_bm_avail"] = inv.get("avail_total", 0)
                p["_bm_reserved"] = inv.get("reserved_total", 0)


def _enrich_sku_from_orders(products: list, orders: list):
    """Enriquece SKU de productos desde ordenes (fallback para items sin SKU en datos)."""
    sku_map = {}
    for order in orders:
        for oi in order.get("order_items", []):
            it = oi.get("item", {})
            raw_sku = it.get("seller_sku") or it.get("seller_custom_field") or ""
            if raw_sku and it.get("id"):
                base = normalize_to_bm_sku(raw_sku)
                existing = sku_map.get(it["id"], "")
                if not existing or len(base) < len(existing):
                    sku_map[it["id"]] = base
    for p in products:
        if not p.get("sku") and p["id"] in sku_map:
            p["sku"] = sku_map[p["id"]]


def _get_var_sku(v: dict) -> str:
    """Extrae SKU de una variación (seller_custom_field o SELLER_SKU attribute)."""
    v_sku = v.get("seller_custom_field") or ""
    if not v_sku or v_sku == "None":
        v_sku = ""
        for va in v.get("attributes", []):
            if va.get("id") == "SELLER_SKU" and va.get("value_name"):
                v_sku = va["value_name"]
                break
    return v_sku


def _build_product_list(bodies: list, sales_map: dict = None) -> list[dict]:
    """Construye lista de productos desde item bodies con SKU y ventas."""
    products = []
    for body in bodies:
        iid = body.get("id", "")
        if not iid:
            continue
        sku = _get_item_sku(body)
        shipping = body.get("shipping", {})

        # Para items con variaciones, mostrar la suma de stock de TODAS las variaciones
        # en la fila del parent. Cada variacion tiene su stock independiente en MeLi.
        # El parent.available_quantity puede ser 0 aunque una variacion tenga stock
        # (comportamiento FULL donde cada variacion es independiente).
        raw_vars = body.get("variations", [])
        avail_qty = body.get("available_quantity", 0)
        if raw_vars and len(raw_vars) > 1:
            # Sumar stock de todas las variaciones — refleja la realidad del listing
            avail_qty = sum(v.get("available_quantity", 0) for v in raw_vars)

        p = {
            "id": iid,
            "title": body.get("title", ""),
            "thumbnail": body.get("thumbnail", ""),
            "price": body.get("price", 0),
            "original_price": body.get("original_price"),
            "available_quantity": avail_qty,
            "sku": sku,
            "permalink": body.get("permalink", ""),
            "pictures_count": len(body.get("pictures", [])),
            "has_video": body.get("video_id") is not None,
            "category_id": body.get("category_id", ""),
            "status": body.get("status", "active"),
            "is_full": shipping.get("logistic_type", "") == "fulfillment",
            "catalog_listing": bool(body.get("catalog_listing")),
        }
        if sales_map:
            s = sales_map.get(iid, {"units": 0, "revenue": 0, "fees": 0})
            p["units"] = s["units"]
            p["units_30d"] = s["units"]
            p["revenue"] = s["revenue"]
            p["revenue_30d"] = s["revenue"]
            p["fees"] = s.get("fees", 0)
        else:
            p["units_30d"] = 0
            p["revenue_30d"] = 0

        # Extraer variaciones si hay mas de 1
        if len(raw_vars) > 1:
            variations = []
            for v in raw_vars:
                v_sku = _get_var_sku(v)
                combos = []
                for ac in v.get("attribute_combinations", []):
                    combos.append(f"{ac.get('name', '')}: {ac.get('value_name', '')}")
                variations.append({
                    "id": v.get("id", ""),
                    "sku": v_sku,
                    "stock": v.get("available_quantity", 0),
                    "price": v.get("price", p["price"]),
                    "combo": ", ".join(combos) if combos else f"Var {v.get('id', '')}",
                })
            p["variations"] = variations
            p["has_variations"] = True

        products.append(p)
    return products


async def _enrich_category_names(client, products: list):
    """Resuelve category_id -> nombre legible. Usa cache permanente."""
    cat_ids = set()
    for p in products:
        cid = p.get("category_id", "")
        if cid and cid not in _category_cache:
            cat_ids.add(cid)
    if cat_ids:
        sem = asyncio.Semaphore(10)
        async def _fetch_cat(cid):
            async with sem:
                try:
                    data = await client.get(f"/categories/{cid}")
                    return cid, data.get("name", cid)
                except Exception:
                    return cid, cid
        results = await asyncio.gather(*[_fetch_cat(c) for c in cat_ids])
        for cid, name in results:
            _category_cache[cid] = name
    for p in products:
        cid = p.get("category_id", "")
        p["category_name"] = _category_cache.get(cid, cid)


async def _enrich_variation_skus(client, products: list):
    """Para items con variaciones sin SKU, fetcha datos individuales para obtener SELLER_SKU."""
    # Identificar items que tienen variaciones con SKU vacios
    needs_fetch = []
    for p in products:
        vars_list = p.get("variations")
        if not vars_list:
            continue
        has_empty = any(not v.get("sku") for v in vars_list)
        if has_empty:
            needs_fetch.append(p["id"])

    if not needs_fetch:
        return

    # Limitar a 20 items para no sobrecargar
    needs_fetch = needs_fetch[:20]
    sem = asyncio.Semaphore(5)

    async def _fetch(item_id):
        async with sem:
            try:
                data = await client.get(f"/items/{item_id}")
                return item_id, data
            except Exception:
                return item_id, None

    results = await asyncio.gather(*[_fetch(iid) for iid in needs_fetch], return_exceptions=True)

    # Mapear variaciones actualizadas
    var_sku_map = {}  # {item_id: {var_id: sku}}
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        iid, data = r
        if not data or not isinstance(data, dict):
            continue
        for v in data.get("variations", []):
            vid = v.get("id")
            if not vid:
                continue
            v_sku = v.get("seller_custom_field") or ""
            if not v_sku or v_sku == "None":
                v_sku = ""
                for va in v.get("attributes", []):
                    if va.get("id") == "SELLER_SKU" and va.get("value_name"):
                        v_sku = va["value_name"]
                        break
            if v_sku:
                var_sku_map.setdefault(iid, {})[str(vid)] = v_sku

    # Aplicar SKUs encontrados
    for p in products:
        iid = p["id"]
        if iid not in var_sku_map:
            continue
        for v in p.get("variations", []):
            if not v.get("sku"):
                v["sku"] = var_sku_map[iid].get(str(v["id"]), "")


# ---------- Product Intelligence tabs ----------

@app.post("/api/cache/invalidate-products")
async def invalidate_products_cache():
    """Invalida cache de productos para forzar re-fetch desde MeLi."""
    cleared = 0
    for cache in (_products_cache, _orders_cache, _sale_price_cache):
        cleared += len(cache)
        cache.clear()
    return {"ok": True, "cleared": cleared}


@app.get("/partials/products-inventory", response_class=HTMLResponse)
async def products_inventory_partial(
    request: Request,
    preset: str = "all",
    search: str = "",
    sort_by: str = "",
    enrich: str = "basic",
    full_filter: str = "all",
    page: int = 1,
    per_page: int = 20,
    alert_days: int = 30,
):
    """Tab Inventario unificado: reemplaza all/top/stock/low/full."""
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        days = max(7, min(alert_days, 90))
        date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Fase 1: fetch paralelo (products + orders)
        # include_paused=True (active+paused) — misma clave que usa el prewarm,
        # por lo que el prewarm ya calienta este cache y la respuesta es instantanea.
        # include_all=True traía closed/inactive/under_review → key diferente → siempre frío.
        all_bodies, all_orders = await asyncio.gather(
            _get_all_products_cached(client, include_paused=True),
            _get_orders_cached(client, date_from, date_to),
        )
        sales_map = _aggregate_sales_by_item(all_orders)
        products = _build_product_list(all_bodies, sales_map)
        _enrich_sku_from_orders(products, all_orders)

        # --- Apply CACHED BM stock (instant, no API calls) ---
        # Only use whatever is already in the BM cache from prewarm/previous loads
        # Entries with total=0 AND avail=0 (EMPTY) are skipped — will be re-fetched per page
        for p in products:
            sku = p.get("sku", "")
            if sku:
                cached = _bm_stock_cache.get(normalize_to_bm_sku(sku))
                if cached and (_time.time() - cached[0]) < _BM_CACHE_TTL:
                    data = cached[1]
                    p["_bm_total"] = data.get("total", 0)
                    p["_bm_mty"] = data.get("mty", 0)
                    p["_bm_cdmx"] = data.get("cdmx", 0)
                    p["_bm_tj"] = data.get("tj", 0)
                    p["_bm_avail"] = data.get("avail_total", 0)
                    p["_bm_reserved"] = data.get("reserved_total", 0)
                    p["_bm_has_data"] = True

        # Recomendaciones
        for p in products:
            recs = []
            if p["pictures_count"] < 6:
                recs.append("Agregar mas fotos (minimo 8)")
            if not p["has_video"]:
                recs.append("Agregar video al listado")
            if p["available_quantity"] == 0:
                recs.append("Reponer stock para no perder posicionamiento")
            if p.get("units", 0) == 0 and p["available_quantity"] > 0:
                recs.append("Candidato para deal/promocion")
            p["recommendations"] = recs

        # --- Stock alerts from cached BM data (no waiting) ---
        # Muestra items con ventas, sin stock MeLi, y BM disponible>0 para alertar al usuario
        _sa_now = _time.time()
        _sa_synced = {iid for iid, ts in _synced_alert_items.items() if _sa_now - ts < 600}
        stock_alerts = [
            p for p in products
            if p.get("units", 0) > 0
            and p.get("available_quantity", 0) == 0
            and (p.get("_bm_avail") or 0) > 0  # tiene stock disponible (excluye reservados)
            and p.get("id") not in _sa_synced
        ]
        stock_alerts.sort(key=lambda x: x.get("units", 0), reverse=True)

        # --- Filtrado por preset ---
        if preset == "top":
            products = [p for p in products if p.get("units", 0) > 0]
        elif preset == "stock":
            products = [p for p in products if p.get("_bm_avail", 0) > 0]
        elif preset == "low":
            products = [p for p in products if p.get("units", 0) <= 2]
        elif preset == "full":
            products = [p for p in products if p.get("is_full")]
        elif preset == "no_stock":
            products = [p for p in products if p.get("available_quantity", 0) == 0]
        elif preset == "accion":
            # Vista unificada de urgencia: sobreventa + sin stock + stock crítico
            THRESHOLD = 10
            if enrich != "full":
                enrich = "full"  # siempre enriquecido para mostrar costos y márgenes
            risk_ids = {
                p["id"] for p in products
                if p.get("available_quantity", 0) > 0
                and (p.get("_bm_avail") or 0) == 0
                and not p.get("is_full")
                and p.get("sku")
            }
            restock_ids = {
                p["id"] for p in products
                if p.get("available_quantity", 0) == 0
                and (p.get("_bm_avail") or 0) > 0
                and p.get("units", 0) > 0
            }
            critical_ids = {
                p["id"] for p in products
                if p.get("available_quantity", 0) > 0
                and 0 < (p.get("_bm_avail") or 0) <= THRESHOLD
                and not p.get("is_full")
                and p.get("sku")
                and p["id"] not in risk_ids
            }
            for p in products:
                pid = p["id"]
                if pid in risk_ids:
                    p["_urgency"] = "risk"
                elif pid in restock_ids:
                    p["_urgency"] = "restock"
                elif pid in critical_ids:
                    p["_urgency"] = "critical"
            products = [p for p in products if p.get("_urgency")]
            _urg_order = {"risk": 0, "restock": 1, "critical": 2}
            products.sort(key=lambda x: (_urg_order[x.get("_urgency", "critical")], -x.get("units", 0)))

        # Filtro FULL adicional
        if full_filter == "full":
            products = [p for p in products if p.get("is_full")]
        elif full_filter == "not_full":
            products = [p for p in products if not p.get("is_full")]

        # Busqueda por texto (incluye SKUs de variaciones)
        # Ademas busca directamente en MeLi API para encontrar items que no esten en cache
        if search and search.strip():
            q = search.strip().lower()

            # 1. Busqueda directa en MeLi API por seller_sku y keyword
            existing_ids = {p["id"] for p in products}
            extra_ids = set()
            try:
                # Buscar por seller_sku (mas preciso)
                sku_results = await client.get(
                    f"/users/{client.user_id}/items/search",
                    params={"seller_sku": search.strip(), "limit": 50},
                )
                for rid in sku_results.get("results", []):
                    if rid not in existing_ids:
                        extra_ids.add(rid)
                # Buscar por keyword (titulo, etc)
                kw_results = await client.get(
                    f"/users/{client.user_id}/items/search",
                    params={"q": search.strip(), "limit": 50},
                )
                for rid in kw_results.get("results", []):
                    if rid not in existing_ids:
                        extra_ids.add(rid)
            except Exception:
                pass

            # 2. Fetch detalles de items extra encontrados
            if extra_ids:
                extra_list = list(extra_ids)
                sem = asyncio.Semaphore(5)
                async def _fbatch(ids):
                    async with sem:
                        try:
                            return await client.get_items_details(ids)
                        except Exception:
                            return []
                batches = [extra_list[i:i+20] for i in range(0, len(extra_list), 20)]
                batch_results = await asyncio.gather(*[_fbatch(b) for b in batches])
                extra_bodies = [b for br in batch_results for b in br]
                extra_products = _build_product_list(extra_bodies, sales_map)
                _enrich_sku_from_orders(extra_products, all_orders)
                # Aplicar BM cache
                for p in extra_products:
                    sku_val = p.get("sku", "")
                    if sku_val:
                        cached = _bm_stock_cache.get(normalize_to_bm_sku(sku_val))
                        if cached and (_time.time() - cached[0]) < _BM_CACHE_TTL:
                            bm_data = cached[1]
                            p["_bm_total"] = bm_data.get("total", 0)
                            p["_bm_mty"] = bm_data.get("mty", 0)
                            p["_bm_cdmx"] = bm_data.get("cdmx", 0)
                            p["_bm_tj"] = bm_data.get("tj", 0)
                            p["_bm_avail"] = bm_data.get("avail_total", 0)
                            p["_bm_reserved"] = bm_data.get("reserved_total", 0)
                            p["_bm_has_data"] = True
                products.extend(extra_products)
                existing_ids.update(extra_ids)

            # 3. Filtrar por texto
            def _matches(p):
                if q in p.get("id", "").lower():
                    return True
                if q in (p.get("sku") or "").lower():
                    return True
                if q in p.get("title", "").lower():
                    return True
                for v in p.get("variations", []):
                    if q in (v.get("sku") or "").lower():
                        return True
                return False
            products = [p for p in products if _matches(p)]

        total_count = len(products)

        # Ordenamiento
        if not sort_by:
            sort_by = {
                "top": "units_desc",
                "stock": "bm_desc",
                "low": "stock_desc",
                "full": "stock_desc",
                "no_stock": "units_desc",
            }.get(preset, "stock_desc")

        field, direction = (sort_by.rsplit("_", 1) + ["desc"])[:2]
        reverse = direction == "desc"
        sort_keys = {
            "stock": lambda p: p.get("available_quantity", 0),
            "units": lambda p: p.get("units", 0),
            "bm": lambda p: p.get("_bm_avail", 0),
            "price": lambda p: p.get("price", 0),
            "revenue": lambda p: p.get("revenue", 0),
            "margin": lambda p: p.get("_margen_pct") if p.get("_margen_pct") is not None else -999,
            "photos": lambda p: p.get("pictures_count", 0),
        }
        products.sort(key=sort_keys.get(field, sort_keys["stock"]), reverse=reverse)

        # --- Pagination ---
        from math import ceil
        if per_page <= 0:
            per_page = 20
        if per_page >= total_count:
            total_pages = 1
            page = 1
            page_products = products
        else:
            total_pages = max(1, ceil(total_count / per_page))
            page = max(1, min(page, total_pages))
            start = (page - 1) * per_page
            page_products = products[start:start + per_page]

        # --- Trigger background BM prefetch para candidatos (non-blocking) ---
        # Solo productos con ventas o stock (no todos los 6000+) para no timeout.
        _bg_key = f"bm_bg:{client.user_id}"
        if _bg_key not in _bm_stock_cache:
            _bm_stock_cache[_bg_key] = (_time.time(), {})
            _bg_candidates = [
                p for p in products
                if p.get("sku") and (p.get("units", 0) > 0 or p.get("available_quantity", 0) > 0 or p.get("is_full"))
            ]
            asyncio.ensure_future(_get_bm_stock_cached(_bg_candidates))

        # --- Enrich ONLY page products ---
        # ORDEN CRITICO: variation SKUs deben popularse ANTES del BM fetch.
        # Si corren en paralelo, _get_bm_stock_cached ve variaciones sin SKU y solo
        # fetcha el padre. Luego _apply_bm_stock ve variaciones con SKU (enriquecidas)
        # pero bm_map no tiene esos SKUs específicos → BM=0 para todos con variaciones.
        usd_to_mxn = 0.0
        page_ids = [p["id"] for p in page_products]

        # Paso 1: SKUs de variaciones (debe terminar antes del BM fetch)
        await _enrich_variation_skus(client, page_products)

        # Paso 2: BM + precios en paralelo (variaciones ya tienen SKU correcto)
        parallel_tasks = [
            _get_bm_stock_cached(page_products),
            _get_sale_prices_cached(client, page_ids),
        ]
        if enrich == "full":
            parallel_tasks.append(_enrich_with_bm_product_info(page_products))
            parallel_tasks.append(_get_usd_to_mxn(client))

        enrich_results = await asyncio.gather(*parallel_tasks)
        bm_map = enrich_results[0]
        sale_prices = enrich_results[1]

        _apply_bm_stock(page_products, bm_map)

        if enrich == "full":
            usd_to_mxn = enrich_results[3] if len(enrich_results) > 3 else 0.0
            _calc_margins(page_products, usd_to_mxn)

        for p in page_products:
            sp = sale_prices.get(p["id"])
            if sp:
                p["original_price"] = sp["regular_amount"]
                p["price"] = sp["amount"]
                p["_has_deal"] = True
            else:
                p["_has_deal"] = False

        # --- Enrich alerts with estimated lost revenue ---
        for a in stock_alerts:
            units = a.get("units", 0)
            price = a.get("price", 0)
            d = alert_days or 30
            daily_avg = units / d if d > 0 else 0
            a["_alert_units"] = units
            a["_alert_revenue"] = round(units * price, 0)
            a["_est_lost_revenue"] = round(daily_avg * price * d * 0.5, 0)
        stock_alerts.sort(key=lambda x: x.get("_est_lost_revenue", 0), reverse=True)

        return templates.TemplateResponse(request, "partials/products_inventory.html", {            "products": page_products,
            "preset": preset,
            "search": search,
            "sort_by": sort_by,
            "enrich": enrich,
            "full_filter": full_filter,
            "total_count": total_count,
            "usd_to_mxn": round(usd_to_mxn, 2),
            "stock_alerts": stock_alerts,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "alert_days": alert_days,
        })
    finally:
        await client.close()


# --- Mark alert item as synced (evita duplicar trabajo entre usuarios) ---

@app.post("/partials/mark-synced/{item_id}")
async def mark_alert_synced(item_id: str):
    """Registra un item como sincronizado; se excluye de alertas hasta que la cache expire."""
    _synced_alert_items[item_id] = _time.time()
    return Response(status_code=204)


# --- Legacy redirects (old tabs → new inventory endpoint) ---

@app.get("/partials/products-all", response_class=HTMLResponse)
async def products_all_partial(request: Request):
    return await products_inventory_partial(request, preset="all")


@app.get("/partials/products-summary", response_class=HTMLResponse)
async def products_summary_partial(request: Request):
    """KPIs — ultra rapido: solo ordenes (cached) + item count."""
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Ordenes (cached despues del 1er fetch) + item count: 2 calls en paralelo
        all_orders, items_data = await asyncio.gather(
            client.fetch_all_orders(date_from=date_from, date_to=date_to),
            client.get_items(limit=1, status="active"),
        )

        total_active = items_data.get("paging", {}).get("total", 0)
        sales_map = _aggregate_sales_by_item(all_orders)
        paid_orders = [o for o in all_orders if o.get("status") in ("paid", "delivered")]
        total_orders = len(paid_orders)
        total_units = sum(s["units"] for s in sales_map.values())
        revenue_30d = sum(s["revenue"] for s in sales_map.values())
        avg_ticket = revenue_30d / total_orders if total_orders > 0 else 0
        products_with_sales = len(sales_map)
        products_no_sales = max(0, total_active - products_with_sales)
        unique_skus = len(set(
            _extract_base_sku(
                oi.get("item", {}).get("seller_sku") or
                oi.get("item", {}).get("seller_custom_field") or "SIN_SKU"
            )
            for o in paid_orders
            for oi in o.get("order_items", [])
        ))

        # Top 5 desde order data (titulo ya viene en order_items, 0 API calls extras)
        top_ids = sorted(sales_map.keys(), key=lambda x: sales_map[x]["units"], reverse=True)[:5]
        # Construir top con titulos de las ordenes
        title_map = {}
        for o in paid_orders:
            for oi in o.get("order_items", []):
                item = oi.get("item", {})
                iid = item.get("id", "")
                if iid and iid not in title_map:
                    title_map[iid] = {
                        "title": item.get("title", iid),
                        "thumbnail": item.get("variation_attributes", [{}])[0].get("value_name", "") if item.get("variation_attributes") else "",
                    }

        top_products = []
        for iid in top_ids:
            info = title_map.get(iid, {})
            s = sales_map[iid]
            top_products.append({
                "title": info.get("title", iid),
                "thumbnail": "",  # No gastar API call en thumbnails
                "units": s["units"],
                "revenue": s["revenue"],
            })

        daily_goal = await token_store.get_daily_goal(client.user_id)
        return templates.TemplateResponse(request, "partials/products_summary.html", {            "revenue_30d": revenue_30d,
            "total_units": total_units,
            "total_orders": total_orders,
            "total_active": total_active,
            "products_with_sales": products_with_sales,
            "products_no_sales": products_no_sales,
            "unique_skus": unique_skus,
            "avg_ticket": avg_ticket,
            "top_products": top_products,
            "daily_goal": daily_goal,
        })
    finally:
        await client.close()


@app.get("/partials/products-top-sellers", response_class=HTMLResponse)
async def products_top_sellers_partial(request: Request):
    return await products_inventory_partial(request, preset="top", enrich="full")


@app.get("/partials/products-high-stock", response_class=HTMLResponse)
async def products_high_stock_partial(request: Request):
    return await products_inventory_partial(request, preset="stock", enrich="full")


@app.get("/partials/products-low-sellers", response_class=HTMLResponse)
async def products_low_sellers_partial(request: Request):
    return await products_inventory_partial(request, preset="low")


@app.get("/partials/products-full", response_class=HTMLResponse)
async def products_full_partial(request: Request, stock_filter: str = "all"):
    return await products_inventory_partial(request, preset="full", enrich="full", full_filter=stock_filter)


@app.get("/partials/products-deals", response_class=HTMLResponse)
async def products_deals_partial(request: Request):
    """Deals: detecta deals por original_price + pricing con FX rate, margenes y ventas."""
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Items + orders en paralelo (ambos cached)
        all_bodies, all_orders = await asyncio.gather(
            _get_all_products_cached(client, include_paused=True),
            client.fetch_all_orders(date_from=date_from, date_to=date_to),
        )

        sales_map = _aggregate_sales_by_item(all_orders)
        products = _build_product_list(all_bodies, sales_map)

        # Fase 1: clasificar por original_price del body (rapido, sin API extra)
        active_deals = []
        candidates = []
        for p in products:
            op = p.get("original_price")
            if op and op > p["price"]:
                p["_has_deal"] = True
                active_deals.append(p)
            elif p["available_quantity"] > 0:
                candidates.append(p)

        # Fase 2: verificar deals perdidos via promotions API en candidatos
        # (el body batch a veces no incluye original_price aunque haya deal activo)
        await _enrich_with_promotions(client, candidates, id_key="id")
        newly_found = [p for p in candidates if p.get("_has_deal")]
        if newly_found:
            active_deals.extend(newly_found)
            candidates = [p for p in candidates if not p.get("_has_deal")]

        candidates.sort(key=lambda p: p.get("available_quantity", 0), reverse=True)
        candidates = candidates[:60]

        # BM data + FX rate + variation SKUs en paralelo
        all_to_enrich = active_deals + candidates

        bm_map, _, usd_to_mxn, _, _ = await asyncio.gather(
            _get_bm_stock_cached(all_to_enrich),
            _enrich_with_bm_product_info(all_to_enrich),
            _get_usd_to_mxn(client),
            _enrich_variation_skus(client, all_to_enrich),
            _enrich_category_names(client, all_to_enrich),
        )
        _apply_bm_stock(all_to_enrich, bm_map)
        _calc_margins(all_to_enrich, usd_to_mxn)

        # KPIs resumen
        deals_revenue = sum(p.get("revenue_30d", 0) for p in active_deals)
        deals_units = sum(p.get("units_30d", 0) for p in active_deals)

        # Recomendaciones inteligentes
        recs = []
        # 1. Deals activos con margen negativo — revisar urgente
        neg_margin = [p for p in active_deals if p.get("_margen_pct") is not None and p["_margen_pct"] < 0]
        if neg_margin:
            neg_margin.sort(key=lambda p: p["_margen_pct"])
            recs.append({
                "type": "danger",
                "icon": "!",
                "title": f"{len(neg_margin)} deal(s) con margen negativo",
                "desc": "Estos deals pierden dinero en cada venta. Revisa si conviene desactivarlos o subir el precio.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"Margen {p['_margen_pct']:.1f}%"} for p in neg_margin[:5]],
            })
        # 2. Candidatos con alto stock + cero ventas — urge mover inventario
        high_stock_no_sales = [p for p in candidates if p.get("available_quantity", 0) >= 10 and p.get("units_30d", 0) == 0]
        if high_stock_no_sales:
            high_stock_no_sales.sort(key=lambda p: p["available_quantity"], reverse=True)
            recs.append({
                "type": "warning",
                "icon": "S",
                "title": f"{len(high_stock_no_sales)} producto(s) con alto stock sin ventas",
                "desc": "Mucho inventario parado. Un deal agresivo puede activar la demanda.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"Stock: {p['available_quantity']}"} for p in high_stock_no_sales[:5]],
            })
        # 3. Candidatos con buenas ventas que podrian vender mas con deal
        good_sellers_no_deal = [p for p in candidates if p.get("units_30d", 0) >= 3 and p.get("_margen_pct") is not None and p["_margen_pct"] >= 15]
        if good_sellers_no_deal:
            good_sellers_no_deal.sort(key=lambda p: p["units_30d"], reverse=True)
            recs.append({
                "type": "success",
                "icon": "^",
                "title": f"{len(good_sellers_no_deal)} producto(s) vendiendo bien con buen margen",
                "desc": "Ya venden sin deal y tienen margen para descuento. Un deal los puede catapultar.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"{p['units_30d']} uds, margen {p['_margen_pct']:.0f}%"} for p in good_sellers_no_deal[:5]],
            })
        # 4. Stock BM disponible pero poco stock en MeLi
        bm_available = [p for p in candidates if p.get("_bm_avail") is not None and p["_bm_avail"] > 20 and p["available_quantity"] <= 5]
        if bm_available:
            bm_available.sort(key=lambda p: p["_bm_avail"], reverse=True)
            recs.append({
                "type": "info",
                "icon": "R",
                "title": f"{len(bm_available)} producto(s) con stock BM disponible alto pero poco en MeLi",
                "desc": "Reabastecer MeLi y activar deal para impulsar rotacion.",
                "products": [{"id": p["id"], "title": p["title"][:40], "detail": f"BM disp: {p['_bm_avail']}, MeLi: {p['available_quantity']}"} for p in bm_available[:5]],
            })

        # Categorias unicas para filtro
        cat_counts = {}
        for p in candidates:
            cn = p.get("category_name", "")
            if cn:
                cat_counts[cn] = cat_counts.get(cn, 0) + 1
        categories = sorted(cat_counts.keys())

        return templates.TemplateResponse(request, "partials/products_deals.html", {            "active_deals": active_deals,
            "candidates": candidates,
            "usd_to_mxn": round(usd_to_mxn, 2),
            "deals_revenue": deals_revenue,
            "deals_units": deals_units,
            "recommendations": recs,
            "categories": categories,
            "cat_counts": cat_counts,
            "total_no_deal_no_sales": len([p for p in candidates if p.get("units_30d", 0) == 0]),
        })
    finally:
        await client.close()


@app.get("/partials/products-not-published", response_class=HTMLResponse)
async def products_not_published_partial(request: Request):
    """SKUs en BM sin listing en MeLi. Usa cache de items."""
    import httpx
    from datetime import datetime, timedelta
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        now = datetime.utcnow()
        date_from = (now - timedelta(days=60)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        # Items (cached) + ordenes (cached) en paralelo
        all_bodies, all_orders = await asyncio.gather(
            _get_all_products_cached(client, include_paused=True),
            client.fetch_all_orders(date_from=date_from, date_to=date_to),
        )

        # Recopilar SKUs conocidos
        known_skus = set()
        for body in all_bodies:
            sku = _get_item_sku(body)
            if sku:
                known_skus.add(_extract_base_sku(sku).upper())
        for order in all_orders:
            for oi in order.get("order_items", []):
                item = oi.get("item", {})
                raw_sku = item.get("seller_sku") or item.get("seller_custom_field") or ""
                if raw_sku:
                    known_skus.add(_extract_base_sku(raw_sku).upper())

        if not known_skus:
            return HTMLResponse('<p class="text-center py-8 text-gray-500">No se encontraron SKUs para comparar</p>')

        BM_INV_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
        sem = asyncio.Semaphore(15)

        # Fase 1: BM InventoryReport + exchange rate en paralelo
        async def _fetch_bm_inv(base_sku, http):
            async with sem:
                try:
                    resp = await http.post(BM_INV_URL, json={
                        "COMPANYID": 1,
                        "SEARCH": base_sku,
                        "CONCEPTID": 8,
                        "NUMBERPAGE": 1,
                        "RECORDSPAGE": 10,
                    }, headers={"Content-Type": "application/json"}, timeout=10.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data and isinstance(data, list) and data:
                            for item in data:
                                if item.get("SKU", "").upper() == base_sku.upper():
                                    return base_sku, item
                            return base_sku, data[0]
                except Exception:
                    pass
                return base_sku, None

        async with httpx.AsyncClient() as http:
            inv_tasks = [_fetch_bm_inv(sku, http) for sku in list(known_skus)[:100]]
            inv_results = await asyncio.gather(*inv_tasks, return_exceptions=True)

        bm_products = {}
        for r in inv_results:
            if isinstance(r, Exception) or r is None:
                continue
            base_sku, data = r
            if data:
                bm_products[base_sku] = data

        usd_to_mxn = await _get_usd_to_mxn(client)

        # Fase 2: BM Warehouse endpoint (stock real por almacen)
        BM_WH_URL2 = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"

        async def _check_base_wh(base_sku, http):
            """Consulta Warehouse endpoint para obtener stock real."""
            async with sem:
                try:
                    resp = await http.post(BM_WH_URL2, json={
                        "COMPANYID": 1, "SKU": base_sku, "WarehouseID": None,
                        "LocationID": "47,62,68", "BINID": None,
                        "Condition": _bm_conditions_for_sku(base_sku), "ForInventory": 0, "SUPPLIERS": None,
                    }, timeout=10.0)
                    if resp.status_code == 200:
                        rows = resp.json() or []
                        mty = cdmx = tj = 0
                        for row in rows:
                            qty = row.get("QtyTotal", 0) or 0
                            wname = (row.get("WarehouseName") or "").lower()
                            if "monterrey" in wname or "maxx" in wname:
                                mty += qty
                            elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                                cdmx += qty
                            else:
                                tj += qty
                        total = mty + cdmx
                        if total > 0:
                            return base_sku, {"mty": mty, "cdmx": cdmx, "tj": tj, "total": total}
                except Exception:
                    pass
                return base_sku, None

        async with httpx.AsyncClient() as http:
            ff_tasks = [_check_base_wh(sku, http) for sku in bm_products.keys()]
            ff_results = await asyncio.gather(*ff_tasks, return_exceptions=True)

        skus_with_stock = {}
        for r in ff_results:
            if isinstance(r, Exception) or r is None:
                continue
            base_sku, stock_data = r
            if stock_data:
                skus_with_stock[base_sku.upper()] = stock_data

        # Fase 3: Verificar cuales NO estan en MeLi
        # Buscar por SKU completo (con sufijo) y tambien por base SKU
        async def _check_meli(sku):
            async with sem:
                try:
                    result = await client.get(
                        f"/users/{client.user_id}/items/search",
                        params={"seller_sku": sku, "limit": 1}
                    )
                    if result.get("results"):
                        return sku, True
                    # Fallback: buscar por base SKU (sin sufijo)
                    base = _extract_base_sku(sku)
                    if base != sku:
                        result2 = await client.get(
                            f"/users/{client.user_id}/items/search",
                            params={"seller_sku": base, "limit": 1}
                        )
                        if result2.get("results"):
                            return sku, True
                    return sku, False
                except Exception:
                    return sku, False

        meli_checks = await asyncio.gather(
            *[_check_meli(sku) for sku in list(skus_with_stock.keys())[:100]],
            return_exceptions=True
        )

        meli_published = set()
        for r in meli_checks:
            if isinstance(r, Exception) or r is None:
                continue
            sku, found = r
            if found:
                meli_published.add(sku)

        not_published = []
        for sku_upper, stock_data in skus_with_stock.items():
            if sku_upper in meli_published:
                continue
            base = _extract_base_sku(sku_upper)
            bm_info = bm_products.get(base, {})
            retail_price = bm_info.get("RetailPrice", 0) or 0
            avg_cost = bm_info.get("AvgCostQTY", 0) or 0
            if retail_price > 0 and retail_price < 9999:
                estimated_price = round(retail_price * usd_to_mxn * 1.16, 0)
            elif avg_cost > 0 and avg_cost < 9999:
                estimated_price = round(avg_cost * usd_to_mxn * 2 * 1.16, 0)
            else:
                estimated_price = 0
            not_published.append({
                "sku": sku_upper,
                "base_sku": base,
                "title": bm_info.get("Title", ""),
                "brand": bm_info.get("Brand", ""),
                "retail_price_usd": retail_price,
                "avg_cost_usd": avg_cost,
                "estimated_price_mxn": estimated_price,
                "mty": stock_data["mty"],
                "cdmx": stock_data["cdmx"],
                "tj": stock_data["tj"],
                "total_stock": stock_data["total"],
            })

        not_published.sort(key=lambda x: x["total_stock"], reverse=True)

        return templates.TemplateResponse(request, "partials/products_not_published.html", {            "products": not_published,
            "usd_to_mxn": usd_to_mxn,
        })
    finally:
        await client.close()


# ---------- Health helpers ----------

_MELI_THRESHOLDS = {
    "claims":        {"green": 0.02, "yellow": 0.04, "red": 0.07},
    "cancellations": {"green": 0.025, "yellow": 0.05, "red": 0.09},
    "delays":        {"green": 0.15, "yellow": 0.20, "red": 0.30},
}


def _metric_status(rate: float, key: str) -> str:
    t = _MELI_THRESHOLDS[key]
    if rate < t["green"]:
        return "green"
    elif rate < t["yellow"]:
        return "yellow"
    elif rate < t["red"]:
        return "orange"
    return "red"


_STATUS_LABELS = {"green": "Excelente", "yellow": "Atencion", "orange": "Riesgo", "red": "Critico"}


def _compute_health_score(claims_rate: float, cancel_rate: float, delay_rate: float,
                           open_claims: int, unanswered_q: int) -> int:
    """Compute a composite health score 0-100.
    Weights: claims 30%, cancellations 20%, delays 20%, open_claims 15%, questions 15%.
    Each sub-score is 100 when perfect and 0 when at/above red threshold."""
    t = _MELI_THRESHOLDS
    def _rate_score(rate, key):
        red = t[key]["red"]
        if rate <= 0:
            return 100
        if rate >= red:
            return 0
        return max(0, round((1 - rate / red) * 100))

    s_claims = _rate_score(claims_rate, "claims")
    s_cancel = _rate_score(cancel_rate, "cancellations")
    s_delays = _rate_score(delay_rate, "delays")
    # Open claims: 0 = 100, 5+ = 0
    s_open = max(0, round((1 - min(open_claims, 5) / 5) * 100))
    # Unanswered questions: 0 = 100, 10+ = 0
    s_unans = max(0, round((1 - min(unanswered_q, 10) / 10) * 100))

    score = round(s_claims * 0.30 + s_cancel * 0.20 + s_delays * 0.20 + s_open * 0.15 + s_unans * 0.15)
    return max(0, min(100, score))


def _classify_question(text: str) -> str:
    """Classify a question by type based on keywords."""
    t = (text or "").lower()
    if any(w in t for w in ["envio", "envío", "llega", "demora", "entrega", "shipping", "despacho", "tarda"]):
        return "envio"
    if any(w in t for w in ["stock", "disponible", "queda", "hay", "tienen", "unidades"]):
        return "stock"
    if any(w in t for w in ["compatible", "sirve para", "funciona con", "modelo", "medida", "tamaño", "talla"]):
        return "compatibilidad"
    if any(w in t for w in ["precio", "descuento", "oferta", "costo", "vale", "barato", "rebaja", "promocion"]):
        return "precio"
    if any(w in t for w in ["garantia", "garantía", "devolucion", "devolución", "cambio"]):
        return "garantia"
    if any(w in t for w in ["factura", "fiscal", "iva", "cfdi", "boleta"]):
        return "factura"
    return "general"


_QUESTION_TYPE_LABELS = {
    "envio": {"label": "Envio", "color": "bg-blue-100 text-blue-700"},
    "stock": {"label": "Stock", "color": "bg-green-100 text-green-700"},
    "compatibilidad": {"label": "Compat.", "color": "bg-purple-100 text-purple-700"},
    "precio": {"label": "Precio", "color": "bg-yellow-100 text-yellow-700"},
    "garantia": {"label": "Garantia", "color": "bg-orange-100 text-orange-700"},
    "factura": {"label": "Factura", "color": "bg-gray-100 text-gray-700"},
    "general": {"label": "General", "color": "bg-gray-100 text-gray-600"},
}

_QUESTION_TEMPLATES = {
    "envio": [
        "El envio se realiza por Mercado Envios. Una vez despachado, recibiras el numero de seguimiento para rastrear tu paquete.",
        "Los tiempos de entrega dependen de tu ubicacion. Puedes ver la fecha estimada antes de comprar.",
    ],
    "stock": [
        "Si, tenemos stock disponible. Puedes comprarlo directamente.",
        "Por el momento no tenemos stock. Te recomiendo agregar a favoritos para que te notifique cuando este disponible.",
    ],
    "compatibilidad": [
        "Este producto es compatible con los modelos indicados en la descripcion. Revisa la ficha tecnica para confirmar.",
        "Verificamos que es compatible. Puedes comprarlo con confianza.",
    ],
    "precio": [
        "El precio publicado es el precio final. Incluye envio gratis si tu compra supera el monto minimo.",
        "Por el momento no manejamos descuentos adicionales, pero el precio ya es competitivo.",
    ],
    "garantia": [
        "El producto cuenta con garantia del vendedor. Si tienes algun problema, puedes iniciar un reclamo desde tu compra.",
        "Ofrecemos devolucion gratis dentro de los 30 dias de recibido el producto.",
    ],
    "factura": [
        "Emitimos factura. Una vez realizada la compra, solicita la factura por mensaje y te la enviamos.",
        "La factura se genera automaticamente y la puedes descargar desde tu compra en MercadoLibre.",
    ],
    "general": [
        "Gracias por tu pregunta. Quedamos a tu disposicion para cualquier duda adicional.",
    ],
}


def _compute_metric_margin(rate: float, key: str) -> dict:
    """Compute how far the current rate is from each threshold and remaining margin."""
    t = _MELI_THRESHOLDS[key]
    status = _metric_status(rate, key)
    # How many percentage points until next worse threshold
    if rate < t["green"]:
        next_threshold = t["green"]
        margin_pct = round((next_threshold - rate) * 100, 2)
        margin_label = f"{margin_pct}pp antes de amarillo"
    elif rate < t["yellow"]:
        next_threshold = t["yellow"]
        margin_pct = round((next_threshold - rate) * 100, 2)
        margin_label = f"{margin_pct}pp antes de naranja"
    elif rate < t["red"]:
        next_threshold = t["red"]
        margin_pct = round((next_threshold - rate) * 100, 2)
        margin_label = f"{margin_pct}pp antes de rojo"
    else:
        margin_pct = 0
        margin_label = "En zona critica"
    # Position as percentage of the gauge (0-100 scale where red threshold = ~90%)
    max_val = t["red"] * 1.2  # give some space beyond red
    gauge_position = min(100, round((rate / max_val) * 100)) if max_val > 0 else 0
    return {
        "status": status,
        "label": _STATUS_LABELS[status],
        "margin_pct": margin_pct,
        "margin_label": margin_label,
        "gauge_position": gauge_position,
        "green_end": round((t["green"] / max_val) * 100),
        "yellow_end": round((t["yellow"] / max_val) * 100),
        "red_start": round((t["red"] / max_val) * 100),
    }


def _elapsed_str(iso_date: str) -> tuple:
    """Return (human string, total seconds) from an ISO date to now(UTC)."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            secs = 0
        if secs < 60:
            return (f"hace {secs}s", secs)
        mins = secs // 60
        if mins < 60:
            return (f"hace {mins}m", secs)
        hours = mins // 60
        if hours < 24:
            return (f"hace {hours}h", secs)
        days = hours // 24
        return (f"hace {days}d", secs)
    except Exception:
        return ("-", 0)


@app.get("/partials/health-summary", response_class=HTMLResponse)
async def health_summary_partial(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        errors = []
        df = date_from or None
        dt = date_to or None

        # Parallel fetch: user info + counts
        async def _fetch_questions():
            # Sin filtro de fecha: una pregunta sin responder requiere atencion sin importar cuando fue hecha
            try:
                r = await client.get_questions(status="UNANSWERED", limit=1)
                # MeLi Questions API devuelve 'total' en raiz, no en 'paging'
                return r.get("total", r.get("paging", {}).get("total", 0))
            except Exception:
                return 0

        async def _fetch_claims():
            # Reintentar una vez si falla (API intermitente)
            for attempt in range(2):
                try:
                    r = await client.get_claims(limit=1, status="opened")
                    return r.get("paging", {}).get("total", 0)
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            return 0

        async def _fetch_messages():
            # Cuenta conversaciones con mensajes en ordenes recientes
            try:
                r = await client.get_messages(limit=0)
                return r.get("paging", {}).get("total", 0)
            except Exception:
                return 0

        user_task = asyncio.ensure_future(client.get_user_info())
        q_task = asyncio.ensure_future(_fetch_questions())
        c_task = asyncio.ensure_future(_fetch_claims())
        m_task = asyncio.ensure_future(_fetch_messages())

        user, unanswered_questions, open_claims, unread_messages = await asyncio.gather(
            user_task, q_task, c_task, m_task
        )

        reputation = user.get("seller_reputation", {})
        metrics = reputation.get("metrics", {})

        claims_rate = metrics.get("claims", {}).get("rate", 0) or 0
        cancel_rate = metrics.get("cancellations", {}).get("rate", 0) or 0
        delay_rate = metrics.get("delayed_handling_time", {}).get("rate", 0) or 0

        claims_status = _metric_status(claims_rate, "claims")
        cancel_status = _metric_status(cancel_rate, "cancellations")
        delay_status = _metric_status(delay_rate, "delays")

        claims_value = metrics.get("claims", {}).get("value", 0) or 0
        cancel_value = metrics.get("cancellations", {}).get("value", 0) or 0
        delay_value = metrics.get("delayed_handling_time", {}).get("value", 0) or 0

        urgent_count = open_claims + unanswered_questions

        health_score = _compute_health_score(claims_rate, cancel_rate, delay_rate,
                                              open_claims, unanswered_questions)

        sales_period = metrics.get("claims", {}).get("period", "60 days")
        claims_margin = _compute_metric_margin(claims_rate, "claims")
        cancel_margin = _compute_metric_margin(cancel_rate, "cancellations")
        delay_margin = _compute_metric_margin(delay_rate, "delays")

        summary = SimpleNamespace(
            reputation_level=reputation.get("level_id", "unknown"),
            power_seller_status=reputation.get("power_seller_status", None),
            open_claims=open_claims,
            unanswered_questions=unanswered_questions,
            unread_messages=unread_messages,
            urgent_count=urgent_count,
            health_score=health_score,
            sales_period=sales_period,
            claims_rate=claims_rate,
            claims_pct=round(claims_rate * 100, 2),
            claims_status=claims_status,
            claims_label=_STATUS_LABELS[claims_status],
            claims_value=claims_value,
            claims_margin=claims_margin,
            cancellation_rate=cancel_rate,
            cancellation_pct=round(cancel_rate * 100, 2),
            cancellation_status=cancel_status,
            cancellation_label=_STATUS_LABELS[cancel_status],
            cancellation_value=cancel_value,
            cancel_margin=cancel_margin,
            delayed_rate=delay_rate,
            delayed_pct=round(delay_rate * 100, 2),
            delayed_status=delay_status,
            delayed_label=_STATUS_LABELS[delay_status],
            delayed_value=delay_value,
            delay_margin=delay_margin,
        )

        return templates.TemplateResponse(request, "partials/health_summary.html", {            "summary": summary,
            "thresholds": _MELI_THRESHOLDS,
            "errors": errors,
            "date_from": date_from,
            "date_to": date_to,
        })
    finally:
        await client.close()


@app.get("/partials/health-claims", response_class=HTMLResponse)
async def health_claims_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query(""),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    order_id: str = Query("", description="Filter by order/resource ID"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        filter_order_id = order_id.strip() if order_id else ""
        params_status = status if status else None
        df = date_from or None
        dt = date_to or None

        if filter_order_id:
            # Fetch all claims WITHOUT date filter — the claim may predate the active range
            try:
                all_claims = await client.fetch_all_claims(status=params_status,
                                                            date_from=None, date_to=None)
                raw_claims = [c for c in all_claims
                              if str(c.get("resource_id", "")) == filter_order_id]
            except Exception:
                raw_claims = []
            paging = {"total": len(raw_claims), "offset": 0, "limit": len(raw_claims) or limit}
            # Apply offset/limit manually
            raw_claims = raw_claims[offset:offset + limit]
        else:
            try:
                data = await client.get_claims(offset=offset, limit=limit, status=params_status,
                                               date_from=df, date_to=dt)
            except Exception:
                data = {"results": [], "paging": {"total": 0, "offset": 0, "limit": limit}}
            raw_claims = data.get("results", [])
            paging = data.get("paging", {"total": len(raw_claims), "offset": offset, "limit": limit})

        # --- Refresh status of "opened" claims via individual endpoint ---
        # The search API can return stale status; the detail endpoint is authoritative
        opened_ids = [c for c in raw_claims if c.get("status") == "opened"]
        if opened_ids:
            sem_refresh = asyncio.Semaphore(5)

            async def _refresh_status(claim):
                async with sem_refresh:
                    try:
                        detail = await client.get_claim_detail(str(claim.get("id", "")))
                        if isinstance(detail, dict) and detail.get("status"):
                            claim["status"] = detail["status"]
                            if detail.get("stage"):
                                claim["stage"] = detail["stage"]
                            # Also refresh players (for due_date / actions)
                            if detail.get("players"):
                                claim["players"] = detail["players"]
                    except Exception:
                        pass  # Keep original status if detail fetch fails

            # Only refresh the most recent 10 opened claims (the ones most likely
            # to have a tight deadline). Older claims rarely change status and
            # the 30-call cap was adding unnecessary latency.
            await asyncio.gather(*[_refresh_status(c) for c in opened_ids[:10]],
                                 return_exceptions=True)

        # Reason code mapping (PDD = producto defectuoso, PNR = no recibido)
        REASON_MAP = {
            "PNR": ("No recibido", "not_received"),
            "PDD": ("Producto defectuoso/diferente", "defective"),
        }

        # Batch fetch order info for product titles
        order_ids = list({str(c.get("resource_id", "")) for c in raw_claims
                          if c.get("resource") == "order" and c.get("resource_id")})
        orders_map = {}
        if order_ids:
            try:
                for oid in order_ids[:20]:
                    try:
                        order = await client.get(f"/orders/{oid}")
                        oi = order.get("order_items", [])
                        if oi:
                            item = oi[0].get("item", {})
                            orders_map[oid] = {
                                "title": item.get("title", ""),
                                "price": order.get("total_amount", 0),
                                "item_id": item.get("id", ""),
                                "seller_sku": item.get("seller_sku") or item.get("seller_custom_field") or "",
                            }
                    except Exception:
                        pass
            except Exception:
                pass

        # Parallel fetch: claim messages + shipment tracking for opened claims
        opened_claims = [c for c in raw_claims if c.get("status") == "opened"]
        claim_messages_map = {}  # claim_id -> list of messages
        shipment_tracking_map = {}  # order_id -> tracking info

        if opened_claims:
            sem = asyncio.Semaphore(5)

            async def _fetch_claim_msgs(claim_id):
                async with sem:
                    try:
                        msgs = await client.get_claim_messages(str(claim_id))
                        if isinstance(msgs, list):
                            return str(claim_id), msgs
                        return str(claim_id), msgs.get("results", msgs.get("messages", []))
                    except Exception:
                        return str(claim_id), []

            async def _fetch_shipment_tracking(order_id):
                async with sem:
                    try:
                        order = await client.get(f"/orders/{order_id}")
                        ship_id = order.get("shipping", {}).get("id")
                        if ship_id:
                            ship = await client.get_shipment(str(ship_id))
                            return str(order_id), {
                                "status": ship.get("status", ""),
                                "substatus": ship.get("substatus", ""),
                                "tracking_number": ship.get("tracking_number", ""),
                                "tracking_url": ship.get("tracking_url", ""),
                                "carrier": ship.get("logistic_type", ""),
                            }
                    except Exception:
                        pass
                    return str(order_id), {}

            # Fetch messages for up to 20 opened claims
            msg_tasks = [_fetch_claim_msgs(c.get("id", "")) for c in opened_claims[:20]]
            # Fetch tracking for PNR (not received) claims
            pnr_claims = [c for c in opened_claims
                          if (c.get("reason_id", "")[:3] == "PNR") and c.get("resource_id")]
            track_tasks = [_fetch_shipment_tracking(str(c.get("resource_id", ""))) for c in pnr_claims[:20]]

            all_results = await asyncio.gather(*msg_tasks, *track_tasks, return_exceptions=True)
            for r in all_results[:len(msg_tasks)]:
                if isinstance(r, tuple):
                    claim_messages_map[r[0]] = r[1]
            for r in all_results[len(msg_tasks):]:
                if isinstance(r, tuple):
                    shipment_tracking_map[r[0]] = r[1]

        enriched = []
        for c in raw_claims:
            date_created = c.get("date_created", "")
            elapsed_str, elapsed_secs = _elapsed_str(date_created)
            days_open = elapsed_secs // 86400 if elapsed_secs else 0

            c_status = c.get("status", "")
            stage = c.get("stage", "")

            # Due date for mandatory action (compute first for urgency)
            due_date_raw = ""
            due_date = ""
            for player in c.get("players", []):
                if player.get("role") == "respondent":
                    for a in player.get("available_actions", []):
                        if a.get("mandatory") and a.get("due_date"):
                            due_date_raw = a["due_date"]
                            due_date = due_date_raw[:10]
                            break
                    break

            # Compute countdown hours until due_date
            countdown_hours = None
            if due_date_raw and c_status == "opened":
                from datetime import datetime, timezone
                try:
                    due_dt = datetime.fromisoformat(due_date_raw.replace("Z", "+00:00"))
                    remaining = due_dt - datetime.now(timezone.utc)
                    countdown_hours = max(0, round(remaining.total_seconds() / 3600, 1))
                except Exception:
                    pass

            # Urgency based on countdown (more precise than days_open)
            if c_status == "opened":
                if countdown_hours is not None:
                    if countdown_hours < 8:
                        urgency = "red"
                    elif countdown_hours < 24:
                        urgency = "yellow"
                    else:
                        urgency = "green"
                else:
                    if days_open > 7:
                        urgency = "red"
                    elif days_open > 3:
                        urgency = "yellow"
                    else:
                        urgency = "green"
            else:
                urgency = "gray"

            reason_id = c.get("reason_id", "")
            reason_prefix = reason_id[:3] if reason_id else ""
            reason_info = REASON_MAP.get(reason_prefix, ("Reclamo", "other"))
            reason_desc = reason_info[0]
            reason_type = reason_info[1]

            # Get seller available actions
            seller_actions = []
            for player in c.get("players", []):
                if player.get("role") == "respondent":
                    seller_actions = [a.get("action", "") for a in player.get("available_actions", [])]
                    break

            issues = []
            suggestions = []

            if reason_type == "not_received":
                issues.append("Comprador reporta no haber recibido el producto")
                suggestions.append("Verificar tracking del envio y confirmar entrega con la paqueteria")
                suggestions.append("Contactar al comprador para confirmar direccion de entrega")
            elif reason_type == "defective":
                issues.append("Comprador reporta producto defectuoso o diferente")
                suggestions.append("Solicitar fotos del defecto al comprador")
                suggestions.append("Ofrecer reemplazo o devolucion inmediata")
            else:
                issues.append(f"Reclamo: {reason_id}")
                suggestions.append("Contactar al comprador para entender el problema")

            if days_open > 7:
                issues.append(f"Reclamo abierto hace {days_open} dias - URGENTE")
                suggestions.append("Resolver HOY: reclamos abiertos > 7 dias impactan fuerte la reputacion")
            elif days_open > 3 and c_status == "opened":
                issues.append(f"Reclamo abierto hace {days_open} dias")
                suggestions.append("Resolver pronto para evitar impacto negativo en metricas")

            if stage == "dispute":
                issues.append("En disputa con mediacion de MeLi")
                suggestions.append("Responder al mediador con evidencia clara")

            resource_id = str(c.get("resource_id", ""))
            order_info = orders_map.get(resource_id, {})

            # Conversation messages
            claim_id_str = str(c.get("id", ""))
            raw_msgs = claim_messages_map.get(claim_id_str, [])
            conversation = []
            for msg in raw_msgs[-10:]:
                sender = msg.get("sender_role", msg.get("role", ""))
                text = msg.get("text", msg.get("message", ""))
                msg_date = msg.get("date_created", "")
                conversation.append({
                    "sender": sender,
                    "text": text,
                    "date": msg_date[:16].replace("T", " ") if msg_date else "",
                })

            # Tracking info for PNR claims
            tracking = shipment_tracking_map.get(resource_id, {})

            enriched.append(SimpleNamespace(
                id=c.get("id", ""),
                order_id=resource_id,
                status=c_status,
                stage=stage,
                date_created=date_created[:10] if date_created else "-",
                _sort_date=date_created or "",
                elapsed=elapsed_str,
                days_open=days_open,
                urgency=urgency,
                countdown_hours=countdown_hours,
                reason_desc=reason_desc,
                reason_id=reason_id,
                reason_type=reason_type,
                product_title=order_info.get("title", ""),
                product_price=order_info.get("price", 0),
                product_sku=order_info.get("seller_sku", ""),
                seller_actions=seller_actions,
                due_date=due_date,
                issues=issues,
                suggestions=suggestions,
                conversation=conversation,
                tracking=tracking,
            ))

        # Sort: urgency first (red > yellow > green > gray), then by date
        _urgency_order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
        enriched.sort(key=lambda c: (_urgency_order.get(c.urgency, 3), not c._sort_date, c._sort_date), reverse=False)
        # Reverse date within same urgency (newest first) — already handled by tuple sort

        return templates.TemplateResponse(request, "partials/health_claims.html", {            "claims": enriched,
            "paging": paging,
            "offset": offset,
            "limit": limit,
            "status": status,
            "filter_order_id": filter_order_id,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando reclamos: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-questions", response_class=HTMLResponse)
async def health_questions_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query("UNANSWERED"),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        # MeLi Questions API does not support date_from/date_to — ignore them
        try:
            data = await client.get_questions(status=status, offset=offset, limit=limit,
                                              date_from=date_from or None, date_to=date_to or None)
        except Exception:
            # Retry without dates (MeLi rejects date params on questions)
            data = await client.get_questions(status=status, offset=offset, limit=limit)
        raw_questions = data.get("questions", data.get("results", []))
        # MeLi Questions API devuelve 'total' en raiz, no dentro de 'paging'
        paging = data.get("paging") or {
            "total": data.get("total", len(raw_questions)),
            "offset": offset,
            "limit": limit,
        }

        # Batch fetch product info for all unique item_ids
        item_ids = list({q.get("item_id") for q in raw_questions if q.get("item_id")})
        items_map = {}
        if item_ids:
            try:
                # Fetch in batches of 20
                for i in range(0, len(item_ids), 20):
                    batch = item_ids[i:i+20]
                    details = await client.get_items_details(batch)
                    for d in details:
                        body = d.get("body", d) if isinstance(d, dict) else {}
                        iid = body.get("id", "")
                        if iid:
                            items_map[iid] = {
                                "title": body.get("title", ""),
                                "thumbnail": body.get("thumbnail", body.get("secure_thumbnail", "")),
                                "price": body.get("price", 0),
                                "stock": body.get("available_quantity", 0),
                                "seller_sku": body.get("seller_custom_field") or "",
                                "permalink": body.get("permalink", ""),
                                "attributes": _extract_key_attributes(body),
                            }
            except Exception:
                pass

        # Populate seller catalog cache (for cross-sell related listings)
        _uid_str = str(client.user_id)
        # Merge current items_map into catalog (quick route)
        if _uid_str not in _seller_catalog:
            _seller_catalog[_uid_str] = {}
        for _iid, _iprod in items_map.items():
            if _iprod.get("permalink"):
                _seller_catalog[_uid_str][_iid] = {
                    "title": _iprod.get("title", ""),
                    "permalink": _iprod.get("permalink", ""),
                    "price": _iprod.get("price", 0),
                }
        # Kick off background refresh if stale or too small
        _cat_age = _time.time() - _seller_catalog_ts.get(_uid_str, 0)
        _cat_size = len(_seller_catalog.get(_uid_str, {}))
        if (_cat_age > _CATALOG_CACHE_TTL or _cat_size < 10) and _uid_str not in _seller_catalog_refresh_tasks:
            _token_row = await token_store.get_tokens(_uid_str)
            _access_token = (_token_row.get("access_token") or "") if _token_row else ""
            _refresh_token = (_token_row.get("refresh_token") or "") if _token_row else ""
            if _access_token:
                _seller_catalog_refresh_tasks.add(_uid_str)
                asyncio.create_task(_refresh_seller_catalog(_uid_str, _access_token, _refresh_token))

        # Fetch buyer history for UNANSWERED questions only
        buyer_history_map = {}  # buyer_id -> list of past questions
        if status == "UNANSWERED":
            unanswered_buyer_ids = list({
                str(q.get("from", {}).get("id", ""))
                for q in raw_questions
                if q.get("status", "UNANSWERED") == "UNANSWERED" and q.get("from", {}).get("id")
            })
            if unanswered_buyer_ids:
                sem_bh = asyncio.Semaphore(5)
                async def _fetch_buyer_hist(bid):
                    async with sem_bh:
                        return bid, await client.get_buyer_questions(bid)
                bh_tasks = [_fetch_buyer_hist(bid) for bid in unanswered_buyer_ids]
                bh_results = await asyncio.gather(*bh_tasks, return_exceptions=True)
                for r in bh_results:
                    if isinstance(r, tuple):
                        buyer_history_map[r[0]] = r[1]

        enriched = []
        for q in raw_questions:
            date_created = q.get("date_created", "")
            elapsed_str, elapsed_secs = _elapsed_str(date_created)

            q_status = q.get("status", "UNANSWERED")
            if q_status == "UNANSWERED":
                hours = elapsed_secs / 3600
                if hours > 12:
                    urgency = "red"
                elif hours > 1:
                    urgency = "yellow"
                else:
                    urgency = "green"
            else:
                urgency = "gray"

            item_id = q.get("item_id", "")
            prod = items_map.get(item_id, {})

            answer_data = q.get("answer")
            answer = None
            if answer_data:
                answer = SimpleNamespace(
                    text=answer_data.get("text", "-"),
                    date_created=answer_data.get("date_created", ""),
                )

            # Buyer history
            buyer_id = str(q.get("from", {}).get("id", ""))
            current_qid = q.get("id", "")
            raw_history = buyer_history_map.get(buyer_id, [])
            # Exclude current question from history
            buyer_history = []
            for hq in raw_history:
                if hq.get("id") == current_qid:
                    continue
                ans = hq.get("answer")
                buyer_history.append({
                    "qid": hq.get("id", ""),
                    "text": hq.get("text", ""),
                    "status": hq.get("status", ""),
                    "date_created": (hq.get("date_created", "") or "")[:10],
                    "item_id": hq.get("item_id", ""),
                    "answer_text": ans.get("text", "") if ans else "",
                })

            # Pre-serialized JSON for data attribute (max 5, truncated text)
            bh_for_json = []
            for entry in buyer_history[:5]:
                bh_for_json.append({
                    "text": entry["text"][:150],
                    "status": entry["status"],
                    "date": entry["date_created"],
                    "item_id": entry["item_id"],
                    "answer": entry["answer_text"][:150] if entry["answer_text"] else "",
                })

            # Same-item history: all prior Q&A on this exact listing (answered or not)
            same_item_history = [
                e for e in buyer_history
                if e.get("item_id") == item_id
            ][:5]
            sih_for_json = []
            for e in same_item_history:
                sih_for_json.append({
                    "text": e["text"][:200],
                    "date": e["date_created"],
                    "answer": e["answer_text"][:200] if e["answer_text"] else "",
                })

            # Related listings: search full seller catalog (not just current page items)
            _q_words = set((q.get("text", "") or "").lower().split())
            _stop = {"de", "la", "el", "en", "es", "un", "lo", "me", "si", "se", "a", "y", "o", "que", "pero", "favor", "link"}
            _q_keywords = _q_words - _stop
            related_listings = []
            _catalog_to_search = {**_seller_catalog.get(_uid_str, {}), **{
                k: {"title": v.get("title",""), "permalink": v.get("permalink",""), "price": v.get("price",0)}
                for k, v in items_map.items() if v.get("permalink")
            }}
            if _q_keywords and _catalog_to_search:
                _scored = []
                for _iid, _iprod in _catalog_to_search.items():
                    if str(_iid) == str(item_id):
                        continue
                    _t_words = set((_iprod.get("title", "") or "").lower().split())
                    _score = len(_q_keywords & _t_words)
                    if _score > 0 and _iprod.get("permalink"):
                        _scored.append((_score, _iid, _iprod))
                _scored.sort(key=lambda x: -x[0])
                for _, _, _iprod in _scored[:3]:
                    related_listings.append({
                        "title": _iprod.get("title", ""),
                        "permalink": _iprod.get("permalink", ""),
                        "price": _iprod.get("price", 0),
                    })

            # Attributes JSON for AI (top 20 specs)
            _prod_attrs = prod.get("attributes", [])
            _attrs_json = json.dumps(_prod_attrs, ensure_ascii=False) if _prod_attrs else "[]"

            # Classify question type
            q_text = q.get("text", "")
            q_type = _classify_question(q_text)
            q_type_info = _QUESTION_TYPE_LABELS.get(q_type, _QUESTION_TYPE_LABELS["general"])

            # Get quick templates for this type
            quick_templates = _QUESTION_TEMPLATES.get(q_type, _QUESTION_TEMPLATES["general"])

            enriched.append(SimpleNamespace(
                id=q.get("id", ""),
                text=q_text or "-",
                status=q_status,
                date_created=date_created[:10] if date_created else "-",
                _sort_date=date_created or "",
                elapsed=elapsed_str,
                urgency=urgency,
                item_id=item_id,
                product_title=prod.get("title", ""),
                product_thumbnail=prod.get("thumbnail", ""),
                product_price=prod.get("price", 0),
                product_stock=prod.get("stock", 0),
                product_sku=prod.get("seller_sku", ""),
                product_permalink=prod.get("permalink", ""),
                product_attributes=_prod_attrs,
                product_attributes_json=_attrs_json,
                same_item_history=same_item_history,
                same_item_history_json=json.dumps(sih_for_json, ensure_ascii=False) if sih_for_json else "[]",
                related_listings=related_listings,
                related_listings_json=json.dumps(related_listings, ensure_ascii=False) if related_listings else "[]",
                answer=answer,
                buyer_id=buyer_id,
                buyer_history=buyer_history,
                buyer_question_count=len(buyer_history),
                buyer_history_json=json.dumps(bh_for_json, ensure_ascii=False) if bh_for_json else "[]",
                q_type=q_type,
                q_type_label=q_type_info["label"],
                q_type_color=q_type_info["color"],
                quick_templates=quick_templates,
            ))

        # Sort: urgency-first for UNANSWERED (red > yellow > green > gray), then newest
        _urgency_order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
        enriched.sort(key=lambda q: (_urgency_order.get(q.urgency, 3), q._sort_date), reverse=False)
        # Within same urgency, newest first — fix by making date descending
        enriched.sort(key=lambda q: (_urgency_order.get(q.urgency, 3), ""), reverse=False)
        # Stable sort by urgency, then reverse date within group
        from itertools import groupby
        sorted_enriched = []
        enriched_by_urg = sorted(enriched, key=lambda q: _urgency_order.get(q.urgency, 3))
        for _, group in groupby(enriched_by_urg, key=lambda q: q.urgency):
            grp = sorted(list(group), key=lambda q: q._sort_date, reverse=True)
            sorted_enriched.extend(grp)
        enriched = sorted_enriched

        return templates.TemplateResponse(request, "partials/health_questions.html", {            "questions": enriched,
            "paging": paging,
            "offset": offset,
            "limit": limit,
            "status": status,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando preguntas: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-search", response_class=HTMLResponse)
async def health_search_partial(
    request: Request,
    q: str = Query("", description="Search query"),
):
    """Global search: order ID, claim ID, or keyword search."""
    query = (q or "").strip()
    if not query:
        return HTMLResponse('<p class="text-center py-4 text-gray-400 text-sm">Ingresa un termino de busqueda</p>')

    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        results = []
        result_type = "unknown"

        # Detect query type
        is_numeric = query.replace("-", "").isdigit()
        is_long_number = is_numeric and len(query.replace("-", "")) >= 8

        if is_long_number:
            # Try as Order ID and Claim ID in parallel
            async def _try_order():
                try:
                    # resolve_order maneja pack_id → order_id automáticamente
                    order = await client.resolve_order(query)
                    if order and order.get("id"):
                        return order
                except Exception:
                    return None

            async def _try_claim():
                try:
                    claim = await client.get(f"/post-purchase/v1/claims/{query}")
                    if claim and claim.get("id"):
                        return claim
                except Exception:
                    return None

            # Search claims that match this order (resource_id) — fetch ALL claims, no date filter
            async def _find_claims_for_order(order_id):
                try:
                    all_cl = await client.fetch_all_claims(status=None, date_from=None, date_to=None)
                    return [cl for cl in all_cl
                            if str(cl.get("resource_id", "")) == str(order_id)]
                except Exception:
                    return []

            order_result, claim_result = await asyncio.gather(
                _try_order(), _try_claim(), return_exceptions=True
            )
            order_result = order_result if not isinstance(order_result, Exception) else None
            claim_result = claim_result if not isinstance(claim_result, Exception) else None

            if order_result and order_result.get("id"):
                order = order_result
                oi = order.get("order_items", [])
                item_info = oi[0].get("item", {}) if oi else {}
                ship = order.get("shipping", {})
                claims_for_order = await _find_claims_for_order(order["id"])
                # Refresh status of claims from search API (may be stale)
                if claims_for_order:
                    sem_r = asyncio.Semaphore(5)
                    async def _refresh_cl(cl):
                        async with sem_r:
                            try:
                                d = await client.get_claim_detail(str(cl.get("id", "")))
                                if isinstance(d, dict) and d.get("status"):
                                    cl["status"] = d["status"]
                                    if d.get("stage"):
                                        cl["stage"] = d["stage"]
                            except Exception:
                                pass
                    await asyncio.gather(*[_refresh_cl(cl) for cl in claims_for_order[:10]],
                                         return_exceptions=True)
                results.append({
                    "type": "order",
                    "id": order.get("id", ""),
                    "status": order.get("status", ""),
                    "date": (order.get("date_created", "") or "")[:10],
                    "buyer": order.get("buyer", {}).get("nickname", ""),
                    "buyer_id": order.get("buyer", {}).get("id", ""),
                    "product_title": item_info.get("title", ""),
                    "product_id": item_info.get("id", ""),
                    "total_amount": order.get("total_amount", 0),
                    "currency": order.get("currency_id", ""),
                    "shipping_status": ship.get("status", ""),
                    "shipping_id": ship.get("id", ""),
                    "claims": claims_for_order,
                })
                result_type = "order"

            if claim_result and claim_result.get("id"):
                # Add claim as a separate result (even if order was also found)
                claim = claim_result
                results.append({
                    "type": "claim",
                    "id": claim.get("id", ""),
                    "status": claim.get("status", ""),
                    "reason_id": claim.get("reason_id", ""),
                    "date": (claim.get("date_created", "") or "")[:10],
                    "order_id": claim.get("resource_id", ""),
                    "stage": claim.get("stage", ""),
                })
                if not result_type:
                    result_type = "claim"

        # Keyword search via orders
        if not results:
            try:
                seller_id = client.user_id
                data = await client.get(f"/orders/search?seller={seller_id}&q={query}&sort=date_desc&limit=10")
                for order in data.get("results", []):
                    oi = order.get("order_items", [])
                    item_info = oi[0].get("item", {}) if oi else {}
                    ship = order.get("shipping", {})
                    results.append({
                        "type": "order",
                        "id": order.get("id", ""),
                        "status": order.get("status", ""),
                        "date": (order.get("date_created", "") or "")[:10],
                        "buyer": order.get("buyer", {}).get("nickname", ""),
                        "buyer_id": order.get("buyer", {}).get("id", ""),
                        "product_title": item_info.get("title", ""),
                        "product_id": item_info.get("id", ""),
                        "total_amount": order.get("total_amount", 0),
                        "currency": order.get("currency_id", ""),
                        "shipping_status": ship.get("status", ""),
                        "shipping_id": ship.get("id", ""),
                        "claims": [],
                    })
                result_type = "search"
            except Exception:
                pass

        return templates.TemplateResponse(request, "partials/health_search_results.html", {            "results": results,
            "query": query,
            "result_type": result_type,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error en busqueda: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-messages", response_class=HTMLResponse)
async def health_messages_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        df = date_from or None
        dt = date_to or None
        try:
            data = await client.get_messages(offset=offset, limit=limit,
                                             date_from=df, date_to=dt)
        except Exception:
            data = {"results": [], "paging": {"total": 0, "offset": 0, "limit": limit}}
        raw_messages = data.get("results", data if isinstance(data, list) else [])
        paging = data.get("paging", {"total": len(raw_messages), "offset": offset, "limit": limit})
        seller_id = str(client.user_id)

        # Collect order_ids for context enrichment
        order_context_map = {}  # order_id -> {product, amount, buyer}
        order_ids_for_context = []
        for msg in raw_messages:
            oid = msg.get("order_id", msg.get("resource_id", ""))
            if oid:
                order_ids_for_context.append(str(oid))

        if order_ids_for_context:
            sem_oc = asyncio.Semaphore(5)
            async def _fetch_order_ctx(oid):
                async with sem_oc:
                    try:
                        order = await client.get(f"/orders/{oid}")
                        oi = order.get("order_items", [])
                        item_info = oi[0].get("item", {}) if oi else {}
                        return oid, {
                            "product_title": item_info.get("title", ""),
                            "total_amount": order.get("total_amount", 0),
                            "currency": order.get("currency_id", ""),
                            "buyer": order.get("buyer", {}).get("nickname", ""),
                            "status": order.get("status", ""),
                        }
                    except Exception:
                        return oid, {}
            oc_tasks = [_fetch_order_ctx(oid) for oid in list(set(order_ids_for_context))[:20]]
            oc_results = await asyncio.gather(*oc_tasks, return_exceptions=True)
            for r in oc_results:
                if isinstance(r, tuple):
                    order_context_map[r[0]] = r[1]

        enriched = []
        for msg in raw_messages:
            messages_list = msg.get("messages", [])
            last_5 = messages_list[-5:] if messages_list else []

            # Determine who wrote last and elapsed time
            last_msg = messages_list[-1] if messages_list else None
            last_from_buyer = False
            last_elapsed = "-"
            needs_response = False
            if last_msg:
                from_id = str(last_msg.get("from", {}).get("user_id", ""))
                last_from_buyer = from_id != seller_id
                needs_response = last_from_buyer
                ts = last_msg.get("date_created", last_msg.get("date", ""))
                if ts:
                    last_elapsed, _ = _elapsed_str(ts)

            conv_date = msg.get("date_created", msg.get("date", ""))

            # Enrich individual messages
            enriched_msgs = []
            for m in last_5:
                from_id = str(m.get("from", {}).get("user_id", ""))
                is_seller = from_id == seller_id
                text_raw = m.get("text", "")
                if isinstance(text_raw, dict):
                    text = text_raw.get("plain", str(text_raw))
                else:
                    text = str(text_raw) if text_raw else "-"
                msg_date = m.get("date_created", m.get("date", ""))
                msg_time = msg_date[11:16] if msg_date and len(msg_date) > 16 else ""
                enriched_msgs.append(SimpleNamespace(
                    text=text,
                    is_seller=is_seller,
                    time=msg_time,
                ))

            pack_id = msg.get("id", msg.get("pack_id", ""))
            oid = str(msg.get("order_id", msg.get("resource_id", "")))
            order_ctx = order_context_map.get(oid, {})

            enriched.append(SimpleNamespace(
                pack_id=pack_id,
                order_id=oid,
                date=conv_date[:10] if conv_date else "-",
                _sort_date=conv_date or "",
                last_from_buyer=last_from_buyer,
                last_elapsed=last_elapsed,
                needs_response=needs_response,
                messages=enriched_msgs,
                order_product=order_ctx.get("product_title", ""),
                order_amount=order_ctx.get("total_amount", 0),
                order_currency=order_ctx.get("currency", ""),
                order_buyer=order_ctx.get("buyer", ""),
                order_status=order_ctx.get("status", ""),
            ))

        # Sort: needs_response first, then newest first
        enriched.sort(key=lambda m: (0 if m.needs_response else 1, m._sort_date), reverse=False)
        # Within each group, sort by date descending
        from itertools import groupby as _grp
        sorted_msgs = []
        enriched_by_nr = sorted(enriched, key=lambda m: 0 if m.needs_response else 1)
        for _, group in _grp(enriched_by_nr, key=lambda m: m.needs_response):
            grp = sorted(list(group), key=lambda m: m._sort_date, reverse=True)
            sorted_msgs.extend(grp)
        enriched = sorted_msgs

        return templates.TemplateResponse(request, "partials/health_messages.html", {            "conversations": enriched,
            "paging": paging,
            "offset": offset,
            "limit": limit,
            "seller_id": seller_id,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando mensajes: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/health-reputation", response_class=HTMLResponse)
async def health_reputation_partial(request: Request):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        user = await client.get_user_info()
        rep = user.get("seller_reputation", {})
        metrics = rep.get("metrics", {})
        transactions = rep.get("transactions", {})
        ratings = transactions.get("ratings", {})

        claims_rate = metrics.get("claims", {}).get("rate", 0) or 0
        cancel_rate = metrics.get("cancellations", {}).get("rate", 0) or 0
        delay_rate = metrics.get("delayed_handling_time", {}).get("rate", 0) or 0

        level = rep.get("level_id", "unknown")

        # Compute margin data
        claims_margin = _compute_metric_margin(claims_rate, "claims")
        cancel_margin = _compute_metric_margin(cancel_rate, "cancellations")
        delay_margin = _compute_metric_margin(delay_rate, "delays")

        # Sales count for context
        total_sales = metrics.get("claims", {}).get("value", 0) or 0
        period = metrics.get("claims", {}).get("period", "60 days")

        # Compute how many more incidents before next threshold
        def _margin_count(rate, key, total):
            t = _MELI_THRESHOLDS[key]
            if rate < t["green"]:
                remaining = (t["green"] - rate) * max(total, 100)
                return int(remaining), "verde"
            elif rate < t["yellow"]:
                remaining = (t["yellow"] - rate) * max(total, 100)
                return int(remaining), "naranja"
            elif rate < t["red"]:
                remaining = (t["red"] - rate) * max(total, 100)
                return int(remaining), "rojo"
            return 0, "critico"

        claims_remaining, claims_next_zone = _margin_count(claims_rate, "claims", total_sales)
        cancel_remaining, cancel_next_zone = _margin_count(cancel_rate, "cancellations", total_sales)
        delay_remaining, delay_next_zone = _margin_count(delay_rate, "delays", total_sales)

        tips = []
        if claims_rate >= 0.02:
            tips.append({
                "text": "Reducir reclamos: responder preguntas proactivamente y mejorar descripciones",
                "severity": "red" if claims_rate >= 0.04 else "yellow",
                "tab": "claims",
            })
        if cancel_rate >= 0.025:
            tips.append({
                "text": "Reducir cancelaciones: mantener stock actualizado y verificar antes de publicar",
                "severity": "red" if cancel_rate >= 0.05 else "yellow",
                "tab": "claims",
            })
        if delay_rate >= 0.15:
            tips.append({
                "text": "Reducir demoras: usar Fulfillment o enviar el mismo dia del pago",
                "severity": "red" if delay_rate >= 0.20 else "yellow",
                "tab": "reputation",
            })
        if level != "5_green":
            tips.append({
                "text": "Para subir a MercadoLider: mantener las 3 metricas en verde y aumentar volumen",
                "severity": "blue",
                "tab": "reputation",
            })
        if not tips:
            tips.append({
                "text": "Excelente! Todas las metricas estan en rango optimo",
                "severity": "green",
                "tab": "",
            })

        reputation = SimpleNamespace(
            level=level,
            power_seller=rep.get("power_seller_status", None),
            completed=transactions.get("completed", 0),
            canceled=transactions.get("canceled", 0),
            positive=ratings.get("positive", 0),
            negative=ratings.get("negative", 0),
            neutral=ratings.get("neutral", 0),
            claims_rate=claims_rate,
            claims_pct=round(claims_rate * 100, 2),
            claims_status=_metric_status(claims_rate, "claims"),
            claims_margin=claims_margin,
            claims_remaining=claims_remaining,
            claims_next_zone=claims_next_zone,
            cancellation_rate=cancel_rate,
            cancellation_pct=round(cancel_rate * 100, 2),
            cancellation_status=_metric_status(cancel_rate, "cancellations"),
            cancel_margin=cancel_margin,
            cancel_remaining=cancel_remaining,
            cancel_next_zone=cancel_next_zone,
            delayed_rate=delay_rate,
            delayed_pct=round(delay_rate * 100, 2),
            delayed_status=_metric_status(delay_rate, "delays"),
            delay_margin=delay_margin,
            delay_remaining=delay_remaining,
            delay_next_zone=delay_next_zone,
            improvement_tips=tips,
            period=period,
        )

        return templates.TemplateResponse(request, "partials/health_reputation.html", {            "reputation": reputation,
            "thresholds": _MELI_THRESHOLDS,
        })
    finally:
        await client.close()


@app.get("/partials/item-edit/{item_id}", response_class=HTMLResponse)
async def item_edit_partial(request: Request, item_id: str):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        from app.api.items import _calculate_health_score
        item = await client.get_item(item_id)

        # Enriquecer con sale_price para detectar deals
        sp = await client.get_item_sale_price(item_id)
        if sp and isinstance(sp, dict):
            amount = sp.get("amount")
            regular = sp.get("regular_amount")
            if amount and regular and regular > amount:
                item["price"] = amount
                item["original_price"] = regular

        # Obtener descripcion
        try:
            desc_data = await client.get_item_description(item_id)
            description = desc_data.get("plain_text", desc_data.get("text", ""))
        except Exception:
            description = ""

        score, problems, breakdown = _calculate_health_score(item, description)

        # Extract seller_sku — prioridad: variaciones > padre
        seller_sku = _get_item_sku(item)

        listing_type = item.get("listing_type_id", "")

        return templates.TemplateResponse(request, "partials/item_edit_modal.html", {
            "item": item,
            "description": description,
            "score": score,
            "problems": problems,
            "breakdown": breakdown,
            "seller_sku": seller_sku,
            "listing_type": listing_type,
        })
    finally:
        await client.close()


@app.get("/partials/item-deal/{item_id}", response_class=HTMLResponse)
async def item_deal_partial(request: Request, item_id: str):
    """Deal management partial: loads inside edit-modal for a single item."""
    import httpx
    BM_INV_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU"
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        item = await client.get_item(item_id)
        sku = _get_item_sku(item) if item else ""
        price = item.get("price", 0)
        original_price = item.get("original_price") or 0

        # Sale price enrichment
        sp = await client.get_item_sale_price(item_id)
        if sp and isinstance(sp, dict):
            amt = sp.get("amount")
            reg = sp.get("regular_amount")
            if amt and reg and reg > amt:
                price = amt
                original_price = reg

        has_deal = original_price and original_price > price

        # BM cost + FX rate in parallel
        usd_to_mxn = 20.0
        bm_cost_usd = 0
        bm_retail_usd = 0
        async def _get_bm():
            if not sku:
                return 0, 0
            base = _extract_base_sku(sku).upper()
            async with httpx.AsyncClient() as http:
                resp = await http.post(BM_INV_URL, json={
                    "COMPANYID": 1, "SEARCH": base, "CONCEPTID": 8,
                    "NUMBERPAGE": 1, "RECORDSPAGE": 10,
                }, headers={"Content-Type": "application/json"}, timeout=30.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list):
                        for it in data:
                            if it.get("SKU", "").upper() == base:
                                return it.get("AvgCostQTY", 0) or 0, it.get("RetailPrice", 0) or 0
                        if data:
                            return data[0].get("AvgCostQTY", 0) or 0, data[0].get("RetailPrice", 0) or 0
            return 0, 0

        (bm_cost_usd, bm_retail_usd), usd_to_mxn = await asyncio.gather(
            _get_bm(),
            _get_usd_to_mxn(client),
        )

        return templates.TemplateResponse(request, "partials/item_deal_modal.html", {            "item_id": item_id,
            "title": item.get("title", ""),
            "thumbnail": item.get("thumbnail", ""),
            "sku": sku,
            "price": price,
            "original_price": original_price,
            "has_deal": has_deal,
            "bm_cost_usd": bm_cost_usd,
            "bm_retail_usd": bm_retail_usd,
            "usd_to_mxn": round(usd_to_mxn, 2),
        })
    finally:
        await client.close()


@app.get("/partials/sku-sales-table", response_class=HTMLResponse)
async def sku_sales_table_partial(
    request: Request,
    date_from: str = Query("", description="Fecha inicio YYYY-MM-DD"),
    date_to: str = Query("", description="Fecha fin YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        all_orders = await client.fetch_all_orders(
            date_from=date_from or None,
            date_to=date_to or None
        )

        # Paso 1: recolectar ventas por SKU crudo (tal cual viene de la orden)
        _EXCL_SKU = {"cancelled", "payment_required", "payment_in_process"}
        raw_map = {}
        for order in all_orders:
            if order.get("status") in _EXCL_SKU:
                continue

            for order_item in order.get("order_items", []):
                item = order_item.get("item", {})
                sku = item.get("seller_sku") or item.get("seller_custom_field") or "SIN SKU"
                title = item.get("title", "-")
                quantity = order_item.get("quantity", 1)
                unit_price = order_item.get("unit_price", 0)
                sale_fee = order_item.get("sale_fee", 0) or 0
                iva_fee = sale_fee * 0.16
                net = unit_price * quantity - sale_fee - iva_fee

                if sku not in raw_map:
                    raw_map[sku] = {"sku": sku, "title": title, "quantity": 0, "revenue": 0}
                raw_map[sku]["quantity"] += quantity
                raw_map[sku]["revenue"] += net

        # Paso 2: agrupar variantes por SKU base
        # - SKU base / sufijo GR (-NEW,-GRA,-GRB,-GRC): suma solo variantes GR
        # - Sufijo IC (-ICC,-ICB): suma TODAS las variantes (GR + IC)
        base_groups = {}  # base -> {raw_skus: set, has_ic: bool}
        for sku in raw_map:
            base = _extract_base_sku(sku)
            if base not in base_groups:
                base_groups[base] = {"raw_skus": set(), "has_ic": False}
            base_groups[base]["raw_skus"].add(sku)
            upper = sku.upper()
            if any(upper.endswith(sfx) for sfx in _IC_SUFFIXES):
                base_groups[base]["has_ic"] = True

        sku_map = {}
        for base, group in base_groups.items():
            has_ic = group["has_ic"]
            for raw_sku in group["raw_skus"]:
                upper = raw_sku.upper()
                is_ic = any(upper.endswith(sfx) for sfx in _IC_SUFFIXES)
                is_gr = any(upper.endswith(sfx) for sfx in _GR_SUFFIXES)
                is_base = (not is_ic and not is_gr and raw_sku != "SIN SKU")

                # Si hay variantes IC, los GR y base se fusionan en la fila IC
                if has_ic:
                    # Todos van a la misma fila
                    key = base + "_ALL"
                else:
                    # Solo variantes GR y base se fusionan
                    if is_gr or is_base:
                        key = base + "_GR"
                    else:
                        # SKU sin sufijo conocido, dejar individual
                        key = raw_sku

                data = raw_map[raw_sku]
                if key not in sku_map:
                    sku_map[key] = {"sku": data["sku"], "title": data["title"], "quantity": 0, "revenue": 0}
                sku_map[key]["quantity"] += data["quantity"]
                sku_map[key]["revenue"] += data["revenue"]

        # Ordenar por cantidad vendida descendente
        sku_sales = sorted(sku_map.values(), key=lambda x: x["quantity"], reverse=True)
        total_quantity = sum(s["quantity"] for s in sku_sales)
        total_revenue = sum(s["revenue"] for s in sku_sales)

        # Enriquecer con BinManager (costo, retail PH) y calcular márgenes
        for s in sku_sales:
            # Precio promedio de venta para cálculos de margen
            s["price"] = round(s["revenue"] / s["quantity"], 2) if s["quantity"] else 0

        usd_to_mxn, _ = await asyncio.gather(
            _get_usd_to_mxn(client),
            _enrich_with_bm_product_info(sku_sales),
        )
        _calc_margins(sku_sales, usd_to_mxn)

        return templates.TemplateResponse(request, "partials/sku_sales_table.html", {            "sku_sales": sku_sales,
            "total_quantity": total_quantity,
            "total_revenue": total_revenue
        })
    finally:
        await client.close()


# === Ads API y Partials ===

def _default_dates(date_from, date_to):
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    return (
        date_from or (now - timedelta(days=7)).strftime("%Y-%m-%d"),
        date_to or now.strftime("%Y-%m-%d"),
    )


def _extract_metrics(data: dict) -> dict:
    """Extrae metricas de la respuesta de Product Ads (soporta summary y results)."""
    # La API puede retornar metrics_summary o results con metricas embebidas
    summary = data.get("metrics_summary", {})
    if summary:
        return {
            "cost": summary.get("cost", 0) or 0,
            "clicks": summary.get("clicks", 0) or 0,
            "prints": summary.get("prints", 0) or 0,
            "cpc": summary.get("cpc", 0) or 0,
            "acos": summary.get("acos", 0) or 0,
            "units": summary.get("units_quantity", 0) or 0,
            "revenue": summary.get("total_amount", 0) or 0,
        }
    # Fallback: sumar de results
    results = data.get("results", data if isinstance(data, list) else [])
    if isinstance(results, list):
        total = {"cost": 0, "clicks": 0, "prints": 0, "units": 0, "revenue": 0}
        for r in results:
            m = r.get("metrics", r)
            total["cost"] += (m.get("cost", 0) or 0)
            total["clicks"] += (m.get("clicks", 0) or 0)
            total["prints"] += (m.get("prints", 0) or 0)
            total["units"] += (m.get("units_quantity", 0) or 0)
            total["revenue"] += (m.get("total_amount", 0) or 0)
        return total
    return {"cost": 0, "clicks": 0, "prints": 0, "units": 0, "revenue": 0}


def _enrich_campaigns(campaigns_data) -> list:
    """Convierte respuesta de campanas en lista enriquecida con metricas calculadas."""
    results = campaigns_data.get("results", campaigns_data if isinstance(campaigns_data, list) else [])
    enriched = []
    for c in results:
        metrics = c.get("metrics", {})
        cost = metrics.get("cost", 0) or 0
        clicks = metrics.get("clicks", 0) or 0
        prints = metrics.get("prints", 0) or 0
        revenue = metrics.get("total_amount", 0) or 0
        units = metrics.get("units_quantity", 0) or 0
        budget_data = c.get("budget", {})
        daily_budget = budget_data.get("amount", 0) if isinstance(budget_data, dict) else (budget_data or 0)
        enriched.append({
            "id": c.get("id", "-"),
            "name": c.get("name", c.get("id", "-")),
            "status": c.get("status", "-"),
            "daily_budget": daily_budget,
            "cost": cost,
            "clicks": clicks,
            "impressions": prints,
            "revenue": revenue,
            "units": units,
            "ctr": clicks / prints * 100 if prints > 0 else 0,
            "roas": (revenue / cost) if cost > 0 else 0,
            "cpc": cost / clicks if clicks > 0 else 0,
            "acos": (cost / revenue * 100) if revenue > 0 else 0,
        })
    return enriched


@app.get("/api/ads/metrics")
async def ads_metrics_api(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD")
):
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        m = None
        # Intentar get_ads_items primero
        try:
            data = await client.get_ads_items(date_from, date_to)
            m = _extract_metrics(data)
        except Exception:
            pass

        # Fallback: usar metricas agregadas de campanas
        if m is None or (m["cost"] == 0 and m["clicks"] == 0):
            try:
                campaigns_data = await client.get_ads_campaigns(date_from, date_to)
                m_camp = _extract_metrics(campaigns_data)
                # Si campanas tiene datos, usarlos
                if m_camp["cost"] > 0 or m_camp["clicks"] > 0:
                    m = m_camp
                elif m is None:
                    m = m_camp
            except Exception as e:
                if m is None:
                    return {"error": f"No se pudieron obtener metricas de Ads: {e}"}

        total_cost = m["cost"]
        total_clicks = m["clicks"]
        total_prints = m["prints"]
        total_units = m["units"]
        total_revenue = m.get("revenue", 0)

        cpc = total_cost / total_clicks if total_clicks > 0 else 0
        ctr = total_clicks / total_prints * 100 if total_prints > 0 else 0
        roas = total_revenue / total_cost if total_cost > 0 else 0
        acos = (total_cost / total_revenue * 100) if total_revenue > 0 else 0

        return {
            "total_cost": f"{total_cost:.2f}",
            "total_revenue": f"{total_revenue:.2f}",
            "clicks": total_clicks,
            "impressions": total_prints,
            "sales": total_units,
            "cpc": f"{cpc:.2f}",
            "ctr": f"{ctr:.2f}",
            "acos": f"{acos:.1f}",
            "roas": f"{roas:.2f}",
        }
    finally:
        await client.close()


@app.get("/partials/ads-campaigns", response_class=HTMLResponse)
async def ads_campaigns_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
        except Exception as e:
            return HTMLResponse(f"<p class='p-6 text-center text-gray-500'>Error obteniendo campanas: {e}</p>")

        enriched = _enrich_campaigns(campaigns_data)
        enriched.sort(key=lambda x: x["cost"], reverse=True)

        return templates.TemplateResponse(request, "partials/ads_campaigns.html", {            "campaigns": enriched,
        })
    finally:
        await client.close()


@app.get("/partials/ads-products", response_class=HTMLResponse)
async def ads_products_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # Obtener campanas primero (siempre funciona) como fallback seguro
        camps = None
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
        except Exception:
            pass

        # Intentar items para detalle por producto
        try:
            data = await client.get_ads_items(date_from, date_to)
            results = data.get("results", data if isinstance(data, list) else [])
            products = []
            for item in results:
                metrics = item.get("metrics", item)
                cost = metrics.get("cost", 0) or 0
                clicks = metrics.get("clicks", 0) or 0
                prints = metrics.get("prints", 0) or 0
                units = metrics.get("units_quantity", 0) or 0
                revenue = metrics.get("total_amount", 0) or 0
                roas = (revenue / cost) if cost > 0 else 0
                products.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "cost": cost,
                    "clicks": clicks,
                    "impressions": prints,
                    "sales": units,
                    "revenue": revenue,
                    "roas": roas,
                })
            products.sort(key=lambda x: x["cost"], reverse=True)
            top = products[:20]
            return templates.TemplateResponse(request, "partials/ads_products.html", {                "products": top,
                "total_cost": sum(p["cost"] for p in products),
                "total_revenue": sum(p["revenue"] for p in products),
            })
        except Exception:
            pass

        # Fallback: mostrar campanas ordenadas por gasto
        if camps:
            camps_with_cost = [c for c in camps if c["cost"] > 0]
            camps_with_cost.sort(key=lambda x: x["cost"], reverse=True)
            return templates.TemplateResponse(request, "partials/ads_products.html", {                "products": [],
                "total_cost": sum(c["cost"] for c in camps_with_cost),
                "total_revenue": sum(c["revenue"] for c in camps_with_cost),
                "fallback_campaigns": camps_with_cost,
            })

        return templates.TemplateResponse(request, "partials/ads_products.html", {            "products": [],
            "total_cost": 0,
            "total_revenue": 0,
            "unavailable": True,
        })
    finally:
        await client.close()


@app.get("/partials/ads-burning", response_class=HTMLResponse)
async def ads_burning_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # Obtener campanas primero (fiable) como fallback
        camps = None
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
        except Exception:
            pass

        # Intentar items para detalle por producto
        items_ok = False
        try:
            data = await client.get_ads_items(date_from, date_to)
            items_ok = True
        except Exception:
            pass

        if not items_ok:
            if camps is not None:
                burning_camps = [c for c in camps if c["cost"] > 0 and c["units"] == 0]
                burning_camps.sort(key=lambda x: x["cost"], reverse=True)
                return templates.TemplateResponse(request, "partials/ads_burning.html", {                    "burning": [],
                    "total_burned": sum(c["cost"] for c in burning_camps),
                    "fallback_campaigns": burning_camps,
                })
            return templates.TemplateResponse(request, "partials/ads_burning.html", {                "burning": [],
                "total_burned": 0,
                "unavailable": True,
            })

        results = data.get("results", data if isinstance(data, list) else [])

        burning = []
        for item in results:
            metrics = item.get("metrics", item)
            cost = metrics.get("cost", 0) or 0
            units = metrics.get("units_quantity", 0) or 0
            if cost > 0 and units == 0:
                clicks = metrics.get("clicks", 0) or 0
                prints = metrics.get("prints", 0) or 0
                burning.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "cost": cost,
                    "clicks": clicks,
                    "impressions": prints,
                    "ctr": round(clicks / prints * 100, 3) if prints > 0 else 0,
                })

        burning.sort(key=lambda x: x["cost"], reverse=True)
        total_burned = sum(p["cost"] for p in burning)

        # Enriquecer con detalles del listado para diagnostico
        item_ids = [p["item_id"] for p in burning if p["item_id"] != "-"]
        item_details = {}
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if body:
                        item_details[body.get("id", "")] = body
            except Exception:
                pass

        for p in burning:
            body = item_details.get(p["item_id"], {})
            p["price"] = body.get("price", 0)
            p["original_price"] = body.get("original_price") or None
            p["stock"] = body.get("available_quantity", 0)
            p["sold"] = body.get("sold_quantity", 0)
            p["status"] = body.get("status", "unknown")
            p["photos"] = len(body.get("pictures", []))
            p["has_video"] = bool(body.get("video_id"))
            p["catalog"] = bool(body.get("catalog_product_id"))
            shipping = body.get("shipping", {})
            p["free_shipping"] = shipping.get("free_shipping", False) or shipping.get("logistic_type") == "fulfillment"
            p["logistic"] = shipping.get("logistic_type", "")
            p["permalink"] = body.get("permalink", "")

            # Generar diagnostico y sugerencias
            issues = []
            suggestions = []

            if p["status"] == "paused":
                issues.append("Producto PAUSADO — Ads sigue cobrando sin posibilidad de venta")
                suggestions.append("Pausar campana de Ads inmediatamente para dejar de perder dinero")
            if p["stock"] == 0 and p["status"] != "paused":
                issues.append("Sin stock disponible")
                suggestions.append("Reponer stock o pausar Ads hasta tener inventario")
            if p["photos"] < 5:
                issues.append(f"Solo {p['photos']} fotos — insuficiente para generar confianza")
                suggestions.append("Subir minimo 8-10 fotos: frente, perfil, detalle, producto en uso, empaque")
            elif p["photos"] < 8:
                issues.append(f"{p['photos']} fotos — por debajo del promedio competitivo")
                suggestions.append("Agregar fotos de contexto/ambientacion y detalles del producto")
            if not p["has_video"]:
                issues.append("Sin video en el listado")
                suggestions.append("Agregar video corto mostrando el producto en uso")
            if p["ctr"] < 0.1 and p["impressions"] > 1000:
                issues.append(f"CTR muy bajo ({p['ctr']}%) — el anuncio no atrae clics")
                suggestions.append("Mejorar foto principal, titulo y precio para atraer mas clics")
            elif p["ctr"] > 0.3 and p["clicks"] > 100:
                issues.append(f"CTR alto ({p['ctr']}%) pero 0 ventas — el listado no convierte")
                suggestions.append("Revisar precio vs competencia, mejorar descripcion y fotos del listado")
            if p["clicks"] > 300 and p["price"] > 5000:
                issues.append(f"Muchos clics ({p['clicks']}) sin conversion en producto de ${p['price']:,.0f}")
                suggestions.append("Verificar competitividad de precio — posiblemente esta por encima del mercado")
            if not p["free_shipping"]:
                issues.append("No ofrece envio gratis")
                suggestions.append("Activar envio gratis — es casi obligatorio para competir en MeLi")
            if p["catalog"] and p["clicks"] > 200:
                issues.append("Producto en catalogo — puede no estar ganando el Buy Box")
                suggestions.append("Verificar si estas ganando el Buy Box; ajustar precio o mejorar reputacion/envio")

            if not issues:
                issues.append("No se detectaron problemas evidentes en el listado")
                suggestions.append("Considerar pausar Ads y evaluar si el producto tiene demanda real")

            p["issues"] = issues
            p["suggestions"] = suggestions

        # Enriquecer con sale_price para detectar deals
        await _enrich_with_sale_prices(client, burning, id_key="item_id", price_key="price")

        return templates.TemplateResponse(request, "partials/ads_burning.html", {            "burning": burning,
            "total_burned": total_burned,
        })
    finally:
        await client.close()


@app.get("/api/ads/spend-timeline")
async def ads_spend_timeline_api(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    group_by: str = Query("day", description="day|month")
):
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        from datetime import datetime, timedelta

        date_from, date_to = _default_dates(date_from, date_to)
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")

        timeline = []

        async def _get_period_metrics(df: str, dt: str):
            """Intenta get_ads_items, fallback a get_ads_campaigns para un periodo."""
            try:
                data = await client.get_ads_items(df, dt)
                m = _extract_metrics(data)
                if m["cost"] > 0 or m["clicks"] > 0:
                    return m
            except Exception:
                pass
            try:
                data = await client.get_ads_campaigns(df, dt)
                return _extract_metrics(data)
            except Exception:
                return {"cost": 0, "clicks": 0, "prints": 0, "units": 0, "revenue": 0}

        if group_by == "month":
            # Agrupar por mes (max 12 meses)
            current = start.replace(day=1)
            count = 0
            while current <= end and count < 12:
                month_start = current
                if current.month == 12:
                    month_end = current.replace(year=current.year + 1, month=1, day=1) - timedelta(days=1)
                else:
                    month_end = current.replace(month=current.month + 1, day=1) - timedelta(days=1)
                if month_end > end:
                    month_end = end

                m = await _get_period_metrics(
                    month_start.strftime("%Y-%m-%d"),
                    month_end.strftime("%Y-%m-%d")
                )
                timeline.append({
                    "date": month_start.strftime("%Y-%m"),
                    "cost": m["cost"],
                    "clicks": m["clicks"],
                    "impressions": m["prints"],
                    "units": m["units"],
                    "revenue": m["revenue"],
                })

                if current.month == 12:
                    current = current.replace(year=current.year + 1, month=1)
                else:
                    current = current.replace(month=current.month + 1)
                count += 1
        else:
            # Agrupar por dia (max 31 dias)
            delta = (end - start).days
            if delta > 31:
                start = end - timedelta(days=31)

            current = start
            while current <= end:
                day_str = current.strftime("%Y-%m-%d")
                m = await _get_period_metrics(day_str, day_str)
                timeline.append({
                    "date": day_str,
                    "cost": m["cost"],
                    "clicks": m["clicks"],
                    "impressions": m["prints"],
                    "units": m["units"],
                    "revenue": m["revenue"],
                })
                current += timedelta(days=1)

        return {"timeline": timeline}
    finally:
        await client.close()


@app.get("/partials/ads-best", response_class=HTMLResponse)
async def ads_best_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # Obtener campanas primero (fiable) como fallback
        camps = None
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
        except Exception:
            pass

        # Intentar items para detalle por producto
        items_ok = False
        try:
            data = await client.get_ads_items(date_from, date_to)
            items_ok = True
        except Exception:
            pass

        if not items_ok:
            if camps is not None:
                best_camps = [c for c in camps if c["units"] > 0 and c["cost"] > 0]
                best_camps.sort(key=lambda x: x["roas"], reverse=True)
                return templates.TemplateResponse(request, "partials/ads_best.html", {                    "best": [],
                    "total_cost": sum(c["cost"] for c in best_camps),
                    "total_units": sum(c["units"] for c in best_camps),
                    "total_revenue": sum(c["revenue"] for c in best_camps),
                    "fallback_campaigns": best_camps,
                })
            return templates.TemplateResponse(request, "partials/ads_best.html", {                "best": [],
                "total_cost": 0,
                "total_units": 0,
                "total_revenue": 0,
                "unavailable": True,
            })

        results = data.get("results", data if isinstance(data, list) else [])

        best = []
        for item in results:
            metrics = item.get("metrics", item)
            cost = metrics.get("cost", 0) or 0
            clicks = metrics.get("clicks", 0) or 0
            prints = metrics.get("prints", 0) or 0
            units = metrics.get("units_quantity", 0) or 0
            revenue = metrics.get("total_amount", 0) or 0
            roas = (revenue / cost) if cost > 0 else 0

            if units > 0:
                best.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "cost": cost,
                    "clicks": clicks,
                    "units": units,
                    "revenue": revenue,
                    "roas": roas,
                })

        # Ordenar por ROAS descendente
        best.sort(key=lambda x: x["roas"], reverse=True)
        top = best[:15]
        total_cost = sum(p["cost"] for p in top)
        total_units = sum(p["units"] for p in top)
        total_revenue = sum(p["revenue"] for p in top)

        return templates.TemplateResponse(request, "partials/ads_best.html", {            "best": top,
            "total_cost": total_cost,
            "total_units": total_units,
            "total_revenue": total_revenue,
        })
    finally:
        await client.close()


@app.get("/partials/ads-performance", response_class=HTMLResponse)
async def ads_performance_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
    sort: str = Query("cost"),
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1),
    category: str = Query(""),
    tier: str = Query("all"),
):
    """Tabla unificada de rendimiento por producto con paginacion, tiers y filtro categoria."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)
        sort_key = sort if sort in ("cost", "roas", "revenue", "clicks", "units", "acos") else "cost"
        show_all = per_page >= 9999
        valid_tiers = ("all", "top", "medio", "bajo", "sin_venta")
        tier_filter = tier if tier in valid_tiers else "all"

        # Obtener campanas como fallback
        camps = None
        camp_name_map = {}  # campaign_id -> name
        try:
            campaigns_data = await client.get_ads_campaigns(date_from, date_to)
            camps = _enrich_campaigns(campaigns_data)
            # Mapear campaign_id -> name para enriquecer items
            for c in campaigns_data.get("results", []):
                cid = str(c.get("id", c.get("campaign_id", "")))
                camp_name_map[cid] = c.get("name", cid)
        except Exception:
            pass

        # Intentar items para detalle por producto
        try:
            data = await client.get_ads_items(date_from, date_to)
            results = data.get("results", data if isinstance(data, list) else [])
            products = []
            for item in results:
                metrics = item.get("metrics", item)
                cost = metrics.get("cost", 0) or 0
                clicks = metrics.get("clicks", 0) or 0
                units = metrics.get("units_quantity", 0) or 0
                revenue = metrics.get("total_amount", 0) or 0
                roas = (revenue / cost) if cost > 0 else 0
                acos = (cost / revenue * 100) if revenue > 0 else 0
                # Tier classification
                if units == 0:
                    tier_val = "sin_venta"
                elif roas >= 5:
                    tier_val = "top"
                elif roas >= 2:
                    tier_val = "medio"
                else:
                    tier_val = "bajo"
                camp_id = str(item.get("campaign_id", ""))
                products.append({
                    "item_id": item.get("item_id", item.get("id", "-")),
                    "title": item.get("title", item.get("item_id", "-")),
                    "category_id": item.get("category_id", ""),
                    "category_name": item.get("category_name", ""),
                    "campaign_name": camp_name_map.get(camp_id, ""),
                    "cost": cost,
                    "clicks": clicks,
                    "units": units,
                    "revenue": revenue,
                    "roas": roas,
                    "acos": acos,
                    "tier": tier_val,
                })

            # Enriquecer category_name si falta (batch fetch)
            missing_cat_ids = {p["category_id"] for p in products if p["category_id"] and not p["category_name"]}
            if missing_cat_ids:
                for cid in missing_cat_ids:
                    if cid in _category_cache:
                        continue
                    try:
                        cat = await client.get(f"/categories/{cid}")
                        _category_cache[cid] = cat.get("name", cid)
                    except Exception:
                        _category_cache[cid] = cid
                for p in products:
                    if p["category_id"] and not p["category_name"]:
                        p["category_name"] = _category_cache.get(p["category_id"], p["category_id"])

            # Contar tiers antes de filtrar
            tier_counts = {
                "top": sum(1 for p in products if p["tier"] == "top"),
                "medio": sum(1 for p in products if p["tier"] == "medio"),
                "bajo": sum(1 for p in products if p["tier"] == "bajo"),
                "sin_venta": sum(1 for p in products if p["tier"] == "sin_venta"),
            }

            # Aplicar filtro de tier
            if tier_filter != "all":
                products = [p for p in products if p["tier"] == tier_filter]

            # Aplicar filtro de categoria
            if category:
                products = [p for p in products if p["category_id"] == category or p["category_name"] == category]

            products.sort(key=lambda x: x[sort_key], reverse=True)
            total_count = len(products)
            all_products_cost = sum(p["cost"] for p in products)
            all_products_revenue = sum(p["revenue"] for p in products)

            if show_all:
                page_products = products
                total_pages = 1
                page = 1
            else:
                total_pages = max(1, (total_count + per_page - 1) // per_page)
                page = min(page, total_pages)
                start = (page - 1) * per_page
                page_products = products[start:start + per_page]

            # Categorias unicas para el filtro dropdown
            all_categories = sorted(
                {(p["category_id"], p["category_name"]) for p in products if p["category_id"]},
                key=lambda x: x[1]
            )

            return templates.TemplateResponse(request, "partials/ads_performance.html", {                "products": page_products,
                "total_cost": all_products_cost,
                "total_revenue": all_products_revenue,
                "current_sort": sort_key,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "total_count": total_count,
                "show_all": show_all,
                "tier_filter": tier_filter,
                "category_filter": category,
                "tier_counts": tier_counts,
                "all_categories": all_categories,
            })
        except Exception:
            pass

        # Fallback: campanas ordenadas
        if camps:
            camps_with_cost = [c for c in camps if c["cost"] > 0]
            camps_with_cost.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
            return templates.TemplateResponse(request, "partials/ads_performance.html", {                "products": [],
                "total_cost": sum(c["cost"] for c in camps_with_cost),
                "total_revenue": sum(c["revenue"] for c in camps_with_cost),
                "fallback_campaigns": camps_with_cost,
                "current_sort": sort_key,
                "page": 1,
                "per_page": per_page,
                "total_pages": 1,
                "total_count": len(camps_with_cost),
                "show_all": False,
                "tier_filter": "all",
                "category_filter": "",
                "tier_counts": {},
                "all_categories": [],
            })

        return templates.TemplateResponse(request, "partials/ads_performance.html", {            "products": [],
            "total_cost": 0,
            "total_revenue": 0,
            "unavailable": True,
            "current_sort": "cost",
            "page": 1,
            "per_page": per_page,
            "total_pages": 1,
            "total_count": 0,
            "show_all": False,
            "tier_filter": "all",
            "category_filter": "",
            "tier_counts": {},
            "all_categories": [],
        })
    finally:
        await client.close()


@app.get("/partials/ads-no-ads", response_class=HTMLResponse)
async def ads_no_ads_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query("")
):
    """Productos activos que NO están en ninguna campaña de Ads."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        # 1. Obtener todos los item IDs activos
        try:
            all_active_ids = await client.get_all_active_item_ids()
        except Exception as e:
            return HTMLResponse(f"<p class='p-6 text-center text-gray-500'>Error obteniendo items activos: {e}</p>")

        # 2. Obtener TODOS los items con ads (sin filtro de fechas)
        #    Esto evita el bug de mostrar items que SI tienen ads pero sin metricas recientes
        ads_item_ids = set()
        try:
            ads_item_ids = await client.get_all_ads_item_ids()
        except Exception:
            pass

        # Fallback: si el metodo paginado fallo, intentar via campañas (sin filtro de fecha)
        if not ads_item_ids:
            try:
                campaigns_data = await client.get_ads_campaigns()
                results = campaigns_data.get("results", [])
                for c in results:
                    for item in c.get("items", []):
                        iid = item.get("id", "")
                        if iid:
                            ads_item_ids.add(iid)
            except Exception:
                pass

        # 3. Diferencia: activos sin ads
        no_ads_ids = [iid for iid in all_active_ids if iid not in ads_item_ids]

        # 4. Fetch detalles en batches (primeros 40)
        no_ads_ids = no_ads_ids[:40]
        products = []
        category_ids = set()
        for i in range(0, len(no_ads_ids), 20):
            batch = no_ads_ids[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if not body:
                        continue
                    cat_id = body.get("category_id", "")
                    if cat_id:
                        category_ids.add(cat_id)
                    products.append({
                        "id": body.get("id", ""),
                        "title": body.get("title", "-"),
                        "price": body.get("price", 0),
                        "original_price": body.get("original_price") or None,
                        "stock": body.get("available_quantity", 0),
                        "sold_quantity": body.get("sold_quantity", 0),
                        "thumbnail": body.get("thumbnail", body.get("secure_thumbnail", "")),
                        "permalink": body.get("permalink", ""),
                        "category_id": cat_id,
                        "category_name": "",
                    })
            except Exception:
                pass

        # Enriquecer con sale_price para detectar deals
        await _enrich_with_sale_prices(client, products, id_key="id", price_key="price")

        # 4b. Fetch nombres de categorias (pocos unicos)
        cat_names = {}
        for cat_id in category_ids:
            try:
                cat = await client.get(f"/categories/{cat_id}")
                cat_names[cat_id] = cat.get("name", cat_id)
            except Exception:
                cat_names[cat_id] = cat_id
        for p in products:
            p["category_name"] = cat_names.get(p["category_id"], p["category_id"])

        # 5. Ordenar por sold_quantity desc (mayor oportunidad primero)
        products.sort(key=lambda x: x["sold_quantity"], reverse=True)

        # 6. Obtener campanas activas para el selector
        campaigns = []
        try:
            camp_data = await client.get_ads_campaigns()
            for c in camp_data.get("results", []):
                if c.get("status") == "active":
                    campaigns.append({
                        "id": c.get("campaign_id", c.get("id", "")),
                        "name": c.get("name", ""),
                    })
        except Exception:
            pass
        # Fallback: si no obtuvimos campanas, intentar con fechas
        if not campaigns:
            try:
                camp_data = await client.get_ads_campaigns(date_from, date_to)
                for c in camp_data.get("results", []):
                    if c.get("status") == "active":
                        campaigns.append({
                            "id": c.get("campaign_id", c.get("id", "")),
                            "name": c.get("name", ""),
                        })
            except Exception:
                pass

        return templates.TemplateResponse(request, "partials/ads_no_ads.html", {            "products": products,
            "total_active": len(all_active_ids),
            "total_with_ads": len(ads_item_ids),
            "campaigns": campaigns,
        })
    finally:
        await client.close()


@app.post("/api/ads/assign-to-campaign")
async def assign_items_to_campaign_api(request: Request):
    """Asigna items a una campana de Product Ads."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        item_ids = body.get("item_ids", [])
        campaign_id = body.get("campaign_id")
        if not item_ids or not campaign_id:
            return JSONResponse({"detail": "item_ids y campaign_id requeridos"}, status_code=400)
        result = await client.assign_items_to_campaign(item_ids, int(campaign_id))
        return JSONResponse({"ok": True, "result": result})
    except MeliApiError as e:
        if e.status_code == 401 or "UnauthorizedException" in str(e):
            return JSONResponse({
                "detail": "Tu app de MeLi no tiene permiso de escritura para Product Ads. "
                          "Activa el permiso en developers.mercadolibre.com.mx > Tu App > "
                          "Permisos, o re-autenticate en /auth/connect"
            }, status_code=403)
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/ads/item/{item_id}")
async def get_ad_item_status_api(item_id: str):
    """Estado de un item en Product Ads via marketplace API v2."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        resp = await client._request_raw(
            "GET",
            f"/marketplace/advertising/MLM/product_ads/ads/{item_id}",
            extra_headers={"api-version": "2"},
        )
        return JSONResponse(resp)
    except MeliApiError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.post("/api/ads/item/{item_id}/status")
async def update_ad_item_status_api(item_id: str, request: Request):
    """Activa, pausa o remueve un item de Product Ads."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        status = body.get("status")  # "active", "paused", "idle"
        campaign_id = body.get("campaign_id")
        if not status:
            return JSONResponse({"detail": "Campo 'status' requerido"}, status_code=400)
        result = await client.update_ad_item_status(item_id, status, campaign_id)
        return JSONResponse({"ok": True, "result": result})
    except MeliApiError as e:
        is_cert = e.status_code == 401
        return JSONResponse({
            "detail": str(e),
            "requires_certification": is_cert
        }, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/ads/campaigns-list")
async def get_campaigns_list():
    """Lista todas las campanas con ID y nombre (sin metricas, rapido)."""
    from app.services.meli_client import MeliApiError
    from datetime import date, timedelta
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        data = await client.get_ads_campaigns(date_from=week_ago, date_to=today)
        results = data.get("results", [])
        campaigns = [
            {"id": c.get("id"), "name": c.get("name"), "status": c.get("status")}
            for c in results
        ]
        return JSONResponse({"campaigns": campaigns})
    except MeliApiError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/ads/debug-assign/{item_id}/{campaign_id}")
async def debug_ads_assign(item_id: str, campaign_id: str):
    """Prueba TODOS los patrones de endpoint para asignar item a campaña. Retorna raw MeLi responses."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    try:
        token = client.access_token
        adv_id = await client._get_advertiser_id()
        cid = int(campaign_id)
        results = {}

        async def try_req(label, method, url, **kwargs):
            try:
                r = await client._client.request(method, url, **kwargs)
                try:
                    body = r.json()
                except Exception:
                    body = r.text[:300]
                results[label] = {"status": r.status_code, "body": body}
            except Exception as ex:
                results[label] = {"status": "error", "body": str(ex)}

        h2 = {"api-version": "2"}
        h1 = {"Api-Version": "1"}

        await try_req("PUT_marketplace_v2",
            "PUT", f"https://api.mercadolibre.com/marketplace/advertising/MLM/product_ads/ads/{item_id}",
            params={"channel": "marketplace"}, json={"status": "active", "campaign_id": cid}, headers=h2)

        await try_req("PUT_advertising_v2",
            "PUT", f"https://api.mercadolibre.com/advertising/product_ads/ads/{item_id}",
            json={"status": "active", "campaign_id": cid}, headers=h2)

        await try_req("PUT_with_advertiser",
            "PUT", f"https://api.mercadolibre.com/advertising/MLM/advertisers/{adv_id}/product_ads/ads/{item_id}",
            json={"status": "active", "campaign_id": cid}, headers=h2)

        await try_req("POST_campaign_ads",
            "POST", f"https://api.mercadolibre.com/advertising/product_ads_2/campaigns/{campaign_id}/ads",
            json={"item_id": item_id, "status": "active"}, headers=h2)

        await try_req("POST_ads_direct",
            "POST", f"https://api.mercadolibre.com/advertising/product_ads/ads",
            json={"item_id": item_id, "campaign_id": cid, "status": "active"}, headers=h2)

        await try_req("PUT_bulk_advertiser",
            "PUT", f"https://api.mercadolibre.com/marketplace/advertising/MLM/advertisers/{adv_id}/product_ads/ads",
            params={"channel": "marketplace"},
            json={"target": [item_id], "payload": {"status": "active", "campaign_id": cid}}, headers=h2)

        await try_req("PUT_v1",
            "PUT", f"https://api.mercadolibre.com/advertising/advertisers/{adv_id}/product_ads/items/{item_id}",
            json={"status": "active", "campaign_id": cid}, headers=h1)

        working = {k: v for k, v in results.items() if v.get("status") not in (401, 403, "error")}
        return JSONResponse({"advertiser_id": adv_id, "item": item_id, "campaign": campaign_id,
                             "working_patterns": working, "all_results": results})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/api/token-refresh")
async def get_refresh_token():
    """Muestra el refresh token actual para configurar Railway Variables."""
    tokens = await token_store.get_any_tokens()
    if not tokens:
        return JSONResponse({"error": "No autenticado — ve primero a /auth/connect"}, status_code=401)
    rt = tokens.get("refresh_token", "")
    return JSONResponse({
        "refresh_token": rt,
        "user_id": tokens.get("user_id"),
        "instrucciones": "Copia el refresh_token y agrégalo como variable MELI_REFRESH_TOKEN en Railway Variables panel"
    })


@app.get("/api/ads/check-write-permission")
async def check_ads_write_permission():
    """Verifica certification_status de la app y si tiene permiso de escritura en Product Ads."""
    from app.services.meli_client import MeliApiError
    from app.config import MELI_CLIENT_ID as _app_id
    client = await get_meli_client()
    if not client:
        return JSONResponse({"write_enabled": False, "error": "not_authenticated"}, status_code=401)
    try:
        app_id = _app_id
        # 1. Verificar certification_status de la app
        cert_status = "unknown"
        try:
            app_info = await client._request_raw("GET", f"/applications/{app_id}")
            cert_status = app_info.get("certification_status", "unknown")
        except Exception:
            pass

        # 2. Intentar el PUT de todas formas para comprobar si ya funciona
        try:
            await client._request_raw(
                "PUT",
                "/marketplace/advertising/MLM/product_ads/ads/MLM1346239567",
                extra_headers={"api-version": "2"},
                json={"status": "idle", "campaign_id": 0},
            )
            return JSONResponse({"write_enabled": True, "certification_status": cert_status})
        except MeliApiError as e:
            if e.status_code == 401:
                return JSONResponse({
                    "write_enabled": False,
                    "error": "not_certified",
                    "certification_status": cert_status,
                    "detail": "La app requiere certificacion MeLi para escribir en Product Ads"
                })
            # Cualquier otro error != 401 significa que el write ya funciona
            return JSONResponse({"write_enabled": True, "certification_status": cert_status})
    except Exception as e:
        return JSONResponse({"write_enabled": False, "error": str(e)}, status_code=500)
    finally:
        await client.close()


@app.post("/api/ads/campaigns/{campaign_id}")
async def update_campaign_api(request: Request, campaign_id: str):
    """Actualiza una campaña de Product Ads (status, budget, acos_target)."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        body = await request.json()
        result = await client.update_campaign(
            campaign_id,
            status=body.get("status"),
            budget=body.get("budget"),
            acos_target=body.get("acos_target"),
        )
        return {"ok": True, "result": result}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.close()


@app.post("/api/ads/campaigns")
async def create_campaign_api(request: Request):
    """Crea una nueva campaña de Product Ads."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        body = await request.json()
        name = body.get("name", "")
        budget = body.get("budget", 0)
        if not name or not budget:
            return {"error": "name y budget son requeridos"}
        result = await client.create_campaign(
            name=name,
            budget=float(budget),
            acos_target=body.get("acos_target"),
            status=body.get("status", "active"),
        )
        return {"ok": True, "result": result}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.close()


# Cache para ads-by-category: key -> (timestamp, data)
_ads_category_cache: dict[str, tuple[float, list]] = {}
_ADS_CATEGORY_CACHE_TTL = 1800  # 30 minutos


@app.post("/api/ads/campaigns-with-items")
async def create_campaign_with_items_api(request: Request):
    """Crea campaña y asigna items TOP en un solo flujo."""
    from app.services.meli_client import MeliApiError
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        name = body.get("name", "")
        budget = body.get("budget", 0)
        acos_target = body.get("acos_target")
        item_ids = body.get("item_ids", [])
        if not name or not budget:
            return JSONResponse({"detail": "name y budget son requeridos"}, status_code=400)
        # 1. Crear campaña
        campaign = await client.create_campaign(
            name=name,
            budget=float(budget),
            acos_target=acos_target,
            status="active",
        )
        campaign_id = campaign.get("id", campaign.get("campaign_id"))
        assigned_count = 0
        errors = []
        # 2. Asignar items en lotes de 50
        if item_ids and campaign_id:
            for i in range(0, len(item_ids), 50):
                batch = item_ids[i:i+50]
                try:
                    result = await client.assign_items_to_campaign(batch, int(campaign_id))
                    assigned_count += len(result.get("results", []))
                    errors.extend(result.get("errors", []))
                except Exception as e:
                    errors.append({"batch_start": i, "error": str(e)})
        return JSONResponse({
            "ok": True,
            "campaign_id": campaign_id,
            "campaign_name": name,
            "assigned_count": assigned_count,
            "errors": errors,
        })
    except MeliApiError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        await client.close()


@app.get("/partials/ads-by-category", response_class=HTMLResponse)
async def ads_by_category_partial(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
):
    """Agrupacion de metricas de ads por categoria de producto."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p class='p-6 text-center text-gray-500'>No autenticado</p>")
    try:
        import time as _time_mod
        date_from, date_to = _default_dates(date_from, date_to)
        cache_key = f"ads_cat:{date_from}:{date_to}"
        cached = _ads_category_cache.get(cache_key)
        if cached and (_time_mod.time() - cached[0]) < _ADS_CATEGORY_CACHE_TTL:
            categories = cached[1]
            return templates.TemplateResponse(request, "partials/ads_by_category.html", {                "categories": categories,
                "from_cache": True,
            })

        # 1. Obtener todos los items con metricas (paginado)
        adv_id = await client._get_advertiser_id()
        all_items = []
        offset = 0
        limit = 100
        while True:
            params = {
                "metrics": "clicks,prints,cost,units_quantity,total_amount",
                "limit": limit,
                "offset": offset,
            }
            if date_from:
                params["date_from"] = date_from
            if date_to:
                params["date_to"] = date_to
            try:
                data = await client.get(
                    f"/advertising/advertisers/{adv_id}/product_ads/items",
                    params=params,
                )
            except Exception:
                break
            results = data.get("results", data if isinstance(data, list) else [])
            if not results:
                break
            all_items.extend(results)
            if len(results) < limit:
                break
            offset += limit

        # 2. Extraer item_ids únicos con métricas
        item_id_to_metrics = {}
        for item in all_items:
            iid = item.get("item_id", item.get("id", ""))
            if not iid:
                continue
            metrics = item.get("metrics", item)
            item_id_to_metrics[iid] = {
                "cost": metrics.get("cost", 0) or 0,
                "clicks": metrics.get("clicks", 0) or 0,
                "units": metrics.get("units_quantity", 0) or 0,
                "revenue": metrics.get("total_amount", 0) or 0,
                "title": item.get("title", ""),
            }

        # 3. Fetch category_id via batch (grupos de 20)
        item_ids = list(item_id_to_metrics.keys())
        item_cat_map = {}  # item_id -> category_id
        item_title_map = {}  # item_id -> title (from MeLi item data)
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i+20]
            try:
                details = await client.get_items_details(batch)
                for d in details:
                    body = d.get("body", d)
                    if not body:
                        continue
                    iid = body.get("id", "")
                    if iid:
                        item_cat_map[iid] = body.get("category_id", "")
                        item_title_map[iid] = body.get("title", item_id_to_metrics.get(iid, {}).get("title", ""))
            except Exception:
                pass

        # 4. Lookup category names via cache
        missing_cats = {cid for cid in item_cat_map.values() if cid and cid not in _category_cache}
        for cid in missing_cats:
            try:
                cat = await client.get(f"/categories/{cid}")
                _category_cache[cid] = cat.get("name", cid)
            except Exception:
                _category_cache[cid] = cid

        # 5. Agrupar por category_id
        cat_groups: dict[str, dict] = {}
        for iid, met in item_id_to_metrics.items():
            cat_id = item_cat_map.get(iid, "")
            cat_name = _category_cache.get(cat_id, cat_id) if cat_id else "Sin categoría"
            key = cat_id or "unknown"
            if key not in cat_groups:
                cat_groups[key] = {
                    "category_id": cat_id,
                    "category_name": cat_name,
                    "count": 0,
                    "cost": 0.0,
                    "revenue": 0.0,
                    "units": 0,
                    "clicks": 0,
                    "best_title": "",
                    "best_revenue": 0.0,
                }
            g = cat_groups[key]
            g["count"] += 1
            g["cost"] += met["cost"]
            g["revenue"] += met["revenue"]
            g["units"] += met["units"]
            g["clicks"] += met["clicks"]
            title = item_title_map.get(iid, met.get("title", ""))
            if met["revenue"] > g["best_revenue"]:
                g["best_revenue"] = met["revenue"]
                g["best_title"] = title

        # 6. Calcular ROAS, ACOS, CTR
        categories = []
        for g in cat_groups.values():
            cost = g["cost"]
            revenue = g["revenue"]
            g["roas"] = round(revenue / cost, 2) if cost > 0 else 0.0
            g["acos"] = round(cost / revenue * 100, 1) if revenue > 0 else 0.0
            categories.append(g)

        # Ordenar por ingresos desc
        categories.sort(key=lambda x: x["revenue"], reverse=True)

        # Guardar en cache
        _ads_category_cache[cache_key] = (_time_mod.time(), categories)

        return templates.TemplateResponse(request, "partials/ads_by_category.html", {            "categories": categories,
            "from_cache": False,
        })
    finally:
        await client.close()


# === Promotions API ===

@app.get("/api/items/{item_id}/promotions")
async def get_item_promotions_api(item_id: str):
    """Consulta promociones disponibles para un item."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        promos = await client.get_item_promotions(item_id)
        import logging
        logging.getLogger("api").info(f"promotions({item_id}): {promos}")
        return {"promotions": promos, "error": None}
    except Exception as e:
        import logging
        logging.getLogger("api").warning(f"promotions({item_id}) error: {e}")
        return {"promotions": [], "error": str(e)}
    finally:
        await client.close()


@app.post("/api/items/{item_id}/promotions/activate")
async def activate_item_promotion_api(item_id: str, request: Request):
    """Activa una promocion para un item."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        deal_price = body.get("deal_price")
        promotion_type = body.get("promotion_type")
        if not deal_price or not promotion_type:
            return JSONResponse({"detail": "deal_price y promotion_type requeridos"}, status_code=400)
        kwargs = {}
        if body.get("start_date"):
            kwargs["start_date"] = body["start_date"]
        if body.get("finish_date"):
            kwargs["finish_date"] = body["finish_date"]
        if body.get("original_price"):
            kwargs["original_price"] = float(body["original_price"])
        if body.get("promotion_id"):
            kwargs["promotion_id"] = body["promotion_id"]
        if body.get("is_modification"):
            kwargs["is_modification"] = True
        result = await client.activate_item_promotion(
            item_id, float(deal_price), promotion_type, **kwargs
        )
        return {"ok": True, "result": result}
    except Exception as e:
        import logging, json as _json
        error_body = getattr(e, "body", None)
        logging.getLogger("api").warning(f"activate_promotion({item_id}) error: {e} body={error_body}")
        # Build detailed error message from MeLi response
        detail = str(e)
        if isinstance(error_body, dict):
            parts = []
            if error_body.get("error"):
                parts.append(error_body["error"])
            if error_body.get("message"):
                parts.append(error_body["message"])
            cause = error_body.get("cause", [])
            if isinstance(cause, list):
                for c in cause[:5]:
                    if isinstance(c, dict):
                        parts.append(c.get("message") or c.get("code") or str(c))
                    else:
                        parts.append(str(c))
            detail = " | ".join(parts) if parts else detail
        elif isinstance(error_body, str) and error_body:
            detail = error_body
        return JSONResponse({"ok": False, "detail": detail, "meli_body": error_body}, status_code=400)
    finally:
        await client.close()


@app.delete("/api/items/{item_id}/promotions/{promotion_type}")
async def delete_item_promotion_api(item_id: str, promotion_type: str):
    """Desactiva una promocion de un item."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        await client.delete_item_promotion(item_id, promotion_type)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    finally:
        await client.close()


@app.put("/api/items/{item_id}/stock")
async def update_item_stock_api(item_id: str, request: Request):
    """Actualiza el stock de un item SIN variaciones.
    Para items con variaciones, usa /api/items/{item_id}/sync-variation-stocks."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        quantity = int(body.get("quantity", 0))
        # Protección BM caído: bloquear qty=0 cuando BM está confirmado down.
        # Evita que un trabajador ponga listings en 0 basándose en alertas falsas
        # generadas por datos stale de BM. qty>0 siempre se permite (no hay riesgo).
        if quantity == 0 and _bm_is_confirmed_down():
            return JSONResponse({
                "ok": False,
                "bm_down": True,
                "detail": "BinManager está caído. No se puede poner en 0 hasta que BM responda — las alertas pueden ser incorrectas.",
            }, status_code=503)
        result = await client.update_item_stock(item_id, quantity)
        # Invalidar cache en memoria y actualizar DB para reflejar nuevo stock
        uid = str(client.user_id)
        for k in [k for k in _products_cache if k.startswith(f"{uid}:")]:
            del _products_cache[k]
        from app.services.token_store import update_ml_listing_qty as _update_qty
        asyncio.create_task(_update_qty(item_id, quantity))
        # Fase C: si el listing quedó pausado por out_of_stock, reactivarlo
        asyncio.create_task(_reactivate_if_oos_bg(item_id, uid))
        return {"ok": True, "quantity": quantity}
    except ValueError as e:
        # Item tiene variaciones — rechazar con mensaje claro
        return JSONResponse({"ok": False, "has_variations": True, "detail": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    finally:
        await client.close()


@app.post("/api/items/{item_id}/sync-variation-stocks")
async def sync_variation_stocks_api(item_id: str, request: Request):
    """Sincroniza stock de CADA variacion de un item multi-variacion con su propio SKU en BinManager.

    Para cada variacion:
    1. Obtiene su SKU (SELLER_SKU o seller_custom_field)
    2. Consulta BinManager con ese SKU base
    3. Actualiza SOLO ESA variacion con floor(bm_stock * pct)
       No toca las demas variaciones.

    Body (optional): { "pct": 1.0 }  — porcentaje del stock BM DISPONIBLE a usar (default 100%)
    Returns: { ok, item_id, results: [{variation_id, sku, combo, bm_total, bm_avail, meli_qty, updated}] }
    """
    import httpx
    BM_WH_URL = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/Get_GlobalStock_InventoryBySKU_Warehouse"
    BM_AVAIL_URL_SYNC = "https://binmanager.mitechnologiesinc.com/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"

    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        pct = float(body.get("pct", 1.0))
        pct = max(0.0, min(1.0, pct))
        dry_run = bool(body.get("dry_run", False))  # Si True: consulta BM pero NO actualiza MeLi

        # 1. Obtener variaciones con SKUs
        # El batch fetch (GET /items?ids=) no devuelve attributes por variacion.
        # GET /items/{id}/variations/{var_id} SI devuelve seller_custom_field real.
        client_vars = body.get("variations", [])  # [{id, sku, stock, combo}, ...] para combos
        client_var_map = {str(cv.get("id")): cv for cv in client_vars}

        # Fetch item individual para obtener variation IDs y attribute_combinations
        item = await client.get(f"/items/{item_id}")
        raw_vars_base = item.get("variations", [])
        if not raw_vars_base:
            return JSONResponse({"ok": False, "detail": "El item no tiene variaciones"}, status_code=400)

        # Fetch detalle de cada variacion via /items/{id}/variations/{var_id}
        # Este endpoint devuelve seller_custom_field por variacion (el batch no lo hace)
        access_token = client.access_token

        async def _fetch_variation_detail(var_id, http_client):
            try:
                resp = await http_client.get(
                    f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
            return None

        async with httpx.AsyncClient() as http_pre:
            var_details = await asyncio.gather(
                *[_fetch_variation_detail(v.get("id"), http_pre) for v in raw_vars_base]
            )

        # Construir raw_vars enriquecidas: SKU real del detail + combo/stock del cliente
        raw_vars = []
        for base_v, detail in zip(raw_vars_base, var_details):
            merged = dict(base_v)
            if detail:
                scf = detail.get("seller_custom_field")
                if scf and scf != "None":
                    merged["seller_custom_field"] = scf
                if detail.get("attributes"):
                    merged["attributes"] = detail["attributes"]
            cv = client_var_map.get(str(base_v.get("id")), {})
            if cv.get("combo"):
                merged["_combo_override"] = cv["combo"]
            raw_vars.append(merged)

        # 2. Pre-construir mapas desde caché BM (evita HTTP calls por variación)
        import re as _re_var
        # Fix 2: mapas separados por condición — GR (no-SNTV) y ALL (SNTV con ICB/ICC).
        # Antes: _var_bulk = _bm_bulk_gr_cache or _bm_bulk_all_cache → siempre GR aunque
        # el SKU sea SNTV, causando cache miss → HTTP fallback con conditions incorrectas.
        _bulk_avail_map_gr:  dict = {}   # base_sku -> AvailableQTY (GR bulk)
        _bulk_avail_map_all: dict = {}   # base_sku -> AvailableQTY (ALL bulk, ICB/ICC)
        _bulk_avail_map: dict = {}       # legacy alias — apunta a GR; se usa solo si ALL no aplica
        _wh_cache_map: dict = {}         # base_sku -> {mty, cdmx, total} (desde per-SKU cache)

        def _build_bulk_map(bulk_tuple):
            m: dict = {}
            if bulk_tuple and (_time.time() - bulk_tuple[0]) < _BM_CACHE_TTL:
                for _br in bulk_tuple[1]:
                    _bsk = (_br.get("SKU") or "").upper().strip()
                    if not _bsk:
                        continue
                    _bb = _extract_base_sku(_bsk)
                    if _bb not in m:
                        m[_bb] = int(_br.get("AvailableQTY") or 0)
            return m

        if _bm_bulk_gr_cache:
            _bulk_avail_map_gr = _build_bulk_map(_bm_bulk_gr_cache)
        if _bm_bulk_all_cache:
            _bulk_avail_map_all = _build_bulk_map(_bm_bulk_all_cache)
        _bulk_avail_map = _bulk_avail_map_gr  # legacy alias para código existente
        for _bkey, (_bts, _bcdata) in list(_bm_stock_cache.items()):
            if (_time.time() - _bts) < _BM_CACHE_TTL:
                _wh_cache_map[_bkey.upper()] = {
                    "mty": _bcdata.get("mty", 0),
                    "cdmx": _bcdata.get("cdmx", 0),
                    "total": _bcdata.get("total", 0),
                }

        # 2b. Para cada variacion: obtener SKU y consultar BM (cache-first)
        async def _fetch_var_bm(v: dict, http: httpx.AsyncClient):
            v_sku = _get_var_sku(v)
            if v.get("_combo_override"):
                combo_str = v["_combo_override"]
            else:
                combos = []
                for ac in v.get("attribute_combinations", []):
                    combos.append(f"{ac.get('name','')}: {ac.get('value_name','')}")
                combo_str = ", ".join(combos) if combos else f"Var {v.get('id','')}"
            result = {
                "variation_id": v.get("id"),
                "sku": v_sku,
                "combo": combo_str,
                "meli_stock": v.get("available_quantity", 0),
                "bm_mty": 0,
                "bm_cdmx": 0,
                "bm_total": 0,
                "bm_avail": 0,
                "meli_qty": 0,
                "updated": False,
                "error": None,
            }
            if not v_sku:
                result["error"] = "Sin SKU en variacion"
                return result
            # Para bundles (A / B o A + B): extraer todos los SKUs componentes
            raw_parts = _re_var.split(r'\s*[/+]\s*', v_sku)
            sku_parts = []
            for part in raw_parts:
                base = _extract_base_sku(part.strip())
                clean = _clean_sku_for_bm(base)
                if clean:
                    sku_parts.append(clean)
            if not sku_parts:
                result["error"] = "SKU no mapeable a BM"
                return result

            primary_sku = sku_parts[0]
            primary_base = _extract_base_sku(primary_sku).upper()

            # --- Intentar resolver desde caché (sin HTTP) ---
            # Fix 2: usar bulk map correcto según condiciones del SKU.
            # SNTV→ _bulk_avail_map_all (incluye ICB/ICC); resto → _bulk_avail_map_gr.
            _conds_v = _bm_conditions_for_sku(v_sku)
            _BM_COND_ALL_V = "GRA,GRB,GRC,ICB,ICC,NEW"
            _active_bulk_map = _bulk_avail_map_all if _conds_v == _BM_COND_ALL_V else _bulk_avail_map_gr
            avails_from_cache = []
            all_in_cache = True
            for sp in sku_parts:
                sp_base = _extract_base_sku(sp).upper()
                if sp_base in _active_bulk_map:
                    avails_from_cache.append(_active_bulk_map[sp_base])
                else:
                    all_in_cache = False
                    break

            if all_in_cache and avails_from_cache:
                result["bm_avail"] = min(avails_from_cache)  # bottleneck para bundles
                wh = _wh_cache_map.get(normalize_to_bm_sku(primary_sku).upper())
                if wh:
                    result["bm_mty"]   = wh["mty"]
                    result["bm_cdmx"]  = wh["cdmx"]
                    result["bm_total"] = wh["total"]
                return result

            # --- Fallback: HTTP a BM (si caché vacío o no encontrado) ---
            # Fix 1: pasar conditions por SKU — sin esto, SKUs SNTV con stock ICB/ICC
            # reciben conditions=GRA,GRB,GRC,NEW y BM devuelve 0 (no encuentra GR stock).
            async def _query_bm_avail(sku: str, conds: str = "GRA,GRB,GRC,NEW") -> int:
                from app.services.binmanager_client import get_shared_bm
                try:
                    bm = await get_shared_bm()
                    return await bm.get_available_qty(sku, conditions=conds)
                except Exception:
                    return -1

            try:
                # Usar el SKU completo de la variación para determinar condiciones
                # (ej: "SNTV003807 / SNWM000001" → ALL, no solo primary_sku → GR)
                conditions_primary = _bm_conditions_for_sku(v_sku)
                avail_tasks = [_query_bm_avail(s, conds=conditions_primary) for s in sku_parts]
                r_wh, *avail_results = await asyncio.gather(
                    http.post(BM_WH_URL, json={
                        "COMPANYID": 1, "SKU": primary_sku, "WarehouseID": None,
                        "LocationID": "47,62,68", "BINID": None,
                        "Condition": conditions_primary, "ForInventory": 0, "SUPPLIERS": None,
                    }, headers={"Content-Type": "application/json"}, timeout=15.0),
                    *avail_tasks,
                    return_exceptions=True,
                )
                if not isinstance(r_wh, Exception) and r_wh.status_code == 200:
                    rows = r_wh.json()
                    if isinstance(rows, dict): rows = [rows]
                    if not isinstance(rows, list): rows = []
                    mty = cdmx = 0
                    for row in rows:
                        qty = row.get("QtyTotal", 0) or 0
                        wname = (row.get("WarehouseName") or "").lower()
                        if "monterrey" in wname or "maxx" in wname:
                            mty += qty
                        elif "autobot" in wname or "cdmx" in wname or "ebanistas" in wname:
                            cdmx += qty
                    result["bm_mty"] = mty
                    result["bm_cdmx"] = cdmx
                    result["bm_total"] = mty + cdmx
                valid_avails = [a for a in avail_results if isinstance(a, int) and a >= 0]
                # Fix 3: si algún componente del bundle falló (-1), no podemos saber
                # su stock real. Usar 0 (seguro) en lugar de min(válidos) que ignoraría
                # el componente fallido y daría stock incorrecto al bundle.
                _has_errors = any(isinstance(a, int) and a == -1 for a in avail_results)
                if _has_errors:
                    result["bm_avail"] = 0
                    result["error"] = "BM no respondió para uno de los componentes"
                elif valid_avails:
                    result["bm_avail"] = min(valid_avails)
            except Exception as ex:
                result["error"] = f"BM error: {ex}"
            return result

        async with httpx.AsyncClient() as http:
            var_results = await asyncio.gather(*[_fetch_var_bm(v, http) for v in raw_vars])

        # 3. Dividir stock entre variaciones del mismo SKU base para evitar sobreventa
        # Si var1 y var2 comparten SNTV001864 con BM=244 → cada una recibe 122, no 244
        from collections import defaultdict as _defdict
        _sku_base_groups: dict = _defdict(list)
        for _rv in var_results:
            if not _rv["error"]:
                _rv_parts = _re_var.split(r'\s*[/+]\s*', _rv.get("sku") or "")
                _rv_base = normalize_to_bm_sku(_rv_parts[0].strip()) if _rv_parts else "__none__"
                _sku_base_groups[_rv_base].append(_rv)

        var_updates = []
        for r in var_results:
            if r["error"]:
                r["meli_qty"] = 0
                continue
            _parts = _re_var.split(r'\s*[/+]\s*', r.get("sku") or "")
            _base = normalize_to_bm_sku(_parts[0].strip()) if _parts else "__none__"
            _n_sharing = len(_sku_base_groups.get(_base, [r]))
            qty = int((r["bm_avail"] / _n_sharing) * pct)
            r["meli_qty"] = qty
            r["split_n"] = _n_sharing  # info para UI
            var_updates.append({"id": r["variation_id"], "available_quantity": qty})

        if var_updates and not dry_run:
            try:
                await client.update_variation_stocks_directly(item_id, var_updates)
                # Marcar como actualizadas
                updated_ids = {u["id"] for u in var_updates}
                for r in var_results:
                    if r["variation_id"] in updated_ids:
                        r["updated"] = True
                # Invalidar cache de stock issues y productos para reflejar cambio
                _synced_alert_items[item_id] = _time.time()
                _stock_issues_cache.clear()
                uid = str(client.user_id)
                for k in [k for k in _products_cache if k.startswith(f"{uid}:")]:
                    del _products_cache[k]
                # Actualizar DB con qty total (suma de variaciones actualizadas)
                _new_total_qty = sum(u["available_quantity"] for u in var_updates)
                from app.services.token_store import update_ml_listing_qty as _update_qty
                asyncio.create_task(_update_qty(item_id, _new_total_qty))
                # Fase C: si el listing quedó pausado por out_of_stock, reactivarlo
                asyncio.create_task(_reactivate_if_oos_bg(item_id, str(client.user_id)))
            except Exception as ex:
                for r in var_results:
                    if not r["error"]:
                        r["error"] = f"MeLi update error: {ex}"

        return JSONResponse({
            "ok": True,
            "item_id": item_id,
            "pct": pct,
            "dry_run": dry_run,
            "results": list(var_results),
            "updated_count": sum(1 for r in var_results if r["updated"]),
        })
    finally:
        await client.close()


@app.get("/api/items/{item_id}/debug-variations")
async def debug_item_variations(item_id: str, request: Request):
    """Debug: retorna estructura cruda de variaciones (batch vs individual) para diagnosticar SKU lookup."""
    import httpx as _httpx
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        item = await client.get(f"/items/{item_id}")
        variations = item.get("variations", [])
        debug = []
        async with _httpx.AsyncClient() as http:
            for v in variations:
                var_id = v.get("id")
                # Fetch individual
                individual = None
                try:
                    r = await http.get(
                        f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}",
                        headers={"Authorization": f"Bearer {client.access_token}"},
                        timeout=10.0,
                    )
                    if r.status_code == 200:
                        individual = r.json()
                except Exception as ex:
                    individual = {"error": str(ex)}

                debug.append({
                    "id": var_id,
                    "from_batch": {
                        "available_quantity": v.get("available_quantity"),
                        "seller_custom_field": v.get("seller_custom_field"),
                        "attribute_combinations": v.get("attribute_combinations", []),
                        "attributes": v.get("attributes", []),
                        "_get_var_sku_result": _get_var_sku(v),
                    },
                    "from_individual": {
                        "seller_custom_field": individual.get("seller_custom_field") if individual else None,
                        "attributes": individual.get("attributes", []) if individual else [],
                        "_get_var_sku_result": _get_var_sku(individual) if individual and "id" in individual else "N/A",
                    } if individual else "fetch failed",
                })
        return JSONResponse({"item_id": item_id, "variation_count": len(variations), "variations": debug})
    finally:
        await client.close()


@app.put("/api/items/{item_id}/status")
async def update_item_status_api(item_id: str, request: Request):
    """Cambia el estado de un item (active/paused)."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = await request.json()
        status = body.get("status", "active")
        if status not in ("active", "paused"):
            return JSONResponse({"detail": "Status invalido"}, status_code=400)
        result = await client.update_item_status(item_id, status)
        return {"ok": True, "status": status}
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    finally:
        await client.close()


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INVENTORY CENTER — BinManager × 4 cuentas MeLi
# ═══════════════════════════════════════════════════════════════════════════

_inventory_global_cache: dict[str, tuple[float, dict]] = {}
_INVENTORY_GLOBAL_TTL = 900  # 15 min

# Estado del scan global — persiste entre requests HTTP
_scan_state: dict = {
    "status": "idle",   # idle | running | done | error
    "pct": 0,
    "label": "",
    "detail": "",
    "result": None,
    "error": None,
    "threshold": 10,
}
_scan_bg_task: asyncio.Task | None = None


@app.get("/inventory-global", response_class=HTMLResponse)
async def inventory_global_page(request: Request):
    """Centro de inventario global: BM × 4 cuentas MeLi."""
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "inventory_global.html", {        "user": user,
        "active": "inventory_global",
        **ctx
    })


@app.get("/api/inventory/global-scan")
async def inventory_global_scan(threshold: int = 10):
    """Versión no-streaming (para uso de API). Usa la misma cache que el stream."""
    cache_key = f"inv_global:{threshold}"
    cached = _inventory_global_cache.get(cache_key)
    if cached and (_time_module.time() - cached[0]) < _INVENTORY_GLOBAL_TTL:
        return cached[1]
    return {"rows": [], "account_nicknames": {}, "total": 0, "pending": True,
            "message": "Usa /api/inventory/global-scan-stream para el escaneo completo"}


async def _run_global_scan(threshold: int):
    """Background task: corre independientemente de cualquier request HTTP."""
    global _scan_state
    try:
        _scan_state.update({"status": "running", "pct": 1, "label": "Conectando...", "detail": ""})

        # Cache hit
        cache_key = f"inv_global:{threshold}"
        cached = _inventory_global_cache.get(cache_key)
        if cached and (_time_module.time() - cached[0]) < _INVENTORY_GLOBAL_TTL:
            _scan_state.update({"status": "done", "pct": 100, "label": "Resultado desde caché", "detail": "", "result": cached[1]})
            return

        accounts_list = await token_store.get_all_tokens()
        if not accounts_list:
            _scan_state.update({"status": "error", "error": "No hay cuentas autenticadas", "label": "Error: No hay cuentas autenticadas"})
            return

        now = datetime.utcnow()
        date_from_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        _scan_state.update({"pct": 3, "label": f"Cargando productos de {len(accounts_list)} cuentas...", "detail": "Puede tardar 1-2 min en primera ejecución"})

        async def _fetch_one(account):
            uid = account["user_id"]
            nickname = account.get("nickname") or uid
            try:
                client = await get_meli_client(user_id=uid)
                all_bodies, all_orders = await asyncio.gather(
                    _get_all_products_cached(client, include_all=False),
                    _get_orders_cached(client, date_from_30d, date_to),
                )
                await client.close()
                sales_map = _aggregate_sales_by_item(all_orders)
                products = _build_product_list(all_bodies, sales_map)
                _enrich_sku_from_orders(products, all_orders)
                return {"uid": uid, "nickname": nickname, "products": products, "count": len(products)}
            except Exception as e:
                return {"uid": uid, "nickname": nickname, "products": [], "count": 0, "error": str(e)}

        result_q: asyncio.Queue = asyncio.Queue()
        total_acc = len(accounts_list)

        async def _fetch_and_queue(account):
            r = await _fetch_one(account)
            await result_q.put(r)

        for a in accounts_list:
            asyncio.ensure_future(_fetch_and_queue(a))

        acc_results = []
        done = 0
        while done < total_acc:
            result = await result_q.get()
            acc_results.append(result)
            done += 1
            pct = 8 + int(done / total_acc * 45)
            err_suffix = " ⚠" if result.get("error") else ""
            _scan_state.update({
                "pct": pct,
                "label": f"✓ {result['nickname']}: {result['count']} productos ({done}/{total_acc}){err_suffix}",
                "detail": result.get("error", f"Cuentas procesadas: {done}/{total_acc}"),
            })

        _scan_state.update({"pct": 55, "label": "Agrupando SKUs únicos...", "detail": ""})

        sku_accounts: dict[str, dict] = {}
        sku_titles: dict[str, str] = {}
        for acc_result in acc_results:
            uid = acc_result["uid"]
            nickname = acc_result["nickname"]
            for p in acc_result["products"]:
                sku = p.get("sku") or ""
                if not sku:
                    continue
                base = normalize_to_bm_sku(sku)
                if not base:
                    continue
                if base not in sku_accounts:
                    sku_accounts[base] = {}
                    sku_titles[base] = p.get("title", "")
                sku_accounts[base][uid] = {
                    "listed": True,
                    "meli_stock": p.get("available_quantity", 0) or 0,
                    "sold_30d": p.get("units", 0) or 0,
                    "nickname": nickname,
                }

        unique_count = len(sku_accounts)
        account_nicknames = {a["user_id"]: a.get("nickname", a["user_id"]) for a in accounts_list}

        if not sku_accounts:
            result_data = {"rows": [], "account_nicknames": account_nicknames, "total": 0}
            _inventory_global_cache[cache_key] = (_time_module.time(), result_data)
            _scan_state.update({"status": "done", "pct": 100, "label": "Completado — 0 SKUs", "detail": "", "result": result_data})
            return

        _scan_state.update({"pct": 58, "label": f"{unique_count} SKUs únicos — consultando BinManager stock...", "detail": "Paso 2/3"})
        synthetic = [{"sku": base, "title": sku_titles.get(base, "")} for base in sku_accounts]

        # BM stock: await directo (sin keepalive — no hay HTTP en medio)
        bm_map = await _get_bm_stock_cached(synthetic)
        _apply_bm_stock(synthetic, bm_map)

        _scan_state.update({"pct": 72, "label": f"BinManager precios/marca ({unique_count} SKUs)...", "detail": "Paso 3/3"})
        CHUNK = 25
        chunks_total = max(1, (len(synthetic) + CHUNK - 1) // CHUNK)
        for i in range(0, len(synthetic), CHUNK):
            await _enrich_with_bm_product_info(synthetic[i:i + CHUNK])
            chunk_num = i // CHUNK + 1
            pct = 72 + int(chunk_num / chunks_total * 22)
            _scan_state.update({
                "pct": min(pct, 93),
                "label": f"BinManager metadata: {chunk_num}/{chunks_total} lotes",
                "detail": f"{min(i + CHUNK, len(synthetic))}/{len(synthetic)} SKUs",
            })

        _scan_state.update({"pct": 96, "label": "Construyendo tabla final...", "detail": ""})

        rows = []
        for p_bm in synthetic:
            base = p_bm["sku"]
            rows.append({
                "sku": base,
                "title": p_bm.get("_bm_title") or p_bm.get("title") or sku_titles.get(base, ""),
                "brand": p_bm.get("_bm_brand", ""),
                "bm_avail": p_bm.get("_bm_avail", 0) or 0,
                "bm_reserved": p_bm.get("_bm_reserved", 0) or 0,
                "bm_total": p_bm.get("_bm_total", 0) or 0,
                "mty": p_bm.get("_bm_mty", 0) or 0,
                "cdmx": p_bm.get("_bm_cdmx", 0) or 0,
                "tj": p_bm.get("_bm_tj", 0) or 0,
                "retail_price_usd": p_bm.get("_bm_retail_price", 0) or 0,
                "accounts": sku_accounts.get(base, {}),
            })

        def _sort_key(r):
            avail = r["bm_avail"]
            if avail == 0:
                return (1, 0)
            if avail <= threshold:
                return (0, avail)
            return (2, avail)
        rows.sort(key=_sort_key)

        result_data = {"rows": rows, "account_nicknames": account_nicknames, "total": len(rows)}
        _inventory_global_cache[cache_key] = (_time_module.time(), result_data)
        _scan_state.update({"status": "done", "pct": 100, "label": f"Completado — {len(rows)} SKUs", "detail": "", "result": result_data})

    except Exception as e:
        _scan_state.update({"status": "error", "pct": 0, "error": str(e), "label": f"Error: {str(e)[:80]}", "detail": ""})


@app.post("/api/inventory/global-scan-start")
async def start_global_scan(threshold: int = 10):
    """Inicia el scan de inventario global como background task."""
    global _scan_bg_task, _scan_state
    if _scan_state["status"] == "running":
        return {"status": "already_running", "pct": _scan_state["pct"]}
    _scan_state = {
        "status": "running", "pct": 1, "label": "Iniciando...", "detail": "",
        "result": None, "error": None, "threshold": threshold,
    }
    _scan_bg_task = asyncio.create_task(_run_global_scan(threshold))
    return {"status": "started"}


@app.get("/api/inventory/global-scan-status")
async def get_global_scan_status():
    """Devuelve el estado actual del scan (para polling desde el browser)."""
    return _scan_state


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-ACCOUNT DASHBOARD — Vista General de todas las cuentas
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/multi-dashboard", response_class=HTMLResponse)
async def multi_dashboard_page(request: Request):
    """Vista general consolidada de todas las cuentas MeLi."""
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "multi_dashboard.html", {        "user": user,
        "active": "multi_dashboard",
        **ctx
    })


@app.get("/api/dashboard/multi-account")
async def get_multi_account_dashboard(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    """Dashboard consolidado: métricas de todas las cuentas en una sola respuesta.
    Cache de 5 minutos cross-account (independiente de la cuenta activa).
    """
    now = datetime.utcnow()
    # México CST = UTC-6 permanente (DST eliminado en 2022).
    # Sin este ajuste, "hoy" se corta a las 6 PM hora México porque UTC ya es el día siguiente.
    now_mx = now - timedelta(hours=6)
    if not date_from:
        date_from = now_mx.replace(day=1).strftime("%Y-%m-%d")
    if not date_to:
        date_to = now_mx.strftime("%Y-%m-%d")

    cache_key = f"multi_account:{date_from}:{date_to}"
    cached = _multi_account_cache.get(cache_key)
    if cached and (_time_module.time() - cached[0]) < _MULTI_ACCOUNT_CACHE_TTL:
        return cached[1]

    accounts_list = await token_store.get_all_tokens()
    today_str = now_mx.strftime("%Y-%m-%d")
    week_start_str = (now_mx - timedelta(days=6)).strftime("%Y-%m-%d")

    ACC_COLORS = {
        "523916436": "#3B82F6",  # APANTALLATEMX - azul
        "292395685": "#10B981",  # AUTOBOT - verde
        "391393176": "#8B5CF6",  # BLOWTECHNOLOGIES - morado
        "515061615": "#F97316",  # LUTEMAMEXICO - naranja
    }

    async def _fetch_account_data(account):
        uid = account["user_id"]
        nickname = account.get("nickname") or uid
        try:
            client = await get_meli_client(user_id=uid)
            # +1 día en date_to para que la API de ML devuelva órdenes de la noche
            # del día seleccionado en hora México (p.ej. 11 PM CST = 5 AM UTC del día siguiente).
            # El filtro _in_period usa hora MX, así que nada se cuenta de más.
            _fetch_date_to = (
                datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
            all_orders, items_data = await asyncio.gather(
                client.fetch_all_orders(date_from=date_from, date_to=_fetch_date_to),
                client.get_items(limit=1)
            )
            await client.close()

            def _order_mx_date(order) -> str:
                """Devuelve la fecha de la orden en hora México (CST UTC-6)."""
                od_utc = datetime.fromisoformat(
                    order["date_created"].replace("Z", "+00:00")
                ).replace(tzinfo=None)
                return (od_utc - timedelta(hours=6)).strftime("%Y-%m-%d")

            paid = [
                o for o in all_orders
                if o.get("status") in ("paid", "delivered")
                and date_from <= _order_mx_date(o) <= date_to
            ]

            def _in_period(order, start_str):
                try:
                    return _order_mx_date(order) >= start_str
                except Exception:
                    return False

            today_orders = [o for o in paid if _in_period(o, today_str)]
            week_orders = [o for o in paid if _in_period(o, week_start_str)]

            def _agg(orders):
                units = sum(
                    sum(oi.get("quantity", 1) for oi in o.get("order_items", []))
                    for o in orders
                )
                revenue = sum(order_net_revenue(o) for o in orders)
                return {"orders": len(orders), "units": units, "revenue": round(revenue, 2)}

            # Daily revenues para la gráfica comparativa (fecha en hora México)
            daily_revenues: dict[str, float] = {}
            for o in paid:
                try:
                    dk = _order_mx_date(o)
                    daily_revenues[dk] = daily_revenues.get(dk, 0) + order_net_revenue(o)
                except Exception:
                    pass

            # Items más vendidos (agrupados por item_id)
            items_sales: dict[str, dict] = {}
            for o in paid:
                for oi in o.get("order_items", []):
                    item = oi.get("item", {})
                    iid = item.get("id", "")
                    if not iid:
                        continue
                    title = item.get("title", "")
                    seller_sku = item.get("seller_sku", "")
                    qty = oi.get("quantity", 0)
                    rev = qty * oi.get("unit_price", 0)
                    if iid not in items_sales:
                        items_sales[iid] = {
                            "title": title,
                            "sku": seller_sku,
                            "units": 0,
                            "revenue": 0,
                            "user_id": uid,
                            "nickname": nickname,
                        }
                    items_sales[iid]["units"] += qty
                    items_sales[iid]["revenue"] += rev

            return {
                "user_id": uid,
                "nickname": nickname,
                "color": ACC_COLORS.get(uid, "#6B7280"),
                "today": _agg(today_orders),
                "week": _agg(week_orders),
                "month": _agg(paid),
                "active_items": items_data.get("paging", {}).get("total", 0),
                "items_sales": items_sales,
                "daily_revenues": daily_revenues,
                "error": None,
            }
        except Exception as e:
            return {
                "user_id": uid,
                "nickname": nickname,
                "color": ACC_COLORS.get(uid, "#6B7280"),
                "today": {"orders": 0, "units": 0, "revenue": 0},
                "week": {"orders": 0, "units": 0, "revenue": 0},
                "month": {"orders": 0, "units": 0, "revenue": 0},
                "active_items": 0,
                "items_sales": {},
                "daily_revenues": {},
                "error": str(e),
            }

    accounts_data = list(await asyncio.gather(*[_fetch_account_data(a) for a in accounts_list]))

    def _sum_period(period):
        return {
            "orders": sum(a[period]["orders"] for a in accounts_data),
            "units": sum(a[period]["units"] for a in accounts_data),
            "revenue": round(sum(a[period]["revenue"] for a in accounts_data), 2),
        }

    totals = {
        "today": _sum_period("today"),
        "week": _sum_period("week"),
        "month": _sum_period("month"),
        "active_items": sum(a["active_items"] for a in accounts_data),
    }

    # Guard: sin cuentas, devolver estructura vacía para no crashear
    if not accounts_data:
        return {"date_from": date_from, "date_to": date_to, "accounts": [],
                "totals": {"today": {"orders": 0, "units": 0, "revenue": 0},
                           "week": {"orders": 0, "units": 0, "revenue": 0},
                           "month": {"orders": 0, "units": 0, "revenue": 0},
                           "active_items": 0},
                "top_products": [], "leader_today": None, "leader_week": None, "leader_month": None}

    def _leader(period):
        best = max(accounts_data, key=lambda a: a[period]["revenue"])
        return {
            "user_id": best["user_id"],
            "nickname": best["nickname"],
            "revenue": best[period]["revenue"],
            "color": best["color"],
        }

    # Top productos cross-account
    global_items: dict[str, dict] = {}
    for acc in accounts_data:
        for iid, idata in acc.get("items_sales", {}).items():
            if iid not in global_items:
                global_items[iid] = {
                    "title": idata["title"],
                    "sku": idata["sku"],
                    "total_units": 0,
                    "total_revenue": 0,
                    "by_account": [],
                }
            global_items[iid]["total_units"] += idata["units"]
            global_items[iid]["total_revenue"] += idata["revenue"]
            if idata["units"] > 0:
                global_items[iid]["by_account"].append({
                    "user_id": acc["user_id"],
                    "nickname": acc["nickname"],
                    "color": acc["color"],
                    "units": idata["units"],
                })

    top_products = sorted(
        global_items.values(),
        key=lambda x: x["total_revenue"],  # Ordenar por ingreso neto (más relevante para negocio)
        reverse=True
    )[:15]
    for p in top_products:
        p["total_revenue"] = round(p["total_revenue"], 2)

    result = {
        "date_from": date_from,
        "date_to": date_to,
        "accounts": accounts_data,
        "totals": totals,
        "top_products": top_products,
        "leader_today": _leader("today"),
        "leader_week": _leader("week"),
        "leader_month": _leader("month"),
    }

    _multi_account_cache[cache_key] = (_time_module.time(), result)
    return result


@app.get("/api/dashboard/morning-briefing")
async def morning_briefing():
    """Resumen matutino de todas las cuentas: ventas hoy, alertas, estado."""
    # Usar hora México (CST UTC-6) para que la fecha del resumen sea correcta en el servidor UTC
    today = (datetime.utcnow() - timedelta(hours=6)).strftime("%Y-%m-%d")
    accounts_list = await token_store.get_all_tokens()
    all_alerts = await token_store.get_all_sync_alerts()

    # Count alerts per user
    alerts_by_user = {}
    for a in all_alerts:
        uid = a.get("user_id", "")
        alerts_by_user[uid] = alerts_by_user.get(uid, 0) + 1

    # Fetch daily_goal per account in parallel
    import asyncio as _asyncio
    goals = await _asyncio.gather(*[
        token_store.get_daily_goal(acc.get("user_id", ""))
        for acc in accounts_list
    ])
    total_goal = sum(goals)

    result = []
    for acc, goal in zip(accounts_list, goals):
        uid = acc.get("user_id", "")
        label = acc.get("label") or acc.get("nickname") or uid[:8]
        result.append({
            "user_id": uid,
            "label": label,
            "alert_count": alerts_by_user.get(uid, 0),
            "daily_goal": goal,
            "today_revenue": 0,  # loaded async by client
        })

    return {
        "accounts": result,
        "date": today,
        "total_alerts": len(all_alerts),
        "total_goal": total_goal,
    }


@app.get("/api/dashboard/multi-account-amazon")
async def get_multi_account_amazon_dashboard(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    """Dashboard consolidado de todas las cuentas Amazon configuradas."""
    from app.services.amazon_client import get_amazon_client as _get_amz_client

    now = datetime.utcnow()
    # México CST = UTC-6 permanente (DST eliminado en 2022).
    now_mx = now - timedelta(hours=6)
    if not date_from:
        date_from = now_mx.replace(day=1).strftime("%Y-%m-%d")
    if not date_to:
        date_to = now_mx.strftime("%Y-%m-%d")

    cache_key = f"multi_amazon:{date_from}:{date_to}"
    cached = _multi_account_cache.get(cache_key)
    if cached and (_time_module.time() - cached[0]) < _MULTI_ACCOUNT_CACHE_TTL:
        return cached[1]

    amazon_accounts_list = await token_store.get_all_amazon_accounts()
    today_str = now_mx.strftime("%Y-%m-%d")
    week_start_str = (now_mx - timedelta(days=6)).strftime("%Y-%m-%d")

    # Rango fijo de 29 días — IGUAL al default del Amazon dashboard
    # Así el multi-dashboard comparte el mismo cache key y NO hace llamadas extra a SP-API
    cache_date_from = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    cache_date_to   = now.strftime("%Y-%m-%d")

    async def _fetch_amz_data(account: dict) -> dict:
        # Sales API (OPS) = lo mismo que muestra Seller Central — más preciso que OrderTotal
        from app.api.metrics import _get_cached_order_metrics as _cached_metrics
        seller_id   = account["seller_id"]
        nickname    = account.get("nickname") or seller_id
        marketplace = account.get("marketplace_name", "MX")
        try:
            client = await _get_amz_client(seller_id=seller_id)
            if not client:
                raise ValueError("No client para seller_id=" + seller_id)

            # Amazon Sales API usa US/Pacific (PDT=UTC-7 mar-nov, PST=UTC-8 nov-mar)
            try:
                import zoneinfo as _zi
                _la = _zi.ZoneInfo("America/Los_Angeles")
                _offset_h = int(datetime.now(_la).utcoffset().total_seconds() // 3600)
            except Exception:
                _offset_h = -8
            now_pac     = now + timedelta(hours=_offset_h)
            today_pst   = now_pac.strftime("%Y-%m-%d")
            week_start  = (now_pac - timedelta(days=6)).strftime("%Y-%m-%d")
            month_start = now_pac.replace(day=1).strftime("%Y-%m-%d")

            metrics_data = await _cached_metrics(client, cache_date_from, cache_date_to)

            def _sum_period(min_date: str) -> dict:
                orders = units = 0
                revenue = 0.0
                for item in metrics_data:
                    # interval = "YYYY-MM-DDT00:00:00-08:00--..." → primeros 10 chars = fecha PST
                    dk = (item.get("interval") or "")[:10]
                    if dk and dk >= min_date:
                        orders += int(item.get("orderCount", 0) or 0)
                        units  += int(item.get("unitCount",  0) or 0)
                        try:
                            revenue += float((item.get("totalSales") or {}).get("amount", 0) or 0)
                        except (TypeError, ValueError):
                            pass
                return {"orders": orders, "units": units, "revenue": round(revenue, 2)}

            daily_revenues: dict[str, float] = {}
            for item in metrics_data:
                dk = (item.get("interval") or "")[:10]
                if dk:
                    try:
                        daily_revenues[dk] = daily_revenues.get(dk, 0.0) + float(
                            (item.get("totalSales") or {}).get("amount", 0) or 0
                        )
                    except (TypeError, ValueError):
                        pass

            return {
                "seller_id":      seller_id,
                "nickname":       nickname,
                "marketplace":    marketplace,
                "platform":       "amazon",
                "color":          "#F97316",
                "today":          _sum_period(today_pst),
                "week":           _sum_period(week_start),
                "month":          _sum_period(month_start),
                "active_items":   0,
                "daily_revenues": {k: round(v, 2) for k, v in daily_revenues.items()},
                "error":          None,
            }
        except Exception as exc:
            return {
                "seller_id":      seller_id,
                "nickname":       nickname,
                "marketplace":    marketplace,
                "platform":       "amazon",
                "color":          "#F97316",
                "today":          {"orders": 0, "units": 0, "revenue": 0},
                "week":           {"orders": 0, "units": 0, "revenue": 0},
                "month":          {"orders": 0, "units": 0, "revenue": 0},
                "active_items":   0,
                "daily_revenues": {},
                "error":          str(exc),
            }

    # Sequential — no parallel. Con 1 cuenta ahorra llamadas; con N cuentas evita 429
    amazon_data = []
    for _acct in amazon_accounts_list:
        amazon_data.append(await _fetch_amz_data(_acct))

    def _sum_p(period: str) -> dict:
        return {
            "orders":  sum(a[period]["orders"]  for a in amazon_data),
            "units":   sum(a[period]["units"]   for a in amazon_data),
            "revenue": round(sum(a[period]["revenue"] for a in amazon_data), 2),
        }

    result = {
        "date_from":       date_from,
        "date_to":         date_to,
        "amazon_accounts": amazon_data,
        "totals": {
            "today": _sum_p("today"),
            "week":  _sum_p("week"),
            "month": _sum_p("month"),
        },
    }
    _multi_account_cache[cache_key] = (_time_module.time(), result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# CONCENTRACIÓN INTELIGENTE DE STOCK
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/stock/concentration/preview")
async def stock_concentration_preview_api(
    sku: str = Query(..., description="SKU base a analizar"),
):
    """Analiza cómo se concentraría el stock de un SKU. No ejecuta ningún cambio."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    await client.close()
    from app.services.stock_concentrator import preview_concentration
    return await preview_concentration(sku)


@app.post("/api/stock/concentration/execute")
async def stock_concentration_execute_api(request: Request):
    """Ejecuta la concentración de stock de un SKU en la cuenta ganadora.

    Body: {sku, winner_user_id, total_stock, dry_run (default true)}
    Por seguridad, dry_run=true por defecto. Pasar dry_run=false para ejecutar.
    """
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    await client.close()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "JSON inválido"}, status_code=400)

    sku = body.get("sku", "").strip()
    winner_uid = body.get("winner_user_id", "").strip()
    total_stock = int(body.get("total_stock", 0))
    dry_run = bool(body.get("dry_run", True))
    trigger = body.get("trigger", "manual")

    if not sku or not winner_uid:
        return JSONResponse({"detail": "sku y winner_user_id son requeridos"}, status_code=400)

    # Protección BM caído: concentración real (dry_run=False) zerearías cuentas perdedoras
    # basándose en stock BM que puede estar desactualizado o en cero por el corte.
    if not dry_run and _bm_is_confirmed_down():
        return JSONResponse({
            "ok": False,
            "bm_down": True,
            "detail": "BinManager está caído. No se puede ejecutar concentración de stock hasta que BM responda.",
        }, status_code=503)

    # Auditoría: solo registrar ejecuciones reales (dry_run=False)
    if not dry_run:
        try:
            du = getattr(request.state, "dashboard_user", None)
            if du:
                await user_store.log_action(
                    username=du["username"],
                    user_id=du.get("id"),
                    action="ml_concentration",
                    item_id=sku,
                    detail={"winner": winner_uid, "total_stock": total_stock},
                    ip=request.headers.get("X-Forwarded-For", request.client.host if request.client else None),
                )
        except Exception:
            pass

    from app.services.stock_concentrator import execute_concentration
    result = await execute_concentration(sku, winner_uid, total_stock, dry_run=dry_run, trigger=trigger)

    # Post-execution: sincronizar DB local + cache para que todos los usuarios vean
    # el estado correcto inmediatamente, sin esperar el qty-sync de 3 min.
    if not dry_run and result.get("ok"):
        from app.services.token_store import update_ml_listing_qty as _upd_qty
        _conc_tasks = []

        # 1. Actualizar ml_listings para losers (qty → 0)
        for _loser in result.get("losers", []):
            if _loser.get("ok") and _loser.get("item_id"):
                _conc_tasks.append(asyncio.create_task(_upd_qty(_loser["item_id"], 0)))
                _synced_alert_items[_loser["item_id"]] = _time.time()

        # 2. Actualizar ml_listings para winner (qty → total_stock)
        _winner = result.get("winner") or {}
        if _winner.get("ok") and _winner.get("item_id"):
            _conc_tasks.append(asyncio.create_task(_upd_qty(_winner["item_id"], total_stock)))
            _synced_alert_items[_winner["item_id"]] = _time.time()

        # 3. Limpiar cache de stock issues para todos los usuarios
        _stock_issues_cache.clear()

    return result


@app.post("/api/stock/concentration/scan")
async def stock_concentration_scan_api(request: Request):
    """Escanea los productos de la cuenta activa y detecta candidatos a concentración.

    Filtra productos con _bm_avail < threshold (default 5).
    Para cada candidato, obtiene el preview de concentración.

    Body (opcional): {threshold: int}
    """
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        threshold = int(body.get("threshold", 5))

        # Obtener productos con BM stock ya cargado (usa cache si disponible)
        uid = client.user_id
        products = await _get_all_products_cached(client, include_paused=True)
        if products:
            bm_map = await _get_bm_stock_cached(products)
            _apply_bm_stock(products, bm_map)
        # También necesita _bm_avail (de _wh_phase / InventoryBySKUAndCondicion_Quantity)
        # Si no está en cache, la función scan_low_stock_skus usa lo que haya en _bm_avail

        from app.services.stock_concentrator import scan_low_stock_skus
        result = await scan_low_stock_skus(products, threshold)
        return result
    finally:
        await client.close()


@app.get("/api/stock/concentration/log")
async def stock_concentration_log_api(
    limit: int = Query(50, ge=1, le=200),
):
    """Historial de concentraciones ejecutadas (reales y simuladas)."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    await client.close()
    entries = await token_store.get_concentration_log(limit=limit)
    return {"entries": entries}


@app.get("/api/stock/concentration/processed-skus")
async def stock_concentration_processed_skus_api(
    days: int = Query(30, ge=1, le=365),
):
    """SKUs que ya fueron concentrados exitosamente en los últimos N días.
    Usado por el frontend para ocultar productos ya procesados del bulk."""
    skus = await token_store.get_concentrated_skus(days=days)
    return {"skus": skus, "days": days, "count": len(skus)}


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON — Dashboard principal de la cuenta Amazon activa
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/amazon", response_class=HTMLResponse)
async def amazon_dashboard(request: Request, tab: str = Query(default="dashboard")):
    """
    Dashboard Amazon — muestra ventas diarias, métricas y contexto de la
    cuenta Amazon activa (seleccionada con active_amazon_id cookie).

    Si no hay cuentas Amazon configuradas, muestra pantalla de bienvenida
    con botón para conectar.
    El PIN se valida automáticamente via PinMiddleware.
    """
    user = await get_current_user()
    ctx = await _accounts_ctx(request)

    # Obtener info de la cuenta Amazon activa (para el banner)
    active_amazon_id = ctx.get("active_amazon_id")
    amazon_account = None
    if active_amazon_id:
        amazon_account = await token_store.get_amazon_account(active_amazon_id)

    active_tab = tab if tab in ("dashboard", "ventas", "salud", "operaciones", "finanzas", "fba", "listings", "deals") else "dashboard"
    ctx["amazon_account"] = amazon_account
    ctx["active_platform"] = "amazon"
    ctx["active_amazon_tab"] = active_tab
    return templates.TemplateResponse(request, "amazon_dashboard.html", {"user": user, **ctx})


@app.get("/amazon/products", response_class=HTMLResponse)
async def amazon_products_page(request: Request):
    """Centro de Productos Amazon — Resumen, Inventario, Stock, Sin Publicar, Sin Lanzar."""
    user = await get_current_user()
    ctx = await _accounts_ctx(request)
    active_amazon_id = ctx.get("active_amazon_id")
    amazon_account = None
    if active_amazon_id:
        amazon_account = await token_store.get_amazon_account(active_amazon_id)
    ctx["amazon_account"] = amazon_account
    ctx["active_platform"] = "amazon"
    ctx["active_amazon_tab"] = "productos"
    return templates.TemplateResponse(request, "amazon_products.html", {"user": user, **ctx})


@app.get("/amazon/orders", response_class=HTMLResponse)
async def amazon_orders_page(request: Request):
    """
    Historial de Órdenes Amazon — tabla con fecha, canal FBA/FBM, estado
    y detalle lazy de items por orden.
    """
    user = await get_current_user()
    ctx = await _accounts_ctx(request)
    active_amazon_id = ctx.get("active_amazon_id")
    amazon_account = None
    if active_amazon_id:
        amazon_account = await token_store.get_amazon_account(active_amazon_id)
    ctx["amazon_account"] = amazon_account
    ctx["active_platform"] = "amazon"
    ctx["active_amazon_tab"] = "orders"
    return templates.TemplateResponse(request, "amazon_orders.html", {"user": user, **ctx})


# ═══════════════════════════════════════════════════════════════════════════
# STOCK SYNC SCHEDULER — Alertas proactivas de sobreventa (Week 3)
# Cada 4 horas verifica: items activos en MeLi con BM disponible = 0
# ═══════════════════════════════════════════════════════════════════════════

_STOCK_SYNC_INTERVAL = 4 * 3600   # 4 horas
_stock_sync_running: dict = {}     # user_id -> bool (lock por cuenta)
_auto_zero_enabled: dict = {}      # user_id -> bool (poner qty=0 automáticamente al detectar riesgo)


async def _run_stock_sync_for_user(user_id: str):
    """Compara stock activo de MeLi con BM disponible para detectar riesgo de sobreventa."""
    if _stock_sync_running.get(user_id):
        return
    _stock_sync_running[user_id] = True
    try:
        print(f"[STOCK-SYNC] Iniciando sync para user {user_id}...")
        client = await get_meli_client(user_id=user_id)

        # 1. Obtener todos los items activos (scroll completo)
        all_items = []
        offset = 0
        limit = 50
        while True:
            try:
                resp = await client.get(f"/users/{user_id}/items/search",
                                        params={"status": "active", "offset": offset, "limit": limit})
                ids = resp.get("results", [])
                if not ids:
                    break
                # Fetch detalles
                details = await client.get_items_details(ids)
                all_items.extend(details)
                paging = resp.get("paging", {})
                total = paging.get("total", 0)
                offset += limit
                if offset >= total:
                    break
            except Exception as e:
                print(f"[STOCK-SYNC] Error fetching items page offset={offset}: {e}")
                break

        print(f"[STOCK-SYNC] {len(all_items)} items activos obtenidos para {user_id}")

        # 2. Construir lista minimal de productos para _get_bm_stock_cached
        products = []
        item_map = {}
        for item in all_items:
            body = item.get("body", item) if isinstance(item, dict) else item
            iid = body.get("id", "") if isinstance(body, dict) else getattr(body, "id", "")
            body_dict = body if isinstance(body, dict) else vars(body)
            sku = _get_item_sku(body_dict)
            if not sku or not iid:
                continue
            # Excluir FULL items — ML controla su stock, no se puede modificar vía API
            logistic_type = (body_dict.get("shipping") or {}).get("logistic_type", "")
            if logistic_type == "fulfillment":
                continue
            qty   = body_dict.get("available_quantity", 0) or 0
            title = body_dict.get("title", "") or ""
            price = body_dict.get("price", 0) or 0
            products.append({"sku": sku, "item_id": iid, "meli_stock": qty, "title": title, "price": price})
            item_map[iid] = {"sku": sku, "meli_stock": qty, "title": title, "price": price}

        # 3. Obtener stock BM para todos los productos
        bm_map = await _get_bm_stock_cached(products)

        # 4. Detectar sobreventas: MeLi stock > 0 pero BM disponible = 0
        # IMPORTANTE: si BM no retornó datos para un SKU (error/503) → NO flaggear.
        # Solo flaggear si BM confirmó explícitamente avail_total=0.
        alerts = []
        for p in products:
            sku = p["sku"]
            base_sku = _clean_sku_for_bm(sku)
            bm_info = bm_map.get(sku) or bm_map.get(base_sku)
            if bm_info is None:
                # BM no retornó datos (error/503) → skip, no crear falso positivo
                continue
            bm_avail = bm_info.get("avail_total", 0)
            meli_stock = p["meli_stock"]
            if meli_stock > 0 and bm_avail == 0:
                alerts.append({
                    "item_id": p["item_id"],
                    "title": p["title"],
                    "sku": sku,
                    "meli_stock": meli_stock,
                    "price": p.get("price", 0),
                    "bm_avail": 0,
                    "alert_type": "oversell",
                })

        # 5. Guardar alertas y status
        await token_store.save_sync_alerts(user_id, alerts)
        await token_store.save_sync_status(user_id, len(alerts), "ok")
        print(f"[STOCK-SYNC] Done user {user_id}: {len(alerts)} alertas de sobreventa")

        # Auto qty=0: delegado al stock_sync_multi (BM→ML+Amazon cada 5 min).
        # Este sync viejo solo detecta alertas — no modifica stock para evitar conflictos.
    except Exception as e:
        print(f"[STOCK-SYNC] Error en sync para {user_id}: {e}")
        try:
            await token_store.save_sync_status(user_id, 0, f"error: {str(e)[:100]}")
        except Exception:
            pass
    finally:
        _stock_sync_running[user_id] = False


async def _stock_sync_loop():
    """Loop periódico que ejecuta el stock sync para todas las cuentas MeLi."""
    await asyncio.sleep(60)  # Esperar 1 min al arranque para que los tokens se siembren
    while True:
        try:
            accounts = await token_store.get_all_tokens()
            for acc in accounts:
                uid = acc.get("user_id", "")
                if uid:
                    await _run_stock_sync_for_user(uid)
                    await asyncio.sleep(5)  # Separar llamadas entre cuentas
        except Exception as e:
            print(f"[STOCK-SYNC-LOOP] Error: {e}")
        await asyncio.sleep(_STOCK_SYNC_INTERVAL)


def start_stock_sync():
    """Inicia el loop de stock sync en background."""
    asyncio.create_task(_stock_sync_loop())


async def _token_refresh_loop():
    """Auto-renueva tokens MeLi cada 5 horas — evita expiración silenciosa.
    MeLi tokens duran 6 horas; refrescamos a las 5h para tener margen.
    """
    await asyncio.sleep(300)  # 5 min initial delay — dejar que todo arranque primero
    while True:
        try:
            print("[TOKEN_REFRESH] Renovando tokens MeLi automáticamente...")
            await _seed_tokens()
            print("[TOKEN_REFRESH] Tokens renovados OK")
        except Exception as e:
            print(f"[TOKEN_REFRESH] Error al renovar tokens: {e}")
        await asyncio.sleep(5 * 3600)  # Cada 5 horas


def start_token_refresh():
    """Inicia el loop de auto-refresh de tokens en background."""
    asyncio.create_task(_token_refresh_loop())


# ─── Endpoints de Sync ───────────────────────────────────────────────────────

@app.get("/api/sync/alerts", response_class=HTMLResponse)
async def get_sync_alerts_partial(request: Request):
    """Retorna HTML con las alertas de sobreventa del usuario actual."""
    client = await get_meli_client()
    if not client:
        return HTMLResponse("")
    user_id = client.user_id
    alerts = await token_store.get_sync_alerts(user_id)
    status = await token_store.get_sync_status(user_id)
    last_run = status.get("last_run", "") if status else ""
    if not alerts:
        return HTMLResponse("")
    total = len(alerts)
    last_str = f"sync: {last_run[:16]}" if last_run else ""
    all_ids = ",".join(f"'{a['item_id']}'" for a in alerts)

    rows = ""
    for i, a in enumerate(alerts):
        sku_str   = a.get("sku") or ""
        price_val = a.get("price", 0) or 0
        price_html = (f'<div class="text-center hidden md:block">'
                      f'<div class="text-[10px] text-gray-400 mb-0.5">Precio</div>'
                      f'<span class="text-xs font-semibold text-gray-700">${price_val:,.0f}</span>'
                      f'</div>') if price_val else ""
        sku_html = (f'<span class="font-mono text-[11px] font-bold text-orange-600 bg-orange-50 px-1.5 py-0.5 rounded">{sku_str}</span>'
                    if sku_str else '<span class="text-[10px] text-gray-300 font-mono">sin SKU</span>')
        rows += (
            f'<div class="alert-row flex items-center gap-3 px-4 py-3 border-b border-gray-100'
            f' last:border-0 hover:bg-red-50/20 transition-colors" data-idx="{i}" data-item-id="{a["item_id"]}" style="display:none">'
            f'<div class="min-w-0 flex-1">'
            f'<div class="flex items-center gap-2 flex-wrap mb-0.5">'
            f'<span class="font-mono text-[11px] font-semibold text-blue-600">{a["item_id"]}</span>'
            f'{sku_html}'
            f'</div>'
            f'<span class="text-xs text-gray-600 truncate block" title="{a["title"]}">{a["title"][:70]}</span>'
            f'</div>'
            f'<div class="flex-shrink-0 flex items-center gap-4 text-xs">'
            f'{price_html}'
            f'<div class="text-center">'
            f'<div class="text-[10px] text-gray-400 mb-0.5">MeLi</div>'
            f'<span class="bg-red-100 text-red-700 font-bold px-2 py-0.5 rounded-lg text-xs">{a["meli_stock"]}</span>'
            f'</div>'
            f'<div class="text-center">'
            f'<div class="text-[10px] text-gray-400 mb-0.5">BM</div>'
            f'<span class="bg-gray-100 text-gray-500 font-bold px-2 py-0.5 rounded-lg text-xs">0</span>'
            f'</div>'
            f'</div>'
            f'<button onclick="zeroAlertItem(\'{a["item_id"]}\', this)"'
            f' class="flex-shrink-0 bg-red-500 hover:bg-red-600 active:bg-red-700 text-white'
            f' px-3 py-1.5 rounded-xl text-[11px] font-semibold transition-colors min-w-[56px] text-center">Qty 0</button>'
            f'</div>'
        )

    html = f"""<div class="mb-4 bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden">
  <div class="flex items-center justify-between px-5 py-3.5 border-b border-gray-100">
    <div class="flex items-center gap-2">
      <div class="w-1 h-5 bg-red-500 rounded"></div>
      <span class="font-semibold text-gray-800 text-sm">{total} items en riesgo de sobreventa</span>
      {f'<span class="text-[10px] text-gray-400 hidden md:inline">— {last_str}</span>' if last_str else ''}
    </div>
    <div class="flex items-center gap-2">
      <button onclick="bulkZeroAlerts()" id="btn-bulk-zero"
              class="text-[11px] bg-red-500 hover:bg-red-600 text-white font-semibold px-3 py-1.5 rounded-xl transition-colors">
        Poner en 0 ({total})
      </button>
      <label class="flex items-center gap-1.5 cursor-pointer" title="Poner qty=0 autom\u00e1ticamente al detectar riesgo">
        <span class="text-[11px] text-gray-500">Auto qty=0</span>
        <input type="checkbox" id="chk-auto-pause" onchange="toggleAutoPause(this.checked)"
               class="w-3.5 h-3.5 accent-red-500">
      </label>
      <button onclick="triggerStockSync()" id="btn-sync-now"
              class="text-[11px] text-gray-500 hover:text-gray-700 font-medium">Sync ahora</button>
    </div>
  </div>
  <p class="text-[11px] text-gray-400 px-5 py-2 border-b border-gray-50 bg-red-50/40">
    Items activos en MeLi con stock &gt; 0 pero BM disponible = 0. Riesgo de vender sin stock fisico.
  </p>
  <div id="alerts-list">{rows}</div>
  <div class="flex items-center justify-between px-5 py-2.5 bg-gray-50/60 border-t border-gray-100">
    <span id="alerts-page-info" class="text-xs text-gray-400"></span>
    <div class="flex items-center gap-1" id="alerts-pagination"></div>
  </div>
</div>
<script>
(function() {{
  var _page = 1, _per = 10;
  var _rows = document.querySelectorAll('#alerts-list .alert-row');
  var _total = _rows.length;
  var _pages = Math.ceil(_total / _per);
  function render(p) {{
    _page = p;
    var s = (p - 1) * _per, e = Math.min(s + _per, _total);
    _rows.forEach(function(r, i) {{ r.style.display = (i >= s && i < e) ? 'flex' : 'none'; }});
    var info = document.getElementById('alerts-page-info');
    if (info) info.textContent = 'Mostrando ' + (s + 1) + '\u2013' + e + ' de ' + _total;
    var pag = document.getElementById('alerts-pagination');
    if (!pag) return;
    var btn = function(label, page, active, disabled) {{
      return '<button onclick="window._alertsPage(' + page + ')" ' + (disabled ? 'disabled' : '') +
        ' class="px-2.5 py-1 text-xs rounded-lg border font-medium transition-colors ' +
        (disabled ? 'text-gray-300 border-gray-200 cursor-not-allowed ' :
         active ? 'bg-red-500 text-white border-red-500 ' :
         'text-gray-600 border-gray-300 hover:bg-gray-100 ') + '">' + label + '</button>';
    }};
    var html = btn('\u2039', p - 1, false, p <= 1);
    var sp = Math.max(1, Math.min(p - 2, _pages - 4));
    for (var i = sp; i <= Math.min(sp + 4, _pages); i++) html += btn(i, i, i === p, false);
    html += btn('\u203a', p + 1, false, p >= _pages);
    pag.innerHTML = html;
  }}
  window._alertsPage = function(p) {{ if (p >= 1 && p <= _pages) render(p); }};
  window.bulkZeroAlerts = function() {{
    var ids = [{all_ids}];
    if (!confirm('Poner en 0 el stock de ' + ids.length + ' productos en riesgo de sobreventa?')) return;
    var btn = document.getElementById('btn-bulk-zero');
    if (btn) {{ btn.disabled = true; btn.textContent = 'Procesando...'; }}
    var done = 0; var blocked = 0;
    ids.forEach(function(id) {{
      fetch('/api/items/' + id + '/stock', {{
        method: 'PUT',
        headers: {{'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true'}},
        body: JSON.stringify({{quantity: 0}})
      }}).then(function(r) {{
        if (r.status === 503) {{
          blocked++;
          return r.json().then(function(d) {{
            if (d.bm_down && blocked === 1) {{
              alert('\u26a0\ufe0f BinManager est\u00e1 ca\u00eddo. No se puede poner en 0 hasta que BM responda — las alertas pueden ser incorrectas.');
              if (btn) {{ btn.disabled = false; btn.textContent = 'BM ca\u00eddo \u2014 bloqueado'; }}
            }}
          }});
        }}
      }}).finally(function() {{
        done++;
        if (btn && blocked === 0) btn.textContent = 'Procesando ' + done + '/' + ids.length + '...';
        if (done === ids.length && btn && blocked === 0) {{
          btn.textContent = 'Completado \u2713';
          btn.className = btn.className.replace('bg-red-100 hover:bg-red-200 text-red-700', 'bg-green-100 text-green-700');
        }}
      }});
    }});
  }};
  render(1);
}})();
window.zeroAlertItem = function(itemId, btn) {{
  btn.disabled = true; btn.textContent = '...';
  fetch('/api/items/' + itemId + '/stock', {{
    method: 'PUT',
    headers: {{'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true'}},
    body: JSON.stringify({{quantity: 0}})
  }})
  .then(function(r) {{ return r.json().then(function(d) {{ return {{ok: r.ok, status: r.status, data: d}}; }}); }})
  .then(function(res) {{
    if (res.ok) {{
      btn.textContent = '✓ 0';
      btn.className = btn.className.replace(/bg-red-\d00/g, 'bg-green-600').replace(/hover:bg-red-\d00/g, '').replace(/active:bg-red-\d00/g, '');
      var row = btn.closest('.alert-row');
      if (row) row.style.opacity = '0.4';
    }} else if (res.status === 503 && res.data && res.data.bm_down) {{
      btn.textContent = 'BM caído';
      btn.title = res.data.detail || 'BinManager no disponible';
      btn.disabled = false;
      btn.className = btn.className.replace(/bg-red-\d00/g, 'bg-yellow-500').replace(/hover:bg-red-\d00/g, '').replace(/active:bg-red-\d00/g, '');
    }} else {{
      var errMsg = (res.data && res.data.detail) || ('HTTP ' + res.status);
      btn.textContent = 'Error';
      btn.title = errMsg;
      btn.disabled = false;
      var row = btn.closest('.alert-row');
      if (row && !row.querySelector('.err-detail')) {{
        var span = document.createElement('span');
        span.className = 'err-detail text-[10px] text-red-600 block mt-1 truncate max-w-[200px]';
        span.textContent = errMsg.substring(0, 80);
        row.querySelector('.min-w-0').appendChild(span);
      }}
    }}
  }})
  .catch(function(e) {{ btn.textContent = 'Error'; btn.title = e.message; btn.disabled = false; }});
}};
window.triggerStockSync = function() {{
  var btn = document.getElementById('btn-sync-now');
  if (btn) {{ btn.textContent = 'Iniciando...'; btn.style.pointerEvents = 'none'; }}
  fetch('/api/stock/multi-sync/trigger', {{method:'POST'}})
    .then(function(r) {{ return r.json(); }})
    .then(function() {{
      var secs = 0;
      // Fase 1: esperar a que multi-sync termine
      var poll = setInterval(function() {{
        secs += 2;
        var b = document.getElementById('btn-sync-now');
        if (b) b.textContent = 'Sincronizando (' + secs + 's)...';
        fetch('/api/stock/multi-sync/status')
          .then(function(r) {{ return r.json(); }})
          .then(function(s) {{
            if (!s.running) {{
              clearInterval(poll);
              // Fase 2: esperar prewarm (re-fetcha BM con datos frescos)
              var b2 = document.getElementById('btn-sync-now');
              if (b2) b2.textContent = 'Actualizando BM...';
              setTimeout(function() {{
                var pw = setInterval(function() {{
                  secs += 2;
                  var b3 = document.getElementById('btn-sync-now');
                  if (b3) b3.textContent = 'Actualizando (' + secs + 's)...';
                  fetch('/api/stock/prewarm-status')
                    .then(function(r) {{ return r.json(); }})
                    .then(function(p) {{
                      if (!p.running) {{
                        clearInterval(pw);
                        var b4 = document.getElementById('btn-sync-now');
                        if (b4) {{ b4.textContent = 'Sync ahora'; b4.style.pointerEvents = 'auto'; }}
                        var upd = (s.last_result || {{}}).updates || 0;
                        var toast = document.createElement('div');
                        toast.innerHTML = '<span style="font-size:1.1em">✓</span> Sync completado — ' + upd + ' updates';
                        toast.className = 'fixed bottom-4 right-4 z-50 px-4 py-3 rounded-xl shadow-lg text-sm font-medium bg-green-50 text-green-700 border border-green-200';
                        document.body.appendChild(toast);
                        setTimeout(function() {{ toast.remove(); }}, 5000);
                        // Recargar la seccion con datos frescos
                        if (window.switchProductTab) {{
                          window.switchProductTab('inventory', '/partials/products-inventory?preset=accion&enrich=full');
                        }}
                      }}
                    }})
                    .catch(function() {{ clearInterval(pw); }});
                }}, 2000);
                setTimeout(function() {{
                  clearInterval(pw);
                  var b5 = document.getElementById('btn-sync-now');
                  if (b5) {{ b5.textContent = 'Sync ahora'; b5.style.pointerEvents = 'auto'; }}
                }}, 120000);
              }}, 3000); // dar 3s para que el prewarm arranque
            }}
          }})
          .catch(function() {{ clearInterval(poll); }});
      }}, 2000);
      setTimeout(function() {{
        clearInterval(poll);
        var b = document.getElementById('btn-sync-now');
        if (b) {{ b.textContent = 'Sync ahora'; b.style.pointerEvents = 'auto'; }}
      }}, 90000);
    }})
    .catch(function() {{
      var b = document.getElementById('btn-sync-now');
      if (b) {{ b.textContent = 'Error — reintentar'; b.style.pointerEvents = 'auto'; b.style.color = '#dc2626'; }}
    }});
}}
// Load initial auto-pause state
fetch('/api/config/auto-pause').then(function(r){{return r.json();}}).then(function(d){{
  var chk = document.getElementById('chk-auto-pause');
  if (chk) chk.checked = d.enabled || false;
}}).catch(function(){{}});
window.toggleAutoPause = function(enabled) {{
  fetch('/api/config/auto-pause', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{enabled: enabled}})
  }}).then(function(r){{return r.json();}}).then(function(d){{
    var chk = document.getElementById('chk-auto-pause');
    if (chk) chk.checked = d.enabled;
  }});
}}
</script>"""
    return HTMLResponse(html)


@app.get("/api/sync/stock-counts")
async def get_stock_counts():
    """Conteos rápidos de los 4 grupos de acción. Usa _stock_issues_cache si disponible."""
    client = await get_meli_client()
    if not client:
        return {"sin_stock": 0, "riesgo": 0, "critico": 0, "sin_publicar": 0, "pausados": 0}
    try:
        # Paused count from cache (non-blocking — returns 0 if not yet fetched)
        uid = str(client.user_id)
        cached_paused = _paused_items_cache.get(uid)
        pausados = len(cached_paused[1]) if cached_paused else 0

        key = f"stock_issues:{client.user_id}:t10"
        entry = _stock_issues_cache.get(key)
        if entry and (_time.time() - entry[0]) < _STOCK_ISSUES_TTL:
            ctx = entry[1]
            return {
                "sin_stock": ctx.get("restock_count", 0),
                "riesgo": ctx.get("risk_count", 0),
                "critico": ctx.get("critical_count", 0),
                "sin_publicar": 0,
                "pausados": pausados,
            }
        # Fallback: solo alertas de sobreventa desde DB (siempre disponibles)
        alerts = await token_store.get_sync_alerts(client.user_id)
        return {"sin_stock": 0, "riesgo": len(alerts), "critico": 0, "sin_publicar": 0, "pausados": pausados}
    finally:
        await client.close()


# ── Listings pausados por stock 0 ─────────────────────────────────────────────
_paused_items_cache: dict[str, tuple[float, list]] = {}
_PAUSED_ITEMS_TTL = 300  # 5 min


async def _reactivate_if_oos_bg(item_id: str, user_id: str):
    """Background task: if item is paused due to out_of_stock, reactivate it silently."""
    try:
        from app.services.meli_client import get_meli_client as _gmcl
        _client = await _gmcl(user_id=user_id)
        if not _client:
            return
        try:
            detail = await _client.get_item(item_id)
            sub = detail.get("sub_status") or []
            if detail.get("status") == "paused" and "out_of_stock" in sub:
                await _client.update_item_status(item_id, "active")
                _paused_items_cache.pop(user_id, None)
        finally:
            await _client.close()
    except Exception:
        pass


async def _get_paused_oos_items(client) -> list[dict]:
    """Fetches all paused items with sub_status=out_of_stock from ML.
    Returns list of dicts: {id, title, price, sku}.
    Result is cached per user for 5 minutes.
    """
    uid = str(client.user_id)
    cached = _paused_items_cache.get(uid)
    if cached and (_time.time() - cached[0]) < _PAUSED_ITEMS_TTL:
        return cached[1]

    ids = await client.get_all_paused_item_ids()
    items: list[dict] = []
    # Batch-fetch details in chunks of 20
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        try:
            details = await client.get_items_details(chunk)
            for d in (details if isinstance(details, list) else []):
                if not isinstance(d, dict):
                    continue
                sub = d.get("sub_status") or []
                if "out_of_stock" not in sub:
                    continue
                # Extract SKU from attributes
                sku = ""
                for att in (d.get("attributes") or []):
                    if att.get("id") == "SELLER_SKU":
                        sku = att.get("value_name") or ""
                        break
                items.append({
                    "id": d.get("id", ""),
                    "title": d.get("title", ""),
                    "price": d.get("price", 0),
                    "sku": sku,
                })
        except Exception:
            pass

    _paused_items_cache[uid] = (_time.time(), items)
    return items


@app.get("/api/ml/paused-items")
async def get_paused_items_api():
    """Lista todos los listings pausados por stock=0 (sub_status=out_of_stock)."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_session"}, status_code=401)
    try:
        items = await _get_paused_oos_items(client)
        return {"count": len(items), "items": items}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await client.close()


@app.post("/api/ml/reactivate/{item_id}")
async def reactivate_item_api(item_id: str):
    """Reactiva un listing pausado (solo cambia status=active, no toca stock)."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_session"}, status_code=401)
    try:
        result = await client.update_item_status(item_id, "active")
        # Invalidar cache
        uid = str(client.user_id)
        _paused_items_cache.pop(uid, None)
        return {"ok": True, "item_id": item_id, "result": result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    finally:
        await client.close()


@app.post("/api/ml/reactivate-all")
async def reactivate_all_paused_api():
    """Reactiva en bulk todos los listings pausados por out_of_stock."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_session"}, status_code=401)
    try:
        items = await _get_paused_oos_items(client)
        ok_ids, fail_ids = [], []
        for it in items:
            try:
                await client.update_item_status(it["id"], "active")
                ok_ids.append(it["id"])
            except Exception:
                fail_ids.append(it["id"])
        # Invalidar cache
        uid = str(client.user_id)
        _paused_items_cache.pop(uid, None)
        return {"ok": True, "reactivated": len(ok_ids), "failed": len(fail_ids),
                "ok_ids": ok_ids, "fail_ids": fail_ids}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        await client.close()


@app.post("/api/sync/trigger")
async def trigger_stock_sync():
    """Dispara el stock sync manualmente para el usuario actual."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_session"}, status_code=401)
    user_id = client.user_id
    asyncio.create_task(_run_stock_sync_for_user(user_id))
    return {"status": "triggered", "user_id": user_id}


@app.get("/api/sync/status")
async def get_stock_sync_status():
    """Retorna el estado del último sync y conteo de alertas."""
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_session"}, status_code=401)
    user_id = client.user_id
    status = await token_store.get_sync_status(user_id)
    alerts = await token_store.get_sync_alerts(user_id)
    return {
        "user_id": user_id,
        "last_run": status.get("last_run") if status else None,
        "last_result": status.get("last_result") if status else None,
        "alerts_count": len(alerts),
        "running": _stock_sync_running.get(user_id, False),
    }


@app.get("/api/sync/alerts-count")
async def get_sync_alerts_count():
    """Retorna solo el conteo de alertas (para badges)."""
    client = await get_meli_client()
    if not client:
        return {"count": 0}
    alerts = await token_store.get_sync_alerts(client.user_id)
    return {"count": len(alerts)}


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-PLATFORM STOCK SYNC — estado, trigger, historial, reglas por SKU
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/stock/multi-sync/status")
async def multi_sync_status():
    """Estado del último ciclo de sync multi-plataforma."""
    from app.services.stock_sync_multi import get_sync_status
    status = get_sync_status()
    try:
        history = await token_store.get_multi_sync_last_runs(limit=5)
        status["recent_runs"] = history
    except Exception:
        status["recent_runs"] = []
    return status


@app.get("/api/stock/prewarm-status")
async def prewarm_status():
    """Estado del prewarm de stock issues — para polling desde loading page."""
    client = await get_meli_client()
    uid = client.user_id if client else None
    if client:
        await client.close()
    key = f"stock_issues:{uid}:t10" if uid else None
    cache_entry = key and _stock_issues_cache.get(key)
    cache_ready = bool(cache_entry and (_time.time() - cache_entry[0]) < _STOCK_ISSUES_TTL)
    last_updated = round(_time.time() - cache_entry[0]) if cache_entry else None
    progress = {}
    if _prewarm_running and _prewarm_progress.get("total", 0) > 0:
        elapsed = round(_time.time() - _prewarm_progress.get("started_at", _time.time()))
        progress = {
            "done": _prewarm_progress.get("done", 0),
            "total": _prewarm_progress.get("total", 0),
            "elapsed_s": elapsed,
        }
    bm_down = _bm_health.get("ok") is False and _bm_health.get("consecutive_failures", 0) >= 2
    bm_down_min = None
    if bm_down and _bm_health.get("last_ok_ts", 0) > 0:
        bm_down_min = round((_time.time() - _bm_health["last_ok_ts"]) / 60)
    # stale_available: hay datos en cache aunque estén expirados — permite mostrarlos mientras BM está caído
    stale_available = bool(cache_entry)
    # Estadísticas de cobertura BM del último bulk — para diagnóstico en Sync Stock
    bulk_stats = {}
    if _bm_bulk_stats and _bm_bulk_stats.get("ts"):
        bulk_stats = {
            "bulk_gr_rows":      _bm_bulk_stats.get("bulk_gr_rows", 0),
            "bulk_all_rows":     _bm_bulk_stats.get("bulk_all_rows", 0),
            "skus_total":        _bm_bulk_stats.get("skus_total", 0),
            "found":             _bm_bulk_stats.get("found", 0),
            "zero":              _bm_bulk_stats.get("zero", 0),
            "zero_in_bulk":      _bm_bulk_stats.get("zero_in_bulk", 0),
            "not_in_bulk":       _bm_bulk_stats.get("not_in_bulk", 0),
            "fallback_used":     _bm_bulk_stats.get("fallback_used", 0),
            "zero_skus":         _bm_bulk_stats.get("zero_skus", []),
            "zero_in_bulk_skus": _bm_bulk_stats.get("zero_in_bulk_skus", []),
            "age_min":           round((_time.time() - _bm_bulk_stats["ts"]) / 60),
        }
    return JSONResponse({
        "running": _prewarm_running,
        "ready": cache_ready,
        "error": _prewarm_error[:300] if _prewarm_error else "",
        "last_updated_s": last_updated,
        "progress": progress,
        "bm_down": bm_down,
        "bm_down_min": bm_down_min,
        "stale_available": stale_available,
        "bulk_stats": bulk_stats,
    })


@app.get("/api/stock/bm-sync-log")
async def bm_sync_log_endpoint():
    """Historial de ejecuciones del prewarm BM — para la tarjeta Caché de Stock BM."""
    rows = await token_store.get_bm_sync_log(limit=10)
    import time as _t
    now = _t.time()
    entries = []
    for r in rows:
        age_s = now - r["synced_at"]
        if age_s < 120:
            age_str = f"hace {int(age_s)}s"
        elif age_s < 3600:
            age_str = f"hace {int(age_s//60)}min"
        else:
            age_str = f"hace {int(age_s//3600)}h"
        entries.append({
            "synced_at":  r["synced_at"],
            "age_str":    age_str,
            "sku_count":  r["sku_count"],
            "elapsed_s":  r["elapsed_s"],
            "source":     r["source"],
        })
    return JSONResponse({"log": entries})


@app.get("/api/bm/status")
async def bm_health_status():
    """Estado de salud de BinManager — para el indicador en navbar."""
    from datetime import datetime
    h = _bm_health
    down_since_min = None
    if h["ok"] is False and h["last_ok_ts"] > 0:
        down_since_min = round((_time.time() - h["last_ok_ts"]) / 60)
    last_check_iso = (
        datetime.utcfromtimestamp(h["last_check_ts"]).strftime("%H:%M") if h["last_check_ts"] else None
    )
    return JSONResponse({
        "ok": h["ok"],
        "latency_ms": h["latency_ms"],
        "last_check_iso": last_check_iso,
        "consecutive_failures": h["consecutive_failures"],
        "down_since_min": down_since_min,
        "error_type": h.get("error_type"),
        "last_error": h.get("last_error", ""),
    })


@app.get("/api/bm/health-log")
async def bm_health_log_endpoint():
    """Historial de los últimos checks del health loop de BM (más reciente primero)."""
    return JSONResponse(list(reversed(_bm_health_log)))


# ── Listings Cache — descarga y resumen de listings ML + Amazon ────────────

_listings_refresh_running = False   # flag global para el botón Sync Listings

@app.get("/api/listings/summary")
async def listings_summary_endpoint(request: Request):
    """Retorna conteo de listings por cuenta (ML + Amazon) + estado del sync."""
    _du = getattr(request.state, "dashboard_user", None) or {}
    if _du.get("role") != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    from app.services.token_store import get_listings_summary
    from app.services.ml_listing_sync import get_sync_status as _ml_status
    from app.services.amazon_listing_sync import get_sync_status as _amz_status

    summary = await get_listings_summary()
    ml_st   = _ml_status()
    amz_st  = _amz_status()

    return JSONResponse({
        **summary,
        "ml_running":       ml_st.get("running", False),
        "amz_running":      amz_st.get("running", False),
        "running":          _listings_refresh_running,   # flag cubre ML + Amazon completos
        "ml_error":         ml_st.get("error", ""),
        "amz_error":        amz_st.get("error", ""),
        "ml_last_full_ts":  ml_st.get("last_full_sync_ts", 0),
        "amz_last_sync_ts": amz_st.get("last_sync_ts", 0),
    })


@app.post("/api/listings/refresh")
async def listings_refresh_endpoint(request: Request):
    """Dispara un full sync de listings ML + Amazon en background (solo lectura)."""
    global _listings_refresh_running
    _du = getattr(request.state, "dashboard_user", None) or {}
    if _du.get("role") != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    if _listings_refresh_running:
        return JSONResponse({"status": "already_running"})

    from app.services.ml_listing_sync import run_ml_listing_sync as _ml_sync
    from app.services.amazon_listing_sync import run_amazon_listing_sync as _amz_sync
    import asyncio as _asyncio

    # ML y Amazon en paralelo — flag global cubre ambos
    async def _bg():
        global _listings_refresh_running
        _listings_refresh_running = True
        try:
            await _asyncio.gather(
                _ml_sync(full=True),
                _amz_sync(),
                return_exceptions=True,
            )
        finally:
            _listings_refresh_running = False

    _asyncio.create_task(_bg())
    return JSONResponse({"status": "started"})


@app.get("/api/debug/bm-cache")
async def debug_bm_cache(sku: str = ""):
    """Diagnóstico: muestra estado del caché BM para un SKU y en qué alertas aparece."""
    if not sku:
        return JSONResponse({"error": "sku requerido"}, status_code=400)
    upper = sku.strip().upper()
    bm_key = normalize_to_bm_sku(upper)  # normalizar para buscar en cache unificado
    cached = _bm_stock_cache.get(bm_key)
    if not cached:
        return JSONResponse({"found": False, "sku": upper, "message": "No está en caché — se fetcheará en el próximo prewarm"})

    ts, data = cached
    age_s = round(_time.time() - ts)
    ttl_left = max(0, round(_BM_CACHE_TTL - (_time.time() - ts)))

    avail = data.get("avail_total", 0)
    total = data.get("total", 0)
    reserved = data.get("reserved_total", 0)
    verified = data.get("_v", None)

    # Determinar en qué sección de alertas aparecería
    # Necesitamos datos del producto de MeLi — lo buscamos en el issues cache
    alerts = []
    client = await get_meli_client()
    uid = client.user_id if client else None
    if client:
        await client.close()
    key = f"stock_issues:{uid}:t10" if uid else None
    issues_entry = key and _stock_issues_cache.get(key)
    if issues_entry:
        ctx = issues_entry[1]
        for section in ("restock", "oversell_risk", "activate", "critical", "full_no_stock", "stagnant", "price_risk"):
            items = ctx.get(section, [])
            match = next((p for p in items if (p.get("sku") or "").upper() == upper), None)
            if match:
                alerts.append(section)

    # Validez del caché
    if total == 0 and avail == 0 and not verified:
        cache_status = "STALE — será re-fetcheado (fetch fallido sin verificar)"
    elif total > 0 and avail == 0:
        cache_status = "PARCIAL — stock físico pero avail=0 (get_stock_with_reserve falló)"
    else:
        cache_status = "OK"

    return JSONResponse({
        "found": True,
        "sku": upper,
        "cache": {
            "avail_total": avail,
            "total": total,
            "reserved": reserved,
            "mty": data.get("mty", 0),
            "cdmx": data.get("cdmx", 0),
            "tj": data.get("tj", 0),
            "_v": verified,
            "age_s": age_s,
            "ttl_left_s": ttl_left,
            "status": cache_status,
        },
        "alerts": alerts if alerts else ["ninguna"],
    })


_DEBUG_KEY = "mi-apantallate-debug-2025"
import os as _os_diag
_DIAG_TOKEN = _os_diag.getenv("DIAG_TOKEN", "dk_b55c96a82a49f04908e0079bda6bee41ce2748be2c11f3b5")


@app.get("/api/diag/sku")
async def diag_sku(sku: str = "", token: str = ""):
    """Diagnóstico externo: caché BM + consulta BM en vivo para un SKU.
    No requiere sesión de dashboard — usa token DIAG_TOKEN.
    Devuelve: cache (avail, reserve, edad, verificado, expirado),
              bm_live (avail, reserve directo de BM),
              bulk_cache (si el SKU aparece en el último bulk fetch),
              discrepancy (True si cache=0 pero BM tiene stock).
    """
    if token != _DIAG_TOKEN:
        return JSONResponse({"error": "token inválido"}, status_code=403)
    if not sku:
        return JSONResponse({"error": "sku requerido"}, status_code=400)

    bm_key = normalize_to_bm_sku(sku.strip().upper())
    now = _time.time()

    # ── 1. Caché por SKU ──────────────────────────────────────────────────────
    cached = _bm_stock_cache.get(bm_key)
    if cached:
        ts, data = cached
        age_s = round(now - ts)
        cache_info = {
            "found": True,
            "avail_total": data.get("avail_total", 0),
            "reserved_total": data.get("reserved_total", 0),
            "total_wh": data.get("total", 0),
            "verified": data.get("_v", False),
            "age_s": age_s,
            "expired": age_s >= _BM_CACHE_TTL,
        }
    else:
        cache_info = {"found": False}

    # ── 2. Bulk cache (GR y ALL) ──────────────────────────────────────────────
    def _diag_bulk_info(cache):
        if not cache:
            return {"found": False}
        bulk_ts, bulk_rows = cache
        bulk_age = round(now - bulk_ts)
        match = next((r for r in (bulk_rows or []) if (r.get("SKU") or "").upper().strip() == bm_key.upper()), None)
        if match:
            return {
                "found": True,
                "avail": int(match.get("AvailableQTY") or 0),
                "reserve": int(match.get("Reserve") or 0),
                "total": int(match.get("TotalQty") or 0),
                "bulk_age_s": bulk_age,
            }
        # Also check base-sku variants in the bulk
        base_matches = [r for r in (bulk_rows or [])
                        if _extract_base_sku((r.get("SKU") or "").upper().strip()) == bm_key.upper()]
        if base_matches:
            return {
                "found": True,
                "avail": sum(int(r.get("AvailableQTY") or 0) for r in base_matches),
                "reserve": sum(int(r.get("Reserve") or 0) for r in base_matches),
                "variants": [(r.get("SKU") or "").upper().strip() for r in base_matches],
                "bulk_age_s": bulk_age,
            }
        return {"found": False, "bulk_age_s": bulk_age, "bulk_total_rows": len(bulk_rows or [])}

    bulk_info_gr  = _diag_bulk_info(_bm_bulk_gr_cache)
    bulk_info_all = _diag_bulk_info(_bm_bulk_all_cache)
    # Para compatibilidad con código que usa bulk_info.avail: usar el cache relevante
    bulk_info = bulk_info_all if (_bm_conditions_for_sku(bm_key) == "GRA,GRB,GRC,ICB,ICC,NEW") else bulk_info_gr

    # ── 3. BM en vivo ─────────────────────────────────────────────────────────
    _conditions_used = _bm_conditions_for_sku(bm_key)
    try:
        from app.services.binmanager_client import get_shared_bm as _get_diag_bm
        _bm = await _get_diag_bm()
        _live = await asyncio.wait_for(
            _bm.get_stock_with_reserve(bm_key, conditions=_conditions_used),
            timeout=10.0,
        )
        bm_live = {"avail": _live[0], "reserve": _live[1]} if _live is not None else {"error": "BM no respondió (None)"}
    except Exception as _e:
        bm_live = {"error": str(_e)[:120]}

    # ── 4. Discrepancia ───────────────────────────────────────────────────────
    cache_avail = cache_info.get("avail_total", 0) if cache_info.get("found") else None
    live_avail  = bm_live.get("avail") if "avail" in bm_live else None
    bulk_avail  = bulk_info.get("avail") if bulk_info.get("found") else None
    discrepancy = bool(
        cache_avail == 0 and (
            (live_avail is not None and live_avail > 0) or
            (bulk_avail is not None and bulk_avail > 0)
        )
    )

    return JSONResponse({
        "sku": bm_key,
        "conditions_used": _conditions_used,
        "cache": cache_info,
        "bulk_cache_gr":  bulk_info_gr,
        "bulk_cache_all": bulk_info_all,
        "bulk_cache": bulk_info,  # el relevante según conditions_used (compatibilidad)
        "bm_live": bm_live,
        "discrepancy": discrepancy,
        "summary": (
            f"DISCREPANCIA: caché={cache_avail} pero BM_live={live_avail}, bulk={bulk_avail}"
            if discrepancy else
            f"OK: caché={cache_avail}, BM_live={live_avail}, bulk={bulk_avail}"
        ),
    })


@app.get("/api/diag/cache-health")
async def diag_cache_health(token: str = ""):
    """Diagnóstico: salud general del caché BM.
    Muestra total de SKUs, ceros verificados, expirados, y top SKUs sospechosos
    (caché=0 pero aparecen en bulk con stock > 0).
    """
    if token != _DIAG_TOKEN:
        return JSONResponse({"error": "token inválido"}, status_code=403)

    now = _time.time()
    total = len(_bm_stock_cache)
    expired = 0
    verified_zeros = 0
    suspicious = []
    # Usar GR bulk como fuente para detectar discrepancias (cubre la mayoría de SKUs)
    bulk_rows_map = {}
    if _bm_bulk_gr_cache:
        _, _gr_rows_health = _bm_bulk_gr_cache
        for _r in (_gr_rows_health or []):
            _rsk = (_r.get("SKU") or "").upper().strip()
            if _rsk:
                bulk_rows_map[_rsk] = _r
                bulk_rows_map[_extract_base_sku(_rsk)] = _r  # también por base

    for bm_key, (ts, data) in _bm_stock_cache.items():
        age = now - ts
        if age >= _BM_CACHE_TTL:
            expired += 1
        if data.get("avail_total", 0) == 0 and data.get("_v") and age < _BM_CACHE_TTL:
            verified_zeros += 1
            bulk_match = bulk_rows_map.get(bm_key.upper())
            if bulk_match and int(bulk_match.get("AvailableQTY") or 0) > 0:
                suspicious.append({
                    "sku": bm_key,
                    "cache_avail": 0,
                    "bulk_avail": int(bulk_match.get("AvailableQTY") or 0),
                    "age_s": round(age),
                })

    suspicious.sort(key=lambda x: x["bulk_avail"], reverse=True)
    gr_age_s  = round(now - _bm_bulk_gr_cache[0])  if _bm_bulk_gr_cache  else None
    all_age_s = round(now - _bm_bulk_all_cache[0]) if _bm_bulk_all_cache else None

    return JSONResponse({
        "cache_total_skus": total,
        "cache_expired": expired,
        "cache_verified_zeros": verified_zeros,
        "suspicious_zeros": len(suspicious),
        "top_suspicious": suspicious[:20],
        "bulk_gr_age_s":   gr_age_s,
        "bulk_gr_rows":    len(_bm_bulk_gr_cache[1])  if _bm_bulk_gr_cache  else 0,
        "bulk_all_age_s":  all_age_s,
        "bulk_all_rows":   len(_bm_bulk_all_cache[1]) if _bm_bulk_all_cache else 0,
    })


@app.get("/api/debug/item-stock")
async def debug_item_stock(item_id: str = "", key: str = "", live: int = 0):
    """Diagnóstico: muestra stock ML (DB) + BM caché para un item_id (MLM...).
    Requiere ?key=<DEBUG_KEY> — acceso sin sesión para consultas externas.
    ?live=1 — además consulta ML API en tiempo real para obtener sub_status fresco."""
    if key != _DEBUG_KEY:
        return JSONResponse({"error": "key inválida"}, status_code=401)
    if not item_id:
        return JSONResponse({"error": "item_id requerido"}, status_code=400)
    iid = item_id.strip().upper()

    # --- Leer de ml_listings DB ---
    from app.services.token_store import get_ml_listings_all_accounts as _get_all_listings
    import json as _j, time as _t2
    db_item = None
    db_age_s = None
    try:
        rows = await _get_all_listings()
        for r in rows:
            if str(r.get("item_id", "")).upper() == iid:
                db_item = r
                break
    except Exception:
        pass

    ml_qty_db = None
    title = ""
    status_ml = ""
    sku = ""
    variations_db = []
    if db_item:
        ml_qty_db = db_item.get("available_qty", 0)
        title = db_item.get("title", "")
        status_ml = db_item.get("status", "")
        sku = db_item.get("sku", "")
        synced_at = db_item.get("synced_at", 0)
        db_age_s = round(_t2.time() - float(synced_at)) if synced_at else None
        # Variaciones desde data_json
        dj = db_item.get("data_json", "")
        if dj:
            try:
                body = _j.loads(dj)
                for v in body.get("variations", []):
                    v_sku = ""
                    for attr in v.get("attributes", []):
                        if attr.get("id") in ("SELLER_SKU", "seller_custom_field"):
                            v_sku = attr.get("value_name", "")
                            break
                    combos = [f"{a.get('name')}: {a.get('value_name')}" for a in v.get("attribute_combinations", [])]
                    variations_db.append({
                        "variation_id": v.get("id"),
                        "sku": v_sku,
                        "combo": ", ".join(combos),
                        "ml_qty": v.get("available_quantity", 0),
                    })
            except Exception:
                pass

    # --- Leer BM caché ---
    bm_avail = None
    bm_reserve = None
    bm_cache_age_s = None
    bm_status = "sin datos"
    if sku:
        bm_key = normalize_to_bm_sku(sku.upper())
        cached = _bm_stock_cache.get(bm_key)
        if cached:
            ts, bdata = cached
            bm_avail = bdata.get("avail_total", 0)
            bm_reserve = bdata.get("reserved_total", 0)
            bm_cache_age_s = round(_t2.time() - ts)
            bm_status = "OK" if bdata.get("_v") else "sin verificar"
        # También buscar en variaciones
        for vd in variations_db:
            v_sku = (vd.get("sku") or "").upper()
            if not v_sku:
                continue
            vkey = normalize_to_bm_sku(v_sku)
            vc = _bm_stock_cache.get(vkey)
            if vc:
                _vts, _vd = vc
                vd["bm_avail"] = _vd.get("avail_total", 0)
                vd["bm_reserve"] = _vd.get("reserved_total", 0)

    # --- Alertas activas ---
    alerts = []
    client = await get_meli_client()
    uid = client.user_id if client else None
    if client:
        await client.close()
    issues_key = f"stock_issues:{uid}:t10" if uid else None
    issues_entry = issues_key and _stock_issues_cache.get(issues_key)
    if issues_entry:
        for section in ("restock", "oversell_risk", "activate", "critical", "full_no_stock", "stagnant", "price_risk"):
            if any(str(p.get("id", "")).upper() == iid for p in issues_entry[1].get(section, [])):
                alerts.append(section)

    if not db_item:
        return JSONResponse({"found": False, "item_id": iid,
                             "message": "No encontrado en DB local — puede estar fuera de sync"})

    # --- Motivo de pausa / condición del listing ---
    pause_reasons = []
    health_score = None
    warnings_ml = []
    listing_health = {}
    dj_raw = db_item.get("data_json", "")
    if dj_raw:
        try:
            _body = _j.loads(dj_raw)
            # sub_status: ["out_of_stock", "paused_by_seller", "under_review", etc.]
            pause_reasons = _body.get("sub_status") or []
            # health: {status, color, level_id, reasons[]}
            _h = _body.get("health") or {}
            health_score = _h.get("status")
            listing_health = {
                "status": _h.get("status"),
                "color": _h.get("color"),
                "level": _h.get("level_id"),
                "reasons": _h.get("reasons") or [],
            }
            # warnings: [{type, message}]
            warnings_ml = [
                {"type": w.get("type"), "message": w.get("message")}
                for w in (_body.get("warnings") or [])
            ]
            # catalog_listing puede requerir acción especial
            if _body.get("catalog_listing") and status_ml == "paused":
                pause_reasons = pause_reasons or ["catalog_listing_paused"]
        except Exception:
            pass

    # --- Consulta ML API en tiempo real (solo si ?live=1) ---
    live_data = None
    if live:
        try:
            import httpx as _httpx
            _acc_id = db_item.get("account_id", "") if db_item else ""
            _ml_tok = None
            if _acc_id:
                _live_client = await get_meli_client(user_id=str(_acc_id))
                if _live_client:
                    _ml_tok = _live_client.access_token
                    await _live_client.close()
            if _ml_tok:
                async with _httpx.AsyncClient(timeout=10.0) as _hc:
                    _r = await _hc.get(
                        f"https://api.mercadolibre.com/items/{iid}",
                        headers={"Authorization": f"Bearer {_ml_tok}"},
                        params={"include_attributes": "all"},
                    )
                if _r.status_code == 200:
                    _lb = _r.json()
                    if not isinstance(_lb, dict):
                        live_data = {"error": f"ML API tipo inesperado: {type(_lb).__name__}", "raw": str(_lb)[:200]}
                    else:
                        _lh = _lb.get("health") or {}
                        if not isinstance(_lh, dict):
                            _lh = {}
                        _warns = _lb.get("warnings") or []
                        live_data = {
                            "status": _lb.get("status"),
                            "sub_status": _lb.get("sub_status") or [],
                            "catalog_listing": _lb.get("catalog_listing", False),
                            "catalog_product_id": _lb.get("catalog_product_id"),
                            "available_quantity": _lb.get("available_quantity"),
                            "health": {
                                "status": _lh.get("status"),
                                "color": _lh.get("color"),
                                "level": _lh.get("level_id"),
                                "reasons": _lh.get("reasons") or [],
                            },
                            "warnings": [
                                {"type": w.get("type"), "message": w.get("message")}
                                for w in _warns if isinstance(w, dict)
                            ],
                        }
                        # Detectar causa específica de pausa
                        _cause = "desconocido"
                        _sub = live_data["sub_status"]
                        _cat = live_data["catalog_listing"]
                        _cat_id = live_data["catalog_product_id"]
                        if _cat and _cat_id and _lb.get("status") == "paused":
                            _cause = f"catalog_required — producto catálogo: {_cat_id}"
                        elif "out_of_stock" in _sub:
                            _cause = "sin_stock"
                        elif "paused_by_seller" in _sub:
                            _cause = "pausado_por_vendedor"
                        elif "under_review" in _sub:
                            _cause = "en_revision"
                        elif _sub:
                            _cause = ", ".join(_sub)
                        live_data["pause_cause"] = _cause
                else:
                    live_data = {"error": f"ML API HTTP {_r.status_code}", "body": _r.text[:300]}
            else:
                live_data = {"error": "token no encontrado para esta cuenta"}
        except Exception as _le:
            import traceback as _tb
            live_data = {"error": str(_le)[:200], "traceback": _tb.format_exc()[-600:]}

    return JSONResponse({
        "found": True,
        "item_id": iid,
        "title": title,
        "status": status_ml,
        "sku": sku,
        "ml_qty_db": ml_qty_db,
        "db_age_min": round(db_age_s / 60, 1) if db_age_s is not None else None,
        "bm_avail": bm_avail,
        "bm_reserve": bm_reserve,
        "bm_cache_age_min": round(bm_cache_age_s / 60, 1) if bm_cache_age_s is not None else None,
        "bm_status": bm_status,
        "pause_reasons": pause_reasons,
        "health": listing_health,
        "warnings": warnings_ml,
        "variations": variations_db,
        "alerts": alerts if alerts else ["ninguna"],
        **({"live": live_data} if live_data is not None else {}),
    })


@app.get("/api/debug/test-merchant")
async def debug_test_merchant(item_id: str = "", key: str = "", execute: int = 0):
    """Prueba si ML API permite cambiar logistic_type a not_specified en un item FULL.
    execute=0 → solo lee config actual (sin cambios)
    execute=1 → intenta el PUT real (¡modifica el listing!)
    """
    if key != _DEBUG_KEY:
        return JSONResponse({"error": "key inválida"}, status_code=401)
    if not item_id:
        return JSONResponse({"error": "item_id requerido"}, status_code=400)
    iid = item_id.strip().upper()
    try:
        import httpx as _hx
        from app.services import token_store as _ts2
        # Obtener token
        _all_toks2 = await _ts2.get_all_tokens()
        _tok2 = None
        _acc2 = None
        # Buscar en ml_listings qué cuenta tiene este item
        _rows2 = await _ts2.get_ml_listings_all_accounts()
        for _r2 in _rows2:
            if str(_r2.get("item_id","")).upper() == iid:
                _acc2 = str(_r2.get("account_id",""))
                break
        if _acc2:
            _cl2 = await get_meli_client(user_id=_acc2)
            if _cl2:
                _tok2 = _cl2.access_token
                await _cl2.close()
        if not _tok2:
            return JSONResponse({"error": "token no encontrado"})

        async with _hx.AsyncClient(timeout=15.0) as _hc2:
            # 1. Leer config actual
            _rget = await _hc2.get(
                f"https://api.mercadolibre.com/items/{iid}",
                headers={"Authorization": f"Bearer {_tok2}"},
            )
            _item2 = _rget.json() if _rget.status_code == 200 else {}
            _ship2 = _item2.get("shipping") or {}
            _current = {
                "status": _item2.get("status"),
                "logistic_type": _ship2.get("logistic_type"),
                "mode": _ship2.get("mode"),
                "free_shipping": _ship2.get("free_shipping"),
                "available_quantity": _item2.get("available_quantity"),
                "catalog_listing": _item2.get("catalog_listing"),
            }

            if not execute:
                return JSONResponse({"mode": "readonly", "item_id": iid, "current": _current,
                                     "note": "Pasa ?execute=1 para intentar el cambio real"})

            # 2. Intentar cambio a not_specified
            _rput = await _hc2.put(
                f"https://api.mercadolibre.com/items/{iid}",
                headers={"Authorization": f"Bearer {_tok2}", "Content-Type": "application/json"},
                json={"shipping": {"logistic_type": "not_specified"}},
            )
            return JSONResponse({
                "mode": "execute",
                "item_id": iid,
                "current_before": _current,
                "put_status": _rput.status_code,
                "put_response": _rput.json() if _rput.headers.get("content-type","").startswith("application/json") else _rput.text[:500],
            })
    except Exception as _e2:
        return JSONResponse({"error": str(_e2)[:300]})


@app.post("/api/stock/force-prewarm")
async def force_prewarm(request: Request, full_clear: bool = False):
    """Fuerza un prewarm fresco: limpia caché BM y recalcula stock issues.
    full_clear=True → limpia TODO el cache BM (útil tras cambios de lógica de condiciones).
    full_clear=False (default) → solo limpia entradas stale/fallidas.
    Solo permitido para roles admin y editor.
    """
    _du = getattr(request.state, "dashboard_user", None)
    if not _du or _du.get("role") not in ("admin", "editor"):
        return JSONResponse({"error": "No autorizado — solo admin o editor pueden actualizar BM"}, status_code=403)
    global _prewarm_task, _prewarm_source
    if _prewarm_running:
        return JSONResponse({"status": "already_running"})
    _prewarm_source = "manual"  # marcar como manual para el historial BM
    # Capturar user_id activo para precalentar la cuenta correcta
    _fp_client = await get_meli_client()
    _fp_uid = _fp_client.user_id if _fp_client else None
    if _fp_client:
        await _fp_client.close()

    if full_clear:
        # Limpiar TODO el cache BM para que el prewarm re-fetche con las condiciones actuales
        stale_cleared = len(_bm_stock_cache)
        _bm_stock_cache.clear()
    else:
        # Solo eliminar entradas stale:
        # 1) total=0 AND avail=0 sin _v → fetch fallido que guardó ceros
        # 2) total>0 AND avail=0 → WH ok pero get_stock_with_reserve falló (fallo parcial)
        stale_cleared = 0
        for sku in list(_bm_stock_cache.keys()):
            ts, data = _bm_stock_cache[sku]
            all_zero_unverified = (data.get("total", 0) == 0 and data.get("avail_total", 0) == 0 and not data.get("_v"))
            partial_failure = (data.get("total", 0) > 0 and data.get("avail_total", 0) == 0)
            if all_zero_unverified or partial_failure:
                del _bm_stock_cache[sku]
                stale_cleared += 1

    # Limpiar stock_issues_cache para forzar recalculo completo
    _stock_issues_cache.clear()

    _prewarm_task = asyncio.create_task(_prewarm_caches(user_id=_fp_uid))
    return JSONResponse({"status": "started", "stale_cleared": stale_cleared, "full_clear": full_clear})


@app.get("/api/stock/multi-sync/preview")
async def multi_sync_preview():
    """Dry-run: calcula cambios planificados sin ejecutar nada en ML/Amazon."""
    from app.services.stock_sync_multi import preview_multi_stock_sync, get_sync_status
    if get_sync_status()["running"]:
        return JSONResponse({"status": "already_running", "changes": []})
    result = await preview_multi_stock_sync()
    return JSONResponse(result)


@app.post("/api/stock/multi-sync/trigger")
async def multi_sync_trigger():
    """Dispara sync manual: sync BM→ML/Amazon + fuerza prewarm fresco."""
    from app.services.stock_sync_multi import run_multi_stock_sync, get_sync_status
    if get_sync_status()["running"]:
        return JSONResponse({"status": "already_running"}, status_code=202)

    async def _run_sync_and_alerts():
        # 1. Sync multi-plataforma BM → ML + Amazon (actualiza stock en las plataformas)
        await run_multi_stock_sync()
        # 2. Actualizar _bm_stock_cache con datos frescos del sync (avail_total)
        #    Evita que el prewarm re-fetche todos los SKUs desde cero.
        try:
            from app.services.stock_sync_multi import get_last_bm_stock
            for _sku, _avail in get_last_bm_stock().items():
                _existing = _bm_stock_cache.get(normalize_to_bm_sku(_sku))
                _d = _existing[1].copy() if _existing else {
                    "mty": 0, "cdmx": 0, "tj": 0, "total": 0, "reserved_total": 0
                }
                _d["avail_total"] = int(_avail)
                _bm_stock_cache[normalize_to_bm_sku(_sku)] = (_time.time(), _d)
        except Exception:
            pass
        # 3. Encolar prewarm fresco (si ya hay uno corriendo, se ejecutará al terminar)
        asyncio.create_task(_prewarm_caches())
        # 4. Refrescar alertas de sobreventa
        try:
            accounts = await token_store.get_all_tokens()
            for acc in accounts:
                uid = acc.get("user_id", "")
                if uid:
                    await _run_stock_sync_for_user(uid)
        except Exception:
            pass

    asyncio.create_task(_run_sync_and_alerts())
    return {"status": "triggered"}


@app.get("/api/listings/orphans")
async def listings_orphans_endpoint(request: Request, platform: str = None, account_id: str = None):
    """Retorna listings presentes en DB pero eliminados de la plataforma.
    Accesible para cualquier usuario autenticado — filtrado por account_id."""
    _du = getattr(request.state, "dashboard_user", None) or {}
    if not _du.get("role"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    rows = await token_store.get_orphan_listings(platform=platform, account_id=account_id)
    import time as _t
    now = _t.time()
    for r in rows:
        age_s = now - r.get("detected_at", now)
        r["age_str"] = (
            f"hace {int(age_s)}s" if age_s < 120
            else f"hace {int(age_s//60)}min" if age_s < 3600
            else f"hace {int(age_s//3600)}h"
        )
    return JSONResponse({"orphans": rows, "count": len(rows)})


@app.delete("/api/listings/orphans")
async def delete_orphans_endpoint(request: Request):
    """Elimina listings huérfanos de DB local (orphan_listings + ml_listings/amazon_listings).
    Body: {ids: [1,2,3]} — IDs de orphan_listings.
    Accesible para cualquier usuario autenticado — solo borra de DB local, no de la plataforma."""
    _du = getattr(request.state, "dashboard_user", None) or {}
    if not _du.get("role"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body JSON inválido"}, status_code=400)
    ids = body.get("ids", [])
    if not ids or not isinstance(ids, list):
        return JSONResponse({"error": "ids requerido (lista de enteros)"}, status_code=400)
    deleted = await token_store.delete_orphan_listings([int(i) for i in ids])
    return JSONResponse({"deleted": deleted})


@app.post("/api/stock/multi-sync/trigger-single")
async def trigger_single_account_sync(request: Request):
    """Dispara el sync de stock para una sola cuenta (ML o Amazon).
    Body: {platform: "ml"|"amz", account_id: "..."}
    Usa el mismo circuit-breaker BM que el sync global.
    Solo actualiza la entrada de esa cuenta en el estado por cuenta.
    """
    from app.services.stock_sync_multi import run_single_account_stock_sync, get_sync_status
    _du = getattr(request.state, "dashboard_user", None)
    if not _du or _du.get("role") not in ("admin", "editor"):
        return JSONResponse({"error": "No autorizado — solo admin o editor"}, status_code=403)
    if get_sync_status()["running"]:
        return JSONResponse({"status": "already_running"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body JSON inválido"}, status_code=400)
    platform   = body.get("platform", "").strip()
    account_id = body.get("account_id", "").strip()
    if not platform or not account_id or platform not in ("ml", "amz"):
        return JSONResponse({"error": "platform (ml|amz) y account_id son requeridos"}, status_code=400)
    asyncio.create_task(run_single_account_stock_sync(platform, account_id))
    return JSONResponse({"status": "started", "platform": platform, "account_id": account_id})


@app.get("/api/cannibalization")
async def get_cannibalization():
    """Retorna SKUs con canibalización del último sync."""
    from app.services.stock_sync_multi import get_cannibalization_data
    data = get_cannibalization_data()
    return {"count": len(data), "items": data}


@app.get("/api/stock/nocturnal-protection")
async def nocturnal_protection_get():
    """Estado de la protección nocturna de stock."""
    from app.services.stock_sync_multi import get_nocturnal_protection
    return get_nocturnal_protection()


@app.post("/api/stock/nocturnal-protection")
async def nocturnal_protection_set(request: Request):
    """Activar/desactivar protección nocturna. Body: {\"enabled\": true/false}"""
    from app.services.stock_sync_multi import set_nocturnal_protection
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    set_nocturnal_protection(enabled)
    return {"enabled": enabled}


@app.get("/api/stock/multi-sync/history")
async def multi_sync_history(limit: int = Query(20, ge=1, le=100)):
    """Historial de ciclos de sync (últimos N)."""
    runs = await token_store.get_multi_sync_last_runs(limit=limit)
    return {"runs": runs}


@app.post("/api/stock/multi-sync/rules")
async def set_platform_rule(request: Request):
    """
    Define si un SKU está habilitado para una plataforma (cuenta activa).
    Body: {"sku": "SNFN000941", "platform_id": "ml_123456", "enabled": true}
    platform_id: "ml_{user_id}" o "amz_{seller_id}"
    """
    from app.services.meli_client import _active_user_id as _ctx
    _uid = str(_ctx.get() or "")
    body = await request.json()
    sku         = (body.get("sku") or "").strip().upper()
    platform_id = (body.get("platform_id") or "").strip()
    enabled     = bool(body.get("enabled", True))
    if not sku or not platform_id:
        return JSONResponse({"error": "sku y platform_id requeridos"}, status_code=400)
    await token_store.set_sku_platform_rule(_uid, sku, platform_id, enabled)
    return {"ok": True, "sku": sku, "platform_id": platform_id, "enabled": enabled}


@app.get("/api/stock/multi-sync/rules")
async def get_platform_rules():
    """Lista las reglas de plataforma por SKU para la cuenta activa."""
    from app.services.meli_client import _active_user_id as _ctx
    _uid = str(_ctx.get() or "")
    rules = await token_store.get_all_sku_platform_rules(user_id=_uid)
    return {"rules": rules}


@app.get("/api/config/auto-pause")
async def get_auto_pause():
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_session"}, status_code=401)
    return {"enabled": _auto_zero_enabled.get(client.user_id, False)}


@app.post("/api/config/auto-pause")
async def set_auto_pause(request: Request):
    client = await get_meli_client()
    if not client:
        return JSONResponse({"error": "no_session"}, status_code=401)
    body = await request.json()
    _auto_zero_enabled[client.user_id] = bool(body.get("enabled", False))
    return {"enabled": _auto_zero_enabled[client.user_id]}


@app.get("/api/config/fx-rate")
async def get_fx_rate():
    client = await get_meli_client()
    meli_rate = 0.0
    try:
        if client:
            fx_data = await client.get("/currency_conversions/search", params={"from": "USD", "to": "MXN"})
            meli_rate = fx_data.get("ratio", 0.0)
    except Exception:
        pass
    return {
        "manual_rate": _manual_fx_rate,
        "meli_rate": round(meli_rate, 4),
        "active_rate": _manual_fx_rate if _manual_fx_rate > 0 else meli_rate,
        "is_manual": _manual_fx_rate > 0,
    }


@app.post("/api/config/fx-rate")
async def set_fx_rate(request: Request):
    global _manual_fx_rate
    body = await request.json()
    rate = float(body.get("rate", 0) or 0)
    _manual_fx_rate = max(0.0, rate)
    return {"manual_rate": _manual_fx_rate, "is_manual": _manual_fx_rate > 0}


# ===========================
# RETORNOS / DEVOLUCIONES
# ===========================

_bm_retail_cache: tuple[float, dict] | None = None  # (ts, {sku: retail_usd})
_BM_RETAIL_TTL = 600  # 10 min


async def _get_bm_retail_prices() -> dict:
    """Returns {bm_sku: retail_price_usd} with 10-min cache. Uses get_bulk_stock()."""
    global _bm_retail_cache
    import time as _t2
    if _bm_retail_cache and (_t2.time() - _bm_retail_cache[0]) < _BM_RETAIL_TTL:
        return _bm_retail_cache[1]
    try:
        from app.services.binmanager_client import BinManagerClient
        bm = BinManagerClient()
        if not await bm.login():
            return {}
        items = await bm.get_bulk_stock()
        price_map: dict = {}
        for item in items:
            sku = (item.get("SKU") or "").strip()
            if sku:
                price = float(item.get("LastRetailPricePurchaseHistory") or 0)
                if price > 0:
                    price_map[sku] = price
        _bm_retail_cache = (_t2.time(), price_map)
        return price_map
    except Exception as _e:
        logger.warning(f"[BM-RETAIL] Error al obtener precios: {_e}")
        return {}


@app.get("/returns", response_class=HTMLResponse)
async def returns_page(request: Request):
    user = await get_current_user()
    if not user:
        return templates.TemplateResponse(request, "no_session.html", {})
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "returns.html", {        "user": user,
        "active": "returns",
        **ctx
    })


@app.get("/partials/returns-summary", response_class=HTMLResponse)
async def returns_summary_partial(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        df = date_from or None
        dt = date_to or None

        # Fetch all PDD claims in the period
        all_claims = await client.fetch_all_claims(date_from=df, date_to=dt)
        pdd_claims = [c for c in all_claims
                      if str(c.get("reason_id", "")).upper().startswith("PDD")]

        total = len(pdd_claims)
        opened = sum(1 for c in pdd_claims if c.get("status") == "opened")
        closed = total - opened

        # Count urgent (opened with due_date < 24h)
        from datetime import datetime, timezone
        urgent = 0
        for c in pdd_claims:
            if c.get("status") != "opened":
                continue
            for player in c.get("players", []):
                if player.get("role") == "respondent":
                    for a in player.get("available_actions", []):
                        if a.get("mandatory") and a.get("due_date"):
                            try:
                                due_dt = datetime.fromisoformat(
                                    a["due_date"].replace("Z", "+00:00"))
                                remaining_h = (due_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                                if remaining_h < 24:
                                    urgent += 1
                            except Exception:
                                pass
                    break

        # Total orders for return rate calculation
        total_orders = 0
        try:
            orders_data = await client.get(
                "/orders/search",
                params={"seller": (await client.get_user_info()).get("id", ""),
                        "sort": "date_asc", "offset": 0, "limit": 1,
                        **({"date_from": df} if df else {}),
                        **({"date_to": dt} if dt else {})}
            )
            total_orders = orders_data.get("paging", {}).get("total", 0)
        except Exception:
            pass

        return_rate = (total / total_orders * 100) if total_orders > 0 else 0.0

        # ── Valor monetario de retornos ──────────────────────────────────
        valor_mxn = 0.0
        costo_usd = 0.0
        try:
            oids = list({str(c.get("resource_id", "")) for c in pdd_claims
                         if c.get("resource") == "order" and c.get("resource_id")})[:50]
            if oids:
                sem_v = asyncio.Semaphore(5)
                async def _fetch_order_val(oid):
                    async with sem_v:
                        try:
                            order = await client.get(f"/orders/{oid}")
                            items_o = order.get("order_items", [])
                            sku = ""
                            if items_o:
                                item_d = items_o[0].get("item", {})
                                sku = item_d.get("seller_custom_field") or ""
                            return float(order.get("total_amount") or 0), sku
                        except Exception:
                            return 0.0, ""
                vals = await asyncio.gather(*[_fetch_order_val(o) for o in oids],
                                            return_exceptions=True)
                skus_returned = []
                for v in vals:
                    if isinstance(v, tuple):
                        valor_mxn += v[0]
                        if v[1]:
                            skus_returned.append(normalize_to_bm_sku(v[1]))
                # BM cost
                bm_prices = await _get_bm_retail_prices()
                costo_usd = sum(bm_prices.get(s, 0.0) for s in skus_returned if s)
        except Exception as _ev:
            logger.warning(f"[RETURNS-SUMMARY] valor error: {_ev}")

        summary = SimpleNamespace(
            total=total,
            opened=opened,
            closed=closed,
            urgent=urgent,
            total_orders=total_orders,
            return_rate=round(return_rate, 2),
            valor_mxn=round(valor_mxn, 2),
            costo_usd=round(costo_usd, 2),
        )

        return templates.TemplateResponse(request, "partials/returns_summary.html", {            "summary": summary,
            "date_from": date_from,
            "date_to": date_to,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando resumen de retornos: {e}</p>')
    finally:
        await client.close()


@app.get("/partials/returns-table", response_class=HTMLResponse)
async def returns_table_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query(""),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
):
    client = await get_meli_client()
    if not client:
        return HTMLResponse("<p>Error: No autenticado</p>")
    try:
        df = date_from or None
        dt = date_to or None
        params_status = status if status else None

        # Fetch all PDD claims, then paginate client-side for accuracy
        all_claims = await client.fetch_all_claims(status=params_status,
                                                    date_from=df, date_to=dt)
        pdd_all = [c for c in all_claims
                   if str(c.get("reason_id", "")).upper().startswith("PDD")]

        total_pdd = len(pdd_all)
        paging = {"total": total_pdd, "offset": offset, "limit": limit}
        raw_claims = pdd_all[offset:offset + limit]

        # Refresh status of opened claims via detail endpoint
        opened_ids = [c for c in raw_claims if c.get("status") == "opened"]
        if opened_ids:
            sem_refresh = asyncio.Semaphore(5)

            async def _refresh_status(claim):
                async with sem_refresh:
                    try:
                        detail = await client.get_claim_detail(str(claim.get("id", "")))
                        if isinstance(detail, dict) and detail.get("status"):
                            claim["status"] = detail["status"]
                            if detail.get("stage"):
                                claim["stage"] = detail["stage"]
                            if detail.get("players"):
                                claim["players"] = detail["players"]
                    except Exception:
                        pass

            await asyncio.gather(*[_refresh_status(c) for c in opened_ids[:30]],
                                 return_exceptions=True)

        # Batch fetch order info for product titles
        order_ids = list({str(c.get("resource_id", "")) for c in raw_claims
                          if c.get("resource") == "order" and c.get("resource_id")})
        orders_map = {}
        if order_ids:
            for oid in order_ids[:20]:
                try:
                    order = await client.get(f"/orders/{oid}")
                    oi = order.get("order_items", [])
                    if oi:
                        item = oi[0].get("item", {})
                        orders_map[oid] = {
                            "title": item.get("title", ""),
                            "price": order.get("total_amount", 0),
                            "item_id": item.get("id", ""),
                        }
                except Exception:
                    pass

        # Fetch claim messages for all claims (opened + closed for analysis)
        sem = asyncio.Semaphore(5)
        claim_messages_map = {}

        async def _fetch_msgs(claim_id):
            async with sem:
                try:
                    msgs = await client.get_claim_messages(str(claim_id))
                    if isinstance(msgs, list):
                        return str(claim_id), msgs
                    return str(claim_id), msgs.get("results", msgs.get("messages", []))
                except Exception:
                    return str(claim_id), []

        msg_results = await asyncio.gather(
            *[_fetch_msgs(c.get("id", "")) for c in raw_claims[:20]],
            return_exceptions=True
        )
        for r in msg_results:
            if isinstance(r, tuple):
                claim_messages_map[r[0]] = r[1]

        REASON_MAP = {
            "PDD": ("Producto defectuoso o diferente", "defective"),
        }

        from datetime import datetime, timezone

        enriched = []
        for c in raw_claims:
            date_created = c.get("date_created", "")
            elapsed_str, elapsed_secs = _elapsed_str(date_created)
            days_open = elapsed_secs // 86400 if elapsed_secs else 0

            c_status = c.get("status", "")
            stage = c.get("stage", "")

            # Due date
            due_date_raw = ""
            due_date = ""
            for player in c.get("players", []):
                if player.get("role") == "respondent":
                    for a in player.get("available_actions", []):
                        if a.get("mandatory") and a.get("due_date"):
                            due_date_raw = a["due_date"]
                            due_date = due_date_raw[:10]
                            break
                    break

            # Countdown hours
            countdown_hours = None
            if due_date_raw and c_status == "opened":
                try:
                    due_dt = datetime.fromisoformat(due_date_raw.replace("Z", "+00:00"))
                    remaining = due_dt - datetime.now(timezone.utc)
                    countdown_hours = max(0, round(remaining.total_seconds() / 3600, 1))
                except Exception:
                    pass

            # Urgency
            if c_status == "opened":
                if countdown_hours is not None:
                    urgency = "red" if countdown_hours < 8 else ("yellow" if countdown_hours < 24 else "green")
                else:
                    urgency = "red" if days_open > 7 else ("yellow" if days_open > 3 else "green")
            else:
                urgency = "gray"

            reason_id = c.get("reason_id", "")
            reason_prefix = reason_id[:3].upper() if reason_id else ""
            reason_info = REASON_MAP.get(reason_prefix, ("Retorno / Devolucion", "defective"))
            reason_desc = reason_info[0]

            resource_id = str(c.get("resource_id", ""))
            order_info = orders_map.get(resource_id, {})

            # Conversation messages
            claim_id_str = str(c.get("id", ""))
            raw_msgs = claim_messages_map.get(claim_id_str, [])
            conversation = []
            buyer_complaint = ""
            for msg in raw_msgs:
                sender = msg.get("sender_role", msg.get("role", ""))
                text = msg.get("text", msg.get("message", ""))
                msg_date = msg.get("date_created", "")
                if sender == "complainant" and text and not buyer_complaint:
                    buyer_complaint = text
                conversation.append({
                    "sender": sender,
                    "text": text,
                    "date": msg_date[:16].replace("T", " ") if msg_date else "",
                })

            # Suggestions
            suggestions = []
            if c_status == "opened":
                if countdown_hours is not None and countdown_hours < 24:
                    suggestions.append("URGENTE: Responde antes de " + due_date + " para evitar penalizacion")
                suggestions.append("Solicita fotos del defecto o diferencia al comprador")
                suggestions.append("Ofrece solucion: reemplazo, devolucion o descuento")
                if stage == "dispute":
                    suggestions.append("Responde al mediador de MeLi con evidencia clara")
            else:
                suggestions.append("Retorno resuelto — revisa si el producto tiene un problema recurrente")

            enriched.append(SimpleNamespace(
                id=c.get("id", ""),
                order_id=resource_id,
                status=c_status,
                stage=stage,
                date_created=date_created[:10] if date_created else "-",
                _sort_date=date_created or "",
                elapsed=elapsed_str,
                days_open=days_open,
                urgency=urgency,
                countdown_hours=countdown_hours,
                reason_desc=reason_desc,
                reason_id=reason_id,
                product_title=order_info.get("title", ""),
                product_price=order_info.get("price", 0),
                item_id=str(order_info.get("item_id", "")),
                due_date=due_date,
                buyer_complaint=buyer_complaint,
                conversation=conversation,
                suggestions=suggestions,
                tracking={},
            ))

        # Sort: urgency first, then by date desc
        _urgency_order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
        enriched.sort(key=lambda c: (_urgency_order.get(c.urgency, 3), not c._sort_date, c._sort_date))

        return templates.TemplateResponse(request, "partials/returns_table.html", {            "returns": enriched,
            "paging": paging,
            "offset": offset,
            "limit": limit,
            "status": status,
        })
    except Exception as e:
        return HTMLResponse(f'<p class="text-center py-4 text-red-500">Error cargando retornos: {e}</p>')
    finally:
        await client.close()


@app.get("/api/returns/analysis")
async def returns_analysis(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    limit: int = Query(5, ge=1, le=20),
):
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        df = date_from or None
        dt = date_to or None

        all_claims = await client.fetch_all_claims(date_from=df, date_to=dt)
        pdd_claims = [c for c in all_claims
                      if str(c.get("reason_id", "")).upper().startswith("PDD")]

        total = len(pdd_claims)
        opened = sum(1 for c in pdd_claims if c.get("status") == "opened")
        closed = total - opened

        order_ids = list({str(c.get("resource_id", "")) for c in pdd_claims
                          if c.get("resource") == "order" and c.get("resource_id")})
        sem = asyncio.Semaphore(5)

        async def _fetch_order_info(oid):
            async with sem:
                try:
                    order = await client.get(f"/orders/{oid}")
                    oi = order.get("order_items", [])
                    if oi:
                        item = oi[0].get("item", {})
                        return oid, {"title": item.get("title", ""), "item_id": str(item.get("id", ""))}
                except Exception:
                    pass
                return oid, {"title": "", "item_id": ""}

        info_results = await asyncio.gather(
            *[_fetch_order_info(oid) for oid in order_ids[:40]],
            return_exceptions=True
        )
        order_info_map = {}
        for r in info_results:
            if isinstance(r, tuple):
                order_info_map[r[0]] = r[1]

        product_counts: dict = {}
        for c in pdd_claims:
            oid = str(c.get("resource_id", ""))
            info = order_info_map.get(oid, {})
            title = info.get("title") or "Producto desconocido"
            item_id = info.get("item_id", "")
            if title not in product_counts:
                product_counts[title] = {"title": title, "item_id": item_id, "count": 0}
            product_counts[title]["count"] += 1

        top_products = sorted(product_counts.values(), key=lambda x: x["count"], reverse=True)[:limit]

        return {
            "total": total,
            "by_status": {"opened": opened, "closed": closed},
            "top_products": top_products,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.close()


@app.get("/api/returns/top-products")
async def returns_top_products(
    date_from: str = Query("", description="Period A start YYYY-MM-DD"),
    date_to: str = Query("", description="Period A end YYYY-MM-DD"),
    compare_from: str = Query("", description="Period B start YYYY-MM-DD"),
    compare_to: str = Query("", description="Period B end YYYY-MM-DD"),
    limit: int = Query(10, ge=1, le=20),
):
    """Top N returned products with period comparison, reason breakdown, and action recommendations."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        df_a = date_from or None
        dt_a = date_to or None
        df_b = compare_from or None
        dt_b = compare_to or None

        # PDD sub-reason labels
        REASON_LABELS = {
            "PDD1": "Defecto de fábrica",
            "PDD2": "No coincide con descripción",
            "PDD3": "Producto incorrecto enviado",
            "PDD4": "Partes/accesorios faltantes",
            "PDD5": "Dañado en tránsito",
            "PDD6": "No funciona",
            "PDD":  "Defectuoso o diferente",
        }

        async def _fetch_pdd(df, dt):
            claims = await client.fetch_all_claims(date_from=df, date_to=dt)
            return [c for c in claims if str(c.get("reason_id", "")).upper().startswith("PDD")]

        if df_b or dt_b:
            pdd_a, pdd_b = await asyncio.gather(_fetch_pdd(df_a, dt_a), _fetch_pdd(df_b, dt_b))
        else:
            pdd_a = await _fetch_pdd(df_a, dt_a)
            pdd_b = []

        # Cap order lookups to avoid rate limiting — semaphore(3) + max 30 orders
        oids_a = {str(c.get("resource_id", "")) for c in pdd_a
                  if c.get("resource") == "order" and c.get("resource_id")}
        oids_b = {str(c.get("resource_id", "")) for c in pdd_b
                  if c.get("resource") == "order" and c.get("resource_id")}
        max_orders = max(limit * 2, 30)
        all_oids = list(oids_a | oids_b)[:max_orders]

        sem = asyncio.Semaphore(3)

        async def _fetch_order_info(oid):
            async with sem:
                try:
                    order = await asyncio.wait_for(client.get(f"/orders/{oid}"), timeout=8.0)
                    oi = order.get("order_items", [])
                    if oi:
                        item = oi[0].get("item", {})
                        return oid, {
                            "title": item.get("title", "") or "Producto desconocido",
                            "item_id": str(item.get("id", "")),
                            "sku": item.get("seller_custom_field") or "",
                            "sale_amount": float(order.get("total_amount") or 0),
                        }
                except Exception:
                    pass
                return oid, {"title": "Producto desconocido", "item_id": "", "sku": "", "sale_amount": 0.0}

        results = await asyncio.gather(*[_fetch_order_info(oid) for oid in all_oids], return_exceptions=True)
        order_map = {r[0]: r[1] for r in results if isinstance(r, tuple)}

        # BM retail prices for cost calculation
        bm_prices_tp = await _get_bm_retail_prices()

        def _count_by_product(claims):
            counts = {}
            for c in claims:
                oid = str(c.get("resource_id", ""))
                info = order_map.get(oid, {"title": "Producto desconocido", "item_id": "", "sku": "", "sale_amount": 0.0})
                title = info["title"]
                reason_id = str(c.get("reason_id", "PDD")).upper()
                status = c.get("status", "")
                if title not in counts:
                    counts[title] = {
                        "title": title, "item_id": info["item_id"],
                        "sku": normalize_to_bm_sku(info.get("sku", "")),
                        "count": 0, "opened": 0, "closed": 0, "reasons": {},
                        "sale_amount_mxn": 0.0,
                    }
                counts[title]["count"] += 1
                counts[title]["sale_amount_mxn"] += info.get("sale_amount", 0.0)
                if status == "opened":
                    counts[title]["opened"] += 1
                else:
                    counts[title]["closed"] += 1
                label = REASON_LABELS.get(reason_id, REASON_LABELS.get("PDD", reason_id))
                counts[title]["reasons"][label] = counts[title]["reasons"].get(label, 0) + 1
            return counts

        def _recommendation(reasons: dict, opened: int, total: int) -> dict:
            if not reasons:
                return {"text": "Analizar manualmente", "color": "gray",
                        "actions": ["Revisar historial de mensajes de compradores"]}
            top_reason = max(reasons, key=reasons.get)
            top_pct = round(reasons[top_reason] / total * 100) if total > 0 else 0
            urgency = "high" if opened > 0 else "medium"
            r = top_reason.lower()
            if "descripción" in r or "coincide" in r:
                return {"text": "Actualizar fotos y descripción",
                        "detail": f"{top_pct}% no coincide con descripción",
                        "color": "orange", "urgency": urgency,
                        "actions": ["Verificar fotos vs producto real",
                                    "Actualizar especificaciones exactas",
                                    "Agregar tabla de medidas si aplica"]}
            elif "incorrecto" in r or "diferente" in r:
                return {"text": "Revisar proceso de picking",
                        "detail": f"{top_pct}% producto incorrecto enviado",
                        "color": "red", "urgency": urgency,
                        "actions": ["Verificar SKUs en almacén",
                                    "Auditar últimos envíos de este producto",
                                    "Separar variantes similares"]}
            elif "defecto" in r or "fábrica" in r or "funciona" in r:
                return {"text": "Revisar calidad con proveedor",
                        "detail": f"{top_pct}% defectos de producto",
                        "color": "red", "urgency": urgency,
                        "actions": ["Contactar proveedor con evidencia",
                                    "Revisar lote actual en almacén",
                                    "Implementar control de calidad pre-envío"]}
            elif "tránsito" in r or "dañado" in r:
                return {"text": "Mejorar empaque para envío",
                        "detail": f"{top_pct}% daños en tránsito",
                        "color": "yellow", "urgency": "medium",
                        "actions": ["Usar caja más resistente",
                                    "Agregar protección interna (burbuja/foam)",
                                    "Verificar peso y dimensiones declarados"]}
            elif "faltante" in r:
                return {"text": "Verificar contenido del paquete",
                        "detail": f"{top_pct}% partes faltantes",
                        "color": "orange", "urgency": urgency,
                        "actions": ["Crear checklist de contenido por producto",
                                    "Verificar accesorios listados en descripción",
                                    "Revisar proceso de empaque con proveedor"]}
            else:
                return {"text": "Analizar patrón de retornos",
                        "detail": f"Razón principal: {top_reason}",
                        "color": "gray", "urgency": "medium",
                        "actions": ["Revisar mensajes de compradores",
                                    "Comparar con descripción actual del producto"]}

        counts_a = _count_by_product(pdd_a)
        counts_b = _count_by_product(pdd_b)
        total_a = len(pdd_a)
        total_b = len(pdd_b)

        top_a = sorted(counts_a.values(), key=lambda x: x["count"], reverse=True)[:limit]

        products = []
        for p in top_a:
            count_a = p["count"]
            b_entry = counts_b.get(p["title"])
            count_b = b_entry["count"] if b_entry else (0 if pdd_b else None)
            delta_pct = None
            if count_b is not None and count_b > 0:
                delta_pct = round((count_a - count_b) / count_b * 100, 1)
            rec = _recommendation(p["reasons"], p["opened"], count_a)
            sku = p.get("sku", "")
            cost_usd = bm_prices_tp.get(sku, 0.0) * count_a if sku else 0.0
            products.append({
                "title": p["title"],
                "item_id": p["item_id"],
                "sku": sku,
                "count_a": count_a,
                "count_b": count_b,
                "delta_pct": delta_pct,
                "pct_of_total": round(count_a / total_a * 100, 1) if total_a > 0 else 0,
                "opened": p["opened"],
                "closed": p["closed"],
                "reasons": p["reasons"],
                "recommendation": rec,
                "sale_amount_mxn": round(p.get("sale_amount_mxn", 0.0), 2),
                "cost_usd_bm": round(cost_usd, 2),
            })

        def _label(df, dt):
            if df and dt: return f"{df} al {dt}"
            elif df: return f"Desde {df}"
            elif dt: return f"Hasta {dt}"
            return "Todo el historial"

        return {
            "period_a": {"label": _label(df_a, dt_a), "total": total_a, "products": products},
            "period_b": {"label": _label(df_b, dt_b), "total": total_b} if (df_b or dt_b) else None,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.close()


@app.get("/api/returns/timeline")
async def returns_timeline(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    granularity: str = Query("auto", description="day | week | auto"),
):
    """Timeline de retornos agrupados por día o semana para gráfica de tendencia."""
    client = await get_meli_client()
    if not client:
        return {"error": "No autenticado"}
    try:
        from datetime import datetime, timedelta, timezone
        df = date_from or None
        dt = date_to or None

        all_claims = await client.fetch_all_claims(date_from=df, date_to=dt)
        pdd_claims = [c for c in all_claims
                      if str(c.get("reason_id", "")).upper().startswith("PDD")]

        # Auto-granularity
        gran = granularity
        if gran == "auto":
            if date_from and date_to:
                try:
                    days_range = (datetime.fromisoformat(date_to) - datetime.fromisoformat(date_from)).days
                    gran = "week" if days_range > 90 else "day"
                except Exception:
                    gran = "day"
            else:
                gran = "day"

        REASON_LABELS = {
            "PDD1": "Defecto fábrica", "PDD2": "No coincide",
            "PDD3": "Prod. incorrecto", "PDD4": "Partes faltantes",
            "PDD5": "Dañado tránsito",  "PDD6": "No funciona",
        }

        buckets: dict = {}
        for c in pdd_claims:
            dc = c.get("date_created", "")
            if not dc:
                continue
            try:
                dt_obj = datetime.fromisoformat(dc.replace("Z", "+00:00"))
            except Exception:
                continue
            if gran == "week":
                week_start = dt_obj - timedelta(days=dt_obj.weekday())
                key = week_start.strftime("%Y-%m-%d")
            else:
                key = dc[:10]
            reason_id = str(c.get("reason_id", "PDD")).upper()
            reason_label = REASON_LABELS.get(reason_id, "Otro")
            if key not in buckets:
                buckets[key] = {"date": key, "count": 0, "reasons": {}}
            buckets[key]["count"] += 1
            buckets[key]["reasons"][reason_label] = buckets[key]["reasons"].get(reason_label, 0) + 1

        timeline = sorted(buckets.values(), key=lambda x: x["date"])
        return {"timeline": timeline, "granularity": gran, "total": len(pdd_claims)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.close()


@app.post("/api/returns/flag-item")
async def returns_flag_item(request: Request):
    """Marca un listing desde análisis de retornos. flag_type: 'review' | 'qty_zero'."""
    body = await request.json()
    item_id = str(body.get("item_id", "")).strip()
    flag_type = str(body.get("flag_type", "review"))
    note = str(body.get("note", ""))

    if not item_id:
        return {"ok": False, "error": "item_id requerido"}

    from app.services.meli_client import _active_user_id as _ctx
    _uid = str(_ctx.get() or "")
    from app.services import token_store as _ts
    await _ts.save_return_flag(_uid, item_id, flag_type, note)
    result: dict = {"ok": True, "flagged": item_id, "type": flag_type}

    if flag_type == "qty_zero":
        client = await get_meli_client()
        if client:
            try:
                await client.update_item_stock(item_id, 0)
                result["stock_updated"] = True
                result["message"] = f"Stock del listing {item_id} puesto a 0"
            except Exception as _e2:
                result["stock_error"] = str(_e2)
            finally:
                await client.close()

    return result


@app.get("/api/returns/flags")
async def returns_get_flags():
    """Retorna los listings marcados de la cuenta activa."""
    from app.services.meli_client import _active_user_id as _ctx
    _uid = str(_ctx.get() or "")
    from app.services import token_store as _ts
    flags = await _ts.get_return_flags(_uid)
    return {"flags": flags}


@app.post("/api/returns/resolve-flag")
async def returns_resolve_flag(request: Request):
    """Marca una flag de retorno como resuelta (solo para la cuenta activa)."""
    body = await request.json()
    item_id = str(body.get("item_id", "")).strip()
    if not item_id:
        return {"ok": False, "error": "item_id requerido"}
    from app.services.meli_client import _active_user_id as _ctx
    _uid = str(_ctx.get() or "")
    from app.services import token_store as _ts
    await _ts.resolve_return_flag(_uid, item_id)
    return {"ok": True, "resolved": item_id}


# ============================================================
# PLANNING — Planeación & Requerimientos de Producción
# ============================================================

@app.get("/api/planning/bm-ping")
async def planning_bm_ping():
    """Quick BinManager connectivity check — returns login status and env var presence."""
    import os
    from app.services.binmanager_client import BinManagerClient, _BM_USER, _BM_BASE
    bm = BinManagerClient()
    try:
        login_ok = await bm.login()
        return {
            "login": login_ok,
            "bm_base": _BM_BASE,
            "bm_user": _BM_USER,
            "has_bm_pass_env": bool(os.getenv("BM_PASS")),
        }
    except Exception as e:
        return {"login": False, "error": str(e)}
    finally:
        await bm.close()

@app.get("/planning", response_class=HTMLResponse)
async def planning_page(request: Request):
    user = await get_current_user()
    ctx = await _accounts_ctx(request)
    return templates.TemplateResponse(request, "planning.html", {**ctx, "user": user, "active": "planning"})


async def _planning_fetch_orders_for_user(uid: str, df_str: str, dt_str: str) -> list:
    """Fetch paginated paid orders for a MeLi user in a date range."""
    client = await get_meli_client(user_id=uid)
    if not client:
        return []
    try:
        all_orders, offset, limit = [], 0, 50
        while True:
            try:
                result = await client.get(
                    f"/orders/search?seller={uid}&sort=date_desc"
                    f"&order.status=paid"
                    f"&order.date_created.from={df_str}"
                    f"&order.date_created.to={dt_str}"
                    f"&limit={limit}&offset={offset}"
                )
                orders = result.get("results", [])
                if not orders:
                    break
                all_orders.extend(orders)
                total = result.get("paging", {}).get("total", 0)
                offset += len(orders)
                if offset >= total or offset >= 600:
                    break
            except Exception:
                break
        return all_orders
    finally:
        await client.close()


async def _planning_fetch_amazon_velocity(days: int) -> dict:
    """
    Retorna {SKU_UPPER: {units, units_7d, revenue, accounts}} desde todas las cuentas Amazon.
    Usa caché SQLite de 2 horas para no bloquear. Retorna {} si no hay cuentas o hay errores.
    """
    from app.services.amazon_client import get_amazon_client as _get_amz
    from app.services.token_store import get_all_amazon_accounts, get_amazon_vel_cache, save_amazon_vel_cache
    from datetime import datetime, timedelta, timezone

    try:
        # Check cache first — avoid hammering SP-API on every page load
        cached = await get_amazon_vel_cache(days)
        if cached is not None:
            return cached

        amazon_accounts = await get_all_amazon_accounts()
        if not amazon_accounts:
            return {}

        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to   = now.strftime("%Y-%m-%d")
        date_7d   = (now - timedelta(days=7)).isoformat()

        sku_agg: dict = {}
        sem = asyncio.Semaphore(2)  # SP-API rate limit más estricto

        async def _process_account(acc):
            client = await _get_amz(seller_id=acc.get("seller_id"))
            if not client:
                return
            try:
                orders = await client.fetch_orders_range(date_from, date_to)
                for order in orders:
                    if order.get("OrderStatus") in ("Cancelled", "Pending"):
                        continue
                    order_id = order.get("AmazonOrderId", "")
                    purchase_date = order.get("PurchaseDate", "")
                    is_7d = purchase_date >= date_7d
                    if not order_id:
                        continue
                    try:
                        async with sem:
                            items = await client.get_order_items(order_id)
                        for item in items:
                            sku = (item.get("SellerSKU") or "").upper().strip()
                            qty = int(item.get("QuantityOrdered") or 0)
                            price = float((item.get("ItemPrice") or {}).get("Amount") or 0)
                            if not sku or qty <= 0:
                                continue
                            if sku not in sku_agg:
                                sku_agg[sku] = {"units": 0, "units_7d": 0, "revenue": 0.0, "accounts": set()}
                            sku_agg[sku]["units"]   += qty
                            sku_agg[sku]["revenue"] += price
                            sku_agg[sku]["accounts"].add(acc.get("nickname") or acc.get("seller_id", ""))
                            if is_7d:
                                sku_agg[sku]["units_7d"] += qty
                    except Exception:
                        pass
            except Exception:
                pass

        await asyncio.gather(*[_process_account(a) for a in amazon_accounts], return_exceptions=True)

        # Serialize sets
        for v in sku_agg.values():
            v["accounts"] = sorted(v["accounts"])

        # Persist to cache so next call is instant
        try:
            await save_amazon_vel_cache(days, sku_agg)
        except Exception:
            pass
        return sku_agg
    except Exception:
        return {}


@app.get("/api/planning/velocity")
async def planning_velocity(days: int = Query(30, ge=7, le=90)):
    """Sales velocity per item/SKU from all MeLi accounts in last N days."""
    from app.services.meli_client import token_store as _ts
    from datetime import datetime, timedelta, timezone

    accounts = await _ts.get_all_tokens()
    if not accounts:
        return {"error": "No hay cuentas configuradas", "items": [], "accounts_count": 0}

    now = datetime.now(timezone.utc)
    date_from   = now - timedelta(days=days)
    date_from_7 = now - timedelta(days=7)
    df_str  = date_from.strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
    df7_str = date_from_7.strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
    dt_str  = now.strftime("%Y-%m-%dT%H:%M:%S.000-00:00")

    # Build a user_id → nickname map for display
    nick_map = {a["user_id"]: (a.get("nickname") or a["user_id"]) for a in accounts}

    order_lists = await asyncio.gather(
        *[_planning_fetch_orders_for_user(a["user_id"], df_str, dt_str) for a in accounts],
        return_exceptions=True,
    )

    item_agg: dict = {}
    for acct, orders in zip(accounts, order_lists):
        if not isinstance(orders, list):
            continue
        acct_uid = acct["user_id"]
        acct_nick = nick_map.get(acct_uid, acct_uid)
        for order in orders:
            if order.get("status") not in ("paid", "delivered", "completed"):
                continue
            is_7d = (order.get("date_created", "") >= df7_str)
            for oi in order.get("order_items", []):
                item = oi.get("item", {})
                iid  = str(item.get("id", ""))
                if not iid:
                    continue
                qty   = oi.get("quantity", 0) or 0
                price = oi.get("unit_price", 0) or 0
                if iid not in item_agg:
                    item_agg[iid] = {
                        "item_id": iid,
                        "title": item.get("title", "") or "Sin título",
                        "units": 0, "units_7d": 0, "revenue": 0.0, "sku": "",
                        "accounts": set(),
                    }
                item_agg[iid]["units"]   += qty
                item_agg[iid]["revenue"] += qty * price
                item_agg[iid]["accounts"].add(acct_nick)
                # Record seller uid — seller_custom_field only visible with owner's token
                if "seller_uid" not in item_agg[iid]:
                    item_agg[iid]["seller_uid"] = acct_uid
                if is_7d:
                    item_agg[iid]["units_7d"] += qty

    # ── Step 1a: Load cached SKUs from item_sku_cache (ALL items, not just top-100) ──
    from app.services import token_store as _ts_planning
    all_ids = list(item_agg.keys())
    top_ids = sorted(item_agg, key=lambda x: item_agg[x]["units"], reverse=True)[:100]

    cached_skus = await _ts_planning.get_cached_skus(all_ids)
    for iid, sku in cached_skus.items():
        if iid in item_agg:
            item_agg[iid]["sku"] = sku

    # ── Step 1b: Fallback — ml_listings for items still without SKU ───────────
    # ml_listings is synced every 3 min and covers ~14k items with SKU.
    # This resolves items outside the top-100 that were never live-fetched.
    still_no_sku = [iid for iid in all_ids if not item_agg[iid].get("sku")]
    if still_no_sku:
        listing_skus = await _ts_planning.get_skus_from_listings(still_no_sku)
        for iid, sku in listing_skus.items():
            if iid in item_agg:
                item_agg[iid]["sku"] = sku

    # ── Step 2: Live-fetch SKUs only for top-100 items still without SKU ──────
    # CRITICAL: seller_custom_field only visible with the listing owner's token.
    needs_sku = [iid for iid in top_ids if not item_agg[iid].get("sku")]

    items_by_uid: dict = {}
    for iid in needs_sku:
        uid = item_agg[iid].get("seller_uid", "")
        if uid not in items_by_uid:
            items_by_uid[uid] = []
        items_by_uid[uid].append(iid)

    sem_b = asyncio.Semaphore(3)
    new_sku_entries: list = []

    async def _fetch_sku_for_account(uid: str, iids: list):
        client = await get_meli_client(user_id=uid)
        if not client:
            return
        try:
            batches = [iids[i:i+20] for i in range(0, len(iids), 20)]
            for batch in batches:
                async with sem_b:
                    try:
                        # Include variations — SKU may be stored per-variation
                        entries = await client.get(
                            f"/items?ids={','.join(batch)}"
                            f"&attributes=id,seller_custom_field,attributes,variations"
                        )
                        if not isinstance(entries, list):
                            continue
                        for entry in entries:
                            if not isinstance(entry, dict):
                                continue
                            body = entry.get("body", entry)
                            iid  = str(body.get("id", ""))
                            sku  = _get_item_sku(body).upper().strip()
                            if iid in item_agg and sku:
                                item_agg[iid]["sku"] = sku
                                new_sku_entries.append({
                                    "item_id": iid,
                                    "user_id": uid,
                                    "sku": sku,
                                })
                    except Exception:
                        pass
        finally:
            await client.close()

    # Run Amazon velocity fetch in parallel with MeLi SKU fetch
    amz_task = asyncio.create_task(_planning_fetch_amazon_velocity(days=days))

    await asyncio.gather(
        *[_fetch_sku_for_account(uid, iids) for uid, iids in items_by_uid.items()],
        return_exceptions=True,
    )

    # ── Step 3: Persist new SKUs to cache ─────────────────────────────────────
    if new_sku_entries:
        try:
            await _ts_planning.save_skus_cache(new_sku_entries)
        except Exception:
            pass

    # ── Step 4: Merge Amazon velocity ─────────────────────────────────────────
    # Use shield so the background task keeps running even if we time out.
    # If cached (common case) this resolves instantly; cold compute has 12s budget.
    try:
        amz_vel = await asyncio.wait_for(asyncio.shield(amz_task), timeout=12.0)
    except asyncio.TimeoutError:
        amz_vel = {}  # Return ML data now; Amazon will be cached for next request

    # ── Step 5: Aggregate by SKU ──────────────────────────────────────────────
    # Same SKU can appear in multiple accounts/listings — consolidate for run-rate.
    sku_agg: dict = {}
    no_sku_list: list = []

    for d in item_agg.values():
        sku = (d["sku"] or "").upper().strip()
        if not sku:
            no_sku_list.append(d)
            continue
        if sku not in sku_agg:
            sku_agg[sku] = {
                "sku": sku, "title": d["title"],
                "units": 0, "units_7d": 0, "revenue": 0.0,
                "accounts": set(), "item_ids": [],
                "_best_units": 0,
            }
        ag = sku_agg[sku]
        ag["units"]    += d["units"]
        ag["units_7d"] += d["units_7d"]
        ag["revenue"]  += d["revenue"]
        ag["accounts"]  |= d["accounts"]
        ag["item_ids"].append(d["item_id"])
        # Keep title from the listing with highest individual sales
        if d["units"] > ag["_best_units"]:
            ag["_best_units"] = d["units"]
            ag["title"] = d["title"]

    result_items = []

    # Aggregated SKU rows
    for sku, ag in sku_agg.items():
        daily_rate = round(ag["units"] / days, 2)
        amz = amz_vel.get(sku, {})
        item = {
            "item_id": ag["item_ids"][0],
            "item_ids": ag["item_ids"],
            "sku": sku,
            "title": ag["title"],
            "units_30d": ag["units"],
            "units_7d": ag["units_7d"],
            "revenue_30d": round(ag["revenue"], 2),
            "daily_rate": daily_rate,
            "accounts": sorted(ag["accounts"]),
        }
        if amz:
            amz_daily = round(amz["units"] / days, 2)
            item["amz_units_30d"]   = amz["units"]
            item["amz_units_7d"]    = amz.get("units_7d", 0)
            item["amz_revenue_30d"] = round(amz.get("revenue", 0), 2)
            item["amz_daily_rate"]  = amz_daily
            item["amz_accounts"]    = amz.get("accounts", [])
            item["total_daily_rate"]= round(daily_rate + amz_daily, 2)
        else:
            item["total_daily_rate"] = daily_rate
        result_items.append(item)

    # Items without SKU — still useful for context, appended at the end
    for d in no_sku_list:
        daily_rate = round(d["units"] / days, 2)
        result_items.append({
            "item_id": d["item_id"], "item_ids": [d["item_id"]],
            "sku": "", "title": d["title"],
            "units_30d": d["units"], "units_7d": d["units_7d"],
            "revenue_30d": round(d["revenue"], 2),
            "daily_rate": daily_rate, "accounts": sorted(d["accounts"]),
            "total_daily_rate": daily_rate,
        })

    result_items.sort(key=lambda x: x["total_daily_rate"], reverse=True)
    return {
        "items": result_items[:100],
        "total_items": len(result_items),
        "days": days,
        "accounts_count": len(accounts),
        "has_amazon": bool(amz_vel),
    }


@app.get("/api/planning/no-sku-items")
async def planning_no_sku_items(days: int = Query(30, ge=7, le=90)):
    """Listings de ML que aparecen en ventas pero no tienen seller_custom_field (SKU) asignado."""
    vel = await planning_velocity(days=days)
    if vel.get("error"):
        return {"error": vel["error"], "items": []}
    no_sku = [x for x in vel.get("items", []) if not x.get("sku")]
    # Enriquecer con URL de ML
    for item in no_sku:
        iid = item.get("item_id", "")
        item["ml_url"] = f"https://articulo.mercadolibre.com.mx/{iid.replace('MLM', 'MLM-')}" if iid else ""
    no_sku.sort(key=lambda x: x.get("units_30d", 0), reverse=True)
    return {"items": no_sku, "total": len(no_sku), "days": days}


@app.post("/api/planning/sync-skus")
async def planning_sync_skus():
    """
    Pre-fetches ALL MeLi listings for all accounts and caches item_id → SKU in DB.
    Run once to populate the cache — subsequent velocity calls will be instant.
    """
    from app.services.meli_client import token_store as _ts2
    from app.services import token_store as _ts_cache

    accounts = await _ts2.get_all_tokens()
    if not accounts:
        return {"error": "No hay cuentas configuradas"}

    total_items = 0
    all_entries: list = []
    sem_s = asyncio.Semaphore(3)

    for acct in accounts:
        uid  = acct["user_id"]
        nick = acct.get("nickname", uid)
        client = await get_meli_client(user_id=uid)
        if not client:
            continue
        try:
            offset, limit = 0, 50
            while True:
                try:
                    resp = await client.get(
                        f"/users/{uid}/items/search",
                        params={"limit": limit, "offset": offset},
                    )
                    ids = resp.get("results", [])
                    if not ids:
                        break
                    total_items += len(ids)

                    # Batch-fetch item details using owner's token
                    for i in range(0, len(ids), 20):
                        batch = ids[i:i+20]
                        async with sem_s:
                            try:
                                entries = await client.get(
                                    f"/items?ids={','.join(batch)}"
                                    f"&attributes=id,seller_custom_field,attributes,variations"
                                )
                                if isinstance(entries, list):
                                    for entry in entries:
                                        if not isinstance(entry, dict):
                                            continue
                                        body = entry.get("body", entry)
                                        iid  = str(body.get("id", ""))
                                        sku  = _get_item_sku(body).upper().strip()
                                        if iid and sku:
                                            all_entries.append({"item_id": iid, "user_id": uid, "sku": sku})
                            except Exception:
                                pass

                    paging = resp.get("paging", {})
                    offset += len(ids)
                    if offset >= paging.get("total", 0):
                        break
                except Exception:
                    break
        finally:
            await client.close()

    if all_entries:
        await _ts_cache.save_skus_cache(all_entries)

    return {
        "synced_items": total_items,
        "with_sku": len(all_entries),
        "without_sku": total_items - len(all_entries),
        "accounts": len(accounts),
    }


@app.get("/api/planning/production-kpis")
async def planning_production_kpis(days: int = Query(7, ge=1, le=30)):
    """Production KPIs from BinManager Operations Dashboard."""
    from datetime import datetime, timedelta, timezone
    from app.services.binmanager_client import BinManagerClient

    bm = BinManagerClient()
    try:
        now       = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)  # datos del día en curso aún incompletos en BM
        start     = (yesterday - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        end       = yesterday.strftime("%Y-%m-%d")

        # Explicit login step for better error diagnosis
        login_ok = await bm.login()
        if not login_ok:
            return {"error": "Login fallido — verifica credenciales BM_USER/BM_PASS", "bm_unavailable": True}

        kpis = await bm.get_operations_kpis(start, end)
        if not kpis:
            return {"error": f"GetDashboardKPIs sin datos para {start}→{end}", "bm_unavailable": True}

        fft      = kpis.get("FFT", 0) or 0
        received = kpis.get("QtyReceived", 0) or 0
        sorting  = kpis.get("Sorting", 0) or 0
        recycled = kpis.get("Recycle", 0) or 0
        shipped  = kpis.get("TotalQtyShipped", 0) or 0

        sellable_rate  = round(fft / sorting * 100, 1) if sorting > 0 else 0
        daily_sellable = round(fft / days, 0)
        daily_received = round(received / days, 0)

        return {
            "received": received, "sorting": sorting, "fft": fft,
            "recycled": recycled, "shipped": shipped,
            "sellable_rate": sellable_rate,
            "daily_sellable": int(daily_sellable),
            "daily_received": int(daily_received),
            "days": days, "period": f"{start} al {end}",
        }
    except Exception as e:
        logger.error(f"planning_production_kpis error: {e}")
        return {"error": str(e), "bm_unavailable": True}
    finally:
        await bm.close()


@app.get("/api/planning/coverage")
async def planning_coverage(
    days: int = Query(30, ge=7, le=90),
    target_days: int = Query(14, ge=7, le=60),
):
    """Sales velocity (ML+Amazon) + BinManager stock = days of coverage per SKU."""

    vel = await planning_velocity(days=days)

    if vel.get("error") or not vel.get("items"):
        return {"error": vel.get("error", "Sin datos de velocidad"), "items": []}

    all_vel_items = vel["items"]
    items_without_sku_count = len([x for x in all_vel_items if not x.get("sku")])

    # velocity already returns one row per SKU — filter & take top 50
    items_with_sku = [x for x in all_vel_items if x.get("sku")][:50]

    if not items_with_sku:
        return {
            "items": [], "target_days": target_days,
            "items_without_sku": items_without_sku_count,
            "note": "Ningún item tiene SKU asignado — agrega seller_custom_field en tus publicaciones de ML",
        }

    # Usar el mismo código de inventario BM que funciona en toda la app
    bm_map = await _get_bm_stock_cached(items_with_sku)
    _apply_bm_stock(items_with_sku, bm_map)

    result = []
    for item in items_with_sku:
        stock = item.get("_bm_avail", 0)

        # Use combined ML+Amazon demand for coverage calculation
        daily = item.get("total_daily_rate", item["daily_rate"])

        coverage_days = round(stock / daily, 1) if daily > 0 else None
        if daily == 0:
            status = "no_movement"
        elif stock == 0:
            status = "out_of_stock"
        elif coverage_days is not None and coverage_days < 3:
            status = "critical"
        elif coverage_days is not None and coverage_days < 7:
            status = "alert"
        else:
            status = "ok"

        stock_target     = daily * target_days
        units_to_request = max(0, round(stock_target - stock)) if daily > 0 else 0

        result.append({
            **item,
            "stock_bm": stock,
            "coverage_days": coverage_days,
            "status": status,
            "units_to_request": units_to_request,
            "retail_price": item.get("_bm_retail_price", 0),
            "brand": item.get("_bm_brand", ""),
            "model": item.get("_bm_model", ""),
            "bm_category": item.get("_bm_category", ""),
        })

    order = {"out_of_stock": 0, "critical": 1, "alert": 2, "ok": 3, "no_movement": 4}
    result.sort(key=lambda x: (order.get(x["status"], 5), -(x.get("total_daily_rate") or x["daily_rate"])))
    return {
        "items": result,
        "target_days": target_days,
        "days": days,
        "items_without_sku": items_without_sku_count,
        "has_amazon": vel.get("has_amazon", False),
    }


@app.get("/api/planning/unlaunched")
async def planning_unlaunched():
    """BinManager products with stock that have zero or very low ML/Amazon sales."""
    from app.services.binmanager_client import BinManagerClient

    # Fetch ML velocity + Amazon velocity in parallel
    vel_task = asyncio.create_task(planning_velocity(days=30))
    amz_task = asyncio.create_task(_planning_fetch_amazon_velocity(days=30))

    vel = await vel_task
    # Amazon: use shield+timeout (cached = instant, cold = up to 10s)
    try:
        amz_vel = await asyncio.wait_for(asyncio.shield(amz_task), timeout=10.0)
    except asyncio.TimeoutError:
        amz_vel = {}

    vel_items = vel.get("items", [])

    # ML-selling SKUs (have ML orders)
    ml_selling_skus = {
        x["sku"].upper() for x in vel_items
        if x.get("sku") and x.get("daily_rate", 0) > 0.1
    }
    # Amazon-selling SKUs (selling on Amazon regardless of ML)
    amz_selling_skus = {
        sku for sku, data in amz_vel.items()
        if (data.get("units", 0) / 30) > 0.1
    }

    # Daily rate lookup for Amazon-only SKUs (for revenue potential)
    amz_rate_map = {sku: round(data.get("units", 0) / 30, 2) for sku, data in amz_vel.items()}

    bm = BinManagerClient()
    await bm.login()

    bm_items = []
    for page in range(1, 4):
        page_items = await bm.get_global_inventory(page=page, per_page=50, min_qty=1)
        bm_items.extend(page_items)
        if len(page_items) < 50:
            break

    await bm.close()

    result = []
    for row in bm_items:
        sku   = (row.get("SKU") or "").upper().strip()
        # BM global inventory uses TotalQty; per-SKU uses QTY; older responses QtyTotal
        stock = (row.get("TotalQty") or row.get("AvailableQTY")
                 or row.get("QTY") or row.get("QtyTotal") or 0)
        try:
            stock = int(stock)
        except (TypeError, ValueError):
            stock = 0
        if not sku or stock <= 0:
            continue

        ml_selling  = sku in ml_selling_skus
        amz_selling = sku in amz_selling_skus

        if ml_selling:
            tag = "sleeping"       # Has BM stock + ML sales → boost / promote
        elif amz_selling:
            tag = "amz_only"       # Sells on Amazon but not ML → ML opportunity
        else:
            tag = "unlaunched"     # Stock in BM but no sales anywhere

        retail_usd = row.get("RetailPrice") or row.get("LastRetailPricePurchaseHistory") or 0
        rev_potential = round(stock * float(retail_usd) * 17.5, 0) if retail_usd else 0

        entry = {
            "sku": sku,
            "title": row.get("Title", "") or row.get("Model", sku),
            "brand": row.get("Brand", "") or row.get("BRAND", ""),
            "model": row.get("Model", "") or row.get("MODEL", ""),
            "category": row.get("CategoryName", "") or row.get("Category", ""),
            "stock": stock,
            "retail_price_usd": float(retail_usd),
            "revenue_potential_mxn": rev_potential,
            "tag": tag,
        }
        if amz_selling:
            entry["amz_daily_rate"] = amz_rate_map.get(sku, 0)
            entry["amz_accounts"]   = amz_vel.get(sku, {}).get("accounts", [])
        result.append(entry)

    result.sort(key=lambda x: x["revenue_potential_mxn"], reverse=True)
    return {
        "items": result[:80],
        "total_unlaunched": sum(1 for x in result if x["tag"] == "unlaunched"),
        "total_sleeping":   sum(1 for x in result if x["tag"] == "sleeping"),
        "total_amz_only":   sum(1 for x in result if x["tag"] == "amz_only"),
        "has_amazon": bool(amz_vel),
    }


# ── Proceso 1: Vista unificada cross-account ─────────────────────────────────

@app.get("/api/stock/unified")
async def stock_unified_view():
    """Vista cross-account: por cada SKU base BM, muestra stock BM vs qty en cada cuenta ML.

    Retorna lista ordenada por BM avail desc:
    [{
      bm_sku:      "SNTV007270",
      bm_avail:    86,
      bm_reserved: 0,
      bm_total:    86,
      mty: 35, cdmx: 51, tj: 0,
      accounts: [
        { name: "Cuenta A", user_id: "...", listings: [
            { item_id: "MLM123", title: "...", meli_qty: 5, price: 1500 }
        ], total_meli_qty: 5 }
      ],
      total_meli_qty: 8,
      action:  "ok" | "oversell_risk" | "zero_listing" | "low_stock" | "no_listing"
    }]
    """
    accounts = await token_store.get_all_tokens()
    if not accounts:
        return JSONResponse({"error": "No hay cuentas configuradas"}, status_code=400)

    # Recolectar productos de todas las cuentas
    async def _get_acc_data(acc):
        uid = acc.get("user_id", "")
        name = acc.get("nickname") or acc.get("name") or uid
        try:
            cli = await get_meli_client(user_id=uid)
            if not cli:
                return uid, name, []
            products = await _get_all_products_cached(cli, include_paused=False)
            await cli.close()
            return uid, name, products
        except Exception:
            return uid, name, []

    acc_results = await asyncio.gather(*[_get_acc_data(a) for a in accounts], return_exceptions=True)

    # Mapa: bm_sku → { bm_data, accounts: {uid: {name, listings}} }
    bm_sku_map: dict = {}

    for res in acc_results:
        if isinstance(res, Exception):
            continue
        uid, name, products = res
        for p in products:
            ml_sku = p.get("sku", "")
            if not ml_sku:
                continue
            bm_sku = normalize_to_bm_sku(ml_sku)
            if not bm_sku:
                continue
            if bm_sku not in bm_sku_map:
                bm_sku_map[bm_sku] = {"accounts": {}}
            if uid not in bm_sku_map[bm_sku]["accounts"]:
                bm_sku_map[bm_sku]["accounts"][uid] = {"name": name, "listings": []}
            bm_sku_map[bm_sku]["accounts"][uid]["listings"].append({
                "item_id": p.get("id", ""),
                "title":   p.get("title", ""),
                "meli_qty": p.get("available_quantity", 0),
                "price":   p.get("price", 0),
                "sku_original": ml_sku,
            })

    # Enriquecer con datos BM del cache
    result = []
    for bm_sku, entry in bm_sku_map.items():
        bm_cached = _bm_stock_cache.get(bm_sku)
        bm_data = bm_cached[1] if bm_cached else {}
        bm_avail    = bm_data.get("avail_total", 0)
        bm_reserved = bm_data.get("reserved_total", 0)
        bm_total_wh = bm_data.get("total", 0)

        accounts_list = []
        total_meli_qty = 0
        for uid, acc_info in entry["accounts"].items():
            acc_total = sum(l["meli_qty"] for l in acc_info["listings"])
            total_meli_qty += acc_total
            accounts_list.append({
                "name": acc_info["name"],
                "user_id": uid,
                "listings": acc_info["listings"],
                "total_meli_qty": acc_total,
            })

        # Determinar acción sugerida
        if bm_avail == 0 and total_meli_qty > 0:
            action = "oversell_risk"
        elif total_meli_qty == 0 and bm_avail > 0:
            action = "zero_listing"
        elif 0 < bm_avail <= 10:
            action = "low_stock"
        else:
            action = "ok"

        result.append({
            "bm_sku":        bm_sku,
            "bm_avail":      bm_avail,
            "bm_reserved":   bm_reserved,
            "bm_total":      bm_total_wh,
            "mty":           bm_data.get("mty", 0),
            "cdmx":          bm_data.get("cdmx", 0),
            "tj":            bm_data.get("tj", 0),
            "accounts":      accounts_list,
            "total_meli_qty": total_meli_qty,
            "action":        action,
            "bm_in_cache":   bm_cached is not None,
        })

    result.sort(key=lambda x: (x["action"] != "oversell_risk", x["action"] != "zero_listing", -x["bm_avail"]))
    return JSONResponse({"skus": result, "total": len(result)})


# ── Proceso 2: SKUs de BM sin listing en ninguna cuenta ML ───────────────────

# Cache interno para evitar re-fetch de BM cada request: (timestamp, data_list)
_bm_unlaunched_cache: tuple[float, list] | None = None
_BM_UNLAUNCHED_TTL = 900  # 15 min

@app.get("/api/bm/launch-opportunities")
async def bm_launch_opportunities(
    min_qty: int = 1,
    page: int = 1,
    per_page: int = 20,
    category: str = "",
    search: str = "",
    refresh: bool = False,
):
    """Corrida inversa: SKUs en BM con stock NO lanzados en ninguna cuenta ML (activa o pausada).

    Trae todos los SKUs de BM paginando (SEARCH="", CONCEPTID=1, RECORDSPAGE=500 × N páginas),
    cruza contra todos los SKUs de ML (activos + pausados de todas las cuentas), y retorna
    los que no tienen listing. Resultado cacheado 15 min.

    Parámetros:
      min_qty   — AvailableQTY mínimo (default 1; 0 = todos incluyendo sin stock)
      page      — página (default 1)
      per_page  — items por página (default 20)
      category  — filtrar por categoría exacta (vacío = todas)
      search    — filtrar por SKU/Brand/Model (substring, case-insensitive)
      refresh   — forzar re-fetch de BM aunque el caché sea válido
    """
    global _bm_unlaunched_cache
    from app.services.binmanager_client import get_shared_bm
    from app.services.sku_utils import extract_item_sku

    # 1. Recolectar todos los SKUs de ML (activos + pausados) de todas las cuentas
    #    Usar _products_cache directamente — ya está cargado por prewarm, sin llamadas extra.
    #    Los products en cache son bodies crudos de ML → usar extract_item_sku (no .get("sku"))
    ml_bm_skus: set[str] = set()
    for _cache_key, (_ts, _prods) in list(_products_cache.items()):
        if not _cache_key.startswith("products:"):
            continue
        for _p in _prods:
            _s = extract_item_sku(_p) or ""
            _st = _p.get("status") or ""
            if _s and _st in ("active", "paused", "inactive"):
                ml_bm_skus.add(normalize_to_bm_sku(_s))

    # Si products_cache está vacío (primer arranque), hacer fetch fresco de todas las cuentas
    if not ml_bm_skus:
        accounts = await token_store.get_all_tokens()
        async def _get_ml_skus(acc):
            uid = acc.get("user_id", "")
            try:
                cli = await get_meli_client(user_id=uid)
                if not cli:
                    return set()
                prods = await _get_all_products_cached(cli, include_paused=True)
                await cli.close()
                return {normalize_to_bm_sku(extract_item_sku(p)) for p in prods if extract_item_sku(p)}
            except Exception:
                return set()
        _sets = await asyncio.gather(*[_get_ml_skus(a) for a in accounts], return_exceptions=True)
        for _s in _sets:
            if isinstance(_s, set):
                ml_bm_skus.update(_s)

    # 2. BM inventory con filtros correctos (LOCATIONID=47,62,68 + CONDITION vendible + CONCEPTID=1)
    #    get_bulk_stock() usa los mismos filtros que el resto de la app — stock vendible real.
    now = _time.time()
    if refresh or not _bm_unlaunched_cache or (now - _bm_unlaunched_cache[0]) > _BM_UNLAUNCHED_TTL:
        bm_client = await get_shared_bm()
        bm_raw = await bm_client.get_bulk_stock()
        # Deduplicar por BM base SKU + armar lista de oportunidades
        seen_bm: set[str] = set()
        all_items: list[dict] = []
        for item in bm_raw:
            raw_sku = (item.get("SKU") or "").strip()
            if not raw_sku:
                continue
            bm_sku = normalize_to_bm_sku(raw_sku)
            if bm_sku in seen_bm:
                continue
            seen_bm.add(bm_sku)
            if bm_sku in ml_bm_skus:
                continue  # Ya lanzado en ML
            retail = float(item.get("LastRetailPricePurchaseHistory") or 0)
            all_items.append({
                "bm_sku":   bm_sku,
                "sku_raw":  raw_sku,
                "title":    item.get("Title") or "",
                "brand":    item.get("Brand") or "",
                "model":    item.get("Model") or "",
                "category": item.get("CategoryName") or "",
                "avail":    int(item.get("AvailableQTY") or 0),
                "reserve":  int(item.get("Reserve") or 0),
                "total":    int(item.get("TotalQty") or 0),
                "cost_usd": retail,   # costo = retail BM (precio de adquisición)
                "retail":   retail,
            })
        all_items.sort(key=lambda x: (-x["avail"], x["category"], x["bm_sku"]))
        _bm_unlaunched_cache = (now, all_items)
    else:
        all_items = _bm_unlaunched_cache[1]

    # 3. Filtros en memoria
    filtered = all_items
    if min_qty > 0:
        filtered = [x for x in filtered if x["avail"] >= min_qty]
    if category:
        filtered = [x for x in filtered if x["category"] == category]
    if search:
        q = search.lower()
        filtered = [x for x in filtered if q in x["bm_sku"].lower()
                    or q in x["brand"].lower() or q in x["model"].lower()
                    or q in x["title"].lower()]

    # 4. Categorías disponibles (sobre datos filtrados por min_qty/search pero no category)
    base_for_cats = all_items
    if min_qty > 0:
        base_for_cats = [x for x in all_items if x["avail"] >= min_qty]
    if search:
        q = search.lower()
        base_for_cats = [x for x in base_for_cats if q in x["bm_sku"].lower()
                         or q in x["brand"].lower() or q in x["model"].lower()
                         or q in x["title"].lower()]
    categories = sorted({x["category"] for x in base_for_cats if x["category"]})

    # 5. Paginar
    total = len(filtered)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    page_items = filtered[start: start + per_page]

    cache_age = int(now - _bm_unlaunched_cache[0]) if _bm_unlaunched_cache else 0
    return JSONResponse({
        "items":        page_items,
        "total":        total,
        "page":         page,
        "per_page":     per_page,
        "pages":        pages,
        "categories":   categories,
        "ml_skus":      len(ml_bm_skus),
        "bm_scanned":   len(all_items) + len(ml_bm_skus),  # total BM antes de cruzar
        "cache_age_s":  cache_age,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
