"""alertas_agent.py — responde preguntas del equipo en #alertas-marketplace.

FEATURE 2026-09-17 (pedido de Jovan). Antes de esto, el bot solo decía
"enterado" y la respuesta real dependía de que hubiera una sesión de Claude
abierta con Jovan preguntando. Si él no estaba, el equipo recibía un acuse y
nada más -- que es peor que el silencio, porque promete algo que no llega.

═══════════════════════════════════════════════════════════════════════════
REGLA DE DISEÑO, Y ES LA QUE HACE QUE ESTO SEA SEGURO:

    El ruteo es DETERMINISTA (palabras clave + regex) y los NÚMEROS salen
    SIEMPRE de SQL. La IA no participa en producir cifras.

Por qué: en una sola jornada (2026-09-17) hubo tres casos donde un dato mal
medido habría causado daño real -- un +11% de tipo de cambio que era +1.05%,
un deploy reportado como atorado que ya estaba hecho, y 2,856 órdenes que casi
se dan por duplicadas. Los tres se atraparon verificando. Un agente que redacta
números con un modelo va a equivocarse con total seguridad en la voz, y el
equipo va a actuar sobre eso.

Si la pregunta no cae en un intent con fuente exacta, NO se inventa: se dice
que lo está viendo una persona. Preferimos no responder a responder mal.
═══════════════════════════════════════════════════════════════════════════
"""

import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# user_id ML -> nombre visible. Mismas cuentas que el resto del módulo.
CUENTAS = {
    "523916436": "APANTALLATEMX",
    "292395685": "AUTOBOT MEXICO",
    "391393176": "BLOWTECHNOLOGIES",
    "515061615": "LUTEMAMEXICO",
}
# cómo las nombra la gente en el chat
_ALIAS = {
    "apantallate": "523916436", "apantallatemx": "523916436",
    "autobot": "292395685",
    "blow": "391393176", "blowtechnologies": "391393176", "blowtech": "391393176",
    "lutema": "515061615", "lutemamexico": "515061615",
}

_RX_SKU = re.compile(r"\b(S[A-Z]{3}\d{6})\b", re.IGNORECASE)


def _cuenta_en(texto: str) -> str | None:
    t = texto.lower()
    for alias, uid in _ALIAS.items():
        if alias in t:
            return uid
    return None


def _mxn(v) -> str:
    return f"${v:,.2f}"


# Frescura: un dato viejo dicho con seguridad es peor que no decirlo. El agente
# SIEMPRE dice de cuándo es el dato, y avisa fuerte si ya está rancio.
_STOCK_RANCIO_H = 24        # el maestro se refresca por el loop de categorías
_REPUTACION_RANCIA_D = 2    # reputation_snapshots escribe 1 vez al día


def _antiguedad_horas(ts) -> float | None:
    try:
        return (datetime.now().timestamp() - float(ts)) / 3600.0
    except Exception:
        return None


def _sello(horas: float | None, limite_h: float) -> str:
    """Etiqueta de frescura para pegar al final de un dato."""
    if horas is None:
        return " ⚠️ _(no sé de cuándo es este dato)_"
    if horas > limite_h:
        d = horas / 24.0
        return (f" ⚠️ **dato de hace {d:.0f} día(s)** — puede estar desactualizado, "
                f"confírmalo antes de decidir con él")
    return f" _(actualizado hace {horas:.0f}h)_"


# ── Intents ────────────────────────────────────────────────────────────────
# Cada uno: (nombre, regex que lo dispara). El orden importa: el primero que
# haga match gana, así que van de más específico a más general.
_INTENTS = [
    ("stock",      re.compile(r"\b(stock|inventario|existencia|cu[aá]nt[ao]s?\s+(hay|tenemos|quedan))\b", re.I)),
    ("reclamos",   re.compile(r"\b(reclamo|reclamos|claims?)\b", re.I)),
    ("ventas",     re.compile(r"\b(vent[ao]s?|vendi[óo]|factur[óo]|ingres[oa]s?)\b", re.I)),
    ("reputacion", re.compile(r"\b(reputaci[oó]n|salud|amarill[oa]|verde|m[eé]trica)\b", re.I)),
]


def detectar_intent(texto: str) -> str | None:
    for nombre, rx in _INTENTS:
        if rx.search(texto):
            return nombre
    return None


# ── Handlers: cada uno consulta datos REALES y devuelve texto, o None ──────
async def _resp_stock(texto: str) -> str | None:
    from app.services import token_store
    skus = [s.upper() for s in _RX_SKU.findall(texto)]
    if not skus:
        return None                      # sin SKU no hay nada exacto que responder
    filas = await token_store.get_bm_master_rows_for_skus(skus[:5])
    if not filas:
        return (f"No encontré `{skus[0]}` en el maestro de BinManager. "
                f"Puede ser que el SKU esté mal escrito o que no exista en el catálogo.")
    out = ["Stock según el maestro de BinManager:", ""]
    for sku in skus[:5]:
        r = filas.get(sku)
        if not r:
            out.append(f"• `{sku}` — no está en el maestro")
            continue
        disp = r.get("available_qty")
        tot = r.get("total_qty")
        h = _antiguedad_horas(r.get("stock_updated_at"))
        out.append(f"• `{sku}` — **{disp} disponibles** (total {tot}){_sello(h, _STOCK_RANCIO_H)}")
    out.append("")
    out.append("_Sale del maestro que alimenta el loop de categorías, no de una consulta nueva a BinManager._")
    return "\n".join(out)


