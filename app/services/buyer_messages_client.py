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
import re
import time
import httpx
from email.header import decode_header
from email.message import EmailMessage

from app.config import AMAZON_BUYER_INBOX_ACCOUNTS, GMAIL_OAUTH_CLIENT_ID, GMAIL_OAUTH_CLIENT_SECRET
from app.services import token_store

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
    r'-{5,}\s*Mensaje:\s*-{5,}\s*(.*?)\s*-{5,}\s*Finalizar mensaje\s*-{5,}',
    re.DOTALL,
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
    return "".join(out)


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

    msg_match = _MSG_RE.search(body)
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
    }


def _poll_account_sync(cfg: dict) -> list[dict]:
    """Bloqueante — se llama envuelta en asyncio.to_thread. Busca correos
    entrantes del dominio de Amazon buyer-messaging y parsea los nuevos."""
    found: list[dict] = []
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
    try:
        M.login(cfg["email"], cfg["app_password"])
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, 'FROM "marketplace.amazon.com"')
        if typ != "OK":
            return found
        uids = data[0].split()
        # Solo los últimos 200 en cada pasada — el backlog histórico se
        # importa una sola vez manualmente, el poller normal solo necesita
        # ver los mensajes nuevos desde la última pasada.
        for uid in uids[-200:]:
            typ, msg_data = M.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            parsed = parse_buyer_message_email(raw)
            if parsed:
                parsed["seller_id"] = cfg["seller_id"]
                found.append(parsed)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return found


async def poll_account_inbox(cfg: dict) -> int:
    """Poll de una cuenta — retorna cuántos mensajes nuevos se insertaron."""
    messages = await asyncio.to_thread(_poll_account_sync, cfg)
    inserted = 0
    for m in messages:
        row_id = await token_store.insert_buyer_message(m)
        if row_id:
            inserted += 1
    return inserted


async def poll_all_accounts() -> dict:
    """Poll de todas las cuentas con buzón configurado. No falla si una
    cuenta individual da error (credenciales revocadas, red, etc.) — se
    salta y sigue con las demás."""
    results = {}
    for cfg in AMAZON_BUYER_INBOX_ACCOUNTS:
        try:
            results[cfg["seller_id"]] = await poll_account_inbox(cfg)
        except Exception as e:
            results[cfg["seller_id"]] = f"error: {e}"
    return results


async def poll_loop() -> None:
    """Loop de fondo — se lanza una vez al arrancar la app (main.py startup),
    igual que los demás loops de prewarm/cache existentes."""
    while True:
        if AMAZON_BUYER_INBOX_ACCOUNTS:
            try:
                await poll_all_accounts()
            except Exception:
                pass
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


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


async def _gmail_access_token(refresh_token: str) -> str:
    """Cambia el refresh_token (obtenido una vez en /auth/gmail/connect) por
    un access_token de corta duración — se hace en cada envío, es una sola
    llamada HTTPS y evita tener que manejar expiración manualmente."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(GMAIL_TOKEN_URL, data={
            "client_id": GMAIL_OAUTH_CLIENT_ID,
            "client_secret": GMAIL_OAUTH_CLIENT_SECRET,
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

    access_token = await _gmail_access_token(cfg["gmail_refresh_token"])
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
        )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"Gmail API rechazó el envío: {resp.status_code} {resp.text[:300]}")

    return msg["Message-ID"] or ""
