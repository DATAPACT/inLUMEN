import asyncio
from typing import Optional

from minio import Minio

from runtime_config import get_minio_settings


MINIO_CLIENT: Optional[Minio] = None


def get_minio_client() -> Minio:
    """Lazily load a MinIO client using local config."""
    global MINIO_CLIENT
    if MINIO_CLIENT is not None:
        return MINIO_CLIENT
    endpoint, access_key, secret_key, secure = get_minio_settings()
    MINIO_CLIENT = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )
    return MINIO_CLIENT


async def read_minio_object(bucket_name: str, object_name: str) -> str:
    """Return the text content of a MinIO object."""
    def _sync_read() -> str:
        client = get_minio_client()
        response = client.get_object(bucket_name, object_name)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
        return data.decode("utf-8", errors="ignore")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_read)


async def read_minio_object_bytes(bucket_name: str, object_name: str) -> bytes:
    """Return the raw content of a MinIO object."""
    def _sync_read() -> bytes:
        client = get_minio_client()
        response = client.get_object(bucket_name, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_read)