async def _resp_reclamos(texto: str) -> str | None:
    from app.services import token_store
    uid = _cuenta_en(texto)
    if not uid:
        return None                      # sin cuenta no sé de qué hablar
    import aiosqlite
    from app.config import DATABASE_PATH
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        ab = (await (await db.execute(
            "SELECT COUNT(*) FROM claims_history WHERE account_id=? AND status='opened'", (uid,))).fetchone())[0]
        d60 = (await (await db.execute(
            "SELECT COUNT(*) FROM claims_history WHERE account_id=? AND date_created>=date('now','-60 day')",
            (uid,))).fetchone())[0]
        cur = await db.execute("""
            SELECT sku, COUNT(*) n FROM claims_history
            WHERE account_id=? AND date_created>=date('now','-60 day') AND sku!=''
            GROUP BY 1 ORDER BY n DESC LIMIT 3""", (uid,))
        top = await cur.fetchall()
    out = [f"**{CUENTAS.get(uid, uid)}** — reclamos:", "",
           f"• **{ab} abiertos** ahora mismo",
           f"• **{d60}** en los últimos 60 días (que es la ventana que mide ML)"]
    if top:
        out.append("")
        out.append("SKUs con más reclamos en ese periodo:")
        for sku, n in top:
            out.append(f"• `{sku}` — {n}")
    return "\n".join(out)


async def _resp_ventas(texto: str) -> str | None:
    from app.services import token_store
    uid = _cuenta_en(texto)
    if not uid:
        return None
    rev = await token_store.get_account_daily_revenue(uid, days=30)
    if not rev.get("dias"):
        return None
    return (f"**{CUENTAS.get(uid, uid)}** — ventas de los últimos 30 días:\n\n"
            f"• **{_mxn(rev['total'])}** en total\n"
            f"• **{rev['ordenes']} órdenes**\n"
            f"• promedio de **{_mxn(rev['diario'])} al día**\n\n"
            f"_Sale de `order_history`, con órdenes pagadas y entregadas._")


async def _resp_reputacion(texto: str) -> str | None:
    from app.services import marketplace_alerts as _ma
    import aiosqlite
    from app.config import DATABASE_PATH
    uid = _cuenta_en(texto)
    if not uid:
        return None
    async with aiosqlite.connect(DATABASE_PATH, timeout=15) as db:
        cur = await db.execute("""
            SELECT claims_rate, cancel_rate, delay_rate, captured_date
            FROM reputation_snapshots WHERE account_id=? ORDER BY captured_date DESC LIMIT 1""", (uid,))
        r = await cur.fetchone()
    if not r:
        return None
    m = {"reclamos": round((r[0] or 0) * 100, 2),
         "cancelaciones": round((r[1] or 0) * 100, 2),
         "demora_manejo": round((r[2] or 0) * 100, 2)}
    tier, peor = _ma.account_tier(m)
    lim = _ma.METRIC_THRESHOLDS["reclamos"]["verde"]
    aviso = ""
    try:
        dias = (datetime.now().date() - datetime.strptime(r[3], "%Y-%m-%d").date()).days
        aviso = (f"\n\n⚠️ **Esta foto es del {r[3]}, hace {dias} días** — la reputación cambia "
                 f"a diario, confírmala en el dashboard antes de decidir con ella."
                 if dias > _REPUTACION_RANCIA_D else f"\n\n_Foto del {r[3]}._")
    except Exception:
        aviso = f"\n\n_Foto del {r[3]}._"
    return (f"**{CUENTAS.get(uid, uid)}** — {_ma.TIER_EMOJI.get(tier,'')} {_ma.TIER_LABEL.get(tier,'')}\n\n"
            f"• Reclamos **{m['reclamos']}%** (límite verde {lim}%)\n"
            f"• Cancelaciones {m['cancelaciones']}%\n"
            f"• Demora en manejo {m['demora_manejo']}%\n\n"
            f"La métrica que define el color es **{peor}**.{aviso}")


_HANDLERS = {"stock": _resp_stock, "reclamos": _resp_reclamos,
             "ventas": _resp_ventas, "reputacion": _resp_reputacion}

ESCALA_A_HUMANO = (
    "👀 Enterado — esta la está viendo una persona y te responde aquí mismo.\n\n"
    "_Puedo contestarte al instante si me preguntas por: stock de un SKU, "
    "reclamos de una cuenta, ventas de una cuenta, o su reputación._"
)


async def responder(texto: str) -> tuple[str, bool]:
    """Devuelve (respuesta, la_resolvio_el_agente).

    Si no hay un intent con fuente exacta, devuelve el acuse honesto en vez de
    inventar. Nunca lanza: ante cualquier error, escala a humano."""
    try:
        intent = detectar_intent(texto or "")
        if not intent:
            return ESCALA_A_HUMANO, False
        handler = _HANDLERS.get(intent)
        if not handler:
            return ESCALA_A_HUMANO, False
        r = await handler(texto)
        if not r:
            return ESCALA_A_HUMANO, False
        logger.info(f"[ALERTAS-AGENT] Respondido por intent '{intent}'")
        return r, True
    except Exception as e:
        logger.warning(f"[ALERTAS-AGENT] Error resolviendo, escalo a humano: {e}")
        return ESCALA_A_HUMANO, False
