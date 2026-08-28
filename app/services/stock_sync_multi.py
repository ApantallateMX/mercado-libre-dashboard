"""
stock_sync_multi.py — Sincronización de stock multi-plataforma BM → ML + Amazon

CICLO (cada 5 minutos):
  1. Recopilar listings activos Y pausados: MeLi (todas las cuentas) + Amazon FBM/FLX
  2. Consultar BinManager avail_total por SKU base
  3. Por cada SKU aplicar regla:
       avail >= 10  → todas las plataformas habilitadas muestran avail_total
                      (los pausados se activan automáticamente)
       0 < avail < 10 → solo la cuenta ganadora muestra avail_total; resto = 0
                        (ganador pausado → se activa; perdedores pausados → se ignoran)
       avail == 0   → activos con qty>0 quedan en 0; pausados se ignoran (ya apagados)
  4. Ejecutar solo los updates necesarios (evitar API calls cuando qty ya es correcta)
  5. Registrar log en DB

SCORE (ganador cuando avail < 10):
  score = precio × (1 − comisión%) × velocidad_30d
  Maximiza el Ingreso Neto Proyectado → la cuenta que más ganancia genera.

NOTAS:
  - FULL (logistic_type=fulfillment): ML controla ese stock. No se toca.
  - FBA puro: Amazon controla ese stock físicamente. No se toca.
  - NUNCA se pausa un listing. Solo se pone qty=0.
  - Si no hay reglas para un SKU → todas las plataformas están habilitadas.
"""

import asyncio
import json
import logging
import time as _time
from datetime import datetime

import httpx

from app.services.sku_utils import base_sku as _base_sku, extract_item_sku
from app.services import token_store
# NOTA: stock_winner se importa de forma diferida (dentro de las funciones que
# lo usan) porque stock_winner.py importa constantes de ESTE módulo
# (_REPUTATION_FACTOR, _ml_fee, _AMZ_FEE, _bm_base_for_key) -- un import a
# nivel de módulo aquí crearía un ciclo real (este archivo no habría
# terminado de definir esas constantes todavía cuando stock_winner intenta
# leerlas). En tiempo de llamada ambos módulos ya están completamente
# cargados.

logger = logging.getLogger(__name__)

# ─── BinManager ───────────────────────────────────────────────────────────────
_BM_AVAIL_URL = (
    "https://binmanager.mitechnologiesinc.com"
    "/InventoryReport/InventoryReport/InventoryBySKUAndCondicion_Quantity"
)
_COND_SUFFIXES   = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")
# FIX 2026-08-20: umbral del circuit breaker de bm_sku_master (ver
# run_multi_stock_sync/_run_single_account_sync) -- el loop top de
# categorías refresca cada 15 min; 3 ciclos perdidos (45 min) es señal
# real de que BM está fallando repetido, no solo jitter normal.
_BM_MASTER_STALE_THRESHOLD_S = 45 * 60


def _listing_key(sku: str) -> str:
    """Clave de agrupación por SKU. Preserva sufijos de condición conocidos
    (-GRA, -GRB, -GRC, -ICB, -ICC, -NEW) para no mezclar stock entre condiciones.
    Sufijos no-condición (-FLX01, etc.) se normalizan usando _base_sku."""
    if not sku:
        return ""
    upper = sku.upper().strip()
    for sfx in _COND_SUFFIXES:
        if upper.endswith(sfx):
            return upper  # e.g. "SHIL000154-GRA"
    return _base_sku(sku).upper()


def _cond_for_key(key: str) -> str:
    """Condiciones BM para un listing key.
    - SNTV* con -ICB, -ICC o bundle ("/") → GRA,GRB,GRC,ICB,ICC,NEW
    - Todo lo demás → GRA,GRB,GRC,NEW
    """
    upper = key.upper()
    if upper.startswith("SNTV") and (
        upper.endswith("-ICB") or upper.endswith("-ICC") or "/" in upper
    ):
        return "GRA,GRB,GRC,ICB,ICC,NEW"
    return "GRA,GRB,GRC,NEW"


def _bm_base_for_key(key: str) -> str:
    """SKU base para BM query (sin sufijo de condición)."""
    upper = key.upper()
    for sfx in _COND_SUFFIXES:
        if upper.endswith(sfx):
            return key[: -len(sfx)]
    return key

# ─── Comisiones por plataforma ────────────────────────────────────────────────
# ML aplica tarifa diferenciada por precio (aprox):
#   < 500 MXN      → 18%   (artículos baratos, mayor costo relativo)
#   500–1 500 MXN  → 16%
#   1 500–5 000 MXN→ 14%
#   > 5 000 MXN    → 12%   (TVs, laptops, etc.)
_AMZ_FEE = 0.15   # 15% promedio Amazon (sin escalonado)


def _ml_fee(price: float) -> float:
    """Tarifa ML estimada según precio. Más precisa que un flat 17%."""
    if price >= 5000:
        return 0.12
    if price >= 1500:
        return 0.14
    if price >= 500:
        return 0.16
    return 0.18


def _threshold_for(listings: list) -> int:
    """
    Umbral dinámico de concentración de stock, basado en el precio medio del SKU.

    Lógica: productos caros se venden más despacio → umbral más bajo.
    Productos baratos rotan rápido → umbral más alto para tener buffer.

      Precio medio < 500 MXN   → 20 unidades
      500–2 000 MXN             → 10 unidades  (default actual)
      2 000–10 000 MXN          →  5 unidades
      > 10 000 MXN              →  3 unidades
    """
    if not listings:
        return 10
    prices = [float(lst.get("price") or 0) for lst in listings if (lst.get("price") or 0) > 0]
    if not prices:
        return 10
    avg = sum(prices) / len(prices)
    if avg >= 10_000:
        return 3
    if avg >= 2_000:
        return 5
    if avg >= 500:
        return 10
    return 20


# ─── Regla de distribución (fallback para referencia/logs) ────────────────────
STOCK_THRESHOLD = 10   # valor por defecto — se reemplaza por _threshold_for() en _plan

# ─── Ciclo ────────────────────────────────────────────────────────────────────
_SYNC_INTERVAL = 5 * 60   # 5 minutos

# ─── Estado global ────────────────────────────────────────────────────────────
_sync_running      = False
_last_sync_ts      = 0.0
_last_sync_result: dict = {}
_sync_progress: dict = {}   # progreso en tiempo real mientras corre
_cannibalization_data: list = []  # último resultado de canibalización (de último sync)
_nocturnal_protection: bool = True  # reduce-only 10pm–6am CST por defecto
_last_bm_stock: dict = {}  # {sku_upper: avail_int} del último sync — para prewarm cache
_last_sync_per_account: list = []  # [{platform, account_id, nickname, updates, errors, last_error, ts}]
_on_sync_complete = None           # callback registrado por main.py para invalidar caches post-sync


def register_sync_complete_callback(fn):
    """main.py registra aquí una función async para invalidar _products_cache y _stock_issues_cache."""
    global _on_sync_complete
    _on_sync_complete = fn


