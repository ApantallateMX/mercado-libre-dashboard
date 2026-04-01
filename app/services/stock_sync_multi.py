"""
stock_sync_multi.py — Sincronización de stock multi-plataforma BM → ML + Amazon

CICLO (cada 5 minutos):
  1. Recopilar listings activos: MeLi (todas las cuentas) + Amazon FBM/FLX (todas las cuentas)
  2. Consultar BinManager avail_total por SKU base
  3. Por cada SKU aplicar regla:
       avail >= 10  → todas las plataformas habilitadas muestran avail_total
       0 < avail < 10 → solo la cuenta ganadora muestra avail_total; resto = 0
       avail == 0   → todas = 0
  4. Ejecutar solo los updates necesarios (evitar API calls cuando qty ya es correcta)
  5. Registrar log en DB

SCORE (ganador cuando avail < 10):
  score = precio × (1 − comisión%) × velocidad_30d
  Maximiza el Ingreso Neto Proyectado → la cuenta que más ganancia genera.

NOTAS:
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

logger = logging.getLogger(__name__)

# ─── BinManager ───────────────────────────────────────────────────────────────
_BM_COND_URL = (
    "https://binmanager.mitechnologiesinc.com"
    "/InventoryReport/InventoryReport/GlobalStock_InventoryBySKU_Condition"
)
_BM_LOC_IDS = "47,62,68"
_BM_COND    = "GRA,GRB,GRC,NEW"

# ─── Comisiones por plataforma ────────────────────────────────────────────────
_ML_FEE  = 0.17   # 17% promedio ML (varía por categoría)
_AMZ_FEE = 0.15   # 15% promedio Amazon

# ─── Regla de distribución ────────────────────────────────────────────────────
STOCK_THRESHOLD = 10   # avail < 10 → concentrar en ganadora

# ─── Ciclo ────────────────────────────────────────────────────────────────────
_SYNC_INTERVAL = 5 * 60   # 5 minutos

# ─── Estado global ────────────────────────────────────────────────────────────
_sync_running      = False
_last_sync_ts      = 0.0
_last_sync_result: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS DE SKU
# ─────────────────────────────────────────────────────────────────────────────

def _base_sku(sku: str) -> str:
    """
    Extrae el SKU base para cruzar con BinManager.
    SNFN000941-FLX01 → SNFN000941
    SNFN000941       → SNFN000941
    ML y Amazon usan el mismo SKU base en BM.
    """
    if not sku:
        return ""
    return sku.upper().split("-")[0]


# ─────────────────────────────────────────────────────────────────────────────
# BINMANAGER — consulta avail_total por SKU base
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_bm_avail(base_skus: list[str]) -> dict[str, int]:
    """
    Consulta BM para una lista de SKUs base en paralelo (máx 10 concurrentes).
    Retorna {sku_base_upper: avail_total} donde avail_total = unidades "Producto Vendible".
    """
    result: dict[str, int] = {}
    if not base_skus:
        return result

    sem = asyncio.Semaphore(10)

    async def _one(base: str, http: httpx.AsyncClient) -> None:
        payload = {
            "COMPANYID":  1,      "SKU":        base,
            "WAREHOUSEID": None,  "LOCATIONID": _BM_LOC_IDS,
            "BINID":       None,  "CONDITION":  _BM_COND,
            "FORINVENTORY": 0,    "SUPPLIERS":  None,
        }
        async with sem:
            try:
                r = await http.post(_BM_COND_URL, json=payload, timeout=15.0)
                rows = r.json() if r.status_code == 200 else []
                if not isinstance(rows, list):
                    rows = []
                avail = 0
                for row in rows:
                    cj = row.get("Conditions_JSON")
                    if cj is not None:
                        if isinstance(cj, str):
                            try:
                                cj = json.loads(cj)
                            except Exception:
                                cj = []
                        for cond in (cj if isinstance(cj, list) else []):
                            for item in (cond.get("SKUCondition_JSON") or []):
                                qty = item.get("TotalQty", 0) or 0
                                if item.get("status") == "Producto Vendible":
                                    avail += qty
                    else:
                        qty = row.get("TotalQty", 0) or 0
                        if row.get("status") == "Producto Vendible":
                            avail += qty
                result[base.upper()] = avail
            except Exception as exc:
                logger.warning(f"[MULTI-SYNC-BM] Error {base}: {exc}")
                result[base.upper()] = 0

    async with httpx.AsyncClient(timeout=20.0) as http:
        await asyncio.gather(*[_one(b, http) for b in base_skus], return_exceptions=True)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ML — recopilar listings activos de todas las cuentas
# ─────────────────────────────────────────────────────────────────────────────

async def _collect_ml_listings(ml_accounts: list) -> dict[str, list]:
    """
    Retorna {base_sku: [listing_dict, ...]} para todos los items activos ML.
    listing_dict: {platform, account_id, item_id, price, qty, sold_qty, date_created, sku}
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

            item_ids = await client.get_all_item_ids_by_statuses(["active"])
            if not item_ids:
                continue

            # Batch de 20 (límite ML)
            for i in range(0, len(item_ids), 20):
                batch = item_ids[i : i + 20]
                try:
                    resp = await client.get("/items", params={"ids": ",".join(batch)})
                    entries = resp if isinstance(resp, list) else []
                    for entry in entries:
                        item = entry.get("body") if isinstance(entry, dict) and "body" in entry else entry
                        if not isinstance(item, dict):
                            continue
                        sku = (item.get("seller_custom_field") or "").strip()
                        if not sku:
                            continue
                        base = _base_sku(sku)
                        if not base:
                            continue
                        by_sku.setdefault(base, []).append({
                            "platform":      "ml",
                            "account_id":    uid,
                            "item_id":       str(item.get("id", "")),
                            "price":         float(item.get("price") or 0),
                            "qty":           int(item.get("available_quantity") or 0),
                            "sold_qty":      int(item.get("sold_quantity") or 0),
                            "date_created":  item.get("date_created", ""),
                            "sku":           sku,
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

                base = _base_sku(sku)
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

                by_sku.setdefault(base, []).append({
                    "platform":     "amazon",
                    "account_id":   sid,
                    "sku":          sku,
                    "price":        price,
                    "qty":          qty,
                    "sold_qty_30d": sold_30d,
                    "is_flx":       is_flx,
                })

        except Exception as e:
            logger.warning(f"[MULTI-SYNC-AMZ] Cuenta {sid}: {e}")

    return by_sku


# ─────────────────────────────────────────────────────────────────────────────
# SCORE — Ingreso Neto Proyectado 30d
# ─────────────────────────────────────────────────────────────────────────────

def _score(listing: dict) -> float:
    """
    score = precio_neto × velocidad_30d
    Representa el ingreso neto estimado que generaría esta plataforma en 30 días.
    Mínimo retorna el precio_neto para que ninguna plataforma quede con score=0
    si tiene precio pero no tiene historial de ventas.
    """
    price    = float(listing.get("price") or 0)
    platform = listing.get("platform", "ml")

    if platform == "ml":
        net_price = price * (1 - _ML_FEE)
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
    return net_price * max(1.0, velocity)


# ─────────────────────────────────────────────────────────────────────────────
# PLANIFICACIÓN DE DISTRIBUCIÓN
# ─────────────────────────────────────────────────────────────────────────────

def _plan(base_sku: str, bm_avail: int, listings: list, enabled_ids: set) -> list[dict]:
    """
    Calcula las actualizaciones necesarias para un SKU base.

    enabled_ids: set de "ml_{user_id}" o "amz_{seller_id}" habilitados para este SKU.
                 Si está vacío → todas las plataformas habilitadas.

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

    updates = []

    if bm_avail == 0:
        # Poner todo en 0
        for lst in listings:
            if lst["qty"] != 0:
                updates.append({"listing": lst, "new_qty": 0, "reason": "bm_zero"})

    elif bm_avail < STOCK_THRESHOLD:
        # Concentrar en la cuenta ganadora (mayor score)
        scored  = sorted(listings, key=_score, reverse=True)
        winner  = scored[0]
        for lst in listings:
            new_qty = bm_avail if lst is winner else 0
            if lst["qty"] != new_qty:
                reason = "concentrate_winner" if lst is winner else "concentrate_loser"
                updates.append({"listing": lst, "new_qty": new_qty, "reason": reason})

    else:
        # Distribuir: todas las plataformas muestran avail_total completo
        for lst in listings:
            if lst["qty"] != bm_avail:
                updates.append({"listing": lst, "new_qty": bm_avail, "reason": "distribute"})

    return updates


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
                await client.update_item_stock(lst["item_id"], new_qty)
            else:
                client = amz_clients.get(acct)
                if not client:
                    raise ValueError(f"Sin cliente Amazon para {acct}")
                await client.update_listing_quantity(lst["sku"], new_qty)

            results.append({
                "sku":       lst.get("sku", ""),
                "platform":  platform,
                "account_id": acct,
                "ref":       lst.get("item_id") or lst.get("sku"),
                "new_qty":   new_qty,
                "reason":    entry["reason"],
                "ok":        True,
                "error":     None,
            })
            logger.info(
                f"[MULTI-SYNC] {lst.get('sku')} | {platform}/{acct} "
                f"→ qty={new_qty} ({entry['reason']})"
            )

        except Exception as exc:
            err = str(exc)[:120]
            results.append({
                "sku":       lst.get("sku", ""),
                "platform":  platform,
                "account_id": acct,
                "ref":       lst.get("item_id") or lst.get("sku"),
                "new_qty":   new_qty,
                "reason":    entry["reason"],
                "ok":        False,
                "error":     err,
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
    global _sync_running, _last_sync_ts, _last_sync_result

    if _sync_running:
        return {"status": "already_running"}

    _sync_running = True
    t0 = _time.time()
    summary = {"status": "ok", "skus_processed": 0, "updates": 0, "errors": 0}

    try:
        from app.services import token_store
        from app.services.meli_client import get_meli_client
        from app.services.amazon_client import get_amazon_client

        ml_accounts  = await token_store.get_all_tokens()
        amz_accounts = await token_store.get_all_amazon_accounts()

        if not ml_accounts and not amz_accounts:
            return {"status": "no_accounts"}

        logger.info(
            f"[MULTI-SYNC] Inicio — {len(ml_accounts)} ML, {len(amz_accounts)} Amazon"
        )

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

        # Consultar BM para todos los SKUs base
        bm_stock = await _fetch_bm_avail(list(all_bases))

        # Reglas de plataforma por SKU (tabla sku_platform_rules)
        try:
            all_rules = await token_store.get_all_sku_platform_rules()
        except Exception:
            all_rules = {}

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

        # Procesar cada SKU base
        all_results: list = []
        for base in sorted(all_bases):
            bm_avail  = bm_stock.get(base, 0)
            listings  = (ml_by_sku.get(base) or []) + (amz_by_sku.get(base) or [])
            enabled   = set(all_rules.get(base, []))

            updates = _plan(base, bm_avail, listings, enabled)
            if not updates:
                continue

            summary["skus_processed"] += 1
            res = await _execute(updates, ml_clients, amz_clients)
            all_results.extend(res)
            summary["updates"] += sum(1 for r in res if r["ok"])
            summary["errors"]  += sum(1 for r in res if not r["ok"])

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

    except Exception as exc:
        logger.exception(f"[MULTI-SYNC] Error fatal: {exc}")
        summary["status"] = "error"
        summary["error"]  = str(exc)[:200]
    finally:
        _sync_running     = False
        _last_sync_ts     = _time.time()
        _last_sync_result = summary

    return summary


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
    """Inicia el loop en background. Llamar desde lifespan de FastAPI."""
    asyncio.create_task(_loop())
    logger.info(f"[MULTI-SYNC] Iniciado — ciclo cada {_SYNC_INTERVAL // 60} min, umbral={STOCK_THRESHOLD}")


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
    }
