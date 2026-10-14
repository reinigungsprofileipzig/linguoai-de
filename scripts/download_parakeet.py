"""Download the documented sherpa-onnx NVIDIA v3 INT8 export, pinned and verified.

No arbitrary repository or executable is accepted. Downloads are atomic and each
large model file must match the publisher's LFS SHA-256 before it becomes usable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

REPOSITORY = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download(directory: Path, *, samples: bool = False) -> dict:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        f"https://huggingface.co/api/models/{REPOSITORY}?blobs=true",
        headers={"User-Agent": "LinguoAI-model-setup"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        metadata = json.load(response)
    revision = metadata["sha"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise RuntimeError("Invalid model revision.")
    published = {item["rfilename"]: item for item in metadata["siblings"]}
    names = [*MODEL_FILES, *(["test_wavs/en.wav", "test_wavs/de.wav"] if samples else [])]
    manifest = {
        "repository": REPOSITORY,
        "revision": revision,
        "files": {},
        "attribution": (
            "NVIDIA parakeet-tdt-0.6b-v3; sherpa-onnx conversion by csukuangfj; CC-BY-4.0"
        ),
        "model_url": "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
    }
    for name in names:
        info = published.get(name)
        if not info:
            if name.startswith("test_wavs/"):
                continue
            raise RuntimeError(f"Required model file not published: {name}")
        expected = info.get("lfs", {}).get("sha256")
        if name.endswith(".onnx") and not expected:
            raise RuntimeError(f"Publisher checksum missing: {name}")
        size = info.get("size", info.get("lfs", {}).get("size"))
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and (not size or target.stat().st_size == size):
            digest = sha256_file(target)
            if expected and digest == expected:
                manifest["files"][name] = {"sha256": digest, "size": target.stat().st_size}
                continue
        partial = target.with_name(target.name + ".partial")
        for attempt in range(3):
            try:
                digest = hashlib.sha256()
                consumed = 0
                url = f"https://huggingface.co/{REPOSITORY}/resolve/{revision}/{name}"
                with (
                    urllib.request.urlopen(url, timeout=90) as response,
                    partial.open("wb") as output,
                ):
                    while block := response.read(1024 * 1024):
                        consumed += len(block)
                        if consumed > 800 * 1024**2:
                            raise RuntimeError("Model file exceeds the expected size limit.")
                        digest.update(block)
                        output.write(block)
                actual = digest.hexdigest()
                if (size and consumed != size) or (expected and actual != expected):
                    raise RuntimeError(f"Model checksum/size mismatch: {name}")
                os.replace(partial, target)
                manifest["files"][name] = {"sha256": actual, "size": consumed}
                print(f"Verified {name}: {consumed / 1024**2:.1f} MB", flush=True)
                break
            except Exception:
                partial.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
    (directory / "MODEL_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Install verified Parakeet v3 CPU model")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--with-samples", action="store_true")
    args = parser.parse_args()
    download(args.directory, samples=args.with_samples)


if __name__ == "__main__":
    main()