# ─────────────────────────────────────────────────────────────────────────────
# BINMANAGER — consulta avail_total por SKU base
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_bm_avail(sku_cond_map: dict[str, str]) -> dict[str, int | None]:
    """
    Consulta BM para todos los SKUs usando 1 llamada bulk (get_bulk_stock).
    Retorna {listing_key_upper: avail_total} donde avail_total = unidades "Producto Vendible".

    IMPORTANTE: Si BM retorna error (timeout, 5xx, excepción), el SKU NO aparece en el dict.
    El caller debe distinguir "BM retornó 0" de "BM tuvo error y no sabemos" — solo el primer
    caso debe poner items en 0.  El segundo caso debe ser skip (no tocar ML).
    """
    result: dict[str, int] = {}
    if not sku_cond_map:
        return result

    # FIX 2026-08-20 (directiva de Jovan: "todo debe apuntar a nuestro
    # maestro, nada a BM por el momento"): lectura local de bm_sku_master
    # (mantenido fresco por el loop de categorías) en vez de llamada en vivo.
    # min_qty=0 -- necesitamos el universo completo (incluye ceros reales
    # confirmados), no solo SKUs con stock.
    from app.services import token_store
    bulk_rows = await token_store.get_bm_master_all_as_bulk_rows(min_qty=0)
    if not bulk_rows:
        # bm_sku_master vacío -- nunca corrió el loop de categorías todavía
        # (o la tabla se vació). NUNCA hacer fallback per-SKU: llenaría
        # bm_stock con ceros falsos → sync pondría TODOS los listings en
        # qty=0 en ML y Amazon. Retornar dict vacío: el caller ve "base not
        # in bm_stock" → skip seguro.
        logger.error(
            "[MULTI-SYNC-BM] bm_sku_master vacío -- el loop de categorías "
            "no ha corrido todavía. Retornando vacío para proteger stock ML/Amazon."
        )
        return result  # {} vacío → loop principal salta todos los SKUs

    # Construir lookup desde bulk
    exact_map: dict = {}
    by_base: dict = {}
    _SFXS = ("-GRA", "-GRB", "-GRC", "-ICB", "-ICC", "-NEW")
    for row in bulk_rows:
        sk = (row.get("SKU") or "").upper().strip()
        if not sk:
            continue
        exact_map[sk] = row
        base_sk = sk
        for sfx in _SFXS:
            if sk.endswith(sfx):
                base_sk = sk[:-len(sfx)]
                break
        by_base.setdefault(base_sk, []).append(row)

    for key in sku_cond_map:
        base = _bm_base_for_key(key).upper()
        row = exact_map.get(base)
        if row is not None:
            result[key.upper()] = int(row.get("AvailableQTY") or 0)
        else:
            variants = by_base.get(base, [])
            if variants:
                result[key.upper()] = sum(int(v.get("AvailableQTY") or 0) for v in variants)
            else:
                result[key.upper()] = 0  # confirmado: no está en BM
        if _sync_progress:
            _sync_progress["skus_done"] = _sync_progress.get("skus_done", 0) + 1

    logger.info(f"[MULTI-SYNC-BM] Bulk fetch: {len(bulk_rows)} SKUs BM → {len(result)} mapeados")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ML — recopilar listings activos de todas las cuentas
# ─────────────────────────────────────────────────────────────────────────────

# Factor de reputación ML por level_id — pondera _score() para que el reparto de
# stock escaso no siga favoreciendo a una cuenta con reputación deteriorada
# (ej. BLOWTECHNOLOGIES, amarilla desde 2026-08 con ventas -82%). None/desconocido
# (cuenta nueva sin historial de reputación) no se penaliza.
_REPUTATION_FACTOR = {
    "5_green":       1.00,
    "4_light_green": 0.85,
    "3_yellow":      0.50,
    "2_orange":      0.25,
    "1_red":         0.15,
}


async def _collect_ml_listings(ml_accounts: list) -> dict[str, list]:
    """
    Retorna {base_sku: [listing_dict, ...]} para todos los items activos Y pausados ML.
    listing_dict: {platform, account_id, item_id, price, qty, sold_qty, date_created, sku, status, can_update, rep_factor}

    can_update=False si el item es FULL (logistic_type=fulfillment) — ML gestiona ese stock.
    status='paused' permite que _execute active el listing cuando BM tiene stock.
    rep_factor: multiplicador de _score() según reputación real de la cuenta (ver _REPUTATION_FACTOR).
    """
    from app.services.meli_client import get_meli_client

    by_sku: dict[str, list] = {}

    for acc in ml_accounts:
        uid = acc.get("user_id", "")
        if not uid:
            continue
        try:
            client = await get_meli_client(user_id=uid)
            if not client:
                continue

            rep_factor = 1.0
            try:
                _user_info = await client.get_user_info()
                _seller_rep = _user_info.get("seller_reputation", {}) or {}
                _level_id = _seller_rep.get("level_id")
                rep_factor = _REPUTATION_FACTOR.get(_level_id, 1.0)
                # Snapshot diario para detectar tendencia (get_reputation_trend) --
                # aprovecha esta misma llamada, sin gastar otra a la API de ML.
                try:
                    await token_store.save_reputation_snapshot(uid, _seller_rep)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[MULTI-SYNC] Cuenta {uid}: no se pudo leer reputación ({e}), rep_factor=1.0")

            # Recopilar activos + pausados + inactivos ("Inactiva sin stock")
            item_ids = await client.get_all_item_ids_by_statuses(["active", "paused", "inactive"])
            if not item_ids:
                continue

            for i in range(0, len(item_ids), 20):
                batch = item_ids[i : i + 20]
                try:
                    entries = await client.get_items_details(batch)
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        item = entry.get("body") if isinstance(entry, dict) and "body" in entry else entry
                        if not isinstance(item, dict):
                            continue
                        item_status = item.get("status", "")
                        if item_status not in ("active", "paused", "inactive"):
                            continue

                        sku = extract_item_sku(item)
                        if not sku:
                            continue
                        base = _listing_key(sku)
                        if not base:
                            continue

                        # FULL (fulfillment): ML gestiona stock — no tocar
                        shipping = item.get("shipping") or {}
                        is_full  = shipping.get("logistic_type") == "fulfillment"

                        by_sku.setdefault(base, []).append({
                            "platform":     "ml",
                            "account_id":   uid,
                            "item_id":      str(item.get("id", "")),
                            "title":        (item.get("title") or "")[:80],
                            "price":        float(item.get("price") or 0),
                            "qty":          int(item.get("available_quantity") or 0),
                            "sold_qty":     int(item.get("sold_quantity") or 0),
                            "date_created": item.get("date_created", ""),
                            "sku":          sku,
                            "status":       item_status,
                            "can_update":   not is_full,
                            "rep_factor":   rep_factor,
                        })
                except Exception as e:
                    logger.warning(f"[MULTI-SYNC-ML] Batch error uid={uid} i={i}: {e}")
                    await asyncio.sleep(0.5)

        except Exception as e:
            logger.warning(f"[MULTI-SYNC-ML] Cuenta {uid}: {e}")

    return by_sku


