"""
facturacion.py — Módulo de Facturación (rutas internas, requieren dashboard_user).
Rutas públicas del cliente (/factura/{token}/*) viven en main.py.
"""

import uuid
import json
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from typing import Optional
from app.services import token_store

router = APIRouter(prefix="/api/facturacion", tags=["facturacion"])

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_du(request: Request) -> dict:
    du = getattr(request.state, "dashboard_user", None)
    if not du:
        raise HTTPException(status_code=401, detail="No autenticado")
    return du


def _require_editor(request: Request) -> dict:
    du = _get_du(request)
    from app.services.user_store import ROLE_CAN_FACTURACION
    if du["role"] not in ROLE_CAN_FACTURACION:
        raise HTTPException(status_code=403, detail="Sin permisos de facturación")
    return du


# ─── Catálogos SAT (para referencia en la UI) ─────────────────────────────────

CFDI_USES = [
    ("G01", "G01 — Adquisición de mercancias"),
    ("G02", "G02 — Devoluciones, descuentos o bonificaciones"),
    ("G03", "G03 — Gastos en general"),
    ("I01", "I01 — Construcciones"),
    ("I02", "I02 — Mobiliario y equipo de oficina"),
    ("I03", "I03 — Equipo de transporte"),
    ("I04", "I04 — Equipo de cómputo y accesorios"),
    ("I06", "I06 — Comunicaciones telefónicas"),
    ("I08", "I08 — Otra maquinaria y equipo"),
    ("D01", "D01 — Honorarios médicos, dentales y gastos hospitalarios"),
    ("D10", "D10 — Pagos por servicios educativos"),
    ("P01", "P01 — Por definir"),
    ("S01", "S01 — Sin efectos fiscales"),
    ("CP01", "CP01 — Pagos"),
    ("CN01", "CN01 — Nómina"),
]

FISCAL_REGIMES = [
    ("601", "601 — General de Ley Personas Morales"),
    ("603", "603 — Personas Morales con Fines no Lucrativos"),
    ("605", "605 — Sueldos y Salarios e Ingresos Asimilados a Salarios"),
    ("606", "606 — Arrendamiento"),
    ("607", "607 — Régimen de Enajenación o Adquisición de Bienes"),
    ("608", "608 — Demás ingresos"),
    ("610", "610 — Residentes en el Extranjero sin Establecimiento Permanente"),
    ("611", "611 — Ingresos por Dividendos"),
    ("612", "612 — Personas Físicas con Actividades Empresariales y Profesionales"),
    ("614", "614 — Ingresos por intereses"),
    ("615", "615 — Régimen de los ingresos por obtención de premios"),
    ("616", "616 — Sin obligaciones fiscales"),
    ("620", "620 — Sociedades Cooperativas de Producción"),
    ("621", "621 — Incorporación Fiscal"),
    ("622", "622 — Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras"),
    ("623", "623 — Opcional para Grupos de Sociedades"),
    ("624", "624 — Coordinados"),
    ("625", "625 — Régimen de las Actividades Empresariales con ingresos a través de Plataformas Tecnológicas"),
    ("626", "626 — Régimen Simplificado de Confianza (RESICO)"),
]

FORMAS_PAGO = [
    ("01", "01 — Efectivo"),
    ("02", "02 — Cheque nominativo"),
    ("03", "03 — Transferencia electrónica de fondos"),
    ("04", "04 — Tarjeta de crédito"),
    ("05", "05 — Monedero electrónico"),
    ("06", "06 — Dinero electrónico"),
    ("08", "08 — Vales de despensa"),
    ("12", "12 — Dación en pago"),
    ("13", "13 — Pago por subrogación"),
    ("14", "14 — Pago por consignación"),
    ("15", "15 — Condonación"),
    ("17", "17 — Compensación"),
    ("23", "23 — Novación"),
    ("24", "24 — Confusión"),
    ("25", "25 — Remisión de deuda"),
    ("26", "26 — Prescripción o caducidad"),
    ("27", "27 — A satisfacción del acreedor"),
    ("28", "28 — Tarjeta de débito"),
    ("29", "29 — Tarjeta de servicios"),
    ("30", "30 — Aplicación de anticipos"),
    ("31", "31 — Intermediario pagos"),
    ("99", "99 — Por definir"),
]


# ─── Rutas ────────────────────────────────────────────────────────────────────

def _is_amazon_order_id(order_id: str) -> bool:
    """Detecta si el ID tiene formato Amazon: XXX-XXXXXXX-XXXXXXX"""
    import re
    return bool(re.match(r'^\d{3}-\d{7}-\d{7}$', order_id.strip()))


