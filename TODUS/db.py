"""
db.py — SQLite: users, history, plans, stats.
"""
import os
import re
import shutil
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional

from config import DB_FILE, DAILY_QUOTA_BYTES, LOCAL_DIR, TEMP_DIR, is_admin


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, quota_used INTEGER DEFAULT 0, last_upload_date TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, file_name TEXT, file_size INTEGER, url TEXT, upload_date TEXT)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            user_id INTEGER PRIMARY KEY,
            quota_bytes INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            plan_label TEXT,
            quota_period TEXT DEFAULT 'total'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_stats (
            user_id INTEGER PRIMARY KEY,
            total_files INTEGER DEFAULT 0,
            total_bytes INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS global_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()

    # ─── Migraciones users ───
    cursor.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cursor.fetchall()}
    if "username" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if "first_name" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
    conn.commit()

    # ─── Migraciones plans ───
    cursor.execute("PRAGMA table_info(plans)")
    pcols = {row[1] for row in cursor.fetchall()}
    if "quota_period" not in pcols:
        cursor.execute("ALTER TABLE plans ADD COLUMN quota_period TEXT DEFAULT 'total'")
    conn.commit()

    # ─── Migración history: local_path ───
    cursor.execute("PRAGMA table_info(history)")
    hcols = {row[1] for row in cursor.fetchall()}
    if "local_path" not in hcols:
        cursor.execute("ALTER TABLE history ADD COLUMN local_path TEXT")
    conn.commit()

    _check_weekly_reset(conn)
    conn.close()