# ─────────────────────────────────────────────────────────────────────────────
# AMAZON — recopilar listings FBM/FLX de todas las cuentas
# ─────────────────────────────────────────────────────────────────────────────

async def _collect_amz_listings(amz_accounts: list) -> dict[str, list]:
    """
    Retorna {base_sku: [listing_dict, ...]} para todos los listings FBM/FLX Amazon.
    Omite FBA puro (AMAZON_NA sin sufijo -FLX) — Amazon controla ese stock.
    listing_dict: {platform, account_id, sku, price, qty, sold_qty_30d, is_flx}
    """
    from app.services.amazon_client import get_amazon_client

    # Intentar usar el caché de ventas 30d de amazon_products si está disponible
    _amz_sales_cache: dict = {}
    try:
        from app.api.amazon_products import _sku_sales_cache as _asc
        _amz_sales_cache = _asc
    except Exception:
        pass

    by_sku: dict[str, list] = {}

    for acc in amz_accounts:
        sid = acc.get("seller_id", "")
        if not sid:
            continue
        try:
            client = await get_amazon_client(seller_id=sid)
            if not client:
                continue

            listings = await client.get_all_listings()

            # Ventas 30d del caché (TTL 1h)
            sales_30d: dict = {}
            cached = _amz_sales_cache.get(sid)
            if cached and (_time.time() - cached[0]) < 3600:
                sales_30d = cached[1]

            for item in listings:
                sku = item.get("sku", "")
                if not sku:
                    continue

                # Detectar FBA puro → omitir
                fa       = item.get("fulfillmentAvailability", [])
                channel  = (fa[0].get("fulfillmentChannelCode") or "").upper() if fa else ""
                is_flx   = "-FLX" in sku.upper()
                is_fba_pure = (channel == "AMAZON_NA") and not is_flx
                if is_fba_pure:
                    continue

                base = _listing_key(sku)
                if not base:
                    continue

                price = 0.0
                for offer in (item.get("offers") or []):
                    if offer.get("offerType") == "B2C":
                        try:
                            price = float(offer.get("price", {}).get("amount") or 0)
                        except (TypeError, ValueError):
                            pass
                        break

                qty        = int((fa[0].get("quantity") or 0) if fa else 0)
                sold_30d   = int((sales_30d.get(sku) or {}).get("units", 0))

                # FLX: Amazon gestiona asignación de stock desde tu bodega.
                # Actualizar qty via DEFAULT convertiría el listing de FLX a FBM.
                # Por seguridad solo actualizamos FBM (DEFAULT). FLX se monitorea pero no se toca.
                can_update = not is_flx

                by_sku.setdefault(base, []).append({
                    "platform":     "amazon",
                    "account_id":   sid,
                    "sku":          sku,
                    "price":        price,
                    "qty":          qty,
                    "sold_qty_30d": sold_30d,
                    "is_flx":       is_flx,
                    "can_update":   can_update,
                })

        except Exception as e:
            logger.warning(f"[MULTI-SYNC-AMZ] Cuenta {sid}: {e}")

    return by_sku


# ─────────────────────────────────────────────────────────────────────────────
# SCORE — Ingreso Neto Proyectado 30d
# ─────────────────────────────────────────────────────────────────────────────

def _score(listing: dict) -> float:
    """
    score = precio_neto × velocidad_30d × rep_factor
    Representa el ingreso neto estimado que generaría esta plataforma en 30 días,
    ponderado por la reputación real de la cuenta (rep_factor, solo ML — ver
    _REPUTATION_FACTOR) para no seguir concentrando stock escaso en una cuenta
    en crisis de reputación solo porque vendía bien en el pasado.
    Mínimo retorna el precio_neto para que ninguna plataforma quede con score=0
    si tiene precio pero no tiene historial de ventas.
    """
    price    = float(listing.get("price") or 0)
    platform = listing.get("platform", "ml")

    if platform == "ml":
        net_price = max(0.0, price * (1 - _ml_fee(price)) - 150.0)  # 150 = envío estimado MXN
        sold_qty  = int(listing.get("sold_qty") or 0)
        date_str  = listing.get("date_created", "")
        days_active = 30
        if date_str:
            try:
                # ML devuelve "2024-03-15T10:30:00.000Z"
                dt = datetime.fromisoformat(
                    date_str.replace("Z", "").replace("T", " ").split(".")[0]
                )
                days_active = max(1, (datetime.utcnow() - dt).days)
            except Exception:
                pass
        velocity = sold_qty / max(1, days_active / 30)
    else:
        net_price = price * (1 - _AMZ_FEE)
        velocity  = float(listing.get("sold_qty_30d") or 0)

    # Mínimo = net_price (como si vendiéramos 1 unidad/mes)
    rep_factor = float(listing.get("rep_factor", 1.0)) if platform == "ml" else 1.0
    return net_price * max(1.0, velocity) * rep_factor


def _listing_to_candidate(lst: dict) -> dict:
    """Normaliza un listing de _collect_ml_listings/_collect_amz_listings al
    candidato común de stock_winner.build_candidate().

    OJO ventas 30d en ML: este loop automático NO tiene un "sold_30d" real
    (a diferencia de stock_concentrator, que sí consulta órdenes reales de
    los últimos 30 días por SKU). Fetch de órdenes reales por listing en un
    loop que corre cada 5 min multiplicaría llamadas a la API de ML sobre
    ~40 semáforos independientes ya existentes -- mismo patrón que causó el
    incidente de 429 en cascada documentado en DEVLOG 2026-08-27. Se usa la
    misma aproximación que ya usaba _score(): sold_quantity histórico del
    item / (días activo / 30), igual que antes de esta consolidación.
    Amazon sí trae sold_qty_30d real (viene de _sku_sales_cache, ya cacheado)."""
    if lst["platform"] == "ml":
        sold_qty = int(lst.get("sold_qty") or 0)
        date_str = lst.get("date_created", "")
        days_active = 30
        if date_str:
            try:
                dt = datetime.fromisoformat(
                    date_str.replace("Z", "").replace("T", " ").split(".")[0]
                )
                days_active = max(1, (datetime.utcnow() - dt).days)
            except Exception:
                pass
        approx_30d = int(round(sold_qty / max(1.0, days_active / 30)))
        return _winner_mod().build_candidate(
            platform="ml", account_id=lst["account_id"], nickname=lst["account_id"],
            ref=lst.get("item_id", ""), price=lst.get("price", 0),
            sold_30d=approx_30d, sold_total=sold_qty,
            rep_factor=lst.get("rep_factor", 1.0),
            can_update=lst.get("can_update", True), status=lst.get("status", "active"),
            extra=lst,
        )
    else:
        return _winner_mod().build_candidate(
            platform="amazon", account_id=lst["account_id"], nickname=lst["account_id"],
            ref=lst.get("sku", ""), price=lst.get("price", 0),
            sold_30d=lst.get("sold_qty_30d", 0), sold_total=lst.get("sold_qty_30d", 0),
            rep_factor=1.0, can_update=lst.get("can_update", True),
            status="active", extra=lst,
        )


