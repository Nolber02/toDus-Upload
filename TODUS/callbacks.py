"""
callbacks.py — Callbacks de teclados inline.
"""
import asyncio
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime

from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions,
)

from broadcast import broadcast_message
from config import (
    ADMIN_IDS, ADMIN_ID, BOT_NAME, DAILY_QUOTA_BYTES, DB_FILE,
    LOCAL_DIR, TEMP_DIR, is_admin, todus,
)
from db import (
    delete_history_entry, delete_local_file, clear_local_files, clear_temp_files,
    get_bot_stats, get_effective_quota, get_history_entry,
    get_history, get_plan_total_bytes, get_top_users, get_user_plan,
    get_user_quota, touch_user, get_history_entry_by_filename,
)
from state import (
    CALLBACK_REGISTRY, CANCEL_EVENTS, CONFIRM_REGISTRY, DOWNLOAD_PENDING,
    UPLOAD_PENDING, _download_position, _limpiar_cancelacion, _queue_position,
    MAX_USER_DOWNLOAD_JOBS, MAX_USER_UPLOAD_JOBS,
)
from utils import (
    format_remaining, format_size, make_progress_bar, _run_blocking,
)

_TODUS_TIMEOUT = 45


# ─── Helpers de prefijo (todos derivan de todus.bot_prefix) ───
def _list_scope_prefix() -> str:
    return todus.bot_prefix + "/"


def _admin_own_prefix() -> str:
    return f"{todus.bot_prefix}/tg_{ADMIN_ID}/"


def _full_bot_prefix() -> str:
    return todus.bot_prefix + "/"


def _build_full_key(user_id: int, filename: str) -> str:
    filename = (filename or "").strip("/")
    if "/" in filename:
        return filename
    return f"{todus.bot_prefix}/tg_{user_id}/{filename}"


def _extract_owner_uid(key: str):
    """Extrae el user_id del key S3 (formato: .../tg_{uid}/...). Devuelve None si no encuentra."""
    parts = (key or "").strip("/").split("/")
    for p in parts:
        if p.startswith("tg_"):
            try:
                return int(p[3:])
            except ValueError:
                return None
    return None


class _MsgProxy:
    """Proxy que envuelve un Message pero permite override del from_user (para callbacks)."""
    def __init__(self, real_msg, from_user):
        self._real = real_msg
        self.from_user = from_user
    def __getattr__(self, name):
        return getattr(self._real, name)


async def _safe_answer(callback, text=None, show_alert=False):
    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def _todus_delete_with_timeout(user_id: int, key: str,
                                      full_key: str = None,
                                      host: str = None,
                                      timeout: int = _TODUS_TIMEOUT) -> bool:
    async def _do():
        try:
            if full_key:
                client = todus._client_for(host)
                ok = await _run_blocking(client.delete, full_key)
                if ok:
                    return True
        except Exception:
            pass
        for h in ("stickers", "stream"):
            try:
                ok = await _run_blocking(
                    todus.delete_user_file, user_id, key, host=h)
                if ok:
                    return True
            except Exception:
                continue
        return False

    try:
        return await asyncio.wait_for(_do(), timeout=timeout)
    except asyncio.TimeoutError:
        return False


