"""
handlers/downloads.py — Recepción de archivos de Telegram y URLs (YouTube,
Facebook, Instagram, MediaFire, MEGA, URL directa).
"""
import hashlib
import os
import re
import time
import uuid

import requests
from pyrogram import filters
from pyrogram.types import Message

from config import (
    DEFAULT_HEADERS, FACEBOOK_HEADERS, INSTAGRAM_HEADERS, MEDIAFIRE_HEADERS,
    MEGA_HEADERS, TEMP_DIR, YOUTUBE_HEADERS, is_admin,
)
from db import get_effective_quota, get_user_plan, get_user_quota, touch_user
from keyboards import get_cancel_keyboard
from state import (
    MAX_USER_DOWNLOAD_JOBS, _can_enqueue_download, _download_register,
    _download_position, _download_unregister, _get_download_sem_for,
    _limpiar_cancelacion, _registrar_cancelacion,
)
from transfer import _do_download_with_queue, _watch_position
from upload import process_and_upload
from utils import (
    _eliminar_archivo_seguro, _run_blocking, _sanitize, encode_filename,
    format_eta, format_size, format_speed, make_progress_bar,
)

_TEXT_EXCLUDE_COMMANDS = [
    "start", "help", "myID", "myid", "plan", "msg", "users",
    "historial", "buscar", "status", "delete_all", "list_txt",
    "list_files", "host", "usedescription", "mantenimiento",
]

_TRANSLIT = str.maketrans({
    "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
    "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U",
    "ñ": "n", "Ñ": "N", "ü": "u", "Ü": "U",
})