def _winner_mod():
    """Import diferido de stock_winner (ver nota al inicio del archivo)."""
    from app.services import stock_winner
    return stock_winner


# ─────────────────────────────────────────────────────────────────────────────
# PLANIFICACIÓN DE DISTRIBUCIÓN
# ─────────────────────────────────────────────────────────────────────────────

async def _plan(base_sku: str, bm_avail: int, listings: list, enabled_ids: set, reduce_only: bool = False,
                 cost_mxn: float | None = None, ship_cost_maps: dict | None = None,
                 persist_winner: bool = True) -> list[dict]:
    """
    Calcula las actualizaciones necesarias para un SKU base.

    enabled_ids: set de "ml_{user_id}" o "amz_{seller_id}" habilitados para este SKU.
                 Si está vacío → todas las plataformas habilitadas.

    cost_mxn/ship_cost_maps: pasados por el caller (batch, 1 query por ciclo
    completo -- ver stock_winner.get_cost_map/get_shipping_cost_maps) para
    que la función única de ganador (Fix 1 auditoría 2026-08-28) pueda usar
    margen de contribución real en vez de solo velocidad×reputación.
    persist_winner: False en previews/dry-run -- no debe pisar la histéresis
    persistida solo por mostrar "qué pasaría".

    Retorna lista de {listing, new_qty, reason}.
    Solo incluye entradas donde new_qty != qty actual (evita API calls innecesarios).
    """
    # Filtrar por plataformas habilitadas
    if enabled_ids:
        active = []
        for lst in listings:
            pid = (
                f"ml_{lst['account_id']}"
                if lst["platform"] == "ml"
                else f"amz_{lst['account_id']}"
            )
            if pid in enabled_ids:
                active.append(lst)
        if not active:
            return []
        listings = active

    if not listings:
        return []

    # Separar los que se pueden actualizar de los que solo son informativos (FLX)
    updatable = [lst for lst in listings if lst.get("can_update", True)]
    if not updatable:
        return []

    updates = []

    if bm_avail == 0:
        # Poner todo en 0 — solo los que están activos con qty > 0
        # Los pausados ya están "apagados", no hace falta tocarlos
        for lst in updatable:
            if lst.get("status") == "paused":
                continue
            if lst["qty"] != 0:
                updates.append({"listing": lst, "new_qty": 0, "reason": "bm_zero"})

    elif bm_avail < _threshold_for(updatable):
        # Concentrar en la cuenta ganadora -- función única de ganador (Fix 1
        # auditoría 2026-08-28): margen de contribución real × unidades
        # esperadas × reputación, con histéresis contra stock_winner_cache.
        # Ver app/services/stock_winner.py para la fórmula completa y los
        # fallbacks (sin costo confiable / sin ventas en ninguna cuenta).
        candidates = [_listing_to_candidate(lst) for lst in updatable]
        _wresult = await _winner_mod().resolve_winner(
            base_sku, candidates, bm_avail, cost_mxn, ship_cost_maps,
            persist=persist_winner,
        )
        _wcand = _wresult.get("winner")
        winner = None
        if _wcand is not None:
            winner = next(
                (lst for lst in updatable
                 if lst["platform"] == _wcand["platform"] and lst["account_id"] == _wcand["account_id"]),
                None,
            )
        if winner is None:
            # No debería pasar si updatable no está vacío -- fallback defensivo
            # para no dejar el SKU sin resolver (protege contra un bug futuro
            # en el mapeo de candidatos).
            logger.warning(f"[MULTI-SYNC] {base_sku}: stock_winner no devolvió candidato mapeable, fallback a _score()")
            winner = sorted(updatable, key=_score, reverse=True)[0]
        for lst in updatable:
            new_qty = bm_avail if lst is winner else 0
            # Pausado con new_qty=0 → ya está apagado, skip
            if lst.get("status") == "paused" and new_qty == 0:
                continue
            if lst["qty"] != new_qty:
                reason = "concentrate_winner" if lst is winner else "concentrate_loser"
                updates.append({"listing": lst, "new_qty": new_qty, "reason": reason})

    else:
        # Distribuir: dividir BM stock equitativamente entre listings para evitar sobreventa.
        # El listing de mayor score recibe el sobrante de la división entera.
        # Ej: BM=244, 3 listings → [82, 81, 81] en vez de [244, 244, 244]
        _n = len(updatable)
        _base_share = bm_avail // _n
        _remainder = bm_avail % _n
        _scored_dist = sorted(updatable, key=_score, reverse=True)
        for _i, lst in enumerate(_scored_dist):
            _share = _base_share + (1 if _i < _remainder else 0)
            if lst["qty"] != _share:
                reason = "activate_and_split" if lst.get("status") == "paused" else "split"
                updates.append({"listing": lst, "new_qty": _share, "reason": reason})

    # Protección nocturna: solo permitir reducciones
    if reduce_only:
        updates = [u for u in updates if u["new_qty"] < u["listing"]["qty"]]

    return updates


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3C — DETECCIÓN DE CANIBALIZACIÓN ENTRE CUENTAS
# ─────────────────────────────────────────────────────────────────────────────

def _detect_cannibalization(ml_by_sku: dict[str, list]) -> list[dict]:
    """
    Detecta SKUs activos en 2+ cuentas ML donde solo 1 cuenta está vendiendo.

    Señal: mismo SKU, qty > 0 en múltiples cuentas, pero ventas concentradas en
    1 sola cuenta (o ninguna). Esto indica que las cuentas sin ventas están
    compitiendo y dividiendo visibilidad sin convertir.

    Retorna lista de {sku, active_accounts, selling_accounts, items} por SKU canibalizador.
    """
    cannibals = []
    for base, listings in ml_by_sku.items():
        # Solo ML activo con qty > 0
        active = [
            lst for lst in listings
            if lst.get("platform") == "ml"
            and lst.get("qty", 0) > 0
            and lst.get("status") == "active"
        ]
        if len(active) < 2:
            continue  # menos de 2 cuentas activas → no hay canibalización

        active_accounts = list({lst["account_id"] for lst in active})
        # Cuentas que tienen ventas históricas (sold_qty del listing)
        selling_accounts = list({
            lst["account_id"] for lst in active if lst.get("sold_qty", 0) > 0
        })

        # Solo flag si 0 o 1 cuenta están vendiendo mientras 2+ están activas
        if len(selling_accounts) <= 1:
            cannibals.append({
                "sku":              base,
                "active_accounts":  active_accounts,
                "selling_accounts": selling_accounts,
                "active_qty_total": sum(lst["qty"] for lst in active),
                "items":            [
                    {"account_id": lst["account_id"], "item_id": lst.get("item_id"), "qty": lst["qty"], "sold_qty": lst.get("sold_qty", 0)}
                    for lst in active
                ],
            })

    cannibals.sort(key=lambda x: x["active_qty_total"], reverse=True)
    return cannibals


# ─────────────────────────────────────────────────────────────────────────────
# EJECUCIÓN DE UPDATES
# ─────────────────────────────────────────────────────────────────────────────

