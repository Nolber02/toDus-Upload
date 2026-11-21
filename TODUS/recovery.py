"""Recuperación de archivos ausentes en ToDus S3 desde copias locales."""
import asyncio
import os
import shutil
import sqlite3

from config import (
    ADMIN_ID,
    AUTO_RECOVERY_ENABLED,
    AUTO_RECOVERY_INTERVAL,
    DB_FILE,
    TEMP_DIR,
    todus,
)
from utils import _run_blocking
from db import get_recovery_notification, save_recovery_notification


# Intervalo de la comprobación rápida activada por /sync.
SYNC_INTERVAL_SECONDS = 10

# Evita que el bucle global y /sync intenten recuperar el mismo registro
# al mismo tiempo.
_RECOVERY_LOCKS = {}


class _FakeMessage:
    """Mensaje simulado para reutilizar process_and_upload en segundo plano."""

    def __init__(self, user_id: int):
        self.from_user = type("U", (), {
            "id": user_id,
            "username": None,
            "first_name": None,
        })()
        self.chat = type("C", (), {"id": user_id})()
        self.id = 0
        self.text = ""
        self.caption = None
        self.document = None
        self.video = None
        self.audio = None
        self.photo = None

    async def reply(self, *args, **kwargs):
        return self

    async def reply_text(self, *args, **kwargs):
        return self

    async def reply_document(self, *args, **kwargs):
        return self

    async def reply_photo(self, *args, **kwargs):
        return self

    async def edit_text(self, *args, **kwargs):
        return self

    async def edit(self, *args, **kwargs):
        return self

    async def edit_reply_markup(self, *args, **kwargs):
        return self

    async def edit_caption(self, *args, **kwargs):
        return self

    async def delete(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return self
        return _noop


def _get_history_entries_with_local() -> list:
    conn = sqlite3.connect(DB_FILE)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, user_id, file_name, url, local_path "
            "FROM history "
            "WHERE local_path IS NOT NULL AND local_path != ''"
        )
        return cur.fetchall()
    finally:
        conn.close()


def _history_entry_exists(history_id: int) -> bool:
    """Confirma que el usuario no borró la entrada antes de recuperarla."""
    conn = sqlite3.connect(DB_FILE)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM history WHERE id = ?",
            (history_id,),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def _delete_history_by_id(history_id: int):
    conn = sqlite3.connect(DB_FILE)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM history WHERE id = ?", (history_id,))
        conn.commit()
    finally:
        conn.close()


async def _key_exists(user_id: int, filename: str, host: str = None) -> bool:
    """Devuelve True si existe o si no se pudo comprobar (para no duplicar)."""
    key = f"{todus.bot_prefix}/tg_{user_id}/{filename}"

    try:
        client = todus._client_for(host or todus.default_host)
    except Exception:
        return True

    head = getattr(client, "head_object", None)
    if callable(head):
        try:
            await _run_blocking(head, key)
            return True
        except Exception as error:
            err = str(error).lower()
            if any(text in err for text in (
                "404",
                "not found",
                "nosuchkey",
                "no such key",
            )):
                return False
            # Error de red u otro fallo: no resubir para evitar duplicados.
            return True

    try:
        raw = await _run_blocking(client.list, prefix=key) or []
        if isinstance(raw, dict):
            raw = raw.get("Contents") or raw.get("contents") or []
        return any(item.get("Key") == key for item in raw)
    except Exception:
        return True


async def _recover_entry_locked(
    client,
    history_id: int,
    user_id: int,
    file_name: str,
    url: str,
    local_path: str,
) -> bool:
    # Si el usuario eliminó el archivo y su entrada del historial, no recuperarlo.
    if not _history_entry_exists(history_id):
        return False

    if not local_path or not os.path.exists(local_path):
        return False

    s3_name = url.rstrip("/").split("/")[-1]
    if not s3_name:
        return False

    host = "stream" if "/stream/" in url else "stickers"

    if await _key_exists(user_id, s3_name, host=host):
        return False

    # Revalidar antes de copiar/subir: el usuario pudo borrar el archivo
    # mientras se comprobaba su presencia en la nube.
    if not _history_entry_exists(history_id) or not os.path.exists(local_path):
        return False

    temp_dir = os.path.join(TEMP_DIR, f"u{user_id}")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, s3_name)

    try:
        shutil.copy2(local_path, temp_path)
    except Exception as error:
        print(f"[RECOVERY] No se pudo copiar {local_path}: {error}")
        return False

    try:
        from upload import process_and_upload

        msg = _FakeMessage(user_id)
        await process_and_upload(
            client,
            msg,
            temp_path,
            s3_name,
            "auto-recovery",
            history_id=history_id,
            host_override=host,
        )

        # Solo confirmar y notificar si el objeto quedó disponible en la nube.
        if not await _key_exists(user_id, s3_name, host=host):
            return False
        print(f"[RECOVERY] ✅ Recuperado: {file_name} (uid={user_id})")
        return True
    except Exception as error:
        print(f"[RECOVERY] ❌ Falló {file_name}: {error}")
        return False
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


