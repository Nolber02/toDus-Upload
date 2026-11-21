"""
upload.py — Subida a ToDus con cola, semáforo y watcher de posición.

Usa todus.default_host para decidir el endpoint (stickers o stream).
El comando /host cambia ese valor en caliente.
"""
import asyncio
import os
import time
import uuid
import math

from pyrogram.types import LinkPreviewOptions

from config import ADMIN_ID, todus, is_admin
from db import (
    add_user_usage, add_history, get_user_quota, get_effective_quota,
    get_user_plan, get_user_file_count, add_resupload_history,
    get_history_resupload_count,
)
from keyboards import get_cancel_keyboard, get_file_keyboard
from state import (
    ACTIVE_TASKS, UPLOAD_PENDING, UPLOAD_JOB_COUNTER, USER_CHUNK_SIZES,
    _can_enqueue_upload, _get_upload_sem_for, _queue_position,
    _registrar_cancelacion, _limpiar_cancelacion, MAX_USER_UPLOAD_JOBS,
)
from transfer import _watch_position
from utils import (
    format_size, _run_blocking, _eliminar_archivo_seguro, encode_filename,
    format_speed, make_progress_bar, format_eta
)


class _EditableStatus:
    def __init__(self, client, chat_id, message):
        self.client = client
        self.chat_id = chat_id
        self.message = message

    async def edit_text(self, text, **kwargs):
        try:
            return await self.message.edit_text(text, **kwargs)
        except Exception as error:
            reason = str(error).lower()
            if not any(marker in reason for marker in (
                "message to edit not found", "message_id_invalid",
                "messageidinvalid", "message to be edited not found",
                "message was deleted",
            )):
                raise
            self.message = await self.client.send_message(
                self.chat_id, text, **kwargs)
            return self.message