@router.get("/order-lookup")
async def order_lookup(request: Request, order_number: str = ""):
    """
    Busca una orden en TODAS las cuentas ML y Amazon en paralelo.
    Detecta automáticamente la plataforma por el formato del ID.
    Solo para uso interno (admin/editor).
    """
    _require_editor(request)
    order_number = order_number.strip()
    if not order_number:
        return {"found": False, "error": "Ingresa un número de orden"}

    import asyncio

    # ── Amazon (formato 702-XXXXXXX-XXXXXXX) ────────────────────────────────
    if _is_amazon_order_id(order_number):
        from app.services.amazon_client import get_amazon_client

        amazon_accounts = await token_store.get_all_amazon_accounts()
        if not amazon_accounts:
            return {"found": False, "error": "No hay cuentas Amazon configuradas"}

        async def _try_amazon(acc):
            try:
                client = await get_amazon_client(acc["seller_id"])
                if not client:
                    return None
                # GET /orders/v0/orders/{orderId}
                data = await client._request("GET", f"/orders/v0/orders/{order_number}")
                order = data.get("payload", {})
                if not order or not order.get("AmazonOrderId"):
                    return None
                # GET order items
                items_data = await client._request("GET", f"/orders/v0/orders/{order_number}/orderItems")
                items_raw = items_data.get("payload", {}).get("OrderItems", [])
                return {"account": acc, "order": order, "items_raw": items_raw}
            except Exception:
                return None

        results = await asyncio.gather(*[_try_amazon(a) for a in amazon_accounts])
        match = next((r for r in results if r is not None), None)

        if not match:
            return {"found": False, "error": "Orden Amazon no encontrada en ninguna cuenta"}

        acc = match["account"]
        order = match["order"]
        items = [
            {
                "title": it.get("Title", ""),
                "quantity": it.get("QuantityOrdered", 1),
                "unit_price": float((it.get("ItemPrice") or {}).get("Amount") or 0),
                "sku": it.get("SellerSKU", ""),
                "asin": it.get("ASIN", ""),
            }
            for it in match["items_raw"]
        ]
        total_obj = order.get("OrderTotal") or {}
        return {
            "found": True,
            "ml_user_id": acc["seller_id"],
            "nickname": acc.get("nickname") or acc["seller_id"],
            "platform": "amazon",
            "order": {
                "total": float(total_obj.get("Amount") or 0),
                "currency": total_obj.get("CurrencyCode", "MXN"),
                "date": (order.get("PurchaseDate") or "")[:10],
                "status": order.get("OrderStatus", ""),
                "items": items,
            },
        }

    # ── MercadoLibre (numérico / pack_id) ───────────────────────────────────
    from app.services.meli_client import get_meli_client

    accounts = await token_store.get_all_tokens()
    if not accounts:
        return {"found": False, "error": "No hay cuentas ML configuradas"}

    async def _try_meli(acc):
        try:
            client = await get_meli_client(user_id=acc["user_id"])
            order = await client.resolve_order(order_number)
            await client.close()
            if "error" in order or not order.get("id"):
                return None
            return {"account": acc, "order": order}
        except Exception:
            return None

    results = await asyncio.gather(*[_try_meli(a) for a in accounts])
    match = next((r for r in results if r is not None), None)

    if not match:
        return {"found": False, "error": "Orden no encontrada en ninguna cuenta ML"}

    acc = match["account"]
    order = match["order"]
    items = []
    for oi in order.get("order_items", []):
        item = oi.get("item", {})
        items.append({
            "title": item.get("title", ""),
            "quantity": oi.get("quantity", 1),
            "unit_price": oi.get("unit_price") or oi.get("full_unit_price"),
        })

    return {
        "found": True,
        "ml_user_id": acc["user_id"],
        "nickname": acc.get("nickname") or acc["user_id"],
        "platform": "mercadolibre",
        "order": {
            "total": order.get("total_amount"),
            "currency": order.get("currency_id", "MXN"),
            "date": (order.get("date_closed") or order.get("date_created") or "")[:10],
            "status": order.get("status", ""),
            "items": items,
        },
    }


@router.get("/catalogs")
async def get_catalogs():
    """Devuelve catálogos SAT para dropdowns."""
    return {
        "cfdi_uses": CFDI_USES,
        "fiscal_regimes": FISCAL_REGIMES,
        "formas_pago": FORMAS_PAGO,
    }


@router.get("/requests")
async def list_requests(request: Request, status: str = ""):
    _require_editor(request)
    rows = await token_store.list_billing_requests(status=status or None)

    # Construir mapa user_id/seller_id → nickname
    meli_accounts   = await token_store.get_all_tokens()
    amazon_accounts = await token_store.get_all_amazon_accounts()
    nick_map = {}
    for a in meli_accounts:
        nick_map[str(a["user_id"])] = a.get("nickname") or str(a["user_id"])
    for a in amazon_accounts:
        nick_map[str(a["seller_id"])] = a.get("nickname") or str(a["seller_id"])

    for r in rows:
        r["order_data"] = json.loads(r.get("order_data") or "{}")
        uid = str(r.get("ml_user_id") or "")
        r["account_nickname"] = nick_map.get(uid, "") if uid else ""
    return {"requests": rows}


