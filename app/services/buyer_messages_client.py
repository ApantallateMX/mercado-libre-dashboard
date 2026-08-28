"""Mensajes de Compradores Amazon — captura vía buzón Gmail dedicado.

NO usa SP-API — no existe ningún endpoint de Amazon para leer mensajes
entrantes de compradores (ver .claude/memory/reference_amazon_sp_api_docs.md).
El mecanismo real (el mismo que usan Replyco/eDesk/ChannelReply) es el reenvío
de correo que el propio Amazon ofrece: Seller Central → Notification
Preferences → Messaging → "Buyer Messages" apunta a un buzón que nosotros
controlamos, y ese mismo buzón se registra como "Approved Sender" para poder
responder por email — Amazon relanza la respuesta al comprador real de forma
anónima.

Formato de correo confirmado contra mensajes reales de VECKTOR (2026-07-22):
    From/Reply-To: "{Nombre} <token@marketplace.amazon.com.mx>"
    Subject: "Consulta sobre detalles del producto del cliente de Amazon {Nombre}"
             (a veces con " (Pedido: XXX-XXXXXXX-XXXXXXX)" al final)
    Body (text/plain):
        Recibiste un mensaje.

        # XXX-XXXXXXX-XXXXXXX:            <- opcional, no siempre hay orden
        {qty} / {titulo del producto} | {id} [ASIN: XXXXXXXXXX]

        ------------- Mensaje: -------------

        {texto real del comprador}

        ------------- Finalizar mensaje -------------

        ...boilerplate de Amazon (encuesta, links, copyright)...
"""

import asyncio
import base64
import email
import imaplib
import logging
import re
import time
import httpx
from email.header import decode_header
from email.message import EmailMessage

from app.config import AMAZON_BUYER_INBOX_ACCOUNTS
from app.services import token_store

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
# El ENVÍO ya no usa SMTP — Railway bloquea egress a los puertos 465/587
# (confirmado con /api/diag/smtp-test: "Network is unreachable" en ambos,
# política anti-spam estándar de la mayoría de hosts en la nube). Responder
# usa la API de Gmail por HTTPS (nunca bloqueado), autenticado vía OAuth
# (ver /auth/gmail/connect en auth.py). La LECTURA sigue siendo IMAP normal
# (puerto 993 no está bloqueado, confirmado — el poller funciona en prod).
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

_ORDER_RE = re.compile(r'#\s*(\d{3}-\d{7}-\d{7})\s*:')
_SUBJECT_ORDER_RE = re.compile(r'\(Pedido:\s*(\d{3}-\d{7}-\d{7})\)')
_ASIN_RE = re.compile(r'ASIN:\s*([A-Z0-9]{10})')
_PRODUCT_LINE_RE = re.compile(r'^\s*\d+\s*/\s*(.+?)\s*\|.*\[ASIN:\s*([A-Z0-9]{10})\]', re.MULTILINE)
_MSG_RE = re.compile(
    # Cuentas con marketplaces en varios idiomas (ej. ExclusiveBulbs: MX/CA/US/BR)
    # reciben esta notificación de Amazon en el idioma de cada marketplace —
    # confirmado contra mensajes reales: español ("Iniciar mensaje"/
    # "Finalizar mensaje", y una variante más vieja "Mensaje:"), inglés
    # ("Message"/"End message"), portugués ("Mensagem"/"Encerrar mensagem").
    # El cierre exige un token conocido (no cualquier palabra) para no cortar
    # el match en un sub-encabezado interno de un hilo con cita ("Mensaje de
    # respuesta"/"Mensaje original") en vez del cierre real del mensaje.
    r'-{5,}\s*(?:Mensaje|Iniciar mensaje|Message|Mensagem)\s*:?\s*-{5,}\s*(.*?)\s*-{5,}\s*'
    r'(?:Finalizar mensaje|End message|Encerrar mensagem)\s*-{5,}',
    re.DOTALL | re.IGNORECASE,
)
# Variante sin marcador de apertura — el texto del comprador arranca justo
# después de la línea de producto/ASIN y solo trae el cierre. Se usa como
# respaldo si _MSG_RE no matchea.
_MSG_FALLBACK_RE = re.compile(
    r'\[ASIN:\s*[A-Z0-9]{10}\]\s*\r?\n\r?\n(.*?)\s*-{5,}\s*'
    r'(?:Finalizar mensaje|End message|Encerrar mensagem)\s*-{5,}',
    re.DOTALL | re.IGNORECASE,
)
_FROM_NAME_RE = re.compile(r'^([^<]+)<')
_FROM_ADDR_RE = re.compile(r'<(.+?)>')

