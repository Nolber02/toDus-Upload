"""
utils.py — Formateo, barras de progreso, helpers varios.
"""
import asyncio
import math
import os
import re
from datetime import timedelta
from urllib.parse import quote


def encode_filename(name: str) -> str:
    """
    Limpia el nombre preservando siempre la extensión.
    Solo deja letras, números, guiones, guiones bajos y puntos.
    """
    if not name:
        return "archivo.bin"

    # separar extensión del nombre base
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""

    def _clean(s: str) -> str:
        # 1. Quitar emojis y caracteres de control
        s = re.sub(
            r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF"
            r"\U0001F1E6-\U0001F1FF\U0000FE0F\U0000200D\x00-\x1F\x7F]",
            "", s)
        # 2. Reemplazar espacios por guiones bajos
        s = s.replace(" ", "_")
        # 3. Quedarse SOLO con letras, números, guiones, guiones bajos y puntos
        s = re.sub(r"[^a-zA-Z0-9\._\-]", "", s)
        # 4. Evitar múltiples guiones/puntos seguidos
        s = re.sub(r"_+", "_", s)
        s = re.sub(r"-+", "-", s)
        return s.strip("._-")

    base = _clean(base) or "archivo"
    ext = _clean(ext)
    return f"{base}.{ext}" if ext else base


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.2f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    if bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec / 1024 / 1024:.2f} MB/s"


def format_eta(seconds: float) -> str:
    if seconds < 0 or seconds == float("inf") or math.isnan(seconds):
        return "--:--"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def format_remaining(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total <= 0:
        return "expirado"
    d = total // 86400
    h = (total % 86400) // 3600
    m = (total % 3600) // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m and not d:
        parts.append(f"{m}m")
    return " ".join(parts) or f"{total}s"


def make_progress_bar(percent: float, length: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(length * percent / 100)
    return "▰" * filled + "▱" * (length - filled)


def _eliminar_archivo_seguro(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _sanitize(name: str, fallback: str = "archivo.bin") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip()
    return name or fallback


async def _run_blocking(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))