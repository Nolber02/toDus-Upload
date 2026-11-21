"""
todus_helper.py — Wrapper de ToDus S3 para subir, compartir y borrar archivos.

Mantiene DOS clientes S3 (uno por host: stickers / stream) y expone el activo
según `self.default_host`. El comando /host alterna entre ambos y todo el bot
(subidas, URLs públicas, borrados) usa automáticamente el endpoint correcto.

Estructura de archivos (igual para ambos hosts):
    {root_prefix}/tg_{user_id}/{archivo}
"""
import os
import re
import logging
import mimetypes
from dataclasses import dataclass
from typing import Optional, Callable

try:
    from todus import S3Client
    from todus.exceptions import ToDusError
except ImportError:
    try:
        from todus.client import S3Client
        from todus.exceptions import ToDusError
    except ImportError:
        raise ImportError("No se pudo importar S3Client. Asegúrate de tener 'todus-client' instalado.")

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
        root_prefix: str = "public/todus",
    ):
        self.max_file_size_mb = max_file_size_mb
        self.default_host = (default_host or self.HOST_STICKERS).lower()
        self.namespace_prefix = (namespace_prefix or "hi").strip("/") or "hi"

        # Prefijo raíz del bot (igual para ambos hosts)
        self.bot_prefix = (root_prefix or "public/todus").strip("/")

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
        """Cliente S3 activo (según default_host). Compatibilidad con el resto del código."""
        return self._client_for(self.default_host)

    @property
    def public_base(self) -> str:
        """URL pública activa (según default_host)."""
        return self._public_base_for(self.default_host)

    # ─── Construcción de rutas ───
    def _user_prefix(self, uploader_id: int) -> str:
        return f"{self.bot_prefix}/tg_{uploader_id}/"

    def _build_key(self, host: str, owner_id: int, uploader_id: int,
                   path: str, filename: str) -> str:
        return f"{self._user_prefix(uploader_id)}{filename}"

    # ─── Subida ───
    def upload_to_user(
        self,
        owner_id: int,
        uploader_id: int,
        local_path: str,
        path: str,
        original_name: str,
        host: str = None,
        progress_callback: Callable[[int, int], None] = None,
        chunk_bytes: int = None,
    ) -> UploadResult:
        if not os.path.isfile(local_path):
            return UploadResult(success=False, error=f"Archivo local inexistente: {local_path}")

        size = os.path.getsize(local_path)
        if size == 0:
            return UploadResult(success=False, error="Archivo local vacío (0 bytes)")

        max_bytes = int(self.max_file_size_mb) * 1024 * 1024
        if size > max_bytes:
            return UploadResult(success=False,
                                error=f"Archivo muy grande: {size / 1024 / 1024:.1f} MB")

        filename = _sanitize_filename(original_name)
        key = self._build_key(host or self.default_host, owner_id, uploader_id,
                              path, filename)

        client = self._client_for(host)

        # Aplicar chunk a AMBOS clientes (por si el usuario cambia de host)
        if chunk_bytes:
            self._s3_stickers.chunk_size = chunk_bytes
            self._s3_stream.chunk_size = chunk_bytes

        try:
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            raw = client.put(
                local_path=local_path,
                key=key,
                content_type=content_type,
                filename=filename,
                show_progress=False,
            )
            if not raw.get("success", False):
                return UploadResult(success=False, key=key, size=size,
                                    error=raw.get("error") or "Error desconocido en S3")
        except Exception as e:
            return UploadResult(success=False, key=key, size=size, error=str(e))

        # Verificación de tamaño remoto
        try:
            listed = client.list(prefix=key) or []
            if isinstance(listed, dict):
                listed = listed.get("Contents") or listed.get("contents") or []
            remote = next((f for f in listed if f.get("Key") == key), None)
            remote_size = int(remote.get("Size", 0)) if remote else -1
            if remote_size != size:
                try:
                    client.delete(key)
                except Exception:
                    pass
                if remote_size < size:
                    error = (f"Tamaño remoto ({remote_size}) menor que el original "
                             f"({size}). Subida incompleta.")
                else:
                    error = f"Tamaño remoto ({remote_size}) != local ({size}). Subida interrumpida."
                return UploadResult(
                    success=False, key=key, size=size, error=error)
        except Exception as e:
            log.warning(f"No se pudo verificar tamaño remoto de {key}: {e}")

        return UploadResult(success=True, key=key, size=size, error=None)

    # ─── Compartir ───
    def share_user_file(self, owner_id: int, key: str, host: str = None) -> ShareLink:
        key = (key or "").strip("/")
        if "/" not in key:
            key = f"{self._user_prefix(owner_id)}{key}"
        elif not key.startswith(self.bot_prefix + "/"):
            key = f"{self.bot_prefix}/{key.lstrip('/')}"

        base = self._public_base_for(host)
        return ShareLink(url=f"{base}/{key}", original_filename=key.split("/")[-1], size=0)

    # ─── Eliminar ───
    def delete_user_file(self, owner_id: int, key: str, host: str = None) -> bool:
        key = (key or "").strip("/")
        if "/" not in key:
            key = f"{self._user_prefix(owner_id)}{key}"

        client = self._client_for(host)
        try:
            return bool(client.delete(key))
        except Exception as e:
            log.error(f"Error al eliminar {key}: {e}")
            return False

    # ─── Listar / URLs ───
    def list_prefix(self, prefix: str) -> list:
        try:
            raw = self.s3.list(prefix=prefix) or []
        except Exception as e:
            log.error(f"list_prefix({prefix}): {e}")
            return []
        if isinstance(raw, dict):
            raw = raw.get("Contents") or raw.get("contents") or []
        return [f for f in raw if f.get("Key") and not f["Key"].endswith("/")]

    def public_url(self, key: str, host: str = None) -> str:
        base = self._public_base_for(host)
        return f"{base}/{key.lstrip('/')}"

    def exists(self, key: str) -> bool:
        try:
            return bool(self.s3.exists(key))
        except Exception:
            return False