async def _execute(updates: list[dict], ml_clients: dict, amz_clients: dict) -> list[dict]:
    """
    Ejecuta las actualizaciones de stock.
    ml_clients:  {user_id: MeliClient}
    amz_clients: {seller_id: AmazonClient}
    """
    results = []

    for entry in updates:
        lst      = entry["listing"]
        new_qty  = entry["new_qty"]
        platform = lst["platform"]
        acct     = lst["account_id"]

        try:
            if platform == "ml":
                client = ml_clients.get(acct)
                if not client:
                    raise ValueError(f"Sin cliente ML para {acct}")
                # Si el listing está pausado y vamos a subir stock, activar primero
                if new_qty > 0 and lst.get("status") == "paused":
                    try:
                        await client.update_item_status(lst["item_id"], "active")
                        logger.info(f"[MULTI-SYNC] Activado listing pausado {lst['item_id']}")
                        await asyncio.sleep(0.3)
                    except Exception as exc_act:
                        logger.warning(
                            f"[MULTI-SYNC] No se pudo activar {lst['item_id']}: {exc_act}"
                        )
                await client.update_item_stock(lst["item_id"], new_qty)
            else:
                client = amz_clients.get(acct)
                if not client:
                    raise ValueError(f"Sin cliente Amazon para {acct}")
                await client.update_listing_quantity(lst["sku"], new_qty)

            results.append({
                "sku":        lst.get("sku", ""),
                "platform":   platform,
                "account_id": acct,
                "ref":        lst.get("item_id") or lst.get("sku"),
                "new_qty":    new_qty,
                "reason":     entry["reason"],
                "prev_status": lst.get("status", "active"),
                "ok":         True,
                "error":      None,
            })
            logger.info(
                f"[MULTI-SYNC] {lst.get('sku')} | {platform}/{acct} "
                f"→ qty={new_qty} ({entry['reason']}, prev_status={lst.get('status','active')})"
            )

        except Exception as exc:
            err = str(exc)[:120]
            results.append({
                "sku":        lst.get("sku", ""),
                "platform":   platform,
                "account_id": acct,
                "ref":        lst.get("item_id") or lst.get("sku"),
                "new_qty":    new_qty,
                "reason":     entry["reason"],
                "prev_status": lst.get("status", "active"),
                "ok":         False,
                "error":      err,
            })
            logger.warning(
                f"[MULTI-SYNC] Error {platform}/{acct} sku={lst.get('sku')}: {err}"
            )

        await asyncio.sleep(0.3)   # Rate limiting suave entre updates

    return results


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

