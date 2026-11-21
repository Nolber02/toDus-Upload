"""
handlers/commands.py — Comandos del bot.
"""
import math
import os
import time
import uuid
from datetime import datetime

from pyrogram import filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, LinkPreviewOptions,
)

from broadcast import broadcast_message
from config import (
    ADMIN_IDS, ADMIN_ID, BOT_NAME, DAILY_QUOTA_BYTES,
    TEMP_DIR, is_admin, todus,
)
from db import (
    get_all_users_info, get_bot_stats, get_effective_quota, get_history,
    get_history_entries, get_plan_total_bytes, get_top_users,
    get_user_plan, remove_user_plan, set_user_plan, parse_duration,
    parse_quota_spec, touch_user, get_all_user_ids, get_user_quota,
)
from keyboards import (
    get_main_keyboard, get_delete_list_keyboard,
)
from state import (
    DOWNLOAD_PENDING, UPLOAD_PENDING, CONFIRM_REGISTRY, USER_CHUNK_SIZES,
    MAX_USER_DOWNLOAD_JOBS, MAX_USER_UPLOAD_JOBS,
)
from utils import (
    format_remaining, format_size, make_progress_bar, _run_blocking,
)


# ─── Helpers de prefijo (todos derivan de todus.bot_prefix) ───
def _list_scope_prefix() -> str:
    """Prefijo RAIZ del bot (todos los usuarios)."""
    return todus.bot_prefix + "/"


def _admin_own_prefix() -> str:
    """Prefijo del admin."""
    return f"{todus.bot_prefix}/tg_{ADMIN_ID}/"


def _full_bot_prefix() -> str:
    """Alias del prefijo raíz."""
    return todus.bot_prefix + "/"


