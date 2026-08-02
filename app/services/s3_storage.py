"""Cliente S3/MinIO (MI2) para almacenar archivos fuera del disco de Railway."""
import logging
import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_BUCKET = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")

# El bucket es privado (GET anonimo -> 403) y MINIO_PUBLIC_URL no tiene listener
# en :443 (confirmado con curl, timeout). No hay URL publica usable: toda lectura
# pasa por get_object_bytes() con las credenciales de la app.
_client = None


def is_configured() -> bool:
    return bool(
        os.environ.get("AWS_ENDPOINT_URL_S3")
        and os.environ.get("AWS_ACCESS_KEY_ID")
        and os.environ.get("AWS_SECRET_ACCESS_KEY")
        and _BUCKET
    )


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=5),
        )
    return _client


def upload_bytes(key: str, content: bytes, content_type: str = "application/octet-stream") -> None:
    """Sube bytes al bucket bajo `key`."""
    client = _get_client()
    client.put_object(Bucket=_BUCKET, Key=key, Body=content, ContentType=content_type)


def get_object_bytes(key: str) -> bytes | None:
    """Lee un objeto del bucket. Devuelve None si falla o no existe (MinIO caído, etc.)."""
    client = _get_client()
    try:
        obj = client.get_object(Bucket=_BUCKET, Key=key)
        return obj["Body"].read()
    except ClientError as e:
        logger.warning("s3_storage: fallo al leer %s: %s", key, e)
        return None
    except Exception as e:
        logger.warning("s3_storage: fallo de red al leer %s: %s", key, e)
        return None


def delete_object(key: str) -> None:
    client = _get_client()
    try:
        client.delete_object(Bucket=_BUCKET, Key=key)
    except ClientError as e:
        logger.warning("s3_storage: fallo al borrar %s: %s", key, e)
