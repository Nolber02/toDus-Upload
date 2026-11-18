"""
state.py — Estado global: colas, semáforos, cancelaciones.
"""
import asyncio
import time
import uuid

from config import is_admin

ACTIVE_TASKS = {}
CALLBACK_REGISTRY = {}
CANCEL_EVENTS = {}
CONFIRM_REGISTRY: dict = {}

UPLOAD_PENDING: dict = {}
UPLOAD_JOB_COUNTER = 0
UPLOAD_SEMAPHORE: asyncio.Semaphore = None
ADMIN_UPLOAD_SEMAPHORE: asyncio.Semaphore = None

DOWNLOAD_PENDING: dict = {}
DOWNLOAD_JOB_COUNTER = 0
DOWNLOAD_SEMAPHORE: asyncio.Semaphore = None
ADMIN_DOWNLOAD_SEMAPHORE: asyncio.Semaphore = None

MAX_USER_DOWNLOAD_JOBS = 3
MAX_USER_UPLOAD_JOBS = 3

# ─── NUEVO: Chunk personalizado por usuario ───
# {user_id: bytes}
USER_CHUNK_SIZES: dict = {}


def _get_download_semaphore() -> asyncio.Semaphore:
    global DOWNLOAD_SEMAPHORE
    if DOWNLOAD_SEMAPHORE is None:
        DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)
    return DOWNLOAD_SEMAPHORE


def _get_admin_download_semaphore() -> asyncio.Semaphore:
    global ADMIN_DOWNLOAD_SEMAPHORE
    if ADMIN_DOWNLOAD_SEMAPHORE is None:
        ADMIN_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)
    return ADMIN_DOWNLOAD_SEMAPHORE


def _get_upload_semaphore() -> asyncio.Semaphore:
    global UPLOAD_SEMAPHORE
    if UPLOAD_SEMAPHORE is None:
        UPLOAD_SEMAPHORE = asyncio.Semaphore(1)
    return UPLOAD_SEMAPHORE


def _get_admin_upload_semaphore() -> asyncio.Semaphore:
    global ADMIN_UPLOAD_SEMAPHORE
    if ADMIN_UPLOAD_SEMAPHORE is None:
        ADMIN_UPLOAD_SEMAPHORE = asyncio.Semaphore(2)
    return ADMIN_UPLOAD_SEMAPHORE


def _get_download_sem_for(user_id: int) -> asyncio.Semaphore:
    return _get_admin_download_semaphore() if is_admin(user_id) else _get_download_semaphore()


def _get_upload_sem_for(user_id: int) -> asyncio.Semaphore:
    return _get_admin_upload_semaphore() if is_admin(user_id) else _get_upload_semaphore()


def _user_active_uploads(user_id: int) -> int:
    return sum(1 for info in UPLOAD_PENDING.values() if info.get("user_id") == user_id)


def _user_active_downloads(user_id: int) -> int:
    return sum(1 for info in DOWNLOAD_PENDING.values() if info.get("user_id") == user_id)


def _can_enqueue_upload(user_id: int) -> bool:
    return is_admin(user_id) or _user_active_uploads(user_id) < MAX_USER_UPLOAD_JOBS


def _can_enqueue_download(user_id: int) -> bool:
    return is_admin(user_id) or _user_active_downloads(user_id) < MAX_USER_DOWNLOAD_JOBS


def _queue_position(job_id: str) -> int:
    info = UPLOAD_PENDING.get(job_id)
    if not info:
        return 0
    admin_cola = is_admin(info["user_id"])
    same = sorted(
        ((i.get("created_at", 0), jid) for jid, i in UPLOAD_PENDING.items()
         if i.get("user_id") and is_admin(i["user_id"]) == admin_cola),
        key=lambda x: x[0])
    ordered = [jid for _, jid in same]
    return ordered.index(job_id) + 1 if job_id in ordered else 0


def _queue_size() -> int:
    return len(UPLOAD_PENDING)


def _download_register(user_id: int, filename: str) -> str:
    global DOWNLOAD_JOB_COUNTER
    DOWNLOAD_JOB_COUNTER += 1
    job_id = f"dl{DOWNLOAD_JOB_COUNTER}_{uuid.uuid4().hex[:6]}"
    DOWNLOAD_PENDING[job_id] = {
        "user_id": user_id, "filename": filename,
        "status": "downloading", "started_at": time.time(),
    }
    return job_id


def _download_unregister(job_id: str):
    DOWNLOAD_PENDING.pop(job_id, None)


def _download_position(job_id: str) -> int:
    info = DOWNLOAD_PENDING.get(job_id)
    if not info:
        return 0
    admin_cola = is_admin(info["user_id"])
    same = sorted(
        ((i.get("started_at", 0), jid) for jid, i in DOWNLOAD_PENDING.items()
         if is_admin(i["user_id"]) == admin_cola),
        key=lambda x: x[0])
    ordered = [jid for _, jid in same]
    return ordered.index(job_id) + 1 if job_id in ordered else 0


def _registrar_cancelacion(cancel_id: str) -> asyncio.Event:
    ev = asyncio.Event()
    CANCEL_EVENTS[cancel_id] = ev
    return ev


def _limpiar_cancelacion(cancel_id: str):
    CANCEL_EVENTS.pop(cancel_id, None)