async def process_and_upload(
    client, message, local_path, original_name, plugin,
    status_message=None, history_id=None, host_override=None,
):
    global UPLOAD_JOB_COUNTER
    user_id = message.from_user.id
    is_resupload = plugin in ("resubir", "auto-recovery")

    if not os.path.exists(local_path):
        try: await message.reply("❌ Archivo local inexistente; no se sube.")
        except: pass
        return

    file_size = os.path.getsize(local_path)
    if file_size == 0:
        try: await message.reply("❌ Archivo vacío (0 bytes); no se sube.")
        except: pass
        _eliminar_archivo_seguro(local_path)
        return

    has_pending_normal_upload = any(
        info.get("user_id") == user_id and not info.get("is_resupload")
        for info in UPLOAD_PENDING.values())
    if (not is_admin(user_id) and not is_resupload
            and (get_user_file_count(user_id) >= 1 or has_pending_normal_upload)):
        try:
            await message.reply("❌ Ya tienes un archivo subido o en proceso. Elimínalo antes de subir otro.")
        except Exception:
            pass
        _eliminar_archivo_seguro(local_path)
        return

    if not _can_enqueue_upload(user_id):
        try:
            await message.reply(f"❌ Límite de cola alcanzado.\nYa tienes {MAX_USER_UPLOAD_JOBS} subidas activas.")
        except: pass
        _eliminar_archivo_seguro(local_path)
        return

    if not is_admin(user_id) and not is_resupload:
        used, _ = get_user_quota(user_id)
        effective = get_effective_quota(user_id)
        if effective > 0 and (used + file_size) > effective:
            plan = get_user_plan(user_id)
            tipo = f"plan ({plan['label']})" if plan else "gratis"
            try:
                await message.reply(f"❌ **Cuota excedida ({tipo}).**\n• Usado: {format_size(used)} / {format_size(effective)}")
            except: pass
            _eliminar_archivo_seguro(local_path)
            return

    # ─── NOMBRE ESTABLE: mismo archivo → mismo nombre → misma URL ───
    safe_name = encode_filename(original_name)
    unique_name = safe_name

    UPLOAD_JOB_COUNTER += 1
    job_id = f"job{UPLOAD_JOB_COUNTER}_{uuid.uuid4().hex[:6]}"
    cancel_id = uuid.uuid4().hex[:8]
    cancel_event = _registrar_cancelacion(cancel_id)
    UPLOAD_PENDING[job_id] = {
        "user_id": user_id, "filename": original_name, "size": file_size,
        "status": "pending", "cancel_event": cancel_event, "created_at": time.time(),
        "is_resupload": is_resupload,
    }

    status_msg = status_message or await message.reply(
        f"🕒 **En cola de subida...**\n📄 `{original_name}`\n"
        f"📦 {format_size(file_size)}\n🔢 Posición: **{_queue_position(job_id)}**",
        reply_markup=get_cancel_keyboard(cancel_id))
    status_msg = _EditableStatus(client, message.chat.id, status_msg)

    try:
        sem = _get_upload_sem_for(user_id)
        async with sem:
            if cancel_event.is_set(): raise asyncio.CancelledError()

            UPLOAD_PENDING[job_id]["status"] = "uploading"
            ACTIVE_TASKS.setdefault(user_id, []).append(original_name)

            active_host = host_override or todus.default_host

            try:
                await status_msg.edit_text(
                    f"⬆️ **Subiendo a ToDus...**\n📄 `{original_name}`\n"
                    f"📦 {format_size(file_size)}\n"
                    f"🌐 Host: `{active_host}`\n"
                    f"📁 Ruta: `{todus.bot_prefix}`")
            except: pass

            user_chunk = USER_CHUNK_SIZES.get(user_id, 4 * 1024 * 1024)

            upload_task = asyncio.create_task(_run_blocking(
                todus.upload_to_user,
                owner_id=ADMIN_ID,
                uploader_id=user_id,
                local_path=local_path,
                path="",
                original_name=unique_name,   # ← SIN uuid, nombre limpio estable
                host=active_host,
                chunk_bytes=user_chunk,
            ))

            start_time = time.time()
            estimated_speed = 2 * 1024 * 1024

            while not upload_task.done():
                if cancel_event.is_set():
                    upload_task.cancel()
                    break

                elapsed = time.time() - start_time
                estimated_uploaded = min(file_size, int(elapsed * estimated_speed))
                percent = estimated_uploaded * 100 / file_size if file_size else 0

                if percent > 95: percent = 95

                try:
                    await status_msg.edit_text(
                        f"⬆️ **Subiendo a ToDus...**\n"
                        f"📄 `{original_name}`\n"
                        f"`{make_progress_bar(percent)}` {percent:.1f}%\n"
                        f"📦 {format_size(estimated_uploaded)} / {format_size(file_size)}\n"
                        f"🌐 Host: `{active_host}`\n"
                        f"🚀 Subiendo...\n"
                        f"⏳ ETA: Calculando..."
                    )
                except: pass

                await asyncio.sleep(2)

            result = await upload_task
            retry = 0
            while (not result.success and retry < 2
                   and any(word in (result.error or "").lower()
                           for word in ("tamaño remoto", "más pequeño", "menor que", "incompleta"))):
                retry += 1
                try:
                    await status_msg.edit_text(
                        f"⚠️ La subida quedó más pequeña que el original.\n"
                        f"🔄 Reintentando ({retry}/2)...\n📄 `{original_name}`")
                except Exception:
                    pass
                result = await _run_blocking(
                    todus.upload_to_user,
                    owner_id=ADMIN_ID, uploader_id=user_id, local_path=local_path,
                    path="", original_name=unique_name, host=active_host,
                    chunk_bytes=user_chunk,
                )

            if result.success:
                share = await _run_blocking(
                    todus.share_user_file, ADMIN_ID, result.key, active_host)
                real_size = file_size
                if is_resupload:
                    add_resupload_history(
                        user_id, original_name, real_size, share.url,
                        s3_filename=unique_name, history_id=history_id)
                else:
                    add_user_usage(user_id, real_size)
                    add_history(user_id, original_name, real_size, share.url,
                                s3_filename=unique_name)

                new_used, admin_flag = get_user_quota(user_id)
                effective = get_effective_quota(user_id)
                plan = get_user_plan(user_id)

                if admin_flag: quota_text = "∞ (Ilimitada)"
                elif plan: quota_text = f"{format_size(new_used)} / {format_size(effective)} ({plan['label']})"
                else: quota_text = f"{format_size(new_used)} / {format_size(effective)} (gratis)"

                resupload_count = (get_history_resupload_count(user_id, unique_name)
                                   if is_resupload else 0)
                resupload_line = (f"🔄 **Resubidas:** {resupload_count}\n"
                                  if is_resupload else "")
                caption = (
                    f"✅ **{'RESUBIDA' if is_resupload else 'SUBIDA'} COMPLETADA**\n\n"
                    f"📄 **Archivo:**\n`{original_name}`\n"
                    f"📦 **Tamaño:** {format_size(real_size)}\n"
                    f"🌐 **Host:** `{active_host}`\n"
                    f"📁 **Ruta:** `{todus.bot_prefix}/tg_{user_id}/`\n"
                    f"📊 **Cuota hoy:** {quota_text}\n"
                    f"{resupload_line}\n"
                    f"🔗 **Enlace:**\n{share.url}\n\n"
                    f"_Copia local guardada — usa 🔄 Resubir si el archivo desaparece._"
                )
                await status_msg.edit_text(
                    caption,
                    reply_markup=get_file_keyboard(share.url, result.key),
                    link_preview_options=LinkPreviewOptions(is_disabled=True))
            else:
                await status_msg.edit_text(f"❌ Error al subir a ToDus: {result.error}")

    except asyncio.CancelledError:
        try: await status_msg.edit_text("🛑 **Subida cancelada.**")
        except: pass
    except Exception as e:
        try: await status_msg.edit_text(f"❌ Error: {str(e)}")
        except: pass
    finally:
        if user_id in ACTIVE_TASKS and original_name in ACTIVE_TASKS[user_id]:
            ACTIVE_TASKS[user_id].remove(original_name)
        _limpiar_cancelacion(cancel_id)
        UPLOAD_PENDING.pop(job_id, None)
        # Solo borra el temporal en TEMP_DIR. La copia en LOCAL_DIR la conserva add_history.
        if os.path.exists(local_path):
            try: os.remove(local_path)
            except: pass