def _check_weekly_reset(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM global_config WHERE key = 'last_weekly_reset'")
    row = cursor.fetchone()

    now = datetime.now()

    if not row:
        cursor.execute("INSERT INTO global_config (key, value) VALUES (?, ?)",
                       ("last_weekly_reset", now.isoformat(timespec="seconds")))
        conn.commit()
        return

    last_reset = datetime.fromisoformat(row[0])
    if now - last_reset >= timedelta(days=7):
        cursor.execute("DELETE FROM weekly_stats")
        cursor.execute("UPDATE global_config SET value = ? WHERE key = 'last_weekly_reset'",
                       (now.isoformat(timespec="seconds"),))
        conn.commit()
        print("[DB] Top semanal reiniciado.")


init_db()


# ─── PLANES ───
def parse_duration(text: str) -> timedelta:
    text = (text or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)([dm])", text)
    if not m:
        raise ValueError("Formato inválido. Usa `<N>d` (días) o `<N>m` (meses). Ej: `7d`, `365d`, `2m`, `24m`.")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError("La cantidad debe ser mayor que 0.")
    return timedelta(days=n) if unit == "d" else timedelta(days=n * 30)


def parse_quota_spec(spec: str) -> tuple:
    spec = (spec or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:/([dt]))?", spec)
    if not m:
        raise ValueError(
            "Formato de cuota inválido. Usa `<GB>`, `<GB>/d` o `<GB>/t`.\n"
            "Ej: `10`, `10/d`, `50/t`.")
    gb = float(m.group(1).replace(",", "."))
    if gb <= 0:
        raise ValueError("La cuota debe ser mayor que 0.")
    period = m.group(2) or "d"
    return gb, ("d" if period == "d" else "total")


def set_user_plan(user_id: int, quota_gb: float, duration: timedelta,
                  label: str = None, period: str = "d") -> dict:
    quota_bytes = int(round(quota_gb * 1024 * 1024 * 1024))
    now = datetime.now()
    expires = now + duration
    days = duration.days

    if label:
        plan_label = label
    else:
        if period == "d":
            unidad = "día" if days == 1 else "días"
            plan_label = f"{quota_gb:g} GB/día por {days} {unidad}"
        else:
            if days % 30 == 0 and days >= 30:
                months = days // 30
                unidad = "mes" if months == 1 else "meses"
                plan_label = f"{quota_gb:g} GB total / {months} {unidad} ({days} días)"
            else:
                unidad = "día" if days == 1 else "días"
                plan_label = f"{quota_gb:g} GB total / {days} {unidad}"

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO plans (user_id, quota_bytes, started_at, expires_at, plan_label, quota_period)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            quota_bytes = excluded.quota_bytes,
            started_at = excluded.started_at,
            expires_at = excluded.expires_at,
            plan_label = excluded.plan_label,
            quota_period = excluded.quota_period
    """, (user_id, quota_bytes, now.isoformat(timespec="seconds"),
          expires.isoformat(timespec="seconds"), plan_label, period))
    conn.commit()
    conn.close()
    return {"user_id": user_id, "quota_bytes": quota_bytes,
            "started_at": now, "expires_at": expires,
            "label": plan_label, "period": period}


def get_user_plan(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT quota_bytes, started_at, expires_at, plan_label, quota_period "
        "FROM plans WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    quota_bytes, started_at, expires_at, label, period = row
    try:
        expires_dt = datetime.fromisoformat(expires_at)
    except Exception:
        conn.close()
        return None
    if datetime.now() >= expires_dt:
        cursor.execute("DELETE FROM plans WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return None
    conn.close()
    return {"user_id": user_id, "quota_bytes": quota_bytes,
            "started_at": datetime.fromisoformat(started_at),
            "expires_at": expires_dt, "label": label,
            "period": period or "d"}


def remove_user_plan(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM plans WHERE user_id = ?", (user_id,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


def get_effective_quota(user_id: int) -> int:
    if is_admin(user_id):
        return 0
    plan = get_user_plan(user_id)
    if not plan:
        return DAILY_QUOTA_BYTES
    if plan["period"] == "d":
        return plan["quota_bytes"]
    total_seconds = (plan["expires_at"] - plan["started_at"]).total_seconds()
    days = max(1, int(total_seconds // 86400))
    return max(1, plan["quota_bytes"] // days)


def get_plan_total_bytes(user_id: int) -> int:
    plan = get_user_plan(user_id)
    if not plan:
        return 0
    if plan["period"] == "d":
        total_seconds = (plan["expires_at"] - plan["started_at"]).total_seconds()
        days = max(1, int(total_seconds // 86400))
        return plan["quota_bytes"] * days
    return plan["quota_bytes"]


# ─── USERS ───
def touch_user(user_id: int, username: str = None, first_name: str = None):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cursor.fetchone():
        if username is not None or first_name is not None:
            cursor.execute(
                "UPDATE users SET username = COALESCE(?, username), first_name = COALESCE(?, first_name) WHERE user_id = ?",
                (username, first_name, user_id))
    else:
        cursor.execute(
            "INSERT INTO users (user_id, quota_used, last_upload_date, username, first_name) VALUES (?, 0, ?, ?, ?)",
            (user_id, today, username, first_name))
    conn.commit()
    conn.close()


def get_user_quota(user_id: int) -> tuple:
    if is_admin(user_id):
        return 0, True
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT quota_used, last_upload_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, quota_used, last_upload_date) VALUES (?, 0, ?)", (user_id, today))
        conn.commit()
        used = 0
    else:
        used, last_date = row
        if last_date != today:
            cursor.execute("UPDATE users SET quota_used = 0, last_upload_date = ? WHERE user_id = ?", (today, user_id))
            conn.commit()
            used = 0
    conn.close()
    return used, False


def add_user_usage(user_id: int, size_bytes: int):
    if is_admin(user_id):
        return
    if size_bytes <= 0:
        return
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, quota_used, last_upload_date) VALUES (?, ?, ?)",
            (user_id, size_bytes, today))
    else:
        cursor.execute(
            "UPDATE users SET quota_used = quota_used + ?, last_upload_date = ? WHERE user_id = ?",
            (size_bytes, today, user_id))

    cursor.execute("SELECT user_id FROM weekly_stats WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO weekly_stats (user_id, total_files, total_bytes) VALUES (?, 1, ?)",
            (user_id, size_bytes))
    else:
        cursor.execute(
            "UPDATE weekly_stats SET total_files = total_files + 1, total_bytes = total_bytes + ? WHERE user_id = ?",
            (size_bytes, user_id))

    conn.commit()
    conn.close()


# ─── HISTORY ───
def add_history(user_id: int, file_name: str, file_size: int, url: str,
                s3_filename: str = None) -> int:
    """
    file_name:   nombre para mostrar en DB (puede tener tildes/símbolos)
    s3_filename: nombre sanitizado en S3 (key.split('/')[-1]).
                 Si es None se usa file_name.
                 La copia local se guarda con ESTE nombre para que el botón
                 "🔄 Resubir" la encuentre al reconstruir el key S3.
    """
    s3_name = s3_filename or file_name
    local_dir = os.path.join(LOCAL_DIR, f"u{user_id}")
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, s3_name)

    # Buscar archivo temporal en TEMP_DIR/u{uid}/ — puede llamarse file_name o s3_name
    temp_candidates = [
        os.path.join(TEMP_DIR, f"u{user_id}", file_name),
        os.path.join(TEMP_DIR, f"u{user_id}", s3_name),
    ]
    for tp in temp_candidates:
        if os.path.exists(tp):
            try:
                shutil.copy2(tp, local_path)
                break
            except Exception:
                pass

    stored_path = local_path if os.path.exists(local_path) else None

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO history (user_id, file_name, file_size, url, upload_date, local_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, file_name, file_size, url,
         datetime.now().strftime("%Y-%m-%d %H:%M"), stored_path))
    conn.commit()
    last_id = cursor.lastrowid
    conn.close()
    return last_id


def get_history(user_id: int, search_query: str = None) -> list:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if search_query:
        cursor.execute(
            "SELECT file_name, file_size, url, upload_date FROM history "
            "WHERE user_id = ? AND file_name LIKE ? ORDER BY id DESC LIMIT 10",
            (user_id, f"%{search_query}%"))
    else:
        cursor.execute(
            "SELECT file_name, file_size, url, upload_date FROM history "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_history_entries(user_id: int, limit: int = 20) -> list:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, file_name, file_size, url, upload_date FROM history "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_history_entry(history_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, file_name, file_size, url, upload_date, local_path "
        "FROM history WHERE id = ?", (history_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "file_name": row[2],
            "file_size": row[3], "url": row[4], "upload_date": row[5],
            "local_path": row[6]}


def delete_history_entry(history_id: int) -> Optional[dict]:
    entry = get_history_entry(history_id)
    if not entry:
        return None
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM history WHERE id = ?", (history_id,))
    conn.commit()
    conn.close()
    return entry


def get_bot_stats() -> dict:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*), SUM(file_size) FROM history")
    row = cursor.fetchone()
    conn.close()
    return {"users": users, "files": row[0] or 0, "size": row[1] or 0}


def get_top_users(limit: int = 10) -> list:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT w.user_id, u.username, u.first_name,
               w.total_files, w.total_bytes
        FROM weekly_stats w
        LEFT JOIN users u ON u.user_id = w.user_id
        ORDER BY w.total_bytes DESC, w.total_files DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_user_ids(include_admins: bool = False) -> list:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    if not include_admins:
        ids = [uid for uid in ids if not is_admin(uid)]
    return ids


def get_all_users_info() -> list:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, first_name FROM users ORDER BY user_id ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows


# ─── ALMACENAMIENTO LOCAL ───
def delete_local_file(user_id: int, filename: str) -> bool:
    """Borra el archivo local del usuario. Devuelve True si existía."""
    path = os.path.join(LOCAL_DIR, f"u{user_id}", filename)
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except Exception:
            pass
    return False


def clear_local_files() -> int:
    """Borra TODOS los archivos locales. Devuelve cuántos eliminó."""
    count = 0
    if os.path.isdir(LOCAL_DIR):
        for entry in os.listdir(LOCAL_DIR):
            p = os.path.join(LOCAL_DIR, entry)
            try:
                if os.path.isdir(p):
                    count += len([f for f in os.listdir(p)
                                  if os.path.isfile(os.path.join(p, f))])
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
                    count += 1
            except Exception:
                pass
    os.makedirs(LOCAL_DIR, exist_ok=True)
    return count