"""
hls_telegram.py — Bot de Telegram con Pyrogram (Solo Privado).
"""

import asyncio
import configparser
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait

import hls_core
from hls_core import (
    BASE_DIR, MEDIA_DIR, RUNTIME,
    read_db_one, insert_video, update_video, list_videos,
    count_by_status, generate_hls, verificar_todos_todus,
)

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
def _load_config():
    cfg = configparser.ConfigParser()
    candidates = [
        BASE_DIR / "config" / "config_stream.ini",
        BASE_DIR / "config_stream.ini",
        BASE_DIR / "config.ini",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path:
        cfg.read(path, encoding="utf-8")

    if "TELEGRAM" not in cfg: cfg["TELEGRAM"] = {}
    if "STREAM" not in cfg: cfg["STREAM"] = {}

    cfg["TELEGRAM"]["API_ID"] = os.environ.get("HLS_API_ID", cfg["TELEGRAM"].get("API_ID", "0"))
    cfg["TELEGRAM"]["API_HASH"] = os.environ.get("HLS_API_HASH", cfg["TELEGRAM"].get("API_HASH", ""))
    cfg["TELEGRAM"]["BOT_TOKEN"] = os.environ.get("HLS_BOT_TOKEN", cfg["TELEGRAM"].get("BOT_TOKEN", ""))
    cfg["TELEGRAM"]["OWNER_ID"] = os.environ.get("HLS_OWNER_ID", cfg["TELEGRAM"].get("OWNER_ID", "0"))

    return cfg, path

CFG, CFG_PATH = _load_config()

API_ID = CFG.getint("TELEGRAM", "API_ID", fallback=0)
API_HASH = CFG.get("TELEGRAM", "API_HASH", fallback="")
BOT_TOKEN = CFG.get("TELEGRAM", "BOT_TOKEN", fallback="")
OWNER_ID = CFG.getint("TELEGRAM", "OWNER_ID", fallback=0)
PROCESAR_AUTOMATICO = CFG.getboolean("TELEGRAM", "PROCESAR_AUTOMATICO", fallback=True)

# NUEVOS VALORES POR DEFECTO
RUNTIME.compression_profile = CFG.get("STREAM", "COMPRESSION_PROFILE", fallback="small")
_cal_str = CFG.get("STREAM", "CALIDADES", fallback="360p,480p")
RUNTIME.calidades = [c.strip() for c in _cal_str.split(",") if c.strip()]
RUNTIME.guardar_en_todus = CFG.getboolean("STREAM", "GUARDAR_EN_TODUS", fallback=True)
RUNTIME.subir_mp4_telegram = CFG.getboolean("TELEGRAM", "SUBIR_MP4_TELEGRAM", fallback=False)
RUNTIME.canal_mp4 = os.environ.get(
    "HLS_CANAL_MP4", CFG.get("TELEGRAM", "CANAL_MP4", fallback="")
).strip()

# ─────────────────────────────────────────────────────────────
# RECARGA DE AJUSTES DESDE config.ini
# settings.html (vía hls_server.py) escribe aquí, pero el bot
# vive en otro proceso → hay que releer el archivo si cambia.
# ─────────────────────────────────────────────────────────────
_CFG_LAST_MTIME = 0.0

def _reload_settings_if_changed(force: bool = False) -> bool:
    """Relee config.ini si cambió su mtime. Actualiza RUNTIME + default_host."""
    global _CFG_LAST_MTIME
    try:
        if not CFG_PATH or not Path(CFG_PATH).exists():
            return False
        mtime = Path(CFG_PATH).stat().st_mtime
        if not force and mtime <= _CFG_LAST_MTIME:
            return False

        cfg = configparser.ConfigParser()
        try:
            cfg.read(CFG_PATH, encoding="utf-8")
        except configparser.Error as e:
            print(f"[SETTINGS] config.ini ilegible (¿escritura en curso?): {e}")
            return False

        changed = False

        if "STREAM" in cfg:
            new_profile = cfg.get("STREAM", "COMPRESSION_PROFILE",
                                  fallback=RUNTIME.compression_profile)
            if new_profile != RUNTIME.compression_profile:
                RUNTIME.compression_profile = new_profile
                changed = True

            cal_str = cfg.get("STREAM", "CALIDADES",
                              fallback=",".join(RUNTIME.calidades))
            new_cal = [c.strip() for c in cal_str.split(",") if c.strip()]
            if new_cal and new_cal != RUNTIME.calidades:
                RUNTIME.calidades = new_cal
                changed = True

            new_todus = cfg.getboolean("STREAM", "GUARDAR_EN_TODUS",
                                       fallback=RUNTIME.guardar_en_todus)
            if new_todus != RUNTIME.guardar_en_todus:
                RUNTIME.guardar_en_todus = new_todus
                changed = True

        if "TELEGRAM" in cfg:
            new_mp4tg = cfg.getboolean("TELEGRAM", "SUBIR_MP4_TELEGRAM",
                                       fallback=RUNTIME.subir_mp4_telegram)
            if new_mp4tg != RUNTIME.subir_mp4_telegram:
                RUNTIME.subir_mp4_telegram = new_mp4tg
                changed = True

            new_canal = os.environ.get(
                "HLS_CANAL_MP4",
                cfg.get("TELEGRAM", "CANAL_MP4", fallback=RUNTIME.canal_mp4),
            ).strip()
            if new_canal != RUNTIME.canal_mp4:
                RUNTIME.canal_mp4 = new_canal
                changed = True

        if "TODUS" in cfg:
            host = cfg.get("TODUS", "default_host", fallback="").strip().lower()
            if host in ("stream", "stickers"):
                todus = hls_core.get_todus()
                if todus.default_host != host:
                    todus.default_host = host
                    changed = True
                    print(f"[SETTINGS] default_host → {host}")

        _CFG_LAST_MTIME = mtime

        if changed or force:
            print(f"[SETTINGS] Recargado: profile={RUNTIME.compression_profile} "
                  f"calidades={RUNTIME.calidades} "
                  f"todus={RUNTIME.guardar_en_todus} "
                  f"mp4_tg={RUNTIME.subir_mp4_telegram} "
                  f"canal={RUNTIME.canal_mp4 or '-'}")
        return True
    except Exception as e:
        print(f"[SETTINGS] Error recargando config.ini: {e}")
        return False

# Inicializar mtime con el del archivo para no recargar en vano la 1ª vez
try:
    _CFG_LAST_MTIME = Path(CFG_PATH).stat().st_mtime if CFG_PATH else 0.0
except Exception:
    _CFG_LAST_MTIME = 0.0

_VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".m4v", ".3gp", ".mpg", ".mpeg", ".wmv",
}

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _is_video(msg: Message) -> bool:
    if not msg or not msg.media:
        return False
    if msg.video:
        return True
    if msg.document:
        name = (msg.document.file_name or "").lower()
        if any(name.endswith(ext) for ext in _VIDEO_EXTS):
            return True
        mime = (msg.document.mime_type or "").lower()
        if "video" in mime:
            return True
    return False


