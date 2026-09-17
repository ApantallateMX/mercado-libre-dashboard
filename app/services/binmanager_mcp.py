"""binmanager_mcp.py — stock vendible calculado desde el MCP de BinManager.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ EXISTE (2026-09-17)

El camino viejo (`get_bulk_stock` -> `Get_GlobalStock_InventoryBySKU` contra la
app web) está devolviendo `AvailableQTY = 0` en el 96% de sus filas. Casos
verificados EN VIVO CONTRA BM por Jovan el 2026-09-17:

    SKU           vendible real   lo que guardábamos
    SNTV007756         22              avail=0  (total=22)
    SNTV007863         21              avail=0  (total=20)
    SNTV008058          6              avail=0  (total=5)
    SNTV007630          2              avail=0  (total=2)

O sea: TVs en bins de PRODUCTO TERMINADO, listos para vender, publicados en
cero. No es sobreventa -- es venta perdida, y explica el reporte de Ivana en
#requerimientos-dashboard.

Este módulo NO arregla el camino viejo: calcula el vendible desde cero con los
datos crudos del MCP, que sí dan el número correcto.

═══════════════════════════════════════════════════════════════════════════
CÓMO SE CALCULA, Y POR QUÉ ASÍ

El MCP NO tiene una tool que devuelva "vendible" ya resuelto -- `inventory_by_sku`
cuenta bins de TRÁNSITO como disponibles (SNSB000022: 930 unidades condición NEW
en bins TRANSITO que el almacén no puede surtir). Ver
project_bm_mcp_not_vendible_filtered.md.

Hay DOS motivos independientes por los que algo no es vendible, y hay que
filtrar por los dos:

    1. El TIPO DE BIN donde está parado  -> filter_bins da BinID -> BinTypeName
    2. La CONDICIÓN del producto          -> el sufijo del ProductSKU

Filtrar solo por condición NO basta: las 930 de SNSB000022 son condición NEW
(vendible) dentro de bins TRANSITO. Ese fue el hallazgo que descartó migrar a
`inventory_by_sku` tal cual.

Receta:
    filter_bins(locationId)              -> mapa BinID -> BinTypeName   (3 llamadas)
    inventory_changed_since(locationId)  -> BinContent crudo con BinID  (~35 llamadas)
    join + filtro de bin + filtro de condición + (Qty - QtyReserved)

~662,000 filas del almacén completo en ~38 llamadas / 2.5 min, contra los
cientos que hace hoy el camino viejo paginando de 500 en 500 contra la app web.
═══════════════════════════════════════════════════════════════════════════
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_MCP_URL = os.getenv("BM_MCP_URL", "https://binmanager.mitechnologiesinc.com/mcp")
_BM_USER = os.getenv("BM_USER", "Claude.Jovan@mitechnologiesinc.com")
_BM_PASS = os.getenv("BM_PASS", "")

# Apagado por default. Mientras esté en false este módulo NO corre solo --
# únicamente cuando alguien llama al endpoint de comparación a propósito.
MCP_ENABLED = os.getenv("BM_MCP_ENABLED", "false").strip().lower() == "true"

# GOTCHA REAL (costó un 403 que parecía de credenciales): Cloudflare bloquea
# el User-Agent por default de las librerías HTTP con "error 1010 - Access
# denied ... based on your browser's signature". No es auth. Mandar UA propio.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": "ecomops-dashboard/1.0",
}

# ── Throttle propio del MCP ────────────────────────────────────────────────
# El MCP documenta su límite en concepts/mcp-throttle: 90 tools/call por
# usuario por ventana fija de 60s, 8 slots "light" concurrentes. Corre en el
# MISMO app pool y la MISMA SQL Server que el tráfico real de planta, así que
# pasarse no es "nos regañan", es competirle CPU al picking.
#
# Nos quedamos MUY por debajo a propósito (Jovan ya nos bloquearon una vez por
# volumen): 1 request a la vez + pausa entre requests. Un ciclo completo son
# ~38 llamadas, o sea ~8s de pausas -- irrelevante contra los 144s que tarda
# la transferencia real.
_MCP_SEM: Optional[asyncio.Semaphore] = None
_MCP_DELAY = 0.2
_MCP_MAX_CALLS_PER_CYCLE = 200   # tope duro: si se pasa, algo está en bucle


def _get_sem() -> asyncio.Semaphore:
    global _MCP_SEM
    if _MCP_SEM is None:
        _MCP_SEM = asyncio.Semaphore(1)
    return _MCP_SEM


# ── Reglas de vendibilidad ─────────────────────────────────────────────────
# TIPOS DE BIN que cuentan como inventario real.
#
# Origen: BM.BinTypes tiene una columna `isInventory` que el `list_bin_types`
# del MCP NO expone -- Jovan la sacó directo de la tabla el 2026-09-17. Los 7
# con isInventory=1 son: PRODUCTO TERMINADO, PRODUCTO INCOMPLETO, Finished
# Good, WAREHOSE, Accesorios WIP, Accesorios FG, BTSFBA01.
#
# ECOMMERCE y Released estuvieron aquí un rato: venían de una lista que
# armé por ingeniería inversa y se conservaron al principio por la regla de
# complementar en vez de reemplazar (feedback_complementar_no_eliminar).
# SALIERON el mismo día con evidencia dura, contrastando contra BinManager en
# vivo (filtro guardado "BOUGHTS Teles Celestica Monterrey CDMX Apantallate"):
#
#   SNTV007889 -> BM dice 434. PRODUCTO TERMINADO GRB 429 + ICB 4 + GRC 1 =
#                 434 exacto. La unidad que sobraba era un bin `Released`.
#   SNTV007867 -> BM dice 265. PRODUCTO TERMINADO NEW = 265 exacto. De las 3
#                 que sobraban, 2 eran bins `Released`.
#
# Con `isInventory` sola: 4 de 5 SKUs exactos y +1 unidad sobre 2,058 (0.05%).
# Con ECOMMERCE/Released: 3 de 5 y +4 (0.2%). La bandera de la tabla manda.
# (De ECOMMERCE no hay caso directo -- ninguno de los 5 tenía stock ahí -- pero
# tampoco trae isInventory=1, así que se trata igual.)
#
# FALTA 1 UNIDAD por explicar: un GRB en PRODUCTO TERMINADO de SNTV007867 que
# BM no cuenta y nosotros sí. El filtro guardado de BM aplica 7 criterios que
# todavía no hemos leído; el OKF además menciona que "la versión del SKU puede
# mandar sobre la condición si existe regla por versión". Cerrar esto ANTES de
# escribir a ML/Amazon -- de más es sobreventa.
#
# Quedan FUERA a propósito, y cada uno por una razón distinta:
#   TRANSITO           -> está aquí pero apartado para transferir a otro almacén
#   PRODUCTO EN PROCESO-> en reparación/refurbish, no terminado
#   Proceso Entrada/Salida, PENDING, NO CLASIFICADO -> en flujo, sin destino firme
#   DEFECTUOSO, RECYCLE, MISSING -> no vendible
#   Wholesale          -> apartado para B2B, no para venta en línea
#   FBA / FULL         -> ya está en bodega de Amazon/ML, no en la nuestra
_BINTYPES_VENDIBLES = frozenset({
    "PRODUCTO TERMINADO", "PRODUCTO INCOMPLETO", "Finished Good", "WAREHOSE",
    "Accesorios WIP", "Accesorios FG", "BTSFBA01",
})

# CONDICIONES vendibles en línea. NO se amplía sin aprobación de Jovan.
# El OKF de BinManager dice que ICB/ICC son vendibles online para TODA
# categoría; Jovan lo revisó el 2026-09-17 y confirmó que nosotros los
# aceptamos SOLO en TVs ("estamos en lo correcto nosotros solo aplicamos para
# tvs"). Ver project_bm_icb_icc_solo_tvs_confirmado.md -- si una sesión futura
# lee el OKF y quiere "corregir" esto, la respuesta es no.
_COND_VENDIBLES = frozenset({"GRA", "GRB", "GRC", "NEW"})
_COND_VENDIBLES_TV = frozenset({"ICB", "ICC"})

# LOCACIONES. Tijuana (45,69,43,42) EXCLUIDA del vendible desde 2026-08-05:
# solo CDMX y MTY venden en línea, TJ únicamente reabastece vía transferencias.
# Se consulta aparte para el desglose de Transferencias Sugeridas.
_LOC_MTY = (68,)
_LOC_CDMX = (47, 62)
_LOC_TJ = (45, 69, 43, 42)
_LOCS_VENDIBLES = _LOC_MTY + _LOC_CDMX

_PAGE = 20000


def _es_vendible(base_sku: str, condition: str, bin_type: str | None) -> bool:
    if bin_type not in _BINTYPES_VENDIBLES:
        return False
    if condition in _COND_VENDIBLES:
        return True
    return base_sku.startswith("SNTV") and condition in _COND_VENDIBLES_TV


class McpIncompleto(Exception):
    """El MCP contestó, pero con menos datos de los que él mismo reporta.

    Existe por el incidente del 2026-08-21: una sesión de BM colgada devolvía
    HTTP 200 con respuestas vacías y zereamos ~2,590 SKUs reales. Un dato
    parcial escrito como si fuera completo es PEOR que no escribir nada --
    published_qty=0 en un SKU con stock es venta perdida directa. Ante
    cualquier duda, este módulo lanza y el ciclo se aborta sin escribir.
    """


async def _rpc(client: httpx.AsyncClient, method: str, params: dict) -> dict:
    async with _get_sem():
        r = await client.post(_MCP_URL, headers=_HEADERS,
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        await asyncio.sleep(_MCP_DELAY)
    body = r.text
    # El MCP puede contestar JSON plano o SSE (event: message / data: {...}).
    if body.lstrip().startswith("event:"):
        datos = [ln[6:] for ln in body.splitlines() if ln.startswith("data: ")]
        if not datos:
            raise McpIncompleto(f"respuesta SSE sin data (HTTP {r.status_code})")
        body = datos[-1]
    try:
        return json.loads(body)
    except Exception as e:
        raise McpIncompleto(f"respuesta no-JSON (HTTP {r.status_code}): {body[:200]!r}") from e


async def _tool(client: httpx.AsyncClient, name: str, args: dict) -> tuple[list, str]:
    """Llama una tool. Devuelve (filas, encabezado).

    El encabezado es el texto que el MCP pone ANTES del JSON, del estilo
    "646395 fila(s) (mostrando 20000 de 646395; ...)" -- es la única forma de
    saber si nos truncó, así que se devuelve y se usa, no se tira.
    """
    d = await _rpc(client, "tools/call", {"name": name, "arguments": args})
    if "error" in d:
        raise McpIncompleto(f"{name}: {json.dumps(d['error'], ensure_ascii=False)[:250]}")
    try:
        texto = d["result"]["content"][0]["text"]
    except Exception as e:
        raise McpIncompleto(f"{name}: respuesta sin content: {json.dumps(d)[:200]}") from e
    i = texto.find("[")
    if i < 0:
        return [], texto.strip()
    try:
        return json.loads(texto[i:]), texto[:i].strip()
    except Exception as e:
        raise McpIncompleto(f"{name}: JSON inválido en content: {texto[:200]!r}") from e


async def _mapa_tipos_de_bin(client: httpx.AsyncClient, locs) -> dict[int, str]:
    """BinID -> BinTypeName para las locaciones dadas."""
    mapa: dict[int, str] = {}
    for loc in locs:
        filas, _ = await _tool(client, "filter_bins", {"locationId": loc})
        for b in filas:
            bid = b.get("BinID")
            if bid is not None:
                mapa[bid] = b.get("BinTypeName")
    if not mapa:
        raise McpIncompleto("filter_bins devolvió 0 bins -- sin el mapa de tipos "
                            "no se puede distinguir vendible de tránsito")
    return mapa


async def _contenido_crudo(client: httpx.AsyncClient, loc: int) -> list[dict]:
    """Todas las filas de BinContent de una locación, paginadas por cursor.

    GOTCHA que ya nos mordió una vez: `inventory_changed_since` mete una fila
    de Section='Watermark' DENTRO del límite, así que una página llena
    devuelve 19,999 filas de datos, no 20,000. Cortar con `len < limit` se
    dispara en la PRIMERA página y trae 20k de 646k sin ninguna señal de
    error. Por eso el corte es "una página vino sin filas", y además al final
    se compara contra el total que el propio MCP reporta.
    """
    filas: list[dict] = []
    cursor = 0
    reportado: Optional[int] = None
    llamadas = 0
    while True:
        lote, encabezado = await _tool(client, "inventory_changed_since", {
            "locationId": loc, "limit": _PAGE, "sinceBinContentId": cursor,
        })
        llamadas += 1
        if llamadas > _MCP_MAX_CALLS_PER_CYCLE:
            raise McpIncompleto(f"loc {loc}: {llamadas} páginas -- el cursor no avanza, se aborta")
        if reportado is None:
            reportado = _total_reportado(encabezado)
        datos = [r for r in lote if r.get("Section") == "Row"]
        if not datos:
            break
        filas.extend(datos)
        nuevo_cursor = max(r["BinContentID"] for r in datos)
        if nuevo_cursor <= cursor:
            raise McpIncompleto(f"loc {loc}: el cursor no avanzó ({cursor}) -- se aborta para no ciclar")
        cursor = nuevo_cursor

    # Verificación anti-truncado.
    #
    # El total que reporta el MCP INCLUYE la fila de Watermark, así que las
    # filas de datos esperadas son reportado-1 (verificado: loc 47 dice 15734
    # y trae 15733; loc 45 dice 21 y trae 20).
    #
    # La tolerancia es absoluta Y porcentual a propósito. Solo porcentual
    # rompía en locaciones chicas (20 de 21 = 95.2% y es un pull completo);
    # solo absoluta no serviría en las grandes. El inventario además se mueve
    # mientras paginamos, así que la cuenta puede quedar arriba o abajo por
    # unas cuantas. Lo que sí tiene que atrapar es el truncado de verdad:
    # 20,000 filas de 646,395 (el bug que tuvo mi primer script).
    esperado = (reportado - 1) if reportado else None
    if esperado and esperado > 0:
        margen = max(20, esperado * 0.05)
        if len(filas) < esperado - margen:
            raise McpIncompleto(
                f"loc {loc}: traídas {len(filas)} filas de {esperado} esperadas "
                f"({len(filas) / esperado:.1%}) -- datos incompletos, no se escribe nada")
    return filas


def _total_reportado(encabezado: str) -> Optional[int]:
    """Saca el número de '646395 fila(s) (mostrando 20000 de 646395; ...)'."""
    try:
        return int(encabezado.split()[0].replace(",", ""))
    except Exception:
        return None


async def construir_mapa_vendible() -> dict:
    """Calcula el stock vendible por SKU base desde el MCP.

    Devuelve {"skus": {base_sku: {...}}, "meta": {...}}. Lanza McpIncompleto
    si los datos no vienen completos -- NUNCA devuelve un mapa parcial, porque
    el caller no tendría cómo distinguirlo de "de verdad hay menos stock".
    """
    t0 = time.time()
    if not _BM_PASS:
        raise McpIncompleto("BM_PASS no está en el entorno")

    headers = {**_HEADERS, "X-BinManager-User": _BM_USER, "X-BinManager-Pass": _BM_PASS}
    async with httpx.AsyncClient(timeout=300, headers=headers) as client:
        await _rpc(client, "initialize", {
            "protocolVersion": "2026-11-05", "capabilities": {},
            "clientInfo": {"name": "ecomops-dashboard", "version": "1.0"},
        })
        tipos = await _mapa_tipos_de_bin(client, _LOCS_VENDIBLES + _LOC_TJ)

        por_loc: dict[int, list[dict]] = {}
        for loc in _LOCS_VENDIBLES + _LOC_TJ:
            por_loc[loc] = await _contenido_crudo(client, loc)

    skus: dict[str, dict] = {}
    # best_condition: la condición con MÁS stock vendible. Sin esto, las
    # sugerencias de sustituto muestran un SKU sin sufijo que en el almacén
    # nadie puede tomar (ver FEATURE 2026-08-17 en main.py).
    por_condicion: dict[str, dict[str, int]] = {}

    for loc, filas in por_loc.items():
        es_tj = loc in _LOC_TJ
        for r in filas:
            base = (r.get("SKUPrefix") or "").upper().strip()
            cond = (r.get("SKUCondition") or "").upper().strip()
            if not base:
                continue
            if not _es_vendible(base, cond, tipos.get(r.get("BinID"))):
                continue
            qty = int(r.get("Qty") or 0)
            res = int(r.get("QtyReserved") or 0)
            disp = max(qty - res, 0)
            if qty <= 0:
                continue
            e = skus.setdefault(base, {
                "available_qty": 0, "reserve_qty": 0, "total_qty": 0,
                "mty_qty": 0, "cdmx_qty": 0, "tj_qty": 0,
                "best_condition_sku": "", "best_condition_qty": 0,
            })
            if es_tj:
                # Tijuana NO suma al vendible -- solo se guarda para el
                # desglose y las Transferencias Sugeridas.
                e["tj_qty"] += disp
                continue
            e["available_qty"] += disp
            e["reserve_qty"] += res
            e["total_qty"] += qty
            if loc in _LOC_MTY:
                e["mty_qty"] += disp
            else:
                e["cdmx_qty"] += disp
            c = por_condicion.setdefault(base, {})
            c[cond] = c.get(cond, 0) + disp

    for base, conds in por_condicion.items():
        if not conds:
            continue
        mejor = max(conds.items(), key=lambda kv: kv[1])
        if mejor[1] > 0:
            skus[base]["best_condition_sku"] = f"{base}-{mejor[0]}"
            skus[base]["best_condition_qty"] = mejor[1]

    meta = {
        "skus_con_stock": len(skus),
        "filas_leidas": sum(len(v) for v in por_loc.values()),
        "bins_mapeados": len(tipos),
        "segundos": round(time.time() - t0, 1),
        "generado_ts": time.time(),
    }
    logger.info(f"[BM-MCP] mapa vendible: {meta}")
    return {"skus": skus, "meta": meta}
