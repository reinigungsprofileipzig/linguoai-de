"""Erstellt die direkt über linguoai.de ausgelieferten Release-Artefakte.

Das Script hält das Colab-Notebook, das installierbare Wheel und das Quellcode-ZIP
im Website-Downloadordner synchron. Es schreibt keine Zugangsdaten in die Artefakte.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website" if (ROOT / "website").is_dir() else ROOT
DOWNLOADS = WEBSITE / "downloads"
NOTEBOOK = ROOT / "notebooks" / "LinguoAI_Colab.ipynb"
ROOT_FILES = (
    ".gitattributes",
    ".gitignore",
    "CHANGELOG.md",
    "README.md",
    "main.py",
    "pyproject.toml",
)
SOURCE_DIRECTORIES = ("linguoai", "notebooks", "scripts", "tests")
IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "linguoai.egg-info",
}


def project_version() -> str:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', pyproject, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("Projektversion in pyproject.toml nicht gefunden.")
    return match.group(1)


def source_files() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES]
    for directory_name in SOURCE_DIRECTORIES:
        directory = ROOT / directory_name
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and not any(part in IGNORED_PARTS for part in path.relative_to(ROOT).parts)
        )
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise RuntimeError(f"Release-Dateien fehlen: {missing}")
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def build_source_zip(destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_files():
            archive.write(path, path.relative_to(ROOT).as_posix())


def build_wheel(destination: Path, version: str, source_zip: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="linguoai-wheel-") as temporary:
        workspace = Path(temporary)
        source_dir = workspace / "source"
        output_dir = workspace / "wheels"
        # Build only the just-created source payload, never a stale local build/lib.
        with zipfile.ZipFile(source_zip) as archive:
            archive.extractall(source_dir)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(output_dir),
            ],
            cwd=source_dir,
            check=True,
        )
        wheels = list(output_dir.glob(f"linguoai-{version}-py3-none-any.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Erwartetes Wheel nicht eindeutig: {wheels}")
        shutil.copy2(wheels[0], destination)


def verify_artifacts(source_zip: Path, wheel: Path) -> None:
    with zipfile.ZipFile(source_zip) as archive:
        required = {
            "pyproject.toml",
            "linguoai/__init__.py",
            "linguoai/colab_ui.py",
            "notebooks/LinguoAI_Colab.ipynb",
        }
        names = set(archive.namelist())
        missing = required - names
        if missing:
            raise RuntimeError(f"Source-ZIP ist unvollständig: {sorted(missing)}")
        if archive.read("notebooks/LinguoAI_Colab.ipynb") != NOTEBOOK.read_bytes():
            raise RuntimeError("Source-ZIP enthält nicht das aktuelle Colab-Notebook.")
        for path in source_files():
            if archive.read(path.relative_to(ROOT).as_posix()) != path.read_bytes():
                raise RuntimeError(f"Veraltete Quelldatei im ZIP: {path.name}")

    with zipfile.ZipFile(wheel) as archive:
        if "linguoai/colab_ui.py" not in archive.namelist():
            raise RuntimeError("Wheel enthält die Colab-Oberfläche nicht.")
        expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / "linguoai").rglob("*.py")}
        actual = {name for name in archive.namelist() if name.endswith(".py")}
        if actual != expected:
            raise RuntimeError("Wheel enthält eine abweichende Modulmenge.")
        for name in expected:
            if archive.read(name) != (ROOT / name).read_bytes():
                raise RuntimeError(f"Veraltetes Modul im Wheel: {name}")
        metadata = archive.read(f"linguoai-{project_version()}.dist-info/METADATA").decode()
        if f"Version: {project_version()}" not in metadata.splitlines():
            raise RuntimeError("Wheel-Version stimmt nicht mit pyproject.toml überein.")


def main() -> None:
    version = project_version()
    init_source = (ROOT / "linguoai" / "__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{version}"' not in init_source:
        raise RuntimeError("Paketversion und pyproject.toml stimmen nicht überein.")
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    website_notebook = DOWNLOADS / NOTEBOOK.name
    source_zip = DOWNLOADS / f"LinguoAI-{version}-source.zip"
    wheel = DOWNLOADS / f"linguoai-{version}-py3-none-any.whl"

    # Verify everything before replacing any public download. Same-volume staging
    # makes each replacement atomic; an unsuccessful build leaves the release intact.
    with tempfile.TemporaryDirectory(prefix=".release-", dir=DOWNLOADS) as temporary:
        staged_zip = Path(temporary) / source_zip.name
        staged_wheel = Path(temporary) / wheel.name
        staged_notebook = Path(temporary) / NOTEBOOK.name
        shutil.copy2(NOTEBOOK, staged_notebook)
        build_source_zip(staged_zip)
        build_wheel(staged_wheel, version, staged_zip)
        verify_artifacts(staged_zip, staged_wheel)
        checksum_file = Path(temporary) / f"SHA256SUMS-{version}.txt"
        checksum_file.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                for path in (staged_zip, staged_wheel, staged_notebook)
            ),
            encoding="utf-8",
            newline="\n",
        )
        for path in (staged_zip, staged_wheel, staged_notebook, checksum_file):
            path.replace(DOWNLOADS / path.name)

    print(f"Notebook: {website_notebook.relative_to(ROOT)}")
    print(f"Source ZIP: {source_zip.relative_to(ROOT)}")
    print(f"Wheel: {wheel.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
