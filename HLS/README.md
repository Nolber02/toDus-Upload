# HLS Telegram Bot

### Procesamiento de videos y streaming HLS desde Telegram

Bot privado de Telegram que recibe videos, genera distintas calidades HLS y MP4 con FFmpeg y, opcionalmente, sube el HLS a ToDus S3. Incluye un servidor web con reproductor, ajustes, seguimiento del almacenamiento y herramientas para verificar y reparar archivos en la nube.

---

## ✨ Características

### 📹 Procesamiento y reproducción

- 📥 Recibe videos enviados al bot y los procesa automáticamente.
- 🎞️ Genera playlists HLS y segmentos en las calidades seleccionadas.
- ⚙️ Perfiles de compresión para equilibrar calidad y tamaño.
- 🎬 Genera MP4 descargables a partir del HLS.
- ▶️ Reproductor web con selección de calidad y lista de videos.
- 📊 Muestra el progreso del procesamiento, el estado y el espacio de almacenamiento.

### ☁️ ToDus

- 📤 Subida opcional de los archivos HLS a ToDus S3.
- 🖥️ Configuración del host de ToDus (`stickers` o `stream`).
- 🔍 Verificación de la presencia de archivos HLS en la nube con `/todusVerify`.
- ♻️ Opción en Settings para revisar cada 10 segundos los archivos y volver a subir los que falten. Requiere conservar una copia local del HLS.
- 🗑️ Endpoints para eliminar videos locales o sus archivos de ToDus.

### 📤 Telegram y administración

- 📺 Opción para subir los MP4 generados a un canal o grupo.
- 🔐 El canal para los MP4 se puede configurar con la variable `HLS_CANAL_MP4`.
- 📨 El bot está pensado para procesar videos enviados en privado por el propietario configurado.
- 📈 Comandos de estado, cola, host y verificación de ToDus.

---

## 🧩 Requisitos

