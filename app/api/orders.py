from fastapi import APIRouter, HTTPException, Query
from app.services.meli_client import get_meli_client

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("")
async def get_orders(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    sort: str = Query("date_desc")
):
    """Lista las ordenes del vendedor con paginacion."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        orders = await client.get_orders(offset=offset, limit=limit, sort=sort)
        return orders
    finally:
        await client.close()


@router.get("/{order_id}")
async def get_order(order_id: str):
    """Obtiene el detalle de una orden especifica."""
    client = await get_meli_client()
    if not client:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        order = await client.get_order(order_id)
        return order
    finally:
        await client.close()
