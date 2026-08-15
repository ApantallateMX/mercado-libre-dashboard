"""
higgsfield.py — API endpoints para generación de contenido visual con Higgsfield AI

Endpoints:
  GET  /api/higgsfield/check              → verifica créditos disponibles
  POST /api/higgsfield/generate           → inicia una generación (imagen o video)
  GET  /api/higgsfield/status/{id}        → consulta el estado de una generación
  POST /api/higgsfield/generate-images    → genera VARIAS imagenes basadas en la foto real (2026-08-15)
  GET  /api/higgsfield/image-job/{id}     → estado del job de varias imagenes
"""

import asyncio
import logging
import uuid as _uuid
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


class GenerateImagesRequest(BaseModel):
    """FIX 2026-08-15 (pedido por Jovan): antes solo se generaba 1 imagen y
    no se basaba en la foto real -- este request es para el nuevo modo de
    galeria (varias imagenes, todas ancladas a la foto real del producto)."""
    title: str
    thumbnail_url: str          # OBLIGATORIO -- sin esto no hay como anclar la generacion al producto real
    custom_prompt: str = ""
    sku: str = ""
    count: int = 8               # ver _run_image_job: se piden en lotes de hasta 4 (limite de Higgsfield)


# ─── Estado en memoria de jobs de galeria (mismo patron que _video_jobs en lanzar.py) ──
_image_jobs: dict = {}


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


@router.post("/generate-images")
async def generate_images(req: GenerateImagesRequest):
    """Inicia un job de VARIAS imagenes ancladas a la foto real del producto.
    Retorna job_id de inmediato -- usar GET /image-job/{job_id} para el estado.
    """
    if not req.thumbnail_url:
        raise HTTPException(
            status_code=400,
            detail="thumbnail_url requerido -- las imagenes deben basarse en la foto real del producto",
        )
    if not hf.HIGGSFIELD_KEY_ID or not hf.HIGGSFIELD_SECRET:
        return JSONResponse({"error": "Higgsfield no configurado"}, status_code=503)

    job_id = str(_uuid.uuid4())
    _image_jobs[job_id] = {"status": "processing", "images": [], "error": None}
    asyncio.ensure_future(_run_image_job(job_id, req))
    return {"job_id": job_id}


async def _run_image_job(job_id: str, req: GenerateImagesRequest):
    """Corre en background: somete N/4 lotes en paralelo a soul/reference
    (4 imagenes por lote, limite de Higgsfield), espera a que todos
    terminen y junta los resultados en un solo job que el frontend
    consulta una sola vez -- evita que el frontend tenga que hacer
    polling de varios request_id sueltos."""
    try:
        prompt = hf.build_image_prompt(req.title, req.custom_prompt)
        n_batches = max(1, -(-req.count // 4))  # ceil(count / 4)

        request_ids = []
        for _ in range(n_batches):
            try:
                rid = await hf.generate_image(
                    prompt, image_reference_url=req.thumbnail_url, batch_size=4
                )
                request_ids.append(rid)
            except Exception as e:
                logger.warning(f"Higgsfield: un lote de imagenes fallo al someter: {e}")

        if not request_ids:
            _image_jobs[job_id] = {"status": "error", "images": [], "error": "No se pudo iniciar ninguna generación"}
            return

        collected: list = []
        for _ in range(60):  # hasta 3 min (60 x 3s)
            await asyncio.sleep(3)
            collected = []
            all_done = True
            for rid in request_ids:
                try:
                    s = await hf.get_status(rid)
                except Exception:
                    all_done = False
                    continue
                if s["status"] == "completed":
                    collected.extend(s.get("result_urls") or [])
                elif s["status"] in ("failed", "nsfw"):
                    continue  # ese lote no sirvio, se usan los demas que si salieron
                else:
                    all_done = False
            if all_done:
                break

        if not collected:
            _image_jobs[job_id] = {"status": "error", "images": [], "error": "Ninguna imagen se generó"}
            return
        _image_jobs[job_id] = {"status": "done", "images": collected[:req.count], "error": None}
    except Exception as e:
        logger.error(f"Higgsfield image job {job_id} falló: {e}")
        _image_jobs[job_id] = {"status": "error", "images": [], "error": str(e)}


@router.get("/image-job/{job_id}")
async def image_job_status(job_id: str):
    """Consulta el estado de un job de varias imagenes. Hacer polling cada 3s."""
    job = _image_jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return job
