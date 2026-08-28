"""Replicación de tokens.db (Railway -> Coolify) vía snapshots periódicos a S3.

Railway (DB_REPLICA_ROLE=primary) sube un snapshot consistente cada
DB_SNAPSHOT_INTERVAL_MIN minutos. Coolify (DB_REPLICA_ROLE=standby) descarga
el más reciente y reemplaza su copia local -- mismo patrón ya probado en el
incidente de corrupción del 2026-08-27 (PRAGMA quick_check antes de
os.replace() atómico, nunca escritura directa sobre el archivo vivo del
proceso -- eso fue lo que causó la corrupción original).

Decisión explícita de Jovan (2026-08-27): Coolify hoy no se usa como app
viva, solo como failover de código -- aceptable que cada pull DESCARTE
cualquier escritura local hecha en Coolify desde el último ciclo (sesión,
auditoría, etc.). Por eso el lado standby borra su copia local ANTES de
verificar la nueva (mismo trade-off ya usado en /api/diag/upload-recovered-db
cuando no hay espacio para tener ambas copias a la vez) -- el snapshot en S3
que se está descargando ya fue verificado byte a byte al subirse.
"""
import logging
import os
import sqlite3
import time
from pathlib import Path

from app.config import DATABASE_PATH
from app.services import s3_storage

logger = logging.getLogger(__name__)

_SNAPSHOT_PREFIX = "db_replication/tokens.db."

_status = {
    "last_push_at": 0.0,
    "last_push_ok": None,
    "last_push_error": "",
    "last_push_key": "",
    "last_pull_at": 0.0,
    "last_pull_ok": None,
    "last_pull_error": "",
    "last_pull_key": "",
}


def _slow_s3_client():
    """Cliente S3 con timeouts largos -- tokens.db puede ser 400MB+, el
    cliente default de s3_storage.py (5s/5s) está pensado para adjuntos
    chicos (ver /api/diag/backup-raw-db-to-s3, mismo patrón)."""
    import boto3
    from botocore.client import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4", connect_timeout=30, read_timeout=300),
    )


def _list_snapshot_keys(client, bucket: str) -> list:
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=_SNAPSHOT_PREFIX):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    # El timestamp unix va al final del key -- orden alfabético == orden cronológico
    keys.sort()
    return keys


def push_snapshot_sync(keep_last: int = 6) -> dict:
    """Genera un snapshot consistente (sqlite3 .serialize(), en memoria --
    nunca toca disco local, evita el riesgo de espacio que causó el
    incidente de hoy) y lo sube a S3. Bloqueante: llamar vía asyncio.to_thread."""
    if not s3_storage.is_configured():
        result = {"ok": False, "error": "S3 no configurado"}
        _status["last_push_at"] = time.time()
        _status["last_push_ok"] = False
        _status["last_push_error"] = result["error"]
        return result
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        try:
            data = conn.serialize()
        finally:
            conn.close()

        ts = int(time.time())
        key = f"{_SNAPSHOT_PREFIX}{ts}"
        bucket = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
        client = _slow_s3_client()
        client.put_object(Bucket=bucket, Key=key, Body=bytes(data), ContentType="application/x-sqlite3")
        verify = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if len(verify) != len(data):
            result = {"ok": False, "error": "verificación byte a byte falló tras subir", "key": key}
            _status["last_push_at"] = time.time()
            _status["last_push_ok"] = False
            _status["last_push_error"] = result["error"]
            return result

        if keep_last > 0:
            keys = _list_snapshot_keys(client, bucket)
            for old_key in keys[:-keep_last]:
                try:
                    client.delete_object(Bucket=bucket, Key=old_key)
                except Exception as e:
                    logger.warning("db_replication: no se pudo podar %s: %s", old_key, e)

        result = {"ok": True, "key": key, "bytes": len(data)}
        _status["last_push_at"] = time.time()
        _status["last_push_ok"] = True
        _status["last_push_error"] = ""
        _status["last_push_key"] = key
        return result
    except Exception as e:
        result = {"ok": False, "error": str(e), "type": type(e).__name__}
        _status["last_push_at"] = time.time()
        _status["last_push_ok"] = False
        _status["last_push_error"] = str(e)
        return result


def pull_latest_and_replace_sync() -> dict:
    """Descarga el snapshot más reciente de S3 y reemplaza tokens.db local.
    Bloqueante: llamar vía asyncio.to_thread.

    Mismo patrón que /api/diag/upload-recovered-db: borra la copia local
    vieja (incluyendo -wal/-shm/-journal) ANTES de escribir la nueva, verifica
    con PRAGMA quick_check, y solo entonces promueve con os.replace()
    (atómico en el mismo filesystem). El snapshot que se descarga ya fue
    verificado byte a byte al subirse en push_snapshot_sync -- no hace falta
    tener ambas copias a la vez en disco para confiar en el resultado."""
    if not s3_storage.is_configured():
        result = {"ok": False, "error": "S3 no configurado"}
        _status["last_pull_at"] = time.time()
        _status["last_pull_ok"] = False
        _status["last_pull_error"] = result["error"]
        return result
    try:
        bucket = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
        client = _slow_s3_client()
        keys = _list_snapshot_keys(client, bucket)
        if not keys:
            result = {"ok": False, "error": "no hay snapshots en S3 todavía"}
            _status["last_pull_at"] = time.time()
            _status["last_pull_ok"] = False
            _status["last_pull_error"] = result["error"]
            return result
        key = keys[-1]
        data = client.get_object(Bucket=bucket, Key=key)["Body"].read()

        db_path = Path(DATABASE_PATH)
        tmp_path = db_path.with_suffix(".db.replica_incoming")
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        tmp_path.write_bytes(data)

        conn = sqlite3.connect(str(tmp_path))
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if check != ("ok",):
            tmp_path.unlink(missing_ok=True)
            result = {"ok": False, "error": f"quick_check falló en snapshot descargado: {check}", "key": key}
            _status["last_pull_at"] = time.time()
            _status["last_pull_ok"] = False
            _status["last_pull_error"] = result["error"]
            return result

        os.replace(str(tmp_path), str(db_path))
        result = {"ok": True, "key": key, "bytes": len(data)}
        _status["last_pull_at"] = time.time()
        _status["last_pull_ok"] = True
        _status["last_pull_error"] = ""
        _status["last_pull_key"] = key
        return result
    except Exception as e:
        result = {"ok": False, "error": str(e), "type": type(e).__name__}
        _status["last_pull_at"] = time.time()
        _status["last_pull_ok"] = False
        _status["last_pull_error"] = str(e)
        return result


def get_status() -> dict:
    return dict(_status)
