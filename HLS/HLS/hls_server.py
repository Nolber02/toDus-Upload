"""
hls_server.py — Servidor FastAPI para HLS con ToDus.

Endpoints:
  GET  /                       → player.html
  GET  /player                 → player.html
  GET  /settings               → settings.html
  GET  /list                   → lista de videos (JSON)
  GET  /status/{media_id}      → estado de un video
  GET  /progress/{media_id}    → progreso ffmpeg
  GET  /stream/{id}/master.m3u8           → master (ToDus si existe, local si no)
  GET  /stream/{id}/{quality}/playlist.m3u8 → playlist local
  GET  /stream/{id}/{quality}/{segment}     → segmento local
  GET  /download/{id}/{quality} → MP4 generado
  POST /download/pre-generate/{id} → pre-generar MP4s
  DELETE /media/{id}           → borrar video (local + ToDus)
  GET  /storage                → uso de disco
  GET  /api/settings/stream    → config actual
  POST /api/settings/stream    → guardar config (perfil, calidades, guardar_en_todus)
  GET  /api/host               → host activo (stickers|stream) + endpoints + hls_prefix
  POST /api/host               → cambiar host en caliente
  POST /api/todus/upload/{id}  → subir a ToDus manualmente
  DELETE /api/todus/{id}       → borrar de ToDus sin borrar local
  GET  /debug/db               → diagnóstico DB
"""
import asyncio
import os
import shutil
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import uvicorn

import hls_core
from hls_core import (
    BASE_DIR, MEDIA_DIR, CACHE_DIR, DB_PATH,
    COMPRESSION_PROFILES, BASE_QUALITIES, RUNTIME,
    read_db_one, update_video, delete_video_db, list_videos, count_by_status,
    generar_mp4_desde_hls, subir_hls_a_todus, borrar_hls_de_todus,
    build_master_text, get_todus, reparar_hls_todus,
)

_TODUS_REPAIR_LOCK = asyncio.Lock()


