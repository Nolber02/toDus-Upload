# toDus-Upload
### Telegram Bot File to link


Bot de Telegram para **subir, gestionar y compartir archivos** en la nube de **ToDus S3**, con soporte para descargas desde múltiples plataformas (YouTube, Facebook, Instagram, MediaFire, MEGA y URLs directas), sistema de cuotas por usuario, planes, panel de administración y recuperación automática de archivos.

---

## ✨ Características

### 👤 Para usuarios
- 📤 **Subida de archivos** desde Telegram (documentos, videos, audio, fotos) hasta **2 GB** por archivo.
- 🔗 **Descargas desde URLs**:
  - YouTube
  - Facebook
  - Instagram
  - MediaFire
  - MEGA
  - URLs directas `http(s)`
- 📊 **Sistema de cuotas** diarias (plan gratis por defecto).
- 📁 **Historial** de archivos subidos con enlaces de descarga.
- 🔍 **Búsqueda** de archivos por nombre.
- 🗑️ **Eliminación** de archivos propios (nube + local + historial).
- 🔄 **Resubida** desde el almacenamiento local si el archivo desaparece de la nube.
- 📄 **Generación de enlaces `.txt`** con la URL de descarga.
- ⚙️ **Configuración de tamaño de chunk** (`/chunk`) por usuario.
- 🕒 **Cola de tareas** por usuario (descargas y subidas) con posición visible.
- ❌ **Cancelación** de tareas en curso.

### 👑 Para administradores

- 🌐 Cambio dinámico de **endpoint** (`/host stream|stickers`).
- 💳 Asignación de **planes** de cuota (`/plan`).
- 📢 **Mensajes masivos** (`/msg`) con confirmación y estadísticas.
- 👥 Listado de **usuarios registrados** (`/users`).
- 📡 **Estado del bot y de la nube** (`/status`).
- 📂 **Escaneo de archivos** en S3 (`/list_files`).
- 🧾 Generación de **`.txt` con todos los enlaces** (`/list_txt`).
- 🗑️ **Eliminación masiva** de archivos del bot (`/delete_all`).
- ♻️ **Recuperación** de archivos eliminados de la nube (`/recover`).
- ♻️ **Recuperación** de archivos eliminados de la nube automaticamente cada 10s (`/sync on`).
- 🚧 **Modo mantenimiento** (`/mantenimiento on|off`).
- ⚙️ Alternar nombre desde **descripción/caption** (`/usedescription on|off`).

---

## 🧩 Requisitos

