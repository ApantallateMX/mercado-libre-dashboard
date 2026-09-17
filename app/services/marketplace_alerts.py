"""marketplace_alerts.py — Alertas automáticas del dashboard hacia el equipo
responsable de cada cuenta/área, vía Mattermost (#alertas-marketplace).

FEATURE 2026-08-25 (pedido explícito de Jovan): Vianey (área, recibe TODO),
Arely (Amazon Vektor), Vanessa (Mercado Libre, todas las cuentas), Adrian
Espino (Amazon Exclusive USA), Alejandro Torres + Said (salud/reputación).
Piloto #1: solo reputación ML (verde/amarillo/rojo) -- mensajes sin
responder y órdenes con pérdida se agregan después ("poco a poco", palabras
de Jovan), no en la primera versión.

Requiere las variables de entorno MM_BOT_TOKEN / MM_CHANNEL_ID / MM_URL
(bot @ecomops-agent, canal #alertas-marketplace, provisionado por
mattermost-manager-agent el 2026-08-25). Si faltan, las funciones de este
módulo son no-op silenciosas -- nunca deben tumbar el resto de la app.
"""

import os
import math as _math
import logging
import httpx

logger = logging.getLogger(__name__)

MM_URL = os.getenv("MM_URL", "")
MM_BOT_TOKEN = os.getenv("MM_BOT_TOKEN", "")
MM_CHANNEL_ID = os.getenv("MM_CHANNEL_ID", "")

# PAUSADO 2026-08-25 (pedido explícito de Jovan: "espera aún no mandemos
# alertas") -- interruptor explícito, apagado por default. Nadie recibe
# nada hasta que Jovan pida encenderlo con MARKETPLACE_ALERTS_ENABLED=true
# en Railway. El resto del mecanismo (loop, detección de transición,
# endpoint de prueba) sigue corriendo/probable, solo el envío real queda
# bloqueado aquí, en un único punto.
ALERTS_ENABLED = os.getenv("MARKETPLACE_ALERTS_ENABLED", "false").strip().lower() == "true"

AREA_LEAD = "@vianey.ramirez"
HEALTH_TEAM = ["@alejandro.torres", "@said.ramirez"]

# user_id ML / seller_id Amazon -> dueño directo de la cuenta.
# AUTOBOT AMZ MX es de Vianey directamente (ya está en AREA_LEAD, no se
# duplica aquí para no mandarle el mismo aviso dos veces).
ACCOUNT_OWNERS: dict[str, str] = {
    "523916436": "@vanessa.espino",    # APANTALLATEMX (ML)
    "292395685": "@vanessa.espino",    # AUTOBOT (ML)
    "391393176": "@vanessa.espino",    # BLOWTECHNOLOGIES (ML)
    "515061615": "@vanessa.espino",    # LUTEMAMEXICO (ML)
    "A20NFIUQNEYZ1E": "@arely.rodriguez",   # VECKTOR IMPORTS (Amazon)
    "A22XNR713HGDVG": "@adrian.espino",     # ExclusiveBulbs (Amazon, USA)
}

# FIX 2026-08-25 (bug real encontrado al mandar la primera prueba a
# AUTOBOT -- dio "desconocido" en vez de "amarillo"): los valores REALES de
# level_id son 5_green/4_light_green/3_yellow/2_orange/1_red (verificado en
# vivo con /api/diag/marketplace-alert-debug-user, AUTOBOT trae "3_yellow"
# tal cual), NO "4_yellow"/"3_orange" que tenia aqui antes -- claves
# equivocadas, nunca hacian match. Mapeo a los 3 colores que pidio Jovan:
# 4_light_green sigue siendo familia verde; 2_orange se agrupa con amarillo
# (Jovan pidio 3 colores, no 5 niveles).
_LEVEL_TO_COLOR = {
    "5_green": "verde",
    "4_light_green": "verde",
    "3_yellow": "amarillo",
    "2_orange": "amarillo",
    "1_red": "rojo",
}
_COLOR_EMOJI = {"verde": "🟢", "amarillo": "🟡", "rojo": "🔴", "desconocido": "⚪"}


def level_id_to_color(level_id: str) -> str:
    return _LEVEL_TO_COLOR.get(level_id or "", "desconocido")


