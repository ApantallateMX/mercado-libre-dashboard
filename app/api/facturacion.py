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
    if du["role"] not in ("admin", "editor", "editor_meli"):
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
    # Enriquecer con tiene_datos y tiene_factura
    for r in rows:
        r["order_data"] = json.loads(r.get("order_data") or "{}")
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
        "has_invoice": invoice is not None,
        "invoice_filename": invoice[0] if invoice else None,
    }


@router.post("/requests/{req_id}/invoice")
async def upload_invoice(
    request: Request,
    req_id: int,
    file: UploadFile = File(...),
):
    du = _require_editor(request)
    req = await token_store.get_billing_request_by_id(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB max
        raise HTTPException(status_code=413, detail="Archivo muy grande (máx 10 MB)")

    await token_store.save_billing_invoice(
        request_id=req_id,
        filename=file.filename or "factura.pdf",
        file_data=content,
        uploaded_by=du["username"],
    )
    await token_store.update_billing_status(req_id, "invoice_ready")
    return {"ok": True, "filename": file.filename}


@router.get("/requests/{req_id}/invoice")
async def download_invoice_admin(request: Request, req_id: int):
    _require_editor(request)
    result = await token_store.get_billing_invoice(req_id)
    if not result:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    filename, data = result
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