def _filename_from_msg(msg: Message) -> str:
    if msg.video and msg.video.file_name:
        return msg.video.file_name
    if msg.document and msg.document.file_name:
        return msg.document.file_name
    text = (msg.caption or msg.text or "").strip()
    if text:
        safe = hls_core._sanitize(text[:80])
        if safe:
            return f"{safe}.mp4"
    return f"video_{msg.id}.mp4"


def _msg_size(msg: Message) -> int:
    try:
        if msg.video and msg.video.file_size:
            return msg.video.file_size
        if msg.document and msg.document.file_size:
            return msg.document.file_size
    except Exception:
        pass
    return 0


def _fmt_bytes(n: int) -> str:
    if not n:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/1024/1024:.1f} MB"
    return f"{n/1024/1024/1024:.2f} GB"


def _fmt_eta(sec: int) -> str:
    if not sec or sec <= 0:
        return "--:--"
    m = sec // 60
    s = sec % 60
    return f"{m:02d}:{s:02d}"


def _bar(pct: int, width: int = 18) -> str:
    pct = max(0, min(100, int(pct)))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _render_status_text(
    state: str,
    filename: str = "",
    downloaded: int = 0,
    total: int = 0,
    speed: float = 0,
    eta: int = 0,
    queue_len: int = 0,
    error: str = "",
    etapa: str = "",
    calidad: str = "",
    pct_override: int = None,
) -> str:
    if state == "queued":
        head = "🕒 **En cola…**"
    elif state == "downloading":
        head = "⬇️ **Descargando desde Telegram…**"
    elif state == "downloaded":
        head = "✅ **Descarga completada**"
    elif state == "processing":
        head = "⚙️ **Procesando HLS…**"
    elif state == "completed":
        head = "✅ **Listo**"
    elif state == "error":
        head = "❌ **Error**"
    else:
        head = f"**{state}**"

    lines = [head]
    if filename:
        fn = filename if len(filename) <= 40 else filename[:37] + "..."
        lines.append(f"📄 `{fn}`")

    if state in ("downloading", "downloaded", "processing", "completed"):
        if pct_override is not None:
            pct = max(0, min(100, int(pct_override)))
        else:
            pct = int(downloaded * 100 / total) if total else 0
        lines.append(f"`{_bar(pct)}` **{pct}%**")
        if state in ("downloading", "downloaded"):
            lines.append(f"📦 {_fmt_bytes(downloaded)} / {_fmt_bytes(total)}")
        if state == "downloading":
            lines.append(f"⚡ {_fmt_bytes(int(speed))}/s · ⏱ ETA {_fmt_eta(eta)}")
        if state == "processing" and etapa:
            extra = f"🎬 {etapa}"
            if calidad:
                extra += f" · `{calidad}`"
            lines.append(extra)

    if queue_len > 1:
        lines.append(f"📥 Cola: {queue_len} pendiente(s)")

    if error:
        lines.append(f"⚠️ `{error[:200]}`")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# ESTADO GLOBAL
