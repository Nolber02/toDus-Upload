"""
broadcast.py — Envío masivo de mensajes.
"""
import asyncio

from db import get_all_user_ids
from utils import make_progress_bar


async def broadcast_message(client, text: str, status_msg, include_admins: bool = False) -> dict:
    user_ids = get_all_user_ids(include_admins=include_admins)
    total = len(user_ids)
    sent = failed = blocked = 0

    if total == 0:
        return {"total": 0, "sent": 0, "failed": 0, "blocked": 0}

    for i, uid in enumerate(user_ids, start=1):
        try:
            await client.send_message(uid, text)
            sent += 1
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err or "user_is_blocked" in err:
                blocked += 1
            else:
                failed += 1

        if i % 10 == 0 or i == total:
            try:
                await status_msg.edit_text(
                    f"📤 **Enviando mensaje masivo...**\n\n"
                    f"`{make_progress_bar(i * 100 / total)}` {i * 100 / total:.0f}%\n"
                    f"✅ Enviados: {sent}\n"
                    f"❌ Fallidos: {failed}\n"
                    f"🚫 Bloqueados: {blocked}\n"
                    f"📊 Total: {i} / {total}")
            except Exception:
                pass

        await asyncio.sleep(0.05)

    return {"total": total, "sent": sent, "failed": failed, "blocked": blocked}