def register_commands(app):

    @app.on_message(filters.command("start"))
    async def start_cmd(client, message: Message):
        touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        from recovery import start_user_sync_task
        start_user_sync_task(client, message.from_user.id)
        uid = message.from_user.id

        import config as cfgmod
        if cfgmod.is_maintenance() and not is_admin(uid):
            await message.reply_text(
                "🚧 **Bot en mantenimiento**\n\n"
                "El bot se encuentra temporalmente fuera de servicio.\n"
                "Por favor, intenta más tarde.")
            return

        plan = get_user_plan(uid)
        effective = get_effective_quota(uid)

        if is_admin(uid):
            quota_line = "∞ Ilimitada (admin)"
            extra = "\n📥 Cola: sin límite (admin)\n"
        elif plan:
            remaining = plan["expires_at"] - datetime.now()
            quota_line = (f"{format_size(effective)}/día "
                          f"(plan: {plan['label']}, quedan {format_remaining(remaining)})")
            extra = f"\n📥 Cola máxima: {MAX_USER_DOWNLOAD_JOBS} descargas / {MAX_USER_UPLOAD_JOBS} subidas\n"
        else:
            quota_line = f"{format_size(effective)}/día (plan gratis)"
            extra = f"\n📥 Cola máxima: {MAX_USER_DOWNLOAD_JOBS} descargas / {MAX_USER_UPLOAD_JOBS} subidas\n"

        texto = (
            f"🚀 **Cómo usarlo en {BOT_NAME}**\n\n"
            "1️⃣ Envía un archivo (hasta 2 GB).\n"
            "2️⃣ Pega un enlace directo http(s).\n"
            "3️⃣ Usa `/yt URL` para YouTube.\n"
            "4️⃣ Soporta: YouTube, Facebook, Instagram, MediaFire, MEGA y URLs directas.\n\n"
            "⚙️ **Ajustes actuales**\n"
            f"• Host: `{todus.default_host}`\n"
            f"• Endpoint: `{todus.public_base}`\n"
            f"• Ruta base: `{todus.bot_prefix}`\n"
            f"• Cuota: {quota_line}\n"
            f"{extra}")
        await message.reply_text(texto, reply_markup=get_main_keyboard())

    @app.on_message(filters.command("help"))
    async def help_cmd(client, message: Message):
        texto = (
            "📖 **Comandos**\n"
            "/start - Menú principal\n"
            "/help - Esta ayuda\n"
            "/myID [@usuario|ID] - Ver ID propio o ajeno\n"
            "/yt <url> - Descargar de YouTube\n"
            "/historial - Ver tus archivos\n"
            "/buscar <txt> - Buscar archivo\n"
            "/delete - Eliminar tus propios archivos\n"
            "/chunk - Configurar tamaño de descarga (MB)\n"
            "/cancelar - Cancelar tareas\n"
            "/status - Estado del bot y de la nube\n")
        if is_admin(message.from_user.id):
            texto += (
                "\n👑 **Solo admin**\n"
                "/host - Ver/cambiar endpoint (stream|stickers)\n"
                "/plan <id> <GB>[/d|/t] <tiempo> - Asignar plan\n"
                "/plan <id> - Ver plan actual\n"
                "/plan <id> remove - Quitar plan\n"
                "/msg <texto> - Enviar mensaje masivo\n"
                "/users - Lista todos los usuarios\n"
                "/list_files - Escanear el servidor S3\n"
                "/delete_all - Eliminar TODOS los archivos del bot\n"
                "/list_txt - Generar .txt con todos los enlaces\n"
                "/recover - Escanear y resubir archivos eliminados de la nube\n"
                "/usedescription on|off - Nombre desde descripción\n"
                "/mantenimiento on|off - Activar/desactivar mantenimiento\n")
        await message.reply_text(texto)

    @app.on_message(filters.command("myID") | filters.command("myid"))
    async def myid_cmd(client, message: Message):
        args = message.command[1:] if len(message.command) > 1 else []
        if not args:
            u = message.from_user
            username = f"@{u.username}" if u.username else "—"
            touch_user(u.id, u.username, u.first_name)
            await message.reply_text(
                f"🆔 **Tu información**\n\n"
                f"• **ID:** `{u.id}`\n"
                f"• **Usuario:** {username}\n"
                f"• **Nombre:** {u.first_name or '—'}")
            return
        target = args[0].strip()
        if target.startswith("@"):
            try:
                user = await client.get_users(target[1:])
                display = f"@{user.username}" if user.username else "—"
                await message.reply_text(
                    f"🆔 **Información del usuario**\n\n"
                    f"• **ID:** `{user.id}`\n"
                    f"• **Usuario:** {display}\n"
                    f"• **Nombre:** {user.first_name or '—'}")
            except Exception as e:
                await message.reply_text(f"❌ No se encontró el usuario `{target}`.\n`{e}`")
            return
        if target.isdigit():
            try:
                user = await client.get_users(int(target))
                display = f"@{user.username}" if user.username else "—"
                await message.reply_text(
                    f"🆔 **Información del usuario**\n\n"
                    f"• **ID:** `{user.id}`\n"
                    f"• **Usuario:** {display}\n"
                    f"• **Nombre:** {user.first_name or '—'}")
            except Exception as e:
                await message.reply_text(f"❌ No se encontró el ID `{target}`.\n`{e}`")
            return
        await message.reply_text(
            "🔍 **Uso:**\n"
            "• `/myID` — tu propio ID\n"
            "• `/myID @usuario` — ID de otro usuario\n"
            "• `/myID 123456789` — por ID numérico")

    @app.on_message(filters.command("delete"))
    async def delete_cmd(client, message: Message):
        touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        uid = message.from_user.id

        entries = get_history_entries(uid, limit=20)
        if not entries:
            return await message.reply_text("📭 No tienes archivos para eliminar.")

        lines = [f"🗑️ **Tus archivos** (últimos {len(entries)}):\n"]
        for (hid, fname, fsize, url, udate) in entries:
            lines.append(f"• `{fname}` — {format_size(fsize)} — {udate}")
        lines.append("\n_Pulsa un botón para eliminar un archivo:_")
        texto = "\n".join(lines)

        buttons = []
        for (hid, fname, fsize, url, udate) in entries:
            short = fname if len(fname) <= 45 else fname[:42] + "…"
            buttons.append([
                InlineKeyboardButton(
                    f"🗑️ {short} ({format_size(fsize)})",
                    callback_data=f"delq_{hid}")
            ])
        buttons.append([InlineKeyboardButton("❌ Cerrar", callback_data="del_close")])

        await message.reply_text(
            texto,
            reply_markup=InlineKeyboardMarkup(buttons),
            link_preview_options=LinkPreviewOptions(is_disabled=True))

    @app.on_message(filters.command("chunk"))
    async def chunk_cmd(client, message: Message):
        args = message.command[1:] if len(message.command) > 1 else []
        uid = message.from_user.id

        if not args:
            current = USER_CHUNK_SIZES.get(uid)
            if current is None:
                actual_txt = "el valor global"
            else:
                actual_txt = f"{current // (1024 * 1024)} MB (personalizado)"

            await message.reply_text(
                "🧩 **Tamaño de tus chunks**\n\n"
                f"Actual: {actual_txt}\n\n"
                "**Uso:**\n"
                "• `/chunk 350` para usar 350 MB.\n"
                "• `/chunk predeterminado` para volver al ajuste global.\n"
                "Rango permitido: **1–4096 MB (4 GB)**."
            )
            return

        arg = args[0].strip().lower()

        if arg in ("predeterminado", "default", "reset", "global"):
            if uid in USER_CHUNK_SIZES:
                del USER_CHUNK_SIZES[uid]
                await message.reply_text("✅ **Chunk restablecido al valor global.**")
            else:
                await message.reply_text("ℹ️ Ya estabas usando el valor global.")
            return

        if not arg.isdigit():
            await message.reply_text("❌ Valor inválido. Usa un número entero (1–4096) o `predeterminado`.")
            return

        mb = int(arg)
        if mb < 1 or mb > 4096:
            await message.reply_text("❌ Fuera de rango. El valor debe estar entre **1** y **4096 MB**.")
            return

        USER_CHUNK_SIZES[uid] = mb * 1024 * 1024
        await message.reply_text(f"✅ **Chunk actualizado a {mb} MB.**\n_Se aplicará en tus próximas descargas._")

    # ─── /host ───
    @app.on_message(filters.command("host") & filters.user(list(ADMIN_IDS)))
    async def host_cmd(client, message: Message):
        import config as cfgmod
        args = message.command[1:] if len(message.command) > 1 else []

        if not args:
            await message.reply_text(
                f"🖥️ **Host activo:** `{todus.default_host}`\n"
                f"🌐 **Endpoint:** `{todus.public_base}`\n"
                f"📁 **Ruta base:** `{todus.bot_prefix}`\n\n"
                f"**Endpoints disponibles:**\n"
                f"• `stream`   → `{todus.stream_base}`\n"
                f"• `stickers` → `{todus.stickers_base}`\n\n"
                f"**Uso:**\n"
                f"• `/host stream` — sube a `{todus.stream_base}`\n"
                f"• `/host stickers` — sube a `{todus.stickers_base}`\n\n"
                f"_Los archivos nuevos y los enlaces generados usan el host activo._")
            return

        nuevo = args[0].strip().lower()
        if nuevo not in ("stream", "stickers"):
            await message.reply_text("❌ Usa `/host stream` o `/host stickers`")
            return

        cfgmod.DEFAULT_HOST = nuevo
        todus.default_host = nuevo
        await message.reply_text(
            f"✅ **Host cambiado a {nuevo}.**\n\n"
            f"🌐 **Endpoint activo:** `{todus.public_base}`\n"
            f"📁 **Ruta base:** `{todus.bot_prefix}`\n\n"
            f"_A partir de ahora las subidas y los enlaces nuevos apuntan aquí._")

    @app.on_message(filters.command("mantenimiento") & filters.user(list(ADMIN_IDS)))
    async def mantenimiento_cmd(client, message: Message):
        import config as cfgmod
        args = message.command[1:] if len(message.command) > 1 else []

        if not args:
            estado = "🟢 ON (activo)" if cfgmod.is_maintenance() else "🔴 OFF (normal)"
            await message.reply_text(
                f"🚧 **Modo mantenimiento:** {estado}\n\n"
                "**Uso:**\n"
                "• `/mantenimiento on` — Bloquea el bot para todos los usuarios (solo admin puede usarlo).\n"
                "• `/mantenimiento off` — Desbloquea el bot para todos.\n\n"
                "_Los admins siempre pueden usar el bot._")
            return

        opcion = args[0].strip().lower()
        if opcion in ("on", "activar", "1", "true"):
            cfgmod.set_maintenance(True)
            await message.reply_text(
                "✅ **Mantenimiento ACTIVADO.**\n\n"
                "🚧 Los usuarios no podrán usar el bot.\n"
                "👑 Solo los admins tienen acceso.")
        elif opcion in ("off", "desactivar", "0", "false"):
            cfgmod.set_maintenance(False)
            await message.reply_text(
                "✅ **Mantenimiento DESACTIVADO.**\n\n"
                "🟢 El bot vuelve a estar disponible para todos los usuarios.")
        else:
            await message.reply_text(
                "❌ Uso incorrecto.\n"
                "Usa `/mantenimiento on` o `/mantenimiento off`.")

    # ─── /plan ───
    @app.on_message(filters.command("plan") & filters.user(list(ADMIN_IDS)))
    async def plan_cmd(client, message: Message):
        args = message.command[1:] if len(message.command) > 1 else []
        if not args:
            await message.reply_text(
                "👑 **Comando /plan (solo admin)**\n\n"
                "**Asignar plan:**\n"
                "`/plan <id> <GB>[/d|/t] <tiempo>`\n"
                "• `<GB>/d` → cuota **diaria** (ej: `10/d` = 10 GB cada día)\n"
                "• `<GB>/t` → cuota **total** del plan repartida entre los días\n"
                "• `<GB>` (sin sufijo) → equivale a `/d`\n"
                "• `<tiempo>`: `7d` (días) o `2m` (meses de 30 días)\n\n"
                "**Ejemplos:**\n"
                "`/plan 123456789 10/d 30d` → 10 GB **diarios** por 30 días\n"
                "`/plan 123456789 50/t 2m` → 50 GB **totales** en 2 meses\n"
                "`/plan 123456789 10 7d` → 10 GB **diarios** por 7 días\n\n"
                "**Consultar plan:**\n`/plan <id>`\n\n"
                "**Quitar plan:**\n`/plan <id> remove`")
            return

        if len(args) == 1:
            target = args[0].strip()
            if not target.isdigit():
                await message.reply_text("❌ El ID debe ser numérico.")
                return
            uid = int(target)
            plan = get_user_plan(uid)
            if not plan:
                effective = get_effective_quota(uid)
                await message.reply_text(
                    f"📋 **Usuario** `{uid}` **sin plan activo.**\n"
                    f"• Cuota efectiva: {format_size(effective)}/día (gratis)")
                return
            remaining = plan["expires_at"] - datetime.now()
            effective = get_effective_quota(uid)
            total_bytes = get_plan_total_bytes(uid)
            await message.reply_text(
                f"📋 **Plan de** `{uid}`\n\n"
                f"• **Etiqueta:** {plan['label']}\n"
                f"• **Tipo:** {'diaria' if plan['period'] == 'd' else 'total'}\n"
                f"• **Cuota diaria efectiva:** {format_size(effective)}\n"
                f"• **Cuota total del plan:** {format_size(total_bytes)}\n"
                f"• **Inicio:** {plan['started_at'].strftime('%Y-%m-%d %H:%M')}\n"
                f"• **Expira:** {plan['expires_at'].strftime('%Y-%m-%d %H:%M')}\n"
                f"• **Restante:** {format_remaining(remaining)}")
            return

        if len(args) == 2 and args[1].strip().lower() == "remove":
            target = args[0].strip()
            if not target.isdigit():
                await message.reply_text("❌ El ID debe ser numérico.")
                return
            uid = int(target)
            removed = remove_user_plan(uid)
            if removed:
                await message.reply_text(
                    f"✅ Plan eliminado para `{uid}`.\n"
                    f"Ahora usa el plan gratis: {format_size(DAILY_QUOTA_BYTES)}/día.")
            else:
                await message.reply_text(f"ℹ️ El usuario `{uid}` no tenía plan asignado.")
            return

        if len(args) == 3:
            target, quota_str, dur_str = args
            target = target.strip()
            if not target.isdigit():
                await message.reply_text("❌ El ID debe ser numérico.")
                return
            uid = int(target)

            try:
                gb, period = parse_quota_spec(quota_str)
            except ValueError as e:
                await message.reply_text(f"❌ {e}")
                return

            try:
                duration = parse_duration(dur_str)
            except ValueError as e:
                await message.reply_text(f"❌ {e}")
                return

            plan = set_user_plan(uid, gb, duration, period=period)
            remaining = plan["expires_at"] - datetime.now()
            effective = get_effective_quota(uid)
            total_bytes = get_plan_total_bytes(uid)

            await message.reply_text(
                f"✅ **Plan asignado**\n\n"
                f"• **Usuario:** `{uid}`\n"
                f"• **Tipo:** {'diaria' if period == 'd' else 'total'}\n"
                f"• **Cuota diaria efectiva:** {format_size(effective)}\n"
                f"• **Cuota total del plan:** {format_size(total_bytes)}\n"
                f"• **Inicio:** {plan['started_at'].strftime('%Y-%m-%d %H:%M')}\n"
                f"• **Expira:** {plan['expires_at'].strftime('%Y-%m-%d %H:%M')}\n"
                f"• **Duración:** {format_remaining(remaining)}\n\n"
                f"_Al expirar, el usuario vuelve al plan gratis ({format_size(DAILY_QUOTA_BYTES)}/día)._")
            return

        await message.reply_text(
            "🔍 **Uso:**\n"
            "`/plan <id> <GB>[/d|/t] <tiempo>` — asignar\n"
            "`/plan <id>` — consultar\n"
            "`/plan <id> remove` — quitar\n\n"
            "Ej: `/plan 123456789 10/d 30d`")

    # ─── /msg ───
    @app.on_message(filters.command("msg") & filters.user(list(ADMIN_IDS)))
    async def msg_cmd(client, message: Message):
        if len(message.command) < 2 or not message.text.partition(" ")[2].strip():
            await message.reply_text(
                "📢 **Comando /msg (solo admin)**\n\n"
                "**Uso:**\n`/msg <contenido del mensaje>`")
            return
        contenido = message.text.split(" ", 1)[1].strip()
        if not contenido:
            await message.reply_text("❌ Debes incluir el contenido del mensaje.")
            return
        touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        total_users = len(get_all_user_ids(include_admins=False))
        if total_users == 0:
            await message.reply_text("📭 No hay usuarios registrados.")
            return
        confirm_id = uuid.uuid4().hex[:8]
        CONFIRM_REGISTRY[confirm_id] = {
            "action": "broadcast",
            "user_id": message.from_user.id,
            "text": contenido,
            "total": total_users,
        }
        preview = contenido if len(contenido) <= 500 else contenido[:500] + "…"
        await message.reply(
            f"📢 **Confirmar envío masivo**\n\n"
            f"👥 **Destinatarios:** {total_users} usuario(s)\n\n"
            f"📝 **Mensaje:**\n{preview}\n\n"
            f"⚠️ ¿Enviar a todos?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Sí, enviar", callback_data=f"by_{confirm_id}"),
                 InlineKeyboardButton("❌ Cancelar", callback_data=f"bn_{confirm_id}")]
            ]))

    # ─── /users ───
    @app.on_message(filters.command("users") & filters.user(list(ADMIN_IDS)))
    async def users_cmd(client, message: Message):
        rows = get_all_users_info()
        if not rows:
            await message.reply_text("📭 No hay usuarios registrados.")
            return
        total = len(rows)
        lines = []
        for i, (uid, username, first_name) in enumerate(rows, start=1):
            display_user = f"@{username}" if username else "—"
            name = first_name or "—"
            lines.append(f"{i}. **{name}** | {display_user} | `{uid}`")
        header = f"👥 **Usuarios registrados:** {total}\n\n"
        chunk = header
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await message.reply_text(chunk, link_preview_options=LinkPreviewOptions(is_disabled=True))
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            await message.reply_text(chunk, link_preview_options=LinkPreviewOptions(is_disabled=True))

    @app.on_message(filters.command("historial"))
    async def historial_cmd(client, message: Message):
        touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        archivos = get_history(message.from_user.id)
        if not archivos:
            return await message.reply_text("📭 Historial vacío.")
        texto = "📁 **Tus últimas subidas:**\n\n"
        for f in archivos:
            texto += f"• `{f[0]}` ({format_size(f[1])})\n  [Descargar]({f[2]})\n\n"
        await message.reply_text(texto, link_preview_options=LinkPreviewOptions(is_disabled=True))

    @app.on_message(filters.command("buscar"))
    async def buscar_cmd(client, message: Message):
        if len(message.command) < 2:
            return await message.reply_text("🔍 Uso: `/buscar nombre`")
        touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        archivos = get_history(message.from_user.id, message.command[1])
        if not archivos:
            return await message.reply_text("🔍 No encontrado.")
        texto = "🔍 **Resultados:**\n\n"
        for f in archivos:
            texto += f"• `{f[0]}` ({format_size(f[1])})\n  [Descargar]({f[2]})\n\n"
        await message.reply_text(texto, link_preview_options=LinkPreviewOptions(is_disabled=True))

    @app.on_message(filters.command("status"))
    async def status_cmd(client, message: Message):
        touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

        cloud_ok = False
        cloud_error = None
        cloud_files = 0
        cloud_bytes = 0
        t0 = time.time()
        try:
            prefix = _list_scope_prefix()
            raw = await _run_blocking(todus.s3.list, prefix=prefix) or []
            real_files = [f for f in raw if f.get("Key") and not f["Key"].endswith("/")]
            cloud_files = len(real_files)
            cloud_bytes = sum(int(f.get("Size", 0)) for f in real_files)
            cloud_ok = True
        except Exception as e:
            cloud_error = str(e)
        latency_ms = int((time.time() - t0) * 1000)

        stats = get_bot_stats()

        admin_pending = sum(1 for i in UPLOAD_PENDING.values()
                            if i.get("status") == "pending" and is_admin(i["user_id"]))
        user_pending = sum(1 for i in UPLOAD_PENDING.values()
                           if i.get("status") == "pending" and not is_admin(i["user_id"]))
        admin_dls = sum(1 for i in DOWNLOAD_PENDING.values() if is_admin(i["user_id"]))
        user_dls = sum(1 for i in DOWNLOAD_PENDING.values() if not is_admin(i["user_id"]))

        if cloud_ok:
            cloud_line = f"☁️ **Nube S3:** ✅ Accesible ({latency_ms} ms)"
        else:
            cloud_line = f"☁️ **Nube S3:** ❌ No accesible\n   └ `{cloud_error}`"

        import config as cfgmod
        mant = "🟢 ON (bloqueado)" if cfgmod.is_maintenance() else "🔴 OFF (normal)"
        texto = (
            f"📡 **Estado del bot**\n\n"
            f"🚧 **Mantenimiento:** {mant}\n"
            f"🌐 **Host activo:** `{todus.default_host}`\n"
            f"🔗 **Endpoint:** `{todus.public_base}`\n"
            f"📁 **Ruta base:** `{todus.bot_prefix}`\n"
            f"{cloud_line}\n"
            f"📦 **Archivos en el bot:** {cloud_files}\n"
            f"💾 **Tamaño total:** {format_size(cloud_bytes)}\n\n"
            f"👥 **Usuarios registrados:** {stats['users']}\n"
            f"📊 **Archivos en historial DB:** {stats['files']}\n\n"
            f"🕒 **Cola subida admin:** {admin_pending}\n"
            f"🕒 **Cola subida usuarios:** {user_pending}\n"
            f"⬇️ **Descargas admin:** {admin_dls}\n"
            f"⬇️ **Descargas usuarios:** {user_dls}")

        if is_admin(message.from_user.id):
            rows = get_top_users(limit=5)
            if rows:
                texto += "\n\n🏆 **Top 5 (GB subidos · archivos):**\n"
                for i, (uid, username, first_name, total_files, total_bytes) in enumerate(rows):
                    who = f"@{username}" if username else f"`{uid}`"
                    texto += f"{i+1}. {who} — {format_size(total_bytes)} · {total_files} 📦\n"

        await message.reply_text(texto, link_preview_options=LinkPreviewOptions(is_disabled=True))

    @app.on_message(filters.command("list_files"))
    async def list_files_cmd(client, message: Message):
        status_msg = await message.reply("⏳ **Escaneando el espacio del bot...**")
        try:
            prefix = _list_scope_prefix()
            raw_files = await _run_blocking(todus.s3.list, prefix=prefix) or []
            real_files = [f for f in raw_files if f.get("Key") and not f["Key"].endswith("/")]
            if not real_files:
                await status_msg.edit_text("📭 No hay archivos en el espacio del bot.")
                return

            texto = f"📂 **Archivos en el espacio del bot:**\n\nTotal encontrados: {len(real_files)}\n\n"
            for f in real_files[:30]:
                key = f.get("Key")
                filename = key.split("/")[-1]
                ext = os.path.splitext(filename)[1].lower().replace('.', '') or "bin"
                size = int(f.get("Size", 0))
                texto += f"📄 `{filename}`\n   └ Formato: {ext.upper()} | Tamaño: {format_size(size)}\n\n"
            if len(real_files) > 30:
                texto += f"_(Mostrando 30 de {len(real_files)} archivos)_\n"
            await status_msg.edit_text(texto, link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:
            await status_msg.edit_text(f"❌ Error al escanear el servidor: {e}")

    # ─── /delete_all ───
    @app.on_message(filters.command("delete_all") & filters.user(list(ADMIN_IDS)))
    async def delete_all_cmd(client, message: Message):
        confirm_id = uuid.uuid4().hex[:8]
        CONFIRM_REGISTRY[confirm_id] = {
            "action": "delete_all",
            "user_id": message.from_user.id,
        }
        root_prefix = _full_bot_prefix()
        await message.reply(
            "⚠️ **¿Eliminar TODOS los archivos del bot?**\n\n"
            f"Se borrará **todo** lo que esté bajo:\n`{root_prefix}`\n\n"
            "Esto incluye archivos de **TODOS los usuarios** (tg_*), no solo los tuyos.\n"
            "Se escanearán **ambos endpoints** (stickers y stream).\n\n"
            "⚠️ Esta acción NO se puede deshacer.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Sí, eliminar TODO", callback_data=f"dy_{confirm_id}"),
                 InlineKeyboardButton("❌ Cancelar", callback_data=f"dn_{confirm_id}")]
            ]))

    @app.on_message(filters.command("list_txt") & filters.user(list(ADMIN_IDS)))
    async def list_txt_cmd(client, message: Message):
        status_msg = await message.reply("⏳ **Generando lista de enlaces...**")
        txt_path = None
        try:
            admin_own_prefix = _admin_own_prefix()

            raw_files = await _run_blocking(
                todus.s3.list, prefix=admin_own_prefix) or []
            files = [
                f for f in raw_files
                if f.get("Key")
                and not f["Key"].endswith("/")
                and f["Key"].startswith(admin_own_prefix)
            ]

            if not files:
                await status_msg.edit_text(
                    f"📭 No hay archivos en `{admin_own_prefix}`.")
                return

            def _build_entries():
                out = []
                for file in files:
                    k = file.get("Key")
                    out.append({"url": todus.share_user_file(ADMIN_ID, k).url})
                return out

            entries = await _run_blocking(_build_entries)

            txt_filename = f"enlaces_admin_{int(time.time())}.txt"
            txt_path = os.path.join(TEMP_DIR, txt_filename)
            with open(txt_path, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(f"{e['url']}\n")

            await status_msg.delete()
            await message.reply_document(
                document=txt_path,
                caption=(
                    f"📄 **Lista de enlaces generada**\n"
                    f"Total: {len(files)} archivos.\n"
                    f"Host: `{todus.default_host}`\n"
                    f"Ruta: `{admin_own_prefix}`"),
                file_name=txt_filename)
        except Exception as e:
            await status_msg.edit_text(f"❌ Error al generar el archivo: {e}")
        finally:
            if txt_path and os.path.exists(txt_path):
                os.remove(txt_path)

    # ─── /recover ───  (NUEVO)
    @app.on_message(filters.command("recover") & filters.user(list(ADMIN_IDS)))
    async def recover_cmd(client, message: Message):
        from recovery import _get_history_entries_with_local, _recover_entry
        status = await message.reply("🔍 **Escaneando archivos en la nube...**")
        entries = _get_history_entries_with_local()
        total = len(entries)
        if total == 0:
            await status.edit_text("📭 No hay archivos con copia local para verificar.")
            return

        recovered = 0
        missing_local = 0
        for i, (hid, uid, fname, url, lpath) in enumerate(entries, start=1):
            try:
                if not lpath or not os.path.exists(lpath):
                    missing_local += 1
                    continue
                if await _recover_entry(client, hid, uid, fname, url, lpath):
                    recovered += 1
                    try:
                        await client.send_message(
                            uid,
                            f"♻️ **Archivo recuperado**\n"
                            f"📄 `{fname}`\n"
                            f"_Había desaparecido de la nube y fue resubido automáticamente._")
                    except Exception:
                        pass
                    await asyncio.sleep(2)
            except Exception:
                pass
            if i % 5 == 0 or i == total:
                try:
                    await status.edit_text(
                        f"🔍 **Escaneando...**\n"
                        f"📊 Revisados: {i}/{total}\n"
                        f"♻️ Recuperados: {recovered}\n"
                        f"⚠️ Sin copia local: {missing_local}")
                except Exception:
                    pass

        await status.edit_text(
            f"✅ **Escaneo terminado**\n\n"
            f"• Revisados: {total}\n"
            f"• Recuperados: {recovered}\n"
            f"• Sin copia local: {missing_local}")

    @app.on_message(filters.command("usedescription") & filters.user(list(ADMIN_IDS)))
    async def usedescription_cmd(client, message: Message):
        import config as cfgmod
        args = message.command[1:] if len(message.command) > 1 else []

        if not args:
            estado = "🟢 ON" if cfgmod.USE_DESCRIPTION else "🔴 OFF"
            await message.reply_text(
                f"⚙️ **Uso de descripción como nombre:** {estado}\n\n"
                "**Opciones:**\n"
                "• `/usedescription on` — Usa la descripción del archivo (recortada).\n"
                "• `/usedescription off` — Usa el nombre original del archivo."
            )
            return

        opcion = args[0].strip().lower()
        if opcion == "on":
            cfgmod.USE_DESCRIPTION = True
            await message.reply_text("✅ **Uso de descripción:** ACTIVADO.\nAhora los archivos se llamarán según su caption/descripción.")
        elif opcion == "off":
            cfgmod.USE_DESCRIPTION = False
            await message.reply_text("✅ **Uso de descripción:** DESACTIVADO.\nVolverán a usar el nombre original.")
        else:
            await message.reply_text("❌ Uso incorrecto. Usa `/usedescription on` o `/usedescription off`.")