# ─────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.client = None
        self.procesando = False
        self.activo = False
        self.cola = asyncio.Queue()
        self.worker_task = None
        self.procesados = set()
        self.en_cola = set()
        self.status_msgs = {}
        self.estadisticas = {
            "encolados": 0, "descargados": 0, "errores": 0,
            "iniciado": None,
        }


STATE = BotState()

# ─────────────────────────────────────────────────────────────
# THROTTLE DE EDICIONES
# ─────────────────────────────────────────────────────────────
_LAST_EDIT = {}
_EDIT_LOCK = {}
MIN_EDIT_INTERVAL = 8.0


def _get_edit_lock(msg_id: int) -> asyncio.Lock:
    if msg_id not in _EDIT_LOCK:
        _EDIT_LOCK[msg_id] = asyncio.Lock()
    return _EDIT_LOCK[msg_id]


async def _edit_status(msg: Message, text: str, msg_id: int = None):
    if not msg:
        return
    mid = msg_id or msg.id
    lock = _get_edit_lock(mid)
    async with lock:
        now = time.time()
        last = _LAST_EDIT.get(mid, 0)
        if now - last < MIN_EDIT_INTERVAL:
            return
        try:
            await msg.edit_text(text)
            _LAST_EDIT[mid] = time.time()
        except FloodWait as e:
            print(f"[EDIT] FloodWait {e.value}s (skip, msg {mid})")
            _LAST_EDIT[mid] = time.time() + min(e.value, 30)
        except Exception as e:
            if "not modified" not in str(e).lower():
                print(f"[EDIT] {e}")


# ─────────────────────────────────────────────────────────────
# WATCHER ffmpeg
# ─────────────────────────────────────────────────────────────
async def _watch_ffmpeg(media_id: str, msg_id: int, filename: str,
                        cancel: asyncio.Event = None, max_seconds: int = 10800):
    status_msg = STATE.status_msgs.get(msg_id)
    started = time.time()
    last_edit = 0.0
    while True:
        if cancel is not None and cancel.is_set():
            return
        if time.time() - started > max_seconds:
            print(f"[WATCH] timeout {media_id}")
            return
        media = read_db_one(media_id) or {}
        st = media.get("status")
        if st == "completed":
            await _edit_status(status_msg, _render_status_text(
                "completed", filename=filename, pct_override=100,
                queue_len=STATE.cola.qsize()), msg_id=msg_id)
            STATE.status_msgs.pop(msg_id, None)
            return
        if st == "error":
            await _edit_status(status_msg, _render_status_text(
                "error", filename=filename,
                error=media.get("error", "Error")), msg_id=msg_id)
            STATE.status_msgs.pop(msg_id, None)
            return
        ff = media.get("ffmpeg_progress") or {}
        if ff and (time.time() - last_edit) >= MIN_EDIT_INTERVAL:
            last_edit = time.time()
            pct = ff.get("porcentaje_global", 0)
            etapa = ff.get("etapa", "Procesando…")
            calidad = ff.get("calidad_nombre", "")
            await _edit_status(status_msg, _render_status_text(
                "processing", filename=filename, pct_override=pct,
                etapa=etapa, calidad=calidad,
                queue_len=STATE.cola.qsize()), msg_id=msg_id)
        for _ in range(4):
            if cancel is not None and cancel.is_set():
                return
            await asyncio.sleep(0.5)