async def run_multi_stock_sync() -> dict:
    """
    Ejecuta el ciclo completo de sincronización multi-plataforma.
    Retorna resumen: {status, skus_processed, updates, errors, elapsed_s}.
    """
    global _sync_running, _last_sync_ts, _last_sync_result, _sync_progress, _last_sync_per_account

    if _sync_running:
        return {"status": "already_running"}

    _sync_running = True
    t0 = _time.time()
    summary = {"status": "ok", "skus_processed": 0, "updates": 0, "errors": 0}
    _sync_progress = {
        "phase": "Iniciando...",
        "skus_done": 0, "skus_total": 0,
        "updates": 0, "errors": 0,
        "started_at": t0,
        "error_details": [],
    }

    try:
        from app.services import token_store
        from app.services.meli_client import get_meli_client
        from app.services.amazon_client import get_amazon_client

        # ── Circuit breaker: verificar bm_sku_master antes de tocar stock ─────
        # FIX 2026-08-20 (directiva de Jovan: "nada debe llamar a BM por el
        # momento"): antes probaba BM en vivo con un SKU canario
        # (SNTV001764). Ahora que _fetch_bm_avail lee bm_sku_master en vez de
        # llamar a BM, la señal equivalente es "¿el loop de categorías sigue
        # actualizando la tabla?" -- si _update_bm_master_for_category lleva
        # fallando repetido (BM caído/degradado), stock_updated_at deja de
        # avanzar (el fix de "0 filas = no tocar nada" ya garantiza esto).
        try:
            _cb_meta = await token_store.get_bm_master_sync_meta()
            _cb_age_s = _time.time() - (_cb_meta.get("last_sync_ts") or 0)
            if not _cb_meta.get("total_skus"):
                raise RuntimeError("bm_sku_master vacío -- el loop de categorías no ha corrido todavía")
            if _cb_age_s > _BM_MASTER_STALE_THRESHOLD_S:
                raise RuntimeError(f"bm_sku_master desactualizado hace {_cb_age_s:.0f}s (>{_BM_MASTER_STALE_THRESHOLD_S:.0f}s)")
        except Exception as _cb_exc:
            logger.warning(
                f"[MULTI-SYNC] bm_sku_master no confiable ({_cb_exc.__class__.__name__}: {_cb_exc}) "
                f"— sync ABORTADO para proteger stock en ML/Amazon"
            )
            summary["status"] = "bm_down"
            summary["skipped"] = True
            return summary
        # ── Fin circuit breaker ───────────────────────────────────────────────

        _sync_progress["phase"] = "Recopilando listings..."
        ml_accounts  = await token_store.get_all_tokens()
        amz_accounts = await token_store.get_all_amazon_accounts()

        if not ml_accounts and not amz_accounts:
            return {"status": "no_accounts"}

        logger.info(
            f"[MULTI-SYNC] Inicio — {len(ml_accounts)} ML, {len(amz_accounts)} Amazon"
        )

        # Inicializar stats por cuenta (todas las cuentas, aunque no tengan updates)
        _per_acct: dict = {}
        for _a in ml_accounts:
            _k = f"ml/{_a.get('user_id', '')}"
            _per_acct[_k] = {
                "platform": "ml", "account_id": _a.get("user_id", ""),
                "nickname": _a.get("nickname", _a.get("user_id", ""))[:20],
                "updates": 0, "errors": 0, "last_error": None, "ts": t0,
            }
        for _a in amz_accounts:
            _k = f"amz/{_a.get('seller_id', '')}"
            _per_acct[_k] = {
                "platform": "amz", "account_id": _a.get("seller_id", ""),
                "nickname": _a.get("nickname", _a.get("seller_id", ""))[:20],
                "updates": 0, "errors": 0, "last_error": None, "ts": t0,
            }

        # Recopilar listings ML y Amazon en paralelo
        ml_by_sku, amz_by_sku = await asyncio.gather(
            _collect_ml_listings(ml_accounts),
            _collect_amz_listings(amz_accounts),
        )

        all_bases = set(ml_by_sku.keys()) | set(amz_by_sku.keys())
        if not all_bases:
            logger.info("[MULTI-SYNC] Sin SKUs — skip")
            return {"status": "no_skus"}

        logger.info(f"[MULTI-SYNC] {len(all_bases)} SKUs base encontrados")

        _sync_progress["phase"] = "Consultando BinManager..."
        _sync_progress["skus_total"] = len(all_bases)

        # Consultar BM por SKU+condición específica para no mezclar stock entre condiciones
        global _last_bm_stock
        sku_cond_map = {k: _cond_for_key(k) for k in all_bases}
        bm_stock = await _fetch_bm_avail(sku_cond_map)
        _last_bm_stock = dict(bm_stock)  # snapshot para que main.py lo use en prewarm

        _sync_progress["phase"] = "Aplicando reglas..."

        # Reglas de plataforma por SKU (tabla sku_platform_rules)
        try:
            all_rules = await token_store.get_all_sku_platform_rules()
        except Exception:
            all_rules = {}

        # Costo BM + envío estimado real -- batch, 1 query para todo el ciclo
        # (función única de ganador, Fix 1 auditoría 2026-08-28). Un fallo aquí
        # no debe abortar el sync completo: _plan()/stock_winner ya manejan
        # cost_mxn=None como "sin costo confiable" (fallback a velocidad).
        try:
            cost_map = await _winner_mod().get_cost_map(list(all_bases))
        except Exception as e:
            logger.warning(f"[MULTI-SYNC] Error obteniendo cost_map: {e}")
            cost_map = {}
        try:
            ship_maps = await _winner_mod().get_shipping_cost_maps(list(all_bases))
        except Exception as e:
            logger.warning(f"[MULTI-SYNC] Error obteniendo ship_cost_maps: {e}")
            ship_maps = {}

        # Pre-instanciar clientes (reutilizados por todos los SKUs)
        ml_clients: dict = {}
        for acc in ml_accounts:
            uid = acc.get("user_id", "")
            if uid:
                try:
                    c = await get_meli_client(user_id=uid)
                    if c:
                        ml_clients[uid] = c
                except Exception:
                    pass

        amz_clients: dict = {}
        for acc in amz_accounts:
            sid = acc.get("seller_id", "")
            if sid:
                try:
                    c = await get_amazon_client(seller_id=sid)
                    if c:
                        amz_clients[sid] = c
                except Exception:
                    pass

        # Fase 3C: detección de canibalización entre cuentas
        global _cannibalization_data
        cannibals = _detect_cannibalization(ml_by_sku)
        _cannibalization_data = cannibals  # persiste para el endpoint
        if cannibals:
            logger.warning(
                f"[MULTI-SYNC] {len(cannibals)} SKUs con canibalización detectada: "
                + ", ".join(c["sku"] for c in cannibals[:5])
            )
            summary["cannibalization"] = cannibals

        _sync_progress["phase"] = "Actualizando plataformas..."
        _sync_progress["skus_done"] = 0   # reset — ahora contamos plataformas actualizadas

        # Protección nocturna: solo reducciones en horario 22–06 CST
        ro = _is_reduce_only_mode()
        if ro:
            logger.info("[MULTI-SYNC] Protección nocturna activa — solo reducciones de stock")
            summary["reduce_only"] = True

        # Procesar cada SKU base
        all_results: list = []
        for base in sorted(all_bases):
            if base not in bm_stock:
                # BM tuvo error para este SKU → skip completo (no poner en 0)
                logger.warning(f"[MULTI-SYNC] Skip {base}: BM no retornó datos (error de API)")
                continue
            bm_avail  = bm_stock[base]
            listings  = (ml_by_sku.get(base) or []) + (amz_by_sku.get(base) or [])
            enabled   = set(all_rules.get(base, []))

            updates = await _plan(
                base, bm_avail, listings, enabled, reduce_only=ro,
                cost_mxn=cost_map.get(base), ship_cost_maps=ship_maps,
                persist_winner=True,
            )
            if not updates:
                _sync_progress["skus_done"] += 1
                continue

            summary["skus_processed"] += 1
            res = await _execute(updates, ml_clients, amz_clients)
            all_results.extend(res)
            ok_n  = sum(1 for r in res if r["ok"])
            err_n = sum(1 for r in res if not r["ok"])
            summary["updates"] += ok_n
            summary["errors"]  += err_n
            _sync_progress["skus_done"] += 1
            _sync_progress["updates"]   += ok_n
            _sync_progress["errors"]    += err_n
            # Acumular stats por cuenta
            for r in res:
                _plt = r.get('platform', 'ml')
                _plt = 'amz' if _plt == 'amazon' else _plt  # normalizar "amazon" → "amz"
                _rk = f"{_plt}/{r.get('account_id', '')}"
                if _rk in _per_acct:
                    if r["ok"]:
                        _per_acct[_rk]["updates"] += 1
                    else:
                        _per_acct[_rk]["errors"] += 1
                        if not _per_acct[_rk]["last_error"]:
                            _per_acct[_rk]["last_error"] = str(r.get("error", ""))[:100]
            if err_n:
                for r in res:
                    if not r["ok"]:
                        _sync_progress["error_details"].append({
                            "sku": base, "platform": r.get("platform", "?"),
                            "msg": str(r.get("error", ""))[:120],
                        })

        # Guardar log en DB
        try:
            await token_store.save_multi_sync_log(
                ts=t0,
                skus_processed=summary["skus_processed"],
                updates=summary["updates"],
                errors=summary["errors"],
                results=all_results,
            )
        except Exception as e:
            logger.warning(f"[MULTI-SYNC] Error guardando log: {e}")

        summary["elapsed_s"] = round(_time.time() - t0, 1)
        logger.info(
            f"[MULTI-SYNC] Completado en {summary['elapsed_s']}s — "
            f"{summary['skus_processed']} SKUs, {summary['updates']} updates, "
            f"{summary['errors']} errores"
        )
        # Actualizar ml_listings DB con qty empujadas a ML — evita que el prewarm
        # calcule alertas falsas (Riesgo Sobreventa) con datos ML stale.
        _ml_db_updates = [
            (r["ref"], r["new_qty"])
            for r in all_results
            if r.get("ok") and r.get("platform") == "ml" and r.get("ref")
        ]
        if _ml_db_updates:
            try:
                await token_store.bulk_update_ml_listing_qtys(_ml_db_updates)
                logger.info(f"[MULTI-SYNC] ml_listings DB actualizada: {len(_ml_db_updates)} items")
            except Exception as _e:
                logger.warning(f"[MULTI-SYNC] Error actualizando ml_listings DB: {_e}")

        # Notificar a main.py para que invalide _products_cache + _stock_issues_cache
        if _on_sync_complete:
            try:
                await _on_sync_complete()
            except Exception as _cb_e:
                logger.warning(f"[MULTI-SYNC] Error en sync_complete callback: {_cb_e}")

        # Guardar desglose por cuenta (ML primero, luego Amazon, ordenado por nombre)
        _last_sync_per_account = sorted(
            _per_acct.values(),
            key=lambda x: (0 if x["platform"] == "ml" else 1, x["nickname"].lower()),
        )

    except Exception as exc:
        logger.exception(f"[MULTI-SYNC] Error fatal: {exc}")
        summary["status"] = "error"
        summary["error"]  = str(exc)[:200]
    finally:
        _sync_running     = False
        _last_sync_ts     = _time.time()
        _last_sync_result = summary
        _sync_progress    = {}

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-ACCOUNT SYNC
# ─────────────────────────────────────────────────────────────────────────────

