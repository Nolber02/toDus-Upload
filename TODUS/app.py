# make_launcher.py
import base64
import io
import os
import tarfile

FILES = [
    "bot.py", "runtime.py", "config.py", "config.ini", "db.py",
    "commands.py", "downloads.py", "callbacks.py", "keyboards.py",
    "state.py", "todus_helper.py", "transfer.py", "upload.py",
    "utils.py", "broadcast.py", "requirements.txt", "recovery.py",
]

def build():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in FILES:
            if os.path.exists(f):
                tar.add(f, arcname=f)
            else:
                print(f"[WARN] No existe: {f}")

    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    chunks = [b64[i:i + 120] for i in range(0, len(b64), 120)]

    code = '''import base64
import io
import os
import tarfile
import time

B64 = "".join([
'''
    for c in chunks:
        code += f'    "{c}",\n'
    code += ''' ])

def main():
    data = base64.b64decode(B64)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(".")

    os.makedirs("TODUS/temp_downloads", exist_ok=True)

    from runtime import iniciar_todus_bot
    iniciar_todus_bot()

    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
'''

    with open("deploy_bundle.py", "w", encoding="utf-8") as f:
        f.write(code)
    print("[OK] Generado deploy_bundle.py")

if __name__ == "__main__":
    build()