- **Python 3.10+** y `pip`.
- **FFmpeg** y `ffprobe`, instalados y disponibles en `PATH`.
- **Node.js y npm**, si se ejecuta el lanzador completo, que inicia un túnel con `npx flaregui`.
- Credenciales de Telegram:
  - `API_ID` y `API_HASH`, desde [my.telegram.org](https://my.telegram.org).
  - Token de bot, creado con [@BotFather](https://t.me/BotFather).
  - ID numérico del propietario del bot.
- Acceso a un canal o grupo de Telegram si se desea publicar allí los MP4. El bot debe poder publicar; para canales privados también puede necesitar un enlace de invitación.
- Acceso a ToDus si se desea usar almacenamiento en la nube.

---

## 📥 Instalación y configuración

### 1. Instalar dependencias

Desde la carpeta del proyecto:

```bash
python -m venv .venv
```

Activa el entorno virtual:

```bash
# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Instala los paquetes de Python:

```bash
pip install -r requirements.txt
```

Instala FFmpeg y confirma que los dos comandos estén disponibles:

```bash
ffmpeg -version
ffprobe -version
```

### 2. Configurar las variables de entorno

Crea un archivo `.env` en la raíz del proyecto. El archivo se carga automáticamente al ejecutar `main.py`, el servidor o el bot directamente.

```env
HLS_API_ID=123456
HLS_API_HASH=tu_api_hash
HLS_BOT_TOKEN=123456789:token_de_tu_bot
HLS_OWNER_ID=123456789

# Opcional: @usuario, ID numérico (-100...) o enlace de invitación
# del canal/grupo donde se publican los MP4.
HLS_CANAL_MP4=-123456789000
```

- `HLS_API_ID`, `HLS_API_HASH`, `HLS_BOT_TOKEN` y `HLS_OWNER_ID` son las credenciales para iniciar y controlar el bot.
- `HLS_CANAL_MP4` es opcional. Si está definida, tiene prioridad sobre `CANAL_MP4` en `config.ini` y sobre el canal configurado desde Settings. Si se cambia la variable, reinicia el bot y el servidor para que ambos procesos lean el valor actualizado.
- Mantén `.env` privado: no publiques tokens ni credenciales en el repositorio.

### 3. Configurar `config.ini`

El proyecto busca su configuración en este orden:

1. `config/config_stream.ini`
2. `config_stream.ini`
3. `config.ini`

El archivo incluido `config.ini` contiene las opciones generales. Por ejemplo:

```ini
[TELEGRAM]
procesar_automatico = true
subir_mp4_telegram = false
canal_mp4 =

[STREAM]
compression_profile = small
calidades = 360p, 480p
guardar_en_todus = true
auto_reparar_todus = false
server_port = 24716
ffmpeg_threads = 0

[TODUS]
default_host = stickers
stickers_base_url = https://stickers.todus.cu
stream_base_url = https://s3.todus.cu/stream
hls_prefix = public/hls
```

Los cambios guardados en la página Settings se persisten en el archivo de configuración. `HLS_CANAL_MP4`, cuando se define, sigue teniendo prioridad sobre el canal guardado.

### 4. Iniciar el proyecto

Para iniciar el servidor web, el túnel y el bot con el lanzador:

```bash
python main.py
```

El servidor local escucha en el puerto `24716`; cuando el túnel de Cloudflare esté disponible, el lanzador envía la dirección al propietario por Telegram si están configurados `HLS_BOT_TOKEN` y `HLS_OWNER_ID`.

También puedes ejecutar los procesos por separado, por ejemplo, para desarrollo:

```bash
# Terminal 1: servidor web
python -m uvicorn hls_server:app --host 0.0.0.0 --port 24716

# Terminal 2: bot de Telegram
python hls_telegram.py
```

Abre `http://127.0.0.1:24716/` para el reproductor o `http://127.0.0.1:24716/settings` para la configuración.

---

## ⚙️ Settings

La página `/settings` organiza las opciones en pestañas:

- **Stream:** perfil de compresión y calidades para videos nuevos.
- **ToDus:** subida HLS y reparación automática de archivos faltantes.
- **Telegram:** subida opcional de MP4 y canal de destino.
- **Sistema:** uso de almacenamiento y diagnóstico de la base de datos.

La reparación automática de ToDus comprueba el almacenamiento cada 10 segundos desde el servidor. Sube los archivos que falten y conserva la carpeta y el host asignados al video. Para que pueda reparar un objeto, su archivo original debe seguir disponible localmente; no puede recuperar desde ToDus un archivo que falte tanto en la nube como en el disco.

La variable `HLS_CANAL_MP4` prevalece sobre el campo de canal de Settings. Para cambiar el destino desde la interfaz, elimina o desactiva esa variable de entorno y reinicia los procesos.

---

## 🤖 Comandos del bot

Los comandos están destinados al propietario configurado mediante `HLS_OWNER_ID`.

| Comando | Descripción |
|---|---|
| `/ping` | Comprobar que el bot responde. |
| `/reload` | Recargar los ajustes desde `config.ini`. |
| `/host` | Consultar el host ToDus activo. |
| `/host stickers` | Usar el host `stickers` para nuevas subidas. |
| `/host stream` | Usar el host `stream` para nuevas subidas. |
| `/streamStats` | Ver estadísticas del procesamiento y la cola. |
| `/streamQueue` | Consultar los videos en espera. |
| `/stopStream` | Vaciar la cola de procesamiento. |
| `/todusVerify` | Revisar la presencia de archivos HLS en ToDus. |
| `/todusVerify --solo` | Revisar solamente videos marcados como subidos a ToDus. |
| `/todusVerify --sync` | Verificar y sincronizar algunos estados de ToDus en la base de datos. |

---

## 🔌 Endpoints principales

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` o `/player` | Reproductor y lista de videos. |
| `GET` | `/settings` | Página de configuración. |
| `GET` | `/list` | Listado de videos. |
| `GET` | `/status/{media_id}` | Estado y datos del video. |
| `GET` | `/stream/{media_id}/master.m3u8` | Playlist HLS principal. |
| `GET` | `/download/{media_id}/{quality}` | Descargar MP4 de una calidad. |
| `GET` | `/api/settings/stream` | Consultar configuración. |
| `POST` | `/api/settings/stream` | Actualizar configuración. |
| `POST` | `/api/todus/upload/{media_id}` | Subir HLS a ToDus. |
| `DELETE` | `/api/todus/{media_id}` | Eliminar HLS de ToDus. |
| `DELETE` | `/media/{media_id}` | Eliminar media local, de ToDus o ambas. |

---

## 🗂️ Estructura del proyecto

```text
├── main.py              # Lanzador del servidor, túnel y bot
├── hls_server.py        # Servidor FastAPI, páginas y API
├── hls_telegram.py      # Bot privado, cola y procesamiento de videos
├── hls_core.py          # HLS, SQLite, FFmpeg e integración ToDus
├── todus_hls_helper.py  # Cliente ToDus S3 para archivos HLS
├── player.html          # Interfaz del reproductor web
├── settings.html        # Interfaz de configuración
├── config.ini           # Configuración local
├── .env                 # Variables de entorno privadas (crear localmente)
└── requirements.txt     # Dependencias de Python
```

La base de datos y los medios procesados se almacenan en `media/`.

---

## 🛠️ Solución de problemas

- **El bot no inicia:** verifica `HLS_API_ID`, `HLS_API_HASH`, `HLS_BOT_TOKEN` y `HLS_OWNER_ID`; asegúrate de que el bot no se esté ejecutando en otro proceso con la misma sesión.
- **El bot no recibe videos:** envía el video en un chat privado y confirma que la cuenta que lo envía coincide con `HLS_OWNER_ID` y que `procesar_automatico = true`.
- **No se suben MP4 al canal:** activa la subida en Settings o en `config.ini`, comprueba `HLS_CANAL_MP4` y confirma que el bot puede publicar en el canal o grupo.
- **No se generan videos:** instala FFmpeg y `ffprobe`, y confirma que ambos se pueden ejecutar desde una terminal.
- **La reparación no vuelve a subir un archivo:** comprueba que la opción está activa en Settings, que el servidor está funcionando y que los archivos HLS todavía existen en `media/`.
- **ToDus no está disponible:** revisa la conectividad, el host y las direcciones configuradas en la sección `[TODUS]` de `config.ini`.

---

## 🤝 Contribuciones

1. Haz un fork del repositorio.
2. Crea una rama: `git checkout -b feature/nueva-funcion`.
3. Implementa y prueba los cambios.
4. Sube la rama y abre un Pull Request.

---

## 📬 Contacto

Para dudas, sugerencias o reportar errores, abre un Issue en el repositorio.