# ────────────────────────────────────────────────────────────────────────────
#  Helpers de nombres ESTABLES (misma fuente → mismo nombre → misma URL)
# ────────────────────────────────────────────────────────────────────────────
def _stable_suffix(text: str, length: int = 8) -> str:
    """Hash corto y determinista. Mismo texto → mismo hash → mismo nombre."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:length]


def _unique_dest(file_name: str, user_id: int = None) -> str:
    """
    Ruta física en TEMP_DIR SIN uuid. El nombre del archivo se conserva limpio
    para que la clave S3 (que sale del `file_name`, no de esta ruta) sea estable.
    Si se pasa `user_id`, usa subcarpeta u<user_id>/ para evitar colisiones en disco.
    """
    if user_id is not None:
        d = os.path.join(TEMP_DIR, f"u{user_id}")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, file_name)
    return os.path.join(TEMP_DIR, file_name)


def _build_name_from_caption(caption: str, original_name: str) -> str:
    ext = os.path.splitext(original_name)[1] or ".bin"
    if not caption:
        return original_name

    text = re.sub(
        r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF"
        r"\U0001F1E6-\U0001F1FF\U0000FE0F\U0000200D]", " ", caption)
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s*:\s*", ": ", text)
    text = re.sub(r"[ \t]+", " ", text).strip()

    cap_num = None
    m_cap = re.search(
        r"(?i)\b(?:cap[ií]tulo|cap\.?|episodio|ep\.?|e)\s*[:\.]?\s*(\d{1,4})\b", text)
    if m_cap:
        cap_num = m_cap.group(1)

    sub_lang = None
    m_sub = re.search(
        r"(?i)\b(?:sub(?:titulad[oa]s?)?|subt[ií]tulos?)\s*[:\.]?\s*"
        r"(espa[ñn]ol|latino|castellano|ingl[eé]s|japon[eé]s|portugu[eé]s)\b", text)
    if m_sub:
        sub_lang = m_sub.group(1)
    else:
        m_sub2 = re.search(r"(?i)\b(espa[ñn]ol|latino|castellano)\b", text)
        if m_sub2:
            sub_lang = m_sub2.group(1)

    title = None
    m_split = re.search(r"(?i)\b(?:cap[ií]tulo|cap\.?|episodio|ep\.?|e\s*\d)\b", text)
    if m_split:
        title = text[:m_split.start()].strip(" -:|")
    else:
        title = text[:80].strip(" -:|")

    if title:
        title = re.sub(
            r"(?i)\b(?:audio|temporada|sub(?:titulad[oa]s?)?|subt[ií]tulos?|"
            r"idioma|calidad|resoluci[oó]n)\s*:\s*[^|]+$", "", title).strip(" -:|")
        title = re.sub(r"\s{2,}", " ", title).strip()

    parts = []
    if title:
        parts.append(title)
    if cap_num:
        parts.append(f"Cap{cap_num}")
    if sub_lang:
        parts.append(f"Sub {sub_lang}")

    name = " ".join(parts).strip() or text[:60].strip() or "archivo"
    name = name.translate(_TRANSLIT)

    safe = encode_filename(name)
    if not safe or safe == "archivo.bin":
        return original_name
    if ext and not safe.lower().endswith(ext.lower()):
        return f"{safe}{ext}"
    return safe


def register_downloads(app):

    # ─── RECIBIR ARCHIVOS DE TELEGRAM ───
    @app.on_message((filters.document | filters.video | filters.audio | filters.photo) & filters.incoming)
    async def receive_file(client, message: Message):
        user = message.from_user
        touch_user(user.id, user.username, user.first_name)

        import config as cfgmod
        if cfgmod.is_maintenance() and not is_admin(user.id):
            return await message.reply(
                "🚧 **Bot en mantenimiento**\n\n"
                "El bot se encuentra temporalmente fuera de servicio.\n"
                "Por favor, intenta más tarde.")

        file_obj = message.document or message.video or message.audio or message.photo
        original_name = getattr(file_obj, "file_name", None) or f"media_{_stable_suffix(str(message.id))}.bin"
        file_size = getattr(file_obj, "file_size", 0)

        file_name = original_name
        if cfgmod.USE_DESCRIPTION and message.caption:
            file_name = _build_name_from_caption(message.caption, original_name)

        used, admin_flag = get_user_quota(user.id)
        effective = get_effective_quota(user.id)
        if not admin_flag and (used + file_size > effective):
            plan = get_user_plan(user.id)
            tipo = "plan" if plan else "gratis"
            return await message.reply(
                f"❌ **Cuota diaria excedida ({tipo}).**\n"
                f"• Usado: {format_size(used)} / {format_size(effective)}\n"
                f"• *Contacta al administrador para solicitar un plan.*")

        if not _can_enqueue_download(user.id):
            return await message.reply(
                f"❌ **Límite de cola alcanzado.**\n"
                f"Ya tienes {MAX_USER_DOWNLOAD_JOBS} descargas activas o en espera.\n"
                f"Espera a que terminen antes de enviar otro archivo.")

        cancel_id = uuid.uuid4().hex[:8]
        cancel_event = _registrar_cancelacion(cancel_id)

        status_msg = await message.reply(
            f"⬇️ **Preparando descarga...**\n"
            f"📄 `{file_name}`\n📦 {format_size(file_size)}",
            reply_markup=get_cancel_keyboard(cancel_id))

        state = {"start": time.time(), "last_update": 0.0, "downloaded": 0}
        dest_path = None
        cancelled = False

        async def progress(current, total, *args):
            if cancel_event.is_set():
                raise __import__("asyncio").CancelledError("Descarga cancelada")
            state["downloaded"] = current
            now = time.time()
            if now - state["last_update"] < 1.2:
                return
            state["last_update"] = now
            elapsed = now - state["start"] or 0.001
            speed = current / elapsed
            percent = current * 100 / total if total else 0
            bar = make_progress_bar(percent)
            eta = (total - current) / speed if speed > 0 else 0
            try:
                await status_msg.edit_text(
                    f"⬇️ **Descargando desde Telegram...**\n"
                    f"📄 `{file_name}`\n"
                    f"`{bar}` {percent:.1f}%\n"
                    f"📦 {format_size(current)} / {format_size(total)}\n"
                    f"🚀 {format_speed(speed)}\n"
                    f"⏳ ETA: {format_eta(eta)}",
                    reply_markup=get_cancel_keyboard(cancel_id))
            except Exception:
                pass

        try:
            dest_path = _unique_dest(file_name, user_id=user.id)
            sem = _get_download_sem_for(user.id)
            dl_job = _download_register(user.id, file_name)

            def _build_pending(pos):
                return (f"🕒 **En cola de descarga...**\n📄 `{file_name}`\n"
                        f"📦 {format_size(file_size)}\n🔢 Posición: **{pos}**")

            def _build_next():
                return (f"⏳ **Eres el siguiente en descargar...**\n"
                        f"📄 `{file_name}`\n📦 {format_size(file_size)}")

            watcher_task = __import__("asyncio").create_task(_watch_position(
                job_id=dl_job, status_msg=status_msg, cancel_event=cancel_event,
                cancel_id=cancel_id, build_text_pending=_build_pending,
                build_text_next=_build_next, pos_fn=_download_position,
                job_status_fn=lambda jid: "downloading",
                get_status_trigger="downloading"))

            try:
                async with sem:
                    if cancel_event.is_set():
                        raise __import__("asyncio").CancelledError("Cancelado antes de descargar")
                    watcher_task.cancel()
                    download_path = await message.download(file_name=dest_path, progress=progress)

                real_size = os.path.getsize(download_path) if os.path.exists(download_path) else 0
                if real_size == 0:
                    raise IOError("Descarga Telegram vacía (0 bytes)")
                if file_size and real_size != file_size:
                    raise IOError(
                        f"Descarga Telegram incompleta: {real_size}/{file_size} bytes")

            finally:
                try:
                    watcher_task.cancel()
                except Exception:
                    pass
                _download_unregister(dl_job)

            if cancel_event.is_set():
                cancelled = True
                raise __import__("asyncio").CancelledError("Descarga cancelada")

            await status_msg.delete()
            await process_and_upload(client, message, download_path, file_name, "telegram")

        except __import__("asyncio").CancelledError:
            cancelled = True
            _eliminar_archivo_seguro(dest_path)
            try:
                await status_msg.edit_text("🛑 **Descarga cancelada.**\nEl archivo temporal fue eliminado.")
            except Exception:
                pass
        except Exception as e:
            _eliminar_archivo_seguro(dest_path)
            try:
                await status_msg.edit_text(f"❌ Error: {e}")
            except Exception:
                pass
        finally:
            _limpiar_cancelacion(cancel_id)
            if cancelled:
                _eliminar_archivo_seguro(dest_path)

    # ─── URLs ───
    @app.on_message(filters.text & ~filters.command(_TEXT_EXCLUDE_COMMANDS) & filters.incoming)
    async def text_handler(client, message: Message):
        user_id = message.from_user.id
        touch_user(user_id, message.from_user.username, message.from_user.first_name)

        import config as cfgmod
        if cfgmod.is_maintenance() and not is_admin(user_id):
            return await message.reply(
                "🚧 **Bot en mantenimiento**\n\n"
                "El bot se encuentra temporalmente fuera de servicio.\n"
                "Por favor, intenta más tarde.")

        text = message.text.strip()

        admin_flag = is_admin(user_id)
        used, _ = get_user_quota(user_id)
        effective = get_effective_quota(user_id)
        if not admin_flag and used >= effective:
            return await message.reply("❌ **Cuota diaria excedida.**\n*Contacta al administrador para solicitar un plan.*")

        if not _can_enqueue_download(user_id):
            return await message.reply(
                f"❌ **Límite de cola alcanzado.**\n"
                f"Ya tienes {MAX_USER_DOWNLOAD_JOBS} descargas activas o en espera.\n"
                f"Espera a que terminen antes de enviar otro enlace.")

        # ─── YouTube ───
        if "youtube.com" in text or "youtu.be" in text:
            cancel_id = uuid.uuid4().hex[:8]
            cancel_event = _registrar_cancelacion(cancel_id)
            status_msg = await message.reply(
                "🎬 **Procesando YouTube...**",
                reply_markup=get_cancel_keyboard(cancel_id))

            tmp_path = None
            try:
                r = await _run_blocking(
                    requests.get,
                    f"https://api.delirius.online/download/ytmp4?url={text}&format=360p",
                    timeout=60)
                data = r.json()
                if not data.get("status"):
                    raise Exception(data.get("message", "Error API"))

                dl_url = data["data"]["download"]
                title = (data["data"].get("title") or "").strip()
                if not title:
                    title = f"youtube_{_stable_suffix(text)}"
                safe_title = encode_filename(f"{title}.mp4")

                tmp_path = _unique_dest(safe_title, user_id=user_id)

                await _do_download_with_queue(
                    user_id=user_id, status_msg=status_msg, cancel_id=cancel_id,
                    cancel_event=cancel_event, url=dl_url, dest_path=tmp_path,
                    title=f"YouTube: {title}", headers=YOUTUBE_HEADERS,
                    label=f"YouTube: {title}")

                if cancel_event.is_set():
                    raise __import__("asyncio").CancelledError()

                await status_msg.delete()
                await process_and_upload(client, message, tmp_path, safe_title, "youtube")

            except __import__("asyncio").CancelledError:
                _eliminar_archivo_seguro(tmp_path)
                try:
                    await status_msg.edit_text("🛑 **Descarga cancelada.**")
                except Exception:
                    pass
            except Exception as e:
                _eliminar_archivo_seguro(tmp_path)
                try:
                    await status_msg.edit_text(f"❌ Error YouTube: {e}")
                except Exception:
                    pass
            finally:
                _limpiar_cancelacion(cancel_id)
            return

        # ─── Facebook ───
        if "facebook.com" in text or "fb.watch" in text or "fb.com" in text:
            cancel_id = uuid.uuid4().hex[:8]
            cancel_event = _registrar_cancelacion(cancel_id)
            status_msg = await message.reply(
                "📘 **Procesando Facebook...**",
                reply_markup=get_cancel_keyboard(cancel_id))

            tmp_path = None
            try:
                r = await _run_blocking(
                    requests.get,
                    f"https://api.delirius.online/download/facebook?url={text}",
                    timeout=60)
                data = r.json()
                if not data.get("status"):
                    raise Exception(data.get("message", "Error API Facebook"))
                lista = data.get("list") or []
                best = None
                for item in lista:
                    if item.get("url") and not item.get("shouldRender", False):
                        best = item
                        break
                if best is None:
                    for item in lista:
                        if item.get("url"):
                            best = item
                            break
                if best is None:
                    raise Exception("No se encontró un enlace de descarga válido")

                dl_url = best["url"]
                quality = best.get("quality", "?")

                safe_name = encode_filename(f"facebook_{_stable_suffix(text)}.mp4")
                tmp_path = _unique_dest(safe_name, user_id=user_id)

                await _do_download_with_queue(
                    user_id=user_id, status_msg=status_msg, cancel_id=cancel_id,
                    cancel_event=cancel_event, url=dl_url, dest_path=tmp_path,
                    title=f"Facebook ({quality})", headers=FACEBOOK_HEADERS,
                    label=f"Facebook: {safe_name}")

                if cancel_event.is_set():
                    raise __import__("asyncio").CancelledError()

                await status_msg.delete()
                await process_and_upload(client, message, tmp_path, safe_name, "facebook")

            except __import__("asyncio").CancelledError:
                _eliminar_archivo_seguro(tmp_path)
                try:
                    await status_msg.edit_text("🛑 **Descarga cancelada.**")
                except Exception:
                    pass
            except Exception as e:
                _eliminar_archivo_seguro(tmp_path)
                try:
                    await status_msg.edit_text(f"❌ Error Facebook: {e}")
                except Exception:
                    pass
            finally:
                _limpiar_cancelacion(cancel_id)
            return

        # ─── Instagram ───
        if "instagram.com" in text or "instagr.am" in text:
            cancel_id = uuid.uuid4().hex[:8]
            cancel_event = _registrar_cancelacion(cancel_id)
            status_msg = await message.reply(
                "📸 **Procesando Instagram...**",
                reply_markup=get_cancel_keyboard(cancel_id))
            try:
                r = await _run_blocking(
                    requests.get,
                    f"https://api.delirius.online/download/instagram?url={text}",
                    timeout=60)
                data = r.json()
                if not data.get("status"):
                    raise Exception(data.get("message", "Error API Instagram"))
                items = data.get("data") or []
                if not items:
                    raise Exception("No se encontraron archivos en el post")

                post_hash = _stable_suffix(text)
                total = len(items)
                subidos = 0
                errores = []

                for idx, item in enumerate(items, start=1):
                    if cancel_event.is_set():
                        raise __import__("asyncio").CancelledError()
                    item_url = item.get("url")
                    if not item_url:
                        errores.append(f"#{idx}: sin url")
                        continue
                    item_type = item.get("type", "image")
                    ext = "mp4" if item_type == "video" else "jpg"

                    unique = encode_filename(f"instagram_{post_hash}_{idx}.{ext}")

                    try:
                        await status_msg.edit_text(
                            f"📸 **Instagram ({idx}/{total})**\n⬇️ Preparando descarga...",
                            reply_markup=get_cancel_keyboard(cancel_id))
                    except Exception:
                        pass

                    tmp_path = _unique_dest(unique, user_id=user_id)
                    try:
                        await _do_download_with_queue(
                            user_id=user_id, status_msg=status_msg, cancel_id=cancel_id,
                            cancel_event=cancel_event, url=item_url, dest_path=tmp_path,
                            title=f"Instagram ({idx}/{total})", headers=INSTAGRAM_HEADERS,
                            label=f"Instagram: {unique}")

                        if cancel_event.is_set():
                            raise __import__("asyncio").CancelledError()

                        await process_and_upload(client, message, tmp_path, unique, "instagram")
                        subidos += 1
                    except __import__("asyncio").CancelledError:
                        _eliminar_archivo_seguro(tmp_path)
                        raise
                    except Exception as e:
                        errores.append(f"#{idx}: {e}")
                        _eliminar_archivo_seguro(tmp_path)

                try:
                    await status_msg.delete()
                except Exception:
                    pass
                resumen = f"✅ **Instagram completado**\n\n• Archivos subidos: {subidos}/{total}"
                if errores:
                    resumen += f"\n• Errores: {len(errores)}\n" + "\n".join(f"  - {e}" for e in errores[:5])
                await message.reply(resumen)
                if subidos == 0:
                    raise Exception("No se pudo descargar ningún archivo")

            except __import__("asyncio").CancelledError:
                try:
                    await status_msg.edit_text("🛑 **Descarga cancelada.**")
                except Exception:
                    pass
            except Exception as e:
                try:
                    await status_msg.edit_text(f"❌ Error Instagram: {e}")
                except Exception:
                    pass
            finally:
                _limpiar_cancelacion(cancel_id)
            return

        # ─── MediaFire ───
        if "mediafire.com" in text:
            cancel_id = uuid.uuid4().hex[:8]
            cancel_event = _registrar_cancelacion(cancel_id)
            status_msg = await message.reply(
                "📦 **Procesando MediaFire...**",
                reply_markup=get_cancel_keyboard(cancel_id))
            try:
                r = await _run_blocking(
                    requests.get,
                    f"https://api.delirius.online/download/mediafire?url={text}",
                    timeout=120)
                data = r.json()
                if not data.get("status"):
                    raise Exception(data.get("message", "Error API MediaFire"))

                raw = data.get("data")
                if isinstance(raw, dict):
                    items = [raw]
                elif isinstance(raw, list):
                    items = raw
                else:
                    items = []
                if not items:
                    raise Exception("No se encontraron archivos en el enlace")

                total = len(items)
                subidos = 0
                errores = []

                for idx, item in enumerate(items, start=1):
                    if cancel_event.is_set():
                        raise __import__("asyncio").CancelledError()

                    item_url = item.get("link") or item.get("url") or item.get("download")
                    filename = item.get("filename") or f"archivo_{idx}"
                    if not item_url:
                        errores.append(f"#{idx} {filename}: sin enlace")
                        continue

                    safe_name = _sanitize(filename, f"archivo_{idx}")
                    unique = encode_filename(safe_name) if total == 1 else encode_filename(f"{idx}_{safe_name}")

                    try:
                        await status_msg.edit_text(
                            f"📦 **MediaFire ({idx}/{total})**\n"
                            f"📄 `{safe_name}`\n🔍 Validando enlace...",
                            reply_markup=get_cancel_keyboard(cancel_id))
                    except Exception:
                        pass

                    try:
                        def _head():
                            h = requests.get(item_url, headers=MEDIAFIRE_HEADERS,
                                             stream=True, timeout=30, allow_redirects=True)
                            h.raise_for_status()
                            ct = h.headers.get("Content-Type", "").lower()
                            cl = int(h.headers.get("Content-Length", 0) or 0)
                            fu = h.url
                            h.close()
                            return ct, cl, fu

                        content_type, content_length, final_url = await _run_blocking(_head)
                        if "text/html" in content_type:
                            raise Exception("MediaFire devolvió HTML (enlace expirado o bloqueado)")
                        if 0 < content_length < 10240:
                            raise Exception(f"Archivo sospechosamente pequeño ({content_length} bytes)")
                    except Exception as e:
                        errores.append(f"#{idx} {safe_name}: {e}")
                        continue

                    tmp_path = _unique_dest(unique, user_id=user_id)
                    try:
                        await _do_download_with_queue(
                            user_id=user_id, status_msg=status_msg, cancel_id=cancel_id,
                            cancel_event=cancel_event, url=final_url, dest_path=tmp_path,
                            title=f"MediaFire ({idx}/{total}): {safe_name}",
                            headers=MEDIAFIRE_HEADERS, label=f"MediaFire: {safe_name}")

                        if cancel_event.is_set():
                            raise __import__("asyncio").CancelledError()

                        real_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                        if real_size < 10240:
                            raise Exception(f"Descarga inválida ({real_size} bytes, parece HTML)")

                        await process_and_upload(client, message, tmp_path, unique, "mediafire")
                        subidos += 1
                    except __import__("asyncio").CancelledError:
                        _eliminar_archivo_seguro(tmp_path)
                        raise
                    except Exception as e:
                        errores.append(f"#{idx} {safe_name}: {e}")
                        _eliminar_archivo_seguro(tmp_path)

                try:
                    await status_msg.delete()
                except Exception:
                    pass
                resumen = f"✅ **MediaFire completado**\n\n• Archivos subidos: {subidos}/{total}"
                if errores:
                    resumen += f"\n• Errores: {len(errores)}\n" + "\n".join(f"  - {e}" for e in errores[:5])
                await message.reply(resumen)
                if subidos == 0:
                    raise Exception("No se pudo descargar ningún archivo")

            except __import__("asyncio").CancelledError:
                try:
                    await status_msg.edit_text("🛑 **Descarga cancelada.**")
                except Exception:
                    pass
            except Exception as e:
                try:
                    await status_msg.edit_text(f"❌ Error MediaFire: {e}")
                except Exception:
                    pass
            finally:
                _limpiar_cancelacion(cancel_id)
            return

        # ─── MEGA ───
        if "mega.nz" in text or "mega.co.nz" in text:
            cancel_id = uuid.uuid4().hex[:8]
            cancel_event = _registrar_cancelacion(cancel_id)
            status_msg = await message.reply(
                "☁️ **Procesando MEGA...**",
                reply_markup=get_cancel_keyboard(cancel_id))
            try:
                r = await _run_blocking(
                    requests.post,
                    "https://api.delirius.online/download/mega",
                    json={"url": text}, timeout=180)
                data = r.json()
                if not data.get("status"):
                    raise Exception(data.get("message", "Error API MEGA"))

                raw = data.get("data")
                if not raw:
                    raise Exception("No se encontró información del enlace MEGA")

                if isinstance(raw, dict):
                    if "download" in raw:
                        files = [raw]
                    elif "files" in raw:
                        files = raw["files"]
                    else:
                        files = [raw]
                elif isinstance(raw, list):
                    files = raw
                else:
                    raise Exception("Formato de respuesta MEGA desconocido")

                total = len(files)
                subidos = 0
                errores = []

                for idx, item in enumerate(files, start=1):
                    if cancel_event.is_set():
                        raise __import__("asyncio").CancelledError()
                    name = item.get("name") or item.get("filename") or f"mega_{idx}"
                    safe_name = _sanitize(name, f"mega_{idx}")

                    downloads = item.get("download") or []
                    if isinstance(downloads, str):
                        downloads = [downloads]

                    dl_url = None
                    for d in downloads:
                        if isinstance(d, dict):
                            dl_url = d.get("url") or d.get("link")
                        elif isinstance(d, str):
                            dl_url = d
                        if dl_url:
                            break
                    if not dl_url:
                        dl_url = item.get("url") or item.get("link")

                    if not dl_url:
                        errores.append(f"#{idx} {safe_name}: sin enlace")
                        continue

                    unique = encode_filename(safe_name) if total == 1 else encode_filename(f"{idx}_{safe_name}")

                    try:
                        await status_msg.edit_text(
                            f"☁️ **MEGA ({idx}/{total})**\n"
                            f"📄 `{safe_name}`\n⬇️ Preparando descarga...",
                            reply_markup=get_cancel_keyboard(cancel_id))
                    except Exception:
                        pass

                    tmp_path = _unique_dest(unique, user_id=user_id)
                    try:
                        await _do_download_with_queue(
                            user_id=user_id, status_msg=status_msg, cancel_id=cancel_id,
                            cancel_event=cancel_event, url=dl_url, dest_path=tmp_path,
                            title=f"MEGA ({idx}/{total}): {safe_name}",
                            headers=MEGA_HEADERS, label=f"MEGA: {safe_name}")

                        if cancel_event.is_set():
                            raise __import__("asyncio").CancelledError()

                        real_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                        if real_size == 0:
                            raise Exception("Descarga vacía (0 bytes)")

                        await process_and_upload(client, message, tmp_path, unique, "mega")
                        subidos += 1
                    except __import__("asyncio").CancelledError:
                        _eliminar_archivo_seguro(tmp_path)
                        raise
                    except Exception as e:
                        errores.append(f"#{idx} {safe_name}: {e}")
                        _eliminar_archivo_seguro(tmp_path)

                try:
                    await status_msg.delete()
                except Exception:
                    pass
                resumen = f"✅ **MEGA completado**\n\n• Archivos subidos: {subidos}/{total}"
                if errores:
                    resumen += f"\n• Errores: {len(errores)}\n" + "\n".join(f"  - {e}" for e in errores[:5])
                await message.reply(resumen)
                if subidos == 0:
                    raise Exception("No se pudo descargar ningún archivo")

            except __import__("asyncio").CancelledError:
                try:
                    await status_msg.edit_text("🛑 **Descarga cancelada.**")
                except Exception:
                    pass
            except Exception as e:
                try:
                    await status_msg.edit_text(f"❌ Error MEGA: {e}")
                except Exception:
                    pass
            finally:
                _limpiar_cancelacion(cancel_id)
            return

        # ─── URL DIRECTA ───
        if text.startswith("http://") or text.startswith("https://"):
            cancel_id = uuid.uuid4().hex[:8]
            cancel_event = _registrar_cancelacion(cancel_id)
            status_msg = await message.reply(
                "🔗 **Procesando URL directa...**",
                reply_markup=get_cancel_keyboard(cancel_id))
            tmp_path = None
            try:
                clean = text.split("?")[0].split("#")[0]
                basename = clean.rsplit("/", 1)[-1]

                if basename and "." in basename:
                    safe_name = encode_filename(basename)
                else:
                    ext = os.path.splitext(clean)[1] or ".bin"
                    safe_name = encode_filename(f"url_{_stable_suffix(text)}{ext}")

                tmp_path = _unique_dest(safe_name, user_id=user_id)

                await _do_download_with_queue(
                    user_id=user_id, status_msg=status_msg, cancel_id=cancel_id,
                    cancel_event=cancel_event, url=text, dest_path=tmp_path,
                    title=f"URL: {safe_name}", headers=DEFAULT_HEADERS,
                    label=f"URL: {safe_name}")

                if cancel_event.is_set():
                    raise __import__("asyncio").CancelledError()

                real_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                if real_size == 0:
                    raise Exception("Descarga vacía (0 bytes)")

                await status_msg.delete()
                await process_and_upload(client, message, tmp_path, safe_name, "url")

            except __import__("asyncio").CancelledError:
                _eliminar_archivo_seguro(tmp_path)
                try:
                    await status_msg.edit_text("🛑 **Descarga cancelada.**")
                except Exception:
                    pass
            except Exception as e:
                _eliminar_archivo_seguro(tmp_path)
                try:
                    await status_msg.edit_text(f"❌ Error URL: {e}")
                except Exception:
                    pass
            finally:
                _limpiar_cancelacion(cancel_id)
            return

        await message.reply(
            "❓ No reconozco ese enlace.\n\n"
            "Soporto: YouTube, Facebook, Instagram, MediaFire, MEGA y URLs directas http(s).")