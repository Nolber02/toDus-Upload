"""
todus_hls_helper.py — Wrapper de ToDus S3 SOLO para el proyecto HLS.

⚠️ NO confundir con TODUS/todus_helper.py (ese es del bot ToDus S3 Cloud).

Diferencia clave:
  - Este archivo usa hls_prefix (ej: "public/hls")
  - El de TODUS/ usa root_prefix / namespace_prefix

Estructura de archivos que genera:
    {hls_prefix}/<video>/master.m3u8
    {hls_prefix}/<video>/<video>_<calidad>/playlist.m3u8
    {hls_prefix}/<video>/<video>_<calidad>/segmentNNN.ts
"""
import os
import re
import logging
from dataclasses import dataclass
from typing import Optional

from todus import S3Client
from todus.exceptions import ToDusError, ToDusNotFoundError, ToDusServerError

log = logging.getLogger(__name__)
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

DEFAULT_STICKERS_URL = "https://stickers.todus.cu"
DEFAULT_STREAM_URL = "https://s3.todus.cu/stream"


def _sanitize_filename(name: str, fallback: str = "archivo.bin") -> str:
    name = _INVALID_FILENAME_CHARS.sub("_", name or "").strip()
    return name or fallback


@dataclass
class UploadResult:
    success: bool
    key: str = ""
    size: int = 0
    error: Optional[str] = None


@dataclass
class ShareLink:
    url: str
    original_filename: str
    size: int = 0


class ToDusHelper:
    HOST_STREAM = "stream"
    HOST_STICKERS = "stickers"

    def __init__(
        self,
        namespace_prefix: str = "hi",
        max_file_size_mb: int = 2048,
        default_host: str = HOST_STICKERS,
        stickers_base_url: str = DEFAULT_STICKERS_URL,
        stream_base_url: str = DEFAULT_STREAM_URL,
        hls_prefix: str = "public/hls",
    ):
        self.max_file_size_mb = max_file_size_mb
        self.default_host = (default_host or self.HOST_STICKERS).lower()
        self.namespace_prefix = (namespace_prefix or "hi").strip("/") or "hi"

        # Prefijo raíz del HLS dentro del bucket
        self.hls_prefix = (hls_prefix or "public/hls").strip("/")

        # URLs de cada endpoint
        self.stickers_base = (stickers_base_url or DEFAULT_STICKERS_URL).rstrip("/")
        self.stream_base = (stream_base_url or DEFAULT_STREAM_URL).rstrip("/")

        # Un cliente por endpoint
        self._s3_stickers = S3Client(base_url=self.stickers_base)
        self._s3_stream = S3Client(base_url=self.stream_base)

    # ─── Selección de cliente / URL según host ───
    def _client_for(self, host: str = None):
        h = (host or self.default_host).lower()
        return self._s3_stream if h == self.HOST_STREAM else self._s3_stickers

    def _public_base_for(self, host: str = None) -> str:
        h = (host or self.default_host).lower()
        return self.stream_base if h == self.HOST_STREAM else self.stickers_base

    @property
    def s3(self):
        """Cliente S3 activo (según default_host)."""
        return self._client_for(self.default_host)

    @property
    def public_base(self) -> str:
        """URL pública activa (según default_host)."""
        return self._public_base_for(self.default_host)

    # ─── Construcción de keys ───
    def build_key(self, path: str, filename: str) -> str:
        """
        Construye la key completa usando self.hls_prefix.
        Limpia prefijos legacy ("hls/", "public/") si vienen.
        """
        p = (path or "").strip("/")
        if p.startswith("hls/"):
            p = p[4:]
        elif p == "hls":
            p = ""
        if p.startswith("public/"):
            p = p[len("public/"):]
        parts = [self.hls_prefix]
        if p:
            parts.append(p)
        return "/".join(parts) + "/" + filename

    # ─── Upload ───
    def upload_to_user(
        self,
        owner_id: int,
        uploader_id: int,
        local_path: str,
        path: str,
        original_name: str,
        host: str = None,
        chunk_bytes: int = None,   # aceptado por compatibilidad, ignorado
    ) -> UploadResult:
        if not os.path.isfile(local_path):
            return UploadResult(success=False, error=f"Archivo local inexistente: {local_path}")

        size = os.path.getsize(local_path)
        max_bytes = int(self.max_file_size_mb) * 1024 * 1024
        if size > max_bytes:
            return UploadResult(
                success=False,
                error=f"Archivo muy grande: {size / 1024 / 1024:.1f} MB",
            )

        filename = _sanitize_filename(original_name)
        key = self.build_key(path, filename)
        client = self._client_for(host)

        try:
            raw = client.put(
                local_path=local_path,
                key=key,
                show_progress=False,
            )
            ok = bool(raw.get("success", False)) if isinstance(raw, dict) else bool(raw)
            err = raw.get("error") if isinstance(raw, dict) and not ok else None
        except Exception as e:
            return UploadResult(success=False, key=key, size=size, error=str(e))

        return UploadResult(success=ok, key=key, size=size, error=None if ok else err)

    # ─── Share ───
    def share_user_file(self, owner_id: int, key: str, host: str = None) -> ShareLink:
        key = (key or "").strip("/")
        base = self._public_base_for(host)
        return ShareLink(url=f"{base}/{key}", original_filename=key.split("/")[-1], size=0)

    # ─── Delete ───
    def delete_user_file(self, owner_id: int, key: str, host: str = None) -> bool:
        key = (key or "").strip("/")
        client = self._client_for(host)
        try:
            return bool(client.delete(key))
        except Exception as e:
            log.error(f"Error al eliminar {key}: {e}")
            return False

    # ─── List ───
    def list_prefix(self, prefix: str, host: str = None,
                    raise_on_error: bool = False) -> list:
        client = self._client_for(host)
        try:
            raw = client.list(prefix=prefix) or []
        except Exception as e:
            log.error(f"list_prefix({prefix}): {e}")
            if raise_on_error:
                raise
            return []
        if isinstance(raw, dict):
            raw = raw.get("Contents") or raw.get("contents") or []
        return [f for f in raw if f.get("Key") and not f["Key"].endswith("/")]

    # ─── Helpers ───
    def public_url(self, key: str, host: str = None) -> str:
        base = self._public_base_for(host)
        return f"{base}/{key.lstrip('/')}"

    def exists(self, key: str, host: str = None) -> bool:
        client = self._client_for(host)
        try:
            return bool(client.exists(key))
        except Exception:
            return False