# ─────────────────────────────────────────────────────────────
# SUBIR MP4 A TELEGRAM
# ─────────────────────────────────────────────────────────────
async def _subir_mp4_a_telegram(app: Client, media_id: str, msg_id: int, filename: str):
    # Recargar ajustes por si cambiaron desde settings.html mientras procesábamos
    _reload_settings_if_changed()

    if not RUNTIME.subir_mp4_telegram or not RUNTIME.canal_mp4:
        return
    canal = RUNTIME.canal_mp4.strip()
    if not canal:
        return

    if canal.startswith("https://t.me/+"):
        try:
            chat = await app.join_chat(canal)
            chat_id = chat.id
            print(f"[TG-MP4] Canal resuelto: {chat_id}")
        except Exception as e:
            print(f"[TG-MP4] No se pudo unir al canal {canal}: {e}")
            return
    else:
        try:
            chat_id = int(canal)
        except ValueError:
            chat_id = canal

    media = read_db_one(media_id) or {}
    qualities = media.get("qualities", [])
    if not qualities:
        return

    status_msg = STATE.status_msgs.get(msg_id)

    for q in qualities:
        mp4_path = hls_core.CACHE_DIR / f"{media_id}_{q}.mp4"
        if not mp4_path.exists() or mp4_path.stat().st_size < 1000:
            continue
        try:
            caption = f"🎬 {filename} · {q}"
            if status_msg:
                await _edit_status(status_msg, _render_status_text(
                    "processing", filename=filename,
                    etapa=f"Subiendo MP4 {q} a Telegram…",
                    queue_len=STATE.cola.qsize()), msg_id=msg_id)
            await app.send_video(
                chat_id=chat_id,
                video=str(mp4_path),
                caption=caption,
                supports_streaming=True,
            )
            print(f"[TG-MP4] ✅ {media_id} {q} subido a {chat_id}")
        except Exception as e:
            print(f"[TG-MP4] ❌ {media_id} {q}: {e}")