async def _post_to_mattermost_channel(channel_id: str, text: str, label: str) -> bool:
    """POST puro a un canal de Mattermost -- nunca lanza, solo loguea si
    falla. No-op silencioso si el bot (MM_URL/MM_BOT_TOKEN, compartido) o el
    channel_id puntual no están configurados. `label` es solo para logs."""
    if not (MM_URL and MM_BOT_TOKEN and channel_id):
        logger.info(f"[{label}] MM_* / channel_id no configurado -- mensaje no enviado: %s", text[:120])
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{MM_URL}/api/v4/posts",
                headers={"Authorization": f"Bearer {MM_BOT_TOKEN}"},
                json={"channel_id": channel_id, "message": text},
            )
            if r.status_code not in (200, 201):
                logger.warning(f"[{label}] Mattermost respondió %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:
        logger.warning(f"[{label}] Error posteando a Mattermost: {e}")
        return False


async def post_marketplace_alert(text: str) -> bool:
    """POST puro a Mattermost -- nunca lanza, solo loguea si falla. No-op
    silencioso si el bot no está configurado (env vars ausentes) O si
    ALERTS_ENABLED sigue apagado (pausado a pedido de Jovan 2026-08-25)."""
    if not ALERTS_ENABLED:
        logger.info("[MarketplaceAlerts] PAUSADO (MARKETPLACE_ALERTS_ENABLED != true) -- alerta no enviada: %s", text[:120])
        return False
    return await _post_to_mattermost_channel(MM_CHANNEL_ID, text, "MarketplaceAlerts")


# FEATURE 2026-09-04 (pedido explícito de Jovan): notificación de una
# Requisición de Traspaso nueva (ver transfer_requests en token_store.py) --
# canal DISTINTO al de alertas de reputación (#alertas-marketplace), pedido
# aparte todavía por confirmar. Sin gate de ALERTS_ENABLED -- es una feature
# nueva pedida explícitamente hoy, no la campaña de reputación que Jovan
# pausó el 25-ago; si MM_WAREHOUSE_CHANNEL_ID no está seteado, no-op seguro
# (mismo patrón que el resto de este módulo) hasta que Jovan confirme canal.
MM_WAREHOUSE_CHANNEL_ID = os.getenv("MM_WAREHOUSE_CHANNEL_ID", "")


async def post_warehouse_transfer_request(text: str) -> bool:
    """POST puro al canal de almacén/logística para una Requisición de
    Traspaso nueva. No-op seguro si MM_WAREHOUSE_CHANNEL_ID no está
    configurado todavía -- la requisición igual queda registrada en DB."""
    return await _post_to_mattermost_channel(MM_WAREHOUSE_CHANNEL_ID, text, "WarehouseTransferRequest")


# Umbrales OFICIALES MLM (Mexico) -- verificados en vivo 2026-08-25 contra
# developers.mercadolibre.com.mx/es_ar/manejo-de-ordenes/reputacion-de-vendedores.
# El color de la cuenta = LA PEOR de las 3, nunca un promedio.
METRIC_THRESHOLDS = {
    "reclamos":       {"lideres": 1.0, "verde": 1.5, "amarillo": 3.0, "naranja": 6.0},
    "cancelaciones":  {"lideres": 0.5, "verde": 1.0, "amarillo": 2.5, "naranja": 3.0},
    "demora_manejo":  {"lideres": 8.0, "verde": 10.0, "amarillo": 15.0, "naranja": 22.0},
}
_METRIC_LABEL = {"reclamos": "Reclamos", "cancelaciones": "Cancelaciones", "demora_manejo": "Demora en manejo"}
_STATUS_EMOJI = {"lider": "🏆", "verde": "🟢", "amarillo": "🟡", "naranja": "🟠", "rojo": "🔴"}


def _metric_status(metric_key: str, value_pct: float) -> str:
    t = METRIC_THRESHOLDS[metric_key]
    if value_pct <= t["lideres"]:
        return "lider"
    if value_pct <= t["verde"]:
        return "verde"
    if value_pct <= t["amarillo"]:
        return "amarillo"
    if value_pct <= t["naranja"]:
        return "naranja"
    return "rojo"


# ─────────────────────────────────────────────────────────────────────────
# ESCALONES DE ATENCIÓN — FEATURE 2026-09-15 (pedido de Jovan)
#
# El color oficial de ML solo tiene 2 estados útiles antes del desastre
# (Líder y Verde), y entre ellos cabe TODO el margen de maniobra: para
# reclamos, de 1.0% a 1.5%. Cuando ML por fin cambia el color, ya perdiste.
# Estos 4 escalones parten ese tramo para avisar ANTES.
#
# Jovan los definió sobre reclamos: ≤1% buen trabajo, 1-1.2% atención,
# 1.2-1.5% rojo ("estamos a punto de perder todo"), >1.5% urgencia total.
# El corte intermedio (1.2) cae al 40% del tramo Líder→Verde, así que la
# MISMA proporción se aplica a las otras 2 métricas en su propia escala --
# si no, una cuenta se pondría amarilla por cancelaciones sin que nadie
# hubiera recibido un solo aviso (el color de ML es la PEOR de las 3).
#
#   Reclamos:      1.0 → 1.2 → 1.5      Cancelaciones: 0.5 → 0.7 → 1.0
#   Demora manejo: 8.0 → 8.8 → 10.0
# ─────────────────────────────────────────────────────────────────────────
_TIER_MID_FRACTION = 0.4

# 'sin_dato' NO es un escalón de salud: es "no se pudo consultar esta cuenta".
# Existe para que una cuenta nunca desaparezca del mensaje en silencio (ver
# _run_marketplace_digest). Se ordena junto a 'atencion' -- visible, pero sin
# desplazar a una cuenta que sí tiene una emergencia real confirmada.
TIER_RANK = {"ok": 0, "sin_dato": 1, "atencion": 1, "riesgo": 2, "critico": 3}
TIER_EMOJI = {"ok": "🟢", "atencion": "🟡", "riesgo": "🔴", "critico": "🚨", "sin_dato": "⚪"}
TIER_LABEL = {
    "ok": "buen trabajo",
    "atencion": "poner atención",
    "riesgo": "a punto de perderlo",
    "critico": "ATENDER HOY MISMO",
    "sin_dato": "no se pudo consultar",
}


def metric_tier(metric_key: str, value_pct: float) -> str:
    """Escalón de UNA métrica: ok / atencion / riesgo / critico."""
    t = METRIC_THRESHOLDS[metric_key]
    lider, verde = t["lideres"], t["verde"]
    if value_pct <= lider:
        return "ok"
    if value_pct <= lider + (verde - lider) * _TIER_MID_FRACTION:
        return "atencion"
    if value_pct <= verde:
        return "riesgo"
    return "critico"


def account_tier(metrics: dict) -> tuple[str, str]:
    """Escalón de la CUENTA = el peor de sus 3 métricas (mismo criterio que
    usa ML para el color: nunca un promedio). Retorna (tier, metrica_culpable)."""
    worst_tier, worst_key = "ok", "reclamos"
    for key in ("reclamos", "cancelaciones", "demora_manejo"):
        tier = metric_tier(key, metrics.get(key, 0) or 0)
        if TIER_RANK[tier] > TIER_RANK[worst_tier]:
            worst_tier, worst_key = tier, key
    return worst_tier, worst_key


def extract_metrics(user: dict) -> dict:
    """De seller_reputation.metrics (ya viene de get_user_info) a
    {reclamos, cancelaciones, demora_manejo} en % (0-100), listo para
    comparar contra METRIC_THRESHOLDS."""
    m = (user.get("seller_reputation") or {}).get("metrics") or {}
    return {
        "reclamos": round((m.get("claims") or {}).get("rate", 0) * 100, 2),
        "cancelaciones": round((m.get("cancellations") or {}).get("rate", 0) * 100, 2),
        "demora_manejo": round((m.get("delayed_handling_time") or {}).get("rate", 0) * 100, 2),
    }


def extract_metric_counts(user: dict) -> dict:
    """Conteos crudos (no tasas) de seller_reputation.metrics. `value` es el
    NÚMERO de reclamos/cancelaciones/demoras del período, no el total de
    ventas -- confundirlos fue un bug real corregido el 2026-08-28 (ver
    _margin_count en app/main.py)."""
    m = (user.get("seller_reputation") or {}).get("metrics") or {}
    return {
        "reclamos": int((m.get("claims") or {}).get("value", 0) or 0),
        "cancelaciones": int((m.get("cancellations") or {}).get("value", 0) or 0),
        "demora_manejo": int((m.get("delayed_handling_time") or {}).get("value", 0) or 0),
    }


def claims_headroom(metric_key: str, rate_pct: float, count: int) -> dict:
    """Traduce el % a un número accionable. Devuelve
    {"total": ventas_del_periodo, "excluir": N, "margen": N}.

    - `excluir`: cuántos hay que sacar de la cuenta para volver al límite
      verde (>0 solo si la cuenta ya lo cruzó).
    - `margen`: cuántos más aguanta antes de cruzarlo (>0 solo si todavía
      está por debajo).

    El total de ventas del período no viene directo en la respuesta de ML,
    pero `rate = value/total` con ambos del MISMO período, así que
    `total = value/rate` se deriva sin otra llamada a la API (mismo criterio
    ya usado por _margin_count en app/main.py desde el 2026-08-28).

    Es una foto al volumen de ventas ACTUAL: el denominador se mueve solo
    conforme entran ventas nuevas, así que el número baja aunque nadie toque
    un reclamo. Por eso se presenta como referencia de hoy, no como promesa."""
    limite = METRIC_THRESHOLDS[metric_key]["verde"] / 100.0
    rate = (rate_pct or 0) / 100.0
    if rate <= 0 or count <= 0:
        return {"total": 0, "excluir": 0, "margen": 0}
    total = count / rate
    if rate > limite:
        # cuántos sobran por encima del límite (redondeado hacia arriba: con
        # la parte fraccionaria todavía se sigue estando por encima)
        return {"total": int(round(total)), "excluir": int(_math.ceil(count - limite * total)), "margen": 0}
    return {"total": int(round(total)), "excluir": 0, "margen": int(_math.floor(limite * total) - count)}


def build_metrics_table(metrics: dict) -> str:
    """Tabla markdown con las 3 metricas reales vs umbrales oficiales MLM --
    deja claro CUAL metrica especifica esta empujando el color (nunca es un
    promedio, ver METRIC_THRESHOLDS)."""
    rows = ["| Métrica | Actual | Meta Líder | Límite Verde | Estado |", "|---|---|---|---|---|"]
    worst_key, worst_rank = None, -1
    _rank = {"lider": 0, "verde": 1, "amarillo": 2, "naranja": 3, "rojo": 4}
    for key in ("reclamos", "cancelaciones", "demora_manejo"):
        val = metrics.get(key, 0)
        status = _metric_status(key, val)
        if _rank[status] > worst_rank:
            worst_rank, worst_key = _rank[status], key
        t = METRIC_THRESHOLDS[key]
        rows.append(
            f"| {_METRIC_LABEL[key]} | {val}% | ≤{t['lideres']}% | ≤{t['verde']}% | "
            f"{_STATUS_EMOJI[status]} {status.capitalize()} |"
        )
    table = "\n".join(rows)
    if worst_key:
        table += f"\n\n_El color de la cuenta lo define **{_METRIC_LABEL[worst_key]}** (la métrica en peor estado — el color nunca es un promedio)._"
    return table


async def classify_open_claims(client, max_claims: int = 15) -> tuple[list, dict]:
    """Trae los reclamos abiertos y los clasifica contra la regla oficial de
    exclusión de ML (1 sola llamada de IA para todos). Retorna
    (claims, verdicts). Extraído 2026-09-15 de build_actionable_claims_summary
    para que el digest programado reuse la MISMA clasificación en vez de
    duplicar la llamada -- nunca lanza, si algo falla regresa lo que pudo."""
    from datetime import datetime, timezone
    from app.main import _claim_reason_label
    from app.services import openrouter_client
    from app.services.health_ai import build_claims_batch_exclusion_prompt, parse_claims_batch_exclusion

    data = await client.get_claims(status="opened", limit=max_claims)
    raw_claims = data.get("results", []) or []
    if not raw_claims:
        return [], {}

    now = datetime.now(timezone.utc)
    claims = []
    for c in raw_claims:
        cid = str(c.get("id", ""))
        if not cid:
            continue
        days_open = 0
        try:
            dt_obj = datetime.fromisoformat((c.get("date_created", "") or "").replace("Z", "+00:00"))
            days_open = max(0, (now - dt_obj).days)
        except Exception:
            pass
        claims.append({
            "id": cid,
            "reason_desc": _claim_reason_label(c.get("reason_id", "")),
            "days_open": days_open,
        })

    verdicts = {}
    if claims and openrouter_client.is_available():
        try:
            system, prompt, max_tokens = build_claims_batch_exclusion_prompt(claims)
            raw = await openrouter_client.generate(prompt, system=system, max_tokens=max_tokens)
            verdicts = parse_claims_batch_exclusion(raw, [c["id"] for c in claims])
        except Exception as e:
            logger.warning(f"[MarketplaceAlerts] Error clasificando reclamos: {e}")
    return claims, verdicts


async def build_claims_digest_line(client, max_claims: int = 15, total_open: int = 0) -> str:
    """Versión de UNA línea del análisis de exclusión, para el digest de la
    mañana (el mensaje largo con la lista completa sigue siendo
    build_actionable_claims_summary, que usa la alerta por cambio de color).

    `total_open` es el conteo REAL de reclamos abiertos de la cuenta, que
    puede ser mayor que los que alcanza a revisar la IA (max_claims). Se usa
    para decirlo explícito -- con datos reales salió "34 abiertos / 0 de 15",
    que leído en frío parece un error de cuentas."""
    try:
        claims, verdicts = await classify_open_claims(client, max_claims=max_claims)
    except Exception as e:
        return f"• _No se pudo revisar los reclamos ({e})._"
    if not claims:
        return "• Sin reclamos abiertos 🎉"
    n = len(claims)
    ambito = f"los {n} más recientes" if total_open > n else f"{n}"
    excludable = sum(1 for c in claims if verdicts.get(c["id"], {}).get("exclusion_eligible") == "si")
    if excludable:
        return (f"• {excludable} de {ambito} podrían pedir exclusión "
                f"→ revisar en Métricas → Atención a tus compradores")
    return f"• 0 de {ambito} califican para exclusión → hay que resolverlos con el comprador"


async def build_actionable_claims_summary(client, target_pct: float = 1.5, max_claims: int = 15) -> str:
    """FEATURE 2026-08-25 (pedido de Jovan: "que reclamos podria atender para
    tenerla al 100%"). Trae reclamos abiertos, los clasifica en 1 sola
    llamada de IA contra la regla OFICIAL completa (ver
    health_ai.build_claims_batch_exclusion_prompt) y arma un resumen
    accionable: cuantos son excluibles + cuantos hacen falta resolver para
    volver al umbral objetivo. Nunca lanza -- si algo falla, regresa texto
    explicando que no se pudo generar, para que la alerta principal (cambio
    de color) siga saliendo igual."""
    try:
        claims, verdicts = await classify_open_claims(client, max_claims=max_claims)
    except Exception as e:
        return f"_No se pudo traer los reclamos abiertos ({e})._"

    if not claims:
        return "Sin reclamos abiertos ahora mismo. 🎉"

    excludable = [c for c in claims if verdicts.get(c["id"], {}).get("exclusion_eligible") == "si"]
    manual = [c for c in claims if verdicts.get(c["id"], {}).get("exclusion_eligible") == "revisar_manualmente"]

    lines = [f"**{len(claims)} reclamo(s) abierto(s)**"]
    if excludable:
        lines.append(f"\n✅ **{len(excludable)} podrían pedir exclusión** (revisar y solicitar en Métricas → Atención a tus compradores):")
        for c in excludable[:8]:
            reason = verdicts.get(c["id"], {}).get("exclusion_reason", "")
            lines.append(f"  • #{c['id']} — {c['reason_desc']} ({c['days_open']}d) — _{reason}_")
    if manual:
        lines.append(f"\n⚠️ {len(manual)} necesitan revisión manual (info insuficiente para que la IA decida sola).")
    not_excludable = len(claims) - len(excludable) - len(manual)
    if not_excludable > 0:
        lines.append(f"\n❌ {not_excludable} no califican para exclusión según la regla oficial de ML.")
    if excludable:
        lines.append(f"\n👉 Si se excluyen esos {len(excludable)}, la tasa de reclamos baja de inmediato.")
    return "\n".join(lines)


# FIX 2026-08-25 (pedido de Jovan: "quiero un poco mas bonito y que me lo
# muestres aqui antes de mandar, no mandemos a lo loco"): separado en 2
# pasos -- build_health_alert_message() SOLO arma el texto (nunca llama a
# Mattermost, se puede llamar cuantas veces se quiera para preview), y
# notify_reputation_change() es la unica que de verdad envia (a traves de
# post_marketplace_alert, que ya respeta ALERTS_ENABLED).
async def build_health_alert_message(account_id: str, nickname: str, old_color: str, new_color: str,
                                      metrics: dict | None = None, client=None) -> str:
    owner = ACCOUNT_OWNERS.get(account_id)
    mentions = [AREA_LEAD] + HEALTH_TEAM
    if owner and owner not in mentions:
        mentions.append(owner)
    emoji_new = _COLOR_EMOJI.get(new_color, "⚪")

    if old_color == new_color:
        title = f"### {emoji_new} {nickname} — Reputación (chequeo puntual, sin cambio de color)"
    else:
        emoji_old = _COLOR_EMOJI.get(old_color, "⚪")
        title = f"### {emoji_new} {nickname} — Reputación cambió: {emoji_old} {old_color} → {emoji_new} **{new_color}**"

    parts = [title]
    if metrics:
        parts.append(build_metrics_table(metrics))
    dashboard_url = os.getenv("DASHBOARD_BASE_URL", "https://apantallatemx.up.railway.app")
    parts.append(f"[Ver en el dashboard]({dashboard_url}/health) — pestaña Salud, cuenta {nickname}.")

    if client is not None:
        try:
            claims_summary = await build_actionable_claims_summary(client)
            parts.append("---\n📋 " + claims_summary)
        except Exception as e:
            logger.warning(f"[MarketplaceAlerts] No se pudo agregar resumen de reclamos: {e}")

    parts.append(" ".join(mentions))
    return "\n\n".join(parts)


async def notify_reputation_change(account_id: str, nickname: str, old_color: str, new_color: str,
                                    metrics: dict | None = None, client=None) -> bool:
    text = await build_health_alert_message(account_id, nickname, old_color, new_color, metrics=metrics, client=client)
    return await post_marketplace_alert(text)


# ═════════════════════════════════════════════════════════════════════════
# DIGEST PROGRAMADO — 5:00 AM y 3:00 PM (CDMX), FEATURE 2026-09-15
#
# Un SOLO mensaje con todas las cuentas ML, no uno por cuenta (4 cuentas x 2
# corridas serían 8 posts diarios -- la gente deja de leerlos).
#
#   5:00 AM  "con qué te vas a encontrar hoy"  -> estado + reclamos accionables
#   3:00 PM  "qué se movió hoy"                -> diferencia contra la mañana
#
# El % de ML se mueve con ventana de 60 días: en un día casi nunca cambia.
# Por eso el de la tarde NO mide el trabajo del día por el porcentaje (diría
# "sin cambio" siempre y parecería que nadie hizo nada) sino por los
# reclamos: cuántos entraron, cuántos se cerraron, cuántos siguen parados.
# ═════════════════════════════════════════════════════════════════════════
_DIGEST_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_DIGEST_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun",
                  "jul", "ago", "sep", "oct", "nov", "dic")


