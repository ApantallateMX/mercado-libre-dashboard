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


async def build_actionable_claims_summary(client, target_pct: float = 1.5, max_claims: int = 15) -> str:
    """FEATURE 2026-08-25 (pedido de Jovan: "que reclamos podria atender para
    tenerla al 100%"). Trae reclamos abiertos, los clasifica en 1 sola
    llamada de IA contra la regla OFICIAL completa (ver
    health_ai.build_claims_batch_exclusion_prompt) y arma un resumen
    accionable: cuantos son excluibles + cuantos hacen falta resolver para
    volver al umbral objetivo. Nunca lanza -- si algo falla, regresa texto
    explicando que no se pudo generar, para que la alerta principal (cambio
    de color) siga saliendo igual."""
    from datetime import datetime, timezone
    from app.main import _claim_reason_label
    from app.services import openrouter_client
    from app.services.health_ai import build_claims_batch_exclusion_prompt, parse_claims_batch_exclusion

    try:
        data = await client.get_claims(status="opened", limit=max_claims)
        raw_claims = data.get("results", []) or []
    except Exception as e:
        return f"_No se pudo traer los reclamos abiertos ({e})._"

    if not raw_claims:
        return "Sin reclamos abiertos ahora mismo. 🎉"

    now = datetime.now(timezone.utc)
    claims = []
    for c in raw_claims:
        cid = str(c.get("id", ""))
        if not cid:
            continue
        dc = c.get("date_created", "")
        days_open = 0
        try:
            dt_obj = datetime.fromisoformat(dc.replace("Z", "+00:00"))
            days_open = max(0, (now - dt_obj).days)
        except Exception:
            pass
        claims.append({
            "id": cid,
            "reason_desc": _claim_reason_label(c.get("reason_id", "")),
            "days_open": days_open,
        })

    verdicts = {}
    if openrouter_client.is_available():
        try:
            system, prompt, max_tokens = build_claims_batch_exclusion_prompt(claims)
            raw = await openrouter_client.generate(prompt, system=system, max_tokens=max_tokens)
            verdicts = parse_claims_batch_exclusion(raw, [c["id"] for c in claims])
        except Exception as e:
            logger.warning(f"[MarketplaceAlerts] Error clasificando reclamos: {e}")

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