async def run_single_account_stock_sync(platform: str, account_id: str) -> dict:
    """Ejecuta el sync de stock para UNA sola cuenta.
    platform: "ml" | "amz"
    account_id: user_id (ML) o seller_id (Amazon)
    Usa el mismo circuit-breaker BM y reglas que run_multi_stock_sync.
    Solo actualiza la entrada de esa cuenta en _last_sync_per_account.
    """
    global _sync_running, _last_sync_per_account

    if _sync_running:
        return {"status": "already_running"}

    _sync_running = True
    t0 = _time.time()
    summary = {
        "status": "ok", "platform": platform,
        "account_id": account_id, "updates": 0, "errors": 0,
    }

    try:
        from app.services import token_store
        from app.services.meli_client import get_meli_client
        from app.services.amazon_client import get_amazon_client

        # ── Circuit breaker (bm_sku_master, ver run_multi_stock_sync) ─────────
        try:
            _cb_meta = await token_store.get_bm_master_sync_meta()
            _cb_age_s = _time.time() - (_cb_meta.get("last_sync_ts") or 0)
            if not _cb_meta.get("total_skus"):
                raise RuntimeError("bm_sku_master vacío -- el loop de categorías no ha corrido todavía")
            if _cb_age_s > _BM_MASTER_STALE_THRESHOLD_S:
                raise RuntimeError(f"bm_sku_master desactualizado hace {_cb_age_s:.0f}s (>{_BM_MASTER_STALE_THRESHOLD_S:.0f}s)")
        except Exception as _cb_exc:
            logger.warning(
                f"[SINGLE-SYNC] bm_sku_master no confiable ({_cb_exc.__class__.__name__}: {_cb_exc}) "
                f"— sync ABORTADO para {platform}/{account_id}"
            )
            summary["status"] = "bm_down"
            return summary

        # ── Recopilar listings de TODAS las cuentas (ML + Amazon) ─────────────
        # FIX SOBREVENTA CROSS-CUENTA (2026-08-28, auditoría aprobada por Jovan):
        # antes esta función solo recolectaba listings de la cuenta clickeada
        # (_collect_ml_listings([acc])), así que _plan() siempre "ganaba" con
        # un solo candidato -- si se clickeaba "Sync" en 2+ cuentas para el
        # mismo SKU escaso, AMBAS terminaban escribiendo el pool completo de
        # BM (sobreventa real). Ahora se recolecta TODO el universo (igual que
        # run_multi_stock_sync) para calcular el score/plan real, pero la
        # EJECUCIÓN (update_item_stock) se filtra más abajo a solo los
        # listings que pertenecen a la cuenta que el usuario clickeó.
        all_ml  = await token_store.get_all_tokens()
        all_amz = await token_store.get_all_amazon_accounts()

        acc = None
        if platform == "ml":
            acc = next((a for a in all_ml if a.get("user_id") == account_id), None)
        else:  # amz
            acc = next((a for a in all_amz if a.get("seller_id") == account_id), None)
        if not acc:
            return {"status": "account_not_found"}

        ml_by_sku, amz_by_sku = await asyncio.gather(
            _collect_ml_listings(all_ml),
            _collect_amz_listings(all_amz),
        )

        all_bases = set(ml_by_sku.keys()) | set(amz_by_sku.keys())
        if not all_bases:
            summary["status"] = "no_skus"
            return summary

        # ── BM stock para estos SKUs ───────────────────────────────────────────
        sku_cond_map = {k: _cond_for_key(k) for k in all_bases}
        bm_stock = await _fetch_bm_avail(sku_cond_map)

        # ── Reglas de plataforma ───────────────────────────────────────────────
        try:
            all_rules = await token_store.get_all_sku_platform_rules()
        except Exception:
            all_rules = {}

        # ── Costo BM + envío estimado real (función única de ganador, Fix 1) ──
        try:
            cost_map = await _winner_mod().get_cost_map(list(all_bases))
        except Exception as e:
            logger.warning(f"[SINGLE-SYNC] Error obteniendo cost_map: {e}")
            cost_map = {}
        try:
            ship_maps = await _winner_mod().get_shipping_cost_maps(list(all_bases))
        except Exception as e:
            logger.warning(f"[SINGLE-SYNC] Error obteniendo ship_cost_maps: {e}")
            ship_maps = {}

        # ── Clientes: solo se instancia el de la cuenta que el usuario clickeó
        # -- las demás cuentas se recolectaron solo para calcular el plan/score
        # real, nunca se les va a escribir nada desde esta llamada.
        if platform == "ml":
            try:
                c = await get_meli_client(user_id=account_id)
                ml_clients  = {account_id: c} if c else {}
            except Exception:
                ml_clients  = {}
            amz_clients = {}
        else:
            ml_clients  = {}
            try:
                c = await get_amazon_client(seller_id=account_id)
                amz_clients = {account_id: c} if c else {}
            except Exception:
                amz_clients = {}

        ro = _is_reduce_only_mode()
        all_results: list = []
        # Los listings ML usan platform="ml", los de Amazon usan platform="amazon"
        # (no "amz") -- ver _collect_amz_listings.
        _target_platform = "ml" if platform == "ml" else "amazon"

        for base in sorted(all_bases):
            if base not in bm_stock:
                continue
            bm_avail = bm_stock[base]
            listings = (ml_by_sku.get(base) or []) + (amz_by_sku.get(base) or [])
            enabled  = set(all_rules.get(base, []))
            updates  = await _plan(
                base, bm_avail, listings, enabled, reduce_only=ro,
                cost_mxn=cost_map.get(base), ship_cost_maps=ship_maps,
                persist_winner=True,
            )
            if not updates:
                continue
            # FIX SOBREVENTA: el plan se calculó sobre TODAS las cuentas
            # (necesario para saber quién es el ganador real), pero solo se
            # EJECUTA lo que le corresponde a la cuenta clickeada. Si es la
            # ganadora → escribe bm_avail real. Si es perdedora → escribe 0.
            # Si ni siquiera tiene el SKU → no aparece aquí, no-op para ella.
            my_updates = [
                u for u in updates
                if u["listing"]["platform"] == _target_platform
                and u["listing"]["account_id"] == account_id
            ]
            if not my_updates:
                continue
            res = await _execute(my_updates, ml_clients, amz_clients)
            all_results.extend(res)
            summary["updates"] += sum(1 for r in res if r["ok"])
            summary["errors"]  += sum(1 for r in res if not r["ok"])

        # ── Actualizar solo la entrada de esta cuenta en _last_sync_per_account ─
        plat_key = "ml" if platform == "ml" else "amz"
        nickname  = (
            acc.get("nickname") or acc.get("user_id") or acc.get("seller_id") or account_id
        )[:20]
        last_err = None
        if summary["errors"] > 0:
            bad = next((r for r in all_results if not r["ok"]), None)
            if bad:
                last_err = str(bad.get("error", ""))[:100]
        new_entry = {
            "platform": plat_key, "account_id": account_id,
            "nickname": nickname,
            "updates": summary["updates"], "errors": summary["errors"],
            "last_error": last_err, "ts": t0,
        }
        existing = list(_last_sync_per_account)
        found = False
        for i, entry in enumerate(existing):
            if entry.get("platform") == plat_key and entry.get("account_id") == account_id:
                existing[i] = new_entry
                found = True
                break
        if not found:
            existing.append(new_entry)
        _last_sync_per_account = sorted(
            existing,
            key=lambda x: (0 if x["platform"] == "ml" else 1, x["nickname"].lower()),
        )

        # Actualizar ml_listings DB si fue ML
        if platform == "ml":
            _ml_db_updates = [
                (r["ref"], r["new_qty"])
                for r in all_results
                if r.get("ok") and r.get("platform") == "ml" and r.get("ref")
            ]
            if _ml_db_updates:
                try:
                    await token_store.bulk_update_ml_listing_qtys(_ml_db_updates)
                except Exception as _e_bulk:
                    logger.warning(f"[STOCK-SYNC] bulk_update_ml_listing_qtys falló tras sync exitoso en ML: {_e_bulk}")

        summary["elapsed_s"] = round(_time.time() - t0, 1)
        logger.info(
            f"[SINGLE-SYNC] {platform}/{account_id} completado en {summary['elapsed_s']}s "
            f"— {summary['updates']} updates, {summary['errors']} errores"
        )

    except Exception as exc:
        logger.exception(f"[SINGLE-SYNC] Error fatal {platform}/{account_id}: {exc}")
        summary["status"] = "error"
        summary["error"]  = str(exc)[:200]
    finally:
        _sync_running = False

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# PREVIEW / DRY-RUN
# ─────────────────────────────────────────────────────────────────────────────