- **Python 3.9+**
- **pip** para instalar dependencias
- Cuenta de **Telegram** con:
  - `API_ID` y `API_HASH` (desde [my.telegram.org](https://my.telegram.org))
  - **Token de bot** (desde [@BotFather](https://t.me/BotFather))
- Acceso a la nube de **ToDus S3** (stickers y/o stream)
- **ffmpeg** (opcional, si se procesan medios)

---

## 📥 Instalación

### 1. Clonar el repositorio
```bash
git clone https://github.com/tu-usuario/todus-s3-bot.git
cd todus-s3-bot
```

### 2. Crear entorno virtual (recomendado)
```bash
python -m venv venv
source venv/bin/activate      # Linux / macOS
# venv\Scripts\activate       # Windows
```

### 3. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

Crea un archivo `.env` en la raíz del proyecto:

```env
API_ID=123456
API_HASH=tu_api_hash
BOT_TOKEN=tu_bot_token
ADMIN_IDS=1062249884,1321231231
```

> Las variables de entorno tienen **prioridad** sobre `config.ini`.

### 5. Configurar `config.ini` (opcional)

```ini
[Bot]
bot_name = ToDus S3 Cloud
daily_quota_mb = 1024
max_file_size_mb = 2048
temp_dir = TODUS/temp_downloads
auto_recovery_enabled = true
auto_recovery_interval_minutes = 60

[ToDus]
namespace_prefix = nc
root_prefix = nc/1062249884
stickers_base_url = https://stickers.todus.cu
stream_base_url   = https://s3.todus.cu/stream
default_host = stickers
```

### 6. Ejecutar el bot

```bash
python bot.py
```

O usando el launcher:

```bash
python app.py         # genera deploy_bundle.py
python deploy_bundle.py
```

---

## 🗂️ Estructura del proyecto

```
├── app.py                # Generador del bundle de despliegue
├── bot.py                # Punto de entrada
├── runtime.py            # Arranque/detención del bot
├── config.py             # Carga de configuración (.env > config.ini)
├── config.ini            # Configuración local
├── db.py                 # SQLite: users, history, plans, stats
├── commands.py           # Comandos del bot (/start, /plan, /users, ...)
├── callbacks.py          # Callbacks de botones inline
├── downloads.py          # Recepción de archivos y URLs
├── upload.py             # Subida a ToDus S3
├── transfer.py           # Cola de descargas concurrentes
├── broadcast.py          # Envío masivo de mensajes
├── recovery.py           # Recuperación automática de archivos
├── keyboards.py          # Teclados inline
├── state.py              # Estado global (colas, registros, cancelación)
├── todus_helper.py       # Cliente ToDus (S3 + share)
├── utils.py              # Utilidades (formato, progreso, etc.)
└── requirements.txt
```

---

## 🤖 Comandos disponibles

### Para todos los usuarios
| Comando | Descripción |
|---|---|
| `/start` | Menú principal |
| `/help` | Ayuda |
| `/myID [@usuario\|ID]` | Ver ID propio o ajeno |
| `/historial` | Ver archivos subidos |
| `/delete` | Eliminar archivos propios |
| `/chunk [MB]` | Configurar tamaño de chunk |
| `/cancelar` | Cancelar tareas |
| `/status` | Estado del bot y la nube |

### Solo administradores
| Comando | Descripción |
|---|---|
| `/host stream\|stickers` | Cambiar endpoint activo |
| `/plan <id> <GB>[/d\|/t] <tiempo>` | Asignar plan |
| `/plan <id>` | Ver plan actual |
| `/plan <id> remove` | Quitar plan |
| `/msg <texto>` | Envío masivo |
| `/users` | Listar usuarios |
| `/list_files` | Escanear nube |
| `/list_txt` | Generar `.txt` con enlaces |
| `/delete_all` | Eliminar TODOS los archivos |
| `/recover` | Recuperar archivos eliminados |
| `/usedescription on\|off` | Nombre desde descripción |
| `/mantenimiento on\|off` | Modo mantenimiento |

---

## 💳 Sistema de planes

Ejemplos de uso:

```bash
# 10 GB diarios durante 30 días
/plan 123456789 10/d 30d

# 50 GB totales repartidos en 2 meses
/plan 123456789 50/t 2m

# 10 GB diarios durante 7 días (equivalente a /d)
/plan 123456789 10 7d

# Consultar plan
/plan 123456789

# Quitar plan
/plan 123456789 remove
```

- **`/d`** → cuota **diaria** (se renueva cada 00:00).
- **`/t`** → cuota **total** del plan, repartida entre días.
- **`<tiempo>`**: `7d` (días) o `2m` (meses de 30 días).
- Al expirar, el usuario vuelve al plan gratis (`daily_quota_mb`).

---

## 🤝 Contribuciones

Las contribuciones son bienvenidas:

1. Haz **fork** del proyecto.
2. Crea una rama: `git checkout -b feature/nueva-funcion`.
3. Haz commit de tus cambios: `git commit -m "Añade nueva función"`.
4. Sube la rama: `git push origin feature/nueva-funcion`.
5. Abre un **Pull Request**.

---

## 📬 Contacto

Para dudas, sugerencias o reportar errores, abre un **Issue** en el repositorio.