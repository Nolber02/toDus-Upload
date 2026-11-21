"""
bot.py — Punto de entrada del bot ToDus S3.
Permite ejecutarlo de forma independiente o importarlo desde main.py.
"""
import time
import sys

from runtime import iniciar_todus_bot, detener_todus_bot

__all__ = ["iniciar_todus_bot", "detener_todus_bot"]


if __name__ == "__main__":
    # Si se ejecuta directamente (python bot.py), arrancamos el bot
    print("[INFO] Iniciando Bot ToDus de forma independiente...")
    iniciar_todus_bot()
    try:
        # Mantener el hilo principal vivo
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo Bot ToDus...")
        detener_todus_bot()
        sys.exit(0)