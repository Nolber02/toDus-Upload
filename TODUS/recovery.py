"""recovery.py — Auto-recuperación de archivos eliminados de la nube.

Recorre el historial, comprueba si cada archivo sigue en ToDus S3 y, si
desapareció pero existe copia local, lo resube automáticamente reutilizando
`process_and_upload` (el mismo flujo del botón "🔄 Resubir").
"""
import asyncio
import os
import shutil
import sqlite3

from config import (
    ADMIN_ID, AUTO_RECOVERY_ENABLED, AUTO_RECOVERY_INTERVAL,
    DB_FILE, TEMP_DIR, todus,
)
from utils import _run_blocking


class _FakeMessage:
    """Mensaje sin Telegram real, para usar process_and_upload en background.

    Cualquier método (reply/edit/delete...) devuelve `self` para que el flujo
    de `process_and_upload` no falle por querer editar un mensaje real.
    """
    def __init__(self, user_id: int):
        self.from_user = type("U", (), {
            "id": user_id, "username": None, "first_name": None})()
        self.chat = type("C", (), {"id": user_id})()
        self.id = 0
        self.text = ""
        self.caption = None
        self.document = None
        self.video = None
        self.audio = None
        self.photo = None

    async def reply(self, *a, **k): return self
    async def reply_text(self, *a, **k): return self
    async def reply_document(self, *a, **k): return self
    async def reply_photo(self, *a, **k): return self
    async def edit_text(self, *a, **k): return self
    async def edit(self, *a, **k): return self
    async def edit_reply_markup(self, *a, **k): return self
    async def edit_caption(self, *a, **k): return self
    async def delete(self, *a, **k): return self

    def __getattr__(self, name):
        async def _noop(*a, **k): return self
        return _noop


def _get_history_entries_with_local() -> list:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, file_name, url, local_path FROM history "
        "WHERE local_path IS NOT NULL AND local_path != ''")
    rows = cur.fetchall()
    conn.close()
    return rows


def _delete_history_by_id(hid: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM history WHERE id = ?", (hid,))
    conn.commit()
    conn.close()


async def _key_exists(user_id: int, filename: str, host: str = None) -> bool:
    """True si la key S3 existe (o si no se pudo comprobar → conservador)."""
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
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ("404", "not found", "nosuchkey", "no such key")):
                return False
            return True  # error de red → no resubir para no duplicar

    try:
        raw = await _run_blocking(client.list, prefix=key) or []
        return any(f.get("Key") == key for f in raw)
    except Exception:
        return True


async def _recover_entry(client, hid: int, user_id: int,
                          file_name: str, url: str, local_path: str) -> bool:
    if not local_path or not os.path.exists(local_path):
        return False

    # La key S3 se deriva del último segmento de la URL
    s3_name = url.rstrip("/").split("/")[-1]
    host = "stream" if "/stream/" in url else "stickers"

    if await _key_exists(user_id, s3_name, host=host):
        return False

    temp_dir = os.path.join(TEMP_DIR, f"u{user_id}")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, s3_name)
    try:
        shutil.copy2(local_path, temp_path)
    except Exception as e:
        print(f"[RECOVERY] No se pudo copiar {local_path}: {e}")
        return False

    try:
        from upload import process_and_upload
        msg = _FakeMessage(user_id)
        await process_and_upload(client, msg, temp_path, s3_name, "auto-recovery")
        try:
            _delete_history_by_id(hid)   # borrar la entrada vieja
        except Exception:
            pass
        print(f"[RECOVERY] ✅ Recuperado: {file_name} (uid={user_id})")
        return True
    except Exception as e:
        print(f"[RECOVERY] ❌ Falló {file_name}: {e}")
        return False
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


async def run_recovery_loop(client):
    """Bucle que revisa el historial periódicamente y resube lo que falte."""
    if not AUTO_RECOVERY_ENABLED:
        print("[RECOVERY] Auto-recuperación desactivada.")
        return

    await asyncio.sleep(20)  # esperar arranque del bot
    print(f"[RECOVERY] Loop iniciado (cada {AUTO_RECOVERY_INTERVAL}s)")

    while True:
        try:
            entries = _get_history_entries_with_local()
            recovered = 0
            for hid, user_id, file_name, url, local_path in entries:
                try:
                    if await _recover_entry(client, hid, user_id,
                                            file_name, url, local_path):
                        recovered += 1
                        await asyncio.sleep(3)  # no saturar ToDus
                except Exception as e:
                    print(f"[RECOVERY] Error hid={hid}: {e}")

            if recovered:
                print(f"[RECOVERY] Ciclo: {recovered} archivo(s) recuperado(s)")
                try:
                    await client.send_message(
                        ADMIN_ID,
                        f"♻️ **Auto-recuperación**\n"
                        f"Se resubieron {recovered} archivo(s) que faltaban en la nube.")
                except Exception:
                    pass
        except Exception as e:
            print(f"[RECOVERY] Error en ciclo: {e}")

        await asyncio.sleep(AUTO_RECOVERY_INTERVAL)