async def _vigilar_hls_todus():
    """Revisa cada 10 segundos los HLS completados y repara los archivos ausentes."""
    while True:
        try:
            if RUNTIME.auto_reparar_todus:
                async with _TODUS_REPAIR_LOCK:
                    videos, _ = await _run_blocking(list_videos, 0)
                    for media_id, media in videos:
                        if (media.get("status") != "completed"
                                or media.get("hls_local_borrado")):
                            continue
                        local_root = MEDIA_DIR / media.get("internal_id", "")
                        if not local_root.is_dir():
                            continue
                        resultado = await _run_blocking(reparar_hls_todus, media_id)
                        if resultado.get("archivos_subidos"):
                            print(
                                f"[TODUS-WATCH] {media_id}: "
                                f"{resultado['archivos_subidos']} archivo(s) reparados"
                            )
                        if resultado.get("estado") in ("error", "parcial"):
                            print(f"[TODUS-WATCH] {media_id}: {resultado}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[TODUS-WATCH] Error verificando ToDus: {e}")
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    watcher_task = asyncio.create_task(_vigilar_hls_todus())
    try:
        yield
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="HLS ToDus Server", version="1.1", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# HELPER: ejecutar trabajo bloqueante fuera del event loop
# ─────────────────────────────────────────────────────────────
async def _run_blocking(func, *args, **kwargs):
    """Ejecuta una función bloqueante (SQLite, requests/S3, ffmpeg) fuera del event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# ─────────────────────────────────────────────────────────────
# PÁGINAS HTML
# ─────────────────────────────────────────────────────────────
def _serve_html(filename: str):
    p = BASE_DIR / filename
    if p.exists():
        return FileResponse(p, media_type="text/html")
    return HTMLResponse(
        f"<h1>{filename} no encontrado</h1><p>Colócalo en {BASE_DIR}</p>",
        status_code=404,
    )

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.get("/", include_in_schema=False)
async def root_page():
    return _serve_html("player.html")

@app.get("/player", include_in_schema=False)
async def player_page():
    return _serve_html("player.html")

@app.get("/settings", include_in_schema=False)
async def settings_page():
    return _serve_html("settings.html")

@app.get("/player.css", include_in_schema=False)
async def player_css():
    p = BASE_DIR / "player.css"
    if p.exists():
        return FileResponse(p, media_type="text/css")
    raise HTTPException(404, "player.css no encontrado")

@app.get("/player.js", include_in_schema=False)
async def player_js():
    p = BASE_DIR / "player.js"
    if p.exists():
        return FileResponse(p, media_type="application/javascript")
    raise HTTPException(404, "player.js no encontrado")


# ─────────────────────────────────────────────────────────────
# LISTA / ESTADO
# ─────────────────────────────────────────────────────────────
@app.get("/list")
async def list_media(limit: int = 0, offset: int = 0):
    videos, total = await _run_blocking(list_videos, limit, offset)
    media_list = []

    for mid, data in videos:
        qs = data.get("quality_sizes", {})
        th = data.get("total_hls_size_mb", 0)
        ms, ma = {}, {}
        hls_local_borrado = bool(data.get("hls_local_borrado"))

        if (data.get("status") == "completed"
                and not qs
                and not hls_local_borrado):
            iid = data.get("internal_id")
            calc = {}
            total_hls = 0
            for q in data.get("qualities", []):
                qd = MEDIA_DIR / iid / q
                if qd.exists():
                    ts_list = list(qd.glob("*.ts"))
                    if ts_list:
                        sz = round(sum(f.stat().st_size for f in ts_list) / (1024 * 1024), 2)
                        calc[q] = sz
                        total_hls += sz
                    else:
                        calc[q] = 0
                else:
                    calc[q] = 0
            if calc:
                qs = calc
                th = round(total_hls, 2)
                await _run_blocking(update_video, mid, quality_sizes=qs, total_hls_size_mb=th)

        if data.get("status") == "completed":
            for q in data.get("qualities", []):
                cp = CACHE_DIR / f"{mid}_{q}.mp4"
                if cp.exists() and cp.stat().st_size > 1000:
                    ms[q] = round(cp.stat().st_size / (1024 * 1024), 2)
                    ma[q] = True
                else:
                    ms[q] = 0
                    ma[q] = False

        media_list.append({
            "media_id": mid,
            "filename": data.get("original_filename"),
            "display_name": data.get("display_name") or Path(
                data.get("original_filename", mid)).stem,
            "status": data.get("status"),
            "codec": data.get("codec", "Unknown"),
            "compression": data.get("compression_profile", RUNTIME.compression_profile),
            "upload_time": data.get("upload_time"),
            "file_size_mb": data.get("file_size_mb"),
            "qualities": data.get("qualities", []),
            "description": data.get("description", ""),
            "quality_sizes": qs,
            "mp4_sizes": ms,
            "mp4_available": ma,
            "mp4_ready": data.get("mp4_ready", False),
            "total_hls_size_mb": th,
            "telegram_uploads": data.get("telegram_uploads", {}),
            "hls_source": data.get("hls_source", "local"),
            "hls_local_borrado": hls_local_borrado,
            "en_todus": bool(data.get("todus_hls")),
            "todus_carpeta": (data.get("todus_hls") or {}).get("carpeta_video"),
            "todus_host": (data.get("todus_hls") or {}).get("host"),
        })

    return {"total": total, "returned": len(media_list), "media": media_list}


@app.get("/status/{media_id}")
async def get_status(media_id: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media:
        raise HTTPException(404, "Media not found")

    response = {
        "media_id": media_id,
        "status": media.get("status", "unknown"),
        "codec": media.get("codec", "Unknown"),
        "compression_profile": media.get("compression_profile", RUNTIME.compression_profile),
        "original_filename": media.get("original_filename"),
        "display_name": media.get("display_name") or Path(
            media.get("original_filename", media_id)).stem,
        "upload_time": media.get("upload_time"),
        "file_size_mb": media.get("file_size_mb"),
        "description": media.get("description", ""),
        "mp4_ready": media.get("mp4_ready", False),
        "mp4_sizes": media.get("mp4_sizes", {}),
        "telegram_uploads": media.get("telegram_uploads", {}),
        "hls_source": media.get("hls_source", "local"),
        "hls_local_borrado": bool(media.get("hls_local_borrado")),
        "todus_hls": media.get("todus_hls"),
        "en_todus": bool(media.get("todus_hls")),
    }

    if media.get("status") == "completed":
        response["qualities"] = media.get("qualities", [])
        response["stream_url"] = f"/player?video={media_id}"
        response["master_playlist"] = f"/stream/{media_id}/master.m3u8"

        quality_sizes = media.get("quality_sizes")
        if quality_sizes is None and not media.get("hls_local_borrado"):
            iid = media.get("internal_id")
            quality_sizes = {}
            total_hls = 0
            for q in media.get("qualities", []):
                qd = MEDIA_DIR / iid / q
                sz = (sum(f.stat().st_size for f in qd.glob("*.ts")) / (1024 * 1024)
                      if qd.exists() and list(qd.glob("*.ts")) else 0)
                total_hls += sz
                quality_sizes[q] = round(sz, 2)
            response["total_hls_size_mb"] = round(total_hls, 2)
            await _run_blocking(update_video, media_id, quality_sizes=quality_sizes, total_hls_size_mb=round(total_hls, 2))
        else:
            response["total_hls_size_mb"] = media.get("total_hls_size_mb", 0)
            if quality_sizes is None:
                quality_sizes = {}

        mp4_available = {}
        for q in media.get("qualities", []):
            cp = CACHE_DIR / f"{media_id}_{q}.mp4"
            mp4_available[q] = cp.exists() and cp.stat().st_size > 1000
        response["quality_sizes"] = quality_sizes
        response["mp4_available"] = mp4_available
    elif media.get("status") == "error":
        response["error"] = media.get("error", "Unknown error")

    return response


@app.get("/progress/{media_id}")
async def get_progress(media_id: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media:
        raise HTTPException(404, "Media not found")
    status = media.get("status", "unknown")

    if status == "completed":
        mp4_sizes = media.get("mp4_sizes", {})
        return {
            "media_id": media_id, "status": "completed", "progress": 100,
            "message": "Completado",
            "mp4_ready": media.get("mp4_ready", False),
            "mp4_generados": sum(1 for s in mp4_sizes.values() if s > 0),
            "mp4_total": len(media.get("qualities", [])),
        }
    elif status == "error":
        return {"media_id": media_id, "status": "error",
                "error": media.get("error", "Error")}
    elif status == "processing":
        ff = media.get("ffmpeg_progress") or {}
        return {
            "media_id": media_id, "status": "processing",
            "progress": ff.get("porcentaje_global", 0),
            "calidad_actual": ff.get("calidad_nombre", "Iniciando..."),
            "calidad_index": ff.get("calidad_actual", 0),
            "total_calidades": ff.get("total_calidades", len(RUNTIME.calidades)),
            "etapa": ff.get("etapa", "Preparando..."),
            "tiempo_actual": ff.get("tiempo_actual", 0),
            "total_duracion": ff.get("total_duracion", 0),
            "fps": ff.get("fps", 0),
            "actualizado": ff.get("actualizado", datetime.utcnow().isoformat()),
        }
    return {"media_id": media_id, "status": status, "progress": 0}

# ─────────────────────────────────────────────────────────────
# STREAM (ToDus si existe, local si no)
# ─────────────────────────────────────────────────────────────
@app.get("/stream/{media_id}/master.m3u8")
async def get_master(media_id: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media:
        raise HTTPException(404, "Media not found")
    if media.get("status") != "completed":
        raise HTTPException(425, "Media still processing")

    text = build_master_text(media)
    if text:
        return Response(
            text,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )

    if media.get("hls_local_borrado"):
        raise HTTPException(404, "HLS local borrado y no hay todus_hls disponible")
    master_path = MEDIA_DIR / media["internal_id"] / "master.m3u8"
    if not master_path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(
        master_path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/stream/{media_id}/{quality}/playlist.m3u8")
async def get_quality_playlist(media_id: str, quality: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media or media.get("status") != "completed":
        raise HTTPException(404, "Not found")
    if quality not in media.get("qualities", []):
        raise HTTPException(404, "Quality not found")
    if media.get("hls_local_borrado"):
        raise HTTPException(404, "HLS local borrado (usar ToDus)")
    qp = MEDIA_DIR / media["internal_id"] / quality / "playlist.m3u8"
    if not qp.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(
        qp,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/stream/{media_id}/{quality}/{segment}")
async def get_segment(media_id: str, quality: str, segment: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media or media.get("status") != "completed":
        raise HTTPException(404, "Not found")
    if media.get("hls_local_borrado"):
        raise HTTPException(404, "HLS local borrado (usar ToDus)")
    sp = MEDIA_DIR / media["internal_id"] / quality / segment
    if not sp.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(
        sp,
        media_type="video/MP2T",
        headers={"Cache-Control": "public, max-age=3600",
                 "Access-Control-Allow-Origin": "*"},
    )


# ─────────────────────────────────────────────────────────────
# DESCARGAS MP4
# ─────────────────────────────────────────────────────────────
def _content_disposition(filename: str) -> str:
    ascii_fb = "".join(c if ord(c) < 128 else "_" for c in filename)
    ascii_fb = ascii_fb.replace('"', "_").replace("\\", "_")
    utf8 = urllib.parse.quote(filename, safe="")
    return f'attachment; filename="{ascii_fb}"; filename*=UTF-8\'\'{utf8}'


@app.get("/download/{media_id}")
async def get_download_info(media_id: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media or media.get("status") != "completed":
        raise HTTPException(404, "Not found")
    qualities = media.get("qualities", [])
    iid = media["internal_id"]
    hls_local_borrado = bool(media.get("hls_local_borrado"))
    qs, ma = {}, {}
    for q in qualities:
        cp = CACHE_DIR / f"{media_id}_{q}.mp4"
        if cp.exists() and cp.stat().st_size > 1000:
            qs[q] = round(cp.stat().st_size / (1024 * 1024), 2)
            ma[q] = True
        else:
            qd = MEDIA_DIR / iid / q
            if not hls_local_borrado and qd.exists() and list(qd.glob("*.ts")):
                qs[q] = round(sum(f.stat().st_size for f in qd.glob("*.ts")) / (1024 * 1024), 2)
                ma[q] = False
            else:
                qs[q] = 0
                ma[q] = False
    return {
        "media_id": media_id,
        "original_filename": media.get("original_filename"),
        "display_name": media.get("display_name") or Path(
            media.get("original_filename", media_id)).stem,
        "qualities": qualities,
        "quality_sizes": qs,
        "mp4_available": ma,
        "mp4_ready": media.get("mp4_ready", False),
        "description": media.get("description", ""),
        "hls_local_borrado": hls_local_borrado,
    }


@app.get("/download/{media_id}/{quality}")
async def download_video_as_mp4(media_id: str, quality: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media or media.get("status") != "completed":
        raise HTTPException(404, "Not found")
    if quality not in media.get("qualities", []):
        raise HTTPException(404, "Quality not found")
    iid = media["internal_id"]
    base = (hls_core._sanitize(media.get("display_name"))
            or hls_core._sanitize(Path(media.get("original_filename", media_id)).stem)
            or media_id)
    dfn = f"{base}_{quality}.mp4"
    cp = CACHE_DIR / f"{media_id}_{quality}.mp4"
    if cp.exists() and cp.stat().st_size > 1000:
        return FileResponse(
            cp, media_type="video/mp4", filename=dfn,
            headers={"Content-Disposition": _content_disposition(dfn),
                     "Cache-Control": "public, max-age=86400"},
        )
    if media.get("hls_local_borrado"):
        raise HTTPException(
            404,
            "MP4 no disponible en caché y HLS local borrado (ToDus no permite generar MP4 desde aquí)"
        )
    mp4_path = await _run_blocking(generar_mp4_desde_hls, media_id, iid, quality)
    if not mp4_path or not Path(mp4_path).exists():
        raise HTTPException(503, "Generando MP4, intenta de nuevo")
    return FileResponse(
        mp4_path, media_type="video/mp4", filename=dfn,
        headers={"Content-Disposition": _content_disposition(dfn),
                 "Cache-Control": "public, max-age=86400"},
    )


@app.post("/download/pre-generate/{media_id}")
async def pre_generate_mp4(media_id: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media or media.get("status") != "completed":
        raise HTTPException(404, "Not found")
    if media.get("hls_local_borrado"):
        raise HTTPException(409, "HLS local borrado: no se pueden pre-generar MP4")
    iid = media["internal_id"]
    qualities = media.get("qualities", [])

    import threading
    def _gen():
        for q in qualities:
            try:
                generar_mp4_desde_hls(media_id, iid, q)
            except Exception:
                pass

    threading.Thread(target=_gen, daemon=True).start()
    return {"message": f"Pre-generando {len(qualities)} calidades",
            "qualities": qualities}

# ─────────────────────────────────────────────────────────────
# BORRAR MEDIA (local + ToDus)
# ─────────────────────────────────────────────────────────────
@app.delete("/media/{media_id}")
async def delete_media(media_id: str, source: str = "all"):
    media = await _run_blocking(read_db_one, media_id)
    if not media:
        raise HTTPException(404, "Media not found")

    deleted_todus = 0
    deleted_local = False

    if source in ("all", "todus"):
        if media.get("todus_hls"):
            try:
                deleted_todus = await _run_blocking(borrar_hls_de_todus, media)
            except Exception as e:
                print(f"[DELETE] ToDus: {e}")
            await _run_blocking(update_video, media_id, todus_hls=None, hls_source="local")

    if source in ("all", "local"):
        md = MEDIA_DIR / media["internal_id"]
        if md.exists():
            shutil.rmtree(md)
            deleted_local = True

        for cf in CACHE_DIR.glob(f"{media_id}_*.mp4"):
            try:
                cf.unlink()
            except Exception:
                pass

        await _run_blocking(update_video, media_id, hls_local_borrado=True)

    media_actualizado = await _run_blocking(read_db_one, media_id) or {}
    if not media_actualizado.get("todus_hls") and media_actualizado.get("hls_local_borrado"):
        await _run_blocking(delete_video_db, media_id)

    return {
        "message": "Eliminado",
        "todus_deleted": deleted_todus,
        "local_deleted": deleted_local,
    }

# ─────────────────────────────────────────────────────────────
# STORAGE
# ─────────────────────────────────────────────────────────────
@app.get("/storage")
async def get_storage():
    try:
        total_bytes = 0
        if MEDIA_DIR.exists():
            for f in MEDIA_DIR.rglob("*"):
                if f.is_file():
                    total_bytes += f.stat().st_size
        total_gb = total_bytes / (1024 ** 3)
        disk_total = 15
        disk_used = round(total_gb, 2)
        disk_free = round(disk_total - disk_used, 2)
        percent = round((disk_used / disk_total) * 100, 1) if disk_total else 0

        _, count = await _run_blocking(list_videos, 0)

        return {
            "total": disk_total,
            "used": disk_used,
            "free": disk_free,
            "percent": percent,
            "bytes": total_bytes,
            "video_count": count,
        }
    except Exception:
        return {"total": 15, "used": 0, "free": 15, "percent": 0,
                "bytes": 0, "video_count": 0}

# ─────────────────────────────────────────────────────────────
# SETTINGS — GET/POST
# ─────────────────────────────────────────────────────────────
@app.get("/api/settings/stream")
async def get_settings_stream():
    return {
        "compression_profile": RUNTIME.compression_profile,
        "calidades": list(RUNTIME.calidades),
        "guardar_en_todus": bool(RUNTIME.guardar_en_todus),
        "auto_reparar_todus": bool(RUNTIME.auto_reparar_todus),
        "subir_mp4_telegram": bool(RUNTIME.subir_mp4_telegram),
        "canal_mp4": RUNTIME.canal_mp4,
        "perfiles_disponibles": [
            {
                "id": k,
                "nombre": k,
                "descripcion": v["description"],
                "crf": v["crf"],
                "preset": v["preset"],
                "audio_bitrate": v["audio_bitrate"],
            }
            for k, v in COMPRESSION_PROFILES.items()
        ],
        "calidades_disponibles": [q["name"] for q in BASE_QUALITIES],
    }


@app.post("/api/settings/stream")
async def update_settings_stream(request: Request):
    payload = await request.json()
    try:
        compression_profile = payload.get("compression_profile")
        calidades = payload.get("calidades")
        guardar_en_todus = payload.get("guardar_en_todus")
        auto_reparar_todus = payload.get("auto_reparar_todus")
        subir_mp4_telegram = payload.get("subir_mp4_telegram")
        canal_mp4 = payload.get("canal_mp4")

        if (compression_profile is None and calidades is None
                and guardar_en_todus is None
                and auto_reparar_todus is None
                and subir_mp4_telegram is None and canal_mp4 is None):
            raise HTTPException(400, "Nada que actualizar")

        if compression_profile is not None:
            if compression_profile not in COMPRESSION_PROFILES:
                raise HTTPException(400, f"Perfil inválido: {compression_profile}")
            RUNTIME.compression_profile = compression_profile

        if calidades is not None:
            if not isinstance(calidades, list) or not calidades:
                raise HTTPException(400, "Selecciona al menos una calidad")
            validas = [q["name"] for q in BASE_QUALITIES]
            for c in calidades:
                if c not in validas:
                    raise HTTPException(400, f"Calidad inválida: {c}")
            RUNTIME.calidades = list(calidades)

        if guardar_en_todus is not None:
            RUNTIME.guardar_en_todus = bool(guardar_en_todus)

        if auto_reparar_todus is not None:
            RUNTIME.auto_reparar_todus = bool(auto_reparar_todus)

        if subir_mp4_telegram is not None:
            RUNTIME.subir_mp4_telegram = bool(subir_mp4_telegram)

        if canal_mp4 is not None:
            RUNTIME.canal_mp4 = os.environ.get(
                "HLS_CANAL_MP4", str(canal_mp4)
            ).strip()

        await _run_blocking(_save_config_ini)

        return {
            "status": "ok",
            "compression_profile": RUNTIME.compression_profile,
            "calidades": list(RUNTIME.calidades),
            "guardar_en_todus": bool(RUNTIME.guardar_en_todus),
            "auto_reparar_todus": bool(RUNTIME.auto_reparar_todus),
            "subir_mp4_telegram": bool(RUNTIME.subir_mp4_telegram),
            "canal_mp4": RUNTIME.canal_mp4,
            "message": "Configuración guardada",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


def _save_config_ini():
    """Guarda RUNTIME en config.ini con escritura atómica (.tmp + os.replace)."""
    import configparser
    candidates = [
        BASE_DIR / "config" / "config_stream.ini",
        BASE_DIR / "config_stream.ini",
        BASE_DIR / "config.ini",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        print("[SETTINGS] ⚠️ No se encontró config.ini")
        return
    try:
        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")
        if "STREAM" not in cfg:
            cfg["STREAM"] = {}
        if "TELEGRAM" not in cfg:
            cfg["TELEGRAM"] = {}
        cfg["STREAM"]["COMPRESSION_PROFILE"] = RUNTIME.compression_profile
        cfg["STREAM"]["CALIDADES"] = ", ".join(RUNTIME.calidades)
        cfg["STREAM"]["GUARDAR_EN_TODUS"] = "true" if RUNTIME.guardar_en_todus else "false"
        cfg["STREAM"]["AUTO_REPARAR_TODUS"] = "true" if RUNTIME.auto_reparar_todus else "false"
        cfg["TELEGRAM"]["SUBIR_MP4_TELEGRAM"] = "true" if RUNTIME.subir_mp4_telegram else "false"
        cfg["TELEGRAM"]["CANAL_MP4"] = RUNTIME.canal_mp4

        # Escritura atómica: escribe a .tmp y renombra (evita que el bot
        # lea el archivo a medio escribir)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            cfg.write(f)
        os.replace(tmp, path)
        print(f"[SETTINGS] 💾 Guardado en {path}")
    except Exception as e:
        print(f"[SETTINGS] ❌ No se pudo guardar {path}: {e}")

# ─────────────────────────────────────────────────────────────
# HOST — GET/POST (equivalente a /host en el bot)
# ─────────────────────────────────────────────────────────────
@app.get("/api/host")
async def get_host():
    todus = get_todus()
    return {
        "active": todus.default_host,
        "public_base": todus.public_base,
        "hls_prefix": todus.hls_prefix,
        "endpoints": {
            "stickers": todus.stickers_base,
            "stream": todus.stream_base,
        },
    }


@app.post("/api/host")
async def set_host(request: Request):
    payload = await request.json()
    nuevo = (payload.get("host") or "").strip().lower()
    if nuevo not in ("stream", "stickers"):
        raise HTTPException(400, "host debe ser 'stream' o 'stickers'")

    todus = get_todus()
    todus.default_host = nuevo

    # Persistir en config.ini (escritura atómica) para que sobreviva a reinicios
    try:
        import configparser
        candidates = [
            BASE_DIR / "config" / "config_stream.ini",
            BASE_DIR / "config_stream.ini",
            BASE_DIR / "config.ini",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path:
            cfg = configparser.ConfigParser()
            cfg.read(path, encoding="utf-8")
            if "TODUS" not in cfg:
                cfg["TODUS"] = {}
            cfg["TODUS"]["default_host"] = nuevo
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                cfg.write(f)
            os.replace(tmp, path)
            print(f"[HOST] 💾 default_host={nuevo} guardado en {path}")
    except Exception as e:
        print(f"[HOST] ❌ No se pudo guardar en config.ini: {e}")

    return {
        "status": "ok",
        "active": todus.default_host,
        "public_base": todus.public_base,
        "hls_prefix": todus.hls_prefix,
    }

# ─────────────────────────────────────────────────────────────
# TODUS: subir/borrar manual
# ─────────────────────────────────────────────────────────────
@app.post("/api/todus/upload/{media_id}")
async def subir_a_todus_endpoint(media_id: str, request: Request):
    media = await _run_blocking(read_db_one, media_id)
    if not media or media.get("status") != "completed":
        raise HTTPException(404, "Media not found or not completed")

    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")

    if media.get("todus_hls") and not force:
        return {"status": "already", "todus_hls": media["todus_hls"]}

    if media.get("hls_local_borrado"):
        raise HTTPException(409, "HLS local borrado: no se puede subir a ToDus")

    display = (media.get("display_name")
               or Path(media.get("original_filename", media_id)).stem)

    info = await _run_blocking(subir_hls_a_todus, media_id, display)
    if not info:
        raise HTTPException(500, "No se pudo subir a ToDus (revisa la consola)")
    await _run_blocking(update_video, media_id, todus_hls=info, hls_source="todus", hls_local_borrado=False)
    return {"status": "ok", "todus_hls": info}

@app.delete("/api/todus/{media_id}")
async def borrar_de_todus(media_id: str):
    media = await _run_blocking(read_db_one, media_id)
    if not media:
        raise HTTPException(404, "Media not found")
    if not media.get("todus_hls"):
        return {"status": "not_in_todus", "borrados": 0}

    borrados = await _run_blocking(borrar_hls_de_todus, media)
    await _run_blocking(update_video, media_id, todus_hls=None, hls_source="local")
    return {"status": "ok", "borrados": borrados}

# ─────────────────────────────────────────────────────────────
# DEBUG
# ─────────────────────────────────────────────────────────────
@app.get("/debug/db")
async def debug_db():
    info = {
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
        "db_size_mb": round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0,
        "media_dir_entries": len(list(MEDIA_DIR.iterdir())) if MEDIA_DIR.exists() else 0,
        "cache_mp4_entries": len(list(CACHE_DIR.iterdir())) if CACHE_DIR.exists() else 0,
        "cwd": os.getcwd(),
        "py_file": __file__,
    }
    try:
        info["count_by_status"] = await _run_blocking(count_by_status)
        _, total = await _run_blocking(list_videos, 0)
        info["total_videos"] = total
        todus = get_todus()
        info["todus"] = {
            "default_host": todus.default_host,
            "public_base": todus.public_base,
            "hls_prefix": todus.hls_prefix,
            "stickers_base": todus.stickers_base,
            "stream_base": todus.stream_base,
        }
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info

# ─────────────────────────────────────────────────────────────
# ARRANQUE
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import configparser
    from pathlib import Path

    BASE_DIR = Path(__file__).parent
    port = 24716

    try:
        cfg_path = next(
            (p for p in [
                BASE_DIR / "config" / "config_stream.ini",
                BASE_DIR / "config_stream.ini",
                BASE_DIR / "config.ini",
            ] if p.exists()),
            None,
        )
        if cfg_path:
            cfg = configparser.ConfigParser()
            cfg.read(cfg_path, encoding="utf-8")
            port = cfg.getint("STREAM", "SERVER_PORT", fallback=port)
    except Exception:
        pass

    todus = get_todus()
    print(f"[OK] Host activo: {todus.default_host} → {todus.public_base}")
    print(f"[OK] Ruta HLS: {todus.hls_prefix}")

    uvicorn.run("hls_server:app", host="0.0.0.0", port=port, reload=False)