@router.post("/requests")
async def create_request(
    request: Request,
    ml_user_id: str = Form(""),
    platform: str = Form("mercadolibre"),
    order_number: str = Form(""),
    client_ref: str = Form(""),
    notes: str = Form(""),
):
    du = _require_editor(request)
    token = str(uuid.uuid4())
    req_id = await token_store.create_billing_request(
        token=token,
        ml_user_id=ml_user_id,
        platform=platform,
        order_number=order_number,
        client_ref=client_ref,
        created_by=du["username"],
        notes=notes,
    )
    base_url = str(request.base_url).rstrip("/")
    return {
        "id": req_id,
        "token": token,
        "link": f"{base_url}/factura/{token}",
    }


@router.get("/requests/{req_id}")
async def get_request(request: Request, req_id: int):
    _require_editor(request)
    req = await token_store.get_billing_request_by_id(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    req["order_data"] = json.loads(req.get("order_data") or "{}")
    fiscal = await token_store.get_billing_fiscal_data(req_id)
    invoice = await token_store.get_billing_invoice(req_id)
    return {
        "request": req,
        "fiscal_data": fiscal,
        "has_invoice":    invoice is not None and bool(invoice.get("pdf_data") if invoice else False),
        "invoice_filename": invoice["pdf_filename"] if invoice else None,
        "has_xml":          bool(invoice and invoice.get("xml_data")),
        "xml_filename":     invoice["xml_filename"] if invoice else None,
    }


@router.post("/requests/{req_id}/invoice")
async def upload_invoice(
    request: Request,
    req_id: int,
    pdf_file: UploadFile = File(None),
    xml_file: UploadFile = File(None),
):
    du = _require_editor(request)
    req = await token_store.get_billing_request_by_id(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if not pdf_file and not xml_file:
        raise HTTPException(status_code=400, detail="Sube al menos un archivo (PDF o XML)")

    # Leer PDF
    pdf_content, pdf_name = b"", ""
    if pdf_file and pdf_file.filename:
        pdf_content = await pdf_file.read()
        if len(pdf_content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF muy grande (máx 10 MB)")
        pdf_name = pdf_file.filename

    # Leer XML
    xml_content, xml_name = None, ""
    if xml_file and xml_file.filename:
        xml_content = await xml_file.read()
        if len(xml_content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="XML muy grande (máx 10 MB)")
        xml_name = xml_file.filename

    # Si solo subieron XML (sin PDF), necesitamos preservar el PDF anterior si existe
    existing = await token_store.get_billing_invoice(req_id)
    if not pdf_content and existing:
        pdf_content = existing["pdf_data"]
        pdf_name    = existing["pdf_filename"]
    if xml_content is None and existing:
        xml_content = existing["xml_data"]
        xml_name    = existing["xml_filename"]

    await token_store.save_billing_invoice(
        request_id=req_id,
        filename=pdf_name or "factura.pdf",
        file_data=pdf_content or b"",
        uploaded_by=du["username"],
        xml_filename=xml_name,
        xml_data=xml_content,
    )
    await token_store.update_billing_status(req_id, "invoice_ready")
    return {"ok": True, "pdf_filename": pdf_name, "xml_filename": xml_name}


@router.get("/requests/{req_id}/invoice")
async def download_invoice_admin(request: Request, req_id: int):
    _require_editor(request)
    result = await token_store.get_billing_invoice(req_id)
    if not result or not result["pdf_data"]:
        raise HTTPException(status_code=404, detail="PDF no encontrado")
    return Response(
        content=result["pdf_data"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{result["pdf_filename"]}"'},
    )


@router.get("/requests/{req_id}/invoice/xml")
async def download_invoice_xml_admin(request: Request, req_id: int):
    _require_editor(request)
    result = await token_store.get_billing_invoice(req_id)
    if not result or not result["xml_data"]:
        raise HTTPException(status_code=404, detail="XML no encontrado")
    return Response(
        content=result["xml_data"],
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{result["xml_filename"]}"'},
    )


@router.get("/requests/{req_id}/constancia")
async def download_constancia_admin(request: Request, req_id: int):
    _require_editor(request)
    result = await token_store.get_billing_constancia(req_id)
    if not result:
        raise HTTPException(status_code=404, detail="Constancia no encontrada")
    filename, data = result
    # Detect content type by filename
    ct = "application/pdf" if filename.lower().endswith(".pdf") else "image/jpeg"
    return Response(
        content=data,
        media_type=ct,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/requests/{req_id}")
async def delete_request(request: Request, req_id: int):
    du = _get_du(request)
    if du["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede eliminar solicitudes")
    await token_store.delete_billing_request(req_id)
    return {"ok": True}