# ─────────────────────────────────────────────────────────────
# WORKER DE DESCARGA
# ─────────────────────────────────────────────────────────────
async def _worker_descarga():
    while STATE.activo:
        try:
            item = await asyncio.wait_for(STATE.cola.get(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break

        msg: Message = item["msg"]
        msg_id = msg.id
        try:
            if msg_id in STATE.procesados:
                continue

            status_msg = STATE.status_msgs.get(msg_id)
            if status_msg:
                size = _msg_size(msg)
                await _edit_status(status_msg, _render_status_text(
                    "downloading", filename=_filename_from_msg(msg),
                    downloaded=0, total=size,
                    queue_len=STATE.cola.qsize()), msg_id=msg_id)

            await _procesar_video(msg)
        except Exception as e:
            STATE.estadisticas["errores"] += 1
            print(f"[COLA] ❌ {msg_id}: {e}")
        finally:
            STATE.en_cola.discard(msg_id)
            STATE.cola.task_done()


# ─────────────────────────────────────────────────────────────
# PROCESAR UN VIDEO
# ─────────────────────────────────────────────────────────────
async def _procesar_video(msg: Message):
    # Recargar ajustes desde config.ini (por si settings.html los cambió)
    _reload_settings_if_changed()

    internal_id = str(uuid.uuid4())
    media_id = f"media_{internal_id[:12]}"
    media_dir = MEDIA_DIR / internal_id
    media_dir.mkdir(parents=True, exist_ok=True)

    filename = _filename_from_msg(msg)
    safe_name = hls_core._sanitize(Path(filename).stem) or f"video_{msg.id}"
    safe_name = safe_name[:120]
    ext = Path(filename).suffix.lower() or ".mp4"
    target = media_dir / f"{safe_name}{ext}"

    size = _msg_size(msg)

    print(f"[DESC] ⬇️ {msg.id} → {target.name} ({_fmt_bytes(size)})")

    insert_video(media_id, {
        "media_id": media_id,
        "internal_id": internal_id,
        "original_filename": target.name,
        "display_name": safe_name,
        "upload_time": datetime.utcnow().isoformat(),
        "status": "processing",
        "compression_profile": RUNTIME.compression_profile,
        "codec": "H.264 (Compatible)",
        "qualities": list(RUNTIME.calidades),
        "master_playlist": None,
        "file_size_mb": round(size / (1024 * 1024), 2) if size else 0,
        "telegram_message_id": msg.id,
        "telegram_chat_id": msg.chat.id if msg.chat else None,
        "description": (msg.caption or msg.text or "")[:500],
        "mp4_ready": False,
        "mp4_sizes": {},
        "hls_source": "local",
    })

    status_msg = STATE.status_msgs.get(msg.id)
    await _edit_status(status_msg, _render_status_text(
        "downloading", filename=target.name,
        downloaded=0, total=size,
        queue_len=STATE.cola.qsize()), msg_id=msg.id)

    dl_state = {
        "current": 0,
        "total": size or 0,
        "start": time.time(),
        "done": False,
    }

    def _progress_sync(current, total, *args):
        try:
            dl_state["current"] = int(current)
            if total:
                dl_state["total"] = int(total)
        except Exception:
            pass

    async def _editor_progreso():
        last_edit = 0.0
        while not dl_state["done"]:
            now = time.time()
            if now - last_edit >= 3.0:
                last_edit = now
                cur = dl_state["current"]
                tot = dl_state["total"] or size or cur or 1
                elapsed = now - dl_state["start"] or 0.001
                speed = cur / elapsed if elapsed > 0 else 0
                eta = int((tot - cur) / speed) if speed > 0 and tot > cur else 0
                await _edit_status(status_msg, _render_status_text(
                    "downloading",
                    filename=target.name,
                    downloaded=cur,
                    total=tot,
                    speed=speed,
                    eta=eta,
                    queue_len=STATE.cola.qsize(),
                ), msg_id=msg.id)
            await asyncio.sleep(0.5)

    editor_task = asyncio.create_task(_editor_progreso())

    downloaded = None
    try:
        downloaded = await msg.download(
            file_name=str(target),
            progress=_progress_sync,
        )
    except FloodWait as e:
        print(f"[DESC] FloodWait {e.value}s, reintentando...")
        await asyncio.sleep(min(e.value + 5, 60))
        try:
            downloaded = await msg.download(
                file_name=str(target),
                progress=_progress_sync,
            )
        except Exception as e2:
            dl_state["done"] = True
            try:
                await asyncio.wait_for(editor_task, timeout=5.0)
            except Exception:
                editor_task.cancel()
            update_video(media_id, status="error", error=f"download: {e2}")
            await _edit_status(status_msg, _render_status_text(
                "error", filename=target.name, error=str(e2)), msg_id=msg.id)
            STATE.estadisticas["errores"] += 1
            print(f"[DESC] ❌ {msg.id}: {e2}")
            return
    except Exception as e:
        dl_state["done"] = True
        try:
            await asyncio.wait_for(editor_task, timeout=5.0)
        except Exception:
            editor_task.cancel()
        update_video(media_id, status="error", error=f"download: {e}")
        await _edit_status(status_msg, _render_status_text(
            "error", filename=target.name, error=str(e)), msg_id=msg.id)
        STATE.estadisticas["errores"] += 1
        print(f"[DESC] ❌ {msg.id}: {e}")
        return
    finally:
        dl_state["done"] = True
        try:
            await asyncio.wait_for(editor_task, timeout=5.0)
        except Exception:
            editor_task.cancel()
            try:
                await editor_task
            except Exception:
                pass

    if not downloaded or not Path(downloaded).exists():
        update_video(media_id, status="error", error="download: archivo vacío")
        await _edit_status(status_msg, _render_status_text(
            "error", filename=target.name, error="archivo vacío"), msg_id=msg.id)
        STATE.estadisticas["errores"] += 1
        return

    real_size = Path(downloaded).stat().st_size
    update_video(media_id, file_size_mb=round(real_size / (1024 * 1024), 2))
    STATE.estadisticas["descargados"] += 1
    print(f"[DESC] ✅ {msg.id} descargado ({_fmt_bytes(real_size)})")

    await _edit_status(status_msg, _render_status_text(
        "downloaded", filename=target.name,
        downloaded=real_size, total=real_size,
        queue_len=STATE.cola.qsize()), msg_id=msg.id)

    loop = asyncio.get_running_loop()
    cancel_watch = asyncio.Event()
    watch_task = asyncio.create_task(
        _watch_ffmpeg(media_id, msg.id, target.name, cancel=cancel_watch)
    )

    try:
        await loop.run_in_executor(
            None,
            generate_hls,
            media_id, internal_id, Path(downloaded), target.name,
            (msg.caption or msg.text or "")[:500],
        )
    finally:
        cancel_watch.set()
        try:
            await asyncio.wait_for(watch_task, timeout=10.0)
        except (asyncio.TimeoutError, Exception):
            watch_task.cancel()
            try:
                await watch_task
            except Exception:
                pass

    await _subir_mp4_a_telegram(STATE.client, media_id, msg.id, target.name)

    STATE.procesados.add(msg.id)
    STATE.status_msgs.pop(msg.id, None)
    print(f"[HLS] ✅ {media_id} procesado")


# ─────────────────────────────────────────────────────────────
# ENCOLAR
# ─────────────────────────────────────────────────────────────
async def _ensure_worker():
    if STATE.worker_task is None or STATE.worker_task.done():
        STATE.activo = True
        STATE.worker_task = asyncio.create_task(_worker_descarga())
        print("[worker] relanzado por _ensure_worker")


async def _encolar(msg: Message) -> bool:
    if not msg.from_user or msg.from_user.id != OWNER_ID:
        return False
    if msg.id in STATE.procesados or msg.id in STATE.en_cola:
        return False
    if not _is_video(msg):
        return False

    STATE.en_cola.add(msg.id)
    STATE.estadisticas["encolados"] += 1
    await _ensure_worker()
    await STATE.cola.put({"msg": msg, "encolado_en": datetime.utcnow().isoformat()})
    return True


def _is_owner(msg: Message) -> bool:
    return msg.from_user and msg.from_user.id == OWNER_ID


# ─────────────────────────────────────────────────────────────
# PERSISTENCIA DE default_host EN config.ini (escritura atómica)
# ─────────────────────────────────────────────────────────────
def _persist_host(nuevo_host: str):
    try:
        if not CFG_PATH:
            return
        cfg = configparser.ConfigParser()
        cfg.read(CFG_PATH, encoding="utf-8")
        if "TODUS" not in cfg:
            cfg["TODUS"] = {}
        cfg["TODUS"]["default_host"] = nuevo_host
        tmp = Path(str(CFG_PATH) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            cfg.write(f)
        os.replace(tmp, CFG_PATH)
        # Refrescar mtime para que el bot no lo re-lea como cambio externo
        global _CFG_LAST_MTIME
        try:
            _CFG_LAST_MTIME = Path(CFG_PATH).stat().st_mtime
        except Exception:
            pass
    except Exception as e:
        print(f"[HOST] No se pudo guardar en config.ini: {e}")


# ─────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────
def register_handlers(app: Client):

    @app.on_message(filters.command("ping"))
    async def ping_cmd(client: Client, message: Message):
        if not _is_owner(message):
            return
        t0 = time.time()
        await message.reply("🏓 Pong · HLS ToDus activo")
        print(f"[PING] latencia={time.time()-t0:.2f}s cola={STATE.cola.qsize()}")

    @app.on_message(filters.command("reload"))
    async def reload_cmd(client: Client, message: Message):
        if not _is_owner(message):
            return
        _reload_settings_if_changed(force=True)
        todus = hls_core.get_todus()
        await message.reply(
            "🔄 **Ajustes recargados desde config.ini**\n\n"
            f"⚙️ Perfil: `{RUNTIME.compression_profile}`\n"
            f"🎚️ Calidades: {', '.join(RUNTIME.calidades)}\n"
            f"☁️ ToDus: {'✅' if RUNTIME.guardar_en_todus else '❌'}\n"
            f"📤 MP4 Telegram: {'✅' if RUNTIME.subir_mp4_telegram else '❌'}\n"
            f"📺 Canal MP4: `{RUNTIME.canal_mp4 or '-'}`\n"
            f"🖥️ Host: `{todus.default_host}`\n"
            f"🌐 Endpoint: `{todus.public_base}`"
        )

    @app.on_message(filters.command("host"))
    async def host_cmd(client: Client, message: Message):
        if not _is_owner(message):
            return

        todus = hls_core.get_todus()
        args = (message.text or "").split()[1:]

        if not args:
            await message.reply(
                f"🖥️ **Host activo:** `{todus.default_host}`\n"
                f"🌐 **Endpoint:** `{todus.public_base}`\n"
                f"📁 **Ruta HLS:** `{todus.hls_prefix}`\n\n"
                f"**Endpoints disponibles:**\n"
                f"• `stream`   → `{todus.stream_base}`\n"
                f"• `stickers` → `{todus.stickers_base}`\n\n"
                f"**Uso:**\n"
                f"• `/host stream` — sube a `{todus.stream_base}`\n"
                f"• `/host stickers` — sube a `{todus.stickers_base}`\n\n"
                f"_Los videos nuevos y los enlaces generados usan el host activo. "
                f"Los ya subidos mantienen su URL original (guardada en DB)._")
            return

        nuevo = args[0].strip().lower()
        if nuevo not in ("stream", "stickers"):
            await message.reply("❌ Usa `/host stream` o `/host stickers`")
            return

        todus.default_host = nuevo
        _persist_host(nuevo)

        await message.reply(
            f"✅ **Host cambiado a {nuevo}.**\n\n"
            f"🌐 **Endpoint activo:** `{todus.public_base}`\n"
            f"📁 **Ruta HLS:** `{todus.hls_prefix}`\n\n"
            f"_A partir de ahora las nuevas subidas usan este endpoint. "
            f"Los videos ya subidos conservan su URL original._")

    @app.on_message(filters.command("streamStats"))
    async def stream_stats(client: Client, message: Message):
        if not _is_owner(message):
            return
        _reload_settings_if_changed()
        counts = count_by_status()
        total = sum(counts.values())
        completados = counts.get("completed", 0)
        procesando = counts.get("processing", 0)
        errores = counts.get("error", 0)
        todus = hls_core.get_todus()
        texto = (
            "📊 **Estadísticas HLS**\n\n"
            f"📹 Total: {total}\n"
            f"✅ Completados: {completados}\n"
            f"⏳ Procesando: {procesando}\n"
            f"❌ Errores: {errores}\n\n"
            f"📥 Cola: {STATE.cola.qsize()} pendientes\n"
            f"⚙️ Perfil: `{RUNTIME.compression_profile}`\n"
            f"🎚️ Calidades: {', '.join(RUNTIME.calidades)}\n"
            f"🖥️ Host: `{todus.default_host}`\n"
            f"🌐 Endpoint: `{todus.public_base}`\n"
            f"📁 Ruta HLS: `{todus.hls_prefix}`\n"
            f"☁️ ToDus: {'✅ activado' if RUNTIME.guardar_en_todus else '❌ desactivado'}\n"
            f"📤 MP4 Telegram: {'✅ activado' if RUNTIME.subir_mp4_telegram else '❌ desactivado'}"
        )
        await message.reply(texto)

    @app.on_message(filters.command("streamQueue"))
    async def stream_queue(client: Client, message: Message):
        if not _is_owner(message):
            return
        if STATE.cola.empty():
            return await message.reply("📭 Cola vacía.")
        items = []
        for i, item in enumerate(list(STATE.cola._queue)[:10], start=1):
            m = item["msg"]
            items.append(f"{i}. `{m.id}` — {_filename_from_msg(m)}")
        texto = f"📥 **Cola ({STATE.cola.qsize()}):**\n\n" + "\n".join(items)
        await message.reply(texto)

    @app.on_message(filters.command("stopStream"))
    async def stream_stop(client: Client, message: Message):
        if not _is_owner(message):
            return
        pendientes = STATE.cola.qsize()
        while not STATE.cola.empty():
            try:
                item = STATE.cola.get_nowait()
                STATE.cola.task_done()
                mid = item["msg"].id
                STATE.en_cola.discard(mid)
                sm = STATE.status_msgs.pop(mid, None)
                if sm:
                    try:
                        await sm.edit_text("🛑 Cancelado (stopStream)")
                    except Exception:
                        pass
            except Exception:
                break
        if STATE.worker_task is None or STATE.worker_task.done():
            STATE.activo = True
            STATE.worker_task = asyncio.create_task(_worker_descarga())
        await message.reply(f"🛑 Cola vaciada. Descartados: {pendientes}")

    @app.on_message(filters.private & (filters.video | filters.document))
    async def recibir_video(client: Client, message: Message):
        if not _is_owner(message):
            return
        if not PROCESAR_AUTOMATICO:
            return
        if not _is_video(message):
            return
        if message.id in STATE.procesados or message.id in STATE.en_cola:
            return

        size = _msg_size(message)
        status_msg = await message.reply(_render_status_text(
            "queued",
            filename=_filename_from_msg(message),
            downloaded=0,
            total=size,
            queue_len=STATE.cola.qsize(),
        ))
        STATE.status_msgs[message.id] = status_msg

        ok = await _encolar(message)
        if not ok:
            try:
                await status_msg.delete()
            except Exception:
                pass
            STATE.status_msgs.pop(message.id, None)

    @app.on_message(filters.command("todusVerify"))
    async def todus_verify(client: Client, message: Message):
        if not _is_owner(message):
            return

        args = (message.text or "").split()
        solo_todus = "--solo" in args
        sync = "--sync" in args

        msg = await message.reply("🔍 Verificando ToDus…")

        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None, verificar_todos_todus, solo_todus, sync
            )
        except Exception as e:
            await msg.edit_text(f"💥 Error: {e}")
            return

        r = data["resumen"]
        iconos = {"ok": "✅", "parcial": "⚠️", "ausente": "❌", "sin_info": "➖", "error": "💥"}

        texto = (
            "📊 **Verificación ToDus**\n\n"
            f"📹 Total DB: `{r['total_db']}`\n"
            f"🔎 Revisados: `{r['revisados']}`\n"
            f"✅ OK: `{r['ok']}`\n"
            f"⚠️ Parcial: `{r['parcial']}`\n"
            f"❌ Ausente: `{r['ausente']}`\n"
            f"➖ Sin info: `{r['sin_info']}`\n"
            f"💥 Error: `{r['error']}`"
        )
        if "db_corregidos" in r:
            texto += f"\n\n🔧 **DB corregidos:** `{r['db_corregidos']}`"

        problemas = [x for x in data["resultados"]
                     if x["estado"] in ("ausente", "parcial", "error")][:10]
        if problemas:
            texto += "\n\n**Problemas:**\n"
            for p in problemas:
                nombre = (p["display_name"] or p["media_id"])[:28]
                faltan = ",".join(p["calidades_faltantes"])
                linea = f"{iconos[p['estado']]} `{p['media_id']}` — {nombre}"
                if faltan:
                    linea += f" _(falta: {faltan})_"
                texto += linea + "\n"
            if len(problemas) == 10:
                texto += "_… (más en consola)_\n"
                for p in data["resultados"]:
                    if p["estado"] in ("ausente", "parcial", "error"):
                        print(f"[VERIFY] {iconos[p['estado']]} {p['media_id']} {p['estado']} "
                              f"faltan={p['calidades_faltantes']} bucket={p['archivos_bucket']}")

        if r["revisados"] == 0:
            texto += "\n\n_Aún no hay videos marcados en ToDus._"

        try:
            await msg.edit_text(texto)
        except FloodWait as e:
            print(f"[VERIFY] FloodWait {e.value}s (skip edit)")
        except Exception as e:
            if "not modified" not in str(e).lower():
                print(f"[VERIFY] {e}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
async def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:
        print("❌ Faltan API_ID, API_HASH o BOT_TOKEN en config.ini [TELEGRAM]")
        return

    app = Client(
        "hls_telegram_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=16,
        sleep_threshold=60,
    )
    STATE.client = app
    register_handlers(app)

    await app.start()
    print("[OK] Bot Pyrogram conectado (Modo Privado).")

    todus = hls_core.get_todus()
    print(f"[OK] Host activo: {todus.default_host} → {todus.public_base}")
    print(f"[OK] Ruta HLS: {todus.hls_prefix}")

    STATE.activo = True
    STATE.worker_task = asyncio.create_task(_worker_descarga())

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        STATE.activo = False
        if STATE.worker_task:
            STATE.worker_task.cancel()
            try:
                await STATE.worker_task
            except Exception:
                pass
        try:
            await app.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[EXIT] Interrumpido por el usuario.")