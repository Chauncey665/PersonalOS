#!/usr/bin/env python3
"""Explicit one-time local ASR setup. Never invoked by an audio request."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

REPO = "mlx-community/whisper-large-v3-turbo-8bit"
REVISION = "62103fc276a35fdc76e318f314d8ff47987fba89"
# mlx-whisper 0.4.3 expects this local weight filename. Only the filename changes;
# the safetensors bytes are checked against the publisher's LFS SHA-256.
MODEL_FILES = {"config.json": "config.json", "weights.safetensors": "model.safetensors"}
SUPPORT = Path.home() / "Library/Application Support/PersonalOS-OpenSource"
PYTHON = Path(sys.executable)
MODEL = SUPPORT / "models/whisper-large-v3-turbo-mlx"
VENV = SUPPORT / "speech-venv"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_digest(path):
    """Git's non-LFS blob identity includes the header, not just file bytes."""
    h = hashlib.sha1(b"blob " + str(path.stat().st_size).encode() + b"\0")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_data(url, partial, expected_size):
    if expected_size <= 32 * 1024 * 1024:
        with urllib.request.urlopen(url, timeout=60) as source, partial.open("wb") as destination:
            os.chmod(partial, 0o600)
            downloaded = 0
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                downloaded += len(block)
                if downloaded > expected_size:
                    raise RuntimeError("Model download exceeded declared size")
                destination.write(block)
        return
    # A bounded number of verified HTTP ranges avoids a slow single connection.
    # SHA-256 of the assembled data must still match the pinned LFS object.
    step = 16 * 1024 * 1024
    with partial.open("wb") as destination:
        os.chmod(partial, 0o600)
        destination.truncate(expected_size)
    ranges = [(start, min(start + step, expected_size) - 1) for start in range(0, expected_size, step)]
    def transfer(bounds):
        start, end = bounds
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"Range": "bytes={}-{}".format(start, end)})
                with urllib.request.urlopen(request, timeout=60) as source, partial.open("r+b") as target:
                    if source.status != 206 or source.headers.get("Content-Range") != "bytes {}-{}/{}".format(start, end, expected_size):
                        raise RuntimeError("Model range response did not match request")
                    target.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise RuntimeError("Model range was incomplete")
                        target.write(block)
                        remaining -= len(block)
                return end - start + 1
            except Exception:
                if attempt == 2:
                    raise RuntimeError("Model range transfer failed; rerun explicit setup") from None
                time.sleep(attempt + 1)
    received = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in as_completed([pool.submit(transfer, bounds) for bounds in ranges]):
            received += future.result()
            print("Model data: {} / {} MiB verified ranges received".format(received // 1048576, expected_size // 1048576), flush=True)


def setup_packages():
    if not PYTHON.is_file():
        raise RuntimeError("The selected Python interpreter is unavailable")
    if not (VENV / "bin/python").exists():
        subprocess.run([str(PYTHON), "-m", "venv", str(VENV)], check=True)
    python = str(VENV / "bin/python")
    subprocess.run([python, "-m", "pip", "install", "--index-url", "https://pypi.org/simple",
                    "--only-binary=:all:", "mlx-whisper==0.4.3", "mlx==0.32.4",
                    "opencc-python-reimplemented==0.1.7"], check=True)
    lock = subprocess.run([python, "-m", "pip", "freeze"], check=True, capture_output=True, text=True).stdout
    (SUPPORT / "speech-requirements.lock").write_text(lock)
    os.chmod(SUPPORT / "speech-requirements.lock", 0o600)
    print("Isolated speech packages installed; runtime service Python remains unchanged", flush=True)


def setup_model():
    MODEL.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata_url = "https://huggingface.co/api/models/{}/revision/{}?blobs=true".format(REPO, REVISION)
    with urllib.request.urlopen(metadata_url, timeout=30) as response:
        metadata = json.load(response)
    if metadata.get("sha") != REVISION:
        raise RuntimeError("Model revision did not match the pinned revision")
    siblings = {entry["rfilename"]: entry for entry in metadata.get("siblings", [])}
    files = {}
    for name, remote_name in MODEL_FILES.items():
        entry = siblings[remote_name]
        expected_size = entry.get("size")
        expected_hash = (entry.get("lfs") or {}).get("sha256")
        target = MODEL / name
        if not (target.is_file() and target.stat().st_size == expected_size and
                (digest(target) == expected_hash if expected_hash else
                 git_blob_digest(target) == entry.get("blobId"))):
            partial = MODEL / (name + ".partial")
            url = "https://huggingface.co/{}/resolve/{}/{}".format(REPO, REVISION, remote_name)
            print("Downloading {} at pinned revision ({} bytes)".format(name, expected_size), flush=True)
            try:
                download_data(url, partial, expected_size)
                if partial.stat().st_size != expected_size:
                    raise RuntimeError("Model download was incomplete")
                if expected_hash and digest(partial) != expected_hash:
                    raise RuntimeError("Model SHA-256 validation failed")
                if not expected_hash:
                    if git_blob_digest(partial) != entry.get("blobId"):
                        raise RuntimeError("Model configuration Git blob validation failed")
                partial.replace(target)
            finally:
                if partial.exists():
                    partial.unlink()
        files[name] = {"source_filename": remote_name, "size": target.stat().st_size, "sha256": digest(target)}
    manifest = {"repo": REPO, "revision": REVISION, "data_files_only": True, "files": files}
    manifest_path = MODEL / "personalos-model-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(manifest_path, 0o600)
    print("Pinned local model ready: " + str(MODEL), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("all", "packages", "model"), default="all")
    parser.add_argument('--home',default=str(Path.home()/'Library/Application Support/PersonalOS-OpenSource'))
    args = parser.parse_args()
    global MODEL,VENV,SUPPORT
    support=Path(args.home).expanduser().resolve()
    SUPPORT=support
    MODEL=support/'models/whisper-large-v3-turbo-mlx';VENV=support/'speech-venv'
    support.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.phase in ("all", "packages"):
        setup_packages()
    if args.phase in ("all", "model"):
        setup_model()


if __name__ == "__main__":
    main()
