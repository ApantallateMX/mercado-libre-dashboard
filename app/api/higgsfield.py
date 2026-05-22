"""
higgsfield.py — API endpoints para generación de contenido visual con Higgsfield AI

Endpoints:
  GET  /api/higgsfield/check              → verifica créditos disponibles
  POST /api/higgsfield/generate           → inicia una generación (imagen o video)
  GET  /api/higgsfield/status/{id}        → consulta el estado de una generación
"""

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services import higgsfield_client as hf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/higgsfield", tags=["higgsfield"])


# ─── Schemas ─────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    mode: str           # "image" | "video"
    title: str          # título del listing (para construir el prompt)
    thumbnail_url: str = ""   # URL de la foto actual del producto
    custom_prompt: str = ""   # prompt personalizado del usuario (opcional)
    sku: str = ""


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/check")
async def credit_check():
    """Verifica si hay créditos disponibles en Higgsfield."""
    ok = await hf.check_credits()
    return {"ok": ok, "message": "Créditos disponibles ✓" if ok else "Sin créditos disponibles"}


@router.post("/generate")
async def generate(req: GenerateRequest):
    """
    Inicia una generación de imagen o video.
    Retorna request_id para hacer polling de estado.
    """
    try:
        if req.mode == "image":
            prompt = hf.build_image_prompt(req.title, req.custom_prompt)
            request_id = await hf.generate_image(prompt)

        elif req.mode == "video":
            if not req.thumbnail_url:
                raise HTTPException(status_code=400, detail="thumbnail_url requerido para modo video")

            # Subir la imagen al CDN de Higgsfield y animarla
            try:
                hosted_url = await hf.upload_from_url(req.thumbnail_url)
            except Exception as e:
                logger.warning(f"No se pudo subir thumbnail desde URL, usando directo: {e}")
                hosted_url = req.thumbnail_url

            prompt = hf.build_video_prompt(req.title, req.custom_prompt)
            request_id = await hf.generate_video(hosted_url, prompt)

        else:
            raise HTTPException(status_code=400, detail=f"Modo inválido: {req.mode}")

        return {"request_id": request_id, "mode": req.mode}

    except HTTPException:
        raise
    except ValueError as e:
        err = str(e)
        if "not_enough_credits" in err:
            raise HTTPException(status_code=402, detail="Sin créditos disponibles en Higgsfield")
        raise HTTPException(status_code=500, detail=err)
    except Exception as e:
        logger.error(f"Higgsfield generate error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{request_id}")
async def generation_status(request_id: str):
    """Consulta el estado de una generación. Hacer polling cada 3s."""
    try:
        result = await hf.get_status(request_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
