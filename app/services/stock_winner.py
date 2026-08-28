"""
stock_winner.py — Función única de "ganador" para concentración de stock
==========================================================================
Consolida las 3 fórmulas de ganador que existían por separado antes de esta
auditoría (2026-08-28, aprobada explícitamente por Jovan):
  - stock_sync_multi._score()                    (ciclo automático cada 5 min)
  - stock_concentrator.preview_concentration()    (botón manual "Concentrar")
  - fórmula de margen de contribución diseñada por marketplace-strategist

FÓRMULA PRINCIPAL (margen de contribución real, no solo velocidad de venta):

    margen_contribucion_u(c) = precio_venta(c) − costo_bm
                                − [precio_venta(c) × comision(c) × 1.16]
                                − [costo_envio_est(c) × 1.16]
    unidades_esperadas(c)     = min(stock_a_concentrar, ventas_30d(c))
    score(c)                  = unidades_esperadas(c) × margen_contribucion_u(c) × rep_factor(c)

    Exclusión dura: rep_factor(c) <= rep_factor("1_red") → el candidato NUNCA
    puede ganar por reputación roja, sin importar qué tan bueno sea su margen.
    Esto es una exclusión, no un descuento (a diferencia del resto de niveles
    de reputación, que sí son un multiplicador continuo — ver _REPUTATION_FACTOR
    en stock_sync_multi.py).

FALLBACKS (en este orden):
  1. Sin ventas 30d en ninguna cuenta elegible, pero SÍ ventas históricas →
     unidades_esperadas usa ventas_totales en vez de ventas_30d.
     method = "margin_fallback_total".
  2. Sin costo BM confiable (ausente, <=0, o el centinela "sin costo" >=9000 —
     REGLA DURA del proyecto, jamás usar ese valor como costo real) → cae a la
     fórmula vieja de solo-velocidad×reputación (sin margen), SOLO para este
     SKU específico. method = "velocity_only_no_cost".
  3. Sin ventas en NINGUNA cuenta elegible (ni 30d ni histórico) → no se toma
     como ganador "de facto" con confianza: se marca manual_selection=True y
     la sugerencia deja de ser "mayor stock disponible" para pasar a
     argmax(margen_contribucion_u(c) × rep_factor(c)) -- mejor prior que cajas
     guardadas. Si tampoco hay costo confiable, el prior análogo sin margen es
     argmax(precio_neto(c) × rep_factor(c)) (caso doblemente degradado, no
     especificado explícitamente en el diseño de negocio -- documentado para
     que Jovan lo revise).

HISTÉRESIS: ver resolve_winner(). Un ganador ya persistido en
stock_winner_cache solo se reemplaza si el candidato nuevo supera el score
RECALCULADO HOY del ganador anterior por >15% (evita que el "ganador" cambie
de una corrida a otra solo por ruido de precio/ventas).

NOTA DE DISEÑO — costo de envío estimado: se usa el promedio REAL de los
últimos 90 días por SKU+plataforma (token_store.get_avg_shipping_cost_map,
ya alimentado por order_history con shipping_cost_mxn real por orden). Si no
hay al menos 3 muestras reales para ese SKU+plataforma, se usa el proxy plano
_SHIP_FALLBACK_MXN (150 MXN, mismo valor que ya usaba _score() en
stock_sync_multi.py para ML). Amazon no tenía un proxy propio en el código
existente -- se reutiliza el mismo valor por falta de un dato mejor. Jovan
debe revisar si 150 MXN es razonable como estimado de envío FBM/Seller-Flex
en Amazon, o si conviene un valor propio.
"""

import asyncio
import logging

from app.services import token_store
from app.services.stock_sync_multi import _REPUTATION_FACTOR, _ml_fee, _AMZ_FEE, _bm_base_for_key

logger = logging.getLogger(__name__)

_COST_SENTINEL     = 9000       # AvgCostQTY >= 9000 == "sin costo" (REGLA DURA, ver CLAUDE.md)
_SHIP_FALLBACK_MXN = 150.0      # proxy cuando no hay >=3 muestras reales de envío para el SKU+plataforma
_IVA               = 0.16
_RED_THRESHOLD     = _REPUTATION_FACTOR["1_red"]   # 0.15 -- exclusión dura, no descuento
_HYSTERESIS_FACTOR = 1.15       # el candidato nuevo debe superar al ganador actual por >15% para reemplazarlo


