"""
Deuda con la empresa proveedora
================================
Ledger semanal: por cada unidad vendida (ML + Amazon combinado) se genera
una deuda = % fijo del retail del SKU (80% teles / 50% otras categorías,
configurable). El cálculo real vive en token_store.upsert_order_history —
este módulo solo expone lectura del saldo/semanas y registro de pagos.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.services import token_store

router = APIRouter(prefix="/api/supplier-debt", tags=["supplier-debt"])


def _require_admin(request: Request):
    du = getattr(request.state, "dashboard_user", None)
    if not du or du.get("role") != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


@router.get("/summary")
async def supplier_debt_summary(request: Request):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    return await token_store.get_supplier_debt_summary()


@router.get("/payments")
async def supplier_debt_payments(request: Request):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    return {"payments": await token_store.list_supplier_debt_payments()}


@router.post("/payments")
async def supplier_debt_add_payment(request: Request):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    body = await request.json()
    payment_date = (body.get("payment_date") or "").strip()
    amount_mxn = float(body.get("amount_mxn") or 0)
    reference = (body.get("reference") or "").strip()
    notes = (body.get("notes") or "").strip()
    if not payment_date or amount_mxn <= 0:
        return JSONResponse({"error": "payment_date y amount_mxn > 0 son requeridos"}, status_code=400)
    du = getattr(request.state, "dashboard_user", None) or {}
    payment_id = await token_store.add_supplier_debt_payment(
        payment_date, amount_mxn, reference, notes, du.get("username", "")
    )
    return {"ok": True, "id": payment_id}


@router.delete("/payments/{payment_id}")
async def supplier_debt_delete_payment(payment_id: int, request: Request):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    deleted = await token_store.delete_supplier_debt_payment(payment_id)
    return {"ok": deleted}


@router.get("/export")
async def supplier_debt_export(request: Request):
    """Excel (.xlsx) con la deuda agregada por SKU: título, retail, costo,
    unidades vendidas y monto de deuda generado."""
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden

    import io
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Font

    rows = await token_store.get_supplier_debt_export_data()

    wb = Workbook()
    ws = wb.active
    ws.title = "Deuda por SKU"

    headers = ["SKU", "Título", "Retail (USD)", "Costo (USD)", "Unidades vendidas", "Monto generado (MXN)"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in rows:
        ws.append([
            r["sku"],
            r["titulo"],
            round(r["retail_usd"], 2) if r["retail_usd"] is not None else None,
            round(r["costo_usd"], 2) if r["costo_usd"] is not None else None,
            r["unidades"],
            round(r["monto_generado_mxn"], 2) if r["monto_generado_mxn"] is not None else 0,
        ])

    widths = [16, 45, 14, 14, 18, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    fname = f"deuda_empresa_{datetime.utcnow().strftime('%Y-%m-%d')}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/settings")
async def supplier_debt_get_settings(request: Request):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    return await token_store.get_supplier_debt_settings()


@router.post("/settings")
async def supplier_debt_set_settings(request: Request):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    body = await request.json()
    rate_tv = float(body.get("rate_tv") or 0.80)
    rate_other = float(body.get("rate_other") or 0.50)
    if not (0 < rate_tv <= 1) or not (0 < rate_other <= 1):
        return JSONResponse({"error": "rate_tv y rate_other deben estar entre 0 y 1"}, status_code=400)
    await token_store.set_supplier_debt_settings(rate_tv, rate_other)
    return {"ok": True}
