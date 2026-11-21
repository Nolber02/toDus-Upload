"""
keyboards.py — Teclados inline.
"""
import uuid

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from state import CALLBACK_REGISTRY


def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Mi cola", callback_data="cola"),
         InlineKeyboardButton("📁 Historial", callback_data="historial")],
        [InlineKeyboardButton("📊 Mi cuota", callback_data="cuota"),
         InlineKeyboardButton("🏆 Top usuarios", callback_data="top")],
        [InlineKeyboardButton("📡 Estado", callback_data="estado"),
         InlineKeyboardButton("📖 Ayuda", callback_data="ayuda")],
    ])


def get_file_keyboard(url: str, key: str):
    short_id = str(uuid.uuid4())[:8]
    CALLBACK_REGISTRY[short_id] = key
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Resubir", callback_data=f"c_{short_id}"),
         InlineKeyboardButton("📄 Enlace.txt", callback_data=f"t_{short_id}")],
        [InlineKeyboardButton("🗑️ Eliminar", callback_data=f"d_{short_id}")],
    ])


def get_cancel_keyboard(cancel_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancelar", callback_data=f"x_{cancel_id}")]
    ])


def get_delete_list_keyboard(entries: list):
    """entries: [{id, file_name, file_size, url, upload_date}, ...]"""
    rows = []
    for e in entries:
        name = e["file_name"]
        if len(name) > 45:
            name = name[:42] + "…"
        rows.append([
            InlineKeyboardButton(
                f"🗑️ {name} ({e['file_size'] // (1024*1024)} MB)",
                callback_data=f"delq_{e['id']}")
        ])
    rows.append([
        InlineKeyboardButton("❌ Cerrar", callback_data="del_close")
    ])
    return InlineKeyboardMarkup(rows)


def get_delete_confirm_keyboard(history_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sí, eliminar", callback_data=f"delc_{history_id}"),
         InlineKeyboardButton("❌ Cancelar", callback_data="del_close")],
    ])