# -*- coding: utf-8 -*-
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PORT = 24716
SERVER_URL = f"http://127.0.0.1:{PORT}"
TELEGRAM_BOT_TOKEN = os.environ.get("HLS_BOT_TOKEN")
TELEGRAM_OWNER_ID = os.environ.get("HLS_OWNER_ID")

def enviar_url_telegram(url):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_OWNER_ID:
        print("[WARN] Faltan HLS_BOT_TOKEN o HLS_OWNER_ID.", flush=True)
        return

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    texto = (
        "🚀 **Servidor HLS Iniciado**\n\n"
        f"🔗 [Abrir Reproductor]({url}/player)\n\n"
        f"🌐 URL Base: `{url}`"
    )
    
    payload = {
        "chat_id": TELEGRAM_OWNER_ID, 
        "text": texto, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(api_url, json=payload, timeout=10)
        response.raise_for_status()
        print("[TELEGRAM] [OK] URL enviada exitosamente.", flush=True)
    except Exception as e:
        print(f"[TELEGRAM] [ERROR] Error al enviar: {e}", flush=True)

def esperar_servidor(server_process, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_process.poll() is not None:
            raise RuntimeError("El servidor web termino inesperadamente.")
        try:
            if requests.get(f"{SERVER_URL}/ping", timeout=2).ok:
                print(f"[HLS] [OK] Servidor local listo en el puerto {PORT}.", flush=True)
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("El servidor web no respondio a tiempo.")

def iniciar_flaregui():
    print("[FlareGUI] Iniciando tunel...", flush=True)
    log_file = "flaregui_output.log"
    
    with open(log_file, "w") as f:
        process = subprocess.Popen(
            f"npx -y flaregui --port {PORT}", 
            shell=True, 
            stdout=f, 
            stderr=subprocess.STDOUT, 
            text=True
        )
    
    url = None
    for _ in range(45):
        time.sleep(1)
        if process.poll() is not None:
            print(f"[FlareGUI] [WARN] El proceso termino prematuramente. Revisa {log_file}", flush=True)
            break
        try:
            with open(log_file, "r") as f:
                content = f.read()
                match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', content)
                if match:
                    url = match.group(0)
                    break
        except Exception:
            pass
            
    if url:
        print(f"[FlareGUI] [OK] URL detectada: {url}", flush=True)
    else:
        print(f"[FlareGUI] [WARN] No se detecto la URL a tiempo. Revisa {log_file}", flush=True)
        
    return process, url

if __name__ == "__main__":
    print("[HLS] Iniciando servidor web...", flush=True)
    
    # 1. Iniciar el servidor web (FastAPI)
    server_process = subprocess.Popen([
        sys.executable, "-u", "-m", "uvicorn", "hls_server:app", 
        "--host", "0.0.0.0", "--port", str(PORT)
    ])
    esperar_servidor(server_process)

    # 2. Iniciar el tunel de Cloudflare
    flaregui_process, url_publica = iniciar_flaregui()

    # 3. Iniciar el Bot de Telegram
    print("[HLS] Iniciando Bot de Telegram...", flush=True)
    bot_process = subprocess.Popen([sys.executable, "-u", "hls_telegram.py"])

    # 4. Enviar la URL a Telegram
    if url_publica:
        enviar_url_telegram(url_publica)

    # 5. Mantener los procesos vivos y monitorear
    try:
        while True:
            time.sleep(60)
            if server_process.poll() is not None:
                print("[HLS] [ERROR] El servidor web ha caido.", flush=True)
                break
            if bot_process.poll() is not None:
                print("[HLS] [ERROR] El bot de Telegram ha caido.", flush=True)
                break
    except KeyboardInterrupt:
        print("\n[HLS] Deteniendo procesos...", flush=True)
        flaregui_process.terminate()
        bot_process.terminate()
        server_process.terminate()