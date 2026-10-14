# @title LinguoAI 2.2.1 installieren und starten
# Quelltext der einzigen Startzelle; Notebook mit build_notebook.py erzeugen.
import importlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from getpass import getpass
from pathlib import Path

PROJECT_SOURCE = "download_zip"  # @param ["download_zip", "upload_zip", "git"]
SOURCE_ZIP_URL = "https://linguoai.de/downloads/LinguoAI-2.2.1-source.zip"  # @param {type:"string"}
GIT_URL = ""  # @param {type:"string"}
GIT_REF = ""  # @param {type:"string"}
SHARE_PROVIDER = "gradio"  # @param ["gradio", "cloudflare"]
CLOUDFLARE_TUNNEL_HOSTNAME = ""  # @param {type:"string"}
MOUNT_GOOGLE_DRIVE = False  # @param {type:"boolean"}
CACHE_MODELS_IN_DRIVE = False  # @param {type:"boolean"}
INSTALL_PARAKEET = False  # @param {type:"boolean"}
LOCAL_TTS_EXTRA = "none"  # @param ["none", "piper", "chatterbox"]
MAX_SOURCE_BYTES = 500 * 1024**2

if SHARE_PROVIDER not in {"gradio", "cloudflare"}:
    raise ValueError("SHARE_PROVIDER muss gradio oder cloudflare sein.")
if LOCAL_TTS_EXTRA not in {"none", "piper", "chatterbox"}:
    raise ValueError("LOCAL_TTS_EXTRA muss none, piper oder chatterbox sein.")
if not Path("/content").is_dir():
    raise RuntimeError("Diese Startzelle ist für Google Colab vorgesehen.")

tunnel_token = ""
if SHARE_PROVIDER == "cloudflare" and not CLOUDFLARE_TUNNEL_HOSTNAME.strip():
    raise ValueError("Trage die eigene HTTPS-Adresse deines Cloudflare-Tunnels ein.")


