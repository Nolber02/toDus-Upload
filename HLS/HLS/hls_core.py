"""
hls_core.py — Lógica HLS: ffmpeg, SQLite, ToDus, master/playlist, MP4.
Sin Telethon. Solo stdlib + requests + todus_helper.
"""

import os
import re
import json
import shutil
import sqlite3
import tempfile
import time
import subprocess
import unicodedata
import urllib.parse
import configparser
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from todus_hls_helper import ToDusHelper
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────
# RUTAS / DIRECTORIOS
# ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

MEDIA_DIR = BASE_DIR / "media"
CACHE_DIR = MEDIA_DIR / "cache_mp4"
DB_PATH = MEDIA_DIR / "video_database.sqlite3"

MEDIA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
# CARGA DE CONFIG.INI (TODUS)
# ─────────────────────────────────────────────────────────────
def _load_todus_config() -> dict:
    candidates = [
        BASE_DIR / "config" / "config_stream.ini",
        BASE_DIR / "config_stream.ini",
        BASE_DIR / "config.ini",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if not path:
        return {}
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    if "TODUS" not in cfg:
        return {}
    section = cfg["TODUS"]
    return {
        "namespace_prefix": section.get("namespace_prefix", "telegram"),
        "max_file_size_mb": section.getint("max_file_size_mb", fallback=2048),
        "default_host": section.get("default_host", "stickers").lower(),
        "stickers_base_url": section.get(
            "stickers_base_url", "https://stickers.todus.cu").rstrip("/"),
        "stream_base_url": section.get(
            "stream_base_url", "https://s3.todus.cu/stream").rstrip("/"),
        "hls_prefix": section.get("hls_prefix", "public/hls").strip("/"),
        "owner_id": cfg.getint("TELEGRAM", "owner_id", fallback=0)
                   if "TELEGRAM" in cfg else 0,
    }

_CFG_TODUS = _load_todus_config()

# ─────────────────────────────────────────────────────────────
# TODUS (helper + config expuestos)
# ─────────────────────────────────────────────────────────────
_todus = ToDusHelper(
    namespace_prefix=_CFG_TODUS.get("namespace_prefix", "telegram"),
    max_file_size_mb=_CFG_TODUS.get("max_file_size_mb", 2048),
    default_host=_CFG_TODUS.get("default_host", "stickers"),
    stickers_base_url=_CFG_TODUS.get(
        "stickers_base_url", "https://stickers.todus.cu"),
    stream_base_url=_CFG_TODUS.get(
        "stream_base_url", "https://s3.todus.cu/stream"),
    hls_prefix=_CFG_TODUS.get("hls_prefix", "public/hls"),
)


def get_todus() -> ToDusHelper:
    return _todus


def _hls_base_url(host: str = None) -> str:
    """URL pública del HLS activo (según host)."""
    base = _todus._public_base_for(host)
    return f"{base}/{_todus.hls_prefix}"


def _hls_full_prefix(host: str = None) -> str:
    """Prefijo S3 del HLS activo (siempre termina en '/')."""
    return f"{_todus.hls_prefix}/"

# ─────────────────────────────────────────────────────────────
# PERFILES DE COMPRESIÓN
# ─────────────────────────────────────────────────────────────
COMPRESSION_PROFILES = {
    "balanced": {
        "preset": "medium", "crf": 23, "audio_bitrate": "96k",
        "audio_codec": "aac", "audio_channels": 2, "audio_sample_rate": 48000,
        "bitrate_multiplier": 1.0,
        "description": "⚖️ Calidad/Tamaño equilibrado",
    },
    "small": {
        "preset": "slow", "crf": 26, "audio_bitrate": "64k",
        "audio_codec": "aac", "audio_channels": 2, "audio_sample_rate": 44100,
        "bitrate_multiplier": 0.7,
        "description": "📦 Archivos ~30% más pequeños",
    },
    "tiny": {
        "preset": "slower", "crf": 28, "audio_bitrate": "48k",
        "audio_codec": "aac", "audio_channels": 2, "audio_sample_rate": 44100,
        "bitrate_multiplier": 0.5,
        "description": "💾 Máxima compresión ~50% más pequeño",
    },
}

# ─────────────────────────────────────────────────────────────
# CALIDADES BASE
# ─────────────────────────────────────────────────────────────
BASE_QUALITIES = [
    {"name": "144p",  "height": 144,  "base_bitrate": "150k",  "bandwidth": 150000},
    {"name": "240p",  "height": 240,  "base_bitrate": "250k",  "bandwidth": 250000},
    {"name": "360p",  "height": 360,  "base_bitrate": "400k",  "bandwidth": 400000},
    {"name": "480p",  "height": 480,  "base_bitrate": "600k",  "bandwidth": 600000},
    {"name": "720p",  "height": 720,  "base_bitrate": "800k",  "bandwidth": 800000},
    {"name": "1080p", "height": 1080, "base_bitrate": "1500k", "bandwidth": 1500000},
]

# ─────────────────────────────────────────────────────────────
# RUNTIME CONFIG (mutable desde settings)
# ─────────────────────────────────────────────────────────────
class Runtime:
    def __init__(self):
        # NUEVOS VALORES POR DEFECTO
        self.compression_profile = "small"
        self.calidades = ["360p", "480p"]
        self.guardar_en_todus = True
        self.auto_reparar_todus = _load_auto_reparar_todus()
        self.subir_mp4_telegram = False
        self.canal_mp4 = os.environ.get("HLS_CANAL_MP4", "").strip()


def _load_auto_reparar_todus() -> bool:
    candidates = [
        BASE_DIR / "config" / "config_stream.ini",
        BASE_DIR / "config_stream.ini",
        BASE_DIR / "config.ini",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if not path:
        return False
    cfg = configparser.ConfigParser()
    try:
        cfg.read(path, encoding="utf-8")
        return cfg.getboolean("STREAM", "AUTO_REPARAR_TODUS", fallback=False)
    except (configparser.Error, ValueError):
        return False

RUNTIME = Runtime()

# ─────────────────────────────────────────────────────────────
# SQLITE (WAL) — helpers
# ─────────────────────────────────────────────────────────────
def _init_sqlite():
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                media_id     TEXT PRIMARY KEY,
                data         TEXT NOT NULL,
                upload_time  TEXT,
                status       TEXT,
                updated_at   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON videos(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_time ON videos(upload_time DESC)")
        conn.commit()

@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
    finally:
        conn.close()

def read_db_one(media_id: str):
    try:
        with _db() as conn:
            row = conn.execute("SELECT data FROM videos WHERE media_id = ?", (media_id,)).fetchone()
        return json.loads(row[0]) if row else None
    except Exception as e:
        print(f"[DB] read_db_one({media_id}): {e}")
        return None

def insert_video(media_id: str, data: dict) -> bool:
    try:
        now = datetime.utcnow().isoformat()
        data.setdefault("media_id", media_id)
        with _db() as conn:
            conn.execute("""
                INSERT INTO videos (media_id, data, upload_time, status, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(media_id) DO NOTHING
            """, (
                media_id,
                json.dumps(data, default=str, ensure_ascii=False),
                data.get("upload_time"),
                data.get("status"),
                now,
            ))
            conn.commit()
        return True
    except Exception as e:
        print(f"[DB] insert_video({media_id}): {e}")
        return False

def update_video(media_id: str, **fields) -> bool:
    if not fields:
        return False
    try:
        now = datetime.utcnow().isoformat()
        with _db() as conn:
            row = conn.execute("SELECT data FROM videos WHERE media_id = ?", (media_id,)).fetchone()
            if not row:
                return False
            current = json.loads(row[0])
            current.update(fields)
            conn.execute(
                "UPDATE videos SET data = ?, status = ?, updated_at = ? WHERE media_id = ?",
                (
                    json.dumps(current, default=str, ensure_ascii=False),
                    current.get("status"),
                    now,
                    media_id,
                ),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[DB] update_video({media_id}): {e}")
        return False

def _clear_field(media_id: str, field: str):
    try:
        with _db() as conn:
            row = conn.execute("SELECT data FROM videos WHERE media_id = ?", (media_id,)).fetchone()
            if not row:
                return
            data = json.loads(row[0])
            data.pop(field, None)
            conn.execute(
                "UPDATE videos SET data = ?, updated_at = ? WHERE media_id = ?",
                (json.dumps(data, default=str, ensure_ascii=False),
                 datetime.utcnow().isoformat(),
                 media_id),
            )
            conn.commit()
    except Exception:
        pass

def delete_video_db(media_id: str) -> bool:
    try:
        with _db() as conn:
            conn.execute("DELETE FROM videos WHERE media_id = ?", (media_id,))
            conn.commit()
        return True
    except Exception as e:
        print(f"[DB] delete_video_db({media_id}): {e}")
        return False

def list_videos(limit: int = 0, offset: int = 0):
    try:
        with _db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            q = "SELECT media_id, data FROM videos ORDER BY upload_time DESC"
            params = []
            if limit > 0:
                q += " LIMIT ? OFFSET ?"
                params = [limit, offset]
            rows = conn.execute(q, params).fetchall()
        return [(mid, json.loads(d)) for mid, d in rows], total
    except Exception as e:
        print(f"[DB] list_videos: {e}")
        return [], 0

def count_by_status() -> dict:
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM videos GROUP BY status"
            ).fetchall()
        return {s or "unknown": c for s, c in rows}
    except Exception as e:
        print(f"[DB] count_by_status: {e}")
        return {}

_init_sqlite()

# ─────────────────────────────────────────────────────────────
# UTILIDADES DE TEXTO
# ─────────────────────────────────────────────────────────────
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags (iOS)
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map symbols
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
    "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
    "\U00002700-\U000027BF"  # Dingbats
    "\U00002600-\U000026FF"  # Miscellaneous Symbols
    "\U00002B00-\U00002BFF"  # Miscellaneous Symbols and Arrows
    "\U0000FE00-\U0000FE0F"  # Variation Selectors
    "\U0001F000-\U0001F02F"  # Mahjong
    "\U0001F0A0-\U0001F0FF"  # Playing Cards
    "\U00002300-\U000023FF"  # Misc Technical (⏰ ⏳ ⏩ etc.)
    "\U0000200D"             # Zero Width Joiner
    "\U0000FE0F"             # Variation Selector-16
    "\U000020E3"             # Combining Enclosing Keycap
    "\U00002000-\U0000206F"  # General Punctuation (— – … etc.)
    "\U00002190-\U000021FF"  # Arrows
    "\U000025A0-\U000025FF"  # Geometric Shapes
    "\U00002500-\U0000257F"  # Box Drawing
    "\U00002600-\U000027BF"  # Misc symbols + Dingbats
    "\U00002900-\U0000297F"  # Supplemental Arrows-B
    "\U00002B00-\U00002BFF"  # Misc Symbols and Arrows
    "\U00003030"             # Wavy Dash
    "\U0000303D"             # Part Alternation Mark
    "\U00003297"             # Circled Ideograph Congratulation
    "\U00003299"             # Circled Ideograph Secret
    "\U0001F004"             # Mahjong Red Dragon
    "\U0001F0CF"             # Playing Card Black Joker
    "]+",
    flags=re.UNICODE,
)


def _limpiar_emoji_y_simbolos(texto: str) -> str:
    if not texto:
        return ""
    texto = _EMOJI_PATTERN.sub(" ", texto)
    texto = re.sub(r"[\uFE00-\uFE0F\u200B-\u200F\u202A-\u202E]", "", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _sanitize(name: str, max_len: int = 120) -> str:
    if not name:
        return ""
    name = name.replace("/", "-")
    name = re.sub(r'\b(?:https?|ftp)://\S+', ' ', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(?:t\.me|www\.)\S+', ' ', name, flags=re.IGNORECASE)
    name = re.sub(r'(?<!\w)@[\w]+', ' ', name)
    name = re.sub(r'(?<!\w)#[\w]+', ' ', name)
    name = re.sub(r'\b\S+@\S+\.\S+\b', ' ', name)
    name = _limpiar_emoji_y_simbolos(name)
    name = unicodedata.normalize('NFKD', name)
    out = []
    for c in name:
        if unicodedata.category(c).startswith('M'):
            continue
        if c.isascii() and (c.isalnum() or c in (' ', '-', '_', '.')):
            out.append(c)
    name = ''.join(out)
    name = re.sub(r'\s+', ' ', name).strip().strip('. ')
    name = re.sub(r'[.\-]{2,}', lambda m: m.group(0)[0], name)
    return name[:max_len].strip()


def _safe_folder(name: str, max_len: int = 80) -> str:
    n = _sanitize(name, max_len=max_len) or "video"
    n = n.replace(" ", "_")
    n = n.strip("._-") or "video"
    n = re.sub(r'_+', '_', n)
    return n[:max_len] or "video"

# ─────────────────────────────────────────────────────────────
# FFMPEG HELPERS
# ─────────────────────────────────────────────────────────────
def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"

def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"

def _probe_duration(path: Path) -> float:
    cmd = [
        _ffprobe(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path.absolute()),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
        return float(r.stdout.strip()) if r.stdout else 60.0
    except Exception:
        return 60.0

# ─────────────────────────────────────────────────────────────
# BORRAR HLS LOCAL
# ─────────────────────────────────────────────────────────────
def _borrar_hls_local(internal_id: str) -> int:
    root = MEDIA_DIR / internal_id
    if not root.exists():
        return 0
    borrados = 0
    try:
        for f in root.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".ts", ".m3u8"):
                try:
                    f.unlink()
                    borrados += 1
                except Exception:
                    pass
        for d in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        print(f"[HLS] 🗑️ {internal_id}: {borrados} archivos HLS locales borrados")
    except Exception as e:
        print(f"[HLS] ⚠️ _borrar_hls_local({internal_id}): {e}")
    return borrados

# ─────────────────────────────────────────────────────────────
# GENERACIÓN DE HLS
# ─────────────────────────────────────────────────────────────
def generate_hls(media_id: str, internal_id: str, temp_path: Path,
                 original_filename: str, description: str = ""):
    try:
        profile = COMPRESSION_PROFILES.get(
            RUNTIME.compression_profile, COMPRESSION_PROFILES["small"]
        )
        multiplier = profile["bitrate_multiplier"]
        output_dir = MEDIA_DIR / internal_id
        output_dir.mkdir(parents=True, exist_ok=True)

        if not temp_path.exists():
            raise Exception(f"Archivo no encontrado: {temp_path}")

        total_duration = _probe_duration(temp_path)

        qualities = []
        for q in BASE_QUALITIES:
            if q["name"] not in RUNTIME.calidades:
                continue
            base_k = int(q["base_bitrate"].replace("k", ""))
            adj_k = max(80, int(base_k * multiplier))
            qualities.append({
                "name": q["name"],
                "height": q["height"],
                "bitrate": f"{adj_k}k",
                "bandwidth": adj_k * 1000,
            })
        if not qualities:
            qualities = [
                {"name": q["name"], "height": q["height"],
                 "bitrate": q["base_bitrate"], "bandwidth": q["bandwidth"]}
                for q in BASE_QUALITIES
            ]

        for q in qualities:
            (output_dir / q["name"]).mkdir(exist_ok=True)

        def _progress(idx, total_q, t, dur, stage, fps=0):
            if total_q > 0 and dur > 0:
                pct = min(100, int(((idx / total_q) + (t / dur) / total_q) * 100))
            else:
                pct = 0
            if idx >= total_q:
                pct = 100
            update_video(media_id, ffmpeg_progress={
                "calidad_actual": idx,
                "total_calidades": total_q,
                "calidad_nombre": qualities[idx]["name"] if idx < len(qualities) else "Finalizado",
                "tiempo_actual": t,
                "total_duracion": dur,
                "porcentaje_global": pct,
                "etapa": stage,
                "fps": round(fps, 1) if fps else 0,
                "actualizado": datetime.utcnow().isoformat(),
            })

        for idx, quality in enumerate(qualities):
            q_name = quality["name"]
            q_dir = output_dir / q_name
            _progress(idx, len(qualities), 0, total_duration, f"Iniciando {q_name}")

            playlist = q_dir / "playlist.m3u8"
            seg_pattern = str((q_dir / "segment%03d.ts").absolute())

            cmd = [
                _ffmpeg(), "-i", str(temp_path.absolute()),
                "-c:v", "libx264",
                "-threads", "0",
                "-preset", profile["preset"],
                "-crf", str(profile["crf"]),
                "-b:v", quality["bitrate"],
                "-maxrate", f"{int(quality['bitrate'].replace('k','')) * 12}00",
                "-bufsize", f"{int(quality['bitrate'].replace('k','')) * 24}00",
                "-profile:v", "main", "-level", "3.1",
                "-vf", f"scale=-2:{quality['height']}",
                "-c:a", profile["audio_codec"],
                "-b:a", profile["audio_bitrate"],
                "-ar", str(profile["audio_sample_rate"]),
                "-ac", str(profile["audio_channels"]),
                "-hls_time", "10",
                "-hls_list_size", "0",
                "-hls_segment_filename", seg_pattern,
                "-start_number", "0",
                "-hls_playlist_type", "vod",
                "-progress", "pipe:1", "-y",
                str(playlist.absolute()),
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=os.environ.copy(),
            )
            last_t, last_fps = 0.0, 0.0
            for line in proc.stdout:
                line = line.strip()
                if "out_time_ms" in line:
                    try:
                        t = int(line.split("=")[1]) / 1_000_000
                        if t > last_t:
                            last_t = t
                            pct = min(100, int((t / total_duration) * 100))
                            _progress(idx, len(qualities), t, total_duration,
                                      f"Procesando {q_name} ({pct}%)", last_fps)
                    except Exception:
                        pass

            rc = proc.wait()
            if rc != 0:
                simple = [
                    _ffmpeg(), "-i", str(temp_path.absolute()),
                    "-c:v", "libx264", "-preset", "veryfast",
                    "-b:v", quality["bitrate"],
                    "-vf", f"scale=-2:{quality['height']}",
                    "-c:a", "aac", "-b:a", profile["audio_bitrate"],
                    "-ar", str(profile["audio_sample_rate"]),
                    "-ac", str(profile["audio_channels"]),
                    "-hls_time", "10", "-hls_list_size", "0",
                    "-hls_segment_filename", seg_pattern,
                    "-start_number", "0", "-hls_playlist_type", "vod",
                    "-progress", "pipe:1", "-y",
                    str(playlist.absolute()),
                ]
                p2 = subprocess.Popen(
                    simple, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=os.environ.copy(),
                )
                for _ in p2.stdout:
                    pass
                if p2.wait() != 0:
                    raise Exception(f"ffmpeg falló en {q_name}")

            _progress(idx + 1, len(qualities), total_duration, total_duration,
                      f"{q_name} completado")

        master = output_dir / "master.m3u8"
        with open(master, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:4\n")
            f.write(f"# H.264 - Perfil: {RUNTIME.compression_profile.upper()}\n")
            for q in qualities:
                f.write(f'#EXT-X-STREAM-INF:BANDWIDTH={q["bandwidth"]},'
                        f'RESOLUTION=0x{q["height"]},CODECS="avc1.42E01E,mp4a.40.2"\n')
                f.write(f'{q["name"]}/playlist.m3u8\n')

        quality_sizes = {}
        for q in qualities:
            qd = output_dir / q["name"]
            ts = list(qd.glob("*.ts"))
            quality_sizes[q["name"]] = (
                round(sum(f.stat().st_size for f in ts) / (1024 * 1024), 2)
                if ts else 0
            )

        try:
            temp_path.unlink()
        except Exception:
            pass

        update_video(
            media_id,
            status="completed",
            qualities=[q["name"] for q in qualities],
            bitrates=[q["bitrate"] for q in qualities],
            compression_profile=RUNTIME.compression_profile,
            codec="H.264 (Compatible)",
            master_playlist=str(master),
            completed_at=datetime.utcnow().isoformat(),
            description=description,
            audio={
                "bitrate": profile["audio_bitrate"],
                "sample_rate": profile["audio_sample_rate"],
                "codec": profile["audio_codec"],
                "channels": profile["audio_channels"],
            },
            quality_sizes=quality_sizes,
            total_hls_size_mb=round(sum(quality_sizes.values()), 2),
            mp4_ready=False,
            mp4_sizes={},
        )
        _clear_field(media_id, "ffmpeg_progress")

        subido_a_todus = False
        if RUNTIME.guardar_en_todus:
            info = subir_hls_a_todus(media_id, original_filename)
            if info:
                update_video(media_id, todus_hls=info, hls_source="todus")
                subido_a_todus = True
            else:
                update_video(media_id, hls_source="local")
        else:
            update_video(media_id, hls_source="local")

        for q in qualities:
            try:
                generar_mp4_desde_hls(media_id, internal_id, q["name"])
            except Exception:
                pass
        update_video(media_id, mp4_ready=True, mp4_sizes=_mp4_sizes(media_id, qualities))
        update_video(media_id, hls_local_borrado=False)

    except Exception as e:
        update_video(media_id, status="error", error=str(e))
        _clear_field(media_id, "ffmpeg_progress")
        try:
            if temp_path and temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        print(f"[HLS] ❌ {media_id}: {e}")


def _mp4_sizes(media_id: str, qualities: list) -> dict:
    out = {}
    for q in qualities:
        name = q["name"] if isinstance(q, dict) else q
        cp = CACHE_DIR / f"{media_id}_{name}.mp4"
        out[name] = (
            round(cp.stat().st_size / (1024 * 1024), 2)
            if cp.exists() and cp.stat().st_size > 1000 else 0
        )
    return out

# ─────────────────────────────────────────────────────────────
# MP4 DESDE HLS
# ─────────────────────────────────────────────────────────────
def generar_mp4_desde_hls(media_id: str, internal_id: str, quality: str,
                          force: bool = False):
    cache_path = CACHE_DIR / f"{media_id}_{quality}.mp4"
    if cache_path.exists() and not force and cache_path.stat().st_size > 1000:
        return str(cache_path)

    q_dir = MEDIA_DIR / internal_id / quality
    if not q_dir.exists():
        return None
    ts_files = sorted(q_dir.glob("*.ts"))
    if not ts_files:
        return None

    tmp_mp4 = q_dir / f"_temp_{quality}.mp4"
    if tmp_mp4.exists():
        try:
            tmp_mp4.unlink()
        except Exception:
            pass

    success = False

    playlist = q_dir / "playlist.m3u8"
    if playlist.exists():
        cmd = [
            _ffmpeg(), "-protocol_whitelist", "file,http,https,tcp,tls",
            "-i", str(playlist.absolute()),
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", "-y",
            str(tmp_mp4.absolute()),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               env=os.environ.copy(), timeout=600)
            if r.returncode == 0 and tmp_mp4.exists() and tmp_mp4.stat().st_size > 1000:
                success = True
        except subprocess.TimeoutExpired:
            pass

    if not success:
        concat = q_dir / "_concat.txt"
        with open(concat, "w") as f:
            for ts in ts_files:
                f.write(f"file '{ts.name}'\n")
        cmd = [
            _ffmpeg(), "-f", "concat", "-safe", "0",
            "-i", str(concat.absolute()),
            "-c", "copy", "-movflags", "+faststart", "-y",
            str(tmp_mp4.absolute()),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               env=os.environ.copy(), timeout=600,
                               cwd=str(q_dir.absolute()))
            if r.returncode == 0 and tmp_mp4.exists() and tmp_mp4.stat().st_size > 1000:
                success = True
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                concat.unlink()
            except Exception:
                pass

    if not success:
        try:
            with open(tmp_mp4, "wb") as out:
                for ts in ts_files:
                    with open(ts, "rb") as inp:
                        out.write(inp.read())
            success = tmp_mp4.exists() and tmp_mp4.stat().st_size > 1000
        except Exception:
            success = False

    if not success:
        try:
            tmp_mp4.unlink()
        except Exception:
            pass
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        try:
            cache_path.unlink()
        except Exception:
            pass
    shutil.move(str(tmp_mp4), str(cache_path))
    return str(cache_path)

# ─────────────────────────────────────────────────────────────
# SUBIR HLS A TODUS
# ─────────────────────────────────────────────────────────────
def subir_hls_a_todus(media_id: str, display_name: str) -> dict:
    todus = get_todus()
    media = read_db_one(media_id)
    if not media:
        return {}
    internal_id = media.get("internal_id")
    if not internal_id:
        return {}

    local_root = MEDIA_DIR / internal_id
    if not local_root.exists():
        return {}

    carpeta_video = _safe_folder(
        display_name or media.get("original_filename") or media_id)
    carpeta_video = f"{carpeta_video}__{media_id[-6:]}"

    active_host = todus.default_host

    subidos = 0
    errores = []

    def _subir(local: Path, path_rel: str, name: str):
        nonlocal subidos
        try:
            res = todus.upload_to_user(
                owner_id=0, uploader_id=0,
                local_path=str(local),
                path=path_rel,
                original_name=name,
                host=active_host,
            )
            if res.success:
                subidos += 1
            else:
                errores.append(f"{name}: {res.error}")
        except Exception as e:
            errores.append(f"{name}: {e}")

    qualities_ok = []
    for q in media.get("qualities", []):
        q_dir = local_root / q
        if not q_dir.exists():
            print(f"no existe: {q_dir}")
            continue
        carpeta_calidad = f"{carpeta_video}_{q}"
        path_calidad = f"{carpeta_video}/{carpeta_calidad}"

        playlist = q_dir / "playlist.m3u8"
        if playlist.exists():
            _subir(playlist, path_calidad, "playlist.m3u8")
        for ts in sorted(q_dir.glob("*.ts")):
            _subir(ts, path_calidad, ts.name)
            time.sleep(0.05)
        if playlist.exists():
            qualities_ok.append(q)

    if not qualities_ok:
        print(f"[TODUS-HLS] ❌ {media_id}: no se subió ninguna calidad")
        return {}

    base_url = f"{_hls_base_url(active_host)}/{urllib.parse.quote(carpeta_video)}"
    lines = ["#EXTM3U", "#EXT-X-VERSION:4"]
    for q in qualities_ok:
        bw = next((x["bandwidth"] for x in BASE_QUALITIES if x["name"] == q), 800000)
        height = int(str(q).rstrip("p"))
        cc = urllib.parse.quote(f"{carpeta_video}_{q}")
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION=0x{height},'
            f'CODECS="avc1.42E01E,mp4a.40.2"'
        )
        lines.append(f"{base_url}/{cc}/playlist.m3u8")
    master_text = "\n".join(lines) + "\n"

    tmp_master = Path(tempfile.gettempdir()) / f"master_{media_id}.m3u8"
    try:
        tmp_master.write_text(master_text, encoding="utf-8")
        _subir(tmp_master, carpeta_video, "master.m3u8")
    finally:
        try:
            tmp_master.unlink()
        except Exception:
            pass

    if errores:
        print(f"[TODUS-HLS] ⚠️ {media_id}: {len(errores)} errores (primeros 3): {errores[:3]}")
    print(f"[TODUS-HLS] ✅ {media_id} [{active_host}]: "
          f"{subidos} archivos → {base_url}")

    return {
        "base_url": base_url,
        "prefix": carpeta_video,
        "carpeta_video": carpeta_video,
        "qualities": qualities_ok,
        "archivos_subidos": subidos,
        "errores": errores[:10],
        "host": active_host,
    }

# ─────────────────────────────────────────────────────────────
# BORRAR HLS DE TODUS
# ─────────────────────────────────────────────────────────────
def borrar_hls_de_todus(media: dict, max_workers: int = 8) -> int:
    todus = get_todus()
    info = (media or {}).get("todus_hls") or {}
    prefix = info.get("prefix")
    if not prefix:
        return 0

    p = prefix.strip("/")
    for junk in ("public/hls/", "hls/"):
        if p.startswith(junk):
            p = p[len(junk):]
            break

    hosts_a_probar = [info.get("host") or todus.default_host]
    for h in ("stickers", "stream"):
        if h not in hosts_a_probar:
            hosts_a_probar.append(h)

    total_borrados = 0
    for host in hosts_a_probar:
        full_prefix = f"{todus.hls_prefix}/{p}/"
        try:
            files = todus.list_prefix(full_prefix, host=host)
            keys = [f.get("Key") for f in files if f.get("Key")]
            if not keys:
                continue

            borrados = 0

            def _del(k: str, _h=host) -> bool:
                try:
                    return bool(todus._client_for(_h).delete(k))
                except Exception as e:
                    print(f"[TODUS-HLS] ⚠️ delete({k}) [{_h}]: {e}")
                    return False

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(_del, k) for k in keys]
                for fut in as_completed(futures):
                    if fut.result():
                        borrados += 1

            print(f"[TODUS-HLS] 🗑️ [{host}] {full_prefix}: "
                  f"{borrados}/{len(keys)} archivos borrados")
            total_borrados += borrados

            if borrados > 0 and host == hosts_a_probar[0]:
                break
        except Exception as e:
            print(f"[TODUS-HLS] ⚠️ borrar_hls_de_todus({full_prefix}) [{host}]: {e}")

    return total_borrados

# ─────────────────────────────────────────────────────────────
# MASTER TEXT (URLs absolutas a ToDus)
# ─────────────────────────────────────────────────────────────
def build_master_text(media: dict, proxy_base: str = None) -> str | None:
    info = (media or {}).get("todus_hls") or {}
    if not info.get("base_url") or not info.get("qualities"):
        return None

    base = info["base_url"]
    carpeta_video = info.get("carpeta_video", "")
    lines = ["#EXTM3U", "#EXT-X-VERSION:4"]
    for q in info["qualities"]:
        bw = next((x["bandwidth"] for x in BASE_QUALITIES if x["name"] == q), 800000)
        height = int(str(q).rstrip("p"))
        cc = urllib.parse.quote(f"{carpeta_video}_{q}")
        playlist_url = f"{base}/{cc}/playlist.m3u8"

        if proxy_base:
            safe_url = urllib.parse.quote(playlist_url, safe="")
            playlist_url = f"{proxy_base}/proxy?url={safe_url}"

        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION=0x{height},'
            f'CODECS="avc1.42E01E,mp4a.40.2"'
        )
        lines.append(playlist_url)
    return "\n".join(lines) + "\n"

# ─────────────────────────────────────────────────────────────
# VERIFICACIÓN MASIVA EN TODUS
# ─────────────────────────────────────────────────────────────
def verificar_todus_video(media: dict) -> dict:
    todus = get_todus()
    media_id = media.get("media_id", "?")
    info = media.get("todus_hls") or {}
    carpeta = info.get("carpeta_video")
    host = info.get("host") or todus.default_host

    result = {
        "media_id": media_id,
        "display_name": media.get("display_name") or media.get("original_filename") or media_id,
        "en_db": bool(info),
        "en_bucket": False,
        "master_existe": False,
        "archivos_bucket": 0,
        "calidades_db": list(info.get("qualities") or []),
        "calidades_ok": [],
        "calidades_faltantes": [],
        "base_url": info.get("base_url"),
        "carpeta_video": carpeta,
        "host": host,
        "estado": "desconocido",
    }

    if not carpeta:
        result["estado"] = "sin_info"
        return result

    try:
        prefix = f"{todus.hls_prefix}/{carpeta}/"
        master_key = f"{prefix}master.m3u8"
        result["master_existe"] = todus.exists(master_key, host=host)

        archivos = todus.list_prefix(prefix, host=host, raise_on_error=True)
        result["archivos_bucket"] = len(archivos)
        keys = {f.get("Key", "") for f in archivos}
        result["en_bucket"] = bool(keys) and result["master_existe"]

        archivos_faltantes = []
        for q in result["calidades_db"]:
            prefix_q = f"{prefix}{carpeta}_{q}/"
            playlist = f"{prefix_q}playlist.m3u8"
            segmentos = {
                f"{prefix_q}{f.name}"
                for f in sorted((MEDIA_DIR / (media.get("internal_id") or "") / q).glob("*.ts"))
            }
            playlist_local = MEDIA_DIR / (media.get("internal_id") or "") / q / "playlist.m3u8"
            requeridos_locales = set(segmentos)
            if playlist_local.is_file():
                requeridos_locales.add(playlist)
            faltantes = requeridos_locales - keys
            tiene_playlist = playlist in keys
            tiene_segmentos = any(
                k.startswith(prefix_q) and k.endswith(".ts") for k in keys
            )
            archivos_faltantes.extend(sorted(faltantes))
            if tiene_playlist and tiene_segmentos and not faltantes:
                result["calidades_ok"].append(q)
            else:
                result["calidades_faltantes"].append(q)

        result["archivos_faltantes"] = archivos_faltantes

        if result["en_bucket"] and not result["calidades_faltantes"]:
            result["estado"] = "ok"
        elif result["en_bucket"] or result["archivos_bucket"] > 0:
            result["estado"] = "parcial"
        else:
            result["estado"] = "ausente"
    except Exception as e:
        result["error"] = str(e)
        result["estado"] = "error"

    return result


def reparar_hls_todus(media_id: str) -> dict:
    """Sube a ToDus los archivos HLS locales que falten, conservando su carpeta y host."""
    media = read_db_one(media_id)
    if not media or media.get("status") != "completed":
        return {"estado": "no_disponible"}
    if media.get("hls_local_borrado"):
        return {"estado": "sin_archivos_locales"}

    info = media.get("todus_hls") or {}
    display_name = (media.get("display_name")
                    or media.get("original_filename") or media_id)
    if not info.get("carpeta_video"):
        info = subir_hls_a_todus(media_id, display_name)
        if not info:
            return {"estado": "error", "error": "No se pudo subir el HLS a ToDus"}
        update_video(media_id, todus_hls=info, hls_source="todus")
        return {"estado": "subido", "archivos_subidos": info.get("archivos_subidos", 0)}

    todus = get_todus()
    carpeta = info["carpeta_video"]
    host = info.get("host") or todus.default_host
    prefix = f"{todus.hls_prefix}/{carpeta}/"
    internal_id = media.get("internal_id")
    if not internal_id:
        return {"estado": "sin_archivos_locales"}
    local_root = MEDIA_DIR / internal_id

    try:
        archivos_bucket = todus.list_prefix(prefix, host=host, raise_on_error=True)
        keys = {f.get("Key") for f in archivos_bucket if f.get("Key")}
    except Exception as e:
        return {"estado": "error", "error": str(e)}

    subidos = 0
    errores = []
    calidades_ok = []

    def subir_archivo(local_path: Path, path: str, filename: str):
        nonlocal subidos
        try:
            resultado = todus.upload_to_user(
                owner_id=0, uploader_id=0, local_path=str(local_path),
                path=path, original_name=filename, host=host,
            )
            if resultado.success:
                keys.add(resultado.key)
                subidos += 1
            else:
                errores.append(f"{filename}: {resultado.error or 'error de subida'}")
        except Exception as e:
            errores.append(f"{filename}: {e}")

    master_key = f"{prefix}master.m3u8"
    if master_key not in keys:
        media_para_master = dict(media)
        media_para_master["todus_hls"] = {
            **info,
            "qualities": list(media.get("qualities", [])),
        }
        master_text = build_master_text(media_para_master)
        if master_text:
            tmp_master = Path(tempfile.gettempdir()) / f"master_{media_id}.m3u8"
            try:
                tmp_master.write_text(master_text, encoding="utf-8")
                subir_archivo(tmp_master, carpeta, "master.m3u8")
            finally:
                try:
                    tmp_master.unlink()
                except OSError:
                    pass
        else:
            errores.append("No se pudo generar master.m3u8")

    for quality in media.get("qualities", []):
        quality_dir = local_root / quality
        remote_quality_dir = f"{carpeta}_{quality}"
        playlist_key = f"{prefix}{remote_quality_dir}/playlist.m3u8"
        remote_quality_prefix = f"{prefix}{remote_quality_dir}/"
        remote_has_segments = any(
            key.startswith(remote_quality_prefix) and key.endswith(".ts")
            for key in keys
        )
        if not quality_dir.is_dir():
            if playlist_key in keys and remote_has_segments:
                calidades_ok.append(quality)
            else:
                errores.append(f"No existe la calidad local {quality}")
            continue

        playlist = quality_dir / "playlist.m3u8"
        local_files = ([playlist] if playlist.is_file() else [])
        local_files.extend(sorted(quality_dir.glob("*.ts")))
        for local_file in local_files:
            key = f"{prefix}{remote_quality_dir}/{local_file.name}"
            if key not in keys:
                subir_archivo(local_file, f"{carpeta}/{remote_quality_dir}", local_file.name)
                time.sleep(0.05)

        tiene_segmentos = any(
            key.startswith(remote_quality_prefix) and key.endswith(".ts")
            for key in keys
        )
        if playlist_key in keys and tiene_segmentos:
            calidades_ok.append(quality)
        elif not playlist.is_file() and playlist_key not in keys:
            errores.append(f"Falta playlist local y remota para {quality}")
        elif not list(quality_dir.glob("*.ts")) and not tiene_segmentos:
            errores.append(f"No hay segmentos locales ni remotos para {quality}")

    if subidos:
        update_video(media_id, todus_hls=info, hls_source="todus")

    master_ok = master_key in keys
    if master_ok and len(calidades_ok) == len(media.get("qualities", [])):
        estado = "ok"
    elif keys:
        estado = "parcial"
    else:
        estado = "ausente"
    if errores:
        return {"estado": "parcial", "archivos_subidos": subidos, "errores": errores[:10]}
    return {"estado": estado, "archivos_subidos": subidos}


def verificar_todos_todus(solo_marcados: bool = False, sync_db: bool = False) -> dict:
    videos, total = list_videos(limit=0)
    resultados = []

    for media_id, media in videos:
        if solo_marcados and not media.get("todus_hls"):
            continue
        resultados.append(verificar_todus_video(media))

    resumen = {
        "total_db": total,
        "revisados": len(resultados),
        "ok": sum(1 for r in resultados if r["estado"] == "ok"),
        "parcial": sum(1 for r in resultados if r["estado"] == "parcial"),
        "ausente": sum(1 for r in resultados if r["estado"] == "ausente"),
        "sin_info": sum(1 for r in resultados if r["estado"] == "sin_info"),
        "error": sum(1 for r in resultados if r["estado"] == "error"),
    }

    if sync_db:
        corregidos = 0
        for r in resultados:
            mid = r["media_id"]
            if r["estado"] == "ausente":
                update_video(mid, todus_hls=None, hls_source="local")
                corregidos += 1
            elif r["estado"] == "parcial" and r["calidades_ok"]:
                media = read_db_one(mid) or {}
                info = dict(media.get("todus_hls") or {})
                info["qualities"] = r["calidades_ok"]
                update_video(mid, todus_hls=info)
                corregidos += 1
        resumen["db_corregidos"] = corregidos

    return {"resumen": resumen, "resultados": resultados}