# ─────────────────────────────────────────────────────────────────────────────
# Candidato normalizado — forma común entre listings ML y Amazon
# ─────────────────────────────────────────────────────────────────────────────

def build_candidate(*, platform: str, account_id, nickname: str = "", ref: str = "",
                     price: float = 0.0, sold_30d: int = 0, sold_total: int = 0,
                     rep_factor: float = 1.0, can_update: bool = True,
                     status: str = "active", extra: dict | None = None) -> dict:
    """Normaliza un listing (ML o Amazon) a la forma común que usa
    score_candidates()/resolve_winner(). El caller es responsable de filtrar
    de antemano a solo los listings 'updatable' (no FULL/FLX) -- este módulo
    no conoce esa regla de plataforma.

    ref: identificador para ejecutar el update real (item_id en ML, sku en
         Amazon) -- este módulo no lo usa, solo lo transporta.
    extra: dict libre para que el caller reconstruya su objeto original
           (el listing crudo) a partir del candidato ganador, sin que este
           módulo necesite conocer esa forma.
    """
    return {
        "platform":    platform,
        "account_id":  str(account_id),
        "nickname":    nickname or str(account_id),
        "ref":         ref,
        "price":       float(price or 0),
        "sold_30d":    int(sold_30d or 0),
        "sold_total":  int(sold_total or 0),
        "rep_factor":  float(rep_factor) if rep_factor is not None else 1.0,
        "can_update":  bool(can_update),
        "status":      status,
        "extra":       extra or {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Costo BM y costo de envío estimado — batch (1 query para todo un ciclo)
# ─────────────────────────────────────────────────────────────────────────────

def _get_fx_rate() -> float:
    """Tasa USD→MXN vigente (override manual o última tasa cacheada del
    prewarm en main.py). Import diferido: main.py importa (indirectamente, vía
    stock_concentrator/stock_sync_multi) este módulo al arrancar, así que un
    import a nivel de módulo aquí crearía un ciclo. En tiempo de llamada
    (post-arranque) main.py ya está completamente cargado -- mismo patrón que
    amazon_listing_sync.py y marketplace_alerts.py ya usan para leer de vuelta
    hacia main.py."""
    try:
        import app.main as _main_mod
        return _main_mod._manual_fx_rate if _main_mod._manual_fx_rate > 0 else (_main_mod._last_fx_rate or 17.0)
    except Exception:
        return 17.0


async def get_cost_map(base_skus: list[str]) -> dict[str, float]:
    """{base_sku_original: costo_unitario_mxn} solo para SKUs con costo BM
    confiable (excluye <=0 y el centinela >=9000 -- REGLA DURA del proyecto,
    nunca usar ese valor como costo real). Batch: 1 sola query para todo un
    ciclo de sync, mismo patrón que _sku_cost_map/all_rules en
    stock_sync_multi.py (evita N+1 en el loop por-SKU)."""
    if not base_skus:
        return {}
    lookup_to_orig: dict[str, str] = {}
    for s in base_skus:
        lookup_to_orig[_bm_base_for_key(s).upper()] = s.upper()
    rows = await token_store.get_bm_master_rows_for_skus(list(lookup_to_orig.keys()))
    fx = _get_fx_rate()
    out: dict[str, float] = {}
    for lookup_sku, orig_sku in lookup_to_orig.items():
        row = rows.get(lookup_sku)
        if not row:
            continue
        cost_usd = row.get("cost_usd") or 0
        if cost_usd <= 0 or cost_usd >= _COST_SENTINEL:
            continue
        out[orig_sku] = round(cost_usd * fx, 2)
    return out


async def get_shipping_cost_maps(base_skus: list[str]) -> dict[str, dict[str, float]]:
    """{"ml": {sku: mxn_promedio_real}, "amazon": {sku: mxn_promedio_real}}
    usando el histórico real de envío de los últimos 90 días
    (token_store.get_avg_shipping_cost_map). Un SKU ausente de un sub-dict
    significa "sin >=3 muestras reales" -- el caller debe usar
    _SHIP_FALLBACK_MXN en ese caso (ver _ship_cost_for)."""
    if not base_skus:
        return {"ml": {}, "amazon": {}}
    ml_map, amz_map = await asyncio.gather(
        token_store.get_avg_shipping_cost_map(base_skus, platform="ml"),
        token_store.get_avg_shipping_cost_map(base_skus, platform="amazon"),
    )
    return {"ml": ml_map, "amazon": amz_map}


def _commission_rate(platform: str, price: float) -> float:
    return _ml_fee(price) if platform == "ml" else _AMZ_FEE


def _ship_cost_for(candidate: dict, ship_cost_maps: dict, base_sku: str) -> float:
    plat_map = (ship_cost_maps or {}).get(candidate["platform"], {}) or {}
    val = plat_map.get(base_sku)
    if val is None:
        val = plat_map.get(base_sku.upper())
    return val if val is not None else _SHIP_FALLBACK_MXN


# ─────────────────────────────────────────────────────────────────────────────
# Núcleo de scoring — función pura, sin I/O
# ─────────────────────────────────────────────────────────────────────────────

def score_candidates(base_sku: str, candidates: list[dict], stock_to_concentrate: int,
                      cost_mxn: float | None, ship_cost_maps: dict | None = None) -> dict:
    """Calcula el score de cada candidato elegible para un SKU escaso. No hace
    I/O ni persiste nada -- ver resolve_winner() para histéresis+persistencia.

    candidates: ya deben venir filtrados a solo los listings updatable (no
    FULL/FLX) -- ver build_candidate().

    Retorna:
    {
      "method": "margin" | "margin_fallback_total" | "velocity_only_no_cost"
                | "no_sales_manual" | "no_sales_manual_no_cost" | "no_candidates",
      "eligible": [...candidatos no excluidos por reputación roja...],
      "excluded_red": [...candidatos excluidos por reputación roja...],
      "all_red_fallback": bool,   # caso patológico: TODAS las cuentas están en rojo
      "scored": [{**candidato, "score", "margen_u", "unidades_esperadas", "ship_cost_mxn"}],
      "top": scored[0] | None,
      "manual_selection": bool,
    }
    """
    ship_cost_maps = ship_cost_maps or {}
    if not candidates:
        return {
            "method": "no_candidates", "eligible": [], "excluded_red": [],
            "all_red_fallback": False, "scored": [], "top": None,
            "manual_selection": True,
        }

    eligible = [c for c in candidates if c["rep_factor"] > _RED_THRESHOLD]
    excluded_red = [c for c in candidates if c["rep_factor"] <= _RED_THRESHOLD]
    all_red_fallback = False
    if not eligible:
        # Caso patológico: TODAS las cuentas con este SKU están en reputación
        # roja. Preferimos igual repartir el stock (mejor que dejarlo sin
        # asignar, lo que provocaría un no-op silencioso) en vez de bloquear
        # el sync entero -- documentado para revisión de Jovan.
        eligible = list(candidates)
        excluded_red = []
        all_red_fallback = True

    has_30d = any(c["sold_30d"] > 0 for c in eligible)
    has_total = any(c["sold_total"] > 0 for c in eligible)

    scored = []
    if cost_mxn is not None:
        if has_30d:
            method = "margin"
        elif has_total:
            method = "margin_fallback_total"
        else:
            method = "no_sales_manual"

        for c in eligible:
            ship_c = _ship_cost_for(c, ship_cost_maps, base_sku)
            comision = c["price"] * _commission_rate(c["platform"], c["price"])
            margen_u = c["price"] - cost_mxn - (comision * (1 + _IVA)) - (ship_c * (1 + _IVA))
            if has_30d:
                unidades = min(stock_to_concentrate, c["sold_30d"])
                score = unidades * margen_u * c["rep_factor"]
            elif has_total:
                unidades = min(stock_to_concentrate, c["sold_total"])
                score = unidades * margen_u * c["rep_factor"]
            else:
                # Sin ventas en ninguna cuenta: mejor prior es margen×reputación,
                # no "mayor stock disponible" (regla explícita de negocio).
                unidades = 0
                score = margen_u * c["rep_factor"]
            scored.append({
                **c, "margen_u": round(margen_u, 2), "unidades_esperadas": unidades,
                "ship_cost_mxn": round(ship_c, 2), "score": score,
            })
    else:
        # Sin costo BM confiable -- fallback a la fórmula vieja de
        # solo-velocidad×reputación (sin margen), SOLO para este SKU.
        method = "velocity_only_no_cost"
        for c in eligible:
            ship_c = _ship_cost_for(c, ship_cost_maps, base_sku)
            comision = c["price"] * _commission_rate(c["platform"], c["price"])
            net_price = max(0.0, c["price"] - comision - ship_c)
            velocity = c["sold_30d"] if has_30d else c["sold_total"]
            score = net_price * max(1.0, velocity) * c["rep_factor"]
            scored.append({
                **c, "margen_u": None, "unidades_esperadas": velocity,
                "ship_cost_mxn": round(ship_c, 2), "score": score,
            })
        if not has_30d and not has_total:
            method = "no_sales_manual_no_cost"

    scored.sort(key=lambda c: c["score"], reverse=True)
    top = scored[0] if scored else None
    manual_selection = method in ("no_sales_manual", "no_sales_manual_no_cost")

    return {
        "method": method,
        "eligible": eligible,
        "excluded_red": excluded_red,
        "all_red_fallback": all_red_fallback,
        "scored": scored,
        "top": top,
        "manual_selection": manual_selection,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Histéresis + persistencia — usar esto (no score_candidates directo) en
# cualquier flujo que vaya a EJECUTAR una concentración real.
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_winner(base_sku: str, candidates: list[dict], stock_to_concentrate: int,
                          cost_mxn: float | None, ship_cost_maps: dict | None = None,
                          persist: bool = True) -> dict:
    """score_candidates() + histéresis contra stock_winner_cache (+
    persistencia si persist=True).

    persist=True SOLO en corridas reales que van a escribir stock en ML/Amazon
    (run_multi_stock_sync, run_single_account_stock_sync, o una concentración
    manual ejecutada con dry_run=False) -- una preview/dry-run NO debe
    sobreescribir el ganador persistido, o cada vista de "¿qué pasaría?"
    correría el riesgo de voltear el ganador sin que nadie ejecutara nada.
    """
    result = score_candidates(base_sku, candidates, stock_to_concentrate, cost_mxn, ship_cost_maps)
    top = result["top"]
    winner = top
    switched_from_previous = False

    persisted = None
    try:
        persisted = await token_store.get_stock_winner_cache_one(base_sku)
    except Exception as e:
        logger.warning(f"[STOCK-WINNER] No se pudo leer ganador persistido de {base_sku}: {e}")

    if persisted and result["scored"]:
        prev_candidate = next(
            (c for c in result["scored"]
             if c["platform"] == persisted.get("winner_platform")
             and c["account_id"] == persisted.get("winner_account_id")),
            None,
        )
        if prev_candidate is not None and top is not None:
            same_account = (top["platform"] == prev_candidate["platform"]
                             and top["account_id"] == prev_candidate["account_id"])
            if not same_account:
                if top["score"] > prev_candidate["score"] * _HYSTERESIS_FACTOR:
                    switched_from_previous = True
                else:
                    # No supera el umbral de histéresis -- mantener al ganador
                    # anterior aunque no sea el máximo puntual de esta corrida.
                    winner = prev_candidate
        elif prev_candidate is not None and top is None:
            winner = prev_candidate
        # Si prev_candidate es None, el ganador anterior ya no es elegible
        # (ej. cayó a reputación roja, o ya no tiene listing en este SKU) --
        # no aplica histéresis, se adopta el nuevo top directo.

    result["winner"] = winner
    result["switched_from_previous"] = switched_from_previous
    result["previous_winner"] = persisted

    if persist and winner is not None:
        try:
            period_used = (
                "30d" if result["method"] == "margin" else
                "total" if result["method"] == "margin_fallback_total" else
                "sin_ventas" if result["manual_selection"] else
                "velocity"
            )
            await token_store.upsert_stock_winner_cache(
                base_sku=base_sku,
                winner_platform=winner["platform"],
                winner_account_id=winner["account_id"],
                winner_nickname=winner.get("nickname", ""),
                score=winner["score"],
                method=result["method"],
                period_used=period_used,
            )
        except Exception as e:
            logger.warning(f"[STOCK-WINNER] No se pudo persistir ganador de {base_sku}: {e}")

    return result