async def aviso_corrida_sin_respuesta(channel_name: str = "alertas-marketplace") -> str:
    """¿La última corrida publicada tuvo respuesta de un HUMANO en su hilo?

    Nace de un caso real (2026-09-17): salieron 3 corridas seguidas sin una sola
    respuesta y nadie lo notó hasta que Jovan lo preguntó. Convertir el silencio
    en un dato del propio mensaje evita depender de que alguien se dé cuenta.

    Cuenta como acuse de recibo CUALQUIERA de estas dos, de una persona:
      - una respuesta escrita en el hilo, o
      - una REACCIÓN (👍, ✅, lo que sea) sobre la corrida o sobre cualquier
        mensaje de su hilo.

    Lo segundo lo pidió Jovan explícitamente (2026-09-17): "a veces las
    respuestas pueden ser con un emoji o un like". Decirle "nadie respondió" a
    alguien que sí reaccionó es peor que no avisar nada -- acusa en falso y
    quema la credibilidad de la alerta.

    Un post o una reacción del propio bot NO cuentan. Nunca lanza: si Mattermost
    no responde, devuelve "" y el digest sale igual."""
    try:
        from app.services.mattermost_bot import get_channel_posts, get_my_user_id
        posts = await get_channel_posts(channel_name, limit=40)
        if not posts:
            return ""
        yo = await get_my_user_id()
        raiz = next((p for p in posts
                     if not p.get("root_id")
                     and p.get("user_id") == yo
                     and ("Salud de cuentas ML" in (p.get("message") or "")
                          or "Cierre del día" in (p.get("message") or ""))), None)
        if not raiz:
            return ""

        # El hilo completo: la corrida + todo lo que cuelga de ella.
        hilo = [raiz] + [p for p in posts if p.get("root_id") == raiz.get("id")]

        # a) respuesta escrita de una persona
        if any(p.get("user_id") != yo for p in hilo if p is not raiz):
            return ""
        # b) reacción de una persona sobre cualquier mensaje del hilo
        for p in hilo:
            for reac in (p.get("reactions") or []):
                if reac.get("user_id") and reac.get("user_id") != yo:
                    return ""

        return ("La corrida anterior quedó sin respuesta de nadie. "
                "Con un \"visto\" o una reacción en el hilo basta para saber "
                "que esto se está leyendo.")
    except Exception as e:
        logger.warning(f"[MarketplaceAlerts] No se pudo revisar respuestas del hilo: {e}")
        return ""