_POLL_INTERVAL_SECONDS = 300  # 5 min


def _decode_header_value(raw: str) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    value = "".join(out)
    # Los headers largos vienen "plegados" (RFC 2822/5322: continúan en la
    # siguiente línea con \r\n + espacio) — sin normalizar esto, un Subject
    # así guardado revienta más tarde al construir la respuesta
    # (email.message rechaza headers con \r\n: "Header values may not
    # contain linefeed or carriage return characters").
    return re.sub(r"\s+", " ", value).strip()


def _get_text_body(msg: email.message.Message) -> str | None:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        return None
    charset = msg.get_content_charset() or "utf-8"
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors="replace") if payload else None


_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024  # 5MB — son capturas de pantalla, no debería pasar


def _get_attachments(msg: email.message.Message) -> list[dict]:
    """Recolecta partes MIME de imagen (adjuntas o inline) del correo
    reenviado por Amazon. FIX 2026-08-27: hasta ahora se descartaban en
    silencio -- _get_text_body() solo mira text/plain y hace return en
    cuanto encuentra la primera, sin recorrer el resto de las partes.
    Confirmado con un caso real (comprador adjuntó una captura de pantalla
    de un error de feed, orden 702-6149854-7649051): Amazon SÍ reenvía el
    adjunto real como parte MIME normal (verificado por Jovan viendo el
    correo crudo en Gmail: "One attachment"), no solo un link a Seller
    Central. NUNCA se persiste a disco -- los bytes viven en memoria hasta
    que se insertan como BLOB en SQLite (mismo criterio que _build_mime_message
    para adjuntos salientes, y misma razón: este proyecto ya tuvo 2
    incidentes reales de disco lleno en Railway)."""
    out = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        ctype = part.get_content_type()
        if not ctype.startswith("image/"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > _ATTACHMENT_MAX_BYTES:
            logger.warning(f"[BUYER-MSG] adjunto descartado por tamaño ({len(payload)}B): {part.get_filename()}")
            continue
        out.append({
            "filename": part.get_filename() or "adjunto",
            "content_type": ctype,
            "data": payload,
        })
    return out


def parse_buyer_message_email(raw_bytes: bytes) -> dict | None:
    """Parsea un correo crudo del buzón dedicado. Retorna None si no es un
    mensaje real de comprador (otras notificaciones del mismo dominio no
    traen los marcadores 'Mensaje:'/'Finalizar mensaje')."""
    msg = email.message_from_bytes(raw_bytes)
    from_header = _decode_header_value(msg.get("From", ""))
    addr_match = _FROM_ADDR_RE.search(from_header)
    from_addr = addr_match.group(1) if addr_match else from_header
    if "marketplace.amazon.com" not in from_addr:
        return None

    body = _get_text_body(msg)
    if not body:
        return None

    msg_match = _MSG_RE.search(body) or _MSG_FALLBACK_RE.search(body)
    if not msg_match:
        return None

    subject = _decode_header_value(msg.get("Subject", ""))
    order_match = _ORDER_RE.search(body) or _SUBJECT_ORDER_RE.search(subject)
    product_match = _PRODUCT_LINE_RE.search(body)
    asin_match = _ASIN_RE.search(body)
    name_match = _FROM_NAME_RE.match(from_header)

    date_hdr = msg.get("Date")
    try:
        ts = email.utils.mktime_tz(email.utils.parsedate_tz(date_hdr)) if date_hdr else time.time()
    except Exception:
        ts = time.time()

    return {
        "buyer_name": name_match.group(1).strip() if name_match else "",
        "order_id": order_match.group(1) if order_match else "",
        "asin": (product_match.group(2) if product_match else (asin_match.group(1) if asin_match else "")),
        "product_title": product_match.group(1).strip() if product_match else "",
        "subject": subject,
        "body_text": msg_match.group(1).strip(),
        "reply_to_addr": from_addr,
        "message_id": (msg.get("Message-ID") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "ts": ts,
        "attachments": _get_attachments(msg),
    }


def _find_all_mail_folder(M: imaplib.IMAP4_SSL) -> str:
    """Encuentra el nombre real del folder "Todos los correos" vía el
    atributo especial \\All (RFC 6154) en vez de asumir un nombre en inglés
    ("All Mail") — el nombre visible cambia según el idioma de la cuenta de
    Gmail (ej. "[Gmail]/Todos" en español). Cae a INBOX si no lo encuentra."""
    typ, data = M.list()
    if typ == "OK":
        for line in data:
            line_str = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
            if "\\All" in line_str:
                # Formato: (flags) "delimiter" "nombre del folder"
                match = re.search(r'"([^"]+)"$', line_str)
                if match:
                    return match.group(1)
    return "INBOX"


def _poll_account_sync(cfg: dict, last_uid: int = 0) -> tuple[list[dict], int]:
    """Bloqueante — se llama envuelta en asyncio.to_thread. Busca correos
    entrantes del dominio de Amazon buyer-messaging y parsea los nuevos.
    Busca en "Todos los correos" (no solo INBOX) porque Jovan usa un filtro
    de Gmail que etiqueta y archiva (Skip Inbox) los correos de Amazon para
    mantener su bandeja limpia — un mensaje archivado ya no aparece en INBOX
    pero sigue existiendo ahí sin importar la etiqueta.

    Usa UID de IMAP (M.uid(...), no M.search()/M.fetch() de sequence number)
    porque el UID es estable entre sesiones — permite guardar un watermark
    real (2026-08-04: antes se re-descargaban los mismos 200 correos en CADA
    pasada, 60-80s por cuenta, sin importar si ya se habían visto).
    Retorna (mensajes_parseados, uid_mas_alto_visto) — el caller persiste el
    watermark aunque un UID no haya parseado como mensaje de comprador real,
    para no volver a revisarlo nunca."""
    found: list[dict] = []
    max_uid_seen = last_uid
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
    try:
        M.login(cfg["email"], cfg["app_password"])
        all_mail_folder = _find_all_mail_folder(M)
        M.select(f'"{all_mail_folder}"', readonly=True)
        if last_uid:
            criteria = f'(FROM "marketplace.amazon.com" UID {last_uid + 1}:*)'
        else:
            criteria = 'FROM "marketplace.amazon.com"'
        typ, data = M.uid("search", None, criteria)
        if typ != "OK":
            return found, max_uid_seen
        uids = data[0].split()
        # Primer poll de una cuenta (sin watermark todavía): acotar a los
        # últimos 200 como antes, para no descargar el historial completo de
        # un buzón reusado (puede tener miles de correos viejos). Con
        # watermark ya puesto, "uids" son solo los nuevos — normalmente 0-5.
        scan_uids = uids if last_uid else uids[-200:]
        for uid in scan_uids:
            typ, msg_data = M.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            parsed = parse_buyer_message_email(raw)
            if parsed:
                parsed["seller_id"] = cfg["seller_id"]
                found.append(parsed)
            uid_int = int(uid)
            if uid_int > max_uid_seen:
                max_uid_seen = uid_int
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return found, max_uid_seen


def _inspect_account_sync(cfg: dict, sample_n: int = 5) -> dict:
    """DIAGNÓSTICO — variante no destructiva de _poll_account_sync que reporta
    conteos crudos (cuántos correos matchean el FROM, cuántos parsean vs no)
    y una muestra de los que NO parsean (asunto + primeros 300 chars del
    cuerpo) para ver si es una plantilla de Amazon distinta que la regex no
    cubre, en vez de asumir 'no hay mensajes nuevos'."""
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
    try:
        M.login(cfg["email"], cfg["app_password"])
        all_mail_folder = _find_all_mail_folder(M)
        M.select(f'"{all_mail_folder}"', readonly=True)
        typ, data = M.search(None, 'FROM "marketplace.amazon.com"')
        if typ != "OK":
            return {"folder": all_mail_folder, "error": f"search failed: {typ}"}
        uids = data[0].split()
        recent = uids[-200:]

        # DIAGNÓSTICO: comparar contra un SINCE real (por fecha IMAP, no por
        # posición en la lista de UIDs) — si aparecen UIDs por-fecha que NO
        # están en los últimos 200 por-UID, el orden de UID de este buzón no
        # refleja orden cronológico real (ej. import histórico masivo con
        # UIDs altos pero Date viejo) y la ventana "últimos 200 por UID" puede
        # estar dejando fuera mensajes genuinamente nuevos.
        from datetime import datetime as _dt_since, timedelta as _td_since
        since_imap = (_dt_since.utcnow() - _td_since(days=7)).strftime("%d-%b-%Y")
        typ2, data2 = M.search(None, f'(FROM "marketplace.amazon.com" SINCE {since_imap})')
        since_uids = data2[0].split() if typ2 == "OK" else []
        recent_set = set(recent)
        uids_not_in_recent_window = [u for u in since_uids if u not in recent_set]
        parsed_ok = 0
        failures = []
        for uid in recent:
            typ, msg_data = M.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            parsed = parse_buyer_message_email(raw)
            if parsed:
                parsed_ok += 1
            elif len(failures) < sample_n:
                msg = email.message_from_bytes(raw)
                subj = _decode_header_value(msg.get("Subject", ""))
                date_hdr = msg.get("Date", "")
                body = _get_text_body(msg) or ""
                failures.append({"subject": subj, "date": date_hdr, "body_snippet": body[:300]})

        missed_samples = []
        for uid in uids_not_in_recent_window[:sample_n]:
            typ, msg_data = M.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            missed_samples.append({
                "subject": _decode_header_value(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
            })

        return {
            "folder": all_mail_folder, "total_matched_from_filter": len(uids),
            "scanned_most_recent": len(recent), "parsed_ok": parsed_ok,
            "failed_to_parse": len(recent) - parsed_ok,
            "sample_failures": failures,
            "since_7d_search_total": len(since_uids),
            "since_7d_outside_uid_window": len(uids_not_in_recent_window),
            "sample_missed_by_uid_window": missed_samples,
        }
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _backfill_attachments_sync(cfg: dict, pending: list[dict]) -> dict:
    """Bloqueante — se llama envuelta en asyncio.to_thread. Re-consulta por
    Message-ID (una sola conexión IMAP para todo el lote, no una por
    mensaje) los correos ya guardados ANTES del fix de adjuntos, y extrae
    cualquier imagen que el parser viejo haya descartado en silencio.
    Solo lectura (readonly=True) -- mismo patrón que _inspect_account_sync."""
    checked_ids: list[int] = []
    found_total = 0
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
    try:
        M.login(cfg["email"], cfg["app_password"])
        all_mail_folder = _find_all_mail_folder(M)
        M.select(f'"{all_mail_folder}"', readonly=True)
        for row in pending:
            message_id = row["message_id"]
            checked_ids.append(row["id"])
            if not message_id:
                continue
            try:
                # HEADER Message-ID busca substring -- el valor guardado ya
                # incluye los < > del header real, sirve como match exacto.
                typ, data = M.search(None, f'(HEADER Message-ID "{message_id}")')
                if typ != "OK" or not data or not data[0]:
                    continue
                uid = data[0].split()[0]
                typ2, msg_data = M.fetch(uid, "(RFC822)")
                if typ2 != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                attachments = _get_attachments(msg)
                if attachments:
                    found_total += len(attachments)
                    row["_recovered"] = {"message_row_id": row["id"], "attachments": attachments}
            except Exception as _e:
                logger.warning(f"[BUYER-MSG-BACKFILL] error message_id={message_id}: {_e}")
                continue
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return {"checked_ids": checked_ids, "found_total": found_total,
            "recovered": [row["_recovered"] for row in pending if row.get("_recovered")]}


async def backfill_attachments_for_seller(seller_id: str = "", limit: int = 500) -> dict:
    """Backfill puntual (2026-08-27): recorre mensajes guardados ANTES del fix
    de adjuntos (attachments_checked=0), re-consulta el correo real por
    Message-ID en el buzón dedicado, y recupera cualquier imagen que el
    parser viejo haya descartado en silencio. Solo lectura contra IMAP --
    nunca escribe nada al buzón. Se marca attachments_checked=1 para TODOS
    los revisados (haya encontrado adjunto o no), para que una segunda
    corrida no repita trabajo ya hecho."""
    pending = await token_store.get_inbound_messages_needing_attachment_check(seller_id, limit=limit)
    if not pending:
        return {"checked": 0, "recovered_messages": 0, "recovered_attachments": 0, "by_account": {}}

    by_account: dict = {}
    for row in pending:
        by_account.setdefault(row["seller_id"], []).append(row)

    summary_by_account = {}
    total_checked = 0
    total_recovered_msgs = 0
    total_recovered_atts = 0
    for sid, rows in by_account.items():
        cfg = next((c for c in AMAZON_BUYER_INBOX_ACCOUNTS if c["seller_id"] == sid), None)
        if not cfg:
            continue
        result = await asyncio.to_thread(_backfill_attachments_sync, cfg, rows)
        for rec in result["recovered"]:
            await token_store.insert_buyer_message_attachments(rec["message_row_id"], rec["attachments"])
        await token_store.mark_attachments_checked(result["checked_ids"])
        summary_by_account[sid] = {
            "checked": len(result["checked_ids"]),
            "recovered_messages": len(result["recovered"]),
            "recovered_attachments": result["found_total"],
        }
        total_checked += len(result["checked_ids"])
        total_recovered_msgs += len(result["recovered"])
        total_recovered_atts += result["found_total"]
    return {
        "checked": total_checked, "recovered_messages": total_recovered_msgs,
        "recovered_attachments": total_recovered_atts, "by_account": summary_by_account,
    }


async def poll_account_inbox(cfg: dict) -> int:
    """Poll de una cuenta — retorna cuántos mensajes nuevos se insertaron.
    Usa el watermark de UID persistido (token_store) para no re-descargar
    correos ya vistos en pasadas anteriores."""
    last_uid = await token_store.get_buyer_inbox_watermark(cfg["seller_id"])
    messages, max_uid = await asyncio.to_thread(_poll_account_sync, cfg, last_uid)
    inserted = 0
    for m in messages:
        row_id = await token_store.insert_buyer_message(m)
        if row_id:
            inserted += 1
            if m.get("attachments"):
                await token_store.insert_buyer_message_attachments(row_id, m["attachments"])
    if max_uid > last_uid:
        await token_store.set_buyer_inbox_watermark(cfg["seller_id"], max_uid)
    return inserted


async def poll_all_accounts() -> dict:
    """Poll de todas las cuentas con buzón configurado, EN PARALELO (antes
    secuencial — una cuenta lenta ya no retrasa a las demás dentro del mismo
    ciclo). No falla si una cuenta individual da error (credenciales
    revocadas, red, etc.) — se salta y sigue con las demás."""
    async def _one(cfg):
        try:
            return cfg["seller_id"], await poll_account_inbox(cfg)
        except Exception as e:
            return cfg["seller_id"], f"error: {e}"

    pairs = await asyncio.gather(*[_one(cfg) for cfg in AMAZON_BUYER_INBOX_ACCOUNTS])
    results = dict(pairs)
    return results


async def poll_loop() -> None:
    """Loop de fondo — se lanza una vez al arrancar la app (main.py startup),
    igual que los demás loops de prewarm/cache existentes. Log a nivel INFO
    en cada ciclo (antes solo se logueaba en error) para poder confirmar
    desde los logs de Railway que el loop sigue vivo — se detectó 2026-08-05
    que el índice se quedaba con mensajes de hasta 4 días de antigüedad sin
    que hubiera ningún error visible, y sin heartbeat no había forma de saber
    si el loop seguía corriendo o simplemente dejó de ejecutarse."""
    last_purge_day = -1
    while True:
        if AMAZON_BUYER_INBOX_ACCOUNTS:
            try:
                results = await poll_all_accounts()
                errors = {k: v for k, v in results.items() if isinstance(v, str) and v.startswith("error:")}
                total_new = sum(v for v in results.values() if isinstance(v, int))
                logger.info(f"[BUYER-MSG-POLL] ciclo ok — {len(AMAZON_BUYER_INBOX_ACCOUNTS)} cuenta(s), {total_new} mensaje(s) nuevo(s)")
                if errors:
                    logger.warning(f"[BUYER-MSG-POLL] Error en {len(errors)} cuenta(s): {errors}")
            except Exception as _e:
                logger.warning(f"[BUYER-MSG-POLL] Error en poll_all_accounts: {_e}")
        # Purga de retención de adjuntos (2026-08-27) — 1x/día, no cada 5 min.
        # Borra solo el BLOB (>6 meses), conserva la fila con metadata. Ver
        # token_store.purge_old_buyer_message_attachments para el porqué.
        _today = int(time.time() // 86400)
        if _today != last_purge_day:
            last_purge_day = _today
            try:
                purged = await token_store.purge_old_buyer_message_attachments(months=6)
                if purged:
                    logger.info(f"[BUYER-MSG-POLL] {purged} adjunto(s) purgado(s) por retención (>6 meses)")
            except Exception as _e:
                logger.warning(f"[BUYER-MSG-POLL] Error purgando adjuntos: {_e}")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


_opportunistic_last_run: dict[str, float] = {}
_OPPORTUNISTIC_COOLDOWN_SECONDS = 60


def trigger_opportunistic_poll(seller_id: str) -> None:
    """Red de seguridad: dispara un poll en background de ESTA cuenta cuando
    alguien abre la pestaña de Mensajes, sin bloquear la carga de la página
    (fire-and-forget) y sin golpear IMAP en cada refresh del tab (cooldown).
    Cubre el caso de que poll_loop() se detenga sin error visible (ver nota
    en poll_loop) — el índice se auto-repara solo con que alguien mire la
    pestaña, sin depender de que el loop de fondo esté sano."""
    seller_id = (seller_id or "").strip()
    if not seller_id:
        return
    now = time.time()
    if now - _opportunistic_last_run.get(seller_id, 0) < _OPPORTUNISTIC_COOLDOWN_SECONDS:
        return
    cfg = next((c for c in AMAZON_BUYER_INBOX_ACCOUNTS if c["seller_id"] == seller_id), None)
    if not cfg:
        return
    _opportunistic_last_run[seller_id] = now

    async def _run():
        try:
            await poll_account_inbox(cfg)
        except Exception as _e:
            logger.warning(f"[BUYER-MSG-OPPORTUNISTIC] error cuenta={seller_id}: {_e}")

    asyncio.create_task(_run())


def _build_mime_message(
    from_addr: str, to_addr: str, subject: str, body: str, in_reply_to: str,
    attachment: tuple[str, bytes, str] | None = None,
) -> EmailMessage:
    """attachment, si se da, es (filename, contenido, content_type) — NO se
    persiste en disco en ningún punto de este flujo, solo vive en memoria
    hasta que se manda. No está confirmado que Amazon preserve el adjunto al
    relanzar el correo al comprador real (es el canal de reenvío, no la API
    oficial de Seller Central) — se manda de todos modos, pendiente de
    verificar."""
    # Defensivo: filas guardadas antes de este fix pueden traer el Subject
    # con \r\n de header plegado (RFC 5322) sin normalizar — email.message
    # rechaza cualquier header con salto de línea.
    subject = re.sub(r"\s+", " ", subject or "").strip()

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["Message-ID"] = email.utils.make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)

    if attachment:
        filename, data, content_type = attachment
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename)
    return msg


async def _gmail_access_token(refresh_token: str, client_id: str, client_secret: str) -> str:
    """Cambia el refresh_token (obtenido una vez en /auth/gmail/connect) por
    un access_token de corta duración — se hace en cada envío, es una sola
    llamada HTTPS y evita tener que manejar expiración manualmente. client_id/
    secret DEBEN ser los del mismo proyecto de Google que emitió ese
    refresh_token específico (cada cuenta Amazon2026-07-24 en adelante tiene
    su propio proyecto/cliente OAuth) — usar el cliente equivocado da
    'unauthorized_client', un bug real que pasó aquí el mismo día que se
    crearon los clientes _2/_3 y no se propagó a esta función."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(GMAIL_TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    if resp.status_code != 200:
        raise RuntimeError(f"No se pudo renovar el token de Gmail: {resp.status_code} {resp.text[:200]}")
    return resp.json()["access_token"]


async def send_reply(
    seller_id: str, to_addr: str, subject: str, body: str, in_reply_to: str = "",
    attachment: tuple[str, bytes, str] | None = None,
) -> str:
    """Envía por la API de Gmail (HTTPS) — NO por SMTP. Railway bloquea el
    egress a los puertos de envío de correo (465/587, confirmado con
    /api/diag/smtp-test), así que smtplib no funciona en producción aunque sí
    funcione en local. La API de Gmail usa HTTPS (nunca bloqueado)."""
    cfg = next((c for c in AMAZON_BUYER_INBOX_ACCOUNTS if c["seller_id"] == seller_id), None)
    if cfg is None:
        raise ValueError(f"No hay buzón configurado para seller_id={seller_id}")
    if not cfg.get("gmail_refresh_token"):
        raise ValueError(
            f"La cuenta {cfg['email']} no ha autorizado la API de Gmail todavía — "
            f"visita /auth/gmail/connect para hacerlo."
        )

    msg = _build_mime_message(cfg["email"], to_addr, subject, body, in_reply_to, attachment)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    access_token = await _gmail_access_token(
        cfg["gmail_refresh_token"], cfg["gmail_client_id"], cfg["gmail_client_secret"]
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
        )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"Gmail API rechazó el envío: {resp.status_code} {resp.text[:300]}")

    return msg["Message-ID"] or ""


async def send_notification(to_addr: str, subject: str, body: str) -> str:
    """Envía un correo interno (alertas del sistema, no respuesta a comprador)
    usando cualquier cuenta Amazon ya autorizada en Gmail — a diferencia de
    send_reply(), NO fuerza el prefijo "Re:" (no tiene sentido en un correo
    nuevo) y prueba las cuentas configuradas en orden hasta que una logre
    enviar, para no depender de que una cuenta específica siga en modo
    OAuth "Testing" (refresh_token expira cada 7 días — ver incidente
    VECKTOR 2026-07-29)."""
    last_err = None
    for cfg in AMAZON_BUYER_INBOX_ACCOUNTS:
        if not cfg.get("gmail_refresh_token"):
            continue
        try:
            msg = _build_mime_message(cfg["email"], to_addr, subject, body, in_reply_to="")
            msg.replace_header("Subject", re.sub(r"\s+", " ", subject or "").strip())
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
            access_token = await _gmail_access_token(
                cfg["gmail_refresh_token"], cfg["gmail_client_id"], cfg["gmail_client_secret"]
            )
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    GMAIL_SEND_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"raw": raw},
                )
            if resp.status_code in (200, 202):
                return msg["Message-ID"] or ""
            last_err = f"{cfg['email']}: {resp.status_code} {resp.text[:200]}"
        except Exception as e:
            last_err = f"{cfg['email']}: {e}"
    raise RuntimeError(f"No se pudo enviar la notificación con ninguna cuenta configurada. Último error: {last_err}")


async def setup_organization_filter(seller_id: str, from_domain: str, label_name: str) -> dict:
    """Crea (o reusa) una etiqueta y un filtro que la aplica automáticamente
    a todo correo entrante de from_domain, y lo saca del inbox (Skip Inbox/
    Archivar) — Jovan pidió esto para mantener su bandeja limpia sin tener
    que crear el filtro él mismo a mano en la interfaz de Gmail. Requiere el
    scope gmail.settings.basic (ver /auth/gmail/connect)."""
    cfg = next((c for c in AMAZON_BUYER_INBOX_ACCOUNTS if c["seller_id"] == seller_id), None)
    if cfg is None:
        raise ValueError(f"No hay buzón configurado para seller_id={seller_id}")
    if not cfg.get("gmail_refresh_token"):
        raise ValueError(f"La cuenta {cfg['email']} no ha autorizado la API de Gmail todavía.")

    access_token = await _gmail_access_token(
        cfg["gmail_refresh_token"], cfg["gmail_client_id"], cfg["gmail_client_secret"]
    )
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get("https://gmail.googleapis.com/gmail/v1/users/me/labels", headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"No se pudieron leer las etiquetas: {resp.status_code} {resp.text[:200]}")
        labels = resp.json().get("labels", [])
        label = next((l for l in labels if l["name"] == label_name), None)

        if label is None:
            resp = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/labels", headers=headers,
                json={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"No se pudo crear la etiqueta: {resp.status_code} {resp.text[:200]}")
            label = resp.json()

        # Verificar si ya existe un filtro con este criterio (evita duplicar
        # si setup_organization_filter se corre más de una vez)
        existing = await client.get("https://gmail.googleapis.com/gmail/v1/users/me/settings/filters", headers=headers)
        filters = existing.json().get("filter", []) if existing.status_code == 200 else []
        gmail_filter = next((f for f in filters if f.get("criteria", {}).get("from") == from_domain), None)

        if gmail_filter is None:
            resp = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/settings/filters", headers=headers,
                json={
                    "criteria": {"from": from_domain},
                    "action": {"addLabelIds": [label["id"]], "removeLabelIds": ["INBOX"]},
                },
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"No se pudo crear el filtro: {resp.status_code} {resp.text[:300]}")
            gmail_filter = resp.json()

        # Gmail NO aplica el filtro retroactivamente a correos que ya estaban
        # en la bandeja antes de crearlo — hay que etiquetar/archivar el
        # backlog existente a mano vía batchModify.
        applied = await _apply_label_to_existing(client, headers, from_domain, label["id"])

        return {"label": label, "filter": gmail_filter, "applied_to_existing": applied}


async def _apply_label_to_existing(client: httpx.AsyncClient, headers: dict, from_domain: str, label_id: str) -> int:
    """Aplica la etiqueta (y saca de INBOX) a todos los correos YA existentes
    que matcheen from_domain — los filtros de Gmail solo corren hacia
    adelante, nunca retroactivo."""
    total = 0
    page_token = None
    while True:
        params = {"q": f"from:{from_domain}", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        resp = await client.get("https://gmail.googleapis.com/gmail/v1/users/me/messages", headers=headers, params=params)
        if resp.status_code != 200:
            break
        data = resp.json()
        ids = [m["id"] for m in data.get("messages", [])]
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            if not chunk:
                continue
            mod = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify", headers=headers,
                json={"ids": chunk, "addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
            )
            if mod.status_code == 204:
                total += len(chunk)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return total
