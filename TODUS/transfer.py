"""
transfer.py — Descarga HTTP con progreso, watcher de posición, cola de descargas.
"""
import asyncio
import os
import time

import requests
from pyrogram.types import Message

from config import DEFAULT_HEADERS
from state import (
    DOWNLOAD_PENDING, _download_register, _download_position,
    _download_unregister, _get_download_sem_for, _can_enqueue_download,
    MAX_USER_DOWNLOAD_JOBS, USER_CHUNK_SIZES,
)
from keyboards import get_cancel_keyboard
from utils import (
    format_size, format_speed, format_eta, make_progress_bar,
    _eliminar_archivo_seguro,
)


async def _watch_position(
    job_id,
    status_msg,
    cancel_event,
    cancel_id,
    build_text_pending,
    build_text_next,
    pos_fn,
    job_status_fn,
    get_status_trigger,
    interval: float = 1.5,
):
    last_pos = -1
    while True:
        if cancel_event.is_set():
            return
        status_now = job_status_fn(job_id)
        if status_now is None or status_now in ("uploading", "downloading"):
            return
        pos = pos_fn(job_id)
        if pos != last_pos:
            last_pos = pos
            try:
                text = build_text_next() if pos == 1 else build_text_pending(pos)
                await status_msg.edit_text(text, reply_markup=get_cancel_keyboard(cancel_id))
            except Exception:
                pass
        await asyncio.sleep(interval)


async def download_with_progress(
    message: Message,
    url: str,
    dest_path: str,
    status_msg: Message,
    title: str = "Descargando",
    headers: dict = None,
    timeout: int = 60,
    cancel_event: asyncio.Event = None,
    user_id: int = 0,  # <--- Añadido para saber el chunk del usuario
) -> str:
    loop = asyncio.get_event_loop()
    req_headers = headers or DEFAULT_HEADERS
    
    # ─── LEER CHUNK PERSONALIZADO ───
    # Si el usuario tiene un chunk configurado, lo usamos. Si no, 1MB por defecto.
    chunk_bytes = USER_CHUNK_SIZES.get(user_id, 1024 * 1024)

    def _download_sync():
        with requests.get(url, stream=True, timeout=(10, 120),
                          headers=req_headers, allow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            downloaded = 0
            start_time = time.time()
            last_update = 0.0

            with open(dest_path, "wb") as f:
                # Usar chunk_bytes
                for chunk in r.iter_content(chunk_size=chunk_bytes):
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError()
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_update < 1.2:
                        continue
                    last_update = now
                    elapsed = now - start_time or 0.001
                    speed = downloaded / elapsed
                    if total > 0:
                        percent = downloaded * 100 / total
                        text = (f"⬇️ **{title}**\n"
                                f"`{make_progress_bar(percent)}` {percent:.1f}%\n"
                                f"📦 {format_size(downloaded)} / {format_size(total)}\n"
                                f"🚀 {format_speed(speed)}\n"
                                f"⏳ ETA: {format_eta((total - downloaded) / speed if speed else 0)}")
                    else:
                        text = (f"⬇️ **{title}**\n"
                                f"📦 {format_size(downloaded)}\n"
                                f"🚀 {format_speed(speed)}")
                    try:
                        asyncio.run_coroutine_threadsafe(status_msg.edit_text(text), loop)
                    except Exception:
                        pass

            # ── VALIDACIÓN DE INTEGRIDAD ──
            ce = (r.headers.get("Content-Encoding", "identity") or "identity").lower()
            if ce not in ("identity", ""):
                raise IOError(f"Servidor devolvió Content-Encoding={ce}; no se puede garantizar integridad")

        actual = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        if actual == 0:
            raise IOError("Descarga vacía (0 bytes)")
        if total > 0 and actual != total:
            raise IOError(
                f"Descarga incompleta: {actual}/{total} bytes "
                f"({actual * 100 / total:.1f}%)"
            )

    try:
        await loop.run_in_executor(None, _download_sync)
    except BaseException:
        _eliminar_archivo_seguro(dest_path)
        raise
    return dest_path


async def _do_download_with_queue(
    user_id: int,
    status_msg: Message,
    cancel_id: str,
    cancel_event: asyncio.Event,
    url: str,
    dest_path: str,
    title: str,
    headers: dict,
    label: str,
    timeout: int = 60,
) -> str:
    if not _can_enqueue_download(user_id):
        raise RuntimeError(f"Límite de {MAX_USER_DOWNLOAD_JOBS} descargas alcanzado")

    sem = _get_download_sem_for(user_id)
    dl_job = _download_register(user_id, label)

    def _build_pending(pos):
        return f"🕒 **En cola de descarga...**\n🎬 `{label}`\n🔢 Posición: **{pos}**"

    def _build_next():
        return f"⏳ **Eres el siguiente en descargar...**\n🎬 `{label}`"

    watcher_task = asyncio.create_task(_watch_position(
        job_id=dl_job, status_msg=status_msg, cancel_event=cancel_event,
        cancel_id=cancel_id, build_text_pending=_build_pending,
        build_text_next=_build_next, pos_fn=_download_position,
        job_status_fn=lambda jid: "downloading" if jid in DOWNLOAD_PENDING else None,
        get_status_trigger="downloading"))

    try:
        async with sem:
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            watcher_task.cancel()
            await download_with_progress(
                message=status_msg, url=url, dest_path=dest_path,
                status_msg=status_msg, title=title, cancel_event=cancel_event,
                headers=headers, timeout=timeout, user_id=user_id) # Pasamos user_id
        return dest_path
    finally:
        try:
            watcher_task.cancel()
        except Exception:
            pass
        _download_unregister(dl_job)