def digest_mentions(account_ids: list[str]) -> str:
    """Área + equipo de salud + dueños de las cuentas incluidas, sin repetir.
    Jovan confirmó 2026-09-15 que van los 4 (Vianey, Alejandro, Said y
    Vanessa como dueña de las cuentas ML)."""
    mentions = [AREA_LEAD] + list(HEALTH_TEAM)
    for aid in account_ids:
        owner = ACCOUNT_OWNERS.get(str(aid))
        if owner and owner not in mentions:
            mentions.append(owner)
    return " ".join(mentions)


def _digest_date_label(dt) -> str:
    return f"{_DIGEST_DAYS[dt.weekday()]} {dt.day}/{_DIGEST_MONTHS[dt.month - 1]}"


def _fmt_delta(now_val: float, prev_val, ref_label: str) -> str:
    """Texto del cambio contra una corrida previa. Vacío si no hay con qué
    comparar (primer día, o la corrida anterior no se registró)."""
    if prev_val is None:
        return ""
    d = round(float(now_val) - float(prev_val), 2)
    if abs(d) < 0.005:
        return f" · igual que {ref_label}"
    return f" · {'↑' if d > 0 else '↓'}{abs(d):.2f} desde {ref_label}"


def _sorted_worst_first(accounts: list[dict]) -> list[dict]:
    """Orden del digest. Cambiado 2026-09-17 por un caso real: ordenar solo por
    gravedad ponía arriba a AUTOBOT (🚨 ya caída, $1.2M/mes) y debajo a
    BLOWTECHNOLOGIES (🔴 a 6 reclamos de caer, $4.5M/mes). El mensaje era
    correcto y aun así dirigía la atención a la cuenta equivocada.

    Ahora, entre las cuentas que necesitan atención, manda el DINERO EN RIESGO.
    Las sanas siempre van al final."""
    def clave(a):
        tier = a.get("tier", "ok")
        rev = ((a.get("revenue") or {}).get("diario") or 0)
        if tier == "ok":
            return (2, 0, 0)               # sanas al final
        if tier == "sin_dato":
            return (1, 0, 0)               # no consultadas, antes de las sanas
        return (0, -rev, -TIER_RANK.get(tier, 0))
    return sorted(accounts, key=clave)