async def _recover_entry(
    client,
    history_id: int,
    user_id: int,
    file_name: str,
    url: str,
    local_path: str,
) -> bool:
    lock = _RECOVERY_LOCKS.setdefault(history_id, asyncio.Lock())

    async with lock:
        try:
            return await _recover_entry_locked(
                client,
                history_id,
                user_id,
                file_name,
                url,
                local_path,
            )
        finally:
            # No borrar el lock si hay otra tarea esperando por él.
            if not lock.locked():
                _RECOVERY_LOCKS.pop(history_id, None)


async def _notify_recovery(client, user_id: int, file_name: str):
    previous = get_recovery_notification(user_id)
    count = (previous[1] if previous else 0) + 1
    text = (f"♻️ **Archivo recuperado**\n📄 `{file_name}`\n"
            f"🔄 **Recuperaciones automáticas:** {count}\n"
            "El archivo faltaba en la nube y se resubió desde la copia local.")

    if previous:
        try:
            message = await client.get_messages(user_id, previous[0])
            if message and not getattr(message, "empty", False):
                await message.edit_text(text)
                save_recovery_notification(user_id, previous[0], count)
                return
        except Exception:
            pass

    message = await client.send_message(user_id, text)
    save_recovery_notification(user_id, message.id, count)


def start_user_sync_task(client, user_id: int):
    from db import is_sync_enabled
    if not is_sync_enabled(user_id):
        return None
    task = SYNC_TASKS.get(user_id)
    if task is None or task.done():
        task = asyncio.create_task(run_user_recovery_loop(client, user_id))
        SYNC_TASKS[user_id] = task
        task.add_done_callback(
            lambda completed, uid=user_id:
                SYNC_TASKS.pop(uid, None)
                if SYNC_TASKS.get(uid) is completed else None
        )
    return task


async def start_default_sync_tasks(client):
    """Inicia /sync para los usuarios habilitados al arrancar el bot."""
    from db import get_sync_enabled_user_ids
    for user_id in get_sync_enabled_user_ids():
        start_user_sync_task(client, user_id)


SYNC_TASKS = {}


async def run_user_recovery_loop(client, user_id: int):
    """Comprueba cada 10 segundos solo los archivos del usuario que activó /sync."""
    from db import is_sync_enabled
    print(
        f"[SYNC] Loop iniciado para uid={user_id} "
        f"(cada {SYNC_INTERVAL_SECONDS}s)"
    )

    while True:
        try:
            if not is_sync_enabled(user_id):
                return
            entries = _get_history_entries_with_local()

            for history_id, entry_user_id, file_name, url, local_path in entries:
                if entry_user_id != user_id:
                    continue

                try:
                    recovered = await _recover_entry(
                        client,
                        history_id,
                        entry_user_id,
                        file_name,
                        url,
                        local_path,
                    )
                    if recovered:
                        try:
                            await _notify_recovery(client, user_id, file_name)
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    print(
                        f"[SYNC] Error history_id={history_id}, "
                        f"uid={user_id}: {error}"
                    )
        except asyncio.CancelledError:
            print(f"[SYNC] Loop detenido para uid={user_id}")
            raise
        except Exception as error:
            print(f"[SYNC] Error en ciclo uid={user_id}: {error}")

        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


async def run_recovery_loop(client):
    """Bucle global de recuperación automática configurado en config.ini."""
    if not AUTO_RECOVERY_ENABLED:
        print("[RECOVERY] Auto-recuperación global desactivada.")
        return

    await asyncio.sleep(20)
    print(f"[RECOVERY] Loop global iniciado (cada {AUTO_RECOVERY_INTERVAL}s)")

    while True:
        try:
            entries = _get_history_entries_with_local()
            recovered = 0

            for history_id, user_id, file_name, url, local_path in entries:
                try:
                    if await _recover_entry(
                        client,
                        history_id,
                        user_id,
                        file_name,
                        url,
                        local_path,
                    ):
                        recovered += 1
                        try:
                            await _notify_recovery(client, user_id, file_name)
                        except Exception as error:
                            print(f"[RECOVERY] No se pudo notificar uid={user_id}: {error}")
                        await asyncio.sleep(3)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    print(f"[RECOVERY] Error history_id={history_id}: {error}")

            if recovered:
                print(
                    f"[RECOVERY] Ciclo global: "
                    f"{recovered} archivo(s) recuperado(s)"
                )
                try:
                    await _notify_recovery(
                        client, ADMIN_ID,
                        f"{recovered} archivo(s) recuperado(s)")
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[RECOVERY] Error en ciclo global: {error}")

        await asyncio.sleep(AUTO_RECOVERY_INTERVAL)