def read_tunnel_token() -> str:
    token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    if not token:
        try:
            from google.colab import userdata

            token = (userdata.get("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
        except Exception:
            pass
    if not token:
        token = getpass("Cloudflare Tunnel Token (verdeckt): ").strip()
    if not token:
        raise RuntimeError("Cloudflare benötigt den Token deines benannten Tunnels.")
    return token


print("[1/5] Colab-Umgebung vorbereiten")
if MOUNT_GOOGLE_DRIVE:
    from google.colab import drive

    drive.mount("/content/drive", force_remount=False)
model_cache = (
    Path("/content/drive/MyDrive/LinguoAI/model-cache")
    if CACHE_MODELS_IN_DRIVE and MOUNT_GOOGLE_DRIVE
    else Path("/content/linguoai-model-cache")
)
model_cache.mkdir(parents=True, exist_ok=True)
os.environ["HF_HOME"] = str(model_cache)
os.environ["LINGUOAI_MODEL_CACHE"] = str(model_cache)


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_SOURCE_BYTES:
            raise ValueError("Projekt-ZIP ist ungewöhnlich groß.")
        for info in entries:
            member = Path(info.filename)
            target = (root / member).resolve()
            mode = (info.external_attr >> 16) & 0o170000
            if member.is_absolute() or ".." in member.parts or not target.is_relative_to(root):
                raise ValueError(f"Unsicherer ZIP-Pfad: {info.filename}")
            if mode == stat.S_IFLNK:
                raise ValueError(f"Symlink im ZIP ist nicht erlaubt: {info.filename}")
        archive.extractall(root)


print("[2/5] LinguoAI-Quellcode laden")
# Ein frischer Ordner schützt frühere Sitzungen und verhindert vermischte Versionen.
project_area = Path(tempfile.mkdtemp(prefix="linguoai-2.2.1-", dir="/content"))
archive_path = project_area / "project.zip"
extraction = project_area / "source"
if PROJECT_SOURCE == "git":
    if not GIT_URL.startswith("https://"):
        raise ValueError("GIT_URL muss eine HTTPS-Adresse eines eigenen Projekts sein.")
    subprocess.run(
        ["git", "clone", "--filter=blob:none", "--", GIT_URL, str(extraction)], check=True
    )
    if GIT_REF.strip():
        subprocess.run(
            ["git", "-C", str(extraction), "fetch", "--depth", "1", "origin", GIT_REF],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(extraction), "checkout", "--detach", "FETCH_HEAD"], check=True
        )
elif PROJECT_SOURCE == "download_zip":
    if not SOURCE_ZIP_URL.startswith("https://"):
        raise ValueError("SOURCE_ZIP_URL muss eine HTTPS-Adresse sein.")
    request = urllib.request.Request(SOURCE_ZIP_URL, headers={"User-Agent": "LinguoAI-Colab/2.2.1"})
    downloaded = 0
    with urllib.request.urlopen(request, timeout=120) as response, archive_path.open("wb") as out:
        if not response.geturl().startswith("https://"):
            raise ValueError("Unsichere Weiterleitung des Projekt-Downloads.")
        if int(response.headers.get("Content-Length", "0") or "0") > MAX_SOURCE_BYTES:
            raise ValueError("Projekt-ZIP ist ungewöhnlich groß.")
        while chunk := response.read(1024 * 1024):
            downloaded += len(chunk)
            if downloaded > MAX_SOURCE_BYTES:
                raise ValueError("Projekt-ZIP ist ungewöhnlich groß.")
            out.write(chunk)
    safe_extract_zip(archive_path, extraction)
elif PROJECT_SOURCE == "upload_zip":
    from google.colab import files

    print("Bitte LinguoAI-2.2.1-source.zip auswählen.")
    uploaded = files.upload()
    archives = [(name, data) for name, data in uploaded.items() if name.lower().endswith(".zip")]
    if len(archives) != 1 or len(archives[0][1]) > MAX_SOURCE_BYTES:
        raise ValueError("Bitte genau ein Projekt-ZIP unter 500 MiB hochladen.")
    archive_path.write_bytes(archives[0][1])
    safe_extract_zip(archive_path, extraction)
else:
    raise ValueError("Unbekannte Projektquelle.")

candidates = [
    path.parent
    for path in extraction.rglob("pyproject.toml")
    if (path.parent / "linguoai" / "colab_ui.py").is_file()
]
if len(candidates) != 1:
    raise RuntimeError(f"Projektwurzel nicht eindeutig gefunden: {candidates}")
project_root = candidates[0]

print("[3/5] System- und Python-Abhängigkeiten installieren")
if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    subprocess.run(["apt-get", "update", "-qq"], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "ffmpeg"], check=True)
if SHARE_PROVIDER == "cloudflare" and shutil.which("cloudflared") is None:
    cloudflared_install = r"""
set -euo pipefail
apt-get update -qq
apt-get install -y -qq curl ca-certificates
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  -o /usr/share/keyrings/cloudflare-main.gpg
printf '%s\n' \
  'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg]' \
  'https://pkg.cloudflare.com/cloudflared any main' \
  | paste -sd ' ' > /etc/apt/sources.list.d/cloudflared.list
apt-get update -qq
apt-get install -y -qq cloudflared
"""
    subprocess.run(["bash", "-c", cloudflared_install], check=True)

extras = ["colab"]
if INSTALL_PARAKEET:
    extras.append("parakeet")
if LOCAL_TTS_EXTRA == "piper":
    extras.append("piper")
elif LOCAL_TTS_EXTRA == "chatterbox":
    extras.append("voice-clone")
subprocess.run(
    [sys.executable, "-m", "pip", "install", "-q", "-e", f"{project_root}[{','.join(extras)}]"],
    check=True,
)
if LOCAL_TTS_EXTRA == "piper" and shutil.which("nvidia-smi"):
    subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-q", "-y", "onnxruntime", "onnxruntime-gpu"],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "onnxruntime-gpu>=1.20,<2"], check=True
    )

print("[4/5] Import und Version prüfen")
for module_name in list(sys.modules):
    if module_name == "linguoai" or module_name.startswith("linguoai."):
        del sys.modules[module_name]
sys.path.insert(0, str(project_root))
importlib.invalidate_caches()
from linguoai import __version__  # noqa: E402
from linguoai.colab_ui import launch_colab  # noqa: E402

if __version__ != "2.2.1":
    raise RuntimeError(f"Erwartet LinguoAI 2.2.1, geladen wurde {__version__}.")
print("LinguoAI", __version__, "ist bereit.")
print("[5/5] Geschützte Oberfläche starten")
print("Die Zelle bleibt aktiv. Ergebnisse vor Ende der Colab-Sitzung herunterladen.")
try:
    # Secrets erst nach Download/Installation lesen, innerhalb des Cleanup-Blocks.
    if SHARE_PROVIDER == "cloudflare":
        tunnel_token = read_tunnel_token()
    launch_colab(
        share_provider=SHARE_PROVIDER,
        tunnel_token=tunnel_token or None,
        tunnel_hostname=CLOUDFLARE_TUNNEL_HOSTNAME or None,
    )
finally:
    tunnel_token = ""