async def preview_multi_stock_sync() -> dict:
    """Simula el sync completo sin ejecutar ningún cambio en ML ni Amazon.

    Retorna los mismos datos que run_multi_stock_sync pero con dry_run=True:
    la lista de cambios planificados por _plan, sin llamar a _execute.

    Útil para revisar qué se actualizaría antes de confirmar.
    """
    from app.services import token_store

    t0 = _time.time()
    try:
        ml_accounts  = await token_store.get_all_tokens()
        amz_accounts = await token_store.get_all_amazon_accounts()

        ml_by_sku, amz_by_sku = await asyncio.gather(
            _collect_ml_listings(ml_accounts),
            _collect_amz_listings(amz_accounts),
        )

        all_bases = set(ml_by_sku.keys()) | set(amz_by_sku.keys())
        if not all_bases:
            return {"status": "no_skus", "changes": [], "bm_stock": {}}

        sku_cond_map = {k: _cond_for_key(k) for k in all_bases}
        bm_stock = await _fetch_bm_avail(sku_cond_map)

        try:
            all_rules = await token_store.get_all_sku_platform_rules()
        except Exception:
            all_rules = {}

        # Costo BM + envío estimado real -- igual que run_multi_stock_sync,
        # para que la preview muestre el MISMO ganador que produciría una
        # corrida real (función única de ganador, Fix 1). persist_winner=False
        # más abajo: una preview no debe pisar la histéresis persistida.
        try:
            cost_map = await _winner_mod().get_cost_map(list(all_bases))
        except Exception:
            cost_map = {}
        try:
            ship_maps = await _winner_mod().get_shipping_cost_maps(list(all_bases))
        except Exception:
            ship_maps = {}

        ro = _is_reduce_only_mode()
        changes = []

        for base in sorted(all_bases):
            if base not in bm_stock:
                continue
            bm_avail = bm_stock[base]
            listings = (ml_by_sku.get(base) or []) + (amz_by_sku.get(base) or [])
            enabled  = set(all_rules.get(base, []))
            updates  = await _plan(
                base, bm_avail, listings, enabled, reduce_only=ro,
                cost_mxn=cost_map.get(base), ship_cost_maps=ship_maps,
                persist_winner=False,
            )

            for u in updates:
                lst = u["listing"]
                changes.append({
                    "sku":         base,
                    "platform":    lst["platform"],
                    "account_id":  lst["account_id"],
                    "item_id":     lst.get("item_id") or lst.get("sku", ""),
                    "title":       lst.get("title", ""),
                    "status":      lst.get("status", "active"),
                    "current_qty": lst["qty"],
                    "bm_avail":    bm_avail,
                    "new_qty":     u["new_qty"],
                    "reason":      u["reason"],
                })

        return {
            "status":       "ok",
            "changes":      changes,
            "total_skus":   len(all_bases),
            "bm_queried":   len(bm_stock),
            "reduce_only":  ro,
            "elapsed_s":    round(_time.time() - t0, 1),
        }
    except Exception as exc:
        logger.exception(f"[MULTI-SYNC-PREVIEW] Error: {exc}")
        return {"status": "error", "error": str(exc)[:200], "changes": []}


# ─────────────────────────────────────────────────────────────────────────────
# LOOP Y ARRANQUE
# ─────────────────────────────────────────────────────────────────────────────

async def _loop():
    """Loop periódico del sync multi-plataforma."""
    await asyncio.sleep(120)   # 2 min delay al arranque inicial
    while True:
        try:
            await run_multi_stock_sync()
        except Exception as e:
            logger.error(f"[MULTI-SYNC-LOOP] Error inesperado: {e}")
        await asyncio.sleep(_SYNC_INTERVAL)


def start_multi_stock_sync():
    """
    NO inicia loop automático — el sync BM→ML solo se ejecuta manualmente
    (botón 'Sync ahora' en el dashboard). Solo lectura puede ser automática.
    Esta función se mantiene por compatibilidad con el lifespan de FastAPI.
    """
    logger.info("[MULTI-SYNC] Modo manual — sync solo se ejecuta al presionar 'Sync ahora'")


def _is_reduce_only_mode() -> bool:
    """True durante protección nocturna (22:00–06:00 CST). Solo reducciones de stock."""
    if not _nocturnal_protection:
        return False
    from datetime import timezone, timedelta
    cst = timezone(timedelta(hours=-6))
    h = datetime.now(tz=cst).hour
    return h >= 22 or h < 6


def get_last_bm_stock() -> dict:
    """Retorna {sku_upper: avail_int} del último sync — para warm up del prewarm cache."""
    return _last_bm_stock


def get_nocturnal_protection() -> dict:
    return {
        "enabled": _nocturnal_protection,
        "active_now": _is_reduce_only_mode(),
        "hours": "22:00–06:00 CST",
    }


def set_nocturnal_protection(enabled: bool):
    global _nocturnal_protection
    _nocturnal_protection = enabled


def get_cannibalization_data() -> list:
    """Retorna lista de SKUs con canibalización del último sync."""
    return _cannibalization_data


def get_sync_status() -> dict:
    """Estado del último sync para el endpoint /api/stock/multi-sync/status."""
    return {
        "running":      _sync_running,
        "last_sync_ts": _last_sync_ts,
        "last_sync_iso": (
            datetime.utcfromtimestamp(_last_sync_ts).isoformat()
            if _last_sync_ts else None
        ),
        "last_result":  _last_sync_result,
        "interval_min": _SYNC_INTERVAL // 60,
        "threshold":    STOCK_THRESHOLD,
        "progress":     _sync_progress if _sync_running else {},
        "per_account":  list(_last_sync_per_account),
    }
