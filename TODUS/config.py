"""
config.py — Carga de config.ini y variables de entorno (.env > config.ini).
"""
import os
import configparser

from dotenv import load_dotenv

from todus_helper import ToDusHelper

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_HERE, "config.ini")
DB_FILE     = os.path.join(_HERE, "users.db")
LOCAL_DIR   = os.path.join(_HERE, "TODUS", "local_files")

# ─── Cargar .env ANTES de leer os.environ ───
# Prioridad final: variables ya exportadas en el sistema > .env > config.ini
# load_dotenv() NO sobreescribe variables existentes (override=False por defecto).
load_dotenv(os.path.join(_HERE, ".env"))


def _load_config():
    config = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE, encoding="utf-8")
    return config


cfg = _load_config()

# ─── PRIORIDAD: Variables de Entorno (.env / panel) > config.ini (Local) ───
API_ID    = int(os.environ.get("API_ID",    cfg.get("Telegram", "api_id",    fallback="0")))
API_HASH  =     os.environ.get("API_HASH",  cfg.get("Telegram", "api_hash",  fallback=""))
BOT_TOKEN =     os.environ.get("BOT_TOKEN", cfg.get("Telegram", "bot_token", fallback=""))

BOT_NAME = cfg.get("Bot", "bot_name", fallback="ToDus S3 Cloud")

# ─── CUOTA POR DEFECTO EN CERO (Bloquea a usuarios sin plan) ───
DAILY_QUOTA_BYTES = cfg.getint("Bot", "daily_quota_mb", fallback=0) * 1024 * 1024
MAX_FILE_BYTES    = cfg.getint("Bot", "max_file_size_mb", fallback=2048) * 1024 * 1024
TEMP_DIR          = cfg.get("Bot", "temp_dir", fallback="TODUS/temp_downloads")

# ─── AUTO-RECUPERACIÓN ───
AUTO_RECOVERY_ENABLED  = cfg.getboolean("Bot", "auto_recovery_enabled", fallback=True)
AUTO_RECOVERY_INTERVAL = cfg.getint("Bot", "auto_recovery_interval_minutes", fallback=60) * 60

NAMESPACE_PREFIX  = cfg.get("ToDus", "namespace_prefix",  fallback="nc")
DEFAULT_HOST      = cfg.get("ToDus", "default_host",      fallback="stickers").lower()
STICKERS_BASE_URL = cfg.get("ToDus", "stickers_base_url", fallback="https://stickers.todus.cu").rstrip("/")
STREAM_BASE_URL   = cfg.get("ToDus", "stream_base_url",   fallback="https://s3.todus.cu/stream").rstrip("/")

# ─── ADMIN IDs ───
_env_admins = os.environ.get("ADMIN_IDS", "")
if _env_admins:
    ADMIN_IDS = {int(p.strip()) for p in _env_admins.split(",") if p.strip().isdigit()}
else:
    raw_admins = cfg.get("Admin", "admin_ids", fallback="0")
    ADMIN_IDS = {int(p.strip()) for p in raw_admins.split(",") if p.strip().isdigit()}

ADMIN_ID = min(ADMIN_IDS) if ADMIN_IDS else 0


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─── RUTA BASE ───
_root_raw = (cfg.get("ToDus", "root_prefix", fallback="public/todus").strip().strip("/")) or "public/todus"
ROOT_PREFIX = _root_raw.replace("{admin_id}", str(ADMIN_ID))


# ─── MANTENIMIENTO ───
MAINTENANCE_MODE = False

def set_maintenance(value: bool):
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = bool(value)

def is_maintenance() -> bool:
    return MAINTENANCE_MODE


os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOCAL_DIR, exist_ok=True)

todus = ToDusHelper(
    namespace_prefix=NAMESPACE_PREFIX,
    max_file_size_mb=MAX_FILE_BYTES // (1024 * 1024),
    default_host=DEFAULT_HOST,
    stickers_base_url=STICKERS_BASE_URL,
    stream_base_url=STREAM_BASE_URL,
    root_prefix=ROOT_PREFIX,
)


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DEFAULT_HEADERS    = {"User-Agent": BROWSER_UA, "Accept": "*/*",
                      "Accept-Encoding": "identity",
                      "Referer": "https://www.google.com/"}
MEDIAFIRE_HEADERS  = {**DEFAULT_HEADERS, "Referer": "https://www.mediafire.com/"}
FACEBOOK_HEADERS   = {**DEFAULT_HEADERS, "Referer": "https://www.facebook.com/"}
INSTAGRAM_HEADERS  = {**DEFAULT_HEADERS, "Referer": "https://www.instagram.com/"}
YOUTUBE_HEADERS    = {**DEFAULT_HEADERS, "Referer": "https://www.youtube.com/"}
MEGA_HEADERS       = {**DEFAULT_HEADERS, "Referer": "https://mega.nz/"}

USE_DESCRIPTION = True