def register_callbacks(app):

    @app.on_callback_query()
    async def handle_callbacks(client, callback):
        user_id = callback.from_user.id
        touch_user(user_id, callback.from_user.username, callback.from_user.first_name)
        data = callback.data

        import config as cfgmod
        if cfgmod.is_maintenance() and not is_admin(user_id):
            await _safe_answer(
                callback,
                "🚧 Bot en mantenimiento. Intenta más tarde.",
                show_alert=True)
            return

        # ─── Cuota ───
        if data == "cuota":
            used, admin_flag = get_user_quota(user_id)
            if admin_flag:
                await _safe_answer(
                    callback,
                    "👑 **Administrador**\n• Cuota: ∞ (Ilimitada)",
                    show_alert=True)
                return
            plan = get_user_plan(user_id)
            effective = get_effective_quota(user_id)
            porcentaje = (used / effective) * 100 if effective else 0

            if plan:
                remaining = plan["expires_at"] - datetime.now()
                total_bytes = get_plan_total_bytes(user_id)
                texto = (
                    f"📊 **Mi cuota (plan activo)**\n\n"
                    f"• **Plan:** {plan['label']}\n"
                    f"• **Tipo:** {'diaria' if plan['period'] == 'd' else 'total'}\n"
                    f"• **Máximo diario:** {format_size(effective)}\n"
                    f"• **Usado hoy:** {format_size(used)} / {format_size(effective)} ({porcentaje:.1f}%)\n"
                    f"• **Cuota total del plan:** {format_size(total_bytes)}\n"
                    f"• **Inicio:** {plan['started_at'].strftime('%Y-%m-%d %H:%M')}\n"
                    f"• **Expira:** {plan['expires_at'].strftime('%Y-%m-%d %H:%M')}\n"
                    f"• **Restante:** {format_remaining(remaining)}\n\n"
                    f"_La cuota diaria se renueva cada día a las 00:00._")
            else:
                texto = (
                    f"📊 **Mi cuota (plan gratis)**\n\n"
                    f"• **Máximo diario:** {format_size(effective)}\n"
                    f"• **Usado hoy:** {format_size(used)} / {format_size(effective)} ({porcentaje:.1f}%)\n\n"
                    f"_Contacta a un administrador para solicitar un plan._")
            if len(texto) <= 190:
                await _safe_answer(callback, texto, show_alert=True)
            else:
                await _safe_answer(callback)
                try:
                    await callback.message.reply_text(
                        texto,
                        link_preview_options=LinkPreviewOptions(is_disabled=True))
                except Exception:
                    pass

        # ─── Estado ───
        elif data == "estado":
            cloud_ok = False
            cloud_error = None
            cloud_files = 0
            t0 = time.time()
            try:
                prefix = _list_scope_prefix()
                raw = await asyncio.wait_for(
                    _run_blocking(todus.s3.list, prefix=prefix),
                    timeout=_TODUS_TIMEOUT) or []
                cloud_ok = True
                cloud_files = len([f for f in raw
                                   if f.get("Key") and not f["Key"].endswith("/")])
            except Exception as e:
                cloud_error = str(e)[:50]
            latency_ms = int((time.time() - t0) * 1000)
            stats = get_bot_stats()

            if cloud_ok:
                resumen = (f"📡 **Estado**\n\n"
                           f"🌐 Host: `{todus.default_host}`\n"
                           f"☁️ S3: ✅ ({latency_ms} ms)\n"
                           f"📦 Archivos: {cloud_files}\n"
                           f"👥 Usuarios: {stats['users']}")
            else:
                resumen = f"📡 **Estado**\n\n☁️ S3: ❌\n`{cloud_error}`"

            if len(resumen) <= 190:
                await _safe_answer(callback, resumen, show_alert=True)
            else:
                await _safe_answer(callback)
                try:
                    await callback.message.reply_text(
                        resumen,
                        link_preview_options=LinkPreviewOptions(is_disabled=True))
                except Exception:
                    pass

        # ─── Cola ───
        elif data == "cola":
            lines = []
            my_dls = [(jid, info) for jid, info in DOWNLOAD_PENDING.items()
                      if info["user_id"] == user_id]
            if my_dls:
                lines.append("⬇️ **Descargas:**")
                for jid, info in my_dls:
                    pos = _download_position(jid)
                    estado = "descargando ahora" if pos == 1 else f"en espera (posición {pos})"
                    lines.append(f"• `{info['filename']}` → {estado}")
            my_ups = [(jid, info) for jid, info in UPLOAD_PENDING.items()
                      if info["user_id"] == user_id]
            if my_ups:
                if lines:
                    lines.append("")
                lines.append("⬆️ **Subidas:**")
                for jid, info in my_ups:
                    pos = _queue_position(jid)
                    estado = "subiendo ahora" if info["status"] == "uploading" else f"en espera (posición {pos})"
                    lines.append(f"• `{info['filename']}` → {estado}")
            if not lines:
                await _safe_answer(
                    callback,
                    "✅ No tienes tareas en cola.",
                    show_alert=True)
                return
            texto = "📋 **Tu cola:**\n\n" + "\n".join(lines)
            if len(texto) <= 190:
                await _safe_answer(callback, texto, show_alert=True)
            else:
                await _safe_answer(callback)
                try:
                    await callback.message.reply_text(
                        texto,
                        link_preview_options=LinkPreviewOptions(is_disabled=True))
                except Exception:
                    pass

        # ─── Historial ───
        elif data == "historial":
            await _safe_answer(callback)
            archivos = get_history(user_id)
            if not archivos:
                await callback.message.reply_text("📭 Historial vacío.")
                return
            texto = "📁 **Tus últimas subidas:**\n\n"
            for f in archivos:
                texto += f"• `{f[0]}` ({format_size(f[1])})\n  [Descargar]({f[2]})\n\n"
            await callback.message.reply_text(
                texto,
                link_preview_options=LinkPreviewOptions(is_disabled=True))

        # ─── Ayuda ───
        elif data == "ayuda":
            await _safe_answer(callback)
            texto = (
                "📖 **Comandos**\n"
                "/start - Menú principal\n"
                "/help - Esta ayuda\n"
                "/myID [@usuario|ID] - Ver ID propio o ajeno\n"
                "/yt <url> - Descargar de YouTube\n"
                "/historial - Ver tus archivos\n"
                "/buscar <txt> - Buscar archivo\n"
                "/delete - Eliminar tus propios archivos\n"
                "/cancelar - Cancelar tareas\n"
                "/status - Estado del bot y de la nube\n")
            if is_admin(user_id):
                texto += (
                    "\n👑 **Solo admin**\n"
                    "/host - Ver/cambiar endpoint (stream|stickers)\n"
                    "/plan <id> <GB>[/d|/t] <tiempo> - Asignar plan\n"
                    "/plan <id> - Ver plan actual\n"
                    "/plan <id> remove - Quitar plan\n"
                    "/msg <texto> - Enviar mensaje masivo\n"
                    "/users - Lista todos los usuarios\n"
                    "/delete_all - Eliminar TODOS los archivos del bot\n"
                    "/list_txt - Generar .txt con todos los enlaces\n"
                    "/usedescription on|off - Nombre desde descripción\n"
                    "/mantenimiento on|off - Activar/desactivar mantenimiento\n")
            await callback.message.reply_text(texto)

        # ─── Top usuarios ───
        elif data == "top":
            rows = get_top_users(limit=15)
            if not rows:
                await _safe_answer(
                    callback,
                    "📭 Aún no hay archivos subidos esta semana.",
                    show_alert=True)
                return
            medals = ["🥇", "🥈", "🥉"]
            lines = ["🏆 **Ranking Semanal**\n"]
            for i, (uid, username, first_name, total_files, total_bytes) in enumerate(rows):
                if username:
                    who = f"@{username}"
                else:
                    who = f"`{uid}`"
                    if first_name:
                        who += f" ({first_name})"
                prefix = medals[i] if i < len(medals) else f"{i + 1}."
                lines.append(
                    f"{prefix} {who}\n"
                    f"   💾 {format_size(total_bytes)} · 📦 {total_files} archivo(s)"
                )
            texto = "\n".join(lines)

            if len(texto) <= 190:
                await _safe_answer(callback, texto, show_alert=True)
            else:
                await _safe_answer(callback)
                try:
                    await callback.message.reply_text(
                        texto,
                        link_preview_options=LinkPreviewOptions(is_disabled=True))
                except Exception:
                    pass

        # ─── 🔄 RESUBIR desde almacenamiento local ───
        elif data.startswith("c_"):
            short_id = data[2:]
            key = CALLBACK_REGISTRY.get(short_id)
            if not key:
                await _safe_answer(
                    callback,
                    "❌ Enlace expirado. Vuelve a subir el archivo.",
                    show_alert=True)
                return

            filename = key.strip("/").split("/")[-1]
            owner_uid = _extract_owner_uid(key)
            if owner_uid is None:
                owner_uid = user_id

            local_path = os.path.join(LOCAL_DIR, f"u{owner_uid}", filename)

            if not os.path.exists(local_path):
                await _safe_answer(
                    callback,
                    (f"❌ Archivo local no encontrado.\n\n"
                     f"`{filename}` no está en el almacenamiento local.\n"
                     f"_Súbelo de nuevo manualmente._"),
                    show_alert=True)
                return

            await _safe_answer(callback, "🔄 Resubiendo archivo...")

            # Copia temporal al dir del usuario que pulsa (add_history usa message.from_user.id)
            temp_dir = os.path.join(TEMP_DIR, f"u{user_id}")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, filename)
            try:
                shutil.copy2(local_path, temp_path)
            except Exception as e:
                try:
                    await callback.message.reply(f"❌ Error al preparar archivo: {e}")
                except Exception:
                    pass
                return

            try:
                status = callback.message
                await status.edit_text(f"🔄 **Resubiendo** `{filename}`...\n🔄 Resubidas: contando")
            except Exception:
                try:
                    status = await callback.message.reply(
                        f"🔄 **Resubiendo** `{filename}`...\n🔄 Resubidas: contando")
                except Exception:
                    status = callback.message

            try:
                from upload import process_and_upload
                proxy = _MsgProxy(callback.message, callback.from_user)
                entry = get_history_entry_by_filename(user_id, filename)
                await process_and_upload(
                    client, proxy, temp_path, filename, "resubir",
                    status_message=status,
                    history_id=entry["id"] if entry else None)
            except Exception as e:
                try:
                    await status.edit_text(f"❌ Error al resubir: {e}")
                except Exception:
                    pass

        # ─── Enlace .txt ───
        elif data.startswith("t_"):
            short_id = data[2:]
            key = CALLBACK_REGISTRY.get(short_id)
            if not key:
                await _safe_answer(
                    callback,
                    "❌ Enlace expirado.",
                    show_alert=True)
                return
            await _safe_answer(callback, "📄 Generando archivo .txt...")
            share = await _run_blocking(todus.share_user_file, ADMIN_ID, key)
            txt_filename = f"enlace_{int(time.time())}.txt"
            txt_path = os.path.join(TEMP_DIR, txt_filename)
            try:
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Enlace de descarga de ToDus:\n\n{share.url}\n")
                await callback.message.reply_document(
                    document=txt_path,
                    caption=f"📄 **Enlace de descarga:**\n`{share.original_filename}`",
                    file_name=f"{share.original_filename}.txt")
            except Exception as e:
                try:
                    await callback.message.reply(f"❌ Error al enviar el .txt: {e}")
                except Exception:
                    pass
            finally:
                if os.path.exists(txt_path):
                    os.remove(txt_path)

        # ─── /delete: pedir confirmación ───
        elif data.startswith("delq_"):
            hid_str = data[5:]
            if not hid_str.isdigit():
                await _safe_answer(callback, "❌ Entrada inválida.", show_alert=True)
                return
            hid = int(hid_str)
            entry = get_history_entry(hid)
            if not entry:
                await _safe_answer(callback, "❌ Archivo no encontrado.", show_alert=True)
                return
            if entry["user_id"] != user_id:
                await _safe_answer(
                    callback,
                    "❌ No puedes eliminar archivos de otro usuario.",
                    show_alert=True)
                return
            await _safe_answer(callback)
            try:
                await callback.message.edit_text(
                    f"⚠️ **¿Eliminar este archivo?**\n\n"
                    f"📄 `{entry['file_name']}`\n"
                    f"📦 {format_size(entry['file_size'])}\n"
                    f"📅 {entry['upload_date']}\n\n"
                    f"_Se borrará de la nube, del historial y del almacenamiento local._",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Sí, eliminar", callback_data=f"delc_{hid}"),
                         InlineKeyboardButton("❌ Cancelar", callback_data="del_close")],
                    ]))
            except Exception:
                pass

        # ─── /delete: confirmar y borrar en background ───
        elif data.startswith("delc_"):
            hid_str = data[5:]
            if not hid_str.isdigit():
                await _safe_answer(callback, "❌ Entrada inválida.", show_alert=True)
                return
            hid = int(hid_str)
            entry = get_history_entry(hid)
            if not entry:
                await _safe_answer(callback, "❌ Archivo no encontrado.", show_alert=True)
                return
            if entry["user_id"] != user_id:
                await _safe_answer(
                    callback,
                    "❌ No puedes eliminar archivos de otro usuario.",
                    show_alert=True)
                return

            await _safe_answer(callback, "🗑️ Eliminando archivo...")

            msg = callback.message
            try:
                await msg.edit_text(
                    f"⏳ **Eliminando archivo...**\n\n"
                    f"📄 `{entry['file_name']}`\n"
                    f"📦 {format_size(entry['file_size'])}\n\n"
                    f"_Esperando respuesta de ToDus..._")
            except Exception:
                pass

            async def _background_delete():
                entry_url = entry["url"].rstrip("/")
                filename = entry_url.split("/")[-1]

                inferred_host = "stickers"
                if "/stream/" in entry_url:
                    inferred_host = "stream"

                full_key = _build_full_key(entry["user_id"], filename)

                success = await _todus_delete_with_timeout(
                    entry["user_id"], filename, full_key=full_key,
                    host=inferred_host, timeout=_TODUS_TIMEOUT)

                if success:
                    # 🗑️ Borrar copia local
                    local_path = entry.get("local_path")
                    if local_path and os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                        except Exception:
                            pass
                    try:
                        delete_local_file(entry["user_id"], entry["file_name"])
                    except Exception:
                        pass

                    try:
                        delete_history_entry(hid)
                    except Exception:
                        pass
                    try:
                        await msg.edit_text(
                            f"✅ **Archivo eliminado.**\n\n"
                            f"📄 `{entry['file_name']}`\n"
                            f"📦 {format_size(entry['file_size'])}\n\n"
                            f"_Se borró de la nube y del almacenamiento local._\n"
                            f"_La cuota diaria NO se descuenta al eliminar._")
                    except Exception:
                        pass
                else:
                    try:
                        await msg.edit_text(
                            f"❌ **No se pudo eliminar el archivo.**\n\n"
                            f"📄 `{entry['file_name']}`\n\n"
                            f"_ToDus no respondió a tiempo o rechazó la operación._\n"
                            f"_Intenta de nuevo más tarde._",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔄 Reintentar", callback_data=f"delc_{hid}")],
                                [InlineKeyboardButton("❌ Cerrar", callback_data="del_close")],
                            ]))
                    except Exception:
                        pass

            asyncio.create_task(_background_delete())

        # ─── Cerrar /delete ───
        elif data == "del_close":
            await _safe_answer(callback, "Cerrado.")
            try:
                await callback.message.delete()
            except Exception:
                pass

        # ─── Eliminar archivo (flujo viejo del teclado de subida) ───
        elif data.startswith("d_"):
            short_id = data[2:]
            key = CALLBACK_REGISTRY.get(short_id)
            if not key:
                await _safe_answer(callback, "❌ Enlace expirado.", show_alert=True)
                return
            await _safe_answer(callback)
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Sí, eliminar", callback_data=f"dy_{short_id}"),
                         InlineKeyboardButton("❌ Cancelar", callback_data=f"dn_{short_id}")]
                    ]))
            except Exception:
                pass

        # ─── Confirmación de eliminación (flujo viejo) ───
        elif data.startswith("dy_"):
            short_id = data[3:]

            # Confirmación de delete_all
            if short_id in CONFIRM_REGISTRY:
                info = CONFIRM_REGISTRY.pop(short_id, None)
                if info and info.get("action") == "delete_all":
                    await _safe_answer(callback, "🗑️ Eliminando archivos del bot...")

                    msg = callback.message
                    try:
                        await msg.edit_text(
                            "⏳ **Eliminando TODOS los archivos del bot...**\n_Esto puede tardar._")
                    except Exception:
                        pass

                    async def _background_delete_all():
                        try:
                            bot_prefix = _full_bot_prefix().rstrip("/")
                            prefix_dir = bot_prefix + "/"
                            deleted_count = 0
                            failed = set()
                            rounds = 0
                            max_rounds = 10

                            # Volver a listar tras cada pasada: la lista puede estar
                            # paginada/truncada y algunos borrados pueden fallar.
                            while rounds < max_rounds:
                                rounds += 1
                                found = set()
                                list_errors = []
                                for host in ("stickers", "stream"):
                                    client = todus._client_for(host)
                                    try:
                                        raw = await asyncio.wait_for(
                                            _run_blocking(client.list, prefix=prefix_dir),
                                            timeout=_TODUS_TIMEOUT)
                                        if isinstance(raw, dict):
                                            raw = raw.get("Contents") or raw.get("contents") or []
                                        for item in raw or []:
                                            key = item.get("Key") if isinstance(item, dict) else None
                                            if key and key.startswith(prefix_dir) and not key.endswith("/"):
                                                found.add((host, key))
                                    except Exception as e:
                                        list_errors.append(f"{host}: {e}")

                                if not found:
                                    # No informar éxito si la nube no pudo listarse.
                                    if list_errors:
                                        raise RuntimeError("No se pudo listar la nube: " + "; ".join(list_errors))
                                    break

                                deleted_this_round = 0
                                failed = set()
                                for host, key in found:
                                    try:
                                        ok = await asyncio.wait_for(
                                            _run_blocking(todus._client_for(host).delete, key),
                                            timeout=_TODUS_TIMEOUT)
                                    except Exception:
                                        ok = False
                                    if ok:
                                        deleted_count += 1
                                        deleted_this_round += 1

                                try:
                                    await msg.edit_text(
                                        f"🗑️ **Eliminando archivos...**\n"
                                        f"Ronda {rounds}/{max_rounds}\n"
                                        f"✅ Eliminados: {deleted_count}\n"
                                        f"⚠️ Pendientes en esta ronda: {len(found) - deleted_this_round}")
                                except Exception:
                                    pass

                                if deleted_this_round == 0:
                                    failed = found
                                    break

                            # Comprobación final para no reportar una limpieza completa
                            # mientras queden objetos listables bajo el prefijo.
                            remaining = set(failed)
                            for host in ("stickers", "stream"):
                                try:
                                    raw = await asyncio.wait_for(
                                        _run_blocking(todus._client_for(host).list, prefix=prefix_dir),
                                        timeout=_TODUS_TIMEOUT)
                                    if isinstance(raw, dict):
                                        raw = raw.get("Contents") or raw.get("contents") or []
                                    for item in raw or []:
                                        key = item.get("Key") if isinstance(item, dict) else None
                                        if key and key.startswith(prefix_dir) and not key.endswith("/"):
                                            remaining.add((host, key))
                                except Exception:
                                    # La comprobación falló; conservar el aviso de pendientes.
                                    remaining.add((host, "<no se pudo verificar el listado>"))

                            try:
                                local_count = await _run_blocking(clear_local_files)
                            except Exception:
                                local_count = 0
                            try:
                                temp_count = await _run_blocking(clear_temp_files)
                            except Exception:
                                temp_count = 0

                            # Conservar historial como referencia si quedan objetos remotos.
                            if not remaining:
                                def _cleanup_all():
                                    conn = sqlite3.connect(DB_FILE)
                                    try:
                                        conn.execute("DELETE FROM history")
                                        conn.commit()
                                    finally:
                                        conn.close()
                                await _run_blocking(_cleanup_all)

                            texto = (
                                f"✅ **Limpieza finalizada.**\n"
                                f"• Prefijo: `{prefix_dir}`\n"
                                f"• Archivos nube eliminados: {deleted_count}\n"
                                f"• Archivos locales eliminados: {local_count}\n"
                                f"• Temporales eliminados: {temp_count}\n")
                            if remaining:
                                texto += (
                                    f"⚠️ Quedan {len(remaining)} archivo(s) o no se pudo verificar. "
                                    "El historial se conservó; vuelve a intentar /delete_all.\n")
                            else:
                                texto += "• Historial borrado (las cuotas NO se reiniciaron)."
                            try:
                                await msg.edit_text(texto)
                            except Exception:
                                pass
                        except Exception as e:
                            try:
                                await msg.edit_text(f"❌ Error al eliminar: {e}")
                            except Exception:
                                pass

                    asyncio.create_task(_background_delete_all())
                return

            # Borrado individual del flujo viejo
            key = CALLBACK_REGISTRY.get(short_id)
            if not key:
                await _safe_answer(callback, "❌ Enlace expirado.", show_alert=True)
                return

            await _safe_answer(callback, "🗑️ Eliminando archivo...")

            msg = callback.message
            full_key = key

            try:
                await msg.edit_text(
                    "⏳ **Eliminando archivo...**\n_Esperando respuesta de ToDus..._")
            except Exception:
                pass

            async def _background_delete_single():
                success = False
                for h in ("stickers", "stream"):
                    client = todus._client_for(h)
                    try:
                        if await asyncio.wait_for(
                                _run_blocking(client.delete, full_key),
                                timeout=_TODUS_TIMEOUT):
                            success = True
                            break
                    except Exception:
                        continue

                if success:
                    CALLBACK_REGISTRY.pop(short_id, None)
                    fname = full_key.split("/")[-1]

                    # 🗑️ Borrar copia local
                    try:
                        owner_uid = _extract_owner_uid(full_key)
                        if owner_uid is None:
                            owner_uid = user_id
                        delete_local_file(owner_uid, fname)
                    except Exception:
                        pass

                    def _del_history():
                        conn = sqlite3.connect(DB_FILE)
                        cur = conn.cursor()
                        cur.execute("DELETE FROM history WHERE url LIKE ?",
                                    (f"%{fname}%",))
                        conn.commit()
                        conn.close()

                    try:
                        await _run_blocking(_del_history)
                    except Exception:
                        pass
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                else:
                    try:
                        await msg.edit_text(
                            "❌ **No se pudo eliminar el archivo.**\n"
                            "_ToDus no respondió a tiempo._",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔄 Reintentar", callback_data=f"dy_{short_id}")],
                            ]))
                    except Exception:
                        pass

            asyncio.create_task(_background_delete_single())

        # ─── Cancelar borrado (flujo viejo) ───
        elif data.startswith("dn_"):
            short_id = data[3:]
            CONFIRM_REGISTRY.pop(short_id, None)
            await _safe_answer(
                callback,
                "✅ Cancelado. No se eliminó nada.",
                show_alert=True)
            key = CALLBACK_REGISTRY.get(short_id)
            if key:
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 Resubir", callback_data=f"c_{short_id}"),
                             InlineKeyboardButton("📄 Enlace.txt", callback_data=f"t_{short_id}")],
                            [InlineKeyboardButton("🗑️ Eliminar", callback_data=f"d_{short_id}")]
                        ]))
                except Exception:
                    pass

        # ─── Broadcast confirmar ───
        elif data.startswith("by_"):
            short_id = data[3:]
            info = CONFIRM_REGISTRY.pop(short_id, None)
            if not info or info.get("action") != "broadcast":
                await _safe_answer(callback, "⚠️ Confirmación expirada.", show_alert=True)
                return
            contenido = info["text"]
            total = info["total"]
            await _safe_answer(callback, "📤 Iniciando envío...")
            try:
                await callback.message.edit_text(
                    f"📤 **Enviando mensaje masivo...**\n\n"
                    f"`{make_progress_bar(0)}` 0%\n"
                    f"✅ Enviados: 0\n❌ Fallidos: 0\n🚫 Bloqueados: 0\n"
                    f"📊 Total: 0 / {total}")
            except Exception:
                pass

            stats = await broadcast_message(
                client, contenido, callback.message, include_admins=False)

            try:
                await callback.message.edit_text(
                    f"✅ **Envío masivo completado**\n\n"
                    f"👥 **Total:** {stats['total']}\n"
                    f"✅ **Enviados:** {stats['sent']}\n"
                    f"❌ **Fallidos:** {stats['failed']}\n"
                    f"🚫 **Bloqueados:** {stats['blocked']}")
            except Exception:
                pass

        elif data.startswith("bn_"):
            short_id = data[3:]
            CONFIRM_REGISTRY.pop(short_id, None)
            await _safe_answer(callback, "✅ Envío cancelado.", show_alert=True)
            try:
                await callback.message.edit_text(
                    "❌ **Envío masivo cancelado.** No se envió ningún mensaje.")
            except Exception:
                pass

        elif data.startswith("x_"):
            cancel_id = data[2:]
            ev = CANCEL_EVENTS.get(cancel_id)
            if not ev:
                await _safe_answer(
                    callback,
                    "⚠️ Esta tarea ya terminó o fue cancelada.",
                    show_alert=True)
                return
            ev.set()
            _limpiar_cancelacion(cancel_id)
            await _safe_answer(
                callback,
                "🛑 Cancelando tarea...",
                show_alert=True)

        else:
            await _safe_answer(callback, "🚧 En desarrollo.", show_alert=True)