def _es_prevenible(tier: str) -> bool:
    """'critico' = ya cruzó el límite de ML: es recuperación. 'riesgo'/'atencion'
    = todavía no cruza: es prevención, y prevenir cuesta menos."""
    return tier in ("riesgo", "atencion")


def _mxn(v) -> str:
    return f"${v:,.0f}"


def racha_sin_movimiento(historial: list[dict], tier_actual: str, abiertos_actual: int) -> int:
    """Cuántas corridas consecutivas lleva la cuenta en el mismo escalón SIN que
    bajen los reclamos abiertos. `historial` viene de get_recent_digest_runs
    (más reciente primero) y NO incluye la corrida en curso.

    Devuelve 0 si no hay racha que reportar. Solo cuenta si el escalón es el
    mismo: una cuenta que empeoró no está "estancada", está cayendo."""
    if tier_actual in ("ok", "sin_dato"):
        return 0
    n = 1
    for h in historial:
        if h.get("tier") != tier_actual:
            break
        if int(h.get("open_claims") or 0) < abiertos_actual:
            break          # los reclamos SÍ bajaron en algún momento
        n += 1
    return n if n >= 2 else 0


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def build_morning_digest(accounts: list[dict], now_mx, prev_run: dict | None = None,
                         sin_respuesta: str = "") -> str:
    """Corrida de las 5 AM. `accounts`: dicts con account_id, nickname,
    metrics, open_claims, revenue, racha y (opcional) claims_summary.
    `prev_run`: fila 'pm' del día anterior, para el comparativo.
    `sin_respuesta`: aviso si la corrida anterior no tuvo respuesta humana."""
    prev_run = prev_run or {}
    parts = [f"## ☀️ Salud de cuentas ML — {_digest_date_label(now_mx)}, 5:00 AM"]

    # Encabezado de dinero: lo EVITABLE primero, porque prevenir cuesta menos
    # que recuperar. Sin esto, una cuenta chica ya caída tapaba a una grande
    # que todavía se podía salvar (caso real 2026-09-17).
    _evit = [a for a in accounts if _es_prevenible(a.get("tier", "ok"))
             and (a.get("revenue") or {}).get("diario")]
    if _evit:
        _top = max(_evit, key=lambda a: a["revenue"]["diario"])
        parts.append(f"⚠️ **Lo más urgente por dinero: {_top['nickname']}** — vende "
                     f"**{_mxn(_top['revenue']['diario'])}/día** y **todavía no cae**. "
                     f"Es lo único de esta lista que aún se puede evitar.")
    if sin_respuesta:
        parts.append(f"🔇 _{sin_respuesta}_")

    for a in _sorted_worst_first(accounts):
        tier = a.get("tier", "ok")
        if tier == "sin_dato":
            parts.append(f"{TIER_EMOJI['sin_dato']} **{a['nickname']}** — {TIER_LABEL['sin_dato']}\n"
                         f"   _{a.get('error', 'error desconocido')}_ — revisar a mano.")
            continue
        m = a.get("metrics") or {}
        reclamos = m.get("reclamos", 0)
        prev = prev_run.get(str(a.get("account_id", "")))
        delta = _fmt_delta(reclamos, (prev or {}).get("claims_rate"), "ayer")
        emoji, label = TIER_EMOJI[tier], TIER_LABEL[tier]
        limite = METRIC_THRESHOLDS["reclamos"]["verde"]

        if tier == "ok":
            parts.append(
                f"{emoji} **{a['nickname']}** — {label}\n"
                f"   Reclamos {reclamos}%{delta} · {a.get('open_claims', 0)} abiertos"
            )
            continue

        block = [f"{emoji} **{a['nickname']}** — {label}"]
        # Traducción del % a un número que el equipo puede accionar hoy.
        # OJO: resolver un reclamo NO lo saca de la métrica -- ML cuenta los
        # reclamos abiertos en la ventana. Lo que sí baja la tasa es que ML
        # los EXCLUYA (regla oficial) o que salgan de la ventana de 60 días.
        # Decir "resuelve N para volver a verde" sería falso.
        hr = a.get("headroom") or {}
        rev = a.get("revenue") or {}
        # Lo que está en juego, en pesos. Un % no mueve a nadie; "$144,289 al
        # día a 6 reclamos del borde" sí.
        if rev.get("diario"):
            etiqueta = ("🛡️ EVITABLE — todavía no cae" if _es_prevenible(tier)
                        else "💸 YA CAYÓ — esto se está pagando")
            block.append(f"   {etiqueta} · vende **{_mxn(rev['diario'])}/día** "
                         f"({_mxn(rev.get('total', 0))} en {rev.get('dias', 0)} días)")
        if tier == "critico":
            block.append(f"   Reclamos **{reclamos}%** (límite verde {limite}%){delta}")
            block.append("   La cuenta YA está en amarillo con ML. Cada día así")
            block.append("   cuesta exposición y ventas.")
        elif tier == "riesgo":
            faltan = round(limite - reclamos, 2)
            block.append(f"   Reclamos **{reclamos}%** (te faltan {faltan} pts para caer){delta}")
            block.append("   Estamos a un pelo de perder el verde por mala atención.")
        else:
            meta = METRIC_THRESHOLDS["reclamos"]["lideres"]
            block.append(f"   Reclamos **{reclamos}%** (meta Líder ≤{meta}%){delta}")
            block.append("   Ya perdimos el umbral de MercadoLíder — todavía se recupera.")
        block.append(f"   • {_plural(a.get('open_claims', 0), 'reclamo abierto', 'reclamos abiertos')}")
        if a.get("claims_summary"):
            block.append(f"   {a['claims_summary']}")
        # El número accionable va AL FINAL, después del análisis de exclusión:
        # puesto antes se contradecía con él ("excluir 4" seguido de "0
        # califican para exclusión" se lee como error). Aquí se presenta como
        # el TAMAÑO DE LA BRECHA, que es compatible con que hoy ninguno
        # califique -- en ese caso la vía real es que salgan de la ventana.
        if tier == "critico" and hr.get("excluir"):
            block.append(f"   👉 La brecha son **{_plural(hr['excluir'], 'reclamo', 'reclamos')}**: "
                         f"esa cantidad tiene que salir de la cuenta (por exclusión aprobada "
                         f"o al cumplir 60 días) para volver a verde.")
        elif tier in ("riesgo", "atencion") and hr.get("total"):
            block.append(f"   👉 Con el volumen de ventas de hoy, **aguanta "
                         f"{_plural(hr.get('margen', 0), 'reclamo más', 'reclamos más')}** "
                         f"antes de cruzar a amarillo.")
        # El silencio como dato: si lleva varias corridas igual, que lo diga el
        # mensaje en vez de depender de que alguien lo note.
        racha = a.get("racha") or 0
        if racha >= 2:
            block.append(f"   ⏳ **{racha}ª corrida sin movimiento** — mismos reclamos, mismo escalón.")
        parts.append("\n".join(block))

    dashboard_url = os.getenv("DASHBOARD_BASE_URL", "https://apantallatemx.up.railway.app")
    parts.append(f"[Ver detalle en el dashboard]({dashboard_url}/health)")
    parts.append(digest_mentions([a.get("account_id", "") for a in accounts]))
    return "\n\n".join(parts)


