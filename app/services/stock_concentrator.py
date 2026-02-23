"""
Servicio de Concentración Inteligente de Stock
===============================================
Lógica de negocio para concentrar el stock de un SKU en la cuenta
que tiene más ventas, evitando sobreventa cuando el stock es bajo.

Flujo:
1. Buscar el mismo SKU (base) en todas las cuentas de MeLi
2. Obtener ventas de los últimos 30 días por cuenta
3. Determinar la cuenta "ganadora" (más ventas)
4. En modo dry_run: solo mostrar el plan
5. En modo execute: poner stock=0 en perdedoras, luego asignar total al ganador
6. Registrar todo en stock_concentration_log

Reglas:
- Producto sin ventas en NINGUNA cuenta → no concentrar (producto nuevo)
- Solo 1 cuenta encontrada con el SKU → no es necesario concentrar
- Ganador = cuenta con más ventas en los últimos 30 días
  Fallback: si todos tienen 0 ventas en 30d, usar sold_quantity acumulado de MeLi
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from app.services import token_store
from app.services.meli_client import get_meli_client


# ─── Helpers ────────────────────────────────────────────────────────────────

_BM_SUFFIXES = ("-NEW", "-GRA", "-GRB", "-GRC", "-ICB", "-ICC")


def _normalize_sku(sku: str) -> str:
    """Quita sufijos de condición BM para comparar SKUs base."""
    upper = sku.upper().strip()
    for sfx in _BM_SUFFIXES:
        if upper.endswith(sfx):
            return sku[:-len(sfx)].strip()
    return sku.strip()


# ─── Core: buscar SKU en todas las cuentas ──────────────────────────────────

async def find_sku_across_accounts(base_sku: str) -> dict:
    """Busca el mismo SKU en todas las cuentas de la aplicación.

    Retorna:
    {
      user_id: {
        "nickname": str,
        "items": [item_body, ...],   # items encontrados en MeLi para este SKU
        "error": str | None
      }
    }
    """
    accounts = await token_store.get_all_tokens()

    async def _search_one(account):
        uid = account["user_id"]
        nickname = account.get("nickname") or uid
        try:
            client = await get_meli_client(user_id=uid)
            items = await client.search_all_items_by_sku(base_sku)
            await client.close()
            return uid, {"nickname": nickname, "items": items, "error": None}
        except Exception as e:
            return uid, {"nickname": nickname, "items": [], "error": str(e)}

    results = await asyncio.gather(*[_search_one(a) for a in accounts])
    return dict(results)


# ─── Core: obtener ventas 30d por cuenta ────────────────────────────────────

async def enrich_with_sales(account_data: dict, days: int = 30) -> dict:
    """Para cada cuenta en account_data, obtiene las ventas de los últimos N días.
    Modifica account_data in-place agregando 'units_by_item' y 'sold_30d'.
    """
    now = datetime.utcnow()
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    async def _get_sales_one(uid, data):
        try:
            client = await get_meli_client(user_id=uid)
            orders = await client.fetch_all_orders(date_from=date_from, date_to=date_to)
            await client.close()

            units_by_item: dict[str, int] = {}
            for order in orders:
                if order.get("status") not in ("paid", "delivered"):
                    continue
                for oi in order.get("order_items", []):
                    iid = oi.get("item", {}).get("id", "")
                    if iid:
                        units_by_item[iid] = units_by_item.get(iid, 0) + oi.get("quantity", 0)

            data["units_by_item"] = units_by_item
            data["sold_30d"] = sum(units_by_item.values())
        except Exception as e:
            data["units_by_item"] = {}
            data["sold_30d"] = 0
            data["error_sales"] = str(e)

    await asyncio.gather(*[
        _get_sales_one(uid, data)
        for uid, data in account_data.items()
    ])
    return account_data


# ─── Core: preview (dry run) ────────────────────────────────────────────────

async def preview_concentration(base_sku: str) -> dict:
    """Analiza cómo se concentraría el stock de este SKU. NO ejecuta ningún cambio.

    Retorna:
    {
      "sku": str,
      "action": "concentrar" | "no_concentrar" | "no_items_found" | "single_account",
      "message": str,
      "winner": {user_id, nickname, item_id, title, available_quantity, sold_30d, sold_total},
      "losers": [{...}, ...],
      "total_stock": int,       # stock total entre todas las cuentas (MeLi available_quantity)
      "details": [{...}, ...]   # todos los items encontrados
    }
    """
    # 1. Buscar SKU en todas las cuentas
    account_data = await find_sku_across_accounts(base_sku)

    # 2. Obtener ventas 30d para cada cuenta
    await enrich_with_sales(account_data)

    # 3. Construir lista plana de resultados
    details = []
    for uid, data in account_data.items():
        units_by_item = data.get("units_by_item", {})
        for item in data.get("items", []):
            item_id = item.get("id", "")
            avail = item.get("available_quantity", 0) or 0
            sold_30d = units_by_item.get(item_id, 0)
            sold_total = item.get("sold_quantity", 0) or 0
            details.append({
                "user_id": uid,
                "nickname": data.get("nickname", uid),
                "item_id": item_id,
                "title": item.get("title", ""),
                "status": item.get("status", ""),
                "available_quantity": avail,
                "sold_30d": sold_30d,
                "sold_total": sold_total,
                "has_variations": bool(item.get("variations")),
            })

    if not details:
        return {
            "sku": base_sku,
            "action": "no_items_found",
            "message": f"No se encontró el SKU '{base_sku}' en ninguna cuenta.",
            "winner": None,
            "losers": [],
            "total_stock": 0,
            "details": [],
        }

    if len(details) == 1:
        d = details[0]
        return {
            "sku": base_sku,
            "action": "single_account",
            "message": f"El SKU solo existe en una cuenta ({d['nickname']}). No es necesario concentrar.",
            "winner": d,
            "losers": [],
            "total_stock": d["available_quantity"],
            "details": details,
        }

    # 4. Determinar ganador
    has_30d_sales = any(d["sold_30d"] > 0 for d in details)
    has_any_sales = any(d["sold_total"] > 0 for d in details)

    total_stock = sum(d["available_quantity"] for d in details)

    if has_30d_sales:
        winner = max(details, key=lambda d: (d["sold_30d"], d["sold_total"]))
        period_label = "30 días"
        manual_selection = False
        message = (
            f"GANADOR: {winner['nickname']} ({winner['sold_30d']} ventas últimos 30d / "
            f"{winner['sold_total']} históricas). "
            f"Stock total: {total_stock} unidades → "
            f"{total_stock} al ganador, 0 a {len([d for d in details if d['item_id'] != winner['item_id']])} cuenta(s)."
        )
    elif has_any_sales:
        # Sin ventas recientes pero sí historial — usar acumulado
        winner = max(details, key=lambda d: d["sold_total"])
        period_label = "histórico"
        manual_selection = False
        message = (
            f"GANADOR (histórico): {winner['nickname']} ({winner['sold_total']} ventas acumuladas). "
            f"Stock total: {total_stock} → {total_stock} al ganador."
        )
    else:
        # Sin ventas en ninguna cuenta — sugerir la de mayor stock MeLi, permitir cambio manual
        winner = max(details, key=lambda d: (d["available_quantity"], d["sold_30d"]))
        period_label = "sin_ventas"
        manual_selection = True
        message = (
            f"Sin ventas registradas en ninguna cuenta. "
            f"Se sugiere concentrar en {winner['nickname']} (mayor stock MeLi). "
            f"Puedes seleccionar otra cuenta manualmente."
        )

    losers = [d for d in details if d["item_id"] != winner["item_id"]]
    return {
        "sku": base_sku,
        "action": "concentrar",
        "message": message,
        "winner": winner,
        "losers": losers,
        "total_stock": total_stock,
        "period_used": period_label,
        "manual_selection": manual_selection,
        "details": details,
    }


# ─── Core: execute concentration ────────────────────────────────────────────

async def execute_concentration(
    base_sku: str,
    winner_user_id: str,
    total_stock: int,
    dry_run: bool = True,
    trigger: str = "manual",
) -> dict:
    """Ejecuta la concentración de stock de un SKU.

    Orden de operaciones (seguro para evitar sobreventa):
    1. Poner stock=0 en TODOS los items perdedores (otras cuentas)
    2. Poner stock=total_stock en el item ganador

    Args:
        base_sku: SKU base a concentrar
        winner_user_id: user_id de la cuenta ganadora
        total_stock: cantidad total a asignar al ganador
        dry_run: si True, solo simula sin ejecutar cambios reales
        trigger: razón de la concentración ('manual', 'below_threshold', 'first_sale')
    """
    # Verificar que no se haya concentrado recientemente (solo aplica si no dry_run)
    if not dry_run:
        recent = await token_store.last_concentration_for_sku(base_sku, hours=6)
        if recent:
            return {
                "ok": False,
                "dry_run": dry_run,
                "sku": base_sku,
                "error": f"Ya se concentró este SKU hace menos de 6 horas ({recent['executed_at']}). Espera antes de repetir.",
                "recent_log": recent,
            }

    # Buscar el SKU en todas las cuentas para obtener item_ids actuales
    account_data = await find_sku_across_accounts(base_sku)

    winner_items = []
    loser_actions = []

    for uid, data in account_data.items():
        for item in data.get("items", []):
            item_id = item.get("id", "")
            prev_qty = item.get("available_quantity", 0) or 0
            if uid == winner_user_id:
                winner_items.append({
                    "user_id": uid,
                    "nickname": data.get("nickname", uid),
                    "item_id": item_id,
                    "prev_qty": prev_qty,
                    "new_qty": total_stock,
                })
            else:
                loser_actions.append({
                    "user_id": uid,
                    "nickname": data.get("nickname", uid),
                    "item_id": item_id,
                    "prev_qty": prev_qty,
                    "new_qty": 0,
                })

    if not winner_items:
        return {
            "ok": False,
            "dry_run": dry_run,
            "sku": base_sku,
            "error": f"No se encontró ningún item con ese SKU en la cuenta ganadora (user_id={winner_user_id}).",
        }

    errors = []
    executed_losers = []
    executed_winner = None

    if dry_run:
        # Solo reportar qué haría
        executed_losers = [{"action": "would_zero", **la} for la in loser_actions]
        executed_winner = {"action": "would_assign", **winner_items[0], "new_qty": total_stock}
    else:
        # PASO 1: Poner 0 en todos los perdedores primero (previene sobreventa)
        async def _zero_loser(la):
            try:
                client = await get_meli_client(user_id=la["user_id"])
                await client.update_item_stock(la["item_id"], 0)
                await client.close()
                return {"ok": True, "action": "zeroed", **la}
            except Exception as e:
                return {"ok": False, "action": "zero_failed", "error": str(e), **la}

        loser_results = await asyncio.gather(*[_zero_loser(la) for la in loser_actions])
        executed_losers = list(loser_results)
        errors.extend([r for r in executed_losers if not r.get("ok")])

        # PASO 2: Asignar stock total al ganador
        winner = winner_items[0]
        try:
            client = await get_meli_client(user_id=winner["user_id"])
            await client.update_item_stock(winner["item_id"], total_stock)
            await client.close()
            executed_winner = {"ok": True, "action": "assigned", **winner}
        except Exception as e:
            executed_winner = {"ok": False, "action": "assign_failed", "error": str(e), **winner}
            errors.append(executed_winner)

    # Registrar en log
    accounts_zeroed_log = [
        {
            "user_id": la["user_id"],
            "nickname": la.get("nickname", ""),
            "item_id": la["item_id"],
            "prev_qty": la.get("prev_qty", 0),
        }
        for la in loser_actions
    ]

    winner_info = winner_items[0] if winner_items else {}
    await token_store.log_concentration(
        base_sku=base_sku,
        trigger=trigger,
        winner_user_id=winner_user_id,
        winner_nickname=winner_info.get("nickname", ""),
        winner_item_id=winner_info.get("item_id", ""),
        winner_units_30d=0,  # Se completa mejor desde el preview
        total_bm_avail=total_stock,
        accounts_zeroed=accounts_zeroed_log,
        dry_run=dry_run,
        status="ok" if not errors else "partial_error",
        notes=f"trigger={trigger}, losers={len(loser_actions)}, errors={len(errors)}",
    )

    return {
        "ok": len(errors) == 0,
        "dry_run": dry_run,
        "sku": base_sku,
        "winner": executed_winner,
        "losers": executed_losers,
        "errors": errors,
        "summary": (
            f"{'[DRY RUN] ' if dry_run else ''}"
            f"Ganador: {winner_info.get('nickname', winner_user_id)} → {total_stock} unidades. "
            f"Cuentas puestas a 0: {len(loser_actions)}. "
            f"Errores: {len(errors)}."
        ),
    }


# ─── Scan: detectar SKUs candidatos a concentración ─────────────────────────

async def scan_low_stock_skus(products: list, threshold: int = 5) -> dict:
    """Dado un listado de productos (con _bm_avail), detecta cuáles tienen
    stock BM disponible < threshold y tienen el SKU en múltiples cuentas.

    products: lista de dicts con campos 'sku' y '_bm_avail' (del inventario)
    threshold: máximo de unidades disponibles para activar la revisión

    Retorna:
    {
      "candidates": [
        {
          "sku": str, "bm_avail": int, "found_in": int,
          "preview": {...}  # resultado de preview_concentration
        }
      ],
      "skipped": int,  # productos sin SKU o sin BM data
      "total_scanned": int
    }
    """
    # Filtrar productos con SKU y stock bajo
    low_stock = []
    skipped = 0
    seen_skus = set()

    for p in products:
        sku = p.get("sku", "").strip()
        if not sku:
            skipped += 1
            continue
        bm_avail = p.get("_bm_avail", None)
        if bm_avail is None:
            skipped += 1
            continue
        base_sku = _normalize_sku(sku)
        if base_sku in seen_skus:
            continue
        if bm_avail < threshold:
            low_stock.append({"sku": base_sku, "bm_avail": bm_avail})
            seen_skus.add(base_sku)

    if not low_stock:
        return {
            "candidates": [],
            "skipped": skipped,
            "total_scanned": len(products),
            "message": f"No hay productos con stock BM < {threshold} unidades.",
        }

    # Para cada candidato, ejecutar preview en paralelo (max 5 concurrent)
    sem = asyncio.Semaphore(5)

    async def _preview_one(item):
        async with sem:
            try:
                preview = await preview_concentration(item["sku"])
                return {
                    "sku": item["sku"],
                    "bm_avail": item["bm_avail"],
                    "found_in": len(preview.get("details", [])),
                    "action": preview.get("action"),
                    "preview": preview,
                }
            except Exception as e:
                return {
                    "sku": item["sku"],
                    "bm_avail": item["bm_avail"],
                    "found_in": 0,
                    "action": "error",
                    "error": str(e),
                    "preview": None,
                }

    results = await asyncio.gather(*[_preview_one(item) for item in low_stock])

    # Solo devolver candidatos que realmente necesitan concentración
    candidates = [r for r in results if r.get("action") == "concentrar"]
    no_action = [r for r in results if r.get("action") != "concentrar"]

    return {
        "candidates": candidates,
        "no_action": no_action,
        "skipped": skipped,
        "total_scanned": len(products),
        "threshold": threshold,
        "message": (
            f"Escaneados {len(products)} productos. "
            f"{len(low_stock)} con stock < {threshold}. "
            f"{len(candidates)} requieren concentración."
        ),
    }
