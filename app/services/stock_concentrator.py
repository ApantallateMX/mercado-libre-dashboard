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

    # 3. Construir lista plana de resultados, agrupando por cuenta
    #    Si una cuenta tiene varios items del mismo SKU, elegir el mejor (más ventas)
    #    como representante de esa cuenta; los demás se zeroan también.
    all_items_flat = []
    for uid, data in account_data.items():
        units_by_item = data.get("units_by_item", {})
        for item in data.get("items", []):
            item_id = item.get("id", "")
            avail = item.get("available_quantity", 0) or 0
            sold_30d = units_by_item.get(item_id, 0)
            sold_total = item.get("sold_quantity", 0) or 0
            logistic = item.get("shipping", {}).get("logistic_type", "")
            is_full = logistic == "fulfillment"
            all_items_flat.append({
                "user_id": uid,
                "nickname": data.get("nickname", uid),
                "item_id": item_id,
                "title": item.get("title", ""),
                "status": item.get("status", ""),
                "available_quantity": avail,
                "sold_30d": sold_30d,
                "sold_total": sold_total,
                "has_variations": bool(item.get("variations")),
                "is_full": is_full,
            })

    if not all_items_flat:
        return {
            "sku": base_sku,
            "action": "no_items_found",
            "message": f"No se encontró el SKU '{base_sku}' en ninguna cuenta.",
            "winner": None,
            "losers": [],
            "total_stock": 0,
            "details": [],
        }

    # Agrupar por cuenta: elegir el mejor representante (mayor sold_30d, luego sold_total)
    # Los items secundarios de una misma cuenta se zeroan igual que los perdedores
    best_per_account: dict = {}  # uid -> mejor item
    secondary_items: list = []   # items duplicados de la misma cuenta

    for item in all_items_flat:
        uid = item["user_id"]
        if uid not in best_per_account:
            best_per_account[uid] = item
        else:
            current = best_per_account[uid]
            # El nuevo item es mejor si tiene más ventas 30d, o históricas, o más stock
            if (item["sold_30d"], item["sold_total"], item["available_quantity"]) > \
               (current["sold_30d"], current["sold_total"], current["available_quantity"]):
                secondary_items.append(current)
                best_per_account[uid] = item
            else:
                secondary_items.append(item)

    # details = un item representativo por cuenta
    details = list(best_per_account.values())

    # Cuentas únicas con publicaciones
    unique_accounts = len(details)

    if unique_accounts == 1 and not secondary_items:
        d = details[0]
        return {
            "sku": base_sku,
            "action": "single_account",
            "message": f"El SKU solo existe en una cuenta ({d['nickname']}). No es necesario concentrar.",
            "winner": d,
            "losers": [],
            "total_stock": d["available_quantity"],
            "details": details,
            "secondary_items": [],
        }

    # 4. Determinar ganador considerando FULL
    # Regla: si hay items FULL activos, el ganador para stock BM es el mejor MERCHANT.
    # Los FULL permanecen activos sin cambio de stock (su inventario es del fulfillment center).
    full_items = [d for d in details if d["is_full"]]
    merchant_items = [d for d in details if not d["is_full"]]

    has_full = bool(full_items)
    # Para la selección de ganador, solo usar Merchant si hay alguno disponible
    candidate_pool = merchant_items if merchant_items else details

    has_30d_sales = any(d["sold_30d"] > 0 for d in candidate_pool)
    has_any_sales = any(d["sold_total"] > 0 for d in candidate_pool)

    total_meli_stock = sum(d["available_quantity"] for d in details)

    if has_30d_sales:
        winner = max(candidate_pool, key=lambda d: (d["sold_30d"], d["sold_total"]))
        period_label = "30 días"
        manual_selection = False
    elif has_any_sales:
        winner = max(candidate_pool, key=lambda d: d["sold_total"])
        period_label = "histórico"
        manual_selection = False
    else:
        winner = max(candidate_pool, key=lambda d: (d["available_quantity"], d["sold_30d"]))
        period_label = "sin_ventas"
        manual_selection = True

    # Los perdedores son: todos los items que NO son el ganador
    # FULL items: se listan aparte, no se zeroan (su stock lo gestiona MeLi)
    # Merchant items no ganadores: se zeroan
    loser_merchants = [d for d in merchant_items if d["item_id"] != winner["item_id"]]
    # Items secundarios de la misma cuenta: también se zeroan
    all_to_zero = loser_merchants + secondary_items

    # Construir mensaje
    full_note = f" · {len(full_items)} FULL (sin cambio de stock)" if has_full else ""
    if period_label == "30 días":
        message = (
            f"GANADOR: {winner['nickname']} · {winner['item_id']} "
            f"({winner['sold_30d']} v/30d · {winner['sold_total']} hist.)"
            f"{full_note}"
        )
    elif period_label == "histórico":
        message = (
            f"GANADOR (histórico): {winner['nickname']} · {winner['item_id']} "
            f"({winner['sold_total']} ventas acumuladas)"
            f"{full_note}"
        )
    else:
        message = (
            f"Sin ventas registradas. Sugerencia: {winner['nickname']} · {winner['item_id']} "
            f"(mayor stock MeLi). Puedes cambiar manualmente."
            f"{full_note}"
        )

    return {
        "sku": base_sku,
        "action": "concentrar",
        "message": message,
        "winner": winner,
        "losers": all_to_zero,       # items que recibirán stock=0
        "full_items": full_items,     # items FULL que NO se modifican
        "total_stock": total_meli_stock,
        "period_used": period_label,
        "manual_selection": manual_selection,
        "has_full": has_full,
        "details": details,          # un representante por cuenta
        "secondary_items": secondary_items,  # publicaciones extra del mismo SKU/cuenta
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

    # Obtener preview actualizado para saber qué es FULL, qué es el ganador real
    # y cuáles son los items secundarios de la misma cuenta
    preview = await preview_concentration(base_sku)

    # Reusar el winner_item_id del preview si coincide con el winner_user_id solicitado
    # Si el usuario cambió manualmente el ganador, buscamos el item de esa cuenta
    preview_winner = preview.get("winner") or {}
    all_details = preview.get("details", [])
    secondary = preview.get("secondary_items", [])
    full_items_list = preview.get("full_items", [])

    # Encontrar el item ganador: el representante de la cuenta winner_user_id
    winner_item = next(
        (d for d in all_details if d["user_id"] == winner_user_id),
        None
    )

    if not winner_item:
        return {
            "ok": False,
            "dry_run": dry_run,
            "sku": base_sku,
            "error": f"No se encontró ningún item con ese SKU en la cuenta ganadora (user_id={winner_user_id}).",
        }

    # FULL items: NO se zeroan (su stock lo gestiona MeLi Fulfillment)
    full_item_ids = {f["item_id"] for f in full_items_list}

    # Items a zerear: todos los representantes de otras cuentas (excepto FULL) + secundarios
    loser_actions = []
    for d in all_details:
        if d["user_id"] == winner_user_id:
            continue  # es la cuenta ganadora, no zerear su representante
        if d["item_id"] in full_item_ids:
            continue  # FULL: no se toca
        loser_actions.append({
            "user_id": d["user_id"],
            "nickname": d.get("nickname", d["user_id"]),
            "item_id": d["item_id"],
            "prev_qty": d["available_quantity"],
            "new_qty": 0,
        })

    # Items secundarios de la cuenta ganadora: también zerear
    for s in secondary:
        if s["item_id"] != winner_item["item_id"] and s["item_id"] not in full_item_ids:
            loser_actions.append({
                "user_id": s["user_id"],
                "nickname": s.get("nickname", s["user_id"]),
                "item_id": s["item_id"],
                "prev_qty": s["available_quantity"],
                "new_qty": 0,
            })

    winner_entry = {
        "user_id": winner_user_id,
        "nickname": winner_item.get("nickname", winner_user_id),
        "item_id": winner_item["item_id"],
        "prev_qty": winner_item["available_quantity"],
        "new_qty": total_stock,
    }

    if not winner_entry["item_id"]:
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
        executed_losers = [{"action": "would_zero", **la} for la in loser_actions]
        executed_winner = {"action": "would_assign", **winner_entry}
    else:
        # PASO 1: Poner 0 en todos los perdedores (previene sobreventa)
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
        try:
            client = await get_meli_client(user_id=winner_entry["user_id"])
            await client.update_item_stock(winner_entry["item_id"], total_stock)
            await client.close()
            executed_winner = {"ok": True, "action": "assigned", **winner_entry}
        except Exception as e:
            executed_winner = {"ok": False, "action": "assign_failed", "error": str(e), **winner_entry}
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

    await token_store.log_concentration(
        base_sku=base_sku,
        trigger=trigger,
        winner_user_id=winner_user_id,
        winner_nickname=winner_entry.get("nickname", ""),
        winner_item_id=winner_entry.get("item_id", ""),
        winner_units_30d=winner_item.get("sold_30d", 0),
        total_bm_avail=total_stock,
        accounts_zeroed=accounts_zeroed_log,
        dry_run=dry_run,
        status="ok" if not errors else "partial_error",
        notes=f"trigger={trigger}, losers={len(loser_actions)}, errors={len(errors)}, full_skipped={len(full_items_list)}",
    )

    zeroed_nicknames = list({la["nickname"] for la in loser_actions})
    return {
        "ok": len(errors) == 0,
        "dry_run": dry_run,
        "sku": base_sku,
        "winner": executed_winner,
        "winner_item_id": winner_entry["item_id"],
        "winner_nickname": winner_entry.get("nickname", winner_user_id),
        "losers": executed_losers,
        "zeroed_accounts": zeroed_nicknames,
        "full_skipped": [f["item_id"] for f in full_items_list],
        "errors": errors,
        "summary": (
            f"{'[DRY RUN] ' if dry_run else ''}"
            f"Ganador: {winner_entry.get('nickname', winner_user_id)} ({winner_entry['item_id']}) → {total_stock} u. "
            f"Puestos a 0: {len(loser_actions)} items. "
            f"FULL no tocados: {len(full_items_list)}. "
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