def build_afternoon_digest(accounts: list[dict], now_mx, am_run: dict | None = None) -> str:
    """Corrida de las 3 PM -- el cierre del día. Compara contra la corrida de
    las 5 AM del MISMO día: qué se movió, qué sigue parado."""
    am_run = am_run or {}
    header = f"## 🔔 Cierre del día — {_digest_date_label(now_mx)}, 3:00 PM"
    sub = "Contra cómo amanecimos a las 5:00 AM:" if am_run else \
          "_(no hubo corrida de la mañana hoy, no hay con qué comparar)_"
    parts = [f"{header}\n{sub}"]

    tot_nuevos = tot_cerrados = tot_abiertos = 0
    sin_conteo = []
    for a in _sorted_worst_first(accounts):
        tier = a.get("tier", "ok")
        if tier == "sin_dato":
            parts.append(f"{TIER_EMOJI['sin_dato']} **{a['nickname']}** — {TIER_LABEL['sin_dato']}\n"
                         f"   _{a.get('error', 'error desconocido')}_ — revisar a mano.")
            continue
        m = a.get("metrics") or {}
        reclamos = m.get("reclamos", 0)
        prev = am_run.get(str(a.get("account_id", "")))
        delta = _fmt_delta(reclamos, (prev or {}).get("claims_rate"), "la mañana")
        abiertos = a.get("open_claims", 0)
        tot_abiertos += abiertos
        emoji = TIER_EMOJI[tier]

        # claims_today None = no se pudo contar (no es lo mismo que "0 hoy").
        # Se dice explícito en vez de reportar ceros que se leerían como
        # "no hicieron nada en todo el día", que sería acusar en falso.
        hoy = a.get("claims_today")
        if hoy is None:
            sin_conteo.append(a["nickname"])
            parts.append(f"{emoji} **{a['nickname']}** — Reclamos {reclamos}%{delta}\n"
                         f"   {abiertos} abiertos · _no se pudo calcular el movimiento de hoy_")
            continue
        nuevos, cerrados = hoy.get("nuevos", 0), hoy.get("cerrados", 0)
        tot_nuevos += nuevos
        tot_cerrados += cerrados

        if tier == "ok" and nuevos == 0 and cerrados == 0:
            parts.append(f"{emoji} **{a['nickname']}** — {reclamos}%{delta} · {abiertos} abiertos, sin movimiento")
            continue

        block = [f"{emoji} **{a['nickname']}** — Reclamos {reclamos}%{delta}",
                 f"   Hoy: {_plural(nuevos, 'nuevo', 'nuevos')} · "
                 f"{_plural(cerrados, 'cerrado', 'cerrados')} · "
                 f"{_plural(abiertos, 'abierto', 'abiertos')}"]
        # Lectura del día, en función de lo que de verdad controlan
        # El juicio sobre el día SOLO se emite si hubo corrida de la mañana con
        # qué comparar. Sin esa base no sabemos si se movieron o no, y decir
        # "llevan todo el día sin moverse" sería acusar en falso al equipo --
        # pasaría literalmente el primer día, cuando closed_date todavía está
        # vacío por diseño (se sella por observación hacia adelante).
        if not am_run:
            pass
        elif tier in ("critico", "riesgo") and cerrados == 0 and abiertos > 0:
            block.append(f"   ⚠️ {'El mismo reclamo lleva' if abiertos == 1 else f'Los mismos {abiertos} reclamos llevan'} todo el día sin moverse.")
        elif cerrados > nuevos:
            block.append(f"   👏 Bajaron el pendiente en {cerrados - nuevos} hoy.")
        elif cerrados and cerrados == nuevos:
            block.append("   Se mantuvieron parejos, pero no bajaron.")
        elif nuevos > cerrados:
            block.append(f"   ⚠️ Entraron {nuevos - cerrados} más de los que se cerraron.")
        parts.append("\n".join(block))

    total_line = (f"📋 Del día: {_plural(tot_nuevos, 'nuevo', 'nuevos')} · "
                  f"{_plural(tot_cerrados, 'cerrado', 'cerrados')} · "
                  f"{tot_abiertos} siguen abiertos")
    if sin_conteo:
        total_line += f"\n_(sin contar {', '.join(sin_conteo)} — no se pudo leer su movimiento de hoy)_"
    parts.append(total_line)
    parts.append(digest_mentions([a.get("account_id", "") for a in accounts]))
    return "\n\n".join(parts)
