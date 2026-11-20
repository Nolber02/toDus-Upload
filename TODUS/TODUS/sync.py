"""Comando /sync para activar o desactivar la recuperación rápida por usuario."""
import asyncio

from pyrogram import filters

from recovery import SYNC_INTERVAL_SECONDS, run_user_recovery_loop


SYNC_TASKS = {}


def register_sync_commands(app):

    @app.on_message(filters.command("sync"))
    async def sync_cmd(client, message):
        user_id = message.from_user.id
        args = message.command[1:] if len(message.command) > 1 else []

        if not args:
            task = SYNC_TASKS.get(user_id)
            active = task is not None and not task.done()
            estado = "activada" if active else "desactivada"
            await message.reply_text(
                f"🔄 **Sincronización rápida:** {estado}\n\n"
                "Uso:\n"
                "• `/sync on` — comprobar tus archivos cada 10 segundos.\n"
                "• `/sync off` — detener la comprobación rápida."
            )
            return

        opcion = args[0].strip().lower()

        if opcion in ("on", "activar", "1", "true"):
            current_task = SYNC_TASKS.get(user_id)
            if current_task is not None and not current_task.done():
                await message.reply_text(
                    "ℹ️ La sincronización rápida ya está activada "
                    f"(cada {SYNC_INTERVAL_SECONDS} segundos)."
                )
                return

            task = asyncio.create_task(
                run_user_recovery_loop(client, user_id)
            )
            SYNC_TASKS[user_id] = task
            task.add_done_callback(
                lambda completed, uid=user_id:
                    SYNC_TASKS.pop(uid, None)
                    if SYNC_TASKS.get(uid) is completed else None
            )

            await message.reply_text(
                "✅ **Sincronización rápida activada.**\n"
                f"Revisaré tus archivos cada {SYNC_INTERVAL_SECONDS} segundos "
                "y resubiré los que falten si conservan su copia local.\n"
                "Usa `/sync off` para detenerla."
            )
            return

        if opcion in ("off", "desactivar", "0", "false"):
            task = SYNC_TASKS.pop(user_id, None)
            if task is None or task.done():
                await message.reply_text(
                    "ℹ️ La sincronización rápida ya estaba desactivada."
                )
                return

            task.cancel()
            await message.reply_text(
                "✅ **Sincronización rápida desactivada.**"
            )
            return

        await message.reply_text(
            "❌ Opción inválida. Usa `/sync on` o `/sync off`."
        )