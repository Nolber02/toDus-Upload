"""
runtime.py — Arranque/parada del bot en un hilo separado.
"""
import asyncio
import threading
import time

from pyrogram import Client

from config import API_ID, API_HASH, BOT_TOKEN
from commands import register_commands
from downloads import register_downloads
from callbacks import register_callbacks
from recovery import run_recovery_loop


_app_instance = None


def iniciar_todus_bot():
    print("[INICIANDO] Bot de ToDus S3 (Kurigram) en hilo separado...")

    def run_bot():
        global _app_instance
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            _app_instance = Client(
                "todus_bot_session",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=BOT_TOKEN,
                workers=8,
                sleep_threshold=5,
                max_concurrent_transmissions=8,
            )
            register_commands(_app_instance)
            register_downloads(_app_instance)
            register_callbacks(_app_instance)

            async def start_bot():
                await _app_instance.start()
                print("[OK] Bot de ToDus conectado y escuchando.")

                # ♻️ Auto-recuperación: loop de fondo que revisa la nube
                # y resube lo que falte desde la copia local.
                asyncio.create_task(run_recovery_loop(_app_instance))
                print("[OK] Loop de auto-recuperación iniciado.")

                await asyncio.Event().wait()

            loop.run_until_complete(start_bot())

        except Exception as e:
            print(f"[ERROR] Fallo en el hilo del bot de ToDus: {e}")
        finally:
            if _app_instance and _app_instance.is_connected:
                try:
                    loop.run_until_complete(_app_instance.stop())
                except Exception:
                    pass
            loop.close()

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    time.sleep(2)
    print("[OK] Bot de ToDus S3 lanzado en segundo plano.")
    return _app_instance


def detener_todus_bot():
    global _app_instance
    print("[DETENIENDO] Bot de ToDus S3...")
    if _app_instance is None:
        print("[WARN] No hay instancia activa que detener.")
        return
    try:
        _app_instance.stop()
        print("[OK] Bot de ToDus S3 detenido.")
    except Exception as e:
        print(f"[ERROR] Al detener el bot de ToDus: {e}")